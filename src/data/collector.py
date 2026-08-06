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

# ka10099의 upSizeName 분류 (KRX 시가총액 규모별). 코스피 기준 대형주 약 98종목.
# 중형주(192종목)는 2026-08-05에 넣었다가 지정가 매수를 도입하며 하루 만에 뺐다 — 호가가
# 얇아 목표가 지정가의 체결·슬리피지 조건이 나쁘다. 소형주(512종목)도 같은 이유로 제외한다.
LARGE_CAP_LABEL = "대형주"
KOSPI_MARKET_NAME = "거래소"  # ETF/ETN/리츠 등을 제외하기 위한 값

# 매매 불가 상태를 나타내는 문자열 (state 필드에 파이프로 여러 값이 들어온다)
BLOCKED_STATE_KEYWORDS = ("거래정지", "관리종목", "투자위험", "정리매매")

# 종목별 일봉 조회 사이의 최소 간격 — 유량 제한(Rate Limit) 회피용 (PRD 8절)
DETAIL_REQUEST_INTERVAL_SECONDS = 0.2

# LLM에 넘길 후보 수. 유니버스 98종목을 통째로 넣으면 프롬프트가 지나치게 길어져,
# 전일 거래량 급증 배수 상위만 추린다.
SHORTLIST_SIZE = 25


@dataclass
class DailyStockData:
    """1호 전략의 LLM 프롬프트에 쓰이는 종목별 데이터 — 전부 **전일** 기준이다 (PRD 5.5-B).

    당일 지표를 쓰지 않는 이유는 PRD 10절 '장 전 당일 지표 부재'에 있다. 09:00 이전에는
    등락률·시가갭이 전 종목 0으로 돌아와 후보를 고를 근거가 되지 못한다.
    """

    ticker: str
    name: str
    prev_close: float        # 전일 종가 (목표 매수가 산정의 기준값)
    prev_high: float         # 전일 고가
    prev_low: float          # 전일 저가
    prev_change_rate: float  # 전일 등락률 (%)
    prev_volume: int         # 전일 거래량
    volume_surge: float      # 전일 거래량 ÷ 그 이전 거래일 평균 (0이면 산출 불가)
    headlines: List[str] = field(default_factory=list)  # 뉴스/공시 헤드라인


class LargeCapUniverse:
    """대형주 유니버스 — 키움 ka10099의 시가총액 규모별 분류를 사용한다.

    PRD는 코스피200을 기준으로 적었으나, 키움 API가 코스피200 구성종목을 직접 제공하지 않고
    시가총액 규모별 분류(대형/중형/소형)를 제공하므로 이를 대신 쓴다. 2026-08-05에 중형주까지
    넓혔다가 2026-08-06에 대형주만으로 되돌렸다(위 LARGE_CAP_LABEL 주석 참고).
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
    헤드라인이 없어도 전일 등락률·거래량 급증 배수만으로 LLM 추천은 동작한다.
    """

    def get_headlines(self, ticker: str) -> List[str]:
        return []


