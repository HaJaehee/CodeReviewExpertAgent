# Architecture

## Pipeline

```
git diff (or file list for scan mode)
   │
   ▼  crex/chunk.py :: parse_unified_diff()
FileDiff[]              hunks with per-line status + line numbers
   │
   ▼  crex/chunk.py :: Chunker.chunk_file()
   │    1. verify diff matches on-disk source  → DiffSourceMismatch
   │    2. expand each hunk to enclosing symbol → capped at 4×, truncated to 3×
   │    3. merge overlapping ranges
   │    4. render with [added @142] line annotations
ReviewChunk[]
   │
   ▼  crex/ground.py :: GroundingGate.collect() + attach()
   │    analyzers run in parallel (6 default, 8 available); missing tools skip silently
StaticFinding[] attached to chunks by line range
   │
   ▼  crex/generate.py :: RuleChecker.review()
   │    one LLM call per chunk, JSON Schema with enum constraints
Finding[]
   │
   ▼  crex/filter.py :: ReviewFilter.filter()
   │    1. deterministic checks (no LLM call)
   │    2. surviving items → cross-model verdict, Conclusion-First
Finding[] kept  +  FilterVerdict[] rejected
   │
   ▼  crex/report.py :: write_all()
Markdown / SARIF / JSON
```

Two LLM calls per chunk at most: one to generate, one to verify (only if the chunk
produced findings). Typical ratio is ~1.3 calls per chunk.

## Four layers of hallucination defense

This is the core idea. Each layer catches what the previous one missed.

| Layer | Mechanism | Blocks | Where |
|---|---|---|---|
| 1. Input | `[added @142]` annotation on every line | Removes any need for the model to *infer* line numbers | `chunk.py :: DiffLine.annotate()` |
| 2. Generation | JSON Schema `enum` on `line` and `rule_id` | Makes fabricated line numbers and rule IDs **impossible to emit** | `generate.py :: build_findings_schema()` |
| 3. Verification (deterministic) | Line-range and changed-line checks | Anything layer 2 let through; costs nothing, 100% reliable | `filter.py :: _check_deterministic()` |
| 4. Verification (LLM) | Different model returns yes/no | Claims with no basis in the code | `filter.py :: _verify_llm()` |

Layer 2 is the distinctive one. vLLM's guided decoding masks tokens that would
violate the schema, so putting the chunk's actual changed line numbers into
`{"line": {"enum": [8, 10]}}` means the model *cannot generate* line 11. This is
generation-time prevention, not post-hoc filtering.

Layers 3 and 4 stay in place because guided decoding is not guaranteed — older vLLM
builds or a misconfigured `structured_output_mode` silently drop the constraint.

Because layer 2 depends on the server supporting guided decoding at all, `llm.py`
treats that support as something to establish rather than assume. On the first
structured call it walks a ladder — `response_format` → `guided_json`, each with the
original schema and then a relaxed one — and caches whichever combination the server
accepts. If none works it raises `StructuredOutputError` rather than returning an
empty result: a review that cannot constrain generation must fail loudly, because
its output is indistinguishable from clean code. `doctor` walks the same ladder and
additionally verifies the response honors the enums (`_enum_violations()`), which
catches servers that accept the schema and ignore it.

## Module map

| Module | Lines | Responsibility |
|---|---|---|
| `crex/schema.py` | 333 | Dataclasses. `Finding`, `ReviewChunk`, `StaticFinding`, `FilterVerdict`, `ReviewResult`, enums |
| `crex/llm.py` | 512 | OpenAI-compatible client over `urllib`. Guided decoding, token budget, retry |
| `crex/chunk.py` | 618 | Diff parsing, symbol location (tree-sitter + fallback), chunking, consistency check |
| `crex/ground.py` | 548 | 8 static-analyzer adapters + output normalization + attachment |
| `crex/generate.py` | 254 | RuleChecker: enum-constrained schema, prompt, parsing |
| `crex/filter.py` | 291 | ReviewFilter: deterministic checks + cross-model verdict |
| `crex/rules.py` | 245 | Taxonomy loader, per-language selection, OCR `rule.json` emitter |
| `crex/pipeline.py` | 328 | Orchestration for `run_diff()` and `run_scan()`. `_timed()` is the only stage boundary — subclasses observe stages by wrapping it |
| `crex/report.py` | 191 | Markdown / SARIF 2.1.0 / JSON output |
| `crex/config.py` | 234 | TOML config loading with unknown-key rejection (sections *and* top level) |
| `crex/cli.py` | 365 | `review` / `scan` / `doctor` / `workspace` subcommands |
| `crex/workspace.py` | 351 | Which repository is under review — resolve, switch at runtime, pin to `crex.toml`. One rule shared by CLI, MCP, and dashboard |
| `crex/paths.py` | 138 | Directory expansion, exclude globs, diff path filtering |
| `crex/gitio.py` | 147 | git diff / merge-base. GitPython with subprocess fallback |
| `crex/service.py` | 268 | `ReviewService` — the 7 MCP operations. **No FastMCP import** |
| `crex/mcp.py` | 355 | FastMCP binding only. Tool schemas from type hints + docstrings; stdio and Streamable HTTP transports |

