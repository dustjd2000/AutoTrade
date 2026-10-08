"""종목별 외국인·기관 수급 TR 진단 (조회 전용 — 주문을 내지 않는다).

AI 매도 판단에 수급을 붙일지 정하기 전에, 먼저 장중에 값을 기록만 해 보려 한다.
그러려면 어느 TR이 **종목 단위로, 장중에** 외국인·기관 순매수를 돌려주는지, 필드명이
무엇이고 언제 갱신되는지를 알아야 한다 — 그것을 확인하기 위한 도구다.

TR ID·경로·요청 본문은 확정된 것이 아니다. 실패하면 키움이 돌려준 return_msg를 그대로
보여준다 (어느 필드가 문제인지 드러난다).

**엔진이 떠 있으면 돌리지 않는다.** 키움은 앱키당 토큰을 하나만 유지해서, 이 스크립트가
토큰을 발급하는 순간 엔진의 토큰이 무효화된다 (PRD 10절 '토큰 무효화와 자동 재발급').

    python scripts/check_investor_flow.py [종목코드 ...]
"""
import json
import subprocess
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

TODAY = date.today().strftime("%Y%m%d")
DEFAULT_TICKERS = ["005930", "000660"]

# (api_id, 이름, 경로, 요청 본문 생성 함수)
CANDIDATES = [
    (
        "ka10064",
        "장중투자자별매매차트요청",
        "/api/dostk/chart",
        lambda t: {"mrkt_tp": "000", "amt_qty_tp": "2", "trde_tp": "0", "stk_cd": t},
    ),
    (
        "ka10059",
        "종목별투자자기관별요청",
        "/api/dostk/stkinfo",
        lambda t: {"dt": TODAY, "stk_cd": t, "amt_qty_tp": "2", "trde_tp": "0", "unit_tp": "1"},
    ),
    (
        "ka10060",
        "종목별투자자기관별차트요청",
        "/api/dostk/chart",
        lambda t: {"dt": TODAY, "stk_cd": t, "amt_qty_tp": "2", "trde_tp": "0", "unit_tp": "1"},
    ),
    ("ka10008", "주식외국인종목별매매동향", "/api/dostk/frgnistt", lambda t: {"stk_cd": t}),
    ("ka10009", "주식기관요청", "/api/dostk/frgnistt", lambda t: {"stk_cd": t}),
    ("ka10040", "당일주요거래원요청", "/api/dostk/rkinfo", lambda t: {"stk_cd": t}),
]

# 우리가 필요한 값 — 이 이름 조각이 들어간 응답 필드를 찾아 보여준다
WANTED = {
    "시각/일자": ("tm", "dt"),
    "외국인": ("frgn", "for"),
    "기관": ("orgn", "inst"),
    "개인": ("ind",),
    "순매수": ("netprps", "netslmt", "net"),
}


def engine_running() -> bool:
    """pythonw.exe가 떠 있으면 엔진이 돌고 있다고 본다 (AutoTrade.bat이 pythonw로 띄운다)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq pythonw.exe"], capture_output=True, text=True
        ).stdout
    except Exception:
        return False
    return "pythonw.exe" in out


def describe(client: KiwoomClient, api_id: str, label: str, path: str, body: dict) -> None:
    print(f"\n{'=' * 74}\n{api_id}  {label}  ({path})\n{'=' * 74}")
    print(f"  요청 본문: {json.dumps(body, ensure_ascii=False)}")
    try:
        data, headers = client.request(path, api_id, body)
    except Exception as e:
        print(f"  [실패] {type(e).__name__}: {e}")
        return

    print(f"  응답 최상위 키: {list(data.keys())}")
    print(f"  연속조회: cont-yn={headers.get('cont-yn')} next-key={headers.get('next-key')}")

    scalars = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
    if scalars:
        print(f"  단일 값: {json.dumps(scalars, ensure_ascii=False)[:600]}")

    for key in [k for k, v in data.items() if isinstance(v, list)]:
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
        for r in rows[:5]:
            print(f"    행: {json.dumps(r, ensure_ascii=False)[:500]}")


def main() -> None:
    if engine_running():
        print("엔진(pythonw.exe)이 실행 중입니다. 토큰 무효화를 막기 위해 중단합니다.")
        sys.exit(1)

    settings = Settings()
    settings.validate()
    print(f"모드: {settings.mode} / base_url: {settings.api_base_url}")

    tickers = sys.argv[1:] or DEFAULT_TICKERS
    client = KiwoomClient(settings, AuthClient(settings))
    for ticker in tickers:
        print(f"\n\n######## {ticker} ########")
        for api_id, label, path, make_body in CANDIDATES:
            describe(client, api_id, label, path, make_body(ticker))


if __name__ == "__main__":
    main()
