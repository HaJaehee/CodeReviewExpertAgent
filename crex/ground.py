"""정적분석 그라운딩 게이트.

LLM 을 부르기 *전에* 결정론적 도구를 돌려 사실을 확보한다. 그리고 프롬프트에서
LLM 의 역할을 재정의한다 — "결함을 찾아라"가 아니라 **"이 도구 결과를 검증하고,
도구가 못 잡는 로직/설계 결함만 추가하라"**.

이 전환이 중요한 이유: 소형 모델이 "여기 널 체크가 없다"고 지어내는 대신,
도구가 실제로 확인한 항목을 놓고 판단하게 된다. 근거 없는 지적의 상당수가
프롬프트 단계에서 사라진다.

모든 분석기는 **없으면 조용히 건너뛴다**. 폐쇄망에서는 도구 설치 상태가
장비마다 다르므로, 하나가 없다고 파이프라인이 멈춰선 안 된다.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Language, ReviewChunk, Severity, StaticFinding

log = logging.getLogger(__name__)

#: 분석기 1회 실행 상한(초). 초과하면 죽이고 건너뛴다.
#: clang-tidy 는 헤더가 많은 C++ 파일에서 쉽게 수 분을 먹으므로 반드시 필요하다.
DEFAULT_TIMEOUT = 120.0


@dataclass
class AnalyzerResult:
    tool: str
    findings: list[StaticFinding] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    duration: float = 0.0


#: "아직 찾아보지 않았다". None 은 "찾아봤고 없었다" 라서 구분이 필요하다.
_UNRESOLVED = object()


class Analyzer(ABC):
    """정적분석 도구 하나에 대한 어댑터."""

    name: str = "analyzer"
    executable: str = ""
    languages: tuple[Language, ...] = ()

    def __init__(self, *, extra_args: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.extra_args = extra_args or []
        self.timeout = timeout
        self._resolved: str | None | object = _UNRESOLVED

    def resolve_executable(self) -> str | None:
        """실행 파일의 실제 경로. 못 찾으면 None.

        결과를 캐시한다 — available() 과 run() 이 각각 부르고 doctor 는 분석기
        수만큼 부르는데, 하위 클래스는 여기서 vswhere 같은 외부 프로세스를 띄운다.
        """
        if self._resolved is _UNRESOLVED:
            self._resolved = self._locate()
        return self._resolved  # type: ignore[return-value]

    def _locate(self) -> str | None:
        """기본은 PATH 뿐이다. PATH 밖을 볼 도구는 하위 클래스가 덮어쓴다."""
        return shutil.which(self.executable)

    def available(self) -> bool:
        return self.resolve_executable() is not None

    def run(self, paths: list[str], cwd: Path) -> AnalyzerResult:
        if not paths:
            return AnalyzerResult(self.name, skipped=True, skip_reason="대상 파일 없음")
        if not self.available():
            return AnalyzerResult(
                self.name, skipped=True,
                skip_reason=f"{self.executable} 를 PATH 에서 찾을 수 없습니다",
            )

        command = self.build_command(paths)
        # build_command 는 도구 이름을 쓴다. PATH 밖에서 찾은 경우 그 이름으로는
        # 실행되지 않으므로 해석된 절대 경로로 바꾼다.
        resolved = self.resolve_executable()
        if resolved and command and command[0] == self.executable:
            command[0] = resolved
        log.debug("[%s] %s", self.name, " ".join(command))
        try:
            completed = subprocess.run(  # noqa: S603 - 명령은 코드에서 구성되며 사용자 입력이 아니다
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AnalyzerResult(
                self.name, skipped=True,
                skip_reason=f"{self.timeout:.0f}초 내에 끝나지 않아 중단",
            )
        except OSError as exc:
            return AnalyzerResult(self.name, skipped=True, skip_reason=f"실행 실패: {exc}")

        try:
            findings = self.parse(completed.stdout, completed.stderr)
        except Exception as exc:  # noqa: BLE001 - 파서 실패로 리뷰 전체를 막지 않는다
            log.warning("[%s] 출력 파싱 실패: %s", self.name, exc)
            return AnalyzerResult(self.name, skipped=True, skip_reason=f"파싱 실패: {exc}")

        broken = self.diagnose(completed, findings)
        if broken:
            return AnalyzerResult(self.name, skipped=True, skip_reason=broken)

        return AnalyzerResult(self.name, findings=findings)

    def diagnose(
        self, completed: subprocess.CompletedProcess, findings: list[StaticFinding]
    ) -> str | None:
        """도구가 제대로 돌지 못한 것인지 판정한다. 이유를 돌려주면 건너뛴 것으로 본다.

        대부분의 분석기는 지적을 찾으면 0 이 아닌 코드로 끝나므로 종료 코드만으로는
        판정할 수 없다. 그래서 기본은 판정하지 않는다. 빌드를 태우는 분석기처럼
        '실패했는데 결과가 0건' 이 곧 조용한 사고인 경우에만 덮어쓴다.
        """
        return None

    @abstractmethod
    def build_command(self, paths: list[str]) -> list[str]: ...

    @abstractmethod
    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]: ...


# --------------------------------------------------------------------------
# 공통 파서
# --------------------------------------------------------------------------

#: `path:line:col: severity: message [rule]` — clang-tidy, gcc, cppcheck 계열 공통.
#: Windows 드라이브 문자("C:")를 경로 앞에 허용한다. 이게 없으면 매칭이
#: 드라이브 문자의 콜론에서 끊겨 줄 전체가 실패한다 — clang-tidy 는 상대
#: 경로를 줘도 절대 경로로 되받아 내므로, Windows 장비에서 C++ 그라운딩이
#: 항상 0건이 된다. 조용한 0건이라 아무도 눈치채지 못한다.
_GNU_STYLE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>error|warning|note|style|performance|portability|information):\s*"
    r"(?P<message>.*?)(?:\s*\[(?P<rule>[\w\-\.,]+)\])?$",
    re.MULTILINE,
)

#: `path(line,col): severity CODE: message` — MSBuild / Roslyn 계열.
_MSBUILD_STYLE = re.compile(
    r"^\s*(?P<path>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<severity>error|warning|info)\s+(?P<rule>[A-Z]+\d+):\s*(?P<message>.*?)(?:\s*\[.*\])?$",
    re.MULTILINE,
)

_SEVERITY_MAP = {
    "error": Severity.HIGH,
    "warning": Severity.MEDIUM,
    "performance": Severity.MEDIUM,
    "portability": Severity.LOW,
    "style": Severity.LOW,
    "note": Severity.LOW,
    "information": Severity.LOW,
    "info": Severity.LOW,
}


def _severity(raw: str) -> Severity:
    return _SEVERITY_MAP.get(raw.lower(), Severity.MEDIUM)


#: MSBuild 다중 노드 로거가 줄머리에 붙이는 노드 번호("1>").
_MSBUILD_NODE_PREFIX = re.compile(r"^\d+>")


def _parse_msbuild(text: str, tool: str) -> list[StaticFinding]:
    """MSBuild 출력에서 진단을 뽑는다.

    같은 경고가 두 번 나온다 — 프로젝트별 출력에 한 번, 빌드 말미 요약에 또 한 번.
    그대로 두면 근거가 2배로 부풀어 프롬프트에 들어가고, 모델은 서로 다른 두 도구가
    같은 곳을 지적한 것으로 읽는다. 컨텍스트를 아껴 쓰는 것이 이 파이프라인의
    목적이므로 여기서 접는다.

    노드 번호 접두사("1>")도 뗀다. 청크 매칭은 접미사 비교라 살아남지만, 프롬프트에는
    "1>C:/..." 같은 깨진 경로가 그대로 노출된다.
    """
    findings = _parse_regex(_MSBUILD_STYLE, text, tool)
    unique: dict[tuple, StaticFinding] = {}
    for finding in findings:
        finding.path = _MSBUILD_NODE_PREFIX.sub("", finding.path)
        key = (finding.path, finding.line, finding.column, finding.rule_id, finding.message)
        unique.setdefault(key, finding)
    return list(unique.values())


def _parse_regex(pattern: re.Pattern[str], text: str, tool: str, default_rule: str = "") -> list[StaticFinding]:
    findings: list[StaticFinding] = []
    for match in pattern.finditer(text):
        if match.group("severity").lower() == "note":
            continue  # note 는 직전 경고의 부연이므로 중복 지적이 된다
        findings.append(
            StaticFinding(
                tool=tool,
                path=_normalize(match.group("path")),
                line=int(match.group("line")),
                column=int(match.group("col")) if match.group("col") else None,
                rule_id=(match.groupdict().get("rule") or default_rule or "").strip(),
                message=match.group("message").strip(),
                severity=_severity(match.group("severity")),
            )
        )
    return findings


def _normalize(path: str) -> str:
    return path.strip().replace("\\", "/")


# --------------------------------------------------------------------------
# C / C++
# --------------------------------------------------------------------------


class ClangTidy(Analyzer):
    """버그 탐지 위주로 체크를 좁혀 실행한다.

    스타일 체크(readability-*)는 켜지 않는다. 오탐이 많고, 스타일은 LLM 이
    판단할 영역도 아니다. 여기서 나오는 항목은 전부 '실제 버그 후보'여야 한다.
    """

    name = "clang-tidy"
    executable = "clang-tidy"
    languages = (Language.CPP,)

    DEFAULT_CHECKS = "-*,bugprone-*,cert-*,clang-analyzer-*,concurrency-*,misc-*,performance-*"

    def __init__(
        self,
        *,
        checks: str | None = None,
        compile_commands_dir: str | None = None,
        project_root: Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        #: None 이면 프로젝트의 .clang-tidy 를 존중하고, 그것도 없으면 기본값을 쓴다.
        self.checks = checks
        #: compile_commands.json 이 있는 디렉터리. 없으면 정확도가 크게 떨어진다.
        self.compile_commands_dir = compile_commands_dir
        self.project_root = project_root

    def _locate(self) -> str | None:
        found = super()._locate()
        if found:
            return found
        # 실행 파일 이름을 바꿔 쓴 하위 클래스라면 여기서 멈춘다. 아래 탐색은
        # clang-tidy 라는 이름에만 해당하는 지식이라, 그대로 두면 다른 도구를
        # 찾아 주는 꼴이 된다.
        if self.executable != ClangTidy.executable:
            return None
        # Windows 에서 clang-tidy 는 Visual Studio 의 'C++ Clang 도구' 컴포넌트로
        # 들어오고 PATH 에는 들어가지 않는다. 여기서 포기하면 설치된 장비에서도
        # C++ 그라운딩이 통째로 빠진다.
        from .compiledb import find_clang_tidy

        candidate = find_clang_tidy()
        return str(candidate) if candidate is not None else None

    def has_project_config(self) -> bool:
        """팀이 관리하는 .clang-tidy 가 있는가."""
        if self.project_root is None:
            return False
        return (self.project_root / ".clang-tidy").is_file()

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "--quiet"]

        # 명령줄 --checks 는 .clang-tidy 의 Checks 를 덮어쓴다. 팀이 관리하는
        # 설정이 있으면 넘기지 않는 것이 맞다 — 사내 코딩 룰이 거기 들어 있고,
        # 여기서 덮으면 그 룰이 통째로 무시된다.
        if self.checks:
            command.append(f"--checks={self.checks}")
        elif not self.has_project_config():
            command.append(f"--checks={self.DEFAULT_CHECKS}")

        if self.compile_commands_dir:
            command.append(f"-p={self.compile_commands_dir}")
        command.extend(self.extra_args)
        command.extend(paths)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        return _parse_regex(_GNU_STYLE, stdout + "\n" + stderr, self.name)


class Cppcheck(Analyzer):
    """오탐률이 매우 낮은 것이 강점. clang-tidy 와 잡는 결함군이 다르므로 함께 쓴다."""

    name = "cppcheck"
    executable = "cppcheck"
    languages = (Language.CPP,)

    TEMPLATE = "{file}:{line}:{column}: {severity}: {message} [{id}]"

    def build_command(self, paths: list[str]) -> list[str]:
        command = [
            self.executable,
            "--enable=warning,performance,portability",
            "--inline-suppr",
            "--quiet",
            f"--template={self.TEMPLATE}",
        ]
        command.extend(self.extra_args)
        command.extend(paths)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        # cppcheck 는 진단을 stderr 로 낸다.
        return _parse_regex(_GNU_STYLE, stderr + "\n" + stdout, self.name)


# --------------------------------------------------------------------------
# C#
# --------------------------------------------------------------------------


def find_dotnet_project(root: Path, paths: list[str] | None = None) -> Path | None:
    """무엇을 빌드할지 정한다. 못 정하면 None.

    `dotnet build` 는 인자가 없으면 현재 디렉터리에서 스스로 찾는데, 저장소 루트에
    프로젝트가 없거나 둘 이상이면 MSB1003/MSB1011 로 죽는다. 그 오류 문구는 경고
    형식이 아니라 파서에 걸리지 않고, 결과는 **조용히 0건**이 된다. 설정을 안 한
    사람에게 "roslyn 이 도는 줄 알았는데 아니었다"가 되는 경로라 여기서 직접 정한다.

    바뀐 파일에서 위로 올라가며 `.csproj` 를 먼저 찾는다. 솔루션 전체가 아니라 그
    파일이 속한 프로젝트만 빌드하면 되기 때문이다 — 리뷰 한 번에 태우는 빌드가
    작을수록 좋다. 바뀐 파일이 여러 프로젝트에 걸쳐 있으면 그때 솔루션으로 올린다.
    """
    root = Path(root)

    projects: list[Path] = []
    for path in paths or []:
        candidate = _nearest_csproj(root, path)
        if candidate is not None and candidate not in projects:
            projects.append(candidate)

    if len(projects) == 1:
        return projects[0]

    solutions = sorted(root.glob("*.sln")) or sorted(root.glob("*/*.sln"))
    if len(solutions) == 1:
        return solutions[0]

    if len(projects) > 1:
        # 솔루션이 없거나 여러 개인데 프로젝트도 여럿이다. 하나를 몰래 고르면
        # 나머지 프로젝트의 경고가 통째로 빠진 채 리뷰가 돈다.
        return None

    # 얕은 곳부터 본다. src/Api/Api.csproj 같은 배치가 흔해서 세 단계까지 훑는다.
    # 저장소 전체를 rglob 하지 않는 이유는 bin/obj 와 남의 소스까지 걸리기 때문이다.
    for pattern in ("*.csproj", "*/*.csproj", "*/*/*.csproj"):
        found = sorted(root.glob(pattern))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None


def _nearest_csproj(root: Path, path: str) -> Path | None:
    """바뀐 파일이 속한 프로젝트. 파일 자리에서 저장소 루트까지만 올라간다."""
    current = (root / path).parent
    try:
        current.relative_to(root)
    except ValueError:
        return None

    while True:
        found = sorted(current.glob("*.csproj"))
        if found:
            return found[0]
        if current == root or current.parent == current:
            return None
        current = current.parent


class DotnetProjectAnalyzer(Analyzer):
    """빌드할 C# 프로젝트를 정해야 하는 분석기의 공통부."""

    #: 프로젝트를 정하지 못했을 때 남길 이유. 다음 행동이 들어 있어야 한다.
    NO_PROJECT = (
        "빌드할 .csproj/.sln 을 정하지 못했습니다 — grounding.dotnet_project 를 지정하십시오"
    )

    def __init__(self, *, project: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        #: 설정으로 지정한 .sln/.csproj. 없으면 바뀐 파일에서 찾아낸다.
        self.project = project
        #: 이번 실행에서 실제로 쓸 대상. run() 이 정하고 build_command() 가 읽는다.
        #: 인스턴스 하나는 리뷰 한 번에 한 번만 돌므로 상태를 들고 있어도 된다.
        self._target: str | None = None

    def run(self, paths: list[str], cwd: Path) -> AnalyzerResult:
        # 대상 파일이 없거나 도구가 안 깔린 경우는 기반 클래스가 먼저 판정한다.
        # 그쪽 이유가 더 정확하다 — dotnet 이 없는 장비에서 "프로젝트를 못 정했다"
        # 고 하면 엉뚱한 것을 고치러 간다.
        if not paths or not self.available():
            return super().run(paths, cwd)

        self._target = self.project or self._discover(cwd, paths)
        if self._target is None:
            return AnalyzerResult(self.name, skipped=True, skip_reason=self.NO_PROJECT)
        log.debug("[%s] 대상 프로젝트: %s", self.name, self._target)
        return super().run(paths, cwd)

    def _discover(self, cwd: Path, paths: list[str]) -> str | None:
        found = find_dotnet_project(cwd, paths)
        return str(found) if found is not None else None


class RoslynAnalyzers(DotnetProjectAnalyzer):
    """`dotnet build` 의 경고를 그대로 수확한다.

    팀이 이미 쓰는 빌드 파이프라인의 분석기 설정(.editorconfig, Directory.Build.props)을
    그대로 재사용하므로, 별도 룰 관리 없이 사내 표준과 자동으로 일치한다.
    C# 은 OCR 내장 룰이 없어 그라운딩의 가치가 특히 크다.
    """

    name = "roslyn"
    executable = "dotnet"
    languages = (Language.CSHARP,)

    def build_command(self, paths: list[str]) -> list[str]:
        # 파일 단위 분석이 불가하므로 프로젝트를 빌드하고 경고를 걷는다.
        #
        # --no-incremental 이 필요한 이유: Roslyn 경고는 컴파일할 때만 나온다.
        # 개발자는 보통 고치고, 빌드해서 확인하고, 그다음 리뷰를 돌린다. 그 순서에서는
        # 다시 컴파일할 것이 없어 경고가 한 줄도 안 나오고, 그 0건이 "도구가 검사했고
        # 깨끗하다"는 거짓 전제로 프롬프트에 들어간다. 느린 쪽이 맞다.
        command = [self.executable, "build", "--nologo", "--no-incremental", "-v", "normal"]
        if self._target:
            command.append(self._target)
        command.extend(self.extra_args)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        return _parse_msbuild(stdout + "\n" + stderr, self.name)

    def diagnose(
        self, completed: subprocess.CompletedProcess, findings: list[StaticFinding]
    ) -> str | None:
        """빌드가 실패했는데 걷은 것도 없으면 그건 '깨끗함' 이 아니다.

        폐쇄망에서 제일 흔한 경우가 NuGet 복원 실패다. 그 오류는 줄·열이 없어
        경고 파서에 걸리지 않고, 그대로 두면 0건이 '지적 없음' 으로 보고된다.
        """
        if completed.returncode == 0 or findings:
            return None
        tail = _last_meaningful_line(completed.stdout, completed.stderr)
        return f"dotnet build 실패 (코드 {completed.returncode}){f': {tail}' if tail else ''}"


class Roslynator(DotnetProjectAnalyzer):
    """Roslynator CLI. dotnet build 보다 룰이 풍부하지만 별도 설치가 필요하다."""

    name = "roslynator"
    executable = "roslynator"
    languages = (Language.CSHARP,)

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "analyze"]
        if self._target:
            command.append(self._target)
        command.extend(self.extra_args)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        return _parse_msbuild(stdout + "\n" + stderr, self.name)


