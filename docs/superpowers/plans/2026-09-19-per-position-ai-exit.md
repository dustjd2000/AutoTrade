# AI 매도 판단 종목별 전환 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 매도 판단을 보유 목록 전량에서 종목별로 바꾸고, 주 목적을 "오른 종목의 이익을 제때 확정하는 것"으로 옮긴다.

**Architecture:** `ExitAdvisor`가 `{sell, reason}` 하나 대신 **종목별 판정 배열**을 받아 팔 종목코드 목록을 돌려주고, `run_ai_exit_cycle`이 그 종목만 기존 `_execute_portfolio_exit`에 넘긴다 — 2026-09-18에 종목별 손절이 이미 쓰는 경로다. 프롬프트에서는 합산 정보를 빼 AI가 합산 숫자로 종목을 판단하지 못하게 한다.

**Tech Stack:** Python 3.14 / pytest / anthropic SDK 1.2.0 (structured outputs)

## Global Constraints

- 설계 문서: `docs/superpowers/specs/2026-09-19-per-position-ai-exit-design.md` — 충돌하면 spec이 정본이다.
- **15:15 강제청산은 전량 그대로.** 손대지 않는다.
- **`_execute_portfolio_exit(holdings, reason, note=None)` 시그니처를 바꾸지 않는다** — 대상 목록을 좁혀 넘길 뿐이다. 종목별 손절이 같은 경로를 쓴다.
- **실패는 언제나 "팔지 않음"이다** — 호출 실패·타임아웃·형식 오류·응답 누락 전부.
- **호출 주기(30분)·호출 창(매수 +15분 ~ 15:00)·하루 상한·이익 반납 감시·공시 트리거는 건드리지 않는다.**
- **`exit_advisor`의 비스트리밍 120초 타임아웃은 이번에 고치지 않는다** (다음 작업).
- **UI·로그의 합산 값(`portfolio_return`, `portfolio_net_pnl`)은 유지한다** — 사람이 보는 용도다. 빼는 것은 **AI 프롬프트에 들어가는 합산**뿐이다.
- 파이썬은 `.venv/Scripts/python.exe`로 실행한다. 전체 테스트는 `.venv/Scripts/python.exe -m pytest -q` (현재 810개 통과).
- 줄 번호는 앞 태스크의 편집으로 밀리므로 **검색 문자열로 대상을 찾는다.**
- 깨지는 테스트는 **지우지 말고** 새 동작에 맞춰 고친다. 검증 대상 자체가 사라진 것만 삭제하고 보고서에 명시한다.
- 커밋 메시지는 한국어 평서문 한 줄 + 필요하면 본문. 끝에 빈 줄 하나를 두고 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- 태스크마다 커밋을 따로 남긴다 — 며칠 돌려 보고 항목 단위로 되돌릴 수 있어야 한다.

---

### Task 1: 종목별 판정 구조와 파서

응답 스키마와 파서만 바꾼다. 아직 아무도 새 구조를 쓰지 않으므로 이 태스크가 끝난 시점에도 동작은 그대로다.

**Files:**
- Modify: `src/llm/exit_advisor.py` (`EXIT_SCHEMA`, `ExitDecision`, `parse_exit_decision`)
- Test: `tests/test_llm/test_exit_advisor.py`

**Interfaces:**
- Consumes: 없음
- Produces: 아래 두 가지. Task 2·3이 이것을 쓴다.
  ```python
  @dataclass
  class PositionExit:
      ticker: str
      sell: bool
      reason: str

  @dataclass
  class ExitDecision:
      decisions: List[PositionExit]

      def sell_tickers(self, held: Iterable[str]) -> List[str]: ...
      def note(self, held: Iterable[str]) -> str: ...
  ```

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_llm/test_exit_advisor.py` 끝에 추가한다. 파일 상단의 import에 `PositionExit`와 `parse_exit_decision`이 있는지 확인하고 없으면 추가한다.

```python
# ── 종목별 판정 (2026-09-19) ──────────────────────────────
def test_parse_picks_only_the_sold_tickers():
    raw = """{"decisions": [
        {"ticker": "005930", "sell": true, "reason": "고점 +2.1%에서 +0.9%로 절반 넘게 반납"},
        {"ticker": "000660", "sell": false, "reason": "아침 시나리오가 아직 유효"}
    ]}"""

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930", "000660"]) == ["005930"]


