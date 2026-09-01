# 보유 종목 순손익 표시 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 보유 종목 표의 '손익' 열을 수수료·세금을 반영한 순손익 금액·순손익률로 바꾸고, 요약줄도 같은 기준으로 맞춘다.

**Architecture:** 키움 잔고 TR(kt00018)이 주는 수수료·세금 **비용** 필드를 `Position`에 옵션으로 담고, 없으면 실측으로 확정된 절사 규칙으로 계산한다(오차 1원 이내). 손익 조립은 `src/risk/manager.py`에 모아 테스트 가능하게 두고, UI는 엔진이 넘긴 값만 그린다. 익절/손절 **판정** 경로(`net_return`/`portfolio_net_return`, 슬리피지 포함)는 손대지 않는다 — 새 함수는 표시 전용이다.

**Tech Stack:** Python 3, pytest, PyQt6. 새 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-09-01-net-pnl-display-design.md`

## Global Constraints

- 표시값에서 **슬리피지는 뺀다**. 판정(`check_portfolio_exit`)은 지금처럼 슬리피지를 포함한다 — 기존 함수 `net_return` / `portfolio_net_return` / `exit_trigger_price`는 **수정 금지**.
- 세금은 `⌊금액 × tax_ratio⌋` 한 값으로 절사한다. `0.15% + 0.05%`로 쪼개지 않는다.
- 수수료는 `⌊금액 × commission_ratio ÷ 10⌋ × 10` — 키움이 10원 단위로 절사한다(실측).
- 순손익률의 분모는 **매입금액**(수수료 제외). 기존 `portfolio_net_return`과 같은 기준이라 두 값이 나란히 읽힌다.
- `Optional[float]`의 `None`은 "응답에서 읽지 못했다", `0.0`은 "비용이 0"이다. 두 값을 섞지 않는다 — `sellable_quantity`가 이미 쓰는 규약이다.
- 커밋 메시지는 한국어 현재형 한 줄 + 본문. 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- 테스트는 `.venv/Scripts/python.exe -m pytest`로 돌린다.

**시작 전 확인:** 작업 트리에 세율 수정(`config/settings.py`, `src/risk/manager.py`, `.env.example`)이 미커밋 상태로 남아 있을 수 있다. 있으면 **먼저 별도 커밋**하고 Task 1을 시작한다 — 이 계획의 커밋에 섞지 않는다.

## File Structure

| 파일 | 역할 |
|---|---|
| `src/api/account.py` (수정) | kt00018 응답에서 수수료·세금 필드를 찾아 `Position`에 담는다. 못 찾으면 `None` + 1회 경고 |
| `src/risk/manager.py` (수정) | 순손익 금액·비율 계산. 표시 전용 함수 3개 + `RiskManager` 메서드 2개 |
| `src/core/engine.py` (수정) | `PositionView`에 순손익 필드, 표시용 합산 스냅샷 |
| `src/ui/engine_thread.py` (수정) | 합산 스냅샷 패스스루 |
| `src/ui/main_window.py` (수정) | 열 이름·셀 값·요약줄·툴팁 |
| `tests/test_api/test_account.py` (수정) | 필드 있음/없음 두 경로 |
| `tests/test_risk/test_manager.py` (수정) | 실제 체결 3건으로 계산 검증 |
| `tests/test_core/test_net_pnl_snapshot.py` (신규) | 엔진 스냅샷 |

---

### Task 1: 잔고 응답에서 수수료·세금 필드를 읽는다

**Files:**
- Modify: `src/api/account.py`
- Test: `tests/test_api/test_account.py`

**Interfaces:**
- Consumes: 없음 (첫 작업)
- Produces: `Position.buy_fee: Optional[float]`, `Position.sell_cost: Optional[float]` — Task 2가 쓴다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_api/test_account.py` 맨 끝에 추가한다.

```python
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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api/test_account.py -k "fee" -v`
Expected: FAIL — `AttributeError: 'Position' object has no attribute 'buy_fee'`

- [ ] **Step 3: 구현한다**

