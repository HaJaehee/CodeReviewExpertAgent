# 폐쇄망 반입

Python 런타임까지 담은 zip 하나를 만들어 들고 들어갑니다. PyInstaller 를 쓰지
않습니다 — 소스가 소스 그대로 들어가므로 반입 심사에서 사람이 읽고 확인할 수
있고, 그게 통과시키기 쉬운 형태입니다.

```
인터넷 되는 장비                 반입              폐쇄망
─────────────────                ────              ──────
tools\package.ps1  ──▶  crex-YYYYMMDD.zip  ──▶  압축 해제
                        + .sha256                tools\verify.ps1
                                                 crex.cmd doctor
```

---

## 1. 번들 만들기 (인터넷 되는 장비)

```powershell
cd <저장소>
.\tools\package.ps1
```

`dist\crex-<날짜>.zip` 과 `dist\crex-<날짜>.zip.sha256` 이 나옵니다. 약 54MB,
5~10분 걸립니다.

### 옵션

```powershell
.\tools\package.ps1 -PythonVersion 3.12.10   # 담을 Python 버전 (3.11 이상)
.\tools\package.ps1 -SkipRuntime             # 대상 장비에 Python 3.11+ 가 이미 있을 때
.\tools\package.ps1 -SkipDeps                # MCP(Zed 연동) 를 안 쓸 때
.\tools\package.ps1 -OutDir build            # 출력 위치 변경
```

`-SkipDeps` 를 쓰면 번들이 약 10MB 로 줄어듭니다. 코어(`review` / `scan` /
`doctor` / 테스트)는 표준 라이브러리만 쓰므로 그대로 동작하고, `crex-mcp.cmd`
만 못 씁니다. **보안 검토 대상을 최소화하고 싶으면 이쪽이 낫습니다** — fastmcp
하나가 wheel 70개를 끌고 옵니다.

### 번들 안에 뭐가 들어가나

```
crex-20260817/
  runtime/          Python 임베더블 — 설치 불필요, 레지스트리·PATH 안 건드림
  pylibs/           fastmcp, GitPython 등을 미리 풀어둔 것 (pip 실행 불필요)
  wheels/           원본 wheel — 사내 다른 Python 에 직접 설치할 때만
  crex/ docs/ rules/ tests/ tools/
  docs/user_manual/       설명서 원본 (마크다운)
  docs/user_manual_html/  같은 설명서를 브라우저로 — index.html 부터
  tools/msbuild-compiledb/   MSBuild → compile_commands.json 로거 (C# DLL, MIT)
  crex.cmd           리뷰 실행
  crex-mcp.cmd       MCP 서버 (Zed)
  run_viz.ps1        웹 UI 실행 (PowerShell)
  crex-viz.cmd       웹 UI 실행 (cmd)
  테스트.cmd         반입 무결성 확인
  LICENSE.md        서드파티 라이선스 고지 (certifi 의 MPL-2.0 전문)
  MANIFEST.txt      전 파일 SHA256
```

---

## 2. 반입 신청

zip 과 `.sha256` 을 **함께** 제출합니다. 심사에서 물어볼 만한 것들을 미리 정리해
두면 빠릅니다.

