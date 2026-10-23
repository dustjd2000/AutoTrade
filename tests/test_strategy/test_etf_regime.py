from datetime import date, timedelta

from src.strategy import etf_regime
from src.strategy.etf_regime import (
    INVERSE_ETF,
    LONG_ETF,
    LOOKBACK_DAYS,
    ETFRegimeStrategy,
    intraday_returns,
    pick_etf,
    regime_signal,
    traded_today,
)
from src.strategy.base import StockRecommendation

TODAY = date(2026, 10, 23)


def candle(day: date, open_price: float, close_price: float, volume: int = 1000):
    # 키움 응답처럼 부호가 붙은 문자열로 준다
    return {
        "dt": day.strftime("%Y%m%d"),
        "open_pric": f"+{open_price:.0f}",
        "close_pric": f"-{close_price:.0f}",
        "trde_qty": str(volume),
    }


def past_candles(count: int, change_percent: float, start: date = TODAY):
    """오늘 이전 `count`거래일 — 매일 장중 수익률이 `change_percent`%인 봉."""
    rows = []
    day = start
    while len(rows) < count:
        day -= timedelta(days=1)
        rows.append(candle(day, 10000, 10000 * (1 + change_percent / 100)))
    return rows


def test_intraday_returns_skips_today_and_counts_each_day_once():
    """당일 봉은 진행 중인 값이라 빼고, 두 조회를 합쳐 넘겨도 같은 날짜는 한 번만 센다."""
    past = past_candles(25, 1.0)
    today_bar = candle(TODAY, 10000, 9000)

    returns = intraday_returns([today_bar] + past[:19] + past, TODAY)

    assert len(returns) == LOOKBACK_DAYS
    assert all(abs(r - 1.0) < 1e-9 for r in returns)


def test_intraday_returns_takes_the_most_recent_days():
    recent = past_candles(20, 0.5)
    older = past_candles(10, -3.0, start=TODAY - timedelta(days=60))

    returns = intraday_returns(older + recent, TODAY)

    assert all(abs(r - 0.5) < 1e-9 for r in returns)


def test_regime_signal_is_none_when_history_is_short():
    """봉이 모자라면 판단하지 않는다 — 적은 표본으로 방향을 정하면 안 된다."""
    assert regime_signal(past_candles(LOOKBACK_DAYS - 1, 1.0), TODAY) is None


def test_regime_signal_is_mean_of_intraday_returns():
    rows = past_candles(10, 1.0) + past_candles(10, -0.5, start=TODAY - timedelta(days=10))
    assert abs(regime_signal(rows, TODAY) - 0.25) < 1e-9


def test_pick_etf_follows_the_signal_direction_beyond_the_threshold():
    assert pick_etf(0.2) == LONG_ETF
    assert pick_etf(0.6) == LONG_ETF
    assert pick_etf(-0.2) == INVERSE_ETF
    assert pick_etf(-0.6) == INVERSE_ETF


def test_pick_etf_rests_inside_the_threshold_or_without_signal():
    assert pick_etf(0.19) is None
    assert pick_etf(-0.19) is None
    assert pick_etf(0.0) is None
    assert pick_etf(None) is None


def test_traded_today_needs_a_today_bar_with_volume():
    assert traded_today([candle(TODAY, 1, 1, volume=10)], TODAY) is True
    assert traded_today([candle(TODAY, 1, 1, volume=0)], TODAY) is False
    assert traded_today(past_candles(5, 1.0), TODAY) is False


def test_strategy_invests_the_whole_ratio_in_one_etf():
    strategy = ETFRegimeStrategy(investable_ratio=0.5)
    strategy.set_recommendations(
        [StockRecommendation(ticker=LONG_ETF[0], name=LONG_ETF[1], target_price=30000, reason="r")]
    )

    plans = strategy.build_buy_plans(1_000_000)

    assert strategy.target_stock_count == 1
    assert [(p.ticker, p.amount) for p in plans] == [(LONG_ETF[0], 500_000)]


def test_etf_tickers_are_the_two_etfs():
    assert etf_regime.ETF_TICKERS == ("069500", "114800")
