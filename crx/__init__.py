"""crx — 폐쇄망 sLLM 코드리뷰 파이프라인.

생성(RuleChecker)과 검증(ReviewFilter)을 분리한 2단계 구조로, 소형 모델의
환각을 구조적으로 걸러낸다.
"""

__version__ = "0.1.0"
