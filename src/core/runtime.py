"""매매 런타임 조립 및 구동.

엔진 단독 실행은 하지 않고 UI에서만 제어하므로, UI 스레드가 이 모듈을 통해
전체 구성요소를 만들고 돌린다.
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Callable, List, Optional, Tuple

from config.settings import Settings
from src.api.account import AccountClient
from src.api.auth import AuthClient
from src.api.client import KiwoomClient
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
from src.core.events import ExitReason
from src.data.collector import DataCollector, LargeCapUniverse
from src.core.disclosure_watch import DisclosureWatch
from src.data.disclosure import DisclosureClient
from src.llm.exit_advisor import ExitAdvisor, HoldingView
from src.llm.recommender import LLMRecommender
from src.llm.reviewer import LLMReviewer
from src.llm.tuner import PromptTuner
from src.logger.trade_store import TradeStore
from src.notification.alert import AlertNotifier
from src.notification.email import EmailNotifier
from src.risk.manager import RiskManager
from src.scheduler.scheduler import TimeScheduler
from src.strategy.llm_momentum import LLMMomentumStrategy

logger = logging.getLogger(__name__)

# 1호 전략 하루 흐름 트리거 시각 (PRD 5.5-B, 5.11)
# **추천(`settings.recommend_time`)과 매수(`settings.buy_time`)만 설정값이다** — 둘 다
# `.env`로만 바꾸고 UI는 값을 보여주기만 한다 (확정 2026-08-20). 두 시각의 순서와 최소
# 간격은 `Settings.validate`가 강제한다. 나머지는 장 운영 시간에 맞춰 고정한다.
# 매수를 개장 후로 옮긴 이력: 2026-08-14에 09:00 → 09:10, 2026-08-20에 09:10 → 09:08
# (기본값은 `config/settings.py`의 `DEFAULT_BUY_TIME_HHMM`, 근거는 PRD 10절
# "개장 후 추천으로 이동"·"매수 타이밍 조정").
DAILY_RESET_TIME = dt_time(8, 40)   # 일일 손실 한도·매매중지 초기화 (연속 실행 대비)
# 눌림을 기다리는 목표가가 닿을 시간을 30분 더 줬다 (2026-08-20: 09:40 → 10:10).
# 매수 시각이 설정값이 된 뒤로는 '매수 +N분'이 아니라 고정 시각이다 — 매수를 늦출수록
# 미체결을 기다리는 시간이 그만큼 짧아진다.
CANCEL_UNFILLED_TIME = dt_time(10, 10)  # 미체결 매수 취소 → 매수 결과 메일
# 청산은 장마감 동시호가(15:20~15:30) '이전'에 내야 한다 — 동시호가에 들어간 시장가 주문은
# 15:30 종가에야 체결되어, 그 사이에 나간 리포트가 그 매도를 미정산으로 싣는다 (2026-08-12).
# 리포트는 반대로 마감 뒤로 5분 물려, 마감 동시호가 체결분까지 체결내역에 잡힌 뒤 집계한다.
FORCE_CLOSE_TIME = dt_time(15, 15)  # 당일 매도 원칙에 따른 미청산 포지션 정리
REPORT_TIME = dt_time(15, 35)       # 일일/월간 성과 리포트 이메일 (정규장 마감 15:30 이후)

# 시세 끊김 감시 — 익절/손절이 실시간 시세에만 의존하므로(키움 REST 스탑오더 미지원),
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
# 기록 파일이 지워져 판정이 API를 부르지 않게 되므로, 하루 호출은 09:08~체결확인 구간에 그친다.
BUY_RESULT_CHECK_INTERVAL_SECONDS = 60

# 예수금 캐시 갱신 — 입금 등 장중 잔고 변동을 "총 매수가능 금액" UI 표시에 반영한다.
# 시세 틱과 달리 예수금은 자연스러운 갱신 계기가 없어 별도 주기로 돈다.
CASH_REFRESH_INTERVAL_SECONDS = 60

# AI 매도 판단 — 매수 체결 뒤 이 시간이 지나야 첫 판단을 한다. 체결 직후에는 근거가 없다.
AI_EXIT_START_DELAY_MINUTES = 15
# 15:15 강제청산 직전에는 부르지 않는다 — 어차피 곧 팔린다.
AI_EXIT_END_TIME = dt_time(15, 0)
AI_EXIT_POLL_SECONDS = 30.0
# 장중 공시 조회 주기 (PRD 5.5-B '장중 공시'). DART는 시각을 주지 않아 목록을 다시 받아
# 비교하는 수밖에 없다 — 악재 공시가 뜬 뒤 판단이 돌기까지의 최대 지연이 이 값이다.
DISCLOSURE_POLL_SECONDS = 300.0


def _submit(runtime: "Runtime", action: str) -> Callable[[], None]:
    """스케줄 잡을 실행 통로 접수로 바꾼다 — 버튼과 같은 큐를 탄다."""

    def job() -> None:
        runtime.runner.submit(action)

    return job


def _trading_days_only(job, name: str):
    """거래일이 아닌 날에는 작업을 건너뛰도록 감싼다.

    앱을 며칠 연속 켜두면 스케줄러가 요일과 무관하게 매일 발동한다. 주말에 그대로 두면
    쓸모없는 LLM 호출(비용)과 추천·리포트 메일이 나가고, 매수는 거래소에서 거부된다.
    공휴일은 걸러내지 못한다 — 별도 휴장일 캘린더가 필요하다.
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


