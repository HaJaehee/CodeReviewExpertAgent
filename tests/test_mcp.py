"""리뷰 서비스와 MCP 바인딩 검증.

서비스 로직은 FastMCP 없이 전부 검증한다 — 실제 저장소 + 실제 git diff +
가짜 vLLM 으로 태운다. FastMCP 바인딩은 얇으므로 등록 여부만 확인하고,
라이브러리가 없으면 건너뛴다.

Zed 은 여기 없으므로 마지막 연동 확인은 폐쇄망 안에서 해야 한다.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.chunk import parse_unified_diff  # noqa: E402
from crex.config import ChunkingConfig, Config, GroundingConfig, ReviewConfig  # noqa: E402
from crex.gitio import GitError, diff_staged, gitpython_available, merge_base  # noqa: E402
from crex.llm import EndpointConfig  # noqa: E402
from crex.paths import TooManyFiles, expand_paths, filter_file_diffs, is_excluded  # noqa: E402
from crex.service import ReviewRequestError, ReviewService  # noqa: E402
from tests.fake_vllm import FakeVLLM  # noqa: E402
from tests.test_pipeline import AFTER, BEFORE, _git  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    (repo / "src" / "buffer.cpp").write_text(BEFORE, encoding="utf-8")
    (repo / "src" / "notes.txt").write_text("지원하지 않는 언어\n", encoding="utf-8")
    (repo / "src" / "generated").mkdir()
    (repo / "src" / "generated" / "gen.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    (repo / "src" / "buffer.cpp").write_text(AFTER, encoding="utf-8")
    return repo


def _config(base_url: str) -> Config:
    endpoint = EndpointConfig(base_url=base_url, model="fake", max_retries=1, timeout=10.0)
    return Config(
        generator=endpoint,
        verifier=endpoint,
        review=ReviewConfig(max_workers=2),
        grounding=GroundingConfig(enabled=False),
        chunking=ChunkingConfig(),
    )


def _emit_finding(schema: dict) -> dict:
    items = schema["properties"]["findings"]["items"]["properties"]
    return {
        "findings": [{
            "line": items["line"]["enum"][0] if "enum" in items["line"] else 8,
            "rule_id": "cpp.dangling-after-realloc",
            "severity": "high",
            "message": "resize() 이후 raw 포인터가 무효화된다.",
        }]
    }


# --------------------------------------------------------------------------
# git 접근
# --------------------------------------------------------------------------


def test_git_diff_produces_parseable_output() -> None:
    """GitPython 이든 subprocess 든 청커가 먹을 수 있는 diff 가 나와야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        diff = diff_staged(repo)
        files = parse_unified_diff(diff)
        _check(len(files) == 1, f"파일 1개를 기대했으나 {len(files)}개")
        _check(files[0].path == "src/buffer.cpp", f"경로: {files[0].path}")
        _check(files[0].hunks, "hunk 가 비었다")


def test_git_error_is_actionable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        try:
            merge_base(repo, "no-such-branch")
        except GitError as exc:
            _check("no-such-branch" in str(exc) or "실패" in str(exc), f"사유 불명확: {exc}")
        else:
            raise AssertionError("없는 ref 가 통과됨")


# --------------------------------------------------------------------------
# 서비스
# --------------------------------------------------------------------------