def test_parse_treats_missing_ticker_as_hold():
    """응답에 없는 보유 종목은 보유 — 누락이 매도 쪽으로 기울면 안 된다."""
    raw = '{"decisions": [{"ticker": "005930", "sell": true, "reason": "반납"}]}'

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930", "000660"]) == ["005930"]


def test_parse_drops_unheld_ticker():
    """보유하지 않은 종목을 팔라고 해도 버린다 (환각 방어)."""
    raw = '{"decisions": [{"ticker": "999999", "sell": true, "reason": "환각"}]}'

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930"]) == []


def test_parse_non_boolean_sell_is_hold():
    """sell이 불리언 true가 아니면 보유 — 기존 원칙 그대로."""
    raw = '{"decisions": [{"ticker": "005930", "sell": "yes", "reason": "애매"}]}'

    decision = parse_exit_decision(raw)

    assert decision.sell_tickers(["005930"]) == []


def test_parse_rejects_non_object():
    with pytest.raises(Exception):
        parse_exit_decision("[]")


def test_note_carries_per_ticker_reasons():
    """매도한 종목의 사유만 묶는다 — 청산 로그와 알림 메일이 이걸 싣는다."""
    raw = """{"decisions": [
        {"ticker": "005930", "sell": true, "reason": "반납 절반 초과"},
        {"ticker": "000660", "sell": false, "reason": "유효"}
    ]}"""

    note = parse_exit_decision(raw).note(["005930", "000660"])

    assert "005930" in note
    assert "반납 절반 초과" in note
    assert "000660" not in note
```

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_llm/test_exit_advisor.py -q
```

기대: 새 테스트 6개가 FAIL (`AttributeError: 'ExitDecision' object has no attribute 'sell_tickers'` 또는 `ImportError`). 기존 테스트 중 `ExitDecision(sell=..., reason=...)`을 쓰던 것도 함께 깨진다 — Step 3 뒤에 고친다.

- [ ] **Step 3: 스키마와 구조를 바꾼다**

`src/llm/exit_advisor.py`에서 `EXIT_SCHEMA`를 찾아 통째로 바꾼다.

```python
# 응답 스키마 — 종목마다 판정과 근거를 받는다 (확정 2026-09-19). 전량 판정이던 시절에는
# {sell, reason} 하나였다.
EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "종목코드 6자리"},
                    "sell": {"type": "boolean", "description": "이 종목을 지금 매도할지"},
                    "reason": {
                        "type": "string",
                        "description": "판단 근거 — 그 종목 궤적의 구체적 수치를 인용",
                    },
                },
                "required": ["ticker", "sell", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}
```

`@dataclass class ExitDecision:` 블록을 찾아 아래로 바꾼다 (기존 `sell`/`reason` 두 필드를 대체한다).

```python
@dataclass
class PositionExit:
    """한 종목에 대한 판정."""

    ticker: str
    sell: bool
    reason: str


@dataclass
class ExitDecision:
    """한 주기의 종목별 판정 묶음 (확정 2026-09-19, PRD 5.5-B).

    보유하지 않은 종목과 응답에서 빠진 종목은 `sell_tickers`가 걸러낸다 — 누락이
    매도 쪽으로 기울면 안 되므로 **모르는 것은 전부 보유**로 떨어진다.
    """

    decisions: List[PositionExit]

    def sell_tickers(self, held: Iterable[str]) -> List[str]:
        """보유 중이면서 매도로 판정된 종목코드."""
        held_set = set(held)
        return [d.ticker for d in self.decisions if d.sell and d.ticker in held_set]

    def note(self, held: Iterable[str]) -> str:
        """매도한 종목의 사유만 묶는다 — 청산 로그와 알림 메일에 실린다."""
        held_set = set(held)
        return " / ".join(
            f"{d.ticker}: {d.reason}" for d in self.decisions if d.sell and d.ticker in held_set
        )
```

`parse_exit_decision`을 찾아 아래로 바꾼다.

