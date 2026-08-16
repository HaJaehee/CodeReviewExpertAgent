"""청킹 엔진 검증.

외부 의존 없이 `python -m tests.test_chunk` 또는 pytest 로 실행된다.
폐쇄망에서 pytest 반입이 안 될 수도 있으므로 __main__ 진입점을 둔다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crx.chunk import Chunker, DiffSourceMismatch, parse_unified_diff  # noqa: E402
from crx.schema import Language, LineStatus  # noqa: E402

CPP_SOURCE = """\
#include <vector>
#include <memory>

namespace app {

class Buffer {
 public:
  explicit Buffer(size_t n) : data_(n) {}

  int& At(size_t index) {
    return data_[index];
  }

  void Grow(size_t extra) {
    int* raw = &data_[0];
    data_.resize(data_.size() + extra);
    raw[0] = 42;
  }

 private:
  std::vector<int> data_;
};

}  // namespace app
"""

CPP_DIFF = """\
diff --git a/src/buffer.cpp b/src/buffer.cpp
index 1111111..2222222 100644
--- a/src/buffer.cpp
+++ b/src/buffer.cpp
@@ -14,3 +14,6 @@ class Buffer {
   void Grow(size_t extra) {
-    data_.resize(data_.size() + extra);
+    int* raw = &data_[0];
+    data_.resize(data_.size() + extra);
+    raw[0] = 42;
   }
"""

PY_SOURCE = """\
import os


class Config:
    def __init__(self, path):
        self.path = path

    def load(self, defaults={}):
        with open(self.path) as handle:
            data = handle.read()
        defaults.update({"raw": data})
        return defaults


def main():
    return Config("a.toml").load()
"""

PY_DIFF = """\
diff --git a/config.py b/config.py
index 3333333..4444444 100644
--- a/config.py
+++ b/config.py
@@ -7,4 +7,6 @@ class Config:

-    def load(self):
+    def load(self, defaults={}):
         with open(self.path) as handle:
             data = handle.read()
+        defaults.update({"raw": data})
+        return defaults
"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_parse_cpp_diff() -> None:
    files = parse_unified_diff(CPP_DIFF)
    _check(len(files) == 1, f"파일 1개를 기대했으나 {len(files)}개")
    fd = files[0]
    _check(fd.path == "src/buffer.cpp", f"경로 불일치: {fd.path}")
    _check(fd.language is Language.CPP, f"언어 판별 실패: {fd.language}")
    _check(len(fd.hunks) == 1, f"hunk 1개를 기대했으나 {len(fd.hunks)}개")

    added = fd.hunks[0].added_linenos()
    _check(added == {15, 16, 17}, f"추가 라인 불일치: {sorted(added)}")


def test_chunk_cpp_expands_to_function() -> None:
    fd = parse_unified_diff(CPP_DIFF)[0]
    chunks = Chunker().chunk_file(fd, CPP_SOURCE)
    _check(len(chunks) == 1, f"청크 1개를 기대했으나 {len(chunks)}개")

    chunk = chunks[0]
    rendered = chunk.render_code()

    # 확장이 Grow 함수 시그니처를 포함해야 한다 — 리뷰어가 함수 계약을 봐야 하므로.
    _check("void Grow" in rendered, f"함수 시그니처가 청크에 없음:\n{rendered}")
    # 라인 상태 주석이 붙어야 한다.
    _check("[added @15]" in rendered, f"추가 라인 주석 누락:\n{rendered}")
    _check("[deleted @" in rendered, f"삭제 라인 주석 누락:\n{rendered}")
    _check("[unchanged @" in rendered, f"미변경 라인 주석 누락:\n{rendered}")
    # 확장 상한이 지켜져야 한다: 원본 hunk 3라인 → 4배 = 12라인 상한.
    span = chunk.end_line - chunk.start_line + 1
    _check(span <= 12, f"확장 상한 위반: {span}라인 (start={chunk.start_line}, end={chunk.end_line})")
    _check(chunk.changed_linenos == {15, 16, 17}, f"변경 라인 집합 불일치: {sorted(chunk.changed_linenos)}")


def test_chunk_python_block() -> None:
    fd = parse_unified_diff(PY_DIFF)[0]
    chunks = Chunker().chunk_file(fd, PY_SOURCE)
    _check(len(chunks) == 1, f"청크 1개를 기대했으나 {len(chunks)}개")

    chunk = chunks[0]
    rendered = chunk.render_code()
    _check(chunk.language is Language.PYTHON, f"언어 판별 실패: {chunk.language}")
    _check("def load" in rendered, f"함수 시그니처가 청크에 없음:\n{rendered}")
    _check("[added @" in rendered, f"추가 라인 주석 누락:\n{rendered}")
    # 변경 라인이 실제 소스 위치와 정확히 일치해야 한다.
    _check(chunk.changed_linenos == {8, 11, 12}, f"변경 라인 불일치: {sorted(chunk.changed_linenos)}")
    # 클래스 전체가 아니라 load 메서드로 좁혀져야 한다.
    _check(chunk.enclosing_symbol == "load", f"심볼이 좁혀지지 않음: {chunk.enclosing_symbol}")


def test_stale_source_is_rejected() -> None:
    """diff 이후 파일이 바뀌면 조용히 넘어가지 않고 실패해야 한다.

    이걸 놓치면 라인 번호가 통째로 밀린 채 '자신 있게 틀린' 리뷰가 나간다.
    """
    fd = parse_unified_diff(PY_DIFF)[0]
    stale = "# 앞에 라인이 하나 추가되어 이후 전체가 밀린 상태\n" + PY_SOURCE
    try:
        Chunker().chunk_file(fd, stale)
    except DiffSourceMismatch:
        pass
    else:
        raise AssertionError("불일치를 감지하지 못했다")

    # warn 모드에서는 진행하되 경고만 남긴다.
    chunks = Chunker(on_mismatch="warn").chunk_file(fd, stale)
    _check(len(chunks) >= 1, "warn 모드에서 청크가 생성되지 않음")


def test_replacement_hunk_at_module_level() -> None:
    """삭제 1줄 + 추가 1줄로만 이루어진 hunk 도 청크가 나와야 한다."""
    diff = """\
diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -14,3 +14,3 @@

 def main():
-    return Config("a.toml").load()
+    return None
"""
    # diff 는 PY_SOURCE 를 *변경 전* 으로 본다. 변경 후 내용을 넘겨야 정합적이다.
    new_source = PY_SOURCE.replace('    return Config("a.toml").load()', "    return None")

    fd = parse_unified_diff(diff)[0]
    chunks = Chunker().chunk_file(fd, new_source)
    _check(len(chunks) >= 1, "변경 hunk 에서 청크가 생성되지 않음")

    chunk = chunks[0]
    _check(chunk.changed_linenos == {16}, f"변경 라인 불일치: {sorted(chunk.changed_linenos)}")
    rendered = chunk.render_code()
    _check("[deleted @16]" in rendered, f"삭제 라인 주석 누락:\n{rendered}")
    _check("[added @16]" in rendered, f"추가 라인 주석 누락:\n{rendered}")


def test_line_annotation_is_unambiguous() -> None:
    """모든 렌더 라인이 `[status @lineno] ` 접두어를 갖는지 확인한다.

    이 형식이 깨지면 필터의 결정론적 라인 검증이 무력화되므로 회귀 방지가 중요하다.
    """
    fd = parse_unified_diff(CPP_DIFF)[0]
    chunk = Chunker().chunk_file(fd, CPP_SOURCE)[0]
    valid = {s.value for s in LineStatus}
    for line in chunk.render_code().splitlines():
        _check(line.startswith("["), f"주석 접두어 없음: {line!r}")
        status = line[1 : line.index(" @")]
        _check(status in valid, f"알 수 없는 상태 {status!r}: {line!r}")


TESTS = [
    test_parse_cpp_diff,
    test_chunk_cpp_expands_to_function,
    test_chunk_python_block,
    test_stale_source_is_rejected,
    test_replacement_hunk_at_module_level,
    test_line_annotation_is_unambiguous,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
    from crx.cli import force_utf8_output

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
