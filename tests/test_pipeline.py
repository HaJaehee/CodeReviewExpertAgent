"""파이프라인 종단 검증.

실제 git 저장소를 임시로 만들고, 실제 `git diff` 를 뽑고, 가짜 vLLM 을 띄워
HTTP 로 태운다. 손으로 쓴 diff 는 앞서 두 번이나 틀렸으므로 git 이 만든 것만 쓴다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.config import ChunkingConfig, Config, GroundingConfig, ReviewConfig  # noqa: E402
from crex.llm import EndpointConfig  # noqa: E402
from crex.pipeline import Pipeline  # noqa: E402
from crex.report import to_markdown, to_sarif  # noqa: E402
from tests.fake_vllm import FakeVLLM  # noqa: E402

BEFORE = """\
#include <vector>

class Buffer {
 public:
  explicit Buffer(size_t n) : data_(n) {}

  void Grow(size_t extra) {
    data_.resize(data_.size() + extra);
  }

 private:
  std::vector<int> data_;
};
"""

AFTER = """\
#include <vector>

class Buffer {
 public:
  explicit Buffer(size_t n) : data_(n) {}

  void Grow(size_t extra) {
    int* raw = &data_[0];
    data_.resize(data_.size() + extra);
    raw[0] = 42;
  }

 private:
  std::vector<int> data_;
};
"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return completed.stdout


def _make_repo(tmp: Path) -> tuple[Path, str]:
    """커밋 하나짜리 저장소를 만들고 작업 트리를 수정한 뒤 diff 를 돌려준다."""
    repo = tmp / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    target = repo / "src" / "buffer.cpp"
    target.write_text(BEFORE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    target.write_text(AFTER, encoding="utf-8")
    diff = _git(repo, "diff", "--no-color", "-U3")
    return repo, diff


def _config(base_url: str) -> Config:
    endpoint = EndpointConfig(base_url=base_url, model="fake", max_retries=1, timeout=10.0)
    return Config(
        generator=endpoint,
        verifier=EndpointConfig(base_url=base_url, model="fake-verifier", max_retries=1, timeout=10.0),
        review=ReviewConfig(max_workers=2),
        # 정적분석은 이 테스트의 관심사가 아니고, 도구 설치 여부에 따라 결과가 흔들린다.
        grounding=GroundingConfig(enabled=False),
        chunking=ChunkingConfig(),
    )


def test_end_to_end_finding_survives() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))
        _check("Grow" in diff, f"diff 가 비었다:\n{diff}")

        def emit_finding(schema: dict) -> dict:
            # 스키마가 허용하는 첫 라인과 첫 룰을 그대로 쓴다 — guided decoding 이
            # 실제로 하는 일과 같다.
            items = schema["properties"]["findings"]["items"]["properties"]
            return {
                "findings": [
                    {
                        "line": items["line"]["enum"][0],
                        "rule_id": "cpp.dangling-after-realloc",
                        "severity": "high",
                        "message": "resize() 이후 raw 포인터가 무효화된다.",
                        "suggestion": "resize 이후 &data_[0] 를 다시 얻어라.",
                    }
                ]
            }

        with FakeVLLM(on_findings=emit_finding) as server:
            result = Pipeline(_config(server.base_url)).run_diff(diff, repo)

        _check(server.schema_violations == [], f"스키마 위반: {server.schema_violations}")
        _check(result.chunks_reviewed >= 1, f"청크가 없다: {result.chunks_reviewed}")
        _check(len(result.kept) == 1, f"지적 1건을 기대했으나 {len(result.kept)}건: {result.rejected}")

        finding = result.kept[0]
        _check(finding.path == "src/buffer.cpp", f"경로: {finding.path}")
        _check(finding.rule_id == "cpp.dangling-after-realloc", f"룰: {finding.rule_id}")
        _check(finding.severity.value == "high", f"심각도: {finding.severity}")
        _check(result.errors == [], f"오류 발생: {result.errors}")


