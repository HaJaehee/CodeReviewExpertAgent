"""compile_commands.json 생성 검증.

MSBuild 경로는 이 저장소의 CI(리눅스 컨테이너)에서 실행할 수 없다. 그래서
**명령 조립까지**를 테스트한다 — 로거를 붙이는 방식, Rebuild 기본값, verbosity
같은 것들이다. 이 셋 중 하나만 틀려도 결과가 조용히 비거나 반쪽이 되고,
조용히 틀리는 실패가 이 프로젝트에서 제일 비싸다.

CMake 경로는 cmake 가 있으면 실제로 돌린다. 없으면 그 테스트만 건너뛴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.compiledb import (  # noqa: E402
    DEFAULT_TARGET,
    CompileDbError,
    build_cmake_command,
    ensure_logger,
    split_batched_commands,
    build_msbuild_command,
    detect_project,
    generate,
    prepare_output_dir,
    vendored_dir,
)
from crex.config import load_config  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="crex-compiledb-"))


# --------------------------------------------------------------------------
# 탐지
# --------------------------------------------------------------------------


def test_cmake_wins_over_generated_solution() -> None:
    """CMake 가 만들어낸 .sln 이 같이 있어도 원본인 CMake 쪽을 잡아야 한다.

    .sln 을 잡으면 전체 Rebuild 를 돌리게 된다 — CMake 쪽은 구성만 하면 끝나는데도.
    """
    root = _tmp()
    (root / "CMakeLists.txt").write_text("project(x)\n", encoding="utf-8")
    (root / "demo.sln").write_text("", encoding="utf-8")

    found = detect_project(root)
    _check(found.kind == "cmake", f"kind={found.kind}")
    _check(found.path.name == "CMakeLists.txt", f"path={found.path}")


def test_single_solution_is_found() -> None:
    root = _tmp()
    (root / "App.sln").write_text("", encoding="utf-8")

    found = detect_project(root)
    _check(found.kind == "msbuild", f"kind={found.kind}")
    _check(found.path.name == "App.sln", f"path={found.path}")


def test_vcxproj_one_level_down_is_found() -> None:
    """.sln 없이 프로젝트 파일만 하위 폴더에 두는 배치도 흔하다."""
    root = _tmp()
    (root / "src").mkdir()
    (root / "src" / "App.vcxproj").write_text("", encoding="utf-8")

    found = detect_project(root)
    _check(found.kind == "msbuild", f"kind={found.kind}")
    _check(found.path.name == "App.vcxproj", f"path={found.path}")


def test_ambiguous_solutions_ask_instead_of_guessing() -> None:
    """둘 중 하나를 몰래 고르면 엉뚱한 프로젝트의 DB 가 만들어진다."""
    root = _tmp()
    (root / "A.sln").write_text("", encoding="utf-8")
    (root / "B.sln").write_text("", encoding="utf-8")

    try:
        detect_project(root)
    except CompileDbError as exc:
        _check("--project" in str(exc), f"다음 행동이 없다: {exc}")
    else:
        raise AssertionError("여러 개인데 그냥 골랐다")


def test_nothing_found_says_what_to_do() -> None:
    try:
        detect_project(_tmp())
    except CompileDbError as exc:
        message = str(exc)
        _check("--project" in message and "--workspace" in message, f"안내가 없다: {message}")
    else:
        raise AssertionError("아무것도 없는데 성공했다")


def test_explicit_project_is_resolved_against_root() -> None:
    root = _tmp()
    (root / "sub").mkdir()
    (root / "sub" / "App.vcxproj").write_text("", encoding="utf-8")

    found = detect_project(root, "sub/App.vcxproj")
    _check(found.kind == "msbuild", f"kind={found.kind}")
    _check(found.path == root / "sub" / "App.vcxproj", f"path={found.path}")


# --------------------------------------------------------------------------
# 명령 조립
# --------------------------------------------------------------------------


def test_msbuild_command_attaches_logger_and_rebuilds() -> None:
    command = build_msbuild_command(
        Path("C:/vs/MSBuild.exe"),
        Path("C:/work/App.sln"),
        Path("C:/work/.crex/compiledb/CompileCommandsJson.dll"),
        Path("C:/work/.crex/compiledb/compile_commands.json.partial"),
    )
    joined = " ".join(command)

    logger = [arg for arg in command if arg.startswith("/logger:")]
    _check(len(logger) == 1, f"/logger 가 하나가 아니다: {command}")
    # MSBuild 의 로거 문법은 `어셈블리;파라미터` 다. 세미콜론이 빠지면 로거는
    # 붙는데 출력 경로가 기본값(현재 디렉터리)으로 가서 결과를 못 찾는다.
    _check(";" in logger[0], f"출력 경로가 안 붙었다: {logger[0]}")
    _check(logger[0].endswith("compile_commands.json.partial"), f"{logger[0]}")

    _check(f"/t:{DEFAULT_TARGET}" in command, f"타깃이 Rebuild 가 아니다: {command}")
    _check("/p:Configuration=Debug" in command, joined)
    _check("/p:Platform=x64" in command, joined)
    # detailed 미만이면 CL 명령줄 이벤트가 로거까지 오지 않을 수 있다.
    _check("/v:detailed" in command, f"verbosity 가 낮다: {command}")
    # 병렬은 기본으로 켜지 않는다 — 이벤트 전달이 걸러지면 DB 가 조용히 반쪽이 된다.
    _check("/m" not in command, f"/m 이 기본으로 들어갔다: {command}")


def test_msbuild_command_passes_extra_args_last() -> None:
    command = build_msbuild_command(
        Path("msbuild"), Path("App.sln"), Path("logger.dll"), Path("out.json"),
        platform=None, extra=("/m", "/p:Foo=1"),
    )
    _check(command[-2:] == ["/m", "/p:Foo=1"], f"{command}")
    _check(not any(arg.startswith("/p:Platform") for arg in command), f"{command}")


def test_cmake_command_forces_ninja_and_export() -> None:
    """Visual Studio 제너레이터는 CMAKE_EXPORT_COMPILE_COMMANDS 를 무시한다."""
    command = build_cmake_command(Path("cmake"), Path("/src"), Path("/build"))
    _check("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in command, f"{command}")
    _check("-G" in command and command[command.index("-G") + 1] == "Ninja", f"{command}")
    # 구성만 한다. 빌드까지 가면 몇십 분이 더 걸리는데 얻는 게 없다.
    _check("--build" not in command, f"{command}")


# --------------------------------------------------------------------------
# 산출물 자리
# --------------------------------------------------------------------------


def test_output_dir_ignores_itself_in_git() -> None:
    """남의 저장소 .gitignore 를 건드리지 않고, 그렇다고 더럽히지도 않는다."""
    root = _tmp()
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=False)

    directory = prepare_output_dir(root)
    _check(directory.is_dir(), f"{directory} 가 없다")
    (directory / "compile_commands.json").write_text("[]", encoding="utf-8")

    _check((root / ".crex" / ".gitignore").read_text(encoding="utf-8").strip() == "*",
           "자기 자신을 무시하지 않는다")
    _check(not (root / ".gitignore").exists(), "사용자의 .gitignore 를 건드렸다")

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root), capture_output=True, text=True, check=False,
    )
    _check(".crex" not in status.stdout, f"git 에 잡힌다: {status.stdout!r}")


def test_custom_output_dir_is_honored() -> None:
    root = _tmp()
    directory = prepare_output_dir(root, "build/db")
    _check(directory == root / "build" / "db", f"{directory}")
    _check(directory.is_dir(), "만들어지지 않았다")
    # .crex 가 아닌 자리에는 무시 표시를 만들지 않는다 — 사용자가 고른 자리다.
    _check(not (root / "build" / ".gitignore").exists(), "남의 자리에 .gitignore 를 만들었다")


# --------------------------------------------------------------------------
# 실제 실행 (cmake 가 있을 때만)
# --------------------------------------------------------------------------


def test_cmake_route_end_to_end() -> None:
    if shutil.which("cmake") is None or shutil.which("ninja") is None:
        print("     (cmake/ninja 없음 — 건너뜀)")
        return

    root = _tmp()
    (root / "src").mkdir()
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(demo CXX)\n"
        "add_library(demo src/a.cpp src/b.cpp)\n",
        encoding="utf-8",
    )
    (root / "src" / "a.cpp").write_text("int a(){return 0;}\n", encoding="utf-8")
    (root / "src" / "b.cpp").write_text("int b(){return 1;}\n", encoding="utf-8")

    result = generate(root)
    _check(result.entries == 2, f"엔트리 {result.entries}개")
    _check(result.json_path.is_file(), f"{result.json_path} 가 없다")

    entries = json.loads(result.json_path.read_text(encoding="utf-8"))
    files = sorted(Path(entry["file"]).name for entry in entries)
    _check(files == ["a.cpp", "b.cpp"], f"{files}")

    # clang-tidy 에 넘길 값은 디렉터리다. 파일을 적으면 -p 가 조용히 실패한다.
    _check(result.directory == result.json_path.parent, f"{result.directory}")


def test_cli_writes_the_path_into_config() -> None:
    """만들어만 주고 설정은 사람이 적게 두면, 거기서 절반이 떨어져 나간다."""
    if shutil.which("cmake") is None or shutil.which("ninja") is None:
        print("     (cmake/ninja 없음 — 건너뜀)")
        return

    from crex.cli import main

    root = _tmp()
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(demo CXX)\nadd_library(demo a.cpp)\n",
        encoding="utf-8",
    )
    (root / "a.cpp").write_text("int a(){return 0;}\n", encoding="utf-8")

    code = main(["compiledb", "--workspace", str(root)])
    _check(code == 0, f"종료 코드 {code}")

    config = load_config(root / "crex.json")
    _check(config.grounding.compile_commands_dir == ".crex/compiledb",
           f"적힌 값: {config.grounding.compile_commands_dir!r}")

    # 상대경로로 적어야 분석기 cwd(워크스페이스 루트) 기준으로 그대로 맞는다.
    written = root / config.grounding.compile_commands_dir / "compile_commands.json"
    _check(written.is_file(), f"{written} 을 가리키지 않는다")



def test_output_is_streamed_line_by_line() -> None:
    """관제 화면은 이 콜백으로 진행 상황을 중계한다.

    빌드가 몇십 분인데 화면이 조용하면 사람은 멈춘 줄 알고 끊는다. 실제 도구를
    부르지 않고 파이썬으로 대신 출력만 내 본다 — 여기서 확인할 것은 중계 배선이지
    MSBuild 가 아니다.
    """
    from crex.compiledb import _run

    root = _tmp()
    seen: list[str] = []
    log_path = root / "run.log"
    _run(
        [sys.executable, "-c", "print('첫 줄'); print('둘째 줄')"],
        what="테스트",
        cwd=root,
        capture=True,
        log_path=log_path,
        on_line=seen.append,
    )

    _check(seen == ["첫 줄", "둘째 줄"], f"{seen}")
    # 콜백이 있어도 전문은 여전히 파일로 남아야 한다 — 화면은 꼬리만 들고 있다.
    _check(log_path.read_text(encoding="utf-8").splitlines() == seen, log_path.read_text(encoding="utf-8"))


def test_utf8_output_survives_a_cp949_console() -> None:
    """빌드 로그가 깨지면 실패 원인을 읽을 수 없다.

    한국어 Windows 의 시스템 기본 코드페이지는 cp949 인데 MSBuild 는 파이프로
    UTF-8 을 내보낸다. 시스템 기본값으로 읽으면 로그가 통째로 깨진 글자가 된다 —
    실제로 그렇게 나왔다(2026-08-29).
    """
    from crex.compiledb import _run

    root = _tmp()
    seen: list[str] = []
    _run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write('경과 시간: 0.9\\n'.encode('utf-8'))"],
        what="테스트",
        cwd=root,
        capture=True,
        on_line=seen.append,
    )
    _check(seen == ["경과 시간: 0.9"], f"{seen}")

    # 어느 인코딩도 아닌 바이트가 섞여도 빌드를 세우지 않는다.
    seen.clear()
    _run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe ok\\n')"],
        what="테스트",
        cwd=root,
        capture=True,
        on_line=seen.append,
    )
    _check(len(seen) == 1 and seen[0].endswith("ok"), f"{seen}")


def test_cancel_stops_the_build_and_says_so() -> None:
    """중단은 실패와 구분되어야 한다. 화면이 '실패'로 적으면 사람이 원인을 찾아 나선다."""
    from crex.compiledb import CompileDbCancelled, _run

    root = _tmp()
    script = (
        "import sys, time\n"
        "for i in range(1000):\n"
        "    print(i, flush=True)\n"
        "    time.sleep(0.01)\n"
    )
    seen: list[str] = []
    try:
        _run(
            [sys.executable, "-c", script],
            what="테스트",
            cwd=root,
            capture=True,
            on_line=seen.append,
            cancel=lambda: len(seen) >= 3,
        )
    except CompileDbCancelled as exc:
        _check("중단" in str(exc), str(exc))
    else:
        raise AssertionError("중단했는데 예외가 안 나왔다")

    _check(len(seen) < 1000, f"끝까지 돌았다: {len(seen)}줄")


def test_status_agrees_with_what_was_built() -> None:
    """doctor 와 관제 화면이 같은 판정을 쓰게 하는 함수다. 세 상태를 다 본다."""
    from crex.compiledb import describe_status

    root = _tmp()
    empty = describe_status(None, root)
    _check(empty["configured"] is None and empty["error"] is None, f"{empty}")

    missing = describe_status(".crex/compiledb", root)
    _check(missing["exists"] is False, f"{missing}")
    _check("없습니다" in (missing["error"] or ""), str(missing["error"]))

    directory = root / ".crex" / "compiledb"
    directory.mkdir(parents=True)
    (directory / "compile_commands.json").write_text(
        json.dumps([{"file": "a.cpp"}, {"file": "b.cpp"}]), encoding="utf-8"
    )
    good = describe_status(".crex/compiledb", root)
    _check(good["exists"] and good["entries"] == 2 and good["error"] is None, f"{good}")

    (directory / "compile_commands.json").write_text("{깨진 JSON", encoding="utf-8")
    broken = describe_status(".crex/compiledb", root)
    _check(broken["exists"] is False and broken["error"], f"{broken}")


def test_ensure_logger_returns_the_bundled_dll() -> None:
    """로거는 빌드하지 않는다. 담겨 온 DLL 을 그대로 쓴다."""
    dll = ensure_logger()
    _check(dll.is_file(), f"로거 DLL 이 없다: {dll}")
    _check(dll.parent == vendored_dir(), f"번들 밖을 가리킨다: {dll}")
    # .NET 어셈블리는 PE 파일이다. 잘못된 파일이 들어오면 여기서 걸린다.
    _check(dll.read_bytes()[:2] == b"MZ", "PE 파일이 아니다")


def test_batched_cl_command_is_split_per_file() -> None:
    """MSBuild 는 설정이 같은 .cpp 를 모아 cl.exe 를 한 번만 부른다.

    로거는 그 명령을 항목마다 그대로 적으므로 명령줄에 남의 소스가 남는다.
    clang-tidy 는 그걸 받으면 "expected exactly one compiler job" 으로 파일을
    통째로 포기하는데, 그 실패는 진단 형식이 아니라서 파서에 걸리지 않는다 —
    지적 0건으로 보인다. 실제 .vcxproj 로 재현해서 잡은 항목이다.
    """
    batched = r'CL.exe /c /I"C:\repo\include" /D CREX=1 src\buffer.cpp src\main.cpp'
    entries = [
        {"file": r"src\buffer.cpp", "directory": r"C:\repo", "command": batched},
        {"file": r"src\main.cpp", "directory": r"C:\repo", "command": batched},
    ]
    changed = split_batched_commands(entries)
    _check(changed == 2, f"2건을 바꿨어야 한다: {changed}")

    first = entries[0]["command"]
    _check("buffer.cpp" in first, f"자기 파일이 사라졌다: {first}")
    _check("main.cpp" not in first, f"남의 파일이 남았다: {first}")
    # 컴파일 옵션은 손대지 않는다 — include 경로가 빠지면 clang-tidy 가 반쯤 눈을 감는다.
    _check(r'/I"C:\repo\include"' in first, f"include 경로가 사라졌다: {first}")
    _check("/D CREX=1" in first, f"매크로 정의가 사라졌다: {first}")

    second = entries[1]["command"]
    _check("main.cpp" in second and "buffer.cpp" not in second, f"{second}")


def test_unbatched_command_is_left_alone() -> None:
    """파일 하나짜리 명령은 건드리지 않는다. 바꿀 이유가 없다."""
    entries = [{"file": "src/a.cpp", "directory": "/repo", "command": "clang++ -c src/a.cpp"}]
    _check(split_batched_commands(entries) == 0, "멀쩡한 명령을 바꿨다")
    _check(entries[0]["command"] == "clang++ -c src/a.cpp", f'{entries[0]["command"]}')


def test_logger_dll_is_bundled_with_its_license() -> None:
    """반입 번들에서 tools/ 가 빠지면 MSBuild 경로 전체가 죽는다.

    LICENSE 도 같이 본다 — MIT 는 바이너리로 배포할 때도 라이선스 고지를 함께
    두라고 요구한다. 소스를 지웠다고 고지 의무까지 사라지지 않는다.
    """
    bundle = vendored_dir()
    for name in ("CompileCommandsJson.dll", "LICENSE"):
        _check((bundle / name).is_file(), f"{name} 이 없다")


TESTS = [
    test_cmake_wins_over_generated_solution,
    test_single_solution_is_found,
    test_vcxproj_one_level_down_is_found,
    test_ambiguous_solutions_ask_instead_of_guessing,
    test_nothing_found_says_what_to_do,
    test_explicit_project_is_resolved_against_root,
    test_msbuild_command_attaches_logger_and_rebuilds,
    test_msbuild_command_passes_extra_args_last,
    test_output_is_streamed_line_by_line,
    test_utf8_output_survives_a_cp949_console,
    test_cancel_stops_the_build_and_says_so,
    test_status_agrees_with_what_was_built,
    test_ensure_logger_returns_the_bundled_dll,
    test_batched_cl_command_is_split_per_file,
    test_unbatched_command_is_left_alone,
    test_cmake_command_forces_ninja_and_export,
    test_logger_dll_is_bundled_with_its_license,
    test_output_dir_ignores_itself_in_git,
    test_custom_output_dir_is_honored,
    test_cmake_route_end_to_end,
    test_cli_writes_the_path_into_config,
]


def main() -> int:
    from crex.cli import force_utf8_output

    force_utf8_output()
    failures = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
