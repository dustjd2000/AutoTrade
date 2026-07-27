"""매매 런타임 조립 및 구동.

엔진 단독 실행은 하지 않고 UI에서만 제어하므로, UI 스레드가 이 모듈을 통해
전체 구성요소를 만들고 돌린다.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dt_time

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
RECOMMEND_TIME = dt_time(8, 45)     # 데이터 수집 → LLM 추천 → 이메일 발송
BUY_TIME = dt_time(9, 0)            # 자금 산정 → 매수 실행
FORCE_CLOSE_TIME = dt_time(15, 20)  # 당일 매도 원칙에 따른 미청산 포지션 정리
REPORT_TIME = dt_time(15, 30)       # 일일/월간 성과 리포트 이메일


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

    strategy = LLMMomentumStrategy()
    risk_manager = RiskManager(
        max_position_ratio=settings.max_position_ratio,
        max_daily_loss_ratio=settings.max_daily_loss_ratio,
        take_profit_ratio=settings.take_profit_ratio,
        stop_loss_ratio=settings.stop_loss_ratio,
        max_total_exposure_ratio=settings.max_total_exposure_ratio,
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

    scheduler = TimeScheduler()
    scheduler.add_job(RECOMMEND_TIME, workflow.recommend_and_notify, name="llm_recommend")
    scheduler.add_job(BUY_TIME, workflow.execute_buys, name="execute_buys")
    scheduler.add_job(
        FORCE_CLOSE_TIME,
        lambda: engine.force_close_all_positions(reason="day_end"),
        name="force_close_all_positions",
    )
    scheduler.add_job(REPORT_TIME, workflow.send_daily_report, name="daily_report")

    return Runtime(
        settings=settings,
        engine=engine,
        scheduler=scheduler,
        ws_client=ws_client,
        workflow=workflow,
    )


def request_stop(runtime: Runtime) -> None:
    """구동 루프가 스스로 빠져나오도록 표시한다 (다른 스레드에서 호출해도 안전)."""
    runtime.scheduler.stop()
    runtime.engine.stop()
    runtime.ws_client.request_stop()


async def run(runtime: Runtime) -> None:
    """엔진을 시작하고 스케줄러·실시간 시세를 함께 구동한다."""
    runtime.engine.start()
    try:
        await asyncio.gather(runtime.ws_client.connect(), runtime.scheduler.run())
    except asyncio.CancelledError:
        pass
    finally:
        request_stop(runtime)
        await runtime.ws_client.disconnect()
        logger.info("매매 런타임이 종료되었습니다.")
