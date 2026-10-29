# 2호 전략 규칙 자동 검증·적용 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 1분봉으로 2호 전략 규칙 후보 160개를 매주 walk-forward 검증하고, 엄격한 조건을 모두 만족하면 `.env`를 자동으로 바꾼다.

**Architecture:** 엔진 안의 스케줄 작업 두 개(15:45 분봉 수집, 금요일 16:00 평가)가 실행 큐(`ActionRunner`)를 탄다. 순수 계산(`src/research/backtest.py`, `rule_search.py`)과 파일 저장(`minute_store.py`)을 오케스트레이터(`rule_tuning.py`)가 묶고, 결과는 `trades.db`의 `rule_evaluations`와 메일로 남는다. 적용하면 UI가 설정을 다시 읽고 엔진을 재시작한다.

**Tech Stack:** Python 3.14, sqlite3, PyQt6(UI 연결만), pytest. 외부 라이브러리 추가 없음.

**Spec:** `docs/superpowers/specs/2026-10-29-rule-auto-tuning-design.md`

## Global Constraints

- 실행: `.venv\Scripts\python.exe -m pytest ...` (Windows, PowerShell 5.1 — `&&` 금지, `;` 사용).
- 주석·docstring·로그·메일 문구는 한국어, 식별자·커밋 메시지는 영어. 주변 코드의 주석 밀도를 따른다.
- `.env`를 직접 고치지 않는다. API(키움·Anthropic)를 부르는 코드를 테스트에서 실제로 실행하지 않는다.
- 후보: 신호 문턱 `0, 0.1, 0.2, 0.3`(%) × 뒤집기 `켬, 끔` × 결정 시각 `09:05, 09:10, 09:30, 10:00` × 손절 `1, 1.5, 2, 2.5, 3`(%) = 160개.
- 시험 구간 63거래일, 고르기 구간 최소 57거래일, 신호 계산 20거래일 → 사용 가능 거래일 140일 미만이면 평가하지 않는다.
- 적용 조건: 시험 구간 1등 − 현행 ≥ 0.05%p/일, 1등 시험 구간 > 0, 최근 4번 평가 연속 같은 1등이고 4번 모두 앞의 두 조건 충족, 마지막 적용 뒤 4번 이상 평가, `RULE_AUTO_APPLY_ENABLED=1`.
- 일 평균은 쉬는 날을 0%로 넣어 전체 거래일로 나눈다.
- 비용: 수수료 `Settings.commission_ratio` 양방향, 슬리피지 체결당 2.5원(호가 반칸). 하루 봉 300개 미만인 날은 뺀다.
- 신설 설정 기본값은 지금 동작과 같다: `ETF_SIGNAL_BAND_PERCENT=0`, `ETF_FLIP_ENABLED=1`, `RULE_AUTO_APPLY_ENABLED=1`.
- 결정 시각을 바꾸면 `BUY_TIME` = 결정 + 3분.
- 커밋은 각 Task 끝에서 한다. 메시지 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| 파일 | 책임 |
|---|---|
| `config/settings.py` (수정) | 신설 설정 3개 |
| `src/strategy/etf_regime.py` (수정) | `pick_etf`에 문턱·뒤집기 인자 |
| `src/core/daily_workflow.py` (수정) | 실전 결정이 신설 설정을 쓰게 함, 쉼 사유 문구 |
| `src/research/__init__.py` (신규) | 빈 파일 |
| `src/research/minute_store.py` (신규) | 1분봉 JSON 저장소, `Bar`/`DayBars` 타입 |
| `src/api/market_data.py` (수정) | `get_minute_candles` (ka10080 연속조회) |
| `src/research/backtest.py` (신규) | `Rule`, `Backtester` — 규칙 하나의 거래일별 순수익률 |
| `src/research/rule_search.py` (신규) | 후보 생성, 평가, 연속·휴지 판정 |
| `src/logger/trade_store.py` (수정) | `rule_evaluations` 테이블, `RuleEvaluationRow` |
| `src/notification/templates.py` (수정) | `rule_evaluation_email` |
| `src/research/rule_tuning.py` (신규) | 수집·평가·적용 오케스트레이터 `RuleTuning` |
| `src/core/actions.py`, `src/core/runtime.py` (수정) | 스케줄 액션·잡 등록, 금요일 가드 |
| `src/ui/engine_thread.py`, `src/ui/main_window.py` (수정) | 적용 뒤 설정 재로딩·재시작 |
| `.env.example`, `CLAUDE.md`, `주식자동매매_PRD.md` (수정) | 문서 |

---

### Task 1: 문턱·뒤집기를 설정으로 꺼내기 (동작 변화 없음)

**Files:**
- Modify: `config/settings.py` (`etf_start_date` 필드 바로 아래, ~line 219)
- Modify: `src/strategy/etf_regime.py:96-112` (`pick_etf`)
- Modify: `src/core/daily_workflow.py` (생성자 ~line 107-145, `_recommend_etf` ~line 410-420, `_send_etf_mail` ~line 572-585)
- Modify: `src/core/runtime.py` (`build_runtime`의 `DailyWorkflow(...)` 호출)
- Modify: `.env.example`
- Test: `tests/test_strategy/test_etf_regime.py`, `tests/test_config/test_settings.py`, `tests/test_core/test_etf_workflow.py`

**Interfaces:**
- Produces: `Settings.etf_signal_band_percent: float`, `Settings.etf_flip_enabled: bool`, `Settings.rule_auto_apply_enabled: bool`
- Produces: `etf_regime.pick_etf(signal: Optional[float], day_return: Optional[float] = None, band_percent: float = 0.0, flip_enabled: bool = True) -> Optional[Tuple[str, str]]`
- Produces: `DailyWorkflow(..., signal_band_percent: float = 0.0, flip_enabled: bool = True)`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_strategy/test_etf_regime.py` 끝에 추가:

```python
def test_pick_etf_rests_inside_the_band():
    assert pick_etf(0.1, band_percent=0.2) is None
    assert pick_etf(-0.19, band_percent=0.2) is None
    assert pick_etf(0.2, band_percent=0.2) == LONG_ETF       # 문턱과 같으면 산다
    assert pick_etf(-0.3, band_percent=0.2) == INVERSE_ETF


def test_pick_etf_does_not_flip_when_disabled():
    assert pick_etf(-0.6, day_return=0.3, flip_enabled=False) == INVERSE_ETF
    assert pick_etf(0.6, day_return=-0.3, flip_enabled=False) == LONG_ETF


def test_pick_etf_band_applies_before_flip():
    # 문턱 안쪽이면 오늘 움직임이 커도 쉰다
    assert pick_etf(0.1, day_return=-1.0, band_percent=0.2) is None
```

`tests/test_config/test_settings.py` 끝에 추가 (파일 맨 위의 기존 import·fixture 패턴을 따른다 — 다른 기본값 테스트가 `monkeypatch.delenv`를 쓰는 방식 그대로):

```python
def test_rule_tuning_settings_default_to_current_behavior(monkeypatch):
    for key in ("ETF_SIGNAL_BAND_PERCENT", "ETF_FLIP_ENABLED", "RULE_AUTO_APPLY_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.etf_signal_band_percent == 0.0
    assert settings.etf_flip_enabled is True
    assert settings.rule_auto_apply_enabled is True


def test_rule_tuning_settings_read_env(monkeypatch):
    monkeypatch.setenv("ETF_SIGNAL_BAND_PERCENT", "0.2")
    monkeypatch.setenv("ETF_FLIP_ENABLED", "0")
    monkeypatch.setenv("RULE_AUTO_APPLY_ENABLED", "0")
    settings = Settings()
    assert settings.etf_signal_band_percent == 0.2
    assert settings.etf_flip_enabled is False
    assert settings.rule_auto_apply_enabled is False
```

`tests/test_core/test_etf_workflow.py`: `make_workflow`에 `band=0.0, flip=True` 키워드를 추가해 `DailyWorkflow(..., signal_band_percent=band, flip_enabled=flip)`로 넘기고, 끝에 추가:

```python
def test_weak_signal_rests_inside_the_band_and_says_why():
    t = make_workflow(candles(0.1), band=0.2)

    t.workflow.recommend_and_notify(today=TODAY)

    assert t.strategy._recommendations == []
    assert "쉽니다" in t.email.sent[0][0]
    assert "문턱" in t.email.sent[0][1]


def test_flip_can_be_disabled():
    rows = candles(-0.5)
    rows[0]["close_pric"] = "10050"          # 오늘 시가 대비 +0.5% — 뒤집기 조건
    t = make_workflow(rows, flip=False)

    t.workflow.recommend_and_notify(today=TODAY)

    assert [r.ticker for r in t.strategy._recommendations] == [INVERSE_ETF[0]]
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_strategy/test_etf_regime.py tests/test_config/test_settings.py tests/test_core/test_etf_workflow.py -q`
Expected: FAIL (`pick_etf() got an unexpected keyword argument 'band_percent'`, `'Settings' object has no attribute 'etf_signal_band_percent'`, `make_workflow() got an unexpected keyword argument 'band'`)

- [ ] **Step 3: 구현**

`config/settings.py` — `etf_start_date` 필드 아래에:

```python
    # 2호 신호 문턱(%) — |20일 신호|가 이 값 미만이면 그날 쉰다. 0이면 매일 산다(2026-10-29 현행).
    # 금요일 규칙 자동 평가가 바꾸는 손잡이다 (docs/superpowers/specs/2026-10-29-rule-auto-tuning-design.md).
    etf_signal_band_percent: float = field(
        default_factory=lambda: float(os.getenv("ETF_SIGNAL_BAND_PERCENT", "0"))
    )
    # 오늘 장중 움직임으로 방향을 뒤집을지 (`etf_regime.FLIP_PERCENT`, 1=켬). 규칙 자동 평가의 손잡이.
    etf_flip_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("ETF_FLIP_ENABLED"), True)
    )
    # 규칙 자동 평가가 조건을 모두 만족했을 때 `.env`를 실제로 바꿀지 (1=켬, 0이면 평가·메일만)
    rule_auto_apply_enabled: bool = field(
        default_factory=lambda: parse_flag(os.getenv("RULE_AUTO_APPLY_ENABLED"), True)
    )
