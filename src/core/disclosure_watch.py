import logging
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.data.disclosure import DisclosureClient, blocking_disclosure

logger = logging.getLogger(__name__)

# AI 매도 판단 프롬프트에 실을 종목당 공시 제목 수 — 수집 쪽(MAX_HEADLINES_PER_TICKER)과
# 따로 둔다. 장중에는 "아침에 없던 것"이 핵심이라 신규 공시를 먼저 싣고 남는 자리를 채운다.
MAX_HEADLINES_PER_TICKER = 5

# 하루에 허용할 즉시 판단 트리거 수. 새로 뜬 악재 공시 하나가 한 번만 트리거하므로
# (같은 제목은 두 번 '신규'가 되지 않는다) 실제로는 0~1건이지만, 공시가 쏟아지는 날에
# LLM 호출이 상한 밖에서 늘어나지 않도록 못을 박아 둔다.
MAX_URGENT_TRIGGERS_PER_DAY = 5


class DisclosureWatch:
    """장중에 새로 뜬 공시를 가려내 AI 매도 판단에 넘긴다 (PRD 5.5-B '장중 공시').

    DART `list.json`의 `rcept_dt`는 **날짜 단위**라 "몇 시에 떴는지"를 알 수 없다. 그래서
    시각으로 고르지 않고, 처음 조회한 목록을 기준선으로 삼아 **그 뒤에 새로 나타난 제목**을
    장중 신규 공시로 본다. 엔진을 장중에 켠 날은 그 시점이 기준선이 되므로 그 전에 뜬
    공시는 '신규'로 잡히지 않는다 — 대신 당일 공시 전체를 그대로 들고 있어 프롬프트에는
    실린다.

    새로 뜬 공시가 배제 키워드(`BLOCKING_KEYWORDS`)에 걸리면 호출 주기를 기다리지 않고
    AI 판단을 한 번 앞당긴다(`urgent_pending`). 여기서 곧바로 매도하지는 않는다 — 판정
    단위가 보유 목록 전체라, 키워드 하나로 멀쩡한 다른 종목까지 던지게 된다.
    """

    def __init__(self, client: DisclosureClient):
        self._client = client
        self._date: Optional[date] = None
        # 종목코드 → 오늘 공시 제목 (최신순). 조회할 때마다 통째로 갈아 끼운다.
        self._today: Dict[str, List[str]] = {}
        # 지금까지 한 번이라도 본 제목 — '이번에 처음 나타난 것'을 가려내는 데 쓴다
        self._seen: Dict[str, Set[str]] = {}
        # 첫 조회 시점의 `_seen` 사본. 이 뒤에 붙은 제목이 '장중 신규'다
        self._baseline: Dict[str, Set[str]] = {}
        self._primed = False
        self._urgent_pending = False
        self._urgent_triggers = 0

    # ── 조회 ────────────────────────────────────────────────
    def poll(self, tickers: Iterable[str], now: datetime) -> List[Tuple[str, str]]:
        """당일 공시를 다시 받아 캐시를 갱신하고, 새로 뜬 악재 공시를 돌려준다.

        반환값은 `(종목코드, 공시 제목)` 목록이며, 하나라도 있으면 `urgent_pending`이 선다.
        조회 실패는 그대로 올린다 — 호출측(`runtime.maybe_poll_disclosures`)이 그 주기를
        건너뛴다.
        """
        self._roll_over(now.date())

        wanted = list(tickers)
        fetched = self._client.fetch(wanted, today=now.date(), since=now.date())

        triggered: List[Tuple[str, str]] = []
        for ticker in wanted:
            titles = fetched.get(ticker, [])
            self._today[ticker] = list(titles)
            seen = self._seen.setdefault(ticker, set())
            appeared = [title for title in titles if title not in seen]
            seen.update(titles)
            if not self._primed:
                continue
            blocking = blocking_disclosure(appeared)
            if blocking:
                triggered.append((ticker, blocking))

        if not self._primed:
            # 첫 조회는 기준선을 잡을 뿐이다 — 그 시점에 이미 있던 공시는 '장중 신규'가 아니다
            self._baseline = {ticker: set(titles) for ticker, titles in self._seen.items()}
            self._primed = True
            logger.info("장중 공시 감시 기준선을 잡았습니다 — %d종목.", len(self._baseline))
            return []

        if triggered and self._urgent_triggers < MAX_URGENT_TRIGGERS_PER_DAY:
            self._urgent_triggers += 1
            self._urgent_pending = True
        elif triggered:
            logger.warning(
                "악재 공시가 떴지만 즉시 판단 트리거가 하루 상한(%d회)에 걸려 다음 정규 주기로 미룹니다.",
                MAX_URGENT_TRIGGERS_PER_DAY,
            )
        return triggered

    def _roll_over(self, today: date) -> None:
        """날짜가 바뀌면 전부 비운다 — 어제 공시를 오늘 기준선으로 쓰면 안 된다."""
        if self._date == today:
            return
        self._date = today
        self._today = {}
        self._seen = {}
        self._baseline = {}
        self._primed = False
        self._urgent_pending = False
        self._urgent_triggers = 0

    # ── 프롬프트 재료 ────────────────────────────────────────
    def headlines_for(self, ticker: str) -> List[str]:
        """오늘 공시 제목 (최신순). 신규 공시를 앞에 세워 잘릴 때 먼저 남게 한다."""
        titles = self._today.get(ticker, [])
        new = self.new_headlines_for(ticker)
        rest = [title for title in titles if title not in set(new)]
        return (new + rest)[:MAX_HEADLINES_PER_TICKER]

    def new_headlines_for(self, ticker: str) -> List[str]:
        """기준선 이후 새로 나타난 제목 — 장중에 뜬 공시다."""
        baseline = self._baseline.get(ticker, set())
        return [title for title in self._today.get(ticker, []) if title not in baseline]

    # ── 즉시 판단 트리거 ─────────────────────────────────────
    @property
    def urgent_pending(self) -> bool:
        """다음 사이클에서 호출 주기를 건너뛰어야 하는지."""
        return self._urgent_pending

    def take_urgent(self) -> bool:
        """트리거를 소비한다 — AI 판단 사이클이 실제로 돌 때 한 번 부른다."""
        pending = self._urgent_pending
        self._urgent_pending = False
        return pending
