# 설정

`crex.toml` 한 파일로 전부 제어합니다. 찾는 순서는 이렇습니다.

1. `--config` 또는 `CREX_CONFIG` 로 직접 지정한 파일
2. 워크스페이스를 `--workspace` 나 환경변수로 지정했다면 `<워크스페이스>/crex.toml`
3. 현재 디렉터리에서 위로 올라가며 `crex.toml` → `.crex.toml`
4. 그래도 없으면 기본값

2번이 있는 이유는 저장소마다 `compile_commands_dir` 과 `dotnet_project` 가 다르기
때문입니다. CREX 설치본 하나로 여러 저장소를 볼 때, 설정도 저장소를 따라와야
합니다. 워크스페이스 *안*만 봅니다 — 없다고 위로 올라가서 남의 설정을 주워오지
않습니다.

설정 키에 오타가 나면 조용히 무시하지 않고 오류를 냅니다. `max_worker` 라고
쓰면 알려줍니다.

```
설정 오류: ReviewConfig 에 알 수 없는 설정 키: ['max_worker']. 사용 가능한 키: [...]
```

최상위 키도 마찬가지입니다. `workspase` 라고 쓰면 오류가 납니다 — 조용히
무시하면 리뷰 대상 저장소가 말없이 바뀌므로 가장 위험한 오타입니다.

---

## `workspace` — 리뷰 대상 저장소

```toml
workspace = "D:/work/myrepo"
```

**CREX 를 리뷰 대상 저장소 안에 둘 필요가 없습니다.** 설치본은 한 자리에 두고
(폐쇄망에서는 반입본이 하나여야 무결성 관리가 됩니다) 대상만 가리킵니다.

```
D:\tools\crex\        ← 여기서 실행. 작업 디렉터리이자 모듈 위치
D:\work\myrepo\.git   ← 여기를 리뷰
```

```bash
cd D:\tools\crex
python -m crex review --workspace D:\work\myrepo --staged
```

`crex.toml` 에 적어두는 것은 손으로 열지 않고 명령으로 할 수 있습니다.

```bash
python -m crex workspace                    # 지금 무엇을 보고 있나
python -m crex workspace D:\work\myrepo     # crex.toml 에 고정
python -m crex workspace --clear            # 고정 해제
```

경로를 검증한 뒤에 적습니다 — 없는 경로가 설정 파일에 박히면 다음 실행이 죽습니다.
주석은 그대로 두고 그 한 줄만 갈아 끼웁니다.

정해지는 순서는 위에서 아래로, 먼저 정해지면 아래는 보지 않습니다.

| 순위 | 출처 |
|---|---|
| 1 | `--workspace D:\work\myrepo` (`--repo` 는 예전 이름, 계속 받습니다) |
| 2 | 환경변수 `CREX_WORKSPACE` (이전 이름 `CREX_REPO` 도 받습니다) |
| 3 | `crex.toml` 의 `workspace` |
| 4 | 현재 디렉터리에서 git 루트 탐색 (예전 동작) |

몇 가지 규칙:

- **상대경로는 이 설정 파일이 있는 디렉터리 기준**입니다. 실행 위치 기준으로 두면
  어디서 실행했느냐에 따라 대상이 바뀝니다. 반입 번들을 통째로 옮겨도 설정이
  그대로 살아 있어야 합니다.
- **하위 디렉터리를 지정하면 저장소 루트로 올라갑니다.** `D:\work\myrepo\src` 를
  줘도 `D:\work\myrepo` 로 맞춥니다. 리포트의 모든 경로가 저장소 루트 기준이라
  기준점이 흔들리면 정적분석 결과와 청크가 어긋납니다.
- **`%VAR%` 와 `$VAR`, `~` 를 풉니다.** `%USERPROFILE%/work/myrepo` 처럼 쓸 수 있습니다.
- 경로가 없으면 그 자리에서 오류를 냅니다. `.git` 이 없으면 경고만 하고 계속
  진행합니다 — `scan` 은 git 없이도 동작하지만 `review` 는 거부합니다.
