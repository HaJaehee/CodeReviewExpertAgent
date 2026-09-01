"""diff 청킹 엔진.

5000줄+ 코드를 리뷰 가능한 단위로 쪼개는 부분이다. BitsAI-CR 의 방식을 따른다.

1. diff 의 **hunk 헤더 단위**로 1차 분할한다.
2. 각 블록을 **함수/클래스 경계까지 확장**한다 (tree-sitter, 없으면 휴리스틱).
3. **확장 상한을 건다** — 원본 hunk 의 4배를 넘으면 3배로 절단한다. 무한 확장은
   컨텍스트 폭주와 정밀도 하락으로 직결된다.
4. 각 라인에 `[added @142]` 형태의 상태 주석을 단다. 모델이 라인 번호를 추론할
   필요가 없어지므로 라인 번호 환각이 구조적으로 사라진다.

tree-sitter 는 **선택 의존성**이다. 폐쇄망에 wheel 반입이 어려우면 휴리스틱
폴백(중괄호 매칭 / 인덴트 추적)으로도 동작한다 — 정확도는 다소 떨어지지만
파이프라인이 멈추지는 않는다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .schema import DiffLine, FileDiff, Hunk, Language, LineStatus, ReviewChunk
from .treesitter import TreeSitterAnalyzer

log = logging.getLogger(__name__)

#: 확장 상한. 원본 hunk 라인 수의 배수.
EXPANSION_LIMIT = 4.0
#: 상한 초과 시 절단할 배수.
EXPANSION_TRUNCATE = 3.0
#: 심볼 경계를 못 찾았을 때 hunk 위아래로 붙일 최소 문맥 라인 수.
FALLBACK_CONTEXT_LINES = 6
#: 청크 하나가 가질 수 있는 절대 최대 라인 수. 거대 함수에 대한 안전판.
ABSOLUTE_MAX_LINES = 150


def calculate_safe_max_lines(max_input_tokens: int) -> int:
    """max_input_tokens 에 맞춰 프롬프트 오버헤드를 제외한 안전한 최대 청크 라인 수를 자동 계산한다."""
    overhead = min(3200, int(max_input_tokens * 0.45))
    code_token_budget = max(max_input_tokens - overhead, 500)
    safe_lines = int(code_token_budget / 30)
    return max(20, min(safe_lines, 250))

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class DiffSourceMismatch(RuntimeError):
    """diff 가 기술하는 내용과 디스크의 파일 내용이 어긋난다.

    보통 diff 생성 이후 작업 트리가 바뀌었을 때 발생한다. 이 상태로 진행하면
    라인 주석이 조용히 틀어지고, 결국 필터의 결정론적 라인 검증까지 무력화된다.
    라인 번호가 틀린 리뷰는 틀린 리뷰이므로 조용히 넘어가지 않는다.
    """


# --------------------------------------------------------------------------
# unified diff 파서
# --------------------------------------------------------------------------


def parse_unified_diff(text: str) -> list[FileDiff]:
    """`git diff -U<n>` 출력을 FileDiff 목록으로 파싱한다."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_ln = new_ln = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current = _start_file(raw)
            if current:
                files.append(current)
            hunk = None
            continue

        if current is None:
            continue

        if raw.startswith("--- "):
            path = raw[4:].strip()
            current.is_new = path == "/dev/null"
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current.is_deleted = path == "/dev/null"
            if not current.is_deleted and path not in ("/dev/null",):
                current.path = _strip_prefix(path)
            continue
        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        if raw.startswith("rename from "):
            current.old_path = _strip_prefix(raw[len("rename from ") :].strip())
            continue

        match = _HUNK_HEADER.match(raw)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_count = int(match.group(4) or 1)
            hunk = Hunk(old_start, old_count, new_start, new_count)
            current.hunks.append(hunk)
            old_ln, new_ln = old_start, new_start
            continue

        if hunk is None:
            continue

        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            hunk.lines.append(DiffLine(LineStatus.ADDED, new_ln, raw[1:]))
            new_ln += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine(LineStatus.DELETED, old_ln, raw[1:]))
            old_ln += 1
        elif raw.startswith(" ") or raw == "":
            hunk.lines.append(DiffLine(LineStatus.UNCHANGED, new_ln, raw[1:] if raw else ""))
            old_ln += 1
            new_ln += 1
        # 그 외(index 줄, mode 줄 등)는 무시한다.

    return [f for f in files if f.path and not f.is_binary]


