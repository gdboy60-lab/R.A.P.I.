"""설정값 (명세서 NFR-05: 요율은 코드가 아닌 설정으로 관리).

아래 수수료·세율은 개발용 임시값이다. 시연 전에 최신 기준을 확인해서 바꾼다.
- 증권사 매매 수수료는 증권사·상품마다 다르다.
- 증권거래세(농어촌특별세 포함)는 세법 개정으로 해마다 바뀔 수 있다.
체결 시 계산한 금액이 executions 에 그대로 저장되므로, 값을 바꿔도 과거 거래 손익은 변하지 않는다.
"""
BUY_FEE_RATE = 0.00015      # 매수 수수료 0.015% (임시값)
SELL_FEE_RATE = 0.00015     # 매도 수수료 0.015% (임시값)
SELL_TAX_RATE = 0.0020      # 매도 세금 0.20% (임시값, 시연 전 확인)

INITIAL_CASH = 10_000_000
WATCHLIST_MAX = 20

DATA_SOURCE_NOTE = "시세 출처: 금융위원회, 공공데이터포털 (과거 일봉 기반 리플레이)"
DEV_DATA_NOTE = "개발용 가상 시세"

# AI 피드백 (명세서 11장)
# API 키는 코드에 넣지 않는다. 환경 변수 ANTHROPIC_API_KEY 로 받는다 (Codespaces 비밀값).
# 키가 없거나 호출이 실패하면 규칙 기반 기본 피드백으로 대신한다.
AI_MODEL = "claude-haiku-5-5"
AI_MAX_ATTEMPTS = 3          # 최초 1회 + 재시도 2회
AI_TIMEOUT_SECONDS = 20
AI_MAX_TOKENS = 1000