```python
def parse_exit_decision(raw_text: str) -> ExitDecision:
    """청산 판단 응답을 파싱한다.

    `sell`이 불리언 `true`가 아니면 그 종목은 보유로 떨어진다 — 기본은 보유이므로
    형식 오류가 매도 쪽으로 기울면 안 된다. JSON 자체가 깨졌거나 최상위가 객체가
    아니면 이 함수가 예외를 던지고, 호출측(`ExitAdvisor.decide`)이 이를 None으로 받아
    그 주기에 아무것도 팔지 않는다.
    """
    data = json.loads(_extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("LLM exit response must be a JSON object")
    items = data.get("decisions")
    if not isinstance(items, list):
        raise ValueError("LLM exit response must carry a 'decisions' array")
    return ExitDecision(
        decisions=[
            PositionExit(
                ticker=str(item.get("ticker", "")).strip(),
                sell=item.get("sell") is True,
                reason=str(item.get("reason", "")).strip(),
            )
            for item in items
            if isinstance(item, dict)
        ]
    )
```

`from typing import ...` 줄에 `Iterable`이 없으면 추가한다.

- [ ] **Step 4: 깨진 기존 테스트를 고친다**

```
grep -rn "ExitDecision(" src/ tests/ --include=*.py
```

`ExitDecision(sell=..., reason=...)` 형태를 쓰는 곳을 새 구조에 맞춘다. **테스트를 지우지 말고** 새 형태로 같은 것을 검증하게 고친다. `src/core/runtime.py`의 `decision.sell` / `decision.reason` 참조는 **Task 3에서** 고치므로 지금은 건드리지 않는다 — 이 태스크에서는 테스트만 통과하면 된다.

- [ ] **Step 5: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_llm/test_exit_advisor.py -q
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. `runtime.py`가 아직 옛 필드를 참조해 깨지면, 그 테스트만 Task 3까지 임시로 새 구조에 맞춰 두고 보고서에 적는다.

- [ ] **Step 6: 커밋한다**

```bash
git add src/llm/exit_advisor.py tests/test_llm/test_exit_advisor.py
git commit -m "AI 매도 판단 응답을 종목별 판정으로 받는다"
```

---

### Task 2: 프롬프트를 종목별로 다시 쓰고 합산을 뺀다

**Files:**
- Modify: `src/llm/exit_advisor.py` (`build_exit_system_prompt`, `build_exit_user_prompt`, `ExitAdvisor.decide`)
- Modify: `src/core/runtime.py` (호출부 인자)
- Test: `tests/test_llm/test_exit_advisor.py`

**Interfaces:**
- Consumes: Task 1의 `ExitDecision`/`PositionExit`
- Produces: `build_exit_user_prompt(holdings, trace, minutes_to_close, partial)` — `portfolio_return`과 `portfolio_peak` 인자가 사라진다. `ExitAdvisor.decide(holdings, trace, minutes_to_close, partial, timeout_seconds=120.0)`도 같다. Task 3이 이 시그니처로 부른다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_user_prompt_has_no_portfolio_aggregate():
    """합산을 주면 AI가 종목별 판단에 그 숫자를 쓴다 — 손절선을 뺀 것과 같은 이유다.

    2026-09-18에 손절선을 뺀 근거가 그대로 적용된다: 시스템 프롬프트가 신경 쓰지
    말라고 못박아도, 숫자가 주어지면 그것이 근거가 된다.
    """
    prompt = build_exit_user_prompt(
        holdings=[],
        trace=[],
        minutes_to_close=300,
        partial=False,
    )

    assert "합산" not in prompt


def test_system_prompt_asks_per_position_judgment():
    prompt = build_exit_system_prompt()

    assert "전량" not in prompt
    assert "종목마다" in prompt or "종목별" in prompt
```

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_llm/test_exit_advisor.py -q -k "no_portfolio_aggregate or per_position_judgment"
```

기대: FAIL — `TypeError`(없앨 인자를 아직 요구함) 또는 단언 실패.

- [ ] **Step 3: user prompt에서 합산을 뺀다**

`build_exit_user_prompt`의 시그니처에서 `portfolio_return: float,`와 `portfolio_peak: Optional[float] = None,`를 지운다.

`lines = [` 블록에서 아래 두 가지를 지운다.

- `"\n## 현재"`와 `f"- 합산 순손익률: {_pct(portfolio_return)}"` 두 항목
- 그 아래 `if portfolio_peak is not None:` 블록 전체 (`- 합산 당일 고점: ...`)

궤적 렌더링을 찾는다.

```python
    lines.append("\n## 순손익률 궤적 (시각 → 합산 / 종목별)")
```

아래로 바꾼다. 합산 열을 빼고 종목별만 남긴다.