`src/api/account.py`의 `Position` dataclass에 필드 두 개를 추가한다 (`sellable_quantity` 아래).

```python
    # 키움 잔고 응답이 주는 실제 비용. None은 '응답에서 읽지 못했다'는 뜻이며
    # 0.0(비용 없음)과 구분해야 한다 — sellable_quantity와 같은 규약이다.
    # 못 읽으면 표시용 순손익은 절사 규칙으로 계산한다 (src/risk/manager.py).
    buy_fee: Optional[float] = None      # 매입수수료 — 이미 낸 확정값
    sell_cost: Optional[float] = None    # 예상 매도수수료 + 세금 (키움 추정)
```

같은 파일에 모듈 수준 헬퍼를 추가한다 (`_first_present` 바로 아래).

```python
def _fee_fields(row: Dict) -> tuple:
    """잔고 행에서 매입수수료와 예상 매도비용(수수료+세금)을 뽑는다.

    키움이 이 필드들을 주는지는 실계좌 응답으로 확인되지 않았다 (2026-09-01 시점에
    보유 종목이 없어 확인 불가). 못 찾으면 (None, None)을 돌려주고 호출부가
    절사 규칙 계산으로 폴백한다 — 실측 오차가 1원 이내라 실질 차이는 없다.
    """
    buy_fee = _first_present(row, "pur_cmsn", "buy_cmsn", "pchs_cmsn")
    sell_cmsn = _first_present(row, "sell_cmsn", "evlt_cmsn", "sl_cmsn")
    tax = _first_present(row, "tax", "sell_tax", "evlt_tax")

    # 매도비용은 수수료와 세금이 둘 다 있어야 의미가 있다 — 한쪽만 있으면 폴백이 낫다
    sell_cost = to_float(sell_cmsn) + to_float(tax) if sell_cmsn is not None and tax is not None else None
    return (to_float(buy_fee) if buy_fee is not None else None), sell_cost
```

`AccountClient.__init__`에 플래그를 추가한다.

```python
        # 잔고는 5초마다 돌아서, 필드를 못 찾는다는 경고를 매번 남기면 로그가 묻힌다
        self._logged_missing_fees = False
```

`get_positions`의 `positions[ticker] = Position(...)` 호출에 두 값을 넘긴다.

```python
            buy_fee, sell_cost = _fee_fields(row)
            positions[ticker] = Position(
                ...
                sellable_quantity=to_int(sellable) if sellable is not None else None,
                buy_fee=buy_fee,
                sell_cost=sell_cost,
            )
```

`return positions` 직전, 기존 `if rows and not positions:` 블록 **뒤에** 경고를 넣는다.

```python
        if positions and not self._logged_missing_fees and all(
            p.buy_fee is None and p.sell_cost is None for p in positions.values()
        ):
            self._logged_missing_fees = True
            logger.warning(
                "잔고 응답에서 수수료·세금 필드를 찾지 못해 순손익을 자체 계산합니다. "
                "첫 행 키: %s",
                list(rows[0].keys()),
            )
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api/test_account.py -v`
Expected: PASS (기존 테스트 포함 전부)

- [ ] **Step 5: 커밋**

