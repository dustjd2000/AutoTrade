# 종목별 손절과 익절 제거 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 손절 판정·매도를 보유 목록 합산에서 종목별로 되돌리고, 매도를 일으키지 않는 익절 설정을 전부 걷어내고, AI 매도 판단이 노이즈에 덜 불려 나오고 손절선에 기대지 않게 만든다.

**Architecture:** `RiskManager`가 합산 순손익률 하나 대신 손절선에 닿은 **종목 목록**을 돌려주고, `TradingEngine`이 그 종목만 기존 전량 매도 경로(`_execute_portfolio_exit`)에 넘긴다 — 주문·체결·알림·기록 로직은 재사용한다. `DrawdownTracker`는 고점이 임계값 이상인 종목만 감시하고, `exit_advisor`의 프롬프트에서는 손절선과 익절 기준선을 아예 빼 AI가 그 숫자를 근거로 쓸 수 없게 한다.

**Tech Stack:** Python 3.14 / pytest / PyQt6 (UI) / anthropic SDK 1.2.0

## Global Constraints

- 설계 문서: `docs/superpowers/specs/2026-09-18-position-level-stop-loss-design.md` — 충돌하면 spec이 정본이다.
- **AI 매도 판단은 전량 매도를 유지한다.** 종목별로 바꾸지 않는다.
- **`_execute_portfolio_exit` 경로를 새로 만들지 않는다.** 매도 대상 목록을 좁혀 넘길 뿐이다.
- **`AI_EXIT_DRAWDOWN_PERCENT`(1%p)·`AI_EXIT_INTERVAL_MINUTES`(30분) 값은 건드리지 않는다.**
- **지우지 않는 것**: `exit_trigger_price`(손절가 계산에도 쓰인다), LLM 목표 매도가(`target_sell_price`/`sell_target` — 추천이 낸 참고 수치로 익절 설정과 무관), `portfolio_return`/`portfolio_net_pnl`(UI 표시·AI 프롬프트에서 계속 쓴다).
- 파이썬은 `.venv/Scripts/python.exe`로 실행한다. 전체 테스트는 `.venv/Scripts/python.exe -m pytest -q`.
- 기존 804개 테스트가 계속 통과해야 한다.
- 줄 번호는 앞 태스크의 편집으로 밀리므로 **검색 문자열로 대상을 찾는다.**
- 커밋 메시지는 한국어 평서문 한 줄 + 필요하면 본문. 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- 태스크마다 커밋을 따로 남긴다 — 며칠 돌려 보고 항목 단위로 되돌릴 수 있어야 한다.

---

### Task 1: 이익 반납 감시에 최소 고점 요건 (①)

고점이 +0.05%인 종목이 1%p 내려간 것을 "고점 이익의 100% 반납"으로 보고 AI를 부르던 것을 막는다. 관측된 발동 11건 중 3건만 남는다.

**Files:**
- Modify: `src/core/exit_drawdown.py` (`DrawdownTracker.update`)
- Test: `tests/test_core/test_exit_drawdown.py`

**Interfaces:**
- Consumes: 없음
- Produces: `DrawdownTracker.update(per_ticker, portfolio)` 동작 변경. 시그니처는 그대로.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_exit_drawdown.py` 끝에 추가한다. 기존 파일의 import와 헬퍼를 그대로 쓴다 (파일 첫 30줄을 먼저 읽고 `DrawdownTracker` import 방식을 확인할 것).

```python
def test_small_peak_does_not_trigger():
    """고점이 임계값에 못 미치면 반납으로 보지 않는다 — 매수가 근처의 출렁임이다.

    2026-09-17 278470이 고점 +0.05%에서 -1.06%로 간 것을 '고점 이익의 100% 반납'으로
    잡아 AI를 불렀다. 0.05%는 이익이 아니라 노이즈다.
    """
    tracker = DrawdownTracker(threshold_ratio=0.01)

    tracker.update({"278470": 0.0005})          # 고점 +0.05%
    crossed = tracker.update({"278470": -0.0106})  # 1.11%p 반납

    assert crossed == []


def test_peak_at_threshold_still_triggers():
    """고점이 임계값 이상이면 기존대로 발동한다."""
    tracker = DrawdownTracker(threshold_ratio=0.01)

    tracker.update({"062040": 0.0217})          # 고점 +2.17%
    crossed = tracker.update({"062040": 0.0117})  # 1.00%p 반납

    assert crossed == ["062040"]
```

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_core/test_exit_drawdown.py -q
```

기대: `test_small_peak_does_not_trigger`가 FAIL (`crossed == ["278470"]`이 나온다). `test_peak_at_threshold_still_triggers`는 이미 PASS.

- [ ] **Step 3: 구현한다**

`src/core/exit_drawdown.py`의 `update` 안에서 아래 줄을 찾는다.