```python
    lines.append("\n## 순손익률 궤적 (시각 → 종목별)")
```

그 아래 루프에서 `f"- {point.at:%H:%M} 합산 {_pct(point.portfolio_return)} ({per_ticker})"`를 찾아 아래로 바꾼다.

```python
        lines.append(f"- {point.at:%H:%M} {per_ticker}")
```

마지막 줄 `lines.append("\n지금 전량 매도할지 판단하세요.")`를 아래로 바꾼다.

```python
    lines.append("\n종목마다 지금 매도할지 판단하세요.")
```

`TracePoint`의 `portfolio_return` 필드 자체는 **지우지 않는다** — UI와 로그가 쓴다.

- [ ] **Step 4: 시스템 프롬프트를 다시 쓴다**

`build_exit_system_prompt`에서 `## 역할` 절을 찾아 아래로 바꾼다.

```
## 역할
보유 종목을 **하나씩** 보고, 그 종목을 지금 매도할지 종목마다 판단합니다.
판단의 주된 목적은 **오른 종목의 이익을 제때 확정하는 것**입니다 — 이 시스템에는
고정 익절선이 없어, 이익을 실현할지 정하는 것은 이 판단뿐입니다.
```

`## 기본은 보유입니다` 절을 찾아 아래로 바꾼다.

```
## 기본은 보유입니다
확실한 근거가 없으면 팔지 않습니다. 이 판단은 30분 남짓한 주기로 반복해서 묻는
구조이고 종목마다 따로 묻기까지 하므로, 매번 무언가 이유를 찾아 매도 쪽으로 기울기
쉽습니다. 그렇게 되면 이 판단 자체가 무의미해집니다. "이 정도면 팔아도 되지 않을까"
수준의 애매한 근거로는 그 종목의 `sell`을 `false`로 남기십시오.

## 손실 중인 종목은 문턱이 더 높습니다
순손익률이 마이너스인 종목은 **아침 시나리오가 무너졌다는 구체적 근거가 있을 때만**
매도하십시오. 마이너스라는 사실 자체는 근거가 아닙니다. 하방은 손절선이 맡고 있고,
그 선에 닿으면 이 판단과 무관하게 코드가 자동으로 그 종목을 정리합니다. 손실 구간에서
서둘러 파는 것은 이 판단의 역할이 아닙니다.
```

`## 판단 기준` 절의 도입부에 한 줄을 덧붙인다 (기준 1·2·3의 내용은 그대로 둔다).

```
## 판단 기준
아래 기준을 **종목마다 따로** 적용하십시오.
```

`## reason 작성 지침` 절에서 예시 문장을 종목별로 바꾼다.

```
`reason`에는 **그 종목** 궤적의 구체적 수치를 인용하십시오. ("고점 +2.10%에서
+0.90%로 1.20%p 반납했다"처럼.) "모멘텀이 약화되었다", "분위기가 좋지 않다" 같은
모호한 표현은 금지합니다.
```

- [ ] **Step 5: 호출부 시그니처를 맞춘다**

`ExitAdvisor.decide`의 시그니처에서 `portfolio_return: float,`와 `portfolio_peak: Optional[float] = None,`를 지우고, 내부의 `build_exit_user_prompt(...)` 호출에서도 두 인자를 뺀다.

`src/core/runtime.py`의 `decide(` 호출부에서 같은 두 인자를 지운다.

```
grep -n "decide(\|portfolio_return\|portfolio_peak" src/core/runtime.py
```

`runtime.py`에서 **UI·로그용으로 합산을 계산하는 코드는 지우지 않는다** — AI에 넘기는 인자만 뺀다.

- [ ] **Step 6: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. 합산 문자열을 단언하던 기존 프롬프트 테스트가 깨지면 새 동작에 맞춰 고친다.

- [ ] **Step 7: 커밋한다**

```bash
git add src/llm/exit_advisor.py src/core/runtime.py tests/test_llm/test_exit_advisor.py
git commit -m "AI 매도 판단 프롬프트를 종목별로 바꾸고 합산을 뺀다"
```

---

### Task 3: 판정된 종목만 매도한다

여기서 실제 동작이 바뀐다.

**Files:**
- Modify: `src/core/runtime.py` (`run_ai_exit_cycle`)
- Modify: `src/core/engine.py` (`note_ai_exit_result` — 필요한 경우)
- Test: `tests/test_core/test_ai_exit.py`

