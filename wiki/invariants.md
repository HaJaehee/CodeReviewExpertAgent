# Invariants

Things that look incidental but are load-bearing. Breaking any of these degrades
the system quietly — tests may still pass, reviews still produce output, and the
output is worse.

---

## Conclusion-First property order

**`VERDICT_SCHEMA` in `crx/filter.py` must list `verdict` first.**

```python
VERDICT_SCHEMA = {
    "properties": {
        "verdict": {...},        # ← must stay first
        "code_present": {...},
        "reason": {...},
    },
}
```

Under guided decoding, JSON Schema property order determines token generation
order. Putting `reason` first turns this into Reasoning-First, which BitsAI-CR
measured as both slower and less accurate. Nothing will fail; it will just get worse.

---

## The `line` enum must contain only the chunk's changed lines

In `crx/generate.py :: build_findings_schema()`:

```python
line_schema = {"type": "integer", "enum": allowed_lines}
```

This is the mechanism that makes line-number hallucination impossible at generation
time. Replacing it with an unconstrained `{"type": "integer"}` moves the burden
entirely onto the filter, which catches most but not all of it.

`test_schema_constrains_line_to_changed_lines` in `tests/test_pipeline.py` pins
this. If you change chunking and that test fails, fix chunking — don't relax the test.

In `scan` mode (`restrict_lines=False`) the enum is deliberately absent, because
there is no "changed line" concept. Scan mode is correspondingly noisier.

---

## `rule_id` enum must come from the taxonomy

Same function. The model can only emit rule IDs that exist. `RuleChecker._parse()`
additionally drops unknown IDs as a second guard for environments where guided
decoding is off.

---

## Rule IDs never change

`rules/taxonomy.toml`. IDs join evaluation reports, per-rule precision, and flywheel
records across months. Renaming severs history.

The prefix must match the `language` field — `any.hardcoded-secret` for
`language = "any"`, not `python.hardcoded-secret`. Otherwise per-language statistics
blend. `load_taxonomy()` raises on duplicate IDs for the same reason.

To rename: add new, delete old.

---

## `RejectReason` values are append-only

`crx/schema.py`. Reject reasons are the tuning evidence for the filter. Changing
what an existing value means breaks comparison with past reports. Add new members;
don't repurpose existing ones.

---

## Verification failure must reject, not pass

`crx/filter.py :: _verify_llm()` returns `kept=False` with
`RejectReason.FILTER_ERROR` when the LLM call raises.

Flipping this to fail-open would let unverified findings ship whenever the verifier
endpoint hiccups — the loudest possible violation of the precision-first principle.
Pinned by `test_llm_error_rejects_conservatively`.

---

## `code_present: false` outranks `verdict: "yes"`

Order of checks in `_verify_llm()` matters:

```python
if not response.get("code_present", True):  # checked first
    ... CODE_NOT_FOUND
if response.get("verdict") != "yes":
    ... VERDICT_NO
```

If the verifier says the described code isn't in the snippet, that is an
unambiguous hallucination signal, and a `yes` alongside it means the verifier
itself wavered. Pinned by `test_code_not_present_rejects_even_if_verdict_yes`.

---

## Severity may only be lowered, never raised

`crx/generate.py :: _severity_of()` caps the model's reported severity at the
taxonomy value. Left free, small models mark everything `high` and priority
ordering becomes meaningless.

---

## Line annotation format is `[status @lineno] text`

`crx/schema.py :: DiffLine.annotate()`. The filter's deterministic line checks and
the whole prompt contract depend on this shape. `test_line_annotation_is_unambiguous`
pins it.

---

## `on_mismatch` default stays `"raise"`

`crx/config.py :: ChunkingConfig`. If the diff and the on-disk file disagree, line
numbers have shifted and every finding for that file is wrong. Skipping the file is
correct; proceeding produces confidently false output.

`"warn"` and `"ignore"` exist for debugging. Don't make either the default.

---

## Config must reject unknown keys

`crx/config.py :: _subset()` raises on unrecognized keys. A silently ignored typo
means someone changes a setting, sees no effect, and concludes the setting doesn't
work.

