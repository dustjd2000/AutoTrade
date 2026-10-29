from datetime import date

from src.logger.trade_store import RuleEvaluationRow, TradeStore


def test_rule_evaluations_round_trip_in_day_order(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_rule_evaluation(RuleEvaluationRow(day=date(2026, 11, 6), status="evaluated",
                                                 current_rule="c", best_rule="b", select_current=0.1,
                                                 select_best=0.2, test_current=0.0, test_best=0.08,
                                                 beats=True, positive=True))
    store.save_rule_evaluation(RuleEvaluationRow(day=date(2026, 10, 30), status="insufficient_data",
                                                 current_rule="c", note="부족"))

    rows = store.rule_evaluations()

    assert [r.day for r in rows] == [date(2026, 10, 30), date(2026, 11, 6)]
    assert rows[0].note == "부족" and rows[0].best_rule == ""
    assert rows[1].beats is True and rows[1].applied is False and rows[1].test_best == 0.08


def test_same_day_is_overwritten(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    day = date(2026, 11, 6)
    store.save_rule_evaluation(RuleEvaluationRow(day=day, status="evaluated", current_rule="c"))
    store.save_rule_evaluation(RuleEvaluationRow(day=day, status="evaluated", current_rule="c", applied=True))

    rows = store.rule_evaluations()
    assert len(rows) == 1 and rows[0].applied is True