def _last_meaningful_line(stdout: str, stderr: str) -> str:
    """실패 이유로 보여줄 한 줄. 뒤에서부터 실제 내용이 있는 줄을 찾는다."""
    for source in (stderr, stdout):
        for line in reversed((source or "").splitlines()):
            stripped = line.strip()
            if stripped:
                return stripped[:200]
    return ""


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


class Ruff(Analyzer):
    name = "ruff"
    executable = "ruff"
    languages = (Language.PYTHON,)

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "check", "--output-format=json", "--no-fix", "--quiet"]
        command.extend(self.extra_args)
        command.extend(paths)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        if not stdout.strip():
            return []
        findings: list[StaticFinding] = []
        for item in json.loads(stdout):
            location = item.get("location") or {}
            findings.append(
                StaticFinding(
                    tool=self.name,
                    path=_normalize(item.get("filename", "")),
                    line=int(location.get("row", 1)),
                    column=location.get("column"),
                    rule_id=item.get("code") or "",
                    message=item.get("message", ""),
                    severity=Severity.MEDIUM,
                )
            )
        return findings


class Mypy(Analyzer):
    name = "mypy"
    executable = "mypy"
    languages = (Language.PYTHON,)

    _PATTERN = re.compile(
        r"^(?P<path>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
        r"(?P<severity>error|warning|note):\s*(?P<message>.*?)(?:\s*\[(?P<rule>[\w\-]+)\])?$",
        re.MULTILINE,
    )

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "--no-error-summary", "--show-column-numbers", "--no-color-output"]
        command.extend(self.extra_args)
        command.extend(paths)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        return _parse_regex(self._PATTERN, stdout, self.name)


