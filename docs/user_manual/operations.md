# 운영

## 폐쇄망 반입

> Python 런타임과 의존성까지 한 번에 묶어 반입하는 절차는 [반입](transfer.md) 문서에 자세히 정리되어 있습니다(루트의 `package.ps1` 실행). 아래는 소스 파일을 직접 수동으로 옮길 때의 기준입니다.

코어 파이프라인은 외부 패키지를 전혀 사용하지 않으므로 소스 파일만 복사하면 즉시 동작합니다.

수동 반입 대상 목록:

```
crex/                     파이프라인 코어 패키지
rules/                    룰 택소노미 (taxonomy.toml)
tests/                    단위·통합 테스트 스위트
docs/                     사용 설명서 (마크다운 및 렌더링된 HTML)
tools/                    verify.ps1, render_docs.py, MSBuild 로거
package.ps1               오프라인 번들 패키징 스크립트
README.md
LICENSE.md                서드파티 라이선스 고지문
AGENTS.md                 Zed 에이전트 지시 파일 (리뷰 대상 저장소 루트로 복사)
crex.example.json         설정 파일 예시
requirements.txt          MCP 서버용 의존성 목록 (코어 CLI는 불필요)
requirements-optional.txt Tree-sitter 선택 의존성 목록
```

반입 직후 점검 순서:

```bash
python --version                # Python 3.11 이상인지 확인
python tests/run_all.py         # 반입 무결성 및 전 구간 통합 테스트 실행
cp crex.example.json crex.json  # 사내 vLLM 엔드포인트 및 모델명 설정
python -m crex doctor           # 엔드포인트 연결 및 분석기 상태 점검
```

`tests/run_all.py`는 실제 LLM 서버나 외부 패키지 설치 없이도 내장 모의 서버(`fake_vllm.py`)를 띄워 HTTP 통신부터 리포트 생성까지 전 구간을 검증합니다.

### 런타임 의존성

코어 기능(`review`, `scan`, `doctor`, 관제 화면, 테스트)은 Python 표준 라이브러리만 사용합니다. **Zed 에디터와 연동할 MCP 서버를 띄울 장비에만** `requirements.txt`를 설치하면 됩니다.

```bash
pip download -r requirements.txt -d wheels/ \
    --platform win_amd64 --python-version 312 --only-binary=:all:
```

```bash
pip install --no-index --find-links wheels/ -r requirements.txt
```

`GitPython`이 설치되어 있지 않아도 `crex/gitio.py`가 자동으로 `git` CLI 서브프로세스로 폴백하므로 정상 동작합니다.

### Tree-sitter (선택 사항)

설치되어 있지 않아도 중괄호 매칭과 들여쓰기 추적을 이용한 휴리스틱 폴백으로 함수 경계와 심볼 계약을 추출합니다. 다만 복잡한 C++ 매크로나 전처리기 조건부 컴파일(`#if`)이 많은 코드베이스에서는 Tree-sitter 파서를 설치했을 때 구문 분석 정확도가 더 높습니다.

```bash
# 인터넷 연결 장비에서 wheel 다운로드
pip download -r requirements-optional.txt -d wheels/ \
    --platform win_amd64 --python-version 312 --only-binary=:all:

# 폐쇄망 내부에서 설치
pip install --no-index --find-links wheels/ -r requirements-optional.txt
```

### Semgrep 룰팩

`semgrep_config = "auto"`는 외부 인터넷에서 룰을 내려받으려 시도하므로 폐쇄망에서 동작하지 않습니다. Semgrep을 사용하려면 룰팩을 미리 반입한 뒤 로컬 경로를 지정하십시오.

```bash
# 인터넷 연결 장비에서 다운로드
git clone --depth 1 https://github.com/semgrep/semgrep-rules /tmp/semgrep-rules
```

```json
{
  "grounding": { "semgrep_config": "/opt/semgrep-rules" }
}
```

---

## vLLM 기동

### Qwen3.6-27B (생성 모델)

```bash
vllm serve /models/Qwen3.6-27B \
  --served-model-name Qwen3.6-27B \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --port 8000
```