```

`src/strategy/etf_regime.py` — `pick_etf` 전체를 교체:

```python
def pick_etf(
    signal: Optional[float],
    day_return: Optional[float] = None,
    band_percent: float = 0.0,
    flip_enabled: bool = True,
) -> Optional[Tuple[str, str]]:
    """신호로 오늘 살 ETF를 정한다. 신호가 없거나 |신호|가 `band_percent` 미만이면 None(쉼).

    신호 0 이상이면 KODEX 200, 음수이면 KODEX 인버스. `flip_enabled`이고 `day_return`(오늘
    KODEX 200 시가 대비 %)이 신호와 반대로 `FLIP_PERCENT` 이상 움직였으면 방향을 뒤집는다.
    문턱과 뒤집기는 `.env`(`ETF_SIGNAL_BAND_PERCENT`·`ETF_FLIP_ENABLED`)로 정하고, 금요일 규칙
    자동 평가가 바꿀 수 있다 (2026-10-29). 실전과 백테스트(`src/research/backtest.py`)가 이 함수를 함께 쓴다.
    """
    if signal is None or abs(signal) < band_percent:
        return None
    long = signal >= 0
    if flip_enabled and day_return is not None:
        if long and day_return <= -FLIP_PERCENT:
            long = False
        elif not long and day_return >= FLIP_PERCENT:
            long = True
    return LONG_ETF if long else INVERSE_ETF
```

`src/core/daily_workflow.py`:
1. 생성자 시그니처에 `signal_band_percent: float = 0.0, flip_enabled: bool = True,`를 `etf_start_date` 인자 근처에 추가하고 본문에 `self.signal_band_percent = signal_band_percent`, `self.flip_enabled = flip_enabled`.
2. `_recommend_etf`의 결정 부분을 다음으로 교체:

```python
        pick = etf_regime.pick_etf(signal, day_return, self.signal_band_percent, self.flip_enabled)
        # 신호만으로 정한 방향과 다르면 오늘 장중 흐름 때문에 뒤집힌 것이다
        flipped = pick is not None and pick != etf_regime.pick_etf(signal, None, self.signal_band_percent)
        if pick is None:
            if signal is None:
                logger.info("2호 전략: 오늘은 쉽니다 (신호 계산 불가 — 일봉 부족)")
            else:
                logger.info(
                    "2호 전략: 오늘은 쉽니다 (신호 %+.3f%% — 문턱 ±%g%% 안쪽)", signal, self.signal_band_percent
                )
```
(그 아래 `self.strategy.set_recommendations([])` 등 기존 줄은 그대로.)
3. `_send_etf_mail`의 쉼 분기 본문을 교체:

```python
        elif recommendation is None:
            subject = f"[2호 전략] {today} 오늘은 쉽니다"
            reason = (
                "일봉이 모자라 신호를 계산하지 못해"
                if signal is None
                else f"신호가 문턱 ±{self.signal_band_percent:g}% 안쪽이라"
            )
            body = (
                f"직전 {etf_regime.LOOKBACK_DAYS}거래일 장중 평균 {signal_text} — "
                f"{reason} 매수하지 않습니다."
            )
```

`src/core/runtime.py` — `DailyWorkflow(...)` 호출에 두 인자 추가:

```python
        signal_band_percent=settings.etf_signal_band_percent,
        flip_enabled=settings.etf_flip_enabled,
```

`.env.example` — `ETF_KILL_LOSS_PERCENT` 줄 아래에:

```
# 2호 신호 문턱(%) — |20일 신호|가 이 값 미만이면 쉰다 (0=매일 매수). 금요일 규칙 자동 평가가 바꿀 수 있다
ETF_SIGNAL_BAND_PERCENT=0
# 오늘 장중 움직임으로 방향을 뒤집을지 (1=켬). 금요일 규칙 자동 평가가 바꿀 수 있다
ETF_FLIP_ENABLED=1
# 금요일 규칙 자동 평가 결과를 .env에 자동 적용할지 (1=켬, 0=평가·메일만)
RULE_AUTO_APPLY_ENABLED=1
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: 전체 PASS (기존 `pick_etf` 테스트도 기본 인자로 그대로 통과)

- [ ] **Step 5: Commit**

```bash
git add config/settings.py src/strategy/etf_regime.py src/core/daily_workflow.py src/core/runtime.py .env.example tests/test_strategy/test_etf_regime.py tests/test_config/test_settings.py tests/test_core/test_etf_workflow.py
git commit -m "Make the ETF signal band and flip rule .env settings"
```

---

### Task 2: 1분봉 저장소와 수집 API

**Files:**
- Create: `src/research/__init__.py` (빈 파일), `src/research/minute_store.py`
- Modify: `src/api/market_data.py` (상수는 ~line 83-90 근처, 메서드는 `get_ohlcv` 아래)
- Test: `tests/test_research/__init__.py` (빈 파일), `tests/test_research/test_minute_store.py`, `tests/test_api/test_market_data.py`

**Interfaces:**
- Produces: `minute_store.Bar = Tuple[str, float]` (HHMM, 종가), `minute_store.DayBars = Dict[str, List[Bar]]` (YYYYMMDD → 시각순)
- Produces: `MinuteStore(root: Path = DEFAULT_MINUTE_DIR)`, `.load(ticker) -> DayBars`, `.last_day(ticker) -> Optional[str]`, `.merge(ticker, bars: DayBars) -> int`
- Produces: `MarketDataClient.get_minute_candles(ticker: str, since: Optional[str] = None) -> DayBars`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_research/test_minute_store.py`:

```python
from src.research.minute_store import MinuteStore


def test_empty_store_has_nothing(tmp_path):
    store = MinuteStore(tmp_path)
    assert store.load("069500") == {}
    assert store.last_day("069500") is None


def test_merge_adds_and_overwrites_by_day(tmp_path):
    store = MinuteStore(tmp_path)
    store.merge("069500", {"20261028": [("0900", 100.0)], "20261029": [("0900", 101.0)]})
    store.merge("069500", {"20261029": [("0900", 102.0), ("0901", 103.0)]})

    bars = store.load("069500")
    assert bars["20261028"] == [("0900", 100.0)]
    assert bars["20261029"] == [("0900", 102.0), ("0901", 103.0)]
    assert store.last_day("069500") == "20261029"


def test_tickers_are_separate_files(tmp_path):
    store = MinuteStore(tmp_path)
    store.merge("069500", {"20261029": [("0900", 1.0)]})
    assert store.load("114800") == {}


def test_broken_file_reads_as_empty(tmp_path):
    (tmp_path / "069500.json").write_text("{broken", encoding="utf-8")
    assert MinuteStore(tmp_path).load("069500") == {}
```

`tests/test_api/test_market_data.py` 끝에 추가:

```python
def test_get_minute_candles_pages_until_since(monkeypatch):
    from src.api import market_data as module

    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    pages = [
        ({"stk_min_pole_chart_qry": [
            {"cntr_tm": "20261029090100", "cur_prc": "+101"},
            {"cntr_tm": "20261029090000", "cur_prc": "-100"},
        ]}, {"cont-yn": "Y", "next-key": "k1"}),
        ({"stk_min_pole_chart_qry": [
            {"cntr_tm": "20261028153000", "cur_prc": "99"},
            {"cntr_tm": "20261027153000", "cur_prc": "98"},
        ]}, {"cont-yn": "Y", "next-key": "k2"}),
    ]
    calls = []

    def request(path, api_id, params, cont_yn="N", next_key=""):
        calls.append((api_id, params, cont_yn, next_key))
        return pages[len(calls) - 1]

    client = MarketDataClient.__new__(MarketDataClient)
    client._client = SimpleNamespace(request=request)

    bars = client.get_minute_candles("069500", since="20261028")

    assert bars == {"20261029": [("0900", 100.0), ("0901", 101.0)], "20261028": [("1530", 99.0)]}
    assert len(calls) == 2                       # 27일이 나와 멈췄다
    assert calls[0] == ("ka10080", {"stk_cd": "069500", "tic_scope": "1", "upd_stkpc_tp": "1"}, "N", "")
    assert calls[1][2:] == ("Y", "k1")


