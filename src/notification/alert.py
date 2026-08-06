import logging
import re
from datetime import date
from typing import Callable, Optional, Set

from src.notification.email import EmailNotifier

logger = logging.getLogger(__name__)

ALERT_SUBJECT = "[AutoTrade] 운영 알림"

# 중복 판정 키를 만들 때 지워낼 부분 — 숫자와 천단위 구분자.
# 같은 장애가 반복돼도 가격·수량·손익이 조금씩 달라 문자열이 매번 바뀐다.
# (2026-08-06: 같은 손절 실패 알림이 30통 나갔는데 문장이 완전히 같은 것은 24통뿐이었다.)
_VARYING_NUMBERS = re.compile(r"[\d,]+")


def dedupe_key(message: str) -> str:
    """알림 본문에서 숫자를 지워 '같은 장애'인지 판정할 키를 만든다.

    종목명·사유·오류 메시지는 그대로 남으므로, 종목이나 원인이 다르면 키도 달라진다.
    """
    return _VARYING_NUMBERS.sub("#", message)


class AlertNotifier:
    """운영 알림(주문 실패, 손실 한도 도달, 장애, 강제청산 등)을 이메일로 발송한다 (PRD 5.8).

    엔진은 `send(message)` 한 가지만 알면 되도록 이메일의 제목/본문 구조를 감춘다.

    **같은 장애는 거래일당 한 번만 보낸다** (확정 2026-08-06). 알림 상당수가 실시간 시세
    콜백에서 나오는데, 장애가 몇 분만 이어져도 틱마다 같은 메일이 수십 통 쌓인다.
    억제된 알림도 로그에는 그대로 남으므로 사후 추적에는 공백이 생기지 않는다.
    """

    def __init__(self, email: EmailNotifier, today: Callable[[], date] = date.today):
        self._email = email
        self._today = today
        self._day: Optional[date] = None
        self._sent: Set[str] = set()

    def send(self, message: str) -> None:
        today = self._today()
        if self._day != today:
            # 날짜가 바뀌면 억제 이력을 비운다 — 어제 한 번 본 장애라고 오늘까지 묻어두지 않는다
            self._day = today
            self._sent.clear()

        key = dedupe_key(message)
        if key in self._sent:
            logger.info("같은 내용의 알림을 오늘 이미 보냈습니다 — 메일을 건너뜁니다: %s", message)
            return

        self._sent.add(key)
        self._email.send(ALERT_SUBJECT, message)
