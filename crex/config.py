"""설정 로딩.

JSON 한 파일로 전부 제어한다. 폐쇄망에서는 환경변수보다 파일이 다루기 쉽다
(장비마다 셸 프로파일이 제각각이고, 설정 내용을 감사 기록으로 남겨야 한다).

## JSON 에 없는 두 가지를 규칙으로 채운다

JSON 에는 주석이 없고 여러 줄 문자열이 없다. 설정 파일은 사람이 읽고 고치는
물건이라 둘 다 필요하다. 그래서 형식 자체는 순수 JSON 으로 두고 **약속으로**
채운다 — 파서를 바꾸지 않으므로 어떤 JSON 도구로 열어도 그대로 열린다.

1. **주석** — 키가 `//` 로 시작하면 설명이다. `strip_comment_keys()` 가 검증
   전에 걷어낸다. 값은 무엇이든 되고, 긴 설명은 문자열 배열로 적는다.
   TOML 주석과 다른 점이 하나 있고 그게 이득이다: 주석이 **데이터의 일부**라
   프로그램이 파일을 고쳐 써도 살아남는다 (`workspace.persist_key()`).
2. **여러 줄 글** — 문자열 설정은 문자열 배열로도 적을 수 있고, 읽을 때
   줄바꿈으로 이어 붙인다. `system_prompt` / `prompt_template` 처럼 긴 글을
   역슬래시 n 이스케이프 한 줄로 적으면 사람이 읽을 수 없는 파일이 된다.
   배열이 허용되는 곳은 **선언 타입이 문자열인 설정뿐**이다 — `analyzers`
   처럼 원래 목록인 설정은 목록 그대로 남는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import EndpointConfig

DEFAULT_CONFIG_NAMES = ("crex.json", ".crex.json")

#: 예전 TOML 설정. 읽지는 않지만 **있으면 알린다.** 조용히 무시하면 파일을
#: 고쳤는데 아무것도 안 바뀌는, 이 프로젝트가 가장 싫어하는 실패가 된다.
LEGACY_CONFIG_NAMES = ("crex.toml", ".crex.toml")

#: 설명 키의 접두사. 이 접두사로 시작하는 키는 설정이 아니다.
COMMENT_PREFIX = "//"


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
    absolute_max_lines: int = 150
    #: diff 와 파일 내용이 어긋날 때: "raise" | "warn" | "ignore"
    on_mismatch: str = "raise"


#: 현재 구현된 생성 모드. "ocr"(alibaba/open-code-review 위임)은 Phase 1 에서
#: 실제 바이너리의 출력 스키마를 확인한 뒤 추가한다. 그때까지는 설정해도 받지 않는다.
SUPPORTED_MODES = ("native",)

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

#: 검증 엔드포인트의 출력 예산 상한. 검증 응답은 결론(yes/no) 하나와 짧은
#: 근거뿐이라 더 줄 이유가 없고, 크게 잡으면 GPU 만 오래 붙든다.
VERIFIER_MAX_OUTPUT_TOKENS = 400


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
                f"review.mode = {self.mode!r} 는 아직 구현되지 않았습니다. "
                f"사용 가능: {list(SUPPORTED_MODES)}"
            )
        if self.min_severity not in SEVERITY_ORDER:
            raise ValueError(
                f"review.min_severity = {self.min_severity!r} 가 잘못되었습니다. "
                f"사용 가능: {list(SEVERITY_ORDER)}"
            )


#: 최상위에서 받는 키. 오타를 조용히 무시하면 "설정했는데 안 먹는다"가 된다.
TOP_LEVEL_KEYS = frozenset(
    {"llm", "review", "grounding", "chunking", "taxonomy_path", "workspace"}
)


@dataclass
class Config:
    generator: EndpointConfig
    verifier: EndpointConfig
    review: ReviewConfig = field(default_factory=ReviewConfig)
    grounding: GroundingConfig = field(default_factory=GroundingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    taxonomy_path: Path | None = None
    #: 리뷰 대상 저장소 루트. 비우면 현재 디렉터리에서 git 루트를 찾는다.
    #: 명령줄 --workspace 와 환경변수가 이것보다 우선한다 (crex/workspace.py).
    workspace: Path | None = None
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


def find_legacy_config(start: Path | None = None) -> Path | None:
    """같은 방식으로 예전 TOML 설정을 찾는다. 안내에만 쓴다."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for name in LEGACY_CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def strip_comment_keys(value: Any) -> Any:
    """`//` 로 시작하는 키를 걷어낸다. 검증에 들어가기 전에 한 번 돌린다.

    JSON 에는 주석이 없으므로 설명을 데이터로 적는다. 중첩 객체 안쪽까지
    내려가며 지운다. 설명 자체는 무엇이든 될 수 있어서 값은 보지 않는다.

    **`extra_body` 안까지 들어간다.** 그쪽은 vLLM 에 그대로 넘기는 값이라
    건드리지 않는 편이 안전해 보이지만, 설명을 못 다는 구멍을 하나 두면
    그 구멍이 하필 가장 설명이 필요한 자리다. `//` 로 시작하는 파라미터를
    받는 추론 서버는 없다.
    """
    if isinstance(value, dict):
        return {
            key: strip_comment_keys(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.startswith(COMMENT_PREFIX))
        }
    if isinstance(value, list):
        return [strip_comment_keys(item) for item in value]
    return value


