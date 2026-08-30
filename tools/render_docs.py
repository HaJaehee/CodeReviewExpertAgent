"""사용 설명서(`docs/user_manual/`)를 HTML 로 렌더한다.

    python tools/render_docs.py

폐쇄망에서 마크다운 뷰어 없이 설명서를 읽을 수 있게 하는 것이 목적이다. 그래서
두 가지를 지킨다.

- **표준 라이브러리만 쓴다.** markdown 패키지 하나를 반입하려면 보안 심사가
  붙고, 그 비용은 릴리스마다 반복된다 (`wiki/design-decisions.md`).
- **결과물이 자기충족적이다.** CSS 는 각 페이지 안에 넣고 폰트·스크립트·CDN 을
  일절 부르지 않는다. 폐쇄망 브라우저에서 외부 자원은 실패가 아니라 **멈춤**이다.

## 마크다운 전부를 지원하지 않는다

이 문서들이 실제로 쓰는 문법만 처리한다 — 제목, 문단, 목록, 표, 코드 펜스,
인용, 수평선, 그리고 인라인의 코드·강조·링크. 범용 파서를 흉내 내면 검증할 수
없는 코드가 늘어난다. `_` 강조는 **일부러 뺐다**: `compile_commands_dir` 처럼
밑줄이 든 식별자가 본문에 그대로 나오는데, 그것을 기울임으로 먹으면 문서가
조용히 망가진다.

## 링크는 검사한다

이 저장소에서 문서 드리프트는 실제로 일어난 실패다. 그래서 렌더링하면서 문서
사이의 링크와 `#앵커` 를 전부 확인하고, 깨진 것이 하나라도 있으면 **종료 코드
1** 로 알린다. 조용히 렌더해 놓으면 깨진 링크가 그대로 배포된다.

앵커 규칙은 GitHub 과 같다 — 소문자로 낮추고, 낱말·공백·하이픈이 아닌 글자를
지우고, 공백을 하이픈으로 바꾼다. 원본 `.md` 를 GitHub 에서 볼 때와 렌더한
HTML 에서 볼 때 같은 링크가 동작해야 하기 때문이다.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 기본 입출력. 저장소 루트 기준이다.
DEFAULT_SRC = Path("docs/user_manual")
DEFAULT_OUT = Path("docs/user_manual_html")

#: 왼쪽 목차의 순서를 정하는 문서. 여기에 적힌 순서가 곧 설명서의 순서다.
INDEX_NAME = "index.md"


# --------------------------------------------------------------------------
# 앵커
# --------------------------------------------------------------------------

_INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_INLINE_MARK = re.compile(r"[`*]")


def plain_text(text: str) -> str:
    """인라인 마크업을 벗겨 낸, 화면에 보이는 글자."""
    return _INLINE_MARK.sub("", _INLINE_LINK.sub(r"\1", text))


def slugify(text: str) -> str:
    """제목 → 앵커. GitHub 규칙 그대로다.

    `\\w` 가 유니코드라 한글이 그대로 남는다. 온점·괄호·em 대시는 지워지고
    공백만 하이픈이 되므로, `A — B` 는 하이픈 **두 개**가 된다. 이 문서들의
    링크가 실제로 그렇게 적혀 있다 (`#workspace--리뷰-대상-저장소`).
    """
    text = plain_text(text).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


# --------------------------------------------------------------------------
# 블록 파서
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^```(\S*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^-{3,}\s*$")
_UL = re.compile(r"^[-*]\s+(.*)$")
_OL = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|\s*$")


@dataclass
class Heading:
    level: int
    text: str
    slug: str


@dataclass
class Document:
    """문서 하나. 렌더 결과와 링크 검사에 필요한 것들."""

    path: Path
    title: str
    body: str
    headings: list[Heading] = field(default_factory=list)
    #: 이 문서가 가리키는 (대상 문서, 앵커) 목록. 검사에만 쓴다.
    links: list[tuple[str, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def slugs(self) -> set[str]:
        return {h.slug for h in self.headings}


def _starts_block(line: str) -> bool:
    """문단이 여기서 끝나야 하는가. 느슨한 이어쓰기를 어디서 끊을지 정한다."""
    return bool(
        not line.strip()
        or _FENCE.match(line)
        or _HEADING.match(line)
        or _HR.match(line)
        or _UL.match(line)
        or _OL.match(line)
        or _QUOTE.match(line)
        or line.startswith("|")
    )


def render_markdown(text: str, doc: Document) -> str:
    """마크다운 한 편을 HTML 본문으로 바꾼다. 제목과 링크는 `doc` 에 모은다."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    seen: dict[str, int] = {}
    i = 0

    while i < len(lines):
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            lang = fence.group(1)
            i += 1
            block: list[str] = []
            while i < len(lines) and not _FENCE.match(lines[i]):
                block.append(lines[i])
                i += 1
            i += 1  # 닫는 펜스
            klass = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{klass}>{html.escape(chr(10).join(block))}</code></pre>")
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            raw = heading.group(2).strip()
            slug = slugify(raw)
            # 같은 제목이 두 번 나오면 GitHub 처럼 뒤에 번호를 붙인다.
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            if count:
                slug = f"{slug}-{count}"
            doc.headings.append(Heading(level, plain_text(raw), slug))
            inner = render_inline(raw, doc)
            out.append(
                f'<h{level} id="{html.escape(slug)}">'
                f'<a class="anchor" href="#{html.escape(slug)}">#</a>{inner}</h{level}>'
            )
            i += 1
            continue

        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue

        if line.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            rows: list[str] = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows, doc))
            continue

        if _QUOTE.match(line):
            inner_lines: list[str] = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                inner_lines.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            nested = render_markdown(chr(10).join(inner_lines), doc)
            out.append(f"<blockquote>{nested}</blockquote>")
            continue

        if _UL.match(line) or _OL.match(line):
            ordered = bool(_OL.match(line))
            pattern = _OL if ordered else _UL
            items: list[str] = []
            while i < len(lines):
                item = pattern.match(lines[i])
                if item:
                    i += 1
                    rest, i = _item_body(lines, i)
                    items.append(f"<li>{_item_html(item.group(1), rest, doc)}</li>")
                    continue
                # 항목 사이의 빈 줄 하나는 목록을 끊지 않는다. 끊으면 `<ol>` 이
                # 둘로 갈라져 번호가 1 부터 다시 시작한다 — 절차 문서에서 치명적이다.
                if not lines[i].strip() and i + 1 < len(lines) and pattern.match(lines[i + 1]):
                    i += 1
                    continue
                break
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        if not line.strip():
            i += 1
            continue

        para = [line.strip()]
        i += 1
        while i < len(lines) and not _starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{render_inline(' '.join(para), doc)}</p>")

    return "\n".join(out)


