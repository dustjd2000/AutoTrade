import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from config.settings import Settings

logger = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, settings: Settings):
        self.smtp_host = settings.smtp_host
        self.smtp_port = settings.smtp_port
        self.smtp_user = settings.smtp_user
        self.smtp_password = settings.smtp_password
        self.email_from = settings.email_from or settings.smtp_user
        self.email_to = settings.email_to
        self._enabled = bool(self.smtp_host and self.smtp_user and self.smtp_password and self.email_to)

    def send(self, subject: str, message: str, html: Optional[str] = None) -> None:
        """html을 함께 주면 multipart로 보낸다 — 표는 HTML로 보이고, 평문 클라이언트도 깨지지 않는다."""
        if not self._enabled:
            logger.debug("Email notifier disabled. Subject: %s, Message: %s", subject, message)
            return
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.email_from
            msg["To"] = self.email_to
            msg.set_content(message)
            if html:
                msg.add_alternative(html, subtype="html")

            # 465는 접속부터 SSL, 587은 평문 접속 후 STARTTLS로 승격한다 (Gmail 기본은 587)
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10) as server:
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
        except Exception as e:
            logger.error("Email send failed: %s", e)
