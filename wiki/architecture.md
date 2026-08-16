# Architecture

## Pipeline

```
git diff (or file list for scan mode)
   │
   ▼  crx/chunk.py :: parse_unified_diff()
FileDiff[]              hunks with per-line status + line numbers
   │
   ▼  crx/chunk.py :: Chunker.chunk_file()
   │    1. verify diff matches on-disk source  → DiffSourceMismatch
   │    2. expand each hunk to enclosing symbol → capped at 4×, truncated to 3×
   │    3. merge overlapping ranges
   │    4. render with [added @142] line annotations
ReviewChunk[]
   │
   ▼  crx/ground.py :: GroundingGate.collect() + attach()
   │    analyzers run in parallel (6 default, 8 available); missing tools skip silently
StaticFinding[] attached to chunks by line range
   │
   ▼  crx/generate.py :: RuleChecker.review()
   │    one LLM call per chunk, JSON Schema with enum constraints
Finding[]
   │
   ▼  crx/filter.py :: ReviewFilter.filter()
   │    1. deterministic checks (no LLM call)
   │    2. surviving items → cross-model verdict, Conclusion-First
Finding[] kept  +  FilterVerdict[] rejected
   │
   ▼  crx/report.py :: write_all()
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

## Module map

| Module | Lines | Responsibility |
|---|---|---|
| `crx/schema.py` | 316 | Dataclasses. `Finding`, `ReviewChunk`, `StaticFinding`, `FilterVerdict`, `ReviewResult`, enums |
| `crx/llm.py` | 250 | OpenAI-compatible client over `urllib`. Guided decoding, token budget, retry |
| `crx/chunk.py` | 618 | Diff parsing, symbol location (tree-sitter + fallback), chunking, consistency check |
| `crx/ground.py` | 509 | 8 static-analyzer adapters + output normalization + attachment |
| `crx/generate.py` | 243 | RuleChecker: enum-constrained schema, prompt, parsing |
| `crx/filter.py` | 283 | ReviewFilter: deterministic checks + cross-model verdict |
| `crx/rules.py` | 245 | Taxonomy loader, per-language selection, OCR `rule.json` emitter |
| `crx/pipeline.py` | 262 | Orchestration for `run_diff()` and `run_scan()` |
| `crx/report.py` | 163 | Markdown / SARIF 2.1.0 / JSON output |
| `crx/config.py` | 164 | TOML config loading with unknown-key rejection |
| `crx/cli.py` | 187 | `review` / `scan` / `doctor` subcommands |
| `crx/paths.py` | 138 | Directory expansion, exclude globs, diff path filtering |
| `crx/gitio.py` | 147 | git diff / merge-base. GitPython with subprocess fallback |
| `crx/service.py` | 209 | `ReviewService` — the 5 MCP operations. **No FastMCP import** |
| `crx/mcp.py` | 205 | FastMCP binding only. Tool schemas from type hints + docstrings |

Dependency direction is strictly downward: `mcp → service → pipeline → {chunk,
ground, generate, filter, report, paths, gitio} → {schema, llm, rules, config}`.
`cli` sits alongside `service`. `schema.py` imports nothing from the package.
FastMCP appears in `mcp.py` and nowhere else.

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