def _item_body(lines: list[str], i: int) -> tuple[list[str], int]:
    """목록 항목의 첫 줄 다음에 딸린 들여쓴 줄들을 모아 내어쓰기까지 해서 준다.

    빈 줄 뒤에도 들여쓴 줄이 이어지면 여전히 같은 항목이다 — 번호 매긴 절차 안에
    코드 펜스를 넣을 때 그렇게 적는다.
    """
    body: list[str] = []
    while i < len(lines):
        line = lines[i]
        if line.strip():
            if not line.startswith("  "):
                break
            body.append(line)
            i += 1
            continue
        nxt = i + 1
        if nxt < len(lines) and lines[nxt].startswith("  ") and lines[nxt].strip():
            body.append("")
            i += 1
            continue
        break

    indents = [len(l) - len(l.lstrip()) for l in body if l.strip()]
    cut = min(indents) if indents else 0
    return [l[cut:] if l.strip() else "" for l in body], i


def _item_html(head: str, body: list[str], doc: Document) -> str:
    """항목 하나를 HTML 로.

    딸린 줄이 그냥 이어쓰기면 한 문단으로 붙여 목록을 촘촘하게 유지하고, 코드
    펜스처럼 블록이 들어 있으면 그때만 통째로 다시 파싱한다. 늘 재귀로 돌리면
    모든 항목이 `<p>` 로 감싸여 목록이 성기게 벌어진다.
    """
    if body and any(_starts_block(l) and l.strip() for l in body):
        return render_markdown("\n".join([head, *body]), doc)
    return render_inline(" ".join([head, *(l.strip() for l in body if l.strip())]), doc)


