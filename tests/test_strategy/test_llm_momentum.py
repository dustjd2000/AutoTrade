from src.llm.recommender import StockRecommendation
from src.strategy.llm_momentum import LLMMomentumStrategy


def make_strategy(count):
    strategy = LLMMomentumStrategy()
    strategy.set_recommendations(
        [
            StockRecommendation(ticker=f"00{i}", name=f"종목{i}", reason="사유")
            for i in range(count)
        ]
    )
    return strategy


def test_three_recommendations_split_investable_amount_into_thirds():
    strategy = make_strategy(3)
    plans = strategy.build_buy_plans(cash=12_000_000)

    # 매수가능금액 600만원을 3등분 → 종목당 200만원 (= 예수금의 1/6)
    assert len(plans) == 3
    assert all(p.amount == 2_000_000 for p in plans)
    assert sum(p.amount for p in plans) == 6_000_000


def test_fewer_recommendations_keep_per_stock_amount_fixed():
    strategy = make_strategy(2)
    plans = strategy.build_buy_plans(cash=12_000_000)

    # 2개만 추천돼도 종목당 금액은 1/6(200만원) 그대로, 남은 200만원은 현금 유지
    assert len(plans) == 2
    assert all(p.amount == 2_000_000 for p in plans)
    assert sum(p.amount for p in plans) == 4_000_000


def test_no_recommendations_produces_no_plans():
    strategy = make_strategy(0)
    assert strategy.build_buy_plans(cash=12_000_000) == []


def test_more_than_three_recommendations_are_truncated():
    strategy = make_strategy(5)
    plans = strategy.build_buy_plans(cash=12_000_000)

    assert len(plans) == 3
