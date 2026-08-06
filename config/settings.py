import logging
import os
from dataclasses import dataclass, field
from datetime import time as dt_time

logger = logging.getLogger(__name__)

# 1호 전략 LLM 추천 시각의 기본값 — UI 콤보박스 선택 범위는 08:40~08:55(5분 단위)다.
# 09:00 매수보다 반드시 앞서야 하므로 상한을 08:55로 둔다 (확정 2026-08-04).
DEFAULT_RECOMMEND_TIME_HHMM = "08:45"


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

    # LLM 연동 (1호 전략 — Anthropic Claude API로 확정)
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-5"))

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

    # 1호 전략 LLM 추천 시각 — .env/UI에는 "HH:MM" 문자열로 저장, 스케줄러가 쓰는
    # datetime.time은 프로퍼티로 환산한다 (확정 2026-08-04)
    recommend_time_hhmm: str = field(
        default_factory=lambda: os.getenv("RECOMMEND_TIME", DEFAULT_RECOMMEND_TIME_HHMM)
    )

    @property
    def recommend_time(self) -> dt_time:
        """스케줄러에 넘길 추천 시각.

        값이 깨져 있으면 엔진 자체를 막지 않고 기본값으로 돌린다 — .env 한 줄 오타로
        그날 매매가 통째로 중단되는 편보다 낫다. 대신 경고를 남겨 넘어간 사실을 알린다.
        """
        try:
            hour, minute = (int(part) for part in self.recommend_time_hhmm.split(":"))
            return dt_time(hour, minute)
        except (AttributeError, ValueError):
            logger.warning(
                "RECOMMEND_TIME 값이 올바르지 않아 기본값 %s를 사용합니다: %r",
                DEFAULT_RECOMMEND_TIME_HHMM,
                self.recommend_time_hhmm,
            )
            hour, minute = (int(part) for part in DEFAULT_RECOMMEND_TIME_HHMM.split(":"))
            return dt_time(hour, minute)

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

    # 익절/손절 판정에 반영할 비용 — 매매수수료(매수·매도 동일), 세금(매도 시만), 슬리피지(추정)
    commission_percent: float = field(default_factory=lambda: float(os.getenv("COMMISSION_PERCENT", "0.015")))
    tax_percent: float = field(default_factory=lambda: float(os.getenv("TAX_PERCENT", "0.18")))
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
        if not self.app_key or not self.app_secret:
            raise ValueError("KIWOOM_APP_KEY and KIWOOM_APP_SECRET must be set")
        if self.mode == "live":
            # 실전 계좌 전환 시 명시적 확인 강제
            confirm = os.getenv("LIVE_TRADE_CONFIRMED", "")
            if confirm != "YES_I_UNDERSTAND":
                raise RuntimeError(
                    "실전 계좌 사용 시 환경변수 LIVE_TRADE_CONFIRMED=YES_I_UNDERSTAND 를 설정하세요."
                )