def _start_file(header: str) -> FileDiff | None:
    # `diff --git a/foo/bar.cpp b/foo/bar.cpp`
    parts = header.split(" ")
    if len(parts) < 4:
        return None
    return FileDiff(path=_strip_prefix(parts[-1]), old_path=_strip_prefix(parts[-2]))


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[2:]
    return path


# --------------------------------------------------------------------------
# 심볼 경계 탐색
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolRange:
    """소스에서 찾아낸 둘러싸는 심볼의 범위 (1-indexed, 양끝 포함)."""

    start: int
    end: int
    name: str | None


class SymbolLocator:
    """tree-sitter 우선, 실패 시 휴리스틱으로 심볼 경계를 찾는다."""

    #: 언어별로 '리뷰 단위가 될 만한' 노드 타입. 앞쪽이 더 선호된다(함수 > 클래스).
    _NODE_TYPES: dict[Language, tuple[str, ...]] = {
        Language.CPP: (
            "function_definition",
            "lambda_expression",
            "class_specifier",
            "struct_specifier",
            "namespace_definition",
        ),
        Language.CSHARP: (
            "method_declaration",
            "constructor_declaration",
            "destructor_declaration",
            "property_declaration",
            "local_function_statement",
            "class_declaration",
            "struct_declaration",
            "record_declaration",
        ),
        Language.PYTHON: ("function_definition", "class_definition"),
    }

    _MODULES: dict[Language, str] = {
        Language.CPP: "tree_sitter_cpp",
        Language.CSHARP: "tree_sitter_c_sharp",
        Language.PYTHON: "tree_sitter_python",
    }

    def __init__(self) -> None:
        self._parsers: dict[Language, object | None] = {}

    def locate(self, source_lines: list[str], language: Language, start: int, end: int) -> SymbolRange | None:
        """[start, end] 를 감싸는 가장 작은 심볼 범위를 돌려준다."""
        parser = self._parser_for(language)
        if parser is not None:
            found = self._locate_treesitter(parser, source_lines, language, start, end)
            if found is not None:
                return found
        return self._locate_heuristic(source_lines, language, start, end)

    # -- tree-sitter -------------------------------------------------------

    def _parser_for(self, language: Language) -> object | None:
        if language in self._parsers:
            return self._parsers[language]

        parser: object | None = None
        module_name = self._MODULES.get(language)
        if module_name:
            try:
                import importlib

                from tree_sitter import Language as TSLanguage
                from tree_sitter import Parser as TSParser

                grammar = importlib.import_module(module_name)
                parser = TSParser(TSLanguage(grammar.language()))
            except Exception as exc:  # noqa: BLE001 - 선택 의존성이므로 조용히 폴백한다
                log.debug("tree-sitter 사용 불가 (%s): %s — 휴리스틱으로 폴백", language.value, exc)
                parser = None

        self._parsers[language] = parser
        return parser

    def _locate_treesitter(
        self, parser: object, source_lines: list[str], language: Language, start: int, end: int
    ) -> SymbolRange | None:
        source = "\n".join(source_lines).encode("utf-8")
        try:
            tree = parser.parse(source)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.debug("파싱 실패, 휴리스틱으로 폴백: %s", exc)
            return None

        wanted = self._NODE_TYPES.get(language, ())
        target_start, target_end = start - 1, end - 1  # tree-sitter 는 0-indexed
        best: tuple[int, object] | None = None

        stack = [tree.root_node]  # type: ignore[attr-defined]
        while stack:
            node = stack.pop()
            node_start, node_end = node.start_point[0], node.end_point[0]
            if node_start > target_start or node_end < target_end:
                # 대상 범위를 감싸지 않는 노드. 자식도 볼 필요 없다.
                if node_end < target_start or node_start > target_end:
                    continue
                stack.extend(node.children)
                continue

            if node.type in wanted:
                span = node_end - node_start
                # 감싸는 것들 중 가장 작은 것 = 가장 구체적인 심볼.
                if best is None or span < best[0]:
                    best = (span, node)
            stack.extend(node.children)

        if best is None:
            return None

        node = best[1]
        return SymbolRange(
            start=node.start_point[0] + 1,
            end=node.end_point[0] + 1,
            name=_node_name(node),
        )

    # -- 휴리스틱 폴백 -----------------------------------------------------

    def _locate_heuristic(
        self, source_lines: list[str], language: Language, start: int, end: int
    ) -> SymbolRange | None:
        if language is Language.PYTHON:
            return _python_block(source_lines, start, end)
        if language in (Language.CPP, Language.CSHARP):
            return _brace_block(source_lines, start, end)
        return None