Dependency direction is strictly downward: `mcp → service → pipeline → {chunk,
ground, generate, filter, report, paths, gitio} → {schema, llm, rules, config}`.
`cli` sits alongside `service`; both reach `workspace`, which sits directly above
`config` and `gitio`. `schema.py` imports nothing from the package. FastMCP appears
in `mcp.py` and nowhere else.

`mcp.py` serves stdio by default and Streamable HTTP under `--transport http`. The
transport changes nothing below it — same `ReviewService`, same workspace rules — but
it does change the threat model: the HTTP endpoint has no authentication, so a
non-loopback bind disables `set_workspace` there, the same call the dashboard blocks
for the same reason. Tool docstrings in that file are English because they *are* the
tool schema the agent reads.

### Workspace resolution

CREX does not have to live inside the repository it reviews, and in an air-gapped
setup it should not: one imported copy stays verifiable, many copies do not. The
working directory is CREX's own root; the target is named separately.

    --workspace  >  CREX_WORKSPACE / CREX_REPO  >  crex.toml `workspace`  >  git root of cwd

`crex/workspace.py` returns a `Workspace` (root, config, reports dir, origin,
`is_git`) and is the *only* place that reads those environment variables — CLI, MCP
server, and dashboard used to each read them separately, which is exactly how three
entry points end up reviewing three different repositories. A subdirectory argument
is promoted to the git root, because every path in a chunk and every static-analyzer
finding is relative to that root. When the workspace comes from an argument or the
environment and no config file is named, `<workspace>/crex.toml` wins over CREX's own
— per-repository `compile_commands_dir` and `dotnet_project` differ — and the search
never walks above the workspace.

The target can also move while a process is up: `switch()` re-runs `resolve()` rather
than re-implementing its checks, so the dashboard button and the MCP `set_workspace`
tool cannot be a looser path in than startup was. Whatever the user pinned explicitly
(`--config`, `--out`) follows the switch; everything else follows the new workspace.
Only `persist_workspace()` — the `python -m crex workspace` command — writes to
`crex.toml`; a dashboard click or an agent turn stays in-process, because neither
should decide what the next person's run targets.

### `crex/viz/` — the observability surface

