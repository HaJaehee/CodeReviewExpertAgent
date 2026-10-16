# 폐쇄망 반입

Python 임베더블 런타임과 서드파티 라이브러리를 소스 코드와 분리하여 오프라인 반입 번들을 생성합니다. `PyInstaller` 같은 단일 실행 파일 압축 도구는 사용하지 않습니다 — 소스 코드가 텍스트 형태 그대로 보존되어야 보안 반입 심사에서 코드를 직접 열람·검증하기 쉽고, 반입 이후에도 현장에서 룰(`taxonomy.toml`)과 설정을 즉시 수정할 수 있기 때문입니다.

```
인터넷 연결 장비                  반입              폐쇄망 내부 장비
─────────────────                ────              ────────────────
.\package.ps1      ──▶  crex-YYYYMMDD.zip       ──▶  동일 상위 폴더에 압축 해제
                        + crex-YYYYMMDD-deps.zip     .\tools\verify.ps1
                        + 각각의 .sha256             .\crex.cmd doctor
```

---

## 1. 번들 만들기 (인터넷 연결 장비)

저장소 루트에서 `package.ps1` 스크립트를 실행합니다.

```powershell
cd <저장소-루트>
.\package.ps1
```

실행이 완료되면 `dist\crex-<날짜>.zip`(앱 소스 번들)과 `dist\crex-<날짜>-deps.zip`(런타임 및 의존성 번들: `runtime`, `pylibs`, `wheels`), 그리고 각 파일의 `.sha256` 해시 파일이 생성됩니다.

### 패키징 옵션

```powershell
.\package.ps1 -PythonVersion 3.12.10   # 포함할 Python 버전 지정 (3.11 이상)
.\package.ps1 -SkipRuntime             # 대상 장비에 이미 Python 3.11+가 설치되어 있을 때
.\package.ps1 -SkipDeps                # MCP 서버(Zed 연동)를 사용하지 않을 때
.\package.ps1 -OutDir build            # 번들 출력 디렉터리 변경
```

`-SkipDeps` 옵션을 사용하면 앱 번들 하나만 생성되어 용량이 약 10MB 수준으로 줄어듭니다. 코어 기능(`review`, `scan`, `doctor`, 관제 화면, 전체 테스트)은 표준 라이브러리만 사용하므로 그대로 동작하며, `crex-mcp.cmd`만 제외됩니다. **보안 심사 대상을 최소화하고 싶다면 `-SkipDeps` 옵션을 권장합니다.**

### 번들 내부 구성

```
crex-20260817/
  runtime/                  Python 임베더블 런타임 (설치 불필요, 레지스트리·PATH 미변경)
  pylibs/                   fastmcp, GitPython, mypy 등을 미리 풀어 둔 디렉터리 (pip 실행 불필요)
  wheels/                   원본 wheel 파일 보관소 (사내 별도 Python 환경에 설치할 때 활용)
  crex/ docs/ rules/ tests/ tools/
  docs/user_manual/         사용 설명서 원본 (마크다운)
  docs/user_manual_html/    브라우저 열람용 오프라인 HTML 설명서 (index.html)
  tools/msbuild-compiledb/  MSBuild → compile_commands.json 추출 로거 (C# DLL, MIT)
  crex.cmd                  CLI 실행 런처
  crex-mcp.cmd              MCP 서버 실행 런처 (Zed 연동용)
  run_crex_viz.ps1          관제 화면(웹 UI) 실행 스크립트 (PowerShell)
  crex-viz.cmd              관제 화면(웹 UI) 실행 런처 (cmd)
  테스트.cmd                 반입 무결성 및 전체 테스트 실행기
  LICENSE.md                서드파티 라이선스 고지문 (certifi MPL-2.0 전문 포함)
  MANIFEST.txt              번들 내 전체 파일 SHA-256 해시 목록
```

---

## 2. 보안 반입 신청

생성된 `.zip` 파일과 `.sha256` 파일을 함께 제출합니다. 보안 심사에서 자주 묻는 항목과 답변은 다음과 같습니다.

