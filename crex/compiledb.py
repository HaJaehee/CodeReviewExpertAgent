"""compile_commands.json 생성.

clang-tidy 는 컴파일 명령을 모르면 헤더를 못 찾는다. 그 상태의 지적은 절반이
오탐이다. 그래서 이 파일을 만드는 일은 선택 사항이 아니다.

문제는 만드는 절차가 프로젝트 형식마다 다르다는 것이다 — CMake 는 한 줄이고,
MSBuild 는 로거를 붙여야 한다. 그래서 명령 하나로 만든다.

    python -m crex compiledb

MSBuild 경로는 Windows + Visual Studio 가 있어야 한다. 로거는
`tools/msbuild-compiledb/` 에 빌드된 DLL 로 들어 있다 — 폐쇄망 안에서 빌드할
필요가 없다.

실패할 때는 무엇을 실행했고 무엇이 나왔는지를 전부 남긴다 — 폐쇄망 안에서 사람이
이어받아 고칠 수 있어야 한다.
"""

from __future__ import annotations

import json
import locale
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

log = logging.getLogger(__name__)

#: 산출물 자리. 대상 저장소 안이지만 git 에는 잡히지 않게 한다(`prepare_output_dir`).
OUTPUT_DIR = Path(".crex") / "compiledb"

#: 빌드해서 캐시해 둘 로거 이름. 상류 어셈블리 이름과 같아야 한다.
LOGGER_DLL = "CompileCommandsJson.dll"

#: MSBuild 기본 verbosity. `detailed` 미만에서는 CL 명령줄 이벤트가 로거까지
#: 오지 않을 수 있다. 콘솔이 지저분해지는 대신 결과가 비는 사고를 막는다.
DEFAULT_VERBOSITY = "detailed"

#: 관찰 방식이라 컴파일되지 않은 파일은 기록되지 않는다. 증분 빌드는 반쪽짜리
#: DB 를 만들고, 반쪽짜리 DB 는 없는 것보다 나쁘다 — 조용히 일부만 맞기 때문이다.
DEFAULT_TARGET = "Rebuild"


class CompileDbError(RuntimeError):
    """사용자가 고칠 수 있는 실패. 메시지에 다음 행동이 들어 있어야 한다."""


class CompileDbCancelled(CompileDbError):
    """사용자가 빌드를 중단했다.

    `CompileDbError` 를 상속한다 — 부르는 쪽에서 따로 잡지 않아도 "사용자가
    고칠 수 있는 실패"와 같은 자리로 떨어지고, 구분이 필요한 쪽(관제 화면)만
    이 타입을 본다.
    """


#: 빌드 출력 한 줄을 받는 콜백. 관제 화면이 진행 상황을 중계하는 통로다.
OnLine = Callable[[str], None]

#: 중단 여부를 묻는 콜백. True 를 돌려주면 실행 중인 빌드를 끊는다.
ShouldCancel = Callable[[], bool]


@dataclass(frozen=True)
class Project:
    """무엇을 상대로 만들 것인가."""

    #: "cmake" | "msbuild"
    kind: str
    #: CMakeLists.txt / .sln / .vcxproj
    path: Path

    def describe(self) -> str:
        return f"{self.kind}: {self.path}"


@dataclass(frozen=True)
class Result:
    project: Project
    #: compile_commands.json 이 있는 디렉터리. 그대로 `compile_commands_dir` 에 넣는다.
    directory: Path
    json_path: Path
    #: 엔트리 수. 0 이면 만들기는 했는데 아무것도 안 담긴 것이다.
    entries: int
    #: 빌드 로그. 실패했거나 엔트리가 적을 때 여기부터 본다.
    log_path: Path | None = None


# --------------------------------------------------------------------------
# 탐지
# --------------------------------------------------------------------------


