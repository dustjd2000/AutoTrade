from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from src.logger.trade_store import TradeStore
from src.research import rule_search
from src.research.backtest import Rule
from src.research.minute_store import MinuteStore
from src.research.rule_tuning import RuleTuning, add_minutes

BEST = Rule(0.2, False, "09:30", 2.0)


@dataclass
class FakeSettings:
    etf_signal_band_percent: float = 0.0
    etf_flip_enabled: bool = True
    recommend_time_hhmm: str = "09:10"
    buy_time_hhmm: str = "09:13"
    stop_loss_percent: float = 2.0
    commission_ratio: float = 0.00015
    rule_auto_apply_enabled: bool = True
    invalid: bool = False

    @property
    def recommend_time(self):
        return datetime.strptime(self.recommend_time_hhmm, "%H:%M").time()

    @property
    def buy_time(self):
        return datetime.strptime(self.buy_time_hhmm, "%H:%M").time()

    def validate(self):
        if self.invalid:
            raise ValueError("bad")


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None, images=None):
        self.sent.append((subject, message))


def make(tmp_path, evaluation, settings=None, holdings=(), writer_error=None):
    written = []

    def env_writer(values):
        if writer_error:
            raise writer_error
        written.append(values)

    tuning = RuleTuning(
        settings=settings or FakeSettings(),
        market_data=SimpleNamespace(),
        trade_store=TradeStore(tmp_path / "t.db"),
        email=FakeEmail(),
        env_writer=env_writer,
        open_tickers=lambda: list(holdings),
        minute_store=MinuteStore(tmp_path / "minute"),
        backtester_factory=lambda *a: SimpleNamespace(days=list(range(200))),
    )
    tuning._evaluate = lambda backtester, current: evaluation(current)
    return tuning, written


def good(current):
    return rule_search.Evaluation(current, BEST, 0.0, 0.2, 0.0, 0.1)


def run_weeks(tuning, n, start_day=1):
    for i in range(n):
        tuning.evaluate_rules(date(2026, 11, start_day) + timedelta(days=7 * i))


def test_add_minutes():
    assert add_minutes("09:10", 3) == "09:13"
    assert add_minutes("09:58", 3) == "10:01"


def test_applies_after_four_consecutive_wins(tmp_path):
    tuning, written = make(tmp_path, good)
    run_weeks(tuning, 3)
    assert written == [] and not tuning.applied_pending

    tuning.evaluate_rules(date(2026, 11, 22))

    assert written == [{
        "ETF_SIGNAL_BAND_PERCENT": "0.2", "ETF_FLIP_ENABLED": "0",
        "RECOMMEND_TIME": "09:30", "BUY_TIME": "09:33",
    }]
    assert tuning.applied_pending
    rows = tuning.trade_store.rule_evaluations()
    assert rows[-1].applied and not any(r.applied for r in rows[:-1])
    assert "자동 변경" in tuning.email.sent[-1][0]
    assert "STOP_LOSS_PERCENT" not in tuning.email.sent[-1][1]       # 안 바뀐 값은 쓰지도, 메일에 싣지도 않는다


def test_does_not_apply_when_switched_off(tmp_path):
    tuning, written = make(tmp_path, good, settings=FakeSettings(rule_auto_apply_enabled=False))
    run_weeks(tuning, 4)
    assert written == []
    assert "자동 적용 꺼짐" in tuning.trade_store.rule_evaluations()[-1].note


def test_postpones_while_holding(tmp_path):
    tuning, written = make(tmp_path, good, holdings=["069500"])
    run_weeks(tuning, 4)
    assert written == []
    assert "보유" in tuning.trade_store.rule_evaluations()[-1].note


def test_validate_failure_blocks(tmp_path):
    tuning, written = make(tmp_path, good, settings=FakeSettings(invalid=True))
    run_weeks(tuning, 4)
    assert written == []
    assert "설정 검사 실패" in tuning.trade_store.rule_evaluations()[-1].note


def test_env_write_failure_blocks(tmp_path):
    tuning, written = make(tmp_path, good, writer_error=OSError("locked"))
    run_weeks(tuning, 4)
    assert not tuning.applied_pending
    rows = tuning.trade_store.rule_evaluations()
    assert not rows[-1].applied and ".env 쓰기 실패" in rows[-1].note


def test_insufficient_data_is_recorded_and_breaks_the_streak(tmp_path):
    results = iter([good, good, lambda c: None, good, good, good])
    tuning, written = make(tmp_path, lambda current: next(results)(current))
    run_weeks(tuning, 6)
    assert written == []                              # 끊긴 뒤 3번뿐이다
    assert tuning.trade_store.rule_evaluations()[2].status == rule_search.STATUS_INSUFFICIENT


def test_exception_is_recorded_as_failed(tmp_path):
    def boom(current):
        raise RuntimeError("x")

    tuning, written = make(tmp_path, boom)
    tuning.evaluate_rules(date(2026, 11, 6))
    assert tuning.trade_store.rule_evaluations()[0].status == rule_search.STATUS_FAILED
    assert len(tuning.email.sent) == 1


def test_best_equal_to_current_is_not_applied(tmp_path):
    tuning, written = make(tmp_path, lambda c: rule_search.Evaluation(c, c, 0.1, 0.1, 0.1, 0.1))
    run_weeks(tuning, 4)
    assert written == []