- 리포트 기본 위치는 `<워크스페이스>/reports` 입니다. `--out` 이나
  `CREX_REPORTS` 로 옮길 수 있습니다.

MCP 서버와 관제 화면도 같은 규칙을 씁니다. 세 진입점이 서로 다른 저장소를 보고
있으면 관제 화면의 의미가 없어지므로 `crex/workspace.py` 한 곳에 모아 뒀습니다.

### 돌고 있는 중에 바꾸기

| 어디서 | 어떻게 | 지속 |
|---|---|---|
| 터미널 | `python -m crex workspace <경로>` | `crex.toml` 에 남습니다 |
| 관제 화면 | 왼쪽 "워크스페이스" 옆 **변경** | 서버가 사는 동안만 |
| Zed 에이전트 | `set_workspace` 도구 | MCP 서버가 사는 동안만 |

화면과 MCP 쪽은 설정 파일을 고치지 않습니다. 대화 한 번이나 클릭 한 번이 다음
사람의 실행 대상까지 바꿔 놓으면 안 되기 때문입니다. 영구히 바꾸려면 명령을
쓰세요.

셋 다 처음 정할 때와 **똑같은 검증**을 거칩니다. 없는 경로는 거부하고, 하위
폴더는 저장소 루트로 올리고, 새 워크스페이스 안의 `crex.toml` 을 따라갑니다.
`--config` 나 `--out` 으로 고정해 둔 것은 대상을 바꿔도 그대로 유지됩니다.

---

## `[llm.generator]` — 지적을 만드는 모델

```toml
[llm.generator]
base_url = "http://vllm-qwen:8000/v1"
model = "Qwen3.6-27B"
api_key = "EMPTY"
temperature = 0.0
max_output_tokens = 900
max_input_tokens = 8192
timeout = 120.0
max_retries = 3
structured_output_mode = "auto"
```

`base_url` 은 `/v1` 까지 포함합니다. 뒤에 `/chat/completions` 를 붙이지 마세요.

`model` 은 vLLM 을 띄울 때 `--served-model-name` 으로 준 이름과 정확히 같아야
합니다. 안 맞으면 404가 나고 재시도 없이 즉시 실패합니다 (재시도해도 같으니까요).

`api_key` 는 vLLM 이 `--api-key` 없이 떴다면 아무 값이나 됩니다. 헤더 자체는
보내야 해서 `"EMPTY"` 를 기본값으로 둡니다.

`temperature` 는 0.0 에서 올리지 마세요. 같은 코드를 두 번 리뷰했는데 다른
결과가 나오면 골든셋 평가가 무의미해집니다. 다양성이 필요한 작업이 아닙니다.

### 입력 토큰 상한을 왜 8192 로 두나

Qwen3.6은 256K, Gemma4는 256K 컨텍스트를 지원합니다. 그런데 8192로 씁니다.

컨텍스트를 늘리면 정밀도가 떨어지기 때문입니다. ASE 2025 연구에서 검색 예시를
top-1 → top-3 → top-5로 늘리자 BLEU-4가 12.32 → 11.76 → 10.81로 단조 감소했습니다.
중복되거나 상충하는 신호가 섞이면 모델이 흔들립니다. 리뷰에서도 같은 일이
벌어집니다 — 함수 하나만 보여주면 정확히 짚던 모델이, 파일 전체를 주면
엉뚱한 데를 지적하기 시작합니다.

속도 문제도 있습니다. Qwen3.6은 32K에서 128K로 가면 처리량이 26 → 9 tok/s로
무너집니다. Gemma4가 96 → 65로 완만한 편이지만 역시 떨어집니다.

