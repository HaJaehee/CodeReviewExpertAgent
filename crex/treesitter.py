"""Tree-sitter AST 구문 분석 도구 — 구문 구조 및 변경 노드 분석.

소형 LLM(25~40B)이 텍스트만으로 추론하기 어려운 코드 구조(함수 시그니처, 호출되는 API,
포인터/자원 연산, 제어 흐름, 변수 선언/대입)를 AST 수준에서 명확히 추출해 프롬프트에 주입한다.

tree-sitter 라이브러리가 없거나 해당 언어 문법이 없으면 안전하게 폴백한다.
코어의 무의존성 원칙을 깨지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .schema import Language

log = logging.getLogger(__name__)

_MODULES: dict[Language, str] = {
    Language.CPP: "tree_sitter_cpp",
    Language.CSHARP: "tree_sitter_c_sharp",
    Language.PYTHON: "tree_sitter_python",
}

# 심볼 후보 노드 타입
_SYMBOL_NODE_TYPES: dict[Language, tuple[str, ...]] = {
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

# 관심 대상 구문 노드 타입 (호출, 할당, 분기, 포인터/자원 관리 등)
_INTERESTING_NODE_TYPES = {
    # C++
    "call_expression",
    "declaration",
    "assignment_expression",
    "pointer_expression",
    "delete_expression",
    "new_expression",
    "if_statement",
    "for_range_loop",
    "while_statement",
    "return_statement",
    "throw_statement",
    "try_statement",
    # C#
    "invocation_expression",
    "local_declaration_statement",
    "using_statement",
    "lock_statement",
    # Python
    "with_statement",
    "raise_statement",
    "assert_statement",
}


@dataclass
class SymbolContract:
    """파일 내 함수/메서드 또는 클래스 심볼의 시그니처 및 핵심 계약 요약."""

    name: str
    bare_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    returns: list[str] = field(default_factory=list)
    throws: list[str] = field(default_factory=list)
    resource_ops: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    member_fields: list[str] = field(default_factory=list)
    definition_snippet: str = ""
    path: str = ""

    def render_contract(self) -> str:
        """피호출/연관 심볼의 1줄 계약 요약."""
        loc = f"{self.path}:L{self.start_line}-L{self.end_line}" if self.path else f"L{self.start_line}-L{self.end_line}"
        parts = [f"`{self.signature}` ({loc})"]
        details: list[str] = []
        if self.returns:
            ret_str = ", ".join(f"`{r}`" for r in self.returns[:4])
            details.append(f"반환: {ret_str}")
        if self.throws:
            thr_str = ", ".join(f"`{t}`" for t in self.throws[:3])
            details.append(f"예외: {thr_str}")
        if self.resource_ops:
            res_str = ", ".join(f"`{op}`" for op in self.resource_ops[:3])
            details.append(f"자원/락: {res_str}")
        if details:
            parts.append(" → " + " | ".join(details))
        return "".join(parts)


@dataclass
class TreeSitterContext:
    """한 청크에 대한 Tree-sitter AST 분석 결과."""

    enclosing_symbol: str | None = None
    symbol_kind: str | None = None
    changed_line_nodes: dict[int, list[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    control_flow: list[str] = field(default_factory=list)
    member_fields: list[str] = field(default_factory=list)
    callee_contracts: list[str] = field(default_factory=list)
    change_map: list[str] = field(default_factory=list)
    symbol_definitions: dict[str, str] = field(default_factory=dict)
    has_error: bool = False

    def render_for_prompt(self, max_lines: int = 8, compact: bool = False) -> str:
        """프롬프트에 주입할 마크다운 형태의 한국어 요약을 컴팩트하게 만든다."""
        lines: list[str] = []

        if self.enclosing_symbol:
            kind_str = f" ({self.symbol_kind})" if self.symbol_kind else ""
            lines.append(f"- 둘러싼 심볼: `{self.enclosing_symbol}`{kind_str}")

        if self.member_fields:
            limit = 4 if compact else 8
            fields_str = ", ".join(f"`{f}`" for f in self.member_fields[:limit])
            lines.append(f"- 소속 클래스/구조체 멤버 필드: {fields_str}")

        if self.changed_line_nodes:
            lines.append("- 변경 라인 AST 구문 요소:")
            sorted_linenos = sorted(self.changed_line_nodes.keys())
            line_cap = 4 if compact else max_lines
            for lineno in sorted_linenos[:line_cap]:
                items = self.changed_line_nodes[lineno]
                summary = ", ".join(items[:3])
                lines.append(f"  - L{lineno}: {summary}")
            if len(sorted_linenos) > line_cap:
                lines.append(f"  - ... (외 {len(sorted_linenos) - line_cap}개 변경 라인 생략)")

        if self.calls:
            unique_calls = list(dict.fromkeys(self.calls))[:8]
            lines.append(f"- 호출된 주요 함수/메서드: {', '.join(f'`{c}`' for c in unique_calls)}")

        if self.callee_contracts:
            contract_cap = 3 if compact else 6
            lines.append("- 연관/피호출 함수 시그니처 및 계약:")
            for contract in self.callee_contracts[:contract_cap]:
                lines.append(f"  - {contract}")

        if self.control_flow:
            unique_cf = list(dict.fromkeys(self.control_flow))[:4]
            lines.append(f"- 제어 흐름/조건: {', '.join(unique_cf)}")

        if self.change_map and not compact:
            lines.append("- 전체 변경 요약 지도(Change Map):")
            for item in self.change_map[:8]:
                lines.append(f"  - {item}")

        lines.append(f"- 구문 오류(has_error): {'감지됨 (불완전하거나 오류가 있는 구문)' if self.has_error else '없음'}")

        return "\n".join(lines) if lines else "(Tree-sitter 구문 분석 정보 없음)"


class TreeSitterAnalyzer:
    """Tree-sitter 파서를 관리하고 청크 단위로 AST 를 분석한다."""

    def __init__(self) -> None:
        self._parsers: dict[Language, Any] = {}
        self._available: dict[Language, bool] = {}

    def available(self, language: Language) -> bool:
        """해당 언어의 tree-sitter 파서를 쓸 수 있는지."""
        if language in self._available:
            return self._available[language]
        parser = self._get_parser(language)
        self._available[language] = parser is not None
        return self._available[language]

    def _get_parser(self, language: Language) -> Any:
        if language in self._parsers:
            return self._parsers[language]

        module_name = _MODULES.get(language)
        if not module_name:
            self._parsers[language] = None
            return None

        try:
            import importlib
            from tree_sitter import Language as TSLanguage, Parser as TSParser

            grammar = importlib.import_module(module_name)
            parser = TSParser(TSLanguage(grammar.language()))
            self._parsers[language] = parser
            return parser
        except Exception as exc:  # noqa: BLE001 - 선택 의존성이므로 실패 시 None
            log.debug("tree-sitter 파서 로드 실패 (%s): %s", language.value, exc)
            self._parsers[language] = None
            return None

    def extract_file_symbols(
        self,
        source_lines: list[str],
        language: Language,
        path: str = "",
    ) -> dict[str, SymbolContract]:
        """파일 전체에서 함수/메서드 및 클래스 심볼의 시그니처와 계약 정보를 추출한다."""
        if not source_lines:
            return {}

        parser = self._get_parser(language)
        if parser is not None:
            try:
                source_bytes = "\n".join(source_lines).encode("utf-8")
                tree = parser.parse(source_bytes)
                symbols = self._extract_symbols_treesitter(
                    tree.root_node, source_lines, source_bytes, language, path
                )
                if symbols:
                    return symbols
            except Exception as exc:  # noqa: BLE001
                log.debug("tree-sitter 심볼 추출 실패: %s", exc)

        return _extract_symbols_heuristic(source_lines, language, path)

    def analyze(
        self,
        source_lines: list[str],
        language: Language,
        start_line: int,
        end_line: int,
        changed_lines: set[int],
        *,
        path: str = "",
        file_symbols: dict[str, SymbolContract] | None = None,
    ) -> TreeSitterContext:
        """주어진 소스 라인과 대상 범위, 변경 라인들을 분석하여 TreeSitterContext 를 생성한다."""
        if not source_lines:
            return TreeSitterContext()

        if file_symbols is None:
            file_symbols = self.extract_file_symbols(source_lines, language, path=path)

        parser = self._get_parser(language)
        if parser is None:
            return self._analyze_heuristic(
                source_lines, language, start_line, end_line, changed_lines, file_symbols
            )

        source_bytes = "\n".join(source_lines).encode("utf-8")
        try:
            tree = parser.parse(source_bytes)
        except Exception as exc:  # noqa: BLE001
            log.debug("tree-sitter 파싱 실패: %s", exc)
            return self._analyze_heuristic(
                source_lines, language, start_line, end_line, changed_lines, file_symbols
            )

        context = TreeSitterContext(has_error=bool(tree.root_node.has_error))

        # 변경 라인이 있으면 변경 라인을 감싸는 가장 구체적인 심볼을 찾고,
        # 없으면 청크 전체 범위를 기준으로 찾는다.
        if changed_lines:
            target_start = min(changed_lines) - 1
            target_end = max(changed_lines) - 1
        else:
            target_start = start_line - 1
            target_end = end_line - 1

        # 1. 둘러싼 심볼 탐색
        symbol_node, symbol_name = self._find_enclosing_symbol(
            tree.root_node, language, target_start, target_end, source_bytes
        )
        if symbol_name:
            context.enclosing_symbol = symbol_name
            context.symbol_kind = _human_kind(symbol_node.type if symbol_node else "")

        # 2. 둘러싼 클래스/구조체 멤버 필드 수집
        enclosing_class_fields = self._find_enclosing_class_fields(
            tree.root_node, language, target_start, target_end, source_bytes, file_symbols, symbol_name
        )
        if enclosing_class_fields:
            context.member_fields = enclosing_class_fields

        # 3. 변경된 라인 대상 AST 노드 탐색
        scan_lines = changed_lines if changed_lines else set(range(start_line, end_line + 1))
        self._collect_line_nodes(tree.root_node, scan_lines, source_bytes, context)

        # 4. 같은 파일 내 피호출 함수 계약 및 정의 스니펫 연결
        _attach_local_callees(context, file_symbols, start_line, end_line)

        return context

    def _analyze_heuristic(
        self,
        source_lines: list[str],
        language: Language,
        start_line: int,
        end_line: int,
        changed_lines: set[int],
        file_symbols: dict[str, SymbolContract],
    ) -> TreeSitterContext:
        context = TreeSitterContext()
        target_ln = min(changed_lines) if changed_lines else start_line
        for sym in file_symbols.values():
            if sym.start_line <= target_ln <= sym.end_line:
                if sym.kind == "함수/메서드" and context.enclosing_symbol is None:
                    context.enclosing_symbol = sym.name
                    context.symbol_kind = sym.kind
                elif sym.member_fields and not context.member_fields:
                    context.member_fields = list(sym.member_fields)

        scan_lines = changed_lines if changed_lines else set(range(start_line, end_line + 1))
        for ln in sorted(scan_lines):
            if 1 <= ln <= len(source_lines):
                text = source_lines[ln - 1]
                for call_name in _extract_calls_from_line(text):
                    if call_name not in context.calls:
                        context.calls.append(call_name)

        _attach_local_callees(context, file_symbols, start_line, end_line)
        return context

    def _find_enclosing_symbol(
        self,
        root_node: Any,
        language: Language,
        target_start: int,
        target_end: int,
        source_bytes: bytes,
    ) -> tuple[Any, str | None]:
        wanted = _SYMBOL_NODE_TYPES.get(language, ())
        best: tuple[int, Any] | None = None

        stack = [root_node]
        while stack:
            node = stack.pop()
            node_start, node_end = node.start_point[0], node.end_point[0]
            if node_start > target_start or node_end < target_end:
                if node_end < target_start or node_start > target_end:
                    continue
                stack.extend(node.children)
                continue

            if node.type in wanted:
                span = node_end - node_start
                if best is None or span < best[0]:
                    best = (span, node)
            stack.extend(node.children)

        if best is None:
            return None, None

        node = best[1]
        name = _extract_node_name(node, source_bytes)
        return node, name

    def _find_enclosing_class_fields(
        self,
        root_node: Any,
        language: Language,
        target_start: int,
        target_end: int,
        source_bytes: bytes,
        file_symbols: dict[str, SymbolContract],
        enclosing_symbol: str | None,
    ) -> list[str]:
        """청크가 속한 클래스/구조체의 멤버 필드 선언을 추출한다."""
        class_types = (
            "class_specifier",
            "struct_specifier",
            "class_declaration",
            "struct_declaration",
            "record_declaration",
            "class_definition",
        )
        fields: list[str] = []
        stack = [root_node]
        while stack:
            node = stack.pop()
            node_start, node_end = node.start_point[0], node.end_point[0]
            if node_end < target_start or node_start > target_end:
                continue
            if node.type in class_types and node_start <= target_start and node_end >= target_end:
                fields.extend(_extract_class_fields_from_node(node, source_bytes, language))
            stack.extend(node.children)

        if not fields and enclosing_symbol and "::" in enclosing_symbol:
            cls_name = enclosing_symbol.split("::", 1)[0].strip()
            sym = file_symbols.get(cls_name)
            if sym and sym.member_fields:
                fields.extend(sym.member_fields)

        if not fields:
            # 파일 내 유일한 클래스가 있으면 그 멤버 필드를 참고용으로 제공
            class_syms = [s for s in file_symbols.values() if s.member_fields]
            if len(class_syms) == 1:
                fields.extend(class_syms[0].member_fields)

        return list(dict.fromkeys(fields))[:10]

    def _extract_symbols_treesitter(
        self,
        root_node: Any,
        source_lines: list[str],
        source_bytes: bytes,
        language: Language,
        path: str,
    ) -> dict[str, SymbolContract]:
        wanted = _SYMBOL_NODE_TYPES.get(language, ())
        fn_types = {
            "function_definition",
            "method_declaration",
            "constructor_declaration",
            "destructor_declaration",
            "local_function_statement",
        }
        class_types = {
            "class_specifier",
            "struct_specifier",
            "class_declaration",
            "struct_declaration",
            "record_declaration",
            "class_definition",
        }

        symbols: dict[str, SymbolContract] = {}
        stack = [root_node]
        while stack:
            node = stack.pop()
            if node.type in wanted:
                name = _extract_node_name(node, source_bytes)
                if name:
                    s_line = node.start_point[0] + 1
                    e_line = node.end_point[0] + 1
                    sig = _extract_signature_line(source_lines, s_line, e_line)
                    bare = _bare_symbol_name(name)
                    kind = _human_kind(node.type)
                    snippet_end = min(e_line, s_line + 17)
                    snippet = "\n".join(
                        f"L{ln}: {source_lines[ln - 1]}"
                        for ln in range(s_line, snippet_end + 1)
                        if 1 <= ln <= len(source_lines)
                    )
                    if e_line > snippet_end:
                        snippet += f"\n... (L{e_line}까지)"

                    contract = SymbolContract(
                        name=name,
                        bare_name=bare,
                        kind=kind,
                        start_line=s_line,
                        end_line=e_line,
                        signature=sig,
                        definition_snippet=snippet,
                        path=path,
                    )
                    if node.type in fn_types:
                        _populate_function_contract_treesitter(node, source_bytes, contract)
                    elif node.type in class_types:
                        contract.member_fields = _extract_class_fields_from_node(
                            node, source_bytes, language
                        )

                    symbols[name] = contract
                    if bare and bare not in symbols:
                        symbols[bare] = contract

            stack.extend(node.children)

        return symbols

    def _collect_line_nodes(
        self,
        root_node: Any,
        changed_lines: set[int],
        source_bytes: bytes,
        context: TreeSitterContext,
    ) -> None:
        """변경된 라인에 위치한 주요 구문 노드 및 호출 정보를 수집한다."""
        target_rows = {ln - 1 for ln in changed_lines}  # 0-indexed
        stack = [root_node]

        while stack:
            node = stack.pop()
            s_row, e_row = node.start_point[0], node.end_point[0]

            if not any(s_row <= r <= e_row for r in target_rows):
                continue

            # 함수 호출 수집
            if node.type in ("call_expression", "invocation_expression", "call"):
                fn_node = (
                    node.child_by_field_name("function")
                    or node.child_by_field_name("expression")
                )
                if fn_node is not None:
                    call_text = (
                        source_bytes[fn_node.start_byte : fn_node.end_byte]
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
                    if call_text and call_text not in context.calls:
                        context.calls.append(call_text)

            # 제어문 / 분기문 수집
            if node.type in (
                "if_statement",
                "switch_statement",
                "try_statement",
                "catch_clause",
                "except_clause",
            ):
                cond_node = node.child_by_field_name("condition")
                if cond_node is not None:
                    cond_text = (
                        source_bytes[cond_node.start_byte : cond_node.end_byte]
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
                    context.control_flow.append(
                        f"{node.type.replace('_statement', '')}({cond_text[:40]})"
                    )
                else:
                    context.control_flow.append(node.type.replace("_statement", ""))

            # 각 변경 라인에 해당하는 주요 구문 기록
            for row in range(s_row, min(e_row + 1, s_row + 2)):
                lineno = row + 1
                if lineno in changed_lines and node.type in _INTERESTING_NODE_TYPES:
                    desc = _describe_node(node, source_bytes)
                    if desc:
                        line_list = context.changed_line_nodes.setdefault(lineno, [])
                        if desc not in line_list:
                            line_list.append(desc)

            stack.extend(node.children)


def _attach_local_callees(
    context: TreeSitterContext,
    file_symbols: dict[str, SymbolContract],
    start_line: int,
    end_line: int,
) -> None:
    """청크에서 호출된 함수나 소속 클래스의 정의/계약 정보를 context 에 연결한다."""
    if not file_symbols:
        return

    enclosing_bare = _bare_symbol_name(context.enclosing_symbol or "")

    # 소속 클래스 정의도 symbol_definitions 에 등록
    for sym in file_symbols.values():
        if sym.member_fields and sym.bare_name not in context.symbol_definitions:
            fields_preview = "\n".join(f"  {f}" for f in sym.member_fields[:10])
            header = f"// {sym.path}:L{sym.start_line}-L{sym.end_line}" if sym.path else f"// L{sym.start_line}-L{sym.end_line}"
            context.symbol_definitions[sym.bare_name] = (
                f"{header}\n{sym.signature} {{\n{fields_preview}\n}}"
            )

    for call in context.calls:
        bare = _bare_symbol_name(call)
        sym = file_symbols.get(call) or file_symbols.get(bare)
        if sym is None:
            continue
        if sym.bare_name == enclosing_bare and start_line <= sym.start_line and sym.end_line <= end_line:
            continue
        rendered = sym.render_contract()
        if rendered not in context.callee_contracts:
            context.callee_contracts.append(rendered)
        if sym.definition_snippet:
            context.symbol_definitions[sym.bare_name] = sym.definition_snippet
            context.symbol_definitions[sym.name] = sym.definition_snippet


def _populate_function_contract_treesitter(
    fn_node: Any, source_bytes: bytes, contract: SymbolContract
) -> None:
    """함수 AST 본문을 순회하며 반환값, 예외 발생, 자원/락 연산, 피호출 함수를 추출한다."""
    stack = [fn_node]
    while stack:
        node = stack.pop()
        ntype = node.type

        if ntype == "return_statement":
            text = (
                source_bytes[node.start_byte : node.end_byte]
                .decode("utf-8", errors="replace")
                .strip()
            )
            ret_val = text[len("return") :].strip().rstrip(";").strip()
            if not ret_val:
                ret_val = "void"
            if len(ret_val) > 40:
                ret_val = ret_val[:37] + "..."
            if ret_val not in contract.returns:
                contract.returns.append(ret_val)

        elif ntype in ("throw_statement", "raise_statement", "throw_expression"):
            text = (
                source_bytes[node.start_byte : node.end_byte]
                .decode("utf-8", errors="replace")
                .strip()
                .rstrip(";")
            )
            if len(text) > 45:
                text = text[:42] + "..."
            if text not in contract.throws:
                contract.throws.append(text)

        elif ntype in ("delete_expression", "new_expression", "lock_statement", "with_statement", "using_statement"):
            text = (
                source_bytes[node.start_byte : node.end_byte]
                .decode("utf-8", errors="replace")
                .splitlines()[0]
                .strip()
            )
            if len(text) > 40:
                text = text[:37] + "..."
            if text not in contract.resource_ops:
                contract.resource_ops.append(text)

        elif ntype in ("call_expression", "invocation_expression", "call"):
            fn_child = (
                node.child_by_field_name("function")
                or node.child_by_field_name("expression")
            )
            if fn_child is not None:
                call_name = (
                    source_bytes[fn_child.start_byte : fn_child.end_byte]
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                if call_name and call_name not in contract.calls:
                    contract.calls.append(call_name)
                bare_call = _bare_symbol_name(call_name).lower()
                if bare_call in (
                    "lock", "unlock", "acquire", "release", "free", "malloc",
                    "realloc", "close", "dispose", "fopen", "fclose",
                ):
                    if call_name not in contract.resource_ops:
                        contract.resource_ops.append(call_name)

        stack.extend(node.children)


def _extract_class_fields_from_node(
    class_node: Any, source_bytes: bytes, language: Language
) -> list[str]:
    """클래스/구조체 AST 노드에서 멤버 변수/필드 선언 목록을 추출한다."""
    fields: list[str] = []
    field_types = {
        "field_declaration",
        "property_declaration",
        "expression_statement",
        "assignment",
    }
    stack = list(class_node.children)
    while stack:
        node = stack.pop(0)
        # 중첩된 메서드 본문 안의 일반 로컬 변수는 건너뜀 (단 Python __init__ 의 self.x 는 수집)
        if node.type in ("function_definition", "method_declaration", "constructor_declaration"):
            if language is Language.PYTHON:
                _extract_python_self_fields(node, source_bytes, fields)
            continue

        if node.type in field_types:
            line = (
                source_bytes[node.start_byte : node.end_byte]
                .decode("utf-8", errors="replace")
                .splitlines()[0]
                .strip()
                .rstrip(";")
            )
            if line and not line.startswith(("def ", "class ", "//", "#")):
                if len(line) > 60:
                    line = line[:57] + "..."
                if line not in fields:
                    fields.append(line)
            continue

        stack.extend(node.children)
    return fields[:10]


def _extract_python_self_fields(fn_node: Any, source_bytes: bytes, fields: list[str]) -> None:
    text = source_bytes[fn_node.start_byte : fn_node.end_byte].decode("utf-8", errors="replace")
    import re
    for match in re.finditer(r"\bself\.([A-Za-z_]\w*)\s*(?::\s*[^=\n]+)?=", text):
        field_name = f"self.{match.group(1)}"
        if field_name not in fields:
            fields.append(field_name)


def _extract_symbols_heuristic(
    source_lines: list[str], language: Language, path: str = ""
) -> dict[str, SymbolContract]:
    """tree-sitter 미설치 환경을 위한 정규식/블록 기반 심볼 계약 추출."""
    import re

    symbols: dict[str, SymbolContract] = {}
    if language is Language.PYTHON:
        py_def = re.compile(r"^(\s*)(?:async\s+def|def|class)\s+([A-Za-z_]\w*)\b")
        class_fields: list[str] = []
        for idx, line in enumerate(source_lines):
            m_self = re.search(r"\bself\.([A-Za-z_]\w*)\s*=", line)
            if m_self:
                fname = f"self.{m_self.group(1)}"
                if fname not in class_fields:
                    class_fields.append(fname)
            m = py_def.match(line)
            if not m:
                continue
            indent = len(m.group(1))
            name = m.group(2)
            s_line = idx + 1
            e_line = s_line
            for j in range(idx + 1, len(source_lines)):
                s = source_lines[j]
                if not s.strip() or s.lstrip().startswith("#"):
                    continue
                if len(s) - len(s.lstrip()) <= indent:
                    break
                e_line = j + 1
            kind = "클래스" if line.lstrip().startswith("class ") else "함수/메서드"
            sig = line.strip().rstrip(":")
            snippet_end = min(e_line, s_line + 17)
            snippet = "\n".join(
                f"L{ln}: {source_lines[ln - 1]}" for ln in range(s_line, snippet_end + 1)
            )
            contract = SymbolContract(
                name=name,
                bare_name=name,
                kind=kind,
                start_line=s_line,
                end_line=e_line,
                signature=sig,
                definition_snippet=snippet,
                path=path,
            )
            if kind == "함수/메서드":
                _populate_function_contract_lines(source_lines[s_line - 1 : e_line], contract)
            symbols[name] = contract
        for sym in symbols.values():
            if sym.kind == "클래스" and class_fields:
                sym.member_fields = class_fields[:10]
        return symbols

    # C++ / C# 휴리스틱
    fn_re = re.compile(
        r"^\s*(?:[\w:<>\*\s&~]+?)\b([A-Za-z_~][\w:]*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{?"
    )
    cls_re = re.compile(r"^\s*(?:class|struct|record)\s+([A-Za-z_]\w*)")
    ctrl_keywords = {"if", "for", "while", "switch", "catch", "return", "sizeof", "lock", "using"}

    for idx, line in enumerate(source_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*")):
            continue
        mc = cls_re.match(line)
        if mc and not stripped.endswith(";"):
            cname = mc.group(1)
            s_line = idx + 1
            e_line = _find_brace_end(source_lines, idx)
            fields = _extract_cpp_csharp_fields_lines(source_lines[s_line:e_line])
            symbols[cname] = SymbolContract(
                name=cname,
                bare_name=cname,
                kind="클래스",
                start_line=s_line,
                end_line=e_line,
                signature=stripped.rstrip("{").strip(),
                member_fields=fields,
                path=path,
            )
            continue

        mf = fn_re.match(line)
        if mf:
            name = mf.group(1)
            bare = _bare_symbol_name(name)
            if bare in ctrl_keywords:
                continue
            if "{" not in line and (idx + 1 >= len(source_lines) or "{" not in source_lines[idx + 1]):
                continue
            s_line = idx + 1
            e_line = _find_brace_end(source_lines, idx)
            sig = stripped.rstrip("{").strip()
            snippet_end = min(e_line, s_line + 17)
            snippet = "\n".join(
                f"L{ln}: {source_lines[ln - 1]}" for ln in range(s_line, snippet_end + 1)
            )
            contract = SymbolContract(
                name=name,
                bare_name=bare,
                kind="함수/메서드",
                start_line=s_line,
                end_line=e_line,
                signature=sig,
                definition_snippet=snippet,
                path=path,
            )
            _populate_function_contract_lines(source_lines[s_line - 1 : e_line], contract)
            symbols[name] = contract
            if bare not in symbols:
                symbols[bare] = contract

    return symbols


def _find_brace_end(source_lines: list[str], start_idx: int) -> int:
    depth = 0
    opened = False
    for i in range(start_idx, len(source_lines)):
        line = source_lines[i]
        opens = line.count("{")
        closes = line.count("}")
        if opens > 0:
            opened = True
        depth += opens - closes
        if opened and depth <= 0:
            return i + 1
    return min(len(source_lines), start_idx + 25)


def _extract_cpp_csharp_fields_lines(lines: list[str]) -> list[str]:
    import re
    fields: list[str] = []
    depth = 0
    for line in lines:
        s = line.strip()
        depth += s.count("{") - s.count("}")
        if depth > 0 or not s or s.startswith(("//", "#", "public:", "private:", "protected:")):
            continue
        if s.endswith(";") and "(" not in s and not s.startswith(("return ", "using ", "typedef ")):
            clean = s.rstrip(";").strip()
            if re.search(r"\b[A-Za-z_]\w*$", clean.split("=")[0].strip()):
                fields.append(clean[:60])
    return fields[:10]


def _populate_function_contract_lines(lines: list[str], contract: SymbolContract) -> None:
    import re
    for line in lines:
        s = line.strip()
        if s.startswith("return ") or s == "return;":
            val = s[len("return") :].strip().rstrip(";").strip() or "void"
            if val not in contract.returns:
                contract.returns.append(val[:40])
        if s.startswith(("throw ", "raise ")):
            if s.rstrip(";") not in contract.throws:
                contract.throws.append(s.rstrip(";")[:45])
        for call_name in _extract_calls_from_line(s):
            if call_name not in contract.calls:
                contract.calls.append(call_name)
            bare = _bare_symbol_name(call_name).lower()
            if bare in ("lock", "unlock", "free", "malloc", "realloc", "delete", "close", "dispose"):
                if call_name not in contract.resource_ops:
                    contract.resource_ops.append(call_name)


def _extract_calls_from_line(line: str) -> list[str]:
    import re
    ctrl = {"if", "for", "while", "switch", "catch", "return", "sizeof", "typeof", "nameof", "lock", "using"}
    out: list[str] = []
    for m in re.finditer(r"([A-Za-z_][\w\.:>-]*)\s*\(", line):
        cand = m.group(1)
        bare = _bare_symbol_name(cand)
        if bare not in ctrl and cand not in out:
            out.append(cand)
    return out


def _extract_signature_line(source_lines: list[str], start_line: int, end_line: int) -> str:
    parts: list[str] = []
    for ln in range(start_line, min(end_line + 1, start_line + 4)):
        if 1 <= ln <= len(source_lines):
            seg = source_lines[ln - 1].strip()
            parts.append(seg)
            if "{" in seg or seg.endswith(":"):
                break
    joined = " ".join(parts)
    if "{" in joined:
        joined = joined[: joined.index("{")].strip()
    elif joined.endswith(":"):
        joined = joined[:-1].strip()
    return joined[:120]


def _bare_symbol_name(name: str) -> str:
    if not name:
        return ""
    cleaned = name.split("(")[0].strip()
    for sep in ("::", "->", "."):
        if sep in cleaned:
            cleaned = cleaned.rsplit(sep, 1)[-1].strip()
    return cleaned


def _describe_node(node: Any, source_bytes: bytes) -> str:
    ntype = node.type
    text_snippet = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace").splitlines()
    snippet = text_snippet[0].strip() if text_snippet else ""
    if len(snippet) > 50:
        snippet = snippet[:47] + "..."

    label = _human_node_type(ntype)
    return f"{label} (`{snippet}`)" if snippet else label


def _human_node_type(ntype: str) -> str:
    mapping = {
        "call_expression": "함수 호출",
        "invocation_expression": "메서드 호출",
        "declaration": "변수/객체 선언",
        "local_declaration_statement": "로컬 변수 선언",
        "assignment_expression": "값 대입",
        "pointer_expression": "포인터/주소 연산",
        "delete_expression": "메모리 해제(delete)",
        "new_expression": "메모리 할당(new)",
        "if_statement": "if 조건문",
        "while_statement": "while 반복문",
        "for_range_loop": "for 범위 루프",
        "return_statement": "return 반환",
        "throw_statement": "throw 예외 발생",
        "try_statement": "try 예외 처리",
        "with_statement": "with 리소스 컨텍스트",
        "lock_statement": "lock 동기화 블록",
    }
    return mapping.get(ntype, ntype)


def _human_kind(ntype: str) -> str:
    if "function" in ntype or "method" in ntype or "constructor" in ntype or "destructor" in ntype:
        return "함수/메서드"
    if "class" in ntype:
        return "클래스"
    if "struct" in ntype or "record" in ntype:
        return "구조체"
    if "namespace" in ntype:
        return "네임스페이스"
    return "심볼"


def _extract_node_name(node: Any, source_bytes: bytes) -> str | None:
    try:
        named = (
            node.child_by_field_name("name")
            or node.child_by_field_name("declarator")
        )
        if named is not None:
            # C++ function_declarator 내부에 identifier / field_identifier / qualified_identifier 가 있을 수 있음
            if named.type in ("function_declarator", "pointer_declarator", "reference_declarator"):
                inner = named.child_by_field_name("declarator")
                while inner is not None and inner.type in ("function_declarator", "pointer_declarator", "reference_declarator"):
                    inner = inner.child_by_field_name("declarator")
                if inner is not None:
                    named = inner
            text = source_bytes[named.start_byte : named.end_byte]
            return text.decode("utf-8", errors="replace").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    return None

