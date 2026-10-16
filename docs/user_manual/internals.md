# 동작 원리

이 문서는 CREX의 내부 구조를 이해하거나 코드를 수정하려는 개발자를 위한 아키텍처 문서입니다.

## 전체 파이프라인 흐름

```
git diff (또는 scan 대상 파일 목록)
   │
   ▼
parse_unified_diff()                 crex/chunk.py
   │  FileDiff[] — hunk별로 라인 상태(added/deleted/unchanged)와 라인 번호 인덱싱
   ▼
Chunker.chunk_file()                 crex/chunk.py + crex/treesitter.py
   │  ① diff와 디스크 파일 정합성 검증 (_verify_source)
   │  ② hunk → 심볼(함수/클래스) 경계 확장 → 4배/3배 상한 및 최대 150줄 분할
   │  ③ Tree-sitter AST 분석: 둘러싼 심볼, 클래스 멤버 필드, 피호출 함수 계약(SymbolContract) 추출
   │  ④ attach_cross_chunk_context() → 다중 청크·파일 변경 요약 지도(Change Map) 결합
   ▼
FlowAnalyzer.enrich()                crex/flow.py
   │  ⑤ 저장소 내 미수정 인접 파일 심볼 탐색 (최대 40개 파일)
   │  ⑥ 다단계 호출 그래프(Caller → 변경 함수 → Callee 1~2 hop) 접힌 실행 경로 슬라이스 생성
   │  ⑦ 결정론적 교차 함수 흐름 위험 신호(Flow Hazards) 탐지 및 주입
   │  ReviewChunk[]
   ▼
GroundingGate.collect()              crex/ground.py
   │  정적분석기 병렬 실행 (기본 6종, 전체 8종) → StaticFinding[]
   │  attach()로 라인 범위가 겹치는 청크에 배정 (변경 라인 지적 우선 정렬)
   ▼
RuleChecker.review()                 crex/generate.py
   │  청크당 LLM 1회 호출, 컴포넌트별 토큰 예산 배분 및 JSON Schema enum 강제
   │  Finding[]
   ▼
ReviewFilter.filter()                crex/filter.py
   │  ① 결정론적 검증 (LLM 호출 없이 범위 밖·미변경 라인·제외 룰·중복 즉시 기각)
   │  ② 교차 모델 재판정 (±25줄 집중 윈도우 + 창 외부 연관 심볼 정의 스니펫 확장)
   │  Finding[] (유지) + FilterVerdict[] (기각)
   ▼
report.write_all()                   crex/report.py
   │  Markdown / SARIF / JSON / HTML 리포트 출력 (+ .crex/viz.db SQLite 이력 저장)
```

---

## 청킹 및 교차 흐름 문맥 구성

### 왜 diff hunk를 그대로 쓰지 않나

