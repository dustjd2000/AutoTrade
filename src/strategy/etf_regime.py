"""2호 전략 — 지수 ETF 국면 추종 당일 매매 (PRD 5.5-C, 실험 단계).

직전 20거래일 KODEX 200의 장중 수익률(시가→종가) 평균이 +0.2% 이상이면 KODEX 200을,
-0.2% 이하이면 KODEX 인버스를 산다. 그 사이면 쉰다. 매수·손절·15:15 청산은 1호와 같은
경로를 쓰고, 여기서는 오늘 무엇을 살지만 정한다.

근거는 2024년 이후 국면뿐이다 — 1999~2026 장기로는 손실인 규칙이라(PRD 5.5-C '근거와
한계') 자동 중단 장치(`DailyWorkflow._etf_kill_reason`)와 함께 돈다.
"""
import logging
from datetime import date
from statistics import mean
from typing import Dict, Iterable, List, Optional, Tuple

from src.api.client import to_float
from src.strategy.llm_momentum import LLMMomentumStrategy

logger = logging.getLogger(__name__)

LONG_ETF: Tuple[str, str] = ("069500", "KODEX 200")
INVERSE_ETF: Tuple[str, str] = ("114800", "KODEX 인버스")
ETF_TICKERS = (LONG_ETF[0], INVERSE_ETF[0])

# 판단 구간과 문턱 — 둘 다 2026-10-22 검증에서 고른 값이다. 문턱이 없으면(매일 매매)
# 2024~2026에서도 연 -4%였다 (PRD 5.5-C).
LOOKBACK_DAYS = 20
MIN_SIGNAL_PERCENT = 0.2


def _candle_day(candle: Dict) -> str:
    return str(candle.get("dt") or candle.get("date") or "").strip()


def _price(candle: Dict, *keys: str) -> float:
    for key in keys:
        value = candle.get(key)
        if value is not None and str(value).strip() != "":
            # 키움은 가격에 등락 방향 부호를 붙여 보낸다
            return abs(to_float(value))
    return 0.0


def intraday_returns(candles: Iterable[Dict], today: date) -> List[float]:
    """오늘 이전 일봉들의 장중 수익률(%) — 최근 날짜부터 `LOOKBACK_DAYS`개까지.

    당일 봉은 뺀다 — 장중이면 아직 진행 중인 값이다 (`get_previous_day_metrics`와 같은 이유).
    시가나 종가가 0인 봉(거래 없음)은 건너뛴다. 기준일을 달리한 여러 조회를 합쳐 넘겨도 되도록
    같은 날짜는 한 번만 센다 — 한 번 조회가 20거래일치라 당일 봉이 섞이면 지난 봉이 19개뿐이다.
    """
    today_str = today.strftime("%Y%m%d")
    by_day = {_candle_day(c): c for c in candles if _candle_day(c) and _candle_day(c) < today_str}
    returns = []
    for day in sorted(by_day, reverse=True):
        candle = by_day[day]
        open_price = _price(candle, "open_pric")
        close_price = _price(candle, "close_pric", "cur_prc")
        if open_price <= 0 or close_price <= 0:
            continue
        returns.append((close_price / open_price - 1) * 100)
        if len(returns) == LOOKBACK_DAYS:
            break
    return returns


def regime_signal(candles: Iterable[Dict], today: date) -> Optional[float]:
    """직전 `LOOKBACK_DAYS`거래일 장중 수익률 평균(%). 봉이 모자라면 None — 판단하지 않는다."""
    returns = intraday_returns(candles, today)
    if len(returns) < LOOKBACK_DAYS:
        logger.warning("2호 전략 신호 계산 불가 — 지난 일봉 %d개 (필요 %d개)", len(returns), LOOKBACK_DAYS)
        return None
    return mean(returns)


def pick_etf(signal: Optional[float]) -> Optional[Tuple[str, str]]:
    """신호로 오늘 살 ETF를 정한다. 문턱 안쪽이거나 신호가 없으면 None(쉼)."""
    if signal is None:
        return None
    if signal >= MIN_SIGNAL_PERCENT:
        return LONG_ETF
    if signal <= -MIN_SIGNAL_PERCENT:
        return INVERSE_ETF
    return None


def traded_today(candles: Iterable[Dict], today: date) -> bool:
    """응답에 당일 봉이 있고 거래량이 0보다 큰지 — 아니면 휴장으로 본다 (PRD 5.5-C '휴장 판정')."""
    today_str = today.strftime("%Y%m%d")
    for candle in candles:
        if _candle_day(candle) == today_str:
            return to_float(candle.get("trde_qty") or candle.get("acml_vol") or 0) > 0
    return False


class ETFRegimeStrategy(LLMMomentumStrategy):
    """2호 전략. 매수 계획·선택 삭제는 1호와 같아 그대로 물려받고, 종목 수만 1로 고정한다.

    오늘의 종목은 `DailyWorkflow`가 추천 시각에 `pick_etf`로 정해 `set_recommendations`로 넣는다.
    """

    def __init__(self, investable_ratio: float = 0.5):
        super().__init__(investable_ratio=investable_ratio, target_stock_count=1)

    @property
    def name(self) -> str:
        return "ETFRegimeStrategy"
