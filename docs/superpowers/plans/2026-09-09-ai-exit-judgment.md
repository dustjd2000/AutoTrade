# AI 매도 판단 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 익절을 자동 청산 트리거에서 빼고 그 자리를 AI 판단으로 바꾼다 — 보유 종목이 있을 때만, 설정된 주기(15/30/60분)로 순손익 궤적을 보고 전량 매도 여부를 정한다. 손절선은 실시간 하드 라인으로 그대로 남는다.

**Architecture:** `RiskManager.check_portfolio_exit`이 손절만 판정하도록 좁히고 단순익절을 걷어낸다. 새로 생기는 것은 순손익 궤적을 담는 `ExitTrace`, LLM 판단 모듈 `src/llm/exit_advisor.py`, 그리고 이 둘을 엮어 주기마다 도는 `watch_ai_exit` 워처다. 매도는 기존 `_execute_portfolio_exit`을 `ExitReason.AI_JUDGMENT`로 재사용한다.

**Tech Stack:** Python 3.14, `anthropic` SDK, PyQt6, pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-ai-exit-judgment-design.md`

## Global Constraints

- **손절은 실시간 하드 라인으로 남는다.** `check_portfolio_exit`이 시세 콜백에서 도는 구조를 바꾸지 않는다.
- **매수 경로와 15:15 강제청산은 건드리지 않는다.**
- **실패는 언제나 "팔지 않음"으로 떨어진다.** LLM 호출 실패·타임아웃·형식 오류 모두 매도하지 않는다. 잘못 파는 것이 안 파는 것보다 되돌리기 어렵다.
- **LLM 호출은 별도 스레드, 주문 실행은 루프 스레드.** 호출이 이벤트 루프를 막으면 WebSocket PING에 응답하지 못해 시세가 끊긴다. 주문은 실시간 손절 감시와 직렬화해야 한다.
- **판단도 실행도 전량 단위다.** 종목별 매도는 만들지 않는다.
- **AI는 매도 가격을 내지 않는다.** 실행은 기존 청산과 같은 시장가다.
- 익절과 손절은 **입력란 하나를 공유한다** (2026-08-21 확정) — 그 통합을 되돌리지 않는다.
- `AI 매도 판단` 체크박스 기본값은 **켬**이다 (사용자 결정 2026-09-09).
- 코드·식별자는 영어, **커밋 메시지·주석·UI·프롬프트 문자열은 한국어.** 이 저장소의 커밋 이력이 한국어다.
- 커밋은 각 Task 끝에서 한 번. **push는 하지 않는다.**
- Windows / PowerShell 5.1 — `&&`, `||`, `head`, `tail` 등은 쓸 수 없다. Bash 도구를 쓰면 POSIX sh로 돌아간다.
- 저장소에 가상환경이 있으면(`.venv` 또는 `venv`) 그 인터프리터로 pytest를 돌린다.

---

### Task 1: 익절 자동 트리거와 단순익절을 걷어낸다

**Files:**
- Modify: `src/risk/manager.py`, `src/core/engine.py`
- Test: `tests/test_risk/test_manager.py`, `tests/test_core/test_portfolio_exit.py`

**Interfaces:**
- Produces: `RiskManager(..., stop_loss_enabled: bool = True, ...)` — `take_profit_enabled`와 `simple_take_profit_enabled`가 사라진다. `take_profit_ratio`는 **남는다** (AI 기준선으로 넘길 값)
- `check_portfolio_exit(positions) -> Optional[ExitReason]` — 손절만 판정
- `check_simple_take_profits`와 `TradingEngine._execute_simple_take_profit`이 사라진다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_risk/test_manager.py`에 붙인다. 이 파일의 기존 `RiskManager` 조립 헬퍼를 그대로 쓴다.

