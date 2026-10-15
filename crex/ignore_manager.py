"""저장소 루트 .crexignore 관리자.

.crexignore 파일의 조회, 패턴 파싱, 추가, 삭제, 지적(Finding) 매칭 판별을 수행한다.
CLI, MCP, Web 관제 화면(viz)에서 공용으로 쓸 수 있도록 설계되었다.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rules import _is_path_matching
from .service import ReviewRequestError


@dataclass
class IgnorePattern:
    raw: str
    pattern_type: str  # "path" | "global_rule" | "path_rule"
    pattern: str
    path: str | None = None
    rule: str | None = None
    line_no: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "type": self.pattern_type,
            "pattern": self.pattern,
            "path": self.path,
            "rule": self.rule,
            "line_no": self.line_no,
        }


def parse_ignore_line(raw_line: str, line_no: int = 0) -> IgnorePattern | None:
    """한 줄을 파싱하여 IgnorePattern 객체를 생성한다. 주석이거나 빈 줄이면 None."""
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    norm = line.replace("\\", "/")

    # 1. rule:xxx (전역 룰 제외)
    if norm.lower().startswith("rule:"):
        rule_id = norm[5:].strip()
        return IgnorePattern(
            raw=line,
            pattern_type="global_rule",
            pattern=f"rule:{rule_id}",
            rule=rule_id,
            line_no=line_no,
        )

    # 2. path_pattern rule:rule_pattern (경로 한정 룰)
    if " rule:" in norm.lower():
        idx = norm.lower().find(" rule:")
        path_part = norm[:idx].strip()
        rule_part = norm[idx + 6:].strip()
        return IgnorePattern(
            raw=line,
            pattern_type="path_rule",
            pattern=f"{path_part} rule:{rule_part}",
            path=path_part,
            rule=rule_part,
            line_no=line_no,
        )

    # 3. path_pattern:rule:rule_pattern (경로 한정 룰)
    if ":rule:" in norm.lower():
        idx = norm.lower().find(":rule:")
        path_part = norm[:idx].strip()
        rule_part = norm[idx + 6:].strip()
        return IgnorePattern(
            raw=line,
            pattern_type="path_rule",
            pattern=f"{path_part}:rule:{rule_part}",
            path=path_part,
            rule=rule_part,
            line_no=line_no,
        )

    # 4. path_pattern:rule_pattern (경로 한정 룰)
    if ":" in norm:
        parts = norm.rsplit(":", 1)
        path_part, rule_part = parts[0].strip(), parts[1].strip()
        if not (len(path_part) == 1 and path_part.isalpha()):
            return IgnorePattern(
                raw=line,
                pattern_type="path_rule",
                pattern=f"{path_part}:{rule_part}",
                path=path_part,
                rule=rule_part,
                line_no=line_no,
            )

    # 5. 파일 / 디렉터리 경로 패턴
    return IgnorePattern(
        raw=line,
        pattern_type="path",
        pattern=norm,
        path=norm,
        line_no=line_no,
    )


class CrexIgnoreManager:
    """워크스페이스 루트의 .crexignore 파일을 안전하게 읽고 수정한다."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.file_path = self.workspace_root / ".crexignore"

    def exists(self) -> bool:
        return self.file_path.is_file()

    def read_raw(self) -> str:
        if not self.exists():
            return ""
        try:
            return self.file_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ReviewRequestError(f".crexignore 파일을 읽을 수 없습니다: {exc}") from exc

    def list_patterns(self) -> list[IgnorePattern]:
        """파싱된 모든 활성 패턴 목록을 반환한다."""
        raw = self.read_raw()
        patterns: list[IgnorePattern] = []
        for line_no, line in enumerate(raw.splitlines(), start=1):
            parsed = parse_ignore_line(line, line_no=line_no)
            if parsed is not None:
                patterns.append(parsed)
        return patterns

    def find_match(self, file_path: str, rule_id: str) -> str | None:
        """주어진 파일 경로와 룰 ID에 대해 .crexignore 에서 매칭되는 패턴 원문을 반환한다.

        가장 구체적인 패턴(경로 한정 룰) -> 전역 룰 -> 파일 경로 순으로 검사한다.
        매칭되는 규칙이 없으면 None을 반환한다.
        """
        patterns = self.list_patterns()
        norm_path = file_path.replace("\\", "/").strip("/")

        # 1. 경로 한정 룰 패턴
        for p in patterns:
            if p.pattern_type == "path_rule" and p.path and p.rule:
                if _is_path_matching(norm_path, p.path):
                    if p.rule == rule_id or fnmatch.fnmatch(rule_id, p.rule):
                        return p.raw

        # 2. 전역 룰 패턴
        for p in patterns:
            if p.pattern_type == "global_rule" and p.rule:
                if p.rule == rule_id or fnmatch.fnmatch(rule_id, p.rule):
                    return p.raw

        # 3. 파일 및 디렉터리 경로 패턴
        for p in patterns:
            if p.pattern_type == "path" and p.path:
                if _is_path_matching(norm_path, p.path):
                    return p.raw

        return None

    def add_pattern(self, pattern: str, comment: str | None = None) -> str:
        """새 패턴을 .crexignore 에 추가한다. 이미 존재하면 기존 패턴을 반환한다."""
        clean = pattern.strip().replace("\\", "/")
        if not clean:
            raise ReviewRequestError("제외 패턴을 입력하십시오")

        # 상위 디렉터리 탈출 검사
        if ".." in clean.split("/"):
            raise ReviewRequestError("상위 디렉터리(..)로 벗어나는 경로는 사용할 수 없습니다")

        raw = self.read_raw()
        lines = raw.splitlines()

        # 이미 존재하는지 확인
        for line in lines:
            line_clean = line.strip().replace("\\", "/")
            if line_clean == clean:
                return clean
            if clean.lower().startswith("rule:") and line_clean.lower() == clean.lower():
                return clean

        # 새 라인 추가
        newline = "\r\n" if "\r\n" in raw else "\n"
        to_add: list[str] = []
        if comment:
            to_add.append(f"# {comment}")
        to_add.append(clean)

        new_content = raw
        if new_content and not new_content.endswith(("\r\n", "\n")):
            new_content += newline
        new_content += newline.join(to_add) + newline

        try:
            self.file_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise ReviewRequestError(f".crexignore 파일에 쓰지 못했습니다: {exc}") from exc

        return clean

    def remove_pattern(self, pattern: str) -> bool:
        """패턴 라인을 찾아 .crexignore 에서 삭제한다.

        바로 윗줄에 viz 자동 생성 주석(# viz: ...)이 있다면 함께 정리한다.
        """
        clean = pattern.strip().replace("\\", "/")
        if not clean or not self.exists():
            return False

        raw = self.read_raw()
        lines = raw.splitlines()
        newline = "\r\n" if "\r\n" in raw else "\n"

        target_idx: int | None = None
        for i, line in enumerate(lines):
            l_clean = line.strip().replace("\\", "/")
            if l_clean == clean:
                target_idx = i
                break
            if clean.lower().startswith("rule:") and l_clean.lower() == clean.lower():
                target_idx = i
                break
            # 'rule:xxx' vs 'xxx' 매칭 지원
            if clean.lower() == f"rule:{l_clean.lower()}":
                target_idx = i
                break
            if l_clean.lower() == f"rule:{clean.lower()}":
                target_idx = i
                break

        if target_idx is None:
            return False

        # 바로 윗줄이 viz 자동 주석이면 함께 제거
        indices_to_remove = {target_idx}
        if target_idx > 0:
            prev = lines[target_idx - 1].strip()
            if prev.startswith("# viz:") or prev.startswith("# [viz]"):
                indices_to_remove.add(target_idx - 1)

        new_lines = [l for i, l in enumerate(lines) if i not in indices_to_remove]
        new_content = newline.join(new_lines)
        if new_lines:
            new_content += newline

        try:
            self.file_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise ReviewRequestError(f".crexignore 파일을 갱신하지 못했습니다: {exc}") from exc

        return True
