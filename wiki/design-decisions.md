# Design Decisions

Each entry states the decision, the reason, and what would have to be true to
revisit it. Several of these look arbitrary from the code alone.

---

## Context budget is 8,192 tokens, not 256K

Qwen3.6 and Gemma 4 both support 256K context. CREX uses 8,192.

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

**Implementation.** `EndpointConfig.max_input_tokens` (8,192 default); prompts are
assembled using pre-allocated component budgets (System ~600, Rules ~1,500,
AST ~400, Static ~400, Code ~4,500 tokens) guaranteeing 100% of the chunk lines
are preserved without blind string slicing.

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

**Fragility.** If guided decoding is not actually in force, two of four defense
layers vanish — and the failure is silent in both directions. A server that rejects
the schema makes every chunk fail (zero findings, which reads as clean code); a
server that accepts and ignores it produces unconstrained output (hallucinated line
numbers, which reads as findings).

This actually happened: `doctor` reported OK while every C++/C# review returned zero
findings, because `health()` sent no schema and the per-chunk `except Exception`
swallowed the 400s. Three changes came out of it.

- **Negotiate instead of configure.** `structured_output_mode` defaults to `"auto"`
  and `llm.py` walks a ladder to find what the server accepts, caching the result.
  Most version mismatches no longer need a human at all.
