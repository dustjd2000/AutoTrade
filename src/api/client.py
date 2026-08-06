import logging
from typing import Any, Dict, Optional, Tuple

import requests

from config.settings import Settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10

# 키움이 인증 실패를 알리는 return_code. 토큰이 만료됐거나, 다른 프로세스가 같은 앱키로
# 토큰을 발급해 이쪽 토큰이 무효화된 경우 모두 여기로 온다 (PRD 10절 '토큰 무효화').
# 다만 이 코드가 인증 전용이라고 확인된 것은 아니어서 메시지로 한 번 더 가린다 — 주문 거부
# 같은 업무 오류까지 재시도하면 같은 주문이 두 번 나갈 수 있다.
AUTH_ERROR_RETURN_CODE = 3
AUTH_ERROR_KEYWORDS = ("Token", "토큰", "인증")


def is_auth_error(return_code: Any, return_msg: Any) -> bool:
    """토큰을 다시 발급받아야 하는 오류인지."""
    return return_code == AUTH_ERROR_RETURN_CODE and any(
        keyword in str(return_msg) for keyword in AUTH_ERROR_KEYWORDS
    )


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

        인증 오류(`AUTH_ERROR_RETURN_CODE`)는 토큰을 강제로 재발급해 **한 번만** 재시도한다.
        만료 시각만 보는 `AuthClient._is_token_valid`로는 '서버가 이미 죽였다고 판정한 토큰'을
        가려낼 수 없어, 이 경로가 없으면 죽은 토큰으로 계속 호출한다 — 2026-08-06에 손절
        청산이 22분간 거부되면서 드러났다 (PRD 10절 '토큰 무효화').
        """
        url = f"{self.settings.api_base_url}{path}"
        token = self.auth.ensure_token()
        data, response_headers = self._post(url, token, api_id, body, cont_yn, next_key)

        # 키움은 HTTP 200이어도 본문의 return_code로 실패를 알린다 (0 = 정상)
        return_code = data.get("return_code")
        if is_auth_error(return_code, data.get("return_msg", "")):
            logger.warning(
                "인증 오류로 토큰을 재발급하고 다시 시도합니다 (%s): %s",
                api_id,
                data.get("return_msg", ""),
            )
            token = self.auth.refresh_token(rejected=token)
            data, response_headers = self._post(url, token, api_id, body, cont_yn, next_key)
            return_code = data.get("return_code")

        if return_code not in (0, None):
            raise KiwoomAPIError(api_id, return_code, data.get("return_msg", ""))

        return data, response_headers

    def _post(
        self,
        url: str,
        token: str,
        api_id: str,
        body: Optional[Dict[str, Any]],
        cont_yn: str,
        next_key: str,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }
        response = requests.post(
            url, headers=headers, json=body or {}, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json(), dict(response.headers)


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
