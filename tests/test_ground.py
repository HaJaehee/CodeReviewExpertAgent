"""정적분석 출력 파서 검증.

도구를 실제로 설치하지 않고 실제 출력 형식만 먹여 파서를 검증한다.
정규식이 이 파이프라인에서 가장 깨지기 쉬운 부분이므로 회귀 방지가 중요하다.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crex.ground import (  # noqa: E402
    Bandit,
    ClangTidy,
    Cppcheck,
    GroundingGate,
    Mypy,
    RoslynAnalyzers,
    Ruff,
    Semgrep,
    find_dotnet_project,
)
from crex.schema import Language, ReviewChunk, Severity, StaticFinding  # noqa: E402

CLANG_TIDY_OUT = """\
src/buffer.cpp:16:5: warning: pointer 'raw' is invalidated by call to 'resize' [bugprone-dangling-handle]
src/buffer.cpp:20:9: warning: 'memcpy' may overflow destination buffer [clang-analyzer-security.insecureAPI]
src/buffer.cpp:16:5: note: pointer obtained here
src/other.cpp:3:1: error: use of undeclared identifier 'foo' [clang-diagnostic-error]
"""

CPPCHECK_OUT = """\
src/buffer.cpp:17:5: error: Dereferencing pointer that may be invalid [invalidPointer]
src/buffer.cpp:22:1: performance: Function parameter 'name' should be passed by const reference [passedByValue]
src/buffer.cpp:30:3: style: Variable 'unused' is assigned a value that is never used [unreadVariable]
"""

MSBUILD_OUT = """\
  Determining projects to restore...
D:\\repo\\src\\Service.cs(42,13): warning CA2000: Call System.IDisposable.Dispose on object [D:\\repo\\src\\App.csproj]
D:\\repo\\src\\Service.cs(58,9): warning CS4014: Because this call is not awaited, execution continues [D:\\repo\\src\\App.csproj]
D:\\repo\\src\\Bad.cs(7,5): error CS0103: The name 'x' does not exist in the current context [D:\\repo\\src\\App.csproj]
Build succeeded.
"""

MYPY_OUT = """\
config.py:11:9: error: Argument 1 has incompatible type "str"; expected "int"  [arg-type]
config.py:14:1: note: See https://mypy.rtfd.io/en/stable/
util.py:3:1: error: Cannot find implementation for module "missing"  [import-not-found]
"""

RUFF_JSON = """\
[
  {"code": "B006", "message": "Do not use mutable data structures for argument defaults",
   "filename": "/repo/config.py", "location": {"row": 8, "column": 22}},
  {"code": "E722", "message": "Do not use bare `except`",
   "filename": "/repo/config.py", "location": {"row": 30, "column": 5}}
]
"""

BANDIT_JSON = """\
{"results": [
  {"filename": "config.py", "line_number": 21, "test_id": "B602",
   "issue_text": "subprocess call with shell=True identified", "issue_severity": "HIGH"}
]}
"""

SEMGREP_JSON = """\
{"results": [
  {"check_id": "python.lang.security.audit.eval-detected",
   "path": "config.py", "start": {"line": 33, "col": 12},
   "extra": {"message": "Detected eval() on user input", "severity": "ERROR"}}
]}
"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_clang_tidy_parse() -> None:
    findings = ClangTidy().parse(CLANG_TIDY_OUT, "")
    # note 라인은 직전 경고의 부연이므로 제외되어야 한다.
    _check(len(findings) == 3, f"3건을 기대했으나 {len(findings)}건: {[f.rule_id for f in findings]}")

    first = findings[0]
    _check(first.path == "src/buffer.cpp", f"경로: {first.path}")
    _check(first.line == 16, f"라인: {first.line}")
    _check(first.column == 5, f"컬럼: {first.column}")
    _check(first.rule_id == "bugprone-dangling-handle", f"룰: {first.rule_id}")
    _check("invalidated" in first.message, f"메시지: {first.message}")
    _check(first.severity is Severity.MEDIUM, f"심각도: {first.severity}")

    _check(findings[2].severity is Severity.HIGH, f"error 는 HIGH 여야 한다: {findings[2].severity}")


