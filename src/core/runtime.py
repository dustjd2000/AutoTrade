"""매매 런타임 조립 및 구동.

엔진 단독 실행은 하지 않고 UI에서만 제어하므로, UI 스레드가 이 모듈을 통해
전체 구성요소를 만들고 돌린다.
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Callable, List, Optional

from config.settings import Settings
from src.api.account import AccountClient
from src.api.auth import AuthClient
from src.api.market_data import MarketDataClient
from src.api.order import OrderClient
from src.api.websocket_client import WebSocketClient
# 액션 정의는 src/core/actions.py로 옮겼다. UI(main_window)와 기존 테스트가 이 모듈에서
# import하고 있으므로 이름을 그대로 다시 내보낸다.
from src.core.actions import (  # noqa: F401  (재수출)
    ACTION_LABELS,
    CONFIRM_ACTIONS,
    MANUAL_ACTIONS,
    ORDER_ACTIONS,
    SCHEDULED_ACTIONS,
    ActionRunner,
    ManualStep,
    close_out,
    manual_steps,
)
from src.core.daily_workflow import DailyWorkflow
from src.core.engine import TradingEngine
from src.core.events import ExitReason, OrderSide
from src.core.etf_exit_trigger import StepTrigger
from src.llm.exit_advisor import ExitAdvisor, HoldingView, UserSell, make_exit_prompt_store
from src.llm.exit_reviewer import ExitReviewer
from src.llm.exit_tuner import ExitPromptTuner
from src.logger.trade_store import AIExitDecisionRow, TradeStore
from src.notification.alert import AlertNotifier
from src.notification.email import EmailNotifier
from src.research.rule_tuning import RuleTuning
from src.risk.manager import RiskManager
from src.scheduler.scheduler import TimeScheduler
from src.strategy import etf_regime
from src.strategy.etf_regime import ETFRegimeStrategy
from src.ui.env_store import save_env

logger = logging.getLogger(__name__)

# 하루 흐름 트리거 시각 (PRD 5.5-C, 5.11)
# **결정(`settings.recommend_time`)과 매수 안전망(`settings.buy_time`)만 설정값이다** — 둘 다
# `.env`로만 바꾸고 UI는 값을 보여주기만 한다. 두 시각의 순서와 최소 간격은
# `Settings.validate`가 강제한다. 나머지는 장 운영 시간에 맞춰 고정한다. 결정 시각에
# 곧바로 시장가 매수가 나가고, 매수 시각은 그 사이 재시작으로 결정이 사라진 날의 그물이다.
DAILY_RESET_TIME = dt_time(8, 40)   # 일일 손실 한도·매매중지 초기화 (연속 실행 대비)
# 시장가 매수라 보통은 거둘 주문이 없다 — 매수 결과 메일을 보내는 시각이다.
CANCEL_UNFILLED_TIME = dt_time(10, 10)  # 미체결 매수 취소 → 매수 결과 메일
# 청산은 장마감 동시호가(15:20~15:30) '이전'에 내야 한다 — 동시호가에 들어간 시장가 주문은
# 15:30 종가에야 체결되어, 그 사이에 나간 리포트가 그 매도를 미정산으로 싣는다 (2026-08-12).
# 리포트는 반대로 마감 뒤로 5분 물려, 마감 동시호가 체결분까지 체결내역에 잡힌 뒤 집계한다.
FORCE_CLOSE_TIME = dt_time(15, 15)  # 당일 매도 원칙에 따른 미청산 포지션 정리
REPORT_TIME = dt_time(15, 35)       # 일일/월간 성과 리포트 이메일 (정규장 마감 15:30 이후)
# 매도 판단 검증·매도 프롬프트 자동 수정 — 리포트 다음이다. 마감 동시호가 체결분까지 체결내역에
# 잡힌 뒤라야 순손익을 제대로 대조한다 (PRD 5.14). 같은 큐를 타므로 리포트와 겹치면 순서대로 돈다.
REVIEW_EXITS_TIME = dt_time(15, 40)
# 2호 규칙 자동 검증 (docs/superpowers/specs/2026-10-29-rule-auto-tuning-design.md).
# 분봉 수집은 매도 검증 뒤, 평가는 금요일 장 마감 뒤 — 15:15 강제청산 뒤라 적용 시점에 보유 종목이 없다.
MINUTE_COLLECT_TIME = dt_time(15, 45)
RULE_EVAL_TIME = dt_time(16, 0)
RULE_EVAL_WEEKDAY = 4  # 금요일
# 자동 적용이 쓰는 .env — UI(`main_window.ENV_PATH`)와 같은 파일이다
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# 시세 끊김 감시 — 손절이 실시간 시세에만 의존하므로(키움 REST 스탑오더 미지원),
# 보유 종목이 있는데 시세가 끊기면 손절이 조용히 멈춘다. 그 공백을 알린다.
MARKET_OPEN_TIME = dt_time(9, 0)
MARKET_CLOSE_TIME = dt_time(15, 30)
QUOTE_CHECK_INTERVAL_SECONDS = 60
QUOTE_STALE_AFTER_SECONDS = 300

# 전량 매도 완료 감시 — 보유 종목이 다 팔렸으면 15:35를 기다리지 않고 결과 리포트를 보낸다.
CLOSEOUT_CHECK_INTERVAL_SECONDS = 30
# 매도 체결이 잔고·체결내역에 반영될 시간. 주문 접수 직후에 집계하면 방금 팔린 종목이
# '보유중'으로 실린다 (DailyWorkflow._fill_buy_prices의 같은 시차 참고).
CLOSEOUT_SETTLE_SECONDS = 60

# 매수 체결 완료 감시 — 접수한 매수가 전부 체결됐으면 10:10을 기다리지 않고 결과 메일을 보낸다.
# 판정이 체결내역 조회를 부르므로 시세 감시보다 넉넉한 주기를 쓴다. 결과 메일이 나가면
# 기록 파일이 지워져 판정이 API를 부르지 않게 되므로, 하루 호출은 매수 시각~체결확인 구간에 그친다.
BUY_RESULT_CHECK_INTERVAL_SECONDS = 60

# 예수금 캐시 갱신 — 입금 등 장중 잔고 변동을 "총 매수가능 금액" UI 표시에 반영한다.
# 시세 틱과 달리 예수금은 자연스러운 갱신 계기가 없어 별도 주기로 돈다.
CASH_REFRESH_INTERVAL_SECONDS = 60

# 15:15 강제청산 직전에는 부르지 않는다 — 어차피 곧 팔린다.
AI_EXIT_END_TIME = dt_time(15, 0)
AI_EXIT_POLL_SECONDS = 30.0
# 휴장으로 판정돼도 그대로 도는 스케줄 (PRD 10절 '휴장일 자동 판정').
# 상태 초기화는 다음 거래일을 위해 필요하고, 마감 정리는 판정이 틀렸을 때 보유분을 그대로
# 넘기지 않기 위해 남긴다 — 팔 것이 없으면 조용히 끝난다.
HOLIDAY_ALWAYS_ACTIONS = ("daily_reset", "close_out")


def _submit(runtime: "Runtime", action: str) -> Callable[[], None]:
    """스케줄 잡을 실행 통로 접수로 바꾼다 — 버튼과 같은 큐를 탄다.

    오늘이 휴장으로 판정됐으면(`engine.market_closed_today`) 대부분의 스케줄을 건너뛴다.
    **UI 버튼은 막지 않는다** — 사용자가 직접 누른 실행이고, 판정이 틀렸을 때 손으로
    돌릴 길을 남겨야 한다 (버튼은 `ActionRunner.submit`을 직접 부른다).
    """

    def job() -> None:
        if runtime.engine.market_closed_today and action not in HOLIDAY_ALWAYS_ACTIONS:
            logger.info(
                "휴장으로 판정해 '%s' 작업을 건너뜁니다.", ACTION_LABELS.get(action, action)
            )
            return
        runtime.runner.submit(action)

    return job


def _trading_days_only(job, name: str):
    """거래일이 아닌 날에는 작업을 건너뛰도록 감싼다.

    앱을 며칠 연속 켜두면 스케줄러가 요일과 무관하게 매일 발동한다. 주말에 그대로 두면
    쓸모없는 결정·리포트 메일이 나가고, 매수는 거래소에서 거부된다.
    **공휴일은 여기서 걸러내지 못한다** — 요일만 보기 때문이다. 대신 결정 시각에 KODEX 200
    당일 거래 여부로 판정해(`etf_regime.traded_today`) 그날 남은 스케줄을 `_submit`이 건너뛴다
    (PRD 5.5-C '휴장 판정', 10절 '휴장일 자동 판정').
    """

    def skipped() -> bool:
        if is_trading_day():
            return False
        logger.info("거래일이 아니므로 '%s' 작업을 건너뜁니다.", name)
        return True

    if inspect.iscoroutinefunction(job):

        async def guarded_async() -> None:
            if not skipped():
                await job()

        return guarded_async

    def guarded() -> None:
        if not skipped():
            job()

    return guarded


def _on_weekday(weekday: int, job: Callable[[], None], now_fn=None) -> Callable[[], None]:
    """정한 요일에만 작업을 넘긴다 — 스케줄러는 매일 발동하므로 주간 작업은 여기서 거른다."""

    def guarded() -> None:
        if (now_fn or datetime.now)().weekday() == weekday:
            job()

    return guarded


@dataclass
class Runtime:
    settings: Settings
    engine: TradingEngine
    scheduler: TimeScheduler
    ws_client: WebSocketClient
    workflow: DailyWorkflow
    # 보유 종목을 하나씩 지금 매도할지 판단하는 LLM 모듈 (PRD 5.5-C '매도 판단').
    exit_advisor: ExitAdvisor
    # 스케줄 잡과 UI 버튼이 공유하는 실행 통로. build_runtime이 Runtime을 만든 뒤에
    # 채운다 — ActionRunner가 runtime을 참조해야 해서 순서를 뒤집을 수 없다.
    runner: Optional[ActionRunner] = None
    # AI 매도 판단 트리거 — 순손익률이 설정 폭(`AI_EXIT_STEP_PERCENT`)의 단계를 새로 넘을 때 묻는다 (PRD 5.5-C '매도 판단')
    etf_exit_trigger: StepTrigger = field(default_factory=StepTrigger)
    # 2호 규칙 자동 검증 — 15:45 분봉 수집, 금요일 16:00 평가·자동 적용
    research: Optional[RuleTuning] = None
    # 엔진이 실제로 시작된 직후(토큰·계좌 조회 성공 뒤)에 부르는 알림 — UI가 '재시작 성공'을 판단한다
    on_ready: Optional[Callable[[], None]] = None


# 손절 판정(순손익률)에 넣을 매도세율 — 2호 전략은 지수 ETF만 사고, 국내 주식형 ETF는
# 증권거래세가 면제다. 주식 세율(`TAX_PERCENT`)을 그대로 넣으면 비용이 0.2%p 과대 계상돼
# 손절이 설정보다 일찍 나간다 (PRD 5.5-C).
ETF_TAX_RATIO = 0.0


def register_jobs(scheduler: TimeScheduler, runtime: Runtime, settings: Settings) -> None:
    """스케줄러에 시간 기반 작업을 등록한다. 모두 거래일 가드를 거치고, 규칙 평가는 금요일에만 넘긴다."""
    for trigger_time, action in (
        (DAILY_RESET_TIME, "daily_reset"),
        (settings.recommend_time, "recommend"),
        (settings.buy_time, "buy"),
        (CANCEL_UNFILLED_TIME, "cancel_unfilled"),
        (FORCE_CLOSE_TIME, "close_out"),
        (REPORT_TIME, "daily_report"),
        (REVIEW_EXITS_TIME, "review_exits"),
        (MINUTE_COLLECT_TIME, "collect_minute_bars"),
    ):
        scheduler.add_job(
            trigger_time,
            _trading_days_only(_submit(runtime, action), action),
            name=action,
        )
    scheduler.add_job(
        RULE_EVAL_TIME,
        _trading_days_only(
            _on_weekday(RULE_EVAL_WEEKDAY, _submit(runtime, "evaluate_rules")), "evaluate_rules"
        ),
        name="evaluate_rules",
    )


def build_runtime(settings: Settings) -> Runtime:
    """설정으로부터 매매 런타임 전체를 조립한다 (아직 시작하지는 않는다)."""
    auth = AuthClient(settings)
    market_data = MarketDataClient(settings, auth)
    order_client = OrderClient(settings, auth)
    account = AccountClient(settings, auth)
    ws_client = WebSocketClient(settings, auth)

    # 2호 전략 (PRD 5.5-C) — 1호(LLM 대형주 추천)는 2026-10-23에 폐지됐다
    strategy = ETFRegimeStrategy(investable_ratio=settings.investable_ratio)
    risk_manager = RiskManager(
        max_position_ratio=settings.max_position_ratio,
        max_daily_loss_ratio=settings.max_daily_loss_ratio,
        stop_loss_ratio=settings.stop_loss_ratio,
        stop_loss_enabled=settings.stop_loss_enabled,
        take_profit_ratio=settings.take_profit_ratio,
        trail_activate_ratio=settings.trail_activate_ratio,
        trail_giveback_ratio=settings.trail_giveback_ratio,
        max_total_exposure_ratio=settings.max_total_exposure_ratio,
        commission_rate=settings.commission_ratio,
        tax_rate=ETF_TAX_RATIO,
        slippage_rate=settings.slippage_ratio,
    )
    trade_store = TradeStore()
    email = EmailNotifier(settings)

    engine = TradingEngine(
        auth=auth,
        market_data=market_data,
        order_client=order_client,
        account=account,
        strategy=strategy,
        risk_manager=risk_manager,
        trade_store=trade_store,
        notifier=AlertNotifier(email),
        emergency_action=settings.emergency_action,
        ai_exit_enabled=settings.ai_exit_enabled,
        ai_exit_drawdown_ratio=settings.ai_exit_drawdown_ratio,
    )
    ws_client.on_data(engine.on_market_data)

    # 매도 프롬프트 저장소는 AI 매도 판단(읽기)과 15:40 자동 수정(쓰기)이 함께 쓴다
    exit_prompt_store = make_exit_prompt_store()
    workflow = DailyWorkflow(
        strategy=strategy,
        engine=engine,
        account=account,
        trade_store=trade_store,
        email=email,
        exit_reviewer=ExitReviewer(settings),
        exit_tuner=ExitPromptTuner(settings),
        exit_prompt_store=exit_prompt_store,
        tune_skip_monthly_return_ratio=settings.tune_skip_monthly_return_ratio,
        ws_client=ws_client,
        etf_kill_loss_percent=settings.etf_kill_loss_percent,
        etf_start_date=date.fromisoformat(settings.etf_start_date) if settings.etf_start_date else None,
        signal_band_percent=settings.etf_signal_band_percent,
        flip_enabled=settings.etf_flip_enabled,
    )
    exit_advisor = ExitAdvisor(settings, prompt_store=exit_prompt_store)

    # 매수/강제청산은 실시간 손절 감시와 직렬화되도록 루프 스레드에서 그대로 실행하고,
    # 오래 걸리는 수집·LLM·리포트는 루프를 막지 않도록 별도 스레드로 넘긴다 — 이 배분은
    # 이제 ActionRunner가 ManualStep.touches_orders를 보고 결정한다.
    # 모든 작업은 거래일에만 돌도록 감싼다 (_trading_days_only 참고). 거래일 가드는
    # 스케줄 쪽에만 있다 — UI 버튼은 지금처럼 요일과 무관하게 눌린다.
    scheduler = TimeScheduler()
    runtime = Runtime(
        settings=settings,
        engine=engine,
        scheduler=scheduler,
        ws_client=ws_client,
        workflow=workflow,
        exit_advisor=exit_advisor,
        etf_exit_trigger=StepTrigger(settings.ai_exit_step_percent),
    )
    runtime.runner = ActionRunner(runtime)
    runtime.research = RuleTuning(
        settings=settings,
        market_data=market_data,
        trade_store=trade_store,
        email=email,
        env_writer=lambda values: save_env(ENV_PATH, values),
        open_tickers=lambda: engine.open_tickers,
    )

    register_jobs(scheduler, runtime, settings)

    return runtime


def is_trading_day(now: Optional[datetime] = None) -> bool:
    """거래일(월~금)인지. 공휴일은 여기서 판별하지 않는다 — 당일 지표 판정이 맡는다
    (`etf_regime.traded_today` → `engine.market_closed_today` → `_submit`)."""
    now = now or datetime.now()
    return now.weekday() < 5  # 5·6 = 토·일


def is_market_hours(now: Optional[datetime] = None) -> bool:
    """정규장 시간대인지 (월~금, 09:00~15:30). 공휴일은 판별하지 않는다."""
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    return MARKET_OPEN_TIME <= now.time() <= MARKET_CLOSE_TIME


def quote_stall_seconds(runtime: Runtime, now: Optional[datetime] = None) -> Optional[float]:
    """시세가 끊긴 시간(초). 감시할 보유 종목이 없거나 장 시간이 아니면 None.

    보유 종목이 있는데 시세가 오지 않으면 손절 판정이 멈춘 상태다.
    """
    now = now or datetime.now()
    if not runtime.engine.open_tickers or not is_market_hours(now):
        return None

    last = runtime.engine.last_market_data_at
    if last is None:
        # 매수는 됐는데 첫 시세조차 못 받은 상태 — 구독 실패 가능성
        return float("inf")
    return (now - last).total_seconds()


def _describe_stall(stalled_seconds: float) -> str:
    """끊김 상태를 한 문장으로 만든다 (경고 로그·메일 공통)."""
    if stalled_seconds == float("inf"):
        return "실시간 시세를 한 번도 받지 못했습니다 (구독 실패 가능)."
    if stalled_seconds < 120:
        return f"실시간 시세가 {stalled_seconds:.0f}초간 끊겼습니다."
    return f"실시간 시세가 {stalled_seconds / 60:.0f}분간 끊겼습니다."


async def watch_quote_stall(
    runtime: Runtime,
    interval_seconds: float = QUOTE_CHECK_INTERVAL_SECONDS,
    stale_after_seconds: float = QUOTE_STALE_AFTER_SECONDS,
) -> None:
    """보유 종목이 있는데 시세가 끊기면 경고한다 (같은 공백에 대해 한 번만)."""
    warned = False
    while True:
        await asyncio.sleep(interval_seconds)

        stalled = quote_stall_seconds(runtime)
        if stalled is None:
            warned = False
            continue

        if stalled >= stale_after_seconds:
            if not warned:
                held = runtime.engine.open_tickers
                detail = _describe_stall(stalled)
                logger.error(
                    "%s 손절 감시가 멈춘 상태입니다. 보유: %s", detail, held
                )
                runtime.engine.notify(
                    f"[경고] {detail} 손절 감시가 멈췄습니다. "
                    f"보유 종목: {held}. 앱과 네트워크 상태를 확인하세요."
                )
                warned = True
        elif warned:
            logger.info("실시간 시세 수신이 복구되었습니다.")
            runtime.engine.notify("실시간 시세 수신이 복구되었습니다.")
            warned = False


def closeout_report_due(
    runtime: Runtime,
    now: Optional[datetime] = None,
    settle_seconds: float = CLOSEOUT_SETTLE_SECONDS,
) -> bool:
    """보유 종목 전량 매도가 끝나 결과 리포트를 보낼 때인지.

    청산 주문 접수만으로는 판단하지 않는다 — 체결이 잔고에 반영되기까지 시차가 있어 그
    사이에 집계하면 방금 판 종목이 '보유중'으로 실린다. 잔고 기준 보유 목록이 비어 있고
    (engine.open_tickers는 시세 틱마다 잔고로 갱신된다) settle_seconds가 지난 뒤에 True가 된다.
    """
    now = now or datetime.now()
    closed_at = runtime.engine.closed_out_at
    if closed_at is None:
        return False
    # 자정을 넘겨 앱이 켜져 있는 경우 — 어제 청산으로 오늘 리포트를 보내지 않는다
    if closed_at.date() != now.date():
        return False
    # 체결이 아직 잔고에 반영되지 않았거나, 청산 후 다시 매수됐다
    if runtime.engine.open_tickers:
        return False
    return (now - closed_at).total_seconds() >= settle_seconds


async def watch_closeout_report(
    runtime: Runtime,
    interval_seconds: float = CLOSEOUT_CHECK_INTERVAL_SECONDS,
) -> None:
    """보유 종목이 전부 매도되면 15:35를 기다리지 않고 결과 리포트를 보낸다.

    청산 건당 한 번만 시도한다 — 발송에 실패하거나 매도 체결이 아직 확인되지 않아
    미뤄지면 표시가 서지 않으므로 15:35 스케줄이 대신 보낸다
    (DailyWorkflow.send_final_report). 매수로 보유가 다시 생기면 엔진이 청산 시각을
    지우므로, 그 보유분을 또 전량 매도하면 새 시각으로 다시 발송한다.
    """
    reported_at: Optional[datetime] = None
    while True:
        await asyncio.sleep(interval_seconds)

        closed_at = runtime.engine.closed_out_at
        if closed_at == reported_at or not closeout_report_due(runtime):
            continue

        reported_at = closed_at
        logger.info(
            "보유 종목 전량 매도 완료 (%s) — 15:35를 기다리지 않고 결과 리포트를 발송합니다.",
            closed_at.strftime("%H:%M:%S"),
        )
        try:
            # SMTP·체결조회가 몇 초 걸리므로 이벤트 루프를 막지 않는다
            # (ActionRunner가 ManualStep.touches_orders를 보고 배분하는 것과 같은 이유)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: runtime.workflow.send_final_report(closed_out=True)
            )
        except Exception:
            logger.exception("전량 매도 결과 리포트 발송 실패 — 15:35 리포트에 맡깁니다.")


async def watch_buy_result(
    runtime: Runtime,
    interval_seconds: float = BUY_RESULT_CHECK_INTERVAL_SECONDS,
) -> None:
    """접수한 매수가 전부 체결되면 10:10을 기다리지 않고 결과 메일을 보낸다.

    주문 지정가를 허용 밴드 상단으로 올린 뒤로(2026-08-26) 접수 직후 전량 체결되는 날이
    대부분인데, 결과 메일만 `CANCEL_UNFILLED_TIME`에 묶여 한 시간 늦게 나갔다. 매도 쪽
    `watch_closeout_report`와 같은 구조다.

    판정은 `DailyWorkflow.buy_orders_filled` — 체결내역 조회의 주문번호 대조다. 2026-09-01에
    되돌린 잔고 대조와 달리 남의 물량에 속지 않는다 (PRD 10절).

    발송은 직접 부르지 않고 실행 통로에 접수한다. 매수가 아직 도는 중이면 그 뒤에서
    기다리고, 실시간 손절 감시와도 직렬화된다. 이미 큐에 있으면 러너가 걸러낸다.
    메일이 나가면 기록 파일이 지워지므로 10:10·15:15가 같은 메일을 다시 보내지 않는다 —
    다만 그 삭제(`_clear_buy_records`)가 `OSError`로 실패하면(Windows에서 백신·OneDrive가
    파일을 잠근 경우 등) 판정이 계속 True로 남아 재접수를 시도할 수 있으므로, 하루 한 번
    제출했으면 그 날짜 동안은 더 시도하지 않는다 (`watch_closeout_report`의 `reported_at`과
    같은 구조).
    """
    submitted_on: Optional[date] = None
    while True:
        await asyncio.sleep(interval_seconds)

        today = date.today()
        if submitted_on == today:
            continue

        try:
            # 체결내역 조회는 페이지네이션이 걸린 블로킹 requests 호출이라 이벤트 루프를
            # 막으면 그동안 WebSocket PING 응답도, 실시간 손절 콜백도 멈춘다.
            filled = await asyncio.get_running_loop().run_in_executor(
                None, runtime.workflow.buy_orders_filled
            )
            if not filled:
                continue
        except Exception:
            logger.exception("매수 체결 확인에 실패했습니다 — 10:10 마무리에 맡깁니다.")
            continue

        logger.info(
            "접수한 매수가 전부 체결됐습니다 — %s을 기다리지 않고 매수 결과 메일을 발송합니다.",
            CANCEL_UNFILLED_TIME.strftime("%H:%M"),
        )
        submitted_on = today
        if not runtime.runner.submit("cancel_unfilled"):
            logger.info("매수 결과 메일 발송이 이미 큐에 있어 건너뜁니다 (중복 접수 아님).")


async def watch_cash_refresh(
    runtime: Runtime,
    interval_seconds: float = CASH_REFRESH_INTERVAL_SECONDS,
) -> None:
    """예수금 캐시를 주기적으로 다시 읽는다 — 입금 등으로 잔고가 바뀌어도

    "총 매수가능 금액" UI 표시가 최신 상태를 따라가도록 한다 (engine.cash_snapshot).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        # 네트워크 호출이 이벤트 루프를 막지 않도록 별도 스레드로 넘긴다
        # (ActionRunner가 ManualStep.touches_orders를 보고 배분하는 것과 같은 이유)
        await asyncio.get_running_loop().run_in_executor(None, runtime.engine.refresh_cash)