- `--served-model-name`은 `crex.json`의 `model` 값과 정확히 일치해야 합니다.
- `--max-model-len`은 `32768`이면 충분합니다. CREX는 입력 프롬프트를 8,192 토큰 예산 안에서 구성하므로 모델 컨텍스트를 불필요하게 크게 잡을 필요가 없으며, 적정 크기로 제한해야 KV 캐시 메모리를 아껴 동시 처리량(Batch Throughput)을 높일 수 있습니다.

### 구조화 출력 백엔드

CREX의 4단계 환각 방어선 중 핵심인 라인 번호·룰 ID `enum` 강제가 이 기능에 의존합니다.

최신 vLLM 빌드에서는 구조화 출력이 기본적으로 활성화되어 있습니다. 구버전(0.6~0.8대) vLLM을 사용하는 경우에는 `--guided-decoding-backend xgrammar` 플래그가 필요할 수 있습니다. 기동 후 반드시 `python -m crex doctor`를 실행해 **구조화 출력** 항목까지 `OK`가 나오는지 확인하십시오.

### Gemma 4 26B (검증 모델)

```bash
vllm serve /models/gemma-4-26b-it \
  --served-model-name gemma-4-26b-it \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.45 \
  --port 8001
```

검증 단계는 입력과 출력이 모두 짧아 메모리 부담이 작습니다. 만약 단일 GPU에 두 모델을 함께 올려야 한다면 `--gpu-memory-utilization`을 생성 모델 `0.55`, 검증 모델 `0.35` 수준으로 나누어 기동할 수 있습니다. GPU 메모리가 부족해 두 모델을 동시에 띄우기 어렵다면 `crex.json`에서 `verifier` 블록을 생략해 생성 모델을 재사용하십시오.

---

## 외부 통신 및 텔레메트리

CREX는 어떠한 외부 텔레메트리나 업데이트 서버로도 접속하지 않습니다. 오직 `crex.json`에 지정된 사내 vLLM 엔드포인트로만 HTTP 요청을 전송합니다.

---

## 일상 운영

### MR 리뷰

```bash
python -m crex review --from $(git merge-base main HEAD) --to HEAD --out reports/
```

`--from`에 단순히 `main`을 넣으면 브랜치 분기 이후 다른 개발자가 `main`에 병합한 커밋까지 비교 대상에 섞일 수 있으므로, `git merge-base`로 실제 분기점을 구해 지정하는 것이 정확합니다.

### 커밋 전 자가 점검

```bash
python -m crex review --staged
```

개발자가 커밋 직전에 스스로 실행하는 경로이며, 실무에서 활용도가 가장 높습니다. 셸 별칭(alias)으로 등록해 두면 더욱 편리합니다.

### 레거시 파일 감사

```bash
python -m crex scan src/legacy/*.cpp --out reports/legacy/
```

변경 라인 제약 없이 파일 전체를 검사하므로 diff 리뷰보다 오탐이 많습니다. 정기 자동화보다는 낯선 레거시 모듈을 집중 점검할 때 실행해 사람이 결과를 선별하는 용도로 활용하십시오.

---

## Zed 연동 (MCP)

Zed 에이전트 패널에서 `"변경사항 리뷰해줘"`라고 요청해 바로 실행할 수 있습니다.

### 설정 방법

`~/.config/zed/settings.json` (전역) 또는 프로젝트 루트의 `.zed/settings.json`에 다음과 같이 등록합니다.

```json
{
  "context_servers": {
    "crex": {
      "command": "python",
      "args": ["-m", "crex.mcp"],
      "env": {
        "CREX_WORKSPACE": "/work/myrepo",
        "CREX_CONFIG": "/work/myrepo/crex.json",
        "CREX_REPORTS": "/work/myrepo/reports"
      }
    }
  }
}
```

- **사전 준비**: MCP 서버를 구동할 Python 환경에 `pip install -r requirements.txt`가 설치되어 있어야 합니다(오프라인 번들을 사용할 때는 `crex-mcp.cmd`를 지정하면 별도 설치 없이 바로 동작합니다).
- **Python 실행 경로**: Zed가 셸 프로필의 `PATH`나 가상환경을 인식하지 못하는 경우가 흔하므로, `command`에는 가상환경 또는 번들 Python의 **절대 경로**(`/work/venv/bin/python`, `D:\\tools\\crex\\crex-mcp.cmd` 등)를 적는 것이 가장 확실합니다.