| 심사 질의 항목 | 답변 내용 |
|---|---|
| 바이너리 실행 파일이 포함되어 있나 | 공식 Python 임베더블(`runtime\python.exe`)과 MSBuild 로거 DLL(`tools\msbuild-compiledb\CompileCommandsJson.dll`, 7,680바이트)뿐이며, CREX 본체는 전부 순수 파이썬 텍스트 소스입니다. |
| 반입 후 설치 스크립트를 실행하나 | 아니오. 압축만 풀면 즉시 동작하며, 레지스트리 등록이나 `pip install`도 실행하지 않습니다. |
| 외부 네트워크로 통신하나 | 아니오. `crex.json`에 설정된 사내 vLLM 서버 주소로만 HTTP 요청을 보냅니다. |
| 수신 네트워크 포트를 여나 | 기본 CLI와 MCP(`stdio`)는 네트워크 포트를 전혀 열지 않습니다 (`crex.viz` 실행 시에만 로컬 루프백 `127.0.0.1:18765` 사용). |
| 서드파티 라이브러리는 무엇인가 | `requirements.txt`에 명시된 패키지들과 그 의존성이며, `wheels\` 폴더에 원본 `.whl` 파일이 그대로 보관되어 있습니다. |
| 포함된 C# DLL의 용도는 무엇인가 | `tools\msbuild-compiledb\CompileCommandsJson.dll`은 C++ 프로젝트 빌드 시 컴파일 명령을 기록하는 오픈소스(MIT) MSBuild 로거입니다. 원본 저장소·커밋·빌드 방법·해시가 [`tools/msbuild-compiledb/README.md`](../../tools/msbuild-compiledb/README.md)에 명시되어 있어 동일하게 재현·대조할 수 있습니다. |

> **참고:** FastMCP 라이브러리는 기본값으로 기동 시 `pypi.org`에 최신 버전을 확인하려는 동작이 있으나, CREX는 모듈 임포트 전에 코드 수준에서 `FASTMCP_CHECK_FOR_UPDATES=off`를 강제 설정하여 외부 접속 시도를 원천 차단합니다.

---

## 3. 반입 후 무결성 검증 (폐쇄망 내부)

앱 번들(`crex-<날짜>.zip`)과 의존성 번들(`crex-<날짜>-deps.zip`)을 동일한 상위 폴더에 압축 해제하여 하나의 `crex-<날짜>` 폴더로 합친 뒤, 아래 검증 스크립트를 실행합니다.

```powershell
cd crex-20260817
.\tools\verify.ps1
```

검증 스크립트는 다음 세 단계를 자동으로 확인합니다.

```
==> 무결성 확인
    OK   파일 4415 개 해시 일치

==> Python 확인
    번들 런타임 사용
    OK   Python 3.12.10
    OK   crex import 성공

==> 테스트 (LLM·네트워크·pip 불필요)
    전체 통과 (14개 모듈)
    OK   전체 통과
