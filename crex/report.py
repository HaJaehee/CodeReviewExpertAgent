"""리뷰 결과 출력.

세 가지 형식을 낸다.

- **Markdown** — 사람이 읽는다. 개발자에게 그대로 붙여넣을 수 있어야 한다.
- **SARIF** — 도구가 읽는다. IDE·대시보드가 표준으로 지원한다.
- **JSON** — 평가 하네스가 읽는다. 기각 내역까지 전부 담는다.

기각된 지적은 Markdown 에 넣지 않는다. 리뷰어가 볼 필요가 없다. 대신 JSON 에
사유와 함께 전부 남긴다 — 필터 튜닝의 유일한 근거 데이터이기 때문이다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import __version__
from .schema import Finding, ReviewResult, Severity

_SEVERITY_LABEL = {
    Severity.HIGH: "높음",
    Severity.MEDIUM: "중간",
    Severity.LOW: "낮음",
}

_SEVERITY_MARK = {Severity.HIGH: "🔴", Severity.MEDIUM: "🟡", Severity.LOW: "⚪"}

_SARIF_LEVEL = {Severity.HIGH: "error", Severity.MEDIUM: "warning", Severity.LOW: "note"}


def to_markdown(result: ReviewResult, *, title: str = "코드리뷰 결과") -> str:
    lines: list[str] = [f"# {title}", ""]

    if not result.kept:
        # "0건"이 두 가지를 뜻할 수 있다는 것이 이 도구의 가장 위험한 지점이다.
        # 코드가 깨끗한 것과 파이프라인이 고장난 것을 같은 문장으로 보고하면
        # 사용자는 고장을 신뢰로 착각한다.
        lines.append("지적 사항 없음." if result.healthy else _failure_banner(result))
        lines.append("")
        lines.append(_stats_block(result))
        return "\n".join(lines)

    by_severity: dict[Severity, list[Finding]] = defaultdict(list)
    for finding in result.kept:
        by_severity[finding.severity].append(finding)

    counts = ", ".join(
        f"{_SEVERITY_LABEL[s]} {len(by_severity[s])}건"
        for s in (Severity.HIGH, Severity.MEDIUM, Severity.LOW)
        if by_severity[s]
    )
    lines.append(f"총 {len(result.kept)}건 ({counts})")
    lines.append("")

    for severity in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        findings = by_severity.get(severity)
        if not findings:
            continue
        lines.append(f"## {_SEVERITY_MARK[severity]} {_SEVERITY_LABEL[severity]}")
        lines.append("")
        for finding in findings:
            lines.extend(_render_finding(finding))
        lines.append("")

    lines.append(_stats_block(result))
    return "\n".join(lines)


def _failure_banner(result: ReviewResult) -> str:
    """실패로 0건이 된 경우의 경고. 리포트 맨 위에 둔다."""
    causes: list[str] = []
    if result.generation_errors:
        causes.append(f"생성 호출 실패 {result.generation_errors}건")
    if result.verification_errors:
        causes.append(f"검증 호출 실패 {result.verification_errors}건")
    other = len(result.errors) - result.generation_errors - result.verification_errors
    if other > 0:
        causes.append(f"기타 오류 {other}건")

    detail = ", ".join(causes) or f"오류 {len(result.errors)}건"
    return (
        f"> ⚠️ **이 결과는 신뢰할 수 없다 — {detail}.**\n"
        "> 지적이 0건인 것은 코드가 깨끗해서가 아니라 파이프라인이 끝까지 돌지\n"
        "> 못했기 때문이다. `python -m crex doctor` 로 엔드포인트를 점검하라."
    )


def _render_finding(finding: Finding) -> list[str]:
    location = f"`{finding.path}:{finding.line}`"
    if finding.end_line and finding.end_line != finding.line:
        location = f"`{finding.path}:{finding.line}-{finding.end_line}`"

    lines = [f"### {location} — {finding.rule_id}", "", finding.message, ""]
    if finding.suggestion:
        lines.extend(["```", finding.suggestion, "```", ""])
    return lines


def _stats_block(result: ReviewResult) -> str:
    """실행 통계. 기각률이 정상 범위(40~60%)인지 한눈에 보이게 한다."""
    timings = " / ".join(f"{k} {v:.1f}s" for k, v in result.timings.items())
    lines = [
        "---",
        "",
        "<details><summary>실행 통계</summary>",
        "",
        f"- 리뷰한 청크: {result.chunks_reviewed}개",
        f"- 정적분석 결과: {result.static_findings_count}건",
        f"- 생성된 지적: {result.total_raw}건 → 검증 통과 {len(result.kept)}건 "
        f"(기각률 {result.reject_rate:.1%})",
    ]
    if timings:
        lines.append(f"- 소요: {timings}")
    if result.errors:
        lines.append(
            f"- 오류 {len(result.errors)}건"
            f" (생성 {result.generation_errors}, 검증 {result.verification_errors}):"
        )
        lines.extend(f"  - {e.splitlines()[0]}" for e in result.errors[:5])
        if len(result.errors) > 5:
            lines.append(f"  - ... 외 {len(result.errors) - 5}건")
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def to_sarif(result: ReviewResult, *, tool_name: str = "CREX", version: str = __version__) -> dict:
    """SARIF 2.1.0. IDE 와 품질 대시보드가 그대로 읽는다."""
    rules_seen: dict[str, dict] = {}
    sarif_results = []

    for finding in result.kept:
        if finding.rule_id not in rules_seen:
            rules_seen[finding.rule_id] = {
                "id": finding.rule_id,
                "properties": {"category": finding.dimension.value},
            }
        sarif_results.append(
            {
                "ruleId": finding.rule_id,
                "level": _SARIF_LEVEL[finding.severity],
                "message": {"text": finding.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": finding.path},
                            "region": {
                                "startLine": finding.line,
                                "endLine": finding.end_line or finding.line,
                            },
                        }
                    }
                ],
            }
        )

    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "version": version,
                        "rules": list(rules_seen.values()),
                    }
                },
                "results": sarif_results,
            }
        ],
    }


def write_all(result: ReviewResult, out_dir: Path, *, stem: str = "review") -> dict[str, Path]:
    """세 형식을 모두 쓰고 경로를 돌려준다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": out_dir / f"{stem}.md",
        "sarif": out_dir / f"{stem}.sarif",
        "json": out_dir / f"{stem}.json",
    }
    paths["markdown"].write_text(to_markdown(result), encoding="utf-8")
    paths["sarif"].write_text(
        json.dumps(to_sarif(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths["json"].write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return paths