def load_config(path: Path | str | None = None, *, search_from: Path | None = None) -> Config:
    """설정 파일을 읽는다. 경로가 없으면 탐색하고, 그래도 없으면 기본값을 쓴다.

    `search_from` 은 탐색 시작 위치다. 비우면 현재 디렉터리에서 시작한다.
    """
    resolved = Path(path) if path else find_config(search_from)
    if resolved is None:
        _reject_legacy(find_legacy_config(search_from))
    elif resolved.suffix.lower() == ".toml":
        _reject_legacy(resolved)

    data: dict = {}
    if resolved and resolved.is_file():
        data = strip_comment_keys(_read_json(resolved))

    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            f"설정 파일 최상위에 알 수 없는 키: {sorted(unknown)}. "
            f"사용 가능: {sorted(TOP_LEVEL_KEYS)}"
        )

    llm = data.get("llm", {})
    generator = _endpoint(llm.get("generator", {}), default_model="Qwen3.6-27B")
    # 검증 엔드포인트가 없으면 생성 엔드포인트를 재사용한다. 교차 모델 검증의
    # 이점은 잃지만 단일 GPU 환경에서도 파이프라인은 돌아가야 한다.
    verifier = (
        _endpoint(llm["verifier"], default_model="gemma-4-26b")
        if "verifier" in llm
        else _endpoint(llm.get("generator", {}), default_model="Qwen3.6-27B")
    )
    # 검증은 짧은 입력에 결론 하나와 짧은 근거면 되므로 예산을 따로 조인다.
    # 상한이 VERIFIER_MAX_OUTPUT_TOKENS 인 이유: reason 이 500자(≈170토큰)까지
    # 허용되므로 그보다 낮게 조이면 근거가 문장 중간에서 잘린다.
    verifier.max_output_tokens = min(verifier.max_output_tokens, VERIFIER_MAX_OUTPUT_TOKENS)

    review = ReviewConfig(**_subset(data.get("review", {}), ReviewConfig))
    grounding = GroundingConfig(**_subset(data.get("grounding", {}), GroundingConfig))
    chunking = ChunkingConfig(**_subset(data.get("chunking", {}), ChunkingConfig))

    taxonomy_path = _text(data.get("taxonomy_path"), "taxonomy_path")
    return Config(
        generator=generator,
        verifier=verifier,
        review=review,
        grounding=grounding,
        chunking=chunking,
        taxonomy_path=Path(taxonomy_path) if taxonomy_path else None,
        workspace=_anchor(_text(data.get("workspace"), "workspace"), resolved),
        source=resolved,
    )


def _anchor(value: str | None, config_path: Path | None) -> Path | None:
    """설정 파일 안의 상대경로는 **설정 파일이 있는 디렉터리** 기준이다.

    현재 디렉터리 기준으로 두면 어디서 실행했느냐에 따라 대상 저장소가 바뀐다.
    반입 번들을 통째로 옮겨도 설정이 그대로 살아 있어야 한다.
    """
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.is_absolute() or config_path is None:
        return path
    return (config_path.resolve().parent / path).resolve()


#: 엔드포인트 테이블에서 받는 키. dataclass 필드와 일치해야 한다.
ENDPOINT_KEYS = frozenset(
    {
        "base_url", "model", "api_key", "temperature",
        "max_output_tokens", "max_input_tokens", "timeout", "max_retries",
        "structured_output_mode", "guided_decoding_backend", "extra_body",
    }
)