| 질문 | 답 |
|---|---|
| 실행 파일이 있나 | Python 임베더블(`runtime\python.exe`)과 MSBuild 로거 DLL(`tools\msbuild-compiledb\CompileCommandsJson.dll`, 7,680바이트). 나머지는 전부 텍스트 소스 |
| 설치 스크립트를 돌리나 | 아니오. 압축만 풀면 됩니다. `pip` 도 안 돌립니다 |
| 외부로 나가나 | 아니오. `crex.json` 에 적은 사내 vLLM 주소로만 HTTP 를 보냅니다 |
| 네트워크 포트를 여나 | 아니오. MCP 는 stdio 라 리스너가 생기지 않습니다 |
| 서드파티는 | `requirements.txt` 두 줄과 그 의존성. `wheels\` 에 원본이 그대로 있습니다 |
| C# DLL 은 뭔가 | `tools\msbuild-compiledb\CompileCommandsJson.dll` — MSBuild 로거입니다 (MIT). C++ 프로젝트의 컴파일 명령을 뽑는 데 씁니다. 상류 저장소·커밋·빌드 설정·SHA-256 이 [`tools/msbuild-compiledb/README.md`](../../tools/msbuild-compiledb/README.md) 에 적혀 있어 같은 설정으로 다시 빌드해 대조할 수 있습니다 |

번들은 자체 무결성 확인이 가능합니다 — `MANIFEST.txt` 에 파일별 SHA256 이
들어 있고 `tools\verify.ps1` 이 대조합니다.

> FastMCP 는 기동할 때 pypi.org 로 새 버전을 확인하러 나갑니다. CREX 는
> import 전에 이 기능을 꺼둡니다(`FASTMCP_CHECK_FOR_UPDATES=off`). 설정으로
> 미루지 않고 코드에서 못 박아 두었습니다.

---

## 3. 검증 (폐쇄망)

압축을 풀고 번들 안에서 실행합니다.

```powershell
cd crex-20260817
.\tools\verify.ps1
```

세 가지를 봅니다.

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

해시 대조는 파일이 많아 몇 분 걸립니다. 급하면 `-SkipManifest` 로 건너뛸 수
있지만, 반입 직후 한 번은 돌리는 게 맞습니다.

---

## 4. 설정과 첫 실행

```powershell
copy crex.example.json crex.json
notepad crex.json       # vLLM 주소와 모델명을 넣는다
.\crex.cmd doctor
```

`crex.cmd` 는 번들 안의 Python 을 씁니다. PATH 를 건드릴 필요가 없고, 장비에
다른 Python 이 있어도 섞이지 않습니다.

```powershell
.\crex.cmd review --staged
.\crex.cmd review --from main --out reports\
```

번들에는 CREX 소스와 Python 런타임만 들어갑니다. clang-tidy·cppcheck·ruff 같은
### 라이선스

번들에 함께 담기는 서드파티 중 카피레프트는 `certifi`(**MPL-2.0**) 하나뿐이고,
나머지는 전부 퍼미시브(MIT·BSD·Apache-2.0 등)입니다. CREX 는 `certifi` 를
**고치지 않고 그대로** 재배포하므로 지켜야 할 의무는 고지뿐이며, 루트의
`LICENSE.md` 에 MPL-2.0 전문을 실어 그 의무를 채웁니다. `certifi` 자신은 전문
대신 mozilla.org 링크만 담고 있는데 폐쇄망에서는 그 링크가 열리지 않습니다.

`-SkipDeps` 로 만든 번들에는 `pylibs/` 가 아예 없어서 카피레프트 구성요소가
하나도 들어가지 않습니다. 심사 대상을 줄이고 싶으면 이쪽을 쓰십시오.

정적분석 도구는 **별도로 반입 신청**해야 합니다 — 내려받는 곳과 라이선스는
[정적분석 도구](analyzers.md)에 정리해 두었습니다. 없어도 리뷰는 되므로 나중에
채워도 됩니다.

번들은 리뷰 대상 저장소 밖에 두고 씁니다. 저장소마다 번들을 복사하면 어느 것이
반입 심사를 통과한 사본인지 알 수 없게 되므로, 반입본은 한 벌만 두고 대상만
가리키세요. 번들 Python 은 자기 위치를 기준으로 `crex` 를 찾으므로 현재 디렉터리가
어디든 상관없습니다.

```powershell
D:\tools\crex-20260817\crex.cmd review --workspace D:\work\myrepo --staged
```

### Zed 연동

`settings.json` 의 `command` 에 번들의 `crex-mcp.cmd` 를 지정합니다.

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

`crex-mcp.cmd` 가 번들 Python 을 부르므로 가상환경 경로 문제가 생기지 않습니다.
인자를 그대로 넘기므로 HTTP 엔드포인트도 이 런처로 엽니다.

```powershell
.\crex-mcp.cmd --transport http --port 18766
```

인증이 없는 엔드포인트입니다. 반입 신청서에 포트를 적어야 하고, 루프백 밖으로
열려면 그만한 이유가 있어야 합니다 —
[운영](operations.md#streamable-http-엔드포인트) 참고.
`AGENTS.md` 를 리뷰 대상 저장소로 복사하는 것도 잊지 마세요 —
[Zed 연동](operations.md#zed-연동-mcp) 참고.

---

## 5. 갱신 반입

소스만 바뀌었다면 런타임과 서드파티를 다시 담을 필요가 없습니다.

```powershell
.\tools\package.ps1 -SkipRuntime -SkipDeps
```

몇 MB 짜리 zip 이 나옵니다. 폐쇄망에서 기존 번들 위에 `crex\`, `rules\`,
`docs\`, `tests\` 만 덮어쓰면 됩니다. `runtime\` 과 `pylibs\` 는
그대로 둡니다.

덮어쓴 뒤 반드시 다시 확인하세요.

```powershell
.\테스트.cmd
```

---

## 문제 해결

### `.ps1` 파일이 파싱 오류를 낸다

```
Missing argument in parameter list.
+     Write-Step "?쒕뱶?뚰떚 ?대젮諛쏄린"
```

**PowerShell 5.1 은 BOM 없는 `.ps1` 을 시스템 코드페이지(한국어 Windows 는
cp949)로 읽습니다.** 한글이 들어간 스크립트는 **UTF-8 BOM 으로 저장**해야 합니다.

저장소의 스크립트에는 BOM 이 들어 있습니다. 편집기로 고친 뒤 이 오류가 나면
BOM 이 날아간 것입니다. `python tests/run_all.py` 가 이것도 확인하므로, 반입 전에
한 번 돌려 두면 여기서 걸립니다.

```powershell
# 확인
Get-Content tools\package.ps1 -Encoding Byte -TotalCount 3
# 239 187 191 이 나와야 정상 (EF BB BF)
```

### `crex import 실패`

임베더블 Python 의 `sys.path` 는 `runtime\python*._pth` 가 결정합니다.
번들 루트(`..`)가 없으면 `crex` 를 못 찾습니다.

```
python312.zip
.
..
..\pylibs
..\pylibs\win32
..\pylibs\win32\lib
import site
```

`package.ps1` 이 이렇게 써 둡니다. 손으로 고쳤다면 되돌리세요.

### `No module named 'pywintypes'`

MCP SDK 가 Windows stdio 를 다룰 때 `pywin32` 를 씁니다. `pip install --target`
로 넣으면 두 가지가 어긋납니다 — `pywin32.pth` 가 실행되지 않아 `win32\lib` 이
경로에 없고, `pywintypes312.dll` 이 DLL 검색 경로 밖입니다.

`package.ps1` 이 둘 다 처리합니다(`._pth` 에 경로 추가, DLL 을 `runtime\` 으로
복사). 이 오류가 나면 번들이 옛 스크립트로 만들어진 것이니 다시 만드세요.

### `pip download` 가 실패한다 (번들 만들 때)

사내 프록시나 미러를 쓰는 환경이면 `pip` 설정이 필요합니다.

```powershell
$env:PIP_INDEX_URL = "https://사내미러/simple"
.\tools\package.ps1
```

### 테스트가 실패한다

파일이 덜 복사된 경우가 대부분입니다. `verify.ps1` 의 무결성 확인이 먼저
알려줍니다. `rules\taxonomy.toml` 이 빠지면 여러 모듈이 한꺼번에 터집니다.

### Python 3.10 이하만 있다

룰 택소노미를 읽는 `tomllib` 이 3.11 부터 표준이라 동작하지 않습니다. `-SkipRuntime` 을 빼고
번들에 런타임을 담으면 장비의 Python 과 무관하게 돌아갑니다.

---

## 왜 PyInstaller 를 안 쓰나

단일 exe 는 편하지만 폐쇄망에서는 불리합니다.

**심사에서 설명하기 어렵습니다.** 바이너리 하나를 열어볼 수 없으니 "안에 뭐가
들었는지" 를 증명할 방법이 없습니다. 소스가 그대로 있으면 필요한 파일을 열어
보여주면 됩니다.

**수정이 안 됩니다.** 룰 하나 고치려고 매번 인터넷 되는 장비로 나가 다시
빌드해서 재반입해야 합니다. 소스 번들은 `rules\taxonomy.toml` 을 그 자리에서
고치고 `테스트.cmd` 로 확인하면 끝입니다. 룰 튜닝이 2주 주기로 도는 작업이라
이 차이가 큽니다.

**디버깅이 안 됩니다.** 폐쇄망에서 문제가 생기면 스택트레이스의 파일과 줄 번호를
그대로 열어볼 수 있어야 합니다.

임베더블 Python 은 그 대가로 폴더 하나가 늘어날 뿐입니다. 레지스트리도 PATH 도
건드리지 않아 장비에 흔적을 남기지 않습니다.