def test_schema_constrains_line_to_changed_lines() -> None:
    """생성 스키마의 line enum 이 변경된 라인만 담고 있어야 한다.

    이게 깨지면 라인 번호 환각이 생성 단계에서 막히지 않고 필터로 흘러간다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))
        captured: dict = {}

        def capture(schema: dict) -> dict:
            captured["schema"] = schema
            return {"findings": []}

        with FakeVLLM(on_findings=capture) as server:
            Pipeline(_config(server.base_url)).run_diff(diff, repo)

        items = captured["schema"]["properties"]["findings"]["items"]["properties"]
        allowed = items["line"]["enum"]
        # AFTER 에서 추가된 줄은 raw 선언(8)과 raw[0]=42(10) 이다.
        _check(allowed == [8, 10], f"허용 라인 불일치: {allowed}")
        _check("cpp.dangling-after-realloc" in items["rule_id"]["enum"], "C++ 룰이 주입되지 않음")
        _check(
            all(r.startswith(("cpp.", "any.")) for r in items["rule_id"]["enum"]),
            f"다른 언어 룰이 섞임: {items['rule_id']['enum']}",
        )


def test_rejected_finding_does_not_reach_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))

        def emit_finding(schema: dict) -> dict:
            items = schema["properties"]["findings"]["items"]["properties"]
            return {
                "findings": [
                    {
                        "line": items["line"]["enum"][0],
                        "rule_id": "cpp.dangling-after-realloc",
                        "severity": "high",
                        "message": "근거 없는 지적",
                    }
                ]
            }

        def reject(_schema: dict) -> dict:
            return {"verdict": "no", "code_present": True, "reason": "코드에 그런 문제가 없다"}

        with FakeVLLM(on_findings=emit_finding, on_verdict=reject) as server:
            result = Pipeline(_config(server.base_url)).run_diff(diff, repo)

        _check(result.kept == [], f"기각되어야 할 지적이 통과됨: {result.kept}")
        _check(len(result.rejected) == 1, f"기각 {len(result.rejected)}건")
        _check(abs(result.reject_rate - 1.0) < 1e-9, f"기각률: {result.reject_rate}")

        markdown = to_markdown(result)
        _check("지적 사항 없음" in markdown, f"기각된 지적이 리포트에 노출됨:\n{markdown}")
        _check("근거 없는 지적" not in markdown, "기각된 내용이 Markdown 에 새어나감")
        # 기각 내역은 JSON 에는 남아야 한다 — 필터 튜닝의 유일한 근거다.
        _check("근거 없는 지적" in str(result.to_dict()), "기각 내역이 JSON 에 없음")


def test_input_stays_within_token_budget() -> None:
    """프롬프트가 설정한 입력 상한을 넘지 않아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))

        with FakeVLLM() as server:
            config = _config(server.base_url)
            Pipeline(config).run_diff(diff, repo)

        from crex.llm import estimate_tokens

        for request in server.requests:
            total = sum(estimate_tokens(m["content"]) for m in request["messages"])
            _check(
                total <= config.generator.max_input_tokens,
                f"입력 예산 초과: {total} > {config.generator.max_input_tokens}",
            )


def test_sarif_output_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))

        def emit_finding(schema: dict) -> dict:
            items = schema["properties"]["findings"]["items"]["properties"]
            return {
                "findings": [{
                    "line": items["line"]["enum"][0],
                    "rule_id": "cpp.dangling-after-realloc",
                    "severity": "high",
                    "message": "무효 포인터",
                }]
            }

        with FakeVLLM(on_findings=emit_finding) as server:
            result = Pipeline(_config(server.base_url)).run_diff(diff, repo)

        sarif = to_sarif(result)
        _check(sarif["version"] == "2.1.0", "SARIF 버전 불일치")
        run = sarif["runs"][0]
        _check(len(run["results"]) == 1, f"SARIF 결과 수: {len(run['results'])}")
        entry = run["results"][0]
        _check(entry["level"] == "error", f"high → error 여야 한다: {entry['level']}")
        location = entry["locations"][0]["physicalLocation"]
        _check(location["artifactLocation"]["uri"] == "src/buffer.cpp", "SARIF 경로 불일치")
        _check(location["region"]["startLine"] == 8, f"SARIF 라인: {location['region']}")