def ai_exit_due(
    runtime: Runtime,
    now: Optional[datetime] = None,
    last_called_at: Optional[datetime] = None,
) -> bool:
    """이번 사이클에 AI 매도 판단을 부를 조건인지 (PRD 5.5-C '매도 판단').

    보유 종목 순손익률이 설정 폭(기본 0.5%p)의 이익·손실 단계를 새로 넘었거나, 이익 고점에서
    단계를 새로 내려왔으면(0단계까지) True다 (`StepTrigger`).
    1호 전략 시절의 15분 주기·하루 상한·장중 공시 트리거는 2026-10-23에 폐지됐다.
    UI의 AI 매도 판단 체크박스(`engine.ai_exit_enabled`)는 그대로 따른다 — 끄면 묻지 않는다.
    시간은 개장~`AI_EXIT_END_TIME`(15:15 강제청산 직전에는 어차피 곧 팔린다). 호출이 실패해도
    닿은 단계는 기록된 채로 남는다 — 실패는 보유로 떨어진다는 원칙과 같다.
    `last_called_at`은 쓰지 않는다 (`maybe_run_ai_exit_cycle`과의 계약 때문에 받기만 한다).
    """
    now = now or datetime.now()
    if not is_trading_day(now):
        return False
    engine = runtime.engine
    trigger = getattr(runtime, "etf_exit_trigger", None)
    if trigger is None or not engine.ai_exit_enabled:
        return False
    if not (MARKET_OPEN_TIME <= now.time() <= AI_EXIT_END_TIME):
        return False
    fired = []
    for position in engine.exit_candidates():
        step = trigger.crossed(position.ticker, engine.position_net_return(position), now.date())
        if step is not None:
            fired.append(f"{position.ticker} {step * trigger.step_percent:+.1f}% 단계")
    if fired:
        logger.info("AI 매도 판단 트리거: %s", ", ".join(fired))
    return bool(fired)