def test_get_minute_candles_stops_when_no_more_pages(monkeypatch):
    from src.api import market_data as module

    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    client = MarketDataClient.__new__(MarketDataClient)
    client._client = SimpleNamespace(request=lambda *a, **kw: (
        {"stk_min_pole_chart_qry": [{"cntr_tm": "20261029090000", "cur_prc": "100"}]}, {"cont-yn": "N"}
    ))

    assert client.get_minute_candles("069500") == {"20261029": [("0900", 100.0)]}
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research tests/test_api/test_market_data.py -q`
Expected: FAIL (`No module named 'src.research'`, `has no attribute 'get_minute_candles'`)

- [ ] **Step 3: 구현**

`src/research/minute_store.py`:

```python
"""2호 전략 규칙 자동 평가용 1분봉 저장소 (스펙 2026-10-29 5.1).

종목별 JSON 파일 하나에 {YYYYMMDD: [[HHMM, 종가], ...]}로 쌓는다. 같은 날짜는 덮어쓴다 — 장중에
받은 반쪽 날도 다음 수집이 통째로 바꾼다. `data/`는 gitignore 대상이다. API를 모르는 파일 입출력만 맡는다.
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

Bar = Tuple[str, float]           # (HHMM, 종가)
DayBars = Dict[str, List[Bar]]    # YYYYMMDD → 시각순 봉

DEFAULT_MINUTE_DIR = Path("data") / "minute"


class MinuteStore:
    def __init__(self, root: Path = DEFAULT_MINUTE_DIR):
        self.root = Path(root)

    def _path(self, ticker: str) -> Path:
        return self.root / f"{ticker}.json"

    def load(self, ticker: str) -> DayBars:
        """저장된 봉. 파일이 없거나 깨졌으면 빈 dict — 다음 수집이 처음부터 다시 채운다."""
        try:
            raw = json.loads(self._path(ticker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {day: [(str(t), float(p)) for t, p in bars] for day, bars in raw.items()}

    def last_day(self, ticker: str) -> Optional[str]:
        days = self.load(ticker)
        return max(days) if days else None

    def merge(self, ticker: str, bars: DayBars) -> int:
        """`bars`의 날짜를 덮어써 저장하고 저장한 날짜 수를 돌려준다. 임시 파일 → os.replace로 원자적으로 쓴다."""
        current = self.load(ticker)
        current.update(bars)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(ticker)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(current), encoding="utf-8")
        os.replace(temp, path)
        return len(bars)
```

`src/api/market_data.py`:
1. 맨 위 import에 `import time`과 `from typing import ... Tuple`(없으면 추가).
2. 상수 블록(`INTRADAY_INVESTOR_API_ID` 근처)에:

```python
MINUTE_CHART_API_ID = "ka10080"  # 주식분봉차트조회요청 — 규칙 자동 평가용 1분봉
# 연속조회 사이 쉬는 시간과 안전 상한. 한 페이지가 약 900봉(2.4거래일)이라 400페이지면 약 13개월이다.
MINUTE_PAGE_PAUSE_SECONDS = 0.4
MINUTE_MAX_PAGES = 400
```

3. `get_ohlcv` 아래에 메서드:

```python
    def get_minute_candles(
        self, ticker: str, since: Optional[str] = None
    ) -> Dict[str, List[Tuple[str, float]]]:
        """1분봉을 최신부터 거꾸로 받아 {YYYYMMDD: [(HHMM, 종가), ...]}로 돌려준다 (스펙 2026-10-29 5.1).

        `since`(YYYYMMDD)보다 이전 날짜가 나오면 멈추고 `since` 이후 날짜만 담는다. None이면
        키움이 주는 만큼(약 13개월) 받는다. 2026-10-29 세션에서 확인한 요청 모양이다.
        """
        days: Dict[str, Dict[str, float]] = {}
        cont_yn, next_key = "N", ""
        for _ in range(MINUTE_MAX_PAGES):
            data, headers = self._client.request(
                CHART_PATH,
                MINUTE_CHART_API_ID,
                {"stk_cd": ticker, "tic_scope": "1", "upd_stkpc_tp": "1"},
                cont_yn=cont_yn,
                next_key=next_key,
            )
            rows = data.get("stk_min_pole_chart_qry") or []
            reached = False
            for row in rows:
                stamp = str(row.get("cntr_tm", "")).strip()
                if len(stamp) < 12:
                    continue
                day, hhmm = stamp[:8], stamp[8:12]
                if since is not None and day < since:
                    reached = True
                    continue
                # 키움은 가격에 등락 방향 부호를 붙여 보낸다
                price = abs(to_float(row.get("cur_prc")))
                if price > 0:
                    days.setdefault(day, {})[hhmm] = price
            cont_yn = headers.get("cont-yn", "N")
            next_key = headers.get("next-key", "")
            if reached or cont_yn != "Y" or not rows:
                break
            time.sleep(MINUTE_PAGE_PAUSE_SECONDS)
        return {day: sorted(bars.items()) for day, bars in days.items()}
```

`tests/test_research/__init__.py` 빈 파일도 만든다.

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research tests/test_api/test_market_data.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research/__init__.py src/research/minute_store.py src/api/market_data.py tests/test_research/__init__.py tests/test_research/test_minute_store.py tests/test_api/test_market_data.py
git commit -m "Add a 1-minute bar store and ka10080 paging fetch"
```

---

### Task 3: 백테스트

**Files:**
- Create: `src/research/backtest.py`
- Test: `tests/test_research/test_backtest.py`

**Interfaces:**
- Consumes: `minute_store.Bar`, `minute_store.DayBars`, `etf_regime.pick_etf(signal, day_return, band_percent, flip_enabled)`, `etf_regime.LONG_ETF`, `etf_regime.LOOKBACK_DAYS` (=20)
- Produces: `Rule(band_percent: float, flip_enabled: bool, decision_hhmm: str, stop_percent: float)` (frozen dataclass, `decision_hhmm`은 `"HH:MM"`), `Rule.to_json() -> str`, `Rule.from_json(text) -> Rule`
- Produces: `trade_return(bars: List[Bar], entry_hhmm: str, stop_percent: float, commission_ratio: float, half_tick: float = HALF_TICK_WON) -> Optional[float]` (`entry_hhmm`은 `"HHMM"`, 결과는 %)
- Produces: `Backtester(long_bars: DayBars, inverse_bars: DayBars, commission_ratio: float)`, `.days: List[str]`, `.day_return(day, rule) -> float`, `.returns(rule, days: Sequence[str]) -> List[float]`
- Produces: 상수 `MIN_BARS_PER_DAY = 300`, `CLOSE_OUT_HHMM = "1515"`, `HALF_TICK_WON = 2.5`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_research/test_backtest.py`:

```python
import pytest

from src.research.backtest import Backtester, Rule, trade_return

COMMISSION = 0.00015


def minutes(start="0900", end="1530"):
    out = []
    h, m = int(start[:2]), int(start[2:])
    while f"{h:02d}{m:02d}" <= end:
        out.append(f"{h:02d}{m:02d}")
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def flat_day(price, overrides=None):
    """하루 391봉. `overrides`={HHMM: 가격}이면 그 시각부터 그 가격이 이어진다."""
    overrides = overrides or {}
    bars, current = [], price
    for t in minutes():
        current = overrides.get(t, current)
        bars.append((t, float(current)))
    return bars


def test_trade_return_holds_to_1515_with_costs():
    bars = flat_day(10000, {"0910": 10000, "1000": 10100})
    result = trade_return(bars, "0910", 2.0, COMMISSION, half_tick=2.5)
    expected = ((10100 - 2.5) / (10000 + 2.5) - 1 - 2 * COMMISSION) * 100
    assert result == pytest.approx(expected)


def test_trade_return_stops_at_the_first_bar_past_the_stop():
    bars = flat_day(10000, {"1000": 9900, "1001": 9750, "1002": 9000})
    result = trade_return(bars, "0910", 2.0, COMMISSION, half_tick=0.0)
    assert result == pytest.approx((9750 / 10000 - 1 - 2 * COMMISSION) * 100)   # 1001봉에서 -2.5%로 손절


def test_trade_return_without_an_entry_bar_is_none():
    assert trade_return([("0900", 1.0)], "0910", 2.0, COMMISSION) is None


def test_rule_json_round_trip():
    rule = Rule(0.2, False, "09:10", 2.0)
    assert Rule.from_json(rule.to_json()) == rule


def make_history(signal_up: bool, days=22):
    """신호 계산용 20일 + 거래일 2일. 지난날은 KODEX 200이 장중 +0.5%(또는 -0.5%) 움직였다."""
    long_bars, inverse_bars = {}, {}
    move = 10050 if signal_up else 9950
    for i in range(days):
        day = f"202610{i + 1:02d}"
        long_bars[day] = flat_day(10000, {"1200": move})
        inverse_bars[day] = flat_day(1000)
    return long_bars, inverse_bars


def test_backtester_days_start_after_the_lookback():
    long_bars, inverse_bars = make_history(True)
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    assert bt.days == ["20261021", "20261022"]


def test_backtester_buys_kodex200_on_an_up_signal():
    long_bars, inverse_bars = make_history(True)
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    result = bt.day_return("20261021", Rule(0.0, True, "09:10", 2.0))
    # KODEX 200이 12:00에 +0.5%로 올라 15:15까지 유지
    assert result == pytest.approx(((10050 - 2.5) / (10000 + 2.5) - 1 - 2 * COMMISSION) * 100)


def test_backtester_rests_inside_the_band_as_zero():
    long_bars, inverse_bars = make_history(True)
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    assert bt.day_return("20261021", Rule(1.0, True, "09:10", 2.0)) == 0.0


def test_backtester_flips_on_the_morning_move():
    long_bars, inverse_bars = make_history(True)
    long_bars["20261021"] = flat_day(10000, {"0905": 9970})       # 결정 전 -0.3%
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    flipped = bt.day_return("20261021", Rule(0.0, True, "09:10", 2.0))
    kept = bt.day_return("20261021", Rule(0.0, False, "09:10", 2.0))
    inverse_cost = ((1000 - 2.5) / (1000 + 2.5) - 1 - 2 * COMMISSION) * 100   # 인버스 가격 변화 없음
    assert flipped == pytest.approx(inverse_cost)
    assert kept != pytest.approx(inverse_cost)


def test_backtester_skips_thin_days():
    long_bars, inverse_bars = make_history(True)
    long_bars["20261022"] = long_bars["20261022"][:100]
    bt = Backtester(long_bars, inverse_bars, COMMISSION)
    assert "20261022" not in bt.days
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research/test_backtest.py -q`
Expected: FAIL (`No module named 'src.research.backtest'`)

- [ ] **Step 3: 구현**

`src/research/backtest.py`:

```python
"""2호 전략 규칙 백테스트 — 1분봉으로 규칙 하나의 거래일별 순수익률을 낸다 (스펙 2026-10-29 5.2).

