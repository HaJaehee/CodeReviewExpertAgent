"""리뷰 결과 출력.

세 가지 형식을 낸다.

- **Markdown** — 사람이 읽는다. 개발자에게 그대로 붙여넣을 수 있어야 한다.
- **SARIF** — 도구가 읽는다. IDE·대시보드가 표준으로 지원한다.
- **JSON** — 평가 하네스가 읽는다. 기각 내역까지 전부 담는다.

기각된 지적은 Markdown 에 넣지 않는다. 리뷰어가 볼 필요가 없다. 대신 JSON 에
사유와 함께 전부 남긴다 — 필터 튜닝의 유일한 근거 데이터이기 때문이다.
"""

from __future__ import annotations

import html
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

_REJECT_LABEL = {
    "line_out_of_range": "범위 밖 라인",
    "line_not_changed": "미변경 라인",
    "verdict_no": "검증자 기각",
    "code_not_found": "코드가 스니펫에 없음",
    "duplicate": "중복 지적",
    "filter_error": "검증 호출 실패",
    "rule_excluded": "룰 제외",
}


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
        f"> ⚠️ **이 결과는 신뢰할 수 없습니다 — {detail}.**\n"
        "> 지적이 0건인 것은 코드가 깨끗해서가 아니라 파이프라인이 끝까지 돌지\n"
        "> 못했기 때문입니다. `python -m crex doctor` 로 엔드포인트를 점검하십시오."
    )


def _render_finding(finding: Finding) -> list[str]:
    location = f"`{finding.path}:{finding.line}`"
    if finding.end_line and finding.end_line != finding.line:
        location = f"`{finding.path}:{finding.line}-{finding.end_line}`"

    lines = [
        "<details open>",
        f"<summary><b>{location} — {finding.rule_id}</b></summary>",
        "",
        finding.message,
        "",
    ]
    if finding.verifier_comment:
        lines.extend([f"> 💬 **검증관 코멘트**: {finding.verifier_comment}", ""])
    if finding.suggestion:
        lines.extend(["```", finding.suggestion, "```", ""])
    lines.extend(["</details>", ""])
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