A separate 3-tier package that runs the same pipeline under instrumentation and
streams it to a browser. It adds no required wheel; see
[invariants](invariants.md#the-core-stays-dependency-free-only-the-mcp-layer-may-not).

| Tier | Module | Lines | Responsibility |
|---|---|---|---|
| Engine | `viz/trace.py` | 238 | Event model, `Tracer`, prompt↔chunk↔finding correlation |
| Engine | `viz/engine.py` | 476 | `TracedPipeline` / `TracedLLMClient`, `RunRegistry` (one thread per run) |
| Application | `viz/api.py` | 258 | Transport-agnostic router. `Request → Response`, nothing else |
| Application | `viz/server.py` | 252 | Hand-written ASGI app for uvicorn + stdlib `http.server` fallback |
| Presentation | `viz/web/*` | 1792 | `index.html`, `style.css`, `store.js` (localStorage), `client.js`, `view.js` |

Dependencies point down and never back: `server → api → engine → trace → crex.*`.
`api.py` never imports `Pipeline`; `engine.py` never imports HTTP.

Three instrumentation points, chosen so that **no pipeline logic is duplicated**:

| Override | What it yields |
|---|---|
| `Pipeline._timed` | stage start/end for chunk, ground, generate, filter |
| `Pipeline._review` | the chunk list — the anchor for correlation |
| `LLMClient.complete` / `.complete_json` | prompts, schema, raw response, latency, per role |

Correlation is by prompt content, not template parsing: `chunk.render_code()`
appears verbatim inside both prompts, and a verifier prompt is scored against
registered findings on `path:line` (3) + message (2) + `rule_id` (1). Regex over
the prompt templates would break silently whenever a template changed.

Runs execute in a daemon thread; the browser polls
`GET /api/runs/{id}/events?since=<cursor>`. There is no server-side database —
history lives in the browser's localStorage. Full reports still go to disk through
the normal `ReviewService` path, which is also what makes the "what the agent
receives" panel exact rather than reconstructed.

## Key data types

```python
ReviewChunk:
    chunk_id: str                 # "src/buffer.cpp#0"
    path, language
    start_line, end_line          # 1-indexed, inclusive, new-file coordinates
    lines: list[DiffLine]         # rendered with status annotations
    changed_linenos: set[int]     # the ONLY lines a finding may target
    enclosing_symbol: str | None
    static_findings: list[StaticFinding]

Finding:
    path, line, end_line
    dimension: Dimension          # code_defect | security_vulnerability |
                                  # maintainability | performance
    severity: Severity            # capped by the rule's taxonomy severity
    rule_id: str                  # must exist in taxonomy — join key
    message, suggestion
    chunk_id: str | None          # lets the filter recover the source chunk

FilterVerdict:
    finding, kept: bool, reason: str
    reject_reason: RejectReason | None
    short_circuited: bool         # True = rejected without an LLM call
```

`ReviewResult.reject_rate` is the health signal to watch. 40–60% is normal;
outside that range something is wrong (see
[`docs/troubleshooting.md`](../docs/troubleshooting.md)).

## Chunking details

**Why expand at all.** Showing the model three changed lines gives it no context —
it doesn't see the function signature or what came before. Showing the whole file
blows the context budget and degrades precision.

**Expansion cap.** If the enclosing symbol is more than `expansion_limit` (4.0)
times the hunk size, truncate to `expansion_truncate` (3.0) times, centered on the
hunk. A hard ceiling `absolute_max_lines` (400) sits above that for god functions.

**Symbol location.** `SymbolLocator` tries tree-sitter first, picking the *smallest*
node that encloses the target range (method beats class). Falls back to:
- Python: indentation tracking from the nearest shallower `def`/`class`
- C++/C#: brace-depth counting with string/comment literals stripped

Fallback returning `None` means the chunk gets ±6 lines of raw context.

**Line annotation format.** Every rendered line is `[status @lineno] text` where
status is `added` / `deleted` / `unchanged`. Deleted lines have no position in the
new file, so they are inserted before the next surviving line with their *old*
line number.

**Consistency check.** `_verify_source()` compares every non-deleted diff line
against the actual file. Any mismatch raises `DiffSourceMismatch` and the file is
skipped. This was added after a hand-written test fixture with an off-by-one hunk
header produced silently misnumbered chunks — the exact failure the annotation
scheme exists to prevent.

## Grounding

Each analyzer subclasses `Analyzer` and implements `build_command()` + `parse()`.
`available()` uses `shutil.which()`. Timeouts, `OSError`, and parser exceptions are
all absorbed into `AnalyzerResult(skipped=True)` — one missing tool must never stop
a review.

| Analyzer | Tool | Languages |
|---|---|---|
| `ClangTidy` | `clang-tidy` | C++ |
| `Cppcheck` | `cppcheck` | C++ |
| `RoslynAnalyzers` | `dotnet build` | C# |
| `Roslynator` | `roslynator` | C# |
| `Ruff` | `ruff` | Python |
| `Mypy` | `mypy` | Python |
| `Bandit` | `bandit` | Python |
| `Semgrep` | `semgrep` | all |

Two regexes handle most output: `_GNU_STYLE` (`path:line:col: severity: msg [rule]`)
and `_MSBUILD_STYLE` (`path(line,col): severity CODE: msg`). JSON-emitting tools
(ruff, bandit, semgrep) have dedicated parsers. `note`-level diagnostics are
dropped — they are elaborations of the preceding warning and would double-report.

`attach()` matches paths by suffix because analyzers mix absolute and relative paths.

The findings then reframe the LLM's job in the prompt: not *"find defects"* but
*"verify these tool results and add only what tools structurally cannot catch."*

## Generation

`build_findings_schema()` produces per-chunk schemas:

```python
{
  "line":     {"type": "integer", "enum": [8, 10]},        # chunk's changed lines
  "rule_id":  {"type": "string",  "enum": [...taxonomy IDs for this language...]},
  "severity": {"type": "string",  "enum": ["high", "medium", "low"]},
  "message":  {"type": "string", "maxLength": 400},
  "suggestion": {"type": "string", "maxLength": 400},
}
```

wrapped in `{"findings": {"type": "array", "maxItems": 5}}`.

No agent loop. Small models given file-read/search tools fail tool calls and blow
context. Fixed steps instead: one chunk, one prompt, one list.

`_severity_of()` caps reported severity at the taxonomy value. Left free, small
models mark everything `high`. Models may lower severity, never raise it.

## Verification

Deterministic stage rejects, in order: unresolvable chunk, line outside chunk range,
line not in `changed_linenos` (diff mode only), duplicate `(path, line, rule_id)`.
All are `short_circuited=True` — no LLM call.

LLM stage sends only the finding plus its chunk. No original review context, no
sibling findings. Giving the verifier the generator's framing drags it along.

`code_present: false` overrides `verdict: "yes"` — if the verifier says the described
code isn't in the snippet, that's an unambiguous hallucination signal.

Verification failure rejects conservatively (`RejectReason.FILTER_ERROR`). A dead
verifier endpoint yields an empty review, which is better than unverified findings
shipping.
