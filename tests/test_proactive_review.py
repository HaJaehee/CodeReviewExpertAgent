"""Tree-sitter AST 구문 분석 및 적극적 리뷰/검증 프롬프트 연동 검증."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.chunk import Chunker, parse_unified_diff
from crex.filter import ReviewFilter, VERDICT_SCHEMA, VERIFIER_SYSTEM
from crex.generate import RULECHECKER_SYSTEM, RULECHECKER_USER, RuleChecker
from crex.report import to_markdown
from crex.rules import load_taxonomy
from crex.schema import Dimension, Finding, Language, ReviewChunk, Severity, StaticFinding
from crex.treesitter import TreeSitterAnalyzer
from tests.test_chunk import CPP_DIFF, CPP_SOURCE, PY_DIFF, PY_SOURCE


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_treesitter_ast_analyzer_cpp() -> None:
    analyzer = TreeSitterAnalyzer()
    lines = CPP_SOURCE.splitlines()
    ctx = analyzer.analyze(lines, Language.CPP, 14, 18, {15, 16, 17})

    _check(ctx.enclosing_symbol is not None and "Grow" in ctx.enclosing_symbol, f"심볼 누락: {ctx.enclosing_symbol}")
    _check("data_.resize" in ctx.calls or "resize" in "".join(ctx.calls), f"호출 누락: {ctx.calls}")
    _check(not ctx.has_error, "has_error 가 True")

    rendered = ctx.render_for_prompt()
    _check("둘러싼 심볼" in rendered, "심볼 렌더 누락")
    _check("변경 라인 AST 구문 요소" in rendered, "변경 라인 AST 렌더 누락")
    _check("호출된 주요 함수/메서드" in rendered, "호출 렌더 누락")


def test_treesitter_ast_analyzer_python() -> None:
    analyzer = TreeSitterAnalyzer()
    lines = PY_SOURCE.splitlines()
    ctx = analyzer.analyze(lines, Language.PYTHON, 8, 12, {8, 11, 12})

    _check(ctx.enclosing_symbol is not None and "load" in ctx.enclosing_symbol, f"심볼 누락: {ctx.enclosing_symbol}")
    _check(not ctx.has_error, "has_error 가 True")


def test_chunk_carries_ast_context() -> None:
    fd = parse_unified_diff(CPP_DIFF)[0]
    chunks = Chunker().chunk_file(fd, CPP_SOURCE)
    _check(len(chunks) == 1, "청크 1개여야 함")
    chunk = chunks[0]

    _check(chunk.ast_context is not None, "chunk.ast_context 가 None")
    _check("Grow" in chunk.render_ast_context(), "Grow 심볼이 ast_context 에 없음")


def test_static_finding_render_includes_review_guidance() -> None:
    sf = StaticFinding(
        tool="clang-tidy",
        path="src/buffer.cpp",
        line=33,
        rule_id="bugprone-use-after-move",
        message="use after move",
        severity=Severity.HIGH,
    )
    rendered = sf.render()
    _check("[clang-tidy:bugprone-use-after-move]" in rendered, "검사 ID 누락")
    _check("검토 지침" in rendered, "검토 지침 안내 누락")
    _check("적극 채택" in rendered, "적극 채택 지침 누락")


def test_rulechecker_prompt_contains_proactive_instructions() -> None:
    _check("엄격하고 능동적인" in RULECHECKER_SYSTEM, "능동적 시니어 코드리뷰어 페르소나 누락")
    _check("Tree-sitter AST 구문 분석 활용" in RULECHECKER_SYSTEM, "Tree-sitter 활용 섹션 누락")
    _check("정적분석 결과의 적극적 반영" in RULECHECKER_SYSTEM, "정적분석 적극 반영 섹션 누락")
    _check("Tree-sitter 구문 분석 (AST)" in RULECHECKER_USER, "User 템플릿에 AST 섹션 누락")


def test_verifier_prompt_and_comment_propagation() -> None:
    _check("수석 리뷰 검증관" in VERIFIER_SYSTEM, "수석 리뷰 검증관 페르소나 누락")
    _check("적극적으로 \"yes\" 로 승인하라" in VERIFIER_SYSTEM, "적극적 승인 지침 누락")
    _check(list(VERDICT_SCHEMA["properties"].keys())[0] == "verdict", "Conclusion-First 순서 위반")

    class StubVerifierClient:
        def complete_json(self, *args, **kwargs):
            return {
                "verdict": "yes",
                "code_present": True,
                "reason": "raw 포인터가 resize 이후 무효화되어 42 대입 시 댕글링 포인터 크래시 위험이 발생함.",
            }

    fd = parse_unified_diff(CPP_DIFF)[0]
    chunk = Chunker().chunk_file(fd, CPP_SOURCE)[0]
    finding = Finding(
        path="src/buffer.cpp",
        line=16,
        dimension=Dimension.DEFECT,
        severity=Severity.HIGH,
        rule_id="cpp.dangling-after-realloc",
        message="resize 후 raw 포인터 무효화",
        chunk_id=chunk.chunk_id,
    )

    rf = ReviewFilter(StubVerifierClient(), {chunk.chunk_id: chunk})
    kept, rejected = rf.filter([finding])

    _check(len(kept) == 1, "승인되어야 함")
    _check(kept[0].verifier_comment is not None, "verifier_comment 가 채워져야 함")
    _check("크래시 위험" in kept[0].verifier_comment, f"코멘트 내용 불일치: {kept[0].verifier_comment}")

    from crex.schema import ReviewResult
    res = ReviewResult(kept=kept)
    md = to_markdown(res)
    _check("검증관 코멘트" in md, f"마크다운 리포트에 검증관 코멘트 누락:\n{md}")
    _check("크래시 위험" in md, f"코멘트 내용 누락:\n{md}")


def test_stage1_symbol_contracts_and_member_fields() -> None:
    """1단계: 클래스 멤버 필드 및 같은 파일 내 피호출 함수의 반환/예외 계약 추출 검증."""
    cpp_code = """\