```python
def test_portfolio_exit_no_longer_takes_profit():
    """익절은 자동 청산에서 빠졌다 — 이익이 아무리 커도 여기서는 팔지 않는다."""
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    profitable = [position(avg_price=1000.0, current_price=1100.0, quantity=10)]
    assert manager.check_portfolio_exit(profitable) is None


def test_portfolio_exit_still_stops_loss():
    manager = make_manager(take_profit_ratio=0.005, stop_loss_ratio=0.02)
    losing = [position(avg_price=1000.0, current_price=900.0, quantity=10)]
    assert manager.check_portfolio_exit(losing) is ExitReason.STOP_LOSS


def test_stop_loss_can_still_be_disabled():
    manager = make_manager(stop_loss_ratio=0.02, stop_loss_enabled=False)
    losing = [position(avg_price=1000.0, current_price=900.0, quantity=10)]
    assert manager.check_portfolio_exit(losing) is None


def test_take_profit_ratio_survives_as_a_reference_line():
    """자동 청산에는 안 쓰지만 AI에게 넘길 기준선이라 설정은 남는다."""
    manager = make_manager(take_profit_ratio=0.005)
    assert manager.take_profit_ratio == 0.005


def test_simple_take_profit_is_gone():
    manager = make_manager()
    assert not hasattr(manager, "check_simple_take_profits")
    assert not hasattr(manager, "simple_take_profit_enabled")
```

`make_manager`/`position` 헬퍼 이름은 그 파일이 실제로 쓰는 것에 맞춘다.

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_risk/test_manager.py -k "take_profit or stop_loss or simple" -v`
Expected: FAIL — `check_portfolio_exit`이 아직 익절을 판정한다

- [ ] **Step 3: `RiskManager`를 좁힌다**

`src/risk/manager.py`:

- 생성자에서 `take_profit_enabled`와 `simple_take_profit_enabled` 인자를 없앤다. `self.take_profit_enabled`/`self.simple_take_profit_enabled`도 없앤다
- `take_profit_ratio`는 그대로 둔다 — 주석을 고쳐 **자동 청산이 아니라 AI에게 주는 기준선**임을 밝힌다
- `check_portfolio_exit`에서 익절 분기(`if self.take_profit_enabled and ret >= self.take_profit_ratio`)를 지운다. docstring도 손절만 판정한다고 고친다
- `check_simple_take_profits` 메서드를 통째로 지운다

- [ ] **Step 4: 엔진에서 단순익절 경로를 걷어낸다**

`src/core/engine.py`:

- `_check_portfolio_exit`에서 `check_simple_take_profits` 호출과 그 뒤 분기를 지운다
- `_execute_simple_take_profit` 메서드를 통째로 지운다
- 남는 흐름: `check_portfolio_exit` → 손절이면 `_execute_portfolio_exit`

- [ ] **Step 5: 호출부를 고친다**

`src/core/runtime.py`의 `RiskManager(...)` 생성에서 지워진 인자를 뺀다. `src/ui/main_window.py`가 `risk_manager.take_profit_enabled` / `simple_take_profit_enabled`를 건드리는 곳이 있으면 Task 6에서 정리하므로, 여기서는 **엔진이 뜨는 데 필요한 최소한만** 고치고 UI는 그대로 둔다. `grep -rn "take_profit_enabled\|simple_take_profit" src/ tests/`로 남은 참조를 전부 찾아 확인한다.

- [ ] **Step 6: 기존 테스트를 고친다**

익절 자동 청산과 단순익절을 검증하던 기존 테스트가 있다. **지우지 말고**, 그 동작이 이제 없다는 것을 확인하는 쪽으로 바꾸거나, Task 5에서 AI 판단이 그 자리를 대신한다는 주석을 남기고 지운다. 어느 쪽인지 보고서에 적는다.

- [ ] **Step 7: 테스트**

Run: `pytest`
Expected: PASS

- [ ] **Step 8: 커밋**

```bash
git add src/risk/manager.py src/core/engine.py src/core/runtime.py tests/
git commit -m "익절 자동 청산과 단순익절을 걷어낸다"
```

---

### Task 2: `ExitReason.AI_JUDGMENT`과 순손익 궤적

**Files:**
- Modify: `src/core/events.py`
- Create: `src/core/exit_trace.py`
- Create: `tests/test_core/test_exit_trace.py`

**Interfaces:**
- Produces:
  - `ExitReason.AI_JUDGMENT = "ai_judgment"`
  - `TracePoint` dataclass — `at: datetime`, `portfolio_return: float`, `per_ticker: Dict[str, float]`, `prices: Dict[str, float]`
  - `ExitTrace` — `append(at, portfolio_return, per_ticker, prices) -> None`, `points() -> List[TracePoint]`, `clear() -> None`, `count: int`, `partial: bool`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_exit_trace.py`를 새로 만든다.