```bash
git add src/api/account.py tests/test_api/test_account.py
git commit -F - <<'EOF'
잔고 응답에서 수수료·세금을 읽어 둔다

보유 종목 순손익을 추정 세율이 아니라 실제 비용으로 계산하기 위한 준비다.
키움이 이 필드를 주는지는 아직 확인하지 못했다 — 확인할 수 있는 날 보유
종목이 없었다. 못 찾으면 None으로 두고 첫 행 키를 한 번만 남긴다. 잔고는
5초마다 돌아서 매번 남기면 로그가 묻힌다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 2: 순손익 계산을 RiskManager에 넣는다

**Files:**
- Modify: `src/risk/manager.py`
- Test: `tests/test_risk/test_manager.py`

**Interfaces:**
- Consumes: `Position.buy_fee`, `Position.sell_cost` (Task 1)
- Produces:
  - `position_costs(position: Position, commission_rate: float, tax_rate: float) -> float`
  - `position_net_pnl(position: Position, commission_rate: float, tax_rate: float) -> float`
  - `portfolio_net_pnl(positions: Iterable[Position], commission_rate: float, tax_rate: float) -> Tuple[float, Optional[float]]` — (금액, 비율). 계산할 종목이 없으면 `(0.0, None)`
  - `RiskManager.position_net_pnl(position) -> float`
  - `RiskManager.portfolio_net_pnl(positions) -> Tuple[float, Optional[float]]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_risk/test_manager.py` 맨 끝에 추가한다. 숫자는 2026-09-01 실제 체결 3건이다.

```python
# 2026-09-01 실매매 3건 — (평단, 현재가, 수량, 실제 순손익)
# 실제 순손익 = 실현손익 - 실제 매수수수료 - 실제 매도수수료 - 실제 세금
REAL_FILLS = [
    ("047050", 55600.0, 56400.0, 15, 10068.0),   # 12000 - 120 - 120 - 1692
    ("012330", 453000.0, 447500.0, 1, -6514.0),  # -5500 - 60 - 60 - 894
    ("161390", 67100.0, 66900.0, 12, -4245.0),   # -2400 - 120 - 120 - 1605
]


def fill_position(ticker, avg, cur, qty):
    return Position(ticker=ticker, quantity=qty, avg_price=avg, current_price=cur)


@pytest.mark.parametrize("ticker,avg,cur,qty,actual", REAL_FILLS)
def test_net_pnl_matches_real_fills_within_one_won(ticker, avg, cur, qty, actual):
    """절사 규칙 폴백이 실제 체결 결과와 1원 이내로 맞아야 한다."""
    pnl = position_net_pnl(fill_position(ticker, avg, cur, qty), 0.00015, 0.002)
    assert abs(pnl - actual) <= 1.0, f"{ticker}: {pnl} vs {actual}"


def test_commission_is_truncated_to_ten_won():
    """키움은 매매수수료를 10원 단위로 절사한다 — 453,000원이면 67.95원이 아니라 60원."""
    position = fill_position("012330", 453000.0, 453000.0, 1)
    # 매수 60 + 매도 60 + 세금 ⌊453000*0.002⌋=906
    assert position_costs(position, 0.00015, 0.002) == 1026.0


def test_kiwoom_values_take_precedence_over_the_formula():
    """키움이 실제 비용을 주면 절사 계산 대신 그 값을 쓴다."""
    position = fill_position("047050", 55600.0, 56400.0, 15)
    position.buy_fee = 100.0
    position.sell_cost = 200.0
    assert position_costs(position, 0.00015, 0.002) == 300.0


def test_portfolio_net_pnl_sums_positions_and_divides_by_cost():
    """합산 비율의 분모는 매입금액 — portfolio_net_return과 같은 기준이라야 나란히 읽힌다."""
    positions = [fill_position(t, a, c, q) for t, a, c, q, _ in REAL_FILLS]

    amount, ratio = portfolio_net_pnl(positions, 0.00015, 0.002)

    assert amount == pytest.approx(-692.0, abs=1.0)
    cost = 55600 * 15 + 453000 * 1 + 67100 * 12
    assert ratio == pytest.approx(amount / cost)


def test_portfolio_net_pnl_skips_positions_without_a_price():
    """현재가 0을 그대로 넣으면 -100%로 잡힌다 — portfolio_net_return과 같은 방어다."""
    positions = [
        fill_position("047050", 55600.0, 56400.0, 15),
        fill_position("000000", 10000.0, 0.0, 10),
    ]

    amount, ratio = portfolio_net_pnl(positions, 0.00015, 0.002)

    assert amount == pytest.approx(10068.0, abs=1.0)
    assert ratio == pytest.approx(10068.0 / (55600 * 15), abs=1e-6)


def test_portfolio_net_pnl_returns_none_ratio_when_nothing_to_measure():
    assert portfolio_net_pnl([], 0.00015, 0.002) == (0.0, None)