실전과 어긋나지 않도록 방향은 실전 함수(`etf_regime.pick_etf`)로 정한다. 신호는 직전 20거래일
KODEX 200의 장중(첫 봉 → 마지막 봉) 수익률 평균이다 — 실전은 일봉 시가·종가를 쓰므로 첫 봉 종가만큼
차이가 난다. AI 매도 판단은 재현하지 않는다: 청산은 손절과 15:15뿐이다.
"""
import json
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Dict, List, Optional, Sequence, Tuple

from src.research.minute_store import Bar, DayBars
from src.strategy.etf_regime import LONG_ETF, LOOKBACK_DAYS, pick_etf

# 반일장·수집 누락일을 거르는 기준 — 정규장 하루는 약 390봉이다
MIN_BARS_PER_DAY = 300
CLOSE_OUT_HHMM = "1515"
# 시장가 체결이 호가 한 칸을 건너간다고 보고 체결마다 반칸(ETF 호가단위 5원)을 잃는다
HALF_TICK_WON = 2.5


@dataclass(frozen=True)
class Rule:
    band_percent: float
    flip_enabled: bool
    decision_hhmm: str      # "HH:MM" — `.env`의 RECOMMEND_TIME 꼴
    stop_percent: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "Rule":
        return Rule(**json.loads(text))


def usable_days(long_bars: DayBars, inverse_bars: DayBars) -> List[str]:
    """두 종목 모두 봉이 충분한 날 (오름차순)."""
    return sorted(
        day
        for day in long_bars
        if day in inverse_bars
        and len(long_bars[day]) >= MIN_BARS_PER_DAY
        and len(inverse_bars[day]) >= MIN_BARS_PER_DAY
    )


def intraday_return(bars: List[Bar]) -> float:
    return (bars[-1][1] / bars[0][1] - 1) * 100


def trade_return(
    bars: List[Bar],
    entry_hhmm: str,
    stop_percent: float,
    commission_ratio: float,
    half_tick: float = HALF_TICK_WON,
) -> Optional[float]:
    """`entry_hhmm`(HHMM) 이후 첫 봉에 사서, 순손익률이 −손절 이하가 된 첫 봉이나 15:15 이후 첫 봉에 판다.

    순손익률(%) = (매도가 − 반호가) / (매수가 + 반호가) − 1 − 수수료 양방향. 진입할 봉이 없으면 None.
    """
    entry_index = next((i for i, (t, _) in enumerate(bars) if t >= entry_hhmm), None)
    if entry_index is None:
        return None
    entry = bars[entry_index][1] + half_tick
    net = None
    for t, price in bars[entry_index + 1:]:
        net = ((price - half_tick) / entry - 1 - 2 * commission_ratio) * 100
        if net <= -stop_percent or t >= CLOSE_OUT_HHMM:
            return net
    return net


class Backtester:
    """분봉 한 벌로 여러 규칙을 평가한다. 같은 (날짜, 종목, 진입 시각, 손절) 매매는 한 번만 계산한다."""

    def __init__(self, long_bars: DayBars, inverse_bars: DayBars, commission_ratio: float):
        self._long = long_bars
        self._inverse = inverse_bars
        self._commission = commission_ratio
        all_days = usable_days(long_bars, inverse_bars)
        # 신호에 직전 20거래일이 필요하므로 그 뒤부터가 평가할 수 있는 날이다
        self.days: List[str] = all_days[LOOKBACK_DAYS:]
        self._signals: Dict[str, float] = {
            day: mean(intraday_return(long_bars[x]) for x in all_days[i - LOOKBACK_DAYS:i])
            for i, day in enumerate(all_days)
            if i >= LOOKBACK_DAYS
        }
        self._trades: Dict[Tuple[str, str, str, float], Optional[float]] = {}

    def _trade(self, day: str, ticker: str, hhmm: str, stop_percent: float) -> Optional[float]:
        key = (day, ticker, hhmm, stop_percent)
        if key not in self._trades:
            bars = self._long[day] if ticker == LONG_ETF[0] else self._inverse[day]
            self._trades[key] = trade_return(bars, hhmm, stop_percent, self._commission)
        return self._trades[key]

    def day_return(self, day: str, rule: Rule) -> float:
        """그날 규칙대로 매매한 순수익률(%). 쉬거나 진입할 봉이 없으면 0."""
        hhmm = rule.decision_hhmm.replace(":", "")
        bars = self._long[day]
        entry_price = next((price for t, price in bars if t >= hhmm), None)
        if entry_price is None:
            return 0.0
        morning = (entry_price / bars[0][1] - 1) * 100
        pick = pick_etf(self._signals[day], morning, rule.band_percent, rule.flip_enabled)
        if pick is None:
            return 0.0
        result = self._trade(day, pick[0], hhmm, rule.stop_percent)
        return 0.0 if result is None else result

    def returns(self, rule: Rule, days: Sequence[str]) -> List[float]:
        return [self.day_return(day, rule) for day in days]
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research/test_backtest.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research/backtest.py tests/test_research/test_backtest.py
git commit -m "Add a 1-minute backtester for ETF strategy rules"
```

---

### Task 4: 후보 탐색과 적용 조건 판정

**Files:**
- Create: `src/research/rule_search.py`
- Test: `tests/test_research/test_rule_search.py`

**Interfaces:**
- Consumes: `Rule`, `Backtester.days`, `Backtester.returns(rule, days)`
- Produces: 상수 `SIGNAL_BANDS`, `FLIP_OPTIONS`, `DECISION_TIMES`, `STOP_PERCENTS`, `TEST_DAYS = 63`, `MIN_SELECT_DAYS = 57`, `MIN_EDGE_PERCENT = 0.05`, `STREAK_EVALUATIONS = 4`, `COOLDOWN_EVALUATIONS = 4`, `STATUS_EVALUATED = "evaluated"`, `STATUS_INSUFFICIENT = "insufficient_data"`, `STATUS_FAILED = "failed"`
- Produces: `candidates() -> List[Rule]`, `in_range(rule) -> bool`, `distance(a, b) -> int`
- Produces: `Evaluation(current, best, select_current, select_best, test_current, test_best)` frozen dataclass + 프로퍼티 `beats: bool`, `positive: bool`
- Produces: `evaluate(backtester, current: Rule) -> Optional[Evaluation]`
- Produces: `streak_count(rows) -> int`, `cooldown_left(rows) -> int` — `rows`는 오래된 것부터, 각 행에 `.status`, `.best_rule`(JSON 문자열), `.beats`, `.positive`, `.applied` 속성

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_research/test_rule_search.py`:

```python
from types import SimpleNamespace

import pytest

from src.research import rule_search
from src.research.backtest import Rule
from src.research.rule_search import (
    STATUS_EVALUATED,
    STATUS_FAILED,
    candidates,
    cooldown_left,
    distance,
    evaluate,
    in_range,
    streak_count,
)

CURRENT = Rule(0.0, True, "09:10", 2.0)


def test_there_are_160_distinct_candidates():
    rules = candidates()
    assert len(rules) == 160 and len(set(rules)) == 160
    assert all(in_range(r) for r in rules)


def test_in_range_rejects_off_grid_values():
    assert not in_range(Rule(0.15, True, "09:10", 2.0))
    assert not in_range(Rule(0.2, True, "09:11", 2.0))


def test_distance_counts_changed_knobs():
    assert distance(CURRENT, CURRENT) == 0
    assert distance(CURRENT, Rule(0.2, False, "09:10", 2.0)) == 2


class FakeBacktester:
    """규칙마다 고르기·시험 구간 일 수익률을 정해 둔 가짜."""

    def __init__(self, n_days, scores):
        self.days = [f"d{i:03d}" for i in range(n_days)]
        self.scores = scores              # Rule -> (고르기 구간 값, 시험 구간 값)
        self.seen_days = []

    def returns(self, rule, days):
        self.seen_days.append(tuple(days))
        select, test = self.scores.get(rule, (-1.0, -1.0))
        test_days = set(self.days[-rule_search.TEST_DAYS:])
        return [test if d in test_days else select for d in days]


def test_evaluate_needs_enough_days():
    assert evaluate(FakeBacktester(119, {}), CURRENT) is None


def test_evaluate_picks_on_the_select_window_only():
    best = Rule(0.2, False, "09:10", 2.0)
    tempting = Rule(0.3, False, "10:00", 1.0)       # 시험 구간에서만 좋다 — 골라지면 안 된다
    bt = FakeBacktester(140, {CURRENT: (0.0, 0.01), best: (0.3, 0.1), tempting: (0.1, 5.0)})

    ev = evaluate(bt, CURRENT)

    assert ev.best == best
    assert ev.select_best == pytest.approx(0.3)
    assert ev.test_best == pytest.approx(0.1) and ev.test_current == pytest.approx(0.01)
    assert ev.beats and ev.positive


def test_evaluate_breaks_ties_toward_the_current_rule():
    near = Rule(0.0, True, "09:10", 2.5)
    far = Rule(0.3, False, "10:00", 1.0)
    bt = FakeBacktester(140, {near: (0.2, 0.1), far: (0.2, 0.1)})
    assert evaluate(bt, CURRENT).best == near


def test_beats_needs_the_margin():
    best = Rule(0.2, False, "09:10", 2.0)
    bt = FakeBacktester(140, {CURRENT: (0.0, 0.10), best: (0.3, 0.14)})
    ev = evaluate(bt, CURRENT)
    assert not ev.beats and ev.positive


def row(best="A", status=STATUS_EVALUATED, beats=True, positive=True, applied=False):
    return SimpleNamespace(status=status, best_rule=best, beats=beats, positive=positive, applied=applied)


def test_streak_counts_trailing_same_best_rows():
    assert streak_count([row("B"), row("A"), row("A"), row("A")]) == 3
    assert streak_count([row("A")] * 4) == 4
    assert streak_count([row("A"), row("A", beats=False)]) == 0
    assert streak_count([row("A"), row("A", status=STATUS_FAILED), row("A")]) == 1


def test_cooldown_after_an_applied_row():
    assert cooldown_left([row(), row()]) == 0
    assert cooldown_left([row(applied=True), row(), row()]) == 2
    assert cooldown_left([row(applied=True)] + [row()] * 4) == 0
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research/test_rule_search.py -q`
Expected: FAIL (`No module named 'src.research.rule_search'`)

- [ ] **Step 3: 구현**

`src/research/rule_search.py`:

```python
"""2호 전략 규칙 후보 탐색과 자동 적용 조건 판정 (스펙 2026-10-29 3·4절).