def detect_project(root: Path, explicit: Path | str | None = None) -> Project:
    """무엇을 빌드할지 정한다.

    CMakeLists.txt 를 .sln 보다 먼저 본다. CMake 프로젝트가 만들어낸 .sln 이
    같이 있는 경우가 흔한데, 그때 원본은 CMake 쪽이고 그쪽이 훨씬 빠르다
    (구성만 하면 되고 컴파일이 필요 없다).
    """
    root = Path(root)

    if explicit is not None:
        path = Path(explicit)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise CompileDbError(f"{path} 가 없다.")
        return Project(_kind_of(path), path)

    cmake_lists = root / "CMakeLists.txt"
    if cmake_lists.is_file():
        return Project("cmake", cmake_lists)

    for pattern in ("*.sln", "*.vcxproj", "*/*.sln", "*/*.vcxproj"):
        found = sorted(root.glob(pattern))
        if len(found) == 1:
            return Project("msbuild", found[0])
        if len(found) > 1:
            names = ", ".join(p.relative_to(root).as_posix() for p in found)
            raise CompileDbError(
                f"{pattern} 가 여러 개다 ({names}). --project 로 하나를 지정하라."
            )

    raise CompileDbError(
        f"{root} 에서 CMakeLists.txt / .sln / .vcxproj 를 찾지 못했다. "
        f"--project 로 직접 지정하거나, --workspace 로 프로젝트 루트를 가리키라."
    )


def _kind_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".sln", ".vcxproj", ".slnx"):
        return "msbuild"
    if path.name.lower() == "cmakelists.txt":
        return "cmake"
    raise CompileDbError(
        f"{path.name} 로는 무엇을 할지 알 수 없다. .sln, .vcxproj, CMakeLists.txt 중 하나여야 한다."
    )


# --------------------------------------------------------------------------
# 도구 찾기
# --------------------------------------------------------------------------


def find_msbuild(env: Mapping[str, str] | None = None) -> Path:
    """MSBuild.exe 를 찾는다. vswhere 를 PATH 보다 먼저 본다.

    PATH 에 있는 msbuild 는 .NET Framework 4.0 에 딸려온 옛 버전일 수 있고,
    그것으로는 요즘 .vcxproj 를 빌드하지 못한다. vswhere 는 Visual Studio
    설치 관리자가 항상 같은 자리에 놓으므로 그쪽이 확실하다.
    """
    env = os.environ if env is None else env

    vswhere = _vswhere_path(env)
    if vswhere is not None:
        found = _vswhere_find(vswhere, r"MSBuild\**\Bin\MSBuild.exe")
        # 64비트 쪽을 고른다. 큰 솔루션에서 32비트 MSBuild 는 메모리로 죽는다.
        for candidate in found:
            if "amd64" in candidate.as_posix().lower():
                return candidate
        if found:
            return found[0]

    from_path = shutil.which("msbuild") or shutil.which("MSBuild.exe")
    if from_path:
        return Path(from_path)

    raise CompileDbError(
        "MSBuild 를 찾지 못했다. Visual Studio(또는 Build Tools)가 설치된 장비에서 "
        "실행하거나, 'x64 Native Tools Command Prompt' 에서 다시 시도하라."
    )


def find_cmake(env: Mapping[str, str] | None = None) -> Path:
    """cmake 를 찾는다. PATH 다음으로 Visual Studio 에 딸려온 것을 본다."""
    env = os.environ if env is None else env

    from_path = shutil.which("cmake")
    if from_path:
        return Path(from_path)

    vswhere = _vswhere_path(env)
    if vswhere is not None:
        found = _vswhere_find(
            vswhere,
            r"Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
            # MSBuild 컴포넌트를 요구하지 않는다. CMake 도구만 깔린 설치본도 있고,
            # -find 로 파일 존재를 이미 확인하므로 더 좁힐 이유가 없다.
            requires=None,
        )
        if found:
            return found[0]

    raise CompileDbError(
        "cmake 를 찾지 못했다. PATH 에 넣거나, Visual Studio 설치 관리자에서 "
        "'Windows용 C++ CMake 도구' 를 추가하라."
    )