def test_manager_exposes_net_pnl_with_its_own_rates():
    manager = make_manager(commission_rate=0.00015, tax_rate=0.002)
    position = fill_position("047050", 55600.0, 56400.0, 15)

    assert manager.position_net_pnl(position) == pytest.approx(10068.0, abs=1.0)
    assert manager.portfolio_net_pnl([position])[0] == pytest.approx(10068.0, abs=1.0)


def test_net_pnl_ignores_slippage():
    """표시용이라 슬리피지를 빼지 않는다 — 판정(portfolio_net_return)과 다른 점이다."""
    manager = make_manager(commission_rate=0.0, tax_rate=0.0, slippage_rate=0.5)
    position = fill_position("047050", 1000.0, 1100.0, 10)

    assert manager.position_net_pnl(position) == pytest.approx(1000.0)
```

import 줄도 고친다.

```python
from src.risk.manager import (
    RiskManager,
    exit_trigger_price,
    net_return,
    portfolio_net_pnl,
    portfolio_net_return,
    position_costs,
    position_net_pnl,
)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_risk/test_manager.py -v`
Expected: FAIL — `ImportError: cannot import name 'position_costs'`

- [ ] **Step 3: 구현한다**

`src/risk/manager.py` 맨 위에 `import math`를 추가하고, `typing` import에 `Tuple`을 더한다.

`portfolio_net_return` 아래에 표시 전용 함수들을 넣는다.

```python
# --- 표시 전용 (슬리피지 없음) --------------------------------------------
# 위의 net_return / portfolio_net_return은 익절/손절 **판정**용이라 슬리피지를 포함한다.
# 아래 함수들은 화면에 찍는 값이라 슬리피지를 빼고, 대신 키움의 절사 규칙을 재현한다.
# 슬리피지 0.1%는 세율 오차(0.02%p)의 5배라, 표시값에 넣으면 가장 근거가 약한 가정이
# 화면 숫자를 지배한다 (설계 문서 2026-09-01).


def _truncated_fee(amount: float, rate: float) -> float:
    """매매수수료 — 키움은 10원 단위로 절사한다 (매도 49건 실측, 2026-09-01)."""
    return math.floor(amount * rate / 10) * 10


def position_costs(position: Position, commission_rate: float, tax_rate: float) -> float:
    """이 종목을 지금 팔았을 때 나가는 비용 — 매수·매도 수수료와 매도세금의 합.

    키움 잔고 응답이 실제 비용을 주면(`buy_fee`/`sell_cost`) 그 값을 쓰고, 없으면
    절사 규칙으로 계산한다. 세금은 `0.15%+0.05%`로 쪼개지 않고 `tax_rate` 한 값으로
    절사한다 — 쪼개면 1원 더 정확하지만 TAX_PERCENT 설정을 무시하게 된다.
    """
    buy_amount = position.avg_price * position.quantity
    sell_amount = position.current_price * position.quantity

    buy_fee = (
        position.buy_fee
        if position.buy_fee is not None
        else _truncated_fee(buy_amount, commission_rate)
    )
    sell_cost = (
        position.sell_cost
        if position.sell_cost is not None
        else _truncated_fee(sell_amount, commission_rate) + math.floor(sell_amount * tax_rate)
    )
    return buy_fee + sell_cost


def position_net_pnl(position: Position, commission_rate: float, tax_rate: float) -> float:
    """수수료·세금을 뺀 순손익 금액 (원)."""
    gross = (position.current_price - position.avg_price) * position.quantity
    return gross - position_costs(position, commission_rate, tax_rate)


def portfolio_net_pnl(
    positions: Iterable[Position],
    commission_rate: float,
    tax_rate: float,
) -> Tuple[float, Optional[float]]:
    """보유 종목 전체의 (순손익 금액, 순손익률).

    분모는 매입금액이라 `portfolio_net_return`과 같은 기준이다 — 요약줄에서 두 값이
    나란히 읽혀야 한다. 평단·수량·현재가 중 하나라도 0인 종목은 뺀다: 현재가 0은
    조회 실패나 장 전 상태인데 그대로 넣으면 -100%로 잡힌다.
    계산할 종목이 하나도 없으면 비율은 None — '판정하지 않는다'는 뜻이다.
    """
    total = 0.0
    cost = 0.0
    for position in positions:
        if position.avg_price <= 0 or position.quantity <= 0 or position.current_price <= 0:
            continue
        total += position_net_pnl(position, commission_rate, tax_rate)
        cost += position.avg_price * position.quantity

    if cost <= 0:
        return 0.0, None
    return total, total / cost
