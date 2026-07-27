import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient, to_float, to_int
from src.core.events import MarketData

logger = logging.getLogger(__name__)


@dataclass
class StockDetail:
    """주식기본정보(ka10001)에서 뽑아낸 당일 지표."""

    ticker: str
    price: float        # 현재가
    volume: int         # 누적 거래량
    change_rate: float  # 전일 대비 등락률 (%)
    gap_rate: float     # 시가 갭 (%) — 전일 종가 대비 당일 시가

# 키움 API ID / 경로 (경로는 /api/dostk/{분류} 규약)
STOCK_INFO_API_ID = "ka10001"   # 주식기본정보요청
ORDERBOOK_API_ID = "ka10004"    # 주식호가요청
DAILY_PRICE_API_ID = "ka10086"  # 일별주가요청

STOCK_INFO_PATH = "/api/dostk/stkinfo"
MARKET_PATH = "/api/dostk/mrkcond"


def _first_present(row: Dict[str, Any], *keys: str):
    for key in keys:
        if key in row and str(row[key]).strip() != "":
            return row[key]
    return None


class MarketDataClient:
    def __init__(self, settings: Settings, auth: AuthClient):
        self.settings = settings
        self.auth = auth
        self._client = KiwoomClient(settings, auth)

    def get_current_price(self, ticker: str) -> MarketData:
        """현재가 단건 조회."""
        data, _ = self._client.request(STOCK_INFO_PATH, STOCK_INFO_API_ID, {"stk_cd": ticker})

        price = _first_present(data, "cur_prc", "prpr", "now_pric")
        if price is None:
            raise ValueError(f"현재가 응답에서 가격 필드를 찾지 못했습니다: {list(data.keys())}")

        # 키움은 등락 방향을 부호로 실어 보내므로 절댓값을 취한다
        return MarketData(
            ticker=ticker,
            price=abs(to_float(price)),
            volume=to_int(_first_present(data, "trde_qty", "acml_vol", "vol")),
        )

    def get_stock_detail(self, ticker: str) -> StockDetail:
        """등락률·시가갭까지 포함한 당일 지표를 한 번의 호출로 가져온다 (LLM 프롬프트용)."""
        data, _ = self._client.request(STOCK_INFO_PATH, STOCK_INFO_API_ID, {"stk_cd": ticker})

        # 키움은 가격에 등락 방향 부호를 붙여 보내므로 절댓값을 취한다
        price = abs(to_float(_first_present(data, "cur_prc", "prpr")))
        base_price = abs(to_float(_first_present(data, "base_pric")))   # 전일 종가
        open_price = abs(to_float(_first_present(data, "open_pric")))   # 당일 시가

        # flu_rt는 키움이 계산해 주는 등락률. 없으면 전일 종가로 직접 계산한다.
        change_rate = to_float(_first_present(data, "flu_rt"))
        if change_rate == 0.0 and base_price > 0 and price > 0:
            change_rate = (price - base_price) / base_price * 100

        gap_rate = ((open_price - base_price) / base_price * 100) if base_price > 0 else 0.0

        return StockDetail(
            ticker=ticker,
            price=price,
            volume=to_int(_first_present(data, "trde_qty", "acml_vol")),
            change_rate=change_rate,
            gap_rate=gap_rate,
        )

    def get_ohlcv(self, ticker: str, period: str = "D", count: int = 100) -> List[dict]:
        """일봉 데이터 조회. period는 현재 일봉('D')만 지원한다."""
        if period != "D":
            raise NotImplementedError("분봉 조회는 별도 TR(ka10080 계열) 연동이 필요합니다.")

        data, _ = self._client.request(
            MARKET_PATH, DAILY_PRICE_API_ID, {"stk_cd": ticker, "qry_dt": "", "indc_tp": "0"}
        )
        for key in ("daly_stkpc", "output", "list"):
            if isinstance(data.get(key), list):
                return data[key][:count]

        logger.error("일별주가 응답에서 목록 필드를 찾지 못했습니다. 응답 키: %s", list(data.keys()))
        return []

    def get_orderbook(self, ticker: str) -> dict:
        """호가 조회."""
        data, _ = self._client.request(MARKET_PATH, ORDERBOOK_API_ID, {"stk_cd": ticker})
        return data
