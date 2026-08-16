# Design Decisions

Each entry states the decision, the reason, and what would have to be true to
revisit it. Several of these look arbitrary from the code alone.

---

## Context budget is 8,192 tokens, not 256K

Qwen3.6 and Gemma 4 both support 256K context. `crx` uses 8,192.

**Why.** Longer context measurably reduces precision on this task. In the ASE 2025
retrieval-augmented code review study, expanding retrieved examples from top-1 to
top-3 to top-5 dropped BLEU-4 monotonically: 12.32 → 11.76 → 10.81. Redundant and
conflicting signals degrade the model's focus. The same effect appears in review:
a model that pinpoints a defect when shown one function starts pointing at the
wrong place when shown the whole file.

Throughput collapses too — Qwen3.6 goes from 26 to 9 tok/s between 32K and 128K.
Gemma 4 degrades more gently (96 → 65) but still degrades.

**To revisit.** Measure FAR change on the golden set before raising it. Raising the
budget without measurement means degrading quietly.

**Implementation.** `EndpointConfig.max_input_tokens`; `truncate_to_budget()` cuts
the *middle* of over-long text, keeping head and tail — function signature and
return/cleanup are both load-bearing.

---

## Two-stage generate-then-verify, with different models

**Why.** This is the BitsAI-CR architecture (ByteDance, arXiv 2501.15134),
validated in production serving 12,000+ weekly active users. Their verification
stage rejects 55.25% of generated comments and lifts precision to 77%.

Self-verification by the same model is weak — the model just wrote the sentence it
is being asked to doubt. A different model has no such attachment.

**To revisit.** If measurement shows reject rate consistently below 40%, the
verifier is rubber-stamping; check that it is actually a different model and that
guided decoding is applied.

**Fallback.** Omitting `[llm.verifier]` reuses the generator endpoint. The
deterministic checks still work; cross-model benefit is lost.

---

## Conclusion-First property order

`VERDICT_SCHEMA` lists `verdict` before `code_present` before `reason`.

**Why.** Under guided decoding, JSON Schema property order determines generation
order. BitsAI-CR compared three patterns — Direct Conclusion, Reasoning-First
(chain-of-thought before the decision), and Conclusion-First (decision token then
rationale) — and shipped Conclusion-First: 77.09% precision, 1.7s per sample.

Reordering makes latency *and* accuracy worse simultaneously.

---

## Guided decoding used for hallucination prevention, not JSON validity

The conventional use of structured output is "stop the model emitting malformed
JSON." Here the `enum` constraints carry the anti-hallucination weight:

- `line` enum = exactly the chunk's changed line numbers
- `rule_id` enum = exactly the taxonomy IDs for that language

The model physically cannot emit line 11 when the enum is `[8, 10]`. This converts
line-number hallucination from a filtering problem into an impossibility.

**Fragility.** If `structured_output_mode` is misconfigured, vLLM ignores the
constraint and two of four defense layers vanish silently. `doctor` plus an actual
review run is the only way to confirm. This is documented prominently in
`docs/troubleshooting.md` because it degrades quietly rather than failing loudly.

---

## Expansion cap of 4× / 3×

Hunk expansion stops at 4× the hunk size and truncates to 3×.

**Why these numbers.** They are BitsAI-CR's production values. Not tuned here —
adopted from a system with measured results.

**To revisit.** Golden-set measurement. Larger chunks cost tokens and precision;
smaller chunks cost context the model needs to judge correctly.

---

## No agent loop

`alibaba/open-code-review` and similar tools give the model `file_read`,
`code_search`, and let it explore. That works at frontier scale.

**Why not here.** At 25–40B, tool-call failures and context blowup arrive together.
The model opens a file, searches, opens another, exhausts context, then produces
findings from a degraded state. Failure is also hard to attribute.

Fixed steps are slower but predictable and debuggable: one chunk, one prompt, one
list of findings.

**Evidence available.** Reports indicate Qwen3-Coder 30B / Gemma 4 27B class models
*do* emit clean tool calls (unlike sub-7B models). So this is a defensible bet, not
a certainty. Phase 1 measures OCR against native mode on the same golden set — that
comparison decides it.

---

## Static analysis before the LLM, and the prompt reframe

Analyzers run first and their findings go into the prompt under
`## 정적분석 도구 결과`. The system prompt then says the model's job is to
(1) judge whether the reported items are real, and (2) add only what tools
structurally cannot catch.

**Why.** "Find defects" invites invention when there is nothing to find. "Verify
these and add what tools miss" gives the model a grounded starting point. A
meaningful share of baseless findings disappears at prompt level rather than being
filtered later.

---

## Fail loudly on diff/source mismatch

