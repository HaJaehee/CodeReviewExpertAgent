# CREX — 폐쇄망 sLLM 코드리뷰 파이프라인

**버전 0.5** · **C**ode **R**eview **EX**pert Agent. 명령어와 패키지 이름은 소문자 `crex`를 사용합니다.

25~40B 규모의 로컬 모델(Qwen3.6, Gemma4)을 활용해 C++, C#, Python 코드를 리뷰합니다.
모든 설계는 단 하나의 목표에 맞춰져 있습니다 — **모델의 환각(Hallucination)을 구조적으로 차단하는 것**입니다.

```
git diff
   │
   ├─▶ 청킹          chunk → Tree-sitter 함수 경계 확장(4배 상한) · AST/호출 흐름 분석 → 라인 상태 주석
   │
   ├─▶ 그라운딩      clang-tidy / cppcheck / Roslyn / ruff / mypy / bandit / semgrep
   │                 정적분석으로 결정론적 사실을 먼저 확보합니다
   │
   ├─▶ RuleChecker   Qwen3.6-27B · Guided Decoding으로 룰 ID와 라인 번호를 enum 강제
   │
   ├─▶ ReviewFilter  Gemma4 26B · 결정론적 검사 + Conclusion-First 교차 모델 재판정
   │
   └─▶ 리포트        Markdown / SARIF / JSON / HTML
```

---

## 환각을 차단하는 4단계 방어선

| 단계 | 방어 장치 | 차단 대상 |
|---|---|---|
| 1. 입력 | 라인마다 `[added @142]` 상태 주석 부여 | 모델이 라인 번호를 직접 세거나 추론할 필요를 없앱니다 |
| 2. 생성 | Guided Decoding의 `enum` 제약 | **라인 번호와 룰 ID 날조를 생성 단계에서 원천 차단합니다** |
| 3. 검증(결정론) | 라인 범위·변경 라인 여부·제외 규칙 검사 | LLM 호출 없이 즉시 기각합니다 (비용 0, 정확도 100%) |
| 4. 검증(LLM) | 별도 모델의 Yes/No 교차 재판정 | 코드와 문맥에 근거가 없는 추측성 지적을 걸러냅니다 |

2단계가 이 아키텍처의 핵심입니다. vLLM의 스키마 강제 기능은 보통 "JSON 파싱 실패 방지" 용도로 쓰이지만, CREX에서는 **실제 변경된 라인 번호 목록**과 **택소노미에 존재하는 룰 ID**만 `enum`에 허용합니다. 모델이 그 밖의 토큰을 생성하는 것 자체가 불가능하므로, 사후 필터링이 아니라 생성 시점에 환각이 차단됩니다.

3·4단계는 구버전 vLLM 등 Guided Decoding을 지원하지 않는 환경을 위한 안전장치이자, 창 외부 심볼 정의와 호출 흐름까지 대조하여 근거 없는 지적을 기각하는 교차 검증 단계입니다.

---

## 워크플로 (Zed / MCP)

개발자는 터미널로 이동할 필요 없이 Zed 에이전트 패널에서 대화형으로 코드 리뷰를 실행할 수 있습니다.

```
코드 작성 → git add → "변경사항 리뷰해줘" → 수정 반영 → commit → "main이랑 비교해서 리뷰해줘"
                          review_staged                             review_diff
```

| 사용자 요청 예시 | 호출되는 도구 |
|---|---|
| "변경사항 리뷰해줘" | `review_staged` — 커밋 직전에 가장 자주 사용하는 리뷰 |
| "커밋 안 한 거 전부 봐줘" | `review_working_tree` — 스테이징 여부와 무관하게 작업 트리 전체 변경 리뷰 |
| "main이랑 비교해서 리뷰해줘" | `review_diff` — 공통 분기점(merge-base)을 자동으로 찾아 비교 |
| "이 파일 전체 봐줘" | `review_file` — diff 없이 단일 파일 전체 감사 |
| "이 폴더 감사해줘" | `review_directory` — 디렉터리 전체 감사 (40개 파일 초과 시 거부) |
| "파서 쪽 변경만 봐줘" | `paths=["src/parser"]` — 앞의 diff 계열 도구에 경로 필터로 전달 |

