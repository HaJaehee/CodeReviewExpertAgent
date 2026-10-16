# 정적분석 도구

CREX는 LLM을 호출하기 **전에** 정적분석 도구를 먼저 실행합니다. 정적분석 결과를 프롬프트에 함께 넣어 모델의 역할을 바꾸기 위해서입니다 — 막연하게 "결함을 찾아라"라고 시키는 대신 **"정적분석 도구가 찾은 결과를 검증하고, 정적분석이 구조적으로 잡지 못하는 로직·설계·흐름 결함을 보완하라"**고 지시합니다.

이 과정이 환각을 억제하는 첫 번째 방어선입니다. 소형 모델이 근거 없이 추측하는 대신, 정적분석기가 확인한 사실을 바탕으로 판단하게 됩니다.

**정적분석 도구가 없어도 리뷰는 정상 동작합니다.** `PATH`에서 실행 파일을 찾지 못하면 해당 분석기만 조용히 건너뛰고 파이프라인은 계속 진행됩니다. 폐쇄망에서는 장비마다 설치된 도구가 다르므로 특정 분석기가 없다고 전체 리뷰가 멈추지 않도록 설계했습니다.

```
INFO    crex.ground: [cppcheck] 건너뜀 — cppcheck 를 PATH 에서 찾을 수 없습니다
```

다만 정적분석 도구가 없으면 모델이 참고할 사실 근거가 줄어들어 오탐이나 누락이 늘어날 수 있습니다. 주력으로 사용하는 언어의 분석기만이라도 설치하는 것을 권장합니다.

---

## 무엇이 필요한가

`python -m crex doctor`를 실행하면 현재 설치 상태를 한눈에 확인할 수 있습니다.

```
정적분석 도구
  OK  clang-tidy (clang-tidy)
  없음 cppcheck (cppcheck)
  OK  roslyn (dotnet)
  OK  ruff (ruff)
  없음 mypy (mypy)
  없음 bandit (bandit)
```

| 분석기 이름 | 대상 언어 | 실행 파일 | 기본 활성화 | 라이선스 |
|---|---|---|---|---|
| `clang-tidy` | C++ | `clang-tidy` | 예 | Apache-2.0 with LLVM-exception |
| `cppcheck` | C++ | `cppcheck` | 예 | **GPL-3.0** |
| `roslyn` | C# | `dotnet` | 예 | MIT (.NET SDK) |
| `ruff` | Python | `ruff` | 예 | MIT |
| `mypy` | Python | `mypy` | 예 | MIT |
| `bandit` | Python | `bandit` | 예 | Apache-2.0 |
| `roslynator` | C# | `roslynator` | 아니오 | Apache-2.0 |
| `semgrep` | 공통 | `semgrep` | 아니오 | LGPL-2.1 (룰팩 라이선스는 별도, 아래 참고) |

