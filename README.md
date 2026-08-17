# CREX — 폐쇄망 sLLM 코드리뷰 파이프라인

**C**ode **R**eview **EX**pert Agent. 명령과 패키지는 소문자 `crex` 를 쓴다.

25~40B급 로컬 모델(Qwen3.6, Gemma4)로 C++/C#/Python 코드를 리뷰한다.
설계의 전부는 하나의 목표로 수렴한다 — **환각을 구조적으로 막는 것**.

오탐이 많은 리뷰 도구는 3주 안에 무시당한다. 그래서 재현율보다 정밀도를 택했고,
그 대가로 일부 결함은 놓친다. 의도된 트레이드오프다.

```
git diff
   │
   ├─▶ 청킹          chunk → tree-sitter 함수 경계 확장(4배 상한) → 라인 상태 주석
   │
   ├─▶ 그라운딩      clang-tidy / cppcheck / Roslyn / ruff / mypy / bandit / semgrep
   │                 결정론적 사실을 먼저 확보한다
   │
   ├─▶ RuleChecker   Qwen3.6-27B · guided decoding 으로 룰 ID·라인 번호를 enum 강제
   │
   ├─▶ ReviewFilter  Gemma4 26B · 결정론적 검사 + Conclusion-First 재판정
   │                 (교차 모델 검증)
   │
   └─▶ 리포트        Markdown / SARIF / JSON
```

---

## 환각을 막는 네 겹

| 층 | 장치 | 막는 것 |
|---|---|---|
| 1. 입력 | 라인마다 `[added @142]` 주석 | 모델이 라인 번호를 추론할 필요 자체를 없앤다 |
| 2. 생성 | guided decoding 의 `enum` 제약 | **라인 번호·룰 ID 날조가 생성 단계에서 불가능해진다** |
| 3. 검증(결정론) | 라인 범위·변경 여부 검사 | LLM 을 부르지 않고 즉시 기각. 공짜이고 100% 확실 |
| 4. 검증(LLM) | 다른 모델의 Yes/No 재판정 | 코드에 근거가 없는 지적 |

2층이 이 구현의 핵심이다. vLLM 의 스키마 강제는 보통 "JSON 파싱 실패 방지"에
쓰이지만, 여기서는 **허용된 라인 번호 집합**과 **택소노미에 존재하는 룰 ID**만
enum 에 넣는다. 모델은 그 밖의 토큰을 생성할 수 없다. 사후 필터링이 아니라
생성 자체가 봉쇄된다.

3·4층은 guided decoding 이 없는 환경(구버전 vLLM)을 위해 그대로 남겨둔다.

---

## 워크플로 (Zed / MCP)

개발자는 Zed 에이전트 패널에서 부른다. 터미널로 나갈 일이 없다.

```
코드 작성 → git add → "변경사항 리뷰해줘" → 대응 → commit → "main 이랑 비교해서 리뷰해줘"
                          review_staged                        review_diff
```

| 이렇게 말하면 | 불리는 도구 |
|---|---|
| "변경사항 리뷰해줘" | `review_staged` — 커밋 직전, 가장 자주 |
| "커밋 안 한 거 전부" | `review_working_tree` |
| "main 이랑 비교해서" | `review_diff` — 분기점을 자동으로 잡는다 |
| "이 파일 전체 봐줘" | `review_file` |
| "이 폴더 감사해줘" | `review_directory` — 40개 초과 시 거부 |
| "파서 쪽 변경만" | `paths=["src/parser"]` — 앞의 셋에 붙는다 |

도구는 **요약만** 돌려주고 전체 리포트는 파일로 저장된다. 도구 반환값이 그대로
에이전트 컨텍스트로 들어가기 때문이다.

### 지적을 받았을 때

```
맞다   → 고친다
애매   → 코드 재확인 → 여전히 애매하면 무시해도 된다
        (CREX 는 확신 없으면 침묵한다. 애매한 지적이 나온 것 자체가 신호다)
틀렸다 → 룰 ID 를 담당자에게 전달 → counter 보강 → 팀 전체에서 사라진다
```