```python
            if peak <= 0:
                continue
```

이렇게 바꾼다.

```python
            # 고점이 임계값에 못 미치면 반납을 논할 이익이 아니다 — 고점 +0.05%에서 1%p
            # 내려간 것은 매수가 근처의 출렁임이지 이익 반납이 아니다 (2026-09-18).
            if peak < self.threshold_ratio:
                continue
```

`threshold_ratio <= 0`(감시 끔) 분기는 이 줄 **앞**에 이미 있으므로 그대로 둔다.

- [ ] **Step 4: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_core/test_exit_drawdown.py -q
.venv/Scripts/python.exe -m pytest -q
```

기대: 둘 다 전부 PASS.

- [ ] **Step 5: 클래스 docstring을 갱신한다**

`DrawdownTracker`의 docstring 끝(판정 단위 설명 뒤)에 한 문단을 덧붙인다.

```
    **고점이 임계값 이상일 때만 감시한다** (2026-09-18). 임계값이 절대값(%p)이라 고점이
    작으면 "1%p 반납"이 사실상 "매수가 대비 -1%"가 되어, 이익을 지키는 감시가 아니라
    그냥 하락 감지가 된다 — 관측된 발동 11건 중 8건이 고점 +1% 미만이었다.
```

- [ ] **Step 6: 커밋한다**

```bash
git add src/core/exit_drawdown.py tests/test_core/test_exit_drawdown.py
git commit -m "고점이 작은 종목은 이익 반납 감시에서 뺀다"
```

---

### Task 2: 종목별 손절 판정을 RiskManager에 추가 (B-1)

판정만 추가한다. 엔진 전환은 Task 3이라, 이 태스크가 끝난 시점에도 동작은 그대로다.

**Files:**
- Modify: `src/risk/manager.py`
- Test: `tests/test_core/test_portfolio_exit.py`

spec은 `tests/test_risk/`를 적었지만 그 디렉터리에는 `__init__.py`밖에 없고, 청산 판정
테스트는 실물 `RiskManager`를 엔진에 붙여 쓰는 `tests/test_core/test_portfolio_exit.py`에
모여 있다. 같은 파일에 둔다.

**Interfaces:**
- Consumes: `net_return(current_price, avg_price, commission_rate, tax_rate, slippage_rate) -> float` (이미 있는 모듈 함수)
- Produces: `RiskManager.check_position_exits(positions: Iterable[Position]) -> List[str]` — 손절선에 닿은 종목코드 목록. Task 3이 이것을 쓴다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_portfolio_exit.py` 끝에 추가한다. 이 파일은 비용을 0으로 둔 실물 `RiskManager`를 쓰므로 가격 변동률이 곧 순손익률이다.