| 환경변수 | 설명 |
|---|---|
| `CREX_WORKSPACE` | 리뷰 대상 저장소 루트 경로 (`CREX_REPO` 별칭 지원) |
| `CREX_CONFIG` | 사용할 설정 파일 경로 (생략 시 `<워크스페이스>/crex.json` 우선 탐색) |
| `CREX_REPORTS` | 리포트 저장 디렉터리 (생략 시 `<워크스페이스>/reports`) |

**CREX 설치본은 한 벌만 두면 됩니다.** 프로젝트마다 `.zed/settings.json`에서 `CREX_WORKSPACE`만 해당 프로젝트 경로로 지정하십시오. 설정 변경 후에는 Zed를 재시작해야 반영됩니다.

### 기본 전송 방식은 `stdio`입니다

Zed를 비롯한 대부분의 에디터는 로컬 MCP 서버를 `stdio`(표준 입출력) 방식으로 실행합니다. 에디터가 자식 프로세스로 띄우고 에디터 종료 시 함께 종료되므로, 별도 데몬이나 열린 네트워크 포트가 전혀 생기지 않습니다. 보안성 면에서도 가장 깔끔하므로 로컬 환경에서는 `stdio` 방식을 권장합니다.

### Streamable HTTP 엔드포인트

서버 한 대에 설치된 CREX를 여러 개발자가 공유하거나 클라이언트가 다른 장비에 있는 경우에만 HTTP 모드를 사용합니다.

```bash
python -m crex.mcp --transport http
```

