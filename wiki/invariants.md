# Invariants

Things that look incidental but are load-bearing. Breaking any of these degrades
the system quietly — tests may still pass, reviews still produce output, and the
output is worse.

---

## Conclusion-First property order

**`VERDICT_SCHEMA` in `crex/filter.py` must list `verdict` first.**

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

In `crex/generate.py :: build_findings_schema()`:

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

`crex/schema.py`. Reject reasons are the tuning evidence for the filter. Changing
what an existing value means breaks comparison with past reports. Add new members;
don't repurpose existing ones.

---

## A failed run must never look like a clean one

Zero findings has two possible meanings — the code is clean, or the pipeline never
ran. Reporting them the same way turns a broken deployment into a passing gate.

This was a real incident: `doctor` reported every endpoint OK while every C++ and C#
review returned zero findings. The generator was rejecting the guided-decoding
request with HTTP 400, `RuleChecker.review_chunk()` swallowed it per chunk into a
`log.warning`, and the report said `지적 사항 없음.` with exit code 0.

Four things keep them distinguishable, and all four must hold together:

- `RuleChecker.errors` and `ReviewFilter.errors` collect every swallowed call
  failure. The blanket `except Exception` around each LLM call stays — partial
  failure really is normal in an air-gapped network — but it records before it
  returns `[]`.
- `Pipeline._review()` copies both into `ReviewResult.errors` and counts them in
  `generation_errors` / `verification_errors`.
- `ReviewResult.healthy` is False whenever `errors` is non-empty. Markdown replaces
  `지적 사항 없음.` with a warning banner, and `service.summarize()` does the same
  for the MCP summary — the agent in Zed reads only that summary and would otherwise
  tell the user the code is clean.
- The CLI exits **3**, not 0. Distinct from 1 (`high` findings) so CI can tell
  "found problems" from "found nothing because it was broken".

Pinned by `test_total_generation_failure_is_not_silent` and
`test_zero_findings_with_errors_is_not_reported_as_clean`.

---

## `doctor` must exercise the path a review actually uses

`LLMClient.health()` sends a plain chat request with no schema. It proves the
endpoint answers and the model name resolves — nothing more. Every review call
additionally carries a JSON Schema, and that is the part that breaks.

So `doctor` calls `LLMClient.probe()`, which sends the *real* schemas
(`build_findings_schema()` with populated enums, and `VERDICT_SCHEMA`) and then
checks the response against them. It reports three distinct failures that a
connection check cannot see:

| Condition | What it means |
|---|---|
| Ladder exhausted | The server refuses schema requests outright |
| Response isn't parseable JSON | Guided decoding isn't actually engaged |
| `_enum_violations()` non-empty | Schema accepted but constraints ignored |

The last two return HTTP 200. A green connection check says nothing about them, and
in that state line-number hallucination is no longer structurally prevented.

Don't add a diagnostic that only pings the endpoint. Pinned by
`test_probe_catches_what_health_alone_misses`.

---

## A truncated response loses items, never the whole chunk

`max_tokens` cuts generation mid-token; guided decoding does not protect against
it. The observed shape: chunk 0 reviews fine, chunk 1 dies with `JSON 파싱 실패`,
and the raw response ends inside a string — usually while quoting source into
`suggestion`. Chunk length varies, so this looks like a per-chunk mystery rather
than a budget problem, and the old message pointed at guided decoding, which was
never the cause.

Three rules hold here:

- `_repair_truncated_json()` cuts back to the last point where a value certainly
  ended (a comma, or a closing bracket) and closes the open containers. It never
  closes an open string. Half a sentence completed into a finding would read as a
  real defect, and that is precisely what this pipeline exists to prevent — the
  partial trailing item comes out missing required fields and `RuleChecker._parse()`
  drops it.
- `_extract_first_json_object()` stops at an unbalanced `{` instead of walking
  into it. Otherwise a truncated `{"findings": [{...` yields the first *finding*
  as if it were the whole response, and the rest disappear silently. It does skip
  past a balanced-but-unparseable object — models quote `struct S { int a; }`
  before the JSON.
- Salvage is never silent. `LLMClient.last_call_truncated` tells the caller, and
  `RuleChecker` records it in `errors`, which makes the run unhealthy. "3 findings"
  and "3 findings out of an unknown number" must not read alike.