```python
# ── 종목별 손절 판정 (2026-09-18) ──────────────────────────
def _risk(stop_loss_ratio=0.05, stop_loss_enabled=True):
    return RiskManager(
        stop_loss_ratio=stop_loss_ratio,
        stop_loss_enabled=stop_loss_enabled,
        commission_rate=0.0,
        tax_rate=0.0,
        slippage_rate=0.0,
    )


def _pos(ticker, avg_price, current_price):
    return Position(
        ticker=ticker,
        quantity=1,
        avg_price=avg_price,
        current_price=current_price,
        name=ticker,
    )


def test_position_exits_picks_only_the_broken_one():
    """한 종목만 손절선에 닿으면 그 종목만 돌려준다."""
    positions = [
        _pos("000660", 100_000, 93_000),   # -7.0%
        _pos("005930", 100_000, 101_000),  # +1.0%
    ]

    assert _risk().check_position_exits(positions) == ["000660"]


def test_position_exits_returns_all_broken():
    """여러 종목이 동시에 닿으면 전부 돌려준다."""
    positions = [
        _pos("000660", 100_000, 93_000),
        _pos("005930", 100_000, 94_000),
    ]

    assert sorted(_risk().check_position_exits(positions)) == ["000660", "005930"]


def test_position_exits_ignores_portfolio_average():
    """합산은 손절선인데 개별은 아무도 안 닿으면 매도하지 않는다.

    합산 판정이던 시절에는 전량 매도였다 — 종목별로 바꾸며 의도적으로 달라진 지점이다.
    """
    positions = [
        _pos("000660", 100_000, 96_000),   # -4.0%
        _pos("005930", 100_000, 95_500),   # -4.5%
    ]                                       # 합산 -4.25%, 손절선 -4%

    assert _risk(stop_loss_ratio=0.04).check_position_exits(positions) == []


def test_position_exits_empty_when_disabled():
    """손절을 끄면 빈 목록 — 판정 자체를 하지 않는다."""
    positions = [_pos("000660", 100_000, 90_000)]

    assert _risk(stop_loss_enabled=False).check_position_exits(positions) == []
```

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_core/test_portfolio_exit.py -q -k position_exits
```

기대: 네 개 모두 FAIL — `AttributeError: 'RiskManager' object has no attribute 'check_position_exits'`.

- [ ] **Step 3: 구현한다**

`src/risk/manager.py`의 `check_portfolio_exit` 메서드 **바로 앞**에 추가한다. (`check_portfolio_exit`은 Task 3에서 지운다 — 지금은 그대로 둔다.)

```python
    def check_position_exits(self, positions: Iterable[Position]) -> List[str]:
        """손절선에 닿은 **종목**을 골라 돌려준다 (확정 2026-09-18, PRD 5.5-B).

        판정 단위를 합산에서 종목별로 되돌린 것이다. 2026-08-10에 합산으로 간 근거는
        "익절과 손절 두 규칙을 두면 먼저 팔린 종목의 확정 손실이 이후 합산에서 빠져
        순서에 따라 결과가 달라진다"였는데, 익절을 걷어내 규칙이 손절 하나만 남으면서
        그 근거가 해소됐다.

        판정은 가격 변동률이 아니라 왕복 수수료·매도세금·슬리피지를 뺀 순손익률 기준이다
        — 합산 판정이 쓰던 것과 같은 식(`net_return`)을 종목 단위로 적용한다.

        `stop_loss_enabled`가 꺼져 있으면 빈 목록이다. 합산 순손익률(`portfolio_return`)
        계산은 멈추지 않으므로 UI 표시와 AI 프롬프트는 그대로다.

        키움 REST API에 조건부 예약주문(스탑오더)이 없어, 이 실시간 감시가 하방의
        사실상 유일한 청산 수단이다 — 앱이 꺼지거나 WebSocket이 끊기면 그 사이 손절도
        멈춘다.
        """
        if not self.stop_loss_enabled:
            return []
        broken = []
        for position in positions:
            if position.avg_price <= 0 or position.current_price <= 0:
                continue
            ret = net_return(
                position.current_price,
                position.avg_price,
                self.commission_rate,
                self.tax_rate,
                self.slippage_rate,
            )
            if ret <= -self.stop_loss_ratio:
                broken.append(position.ticker)
        return broken
```

`List`가 이미 import되어 있는지 확인한다 — 파일 상단이 `from typing import Dict, Iterable, Optional, Tuple`이면 `List`를 더한다.

- [ ] **Step 4: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_core/test_portfolio_exit.py -q
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. 이 시점에는 아직 아무도 새 메서드를 부르지 않으므로 동작은 그대로다.

- [ ] **Step 5: 커밋한다**

```bash
git add src/risk/manager.py tests/test_core/test_portfolio_exit.py
git commit -m "손절선에 닿은 종목을 골라내는 판정을 추가한다"
```

---

### Task 3: 엔진이 종목별 손절을 실행 (B-2)

여기서 실제 동작이 바뀐다.

**Files:**
- Modify: `src/core/engine.py` (`_check_portfolio_exit`, `_execute_portfolio_exit`)
- Modify: `src/risk/manager.py` (`check_portfolio_exit` 삭제)
- Test: `tests/test_core/test_portfolio_exit.py`

**Interfaces:**
- Consumes: `RiskManager.check_position_exits(positions) -> List[str]` (Task 2)
- Produces: 동작 변경. `_execute_portfolio_exit(holdings, reason, note=None)` 시그니처는 그대로라 AI 매도 판단 경로(`runtime.run_ai_exit_cycle`)는 손대지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_portfolio_exit.py`에 추가한다. `make_engine` 헬퍼의 시그니처를 파일에서 먼저 확인하고, 기존 테스트가 시세를 넣는 방식(`engine.on_market_data(MarketData(...))`)을 그대로 따른다.

```python
def test_engine_sells_only_the_broken_position():
    """손절선에 닿은 종목만 팔고 나머지는 보유한다 (2026-09-18).

    합산 판정이던 시절에는 한 종목이 닿으면 전량이 나갔다.
    """
    engine, orders = make_engine(
        {
            "000660": Position("000660", 1, 100_000, 100_000, name="하이닉스"),
            "005930": Position("005930", 1, 100_000, 100_000, name="삼성전자"),
        }
    )
    engine.risk_manager.stop_loss_ratio = 0.05

    engine.on_market_data(MarketData(ticker="000660", price=93_000))

    assert [o.ticker for o in orders] == ["000660"]
```

