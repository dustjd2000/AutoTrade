import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from src.api.client import KiwoomClient
from src.api.market_data import MarketDataClient

logger = logging.getLogger(__name__)

# 종목정보 리스트 (ka10099) — 시장 전체 종목과 분류 정보를 반환한다
STOCK_LIST_API_ID = "ka10099"
STOCK_LIST_PATH = "/api/dostk/stkinfo"
MARKET_TYPE_KOSPI = "0"

# ka10099의 upSizeName 분류 (KRX 시가총액 규모별). 코스피 기준 대형주 99 / 중형주 192종목.
# 소형주(512종목)는 유동성이 얇아 시장가 매수 슬리피지가 커서 유니버스에서 제외한다 (확정 2026-08-05).
LARGE_CAP_LABEL = "대형주"
MID_CAP_LABEL = "중형주"
TARGET_CAP_LABELS = (LARGE_CAP_LABEL, MID_CAP_LABEL)
KOSPI_MARKET_NAME = "거래소"  # ETF/ETN/리츠 등을 제외하기 위한 값

# 매매 불가 상태를 나타내는 문자열 (state 필드에 파이프로 여러 값이 들어온다)
BLOCKED_STATE_KEYWORDS = ("거래정지", "관리종목", "투자위험", "정리매매")

# 종목별 상세 조회 사이의 최소 간격 — 유량 제한(Rate Limit) 회피용 (PRD 8절)
DETAIL_REQUEST_INTERVAL_SECONDS = 0.2

# LLM에 넘길 후보 수 (시가갭 상위 기준). 291종목을 통째로 넣으면 프롬프트가 지나치게 길고
# 거래량 급증률 조회도 291회가 필요해, 규모별로 상위만 추려 2단계 조회 대상을 묶는다.
SHORTLIST_BY_CAP = {LARGE_CAP_LABEL: 25, MID_CAP_LABEL: 15}


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
    cap_tier: str = LARGE_CAP_LABEL  # 시가총액 규모 분류 (대형주/중형주)
    volume_surge: float = 0.0  # 20일 평균 거래량 대비 배수 (0이면 미조회/산출 불가)

    @property
    def has_direction(self) -> bool:
        """방향성 신호가 있는지 — 등락률과 시가갭이 모두 0이면 판단 근거가 없다.

        장 전에는 동시호가 예상체결가가 아직 잡히지 않은 종목이 두 값 모두 0.00%로 넘어온다.
        이런 종목을 후보에 남겨두면, 추천 개수를 채우라는 지시(프롬프트 v3~)에 밀려
        "등락률 +0.00%, 시가갭 +0.00%"를 근거로 든 추천이 나온다 (확정 2026-08-05).
        """
        return self.change_rate != 0.0 or self.gap_rate != 0.0


