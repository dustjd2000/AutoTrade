import pytest

from src.api.client import KiwoomAPIError, to_float, to_int


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