"기본 활성화"가 `예`인 도구들은 별도 설정 없이도 설치되어 있으면 자동으로 실행됩니다. `아니오`인 두 도구는 `grounding.analyzers`에 이름을 명시해야 켜집니다(단, `semgrep`은 `analyzers`를 비워 둔 채 `semgrep_config` 경로만 지정해도 자동으로 활성화됩니다). 자세한 설정 방법은 [설정의 `grounding` 절](configuration.md#grounding--정적분석)을 참고하십시오.

위 도구들은 모두 무료 오픈소스입니다. 상용 구독료나 사용자 수 제한, 사용 기한이 없습니다. 보안 반입 신청서를 작성할 때는 "프리웨어"라고 적기보다 위 표의 정확한 오픈소스 라이선스 명칭을 기재하는 것이 심사에 유리합니다.

---

## C++ — clang-tidy

LLVM 공식 설치 프로그램에 `bin\clang-tidy.exe`로 포함되어 있습니다.

- 다운로드: [releases.llvm.org](https://releases.llvm.org/download.html) 또는 [GitHub Releases](https://github.com/llvm/llvm-project/releases)
- Windows 설치 파일: `LLVM-<버전>-win64.exe` (전체 툴체인이 포함되어 있어 400MB 이상입니다)
- Linux: `apt install clang-tidy` / `dnf install clang-tools-extra`

> **Visual Studio가 이미 설치되어 있다면** Visual Studio Installer에서 **"Windows용 C++ Clang 도구"** 컴포넌트만 추가로 체크하면 `clang-tidy`가 함께 설치됩니다. 이미 반입 승인된 Visual Studio의 하위 구성요소이므로 별도 반입 신청이 필요 없을 수 있습니다. 다만 해당 실행 파일 위치(`...\VC\Tools\Llvm\x64\bin\clang-tidy.exe`)는 시스템 `PATH`에 자동으로 등록되지 않으므로, 해당 디렉터리를 `PATH`에 추가한 뒤 `python -m crex doctor`로 인식 여부를 확인하십시오.

### compile_commands.json 이 없으면 반쯤 눈을 감습니다

`clang-tidy`는 각 소스 파일의 컴파일 명령(헤더 경로 및 매크로 정의)을 알아야 정확하게 구문을 분석할 수 있습니다. `compile_commands.json`이 없으면 헤더 파일을 찾지 못해 분석 정확도가 크게 떨어집니다. 프로젝트 형식마다 생성 방법이 다르지만, CREX에서는 아래 명령 하나로 자동 생성할 수 있습니다.

```bash
python -m crex compiledb
```

워크스페이스에서 CMake 또는 MSBuild 프로젝트를 자동으로 찾아 `compile_commands.json`을 생성하고, 생성된 경로를 `crex.json`의 `compile_commands_dir`에 기록합니다. 다음 리뷰부터 즉시 반영됩니다.

```
D:\work\repo\.crex\compiledb\compile_commands.json
  대상   : msbuild: D:\work\repo\App.sln
  엔트리 : 412개
  기록   : D:\work\repo\crex.json
```

산출물은 `<저장소>/.crex/compiledb/`에 저장되며, 해당 폴더 안에 `.gitignore`(`*`)가 함께 생성되므로 대상 저장소의 `.gitignore`를 건드리지 않습니다.

터미널 대신 웹 화면에서도 동일하게 생성할 수 있습니다 — [관제 화면](visualizer.md#c-compile_commandsjson-만들기) 왼쪽 패널에서 워크스페이스를 확인한 뒤 실행 버튼을 누르면 됩니다.

자주 사용하는 옵션은 다음과 같습니다.

| 옵션 | 사용 시점 |
|---|---|
| `--configuration Release` | `Debug` 대신 `Release` 등 다른 구성으로 생성할 때 |
| `--platform Win32` | 32비트 타깃 빌드일 때 |
| `--project src\App.vcxproj` | `.sln` 파일이 여러 개이거나 특정 프로젝트만 지정할 때 |
| `--msbuild-arg /m` | MSBuild 병렬 빌드 활성화 (아래 주의사항 참고) |
| `--no-save` | 파일만 생성하고 `crex.json` 자동 기록은 생략할 때 |

현재 인식 상태는 `python -m crex doctor`에서 확인할 수 있습니다.

```
compile_commands.json (C++ clang-tidy)
  OK  D:\work\repo\.crex\compiledb\compile_commands.json (엔트리 412개)
```

#### 내부 동작 방식

- **CMake 프로젝트 (`CMakeLists.txt`)**: Ninja 제너레이터로 **구성(Configure)만** 수행합니다. 실제 컴파일은 하지 않으므로 수 초 내에 끝납니다. Visual Studio 제너레이터(`.sln` 생성)는 `CMAKE_EXPORT_COMPILE_COMMANDS` 옵션을 지원하지 않기 때문에, 기존 프로젝트가 VS 제너레이터로 구성되어 있더라도 별도 디렉터리에서 Ninja로 구성하여 추출합니다.
- **MSBuild 프로젝트 (`.sln` / `.vcxproj`)**: MSBuild 커스텀 로거를 부착하여 빌드하면서 실제 `cl.exe` 호출 명령줄을 수집합니다. 속성 시트(Property Sheet), 전처리기 매크로, 상속된 include 경로가 모두 정확히 반영됩니다. 로거 DLL은 번들에 기본 포함되어 있습니다([`tools/msbuild-compiledb/`](../../tools/msbuild-compiledb/README.md), MIT 라이선스).

MSBuild 프로젝트를 다룰 때 유의할 점 두 가지:

1. **전체 리빌드(`Rebuild`)를 수행합니다.** 컴파일 이벤트를 관찰해 기록하는 방식이므로 컴파일되지 않은 파일은 DB에서 누락됩니다. 증분 빌드로 추출하면 일부 파일만 담긴 불완전한 DB가 만들어지므로 전체 리빌드가 필수입니다. 대형 솔루션은 시간이 걸리지만, 소스 파일이나 헤더 경로 구성이 바뀔 때만 가끔 실행하면 됩니다.
2. **병렬 빌드(`/m`)를 기본으로 켜지 않습니다.** MSBuild 다중 프로세스 빌드에서는 워커 노드의 컴파일 이벤트가 중앙 로거로 전달되는 과정에서 일부 누락되는 경우가 있습니다. 빌드 속도가 급하다면 `--msbuild-arg /m`을 추가하되, 생성된 **엔트리 수가 줄어들지 않았는지 반드시 확인하십시오.**

빌드 상세 로그는 `.crex/compiledb/msbuild.log`에 저장됩니다.

#### 수동으로 직접 생성하려면

CI 파이프라인 등에서 직접 생성하고 싶다면 아래 방식을 사용할 수 있습니다. CMake는 명령 한 줄이면 됩니다.

```bash
cmake -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build
```

Visual Studio 2022의 "폴더 열기" CMake 프로젝트를 사용 중이라면 `CMakePresets.json`에 캐시 변수를 추가해 두면 구성할 때마다 자동으로 생성됩니다.

```json
{
  "name": "x64-debug",
  "generator": "Ninja",
  "binaryDir": "${sourceDir}/out/build/${presetName}",
  "cacheVariables": { "CMAKE_EXPORT_COMPILE_COMMANDS": "ON" }
}
```

생성된 파일이 들어 있는 **디렉터리 경로**를 `crex.json`에 지정하면 됩니다(상대 경로는 리뷰 대상 저장소 루트 기준).

```json
{
  "grounding": { "compile_commands_dir": "build" }
}
```

#### MSVC 플래그와 clang-tidy 호환성

컴파일러가 `cl.exe`로 기록되어 있으면 Clang 툴링이 자동으로 MSVC 호환 모드로 동작합니다. 다만 `/ZI`, `/Gm`, C++/CLI, 일부 미리 컴파일된 헤더(PCH) 옵션에서는 `clang-tidy`가 파싱 오류를 낼 수 있습니다. 이 경우 저장소 루트의 `.clang-tidy` 파일에 `ExtraArgs` 또는 `ExtraArgsBefore` 설정을 추가해 문제 플래그를 마스킹하십시오.

만약 컴파일 DB 구축이 너무 부담스러운 환경이라면 `clang-tidy` 대신 `cppcheck`만 사용하는 것도 좋은 선택입니다 — `cppcheck`는 컴파일 DB 없이도 동작하며 오탐률이 낮습니다.

### 어떤 체크를 활성화하나

`clang_tidy_checks` 설정을 비워 두면 대상 저장소의 `.clang-tidy` 파일을 그대로 따르고, `.clang-tidy` 파일도 없으면 실제 결함 탐지 위주의 기본 체크셋을 사용합니다.

```
-*,bugprone-*,cert-*,clang-analyzer-*,concurrency-*,misc-*,performance-*
```

`readability-*` 같은 코딩 스타일 체크는 활성화하지 않습니다. 스타일 지적은 오탐이 많을 뿐 아니라 LLM 코드 리뷰의 목적과도 맞지 않기 때문입니다.

---

## C++ — cppcheck

- 다운로드: [github.com/cppcheck-opensource/cppcheck](https://github.com/cppcheck-opensource/cppcheck/releases) ([SourceForge 미러](https://sourceforge.net/projects/cppcheck/))
- Windows 설치 파일: `cppcheck-<버전>-x64-Setup.msi` (약 20MB)
- Linux: `apt install cppcheck`

### GPL-3.0 라이선스 유의사항

기본 분석기 중 유일하게 카피레프트 라이선스(GPL-3.0)를 사용하므로 반입 시 아래 사항을 참고하십시오.

- CREX는 `cppcheck`를 **별도 외부 프로세스로 실행**하고 표준 출력만 읽어 들입니다(`crex/ground.py`의 `subprocess.run`). 라이브러리 형태로 링크하지 않으므로 **CREX 자체나 리뷰 대상 사내 소스 코드에 GPL 라이선스가 전파되지 않습니다.**
- 사내에 `cppcheck` 설치 파일을 배포할 때 해당 버전의 소스 코드 압축본(tarball)을 함께 반입해 보관해 두면 라이선스 준수 요건을 완벽히 충족할 수 있습니다.
-참고로 **Cppcheck Premium**([cppcheck.com](https://www.cppcheck.com/))은 별도의 상용 유료 버전이며, CREX가 사용하는 것은 무료 오픈소스 버전(Cppcheck CE)입니다.

---

## C# — roslyn / roslynator

`roslyn` 분석기는 **별도 도구를 설치할 필요가 없습니다.** `dotnet build`를 실행해 컴파일러와 내장 Roslyn 분석기가 출력하는 경고를 수집하므로 .NET SDK만 설치되어 있으면 됩니다.

- .NET SDK: [dotnet.microsoft.com/download](https://dotnet.microsoft.com/download) (MIT 라이선스)

C# Roslyn 분석기는 컴파일 과정의 일부로 동작하므로 단일 파일만 떼어내 분석할 수 없습니다. 따라서 CREX는 다음 명령을 실행합니다.

```
dotnet build --nologo --no-incremental -v normal <대상>
```

빌드 출력에서 `Service.cs(42,13): warning CA2000: ...` 형식의 경고 줄을 추출해 프롬프트에 주입합니다. 프로젝트에 이미 설정된 `.editorconfig`와 `Directory.Build.props` 규칙이 그대로 적용되므로 **사내 코딩 표준과 자동으로 일치합니다.**

### 대상은 알아서 정합니다

`dotnet_project`를 일일이 지정하지 않아도 다음 순서로 빌드 대상을 자동 탐색합니다.

1. `grounding.dotnet_project`가 명시되어 있으면 해당 경로를 사용합니다.
2. **변경된 파일이 속한 `.csproj`** — 파일 위치에서 저장소 루트까지 올라가며 가장 가까운 프로젝트를 찾습니다. 솔루션 전체가 아니라 변경된 프로젝트 하나만 빌드하므로 가장 빠릅니다.
3. 변경된 파일이 여러 프로젝트에 걸쳐 있으면 저장소의 `.sln` 파일을 찾습니다.
4. 후보가 여러 개여서 하나로 특정할 수 없으면 임의로 고르지 않고 **건너뛴 뒤 로그로 알립니다.**

```
INFO    crex.ground: [roslyn] 건너뜀 — 빌드할 .csproj/.sln 을 정하지 못했습니다
                     — grounding.dotnet_project 를 지정하십시오
```

현재 어떤 빌드 대상이 선택되는지는 `python -m crex doctor`에서 확인할 수 있습니다.

```
빌드 대상 (C# roslyn)
  OK  D:\work\repo\App.sln (자동 탐색)
```

### 매번 다시 컴파일합니다 (`--no-incremental`)

Roslyn 경고는 실제로 컴파일이 수행될 때만 출력됩니다. 개발자가 로컬에서 방금 빌드한 직후에 증분 빌드를 실행하면 컴파일할 대상이 없어 경고가 0건으로 나오고, 파이프라인은 "경고가 없는 깨끗한 코드"로 오인하게 됩니다. 이를 막기 위해 `--no-incremental` 옵션을 사용합니다.

만약 솔루션 규모가 매우 커서 빌드 시간이 부담스럽다면 `grounding.timeout`을 늘리거나 `grounding.analyzers`에서 `roslyn`을 제외하십시오.

### 빌드 실패와 경고 0건의 구분

폐쇄망에서 자주 발생하는 NuGet 패키지 복원 실패(`error NU1101` 등)는 파일 줄·열 번호가 없어 일반 경고 파서에 잡히지 않습니다. CREX는 빌드 종료 코드가 0이 아니면서 추출된 경고가 없으면 이를 "정상 0건"이 아니라 **분석기 실행 실패(건너뜀)**로 명확히 기록합니다. 반면 일반 C# 컴파일 오류(`error CS...`)는 줄 번호가 있으므로 정적분석 지적에 그대로 포함됩니다.

`roslynator`는 별도 설치가 필요하고 기본 Roslyn 분석기와 겹치는 규칙이 많아 기본 목록에서는 제외되어 있습니다. 사용하려면 도구를 설치한 뒤 `grounding.analyzers`에 이름을 추가하십시오.

```bash
dotnet tool install -g roslynator.dotnet.cli
```

```json
{
  "grounding": {
    "analyzers": ["clang-tidy", "cppcheck", "roslyn", "ruff", "mypy", "bandit", "roslynator"]
  }
}
```

> **주의:** `analyzers` 배열을 비워 두면 기본 6종이 모두 실행되지만, **하나라도 이름을 적으면 적은 분석기만 실행됩니다.** `roslynator`만 적으면 나머지 기본 분석기가 꺼지므로 사용할 분석기 이름을 모두 나열하십시오.

---

## Python — ruff / mypy / bandit

`mypy`는 CREX 기본 패키징(`requirements.txt`, `package.ps1`)에 포함되어 있어 오프라인 번들 배포 시 `pylibs/`에 함께 담기며, 별도 설치 없이 즉시 동작합니다.

`ruff`와 `bandit`도 `pip`로 설치하거나 wheel 파일로 반입할 수 있습니다.

```bash
pip install ruff mypy bandit
```

특히 `ruff`는 단일 실행 파일(`ruff.exe`)로 배포되므로 `pip` 없이 실행 파일 하나만 반입해 `PATH`에 두는 방식이 폐쇄망에서 가장 간편합니다.

- 다운로드: [github.com/astral-sh/ruff/releases](https://github.com/astral-sh/ruff/releases) → `ruff-x86_64-pc-windows-msvc.zip` (압축 해제 시 `ruff.exe` 단일 파일)

세 도구 모두 퍼미시브 오픈소스 라이선스(ruff·mypy: MIT, bandit: Apache-2.0)입니다.

---

## semgrep (선택)

보안 취약점 패턴 검사에 강점이 있는 도구입니다. 폐쇄망에서 사용하려면 다음 사항을 준비해야 합니다.

- 분석 엔진(Semgrep CE)은 LGPL-2.1 라이선스입니다.
- **`semgrep_config = "auto"`는 폐쇄망에서 동작하지 않습니다.** 외부 서버에서 룰을 내려받으려 시도하기 때문입니다. 반드시 룰팩을 미리 반입한 뒤 로컬 경로를 지정해야 합니다.
- **룰팩 라이선스를 별도로 확인하십시오.** 공식 `semgrep-rules` 저장소는 2024년 12월부터 [Semgrep Rules License v1.0](https://semgrep.dev/legal/rules-license/)을 적용하며, 사내 비경쟁·비SaaS 용도로는 무료 사용이 가능하지만 OSI 공인 오픈소스 라이선스는 아니므로 보안·법무 심사에 참고하십시오.

```bash
git clone --depth 1 https://github.com/semgrep/semgrep-rules /tmp/semgrep-rules
```

```json
{
  "grounding": { "semgrep_config": "/opt/semgrep-rules" }
}
```

---

## 폐쇄망 반입 절차

루트의 `package.ps1`이 생성하는 CREX 번들에는 **CREX 소스 코드, Python 임베더블 런타임, MCP 연동용 wheel(`mypy` 포함)**이 담깁니다. `clang-tidy`, `cppcheck`, `ruff` 등 외부 바이너리 분석기는 별도로 반입합니다.

1. 인터넷이 연결된 장비에서 설치 파일을 내려받습니다.
2. SHA-256 해시를 계산해 반입 신청서에 기재합니다.

   ```powershell
   Get-FileHash .\LLVM-22.1.8-win64.exe -Algorithm SHA256
   ```

3. 폐쇄망 내부 장비로 파일을 반입한 뒤 동일한 해시값이 나오는지 대조하고 설치합니다.
4. `python -m crex doctor`를 실행해 해당 도구가 `OK`로 표시되는지 확인합니다.

Python 기반 분석기(`ruff`, `mypy`, `bandit`)를 wheel로 반입할 때는 다음과 같이 내려받아 설치합니다.

```bash
pip download ruff mypy bandit -d wheels/ \
    --platform win_amd64 --python-version 312 --only-binary=:all:
```

```bash
pip install --no-index --find-links wheels/ ruff mypy bandit
```

---

## 하나도 없으면 어떻게 되나

정적분석 도구가 하나도 없어도 리뷰 파이프라인은 정상 동작합니다.

- 대신 Tree-sitter AST 심볼 계약, 변경 요약 지도(Change Map), 호출 그래프 실행 경로 슬라이스(Call-Graph Flow) 및 LLM 자체 분석만으로 결함을 탐지합니다.
- 결정론적 라인 검증과 교차 모델 재판정(`ReviewFilter`)은 그대로 작동하므로 라인 번호 날조 등 구조적 환각 방어는 그대로 유지됩니다.

도입 우선순위를 하나만 꼽는다면 **주력 언어의 대표 도구 하나**부터 설치하십시오. C++은 `cppcheck`, Python은 `ruff`가 설치 비용 대비 효과가 가장 뛰어납니다.

---

## 다음

- [설정](configuration.md#grounding--정적분석) — `[grounding]` 설정 항목 상세 안내
- [문제 해결](troubleshooting.md#정적분석이-안-돌아간다) — 도구를 설치했는데 실행되지 않을 때
- [반입](transfer.md) — CREX 오프라인 반입 번들 생성 및 검증
