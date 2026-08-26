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
from crex.workspace import (  # noqa: E402
    WorkspaceError,
    persist_compile_commands_dir,
    persist_workspace,
    resolve,
    switch,
)


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


# --------------------------------------------------------------------------
# 도중에 바꾸기
# --------------------------------------------------------------------------


def test_switch_validates_exactly_like_resolve() -> None:
    """전환 경로가 느슨하면 "처음엔 거부, 바꾸기로는 통과"하는 우회로가 생긴다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        home = _crex_home(root / "crex")
        current = resolve(target, start=home, env={})

        try:
            switch(current, root / "없는폴더")
        except WorkspaceError:
            pass
        else:
            raise AssertionError("없는 경로로 전환됐다")

        # 하위 디렉터리 승격도 그대로 적용된다.
        other = _git_repo(root / "other")
        (other / "src").mkdir(exist_ok=True)
        changed = switch(current, other / "src")
        _check(changed.root == other.resolve(), f"root: {changed.root}")
        _check(changed.origin == "실행 중 변경", f"origin: {changed.origin}")


def test_switch_follows_workspace_config_when_not_pinned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = _git_repo(root / "first")
        second = _git_repo(root / "second")
        (second / "crex.toml").write_text(
            '[llm.generator]\nmodel = "두번째-모델"\n', encoding="utf-8"
        )
        home = _crex_home(root / "crex")

        current = resolve(first, start=home, env={})
        changed = switch(current, second)
        _check(
            changed.config.generator.model == "두번째-모델",
            f"model: {changed.config.generator.model}",
        )
        _check(changed.reports == second.resolve() / "reports", f"reports: {changed.reports}")


def test_switch_keeps_pinned_config_and_reports() -> None:
    """사용자가 --config / --out 으로 고정한 것은 대상을 바꿔도 따라간다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = _git_repo(root / "first")
        second = _git_repo(root / "second")
        (second / "crex.toml").write_text(
            '[llm.generator]\nmodel = "두번째-모델"\n', encoding="utf-8"
        )
        home = _crex_home(root / "crex")
        pinned = home / "pinned.toml"
        pinned.write_text('[llm.generator]\nmodel = "고정-모델"\n', encoding="utf-8")
        reports = root / "리포트"

        current = resolve(first, pinned, reports=reports, start=home, env={})
        _check(current.config_explicit and current.reports_explicit, "명시 표시가 안 붙었다")

        changed = switch(current, second)
        _check(
            changed.config.generator.model == "고정-모델",
            f"고정한 설정이 밀렸다: {changed.config.generator.model}",
        )
        _check(changed.reports == reports, f"reports: {changed.reports}")
        _check(changed.root == second.resolve(), f"root: {changed.root}")


# --------------------------------------------------------------------------
# 설정 파일에 고정
# --------------------------------------------------------------------------


def test_persist_writes_and_updates_top_level_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _git_repo(root / "target")
        other = _git_repo(root / "other")
        config = root / "crex.toml"

        persist_workspace(config, target)
        _check(load_config(config).workspace == target.resolve(), "처음 기록이 안 읽힌다")

        persist_workspace(config, other)
        _check(load_config(config).workspace == other.resolve(), "갱신이 안 됐다")
        _check(config.read_text(encoding="utf-8").count("workspace =") == 1, "키가 두 개 생겼다")

        persist_workspace(config, None)
        _check(load_config(config).workspace is None, "삭제가 안 됐다")


def test_persist_keeps_comments_and_sections() -> None:
    """사람이 읽고 고치는 파일이다. 주석이 내용의 절반이라 다시 써 내면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "crex.toml"
        original = (
            "#  머리말 주석\n"
            "#  두 번째 줄\n"
            "\n"
            "[llm.generator]\n"
            "#  모델 이름은 vLLM 에 뜬 것과 같아야 한다\n"
            'model = "Qwen3.6-27B"\n'
        )
        config.write_text(original, encoding="utf-8")
        target = _git_repo(Path(tmp) / "target")

        persist_workspace(config, target)
        text = config.read_text(encoding="utf-8")

        _check("#  머리말 주석" in text, "머리말 주석이 사라졌다")
        _check("#  모델 이름은" in text, "섹션 안 주석이 사라졌다")
        _check('model = "Qwen3.6-27B"' in text, "다른 설정이 사라졌다")
        # 최상위 키는 첫 섹션보다 앞에 있어야 그 섹션의 키가 되지 않는다.
        _check(
            text.index("workspace =") < text.index("[llm.generator]"),
            f"워크스페이스 키가 섹션 안으로 들어갔다:\n{text}",
        )
        _check(load_config(config).workspace == target.resolve(), "다시 읽히지 않는다")


def test_persist_preserves_crlf() -> None:
    """윈도우 사용자의 파일이다. LF 로 되돌리면 한 줄 고치고 전체가 바뀐 것으로 보인다."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "crex.toml"
        config.write_bytes(b'#  header\r\n\r\n[llm.generator]\r\nmodel = "m"\r\n')
        target = _git_repo(Path(tmp) / "target")

        persist_workspace(config, target)
        raw = config.read_bytes()
        _check(raw.count(b"\r\n") == raw.count(b"\n"), f"LF 가 섞였다: {raw!r}")
        _check(b'workspace = "' in raw, "키가 안 들어갔다")


