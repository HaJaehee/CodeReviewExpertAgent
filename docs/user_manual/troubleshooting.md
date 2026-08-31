# 문제 해결

## 지적이 항상 0건입니다

가장 먼저 확인할 것은 **그 0건이 정상인지 고장인지**입니다. 리포트 맨 위를 보세요.

```
> ⚠️ **이 결과는 신뢰할 수 없습니다 — 생성 호출 실패 12건.**
> 지적이 0건인 것은 코드가 깨끗해서가 아니라 파이프라인이 끝까지 돌지
> 못했기 때문입니다. `python -m crex doctor` 로 엔드포인트를 점검하십시오.
```

이 경고가 있으면 고장입니다. 종료 코드도 3이 나옵니다. 없으면 파이프라인은
정상이고, 아래 "정상인데 0건일 때" 로 가세요.

### doctor 는 OK 인데 0건일 때

`doctor` 의 LLM 항목은 두 줄로 나옵니다. 둘 다 봐야 합니다.

```
  생성: Qwen3.6-27B @ http://vllm-qwen:8000/v1
    OK   연결·모델
         ok
    실패 구조화 출력 (findings)
         guided decoding 을 성립시키지 못했다 — 이 엔드포인트로는 리뷰가 불가능하다.
         시도: response_format → HTTP 400, guided_json → HTTP 400, ...
```

