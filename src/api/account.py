import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient, to_float, to_int
from src.core.events import format_stock

logger = logging.getLogger(__name__)

# 키움 API ID / 경로 — 실계좌 응답으로 검증했다 (2026-07-28, scripts/check_balance.py).
#
# 잔고는 kt00018(계좌평가잔고내역요청)을 쓴다. kt00005(체결잔고요청)도 보유 종목을 돌려주지만
# 수량 필드가 `cur_qty`, 평단이 `buy_uv`로 이름이 달라 아래 파싱 후보와 맞지 않는다.
# 그대로 두면 행을 전부 버리고 '보유 없음'으로 읽혀 익절/손절·강제청산이 조용히 멈춘다.
DEPOSIT_API_ID = "kt00001"   # 예수금상세현황요청
BALANCE_API_ID = "kt00018"   # 계좌평가잔고내역요청
ACCOUNT_PATH = "/api/dostk/acnt"


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_price: float
    current_price: float = 0.0
    name: Optional[str] = None
    # 매도가능수량. 미결제·미체결 매도 주문이 걸려 있으면 보유수량보다 적다.
    # None은 '응답에서 읽지 못했다'는 뜻이며, 0(정말로 팔 수 없음)과 구분해야 한다.
    sellable_quantity: Optional[int] = None

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

    @property
    def label(self) -> str:
        """로그·알림 표기 — `(종목코드)종목명`."""
        return format_stock(self.ticker, self.name)

    @property
    def closable_quantity(self) -> int:
        """청산 주문에 실을 수량.

        매도가능수량을 읽지 못했으면 보유수량으로 시도한다 — 필드를 못 읽었다는 이유로
        매도를 포기하면 포지션이 조용히 이월된다. 거부되더라도 시도하는 편이 낫다.
        """
        if self.sellable_quantity is None:
            return self.quantity
        return min(self.sellable_quantity, self.quantity)

    @property
    def has_cost_basis(self) -> bool:
        """매입 이력이 있는 포지션인지 (평단 0이면 이 프로그램이 매수한 것이 아니다)."""
        return self.avg_price > 0


@dataclass
class BalanceSnapshot:
    cash: float
    positions: Dict[str, Position]
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def total_asset(self) -> float:
        stock_value = sum(p.quantity * p.current_price for p in self.positions.values())
        return self.cash + stock_value


def _first_present(row: Dict, *keys: str):
    """키움 응답 필드명이 TR마다 조금씩 달라 후보 키를 순서대로 찾는다."""
    for key in keys:
        if key in row and str(row[key]).strip() != "":
            return row[key]
    return None


class AccountClient:
    def __init__(self, settings: Settings, auth: AuthClient):
        self.settings = settings
        self.auth = auth
        self._client = KiwoomClient(settings, auth)

    def get_balance_snapshot(self) -> BalanceSnapshot:
        return BalanceSnapshot(cash=self.get_cash(), positions=self.get_positions())

    def get_cash(self) -> float:
        """주문 가능 예수금(원)."""
        data, _ = self._client.request(
            ACCOUNT_PATH, DEPOSIT_API_ID, {"qry_tp": "3"}  # 3: 추정조회
        )
        # 주문가능금액 → 없으면 예수금 계열 필드로 대체
        cash = _first_present(data, "ord_alow_amt", "ord_alowa", "entr", "dnca_tot_amt")
        if cash is None:
            logger.error("예수금 응답에서 금액 필드를 찾지 못했습니다. 응답 키: %s", list(data.keys()))
            return 0.0
        return to_float(cash)

    def get_positions(self) -> Dict[str, Position]:
        """보유 종목 딕셔너리 (종목코드 → Position)."""
        data, _ = self._client.request(
            ACCOUNT_PATH, BALANCE_API_ID, {"qry_tp": "1", "dmst_stex_tp": "KRX"}
        )

        rows = None
        for key in ("stk_cntr_remn", "acnt_evlt_remn_indv_tot", "output", "list"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        if rows is None:
            logger.error("잔고 응답에서 목록 필드를 찾지 못했습니다. 응답 키: %s", list(data.keys()))
            return {}

        positions: Dict[str, Position] = {}
        for row in rows:
            ticker = _first_present(row, "stk_cd", "pdno", "stkcd")
            if not ticker:
                continue
            ticker = str(ticker).strip().lstrip("A")  # 'A005930' 형태 대비

            quantity = to_int(_first_present(row, "rmnd_qty", "hldg_qty", "cntr_qty", "cur_qty"))
            if quantity <= 0:
                continue

            # 키움은 가격에 등락 방향 부호를 붙여 보낸다(하락 시 '-'). 절댓값을 취하지 않으면
            # 하락 종목의 현재가가 음수가 되어 손절/익절 판정이 완전히 어긋난다.
            name = _first_present(row, "stk_nm", "prdt_name", "stk_nm_shrt", "hts_kor_isnm")
            # 매도가능수량도 TR마다 이름이 다르다. 못 찾으면 None으로 두어야
            # closable_quantity가 보유수량으로 폴백한다 (0으로 읽으면 매도가 아예 막힌다).
            sellable = _first_present(
                row, "trde_able_qty", "ord_psbl_qty", "sll_able_qty", "sell_able_qty"
            )
            positions[ticker] = Position(
                ticker=ticker,
                quantity=quantity,
                avg_price=abs(
                    to_float(_first_present(row, "pur_pric", "pchs_avg_pric", "avg_prc", "buy_uv"))
                ),
                current_price=abs(to_float(_first_present(row, "cur_prc", "prpr", "now_pric"))),
                name=str(name).strip() if name else None,
                sellable_quantity=to_int(sellable) if sellable is not None else None,
            )

        # 응답은 왔는데 한 건도 해석하지 못한 상태를 조용히 넘기면 '보유 없음'으로 읽혀
        # 익절/손절 감시와 강제청산이 아무 일도 하지 않는다 (2026-07-28 실제 발생).
        if rows and not positions:
            logger.error(
                "잔고 %d행을 받았지만 보유 종목을 하나도 해석하지 못했습니다. "
                "응답 필드명이 바뀌었을 수 있습니다 — scripts/check_balance.py로 확인하세요. 첫 행 키: %s",
                len(rows),
                list(rows[0].keys()),
            )
        return positions