def _node_name(node: object) -> str | None:
    """tree-sitter 노드에서 식별자 이름을 최선껏 뽑아낸다."""
    try:
        named = node.child_by_field_name("name") or node.child_by_field_name("declarator")  # type: ignore[attr-defined]
        if named is not None:
            text = named.text
            if isinstance(text, bytes):
                return text.decode("utf-8", errors="replace").strip()[:120]
    except Exception:  # noqa: BLE001 - 이름은 표시용이므로 실패해도 무해하다
        pass
    return None


_PY_DEF = re.compile(r"^(\s*)(async\s+def|def|class)\s+(\w+)")


def _python_block(source_lines: list[str], start: int, end: int) -> SymbolRange | None:
    """인덴트를 따라 파이썬 def/class 블록 경계를 찾는다."""
    index = start - 1
    if index >= len(source_lines):
        return None

    # 대상 라인의 인덴트보다 얕은 def/class 를 위로 훑는다.
    base_indent: int | None = None
    for i in range(min(index, len(source_lines) - 1), -1, -1):
        line = source_lines[i]
        if not line.strip():
            continue
        match = _PY_DEF.match(line)
        indent = len(line) - len(line.lstrip())
        if base_indent is None or indent < base_indent:
            if match:
                base_indent = indent
                block_start = i + 1
                name = match.group(3)
                block_end = _python_block_end(source_lines, i, indent)
                if block_end >= end:
                    return SymbolRange(block_start, block_end, name)
                # 이 블록이 대상 범위를 다 못 감싸면 더 바깥 블록을 계속 찾는다.
                base_indent = indent
            elif indent == 0 and line.strip():
                base_indent = 0
    return None


def _python_block_end(source_lines: list[str], def_index: int, indent: int) -> int:
    for i in range(def_index + 1, len(source_lines)):
        line = source_lines[i]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            return i  # 1-indexed 로는 i, 즉 직전 라인까지가 블록
    return len(source_lines)


def _brace_block(source_lines: list[str], start: int, end: int) -> SymbolRange | None:
    """중괄호 깊이를 세어 C++/C# 함수 본문 경계를 찾는다.

    문자열/주석 안의 중괄호를 제거한 뒤 세므로 대부분의 실제 코드에서 동작한다.
    전처리기 조건부(`#if`) 안에서 괄호가 불균형한 코드는 놓칠 수 있다 —
    그럴 땐 None 을 돌려 상위에서 고정 문맥 폴백을 쓰게 한다.
    """
    stripped = [_strip_literals(line) for line in source_lines]

    # 대상 시작 지점의 중괄호 깊이를 구한다.
    depth = 0
    for i in range(min(start - 1, len(stripped))):
        depth += stripped[i].count("{") - stripped[i].count("}")
    if depth <= 0:
        return None

    # 깊이가 1 줄어드는 지점까지 위로 올라가며 여는 괄호를 찾는다.
    open_index = None
    scan_depth = depth
    for i in range(min(start - 1, len(stripped)) - 1, -1, -1):
        scan_depth -= stripped[i].count("{") - stripped[i].count("}")
        if scan_depth < depth:
            open_index = i
            break
    if open_index is None:
        return None

    # 시그니처는 여는 괄호보다 위에 있을 수 있다 (개행된 파라미터 목록).
    sig_index = open_index
    while sig_index > 0 and not stripped[sig_index].strip().rstrip("{").strip():
        sig_index -= 1
    while sig_index > 0:
        prev = stripped[sig_index - 1].strip()
        if not prev or prev.endswith((";", "}", "{")) or prev.startswith(("#", "//")):
            break
        sig_index -= 1

    # 아래로 내려가며 짝이 되는 닫는 괄호를 찾는다.
    close_index = None
    scan_depth = depth
    for i in range(min(start - 1, len(stripped)), len(stripped)):
        scan_depth += stripped[i].count("{") - stripped[i].count("}")
        if scan_depth < depth:
            close_index = i
            break
    if close_index is None or close_index + 1 < end:
        return None

    name = _signature_name(source_lines[sig_index])
    return SymbolRange(sig_index + 1, close_index + 1, name)


