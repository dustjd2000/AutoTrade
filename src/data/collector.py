import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

from src.api.client import KiwoomClient
from src.api.market_data import MarketDataClient

logger = logging.getLogger(__name__)

# 종목정보 리스트 (ka10099) — 시장 전체 종목과 분류 정보를 반환한다
STOCK_LIST_API_ID = "ka10099"
STOCK_LIST_PATH = "/api/dostk/stkinfo"
MARKET_TYPE_KOSPI = "0"

# ka10099의 upSizeName 분류. '대형주'는 KRX 시가총액 규모별 분류의 상위 100종목이다.
LARGE_CAP_LABEL = "대형주"
KOSPI_MARKET_NAME = "거래소"  # ETF/ETN/리츠 등을 제외하기 위한 값

# 매매 불가 상태를 나타내는 문자열 (state 필드에 파이프로 여러 값이 들어온다)
BLOCKED_STATE_KEYWORDS = ("거래정지", "관리종목", "투자위험", "정리매매")

# 종목별 상세 조회 사이의 최소 간격 — 유량 제한(Rate Limit) 회피용 (PRD 8절)
DETAIL_REQUEST_INTERVAL_SECONDS = 0.2


@dataclass
class DailyStockData:
    """1호 전략의 LLM 프롬프트에 쓰이는 종목별 당일 데이터 (PRD 5.5-B 1단계)."""

    ticker: str
    name: str
    change_rate: float  # 전일 종가 대비 등락률 (%)
    volume: int  # 당일 거래량 (장 전에는 동시호가 예상체결량)
    gap_rate: float  # 시가 갭 (%) (장 전에는 예상체결가 기준)
    headlines: List[str] = field(default_factory=list)  # 뉴스/공시 헤드라인
    is_premarket: bool = False  # 동시호가 예상체결 기준 값인지 여부


class LargeCapUniverse:
    """대형주 유니버스 — 키움 ka10099의 '대형주' 분류(시가총액 상위 100종목)를 사용한다.

    PRD는 코스피200을 기준으로 적었으나, 키움 API가 코스피200 구성종목을 직접 제공하지 않고
    시가총액 규모별 분류(대형/중형/소형)를 제공하므로 '대형주 100종목'으로 구현한다.
    거래정지·관리종목처럼 매매가 막힌 종목은 여기서 미리 걸러낸다.
    """

    def __init__(self, client: KiwoomClient):
        self._client = client

    def get_tickers(self) -> List[str]:
        return [row["code"] for row in self.get_rows()]

    def get_names(self) -> Dict[str, str]:
        return {row["code"]: row.get("name", row["code"]) for row in self.get_rows()}

    def get_rows(self) -> List[dict]:
        data, _ = self._client.request(
            STOCK_LIST_PATH, STOCK_LIST_API_ID, {"mrkt_tp": MARKET_TYPE_KOSPI}
        )
        rows = data.get("list")
        if not isinstance(rows, list):
            logger.error("종목 리스트 응답 형식이 예상과 다릅니다. 키: %s", list(data.keys()))
            return []

        selected = [
            row
            for row in rows
            if row.get("upSizeName") == LARGE_CAP_LABEL
            and row.get("marketName") == KOSPI_MARKET_NAME
            and not self._is_blocked(row)
        ]
        logger.info("대형주 유니버스 %d종목 (전체 %d종목 중)", len(selected), len(rows))
        return selected

    @staticmethod
    def _is_blocked(row: dict) -> bool:
        state = str(row.get("state", ""))
        if any(keyword in state for keyword in BLOCKED_STATE_KEYWORDS):
            return True
        # orderWarning: '0'이 정상, 그 외는 투자주의/경고/위험 등
        return str(row.get("orderWarning", "0")) not in ("0", "")


class NewsClient:
    """종목별 당일 뉴스/공시 헤드라인 수집.

    데이터 소스(DART, 뉴스 API 등)가 아직 확정되지 않아 빈 목록을 반환한다 (PRD 10절).
    헤드라인이 없어도 등락률·거래량·시가갭만으로 LLM 추천은 동작한다.
    """

    def get_headlines(self, ticker: str) -> List[str]:
        return []


class DataCollector:
    """08:45 LLM 추천에 사용할 당일 데이터를 종목별로 수집한다 (PRD 5.5-B 1단계).

    개별 종목 수집이 실패해도 전체 수집을 중단하지 않고 건너뛴다 — 일부 종목의
    일시적 조회 실패로 당일 추천 전체가 스킵되는 것을 방지한다.
    """

    def __init__(
        self,
        market_data: MarketDataClient,
        universe: LargeCapUniverse,
        news_client: NewsClient,
        request_interval: float = DETAIL_REQUEST_INTERVAL_SECONDS,
    ):
        self.market_data = market_data
        self.universe = universe
        self.news_client = news_client
        self.request_interval = request_interval

    def collect(self) -> List[DailyStockData]:
        results: List[DailyStockData] = []
        try:
            rows = self.universe.get_rows()
        except Exception:
            logger.exception("대형주 유니버스 조회에 실패했습니다.")
            return results

        for index, row in enumerate(rows):
            ticker = row["code"]
            try:
                results.append(self._collect_one(ticker, row.get("name", ticker)))
            except Exception:
                logger.exception("종목 데이터 수집 실패: %s", ticker)

            # 마지막 종목 뒤에는 대기하지 않는다
            if self.request_interval and index < len(rows) - 1:
                time.sleep(self.request_interval)

        logger.info("당일 데이터 수집 완료: %d/%d 종목", len(results), len(rows))
        return results

    def _collect_one(self, ticker: str, name: str) -> DailyStockData:
        detail = self.market_data.get_stock_detail(ticker)
        return DailyStockData(
            ticker=ticker,
            name=name,
            change_rate=detail.change_rate,
            volume=detail.volume,
            gap_rate=detail.gap_rate,
            headlines=self.news_client.get_headlines(ticker),
            is_premarket=detail.is_premarket,
        )