순수 함수만 둔다 — DB·메일·`.env`는 `rule_tuning`이 맡는다.
1등은 고르기 구간(최근 63거래일을 뺀 앞 기간)에서만 고르고, 시험 구간(최근 63거래일)은 현행과의
비교에만 쓴다. 시험 구간이 고르기에 새면 우연히 좋은 후보를 고르게 된다.
"""
import itertools
from dataclasses import dataclass
from statistics import mean
from typing import List, Optional, Sequence

from src.research.backtest import Rule

SIGNAL_BANDS = (0.0, 0.1, 0.2, 0.3)
FLIP_OPTIONS = (True, False)
DECISION_TIMES = ("09:05", "09:10", "09:30", "10:00")
STOP_PERCENTS = (1.0, 1.5, 2.0, 2.5, 3.0)

TEST_DAYS = 63
MIN_SELECT_DAYS = 57
# 시험 구간에서 1등이 현행보다 이만큼(%p/일) 나아야 한다
MIN_EDGE_PERCENT = 0.05
# 같은 1등이 이만큼 연속돼야 적용하고, 적용한 뒤 이만큼 평가가 지나야 다시 적용한다
STREAK_EVALUATIONS = 4
COOLDOWN_EVALUATIONS = 4

STATUS_EVALUATED = "evaluated"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_FAILED = "failed"

_TIE_TOLERANCE = 1e-12


def candidates() -> List[Rule]:
    return [
        Rule(band, flip, hhmm, stop)
        for band, flip, hhmm, stop in itertools.product(
            SIGNAL_BANDS, FLIP_OPTIONS, DECISION_TIMES, STOP_PERCENTS
        )
    ]


def in_range(rule: Rule) -> bool:
    return (
        rule.band_percent in SIGNAL_BANDS
        and rule.flip_enabled in FLIP_OPTIONS
        and rule.decision_hhmm in DECISION_TIMES
        and rule.stop_percent in STOP_PERCENTS
    )


def distance(a: Rule, b: Rule) -> int:
    """다른 손잡이 수 — 동점일 때 현행에 가까운 후보를 고르는 데 쓴다."""
    return sum(
        (a.band_percent != b.band_percent, a.flip_enabled != b.flip_enabled,
         a.decision_hhmm != b.decision_hhmm, a.stop_percent != b.stop_percent)
    )


@dataclass(frozen=True)
class Evaluation:
    current: Rule
    best: Rule
    select_current: float
    select_best: float
    test_current: float
    test_best: float

    @property
    def beats(self) -> bool:
        return self.test_best - self.test_current >= MIN_EDGE_PERCENT - _TIE_TOLERANCE

    @property
    def positive(self) -> bool:
        return self.test_best > 0


def evaluate(backtester, current: Rule) -> Optional[Evaluation]:
    """고르기 구간 1등과 현행을 시험 구간에서 비교한다. 거래일이 모자라면 None.

    일 평균은 쉬는 날을 0%로 넣어 전체 거래일로 나눈다 (`Backtester.day_return`이 쉬면 0을 준다).
    """
    days = backtester.days
    if len(days) < TEST_DAYS + MIN_SELECT_DAYS:
        return None
    select, test = days[:-TEST_DAYS], days[-TEST_DAYS:]

    def average(rule: Rule, window: Sequence[str]) -> float:
        return mean(backtester.returns(rule, window))

    scored = [(average(rule, select), rule) for rule in candidates()]
    top = max(score for score, _ in scored)
    best = min(
        (rule for score, rule in scored if score >= top - _TIE_TOLERANCE),
        key=lambda rule: distance(rule, current),
    )
    return Evaluation(
        current=current,
        best=best,
        select_current=average(current, select),
        select_best=top,
        test_current=average(current, test),
        test_best=average(best, test),
    )


def streak_count(rows) -> int:
    """끝에서부터 '평가함 + 조건 1·2 충족 + 같은 1등'이 몇 번 이어졌는지."""
    count, target = 0, None
    for row in reversed(list(rows)):
        if not (row.status == STATUS_EVALUATED and row.beats and row.positive):
            break
        if target is None:
            target = row.best_rule
        elif row.best_rule != target:
            break
        count += 1
    return count


def cooldown_left(rows) -> int:
    """마지막 적용 뒤 다시 적용할 수 있을 때까지 남은 평가 횟수 (0이면 가능)."""
    rows = list(rows)
    applied = [i for i, row in enumerate(rows) if row.applied]
    if not applied:
        return 0
    since = len(rows) - 1 - applied[-1]
    return max(0, COOLDOWN_EVALUATIONS - since)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research/test_rule_search.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research/rule_search.py tests/test_research/test_rule_search.py
git commit -m "Add rule candidate search and auto-apply condition checks"
```

---

### Task 5: 평가 기록 테이블과 메일

**Files:**
- Modify: `src/logger/trade_store.py` (스키마 상수는 `EXIT_REVIEW_SCHEMA_SQL` 아래, `__init__`의 `conn.execute(EXIT_REVIEW_SCHEMA_SQL)` 아래, dataclass는 `ExitReviewRow` 근처, 메서드는 `save_exit_review` 근처)
- Modify: `src/notification/templates.py` (파일 끝)
- Test: `tests/test_logger/test_rule_evaluations.py`, `tests/test_notification/test_templates.py`

**Interfaces:**
- Consumes: `rule_search` 상수(`STATUS_*`, `MIN_EDGE_PERCENT`, `STREAK_EVALUATIONS`, `TEST_DAYS`), `Rule.from_json`
- Produces: `RuleEvaluationRow(day: date, status: str, current_rule: str = "", best_rule: str = "", select_current: Optional[float] = None, select_best: Optional[float] = None, test_current: Optional[float] = None, test_best: Optional[float] = None, beats: bool = False, positive: bool = False, applied: bool = False, note: str = "")`
- Produces: `TradeStore.save_rule_evaluation(row) -> None` (같은 날은 덮어씀), `TradeStore.rule_evaluations() -> List[RuleEvaluationRow]` (오래된 것부터)
- Produces: `templates.rule_evaluation_email(row: RuleEvaluationRow, streak: int, cooldown: int, applied_values: Optional[Dict[str, Tuple[str, str]]] = None) -> tuple[str, str]`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_logger/test_rule_evaluations.py`:

```python
from datetime import date

from src.logger.trade_store import RuleEvaluationRow, TradeStore


def test_rule_evaluations_round_trip_in_day_order(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_rule_evaluation(RuleEvaluationRow(day=date(2026, 11, 6), status="evaluated",
                                                 current_rule="c", best_rule="b", select_current=0.1,
                                                 select_best=0.2, test_current=0.0, test_best=0.08,
                                                 beats=True, positive=True))
    store.save_rule_evaluation(RuleEvaluationRow(day=date(2026, 10, 30), status="insufficient_data",
                                                 current_rule="c", note="부족"))

    rows = store.rule_evaluations()

    assert [r.day for r in rows] == [date(2026, 10, 30), date(2026, 11, 6)]
    assert rows[0].note == "부족" and rows[0].best_rule == ""
    assert rows[1].beats is True and rows[1].applied is False and rows[1].test_best == 0.08


def test_same_day_is_overwritten(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    day = date(2026, 11, 6)
    store.save_rule_evaluation(RuleEvaluationRow(day=day, status="evaluated", current_rule="c"))
    store.save_rule_evaluation(RuleEvaluationRow(day=day, status="evaluated", current_rule="c", applied=True))

    rows = store.rule_evaluations()
    assert len(rows) == 1 and rows[0].applied is True
```

`tests/test_notification/test_templates.py` 끝에 추가 (아래 import 두 줄도 함께 — 파일에 이미 `from src.notification import templates`와 `from datetime import date`가 있는지 확인하고 없으면 추가):

```python
from src.research.backtest import Rule as _Rule
from src.logger.trade_store import RuleEvaluationRow as _RuleEvaluationRow


def test_rule_evaluation_email_reports_conditions():
    row = _RuleEvaluationRow(
        day=date(2026, 11, 6), status="evaluated",
        current_rule=_Rule(0.0, True, "09:10", 2.0).to_json(),
        best_rule=_Rule(0.2, False, "09:10", 2.0).to_json(),
        select_current=-0.3, select_best=0.2, test_current=-0.1, test_best=0.09,
        beats=True, positive=True,
    )
    subject, body = templates.rule_evaluation_email(row, streak=2, cooldown=0)

    assert "주간 평가" in subject
    assert "문턱 0.2% / 뒤집기 끔 / 결정 09:10 / 손절 2%" in body
    assert "2/4" in body
    assert "+0.090%" in body


def test_rule_evaluation_email_for_an_applied_change():
    row = _RuleEvaluationRow(day=date(2026, 11, 27), status="evaluated",
                             current_rule=_Rule(0.0, True, "09:10", 2.0).to_json(),
                             best_rule=_Rule(0.2, False, "09:10", 2.0).to_json(),
                             select_current=0.0, select_best=0.2, test_current=0.0, test_best=0.1,
                             beats=True, positive=True, applied=True, note="자동 적용")
    subject, body = templates.rule_evaluation_email(
        row, streak=4, cooldown=0, applied_values={"ETF_FLIP_ENABLED": ("1", "0")}
    )

    assert "자동 변경" in subject
    assert "ETF_FLIP_ENABLED: 1 → 0" in body


def test_rule_evaluation_email_when_not_evaluated():
    row = _RuleEvaluationRow(day=date(2026, 10, 30), status="insufficient_data",
                             current_rule=_Rule(0.0, True, "09:10", 2.0).to_json(), note="거래일 100일 < 120일")
    subject, body = templates.rule_evaluation_email(row, streak=0, cooldown=0)
    assert "평가하지 않았습니다" in body and "거래일 100일" in body
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_logger/test_rule_evaluations.py tests/test_notification/test_templates.py -q`
Expected: FAIL (`cannot import name 'RuleEvaluationRow'`)

- [ ] **Step 3: 구현**

`src/logger/trade_store.py`:

```python
RULE_EVALUATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rule_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    current_rule TEXT NOT NULL DEFAULT '',
    best_rule TEXT NOT NULL DEFAULT '',
    select_current REAL,
    select_best REAL,
    test_current REAL,
    test_best REAL,
    beats INTEGER NOT NULL DEFAULT 0,
    positive INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
)
"""


@dataclass
class RuleEvaluationRow:
    """금요일 규칙 자동 평가 한 번의 기록 (스펙 2026-10-29 5.5). 규칙은 `Rule.to_json()` 문자열이다."""

    day: date
    status: str
    current_rule: str = ""
    best_rule: str = ""
    select_current: Optional[float] = None
    select_best: Optional[float] = None
    test_current: Optional[float] = None
    test_best: Optional[float] = None
    beats: bool = False
    positive: bool = False
    applied: bool = False
    note: str = ""
```

`__init__`의 `conn.execute(EXIT_REVIEW_SCHEMA_SQL)` 아래에 `conn.execute(RULE_EVALUATION_SCHEMA_SQL)`.

메서드 (`save_exit_review` 근처):

```python
    def save_rule_evaluation(self, row: RuleEvaluationRow) -> None:
        """평가 기록을 남긴다. 같은 날은 덮어쓴다 — 적용 여부를 정한 뒤 같은 줄을 다시 쓴다."""
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO rule_evaluations
                   (day, status, current_rule, best_rule, select_current, select_best,
                    test_current, test_best, beats, positive, applied, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.day.isoformat(), row.status, row.current_rule, row.best_rule,
                    row.select_current, row.select_best, row.test_current, row.test_best,
                    int(row.beats), int(row.positive), int(row.applied), row.note,
                ),
            )
            conn.commit()

    def rule_evaluations(self) -> List[RuleEvaluationRow]:
        """평가 기록 전체 (오래된 것부터) — 연속·휴지 판정의 재료다."""
        with closing(self._connect()) as conn:
            records = conn.execute("SELECT * FROM rule_evaluations ORDER BY day").fetchall()
        return [
            RuleEvaluationRow(
                day=date.fromisoformat(r["day"]),
                status=r["status"],
                current_rule=r["current_rule"],
                best_rule=r["best_rule"],
                select_current=r["select_current"],
                select_best=r["select_best"],
                test_current=r["test_current"],
                test_best=r["test_best"],
                beats=bool(r["beats"]),
                positive=bool(r["positive"]),
                applied=bool(r["applied"]),
                note=r["note"],
            )
            for r in records
        ]