변경된 세 줄만 모델에 보여주면 함수 시그니처나 앞선 변수 초기화 상태를 알 수 없어 모델이 추측에 의존하게 됩니다. 반대로 수천 줄짜리 파일 전체를 통째로 넣으면 소형 모델의 주의력이 분산되어 정밀도가 급격히 떨어집니다([설정 문서](configuration.md#입력-토큰-상한을-왜-8192-로-두나) 참고).

따라서 인간 리뷰어가 코드를 읽는 단위와 동일하게 **변경된 라인을 감싸는 함수·메서드 경계**까지만 확장합니다.

### 확장 상한 및 안전 분할 알고리즘

수백 줄짜리 거대 함수를 만났을 때 컨텍스트가 폭주하지 않도록 5단계 방어 장치를 적용합니다.

1. **배수 기반 절단**: 심볼 경계까지 확장한 길이가 원본 hunk의 4배(`expansion_limit`)를 초과하면 hunk를 중심으로 위아래 대칭이 되도록 3배(`expansion_truncate`) 길이로 잘라냅니다(BitsAI-CR 프로덕션 기준값).
2. **절대 라인 수 상한 (`absolute_max_lines`, 기본 150줄)**: 배수와 무관하게 단일 청크가 가질 수 있는 최대 라인 수를 제한합니다.
3. **토큰 예산 연동 자동 상한 (`calculate_safe_max_lines`)**: `generator.max_input_tokens`에서 시스템 프롬프트·룰·AST·정적분석 오버헤드를 제외한 안전 라인 수를 계산하여 설정 불일치 시 자동으로 상한을 낮춥니다(Auto-Clamping).
4. **병합 상한 검사 (`_merge_ranges`)**: 인접한 여러 hunk를 하나로 합칠 때 합산 줄 수가 상한을 초과하면 병합하지 않고 독립 청크로 유지합니다.
5. **거대 범위 군집 분할 (`_partition_oversized_range`)**: 단일 hunk 자체가 상한을 넘으면 변경 라인 군집을 기준으로 `±6`줄 문맥을 유지하며 여러 청크로 균등 분할합니다.

### 심볼 경계 탐색 (`SymbolLocator`)

`SymbolLocator`는 `tree-sitter` 파서를 우선 사용하고, 패키지가 설치되어 있지 않으면 언어별 휴리스틱 폴백으로 동작합니다.

- **Tree-sitter 경로**: 변경 범위를 완전히 감싸는 AST 심볼 노드 중 **가장 작은 노드**(클래스보다 메서드, 메서드보다 람다 우선)를 선택합니다.
- **휴리스틱 폴백**:
  - **Python**: 들여쓰기 수준을 추적하여 상위 `def`/`class` 선언부터 블록이 끝나는 지점까지 찾습니다.
  - **C++ / C#**: 문자열 리터럴과 주석 내부의 중괄호를 제거한 뒤 중괄호 깊이를 추적하여 함수 본문 경계를 찾습니다.

### 3단계 교차 흐름(Cross-Flow) 문맥 주입

청크 분할로 인해 함수·파일 경계 너머의 흐름을 놓치는 문제를 해결하기 위해, 추가 LLM 호출 없이 결정론적 정적 분석으로 3가지 교차 문맥을 추출합니다.

1. **Stage 1 — AST 심볼 계약(`SymbolContract`) 및 변경 요약 지도(`Change Map`)**:
   - `TreeSitterAnalyzer.extract_file_symbols()`가 파일 내 모든 함수·클래스의 시그니처, 반환 표현식(`returns`), 예외(`throws`), 자원/락 연산(`resource_ops`), 클래스 멤버 필드(`member_fields`), 정의 스니펫(`symbol_definitions`)을 추출합니다.
   - `attach_cross_chunk_context()`가 이번 리뷰에서 함께 수정된 다른 청크·파일들의 1줄 변경 요약 지도(`Change Map`)와 교차 파일 피호출 계약을 각 청크에 부착합니다.
2. **Stage 2 — 검증기 창 외부 심볼 정의 확장 (`ReviewFilter._resolve_related_context`)**:
   - 검증 모델이 지적 라인 주변 `±25`줄 윈도우만 볼 때 창 바깥(다른 함수나 다른 파일)에 정의된 피호출 함수의 계약을 확인하지 못해 정당한 교차 함수 지적을 기각하거나 오탐을 통과시키는 문제를 막기 위해, 관련도 점수가 높은 상위 최대 3개의 창 외부 심볼 정의 스니펫을 검증 프롬프트에 함께 첨부합니다.
3. **Stage 3 — 호출 그래프 실행 경로 슬라이싱 및 흐름 위험 신호 (`FlowAnalyzer`)**:
   - `crex/flow.py`의 `FlowAnalyzer`가 diff에 포함되지 않은 저장소 내 미수정 인접 파일(최대 40개)까지 탐색해 미해결 피호출 함수(Callee)와 변경된 함수를 호출하는 상위 호출부(Caller)를 찾습니다.
   - `Caller → 변경 함수 → Callee (1~2 hop)` 호출 체인을 구성하고, 중간 일반 대입문은 `// ... (중간 생략)`으로 접고 분기·자원/락·호출·반환/예외문만 남긴 실행 경로 슬라이스를 생성합니다.
   - 널/오류 반환 미검사 역참조, 예외 전파 시 선행 자원/락 누수, 중첩 락 호출, 상위 호출부 계약 불일치를 대조하여 `- 교차 함수 흐름 위험 신호(Flow Hazards):`로 주입합니다.

### 라인 상태 주석 (`DiffLine.annotate`)

```
[unchanged @14]   void Grow(size_t extra) {
[deleted @15]     data_.resize(data_.size() + extra);
[added @15]     int* raw = &data_[0];
[added @16]     data_.resize(data_.size() + extra);
[added @17]     raw[0] = 42;
[unchanged @18]   }
```

모든 코드 줄 앞에 변경 상태와 실제 라인 번호를 명시하여 모델이 줄 번호를 직접 세거나 추론할 필요를 없앱니다.

### 소스 정합성 검증 (`_verify_source`)

diff가 기술하는 줄 내용과 디스크의 실제 파일 줄 내용을 대조합니다. 한 줄이라도 어긋나면 기본적으로 `DiffSourceMismatch` 예외를 발생시키고 해당 파일을 건너뛰어, 밀린 라인 번호로 인해 엉뚱한 줄을 지적하는 사고를 원천 방지합니다.

---

## 정적분석 그라운딩 (`crex/ground.py`)

모든 정적분석 어댑터는 `Analyzer` 추상 클래스를 상속하며 `build_command()`와 `parse()` 두 메서드만 구현합니다.

- 실행 파일이 `PATH`에 없거나 타임아웃·`OSError`·파싱 오류가 발생하면 모두 `AnalyzerResult(skipped=True)`로 안전하게 흡수하여 분석기 하나 때문에 전체 파이프라인이 중단되지 않도록 보장합니다.
- 수집된 `StaticFinding`은 `GroundingGate.attach()`를 통해 파일 경로 접미사와 라인 범위가 일치하는 청크에 배정되며, 실제 변경된 라인(`changed_linenos`)에 해당하는 지적이 우선적으로 프롬프트에 실립니다.

---

## 지적 생성 (`crex/generate.py`)

### JSON Schema `enum` 제약

`build_findings_schema()`가 청크마다 동적으로 생성하는 스키마는 다음과 같습니다.

```python
{
  "line": {"type": "integer", "enum": [15, 16, 17]},   # 이 청크에서 실제 added된 라인만 허용
  "rule_id": {"type": "string", "enum": ["cpp.dangling-after-realloc", ...]},
  "severity": {"type": "string", "enum": ["high", "medium", "low"]},
  ...
}
```

vLLM의 Guided Decoding은 스키마에 어긋나는 토큰의 생성 확률을 마스킹합니다. 따라서 모델이 변경되지 않은 14번 줄이나 존재하지 않는 룰 ID를 출력하는 것 자체가 불가능해집니다.

### 심각도 상한 클램핑 (`_severity_of`)

모델이 자신이 찾은 지적의 심각도를 과장하지 못하도록, 각 룰의 택소노미 정의 `severity`를 상한선으로 적용합니다.

---

## 교차 검증 필터 (`crex/filter.py`)

### 2단계 필터링

1. **결정론적 검증 (`_check_deterministic`)**: LLM 호출 없이 즉시 검사합니다.
   - 제외 규칙(`.crexignore` / `exclude_rules`)에 해당하는 룰 → `RULE_EXCLUDED`
   - 지적이 속한 청크가 없거나 청크 라인 범위를 벗어남 → `LINE_OUT_OF_RANGE`
   - `require_changed_line=True`일 때 변경되지 않은 라인을 지적함 → `LINE_NOT_CHANGED`
   - 동일 파일·라인·룰 중복 지적 → `DUPLICATE`
2. **교차 모델 재판정 (`_verify_llm`)**: 결정론적 검사를 통과한 지적만 별도 검증 모델로 보냅니다. 지적 라인 주변 `±25`줄 집중 윈도우와 창 외부 연관 심볼 정의(`_resolve_related_context`)를 함께 제공하여, 생성 모델의 프롬프트 분위기에 휩쓸리지 않고 객관적으로 타당성을 재판정합니다.

### Conclusion-First 스키마

검증 스키마의 프로퍼티 순서는 생성 순서와 직결됩니다.

```python
VERDICT_SCHEMA = {
    "properties": {
        "verdict": {...},        # ← 결론(yes/no)을 가장 먼저 생성
        "code_present": {...},
        "reason": {...},         # ← 판정 사유와 코멘트는 뒤에 생성
    },
}
```

결론 토큰(`verdict`)을 먼저 출력하게 하고 근거(`reason`)를 뒤에 붙이는 방식(BitsAI-CR의 Conclusion-First 구조)으로 지연 시간과 정밀도를 동시에 확보합니다.

또한 검증 모델이 `verdict: "yes"`를 반환하더라도 `code_present: false`("지적이 설명하는 코드가 스니펫에 존재하지 않음")이면 즉시 `CODE_NOT_FOUND`로 기각합니다. 검증 API 호출 자체가 실패한 경우에도 보수적으로 기각(`FILTER_ERROR`)하여 검증되지 않은 지적이 리포트에 섞이지 않도록 방어합니다.

---

## MCP 계층 분리 (`crex/service.py` / `crex/mcp.py`)

- `crex/service.py` (`ReviewService`): 7종 MCP 도구의 실제 비즈니스 로직을 모두 담고 있으며 **`fastmcp`를 임포트하지 않습니다.**
- `crex/mcp.py`: `FastMCP` 데코레이터 바인딩과 전송 계층(`stdio` / `Streamable HTTP`)만 담당합니다.

이 분리 덕분에 `fastmcp`가 설치되지 않은 순수 표준 라이브러리 환경에서도 `ReviewService`의 모든 동작을 100% 단위 테스트할 수 있습니다.

---

## 왜 외부 의존성을 최소화했나

폐쇄망 환경에서는 외부 패키지 하나를 반입할 때마다 하위 의존성 전체의 보안 심사와 해시 검증을 거쳐야 합니다. 따라서 CREX 코어는 Python 3.11+ 표준 라이브러리만으로 완결되도록 설계했습니다.

- HTTP 클라이언트: `urllib.request`
- 데이터 모델: `dataclasses`
- 설정 파일 파서: `json` (`//` 주석 키 및 다중 줄 배열 지원)
- 룰 택소노미 파서: `tomllib`
- 관제 화면 영구 저장소: `sqlite3` (`.crex/viz.db`)
- 관제 화면 웹 서버: `http.server.ThreadingHTTPServer` (`uvicorn` 선택 지원)
- 병렬 실행: `concurrent.futures.ThreadPoolExecutor`
- 테스트 프레임워크: 자체 경량 러너 (`tests/run_all.py`)

---

## 테스트 스위트 구조

```bash
python tests/run_all.py
```

| 테스트 모듈 | 검증 영역 |
|---|---|
| `test_chunk.py` | diff 파싱, 심볼 경계 확장, 거대 청크 분할, 라인 주석, 파일 정합성 검증 |
| `test_proactive_review.py` | Tree-sitter AST 계약, 교차 청크 Change Map, 검증기 문맥 확장, 호출 그래프 슬라이싱 및 흐름 위험 신호 |
| `test_filter.py` | 결정론적 기각, 교차 모델 판정, 보수적 오류 기각 |
| `test_ground.py` | 8종 정적분석기 출력 파서 및 설정별 활성화 규칙 |
| `test_pipeline.py` | 실제 임시 Git 저장소 + 모의 vLLM(`fake_vllm.py`) 전 구간 통합 검증 |
| `test_viz.py` | 관제 화면 이벤트 추적, SQLite DB 영속성, `.crexignore` API, HTTP/ASGI 서버 |
| `test_cli.py` | CLI 인자 우선순위, CP949 콘솔 인코딩, 합쇼체 문장 규칙, PowerShell BOM 검증 |
| `test_config.py` | JSON 설정 주석 키 제거, 배열 줄바꿈 병합, 오타 검증 |
| `test_workspace.py` | 워크스페이스 해석 우선순위, 런타임 전환, `crex.json` 고정 |
| `test_compiledb.py` | CMake/MSBuild 컴파일 DB 생성 및 로거 연동 |
| `test_docs.py` | 사용 설명서 마크다운→HTML 렌더러, GitHub 슬러그 호환성, 내부 앵커 링크 무결성 |
| `test_eval.py` | 골든셋 KBI / FAR / 룰별 정밀도 산출 |

---

## 코드 수정 시 반드시 지켜야 할 불변식

1. **검증 스키마 프로퍼티 순서**: `VERDICT_SCHEMA`에서 `verdict`를 항상 첫 번째 프로퍼티로 유지하십시오.
2. **`RejectReason` 열거형 하위 호환성**: 새로운 기각 사유를 추가할 수는 있지만 기존 값의 이름을 바꾸지 마십시오(과거 평가 리포트와의 비교 호환성 유지).
3. **룰 ID 불변성**: [룰 작성법](writing-rules.md#id-는-절대-바꾸지-마세요)에 설명된 대로 기존 룰 ID는 변경하지 마십시오.
4. **사용자 대면 문자열 존댓말 규칙**: `crex/` 및 `crex/viz/web/` 내 사용자에게 노출되는 런타임 한국어 문자열은 합쇼체/해요체 규칙을 지켜야 합니다(`test_cli.py::test_user_facing_text_is_written_in_polite_form`이 자동 검사합니다).
5. **한글 PowerShell 스크립트 UTF-8 BOM 유지**: 한글이 포함된 `.ps1` 파일은 반드시 UTF-8 BOM(`EF BB BF`)을 유지해야 합니다(`test_cli.py::test_powershell_scripts_carry_a_bom`이 자동 검사합니다).