MCP 도구는 에이전트 컨텍스트가 넘치지 않도록 **핵심 요약만** 반환하며, 상세 리포트는 파일로 저장합니다.

### 리뷰 지적을 확인했을 때

```
타당한 지적   → 코드를 수정합니다
애매한 지적   → 코드를 다시 확인하고, 실제 문제가 아니면 무시하거나 .crexignore에 등록합니다
                (CREX는 확신이 없으면 침묵하도록 설계되어 있으므로 일단 한 번은 확인하는 것이 좋습니다)
명백한 오탐   → 해당 룰 ID를 담당자에게 전달합니다 → counter(예외 조건) 보강 후 팀 전체에서 오탐이 사라집니다
```

`"리뷰 결과가 이상해요"`라는 피드백만으로는 원인을 조치하기 어렵지만, `"csharp.idisposable-not-disposed 룰이 DI 컨테이너가 관리하는 객체를 반복해서 지적해요"`처럼 룰 ID를 포함해 전달하면 즉시 튜닝할 수 있습니다. (관제 화면에서도 클릭 한 번으로 `.crexignore`에 파일·룰 단위 제외 규칙을 추가하거나 취소할 수 있습니다.)

에이전트가 MCP 도구를 호출하지 않고 직접 diff를 읽어 임의로 리뷰하는 현상이 가장 흔한 문제입니다. [`AGENTS.md`](AGENTS.md) 파일을 리뷰 대상 저장소 루트에 복사해 두면 이 문제를 방지할 수 있습니다.