AI_EXIT_FAILURE_TEXT = "LLM 호출 실패·타임아웃·형식 오류 (이번 주기는 매도하지 않습니다)"
AI_EXIT_MISSING_TEXT = "(응답 없음 — 보유로 처리)"


def _record_ai_exit_decisions(runtime: Runtime, now: datetime, holdings_view, decision) -> None:
    """이번 주기의 판정을 보유 종목마다 한 줄씩 남긴다 (스펙 2026-09-22 2.1).

    매도 실행 **전에** 부른다 — 무엇을 판단했는지가 기록의 대상이고, 무엇이 팔렸는지는
    trades가 따로 남긴다. 기록 실패가 매도를 막으면 안 되므로 예외를 삼킨다 — 행을
    만드는 과정(예: `exit_advisor.prompt_version`이 프롬프트 저장소 파일을 읽다 던지는 경우)도
    `store.save_ai_exit_decisions` 호출과 함께 통째로 try 안에 둔다. 종전에는 DB 저장
    호출만 감싸, 그 앞의 행 조립 단계에서 터지면 예외가 그대로 새 나가 매도를 막을 수
    있었다 (F4, 2026-09-22 최종 리뷰).
    """
    store = runtime.engine.trade_store
    if store is None:
        return
    try:
        version = getattr(runtime.exit_advisor, "prompt_version", "") or ""
        verdicts = {}
        if decision is not None:
            for d in decision.decisions:
                verdicts.setdefault(d.ticker, d)
        rows = []
        for h in holdings_view:
            verdict = verdicts.get(h.ticker)
            if decision is None:
                sell, ok, reason = False, False, AI_EXIT_FAILURE_TEXT
            elif verdict is None:
                sell, ok, reason = False, True, AI_EXIT_MISSING_TEXT
            else:
                sell, ok, reason = verdict.sell, True, verdict.reason
            rows.append(
                AIExitDecisionRow(
                    day=now.date(),
                    at=now,
                    ticker=h.ticker,
                    name=h.name,
                    sell=sell,
                    ok=ok,
                    reason=reason,
                    net_return=h.net_return,
                    peak_return=h.peak_return,
                    current_price=h.current_price,
                    exit_prompt_version=version,
                )
            )
        store.save_ai_exit_decisions(rows)
    except Exception:
        logger.warning("AI 매도 판단 기록 실패 — 매도 흐름은 그대로 진행합니다.", exc_info=True)