`make_engine`이 `(engine, orders)`가 아닌 다른 것을 돌려주면 그 형태에 맞춘다 — 파일 안의 기존 테스트가 쓰는 방식을 그대로 베낀다.

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_core/test_portfolio_exit.py -q -k only_the_broken
```

기대: FAIL — 두 종목 모두 주문이 나가거나(`["000660", "005930"]`), 합산 -3.5%라 손절선에 못 닿아 주문이 하나도 없다(`[]`).

- [ ] **Step 3: 엔진을 전환한다**

`src/core/engine.py`의 `_check_portfolio_exit`에서 아래를 찾는다.

```python
        reason = self.risk_manager.check_portfolio_exit(holdings)
        if reason is not None:
            self._execute_portfolio_exit(holdings, reason)
            return True

        return False
```

이렇게 바꾼다.

```python
        broken = self.risk_manager.check_position_exits(holdings)
        if broken:
            targets = [p for p in holdings if p.ticker in set(broken)]
            self._execute_portfolio_exit(targets, ExitReason.STOP_LOSS)
            return True

        return False
```

같은 메서드의 docstring에서 아래 문단을 찾아

```
        판정은 **합산 손절** 하나다 (PRD 5.5-B, 확정 2026-08-10). 닿으면 보유 종목을
        전량 매도한다. 익절 자동 청산과 종목별 단순익절은 2026-09-09에 걷어냈다 (PRD 10절)
        — 실매매 대조에서 어떤 익절선도 "익절 없음"보다 낫지 않았기 때문이다.
```

이렇게 바꾼다.

```
        판정은 **종목별 손절** 하나다 (PRD 5.5-B, 확정 2026-09-18). 손절선에 닿은 종목만
        판다 — 나머지는 그대로 들고 간다. 2026-08-10부터 2026-09-18까지는 합산으로 판정해
        전량을 팔았다. 익절 자동 청산은 2026-09-09에, 익절 설정 자체는 2026-09-18에
        걷어냈다 (PRD 10절).

        AI 매도 판단은 여전히 **전량**이다 — 그쪽은 "지금 전부 정리할 상황인가"를 묻는
        별개 경로이고, 같은 `_execute_portfolio_exit`을 보유 목록 전체로 부른다.
