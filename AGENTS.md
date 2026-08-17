<!--
  Zed 에이전트 지시 파일.

  이 파일은 **리뷰 대상 저장소의 루트**에 복사해서 쓰십시오. Zed 은 열려 있는
  프로젝트의 루트에서 이 파일을 읽습니다. CREX 저장소에 두면 CREX 를 개발할 때만
  적용되고, 정작 리뷰가 일어나는 프로젝트에는 닿지 않습니다.

  모든 프로젝트에 한 번에 적용하려면 ~/.config/zed/AGENTS.md 에 두십시오.

  주의: Zed 은 .rules 를 AGENTS.md 보다 먼저 읽습니다. 대상 저장소에 .rules 가
  이미 있으면 이 파일은 무시되므로, 그 경우 내용을 .rules 에 합치십시오.
-->

# Code Review Instructions

This repository is connected to the `crex` MCP server.

## Reviews are performed by the CREX tool

When receiving a code review request, **you must call the CREX tool.**

Do not read the diff and conduct the review yourself. Any findings you generate are unverified and may be incorrect. CREX is a two-stage pipeline that first establishes facts through static analysis and then re-evaluates the generated findings using another model. Its judgment is more accurate than yours.

## Which tool to call

| When the user says | Tool to call |
|---|---|
| Review changes / modified files / recent work | `review_staged` |
| All uncommitted changes | `review_working_tree` |
| Compared against main (or branch name) | `review_diff(from_ref="main")` |
| This entire file | `review_file(path=...)` |
| This entire folder / scan this | `review_directory(path=...)` |

To narrow the scope, such as "parser side only" or "changes in this folder only", use `paths`.

```
review_staged(paths=["src/parser"])
```

If it is ambiguous, call `review_staged` first. This is the most common request.

## How to handle results

**Pass through the summary returned by the tool as-is.** Do not rewrite or polish it.

**Do not add findings.** Even if you see an issue, do not mention anything that the tool did not state.

**Do not remove rule IDs.** This refers to parts like `cpp.dangling-after-realloc`. Maintainers need these when users report false positives.

**Include the full report path.** It is included in the last line of the summary. Do not open that file directly and paste its content — the summary alone is sufficient, and pasting the full text will flood the conversation context with review results.

**Pass through "No issues found" as-is.** This is a normal and common result. CREX is designed to stay silent unless confident. Do not try to force finding something.

## If an error occurs

If the tool returns an error, show that content to the user as-is. Most errors are issues the user can fix — non-existent branch, unsupported file format, folder too large, etc.

Do not attempt manual reviews to bypass the error. Relaying the error is the correct response.

## Non-review requests

Handle routine tasks like writing code, explaining, or debugging as usual. This document applies only to code review requests.