When nothing survives, `TruncatedOutputError` names `max_output_tokens` — a
different prescription from `StructuredOutputError`, which is why it is a different
type.

Pinned by `test_truncated_response_keeps_the_completed_findings`,
`test_truncated_response_does_not_leak_a_nested_object`, and
`test_rulechecker_reports_that_a_chunk_was_cut_short`.

---

## Output budgets live in config, not in call sites

`RuleChecker` used to pass `max_output_tokens=900` and `ReviewFilter` `400`,
overriding `llm.*.max_output_tokens` on the exact calls the setting exists for.
A user hitting truncation would follow the manual, raise the number, and see no
change. Both now pass nothing and let `EndpointConfig` decide; the verifier's cap
(`VERIFIER_MAX_OUTPUT_TOKENS`) is applied once, in `config.py`.

A hard-coded budget at a call site is a dead setting, and per this repo's rules a
dead setting is worse than a missing one.

---

## Relaxing a schema may drop limits, never `enum`

When a backend refuses a schema, `_relax_schema()` retries without the keywords in
`RELAXABLE_KEYWORDS` (`maxLength`, `maxItems`, `minLength`, `minItems`, `pattern`) —
xgrammar cannot compile some of them.

`enum` and `type` are never removed. The `line` and `rule_id` enums are what make
hallucination impossible rather than merely filtered; length caps are prompt
hygiene. Dropping the caps costs some verbosity, dropping the enums costs the whole
premise of the tool.

`_relax_schema()` also preserves property order, so `verdict` stays first
(see Conclusion-First above). Pinned by `test_relax_keeps_enum_and_type` and
`test_verdict_schema_property_order_survives_relaxation`.

---

## Verification failure must reject, not pass

`crex/filter.py :: _verify_llm()` returns `kept=False` with
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

`crex/generate.py :: _severity_of()` caps the model's reported severity at the
taxonomy value. Left free, small models mark everything `high` and priority
ordering becomes meaningless.

---

## Line annotation format is `[status @lineno] text`

`crex/schema.py :: DiffLine.annotate()`. The filter's deterministic line checks and
the whole prompt contract depend on this shape. `test_line_annotation_is_unambiguous`
pins it.

---

## `on_mismatch` default stays `"raise"`

`crex/config.py :: ChunkingConfig`. If the diff and the on-disk file disagree, line
numbers have shifted and every finding for that file is wrong. Skipping the file is
correct; proceeding produces confidently false output.

`"warn"` and `"ignore"` exist for debugging. Don't make either the default.

---

## Config must reject unknown keys

`crex/config.py :: _subset()` raises on unrecognized keys. A silently ignored typo
means someone changes a setting, sees no effect, and concludes the setting doesn't
work.

`load_config()` applies the same rule to top-level keys against `TOP_LEVEL_KEYS`,
and `_endpoint()` applies it to the `[llm.*]` tables against `ENDPOINT_KEYS`. That
last one was a hole: `_endpoint()` used to read with `raw.get(...)` and ignore
everything else, so a `bas_url` typo silently fell back to `http://localhost:8000/v1`
— the endpoint appeared unchanged no matter what you edited.

The top-level rule matters for the same reason: a `workspase` typo silently ignored
means the review runs against a different repository than the one the user named.

Similarly, `ReviewConfig.__post_init__` rejects unimplemented `mode` values rather
than falling back to `native`. Both `min_severity` and `mode` were previously dead
settings — declared but never read — and were fixed for exactly this reason. Don't
reintroduce a declared-but-unused setting.

Pinned by `test_unknown_top_level_config_key_is_rejected`.

---

## The config file is JSON, with two conventions layered on top

`crex.json` / `.crex.json`, read with stdlib `json`. JSON has no comments and no
multi-line strings, and this file is one a human reads and edits, so two rules fill
those gaps without touching the parser — any JSON tool still opens the file.

- **A key starting with `//` is documentation.** `strip_comment_keys()` removes them
  recursively before validation, `extra_body` included. Skipping `extra_body` would
  leave no way to explain the one place that most needs explaining — a model's magic
  parameters — and no inference server accepts a parameter named `// ...`.
