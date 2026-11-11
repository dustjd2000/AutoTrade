# 2호 전략 장중 전환(반대 ETF 갈아타기) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 아침에 산 ETF가 틀린 방향으로 드러나면(KODEX 200 가격 반대 ≥ 0.5% AND 5분 체결강도 반대 AND 순손익 < 0) 그날 안에 팔고 반대 ETF를 시장가로 산다 (하루 1회, 2번째 종목은 매도만).

**Architecture:** 순수 로직 두 개(`TradeStrength` 체결강도 창, `SwitchState` 판정기)를 엔진의 시세 콜백이 기존 청산 판정 **뒤에** 부른다. 전환 매도는 기존 `_execute_exit` 경로를 타고, 매수는 `ActionRunner` 큐의 코루틴 단계(`SwitchFlow`)가 매도 체결을 기다린 뒤 `DailyWorkflow.execute_switch_buy`로 낸다. 다리(leg) 기록은 `trades.db`의 `etf_switch_legs`에 남겨 재시작 복원과 사후 대조에 쓴다.

**Tech Stack:** Python 3.14, asyncio, sqlite3, pytest. 외부 라이브러리 추가 없음.

**Spec:** `docs/superpowers/specs/2026-11-11-etf-intraday-switch-design.md` (이 계획의 Task 10이 아래 "스펙과 달라지는 점"을 스펙에 반영한다)

## Global Constraints

- 실행: `.venv\Scripts\python.exe -m pytest ...` (Windows, PowerShell 5.1 — `&&` 금지, `;` 사용).
- 주석·docstring·로그·메일 문구는 한국어, 식별자·커밋 메시지는 영어. 주변 코드의 주석 밀도를 따른다.
- `.env`를 직접 고치지 않는다. 키움·Anthropic API를 부르는 코드를 테스트에서 실제로 실행하지 않는다. 엔진이 떠 있는 동안 `scripts/check_*.py`를 돌리지 않는다.
- 전환 조건(모두 참): `against ≥ ETF_SWITCH_INDEX_PERCENT`(기본 0.5), 체결강도 반대(롱 보유면 `< 100`, 인버스 보유면 `> 100` — 문턱 없음), 순손익 `< 0`.
- 지수·체결강도는 **KODEX 200(069500)** 기준이다. 인버스를 들고 있어도 인버스의 체결강도는 보지 않는다.
- 판정 허용 시각: 15:14까지. 지수 틱이 60초 넘게 없거나 체결강도가 "모름"(5분 미만)이면 판정하지 않는다.
- 1번째 종목 충족 → 매도 후 반대 ETF 매수(하루 1회). 2번째 종목 충족 → 매도만. 손절·익절·고점 반납이 **항상 먼저**다.
- 매도 체결 대기: 5초 간격 60초, 그 뒤 30초 간격, 15:14를 넘으면 포기. 매수 재시도: 10초 간격 3회, 결과 불명이면 재시도하지 않는다.
- 설정은 `.env`만 (UI 노출 없음): `ETF_SWITCH_ENABLED=1`, `ETF_SWITCH_INDEX_PERCENT=0.5`, `ETF_SWITCH_STRENGTH_MINUTES=5`.
- **메일 (사용자 지시 2026-11-12):** 알림은 매도가 끝났을 때와 전환이 일어났을 때 건별 1통. 매매 결과(최종 리포트) 메일은 **모든 매도가 끝난 뒤 1통만** — 전환 매수를 기다리는 동안에는 보내지 않는다. 전환 매수에는 매수 결과 메일을 따로 보내지 않는다.
- 커밋은 각 Task 끝에서 한다(푸시는 하지 않는다). 메시지 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## 스펙과 달라지는 점 (Task 10에서 스펙에 반영)

| 스펙 | 계획 | 이유 |
|---|---|---|
| 5.5 "매수 기록 파일에 전환 매수도 쓴다" | 쓰지 않는다 | 파일에 쓰면 매수 결과 메일이 한 통 더 나간다 (사용자 지시 2026-11-12). 미체결 취소는 체결내역 조회 기준이라 파일 없이도 10:10·15:15가 거둔다 (`_cancel_targets` 1순위) |
| 6 "전환 1통" + 매도 알림 | 전환(`SWITCH`)은 매수 접수 뒤 **1통**(판 종목·산 종목·판정 값), 매도 시점에는 보내지 않는다 | 한 사건에 메일 한 통 (같은 지시) |
| 5.11 최종 리포트 | 전환 매수를 기다리는 동안 "전량 매도 완료" 리포트를 보류 | 1번째 종목 매도 뒤 리포트가 나가고, 2번째 종목 뒤 또 나가는 것을 막는다 (같은 지시) |
| 4.4-5 청산 사유 한글 표가 `templates.py` | 표는 `src/llm/exit_reviewer.py`의 `EXIT_REASON_LABELS` | 실제 위치 |
| 5.3 "결과 불명"을 `order.py`가 분류 | `OrderResult.outcome_unknown` 필드를 새로 둔다 | 지금은 로그로만 구분돼 호출부가 알 수 없다 |
| 5.4 재시작 | 전환 매도 뒤·매수 전에 재시작되면 그날은 다시 사지 않는다(현금 마감, 경고 메일) | 스펙에 없던 경우. 이중 매수 위험이 없는 쪽 |
| 4.4-2 "`watch_ai_exit`의 구독 갱신" | `runtime.run`에서 한 번 구독 | `watch_ai_exit`에는 구독 갱신이 없다. 재접속 복구는 `WebSocketClient.connect`가 한다 |

## File Structure

| 파일 | 책임 |
|---|---|
| `config/settings.py` (수정) | 신설 설정 3개와 검증 |
| `.env.example` (수정) | 신설 키 3개 |
| `src/core/trade_strength.py` (신규) | 5분 체결강도 창 — 순수 로직 |
| `src/core/etf_switch.py` (신규) | `SwitchState`·`Decision`·`PendingSwitch` — 순수 판정 로직 |
| `src/logger/trade_store.py` (수정) | `etf_switch_legs` 테이블, `SwitchLegRow` |
| `src/core/events.py` (수정) | `ExitReason.ETF_SWITCH`, `MarketData.signed_volume`/`strength_cum`, `OrderResult.outcome_unknown` |
| `src/api/websocket_client.py` (수정) | 0B 필드 15·228 파싱 |
| `src/api/order.py` (수정) | 결과 불명 매수에 `outcome_unknown=True` |
| `src/llm/exit_reviewer.py` (수정) | `EXIT_REASON_LABELS`에 `etf_switch` |
| `src/core/engine.py` (수정) | 지수 틱 수집, `_check_etf_switch`, 다리 기록·복원, `pending_switch` |
| `src/core/daily_workflow.py` (수정) | `_place_etf_buy` 추출, `execute_switch_buy`, 아침 매수 뒤 1번째 다리 기록 |
| `src/core/switch_flow.py` (신규) | 매도 체결 대기 → 반대 ETF 매수 코루틴 |
| `src/core/actions.py` (수정) | `switch_buy` 액션 |
| `src/core/runtime.py` (수정) | 조립, 069500 상시 구독, 리포트 보류 |
| `src/research/backtest.py` (수정) | C만의 전환 (`switch_percent`) |
| `src/research/rule_tuning.py` (수정) | 주간 평가에 전환 문턱 전달 |
| `주식자동매매_PRD.md`, `CLAUDE.md`, 스펙 (수정) | 동작 규칙 기록 |

---

### Task 1: 설정 3개

**Files:**
- Modify: `config/settings.py` (`rule_auto_apply_enabled` 필드 아래, `validate`)
- Modify: `.env.example` (`RULE_AUTO_APPLY_ENABLED=1` 줄 아래)
- Test: `tests/test_config/test_settings.py`

**Interfaces:**
- Produces: `Settings.etf_switch_enabled: bool`, `Settings.etf_switch_index_percent: float`, `Settings.etf_switch_strength_minutes: float`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_config/test_settings.py` 끝에 추가

```python
# ── 2호 장중 전환 (스펙 2026-11-11) ─────────────────────────
SWITCH_KEYS = ("ETF_SWITCH_ENABLED", "ETF_SWITCH_INDEX_PERCENT", "ETF_SWITCH_STRENGTH_MINUTES")


def test_etf_switch_defaults(monkeypatch):
    for key in SWITCH_KEYS:
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.etf_switch_enabled is True
    assert settings.etf_switch_index_percent == 0.5
    assert settings.etf_switch_strength_minutes == 5.0


def test_etf_switch_reads_env(monkeypatch):
    settings = _strategy_settings(
        monkeypatch,
        ETF_SWITCH_ENABLED="0", ETF_SWITCH_INDEX_PERCENT="0.7", ETF_SWITCH_STRENGTH_MINUTES="3",
    )
    assert settings.etf_switch_enabled is False
    assert settings.etf_switch_index_percent == 0.7
    assert settings.etf_switch_strength_minutes == 3.0
    settings.validate()


@pytest.mark.parametrize("key", ["ETF_SWITCH_INDEX_PERCENT", "ETF_SWITCH_STRENGTH_MINUTES"])
def test_etf_switch_values_must_be_positive(monkeypatch, key):
    with pytest.raises(ValueError, match=key):
        _strategy_settings(monkeypatch, **{key: "0"}).validate()
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config/test_settings.py -k etf_switch -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'etf_switch_enabled'`

- [ ] **Step 3: 구현** — `config/settings.py`의 `rule_auto_apply_enabled` 필드 바로 아래에 추가

```python
    # 2호 장중 전환 (스펙 2026-11-11) — 산 ETF가 틀린 방향이면 팔고 반대 ETF로 갈아탄다.
    # 0이면 판정만 끈다 (KODEX 200 구독과 체결강도 계산은 그대로 돈다). `.env` 전용.
    etf_switch_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("ETF_SWITCH_ENABLED"), True)
    )
    # KODEX 200이 산 시점 대비 보유 방향과 반대로 이만큼(%) 움직여야 전환한다
    etf_switch_index_percent: float = field(
        default_factory=lambda: float(os.getenv("ETF_SWITCH_INDEX_PERCENT", "0.5"))
    )
    # 체결강도(매수 체결량 ÷ 매도 체결량)를 재는 구간(분). 이만큼 차기 전에는 판정하지 않는다
    etf_switch_strength_minutes: float = field(
        default_factory=lambda: float(os.getenv("ETF_SWITCH_STRENGTH_MINUTES", "5"))
    )
```

`validate()`의 `TRAIL_GIVEBACK_PERCENT` 검사 바로 아래에 추가

```python
        if self.etf_switch_index_percent <= 0:
            raise ValueError(
                f"ETF_SWITCH_INDEX_PERCENT는 0보다 커야 합니다: {self.etf_switch_index_percent}"
            )
        if self.etf_switch_strength_minutes <= 0:
            raise ValueError(
                f"ETF_SWITCH_STRENGTH_MINUTES는 0보다 커야 합니다: {self.etf_switch_strength_minutes}"
            )
```

`.env.example`의 `RULE_AUTO_APPLY_ENABLED=1` 줄 아래에 추가

```
# 장중 전환 — 산 ETF가 틀린 방향이면(KODEX 200이 산 시점 대비 반대로 INDEX_PERCENT% 이상 AND
# 최근 STRENGTH_MINUTES분 체결강도도 반대 AND 순손익 마이너스) 팔고 반대 ETF를 시장가로 산다.
# 하루 1회, 2번째 종목은 같은 조건이면 매도만. 0=판정 끔 (UI에 없음, 바꾸면 엔진 재시작)
ETF_SWITCH_ENABLED=1
ETF_SWITCH_INDEX_PERCENT=0.5
ETF_SWITCH_STRENGTH_MINUTES=5
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config/test_settings.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add config/settings.py .env.example tests/test_config/test_settings.py
git commit -m "Add ETF intraday switch settings"
```

---

### Task 2: 체결강도 계산기 `TradeStrength`

**Files:**
- Create: `src/core/trade_strength.py`
- Test: `tests/test_core/test_trade_strength.py`

**Interfaces:**
- Produces: `TradeStrength(window_minutes: float = 5.0)`, `.window_minutes: float`, `.add(at: datetime, signed_volume: int) -> None`, `.value(now: datetime) -> Optional[float]`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_core/test_trade_strength.py`

```python
"""KODEX 200 5분 체결강도 창 (스펙 2026-11-11 4.1)."""
from datetime import datetime, timedelta

import pytest

from src.core.trade_strength import MAX_STRENGTH, TradeStrength

T0 = datetime(2026, 11, 12, 9, 0, 0)


def feed(strength, start, minutes, buy, sell):
    """`start`부터 `minutes`분 동안 30초마다 매수 `buy`주·매도 `sell`주 체결을 넣는다."""
    at = start
    while at <= start + timedelta(minutes=minutes):
        if buy:
            strength.add(at, buy)
        if sell:
            strength.add(at, -sell)
        at += timedelta(seconds=30)


def test_unknown_until_the_window_is_covered():
    strength = TradeStrength(5)
    feed(strength, T0, 4, buy=10, sell=10)
    assert strength.value(T0 + timedelta(minutes=4)) is None


def test_ratio_of_buy_to_sell_volume_in_percent():
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=30, sell=10)
    assert strength.value(T0 + timedelta(minutes=5)) == pytest.approx(300.0)


def test_ticks_outside_the_window_are_dropped():
    strength = TradeStrength(5)
    strength.add(T0, -10_000)                       # 창 밖으로 밀려날 큰 매도
    feed(strength, T0 + timedelta(seconds=30), 6, buy=10, sell=10)
    assert strength.value(T0 + timedelta(minutes=6, seconds=30)) == pytest.approx(100.0)


def test_no_sell_volume_caps_at_the_maximum():
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=10, sell=0)
    assert strength.value(T0 + timedelta(minutes=5)) == MAX_STRENGTH


def test_no_buy_volume_is_zero():
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=0, sell=10)
    assert strength.value(T0 + timedelta(minutes=5)) == 0.0


def test_a_gap_in_ticks_restarts_the_window():
    """재접속 뒤에는 다시 5분이 필요하다 — 끊긴 사이의 체결을 모른다."""
    strength = TradeStrength(5)
    feed(strength, T0, 5, buy=30, sell=10)
    resumed = T0 + timedelta(minutes=7)             # 2분 공백
    strength.add(resumed, 10)
    assert strength.value(resumed + timedelta(seconds=1)) is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_trade_strength.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.trade_strength'`

- [ ] **Step 3: 구현** — `src/core/trade_strength.py`

```python
"""체결강도 계산기 — 최근 몇 분의 매수 체결량 ÷ 매도 체결량 × 100 (스펙 2026-11-11 4.1).

2호 전략 장중 전환이 "다른 사람들의 매수 추세"를 보는 두 입력 중 하나다. KODEX 200(069500)의
실시간 체결 틱에서 부호 있는 체결량(양수 = 매수 체결, 음수 = 매도 체결)을 받아 쌓는다.
창이 다 차기 전에는 값을 내지 않는다("모름") — 몇 틱만으로 방향을 판정하지 않기 위해서다.
"""
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Optional, Tuple

# 틱이 이보다 오래 끊기면(재접속 등) 그 사이의 체결을 모르므로 창을 처음부터 다시 채운다
GAP_SECONDS = 60.0
# 매도 체결이 하나도 없을 때의 상한 — 0으로 나누지 않는다
MAX_STRENGTH = 999.0


class TradeStrength:
    def __init__(self, window_minutes: float = 5.0):
        self.window_minutes = window_minutes
        self._window = timedelta(minutes=window_minutes)
        self._ticks: Deque[Tuple[datetime, int]] = deque()
        # 창 안 합계 — 틱마다 다시 더하지 않도록 넣고 뺄 때 함께 고친다
        self._buy = 0
        self._sell = 0
        # 끊김 없이 틱을 받기 시작한 시각. 여기서 창 길이만큼 지나야 값을 낸다
        self._covered_from: Optional[datetime] = None
        self._last_at: Optional[datetime] = None

    def add(self, at: datetime, signed_volume: int) -> None:
        if self._last_at is None or (at - self._last_at).total_seconds() > GAP_SECONDS:
            self._ticks.clear()
            self._buy = self._sell = 0
            self._covered_from = at
        self._last_at = at
        if signed_volume > 0:
            self._buy += signed_volume
        elif signed_volume < 0:
            self._sell -= signed_volume
        else:
            return
        self._ticks.append((at, signed_volume))
        self._trim(at)

    def _trim(self, now: datetime) -> None:
        cutoff = now - self._window
        while self._ticks and self._ticks[0][0] < cutoff:
            _, volume = self._ticks.popleft()
            if volume > 0:
                self._buy -= volume
            else:
                self._sell += volume

    def value(self, now: datetime) -> Optional[float]:
        """`now` 기준 창 안의 체결강도. 창이 덜 찼거나 체결이 없으면 None("모름")."""
        if self._covered_from is None or now - self._covered_from < self._window:
            return None
        self._trim(now)
        if self._buy == 0 and self._sell == 0:
            return None
        if self._sell == 0:
            return MAX_STRENGTH
        return min(self._buy / self._sell * 100, MAX_STRENGTH)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_trade_strength.py -v`