전체 사용 흐름과 담당자 작업(골든셋 구축·룰 튜닝)은 [워크플로 문서](docs/user_manual/workflow.md)를, 에디터 설정 방법은 [Zed 연동](docs/user_manual/operations.md#zed-연동-mcp)을 참고하십시오.

---

## 문서

상세한 사용 설명서는 [`docs/user_manual/`](docs/user_manual/index.md)에 정리되어 있습니다.
마크다운 뷰어가 없는 폐쇄망 장비에서는 아래 명령으로 HTML 설명서를 생성해 브라우저에서 열람하십시오. 외부 패키지나 인터넷 연결은 필요하지 않습니다.

```bash
python tools/render_docs.py   # → docs/user_manual_html/index.html
```

| 문서 | 설명 |
|---|---|
| [시작하기](docs/user_manual/getting-started.md) | 설치, 첫 실행, 리포트 읽는 법 |
| [워크플로](docs/user_manual/workflow.md) | Zed에서 리뷰 실행하기, 지적 사항 대응 절차 |
| [설정](docs/user_manual/configuration.md) | `crex.json` 전체 설정 항목 안내 |
| [제외 규칙 (.crexignore)](docs/user_manual/crexignore.md) | 경로·전역 룰·파일별 룰 제외 패턴 작성법 |
| [정적분석 도구](docs/user_manual/analyzers.md) | `clang-tidy`·`cppcheck`·`ruff` 설치, 라이선스, 반입 절차 |
| [룰 작성법](docs/user_manual/writing-rules.md) | 오탐을 늘리지 않고 새 룰을 추가하는 방법 |
| [평가와 튜닝](docs/user_manual/evaluation.md) | 골든셋 구축, KBI/FAR 측정, 저품질 룰 폐기 기준 |
| [관제 화면](docs/user_manual/visualizer.md) | 생성·검증 모델의 프롬프트, 응답, 판정 내역을 웹에서 확인 |
| [반입](docs/user_manual/transfer.md) | Python 런타임까지 포함한 오프라인 반입 번들 생성 및 검증 |
| [운영](docs/user_manual/operations.md) | vLLM 서버 기동, Zed 연동, 일상 운영 가이드 |
| [문제 해결](docs/user_manual/troubleshooting.md) | 자주 발생하는 증상별 원인과 해결 방법 |
| [동작 원리](docs/user_manual/internals.md) | 파이프라인 내부 구조와 아키텍처 설계 의도 |

---

## 빠른 시작

```bash
python -m crex --version        # crex 0.5
cp crex.example.json crex.json  # 엔드포인트·모델명 수정
python -m crex doctor           # 엔드포인트 연결 및 분석기 동작 상태 점검
python -m crex review --from HEAD~1 --to HEAD
```

CREX를 리뷰 대상 저장소 안에 설치할 필요는 없습니다. CREX는 한곳에 설치해 두고 `--workspace` 옵션으로 리뷰할 저장소 경로만 지정하면 됩니다. 매번 옵션을 입력하기 번거롭다면 `crex.json`에 `"workspace": "D:/work/myrepo"`를 설정하거나 `CREX_WORKSPACE` 환경변수를 사용하십시오.

```bash
cd D:\tools\crex
python -m crex review --workspace D:\work\myrepo --staged

python -m crex workspace D:\work\myrepo   # crex.json에 대상 저장소 고정 (--clear로 해제)
python -m crex workspace                  # 현재 연결된 대상 저장소 확인
```

실행 중에도 리뷰 대상 저장소를 즉시 바꿀 수 있습니다. 관제 화면 왼쪽의 **변경** 버튼이나 Zed 에이전트의 `set_workspace` 도구를 사용하면 되며, 두 방식 모두 설정 파일(`crex.json`)은 수정하지 않습니다.

```bash
python -m crex review --staged --out reports/
```

```bash
python -m crex scan src/legacy.cpp    # diff 없이 파일 전체 감사
```

C++ 저장소라면 최초 1회 아래 명령도 실행하십시오. `clang-tidy`가 헤더 경로와 컴파일 옵션을 인식하려면 `compile_commands.json`이 필요한데, CMake와 Visual Studio(MSBuild) 프로젝트 모두 이 명령 하나로 컴파일 DB를 생성하고 `crex.json`에 경로를 자동 등록합니다.

```bash
python -m crex compiledb
```

리뷰 결과의 판단 과정과 근거를 상세히 살펴보려면 관제 화면(Visualizer)을 실행하십시오. 생성 모델이 받은 프롬프트와 강제된 스키마, 검증 모델의 판정과 사유를 웹 브라우저에서 그대로 확인할 수 있습니다.

```bash
python -m crex.viz --port 18765       # http://127.0.0.1:18765
```

Zed 등의 에이전트 패널과 연동할 때는 MCP 서버를 사용합니다. 기본 전송 방식은 `stdio`(에디터가 자식 프로세스로 직접 실행)이며, 여러 개발자가 서버 한 대를 공유하거나 클라이언트가 다른 장비에 있을 때만 HTTP 모드로 실행하십시오.

```bash
python -m crex.mcp                    # stdio (권장)
python -m crex.mcp --transport http   # Streamable HTTP — http://127.0.0.1:18766/mcp
```

HTTP 엔드포인트에는 별도 인증이 없습니다. 반드시 루프백(`127.0.0.1`)에 바인딩하거나 접근 제어가 적용된 내부망에서만 개방하십시오 — 자세한 내용은 [운영 문서](docs/user_manual/operations.md#streamable-http-엔드포인트)를 참고하십시오.

전체 단위·통합 테스트 실행 (외부 패키지 및 LLM 서버 불필요):

```bash
python tests/run_all.py
```

---

## 정적분석 도구 (선택 사항이나 설치 권장)

CREX는 LLM을 호출하기 **전에** `clang-tidy`, `cppcheck`, `ruff` 같은 정적분석 도구를 먼저 실행하고 그 결과를 프롬프트에 주입합니다. 이를 통해 모델의 역할을 막연한 "결함 찾기"에서 **"정적분석 결과를 검증하고, 정적분석이 잡지 못하는 로직·흐름 결함을 보완하는 것"**으로 구체화합니다.

**정적분석 도구가 설치되어 있지 않아도 리뷰는 정상 동작합니다.** `PATH`에서 도구를 찾지 못하면 해당 분석기만 조용히 건너뜁니다. 폐쇄망에서는 장비마다 도구 설치 환경이 다르므로 특정 도구가 없다고 파이프라인이 멈추지 않도록 설계했습니다. 다만 정적분석 도구가 함께 동작할 때 리뷰의 정확도와 재현율이 훨씬 높아집니다.

| 언어 | 기본 실행 도구 | 설치 방법 |
|---|---|---|
| C++ | `clang-tidy`, `cppcheck` | LLVM 설치 프로그램 / cppcheck MSI |
| C# | `roslyn` (`dotnet build`) | .NET SDK만 설치되어 있으면 동작합니다 |
| Python | `ruff`, `mypy`, `bandit` | `pip install ruff mypy bandit` |

`roslynator`와 `semgrep`은 설정 파일에 이름을 명시해야 활성화되는 선택 도구입니다.

모든 도구는 무료 오픈소스입니다 — LLVM(Apache-2.0 with LLVM-exception), cppcheck(**GPL-3.0**), ruff·mypy(MIT), bandit·roslynator(Apache-2.0), .NET SDK(MIT). 상용 구독이나 셰어웨어 도구는 포함되어 있지 않습니다. `cppcheck`만 카피레프트(GPL-3.0) 라이선스이지만, CREX가 별도 프로세스로 실행해 표준 출력만 읽으므로 분석 대상 사내 소스 코드에는 라이선스가 전파되지 않습니다.

```bash
python -m crex doctor    # 설치된 도구와 누락된 도구를 한눈에 확인
```

```
정적분석 도구
  OK  clang-tidy (clang-tidy)
  없음 cppcheck (cppcheck)
  OK  ruff (ruff)
```

설치 파일 다운로드 경로, 폐쇄망 반입 절차(해시 대조 포함), `compile_commands.json`의 필요성, Semgrep 룰팩의 별도 라이선스 안내는 **[정적분석 도구 문서](docs/user_manual/analyzers.md)**를 참고하십시오.

하나만 먼저 도입한다면 주력 언어의 대표 도구 하나부터 시작하십시오. C++은 `cppcheck`, Python은 `ruff`가 설치 부담 대비 효과가 가장 큽니다.

---

## 폐쇄망 반입 체크리스트

**코어 엔진은 외부 패키지 의존성이 없습니다.** Python 3.11 이상의 표준 라이브러리만 사용하므로 소스 코드만 반입하면 CLI, 관제 화면, 테스트가 즉시 동작합니다(`package.ps1`로 Python 런타임 포함 오프라인 번들 생성 가능). MCP 서버(Zed 연동)를 사용할 때만 `requirements.txt`가 필요합니다.

- [ ] Python 3.11 이상 설치 확인 (룰 택소노미를 파싱하는 `tomllib`가 3.11부터 표준 라이브러리에 포함됨)
- [ ] `crex/`, `rules/`, `tests/`, `docs/`, `tools/` 디렉터리 반입 (또는 `package.ps1`로 생성한 번들 압축 해제)
- [ ] `crex.example.json`을 `crex.json`으로 복사한 뒤 사내 vLLM 엔드포인트 및 모델명 입력
- [ ] `python tests/run_all.py` 실행으로 반입 파일 무결성 확인
- [ ] `python -m crex doctor` 실행으로 LLM 엔드포인트 및 정적분석 도구 상태 확인
- [ ] vLLM 서버의 Guided Decoding 지원 여부 확인 — `doctor` 실행 결과에서 LLM 항목은 **연결·모델**과 **구조화 출력** 두 줄로 표시되며, 두 항목 모두 `OK`여야 합니다. 연결만 `OK`이고 구조화 출력이 실패하면 모든 청크가 실패하여 리뷰 지적이 0건으로 나옵니다 ([문제 해결](docs/user_manual/troubleshooting.md#지적이-항상-0건입니다) 참고)
- [ ] (선택) MCP 서버를 구동할 장비에만 `pip install -r requirements.txt` 실행
- [ ] (선택) Tree-sitter wheel 반입 — `requirements-optional.txt` 참고 (없으면 정규식·중괄호 휴리스틱 폴백으로 동작)
- [ ] (선택) 정적분석 도구 반입 — `clang-tidy` / `cppcheck` / `ruff` 등 ([설치 가이드](docs/user_manual/analyzers.md) 참고, 미설치 시 해당 분석기만 건너뜀)
- [ ] (선택) Semgrep 룰팩 반입 — `"auto"` 설정은 외부 네트워크에 접속하므로 폐쇄망에서 동작하지 않습니다
- [ ] C++ 프로젝트라면 `python -m crex compiledb`로 `compile_commands.json` 생성 (CMake와 MSBuild 모두 지원하며, 없으면 `clang-tidy`가 헤더를 찾지 못해 분석 정확도가 떨어집니다)

---

## 설계 원칙과 배경

### 컨텍스트를 무작정 늘리지 않습니다

Qwen3.6과 Gemma4 모두 256K 컨텍스트 길이를 지원하지만, CREX는 기본 **8,192 토큰 예산으로 운영합니다.**

ASE 2025 연구에 따르면 프롬프트에 넣는 검색 예시를 top-1 → top-3 → top-5로 늘렸을 때 BLEU-4 점수가 오히려 12.32 → 11.76 → 10.81로 단조 감소했습니다. 불필요한 주변 코드가 늘어나면 중복·상충 신호로 인해 모델의 주의력이 분산되기 때문입니다. 또한 Qwen3.6은 컨텍스트가 32K에서 128K로 커질 때 추론 처리량이 26 tok/s에서 9 tok/s로 급격히 저하됩니다.

따라서 파일 전체를 통째로 넣는 대신, 변경된 함수 경계(최대 150줄)로 청크를 나누고 Tree-sitter AST 심볼 계약·변경 요약 지도(Change Map)·호출 그래프 실행 경로 슬라이스(Call-Graph Flow)만 고밀도로 압축해 주입합니다. `max_input_tokens`를 늘리기 전에는 반드시 골든셋으로 오탐률(FAR) 변화를 먼저 측정하십시오.

### 자율 에이전트 루프를 사용하지 않습니다

25~40B 규모의 로컬 모델에게 도구를 쥐여주고 자율 탐색 루프를 돌리면 도구 호출(Tool Call) 오류와 컨텍스트 폭주가 빈번하게 발생합니다. 대신 CREX는 **고정된 파이프라인 단계**(청크 하나 → 프롬프트 하나 → 지적 목록 하나)로 동작합니다. 실행 시간이 예측 가능하고, 문제가 발생했을 때 어느 단계에서 왜 실패했는지 정확히 추적할 수 있습니다.

### 생성과 검증에 서로 다른 모델을 사용합니다

동일한 모델에게 자신의 출력을 스스로 검증하게 하면 자신이 만들어낸 환각을 제대로 걸러내지 못합니다. ByteDance의 BitsAI-CR 프로덕션 사례에서도 교차 검증 단계가 초기 지적의 55.25%를 기각함으로써 최종 정밀도를 77%까지 끌어올렸습니다. 검증 기각률 40~60%는 정상 범위이며, 기각률이 지나치게 낮다면 검증 모델이 제 역할을 하지 못하고 있다는 신호입니다.

### LLM의 역할을 구체적으로 제한합니다

정적분석 도구를 먼저 실행하고 AST 구문 및 호출 그래프 흐름을 함께 제공한 뒤, 프롬프트에서 모델의 역할을 명확히 규정합니다. 막연하게 *"코드에서 결함을 찾아라"*라고 지시하는 대신, **"정적분석 결과와 AST·호출 흐름 계약을 바탕으로 실제 결함을 검증하고, 정적분석 도구가 구조적으로 잡지 못하는 비즈니스 로직·설계·교차 함수 흐름 결함을 지적하라"**고 지시하여 근거 없는 추측성 지적을 줄입니다.

### diff와 디스크 파일이 어긋나면 즉시 중단합니다

diff를 생성한 이후 작업 트리 파일이 변경되면 모든 라인 번호가 밀리게 됩니다. 라인 번호가 어긋난 상태에서 생성된 리뷰는 신뢰할 수 없으므로, 기본 설정(`on_mismatch = "raise"`)에서는 해당 파일을 건너뛰고 불일치 오류를 명확히 알립니다.

---

## 룰 택소노미

`rules/taxonomy.toml`이 단일 진실 공급원(Single Source of Truth)입니다.

| 언어 | 주요 점검 영역 |
|---|---|
| 공통 (`any`) | 비즈니스 로직 오류, 경계 조건 누락, 예외/에러 미처리, 널 역참조, 자원 누수 |
| C++ (`cpp`) | 댕글링 포인터, use-after-move, RAII 위반, 반복자 무효화, 부호 혼용 등 |
| C# (`csharp`) | `IDisposable` 미해제, `async void`, `.Result` 데드락, LINQ 다중 열거 등 |
| Python (`python`) | 가변 기본 인자, late-binding 클로저, 컨텍스트 매니저 미사용 리소스 누수 등 |

**`chunk_local` 필드가 핵심입니다.** 이 필드는 "제시된 코드 청크와 AST 문맥만으로 확정 판정이 가능한가"를 나타냅니다. `false`로 지정된 룰은 소형 모델이 추측으로 지어낼 위험이 크므로 기본 프로파일에서 제외됩니다.

각 룰의 `counter` 필드는 오탐 방지 조건("다음과 같은 경우에는 지적하지 마라")을 정의합니다. 오탐률(FAR) 관리의 상당 부분이 이 `counter` 조건을 정교하게 다듬는 과정에서 이루어집니다.

```bash
python -m crex.rules                                      # 택소노미 검증 및 통계 출력
python -m crex.rules --out .opencodereview/rule.json      # OCR 호환 rule.json 생성
```

새로운 룰을 추가할 때는 **한 번에 하나씩** 추가하고 골든셋으로 재측정하십시오. 오탐률(FAR)을 높이는 룰은 즉시 수정하거나 제거해야 합니다.

---

## 평가 (Phase 0)

정량적 측정 없이는 리뷰 품질을 개선할 수 없습니다.

```bash
python eval/run_eval.py init                        # 골든셋 기본 뼈대 생성
python eval/run_eval.py run --out reports/phase-1.json
python eval/run_eval.py compare reports/phase-1.json reports/phase-3.json
```

핵심 관리 지표 3가지:

- **KBI (Known Bug Inclusion)** — 알려진 실제 결함 중 리뷰가 잡아낸 비율 (재현율)
- **FAR (False Alarm Rate)** — 전체 지적 중 오탐이 차지하는 비율 (**최우선 관리 지표**)
- **룰별 정밀도 (Per-Rule Precision)** — 오탐이 많은 저품질 룰을 식별해 튜닝하거나 폐기하는 근거

단계별 수용 기준:

| 단계 | KBI (재현율) | FAR (오탐률) | 청크당 지연 시간 |
|---|---|---|---|
| Phase 1 (베이스라인) | 기준치 측정 | 기준치 측정 | 기준치 측정 |
| Phase 2 (룰 적용) | ≥ 베이스라인 | ≤ 베이스라인 | — |
| Phase 3 (필터 적용) | 베이스라인의 90% 이상 유지 | **≤ 25%** | ≤ 10초 |

CI 품질 게이트로 활용할 때는 `--max-far 0.25` 옵션을 부여하십시오. 오탐률이 기준치를 초과하면 종료 코드 `1`을 반환합니다.

---

## alibaba/open-code-review와의 관계

초기 계획에서는 `alibaba/open-code-review`(OCR)를 베이스로 삼고 그 위에 정적분석 그라운딩과 검증 필터를 얹을 예정이었으나, 실제로는 **자체 파이프라인(native 모드)을 먼저 완성했습니다.** 그 이유는 다음과 같습니다.

1. 공개 문서에 OCR의 리뷰 결과 JSON 스키마가 명시되어 있지 않아, 외부에서 파서를 작성하면 추측에 의존할 수밖에 없습니다. 불확실한 파서를 넣기보다 실제 반입 환경에서 스키마를 확인한 뒤 연동하는 편이 안전합니다.
2. OCR은 자율 에이전트 툴콜 루프에 의존합니다. 27B 규모 모델에서 툴콜 루프가 안정적으로 동작하는지 검증하는 것이 Phase 1의 판단 게이트였고, 불안정할 때 사용할 대비책(Plan B)이 바로 현재의 고정 단계 파이프라인이었습니다. 그 대비책을 먼저 완성해 두었습니다.

**Phase 1의 검증 절차는 그대로 유효합니다.** 폐쇄망에 OCR을 반입해 골든셋으로 베이스라인을 측정한 뒤, 현재의 native 모드와 성능을 비교합니다. `.opencodereview/rule.json` 생성기는 이미 구현되어 있으므로 OCR용 룰 설정은 즉시 생성할 수 있습니다.

- OCR의 성능이 더 우수한 경우 → `crex/pipeline.py`에 OCR 어댑터를 추가하고 정적분석 그라운딩과 `ReviewFilter`는 그대로 결합합니다 (`review.mode = "ocr"` 설정 자리가 준비되어 있습니다).
- native 모드의 성능이 더 우수한 경우 → 현재 완성된 파이프라인을 그대로 사용합니다.

어느 쪽을 선택하든 정적분석 그라운딩과 `ReviewFilter`는 그대로 재사용됩니다.

---

## 프로젝트 구조

```
package.ps1         폐쇄망 반입용 오프라인 번들 패키징 스크립트
crex/
  schema.py         데이터 모델 (dataclass, 외부 의존성 없음)
  llm.py            vLLM 클라이언트 (표준 urllib) + Guided Decoding + 토큰 예산 관리
  chunk.py          diff 파싱, 심볼 경계 청킹, 교차 청크 변경 요약 지도(Change Map)
  treesitter.py     Tree-sitter AST 구문 분석 및 심볼 계약(SymbolContract) 추출
  flow.py           호출 그래프 실행 경로 슬라이싱(Call-Graph Flow) 및 흐름 위험 신호 분석
  ground.py         정적분석 도구 어댑터 8종 + 출력 정규화
  generate.py       RuleChecker — enum 제약 스키마 기반 지적 생성
  filter.py         ReviewFilter — 결정론적 검사 + 창 외부 문맥 확장 교차 모델 재판정
  rules.py          택소노미 로더 및 OCR rule.json 생성기
  ignore_manager.py .crexignore 파싱, 매칭, 규칙 추가/취소 관리
  pipeline.py       파이프라인 오케스트레이션 (diff / scan)
  report.py         리포트 출력 (Markdown / SARIF / JSON / HTML)
  cli.py            CLI 진입점 (review / scan / compiledb / doctor / workspace)
  workspace.py      리뷰 대상 저장소 해석 — CLI·MCP·관제 화면 공통 규칙
  paths.py          디렉터리 확장, exclude glob, diff 경로 및 룰 필터링
  gitio.py          git diff / merge-base 추출 (GitPython 우선, subprocess 폴백)
  service.py        ReviewService — MCP 도구 7종의 실제 구현 (FastMCP 비의존)
  mcp.py            FastMCP 바인딩 — Zed 에이전트 패널 연동 (stdio / Streamable HTTP)
  viz/              관제 화면 (3계층 아키텍처)
    db.py             SQLite 기반 실행 이력 및 설정 영구 저장소 (.crex/viz.db)
    trace.py          이벤트 모델, 프롬프트↔청크↔지적 상관관계 추적     ┐ Engine
    engine.py         계측된 파이프라인 및 실행 레지스트리               ┘
    api.py            전송 방식과 무관한 라우팅 (Request → Response)     ┐ Application
    server.py         ASGI(uvicorn) + 표준 http.server 폴백 서버         ┘
    web/              index.html, style.css, store.js, client.js, view.js ← Presentation
rules/
  taxonomy.toml     룰 정의 — 단일 진실 공급원
eval/
  run_eval.py       골든셋 평가 도구 (KBI / FAR / 룰별 정밀도)
tests/
  run_all.py        전체 테스트 스위트 실행기 (외부 의존성 없음)
  fake_vllm.py      가짜 vLLM 서버 — 실제 LLM 없이 전 구간 통합 검증
```

---

## 참고 문헌

- [BitsAI-CR: Automated Code Review via LLM in Practice](https://arxiv.org/abs/2501.15134) — 2단계 생성·검증 파이프라인, 룰 택소노미, Conclusion-First, Outdated Rate
- [Towards Practical Defect-Focused Automated Code Review (ICML 2025)](https://arxiv.org/abs/2505.17928) — 대규모 C++ 코드베이스, 코드 슬라이싱, 오탐 필터링
- [When More Retrieval Hurts](https://arxiv.org/html/2511.05302v2) — 과도한 컨텍스트 확장이 모델 성능을 떨어뜨리는 현상 분석
- [Structured Outputs — vLLM](https://docs.vllm.ai/en/v0.8.4/features/structured_outputs.html) — JSON Schema `enum` 제약 기반 Guided Decoding
