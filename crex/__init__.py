"""CREX — 폐쇄망 sLLM 코드리뷰 파이프라인.

생성(RuleChecker)과 검증(ReviewFilter)을 분리한 2단계 구조로, 소형 모델의
환각을 구조적으로 걸러낸다.

## 버전은 여기 한 줄이 전부다

CLI(`--version`, `doctor`), SARIF 리포트의 `tool.driver.version`, 관제 화면의
`/api/config`, MCP 서버가 클라이언트에 알리는 서버 버전, 반입 번들의 매니페스트가
전부 이 값을 읽는다. 어딘가에 숫자를 또 적으면 그 순간부터 둘 중 어느 쪽이 진짜인지
알 수 없게 된다 — 리포트에 찍힌 버전으로 "그때 뭘로 돌렸나"를 되짚는 것이 목적인데
그 값이 거짓이면 아무 쓸모가 없다.

사람이 손으로 맞춰야 하는 곳은 `README.md` 하나뿐이고, 그것도 테스트가 대조한다
(`tests/test_cli.py::test_version_declared_in_one_place`).

룰 택소노미의 버전(`rules/taxonomy.toml` 의 `meta.version`)은 이것과 **별개다.**
룰은 평가 리포트와 플라이휠 통계를 몇 달에 걸쳐 잇는 키라 자기 수명을 따로 가진다.
둘을 하나로 합치지 마라.
"""

__version__ = "0.2"
