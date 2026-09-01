from types import SimpleNamespace

from src.api.account import AccountClient


def make_client(response):
    client = AccountClient.__new__(AccountClient)
    client.settings = SimpleNamespace()
    client.auth = SimpleNamespace()
    client._client = SimpleNamespace(request=lambda *a, **kw: (response, {}))
    client._logged_missing_fees = False   # __new__로 만들어 __init__을 건너뛴다
    client._logged_fee_values = False
    return client


def test_get_cash_reads_orderable_amount():
    client = make_client({"ord_alow_amt": "+0000010000000"})
    assert client.get_cash() == 10_000_000.0


def test_get_cash_falls_back_to_alternate_field_names():
    client = make_client({"entr": "5000000"})
    assert client.get_cash() == 5_000_000.0


def test_get_cash_returns_zero_when_no_known_field():
    client = make_client({"unexpected": "123"})
    assert client.get_cash() == 0.0


def test_get_positions_parses_holdings():
    client = make_client(
        {
            "stk_cntr_remn": [
                {"stk_cd": "A005930", "rmnd_qty": "10", "pur_pric": "70000", "cur_prc": "+71000"},
                {"stk_cd": "000660", "rmnd_qty": "5", "pur_pric": "180000", "cur_prc": "-179000"},
            ]
        }
    )

    positions = client.get_positions()

    assert set(positions) == {"005930", "000660"}   # 'A' 접두사 제거
    assert positions["005930"].quantity == 10
    assert positions["005930"].current_price == 71000.0
    assert positions["000660"].current_price == 179000.0  # 부호는 등락 방향일 뿐


def test_get_positions_skips_zero_quantity_rows():
    client = make_client(
        {"stk_cntr_remn": [{"stk_cd": "005930", "rmnd_qty": "0", "pur_pric": "70000"}]}
    )
    assert client.get_positions() == {}


def test_get_positions_returns_empty_when_no_list_field():
    client = make_client({"return_code": 0})
    assert client.get_positions() == {}


def test_get_positions_parses_the_live_balance_response():
    """kt00018(계좌평가잔고내역요청) 실제 응답 형태 — 2026-07-28 실계좌로 확인."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "A032640",
                    "stk_nm": "LG유플러스",
                    "rmnd_qty": "000000000000022",
                    "pur_pric": "000000000014758",
                    "cur_prc": "000000014650",
                }
            ]
        }
    )

    positions = client.get_positions()

    assert set(positions) == {"032640"}
    held = positions["032640"]
    assert held.quantity == 22
    assert held.avg_price == 14758.0
    assert held.current_price == 14650.0
    assert held.name == "LG유플러스"


def test_get_positions_reads_alternate_quantity_and_price_names():
    """kt00005(체결잔고요청)는 수량이 cur_qty, 평단이 buy_uv로 이름이 다르다."""
    client = make_client(
        {
            "stk_cntr_remn": [
                {"stk_cd": "A032640", "cur_qty": "000000000022", "buy_uv": "000000014758",
                 "cur_prc": "000000014650"}
            ]
        }
    )

    positions = client.get_positions()

    assert positions["032640"].quantity == 22
    assert positions["032640"].avg_price == 14758.0


def test_get_positions_reads_the_sellable_quantity():
    """미체결 매도가 걸려 있으면 매도가능수량이 보유수량보다 적다 — 청산은 이 값으로 나간다."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "A032640",
                    "rmnd_qty": "000000000000022",
                    "trde_able_qty": "000000000000018",
                    "pur_pric": "000000000014758",
                }
            ]
        }
    )

    held = client.get_positions()["032640"]

    assert held.sellable_quantity == 18
    assert held.closable_quantity == 18


def test_sellable_quantity_is_none_when_the_field_is_absent():
    """0으로 읽으면 매도가 아예 막힌다 — 못 읽은 것과 정말 0인 것은 달라야 한다."""
    client = make_client(
        {"stk_cntr_remn": [{"stk_cd": "005930", "rmnd_qty": "10", "pur_pric": "70000"}]}
    )

    held = client.get_positions()["005930"]

    assert held.sellable_quantity is None
    assert held.closable_quantity == 10