class PacketProcessor {
private:
    int* buffer_;
    size_t capacity_;
public:
    int* parse_header(const char* raw, size_t len) {
        if (len < 4) {
            return nullptr;
        }
        return buffer_;
    }

    int process(const char* input, size_t n) {
        int* hdr = parse_header(input, n);
        *hdr = 1;
        return 0;
    }
};
"""
    analyzer = TreeSitterAnalyzer()
    lines = cpp_code.splitlines()
    symbols = analyzer.extract_file_symbols(lines, Language.CPP, path="src/packet.cpp")
    _check("parse_header" in symbols, f"parse_header 심볼 추출 실패: {list(symbols.keys())}")
    _check("nullptr" in symbols["parse_header"].returns, f"반환값 nullptr 누락: {symbols['parse_header'].returns}")

    # process 메서드(L13-L17) 분석 시 소속 클래스 멤버 필드와 parse_header 피호출 계약이 주입되어야 함
    ctx = analyzer.analyze(
        lines, Language.CPP, 13, 17, {14, 15}, path="src/packet.cpp", file_symbols=symbols
    )
    rendered = ctx.render_for_prompt()
    _check(
        any("buffer_" in f for f in ctx.member_fields),
        f"멤버 필드 buffer_ 추출 누락: {ctx.member_fields}",
    )
    _check(
        any("parse_header" in c and "nullptr" in c for c in ctx.callee_contracts),
        f"피호출 함수 parse_header 계약 누락: {ctx.callee_contracts}",
    )
    _check("소속 클래스/구조체 멤버 필드" in rendered, f"멤버 필드 프롬프트 렌더 누락:\n{rendered}")
    _check("연관/피호출 함수 시그니처 및 계약" in rendered, f"피호출 계약 프롬프트 렌더 누락:\n{rendered}")
    _check("parse_header" in ctx.symbol_definitions, "symbol_definitions 에 parse_header 스니펫 누락")


def test_stage1_cross_chunk_change_map() -> None:
    """1단계: 다중 파일/청크 간 Change Map 및 교차 파일 피호출 계약 주입 검증."""
    from crex.chunk import attach_cross_chunk_context

    callee_src = """\