def collect_user_sells(runtime: Runtime, holdings, now: datetime) -> List[UserSell]:
    """사용자가 키움 앱에서 직접 판 당일 매도를 종목별로 묶는다 (프롬프트 v4 참고 정보).

    장중에는 외부 체결이 DB에 없다(`apply_fills`는 리포트 때 돈다) — 체결내역을 직접 조회해
    이 프로그램이 낸 주문번호를 뺀 매도 체결을 사용자 매도로 본다. REST 호출이라 별도
    스레드에서 불러야 한다. 어떤 실패도 빈 목록으로 떨어진다 — 참고 정보일 뿐이라 매도
    판단을 막지 않는다.
    """
    try:
        engine = runtime.engine
        own = engine.trade_store.program_order_ids(now.date())
        held_avg = {p.ticker: p.avg_price for p in holdings}
        grouped = {}
        for fill in engine.order_client.get_today_fills():
            if fill.side != OrderSide.SELL or fill.filled_quantity <= 0 or fill.order_id in own:
                continue
            grouped.setdefault(fill.ticker, []).append(fill)

        sells = []
        for ticker, fills in grouped.items():
            qty = sum(f.filled_quantity for f in fills)
            price = sum(f.filled_price * f.filled_quantity for f in fills) / qty
            buy_price = engine.trade_store.same_day_buy_price(ticker, now.date()) or held_avg.get(ticker)
            sells.append(
                UserSell(
                    ticker=ticker,
                    name=next((f.name for f in fills if f.name), ""),
                    quantity=qty,
                    price=price,
                    buy_price=buy_price,
                )
            )
        return sells
    except Exception:
        logger.warning("사용자 직접 매도 조회 실패 — 이번 판단에서는 그 절을 뺍니다.", exc_info=True)
        return []


