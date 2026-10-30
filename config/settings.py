import logging
import os
from dataclasses import dataclass, field
from datetime import date
from datetime import time as dt_time
from typing import Optional

logger = logging.getLogger(__name__)

# 결정 시각(`RECOMMEND_TIME`)과 매수 안전망 시각(`BUY_TIME`) 기본값 — 둘 다 `.env`로만 바꾼다
# (UI에서는 값을 보여주기만 한다). 결정 시각에 당일 거래 여부(휴장 판정)를 보므로 개장 뒤여야
# 하고, 결정 직후 시장가 매수가 같은 실행에서 나간다 (PRD 5.5-C). 매수 시각은 그 사이 엔진이
# 재시작돼 결정이 사라진 날의 안전망이라, 결정보다 `MIN_RECOMMEND_TO_BUY_MINUTES`분 이상 뒤다.
DEFAULT_RECOMMEND_TIME_HHMM = "09:05"
DEFAULT_BUY_TIME_HHMM = "09:10"
MIN_RECOMMEND_TO_BUY_MINUTES = 3
# AI 매도 판단 단계 폭 하한(%p) — 이보다 좁으면 거의 틱마다 LLM을 부르게 된다
MIN_AI_EXIT_STEP_PERCENT = 0.1
MARKET_OPEN_HHMM = "09:00"

# 이번 달 순수익률이 이 값(%) 이상이면 15:40 매도 프롬프트 자동 수정을 건너뛴다
# (스펙 2026-09-22 4-A, 2026-10-29 복구). `.env` 전용 — 허용 값은 1~10%, 0.5 단위.
TUNE_SKIP_MONTHLY_RETURN_CHOICES = tuple(step / 2 for step in range(2, 21))
DEFAULT_TUNE_SKIP_MONTHLY_RETURN_PERCENT = 5.0


def _parse_tune_skip_percent(raw: Optional[str]) -> float:
    """월 순수익 게이트 값을 읽는다. 비었거나 목록 밖 값이면 경고 후 기본값(5)으로 돌린다.

    validate()로 막지 않는 것은 의도한 것이다 (2026-09-22) — 튜닝에만 쓰이는 값 하나가
    틀렸다고 엔진이 뜨지 않으면 그날 매매 전체가 멈춘다. `_parse_hhmm`과 같은 이유다.
    """
    if raw is None or not raw.strip():
        return DEFAULT_TUNE_SKIP_MONTHLY_RETURN_PERCENT
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value not in TUNE_SKIP_MONTHLY_RETURN_CHOICES:
        logger.warning(
            "TUNE_SKIP_MONTHLY_RETURN_PERCENT 값이 올바르지 않아(1~10, 0.5 단위) 기본값 %s를 사용합니다: %r",
            DEFAULT_TUNE_SKIP_MONTHLY_RETURN_PERCENT,
            raw,
        )
        return DEFAULT_TUNE_SKIP_MONTHLY_RETURN_PERCENT
    return value


def _parse_hhmm(raw: str, default: str, name: str) -> dt_time:
    """"HH:MM" 문자열을 `datetime.time`으로 바꾼다. 깨져 있으면 기본값으로 돌린다.

    엔진 자체를 막지 않는 것은 의도한 것이다 — .env 한 줄 오타로 그날 매매가 통째로
    중단되는 편보다 낫다. 대신 경고를 남겨 넘어간 사실을 알린다.
    """
    try:
        hour, minute = (int(part) for part in raw.split(":"))
        return dt_time(hour, minute)
    except (AttributeError, ValueError):
        logger.warning("%s 값이 올바르지 않아 기본값 %s를 사용합니다: %r", name, default, raw)
        hour, minute = (int(part) for part in default.split(":"))
        return dt_time(hour, minute)


def _minutes(value: dt_time) -> int:
    return value.hour * 60 + value.minute


# .env에서 켬/끔으로 인정하는 표기 — UI는 "1"/"0"으로 저장하지만 손으로 적어 넣는 경우가
# 있어 흔한 표기를 함께 받는다.
_TRUE_TEXTS = ("1", "true", "yes", "on")
_FALSE_TEXTS = ("0", "false", "no", "off")


