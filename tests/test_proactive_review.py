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


def test_stage2_verifier_context_expansion() -> None:
    """2단계: 검증기(ReviewFilter)가 ±25줄 창 바깥의 피호출 함수 정의를 프롬프트에 첨부하는지 검증."""
    from crex.schema import DiffLine, LineStatus

    captured_prompts: list[str] = []

    class CapturingVerifier:
        def complete_json(self, system: str, user: str, *args, **kwargs):
            captured_prompts.append(user)
            return {
                "verdict": "yes",
                "code_present": True,
                "reason": "창 외부 정의에서 acquire_slot 이 nullptr 을 반환함이 확인되어 역참조 위험이 타당함.",
            }

    # L1~L6 에 정의된 acquire_slot 은 L80 주변 ±25줄 창(L55~L105) 밖에 위치함
    chunk = ReviewChunk(
        chunk_id="src/pool.cpp#0",
        path="src/pool.cpp",
        language=Language.CPP,
        start_line=75,
        end_line=85,
        lines=[
            DiffLine(LineStatus.UNCHANGED, 78, "void Pool::Run() {"),
            DiffLine(LineStatus.ADDED, 79, "    Slot* s = acquire_slot();"),
            DiffLine(LineStatus.ADDED, 80, "    s->use();"),
            DiffLine(LineStatus.UNCHANGED, 81, "}"),
        ],
        changed_linenos={79, 80},
        enclosing_symbol="Pool::Run",
        calls=["acquire_slot", "s->use"],
        symbol_definitions={
            "acquire_slot": (
                "L1: Slot* Pool::acquire_slot() {\n"
                "L2:     if (free_list_.empty()) return nullptr;\n"
                "L3:     return free_list_.back();\n"
                "L4: }"
            ),
            "in_window_helper": (
                "L76: void in_window_helper() {\n"
                "L77: }"
            ),
        },
    )

    finding = Finding(
        path="src/pool.cpp",
        line=80,
        dimension=Dimension.DEFECT,
        severity=Severity.HIGH,
        rule_id="any.null-dereference",
        message="acquire_slot() 이 nullptr 을 반환할 수 있는데 널 검사 없이 s->use() 를 호출함",
        chunk_id=chunk.chunk_id,
    )

    rf = ReviewFilter(CapturingVerifier(), {chunk.chunk_id: chunk})
    kept, rejected = rf.filter([finding])

    _check(len(kept) == 1 and not rejected, "지적이 유지되어야 함")
    _check(len(captured_prompts) == 1, "검증기 호출 1회여야 함")
    prompt = captured_prompts[0]
    _check(
        "연관 심볼 및 피호출 함수 정의 (창 외부 문맥)" in prompt,
        f"창 외부 문맥 섹션이 검증 프롬프트에 없음:\n{prompt}",
    )
    _check(
        "free_list_.empty()" in prompt and "return nullptr" in prompt,
        f"창 외부 acquire_slot 본문이 검증 프롬프트에 첨부되지 않음:\n{prompt}",
    )
    _check(
        "### `in_window_helper`" not in prompt,
        f"이미 ±25줄 창 안에 있는 심볼이 중복 첨부됨:\n{prompt}",
    )


def test_stage3_call_graph_slice_and_flow_hazards() -> None:
    """3단계: 다단계 호출 그래프 슬라이싱(Caller -> Fn -> Callee 1~2 hop) 및 결정론적 흐름 위험 신호 탐지 검증."""
    from crex.chunk import Chunker, attach_cross_chunk_context, parse_unified_diff
    from crex.flow import FlowAnalyzer

    repo_src = """\
Record* fetch_from_db(int id) {
    if (id <= 0) {
        return nullptr;
    }
    return db_query(id);
}

Record* load_user_record(int uid) {
    int normalized = uid * 10;
    int step1 = normalized + 1;
    int step2 = step1 + 2;
    int step3 = step2 + 3;
    int step4 = step3 + 4;
    int step5 = step4 + 5;
    return fetch_from_db(uid);
}
"""
    service_src = """\
bool run_job(int uid) {
    Buffer* raw_buf = new Buffer(256);
    Record* rec = load_user_record(uid);
    rec->touch();
    delete raw_buf;
    return true;
}
"""
    diff_text = """\
diff --git a/src/service.cpp b/src/service.cpp
index 1111111..2222222 100644
--- a/src/service.cpp
+++ b/src/service.cpp
@@ -1,6 +1,7 @@
 bool run_job(int uid) {
     Buffer* raw_buf = new Buffer(256);
+    Record* rec = load_user_record(uid);
+    rec->touch();
     delete raw_buf;
     return true;
 }
"""
    chunker = Chunker()
    ts = chunker.treesitter_analyzer
    file_symbols = {
        "src/repo.cpp": ts.extract_file_symbols(repo_src.splitlines(), Language.CPP, path="src/repo.cpp"),
        "src/service.cpp": ts.extract_file_symbols(service_src.splitlines(), Language.CPP, path="src/service.cpp"),
    }
    fdiffs = parse_unified_diff(diff_text)
    chunks = chunker.chunk_file(fdiffs[0], service_src)
    attach_cross_chunk_context(chunks, file_symbols_by_path=file_symbols)
    FlowAnalyzer(ts).enrich(chunks, file_symbols_by_path=file_symbols)

    _check(len(chunks) == 1, f"청크 개수 불일치: {len(chunks)}")
    chunk = chunks[0]
    _check(bool(chunk.flow_context), "flow_context 가 생성되지 않음")
    rendered = chunk.render_ast_context()
    _check(
        "호출 그래프 실행 경로 슬라이스(Call-Graph Flow)" in rendered,
        f"호출 그래프 슬라이스 섹션 누락:\n{rendered}",
    )
    _check(
        "load_user_record" in rendered and "fetch_from_db" in rendered,
        f"2단계 피호출 체인(load_user_record -> fetch_from_db) 누락:\n{rendered}",
    )
    _check(
        "중간 생략" in rendered,
        f"긴 함수에 대한 접힌 실행 슬라이스(중간 생략)가 동작하지 않음:\n{rendered}",
    )
    _check(
        "교차 함수 흐름 위험 신호(Flow Hazards)" in rendered,
        f"흐름 위험 신호 섹션 누락:\n{rendered}",
    )
    _check(
        "[널/오류 반환 미검사 위험]" in rendered and "nullptr" in rendered,
        f"2-hop nullptr 반환 미검사 역참조 위험 신호 누락:\n{rendered}",
    )