def fetch_session(token: str):
    if not token:
        return None
    if len(token) < 8:
        raise ValueError('invalid token')
    return {'user': token}
"""
    caller_src = """\
def handle_request(req_token: str):
    sess = fetch_session(req_token)
    return sess['user']
"""
    analyzer = TreeSitterAnalyzer()
    callee_lines = callee_src.splitlines()
    caller_lines = caller_src.splitlines()

    callee_syms = analyzer.extract_file_symbols(callee_lines, Language.PYTHON, path="src/auth.py")
    caller_syms = analyzer.extract_file_symbols(caller_lines, Language.PYTHON, path="src/handler.py")

    callee_ctx = analyzer.analyze(
        callee_lines, Language.PYTHON, 1, 6, {2, 3}, path="src/auth.py", file_symbols=callee_syms
    )
    caller_ctx = analyzer.analyze(
        caller_lines, Language.PYTHON, 1, 3, {2, 3}, path="src/handler.py", file_symbols=caller_syms
    )

    c1 = ReviewChunk(
        chunk_id="src/auth.py#0",
        path="src/auth.py",
        language=Language.PYTHON,
        start_line=1,
        end_line=6,
        changed_linenos={2, 3},
        enclosing_symbol=callee_ctx.enclosing_symbol or "fetch_session",
        ast_context=callee_ctx.render_for_prompt(),
        ast_context_compact=callee_ctx.render_for_prompt(compact=True),
        calls=list(callee_ctx.calls),
        symbol_definitions=dict(callee_ctx.symbol_definitions),
    )
    c2 = ReviewChunk(
        chunk_id="src/handler.py#0",
        path="src/handler.py",
        language=Language.PYTHON,
        start_line=1,
        end_line=3,
        changed_linenos={2, 3},
        enclosing_symbol=caller_ctx.enclosing_symbol or "handle_request",
        ast_context=caller_ctx.render_for_prompt(),
        ast_context_compact=caller_ctx.render_for_prompt(compact=True),
        calls=list(caller_ctx.calls),
        symbol_definitions=dict(caller_ctx.symbol_definitions),
    )

    attach_cross_chunk_context(
        [c1, c2],
        file_symbols_by_path={"src/auth.py": callee_syms, "src/handler.py": caller_syms},
    )

    c2_ast = c2.render_ast_context()
    _check("전체 변경 요약 지도(Change Map)" in c2_ast, f"Change Map 누락:\n{c2_ast}")
    _check("src/auth.py" in c2_ast, f"타 파일 변경 내역이 Change Map 에 없음:\n{c2_ast}")
    _check("fetch_session" in c2_ast and "None" in c2_ast, f"교차 파일 피호출 계약 누락:\n{c2_ast}")
    _check("fetch_session" in c2.symbol_definitions, "교차 파일 심볼 정의가 c2.symbol_definitions 에 없음")


TESTS = [
    test_treesitter_ast_analyzer_cpp,
    test_treesitter_ast_analyzer_python,
    test_chunk_carries_ast_context,
    test_static_finding_render_includes_review_guidance,
    test_rulechecker_prompt_contains_proactive_instructions,
    test_verifier_prompt_and_comment_propagation,
    test_stage1_symbol_contracts_and_member_fields,
    test_stage1_cross_chunk_change_map,
]


def main() -> int:
    from crex.cli import force_utf8_output

    force_utf8_output()
    failures = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