def test_cppcheck_parse() -> None:
    # cppcheck 는 진단을 stderr 로 낸다.
    findings = Cppcheck().parse("", CPPCHECK_OUT)
    _check(len(findings) == 3, f"3건을 기대했으나 {len(findings)}건")
    _check(findings[0].rule_id == "invalidPointer", f"룰: {findings[0].rule_id}")
    _check(findings[0].severity is Severity.HIGH, f"심각도: {findings[0].severity}")
    _check(findings[1].severity is Severity.MEDIUM, f"performance 심각도: {findings[1].severity}")
    _check(findings[2].severity is Severity.LOW, f"style 심각도: {findings[2].severity}")


#: 실제 `dotnet build -v normal` 이 내보내는 모양. 같은 경고가 두 번 나오고,
#: 한쪽에는 다중 노드 로거의 노드 번호("1>")가 경로 앞에 붙는다.
#: 2026-08-27, .NET SDK 8.0.100 실측.
MSBUILD_DUPLICATED_OUT = """\
     1>D:\\repo\\src\\Program.cs(8,20): warning CS8600: null 리터럴 변환 [D:\\repo\\src\\App.csproj]
     1>D:\\repo\\src\\Program.cs(7,13): warning CS0219: 'unused' 할당되었지만 사용되지 않음 [D:\\repo\\src\\App.csproj]
Build succeeded.

경고:
         D:\\repo\\src\\Program.cs(8,20): warning CS8600: null 리터럴 변환 [D:\\repo\\src\\App.csproj]
         D:\\repo\\src\\Program.cs(7,13): warning CS0219: 'unused' 할당되었지만 사용되지 않음 [D:\\repo\\src\\App.csproj]

    경고 2개
"""


#: 실제 clang-tidy 가 Windows 에서 내보내는 모양. 상대 경로를 줘도 절대 경로로
#: 되받아 내고, 거기에는 드라이브 문자의 콜론이 들어 있다.
#: 2026-08-27, Visual Studio 2022 의 clang-tidy 실측.
CLANG_TIDY_WINDOWS_OUT = """\
C:\\repo\\src\\buffer.cpp:6:3: warning: Call to function 'strcpy' is insecure [clang-analyzer-security.insecureAPI.strcpy]
C:\\repo\\src\\buffer.cpp:6:3: note: Call to function 'strcpy' is insecure
C:\\repo\\src\\buffer.cpp:7:3: warning: 'memcpy' size exceeds destination [clang-analyzer-unix.cstring.BadSizeArg]
"""

def test_msbuild_parse() -> None:
    findings = RoslynAnalyzers().parse(MSBUILD_OUT, "")
    _check(len(findings) == 3, f"3건을 기대했으나 {len(findings)}건: {[f.rule_id for f in findings]}")

    first = findings[0]
    _check(first.line == 42, f"라인: {first.line}")
    _check(first.rule_id == "CA2000", f"룰: {first.rule_id}")
    # 경로가 정규화되고 프로젝트 접미사 [.csproj] 가 메시지에서 제거되어야 한다.
    _check(first.path.endswith("src/Service.cs"), f"경로: {first.path}")
    _check("csproj" not in first.message, f"메시지에 프로젝트 경로가 남음: {first.message}")
    _check(findings[2].severity is Severity.HIGH, f"error 심각도: {findings[2].severity}")


def test_mypy_parse() -> None:
    findings = Mypy().parse(MYPY_OUT, "")
    _check(len(findings) == 2, f"2건을 기대했으나 {len(findings)}건")
    _check(findings[0].rule_id == "arg-type", f"룰: {findings[0].rule_id}")
    _check(findings[0].line == 11, f"라인: {findings[0].line}")


def test_ruff_parse() -> None:
    findings = Ruff().parse(RUFF_JSON, "")
    _check(len(findings) == 2, f"2건을 기대했으나 {len(findings)}건")
    _check(findings[0].rule_id == "B006", f"룰: {findings[0].rule_id}")
    _check(findings[0].line == 8, f"라인: {findings[0].line}")


