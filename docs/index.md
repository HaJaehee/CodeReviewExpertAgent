# CREX 사용 설명서

폐쇄망에 있는 25~40B급 로컬 모델로 C++/C#/Python 코드를 리뷰하는 도구입니다.

처음이라면 [시작하기](getting-started.md)부터 보세요. 설치하고 첫 리뷰를 돌려서
결과를 읽는 데까지 30분이면 됩니다.

Zed 을 쓴다면 [워크플로](workflow.md)가 주 사용 문서입니다. 어떤 말이 어떤 도구를
부르는지, 지적을 받았을 때 뭘 하는지를 다룹니다.

| 문서 | 언제 보나 |
|---|---|
| [시작하기](getting-started.md) | 설치, 첫 실행, 결과 읽는 법 |
| [워크플로](workflow.md) | Zed 에서 리뷰 부르기, 지적 받았을 때 |
| [설정](configuration.md) | `crex.toml` 의 모든 항목과 손대야 할 때 |
| [정적분석 도구](analyzers.md) | clang-tidy·cppcheck·ruff 설치, 라이선스, 반입 |
| [룰 작성법](writing-rules.md) | 오탐을 늘리지 않고 룰을 추가하려면 |
| [평가와 튜닝](evaluation.md) | 골든셋 만들기, KBI/FAR 해석, 룰 폐기 |
| [관제 화면](visualizer.md) | 두 모델의 프롬프트·응답·판정을 웹에서 지켜보기 |
| [반입](transfer.md) | Python 런타임까지 담은 번들 만들기·검증 |
| [운영](operations.md) | vLLM 기동, Zed 연동, 일상 운영 |
| [문제 해결](troubleshooting.md) | 증상별 원인과 조치 |
| [동작 원리](internals.md) | 내부 구조와 설계 의도 |

## 30초 요약

```bash
cp crex.example.toml crex.toml  # 엔드포인트 주소만 고치면 됩니다
python -m crex doctor           # 무엇이 되고 무엇이 안 되는지 확인
python -m crex review --staged  # 스테이징된 변경 리뷰
```

리뷰 결과는 기본적으로 stdout 에 마크다운으로 나옵니다. 파일로 받으려면
`--out reports/` 를 붙이세요. 마크다운, SARIF, JSON 세 가지가 함께 나옵니다.

CREX 를 리뷰 대상 저장소 안에 둘 필요는 없습니다. 설치본은 한 자리에 두고
`--workspace` 로 대상만 가리킵니다 —
[시작하기](getting-started.md#crex-는-어디에-두나) 참고.

```bash
python -m crex review --workspace D:\work\myrepo --staged
```

Zed 을 쓴다면 에이전트 패널에서 바로 부를 수도 있습니다 —
[운영 문서의 Zed 연동](operations.md#zed-연동-mcp)을 보세요. 여러 사람이 서버 하나를
같이 쓰거나 클라이언트가 다른 장비에 있다면 `python -m crex.mcp --transport http` 로
[Streamable HTTP 엔드포인트](operations.md#streamable-http-엔드포인트)를 엽니다.

리뷰가 왜 그런 결과를 냈는지 들여다보려면 [관제 화면](visualizer.md)을 띄우세요.
두 모델이 주고받은 프롬프트와 응답이 그대로 보입니다.

```bash
python -m crex.viz               # http://127.0.0.1:18765
```

## 이 도구가 하지 않는 것

코드를 고쳐주지 않습니다. 지적만 하고 수정안을 제시할 뿐, 파일을 건드리지 않습니다.

전체 저장소를 이해하지 못합니다. 리뷰 단위는 변경된 함수 하나이고, 그 밖의
파일은 보지 않습니다. "이 함수를 호출하는 쪽에서 널을 넘길 수 있다" 같은 지적은
원리적으로 나오지 않습니다. 이건 한계이자 의도입니다 — 소형 모델에게 저장소
전체를 뒤지게 하면 정확도가 무너집니다.

스타일을 지적하지 않습니다. 들여쓰기, 명명 규칙, 주석 유무 같은 건 포매터와
린터가 할 일입니다. 여기서는 동작에 영향을 주는 결함만 봅니다.