`on_mismatch = "raise"` is the default; the affected file is skipped.

**Why.** If the working tree changed after the diff was produced, every line number
shifts. Reviews then confidently cite lines that don't exist — precisely the failure
the annotation scheme exists to prevent. Silent wrongness is worse than a skipped file.

This check was added mid-development after it caught two hand-written test fixtures
with off-by-one hunk headers. That is also why pipeline tests now build a real
temporary git repo and use real `git diff` output rather than hand-written diffs.

---

## Precision over recall, stated explicitly

`ReviewFilter` rejects on verification failure. `_severity_of()` caps severity.
`max_findings_per_chunk` is 5. Rules carry a `counter` field listing what *not* to
flag. `min_severity` exists so teams can start at `high` only.

**Why.** A review tool with a high false-alarm rate stops being read within about
three weeks, and at that point recall is irrelevant. Accepted target is FAR ≤ 25%,
matching BitsAI-CR's ~75% precision. OCR makes the same trade ("sacrifices recall
for accuracy").

Phase 3 acceptance explicitly allows KBI to drop to 90% of baseline in exchange.

---

## `chunk_local` flag on every rule

Rules marked `chunk_local = false` are excluded from the default profile.

**Why.** A rule the model cannot decide from the snippet alone — "calls a
deprecated internal API," "the caller may pass null" — forces invention. Small
models are bad at answering "I don't know"; given a rule with no decidable basis,
they manufacture a basis.

Test for a new rule: *could a new hire, handed only this function on paper, answer
whether the rule is violated?* If no, it is not chunk-local.

---

## Rule IDs are immutable join keys

Rule IDs connect evaluation reports, per-rule precision statistics, and flywheel
records across time. Renaming one severs that rule's history exactly when three
months of data would let you decide whether to retire it.

The ID prefix must match the `language` field (`any.` for cross-language rules) or
per-language statistics blend together. `python -m crx.rules` rejects duplicates.

To rename: add under the new ID, delete the old one. Don't mutate.

---

## Dependencies confined to the MCP layer

The core uses stdlib only:

- HTTP: `urllib.request`, not httpx/requests
- Models: `dataclasses`, not pydantic
- Config: `tomllib` (stdlib since 3.11)
- Tests: custom runner, not pytest

**Why.** Importing a wheel into an air-gapped corporate network requires security
review, transitive dependency tracing, and repetition on every version bump. The
cost is real and recurring.

**What changed.** The user asked for FastMCP and said they would provide the
runtime, so `requirements.txt` now exists. Two properties were preserved rather
than dropping the principle wholesale:

| Dependency | Status | Fallback |
|---|---|---|
| FastMCP | required by `crx/mcp.py` only | none — but CLI and tests don't need it |
| GitPython | preferred in `crx/gitio.py` | subprocess, same diff text |
| tree-sitter | optional in `crx/chunk.py` | brace/indent heuristic |

`crx/service.py` was split out of `crx/mcp.py` specifically so the review logic
imports no FastMCP and stays testable without it. `python -m crx review|scan|doctor`
and `python tests/run_all.py` still run on a bare interpreter — post-transfer
integrity verification cannot presuppose a working `pip install`.

FastMCP earned the exception because MCP is a moving spec and hand-rolling it means
tracking that movement forever. GitPython did not clear the same bar, hence the
fallback: the subprocess path is ten lines and never breaks.

Token counting is a `len(text) / 3.0` estimate rather than a real tokenizer. Exact
counting would require importing `transformers`. At an 8,192 budget against a 32K
window, a 20% estimation error is harmless.

---

## Korean for prompts, comments, and user docs

The user is a Korean-speaking engineer and findings are read by Korean developers.
Qwen3.6 (201 languages) and Gemma 4 are both strongly multilingual, so Korean
prompts do not cost measurable instruction-following quality at this scale.

This wiki is English because it is written for agents.

---

## Research sources

| Source | Used for |
|---|---|
| [BitsAI-CR](https://arxiv.org/abs/2501.15134) (ByteDance, production) | Two-stage pipeline, Conclusion-First, 4×/3× expansion, rule taxonomy shape, Outdated Rate |
| [Towards Practical Defect-Focused Automated Code Review](https://arxiv.org/abs/2505.17928) (ICML 2025 Spotlight) | Industrial C++ codebases, code slicing, false-alarm filtering |
| [When More Retrieval Hurts](https://arxiv.org/html/2511.05302v2) | top-1 > top-k; context expansion degrades quality |
| [LAURA](https://arxiv.org/abs/2512.01356) (ASE 2025) | Context augmentation, review exemplar retrieval |
| [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | Hybrid deterministic+LLM architecture, smart bundling, rule.json format |