올리고 싶다면 [평가](evaluation.md)를 먼저 세팅하고 FAR 변화를 재세요.
근거 없이 올리면 나빠졌다는 걸 모른 채로 나빠집니다.

프롬프트가 상한을 넘으면 코드 가운데를 잘라냅니다 (앞뒤를 남깁니다 — 함수
시그니처와 반환부가 둘 다 중요하므로). 잘리는 일이 잦다면 상한을 올리기보다
`chunking.absolute_max_lines` 를 줄이는 쪽이 낫습니다.

### `structured_output_mode`

vLLM 버전마다 구조화 출력을 받는 필드가 다릅니다.

| 값 | 동작 |
|---|---|
| `"auto"` | 되는 쪽을 스스로 찾습니다 (기본값, 권장) |
| `"response_format"` | 최신 vLLM 방식으로 고정 |
| `"guided_json"` | 구버전 vLLM 방식으로 고정 |

`"auto"` 는 첫 호출에서 다음 순서로 시도하고, 성공한 조합을 기억해 이후 호출에
바로 씁니다. 그래서 서버 버전을 몰라도 그대로 두면 됩니다.

1. `response_format` + 원본 스키마
2. `guided_json` + 원본 스키마
3. `response_format` + 완화 스키마
4. `guided_json` + 완화 스키마

**완화 스키마**는 `maxLength` · `maxItems` 처럼 xgrammar 백엔드가 컴파일하지
못하는 키워드를 뺀 것입니다. `enum` 은 절대 빼지 않습니다 — 룰 ID와 라인 번호를
묶어두는 그 두 개가 환각을 막는 장치이고, 길이 상한은 프롬프트 위생에 가깝기
때문입니다. 완화가 쓰이면 로그에 경고가 남으니 백엔드를 점검하세요.

