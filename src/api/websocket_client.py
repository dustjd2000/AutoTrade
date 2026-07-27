import asyncio
import json
import logging
from typing import Callable, List, Optional

import websockets

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import to_float, to_int
from src.core.events import MarketData

logger = logging.getLogger(__name__)

# 실시간 등록 타입: 0B = 주식체결
REALTIME_TYPE_TRADE = "0B"

RECONNECT_DELAY_SECONDS = 5


class WebSocketClient:
    """키움 실시간 시세 수신 (PRD 5.2).

    익절/손절 감시가 이 스트림에 의존하므로, 연결이 끊기면 재접속하며 구독을 복구한다.
    """

    def __init__(self, settings: Settings, auth: AuthClient):
        self.settings = settings
        self.auth = auth
        self._on_market_data: List[Callable[[MarketData], None]] = []
        self._tickers: List[str] = []
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

    def subscribe(self, tickers: List[str]) -> None:
        """관심종목 등록. 연결 중이면 즉시 등록 요청을 보낸다."""
        new = [t for t in tickers if t not in self._tickers]
        self._tickers.extend(new)
        if self._ws is not None and new:
            asyncio.create_task(self._send_subscribe(new))

    def on_data(self, callback: Callable[[MarketData], None]) -> None:
        self._on_market_data.append(callback)

    async def connect(self) -> None:
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.settings.websocket_url) as ws:
                    self._ws = ws
                    await self._login(ws)
                    if self._tickers:
                        await self._send_subscribe(self._tickers)
                    logger.info("WebSocket connected. Subscribed: %s", self._tickers)
                    await self._receive_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WebSocket error: %s", e)
            finally:
                self._ws = None

            if self._running:
                logger.warning("WebSocket 재접속을 %d초 후 시도합니다.", RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def disconnect(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _login(self, ws) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": self.auth.ensure_token()}))
        response = json.loads(await ws.recv())
        if response.get("return_code") not in (0, None):
            raise RuntimeError(f"WebSocket 로그인 실패: {response.get('return_msg')}")

    async def _send_subscribe(self, tickers: List[str]) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [{"item": tickers, "type": [REALTIME_TYPE_TRADE]}],
                }
            )
        )

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("WebSocket 메시지 파싱 실패: %s", raw[:200])
                continue

            trnm = message.get("trnm")
            if trnm == "PING":
                await ws.send(json.dumps({"trnm": "PONG"}))
                continue
            if trnm != "REAL":
                continue

            for item in message.get("data", []):
                data = self._parse_trade(item)
                if data is not None:
                    self._dispatch(data)

    def _parse_trade(self, item: dict) -> Optional[MarketData]:
        if item.get("type") != REALTIME_TYPE_TRADE:
            return None
        values = item.get("values", {})
        ticker = (item.get("item") or "").strip().lstrip("A")
        if not ticker:
            return None
        # 10: 현재가(부호 포함), 13: 누적거래량
        price = abs(to_float(values.get("10")))
        if price <= 0:
            return None
        return MarketData(ticker=ticker, price=price, volume=to_int(values.get("13")))

    def _dispatch(self, data: MarketData) -> None:
        for cb in self._on_market_data:
            try:
                cb(data)
            except Exception as e:
                logger.error("Market data callback error: %s", e)
