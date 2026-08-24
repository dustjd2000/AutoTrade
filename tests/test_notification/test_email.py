from config.settings import Settings
from src.notification.email import EmailNotifier


def make_notifier():
    settings = Settings()
    settings.smtp_host = "smtp.example.com"
    settings.smtp_user = "me@example.com"
    settings.smtp_password = "secret"
    settings.email_to = "me@example.com"
    return EmailNotifier(settings)


def test_inline_image_rides_along_with_the_html_part():
    """이미지는 HTML 파트에 related로 붙어야 cid: 참조가 열린다."""
    msg = make_notifier()._compose(
        "제목", "평문", "<div><img src=\"cid:chart\"></div>", {"chart": b"\x89PNG\r\n\x1a\nfake"}
    )

    related = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(related) == 1
    assert related[0]["Content-ID"] == "<chart>"
    assert related[0].get_payload(decode=True) == b"\x89PNG\r\n\x1a\nfake"


def test_message_without_images_stays_two_parts():
    msg = make_notifier()._compose("제목", "평문", "<div>표</div>", None)

    assert [p.get_content_type() for p in msg.walk() if not p.is_multipart()] == [
        "text/plain",
        "text/html",
    ]
