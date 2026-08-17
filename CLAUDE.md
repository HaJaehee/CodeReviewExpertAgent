# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**CREX** (Code Review EXpert) — an AI code review pipeline for C++/C#/Python that
runs entirely inside a corporate **air-gapped network** against **small local LLMs**
(25–40B: Qwen3.6-27B for generation, Gemma 4 26B for verification) served by vLLM.

The Python package, CLI, and config file are lowercase `crex`; `CREX` is the name.

Every design choice serves one goal: **suppressing hallucination**. Small models
confidently invent line numbers, APIs, and defects. A review tool with a high
false-alarm rate gets ignored within three weeks, so this project trades recall for
precision on purpose. Target is FAR ≤ 25%.

## Read first

[`wiki/`](wiki/README.md) is written for agents, in English:

- [`wiki/architecture.md`](wiki/architecture.md) — pipeline, data flow, module map
- [`wiki/design-decisions.md`](wiki/design-decisions.md) — why each choice, with research basis
- [`wiki/invariants.md`](wiki/invariants.md) — **do not break these**
- [`wiki/roadmap.md`](wiki/roadmap.md) — what exists, what doesn't, what's next

[`docs/`](docs/index.md) is the Korean end-user manual. Don't duplicate it in the wiki.
[`docs/workflow.md`](docs/workflow.md) is the MCP/Zed usage flow — which tool gets
called by which phrasing, and what to do with a finding.
[`docs/visualizer.md`](docs/visualizer.md) is the dashboard manual. Maintainer-only work
(golden set, rule tuning) is a short section at the end pointing elsewhere.

## Language conventions

Do not "clean these up" by translating.

| Where | Language |
|---|---|
| Code comments, docstrings | **Korean** |
| LLM prompt templates | **Korean** |
| Log messages, CLI output, errors | **Korean** |
| `docs/` user manual | **Korean** |
| `wiki/`, this file | English |
| Identifiers, type names, rule IDs | English |

The user is a Korean-speaking engineer. Match the surrounding comment style:
direct, explaining *why* rather than restating *what*, willing to state trade-offs
plainly.

## Commands

```bash
python tests/run_all.py                     # 79 tests, no LLM or network needed
```

```bash
python -m crex doctor                        # endpoints, analyzers, tree-sitter status
```

```bash
python -m crex review --from main --to HEAD  # diff review
python -m crex review --staged --out reports/
python -m crex scan src/legacy.cpp           # whole-file audit, no diff
```

```bash
python -m crex.mcp  # MCP stdio server (Zed context_servers)
                    # needs `pip install -r requirements.txt`
```

```bash
python -m crex.viz  # pipeline dashboard, http://127.0.0.1:8765
                    # uvicorn if installed, stdlib otherwise
```

```bash
python -m crex.rules                                   # validate taxonomy
python -m crex.rules --out .opencodereview/rule.json   # emit OCR rule file
```

```bash
python eval/run_eval.py init
python eval/run_eval.py run --out reports/phase-1.json
python eval/run_eval.py compare reports/phase-1.json reports/phase-3.json
```

Exit code 1 from `review` means at least one `high` severity finding.

## Architecture in one screen

```
git diff → chunk → ground → generate → filter → report
```

- **chunk** (`crex/chunk.py`) — parse diff, expand each hunk to the enclosing symbol
  (capped at 4× hunk size, truncated to 3×), annotate every line as
  `[added @142]`. Verifies diff matches on-disk source and skips the file if not.
- **ground** (`crex/ground.py`) — run static analyzers in parallel (6 by default, 8 available); missing tools
  skip silently. Findings go into the prompt so the LLM's job becomes *verify these
  and add what tools can't catch*, not *find defects*.
- **generate** (`crex/generate.py`) — one LLM call per chunk with a JSON Schema whose
  `enum`s restrict `line` to the chunk's changed lines and `rule_id` to the taxonomy.
- **filter** (`crex/filter.py`) — deterministic checks first (no LLM call), then a
  *different* model returns yes/no in Conclusion-First order.
- **report** (`crex/report.py`) — Markdown / SARIF 2.1.0 / JSON.

`crex/service.py` + `crex/mcp.py` expose the same pipeline to Zed's agent panel over
MCP. All logic lives in `service.py` (no FastMCP import, fully testable); `mcp.py`
is a thin FastMCP binding. 5 tools, returning a **compact summary**, not the full report —
tool results land in the agent's context, and the whole design is about spending
context carefully. Full reports go to disk. The agent decides *when* to review;
the pipeline still decides *how*.

