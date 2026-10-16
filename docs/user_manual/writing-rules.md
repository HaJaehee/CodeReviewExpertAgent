# 룰 작성법

CREX를 사내 코드베이스에 맞게 고도화하는 작업의 90%는 룰 택소노미를 다듬는 일입니다. 모델을 교체하거나 시스템 프롬프트를 고치는 것보다 룰 하나를 정교하게 다듬는 것이 리뷰 품질에 훨씬 큰 영향을 미칩니다.

모든 룰은 `rules/taxonomy.toml` 파일 하나에서 관리합니다. 이 파일이 유일한 기준(Single Source of Truth)이며, 생성 모델 프롬프트에 주입되는 룰 목록과 OCR 호환용 `rule.json` 모두 이 파일에서 생성됩니다.

## 룰 하나의 생김새

```toml
[[rule]]
id = "cpp.use-after-move"
language = "cpp"
dimension = "code_defect"
severity = "high"
chunk_local = true
title = "std::move 이후 원본 객체 사용"
criteria = """
std::move(x)로 x를 넘긴 뒤 같은 스코프에서 x의 값을 읽거나 멤버 함수를 호출하는 경우.
"""
counter = "재대입(x = ...)이나 clear() 호출 이후에 사용하는 경우는 정상이다. unique_ptr의 == nullptr 비교도 정상이다."
```

| 필드 | 설명 |
|---|---|
| `id` | 고유 식별자 (`언어.케밥-케이스` 형식) |
| `language` | 대상 언어 (`cpp` / `csharp` / `python` / `any`) |
| `dimension` | 결함 분류 (`code_defect` / `security_vulnerability` / `maintainability` / `performance`) |
| `severity` | 최대 심각도 (`high` / `medium` / `low`) |
| `chunk_local` | 제시된 코드 청크와 AST 문맥만으로 판정 가능한지 여부 (`true` / `false`) |
| `title` | 리포트에 표시되는 한국어 제목 |
| `criteria` | 모델에게 제시하는 결함 판정 조건 |
| `counter` | 지적해서는 안 되는 정상 패턴 (오탐 방지 조건) |

## `id` 는 절대 바꾸지 마세요

룰 ID는 평가 리포트, 룰별 정밀도 통계, `.crexignore` 제외 규칙을 연결하는 기준 키입니다. 중간에 ID 이름을 바꾸면 그 룰의 과거 정밀도 이력이 통계에서 끊어집니다. 수개월간 데이터를 모아 정밀도가 낮은 룰을 정리하려는 시점에 ID가 바뀌면 판단 근거가 사라지게 됩니다.

오타가 있거나 이름을 꼭 바꿔야 한다면 기존 룰 ID를 수정하지 말고 새 ID로 룰을 만든 뒤 기존 룰을 삭제하십시오.

접두어는 반드시 `language` 필드와 일치해야 합니다. `language = "any"`인 범용 룰은 `any.`로 시작해야 언어별 통계와 제외 규칙 와일드카드(`cpp.*` 등)가 정확히 동작합니다.

## `chunk_local` — 오탐의 근원

`taxonomy.toml`에서 가장 중요한 필드입니다.

`chunk_local = true`는 "제시된 코드 청크(및 함께 주입되는 AST·호출 흐름 문맥)만 보고도 이 룰의 위반 여부를 확정할 수 있다"는 뜻입니다. CREX 기본 프로파일은 이 값이 `true`인 룰만 활성화합니다.

만약 청크 문맥만으로 알 수 없는 룰을 켜면 어떻게 되는지 살펴보겠습니다.

```toml
# 나쁜 예 — 이렇게 작성하지 마십시오
id = "cpp.deprecated-api"
criteria = "사내에서 폐기 예정으로 지정된 레거시 API를 호출하는 경우."
```

모델은 사내에서 어떤 함수가 폐기 예정인지 목록을 알지 못합니다. 그러나 적용할 룰 목록에 이 항목이 들어 있으면, 소형 모델은 "모르겠다"고 넘어가는 대신 **그럴듯해 보이는 함수 이름을 골라 폐기 예정 API라고 추측성 지적을 만들어냅니다.**

