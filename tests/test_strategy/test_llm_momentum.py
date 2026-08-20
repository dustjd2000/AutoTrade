from src.llm.recommender import StockRecommendation
from src.strategy.llm_momentum import LLMMomentumStrategy


def make_strategy(count):
    strategy = LLMMomentumStrategy()
    strategy.set_recommendations(
        [
            StockRecommendation(ticker=f"00{i}", name=f"종목{i}", target_price=1000, reason="사유")
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


def test_custom_ratio_and_count_change_allocation():
    strategy = LLMMomentumStrategy(investable_ratio=0.8, target_stock_count=4)
    strategy.set_recommendations(
        [
            StockRecommendation(ticker=f"00{i}", name=f"종목{i}", target_price=1000, reason="사유")
            for i in range(4)
        ]
    )
    plans = strategy.build_buy_plans(cash=10_000_000)

    # 매수가능금액 800만원을 4등분 → 종목당 200만원
    assert len(plans) == 4
    assert all(p.amount == 2_000_000 for p in plans)
    assert sum(p.amount for p in plans) == 8_000_000


def test_buy_plan_carries_the_recommend_price_for_the_gap_down_check():
    """09:08 갭 하락 판정 기준값이 추천 → 계획으로 넘어와야 한다 (PRD 5.5-B)."""
    strategy = LLMMomentumStrategy()
    strategy.set_recommendations(
        [
            StockRecommendation(
                ticker="003670",
                name="포스코퓨처엠",
                target_price=163_500,
                reason="사유",
                recommend_price=165_500.0,
            )
        ]
    )

    plans = strategy.build_buy_plans(cash=12_000_000)

    assert plans[0].recommend_price == 165_500.0
