# 정적분석 도구

CREX 는 LLM 을 부르기 **전에** 정적분석기를 돌립니다. 그 결과를 프롬프트에 넣어
모델의 역할을 바꾸기 위해서입니다 — "결함을 찾아라"가 아니라 **"이 도구 결과를
검증하고, 도구가 못 잡는 로직·설계 결함만 추가하라"**.

이 전환이 환각 억제의 첫 번째 겹입니다. 소형 모델이 "여기 널 체크가 없다"고
지어내는 대신, 도구가 실제로 확인한 항목을 놓고 판단하게 됩니다.

**없어도 리뷰는 됩니다.** PATH 에서 못 찾으면 그 분석기만 조용히 건너뛰고
파이프라인은 계속 돕니다. 폐쇄망에서는 장비마다 설치 상태가 다르므로, 하나가
없다고 멈춰서는 안 되기 때문입니다.

```
INFO    crex.ground: [cppcheck] 건너뜀 — cppcheck 를 PATH 에서 찾을 수 없다
```

다만 그만큼 근거 없는 지적이 늘어납니다. 쓰는 언어에 해당하는 것만이라도
채우세요. C++ 이라면 최소 하나는 꼭 필요합니다.

---

## 무엇이 필요한가

`python -m crex doctor` 가 현재 상태를 한 화면에 보여줍니다.

```
정적분석 도구
  OK  clang-tidy (clang-tidy)
  없음 cppcheck (cppcheck)
  OK  roslyn (dotnet)
  OK  ruff (ruff)
  없음 mypy (mypy)
  없음 bandit (bandit)
```

| 이름 | 언어 | 실행 파일 | 기본 실행 | 라이선스 |
|---|---|---|---|---|
| `clang-tidy` | C++ | `clang-tidy` | 예 | Apache-2.0 with LLVM-exception |
| `cppcheck` | C++ | `cppcheck` | 예 | **GPL-3.0** |
| `roslyn` | C# | `dotnet` | 예 | MIT (.NET SDK) |
| `ruff` | Python | `ruff` | 예 | MIT |
| `mypy` | Python | `mypy` | 예 | MIT |
| `bandit` | Python | `bandit` | 예 | Apache-2.0 |
| `roslynator` | C# | `roslynator` | 아니오 | Apache-2.0 |
| `semgrep` | 전체 | `semgrep` | 아니오 | LGPL-2.1 (룰팩은 별도, 아래 참고) |

