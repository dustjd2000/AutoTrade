from datetime import time as dt_time

import pytest

from config.settings import DEFAULT_RECOMMEND_TIME_HHMM, Settings


@pytest.fixture(autouse=True)
def clear_recommend_time(monkeypatch):
    """다른 테스트나 실제 .env가 남긴 값이 섞이지 않도록 매번 비운 상태에서 시작한다."""
    monkeypatch.delenv("RECOMMEND_TIME", raising=False)


def test_recommend_time_defaults_to_0905():
    """추천은 개장(09:00) 후여야 당일 지표를 볼 수 있다 (PRD 10절 '개장 후 추천으로 이동').

    2026-08-14 이전 기본값은 08:45였다 — 그때는 장 전이라 당일 지표가 없었다.
    """
    assert Settings().recommend_time == dt_time(9, 5)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("09:00", dt_time(9, 0)),
        ("09:10", dt_time(9, 10)),
        ("09:20", dt_time(9, 20)),
    ],
)
def test_recommend_time_reads_env_value(monkeypatch, raw, expected):
    monkeypatch.setenv("RECOMMEND_TIME", raw)

    assert Settings().recommend_time == expected


@pytest.mark.parametrize("raw", ["", "oops", "9시05분", "09:05:00", "99:99"])
def test_recommend_time_falls_back_when_value_is_broken(monkeypatch, raw):
    """오타 하나로 엔진이 뜨지 않는 것보다 기본값으로 도는 편이 낫다 (Settings.recommend_time)."""
    monkeypatch.setenv("RECOMMEND_TIME", raw)

    settings = Settings()

    assert settings.recommend_time == dt_time(9, 5)
    # 원값은 그대로 보존한다 — .env를 다시 저장할 때 사용자가 넣은 값을 덮어쓰지 않도록
    assert settings.recommend_time_hhmm == raw


def test_default_constant_matches_property_fallback():
    """상수와 실제 폴백 값이 어긋나면 UI 기본 선택과 엔진 동작이 갈린다."""
    hour, minute = (int(part) for part in DEFAULT_RECOMMEND_TIME_HHMM.split(":"))

    assert Settings().recommend_time == dt_time(hour, minute)


# ── 갭 하락 허용치 (PRD 5.5-B, 확정 2026-08-11) ──────────────
def test_gap_down_tolerance_defaults_to_one_percent(monkeypatch):
    monkeypatch.delenv("GAP_DOWN_TOLERANCE_PERCENT", raising=False)

    assert Settings().gap_down_tolerance_ratio == 0.01


def test_gap_down_tolerance_zero_means_off(monkeypatch):
    """갭 상승 쪽(0 = 가장 엄격)과 반대 규약이라 값이 그대로 0으로 와야 한다."""
    monkeypatch.setenv("GAP_DOWN_TOLERANCE_PERCENT", "0")

    assert Settings().gap_down_tolerance_ratio == 0.0


def test_recommend_time_is_between_market_open_and_buy():
    """추천은 개장 후, 매수 전이어야 한다 (PRD 10절 '개장 후 추천으로 이동')."""
    from src.core.runtime import BUY_TIME, MARKET_OPEN_TIME

    settings = Settings()

    assert settings.recommend_time >= MARKET_OPEN_TIME
    assert settings.recommend_time < BUY_TIME


def test_buy_and_cancel_times_moved_after_open():
    from src.core.runtime import BUY_TIME, CANCEL_UNFILLED_TIME

    assert BUY_TIME == dt_time(9, 8)
    assert CANCEL_UNFILLED_TIME == dt_time(10, 10)


def test_buy_leaves_room_for_the_recommendation_to_finish():
    """매수는 추천 시각 +3분 이상이어야 한다 (PRD 10절 '매수 타이밍 조정').

    추천은 수집 약 40초 + LLM 타임아웃 상한 120초라 최악의 경우 2분 35초가 걸린다.
    이보다 매수를 앞당기면 추천이 없는 채로 주문이 돌아 그날이 통째로 빈다.
    """
    from src.core.runtime import BUY_TIME

    settings = Settings()
    gap_minutes = (
        BUY_TIME.hour * 60 + BUY_TIME.minute
    ) - (settings.recommend_time.hour * 60 + settings.recommend_time.minute)

    assert gap_minutes >= 3
