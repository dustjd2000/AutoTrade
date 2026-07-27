import logging
import uuid

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient
from src.core.events import OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# 키움 API ID / 경로
BUY_API_ID = "kt10000"     # 주식 매수주문
SELL_API_ID = "kt10001"    # 주식 매도주문
CANCEL_API_ID = "kt10003"  # 주식 취소주문
MODIFY_API_ID = "kt10002"  # 주식 정정주문
ORDER_PATH = "/api/dostk/ordr"

# 매매구분: "0" 보통(지정가), "3" 시장가
TRADE_TYPE_LIMIT = "0"
TRADE_TYPE_MARKET = "3"

DOMESTIC_EXCHANGE = "KRX"


class OrderClient:
    def __init__(self, settings: Settings, auth: AuthClient):
        self.settings = settings
        self.auth = auth
        self._client = KiwoomClient(settings, auth)

    def send_order(self, request: OrderRequest) -> OrderResult:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._call_api(request)
            except Exception as e:
                logger.warning("Order attempt %d failed: %s", attempt, e)
                if attempt == MAX_RETRIES:
                    return OrderResult(
                        order_id=str(uuid.uuid4()),
                        ticker=request.ticker,
                        side=request.side,
                        status=OrderStatus.REJECTED,
                        quantity=request.quantity,
                        error_message=str(e),
                    )

    def cancel_order(self, order_id: str, ticker: str, quantity: int = 0) -> bool:
        """주문 취소. quantity=0이면 잔량 전부 취소."""
        try:
            self._client.request(
                ORDER_PATH,
                CANCEL_API_ID,
                {
                    "dmst_stex_tp": DOMESTIC_EXCHANGE,
                    "orig_ord_no": order_id,
                    "stk_cd": ticker,
                    "cncl_qty": str(quantity),
                },
            )
            return True
        except Exception as e:
            logger.error("Cancel order failed (%s): %s", order_id, e)
            return False

    def modify_order(
        self, order_id: str, ticker: str, new_price: float, new_quantity: int
    ) -> OrderResult:
        data, _ = self._client.request(
            ORDER_PATH,
            MODIFY_API_ID,
            {
                "dmst_stex_tp": DOMESTIC_EXCHANGE,
                "orig_ord_no": order_id,
                "stk_cd": ticker,
                "mdfy_qty": str(new_quantity),
                "mdfy_uv": str(int(new_price)),
            },
        )
        return OrderResult(
            order_id=str(data.get("ord_no", order_id)),
            ticker=ticker,
            side=OrderSide.BUY,
            status=OrderStatus.PENDING,
            quantity=new_quantity,
        )

    def send_stop_order(self, ticker: str, quantity: int, trigger_price: float, side=OrderSide.SELL):
        """조건부 예약주문(스탑오더).

        키움 REST API에서 해당 엔드포인트가 확인되지 않아 미구현 상태다. 익절/손절은
        RiskManager.check_exit 기반 실시간 모니터링으로 처리한다 (PRD 5.5-B).
        """
        raise NotImplementedError("키움 REST API의 스탑오더 지원이 확인되지 않았습니다.")

    def _call_api(self, request: OrderRequest) -> OrderResult:
        api_id = BUY_API_ID if request.side == OrderSide.BUY else SELL_API_ID
        is_market = request.order_type == OrderType.MARKET

        body = {
            "dmst_stex_tp": DOMESTIC_EXCHANGE,
            "stk_cd": request.ticker,
            "ord_qty": str(request.quantity),
            # 시장가는 단가를 비워서 보낸다
            "ord_uv": "" if is_market else str(int(request.price or 0)),
            "trde_tp": TRADE_TYPE_MARKET if is_market else TRADE_TYPE_LIMIT,
            "cond_uv": "",
        }

        data, _ = self._client.request(ORDER_PATH, api_id, body)

        order_no = data.get("ord_no")
        if not order_no:
            raise ValueError(f"주문 응답에 주문번호가 없습니다: {data}")

        # 접수 성공 = 체결 아님. 실제 체결은 WebSocket 체결 통보로 확정된다.
        return OrderResult(
            order_id=str(order_no),
            ticker=request.ticker,
            side=request.side,
            status=OrderStatus.PENDING,
            quantity=request.quantity,
        )