"기본 실행"이 예인 것은 설정 없이 자동으로 돕니다. 아니오인 둘은
`grounding.analyzers` 에 이름을 적어야 켜집니다. 다만 semgrep 은 `analyzers` 를
비워 둔 채 `semgrep_config` 만 지정해도 켜집니다 — 룰팩 경로를 적었다는 것은 쓰겠다는
뜻이기 때문입니다. 자세한 것은
[설정의 `[grounding]`](configuration.md#grounding--정적분석)에 있습니다.

전부 무료 오픈소스입니다. 사용료도, 사용자 수 제한도, 기간 만료도 없습니다.
반입 신청서에는 "프리웨어" 대신 위 표의 라이선스 이름을 그대로 적는 편이
심사에 유리합니다.

---

## C++ — clang-tidy

LLVM 공식 인스톨러에 `bin\clang-tidy.exe` 로 포함됩니다.

- 내려받는 곳: [releases.llvm.org](https://releases.llvm.org/download.html) 또는
  [GitHub Releases](https://github.com/llvm/llvm-project/releases)
- Windows 파일: `LLVM-<버전>-win64.exe` (툴체인 전체라 400MB 을 넘습니다)
- Linux: `apt install clang-tidy` / `dnf install clang-tools-extra`

> **Visual Studio 가 이미 있다면** 설치 관리자에서 **"Windows용 C++ Clang 도구"**
> 컴포넌트만 체크하면 clang-tidy 가 같이 들어옵니다. 이미 승인된 소프트웨어의
> 옵션이라 반입 신청이 아예 필요 없을 수 있습니다. 다만 그 실행 파일
> (`...\VC\Tools\Llvm\x64\bin\clang-tidy.exe`)은 PATH 에 자동으로 올라가지
> 않습니다. CREX 는 PATH 에서만 찾으므로 그 디렉터리를 직접 추가하고
> `python -m crex doctor` 로 확인하세요.

### compile_commands.json 이 없으면 반쯤 눈을 감습니다

clang-tidy 는 컴파일 명령을 알아야 헤더를 찾습니다. CMake 라면 한 줄입니다.

```bash
cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build
```

그 경로를 설정에 적습니다. 파일 이름이 아니라 **파일이 들어 있는 디렉터리**를
적고, 상대 경로는 리뷰 대상 저장소 루트 기준입니다(분석기가 거기서 실행됩니다).
절대 경로도 받습니다.

```toml
[grounding]
compile_commands_dir = "build"
```

### Visual Studio 2022 에서 만들기

사내 C++ 프로젝트는 대개 여기에 걸립니다. 프로젝트 형식에 따라 갈립니다.

#### CMake 프로젝트 ("폴더 열기")

VS 2022 의 CMake 통합은 기본 제너레이터가 Ninja 라 그대로 됩니다.
`CMakePresets.json` 에 캐시 변수만 넣으세요.

```json
{
  "name": "x64-debug",
  "generator": "Ninja",
  "binaryDir": "${sourceDir}/out/build/${presetName}",
  "cacheVariables": { "CMAKE_EXPORT_COMPILE_COMMANDS": "ON" }
}
```

구성(Configure)만 하면 — 빌드까지 갈 필요 없습니다 —
`out/build/x64-debug/compile_commands.json` 이 생깁니다.

```toml
[grounding]
compile_commands_dir = "out/build/x64-debug"
```

`.sln` 을 만들어내는 **Visual Studio(MSBuild) 제너레이터는 이 변수를 무시**합니다.
`-G "Visual Studio 17 2022"` 로 구성했다면 파일이 안 생기니, Ninja 로 구성 디렉터리를
하나 더 만드는 편이 빠릅니다.

#### MSBuild(.vcxproj/.sln) 프로젝트

VS 자체에는 내보내기 기능이 없습니다. `속성 → 코드 분석 → Clang-Tidy` 는 컴파일
플래그를 내부적으로 합성해서 쓸 뿐이라 재활용할 수 없습니다. 세 갈래가 있습니다.

**(a) MSBuild 로거 — 폐쇄망에 제일 잘 맞습니다.**
[0xabu/MsBuildCompileCommandsJson](https://github.com/0xabu/MsBuildCompileCommandsJson)
은 C# 파일 하나짜리 로거입니다. NuGet 없이 VS 에 딸려오는 `csc` 로 컴파일되므로
반입 부담이 거의 없습니다.

```
msbuild App.sln /t:Rebuild /p:Configuration=Debug /p:Platform=x64 ^
        /logger:C:\tools\CompileCommandsJson.dll
```

실제 컴파일 호출을 관찰하는 방식이라 **반드시 Rebuild** 여야 전체 파일이 들어갑니다.
증분 빌드면 그때 컴파일된 것만 기록됩니다.

**(b) Microsoft 공식 샘플.**
[microsoft/msbuild-extractor-sample](https://github.com/microsoft/msbuild-extractor-sample)
은 MSBuild API 로 design-time 빌드(`GetClCommandLines`)만 돌려 뽑기 때문에 **실제
컴파일이 필요 없습니다**. MIT 이고 `-p/-s/-c/-a/-o` 옵션을 받습니다. 대신 .NET SDK 와
`dotnet build`(NuGet 복원)가 필요해서 폐쇄망에서는 (a)보다 준비가 큽니다.

**(c) 빌드 로그 변환.** `msbuild /v:detailed` 로그를
[ms2cc](https://github.com/freddiehaddad/ms2cc) 같은 도구로 변환합니다. `/v:detailed`
미만은 정보가 모자라 실패합니다.

#### 만든 뒤 확인할 것

- **MSVC 플래그 호환** — 엔트리의 컴파일러가 `cl.exe` 면 clang 툴링이 알아서 CL 드라이버
  모드로 붙습니다. 그래도 `/ZI`, `/Gm`, C++/CLI, 일부 PCH 옵션에서는 clang-tidy 가
  오류를 냅니다. CREX 에는 clang-tidy 추가 인자를 넣는 설정이 없으니, 이때는 저장소의
  `.clang-tidy` 에 `ExtraArgs` / `ExtraArgsBefore` 로 넣으세요.
- **구성 일치** — Debug/x64 로 뽑았으면 리뷰도 그 전제로 돕니다. `#ifdef _DEBUG` 로
  갈리는 코드는 결과가 달라집니다.
- **경로 표기** — TOML 에는 슬래시로 적으세요 (`"D:/work/repo/out/build/x64-debug"`).
- **빠른 검증** — `clang-tidy -p out/build/x64-debug src\foo.cpp` 가 헤더 못 찾는
  오류 없이 돌면 된 것입니다.

여기까지가 부담스럽다면 clang-tidy 를 포기하고 cppcheck 만 쓰는 것도 나쁘지 않습니다
— cppcheck 는 컴파일 DB 없이도 돌고, 오탐률이 매우 낮습니다.

### 어떤 체크를 켜나

`clang_tidy_checks` 를 비워 두면 프로젝트의 `.clang-tidy` 를 그대로 존중하고,
그것도 없으면 버그 탐지 위주의 기본값을 씁니다.

```
-*,bugprone-*,cert-*,clang-analyzer-*,concurrency-*,misc-*,performance-*
```

`readability-*` 같은 스타일 체크는 켜지 않습니다. 오탐이 많고, 스타일은 LLM 이
판단할 영역도 아닙니다. 여기서 나오는 항목은 전부 '실제 버그 후보'여야 합니다.

---

## C++ — cppcheck

- 내려받는 곳: [github.com/cppcheck-opensource/cppcheck](https://github.com/cppcheck-opensource/cppcheck/releases)
  (저장소가 최근 `danmar/cppcheck` 에서 이전했습니다. 옛 주소는 리다이렉트됩니다.)
  [SourceForge 미러](https://sourceforge.net/projects/cppcheck/)도 있습니다.
- Windows 파일: `cppcheck-<버전>-x64-Setup.msi` (20MB대)
- Linux: `apt install cppcheck`

### GPL-3.0 이라는 점

세 도구 중 유일하게 신경 쓸 라이선스입니다.

- CREX 는 cppcheck 를 **별도 프로세스로 실행**할 뿐입니다 (`crex/ground.py` 의
  `subprocess.run`). 라이브러리로 링크하지 않으므로 **회사 소스코드에 GPL 이
  전염되지 않습니다.** 컴파일러나 grep 을 쓰는 것과 같은 관계입니다.
- 사내에 바이너리를 배포한다면 소스 tarball 사본도 함께 반입해 보관해 두면
  논란의 여지가 없습니다.
- MSI 에 딸려오는 GUI(`cppcheck-gui.exe`)는 Qt(LGPL) 기반입니다. CREX 는 CLI 만
  쓰므로, 신경 쓰기 싫으면 설치할 때 GUI 컴포넌트를 빼세요.

**Cppcheck Premium 은 별개의 유료 상용 제품입니다**
([cppcheck.com](https://www.cppcheck.com/)). 원작자가 MISRA/CERT 룰 등을 얹어 파는
버전이고, 우리가 쓰는 것은 GPL 무료판입니다. 검색하다 구매 페이지에 먼저 닿으면
"이거 유료네" 하고 오해하기 쉬우니 구분하세요.

---

## C# — roslyn / roslynator

`roslyn` 은 **별도 설치가 필요 없습니다.** `dotnet build` 를 부르고 그 경고를
읽는 방식이라 .NET SDK 만 있으면 됩니다.

- .NET SDK: [dotnet.microsoft.com/download](https://dotnet.microsoft.com/download) (MIT)

빌드를 태우므로 프로젝트가 크면 리뷰 한 번에 몇 분씩 걸립니다. 감당하기 어렵다면
`grounding.analyzers` 에서 `roslyn` 을 빼세요.

`roslynator` 는 기본에서 빠져 있습니다. 별도 설치가 필요하고 `dotnet build` 와
겹치는 룰이 많기 때문입니다. 쓰려면 이름을 적으세요.

```bash
dotnet tool install -g roslynator.dotnet.cli
```

> roslynator CLI 자체에는 분석기가 들어 있지 않습니다. 프로젝트가 NuGet 으로
> 참조하는 분석기를 돌립니다. 아무것도 참조하지 않는 프로젝트라면 설치해도
> 0건이 나옵니다.

```toml
[grounding]
analyzers = ["clang-tidy", "cppcheck", "roslyn", "ruff", "mypy", "bandit", "roslynator"]
```

> `analyzers` 를 비우면 기본 6종이 전부 돕니다. **이름을 하나라도 적으면 적은
> 것만 돕니다.** roslynator 하나를 추가하려고 `["roslynator"]` 만 적으면 나머지가
> 전부 꺼집니다.

---

## Python — ruff / mypy / bandit

셋 다 pip 로 들어갑니다.

```bash
pip install ruff mypy bandit
```

ruff 는 단일 실행 파일이라 pip 없이도 됩니다. 폐쇄망에는 이쪽이 편합니다.

- [github.com/astral-sh/ruff/releases](https://github.com/astral-sh/ruff/releases) →
  `ruff-x86_64-pc-windows-msvc.zip` (10MB 남짓, 압축을 풀면 `ruff.exe` 하나뿐)
- ARM 장비라면 `ruff-aarch64-pc-windows-msvc.zip`

셋 다 퍼미시브 라이선스입니다 — ruff MIT, mypy MIT, bandit Apache-2.0.

mypy 는 타입 힌트가 거의 없는 코드베이스에서는 얻는 게 적습니다. 그런 저장소라면
빼도 됩니다.

---

## semgrep (선택)

보안 패턴 검사입니다. 폐쇄망에서 특히 손이 많이 갑니다.

- 엔진(Semgrep CE)은 LGPL-2.1 입니다.
- **`semgrep_config = "auto"` 는 폐쇄망에서 동작하지 않습니다.** 룰을 인터넷에서
  받아오기 때문입니다. 룰팩을 미리 반입하고 로컬 경로를 지정해야 합니다.
- **룰팩 라이선스를 따로 확인하세요.** semgrep 이 관리하는 룰은 2024년 12월부터
  엔진과 다른 [Semgrep Rules License v1.0](https://semgrep.dev/legal/rules-license/)
  을 씁니다 — 사내 비경쟁·비SaaS 용도로 한정됩니다. 사내 코드리뷰 용도라면
  해당되지만, OSI 승인 오픈소스 라이선스가 아니므로 심사에서 물어볼 수 있습니다.

```bash
git clone --depth 1 https://github.com/semgrep/semgrep-rules /tmp/semgrep-rules
```

```toml
[grounding]
semgrep_config = "/opt/semgrep-rules"
```

지정하지 않으면 semgrep 은 아예 실행되지 않습니다. 다른 분석기는 그대로 돕니다.

---

## 폐쇄망 반입

`tools/package.ps1` 이 만드는 CREX 번들에는 **CREX 소스와 Python 런타임, MCP 용
wheel 만** 들어갑니다. 위 분석기들은 **별도로 반입 신청**해야 합니다.

1. 인터넷이 되는 장비에서 설치 파일을 내려받습니다.
2. SHA-256 해시를 떠서 신청서에 적습니다.

   ```powershell
   Get-FileHash .\LLVM-22.1.8-win64.exe -Algorithm SHA256
   ```

3. 폐쇄망 반대편에서 같은 해시가 나오는지 대조한 뒤 설치합니다.
4. `python -m crex doctor` 로 `OK` 로 바뀌었는지 확인합니다.

Python 패키지(ruff·mypy·bandit)는 wheel 을 미리 받아 옮깁니다.

```bash
pip download ruff mypy bandit -d wheels/ \
    --platform win_amd64 --python-version 312 --only-binary=:all:
```

```bash
pip install --no-index --find-links wheels/ ruff mypy bandit
```

ruff 는 zip 하나를 풀어 PATH 에 두는 쪽이 더 간단합니다. pip 를 쓰지 않으므로
반입 목록이 짧아집니다.

> 버전은 이 문서를 쓴 2026년 8월 기준으로 LLVM 22.1.8, cppcheck 2.21.0,
> ruff 0.16.4 였습니다. 위 링크는 버전이 아니라 배포 페이지를 가리키므로 최신을
> 받으면 됩니다. CREX 는 특정 버전에 묶여 있지 않습니다 — 출력 형식만 맞으면
> 됩니다.

---

## 하나도 없으면 어떻게 되나

리뷰는 돕니다. 대신 이렇게 됩니다.

- 프롬프트의 "확인된 사실" 절이 비고, LLM 이 처음부터 결함을 찾는 역할로
  돌아갑니다. 근거 없는 지적이 늘어납니다.
- 결정론적 기각(라인 범위·변경 여부)과 교차 모델 검증은 그대로 동작하므로,
  환각 방어의 나머지 세 겹은 살아 있습니다.
- 리포트의 "정적분석 결과 0건"이 정상인지 도구가 없어서인지 헷갈립니다.
  실행 로그와 `doctor` 로 구분하세요.

우선순위를 하나만 꼽으라면 **쓰는 언어의 도구 하나**입니다. C++ 은 cppcheck,
Python 은 ruff 가 설치 비용 대비 효과가 가장 큽니다.

---

## 다음

- [설정](configuration.md#grounding--정적분석) — `[grounding]` 항목 전체
- [문제 해결](troubleshooting.md#정적분석이-안-돌아간다) — 설치했는데 안 도는 경우
- [반입](transfer.md) — CREX 번들 만들기·검증