```

(파일 상단 import에 `date`, `List`, `Optional`이 이미 있는지 확인하고 없으면 추가.)

`src/notification/templates.py` 끝에:

```python
def _rule_text(rule_json: str) -> str:
    """규칙 JSON → '문턱 0.2% / 뒤집기 끔 / 결정 09:10 / 손절 2%'. 비었으면 '-'."""
    if not rule_json:
        return "-"
    from src.research.backtest import Rule

    rule = Rule.from_json(rule_json)
    return (
        f"문턱 {rule.band_percent:g}% / 뒤집기 {'켬' if rule.flip_enabled else '끔'} / "
        f"결정 {rule.decision_hhmm} / 손절 {rule.stop_percent:g}%"
    )


def _daily_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.3f}%"


def rule_evaluation_email(
    row: "RuleEvaluationRow",
    streak: int,
    cooldown: int,
    applied_values: Optional[Dict[str, tuple]] = None,
) -> tuple[str, str]:
    """금요일 2호 규칙 평가 메일 (스펙 2026-10-29 5.7). 적용한 주는 제목과 '바꾼 값'이 달라진다."""
    from src.research import rule_search

    kind = "자동 변경" if applied_values else "주간 평가"
    subject = f"[AutoTrade] {row.day:%Y-%m-%d} 2호 규칙 {kind}"
    lines = [f"현행 규칙: {_rule_text(row.current_rule)}"]
    if row.status != rule_search.STATUS_EVALUATED:
        lines.extend(["", f"평가하지 않았습니다: {row.note or row.status}"])
        return subject, "\n".join(lines)

    lines.extend([
        f"1등 후보: {_rule_text(row.best_rule)}",
        "",
        "일 평균 순수익률 (수수료·슬리피지 포함, 쉬는 날 0%)",
        f" - 고르기 구간: 현행 {_daily_pct(row.select_current)} / 1등 {_daily_pct(row.select_best)}",
        f" - 시험 구간(최근 {rule_search.TEST_DAYS}거래일): 현행 {_daily_pct(row.test_current)} / "
        f"1등 {_daily_pct(row.test_best)}",
        "",
        "적용 조건",
        f" 1. 시험 구간에서 현행보다 {rule_search.MIN_EDGE_PERCENT}%p/일 이상: "
        f"{'충족' if row.beats else '미충족'}",
        f" 2. 1등의 시험 구간 성과가 플러스: {'충족' if row.positive else '미충족'}",
        f" 3. 같은 1등 연속: {streak}/{rule_search.STREAK_EVALUATIONS}",
        f" 4. 휴지 기간: {'충족' if cooldown == 0 else f'{cooldown}회 남음'}",
    ])
    if applied_values:
        lines.extend(["", "바꾼 값 (.env)"])
        lines.extend(f" - {key}: {old} → {new}" for key, (old, new) in applied_values.items())
    if row.note:
        lines.extend(["", f"비고: {row.note}"])
    lines.extend(["", "※ 평가는 손절과 15:15 청산만으로 한다 — AI 매도 판단은 재현하지 않는다."])
    return subject, "\n".join(lines)
```

(`Optional`, `Dict`는 이미 import돼 있다. `RuleEvaluationRow`는 문자열 주석이라 import하지 않아도 되지만, 상단의 `from src.logger.trade_store import (...)`에 추가해도 된다.)

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_logger/test_rule_evaluations.py tests/test_notification/test_templates.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/logger/trade_store.py src/notification/templates.py tests/test_logger/test_rule_evaluations.py tests/test_notification/test_templates.py
git commit -m "Store weekly rule evaluations and mail their results"
```

---

### Task 6: 오케스트레이터 `RuleTuning`

**Files:**
- Create: `src/research/rule_tuning.py`
- Test: `tests/test_research/test_rule_tuning.py`

**Interfaces:**
- Consumes: `MinuteStore`, `MarketDataClient.get_minute_candles`, `Backtester`, `Rule`, `rule_search.*`, `TradeStore.save_rule_evaluation/rule_evaluations`, `RuleEvaluationRow`, `templates.rule_evaluation_email`, `Settings` 필드(`etf_signal_band_percent`, `etf_flip_enabled`, `recommend_time_hhmm`, `buy_time_hhmm`, `stop_loss_percent`, `commission_ratio`, `rule_auto_apply_enabled`, `validate()`)
- Produces: `RuleTuning(settings, market_data, trade_store, email, env_writer: Callable[[Dict[str, str]], None], open_tickers: Callable[[], List[str]], minute_store: Optional[MinuteStore] = None, backtester_factory=Backtester)`
- Produces: `.collect_minute_bars() -> None`, `.evaluate_rules(today: Optional[date] = None) -> None`, `.current_rule() -> Rule`, 속성 `.applied_pending: bool`
- Produces: `add_minutes(hhmm: str, minutes: int) -> str`, 상수 `BUY_AFTER_DECISION_MINUTES = 3`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_research/test_rule_tuning.py`:

```python
from dataclasses import dataclass, replace
from datetime import date
from types import SimpleNamespace

from src.logger.trade_store import TradeStore
from src.research import rule_search
from src.research.backtest import Rule
from src.research.minute_store import MinuteStore
from src.research.rule_tuning import RuleTuning, add_minutes

BEST = Rule(0.2, False, "09:30", 2.0)


@dataclass
class FakeSettings:
    etf_signal_band_percent: float = 0.0
    etf_flip_enabled: bool = True
    recommend_time_hhmm: str = "09:10"
    buy_time_hhmm: str = "09:13"
    stop_loss_percent: float = 2.0
    commission_ratio: float = 0.00015
    rule_auto_apply_enabled: bool = True
    invalid: bool = False

    def validate(self):
        if self.invalid:
            raise ValueError("bad")


class FakeEmail:
    def __init__(self):
        self.sent = []

    def send(self, subject, message, html=None, images=None):
        self.sent.append((subject, message))


def make(tmp_path, evaluation, settings=None, holdings=(), writer_error=None):
    written = []

    def env_writer(values):
        if writer_error:
            raise writer_error
        written.append(values)

    tuning = RuleTuning(
        settings=settings or FakeSettings(),
        market_data=SimpleNamespace(),
        trade_store=TradeStore(tmp_path / "t.db"),
        email=FakeEmail(),
        env_writer=env_writer,
        open_tickers=lambda: list(holdings),
        minute_store=MinuteStore(tmp_path / "minute"),
        backtester_factory=lambda *a: SimpleNamespace(days=list(range(200))),
    )
    tuning._evaluate = lambda backtester, current: evaluation(current)
    return tuning, written


def good(current):
    return rule_search.Evaluation(current, BEST, 0.0, 0.2, 0.0, 0.1)


def run_weeks(tuning, n, start_day=1):
    for i in range(n):
        tuning.evaluate_rules(date(2026, 11, start_day + 7 * i))


def test_add_minutes():
    assert add_minutes("09:10", 3) == "09:13"
    assert add_minutes("09:58", 3) == "10:01"


def test_applies_after_four_consecutive_wins(tmp_path):
    tuning, written = make(tmp_path, good)
    run_weeks(tuning, 3)
    assert written == [] and not tuning.applied_pending

    tuning.evaluate_rules(date(2026, 11, 22))

    assert written == [{
        "ETF_SIGNAL_BAND_PERCENT": "0.2", "ETF_FLIP_ENABLED": "0",
        "RECOMMEND_TIME": "09:30", "BUY_TIME": "09:33", "STOP_LOSS_PERCENT": "2",
    }]
    assert tuning.applied_pending
    rows = tuning.trade_store.rule_evaluations()
    assert rows[-1].applied and not any(r.applied for r in rows[:-1])
    assert "자동 변경" in tuning.email.sent[-1][0]
    assert "STOP_LOSS_PERCENT" not in tuning.email.sent[-1][1]       # 안 바뀐 값은 메일에 없다


def test_does_not_apply_when_switched_off(tmp_path):
    tuning, written = make(tmp_path, good, settings=FakeSettings(rule_auto_apply_enabled=False))
    run_weeks(tuning, 4)
    assert written == []
    assert "자동 적용 꺼짐" in tuning.trade_store.rule_evaluations()[-1].note


def test_postpones_while_holding(tmp_path):
    tuning, written = make(tmp_path, good, holdings=["069500"])
    run_weeks(tuning, 4)
    assert written == []
    assert "보유" in tuning.trade_store.rule_evaluations()[-1].note


def test_validate_failure_blocks(tmp_path):
    tuning, written = make(tmp_path, good, settings=FakeSettings(invalid=True))
    run_weeks(tuning, 4)
    assert written == []
    assert "설정 검사 실패" in tuning.trade_store.rule_evaluations()[-1].note


def test_env_write_failure_blocks(tmp_path):
    tuning, written = make(tmp_path, good, writer_error=OSError("locked"))
    run_weeks(tuning, 4)
    assert not tuning.applied_pending
    rows = tuning.trade_store.rule_evaluations()
    assert not rows[-1].applied and ".env 쓰기 실패" in rows[-1].note


def test_insufficient_data_is_recorded_and_breaks_the_streak(tmp_path):
    results = iter([good, good, lambda c: None, good, good, good])
    tuning, written = make(tmp_path, lambda current: next(results)(current))
    run_weeks(tuning, 6)
    assert written == []                              # 끊긴 뒤 3번뿐이다
    assert tuning.trade_store.rule_evaluations()[2].status == rule_search.STATUS_INSUFFICIENT