**Interfaces:**
- Consumes: Task 1의 `ExitDecision.sell_tickers(held)` / `.note(held)`, Task 2의 `decide(...)` 시그니처
- Produces: 동작 변경. `_execute_portfolio_exit(holdings, reason, note=None)` 시그니처는 그대로다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_core/test_ai_exit.py`에 추가한다. 이 파일에는 이미 다음이 있으니 그대로 쓴다 — `make_runtime(holdings=..., decide_result=...)`가 가짜 런타임을 만들고, `FakeEngine.executed`가 `[(holdings, reason, note), ...]` 형태로 `_execute_portfolio_exit` 호출을 기록하며, `holding(ticker=..., ...)`가 보유 종목 하나를 만든다. `IN_WINDOW`는 호출 창 안의 시각 상수다.

```python
# ── 종목별 판정 (2026-09-19) ──────────────────────────────
def test_cycle_sells_only_the_judged_ticker():
    """팔기로 판정된 종목만 주문이 나가고 나머지는 보유로 남는다.

    전량 판정이던 시절에는 sell=true 하나로 보유 목록 전체가 나갔다.
    """
    holdings = [holding(ticker="005930"), holding(ticker="000660", name="SK하이닉스")]
    runtime, engine, _ = make_runtime(
        holdings=holdings,
        decide_result=ExitDecision(
            decisions=[
                PositionExit(ticker="005930", sell=True, reason="고점 +2.10%에서 +0.90%로 반납"),
                PositionExit(ticker="000660", sell=False, reason="아침 시나리오가 아직 유효"),
            ]
        ),
    )

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert len(engine.executed) == 1
    sold, _reason, note = engine.executed[0]
    assert [p.ticker for p in sold] == ["005930"]
    assert "005930" in note
    assert "000660" not in note


def test_cycle_sells_nothing_when_every_ticker_holds():
    """전부 보유 판정이면 매도가 없다 — 사유는 UI 기록에 남는다."""
    holdings = [holding(ticker="005930"), holding(ticker="000660", name="SK하이닉스")]
    runtime, engine, _ = make_runtime(
        holdings=holdings,
        decide_result=ExitDecision(
            decisions=[
                PositionExit(ticker="005930", sell=False, reason="되돌림 미미"),
                PositionExit(ticker="000660", sell=False, reason="시나리오 유효"),
            ]
        ),
    )

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert engine.executed == []
    assert engine.ai_exit_results[-1][1] is False  # sell=False


def test_cycle_skips_a_ticker_already_sold_by_stop_loss():
    """응답을 기다리는 동안 손절이 먼저 판 종목은 제외된다.

    fresh_holdings가 매도 직전 재조회 결과다 — 그 목록에 없으면 팔 것이 없다.
    """
    runtime, engine, _ = make_runtime(
        holdings=[holding(ticker="005930")],
        fresh_holdings=[],
        decide_result=ExitDecision(
            decisions=[PositionExit(ticker="005930", sell=True, reason="반납")]
        ),
    )

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert engine.executed == []
```

`ExitDecision`과 `PositionExit`을 이 파일 상단 import에 추가한다 (`from src.llm.exit_advisor import ExitDecision, PositionExit` — 기존 import 줄에 이미 `exit_advisor`에서 가져오는 것이 있으면 거기 더한다).

- [ ] **Step 2: 실패를 확인한다**

```
.venv/Scripts/python.exe -m pytest tests/test_core/test_ai_exit.py -q -k "only_the_judged or all_hold"
```

기대: FAIL — 두 종목 모두 주문이 나가거나, `AttributeError: 'ExitDecision' object has no attribute 'sell'`.

- [ ] **Step 3: 실행 경로를 바꾼다**

`src/core/runtime.py`의 `run_ai_exit_cycle`에서 아래를 찾는다.

```python
    if decision is None or not decision.sell:
```

이 분기부터 `engine._execute_portfolio_exit(fresh_holdings, ExitReason.AI_JUDGMENT, note=decision.reason)`까지를 아래로 바꾼다.

```python
    if decision is None:
        reason_text = "LLM 호출 실패·타임아웃·형식 오류 (이번 주기는 매도하지 않습니다)"
        logger.info("AI 매도 판단: 보유 유지 (%s)", reason_text)
        engine.note_ai_exit_result(now, sell=False, reason=reason_text, ok=False)
        return

    held = [position.ticker for position in holdings]
    targets = decision.sell_tickers(held)
    if not targets:
        # 판정은 받았으나 전부 보유 — 종목별 사유를 그대로 남겨 다음 주기에 되짚을 수 있게 한다
        reason_text = " / ".join(f"{d.ticker}: {d.reason}" for d in decision.decisions)
        logger.info("AI 매도 판단: 보유 유지 (%s)", reason_text)
        engine.note_ai_exit_result(now, sell=False, reason=reason_text)
        return

    # 매도 주문은 루프 스레드에서 그대로 실행해 실시간 손절 감시와 직렬화한다. LLM 응답을
    # 기다리는 동안(최대 120초) 손절이 먼저 정리했을 수 있어, 판단 시점 스냅샷을 그대로
    # 팔지 않고 매도 직전에 보유 종목을 다시 읽는다 (force_close_all_positions와 같은 이유).
    fresh_holdings = engine.exit_candidates(force=True)
    fresh_targets = [p for p in fresh_holdings if p.ticker in set(targets)]
    note = decision.note(held)
    engine.note_ai_exit_result(now, sell=True, reason=note)
    if not fresh_targets:
        logger.info("AI 매도 판단: 매도로 판단했지만 그 사이 그 종목이 이미 정리되었습니다.")
        return

    logger.warning("AI 매도 판단: %d종목 매도 (%s)", len(fresh_targets), note)
    # 근거를 함께 넘겨 청산 로그와 알림 메일에 남긴다 — 사유 코드(ai_judgment)만으로는
    # 왜 팔았는지 나중에 되짚을 수 없다.
    engine._execute_portfolio_exit(fresh_targets, ExitReason.AI_JUDGMENT, note=note)
```

`holdings`가 이 함수 안에서 어떤 이름으로 있는지 먼저 확인하고(판정에 넘긴 보유 스냅샷), 그 이름을 쓴다.

- [ ] **Step 4: 통과를 확인한다**

```
.venv/Scripts/python.exe -m pytest -q
```

기대: 전부 PASS. "전량 매도" 전제의 기존 테스트가 깨지면 새 동작에 맞춰 고친다. 각 테스트에 왜 바뀌었는지 한 줄 주석을 남긴다.

- [ ] **Step 5: 커밋한다**

```bash
git add src/core/runtime.py src/core/engine.py tests/test_core/test_ai_exit.py
git commit -m "AI 매도 판단이 고른 종목만 판다"
```

---

### Task 4: 문서를 갱신한다

**Files:**
- Modify: `주식자동매매_PRD.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1~3의 최종 동작
- Produces: 없음

- [ ] **Step 1: 현재 거짓이 된 서술을 전부 찾는다**

**ripgrep 기반 Grep 도구를 쓴다** — Bash grep은 한글 다바이트 패턴을 놓친다. 2026-09-18 작업에서 실제로 두 번 놓쳤다.

패턴: `전량|보유 목록 전체|AI 매도 판단`

`주식자동매매_PRD.md`, `CLAUDE.md`, `README.md`, `src/`를 훑고, 걸리는 것 하나하나가 **지금도 참인지** 판정한다. 다음은 정당하다.

- **15:15 강제청산** 맥락의 "전량" — 그대로다
- 날짜가 박힌 **과거 결정 로그** (PRD 10절) — 그때는 참이었던 기록
- 시한부·과거형으로 명시된 서술

- [ ] **Step 2: PRD 5.5-B를 고친다**

"AI 매도 판단" 절에 반영할 것:
- 판정 단위가 **종목별**이고, 팔기로 한 종목만 매도한다
- 주 목적은 **오른 종목의 이익을 제때 확정하는 것** — 고정 익절선이 없어 이익 실현을 정하는 것은 이 판단뿐이다
- **손실 중인 종목은 문턱이 높다** — 아침 시나리오가 무너졌다는 구체적 근거가 있을 때만. 하방은 손절선이 맡는다
- 프롬프트에서 **합산 순손익률·합산 당일 고점·궤적의 합산 열을 뺐다**. 이유는 손절선을 뺀 것과 같다 — 종목별 판단에 합산 숫자를 주면 그것으로 판단한다
- 응답에서 빠진 종목과 보유하지 않은 종목은 **보유**로 떨어진다
- **15:15 강제청산은 전량 그대로**