`crex/viz/` is a 3-tier dashboard over the same pipeline: engine (`trace.py`,
`engine.py`) subclasses `Pipeline`/`LLMClient` to emit events, application
(`api.py`, `server.py`) is a transport-agnostic router with uvicorn and stdlib
backends, presentation (`web/`) is dependency-free HTML/CSS/JS storing history in
localStorage. It exists because MCP returns only a summary, and prompt tuning needs
the inside. **Instrumentation must never change results** — pinned by a test that
compares `Pipeline` and `TracedPipeline` on the same diff.

Four layers of defense: line annotations remove the need to infer line numbers;
enum constraints make fabrication impossible at generation time; deterministic
checks catch anything that slips; cross-model verification catches ungrounded claims.

## Critical invariants

Full list in [`wiki/invariants.md`](wiki/invariants.md). The ones most easily broken:

- **`VERDICT_SCHEMA` property order** — `verdict` must stay first. Schema order is
  generation order under guided decoding; reordering silently costs accuracy and speed.
- **`line` enum = the chunk's changed lines** — this is what makes line-number
  hallucination impossible rather than merely filtered.
- **Rule IDs never change** — they join evaluation reports and flywheel statistics
  across months. Add new, delete old; never rename.
- **Verification failure rejects** — never fail-open.
- **`on_mismatch` default stays `"raise"`** — a line-shifted review is entirely wrong.
- **Config rejects unknown keys** — a silently ignored typo means a setting that
  appears not to work.
- **Core stays dependency-free** — only `crex/mcp.py` may require a wheel (FastMCP).
  `python -m crex review|scan|doctor`, `python -m crex.viz`, and `tests/run_all.py`
  must work with nothing installed; each wheel costs a security review on every
  air-gapped transfer. The dashboard's front end loads no CDN, font, or framework
  for the same reason — in an air-gapped browser those hang rather than fail.
- **Instrumentation must not change results** — `crex/viz/` observes the pipeline by
  subclassing it, never by re-implementing it.
- **Tests run without network, LLM, or pip install** — `tests/fake_vllm.py` stands
  in for vLLM; FastMCP-dependent tests skip cleanly. This is how post-transfer
  integrity gets verified inside the network.

## Working style in this repo

**Verify claims against code.** Documentation drift is a real failure here.
`docs/` was cross-checked against the source (config keys, analyzer names, enum
values, defaults, rule counts, anchor links) and two dead settings were found and
fixed during that pass. Do the same for anything you add.

**Don't hand-write diffs in tests.** Two hand-written fixtures were wrong during
development. `tests/test_pipeline.py` builds a real temporary git repo and uses real
`git diff` output.

**Rules go in one at a time.** Add a rule, run the golden set, compare. Batching
five means you can't tell which one raised FAR. See
[`docs/writing-rules.md`](docs/writing-rules.md).

**Don't add settings that do nothing.** `min_severity` and `review.mode` were
declared but never read; both were made real (filtering, and validation that
rejects unimplemented modes). A dead setting is worse than a missing one.

## Current state

Working and tested: chunking, grounding, generation, filtering, reporting, CLI,
evaluation harness, MCP server, visualizer. 41 rules. 79 tests passing.
~5,377 lines of Python in `crex/`, plus ~1,800 lines of front end in `crex/viz/web/`.

**Not yet true, and load-bearing:**

- **No golden set exists.** `eval/golden/` is empty. Blocking for any tuning claim —
  needs 50–100 labeled MRs from the user's history, which only they can produce.
- **Never run against a real LLM.** All verification is against a fake vLLM server.
  Prompt quality, actual reject rates, and latency are unmeasured. Quality numbers in
  the docs are targets drawn from literature, not observations from this system.
- **`review.mode = "ocr"` raises.** OCR delegation is Phase 1 work, pending
  inspection of the real binary's output schema.
- **Outdated Rate not implemented.** Phase 4 flywheel metric.

The immediate next step is Phase 0: build the golden set. Everything downstream
depends on being able to measure.

## Relationship to alibaba/open-code-review

The approved plan made OCR the base and layered grounding + filter on top. What was
built is the native pipeline first — which was the plan's own designated fallback —
because OCR's output schema isn't publicly documented and guessing at a parser would
be fabrication.

Phase 1 is unchanged: import OCR, measure both on the golden set, decide. Grounding
and ReviewFilter are reusable either way. Nothing here forecloses adopting OCR.
