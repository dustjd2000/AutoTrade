import logging
import os
from dataclasses import dataclass, field
from datetime import time as dt_time

logger = logging.getLogger(__name__)

# 1호 전략의 추천·매수 시각 기본값 — 둘 다 `.env`로만 바꾼다 (확정 2026-08-20, UI에서는
# 값을 보여주기만 한다). 개장(09:00)보다 앞서면 당일 지표가 오지 않아 후보 선정이 성립하지
# 않는다. 2026-08-14 이전 추천 시각은 08:45였다 — 장 전이라 당일 지표가 존재하지 않았다
# (PRD 10절 "개장 후 추천으로 이동").
DEFAULT_RECOMMEND_TIME_HHMM = "09:05"
# 매수는 추천보다 최소 `MIN_RECOMMEND_TO_BUY_MINUTES`분 뒤여야 한다 — 수집 약 40초 +
# LLM 타임아웃 상한 120초라, 그보다 앞당기면 추천이 아직 없는 채로 매수가 돌아 그날이
# 통째로 빈다. 간격을 넓히면 그만큼 추천 시점 가격과 주문 시점 가격이 벌어져 갭 판정에
# 걸리기 쉬워진다 (PRD 10절 "매수 타이밍 조정").
DEFAULT_BUY_TIME_HHMM = "09:08"
MIN_RECOMMEND_TO_BUY_MINUTES = 3
MARKET_OPEN_HHMM = "09:00"


