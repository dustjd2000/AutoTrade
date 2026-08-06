from types import SimpleNamespace

import pytest

from src.api import client as client_module
from src.api.client import KiwoomAPIError, KiwoomClient, to_float, to_int


def test_to_int_handles_signed_and_padded_values():
    # 키움은 '+000012345' / '-000012345' 형태로 부호와 0패딩을 붙여 보낸다
    assert to_int("+000012345") == 12345
    assert to_int("-000012345") == -12345
    assert to_int("1,234") == 1234


def test_to_int_handles_empty_and_none():
    assert to_int("") == 0
    assert to_int(None) == 0
    assert to_int("-") == 0
    assert to_int("+") == 0


def test_to_float_handles_signed_values():
    assert to_float("+1234.5") == 1234.5
    assert to_float("-1234.5") == -1234.5
    assert to_float("") == 0.0
    assert to_float(None) == 0.0


def test_kiwoom_api_error_message_includes_context():
    err = KiwoomAPIError("kt10000", 3, "주문가능금액 부족")
    assert "kt10000" in str(err)
    assert "3" in str(err)
    assert "주문가능금액 부족" in str(err)


# ── 인증 오류 시 토큰 재발급 후 재시도 (2026-08-06) ──────────
class FakeAuth:
    """ensure_token/refresh_token 호출 순서를 기록하는 가짜 인증 클라이언트."""

    def __init__(self, tokens=("old", "new")):
        self._tokens = list(tokens)
        self.issued = [self._tokens[0]]
        self.rejected = []

    def ensure_token(self):
        return self.issued[-1]

    def refresh_token(self, rejected=None):
        self.rejected.append(rejected)
        self.issued.append(self._tokens[min(len(self.issued), len(self._tokens) - 1)])
        return self.issued[-1]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def make_client(responses, auth=None, calls=None):
    """responses를 순서대로 돌려주는 KiwoomClient. 호출 헤더를 calls에 적는다."""
    auth = auth or FakeAuth()
    queue = list(responses)
    client = KiwoomClient(SimpleNamespace(api_base_url="https://api.test"), auth)

    def fake_post(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append(headers["authorization"])
        return FakeResponse(queue.pop(0))

    return client, auth, fake_post


def test_request_returns_payload_and_headers(monkeypatch):
    client, _, fake_post = make_client([{"return_code": 0, "cur_prc": "1000"}])
    monkeypatch.setattr(client_module.requests, "post", fake_post)

    data, headers = client.request("/api/dostk/stkinfo", "ka10001", {"stk_cd": "005930"})

    assert data["cur_prc"] == "1000"
    assert headers == {}


def test_auth_error_reissues_token_and_retries(monkeypatch):
    """토큰이 무효화되면 재발급 후 한 번 더 시도한다 — 08-06 손절 실패의 재발 방지."""
    calls = []
    client, auth, fake_post = make_client(
        [
            {"return_code": 3, "return_msg": "인증에 실패했습니다[8005:Token이 유효하지 않습니다]"},
            {"return_code": 0, "ord_no": "0114848"},
        ],
        calls=calls,
    )
    monkeypatch.setattr(client_module.requests, "post", fake_post)

    data, _ = client.request("/api/dostk/ordr", "kt10001", {})

    assert data["ord_no"] == "0114848"
    assert auth.rejected == ["old"]              # 거부당한 토큰을 넘겨 중복 발급을 막는다
    assert calls == ["Bearer old", "Bearer new"]  # 재시도는 새 토큰으로 나간다


def test_auth_error_twice_raises_instead_of_looping(monkeypatch):
    """재발급하고도 거부되면 그대로 실패시킨다 — 무한 재시도로 토큰을 계속 발급하지 않는다."""
    client, _, fake_post = make_client(
        [
            {"return_code": 3, "return_msg": "인증에 실패했습니다"},
            {"return_code": 3, "return_msg": "인증에 실패했습니다"},
        ]
    )
    monkeypatch.setattr(client_module.requests, "post", fake_post)

    with pytest.raises(KiwoomAPIError):
        client.request("/api/dostk/ordr", "kt10001", {})


@pytest.mark.parametrize(
    "payload",
    [
        {"return_code": 4, "return_msg": "주문가능금액이 부족합니다"},
        # return_code=3이 인증 전용이라고 확인된 것은 아니다 — 메시지로 한 번 더 가린다
        {"return_code": 3, "return_msg": "주문가능금액이 부족합니다"},
    ],
)
def test_non_auth_error_is_not_retried(monkeypatch, payload):
    """업무 오류까지 재시도하면 같은 주문이 두 번 나갈 수 있다."""
    calls = []
    client, auth, fake_post = make_client([payload], calls=calls)
    monkeypatch.setattr(client_module.requests, "post", fake_post)

    with pytest.raises(KiwoomAPIError):
        client.request("/api/dostk/ordr", "kt10001", {})

    assert len(calls) == 1
    assert auth.rejected == []
