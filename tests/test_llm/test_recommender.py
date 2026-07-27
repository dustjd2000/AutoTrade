import pytest

from src.data.collector import DailyStockData
from src.llm.recommender import build_user_prompt, parse_recommendations


def test_parse_valid_json_array():
    raw = (
        '[{"ticker": "005930", "name": "삼성전자", "reason": "외국인 순매수 전환"},'
        ' {"ticker": "000660", "name": "SK하이닉스", "reason": "HBM 수요 증가"}]'
    )
    result = parse_recommendations(raw)

    assert len(result) == 2
    assert result[0].ticker == "005930"
    assert result[1].name == "SK하이닉스"


def test_parse_rejects_non_json():
    with pytest.raises(Exception):
        parse_recommendations("죄송하지만 추천을 드릴 수 없습니다.")


def test_parse_rejects_empty_array():
    with pytest.raises(ValueError):
        parse_recommendations("[]")


def test_parse_rejects_missing_field():
    with pytest.raises(KeyError):
        parse_recommendations('[{"ticker": "005930", "name": "삼성전자"}]')


def test_build_user_prompt_includes_all_stock_data():
    daily_data = [
        DailyStockData(
            ticker="005930",
            name="삼성전자",
            change_rate=1.5,
            volume=1_000_000,
            gap_rate=0.8,
            headlines=["신규 수주 공시"],
        )
    ]
    prompt = build_user_prompt(daily_data)

    assert "005930" in prompt
    assert "삼성전자" in prompt
    assert "+1.50%" in prompt
    assert "신규 수주 공시" in prompt