@dataclass
class Runtime:
    settings: Settings
    engine: TradingEngine
    scheduler: TimeScheduler
    ws_client: WebSocketClient
    workflow: DailyWorkflow
    # 보유 종목을 지금 전량 정리할지 판단하는 LLM 모듈 (PRD 5.5-B 'AI 매도 판단').
    exit_advisor: ExitAdvisor
    # 장중에 새로 뜬 공시를 가려내 위 판단에 넘긴다 (PRD 5.5-B '장중 공시').
    disclosure_watch: Optional[DisclosureWatch] = None
    # 스케줄 잡과 UI 버튼이 공유하는 실행 통로. build_runtime이 Runtime을 만든 뒤에
    # 채운다 — ActionRunner가 runtime을 참조해야 해서 순서를 뒤집을 수 없다.
    runner: Optional[ActionRunner] = None


def build_runtime(settings: Settings) -> Runtime:
    """설정으로부터 매매 런타임 전체를 조립한다 (아직 시작하지는 않는다)."""
    auth = AuthClient(settings)
    market_data = MarketDataClient(settings, auth)
    order_client = OrderClient(settings, auth)
    account = AccountClient(settings, auth)
    ws_client = WebSocketClient(settings, auth)

    strategy = LLMMomentumStrategy(
        investable_ratio=settings.investable_ratio,
        target_stock_count=settings.target_stock_count,
    )
    risk_manager = RiskManager(
        max_position_ratio=settings.max_position_ratio,
        max_daily_loss_ratio=settings.max_daily_loss_ratio,
        take_profit_ratio=settings.take_profit_ratio,
        stop_loss_ratio=settings.stop_loss_ratio,
        stop_loss_enabled=settings.stop_loss_enabled,
        max_total_exposure_ratio=settings.max_total_exposure_ratio,
        commission_rate=settings.commission_ratio,
        tax_rate=settings.tax_ratio,
        slippage_rate=settings.slippage_ratio,
    )
    trade_store = TradeStore()
    email = EmailNotifier(settings)
    # 아침 수집과 장중 감시가 같은 클라이언트를 쓴다 — 상태가 없고 API 키만 들고 있다
    disclosures = DisclosureClient(settings.dart_api_key)

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

    workflow = DailyWorkflow(
        collector=DataCollector(
            market_data,
            LargeCapUniverse(KiwoomClient(settings, auth)),
            disclosures,
            gap_down_tolerance_ratio=settings.gap_down_tolerance_ratio,
            notify=engine.notify,
        ),
        recommender=LLMRecommender(settings),
        strategy=strategy,
        engine=engine,
        account=account,
        trade_store=trade_store,
        email=email,
        reviewer=LLMReviewer(settings),
        tuner=PromptTuner(settings),
        buy_price_tolerance_ratio=settings.buy_price_tolerance_ratio,
        gap_down_tolerance_ratio=settings.gap_down_tolerance_ratio,
        ws_client=ws_client,
    )
    exit_advisor = ExitAdvisor(settings)

    # 매수/강제청산은 실시간 익절·손절 감시와 직렬화되도록 루프 스레드에서 그대로 실행하고,
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
        disclosure_watch=DisclosureWatch(disclosures),
    )
    runtime.runner = ActionRunner(runtime)

    for trigger_time, action in (
        (DAILY_RESET_TIME, "daily_reset"),
        (settings.recommend_time, "recommend"),
        (settings.buy_time, "buy"),
        (CANCEL_UNFILLED_TIME, "cancel_unfilled"),
        (FORCE_CLOSE_TIME, "close_out"),
        (REPORT_TIME, "daily_report"),
        # 리포트 다음에 등록한다 — 같은 시각의 두 잡은 등록 순서대로 큐에 들어간다
        (REPORT_TIME, "review_recommendations"),
        # 검증 다음에 등록한다 — 그날 검증 결과가 이 단계의 판단 재료다
        (REPORT_TIME, "tune_prompt"),
    ):
        scheduler.add_job(
            trigger_time,
            _trading_days_only(_submit(runtime, action), action),
            name=action,
        )

    return runtime


