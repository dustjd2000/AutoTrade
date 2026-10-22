"""2호 전략(지수 ETF 국면 추종)의 아침 결정 분기 (PRD 5.5-C).

1호와 갈리는 곳은 추천 단계와 15:35 검증뿐이다 — 매수·청산은 같은 경로라 여기서 다시 보지 않는다.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from config.settings import STRATEGY_ETF_REGIME
from src.core import daily_workflow
from src.core.daily_workflow import DailyWorkflow
from src.core.events import MarketData
from src.strategy.etf_regime import INVERSE_ETF, LONG_ETF, ETFRegimeStrategy

TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_workflow, "DEFAULT_REPORT_MARK_PATH", tmp_path / "final_report_sent")
    monkeypatch.setattr(daily_workflow, "DEFAULT_BUY_RECORDS_PATH", tmp_path / "buy_records.json")


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None, images=None):
        self.sent.append((subject, message))


def candles(change_percent, days=20, traded=True):
    """오늘 봉 + 지난 `days`거래일 봉 (장중 수익률이 매일 `change_percent`%)."""
    rows = [{"dt": TODAY.strftime("%Y%m%d"), "open_pric": "10000", "close_pric": "10000",
             "trde_qty": "500" if traded else "0"}]
    for i in range(1, days + 1):
        day = TODAY - timedelta(days=i)
        rows.append({
            "dt": day.strftime("%Y%m%d"),
            "open_pric": "10000",
            "close_pric": f"{10000 * (1 + change_percent / 100):.0f}",
            "trde_qty": "1000",
        })
    return rows


class FakeMarketData:
    def __init__(self, rows, price=30000.0):
        self.rows = rows
        self.price = price
        self.ohlcv_calls = []
        self.price_calls = []

    def get_ohlcv(self, ticker, period="D", count=100, base_date=None):
        self.ohlcv_calls.append((ticker, base_date))
        return list(self.rows)

    def get_current_price(self, ticker):
        self.price_calls.append(ticker)
        return MarketData(ticker=ticker, price=self.price, volume=100)


def make_workflow(rows, returns=(), kill_percent=5.0, start=None):
    email = FakeEmail()
    notifications, closed = [], []
    market_data = FakeMarketData(rows)
    strategy = ETFRegimeStrategy(investable_ratio=0.5)
    store_calls = []

    def closed_trade_returns(start_day, up_to, tickers):
        store_calls.append((start_day, up_to, tuple(tickers)))
        return list(returns)

    engine = SimpleNamespace(
        market_data=market_data,
        notify=notifications.append,
        note_market_closed=closed.append,
    )
    fail = lambda *a, **kw: pytest.fail("2호 모드에서 1호 경로를 불렀습니다")  # noqa: E731
    workflow = DailyWorkflow(
        collector=SimpleNamespace(collect=fail),
        recommender=SimpleNamespace(recommend=fail, prompt_version="v"),
        strategy=strategy,
        engine=engine,
        account=SimpleNamespace(),
        trade_store=SimpleNamespace(
            closed_trade_returns=closed_trade_returns,
            save_recommendations=fail,
            recommendations_for=fail,
            ai_exit_decisions_for=fail,
        ),
        email=email,
        strategy_mode=STRATEGY_ETF_REGIME,
        etf_kill_loss_percent=kill_percent,
        etf_start_date=start,
    )
    return SimpleNamespace(
        workflow=workflow, email=email, notifications=notifications, closed=closed,
        market_data=market_data, strategy=strategy, store_calls=store_calls,
    )


def test_rising_regime_buys_kodex200_at_the_current_price():
    t = make_workflow(candles(0.5))

    t.workflow.recommend_and_notify(today=TODAY)

    recs = t.strategy._recommendations
    assert [(r.ticker, r.target_price, r.recommend_price) for r in recs] == [(LONG_ETF[0], 30000, 30000.0)]
    assert t.market_data.price_calls == [LONG_ETF[0]]
    assert len(t.email.sent) == 1 and "KODEX 200 매수 예정" in t.email.sent[0][0]


def test_falling_regime_buys_the_inverse_etf():
    t = make_workflow(candles(-0.5))

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [INVERSE_ETF[0]]


def test_signal_reads_two_queries_so_twenty_past_days_are_available():
    """한 번 조회가 20거래일치라 당일 봉이 섞이면 19개뿐이다 — 기준일을 하루 당긴 조회를 함께 한다."""
    t = make_workflow(candles(0.5))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.market_data.ohlcv_calls == [
        (LONG_ETF[0], TODAY), (LONG_ETF[0], TODAY - timedelta(days=1)),
    ]


def test_weak_signal_rests_and_says_so():
    t = make_workflow(candles(0.1))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []
    assert t.market_data.price_calls == []
    assert "쉽니다" in t.email.sent[0][0]


def test_short_history_rests_instead_of_guessing():
    t = make_workflow(candles(0.5, days=10))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []


def test_no_trade_today_marks_market_closed():
    t = make_workflow(candles(0.5, traded=False))

    t.workflow.recommend_and_notify(today=TODAY)

    assert len(t.closed) == 1
    assert t.strategy._recommendations == []
    assert t.email.sent == []


def test_kill_switch_stops_buying_when_cumulative_loss_reaches_the_limit():
    t = make_workflow(candles(0.5), returns=[-2.0, -1.5, -1.5], start=date(2026, 10, 1))

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []
    assert t.market_data.ohlcv_calls == []
    assert "자동 중단" in t.email.sent[0][0]
    assert "ETF_START_DATE" in t.email.sent[0][1]
    assert t.store_calls == [(date(2026, 10, 1), TODAY, ("069500", "114800"))]


def test_kill_switch_lets_trading_continue_above_the_limit():
    t = make_workflow(candles(0.5), returns=[-2.0, -1.5, 1.0])

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [LONG_ETF[0]]


def test_kill_switch_zero_means_off():
    t = make_workflow(candles(0.5), returns=[-50.0], kill_percent=0)

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.store_calls == []
    assert [r.ticker for r in t.strategy._recommendations] == [LONG_ETF[0]]


def test_reviews_are_skipped_in_etf_mode():
    """15:35 검증·프롬프트 자동 수정은 1호 재료다 — 2호 매매가 섞이면 안 된다."""
    t = make_workflow(candles(0.5))

    t.workflow.review_recommendations(today=TODAY)
    t.workflow.review_exits(today=TODAY)

    assert t.email.sent == []