`"리뷰가 이상해요"` 는 조치할 수 없지만
`"csharp.idisposable-not-disposed 가 DI 객체를 자꾸 지적해요"` 는 즉시 조치된다.

에이전트가 도구를 안 부르고 직접 리뷰해 버리는 게 가장 흔한 문제다.
[`AGENTS.md`](AGENTS.md) 를 리뷰 대상 저장소 루트에 복사하면 대부분 해결된다.

전체 흐름과 담당자 작업(골든셋·룰 튜닝)은 [워크플로 문서](docs/workflow.md)에 있다.
설정은 [Zed 연동](docs/operations.md#zed-연동-mcp)을 보라.

---

## 문서

자세한 사용법은 [`docs/`](docs/index.md) 에 있습니다.

| | |
|---|---|
| [시작하기](docs/getting-started.md) | 설치, 첫 실행, 결과 읽는 법 |
| [워크플로](docs/workflow.md) | Zed 에서 리뷰 부르기, 지적 받았을 때 |
| [설정](docs/configuration.md) | `crex.toml` 전체 항목 |
| [룰 작성법](docs/writing-rules.md) | 오탐을 늘리지 않고 룰을 추가하려면 |
| [평가와 튜닝](docs/evaluation.md) | 골든셋, KBI/FAR, 룰 폐기 |
| [관제 화면](docs/visualizer.md) | 두 모델의 프롬프트·응답·판정을 웹에서 보기 |
| [반입](docs/transfer.md) | Python 런타임까지 담은 번들 만들기·검증 |
| [운영](docs/operations.md) | vLLM 기동, Zed 연동, 일상 운영 |
| [문제 해결](docs/troubleshooting.md) | 증상별 원인과 조치 |
| [동작 원리](docs/internals.md) | 내부 구조와 설계 의도 |

## 빠른 시작

```bash
cp crex.example.toml crex.toml  # 엔드포인트·모델명 수정
python -m crex doctor           # 무엇이 되고 무엇이 안 되는지 확인
python -m crex review --from HEAD~1 --to HEAD
```

```bash
python -m crex review --staged --out reports/
```

```bash
python -m crex scan src/legacy.cpp    # diff 없이 전체 파일 감사
```

리뷰가 왜 그런 결과를 냈는지 들여다보려면 관제 화면을 띄웁니다. 생성 모델이 받은
프롬프트, 강제된 스키마, 검증 모델의 판정과 그 근거가 그대로 보입니다.

```bash
python -m crex.viz --port 18765       # http://127.0.0.1:18765
```

테스트 (외부 의존 없음, LLM 불필요):

```bash
python tests/run_all.py
```

---

## 폐쇄망 반입 체크리스트

**코어는 외부 의존이 없다.** Python 3.11+ 표준 라이브러리만 쓴다. 소스만 옮기면
CLI, 관제 화면, 테스트가 그대로 돈다. MCP 서버(Zed 연동)만 `requirements.txt` 가
필요하다.

- [ ] Python 3.11 이상 확인 (`tomllib` 이 3.11부터 stdlib)
- [ ] `crex/`, `rules/`, `eval/`, `tests/`, `wiki/`, `docs/` 디렉터리 복사
- [ ] `crex.example.toml` → `crex.toml` 로 복사 후 엔드포인트 수정
- [ ] `python tests/run_all.py` 로 반입 무결성 확인
- [ ] `python -m crex doctor` 로 엔드포인트·분석기 상태 확인
- [ ] vLLM 이 guided decoding 을 지원하는지 확인 — `doctor` 의 LLM 항목이 실패하면
      `structured_output_mode` 를 `"guided_json"` 으로 바꿔 재시도
- [ ] (선택) MCP 서버를 쓸 장비에만 `pip install -r requirements.txt`
- [ ] (선택) tree-sitter wheel 반입 — `requirements-optional.txt` 참고
- [ ] (선택) Semgrep 룰팩 반입 — `"auto"` 는 폐쇄망에서 동작하지 않는다
- [ ] C++ 이라면 `compile_commands.json` 생성
      (`cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build`)

---

## 왜 이렇게 만들었나

### 컨텍스트를 늘리지 않는다

Qwen3.6과 Gemma4 모두 256K 컨텍스트를 지원하지만 **8192 토큰으로 운영한다.**

ASE 2025 연구에서 검색 예시를 top-1 → top-3 → top-5로 늘리자 BLEU-4가
12.32 → 11.76 → 10.81로 단조 감소했다. 중복과 상충 신호 때문이다.
Qwen3.6은 32K→128K에서 처리량도 26→9 tok/s로 붕괴한다.

`max_input_tokens` 를 올리기 전에 반드시 골든셋으로 FAR 변화를 측정하라.

### 에이전트 루프를 쓰지 않는다

25~40B급 모델에게 툴을 쥐어주고 자율 탐색을 시키면 툴콜 실패와 컨텍스트 폭주가
함께 온다. 대신 고정 단계로 간다 — 청크 하나, 프롬프트 하나, 지적 목록 하나.
느리지만 예측 가능하고, 어디서 무엇이 실패했는지 정확히 알 수 있다.

### 생성과 검증에 다른 모델을 쓴다

같은 모델의 자기검증은 자기 환각을 잘 못 잡는다. BitsAI-CR 은 프로덕션에서
검증 단계가 지적의 55.25%를 기각하고 정밀도 77%를 만들었다.
기각률 40~60%가 정상 범위다 — 낮으면 검증이 일을 안 하고 있는 것이다.

### LLM 의 역할을 재정의한다

정적분석을 먼저 돌리고, 프롬프트에서 이렇게 지시한다:
*"결함을 찾아라"가 아니라 **"이 도구 결과를 검증하고, 도구가 못 잡는 로직·설계
결함만 추가하라"***. 근거 없는 지적의 상당수가 프롬프트 단계에서 사라진다.

### diff 와 파일이 어긋나면 멈춘다

diff 생성 이후 작업 트리가 바뀌면 모든 라인 번호가 밀린다. 그 상태의 리뷰는
전부 거짓말이다. 기본값은 해당 파일을 건너뛰는 것(`on_mismatch = "raise"`)이다.

---

## 룰 택소노미

`rules/taxonomy.toml` 이 단일 진실 공급원이다. 현재 41개 룰.

| 언어 | 룰 수 | 비고 |
|---|---|---|
| C++ | 14 | 댕글링 포인터, use-after-move, RAII, 반복자 무효화, 부호 혼용 등 |
| C# | 15 | IDisposable, async void, .Result 데드락, LINQ 다중 열거 등 |
| Python | 14 | 가변 기본인자, late-binding closure, 리소스 누수 등 |

**`chunk_local` 필드가 핵심이다.** "이 룰은 제시된 스니펫만 보고 판정 가능한가"를
뜻한다. false 인 룰은 소형 모델이 반드시 지어내므로 기본 프로파일에서 제외된다.

각 룰의 `counter` 필드는 오탐 방지 단서다 — "이런 경우는 지적하지 마라".
FAR 관리의 상당 부분이 여기서 이뤄진다.

```bash
python -m crex.rules                                      # 택소노미 검증·통계
python -m crex.rules --out .opencodereview/rule.json      # OCR 용 rule.json 생성
```

룰을 추가할 때는 **한 번에 하나씩** 넣고 골든셋을 재측정하라. FAR 을 올리는 룰은
즉시 제거한다.

---

## 평가 (Phase 0)

측정 없이는 아무것도 개선할 수 없다.

```bash
python eval/run_eval.py init                        # 골든셋 뼈대
python eval/run_eval.py run --out reports/phase-1.json
python eval/run_eval.py compare reports/phase-1.json reports/phase-3.json
```

지표 셋:

- **KBI** — 실제 결함 중 잡아낸 비율 (재현율)
- **FAR** — 지적 중 오탐 비율. **최우선 관리 지표**
- **룰별 정밀도** — 폐기할 룰을 고르는 근거

수용 기준:

| Phase | KBI | FAR | 청크당 지연 |
|---|---|---|---|
| 1 (베이스라인) | 측정만 | 측정만 | 측정만 |
| 2 (룰 적용) | ≥ 베이스라인 | ≤ 베이스라인 | — |
| 3 (필터 적용) | 베이스라인의 90% 유지 | **≤ 25%** | ≤ 10초 |

CI 게이트로 쓰려면 `--max-far 0.25` 를 붙인다. 초과 시 종료 코드 1.

---

## alibaba/open-code-review 와의 관계

계획에서는 OCR 을 베이스로 삼고 그 위에 그라운딩·필터를 얹기로 했다.
실제로는 **자체 파이프라인(native 모드)을 먼저 완성했다.** 이유:

1. OCR 의 리뷰 결과 JSON 스키마가 공개 문서에 없어, 폐쇄망 밖에서 파서를
   작성하면 추측이 된다. 추측한 파서를 넣는 것보다 없는 편이 낫다.
2. OCR 은 에이전트 툴콜 루프에 의존한다. 그것이 27B 에서 안정적인지가 계획의
   Phase 1 판단 게이트였고, 불안정할 경우의 대비책(Plan B)이 바로 이 자체
   파이프라인이었다. 그 대비책을 먼저 만들어 두었다.

**Phase 1 에서 할 일은 그대로다** — OCR 을 반입해 골든셋으로 베이스라인을 재고,
native 모드와 비교한다. `.opencodereview/rule.json` 생성기는 이미 있으므로
OCR 쪽 룰 설정은 바로 쓸 수 있다.

- OCR 이 더 낫다 → `crex/pipeline.py` 에 OCR 어댑터를 추가하고
  ground/filter 는 그대로 재사용한다 (`review.mode = "ocr"` 자리는 비워 두었다)
- native 가 더 낫다 → 이미 완성되어 있다

두 경우 모두 그라운딩과 ReviewFilter 는 버릴 것이 없다.

---

## 구조

```
crex/
  schema.py     데이터 모델 (dataclass, 외부 의존 없음)
  llm.py        vLLM 클라이언트 (stdlib urllib) + guided decoding + 토큰 예산
  chunk.py      diff 파싱, tree-sitter 청킹, 라인 상태 주석, 정합성 검사
  ground.py     정적분석 어댑터 8종 + 결과 정규화
  generate.py   RuleChecker — enum 제약 스키마로 지적 생성
  filter.py     ReviewFilter — 결정론적 검사 + 교차 모델 재판정
  rules.py      택소노미 로더, OCR rule.json 생성기
  pipeline.py   오케스트레이션 (diff / scan)
  report.py     Markdown / SARIF / JSON
  cli.py        review / scan / doctor
  paths.py      디렉터리 확장, exclude glob, diff 경로 필터
  gitio.py      git diff / merge-base (GitPython, 없으면 subprocess)
  service.py    ReviewService — MCP 도구 5종의 실제 동작 (FastMCP 미의존)
  mcp.py        FastMCP 바인딩 — Zed 에이전트 패널 연동
  viz/          관제 화면 — 3계층
    trace.py      이벤트 모델, 프롬프트↔청크↔지적 상관관계      ┐ Engine
    engine.py     계측된 파이프라인, 실행 레지스트리            ┘
    api.py        전송 무관 라우팅 (Request → Response)          ┐ Application
    server.py     ASGI(uvicorn) + stdlib http.server 폴백        ┘
    web/          index.html, style.css, store.js, client.js, view.js  ← Presentation
rules/
  taxonomy.toml 룰 정의 — 단일 진실 공급원
eval/
  run_eval.py   KBI / FAR / 룰별 정밀도
tests/
  run_all.py    전체 실행 (외부 의존 없음)
  fake_vllm.py  가짜 vLLM — LLM 없이 전 구간 검증
```

---

## 참고 문헌

- [BitsAI-CR: Automated Code Review via LLM in Practice](https://arxiv.org/abs/2501.15134) — 2단계 파이프라인, 룰 택소노미, Conclusion-First, Outdated Rate
- [Towards Practical Defect-Focused Automated Code Review (ICML 2025)](https://arxiv.org/abs/2505.17928) — 대규모 C++ 코드베이스, 코드 슬라이싱, 오탐 필터링
- [When More Retrieval Hurts](https://arxiv.org/html/2511.05302v2) — 컨텍스트를 늘리면 나빠진다
- [Structured Outputs — vLLM](https://docs.vllm.ai/en/v0.8.4/features/structured_outputs.html)