```

`RiskManager.portfolio_return` 아래에 메서드 두 개를 넣는다.

```python
    def position_net_pnl(self, position: Position) -> float:
        """종목 순손익 금액 (표시용 — 슬리피지 없음)."""
        return position_net_pnl(position, self.commission_rate, self.tax_rate)

    def portfolio_net_pnl(self, positions: Iterable[Position]) -> Tuple[float, Optional[float]]:
        """보유 종목 합산 (순손익 금액, 순손익률) — 표시용이라 슬리피지를 빼지 않는다.

        판정에 쓰는 `portfolio_return`과는 슬리피지만큼 다르다. 표시값이 익절선에
        닿아도 실제 매도는 조금 뒤에 일어난다 — 요약줄 툴팁이 이걸 알린다.
        """
        return portfolio_net_pnl(positions, self.commission_rate, self.tax_rate)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_risk/test_manager.py -v`
Expected: PASS (신규 포함 전부)

- [ ] **Step 5: 커밋**

```bash
git add src/risk/manager.py tests/test_risk/test_manager.py
git commit -F - <<'EOF'
수수료·세금을 반영한 순손익을 계산한다

화면에 찍을 값이라 슬리피지를 빼고, 대신 키움의 절사 규칙을 재현한다 —
수수료는 10원 단위 절사, 세금은 원 단위 절사다. 키움 잔고가 실제 비용을
주면 그 값이 우선한다.

2026-09-01 실매매 3건으로 검산하면 오차가 1원 이내다. 판정에 쓰는
net_return / portfolio_net_return은 슬리피지를 계속 포함하므로 그대로 뒀다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 3: 엔진이 순손익을 스냅샷에 실어 보낸다

**Files:**
- Modify: `src/core/engine.py`
- Modify: `src/ui/engine_thread.py`
- Modify: `tests/test_core/test_carried_over_positions.py:47`, `test_closeout_report.py:50`, `test_last_price_cache.py:30`, `test_open_tickers.py:35`, `test_position_cache.py:64`, `test_selected_close.py:41`, `test_sellable_quantity.py:56`, `test_unsellable_list.py:35`, `test_untradable_positions.py:85`
- Test: `tests/test_core/test_net_pnl_snapshot.py` (신규)

**Interfaces:**
- Consumes: `RiskManager.position_net_pnl`, `RiskManager.portfolio_net_pnl` (Task 2)
- Produces:
  - `PositionView.net_pnl: float`, `PositionView.net_pnl_percent: float`
  - `TradingEngine.portfolio_net_pnl_snapshot() -> Tuple[float, Optional[float]]`
  - `EngineThread.portfolio_net_pnl() -> Tuple[float, Optional[float]]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_net_pnl_snapshot.py`를 새로 만든다. `risk_manager`는 스텁이 아니라 **진짜** `RiskManager`를 쓴다 — 표시값과 판정값이 어긋나는지 보려면 실제 계산이 필요하다.

