# 운영

## 폐쇄망 반입

> Python 런타임까지 통째로 담아 옮기는 절차는 [반입](transfer.md)에 따로
> 있습니다. `tools\package.ps1` 하나로 zip 이 나옵니다. 아래는 수동으로 옮길 때의
> 파일 목록입니다.

코어는 외부 패키지를 쓰지 않습니다. 소스만 옮기면 됩니다.

옮길 것:

```
crex/            파이프라인 본체
rules/          룰 택소노미
eval/           평가 하네스
tests/          테스트
docs/           이 문서
wiki/           에이전트용 영문 문서
README.md
CLAUDE.md
AGENTS.md                 Zed 에이전트 지시 (대상 저장소로 복사)
crex.example.toml
requirements.txt          MCP 서버용 (코어는 불필요)
requirements-optional.txt tree-sitter (선택)
```

반입 직후 순서:

```bash
python --version                # 3.11 이상인지
python tests/run_all.py         # 반입 무결성 — 128개 전부 통과해야 합니다
cp crex.example.toml crex.toml  # 엔드포인트 수정
python -m crex doctor           # 연결 확인
```

`tests/run_all.py` 는 LLM 없이, pip install 없이 돕니다. 가짜 vLLM 을 프로세스
안에 띄워서 HTTP 경로까지 실제로 태웁니다. 여기서 실패하면 파일이 덜 복사된 겁니다.

### 런타임 의존성

코어(`review` / `scan` / `doctor` / 테스트)는 표준 라이브러리만 씁니다.
**MCP 서버를 쓸 장비에만** 설치하면 됩니다.

```bash
pip download -r requirements.txt -d wheels/ \
    --platform win_amd64 --python-version 312 --only-binary=:all:
```

```bash
pip install --no-index --find-links wheels/ -r requirements.txt
```

fastmcp 는 의존성이 적지 않습니다(pydantic, httpx, mcp 등). `pip download` 가
끌어오는 wheel 전부를 함께 옮겨야 하고, 그만큼 보안 검토 대상이 늘어납니다.
Zed 을 쓰는 개발자 장비에만 설치하고, 빌드 서버에는 코어만 두는 편이 낫습니다.

GitPython 은 없어도 됩니다 — `crex/gitio.py` 가 subprocess 로 폴백합니다.
`python -m crex doctor` 의 마지막 절이 현재 상태를 보여줍니다.

### tree-sitter (선택)

없어도 동작합니다. 중괄호 매칭과 인덴트 추적으로 함수 경계를 찾는 폴백이
들어 있습니다. 정확도가 좀 떨어지는데, 특히 전처리기 조건부(`#if`) 안에서
괄호가 불균형한 C++ 코드에서 경계를 놓칠 수 있습니다. 그럴 땐 hunk 위아래로
6줄씩만 붙여서 리뷰합니다.

넣고 싶다면 인터넷 되는 장비에서:

```bash
pip download -r requirements-optional.txt -d wheels/ \
    --platform win_amd64 --python-version 312 --only-binary=:all:
```

폐쇄망에서:

```bash
pip install --no-index --find-links wheels/ -r requirements-optional.txt
```

`doctor` 로 확인하세요.

### Semgrep 룰팩

`semgrep_config = "auto"` 는 폐쇄망에서 동작하지 않습니다. 룰을 인터넷에서
받아오기 때문입니다. 룰팩을 미리 받아 옮기고 로컬 경로를 지정하세요.

```bash
# 인터넷 되는 장비에서
git clone --depth 1 https://github.com/semgrep/semgrep-rules /tmp/semgrep-rules
```

```toml
[grounding]
semgrep_config = "/opt/semgrep-rules"
```

지정하지 않으면 semgrep 은 아예 실행되지 않습니다. 다른 분석기는 그대로 돕니다.

---

## vLLM 기동

### Qwen3.6-27B (생성)

```bash
vllm serve /models/Qwen3.6-27B \
  --served-model-name Qwen3.6-27B \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --port 8000
```

`--served-model-name` 이 `crex.toml` 의 `model` 과 정확히 같아야 합니다.
경로를 그대로 쓰면 모델 이름도 경로가 되므로 명시해 주는 게 편합니다.

`--max-model-len` 은 32768 이면 충분합니다. CREX 는 8192 로 잘라서 보내므로
더 크게 잡을 이유가 없고, 크게 잡으면 KV 캐시가 메모리를 먹어 동시 처리량이
떨어집니다.

### 구조화 출력 백엔드

여기가 중요합니다. CREX 의 환각 방어 중 두 겹이 이것에 의존합니다.

