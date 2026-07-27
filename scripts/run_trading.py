"""자동매매 시스템 진입점."""
import asyncio
import logging
import signal
import sys
from datetime import time as dt_time
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from config.settings import Settings
from src.logger.logger import setup_logging
from src.api.auth import AuthClient
from src.api.market_data import MarketDataClient
from src.api.order import OrderClient
from src.api.account import AccountClient
from src.api.websocket_client import WebSocketClient
from src.strategy.llm_momentum import LLMMomentumStrategy
from src.risk.manager import RiskManager
from src.core.engine import TradingEngine
from src.core.daily_workflow import DailyWorkflow
from src.scheduler.scheduler import TimeScheduler
from src.logger.trade_store import TradeStore
from src.notification.alert import AlertNotifier
from src.notification.email import EmailNotifier
from src.api.client import KiwoomClient
from src.data.collector import DataCollector, LargeCapUniverse, NewsClient
from src.llm.recommender import LLMRecommender

# 1호 전략 하루 흐름 트리거 시각 (PRD 5.5-B, 5.11)
RECOMMEND_TIME = dt_time(8, 45)   # 데이터 수집 → LLM 추천 → 이메일 발송
BUY_TIME = dt_time(9, 0)          # 자금 산정 → 매수 실행
FORCE_CLOSE_TIME = dt_time(15, 20)  # 당일 매도 원칙에 따른 미청산 포지션 강제 정리
REPORT_TIME = dt_time(15, 30)     # 일일/월간 성과 리포트 이메일

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    settings = Settings()
    settings.validate()

    logger.info("Mode: %s", settings.mode)

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
    notifier = AlertNotifier(email)

    engine = TradingEngine(
        auth=auth,
        market_data=market_data,
        order_client=order_client,
        account=account,
        strategy=strategy,
        risk_manager=risk_manager,
        trade_store=trade_store,
        notifier=notifier,
        emergency_action=settings.emergency_action,
    )

    ws_client.on_data(engine.on_market_data)
    engine.start()

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

    loop = asyncio.get_running_loop()

    def shutdown():
        logger.info("Shutdown signal received.")
        scheduler.stop()
        engine.stop()
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    await asyncio.gather(ws_client.connect(), scheduler.run())


if __name__ == "__main__":
    asyncio.run(main())
