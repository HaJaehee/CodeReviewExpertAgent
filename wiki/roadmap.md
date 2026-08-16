# State and Roadmap

## Origin

The user asked three questions and requested deep research before a decision:

1. Install an existing open-source coding app and use its built-in skills?
2. Does a code-review-specific MCP server exist?
3. Build an MCP server tuned to requirements?

Research conclusions:

1. **Yes, as a starting point** — `alibaba/open-code-review` (Apache-2.0, 20.5k★,
   Go single binary, hybrid deterministic + LLM agent, OpenAI-compatible, C/C++ and
   Python built-in rules, **no C# rules**). Qodo PR-Agent has unresolved local-model
   config bugs; Continue.dev was acquired by Cursor; Roo Code was archived May 2026.
2. **No usable general review MCP** — most are cloud-model orchestration wrappers,
   useless air-gapped. 66% of scanned MCP servers have security findings. More
   decisively, attaching many MCP servers eats context and degrades tool-call
   accuracy at 25–40B. Only "fact-supplying" servers (clangd-mcp, Semgrep MCP,
   offline `codesearch`) are worth considering, and later.
3. **Not a full build — two thin adapters** — static-analysis grounding and a
   ReviewFilter. Neither exists in any off-the-shelf tool, and they are the core of
   hallucination control.

Confirmed constraints: MR/PR diff review plus on-demand CLI; vLLM serving; git only
with no CI platform integration; 1–2 part-time engineers wanting fast results.

## Deviation from the approved plan

The plan sequenced OCR adoption first (Phase 1), with a non-agentic native pipeline
as the Plan B fallback if agent tool-calling proved unreliable at 27B.

**What was actually built: the native pipeline first, complete, plus all of Phase 3.**

Two reasons:

1. OCR's review-output JSON schema is not in public documentation. Writing a parser
   from outside the air-gapped network would be guesswork, and a guessed parser is
   worse than none.
2. OCR depends on the agent tool-call loop. Whether that holds at 27B was the plan's
   own Phase 1 gate, and the native pipeline was the designated fallback. It now
   exists and is verifiable.

Phase 1 work is unchanged: import OCR, measure it on the golden set, compare against
native. `.opencodereview/rule.json` generation already works, so OCR rule
configuration is ready. Grounding and ReviewFilter are reusable either way.

## What exists

| Component | State |
|---|---|
| Diff parsing, chunking, symbol expansion | Complete, 6 tests |
| tree-sitter integration + heuristic fallback | Complete, fallback verified (tree-sitter not installed locally) |
| Diff/source consistency check | Complete |
|  8 static-analysis adapters | Complete, 11 parser tests against real output formats |
| RuleChecker with enum-constrained schema | Complete |
| ReviewFilter, deterministic + cross-model | Complete, 9 tests |
| Rule taxonomy | 41 rules (C++ 14, C# 15, Python 14) |
| OCR `rule.json` generator | Complete |
| Pipeline: `run_diff` / `run_scan` | Complete, 5 end-to-end tests via fake vLLM |
| Markdown / SARIF / JSON output | Complete |
| CLI: `review` / `scan` / `doctor` | Complete |
| `ReviewService` (5 MCP operations) | Complete, 13 tests, no FastMCP needed |
| FastMCP binding (`mcp.py`) | Written; binding test skips when FastMCP absent |
| git access (GitPython + subprocess fallback) | Complete |
| Path expansion + diff path filtering | Complete |
| Golden-set evaluation harness | Complete, 7 metric tests |
| Korean user manual (`docs/`, 7 files) | Complete, claims cross-checked against code |

64 tests total, all runnable offline.

## What does not exist

**Golden set.** Empty. This is the blocking item — 50–100 labeled MRs are required
before any tuning claim can be evaluated. Only the user can produce it; it needs
their MR history. `python eval/run_eval.py init` scaffolds the structure.

**No run against a real LLM.** Everything is verified against `tests/fake_vllm.py`.
The pipeline has never touched actual Qwen3.6 or Gemma 4. Prompt quality, real
reject rates, and latency are all unmeasured. Treat every quality number in the docs
as a target from literature, not an observed result.

**Verified FastMCP binding.** `crx/mcp.py` was written against the documented
FastMCP API but never executed — the library is not installed on the development
machine. `ReviewService` beneath it is fully tested. First run inside the network
should be `python -m crx.mcp` plus a Zed connection check.

**OCR comparison.** `review.mode = "ocr"` raises rather than running.

**Outdated Rate.** The flywheel metric — whether flagged lines get modified in later
commits. Documented in `docs/evaluation.md` as a Phase 4 item and explicitly marked
as not implemented.

**Path-scoped rules in native mode.** OCR's `rule.json` supports per-glob rules;
native mode applies rules by language only. Workaround is the rule's `counter` field.

**LoRA fine-tuning.** Phase 5, requires ~10k accumulated samples.

## Phase plan

**Phase 0 — golden set (1 week).** 50–100 past MRs, defects only (not praise,
questions, or style comments), 20–30% clean MRs to measure FAR. Needs a repo
snapshot per case (git worktree) because `crx` reads source from disk, not just
the diff.

**Phase 1 — baseline (1–2 weeks).** Import OCR, disable its telemetry, block egress
at network level. Measure both OCR and native on the golden set. Decide.

**Phase 2 — rules (2–3 weeks).** Grow the taxonomy one rule at a time, re-measuring
after each. Any rule that raises FAR gets removed or its `counter` strengthened.
C# needs the most work — OCR has no built-in C# rules.

**Phase 3 — already built.** Grounding and ReviewFilter exist. Remaining work is
tuning against real measurements.

**Phase 4 — flywheel.** Outdated Rate, thumbs up/down collection, biweekly per-rule
scorecards. Retire rules below ~65% precision / ~25% outdated rate.

## Acceptance criteria

| Phase | KBI | FAR | Latency/chunk |
|---|---|---|---|
| 1 baseline | measure | measure | measure |
| 2 rules | ≥ baseline | ≤ baseline | — |
| 3 filter | ≥ 90% of baseline | **≤ 25%** | ≤ 10s |

KBI dropping in Phase 3 is intended. The filter trades recall for precision.

## Likely first questions

**"Why is the reject rate 100%?"** Usually a dead verifier endpoint (all
`FILTER_ERROR`) or guided decoding not applied so line numbers are arbitrary
(all `LINE_OUT_OF_RANGE`). Check the JSON report's `rejected` array.

**"Can we raise `max_input_tokens`?"** Only with golden-set measurement. See
[design-decisions.md](design-decisions.md#context-budget-is-8192-tokens-not-256k).

**"Should we add more rules?"** Per-chunk rules are capped at 15. All three
languages currently sit at or under that, so new rules displace existing ones by
severity ordering. Check `python -m crx.rules` output — a gap between "전체" and
"활성" means prune before adding.

**"Why not just use OCR?"** Possibly you should. That is exactly the Phase 1
measurement. Nothing here forecloses it.
