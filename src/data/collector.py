import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from src.api.client import KiwoomClient
from src.api.market_data import MarketDataClient
from src.data.disclosure import DisclosureClient, blocking_disclosure

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

# 당일 현재가(ka10001)를 조회할 종목 수 — 전일 급증 배수 상위부터 자른다.
# 최종 후보가 25종목이라 40이면 당일 필터로 빠진 자리를 채우고도 남고, 유니버스 98종목
# 전부에 돌리면 20초가 더 드는데 그중 3분의 2는 어차피 후보에 못 든다 (PRD 5.5-B).
PRESCREEN_SIZE = 40

# 당일 필터를 통과한 종목이 이 수보다 적으면 필터를 통째로 걷는다. 지수가 함께 갭 하락한
# 날에 후보가 비어 추천이 스킵되는 것을 막는 하한선이다 — 2026-08-06에 필터 하나로
# 후보 0종목 → 매수 0건이 된 실패를 되풀이하지 않기 위함이다 (PRD 5.5-B).
MIN_CANDIDATES_AFTER_FILTER = 5

# 프롬프트에 실을 종목당 공시 제목 수 (최신순). 정기보고서까지 다 실으면 후보 25종목만으로도
# 프롬프트가 길어지고, 정작 중요한 재료가 묻힌다.
MAX_HEADLINES_PER_TICKER = 3


@dataclass
class DailyStockData:
    """1호 전략의 LLM 프롬프트에 쓰이는 종목별 데이터 (PRD 5.5-B).

    뼈대는 **전일** 일봉이고, 여기에 추천 시각(09:05)의 **당일** 지표가 더해진다
    (2026-08-14). 2026-08-06~08-13에는 추천이 장 전이라 당일 지표가 전 종목 0으로
    돌아와 근거가 되지 못했다 (PRD 10절 '장 전 당일 지표 부재') — 그 제약은 추천을
    개장 후로 옮기면서 사정권을 벗어났다 (PRD 10절 '개장 후 추천으로 이동').
    """

    ticker: str
    name: str
    prev_close: float        # 전일 종가 (목표 매수가 산정의 기준값)
    prev_high: float         # 전일 고가
    prev_low: float          # 전일 저가
    prev_change_rate: float  # 전일 등락률 (%)
    prev_volume: int         # 전일 거래량
    volume_surge: float      # 전일 거래량 ÷ 그 이전 거래일 평균 (0이면 산출 불가)
    # 최근 가격대 — 전일 종가만으로는 지금이 비싼지 싼지 알 수 없어 목표 매수가의 기준이 된다
    recent_high: float = 0.0      # 당일 제외 최근 거래일 중 최고가
    recent_low: float = 0.0       # 당일 제외 최근 거래일 중 최저가
    moving_average: float = 0.0   # 당일 제외 최근 거래일 종가 평균
    # 전일 변동폭 (%) = (전일 고가 − 전일 저가) ÷ 전일 종가 × 100 (확정 2026-08-14).
    # 후보를 거르는 데는 쓰지 않고 프롬프트에만 싣는다 — 2026-08-14 실측 분포에서 변동폭이
    # 큰 종목이 곧 손실 종목이 아니었다 (PRD 5.5-B '당일 지표 병행 수집'의 변동폭 항목).
    prev_range_pct: float = 0.0
    # 당일 지표 — 추천 시각(09:05)에 현재가로 계산한다. 0은 '산출 안 됨'이며(급증 배수와
    # 같은 규약), 조회에 실패한 종목이 여기 해당한다 (PRD 5.5-B '당일 지표 병행 수집').
    today_price: float = 0.0        # 추천 시각 현재가
    today_change_rate: float = 0.0  # (현재가 − 전일 종가) ÷ 전일 종가 × 100
    today_volume: int = 0           # 추천 시각까지의 당일 누적 거래량
    # DART 공시 제목 (최신순, 최대 MAX_HEADLINES_PER_TICKER건) — 수집은 DisclosureClient가 한다
    headlines: List[str] = field(default_factory=list)


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


