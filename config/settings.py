import os
from dataclasses import dataclass, field


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
    # 전체 계좌 대비 최대 노출 비중 — 1호 전략 자체 규칙(1/2)보다 느슨하게 잡아
    # "전략 버그로 과도하게 매수되는 경우"만 걸러내는 상위 안전장치로 둔다
    max_total_exposure_ratio: float = 0.7

    # 익절/손절 라인 — UI/환경변수에는 %(예: 2)로 저장, 내부 계산은 비율(0.02)로 환산
    take_profit_percent: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PERCENT", "2")))
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