```python
from datetime import datetime

from src.core.exit_trace import ExitTrace


def test_append_and_read_back():
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {"005930": 0.006}, {"005930": 71000.0})
    points = trace.points()
    assert len(points) == 1
    assert points[0].portfolio_return == 0.004
    assert points[0].per_ticker["005930"] == 0.006


def test_clear_empties_the_trace():
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {}, {})
    trace.clear()
    assert trace.points() == []
    assert trace.count == 0


def test_partial_is_true_until_the_first_point():
    """엔진을 장중에 다시 켜면 궤적이 비어 시작한다 — 없는 것을 있는 척하면 안 된다."""
    trace = ExitTrace()
    assert trace.partial is True
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {}, {})
    assert trace.partial is False


def test_points_returns_a_copy():
    """호출측이 리스트를 건드려도 내부 상태가 흔들리지 않는다."""
    trace = ExitTrace()
    trace.append(datetime(2026, 9, 9, 9, 20), 0.004, {}, {})
    trace.points().clear()
    assert trace.count == 1
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_core/test_exit_trace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.core.exit_trace'`

- [ ] **Step 3: `ExitReason`에 값을 더한다**

`src/core/events.py`:

```python
class ExitReason(Enum):
    """전략과 무관하게 시스템이 강제 청산하는 사유."""
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    # AI 매도 판단 (PRD 5.5-B). 익절 자동 청산이 빠진 자리를 대신한다 — 나중에 성과를
    # 되짚을 때 손절·강제청산과 갈라서 보려고 별도 값으로 남긴다.
    AI_JUDGMENT = "ai_judgment"
```

`TAKE_PROFIT`은 **지우지 않는다.** 과거 `trades` 기록에 그 값이 남아 있고, 리포트가 그것을 읽는다.

- [ ] **Step 4: `ExitTrace`를 만든다**

`src/core/exit_trace.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass
class TracePoint:
    """한 시점의 손익 상태 — AI 매도 판단이 되돌림을 알아보는 근거다."""

    at: datetime
    portfolio_return: float          # 보유 합산 순손익률
    per_ticker: Dict[str, float]     # 종목별 순손익률
    prices: Dict[str, float]         # 종목별 현재가


class ExitTrace:
    """당일 순손익 궤적 (PRD 5.5-B 'AI 매도 판단').

    스냅샷 하나로는 +0.4%가 올라오는 중인지 +0.9%에서 밀린 것인지 구분할 수 없다.
    두 상황의 정답이 정반대라 경로가 필요하다.

    메모리에만 둔다 — 하루치 최대 24개 지점이라 파일로 남길 이유가 없고, 엔진을 다시
    켜면 비는 것이 정상이다. 그 사실은 `partial`로 드러낸다.
    """

    def __init__(self):
        self._points: List[TracePoint] = []

    def append(
        self,
        at: datetime,
        portfolio_return: float,
        per_ticker: Dict[str, float],
        prices: Dict[str, float],
    ) -> None:
        self._points.append(
            TracePoint(
                at=at,
                portfolio_return=portfolio_return,
                per_ticker=dict(per_ticker),
                prices=dict(prices),
            )
        )

    def points(self) -> List[TracePoint]:
        """사본을 돌려준다 — 호출측이 건드려도 내부 상태가 흔들리지 않는다."""
        return list(self._points)

    @property
    def count(self) -> int:
        return len(self._points)

    @property
    def partial(self) -> bool:
        """궤적이 비어 있는가 — 엔진을 장중에 다시 켠 직후가 그 상태다."""
        return not self._points

    def clear(self) -> None:
        self._points.clear()
```

- [ ] **Step 5: 테스트**

Run: `pytest tests/test_core/test_exit_trace.py -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/core/events.py src/core/exit_trace.py tests/test_core/test_exit_trace.py
git commit -m "AI 매도 판단이 볼 순손익 궤적을 담는다"
```

---

### Task 3: `src/llm/exit_advisor.py` — 판단 모듈

**Files:**
- Create: `src/llm/exit_advisor.py`
- Create: `tests/test_llm/test_exit_advisor.py`

