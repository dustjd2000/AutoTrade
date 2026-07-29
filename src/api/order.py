import logging
import uuid
from typing import List, Optional

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient, to_float, to_int
from src.core.events import (
    FillRecord,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# 키움 API ID / 경로
BUY_API_ID = "kt10000"     # 주식 매수주문
SELL_API_ID = "kt10001"    # 주식 매도주문
CANCEL_API_ID = "kt10003"  # 주식 취소주문
MODIFY_API_ID = "kt10002"  # 주식 정정주문
ORDER_PATH = "/api/dostk/ordr"

# 당일 체결내역 조회 — 2026-07-29 실계좌 응답으로 확정 (scripts/check_fills.py)
FILLS_API_ID = "ka10076"   # 실시간체결요청
FILLS_PATH = "/api/dostk/acnt"
FILLS_ROWS_KEY = "cntr"
# 하루 주문량을 크게 넘는 값 — 연속조회가 끝나지 않을 때 무한 루프를 막는 한도
MAX_FILL_PAGES = 20

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
                logger.warning(
                    "주문 시도 %d/%d 실패: %s %s x%d주 — %s",
                    attempt,
                    MAX_RETRIES,
                    request.side.value,
                    request.label,
                    request.quantity,
                    e,
                )
                if attempt == MAX_RETRIES:
                    # 여기서 ERROR로 남기지 않으면 거부된 주문이 error 로그에 전혀 남지 않는다
                    logger.error(
                        "주문 최종 실패 (%d회 시도): %s %s x%d주 — %s",
                        MAX_RETRIES,
                        request.side.value,
                        request.label,
                        request.quantity,
                        e,
                    )
                    return OrderResult(
                        order_id=str(uuid.uuid4()),
                        ticker=request.ticker,
                        side=request.side,
                        status=OrderStatus.REJECTED,
                        quantity=request.quantity,
                        error_message=str(e),
                        name=request.name,
                    )

    def get_today_fills(self) -> List[FillRecord]:
        """당일 주문의 체결 결과를 주문번호 단위로 조회한다.

        주문 접수(send_order)는 주문번호만 받고 체결 여부를 모르기 때문에, 이 조회가
        없으면 매매 기록이 영영 pending으로 남아 일일 리포트가 0건으로 집계된다.
        체결 통보(WebSocket) 대신 조회를 쓰는 이유는, 프로그램이 꺼져 있던 사이의
        체결도 나중에 그대로 채울 수 있어야 하기 때문이다.
        """
        body = {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"}
        fills: List[FillRecord] = []
        cont_yn, next_key = "N", ""

        for _ in range(MAX_FILL_PAGES):
            data, headers = self._client.request(
                FILLS_PATH, FILLS_API_ID, body, cont_yn=cont_yn, next_key=next_key
            )
            for row in data.get(FILLS_ROWS_KEY) or []:
                record = self._parse_fill(row)
                if record is not None:
                    fills.append(record)

            if headers.get("cont-yn") != "Y":
                break
            cont_yn, next_key = "Y", headers.get("next-key", "")
        else:
            logger.warning("체결내역 연속조회가 %d페이지를 넘었습니다. 이후 분은 누락됩니다.", MAX_FILL_PAGES)

        logger.info("당일 체결내역 %d건을 조회했습니다.", len(fills))
        return fills

    @staticmethod
    def _parse_fill(row: dict) -> Optional[FillRecord]:
        order_id = str(row.get("ord_no", "")).strip()
        # ka10076은 종목코드를 접두 없이 주지만, 다른 TR처럼 'A'가 붙어도 깨지지 않게 둔다
        ticker = str(row.get("stk_cd", "")).strip().lstrip("A")
        if not order_id or not ticker:
            return None

        # io_tp_nm은 '+매수' / '-매도' 형태로 온다
        io_type = str(row.get("io_tp_nm", ""))
        return FillRecord(
            order_id=order_id,
            ticker=ticker,
            side=OrderSide.SELL if "매도" in io_type else OrderSide.BUY,
            filled_quantity=to_int(row.get("cntr_qty")),
            filled_price=to_float(row.get("cntr_pric")),
            unfilled_quantity=to_int(row.get("oso_qty")),
            commission=to_float(row.get("tdy_trde_cmsn")),
            tax=to_float(row.get("tdy_trde_tax")),
            name=str(row.get("stk_nm", "")).strip() or None,
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
        """조건부 예약주문(스탑오더) — **키움 REST API 미지원 확정 (2026-07-28 확인)**.

        국내주식 주문 엔드포인트는 매수/매도/정정/취소(kt10000~kt10003)뿐이고 조건부·스탑·
        예약 주문은 존재하지 않는다. 따라서 익절/손절은 RiskManager.check_exit 기반 실시간
        모니터링으로만 처리된다 — 이 프로그램이 떠 있는 동안에만 동작한다는 뜻이다.

        서버 측 감시가 필요하면 영웅문4 [0624] 자동감시주문을 수동 등록해야 한다 (API 불가).
        이 메서드는 향후 키움이 조건부 주문 엔드포인트를 추가할 경우를 위한 자리로만 남긴다.
        """
        raise NotImplementedError(
            "키움 REST API는 조건부/스탑 주문을 제공하지 않는다 (2026-07-28 확인). "
            "서버 측 감시가 필요하면 영웅문4 [0624] 자동감시주문을 수동 등록할 것."
        )

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

        # 접수 성공 = 체결 아님. 실제 체결은 get_today_fills()로 확정한다.
        return OrderResult(
            order_id=str(order_no),
            ticker=request.ticker,
            side=request.side,
            status=OrderStatus.PENDING,
            quantity=request.quantity,
            name=request.name,
        )
