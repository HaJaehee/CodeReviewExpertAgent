# CREX — Agent Orientation Wiki

English reference for AI agents working on this codebase.
The user-facing manual in [`docs/`](../docs/index.md) is Korean; this wiki is not
a translation of it. It covers what an agent needs to make correct changes.

## What this project is

CREX reviews C++/C#/Python code changes using small local LLMs (25–40B, typically
Qwen3.6-27B and Gemma 4 26B) served by vLLM inside a corporate air-gapped network.

The entire design optimizes for one thing: **suppressing hallucination**. Small
models confidently invent line numbers, non-existent APIs, and defects that aren't
there. A false positive costs more than a miss, so the project trades recall for
precision deliberately and says so out loud.

## Read these in order

| Doc | Covers |
|---|---|
| [architecture.md](architecture.md) | Pipeline stages, data flow, module map |
| [design-decisions.md](design-decisions.md) | Why each choice was made, with research basis |
| [invariants.md](invariants.md) | **Rules you must not break when editing** |
| [roadmap.md](roadmap.md) | What exists, what doesn't, what's next |

If you only read one, read [invariants.md](invariants.md). Several parts of this
system look arbitrary but are load-bearing.

For how the tool is meant to be *used*, see [`docs/workflow.md`](../docs/workflow.md).
It is MCP-first: developers call the review tools from Zed's agent panel, and the
CLI is documented there only as maintainer tooling (golden set, rule tuning).

## Fast facts

- **Python 3.11+**. Core is stdlib-only; only the MCP server needs wheels
  (`requirements.txt`: FastMCP, GitPython). tree-sitter and GitPython both have
  working fallbacks — see [invariants.md](invariants.md).
- **~6,137 lines** across 20 modules in `crex/`.
- **180 tests**, all runnable without an LLM or network: `python tests/run_all.py`
- **41 rules** in `rules/taxonomy.toml` (C++ 14, C# 15, Python 14).
- Entry point: `python -m crex {review|scan|doctor|workspace}`

## Language conventions

This matters — don't normalize it away.

| Where | Language |
|---|---|
| Code comments, docstrings | Korean |
| `docs/` user manual | Korean |
| LLM prompts (system/user templates) | Korean |
| Log messages, CLI output, error messages | Korean |
| Identifiers, type names, rule IDs | English |
| `wiki/` (this folder) | English |

The user is a Korean-speaking engineer. Keep new code comments in Korean and match
the surrounding tone: direct, explaining *why* rather than restating *what*.

## Orientation by task

**Adding a review rule** → `rules/taxonomy.toml`, then read
[`docs/writing-rules.md`](../docs/writing-rules.md). Rule IDs are join keys for
evaluation statistics; never rename one.

**Changing chunking** → `crex/chunk.py`. Read the expansion-cap section in
[design-decisions.md](design-decisions.md) first; the 4×/3× numbers come from
published production practice, not guesswork.

**Touching prompts or schemas** → `crex/generate.py` and `crex/filter.py`.
The JSON Schema property order in `VERDICT_SCHEMA` is load-bearing
(see [invariants.md](invariants.md#conclusion-first-property-order)).

**Adding a static analyzer** → subclass `Analyzer` in `crex/ground.py`; implement
`build_command()` and `parse()` only. Register it in `DEFAULT_ANALYZERS` (runs
automatically) or `OPTIONAL_ANALYZERS` (opt-in by name).

**Adding an output format** → `crex/report.py`.

**Adding or changing an MCP tool** → the operation goes in `crex/service.py`
(`ReviewService`), the binding in `crex/mcp.py`. Keep the split: `service.py` must
not import FastMCP, or the tests stop running on a bare interpreter. Tool schemas
come from type hints and docstrings, so the docstring *is* the spec the model reads.

**Changing git access** → `crex/gitio.py`. Both the GitPython path and the
subprocess fallback must return identical unified-diff text; the chunker consumes
only that.
