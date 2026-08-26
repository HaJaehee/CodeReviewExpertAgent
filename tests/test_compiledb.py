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
    build_logger_command,
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


def test_logger_output_path_ends_with_separator() -> None:
    """MSBuild 는 OutputPath 끝에 구분자가 없으면 파일 이름으로 본다."""
    import os

    command = build_logger_command(Path("msbuild"), Path("logger.csproj"), Path("/out/dir"))
    out = [arg for arg in command if arg.startswith("/p:OutputPath=")]
    _check(len(out) == 1, f"{command}")
    _check(out[0].endswith(os.sep), f"구분자가 없다: {out[0]}")


def test_cmake_command_forces_ninja_and_export() -> None:
    """Visual Studio 제너레이터는 CMAKE_EXPORT_COMPILE_COMMANDS 를 무시한다."""
    command = build_cmake_command(Path("cmake"), Path("/src"), Path("/build"))
    _check("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in command, f"{command}")
    _check("-G" in command and command[command.index("-G") + 1] == "Ninja", f"{command}")
    # 구성만 한다. 빌드까지 가면 몇십 분이 더 걸리는데 얻는 게 없다.
    _check("--build" not in command, f"{command}")


def test_logger_sources_are_bundled() -> None:
    """반입 번들에서 tools/ 가 빠지면 MSBuild 경로 전체가 죽는다."""
    source = vendored_dir()
    for name in ("CompileCommandsJson.cs", "CompileCommandsJson.crex.csproj", "LICENSE"):
        _check((source / name).is_file(), f"{name} 이 없다")


def test_vendored_csproj_needs_no_nuget() -> None:
    """PackageReference 가 하나라도 있으면 폐쇄망에서 복원 단계에서 죽는다."""
    text = (vendored_dir() / "CompileCommandsJson.crex.csproj").read_text(encoding="utf-8")
    # 주석에는 이 낱말이 나온다. 실제 요소가 있는지를 본다.
    _check("<PackageReference" not in text, "NuGet 복원이 필요한 참조가 들어 있다")
    _check("$(MSBuildToolsPath)" in text, "MSBuild 어셈블리를 설치본에서 가리키지 않는다")


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

    config = load_config(root / "crex.toml")
    _check(config.grounding.compile_commands_dir == ".crex/compiledb",
           f"적힌 값: {config.grounding.compile_commands_dir!r}")

    # 상대경로로 적어야 분석기 cwd(워크스페이스 루트) 기준으로 그대로 맞는다.
    written = root / config.grounding.compile_commands_dir / "compile_commands.json"
    _check(written.is_file(), f"{written} 을 가리키지 않는다")


TESTS = [
    test_cmake_wins_over_generated_solution,
    test_single_solution_is_found,
    test_vcxproj_one_level_down_is_found,
    test_ambiguous_solutions_ask_instead_of_guessing,
    test_nothing_found_says_what_to_do,
    test_explicit_project_is_resolved_against_root,
    test_msbuild_command_attaches_logger_and_rebuilds,
    test_msbuild_command_passes_extra_args_last,
    test_logger_output_path_ends_with_separator,
    test_cmake_command_forces_ninja_and_export,
    test_logger_sources_are_bundled,
    test_vendored_csproj_needs_no_nuget,
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