Expected: 6 passed

- [ ] **Step 5: 커밋**

```bash
git add src/core/trade_strength.py tests/test_core/test_trade_strength.py
git commit -m "Add the rolling trade-strength window for KODEX 200"
```

---

### Task 3: 전환 판정기 `SwitchState`

**Files:**
- Create: `src/core/etf_switch.py`
- Test: `tests/test_core/test_etf_switch.py`

**Interfaces:**
- Consumes: `src.strategy.etf_regime.LONG_ETF`, `INVERSE_ETF` (각각 `(종목코드, 종목명)` 튜플)
- Produces:
  - `SWITCH_END_TIME = time(15, 14)`, `INDEX_STALE_SECONDS = 60.0`, `SWITCH_REASON = "etf_switch"`
  - `SwitchKind.SWITCH` / `SwitchKind.SELL_ONLY`
  - `Decision(kind: SwitchKind, against: float, strength: float, net: float)` — 값은 모두 % 단위
  - `Leg(leg: int, ticker: str, index_entry: float, started_at: datetime)`
  - `PendingSwitch(sold_ticker, sold_summary, quantity, sell_order_id, buy_ticker, buy_name, index_entry, decision, at)`
  - `opposite_etf(ticker: str) -> Tuple[str, str]`
  - `SwitchState(index_percent: float = 0.5)` — `.index_percent`, `.switched_today: bool`, `.has_leg(leg, today) -> bool`, `.begin_leg(leg, ticker, index_entry, at)`, `.open_leg(ticker, today) -> Optional[Leg]`, `.end_leg(switched: bool = False)`, `.restore(rows, today)`, `.evaluate(ticker, net, index_now, index_at, strength, now) -> Optional[Decision]`
  - `restore`의 `rows`는 `.leg`, `.ticker`, `.index_entry`, `.started_at`, `.ended_at`, `.end_reason` 속성을 가진 객체들 (Task 4의 `SwitchLegRow`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_core/test_etf_switch.py`

```python
"""2호 장중 전환 판정 (스펙 2026-11-11 3절)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.etf_switch import SwitchKind, SwitchState, opposite_etf

LONG, INVERSE = "069500", "114800"
BOUGHT = datetime(2026, 11, 12, 9, 5, 0)
NOW = datetime(2026, 11, 12, 10, 0, 0)


def state(ticker=LONG, leg=1):
    s = SwitchState(index_percent=0.5)
    s.begin_leg(leg, ticker, 10000.0, BOUGHT)
    return s


def decide(s, ticker=LONG, net=-0.3, index_now=9940.0, strength=80.0, now=NOW, index_at=None):
    return s.evaluate(ticker, net, index_now, index_at or now, strength, now)


def test_long_holder_switches_when_the_index_fell_and_sellers_lead():
    decision = decide(state())
    assert decision.kind is SwitchKind.SWITCH
    assert decision.against == pytest.approx(0.6)
    assert (decision.strength, decision.net) == (80.0, -0.3)


def test_inverse_holder_switches_when_the_index_rose_and_buyers_lead():
    decision = decide(state(INVERSE), ticker=INVERSE, index_now=10060.0, strength=130.0)
    assert decision.kind is SwitchKind.SWITCH
    assert decision.against == pytest.approx(0.6)


@pytest.mark.parametrize(
    "overrides",
    [
        {"index_now": 9960.0},          # 지수 반대 0.4% — 문턱 미만
        {"strength": 120.0},            # 롱 보유인데 매수 체결이 우세
        {"strength": 100.0},            # 정확히 100은 반대가 아니다
        {"net": 0.0},                   # 순손익이 마이너스가 아니다
        {"strength": None},             # 체결강도 모름
        {"index_now": None},            # 지수 틱이 아직 없다
    ],
)
def test_any_missing_condition_means_no_switch(overrides):
    assert decide(state(), **overrides) is None


def test_index_moving_in_our_favour_is_not_against():
    assert decide(state(), index_now=10060.0) is None
    assert decide(state(INVERSE), ticker=INVERSE, index_now=9940.0, strength=130.0) is None


def test_second_leg_only_sells():
    assert decide(state(INVERSE, leg=2), ticker=INVERSE, index_now=10060.0, strength=130.0).kind \
        is SwitchKind.SELL_ONLY


def test_no_decision_after_1514():
    s = state()
    assert decide(s, now=NOW.replace(hour=15, minute=14, second=0)) is not None
    assert decide(s, now=NOW.replace(hour=15, minute=14, second=1)) is None


def test_stale_index_tick_means_no_decision():
    s = state()
    assert decide(s, index_at=NOW - timedelta(seconds=60)) is not None
    assert decide(s, index_at=NOW - timedelta(seconds=61)) is None


def test_no_decision_for_a_ticker_without_an_open_leg():
    assert decide(state(), ticker=INVERSE, index_now=10060.0, strength=130.0) is None


def test_ended_leg_is_not_evaluated_and_a_switch_is_remembered():
    s = state()
    s.end_leg(switched=True)
    assert decide(s) is None
    assert s.switched_today is True


def test_state_rolls_over_at_a_new_day():
    s = state()
    s.end_leg(switched=True)
    tomorrow = NOW + timedelta(days=1)
    assert s.has_leg(1, tomorrow.date()) is False
    s.begin_leg(1, LONG, 10000.0, tomorrow)
    assert s.switched_today is False


def row(leg, ticker, ended=False, reason=None):
    return SimpleNamespace(
        leg=leg, ticker=ticker, index_entry=10000.0, started_at=BOUGHT,
        ended_at=NOW if ended else None, end_reason=reason,
    )


def test_restore_reopens_the_first_leg():
    s = SwitchState(0.5)
    s.restore([row(1, LONG)], NOW.date())
    assert s.open_leg(LONG, NOW.date()).index_entry == 10000.0
    assert s.switched_today is False and s.has_leg(1, NOW.date())


def test_restore_reopens_the_second_leg_as_switched():
    s = SwitchState(0.5)
    s.restore([row(1, LONG, ended=True, reason="etf_switch"), row(2, INVERSE)], NOW.date())
    assert s.open_leg(INVERSE, NOW.date()).leg == 2
    assert s.switched_today is True


def test_restore_after_a_switch_sell_without_a_buy_stays_in_cash():
    """전환 매도 뒤·매수 전에 재시작됐다 — 그날은 다시 사지 않는다."""
    s = SwitchState(0.5)
    s.restore([row(1, LONG, ended=True, reason="etf_switch")], NOW.date())
    assert s.open_leg(LONG, NOW.date()) is None
    assert s.switched_today is True


def test_opposite_etf():
    assert opposite_etf(LONG)[0] == INVERSE
    assert opposite_etf(INVERSE)[0] == LONG
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_etf_switch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.etf_switch'`

- [ ] **Step 3: 구현** — `src/core/etf_switch.py`

```python
"""2호 전략 장중 전환 판정 — 산 ETF가 틀린 방향이면 반대 ETF로 갈아탄다 (스펙 2026-11-11 3절).

세 조건이 모두 참일 때만 판정한다: KODEX 200 가격이 산 시점 대비 보유 방향과 반대로 문턱 이상
움직였고(C), KODEX 200 체결강도도 반대이고(B), 보유 종목 순손익이 마이너스다. 그날 1번째 종목이면
전환(매도 후 반대 ETF 매수), 2번째 종목이면 매도만이다.

여기는 순수 로직이다 — 주문·DB·시세 수신은 엔진(`TradingEngine._check_etf_switch`)이 맡는다.
근거가 약한 규칙이다: C만의 백테스트는 어느 문턱에서도 현행보다 나빴고 B는 검증되지 않았다 (스펙 1·11절).
"""
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from enum import Enum
from typing import Iterable, Optional, Set, Tuple

from src.strategy.etf_regime import INVERSE_ETF, LONG_ETF

# 15:15 강제청산과 겹치지 않기 위한 여유 — 이 시각을 넘으면 판정하지 않는다
SWITCH_END_TIME = dt_time(15, 14)
# KODEX 200 틱이 이보다 오래됐으면 지수 가격을 모르는 것으로 본다
INDEX_STALE_SECONDS = 60.0
# `ExitReason.ETF_SWITCH.value` — 다리가 전환으로 끝났는지 가리는 데 쓴다
SWITCH_REASON = "etf_switch"


class SwitchKind(Enum):
    SWITCH = "switch"          # 1번째 종목 — 매도 후 반대 ETF 매수
    SELL_ONLY = "sell_only"    # 2번째 종목 — 매도만, 현금으로 마감


@dataclass(frozen=True)
class Decision:
    """판정 결과와 그 시점의 세 조건 값(모두 %) — 알림과 `etf_switch_legs` 기록에 쓴다."""
    kind: SwitchKind
    against: float
    strength: float
    net: float


@dataclass(frozen=True)
class Leg:
    leg: int                   # 1 = 아침 매수, 2 = 전환 매수
    ticker: str
    index_entry: float         # 이 종목을 산 시점의 KODEX 200 가격
    started_at: datetime


@dataclass(frozen=True)
class PendingSwitch:
    """전환 매도는 나갔고 반대 ETF 매수는 아직인 상태 (`SwitchFlow`가 집어 간다)."""
    sold_ticker: str
    sold_summary: str          # 알림에 실을 판 종목 요약
    quantity: int              # 매도 주문 수량 — 체결 확인의 기준
    sell_order_id: str
    buy_ticker: str
    buy_name: str
    index_entry: float         # 2번째 다리의 기준 지수 가격 = 매도 주문 시점의 KODEX 200
    decision: Decision
    at: datetime


def opposite_etf(ticker: str) -> Tuple[str, str]:
    return INVERSE_ETF if ticker == LONG_ETF[0] else LONG_ETF


class SwitchState:
    """오늘의 다리 상태. 날짜가 바뀌면 스스로 비운다."""

    def __init__(self, index_percent: float = 0.5):
        self.index_percent = index_percent
        self.day: Optional[date] = None
        self.switched_today = False
        self._open: Optional[Leg] = None
        self._seen: Set[int] = set()

    def _roll(self, today: date) -> None:
        if self.day != today:
            self.day = today
            self.switched_today = False
            self._open = None
            self._seen = set()

    def has_leg(self, leg: int, today: date) -> bool:
        """오늘 이 번호의 다리를 이미 시작했는지 (끝났어도 True)."""
        return self.day == today and leg in self._seen

    def begin_leg(self, leg: int, ticker: str, index_entry: float, at: datetime) -> None:
        self._roll(at.date())
        self._open = Leg(leg, ticker, index_entry, at)
        self._seen.add(leg)
        if leg >= 2:
            self.switched_today = True

    def open_leg(self, ticker: str, today: date) -> Optional[Leg]:
        if self.day != today or self._open is None or self._open.ticker != ticker:
            return None
        return self._open

    def end_leg(self, switched: bool = False) -> None:
        """열린 다리를 닫는다. `switched`면 오늘의 전환 1회를 쓴 것으로 남긴다 —
        반대 ETF 매수가 실패해도 다시 시도하지 않는다 (현금 마감)."""
        self._open = None
        if switched:
            self.switched_today = True

    def restore(self, rows: Iterable, today: date) -> None:
        """엔진 재시작 뒤 `etf_switch_legs`의 오늘 행으로 상태를 되살린다 (스펙 5.4).

        전환으로 끝난 1번째 다리만 있고 2번째 다리가 없으면(매도 뒤·매수 전에 재시작) 전환을
        쓴 것으로 본다 — 다시 사면 이중 매수 위험이 있다.
        """
        self._roll(today)
        for row in sorted(rows, key=lambda r: r.leg):
            self._seen.add(row.leg)
            if row.leg >= 2 or row.end_reason == SWITCH_REASON:
                self.switched_today = True
            if row.ended_at is None:
                self._open = Leg(row.leg, row.ticker, row.index_entry, row.started_at)

    def evaluate(
        self,
        ticker: str,
        net: float,
        index_now: Optional[float],
        index_at: Optional[datetime],
        strength: Optional[float],
        now: datetime,
    ) -> Optional[Decision]:
        """세 조건이 모두 참이면 판정을, 아니면 None. `net`은 보유 종목 순손익률(%)."""
        if now.time() > SWITCH_END_TIME:
            return None
        leg = self.open_leg(ticker, now.date())
        if leg is None or leg.index_entry <= 0:
            return None
        if not index_now or index_at is None:
            return None
        if (now - index_at).total_seconds() > INDEX_STALE_SECONDS:
            return None
        if strength is None:
            return None

        move = (index_now / leg.index_entry - 1) * 100
        long = ticker == LONG_ETF[0]
        against = -move if long else move
        strength_against = strength < 100 if long else strength > 100
        if against < self.index_percent or not strength_against or net >= 0:
            return None
        kind = SwitchKind.SWITCH if leg.leg == 1 and not self.switched_today else SwitchKind.SELL_ONLY
        return Decision(kind, against, strength, net)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_etf_switch.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/core/etf_switch.py tests/test_core/test_etf_switch.py
git commit -m "Add the intraday ETF switch decision logic"
```

---

### Task 4: `etf_switch_legs` 테이블

**Files:**
- Modify: `src/logger/trade_store.py` (스키마 상수, `SwitchLegRow`, `TradeStore.__init__`, 메서드 3개)
- Test: `tests/test_logger/test_trade_store.py`

**Interfaces:**
- Produces:
  - `SwitchLegRow(day: date, leg: int, ticker: str, started_at: datetime, index_entry: float, ended_at: Optional[datetime] = None, end_reason: Optional[str] = None, switch_against: Optional[float] = None, switch_strength: Optional[float] = None, switch_net: Optional[float] = None)`
  - `TradeStore.save_switch_leg(day: date, leg: int, ticker: str, started_at: datetime, index_entry: float) -> None` — 같은 (day, leg)가 있으면 덮어쓰지 않는다
  - `TradeStore.close_switch_leg(day: date, leg: int, ended_at: datetime, end_reason: str, against: Optional[float] = None, strength: Optional[float] = None, net: Optional[float] = None) -> None` — 이미 닫힌 다리는 건드리지 않는다
  - `TradeStore.switch_legs_for(day: date) -> List[SwitchLegRow]` — leg 오름차순

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_logger/test_trade_store.py` 끝에 추가 (`make_store(tmp_path)`는 파일에 이미 있다)

```python
# ── 2호 장중 전환 다리 기록 (스펙 2026-11-11 4.3) ────────────
def test_switch_legs_round_trip(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 11, 12)
    bought = datetime(2026, 11, 12, 9, 5, 3)
    switched = datetime(2026, 11, 12, 10, 20, 0)

    store.save_switch_leg(day, 1, "069500", bought, 35000.0)
    store.close_switch_leg(day, 1, switched, "etf_switch", against=0.62, strength=71.0, net=-0.58)
    store.save_switch_leg(day, 2, "114800", switched, 34783.0)

    first, second = store.switch_legs_for(day)
    assert (first.leg, first.ticker, first.index_entry, first.started_at) == (1, "069500", 35000.0, bought)
    assert (first.ended_at, first.end_reason) == (switched, "etf_switch")
    assert (first.switch_against, first.switch_strength, first.switch_net) == (0.62, 71.0, -0.58)
    assert (second.leg, second.ticker, second.ended_at, second.end_reason) == (2, "114800", None, None)
    assert store.switch_legs_for(date(2026, 11, 13)) == []


def test_switch_leg_is_not_overwritten_or_closed_twice(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 11, 12)
    bought = datetime(2026, 11, 12, 9, 5, 3)
    store.save_switch_leg(day, 1, "069500", bought, 35000.0)
    store.save_switch_leg(day, 1, "114800", bought, 1.0)             # 무시된다
    store.close_switch_leg(day, 1, datetime(2026, 11, 12, 10, 0), "stop_loss")
    store.close_switch_leg(day, 1, datetime(2026, 11, 12, 15, 15), "day_end")   # 무시된다

    [row] = store.switch_legs_for(day)
    assert (row.ticker, row.index_entry, row.end_reason) == ("069500", 35000.0, "stop_loss")
    assert row.switch_against is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_logger/test_trade_store.py -k switch_leg -v`
Expected: FAIL — `AttributeError: 'TradeStore' object has no attribute 'save_switch_leg'`

- [ ] **Step 3: 구현** — `src/logger/trade_store.py`

`RULE_EVALUATION_SCHEMA_SQL` 정의 아래에 추가

```python
# 2호 장중 전환의 다리(leg) 기록 — 하루에 많아야 두 줄 (스펙 2026-11-11 4.3). 재시작 때 판정 상태를
# 되살리고, 몇 주 뒤 "체결강도 조건이 되돌림 전환을 걸렀는가"를 2번째 다리 순손익으로 대조한다.
# `trades`와 겹치는 값(체결가·수량·손익)은 넣지 않는다.
ETF_SWITCH_LEG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etf_switch_legs (
    day TEXT NOT NULL,
    leg INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    started_at TEXT NOT NULL,
    index_entry REAL NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    switch_against REAL,
    switch_strength REAL,
    switch_net REAL,
    PRIMARY KEY (day, leg)
)
"""
```

`RuleEvaluationRow` 데이터클래스 정의 **위**에 추가

```python
@dataclass
class SwitchLegRow:
    """2호 장중 전환의 다리 한 줄 — 1은 아침 매수, 2는 전환 매수."""
    day: date
    leg: int
    ticker: str
    started_at: datetime                 # 매수 주문 접수 시각
    index_entry: float                   # 그 시점의 KODEX 200 가격
    ended_at: Optional[datetime] = None
    end_reason: Optional[str] = None     # `trades.exit_reason`과 같은 값
    # 전환(또는 2번째 다리 매도) 판정 시점의 세 조건 값(%) — 전환으로 끝난 다리만
    switch_against: Optional[float] = None
    switch_strength: Optional[float] = None
    switch_net: Optional[float] = None
```

`TradeStore.__init__`의 `conn.execute(RULE_EVALUATION_SCHEMA_SQL)` 아래에 추가

```python
            conn.execute(ETF_SWITCH_LEG_SCHEMA_SQL)
```

`TradeStore.save_rule_evaluation` 메서드 **위**에 추가

```python
    def save_switch_leg(
        self, day: date, leg: int, ticker: str, started_at: datetime, index_entry: float
    ) -> None:
        """전환 다리를 연다. 같은 (날짜, 다리)가 이미 있으면 그대로 둔다 — 재시작 뒤 안전망이
        다시 불러도 처음 잡은 기준 지수 가격이 바뀌지 않는다."""
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO etf_switch_legs (day, leg, ticker, started_at, index_entry)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(day, leg) DO NOTHING""",
                (day.isoformat(), leg, ticker, started_at.isoformat(), index_entry),
            )
            conn.commit()

    def close_switch_leg(
        self,
        day: date,
        leg: int,
        ended_at: datetime,
        end_reason: str,
        against: Optional[float] = None,
        strength: Optional[float] = None,
        net: Optional[float] = None,
    ) -> None:
        """전환 다리를 닫는다. 이미 닫힌 다리는 건드리지 않는다 (15:15 마감 정리가 다시 불러도 무해)."""
        with closing(self._connect()) as conn:
            conn.execute(
                """UPDATE etf_switch_legs
                   SET ended_at = ?, end_reason = ?, switch_against = ?, switch_strength = ?,
                       switch_net = ?
                   WHERE day = ? AND leg = ? AND ended_at IS NULL""",
                (ended_at.isoformat(), end_reason, against, strength, net, day.isoformat(), leg),
            )
            conn.commit()

    def switch_legs_for(self, day: date) -> List[SwitchLegRow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM etf_switch_legs WHERE day = ? ORDER BY leg", (day.isoformat(),)
            ).fetchall()
        return [
            SwitchLegRow(
                day=date.fromisoformat(row["day"]),
                leg=row["leg"],
                ticker=row["ticker"],
                started_at=datetime.fromisoformat(row["started_at"]),
                index_entry=row["index_entry"],
                ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
                end_reason=row["end_reason"],
                switch_against=row["switch_against"],
                switch_strength=row["switch_strength"],
                switch_net=row["switch_net"],
            )
            for row in rows
        ]
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_logger/test_trade_store.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/logger/trade_store.py tests/test_logger/test_trade_store.py
git commit -m "Add the etf_switch_legs table"
```

---

### Task 5: 이벤트 타입·WebSocket 파싱·결과 불명 표시·청산 사유 문구

**Files:**
- Modify: `src/core/events.py` (`ExitReason`, `MarketData`, `OrderResult`)
- Modify: `src/api/websocket_client.py:154-165` (`_parse_trade`)
- Modify: `src/api/order.py:86-94` (`send_order`의 최종 실패 반환)
- Modify: `src/llm/exit_reviewer.py:42-50` (`EXIT_REASON_LABELS`)
- Test: `tests/test_api/test_websocket_client.py`, `tests/test_api/test_order.py`, `tests/test_llm/test_exit_reviewer.py`

**Interfaces:**
- Produces: `ExitReason.ETF_SWITCH` (값 `"etf_switch"`), `MarketData.signed_volume: Optional[int]`, `MarketData.strength_cum: Optional[float]`, `OrderResult.outcome_unknown: bool`, `EXIT_REASON_LABELS["etf_switch"] == "장중 전환"`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_api/test_websocket_client.py` 끝에 추가

```python
# ── 0B 체결 파싱: 부호 있는 체결량(15)·누적 체결강도(228) (스펙 2026-11-11 4.4) ──
def trade_item(values):
    return {"type": "0B", "item": "A069500", "values": {"10": "-35000", "13": "1000", **values}}


def test_parse_trade_reads_signed_volume_and_cumulative_strength():
    data = make_client()._parse_trade(trade_item({"15": "-12", "228": "98.50"}))

    assert (data.ticker, data.price, data.volume) == ("069500", 35000.0, 1000)
    assert data.signed_volume == -12
    assert data.strength_cum == 98.5


def test_parse_trade_leaves_missing_fields_as_none():
    data = make_client()._parse_trade(trade_item({"15": ""}))

    assert data.signed_volume is None
    assert data.strength_cum is None
```

`tests/test_api/test_order.py` 끝에 추가 (`_client_raising`, `_market_order`는 파일에 이미 있다)

```python
def test_unknown_buy_result_is_flagged_and_a_definite_failure_is_not():
    """전환 매수는 결과를 모르는 실패와 확실한 실패를 달리 다룬다 (스펙 2026-11-11 5.3)."""
    client, _ = _client_raising(requests.exceptions.ReadTimeout("read timed out"))
    assert client.send_order(_market_order(OrderSide.BUY)).outcome_unknown is True

    client, _ = _client_raising(requests.exceptions.ConnectTimeout("connect timed out"))
    assert client.send_order(_market_order(OrderSide.BUY)).outcome_unknown is False

    client, _ = _client_raising(requests.exceptions.ReadTimeout("read timed out"))
    assert client.send_order(_market_order(OrderSide.SELL)).outcome_unknown is False
```

`tests/test_llm/test_exit_reviewer.py` 끝에 추가

```python
def test_switch_exit_reason_has_a_label():
    from src.core.events import ExitReason
    from src.llm.exit_reviewer import EXIT_REASON_LABELS

    assert EXIT_REASON_LABELS[ExitReason.ETF_SWITCH.value] == "장중 전환"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api/test_websocket_client.py tests/test_api/test_order.py tests/test_llm/test_exit_reviewer.py -k "parse_trade or unknown_buy or switch_exit" -v`
Expected: FAIL — `AttributeError` (`signed_volume`, `outcome_unknown`, `ETF_SWITCH`)

- [ ] **Step 3: 구현**

`src/core/events.py` — `ExitReason`의 `AI_JUDGMENT` 아래에 추가

```python
    # 2호 장중 전환 (스펙 2026-11-11) — 산 ETF가 틀린 방향이라 팔았다. 1번째 종목이면 반대 ETF를
    # 이어서 사고, 2번째 종목이면 매도만 한다 (둘 다 이 사유로 남긴다).
    ETF_SWITCH = "etf_switch"
```

`MarketData`의 `ask` 아래에 추가

```python
    # 0B 체결 틱의 부호 있는 체결량(양수 = 매수 체결, 음수 = 매도 체결)과 키움 누적 체결강도.
    # 체결강도 계산(`TradeStrength`)과 실측 대조 로그에만 쓴다. 필드가 없으면 None.
    signed_volume: Optional[int] = None
    strength_cum: Optional[float] = None
```

`OrderResult`의 `name` 아래에 추가

```python
    # 거부로 기록했지만 서버가 접수했을 수도 있는 매수 (읽기 타임아웃·502 등, `OrderClient.send_order`).
    # 이 표시가 서면 호출부는 재시도하지 않는다 — 이중 매수가 된다.
    outcome_unknown: bool = False
```

`src/api/websocket_client.py` — `_parse_trade`의 마지막 두 줄(주석과 `return MarketData(...)`)을 교체

```python
        # 10: 현재가(부호 포함), 13: 누적거래량, 15: 체결량(부호 = 매수/매도 체결), 228: 누적 체결강도.
        # 15·228의 필드 번호는 실측 전 가정이다 (스펙 2026-11-11 8절) — 없으면 None으로 둔다.
        price = abs(to_float(values.get("10")))
        if price <= 0:
            return None
        signed = values.get("15")
        strength = values.get("228")
        return MarketData(
            ticker=ticker,
            price=price,
            volume=to_int(values.get("13")),
            signed_volume=to_int(signed) if signed not in (None, "") else None,
            strength_cum=to_float(strength) if strength not in (None, "") else None,
        )
```

(기존의 `# 10: 현재가(부호 포함), 13: 누적거래량` 주석과 `price = ...`, `if price <= 0: return None` 줄은 위 블록으로 대체되므로 지운다.)

`src/api/order.py` — `send_order`의 최종 실패 `return OrderResult(...)`에 인자 하나 추가

```python
                    return OrderResult(
                        order_id=str(uuid.uuid4()),
                        ticker=request.ticker,
                        side=request.side,
                        status=OrderStatus.REJECTED,
                        quantity=request.quantity,
                        error_message=str(e),
                        name=request.name,
                        outcome_unknown=unknown_buy,
                    )
```

`src/llm/exit_reviewer.py` — `EXIT_REASON_LABELS`의 `"trailing_stop"` 줄 아래에 추가

```python
    "etf_switch": "장중 전환",
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api tests/test_llm/test_exit_reviewer.py tests/test_core/test_events.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/core/events.py src/api/websocket_client.py src/api/order.py src/llm/exit_reviewer.py tests/test_api/test_websocket_client.py tests/test_api/test_order.py tests/test_llm/test_exit_reviewer.py
git commit -m "Parse signed trade volume, flag unknown buy results, add the etf_switch exit reason"
```

---

### Task 6: 엔진 — 지수 틱 수집, 전환 판정·매도, 다리 기록·복원

**Files:**
- Modify: `src/core/engine.py` (import, `__init__`, `reset_for_new_day`, `start`, `_sell_positions`, `on_market_data`, `_execute_exit`, 새 메서드들)
- Test: `tests/test_core/test_etf_switch_engine.py` (신규)

**Interfaces:**
- Consumes: Task 2 `TradeStrength`, Task 3 `SwitchState`·`Decision`·`PendingSwitch`·`SwitchKind`·`opposite_etf`·`INDEX_STALE_SECONDS`, Task 4 `TradeStore.save_switch_leg`/`close_switch_leg`/`switch_legs_for`, Task 5 `ExitReason.ETF_SWITCH`·`MarketData.signed_volume`
- Produces:
  - `TradingEngine(..., etf_switch_enabled: bool = False, etf_switch_index_percent: float = 0.5, etf_switch_strength_minutes: float = 5.0)`
  - `engine.switch_state: SwitchState`, `engine.trade_strength: TradeStrength`
  - `engine.pending_switch: Optional[PendingSwitch]`, `engine.switch_pending: bool` (프로퍼티), `engine.finish_switch() -> None`
  - `engine.on_switch: Optional[Callable[[], None]]` — 전환 매도가 접수된 직후 루프 스레드에서 한 번 부른다
  - `engine.begin_switch_leg(leg: int, ticker: str, index_entry: Optional[float] = None, at: Optional[datetime] = None) -> None`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_core/test_etf_switch_engine.py`

```python
"""엔진의 2호 장중 전환 — 판정이 기존 청산 뒤에서 돌고 매도 주문·다리 기록으로 이어지는가 (스펙 2026-11-11).

반대 ETF 매수(매도 체결 대기·재시도)는 `SwitchFlow`가 맡아 `test_switch_flow.py`에서 본다.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.api.account import BalanceSnapshot, Position
from src.core import engine as engine_module
from src.core.engine import TradingEngine
from src.core.events import MarketData, OrderResult, OrderSide, OrderStatus, Signal
from src.logger.trade_store import TradeStore
from src.risk.manager import RiskManager

LONG, INVERSE = "069500", "114800"
NOW = datetime(2026, 11, 12, 10, 0, 0)
BOUGHT = datetime(2026, 11, 12, 9, 5, 0)


class Clock(datetime):
    """엔진이 보는 현재 시각 — 판정이 15:14 마감과 틱 나이를 보므로 고정한다."""
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    Clock.current = NOW
    monkeypatch.setattr(engine_module, "datetime", Clock)


class FakeAccount:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        # 엔진이 캐시본을 변형(current_price)하므로 매번 새 객체를 준다
        return {
            t: Position(ticker=p.ticker, quantity=p.quantity, avg_price=p.avg_price,
                        current_price=p.current_price, name=p.name)
            for t, p in self.positions.items()
        }

    def get_cash(self):
        return 1_000_000.0

    def get_balance_snapshot(self):
        return BalanceSnapshot(cash=self.get_cash(), positions=self.get_positions())


def held(ticker, price):
    return Position(ticker=ticker, quantity=100, avg_price=price, current_price=price, name=ticker)


def make_engine(positions, tmp_path, sell_status=OrderStatus.FILLED, enabled=True):
    """비용 0인 실물 RiskManager와 실물 TradeStore를 붙인 엔진 — 가격 변동률이 곧 순손익률이다."""
    orders, alerts, switched = [], [], []

    def send_order(request):
        orders.append(request)
        return OrderResult(
            order_id=f"S{len(orders)}", ticker=request.ticker, side=request.side,
            status=sell_status, quantity=request.quantity, filled_quantity=request.quantity,
            filled_price=1000.0,
            error_message=None if sell_status != OrderStatus.REJECTED else "거부",
        )

    engine = TradingEngine(
        auth=SimpleNamespace(ensure_token=lambda: "t"),
        market_data=None,
        order_client=SimpleNamespace(send_order=send_order),
        account=FakeAccount(positions),
        strategy=SimpleNamespace(name="s", generate_signal=lambda d: Signal.HOLD),
        risk_manager=RiskManager(
            stop_loss_ratio=0.02, commission_rate=0.0, tax_rate=0.0, slippage_rate=0.0
        ),
        notifier=SimpleNamespace(send=alerts.append),
        trade_store=TradeStore(tmp_path / "t.db"),
        etf_switch_enabled=enabled,
        etf_switch_index_percent=0.5,
        etf_switch_strength_minutes=5,
    )
    engine.on_switch = lambda: switched.append(1)
    engine.start()
    return engine, orders, alerts, switched


def load_strength(engine, buy, sell, until=NOW):
    """`until` 직전 6분 동안 30초마다 매수 `buy`주·매도 `sell`주 체결을 넣어 5분 창을 채운다."""
    at = until - timedelta(minutes=6)
    while at < until:
        engine.trade_strength.add(at, buy)
        engine.trade_strength.add(at, -sell)
        at += timedelta(seconds=30)


def long_tick(price, signed_volume=-5):
    return MarketData(ticker=LONG, price=price, volume=1, signed_volume=signed_volume)


def legs(engine):
    return engine.trade_store.switch_legs_for(NOW.date())


def test_first_leg_switches_when_index_strength_and_loss_all_agree(tmp_path):
    engine, orders, alerts, switched = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))            # 지수·보유 종목 모두 -0.6%

    assert [(o.ticker, o.side) for o in orders] == [(LONG, OrderSide.SELL)]
    pending = engine.pending_switch
    assert (pending.buy_ticker, pending.sell_order_id, pending.quantity) == (INVERSE, "S1", 100)
    assert pending.index_entry == 9940.0                # 2번째 다리 기준 = 매도 주문 시점의 지수
    assert switched == [1]
    assert alerts == []                                 # 전환 메일은 매수 접수 뒤 한 통 (SwitchFlow)
    [row] = legs(engine)
    assert row.end_reason == "etf_switch"
    assert row.switch_against == pytest.approx(0.6)
    assert row.switch_net == pytest.approx(-0.6)
    assert engine.switch_state.switched_today is True
    assert engine.switch_pending is True