def parse_flag(raw: Optional[str], default: bool) -> bool:
    """.env의 켬/끔 문자열을 bool로 바꾼다. 비었거나 알 수 없는 값이면 기본값을 쓴다.

    알 수 없는 값을 '끔'으로 읽지 않는 것이 요점이다 — 오타 하나로 손절 감시가 조용히
    빠지는 것이 이 파일에서 가장 위험한 실패다. UI(`MainWindow._load_settings`)도 체크박스를
    복원할 때 같은 함수를 쓴다.
    """
    text = (raw or "").strip().lower()
    if text in _TRUE_TEXTS:
        return True
    if text in _FALSE_TEXTS:
        return False
    if text:
        logger.warning("켬/끔 값을 알 수 없어 기본값 %s를 사용합니다: %r", default, raw)
    return default


@dataclass
class Settings:
    # 매매 모드 — paper(모의) / live(실전). 기본값은 안전한 모의투자
    mode: str = field(default_factory=lambda: os.getenv("TRADE_MODE", "paper"))

    # 키움 REST API
    app_key: str = field(default_factory=lambda: os.getenv("KIWOOM_APP_KEY", ""))
    app_secret: str = field(default_factory=lambda: os.getenv("KIWOOM_APP_SECRET", ""))

    # 키움 REST API 도메인 (openapi.kiwoom.com 공식 가이드 기준)
    @property
    def api_base_url(self) -> str:
        if self.mode == "live":
            return "https://api.kiwoom.com"
        return "https://mockapi.kiwoom.com"

    @property
    def websocket_url(self) -> str:
        if self.mode == "live":
            return "wss://api.kiwoom.com:10000/api/dostk/websocket"
        return "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"

    # 계좌번호
    account_number: str = field(default_factory=lambda: os.getenv("KIWOOM_ACCOUNT", ""))

    # 이메일 알림 (SMTP) — 서버/포트/비밀번호는 UI에서 다루지 않고 .env로만 관리한다
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    email_from: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", ""))
    email_to: str = field(default_factory=lambda: os.getenv("EMAIL_TO", ""))

    # 장애 대응: 장중 예외 발생 시 동작 방식 ("close_all" | "hold") — 6절
    emergency_action: str = field(default_factory=lambda: os.getenv("EMERGENCY_ACTION", "hold"))

    # LLM 연동 — AI 매도 판단 (PRD 5.5-C '매도 판단'). Anthropic Claude API.
    # 키는 UI "계좌 / API 설정"의 'Anthropic Key' 칸에서 다룬다(2026-08-25). 모델명은 .env 전용이다.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-5"))

    # 리스크 설정
    max_position_ratio: float = 0.1
    max_daily_loss_ratio: float = 0.02
    # 전체 계좌 대비 최대 노출 비중 — investable_ratio_percent를 100%까지 열어둔 UI 설정값이
    # 이 안전장치에 걸려 매수가 거부되지 않도록 상한을 완화했다(기존 0.7, 확정 2026-08-03).
    # "전략 버그로 과도하게 매수되는 경우"만 걸러내는 상위 안전장치라는 검사 로직 자체는 유지.
    max_total_exposure_ratio: float = 1.0

    # 자금 배분 — 예수금 중 매매에 투입할 비율(%). UI 콤보박스 선택 범위 10~100(10 단위).
    # 2호 전략은 이 금액 전부를 오늘의 ETF 1종목에 쓴다 (PRD 5.5-C)
    investable_ratio_percent: int = field(default_factory=lambda: int(os.getenv("INVESTABLE_RATIO_PERCENT", "50")))

    @property
    def investable_ratio(self) -> float:
        return self.investable_ratio_percent / 100

    # 결정·매수 안전망 시각 — .env에는 "HH:MM" 문자열로 두고, 스케줄러가 쓰는
    # datetime.time은 프로퍼티로 환산한다
    recommend_time_hhmm: str = field(
        default_factory=lambda: os.getenv("RECOMMEND_TIME", DEFAULT_RECOMMEND_TIME_HHMM)
    )
    buy_time_hhmm: str = field(
        default_factory=lambda: os.getenv("BUY_TIME", DEFAULT_BUY_TIME_HHMM)
    )

    @property
    def recommend_time(self) -> dt_time:
        """스케줄러에 넘길 결정 시각 (ETF 국면 신호 → 결정 메일 → 곧바로 시장가 매수)."""
        return _parse_hhmm(
            self.recommend_time_hhmm, DEFAULT_RECOMMEND_TIME_HHMM, "RECOMMEND_TIME"
        )

    @property
    def buy_time(self) -> dt_time:
        """스케줄러에 넘길 매수 안전망 시각 — 오늘 아직 주문하지 않았으면 결정부터 다시 해서 산다."""
        return _parse_hhmm(self.buy_time_hhmm, DEFAULT_BUY_TIME_HHMM, "BUY_TIME")

    # 손절 라인 — UI/환경변수에는 %(예: 2)로 저장, 내부 계산은 비율(0.02)로 환산.
    # 수수료·세금·슬리피지를 뺀 순손익률 기준이다 (PRD 5.5-B).
    stop_loss_percent: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PERCENT", "2")))
    # 익절 라인 — 종목별 순손익률이 이 값 이상이면 그 종목을 시장가로 판다 (재도입 2026-10-30).
    # 0이면 끈다. 손절과 같은 순손익률 기준이다.
    take_profit_percent: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PERCENT", "2")))
    # AI 매도 판단 프롬프트에 '고점 대비 반납' 수치를 실을 최소 고점(%) — 이보다 낮은 고점은
    # 노이즈라 반납 수치를 주지 않는다 (`exit_advisor._peak_text`). `.env` 전용.
    ai_exit_drawdown_percent: float = field(
        default_factory=lambda: float(os.getenv("AI_EXIT_DRAWDOWN_PERCENT", "1"))
    )

    # AI 매도 판단 호출 단계 폭(%p) — 순손익률이 이 폭의 단계를 새로 넘을 때마다 부른다.
    # UI '리스크 관리'에서 조정 (2026-10-29). 너무 작으면 틱마다 LLM을 부르므로 validate가 막는다.
    ai_exit_step_percent: float = field(
        default_factory=lambda: float(os.getenv("AI_EXIT_STEP_PERCENT", "0.5"))
    )

    # 월 순수익 게이트 (스펙 2026-09-22 4-A) — 잘 되고 있을 때는 프롬프트를 건드리지 않는다.
    # 매도 프롬프트 자동 수정(`tune_exit_prompt`)에 걸린다. `.env` 전용.
    tune_skip_monthly_return_percent: float = field(
        default_factory=lambda: _parse_tune_skip_percent(os.getenv("TUNE_SKIP_MONTHLY_RETURN_PERCENT"))
    )

    @property
    def tune_skip_monthly_return_ratio(self) -> float:
        return self.tune_skip_monthly_return_percent / 100

    @property
    def ai_exit_drawdown_ratio(self) -> float:
        return self.ai_exit_drawdown_percent / 100

    @property
    def stop_loss_ratio(self) -> float:
        return self.stop_loss_percent / 100

    @property
    def take_profit_ratio(self) -> float:
        return self.take_profit_percent / 100

    # 청산 경로 적용 여부 — UI 체크박스가 켜고 끄며, 바뀔 때마다 `.env`에 저장된다
    # (확정 2026-09-09, PRD 5.5-B "청산 적용 여부"). 다른 리스크 설정과 달리 엔진을
    # 재시작하지 않고 돌고 있는 엔진에 바로 밀어 넣으므로(`EngineThread.set_exit_flags`),
    # 여기서 읽은 값이 쓰이는 것은 엔진이 새로 뜨는 순간뿐이다.
    # 둘 다 꺼져 있으면 실시간 청산이 사라지고 15:15 강제청산까지 보유한다.
    ai_exit_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("AI_EXIT_ENABLED"), True)
    )
    stop_loss_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("STOP_LOSS_ENABLED"), True)
    )

    # 2호 ETF 매매의 건별 순수익률 누적합이 -이 값(%) 이하가 되면 매수를 멈춘다 (자동 중단 장치).
    # 누적은 `etf_start_date`부터 센다 — 비우면 처음부터. 풀려면 사람이 둘 중 하나를 바꾼다.
    etf_kill_loss_percent: float = field(
        default_factory=lambda: float(os.getenv("ETF_KILL_LOSS_PERCENT", "5"))
    )
    etf_start_date: str = field(default_factory=lambda: os.getenv("ETF_START_DATE", "").strip())
    # 2호 신호 문턱(%) — |20일 신호|가 이 값 미만이면 그날 쉰다. 0이면 매일 산다(2026-10-29 현행).
    # 금요일 규칙 자동 평가가 바꾸는 손잡이다 (docs/superpowers/specs/2026-10-29-rule-auto-tuning-design.md).
    etf_signal_band_percent: float = field(
        default_factory=lambda: float(os.getenv("ETF_SIGNAL_BAND_PERCENT", "0"))
    )
    # 오늘 장중 움직임으로 방향을 뒤집을지 (`etf_regime.FLIP_PERCENT`, 1=켬). 규칙 자동 평가의 손잡이.
    etf_flip_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("ETF_FLIP_ENABLED"), True)
    )
    # 규칙 자동 평가가 조건을 모두 만족했을 때 `.env`를 실제로 바꿀지 (1=켬, 0이면 평가·메일만)
    rule_auto_apply_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("RULE_AUTO_APPLY_ENABLED"), True)
    )

    # 손절 판정(및 AI 매도 판단 프롬프트·UI의 합산 순손익 표시)에 반영할 비용 —
    # 매매수수료(매수·매도 동일), 세금(매도 시만), 슬리피지(추정)
    commission_percent: float = field(default_factory=lambda: float(os.getenv("COMMISSION_PERCENT", "0.015")))
    tax_percent: float = field(default_factory=lambda: float(os.getenv("TAX_PERCENT", "0.20")))
    slippage_percent: float = field(default_factory=lambda: float(os.getenv("SLIPPAGE_PERCENT", "0.1")))

    @property
    def commission_ratio(self) -> float:
        return self.commission_percent / 100

    @property
    def tax_ratio(self) -> float:
        return self.tax_percent / 100

    @property
    def slippage_ratio(self) -> float:
        return self.slippage_percent / 100

    def validate(self) -> None:
        if self.mode not in ("live", "paper"):
            raise ValueError(f"TRADE_MODE must be 'live' or 'paper', got: {self.mode}")
        # 시각 두 개의 관계는 여기서 막는다 — 값이 깨진 경우(기본값 폴백)와 달리, 순서가
        # 뒤집히면 추천이 없는 채로 매수가 돌아 그날 매매가 조용히 통째로 빈다.
        open_time = _parse_hhmm(MARKET_OPEN_HHMM, MARKET_OPEN_HHMM, "MARKET_OPEN")
        if _minutes(self.recommend_time) < _minutes(open_time):
            raise ValueError(
                f"RECOMMEND_TIME은 개장({MARKET_OPEN_HHMM}) 이후여야 합니다: {self.recommend_time_hhmm}"
            )
        gap = _minutes(self.buy_time) - _minutes(self.recommend_time)
        if gap < MIN_RECOMMEND_TO_BUY_MINUTES:
            raise ValueError(
                f"BUY_TIME은 RECOMMEND_TIME보다 {MIN_RECOMMEND_TO_BUY_MINUTES}분 이상 뒤여야 "
                f"합니다: 추천 {self.recommend_time_hhmm} / 매수 {self.buy_time_hhmm}"
            )
        if self.ai_exit_step_percent < MIN_AI_EXIT_STEP_PERCENT:
            raise ValueError(
                f"AI_EXIT_STEP_PERCENT는 {MIN_AI_EXIT_STEP_PERCENT} 이상이어야 합니다: "
                f"{self.ai_exit_step_percent}"
            )
        if self.take_profit_percent < 0:
            raise ValueError(f"TAKE_PROFIT_PERCENT는 0 이상이어야 합니다: {self.take_profit_percent}")
        if self.etf_start_date:
            try:
                date.fromisoformat(self.etf_start_date)
            except ValueError:
                raise ValueError(f"ETF_START_DATE는 YYYY-MM-DD 형식이어야 합니다: {self.etf_start_date}")
        if not self.app_key or not self.app_secret:
            raise ValueError("KIWOOM_APP_KEY and KIWOOM_APP_SECRET must be set")
        if self.mode == "live":
            # 실전 계좌 전환 시 명시적 확인 강제
            confirm = os.getenv("LIVE_TRADE_CONFIRMED", "")
            if confirm != "YES_I_UNDERSTAND":
                raise RuntimeError(
                    "실전 계좌 사용 시 환경변수 LIVE_TRADE_CONFIRMED=YES_I_UNDERSTAND 를 설정하세요."
                )
