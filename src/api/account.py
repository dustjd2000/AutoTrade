import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient, to_float, to_int

logger = logging.getLogger(__name__)

# 키움 API ID / 경로
# NOTE: api-id는 커뮤니티 래퍼 기준으로 확인했고, 경로는 /api/dostk/{분류} 규약을 따른다.
#       응답 필드명은 공식 문서(로그인 필요)로 최종 확인이 필요하다 — PRD 11절 참고.
DEPOSIT_API_ID = "kt00001"   # 예수금상세현황요청
BALANCE_API_ID = "kt00005"   # 체결잔고요청
ACCOUNT_PATH = "/api/dostk/acnt"


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_price: float
    current_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity


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
            ACCOUNT_PATH, BALANCE_API_ID, {"dmst_stex_tp": "KRX"}
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

            quantity = to_int(_first_present(row, "rmnd_qty", "hldg_qty", "cntr_qty"))
            if quantity <= 0:
                continue

            # 키움은 가격에 등락 방향 부호를 붙여 보낸다(하락 시 '-'). 절댓값을 취하지 않으면
            # 하락 종목의 현재가가 음수가 되어 손절/익절 판정이 완전히 어긋난다.
            positions[ticker] = Position(
                ticker=ticker,
                quantity=quantity,
                avg_price=abs(to_float(_first_present(row, "pur_pric", "pchs_avg_pric", "avg_prc"))),
                current_price=abs(to_float(_first_present(row, "cur_prc", "prpr", "now_pric"))),
            )
        return positions