**Interfaces:**
- Consumes: `TracePoint` (Task 2), `MAX_TOKENS`/`_extract_json` (`src/llm/recommender.py`)
- Produces:
  - `EXIT_PROMPT_TEMPLATE_VERSION = "v1"`
  - `HoldingView` dataclass — `ticker: str`, `name: str`, `quantity: int`, `avg_price: float`, `current_price: float`, `net_return: float`, `outlook: str`, `reason: str`
  - `ExitDecision` dataclass — `sell: bool`, `reason: str`
  - `build_exit_system_prompt() -> str`
  - `build_exit_user_prompt(holdings, trace, portfolio_return, stop_loss_ratio, take_profit_ratio, minutes_to_close, partial) -> str`
  - `parse_exit_decision(raw_text: str) -> ExitDecision`
  - `ExitAdvisor(settings)` / `ExitAdvisor.decide(...) -> Optional[ExitDecision]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_llm/test_exit_advisor.py`를 만든다. `tests/test_llm/test_reviewer.py`의 가짜 클라이언트 구조를 그대로 따른다.

```python
from datetime import datetime
from types import SimpleNamespace

from src.core.exit_trace import TracePoint
from src.llm.exit_advisor import (
    ExitAdvisor,
    HoldingView,
    build_exit_system_prompt,
    build_exit_user_prompt,
    parse_exit_decision,
)


def holding(ticker="005930", net_return=0.004):
    return HoldingView(
        ticker=ticker, name="삼성전자", quantity=23, avg_price=35_850.0,
        current_price=36_100.0, net_return=net_return,
        outlook="오전 중 전일 고가 36,150원 돌파를 시도할 것으로 봅니다.",
        reason="이동평균 대비 -6.0%까지 밀린 상태",
    )


def trace_points():
    return [
        TracePoint(datetime(2026, 9, 9, 9, 20), 0.009, {"005930": 0.009}, {"005930": 36_300.0}),
        TracePoint(datetime(2026, 9, 9, 9, 35), 0.004, {"005930": 0.004}, {"005930": 36_100.0}),
    ]


def test_parse_reads_sell_and_reason():
    result = parse_exit_decision('{"sell": true, "reason": "고점 대비 되돌림"}')
    assert result.sell is True
    assert result.reason == "고점 대비 되돌림"


def test_parse_defaults_to_hold_when_sell_is_missing():
    """형식이 어긋나도 매도 쪽으로 기울지 않는다 — 기본은 보유다."""
    result = parse_exit_decision('{"reason": "판단 불가"}')
    assert result.sell is False


def test_user_prompt_carries_the_trace_and_the_lines():
    prompt = build_exit_user_prompt(
        [holding()], trace_points(), portfolio_return=0.004,
        stop_loss_ratio=0.02, take_profit_ratio=0.005,
        minutes_to_close=330, partial=False,
    )
    assert "09:20" in prompt and "09:35" in prompt      # 궤적
    assert "+0.90%" in prompt and "+0.40%" in prompt    # 경로가 숫자로 보인다
    assert "-2.00%" in prompt                            # 손절선
    assert "+0.50%" in prompt                            # 익절 기준선
    assert "330" in prompt                               # 남은 시간
    assert "36,150원 돌파" in prompt                     # 아침 전망


def test_user_prompt_states_when_the_trace_is_partial():
    prompt = build_exit_user_prompt(
        [holding()], [], portfolio_return=0.004,
        stop_loss_ratio=0.02, take_profit_ratio=0.005,
        minutes_to_close=330, partial=True,
    )
    assert "궤적 일부 없음" in prompt


def test_system_prompt_makes_holding_the_default():
    prompt = build_exit_system_prompt()
    assert "기본은 보유" in prompt
    assert "손절" in prompt


def test_decide_returns_none_when_api_raises():
    advisor = ExitAdvisor.__new__(ExitAdvisor)
    advisor.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    advisor._client = Boom()
    assert advisor.decide([holding()], [], 0.004, 0.02, 0.005, 330, False) is None
```

`test_reviewer.py`의 `FakeBlock`/`FakeResponse`/`FakeClient`를 본떠 아래 세 테스트도 함께 쓴다 — `stop_reason`이 `max_tokens`/`refusal`일 때, 응답 텍스트가 빈 때, JSON이 깨졌을 때 각각 `decide`가 `None`을 돌려주고 예외를 내지 않는다.

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_llm/test_exit_advisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.llm.exit_advisor'`

- [ ] **Step 3: 모듈을 만든다**

`src/llm/reviewer.py`의 구조(스키마 → 프롬프트 빌더 → 파서 → 클라이언트, 실패는 전부 `None`)를 그대로 따른다.

스키마:

```python
EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "sell": {"type": "boolean", "description": "지금 전량 매도할지 여부"},
        "reason": {"type": "string", "description": "판단 근거 — 궤적의 구체적 수치를 인용"},
    },
    "required": ["sell", "reason"],
    "additionalProperties": False,
}
```