넷 다 실패하면 그 엔드포인트로는 리뷰가 불가능합니다. 이때 CREX 는 조용히
0건을 내지 않고 오류로 처리합니다 ([종료 코드 3](getting-started.md#종료-코드)).

이 설정은 단순한 호환성 문제가 아닙니다. 구조화 출력이 안 걸리면 룰 ID와 라인
번호에 대한 enum 제약이 사라지고, 그러면 환각 방어 네 겹 중 두 겹이 날아갑니다.
남은 두 겹(결정론적 검사, LLM 재판정)이 받아내긴 하지만 오탐이 눈에 띄게 늘어납니다.
`python -m crex doctor` 가 이걸 직접 확인해 줍니다.

### `guided_decoding_backend`

`guided_json` 경로에서 함께 보낼 백엔드 이름입니다. 기본값은 빈 문자열이고,
비어 있으면 아예 보내지 않습니다.

최신 vLLM 은 이 필드를 모르는 키로 보고 400 을 돌려줍니다. 구버전에서 백엔드를
명시해야 하는 경우에만 `"xgrammar"` 처럼 적으세요.

### `[llm.generator.extra_body]`

요청 본문에 그대로 합쳐지는 추가 필드입니다. Qwen3.x 계열의 추론 모드를 끌 때
씁니다.

```toml
[llm.generator.extra_body]
chat_template_kwargs = { enable_thinking = false }
```

리뷰는 고정 단계로 돌아가므로 모델이 스스로 사고 과정을 늘어놓을 필요가 없고,
켜두면 지연시간만 몇 배가 됩니다.

---

## `[llm.verifier]` — 지적을 재판정하는 모델

```toml
[llm.verifier]
base_url = "http://vllm-gemma:8000/v1"
model = "gemma-4-26b-it"
max_input_tokens = 4096
timeout = 60.0
```

**생성과 다른 모델을 쓰는 게 요점입니다.** 같은 모델에게 자기가 만든 지적을
검증시키면 자기 환각을 잘 못 잡습니다. 자기가 방금 쓴 문장이니까요.

검증은 부하가 훨씬 가볍습니다. 입력은 청크 하나뿐이고 출력은 사실상 토큰
하나(yes/no)에 짧은 근거입니다. GPU 를 적게 배분해도 됩니다. `max_output_tokens`
는 200을 넘지 않도록 잘립니다 — 더 크게 적어도 200이 되고, 더 작게 적으면 그
값이 그대로 쓰입니다.

블록을 통째로 생략하면 생성 쪽 설정을 그대로 씁니다. 인스턴스가 하나뿐인
환경에서도 돌아가야 하니까요. 대신 검증의 효과가 줄어듭니다.

---

## `[review]`

```toml
[review]
mode = "native"
max_findings_per_chunk = 5
max_workers = 4
require_changed_line = true
min_severity = "low"
```

**`mode`** — 현재 `"native"` 만 구현되어 있습니다. `"ocr"` 를 넣으면 조용히
무시하지 않고 오류를 냅니다. OCR 위임은 Phase 1 에서 실제 바이너리의 출력
스키마를 확인한 뒤 붙일 예정입니다.

**`max_findings_per_chunk`** — 청크 하나에서 받을 지적 개수 상한입니다. 스키마의
`maxItems` 로 강제되므로 모델이 더 낼 수가 없습니다. 5를 넘겨 늘리는 건 권하지
않습니다. 함수 하나에 진짜 결함이 다섯 개 넘게 있다면 그건 리뷰가 아니라 재작성
대상이고, 실제로는 모델이 지적을 흩뿌리기 시작한 신호일 가능성이 높습니다.

**`max_workers`** — 동시 LLM 호출 수입니다. vLLM 은 배치를 잘 처리하므로 올리면
대체로 빨라지지만, 다른 팀과 GPU 를 공유한다면 4 정도가 예의입니다. 429나 503이
자주 보이면 낮추세요.

**`require_changed_line`** — diff 리뷰에서 변경된 라인만 지적 대상으로 삼습니다.
켜두는 게 맞습니다. 끄면 "이 함수 전체가 마음에 안 든다"는 식의 지적이 쏟아지는데,
MR 리뷰에서 남이 예전에 쓴 코드를 지적하는 건 대체로 무례하고 대체로 무시됩니다.
`scan` 명령에서는 자동으로 꺼집니다.

**`min_severity`** — 이 심각도 미만은 리포트에서 숨깁니다. 기각과는 다릅니다.
유효하다고 판정된 지적을 "지금은 보고 싶지 않다"고 감추는 것이고, JSON 에도
남지 않습니다.

도입 초기에 `"high"` 로 시작하는 걸 권합니다. 처음 두어 주는 명백한 버그만
보여주면서 신뢰를 쌓고, 사람들이 "이 도구 말이 맞네"라고 느끼기 시작하면
`"medium"` 으로 낮추세요. 처음부터 전부 보여주면 노이즈에 묻혀서 진짜 지적까지
같이 무시당합니다.

---

## `[grounding]` — 정적분석

```toml
[grounding]
enabled = true
timeout = 120.0
compile_commands_dir = "build"
dotnet_project = "src/App.sln"
# semgrep_config = "/opt/semgrep-rules"
# analyzers = ["cppcheck", "ruff"]
```

LLM 을 부르기 전에 결정론적 도구를 돌려서 사실을 확보합니다. 그 결과를 프롬프트에
넣고 모델에게 이렇게 시킵니다 — "결함을 찾아라"가 아니라 "이 도구 결과가 진짜인지
보고, 도구가 원리적으로 못 잡는 로직 문제만 추가해라".

설치되지 않은 도구는 조용히 건너뜁니다. 장비마다 상황이 다른 폐쇄망에서 하나
없다고 파이프라인이 멈추면 안 되니까요. 무엇이 돌았는지는 `-v` 를 붙이면 로그에
나옵니다.

**`compile_commands_dir`** — C++ 을 리뷰한다면 이게 제일 중요합니다. 없으면
clang-tidy 가 헤더를 못 찾아서 절반쯤 눈을 감고 분석합니다. **직접 적을 필요는
없습니다** — 이 명령이 만들고 여기에 적어줍니다.

```bash
python -m crex compiledb
```

CMake 든 Visual Studio 든 알아서 처리합니다. 무슨 일이 일어나는지와 손으로
만드는 방법은 [정적분석 도구 문서](analyzers.md#compile_commandsjson-이-없으면-반쯤-눈을-감습니다)에
있습니다.

값은 파일이 아니라 **디렉터리**이고, 상대 경로는 리뷰 대상 저장소 루트 기준입니다.
어렵다면 clang-tidy 를 포기하고 cppcheck 만 쓰는 것도 나쁘지 않습니다 — cppcheck 는
컴파일 DB 없이도 돌고 오탐률이 매우 낮습니다.

**`clang_tidy_checks`** — 체크 목록입니다. **비워두는 것이 기본이자 권장입니다.**

| 상황 | 동작 |
|---|---|
| 비움 + 프로젝트에 `.clang-tidy` 있음 | 팀 설정을 그대로 씁니다 |
| 비움 + `.clang-tidy` 없음 | 버그 탐지 위주 기본값 (`bugprone-*`, `cert-*`, `clang-analyzer-*` 등) |
| 값을 지정 | 지정한 것이 이깁니다 |

명령줄 `--checks` 는 `.clang-tidy` 의 `Checks` 를 **덮어씁니다.** 그래서 팀 설정이
있으면 아예 넘기지 않습니다. 사내 코딩 룰이 거기 들어 있는데 덮어쓰면 룰이 통째로
무시되고, "룰을 넣었는데 아무 일도 안 일어나요" 가 됩니다.

**`dotnet_project`** — C# 은 파일 단위 분석이 안 되므로 프로젝트를 빌드하고 경고를
수확합니다. 팀이 이미 쓰는 `.editorconfig` 와 `Directory.Build.props` 설정이 그대로
적용되므로, 별도 룰 관리 없이 사내 표준과 자동으로 맞습니다.

빌드가 오래 걸리는 큰 솔루션이라면 `timeout` 을 넉넉히 주거나 `analyzers` 에서
`roslyn` 을 빼세요. 리뷰 한 번에 2분씩 빌드하는 건 감당하기 어렵습니다.

**`semgrep_config`** — 폐쇄망에서 `"auto"` 는 동작하지 않습니다. 룰을 인터넷에서
받아오기 때문입니다. 룰팩을 미리 반입하고 로컬 경로를 지정해야 합니다.
지정하지 않으면 semgrep 은 아예 실행되지 않습니다.

**`analyzers`** — 특정 도구만 쓰고 싶을 때 이름을 나열합니다. 비우면 기본 6종이
전부 돕니다.

| 이름 | 언어 | 기본 실행 |
|---|---|---|
| `clang-tidy` | C++ | 예 |
| `cppcheck` | C++ | 예 |
| `roslyn` | C# | 예 |
| `ruff` | Python | 예 |
| `mypy` | Python | 예 |
| `bandit` | Python | 예 |
| `roslynator` | C# | 아니오 — 이름을 적어야 켜집니다 |
| `semgrep` | 전체 | 아니오 — `semgrep_config` 가 있으면 켜집니다 |

어디서 내려받는지, 라이선스가 무엇인지, 폐쇄망에 어떻게 반입하는지는
[정적분석 도구](analyzers.md)에 있습니다.

`roslynator` 는 별도 설치가 필요하고 `dotnet build` 와 겹치는 룰이 많아서
기본에서 뺐습니다. 둘 다 쓰면 같은 결함이 두 번 보고됩니다.

이름을 틀리면 오류가 납니다. 조용히 넘어가면 그 도구만 빠지는 게 아니라
목록이 비어 있지 않은 탓에 **기본 분석기까지 전부 걸러지기** 때문입니다.

```
설정 오류: grounding.analyzers 에 알 수 없는 분석기: ['ruf'].
사용 가능: ['bandit', 'clang-tidy', 'cppcheck', 'mypy', 'roslyn', 'roslynator', 'ruff', 'semgrep']
```

---

## `[chunking]`

```toml
[chunking]
expansion_limit = 4.0
expansion_truncate = 3.0
absolute_max_lines = 400
on_mismatch = "raise"
```

diff 의 hunk 를 함수 경계까지 넓힌 다음, 너무 커지면 잘라냅니다.
바뀐 세 줄만 보여주면 모델이 맥락을 몰라 헛소리를 하고, 함수 전체를 보여주면
그게 800줄짜리 신(神) 함수일 때 컨텍스트가 통째로 날아갑니다. 그 사이를 잡는
장치입니다.

**`expansion_limit`** / **`expansion_truncate`** — 확장 결과가 원본 hunk 의 4배를
넘으면 3배로 잘라냅니다. hunk 를 가운데 두고 위아래 대칭으로 자릅니다.

**`absolute_max_lines`** — 배수와 무관한 절대 상한입니다. 거대 함수에 대한
안전판입니다. `scan` 모드에서는 이 값이 창 크기가 됩니다.

**`on_mismatch`** — diff 와 디스크 파일이 어긋날 때의 동작입니다.

diff 를 만든 뒤 작업 트리가 바뀌면 라인 번호가 전부 밀립니다. 그 상태로 리뷰하면
존재하지 않는 줄을 자신 있게 지적하게 되고, 이건 라인 주석 체계의 존재 이유를
정면으로 무너뜨립니다. 기본값 `"raise"` 는 해당 파일을 건너뜁니다.

`"warn"` 은 경고만 남기고 진행합니다. 디버깅할 때 말고는 쓸 일이 없습니다.
`"ignore"` 는 검사조차 안 합니다. 쓰지 마세요.

이 오류가 뜨면 대개 다음 중 하나입니다.

- diff 를 파일로 저장해 두고 나중에 돌렸는데 그 사이 코드가 바뀐 경우
- `--from`/`--to` 로 과거 커밋을 비교하면서 작업 트리는 현재 상태인 경우

두 번째가 특히 흔합니다. 과거 시점을 리뷰하려면 그 커밋을 체크아웃한 사본을
만들고 `--workspace` 로 그쪽을 가리키세요.

---

## `taxonomy_path`

```toml
taxonomy_path = "rules/taxonomy.toml"
```

생략하면 패키지에 들어 있는 `rules/taxonomy.toml` 을 씁니다. 팀별로 다른 룰셋을
쓰고 싶을 때만 지정하세요.

---

## 설정 없이 돌리면

`crex.toml` 이 없어도 실행은 됩니다. 엔드포인트가 `http://localhost:8000/v1`,
모델이 `Qwen3.6-27B` 로 잡히고, 나머지는 위에 적힌 기본값입니다.
`doctor` 가 `설정 파일: (없음 — 기본값 사용 중)` 이라고 알려줍니다.

실제로 쓸 때는 설정 파일을 두세요. 실행 로그 첫 줄에 설정 요약이 남으므로,
나중에 "그때 어떤 설정으로 돌린 거지"를 추적할 수 있습니다.

```
INFO    crex.cli: 워크스페이스: D:\work\myrepo [--workspace]
INFO    crex.cli: 설정: 모드=native 생성=Qwen3.6-27B@http://vllm-qwen:8000/v1 ...
```

워크스페이스 줄의 대괄호가 그 값을 어디서 얻었는지입니다 — `--workspace`,
`CREX_WORKSPACE`, `crex.toml 의 workspace`, `현재 디렉터리` 중 하나입니다.
"왜 엉뚱한 저장소를 봤지"를 추적할 때 이 한 줄이면 됩니다.