```python
"""UI에 넘기는 순손익 스냅샷 — 표의 종목별 합계가 요약줄과 맞아야 한다."""
from types import SimpleNamespace

import pytest

from src.api.account import Position
from src.core.engine import TradingEngine
from src.risk.manager import RiskManager


def make_engine(positions):
    # 엔진이 캐시본의 current_price를 제자리에서 고치므로 매번 새 객체를 준다
    def fresh():
        return {
            t: Position(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
            )
            for t, p in positions.items()
        }

    # start()는 get_positions가 아니라 get_balance_snapshot().positions를 쓴다
    account = SimpleNamespace(
        get_positions=fresh,
        get_balance_snapshot=lambda: SimpleNamespace(
            total_asset=10_000_000, cash=10_000_000, positions=fresh()
        ),
    )
    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=lambda r: None),
        account=account,
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: None),
        risk_manager=RiskManager(commission_rate=0.00015, tax_rate=0.002, slippage_rate=0.001),
    )
    engine.start()
    return engine


def held(ticker, avg, cur, qty):
    return Position(ticker=ticker, quantity=qty, avg_price=avg, current_price=cur)


def test_position_view_carries_net_pnl():
    """2026-09-01 삼성E&A 실매매 — 실제 순손익 +10,068원."""
    engine = make_engine({"047050": held("047050", 55600.0, 56400.0, 15)})

    [view] = engine.position_snapshot()

    assert view.net_pnl == pytest.approx(10068.0, abs=1.0)
    assert view.net_pnl_percent == pytest.approx(10068.0 / (55600 * 15) * 100, abs=0.01)


def test_table_rows_sum_to_the_summary_line():
    """종목별 합계 ≠ 요약줄이면 버그처럼 보인다 — 같은 식으로 계산해야 한다."""
    engine = make_engine(
        {
            "047050": held("047050", 55600.0, 56400.0, 15),
            "012330": held("012330", 453000.0, 447500.0, 1),
            "161390": held("161390", 67100.0, 66900.0, 12),
        }
    )

    rows = engine.position_snapshot()
    amount, _ = engine.portfolio_net_pnl_snapshot()

    assert sum(v.net_pnl for v in rows) == pytest.approx(amount)


def test_summary_amount_and_ratio_match_the_real_fills():
    engine = make_engine(
        {
            "047050": held("047050", 55600.0, 56400.0, 15),
            "012330": held("012330", 453000.0, 447500.0, 1),
            "161390": held("161390", 67100.0, 66900.0, 12),
        }
    )

    amount, ratio = engine.portfolio_net_pnl_snapshot()

    assert amount == pytest.approx(-692.0, abs=1.0)
    assert ratio == pytest.approx(amount / (55600 * 15 + 453000 + 67100 * 12))


def test_display_value_is_better_than_the_exit_judgement():
    """표시는 슬리피지를 빼고 판정은 넣는다 — 표시값이 항상 판정값보다 높아야 한다."""
    engine = make_engine({"047050": held("047050", 55600.0, 56400.0, 15)})

    _, display = engine.portfolio_net_pnl_snapshot()
    judged = engine.portfolio_return_snapshot()

    assert display > judged


def test_snapshot_is_empty_when_nothing_is_held():
    engine = make_engine({})
    assert engine.position_snapshot() == []
    assert engine.portfolio_net_pnl_snapshot() == (0.0, None)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_net_pnl_snapshot.py -v`
Expected: FAIL — `TypeError: PositionView.__init__() got an unexpected keyword argument 'net_pnl'`

- [ ] **Step 3: `PositionView`와 스냅샷을 고친다**

`src/core/engine.py`의 `PositionView`에 필드 두 개를 추가한다 (`current_price` 아래, 프로퍼티 위).

```python
    # 수수료·세금을 뺀 순손익 (표시용 — 슬리피지 없음). 엔진이 채워서 넘긴다.
    # 아래 pnl/pnl_percent는 비용을 빼지 않은 평가손익이라 값이 다르다.
    net_pnl: float = 0.0
    net_pnl_percent: float = 0.0
```

`position_snapshot`의 `PositionView(...)` 생성에 두 값을 넣는다.

```python
        positions = self._positions
        views = []
        for p in positions.values():
            if p.quantity <= 0:
                continue
            net_pnl = self.risk_manager.position_net_pnl(p)
            cost = p.avg_price * p.quantity
            views.append(
                PositionView(
                    ticker=p.ticker,
                    name=p.name,
                    quantity=p.quantity,
                    avg_price=p.avg_price,
                    current_price=p.current_price,
                    net_pnl=net_pnl,
                    net_pnl_percent=(net_pnl / cost * 100) if cost > 0 else 0.0,
                )
            )
        return views
```