시스템 프롬프트에 반드시 담을 것:

- 역할 — 보유 종목을 지금 전량 정리할지 판단한다. **종목별 매도는 없다**
- **기본은 보유** — 확실한 근거가 없으면 팔지 않는다. 주기마다 묻는 구조라 매도 쪽으로 기울기 쉬운데, 그러면 전략 자체가 무의미해진다
- 손절선은 **코드가 자동으로 처리한다** — 거기 닿으면 이미 팔렸으므로 그 아래를 걱정하지 말 것
- 익절 기준선은 **자동 청산이 아니라 사용자가 정한 목표**다. 그 위라고 반드시 팔 이유는 없고, 그 아래라고 팔면 안 되는 것도 아니다
- 15:15에 전량 강제청산된다 — 남은 시간이 판단에 들어간다
- 판단 기준 둘: **되돌림**(고점 대비 얼마나 밀렸고 속도가 어떤가)과 **아침 전망의 유효성**(그때 본 시나리오가 아직 살아 있는가)
- `reason`에 **궤적의 구체적 수치를 인용**할 것. "모멘텀 약화" 같은 모호한 표현 금지

사용자 프롬프트에 담을 것: 궤적(시각 + 합산·종목별 순손익률), 현재 스냅샷(평단·현재가·수량·순손익률), 손절선과 남은 거리, 익절 기준선, 15:15까지 남은 분, 종목별 아침 `outlook`·`reason`, 그리고 `partial`이면 `궤적 일부 없음 — 엔진을 장중에 다시 켰습니다` 한 줄.

`parse_exit_decision`은 `sell`이 없거나 형식이 어긋나면 **`sell=False`**로 떨어진다.

`ExitAdvisor.decide`는 `output_config={"format": {...}, "effort": "low"}`로 부른다 — 스펙 10절대로 `effort`를 낮춘다. 실패 경로는 `reviewer.py`와 같이 전부 `None`.

- [ ] **Step 4: 테스트**

Run: `pytest tests/test_llm/test_exit_advisor.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llm/exit_advisor.py tests/test_llm/test_exit_advisor.py
git commit -m "보유 종목을 지금 정리할지 판단하는 LLM 모듈을 더한다"
```

---

### Task 4: 호출 주기 설정

**Files:**
- Modify: `config/settings.py`, `.env.example`
- Test: `tests/test_config/test_settings.py`

**Interfaces:**
- Produces: `Settings.ai_exit_interval_minutes: int` (기본 15), `AI_EXIT_INTERVAL_CHOICES = (15, 30, 60)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_ai_exit_interval_defaults_to_15(monkeypatch):
    monkeypatch.delenv("AI_EXIT_INTERVAL_MINUTES", raising=False)
    assert Settings().ai_exit_interval_minutes == 15


def test_ai_exit_interval_reads_env(monkeypatch):
    monkeypatch.setenv("AI_EXIT_INTERVAL_MINUTES", "60")
    assert Settings().ai_exit_interval_minutes == 60


def test_validate_rejects_an_unsupported_interval(monkeypatch):
    """UI 콤보에 없는 값이 .env로 들어오면 엔진 시작 단계에서 막는다."""
    monkeypatch.setenv("AI_EXIT_INTERVAL_MINUTES", "7")
    with pytest.raises(ValueError):
        Settings().validate()
```

`validate()`가 이미 있는 검증(추천/매수 시각 등)과 같은 방식으로 예외를 던지는지 그 파일에서 확인하고 맞춘다.

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_config/test_settings.py -k ai_exit -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ai_exit_interval_minutes'`

- [ ] **Step 3: 설정을 더한다**

`config/settings.py`:

```python
# AI 매도 판단 호출 주기 (분). UI 콤보가 주는 셋만 허용한다 — 그 밖의 값은
# validate()가 막는다 (PRD 5.5-B 'AI 매도 판단'). 비용이 여기서 갈린다.
AI_EXIT_INTERVAL_CHOICES = (15, 30, 60)
```

`Settings`에 `ai_exit_interval_minutes: int = field(default_factory=lambda: int(os.getenv("AI_EXIT_INTERVAL_MINUTES", "15")))`를 더하고, `validate()`에 값이 `AI_EXIT_INTERVAL_CHOICES` 안인지 검사를 더한다.