"매도(청산) 조건" 절의 청산 경로 표에서 AI 판단의 단위를 고친다.

- [ ] **Step 3: PRD 10절에 결정을 기록한다**

11절 시작 직전에 항목을 추가한다. 담을 것:

- 계기: 2026-09-18에 손절을 종목별로 되돌리면서 청산 경로에서 AI 판단만 전량으로 남았다. 트리거(이익 반납 감시·공시)는 종목별인데 판단이 전량이라 한 종목의 신호가 보유 전체의 운명을 정했다
- 데이터: 손절선이 -5%인데 AI가 판 6번의 시점 합산이 -2.45% / -1.85% / -0.46% / -0.26% / -0.19% / +0.27%로, **손실 종목을 손절선 한참 전에 파는 장치로 동작**하고 있었다. 관측된 종목별 당일 최대 이익이 +2.17%에 그친 것도 이 때문이다
- 합산이 개별을 가린 사례: 09-18 NC +0.97% / NH투자증권 -1.19% → 합산 -0.19%로 둘 다 정리
- 결정 네 가지(종목별 전환, 주 목적을 이익 확정으로, 손실 종목 문턱, 프롬프트에서 합산 제거)와 그 이유
- **검토된 대안**: "AI는 이익 구간 종목만 보게 닫자"는 안이 함께 논의됐고, 조급한 매도가 구조적으로 불가능해지는 대신 시나리오가 깨진 종목을 -5%까지 들고 가야 해서 **열어두는 쪽을 택했다**
- **2026-08-10(합산 전환)·2026-09-18(종목별 손절) 항목은 지우지 않는다** — 교차 참조를 한 줄씩 덧붙인다
- 받아들인 위험: 이익 확정이 늘어날지는 돌려봐야 안다 / 판단 대상이 N배라 매도가 잦아질 수 있다 / 응답이 길어져 비스트리밍 120초 타임아웃 위험이 커진다(다음 작업에서 스트리밍으로 고친다) / 한 종목이 팔린 뒤 남은 종목의 판단은 다음 주기(최대 30분 뒤)다

- [ ] **Step 4: CLAUDE.md를 고친다**

"AI 매도 판단" 단락에서 아래가 사실과 어긋난다.

- "보유 종목 전체를 LLM에 보여 주고 **지금 전량 정리할지**를 묻는다" → 종목마다 판단한다
- "실행은 기존 `_execute_portfolio_exit` 경로의 **전량 시장가**" → 판정된 종목만
- "**판정 단위는 종목이 아니라 보유 목록 전체다**" 계열 서술 → 손절·AI 판단 모두 종목별, 15:15만 전량
- "**판정만 종목별이고 팔 때는 그대로 전량**" (이익 반납 감시 단락) → 이제 판단도 종목별이다
- "파는 단위가 보유 목록 전체라 키워드 하나로 결정할 일이 아니다" (장중 공시 단락) → 전제가 바뀌었다

- [ ] **Step 5: README.md를 확인한다**

청산 설명에 AI 판단 단위가 나오면 갱신한다. **이 파일의 다른 오래된 내용(옛 스케줄, 시장가 매수, 291종목 유니버스)은 이번 작업과 무관하므로 건드리지 않는다.**

- [ ] **Step 6: 전체 테스트와 커밋**

```
.venv/Scripts/python.exe -m pytest -q
git add 주식자동매매_PRD.md CLAUDE.md README.md src/
git commit -m "AI 매도 판단 종목별 전환을 문서에 반영한다"
```

---

## 완료 확인

- [ ] `.venv/Scripts/python.exe -m pytest -q` — 810개 이상 전부 통과
- [ ] AI 프롬프트에 합산이 없다 — `build_exit_user_prompt(holdings=[], trace=[], minutes_to_close=300, partial=False)` 결과에 "합산" 문자열 없음
- [ ] 15:15 강제청산은 여전히 전량이다 (`force_close_all_positions` 경로를 건드리지 않았는지 확인)
- [ ] 실패가 "팔지 않음"으로 떨어진다 — 호출 실패·형식 오류·응답 누락·보유하지 않은 티커 전부
- [ ] 커밋이 항목별로 4개 남았는지 — 되돌릴 때 항목 단위로 집을 수 있어야 한다
