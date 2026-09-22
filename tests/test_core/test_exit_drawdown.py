"""이익 반납 감시 (PRD 5.5-B '이익 반납 감시').

2026-09-10 HD현대중공업이 계기다 — 순손익 +4.46%까지 갔다가 한 시간에 걸쳐 +1.41%로
밀렸는데, 15분 주기 AI 판단이 매번 "여전히 플러스"로 보유했고 15:15에 본전으로 끝났다.
여기서는 고점 추적과 발동 조건만 본다 (판단 자체는 여전히 AI가 한다).
"""

from src.core.events import MarketData
from src.core.exit_drawdown import MAX_URGENT_TRIGGERS_PER_DAY, DrawdownTracker

from tests.test_core.test_portfolio_exit import make_engine, two_holdings

TICKER = "329180"
OTHER = "000270"
THRESHOLD = 0.03  # 3.00%p


def tracker(threshold=THRESHOLD):
    return DrawdownTracker(threshold)


# ── 고점 추적 ────────────────────────────────────────────────
def test_peak_follows_the_highest_value_seen():
    t = tracker()
    for value in (0.01, 0.0446, 0.0315):
        t.update({TICKER: value})

    assert t.retracement(TICKER, 0.0315).peak == 0.0446


def test_retracement_reports_amount_and_share():
    t = tracker()
    t.update({TICKER: 0.04})
    r = t.retracement(TICKER, 0.01)

    assert abs(r.given_back - 0.03) < 1e-9
    assert abs(r.given_back_share - 0.75) < 1e-9


def test_new_high_reports_no_retracement():
    t = tracker()
    t.update({TICKER: 0.02})
    r = t.retracement(TICKER, 0.02)

    assert r.given_back == 0.0
    assert r.given_back_share == 0.0


# ── 발동 조건 ────────────────────────────────────────────────
def test_crossing_the_threshold_triggers():
    """2026-09-10 현대중공업 궤적 그대로 — +4.46% 고점에서 +1.41%면 3.05%p 반납이다."""
    t = tracker()
    t.update({TICKER: 0.0446})

    assert t.update({TICKER: 0.0141}) == [TICKER]
    assert t.urgent_pending is True


def test_staying_inside_the_threshold_does_not_trigger():
    t = tracker()
    t.update({TICKER: 0.0446})

    assert t.update({TICKER: 0.0200}) == []  # 2.46%p 반납
    assert t.urgent_pending is False


def test_loss_deepening_is_not_a_giveback():
    """종일 마이너스인 종목이 더 밀리는 것은 반납이 아니다 — 그 구간은 손절이 맡는다."""
    t = tracker()
    t.update({TICKER: -0.01})

    assert t.update({TICKER: -0.05}) == []
    assert t.urgent_pending is False


def test_threshold_zero_disables_the_watch():
    t = tracker(threshold=0.0)
    t.update({TICKER: 0.05})

    assert t.update({TICKER: -0.05}) == []
    assert t.urgent_pending is False


def test_each_ticker_is_judged_on_its_own_peak():
    """합산으로 걸면 한 종목이 크게 밀려도 다른 종목이 희석해 발동하지 않는다."""
    t = tracker()
    t.update({TICKER: 0.0446, OTHER: 0.0040})

    assert t.update({TICKER: 0.0141, OTHER: 0.0048}) == [TICKER]


# ── 재발동 규칙 ──────────────────────────────────────────────
def test_does_not_retrigger_without_a_new_high():
    """한 번 발동한 뒤 계속 밀린다고 매 틱마다 다시 부르면 안 된다."""
    t = tracker()
    t.update({TICKER: 0.0446})
    t.update({TICKER: 0.0141})
    t.take_urgent()

    assert t.update({TICKER: 0.0100}) == []
    assert t.urgent_pending is False


def test_retriggers_after_a_new_high():
    t = tracker()
    t.update({TICKER: 0.04})
    t.update({TICKER: 0.00})
    t.take_urgent()

    t.update({TICKER: 0.05})  # 새 고점 — 다시 무장한다

    assert t.update({TICKER: 0.01}) == [TICKER]


def test_triggers_are_capped_per_day():
    t = tracker()
    for _ in range(MAX_URGENT_TRIGGERS_PER_DAY + 2):
        t.update({TICKER: 0.05})  # 새 고점으로 재무장
        t.update({TICKER: 0.00})
        t.take_urgent()

    t.update({TICKER: 0.05})
    assert t.update({TICKER: 0.00}) == []


# ── 소비와 초기화 ────────────────────────────────────────────
def test_take_urgent_consumes_the_flag():
    t = tracker()
    t.update({TICKER: 0.04})
    t.update({TICKER: 0.00})

    assert t.take_urgent() is True
    assert t.take_urgent() is False


