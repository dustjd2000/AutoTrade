import logging
from datetime import date, timedelta
from typing import Dict, Iterable, Iterator, List, Optional

import requests

logger = logging.getLogger(__name__)

# DART 전자공시 공시검색 (OPEN DART OpenAPI)
DISCLOSURE_URL = "https://opendart.fss.or.kr/api/list.json"
REQUEST_TIMEOUT_SECONDS = 10

# 유가증권시장(코스피)만 조회한다 — 유니버스가 코스피 대형주뿐이다
KOSPI_CORP_CLASS = "Y"
PAGE_SIZE = 100
# 페이지 상한 — total_page가 예상 밖의 값으로 와도 무한히 돌지 않게 한다.
# 코스피 이틀치 공시는 보통 열 페이지를 넘지 않는다.
MAX_PAGES = 30

STATUS_OK = "000"
STATUS_NO_DATA = "013"  # 조회된 데이터 없음 — 오류가 아니다 (공휴일 다음날 등)

# 후보에서 제외할 공시 — report_nm에 이 문자열이 있으면 그 종목을 뺀다 (PRD 5.5-B).
# 공백을 지우고 비교하므로 키워드에도 공백을 넣지 않는다 ("전환사채권 발행결정"처럼
# 띄어쓰기가 들쭉날쭉하게 오는 것을 흡수한다).
# '소송등의제기'는 일부러 넣지 않았다 — 대형주에는 일상적으로 뜨고 주가 영향이 없는 건이
# 많아, 멀쩡한 종목을 걸러낼 위험이 배제 이득보다 크다. 헤드라인으로만 넘겨 LLM이 감안한다.
BLOCKING_KEYWORDS = (
    "유상증자",
    "전환사채권발행",
    "신주인수권부사채권발행",
    "교환사채권발행",
    "횡령",
    "배임",
    "회계처리기준위반",
    "불성실공시법인",
)


class DisclosureAPIError(RuntimeError):
    """DART가 오류 상태를 반환했을 때 발생한다."""

    def __init__(self, status: str, message: str):
        self.status = status
        self.message = message
        super().__init__(f"DART status={status}: {message}")


def blocking_disclosure(titles: Iterable[str]) -> Optional[str]:
    """배제 대상 공시가 있으면 그 제목을, 없으면 None을 반환한다."""
    for title in titles:
        if any(keyword in "".join(title.split()) for keyword in BLOCKING_KEYWORDS):
            return title
    return None


def previous_business_day(today: date) -> date:
    """직전 영업일 — 토·일만 건너뛴다.

    공휴일은 반영하지 않는다. 휴장일이 끼면 그날 공시가 없어 조회 결과가 비는 것뿐이라,
    거래일 달력을 따로 두는 비용에 비해 잃는 것이 작다.
    """
    return today - timedelta(days={0: 3, 6: 2}.get(today.weekday(), 1))


class DisclosureClient:
    """DART 전자공시에서 직전 거래일·당일 공시를 받아 종목별로 묶는다 (PRD 5.5-B).

    종목당 조회가 아니라 **날짜 범위로 코스피 공시를 통째로** 받는다 — 응답에 `stock_code`가
    들어 있어 고유번호(corp_code) 매핑 파일이 필요 없고, 호출 수도 종목 수와 무관하다.

    기간을 하루가 아니라 이틀로 잡는 것은 `rcept_dt`가 날짜 단위(시각 없음)라 "전일 장 마감
    이후"를 시각으로 골라낼 수 없기 때문이다.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    def fetch(
        self,
        tickers: Iterable[str],
        today: Optional[date] = None,
        since: Optional[date] = None,
    ) -> Dict[str, List[str]]:
        """종목코드 → 공시 제목 목록(최신순).

        `since`는 조회 시작일이며 기본은 직전 영업일이다 — 아침 추천은 "전일 장 마감 이후"를
        보려고 이틀을 받는다. 장중 감시(`DisclosureWatch`)는 당일만 필요해 `since=today`를
        넘긴다. 범위를 좁히면 받아 넘길 페이지 수도 함께 줄어든다.

        배제 대상 공시도 걸러내지 않고 그대로 담아 돌려준다 — 판정은 `blocking_disclosure`가
        하고, 헤드라인을 몇 건만 싣는 것은 호출부가 정한다. 여기서 미리 잘라내면 4번째 공시가
        악재일 때 배제를 놓친다.

        키가 없으면 빈 딕셔너리를 반환하고, 조회가 실패하면 예외를 그대로 올린다
        (호출부가 알림을 보낸다).
        """
        if not self.api_key:
            logger.info("DART_API_KEY가 없어 공시 수집을 건너뜁니다.")
            return {}

        today = today or date.today()
        wanted = set(tickers)
        grouped: Dict[str, List[str]] = {}
        total = 0
        for item in self._iter_disclosures(since or previous_business_day(today), today):
            total += 1
            ticker = str(item.get("stock_code", "")).strip()
            title = str(item.get("report_nm", "")).strip()
            if ticker in wanted and title:
                grouped.setdefault(ticker, []).append(title)

        logger.info("DART 공시 %d건 조회 — 유니버스 %d종목에 걸렸습니다.", total, len(grouped))
        return grouped

    def _iter_disclosures(self, bgn_de: date, end_de: date) -> Iterator[dict]:
        for page_no in range(1, MAX_PAGES + 1):
            payload = self._get(bgn_de, end_de, page_no)
            status = str(payload.get("status", ""))
            if status == STATUS_NO_DATA:
                return
            if status != STATUS_OK:
                raise DisclosureAPIError(status, str(payload.get("message", "")))

            yield from payload.get("list") or []

            if page_no >= int(payload.get("total_page", 1) or 1):
                return

        logger.warning("DART 공시가 %d페이지를 넘어 이후 페이지를 버립니다.", MAX_PAGES)

    def _get(self, bgn_de: date, end_de: date, page_no: int) -> dict:
        response = requests.get(
            DISCLOSURE_URL,
            params={
                "crtfc_key": self.api_key,
                "corp_cls": KOSPI_CORP_CLASS,
                "bgn_de": bgn_de.strftime("%Y%m%d"),
                "end_de": end_de.strftime("%Y%m%d"),
                "page_no": page_no,
                "page_count": PAGE_SIZE,
                # 최신순으로 받아야 호출부가 앞에서부터 잘라 최근 공시를 남길 수 있다
                "sort": "date",
                "sort_mth": "desc",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
