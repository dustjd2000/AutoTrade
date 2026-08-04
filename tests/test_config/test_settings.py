from datetime import time as dt_time

import pytest

from config.settings import DEFAULT_RECOMMEND_TIME_HHMM, Settings


@pytest.fixture(autouse=True)
def clear_recommend_time(monkeypatch):
    """다른 테스트나 실제 .env가 남긴 값이 섞이지 않도록 매번 비운 상태에서 시작한다."""
    monkeypatch.delenv("RECOMMEND_TIME", raising=False)


def test_recommend_time_defaults_to_0845():
    assert Settings().recommend_time == dt_time(8, 45)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("08:40", dt_time(8, 40)),
        ("08:50", dt_time(8, 50)),
        ("08:55", dt_time(8, 55)),
    ],
)
def test_recommend_time_reads_env_value(monkeypatch, raw, expected):
    monkeypatch.setenv("RECOMMEND_TIME", raw)

    assert Settings().recommend_time == expected


@pytest.mark.parametrize("raw", ["", "oops", "8시45분", "08:45:00", "99:99"])
def test_recommend_time_falls_back_when_value_is_broken(monkeypatch, raw):
    """오타 하나로 엔진이 뜨지 않는 것보다 기본값으로 도는 편이 낫다 (Settings.recommend_time)."""
    monkeypatch.setenv("RECOMMEND_TIME", raw)

    settings = Settings()

    assert settings.recommend_time == dt_time(8, 45)
    # 원값은 그대로 보존한다 — .env를 다시 저장할 때 사용자가 넣은 값을 덮어쓰지 않도록
    assert settings.recommend_time_hhmm == raw


def test_default_constant_matches_property_fallback():
    """상수와 실제 폴백 값이 어긋나면 UI 기본 선택과 엔진 동작이 갈린다."""
    hour, minute = (int(part) for part in DEFAULT_RECOMMEND_TIME_HHMM.split(":"))

    assert Settings().recommend_time == dt_time(hour, minute)
