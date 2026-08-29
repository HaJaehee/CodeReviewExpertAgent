"""git 접근.

GitPython 이 있으면 쓰고, 없으면 subprocess 로 떨어진다.

폴백을 남겨두는 이유가 있다. 코어 파이프라인은 여전히 의존성 없이 돌아야 한다 —
반입 직후 `python tests/run_all.py` 로 무결성을 확인하는 절차가 pip install 을
전제하면 곤란하다. MCP 서버는 FastMCP 가 필요하지만 CLI 는 그렇지 않다.

두 경로 모두 **같은 unified diff 텍스트**를 돌려준다. 청커가 그것만 먹기 때문에
어느 쪽으로 왔는지 하위 단계는 알 필요가 없다.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: 청킹이 함수 경계까지 확장하므로 문맥은 3줄이면 충분하다. 늘리면 토큰만 먹는다.
CONTEXT_LINES = 3

_BASE_ARGS = ("--no-color", "--no-ext-diff", f"-U{CONTEXT_LINES}")


class GitError(RuntimeError):
    """git 작업 실패. 사용자가 고칠 수 있는 문제를 담는다."""


def gitpython_available() -> bool:
    try:
        import git  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------


def diff_range(repo_root: Path, from_ref: str, to_ref: str = "HEAD") -> str:
    """두 참조 사이의 diff. MR 리뷰에 쓴다."""
    return _diff(repo_root, [from_ref, to_ref])


def diff_staged(repo_root: Path) -> str:
    """스테이징된 변경. 커밋 전 자가 점검에 쓴다."""
    return _diff(repo_root, ["--cached"])


def diff_working_tree(repo_root: Path) -> str:
    """커밋하지 않은 모든 변경."""
    return _diff(repo_root, ["HEAD"])


def merge_base(repo_root: Path, ref: str, other: str = "HEAD") -> str:
    """두 참조의 실제 분기점.

    `--from main` 을 그대로 쓰면 브랜치가 오래됐을 때 남이 main 에 넣은 변경까지
    리뷰 대상에 들어온다. 분기점을 잡아야 내 변경만 남는다.
    """
    if gitpython_available():
        import git

        try:
            repo = _repo(repo_root)
            return repo.git.merge_base(ref, other).strip()
        except git.exc.GitCommandError as exc:
            raise GitError(f"merge-base 실패 ({ref}...{other}): {_reason(exc)}") from exc

    completed = _run(repo_root, ["git", "merge-base", ref, other])
    return completed.strip()


def resolve_repo_root(start: Path) -> Path:
    """작업 트리 루트를 찾는다. 하위 디렉터리에서 실행해도 동작하게 한다."""
    if gitpython_available():
        import git

        try:
            repo = _repo(start)
            if repo.working_tree_dir:
                return Path(repo.working_tree_dir)
        except Exception:  # noqa: BLE001 - 폴백이 있으므로 조용히 넘어간다
            pass

    try:
        return Path(_run(start, ["git", "rev-parse", "--show-toplevel"]).strip())
    except GitError:
        return start


# --------------------------------------------------------------------------
# 내부
# --------------------------------------------------------------------------


def _diff(repo_root: Path, args: list[str]) -> str:
    if gitpython_available():
        return _diff_gitpython(repo_root, args)
    return _run(repo_root, ["git", "diff", *_BASE_ARGS, *args])


def _diff_gitpython(repo_root: Path, args: list[str]) -> str:
    import git

    try:
        repo = _repo(repo_root)
        # GitPython 은 후행 개행을 떼어낸다. 파서가 줄 단위로 읽으므로 무해하다.
        return repo.git.diff(*_BASE_ARGS, *args)
    except git.exc.InvalidGitRepositoryError as exc:
        raise GitError(f"{repo_root} 는 git 저장소가 아닙니다") from exc
    except git.exc.NoSuchPathError as exc:
        raise GitError(f"경로가 없습니다: {repo_root}") from exc
    except git.exc.GitCommandError as exc:
        raise GitError(f"git diff 실패: {_reason(exc)}") from exc


def _repo(path: Path):
    import git

    return git.Repo(path, search_parent_directories=True)


def _run(cwd: Path, command: list[str]) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - 인자는 코드에서 구성된다
            command, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        raise GitError(f"git 실행 실패: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GitError(f"{' '.join(command[:3])} 실패: {detail}")
    return completed.stdout


def _reason(exc: Exception) -> str:
    """GitCommandError 의 장황한 표현에서 핵심만 뽑는다."""
    stderr = getattr(exc, "stderr", "") or ""
    cleaned = stderr.replace("stderr:", "").strip().strip("'\"")
    return cleaned.splitlines()[0] if cleaned else str(exc)