- **A string setting may be written as an array of strings**, joined with `\n` on
  read. Long prose — `system_prompt`, `prompt_template` — squeezed onto one escaped
  line makes the file unreadable.

The second rule is scoped by **declared field type**, not by an allow-list of key
names: `_text_fields()` reads the dataclass annotations, so `analyzers: list[str]`
stays a list and a new string setting needs no registration. Joining `analyzers`
would fuse three analyzer names into one, and the result of that is zero findings
with no error — indistinguishable from a clean review.

The comment rule buys something TOML could not give: because documentation is
*data*, `persist_key()` reads the file, edits it, and writes the whole thing back
without losing it. Under TOML the writer had to splice individual lines, since
`tomllib` is read-only and any round-trip dropped every comment.

A leftover `crex.toml` is **reported, not ignored** — `_reject_legacy()` raises and
names the file. Silently skipping it is the same failure this section is about:
someone edits a config and nothing happens.

Pinned by `tests/test_config.py`.

---

## One workspace resolution rule, shared by every entry point

CLI, MCP server, and dashboard all call `crex/workspace.py :: resolve()`. They each
used to read `CREX_REPO` and friends themselves, which is how three entry points end
up pointed at three different repositories while every one of them looks correct in
isolation. Don't re-read those environment variables anywhere else; add to `resolve()`
instead.

Two properties within it are load-bearing:

- **A subdirectory argument is promoted to the git root.** Every chunk path and every
  static-analyzer path is relative to that root. A shifted base makes grounding match
  nothing, silently.
- **Config discovery for a given workspace never walks above it.** Picking up a
  `crex.json` from some ancestor directory means nobody can tell which file applied.

Changing the target later goes through the same door: `switch()` calls `resolve()`
instead of re-checking anything itself. A second validation path is a second set of
rules, and the looser one wins the moment they disagree.

Two more, on who may change it:

- **Only the CLI writes to `crex.json`.** The dashboard button and the MCP
  `set_workspace` tool change the running process and say so. A click or an agent
  turn must not decide what the next person's run targets.
- **No switching mid-run.** `RunRegistry.retarget()` refuses while a run is in
  flight, holding the same lock `start()` uses. `_execute` reads `repo_root` and
  `config` as it goes; swapping them halfway produces one report whose chunks came
  from one repository and whose analyzer findings came from another.

Pinned by `tests/test_workspace.py` and the workspace tests in `tests/test_viz.py`.

---

## Missing static analyzers skip, never fail

`crex/ground.py :: Analyzer.run()` converts missing executables, timeouts, `OSError`,
and parser exceptions into `AnalyzerResult(skipped=True)`. In an air-gapped network
tool availability differs per machine; one absent binary must not stop a review.

Pinned by `test_missing_tool_is_skipped_not_fatal`.

---

## Instrumentation must not change review results

`crex/viz/engine.py`. `TracedPipeline` and `TracedLLMClient` subclass the real
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

`crex/report.py`. Reviewers should not read rejected items. But the JSON must retain
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
2. **`python -m crex review|scan|doctor` works with nothing installed.** Only
   `python -m crex.mcp` may require a wheel.

`crex/viz/` is deliberately in the same position as `gitio.py`, not `mcp.py`: it is
a convenience surface, and a convenience surface must not lengthen the transfer
manifest. `python -m crex.viz` runs on stdlib alone; uvicorn is used when present.
For the same reason the page loads no CDN script, web font, or icon service — in
an air-gapped browser those do not fail fast, they hang. Do not "modernize" the
front end by adding a framework or a build step.

This is why `crex/service.py` exists separately from `crex/mcp.py` — all review logic
lives in the service and imports no FastMCP, so the bulk stays testable without it.
`mcp.py` is a thin binding and should stay that way.

Adding a required wheel imposes security review and transitive-dependency tracing on
every air-gapped transfer. Before reaching for a library, check whether stdlib
covers it — `urllib` replaced httpx, `dataclasses` replaced pydantic, a custom
runner replaced pytest. FastMCP earned its place because hand-rolling MCP means
tracking a moving spec; GitPython did not have to (hence the fallback).

---

## Korean stays Korean

Code comments, docstrings, prompts, log messages, CLI output, and `docs/user_manual/` are
Korean. Identifiers and rule IDs are English. This wiki is English.

Don't translate Korean comments to English as "cleanup."