```

파일 해시 대조는 수천 개의 파일을 검사하므로 1~2분 정도 소요됩니다. 빠른 재확인이 필요할 때는 `-SkipManifest` 옵션을 줄 수 있으나, 최초 반입 직후에는 반드시 전체 해시 검증을 실행하십시오.

---

## 4. 설정 및 첫 실행

```powershell
copy crex.example.json crex.json
notepad crex.json       # 사내 vLLM 주소와 모델명을 입력합니다
.\crex.cmd doctor
```

`crex.cmd`는 번들 내부의 독립 Python 런타임(`runtime\python.exe`)을 자동으로 사용합니다. 시스템 `PATH`를 수정할 필요가 없으며, 장비에 다른 버전의 Python이 설치되어 있어도 충돌하지 않습니다.

```powershell
.\crex.cmd review --staged
.\crex.cmd review --from main --out reports\
```

번들은 리뷰 대상 저장소 외부의 고정된 경로(예: `D:\tools\crex-20260817`)에 한 벌만 두고 `--workspace`로 대상 저장소를 지정해 사용하십시오.

```powershell
D:\tools\crex-20260817\crex.cmd review --workspace D:\work\myrepo --staged
```

### 서드파티 라이선스 고지

의존성 번들(`pylibs/`)에 포함되는 서드파티 패키지 중 약한 카피레프트 라이선스는 `certifi`(**MPL-2.0**) 하나뿐이며, 나머지는 모두 퍼미시브 라이선스(MIT, BSD, Apache-2.0 등)입니다. CREX는 `certifi`를 수정 없이 원본 그대로 포함하며, 루트의 `LICENSE.md`에 MPL-2.0 라이선스 전문을 수록하여 고지 의무를 준수합니다.

### Zed 에디터 연동

Zed의 `settings.json`에서 `command`에 번들 내 `crex-mcp.cmd`의 절대 경로를 지정합니다.

```json
{
  "context_servers": {
    "crex": {
      "command": "D:\\tools\\crex-20260817\\crex-mcp.cmd",
      "env": {
        "CREX_WORKSPACE": "D:\\work\\myrepo",
        "CREX_CONFIG": "D:\\work\\myrepo\\crex.json",
        "CREX_REPORTS": "D:\\work\\myrepo\\reports"
      }
    }
  }
}
```

`crex-mcp.cmd`가 번들 내 Python과 `pylibs`를 자동으로 연결하므로 가상환경 경로 문제없이 즉시 기동됩니다. 리뷰 대상 저장소 루트에 [`AGENTS.md`](../../AGENTS.md)를 복사해 두는 것도 잊지 마십시오 ([Zed 연동 문서](operations.md#zed-연동-mcp) 참고).

---

## 5. 소스 코드만 업데이트할 때 (갱신 반입)

CREX의 파이썬 코드나 룰(`taxonomy.toml`), 문서만 변경된 경우에는 무거운 런타임과 의존성 번들을 다시 반입할 필요가 없습니다.

```powershell
.\package.ps1 -SkipRuntime -SkipDeps
```

수 MB 크기의 가벼운 앱 번들 `.zip`만 생성됩니다. 폐쇄망 내부의 기존 번들 폴더 위에 압축을 풀어 `crex\`, `rules\`, `docs\`, `tests\`만 덮어쓴 뒤 `.\테스트.cmd`를 실행해 정상 동작을 확인하면 됩니다.

---

## 반입 시 문제 해결

### `.ps1` 스크립트 실행 시 파싱 오류가 발생합니다

```
Missing argument in parameter list.
+     Write-Step "?쒕뱶?뚰떚 ?대젮諛쏄린"
```

**Windows PowerShell 5.1은 UTF-8 BOM이 없는 `.ps1` 파일을 한국어 Windows 기본 인코딩(CP949)으로 읽습니다.** 한글이 포함된 PowerShell 스크립트는 반드시 **UTF-8 with BOM**으로 저장되어야 합니다.

저장소의 모든 `.ps1` 파일에는 기본적으로 BOM이 적용되어 있으며, `python tests/run_all.py`의 `test_powershell_scripts_carry_a_bom` 테스트가 이를 자동으로 검증합니다.

```powershell
# 파일 앞 3바이트(EF BB BF) 확인
Get-Content package.ps1 -Encoding Byte -TotalCount 3
# 239, 187, 191이 출력되어야 정상입니다
```

### `crex import 실패`

임베더블 Python의 모듈 검색 경로(`sys.path`)는 `runtime\python*._pth` 파일이 결정합니다. `package.ps1`이 아래와 같이 상위 번들 루트(`..`)와 `..\pylibs`를 자동으로 설정해 두므로, 이 파일을 임의로 수정하지 마십시오.

```
python312.zip
.
..
..\pylibs
..\pylibs\win32
..\pylibs\win32\lib
import site
```

### `No module named 'pywintypes'`

Windows 환경에서 MCP `stdio` 통신에 사용되는 `pywin32`는 `win32\lib` 경로 등록과 `pywintypes*.dll` 복사가 필요합니다. `package.ps1`이 이 과정을 자동으로 처리하므로, 만약 해당 오류가 발생한다면 최신 `package.ps1`로 의존성 번들을 다시 생성하십시오.

### 번들 생성 시 `pip download`가 실패합니다

사내 프록시나 내부 PyPI 미러를 사용하는 환경이라면 실행 전에 `PIP_INDEX_URL`을 지정하십시오.

```powershell
$env:PIP_INDEX_URL = "https://사내미러주소/simple"
.\package.ps1
```

---

## 왜 PyInstaller 단일 실행 파일을 쓰지 않나

단일 `.exe` 파일은 겉보기에는 간편하지만 폐쇄망 운영에서는 오히려 큰 단점이 됩니다.

1. **보안 반입 심사가 까다롭습니다**: 수십 MB짜리 불투명한 바이너리 내부에 어떤 코드가 들어 있는지 심사 담당자가 직접 확인할 수 없습니다. 반면 파이썬 소스 코드는 텍스트 파일이므로 즉시 열람하고 대조할 수 있습니다.
2. **현장 룰 튜닝이 불가능합니다**: 단일 `.exe`로 묶어 버리면 `rules\taxonomy.toml`의 `counter` 문구 한 줄을 고치려 해도 외부 장비에서 다시 빌드해 재반입해야 합니다. 소스 번들 형태에서는 현장에서 `taxonomy.toml`을 바로 수정하고 `.\테스트.cmd`로 즉시 검증할 수 있습니다.
3. **장애 추적이 쉽습니다**: 오류 발생 시 스택트레이스에 찍힌 파이썬 파일과 줄 번호를 바로 열어 원인을 파악할 수 있습니다.
