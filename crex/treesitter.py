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
class TreeSitterContext:
    """한 청크에 대한 Tree-sitter AST 분석 결과."""

    enclosing_symbol: str | None = None
    symbol_kind: str | None = None
    changed_line_nodes: dict[int, list[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    control_flow: list[str] = field(default_factory=list)
    has_error: bool = False

    def render_for_prompt(self) -> str:
        """프롬프트에 주입할 마크다운 형태의 한국어 요약을 만든다."""
        lines: list[str] = []

        if self.enclosing_symbol:
            kind_str = f" ({self.symbol_kind})" if self.symbol_kind else ""
            lines.append(f"- 둘러싼 심볼: `{self.enclosing_symbol}`{kind_str}")

        if self.changed_line_nodes:
            lines.append("- 변경 라인 AST 구문 요소:")
            for lineno in sorted(self.changed_line_nodes.keys()):
                items = self.changed_line_nodes[lineno]
                summary = ", ".join(items[:4])
                lines.append(f"  - L{lineno}: {summary}")

        if self.calls:
            unique_calls = list(dict.fromkeys(self.calls))[:10]
            lines.append(f"- 호출된 주요 함수/메서드: {', '.join(f'`{c}`' for c in unique_calls)}")

        if self.control_flow:
            unique_cf = list(dict.fromkeys(self.control_flow))[:6]
            lines.append(f"- 제어 흐름/조건: {', '.join(unique_cf)}")

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

    def analyze(
        self,
        source_lines: list[str],
        language: Language,
        start_line: int,
        end_line: int,
        changed_lines: set[int],
    ) -> TreeSitterContext:
        """주어진 소스 라인과 대상 범위, 변경 라인들을 분석하여 TreeSitterContext 를 생성한다."""
        parser = self._get_parser(language)
        if parser is None or not source_lines:
            return TreeSitterContext()

        source_bytes = "\n".join(source_lines).encode("utf-8")
        try:
            tree = parser.parse(source_bytes)
        except Exception as exc:  # noqa: BLE001
            log.debug("tree-sitter 파싱 실패: %s", exc)
            return TreeSitterContext()

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

        # 2. 변경된 라인 대상 AST 노드 탐색
        if changed_lines:
            self._collect_line_nodes(tree.root_node, changed_lines, source_bytes, context)

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
            if node.type in ("call_expression", "invocation_expression"):
                fn_node = node.child_by_field_name("function") or node.child_by_field_name("expression")
                if fn_node is not None:
                    call_text = source_bytes[fn_node.start_byte : fn_node.end_byte].decode("utf-8", errors="replace").strip()
                    if call_text and call_text not in context.calls:
                        context.calls.append(call_text)

            # 제어문 / 분기문 수집
            if node.type in ("if_statement", "switch_statement", "try_statement", "catch_clause", "except_clause"):
                cond_node = node.child_by_field_name("condition")
                if cond_node is not None:
                    cond_text = source_bytes[cond_node.start_byte : cond_node.end_byte].decode("utf-8", errors="replace").strip()
                    context.control_flow.append(f"{node.type.replace('_statement', '')}({cond_text[:40]})")
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
    if "function" in ntype or "method" in ntype:
        return "함수/메서드"
    if "class" in ntype:
        return "클래스"
    if "struct" in ntype:
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
            text = source_bytes[named.start_byte : named.end_byte]
            return text.decode("utf-8", errors="replace").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    return None