- **Never relax `enum`.** The ladder's last resort drops `maxLength`/`maxItems`
  (xgrammar can't compile them) but never the enums, which are the actual defense.
- **Prove it, don't ping it.** `doctor` sends the real schemas and checks the reply
  against them, so "accepted but ignored" shows up as a failure.

The invariant is in `wiki/invariants.md`; the user-facing symptoms are in
`docs/user_manual/troubleshooting.md`.

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

**Why.** A false positive costs more than a miss: it spends a reviewer's attention
on code that is fine, and every one of them has to be read to be dismissed. Target
is FAR ≤ 25%, matching BitsAI-CR's ~75% precision. OCR makes the same trade
("sacrifices recall for accuracy").

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
per-language statistics blend together. `python -m crex.rules` rejects duplicates.

To rename: add under the new ID, delete the old one. Don't mutate.

---

## Config is JSON, with comments and long prose added back by convention

The config file is `crex.json`, read with stdlib `json`. It used to be TOML.

**Why the change.** The file is written by programs as well as by people —
`crex workspace` pins a repository, `crex compiledb` records where the compile
database landed, and the dashboard's Rebuild does the same through
`repo_config_path()`. `tomllib` is read-only and there is no stdlib TOML writer, so
that path had to splice individual lines with regexes: find the key, replace the
line, guess where to insert a new one, and never touch anything else, because a
round-trip through any parser would erase every comment. JSON round-trips through
the standard library, so the writer reads, edits, and writes the whole file.

**What JSON costs, and how it is paid.** No comments, no multi-line strings — both
of which this file needs, since it is the one artifact a new user reads end to end.
Rather than adopt JSON5 or a wheel, two conventions sit on top of plain JSON, which
means every JSON tool still opens it:

1. A key starting with `//` is documentation, stripped before validation.
2. A string setting may be written as an array of strings, joined with `\n`.

The first turns out to be *better* than TOML comments, not merely equivalent:
documentation is data, so it survives the programs that rewrite the file. What was
previously an argument for line-splicing is now handled by the format itself.

The second is scoped by declared field type rather than by a list of key names, so
`analyzers: list[str]` stays a list. That is not a stylistic point — fusing three
analyzer names into one string yields zero findings and no error, which is
indistinguishable from a clean review.

Blank lines do not survive a programmatic rewrite; comment keys do. Grouping is
carried by the `//` keys, not by whitespace.

**Not converted.** `rules/taxonomy.toml` stays TOML. It is rule data, not
configuration, no program writes to it, and its multi-line `criteria` strings read
better as TOML heredocs than as JSON arrays. It also versions independently — rules
outlive releases.

---

## Dependencies confined to the MCP layer

The core uses stdlib only:

- HTTP: `urllib.request`, not httpx/requests
- Models: `dataclasses`, not pydantic
- Config: `json` (stdlib) — comments are `//` keys, long prose is a string array
- Rule taxonomy: `tomllib` (stdlib since 3.11)
- Tests: custom runner, not pytest

**Why.** Importing a wheel into an air-gapped corporate network requires security
review, transitive dependency tracing, and repetition on every version bump. The
cost is real and recurring.

**What changed.** The user asked for FastMCP and said they would provide the
runtime, so `requirements.txt` now exists. Two properties were preserved rather
than dropping the principle wholesale:

| Dependency | Status | Fallback |
|---|---|---|
| FastMCP | required by `crex/mcp.py` only | none — but CLI and tests don't need it |
| GitPython | preferred in `crex/gitio.py` | subprocess, same diff text |
| tree-sitter | optional in `crex/chunk.py` | brace/indent heuristic |

`crex/service.py` was split out of `crex/mcp.py` specifically so the review logic
imports no FastMCP and stays testable without it. `python -m crex review|scan|doctor`
and `python tests/run_all.py` still run on a bare interpreter — post-transfer
integrity verification cannot presuppose a working `pip install`.

FastMCP earned the exception because MCP is a moving spec and hand-rolling it means
tracking that movement forever. GitPython did not clear the same bar, hence the
fallback: the subprocess path is ten lines and never breaks.

Token counting is a `len(text) / 3.0` estimate rather than a real tokenizer. Exact
counting would require importing `transformers`. At an 8,192 budget against a 32K
window, a 20% estimation error is harmless.

---

## Chunk size capped at 150 lines with bounded merging and cluster partitioning

`absolute_max_lines` is 150 (down from 400). When merging adjacent hunks in `_merge_ranges()`,
ranges that would exceed 150 lines remain separate chunks. Single oversized hunks or giant functions
are partitioned around changed-line clusters (`_partition_oversized_range()`).

**Why.** Annotated source lines consume ~30 tokens/line. A 400-line chunk consumes ~12,000 tokens
alone, which overflows the 8,192 input token context when combined with 15 rules (~1,500 tokens),
Tree-sitter AST context (~400 tokens), and static findings (~400 tokens). A 150-line chunk consumes
~4,500 tokens, fitting comfortably within the 8,192 budget and leaving zero need for prompt truncation.

**Automatic Synchronization & Size Mismatch Defense.**
1. `calculate_safe_max_lines()` automatically derives the safe line count from `generator.max_input_tokens`
   and clamps `effective_max_lines` down (e.g., to ~50 lines if `max_input_tokens` is 4096), preventing
   size mismatch failures if a user sets an incompatible line limit.
2. If dense code or long lines approach the token limit, `RuleChecker` applies progressive pruning
   (reducing static findings to top 3 and rules to top 10) before ever modifying code, ensuring `added`
   code lines are 100% preserved.

---

## Verifier focused context window (±25 lines)

`ReviewFilter._verify_llm()` passes `chunk.render_window(finding.line, window=25)` rather than
the entire chunk.

**Why.** The verifier endpoint operates with a strict 4,096 token limit (`max_input_tokens = 4096`).
Sending a 150~400 line chunk risked middle-truncation where the very line cited by the finding
was cut off, causing the verifier to reject valid defects with `CODE_NOT_FOUND`. A focused 50-line window
around the defect consumes ~1,000 tokens (25% of budget), completely eliminating false rejections.

---

## Local SQLite DB (`.crex/viz.db`) for visualizer persistence

Visualizer runs, event streams, and preferences are stored in `<workspace>/.crex/viz.db` via
Python's standard library `sqlite3`.

**Why.** Browser `localStorage` is capped at ~5MB, which forced clipping prompt bodies at 6,000 characters
and quickly filled up after a few reviews. SQLite has zero external dependencies, requires no setup,
is 100% air-gapped compliant, persists complete unclipped LLM payloads, and supports selective single-run
deletion (`✕`).

---

## Korean for prompts, comments, and user docs

The user is a Korean-speaking engineer and findings are read by Korean developers.
Qwen3.6 (201 languages) and Gemma 4 are both strongly multilingual, so Korean
prompts do not cost measurable instruction-following quality at this scale.

This wiki is English because it is written for agents.

---

## Unescaped double-quote self-repair in structured JSON payloads

When models quote source code containing double quotes (e.g. `printf("hello")` or string literals) in `message`, `suggestion`, or `reason`, they frequently emit raw unescaped quotes inside JSON string values.

**Why.** Small local models (25–40B) do not reliably escape internal quotation marks when reproducing code snippets inside JSON strings, even when guided decoding is enabled. Under standard `json.loads()`, this causes syntax errors or splits the JSON mid-sentence, causing valid defects to be silently discarded.

**Implementation.**
1. `repair_unescaped_quotes()` in `crex/llm.py` and `crex/filter.py` utilizes lookahead regex heuristics targeting structural JSON tokens (`", "`, `": "`, `"\n`, `"}`) to distinguish true string boundaries from raw quotes, escaping internal quotes before JSON parsing.
2. Prompts explicitly instruct both generator and verifier: "Never output raw unescaped double quotes inside JSON string fields; use single quotes or escape them properly."
3. Multi-tier parsing fallback (`robust_json_loads`) repairs and extracts partial objects, preserving code snippet integrity and preventing valid findings from being dropped.

---

## Output token budget of 4,096 tokens and reasoning-model handling

The default `max_output_tokens` for generator and verifier endpoints is 4,096.

**Why.** Modern local models (Qwen 2.5/3.x-Coder with thinking, Gemma 4) frequently employ internal chain-of-thought (CoT) reasoning. Under a tight token budget (e.g., 500–1600 tokens), reasoning models exhaust their token allotment generating internal deliberations and truncate before producing the final JSON structure, or return `content=None` with reasoning-only payloads (`LLMError: content 없음`).

**Implementation.**
1. **Budget expansion**: Default output token budget set to 4,096 in code defaults and `crex.example.json`.
2. **Reasoning extraction fallback**: If `content` is empty/None but `reasoning` or `reasoning_content` is present in the assistant message, `llm.py` extracts the final conclusion and JSON payload from the thought text rather than raising a fatal error.
3. **Verifier comment sanitization**: Internal model deliberation text is explicitly stripped from user-facing `verifier_comment` and reports; only the final verdict and concise justification are displayed.
4. **Prompt instruction**: System prompts instruct thinking models to deliberate concisely and reach the final verdict promptly ("생각을 하되 너무 긴 생각에 빠지지 말고 신속히 결론에 도달하라").

---

## Three-tier exclusion via `.crexignore` with path-scoped rule scoping

Exclusions are supported via repository-root `.crexignore` files and `crex.json` (`review.exclude`, `review.exclude_rules`).

**Why.** Repository maintainers need to commit review exclusion rules directly to the target Git repository without modifying the global or machine-level `crex.json` configuration. Furthermore, coarse-grained exclusions (ignoring an entire directory or disabling a rule globally) are too blunt—teams often want to suppress noise (like magic number checks in `tests/**`) while maintaining rigorous defect checks in production code.

**Three granularities:**
1. **Path glob exclusions** (`vendor/**`, `*.pb.cc`): Skipped before chunking and static analysis, saving 100% of LLM tokens and execution time.
2. **Global rule exclusions** (`rule:cpp.c-style-cast`): Stripped from the generator's taxonomy prompt schema and deterministically rejected by `ReviewFilter` (`RULE_EXCLUDED`) before reaching the verifier.
3. **Path-scoped rule exclusions** (`src/legacy/**:cpp.*`, `tests/** rule:any.magic-number`): Dynamically excluded from the taxonomy schema only when chunking files matching the path glob; files outside that path retain full rule coverage.

**Precedence.** `DEFAULT_EXCLUDE` + `crex.json` + `.crexignore` are merged additively (union).

---

## Visualizer-integrated `.crexignore` addition and rollback

Users can add or cancel `.crexignore` rules directly from code review findings in `crex.viz`.

**Why.** Context-switching between the web dashboard and a code editor to craft glob and rule syntax induces cognitive friction and syntax errors. Managing exclusions directly from the finding row makes false-positive suppression instantaneous.

**Implementation.**
- Finding rows in `crex.viz` feature a 1-click `[+ 제외]` button presenting 4 recommended scopes (file-rule scoped, directory-rule scoped, global rule, whole file).
- Existing `.crexignore` matches are detected dynamically (`POST /api/crexignore/check`) and marked with an `[제외됨]` badge and immediate `[취소]` button for effortless rollback.
- `CrexIgnoreManager` (`crex/ignore_manager.py`) preserves file formatting, comments, and line endings.
- Enforces workspace root containment and blocks non-loopback remote writes (`403 Forbidden`).

---

## Offline interactive HTML report generation

`to_html()` in `crex/report.py` and the `crex.viz` client-side exporter produce self-contained `.html` reports.

**Why.** Markdown reports lack collapsible sections and interactive filters, while CSV reports lack readability for multi-line suggestions and diffs. The HTML report packages the full `crex.viz` dark-mode theme, finding folding (`<details>`), metrics cards, and verdict tabs into a single zero-dependency file that renders cleanly in air-gapped browsers without external fonts, scripts, or CDNs.

---

## Cross-Flow Review Stage 1: AST symbol contracts and cross-chunk Change Map injection

`TreeSitterAnalyzer.extract_file_symbols()` (`crex/treesitter.py`) and `attach_cross_chunk_context()` (`crex/chunk.py`) deterministically extract symbol contracts and a cross-chunk Change Map, injecting them into every `ReviewChunk` prompt without extra LLM calls.

**Why.** Partitioning diffs into 150-line stateless chunks (`ReviewChunk`) prevents context blowup on 25–40B local models, but isolates each chunk from its surrounding class invariants, callee return/exception contracts, and sibling chunks modified in the same review. When a function `parse_header()` can return `nullptr` (or `fetch_session()` can return `None` / raise `ValueError`) in another chunk or earlier in the file, a stateless chunk reviewing only the caller cannot know the callee's contract without guessing.

**Implementation.**
1. **`SymbolContract` extraction**: Per file, `TreeSitterAnalyzer` (with regex/indentation fallback when `tree-sitter` is absent) indexes every function/method and class/struct, capturing its signature, return value expressions (`returns`), thrown/raised exceptions (`throws`), resource/lock operations (`resource_ops`), class member fields (`member_fields`), and a bounded 18-line definition snippet (`symbol_definitions`).
2. **Enclosing class fields & intra/cross-file callee contracts**: Each chunk receives the member fields of its enclosing class/struct and 1-line contract summaries of functions invoked inside the chunk (`callee_contracts`).
3. **Cross-chunk Change Map**: `attach_cross_chunk_context()` builds a compact Change Map summarizing all other chunks and files modified in the review run (`path`, line range, enclosing symbol, changed line count, signature/return hints) and resolves cross-file callees across the modified fileset.
4. **Progressive token-budget pruning**: `ReviewChunk.render_ast_context(compact=True)` drops the Change Map and trims field/contract counts if a dense chunk approaches `generator.max_input_tokens`, preserving 100% of annotated code lines.

---

## Cross-Flow Review Stage 2: Context-expanded verifier with out-of-window symbol resolution

`ReviewFilter._resolve_related_context()` (`crex/filter.py`) attaches out-of-window callee and class definitions to `VERIFIER_USER` when evaluating findings that span across functions or files.

**Why.** `ReviewFilter._verify_llm()` uses a focused `±25`-line window (`chunk.render_window(finding.line, window=25)`) to fit within the verifier's 4,096 input token budget. Once Stage 1 enables `RuleChecker` to flag cross-function contract violations (e.g., dereferencing a nullable return from `acquire_slot()` defined at `L1-L4` while the call site is at `L80`), a verifier that sees only `L55-L105` cannot inspect `acquire_slot()`'s body. It must either reject valid cross-function defects for lack of evidence or rubber-stamp hallucinated claims when the callee is actually safe.

**Implementation.**
1. **Unified symbol pool**: `ReviewFilter` aggregates `symbol_definitions` across all `ReviewChunk`s in the run.
2. **Relevance scoring & in-window deduplication**: For each candidate finding, `_resolve_related_context()` scores symbol definitions by proximity to `finding.line` (`±2` lines: +4), explicit mention in `finding.message` / `finding.suggestion` (+3), presence in `chunk.calls` (+2), and enclosing symbol match (+1). Any definition whose lines already fall inside `[finding.line - 25, finding.line + 25]` of the same file is skipped to avoid duplication.
3. **Bounded injection & budget guard**: Up to 3 top-ranked out-of-window symbol snippets are injected under `## 연관 심볼 및 피호출 함수 정의 (창 외부 문맥)`. If the combined prompt exceeds `verifier.max_input_tokens`, the auxiliary block is automatically dropped so the core `±25`-line code window is never truncated.

---

## Research sources

| Source | Used for |
|---|---|
| [BitsAI-CR](https://arxiv.org/abs/2501.15134) (ByteDance, production) | Two-stage pipeline, Conclusion-First, 4×/3× expansion, rule taxonomy shape, Outdated Rate |
| [Towards Practical Defect-Focused Automated Code Review](https://arxiv.org/abs/2505.17928) (ICML 2025 Spotlight) | Industrial C++ codebases, code slicing, false-alarm filtering |
| [When More Retrieval Hurts](https://arxiv.org/html/2511.05302v2) | top-1 > top-k; context expansion degrades quality |
| [LAURA](https://arxiv.org/abs/2512.01356) (ASE 2025) | Context augmentation, review exemplar retrieval |
| [alibaba/open-code-review](https://github.com/alibaba/open-code-review) | Hybrid deterministic+LLM architecture, smart bundling, rule.json format |