```
INFO    crex.mcp: Streamable HTTP — http://127.0.0.1:18766/mcp
```

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--transport` | 전송 방식 (`stdio` 또는 `http`) | `stdio` |
| `--host` | 바인드 주소 | `127.0.0.1` |
| `--port` | 포트 번호 | `18766` |
| `--path` | 엔드포인트 경로 | `/mcp` |

클라이언트에서는 URL로 연결합니다.

```json
{
  "context_servers": {
    "crex": {
      "source": "custom",
      "url": "http://127.0.0.1:18766/mcp"
    }
  }
}
```

HTTP 모드 운영 시 유의할 점 세 가지:

1. **별도 인증 기능이 없습니다**: 접속 가능한 사용자는 누구나 해당 저장소의 리뷰를 실행할 수 있으므로 루프백(`127.0.0.1`)에 바인딩하거나 접근 제어가 되는 내부망에서만 여십시오.
2. **원격 바인딩 시 `set_workspace`가 차단됩니다**: `--host 0.0.0.0` 등 루프백이 아닌 주소로 열면 보안상 원격 클라이언트의 임의 디렉터리 전환 요청(`set_workspace`)을 거부합니다.
3. **프로세스당 하나의 워크스페이스를 담당합니다**: 여러 저장소를 동시에 HTTP로 서비스하려면 저장소별로 포트를 나누어 별도 프로세스로 띄우십시오.

### 제공되는 MCP 도구 7종

| 도구 이름 | 동작 내용 |
|---|---|
| `review_staged` | 스테이징(`git add`)된 변경 리뷰 (가장 자주 사용) |
| `review_working_tree` | 커밋되지 않은 작업 트리 전체 변경 리뷰 |
| `review_diff` | 두 브랜치·커밋 간 비교 리뷰 (`merge-base` 자동 적용) |
| `review_file` | 단일 파일 전체 감사 |
| `review_directory` | 디렉터리 전체 감사 (최대 40개 파일 상한) |
| `get_workspace` | 현재 연결된 대상 저장소 및 설정 경로 확인 |
| `set_workspace` | 리뷰 대상 저장소 전환 (서버 실행 중에만 유지) |

앞의 세 diff 도구는 `paths=["src/parser"]` 인자로 특정 폴더나 파일만 좁혀서 리뷰할 수 있습니다.

### 에이전트 지시 파일 (AGENTS.md)

**Zed 연동에서 가장 중요한 설정입니다.** 지시 파일이 없으면 에이전트가 MCP 도구를 호출하는 대신 직접 `git diff`를 읽고 임의로 리뷰를 작성하는 경우가 흔합니다.

CREX 루트에 있는 [`AGENTS.md`](../../AGENTS.md) 파일을 **리뷰 대상 저장소 루트**에 복사하십시오.

```bash
cp <crex-설치경로>/AGENTS.md /work/myrepo/AGENTS.md
```

모든 프로젝트에 공통으로 적용하려면 사용자 전역 Zed 설정 폴더에 복사합니다.

```bash
cp <crex-설치경로>/AGENTS.md ~/.config/zed/AGENTS.md
```

> **주의:** Zed는 `.rules` → `.cursorrules` → `AGENTS.md` → `CLAUDE.md` 순서로 파일을 찾으며 **가장 먼저 발견된 파일 하나만** 읽습니다. 대상 저장소에 이미 `.rules` 파일이 있다면 `AGENTS.md`는 무시되므로, 그 경우에는 `AGENTS.md`의 내용을 기존 `.rules` 파일에 합쳐 넣으십시오.

---

## 성능 및 소요 시간

청크 1개당 LLM 호출은 최대 2회(생성 1회 + 지적이 나온 경우 검증 1회)이며, 실제로는 청크당 평균 1.2~1.5회 호출됩니다.

파일 10개, 청크 25개 규모의 MR 기준 예상 소요 시간:

- **청킹 및 AST·호출 흐름 분석**: 1초 미만
- **정적분석 그라운딩**: 5~60초 (C# 솔루션 빌드가 포함되면 이 단계가 대부분을 차지합니다)
- **지적 생성 (`RuleChecker`)**: 25개 청크 ÷ 4 워커 × 약 8초 ≈ 50초
- **교차 검증 (`ReviewFilter`)**: 후보 지적 12건 ÷ 4 워커 × 약 2초 ≈ 6초

전체 합산 약 1~2분 내외에 완료됩니다. 만약 예상보다 훨씬 느리다면 다음 순서로 점검하십시오.

1. **정적분석 소요 시간**: `-v` 옵션으로 단계별 소요 시간을 확인하십시오. C# 솔루션 빌드가 지나치게 오래 걸린다면 `grounding.analyzers`에서 `roslyn`을 제외하는 것을 고려하십시오.
2. **모델 추론(Thinking) 모드**: Qwen3.x 계열에서 장문 사고 과정이 길어지면 응답 시간이 수 배로 늘어납니다 (`extra_body`의 `enable_thinking: false` 설정 참고).
3. **`max_workers` 조정**: vLLM 서버의 GPU 여유가 충분하다면 `review.max_workers`를 `6~8`로 높여 병렬 처리량을 늘릴 수 있습니다.

---

## 상세 로그 확인

```bash
python -m crex review -v
```

`-v` 옵션을 붙이면 청크 분할, 정적분석기별 보고 건수, 검증 필터 기각 통계가 표준 에러(`stderr`)로 출력됩니다(리포트 본문은 `stdout`으로 나가므로 리다이렉션 시 서로 섞이지 않습니다).

```
INFO    crex.pipeline: 청크 25개 생성
INFO    crex.ground: [clang-tidy] 7건 보고
INFO    crex.ground: [cppcheck] 건너뜀 — cppcheck 를 PATH 에서 찾을 수 없습니다
INFO    crex.filter: 검증 12건 → 유지 5건 (기각률 58.3%: 결정론적 3, LLM 4, 오류 0)
```

---

## 버전 및 설정 파일 관리

### CREX 버전

```bash
python -m crex --version    # crex 0.5
```

CLI의 `--version`, `doctor` 출력, SARIF 리포트의 `tool.driver.version`, 관제 화면 헤더, MCP 서버 버전, 반입 번들의 `MANIFEST.txt`까지 모두 `crex/__init__.py`의 단일 버전 문자열을 공유합니다.

### 설정 및 택소노미 커밋

- **`crex.json`**과 **`.crexignore`**: 저장소에 커밋하여 팀원 모두가 동일한 설정과 제외 규칙으로 리뷰하도록 맞추는 것을 권장합니다.
- **`rules/taxonomy.toml`**: 룰 변경 이력이 곧 팀의 코드 리뷰 튜닝 이력이므로 반드시 버전 관리에 포함하십시오. 커밋 메시지에 변경 전후의 KBI/FAR 측정치를 함께 기록해 두면 추후 이력 추적에 큰 도움이 됩니다.
- **`reports/`**: 일회성 리뷰 결과 폴더는 `.gitignore`에 추가하고, 단계별 골든셋 평가 리포트(`reports/phase-*.json`)만 보관해 비교에 활용하십시오.
