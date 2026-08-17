"""설정 로딩.

TOML 한 파일로 전부 제어한다. 폐쇄망에서는 환경변수보다 파일이 다루기 쉽다
(장비마다 셸 프로파일이 제각각이고, 설정 내용을 감사 기록으로 남겨야 한다).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .llm import EndpointConfig

DEFAULT_CONFIG_NAMES = ("crex.toml", ".crex.toml")


@dataclass
class GroundingConfig:
    enabled: bool = True
    #: compile_commands.json 이 있는 디렉터리. 없으면 clang-tidy 정확도가 크게 떨어진다.
    compile_commands_dir: str | None = None
    #: 폐쇄망에서는 "auto" 가 동작하지 않는다. 반입한 룰팩 경로를 지정하라.
    semgrep_config: str | None = None
    #: C# 빌드 대상 .sln/.csproj.
    dotnet_project: str | None = None
    #: clang-tidy 체크 목록. 비우면 프로젝트의 .clang-tidy 를 존중하고,
    #: 그것도 없으면 버그 탐지 위주의 기본값을 쓴다.
    clang_tidy_checks: str | None = None
    #: 사용할 분석기 이름 목록. 비우면 언어별 기본값 전체.
    #: roslynator 와 semgrep 은 여기에 이름을 적어야만 켜진다 (semgrep 은
    #: semgrep_config 가 있으면 이름 없이도 켜진다).
    analyzers: list[str] = field(default_factory=list)
    timeout: float = 120.0

    def __post_init__(self) -> None:
        # 이름을 틀리면 그 분석기만 조용히 빠지는 게 아니라, 목록이 비지 않은 탓에
        # 기본 분석기까지 전부 걸러진다. 조용한 실패의 대가가 커서 여기서 막는다.
        from .ground import ALL_ANALYZER_NAMES

        unknown = set(self.analyzers) - ALL_ANALYZER_NAMES
        if unknown:
            raise ValueError(
                f"grounding.analyzers 에 알 수 없는 분석기: {sorted(unknown)}. "
                f"사용 가능: {sorted(ALL_ANALYZER_NAMES)}"
            )


@dataclass
class ChunkingConfig:
    expansion_limit: float = 4.0
    expansion_truncate: float = 3.0
    absolute_max_lines: int = 400
    #: diff 와 파일 내용이 어긋날 때: "raise" | "warn" | "ignore"
    on_mismatch: str = "raise"


#: 현재 구현된 생성 모드. "ocr"(alibaba/open-code-review 위임)은 Phase 1 에서
#: 실제 바이너리의 출력 스키마를 확인한 뒤 추가한다. 그때까지는 설정해도 받지 않는다.
SUPPORTED_MODES = ("native",)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class ReviewConfig:
    #: 생성 모드. 현재 "native"(자체 RuleChecker)만 구현되어 있다.
    mode: str = "native"
    max_findings_per_chunk: int = 5
    max_workers: int = 4
    #: diff 리뷰에서는 변경된 라인만 지적 대상으로 삼는다.
    require_changed_line: bool = True
    #: 리포트에 포함할 최소 심각도: "high" | "medium" | "low"
    min_severity: str = "low"

    def __post_init__(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise ValueError(
                f"review.mode = {self.mode!r} 는 아직 구현되지 않았다. "
                f"사용 가능: {list(SUPPORTED_MODES)}"
            )
        if self.min_severity not in SEVERITY_ORDER:
            raise ValueError(
                f"review.min_severity = {self.min_severity!r} 가 잘못되었다. "
                f"사용 가능: {list(SEVERITY_ORDER)}"
            )


@dataclass
class Config:
    generator: EndpointConfig
    verifier: EndpointConfig
    review: ReviewConfig = field(default_factory=ReviewConfig)
    grounding: GroundingConfig = field(default_factory=GroundingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    taxonomy_path: Path | None = None
    source: Path | None = None

    def describe(self) -> str:
        """설정 요약. 실행 로그 첫 줄에 남겨 재현성을 확보한다."""
        return (
            f"모드={self.review.mode} "
            f"생성={self.generator.model}@{self.generator.base_url} "
            f"검증={self.verifier.model}@{self.verifier.base_url} "
            f"입력상한={self.generator.max_input_tokens}토큰 "
            f"그라운딩={'on' if self.grounding.enabled else 'off'}"
        )


def find_config(start: Path | None = None) -> Path | None:
    """현재 디렉터리부터 위로 올라가며 설정 파일을 찾는다."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for name in DEFAULT_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(path: Path | str | None = None) -> Config:
    """설정 파일을 읽는다. 경로가 없으면 탐색하고, 그래도 없으면 기본값을 쓴다."""
    resolved = Path(path) if path else find_config()
    data: dict = {}
    if resolved and resolved.is_file():
        with resolved.open("rb") as handle:
            data = tomllib.load(handle)

    llm = data.get("llm", {})
    generator = _endpoint(llm.get("generator", {}), default_model="Qwen3.6-27B")
    # 검증 엔드포인트가 없으면 생성 엔드포인트를 재사용한다. 교차 모델 검증의
    # 이점은 잃지만 단일 GPU 환경에서도 파이프라인은 돌아가야 한다.
    verifier = (
        _endpoint(llm["verifier"], default_model="gemma-4-26b")
        if "verifier" in llm
        else _endpoint(llm.get("generator", {}), default_model="Qwen3.6-27B")
    )
    # 검증은 짧은 입력에 결론 토큰 하나면 되므로 예산을 따로 조인다.
    verifier.max_output_tokens = min(verifier.max_output_tokens, 200)

    review = ReviewConfig(**_subset(data.get("review", {}), ReviewConfig))
    grounding = GroundingConfig(**_subset(data.get("grounding", {}), GroundingConfig))
    chunking = ChunkingConfig(**_subset(data.get("chunking", {}), ChunkingConfig))

    taxonomy_path = data.get("taxonomy_path")
    return Config(
        generator=generator,
        verifier=verifier,
        review=review,
        grounding=grounding,
        chunking=chunking,
        taxonomy_path=Path(taxonomy_path) if taxonomy_path else None,
        source=resolved,
    )


def _endpoint(raw: dict, *, default_model: str) -> EndpointConfig:
    return EndpointConfig(
        base_url=raw.get("base_url", "http://localhost:8000/v1"),
        model=raw.get("model", default_model),
        api_key=raw.get("api_key", "EMPTY"),
        temperature=float(raw.get("temperature", 0.0)),
        max_output_tokens=int(raw.get("max_output_tokens", 1024)),
        max_input_tokens=int(raw.get("max_input_tokens", 8192)),
        timeout=float(raw.get("timeout", 120.0)),
        max_retries=int(raw.get("max_retries", 3)),
        structured_output_mode=raw.get("structured_output_mode", "response_format"),
        extra_body=dict(raw.get("extra_body", {})),
    )


def _subset(raw: dict, cls: type) -> dict:
    """dataclass 가 아는 키만 통과시킨다. 오타난 설정 키에 조용히 당하지 않도록."""
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"{cls.__name__} 에 알 수 없는 설정 키: {sorted(unknown)}. "
            f"사용 가능한 키: {sorted(known)}"
        )
    return {k: v for k, v in raw.items() if k in known}