# AI 매도 판단 호출 주기로 고를 수 있는 값 (분) — UI 콤보와 validate()가 함께 쓴다.
AI_EXIT_INTERVAL_CHOICES = (15, 30, 60)


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

    # LLM 연동 (1호 전략 — Anthropic Claude API로 확정).
    # 키는 UI "계좌 / API 설정"의 'Anthropic Key' 칸에서 다룬다(2026-08-25). 모델명은 .env 전용이다.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-5"))

    # DART 전자공시 — 공시 수집용. UI "계좌 / API 설정"의 'DART Key' 칸에서 다룬다(2026-08-25).
    # 비어 있으면 공시 없이 추천이 진행된다 (PRD 5.5-B '공시 수집과 악재 배제')
    dart_api_key: str = field(default_factory=lambda: os.getenv("DART_API_KEY", ""))

    # 리스크 설정
    max_position_ratio: float = 0.1
    max_daily_loss_ratio: float = 0.02
    # 전체 계좌 대비 최대 노출 비중 — investable_ratio_percent를 100%까지 열어둔 UI 설정값이
    # 이 안전장치에 걸려 매수가 거부되지 않도록 상한을 완화했다(기존 0.7, 확정 2026-08-03).
    # "전략 버그로 과도하게 매수되는 경우"만 걸러내는 상위 안전장치라는 검사 로직 자체는 유지.
    max_total_exposure_ratio: float = 1.0

    # 1호 전략 자금 배분 — 예수금 중 매매에 투입할 비율(%)과 추천받을 종목 수
    # UI 콤보박스 선택 범위: 비율 10~100(10 단위), 종목 수 1~10(1 단위) — 확정 2026-08-03
    investable_ratio_percent: int = field(default_factory=lambda: int(os.getenv("INVESTABLE_RATIO_PERCENT", "50")))
    target_stock_count: int = field(default_factory=lambda: int(os.getenv("TARGET_STOCK_COUNT", "3")))

    @property
    def investable_ratio(self) -> float:
        return self.investable_ratio_percent / 100

    # 1호 전략의 추천·매수 시각 — .env에는 "HH:MM" 문자열로 두고, 스케줄러가 쓰는
    # datetime.time은 프로퍼티로 환산한다 (확정 2026-08-04, 매수 시각 추가 2026-08-20)
    # AI 매도 판단 호출 주기 (분). UI 콤보가 주는 셋만 허용한다 — 그 밖의 값은 validate()가
    # 막는다 (PRD 5.5-B 'AI 매도 판단'). 비용이 여기서 갈린다: 하루 호출 수가 주기에 반비례한다.
    ai_exit_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("AI_EXIT_INTERVAL_MINUTES", "15"))
    )
    recommend_time_hhmm: str = field(
        default_factory=lambda: os.getenv("RECOMMEND_TIME", DEFAULT_RECOMMEND_TIME_HHMM)
    )
    buy_time_hhmm: str = field(
        default_factory=lambda: os.getenv("BUY_TIME", DEFAULT_BUY_TIME_HHMM)
    )

    @property
    def recommend_time(self) -> dt_time:
        """스케줄러에 넘길 추천 시각 (데이터 수집 → LLM 추천 → 추천 메일)."""
        return _parse_hhmm(
            self.recommend_time_hhmm, DEFAULT_RECOMMEND_TIME_HHMM, "RECOMMEND_TIME"
        )

    @property
    def buy_time(self) -> dt_time:
        """스케줄러에 넘길 매수 시각 (자금 산정 → 목표 매수가 지정가 주문)."""
        return _parse_hhmm(self.buy_time_hhmm, DEFAULT_BUY_TIME_HHMM, "BUY_TIME")

    # 익절/손절 라인 — UI/환경변수에는 %(예: 0.5)로 저장, 내부 계산은 비율(0.005)로 환산.
    # 둘 다 수수료·세금·슬리피지를 뺀 순손익률 기준이다 (PRD 5.5-B).
    take_profit_percent: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PERCENT", "0.5")))
    stop_loss_percent: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PERCENT", "2")))

    @property
    def take_profit_ratio(self) -> float:
        return self.take_profit_percent / 100

    @property
    def stop_loss_ratio(self) -> float:
        return self.stop_loss_percent / 100

    # 09:00 매수 직전 현재가가 목표 매수가보다 이 비율을 넘게 높으면 그 종목을 건너뛴다.
    # 추천은 전일 종가 기준이라 갭 상승한 날에는 목표가가 이미 의미를 잃는데, 지정가는
    # 시장가보다 낮게 걸리므로 그대로 두면 종일 미체결로 남거나 고가에 물린다 (PRD 5.5-B).
    buy_price_tolerance_percent: float = field(
        default_factory=lambda: float(os.getenv("BUY_PRICE_TOLERANCE_PERCENT", "2"))
    )

    @property
    def buy_price_tolerance_ratio(self) -> float:
        return self.buy_price_tolerance_percent / 100

    # 09:00 매수 직전 현재가가 전일 종가보다 이 비율을 넘게 낮으면 그 종목을 건너뛴다.
    # 목표 매수가는 눌림을 노려 전일 종가보다 낮게 잡히는 값이라, 위 갭 상승 판정처럼
    # 목표가를 기준으로 하한을 두면 얼마나 낮게 출발했는지를 잡지 못한다 (PRD 5.5-B).
    # 위와 달리 0이 '끔'이다 — 임계값 근거가 아직 약해 꺼둘 수 있어야 한다.
    gap_down_tolerance_percent: float = field(
        default_factory=lambda: float(os.getenv("GAP_DOWN_TOLERANCE_PERCENT", "1"))
    )

    @property
    def gap_down_tolerance_ratio(self) -> float:
        return self.gap_down_tolerance_percent / 100

    # 익절/손절 판정에 반영할 비용 — 매매수수료(매수·매도 동일), 세금(매도 시만), 슬리피지(추정)
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
        if self.ai_exit_interval_minutes not in AI_EXIT_INTERVAL_CHOICES:
            raise ValueError(
                f"AI_EXIT_INTERVAL_MINUTES는 {AI_EXIT_INTERVAL_CHOICES} 중 하나여야 합니다: "
                f"{self.ai_exit_interval_minutes}"
            )
        if not self.app_key or not self.app_secret:
            raise ValueError("KIWOOM_APP_KEY and KIWOOM_APP_SECRET must be set")
        if self.mode == "live":
            # 실전 계좌 전환 시 명시적 확인 강제
            confirm = os.getenv("LIVE_TRADE_CONFIRMED", "")
            if confirm != "YES_I_UNDERSTAND":
                raise RuntimeError(
                    "실전 계좌 사용 시 환경변수 LIVE_TRADE_CONFIRMED=YES_I_UNDERSTAND 를 설정하세요."
                )