def test_unparsable_rows_are_logged_as_an_error(caplog):
    """행은 왔는데 하나도 해석하지 못하면 '보유 없음'으로 읽혀 청산이 조용히 멈춘다."""
    client = make_client({"acnt_evlt_remn_indv_tot": [{"미지의필드": "1", "값": "2"}]})

    with caplog.at_level("ERROR"):
        assert client.get_positions() == {}

    assert "하나도 해석하지 못했습니다" in caplog.text


def test_get_positions_reads_the_fee_fields_when_present():
    """키움이 수수료·세금을 주면 그대로 담는다 — 매도 비용은 수수료+세금 합이다."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "A047050",
                    "rmnd_qty": "000000000000015",
                    "pur_pric": "000000000055600",
                    "cur_prc": "000000056400",
                    "pur_cmsn": "000000000120",
                    "sell_cmsn": "000000000120",
                    "tax": "000000001692",
                }
            ]
        }
    )

    held = client.get_positions()["047050"]

    assert held.buy_fee == 120.0
    assert held.sell_cost == 1812.0   # 매도수수료 120 + 세금 1692


def test_get_positions_leaves_fees_none_when_absent():
    """필드가 없으면 None — 0(비용 없음)과 구분해야 폴백 계산으로 넘어간다."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "A047050",
                    "rmnd_qty": "000000000000015",
                    "pur_pric": "000000000055600",
                    "cur_prc": "000000056400",
                }
            ]
        }
    )

    held = client.get_positions()["047050"]

    assert held.buy_fee is None
    assert held.sell_cost is None


def test_missing_fee_fields_are_logged_once(caplog):
    """잔고는 5초마다 돌므로 매번 남기면 로그가 묻힌다 — 첫 행 키를 한 번만 남긴다."""
    client = make_client(
        {"acnt_evlt_remn_indv_tot": [{"stk_cd": "A047050", "rmnd_qty": "1", "pur_pric": "100"}]}
    )

    with caplog.at_level("WARNING"):
        client.get_positions()
        client.get_positions()

    hits = [r for r in caplog.records if "수수료" in r.message]
    assert len(hits) == 1
    assert "stk_cd" in hits[0].getMessage()   # 실제 응답 키를 남겨야 다음에 후보를 넓힐 수 있다


def test_found_fee_fields_are_logged_once(caplog):
    """무엇을 받았는지 한 번은 남겨야 폴백을 쓰는지 아닌지 로그로 판정할 수 있다."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "A047050",
                    "rmnd_qty": "15",
                    "pur_pric": "55600",
                    "cur_prc": "56400",
                    "pur_cmsn": "120",
                    "sell_cmsn": "120",
                    "tax": "1692",
                }
            ]
        }
    )

    with caplog.at_level("INFO"):
        client.get_positions()
        client.get_positions()

    hits = [r for r in caplog.records if "수수료·세금을 읽었습니다" in r.getMessage()]
    assert len(hits) == 1
    message = hits[0].getMessage()
    assert "120" in message and "1812" in message   # 매입수수료와 예상 매도비용
    assert "pur_cmsn" in message                    # 어느 키가 맞았는지 알 수 있어야 한다


def test_partial_fee_fields_fall_back_and_report_the_keys(caplog):
    """세금만 오면 매도비용 합을 만들 수 없다 — 그때는 전부 폴백이고 키를 남겨야 한다."""
    client = make_client(
        {
            "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A047050", "rmnd_qty": "15", "pur_pric": "55600", "tax": "1692"}
            ]
        }
    )

    with caplog.at_level("INFO"):
        held = client.get_positions()["047050"]

    assert held.sell_cost is None, "매도수수료가 없으면 합을 만들 수 없다"
    assert held.buy_fee is None
    hits = [r for r in caplog.records if "찾지 못해" in r.getMessage()]
    assert len(hits) == 1
    assert "tax" in hits[0].getMessage(), "다음에 후보를 넓히려면 실제 키가 보여야 한다"