def test_bandit_parse() -> None:
    findings = Bandit().parse(BANDIT_JSON, "")
    _check(len(findings) == 1, f"1건을 기대했으나 {len(findings)}건")
    _check(findings[0].severity is Severity.HIGH, f"심각도: {findings[0].severity}")
    _check(findings[0].rule_id == "B602", f"룰: {findings[0].rule_id}")


def test_semgrep_parse() -> None:
    findings = Semgrep().parse(SEMGREP_JSON, "")
    _check(len(findings) == 1, f"1건을 기대했으나 {len(findings)}건")
    _check(findings[0].line == 33, f"라인: {findings[0].line}")
    _check(findings[0].severity is Severity.HIGH, f"ERROR 는 HIGH: {findings[0].severity}")


def test_empty_output_is_safe() -> None:
    """도구가 아무것도 못 찾으면 빈 출력이 온다. 파서가 터지면 안 된다."""
    for analyzer in (ClangTidy(), Cppcheck(), Mypy(), RoslynAnalyzers()):
        _check(analyzer.parse("", "") == [], f"{analyzer.name} 이 빈 출력에서 결과를 냄")
    for analyzer in (Ruff(), Bandit(), Semgrep()):
        _check(analyzer.parse("", "") == [], f"{analyzer.name} 이 빈 출력에서 결과를 냄")


def test_missing_tool_is_skipped_not_fatal() -> None:
    """설치되지 않은 도구는 조용히 건너뛴다 — 폐쇄망에서 필수 동작."""

    class Ghost(ClangTidy):
        name = "ghost"
        executable = "definitely-not-installed-xyz"

    result = Ghost().run(["a.cpp"], Path.cwd())
    _check(result.skipped, "미설치 도구가 건너뛰어지지 않음")
    _check("PATH" in result.skip_reason, f"사유가 불명확: {result.skip_reason}")
    _check(result.findings == [], "건너뛴 도구가 결과를 냄")


def test_attach_matches_by_path_suffix() -> None:
    """분석기가 절대경로를 내도 상대경로 청크에 붙어야 한다."""
    chunk = ReviewChunk(
        chunk_id="src/buffer.cpp#0",
        path="src/buffer.cpp",
        language=Language.CPP,
        start_line=14,
        end_line=18,
    )
    other = ReviewChunk(
        chunk_id="src/buffer.cpp#1",
        path="src/buffer.cpp",
        language=Language.CPP,
        start_line=30,
        end_line=40,
    )
    findings = [
        StaticFinding("cppcheck", "/build/repo/src/buffer.cpp", 16, "invalidPointer", "bad"),
        StaticFinding("cppcheck", "src/buffer.cpp", 35, "unreadVariable", "unused"),
        StaticFinding("cppcheck", "src/elsewhere.cpp", 16, "x", "다른 파일"),
        StaticFinding("cppcheck", "src/buffer.cpp", 999, "y", "범위 밖"),
    ]
    GroundingGate.attach([chunk, other], findings)

    _check(len(chunk.static_findings) == 1, f"청크0 결과: {len(chunk.static_findings)}건")
    _check(chunk.static_findings[0].rule_id == "invalidPointer", "잘못된 지적이 붙음")
    _check(len(other.static_findings) == 1, f"청크1 결과: {len(other.static_findings)}건")
    _check("[cppcheck:invalidPointer]" in chunk.render_static_findings(),
           f"렌더 형식 불일치: {chunk.render_static_findings()}")


def test_render_static_findings_when_empty() -> None:
    """정적분석 결과가 없을 때도 프롬프트에 넣을 문구가 나와야 한다."""
    chunk = ReviewChunk("x#0", "x.py", Language.PYTHON, 1, 10)
    rendered = chunk.render_static_findings()
    _check("없음" in rendered, f"빈 결과 표기 누락: {rendered}")


