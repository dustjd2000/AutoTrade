"""당일 체결내역 조회 TR 진단 (조회 전용 — 주문을 내지 않는다).

일일 리포트의 '매수/매도 체결 건수'가 늘 0으로 나오는 원인은, 주문 접수 시점의
pending 상태가 체결로 갱신되지 않아서다 (OrderClient.send_order는 주문번호만 받고
체결 여부를 모른다). 이를 고치려면 주문번호별 체결수량·체결단가를 돌려주는 TR이
필요한데, 어느 TR이 어떤 필드명으로 응답하는지 확인하기 위한 도구다.

주문 API(kt10000~kt10003)는 호출하지 않는다 — 조회만 한다.

    python scripts/check_fills.py
"""
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from config.settings import Settings
from src.api.auth import AuthClient
from src.api.client import KiwoomClient

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

ACCOUNT_PATH = "/api/dostk/acnt"
TRADE_DB_PATH = ROOT / "data" / "trades.db"

TODAY = date.today().strftime("%Y%m%d")

# 체결내역 계열 TR 후보. 요청 본문 필드도 확정된 것이 아니므로,
# 실패하면 키움이 돌려준 return_msg를 그대로 보여준다 (어느 필드가 문제인지 드러난다).
CANDIDATES = [
    (
        "ka10075",
        "실시간미체결요청",
        {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"},
    ),
    (
        "ka10076",
        "실시간체결요청",
        {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "0"},
    ),
    (
        "kt00007",
        "계좌별주문체결내역상세요청",
        {
            "ord_dt": TODAY,
            "qry_tp": "1",
            "stk_bond_tp": "0",
            "sell_tp": "0",
            "stk_cd": "",
            "fr_ord_no": "",
            "dmst_stex_tp": "KRX",
        },
    ),
    (
        "kt00009",
        "계좌별주문체결현황요청",
        {
            "stk_bond_tp": "0",
            "mrkt_tp": "0",
            "sell_tp": "0",
            "qry_tp": "0",
            "dmst_stex_tp": "KRX",
        },
    ),
]

# 우리가 필요한 값 — 이 이름 조각이 들어간 응답 필드를 찾아 보여준다
WANTED = {
    "주문번호": ("ord_no",),
    "종목코드": ("stk_cd",),
    "종목명": ("stk_nm",),
    "체결수량": ("cntr_qty", "cntr_q"),
    "체결단가": ("cntr_uv", "cntr_pric", "cntr_prc"),
    "주문수량": ("ord_qty",),
    "미체결수량": ("oso_qty", "rmn_qty", "unctr"),
}


def today_order_ids() -> list:
    """DB에 pending으로 남아 있는 오늘 주문번호 — 응답에서 이 번호를 찾을 수 있는지 본다."""
    if not TRADE_DB_PATH.exists():
        return []
    start = f"{date.today().isoformat()}T00:00:00"
    end = f"{date.today().isoformat()}T23:59:59.999999"
    with sqlite3.connect(TRADE_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT order_id, ticker, side, quantity FROM trades WHERE timestamp BETWEEN ? AND ?",
            (start, end),
        ).fetchall()
    return rows


def describe(api_id: str, label: str, body: dict, client: KiwoomClient, order_ids: set) -> None:
    print(f"\n{'=' * 74}\n{api_id}  {label}\n{'=' * 74}")
    print(f"  요청 본문: {json.dumps(body, ensure_ascii=False)}")
    try:
        data, headers = client.request(ACCOUNT_PATH, api_id, body)
    except Exception as e:
        print(f"  [실패] {type(e).__name__}: {e}")
        return

    print(f"  응답 최상위 키: {list(data.keys())}")
    print(f"  연속조회: cont-yn={headers.get('cont-yn')} next-key={headers.get('next-key')}")

    list_fields = [k for k, v in data.items() if isinstance(v, list)]
    if not list_fields:
        print("  리스트 필드 없음 ← 체결 목록을 담고 있지 않다")
        return

    for key in list_fields:
        rows = data[key]
        print(f"\n  '{key}' — {len(rows)}행")
        if not rows:
            print("    (비어 있음)")
            continue

        row = rows[0]
        print(f"    첫 행 키: {list(row.keys())}")
        for want, fragments in WANTED.items():
            hits = [k for k in row if any(f in k for f in fragments)]
            print(f"    {want:>8}: {hits or '없음'}")

        # 오늘 낸 주문번호가 이 목록에 실려 오는지 — 매칭 키를 확정하기 위한 확인
        matched = []
        for r in rows:
            for k, v in r.items():
                if "ord_no" in k and str(v).strip().lstrip("0") in order_ids:
                    matched.append((k, str(v).strip()))
        print(f"    오늘 주문번호 일치: {matched or '없음'}")

        for r in rows[:3]:
            print(f"    행: {json.dumps(r, ensure_ascii=False)[:500]}")


def main() -> None:
    settings = Settings()
    settings.validate()
    print(f"모드: {settings.mode} / base_url: {settings.api_base_url}")
    if settings.mode == "live":
        print("※ 실계좌 조회입니다. 이 스크립트는 주문 API를 호출하지 않습니다.")

    rows = today_order_ids()
    print(f"\n오늘 DB에 기록된 주문 {len(rows)}건:")
    for order_id, ticker, side, quantity in rows:
        print(f"  주문번호 {order_id}  {ticker}  {side}  {quantity}주")
    order_ids = {str(r[0]).strip().lstrip("0") for r in rows}

    client = KiwoomClient(settings, AuthClient(settings))
    for api_id, label, body in CANDIDATES:
        describe(api_id, label, body, client, order_ids)


if __name__ == "__main__":
    main()
