from datetime import time as dt_time

import pytest

from config.settings import (
    AI_EXIT_INTERVAL_CHOICES,
    DEFAULT_RECOMMEND_TIME_HHMM,
    Settings,
    parse_flag,
)


@pytest.fixture(autouse=True)
def clear_schedule_times(monkeypatch):
    """다른 테스트나 실제 .env가 남긴 값이 섞이지 않도록 매번 비운 상태에서 시작한다."""
    monkeypatch.delenv("RECOMMEND_TIME", raising=False)
    monkeypatch.delenv("BUY_TIME", raising=False)


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
    from src.core.runtime import MARKET_OPEN_TIME

    settings = Settings()

    assert settings.recommend_time >= MARKET_OPEN_TIME
    assert settings.recommend_time < settings.buy_time


def test_buy_time_defaults_to_0908(monkeypatch):
    from src.core.runtime import CANCEL_UNFILLED_TIME

    assert Settings().buy_time == dt_time(9, 8)
    assert CANCEL_UNFILLED_TIME == dt_time(10, 10)


def test_buy_time_reads_env_value(monkeypatch):
    monkeypatch.setenv("BUY_TIME", "09:15")

    assert Settings().buy_time == dt_time(9, 15)


def test_buy_time_falls_back_when_value_is_broken(monkeypatch):
    """오타 하나로 엔진이 뜨지 않는 것보다 기본값으로 도는 편이 낫다 (Settings.buy_time)."""
    monkeypatch.setenv("BUY_TIME", "구시")

    assert Settings().buy_time == dt_time(9, 8)


def test_buy_leaves_room_for_the_recommendation_to_finish():
    """매수는 추천 시각 +3분 이상이어야 한다 (PRD 10절 '매수 타이밍 조정').

    추천은 수집 약 40초 + LLM 타임아웃 상한 120초라 최악의 경우 2분 35초가 걸린다.
    이보다 매수를 앞당기면 추천이 없는 채로 주문이 돌아 그날이 통째로 빈다.
    """
    settings = Settings()
    gap_minutes = (settings.buy_time.hour * 60 + settings.buy_time.minute) - (
        settings.recommend_time.hour * 60 + settings.recommend_time.minute
    )

    assert gap_minutes >= 3


def _valid_settings(monkeypatch) -> Settings:
    """validate가 시각 외의 이유로 걸리지 않도록 필수 값만 채운 설정."""
    monkeypatch.setenv("KIWOOM_APP_KEY", "key")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "secret")
    monkeypatch.setenv("TRADE_MODE", "paper")
    return Settings()


def test_validate_rejects_buy_time_too_close_to_recommendation(monkeypatch):
    """순서가 뒤집히면 추천 없이 매수가 돌아 그날이 조용히 빈다 — 시작 자체를 막는다."""
    monkeypatch.setenv("RECOMMEND_TIME", "09:05")
    monkeypatch.setenv("BUY_TIME", "09:07")

    with pytest.raises(ValueError, match="BUY_TIME"):
        _valid_settings(monkeypatch).validate()


def test_validate_rejects_recommend_time_before_open(monkeypatch):
    """개장 전에는 당일 지표가 오지 않아 후보 선정이 성립하지 않는다."""
    monkeypatch.setenv("RECOMMEND_TIME", "08:45")

    with pytest.raises(ValueError, match="RECOMMEND_TIME"):
        _valid_settings(monkeypatch).validate()


def test_validate_accepts_a_later_pair(monkeypatch):
    monkeypatch.setenv("RECOMMEND_TIME", "09:05")
    monkeypatch.setenv("BUY_TIME", "09:20")

    _valid_settings(monkeypatch).validate()


# ── 매매 비용 (익절/손절 순손익률 판정에 그대로 들어간다) ──────────────
def test_tax_defaults_to_the_measured_rate(monkeypatch):
    """실제 키움 매도세금은 0.20%다 — 매도 49건(2026-07-29~09-01) 전수 대조로 확인했다.

    ⌊금액×0.15%⌋ + ⌊금액×0.05%⌋로 1원 오차 없이 맞는다. 2026-09-01 이전 기본값
    0.18%는 한 건도 설명하지 못했고, 그만큼 순손익을 후하게 봐서 손절이 늦게 걸렸다.
    """
    monkeypatch.delenv("TAX_PERCENT", raising=False)

    assert Settings().tax_ratio == 0.002