_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|//.*$')
_SIG_NAME = re.compile(r"([A-Za-z_~][\w:<>]*)\s*\(")


def _strip_literals(line: str) -> str:
    return _LITERAL.sub("", line)


def _signature_name(line: str) -> str | None:
    match = _SIG_NAME.search(line)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# 청커
# --------------------------------------------------------------------------


class Chunker:
    """FileDiff + 신 파일 내용 → ReviewChunk 목록."""

    def __init__(
        self,
        *,
        expansion_limit: float = EXPANSION_LIMIT,
        expansion_truncate: float = EXPANSION_TRUNCATE,
        absolute_max_lines: int = ABSOLUTE_MAX_LINES,
        on_mismatch: str = "raise",
    ) -> None:
        self.expansion_limit = expansion_limit
        self.expansion_truncate = expansion_truncate
        self.absolute_max_lines = absolute_max_lines
        #: diff 와 소스가 어긋날 때의 동작: "raise" | "warn" | "ignore".
        self.on_mismatch = on_mismatch
        self.locator = SymbolLocator()
        self.treesitter_analyzer = TreeSitterAnalyzer()

    def chunk_file(self, file_diff: FileDiff, new_source: str) -> list[ReviewChunk]:
        if file_diff.is_deleted or file_diff.is_binary or not file_diff.hunks:
            return []

        source_lines = new_source.splitlines()
        if not source_lines:
            return []

        language = file_diff.language
        deleted_before, added, unchanged = _index_hunks(file_diff.hunks)
        self._verify_source(file_diff, source_lines)

        ranges: list[tuple[int, int, str | None, set[int]]] = []
        for hunk in file_diff.hunks:
            hunk_start, hunk_end = _hunk_new_range(hunk, len(source_lines))
            symbol = self.locator.locate(source_lines, language, hunk_start, hunk_end)
            start, end, name = self._apply_expansion_cap(
                hunk_start, hunk_end, symbol, len(source_lines)
            )
            changed = {n for n in added if start <= n <= end}
            if not changed and hunk.added_linenos():
                # 순수 삭제 hunk. 삭제 지점 주변을 리뷰 대상으로 삼는다.
                changed = {hunk_start}
            ranges.append((start, end, name, changed or {hunk_start}))

        merged = _merge_ranges(ranges, self.absolute_max_lines)

        final_ranges: list[tuple[int, int, str | None, set[int]]] = []
        for start, end, name, changed in merged:
            final_ranges.extend(
                _partition_oversized_range(
                    start, end, name, changed, self.absolute_max_lines
                )
            )

        chunks: list[ReviewChunk] = []
        for index, (start, end, name, changed) in enumerate(final_ranges):
            lines = _render_range(source_lines, start, end, deleted_before, added, unchanged)
            ast_ctx = self.treesitter_analyzer.analyze(source_lines, language, start, end, changed)
            ast_summary = ast_ctx.render_for_prompt()
            chunks.append(
                ReviewChunk(
                    chunk_id=f"{file_diff.path}#{index}",
                    path=file_diff.path,
                    language=language,
                    start_line=start,
                    end_line=end,
                    lines=lines,
                    changed_linenos=changed,
                    enclosing_symbol=name or ast_ctx.enclosing_symbol,
                    ast_context=ast_summary,
                )
            )
        return chunks

    def _verify_source(self, file_diff: FileDiff, source_lines: list[str]) -> None:
        """diff 가 주장하는 라인 내용이 실제 파일과 일치하는지 확인한다.

        diff 생성 시점과 파일 읽기 시점 사이에 작업 트리가 바뀌면 모든 라인 번호가
        밀린다. 그 상태로 만든 리뷰는 존재하지 않는 라인을 지적하게 되므로,
        여기서 잡지 못하면 파이프라인 전체가 조용히 거짓말을 하게 된다.
        """
        if self.on_mismatch == "ignore":
            return

        mismatches: list[str] = []
        for hunk in file_diff.hunks:
            for line in hunk.lines:
                if line.status is LineStatus.DELETED:
                    continue  # 삭제 라인은 신 파일에 존재하지 않는다
                index = line.lineno - 1
                actual = source_lines[index] if 0 <= index < len(source_lines) else None
                if actual != line.text:
                    mismatches.append(
                        f"  {file_diff.path}:{line.lineno} "
                        f"diff={line.text!r} != file={actual!r}"
                    )

        if not mismatches:
            return

        detail = "\n".join(mismatches[:10])
        more = f"\n  ... 외 {len(mismatches) - 10}건" if len(mismatches) > 10 else ""
        message = (
            f"diff 와 파일 내용이 {len(mismatches)}곳에서 불일치합니다 ({file_diff.path}).\n"
            f"diff 생성 이후 작업 트리가 변경되었을 가능성이 높습니다. "
            f"diff 를 다시 생성하거나 해당 커밋을 체크아웃하십시오.\n{detail}{more}"
        )
        if self.on_mismatch == "raise":
            raise DiffSourceMismatch(message)
        log.warning("%s", message)

    def _apply_expansion_cap(
        self, hunk_start: int, hunk_end: int, symbol: SymbolRange | None, total_lines: int
    ) -> tuple[int, int, str | None]:
        """확장 상한을 적용한다.

        심볼 경계까지 확장하되, 원본 hunk 의 `expansion_limit` 배를 넘으면
        `expansion_truncate` 배로 잘라낸다. 거대 함수 하나가 컨텍스트를 통째로
        먹어버리는 상황을 막는 장치다.
        """
        hunk_size = max(hunk_end - hunk_start + 1, 1)

        if symbol is None:
            start = max(1, hunk_start - FALLBACK_CONTEXT_LINES)
            end = min(total_lines, hunk_end + FALLBACK_CONTEXT_LINES)
            return start, end, None

        start, end = symbol.start, symbol.end
        expanded_size = end - start + 1

        if expanded_size > hunk_size * self.expansion_limit:
            budget = int(hunk_size * self.expansion_truncate)
            # hunk 를 중심에 두고 대칭으로 잘라낸다.
            margin = max((budget - hunk_size) // 2, FALLBACK_CONTEXT_LINES)
            start = max(symbol.start, hunk_start - margin)
            end = min(symbol.end, hunk_end + margin)

        if end - start + 1 > self.absolute_max_lines:
            margin = max((self.absolute_max_lines - hunk_size) // 2, 0)
            start = max(1, hunk_start - margin)
            end = min(total_lines, hunk_end + margin)

        start = max(1, start)
        end = min(total_lines, end)
        return start, end, symbol.name


def _hunk_new_range(hunk: Hunk, total_lines: int) -> tuple[int, int]:
    """hunk 가 신 파일에서 차지하는 라인 범위. 순수 삭제 hunk 도 안전하게 다룬다."""
    changed = [ln.lineno for ln in hunk.lines if ln.status is LineStatus.ADDED]
    if changed:
        return max(1, min(changed)), min(total_lines, max(changed))
    anchor = min(max(hunk.new_start, 1), max(total_lines, 1))
    return anchor, anchor


def _index_hunks(
    hunks: list[Hunk],
) -> tuple[dict[int, list[DiffLine]], set[int], dict[int, str]]:
    """hunk 들을 렌더링에 쓰기 좋은 인덱스로 변환한다.

    - `deleted_before[n]`: 신 파일 n번 라인 *앞에* 놓일 삭제 라인들
    - `added`: 추가된 신 파일 라인 번호 집합
    - `unchanged[n]`: diff 가 알고 있는 n번 라인 원문 (파일 읽기 실패 시 백업)
    """
    deleted_before: dict[int, list[DiffLine]] = {}
    added: set[int] = set()
    unchanged: dict[int, str] = {}

    for hunk in hunks:
        pending: list[DiffLine] = []
        cursor = hunk.new_start
        for line in hunk.lines:
            if line.status is LineStatus.DELETED:
                pending.append(line)
                continue
            if pending:
                deleted_before.setdefault(cursor, []).extend(pending)
                pending = []
            if line.status is LineStatus.ADDED:
                added.add(line.lineno)
            else:
                unchanged[line.lineno] = line.text
            cursor = line.lineno + 1
        if pending:
            deleted_before.setdefault(cursor, []).extend(pending)

    return deleted_before, added, unchanged


def _merge_ranges(
    ranges: list[tuple[int, int, str | None, set[int]]],
    max_lines: int = ABSOLUTE_MAX_LINES,
) -> list[tuple[int, int, str | None, set[int]]]:
    """겹치거나 맞닿은 범위를 합치되, 합쳤을 때 max_lines 를 초과하면 분리 상태를 유지한다."""
    if not ranges:
        return []

    ordered = sorted(ranges, key=lambda r: (r[0], r[1]))
    merged: list[list] = [list(ordered[0])]

    for start, end, name, changed in ordered[1:]:
        last = merged[-1]
        combined_end = max(last[1], end)
        # 합친 크기가 max_lines 이내일 때만 병합
        if start <= last[1] + 1 and (combined_end - last[0] + 1) <= max_lines:
            last[1] = combined_end
            last[2] = last[2] or name
            last[3] = last[3] | changed
        else:
            merged.append([start, end, name, changed])

    return [(m[0], m[1], m[2], m[3]) for m in merged]


def _partition_oversized_range(
    start: int,
    end: int,
    name: str | None,
    changed: set[int],
    max_lines: int = ABSOLUTE_MAX_LINES,
) -> list[tuple[int, int, str | None, set[int]]]:
    """단일 범위가 max_lines 를 초과할 경우 변경 라인 군집을 기준으로 균등하게 분할한다."""
    if end - start + 1 <= max_lines:
        return [(start, end, name, changed)]

    sorted_changed = sorted(changed)
    if not sorted_changed:
        partitions: list[tuple[int, int, str | None, set[int]]] = []
        cur = start
        while cur <= end:
            cur_end = min(cur + max_lines - 1, end)
            partitions.append((cur, cur_end, name, set()))
            cur = cur_end + 1
        return partitions

    partitions = []
    cluster_changed: set[int] = set()
    cluster_start = max(start, sorted_changed[0] - FALLBACK_CONTEXT_LINES)

    for ln in sorted_changed:
        if not cluster_changed:
            cluster_changed.add(ln)
            cluster_start = max(start, ln - FALLBACK_CONTEXT_LINES)
        elif (ln + FALLBACK_CONTEXT_LINES) - cluster_start + 1 <= max_lines:
            cluster_changed.add(ln)
        else:
            cluster_end = min(end, max(cluster_changed) + FALLBACK_CONTEXT_LINES)
            partitions.append((cluster_start, cluster_end, name, set(cluster_changed)))
            cluster_changed = {ln}
            cluster_start = max(start, ln - FALLBACK_CONTEXT_LINES)

    if cluster_changed:
        cluster_end = min(end, max(cluster_changed) + FALLBACK_CONTEXT_LINES)
        partitions.append((cluster_start, cluster_end, name, set(cluster_changed)))

    return partitions


def _render_range(
    source_lines: list[str],
    start: int,
    end: int,
    deleted_before: dict[int, list[DiffLine]],
    added: set[int],
    unchanged: dict[int, str],
) -> list[DiffLine]:
    """[start, end] 범위를 상태 주석이 달린 DiffLine 목록으로 렌더한다."""
    out: list[DiffLine] = []
    for lineno in range(start, end + 1):
        for deleted in deleted_before.get(lineno, []):
            out.append(deleted)
        if lineno - 1 < len(source_lines):
            text = source_lines[lineno - 1]
        else:
            text = unchanged.get(lineno, "")
        status = LineStatus.ADDED if lineno in added else LineStatus.UNCHANGED
        out.append(DiffLine(status, lineno, text))

    # 범위 끝 직후에 매달린 삭제 라인도 놓치지 않는다.
    for deleted in deleted_before.get(end + 1, []):
        out.append(deleted)

    return out
