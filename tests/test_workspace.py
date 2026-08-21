"""워크스페이스 해석 검증.

CREX 를 리뷰 대상 저장소 밖에 두고 쓰는 것이 기본 사용 방식이므로, 우선순위가
틀리면 "엉뚱한 저장소를 리뷰했다"가 된다. 조용히 틀리는 종류라 회귀 방지가
중요하다.

실제 임시 git 저장소를 만들어 검증한다 — `.git` 유무와 하위 디렉터리 승격은
흉내로는 확인할 수 없다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.config import load_config  # noqa: E402
from crex.workspace import WorkspaceError, resolve  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _git_repo(path: Path) -> Path:
    """실제 git 저장소를 만든다. `.git` 이 진짜로 있어야 의미가 있는 검증들이다."""
    (path / "src").mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(path)],
        cwd=str(path.parent), capture_output=True, check=False,
    )
    return path


def _crex_home(path: Path) -> Path:
    """CREX 설치본이 있는 자리. 저장소가 아니다."""
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------


def test_argument_beats_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        other = _git_repo(root / "other")
        home = _crex_home(root / "crex")

        env = {"CREX_WORKSPACE": str(other)}
        workspace = resolve(target, start=home, env=env)

        _check(workspace.root == target.resolve(), f"root: {workspace.root}")
        _check(workspace.origin == "--workspace", f"origin: {workspace.origin}")
        _check(workspace.is_git, "git 저장소로 인식하지 못했다")


def test_env_beats_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        other = _git_repo(root / "other")
        home = _crex_home(root / "crex")
        (home / "crex.toml").write_text(f'workspace = "{other.as_posix()}"\n', encoding="utf-8")

        workspace = resolve(start=home, env={"CREX_WORKSPACE": str(target)})
        _check(workspace.root == target.resolve(), f"root: {workspace.root}")
        _check(workspace.origin == "CREX_WORKSPACE", f"origin: {workspace.origin}")


def test_crex_repo_still_accepted() -> None:
    """이전 이름. MCP 설정과 문서에 이미 퍼져 있어 계속 받아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")

        workspace = resolve(start=home, env={"CREX_REPO": str(target)})
        _check(workspace.root == target.resolve(), f"root: {workspace.root}")
        _check(workspace.origin == "CREX_REPO", f"origin: {workspace.origin}")


def test_config_workspace_is_relative_to_config_file() -> None:
    """설정 파일 안의 상대경로는 실행 위치가 아니라 설정 파일 기준이다.

    현재 디렉터리 기준이면 어디서 실행했느냐에 따라 대상이 바뀐다. 반입 번들을
    통째로 옮겨도 설정이 그대로 살아 있어야 한다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")
        (home / "crex.toml").write_text('workspace = "../target"\n', encoding="utf-8")

        elsewhere = _crex_home(root / "elsewhere")
        workspace = resolve(config_path=home / "crex.toml", start=elsewhere, env={})

        _check(workspace.root == target.resolve(), f"root: {workspace.root}")
        _check("workspace" in workspace.origin, f"origin: {workspace.origin}")


def test_subdirectory_is_promoted_to_repo_root() -> None:
    """하위 디렉터리를 줘도 저장소 루트로 올라가야 한다.

    상대경로 기준이 흔들리면 청크의 경로와 정적분석 결과의 경로가 어긋나
    그라운딩이 통째로 무력해진다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")

        workspace = resolve(target / "src", start=home, env={})
        _check(workspace.root == target.resolve(), f"root: {workspace.root}")


def test_missing_path_raises_with_actionable_message() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = _crex_home(Path(tmp) / "crex")
        try:
            resolve(Path(tmp) / "없는폴더", start=home, env={})
        except WorkspaceError as exc:
            _check("없는폴더" in str(exc), f"메시지에 경로가 없다: {exc}")
        else:
            raise AssertionError("없는 경로인데 통과했다")


