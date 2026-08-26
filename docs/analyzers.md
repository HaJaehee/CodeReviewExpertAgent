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

clang-tidy 는 컴파일 명령을 알아야 헤더를 찾습니다. 없으면 절반쯤 눈을 감고
분석합니다. 만드는 방법은 프로젝트 형식마다 다른데, 그걸 외우고 있을 필요는 없습니다.

```bash
python -m crex compiledb
```

워크스페이스에서 CMake/MSBuild 프로젝트를 찾아 만들고, 나온 자리를 `crex.toml` 의
`compile_commands_dir` 에 적는 것까지 합니다. 다음 리뷰부터 바로 적용됩니다.

```
D:\work\repo\.crex\compiledb\compile_commands.json
  대상   : msbuild: D:\work\repo\App.sln
  엔트리 : 412개
  기록   : D:\work\repo\crex.toml
```

산출물은 `<저장소>/.crex/compiledb/` 에 들어갑니다. 그 폴더는 스스로를 git 에서
제외하므로(`.crex/.gitignore`) 저장소의 `.gitignore` 를 고칠 필요가 없습니다.

자주 쓰는 옵션은 이 정도입니다.

| 옵션 | 언제 |
|---|---|
| `--configuration Release` | Debug 말고 다른 구성으로 뽑을 때 |
| `--platform Win32` | 32비트 빌드일 때 |
| `--project src\App.vcxproj` | .sln 이 여러 개거나 일부만 필요할 때 |
| `--msbuild-arg /m` | 병렬 빌드 (아래 주의 참고) |
| `--no-save` | 만들기만 하고 설정은 직접 적을 때 |

현재 상태는 `python -m crex doctor` 에 나옵니다.

```
compile_commands.json (C++ clang-tidy)
  OK  D:\work\repo\.crex\compiledb\compile_commands.json (엔트리 412개)
```

#### 안에서 무슨 일이 일어나나

**CMake 프로젝트**(`CMakeLists.txt`)면 Ninja 로 **구성만** 합니다 — 컴파일은 하지
않으므로 몇 초면 끝납니다. `.sln` 을 만들어내는 Visual Studio 제너레이터는
`CMAKE_EXPORT_COMPILE_COMMANDS` 를 무시하기 때문에, 대상 저장소가 VS 제너레이터로
구성돼 있어도 여기서는 Ninja 로 따로 구성합니다.

**MSBuild 프로젝트**(`.sln`/`.vcxproj`)면 MSBuild 로거를 붙여 빌드하면서 실제
`cl.exe` 호출을 기록합니다. property sheet·매크로·상속된 include 경로가 전부
반영된 정확한 결과가 나옵니다. 로거는 빌드된 DLL 로 함께 담겨 있어 그대로
실행됩니다 ([`tools/msbuild-compiledb/`](../tools/msbuild-compiledb/README.md), MIT).

MSBuild 쪽에는 알아둘 것이 둘 있습니다.

- **전체 Rebuild 를 합니다.** 관찰 방식이라 컴파일되지 않은 파일은 기록되지
  않습니다. 증분 빌드로 뽑으면 일부만 담긴 DB 가 생기는데, 그건 없는 것보다
  나쁩니다 — 나머지 파일에서 clang-tidy 가 조용히 눈을 감습니다. 큰 솔루션이면
  시간이 걸리지만, 자주 할 일은 아닙니다(빌드 설정이 바뀔 때만).
- **`/m`(병렬)을 기본으로 켜지 않습니다.** 병렬 빌드는 이벤트를 노드에서 중앙
  로거로 전달하는데, 그 전달이 걸러지면 DB 가 조용히 반쪽이 됩니다. 속도가 급하면
  `--msbuild-arg /m` 으로 켜되, **엔트리 수가 줄지 않았는지 확인하세요.**

빌드 로그는 `.crex/compiledb/msbuild.log` 에 남습니다. 실패하면 거기부터 봅니다.

#### 손으로 만들고 싶다면

CI 에 넣거나 다른 방식으로 만들어야 한다면 직접 해도 됩니다. CMake 는 한 줄입니다.

```bash
cmake -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build
```

Visual Studio 2022 의 "폴더 열기" CMake 프로젝트라면 `CMakePresets.json` 에
캐시 변수를 넣어두면 구성할 때마다 자동으로 나옵니다.

```json
{
  "name": "x64-debug",
  "generator": "Ninja",
  "binaryDir": "${sourceDir}/out/build/${presetName}",
  "cacheVariables": { "CMAKE_EXPORT_COMPILE_COMMANDS": "ON" }
}
```

MSBuild 프로젝트를 직접 다루겠다면 선택지가 셋입니다. `crex compiledb` 가 쓰는
것은 첫 번째입니다.