def test_total_generation_failure_is_not_silent() -> None:
    """생성이 전부 실패하면 결과가 그렇게 말해야 한다.

    실제 사고의 회귀 테스트다. 예전에는 이 상황에서 kept=0, errors=[], 종료 코드
    0 이 나와서 "코드가 깨끗하다"와 구분되지 않았다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))

        # 생성 엔드포인트만 죽여둔다. 검증은 살아 있어도 결과는 0건이고,
        # 그 0건이 "깨끗함"으로 보고되지 않아야 한다는 것이 확인 대상이다.
        with FakeVLLM() as server:
            config = _config(server.base_url)
            config.generator.base_url = _dead_endpoint()
            result = Pipeline(config).run_diff(diff, repo)

        _check(result.chunks_reviewed >= 1, "청크는 만들어졌어야 한다")
        _check(len(result.kept) == 0, f"지적이 나오면 안 된다: {result.kept}")
        _check(result.generation_errors >= 1, "생성 실패가 집계돼야 한다")
        _check(result.errors, "실패가 errors 에 실려야 한다")
        _check(not result.healthy, "healthy 가 False 여야 한다")

        markdown = to_markdown(result)
        _check("지적 사항 없음" not in markdown, f"고장을 정상으로 보고했다:\n{markdown}")


def _dead_endpoint() -> str:
    """아무도 듣고 있지 않은 포트. 닫힌 소켓의 포트 번호를 재사용한다."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


def test_min_severity_hides_low_findings() -> None:
    """min_severity 는 유효한 지적을 '숨기는' 것이지 기각하는 게 아니다."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, diff = _make_repo(Path(tmp))

        def emit_low(schema: dict) -> dict:
            items = schema["properties"]["findings"]["items"]["properties"]
            return {
                "findings": [{
                    "line": items["line"]["enum"][0],
                    "rule_id": "cpp.unnecessary-copy",   # 택소노미상 medium
                    "severity": "low",
                    "message": "불필요한 복사",
                }]
            }

        with FakeVLLM(on_findings=emit_low) as server:
            config = _config(server.base_url)
            config.review.min_severity = "high"
            result = Pipeline(config).run_diff(diff, repo)

        _check(result.kept == [], f"low 지적이 노출됨: {result.kept}")
        # 기각된 것이 아니므로 rejected 에도 없어야 한다.
        _check(result.rejected == [], f"숨김이 기각으로 기록됨: {result.rejected}")


def test_unimplemented_mode_is_rejected_loudly() -> None:
    """구현되지 않은 모드를 조용히 무시하면 안 된다."""
    from crex.config import ReviewConfig

    try:
        ReviewConfig(mode="ocr")
    except ValueError as exc:
        _check("구현되지 않았다" in str(exc), f"오류 메시지가 불명확: {exc}")
    else:
        raise AssertionError("mode='ocr' 이 조용히 통과됨")

    try:
        ReviewConfig(min_severity="critical")
    except ValueError:
        pass
    else:
        raise AssertionError("잘못된 min_severity 가 조용히 통과됨")


TESTS = [
    test_end_to_end_finding_survives,
    test_total_generation_failure_is_not_silent,
    test_min_severity_hides_low_findings,
    test_unimplemented_mode_is_rejected_loudly,
    test_schema_constrains_line_to_changed_lines,
    test_rejected_finding_does_not_reach_report,
    test_input_stays_within_token_budget,
    test_sarif_output_shape,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crex.cli import force_utf8_output

    force_utf8_output()
    if shutil.which("git") is None:
        print("SKIP: git 이 없어 파이프라인 테스트를 건너뛴다")
        return 0

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