def find_clang_tidy(env: Mapping[str, str] | None = None) -> Path | None:
    """clang-tidy 를 찾는다. PATH 다음으로 Visual Studio 에 딸려온 것을 본다.

    Windows 에서 clang-tidy 는 Visual Studio 의 'C++ Clang 도구' 컴포넌트로 들어오고,
    설치 관리자는 그것을 PATH 에 넣지 않는다. 설치돼 있는데도 없는 것으로 보이고,
    C++ 그라운딩이 통째로 빠진다.

    찾지 못하면 None 이다 — MSBuild/cmake 와 달리 분석기 부재는 건너뛸 사유이지
    오류가 아니다.
    """
    env = os.environ if env is None else env

    from_path = shutil.which("clang-tidy")
    if from_path:
        return Path(from_path)

    vswhere = _vswhere_path(env)
    if vswhere is None:
        return None
    # MSBuild 컴포넌트를 요구하지 않는다 — Clang 도구만 깔린 설치본도 있고,
    # -find 로 파일 존재는 이미 확인된다.
    found = _vswhere_find(vswhere, r"VC\Tools\Llvm\**\bin\clang-tidy.exe", requires=None)
    if not found:
        return None

    # VS 는 x64 / ARM64 / 32비트를 한꺼번에 깐다. 호스트에 맞는 것을 고른다.
    is_arm = (env.get("PROCESSOR_ARCHITECTURE") or "").upper() == "ARM64"
    preferred = "/arm64/" if is_arm else "/x64/"
    for candidate in found:
        if preferred in candidate.as_posix().lower():
            return candidate
    return found[0]


def _vswhere_path(env: Mapping[str, str]) -> Path | None:
    program_files = env.get("ProgramFiles(x86)") or env.get("ProgramFiles")
    if not program_files:
        return None
    candidate = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    return candidate if candidate.is_file() else None


