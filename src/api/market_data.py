import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient, to_float, to_int
from src.core.events import MarketData

logger = logging.getLogger(__name__)


@dataclass
class PreviousDayMetrics:
    """전일 일봉(ka10086)에서 뽑아낸 후보 선정 지표 (PRD 5.5-B '전일 데이터 기준 수집').

    09:00 이전에는 당일 지표(등락률·시가갭·거래량)가 아예 존재하지 않아 후보를 고를 근거가
    되지 못한다 — 2026-08-06 08:55 수집에서 288종목 전부가 0으로 돌아왔다(PRD 10절
    '장 전 당일 지표 부재'). 그래서 전일 일봉을 쓴다. 장 전에도 장중에도 값이 같다.
    """

    ticker: str
    close: float        # 전일 종가
    high: float         # 전일 고가
    low: float          # 전일 저가
    change_rate: float  # 전일 등락률 (%)
    volume: int         # 전일 거래량
    volume_surge: float  # 전일 거래량 ÷ 그 이전 거래일 평균 (0이면 산출 불가)


@dataclass
class StockMaster:
    """종목 마스터 최소 정보 — 거래 가능한 종목인지 확인하는 용도.

    OpenAPI+(OCX)의 GetMasterCodeName / GetMasterLastPrice에 해당한다.
    REST에는 마스터 파일이 없어 ka10001 응답의 종목명·현재가로 대신한다.
    """

    ticker: str
    name: Optional[str]
    price: float

# 키움 API ID / 경로 (경로는 /api/dostk/{분류} 규약)
STOCK_INFO_API_ID = "ka10001"   # 주식기본정보요청
ORDERBOOK_API_ID = "ka10004"    # 주식호가요청
DAILY_PRICE_API_ID = "ka10086"  # 일별주가요청

STOCK_INFO_PATH = "/api/dostk/stkinfo"
MARKET_PATH = "/api/dostk/mrkcond"

# ka10086이 한 번 호출에 돌려주는 20거래일치를 그대로 쓴다. 여기에 당일 봉이 섞여 오므로
# 그것을 뺀 나머지(전일 1행 + 그 이전 행들)로 전일 지표와 급증 배수를 계산한다.
DAILY_CANDLE_COUNT = 20


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

    def get_stock_master(self, ticker: str) -> StockMaster:
        """종목명과 현재가만 확인하는 최소 조회.

        상장폐지·거래정지 종목은 여기서 KiwoomAPIError('종목 정보가 없습니다')가 나거나
        종목명·현재가가 빈 값으로 돌아온다. 잔고에는 그대로 실려 오므로 이 조회로만 구분된다.
        """
        data, _ = self._client.request(STOCK_INFO_PATH, STOCK_INFO_API_ID, {"stk_cd": ticker})

        name = _first_present(data, "stk_nm", "stk_nm_shrt", "hts_kor_isnm", "prdt_name")
        # 키움은 등락 방향을 부호로 실어 보내므로 절댓값을 취한다
        price = abs(to_float(_first_present(data, "cur_prc", "prpr", "now_pric")))
        return StockMaster(
            ticker=ticker,
            name=str(name).strip() if name else None,
            price=price,
        )

    def get_ohlcv(
        self,
        ticker: str,
        period: str = "D",
        count: int = 100,
        base_date: Optional[date] = None,
    ) -> List[dict]:
        """일봉 데이터 조회 (기준일로부터 과거 방향). period는 현재 일봉('D')만 지원한다.

        `qry_dt`는 ka10086의 필수 파라미터다 — 비워서 보내면 API가 거부한다
        (return_code=2, "필수입력 파라미터=qry_dt"). 기준일을 주지 않으면 오늘로 잡는다.
        """
        if period != "D":
            raise NotImplementedError("분봉 조회는 별도 TR(ka10080 계열) 연동이 필요합니다.")

        query_date = (base_date or date.today()).strftime("%Y%m%d")
        data, _ = self._client.request(
            MARKET_PATH,
            DAILY_PRICE_API_ID,
            {"stk_cd": ticker, "qry_dt": query_date, "indc_tp": "0"},
        )
        for key in ("daly_stkpc", "output", "list"):
            if isinstance(data.get(key), list):
                return data[key][:count]

        logger.error("일별주가 응답에서 목록 필드를 찾지 못했습니다. 응답 키: %s", list(data.keys()))
        return []

    def get_previous_day_metrics(
        self, ticker: str, today: Optional[date] = None
    ) -> Optional[PreviousDayMetrics]:
        """일봉 한 번으로 전일 지표를 모두 산출한다. 쓸 수 있는 봉이 없으면 None.

        응답에는 당일 봉도 섞여 온다(장 전이면 거래량 0, 장중이면 아직 진행 중인 값).
        날짜로 걸러내야 추천을 장 전에 돌리든 장중에 돌리든 같은 결과가 나온다.

        급증 배수의 분모에서는 전일 자신도 뺀다 — 전일을 평균에 넣으면 재려던 급증분이
        그만큼 희석된다.
        """
        candles = self.get_ohlcv(ticker, period="D", count=DAILY_CANDLE_COUNT)
        today_str = (today or date.today()).strftime("%Y%m%d")
        past = [
            candle
            for candle in candles
            if str(_first_present(candle, "date", "dt") or "").strip() != today_str
        ]
        if not past:
            logger.warning("전일 일봉을 찾지 못했습니다: %s", ticker)
            return None

        previous, earlier = past[0], past[1:]
        volumes = [
            volume
            for volume in (to_int(_first_present(c, "trde_qty", "acml_vol")) for c in earlier)
            if volume > 0
        ]
        average = sum(volumes) / len(volumes) if volumes else 0.0
        volume = to_int(_first_present(previous, "trde_qty", "acml_vol"))

        # 키움은 가격에 등락 방향 부호를 붙여 보내므로 절댓값을 취한다 (등락률은 부호가 의미다)
        return PreviousDayMetrics(
            ticker=ticker,
            close=abs(to_float(_first_present(previous, "close_pric", "cur_prc"))),
            high=abs(to_float(_first_present(previous, "high_pric"))),
            low=abs(to_float(_first_present(previous, "low_pric"))),
            change_rate=to_float(_first_present(previous, "flu_rt")),
            volume=volume,
            volume_surge=volume / average if average > 0 else 0.0,
        )

    def get_orderbook(self, ticker: str) -> dict:
        """호가 조회."""
        data, _ = self._client.request(MARKET_PATH, ORDERBOOK_API_ID, {"stk_cd": ticker})
        return data