`portfolio_return_snapshot` 아래에 표시용 스냅샷을 추가한다.

```python
    def portfolio_net_pnl_snapshot(self) -> Tuple[float, Optional[float]]:
        """화면에 찍을 합산 (순손익 금액, 순손익률) — 슬리피지 없음, API 호출 없음.

        `portfolio_return_snapshot`(판정값)과는 슬리피지만큼 다르다. 이쪽이 항상 조금
        높다. 종목별 표와 같은 식으로 계산해야 표의 합계가 요약줄과 맞는다.
        """
        holdings = [p for t, p in self._positions.items() if t not in self._exiting]
        return self.risk_manager.portfolio_net_pnl(holdings)
```

`typing` import에 `Tuple`을 추가한다.

- [ ] **Step 4: `risk_manager` 스텁 9곳에 수수료율을 넣는다**

아래 9개 파일의 `risk_manager=SimpleNamespace(...)` 안에 두 줄을 추가한다. 값이 0.0이라 기존 단언은 그대로다.

```python
            commission_rate=0.0,
            tax_rate=0.0,
            position_net_pnl=lambda p: (p.current_price - p.avg_price) * p.quantity,
            portfolio_net_pnl=lambda ps: (0.0, None),
```

대상: `tests/test_core/test_carried_over_positions.py`, `test_closeout_report.py`, `test_last_price_cache.py`, `test_open_tickers.py`, `test_position_cache.py`, `test_selected_close.py`, `test_sellable_quantity.py`, `test_unsellable_list.py`, `test_untradable_positions.py`

- [ ] **Step 5: `EngineThread`에 패스스루를 추가한다**

`src/ui/engine_thread.py`의 `portfolio_return` 아래에 넣는다. 같은 파일의 기존 메서드와 같은 꼴(런타임 없으면 조기 반환)을 따른다.

```python
    def portfolio_net_pnl(self) -> tuple:
        """표시용 합산 (순손익 금액, 순손익률). 판정값과 달리 슬리피지가 빠져 있다."""
        runtime = self._runtime
        if runtime is None:
            return (0.0, None)
        return runtime.engine.portfolio_net_pnl_snapshot()
```

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 신규 5건 포함 전부

- [ ] **Step 7: 커밋**