def collect_index_return(runtime: Runtime, now: datetime) -> Optional[float]:
    """오늘 KODEX 200의 시가 대비 수익률(%) — 지수 ETF 보유의 방향 유불리 판단 재료 (프롬프트 v5).

    REST 호출이라 별도 스레드에서 불러야 한다. 실패하면 None — 그 줄만 빠지고 판단은 그대로 돈다.
    """
    try:
        candles = runtime.engine.market_data.get_ohlcv(
            etf_regime.LONG_ETF[0], base_date=now.date()
        )
        return etf_regime.today_return(candles, now.date())
    except Exception:
        logger.warning("KODEX 200 오늘 수익률 조회 실패 — 지수 방향 줄을 뺍니다.", exc_info=True)
        return None


async def run_ai_exit_cycle(runtime: Runtime, now: Optional[datetime] = None) -> None:
    """AI 매도 판단 한 사이클을 실행한다 — 궤적에 점을 남기고 종목마다 판정해 매도로 정해진
    종목만 판다.

    호출 전에 `ai_exit_due`로 이번 사이클을 돌 조건인지 먼저 확인해야 한다. 이 함수
    자체는 보유 종목 유무 등을 다시 확인하지 않는다 (게이트와 실행을 분리해 각각 따로
    테스트하기 위함).
    """
    now = now or datetime.now()
    engine = runtime.engine
    # 이익 반납 감시의 즉시 호출 표시는 쓰지 않지만(호출은 단계 트리거가 정한다) 비워 둔다
    engine.exit_drawdown.take_urgent()
    # 판단 전에 남은 고점을 DB에 남긴다 — 재시작해도 프롬프트의 고점이 이어지게 한다
    engine.flush_exit_peaks(force=True)

    holdings = engine.exit_candidates()
    if not holdings:
        return

    try:
        portfolio_return = engine.risk_manager.portfolio_return(holdings)
        if portfolio_return is None:
            logger.info("AI 매도 판단 — 합산 순손익률을 계산할 수 없어 이번 주기를 건너뜁니다.")
            return

        per_ticker = {p.ticker: engine.position_net_return(p) for p in holdings}
        prices = {p.ticker: p.current_price for p in holdings}

        # partial은 이번 점을 더하기 '전' 상태를 봐야 한다 — 엔진을 켠 뒤 첫 사이클에서만
        # True가 되어, 궤적에 과거가 없다는 사실을 이번 한 번만 프롬프트에 알린다.
        partial = engine.exit_trace.partial
        engine.exit_trace.append(now, portfolio_return, per_ticker, prices)
        trace = engine.exit_trace.points()

        # 오늘의 결정(메모리)을 넘겨 판단 근거(신호)·시나리오가 프롬프트에 실리게 한다
        # (PRD 5.5-C '매도 판단'). 재시작으로 결정이 비어 있으면 그 칸은 빈 채로 간다.
        workflow = getattr(runtime, "workflow", None)
        recommendations = {
            rec.ticker: rec
            for rec in getattr(getattr(workflow, "strategy", None), "_recommendations", None) or []
        }
        # 실시간 콜백이 쌓아 둔 당일 고점 — 궤적(15분 간격)에는 없는 봉우리가 여기 남는다
        peaks = {
            p.ticker: r.peak
            for p in holdings
            for r in [engine.exit_drawdown.retracement(p.ticker, per_ticker[p.ticker])]
            if r is not None
        }
        holdings_view = []
        for p in holdings:
            rec = recommendations.get(p.ticker)
            holdings_view.append(
                HoldingView(
                    ticker=p.ticker,
                    name=p.name or "",
                    quantity=p.quantity,
                    avg_price=p.avg_price,
                    current_price=p.current_price,
                    net_return=per_ticker[p.ticker],
                    # 이월 포지션처럼 그날 결정이 아닌 종목은 아침 근거가 없다 — 빈 문자열로 둔다.
                    outlook=rec.outlook if rec is not None else "",
                    reason=rec.reason if rec is not None else "",
                    # 2호는 목표 매도가가 없다 — 0이면 프롬프트에 '없음'으로 실린다
                    target_sell_price=float(rec.target_sell_price or 0) if rec is not None else 0.0,
                    peak_return=peaks.get(p.ticker),
                )
            )

        minutes_to_close = max(
            0,
            int((datetime.combine(now.date(), FORCE_CLOSE_TIME) - now).total_seconds() // 60),
        )

        # 하루 호출 상한은 '시도'를 센다 — 응답이 실패해도 LLM 호출 자체는 이미 나갔다.
        engine.note_ai_exit_call()
        # 체결내역 조회는 REST라 LLM 호출과 마찬가지로 루프를 막지 않게 스레드로 넘긴다
        user_sells = await asyncio.get_running_loop().run_in_executor(
            None, lambda: collect_user_sells(runtime, holdings, now)
        )
        index_return = await asyncio.get_running_loop().run_in_executor(
            None, lambda: collect_index_return(runtime, now)
        )
        # LLM 호출은 별도 스레드로 넘긴다 — ExitAdvisor.decide는 최대 120초까지 걸릴 수
        # 있어(timeout_seconds), 이벤트 루프에서 그대로 기다리면 그동안 WebSocket PING
        # 응답이 끊기고 실시간 손절 콜백도 멈춘다.
        decision = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: runtime.exit_advisor.decide(
                holdings_view,
                trace,
                minutes_to_close,
                partial,
                user_sells=user_sells,
                index_return=index_return,
            ),
        )
    except Exception:
        logger.exception("AI 매도 판단 사이클 처리 중 오류 — 이번 주기는 매도하지 않습니다.")
        return

    _record_ai_exit_decisions(runtime, now, holdings_view, decision)

    if decision is None:
        reason_text = AI_EXIT_FAILURE_TEXT
        logger.info("AI 매도 판단: 보유 유지 (%s)", reason_text)
        engine.note_ai_exit_result(now, sell=False, reason=reason_text, ok=False)
        return

    held = [position.ticker for position in holdings]
    targets = decision.sell_tickers(held)
    if not targets:
        # 판정은 받았으나 전부 보유 — 종목별 사유를 남겨 다음 주기에 되짚을 수 있게 한다.
        # 보유하지 않은 종목코드(응답의 환각 포함)는 걸러낸다 — UI 기록에 실릴 이유가 없다.
        held_set = set(held)
        reason_text = " / ".join(
            f"{d.ticker}: {d.reason}" for d in decision.decisions if d.ticker in held_set
        )
        logger.info("AI 매도 판단: 보유 유지 (%s)", reason_text)
        engine.note_ai_exit_result(now, sell=False, reason=reason_text)
        return

    # 매도 주문은 루프 스레드에서 그대로 실행해 실시간 손절 감시와 직렬화한다. LLM 응답을
    # 기다리는 동안(최대 120초) 손절이 먼저 정리했을 수 있어, 판단 시점 스냅샷을 그대로
    # 팔지 않고 매도 직전에 보유 종목을 다시 읽는다 (force_close_all_positions와 같은 이유).
    fresh_holdings = engine.exit_candidates(force=True)
    fresh_targets = [p for p in fresh_holdings if p.ticker in set(targets)]
    # UI 기록(note_ai_exit_result)은 "AI가 무엇을 판단했는가"를 보여주는 자리라 이 시점의
    # 판정 전체(targets)를 그대로 남긴다 — 그 사이 손절이 먼저 정리했더라도 AI가 그 종목을
    # 팔기로 봤다는 사실 자체는 남는다.
    note = decision.note(held)
    engine.note_ai_exit_result(now, sell=True, reason=note, sold_tickers=targets)
    if not fresh_targets:
        logger.info("AI 매도 판단: 매도로 판단했지만 그 사이 그 종목이 이미 정리되었습니다.")
        return

    # 알림 메일·청산 로그는 반대로 "무엇이 실제로 팔렸는가"라 fresh_targets로 다시 거른다.
    # 응답을 기다리는 동안 손절이 먼저 정리한 종목의 사유가 섞여 들어가면, 실제로는 안 팔린
    # 종목을 "왜 팔았는지"로 오독하게 된다 (2026-09-19 리뷰).
    executed_note = decision.note(p.ticker for p in fresh_targets)
    logger.warning("AI 매도 판단: %d종목 매도 (%s)", len(fresh_targets), executed_note)
    # 근거를 함께 넘겨 청산 로그와 알림 메일에 남긴다 — 사유 코드(ai_judgment)만으로는
    # 왜 팔았는지 나중에 되짚을 수 없다.
    engine._execute_portfolio_exit(fresh_targets, ExitReason.AI_JUDGMENT, note=executed_note)


