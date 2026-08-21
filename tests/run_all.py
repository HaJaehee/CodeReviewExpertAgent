"""전체 테스트 실행.

pytest 반입이 어려운 폐쇄망을 고려해 stdlib 만으로 돌아간다.

    python tests/run_all.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULES = [
    "tests.test_chunk",
    "tests.test_filter",
    "tests.test_ground",
    "tests.test_pipeline",
    "tests.test_mcp",
    "tests.test_viz",
    "tests.test_cli",
    "tests.test_workspace",
    "tests.test_eval",
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crex.cli import force_utf8_output

    force_utf8_output()
    failed: list[str] = []
    for name in MODULES:
        print(f"\n--- {name} " + "-" * (50 - len(name)))
        module = importlib.import_module(name)
        if module.main() != 0:
            failed.append(name)

    print("\n" + "=" * 58)
    if failed:
        print(f"실패한 모듈: {', '.join(failed)}")
        return 1
    print(f"전체 통과 ({len(MODULES)}개 모듈)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