```

`ExitReason`이 `engine.py`에 이미 import되어 있는지 확인한다 (파일 상단 `from src.core.events import ...`).

- [ ] **Step 4: 매도 알림 문구를 고친다**

`_execute_portfolio_exit`은 이제 일부 종목만 받을 수 있다. "전량 청산"이라는 문구가 틀린다.

아래를 찾아

```python
        logger.info(
            "합산 청산 조건 도달 (%s): 합산 순손익 %s, 대상 %d종목%s",
```

이렇게 바꾼다.

```python
        logger.info(
            "청산 조건 도달 (%s): 합산 순손익 %s, 대상 %d종목%s",
```

아래를 찾아

```python
            body = (
                f"{reason.value} 전량 청산 (합산 순손익 {percent}) — {len(sold)}종목\n"
                + "\n".join(sold)
            )
```

이렇게 바꾼다.

```python
            body = (
                f"{reason.value} 청산 (합산 순손익 {percent}) — {len(sold)}종목\n"
                + "\n".join(sold)
            )
```

docstring 첫 줄도 고친다.

```
        """합산 손익이 손절 라인에 닿아 보유 종목을 전량 청산한다.
```
→
```
        """넘겨받은 종목을 청산한다 — 손절은 손절선에 닿은 종목만, AI 판단은 보유 목록 전체다.
```

- [ ] **Step 5: 쓰이지 않게 된 판정을 지운다**

`src/risk/manager.py`에서 `def check_portfolio_exit(` 메서드를 **통째로 삭제**한다 (docstring 포함, 다음 메서드 `def record_order(` 직전까지).

지우기 전에 확인한다.

```
grep -rn "check_portfolio_exit" src/ tests/ --include=*.py
```

`src/core/engine.py`의 메서드 이름 `_check_portfolio_exit`(앞에 밑줄)은 **다른 것이다** — 지우지 않는다. 주석과 docstring에 남은 언급은 Task 8에서 문서와 함께 정리한다.

- [ ] **Step 6: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. **합산 손절을 검증하던 기존 테스트가 깨진다** — 예컨대 "합산이 손절선이면 전량 매도"를 확인하던 것은 이제 동작이 달라졌으므로, 새 동작("개별이 닿지 않으면 팔지 않는다")을 확인하도록 고친다. 테스트를 지우지 말고 의도를 바꿔 남긴다. 각 테스트에 왜 바뀌었는지 한 줄 주석을 남긴다.

- [ ] **Step 7: 커밋한다**

```bash
git add src/core/engine.py src/risk/manager.py tests/test_core/test_portfolio_exit.py
git commit -m "손절을 종목별로 판정하고 닿은 종목만 판다"
```

---

### Task 4: AI 프롬프트에서 손절선과 익절 기준선을 뺀다 (② + A)

시스템 프롬프트가 "손절선은 코드가 처리하니 신경 쓰지 말라"고 못 박는데도 AI가 그것을 주된 보유 근거로 쓴다. user prompt가 `남은 거리 3.81%p`를 숫자로 주기 때문이다. 주지 않으면 쓸 수 없다.

**Files:**
- Modify: `src/llm/exit_advisor.py`
- Modify: `src/core/runtime.py` (호출부 인자)
- Test: `tests/test_llm/test_exit_advisor.py`

**Interfaces:**
- Consumes: 없음
- Produces: `build_exit_user_prompt(holdings, trace, portfolio_return, minutes_to_close, partial, portfolio_peak=None)` — `stop_loss_ratio`와 `take_profit_ratio` 인자가 사라진다. `ExitAdvisor.decide(...)`에서도 두 인자가 빠진다. Task 7이 `take_profit_ratio` 정의를 지울 때 이 태스크가 선행되어 있어야 한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_llm/test_exit_advisor.py` 끝에 추가한다. 기존 파일에서 `build_exit_user_prompt`/`build_exit_system_prompt`를 어떻게 부르는지(헬퍼·더미 데이터) 먼저 확인하고 그 방식을 따른다.

```python
def test_prompt_has_no_stop_loss_line():
    """손절선을 주면 AI가 '아직 여유가 있다'를 보유 근거로 쓴다 — 아예 주지 않는다.

    2026-09-09~18 로그에서 보유 유지 사유가 거의 전부 "손절선(-5.00%)까지 여유"였다.
    시스템 프롬프트가 신경 쓰지 말라고 했는데도 그랬다 (2026-09-18).
    """
    prompt = build_exit_user_prompt(
        holdings=[],
        trace=[],
        portfolio_return=-0.012,
        minutes_to_close=300,
        partial=False,
    )

    assert "손절선" not in prompt
    assert "남은 거리" not in prompt
    assert "익절" not in prompt


def test_system_prompt_has_no_baseline_section():
    prompt = build_exit_system_prompt()

    assert "손절선과 익절 기준선" not in prompt
```

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_llm/test_exit_advisor.py -q -k "no_stop_loss_line or no_baseline_section"
```

기대: FAIL — `TypeError`(없앨 인자를 아직 요구함) 또는 단언 실패.

- [ ] **Step 3: user prompt를 고친다**

`build_exit_user_prompt`의 시그니처에서 `stop_loss_ratio: float,`와 `take_profit_ratio: float,` 두 줄을 지운다.

`lines = [` 블록에서 아래 세 항목을 통째로 지운다.

```python
        "\n## 기준선",
        f"- 손절선: {_pct(-stop_loss_ratio)} "
        f"(남은 거리 {(portfolio_return + stop_loss_ratio) * 100:.2f}%p — "
        "닿으면 코드가 자동으로 처리하므로 이 판단이 신경 쓸 필요는 없습니다)",
        f"- 익절 기준선: {_pct(take_profit_ratio)} "
        "(**보유 종목 합산** 기준이며 자동 청산 트리거가 아닙니다. 미달 자체는 보유 근거가 "
        "되지 않습니다)",
```

남은 `f"- 현재 합산 순손익률: {_pct(portfolio_return)}",`는 `## 기준선` 제목이 사라졌으므로 앞에 제목을 붙인다.

```python
        "\n## 현재",
        f"- 합산 순손익률: {_pct(portfolio_return)}",
```

그 아래 `portfolio_peak` 블록(`- 합산 당일 고점: ...`)은 **그대로 둔다** — 되돌림 판단의 재료이고 손절선과 무관하다.

- [ ] **Step 4: system prompt를 고친다**

`build_exit_system_prompt`의 반환 문자열에서 `## 손절선과 익절 기준선` 절을 통째로 지운다 — 제목 줄부터 그 절의 마지막 불릿(`판단하십시오.`로 끝나는 줄)까지, 다음 `## 시간` 직전까지다.

`## 역할` 절의 아래 문장은 **그대로 둔다** (AI 매도 판단은 전량 유지).

```
지금 보유 중인 종목 전체를 이 시점에 **전량** 정리할지 판단합니다.
```

- [ ] **Step 5: 호출부를 고친다**

`ExitAdvisor.decide`의 시그니처에서 `stop_loss_ratio: float,`와 `take_profit_ratio: float,`를 지우고, 내부에서 `build_exit_user_prompt(...)`에 넘기던 두 인자도 지운다.

`src/core/runtime.py`에서 `decide(` 호출부를 찾아 두 인자를 지운다.

```
grep -n "decide(" src/core/runtime.py
grep -n "stop_loss_ratio\|take_profit_ratio" src/core/runtime.py
```

- [ ] **Step 6: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. 손절선·익절 문자열을 단언하던 기존 테스트가 깨지면 새 동작에 맞춰 고친다.

- [ ] **Step 7: 커밋한다**

```bash
git add src/llm/exit_advisor.py src/core/runtime.py tests/test_llm/test_exit_advisor.py
git commit -m "AI 매도 판단에서 손절선과 익절 기준선을 뺀다"
```

---

### Task 5: UI에서 익절을 뺀다 (A)

입력란 하나가 익절 목표와 손절을 함께 정하던 것을 손절 전용으로 만든다. 입력란이 하나라 기존 값(5)이 그대로 손절 5%로 이어지고, 사용자가 다시 입력할 필요는 없다.

**Files:**
- Modify: `src/ui/main_window.py`
- Test: `tests/test_ui/test_exit_watch_text.py` — 청산 감시 문구를 단언하는 기존 테스트라
  이 태스크에서 깨질 가능성이 높다. 새 문구에 맞춰 고친다 (지우지 말 것).

**Interfaces:**
- Consumes: 없음
- Produces: UI 문구만 바뀐다. `.env` 키 이름과 저장 경로는 그대로다.

- [ ] **Step 1: 대상을 찾는다**

```
grep -n "익절" src/ui/main_window.py
```

바꿀 곳은 그룹박스 제목, 입력란 라벨, 플레이스홀더, 도움말 본문, 상태 표시 문구다.

- [ ] **Step 2: 고친다**

각 위치를 아래 원칙으로 바꾼다.

- 그룹박스 `"리스크 관리 (익절 목표 / 손절 / 갭 허용치)"` → `"리스크 관리 (손절 / 갭 허용치)"`
- 라벨 `"익절 목표 / 손절 (%)"` → `"손절 (%)"`
- 플레이스홀더 `"예: 2 (손절 -2%는 자동 / 익절 목표 +2%는 AI 참고선)"` → `"예: 5 (순손익 -5%에 닿은 종목을 자동 매도)"`
- 도움말 본문 `"(입력값 하나가 손절선(-)과 익절 목표(+)를 함께 정합니다 — ...)"` 로 시작하는 단락 전체를 아래로 교체한다.

```
"손절선입니다. 보유 종목 중 순손익률이 이 값에 닿은 종목을 실시간으로 시장가 매도합니다 "
"(2026-09-18부터 종목별 판정 — 그 전에는 보유 종목 합산이었습니다). 순손익률은 "
"수수료·세금·슬리피지를 뺀 값이라 화면의 평가손익률보다 낮습니다.\n"
"이익 실현은 자동선이 없고 AI 매도 판단이 맡습니다 — 그쪽은 보유 종목 전체를 한 번에 "
"정리할지 판단합니다."
```

- 입력란 설명 주석(`# 익절과 손절은 **입력란 하나를 공유한다** ...`)을 손절 단독 설명으로 고친다.
- `"익절/손절이 자동 감시되며, "` 같은 상태 문구는 `"손절이 자동 감시되며, "`로 바꾼다.
- 툴팁의 `"익절선에 닿아도"` / `"익절선이 +0.5%인데"` 같은 문장은 손절 기준으로 고쳐 쓴다.

**주의**: `TAKE_PROFIT_PERCENT`를 `.env`에 쓰는 코드가 있으면 지운다. 저장되는 키는 `STOP_LOSS_PERCENT` 하나다.

```
grep -n "TAKE_PROFIT_PERCENT" src/ui/main_window.py src/ui/env_store.py
```

- [ ] **Step 3: 테스트를 돌린다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS.

- [ ] **Step 4: 문구가 실제로 바뀌었는지 확인한다**

```
grep -n "익절" src/ui/main_window.py
```

기대: AI 매도 판단 설명 안의 "이익 실현" 맥락 외에 `익절`이라는 단어가 남지 않는다.

- [ ] **Step 5: 커밋한다**

```bash
git add src/ui/main_window.py
git commit -m "설정 화면에서 익절 목표를 빼고 손절 전용으로 만든다"
```

---

### Task 6: 메일과 이벤트에서 익절을 뺀다 (A)

매수 결과 표의 '매도예상가'는 익절선을 역산한 값이라 기준이 사라지면 의미가 없다.

**Files:**
- Modify: `src/core/daily_workflow.py` (`_take_profit_price`와 그 호출부)
- Modify: `src/core/events.py` (`BuyExecution.take_profit_percent`, `simple_take_profit`)
- Modify: `src/notification/templates.py` (`simple_take_profit` 분기, `_take_profit_line`, 익절가 계산)
- Test: `tests/test_core/test_daily_workflow.py`, `tests/test_notification/test_templates.py`
  — 후자는 메일 본문을 단언하므로 '매도예상가' 열이 사라지면 깨진다. 단언을 고친다.

**Interfaces:**
- Consumes: 없음
- Produces: `BuyExecution`에서 `take_profit_percent`·`simple_take_profit` 필드가 사라진다. Task 7이 `take_profit_ratio` 정의를 지우기 전에 끝나 있어야 한다.

- [ ] **Step 1: 대상을 확인한다**

```
grep -rn "take_profit\|simple_take_profit\|매도예상가" src/core/daily_workflow.py src/core/events.py src/notification/templates.py
```

- [ ] **Step 2: 지운다**

- `src/core/daily_workflow.py`: `_take_profit_price` 메서드를 통째로 지우고, 호출부(`sell_price=self._take_profit_price(r.price) ...`)에서 그 인자를 뺀다. 표에 '매도예상가' 열이 있으면 열 자체를 뺀다. `take_profit_percent=...`, `simple_take_profit=False`를 넘기는 곳도 지운다.
- `src/core/events.py`: `take_profit_percent: float = 0.0`과 `simple_take_profit: bool = False` 두 필드를 지운다.
- `src/notification/templates.py`: `if execution.simple_take_profit:` 분기 두 곳과 `_take_profit_line` 함수를 지운다. `take_profit_price = exit_trigger_price(...)`를 계산해 쓰는 곳도 지운다 — **`exit_trigger_price` 함수 자체와 손절가 계산(`stop_loss_price = ...`)은 남긴다.**
- `BuyExecution`에서 필드가 빠지면 이를 만드는 모든 곳이 깨진다. `grep -rn "BuyExecution(" src/ tests/`로 전부 찾아 고친다.

- [ ] **Step 3: 테스트를 돌린다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. 익절 관련 단언(메일 본문에 '매도예상가'가 있는지 등)이 깨지면 그 단언을 지운다.

- [ ] **Step 4: 잔재가 없는지 확인한다**

```
grep -rn "take_profit" src/core/ src/notification/ --include=*.py
```

기대: `exit_trigger_price`의 docstring 안 언급 외에는 남지 않는다.

- [ ] **Step 5: 커밋한다**

```bash
git add src/core/daily_workflow.py src/core/events.py src/notification/templates.py tests/
git commit -m "매수 결과 메일에서 익절 기준 매도예상가를 뺀다"
```

---

### Task 7: 익절 설정 정의를 지운다 (A)

소비처가 모두 정리된 뒤 마지막으로 정의를 없앤다.

**Files:**
- Modify: `config/settings.py`
- Modify: `src/risk/manager.py` (`take_profit_ratio` 필드)
- Modify: `src/core/runtime.py` (`RiskManager(take_profit_ratio=...)`)
- Modify: `.env.example`
- Test: `tests/test_config/test_settings.py`

**Interfaces:**
- Consumes: Task 4·5·6이 모든 소비처를 지워 둔 상태
- Produces: `Settings`에 `take_profit_percent`/`take_profit_ratio`가 없다. `RiskManager.__init__`에서 `take_profit_ratio` 인자가 사라진다.

- [ ] **Step 1: 남은 소비처가 없는지 확인한다**

```
grep -rn "take_profit_ratio\|take_profit_percent\|TAKE_PROFIT_PERCENT" src/ config/ tests/ --include=*.py
```

기대: `config/settings.py`, `src/risk/manager.py`, `src/core/runtime.py`의 정의·전달부와 `tests/test_config/test_settings.py`의 단언만 남는다. 다른 것이 남아 있으면 앞 태스크가 덜 끝난 것이다 — 먼저 정리한다.

- [ ] **Step 2: 지운다**

- `config/settings.py`: `take_profit_percent` 필드와 `take_profit_ratio` 프로퍼티를 지운다.
- `src/risk/manager.py`: `__init__`의 `take_profit_ratio: float = 0.005,` 인자와 `self.take_profit_ratio = take_profit_ratio` 줄을 지운다.
- `src/core/runtime.py`: `RiskManager(...)` 호출에서 `take_profit_ratio=settings.take_profit_ratio,`를 지운다.
- `.env.example`: `TAKE_PROFIT_PERCENT=` 줄을 지우고, `STOP_LOSS_PERCENT` 주석을 "순손익 기준 종목별 손절선(%)"으로 고친다.
- `tests/test_config/test_settings.py`: 익절 관련 단언을 지운다.

사용자의 실제 `.env`는 **건드리지 않는다** — 읽지 않게 되므로 남아 있어도 무해하다.

- [ ] **Step 3: 테스트를 돌린다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS.

- [ ] **Step 4: 앱이 실제로 뜨는지 확인한다**

`Settings`와 `RiskManager` 시그니처가 바뀌었으므로 import 단계에서 깨지지 않는지 본다. **엔진을 시작하지 말고** 구성만 확인한다.

```
.venv/Scripts/python.exe -c "from config.settings import Settings; from src.risk.manager import RiskManager; s=Settings(); RiskManager(stop_loss_ratio=s.stop_loss_ratio); print('ok')"
```

기대: `ok`.

- [ ] **Step 5: 커밋한다**

```bash
git add config/settings.py src/risk/manager.py src/core/runtime.py .env.example tests/
git commit -m "익절 설정을 걷어낸다"
```

---

### Task 8: 문서를 갱신한다

**Files:**
- Modify: `주식자동매매_PRD.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1~7의 최종 동작
- Produces: 없음

- [ ] **Step 1: PRD 5.5-B "매도(청산) 조건"을 고친다**

판정 단위를 종목별로 바꾸고 익절 항목을 지운다. 아래를 반영한다.

- 손절: 종목별 순손익률 판정, 닿은 종목만 시장가 매도
- 익절: 항목 삭제 (설정 자체가 없어졌다)
- AI 매도 판단: 전량 매도 유지, 넘겨받던 익절 기준선이 사라짐

- [ ] **Step 2: PRD 5.5-B "AI 매도 판단"과 "이익 반납 감시"를 고친다**

- 프롬프트에서 손절선·익절 기준선이 빠졌다는 것과 그 이유(숫자를 주면 그것을 보유 근거로 쓴다)
- 반납 감시는 고점이 임계값 이상인 종목만 본다는 것

- [ ] **Step 3: PRD 10절에 결정을 기록한다**

11절 시작 직전에 항목을 추가한다. 담을 것:

- 계기: 2026-09-09~18 로그. 보유 중 관측된 종목 최대 이익 +2.17%(익절선 5%에 닿은 적 없음), AI 청산 6번 중 5번이 마이너스 구간, 반납 감시 발동 11건 중 8건이 고점 +1% 미만
- 결정 네 가지와 서로 맞물리는 구조
- **2026-08-10(합산 전환)·2026-09-09(익절 자동 청산 제거) 항목은 지우지 않는다** — 근거가 철회된 것이 아니라 전제가 바뀐 것이다. 두 항목에 이번 결정으로 이어졌다는 교차 참조를 한 줄씩 덧붙인다
- 받아들인 위험: 이익 실현이 실제로 늘어날지는 돌려봐야 안다, ①이 고점 작은 종목의 급락 대응을 다음 정규 주기까지 늦춘다(최악은 손절선), 종목별 손절은 발동 빈도가 낮을 것이다

- [ ] **Step 4: CLAUDE.md를 고친다**

"리스크 관리는 이중 구조" 단락에서 아래가 사실과 어긋난다. 전부 갱신한다.

- "손절(`check_portfolio_exit`)은..." → 메서드 이름이 `check_position_exits`로 바뀌었다
- "**익절 자동 청산은 없다** (2026-09-09에 걷어냄)" → 익절 설정 자체가 없어졌다
- "**`TAKE_PROFIT_PERCENT`는 남아 있지만 자동 청산에 쓰이지 않는다**" 단락 → 삭제
- "**판정 단위는 종목이 아니라 보유 목록 전체다** (2026-08-10에 종목별에서 바꿈). 손절도 AI 판단도 보유 종목을 **전량** 매도한다." → 손절은 종목별, AI 판단은 전량으로 갈렸다
- "이익 반납 감시" 단락 → 고점이 임계값 이상인 종목만 본다

- [ ] **Step 5: 남은 언급을 찾는다**

```
grep -rn "합산 익절\|익절/손절\|check_portfolio_exit" src/ CLAUDE.md --include=*.py --include=*.md
```

코드 주석에 남은 옛 서술을 고친다.

- [ ] **Step 6: 전체 테스트와 커밋**

```
.venv/Scripts/python.exe -m pytest -q
git add 주식자동매매_PRD.md CLAUDE.md src/
git commit -m "종목별 손절과 익절 제거를 문서에 반영한다"
```

---

## 완료 확인

- [ ] `.venv/Scripts/python.exe -m pytest -q` — 804개 이상 전부 통과
- [ ] `grep -rn "take_profit" src/ config/ --include=*.py` — `exit_trigger_price` docstring 외 없음
- [ ] `grep -rn "check_portfolio_exit" src/ --include=*.py` — 엔진의 `_check_portfolio_exit`(밑줄 있는 것)만 남음
- [ ] 앱이 뜨는지 확인 — `python scripts/run_ui.py`로 창이 뜨고 설정 화면에 "손절 (%)" 한 칸만 보이는지. **엔진 시작 버튼은 누르지 않는다** (장중이면 돌고 있는 엔진의 토큰이 무효화된다)
- [ ] 커밋이 항목별로 8개 남았는지 — 되돌릴 때 항목 단위로 집을 수 있어야 한다
