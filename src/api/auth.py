import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from config.settings import Settings

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_BUFFER_SECONDS = 300  # 만료 5분 전 갱신
TOKEN_PATH = "/oauth2/token"
REQUEST_TIMEOUT_SECONDS = 10

# 키움 응답의 expires_dt 형식 (예: "20261231235959")
EXPIRES_DT_FORMAT = "%Y%m%d%H%M%S"


class AuthClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    def ensure_token(self) -> str:
        if self._is_token_valid():
            return self._token
        return self._issue_token()

    def _is_token_valid(self) -> bool:
        if not self._token or not self._token_expires_at:
            return False
        return datetime.now() < self._token_expires_at - timedelta(seconds=TOKEN_EXPIRY_BUFFER_SECONDS)

    def _issue_token(self) -> str:
        logger.info("Issuing new access token...")
        response = requests.post(
            f"{self.settings.api_base_url}{TOKEN_PATH}",
            headers={"Content-Type": "application/json;charset=UTF-8"},
            json={
                "grant_type": "client_credentials",
                "appkey": self.settings.app_key,
                "secretkey": self.settings.app_secret,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        return_code = data.get("return_code")
        if return_code not in (0, None):
            raise RuntimeError(
                f"토큰 발급 실패 (return_code={return_code}): {data.get('return_msg', '')}"
            )

        token = data.get("token")
        if not token:
            raise RuntimeError(f"토큰 발급 응답에 token 필드가 없습니다: {data}")

        self._token = token
        self._token_expires_at = self._parse_expiry(data.get("expires_dt"))
        logger.info("Access token issued. Expires at: %s", self._token_expires_at)
        return self._token

    def _parse_expiry(self, expires_dt: Optional[str]) -> datetime:
        """만료 시각을 파싱한다. 형식이 예상과 다르면 보수적으로 1시간 뒤로 잡는다."""
        if expires_dt:
            try:
                return datetime.strptime(str(expires_dt), EXPIRES_DT_FORMAT)
            except ValueError:
                logger.warning("expires_dt 형식을 해석하지 못했습니다: %s", expires_dt)
        return datetime.now() + timedelta(hours=1)

    @property
    def token(self) -> str:
        return self.ensure_token()
