"""설정 파일 형식 검증 — JSON 에 없는 두 가지를 규칙으로 채운 부분.

설정은 JSON 이다. JSON 에는 주석이 없고 여러 줄 문자열이 없는데, 이 파일은
사람이 읽고 고치는 물건이라 둘 다 필요하다. 그래서 형식은 순수 JSON 으로 두고
약속으로 채웠다 (`crex/config.py` 의 머리말).

1. 키가 `//` 로 시작하면 설명이다.
2. 문자열 설정은 문자열 배열로도 적을 수 있고 줄바꿈으로 이어 붙인다.

두 규칙 다 **조용히 틀리면 최악**인 종류다. 설명 키가 설정으로 새면 "알 수 없는
키"로 멈추고, 배열 이어붙이기가 `analyzers` 까지 먹으면 분석기 세 개가 이름
하나가 되어 정적분석이 통째로 빠진다 — 그리고 그 결과는 "지적 0건"과 구분되지
않는다.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.config import load_config, strip_comment_keys  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _write(directory: Path, data: dict, name: str = "crex.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 규칙 1 — 주석 키
# --------------------------------------------------------------------------


def test_comment_keys_are_stripped_everywhere() -> None:
    """최상위든 중첩 객체 안이든 `//` 키는 설정으로 읽히지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(
            Path(tmp),
            {
                "//": ["머리말", "여러 줄이어도 된다"],
                "// 워크스페이스": "설명은 아무 값이나 될 수 있다",
                "llm": {
                    "// generator": "설명",
                    "generator": {"// model": "설명", "model": "내-모델"},
                },
                "grounding": {"// timeout": 12345, "timeout": 30.0},
            },
        )
        config = load_config(path)
        _check(config.generator.model == "내-모델", f"model: {config.generator.model}")
        _check(config.grounding.timeout == 30.0, f"timeout: {config.grounding.timeout}")


def test_comment_key_that_shadows_a_real_key_is_still_a_comment() -> None:
    """`"// model"` 은 `model` 과 다른 키다. 설명이 값을 덮어쓰면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(
            Path(tmp),
            {"llm": {"generator": {"// model": "이건 설명", "model": "진짜-모델"}}},
        )
        _check(load_config(path).generator.model == "진짜-모델", "설명이 값을 덮었다")


def test_comment_keys_reach_into_extra_body() -> None:
    """vLLM 에 그대로 넘기는 값 안에서도 설명을 달 수 있어야 한다.

    여기만 예외로 두면, 하필 가장 설명이 필요한 자리(모델별 마법 파라미터)에
    설명을 못 단다. `//` 로 시작하는 파라미터를 받는 추론 서버는 없다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(
            Path(tmp),
            {
                "llm": {
                    "generator": {
                        "extra_body": {
                            "// chat_template_kwargs": "Qwen3.x 의 추론 모드를 끈다",
                            "chat_template_kwargs": {"enable_thinking": False},
                        }
                    }
                }
            },
        )
        extra = load_config(path).generator.extra_body
        _check(extra == {"chat_template_kwargs": {"enable_thinking": False}}, f"{extra!r}")


def test_strip_comment_keys_leaves_the_rest_alone() -> None:
    """목록 안의 객체까지 훑되, 설명이 아닌 것은 그대로 둔다."""
    data = {"//": "설명", "a": 1, "list": [{"// x": "설명", "x": 2}, "문자열"]}
    _check(
        strip_comment_keys(data) == {"a": 1, "list": [{"x": 2}, "문자열"]},
        f"{strip_comment_keys(data)!r}",
    )


# --------------------------------------------------------------------------
# 규칙 2 — 문자열 배열은 줄바꿈으로 이어 붙인다
# --------------------------------------------------------------------------