def test_exception_is_recorded_as_failed(tmp_path):
    def boom(current):
        raise RuntimeError("x")

    tuning, written = make(tmp_path, boom)
    tuning.evaluate_rules(date(2026, 11, 6))
    assert tuning.trade_store.rule_evaluations()[0].status == rule_search.STATUS_FAILED
    assert len(tuning.email.sent) == 1


def test_best_equal_to_current_is_not_applied(tmp_path):
    tuning, written = make(tmp_path, lambda c: rule_search.Evaluation(c, c, 0.1, 0.1, 0.1, 0.1))
    run_weeks(tuning, 4)
    assert written == []


def test_collect_minute_bars_fetches_since_the_last_day(tmp_path):
    calls = []

    def get_minute_candles(ticker, since=None):
        calls.append((ticker, since))
        return {"20261029": [("0900", 1.0)]}

    tuning, _ = make(tmp_path, good)
    tuning.market_data = SimpleNamespace(get_minute_candles=get_minute_candles)
    tuning.minute_store.merge("069500", {"20261028": [("0900", 1.0)]})

    tuning.collect_minute_bars()

    assert calls == [("069500", "20261028"), ("114800", None)]
    assert tuning.minute_store.last_day("114800") == "20261029"


def test_real_settings_survive_replace_and_validate(monkeypatch):
    """자동 적용은 `dataclasses.replace(settings, ...)`로 새 값을 검사한다 — 실제 Settings에서도 돼야 한다.

    `validate()`가 요구하는 나머지 환경변수(키움 키 등)는 tests/test_config/test_settings.py에서
    유효한 Settings를 만드는 방식을 그대로 따른다.
    """
    from config.settings import Settings

    monkeypatch.setenv("TRADE_MODE", "paper")
    monkeypatch.setenv("KIWOOM_APP_KEY", "k")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "s")
    settings = replace(
        Settings(), etf_signal_band_percent=0.2, etf_flip_enabled=False,
        recommend_time_hhmm="09:30", buy_time_hhmm="09:33", stop_loss_percent=1.5,
    )
    settings.validate()
    assert (settings.recommend_time.hour, settings.recommend_time.minute) == (9, 30)


def test_collect_failure_for_one_ticker_does_not_stop_the_other(tmp_path):
    def get_minute_candles(ticker, since=None):
        if ticker == "069500":
            raise RuntimeError("timeout")
        return {"20261029": [("0900", 1.0)]}

    tuning, _ = make(tmp_path, good)
    tuning.market_data = SimpleNamespace(get_minute_candles=get_minute_candles)
    tuning.collect_minute_bars()
    assert tuning.minute_store.last_day("114800") == "20261029"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research/test_rule_tuning.py -q`
Expected: FAIL (`No module named 'src.research.rule_tuning'`)

- [ ] **Step 3: 구현**

`src/research/rule_tuning.py`:

```python
"""2호 전략 규칙 자동 검증·적용 오케스트레이터 (스펙 2026-10-29).

매일 15:45 `collect_minute_bars`가 1분봉을 쌓고, 금요일 16:00 `evaluate_rules`가 후보 160개를 평가해
기록하고, 조건을 모두 만족하면 `.env`를 바꾼 뒤 메일을 보낸다. 적용하면 `applied_pending`을 세워
UI가 설정을 다시 읽고 엔진을 재시작하게 한다 (`EngineThread.consume_rule_change`).

어떤 실패도 매매를 막지 않고, 실패하면 '바꾸지 않음'으로 끝난다.
"""
import logging
from dataclasses import replace
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from src.logger.trade_store import RuleEvaluationRow
from src.notification import templates
from src.research import rule_search
from src.research.backtest import Backtester, Rule
from src.research.minute_store import MinuteStore
from src.strategy.etf_regime import ETF_TICKERS, INVERSE_ETF, LONG_ETF

logger = logging.getLogger(__name__)

# 결정 시각을 바꾸면 매수 안전망 시각은 결정 +3분 (`Settings.validate`의 최소 간격)
BUY_AFTER_DECISION_MINUTES = 3


def add_minutes(hhmm: str, minutes: int) -> str:
    hour, minute = map(int, hhmm.split(":"))
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _env_values(rule: Rule, buy_hhmm: str) -> Dict[str, str]:
    return {
        "ETF_SIGNAL_BAND_PERCENT": f"{rule.band_percent:g}",
        "ETF_FLIP_ENABLED": "1" if rule.flip_enabled else "0",
        "RECOMMEND_TIME": rule.decision_hhmm,
        "BUY_TIME": buy_hhmm,
        "STOP_LOSS_PERCENT": f"{rule.stop_percent:g}",
    }


class RuleTuning:
    def __init__(
        self,
        settings,
        market_data,
        trade_store,
        email,
        env_writer: Callable[[Dict[str, str]], None],
        open_tickers: Callable[[], List[str]],
        minute_store: Optional[MinuteStore] = None,
        backtester_factory=Backtester,
    ):
        self.settings = settings
        self.market_data = market_data
        self.trade_store = trade_store
        self.email = email
        self.env_writer = env_writer
        self.open_tickers = open_tickers
        self.minute_store = minute_store or MinuteStore()
        self.backtester_factory = backtester_factory
        # UI 스레드가 읽고 비운다 — 단순 bool이라 잠금이 없다 (ActionRunner.busy와 같은 방식)
        self.applied_pending = False
        self._evaluate = rule_search.evaluate

    def current_rule(self) -> Rule:
        s = self.settings
        return Rule(s.etf_signal_band_percent, s.etf_flip_enabled, s.recommend_time_hhmm, s.stop_loss_percent)

    def collect_minute_bars(self) -> None:
        """마지막 저장일부터 오늘까지 1분봉을 받아 쌓는다. 종목 하나가 실패해도 다른 종목은 받는다."""
        for ticker in ETF_TICKERS:
            since = self.minute_store.last_day(ticker)
            try:
                bars = self.market_data.get_minute_candles(ticker, since)
                saved = self.minute_store.merge(ticker, bars)
            except Exception:
                logger.exception("1분봉 수집 실패: %s (기준 %s) — 다음 수집이 다시 채웁니다.", ticker, since)
                continue
            logger.info("1분봉 저장: %s %d일 (기준 %s)", ticker, saved, since or "처음")

    def evaluate_rules(self, today: Optional[date] = None) -> None:
        today = today or date.today()
        current = self.current_rule()
        evaluation = None
        try:
            backtester = self.backtester_factory(
                self.minute_store.load(LONG_ETF[0]),
                self.minute_store.load(INVERSE_ETF[0]),
                self.settings.commission_ratio,
            )
            evaluation = self._evaluate(backtester, current)
        except Exception:
            logger.exception("2호 규칙 평가 실패 — 이번 주는 바꾸지 않습니다.")
            row = RuleEvaluationRow(
                day=today, status=rule_search.STATUS_FAILED, current_rule=current.to_json(), note="평가 중 예외"
            )
        else:
            if evaluation is None:
                needed = rule_search.TEST_DAYS + rule_search.MIN_SELECT_DAYS
                row = RuleEvaluationRow(
                    day=today,
                    status=rule_search.STATUS_INSUFFICIENT,
                    current_rule=current.to_json(),
                    note=f"평가 가능한 거래일 {len(backtester.days)}일 < {needed}일",
                )
            else:
                row = RuleEvaluationRow(
                    day=today,
                    status=rule_search.STATUS_EVALUATED,
                    current_rule=current.to_json(),
                    best_rule=evaluation.best.to_json(),
                    select_current=evaluation.select_current,
                    select_best=evaluation.select_best,
                    test_current=evaluation.test_current,
                    test_best=evaluation.test_best,
                    beats=evaluation.beats,
                    positive=evaluation.positive,
                )

        # 이번 주 기록이 들어가야 연속 판정에 이번 주가 든다
        self.trade_store.save_rule_evaluation(row)
        history = self.trade_store.rule_evaluations()
        streak = rule_search.streak_count(history)
        cooldown = rule_search.cooldown_left(history)

        applied_values = None
        if (
            evaluation is not None
            and evaluation.best != current
            and streak >= rule_search.STREAK_EVALUATIONS
            and cooldown == 0
        ):
            applied_values, row.note = self._apply(evaluation.best, current)
            row.applied = applied_values is not None
            self.trade_store.save_rule_evaluation(row)
            logger.warning("2호 규칙 자동 적용 판단: %s — %s", row.note, applied_values)

        subject, body = templates.rule_evaluation_email(row, streak, cooldown, applied_values)
        self.email.send(subject, body)

    def _apply(self, best: Rule, current: Rule) -> Tuple[Optional[Dict[str, Tuple[str, str]]], str]:
        """`.env`를 바꾼다. 바꾼 값 {키: (이전, 이후)}와 비고를 돌려준다. 못 바꿨으면 (None, 사유)."""
        if not self.settings.rule_auto_apply_enabled:
            return None, "자동 적용 꺼짐 (RULE_AUTO_APPLY_ENABLED=0)"
        if self.open_tickers():
            return None, "보유 종목이 있어 다음 평가로 미룸"
        if not rule_search.in_range(best):
            return None, "후보 범위 밖"
        buy_hhmm = add_minutes(best.decision_hhmm, BUY_AFTER_DECISION_MINUTES)
        try:
            replace(
                self.settings,
                etf_signal_band_percent=best.band_percent,
                etf_flip_enabled=best.flip_enabled,
                recommend_time_hhmm=best.decision_hhmm,
                buy_time_hhmm=buy_hhmm,
                stop_loss_percent=best.stop_percent,
            ).validate()
        except Exception as e:
            return None, f"설정 검사 실패: {e}"

        new_values = _env_values(best, buy_hhmm)
        old_values = _env_values(current, self.settings.buy_time_hhmm)
        try:
            self.env_writer(new_values)
        except Exception as e:
            logger.exception(".env 쓰기 실패 — 규칙을 바꾸지 않습니다.")
            return None, f".env 쓰기 실패: {e}"
        self.applied_pending = True
        changed = {key: (old_values[key], new_values[key]) for key in new_values if old_values[key] != new_values[key]}
        return changed, "자동 적용"
```

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_research -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/research/rule_tuning.py tests/test_research/test_rule_tuning.py
git commit -m "Add the weekly rule evaluation and auto-apply orchestrator"
```

---

### Task 7: 스케줄·실행 큐·UI 연결과 문서