def is_trading_day(now: Optional[datetime] = None) -> bool:
    """거래일(월~금)인지. 공휴일은 판별하지 않는다 — 휴장일 캘린더가 없다."""
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

    보유 종목이 있는데 시세가 오지 않으면 익절/손절 판정이 멈춘 상태다.
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
                    "%s 익절/손절 감시가 멈춘 상태입니다. 보유: %s", detail, held
                )
                runtime.engine.notify(
                    f"[경고] {detail} 익절/손절 감시가 멈췄습니다. "
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

    발송은 직접 부르지 않고 실행 통로에 접수한다. 09:08 매수가 아직 도는 중이면 그 뒤에서
    기다리고, 실시간 익절·손절 감시와도 직렬화된다. 이미 큐에 있으면 러너가 걸러낸다.
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
            # 막으면 그동안 WebSocket PING 응답도, 실시간 익절/손절 콜백도 멈춘다.
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


def ai_exit_window_open(settings: Settings, now: Optional[datetime] = None) -> bool:
    """지금이 AI 매도 판단 호출 창 안인지 (매수 시각 +`AI_EXIT_START_DELAY_MINUTES`분 ~ `AI_EXIT_END_TIME`).

    창 시작이 고정 시각이 아닌 이유는 매수 시각(`settings.buy_time`)이 설정값이기 때문이다.
    """
    now = now or datetime.now()
    start = datetime.combine(now.date(), settings.buy_time) + timedelta(
        minutes=AI_EXIT_START_DELAY_MINUTES
    )
    end = datetime.combine(now.date(), AI_EXIT_END_TIME)
    return start <= now <= end


def ai_exit_call_limit(settings: Settings) -> int:
    """하루 동안 허용할 AI 매도 판단 최대 호출 횟수.

    호출 창(매수 시각 +`AI_EXIT_START_DELAY_MINUTES`분 ~ `AI_EXIT_END_TIME`) 길이를 호출
    주기(`settings.ai_exit_interval_minutes`)로 나눈 슬롯 수다 (창이 열리는 순간의 호출을
    포함하도록 +1). 엔진을 하루에 여러 번 재시작해도 누적 호출이 이 값을 넘지 않도록
    막는다 — `TradingEngine._ai_exit_calls`는 재시작해도 유지되고 08:40 reset_for_new_day
    에서만 비워지므로, 이 상한이 없으면 재시작할 때마다 사실상 무제한으로 호출된다.
    """
    start_minutes = (
        settings.buy_time.hour * 60 + settings.buy_time.minute + AI_EXIT_START_DELAY_MINUTES
    )
    end_minutes = AI_EXIT_END_TIME.hour * 60 + AI_EXIT_END_TIME.minute
    window_minutes = end_minutes - start_minutes
    if window_minutes <= 0:
        return 0
    return window_minutes // settings.ai_exit_interval_minutes + 1


def ai_exit_due(
    runtime: Runtime,
    now: Optional[datetime] = None,
    last_called_at: Optional[datetime] = None,
) -> bool:
    """이번 사이클에 AI 매도 판단을 부를 조건인지 (PRD 5.5-B 'AI 매도 판단').

    아래를 전부 만족해야 True다. 하나라도 아니면 그 사이클은 건너뛴다.
    거래일 → `engine.ai_exit_enabled` → 호출 창 안 → 마지막 호출로부터 주기 경과 →
    보유 종목 있음 → 하루 호출 상한 안.

    **즉시 호출 사유가 서 있으면** 주기와 하루 상한 두 가지를 건너뛴다. 사유는 둘이다 —
    장중에 새로 뜬 악재 공시(`DisclosureWatch`, PRD 5.5-B '장중 공시')와 당일 고점 대비
    이익 반납(`DrawdownTracker`, PRD 5.5-B '이익 반납 감시'). 주기만 건너뛰면 그날의
    마지막 정규 사이클이 상한에 걸려 대신 빠지고, 그쪽이 장 마감에 더 가까워 손해가 크다.
    두 트리거 모두 각자의 `MAX_URGENT_TRIGGERS_PER_DAY`(공시 3회 / 반납 5회)로 묶여 있어
    비용은 그만큼만 는다.
    거래일·설정·호출 창·보유 종목은 예외 없이 그대로 본다.
    """
    now = now or datetime.now()
    if not is_trading_day(now):
        return False

    engine = runtime.engine
    if not engine.ai_exit_enabled:
        return False

    if not ai_exit_window_open(runtime.settings, now):
        return False

    urgent = (runtime.disclosure_watch is not None and runtime.disclosure_watch.urgent_pending) or (
        engine.exit_drawdown.urgent_pending
    )

    if last_called_at is not None and not urgent:
        elapsed_minutes = (now - last_called_at).total_seconds() / 60
        if elapsed_minutes < runtime.settings.ai_exit_interval_minutes:
            return False

    if not engine.open_tickers:
        return False

    if not urgent and engine.ai_exit_calls >= ai_exit_call_limit(runtime.settings):
        return False

    return True


async def run_ai_exit_cycle(runtime: Runtime, now: Optional[datetime] = None) -> None:
    """AI 매도 판단 한 사이클을 실행한다 — 궤적에 점을 남기고 필요하면 전량 매도한다.

    호출 전에 `ai_exit_due`로 이번 사이클을 돌 조건인지 먼저 확인해야 한다. 이 함수
    자체는 보유 종목 유무 등을 다시 확인하지 않는다 (게이트와 실행을 분리해 각각 따로
    테스트하기 위함).
    """
    now = now or datetime.now()
    engine = runtime.engine
    # 이번 사이클이 앞당겨진 것이든 아니든 여기서 트리거를 비운다 — 남겨 두면 다음
    # 주기까지 계속 주기를 건너뛰게 된다.
    if runtime.disclosure_watch is not None:
        runtime.disclosure_watch.take_urgent()
    engine.exit_drawdown.take_urgent()

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

        recommendations = (
            {r.ticker: r for r in engine.trade_store.recommendations_for(now.date())}
            if engine.trade_store is not None
            else {}
        )
        watch = runtime.disclosure_watch
        # 실시간 콜백이 쌓아 둔 당일 고점 — 궤적(15분 간격)에는 없는 봉우리가 여기 남는다
        peaks = {
            p.ticker: r.peak
            for p in holdings
            for r in [engine.exit_drawdown.retracement(p.ticker, per_ticker[p.ticker])]
            if r is not None
        }
        portfolio_peak_view = engine.exit_drawdown.portfolio_retracement(portfolio_return)
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
                    # 이월 포지션처럼 그날 추천이 아닌 종목은 아침 근거가 없다 — 빈 문자열로 둔다.
                    outlook=rec.outlook if rec is not None else "",
                    reason=rec.reason if rec is not None else "",
                    # 공시 감시가 아직 한 번도 돌지 않았으면 빈 목록이다 — 없는 것과 같이 다룬다
                    headlines=watch.headlines_for(p.ticker) if watch is not None else [],
                    new_headlines=(
                        watch.new_headlines_for(p.ticker) if watch is not None else []
                    ),
                    # 이월 포지션은 그날 추천이 아니라 목표 매도가가 없다 — 0으로 둔다
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
        # LLM 호출은 별도 스레드로 넘긴다 — ExitAdvisor.decide는 최대 120초까지 걸릴 수
        # 있어(timeout_seconds), 이벤트 루프에서 그대로 기다리면 그동안 WebSocket PING
        # 응답이 끊기고 실시간 손절 콜백도 멈춘다.
        decision = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: runtime.exit_advisor.decide(
                holdings_view,
                trace,
                portfolio_return,
                engine.risk_manager.stop_loss_ratio,
                engine.risk_manager.take_profit_ratio,
                minutes_to_close,
                partial,
                portfolio_peak_view.peak if portfolio_peak_view is not None else None,
            ),
        )
    except Exception:
        logger.exception("AI 매도 판단 사이클 처리 중 오류 — 이번 주기는 매도하지 않습니다.")
        return

    if decision is None or not decision.sell:
        # 실패 사유를 "판단 실패"로 두면 표에서 판정 문구와 그대로 겹쳐 읽을 것이 없어진다
        reason_text = (
            decision.reason
            if decision is not None
            else "LLM 호출 실패·타임아웃·형식 오류 (이번 주기는 매도하지 않습니다)"
        )
        logger.info("AI 매도 판단: 보유 유지 (%s)", reason_text)
        # 로그는 흘러가므로 마지막 판단을 엔진에도 남긴다 — UI 보유 종목 표가 이 값을 읽는다.
        engine.note_ai_exit_result(now, sell=False, reason=reason_text, ok=decision is not None)
        return

    # 매도 주문은 루프 스레드에서 그대로 실행해 실시간 손절 감시와 직렬화한다. LLM 응답을
    # 기다리는 동안(최대 120초) 손절이 먼저 정리했을 수 있어, 판단 시점 스냅샷을 그대로
    # 팔지 않고 매도 직전에 보유 종목을 다시 읽는다 (force_close_all_positions와 같은 이유).
    fresh_holdings = engine.exit_candidates(force=True)
    engine.note_ai_exit_result(now, sell=True, reason=decision.reason)
    if not fresh_holdings:
        logger.info("AI 매도 판단: 전량 매도로 판단했지만 그 사이 보유 종목이 이미 정리되었습니다.")
        return

    logger.warning("AI 매도 판단: 전량 매도 (%s)", decision.reason)
    # 근거를 함께 넘겨 청산 로그와 알림 메일에 남긴다 — 사유 코드(ai_judgment)만으로는
    # 왜 팔았는지 나중에 되짚을 수 없다.
    engine._execute_portfolio_exit(fresh_holdings, ExitReason.AI_JUDGMENT, note=decision.reason)


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