**연결이 OK 인데 구조화 출력이 실패**하는 경우가 있습니다. vLLM 은 떠 있고
모델 이름도 맞는데, guided decoding 이 안 되는 상태입니다. 리뷰는 청크마다
LLM 에 JSON Schema 를 걸어 호출하므로 이게 막히면 전 청크가 실패하고 결과가
0건이 됩니다. 아래 [HTTP 400 / 422](#http-400--422--구조화-출력) 로 가세요.

구조화 출력 단계는 세 가지를 잡아냅니다.

| 메시지 | 뜻 |
|---|---|
| `guided decoding 을 성립시키지 못했다` | 서버가 스키마 요청을 거부합니다 |
| `스키마가 강제되지 않는다 (설명이 섞여 나옴)` | 200 은 주지만 guided decoding 이 실제로 안 걸립니다 |
| `enum 이 지켜지지 않는다` | 스키마를 받긴 하는데 제약을 무시합니다 |

뒤의 둘은 요청이 성공하므로 연결 점검으로는 절대 안 잡힙니다. 그리고 이 상태로
리뷰를 돌리면 라인 번호 환각을 막지 못합니다.

### 정상인데 0건일 때

파이프라인이 건강한데 0건이면 아래를 순서대로 보세요.

```bash
python -m crex review --staged -v 2>&1 | grep -E "청크|생성된 지적|검증"
```

- **`리뷰할 청크가 없습니다`** — 변경된 파일이 지원 언어(`.cpp` `.h` `.cs` `.py` 등)가
  아니거나, diff 가 비어 있습니다. `git diff --stat` 으로 확인하세요.
- **`청크 N개 생성` 인데 `생성된 지적 0건`** — 모델이 지적할 게 없다고 판단한
  겁니다. CREX 는 정밀도를 위해 재현율을 의도적으로 포기하므로 작은 변경에서
  0건은 흔합니다. 확인하려면 알려진 결함이 있는 파일에 `scan` 을 걸어 보세요.
- **`생성된 지적 N건` 인데 `유지 0건`** — 필터가 전부 걷어냈습니다. 기각 사유는
  JSON 리포트의 `rejected` 배열에 남습니다. 기각률이 늘 100% 면 검증 모델이
  너무 엄격하거나 프롬프트가 안 맞는 겁니다.
- **`min_severity=... 로 N건 숨김`** — 유효한 지적을 설정이 가리고 있습니다.
  `review.min_severity` 를 낮추세요.

---

## LLM 엔드포인트 연결 실패

```
실패 생성: Qwen3.6-27B @ http://vllm-qwen:8000/v1
     LLMError: 연결 실패: <urlopen error [WinError 10061] ...>
```

연결 자체가 안 되는 겁니다. 순서대로 확인하세요.

```bash
curl http://vllm-qwen:8000/v1/models
```

이게 안 되면 CREX 문제가 아닙니다. vLLM 이 떠 있는지, 포트가 맞는지, 방화벽이
막는지 보세요. 폐쇄망에서는 사내 프록시 환경변수(`HTTP_PROXY`, `NO_PROXY`)가
`urllib` 을 엉뚱한 데로 보내는 경우가 종종 있습니다. `NO_PROXY` 에 vLLM 호스트를
넣으세요.

`base_url` 끝에 `/v1` 이 있는지도 확인하세요. `/chat/completions` 까지 붙이면
안 됩니다.

---

## HTTP 404

```
LLMError: HTTP 404: {"object":"error","message":"The model `Qwen3.6-27B` does not exist."}
```

모델 이름이 안 맞습니다. vLLM 이 아는 이름을 확인하세요.

```bash
curl http://vllm-qwen:8000/v1/models
```

`--served-model-name` 없이 띄웠다면 모델 이름이 파일 경로 전체입니다.
그 경로를 `crex.json` 에 그대로 넣거나, vLLM 을 재기동하면서
`--served-model-name Qwen3.6-27B` 를 주세요.

404, 400, 422 는 재시도해도 결과가 같으므로 CREX 가 즉시 포기합니다.
로그에 한 번만 찍히는 게 정상입니다.

---

## HTTP 400 / 422 — 구조화 출력

```
LLMError: HTTP 400: {"object":"error","message":"...response_format..."}
```

기본 설정(`structured_output_mode = "auto"`)이면 CREX 가 먼저 스스로 시도합니다.
`response_format` → `guided_json` → 완화 스키마 순으로 내려가면서 되는 조합을
찾고, 찾으면 기억해 이후 호출에 바로 씁니다. 그러니 이 오류가 로그에 한두 번
보이고 리뷰가 정상 진행됐다면 손댈 것이 없습니다.

넷 다 실패하면 이렇게 나옵니다.

```
guided decoding 을 성립시키지 못했다 — 이 엔드포인트로는 리뷰가 불가능하다.
  시도: response_format → HTTP 400, guided_json → HTTP 400,
        response_format+완화 → HTTP 400, guided_json+완화 → HTTP 400
```

이건 vLLM 쪽 문제입니다. 확인하세요.

- vLLM 을 **guided decoding 백엔드와 함께** 띄웠는지
  (`--guided-decoding-backend xgrammar`). 이 옵션 없이 뜬 서버는 스키마 요청을
  전부 거부합니다.
- 구버전 vLLM 이라 백엔드 이름을 요청에 실어야 한다면 다음을 넣으세요.

```json
{
  "llm": {
    "generator": {
      "structured_output_mode": "guided_json",
      "guided_decoding_backend": "xgrammar"
    }
  }
}
```

`guided_decoding_backend` 는 기본적으로 **보내지 않습니다**. 최신 vLLM 이 이
필드를 모르는 키로 보고 400 을 내기 때문입니다. 구버전에서만 적으세요.
`llm.verifier` 에도 같이 넣어야 합니다.

**이걸 대충 넘기지 마세요.** 구조화 출력이 안 걸리면 룰 ID 와 라인 번호에 대한
enum 제약이 사라집니다. 환각 방어 네 겹 중 두 겹이 날아가고, 남은 두 겹이
받아내긴 하지만 오탐이 눈에 띄게 늘어납니다.

---

## `content 없음 (reasoning 전용 응답?)`

```
LLMError: content 없음 (reasoning 전용 응답?): {'role': 'assistant', 'reasoning_content': '...'}
```

추론 모델이 사고 과정만 내놓고 본문을 안 냈습니다. Qwen3.x 계열에서 추론 모드가
켜져 있을 때 나옵니다.

```json
{
  "llm": {
    "generator": {
      "extra_body": {
        "chat_template_kwargs": { "enable_thinking": false }
      }
    }
  }
}
```

`max_output_tokens` 이 너무 작아서 사고 과정만 쓰다가 잘렸을 수도 있습니다.
추론 모드를 꼭 써야 한다면 2000 이상으로 올리세요. 다만 리뷰는 고정 단계로
돌아가므로 추론이 도움이 되지 않습니다. 끄는 쪽을 권합니다.

---

## 어떤 청크만 `JSON 파싱 실패` 가 납니다

```
crex.generate: src/buffer.cpp#1 리뷰 실패: 응답이 출력 토큰 한도(900)에 걸려
잘렸고, 건질 수 있는 항목이 없었습니다. 설정의 max_output_tokens 를 올리거나
review.max_findings_per_chunk 를 줄이십시오. 끝부분: '..."suggestion": "auto val
= NByte(static_cast<byte>(CMSS_ENUM::Repeat'
```

청크 0은 멀쩡한데 청크 1만 실패한다면 거의 항상 이것입니다. 구조화 출력 설정
문제가 아닙니다 — 스키마가 완벽히 걸려 있어도 `max_tokens` 는 문법과 무관하게
그 자리에서 생성을 끊습니다. 모델이 `suggestion` 에 소스코드를 인용하던 중이라면
따옴표가 열린 채로 응답이 끝나고, JSON 은 당연히 파싱되지 않습니다. 청크마다
코드 길이가 다르니 **되는 청크와 안 되는 청크가 갈립니다.**

`llm.generator.max_output_tokens` 를 올리세요. 지적 5건 기준 1600 이면 넉넉합니다
([설정](configuration.md#출력-토큰-상한은-청크당-지적-수에-맞춥니다) 참고).
`review.max_findings_per_chunk` 를 줄여도 됩니다.

온전히 끝난 지적이 하나라도 있으면 그것들은 살려냅니다. 그때는 실패가 아니라
경고로 나옵니다.

```
  - src/buffer.cpp#1 응답이 잘려 지적 3건만 복구했습니다 — llm.generator.max_output_tokens 를
    올리거나 review.max_findings_per_chunk 를 줄이십시오
```

지적이 몇 건인지가 아니라 **몇 건을 잃었는지 모른다**는 것이 문제이므로, 이
경고가 보이면 값을 올리고 다시 돌리세요.

한편 아래 메시지는 다른 병입니다.

```
LLMError: JSON 파싱 실패, guided decoding 설정을 확인하십시오: '물론입니다! 아래에...'
```

이쪽은 응답이 잘린 게 아니라 **스키마가 아예 강제되지 않은** 경우입니다. 모델이
JSON 대신 설명을 늘어놓았다는 뜻이므로, `python -m crex doctor` 로 구조화 출력
단계를 확인하세요.

---

## `diff 와 파일 내용이 N곳에서 불일치한다`

```
crex.pipeline: src/buffer.cpp 건너뜀: diff/파일 불일치
```

diff 가 기술하는 내용과 디스크의 파일이 다릅니다. 그대로 진행하면 라인 번호가
밀린 채로 존재하지 않는 줄을 지적하게 되므로, 해당 파일을 건너뜁니다.

원인은 대개 셋 중 하나입니다.

**diff 를 저장해뒀다가 나중에 돌린 경우.** 그 사이 코드가 바뀌었습니다.
diff 를 다시 만드세요.

**과거 커밋을 비교하는데 작업 트리는 현재 상태인 경우.** 이게 제일 흔합니다.

```bash
python -m crex review --from v1.0 --to v1.1     # 작업 트리는 main 최신
```

`--to v1.1` 의 코드가 디스크에 없습니다. 그 시점을 체크아웃한 사본을 만들고
`--workspace` 로 가리키세요.

```bash
git worktree add ../snap-v1.1 v1.1
python -m crex review --from v1.0 --to v1.1 --workspace ../snap-v1.1
```

**개행 문자 문제.** 파일이 CRLF 인데 git 설정이 어긋나서 diff 는 LF 로 나오는
경우입니다. `git config core.autocrlf` 를 확인하세요.

정말 급하면 `on_mismatch = "warn"` 으로 진행할 수 있지만, 그 결과의 라인 번호는
믿지 마세요.

---

## 지적이 하나도 안 나온다

먼저 이게 정상일 수 있다는 걸 염두에 두세요. CREX 는 확신이 없으면 침묵합니다.

그래도 이상하다면 `-v` 로 단계별 숫자를 보세요.

```
INFO    crex.pipeline: 청크 0개 생성
```

**청크가 0개** — diff 에 지원 언어 파일이 없거나, 전부 `diff/파일 불일치` 로
건너뛰어졌습니다. 위쪽 로그를 확인하세요.

```
INFO    crex.pipeline: 생성된 지적 0건
```

**생성이 0건** — 모델이 아무것도 못 찾았습니다. 프롬프트가 잘렸는지 의심해
보세요. `absolute_max_lines` 가 크고 룰이 많으면 8192 예산을 넘겨 코드 가운데가
잘려나갑니다. `max_input_tokens` 를 잠깐 12288 로 올려서 달라지는지 보고,
달라진다면 청크 크기를 줄이는 쪽으로 해결하세요 (컨텍스트를 늘리는 건 최후의
수단입니다).

```
INFO    crex.filter: 검증 8건 → 유지 0건 (기각률 100%: 결정론적 8, LLM 0, 오류 0)
```

**전부 결정론적으로 기각** — 라인 번호가 어긋나고 있습니다. JSON 리포트의
`rejected` 에서 사유를 보세요. `line_out_of_range` 뿐이라면 구조화 출력이
안 걸려서 모델이 임의의 라인 번호를 내고 있을 가능성이 큽니다.

```
INFO    crex.filter: 검증 8건 → 유지 0건 (기각률 100%: 결정론적 0, LLM 0, 오류 8)
```

**전부 오류** — 검증 엔드포인트가 죽었습니다. CREX 는 검증에 실패한 지적을
보수적으로 기각합니다(통과시키지 않습니다). `doctor` 로 검증 쪽을 확인하세요.

---

## 오탐이 너무 많다

FAR 이 25% 를 넘으면 조치가 필요합니다.

**1. 어떤 룰이 만드는지 봅니다.**

```bash
python eval/run_eval.py run --out reports/now.json
```

룰별 정밀도가 낮은 순으로 나옵니다. 대개 한두 개 룰이 오탐의 절반을 만듭니다.

**2. 그 룰의 `counter` 를 보강합니다.**

JSON 리포트에서 그 룰이 만든 지적을 몇 개 읽어보면 패턴이 보입니다.
"아 이건 DI 컨테이너가 관리하는 거라 괜찮은데" 같은 게 반복되면 그걸
`counter` 에 적으세요. 자세한 건 [룰 작성법](writing-rules.md#counter--여기가-진짜-승부처)에 있습니다.

**3. `exclude` 를 넓힙니다.**

생성 코드와 서드파티가 리뷰 대상에 들어와 있지 않은지 보세요. 이것만으로
꽤 줄어듭니다.

**4. 검증 모델을 바꿔봅니다.**

생성과 검증이 같은 모델이면 자기 환각을 못 잡습니다. 두 번째 vLLM 인스턴스를
띄울 여력이 있는지 검토하세요.

---

## 정적분석이 안 돌아간다

```
INFO    crex.ground: [cppcheck] 건너뜀 — cppcheck 를 PATH 에서 찾을 수 없습니다
```

말 그대로입니다. 없는 도구는 조용히 건너뜁니다. `doctor` 로 전체 목록을 보고,
채우려면 [정적분석 도구](analyzers.md)에서 내려받는 곳을 보세요.

설치했는데도 "찾을 수 없다"가 나온다면 PATH 문제입니다. CREX 는 `shutil.which`
로 찾으므로, 설치 후 새로 연 터미널에서 `clang-tidy --version` 이 되는지부터
확인하세요. Windows 인스톨러가 PATH 등록을 물어보는데 넘긴 경우가 흔합니다.

```
INFO    crex.ground: [clang-tidy] 건너뜀 — 120초 내에 끝나지 않아 중단
```

clang-tidy 는 헤더가 많은 C++ 에서 쉽게 몇 분을 먹습니다.
`compile_commands.json` 이 없으면 특히 심합니다. `python -m crex compiledb` 로
컴파일 DB 를 만들어주거나, `grounding.timeout` 을 올리거나, clang-tidy 를 빼고
cppcheck 만 쓰세요.

```
INFO    crex.ground: [roslyn] 0건 보고
```

`dotnet build` 는 돌았는데 경고가 없습니다. 프로젝트에 분석기가 활성화되어
있는지 확인하세요. `.editorconfig` 나 `Directory.Build.props` 에서
`EnableNETAnalyzers` 를 켜야 합니다.

증분 빌드 때문은 아닙니다 — `--no-incremental` 로 매번 다시 컴파일합니다. 빌드가
실패했다면 0건이 아니라 "건너뜀" 으로 나옵니다.

```
INFO    crex.ground: [roslyn] 건너뜀 — 빌드할 .csproj/.sln 을 정하지 못했다
INFO    crex.ground: [roslyn] 건너뜀 — dotnet build 실패 (코드 1): ...
```

첫 줄은 저장소에 프로젝트가 여럿인데 솔루션이 없거나 여러 개라 대상을 하나로
좁히지 못한 경우입니다. `grounding.dotnet_project` 를 지정하세요 —
`python -m crex doctor` 의 "빌드 대상" 줄에서 지금 상태를 볼 수 있습니다.

둘째 줄은 빌드 자체가 깨진 경우입니다. 폐쇄망에서는 NuGet 복원 실패가 제일
흔합니다. 사내 NuGet 피드를 `nuget.config` 에 넣거나, 패키지를 미리 복원해 둔
장비에서 리뷰를 도세요.

---

## 엉뚱한 저장소를 리뷰한다

실행 로그 둘째 줄을 보세요. 어디를 보고 있는지, 그 값을 어디서 얻었는지가
그대로 찍힙니다.

```
INFO    crex.cli: 워크스페이스: D:\work\other [CREX_WORKSPACE]
```

대괄호 안이 출처입니다. 우선순위는 이렇고, 위가 이깁니다.

1. `--workspace` (`--repo` 도 같습니다)
2. 환경변수 `CREX_WORKSPACE`, `CREX_REPO`
3. `crex.json` 의 `workspace`
4. 현재 디렉터리에서 git 루트 탐색

셸에 예전 `CREX_REPO` 가 남아 있어서 `crex.json` 의 `workspace` 가 안 먹는 경우가
제일 흔합니다. `echo %CREX_REPO%` / `echo $CREX_REPO` 로 확인하세요.

---

## `워크스페이스 경로가 없습니다`

```
설정 오류: 워크스페이스 경로가 없습니다: D:\work\myrepo
  .git 이 있는 프로젝트 루트를 지정하십시오. 예: --workspace D:\work\myrepo
```

경로 오타이거나, `crex.json` 의 상대경로를 실행 위치 기준으로 쓴 경우입니다.
**설정 파일 안의 상대경로는 그 설정 파일이 있는 디렉터리 기준**입니다. 확실하게
하려면 절대경로를 쓰세요.

---

## `... 는 git 저장소가 아닙니다 (.git 이 없습니다)`

`review` 는 `git diff` 가 있어야 동작합니다. 워크스페이스로 지정한 폴더에 `.git`
이 없으면 거부합니다. 두 가지 중 하나입니다.

- 프로젝트 루트가 아니라 그 위나 옆 폴더를 지정했다 → 경로를 고치세요.
  하위 폴더를 지정한 경우는 자동으로 루트까지 올라가므로 문제되지 않습니다.
- 애초에 git 저장소가 아니다 → `review` 대신 `scan` 을 쓰세요. 파일·폴더 감사는
  git 없이 동작합니다.

---

## `설정 오류: ... 알 수 없는 설정 키`

```
설정 오류: ReviewConfig 에 알 수 없는 설정 키: ['max_worker'].
사용 가능한 키: ['max_findings_per_chunk', 'max_workers', 'min_severity', 'mode', 'require_changed_line']
```

오타입니다. 조용히 무시하지 않고 알려주는 게 맞습니다 — 무시하면 설정을 바꿨는데
아무 일도 안 일어나는 상황이 되니까요.

최상위 키도 검사합니다. `workspase` 처럼 쓰면 오류가 납니다. 이건 조용히
무시되면 리뷰 대상 저장소가 말없이 바뀌므로 특히 위험한 오타입니다.

---

## `review.mode = 'ocr' 는 아직 구현되지 않았다`

맞습니다. 현재 `"native"` 만 있습니다. `alibaba/open-code-review` 위임은
실제 바이너리의 출력 스키마를 확인한 뒤에 붙일 예정입니다.

---

## `ModuleNotFoundError: No module named 'fastmcp'`

MCP 서버만 FastMCP 가 필요합니다. CLI 와 테스트는 그대로 돕니다.

```bash
pip install -r requirements.txt
```

Zed 에서 서버가 안 뜬다면 대개 **경로 문제**입니다. 가상환경에 설치했는데
`settings.json` 의 `command` 가 시스템 `python` 을 가리키면 그 환경에는
fastmcp 가 없습니다. 가상환경의 절대 경로를 주세요.

```json
{ "command": "/work/venv/bin/python", "args": ["-m", "crex.mcp"] }
```

현재 상태는 `python -m crex doctor` 의 마지막 절에서 확인합니다.

---

## GitPython 이 없다는데 괜찮은가

괜찮습니다. `crex/gitio.py` 가 subprocess 로 폴백하고, 두 경로 모두 같은
unified diff 를 돌려줍니다. `doctor` 가 어느 쪽을 쓰는지 알려줍니다.

---

## 테스트가 실패한다

```bash
python tests/run_all.py
```

218개가 전부 통과해야 합니다. 반입 직후 실패한다면 파일이 덜 복사된 겁니다.
특히 `rules/taxonomy.toml` 이 빠지면 여러 모듈이 한꺼번에 터집니다.

```
SKIP: git 이 없어 파이프라인 테스트를 건너뛴다
```

파이프라인 테스트는 임시 git 저장소를 만들어 실제 diff 를 뽑습니다. git 이
없으면 건너뜁니다. 나머지 테스트는 그대로 돕니다.

---

## 여전히 모르겠을 때

`-v` 로 로그를 받고, JSON 리포트의 `rejected` 배열을 함께 보세요.
이 둘이면 파이프라인의 어느 단계에서 무엇이 사라졌는지 대부분 추적됩니다.

```bash
python -m crex review --staged --out reports/ -v 2> debug.log
```