`chunk_local`은 이처럼 외부 배경지식이 있어야만 판정할 수 있는 룰을 사전에 걸러내는 장치입니다. 판단이 애매할 때는 다음과 같이 자문해 보십시오 — **"변경된 함수 코드와 피호출 함수 시그니처만 인쇄해서 동료 개발자에게 보여주었을 때, 이 룰의 위반 여부를 즉시 답할 수 있는가?"** 답할 수 없다면 `chunk_local = false`입니다.

## `criteria` 쓰는 법

"코드에서 어떤 패턴이 보일 때 결함으로 판정할 것인가"를 구체적으로 서술합니다.

나쁜 예:

```toml
criteria = "메모리 관리를 제대로 하지 않는 경우."
```

무엇을 확인해야 하는지 기준이 모호합니다. 모델은 이를 "포인터나 메모리 관련 코드가 보이면 무엇이든 지적하라"는 지시로 받아들여 무분별한 오탐을 생성합니다.

좋은 예:

```toml
criteria = """
new로 할당한 자원이 RAII 래퍼(unique_ptr/shared_ptr/lock_guard)에 담기지 않고,
delete에 도달하기 전 경로에 예외를 던질 수 있는 함수 호출이나 조기 반환(early return)이 존재하는 경우.
fopen/malloc/CreateHandle 등 C API도 동일하게 검사한다.
"""
```

확인해야 할 요소(`new` 할당, RAII 래퍼 여부, `delete` 이전의 조기 반환·예외 경로)가 명확하므로 모델이 AST와 코드 흐름을 따라 정확하게 점검할 수 있습니다. 추상적인 표현보다 `resize / push_back / insert / reserve`처럼 구체적인 함수명과 타입명을 나열하는 것이 훨씬 효과적입니다.

## `counter` — 여기가 진짜 승부처

`counter`는 **"다음과 같은 정상 패턴은 결함으로 지적하지 마라"**를 명시하는 필드입니다. 오탐률(FAR) 관리의 핵심이 바로 이 필드입니다.

새로운 룰을 추가하고 골든셋을 실행해 보면 초기에는 예상치 못한 오탐이 발생합니다. 발생한 오탐 사례들을 살펴보면 대개 몇 가지 전형적인 정상 패턴으로 묶이는데, 그 패턴을 `counter`에 구체적으로 적어 주면 다음 실행부터 해당 오탐이 사라집니다.

예를 들어 `csharp.idisposable-not-disposed` 룰을 켰더니 DI 컨테이너가 수명을 관리하는 객체까지 반복해서 지적한다면 다음과 같이 보강합니다.

```toml
counter = "객체를 클래스 필드에 저장하고 해당 클래스가 IDisposable을 구현해 소유권을 이전하는 경우는 정상이다. 생성자 주입 등 DI 컨테이너가 수명을 관리하는 객체도 지적하지 마라."
```

`counter`를 비워 두면 탐지 조건(`criteria`)이 아무리 정확해도 정상적인 예외 패턴을 걸러내지 못해 정밀도가 낮게 머뭅니다. **새 룰을 작성할 때는 `criteria`를 작성하는 시간만큼 `counter` 작성에도 공을 들이십시오.**

## `severity` 는 택소노미가 정합니다

모델에게 심각도를 자율적으로 매기게 두면 자신이 찾은 지적을 대부분 `high`로 부풀리는 경향이 있습니다. 따라서 CREX는 모델이 출력한 심각도를 택소노미에 정의된 `severity` **상한 이내로 클램핑(Clamping)**합니다.

- 택소노미 정의가 `medium`인 룰에 대해 모델이 `high`를 출력하면 → `medium`으로 조정됩니다.
- 모델이 `low`를 출력하면 → `low`를 그대로 인정합니다.

따라서 각 룰의 `severity`가 곧 해당 룰이 낼 수 있는 최대 심각도입니다. 기준은 다음과 같이 잡는 것이 좋습니다.

- **`high`** — 배포 시 즉각적인 장애를 유발하는 결함 (크래시, 널 역참조, 메모리 오염, 심각한 보안 취약점)
- **`medium`** — 특정 조건이나 장기 실행 시 문제를 일으키는 결함 (리소스 누수, 경쟁 상태, 예외 처리 누락, 주요 성능 저하)
- **`low`** — 당장 장애는 아니지만 개선이 권장되는 사항 (가독성 저하, 경미한 비효율)

## 룰은 한 번에 하나씩

