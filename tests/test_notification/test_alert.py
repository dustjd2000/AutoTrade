import threading
from datetime import date

from src.notification.alert import ALERT_SUBJECT, AlertNotifier, dedupe_key


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None):
        self.sent.append((subject, message))


class BlockingEmail:
    """첫 발송이 release될 때까지 SMTP 안에 머무는 이메일 — 발송 지연을 흉내낸다."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent = []

    def send(self, subject, message, html=None):
        self.sent.append((subject, message))
        if len(self.sent) == 1:
            self.entered.set()
            self.release.wait(timeout=5)


def make_notifier(today=date(2026, 8, 6)):
    email = FakeEmail()
    days = [today]
    notifier = AlertNotifier(email, today=lambda: days[0])
    return notifier, email, days


def test_alert_is_sent_with_operation_subject():
    notifier, email, _ = make_notifier()

    notifier.send("[실패] 매수 거부: (005930)삼성전자")

    assert email.sent == [(ALERT_SUBJECT, "[실패] 매수 거부: (005930)삼성전자")]


def test_identical_alert_is_sent_only_once():
    notifier, email, _ = make_notifier()

    for _ in range(5):
        notifier.send("[경고] 실시간 시세 미연결")

    assert len(email.sent) == 1


def test_alerts_differing_only_in_numbers_are_treated_as_the_same():
    """08-06 손절 실패 30통이 이 경우였다 — 평가손익만 달라 문장이 매번 바뀌었다."""
    notifier, email, _ = make_notifier()

    notifier.send("청산 주문 거부됨 (stop_loss): (079900)전진건설로봇 18주, 평가손익 -17,100원")
    notifier.send("청산 주문 거부됨 (stop_loss): (079900)전진건설로봇 18주, 평가손익 -18,000원")

    assert len(email.sent) == 1


def test_different_stock_is_a_different_alert():
    notifier, email, _ = make_notifier()

    notifier.send("청산 주문 거부됨 (stop_loss): (079900)전진건설로봇 18주")
    notifier.send("청산 주문 거부됨 (stop_loss): (454910)두산로보틱스 8주")

    assert len(email.sent) == 2


def test_different_reason_is_a_different_alert():
    notifier, email, _ = make_notifier()

    notifier.send("[실패] 매수 거부: (005930)삼성전자 — CB 발동중입니다")
    notifier.send("[실패] 매수 거부: (005930)삼성전자 — 주문가능금액이 부족합니다")

    assert len(email.sent) == 2


def test_suppression_resets_on_a_new_day():
    """어제 한 번 본 장애라고 오늘까지 묻어두면 안 된다."""
    notifier, email, days = make_notifier()

    notifier.send("[경고] 실시간 시세 미연결")
    days[0] = date(2026, 8, 7)
    notifier.send("[경고] 실시간 시세 미연결")

    assert len(email.sent) == 2


def test_slow_email_send_does_not_block_a_different_alert():
    """중복 판정은 직렬화하되 SMTP 발송은 락 밖이어야 한다.

    알림은 루프 스레드와 `_off_loop` 스레드 양쪽에서 나온다. SMTP는 몇 초가 걸리는데
    그 사이 다른 스레드의 알림까지 막히면, 정작 급한 장애 알림이 뒤로 밀린다.
    """
    email = BlockingEmail()
    notifier = AlertNotifier(email, today=lambda: date(2026, 8, 6))

    blocked = threading.Thread(target=notifier.send, args=("[경고] 실시간 시세 미연결",))
    blocked.start()
    assert email.entered.wait(timeout=5), "첫 알림이 SMTP까지 도달하지 못했습니다."

    delivered = threading.Event()

    def send_other():
        notifier.send("[실패] 손절 매도 실패: (079900)전진건설로봇")
        delivered.set()

    threading.Thread(target=send_other).start()

    assert delivered.wait(timeout=5), "SMTP 발송이 락을 쥐고 있어 다른 알림이 막혔습니다."
    email.release.set()
    blocked.join(timeout=5)


def test_dedupe_key_keeps_text_and_drops_numbers():
    assert dedupe_key("평단 35,200원 → 현재가 34,250원") == dedupe_key("평단 1원 → 현재가 2원")
    assert dedupe_key("(079900)전진건설로봇") != dedupe_key("(454910)두산로보틱스")