async def maybe_run_ai_exit_cycle(
    runtime: Runtime,
    now: Optional[datetime] = None,
    last_called_at: Optional[datetime] = None,
) -> Optional[datetime]:
    """게이트(`ai_exit_due`)를 통과할 때만 한 사이클(`run_ai_exit_cycle`)을 실행한다.

    다음 게이트 판정에 쓸 `last_called_at`을 돌려준다 — 건너뛰면 받은 값을 그대로 돌려준다.
    """
    now = now or datetime.now()
    if not ai_exit_due(runtime, now, last_called_at):
        return last_called_at
    await run_ai_exit_cycle(runtime, now)
    return now


async def watch_ai_exit(runtime: Runtime, poll_seconds: float = AI_EXIT_POLL_SECONDS) -> None:
    """설정된 주기마다 보유 종목을 지금 정리할지 AI에 묻는다 (PRD 5.5-B 'AI 매도 판단').

    익절 자동 청산이 빠진 자리를 대신한다. 손절은 실시간 시세 콜백에서 그대로 도므로,
    이 워처가 멈춰도 하방은 지켜진다.

    LLM 호출은 별도 스레드로 넘긴다 — 20초 넘게 걸려 이벤트 루프를 막으면 WebSocket
    PING에 응답하지 못해 시세가 끊긴다. 매도 주문만 루프 스레드에서 실행해 실시간
    손절 감시와 직렬화한다.
    """
    last_called_at: Optional[datetime] = None
    while True:
        await asyncio.sleep(poll_seconds)
        last_called_at = await maybe_run_ai_exit_cycle(runtime, datetime.now(), last_called_at)