class DataCollector:
    """LLM 추천에 사용할 전일 데이터를 종목별로 수집한다 (PRD 5.5-B).

    유니버스 전 종목에 일봉(ka10086)을 한 번씩 조회해 뼈대를 만든다 — 한 호출이 20거래일치를
    돌려주므로 전일 종가·고가·저가·등락률·거래량과 급증 배수가 모두 여기서 나온다.
    그 다음 급증 배수 상위 PRESCREEN_SIZE 종목에만 현재가(ka10001)를 조회해 당일 등락률을
    계산하고, 당일 하락 출발한 종목을 쇼트리스트 이전에 뺀다 (2026-08-14, 추천이 09:05로
    옮겨지며 추가됨 — 공시 배제와 같은 자리다).

    여기에 DART 공시를 하루치 일괄로 덧붙인다 — 악재 공시가 뜬 종목을 후보에서 빼고, 나머지
    공시 제목은 프롬프트에 싣는다. 배제를 쇼트리스트보다 **앞에** 두는 이유는 뒤에 두면 후보가
    그만큼 줄어들기 때문이다 (앞에 두면 빈자리를 다음 후보가 채운다).

    개별 종목 수집이 실패해도 전체 수집을 중단하지 않고 건너뛴다 — 일부 종목의
    일시적 조회 실패로 당일 추천 전체가 스킵되는 것을 방지한다.
    """

    def __init__(
        self,
        market_data: MarketDataClient,
        universe: LargeCapUniverse,
        disclosures: DisclosureClient,
        request_interval: float = DETAIL_REQUEST_INTERVAL_SECONDS,
        shortlist_size: int = SHORTLIST_SIZE,
        prescreen_size: int = PRESCREEN_SIZE,
        gap_down_tolerance_ratio: float = 0.0,
        min_candidates_after_filter: int = MIN_CANDIDATES_AFTER_FILTER,
        notify: Optional[Callable[[str], None]] = None,
    ):
        self.market_data = market_data
        self.universe = universe
        self.disclosures = disclosures
        self.request_interval = request_interval
        self.shortlist_size = shortlist_size
        self.prescreen_size = prescreen_size
        self.gap_down_tolerance_ratio = gap_down_tolerance_ratio
        self.min_candidates_after_filter = min_candidates_after_filter
        self.notify = notify

    def collect(self) -> List[DailyStockData]:
        try:
            rows = self.universe.get_rows()
        except Exception:
            logger.exception("대형주 유니버스 조회에 실패했습니다.")
            return []

        collected = self._collect_previous_day(rows)
        logger.info("전일 데이터 수집 완료: %d/%d 종목", len(collected), len(rows))

        candidates = self._apply_disclosures(collected)
        prescreened = self._prescreen(candidates)
        self._collect_today(prescreened)
        shortlist = self._shortlist(self._filter_by_today(prescreened))
        self._log_candidates(shortlist)
        return shortlist

    def _apply_disclosures(self, candidates: List[DailyStockData]) -> List[DailyStockData]:
        """악재 공시가 뜬 종목을 빼고, 나머지 종목에 공시 제목을 붙인다 (PRD 5.5-B).

        조회가 실패하면 후보를 그대로 돌려준다 — 공시 하나 때문에 그날 추천 전체가 멈추는
        편보다 낫다. 대신 배제가 빠진 채로 돌았다는 사실을 알림으로 알린다.
        """
        if not candidates:
            # 전일 지표 수집이 통째로 실패한 날 — 붙일 곳이 없는데 코스피 공시를 다 받아올 필요가 없다
            return candidates

        try:
            by_ticker = self.disclosures.fetch([data.ticker for data in candidates])
        except Exception:
            logger.exception("DART 공시 조회에 실패했습니다. 공시 없이 진행합니다.")
            if self.notify:
                self.notify("[경고] DART 공시 조회 실패 — 악재 공시 배제 없이 추천을 진행합니다.")
            return candidates

        remaining: List[DailyStockData] = []
        for data in candidates:
            titles = by_ticker.get(data.ticker, [])
            blocking = blocking_disclosure(titles)
            if blocking:
                logger.info("악재 공시로 후보 제외: %s %s | %s", data.ticker, data.name, blocking)
                continue
            data.headlines = titles[:MAX_HEADLINES_PER_TICKER]
            remaining.append(data)
        return remaining

    def _prescreen(self, candidates: List[DailyStockData]) -> List[DailyStockData]:
        """당일 현재가를 조회할 종목만 급증 배수 상위로 미리 자른다 (PRD 5.5-B).

        최종 쇼트리스트(25종목)보다 넉넉히 잡아, 당일 필터로 빠진 자리를 다음 후보가 채우게 한다.
        """
        by_surge = sorted(candidates, key=lambda data: data.volume_surge, reverse=True)
        return by_surge[: self.prescreen_size]

    def _collect_today(self, candidates: List[DailyStockData]) -> None:
        """추천 시각의 현재가로 당일 지표를 채운다 (제자리 수정, PRD 5.5-B).

        키움이 주는 등락률·시가갭 필드를 쓰지 않고 현재가에서 직접 계산한다 — 2026-08-06에
        무너진 것은 그 필드였지 가격 자체가 아니었다 (PRD 10절 '장 전 당일 지표 부재').

        조회에 실패한 종목은 당일 지표를 0으로 남긴다. 0은 '산출 안 됨'이고, 그 종목은
        아래 필터를 그대로 통과한다 — 일시적 조회 실패로 멀쩡한 종목을 잃지 않기 위함이다.
        """
        for index, data in enumerate(candidates):
            # 마지막 뒤가 아니라 '첫 요청 앞'에서만 건너뛴다 — 조회가 실패해 다음으로
            # 넘어가는 경우에도 요청 간격이 유지되어야 유량 제한에 걸리지 않는다
            if self.request_interval and index > 0:
                time.sleep(self.request_interval)
            try:
                quote = self.market_data.get_current_price(data.ticker)
            except Exception:
                logger.warning(
                    "당일 현재가 조회 실패 — 당일 지표 없이 진행합니다: %s %s",
                    data.ticker,
                    data.name,
                )
                continue
            if quote.price <= 0 or data.prev_close <= 0:
                continue
            data.today_price = quote.price
            data.today_volume = quote.volume
            data.today_change_rate = (
                (quote.price - data.prev_close) / data.prev_close * 100
            )

    def _filter_by_today(self, candidates: List[DailyStockData]) -> List[DailyStockData]:
        """당일 하락 출발한 종목을 후보에서 뺀다 (PRD 5.5-B '당일 지표 병행 수집').

        '전일 강세가 오늘 이어진다'는 전략 전제가 이미 깨졌는지를 본다. 임계값은 주문 직전
        갭 하락 판정과 같은 `GAP_DOWN_TOLERANCE_PERCENT`를 재사용한다 — 판정 내용이 같아
        설정을 둘로 나눌 이유가 없다. 허용치 0은 '끔'이다.
        """
        if self.gap_down_tolerance_ratio <= 0:
            return candidates

        floor = -self.gap_down_tolerance_ratio * 100
        kept, dropped = [], []
        for data in candidates:
            # 조회 실패(0)는 떨어뜨리지 않는다 — '모름'이지 '하락'이 아니다
            if data.today_price <= 0 or data.today_change_rate >= floor:
                kept.append(data)
            else:
                dropped.append(data)

        if dropped:
            logger.info(
                "당일 하락 출발로 후보 제외 %d종목: %s",
                len(dropped),
                ", ".join(
                    f"{d.ticker} {d.name} {d.today_change_rate:+.2f}%" for d in dropped
                ),
            )

        if len(kept) < self.min_candidates_after_filter:
            logger.warning(
                "당일 필터 통과 %d종목 — 하한 %d종목 미만이라 필터를 걷고 진행합니다.",
                len(kept),
                self.min_candidates_after_filter,
            )
            if self.notify:
                self.notify(
                    f"[경고] 당일 하락 출발 필터 통과 {len(kept)}종목 — "
                    "필터를 해제하고 추천을 진행합니다."
                )
            return candidates
        return kept

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
            "LLM 후보 %d종목 (종목 | 전일 등락률 | 당일 등락률 | 전일 거래량 | 평균대비 | "
            "전일 변동폭 | 전일 종가):",
            len(shortlist),
        )
        for data in shortlist:
            surge = f"{data.volume_surge:.2f}배" if data.volume_surge else "판단불가"
            today = (
                f"{data.today_change_rate:+.2f}%" if data.today_price > 0 else "조회실패"
            )
            logger.info(
                "  %s %s | %+.2f%% | %s | %s | %s | %.2f%% | %s원",
                data.ticker,
                data.name,
                data.prev_change_rate,
                today,
                f"{data.prev_volume:,}",
                surge,
                data.prev_range_pct,
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
            recent_high=metrics.recent_high,
            recent_low=metrics.recent_low,
            moving_average=metrics.moving_average,
            prev_range_pct=(
                (metrics.high - metrics.low) / metrics.close * 100
                if metrics.close > 0 and metrics.high > 0 and metrics.low > 0
                else 0.0
            ),
        )
