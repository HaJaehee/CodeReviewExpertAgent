"""경로 확장과 필터.

폴더를 리뷰 대상 파일 목록으로 펼치고, diff 리뷰를 특정 경로로 좁힌다.
MCP 도구와 CLI 가 공유한다.

폴더 감사는 비용이 급격히 커진다. 파일 하나가 청크 여러 개가 되고, 청크마다
LLM 호출이 최대 두 번이다. 200개 파일짜리 디렉터리를 무심코 지정하면 몇 시간이
걸린다. 그래서 개수 상한을 두고, 넘으면 조용히 자르지 않고 알린다.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from .rules import DEFAULT_EXCLUDE
from .schema import FileDiff, Language

#: 폴더 감사 한 번에 볼 수 있는 파일 개수 상한.
MAX_SCAN_FILES = 40


class TooManyFiles(RuntimeError):
    """상한을 넘는 폴더를 감사하려 했다."""


@dataclass
class PathExpansion:
    files: list[str] = field(default_factory=list)
    #: 지원하지 않는 언어라 제외된 개수.
    skipped_unsupported: int = 0
    #: exclude glob 에 걸려 제외된 개수.
    skipped_excluded: int = 0

    def summary(self) -> str:
        parts = [f"파일 {len(self.files)}개"]
        if self.skipped_unsupported:
            parts.append(f"미지원 언어 {self.skipped_unsupported}개 제외")
        if self.skipped_excluded:
            parts.append(f"exclude 규칙으로 {self.skipped_excluded}개 제외")
        return ", ".join(parts)


def expand_paths(
    repo_root: Path,
    targets: list[str],
    *,
    recursive: bool = True,
    exclude: list[str] | None = None,
    max_files: int = MAX_SCAN_FILES,
) -> PathExpansion:
    """파일·폴더 경로를 리뷰 가능한 파일 목록으로 펼친다.

    반환 경로는 항상 repo_root 기준 상대경로이고 `/` 구분자를 쓴다 —
    청크와 정적분석 결과를 맞추는 키가 되므로 표기가 흔들리면 안 된다.
    """
    patterns = DEFAULT_EXCLUDE if exclude is None else exclude
    expansion = PathExpansion()
    seen: set[str] = set()

    for target in targets:
        absolute = (repo_root / target).resolve()

        if absolute.is_file():
            candidates = [absolute]
        elif absolute.is_dir():
            candidates = sorted(absolute.rglob("*") if recursive else absolute.glob("*"))
        else:
            raise FileNotFoundError(f"경로를 찾을 수 없습니다: {target}")

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                relative = candidate.relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                # 저장소 밖의 경로. 정적분석 결과와 매칭이 안 되므로 받지 않는다.
                raise ValueError(f"저장소 밖의 경로입니다: {candidate}") from None

            if relative in seen:
                continue
            seen.add(relative)

            if Language.from_path(relative) is Language.OTHER:
                expansion.skipped_unsupported += 1
                continue
            if is_excluded(relative, patterns):
                expansion.skipped_excluded += 1
                continue

            expansion.files.append(relative)

    if len(expansion.files) > max_files:
        raise TooManyFiles(
            f"대상 파일이 {len(expansion.files)}개로 상한 {max_files}개를 넘습니다. "
            f"폴더 감사는 파일당 LLM 호출이 여러 번이라 비용이 큽니다. "
            f"하위 폴더를 나눠 지정하거나 recursive 를 끄고 다시 시도하십시오."
        )

    return expansion


def is_excluded(path: str, patterns: list[str]) -> bool:
    """glob 패턴 중 하나라도 걸리면 제외한다.

    `**/generated/**` 같은 패턴은 fnmatch 가 그대로 처리하지 못하므로
    경로 앞에 접두어를 붙여 한 번 더 확인한다.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("/" + path, pattern):
            return True
        # `**/x/**` 는 최상위의 `x/` 도 포함해야 한다.
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False


def filter_file_diffs(file_diffs: list[FileDiff], targets: list[str]) -> list[FileDiff]:
    """diff 를 특정 파일·폴더로 좁힌다.

    큰 MR 에서 "내가 건드린 파서 쪽만 보자"에 쓴다. 폴더를 지정하면 그 아래
    전부, 파일을 지정하면 그 파일만 남는다.
    """
    if not targets:
        return file_diffs

    normalized = [t.replace("\\", "/").rstrip("/") for t in targets]
    kept: list[FileDiff] = []

    for file_diff in file_diffs:
        path = file_diff.path.replace("\\", "/")
        for target in normalized:
            if path == target or path.startswith(target + "/") or fnmatch.fnmatch(path, target):
                kept.append(file_diff)
                break

    return kept