def adopt_carried_over_positions(runtime: Runtime) -> List[str]:
    """시작 시점에 남아 있는 보유 종목을 오늘의 매도 대상으로 편입한다.

    전일 청산에 실패했거나 앱이 꺼진 사이 넘어온 포지션은 아무도 구독하지 않는다.
    실시간 시세 구독은 당일 매수분(DailyWorkflow.execute_buys)에서만 걸리므로, 이월 포지션만
    남은 날에는 시세가 한 건도 오지 않아 손절 판정(RiskManager.check_position_exits)이
    아예 돌지 않는다 — 판정은 틱을 받은 순간에만 도는 콜백이다.
    여기서 구독을 걸어야 감시가 시작된다. 15:15 강제청산은 잔고 전체를 읽으므로 자동 포함된다.

    손절은 종목별로 판정하므로(PRD 5.5-B, 확정 2026-09-18), 이월 포지션도 각자의 평단가
    기준 순손익률로 독립적으로 판정된다. 평단가는 최초 매수 시점 기준이라 이월분의 손실이
    그대로 반영된다.
    """
    held = runtime.engine.open_tickers
    if not held:
        return []

    runtime.ws_client.subscribe(held)
    logger.warning("이월 포지션을 매도 감시 대상으로 편입했습니다: %s", held)
    return held