def test_stage3_workspace_neighbor_discovery() -> None:
    """3단계: diff 에 없는 저장소 내 미수정 인접 파일에서 피호출 함수와 상위 호출부를 자동 탐색하는지 검증."""
    import tempfile
    from pathlib import Path
    from crex.chunk import Chunker, parse_unified_diff
    from crex.flow import FlowAnalyzer

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src_dir = root / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        # 미수정 인접 파일 1: 피호출 함수 정의 (None 반환 가능)
        (src_dir / "storage.py").write_text(
            "def find_account(account_id: int):\n"
            "    if account_id < 0:\n"
            "        return None\n"
            "    return {'id': account_id, 'balance': 100}\n",
            encoding="utf-8",
        )
        # 미수정 인접 파일 2: 변경된 함수를 호출하는 상위 호출부 (반환값 미검사 사용)
        (src_dir / "controller.py").write_text(
            "def handle_withdraw_Endpoint(req_id: int):\n"
            "    result = withdraw_amount(req_id)\n"
            "    return result.strip()\n",
            encoding="utf-8",
        )
        # 수정된 파일: find_account 를 호출해 검사 없이 인덱싱하고, 실패 시 None 반환
        billing_src = (
            "def withdraw_amount(account_id: int):\n"
            "    acct = find_account(account_id)\n"
            "    if acct['balance'] <= 0:\n"
            "        return None\n"
            "    return 'ok'\n"
        )
        (src_dir / "billing.py").write_text(billing_src, encoding="utf-8")

        diff_text = """\
diff --git a/src/billing.py b/src/billing.py
index 1111111..2222222 100644
--- a/src/billing.py
+++ b/src/billing.py
@@ -1,5 +1,5 @@
 def withdraw_amount(account_id: int):
+    acct = find_account(account_id)
+    if acct['balance'] <= 0:
+        return None
     return 'ok'
"""
        chunker = Chunker()
        ts = chunker.treesitter_analyzer
        file_symbols = {
            "src/billing.py": ts.extract_file_symbols(
                billing_src.splitlines(), Language.PYTHON, path="src/billing.py"
            )
        }
        fdiffs = parse_unified_diff(diff_text)
        chunks = chunker.chunk_file(fdiffs[0], billing_src)
        FlowAnalyzer(ts).enrich(
            chunks,
            file_symbols_by_path=file_symbols,
            repo_root=root,
        )

        chunk = chunks[0]
        _check("find_account" in chunk.symbol_definitions, "미수정 파일의 find_account 정의가 수집되지 않음")
        _check(
            "handle_withdraw_Endpoint" in chunk.symbol_definitions,
            "미수정 파일의 상위 호출부 handle_withdraw_Endpoint 정의가 수집되지 않음",
        )
        _check(
            "[상위 호출부 슬라이스]" in chunk.flow_context and "src/controller.py" in chunk.flow_context,
            f"미수정 파일의 상위 호출부 슬라이스가 flow_context 에 없음:\n{chunk.flow_context}",
        )
        _check(
            "[하위 피호출 슬라이스]" in chunk.flow_context and "src/storage.py" in chunk.flow_context,
            f"미수정 파일의 하위 피호출 슬라이스가 flow_context 에 없음:\n{chunk.flow_context}",
        )
        _check(
            "[널/오류 반환 미검사 위험]" in chunk.flow_context,
            f"미수정 파일 피호출 함수의 None 반환 미검사 위험 신호 누락:\n{chunk.flow_context}",
        )
        _check(
            "[상위 호출부 계약 불일치 위험]" in chunk.flow_context,
            f"미수정 상위 호출부와의 계약 불일치 위험 신호 누락:\n{chunk.flow_context}",
        )


TESTS = [
    test_treesitter_ast_analyzer_cpp,
    test_treesitter_ast_analyzer_python,
    test_chunk_carries_ast_context,
    test_static_finding_render_includes_review_guidance,
    test_rulechecker_prompt_contains_proactive_instructions,
    test_verifier_prompt_and_comment_propagation,
    test_stage1_symbol_contracts_and_member_fields,
    test_stage1_cross_chunk_change_map,
    test_stage2_verifier_context_expansion,
    test_stage3_call_graph_slice_and_flow_hazards,
    test_stage3_workspace_neighbor_discovery,
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
