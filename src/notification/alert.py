import logging

from src.notification.email import EmailNotifier

logger = logging.getLogger(__name__)

ALERT_SUBJECT = "[AutoTrade] 운영 알림"


class AlertNotifier:
    """운영 알림(주문 실패, 손실 한도 도달, 장애, 강제청산 등)을 이메일로 발송한다 (PRD 5.8).

    엔진은 `send(message)` 한 가지만 알면 되도록 이메일의 제목/본문 구조를 감춘다.
    """

    def __init__(self, email: EmailNotifier):
        self._email = email

    def send(self, message: str) -> None:
        self._email.send(ALERT_SUBJECT, message)