def request_stop(runtime: Runtime) -> None:
    """구동 루프가 스스로 빠져나오도록 표시한다 (다른 스레드에서 호출해도 안전)."""
    runtime.scheduler.stop()
    runtime.engine.stop()
    runtime.ws_client.request_stop()


async def run(runtime: Runtime) -> None:
    """엔진을 시작하고 스케줄러·실시간 시세를 함께 구동한다."""
    runtime.engine.start()
    # 구독은 연결 전에 걸어도 된다 — WebSocketClient.connect가 접속 직후 복구해 보낸다
    adopt_carried_over_positions(runtime)
    if runtime.on_ready is not None:
        try:
            runtime.on_ready()
        except Exception:
            logger.exception("엔진 준비 알림 콜백 실패 — 무시하고 계속합니다.")
    runner_task = asyncio.create_task(runtime.runner.run())
    watchdog = asyncio.create_task(watch_quote_stall(runtime))
    closeout_watch = asyncio.create_task(watch_closeout_report(runtime))
    buy_result_watch = asyncio.create_task(watch_buy_result(runtime))
    cash_watch = asyncio.create_task(watch_cash_refresh(runtime))
    ai_exit_watch = asyncio.create_task(watch_ai_exit(runtime))
    try:
        await asyncio.gather(runtime.ws_client.connect(), runtime.scheduler.run())
    except asyncio.CancelledError:
        pass
    finally:
        runner_task.cancel()
        watchdog.cancel()
        closeout_watch.cancel()
        buy_result_watch.cancel()
        cash_watch.cancel()
        ai_exit_watch.cancel()
        request_stop(runtime)
        await runtime.ws_client.disconnect()
        logger.info("매매 런타임이 종료되었습니다.")