def test_persist_ignores_workspace_key_inside_a_section() -> None:
    """섹션 안의 같은 이름 키는 다른 설정이다. 건드리면 남의 값을 덮어쓴다."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "crex.toml"
        config.write_text(
            '[grounding]\nsemgrep_config = "/opt/rules"\n', encoding="utf-8"
        )
        target = _git_repo(Path(tmp) / "target")

        persist_workspace(config, target)
        text = config.read_text(encoding="utf-8")
        _check(text.index("workspace =") < text.index("[grounding]"), f"위치가 잘못됐다:\n{text}")
        _check('semgrep_config = "/opt/rules"' in text, "다른 키가 사라졌다")


def test_persist_section_key_creates_and_updates_grounding() -> None:
    """`compile_commands_dir` 는 최상위가 아니라 `[grounding]` 안에 들어가야 한다.

    최상위에 적히면 설정이 알 수 없는 키라며 거부한다 — 만들어 주고 나서
    다음 실행이 죽는 최악의 조합이다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "crex.toml"

        # 섹션이 없는 파일에 처음 적을 때
        config.write_text('# 머리말\nworkspace = "/w"\n', encoding="utf-8")
        persist_compile_commands_dir(config, Path("out/build/x64-debug"))
        loaded = load_config(config)
        _check(loaded.grounding.compile_commands_dir == "out/build/x64-debug",
               f"{loaded.grounding.compile_commands_dir!r}")
        # 여기서 지켜야 할 것은 "기존 최상위 키가 날아가지 않는다" 이다. 값으로
        # 비교하면 플랫폼을 탄다 — POSIX 에서 "/w" 는 그대로지만 Windows 에서는
        # 드라이브가 붙어 절대 경로가 된다. 그래서 원문으로 확인한다.
        _check('workspace = "/w"' in config.read_text(encoding="utf-8"),
               "기존 최상위 키가 날아갔다:" + chr(10) + config.read_text(encoding="utf-8"))
        _check(loaded.workspace is not None and loaded.workspace.name == "w",
               f"workspace 를 읽지 못한다: {loaded.workspace!r}")

        # 이미 있는 값을 갱신할 때 — 같은 섹션의 다른 키와 주석은 그대로 남는다
        config.write_text(
            "[grounding]\n# 이 주석은 남아야 한다\ncompile_commands_dir = \"old\"\ntimeout = 30.0\n",
            encoding="utf-8",
        )
        persist_compile_commands_dir(config, Path("new/dir"))
        text = config.read_text(encoding="utf-8")
        _check("이 주석은 남아야 한다" in text, "주석이 날아갔다")
        loaded = load_config(config)
        _check(loaded.grounding.compile_commands_dir == "new/dir",
               f"{loaded.grounding.compile_commands_dir!r}")
        _check(loaded.grounding.timeout == 30.0, "같은 섹션의 다른 키가 바뀌었다")

        # 다른 섹션만 있을 때는 [grounding] 을 새로 만든다
        config.write_text("[review]\nmax_workers = 4\n", encoding="utf-8")
        persist_compile_commands_dir(config, Path("build"))
        loaded = load_config(config)
        _check(loaded.grounding.compile_commands_dir == "build",
               f"{loaded.grounding.compile_commands_dir!r}")
        _check(loaded.review.max_workers == 4, "다른 섹션이 망가졌다")


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
    test_switch_validates_exactly_like_resolve,
    test_switch_follows_workspace_config_when_not_pinned,
    test_switch_keeps_pinned_config_and_reports,
    test_persist_writes_and_updates_top_level_key,
    test_persist_keeps_comments_and_sections,
    test_persist_preserves_crlf,
    test_persist_ignores_workspace_key_inside_a_section,
    test_persist_section_key_creates_and_updates_grounding,
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