def test_clang_tidy_respects_project_config() -> None:
    """팀의 .clang-tidy 가 있으면 --checks 로 덮어쓰면 안 된다.

    사내 코딩 룰이 그 파일에 들어 있는데 명령줄 --checks 가 Checks 를 덮어쓴다.
    조용히 무시되면 "룰을 넣었는데 아무 일도 안 일어나요" 가 된다.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # .clang-tidy 가 없으면 기본 체크를 넘긴다
        without = ClangTidy(project_root=root).build_command(["a.cpp"])
        _check(any(c.startswith("--checks=") for c in without),
               f"기본 체크가 없음: {without}")
        _check("bugprone-*" in " ".join(without), f"기본 체크 내용 불일치: {without}")

        # .clang-tidy 가 있으면 --checks 를 넘기지 않는다
        (root / ".clang-tidy").write_text("Checks: '-*,readability-*'\n", encoding="utf-8")
        with_config = ClangTidy(project_root=root).build_command(["a.cpp"])
        _check(not any(c.startswith("--checks=") for c in with_config),
               f"팀 설정이 있는데 --checks 를 덮어씀: {with_config}")

        # 명시적으로 지정하면 그것이 이긴다
        explicit = ClangTidy(project_root=root, checks="-*,cert-*").build_command(["a.cpp"])
        _check("--checks=-*,cert-*" in explicit, f"명시 설정이 무시됨: {explicit}")


def test_clang_tidy_checks_configurable() -> None:
    """crex.json 에서 체크 목록을 바꿀 수 있어야 한다."""
    from crex.config import GroundingConfig

    config = GroundingConfig(clang_tidy_checks="-*,modernize-*")
    _check(config.clang_tidy_checks == "-*,modernize-*", "설정 키가 없다")


def test_optional_analyzers_are_reachable_from_config() -> None:
    """roslynator/semgrep 은 이름을 적으면 실제로 켜져야 한다.

    문서가 사용 가능하다고 적어둔 이름이 코드에서 도달 불가능하면 그건 거짓말이다.
    """
    from crex.config import ChunkingConfig, Config, GroundingConfig, ReviewConfig
    from crex.ground import ALL_ANALYZER_NAMES, Roslynator, Semgrep
    from crex.llm import EndpointConfig
    from crex.pipeline import Pipeline

    endpoint = EndpointConfig(base_url="http://x/v1", model="m")

    def gate_names(**grounding_kwargs):
        config = Config(
            generator=endpoint,
            verifier=endpoint,
            review=ReviewConfig(),
            grounding=GroundingConfig(**grounding_kwargs),
            chunking=ChunkingConfig(),
        )
        pipeline = Pipeline.__new__(Pipeline)   # 택소노미 로딩·LLM 연결을 건너뛴다
        pipeline.config = config
        return {a.name for a in pipeline._build_gate(Path.cwd()).analyzers}

    # 기본: 6종, 옵션 2종은 빠짐
    default = gate_names()
    _check(len(default) == 6, f"기본 분석기 6종을 기대했으나 {sorted(default)}")
    _check(Roslynator.name not in default, "roslynator 가 기본에 포함됨")
    _check(Semgrep.name not in default, "semgrep 이 설정 없이 포함됨")

    # 이름을 적으면 켜진다
    named = gate_names(analyzers=["roslynator"])
    _check(named == {"roslynator"}, f"roslynator 만 켜져야 한다: {sorted(named)}")

    # semgrep 은 룰팩 경로가 있어야 켜진다
    _check(gate_names(analyzers=["semgrep"]) == set(), "semgrep 이 룰팩 없이 켜짐")
    with_pack = gate_names(analyzers=["semgrep"], semgrep_config="/opt/rules")
    _check(with_pack == {"semgrep"}, f"룰팩 지정 시 켜져야 한다: {sorted(with_pack)}")

    # 룰팩만 있으면 기본 6종 + semgrep
    auto = gate_names(semgrep_config="/opt/rules")
    _check(len(auto) == 7, f"기본+semgrep 7종을 기대했으나 {sorted(auto)}")

    # 오타는 조용히 넘어가면 안 된다 — 기본 분석기까지 전부 사라진다
    try:
        GroundingConfig(analyzers=["ruf"])
    except ValueError as exc:
        _check("알 수 없는 분석기" in str(exc), f"오류 메시지 불명확: {exc}")
    else:
        raise AssertionError("오타난 분석기 이름이 조용히 통과됨")

    _check(len(ALL_ANALYZER_NAMES) == 8, f"분석기 이름 8종: {sorted(ALL_ANALYZER_NAMES)}")



def test_msbuild_duplicate_diagnostics_are_folded() -> None:
    """같은 경고가 프로젝트 출력과 말미 요약에 두 번 나오는데, 한 건이어야 한다.

    접지 않으면 정적분석 근거가 2배로 부풀어 프롬프트에 들어가고, 모델은 서로
    다른 두 도구가 같은 곳을 지적한 것으로 읽는다.
    """
    findings = RoslynAnalyzers().parse(MSBUILD_DUPLICATED_OUT, "")
    _check(len(findings) == 2, f"2건을 기대했으나 {len(findings)}건: {[f.rule_id for f in findings]}")
    rules = sorted(f.rule_id for f in findings)
    _check(rules == ["CS0219", "CS8600"], f"룰: {rules}")


def test_msbuild_node_prefix_is_stripped_from_path() -> None:
    """다중 노드 로거의 "1>" 가 경로 앞에 남으면 프롬프트에 깨진 경로가 나간다."""
    findings = RoslynAnalyzers().parse(MSBUILD_DUPLICATED_OUT, "")
    for finding in findings:
        _check(not finding.path.startswith("1>"), f"노드 번호가 남음: {finding.path}")
        _check(finding.path.endswith("src/Program.cs"), f"경로: {finding.path}")


def test_clang_tidy_is_found_inside_visual_studio() -> None:
    """Visual Studio 는 clang-tidy 를 PATH 에 넣지 않는다.

    PATH 만 보면 설치된 장비에서도 '없음' 으로 뜨고 C++ 그라운딩이 통째로 빠진다.
    VS 없는 장비에서도 돌아야 하므로 탐색 함수를 갈아끼워 검증한다.
    """
    import crex.compiledb as compiledb
    import crex.ground as ground

    fake = Path("D:/VS/VC/Tools/Llvm/x64/bin/clang-tidy.exe")
    original_which = ground.shutil.which
    original_find = compiledb.find_clang_tidy
    try:
        ground.shutil.which = lambda name: None          # PATH 에는 없다
        compiledb.find_clang_tidy = lambda env=None: fake  # VS 안에는 있다
        analyzer = ClangTidy()
        _check(analyzer.available(), "VS 안의 clang-tidy 를 못 찾음")
        _check(analyzer.resolve_executable() == str(fake), f"해석 결과: {analyzer.resolve_executable()}")
    finally:
        ground.shutil.which = original_which
        compiledb.find_clang_tidy = original_find


def test_resolved_path_actually_runs() -> None:
    """PATH 밖에서 찾은 경로가 실제 실행에 쓰여야 한다. 이름만 바꿔선 안 돈다.

    가짜 도구를 만들어 실제 python 실행 파일로 해석시키고, 그 출력이 파서까지
    도달하는지 본다 — subprocess 를 흉내내지 않고 진짜로 돌린다.
    """
    import sys as _sys

    class NotOnPath(ClangTidy):
        name = "not-on-path"
        executable = "definitely-not-installed-xyz"

        def _locate(self) -> str | None:
            return _sys.executable

        def build_command(self, paths: list[str]) -> list[str]:
            return [self.executable, "-c", r"print(r'a.cpp:1:2: warning: 지어낸 경고 [x-y]')"]

    result = NotOnPath().run(["a.cpp"], Path.cwd())
    _check(not result.skipped, f"건너뛰어짐: {result.skip_reason}")
    _check(len(result.findings) == 1, f"1건을 기대했으나 {len(result.findings)}건")
    _check(result.findings[0].line == 1, f"라인: {result.findings[0].line}")



def test_gnu_style_accepts_windows_absolute_paths() -> None:
    """드라이브 문자의 콜론이 경로 매칭을 끊으면 줄 전체가 매칭에 실패한다.

    clang-tidy 는 상대 경로를 줘도 절대 경로로 되받아 내므로, 이게 깨지면 사내
    Windows 장비에서 C++ 그라운딩이 언제나 0건이 된다. 조용한 0건이라 아무도
    눈치채지 못한 채 LLM 은 '도구가 아무것도 못 찾았다'는 거짓 근거를 받는다.
    """
    findings = ClangTidy().parse(CLANG_TIDY_WINDOWS_OUT, "")
    # note 는 직전 경고의 부연이므로 접힌다 — 경고 2건만 남아야 한다.
    _check(len(findings) == 2, f"2건을 기대했으나 {len(findings)}건")

    first = findings[0]
    _check(first.line == 6, f"라인: {first.line}")
    _check(first.path == "C:/repo/src/buffer.cpp", f"경로: {first.path}")
    _check(first.rule_id == "clang-analyzer-security.insecureAPI.strcpy", f"룰: {first.rule_id}")
# --------------------------------------------------------------------------
# C# — 빌드할 프로젝트 정하기
# --------------------------------------------------------------------------


def _csharp_repo(root: Path, projects: tuple[str, ...], solutions: tuple[str, ...] = ()) -> None:
    for project in projects:
        path = root / project
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<Project />", encoding="utf-8")
    for solution in solutions:
        (root / solution).write_text("", encoding="utf-8")


def test_dotnet_project_follows_the_changed_file() -> None:
    """바뀐 파일이 속한 프로젝트만 빌드하면 된다. 솔루션 전체를 태울 이유가 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _csharp_repo(root, ("src/Api/Api.csproj", "src/Core/Core.csproj"), ("App.sln",))

        found = find_dotnet_project(root, ["src/Core/Service.cs"])
        _check(found == root / "src" / "Core" / "Core.csproj", f"{found}")