**Files:**
- Modify: `src/core/actions.py` (`SCHEDULED_ACTIONS` ~line 66-73, `step_factories`의 `review_exits` 아래)
- Modify: `src/core/runtime.py` (상수 ~line 64 근처, `Runtime` dataclass ~line 148-160, `build_runtime` ~line 205-255)
- Modify: `src/ui/engine_thread.py` (메서드 추가), `src/ui/main_window.py:1627-1635` (`_on_action_finished`)
- Modify: `CLAUDE.md`, `주식자동매매_PRD.md`
- Test: `tests/test_core/test_actions.py`, `tests/test_core/test_rule_schedule.py`

**Interfaces:**
- Consumes: `RuleTuning(...)`, `.collect_minute_bars`, `.evaluate_rules`, `.applied_pending`
- Produces: 스케줄 액션 `"collect_minute_bars"`, `"evaluate_rules"`, `Runtime.research: Optional[RuleTuning]`, `runtime._on_weekday(weekday: int, job, now_fn=datetime.now)`, `runtime.ENV_PATH`, `runtime.MINUTE_COLLECT_TIME = 15:45`, `runtime.RULE_EVAL_TIME = 16:00`, `runtime.RULE_EVAL_WEEKDAY = 4`, `EngineThread.consume_rule_change() -> bool`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_core/test_rule_schedule.py`:

```python
from datetime import datetime
from types import SimpleNamespace

from src.core import actions, runtime


def test_on_weekday_runs_only_on_that_weekday():
    calls = []
    friday = lambda: datetime(2026, 10, 30, 16, 0)
    thursday = lambda: datetime(2026, 10, 29, 16, 0)

    runtime._on_weekday(4, lambda: calls.append("x"), now_fn=thursday)()
    runtime._on_weekday(4, lambda: calls.append("x"), now_fn=friday)()

    assert calls == ["x"]


def test_rule_actions_run_off_the_loop_thread():
    research = SimpleNamespace(collect_minute_bars=lambda: None, evaluate_rules=lambda: None)
    rt = SimpleNamespace(research=research)

    for action in ("collect_minute_bars", "evaluate_rules"):
        steps = actions.manual_steps(rt, action)
        assert len(steps) == 1 and steps[0].touches_orders is False
        assert action in actions.SCHEDULED_ACTIONS
        assert action not in actions.MANUAL_ACTIONS
        assert action not in runtime.HOLIDAY_ALWAYS_ACTIONS
```

`tests/test_core/test_actions.py`: 이 파일에 `SCHEDULED_ACTIONS`의 키 목록을 그대로 단언하는 테스트가 있으면 두 액션을 기대값에 추가한다 (`grep -n "SCHEDULED_ACTIONS" tests/test_core/test_actions.py`로 확인).

- [ ] **Step 2: 실패 확인**

Run: `.venv\Scripts\python.exe -m pytest tests/test_core/test_rule_schedule.py -q`
Expected: FAIL (`module 'src.core.runtime' has no attribute '_on_weekday'`)

- [ ] **Step 3: 구현**

`src/core/actions.py` — `SCHEDULED_ACTIONS`에 추가:

```python
    # 2호 규칙 자동 검증 (2026-10-29 스펙) — 매일 분봉을 쌓고 금요일에 평가·자동 적용한다
    "collect_minute_bars": "1분봉 수집 (스케줄)",
    "evaluate_rules": "2호 규칙 주간 평가·자동 적용 (스케줄)",
```

`step_factories`의 `review_exits` 항목 아래:

```python
        # 연속조회·백테스트 계산이 길어 루프 스레드를 쓰지 않는다 (WebSocket PING 응답 유지)
        "collect_minute_bars": lambda: [
            ManualStep(SCHEDULED_ACTIONS["collect_minute_bars"], runtime.research.collect_minute_bars)
        ],
        "evaluate_rules": lambda: [
            ManualStep(SCHEDULED_ACTIONS["evaluate_rules"], runtime.research.evaluate_rules)
        ],
```

`src/core/runtime.py`:
1. import: `from pathlib import Path`, `from src.research.rule_tuning import RuleTuning`, `from src.ui.env_store import save_env` (env_store는 PyQt를 쓰지 않는 순수 모듈이다).
2. 상수 (`REVIEW_EXITS_TIME` 아래):

```python
# 2호 규칙 자동 검증 (docs/superpowers/specs/2026-10-29-rule-auto-tuning-design.md).
# 분봉 수집은 매도 검증 뒤, 평가는 금요일 장 마감 뒤 — 15:15 강제청산 뒤라 적용 시점에 보유 종목이 없다.
MINUTE_COLLECT_TIME = dt_time(15, 45)
RULE_EVAL_TIME = dt_time(16, 0)
RULE_EVAL_WEEKDAY = 4  # 금요일
# 자동 적용이 쓰는 .env — UI(`main_window.ENV_PATH`)와 같은 파일이다
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
```

3. `_trading_days_only` 아래에:

```python
def _on_weekday(weekday: int, job: Callable[[], None], now_fn=datetime.now) -> Callable[[], None]:
    """정한 요일에만 작업을 넘긴다 — 스케줄러는 매일 발동하므로 주간 작업은 여기서 거른다."""

    def guarded() -> None:
        if now_fn().weekday() == weekday:
            job()

    return guarded
```

4. `Runtime` dataclass 끝에 필드:

```python
    # 2호 규칙 자동 검증 — 15:45 분봉 수집, 금요일 16:00 평가·자동 적용
    research: Optional[RuleTuning] = None
```

5. `build_runtime`에서 `Runtime(...)` 생성 직후(`runtime.runner = ActionRunner(runtime)` 근처):

```python
    runtime.research = RuleTuning(
        settings=settings,
        market_data=market_data,
        trade_store=trade_store,
        email=email,
        env_writer=lambda values: save_env(ENV_PATH, values),
        open_tickers=lambda: engine.open_tickers,
    )
```

6. 스케줄 등록 튜플에 `(MINUTE_COLLECT_TIME, "collect_minute_bars"),`를 `(REVIEW_EXITS_TIME, "review_exits"),` 아래에 추가하고, 루프 뒤에:

```python
    scheduler.add_job(
        RULE_EVAL_TIME,
        _trading_days_only(
            _on_weekday(RULE_EVAL_WEEKDAY, _submit(runtime, "evaluate_rules")), "evaluate_rules"
        ),
        name="evaluate_rules",
    )
```

`src/ui/engine_thread.py` — 메서드 추가 (`set_exit_flags` 근처):

```python
    def consume_rule_change(self) -> bool:
        """금요일 규칙 평가가 `.env`를 바꿨으면 한 번만 True — UI가 설정을 다시 읽고 엔진을 재시작한다."""
        runtime = getattr(self, "_runtime", None)
        research = getattr(runtime, "research", None)
        if research is None or not research.applied_pending:
            return False
        research.applied_pending = False
        return True
```

(`EngineThread`가 런타임을 `self._runtime`이 아닌 다른 이름으로 들고 있으면 그 이름을 쓴다 — `run()`의 `self._runtime.runner.on_started` 줄로 확인.)

`src/ui/main_window.py` — `_on_action_finished` 끝에:

```python
        # 금요일 규칙 평가가 .env를 바꿨다 — 화면 값을 먼저 다시 읽어야 재시작 때의 '설정 저장'이
        # 옛 손절값으로 덮어쓰지 않는다 (_start_engine이 화면 값을 .env에 저장한다)
        if (
            action == "evaluate_rules"
            and ok
            and self._engine_thread is not None
            and self._engine_thread.consume_rule_change()
        ):
            logger.warning("2호 규칙이 자동으로 바뀌었습니다 — 설정을 다시 읽고 엔진을 재시작합니다.")
            self._load_settings()
            self._restart_engine()
```

문서:
- `CLAUDE.md` "스레드 / 이벤트 루프 구조"의 스케줄 목록에 15:45 1분봉 수집과 금요일 16:00 2호 규칙 평가·자동 적용을 넣고, "전략 프레임워크"의 2호 문단에 "문턱(`ETF_SIGNAL_BAND_PERCENT`)·뒤집기(`ETF_FLIP_ENABLED`)·결정 시각·손절은 금요일 규칙 자동 평가가 `.env`를 바꿀 수 있다(조건은 스펙 2026-10-29 4.2, 끄려면 `RULE_AUTO_APPLY_ENABLED=0`)"를 한 문장으로 추가한다.
- `주식자동매매_PRD.md` 5.5-C 끝에 "규칙 자동 검증·적용 (확정 2026-10-29)" 항목을 추가한다: 후보 160개, 고르기·시험 구간, 적용 조건 5개, 분봉 저장 위치, 한계(AI 매도 미재현). 맨 위 작성일 행의 2026-10-29 항목 끝에 " / 2호 규칙 주간 자동 검증·적용" 추가.

- [ ] **Step 4: 통과 확인**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: 전체 PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/actions.py src/core/runtime.py src/ui/engine_thread.py src/ui/main_window.py tests/test_core/test_rule_schedule.py tests/test_core/test_actions.py CLAUDE.md 주식자동매매_PRD.md
git commit -m "Schedule minute-bar collection and Friday rule evaluation, restart on auto-apply"
```

---

## Self-Review 결과

- 스펙 커버리지: 3절(후보·설정화) → Task 1·4, 4.1 평가 → Task 4·6, 4.2 조건 → Task 4·6, 4.3 적용·UI 재로딩 → Task 6·7, 5.1 수집 → Task 2·6, 5.2 백테스트 → Task 3, 5.3 품질 → Task 3(`MIN_BARS_PER_DAY`)·4(`TEST_DAYS + MIN_SELECT_DAYS`), 5.4 설정 → Task 1, 5.5 기록 → Task 5, 5.6 스케줄 → Task 7, 5.7 메일 → Task 5, 6절 실패 처리 → Task 6 테스트, 7절 테스트 → 각 Task.
- 스펙과 다른 점 하나: 스펙 5.6은 단계를 `DailyWorkflow`에 둔다고 했지만, `daily_workflow.py`가 이미 1,000줄을 넘어 별도 오케스트레이터(`src/research/rule_tuning.py`)로 분리했다. 실행 큐·스케줄·휴장 처리는 스펙대로다.
- 스펙 7절의 "실제 분봉 회귀 테스트(선택)"는 넣지 않았다 — `data/minute/`이 처음 수집된 뒤 수동으로 확인한다.