def test_string_setting_can_be_written_as_an_array() -> None:
    """긴 글을 역슬래시 n 한 줄로 적으면 파일을 열어도 읽을 수가 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(
            Path(tmp),
            {"grounding": {"clang_tidy_checks": ["-*,", "bugprone-*,", "cert-*"]}},
        )
        checks = load_config(path).grounding.clang_tidy_checks
        _check(checks == "-*,\nbugprone-*,\ncert-*", f"{checks!r}")


def test_list_setting_stays_a_list() -> None:
    """`analyzers` 는 원래 목록이다. 이어 붙이면 분석기 이름 하나가 된다.

    조용히 틀리는 쪽이라 값이 크다 — 이름이 하나로 뭉치면 알 수 없는 분석기로
    걸려 오류가 나거나, 더 나쁘게는 기본 분석기까지 전부 걸러진다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), {"grounding": {"analyzers": ["cppcheck", "ruff"]}})
        analyzers = load_config(path).grounding.analyzers
        _check(analyzers == ["cppcheck", "ruff"], f"{analyzers!r}")


def test_extra_body_arrays_are_left_alone() -> None:
    """vLLM 에 넘기는 값은 손대지 않는다. `stop` 이 문자열 하나가 되면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(
            Path(tmp),
            {"llm": {"generator": {"extra_body": {"stop": ["</s>", "\n\n"]}}}},
        )
        extra = load_config(path).generator.extra_body
        _check(extra == {"stop": ["</s>", "\n\n"]}, f"{extra!r}")


def test_non_string_element_in_a_text_array_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), {"grounding": {"dotnet_project": ["src/App.sln", 3]}})
        try:
            load_config(path)
        except ValueError as exc:
            _check("dotnet_project" in str(exc), f"메시지에 키 이름이 없다: {exc}")
        else:
            raise AssertionError("문자열이 아닌 원소가 통과했다")


# --------------------------------------------------------------------------
# 형식이 바뀐 뒤의 안내
# --------------------------------------------------------------------------


def test_broken_json_says_where() -> None:
    """쉼표 하나로 멈추는 일이 잦다. 어디를 고칠지 안 알려주면 파일 전체를 의심하게 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crex.json"
        path.write_text('{\n  "review": {\n    "max_workers": 4,\n  }\n}\n', encoding="utf-8")
        try:
            load_config(path)
        except ValueError as exc:
            _check("줄" in str(exc) and str(path) in str(exc), f"위치가 없다: {exc}")
        else:
            raise AssertionError("깨진 JSON 이 통과했다")


def test_bom_does_not_break_the_file() -> None:
    """메모장으로 고치면 BOM 이 붙는다. 그러면 json 이 첫 글자에서 바로 죽는다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crex.json"
        path.write_text(
            json.dumps({"review": {"max_workers": 7}}), encoding="utf-8-sig"
        )
        _check(load_config(path).review.max_workers == 7, "BOM 붙은 파일을 못 읽는다")


def test_leftover_toml_config_is_reported_not_ignored() -> None:
    """예전 설정이 남아 있으면 알린다. 조용히 무시하면 고쳤는데 안 바뀐다."""
    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "crex.toml"
        legacy.write_text('workspace = "D:/work/myrepo"\n', encoding="utf-8")
        try:
            load_config(search_from=Path(tmp))
        except ValueError as exc:
            _check("crex.json" in str(exc), f"무엇으로 바꿀지 안 알려준다: {exc}")
        else:
            raise AssertionError("남아 있는 TOML 설정을 조용히 지나쳤다")


def test_json_config_wins_over_a_leftover_toml() -> None:
    """옮겨 적은 뒤 예전 파일을 지우지 않았다고 멈추지는 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "crex.toml").write_text("max_workers = 1\n", encoding="utf-8")
        _write(Path(tmp), {"review": {"max_workers": 9}})
        config = load_config(search_from=Path(tmp))
        _check(config.review.max_workers == 9, f"{config.review.max_workers}")


TESTS = [
    test_comment_keys_are_stripped_everywhere,
    test_comment_key_that_shadows_a_real_key_is_still_a_comment,
    test_comment_keys_reach_into_extra_body,
    test_strip_comment_keys_leaves_the_rest_alone,
    test_string_setting_can_be_written_as_an_array,
    test_list_setting_stays_a_list,
    test_extra_body_arrays_are_left_alone,
    test_non_string_element_in_a_text_array_is_rejected,
    test_broken_json_says_where,
    test_bom_does_not_break_the_file,
    test_leftover_toml_config_is_reported_not_ignored,
    test_json_config_wins_over_a_leftover_toml,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