def test_clear_forgets_yesterday():
    t = tracker()
    t.update({TICKER: 0.04})
    t.update({TICKER: 0.00})

    t.clear()

    assert t.retracement(TICKER, 0.0) is None
    assert t.urgent_pending is False


# ── 실시간 경로 (엔진 콜백) ──────────────────────────────────
def engine_with_watch(ratio=THRESHOLD, prices=(1010.0, 1000.0)):
    """비용 0인 실물 RiskManager를 붙인 엔진 — 가격 변동률이 곧 순손익률이다."""
    engine, orders, _ = make_engine(two_holdings(*prices))
    engine.exit_drawdown = DrawdownTracker(ratio)
    return engine, orders


def test_tick_tracks_the_peak_between_ai_cycles():
    """고점은 15분 주기 사이를 스쳐 간다 — 실시간 콜백이 잡아야 한다."""
    engine, _ = engine_with_watch()

    engine.on_market_data(MarketData(ticker="005930", price=1050.0, volume=1))

    assert engine.exit_drawdown.retracement("005930", 0.0).peak == 0.05


def test_tick_triggers_when_the_giveback_crosses():
    engine, orders = engine_with_watch()

    engine.on_market_data(MarketData(ticker="005930", price=1050.0, volume=1))
    engine.on_market_data(MarketData(ticker="005930", price=1015.0, volume=1))

    assert engine.exit_drawdown.urgent_pending is True
    # 반납 감시는 팔지 않는다 — AI를 부를 시점만 앞당긴다
    assert orders == []


def test_tick_does_not_track_while_ai_exit_is_off():
    """AI 매도 판단이 꺼져 있어도 고점 추적은 계속된다 (스펙 2026-09-22 2.2) — 앞당길 호출만 없다."""
    engine, _ = engine_with_watch()
    engine.ai_exit_enabled = False

    engine.on_market_data(MarketData(ticker="005930", price=1050.0, volume=1))

    assert engine.exit_drawdown.retracement("005930", 0.0).peak == 0.05
    assert engine.exit_drawdown.urgent_pending is False


def test_daily_reset_clears_the_tracker():
    engine, _ = engine_with_watch()
    engine.on_market_data(MarketData(ticker="005930", price=1050.0, volume=1))

    engine.reset_for_new_day()

    assert engine.exit_drawdown.retracement("005930", 0.0) is None


def test_small_peak_does_not_trigger():
    """고점이 임계값에 못 미치면 반납으로 보지 않는다 — 매수가 근처의 출렁임이다.

    2026-09-17 278470이 고점 +0.05%에서 -1.06%로 간 것을 '고점 이익의 100% 반납'으로
    잡아 AI를 불렀다. 0.05%는 이익이 아니라 노이즈다.
    """
    tracker = DrawdownTracker(threshold_ratio=0.01)

    tracker.update({"278470": 0.0005})          # 고점 +0.05%
    crossed = tracker.update({"278470": -0.0106})  # 1.11%p 반납

    assert crossed == []


def test_peak_at_threshold_still_triggers():
    """고점이 임계값 이상이면 기존대로 발동한다."""
    tracker = DrawdownTracker(threshold_ratio=0.01)

    tracker.update({"062040": 0.0217})          # 고점 +2.17%
    crossed = tracker.update({"062040": 0.0117})  # 1.00%p 반납

    assert crossed == ["062040"]


# ── 영속화·복원 (스펙 2026-09-22 2.2) ────────────────────────
def test_new_peaks_are_marked_dirty_until_taken():
    t = tracker()
    t.update({TICKER: 0.01})
    t.update({TICKER: 0.02})
    t.update({TICKER: 0.015})

    assert t.take_dirty() == {TICKER: 0.02}
    assert t.take_dirty() == {}


def test_seed_restores_a_higher_peak_only():
    t = tracker()
    t.update({TICKER: 0.01, OTHER: 0.05})
    t.seed({TICKER: 0.0446, OTHER: 0.02})

    assert t.retracement(TICKER, 0.0).peak == 0.0446
    assert t.retracement(OTHER, 0.0).peak == 0.05


def test_seeded_peak_is_armed_for_the_giveback_trigger():
    """재시작 전에 발동했어야 할 반납이면 복원 직후 한 번 앞당겨지는 것이 의도된 동작이다."""
    t = tracker()
    t.seed({TICKER: 0.0446})

    assert t.update({TICKER: 0.0141}) == [TICKER]


def test_clear_drops_dirty_peaks():
    t = tracker()
    t.update({TICKER: 0.01})
    t.clear()
    assert t.take_dirty() == {}