def _table(rows: list[str], doc: Document) -> str:
    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    head_html = "".join(f"<th>{render_inline(c, doc)}</th>" for c in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{render_inline(c, doc)}</td>" for c in row) + "</tr>"
        for row in body
    )
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody>"
        "</table></div>"
    )


# --------------------------------------------------------------------------
# 인라인
# --------------------------------------------------------------------------

_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(\S(?:[^*]*\S)?)\*(?!\*)")
_AUTO_URL = re.compile(r"(?<![\"(])\bhttps?://[^\s<>)\]]+")

#: 코드 스팬을 잠시 빼 둘 자리표. 본문에 나올 수 없는 제어문자를 쓴다.
_SLOT = "\x00{}\x00"


def render_inline(text: str, doc: Document) -> str:
    """인라인 문법을 HTML 로. 코드 스팬을 먼저 빼내는 것이 요점이다.

    빼내지 않으면 `` `-*,bugprone-*` `` 같은 값의 별표가 기울임으로 먹힌다.
    코드 안의 글자는 마크업이 아니라 값이다.
    """
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(1))
        return _SLOT.format(len(spans) - 1)

    text = _CODE_SPAN.sub(stash, text)
    text = html.escape(text)
    text = _LINK.sub(lambda m: _link(m, doc), text)
    text = _AUTO_URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    for index, code in enumerate(spans):
        text = text.replace(_SLOT.format(index), f"<code>{html.escape(code)}</code>")
    return text


def _link(match: re.Match, doc: Document) -> str:
    label, target = match.group(1), match.group(2)
    if "://" not in target:
        base, _, anchor = target.partition("#")
        doc.links.append((base, anchor))
        # 설명서 안의 문서만 .html 로 바꾼다. `../../AGENTS.md` 처럼 밖을
        # 가리키는 것은 렌더 대상이 아니므로 원본 경로 그대로 둔다.
        if _is_sibling_doc(base):
            target = base[: -len(".md")] + ".html" + (f"#{anchor}" if anchor else "")
    return f'<a href="{target}">{label}</a>'


def _is_sibling_doc(base: str) -> bool:
    """같은 폴더의 설명서인가. 경로 구분자가 있으면 설명서 바깥이다."""
    return base.endswith(".md") and "/" not in base


# --------------------------------------------------------------------------
# 페이지 조립
# --------------------------------------------------------------------------