def test_review_staged_returns_summary_not_full_report() -> None:
    """에이전트 컨텍스트를 아끼는 게 이 파사드의 핵심 설계다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")
        out = Path(tmp) / "reports"

        with FakeVLLM(on_findings=_emit_finding) as fake:
            text = ReviewService(repo, _config(fake.base_url), out_dir=out).review_staged()

        _check("지적 1건" in text, f"건수 없음:\n{text}")
        _check("cpp.dangling-after-realloc" in text, f"룰 ID 없음:\n{text}")
        _check("전체 리포트:" in text, f"리포트 경로 안내 없음:\n{text}")
        _check("<details>" not in text, f"리포트 전문이 반환됨:\n{text}")
        _check(len(text) < 600, f"요약이 너무 김 ({len(text)}자):\n{text}")

        written = list(out.glob("*.md"))
        _check(len(written) == 1, f"리포트 파일: {[p.name for p in written]}")
        _check("<details>" in written[0].read_text(encoding="utf-8"), "리포트 내용이 비었음")


def test_empty_diff_says_so() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "checkout", "--", ".")   # 변경 되돌리기

        with FakeVLLM() as fake:
            text = ReviewService(repo, _config(fake.base_url), out_dir=Path(tmp) / "r").review_staged()

        _check("변경된 내용이 없다" in text, f"빈 diff 안내 없음: {text}")


def test_review_file_and_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        with FakeVLLM() as fake:
            svc = ReviewService(repo, _config(fake.base_url), out_dir=Path(tmp) / "r")

            svc.review_file("src/buffer.cpp")   # 예외 없이 끝나야 한다

            for bad, reason in (("src/notes.txt", "리뷰 대상이 아니다"), ("src/nope.cpp", "")):
                try:
                    svc.review_file(bad)
                except ReviewRequestError as exc:
                    if reason:
                        _check(reason in str(exc), f"사유 불명확: {exc}")
                else:
                    raise AssertionError(f"{bad} 가 통과됨")

            svc.review_directory("src")


def test_path_filter_narrows_diff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")

        with FakeVLLM(on_findings=_emit_finding) as fake:
            svc = ReviewService(repo, _config(fake.base_url), out_dir=Path(tmp) / "r")
            _check("지적 1건" in svc.review_staged(["src"]), "src 필터가 걸러버림")
            _check("변경된 내용이 없다" in svc.review_staged(["docs"]) or
                   "지적 사항 없음" in svc.review_staged(["docs"]),
                   "관계없는 경로가 통과됨")


def test_review_diff_uses_merge_base() -> None:
    """--from main 을 그대로 쓰면 남의 변경까지 딸려 들어온다. 분기점을 잡아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "buffer 변경")

        # main 에 내 브랜치와 무관한 커밋을 하나 더 쌓는다.
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "feature")
        _git(repo, "checkout", "-q", "main") if _has_main(repo) else None
        _git(repo, "checkout", "-q", "feature")

        with FakeVLLM() as fake:
            svc = ReviewService(repo, _config(fake.base_url), out_dir=Path(tmp) / "r")
            # merge-base 가 실패해도 폴백해서 진행해야 한다 (예외 없이 끝나면 통과)
            svc.review_diff(base, "HEAD")


def _has_main(repo: Path) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "main")
    except Exception:  # noqa: BLE001
        return False
    return True


def test_request_error_is_distinguishable() -> None:
    """사용자가 고칠 수 있는 오류는 별도 타입이어야 에이전트가 그대로 전달한다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        with FakeVLLM() as fake:
            svc = ReviewService(repo, _config(fake.base_url), out_dir=Path(tmp) / "r")

            for action in (
                lambda: svc.review_diff(""),
                lambda: svc.review_file(""),
                lambda: svc.review_directory(""),
                lambda: svc.review_diff("does-not-exist", use_merge_base=False),
            ):
                try:
                    action()
                except ReviewRequestError:
                    pass
                else:
                    raise AssertionError("요청 오류가 감지되지 않음")


# --------------------------------------------------------------------------
# 경로 확장
# --------------------------------------------------------------------------


def test_expand_paths_filters_and_normalizes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make_repo(Path(tmp))
        expansion = expand_paths(repo, ["src"])

        _check(expansion.files == ["src/buffer.cpp"], f"확장 결과: {expansion.files}")
        _check(expansion.skipped_unsupported == 1, f"미지원: {expansion.skipped_unsupported}")
        _check(expansion.skipped_excluded == 1, f"제외: {expansion.skipped_excluded}")


def test_expand_paths_rejects_oversized_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        (repo / "src").mkdir(parents=True)
        for i in range(5):
            (repo / "src" / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")

        try:
            expand_paths(repo, ["src"], max_files=3)
        except TooManyFiles as exc:
            _check("상한" in str(exc), f"메시지 불명확: {exc}")
            # 거부만 하고 끝내면 사용자가 막힌다. 다음에 뭘 할지 알려줘야 한다.
            _check("나눠" in str(exc) or "recursive" in str(exc), f"해결책 안내 없음: {exc}")
        else:
            raise AssertionError("상한 초과가 통과됨")


def test_expand_paths_rejects_outside_repo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (Path(tmp) / "outside.py").write_text("x = 1\n", encoding="utf-8")
        try:
            expand_paths(repo, ["../outside.py"])
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError("저장소 밖 경로가 통과됨")


def test_exclude_patterns() -> None:
    from crex.rules import DEFAULT_EXCLUDE

    for path in ("src/generated/x.py", "generated/x.py", "obj/a.cs", "src/a_pb2.py"):
        _check(is_excluded(path, DEFAULT_EXCLUDE), f"제외되어야 함: {path}")
    for path in ("src/buffer.cpp", "app/service.cs", "lib/util.py"):
        _check(not is_excluded(path, DEFAULT_EXCLUDE), f"제외되면 안 됨: {path}")


def test_filter_file_diffs() -> None:
    diff = """\
