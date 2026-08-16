"""골든셋 평가 하네스 (Phase 0).

측정 없이는 아무것도 개선할 수 없다. 룰을 하나 추가했을 때 좋아졌는지 나빠졌는지,
필터가 실제로 도움이 되는지 — 전부 이 스크립트가 답한다.

## 지표

- **KBI** (Key Bug Inclusion) — 실제 결함 중 잡아낸 비율. 재현율.
- **FAR** (False Alarm Rate) — 내보낸 지적 중 오탐 비율. **최우선 관리 지표**.
  오탐이 많은 리뷰 도구는 3주 안에 무시당한다. 재현율보다 중요하다.
- **룰별 정밀도** — Phase 4 플라이휠에서 룰을 취사선택하는 근거.

## 사용법

    python eval/run_eval.py init                       # 골든셋 뼈대 생성
    python eval/run_eval.py run --out reports/phase-1.json
    python eval/run_eval.py compare reports/phase-1.json reports/phase-3.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crx.config import load_config  # noqa: E402
from crx.pipeline import Pipeline  # noqa: E402
from crx.schema import Finding, ReviewResult  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

#: 매칭 허용 오차(라인). 리뷰어는 결함 라인을 정확히 짚지 않고 근처에 코멘트한다.
#: 너무 넓히면 FAR 이 낙관적으로 나오므로 3줄 이상 늘리지 말 것.
LINE_TOLERANCE = 3


@dataclass
class GoldenDefect:
    path: str
    line: int
    category: str = "code_defect"
    severity: str = "high"
    comment: str = ""
    #: 이 결함에 대응한다고 보는 룰 ID (선택). 룰별 재현율 분석에 쓴다.
    expected_rule: str | None = None

    def matches(self, finding: Finding, tolerance: int = LINE_TOLERANCE) -> bool:
        return finding.path == self.path and abs(finding.line - self.line) <= tolerance


@dataclass
class GoldenCase:
    case_id: str
    repo_path: str
    diff_path: str
    defects: list[GoldenDefect] = field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict, base_dir: Path) -> "GoldenCase":
        try:
            return cls(
                case_id=data["case_id"],
                repo_path=data["repo_path"],
                diff_path=data["diff_path"],
                defects=[GoldenDefect(**d) for d in data.get("defects", [])],
                note=data.get("note", ""),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{base_dir} 의 케이스가 잘못되었다: {exc}") from exc


@dataclass
class CaseScore:
    case_id: str
    matched: int = 0
    total_defects: int = 0
    emitted: int = 0
    false_alarms: int = 0
    raw_emitted: int = 0
    reject_rate: float = 0.0
    duration: float = 0.0
    error: str = ""


def load_golden(directory: Path = GOLDEN_DIR) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    for path in sorted(directory.glob("*.jsonl")):
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            raw = raw.strip()
            if not raw or raw.startswith("//"):
                continue
            try:
                cases.append(GoldenCase.from_dict(json.loads(raw), path))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{lineno} — {exc}") from exc
    return cases


def score_case(case: GoldenCase, result: ReviewResult) -> CaseScore:
    """지적과 골든 결함을 매칭한다.

    한 지적이 여러 결함에 매칭될 수 있으나, 결함 하나는 한 번만 센다 —
    같은 결함을 두 번 지적했다고 재현율이 오르면 안 된다.
    """
    score = CaseScore(
        case_id=case.case_id,
        total_defects=len(case.defects),
        emitted=len(result.kept),
        raw_emitted=result.total_raw,
        reject_rate=result.reject_rate,
        duration=sum(result.timings.values()),
    )

    unmatched_findings = list(result.kept)
    for defect in case.defects:
        hit = next((f for f in unmatched_findings if defect.matches(f)), None)
        if hit is not None:
            score.matched += 1
            unmatched_findings.remove(hit)

    # 어떤 결함과도 매칭되지 않은 지적 = 오탐.
    score.false_alarms = sum(
        1 for f in result.kept if not any(d.matches(f) for d in case.defects)
    )
    return score


@dataclass
class Report:
    scores: list[CaseScore] = field(default_factory=list)
    rule_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def kbi(self) -> float:
        total = sum(s.total_defects for s in self.scores)
        return sum(s.matched for s in self.scores) / total if total else 0.0

    @property
    def far(self) -> float:
        emitted = sum(s.emitted for s in self.scores)
        return sum(s.false_alarms for s in self.scores) / emitted if emitted else 0.0

    @property
    def mean_reject_rate(self) -> float:
        rates = [s.reject_rate for s in self.scores if s.raw_emitted]
        return sum(rates) / len(rates) if rates else 0.0

    @property
    def mean_duration(self) -> float:
        durations = [s.duration for s in self.scores if not s.error]
        return sum(durations) / len(durations) if durations else 0.0

    def to_dict(self) -> dict:
        return {
            "summary": {
                "cases": len(self.scores),
                "kbi": round(self.kbi, 4),
                "far": round(self.far, 4),
                "mean_reject_rate": round(self.mean_reject_rate, 4),
                "mean_duration_sec": round(self.mean_duration, 2),
                "total_defects": sum(s.total_defects for s in self.scores),
                "total_emitted": sum(s.emitted for s in self.scores),
                "total_matched": sum(s.matched for s in self.scores),
                "total_false_alarms": sum(s.false_alarms for s in self.scores),
                "errors": sum(1 for s in self.scores if s.error),
            },
            "rules": {
                rule: {
                    **stats,
                    "precision": round(
                        stats["matched"] / stats["emitted"] if stats["emitted"] else 0.0, 4
                    ),
                }
                for rule, stats in sorted(self.rule_stats.items())
            },
            "cases": [vars(s) for s in self.scores],
        }

    def render(self) -> str:
        summary = self.to_dict()["summary"]
        lines = [
            "=" * 62,
            f"케이스 {summary['cases']}개 / 골든 결함 {summary['total_defects']}건",
            "",
            f"  KBI (재현율)   {self.kbi:6.1%}   "
            f"({summary['total_matched']}/{summary['total_defects']} 결함 탐지)",
            f"  FAR (오탐률)   {self.far:6.1%}   "
            f"({summary['total_false_alarms']}/{summary['total_emitted']} 지적이 오탐)",
            f"  필터 기각률    {self.mean_reject_rate:6.1%}   (정상 범위 40~60%)",
            f"  평균 소요      {self.mean_duration:6.1f}s",
        ]
        if summary["errors"]:
            lines.append(f"  오류           {summary['errors']}건")

        if self.rule_stats:
            lines.extend(["", "룰별 정밀도 (낮은 순 — 아래쪽부터 폐기 후보):"])
            ranked = sorted(
                self.to_dict()["rules"].items(), key=lambda kv: kv[1]["precision"]
            )
            for rule, stats in ranked:
                if stats["emitted"] == 0:
                    continue
                lines.append(
                    f"  {stats['precision']:6.1%}  {rule:38s} "
                    f"({stats['matched']}/{stats['emitted']})"
                )
        lines.append("=" * 62)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 명령
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / "diffs").mkdir(exist_ok=True)

    # `.sample` 확장자로 둔다 — load_golden 의 *.jsonl glob 에 걸려 실제 케이스로
    # 실행되면 존재하지 않는 저장소를 가리켜 매번 실패한다.
    template = GOLDEN_DIR / "example.jsonl.sample"
    if template.exists() and not args.force:
        print(f"{template} 가 이미 있다. 덮어쓰려면 --force")
        return 0

    sample = {
        "case_id": "mr-1234",
        "repo_path": "D:/work/myrepo",
        "diff_path": "eval/golden/diffs/mr-1234.diff",
        "note": "리뷰어가 raw 포인터 무효화를 지적한 건",
        "defects": [
            {
                "path": "src/buffer.cpp",
                "line": 16,
                "category": "code_defect",
                "severity": "high",
                "comment": "resize 이후 raw 포인터가 무효화됨",
                "expected_rule": "cpp.dangling-after-realloc",
            }
        ],
    }
    template.write_text(json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{template} 생성 완료.\n")
    print("라벨링 절차:")
    print("  1. 과거 MR 50~100건을 고른다 (C++/C#/Python 균등하게)")
    print("  2. 각 MR 의 diff 를 eval/golden/diffs/<case_id>.diff 로 저장한다")
    print("     git diff <base> <head> > eval/golden/diffs/mr-1234.diff")
    print("  3. 리뷰어가 실제로 지적한 결함만 defects 에 적는다")
    print("     — 칭찬·질문·스타일 코멘트는 제외한다. 결함만이다.")
    print("  4. 해당 커밋을 체크아웃한 상태의 저장소 경로를 repo_path 에 적는다")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        cases = load_golden(args.golden)
    except ValueError as exc:
        print(f"골든셋 오류: {exc}", file=sys.stderr)
        return 2

    if not cases:
        print(f"{args.golden} 에 케이스가 없다. `python eval/run_eval.py init` 부터 실행하라.")
        return 2

    config = load_config(args.config)
    pipeline = Pipeline(config)
    report = Report()

    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case.case_id} ...", end=" ", flush=True)
        diff_file = Path(case.diff_path)
        if not diff_file.is_absolute():
            diff_file = Path.cwd() / diff_file

        try:
            diff_text = diff_file.read_text(encoding="utf-8", errors="replace")
            result = pipeline.run_diff(diff_text, Path(case.repo_path))
        except Exception as exc:  # noqa: BLE001 - 한 케이스 실패로 전체를 멈추지 않는다
            print(f"실패: {exc}")
            report.scores.append(
                CaseScore(case_id=case.case_id, total_defects=len(case.defects), error=str(exc))
            )
            continue

        score = score_case(case, result)
        report.scores.append(score)
        _accumulate_rules(report, case, result)
        print(f"결함 {score.matched}/{score.total_defects} 탐지, 오탐 {score.false_alarms}")

    print()
    print(report.render())

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n{args.out} 저장 완료")

    if args.max_far is not None and report.far > args.max_far:
        print(f"\nFAR {report.far:.1%} 가 상한 {args.max_far:.1%} 를 초과했다.", file=sys.stderr)
        return 1
    return 0


def _accumulate_rules(report: Report, case: GoldenCase, result: ReviewResult) -> None:
    for finding in result.kept:
        stats = report.rule_stats.setdefault(finding.rule_id, {"emitted": 0, "matched": 0})
        stats["emitted"] += 1
        if any(d.matches(finding) for d in case.defects):
            stats["matched"] += 1


def cmd_compare(args: argparse.Namespace) -> int:
    before = json.loads(args.before.read_text(encoding="utf-8"))["summary"]
    after = json.loads(args.after.read_text(encoding="utf-8"))["summary"]

    print(f"{args.before.name}  →  {args.after.name}\n")
    rows = [
        ("KBI (재현율)", before["kbi"], after["kbi"], True),
        ("FAR (오탐률)", before["far"], after["far"], False),
        ("필터 기각률", before["mean_reject_rate"], after["mean_reject_rate"], None),
    ]
    for label, old, new, higher_is_better in rows:
        delta = new - old
        if higher_is_better is None:
            mark = " "
        else:
            improved = delta > 0 if higher_is_better else delta < 0
            mark = "+" if improved else ("-" if abs(delta) > 1e-9 else " ")
        print(f"  {mark} {label:14s} {old:6.1%} → {new:6.1%}  ({delta:+.1%})")

    print(
        f"\n    {'평균 소요':14s} {before['mean_duration_sec']:6.1f}s → "
        f"{after['mean_duration_sec']:6.1f}s"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python eval/run_eval.py", description="골든셋 평가")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="골든셋 디렉터리와 템플릿을 만든다")
    init.add_argument("--force", action="store_true")

    run = sub.add_parser("run", help="골든셋 전체를 평가한다")
    run.add_argument("--golden", type=Path, default=GOLDEN_DIR)
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--out", type=Path, default=None)
    run.add_argument(
        "--max-far", type=float, default=None,
        help="FAR 상한. 초과하면 종료 코드 1 (예: 0.25)",
    )

    compare = sub.add_parser("compare", help="두 리포트를 비교한다")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)

    args = parser.parse_args(argv)
    return {"init": cmd_init, "run": cmd_run, "compare": cmd_compare}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