def test_dotnet_project_rises_to_solution_when_files_span_projects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _csharp_repo(root, ("src/Api/Api.csproj", "src/Core/Core.csproj"), ("App.sln",))

        found = find_dotnet_project(root, ["src/Core/A.cs", "src/Api/B.cs"])
        _check(found == root / "App.sln", f"{found}")


def test_dotnet_project_gives_up_rather_than_guessing() -> None:
    """솔루션이 없고 프로젝트가 여럿이면 하나를 고르면 안 된다.

    몰래 고르면 나머지 프로젝트의 경고가 통째로 빠진 채 리뷰가 돈다 —
    도구가 검사해서 깨끗한 것과 구분이 안 된다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _csharp_repo(root, ("src/Api/Api.csproj", "src/Core/Core.csproj"))

        _check(find_dotnet_project(root, ["src/Core/A.cs", "src/Api/B.cs"]) is None, "골라버렸다")
        _check(find_dotnet_project(root, []) is None, "대상 없이도 골라버렸다")


def test_dotnet_project_found_without_paths() -> None:
    """doctor 처럼 바뀐 파일이 없을 때도 하나뿐이면 찾아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _csharp_repo(root, ("src/Api/Api.csproj",))
        _check(find_dotnet_project(root, []) == root / "src" / "Api" / "Api.csproj", "못 찾았다")


