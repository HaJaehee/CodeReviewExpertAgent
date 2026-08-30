"""설명서 렌더러 검증 (`tools/render_docs.py`).

두 가지를 지킨다.

1. **앵커가 GitHub 과 같아야 한다.** 같은 문서를 GitHub 에서도 보고 렌더한
   HTML 에서도 보는데, 슬러그 규칙이 어긋나면 `#앵커` 링크가 한쪽에서만
   동작한다. 조용히 깨지는 종류라 실제 문서로 확인한다.
2. **결과물이 자기충족적이어야 한다.** 폐쇄망 브라우저에서 외부 자원은 실패가
   아니라 멈춤이다. CDN·폰트·스크립트를 부르면 안 된다.

마지막 검사는 문서 드리프트 방지다 — 실제 `docs/user_manual/` 을 렌더해서 깨진
링크가 하나도 없어야 통과한다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import render_docs  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _render(text: str) -> str:
    doc = render_docs.Document(path=Path("t.md"), title="t", body="")
    doc.body = render_docs.render_markdown(text, doc)
    return doc.body


def _doc(text: str) -> render_docs.Document:
    doc = render_docs.Document(path=Path("t.md"), title="t", body="")
    doc.body = render_docs.render_markdown(text, doc)
    return doc


# --------------------------------------------------------------------------
# 앵커
# --------------------------------------------------------------------------


def test_slug_matches_github_for_the_links_these_docs_actually_use() -> None:
    """실제 문서에 적혀 있는 링크들이다. 규칙이 어긋나면 여기서 걸린다."""
    cases = [
        ("`workspace` — 리뷰 대상 저장소", "workspace--리뷰-대상-저장소"),
        ("`compile_commands.json` 이 없으면 반쯤 눈을 감습니다",
         "compile_commandsjson-이-없으면-반쯤-눈을-감습니다"),
        ("CREX 는 어디에 두나", "crex-는-어디에-두나"),
        ("Zed 연동 (MCP)", "zed-연동-mcp"),
        ("Streamable HTTP 엔드포인트", "streamable-http-엔드포인트"),
        ("지적이 항상 0건입니다", "지적이-항상-0건입니다"),
        ("입력 토큰 상한을 왜 8192 로 두나", "입력-토큰-상한을-왜-8192-로-두나"),
    ]
    for heading, expected in cases:
        got = render_docs.slugify(heading)
        _check(got == expected, f"{heading!r} → {got!r}, 기대 {expected!r}")


def test_duplicate_headings_get_numbered() -> None:
    doc = _doc("## 같은 제목\n\n## 같은 제목\n")
    slugs = [h.slug for h in doc.headings]
    _check(slugs == ["같은-제목", "같은-제목-1"], f"{slugs}")


# --------------------------------------------------------------------------
# 인라인 — 코드 스팬이 마크업보다 먼저다
# --------------------------------------------------------------------------


def test_code_span_protects_asterisks_and_underscores() -> None:
    """`-*,bugprone-*` 의 별표가 기울임으로 먹히면 설정값이 조용히 망가진다."""
    body = _render("체크는 `-*,bugprone-*,cert-*` 입니다.")
    _check("<em>" not in body, f"코드 안의 별표를 기울임으로 먹었다: {body}")
    _check("-*,bugprone-*,cert-*" in body, body)


def test_underscore_is_never_emphasis() -> None:
    """`compile_commands_dir` 처럼 밑줄 든 식별자가 본문에 그대로 나온다."""
    body = _render("값은 compile_commands_dir 과 dotnet_project 입니다.")
    _check("<em>" not in body, f"밑줄을 강조로 먹었다: {body}")
    _check("compile_commands_dir" in body, body)


def test_bold_and_inline_code_render() -> None:
    body = _render("**중요**한 `값` 입니다.")
    _check("<strong>중요</strong>" in body, body)
    _check("<code>값</code>" in body, body)


def test_html_in_text_is_escaped() -> None:
    body = _render("`<워크스페이스>/reports` 와 a < b 를 씁니다.")
    _check("&lt;워크스페이스&gt;" in body, body)
    _check("a &lt; b" in body, body)


# --------------------------------------------------------------------------
# 링크
# --------------------------------------------------------------------------


def test_sibling_md_links_become_html() -> None:
    body = _render("[설정](configuration.md#workspace--리뷰-대상-저장소)을 보세요.")
    _check('href="configuration.html#workspace--리뷰-대상-저장소"' in body, body)


def test_links_outside_the_manual_are_left_alone() -> None:
    """`../../AGENTS.md` 는 렌더 대상이 아니다. `.html` 로 바꾸면 없는 파일을 가리킨다."""
    body = _render("[에이전트 지시](../../AGENTS.md)를 복사하세요.")
    _check('href="../../AGENTS.md"' in body, body)


def test_external_url_is_untouched() -> None:
    body = _render("[semgrep](https://semgrep.dev/) 를 씁니다.")
    _check('href="https://semgrep.dev/"' in body, body)


def test_broken_anchor_is_reported() -> None:
    index = render_docs.Document(path=Path("index.md"), title="i", body="")
    index.body = render_docs.render_markdown(
        "# i\n\n[가기](other.md#없는앵커)\n", index
    )
    other = render_docs.Document(path=Path("other.md"), title="o", body="")
    other.body = render_docs.render_markdown("# o\n\n## 있는 제목\n", other)

    problems = render_docs.check_links([index, other])
    _check(
        any("없는앵커" in p for p in problems), f"깨진 앵커를 그냥 지나쳤다: {problems}"
    )


# --------------------------------------------------------------------------
# 블록
# --------------------------------------------------------------------------


def test_table_renders_with_a_scroll_wrapper() -> None:
    body = _render("| 이름 | 뜻 |\n|---|---|\n| `a` | 값 |\n")
    _check('<div class="table-wrap">' in body, body)
    _check("<th>이름</th>" in body, body)
    _check("<td><code>a</code></td>" in body, body)


def test_fenced_code_keeps_its_language_and_escapes() -> None:
    body = _render("```json\n{\"a\": \"<b>\"}\n```\n")
    _check('<code class="lang-json">' in body, body)
    _check("&lt;b&gt;" in body, body)


def test_list_item_continuation_stays_one_item() -> None:
    body = _render("- 첫 줄이\n  이어집니다\n- 둘째\n")
    _check(body.count("<li>") == 2, body)
    _check("첫 줄이 이어집니다" in body, body)


def test_code_fence_inside_a_numbered_step_keeps_the_numbering() -> None:
    """목록이 둘로 갈라지면 3번이 1번으로 다시 시작한다. 절차 문서에서 치명적이다."""
    body = _render(
        "1. 내려받습니다.\n"
        "2. 해시를 뜹니다.\n"
        "\n"
        "   ```powershell\n"
        "   Get-FileHash a.exe\n"
        "   ```\n"
        "\n"
        "3. 대조합니다.\n"
    )
    _check(body.count("<ol>") == 1, f"목록이 갈라졌다: {body}")
    _check(body.count("<li>") == 3, body)
    _check("<pre><code" in body, body)
    _check("Get-FileHash a.exe" in body, body)


def test_blockquote_renders_its_contents() -> None:
    body = _render("> **주의** 입니다.\n> 두 번째 줄.\n")
    _check("<blockquote>" in body, body)
    _check("<strong>주의</strong>" in body, body)


# --------------------------------------------------------------------------
# 실제 설명서
# --------------------------------------------------------------------------


def test_real_manual_renders_without_broken_links() -> None:
    """문서 드리프트 방지. 문서를 옮기거나 제목을 고치면 여기서 걸린다."""
    src = ROOT / render_docs.DEFAULT_SRC
    if not src.is_dir():
        print("     (설명서 폴더 없음 — 건너뜀)")
        return

    docs, problems = render_docs.build(src, ROOT / render_docs.DEFAULT_OUT)
    _check(not problems, "링크 문제:\n  " + "\n  ".join(problems))
    _check(len(docs) >= 10, f"문서가 {len(docs)}개뿐이다")
    _check(docs[0].name == render_docs.INDEX_NAME, f"첫 문서: {docs[0].name}")


def test_rendered_page_loads_nothing_from_outside() -> None:
    """폐쇄망 브라우저에서 외부 자원은 실패가 아니라 멈춤이다."""
    src = ROOT / render_docs.DEFAULT_SRC
    if not src.is_dir():
        print("     (설명서 폴더 없음 — 건너뜀)")
        return

    docs, _ = render_docs.build(src, ROOT / render_docs.DEFAULT_OUT)
    page = render_docs.PAGE.format(
        title="t", style=render_docs.STYLE, nav="", body=docs[0].body, footer=""
    )
    for tag in ("<script", "<link ", "@import", "url(http"):
        _check(tag not in page, f"외부 자원을 부른다: {tag}")
    # 본문의 http 링크는 사용자가 누르는 것이라 괜찮다. 자동으로 받아오는
    # 자원(src=)만 없으면 된다.
    _check(not re.search(r'\ssrc="https?:', page), "외부 자원을 src 로 부른다")


TESTS = [
    test_slug_matches_github_for_the_links_these_docs_actually_use,
    test_duplicate_headings_get_numbered,
    test_code_span_protects_asterisks_and_underscores,
    test_underscore_is_never_emphasis,
    test_bold_and_inline_code_render,
    test_html_in_text_is_escaped,
    test_sibling_md_links_become_html,
    test_links_outside_the_manual_are_left_alone,
    test_external_url_is_untouched,
    test_broken_anchor_is_reported,
    test_table_renders_with_a_scroll_wrapper,
    test_fenced_code_keeps_its_language_and_escapes,
    test_list_item_continuation_stays_one_item,
    test_code_fence_inside_a_numbered_step_keeps_the_numbering,
    test_blockquote_renders_its_contents,
    test_real_manual_renders_without_broken_links,
    test_rendered_page_loads_nothing_from_outside,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