async def maybe_poll_disclosures(
    runtime: Runtime, now: Optional[datetime] = None
) -> List[Tuple[str, str]]:
    """조건이 맞으면 장중 공시를 한 번 조회한다 (PRD 5.5-B '장중 공시').

    보유 종목이 있는 거래일 장중에만 돈다 — 팔 것이 없으면 볼 이유가 없고, 장이 닫힌
    시간에는 새 공시가 판단으로 이어질 곳이 없다. 새로 뜬 악재 공시를 돌려준다.

    조회는 blocking HTTP라 별도 스레드로 넘긴다 — 이벤트 루프에서 그대로 기다리면 그동안
    WebSocket PING 응답이 끊기고 실시간 손절 콜백도 멈춘다 (AI 매도 판단과 같은 이유).
    """
    now = now or datetime.now()
    watch = runtime.disclosure_watch
    if watch is None:
        return []
    if not is_market_hours(now):
        return []

    tickers = runtime.engine.open_tickers
    if not tickers:
        return []

    try:
        triggered = await asyncio.get_running_loop().run_in_executor(
            None, lambda: watch.poll(tickers, now)
        )
    except Exception:
        # 공시를 못 봤다고 매매를 멈추지는 않는다 — 아침 수집(_apply_disclosures)과 같은 규약이다
        logger.exception("장중 공시 조회에 실패했습니다. 이번 주기는 건너뜁니다.")
        return []

    for ticker, title in triggered:
        logger.warning("장중 악재 공시: %s | %s — AI 매도 판단을 앞당깁니다.", ticker, title)
        runtime.engine.notify(
            f"[경고] 보유 종목 {ticker}에 장중 공시가 떴습니다: {title}. "
            "AI 매도 판단을 호출 주기와 무관하게 곧바로 한 번 돌립니다."
        )
    return triggered


