import logging
from typing import Any, Dict, Optional, Tuple

import requests

from config.settings import Settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10


class KiwoomAPIError(RuntimeError):
    """키움 API가 오류를 반환했을 때 발생한다."""

    def __init__(self, api_id: str, return_code: Any, return_msg: str):
        self.api_id = api_id
        self.return_code = return_code
        self.return_msg = return_msg
        super().__init__(f"[{api_id}] return_code={return_code}: {return_msg}")


class KiwoomClient:
    """키움 REST API 공통 호출부.

    모든 TR 요청이 같은 헤더 규약(authorization / api-id)과 오류 판정을 쓰도록 한 곳에 모은다.
    외부 API 스펙 변경 시 이 파일만 고치면 되도록 감싸는 것이 목적이다 (PRD 8절 API 변경 리스크).
    """

    def __init__(self, settings: Settings, auth):
        self.settings = settings
        self.auth = auth

    def request(
        self,
        path: str,
        api_id: str,
        body: Optional[Dict[str, Any]] = None,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """TR을 호출하고 (응답 본문, 응답 헤더)를 반환한다.

        연속조회가 필요한 TR은 응답 헤더의 cont-yn / next-key를 그대로 다시 넘겨 호출한다.
        """
        url = f"{self.settings.api_base_url}{path}"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.auth.ensure_token()}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }

        response = requests.post(
            url, headers=headers, json=body or {}, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        data = response.json()

        # 키움은 HTTP 200이어도 본문의 return_code로 실패를 알린다 (0 = 정상)
        return_code = data.get("return_code")
        if return_code not in (0, None):
            raise KiwoomAPIError(api_id, return_code, data.get("return_msg", ""))

        return data, dict(response.headers)


def to_int(value: Any) -> int:
    """키움 응답의 수치 문자열을 int로 변환한다.

    부호가 '+000012345' / '-000012345' 처럼 붙어 오거나 빈 문자열인 경우가 있어 방어한다.
    """
    if value is None:
        return 0
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "+"):
        return 0
    try:
        return int(text)
    except ValueError:
        return int(float(text))


def to_float(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "+"):
        return 0.0
    return float(text)