def test_stop_loss_runs_first_and_no_switch_follows(tmp_path):
    engine, orders, _, switched = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9700.0))            # -3% — 손절선(-2%)을 넘겼다

    assert len(orders) == 1
    assert engine.pending_switch is None and switched == []
    [row] = legs(engine)
    assert row.end_reason == "stop_loss" and row.switch_against is None


def test_no_switch_while_buyers_still_lead(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=30, sell=10)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []


def test_no_switch_while_the_strength_window_is_not_filled(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []


def test_no_switch_without_a_recorded_index_entry(tmp_path):
    """아침 매수 뒤 기준 지수 가격을 못 남긴 날은 판정이 돌지 않는다 (스펙 5.4)."""
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []


def test_rejected_sell_keeps_the_leg_open_for_a_retry(tmp_path):
    engine, orders, _, switched = make_engine(
        {LONG: held(LONG, 10000.0)}, tmp_path, sell_status=OrderStatus.REJECTED
    )
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert len(orders) == 1
    assert engine.pending_switch is None and switched == []
    assert engine.switch_state.switched_today is False
    assert engine.switch_state.open_leg(LONG, NOW.date()) is not None
    assert legs(engine)[0].ended_at is None


def test_inverse_holder_switches_to_long_on_a_fresh_index_tick(tmp_path):
    engine, orders, _, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.begin_switch_leg(1, INVERSE, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=30, sell=10)

    engine.on_market_data(long_tick(10060.0, signed_volume=5))   # 지수 +0.6% — 아직 인버스 손익 0
    assert orders == []
    engine.on_market_data(MarketData(ticker=INVERSE, price=994.0, volume=1))

    assert [(o.ticker, o.side) for o in orders] == [(INVERSE, OrderSide.SELL)]
    assert engine.pending_switch.buy_ticker == LONG


def test_stale_index_tick_blocks_the_decision(tmp_path):
    engine, orders, _, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.begin_switch_leg(1, INVERSE, index_entry=10000.0, at=BOUGHT)
    Clock.current = NOW - timedelta(seconds=90)
    load_strength(engine, buy=30, sell=10, until=Clock.current)
    engine.on_market_data(long_tick(10060.0, signed_volume=5))
    Clock.current = NOW                                          # 지수 틱이 90초 묵었다

    engine.on_market_data(MarketData(ticker=INVERSE, price=994.0, volume=1))

    assert orders == []


def test_second_leg_only_sells_and_alerts_once(tmp_path):
    engine, orders, alerts, switched = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.begin_switch_leg(2, INVERSE, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=30, sell=10)
    engine.on_market_data(long_tick(10060.0, signed_volume=5))

    engine.on_market_data(MarketData(ticker=INVERSE, price=994.0, volume=1))

    assert [(o.ticker, o.side) for o in orders] == [(INVERSE, OrderSide.SELL)]
    assert engine.pending_switch is None and switched == []
    assert len(alerts) == 1 and "현금으로 마감" in alerts[0]
    assert legs(engine)[0].end_reason == "etf_switch"


def test_disabled_switch_still_collects_the_index_but_never_sells(tmp_path):
    engine, orders, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path, enabled=False)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)

    engine.on_market_data(long_tick(9940.0))

    assert orders == []
    assert engine.trade_strength.value(NOW) is not None