vLLM 버전에 따라 플래그 이름이 바뀌었습니다. 0.6~0.8 대에서는
`--guided-decoding-backend xgrammar`, 그 이후로는 구조화 출력 설정이 다른
플래그로 옮겨갔습니다. 최근 빌드에서는 대체로 기본 활성화되어 있습니다.

```bash
vllm serve --help | grep -i -E "guided|structured|grammar"
```

로 확인하고 붙이세요. 확실히 아는 방법은 `doctor` 를 돌린 뒤 실제 리뷰를 한 번
해보는 겁니다. 구조화 출력이 안 걸리면 요청이 400이나 422로 떨어집니다.
그럴 때는 `structured_output_mode = "guided_json"` 으로 바꿔 보세요.

### Gemma 4 26B (검증)

```bash
vllm serve /models/gemma-4-26b-it \
  --served-model-name gemma-4-26b-it \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.45 \
  --port 8001
```

검증은 입력이 짧고 출력이 사실상 한 토큰입니다. 메모리를 적게 줘도 됩니다.
GPU 가 하나뿐이면 `--gpu-memory-utilization` 을 나눠서 두 프로세스를 한 장에
올리는 것도 가능합니다. 생성 0.55 / 검증 0.35 정도로 시작해서 조정하세요.

GPU 가 정말 부족하면 `[llm.verifier]` 블록을 지우고 생성 모델을 재사용하세요.
교차 모델 검증의 이점은 잃지만 결정론적 검사와 재판정 자체는 그대로 동작합니다.

---

## 텔레메트리

CREX 자체는 아무 데도 연결하지 않습니다. 설정된 vLLM 엔드포인트에만 HTTP 를
보냅니다. 외부 호출 코드가 없습니다.

`alibaba/open-code-review` 를 병행 평가한다면 그쪽은 따로 확인해야 합니다.
OTLP 텔레메트리 설정이 있는 것으로 보이므로, 반입 전에 비활성화하고
네트워크 레벨에서 egress 를 막아 이중으로 방어하는 걸 권합니다.

---

## 일상 운영

### MR 리뷰

```bash
python -m crex review --from $(git merge-base main HEAD) --to HEAD --out reports/
```

`--from` 에 그냥 `main` 을 넣으면 브랜치가 오래됐을 때 남의 변경까지 딸려
들어옵니다. `merge-base` 로 실제 분기점을 잡으세요.

결과 마크다운을 MR 코멘트에 붙여넣으면 됩니다. 자동화하려면 사내 GitLab/Gerrit
API 를 호출하는 얇은 스크립트를 짜면 되는데, 도입 초기에는 사람이 한 번 훑어보고
붙이는 편을 권합니다. 명백한 오탐을 걸러내면서 룰 튜닝 감각이 생깁니다.

### 커밋 전 자가 점검

```bash
python -m crex review --staged
```

개발자가 스스로 돌리는 용도입니다. 이 경로가 실제로는 제일 많이 쓰이게 됩니다 —
남이 지적하기 전에 내가 먼저 보는 게 심리적으로 훨씬 편하니까요.

셸 별칭을 하나 만들어 배포하면 채택률이 눈에 띄게 오릅니다.

### 레거시 감사

```bash
python -m crex scan src/legacy/*.cpp --out reports/legacy/
```

diff 리뷰보다 오탐이 많습니다. 변경 라인이라는 필터가 없어서 모든 줄이 지적
대상이 되기 때문입니다. 한 번에 몰아서 돌리고 결과를 사람이 선별하는 방식으로
쓰세요. 정기 자동화에는 적합하지 않습니다.

---

## Zed 연동 (MCP)

Zed 에이전트 패널에서 "내 변경사항 리뷰해줘"로 부를 수 있습니다. 터미널로
나갈 필요가 없어져서 실사용 빈도가 눈에 띄게 올라갑니다.

### 설정

`~/.config/zed/settings.json` (또는 프로젝트의 `.zed/settings.json`):

```json
{
  "context_servers": {
    "crex": {
      "command": "python",
      "args": ["-m", "crex.mcp"],
      "env": {
        "CREX_WORKSPACE": "/work/myrepo",
        "CREX_CONFIG": "/work/myrepo/crex.toml",
        "CREX_REPORTS": "/work/myrepo/reports"
      }
    }
  }
}
```

**먼저 `pip install -r requirements.txt` 가 되어 있어야 합니다.** MCP 서버는
FastMCP 를 씁니다. CLI 와 테스트는 그대로 의존성 없이 돕니다.