Similarly, `ReviewConfig.__post_init__` rejects unimplemented `mode` values rather
than falling back to `native`. Both `min_severity` and `mode` were previously dead
settings — declared but never read — and were fixed for exactly this reason. Don't
reintroduce a declared-but-unused setting.

---

## Missing static analyzers skip, never fail

`crx/ground.py :: Analyzer.run()` converts missing executables, timeouts, `OSError`,
and parser exceptions into `AnalyzerResult(skipped=True)`. In an air-gapped network
tool availability differs per machine; one absent binary must not stop a review.

Pinned by `test_missing_tool_is_skipped_not_fatal`.

---

## Instrumentation must not change review results

`crx/viz/engine.py`. `TracedPipeline` and `TracedLLMClient` subclass the real
pipeline and client and add observation only. If watching a review changes its
outcome, there is nothing worth watching.

Pinned by `test_traced_pipeline_matches_plain_pipeline`, which runs the same diff
through `Pipeline` and `TracedPipeline` and compares kept findings field by field.

The corollary is that the visualizer must never grow its own copy of pipeline
logic. `_review()` is overridden to *register* chunks and then delegate, not to
re-implement the stages — a duplicated `_review()` would drift and the dashboard
would quietly display last month's behavior. `Pipeline._timed()` is a method
rather than a module function for exactly this reason: it is the single stage
boundary, so wrapping it is enough.

---

## Rejected findings stay out of Markdown, stay in JSON

`crx/report.py`. Reviewers should not read rejected items. But the JSON must retain
them with reasons — that is the only evidence available for filter tuning.

Pinned by `test_rejected_finding_does_not_reach_report`.

---

## Tests must run without network or LLM

`python tests/run_all.py` uses `tests/fake_vllm.py`, a stdlib `ThreadingHTTPServer`
standing in for vLLM. This is what makes post-transfer integrity verification
possible inside the air-gapped network.

Any new test that requires a real endpoint breaks that property.

`tests/test_pipeline.py` builds a real temporary git repo and uses real `git diff`
output. Hand-written diffs were wrong twice during development; don't reintroduce
them. If `git` is absent the module skips rather than failing.

---

## The core stays dependency-free; only the MCP layer may not

Python 3.11+. The split is deliberate and load-bearing:

| Layer | Modules | Dependencies |
|---|---|---|
| Core | everything except `mcp.py` | **stdlib only** |
| git access | `gitio.py` | GitPython preferred, **subprocess fallback required** |
| MCP transport | `mcp.py` | FastMCP (`requirements.txt`) |
| Symbol location | `chunk.py` | tree-sitter optional, heuristic fallback required |
| Visualizer transport | `viz/server.py` | uvicorn preferred, **stdlib `http.server` fallback required** |

Two properties must survive any change here:

1. **`python tests/run_all.py` passes with nothing installed.** Post-transfer
   integrity verification inside the air-gapped network cannot presuppose a working
   `pip install`. Tests that need FastMCP must skip cleanly, not fail.
2. **`python -m crx review|scan|doctor` works with nothing installed.** Only
   `python -m crx.mcp` may require a wheel.

`crx/viz/` is deliberately in the same position as `gitio.py`, not `mcp.py`: it is
a convenience surface, and a convenience surface must not lengthen the transfer
manifest. `python -m crx.viz` runs on stdlib alone; uvicorn is used when present.
For the same reason the page loads no CDN script, web font, or icon service — in
an air-gapped browser those do not fail fast, they hang. Do not "modernize" the
front end by adding a framework or a build step.

This is why `crx/service.py` exists separately from `crx/mcp.py` — all review logic
lives in the service and imports no FastMCP, so the bulk stays testable without it.
`mcp.py` is a thin binding and should stay that way.

Adding a required wheel imposes security review and transitive-dependency tracing on
every air-gapped transfer. Before reaching for a library, check whether stdlib
covers it — `urllib` replaced httpx, `dataclasses` replaced pydantic, a custom
runner replaced pytest. FastMCP earned its place because hand-rolling MCP means
tracking a moving spec; GitPython did not have to (hence the fallback).

---

## Korean stays Korean

Code comments, docstrings, prompts, log messages, CLI output, and `docs/` are
Korean. Identifiers and rule IDs are English. This wiki is English.

Don't translate Korean comments to English as "cleanup."