룰을 추가하거나 수정할 때는 반드시 **한 번에 하나씩** 반영하고 골든셋으로 지표를 비교하십시오. 2주 주기의 정기 튜닝 절차는 [워크플로](workflow.md#룰-튜닝-루프-2주)에 정리되어 있습니다.

```bash
# 1. 룰 1개 추가 또는 수정
vim rules/taxonomy.toml

# 2. 택소노미 문법 및 중복 검증
python -m crex.rules

# 3. 골든셋 재측정
python eval/run_eval.py run --out reports/rule-42.json

# 4. 직전 측정 결과와 비교
python eval/run_eval.py compare reports/rule-41.json reports/rule-42.json
```

```
reports\rule-41.json  →  reports\rule-42.json

  + KBI (재현율)     58.0% →  61.5%  (+3.5%)
  - FAR (오탐률)     19.0% →  27.2%  (+8.2%)
      필터 기각률     51.0% →  54.1%  (+3.1%)
```

위 예시에서는 재현율(KBI)이 3.5%p 올랐지만 오탐률(FAR)이 8.2%p 급증했습니다. 이 경우 해당 룰의 `counter`를 보강해 다시 측정하거나, 개선되지 않으면 과감히 제거해야 합니다. 여러 룰을 한꺼번에 추가하면 어느 룰이 오탐률을 높였는지 분리해 낼 수 없습니다.

## 몇 개가 적당한가

현재 기본 택소노미에는 범용(`any`) 및 언어별 핵심 룰이 등록되어 있으며, 청크당 프롬프트에 동시에 주입되는 활성 룰 수는 토큰 예산과 모델 집중도를 유지하기 위해 상한(`MAX_RULES_PER_CHUNK`)을 두고 있습니다.

```bash
python -m crex.rules
```

룰 개수가 무조건 많다고 좋은 것이 아닙니다. 프롬프트에 주입되는 룰이 너무 많아지면 소형 모델의 주의력이 분산되어 개별 룰의 정밀도가 떨어집니다. 새 룰을 늘리기 전에 평가 리포트에서 정밀도가 낮은 기존 룰부터 정리하는 것을 권장합니다.

## 팀 전용 룰 추가하기

사내 코딩 표준 중 코드 문맥만으로 명확히 판별할 수 있는 규칙은 좋은 커스텀 룰 후보가 됩니다.

```toml
[[rule]]
id = "cpp.no-raw-new-in-app-layer"
language = "cpp"
dimension = "maintainability"
severity = "medium"
chunk_local = true
title = "응용 계층에서 raw new 직접 사용"
criteria = """
new 키워드로 객체를 직접 할당하는 경우. 사내 표준상 응용 계층에서는 팩토리 함수(MakeShared/MakeUnique)를 통해서만 객체를 생성해야 한다.
"""
counter = "하드웨어 추상화 계층(hal/, platform/) 파일에서는 허용된다. placement new도 제외한다."
```

특정 디렉터리에서만 이 룰을 끄고 싶다면 `counter`에 적는 것과 더불어 저장소 루트의 [`.crexignore`](crexignore.md)에 `hal/**:cpp.no-raw-new-in-app-layer`를 등록하면 프롬프트 단계에서부터 깔끔하게 제외됩니다.

## 문법 오류 잡기

```bash
python -m crex.rules
```

위 명령은 `rules/taxonomy.toml`을 읽어 다음 항목을 자동으로 검증합니다.

- TOML 문법 오류
- 필수 필드 누락 (`id`, `language`, `dimension`, `severity`, `title`, `criteria`)
- 허용되지 않은 `dimension` 또는 `severity` 값
- **중복된 `id`** (통계 왜곡을 막기 위해 중복 ID는 즉시 오류로 차단합니다)

## OCR 용 rule.json 생성

`alibaba/open-code-review`(OCR)를 병행 평가할 때는 동일한 택소노미에서 OCR 형식의 `rule.json`을 바로 생성할 수 있습니다.

```bash
python -m crex.rules --out .opencodereview/rule.json --include "src/**"
```

생성 코드, 서드파티 디렉터리, 마이그레이션 파일(`*.g.cs`, `*_pb2.py` 등)은 기본 제외 목록(`exclude`)에 자동으로 포함되며, `--exclude` 옵션으로 팀 환경에 맞게 조정할 수 있습니다.
