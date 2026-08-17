"""룰 택소노미 로더 및 변환기.

`rules/taxonomy.toml` 이 단일 진실 공급원(single source of truth)이다. 여기서
두 가지 산출물이 나온다.

1. **`.opencodereview/rule.json`** — OCR 위임 모드에서 쓰는 커스텀 룰 파일
2. **RuleChecker 프롬프트 조각** — 자체 생성 모드에서 청크마다 주입하는 룰 목록

룰 선택에는 상한이 있다. 청크 하나에 50개 룰을 다 넣으면 프롬프트가 길어져
소형 모델의 정밀도가 떨어진다. 언어별로 걸러 15개 이하만 활성화한다.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .schema import Dimension, Language, Severity

DEFAULT_TAXONOMY = Path(__file__).resolve().parent.parent / "rules" / "taxonomy.toml"

#: 청크 하나에 주입할 룰 개수 상한. 늘리면 재현율은 오르지만 정밀도가 떨어진다.
MAX_RULES_PER_CHUNK = 15

#: 언어별 glob 패턴. OCR rule.json 생성에 쓴다.
LANGUAGE_GLOBS: dict[Language, str] = {
    Language.CPP: "**/*.{cpp,cc,cxx,c,h,hpp,hh,hxx}",
    Language.CSHARP: "**/*.cs",
    Language.PYTHON: "**/*.py",
}

#: 리뷰 대상에서 제외할 경로. 생성 코드와 서드파티를 빼는 것만으로 오탐이 크게 준다.
DEFAULT_EXCLUDE = [
    "**/generated/**",
    "**/gen/**",
    "**/third_party/**",
    "**/vendor/**",
    "**/node_modules/**",
    "**/obj/**",
    "**/bin/**",
    "**/*.pb.*",
    "**/*.g.cs",
    "**/*.designer.cs",
    "**/*_pb2.py",
    "**/migrations/**",
]

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


@dataclass(frozen=True)
class Rule:
    id: str
    language: str  # "cpp" | "csharp" | "python" | "any"
    dimension: Dimension
    severity: Severity
    title: str
    criteria: str
    counter: str = ""
    #: 제시된 스니펫만으로 판정 가능한가. false 면 소형 모델이 반드시 지어낸다.
    chunk_local: bool = True

    def applies_to(self, language: Language) -> bool:
        return self.language == "any" or self.language == language.value

    def render(self) -> str:
        """프롬프트에 넣을 형태로 렌더한다."""
        parts = [
            f"### {self.id} ({self.severity.value}) — {self.title}",
            f"판정 기준: {self.criteria.strip()}",
        ]
        if self.counter:
            parts.append(f"지적하지 말 것: {self.counter.strip()}")
        return "\n".join(parts)

    def render_compact(self) -> str:
        """OCR rule.json 용 한 줄 요약."""
        text = f"[{self.id}] {self.title}. {_collapse(self.criteria)}"
        if self.counter:
            text += f" 단, {_collapse(self.counter)}"
        return text


class Taxonomy:
    def __init__(self, rules: list[Rule], version: str = "") -> None:
        self.rules = rules
        self.version = version

    def __len__(self) -> int:
        return len(self.rules)

    def for_language(
        self,
        language: Language,
        *,
        chunk_local_only: bool = True,
        max_rules: int = MAX_RULES_PER_CHUNK,
        dimensions: set[Dimension] | None = None,
    ) -> list[Rule]:
        """해당 언어에 적용할 룰을 우선순위 순으로 골라 상한까지만 돌려준다."""
        selected = [r for r in self.rules if r.applies_to(language)]
        if chunk_local_only:
            selected = [r for r in selected if r.chunk_local]
        if dimensions:
            selected = [r for r in selected if r.dimension in dimensions]

        selected.sort(key=lambda r: (_SEVERITY_ORDER[r.severity], r.id))
        return selected[:max_rules]

    def by_id(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def valid_ids(self) -> set[str]:
        return {r.id for r in self.rules}

    def to_ocr_rule_json(
        self, *, include: list[str] | None = None, exclude: list[str] | None = None
    ) -> dict:
        """OCR 의 `.opencodereview/rule.json` 형식으로 변환한다.

        OCR 은 path glob + 자연어 rule 을 선언 순서대로 평가하고 first-match-wins 다.
        따라서 언어별로 한 항목씩만 만들고, 그 안에 룰을 번호 매겨 넣는다.
        """
        entries = []
        for language, glob in LANGUAGE_GLOBS.items():
            rules = self.for_language(language, max_rules=len(self.rules))
            if not rules:
                continue
            body = "\n".join(f"{i}. {r.render_compact()}" for i, r in enumerate(rules, 1))
            entries.append(
                {
                    "path": glob,
                    "rule": (
                        "다음 항목을 중심으로 검토하라. 각 지적에는 반드시 룰 ID를 "
                        f"대괄호로 표기하라.\n{body}"
                    ),
                }
            )

        payload: dict = {"rules": entries}
        if include:
            payload["include"] = include
        payload["exclude"] = exclude if exclude is not None else list(DEFAULT_EXCLUDE)
        return payload


def load_taxonomy(path: Path | str = DEFAULT_TAXONOMY) -> Taxonomy:
    path = Path(path)
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("rule", [])):
        try:
            rule = Rule(
                id=raw["id"],
                language=raw["language"],
                dimension=Dimension(raw["dimension"]),
                severity=Severity(raw["severity"]),
                title=raw["title"],
                criteria=raw["criteria"],
                counter=raw.get("counter", ""),
                chunk_local=bool(raw.get("chunk_local", True)),
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{path} 의 {index + 1}번째 룰이 잘못되었다: {exc}") from exc

        if rule.id in seen:
            # 룰 ID 는 평가·플라이휠의 조인 키다. 중복되면 통계가 조용히 뒤섞인다.
            raise ValueError(f"룰 ID 중복: {rule.id}")
        seen.add(rule.id)
        rules.append(rule)

    if not rules:
        raise ValueError(f"{path} 에 룰이 하나도 없다")

    return Taxonomy(rules, version=str(data.get("meta", {}).get("version", "")))


def render_rules_for_prompt(rules: list[Rule]) -> str:
    return "\n\n".join(rule.render() for rule in rules)


def _collapse(text: str) -> str:
    """여러 줄 criteria 를 한 줄로 접는다."""
    return " ".join(text.split())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m crex.rules",
        description="룰 택소노미를 검증하고 OCR rule.json 을 생성한다.",
    )
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="rule.json 출력 경로 (예: .opencodereview/rule.json). 생략하면 검증만 한다.",
    )
    parser.add_argument(
        "--include", nargs="*", default=None,
        help="리뷰 대상 경로 glob (예: src/**)",
    )
    parser.add_argument("--exclude", nargs="*", default=None)
    args = parser.parse_args(argv)

    try:
        taxonomy = load_taxonomy(args.taxonomy)
    except (OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    print(f"택소노미 v{taxonomy.version}: 룰 {len(taxonomy)}개")
    for language in LANGUAGE_GLOBS:
        active = taxonomy.for_language(language)
        total = len([r for r in taxonomy.rules if r.applies_to(language)])
        print(f"  {language.value:8s} 전체 {total:2d}개 → 청크당 활성 {len(active):2d}개")

    by_dimension: dict[str, int] = {}
    for rule in taxonomy.rules:
        by_dimension[rule.dimension.value] = by_dimension.get(rule.dimension.value, 0) + 1
    print("  차원별: " + ", ".join(f"{k}={v}" for k, v in sorted(by_dimension.items())))

    if args.out:
        payload = taxonomy.to_ocr_rule_json(include=args.include, exclude=args.exclude)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n{args.out} 생성 완료 ({len(payload['rules'])}개 경로 항목)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
