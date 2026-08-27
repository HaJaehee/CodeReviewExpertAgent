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


TESTS = [
    test_treesitter_ast_analyzer_cpp,
    test_treesitter_ast_analyzer_python,
    test_chunk_carries_ast_context,
    test_static_finding_render_includes_review_guidance,
    test_rulechecker_prompt_contains_proactive_instructions,
    test_verifier_prompt_and_comment_propagation,
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
