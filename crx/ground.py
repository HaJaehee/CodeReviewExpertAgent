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


class Analyzer(ABC):
    """정적분석 도구 하나에 대한 어댑터."""

    name: str = "analyzer"
    executable: str = ""
    languages: tuple[Language, ...] = ()

    def __init__(self, *, extra_args: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.extra_args = extra_args or []
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def run(self, paths: list[str], cwd: Path) -> AnalyzerResult:
        if not paths:
            return AnalyzerResult(self.name, skipped=True, skip_reason="대상 파일 없음")
        if not self.available():
            return AnalyzerResult(
                self.name, skipped=True,
                skip_reason=f"{self.executable} 를 PATH 에서 찾을 수 없다",
            )

        command = self.build_command(paths)
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

        return AnalyzerResult(self.name, findings=findings)

    @abstractmethod
    def build_command(self, paths: list[str]) -> list[str]: ...

    @abstractmethod
    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]: ...


# --------------------------------------------------------------------------
# 공통 파서
# --------------------------------------------------------------------------

#: `path:line:col: severity: message [rule]` — clang-tidy, gcc, cppcheck 계열 공통.
_GNU_STYLE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
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

    def __init__(self, *, checks: str | None = None, compile_commands_dir: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.checks = checks or self.DEFAULT_CHECKS
        #: compile_commands.json 이 있는 디렉터리. 없으면 정확도가 크게 떨어진다.
        self.compile_commands_dir = compile_commands_dir

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, f"--checks={self.checks}", "--quiet"]
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


class RoslynAnalyzers(Analyzer):
    """`dotnet build` 의 경고를 그대로 수확한다.

    팀이 이미 쓰는 빌드 파이프라인의 분석기 설정(.editorconfig, Directory.Build.props)을
    그대로 재사용하므로, 별도 룰 관리 없이 사내 표준과 자동으로 일치한다.
    C# 은 OCR 내장 룰이 없어 그라운딩의 가치가 특히 크다.
    """

    name = "roslyn"
    executable = "dotnet"
    languages = (Language.CSHARP,)

    def __init__(self, *, project: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        #: 빌드할 .sln/.csproj. 없으면 cwd 에서 자동 탐색된다.
        self.project = project

    def build_command(self, paths: list[str]) -> list[str]:
        # 파일 단위 분석이 불가하므로 프로젝트를 빌드하고 경고를 걷는다.
        command = [self.executable, "build", "--nologo", "-v", "normal"]
        if self.project:
            command.append(self.project)
        command.extend(self.extra_args)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        return _parse_regex(_MSBUILD_STYLE, stdout + "\n" + stderr, self.name)


class Roslynator(Analyzer):
    """Roslynator CLI. dotnet build 보다 룰이 풍부하지만 별도 설치가 필요하다."""

    name = "roslynator"
    executable = "roslynator"
    languages = (Language.CSHARP,)

    def __init__(self, *, project: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.project = project

    def build_command(self, paths: list[str]) -> list[str]:
        command = [self.executable, "analyze"]
        if self.project:
            command.append(self.project)
        command.extend(self.extra_args)
        return command

    def parse(self, stdout: str, stderr: str) -> list[StaticFinding]:
        return _parse_regex(_MSBUILD_STYLE, stdout + "\n" + stderr, self.name)


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
        """각 지적을 해당 라인을 포함하는 청크에 붙인다.

        경로는 접미사 매칭한다 — 분석기마다 절대/상대 경로를 섞어 내보내기 때문이다.
        """
        for finding in findings:
            normalized = _normalize(finding.path)
            for chunk in chunks:
                if not _paths_match(normalized, chunk.path):
                    continue
                if chunk.covers(finding.line):
                    chunk.static_findings.append(finding)
                    break

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