- [0xabu/MsBuildCompileCommandsJson](https://github.com/0xabu/MsBuildCompileCommandsJson)
  — 로거를 붙여 빌드를 관찰합니다. 빌드된 DLL 이 번들에 들어 있습니다.
- [microsoft/msbuild-extractor-sample](https://github.com/microsoft/msbuild-extractor-sample)
  — MSBuild API 로 design-time 빌드만 돌려 뽑으므로 컴파일이 필요 없습니다.
  대신 .NET SDK 와 NuGet 복원이 필요해서 폐쇄망에서는 준비가 더 큽니다.
- [ms2cc](https://github.com/freddiehaddad/ms2cc) — `msbuild /v:detailed` 로그를
  변환합니다.

어느 쪽이든 만든 **디렉터리**를 설정에 적습니다. 상대 경로는 리뷰 대상 저장소
루트 기준입니다.

```toml
[grounding]
compile_commands_dir = "build"
```

#### MSVC 플래그와 clang-tidy

엔트리의 컴파일러가 `cl.exe` 면 clang 툴링이 알아서 CL 드라이버 모드로 붙습니다.
그래도 `/ZI`, `/Gm`, C++/CLI, 일부 PCH 옵션에서는 clang-tidy 가 오류를 냅니다.
`crex.toml` 에는 clang-tidy 추가 인자를 넣는 설정이 없으니, 이때는 저장소의
`.clang-tidy` 에 `ExtraArgs` / `ExtraArgsBefore` 로 넣으세요.

여기까지가 부담스럽다면 clang-tidy 를 포기하고 cppcheck 만 쓰는 것도 나쁘지
않습니다 — cppcheck 는 컴파일 DB 없이도 돌고, 오탐률이 매우 낮습니다.

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

C# 은 파일 단위 분석이 안 됩니다. Roslyn 분석기는 컴파일러 안에서 도는 것이라
프로젝트를 빌드해야 경고가 나옵니다. 그래서 CREX 는 이렇게 합니다.

```
dotnet build --nologo --no-incremental -v normal <대상>
```

그 출력에서 `Service.cs(42,13): warning CA2000: ...` 형태의 줄을 걷어 프롬프트에
넣습니다. 팀이 이미 쓰는 `.editorconfig` 와 `Directory.Build.props` 설정이 그대로
적용되므로, **CREX 쪽에 룰을 따로 관리하지 않아도 사내 표준과 자동으로 맞습니다.**

### 대상은 알아서 정합니다

`dotnet_project` 를 적지 않아도 됩니다. 순서는 이렇습니다.

1. `grounding.dotnet_project` 가 있으면 그것 (명시가 항상 이깁니다)
2. **바뀐 파일이 속한 `.csproj`** — 파일 자리에서 저장소 루트까지 올라가며 찾습니다.
   솔루션 전체가 아니라 그 프로젝트만 빌드하면 되니 제일 빠릅니다
3. 바뀐 파일이 여러 프로젝트에 걸쳐 있으면 저장소의 `.sln`
4. 그래도 하나로 좁혀지지 않으면 **건너뜁니다** — 몰래 하나를 고르지 않습니다

4번이 중요합니다. 프로젝트가 여럿인데 아무거나 고르면 나머지 프로젝트의 경고가
통째로 빠진 채 리뷰가 돌고, 그 결과는 "도구가 검사했는데 깨끗함" 과 구분되지
않습니다. 그럴 때는 이렇게 나옵니다.

```
INFO    crex.ground: [roslyn] 건너뜀 — 빌드할 .csproj/.sln 을 정하지 못했다
                     — grounding.dotnet_project 를 지정하라
```

지금 무엇이 잡히는지는 `python -m crex doctor` 에 나옵니다.

```
빌드 대상 (C# roslyn)
  OK  D:\work\repo\App.sln (자동 탐색)
```

### 매번 다시 컴파일합니다

`--no-incremental` 이 붙는 이유가 있습니다. Roslyn 경고는 **컴파일할 때만** 나옵니다.
개발자가 방금 Visual Studio 에서 빌드하고 커밋했다면, 그 상태로 `dotnet build` 를
불러봐야 다시 컴파일할 것이 없고 경고도 한 줄도 안 나옵니다. 그러면 "이 파일은
도구가 검사했고 깨끗하다" 는 잘못된 전제로 LLM 이 돕니다.

바뀐 파일이 속한 프로젝트 하나만 태우므로 솔루션 전체를 리빌드하는 것보다는
훨씬 낫지만, 그래도 리뷰 한 번에 빌드 한 번입니다. 큰 프로젝트라 감당이 안 되면
`grounding.timeout` 을 올리거나 `grounding.analyzers` 에서 `roslyn` 을 빼세요.

### 빌드가 실패하면 0건이 아니라 실패라고 합니다

폐쇄망에서 제일 흔한 것이 NuGet 복원 실패입니다. 그 오류에는 줄·열 번호가 없어
경고 파서에 걸리지 않으므로, 그대로 두면 "지적 0건" 으로 보입니다. 빌드가 실패했고
걷은 것도 없으면 실패로 보고합니다.

```
INFO    crex.ground: [roslyn] 건너뜀 — dotnet build 실패 (코드 1):
                     App.csproj : error NU1101: Unable to find package ...
```

컴파일 오류가 나온 경우는 다릅니다. 그건 실패가 아니라 결과라서 `error CS____` 로
그대로 지적에 들어갑니다.

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