```bash
git add src/core/engine.py src/ui/engine_thread.py tests/test_core/
git commit -F - <<'EOF'
순손익을 UI 스냅샷에 실어 보낸다

PositionView가 종목별 순손익을, 새 스냅샷이 합산 금액과 비율을 넘긴다.
둘을 같은 식으로 계산해 표의 합계가 요약줄과 어긋나지 않게 했다.

판정용 portfolio_return_snapshot은 슬리피지를 계속 포함하므로 표시값이
항상 조금 높다 — 테스트로 그 방향을 고정했다. 엔진 테스트의 risk_manager
스텁에 수수료율을 채웠다 (0.0이라 기존 단언은 그대로다).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 4: 화면의 '손익' 열을 '순손익'으로 바꾼다

**Files:**
- Modify: `src/ui/main_window.py:70`, `:786-796`, `:855-863`

**Interfaces:**
- Consumes: `PositionView.net_pnl` / `.net_pnl_percent`, `EngineThread.portfolio_net_pnl()` (Task 3)
- Produces: 없음 (마지막 작업)

이 저장소에는 UI 테스트가 없다. 계산은 Task 2·3에서 이미 검증됐으므로 여기서는 표시만 바꾸고, 확인은 실행으로 한다.

- [ ] **Step 1: 열 이름을 바꾼다**

`src/ui/main_window.py:70`

```python
HOLDINGS_COLUMNS = ("선택", "종목", "수량", "평단", "현재가", "순손익")
```

- [ ] **Step 2: 셀 값을 순손익으로 바꾼다**

`_refresh_holdings`의 `color`와 `cells`를 고친다. `held.pnl` → `held.net_pnl`이다.

```python
            color = (
                COLOR_PROFIT if held.net_pnl > 0 else COLOR_LOSS if held.net_pnl < 0 else COLOR_TEXT
            )
            cells = (
                (held.label, Qt.AlignmentFlag.AlignLeft, COLOR_TEXT),
                (f"{held.quantity:,}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT),
                (f"{held.avg_price:,.0f}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT),
                (f"{held.current_price:,.0f}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT),
                (
                    f"{held.net_pnl:+,.0f} ({held.net_pnl_percent:+.2f}%)",
                    Qt.AlignmentFlag.AlignRight,
                    color,
                ),
            )
```

- [ ] **Step 3: 요약줄을 같은 기준으로 맞춘다**

`_holdings_summary`를 고친다. 종목별 합계와 어긋나지 않도록 엔진이 준 값을 그대로 쓴다.

```python
    def _holdings_summary(self, rows: list) -> str:
        if self._engine_thread is None:
            return "엔진을 시작하면 매수된 종목이 표시됩니다."
        if not rows:
            return "보유 종목이 없습니다."

        # 표의 종목별 순손익과 같은 식으로 계산된 값이다 — UI가 따로 더하지 않는다
        amount, ratio = self._engine_thread.portfolio_net_pnl()
        percent = f" ({ratio * 100:+.2f}%)" if ratio is not None else ""
        return f"{len(rows)}종목 · 순손익 {amount:+,.0f}원{percent}{self._exit_progress()}"
```

- [ ] **Step 4: 툴팁으로 판정과의 차이를 알린다**

`_refresh_holdings`의 `self._holdings_hint.setText(...)` 바로 아래에 넣는다.

```python
        self._holdings_hint.setToolTip(
            "표시값은 수수료·세금을 뺀 순손익입니다 (현재가에 팔린다고 가정).\n"
            "익절/손절 판정은 여기에 시장가 슬리피지까지 더 빼고 하므로, 표시값이\n"
            "익절선에 닿아도 실제 매도는 조금 뒤에 일어납니다."
        )
```

- [ ] **Step 5: 실행해서 확인한다**

엔진을 이미 켜 두었다면 **먼저 창을 닫아 정지**한다 — 두 인스턴스가 뜨면 토큰이 무효화된다.

Run: `.venv/Scripts/python.exe scripts/run_ui.py`

확인할 것:
- 표 머리글이 '순손익'인가
- 보유 종목이 없으면 "보유 종목이 없습니다."가 그대로 나오는가
- 요약줄에 `N종목 · 순손익 ±N원 (±N.NN%)`가 나오는가 (보유가 있을 때)
- 요약줄에 마우스를 올리면 툴팁이 뜨는가

보유 종목이 없는 시간대라면 표와 요약줄이 비어 있는 것까지만 확인하고, 숫자 확인은 다음 보유일로 미룬다.

- [ ] **Step 6: 전체 테스트를 돌린다**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 7: 커밋**

```bash
git add src/ui/main_window.py
git commit -F - <<'EOF'
보유 종목 표에 순손익을 보여준다

'손익' 열이 단순 평가손익이라 실제로 팔았을 때 남는 금액과 달랐다. 열을
순손익으로 바꾸고 요약줄도 같은 기준으로 맞춘다. 요약줄 숫자는 UI가 따로
더하지 않고 엔진이 준 값을 그대로 쓴다 — 표의 합계와 어긋나면 버그처럼
보인다.

판정은 슬리피지를 계속 포함해 표시값보다 늦게 걸린다. 한 줄에 넣으면
길어져서 툴팁으로 적었다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

## 완료 후 남는 일

- **다음 보유일에 로그를 확인한다.** 잔고 응답에 수수료 필드가 없으면 Task 1의 경고가 `logs/autotrade/<날짜>.log`에 한 줄 남는다. 남았다면 그 로그의 "첫 행 키"를 보고 `_fee_fields`의 후보 키를 넓힌다. 안 남았다면 키움 실제값을 쓰고 있는 것이다.
- 어느 쪽이든 화면 숫자는 실측 오차 1원 이내다 — 급한 일이 아니다.