async def watch_disclosures(
    runtime: Runtime, poll_seconds: float = DISCLOSURE_POLL_SECONDS
) -> None:
    """장중 공시를 주기적으로 받아 캐시에 쌓는다 (PRD 5.5-B '장중 공시').

    AI 매도 판단 사이클 안에서 조회하지 않고 따로 떼어 둔 이유는 두 가지다. 판단 주기가
    60분이면 그만큼 반영이 늦고, 판단 경로에 HTTP 호출을 하나 더 넣게 된다. 여기서 미리
    받아 두면 판단은 캐시만 읽는다.
    """
    while True:
        await asyncio.sleep(poll_seconds)
        await maybe_poll_disclosures(runtime, datetime.now())


def adopt_carried_over_positions(runtime: Runtime) -> List[str]:
    """시작 시점에 남아 있는 보유 종목을 오늘의 매도 대상으로 편입한다.

    전일 청산에 실패했거나 앱이 꺼진 사이 넘어온 포지션은 아무도 구독하지 않는다.
    실시간 시세 구독은 당일 매수분(DailyWorkflow.execute_buys)에서만 걸리므로, 이월 포지션만
    남은 날에는 시세가 한 건도 오지 않아 익절/손절 판정(RiskManager.check_portfolio_exit)이
    아예 돌지 않는다 — 판정은 틱을 받은 순간에만 도는 콜백이다.
    여기서 구독을 걸어야 감시가 시작된다. 15:15 강제청산은 잔고 전체를 읽으므로 자동 포함된다.

    판정은 보유 종목 합산 기준이므로, 이월 포지션도 당일 매수분과 한 덩어리로 묶여 함께
    팔린다. 평단가는 최초 매수 시점 기준이라 이월분의 손실이 그대로 합산에 들어온다.
    """
    held = runtime.engine.open_tickers
    if not held:
        return []

    runtime.ws_client.subscribe(held)
    logger.warning("이월 포지션을 매도 감시 대상으로 편입했습니다: %s", held)
    runtime.engine.notify(
        f"[알림] 전일 이월 보유 종목 {len(held)}개를 오늘 매도 대상으로 편입했습니다: {held}. "
        "익절/손절 감시가 시작되며, 남으면 15:15에 강제청산됩니다."
    )
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
    runner_task = asyncio.create_task(runtime.runner.run())
    watchdog = asyncio.create_task(watch_quote_stall(runtime))
    closeout_watch = asyncio.create_task(watch_closeout_report(runtime))
    buy_result_watch = asyncio.create_task(watch_buy_result(runtime))
    cash_watch = asyncio.create_task(watch_cash_refresh(runtime))
    ai_exit_watch = asyncio.create_task(watch_ai_exit(runtime))
    disclosure_watch = asyncio.create_task(watch_disclosures(runtime))
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
        disclosure_watch.cancel()
        request_stop(runtime)
        await runtime.ws_client.disconnect()
        logger.info("매매 런타임이 종료되었습니다.")
