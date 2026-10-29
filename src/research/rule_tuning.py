"""2호 전략 규칙 자동 검증·적용 오케스트레이터 (스펙 2026-10-29).

매일 15:45 `collect_minute_bars`가 1분봉을 쌓고, 금요일 16:00 `evaluate_rules`가 후보 160개를 평가해
기록하고, 조건을 모두 만족하면 `.env`를 바꾼 뒤 메일을 보낸다. 적용하면 `applied_pending`을 세워
UI가 설정을 다시 읽고 엔진을 재시작하게 한다 (`EngineThread.consume_rule_change`).

어떤 실패도 매매를 막지 않고, 실패하면 '바꾸지 않음'으로 끝난다.
"""
import logging
from dataclasses import replace
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from src.logger.trade_store import RuleEvaluationRow
from src.notification import templates
from src.research import rule_search
from src.research.backtest import Backtester, Rule
from src.research.minute_store import MinuteStore
from src.strategy.etf_regime import ETF_TICKERS, INVERSE_ETF, LONG_ETF

logger = logging.getLogger(__name__)

# 결정 시각을 바꾸면 매수 안전망 시각은 결정 +3분 (`Settings.validate`의 최소 간격)
BUY_AFTER_DECISION_MINUTES = 3


def add_minutes(hhmm: str, minutes: int) -> str:
    hour, minute = map(int, hhmm.split(":"))
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _env_values(rule: Rule, buy_hhmm: str) -> Dict[str, str]:
    return {
        "ETF_SIGNAL_BAND_PERCENT": f"{rule.band_percent:g}",
        "ETF_FLIP_ENABLED": "1" if rule.flip_enabled else "0",
        "RECOMMEND_TIME": rule.decision_hhmm,
        "BUY_TIME": buy_hhmm,
        "STOP_LOSS_PERCENT": f"{rule.stop_percent:g}",
    }


class RuleTuning:
    def __init__(
        self,
        settings,
        market_data,
        trade_store,
        email,
        env_writer: Callable[[Dict[str, str]], None],
        open_tickers: Callable[[], List[str]],
        minute_store: Optional[MinuteStore] = None,
        backtester_factory=Backtester,
    ):
        self.settings = settings
        self.market_data = market_data
        self.trade_store = trade_store
        self.email = email
        self.env_writer = env_writer
        self.open_tickers = open_tickers
        self.minute_store = minute_store or MinuteStore()
        self.backtester_factory = backtester_factory
        # UI 스레드가 읽고 비운다 — 단순 bool이라 잠금이 없다 (ActionRunner.busy와 같은 방식)
        self.applied_pending = False
        self._evaluate = rule_search.evaluate

    def current_rule(self) -> Rule:
        s = self.settings
        return Rule(s.etf_signal_band_percent, s.etf_flip_enabled, s.recommend_time_hhmm, s.stop_loss_percent)

    def collect_minute_bars(self) -> None:
        """마지막 저장일부터 오늘까지 1분봉을 받아 쌓는다. 종목 하나가 실패해도 다른 종목은 받는다."""
        for ticker in ETF_TICKERS:
            since = self.minute_store.last_day(ticker)
            try:
                bars = self.market_data.get_minute_candles(ticker, since)
                saved = self.minute_store.merge(ticker, bars)
            except Exception:
                logger.exception("1분봉 수집 실패: %s (기준 %s) — 다음 수집이 다시 채웁니다.", ticker, since)
                continue
            logger.info("1분봉 저장: %s %d일 (기준 %s)", ticker, saved, since or "처음")

    def evaluate_rules(self, today: Optional[date] = None) -> None:
        today = today or date.today()
        current = self.current_rule()
        evaluation = None
        try:
            backtester = self.backtester_factory(
                self.minute_store.load(LONG_ETF[0]),
                self.minute_store.load(INVERSE_ETF[0]),
                self.settings.commission_ratio,
            )
            evaluation = self._evaluate(backtester, current)
        except Exception:
            logger.exception("2호 규칙 평가 실패 — 이번 주는 바꾸지 않습니다.")
            row = RuleEvaluationRow(
                day=today, status=rule_search.STATUS_FAILED, current_rule=current.to_json(), note="평가 중 예외"
            )
        else:
            if evaluation is None:
                needed = rule_search.TEST_DAYS + rule_search.MIN_SELECT_DAYS
                row = RuleEvaluationRow(
                    day=today,
                    status=rule_search.STATUS_INSUFFICIENT,
                    current_rule=current.to_json(),
                    note=f"평가 가능한 거래일 {len(backtester.days)}일 < {needed}일",
                )
            else:
                row = RuleEvaluationRow(
                    day=today,
                    status=rule_search.STATUS_EVALUATED,
                    current_rule=current.to_json(),
                    best_rule=evaluation.best.to_json(),
                    select_current=evaluation.select_current,
                    select_best=evaluation.select_best,
                    test_current=evaluation.test_current,
                    test_best=evaluation.test_best,
                    beats=evaluation.beats,
                    positive=evaluation.positive,
                )

        # 이번 주 기록이 들어가야 연속 판정에 이번 주가 든다
        self.trade_store.save_rule_evaluation(row)
        history = self.trade_store.rule_evaluations()
        streak = rule_search.streak_count(history)
        cooldown = rule_search.cooldown_left(history)

        applied_values = None
        if (
            evaluation is not None
            and evaluation.best != current
            and streak >= rule_search.STREAK_EVALUATIONS
            and cooldown == 0
        ):
            applied_values, row.note = self._apply(evaluation.best, current)
            row.applied = applied_values is not None
            self.trade_store.save_rule_evaluation(row)
            logger.warning("2호 규칙 자동 적용 판단: %s — %s", row.note, applied_values)

        subject, body = templates.rule_evaluation_email(row, streak, cooldown, applied_values)
        self.email.send(subject, body)

    def _apply(self, best: Rule, current: Rule) -> Tuple[Optional[Dict[str, Tuple[str, str]]], str]:
        """`.env`를 바꾼다. 바꾼 값 {키: (이전, 이후)}와 비고를 돌려준다. 못 바꿨으면 (None, 사유)."""
        if not self.settings.rule_auto_apply_enabled:
            return None, "자동 적용 꺼짐 (RULE_AUTO_APPLY_ENABLED=0)"
        if self.open_tickers():
            return None, "보유 종목이 있어 다음 평가로 미룸"
        if not rule_search.in_range(best):
            return None, "후보 범위 밖"
        buy_hhmm = add_minutes(best.decision_hhmm, BUY_AFTER_DECISION_MINUTES)
        try:
            replace(
                self.settings,
                etf_signal_band_percent=best.band_percent,
                etf_flip_enabled=best.flip_enabled,
                recommend_time_hhmm=best.decision_hhmm,
                buy_time_hhmm=buy_hhmm,
                stop_loss_percent=best.stop_percent,
            ).validate()
        except Exception as e:
            return None, f"설정 검사 실패: {e}"

        new_values = _env_values(best, buy_hhmm)
        old_values = _env_values(current, self.settings.buy_time_hhmm)
        try:
            self.env_writer(new_values)
        except Exception as e:
            logger.exception(".env 쓰기 실패 — 규칙을 바꾸지 않습니다.")
            return None, f".env 쓰기 실패: {e}"
        self.applied_pending = True
        changed = {key: (old_values[key], new_values[key]) for key in new_values if old_values[key] != new_values[key]}
        return changed, "자동 적용"
