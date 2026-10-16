# 문제 해결

## 지적이 항상 0건입니다

가장 먼저 확인할 점은 **그 0건이 실제 결함이 없는 정상 결과인지, 아니면 파이프라인 오류로 인한 실패인지** 구분하는 것입니다. 리포트 최상단에 다음과 같은 경고 박스가 있는지 확인하십시오.

```
> ⚠️ **이 결과는 신뢰할 수 없습니다 — 생성 호출 실패 12건.**
> 지적이 0건인 것은 코드가 깨끗해서가 아니라 파이프라인이 끝까지 돌지
> 못했기 때문입니다. `python -m crex doctor` 로 엔드포인트를 점검하십시오.
```

이 경고가 있다면 파이프라인 호출이 실패한 것이며 종료 코드도 `3`으로 반환됩니다. 경고가 없다면 파이프라인은 정상 동작한 것이므로 아래 [정상인데 0건일 때](#정상인데-0건일-때) 절을 확인하십시오.

### doctor 는 OK 인데 0건일 때

`python -m crex doctor` 실행 결과에서 LLM 엔드포인트 항목은 **연결·모델**과 **구조화 출력** 두 줄로 표시됩니다. 두 항목 모두 `OK`인지 확인해야 합니다.

```
  생성: Qwen3.6-27B @ http://vllm-qwen:8000/v1
    OK   연결·모델
         ok
    실패 구조화 출력 (findings)
         guided decoding 을 성립시키지 못했다 — 이 엔드포인트로는 리뷰가 불가능하다.
         시도: response_format → HTTP 400, guided_json → HTTP 400, ...
```

vLLM 서버가 떠 있고 모델 이름도 맞아서 **연결·모델은 `OK`이지만 구조화 출력이 `실패`**하는 경우가 있습니다. CREX는 모든 청크 호출에 JSON Schema 제약을 걸어 요청하므로, 구조화 출력이 작동하지 않으면 전체 청크가 실패하여 결과가 0건이 됩니다. 이 경우 아래 [HTTP 400 / 422 — 구조화 출력](#http-400--422--구조화-출력) 절로 이동하십시오.

`doctor`의 구조화 출력 점검은 다음 세 가지 문제를 자동으로 진단합니다.

| 진단 메시지 | 원인 |
|---|---|
| `guided decoding 을 성립시키지 못했다` | 서버가 JSON Schema 요청 자체를 거부함 (HTTP 400/422) |
| `스키마가 강제되지 않는다 (설명이 섞여 나옴)` | HTTP 200을 반환하지만 실제로는 스키마 제약이 적용되지 않고 일반 텍스트가 섞여 나옴 |
| `enum 이 지켜지지 않는다` | JSON 형식은 지키지만 허용된 라인 번호·룰 ID(`enum`) 제약을 무시함 |

뒤의 두 가지는 단순 연결 확인만으로는 발견할 수 없으며, 그대로 리뷰를 실행하면 라인 번호 환각 방어가 무력화되므로 `doctor`가 실제 스키마 요청을 보내 검증합니다.

### 정상인데 0건일 때

파이프라인 오류 없이 정상 종료되었는데도 0건이라면 `-v` 옵션으로 단계별 로그를 확인하십시오.

```bash
python -m crex review --staged -v 2>&1 | grep -E "청크|생성된 지적|검증"
```

- **`리뷰할 청크가 없습니다`**: 변경된 파일이 지원 언어(`.cpp`, `.h`, `.cs`, `.py` 등)가 아니거나, `.crexignore` 제외 규칙에 매칭되었거나, diff가 비어 있는 경우입니다.
- **`청크 N개 생성`인데 `생성된 지적 0건`**: 생성 모델이 코드에서 지적할 만한 결함이 없다고 판단한 정상 상황입니다. 동작을 테스트해 보고 싶다면 의도적인 버그가 포함된 파일에 `python -m crex scan <파일>`을 실행해 보십시오.
- **`생성된 지적 N건`인데 `유지 0건`**: 생성된 후보 지적들이 검증 단계(`ReviewFilter`)에서 모두 기각된 경우입니다. 기각 사유는 JSON 리포트의 `rejected` 배열이나 관제 화면(`crex.viz`)의 판정 표에서 확인할 수 있습니다.
- **`min_severity=... 로 N건 숨김`**: 검증을 통과한 유효한 지적이 있지만 `review.min_severity` 설정 기준보다 낮아 리포트에서 숨겨진 경우입니다. `min_severity`를 `"low"`로 낮추어 확인하십시오.

---

## LLM 엔드포인트 연결 실패

```
실패 생성: Qwen3.6-27B @ http://vllm-qwen:8000/v1
     LLMError: 연결 실패: <urlopen error [WinError 10061] ...>
```

vLLM 서버와의 네트워크 연결 자체가 실패한 상태입니다.

```bash
curl http://vllm-qwen:8000/v1/models
```

위 명령이 실패한다면 vLLM 서버 기동 여부, 포트 번호, 방화벽 설정을 먼저 확인하십시오. 또한 폐쇄망 장비에 사내 프록시 환경변수(`HTTP_PROXY`, `HTTPS_PROXY`)가 설정되어 있으면 파이썬 `urllib`가 내부 vLLM 요청을 프록시 서버로 보내 연결이 실패할 수 있습니다. 이 경우 `NO_PROXY` 환경변수에 vLLM 호스트 주소를 추가하십시오.

더불어 `crex.json`의 `base_url`이 `/v1`로 끝나는지 확인하십시오(뒤에 `/chat/completions`를 붙이면 안 됩니다).

---

## HTTP 404 (모델 이름 불일치)

```
LLMError: HTTP 404: {"object":"error","message":"The model `Qwen3.6-27B` does not exist."}
```

`crex.json`에 적은 `model` 이름과 vLLM 서버에 등록된 모델 이름이 일치하지 않을 때 발생합니다.

```bash
curl http://vllm-qwen:8000/v1/models
```

vLLM을 띄울 때 `--served-model-name` 옵션을 생략했다면 모델 이름이 `/models/Qwen3.6-27B`처럼 전체 파일 경로로 등록됩니다. `crex.json`의 `model` 값을 서버에 표시된 이름과 맞추거나, vLLM 기동 시 `--served-model-name Qwen3.6-27B`를 지정하십시오.

---

## HTTP 400 / 422 — 구조화 출력

```
LLMError: HTTP 400: {"object":"error","message":"...response_format..."}
```

기본 설정(`structured_output_mode = "auto"`)에서는 CREX가 `response_format` → `guided_json` → 완화 스키마 순으로 자동 탐색하여 성공한 방식을 캐싱합니다. 따라서 실행 초기에 로그에 한두 번 경고가 찍힌 뒤 정상적으로 리뷰가 완료되었다면 별도로 조치할 필요가 없습니다.

네 가지 방식이 모두 실패하면 다음과 같이 출력됩니다.

```
guided decoding 을 성립시키지 못했다 — 이 엔드포인트로는 리뷰가 불가능하다.
  시도: response_format → HTTP 400, guided_json → HTTP 400,
        response_format+완화 → HTTP 400, guided_json+완화 → HTTP 400
```

이 경우 vLLM 서버 실행 옵션을 확인하십시오.

1. vLLM 기동 시 Guided Decoding 백엔드(예: `--guided-decoding-backend xgrammar`)가 활성화되어 있는지 확인합니다.
2. 구버전 vLLM이어서 요청 본문에 백엔드 이름을 명시해야 하는 환경이라면 `crex.json`에 아래 설정을 추가하십시오(`generator`와 `verifier` 양쪽 모두).

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

---

## `content 없음 (reasoning 전용 응답?)`

```
LLMError: content 없음 (reasoning 전용 응답?): {'role': 'assistant', 'reasoning_content': '...'}
```

추론(Thinking) 기능이 켜진 모델이 토큰 한도까지 내부 사고 과정(`reasoning_content`)만 출력하고 정작 최종 JSON 본문(`content`)을 생성하기 전에 잘렸을 때 나타납니다.

- CREX는 기본적으로 `max_output_tokens`를 `4096`으로 넉넉히 설정하고, `content`가 비어 있을 때 `reasoning_content` 내부에 완성된 JSON이 있으면 이를 자동으로 추출하는 폴백을 갖추고 있습니다.
- 만약 구형 설정 파일에서 `max_output_tokens`가 작게(예: `800`) 설정되어 있다면 `4096`으로 늘리거나, 아래와 같이 추론 모드를 비활성화하십시오.

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

---

## 어떤 청크만 `JSON 파싱 실패`가 납니다

```
crex.generate: src/buffer.cpp#1 리뷰 실패: 응답이 출력 토큰 한도(900)에 걸려
잘렸고, 건질 수 있는 항목이 없었습니다. 설정의 max_output_tokens 를 올리거나
review.max_findings_per_chunk 를 줄이십시오.
```

대부분의 청크는 성공하는데 유독 특정 청크만 실패한다면 출력 토큰 상한(`max_output_tokens`) 부족이 원인입니다. 스키마 제약이 걸려 있어도 출력 길이가 `max_tokens` 한도에 도달하면 서버는 문법과 무관하게 그 자리에서 응답을 끊어 버립니다.

- `llm.generator.max_output_tokens` 값을 `4096` 이상으로 높이십시오([설정 문서](configuration.md#출력-토큰-상한은-청크당-지적-수에-맞춥니다) 참고).
- 또는 `review.max_findings_per_chunk` 값을 줄여 한 청크에서 출력할 지적 수를 제한하십시오.

---

## `diff 와 파일 내용이 N곳에서 불일치한다`

```
crex.pipeline: src/buffer.cpp 건너뜀: diff/파일 불일치
```

diff에 기록된 코드와 현재 디스크에 있는 파일 내용이 서로 다를 때 발생합니다. 라인 번호가 밀린 채로 잘못된 줄을 지적하는 일을 막기 위해 해당 파일을 안전하게 건너뜁니다.

주요 원인은 다음 세 가지입니다.

1. **diff를 파일로 저장해 두었다가 나중에 실행한 경우**: 그사이 작업 트리 파일이 수정되었습니다. diff를 새로 추출하십시오.
2. **과거 두 커밋을 비교(`--from v1.0 --to v1.1`)하면서 작업 트리는 현재 `HEAD` 상태인 경우**: 가장 흔한 원인입니다. `--to v1.1` 시점의 코드가 디스크에 없으므로, 아래와 같이 해당 커밋의 워크트리를 만들어 `--workspace`로 지정하십시오.

   ```bash
   git worktree add ../snap-v1.1 v1.1
   python -m crex review --from v1.0 --to v1.1 --workspace ../snap-v1.1
   ```

3. **줄바꿈 문자(CRLF/LF) 불일치**: `git config core.autocrlf` 설정으로 인해 작업 트리와 diff의 개행 문자가 어긋난 경우입니다.

---

## 오탐이 너무 많습니다

오탐률(FAR)이 25%를 넘는다면 다음 순서로 조치하십시오.

1. **오탐을 유발하는 룰 식별**:

   ```bash
   python eval/run_eval.py run --out reports/now.json
   ```

   출력되는 룰별 정밀도 표에서 가장 아래쪽(정밀도가 낮은 순)에 위치한 룰을 확인합니다. 대개 상위 1~2개 룰이 전체 오탐의 절반 이상을 차지합니다.

2. **해당 룰의 `counter` 보강 또는 `.crexignore` 등록**:
   JSON 리포트에서 해당 룰이 낸 오탐 사례들을 확인한 뒤, 반복되는 정상 패턴을 `rules/taxonomy.toml`의 `counter` 필드에 추가합니다([룰 작성법](writing-rules.md#counter--여기가-진짜-승부처) 참고). 특정 레거시 폴더나 테스트 폴더에서만 발생하는 오탐이라면 관제 화면(`crex.viz`)의 `[+ 제외]` 버튼으로 [`.crexignore`](crexignore.md)에 등록하십시오.

3. **생성 코드 및 서드파티 경로 제외**:
   `vendor/`, `third_party/`, 자동 생성 파일(`*.pb.cc`, `*.g.cs` 등)이 리뷰 범위에 포함되어 있지 않은지 확인하고 `.crexignore`에 추가하십시오.

4. **교차 검증 모델 분리 확인**:
   생성과 검증에 동일한 모델을 쓰고 있다면 별도의 검증 모델 인스턴스를 분리해 교차 검증 효과를 확보하십시오.

---

## 정적분석이 안 돌아간다

```
INFO    crex.ground: [cppcheck] 건너뜀 — cppcheck 를 PATH 에서 찾을 수 없습니다
```

설치되지 않은 분석기는 자동으로 건너뜁니다. 설치했는데도 위 메시지가 나온다면 실행 파일 위치가 시스템 `PATH`에 등록되지 않은 것입니다. 새로 연 터미널에서 `clang-tidy --version`이나 `cppcheck --version`이 바로 실행되는지 확인하십시오.

```
INFO    crex.ground: [clang-tidy] 건너뜀 — 120초 내에 끝나지 않아 중단
```

대규모 C++ 프로젝트에서 `compile_commands.json` 없이 `clang-tidy`를 실행하거나 헤더 의존성이 매우 깊을 때 발생합니다. `python -m crex compiledb`로 컴파일 DB를 생성하거나 `grounding.timeout`을 늘리십시오.

```
INFO    crex.ground: [roslyn] 건너뜀 — 빌드할 .csproj/.sln 을 정하지 못했습니다
INFO    crex.ground: [roslyn] 건너뜀 — dotnet build 실패 (코드 1): ...
```

- 첫 번째 줄은 저장소에 프로젝트가 여러 개인데 솔루션 파일이 없거나 여러 개여서 대상을 하나로 특정하지 못한 경우입니다. `crex.json`의 `grounding.dotnet_project`에 빌드할 `.sln` 또는 `.csproj` 경로를 명시하십시오.
- 두 번째 줄은 빌드 자체가 실패한 경우입니다(폐쇄망에서는 주로 사내 NuGet 패키지 복원 실패가 원인입니다).

---

## 엉뚱한 저장소를 리뷰합니다

실행 로그(`-v`)의 첫 줄에서 현재 인식된 워크스페이스 경로와 그 설정 출처(`[...]`)를 확인하십시오.

```
INFO    crex.cli: 워크스페이스: D:\work\other [CREX_WORKSPACE]
```

대괄호 안의 표시가 해당 경로가 결정된 출처입니다. 우선순위는 다음과 같습니다.

1. `--workspace` (또는 `--repo`) 명령줄 인자
2. `CREX_WORKSPACE` (또는 `CREX_REPO`) 환경변수
3. `crex.json`의 `workspace` 설정
4. 현재 작업 디렉터리의 Git 루트

과거에 터미널 세션에 설정해 둔 `CREX_WORKSPACE`나 `CREX_REPO` 환경변수가 남아 있어 `crex.json` 설정보다 우선 적용되는 경우가 가장 흔합니다.

---

## `워크스페이스 경로가 없습니다`

```
설정 오류: 워크스페이스 경로가 없습니다: D:\work\myrepo
  .git 이 있는 프로젝트 루트를 지정하십시오. 예: --workspace D:\work\myrepo
```

경로에 오타가 있거나, `crex.json`에 상대 경로를 적으면서 현재 실행 위치를 기준으로 계산한 경우입니다. **`crex.json` 안의 상대 경로는 항상 해당 `crex.json` 파일이 위치한 폴더 기준**으로 해석됩니다. 혼동을 피하려면 절대 경로를 사용하거나 `python -m crex workspace <경로>` 명령으로 등록하십시오.

---

## `... 는 git 저장소가 아닙니다 (.git 이 없습니다)`

`review` 명령은 `git diff`를 기반으로 동작하므로 대상 워크스페이스에 `.git` 폴더가 있어야 합니다.

- 저장소 상위 폴더를 잘못 지정했다면 실제 프로젝트 폴더로 경로를 수정하십시오(저장소 *하위* 폴더를 지정한 경우에는 자동으로 루트까지 올라가므로 정상 동작합니다).
- 원래 Git으로 관리하지 않는 일반 소스 폴더를 점검하려면 `review` 대신 `python -m crex scan <경로>` 명령을 사용하십시오.

---

## `설정 오류: ... 알 수 없는 설정 키`

```
설정 오류: ReviewConfig 에 알 수 없는 설정 키: ['max_worker'].
```

`crex.json`의 키 이름에 오타가 있는 경우입니다. CREX는 설정 오타로 인해 사용자의 의도가 조용히 무시되는 일을 막기 위해 알 수 없는 키를 만나면 즉시 오류를 보고합니다.

---

## `ModuleNotFoundError: No module named 'fastmcp'`

코어 CLI와 관제 화면은 외부 패키지 없이 동작하지만, MCP 서버(`crex.mcp`)는 `fastmcp` 패키지가 필요합니다.

```bash
pip install -r requirements.txt
```

가상환경에 설치했는데도 Zed에서 이 오류가 발생한다면 Zed의 `settings.json`에 적힌 `command`가 가상환경이 아닌 시스템 기본 `python`을 가리키고 있는 것입니다. 가상환경 내 `python` 실행 파일의 절대 경로를 지정하십시오.

```json
{ "command": "/work/venv/bin/python", "args": ["-m", "crex.mcp"] }
```

---

## 여전히 원인을 찾기 어려울 때

`-v` 옵션으로 상세 실행 로그를 저장하고 JSON 리포트의 `rejected` 배열을 함께 확인하면 어느 단계에서 왜 지적이 걸러졌는지 정확히 추적할 수 있습니다. 또는 `python -m crex.viz`로 관제 화면을 띄워 각 청크의 프롬프트와 응답 원문을 직접 확인하십시오.

```bash
python -m crex review --staged --out reports/ -v 2> debug.log
```