`.env.example`에 `AI_EXIT_INTERVAL_MINUTES=15`를 주석과 함께 더한다.

- [ ] **Step 4: 테스트**

Run: `pytest tests/test_config/test_settings.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add config/settings.py .env.example tests/test_config/test_settings.py
git commit -m "AI 매도 판단 호출 주기를 설정으로 뺀다"
```

---

### Task 5: 주기 워처 배선

**Files:**
- Modify: `src/core/runtime.py`, `src/core/engine.py`
- Test: `tests/test_core/test_ai_exit.py` (신규)

**Interfaces:**
- Consumes: `ExitTrace` (Task 2), `ExitAdvisor`/`HoldingView` (Task 3), `Settings.ai_exit_interval_minutes` (Task 4)
- Produces: `watch_ai_exit(runtime, ...) -> None` (async), `TradingEngine.exit_trace: ExitTrace`, `TradingEngine.ai_exit_enabled: bool` (기본 `True`)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_ai_exit.py`를 만든다. `tests/test_core/test_quote_watchdog.py`가 워처를 테스트하는 방식(가짜 런타임 + 한 사이클만 돌리기)을 그대로 따른다.

covering 할 것:

- 보유 종목이 없으면 advisor를 부르지 않는다
- `ai_exit_enabled`가 꺼져 있으면 부르지 않는다
- 창 밖(매수 시각 +15분 전 / 15:00 이후)이면 부르지 않는다
- 하루 상한을 넘으면 부르지 않는다
- 부르면 궤적에 한 점이 쌓인다
- `sell=True`면 `_execute_portfolio_exit`이 `ExitReason.AI_JUDGMENT`로 불린다
- `sell=False`면 아무것도 팔지 않는다
- advisor가 `None`을 돌려주면 아무것도 팔지 않는다
- 일일 초기화가 궤적과 카운터를 비운다

각 테스트가 실제로 그 분기를 짚는지 확인한다 — 다른 이유로 통과하면 안 된다.

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `pytest tests/test_core/test_ai_exit.py -v`
Expected: FAIL — `ImportError: cannot import name 'watch_ai_exit'`

- [ ] **Step 3: 엔진에 상태를 붙인다**

`src/core/engine.py`:

- `self.exit_trace = ExitTrace()`
- `self.ai_exit_enabled = True` — UI 체크박스가 켜고 끈다 (기존 `stop_loss_enabled`가 `risk_manager`에 붙은 것과 같은 자리)
- `self._ai_exit_calls = 0` — 하루 호출 카운터
- `reset_for_new_day`에서 `exit_trace.clear()`와 `_ai_exit_calls = 0`

- [ ] **Step 4: 워처를 만든다**

`src/core/runtime.py`에 `watch_cash_refresh`와 같은 꼴로 더한다.

```python
# AI 매도 판단 — 매수 체결 뒤 이 시간이 지나야 첫 판단을 한다. 체결 직후에는 근거가 없다.
AI_EXIT_START_DELAY_MINUTES = 15
# 15:15 강제청산 직전에는 부르지 않는다 — 어차피 곧 팔린다.
AI_EXIT_END_TIME = dt_time(15, 0)


async def watch_ai_exit(runtime: Runtime, poll_seconds: float = 30.0) -> None:
    """설정된 주기마다 보유 종목을 지금 정리할지 AI에 묻는다 (PRD 5.5-B 'AI 매도 판단').

    익절 자동 청산이 빠진 자리를 대신한다. 손절은 실시간 시세 콜백에서 그대로 도므로,
    이 워처가 멈춰도 하방은 지켜진다.

    LLM 호출은 별도 스레드로 넘긴다 — 20초 넘게 걸려 이벤트 루프를 막으면 WebSocket
    PING에 응답하지 못해 시세가 끊긴다. 매도 주문만 루프 스레드에서 실행해 실시간
    손절 감시와 직렬화한다.
    """