def test_restart_restores_the_open_leg(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)

    restarted, _, alerts, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)

    assert restarted.switch_state.open_leg(LONG, NOW.date()).index_entry == 10000.0
    assert alerts == []


def test_restart_between_the_switch_sell_and_buy_stays_in_cash_and_warns(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    load_strength(engine, buy=10, sell=30)
    engine.on_market_data(long_tick(9940.0))            # 전환 매도까지 나갔다

    restarted, _, alerts, _ = make_engine({}, tmp_path)

    assert restarted.switch_state.switched_today is True
    assert restarted.pending_switch is None
    assert len(alerts) == 1 and "재시작" in alerts[0]


def test_begin_leg_is_recorded_once(tmp_path):
    engine, _, _, _ = make_engine({LONG: held(LONG, 10000.0)}, tmp_path)
    engine.begin_switch_leg(1, LONG, index_entry=10000.0, at=BOUGHT)
    engine.begin_switch_leg(1, LONG, index_entry=9000.0, at=NOW)     # 안전망이 다시 불러도 무시

    assert [row.index_entry for row in legs(engine)] == [10000.0]


def test_begin_leg_uses_the_latest_index_tick_when_no_entry_is_given(tmp_path):
    engine, _, _, _ = make_engine({INVERSE: held(INVERSE, 1000.0)}, tmp_path)
    engine.on_market_data(long_tick(10020.0, signed_volume=5))

    engine.begin_switch_leg(1, INVERSE)

    assert legs(engine)[0].index_entry == 10020.0
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_etf_switch_engine.py -v`
Expected: FAIL — `TypeError: TradingEngine.__init__() got an unexpected keyword argument 'etf_switch_enabled'`

- [ ] **Step 3: 구현** — `src/core/engine.py`

**(a) import** — `from typing import Dict, Iterable, List, Optional, Set, Tuple`를 아래로 바꾸고, `from src.core.exit_trace import ExitTrace` 아래에 세 줄을 더한다

```python
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
```

```python
from src.core.etf_switch import (
    INDEX_STALE_SECONDS,
    Decision,
    PendingSwitch,
    SwitchKind,
    SwitchState,
    opposite_etf,
)
from src.core.trade_strength import TradeStrength
from src.strategy.etf_regime import ETF_TICKERS, LONG_ETF
```

**(b) 상수** — `TRACE_SAMPLE_MINUTES` 정의 아래에 추가

```python
# KODEX 200 틱과 체결강도를 로그에 남기는 간격(초) — 필드 15·228 실측과 우리 5분 값의 대조용
# (스펙 2026-11-11 8절). 틱마다 남기면 로그가 초당 여러 줄씩 쌓인다.
INDEX_LOG_SECONDS = 60.0
```

**(c) `__init__`** — 시그니처 끝의 `ai_exit_drawdown_ratio: float = 0.0,` 아래에 인자 3개 추가

```python
        etf_switch_enabled: bool = False,
        etf_switch_index_percent: float = 0.5,
        etf_switch_strength_minutes: float = 5.0,
```

본문 끝(`self._last_ai_exit: Optional[AIExitView] = None` 아래)에 추가

```python
        # 2호 장중 전환 (스펙 2026-11-11) — KODEX 200 틱에서 지수 가격과 체결강도를 쌓고,
        # 기존 청산이 걸리지 않은 틱에서 전환을 판정한다 (`_check_etf_switch`).
        # 판정을 꺼도(enabled=False) 수집은 돈다 — 켜는 순간 5분을 다시 기다리지 않게.
        self.etf_switch_enabled = etf_switch_enabled
        self.switch_state = SwitchState(etf_switch_index_percent)
        self.trade_strength = TradeStrength(etf_switch_strength_minutes)
        self._index_price: Optional[float] = None
        self._index_at: Optional[datetime] = None
        self._index_logged_at: Optional[datetime] = None
        # 종목별 마지막 청산 주문번호 — 전환이 그 매도의 체결을 기다릴 때 쓴다
        self._exit_order_ids: Dict[str, str] = {}
        # 전환 매도는 나갔고 반대 ETF 매수는 아직인 상태. 이 동안 '전량 매도 완료' 리포트를
        # 보류한다 (`runtime.closeout_report_due`).
        self.pending_switch: Optional[PendingSwitch] = None
        # 전환 매도가 접수된 직후 부른다 — 런타임이 매수 단계를 실행 큐에 넣는다
        self.on_switch: Optional[Callable[[], None]] = None
```

**(d) 공개 메서드** — `position_net_return` 메서드 아래에 추가

```python
    @property
    def switch_pending(self) -> bool:
        """전환 매도 뒤 반대 ETF 매수가 끝나지 않았는지 (UI·런타임 스레드에서 읽는다)."""
        return self.pending_switch is not None

    def finish_switch(self) -> None:
        """전환 매수 단계가 끝났다(성공·실패·포기 모두) — 리포트 보류를 푼다."""
        self.pending_switch = None

    def begin_switch_leg(
        self,
        leg: int,
        ticker: str,
        index_entry: Optional[float] = None,
        at: Optional[datetime] = None,
    ) -> None:
        """매수 주문이 접수된 종목을 전환 판정 대상으로 연다 (스펙 2026-11-11 3.4).

        기준 지수 가격을 넘기지 않으면 KODEX 200 최근 틱을, 틱이 없으면 현재가 조회를 쓴다.
        그것도 실패하면 기록하지 않는다 — 그날은 이 종목의 전환 판정이 돌지 않는다.
        같은 다리를 두 번 열지 않는다 (매수 시각 안전망이 다시 불러도 처음 값이 남는다).
        """
        at = at or datetime.now()
        if self.switch_state.has_leg(leg, at.date()):
            return
        if index_entry is None:
            index_entry = self._fresh_index_price(at)
        if index_entry is None and self.market_data is not None:
            try:
                index_entry = self.market_data.get_current_price(LONG_ETF[0]).price
            except Exception:
                logger.warning("전환 기준 지수 가격 조회 실패", exc_info=True)
        if not index_entry or index_entry <= 0:
            logger.warning(
                "전환 기준 지수 가격을 구하지 못했습니다 — 오늘은 이 종목의 전환 판정이 돌지 않습니다: %s",
                ticker,
            )
            return
        self.switch_state.begin_leg(leg, ticker, index_entry, at)
        logger.info(
            "전환 판정 시작: %d번째 종목 %s, 기준 KODEX 200 %s원", leg, ticker, f"{index_entry:,.0f}"
        )
        if self.trade_store is None:
            return
        try:
            self.trade_store.save_switch_leg(at.date(), leg, ticker, at, index_entry)
        except Exception:
            logger.warning("전환 다리 기록 실패 — 재시작하면 판정 상태가 사라집니다.", exc_info=True)
```

**(e) `reset_for_new_day`** — `self._last_ai_exit = None` 아래에 추가

```python
        # 전환 상태는 날짜가 바뀌면 `SwitchState`가 스스로 비운다 — 여기서는 엔진 쪽 잔여물만 지운다
        self.pending_switch = None
        self._exit_order_ids.clear()
```

**(f) `start`** — `self._restore_exit_peaks()` 아래에 한 줄 추가하고, `_restore_exit_peaks` 메서드 아래에 새 메서드를 둔다

```python
        self._restore_switch_state()
```

```python
    def _restore_switch_state(self) -> None:
        """오늘의 전환 다리를 DB에서 되살린다 (스펙 2026-11-11 5.4).

        체결강도 창은 되살리지 않는다 — 재접속 뒤 다시 5분이 차야 판정이 돈다.
        """
        if self.trade_store is None:
            return
        today = datetime.now().date()
        try:
            rows = self.trade_store.switch_legs_for(today)
        except Exception:
            logger.warning("전환 다리 복원 실패 — 오늘은 전환 판정이 돌지 않습니다.", exc_info=True)
            return
        if not rows:
            if any(t in ETF_TICKERS for t in self._open_tickers):
                logger.warning("보유 ETF의 전환 기준 기록이 없습니다 — 오늘은 전환 판정이 돌지 않습니다.")
            return
        self.switch_state.restore(rows, today)
        logger.info("전환 다리를 복원했습니다: %s", [(r.leg, r.ticker, r.end_reason) for r in rows])
        if self.switch_state.switched_today and not any(r.leg >= 2 for r in rows):
            # 전환 매도 뒤·매수 전에 꺼졌다. 다시 사면 이중 매수 위험이 있어 현금으로 둔다
            self.notify(
                "[경고] 장중 전환 매도 뒤 반대 ETF를 사기 전에 엔진이 재시작됐습니다 — "
                "오늘은 다시 사지 않고 현금으로 마감합니다. 잔고를 확인하세요."
            )
```

**(g) `_sell_positions`** — `self._mark_exited(position.ticker)` 아래에 한 줄 추가

```python
            self._close_switch_leg(position.ticker, reason)
```

**(h) `on_market_data`** — `self._last_prices[data.ticker] = data.price` 아래에 추가

```python
        # KODEX 200 틱은 보유 여부와 무관하게 지수 가격·체결강도로 쌓는다 (스펙 2026-11-11 4.4)
        if data.ticker == LONG_ETF[0]:
            self._note_index_tick(data)
```

`self._sample_exit_trace(positions)` 아래(그리고 `signal = self.strategy.generate_signal(data)` 위)에 추가

```python
        # 기존 청산(손절·익절·고점 반납)이 걸리지 않은 틱에서만 전환을 판정한다 — 손절이 항상 먼저다
        self._check_etf_switch(positions)
```

**(i) `_execute_exit`** — 시그니처를 아래로 바꾼다

```python
    def _execute_exit(
        self,
        position: Position,
        reason: ExitReason,
        exit_reason: Optional[str] = None,
        switch_decision: Optional[Decision] = None,
    ) -> bool:
```

docstring 끝에 한 문단을 더한다

```python
        `switch_decision`은 장중 전환 판정 값이다 — 전환 다리 기록에 함께 남긴다.
```

`self._mark_exited(position.ticker)` 아래에 추가

```python
        self._exit_order_ids[position.ticker] = result.order_id
        self._close_switch_leg(position.ticker, exit_reason or reason.value, switch_decision)
```

**(j) 새 메서드들** — `_execute_exit` 메서드 **위**에 추가

```python
    # ── 2호 장중 전환 (스펙 2026-11-11) ─────────────────────────
    def _note_index_tick(self, data: MarketData) -> None:
        """KODEX 200 틱에서 지수 가격과 체결강도를 갱신한다."""
        now = datetime.now()
        self._index_price = data.price
        self._index_at = now
        if data.signed_volume is not None:
            self.trade_strength.add(now, data.signed_volume)
        if (
            self._index_logged_at is not None
            and (now - self._index_logged_at).total_seconds() < INDEX_LOG_SECONDS
        ):
            return
        self._index_logged_at = now
        strength = self.trade_strength.value(now)
        logger.info(
            "KODEX 200 틱: 가격 %s원, 체결량(필드 15) %s, 키움 누적 체결강도(필드 228) %s, %g분 체결강도 %s",
            f"{data.price:,.0f}",
            data.signed_volume,
            data.strength_cum,
            self.trade_strength.window_minutes,
            "모름" if strength is None else f"{strength:.1f}",
        )

    def _fresh_index_price(self, now: datetime) -> Optional[float]:
        """최근 KODEX 200 틱 가격. 틱이 없거나 `INDEX_STALE_SECONDS`보다 묵었으면 None."""
        if self._index_price is None or self._index_at is None:
            return None
        if (now - self._index_at).total_seconds() > INDEX_STALE_SECONDS:
            return None
        return self._index_price

    def _check_etf_switch(self, positions: Dict[str, Position]) -> None:
        """보유 ETF가 틀린 방향이면 판다 — 1번째 종목이면 반대 ETF 매수를 이어서 접수한다.

        기존 청산이 걸리지 않은 틱에서만 불린다. 전환 매수를 기다리는 동안(`pending_switch`)과
        판정을 끈 동안에는 돌지 않는다.
        """
        if not self.etf_switch_enabled or self.pending_switch is not None:
            return
        now = datetime.now()
        strength = self.trade_strength.value(now)
        for ticker, position in positions.items():
            if ticker not in ETF_TICKERS or ticker in self._exiting or position.quantity <= 0:
                continue
            decision = self.switch_state.evaluate(
                ticker,
                self.position_net_return(position) * 100,
                self._index_price,
                self._index_at,
                strength,
                now,
            )
            if decision is not None:
                self._execute_switch(position, decision, now)
                return

    def _execute_switch(self, position: Position, decision: Decision, now: datetime) -> None:
        """전환 판정대로 판다. 매도가 거부되면 상태를 그대로 둬 다음 틱에 다시 시도한다 (손절과 같다)."""
        note = (
            f"KODEX 200 산 시점 대비 반대로 {decision.against:.2f}% "
            f"(문턱 {self.switch_state.index_percent:g}%), "
            f"{self.trade_strength.window_minutes:g}분 체결강도 {decision.strength:.0f}, "
            f"순손익 {decision.net:+.2f}%"
        )
        summary = _position_summary(position)
        quantity = position.closable_quantity
        index_entry = self._index_price
        if not self._execute_exit(position, ExitReason.ETF_SWITCH, switch_decision=decision):
            return

        if decision.kind is SwitchKind.SELL_ONLY:
            logger.warning("장중 전환 2번째 종목 매도: %s — %s", summary, note)
            self.notify(
                f"장중 전환 2번째 종목 매도 (다시 사지 않고 현금으로 마감): {summary}\n판정: {note}"
            )
            return

        buy_ticker, buy_name = opposite_etf(position.ticker)
        self.pending_switch = PendingSwitch(
            sold_ticker=position.ticker,
            sold_summary=summary,
            quantity=quantity,
            sell_order_id=self._exit_order_ids.get(position.ticker, ""),
            buy_ticker=buy_ticker,
            buy_name=buy_name,
            index_entry=index_entry,
            decision=decision,
            at=now,
        )
        logger.warning(
            "장중 전환: %s 매도 주문 접수 — 체결되면 %s을(를) 시장가로 삽니다 (%s)",
            summary, format_stock(buy_ticker, buy_name), note,
        )
        if self.on_switch is not None:
            try:
                self.on_switch()
            except Exception:
                logger.exception("전환 매수 접수 실패 — 오늘은 현금으로 마감합니다.")
                self.pending_switch = None

    def _close_switch_leg(
        self, ticker: str, reason: str, decision: Optional[Decision] = None
    ) -> None:
        """이 종목의 전환 다리가 열려 있으면 닫는다 — 어떤 사유로 팔렸든 한 번 부른다.

        시세 콜백(청산 경로)에서 불리므로 기록 실패가 예외로 새지 않게 한다.
        """
        now = datetime.now()
        leg = self.switch_state.open_leg(ticker, now.date())
        if leg is None:
            return
        self.switch_state.end_leg(
            switched=decision is not None and decision.kind is SwitchKind.SWITCH
        )
        if self.trade_store is None:
            return
        try:
            self.trade_store.close_switch_leg(
                now.date(),
                leg.leg,
                now,
                reason,
                against=decision.against if decision is not None else None,
                strength=decision.strength if decision is not None else None,
                net=decision.net if decision is not None else None,
            )
        except Exception:
            logger.warning("전환 다리 종료 기록 실패: %s", ticker, exc_info=True)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_etf_switch_engine.py -v`
Expected: 전부 PASS

Run: `.venv\Scripts\python.exe -m pytest tests/test_core -q`
Expected: 전부 PASS (기존 엔진 테스트는 `etf_switch_enabled` 기본값 False라 영향이 없다. 가짜 `trade_store`에 `switch_legs_for`가 없는 테스트는 `_restore_switch_state`의 `except`가 받아 경고 로그만 남긴다.)

- [ ] **Step 5: 커밋**

```bash
git add src/core/engine.py tests/test_core/test_etf_switch_engine.py
git commit -m "Judge the intraday ETF switch in the tick callback and record its legs"
```

---

### Task 7: 워크플로 — `_place_etf_buy` 추출과 `execute_switch_buy`

**Files:**
- Modify: `src/core/daily_workflow.py` (import, `_execute_etf_buy`의 532~583행, 새 메서드 3개)
- Test: `tests/test_core/test_etf_workflow.py`

**Interfaces:**
- Consumes: Task 5 `OrderResult.outcome_unknown`, Task 6 `engine.begin_switch_leg(leg, ticker)`
- Produces:
  - `DailyWorkflow._place_etf_buy(plan: BuyPlan, cash: float, positions) -> Tuple[Optional[BuyRecord], Optional[OrderResult]]` — 가격을 모르면 `(None, None)`
  - `DailyWorkflow.execute_switch_buy(ticker: str, today: date, reason: str) -> Tuple[Optional[BuyRecord], bool]` — 두 번째 값은 "결과 불명"(재시도 금지). 조회 실패는 예외로 올린다

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_core/test_etf_workflow.py`

먼저 파일의 가짜들을 고친다. `FakeOrderClient`를 아래로 교체한다 (결과 불명을 흉내 낼 수 있게)

```python
class FakeOrderClient:
    def __init__(self, status=OrderStatus.PENDING, error=None):
        self.orders = []
        self.status = status
        self.error = error
        self.unknown = False

    def send_order(self, request):
        self.orders.append(request)
        return OrderResult(order_id=str(len(self.orders)), ticker=request.ticker, side=request.side,
                           status=self.status, quantity=request.quantity, error_message=self.error,
                           outcome_unknown=self.unknown)
```

`make_workflow` 안에서 `recorded = []` 아래에 `legs = []`를 더하고, `engine = SimpleNamespace(...)`에 인자 하나를 더한다

```python
        begin_switch_leg=lambda leg, ticker, **kw: legs.append((leg, ticker)),
```

`make_workflow`의 마지막 `return SimpleNamespace(...)`에 `legs=legs`를 더한다

```python
    return SimpleNamespace(
        workflow=workflow, email=email, notifications=notifications, closed=closed,
        market_data=market_data, strategy=strategy, store_calls=store_calls,
        order_client=order_client, opened=opened, legs=legs,
    )
```

그리고 파일 끝에 테스트를 추가한다

```python
# ── 장중 전환 (스펙 2026-11-11) ──────────────────────────────
def test_morning_buy_opens_the_first_switch_leg():
    t = make_workflow(candles(0.5))
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    assert t.legs == [(1, LONG_ETF[0])]


def test_rejected_morning_buy_opens_no_leg():
    t = make_workflow(candles(0.5), order_status=OrderStatus.REJECTED, order_error="잔고 부족")
    t.workflow.recommend_and_notify(today=TODAY)

    t.workflow._execute_etf_buy(TODAY)

    assert t.legs == []


def test_switch_buy_ignores_the_already_bought_today_guard():
    """아침 매수 기록이 있어도 전환 매수는 나간다 — 별도 함수라 '오늘 이미 샀다' 장치를 타지 않는다."""
    t = make_workflow(candles(0.5), cash=1_000_000, price=1250.0)
    t.workflow.recommend_and_notify(today=TODAY)
    t.workflow._execute_etf_buy(TODAY)

    record, unknown = t.workflow.execute_switch_buy(INVERSE_ETF[0], TODAY, "10:20 전환 — 사유")

    assert [o.ticker for o in t.order_client.orders] == [LONG_ETF[0], INVERSE_ETF[0]]
    assert t.order_client.orders[1].order_type == OrderType.MARKET
    assert t.order_client.orders[1].quantity == 400            # 1,000,000 × 50% ÷ 1,250
    assert record.outcome.is_ordered and unknown is False
    assert t.opened == [LONG_ETF[0], INVERSE_ETF[0]]


def test_switch_buy_replaces_the_decision_for_the_ai_exit_prompt():
    t = make_workflow(candles(0.5))
    t.workflow.recommend_and_notify(today=TODAY)
    t.workflow._execute_etf_buy(TODAY)

    t.workflow.execute_switch_buy(INVERSE_ETF[0], TODAY, "10:20 전환 — 사유")

    [rec] = t.strategy._recommendations
    assert rec.ticker == INVERSE_ETF[0]
    assert rec.reason == "10:20 전환 — 사유"
    assert "장중 전환" in rec.outlook


def test_switch_buy_writes_no_buy_record_and_does_not_wake_the_safety_net():
    """전환 매수에는 매수 결과 메일을 따로 보내지 않는다 (사용자 지시 2026-11-12) — 기록 파일은 아침 것 그대로."""
    t = make_workflow(candles(0.5))
    t.workflow.recommend_and_notify(today=TODAY)
    t.workflow._execute_etf_buy(TODAY)

    t.workflow.execute_switch_buy(INVERSE_ETF[0], TODAY, "사유")
    t.workflow._execute_etf_buy(TODAY)                          # 매수 시각 안전망

    assert [r.ticker for r in t.workflow._read_buy_records(TODAY).records] == [LONG_ETF[0]]
    assert len(t.order_client.orders) == 2
    assert t.legs == [(1, LONG_ETF[0])]                         # 2번째 다리는 SwitchFlow가 연다


def test_switch_buy_reports_a_rejection_and_an_unknown_result():
    t = make_workflow(candles(0.5), order_status=OrderStatus.REJECTED, order_error="증거금 부족")

    record, unknown = t.workflow.execute_switch_buy(INVERSE_ETF[0], TODAY, "사유")
    assert not record.outcome.is_ordered and unknown is False

    t.order_client.unknown = True
    record, unknown = t.workflow.execute_switch_buy(INVERSE_ETF[0], TODAY, "사유")
    assert not record.outcome.is_ordered and unknown is True
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_etf_workflow.py -k "switch or leg" -v`
Expected: FAIL — `AttributeError: 'DailyWorkflow' object has no attribute 'execute_switch_buy'` (첫 테스트는 `assert [] == [(1, '069500')]`)

- [ ] **Step 3: 구현** — `src/core/daily_workflow.py`

**(a) import** — `from typing import Iterable, List, Optional`를 아래로 바꾸고, events import 목록에 `OrderResult`를 더하고, strategy.base import에 `BuyPlan`을 더한다

```python
from typing import Iterable, List, Optional, Tuple
```

```python
from src.core.events import (
    BuyExecution,
    BuyOutcome,
    BuyPlanView,
    BuyRecord,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    TuningOutcome,
    format_stock,
)
```

```python
from src.strategy.base import BuyPlan, StockRecommendation
```

**(b) 상수** — `MARKET_BUY_MARGIN = 1.3` 아래에 추가

```python
# 장중 전환으로 산 종목의 '아침 전망' 칸 — AI 매도 판단이 이것을 시나리오로 읽는다 (스펙 2026-11-11 6절)
SWITCH_OUTLOOK = (
    "2호 전략 장중 전환: 시나리오는 '전환 시점의 장중 흐름이 이어진다'이며, "
    "그 흐름이 다시 꺾였다면 시나리오가 깨진 것이다."
)
```

**(c) `_execute_etf_buy`** — `plan = plans[0]`부터 메서드 끝까지(현재 532~583행)를 아래로 교체한다. 그 위(중복 매수 가드·결정·`plans` 계산)는 그대로 둔다

```python
        plan = plans[0]
        record, _ = self._place_etf_buy(plan, cash, positions)
        if record is None:
            return

        self._set_buy_board(today, self._board_from_records([record]))
        # 접수된 주문만 남긴다 — 이 파일이 "오늘 이미 주문함" 표시라, 실패까지 남기면 매수 시각
        # 안전망이 재시도하지 못한다
        if record.outcome == BuyOutcome.ORDERED:
            self._write_buy_records(cash, plan.amount, [record])
            self._begin_switch_leg(1, plan.ticker)
```

**(d) 새 메서드 3개** — `_execute_etf_buy` 아래, `_etf_kill_reason` 위에 추가. `_place_etf_buy`의 본문은 지금 `_execute_etf_buy`에 있던 코드를 그대로 옮긴 것이다 (바뀐 것은 가격을 모를 때 `(None, None)`을 돌려주는 것과 `result`를 함께 돌려주는 것뿐)

```python
    def _place_etf_buy(
        self, plan: BuyPlan, cash: float, positions
    ) -> Tuple[Optional[BuyRecord], Optional[OrderResult]]:
        """ETF 한 종목을 시장가로 산다 — 아침 매수와 장중 전환 매수가 함께 쓴다.

        수량 계산(배정액·증거금 배수) → 리스크 승인 → 주문 → 기록 → 감시 등록·구독까지다.
        가격을 알 수 없으면 `(None, None)`. 주문을 냈으면 그 응답을 함께 돌려준다 —
        전환 매수가 "결과 불명"(`outcome_unknown`)을 가려 재시도를 멈추는 데 쓴다.
        """
        label = format_stock(plan.ticker, plan.name)
        try:
            price = self.engine.market_data.get_current_price(plan.ticker).price
        except Exception:
            logger.exception("2호 전략 매수 직전 현재가 조회 실패 — 기준가로 수량을 정합니다: %s", label)
            price = float(plan.recommend_price or plan.target_price)
        if price <= 0:
            logger.error("2호 전략 매수 가격을 알 수 없습니다 — 매수를 건너뜁니다: %s", label)
            return None, None

        # 키움은 시장가 매수 증거금을 상한가(+30%) 기준으로 잡는다 — 그 한도 안에서 수량을 정한다
        quantity = int(min(plan.amount // price, cash // (price * MARKET_BUY_MARGIN)))
        record = BuyRecord(ticker=plan.ticker, name=plan.name, quantity=quantity, reference_price=price)
        if quantity <= 0:
            record = replace(record, outcome=BuyOutcome.SKIPPED,
                             note=f"1주 {price:,.0f}원을 살 금액이 없습니다 (배정 {plan.amount:,.0f}원)")
            logger.warning("2호 전략 매수 건너뜀: %s — %s", label, record.note)
            return record, None

        request = OrderRequest(
            ticker=plan.ticker, side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=quantity, price=None, name=plan.name,
        )
        if not self.engine.risk_manager.approve(request, positions, reference_price=price):
            self.engine.notify(f"매수 거부 (리스크 관리): {label}")
            return replace(record, outcome=BuyOutcome.FAILED,
                           note="리스크 관리 규칙에 걸려 주문하지 않았습니다"), None

        result = self.engine.order_client.send_order(request)
        self.engine.risk_manager.record_order(result)
        self.trade_store.record_fill(result)
        if result.status == OrderStatus.REJECTED:
            if _is_market_closed_rejection(result.error_message):
                self.engine.note_market_closed("주문이 '장이 열리지않는 날'로 거부됐습니다")
            self.engine.notify(f"[실패] 2호 전략 매수 거부: {label} x{quantity}주 — {result.error_message}")
            return replace(record, outcome=BuyOutcome.FAILED, order_id=result.order_id,
                           note=f"주문 거부: {result.error_message}"), result

        self.engine.note_open_position(plan.ticker)
        self._clear_report_mark()
        if self.ws_client is not None:
            self.ws_client.subscribe([plan.ticker])
        logger.info(
            "2호 전략 시장가 매수 접수: %s %d주 (현재가 %s원, 주문번호 %s)",
            label, quantity, f"{price:,.0f}", result.order_id,
        )
        return replace(record, order_id=result.order_id), result

    def _begin_switch_leg(self, leg: int, ticker: str) -> None:
        """매수가 접수된 종목을 장중 전환 판정 대상으로 연다 (스펙 2026-11-11 3.4).

        주문은 이미 나갔으므로 여기서 실패해도 매수 흐름을 막지 않는다 — 그날 전환 판정만 빠진다.
        """
        begin = getattr(self.engine, "begin_switch_leg", None)
        if begin is None:
            return
        try:
            begin(leg, ticker)
        except Exception:
            logger.exception("전환 다리 기록 실패 — 오늘은 전환 판정이 돌지 않습니다: %s", ticker)

    def execute_switch_buy(
        self, ticker: str, today: date, reason: str
    ) -> Tuple[Optional[BuyRecord], bool]:
        """장중 전환 — 반대 ETF를 시장가로 산다 (스펙 2026-11-11 5.1). 엔진 루프 스레드에서 부른다.

        아침 매수(`_execute_etf_buy`)의 "오늘 이미 샀으면 건너뜀"·"결정 없으면 다시 결정" 장치를
        타지 않는다 — 전환은 아침 매수가 있었던 날에만 일어난다. 수량·리스크 승인·주문·기록·구독은
        `_place_etf_buy`를 함께 쓴다.

        오늘의 결정을 새 종목으로 갈아끼워 AI 매도 판단 프롬프트가 전환 근거를 읽게 한다.
        **매수 기록 파일에는 쓰지 않는다** — 쓰면 매수 결과 메일이 한 통 더 나간다(사용자 지시
        2026-11-12, 전환 알림 메일이 대신한다). 미체결분은 체결내역 조회 기준이라 10:10·15:15가
        그대로 거둔다. 2번째 다리 기록은 호출부(`SwitchFlow`)가 한다 — 기준 지수 가격이
        매도 주문 시점 값이어야 하기 때문이다.

        돌려주는 두 번째 값이 True면 주문 결과를 모르는 것이다 — 재시도하면 이중 매수가 된다.
        잔고·예수금·현재가 조회 실패는 예외로 올린다 (호출부가 재시도한다).
        """
        name = dict((etf_regime.LONG_ETF, etf_regime.INVERSE_ETF))[ticker]
        positions = self.account.get_positions()
        cash = self.account.get_cash()
        price = self.engine.market_data.get_current_price(ticker).price
        recommendation = StockRecommendation(
            ticker=ticker,
            name=name,
            target_price=int(price),
            reason=reason,
            recommend_price=price,
            setup=etf_regime.SETUP,
            outlook=SWITCH_OUTLOOK,
        )
        self.strategy.set_recommendations([recommendation])
        plan = self.strategy.build_buy_plans(cash)[0]

        record, result = self._place_etf_buy(plan, cash, positions)
        if record is None:
            return None, False
        self._set_buy_board(today, self._board_from_records([record]))
        return record, bool(result is not None and result.outcome_unknown)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_etf_workflow.py tests/test_core/test_daily_workflow.py tests/test_core/test_buy_result_timing.py tests/test_core/test_market_holiday.py -v`
Expected: 전부 PASS (기존 아침 매수 테스트가 그대로 통과해야 추출이 동작을 바꾸지 않은 것이다)

- [ ] **Step 5: 커밋**

```bash
git add src/core/daily_workflow.py tests/test_core/test_etf_workflow.py
git commit -m "Extract the shared ETF buy and add the switch buy to the workflow"
```

---

### Task 8: 전환 매수 흐름 `SwitchFlow`와 런타임 연결

**Files:**
- Create: `src/core/switch_flow.py`
- Modify: `src/core/actions.py` (`SCHEDULED_ACTIONS`, `step_factories`)
- Modify: `src/core/runtime.py` (import, `Runtime`, `build_runtime`, `closeout_report_due`, `run`, 새 함수 `subscribe_index`)
- Test: `tests/test_core/test_switch_flow.py` (신규), `tests/test_core/test_actions.py`, `tests/test_core/test_closeout_report.py`

**Interfaces:**
- Consumes: Task 3 `PendingSwitch`·`SWITCH_END_TIME`, Task 6 `engine.pending_switch`·`engine.finish_switch()`·`engine.begin_switch_leg(...)`·`engine.on_switch`·`engine.switch_pending`, Task 7 `workflow.execute_switch_buy(ticker, today, reason)`
- Produces:
  - `SwitchFlow(engine, workflow, sleep=asyncio.sleep, now=datetime.now)`, `async SwitchFlow.run() -> None`
  - `sell_filled(fills, order_id: str, quantity: int) -> bool`
  - 액션 이름 `"switch_buy"` (스케줄 전용, 버튼 없음)
  - `Runtime.switch: Optional[SwitchFlow]`, `runtime.subscribe_index(runtime) -> None`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_core/test_switch_flow.py` (신규)

```python
"""장중 전환의 매수 단계 — 매도 체결을 기다렸다가 반대 ETF를 산다 (스펙 2026-11-11 5.1·5.3)."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.core.etf_switch import Decision, PendingSwitch, SwitchKind
from src.core.events import BuyOutcome, BuyRecord, FillRecord, OrderSide
from src.core.switch_flow import SwitchFlow, sell_filled

LONG, INVERSE = "069500", "114800"
NOW = datetime(2026, 11, 12, 10, 0, 0)


def pending():
    return PendingSwitch(
        sold_ticker=LONG, sold_summary="(069500)KODEX 200 100주", quantity=100, sell_order_id="S1",
        buy_ticker=INVERSE, buy_name="KODEX 인버스", index_entry=9940.0,
        decision=Decision(SwitchKind.SWITCH, 0.6, 33.0, -0.6), at=NOW,
    )


def sell_fill(filled=100, unfilled=0, order_id="S1"):
    return FillRecord(order_id=order_id, ticker=LONG, side=OrderSide.SELL,
                      filled_quantity=filled, filled_price=9940.0, unfilled_quantity=unfilled)


def bought(outcome=BuyOutcome.ORDERED, unknown=False):
    return (
        BuyRecord(ticker=INVERSE, name="KODEX 인버스", outcome=outcome, quantity=50,
                  reference_price=1000.0, order_id="B1"),
        unknown,
    )


def make(fill_pages, buy_results, now=NOW):
    """`fill_pages`는 체결내역 조회가 차례로 돌려줄 목록들(다 쓰면 마지막 것을 되풀이),
    `buy_results`는 `execute_switch_buy`가 차례로 돌려줄 값(예외 객체면 던진다)."""
    t = SimpleNamespace(notes=[], legs=[], sleeps=[], buys=[], clock=now)
    pages = list(fill_pages)

    def get_today_fills():
        return pages.pop(0) if len(pages) > 1 else pages[0]

    def execute_switch_buy(ticker, today, reason):
        t.buys.append((ticker, today, reason))
        result = buy_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def sleep(seconds):
        t.sleeps.append(seconds)
        t.clock += timedelta(seconds=seconds)

    engine = SimpleNamespace(
        pending_switch=pending(),
        order_client=SimpleNamespace(get_today_fills=get_today_fills),
        notify=t.notes.append,
        begin_switch_leg=lambda leg, ticker, index_entry=None, at=None: t.legs.append(
            (leg, ticker, index_entry)
        ),
    )
    engine.finish_switch = lambda: setattr(engine, "pending_switch", None)
    t.engine = engine
    t.flow = SwitchFlow(
        engine, SimpleNamespace(execute_switch_buy=execute_switch_buy),
        sleep=sleep, now=lambda: t.clock,
    )
    return t


def run(t):
    asyncio.run(t.flow.run())


def test_sell_filled_needs_the_whole_order_and_no_unfilled_rest():
    assert sell_filled([sell_fill()], "S1", 100) is True
    assert sell_filled([sell_fill(filled=60, unfilled=40)], "S1", 100) is False
    assert sell_filled([sell_fill(filled=60)], "S1", 100) is False
    assert sell_filled([sell_fill(order_id="S9")], "S1", 100) is False
    assert sell_filled([], "S1", 100) is False


def test_buys_the_opposite_etf_once_the_sell_is_filled():
    t = make([[sell_fill()]], [bought()])

    run(t)

    assert [(b[0], b[1]) for b in t.buys] == [(INVERSE, NOW.date())]
    assert "10:00 전환" in t.buys[0][2] and "체결강도 33" in t.buys[0][2]
    assert t.legs == [(2, INVERSE, 9940.0)]           # 기준 지수 = 매도 주문 시점 값
    assert len(t.notes) == 1
    assert "장중 전환" in t.notes[0] and "KODEX 인버스" in t.notes[0] and "50주" in t.notes[0]
    assert t.engine.pending_switch is None
    assert t.sleeps == []


def test_waits_for_the_fill_every_five_seconds():
    t = make([[], [sell_fill(filled=60, unfilled=40)], [sell_fill()]], [bought()])

    run(t)

    assert t.sleeps == [5.0, 5.0]
    assert len(t.buys) == 1


def test_slows_to_thirty_seconds_after_the_first_minute():
    t = make([[]] * 13 + [[sell_fill()]], [bought()])

    run(t)

    assert t.sleeps == [5.0] * 12 + [30.0]
    assert len(t.buys) == 1


def test_a_failed_fill_query_counts_as_not_filled_yet():
    t = make([[sell_fill()]], [bought()])
    calls = []
    real = t.engine.order_client.get_today_fills

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("502")
        return real()

    t.engine.order_client.get_today_fills = flaky

    run(t)

    assert t.sleeps == [5.0] and len(t.buys) == 1


def test_gives_up_after_1514_without_buying():
    t = make([[]], [], now=NOW.replace(hour=15, minute=13, second=50))

    run(t)

    assert t.buys == [] and t.legs == []
    assert len(t.notes) == 1 and "[경고]" in t.notes[0] and "사지 않았습니다" in t.notes[0]
    assert t.engine.pending_switch is None


def test_three_failed_buys_end_the_day_in_cash():
    t = make([[sell_fill()]], [bought(BuyOutcome.FAILED), RuntimeError("조회 실패"), bought(BuyOutcome.FAILED)])

    run(t)

    assert len(t.buys) == 3
    assert t.sleeps == [10.0, 10.0]
    assert t.legs == []
    assert len(t.notes) == 1 and "[경고]" in t.notes[0] and "현금으로 마감" in t.notes[0]
    assert t.engine.pending_switch is None


def test_unknown_buy_result_is_not_retried():
    t = make([[sell_fill()]], [bought(BuyOutcome.FAILED, unknown=True), bought()])

    run(t)

    assert len(t.buys) == 1
    assert t.legs == []
    assert len(t.notes) == 1 and "잔고를 확인" in t.notes[0]


def test_unaffordable_buy_is_not_retried():
    t = make([[sell_fill()]], [bought(BuyOutcome.SKIPPED), bought()])

    run(t)

    assert len(t.buys) == 1 and t.sleeps == []
    assert "현금으로 마감" in t.notes[0]


def test_nothing_happens_without_a_pending_switch():
    t = make([[sell_fill()]], [bought()])
    t.engine.pending_switch = None

    run(t)

    assert t.buys == [] and t.notes == []
```

`tests/test_core/test_actions.py` 끝에 추가

```python
def test_switch_buy_is_a_scheduled_only_coroutine_step_on_the_loop():
    """전환 매수는 버튼이 없고, 루프 스레드에서 코루틴으로 돈다 — 매도 체결을 기다리는 동안
    루프를 막지 않는다 (스펙 2026-11-11 5.1)."""
    import asyncio

    ran = []

    async def flow():
        ran.append("switch")

    runtime = make_runtime([])
    runtime.switch = SimpleNamespace(run=flow)

    [step] = manual_steps(runtime, "switch_buy")

    assert "switch_buy" in SCHEDULED_ACTIONS and "switch_buy" not in MANUAL_ACTIONS
    assert step.touches_orders is True
    asyncio.run(step.run())
    assert ran == ["switch"]
```

`tests/test_core/test_closeout_report.py` — `test_not_due_for_yesterdays_closeout` 아래에 추가 (`make_runtime`, `NOW`, `CLOSEOUT_SETTLE_SECONDS`는 파일에 이미 있다)

```python
def test_not_due_while_a_switch_buy_is_pending():
    """전환 매도로 보유가 잠시 비어도 결과 리포트는 모든 매도가 끝난 뒤 한 번만 보낸다
    (사용자 지시 2026-11-12)."""
    runtime = make_runtime(NOW - timedelta(seconds=CLOSEOUT_SETTLE_SECONDS + 1))
    runtime.engine.switch_pending = True
    assert closeout_report_due(runtime, NOW) is False
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_switch_flow.py tests/test_core/test_actions.py tests/test_core/test_closeout_report.py -k "switch" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.switch_flow'`, `ValueError: Unknown manual action: switch_buy`, `assert True is False`

- [ ] **Step 3: 구현**

**(a) `src/core/switch_flow.py` (신규)**

```python
"""2호 장중 전환의 매수 단계 — 매도 체결을 기다렸다가 반대 ETF를 시장가로 산다 (스펙 2026-11-11 5절).

엔진이 시세 콜백에서 전환 매도를 내면(`TradingEngine._execute_switch`) 런타임이 이 단계를 실행 큐
(`ActionRunner`)에 넣는다. 코루틴이라 기다리는 동안 이벤트 루프를 막지 않는다 — WebSocket PING 응답과
실시간 손절 감시가 그대로 돈다. 다만 **이 단계가 끝날 때까지 실행 큐를 잡고 있어** 그 사이 스케줄·버튼
실행은 뒤에서 기다린다 (손절·익절·AI 매도 판단은 큐를 타지 않는다).

실패는 언제나 "사지 않음"으로 떨어진다 — 그날은 현금으로 마감한다.
"""
import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Iterable

from src.core.etf_switch import SWITCH_END_TIME, PendingSwitch
from src.core.events import BuyOutcome, FillRecord, OrderSide, format_stock

logger = logging.getLogger(__name__)

# 매도 체결 확인 — 처음 1분은 촘촘히, 그 뒤로는 느슨하게 본다
FILL_POLL_SECONDS = 5.0
FILL_FAST_WINDOW_SECONDS = 60.0
FILL_SLOW_POLL_SECONDS = 30.0
# 반대 ETF 매수 재시도 (거부·조회 오류). 결과를 모르는 실패는 재시도하지 않는다
BUY_ATTEMPTS = 3
BUY_RETRY_SECONDS = 10.0


def sell_filled(fills: Iterable[FillRecord], order_id: str, quantity: int) -> bool:
    """그 매도 주문이 남김없이 체결됐는지.

    `DailyWorkflow.buy_orders_filled`와 같은 잣대다 — 같은 주문번호의 행을 모두 보고, 미체결
    잔량이 하나라도 있으면 아직이며, 가장 큰 체결수량이 주문수량 이상이어야 한다.
    """
    rows = [f for f in fills if f.order_id == order_id and f.side == OrderSide.SELL]
    if not rows or any(f.unfilled_quantity > 0 for f in rows):
        return False
    return max(f.filled_quantity for f in rows) >= quantity


class SwitchFlow:
    def __init__(
        self,
        engine,
        workflow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.engine = engine
        self.workflow = workflow
        self._sleep = sleep
        self._now = now

    async def run(self) -> None:
        pending = self.engine.pending_switch
        if pending is None:
            logger.info("전환 매수: 기다리는 전환이 없습니다 — 건너뜁니다.")
            return
        try:
            if not await self._wait_sell_fill(pending):
                logger.error("장중 전환: 매도 체결을 확인하지 못해 매수를 포기합니다: %s", pending.sold_summary)
                self.engine.notify(
                    f"[경고] 장중 전환: 매도 체결을 {SWITCH_END_TIME:%H:%M}까지 확인하지 못해 "
                    f"반대 ETF를 사지 않았습니다 — 15:15 마감 정리가 남은 것을 거둡니다: "
                    f"{pending.sold_summary}"
                )
                return
            await self._buy(pending)
        finally:
            # 성공·실패·포기 모두 여기서 끝난다 — '전량 매도 완료' 리포트 보류를 푼다
            self.engine.finish_switch()

    async def _wait_sell_fill(self, pending: PendingSwitch) -> bool:
        """매도 체결을 기다린다. 15:14를 넘기면 False."""
        loop = asyncio.get_running_loop()
        waited = 0.0
        warned = False
        while True:
            if self._now().time() > SWITCH_END_TIME:
                return False
            try:
                # 체결내역 조회는 블로킹 HTTP라 루프 밖에서 부른다
                fills = await loop.run_in_executor(None, self.engine.order_client.get_today_fills)
            except Exception:
                logger.warning("장중 전환: 매도 체결 조회 실패 — 다시 봅니다.", exc_info=True)
                fills = []
            if sell_filled(fills, pending.sell_order_id, pending.quantity):
                return True

            slow = waited >= FILL_FAST_WINDOW_SECONDS
            if slow and not warned:
                warned = True
                logger.warning(
                    "장중 전환: 매도가 %.0f초 안에 체결되지 않았습니다 — %.0f초마다 다시 봅니다: %s",
                    FILL_FAST_WINDOW_SECONDS, FILL_SLOW_POLL_SECONDS, pending.sold_summary,
                )
            interval = FILL_SLOW_POLL_SECONDS if slow else FILL_POLL_SECONDS
            await self._sleep(interval)
            waited += interval

    async def _buy(self, pending: PendingSwitch) -> None:
        decision = pending.decision
        label = format_stock(pending.buy_ticker, pending.buy_name)
        reason = (
            f"{pending.at:%H:%M} 전환 — 지수가 산 시점 대비 반대로 {decision.against:.2f}% 움직였고, "
            f"체결강도 {decision.strength:.0f}"
        )
        for attempt in range(1, BUY_ATTEMPTS + 1):
            if self._now().time() > SWITCH_END_TIME:
                break
            record, unknown = None, False
            try:
                # 주문은 루프 스레드에서 그대로 낸다 — 실시간 손절 감시와 직렬화한다
                record, unknown = self.workflow.execute_switch_buy(
                    pending.buy_ticker, self._now().date(), reason
                )
            except Exception:
                logger.exception("장중 전환 매수 시도 %d/%d 실패", attempt, BUY_ATTEMPTS)

            if record is not None and record.outcome == BuyOutcome.ORDERED:
                self.engine.begin_switch_leg(
                    2, pending.buy_ticker, index_entry=pending.index_entry, at=self._now()
                )
                self.engine.notify(
                    f"장중 전환: {pending.sold_summary} 매도 → {label} {record.quantity}주 시장가 매수\n"
                    f"판정: KODEX 200 산 시점 대비 반대로 {decision.against:.2f}%, "
                    f"체결강도 {decision.strength:.0f}, 순손익 {decision.net:+.2f}%"
                )
                return
            if unknown:
                self.engine.notify(
                    f"[경고] 장중 전환 매수 결과를 알 수 없습니다 — 다시 주문하지 않습니다. "
                    f"잔고를 확인하세요: {label}"
                )
                return
            if record is not None and record.outcome == BuyOutcome.SKIPPED:
                break  # 살 금액이 없다 — 다시 해도 같다
            if attempt < BUY_ATTEMPTS:
                await self._sleep(BUY_RETRY_SECONDS)

        self.engine.notify(
            f"[경고] 장중 전환 매수 실패 — 오늘은 현금으로 마감합니다: {label} "
            f"(매도: {pending.sold_summary})"
        )
```

**(b) `src/core/actions.py`** — `SCHEDULED_ACTIONS`의 `"evaluate_rules"` 줄 아래에 추가

```python
    # 2호 장중 전환의 매수 단계 (스펙 2026-11-11) — 스케줄 시각이 아니라 엔진이 전환 매도를 낸
    # 직후에 접수한다 (`TradingEngine.on_switch`). 버튼이 없어 여기 둔다.
    "switch_buy": "장중 전환 매수 (매도 체결 대기 → 반대 ETF 매수)",
```

`step_factories`의 `"evaluate_rules"` 항목 아래에 추가

```python
        # 코루틴 단계다 — 매도 체결을 기다리는 동안 루프를 막지 않는다. 매수 주문은 그 안에서
        # 루프 스레드로 나가므로 실시간 손절 감시와 직렬화된다 (`SwitchFlow`).
        "switch_buy": lambda: [
            ManualStep(SCHEDULED_ACTIONS["switch_buy"], runtime.switch.run, touches_orders=True)
        ],
```

**(c) `src/core/runtime.py`**

import — `from src.core.etf_exit_trigger import StepTrigger` 아래에 추가

```python
from src.core.switch_flow import SwitchFlow
```

`Runtime` 데이터클래스 — `research: Optional[RuleTuning] = None` 아래에 추가

```python
    # 2호 장중 전환의 매수 단계 (스펙 2026-11-11) — 엔진이 전환 매도를 내면 실행 큐가 이것을 돌린다
    switch: Optional[SwitchFlow] = None
```

`build_runtime` — `TradingEngine(...)` 호출에 인자 3개 추가 (`ai_exit_drawdown_ratio=...` 아래)

```python
        etf_switch_enabled=settings.etf_switch_enabled,
        etf_switch_index_percent=settings.etf_switch_index_percent,
        etf_switch_strength_minutes=settings.etf_switch_strength_minutes,
```

`build_runtime` — `runtime.runner = ActionRunner(runtime)` 아래에 추가

```python
    runtime.switch = SwitchFlow(engine, workflow)
    # 엔진은 시세 콜백(루프 스레드)에서 이 콜백을 부른다 — 매수 단계를 버튼·스케줄과 같은 큐에 넣는다
    engine.on_switch = lambda: runtime.runner.submit("switch_buy")
```

`closeout_report_due` — `if closed_at is None: return False` 아래에 추가

```python
    # 장중 전환 매도로 보유가 잠시 빈 것이다 — 반대 ETF 매수가 끝나기 전에는 보내지 않는다.
    # 결과 리포트는 모든 매도가 끝난 뒤 한 번만 나간다 (사용자 지시 2026-11-12).
    if getattr(runtime.engine, "switch_pending", False):
        return False
```

`adopt_carried_over_positions` 함수 아래에 새 함수 추가

```python
def subscribe_index(runtime: Runtime) -> None:
    """KODEX 200(069500)을 항상 구독한다 — 장중 전환의 지수 가격·체결강도 입력이다 (스펙 2026-11-11 4.4).

    보유 여부나 전환 판정을 켰는지와 무관하게 건다 — 인버스를 든 날에도 필요하고, 체결강도는
    5분이 차야 값이 나오므로 09:00부터 쌓여 있어야 한다. 재접속 때는 `WebSocketClient.connect`가
    구독을 복구한다.
    """
    runtime.ws_client.subscribe([etf_regime.LONG_ETF[0]])
```

`run` — `adopt_carried_over_positions(runtime)` 아래에 추가

```python
    subscribe_index(runtime)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_switch_flow.py tests/test_core/test_actions.py tests/test_core/test_closeout_report.py tests/test_core/test_manual_actions.py tests/test_core/test_carried_over_positions.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/core/switch_flow.py src/core/actions.py src/core/runtime.py tests/test_core/test_switch_flow.py tests/test_core/test_actions.py tests/test_core/test_closeout_report.py
git commit -m "Wire the switch buy flow into the action queue and hold the closeout report"
```

---

### Task 9: 백테스터와 주간 평가에 C만의 전환

**Files:**
- Modify: `src/research/backtest.py` (`trade_return`, 새 함수 `_leg`·`switch_day_return`, `Backtester`)
- Modify: `src/research/rule_tuning.py` (`evaluate_rules`의 `backtester_factory` 호출, 새 메서드 `_switch_percent`)
- Test: `tests/test_research/test_backtest.py`, `tests/test_research/test_rule_tuning.py`

**Interfaces:**
- Consumes: Task 1 `Settings.etf_switch_enabled`·`etf_switch_index_percent`
- Produces:
  - `SWITCH_END_HHMM = "1514"`
  - `switch_day_return(first_bars, second_bars, index_bars, first_is_long: bool, entry_hhmm: str, stop_percent: float, commission_ratio: float, switch_percent: float, half_tick: float = HALF_TICK_WON, take_profit_percent: float = 0.0, trail_activate_percent: float = 0.0, trail_giveback_percent: float = 0.0) -> Optional[float]`
  - `Backtester(long_bars, inverse_bars, commission_ratio, take_profit_percent=0.0, trail_activate_percent=0.0, trail_giveback_percent=0.0, switch_percent=0.0)` — 7번째 위치 인자
  - `trade_return`의 시그니처와 결과는 그대로다

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_research/test_backtest.py` — import 줄을 바꾸고 끝에 테스트를 추가

```python
from src.research.backtest import Backtester, Rule, switch_day_return, trade_return
```

```python
# ── 장중 전환 (C만 — 체결강도는 분봉으로 재현하지 못한다, 스펙 2026-11-11 8절) ──
def net(buy, sell):
    return (sell / buy - 1 - 2 * COMMISSION) * 100


def test_switch_sells_the_first_leg_and_rides_the_opposite_etf():
    long_bars = flat_day(10000, {"1000": 9940})                  # 지수·롱 -0.6%
    inverse_bars = flat_day(1000, {"1100": 1010})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    # 10:00 봉에 롱을 팔고 같은 봉의 인버스(1,000원)로 갈아타 15:15까지 보유
    assert result == pytest.approx(net(10000, 9940) + net(1000, 1010))


def test_second_leg_only_sells_when_the_index_turns_again():
    long_bars = flat_day(10000, {"1000": 9940, "1100": 10010})   # 2번째 다리 기준(9,940) 대비 +0.7%
    inverse_bars = flat_day(1000, {"1100": 995, "1200": 900})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    # 11:00 봉에 인버스를 팔고 끝낸다 — 12:00의 급락은 맞지 않고, 롱을 다시 사지도 않는다
    assert result == pytest.approx(net(10000, 9940) + net(1000, 995))


def test_no_switch_while_the_index_move_is_below_the_threshold():
    long_bars = flat_day(10000, {"1000": 9960})                  # -0.4%
    inverse_bars = flat_day(1000, {"1100": 1010})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    assert result == pytest.approx(net(10000, 9960))


def test_no_switch_after_1514():
    long_bars = flat_day(10000, {"1515": 9940})
    inverse_bars = flat_day(1000, {"1520": 1100})
    result = switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    assert result == pytest.approx(net(10000, 9940))             # 15:15 청산일 뿐 전환이 아니다


def test_inverse_first_leg_switches_to_long_when_the_index_rises():
    long_bars = flat_day(10000, {"1000": 10060, "1100": 10160})
    inverse_bars = flat_day(1000, {"1000": 994})
    result = switch_day_return(
        inverse_bars, long_bars, long_bars, False, "0910", 2.0, COMMISSION, 0.5, half_tick=0.0
    )
    assert result == pytest.approx(net(1000, 994) + net(10060, 10160))


def test_switch_percent_zero_matches_the_plain_trade():
    long_bars = flat_day(10000, {"1000": 9940})
    inverse_bars = flat_day(1000, {"1100": 1010})
    assert switch_day_return(
        long_bars, inverse_bars, long_bars, True, "0910", 2.0, COMMISSION, 0.0
    ) == trade_return(long_bars, "0910", 2.0, COMMISSION)


def test_backtester_adds_both_legs_when_switching_is_on():
    long_bars, inverse_bars = make_history(True)
    long_bars["20261021"] = flat_day(10000, {"1000": 9940})
    inverse_bars["20261021"] = flat_day(1000, {"1100": 1010})
    rule = Rule(0.0, True, "09:10", 2.0)

    plain = Backtester(long_bars, inverse_bars, COMMISSION).day_return("20261021", rule)
    switched = Backtester(long_bars, inverse_bars, COMMISSION, switch_percent=0.5).day_return(
        "20261021", rule
    )

    first = ((9940 - 2.5) / (10000 + 2.5) - 1 - 2 * COMMISSION) * 100
    second = ((1010 - 2.5) / (1000 + 2.5) - 1 - 2 * COMMISSION) * 100
    assert plain == pytest.approx(first)
    assert switched == pytest.approx(first + second)
```

`tests/test_research/test_rule_tuning.py` 끝에 추가

```python
def test_weekly_evaluation_backtests_with_the_live_switch_threshold(tmp_path):
    """실전 규칙에서 체결강도만 뺀 것을 평가한다 — 전환을 끄면 0을 넘긴다 (스펙 2026-11-11 8절)."""
    captured = []

    def factory(*args):
        captured.append(args)
        return SimpleNamespace(days=list(range(200)))

    for enabled, expected in ((True, 0.5), (False, 0.0)):
        settings = FakeSettings()
        settings.etf_switch_enabled = enabled
        settings.etf_switch_index_percent = 0.5
        # 같은 날을 두 번 평가하므로 DB를 따로 쓴다 (rule_evaluations.day는 UNIQUE)
        workdir = tmp_path / ("on" if enabled else "off")
        workdir.mkdir()
        tuning, _ = make(workdir, good, settings=settings)
        tuning.backtester_factory = factory
        tuning.evaluate_rules(date(2026, 11, 13))
        assert captured[-1][6] == expected
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research/test_backtest.py tests/test_research/test_rule_tuning.py -v`
Expected: FAIL — `ImportError: cannot import name 'switch_day_return'`

- [ ] **Step 3: 구현**

**(a) `src/research/backtest.py`**

모듈 docstring의 마지막 문장("AI 매도 판단은 재현하지 않는다.") 뒤에 한 문단을 더한다

```python

장중 전환(스펙 2026-11-11)은 **지수 가격 조건(C)만** 재현한다 — 1분봉에는 매수/매도 체결 구분이 없어
체결강도 조건(B)을 넣을 수 없다. 그래서 백테스트는 실전보다 전환이 많다("B 없이 가장 자주 전환했을 때").
```

`HALF_TICK_WON` 정의 아래에 추가

```python
# 장중 전환 판정 마감 — 실전(`etf_switch.SWITCH_END_TIME`)과 같다
SWITCH_END_HHMM = "1514"
```

`trade_return` 함수 전체를 아래 세 함수로 교체한다

```python
def _leg(
    bars: List[Bar],
    entry_index: int,
    stop_percent: float,
    commission_ratio: float,
    half_tick: float,
    take_profit_percent: float,
    trail_activate_percent: float,
    trail_giveback_percent: float,
    index_bars: Optional[List[Bar]] = None,
    is_long: bool = True,
    switch_percent: float = 0.0,
) -> Tuple[Optional[float], Optional[str], bool]:
    """`entry_index` 봉에 사서 청산 조건에 처음 닿은 봉에 판다. (순손익률, 판 봉의 HHMM, 전환 여부).

    청산 조건(실전과 같은 순서): 순손익률 ≤ −손절, ≥ +익절, 고점(≥ 활성 문턱)에서 반납폭만큼 하락,
    15:15 이후 첫 봉. 그 어느 것도 아니면 전환 조건을 본다 — `switch_percent`가 0보다 크고, 지수
    (`index_bars` = KODEX 200)가 산 봉 대비 보유 방향과 반대로 그만큼 움직였고, 순손익이 마이너스이며
    15:14 이전이다. 익절·고점 반납·전환은 0이면 끈다. 산 뒤에 봉이 없으면 순손익률은 None.
    """
    entry_time, entry_price = bars[entry_index]
    entry = entry_price + half_tick
    index_by_time: Dict[str, float] = dict(index_bars) if index_bars and switch_percent > 0 else {}
    # 산 봉의 지수 가격 — 그 분에 지수 봉이 없으면 그 앞의 마지막 봉
    index_entry = next((p for t, p in reversed(index_bars or []) if t <= entry_time), None)
    index_now = index_entry
    net, sold_at = None, None
    peak = float("-inf")
    for t, price in bars[entry_index + 1:]:
        net = ((price - half_tick) / entry - 1 - 2 * commission_ratio) * 100
        sold_at = t
        peak = max(peak, net)
        if (
            net <= -stop_percent
            or (take_profit_percent > 0 and net >= take_profit_percent)
            or (trail_giveback_percent > 0 and peak >= trail_activate_percent
                and peak - net >= trail_giveback_percent)
            or t >= CLOSE_OUT_HHMM
        ):
            return net, t, False
        if index_by_time and index_entry:
            index_now = index_by_time.get(t, index_now)
            move = (index_now / index_entry - 1) * 100
            against = -move if is_long else move
            if against >= switch_percent and net < 0 and t <= SWITCH_END_HHMM:
                return net, t, True
    return net, sold_at, False


def trade_return(
    bars: List[Bar],
    entry_hhmm: str,
    stop_percent: float,
    commission_ratio: float,
    half_tick: float = HALF_TICK_WON,
    take_profit_percent: float = 0.0,
    trail_activate_percent: float = 0.0,
    trail_giveback_percent: float = 0.0,
) -> Optional[float]:
    """`entry_hhmm`(HHMM) 이후 첫 봉에 사서, 청산 조건에 처음 닿은 봉에 판다 (`_leg` 참고).

    순손익률(%) = (매도가 − 반호가) / (매수가 + 반호가) − 1 − 수수료 양방향. 진입할 봉이 없으면 None.
    """
    entry_index = next((i for i, (t, _) in enumerate(bars) if t >= entry_hhmm), None)
    if entry_index is None:
        return None
    return _leg(
        bars, entry_index, stop_percent, commission_ratio, half_tick,
        take_profit_percent, trail_activate_percent, trail_giveback_percent,
    )[0]


def switch_day_return(
    first_bars: List[Bar],
    second_bars: List[Bar],
    index_bars: List[Bar],
    first_is_long: bool,
    entry_hhmm: str,
    stop_percent: float,
    commission_ratio: float,
    switch_percent: float,
    half_tick: float = HALF_TICK_WON,
    take_profit_percent: float = 0.0,
    trail_activate_percent: float = 0.0,
    trail_giveback_percent: float = 0.0,
) -> Optional[float]:
    """장중 전환을 넣은 하루 순수익률(%) — 두 다리의 합 (스펙 2026-11-11 8절).

    1번째 다리(`first_bars`)가 전환 조건에 닿으면 그 봉에 팔고 같은 봉의 반대 ETF(`second_bars`)를
    사서 2번째 다리를 시작한다. 2번째 다리는 같은 조건이면 팔고 끝낸다(다시 사지 않는다).
    `switch_percent`가 0이면 `trade_return`과 같다. 진입할 봉이 없으면 None.
    """
    entry_index = next((i for i, (t, _) in enumerate(first_bars) if t >= entry_hhmm), None)
    if entry_index is None:
        return None
    exits = (stop_percent, commission_ratio, half_tick,
             take_profit_percent, trail_activate_percent, trail_giveback_percent)
    first, sold_at, switched = _leg(
        first_bars, entry_index, *exits,
        index_bars=index_bars, is_long=first_is_long, switch_percent=switch_percent,
    )
    if not switched:
        return first
    second_index = next((i for i, (t, _) in enumerate(second_bars) if t >= sold_at), None)
    if second_index is None:
        return first
    second, _, _ = _leg(
        second_bars, second_index, *exits,
        index_bars=index_bars, is_long=not first_is_long, switch_percent=switch_percent,
    )
    return first + (second or 0.0)
```

`Backtester.__init__` — 시그니처 끝에 인자를 더하고 본문 첫 줄에 저장한다

```python
        trail_giveback_percent: float = 0.0,
        switch_percent: float = 0.0,
    ):
        # 장중 전환의 지수 문턱(%) — 0이면 전환을 넣지 않는다 (체결강도 조건은 재현하지 못한다)
        self._switch = switch_percent
```

`Backtester._trade` — `if key not in self._trades:` 블록을 아래로 교체

```python
        if key not in self._trades:
            is_long = ticker == LONG_ETF[0]
            bars = self._long[day] if is_long else self._inverse[day]
            exits = dict(
                take_profit_percent=self._take_profit,
                trail_activate_percent=self._trail_activate,
                trail_giveback_percent=self._trail_giveback,
            )
            if self._switch > 0:
                other = self._inverse[day] if is_long else self._long[day]
                self._trades[key] = switch_day_return(
                    bars, other, self._long[day], is_long, hhmm, stop_percent,
                    self._commission, self._switch, **exits,
                )
            else:
                self._trades[key] = trade_return(bars, hhmm, stop_percent, self._commission, **exits)
```

**(b) `src/research/rule_tuning.py`** — `evaluate_rules`의 `backtester_factory(...)` 호출에서 `getattr(self.settings, "trail_giveback_percent", 0.0),` 아래에 한 줄 추가

```python
                self._switch_percent(),
```

`current_rule` 메서드 아래에 추가

```python
    def _switch_percent(self) -> float:
        """백테스트에 넣을 장중 전환 문턱(%) — 실전에서 전환을 껐으면 0 (스펙 2026-11-11 8절).

        실전 규칙에서 체결강도 조건만 뺀 것을 평가한다. 문턱 자체는 자동 조정 후보가 아니다.
        """
        if not getattr(self.settings, "etf_switch_enabled", False):
            return 0.0
        return getattr(self.settings, "etf_switch_index_percent", 0.0)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research -v`
Expected: 전부 PASS (기존 `trade_return`·`Backtester` 테스트가 그대로 통과해야 한다)

- [ ] **Step 5: 커밋**

```bash
git add src/research/backtest.py src/research/rule_tuning.py tests/test_research/test_backtest.py tests/test_research/test_rule_tuning.py
git commit -m "Model the index-only intraday switch in the backtester and weekly evaluation"
```

---

### Task 10: 문서 반영과 전체 검증

**Files:**
- Modify: `주식자동매매_PRD.md` (5.5절 안의 2호 전략 소절 끝 — `### 5.6 리스크 관리` 바로 위, 그리고 5.11절)
- Modify: `CLAUDE.md` ("리스크 관리는 이중 구조" 절의 고점 반납 항목 아래, "스레드 / 이벤트 루프 구조" 절)
- Modify: `docs/superpowers/specs/2026-11-11-etf-intraday-switch-design.md`

- [ ] **Step 1: PRD에 장중 전환 소절 추가** — `### 5.6 리스크 관리 (필수, 시스템 레벨 강제)` 줄 바로 위에 삽입

```markdown
#### 2호 전략 — 장중 전환 (신설 2026-11-12, 설계 2026-11-11)

아침에 산 ETF가 **틀린 방향**으로 드러나면 그날 안에 팔고 반대 ETF로 갈아탄다. 설계 문서는
`docs/superpowers/specs/2026-11-11-etf-intraday-switch-design.md`다.

**판정** (보유 ETF의 틱마다, 기존 청산이 걸리지 않았을 때만 — 손절·익절·고점 반납이 항상 먼저다). 세 조건이
모두 참이어야 한다.

1. **지수 반대 폭**: KODEX 200(069500) 가격이 이 종목을 **산 시점 대비** 보유 방향과 반대로
   `ETF_SWITCH_INDEX_PERCENT`(기본 0.5%) 이상 움직였다. 롱 보유면 하락, 인버스 보유면 상승이 반대다.
2. **체결강도 반대**: KODEX 200 최근 `ETF_SWITCH_STRENGTH_MINUTES`(기본 5)분의 매수 체결량 ÷ 매도 체결량
   × 100이 롱 보유면 100 미만, 인버스 보유면 100 초과다. 문턱은 없다. 인버스를 들고 있어도 **인버스 ETF의
   체결강도는 보지 않는다.**
3. **순손익 마이너스**: 손절과 같은 순손익률이 0 미만이다. 플러스를 거쳤는지는 보지 않는다.

판정하지 않는 경우: 15:14 이후, KODEX 200 틱이 60초 넘게 없을 때, 체결강도 창이 덜 찼을 때(구독·재접속 뒤
5분), 기준 지수 가격을 남기지 못한 날, `ETF_SWITCH_ENABLED=0`. 069500은 보유와 무관하게 항상 구독한다.

**결과**
- **1번째 종목**(아침 매수)에서 충족 → 시장가 매도 → 매도 체결 확인 → 반대 ETF 시장가 매수. **하루 1회.**
- **2번째 종목**(전환 매수)에서 충족 → 시장가 매도만. 다시 사지 않고 현금으로 마감한다.
- 플러스에서 팔렸거나(AI 판단·익절·고점 반납) 손절로 팔렸으면 그날 끝이다 — 전환 없음.
- 기존 청산(손절·익절·고점 반납·AI 매도 판단·15:15 강제청산)은 두 종목 모두 그대로 적용된다.
- 매도 사유는 `trades.exit_reason='etf_switch'`("장중 전환")이다 — 2번째 종목의 매도만도 같다.

**주문과 실패 처리**
- 매도 체결을 5초 간격으로 60초, 그 뒤 30초 간격으로 기다린다. 15:14를 넘으면 매수를 포기한다. 기다리는
  동안 실행 큐를 잡고 있어 스케줄·버튼 실행은 뒤에서 기다린다 (손절·익절·AI 매도 판단은 큐를 타지 않는다).
- 매도 주문이 거부되면 전환하지 않은 것으로 두고 다음 틱에 다시 판정한다 (손절과 같다).
- 매수가 거부되거나 조회가 실패하면 10초 간격으로 3회까지 다시 시도하고, 모두 실패하면 현금으로 마감한다.
  주문 결과를 알 수 없는 실패(읽기 타임아웃·502 등)는 다시 주문하지 않는다 — 이중 매수가 된다.
- 전환 매도 뒤·매수 전에 엔진이 재시작되면 그날은 다시 사지 않는다 (현금 마감, 경고 메일).
- 수량은 아침 매수와 같은 식(예수금 × 투입 비율, 시장가 증거금 1.3배 한도)을 매도 체결 뒤의 예수금으로
  다시 계산한다.

**메일** (사용자 지시 2026-11-12)
- 전환 1건에 알림 1통 — 반대 ETF 매수가 접수된 뒤, 판 종목·산 종목·수량·세 조건 값을 한 통에 싣는다.
  2번째 종목의 매도만도 1통. 실패(체결 미확인·매수 실패·결과 불명)는 경고 메일.
- 전환 매수에는 **매수 결과 메일을 따로 보내지 않는다** (매수 기록 파일에 쓰지 않는다).
- **매매 결과 리포트는 모든 매도가 끝난 뒤 한 번만** 보낸다 — 전환 매도로 보유가 잠시 비어도, 반대 ETF
  매수 단계가 끝나기 전에는 "전량 매도 완료" 리포트를 보내지 않는다 (5.11).

**AI 매도 판단 연동**: 전환 뒤에는 오늘의 결정이 새 종목으로 바뀐다. 프롬프트의 "아침 근거"는 전환 시각과
판정 값, "아침 전망"은 "전환 시점의 장중 흐름이 이어진다 — 그 흐름이 다시 꺾였다면 시나리오가 깨진 것"이다.

**기록**: `trades.db`의 `etf_switch_legs`에 다리마다 한 줄(종목·시작 시각·기준 지수 가격·끝난 시각과 사유·
판정 값)을 남긴다. 재시작 때 판정 상태를 되살리고, 실매매 결과를 백테스트와 같은 잣대로 대조하는 표다.

**근거와 한계 — 근거가 음수다.** 지수 가격 조건만으로 돌린 1분봉 백테스트(249거래일)는 어느 문턱에서도
현행(-0.173%/일)보다 나빴다(-0.228 ~ -0.324%/일). 손실은 전부 2번째 종목에서 났다 — 장중 지수는 1분
단위에서 추세보다 되돌림이 강하다. 체결강도 조건은 과거 데이터가 없어 **검증되지 않았다**. "체결강도를
AND로 걸면 되돌림 전환이 걸러진다"는 가정 위의 실험이며, 사용자가 이 결과를 알고도 그림자 모드 대신
실매매를 택했다(2026-11-11). 몇 주 뒤 `etf_switch_legs`의 2번째 다리 순손익이 플러스가 아니면
`ETF_SWITCH_ENABLED=0`으로 끈다 — 이 판단은 사람이 한다. 주간 규칙 자동 평가의 백테스트는 체결강도를
재현하지 못해 **지수 가격 조건만** 넣는다(실전보다 전환이 많다). 문턱은 자동 조정 후보가 아니다.

**실측 확인 필요**: 0B 체결 데이터의 부호 있는 체결량(필드 15)과 누적 체결강도(필드 228)는 필드 번호가
가정이다. 로그의 `KODEX 200 틱:` 줄(1분 간격)로 첫 거래일에 확인하고, 그때까지 `ETF_SWITCH_ENABLED=0`으로 둔다.
```

- [ ] **Step 2: PRD 5.11에 한 줄 추가** — `### 5.11 일일/월간/연간 성과 리포트 이메일` 절에서 "보유 종목 전량 매도"로 15:35를 기다리지 않고 리포트를 보내는 규칙을 설명하는 문단 바로 아래에 삽입 (`전량 매도`로 검색해 그 문단을 찾는다)

```markdown
- **장중 전환 중에는 보류한다** (2026-11-12): 전환 매도로 보유가 잠시 비어도, 반대 ETF 매수 단계가 끝나기
  전에는 "전량 매도 완료" 리포트를 보내지 않는다. 결과 리포트는 모든 매도가 끝난 뒤 한 번만 나간다.
```

- [ ] **Step 3: CLAUDE.md 갱신**

"리스크 관리는 이중 구조" 절에서 `- **고점 반납 매도가 있다** ...` 항목 아래에 항목 하나를 추가

```markdown
- **장중 전환이 있다** (2026-11-12 신설, 스펙 2026-11-11, `.env`의 `ETF_SWITCH_ENABLED`·`ETF_SWITCH_INDEX_PERCENT`·`ETF_SWITCH_STRENGTH_MINUTES`, UI 없음). 보유 ETF가 **틀린 방향**이면 — KODEX 200 가격이 산 시점 대비 반대로 0.5% 이상 AND KODEX 200 최근 5분 체결강도(`src/core/trade_strength.py`)도 반대 AND 순손익 < 0 — 판다(`TradingEngine._check_etf_switch`, 사유 `etf_switch`). 판정은 `_check_portfolio_exit` **뒤**라 손절·익절·고점 반납이 먼저다. 1번째 종목이면 매도 체결을 기다렸다가 반대 ETF를 시장가로 사고(`src/core/switch_flow.py`, 실행 큐의 `switch_buy` 코루틴 단계, 하루 1회), 2번째 종목이면 매도만 한다. 069500은 항상 구독한다(`runtime.subscribe_index`). 다리 기록은 `etf_switch_legs`. 전환 매수를 기다리는 동안 "전량 매도 완료" 리포트는 보류된다. **근거가 음수인 실험이다** — 지수 조건만의 백테스트는 모든 문턱에서 현행보다 나빴고 체결강도는 검증되지 않았다 (PRD 5.5 "장중 전환").
```

"스레드 / 이벤트 루프 구조" 절에서 `- 스케줄 실행과 UI의 "즉시 실행" 버튼(①~⑤)은 **같은 실행 큐** ...` 항목 끝에 문장을 덧붙인다

```markdown
  장중 전환의 매수 단계(`switch_buy`)도 이 큐를 탄다 — 유일한 **코루틴 단계**라 매도 체결을 기다리는 동안(최대 15:14까지) 루프는 막지 않지만 큐는 잡고 있다.
```

- [ ] **Step 4: 스펙 갱신** — `docs/superpowers/specs/2026-11-11-etf-intraday-switch-design.md`

머리말의 `- 상태: 설계 승인, 구현 전`을 아래로 바꾼다

```markdown
- 상태: 구현 완료 (2026-11-12, 계획 `docs/superpowers/plans/2026-11-12-etf-intraday-switch.md`). 구현하며 바뀐 점은 12절
```

문서 끝에 절을 추가한다

```markdown
## 12. 구현하며 바뀐 점 (2026-11-12)

- **메일** (사용자 지시 2026-11-12): 알림은 매도가 끝났을 때·전환이 일어났을 때 건별 1통, 매매 결과 리포트는
  모든 매도가 끝난 뒤 1통만.
  - 전환(`SWITCH`)은 매도 시점에 메일을 보내지 않고, 반대 ETF 매수가 접수된 뒤 1통에 판 종목·산 종목·판정
    값을 함께 싣는다 (6절의 "전환 1통").
  - 전환 매수는 **매수 기록 파일에 쓰지 않는다** (5.5의 둘째 항목 철회) — 쓰면 매수 결과 메일이 한 통 더
    나간다. 미체결 취소는 체결내역 조회 기준이라 10:10·15:15가 그대로 거둔다.
  - 전환 매수 단계가 끝나기 전에는 "전량 매도 완료" 리포트를 보류한다 (`runtime.closeout_report_due`).
- 청산 사유 한글 표는 `templates.py`가 아니라 `src/llm/exit_reviewer.py`의 `EXIT_REASON_LABELS`다 (4.4-5, 6절).
- "결과 불명" 매수는 `OrderResult.outcome_unknown`으로 구분한다 (5.3) — 종전에는 로그로만 갈렸다.
- 전환 매도 뒤·매수 전에 엔진이 재시작되면 그날은 다시 사지 않는다 (5.4에 없던 경우, 현금 마감·경고 메일).
- 069500 구독은 `runtime.run`에서 한 번 건다 (4.4-2) — `watch_ai_exit`에는 구독 갱신이 없고, 재접속 복구는
  `WebSocketClient.connect`가 한다.
- 체결강도 창은 틱이 60초 넘게 끊기면 처음부터 다시 채운다 (4.1의 "재접속 직후 다시 5분"을 틱 공백으로 판정).
- 매수 단계는 `src/core/switch_flow.py`의 `SwitchFlow`로 분리했다 (4절 구성 요소에 없던 파일).
```

- [ ] **Step 5: 전체 테스트**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: 전부 PASS, 실패 0건. 실패가 있으면 고친 뒤 다시 돌린다 — 실패한 채로 커밋하지 않는다.

- [ ] **Step 6: 커밋**

```bash
git add 주식자동매매_PRD.md CLAUDE.md docs/superpowers/specs/2026-11-11-etf-intraday-switch-design.md
git commit -m "Record the intraday ETF switch in the PRD, CLAUDE.md and the spec"
```

---

## 구현 뒤 사용자가 할 일 (코드 작업 아님)

1. **첫 거래일은 `.env`에 `ETF_SWITCH_ENABLED=0`을 넣고 시작한다.** 판정만 꺼지고 069500 구독·체결강도 계산·로그는 돈다.
2. 09:00~09:30 로그에서 `KODEX 200 틱:` 줄을 본다.
   - `체결량(필드 15)`가 `None`이 아니고 양수·음수가 섞여 나오는가.
   - `키움 누적 체결강도(필드 228)`가 100 안팎의 숫자인가.
   - 09:05 이후 `5분 체결강도`가 "모름"이 아닌 숫자로 나오고, 228과 방향(100 위/아래)이 대체로 맞는가.
3. 맞으면 `ETF_SWITCH_ENABLED=1`로 바꾸고 엔진을 재시작한다. 다르면 `src/api/websocket_client.py`의 필드 번호를 고친 뒤에 켠다.
4. 몇 주 뒤 `etf_switch_legs`의 2번째 다리(`leg = 2`) 순손익을 `trades`와 대조한다 — 플러스가 아니면 끈다.