`command` 는 `python` 이 PATH 에 있어야 잡힙니다. Zed 은 셸 프로파일을 안 읽는
경우가 있으니, 안 되면 절대 경로(`/usr/bin/python3`, `C:\...\python.exe`)를
넣으세요. 가상환경에 설치했다면 그 환경의 `python` 절대 경로를 줘야 합니다 —
이게 제일 흔한 실패 원인입니다.

환경변수 셋 다 선택입니다. 없으면 현재 디렉터리에서 git 루트와 `crex.toml` 을
찾고 `reports/` 에 리포트를 씁니다.

| 변수 | 뜻 |
|---|---|
| `CREX_WORKSPACE` | 리뷰 대상 저장소 루트. 이전 이름 `CREX_REPO` 도 그대로 받습니다 |
| `CREX_CONFIG` | 설정 파일. 생략하면 `<워크스페이스>/crex.toml` 을 먼저 봅니다 |
| `CREX_REPORTS` | 리포트 저장 위치. 기본은 `<워크스페이스>/reports` |

**CREX 설치본은 한 벌이면 됩니다.** 저장소마다 복사하지 말고 프로젝트별
`.zed/settings.json` 에서 `CREX_WORKSPACE` 만 다르게 주세요. `args` 의 `-m crex.mcp`
를 찾으려면 CREX 가 `PYTHONPATH` 에 있거나 `command` 를 CREX 루트의 파이썬으로
지정하면 됩니다. 자세한 우선순위는
[설정](configuration.md#workspace--리뷰-대상-저장소)에 있습니다.

설정을 바꾸면 Zed 을 재시작하거나 창을 새로 고쳐야 반영됩니다.

### 기본은 stdio 입니다

Zed 을 비롯한 대부분의 에디터는 로컬 MCP 서버를 stdio 로 붙입니다. 에디터가
프로세스를 자식으로 띄우고 stdin/stdout 으로 대화하며, 수명이 에디터 세션에
묶입니다. 데몬도 포트도 없고 네트워크 리스너가 아예 안 생깁니다 — 폐쇄망 보안
검토에서 설명하기 가장 쉬운 형태이므로, 가능하면 이쪽을 쓰세요.

### Streamable HTTP 엔드포인트

stdio 로 안 되는 경우가 있습니다. 서버 한 대를 여러 사람이 같이 쓰거나, 클라이언트가
다른 장비에 있거나, MCP 클라이언트가 stdio 를 지원하지 않는 경우입니다. 그때만
엽니다.

```bash
python -m crex.mcp --transport http
```

```
INFO    crex.mcp: Streamable HTTP — http://127.0.0.1:18766/mcp
```

| 옵션 | 뜻 | 기본값 |
|---|---|---|
| `--transport` | `stdio` 또는 `http` | `stdio` |
| `--host` | 바인드 주소 | `127.0.0.1` |
| `--port` | 포트 | `18766` |
| `--path` | 엔드포인트 경로 | `/mcp` |

워크스페이스·설정·리포트 위치는 stdio 와 똑같이 `CREX_WORKSPACE` 등 환경변수로
정합니다. 전송 방식만 다르고 나머지는 전부 같습니다.

클라이언트 쪽 설정은 URL 하나입니다.

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

키 이름은 클라이언트마다 다릅니다(`url` / `endpoint` / `serverUrl`). 쓰는 도구의
"remote MCP server" 또는 "streamable HTTP" 항목을 보세요.

**알아둘 것 세 가지.**

- **인증이 없습니다.** 붙을 수 있는 사람은 누구나 이 저장소의 소스를 리뷰에 태울
  수 있고, 리포트가 어디 있는지 알게 됩니다. 루프백에 묶어 두거나, 앞단에서 접근
  제어를 하는 망 안에서만 여세요.
- **루프백이 아닌 주소로 열면 `set_workspace` 가 막힙니다.** 대상 변경은 이 장비의
  임의 디렉터리를 열 수 있게 하는 일이라, 인증 없는 원격 연결에서는 받지 않습니다.
  다른 저장소를 보려면 `CREX_WORKSPACE` 를 주고 다시 띄우세요.
- **서버 한 대는 저장소 하나를 봅니다.** 워크스페이스는 프로세스 전역 상태라
  사용자별로 갈리지 않습니다. 여러 저장소를 동시에 서비스하려면 포트를 나눠
  여러 개 띄우세요.

포트를 여는 순간 반입 심사에서 설명할 것이 하나 늘어납니다. 그만한 이유가 있을
때만 쓰고, 아니면 stdio 로 두세요.

### 에이전트 모델도 사내 vLLM 으로

Zed 은 OpenAI 호환 커스텀 프로바이더를 지원합니다. `agent: open settings` →
LLM Providers → Add Provider 에서 API URL 에 vLLM 주소를 넣으면 됩니다.
에이전트 패널이 쓰는 모델과 crex 내부가 쓰는 모델은 별개이니, 같은 인스턴스를
가리켜도 되고 다르게 둬도 됩니다.

### 도구

| 도구 | 하는 일 |
|---|---|
| `review_staged` | 스테이징된 변경 — 가장 자주 씁니다 |
| `review_working_tree` | 커밋 안 한 변경 전부 |
| `review_diff` | 두 ref 사이 (MR 리뷰) |
| `review_file` | 파일 하나 전체 감사 |
| `review_directory` | 폴더 전체 감사 |
| `get_workspace` | 지금 어느 저장소를 보고 있는지 |
| `set_workspace` | 리뷰 대상 저장소를 바꾼다 (이 서버가 사는 동안만) |

앞의 셋은 `paths` 로 범위를 좁힐 수 있습니다. 큰 MR 에서 "파서 쪽만 보자" 같은
경우입니다.

`set_workspace` 는 설정 파일을 고치지 않습니다 — 서버를 다시 띄우면 원래 대상으로
돌아옵니다. 영구히 바꾸려면 `python -m crex workspace <경로>` 를 쓰거나
`CREX_WORKSPACE` 를 고치세요. 에이전트와의 대화 한 번이 다음 사람의 실행 대상까지
바꿔 놓으면 안 되기 때문입니다.

```
파서 폴더 변경분만 리뷰해줘
→ review_staged(paths=["src/parser"])
```

### 반환값은 요약입니다

도구가 돌려주는 건 지적 목록 요약이고, 전체 리포트는 `CREX_REPORTS` 경로에
마크다운·SARIF·JSON 으로 저장됩니다.

전문을 돌려주지 않는 건 의도입니다. MCP 도구의 반환값은 그대로 에이전트
컨텍스트로 들어가는데, 지적 20건짜리 리포트를 통째로 넘기면 27B 모델의 컨텍스트가
리뷰 결과로 가득 찹니다. 파이프라인 전체가 컨텍스트를 아끼는 설계인데 마지막에
흘리면 곤란합니다.

### 에이전트 지시 파일 (AGENTS.md)

**이게 제일 효과가 큽니다.** 지시를 주지 않으면 Zed 에이전트가 도구를 부르는
대신 스스로 diff 를 읽고 리뷰해 버립니다. 검증을 안 거친 지적이라 CREX 를 쓰는
의미가 사라집니다.

CREX 저장소 루트의 [`AGENTS.md`](../AGENTS.md) 를 **리뷰 대상 저장소 루트로
복사**하세요.

```bash
cp <crex-설치경로>/AGENTS.md /work/myrepo/AGENTS.md
```

모든 프로젝트에 한 번에 적용하려면 개인 설정 위치에 둡니다.

```bash
cp <crex-설치경로>/AGENTS.md ~/.config/zed/AGENTS.md
```

내용은 여섯 가지입니다 — 직접 리뷰하지 말고 도구를 부를 것, 어떤 말에 어떤
도구인지, 대상 저장소는 사용자가 지목했을 때만 바꿀 것, 요약을 각색하지 말 것,
룰 ID 를 지우지 말 것, 오류는 그대로 전달할 것.

> **주의:** Zed 은 `.rules` → `.cursorrules` → … → `AGENTS.md` → `CLAUDE.md`
> 순서로 읽고 **먼저 찾은 것 하나만** 씁니다. 대상 저장소에 `.rules` 가 이미
> 있으면 `AGENTS.md` 는 무시되므로, 그 경우 내용을 `.rules` 에 합쳐야 합니다.
> 이게 조용히 실패하는 지점입니다.

### 에이전트가 그래도 도구를 안 고를 때

Zed 문서도 "모델에 따라 신뢰도가 다르다"고 적어두고 있고, 27B 급에서는 실제로
그렇습니다. 두 가지가 더 있습니다.

- **서버 이름을 직접 부르기** — "crex 로 리뷰해줘"
- **커스텀 에이전트 프로필** — 내장 도구를 끄고 CREX 도구만 남기면 다른 데로
  새지 않습니다. 도구 권한 키는 `mcp:crex:review_staged` 형식입니다.

리뷰 전용 프로필을 만들어 팀에 배포하는 걸 권합니다.

### 폴더 감사 상한

`review_directory` 는 대상이 40개를 넘으면 거부합니다. 파일 하나가 청크 여러 개가
되고 청크마다 LLM 호출이 최대 두 번이라, 큰 폴더를 무심코 지정하면 몇 시간이
걸립니다. 조용히 자르지 않고 범위를 좁히라고 알려줍니다.

상한은 `crex/paths.py` 의 `MAX_SCAN_FILES` 입니다.

---

## 성능

청크 하나에 LLM 호출이 두 번(생성 1 + 검증 1)입니다. 지적이 나온 청크만 검증하므로
실제로는 1.2~1.5회 정도입니다.

파일 10개, 청크 25개짜리 MR 이면:

- 청킹: 1초 미만
- 정적분석: 5~60초 (C# 빌드가 끼면 여기가 지배적입니다)
- 생성: 청크 25개 ÷ 워커 4개 × 8초 ≈ 50초
- 검증: 지적 12건 ÷ 워커 4개 × 2초 ≈ 6초

합쳐서 1~2분 남짓입니다.

느리다면 순서대로 의심하세요.

1. **정적분석** — `-v` 로 단계별 시간을 보세요. C# 솔루션 빌드가 2분씩 걸리면
   `analyzers` 에서 `roslyn` 을 빼는 걸 고려하세요.
2. **추론 모드** — Qwen3.x 에서 `enable_thinking` 을 안 껐으면 몇 배가 됩니다.
3. **`max_workers`** — vLLM 이 여유롭다면 8까지 올려보세요.
4. **`absolute_max_lines`** — 400줄 청크가 많으면 프롬프트가 길어집니다.
   250 정도로 줄이면 빨라지는데, 함수 맥락이 잘려서 품질이 떨어질 수 있으니
   골든셋으로 확인하고 바꾸세요.

---

## 로그

```bash
python -m crex review -v
```

`-v` 를 붙이면 청크 생성, 분석기 실행 결과, 필터 통계가 stderr 로 나옵니다.
리뷰 결과는 stdout 이므로 섞이지 않습니다.

```
INFO    crex.pipeline: 청크 25개 생성
INFO    crex.ground: [clang-tidy] 7건 보고
INFO    crex.ground: [cppcheck] 건너뜀 — cppcheck 를 PATH 에서 찾을 수 없습니다
INFO    crex.filter: 검증 12건 → 유지 5건 (기각률 58.3%: 결정론적 3, LLM 4, 오류 0)
```

기각률 한 줄만 봐도 그날 파이프라인이 정상인지 대충 압니다.

---

## 버전 관리

### CREX 자체의 버전

```bash
python -m crex --version    # crex 0.1
```

`doctor` 의 첫 줄, SARIF 리포트의 `tool.driver.version`, 관제 화면의
`/api/config`, MCP 서버가 클라이언트에 알리는 서버 버전, 반입 번들의
`MANIFEST.txt` 가 전부 같은 값을 씁니다. 리포트만 보고 "이건 어느 버전이 낸
지적인가"를 되짚을 수 있어야 하기 때문입니다.

값은 `crex/__init__.py` 한 줄에서 옵니다. 올릴 때는 거기와 `README.md` 두 곳만
고치면 되고, 소스 어딘가에 숫자를 또 적으면 테스트가 실패합니다.

룰 택소노미의 버전(`rules/taxonomy.toml` 의 `meta.version`)은 이것과 별개입니다.
룰은 평가 리포트를 몇 달에 걸쳐 잇는 키라 자기 수명을 따로 가집니다.

### 설정 파일

`crex.toml` 은 저장소에 커밋하세요. 팀원이 다른 설정으로 돌려서 다른 결과를 보는
상황을 막아줍니다. 엔드포인트 주소가 장비마다 다르다면 `crex.toml` 에는 공통
설정만 두고 `.crex.toml` 을 개인용으로 `.gitignore` 에 넣는 방법도 있습니다.
탐색 순서상 `crex.toml` 이 먼저이므로, 개인 설정을 우선하려면 `--config` 로
명시하세요.

`rules/taxonomy.toml` 도 당연히 커밋합니다. 룰 변경 이력이 곧 튜닝 이력입니다.
커밋 메시지에 그때의 KBI/FAR 을 적어두면 나중에 큰 도움이 됩니다.

```
룰 추가: csharp.cancellationtoken-ignored

골든셋 62건 기준 KBI 58.3% → 61.1%, FAR 19.4% → 20.1%
FAR 이 0.7%p 올랐지만 재현율 이득이 커서 유지.
```

`reports/` 는 커밋하지 마세요. 대신 Phase 별 평가 리포트
(`reports/phase-*.json`)는 남겨두면 나중에 비교할 때 씁니다.
