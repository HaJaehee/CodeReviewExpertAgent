"""호출 그래프(Call-Graph) 실행 경로 슬라이싱 및 교차 함수 흐름 위험 분석기 (Stage 3).

청크 단위로 분할된 리뷰에서 단일 함수 경계를 넘어 발생하는 제어·데이터 흐름 결함
(호출자-피호출자 계약 불일치, 다단계 호출 체인의 널/오류 미검사, 예외 전파 시 자원/락 누수,
중첩 락 획득 등)을 탐지하기 위해 다음 3가지를 수행한다.

1. **작업 트리 인접 파일 심볼 탐색 (Workspace Neighbor Resolution)**:
   diff 에 포함되지 않은 저장소 내 인접 파일(동일 디렉터리, 헤더/소스 짝 등)에서
   변경 청크가 호출하는 미해결 피호출 함수(Callee)와 변경된 함수를 호출하는 기존 호출부(Caller)를
   경량 탐색하여 심볼 계약을 보강한다.
2. **다단계 호출 그래프 및 접힌 실행 경로 슬라이스 (Folded Execution Slice)**:
   `상위 호출부(Caller) -> 현재 변경 함수 -> 하위 피호출 함수(Callee, 1~2 hop)` 호출 체인을 구성하고,
   중간 일반 대입문은 접고(`// ... (중간 생략)`) 분기 조건·자원/락 연산·호출문·반환/예외문만 남긴
   실행 경로 슬라이스를 생성한다.
3. **결정론적 교차 함수 흐름 위험 신호 (Cross-Function Flow Hazard Signals)**:
   피호출 함수의 반환/예외/락 계약과 호출부의 실제 사용 패턴을 대조하여
   구체적인 교차 함수 결함 의심 지점을 생성기와 검증기 프롬프트에 주입한다.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .paths import is_excluded
from .schema import Language, LineStatus, ReviewChunk
from .treesitter import SymbolContract, TreeSitterAnalyzer, _bare_symbol_name

log = logging.getLogger(__name__)

#: 인접 작업 트리 파일 탐색 상한 (대규모 저장소에서 지연 시간 방어)
MAX_WORKSPACE_FILES = 40
#: 단일 파일 최대 읽기 크기 (512KB 초과 거대 파일은 스킵)
MAX_NEIGHBOR_FILE_BYTES = 512 * 1024

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "out",
    "target",
    "bin",
    "obj",
    ".vs",
    ".idea",
}

_NULL_OR_ERR_SENTINELS = {
    "nullptr",
    "null",
    "none",
    "false",
    "-1",
    "0",
    "err",
    "error",
    "nan",
}

_BRANCH_OR_GUARD_RE = re.compile(
    r"\b(?:if|else\s+if|switch|case|match|try|catch|except|finally|with|using|lock|"
    r"assert|ensure|guard)\b"
)
_RESOURCE_OR_LOCK_RE = re.compile(
    r"\b(?:new|delete|malloc|calloc|realloc|free|fopen|fclose|open|close|"
    r"lock|unlock|try_lock|lock_guard|unique_lock|scoped_lock|shared_lock|"
    r"Monitor\.Enter|Monitor\.Exit|Dispose|Release| resize|reserve|clear)\b"
)
_RETURN_OR_THROW_RE = re.compile(r"\b(?:return|throw|raise|co_return)\b")
_LOCK_TOKEN_RE = re.compile(
    r"\b(?:lock|mutex|lock_guard|unique_lock|scoped_lock|shared_lock|Monitor\.Enter| Acquire)\b"
)

FLOW_SECTION_HEADER_PROMPT = "- 호출 그래프 실행 경로 슬라이스(Call-Graph Flow):"
FLOW_HAZARD_HEADER_PROMPT = "- 교차 함수 흐름 위험 신호(Flow Hazards):"


class FlowAnalyzer:
    """청크 간 및 작업 트리 인접 파일 간의 호출 그래프와 실행 경로 흐름을 분석한다."""

    def __init__(
        self,
        treesitter_analyzer: TreeSitterAnalyzer | None = None,
        *,
        max_workspace_files: int = MAX_WORKSPACE_FILES,
    ) -> None:
        self.ts_analyzer = treesitter_analyzer or TreeSitterAnalyzer()
        self.max_workspace_files = max_workspace_files

    def enrich(
        self,
        chunks: list[ReviewChunk],
        *,
        file_symbols_by_path: dict[str, dict[str, SymbolContract]] | None = None,
        repo_root: Path | None = None,
        path_exclude: list[str] | None = None,
    ) -> None:
        """청크 목록에 호출 그래프 슬라이스(`flow_context`)와 교차 파일 심볼 정의를 주입한다."""
        if not chunks:
            return

        symbols_by_path: dict[str, dict[str, SymbolContract]] = {}
        if file_symbols_by_path:
            for path, sym_map in file_symbols_by_path.items():
                norm_path = path.replace("\\", "/")
                symbols_by_path[norm_path] = dict(sym_map)
                for sym in sym_map.values():
                    if not sym.path:
                        sym.path = norm_path

        # diff/스캔 파일의 심볼이 비어있다면 청크로부터 기본 심볼 보강
        self._discover_workspace_symbols(
            chunks,
            symbols_by_path=symbols_by_path,
            repo_root=repo_root,
            path_exclude=path_exclude,
        )

        # 전역 심볼 인덱스 및 역방향 호출 그래프(Caller map) 구축
        all_symbols: list[SymbolContract] = []
        by_name: dict[str, list[SymbolContract]] = {}
        seen_sym_keys: set[tuple[str, str, int]] = set()

        for path, sym_map in symbols_by_path.items():
            for sym in sym_map.values():
                if sym.kind in ("class", "struct", "record", "namespace"):
                    continue
                key = (sym.path or path, sym.name, sym.start_line)
                if key in seen_sym_keys:
                    continue
                seen_sym_keys.add(key)
                if not sym.path:
                    sym.path = path
                all_symbols.append(sym)
                by_name.setdefault(sym.name, []).append(sym)
                if sym.bare_name and sym.bare_name != sym.name:
                    by_name.setdefault(sym.bare_name, []).append(sym)

        callers_of: dict[str, list[SymbolContract]] = {}
        for sym in all_symbols:
            for called in sym.calls:
                bare_called = _bare_symbol_name(called)
                if not bare_called:
                    continue
                callers_of.setdefault(bare_called, [])
                if sym not in callers_of[bare_called]:
                    callers_of[bare_called].append(sym)

        for chunk in chunks:
            self._enrich_single_chunk(
                chunk,
                by_name=by_name,
                callers_of=callers_of,
            )

    def _discover_workspace_symbols(
        self,
        chunks: list[ReviewChunk],
        *,
        symbols_by_path: dict[str, dict[str, SymbolContract]],
        repo_root: Path | None,
        path_exclude: list[str] | None,
    ) -> None:
        """저장소 내 미수정 인접 파일에서 피호출 심볼 및 호출부 심볼을 경량 탐색한다."""
        if repo_root is None or not repo_root.is_dir():
            return

        known_bares: set[str] = set()
        for sym_map in symbols_by_path.values():
            for sym in sym_map.values():
                known_bares.add(sym.bare_name)
                known_bares.add(sym.name)

        target_tokens: set[str] = set()
        chunk_paths: set[str] = set()
        chunk_dirs: set[Path] = set()
        chunk_stems: set[str] = set()

        for chunk in chunks:
            norm_path = chunk.path.replace("\\", "/")
            chunk_paths.add(norm_path)
            rel_parent = Path(norm_path).parent
            chunk_dirs.add(repo_root / rel_parent)
            chunk_stems.add(Path(norm_path).stem)

            if chunk.enclosing_symbol:
                enc_bare = _bare_symbol_name(chunk.enclosing_symbol)
                if len(enc_bare) >= 3:
                    target_tokens.add(enc_bare)
            for call in chunk.calls:
                bare = _bare_symbol_name(call)
                if len(bare) >= 3:
                    target_tokens.add(bare)

        if not target_tokens and not chunk_stems:
            return

        candidate_files = self._collect_candidate_files(
            repo_root,
            chunk_paths=chunk_paths,
            chunk_dirs=chunk_dirs,
            chunk_stems=chunk_stems,
            path_exclude=path_exclude,
        )

        scanned = 0
        for rel_path, full_path in candidate_files:
            if scanned >= self.max_workspace_files:
                break
            lang = Language.from_path(rel_path)
            if lang is Language.OTHER:
                continue
            try:
                if full_path.stat().st_size > MAX_NEIGHBOR_FILE_BYTES:
                    continue
                text = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            scanned += 1
            is_counterpart = Path(rel_path).stem in chunk_stems
            if not is_counterpart and not any(tok in text for tok in target_tokens):
                continue

            extracted = self.ts_analyzer.extract_file_symbols(
                text.splitlines(), lang, path=rel_path
            )
            if extracted:
                symbols_by_path[rel_path] = extracted

    def _collect_candidate_files(
        self,
        repo_root: Path,
        *,
        chunk_paths: set[str],
        chunk_dirs: set[Path],
        chunk_stems: set[str],
        path_exclude: list[str] | None,
    ) -> list[tuple[str, Path]]:
        """인접성이 높은 순서(짝 파일 -> 동일 폴더 -> 저장소 기타 파일)로 탐색 후보를 정렬한다."""
        counterparts: list[tuple[str, Path]] = []
        same_dir: list[tuple[str, Path]] = []
        others: list[tuple[str, Path]] = []
        seen: set[str] = set(chunk_paths)

        # 1. 변경된 파일이 위치한 디렉터리 먼저 스캔
        for d in sorted(chunk_dirs):
            if not d.is_dir():
                continue
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                try:
                    rel = entry.relative_to(repo_root).as_posix()
                except ValueError:
                    continue
                if rel in seen:
                    continue
                if Language.from_path(rel) is Language.OTHER:
                    continue
                if path_exclude and is_excluded(rel, path_exclude):
                    continue
                seen.add(rel)
                if entry.stem in chunk_stems:
                    counterparts.append((rel, entry))
                else:
                    same_dir.append((rel, entry))

        # 2. 아직 여유가 있으면 저장소 트리 경량 순회
        budget = self.max_workspace_files * 2
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS and not d.startswith("."))
            for fname in sorted(files):
                full = Path(root) / fname
                try:
                    rel = full.relative_to(repo_root).as_posix()
                except ValueError:
                    continue
                if rel in seen:
                    continue
                if Language.from_path(rel) is Language.OTHER:
                    continue
                if path_exclude and is_excluded(rel, path_exclude):
                    continue
                seen.add(rel)
                if full.stem in chunk_stems:
                    counterparts.append((rel, full))
                else:
                    others.append((rel, full))
                if len(counterparts) + len(same_dir) + len(others) >= budget:
                    return counterparts + same_dir + others

        return counterparts + same_dir + others

    def _enrich_single_chunk(
        self,
        chunk: ReviewChunk,
        *,
        by_name: dict[str, list[SymbolContract]],
        callers_of: dict[str, list[SymbolContract]],
    ) -> None:
        enclosing_name = chunk.enclosing_symbol or ""
        enclosing_bare = _bare_symbol_name(enclosing_name)
        current_sym = self._resolve_symbol(
            enclosing_name, enclosing_bare, chunk.path, by_name, prefer_same_path=True
        )

        # 1. 상위 호출부(Callers) 탐색
        callers: list[SymbolContract] = []
        if enclosing_bare:
            for caller in callers_of.get(enclosing_bare, []):
                if caller.path == chunk.path and caller.bare_name == enclosing_bare:
                    continue
                callers.append(caller)
                self._attach_symbol_def(chunk, caller)

        # 2. 하위 피호출 함수(1-hop & 2-hop Callees) 탐색
        callees_1hop: list[SymbolContract] = []
        callees_2hop: list[tuple[SymbolContract, SymbolContract]] = []
        seen_callees: set[tuple[str, str, int]] = set()

        for call in chunk.calls:
            bare = _bare_symbol_name(call)
            sym = self._resolve_symbol(call, bare, chunk.path, by_name, prefer_same_path=False)
            if sym is None:
                continue
            if sym.path == chunk.path and sym.bare_name == enclosing_bare:
                continue
            key = (sym.path, sym.name, sym.start_line)
            if key in seen_callees:
                continue
            seen_callees.add(key)
            callees_1hop.append(sym)
            self._attach_symbol_def(chunk, sym)

            # 2-hop 피호출 함수 탐색 (1-hop 함수가 내부에서 호출하는 주요 함수)
            for sub_call in sym.calls[:4]:
                sub_bare = _bare_symbol_name(sub_call)
                sub_sym = self._resolve_symbol(
                    sub_call, sub_bare, sym.path, by_name, prefer_same_path=False
                )
                if sub_sym is None:
                    continue
                sub_key = (sub_sym.path, sub_sym.name, sub_sym.start_line)
                if sub_key in seen_callees or (
                    sub_sym.path == chunk.path and sub_sym.bare_name == enclosing_bare
                ):
                    continue
                if sub_sym.returns or sub_sym.throws or sub_sym.resource_ops:
                    callees_2hop.append((sym, sub_sym))
                    self._attach_symbol_def(chunk, sub_sym)
                    break

        # 3. 교차 함수 흐름 위험 신호(Flow Hazards) 탐지
        hazards = self._detect_flow_hazards(
            chunk,
            current_sym=current_sym,
            callers=callers,
            callees_1hop=callees_1hop,
            callees_2hop=callees_2hop,
        )

        # 4. 호출 그래프 실행 경로 슬라이스 렌더링
        flow_lines: list[str] = []
        if callers or callees_1hop:
            flow_lines.append(FLOW_SECTION_HEADER_PROMPT)

            # 호출 체인 요약
            chain_parts: list[str] = []
            if callers:
                caller_labels = ", ".join(
                    f"`{c.name}` ({c.path}:L{c.start_line})" for c in callers[:2]
                )
                chain_parts.append(f"[{caller_labels}]")
            cur_label = f"`{enclosing_name or chunk.path}`"
            chain_parts.append(cur_label)
            if callees_1hop:
                callee_labels: list[str] = []
                for c in callees_1hop[:3]:
                    hop2 = next((sub for p, sub in callees_2hop if p is c), None)
                    if hop2 is not None:
                        callee_labels.append(
                            f"`{c.name}` ({c.path}:L{c.start_line}) → `{hop2.name}` ({hop2.path}:L{hop2.start_line})"
                        )
                    else:
                        callee_labels.append(f"`{c.name}` ({c.path}:L{c.start_line})")
                chain_parts.append("[" + ", ".join(callee_labels) + "]")

            if len(chain_parts) >= 2:
                flow_lines.append("  - [호출 체인] " + " → ".join(chain_parts))

            # 상위 호출부(Caller) 실행 슬라이스
            for caller in callers[:2]:
                sliced = _build_folded_slice(
                    caller,
                    focus_token=enclosing_bare,
                    language=chunk.language,
                )
                if sliced:
                    flow_lines.append(
                        f"  - [상위 호출부 슬라이스] `{caller.signature}` ({caller.path}:L{caller.start_line}-L{caller.end_line}):"
                    )
                    for sline in sliced:
                        flow_lines.append(f"      {sline}")

            # 하위 피호출(Callee) 실행 슬라이스
            for callee in callees_1hop[:3]:
                # 동일 파일 내 바로 인접한 함수더라도 핵심 분기/반환 슬라이스를 요약 제공
                sliced = _build_folded_slice(
                    callee,
                    focus_token=None,
                    language=chunk.language,
                )
                if sliced:
                    flow_lines.append(
                        f"  - [하위 피호출 슬라이스] `{callee.signature}` ({callee.path}:L{callee.start_line}-L{callee.end_line}):"
                    )
                    for sline in sliced:
                        flow_lines.append(f"      {sline}")

            for parent_callee, sub_callee in callees_2hop[:2]:
                sliced = _build_folded_slice(
                    sub_callee,
                    focus_token=None,
                    language=chunk.language,
                )
                if sliced:
                    flow_lines.append(
                        f"  - [2단계 피호출 슬라이스 (`{parent_callee.bare_name}` 경유)] "
                        f"`{sub_callee.signature}` ({sub_callee.path}:L{sub_callee.start_line}-L{sub_callee.end_line}):"
                    )
                    for sline in sliced:
                        flow_lines.append(f"      {sline}")

        if hazards:
            flow_lines.append(FLOW_HAZARD_HEADER_PROMPT)
            for h in hazards[:5]:
                flow_lines.append(f"  - {h}")

        if flow_lines:
            chunk.flow_context = "\n".join(flow_lines)

    @staticmethod
    def _resolve_symbol(
        name: str,
        bare_name: str,
        chunk_path: str,
        by_name: dict[str, list[SymbolContract]],
        *,
        prefer_same_path: bool,
    ) -> SymbolContract | None:
        candidates = by_name.get(name) or by_name.get(bare_name) or []
        if not candidates:
            return None
        if prefer_same_path:
            for c in candidates:
                if c.path == chunk_path:
                    return c
        else:
            # 교차 파일 심볼이 있으면 우선하되, 없으면 동일 파일 심볼 반환
            for c in candidates:
                if c.path != chunk_path:
                    return c
        return candidates[0]

    @staticmethod
    def _attach_symbol_def(chunk: ReviewChunk, sym: SymbolContract) -> None:
        if not sym.definition_snippet:
            return
        header = f"// {sym.path}:L{sym.start_line}-L{sym.end_line}\n" if sym.path else ""
        rendered = f"{header}{sym.definition_snippet}"
        if sym.bare_name:
            chunk.symbol_definitions.setdefault(sym.bare_name, rendered)
        if sym.name:
            chunk.symbol_definitions.setdefault(sym.name, rendered)

    def _detect_flow_hazards(
        self,
        chunk: ReviewChunk,
        *,
        current_sym: SymbolContract | None,
        callers: list[SymbolContract],
        callees_1hop: list[SymbolContract],
        callees_2hop: list[tuple[SymbolContract, SymbolContract]],
    ) -> list[str]:
        """호출부-피호출부 계약 및 변경 라인 사용 패턴을 대조하여 흐름 위험 신호를 생성한다."""
        hazards: list[str] = []
        active_lines = [
            ln for ln in chunk.lines if ln.status in (LineStatus.ADDED, LineStatus.UNCHANGED)
        ]
        added_lines = [ln for ln in chunk.lines if ln.status is LineStatus.ADDED]
        target_lines = added_lines if added_lines else active_lines
        chunk_text = "\n".join(ln.text for ln in active_lines)

        # 2-hop 위험을 1-hop 에 전이하여 함께 검사
        hop2_by_parent: dict[int, SymbolContract] = {
            id(parent): sub for parent, sub in callees_2hop
        }

        for callee in callees_1hop:
            sub = hop2_by_parent.get(id(callee))
            effective_returns = list(callee.returns)
            effective_throws = list(callee.throws)
            effective_ops = list(callee.resource_ops)
            via_note = ""
            if sub is not None:
                for r in sub.returns:
                    if r not in effective_returns:
                        effective_returns.append(r)
                for t in sub.throws:
                    if t not in effective_throws:
                        effective_throws.append(t)
                for op in sub.resource_ops:
                    if op not in effective_ops:
                        effective_ops.append(op)
                via_note = f" (내부 호출 `{sub.bare_name}` 포함)"

            # 1) 널/오류 반환 미검사 역참조 또는 사용 위험
            null_or_err = [
                r for r in effective_returns if _is_null_or_error_return(r)
            ]
            if null_or_err:
                call_Usage = _find_unchecked_call_usage(
                    callee.bare_name, target_lines, active_lines
                )
                if call_Usage is not None:
                    lineno, detail = call_Usage
                    ret_list = ", ".join(f"`{r}`" for r in null_or_err[:2])
                    hazards.append(
                        f"[널/오류 반환 미검사 위험] L{lineno}: 피호출 함수 `{callee.name}` "
                        f"({callee.path}:L{callee.start_line}){via_note} 의 실패/널 반환({ret_list}) 가능성에도 "
                        f"{detail} — 역참조 또는 오류 전파 누락 확인 필요"
                    )

            # 2) 피호출 함수 예외 발생 시 호출부 자원/락 누수 또는 미처리 위험
            if effective_throws:
                call_ln = _find_call_line(callee.bare_name, target_lines)
                if call_ln is not None:
                    has_try = bool(re.search(r"\b(?:try|catch|except)\b", chunk_text))
                    has_resource_before = _has_unguarded_resource_before(
                        active_lines, call_ln
                    )
                    thr_list = ", ".join(f"`{t}`" for t in effective_throws[:2])
                    if has_resource_before:
                        hazards.append(
                            f"[예외 전파 및 자원/락 누수 위험] L{call_ln}: `{callee.name}` "
                            f"({callee.path}:L{callee.start_line}){via_note} 호출 시 예외({thr_list}) 발생 가능 — "
                            f"선행 할당된 자원/락 해제 누락 위험"
                        )
                    elif not has_try:
                        hazards.append(
                            f"[교차 함수 예외 전파 위험] L{call_ln}: 피호출 함수 `{callee.name}` "
                            f"({callee.path}:L{callee.start_line}){via_note} 의 예외({thr_list}) 미포착 전파 가능성 확인 필요"
                        )

            # 3) 중첩 락 획득 / 동시성 위험
            callee_locks = [op for op in effective_ops if _LOCK_TOKEN_RE.search(op)]
            caller_locks = bool(_LOCK_TOKEN_RE.search(chunk_text))
            if callee_locks and caller_locks:
                call_ln = _find_call_line(callee.bare_name, target_lines)
                if call_ln is not None:
                    hazards.append(
                        f"[중첩 락/동시성 위험] L{call_ln}: 현재 함수 내 락 보유 상태에서 "
                        f"내부 락 연산(`{callee_locks[0]}`)을 수행하는 `{callee.name}` "
                        f"({callee.path}:L{callee.start_line}){via_note} 호출 — 데드락 또는 락 순서 역전 확인 필요"
                    )

        # 4) 상위 호출부(Caller)와의 계약 불일치 위험 (현재 함수의 반환/예외 계약 vs 기존 호출부)
        if callers and current_sym is not None:
            cur_null_err = [r for r in current_sym.returns if _is_null_or_error_return(r)]
            cur_throws = list(current_sym.throws)
            if cur_null_err or cur_throws:
                for caller in callers[:2]:
                    caller_unchecked = _caller_ignores_return_or_exception(
                        caller, current_sym.bare_name, has_null_err=bool(cur_null_err), has_throws=bool(cur_throws)
                    )
                    if caller_unchecked:
                        contract_desc: list[str] = []
                        if cur_null_err:
                            contract_desc.append(
                                "반환 " + ", ".join(f"`{r}`" for r in cur_null_err[:2])
                            )
                        if cur_throws:
                            contract_desc.append(
                                "예외 " + ", ".join(f"`{t}`" for t in cur_throws[:2])
                            )
                        hazards.append(
                            f"[상위 호출부 계약 불일치 위험] 현재 함수 `{current_sym.name}` 의 "
                            f"{' / '.join(contract_desc)} 계약에 대해 상위 호출부 `{caller.name}` "
                            f"({caller.path}:L{caller.start_line}) 에서 {caller_unchecked} — 호출부 연쇄 결함 확인 필요"
                        )

        return hazards


def _build_folded_slice(
    sym: SymbolContract,
    *,
    focus_token: str | None = None,
    language: Language = Language.CPP,
    max_lines: int = 9,
) -> list[str]:
    """함수 정의 스니펫에서 시그니처·분기·자원/락·호출·반환 라인만 남기고 중간을 접는다."""
    if not sym.definition_snippet:
        return []
    raw_lines = sym.definition_snippet.splitlines()
    if len(raw_lines) <= 4:
        return raw_lines

    comment_prefix = "#" if language is Language.PYTHON else "//"
    ellipsis_line = f"    {comment_prefix} ... (중간 생략)"
    line_prefix_re = re.compile(r"^L\d+:\s*")

    kept_indices: set[int] = {0}
    last_body = line_prefix_re.sub("", raw_lines[-1]).strip()
    if len(raw_lines) > 1 and last_body in ("}", "};"):
        kept_indices.add(len(raw_lines) - 1)

    for idx, line in enumerate(raw_lines[1:], start=1):
        stripped = line_prefix_re.sub("", line).strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*")):
            continue
        is_important = False
        if focus_token and re.search(rf"\b{re.escape(focus_token)}\b", stripped):
            is_important = True
            # 호출 직후 1~2줄(반환값 검사 여부)도 함께 보존
            if idx + 1 < len(raw_lines):
                kept_indices.add(idx + 1)
            if idx + 2 < len(raw_lines):
                kept_indices.add(idx + 2)
        elif _RETURN_OR_THROW_RE.search(stripped):
            is_important = True
        elif _BRANCH_OR_GUARD_RE.search(stripped):
            is_important = True
        elif _RESOURCE_OR_LOCK_RE.search(stripped):
            is_important = True

        if is_important:
            kept_indices.add(idx)

    ordered = sorted(kept_indices)
    result: list[str] = []
    prev_idx = -1
    for idx in ordered:
        if prev_idx != -1 and idx > prev_idx + 1:
            if not result or result[-1] != ellipsis_line:
                result.append(ellipsis_line)
        result.append(raw_lines[idx])
        prev_idx = idx
        if len(result) >= max_lines:
            if idx < len(raw_lines) - 1 and result[-1] != ellipsis_line:
                result.append(ellipsis_line)
            break

    return result


def _is_null_or_error_return(ret_expr: str) -> bool:
    cleaned = ret_expr.strip().strip("()").lower()
    if cleaned in _NULL_OR_ERR_SENTINELS:
        return True
    for tok in ("nullptr", "null", "none", "false", "-1", "err", "fail"):
        if re.search(rf"\b{re.escape(tok)}\b", cleaned):
            return True
    return False


def _find_call_line(callee_bare: str, lines: list) -> int | None:
    if not callee_bare:
        return None
    pat = re.compile(rf"\b{re.escape(callee_bare)}\s*\(")
    for ln in lines:
        if pat.search(ln.text):
            return ln.lineno
    return None


def _find_unchecked_call_usage(
    callee_bare: str,
    target_lines: list,
    all_lines: list,
) -> tuple[int, str] | None:
    """피호출 함수의 반환값을 변수에 받은 뒤 널/오류 검사 없이 역참조·사용하는 패턴을 찾는다."""
    if not callee_bare:
        return None
    call_pat = re.compile(rf"\b{re.escape(callee_bare)}\s*\(")
    assign_pat = re.compile(
        rf"(?:(?:const|auto|var|let|[\w:<>*&]+)\s+)?([A-Za-z_]\w*)\s*=\s*[^;]*\b{re.escape(callee_bare)}\s*\("
    )
    direct_chain_pat = re.compile(
        rf"\b{re.escape(callee_bare)}\s*\([^)]*\)\s*(?:->|\.|\[)"
    )

    by_lineno = {ln.lineno: idx for idx, ln in enumerate(all_lines)}

    for ln in target_lines:
        text = ln.text
        if not call_pat.search(text):
            continue

        # 1) 호출 직후 체이닝 역참조: `callee()->field` 또는 `callee().attr`
        if direct_chain_pat.search(text):
            return ln.lineno, "반환값 즉시 역참조/체이닝 호출 감지됨"

        # 2) 조건문 내부에서 바로 호출 및 검사하는 경우는 안전
        if re.search(r"\b(?:if|while|assert|return)\b", text):
            continue

        m = assign_pat.search(text)
        if m:
            var_name = m.group(1)
            start_idx = by_lineno.get(ln.lineno, 0)
            window_lines = all_lines[start_idx + 1 : start_idx + 7]
            explicit_null_guard_pat = re.compile(
                rf"\b{re.escape(var_name)}\s*(?:==|!=|<|<=|>|>=|\bis\b)|"
                rf"!\s*{re.escape(var_name)}\b|"
                rf"\b(?:if|while|assert)\s*\(?\s*{re.escape(var_name)}\s*(?:\)|:|\band\b|\bor\b|\&\&|\|\|)"
            )
            deref_pat = re.compile(
                rf"(?:\*\s*{re.escape(var_name)}\b|\b{re.escape(var_name)}\s*(?:->|\.|\[))"
            )
            guarded = False
            for wln in window_lines:
                if explicit_null_guard_pat.search(wln.text):
                    guarded = True
                    break
                if deref_pat.search(wln.text):
                    return wln.lineno, f"반환 변수 `{var_name}` 에 대한 널/오류 검사 없는 역참조 감지됨"
            if not guarded and window_lines:
                for wln in window_lines:
                    if re.search(rf"\b{re.escape(var_name)}\b", wln.text):
                        return wln.lineno, f"반환 변수 `{var_name}` 의 널/오류 확인 없이 후속 연산에 사용됨"
        else:
            # 반환값을 아예 무시하고 단독 호출한 경우
            stripped = text.strip()
            if not stripped.startswith(("return ", "if ", "while ", "assert")):
                return ln.lineno, "반환값 저장 및 오류 확인 없는 단독 호출 감지됨"

    return None


def _has_unguarded_resource_before(all_lines: list, call_lineno: int) -> bool:
    """call_lineno 이전에 스마트 포인터/RAII 가 아닌 원시 자원 할당이나 수동 락이 있는지 확인한다."""
    raw_alloc_re = re.compile(
        r"(?:\bnew\s+[A-Za-z_]|\b(?:malloc|calloc|fopen)\s*\(|\.\s*lock\s*\(|\bMonitor\.Enter\s*\()"
    )
    raii_re = re.compile(
        r"\b(?:unique_ptr|shared_ptr|make_unique|make_shared|lock_guard|unique_lock|scoped_lock|using|with)\b"
    )
    for ln in all_lines:
        if ln.lineno >= call_lineno:
            break
        if raw_alloc_re.search(ln.text) and not raii_re.search(ln.text):
            return True
    return False


def _caller_ignores_return_or_exception(
    caller: SymbolContract,
    callee_bare: str,
    *,
    has_null_err: bool,
    has_throws: bool,
) -> str:
    """상위 호출부 스니펫에서 피호출 함수의 반환값/예외 미처리 정황을 점검한다."""
    snippet = caller.definition_snippet
    if not snippet or not callee_bare:
        return ""
    lines = snippet.splitlines()
    call_pat = re.compile(rf"\b{re.escape(callee_bare)}\s*\(")
    assign_pat = re.compile(
        rf"(?:(?:const|auto|var|let|[\w:<>*&]+)\s+)?([A-Za-z_]\w*)\s*=\s*[^;]*\b{re.escape(callee_bare)}\s*\("
    )

    for idx, line in enumerate(lines):
        if not call_pat.search(line):
            continue
        if has_null_err:
            if re.search(rf"\b{re.escape(callee_bare)}\s*\([^)]*\)\s*(?:->|\.|\[)", line):
                return "반환값 즉시 역참조 감지됨"
            m = assign_pat.search(line)
            if m:
                var_name = m.group(1)
                tail = "\n".join(lines[idx + 1 : idx + 6])
                guard_pat = re.compile(
                    rf"\b(?:if|while|assert|switch)\b[^;]*\b{re.escape(var_name)}\b|"
                    rf"\b{re.escape(var_name)}\s*(?:==|!=|<|<=|>|>=|\bis\b)|"
                    rf"!\s*{re.escape(var_name)}\b"
                )
                if not guard_pat.search(tail):
                    return f"반환 변수 `{var_name}` 미검사 사용 감지됨"
            elif not re.search(r"\b(?:if|while|return|assert)\b", line):
                return "반환값 미확인 호출 감지됨"

        if has_throws and not re.search(r"\b(?:try|catch|except)\b", snippet):
            return "예외 미포착 호출 감지됨"

    return ""
