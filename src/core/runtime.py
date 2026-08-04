"""매매 런타임 조립 및 구동.

엔진 단독 실행은 하지 않고 UI에서만 제어하므로, UI 스레드가 이 모듈을 통해
전체 구성요소를 만들고 돌린다.
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Awaitable, Callable, Dict, List, Optional

from config.settings import Settings
from src.api.account import AccountClient
from src.api.auth import AuthClient
from src.api.client import KiwoomClient
from src.api.market_data import MarketDataClient
from src.api.order import OrderClient
from src.api.websocket_client import WebSocketClient
from src.core.daily_workflow import DailyWorkflow
from src.core.engine import TradingEngine
from src.data.collector import DataCollector, LargeCapUniverse, NewsClient
from src.llm.recommender import LLMRecommender
from src.logger.trade_store import TradeStore
from src.notification.alert import AlertNotifier
from src.notification.email import EmailNotifier
from src.risk.manager import RiskManager
from src.scheduler.scheduler import TimeScheduler
from src.strategy.llm_momentum import LLMMomentumStrategy

logger = logging.getLogger(__name__)

# 1호 전략 하루 흐름 트리거 시각 (PRD 5.5-B, 5.11)
DAILY_RESET_TIME = dt_time(8, 40)   # 일일 손실 한도·매매중지 초기화 (연속 실행 대비)
BUY_TIME = dt_time(9, 0)            # 데이터 수집 → LLM 추천 → 자금 산정 → 매수 실행 (일괄)
FORCE_CLOSE_TIME = dt_time(15, 20)  # 당일 매도 원칙에 따른 미청산 포지션 정리
REPORT_TIME = dt_time(15, 30)       # 일일/월간 성과 리포트 이메일

# 시세 끊김 감시 — 익절/손절이 실시간 시세에만 의존하므로(키움 REST 스탑오더 미지원),
# 보유 종목이 있는데 시세가 끊기면 손절이 조용히 멈춘다. 그 공백을 알린다.
MARKET_OPEN_TIME = dt_time(9, 0)
MARKET_CLOSE_TIME = dt_time(15, 30)
QUOTE_CHECK_INTERVAL_SECONDS = 60
QUOTE_STALE_AFTER_SECONDS = 300

# 전량 매도 완료 감시 — 보유 종목이 다 팔렸으면 15:30을 기다리지 않고 결과 리포트를 보낸다.
CLOSEOUT_CHECK_INTERVAL_SECONDS = 30
# 매도 체결이 잔고·체결내역에 반영될 시간. 주문 접수 직후에 집계하면 방금 팔린 종목이
# '보유중'으로 실린다 (DailyWorkflow._fill_buy_prices의 같은 시차 참고).
CLOSEOUT_SETTLE_SECONDS = 60

# 예수금 캐시 갱신 — 입금 등 장중 잔고 변동을 "총 매수가능 금액" UI 표시에 반영한다.
# 시세 틱과 달리 예수금은 자연스러운 갱신 계기가 없어 별도 주기로 돈다.
CASH_REFRESH_INTERVAL_SECONDS = 60


def _off_loop(func: Callable[[], None]):
    """오래 걸리는 동기 작업을 별도 스레드로 넘기는 스케줄러 작업으로 감싼다.

    데이터 수집(99종목)과 LLM 호출은 합쳐 30초 이상 걸린다. 이벤트 루프에서 그대로
    실행하면 그 시간 동안 WebSocket PING에 응답하지 못해 서버가 연결을 끊는다.
    주문·포지션을 건드리지 않는 작업만 이렇게 넘긴다.
    """

    async def runner() -> None:
        await asyncio.get_running_loop().run_in_executor(None, func)

    return runner


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
        max_total_exposure_ratio=settings.max_total_exposure_ratio,
        commission_rate=settings.commission_ratio,
        tax_rate=settings.tax_ratio,
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
    )
    ws_client.on_data(engine.on_market_data)

    workflow = DailyWorkflow(
        collector=DataCollector(
            market_data, LargeCapUniverse(KiwoomClient(settings, auth)), NewsClient()
        ),
        recommender=LLMRecommender(settings),
        strategy=strategy,
        engine=engine,
        account=account,
        trade_store=trade_store,
        email=email,
        ws_client=ws_client,
    )

    # 매수/강제청산은 실시간 익절·손절 감시와 직렬화되도록 루프 스레드에서 그대로 실행하고,
    # 오래 걸리는 수집·LLM·리포트는 루프를 막지 않도록 별도 스레드로 넘긴다.
    # 모든 작업은 거래일에만 돌도록 감싼다 (_trading_days_only 참고).
    scheduler = TimeScheduler()
    runtime = Runtime(
        settings=settings,
        engine=engine,
        scheduler=scheduler,
        ws_client=ws_client,
        workflow=workflow,
    )
    for trigger_time, job, name in (
        (DAILY_RESET_TIME, engine.reset_for_new_day, "daily_reset"),
        # 즉시 실행 ①+② 버튼("full")과 동일한 절차 — 09:00에 추천과 매수를 이어서 수행한다
        (BUY_TIME, _run_manual_steps(runtime, "full"), "recommend_and_buy"),
        (
            FORCE_CLOSE_TIME,
            lambda: engine.force_close_all_positions(reason="day_end"),
            "force_close_all_positions",
        ),
        (REPORT_TIME, _off_loop(workflow.send_final_report), "daily_report"),
    ):
        scheduler.add_job(trigger_time, _trading_days_only(job, name), name=name)

    return runtime


# ── 즉시 실행(점검) 액션 ────────────────────────────────────
# 스케줄 시각을 기다리지 않고 UI에서 바로 하루 흐름의 각 단계를 실행하기 위한 목록.
# 실행 주체는 스케줄러와 동일한 workflow/engine 메서드이므로 동작이 갈리지 않는다.
MANUAL_ACTIONS: Dict[str, str] = {
    "recommend": "① LLM 추천 + 메일 발송",
    "buy": "② 매수 실행",
    "sell_all": "③ 전량 매도 (청산)",
    "report": "④ 최종 리포트 메일",
    # 매도 '설정'은 별도 단계가 아니다 — 익절/손절 라인은 엔진 시작 시 적용되어 있고,
    # 매수로 포지션이 생기는 순간 RiskManager.check_exit 감시가 자동으로 붙는다.
    "full": "매수 및 매도설정까지 일괄 수행",
}

# 실제 주문이 나가는 액션 — UI가 실행 전 확인을 받는다
ORDER_ACTIONS = frozenset({"buy", "sell_all", "full"})


@dataclass(frozen=True)
class ManualStep:
    label: str
    run: Callable[[], None]
    # True면 엔진 루프 스레드에서 실행해 실시간 익절·손절 감시와 직렬화한다
    # (같은 종목을 동시에 청산하는 경쟁 상태 방지). False면 별도 스레드 — `_off_loop` 참고.
    touches_orders: bool = False


def manual_steps(runtime: Runtime, action: str) -> List[ManualStep]:
    """액션 이름을 실행 단계 목록으로 바꾼다. '전체'는 하루 흐름의 진입 단계를 순서대로 이어 붙인다."""
    steps: Dict[str, List[ManualStep]] = {
        "recommend": [
            ManualStep(MANUAL_ACTIONS["recommend"], runtime.workflow.recommend_and_notify)
        ],
        "buy": [
            ManualStep(MANUAL_ACTIONS["buy"], runtime.workflow.execute_buys, touches_orders=True)
        ],
        "sell_all": [
            ManualStep(
                MANUAL_ACTIONS["sell_all"],
                lambda: runtime.engine.force_close_all_positions(reason="manual"),
                touches_orders=True,
            )
        ],
        "report": [ManualStep(MANUAL_ACTIONS["report"], runtime.workflow.send_daily_report)],
    }
    # 일괄 실행은 '진입'까지만 — 청산과 리포트는 스케줄에 맡긴다.
    # 청산(③)을 넣으면 매수 직후 곧바로 되팔아 익절/손절 감시 구간이 사라지고 왕복 비용만 남는다.
    # 당일 매도 원칙은 FORCE_CLOSE_TIME(15:20)이 지키고, 리포트는 당일 매매가 끝난 뒤에야
    # 의미가 있는 집계이므로 REPORT_TIME(15:30)에 맡긴다. 지금 당장 필요하면 ③·④ 버튼으로 따로 실행한다.
    steps["full"] = steps["recommend"] + steps["buy"]

    if action not in steps:
        raise ValueError(f"Unknown manual action: {action}")
    return steps[action]


def _run_manual_steps(runtime: Runtime, action: str) -> Callable[[], Awaitable[None]]:
    """스케줄러가 즉시 실행 액션과 동일한 절차를 그대로 따르게 감싼다.

    touches_orders 단계는 루프 스레드에서 직접 실행해 실시간 익절/손절 감시와 직렬화하고,
    그 외(수집·LLM·메일)는 별도 스레드로 넘긴다 — engine_thread.EngineThread._run_action과 같은 규칙.
    """

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for step in manual_steps(runtime, action):
            if step.touches_orders:
                result = step.run()
                if asyncio.iscoroutine(result):
                    await result
            else:
                await loop.run_in_executor(None, step.run)

    return runner


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
    """보유 종목이 전부 매도되면 15:30을 기다리지 않고 결과 리포트를 보낸다.

    청산 건당 한 번만 시도한다 — 발송에 실패하면 표시가 서지 않으므로 15:30 스케줄이
    대신 보낸다 (DailyWorkflow.send_final_report). 매수로 보유가 다시 생기면 엔진이
    청산 시각을 지우므로, 그 보유분을 또 전량 매도하면 새 시각으로 다시 발송한다.
    """
    reported_at: Optional[datetime] = None
    while True:
        await asyncio.sleep(interval_seconds)

        closed_at = runtime.engine.closed_out_at
        if closed_at == reported_at or not closeout_report_due(runtime):
            continue

        reported_at = closed_at
        logger.info(
            "보유 종목 전량 매도 완료 (%s) — 15:30을 기다리지 않고 결과 리포트를 발송합니다.",
            closed_at.strftime("%H:%M:%S"),
        )
        try:
            # SMTP·체결조회가 몇 초 걸리므로 이벤트 루프를 막지 않는다 (_off_loop 참고)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: runtime.workflow.send_final_report(closed_out=True)
            )
        except Exception:
            logger.exception("전량 매도 결과 리포트 발송 실패 — 15:30 리포트에 맡깁니다.")


async def watch_cash_refresh(
    runtime: Runtime,
    interval_seconds: float = CASH_REFRESH_INTERVAL_SECONDS,
) -> None:
    """예수금 캐시를 주기적으로 다시 읽는다 — 입금 등으로 잔고가 바뀌어도

    "총 매수가능 금액" UI 표시가 최신 상태를 따라가도록 한다 (engine.cash_snapshot).
    """
    while True:
        await asyncio.sleep(interval_seconds)
        # 네트워크 호출이 이벤트 루프를 막지 않도록 별도 스레드로 넘긴다 (_off_loop 참고)
        await asyncio.get_running_loop().run_in_executor(None, runtime.engine.refresh_cash)


def adopt_carried_over_positions(runtime: Runtime) -> List[str]:
    """시작 시점에 남아 있는 보유 종목을 오늘의 매도 대상으로 편입한다.

    전일 청산에 실패했거나 앱이 꺼진 사이 넘어온 포지션은 아무도 구독하지 않는다.
    실시간 시세 구독은 당일 매수분(DailyWorkflow.execute_buys)에서만 걸리므로, 이월 포지션은
    시세가 오지 않아 익절/손절 판정(RiskManager.check_exit)이 한 번도 돌지 않는다.
    여기서 구독을 걸어야 감시가 시작된다. 15:20 강제청산은 잔고 전체를 읽으므로 자동 포함된다.

    평단가는 최초 매수 시점 기준이라, 이미 손절선을 넘긴 포지션은 첫 시세에 곧바로 청산된다.
    """
    held = runtime.engine.open_tickers
    if not held:
        return []

    runtime.ws_client.subscribe(held)
    logger.warning("이월 포지션을 매도 감시 대상으로 편입했습니다: %s", held)
    runtime.engine.notify(
        f"[알림] 전일 이월 보유 종목 {len(held)}개를 오늘 매도 대상으로 편입했습니다: {held}. "
        "익절/손절 감시가 시작되며, 남으면 15:20에 강제청산됩니다."
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
    watchdog = asyncio.create_task(watch_quote_stall(runtime))
    closeout_watch = asyncio.create_task(watch_closeout_report(runtime))
    cash_watch = asyncio.create_task(watch_cash_refresh(runtime))
    try:
        await asyncio.gather(runtime.ws_client.connect(), runtime.scheduler.run())
    except asyncio.CancelledError:
        pass
    finally:
        watchdog.cancel()
        closeout_watch.cancel()
        cash_watch.cancel()
        request_stop(runtime)
        await runtime.ws_client.disconnect()
        logger.info("매매 런타임이 종료되었습니다.")