STYLE = """
:root {
  --bg: #ffffff; --fg: #1f2430; --fg-dim: #55607a; --line: #e2e6ef;
  --sunken: #f5f7fa; --accent: #1d6fd0; --code: #0f4c81;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #10141c; --fg: #dbe2f0; --fg-dim: #98a3ba; --line: #263041;
    --sunken: #171d28; --accent: #63b3f5; --code: #8fd0ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "Segoe UI", "Malgun Gothic", "맑은 고딕", system-ui, sans-serif;
  font-size: 16px; line-height: 1.75;
}
code, pre { font-family: ui-monospace, "Cascadia Mono", "D2Coding", Consolas, monospace; }
.wrap { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 40px;
        max-width: 1180px; margin: 0 auto; padding: 32px 24px 96px; align-items: start; }
nav { position: sticky; top: 24px; font-size: 14px; }
nav h1 { font-size: 15px; margin: 0 0 12px; letter-spacing: -0.2px; }
nav ol, nav ul { list-style: none; margin: 0; padding: 0; }
nav li { margin: 2px 0; }
nav a { display: block; padding: 4px 8px; border-radius: 5px;
        color: var(--fg-dim); text-decoration: none; }
nav a:hover { background: var(--sunken); color: var(--fg); }
nav a[aria-current="page"] { background: var(--sunken); color: var(--fg); font-weight: 600; }
nav .toc { margin: 6px 0 10px 10px; padding-left: 10px; border-left: 1px solid var(--line); }
nav .toc a { font-size: 13px; padding: 2px 6px; }
main { min-width: 0; }
h1, h2, h3, h4 { line-height: 1.35; margin: 1.9em 0 0.6em; letter-spacing: -0.3px; }
h1 { font-size: 1.9em; margin-top: 0; }
h2 { font-size: 1.4em; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
h3 { font-size: 1.15em; }
h4 { font-size: 1em; color: var(--fg-dim); }
a { color: var(--accent); }
a.anchor { float: left; margin-left: -1.1em; padding-right: 0.35em; color: var(--line);
           text-decoration: none; font-weight: 400; }
a.anchor:hover { color: var(--accent); }
p, li { overflow-wrap: anywhere; }
code { background: var(--sunken); border: 1px solid var(--line); border-radius: 4px;
       padding: 0.1em 0.35em; font-size: 0.88em; color: var(--code); }
pre { background: var(--sunken); border: 1px solid var(--line); border-radius: 8px;
      padding: 14px 16px; overflow-x: auto; line-height: 1.6; }
pre code { background: none; border: 0; padding: 0; color: inherit; font-size: 0.86em; }
blockquote { margin: 1em 0; padding: 2px 16px; border-left: 3px solid var(--line);
             color: var(--fg-dim); }
blockquote p { margin: 0.6em 0; }
hr { border: 0; border-top: 1px solid var(--line); margin: 2.4em 0; }
.table-wrap { overflow-x: auto; margin: 1em 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.94em; }
th, td { border: 1px solid var(--line); padding: 7px 11px; text-align: left; vertical-align: top; }
th { background: var(--sunken); }
footer { margin-top: 64px; padding-top: 16px; border-top: 1px solid var(--line);
         color: var(--fg-dim); font-size: 13px; }
@media (max-width: 860px) {
  .wrap { grid-template-columns: minmax(0, 1fr); gap: 20px; padding: 20px 16px 64px; }
  nav { position: static; border-bottom: 1px solid var(--line); padding-bottom: 12px; }
  a.anchor { display: none; }
}
"""

PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
<nav>
<h1>CREX 사용 설명서</h1>
<ol>
{nav}
</ol>
</nav>
<main>
{body}
<footer>{footer}</footer>
</main>
</div>
</body>
</html>
"""


def _nav(docs: list[Document], current: Document) -> str:
    """왼쪽 목차. 지금 보는 문서 아래에만 그 문서의 절 목록을 편다."""
    items = []
    for doc in docs:
        href = doc.path.stem + ".html"
        mark = ' aria-current="page"' if doc is current else ""
        items.append(f'<li><a href="{href}"{mark}>{html.escape(doc.title)}</a>')
        if doc is current:
            subs = [
                f'<li><a href="#{html.escape(h.slug)}">{html.escape(h.text)}</a></li>'
                for h in doc.headings
                if h.level == 2
            ]
            if subs:
                items.append(f'<ul class="toc">{"".join(subs)}</ul>')
        items.append("</li>")
    return "\n".join(items)


def build(src: Path, out: Path) -> tuple[list[Document], list[str]]:
    """전부 렌더하고 (문서 목록, 문제 목록)을 준다. 쓰기는 하지 않는다."""
    paths = sorted(src.glob("*.md"))
    if not paths:
        raise SystemExit(f"{src} 에 마크다운 문서가 없습니다.")

    docs: list[Document] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        doc = Document(path=path, title=_title(text, path), body="")
        doc.body = render_markdown(text, doc)
        docs.append(doc)

    return _ordered(docs, src), check_links(docs)


def _title(text: str, path: Path) -> str:
    for line in text.split("\n"):
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) == 1:
            return plain_text(heading.group(2)).strip()
    return path.stem


def _ordered(docs: list[Document], src: Path) -> list[Document]:
    """`index.md` 가 링크한 순서를 따른다 — 설명서의 순서는 설명서가 정한다."""
    by_name = {doc.name: doc for doc in docs}
    index = by_name.get(INDEX_NAME)
    if index is None:
        return docs

    order = [index]
    for base, _ in index.links:
        doc = by_name.get(base)
        if doc is not None and doc not in order:
            order.append(doc)
    order.extend(doc for doc in docs if doc not in order)
    return order


def check_links(docs: list[Document]) -> list[str]:
    """문서 사이의 링크와 앵커를 확인한다. 드리프트는 여기서 잡는다."""
    by_name = {doc.name: doc for doc in docs}
    problems: list[str] = []

    for doc in docs:
        for base, anchor in doc.links:
            if not base:  # 같은 문서 안의 #앵커
                if anchor and anchor not in doc.slugs:
                    problems.append(f"{doc.name}: 앵커 #{anchor} 가 이 문서에 없습니다")
                continue
            if not _is_sibling_doc(base):
                # 문서 바깥 파일(`../../AGENTS.md` 등). 실제로 있는지만 본다.
                if not (doc.path.parent / base).resolve().exists():
                    problems.append(f"{doc.name}: {base} 파일이 없습니다")
                continue
            if base not in by_name:
                problems.append(f"{doc.name}: {base} 문서가 없습니다")
                continue
            if anchor and anchor not in by_name[base].slugs:
                problems.append(f"{doc.name}: {base}#{anchor} 앵커가 없습니다")

    linked = {base for doc in docs if doc.name == INDEX_NAME for base, _ in doc.links}
    if INDEX_NAME in by_name:
        for doc in docs:
            if doc.name != INDEX_NAME and doc.name not in linked:
                problems.append(f"{INDEX_NAME}: {doc.name} 로 가는 링크가 없습니다")
    return problems


def write(docs: list[Document], out: Path, src: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        page = PAGE.format(
            title=html.escape(f"{doc.title} — CREX"),
            style=STYLE,
            nav=_nav(docs, doc),
            body=doc.body,
            footer=f"{html.escape(src.as_posix())}/{html.escape(doc.name)} 에서 생성했습니다.",
        )
        (out / (doc.path.stem + ".html")).write_text(page, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render_docs",
        description="사용 설명서 마크다운을 HTML 로 렌더합니다 (외부 패키지 불필요).",
    )
    parser.add_argument(
        "--src", type=Path, default=DEFAULT_SRC, help=f"마크다운 위치 (기본 {DEFAULT_SRC})"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"HTML 출력 위치 (기본 {DEFAULT_OUT})"
    )
    parser.add_argument(
        "--check", action="store_true", help="링크만 확인하고 파일은 쓰지 않습니다"
    )
    args = parser.parse_args(argv)

    docs, problems = build(args.src, args.out)

    # 링크가 깨져도 파일은 쓴다 — 무엇이 어떻게 나왔는지 열어 봐야 고칠 수 있다.
    # 다만 성공했다고 말하지는 않는다. 포장 스크립트가 이 종료 코드로 멈춘다.
    if not args.check:
        write(docs, args.out, args.src)

    if problems:
        print(f"링크 문제 {len(problems)}건:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("  고치기 전에는 배포하지 마십시오.", file=sys.stderr)
        return 1

    if args.check:
        print(f"문서 {len(docs)}개, 링크 이상 없습니다.")
    else:
        print(f"문서 {len(docs)}개를 {args.out.as_posix()} 에 썼습니다.")
        print(f"  시작 지점: {(args.out / 'index.html').as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