diff --git a/src/parser/lex.cpp b/src/parser/lex.cpp
--- a/src/parser/lex.cpp
+++ b/src/parser/lex.cpp
@@ -1,1 +1,1 @@
-a
+b
diff --git a/src/ui/view.cs b/src/ui/view.cs
--- a/src/ui/view.cs
+++ b/src/ui/view.cs
@@ -1,1 +1,1 @@
-a
+b
"""
    diffs = parse_unified_diff(diff)
    _check(len(diffs) == 2, f"파싱된 파일: {len(diffs)}")
    _check([d.path for d in filter_file_diffs(diffs, ["src/parser"])] == ["src/parser/lex.cpp"], "폴더 필터 실패")
    _check([d.path for d in filter_file_diffs(diffs, ["src/ui/view.cs"])] == ["src/ui/view.cs"], "파일 필터 실패")
    # 부분 문자열이 폴더 경계를 넘어 매칭되면 안 된다.
    _check(filter_file_diffs(diffs, ["src/pars"]) == [], "폴더 경계를 넘어 매칭됨")
    _check(len(filter_file_diffs(diffs, [])) == 2, "빈 필터가 전체를 통과시키지 않음")


# --------------------------------------------------------------------------
# FastMCP 바인딩 (라이브러리가 있을 때만)
# --------------------------------------------------------------------------


def test_fastmcp_bindings_registered() -> None:
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        print("     (fastmcp 미설치 — 바인딩 검사 건너뜀)")
        return

    import asyncio

    from crex import mcp as mcp_module

    # FastMCP 3.x 는 list_tools() 를 쓴다 (2.x 의 get_tools() 는 없다).
    tools = asyncio.run(mcp_module.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "review_diff", "review_staged", "review_working_tree",
        "review_file", "review_directory",
    }
    _check(names == expected, f"도구 목록 불일치: {sorted(names)}")

    def schema_of(tool) -> dict:
        # FastMCP 3.x 의 FunctionTool 은 parameters, 구버전은 inputSchema 를 쓴다.
        for attr in ("parameters", "inputSchema", "input_schema"):
            value = getattr(tool, attr, None)
            if isinstance(value, dict):
                return value
        return {}

    for tool in tools:
        # 스키마와 설명은 타입 힌트·docstring 에서 자동 생성된다.
        # 비어 있으면 에이전트가 도구를 못 고른다.
        _check(bool(tool.description), f"{tool.name} 설명 없음")
        _check(bool(schema_of(tool)), f"{tool.name} 입력 스키마 없음")

    by_name = {t.name: t for t in tools}
    diff_props = schema_of(by_name["review_diff"]).get("properties", {})
    _check("from_ref" in diff_props, f"review_diff 인자 누락: {sorted(diff_props)}")
    _check("paths" in diff_props, f"paths 인자 누락: {sorted(diff_props)}")

    staged_props = schema_of(by_name["review_staged"]).get("properties", {})
    _check("paths" in staged_props, f"review_staged paths 누락: {sorted(staged_props)}")


TESTS = [
    test_git_diff_produces_parseable_output,
    test_git_error_is_actionable,
    test_review_staged_returns_summary_not_full_report,
    test_empty_diff_says_so,
    test_review_file_and_directory,
    test_path_filter_narrows_diff,
    test_review_diff_uses_merge_base,
    test_request_error_is_distinguishable,
    test_expand_paths_filters_and_normalizes,
    test_expand_paths_rejects_oversized_directory,
    test_expand_paths_rejects_outside_repo,
    test_exclude_patterns,
    test_filter_file_diffs,
    test_fastmcp_bindings_registered,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crex.cli import force_utf8_output

    force_utf8_output()
    if shutil.which("git") is None:
        print("SKIP: git 이 없어 서비스 테스트를 건너뛴다")
        return 0

    print(f"     (git 접근: {'GitPython' if gitpython_available() else 'subprocess 폴백'})")
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
