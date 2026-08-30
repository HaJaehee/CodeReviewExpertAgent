"""compile_commands.json 빌드를 화면에서 돌린다 — Engine 계층.

`crex.compiledb` 를 다시 쓰지 않는다. CLI 의 `python -m crex compiledb` 와 **같은
`generate()`** 를 부르고, 출력 줄과 중단 신호만 콜백으로 받아 화면 쪽으로 넘긴다.
계측이 결과를 바꾸지 않아야 한다는 원칙(`wiki/invariants.md`)은 리뷰 파이프라인에만
해당하는 이야기가 아니다 — 화면에서 만든 DB 와 명령줄에서 만든 DB 가 다르면
"화면에서는 되는데 CLI 에서는 안 된다"가 된다.

## 왜 화면에서도 만들 수 있어야 하나

clang-tidy 는 컴파일 명령을 모르면 헤더를 못 찾고, 그 상태의 지적은 절반이
오탐이다. 그런데 그 DB 를 만드는 일이 **관제 화면을 띄우기 전에 끝나 있어야 하는
별도의 명령**이면, C++ 저장소를 열 때마다 터미널로 돌아갔다 와야 한다. 워크스페이스를
화면에서 바꿀 수 있게 만들어 놓고 그 다음 단계에서 터미널을 요구하는 것은 앞뒤가 맞지
않는다.

## 로그는 꼬리만 들고 있다

MSBuild 는 `detailed` verbosity 로 돈다(그보다 낮으면 CL 명령줄 이벤트가 로거까지
오지 않는다). 큰 솔루션이면 수만 줄이다. 전부 메모리에 이고 있을 이유가 없다 —
전문은 `msbuild.log` 로 나가고, 화면은 진행 중인지 보려는 것뿐이므로 마지막
`MAX_LOG_LINES` 줄만 남긴다. 밀려 나간 줄 수는 커서와 함께 알려준다.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..compiledb import (
    CompileDbCancelled,
    CompileDbError,
    DEFAULT_TARGET,
    Result,
    generate,
)
from ..config import Config
from ..workspace import Workspace, persist_compile_commands_dir, repo_config_path

log = logging.getLogger(__name__)

#: 화면에 들고 있을 빌드 출력 줄 수. 전문은 로그 파일에 있다.
MAX_LOG_LINES = 400


@dataclass
class BuildParams:
    """화면이 고를 수 있는 것만 받는다.

    CLI 의 `--msbuild-arg` 는 일부러 받지 않는다. 임의의 빌드 인자를 HTTP 로 받는
    것은 이 화면이 감당할 일이 아니다 — 필요하면 그때는 명령줄이 맞다.
    """

    project: str | None = None
    configuration: str = "Debug"
    platform: str = "x64"
    target: str = DEFAULT_TARGET
    generator: str = "Ninja"
    #: 만든 자리를 `crex.json` 에 적을지. 끄면 이 서버가 사는 동안만 적용된다.
    save: bool = True

    def describe(self) -> str:
        where = self.project or "(자동 탐지)"
        return f"{where} · {self.configuration}/{self.platform} · {self.target}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "configuration": self.configuration,
            "platform": self.platform,
            "target": self.target,
            "generator": self.generator,
            "save": self.save,
        }


@dataclass
class BuildJob:
    """빌드 한 번. 리뷰 실행(`Run`)과 같은 모양으로 폴링된다."""

    id: str
    params: BuildParams
    created_at: float
    status: str = "running"  # running | done | failed | cancelled
    #: 끝난 시각. 끝난 뒤에도 경과 시간이 계속 자라면 화면이 거짓말을 한다.
    finished_at: float | None = None
    error: str | None = None
    #: 성공했을 때의 산출물 요약. 화면이 그대로 띄운다.
    result: dict[str, Any] | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES), repr=False)
    #: 지금까지 받은 줄 수. 커서이자 전체 줄 수다.
    _seq: int = 0
    #: 버퍼에서 밀려 나간 줄 수.
    _dropped: int = 0

    # -- 로그 ---------------------------------------------------------------

    def append(self, line: str) -> None:
        """빌드 출력 한 줄. `generate()` 가 빌드 스레드에서 직접 부른다."""
        with self._lock:
            if len(self._lines) == self._lines.maxlen:
                self._dropped += 1
            self._lines.append(line)
            self._seq += 1

    def tail(self, since: int) -> dict[str, Any]:
        """`since` 이후의 줄. 밀려 나간 구간은 건너뛴다.

        커서가 버퍼보다 뒤처졌다면 그 사이는 이미 없다 — 없는 것을 있는 척하지
        않고, 몇 줄을 건너뛰었는지 함께 알려준다.
        """
        with self._lock:
            first = self._seq - len(self._lines) + 1  # 버퍼 첫 줄의 커서 값
            start = max(0, since - first + 1)
            lines = list(self._lines)[start:]
            return {"lines": lines, "cursor": self._seq, "dropped": self._dropped}

    # -- 상태 ---------------------------------------------------------------

    def head(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "params": self.params.to_dict(),
            "label": self.params.describe(),
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "elapsed": round((self.finished_at or time.time()) - self.created_at, 1),
        }


def new_job(params: BuildParams) -> BuildJob:
    return BuildJob(id=uuid.uuid4().hex[:12], params=params, created_at=time.time())


def execute(
    job: BuildJob,
    root: Path,
    config: Config,
    workspace: Workspace | None,
) -> None:
    """빌드를 돌리고, 성공하면 그 자리를 지금 설정에 꽂는다.

    **만들기만 하고 끝내지 않는다.** 파일만 만들어 두고 "이제 crex.json 을
    여세요" 하면 거기서 절반이 떨어져 나간다 — CLI 의 `compiledb` 가 만든 자리를
    바로 적는 것과 같은 이유다. 화면에서는 두 단계로 나눠 적용한다.

    1. 돌고 있는 설정 객체에 꽂는다. 다음 리뷰가 곧바로 그 DB 를 쓴다.
    2. `save` 면 `crex.json` 에도 적는다. 다음에 띄울 때도 살아 있어야 한다.

    2번은 남의 저장소 파일을 고치는 일이라 화면에서 체크박스로 끌 수 있다.
    """
    try:
        _execute(job, root, config, workspace)
    finally:
        job.finished_at = time.time()
        # "running" 으로 남은 작업은 리뷰도 워크스페이스 변경도 영원히 막는다.
        # 스레드가 어떻게 끝났든 상태만은 반드시 닫는다.
        if job.status == "running":
            job.status = "failed"
            job.error = job.error or "빌드 스레드가 예상치 못하게 끝났습니다."


def _execute(
    job: BuildJob,
    root: Path,
    config: Config,
    workspace: Workspace | None,
) -> None:
    try:
        result = generate(
            root,
            project=job.params.project or None,
            configuration=job.params.configuration,
            platform=job.params.platform,
            target=job.params.target,
            generator=job.params.generator,
            on_line=job.append,
            cancel=job.cancel.is_set,
        )
    except CompileDbCancelled as exc:
        job.status = "cancelled"
        job.error = str(exc)
        return
    except CompileDbError as exc:
        job.status = "failed"
        job.error = str(exc)
        return
    except Exception as exc:  # noqa: BLE001 - 실행 스레드에서 새어 나가면 서버가 죽는다
        log.exception("compile_commands.json 생성 실패")
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        return

    if result.entries == 0:
        # 파일은 생겼는데 내용이 없다. 이걸 설정에 꽂으면 '설정했는데 왜 안 되지'가
        # 된다. CLI 도 같은 자리에서 멈춘다.
        job.status = "failed"
        job.error = (
            f"{result.json_path} 는 만들어졌지만 비어 있습니다. 설정에 적지 않았습니다.\n"
            "- 증분 빌드였을 수 있습니다. 이미 최신인 파일은 기록되지 않습니다.\n"
            "- 구성/플랫폼이 실제로 빌드되는 조합인지 확인하십시오."
            + (f"\n- 빌드 로그: {result.log_path}" if result.log_path else "")
        )
        job.result = _describe(result, applied=None, saved_to=None)
        return

    value = _config_value(result.directory, root)
    config.grounding.compile_commands_dir = value

    saved_to: str | None = None
    save_error: str | None = None
    if job.params.save:
        if workspace is None:
            save_error = "설정 파일 위치를 알 수 없어 적지 못했습니다. 이 서버가 사는 동안만 적용됩니다."
        else:
            target = repo_config_path(workspace)
            try:
                persist_compile_commands_dir(target, Path(value))
                saved_to = str(target)
            except (OSError, ValueError) as exc:
                save_error = f"{target} 에 적지 못했습니다: {exc}"

    job.status = "done"
    job.error = save_error
    job.result = _describe(result, applied=value, saved_to=saved_to)


def _describe(result: Result, *, applied: str | None, saved_to: str | None) -> dict[str, Any]:
    return {
        "project": result.project.describe(),
        "kind": result.project.kind,
        "directory": str(result.directory),
        "json_path": str(result.json_path),
        "entries": result.entries,
        "log_path": str(result.log_path) if result.log_path else None,
        #: 지금 설정에 꽂힌 값(`grounding.compile_commands_dir`).
        "applied": applied,
        #: `crex.json` 에 적었다면 그 파일.
        "saved_to": saved_to,
    }


def _config_value(directory: Path, root: Path) -> str:
    """설정에 적을 값. 워크스페이스 안이면 상대경로다 (CLI 와 같은 규칙).

    분석기는 워크스페이스를 cwd 로 실행되므로 상대경로가 그대로 맞고, 저장소를
    다른 장비에 클론해도 값이 살아남는다.
    """
    try:
        return directory.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(directory.resolve())


__all__ = ["BuildJob", "BuildParams", "MAX_LOG_LINES", "execute", "new_job"]