```

본문이 매 사이클에 확인할 것: 거래일인가 / `ai_exit_enabled`인가 / 창 안인가 / 마지막 호출로부터 주기가 지났는가 / 보유 종목이 있는가 / 상한 안인가. 하나라도 아니면 그 사이클은 건너뛴다.

호출할 때: 궤적에 한 점을 append → `HoldingView` 목록을 만들고(아침 `outlook`·`reason`은 `trade_store.recommendations_for(오늘)`에서 종목코드로 찾는다) → `run_in_executor`로 `advisor.decide(...)` → `sell=True`면 루프 스레드에서 `engine._execute_portfolio_exit(holdings, ExitReason.AI_JUDGMENT)`.

`build_runtime`에서 `ExitAdvisor(settings)`를 만들어 `Runtime`에 붙이고, 엔진 시작부에서 이 워처를 다른 워처들과 같은 자리에 태운다.

- [ ] **Step 5: 테스트**

Run: `pytest`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/core/ tests/test_core/test_ai_exit.py
git commit -m "주기마다 AI에게 전량 매도 여부를 묻는다"
```

---

### Task 6: UI

**Files:**
- Modify: `src/ui/main_window.py`
- Test: `tests/test_scripts/test_run_ui.py` (또는 이 저장소가 UI를 테스트하는 파일)

**Interfaces:**
- Consumes: `Settings.ai_exit_interval_minutes`, `AI_EXIT_INTERVAL_CHOICES` (Task 4), `engine.ai_exit_enabled` (Task 5)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

이 저장소가 UI를 테스트하는 방식에 맞춘다. 최소한 확인할 것:

- `AI 매도 판단` 체크박스가 기본 **켬**이다
- `단순익절적용` 체크박스가 없다
- 주기 콤보에 15/30/60만 있고, 저장하면 `.env`의 `AI_EXIT_INTERVAL_MINUTES`에 나간다
- 주기 값이 바뀌면 `_needs_restart_for_changed_settings`가 재시작 대상으로 판정한다
- `AI 매도 판단`을 끄면 주기 콤보가 비활성화되고, **입력란은 그대로 살아 있다**(손절이 쓴다)

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: 위 테스트 파일
Expected: FAIL

- [ ] **Step 3: 체크박스를 정리한다**

`src/ui/main_window.py`:

- `_take_profit_enabled` → `_ai_exit_enabled`로 바꾸고 라벨을 **`AI 매도 판단`**, 기본 `True`
- `_simple_take_profit_enabled`와 `_on_simple_take_profit_toggled`를 지운다
- `_on_take_profit_toggled`를 `_on_ai_exit_toggled`로 바꾸고, 배타 처리(단순익절과 서로 끄던 로직)를 지운다. 대신 주기 콤보의 활성/비활성만 처리한다
- `_stop_loss_enabled`는 그대로
- 토글이 `engine.ai_exit_enabled`와 `risk_manager.stop_loss_enabled`를 각각 갱신하게 한다
- 툴팁을 새 동작에 맞게 고친다 — 특히 **AI 매도 판단을 끄면 이익 쪽 청산이 없다**는 것을 밝힌다

- [ ] **Step 4: 라벨과 콤보**

- 입력란 라벨 `익절/손절 (%)` → **`익절 목표 / 손절 (%)`**, 플레이스홀더도 새 뜻에 맞게 고친다 (`예: 2 (손절 -2% 자동 / 익절 목표 +2%는 AI 참고선)`)
- 리스크 박스 제목 `리스크 관리 (익절 / 손절 / 갭 허용치)` → `리스크 관리 (익절 목표 / 손절 / 갭 허용치)`
- **AI 호출 주기** 콤보를 더한다 — `15분` / `30분` / `1시간`, 값은 `AI_EXIT_INTERVAL_CHOICES`
- 저장 시 `.env`의 `AI_EXIT_INTERVAL_MINUTES`로 나가고, 불러올 때 현재 값이 선택되게 한다
- `_needs_restart_for_changed_settings`에 이 값을 더한다

- [ ] **Step 5: 테스트**

