# 동작 원리

이 문서는 CREX 를 고칠 사람을 위한 것입니다. 쓰기만 할 거면 안 읽어도 됩니다.

## 전체 흐름

```
git diff
   │
   ▼
parse_unified_diff()          crex/chunk.py
   │  FileDiff[] — hunk 별로 라인 상태와 번호를 붙여둔다
   ▼
Chunker.chunk_file()          crex/chunk.py
   │  ① diff/파일 정합성 검사
   │  ② hunk → 심볼 경계 확장 → 상한 적용
   │  ③ 겹치는 범위 병합
   │  ReviewChunk[]
   ▼
GroundingGate.collect()       crex/ground.py
   │  분석기 병렬 실행 (기본 6종) → StaticFinding[]
   │  attach() 로 라인 범위가 맞는 청크에 붙임
   ▼
RuleChecker.review()          crex/generate.py
   │  청크당 LLM 1회, enum 제약 스키마
   │  Finding[]
   ▼
ReviewFilter.filter()         crex/filter.py
   │  ① 결정론적 검사 (LLM 없이 기각)
   │  ② 살아남은 것만 교차 모델 재판정
   │  Finding[] + FilterVerdict[]
   ▼
report.write_all()            crex/report.py
```

---

## 청킹

### 왜 hunk 를 그대로 안 쓰나

바뀐 세 줄만 보여주면 모델이 맥락을 몰라서 헛소리를 합니다. 함수 시그니처도,
그 위에서 뭘 했는지도 모르니까요.