def _endpoint(raw: dict, *, default_model: str) -> EndpointConfig:
    # 여기도 다른 섹션과 똑같이 오타를 막는다. base_url 을 bas_url 로 쓰면 조용히
    # localhost 기본값으로 떨어져, 엔드포인트를 바꿨는데 안 바뀌는 상황이 된다.
    unknown = set(raw) - ENDPOINT_KEYS
    if unknown:
        raise ValueError(
            f"llm 엔드포인트 설정에 알 수 없는 키: {sorted(unknown)}. "
            f"사용 가능한 키: {sorted(ENDPOINT_KEYS)}"
        )
    raw = _join_text_fields(raw, EndpointConfig, prefix="llm")
    return EndpointConfig(
        base_url=raw.get("base_url", "http://localhost:8000/v1"),
        model=raw.get("model", default_model),
        api_key=raw.get("api_key", "EMPTY"),
        temperature=float(raw.get("temperature", 0.0)),
        max_output_tokens=int(raw.get("max_output_tokens", 1600)),
        max_input_tokens=int(raw.get("max_input_tokens", 8192)),
        timeout=float(raw.get("timeout", 120.0)),
        max_retries=int(raw.get("max_retries", 3)),
        structured_output_mode=raw.get("structured_output_mode", "auto"),
        guided_decoding_backend=raw.get("guided_decoding_backend", ""),
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
    section = cls.__name__.removesuffix("Config").lower()
    return _join_text_fields({k: v for k, v in raw.items() if k in known}, cls, prefix=section)


# --------------------------------------------------------------------------
# 여러 줄 글 — 문자열 설정은 배열로도 적는다
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    """설정 파일을 읽는다. 깨진 JSON 은 줄·칸까지 짚어 준다.

    `json` 의 기본 오류 메시지는 영어이고 파일 이름이 없다. 설정 파일은 손으로
    고치는 물건이라 쉼표 하나 때문에 여기서 멈추는 일이 잦다 — 어디를 고칠지
    바로 알려 주지 않으면 사용자가 파일 전체를 의심하게 된다.
    """
    try:
        # utf-8-sig 는 메모장이 붙여 놓은 BOM 을 걷어낸다 (없으면 그냥 utf-8 이다).
        # BOM 이 남으면 json 이 첫 글자에서 바로 실패해 원인을 짐작하기 어렵다.
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{path} 를 읽지 못했습니다 ({error.lineno}번째 줄 {error.colno}칸): {error.msg}.\n"
            f"  JSON 은 마지막 항목 뒤의 쉼표를 허용하지 않고, 주석은 "
            f'"// ..." 키로 적어야 합니다.'
        ) from error
    if not isinstance(data, dict):
        raise ValueError(f"{path} 의 최상위는 객체여야 합니다. 지금은 {type(data).__name__} 입니다.")
    return data


def _reject_legacy(path: Path | None) -> None:
    """예전 TOML 설정을 만나면 멈춘다. 조용히 무시하지 않는다."""
    if path is None:
        return
    raise ValueError(
        f"{path} 는 더 이상 읽지 않습니다. 설정 파일은 JSON 으로 바뀌었습니다.\n"
        f"  {path.with_suffix('.json').name} 을 만들어 옮겨 적으십시오. "
        f"양식은 crex.example.json 과 docs/user_manual/configuration.md 에 있습니다."
    )


def _text(value: Any, where: str) -> Any:
    """문자열 설정 하나를 정규화한다. 배열이면 줄바꿈으로 이어 붙인다.

    JSON 에는 여러 줄 문자열이 없다. 프롬프트처럼 긴 글을 한 줄에 넣으면
    파일을 열어도 읽을 수가 없으므로, 줄 단위로 끊어 배열로 적게 한다.
    """
    if isinstance(value, list):
        bad = [item for item in value if not isinstance(item, str)]
        if bad:
            raise ValueError(
                f"{where} 를 배열로 적을 때는 원소가 전부 문자열이어야 합니다. "
                f"문자열이 아닌 값: {bad!r}"
            )
        return "\n".join(value)
    return value


def _join_text_fields(raw: dict, cls: type, *, prefix: str) -> dict:
    """dataclass 가 문자열로 선언한 필드에만 배열 이어붙이기를 적용한다.

    허용 목록을 손으로 관리하지 않는 것이 요점이다. `analyzers: list[str]` 처럼
    원래 목록인 설정까지 이어 붙이면 분석기 이름 세 개가 한 덩어리가 된다.
    선언 타입을 보면 새 설정을 추가할 때 여기를 손볼 일이 없다.
    """
    return {
        key: _text(value, f"{prefix}.{key}") if key in _text_fields(cls) else value
        for key, value in raw.items()
    }


def _text_fields(cls: type) -> frozenset[str]:
    """`str` 로 선언된 필드 이름. `list[str]` 과 `dict` 는 제외한다.

    `from __future__ import annotations` 때문에 애너테이션이 문자열로 남아 있어
    타입 객체가 아니라 글자로 판단한다. 타이핑을 실제 객체로 되살리려면
    `typing.get_type_hints()` 가 필요한데, 그러자고 임포트 순환을 감수할 값이 없다.
    """
    return frozenset(
        name
        for name, spec in cls.__dataclass_fields__.items()
        if "str" in str(spec.type) and "list" not in str(spec.type) and "dict" not in str(spec.type)
    )