Run: `pytest`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/ui/main_window.py tests/
git commit -m "익절 체크박스를 AI 매도 판단으로 바꾸고 호출 주기를 노출한다"
```

---

### Task 7: 문서

**Files:**
- Modify: `주식자동매매_PRD.md`, `CLAUDE.md`

- [ ] **Step 1: PRD 5.5-B의 청산 규칙을 고친다**

담을 내용:

- **익절이 자동 청산에서 빠졌다** — 근거는 10절의 2026-08-12 분석(27건 대조에서 익절선 어느 값도 "익절 없음"보다 못했다). 고정 선이 상방을 자른다면 상황을 보는 판단이 그 자리에 맞다
- **손절은 그대로 하드 라인** — 실시간 시세 콜백, 합산 순손익 기준
- **단순익절 제거** — 종목별 0% 익절이라 AI 판단(전량 단위)과 단위가 어긋났다
- **익절(%)은 참고선이 됐다** — 손절과 입력란을 계속 공유하며(2026-08-21 통합 유지), 같은 값이 손절선이자 AI에게 주는 익절 기준선이다
- **AI 매도 판단** — 조건(보유 있을 때만, 거래일, 매수 시각 +15분 ~ 15:00, 주기 설정), 입력(궤적·스냅샷·손절선·기준선·남은 시간·아침 전망), 출력(매도 여부만, 가격 없음), 실행(전량 시장가, `exit_reason="ai_judgment"`), 실패는 전부 "팔지 않음"
- **기본 상태는 AI 매도 판단 켬 + 손절 켬** (확정 2026-09-09). 익절 기본값 변천사를 적어 둔 자리에 이 변경을 이어 붙인다
- **받아들인 위험** — 설계 문서의 네 가지를 요약해 옮긴다: AI가 유일한 이익 실현 수단, 기본 켬, 상방 해상도가 주기만큼 거칠어짐, "기본은 보유"를 모델이 지키는지 미검증

- [ ] **Step 2: 일정 표와 비용을 적는다**

주기별 하루 호출 수(15분 23회 / 30분 12회 / 1시간 6회)와 월 비용 추정($17~35 / $8~18 / $4~9), 그리고 `effort: "low"`를 쓰는 이유를 남긴다.

- [ ] **Step 3: `CLAUDE.md`를 고친다**

"리스크 관리는 이중 구조" 절이 익절/손절을 한 쌍으로 서술하고 있다. 익절이 AI로 넘어갔고 단순익절이 사라졌다는 것, 체크박스가 셋에서 둘로 줄었다는 것, 기본 상태가 바뀌었다는 것을 반영한다. 익절 기본값 변천사를 적은 문단도 이 변경으로 이어 붙인다.

- [ ] **Step 4: 전체 테스트**

Run: `pytest`
Expected: PASS (문서 변경이라 무영향이어야 한다 — 확인용)

- [ ] **Step 5: 커밋**

```bash
git add 주식자동매매_PRD.md CLAUDE.md
git commit -m "익절을 AI 판단으로 넘긴 청산 규칙을 문서에 반영한다"
```

---

## Self-Review

**Spec coverage**

| 스펙 항목 | Task |
|---|---|
| 익절 자동 트리거 제거, 손절 유지 | 1 |
| 단순익절 제거 | 1 |
| `take_profit_ratio`가 기준선으로 남음 | 1 |
| `ExitReason.AI_JUDGMENT` | 2 |
| 궤적 기록·초기화·`partial` | 2, 5 |
| AI 입력 6종 | 3 (프롬프트), 5 (아침 전망 조회) |
| 출력은 매도 여부만, 기본은 보유 | 3 |
| `effort: "low"` | 3 |
| 호출 조건(보유·거래일·창·주기·상한) | 5 |
| 전량 시장가 실행, `exit_reason` | 5 |
| 실패는 전부 "팔지 않음" | 3 (`None`), 5 (분기) |
| 스레드 배분 | 5 |
| 주기 설정과 검증 | 4 |
| UI 체크박스·라벨·콤보·재시작 | 6 |
| PRD·`CLAUDE.md` | 7 |

**범위 밖 확인** — 매수 경로, 15:15 강제청산, 시스템 레벨 리스크(일일 손실 한도·총노출), 종목별 매도, 지정가 청산은 어느 Task에서도 건드리지 않는다.

**타입 일관성** — `ExitTrace`/`TracePoint`(Task 2)를 Task 3의 프롬프트 빌더와 Task 5의 워처가 같은 이름으로 쓴다. `HoldingView`/`ExitDecision`(Task 3)이 Task 5의 호출부와 일치한다. `ai_exit_enabled`가 Task 5(엔진)와 Task 6(UI)에서 같은 이름이다. `AI_EXIT_INTERVAL_CHOICES`(Task 4)를 Task 6의 콤보가 쓴다.

**순서 의존** — Task 1이 끝나면 **익절이 없는 상태로 잠시 남는다**(AI 판단은 Task 5에서 붙는다). 그 사이에 엔진을 실전으로 돌리면 이익 쪽 청산이 없다. Task 1~5를 한 번에 끝내기 전에는 실전 운영을 하지 않는다.