그렇다고 파일 전체를 주면 컨텍스트가 폭발하고 정밀도가 떨어집니다.
[설정 문서](configuration.md#입력-토큰-상한을-왜-8192-로-두나)에 적은 이유들입니다.

그래서 함수 경계까지만 넓힙니다. 리뷰어가 실제로 보는 단위도 그것이고요.

### 확장 상한

넓히다 보면 800줄짜리 신(神) 함수를 만납니다. 그래서 상한을 겁니다.

원본 hunk 의 4배(`expansion_limit`)를 넘으면 3배(`expansion_truncate`)로 잘라냅니다.
hunk 를 가운데 두고 위아래 대칭으로 자릅니다. 그 위에 절대 상한
(`absolute_max_lines`, 기본 400줄)이 하나 더 있습니다.

이 4배/3배 숫자는 BitsAI-CR 이 프로덕션에서 쓰는 값입니다. 근거가 있는 값이니
바꾸기 전에 골든셋으로 재보세요.

### 심볼 경계 찾기

`SymbolLocator` 가 tree-sitter 를 먼저 시도하고, 안 되면 휴리스틱으로 갑니다.

tree-sitter 경로는 대상 범위를 감싸는 노드 중 **가장 작은 것**을 고릅니다.
클래스보다 메서드가, 메서드보다 람다가 우선입니다. 언어별로 찾는 노드 타입은
`SymbolLocator._NODE_TYPES` 에 있습니다.

휴리스틱은 언어별로 다릅니다.

- **Python** — 인덴트를 추적합니다. 위로 올라가며 대상보다 얕은 `def`/`class` 를
  찾고, 아래로 인덴트가 그 수준 이하로 돌아올 때까지가 블록입니다.
- **C++/C#** — 중괄호 깊이를 셉니다. 문자열 리터럴과 `//` 주석 안의 중괄호는
  제거하고 셉니다. 시그니처가 여러 줄에 걸친 경우를 위해 여는 괄호 위쪽으로도
  좀 더 올라갑니다.

휴리스틱이 실패하면 `None` 을 돌려주고, 그러면 hunk 위아래로 6줄만 붙입니다.
전처리기 조건부 안에서 괄호가 불균형한 C++ 이 대표적인 실패 사례입니다.

### 라인 상태 주석

이게 청킹의 핵심 산출물입니다.

```
[unchanged @14]   void Grow(size_t extra) {
[deleted @15]     data_.resize(data_.size() + extra);
[added @15]     int* raw = &data_[0];
[added @16]     data_.resize(data_.size() + extra);
[added @17]     raw[0] = 42;
[unchanged @18]   }
```

모델이 "이게 몇 번째 줄이더라"를 세지 않아도 됩니다. 읽으면 바로 나옵니다.
라인 번호 환각의 상당수가 여기서 사라집니다.

삭제된 줄은 신 파일에 위치가 없으므로, 원래 있던 자리(다음 유지 라인 앞)에
끼워 넣고 구 파일 기준 번호를 붙입니다.

### 정합성 검사

`_verify_source()` 가 diff 가 주장하는 라인 내용과 실제 파일을 대조합니다.
하나라도 어긋나면 기본적으로 `DiffSourceMismatch` 를 던지고 그 파일을 건너뜁니다.

이 검사가 왜 필요한지는 개발 중에 직접 겪었습니다. 손으로 쓴 테스트 fixture 의
hunk 헤더가 한 줄 어긋나 있었는데, 청커는 아무 불평 없이 라인 번호가 전부 밀린
청크를 만들어냈습니다. 실제 운영에서 이런 일이 생기면 존재하지 않는 줄을
자신 있게 지적하게 됩니다. 라인 주석 체계의 존재 이유를 정면으로 무너뜨리는
실패 모드라서 기본값을 예외로 잡았습니다.

---

## 그라운딩

분석기는 `Analyzer` 추상 클래스를 구현합니다. `build_command()` 와 `parse()`
두 개만 채우면 됩니다.

```python
class Cppcheck(Analyzer):
    name = "cppcheck"
    executable = "cppcheck"
    languages = (Language.CPP,)

    def build_command(self, paths): ...
    def parse(self, stdout, stderr): ...
```

`available()` 은 `shutil.which()` 로 확인하고, 없으면 `AnalyzerResult(skipped=True)`
를 돌려줍니다. 타임아웃, `OSError`, 파서 예외도 전부 skipped 로 흡수합니다.
분석기 하나 때문에 리뷰가 멈추면 안 되니까요.

출력 파싱은 두 정규식이 대부분을 처리합니다.

- `_GNU_STYLE` — `path:line:col: severity: message [rule]` (clang-tidy, cppcheck, mypy)
- `_MSBUILD_STYLE` — `path(line,col): severity CODE: message` (dotnet build, roslynator)

JSON 을 내는 도구(ruff, bandit, semgrep)는 각자 파서를 씁니다.

`note` 레벨은 버립니다. 직전 경고의 부연이라서 그대로 두면 같은 결함이 두 번
지적됩니다.

`attach()` 는 경로를 접미사 매칭합니다. 분석기마다 절대경로와 상대경로를 섞어
내보내기 때문입니다.

### 프롬프트에서의 역할

정적분석 결과는 프롬프트에 이렇게 들어갑니다.

```
## 정적분석 도구 결과
- [cppcheck:invalidPointer] src/buffer.cpp:17 — Dereferencing pointer that may be invalid
```

그리고 시스템 프롬프트가 모델의 역할을 재정의합니다.

> 당신의 역할은 결함을 처음부터 찾는 것이 아니다.
> 1. 도구가 보고한 항목이 실제로 문제인지 코드를 보고 판단한다.
> 2. 도구가 구조적으로 잡을 수 없는 것만 추가로 지적한다.

"찾아라"에서 "검증하고 보완하라"로 바꾸는 것만으로 근거 없는 지적이 줄어듭니다.

---

## 생성

### enum 제약이 핵심

`build_findings_schema()` 가 만드는 스키마를 보세요.

```python
{
  "line": {"type": "integer", "enum": [8, 10]},        # 이 청크의 변경 라인만
  "rule_id": {"type": "string", "enum": ["cpp.use-after-move", ...]},
  "severity": {"type": "string", "enum": ["high", "medium", "low"]},
  ...
}
```

guided decoding 은 스키마를 어기는 토큰을 마스킹합니다. 모델이 라인 11을 내고
싶어도 **그 토큰을 생성할 수 없습니다.** 존재하지 않는 룰 ID 도 마찬가지입니다.

보통 구조화 출력은 "JSON 파싱 실패를 없앤다"는 용도로 쓰이는데, 여기서는
환각 자체를 봉쇄하는 데 씁니다. 사후 필터링이 아니라 생성 단계 차단입니다.

`maxItems` 로 청크당 지적 수도 제한합니다.

### 에이전트 루프를 안 쓰는 이유

OCR 같은 도구는 모델에게 `file_read`, `code_search` 같은 툴을 주고 자율 탐색을
시킵니다. 큰 모델에서는 잘 됩니다.

25~40B 급에서는 툴콜 실패와 컨텍스트 폭주가 함께 옵니다. 모델이 파일을 열고,
검색하고, 또 열다가 컨텍스트를 다 쓰고, 그 상태에서 지적을 만듭니다.
어디서 뭐가 잘못됐는지 추적하기도 어렵습니다.

CREX 는 고정 단계로 갑니다. 청크 하나, 프롬프트 하나, 지적 목록 하나.
느리지만 예측 가능하고 디버깅이 됩니다.

### 심각도 상한

`_severity_of()` 를 보세요. 모델이 보고한 심각도를 택소노미 값의 상한 안에서만
받습니다. 룰이 `medium` 인데 모델이 `high` 라면 `medium` 이 됩니다.

안 그러면 모델이 자기가 찾은 걸 전부 `high` 로 올립니다. 그러면 우선순위가
의미를 잃습니다.

---

## 검증

### 두 겹

**결정론적 검사** (`_check_deterministic`) 가 먼저입니다. LLM 을 부르지 않습니다.

- 지적이 속한 청크를 못 찾음 → `LINE_OUT_OF_RANGE`
- 라인이 청크 범위 밖 → `LINE_OUT_OF_RANGE`
- 라인이 변경되지 않은 줄 → `LINE_NOT_CHANGED`
- 같은 라인·같은 룰 중복 → `DUPLICATE`

공짜이고 100% 확실합니다. guided decoding 이 걸려 있으면 여기서 걸릴 일이
거의 없지만, 구버전 vLLM 이나 구조화 출력이 안 되는 환경을 위해 남겨둡니다.
두 겹으로 막는 겁니다.

**LLM 재판정** 은 살아남은 것만 받습니다. 검증 프롬프트에는 **지적과 해당 청크만**
넣습니다. 원본 리뷰의 문맥이나 다른 지적은 주지 않습니다. 생성 때의 맥락을
그대로 주면 검증자가 그 맥락에 끌려갑니다.

### Conclusion-First

응답 스키마의 프로퍼티 순서가 곧 생성 순서입니다.

```python
VERDICT_SCHEMA = {
    "properties": {
        "verdict": {...},        # ← 먼저
        "code_present": {...},
        "reason": {...},         # ← 나중
    },
}
```

결론 토큰을 먼저 뽑고 근거를 뒤에 붙입니다. BitsAI-CR 이 Reasoning-First(근거
먼저)와 비교한 뒤 프로덕션에 채택한 형태입니다. 정밀도 77.09%, 샘플당 1.7초.

**순서를 바꾸면 지연시간과 정확도가 함께 나빠집니다.** 스키마를 손볼 일이 있으면
이 순서를 유지하세요.

### code_present 가 verdict 보다 강하다

```python
if not response.get("code_present", True):
    return FilterVerdict(finding, False, ..., RejectReason.CODE_NOT_FOUND)
if response.get("verdict") != "yes":
    return FilterVerdict(finding, False, ..., RejectReason.VERDICT_NO)
```

검증자가 `verdict: "yes"` 를 냈더라도 `code_present: false` 면 기각합니다.
"지적이 묘사하는 코드가 스니펫에 없다"는 건 명백한 환각 신호이고, 그 상황에서
`yes` 가 나왔다는 건 검증자도 흔들렸다는 뜻입니다.

### 실패는 기각

검증 호출이 실패하면 통과시키지 않고 기각합니다(`FILTER_ERROR`).
정밀도가 우선이라는 원칙을 여기서도 지킵니다. 검증 엔드포인트가 죽으면
리뷰 결과가 비게 되는데, 그게 근거 없는 지적이 나가는 것보다 낫습니다.

로그의 기각률 한 줄로 바로 알아챌 수 있습니다.

---

## MCP 계층 분리

`crex/service.py` 와 `crex/mcp.py` 가 나뉜 이유가 있습니다.

- `service.py` — `ReviewService`. MCP 도구 5종의 실제 동작이 전부 여기 있습니다.
  **FastMCP 를 import 하지 않습니다.**
- `mcp.py` — FastMCP 바인딩만. 타입 힌트와 docstring 에서 도구 스키마가
  자동 생성되므로 실질적인 코드가 거의 없습니다.

이렇게 두면 FastMCP 없이도 리뷰 로직 전체를 테스트할 수 있습니다. 폐쇄망 반입
직후 무결성 확인이 pip install 을 전제하면 곤란하다는 게 핵심 이유입니다.
MCP 사양이 바뀌어도 `mcp.py` 만 고치면 됩니다.

`ReviewRequestError` 는 "사용자가 고칠 수 있는 오류"를 뜻합니다. `mcp.py` 가
이것만 `ToolError` 로 올려서 에이전트가 사용자에게 그대로 전달하게 하고,
나머지 예외는 로그를 남긴 뒤 일반 실패로 감쌉니다.

---

## 왜 외부 의존이 최소인가

폐쇄망에 wheel 하나 반입하는 게 생각보다 큰 일입니다. 보안 검토를 받아야 하고,
의존성의 의존성까지 따라가야 하고, 버전이 바뀔 때마다 반복해야 합니다.

그래서 코어는 stdlib 만 씁니다. FastMCP 는 MCP 사양이 계속 움직이는 표적이라
직접 구현하면 유지보수가 계속 붙어서 예외로 뒀고, GitPython 은 폴백을 남겨
필수가 아니게 했습니다.

- HTTP: `urllib.request` (httpx/requests 대신)
- 데이터 모델: `dataclasses` (pydantic 대신)
- 설정: `tomllib` (3.11+ stdlib)
- 병렬: `concurrent.futures.ThreadPoolExecutor`
- 테스트: 자체 러너 (pytest 대신)

tree-sitter 만 선택 의존성이고, 없으면 휴리스틱으로 갑니다.

토크나이저도 안 씁니다. `estimate_tokens()` 가 문자 수를 3으로 나누는 개산입니다.
코드는 산문보다 토큰 밀도가 높아서 보수적으로 잡았습니다. 정확한 토크나이저를
쓰려면 transformers 를 반입해야 하는데, 그럴 만한 가치가 없습니다 —
예산을 8192 로 잡고 개산이 20% 틀려도 실제로는 6500~10000 사이이고,
32K 컨텍스트에서는 아무 문제가 없습니다.

---

## 테스트

```bash
python tests/run_all.py
```

| 파일 | 다루는 것 |
|---|---|
| `test_chunk.py` | diff 파싱, 심볼 확장, 라인 주석, 정합성 검사 |
| `test_filter.py` | 결정론적 기각, LLM 판정, 보수적 실패 처리 |
| `test_ground.py` | 분석기 출력 파서, 설정별 활성화 |
| `test_pipeline.py` | 종단 — 실제 git 저장소 + 가짜 vLLM |
| `test_eval.py` | KBI/FAR 계산 |

`tests/fake_vllm.py` 가 `ThreadingHTTPServer` 로 `/v1/chat/completions` 를 흉내냅니다.
LLM 없이 HTTP 경로까지 실제로 태울 수 있습니다.

guided decoding 을 흉내내지는 않지만, **요청된 스키마의 enum 을 실제로 검사해서**
위반이 있으면 기록합니다. 스키마 제약이 의도대로 구성되는지를 검증하는 겁니다.

파이프라인 테스트는 임시 디렉터리에 git 저장소를 만들고 실제 `git diff` 를
뽑습니다. 손으로 쓴 diff 는 개발 중에 두 번이나 틀렸기 때문에 쓰지 않습니다.

---

## 손대기 전에 알아둘 것

**스키마의 프로퍼티 순서** — 검증 스키마에서 `verdict` 를 맨 앞에서 옮기지 마세요.

**`RejectReason` 은 늘리기만 하세요** — 기각 사유는 필터 튜닝의 근거 데이터입니다.
기존 값의 의미를 바꾸면 과거 리포트와 비교가 안 됩니다.

**룰 ID** — [룰 작성법](writing-rules.md#id-는-절대-바꾸지-마세요)에 적은
그대로입니다.

**`Finding` 에 필드를 추가할 때** — `to_dict()`/`from_dict()` 를 함께 고치세요.
평가 하네스가 이 형식으로 JSONL 을 읽습니다.