def test_roslyn_skips_loudly_when_project_is_undecidable() -> None:
    """조용히 0건을 내지 않는다 — 0건은 '검사했고 깨끗하다' 로 읽히기 때문이다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _csharp_repo(root, ("src/Api/Api.csproj", "src/Core/Core.csproj"))

        analyzer = RoslynAnalyzers()
        # dotnet 유무와 무관하게 판정이 같아야 하므로 설치된 것으로 두고 본다.
        analyzer.available = lambda: True  # type: ignore[method-assign]
        result = analyzer.run(["src/Core/A.cs", "src/Api/B.cs"], root)
        _check(result.skipped, "건너뛰지 않았다")
        _check("dotnet_project" in result.skip_reason, f"다음 행동이 없다: {result.skip_reason}")

        # 도구가 아예 없을 때는 프로젝트 얘기가 아니라 그 얘기를 해야 한다.
        missing = RoslynAnalyzers()
        missing.available = lambda: False  # type: ignore[method-assign]
        reason = missing.run(["src/Core/A.cs", "src/Api/B.cs"], root).skip_reason
        _check("dotnet" in reason and "dotnet_project" not in reason, f"이유가 엉뚱하다: {reason}")


def test_roslyn_command_forces_recompilation() -> None:
    """방금 빌드한 솔루션이면 증분 빌드는 경고를 한 줄도 내지 않는다."""
    analyzer = RoslynAnalyzers(project="src/App.sln")
    analyzer._target = analyzer.project
    command = analyzer.build_command(["src/A.cs"])

    _check("--no-incremental" in command, f"증분 빌드로 돈다: {command}")
    _check(command[-1] == "src/App.sln", f"대상이 안 붙었다: {command}")
    _check("-v" in command and command[command.index("-v") + 1] == "normal",
           f"경고가 보이는 verbosity 가 아니다: {command}")


def test_roslyn_reports_a_failed_build_instead_of_zero_findings() -> None:
    """폐쇄망에서 제일 흔한 실패가 NuGet 복원이다. 그 오류는 경고 파서에 안 걸린다."""
    import subprocess

    analyzer = RoslynAnalyzers()
    failed = subprocess.CompletedProcess(
        args=["dotnet", "build"], returncode=1,
        stdout="/w/App.csproj : error NU1101: Unable to find package Foo.\n", stderr="",
    )
    reason = analyzer.diagnose(failed, [])
    _check(reason is not None and "NU1101" in reason, f"이유를 못 만들었다: {reason}")

    # 컴파일 오류는 걷힌 지적이다. 그건 실패가 아니라 결과다.
    compiled_with_errors = subprocess.CompletedProcess(
        args=["dotnet", "build"], returncode=1, stdout="", stderr=""
    )
    findings = RoslynAnalyzers().parse(MSBUILD_OUT, "")
    _check(analyzer.diagnose(compiled_with_errors, findings) is None, "결과가 있는데 건너뛰었다")

    ok = subprocess.CompletedProcess(args=["dotnet", "build"], returncode=0, stdout="", stderr="")
    _check(analyzer.diagnose(ok, []) is None, "성공했는데 건너뛰었다")


TESTS = [
    test_clang_tidy_parse,
    test_cppcheck_parse,
    test_msbuild_parse,
    test_mypy_parse,
    test_ruff_parse,
    test_bandit_parse,
    test_semgrep_parse,
    test_empty_output_is_safe,
    test_missing_tool_is_skipped_not_fatal,
    test_attach_matches_by_path_suffix,
    test_render_static_findings_when_empty,
    test_clang_tidy_respects_project_config,
    test_clang_tidy_checks_configurable,
    test_optional_analyzers_are_reachable_from_config,
    test_msbuild_duplicate_diagnostics_are_folded,
    test_msbuild_node_prefix_is_stripped_from_path,
    test_clang_tidy_is_found_inside_visual_studio,
    test_resolved_path_actually_runs,
    test_gnu_style_accepts_windows_absolute_paths,
    test_dotnet_project_follows_the_changed_file,
    test_dotnet_project_rises_to_solution_when_files_span_projects,
    test_dotnet_project_gives_up_rather_than_guessing,
    test_dotnet_project_found_without_paths,
    test_roslyn_skips_loudly_when_project_is_undecidable,
    test_roslyn_command_forces_recompilation,
    test_roslyn_reports_a_failed_build_instead_of_zero_findings,
]


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 다. 출력에 한글과 기호가 섞여 있어
    # 맞춰주지 않으면 테스트 러너 자체가 UnicodeEncodeError 로 죽는다.
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