def _vswhere_find(
    vswhere: Path, pattern: str, *, requires: str | None = "Microsoft.Component.MSBuild"
) -> list[Path]:
    command = [str(vswhere), "-latest", "-prerelease", "-products", "*"]
    if requires:
        command += ["-requires", requires]
    command += ["-find", pattern]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 장비 의존
        log.debug("vswhere 실행 실패: %s", exc)
        return []
    return [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 명령 조립 — 여기까지는 순수 함수라 어느 OS 에서도 테스트된다
# --------------------------------------------------------------------------


def vendored_dir() -> Path:
    """로거 DLL 이 있는 자리. 반입 번들에서도 상대 위치가 같다."""
    return Path(__file__).resolve().parents[1] / "tools" / "msbuild-compiledb"


def build_msbuild_command(
    msbuild: Path,
    project: Path,
    logger_dll: Path,
    out_json: Path,
    *,
    configuration: str = "Debug",
    platform: str | None = "x64",
    target: str = DEFAULT_TARGET,
    verbosity: str = DEFAULT_VERBOSITY,
    extra: Sequence[str] = (),
) -> list[str]:
    """로거를 붙여 빌드하는 명령.

    `/m`(병렬)을 기본으로 넣지 않는다. 병렬 빌드는 이벤트를 노드에서 중앙 로거로
    전달하는데, 그 전달이 verbosity 에 따라 걸러진다. 걸러지면 DB 가 조용히
    일부만 차고, 그건 비어 있는 것보다 나쁘다. 속도가 급하면 `--msbuild-arg /m`
    으로 직접 켜고 엔트리 수를 확인하라.
    """
    command = [
        str(msbuild), str(project),
        "/nologo", f"/v:{verbosity}",
        f"/t:{target}",
        f"/p:Configuration={configuration}",
    ]
    if platform:
        command.append(f"/p:Platform={platform}")
    command.append(f"/logger:{os.fspath(logger_dll)};{os.fspath(out_json)}")
    command.extend(extra)
    return command


def build_cmake_command(
    cmake: Path,
    source_dir: Path,
    build_dir: Path,
    *,
    generator: str = "Ninja",
    extra: Sequence[str] = (),
) -> list[str]:
    """구성(configure)만 하는 명령. 빌드까지 갈 필요가 없다.

    Visual Studio 제너레이터는 CMAKE_EXPORT_COMPILE_COMMANDS 를 무시한다.
    그래서 대상 저장소가 VS 로 구성돼 있든 말든 여기서는 Ninja 로 따로 구성한다.
    """
    command = [
        str(cmake),
        "-S", os.fspath(source_dir),
        "-B", os.fspath(build_dir),
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    if generator:
        command[1:1] = ["-G", generator]
    command.extend(extra)
    return command


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------


def prepare_output_dir(root: Path, output_dir: Path | str | None = None) -> Path:
    """산출물 자리를 만들고, 그 자리가 git 에 잡히지 않게 한다.

    사용자의 `.gitignore` 를 건드리지 않는다 — 남의 저장소 파일이다. 대신
    `.crex/.gitignore` 에 `*` 를 넣어 스스로를 무시하게 한다.
    """
    root = Path(root)
    directory = Path(output_dir) if output_dir else root / OUTPUT_DIR
    if not directory.is_absolute():
        directory = root / directory
    directory.mkdir(parents=True, exist_ok=True)

    marker = directory.parent / ".gitignore"
    if directory.parent.name == ".crex" and not marker.exists():
        try:
            marker.write_text("*\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 권한 문제는 치명적이지 않다
            log.debug(".gitignore 를 쓰지 못했다: %s", exc)
    return directory


def ensure_logger() -> Path:
    """담겨 온 로거 DLL 의 자리를 준다.

    빌드하지 않는다. DLL 을 반입 번들에 그대로 담기 때문이다 — 폐쇄망 장비에
    MSBuild 가 있어도 로거를 거기서 빌드할 이유가 없고, 빌드는 실패할 자리가
    하나 더 늘어나는 일이다.
    """
    dll = vendored_dir() / LOGGER_DLL
    if not dll.is_file():
        raise CompileDbError(
            f"로거를 찾지 못했다: {dll}. 반입 번들에서 tools/ 가 빠졌을 수 있다."
        )
    return dll


def generate(
    root: Path,
    *,
    project: Path | str | None = None,
    configuration: str = "Debug",
    platform: str | None = "x64",
    target: str = DEFAULT_TARGET,
    verbosity: str = DEFAULT_VERBOSITY,
    generator: str = "Ninja",
    output_dir: Path | str | None = None,
    extra_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    on_line: OnLine | None = None,
    cancel: ShouldCancel | None = None,
) -> Result:
    """compile_commands.json 을 만들고 그 위치를 돌려준다.

    `on_line` 을 주면 빌드 출력이 한 줄씩 그리로도 간다. 화면에서 부르는 쪽은
    빌드가 몇십 분 걸리는 동안 아무것도 못 보면 멈춘 줄 알기 때문이다. CLI 는
    주지 않는다 — 콘솔에는 이미 진행 표시가 나간다.

    `cancel` 이 True 를 돌려주면 실행 중인 빌드를 끊고 `CompileDbCancelled` 를
    던진다.
    """
    root = Path(root).resolve()
    found = detect_project(root, project)
    out_dir = prepare_output_dir(root, output_dir)
    json_path = out_dir / "compile_commands.json"

    if found.kind == "cmake":
        # 콘솔로 흘리면 되는 CLI 와 달리, 화면에서 부르면 출력을 받아야 중계할 수
        # 있다. `on_line` 이 있을 때만 캡처로 바꾼다.
        _run(
            build_cmake_command(
                find_cmake(env), root, out_dir, generator=generator, extra=extra_args
            ),
            what="cmake 구성",
            cwd=root,
            capture=on_line is not None,
            log_path=(out_dir / "cmake.log") if on_line is not None else None,
            on_line=on_line,
            cancel=cancel,
        )
        if not json_path.is_file():
            raise CompileDbError(
                f"cmake 는 끝났는데 {json_path} 가 없다. 제너레이터가 Ninja 가 맞는지, "
                f"CMake 3.5 이상인지 확인하라."
            )
        return Result(found, out_dir, json_path, _count_entries(json_path))

    msbuild = find_msbuild(env)
    logger_dll = ensure_logger()

    # 로거는 빌드 도중 조금씩 써 나간다. 중간에 실패하면 반쪽짜리 JSON 이 남는데,
    # 그게 원래 자리에 있으면 다음 리뷰가 그걸 그대로 믿는다. 임시 파일에 받아
    # 성공했을 때만 옮긴다.
    staging = out_dir / "compile_commands.json.partial"
    log_path = out_dir / "msbuild.log"
    command = build_msbuild_command(
        msbuild, found.path, logger_dll, staging,
        configuration=configuration, platform=platform,
        target=target, verbosity=verbosity, extra=extra_args,
    )

    log.info("%s 를 빌드한다. 큰 솔루션이면 오래 걸린다.", found.path.name)
    _run(
        command, what="MSBuild 빌드", cwd=root, capture=True,
        log_path=log_path, on_line=on_line, cancel=cancel,
    )

    if not staging.is_file():
        raise CompileDbError(
            f"빌드는 끝났는데 {staging} 이 없다. 로거가 붙지 않았을 수 있다 — {log_path} 를 보라."
        )
    _rewrite_batched_commands(staging)
    staging.replace(json_path)
    return Result(found, out_dir, json_path, _count_entries(json_path), log_path)



#: 명령줄 토큰. 따옴표로 묶인 덩어리를 하나로 본다.
_COMMAND_TOKEN = re.compile(r'"[^"]*"|\S+')


def _same_path(a: str, b: str) -> bool:
    return a.strip('"').replace("\\", "/").casefold() == b.strip('"').replace("\\", "/").casefold()


def split_batched_commands(entries: list[dict]) -> int:
    """한 번의 cl.exe 호출에 여러 소스가 묶인 항목을 파일 하나짜리로 줄인다.

    MSBuild 는 설정이 같은 .cpp 를 모아 cl.exe 를 한 번만 부른다. 로거는 관찰한
    명령을 그대로 적으므로, 항목마다 명령줄 끝에 남의 소스 파일까지 들어간다.
    clang-tidy 는 그런 명령을 받으면 "expected exactly one compiler job" 으로
    그 파일을 통째로 포기하는데, 그 실패는 진단 형식이 아니라서 파서에 걸리지
    않는다 — CREX 는 그것을 지적 0건으로 본다. 조용한 0건이다.

    바꾼 항목 수를 돌려준다.
    """
    sources = [entry.get("file", "") for entry in entries]
    changed = 0
    for entry in entries:
        command = entry.get("command")
        mine = entry.get("file", "")
        if not command or not mine:
            continue
        tokens = _COMMAND_TOKEN.findall(command)
        kept = [
            token for token in tokens
            if _same_path(token, mine)
            or not any(_same_path(token, other) for other in sources)
        ]
        if len(kept) != len(tokens):
            entry["command"] = " ".join(kept)
            changed += 1
    return changed


def _rewrite_batched_commands(json_path: Path) -> None:
    """묶인 명령을 풀어 다시 쓴다. 실패해도 리뷰를 막지 않는다."""
    try:
        entries = json.loads(json_path.read_text(encoding="utf-8"))
        changed = split_batched_commands(entries)
        if changed:
            json_path.write_text(
                json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.info("묶여 있던 컴파일 명령 %d건을 파일 단위로 풀었다", changed)
    except (OSError, ValueError, TypeError) as exc:
        log.warning("컴파일 명령 분리 실패 — 원본을 그대로 둔다: %s", exc)


def describe_status(configured: str | None, root: Path) -> dict:
    """설정된 compile_commands.json 이 실제로 쓸 만한 상태인지 본다.

    설정과 파일을 함께 본다 — 둘 중 하나만 맞아도 clang-tidy 는 눈을 감는다.
    `doctor` 의 한 줄과 관제 화면의 상태 표시가 같은 판정을 쓰도록 구조로 돌려준다.
    """
    if not configured:
        return {"configured": None, "path": None, "exists": False, "entries": None, "error": None}

    directory = Path(configured)
    if not directory.is_absolute():
        directory = Path(root) / directory
    path = directory / "compile_commands.json"
    state = {
        "configured": str(configured),
        "path": str(path),
        "exists": False,
        "entries": None,
        "error": None,
    }
    if not path.is_file():
        state["error"] = f"{path} 가 없다 — 경로가 맞는지 확인하거나 다시 만들라"
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        state["error"] = f"{path} 를 읽지 못했다: {exc}"
        return state
    state["exists"] = True
    state["entries"] = len(data) if isinstance(data, list) else 0
    return state


def _count_entries(json_path: Path) -> int:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise CompileDbError(f"{json_path} 를 읽지 못했다: {exc}") from exc
    if not isinstance(data, list):
        raise CompileDbError(f"{json_path} 이 JSON 배열이 아니다. 생성이 중간에 끊겼을 수 있다.")
    return len(data)


def _run(
    command: list[str],
    *,
    what: str,
    cwd: Path | None = None,
    capture: bool,
    log_path: Path | None = None,
    on_line: OnLine | None = None,
    cancel: ShouldCancel | None = None,
) -> None:
    """외부 도구를 돌린다. 실패하면 무엇을 실행했는지까지 함께 알린다.

    `capture=True` 면 출력을 파일로 받는다. verbosity 를 detailed 로 올려 놓기
    때문에 콘솔에 그대로 흘리면 사람이 읽을 수 없고, 정작 필요한 오류가 스크롤에
    묻힌다. 실패했을 때 꼬리만 보여주고 전문은 파일로 남긴다.
    """
    log.debug("실행: %s", " ".join(command))

    if not capture:
        try:
            completed = subprocess.run(command, cwd=str(cwd) if cwd else None, check=False)
        except OSError as exc:
            raise CompileDbError(f"{what} 를 실행하지 못했다 ({command[0]}): {exc}") from exc
        if completed.returncode != 0:
            raise CompileDbError(
                f"{what} 실패 (종료 코드 {completed.returncode}).\n"
                f"실행한 명령: {' '.join(command)}"
            )
        return

    lines, code = _stream(command, cwd, what, log_path, on_line, cancel)
    if code != 0:
        tail = "\n".join(lines[-20:]) if lines else "(출력 없음)"
        where = f"\n전체 로그: {log_path}" if log_path is not None and lines else ""
        raise CompileDbError(
            f"{what} 실패 (종료 코드 {code}).\n"
            f"실행한 명령: {' '.join(command)}\n{tail}{where}"
        )


def _decode(raw: bytes) -> str:
    """빌드 도구 출력 한 줄을 문자열로. UTF-8 을 먼저 보고, 아니면 시스템 기본값.

    MSBuild 와 CMake 가 어떤 인코딩으로 내보내는지는 장비마다 다르다 — 파이프로
    내보낼 때 UTF-8 인 판이 있고, 콘솔 코드페이지 그대로인 판이 있다. 한국어
    Windows 에서 시스템 기본값(cp949)으로 UTF-8 출력을 읽으면 로그가 통째로 깨진
    글자가 되는데, 그 로그는 실패 원인을 읽으라고 남기는 것이다.

    둘 다 아니면 글자를 바꿔서라도 계속 간다 — 로그 한 줄 때문에 빌드를 세우지 않는다.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _stream(
    command: list[str],
    cwd: Path | None,
    what: str,
    log_path: Path | None,
    on_line: OnLine | None = None,
    cancel: ShouldCancel | None = None,
) -> tuple[list[str], int]:
    """출력을 파일로 받으면서 살아 있다는 표시만 화면에 낸다.

    전체 Rebuild 는 몇십 분이 걸린다. 그동안 화면이 완전히 조용하면 사람은
    멈춘 줄 알고 Ctrl-C 를 누른다 — 그러면 처음부터 다시다. 그렇다고 detailed
    verbosity 출력을 그대로 흘리면 읽을 수 없는 양이 쏟아진다. 그래서 진행
    표시만 내고 전문은 로그로 남긴다.
    """
    try:
        # 바이트로 받아 직접 해독한다(`_decode`). 텍스트 모드는 시스템 기본
        # 코드페이지로 읽는데, 한국어 Windows(cp949)에서 MSBuild 가 UTF-8 을
        # 내보내면 로그 전체가 깨진 글자가 된다 — 실패 원인을 읽으라고 남기는
        # 로그다.
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        raise CompileDbError(f"{what} 를 실행하지 못했다 ({command[0]}): {exc}") from exc

    lines: list[str] = []
    handle = None
    cancelled = False
    try:
        if log_path is not None:
            handle = log_path.open("w", encoding="utf-8", errors="replace")
        assert process.stdout is not None
        for raw in process.stdout:
            line = _decode(raw).rstrip("\r\n")
            lines.append(line)
            if handle is not None:
                handle.write(line + "\n")
            if on_line is not None:
                on_line(line)
            elif len(lines) % 500 == 0:
                print(f"  ... {what} 진행 중 ({len(lines):,}줄)", flush=True)
            # 출력이 한 줄 올 때마다 본다. 빌드가 오래 조용한 구간에서는 중단이
            # 그만큼 늦게 듣지만, 별도 감시 스레드를 두는 것보다 이쪽이 단순하다.
            if cancel is not None and cancel():
                cancelled = True
                process.terminate()
                break
    finally:
        if handle is not None:
            handle.close()
        process.wait()

    if cancelled:
        raise CompileDbCancelled(f"{what} 를 중단했다.")
    return lines, process.returncode
