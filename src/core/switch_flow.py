"""2호 장중 전환의 매수 단계 — 매도 체결을 기다렸다가 반대 ETF를 시장가로 산다 (스펙 2026-11-11 5절).

엔진이 시세 콜백에서 전환 매도를 내면(`TradingEngine._execute_switch`) 런타임이 이 단계를 실행 큐
(`ActionRunner`)에 넣는다. 코루틴이라 기다리는 동안 이벤트 루프를 막지 않는다 — WebSocket PING 응답과
실시간 손절 감시가 그대로 돈다. 다만 **이 단계가 끝날 때까지 실행 큐를 잡고 있어** 그 사이 스케줄·버튼
실행은 뒤에서 기다린다 (손절·익절·AI 매도 판단은 큐를 타지 않는다).

실패는 언제나 "사지 않음"으로 떨어진다 — 그날은 현금으로 마감한다.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Iterable

from src.core.etf_switch import SWITCH_BUY_END_TIME, PendingSwitch
from src.core.events import BuyOutcome, FillRecord, OrderSide, format_stock

logger = logging.getLogger(__name__)

# 매도 체결 확인 — 처음 1분은 촘촘히, 그 뒤로는 느슨하게 본다
FILL_POLL_SECONDS = 5.0
FILL_FAST_WINDOW_SECONDS = 60.0
FILL_SLOW_POLL_SECONDS = 30.0
# 판정 시각으로부터 이 시간 안에 체결이 확인되지 않으면 포기한다 (15:14는 그 안에서도 지킨다)
FILL_MAX_WAIT_SECONDS = 300.0
# 반대 ETF 매수 재시도 (거부·조회 오류). 결과를 모르는 실패는 재시도하지 않는다
BUY_ATTEMPTS = 3
BUY_RETRY_SECONDS = 10.0


def sell_filled(fills: Iterable[FillRecord], order_id: str, quantity: int) -> bool:
    """그 매도 주문이 남김없이 체결됐는지.

    `DailyWorkflow.buy_orders_filled`와 같은 잣대다 — 같은 주문번호의 행을 모두 보고, 미체결
    잔량이 하나라도 있으면 아직이며, 가장 큰 체결수량이 주문수량 이상이어야 한다.
    """
    rows = [f for f in fills if f.order_id == order_id and f.side == OrderSide.SELL]
    if not rows or any(f.unfilled_quantity > 0 for f in rows):
        return False
    return max(f.filled_quantity for f in rows) >= quantity


class SwitchFlow:
    def __init__(
        self,
        engine,
        workflow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.engine = engine
        self.workflow = workflow
        self._sleep = sleep
        self._now = now

    async def run(self) -> None:
        pending = self.engine.pending_switch
        if pending is None:
            logger.info("전환 매수: 기다리는 전환이 없습니다 — 건너뜁니다.")
            return
        try:
            if not await self._wait_sell_fill(pending):
                logger.error("장중 전환: 매도 체결을 확인하지 못해 매수를 포기합니다: %s", pending.sold_summary)
                self.engine.notify(
                    f"[경고] 장중 전환: 매도 체결을 판정 후 5분 안에(또는 {SWITCH_BUY_END_TIME:%H:%M}까지) "
                    f"확인하지 못해 반대 ETF를 사지 않았습니다 — 15:15 마감 정리가 남은 것을 거둡니다: "
                    f"{pending.sold_summary}"
                )
                return
            await self._buy(pending)
        finally:
            # 성공·실패·포기 모두 여기서 끝난다 — '전량 매도 완료' 리포트 보류를 푼다
            self.engine.finish_switch()

    async def _wait_sell_fill(self, pending: PendingSwitch) -> bool:
        """매도 체결을 기다린다. 판정 후 5분이나 15:14 중 먼저 오는 시각을 넘기면 False."""
        loop = asyncio.get_running_loop()
        waited = 0.0
        warned = False
        deadline = min(
            pending.at + timedelta(seconds=FILL_MAX_WAIT_SECONDS),
            datetime.combine(pending.at.date(), SWITCH_BUY_END_TIME),
        )
        while True:
            if self._now() >= deadline:
                return False
            try:
                # 체결내역 조회는 블로킹 HTTP라 루프 밖에서 부른다
                fills = await loop.run_in_executor(None, self.engine.order_client.get_today_fills)
            except Exception:
                logger.warning("장중 전환: 매도 체결 조회 실패 — 다시 봅니다.", exc_info=True)
                fills = []
            if sell_filled(fills, pending.sell_order_id, pending.quantity):
                return True

            slow = waited >= FILL_FAST_WINDOW_SECONDS
            if slow and not warned:
                warned = True
                logger.warning(
                    "장중 전환: 매도가 %.0f초 안에 체결되지 않았습니다 — %.0f초마다 다시 봅니다: %s",
                    FILL_FAST_WINDOW_SECONDS, FILL_SLOW_POLL_SECONDS, pending.sold_summary,
                )
            interval = FILL_SLOW_POLL_SECONDS if slow else FILL_POLL_SECONDS
            await self._sleep(interval)
            waited += interval

    async def _buy(self, pending: PendingSwitch) -> None:
        decision = pending.decision
        label = format_stock(pending.buy_ticker, pending.buy_name)
        reason = (
            f"{pending.at:%H:%M} 전환 — 지수가 산 시점 대비 반대로 {decision.against:.2f}% 움직였고, "
            f"체결강도 {decision.strength:.0f}"
        )
        for attempt in range(1, BUY_ATTEMPTS + 1):
            if self._now().time() > SWITCH_BUY_END_TIME:
                break
            record, unknown = None, False
            try:
                # 주문은 루프 스레드에서 그대로 낸다 — 실시간 손절 감시와 직렬화한다
                record, unknown = self.workflow.execute_switch_buy(
                    pending.buy_ticker, self._now().date(), reason
                )
            except Exception:
                logger.exception("장중 전환 매수 시도 %d/%d 실패", attempt, BUY_ATTEMPTS)

            if record is not None and record.outcome == BuyOutcome.ORDERED:
                self.engine.begin_switch_leg(
                    2, pending.buy_ticker, index_entry=pending.index_entry, at=self._now()
                )
                self.engine.notify(
                    f"장중 전환: {pending.sold_summary} 매도 → {label} {record.quantity}주 시장가 매수\n"
                    f"판정: KODEX 200 산 시점 대비 반대로 {decision.against:.2f}%, "
                    f"체결강도 {decision.strength:.0f}, 순손익 {decision.net:+.2f}%"
                )
                return
            if unknown:
                self.engine.notify(
                    f"[경고] 장중 전환 매수 결과를 알 수 없습니다 — 다시 주문하지 않습니다. "
                    f"잔고를 확인하세요: {label}"
                )
                return
            if record is not None and record.outcome == BuyOutcome.SKIPPED:
                break  # 살 금액이 없다 — 다시 해도 같다
            if record is not None and record.outcome == BuyOutcome.FAILED and record.order_id is None:
                break  # 주문 전에 리스크 관리가 막았다 — 다시 해도 같다
            if attempt < BUY_ATTEMPTS:
                await self._sleep(BUY_RETRY_SECONDS)

        self.engine.notify(
            f"[경고] 장중 전환 매수 실패 — 오늘은 현금으로 마감합니다: {label} "
            f"(매도: {pending.sold_summary})"
        )