def test_non_git_directory_is_flagged_not_fatal() -> None:
    """`.git` 이 없어도 죽지 않는다 — scan 은 git 없이도 동작한다.

    다만 diff 리뷰는 불가능하므로 표시를 남긴다. CLI 는 이 값을 보고 review 를
    거부하고, 관제 화면은 왼쪽 패널에 붉게 표시한다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plain = _crex_home(root / "plain")
        home = _crex_home(root / "crex")

        workspace = resolve(plain, start=home, env={})
        _check(workspace.root == plain.resolve(), f"root: {workspace.root}")
        _check(not workspace.is_git, "git 이 아닌데 is_git 이 참이다")


def test_workspace_config_wins_when_none_given() -> None:
    """워크스페이스 안의 crex.toml 을 CREX 쪽 설정보다 먼저 쓴다.

    저장소마다 compile_commands_dir 과 dotnet_project 가 다르다. 설치본 하나로
    여러 저장소를 보려면 설정도 저장소를 따라와야 한다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")
        (home / "crex.toml").write_text(
            '[llm.generator]\nmodel = "설치본-모델"\n', encoding="utf-8"
        )
        (target / "crex.toml").write_text(
            '[llm.generator]\nmodel = "저장소-모델"\n', encoding="utf-8"
        )

        workspace = resolve(target, start=home, env={})
        _check(
            workspace.config.generator.model == "저장소-모델",
            f"model: {workspace.config.generator.model}",
        )


def test_explicit_config_wins_over_workspace_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")
        (home / "crex.toml").write_text(
            '[llm.generator]\nmodel = "설치본-모델"\n', encoding="utf-8"
        )
        (target / "crex.toml").write_text(
            '[llm.generator]\nmodel = "저장소-모델"\n', encoding="utf-8"
        )

        workspace = resolve(target, home / "crex.toml", start=home, env={})
        _check(
            workspace.config.generator.model == "설치본-모델",
            f"model: {workspace.config.generator.model}",
        )


def test_workspace_config_lookup_does_not_escape_workspace() -> None:
    """워크스페이스 안에 설정이 없으면 그 위로 올라가 줍지 않는다.

    올라가면 임시 폴더 구조에 따라 남의 crex.toml 이 딸려온다. 어느 파일이
    적용됐는지 아무도 모르게 되는 것이 가장 나쁜 결과다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 부모에 설정을 둔다. 워크스페이스에는 없다.
        (root / "crex.toml").write_text(
            '[llm.generator]\nmodel = "부모-모델"\n', encoding="utf-8"
        )
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")
        (home / "crex.toml").write_text(
            '[llm.generator]\nmodel = "설치본-모델"\n', encoding="utf-8"
        )

        workspace = resolve(target, start=home, env={})
        _check(
            workspace.config.generator.model == "설치본-모델",
            f"model: {workspace.config.generator.model}",
        )


def test_reports_default_into_workspace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")

        workspace = resolve(target, start=home, env={})
        _check(workspace.reports == target.resolve() / "reports", f"reports: {workspace.reports}")

        override = resolve(target, start=home, env={"CREX_REPORTS": str(root / "리포트")})
        _check(override.reports == root / "리포트", f"reports: {override.reports}")


def test_unknown_top_level_config_key_is_rejected() -> None:
    """`workspace` 오타를 조용히 무시하면 대상 저장소가 말없이 바뀐다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crex.toml"
        path.write_text('workspase = "D:/work/myrepo"\n', encoding="utf-8")
        try:
            load_config(path)
        except ValueError as exc:
            _check("workspase" in str(exc), f"메시지에 키 이름이 없다: {exc}")
        else:
            raise AssertionError("오타난 최상위 키가 통과했다")


def test_env_var_expansion() -> None:
    """`%VAR%` 와 `$VAR` 를 푼다. Windows 에서 `%USERPROFILE%` 로 쓰는 경우가 있다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")

        os.environ["CREX_TEST_BASE"] = str(root)
        try:
            template = "%CREX_TEST_BASE%/target" if os.name == "nt" else "$CREX_TEST_BASE/target"
            workspace = resolve(template, start=home, env={})
            _check(workspace.root == target.resolve(), f"root: {workspace.root}")
        finally:
            del os.environ["CREX_TEST_BASE"]


TESTS = [
    test_argument_beats_everything,
    test_env_beats_config,
    test_crex_repo_still_accepted,
    test_config_workspace_is_relative_to_config_file,
    test_subdirectory_is_promoted_to_repo_root,
    test_missing_path_raises_with_actionable_message,
    test_non_git_directory_is_flagged_not_fatal,
    test_workspace_config_wins_when_none_given,
    test_explicit_config_wins_over_workspace_config,
    test_workspace_config_lookup_does_not_escape_workspace,
    test_reports_default_into_workspace,
    test_unknown_top_level_config_key_is_rejected,
    test_env_var_expansion,
]


def main() -> int:
    from crex.cli import force_utf8_output

    force_utf8_output()
    failures = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
