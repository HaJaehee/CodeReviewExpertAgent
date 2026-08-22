"""워크스페이스 해석 — 리뷰 대상 저장소를 CREX 설치 위치에서 떼어낸다.

CREX 는 `.git` 이 있는 프로젝트 루트 안에 같이 있을 필요가 없다. 폐쇄망에서
CREX 는 반입 절차를 거쳐 한 자리에 풀어두는 물건이고, 리뷰 대상 저장소는
장비마다 여러 개다. 설치본을 저장소마다 복사하는 것은 반입본 무결성 관리와
정면으로 충돌한다 — 사본이 늘면 어느 것이 검증된 것인지 알 수 없게 된다.

그래서 **작업 디렉터리는 CREX 루트로 두고, 리뷰 대상만 지정**한다.

    cd D:\\tools\\crex
    python -m crex review --workspace D:\\work\\myrepo

우선순위는 위에서 아래로, 먼저 정해지면 아래는 보지 않는다.

| 순위 | 출처 | 예 |
|---|---|---|
| 1 | 명령줄 인자 | `--workspace D:\\work\\myrepo` (`--repo` 도 같다) |
| 2 | 환경변수 | `CREX_WORKSPACE`, 이전 이름 `CREX_REPO` |
| 3 | 설정 파일 | `crex.toml` 의 `workspace = "..."` |
| 4 | 없으면 | 현재 디렉터리에서 git 루트 탐색 (예전 동작) |

## 설정 파일은 워크스페이스 것을 먼저 본다

1·2번으로 워크스페이스가 정해졌고 `--config` 도 `CREX_CONFIG` 도 없으면,
`<워크스페이스>/crex.toml` 을 먼저 찾는다. 저장소마다 `compile_commands_dir`,
`dotnet_project`, 쓸 분석기가 다르기 때문이다. 없으면 예전처럼 현재
디렉터리에서 위로 올라가며 찾는다 (= CREX 루트의 설정).

CLI·MCP 서버·관제 화면 세 진입점이 전부 이 모듈 하나를 쓴다. 세 곳이 각자
환경변수를 읽던 것을 여기로 모았다 — 한 곳에서만 틀릴 수 있게.

## 도중에 바꾸기

`switch()` 는 이미 돌고 있는 프로세스의 대상을 바꾼다 (관제 화면의 "변경" 버튼,
MCP 의 `set_workspace`). 처음 정할 때와 **완전히 같은 검증**을 거친다 — 여기서만
느슨하면 "처음엔 거부당했는데 바꾸기로는 통과하는" 경로가 생긴다.

`persist_workspace()` 는 `crex.toml` 의 최상위 `workspace` 키를 갱신한다
(CLI 의 `workspace` 명령). 다음 실행부터 적용된다.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import Config, find_config, load_config
from .gitio import resolve_repo_root

log = logging.getLogger(__name__)

#: 리뷰 대상 저장소. `CREX_REPO` 는 이전 이름이고 계속 받는다.
ENV_WORKSPACE = "CREX_WORKSPACE"
ENV_REPO = "CREX_REPO"
ENV_CONFIG = "CREX_CONFIG"
ENV_REPORTS = "CREX_REPORTS"


class WorkspaceError(ValueError):
    """워크스페이스 지정이 잘못됐다. 사용자가 고칠 수 있는 문제만 담는다."""


@dataclass
class Workspace:
    """리뷰 한 번을 돌리는 데 필요한 위치 정보 전부."""

    #: 리뷰 대상 저장소 루트. 청크·정적분석·리포트의 모든 상대경로 기준점.
    root: Path
    config: Config
    #: 리포트를 쓸 디렉터리.
    reports: Path
    #: root 를 어디서 얻었는지. 로그와 doctor 출력에 쓴다.
    origin: str
    #: root 에 `.git` 이 있는가. 없으면 diff 리뷰는 못 하고 scan 만 된다.
    is_git: bool = True
    #: 설정 파일을 사용자가 직접 지정했는가(`--config`/`CREX_CONFIG`).
    #: 지정했다면 워크스페이스를 바꿔도 그 파일을 계속 쓴다 — 사용자가 고정한 것이다.
    config_explicit: bool = False
    #: 리포트 위치를 직접 지정했는가(`--out`/`CREX_REPORTS`). 같은 이유로 따라간다.
    reports_explicit: bool = False

    def describe(self) -> str:
        note = "" if self.is_git else " (git 저장소가 아님 — scan 만 가능)"
        return f"{self.root} [{self.origin}]{note}"


def resolve(
    explicit: Path | str | None = None,
    config_path: Path | str | None = None,
    *,
    reports: Path | str | None = None,
    start: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Workspace:
    """워크스페이스와 설정을 함께 정한다.

    설정과 워크스페이스는 서로를 참조한다 — 설정이 워크스페이스를 지정할 수 있고,
    워크스페이스가 설정 파일을 품고 있을 수 있다. 순환을 끊기 위해 순서를 못 박는다:
    **인자·환경변수로 정해지는 워크스페이스가 먼저**, 그 다음 설정 파일,
    설정 파일이 워크스페이스를 정하는 것은 앞의 둘이 다 없을 때뿐이다.
    """
    environ = os.environ if env is None else env
    base = (start or Path.cwd()).resolve()

    candidate, origin = _from_argument_or_env(explicit, environ)

    # 명시된 것도, 워크스페이스 안의 것도 없으면 base 에서 위로 올라가며 찾는다.
    config = load_config(_choose_config(config_path, environ, candidate, base), search_from=base)

    if candidate is None and config.workspace is not None:
        candidate, origin = config.workspace, f"{_name(config.source)} 의 workspace"

    root, is_git = _validate(candidate, base)
    return Workspace(
        root=root,
        config=config,
        reports=_reports_dir(reports, environ, root),
        origin=origin,
        is_git=is_git,
        config_explicit=bool(config_path or (environ.get(ENV_CONFIG) or "").strip()),
        reports_explicit=bool(reports or (environ.get(ENV_REPORTS) or "").strip()),
    )


def switch(
    current: Workspace | None,
    target: Path | str,
    *,
    start: Path | None = None,
) -> Workspace:
    """돌고 있는 프로세스의 리뷰 대상을 바꾼다.

    `resolve()` 를 그대로 다시 태운다 — 존재 확인, 저장소 루트 승격, `.git` 확인,
    워크스페이스 안의 `crex.toml` 우선까지 전부 같다. 검증을 여기서 따로 쓰면
    두 경로의 판정이 언젠가 갈린다.

    사용자가 **직접 지정한 것은 따라간다.** `--config` 로 설정 파일을 고정했다면
    대상을 바꿔도 그 파일을 계속 쓰고, `--out`/`CREX_REPORTS` 로 리포트 위치를
    고정했다면 그것도 유지한다. 지정하지 않은 것만 새 워크스페이스를 따른다.

    환경변수는 다시 읽지 않는다. 전환은 "지금 이 값으로 바꾼다"이지 "처음부터
    다시 정한다"가 아니다.
    """
    if current is None:
        return resolve(target, start=start, env={})

    changed = resolve(
        target,
        current.config.source if current.config_explicit else None,
        reports=current.reports if current.reports_explicit else None,
        start=start,
        env={},
    )
    changed.origin = "실행 중 변경"
    changed.config_explicit = current.config_explicit
    changed.reports_explicit = current.reports_explicit
    return changed


#: 설정 파일이 없을 때 만들어 넣는 머리말.
_NEW_CONFIG_HEADER = (
    "#  CREX 설정. 전체 항목은 crex.example.toml 과 docs/configuration.md 를 보라.\n"
)

_WORKSPACE_LINE = re.compile(r"^\s*workspace\s*=", re.IGNORECASE)
_SECTION_LINE = re.compile(r"^\s*\[")


def persist_workspace(config_path: Path, root: Path | None) -> Path:
    """`crex.toml` 의 최상위 `workspace` 키를 갱신한다. `root=None` 이면 지운다.

    TOML 을 다시 써 내지 않고 **그 줄만 갈아 끼운다.** 표준 라이브러리에는 TOML
    작성기가 없고(`tomllib` 은 읽기 전용), 있다 해도 주석을 전부 날려버린다.
    이 파일은 사람이 읽고 고치는 물건이라 주석이 내용의 절반이다.

    최상위 키만 건드린다 — 첫 `[section]` 앞쪽 구간만 본다. 그 뒤의
    `workspace = ...` 는 어느 섹션에 속한 다른 키이므로 손대지 않는다.
    """
    # 줄바꿈 방식을 바꾸지 않는다. 윈도우에서 CRLF 파일을 LF 로 되돌려 놓으면
    # 한 줄 고쳤는데 git 이 파일 전체가 바뀐 것으로 본다. 그래서 읽을 때도
    # 개행 변환을 끈다(`newline=""`) — 켜져 있으면 CRLF 였다는 사실 자체가 지워진다.
    text = ""
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    limit = next((i for i, line in enumerate(lines) if _SECTION_LINE.match(line)), len(lines))
    found = next((i for i, line in enumerate(lines[:limit]) if _WORKSPACE_LINE.match(line)), None)

    if root is None:
        if found is not None:
            del lines[found]
            # 키를 지우고 남은 빈 줄이 쌓이지 않게 한 줄만 정리한다.
            if found < len(lines) and not lines[found].strip() and (
                found == 0 or not lines[found - 1].strip()
            ):
                del lines[found]
    else:
        entry = f'workspace = "{_toml_string(root)}"'
        if found is not None:
            lines[found] = entry
        elif not lines:
            lines = [_NEW_CONFIG_HEADER.rstrip("\n"), "", entry]
        else:
            # 첫 섹션 바로 앞에 넣는다. 파일 맨 위 설명 주석을 밀어내지 않는다.
            insert = limit
            while insert > 0 and not lines[insert - 1].strip():
                insert -= 1
            lines[insert:insert] = [entry, ""] if insert == 0 else ["", entry]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        newline.join(lines).rstrip("\r\n") + newline, encoding="utf-8", newline=""
    )

    # 쓴 것을 바로 다시 읽어 검증한다. 망가진 설정을 남기고 성공을 알리지 않는다.
    load_config(config_path)
    return config_path


def _toml_string(root: Path) -> str:
    """TOML 기본 문자열 값. 윈도우 경로도 `/` 로 적어 역슬래시 이스케이프를 피한다."""
    return root.as_posix().replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------
# 내부
# --------------------------------------------------------------------------


def _from_argument_or_env(
    explicit: Path | str | None, env: Mapping[str, str]
) -> tuple[Path | str | None, str]:
    if explicit is not None:
        return explicit, "--workspace"
    for name in (ENV_WORKSPACE, ENV_REPO):
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    return None, "현재 디렉터리"


def _choose_config(
    config_path: Path | str | None,
    env: Mapping[str, str],
    candidate: Path | str | None,
    base: Path,
) -> Path | None:
    """명시 > 워크스페이스의 crex.toml > 현재 디렉터리에서 위로 탐색."""
    if config_path:
        return Path(config_path)
    from_env = (env.get(ENV_CONFIG) or "").strip()
    if from_env:
        return Path(from_env)

    if candidate is not None:
        try:
            project = _expand(candidate, base)
        except WorkspaceError:
            project = None
        if project is not None and project.is_dir():
            found = find_config(project)
            # 워크스페이스 *안*에 있는 것만 쓴다. 위로 올라가다 CREX 쪽 설정을
            # 주워 오면 어느 파일이 적용됐는지 아무도 모르게 된다.
            if found is not None and _is_within(found.parent, project):
                return found

    # None 이면 호출부가 base 에서부터 탐색한다.
    return None


def _validate(candidate: Path | str | None, base: Path) -> tuple[Path, bool]:
    if candidate is None:
        # 예전 동작. 현재 디렉터리가 저장소 안이면 그 루트를 쓴다.
        root = resolve_repo_root(base)
        return root, (root / ".git").exists()

    path = _expand(candidate, base)
    if not path.exists():
        raise WorkspaceError(
            f"워크스페이스 경로가 없다: {path}\n"
            f"  .git 이 있는 프로젝트 루트를 지정하라. 예: --workspace D:\\work\\myrepo"
        )
    if not path.is_dir():
        raise WorkspaceError(f"워크스페이스는 디렉터리여야 한다: {path}")

    root = resolve_repo_root(path)
    if root != path:
        # 하위 디렉터리를 줬다. 상대경로 기준이 흔들리면 정적분석 결과와 청크가
        # 어긋나므로 항상 저장소 루트로 올린다.
        log.info("워크스페이스를 저장소 루트로 올린다: %s → %s", path, root)

    is_git = (root / ".git").exists()
    if not is_git:
        log.warning(
            "%s 에 .git 이 없다. diff 리뷰(review)는 할 수 없고 scan 만 동작한다.", root
        )
    return root, is_git


def _expand(value: Path | str, base: Path) -> Path:
    """`~`, `%VAR%`, `$VAR` 를 풀고 상대경로는 base 기준으로 맞춘다."""
    text = os.path.expandvars(str(value)).strip()
    if not text:
        raise WorkspaceError("워크스페이스 경로가 비어 있다")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _reports_dir(
    reports: Path | str | None, env: Mapping[str, str], root: Path
) -> Path:
    if reports:
        return Path(reports)
    from_env = (env.get(ENV_REPORTS) or "").strip()
    if from_env:
        return Path(from_env)
    return root / "reports"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _name(source: Path | None) -> str:
    return source.name if source else "설정"


__all__ = [
    "ENV_CONFIG",
    "ENV_REPO",
    "ENV_REPORTS",
    "ENV_WORKSPACE",
    "Workspace",
    "WorkspaceError",
    "persist_workspace",
    "resolve",
    "switch",
]