def test_commission_defaults_to_the_measured_rate(monkeypatch):
    """실효 수수료는 0.015%를 10원 단위로 절사한 값이라 항상 이 값 이하다."""
    monkeypatch.delenv("COMMISSION_PERCENT", raising=False)

    assert Settings().commission_ratio == 0.00015


# ── AI 매도 판단 호출 주기 ─────────────────────────
def test_ai_exit_interval_defaults_to_15(monkeypatch):
    monkeypatch.delenv("AI_EXIT_INTERVAL_MINUTES", raising=False)
    assert Settings().ai_exit_interval_minutes == 15


def test_ai_exit_interval_reads_env(monkeypatch):
    monkeypatch.setenv("AI_EXIT_INTERVAL_MINUTES", "60")
    assert Settings().ai_exit_interval_minutes == 60


def test_validate_accepts_every_ui_choice(monkeypatch):
    for minutes in AI_EXIT_INTERVAL_CHOICES:
        monkeypatch.setenv("AI_EXIT_INTERVAL_MINUTES", str(minutes))
        _valid_settings(monkeypatch).validate()


def test_validate_rejects_an_unsupported_interval(monkeypatch):
    """UI 콤보에 없는 값이 .env로 들어오면 엔진 시작 단계에서 막는다."""
    monkeypatch.setenv("AI_EXIT_INTERVAL_MINUTES", "7")
    with pytest.raises(ValueError, match="AI_EXIT_INTERVAL_MINUTES"):
        _valid_settings(monkeypatch).validate()



# ── 청산 경로 적용 여부 (AI 매도 판단 / 손절) ─────────
@pytest.fixture
def clear_exit_flags(monkeypatch):
    """실제 .env가 남긴 값이 섞이지 않도록 두 키를 비운 상태에서 시작한다."""
    monkeypatch.delenv("AI_EXIT_ENABLED", raising=False)
    monkeypatch.delenv("STOP_LOSS_ENABLED", raising=False)


def test_exit_flags_default_to_enabled(clear_exit_flags):
    """.env에 값이 없으면 AI 매도 판단·손절 둘 다 켬이다 (PRD 5.5-B)."""
    settings = Settings()

    assert settings.ai_exit_enabled is True
    assert settings.stop_loss_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " 0 "])
def test_exit_flags_read_off_from_env(monkeypatch, clear_exit_flags, raw):
    """UI가 저장하는 "0" 외에 손으로 적어 넣은 표기도 끔으로 읽는다."""
    monkeypatch.setenv("AI_EXIT_ENABLED", raw)
    monkeypatch.setenv("STOP_LOSS_ENABLED", raw)

    settings = Settings()

    assert settings.ai_exit_enabled is False
    assert settings.stop_loss_enabled is False


def test_exit_flags_are_independent(monkeypatch, clear_exit_flags):
    """한쪽만 끄는 것이 이 두 키의 존재 이유다 — 손절만 남기거나 AI만 남길 수 있어야 한다."""
    monkeypatch.setenv("AI_EXIT_ENABLED", "0")
    monkeypatch.setenv("STOP_LOSS_ENABLED", "1")

    settings = Settings()

    assert settings.ai_exit_enabled is False
    assert settings.stop_loss_enabled is True


@pytest.mark.parametrize("raw", ["", "   ", "kkeum", "예", "2"])
def test_unknown_flag_text_falls_back_to_on(monkeypatch, clear_exit_flags, raw):
    """오타를 '끔'으로 읽으면 손절 감시가 조용히 빠진다 — 알 수 없는 값은 켬으로 떨어뜨린다."""
    monkeypatch.setenv("STOP_LOSS_ENABLED", raw)

    assert Settings().stop_loss_enabled is True


def test_parse_flag_keeps_the_given_default_when_value_is_missing():
    """`parse_flag`는 UI의 체크박스 복원에도 쓰이므로 기본값을 그대로 존중해야 한다."""
    assert parse_flag(None, True) is True
    assert parse_flag(None, False) is False
    assert parse_flag("1", False) is True
    assert parse_flag("0", True) is False