class Bandit(Analyzer):
    name = "bandit"
    executable = "bandit"
    languages = (Language.PYTHON,)

    _SEVERITY = {"HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "-f", "json", "-q"]
        command.extend(self.extra_args)
        command.extend(paths)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        if not stdout.strip():
            return []
        payload = json.loads(stdout)
        return [
            StaticFinding(
                tool=self.name,
                path=_normalize(item.get("filename", "")),
                line=int(item.get("line_number", 1)),
                rule_id=item.get("test_id", ""),
                message=item.get("issue_text", ""),
                severity=self._SEVERITY.get(item.get("issue_severity", "MEDIUM"), Severity.MEDIUM),
            )
            for item in payload.get("results", [])
        ]


# --------------------------------------------------------------------------
# 다국어
# --------------------------------------------------------------------------


class Semgrep(Analyzer):
    """보안 패턴. 룰팩을 사전에 반입해 `--config` 로 로컬 경로를 가리켜야 한다."""

    name = "semgrep"
    executable = "semgrep"
    languages = (Language.CPP, Language.CSHARP, Language.PYTHON)

    _SEVERITY = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}

    def __init__(self, *, config: str = "auto", **kwargs) -> None:
        super().__init__(**kwargs)
        #: 폐쇄망에서는 "auto" 가 동작하지 않는다. 반입한 룰팩 디렉터리를 지정하라.
        self.config = config

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "scan", "--json", "--quiet", "--config", self.config]
        command.extend(self.extra_args)
        command.extend(paths)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        if not stdout.strip():
            return []
        payload = json.loads(stdout)
        findings: list[StaticFinding] = []
        for item in payload.get("results", []):
            extra = item.get("extra", {})
            findings.append(
                StaticFinding(
                    tool=self.name,
                    path=_normalize(item.get("path", "")),
                    line=int((item.get("start") or {}).get("line", 1)),
                    column=(item.get("start") or {}).get("col"),
                    rule_id=item.get("check_id", ""),
                    message=extra.get("message", ""),
                    severity=self._SEVERITY.get(extra.get("severity", "WARNING"), Severity.MEDIUM),
                )
            )
        return findings