class DataCollector:
    """LLM 추천에 사용할 전일 데이터를 종목별로 수집한다 (PRD 5.5-B).

    유니버스 전 종목에 일봉(ka10086)을 한 번씩 조회하는 것이 전부다 — 한 호출이 20거래일치를
    돌려주므로 전일 종가·고가·저가·등락률·거래량과 급증 배수가 모두 여기서 나온다.
    당일 기본정보(ka10001)는 조회하지 않는다: 09:00 이전에는 값이 전부 0이고(PRD 10절),
    매수 수량도 목표 매수가 기준으로 산정해 현재가가 필요 없다.

    개별 종목 수집이 실패해도 전체 수집을 중단하지 않고 건너뛴다 — 일부 종목의
    일시적 조회 실패로 당일 추천 전체가 스킵되는 것을 방지한다.
    """

    def __init__(
        self,
        market_data: MarketDataClient,
        universe: LargeCapUniverse,
        news_client: NewsClient,
        request_interval: float = DETAIL_REQUEST_INTERVAL_SECONDS,
        shortlist_size: int = SHORTLIST_SIZE,
    ):
        self.market_data = market_data
        self.universe = universe
        self.news_client = news_client
        self.request_interval = request_interval
        self.shortlist_size = shortlist_size

    def collect(self) -> List[DailyStockData]:
        try:
            rows = self.universe.get_rows()
        except Exception:
            logger.exception("대형주 유니버스 조회에 실패했습니다.")
            return []

        collected = self._collect_previous_day(rows)
        logger.info("전일 데이터 수집 완료: %d/%d 종목", len(collected), len(rows))

        shortlist = self._shortlist(collected)
        self._log_candidates(shortlist)
        return shortlist

    def _collect_previous_day(self, rows: List[dict]) -> List[DailyStockData]:
        """유니버스 전 종목의 전일 지표를 모은다 (종목당 일봉 1회)."""
        results: List[DailyStockData] = []
        for index, row in enumerate(rows):
            ticker = row["code"]
            try:
                collected = self._collect_one(ticker, row.get("name", ticker))
                if collected is not None:
                    results.append(collected)
            except Exception:
                logger.exception("종목 데이터 수집 실패: %s", ticker)

            # 마지막 종목 뒤에는 대기하지 않는다
            if self.request_interval and index < len(rows) - 1:
                time.sleep(self.request_interval)
        return results

    def _shortlist(self, candidates: List[DailyStockData]) -> List[DailyStockData]:
        """전일 거래량 급증 배수 상위 N종목 — LLM에 넘길 후보다.

        급등 예상 전략이므로 '전일 상승 + 거래량 급증'이 1순위다. 다만 상승 종목만으로 정원을
        채우지 못하는 날(지수 급락 다음날 등)에 후보가 비면 추천 자체가 스킵되므로, 남는 자리는
        하락·보합 종목 중 급증 배수 순으로 채운다 (PRD 5.5-B '전일 데이터 기준 수집').
        """
        by_surge = sorted(candidates, key=lambda data: data.volume_surge, reverse=True)
        risen = [data for data in by_surge if data.prev_change_rate > 0]
        fallen = [data for data in by_surge if data.prev_change_rate <= 0]

        shortlist = (risen + fallen)[: self.shortlist_size]
        logger.info(
            "후보 %d종목 (전일 상승 %d종목 / 하락·보합 %d종목 중)",
            len(shortlist),
            len(risen),
            len(fallen),
        )
        return shortlist

    def _log_candidates(self, shortlist: List[DailyStockData]) -> None:
        """LLM에 넘길 후보를 그대로 남긴다 — 나중에 추천이 타당했는지 되짚을 유일한 근거다."""
        logger.info(
            "LLM 후보 %d종목 (종목 | 전일 등락률 | 전일 거래량 | 평균대비 | 전일 종가):",
            len(shortlist),
        )
        for data in shortlist:
            surge = f"{data.volume_surge:.2f}배" if data.volume_surge else "판단불가"
            logger.info(
                "  %s %s | %+.2f%% | %s | %s | %s원",
                data.ticker,
                data.name,
                data.prev_change_rate,
                f"{data.prev_volume:,}",
                surge,
                f"{data.prev_close:,.0f}",
            )

    def _collect_one(self, ticker: str, name: str):
        metrics = self.market_data.get_previous_day_metrics(ticker)
        if metrics is None:
            return None
        return DailyStockData(
            ticker=ticker,
            name=name,
            prev_close=metrics.close,
            prev_high=metrics.high,
            prev_low=metrics.low,
            prev_change_rate=metrics.change_rate,
            prev_volume=metrics.volume,
            volume_surge=metrics.volume_surge,
            headlines=self.news_client.get_headlines(ticker),
        )