def to_html(result: ReviewResult, *, title: str = "코드리뷰 결과") -> str:
    """crex.viz 스타일의 단독 실행 가능한 HTML 리포트를 생성한다."""
    chunks = result.chunks_reviewed
    statics = result.static_findings_count
    total_raw = result.total_raw or (len(result.kept) + len(result.rejected))
    det_rejects = len([v for v in result.rejected if v.short_circuited])
    llm_rejects = len([v for v in result.rejected if not v.short_circuited])
    kept_count = len(result.kept)
    reject_rate_str = f"{round(result.reject_rate * 100)}%" if total_raw else "—"

    table_rows: list[str] = []

    # 1. 유지된 지적 (kept)
    for idx, f in enumerate(result.kept, 1):
        loc = f"{f.path}:{f.line}"
        if f.end_line and f.end_line != f.line:
            loc = f"{f.path}:{f.line}-{f.end_line}"
        sugg_html = ""
        if f.suggestion:
            sugg_html = (
                f'<div class="v-sugg"><span class="v-sugg-tag">수정 제안</span>'
                f'<pre class="v-sugg-code"><code>{html.escape(f.suggestion)}</code></pre></div>'
            )
        comm_html = ""
        if f.verifier_comment:
            comm_html = (
                f'<div class="v-comment"><span class="v-comment-tag">검증 코멘트:</span> '
                f'{html.escape(f.verifier_comment)}</div>'
            )
        msg_html = f'<div class="c-msg-text">{html.escape(f.message)}</div>{sugg_html}{comm_html}'

        row_html = (
            f'<tr data-id="kept-{idx}" data-kept="true" data-kind="verdict">\n'
            f'  <td class="c-fold"><button class="btn-fold" type="button" title="접기/펼치기">접기</button></td>\n'
            f'  <td class="c-where">{html.escape(loc)}</td>\n'
            f'  <td><span class="sev" data-sev="{f.severity.value}">{_SEVERITY_LABEL.get(f.severity, f.severity.value)}</span></td>\n'
            f'  <td class="c-rule">{html.escape(f.rule_id)}</td>\n'
            f'  <td class="c-msg">{msg_html}</td>\n'
            f'  <td class="c-verdict"><span class="tag tag-yes">유지</span></td>\n'
            f'  <td class="c-reason">—</td>\n'
            f'</tr>'
        )
        table_rows.append(row_html)

    # 2. 기각된 지적 (rejected)
    for idx, v in enumerate(result.rejected, 1):
        f = v.finding
        loc = f"{f.path}:{f.line}"
        if f.end_line and f.end_line != f.line:
            loc = f"{f.path}:{f.line}-{f.end_line}"
        reason_label = _REJECT_LABEL.get(v.reject_reason.value if v.reject_reason else "", "기각")
        sugg_html = ""
        if f.suggestion:
            sugg_html = (
                f'<div class="v-sugg"><span class="v-sugg-tag">수정 제안</span>'
                f'<pre class="v-sugg-code"><code>{html.escape(f.suggestion)}</code></pre></div>'
            )
        comm_html = ""
        if f.verifier_comment and f.verifier_comment != v.reason:
            comm_html = (
                f'<div class="v-comment"><span class="v-comment-tag">검증 코멘트:</span> '
                f'{html.escape(f.verifier_comment)}</div>'
            )
        msg_html = f'<div class="c-msg-text">{html.escape(f.message)}</div>{sugg_html}{comm_html}'

        row_html = (
            f'<tr data-id="rej-{idx}" data-kept="false" data-kind="verdict">\n'
            f'  <td class="c-fold"><button class="btn-fold" type="button" title="접기/펼치기">접기</button></td>\n'
            f'  <td class="c-where">{html.escape(loc)}</td>\n'
            f'  <td><span class="sev" data-sev="{f.severity.value}">{_SEVERITY_LABEL.get(f.severity, f.severity.value)}</span></td>\n'
            f'  <td class="c-rule">{html.escape(f.rule_id)}</td>\n'
            f'  <td class="c-msg">{msg_html}</td>\n'
            f'  <td class="c-verdict"><span class="tag tag-no">{html.escape(reason_label)}</span></td>\n'
            f'  <td class="c-reason">{html.escape(v.reason or "")}</td>\n'
            f'</tr>'
        )
        table_rows.append(row_html)

    tbody_content = "\n".join(table_rows) if table_rows else '<tr id="empty-row" class="empty"><td colspan="7">지적 사항이 없습니다.</td></tr>'

    banner_html = ""
    if not result.healthy:
        causes = []
        if result.generation_errors:
            causes.append(f"생성 호출 실패 {result.generation_errors}건")
        if result.verification_errors:
            causes.append(f"검증 호출 실패 {result.verification_errors}건")
        other = len(result.errors) - result.generation_errors - result.verification_errors
        if other > 0:
            causes.append(f"기타 오류 {other}건")
        detail = ", ".join(causes) or f"오류 {len(result.errors)}건"
        banner_html = (
            f'<div class="banner">⚠️ <b>이 결과는 신뢰할 수 없습니다 — {html.escape(detail)}.</b> '
            f'파이프라인이 정상적으로 완료되지 않았습니다.</div>'
        )

    timings_str = " / ".join(f"{k} {v:.1f}s" for k, v in result.timings.items()) if result.timings else "—"

    errors_html = ""
    if result.errors:
        err_items = "".join(f"<li>{html.escape(e.splitlines()[0])}</li>" for e in result.errors[:5])
        errors_html = f"<ul>{err_items}</ul>"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} — CREX v{__version__}</title>
  <style>
    :root {{
      --bg: #0b0e14;
      --bg-panel: #12161f;
      --bg-sunken: #0d1119;
      --bg-hover: #1a2029;
      --line: #222a36;
      --line-soft: #1a212b;
      --fg: #dfe5ef;
      --fg-dim: #8b97a8;
      --fg-faint: #5c6779;
      --gen: #38bdf8;
      --gen-soft: rgba(56, 189, 248, 0.13);
      --ver: #a78bfa;
      --ver-soft: rgba(167, 139, 250, 0.13);
      --keep: #4ade80;
      --keep-soft: rgba(74, 222, 128, 0.12);
      --drop: #f87171;
      --drop-soft: rgba(248, 113, 113, 0.11);
      --warn: #fbbf24;
      --high: #f87171;
      --medium: #fbbf24;
      --low: #64748b;
      --radius: 8px;
      --mono: ui-monospace, "Cascadia Mono", "D2Coding", Consolas, "Courier New", monospace;
      --sans: -apple-system, "Segoe UI", "Malgun Gothic", "맑은 고딕", system-ui, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--fg);
      font-family: var(--sans);
      font-size: 13.5px;
      line-height: 1.5;
      padding: 24px;
    }}
    .container {{ max-width: 1320px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 20px;
      background: linear-gradient(180deg, #141926, var(--bg-panel));
      border: 1px solid var(--line);
      border-radius: var(--radius);
    }}
    .brand {{ display: flex; align-items: baseline; gap: 10px; }}
    .mark {{ font-family: var(--mono); font-weight: 700; font-size: 20px; color: var(--gen); }}
    .brand-sub {{ color: var(--fg-dim); font-size: 13px; }}
    .top-meta {{ display: flex; align-items: center; gap: 8px; }}
    .chip {{
      font-family: var(--mono); font-size: 11.5px; color: var(--fg-dim);
      background: var(--bg-sunken); border: 1px solid var(--line);
      border-radius: 20px; padding: 3px 11px;
    }}
    .banner {{
      background: rgba(248, 113, 113, 0.12);
      border: 1px solid var(--drop);
      border-radius: var(--radius);
      padding: 12px 16px;
      color: #fca5a5;
      font-size: 13px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px;
    }}
    .metric {{
      background: var(--bg-panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px 14px;
    }}
    .metric b {{ display: block; font-family: var(--mono); font-size: 20px; font-weight: 600; line-height: 1.2; }}
    .metric span {{ font-size: 11px; color: var(--fg-faint); }}
    .metric-gen b {{ color: var(--gen); }}
    .metric-ver b {{ color: var(--ver); }}
    .metric-keep b {{ color: var(--keep); }}
    .panel {{
      background: var(--bg-panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 16px 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    .panel-head h2 {{ font-size: 14.5px; font-weight: 600; margin: 0; }}
    .tabs {{ display: flex; gap: 6px; }}
    .tab {{
      font: inherit; font-size: 12px; padding: 4px 12px; border-radius: 20px;
      border: 1px solid var(--line); background: transparent; color: var(--fg-dim);
      cursor: pointer;
    }}
    .tab.is-on {{ border-color: var(--gen); color: var(--gen); background: var(--gen-soft); }}
    .badge {{
      background: var(--bg-sunken); border: 1px solid var(--line); border-radius: 10px;
      padding: 1px 6px; font-size: 10.5px; margin-left: 4px;
    }}
    .verdict-actions {{ display: inline-flex; align-items: center; gap: 10px; }}
    .check-inline {{ display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--fg-dim); cursor: pointer; user-select: none; }}
    .check-inline input {{ accent-color: var(--gen); cursor: pointer; margin: 0; }}
    .btn {{
      font: inherit; font-size: 12px; padding: 4px 10px; border-radius: 4px;
      border: 1px solid var(--line); background: var(--bg-sunken); color: var(--fg-dim);
      cursor: pointer; transition: all 0.15s ease;
    }}
    .btn:hover {{ background: var(--line-soft); color: var(--fg); border-color: var(--fg-faint); }}
    .table-wrap {{ overflow-x: auto; }}
    .verdicts {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
    .verdicts th {{
      text-align: left; font-size: 11px; font-weight: 600; letter-spacing: 0.05em;
      text-transform: uppercase; color: var(--fg-faint); padding: 8px 10px 8px 0;
      border-bottom: 1px solid var(--line); white-space: nowrap;
    }}
    .verdicts th.th-fold {{ width: 38px; text-align: center; }}
    .verdicts td.c-fold {{ width: 38px; text-align: center; padding-right: 4px; }}
    .btn-fold {{
      display: inline-flex; align-items: center; justify-content: center;
      padding: 2px 5px; font-size: 10px; font-weight: 600; border-radius: 3px;
      border: 1px solid var(--line-soft); background: var(--bg-sunken); color: var(--fg-dim);
      cursor: pointer; line-height: 1;
    }}
    .btn-fold:hover {{ border-color: var(--fg-faint); color: var(--fg); background: var(--line-soft); }}
    .verdicts td {{ padding: 9px 10px 9px 0; border-bottom: 1px solid var(--line-soft); vertical-align: top; }}
    .verdicts tr:last-child td {{ border-bottom: none; }}
    .c-where {{ font-family: var(--mono); font-size: 12px; color: var(--fg-dim); white-space: nowrap; }}
    .c-rule {{ font-family: var(--mono); font-size: 11.5px; color: var(--fg-faint); white-space: nowrap; }}
    .c-msg-text {{ line-height: 1.45; word-break: break-word; }}
    .sev {{
      display: inline-block; font-family: var(--mono); font-size: 10.5px;
      font-weight: 600; padding: 1px 6px; border-radius: 3px; text-transform: uppercase;
    }}
    .sev[data-sev="high"] {{ color: var(--high); background: rgba(248, 113, 113, 0.12); border: 1px solid rgba(248, 113, 113, 0.35); }}
    .sev[data-sev="medium"] {{ color: var(--medium); background: rgba(251, 191, 36, 0.12); border: 1px solid rgba(251, 191, 36, 0.35); }}
    .sev[data-sev="low"] {{ color: var(--low); background: rgba(100, 116, 139, 0.12); border: 1px solid rgba(100, 116, 139, 0.35); }}
    .tag {{
      font-family: var(--mono); font-size: 10.5px; padding: 1px 6px; border-radius: 4px;
      border: 1px solid var(--line); color: var(--fg-faint); display: inline-block;
    }}
    .tag-yes {{ border-color: rgba(74, 222, 128, 0.5); color: var(--keep); background: var(--keep-soft); }}
    .tag-no {{ border-color: rgba(248, 113, 113, 0.5); color: var(--drop); background: var(--drop-soft); }}
    .tag-folded {{
      display: inline-block; font-family: var(--mono); font-size: 10px; font-weight: 600;
      color: var(--fg-faint); background: var(--bg-sunken); border: 1px solid var(--line-soft);
      border-radius: 3px; padding: 1px 4px; margin-right: 4px;
    }}
    .verdicts tr.is-folded {{ opacity: 0.55; background: rgba(255, 255, 255, 0.02); }}
    .verdicts tr.is-folded .v-sugg, .verdicts tr.is-folded .v-comment {{ display: none !important; }}
    .verdicts tr.is-folded .c-msg-text {{
      font-size: 11.5px; color: var(--fg-faint); white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; max-width: 320px;
    }}
    .v-sugg, .v-comment {{
      margin-top: 6px; padding: 6px 10px; border-radius: 4px; background: var(--bg-sunken);
      border: 1px solid var(--line-soft); font-size: 12px;
    }}
    .v-sugg-tag, .v-comment-tag {{
      font-family: var(--mono); font-size: 10.5px; font-weight: 600; color: var(--gen); margin-bottom: 3px; display: inline-block;
    }}
    .v-comment-tag {{ color: var(--ver); }}
    .v-sugg-code {{
      margin: 0; font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap; word-break: break-all; color: var(--fg);
    }}
    .c-reason {{ font-size: 11.5px; color: var(--fg-dim); line-height: 1.4; word-break: break-word; }}
    .stats-details {{ font-size: 12px; color: var(--fg-dim); }}
    .stats-details summary {{ cursor: pointer; color: var(--fg); font-weight: 500; margin-bottom: 6px; }}
    .stats-details ul {{ padding-left: 18px; margin: 4px 0; }}
    .stats-details li {{ margin: 2px 0; }}
  </style>
</head>
<body>
<div class="container">
  <header class="header">
    <div class="brand">
      <span class="mark">CREX</span>
      <span class="brand-sub">{html.escape(title)}</span>
    </div>
    <div class="top-meta">
      <span class="chip">v{__version__}</span>
      <span class="chip">리뷰 보고서</span>
    </div>
  </header>

  {banner_html}

  <section class="metrics">
    <div class="metric"><b>{chunks}</b><span>청크</span></div>
    <div class="metric"><b>{statics}</b><span>정적분석</span></div>
    <div class="metric metric-gen"><b>{total_raw}</b><span>생성된 지적</span></div>
    <div class="metric"><b>{det_rejects}</b><span>결정론적 기각</span></div>
    <div class="metric metric-ver"><b>{llm_rejects}</b><span>검증 기각</span></div>
    <div class="metric metric-keep"><b>{kept_count}</b><span>최종 유지</span></div>
    <div class="metric"><b>{reject_rate_str}</b><span>기각률</span></div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>판정 내역</h2>
      <div class="tabs" id="verdict-tabs">
        <button class="tab is-on" data-filter="all" type="button">전체 ({kept_count + len(result.rejected)})</button>
        <button class="tab" data-filter="kept" type="button">유지 ({kept_count})</button>
        <button class="tab" data-filter="rejected" type="button">기각 ({len(result.rejected)})</button>
        <button class="tab" data-filter="folded" type="button">접힘 <span class="badge" id="folded-badge" hidden>0</span></button>
      </div>
      <div class="verdict-actions">
        <label class="check-inline" title="접은 지적을 테이블에서 숨깁니다">
          <input id="hide-folded" type="checkbox">
          <span>접힌 항목 숨기기</span>
        </label>
        <button class="btn" id="btn-fold-all" type="button">모두 접기</button>
      </div>
    </div>

    <div class="table-wrap">
      <table class="verdicts">
        <thead>
          <tr>
            <th class="th-fold">접기</th><th>위치</th><th>심각도</th><th>룰</th><th>내용</th><th>판정</th><th>사유</th>
          </tr>
        </thead>
        <tbody>
          {tbody_content}
        </tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <details class="stats-details">
      <summary>실행 통계 및 상세 정보</summary>
      <ul>
        <li>소요 시간: {html.escape(timings_str)}</li>
        <li>검토 청크: {chunks}개</li>
        <li>정적분석 결과: {statics}건</li>
        <li>생성된 지적: {total_raw}건 → 최종 유지 {kept_count}건 (기각률 {reject_rate_str})</li>
        {f'<li>오류: {len(result.errors)}건</li>' if result.errors else ''}
      </ul>
      {errors_html}
    </details>
  </section>
</div>

<script>
(function() {{
  const folded = new Set();
  let filter = 'all';
  let hideFolded = false;

  function update() {{
    const rows = document.querySelectorAll('.verdicts tbody tr[data-kind]');
    let foldedCount = folded.size;
    const badge = document.getElementById('folded-badge');
    if (badge) {{
      badge.textContent = foldedCount;
      badge.hidden = !foldedCount;
    }}
    const btnFoldAll = document.getElementById('btn-fold-all');
    if (btnFoldAll) {{
      const allFolded = rows.length > 0 && Array.from(rows).every(r => folded.has(r.dataset.id));
      btnFoldAll.textContent = allFolded ? '모두 펼치기' : '모두 접기';
    }}

    let visibleCount = 0;
    rows.forEach(r => {{
      const id = r.dataset.id;
      const isFolded = folded.has(id);
      const isKept = r.dataset.kept === 'true';

      r.classList.toggle('is-folded', isFolded);
      const foldBtn = r.querySelector('.btn-fold');
      if (foldBtn) foldBtn.textContent = isFolded ? '펼치기' : '접기';

      let visible = true;
      if (filter === 'kept' && !isKept) visible = false;
      if (filter === 'rejected' && isKept) visible = false;
      if (filter === 'folded' && !isFolded) visible = false;
      if (filter !== 'folded' && hideFolded && isFolded) visible = false;

      r.hidden = !visible;
      if (visible) visibleCount++;
    }});

    const emptyRow = document.getElementById('empty-row');
    if (emptyRow) emptyRow.hidden = (visibleCount > 0);
  }}

  document.querySelectorAll('#verdict-tabs .tab').forEach(tab => {{
    tab.addEventListener('click', () => {{
      document.querySelectorAll('#verdict-tabs .tab').forEach(t => t.classList.remove('is-on'));
      tab.classList.add('is-on');
      filter = tab.dataset.filter;
      update();
    }});
  }});

  document.getElementById('hide-folded')?.addEventListener('change', (e) => {{
    hideFolded = e.target.checked;
    update();
  }});

  document.getElementById('btn-fold-all')?.addEventListener('click', () => {{
    const rows = document.querySelectorAll('.verdicts tbody tr[data-kind]');
    const allFolded = rows.length > 0 && Array.from(rows).every(r => folded.has(r.dataset.id));
    if (allFolded) {{
      folded.clear();
    }} else {{
      rows.forEach(r => folded.add(r.dataset.id));
    }}
    update();
  }});

  document.querySelector('.verdicts')?.addEventListener('click', (e) => {{
    const btn = e.target.closest('.btn-fold');
    if (!btn) return;
    const row = btn.closest('tr');
    if (!row) return;
    const id = row.dataset.id;
    if (folded.has(id)) folded.delete(id);
    else folded.add(id);
    update();
  }});

  update();
}})();
</script>
</body>
</html>"""


def write_all(result: ReviewResult, out_dir: Path, *, stem: str = "review") -> dict[str, Path]:
    """네 형식을 모두 쓰고 경로를 돌려준다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": out_dir / f"{stem}.md",
        "sarif": out_dir / f"{stem}.sarif",
        "json": out_dir / f"{stem}.json",
        "html": out_dir / f"{stem}.html",
    }
    paths["markdown"].write_text(to_markdown(result), encoding="utf-8")
    paths["sarif"].write_text(
        json.dumps(to_sarif(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths["json"].write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths["html"].write_text(to_html(result), encoding="utf-8")
    return paths