# --------------------------------------------------------------------------
# 게이트
# --------------------------------------------------------------------------


#: 언어별로 아무 설정 없이 자동 실행되는 분석기.
DEFAULT_ANALYZERS: dict[Language, list[type[Analyzer]]] = {
    Language.CPP: [ClangTidy, Cppcheck],
    Language.CSHARP: [RoslynAnalyzers],
    Language.PYTHON: [Ruff, Mypy, Bandit],
}

#: 이름을 명시해야만 켜지는 분석기.
#:  - roslynator: 별도 설치가 필요하고 dotnet build 와 겹치는 룰이 많다
#:  - semgrep:    폐쇄망에서는 룰팩 경로(semgrep_config)를 반드시 지정해야 한다
OPTIONAL_ANALYZERS: dict[str, type[Analyzer]] = {
    Roslynator.name: Roslynator,
    Semgrep.name: Semgrep,
}

#: 설정에서 쓸 수 있는 모든 분석기 이름.
ALL_ANALYZER_NAMES: frozenset[str] = frozenset(
    [cls.name for classes in DEFAULT_ANALYZERS.values() for cls in classes]
    + list(OPTIONAL_ANALYZERS)
)


class GroundingGate:
    """리뷰 대상 파일에 정적분석을 돌리고 결과를 청크에 붙인다."""

    def __init__(
        self,
        analyzers: list[Analyzer] | None = None,
        *,
        cwd: Path | None = None,
        max_workers: int = 4,
    ) -> None:
        self.analyzers = analyzers if analyzers is not None else self._default_analyzers()
        self.cwd = cwd or Path.cwd()
        self.max_workers = max_workers
        self.results: list[AnalyzerResult] = []

    @staticmethod
    def _default_analyzers() -> list[Analyzer]:
        seen: set[type[Analyzer]] = set()
        instances: list[Analyzer] = []
        for classes in DEFAULT_ANALYZERS.values():
            for cls in classes:
                if cls not in seen:
                    seen.add(cls)
                    instances.append(cls())
        return instances

    def collect(self, paths: list[str]) -> list[StaticFinding]:
        """대상 파일들에 적용 가능한 분석기를 모두 돌린다."""
        by_language: dict[Language, list[str]] = {}
        for path in paths:
            by_language.setdefault(Language.from_path(path), []).append(path)

        jobs: list[tuple[Analyzer, list[str]]] = []
        for analyzer in self.analyzers:
            targets = [p for lang in analyzer.languages for p in by_language.get(lang, [])]
            if targets:
                jobs.append((analyzer, targets))

        if not jobs:
            self.results = []
            return []

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            self.results = list(pool.map(lambda job: job[0].run(job[1], self.cwd), jobs))

        findings: list[StaticFinding] = []
        for result in self.results:
            if result.skipped:
                log.info("[%s] 건너뜀 — %s", result.tool, result.skip_reason)
            else:
                log.info("[%s] %d건 보고", result.tool, len(result.findings))
                findings.extend(result.findings)

        return findings

    @staticmethod
    def attach(chunks: list[ReviewChunk], findings: list[StaticFinding]) -> None:
        """각 지적을 해당 라인을 포함하는 모든 청크에 붙인다.

        경로는 접미사 매칭한다 — 분석기마다 절대/상대 경로를 섞어 내보내기 때문이다.
        """
        for finding in findings:
            normalized = _normalize(finding.path)
            for chunk in chunks:
                if not _paths_match(normalized, chunk.path):
                    continue
                if chunk.covers(finding.line):
                    if finding not in chunk.static_findings:
                        chunk.static_findings.append(finding)

    def report(self) -> str:
        """어떤 도구가 돌았고 무엇이 빠졌는지 요약한다. 폐쇄망 진단에 필요하다."""
        lines = []
        for result in self.results:
            if result.skipped:
                lines.append(f"  - {result.tool}: 건너뜀 ({result.skip_reason})")
            else:
                lines.append(f"  - {result.tool}: {len(result.findings)}건")
        return "\n".join(lines) if lines else "  (실행된 분석기 없음)"


def _paths_match(analyzer_path: str, chunk_path: str) -> bool:
    if analyzer_path == chunk_path:
        return True
    return analyzer_path.endswith("/" + chunk_path) or chunk_path.endswith("/" + analyzer_path)
