# 룰 작성법

CREX 를 팀에 맞게 길들이는 작업의 90%는 룰을 쓰는 일입니다. 모델을 바꾸거나
프롬프트를 손보는 것보다 여기서 얻는 게 훨씬 큽니다.

룰은 `rules/taxonomy.toml` 한 파일에 모여 있습니다. 여기가 유일한 원본이고,
프롬프트에 들어가는 룰 목록도 OCR 용 `rule.json` 도 전부 여기서 나옵니다.

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
std::move(x) 로 x 를 넘긴 뒤 같은 스코프에서 x 의 값을 읽거나 멤버를 호출하는 경우.
"""
counter = "재대입(x = ...)이나 clear() 이후의 사용은 정상이다. unique_ptr 의 == nullptr 비교도 정상."
```

| 필드 | 설명 |
|---|---|
| `id` | 안정적 식별자. `언어.케밥-케이스` 형식 |
| `language` | `cpp` / `csharp` / `python` / `any` |
| `dimension` | `code_defect` / `security_vulnerability` / `maintainability` / `performance` |
| `severity` | `high` / `medium` / `low` |
| `chunk_local` | 스니펫만 보고 판정 가능한가 |
| `title` | 개발자에게 보이는 이름 |
| `criteria` | 모델에게 주는 판정 기준 |
| `counter` | 지적하면 안 되는 경우 |

## `id` 는 절대 바꾸지 마세요

룰 ID 는 평가 리포트, 룰별 정밀도 통계, 플라이휠 기록을 잇는 조인 키입니다.
이름을 바꾸면 그 룰의 과거 성적이 통계에서 끊깁니다. 3개월치 데이터를 모아놓고
"이 룰은 정밀도가 낮으니 폐기하자"고 판단하려는 시점에 이름을 갈아치우면
그 근거가 사라집니다.

오타가 났거나 이름이 정말 마음에 안 들면, 새 ID 로 룰을 추가하고 옛 룰을 지우세요.
그게 정직합니다.

접두어는 `language` 필드와 맞춰야 합니다. `language = "any"` 인 룰은 `any.` 로
시작하세요. 안 그러면 언어별 통계가 뒤섞입니다.

## `chunk_local` — 오탐의 근원

이게 이 파일에서 제일 중요한 필드입니다.

`chunk_local = true` 는 "제시된 코드 조각만 보고 이 룰의 참거짓을 판정할 수 있다"는
뜻입니다. 기본 프로파일은 이 값이 참인 룰만 활성화합니다.

거짓인 룰을 켜면 어떻게 되는지 보겠습니다. 이런 룰을 만들었다고 칩시다.

```toml
# 나쁜 예 — 절대 이렇게 쓰지 마세요
id = "cpp.deprecated-api"
criteria = "사내에서 폐기 예정으로 표시된 API 를 호출하는 경우."
```

모델은 스니펫만 봅니다. 어떤 API 가 폐기 예정인지 알 방법이 없습니다.
그런데 룰이 주어졌으니 뭔가는 찾아내려 합니다. 그래서 **지어냅니다.**
그럴듯한 함수 이름을 하나 골라서 "이건 폐기 예정 API 입니다"라고 씁니다.

소형 모델은 "모르겠다"고 말하는 걸 어려워합니다. 판정 근거가 없는 룰을 주면
근거를 만들어냅니다. `chunk_local` 은 그런 룰을 애초에 차단하는 장치입니다.

판단이 애매하면 이렇게 자문해 보세요. **함수 하나만 인쇄해서 신입에게 보여줬을 때,
그 사람이 이 룰의 위반 여부를 답할 수 있는가?** 답할 수 없다면 `false` 입니다.

## `criteria` 쓰는 법

"무엇을 보면 참인가"를 구체적으로 씁니다. 모델이 코드에서 찾아야 할 패턴을
그대로 서술하세요.

나쁜 예:

```toml
criteria = "메모리 관리를 제대로 하지 않는 경우."
```

무엇을 봐야 하는지가 없습니다. 모델은 이 문장을 "메모리 관련해서 뭔가 지적해라"로
읽고, 아무 포인터나 잡아서 트집을 잡습니다.

좋은 예:

```toml
criteria = """
new 로 할당한 자원이 RAII 래퍼(unique_ptr/shared_ptr/lock_guard)에 담기지 않고,
delete 까지의 경로에 예외를 던질 수 있는 호출이나 early return 이 존재하는 경우.
fopen/malloc/CreateHandle 등 C API 도 동일하게 본다.
"""
```

찾아야 할 것이 명확합니다. `new`, 래핑 여부, 그 사이의 예외 경로.
모델이 코드를 훑으며 하나씩 확인할 수 있습니다.

구체적인 함수 이름과 타입 이름을 아끼지 마세요. `resize/push_back/insert/reserve`
처럼 나열하는 편이 "컨테이너를 변경하는 연산"보다 훨씬 잘 먹습니다.

## `counter` — 여기가 진짜 승부처

`counter` 는 "이런 경우는 지적하지 마라"를 적는 자리입니다.
FAR(오탐률) 관리의 상당 부분이 이 필드에서 이뤄집니다.

룰을 하나 켜고 골든셋을 돌려보면 오탐이 나옵니다. 그 오탐들을 읽어보면 대개
몇 가지 패턴으로 묶입니다. 그 패턴을 `counter` 에 적으면 다음 실행에서 사라집니다.

예를 들어 `csharp.idisposable-not-disposed` 를 켰더니 DI 컨테이너가 관리하는
객체를 자꾸 지적한다면:

```toml
counter = "필드에 저장하고 클래스가 IDisposable 을 구현해 소유권을 넘기면 정상이다. DI 컨테이너가 관리하는 경우도 정상."
```

`counter` 를 안 쓰면 룰의 정밀도가 40%대에 머뭅니다. 탐지 조건 자체는 맞는데
정상 패턴을 걸러내지 못해서 생기는 손실이라, `criteria` 를 고쳐서는 회복되지
않습니다.

**룰을 새로 쓸 때는 `criteria` 를 쓰는 시간만큼 `counter` 에도 쓰세요.**
처음 쓸 때는 떠오르는 예외를 두어 개 적어두고, 골든셋을 돌린 뒤 발견되는 대로
채워 넣습니다.

## `severity` 는 택소노미가 정합니다

모델이 심각도를 매기게 두면 자기가 찾은 걸 전부 `high` 로 올립니다.
그래서 CREX 는 모델이 보고한 심각도를 택소노미 값의 **상한 안에서만** 받습니다.
룰이 `medium` 인데 모델이 `high` 라고 하면 `medium` 이 됩니다.
`low` 라고 하면 `low` 를 받습니다 (스스로 낮추는 건 신뢰할 만합니다).

그러니 `severity` 는 신중하게 정하세요. 이 값이 실질적인 상한입니다.

기준을 대략 이렇게 잡으면 됩니다.

- **high** — 배포되면 사고가 납니다. 크래시, 데이터 손상, 보안 취약점.
- **medium** — 언젠가 물립니다. 리소스 누수, 경쟁 상태, 성능 함정.
- **low** — 알아두면 좋습니다. 가독성, 사소한 비효율.

`low` 는 아껴 쓰세요. 도입 초기에 `min_severity = "high"` 로 운영하는 걸
권하는데, 그러면 `low` 룰은 아무 일도 하지 않으면서 프롬프트 자리만 차지합니다.

## 룰은 한 번에 하나씩

이게 규율입니다. 지키기 지루하지만 지키세요.
2주 주기의 튜닝 루프 요약은 [워크플로](workflow.md#룰-튜닝-루프-2주)에 있습니다.

```bash
# 1. 룰 하나 추가
vim rules/taxonomy.toml

# 2. 문법 검증
python -m crex.rules

# 3. 골든셋 재측정
python eval/run_eval.py run --out reports/rule-42.json

# 4. 직전 리포트와 비교
python eval/run_eval.py compare reports/rule-41.json reports/rule-42.json
```

```
reports\rule-41.json  →  reports\rule-42.json

  + KBI (재현율)     58.0% →  61.5%  (+3.5%)
  - FAR (오탐률)     19.0% →  27.2%  (+8.2%)
      필터 기각률     51.0% →  54.1%  (+3.1%)
```

재현율이 3.5%p 올랐지만 오탐률이 8.2%p 뛰었습니다. **이 룰은 빼야 합니다.**
아니면 `counter` 를 보강해서 다시 재세요.

다섯 개를 한꺼번에 넣고 측정하면 어느 놈이 오탐을 만드는지 알 수 없습니다.
그러면 다섯 개를 다 빼거나 다 두는 선택밖에 남지 않습니다.

## 몇 개가 적당한가

지금 41개입니다. BitsAI-CR 은 219개를 씁니다. 하지만 그건 ByteDance 가 몇 년에
걸쳐 데이터 플라이휠을 돌리며 검증한 숫자입니다.

당장 늘릴 필요 없습니다. 청크당 활성 룰은 15개로 제한되어 있고, 이건 프롬프트를
짧게 유지하려는 의도적 상한입니다. 언어별 룰이 15개를 넘으면 심각도 순으로
잘려서 뒤쪽 룰은 아예 안 쓰입니다.

```bash
python -m crex.rules
```

```
택소노미 v0.1.0: 룰 41개
  cpp      전체 14개 → 청크당 활성 14개
  csharp   전체 15개 → 청크당 활성 15개
  python   전체 14개 → 청크당 활성 14개
  차원별: code_defect=30, maintainability=1, performance=4, security_vulnerability=6
```

"전체"와 "활성"이 벌어지기 시작하면 룰을 더 넣기 전에 기존 룰부터 정리해야
한다는 신호입니다. 정밀도 낮은 것부터 빼세요 — 평가 리포트가 순위를 매겨줍니다.

상한을 올리고 싶다면 `crex/rules.py` 의 `MAX_RULES_PER_CHUNK` 를 고치면 되지만,
올리기 전에 FAR 변화를 재세요. 룰이 많아질수록 프롬프트가 길어지고, 프롬프트가
길어질수록 소형 모델의 정밀도가 떨어집니다.

## 팀 전용 룰 추가하기

사내 코딩 규약 중에 코드만 보고 판정 가능한 것들이 있을 겁니다. 그런 건 좋은
룰 후보입니다.

```toml
[[rule]]
id = "cpp.no-raw-new-in-app-layer"
language = "cpp"
dimension = "maintainability"
severity = "medium"
chunk_local = true
title = "응용 계층에서 raw new 사용"
criteria = """
new 키워드를 직접 사용하는 경우. 사내 규약상 응용 계층에서는 팩토리
(MakeShared/MakeUnique)를 통해서만 객체를 만들어야 한다.
"""
counter = "플랫폼 추상화 계층(hal/, platform/)에서는 허용된다. placement new 도 제외."
```

경로에 따라 적용 여부가 갈리는 규약은 `counter` 로 처리하기보다 OCR 쪽
`rule.json` 의 `path` glob 을 쓰는 게 깔끔합니다. native 모드에서는 아직 경로별
룰 분기를 지원하지 않으니, 당분간은 `counter` 에 적어두세요.

## 문법 오류 잡기

```bash
python -m crex.rules
```

이 명령이 택소노미를 읽고 검증합니다. 걸리는 것들:

- TOML 문법 오류
- 필수 필드 누락 (`id`, `language`, `dimension`, `severity`, `title`, `criteria`)
- 잘못된 `dimension` / `severity` 값
- **ID 중복** — 이건 특히 조용히 넘어가면 위험합니다. 통계가 뒤섞이므로 오류로 막습니다.

```
오류: rules/taxonomy.toml 의 12번째 룰이 잘못되었다: 'severity' is not a valid Severity
```

## OCR 용 rule.json 생성

`alibaba/open-code-review` 를 병행 평가한다면 같은 택소노미에서 rule.json 을
뽑을 수 있습니다.

```bash
python -m crex.rules --out .opencodereview/rule.json --include "src/**"
```

언어별로 한 항목씩 만들고, 그 안에 룰을 번호 매겨 넣습니다. OCR 은 선언 순서대로
평가하고 first-match-wins 이므로 항목을 언어별로 하나씩만 두는 게 안전합니다.

`exclude` 는 기본값이 들어갑니다 — 생성 코드, 서드파티, 마이그레이션, `*.g.cs`,
`*_pb2.py` 같은 것들. 이것만으로도 오탐이 꽤 줄어듭니다. 팀 사정에 맞게
`--exclude` 로 덮어쓸 수 있습니다.
