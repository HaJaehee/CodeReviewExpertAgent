# CREX 사용 설명서

폐쇄망 환경의 25~40B 규모 로컬 모델로 C++, C#, Python 코드를 리뷰하는 도구입니다.

처음 사용하신다면 [시작하기](getting-started.md)부터 읽어 보십시오. 설치부터 첫 리뷰를 실행하고 결과를 확인하는 데까지 30분이면 충분합니다.

Zed 에디터를 사용하신다면 [워크플로](workflow.md)가 주 사용 문서입니다. 어떤 요청이 어떤 도구를 호출하는지, 리뷰 지적을 받았을 때 어떻게 대응해야 하는지를 다룹니다.

| 문서 | 다루는 내용 |
|---|---|
| [시작하기](getting-started.md) | 설치, 첫 실행, 리포트 읽는 법 |
| [워크플로](workflow.md) | Zed에서 리뷰 호출하기, 지적 사항 대응 절차 |
| [설정](configuration.md) | `crex.json`의 모든 설정 항목과 조정 기준 |
| [리뷰 대상 제외](crexignore.md) | `.crexignore`로 파일 및 룰 제외하기 |
| [정적분석 도구](analyzers.md) | `clang-tidy`·`cppcheck`·`ruff` 설치, 라이선스, 반입 절차 |
| [룰 작성법](writing-rules.md) | 오탐을 늘리지 않고 새 룰을 추가하는 방법 |
| [평가와 튜닝](evaluation.md) | 골든셋 구축, KBI/FAR 지표 해석, 저품질 룰 폐기 |
| [관제 화면](visualizer.md) | 생성·검증 모델의 프롬프트, 응답, 판정 내역을 웹에서 확인 |
| [반입](transfer.md) | Python 런타임까지 포함한 오프라인 번들 생성 및 검증 |
| [운영](operations.md) | vLLM 서버 기동, Zed 연동, 일상 운영 가이드 |
| [문제 해결](troubleshooting.md) | 자주 발생하는 증상별 원인과 해결 방법 |
| [동작 원리](internals.md) | 파이프라인 내부 구조와 설계 의도 |

## 30초 요약

```bash
cp crex.example.json crex.json  # 엔드포인트 주소와 모델명만 수정하면 됩니다
python -m crex doctor           # 연결 및 분석기 동작 상태 확인
python -m crex review --staged  # 스테이징된 변경 사항 리뷰
```

리뷰 결과는 기본적으로 표준 출력(`stdout`)에 마크다운 형식으로 출력됩니다. 파일로 저장하려면 `--out reports/` 옵션을 붙이십시오. 마크다운, SARIF, JSON, HTML 네 가지 형식이 함께 생성됩니다.

CREX를 리뷰 대상 저장소 안에 설치할 필요는 없습니다. 설치본은 한곳에 두고 `--workspace` 옵션으로 대상 저장소만 지정하면 됩니다 — 자세한 내용은 [시작하기](getting-started.md#crex-는-어디에-두나)를 참고하십시오.

```bash
python -m crex review --workspace D:\work\myrepo --staged
```

Zed 에디터를 사용하신다면 에이전트 패널에서 바로 리뷰를 요청할 수도 있습니다 — [운영 문서의 Zed 연동](operations.md#zed-연동-mcp)을 참고하십시오. 여러 개발자가 서버 한 대를 공유하거나 클라이언트가 다른 장비에 있다면 `python -m crex.mcp --transport http` 명령으로 [Streamable HTTP 엔드포인트](operations.md#streamable-http-엔드포인트)를 열 수 있습니다.

리뷰 결과가 나온 과정과 근거를 상세히 살펴보려면 [관제 화면](visualizer.md)을 실행하십시오. 생성·검증 모델이 주고받은 프롬프트와 응답 원문을 그대로 확인할 수 있습니다.

```bash
python -m crex.viz               # http://127.0.0.1:18765
```

## 이 도구가 하지 않는 것

코드를 직접 수정하지 않습니다. 결함을 지적하고 수정 예시 코드(`suggestion`)를 제시할 뿐, 작업 트리의 파일을 변경하지는 않습니다.

저장소 전체를 무제한으로 탐색하지 않습니다. 기본 리뷰 단위는 변경된 함수(청크)이며, 여기에 Tree-sitter AST 심볼 계약·변경 요약 지도(Change Map)·인접 파일 호출 그래프 슬라이스(Call-Graph Flow)를 결합해 교차 함수 결함을 점검합니다. 소형 모델에게 저장소 전체를 자율 탐색하게 하면 컨텍스트가 폭주하고 정확도가 급격히 떨어지기 때문에, 검증된 정적 문맥만 선별하여 주입합니다.

코딩 스타일은 지적하지 않습니다. 들여쓰기, 명명 규칙, 주석 유무 같은 스타일 검사는 포매터와 린터의 역할입니다. CREX는 실행 동작과 안전성에 영향을 주는 실제 결함에 집중합니다.
