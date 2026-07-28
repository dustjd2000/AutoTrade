"""잔고 조회 응답 구조 진단 (조회 전용 — 주문을 내지 않는다).

get_positions()가 보유 종목을 못 찾을 때, 어떤 TR이 어떤 필드명으로 응답하는지
직접 확인하기 위한 도구다. 파싱 후보 키와 실제 응답 키를 나란히 보여준다.

    python scripts/check_balance.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

ACCOUNT_PATH = "/api/dostk/acnt"
STOCK_INFO_PATH = "/api/dostk/stkinfo"
STOCK_INFO_API_ID = "ka10001"  # 주식기본정보요청

# 잔고 계열 TR 후보 — 어느 쪽이 보유 종목을 돌려주는지 확인한다
CANDIDATES = [
    ("kt00001", "예수금상세현황요청", {"qry_tp": "3"}),
    ("kt00005", "체결잔고요청", {"dmst_stex_tp": "KRX"}),
    ("kt00018", "계좌평가잔고내역요청", {"qry_tp": "1", "dmst_stex_tp": "KRX"}),
]

# account.get_positions()가 찾는 키들
ROW_LIST_KEYS = ("stk_cntr_remn", "acnt_evlt_remn_indv_tot", "output", "list")
TICKER_KEYS = ("stk_cd", "pdno", "stkcd")
QTY_KEYS = ("rmnd_qty", "hldg_qty", "cntr_qty")


def describe(api_id: str, label: str, body: dict, client: KiwoomClient) -> None:
    print(f"\n{'=' * 70}\n{api_id}  {label}\n{'=' * 70}")
    try:
        data, _ = client.request(ACCOUNT_PATH, api_id, body)
    except Exception as e:
        print(f"  [실패] {type(e).__name__}: {e}")
        return

    print(f"  응답 최상위 키: {list(data.keys())}")

    list_fields = [k for k, v in data.items() if isinstance(v, list)]
    print(f"  리스트 필드: {list_fields or '없음'}")
    matched = [k for k in ROW_LIST_KEYS if isinstance(data.get(k), list)]
    print(f"  파서가 인식하는 목록 키: {matched or '없음  ← 목록을 못 찾음'}")

    for key in list_fields:
        rows = data[key]
        print(f"\n  '{key}' — {len(rows)}행")
        if not rows:
            print("    (비어 있음)")
            continue
        row = rows[0]
        print(f"    첫 행 키: {list(row.keys())}")
        has_ticker = [k for k in TICKER_KEYS if k in row]
        has_qty = [k for k in QTY_KEYS if k in row]
        print(f"    종목코드 후보 일치: {has_ticker or '없음  ← 행이 버려짐'}")
        print(f"    수량 후보 일치:     {has_qty or '없음  ← 수량 0으로 읽혀 버려짐'}")
        print(f"    첫 행 원문: {json.dumps(row, ensure_ascii=False)[:400]}")


def check_tradable(client: KiwoomClient) -> None:
    """보유 종목마다 ka10001이 응답하는지 확인한다 — 매도 가능 여부 판정의 근거다.

    상장폐지·거래정지 종목은 잔고에는 그대로 실려 오지만 여기서 오류가 나거나
    종목명·현재가가 비어 돌아온다 (TradingEngine._untradable_reason이 쓰는 조건).
    """
    print(f"\n{'=' * 70}\n보유 종목 매도 가능 여부 (ka10001 주식기본정보요청)\n{'=' * 70}")

    data, _ = client.request(
        ACCOUNT_PATH, "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}
    )
    rows = next((data[k] for k in ROW_LIST_KEYS if isinstance(data.get(k), list)), [])
    tickers = []
    for row in rows:
        code = next((row[k] for k in TICKER_KEYS if k in row), None)
        if code:
            tickers.append(str(code).strip().lstrip("A"))
    if not tickers:
        print("  보유 종목이 없습니다.")
        return

    for ticker in tickers:
        try:
            info, _ = client.request(STOCK_INFO_PATH, STOCK_INFO_API_ID, {"stk_cd": ticker})
        except Exception as e:
            print(f"  {ticker}: [조회 실패] {type(e).__name__}: {e}  ← 매도 불가로 제외 대상")
            continue
        name = next((info[k] for k in ("stk_nm", "stk_nm_shrt") if k in info), "")
        price = info.get("cur_prc", "")
        verdict = "제외 대상" if not str(name).strip() else "정상"
        print(f"  {ticker}: 종목명='{name}' 현재가='{price}'  ← {verdict}")


def main() -> None:
    settings = Settings()
    settings.validate()
    print(f"모드: {settings.mode} / base_url: {settings.api_base_url}")

    client = KiwoomClient(settings, AuthClient(settings))
    for api_id, label, body in CANDIDATES:
        describe(api_id, label, body, client)
    check_tradable(client)


if __name__ == "__main__":
    main()