class LargeMidCapUniverse:
    """대형·중형주 유니버스 — 키움 ka10099의 시가총액 규모별 분류를 사용한다.

    PRD는 코스피200을 기준으로 적었으나, 키움 API가 코스피200 구성종목을 직접 제공하지 않고
    시가총액 규모별 분류(대형/중형/소형)를 제공하므로 이를 대신 쓴다. 처음에는 대형주만
    담았고, 중형주에서도 한 종목을 뽑도록 하면서 범위를 넓혔다 (확정 2026-08-05).
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
            if row.get("upSizeName") in TARGET_CAP_LABELS
            and row.get("marketName") == KOSPI_MARKET_NAME
            and not self._is_blocked(row)
        ]
        by_tier = Counter(row.get("upSizeName") for row in selected)
        logger.info(
            "대형·중형주 유니버스 %d종목 (대형 %d / 중형 %d, 전체 %d종목 중)",
            len(selected),
            by_tier.get(LARGE_CAP_LABEL, 0),
            by_tier.get(MID_CAP_LABEL, 0),
            len(rows),
        )
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
    """LLM 추천에 사용할 당일 데이터를 종목별로 수집한다 (PRD 5.5-B 1단계).

    2단계로 나눠 조회한다 (확정 2026-08-05).
      1단계 — 유니버스 291종목의 기본정보(ka10001). 방향성 신호가 없는 종목은 여기서 버린다.
      2단계 — 규모별 시가갭 상위 후보(`SHORTLIST_BY_CAP`)에만 일봉(ka10086)을 붙여
              20일 평균 대비 거래량 급증 배수를 계산한다.
    전 종목에 2단계를 돌리면 호출이 두 배로 늘어 09:00 전에 끝나지 않는다.

    개별 종목 수집이 실패해도 전체 수집을 중단하지 않고 건너뛴다 — 일부 종목의
    일시적 조회 실패로 당일 추천 전체가 스킵되는 것을 방지한다.
    """

    def __init__(
        self,
        market_data: MarketDataClient,
        universe: LargeMidCapUniverse,
        news_client: NewsClient,
        request_interval: float = DETAIL_REQUEST_INTERVAL_SECONDS,
        shortlist_by_cap: Dict[str, int] = None,
    ):
        self.market_data = market_data
        self.universe = universe
        self.news_client = news_client
        self.request_interval = request_interval
        self.shortlist_by_cap = shortlist_by_cap or SHORTLIST_BY_CAP

    def collect(self) -> List[DailyStockData]:
        try:
            rows = self.universe.get_rows()
        except Exception:
            logger.exception("대형·중형주 유니버스 조회에 실패했습니다.")
            return []

        collected = self._collect_details(rows)

        # 등락률·시가갭이 모두 0인 종목은 근거로 쓸 수 없다 (DailyStockData.has_direction)
        candidates = [data for data in collected if data.has_direction]
        dropped = len(collected) - len(candidates)
        logger.info(
            "당일 데이터 수집 완료: %d/%d 종목 (방향성 신호 없어 제외 %d종목)",
            len(collected),
            len(rows),
            dropped,
        )

        shortlist = self._shortlist(candidates)
        self._fill_volume_surge(shortlist)
        self._log_candidates(shortlist)
        return shortlist

    def _collect_details(self, rows: List[dict]) -> List[DailyStockData]:
        """1단계 — 유니버스 전 종목의 기본정보를 모은다."""
        results: List[DailyStockData] = []
        for index, row in enumerate(rows):
            ticker = row["code"]
            try:
                results.append(
                    self._collect_one(
                        ticker,
                        row.get("name", ticker),
                        row.get("upSizeName", LARGE_CAP_LABEL),
                    )
                )
            except Exception:
                logger.exception("종목 데이터 수집 실패: %s", ticker)

            # 마지막 종목 뒤에는 대기하지 않는다
            if self.request_interval and index < len(rows) - 1:
                time.sleep(self.request_interval)
        return results

    def _shortlist(self, candidates: List[DailyStockData]) -> List[DailyStockData]:
        """규모별로 시가갭 상위 후보만 남긴다 — LLM에 넘길 목록이자 2단계 조회 대상.

        급등 예상 전략이므로 시가갭이 큰 쪽이 곧 후보군이다. 규모별로 따로 자르지 않으면
        갭이 큰 중형주가 대형주 자리를 전부 차지하거나 그 반대가 되어 쿼터를 채울 수 없다.
        """
        shortlist: List[DailyStockData] = []
        for tier, limit in self.shortlist_by_cap.items():
            in_tier = [data for data in candidates if data.cap_tier == tier]
            in_tier.sort(key=lambda data: data.gap_rate, reverse=True)
            shortlist.extend(in_tier[:limit])
            logger.info("%s 후보 %d종목 (신호 있는 %d종목 중)", tier, min(limit, len(in_tier)), len(in_tier))
        return shortlist

    def _fill_volume_surge(self, shortlist: List[DailyStockData]) -> None:
        """2단계 — 후보에만 20일 평균 대비 거래량 급증 배수를 채운다.

        조회에 실패한 종목은 0.0으로 남아 "평균 대비 판단 불가"로 프롬프트에 나간다.
        """
        for index, data in enumerate(shortlist):
            try:
                average = self.market_data.get_average_volume(data.ticker)
                if average > 0:
                    data.volume_surge = data.volume / average
            except Exception:
                logger.exception("평균 거래량 조회 실패: %s", data.ticker)

            if self.request_interval and index < len(shortlist) - 1:
                time.sleep(self.request_interval)

    def _log_candidates(self, shortlist: List[DailyStockData]) -> None:
        """LLM에 넘길 후보를 그대로 남긴다 — 나중에 추천이 타당했는지 되짚을 유일한 근거다."""
        logger.info("LLM 후보 %d종목 (규모 | 종목 | 등락률 | 시가갭 | 거래량 | 평균대비):", len(shortlist))
        for data in shortlist:
            surge = f"{data.volume_surge:.2f}배" if data.volume_surge else "판단불가"
            logger.info(
                "  %s | %s %s | %+.2f%% | %+.2f%% | %s | %s",
                data.cap_tier,
                data.ticker,
                data.name,
                data.change_rate,
                data.gap_rate,
                f"{data.volume:,}",
                surge,
            )

    def _collect_one(self, ticker: str, name: str, cap_tier: str) -> DailyStockData:
        detail = self.market_data.get_stock_detail(ticker)
        return DailyStockData(
            ticker=ticker,
            name=name,
            change_rate=detail.change_rate,
            volume=detail.volume,
            gap_rate=detail.gap_rate,
            headlines=self.news_client.get_headlines(ticker),
            is_premarket=detail.is_premarket,
            cap_tier=cap_tier,
        )