def test_collect_minute_bars_fetches_since_the_last_day(tmp_path):
    calls = []

    def get_minute_candles(ticker, since=None):
        calls.append((ticker, since))
        return {"20261029": [("0900", 1.0)]}

    tuning, _ = make(tmp_path, good)
    tuning.market_data = SimpleNamespace(get_minute_candles=get_minute_candles)
    tuning.minute_store.merge("069500", {"20261028": [("0900", 1.0)]})

    tuning.collect_minute_bars()

    assert calls == [("069500", "20261028"), ("114800", None)]
    assert tuning.minute_store.last_day("114800") == "20261029"


def test_real_settings_survive_replace_and_validate(monkeypatch):
    """자동 적용은 `dataclasses.replace(settings, ...)`로 새 값을 검사한다 — 실제 Settings에서도 돼야 한다.

    `validate()`가 요구하는 나머지 환경변수(키움 키 등)는 tests/test_config/test_settings.py에서
    유효한 Settings를 만드는 방식을 그대로 따른다.
    """
    from config.settings import Settings

    monkeypatch.setenv("TRADE_MODE", "paper")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    settings = replace(
        Settings(), etf_signal_band_percent=0.2, etf_flip_enabled=False,
        recommend_time_hhmm="09:30", buy_time_hhmm="09:33", stop_loss_percent=1.5,
    )
    settings.validate()
    assert (settings.recommend_time.hour, settings.recommend_time.minute) == (9, 30)


def test_collect_failure_for_one_ticker_does_not_stop_the_other(tmp_path):
    def get_minute_candles(ticker, since=None):
        if ticker == "069500":
            raise RuntimeError("timeout")
        return {"20261029": [("0900", 1.0)]}

    tuning, _ = make(tmp_path, good)
    tuning.market_data = SimpleNamespace(get_minute_candles=get_minute_candles)
    tuning.collect_minute_bars()
    assert tuning.minute_store.last_day("114800") == "20261029"


def test_second_save_failure_after_write_still_mails_and_keeps_pending(tmp_path):
    tuning, written = make(tmp_path, good)
    run_weeks(tuning, 3)
    real_save = tuning.trade_store.save_rule_evaluation
    calls = []

    def flaky(row):
        calls.append(row)
        if len(calls) == 2:
            raise OSError("db locked")
        real_save(row)

    tuning.trade_store.save_rule_evaluation = flaky
    tuning.evaluate_rules(date(2026, 11, 22))

    assert len(written) == 1 and tuning.applied_pending
    assert "자동 변경" in tuning.email.sent[-1][0]
    assert "기록 저장 실패" in calls[-1].note


def test_open_tickers_failure_postpones(tmp_path):
    tuning, written = make(tmp_path, good)

    def boom():
        raise RuntimeError("api")

    tuning.open_tickers = boom
    run_weeks(tuning, 4)
    assert written == []
    assert "확인 실패" in tuning.trade_store.rule_evaluations()[-1].note


def test_email_failure_does_not_raise(tmp_path):
    tuning, _ = make(tmp_path, good)

    def boom(*a, **k):
        raise OSError("smtp")

    tuning.email.send = boom
    tuning.evaluate_rules(date(2026, 11, 6))


def test_history_read_failure_does_not_apply_but_mails(tmp_path):
    tuning, written = make(tmp_path, good)
    run_weeks(tuning, 3)

    def boom():
        raise OSError("db")

    tuning.trade_store.rule_evaluations = boom
    tuning.evaluate_rules(date(2026, 11, 22))
    assert written == []
    assert len(tuning.email.sent) == 4


def test_same_day_rerun_after_apply_does_nothing(tmp_path):
    tuning, written = make(tmp_path, good)
    run_weeks(tuning, 3)
    tuning.evaluate_rules(date(2026, 11, 22))
    mails = len(tuning.email.sent)

    tuning.evaluate_rules(date(2026, 11, 22))

    assert len(written) == 1 and len(tuning.email.sent) == mails
    assert tuning.trade_store.rule_evaluations()[-1].applied


def test_current_rule_reflects_applied_values(tmp_path):
    tuning, _ = make(tmp_path, good)
    run_weeks(tuning, 4)
    assert tuning.current_rule() == BEST

def test_current_rule_normalizes_unpadded_decision_time(tmp_path):
    """"9:10"처럼 0이 빠진 값도 백테스트의 문자열 비교에 맞게 "09:10"으로 쓴다."""
    tuning, _ = make(tmp_path, good, settings=FakeSettings(recommend_time_hhmm="9:10"))
    assert tuning.current_rule().decision_hhmm == "09:10"


def test_collect_survives_last_day_failure_for_one_ticker(tmp_path):
    fetched = []

    def get_minute_candles(ticker, since=None):
        fetched.append(ticker)
        return {"20261029": [("0900", 1.0)]}

    tuning, _ = make(tmp_path, good)
    tuning.market_data = SimpleNamespace(get_minute_candles=get_minute_candles)
    real_last_day = tuning.minute_store.last_day

    def broken_last_day(ticker):
        if ticker == "069500":
            raise ValueError("broken file")
        return real_last_day(ticker)

    tuning.minute_store.last_day = broken_last_day
    tuning.collect_minute_bars()

    assert fetched == ["114800"]
    assert real_last_day("114800") == "20261029"
