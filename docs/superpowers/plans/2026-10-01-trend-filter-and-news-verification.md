# 추세 필터와 뉴스 검증 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 하락 추세를 프롬프트가 볼 수 있게 하고, 추천된 종목에 구체적 악재가 있으면 빼고 다시 추천받는다.

**Architecture:** ① 이미 받아 둔 20일 종가에서 5일 이평을 함께 내어 후보 줄에 싣는다(추가 API 호출 0). ② 추천 직후 추천된 종목만 종목당 1회·병렬로 웹검색 검증하고, 걸린 종목을 제외해 **1회만** 재추천한다. 모든 실패는 "종전대로 매수"로 떨어진다.

**Tech Stack:** Python 3, `anthropic` SDK (`web_search_20260209` 서버 툴 + `output_config.format` json_schema), SQLite, pytest.

**Spec:** [`docs/superpowers/specs/2026-10-01-trend-filter-and-news-verification-design.md`](../specs/2026-10-01-trend-filter-and-news-verification-design.md)

## Global Constraints

- 답변·주석·로그 문구는 한국어, 코드 식별자는 영어. 기존 파일의 주석 밀도와 어투를 그대로 따른다.
- **실패는 언제나 "종전대로 매수"로 떨어진다** — 검증 실패·타임아웃·형식 오류·`checked=false`·예산 부족 전부. 검증이 매수를 막는 경로는 `blocking=true` 하나뿐이다.
- **재추천은 최대 1회.** 재추천 결과가 또 탈락해도 더 돌지 않는다.
- LLM 호출은 전부 스트리밍 + `max_retries=0` + `deadline`으로 예산을 직접 지킨다 (`recommender.py`·`exit_advisor.py`와 같은 꼴).
- 모델은 `settings.llm_model`을 쓴다. 코드에 모델 ID를 적지 않는다.
- 웹검색 툴은 `{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}`, `output_config`에 `"effort": "low"`. 실측 근거는 스펙 2절.
- `.env` 값은 `config/settings.py`의 `Settings` dataclass에 `os.getenv` 기본값으로만 추가한다. 별도 설정 파일을 만들지 않는다.
- 테스트는 실제 네트워크를 때리지 않는다. LLM 클라이언트는 가짜 객체로 대체한다.
- `pytest`는 프로젝트 루트에서 `.venv\Scripts\python.exe -m pytest`로 돌린다 (Windows / PowerShell 5.1, `&&` 없음).

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/api/market_data.py` | 5일 이평 산출 | 수정 |
| `src/data/collector.py` | 5일 이평을 `DailyStockData`로 전달 | 수정 |
| `src/llm/recommender.py` | 후보 줄에 추세 싣기, 잠긴 절에 추세 지시, 제외 종목 지원 | 수정 |
| `src/llm/news_verifier.py` | 종목별 웹검색 악재 검증 | **신규** |
| `config/settings.py` | `NEWS_VERIFY_ENABLED`, `BUY_TIME` 기본값 | 수정 |
| `src/logger/trade_store.py` | `recommendations` 3열 추가·저장 | 수정 |
| `src/core/daily_workflow.py` | 검증 → 탈락 → 재추천 1회 오케스트레이션 | 수정 |
| `src/notification/templates.py` | 추천 메일에 검증 결과 | 수정 |

---

### Task 1: 5일 이평 산출

**Files:**
- Modify: `src/api/market_data.py` (`PreviousDayMetrics`, `get_previous_day_metrics`)
- Modify: `src/data/collector.py` (`DailyStockData`, 매핑부)
- Test: `tests/test_api/test_market_data.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces: `PreviousDayMetrics.short_moving_average: float`, `DailyStockData.short_moving_average: float`, 모듈 상수 `SHORT_MA_DAYS = 5`. 산출 불가면 `0.0` (거래량 급증 배수와 같은 규약).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_api/test_market_data.py`에 추가한다. 기존 테스트가 쓰는 가짜 캔들 헬퍼를 먼저 확인하고 같은 꼴을 따른다 (`grep -n "def .*candle\|get_previous_day_metrics" tests/test_api/test_market_data.py`).

```python
def test_short_moving_average_uses_the_five_most_recent_closes():
    """20일 이평만으로는 추세 방향을 알 수 없다 — 5일 이평을 함께 낸다 (2026-10-01)."""
    # 최신순 20봉: 최근 5봉이 100, 그 앞 15봉이 200
    closes = [100.0] * 5 + [200.0] * 15
    candles = [
        _candle(date_str=f"2026{i:04d}", close=c, high=c, low=c, volume=1000)
        for i, c in enumerate(closes, start=1)
    ]
    client = _client_returning(candles)

    metrics = client.get_previous_day_metrics("005930", today=date(2026, 10, 1))

    assert metrics.short_moving_average == 100.0
    assert metrics.moving_average == pytest.approx((100 * 5 + 200 * 15) / 20)


def test_short_moving_average_is_zero_when_there_are_too_few_bars():
    """봉이 5개 미만이면 산출 불가 — 0.0으로 둬서 프롬프트에서 통째로 빠지게 한다."""
    candles = [
        _candle(date_str=f"2026{i:04d}", close=100.0, high=100.0, low=100.0, volume=1000)
        for i in range(1, 5)  # 4봉
    ]
    client = _client_returning(candles)

    metrics = client.get_previous_day_metrics("005930", today=date(2026, 10, 1))

    assert metrics.short_moving_average == 0.0
```

`_candle`과 `_client_returning`이 기존 파일에 없으면, 그 파일이 이미 쓰는 가짜 생성 방식에 맞춰 이름을 바꾼다. **새 헬퍼를 만들지 말고 기존 것을 쓴다.**

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_api/test_market_data.py -k short_moving_average -v
```
Expected: FAIL — `AttributeError: 'PreviousDayMetrics' object has no attribute 'short_moving_average'`

- [ ] **Step 3: `PreviousDayMetrics`에 필드를 더한다**

`src/api/market_data.py`의 `DAILY_CANDLE_COUNT = 20` 아래에 상수를 더한다:

```python
# 추세 방향용 단기 이평 구간 (2026-10-01). 20일 이평만 주면 "지금 이평 아래"는 알아도
# "이평이 내려가는 중"은 알 수 없어, 하락 추세 종목이 떨어질수록 더 매력적으로 평가됐다
# (PRD 10절 "하락 추세에 대한 방어가 없다").
SHORT_MA_DAYS = 5
```

`PreviousDayMetrics`의 `moving_average` 줄 바로 아래에:

```python
    # 최근 SHORT_MA_DAYS 거래일 종가 평균. moving_average와의 비율이 추세 방향이다.
    # 봉이 모자라면 0.0 (= 산출 불가, 거래량 급증 배수와 같은 규약)
    short_moving_average: float = 0.0
```

- [ ] **Step 4: `get_previous_day_metrics`에서 계산한다**

`closes`는 `past`(최신순)에서 뽑으므로 앞에서 자르면 최근 구간이다. `return PreviousDayMetrics(` 바로 앞에 넣고, 반환값에 필드를 더한다:

```python
        # closes는 past(최신순)에서 뽑았으므로 앞에서 자른 것이 최근 구간이다
        short_closes = closes[:SHORT_MA_DAYS]
        short_average = (
            sum(short_closes) / len(short_closes) if len(short_closes) == SHORT_MA_DAYS else 0.0
        )

        return PreviousDayMetrics(
            ...
            moving_average=sum(closes) / len(closes) if closes else 0.0,
            short_moving_average=short_average,
        )
```

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_api/test_market_data.py -k short_moving_average -v
```
Expected: PASS (2 passed)

- [ ] **Step 6: `DailyStockData`로 전달한다**

`src/data/collector.py`의 `moving_average: float = 0.0` 줄 아래에:

```python
    # 최근 5거래일 종가 평균 — moving_average와의 비율이 추세 방향이다 (2026-10-01)
    short_moving_average: float = 0.0
```

그리고 `recent_high=metrics.recent_high,` 부근의 매핑에 `short_moving_average=metrics.short_moving_average,`를 더한다 (현재 398행 근처).

- [ ] **Step 7: 전체 테스트를 돌린다**

```
.venv\Scripts\python.exe -m pytest -q
```
Expected: 기존 테스트 전부 통과 + 새 테스트 2건

- [ ] **Step 8: 커밋**

```bash
git add src/api/market_data.py src/data/collector.py tests/test_api/test_market_data.py
git commit -m "20일 이평과 함께 5일 이평을 산출한다"
```

---

### Task 2: 추세를 프롬프트에 싣는다

**Files:**
- Modify: `src/llm/recommender.py` (`build_user_prompt`, `build_locked_prompt_text`)
- Test: `tests/test_llm/test_recommender.py`

**Interfaces:**
- Consumes: `DailyStockData.short_moving_average` (Task 1)
- Produces: 후보 줄의 `5일 이평 N (20일 대비 ±X.X%)` 토막, 잠긴 절 `## 추세 판단`

**왜 잠긴 절인가:** `judgment_criteria`는 15:35 튜너가 고칠 수 있어, 거기 넣으면 자동 수정이 지울 수 있다. 다만 `절대 규칙`에는 넣지 않는다 — 그 절은 "코드가 강제하는 규칙"이라는 뜻이고(PRD 5.13), 이건 코드가 강제하지 않는다. 별도 잠긴 절로 둔다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_llm/test_recommender.py`에 추가한다. 이 파일이 `DailyStockData`를 만드는 기존 헬퍼를 먼저 확인한다 (`grep -n "DailyStockData(" tests/test_llm/test_recommender.py`).

```python
def test_user_prompt_shows_the_short_moving_average_gap():
    """떨어지는 중인지 눌린 것인지를 가르려면 이평 방향이 필요하다 (2026-10-01)."""
    data = _daily(moving_average=207_726.0, short_moving_average=198_420.0,
                  recent_high=220_000.0, recent_low=194_300.0)

    prompt = build_user_prompt([data], target_count=2)

    assert "5일 이평 198,420 (20일 대비 -4.48%)" in prompt


def test_user_prompt_omits_the_short_moving_average_when_unavailable():
    """0을 적으면 그 0을 근거로 삼는다 — 못 구한 값은 줄에서 통째로 뺀다."""
    data = _daily(moving_average=207_726.0, short_moving_average=0.0,
                  recent_high=220_000.0, recent_low=194_300.0)

    prompt = build_user_prompt([data], target_count=2)

    assert "5일 이평" not in prompt
    assert "이동평균 207,726" in prompt  # 20일 이평은 그대로 남는다


def test_locked_prompt_carries_the_trend_section():
    """튜너가 지울 수 없는 자리에 있어야 한다 — judgment_criteria는 편집 가능한 절이다."""
    locked = build_locked_prompt_text(target_count=2)

    assert "## 추세 판단" in locked
    assert "5일 이평이 20일 이평보다 낮고" in locked
```

`_daily` 헬퍼가 없으면 기존 파일의 `DailyStockData(...)` 생성 패턴을 그대로 쓴다.

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_llm/test_recommender.py -k "short_moving_average or trend_section" -v
```
Expected: FAIL (3건)

- [ ] **Step 3: 후보 줄에 추세를 더한다**

`build_user_prompt`의 `band` 블록 바로 아래에 넣는다:

```python
        # 20일 이평만 주면 "지금 이평 아래"는 알아도 "이평이 내려가는 중"은 알 수 없다.
        # 해석("하락 추세")은 코드가 붙이지 않고 비율만 준다 — 라벨을 붙이면 경계값 하나가
        # 판정을 가르고, 그 라벨 자체가 근거로 쓰인다 (2026-10-01)
        trend = ""
        if d.short_moving_average > 0 and d.moving_average > 0:
            gap = (d.short_moving_average - d.moving_average) / d.moving_average * 100
            trend = f"5일 이평 {d.short_moving_average:,.0f} (20일 대비 {gap:+.2f}%), "
```

그리고 줄 조립에서 `f"{band}"` 다음에 `f"{trend}"`를 끼운다.

- [ ] **Step 4: 잠긴 절을 더한다**

`build_locked_prompt_text`의 반환 문자열에서 `## 추천 유형 (setup)` **앞**에 넣는다:

```
## 추세 판단
`5일 이평`이 함께 주어진 종목은 그 값과 `20일 대비` 비율로 **추세 방향**을 판단하십시오.
낙폭이 크다는 것과 떨어지는 중이라는 것은 다릅니다 — 이 전략이 노리는 것은 **눌렸다가
되돌리는 종목**이지, 계속 내려가는 종목이 아닙니다.
**5일 이평이 20일 이평보다 낮고 그 격차가 벌어지는 방향이면 하락 추세로 보고 순위를
낮추십시오.** "이평 대비 더 많이 떨어졌다"는 그 자체로는 매력이 아닙니다 — 추세가 꺾인
종목은 떨어질수록 더 싸 보이지만 되돌림은 오지 않습니다.
`5일 이평`이 없는 종목은 이 기준을 적용하지 말고 나머지 기준으로만 평가하십시오.
```

함수 docstring의 "고칠 수 없는 세 절(역할·절대 규칙·추천 유형)"을 "고칠 수 없는 네 절(역할·절대 규칙·추세 판단·추천 유형)"로 고치고, 142행 주석의 "잠긴 절(역할·절대 규칙·추천 유형)"도 같이 고친다.

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_llm/test_recommender.py -v
```
Expected: 새 3건 포함 전부 PASS

- [ ] **Step 6: 전체 테스트**

```
.venv\Scripts\python.exe -m pytest -q
```

- [ ] **Step 7: 커밋**

```bash
git add src/llm/recommender.py tests/test_llm/test_recommender.py
git commit -m "추세 방향을 후보 줄과 잠긴 절에 싣는다"
```

---

### Task 3: 뉴스 검증 모듈

**Files:**
- Create: `src/llm/news_verifier.py`
- Create: `tests/test_llm/test_news_verifier.py`

**Interfaces:**
- Consumes: `settings.llm_model`, `settings.anthropic_api_key`
- Produces:
  - `@dataclass NewsVerdict(ticker: str, checked: bool, blocking: bool, reason: str)`
  - `build_news_system_prompt() -> str`
  - `build_news_user_prompt(ticker: str, name: str) -> str`
  - `parse_news_verdict(ticker: str, raw_text: str) -> NewsVerdict`
  - `class NewsVerifier.__init__(settings)` / `.verify(items: List[Tuple[str, str]], timeout_seconds: float) -> Dict[str, NewsVerdict]` — `items`는 `(ticker, name)` 목록. 반환 dict에는 **판정이 난 종목만** 담긴다(실패한 종목은 키 자체가 없다 = 통과).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_llm/test_news_verifier.py`:

```python
import json
from types import SimpleNamespace

from src.llm.news_verifier import (
    NewsVerdict,
    NewsVerifier,
    build_news_system_prompt,
    build_news_user_prompt,
    parse_news_verdict,
)


def test_parse_reads_all_three_fields():
    raw = '{"checked": true, "blocking": true, "reason": "실적 하향"}'
    v = parse_news_verdict("259960", raw)
    assert v == NewsVerdict(ticker="259960", checked=True, blocking=True, reason="실적 하향")


def test_parse_defaults_to_not_blocking_when_the_field_is_missing():
    """형식이 어긋나도 매수를 막는 쪽으로 기울지 않는다 — 기본은 통과다."""
    raw = '{"checked": true, "reason": "판단 불가"}'
    v = parse_news_verdict("259960", raw)
    assert v.blocking is False


def test_parse_treats_a_non_boolean_checked_as_unchecked():
    """max_uses에 걸려 확인을 못 했는데 '특이사항 없음'으로 답한 사례가 실측됐다."""
    raw = '{"checked": "maybe", "blocking": false, "reason": "검색 제한"}'
    v = parse_news_verdict("259960", raw)
    assert v.checked is False


def test_system_prompt_says_a_falling_price_is_not_bad_news():
    """전부 낙폭 종목이라, 이걸 적지 않으면 추천 전원이 탈락한다."""
    system = build_news_system_prompt()
    assert "주가가 떨어지고 있다는 것 자체는 악재가 아닙니다" in system


def test_system_prompt_forbids_answering_from_memory():
    """절대 규칙 2와 같은 함정 — 검색 없이 기억으로 답하면 안 된다."""
    system = build_news_system_prompt()
    assert "검색 결과에 근거하지 않은 내용" in system
    assert "checked" in system


def test_user_prompt_names_the_stock():
    prompt = build_news_user_prompt("259960", "크래프톤")
    assert "크래프톤" in prompt and "259960" in prompt


def _verifier(responder):
    """스트리밍 호출만 가짜로 바꾼 검증기."""
    v = NewsVerifier.__new__(NewsVerifier)
    v.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-sonnet-5")

    class _Stream:
        def __init__(self, text):
            self._text = text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(())

        def get_final_message(self):
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=self._text)],
            )

    def stream(**kwargs):
        return _Stream(responder(kwargs))

    v._client = SimpleNamespace(
        with_options=lambda **_: SimpleNamespace(messages=SimpleNamespace(stream=stream))
    )
    return v


def test_verify_returns_one_verdict_per_ticker():
    def responder(kwargs):
        text = kwargs["messages"][0]["content"]
        blocking = "크래프톤" in text
        return json.dumps({"checked": True, "blocking": blocking, "reason": "r"})

    verdicts = _verifier(responder).verify(
        [("259960", "크래프톤"), ("005930", "삼성전자")], timeout_seconds=60
    )

    assert verdicts["259960"].blocking is True
    assert verdicts["005930"].blocking is False


def test_verify_drops_a_ticker_whose_call_fails():
    """실패한 종목은 키 자체가 없다 — 호출측이 '판정 없음 = 통과'로 읽는다."""
    def responder(kwargs):
        if "크래프톤" in kwargs["messages"][0]["content"]:
            raise RuntimeError("API 실패")
        return json.dumps({"checked": True, "blocking": False, "reason": "r"})

    verdicts = _verifier(responder).verify(
        [("259960", "크래프톤"), ("005930", "삼성전자")], timeout_seconds=60
    )

    assert "259960" not in verdicts
    assert "005930" in verdicts


def test_verify_returns_empty_for_no_items():
    assert _verifier(lambda k: "{}").verify([], timeout_seconds=60) == {}
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_llm/test_news_verifier.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'src.llm.news_verifier'`

- [ ] **Step 3: 모듈을 만든다**

`src/llm/news_verifier.py`:

```python
"""추천된 종목에 구체적 악재가 있는지 웹검색으로 확인한다 (PRD 5.5-B '뉴스 검증').

추천 프롬프트가 보는 입력은 가격·거래량·DART 공시 제목뿐이라, "회사가 구조적으로
나쁘다"를 알 방법이 없었다 — 크래프톤을 9/29에 사서 잃고 10/01에 더 떨어졌다는 이유로
다시 산 것이 계기다 (PRD 10절).

**추천된 종목만** 본다. 후보 25종목을 전부 보면 시간도 비용도 감당이 안 된다.
종목당 1회·병렬 호출에 `max_uses=3`·`effort=low`를 걸어 2종목 기준 30초다 — 한 번에
물으면 238초였다 (스펙 2절 실측).
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Dict, List, Optional, Tuple

import anthropic

from config.settings import Settings

logger = logging.getLogger(__name__)

MAX_TOKENS = 2000
# 검색 횟수 상한. 늘리면 느려지고(실측 5회에서 117초) 줄이면 확인을 못 한다.
MAX_SEARCHES_PER_TICKER = 3

NEWS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checked", "blocking", "reason"],
    "properties": {
        "checked": {"type": "boolean"},
        "blocking": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

_SYSTEM_PROMPT = """당신은 한국 주식 단기 매매 직전에 종목의 악재를 확인하는 조사자입니다.

## 역할
주어진 종목 하나에 대해 **웹을 검색해** 최근 2주 내에 주가에 불리한 구체적 사건이 있었는지
확인하고, 오늘 단기 매수를 피할 만한지 판단합니다.

## 악재로 보는 것
실적 하향·어닝쇼크, 신작/수주/수출 부진, 유상증자·전환사채 같은 지분 희석, 규제·제재,
소송·횡령·배임, 애널리스트 목표가 하향, 주요 고객 이탈.

## 악재로 보지 않는 것
**주가가 떨어지고 있다는 것 자체는 악재가 아닙니다.** 이 매매 전략은 최근 낙폭이 큰 종목을
의도적으로 고릅니다. "주가 하락", "약세", "조정", "외국인 매도", "차익실현 매물" 같은 수급·
가격 서술은 `blocking`의 근거가 되지 못합니다. 떨어진 **이유**가 위 '악재로 보는 것'에
해당할 때만 `blocking`입니다.
수개월 전 기사도 근거가 되지 못합니다 — 최근 2주가 기준입니다.

## 출력
- `checked` — 검색 결과를 실제로 확인했으면 true. 검색이 막혔거나 결과가 없어 판단 근거를
  얻지 못했으면 **false**입니다. 확인하지 못한 것을 "악재 없음"으로 적지 마십시오.
- `blocking` — 위 기준의 구체적 악재가 있으면 true. 애매하면 false입니다.
- `reason` — 근거를 한 문장으로. **검색 결과에 근거하지 않은 내용을 쓰지 마십시오.**
  당신의 학습 데이터에 있는 종목 평판이나 과거 기억은 근거가 아닙니다. 근거 기사가 없으면
  `checked`를 false로 두고 그 사실을 적으십시오."""


@dataclass(frozen=True)
class NewsVerdict:
    """한 종목에 대한 악재 판정."""

    ticker: str
    checked: bool   # 웹검색이 실제로 확인했는가
    blocking: bool  # 오늘 매수를 피할 만한 구체적 악재가 있는가
    reason: str


def build_news_system_prompt() -> str:
    return _SYSTEM_PROMPT


def build_news_user_prompt(ticker: str, name: str) -> str:
    return (
        f"{name}({ticker})에 대해 최근 2주 내 주가에 불리한 구체적 악재가 있는지 "
        "웹에서 확인하고 판단하세요."
    )


def parse_news_verdict(ticker: str, raw_text: str) -> NewsVerdict:
    """판정 응답을 파싱한다.

    `blocking`이 불리언 `true`가 아니면 통과로 떨어진다 — 형식 오류가 매수를 막는 쪽으로
    기울면 안 된다. `checked`도 같은 규약이라, 문자열 "maybe" 같은 값은 '확인 못 함'이다.
    """
    data = json.loads(_extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("뉴스 검증 응답이 JSON 객체가 아닙니다")
    return NewsVerdict(
        ticker=ticker,
        checked=data.get("checked") is True,
        blocking=data.get("blocking") is True,
        reason=str(data.get("reason", "")).strip(),
    )


def _extract_json(raw_text: str) -> str:
    """구조화 출력이라 보통 그대로지만, 코드펜스가 붙어 오는 경우를 한 번 벗긴다."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


class NewsVerifier:
    """추천 종목의 악재를 웹검색으로 확인한다.

    **실패한 종목은 반환 dict에 키 자체를 넣지 않는다** — 호출측이 "판정 없음 = 통과"로
    읽는다. 검증은 추가 안전장치지 관문이 아니라, 막히면 종전대로 매수해야 한다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def verify(
        self, items: List[Tuple[str, str]], timeout_seconds: float
    ) -> Dict[str, NewsVerdict]:
        """(ticker, name) 목록을 병렬로 검증한다. 판정이 난 종목만 담아 돌려준다."""
        if not items:
            return {}
        logger.info(
            "뉴스 검증 요청 (%d종목, 예산 %.0f초): %s",
            len(items), timeout_seconds, ", ".join(f"{t} {n}" for t, n in items),
        )
        # 종목마다 따로 물어야 빠르다 — 한 번에 물으면 서로의 검색이 직렬화된다 (238초 vs 30초)
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            results = pool.map(lambda item: self._verify_one(*item, timeout_seconds), items)
        verdicts = {v.ticker: v for v in results if v is not None}
        for v in verdicts.values():
            logger.info(
                "뉴스 검증: %s checked=%s blocking=%s — %s",
                v.ticker, v.checked, v.blocking, v.reason,
            )
        return verdicts

    def _verify_one(
        self, ticker: str, name: str, timeout_seconds: float
    ) -> Optional[NewsVerdict]:
        deadline = monotonic() + timeout_seconds
        try:
            # 스트리밍 + max_retries=0은 추천·매도 판단과 같은 이유다 — 재시도가 예산을
            # 배로 늘리고, 비스트리밍은 생성이 길어진 날 read timeout에 그대로 걸린다.
            with self._client.with_options(
                timeout=timeout_seconds, max_retries=0
            ).messages.stream(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_news_system_prompt(),
                messages=[{"role": "user", "content": build_news_user_prompt(ticker, name)}],
                tools=[
                    {
                        "type": "web_search_20260209",
                        "name": "web_search",
                        "max_uses": MAX_SEARCHES_PER_TICKER,
                    }
                ],
                output_config={
                    "format": {"type": "json_schema", "schema": NEWS_SCHEMA},
                    # 악재가 있나 없나는 깊은 추론이 필요한 질문이 아니다. low로 30초, 비용도 준다
                    "effort": "low",
                },
            ) as stream:
                for _ in stream:
                    if monotonic() > deadline:
                        raise TimeoutError(
                            f"뉴스 검증 예산 {timeout_seconds:.0f}초를 넘겨 중단합니다: {ticker}"
                        )
                response = stream.get_final_message()
        except Exception:
            logger.exception("뉴스 검증 호출이 실패했습니다 (%s %s) — 통과로 둡니다.", ticker, name)
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("뉴스 검증 응답이 정상 종료되지 않았습니다 (%s): %s", ticker, response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("뉴스 검증 응답에 텍스트가 없습니다 (%s).", ticker)
            return None
        try:
            return parse_news_verdict(ticker, raw_text)
        except Exception:
            logger.exception("뉴스 검증 응답 파싱 실패 (%s). 원문(앞 300자): %s", ticker, raw_text[:300])
            return None
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_llm/test_news_verifier.py -v
```
Expected: 10 passed

- [ ] **Step 5: 커밋**

```bash
git add src/llm/news_verifier.py tests/test_llm/test_news_verifier.py
git commit -m "추천 종목의 악재를 웹검색으로 확인하는 모듈을 더한다"
```

---

### Task 4: 설정 — 킬 스위치와 매수 시각

**Files:**
- Modify: `config/settings.py`
- Modify: `.env.example`
- Test: `tests/test_config/test_settings.py`

**Interfaces:**
- Consumes: 없음
- Produces: `Settings.news_verify_enabled: bool`, `Settings.buy_time_hhmm` 기본값 `"09:10"`

**스펙 추가분:** 킬 스위치(`NEWS_VERIFY_ENABLED`)는 스펙에 없지만 넣는다 — 매일 돈이 나가고 아침 critical path를 건드리는 기능이라, 재배포 없이 끌 수 있어야 한다. 손절·AI 매도 판단도 같은 이유로 `.env` 토글이 있다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_news_verify_is_on_by_default(monkeypatch):
    monkeypatch.delenv("NEWS_VERIFY_ENABLED", raising=False)
    assert Settings().news_verify_enabled is True


def test_news_verify_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("NEWS_VERIFY_ENABLED", "0")
    assert Settings().news_verify_enabled is False


def test_buy_time_defaults_to_nine_ten(monkeypatch):
    """추천 09:02 + 검증·재추천 여유 8분 (2026-10-01, 09:05에서 이동)."""
    monkeypatch.delenv("BUY_TIME", raising=False)
    assert Settings().buy_time_hhmm == "09:10"
```

`news_verify_enabled`의 불리언 변환은 **기존 `AI_EXIT_ENABLED`/`STOP_LOSS_ENABLED`와 같은 방식**을 쓴다. 먼저 확인한다: `grep -n "ai_exit_enabled" config/settings.py`

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_config/test_settings.py -k "news_verify or nine_ten" -v
```
Expected: FAIL

- [ ] **Step 3: 설정을 더한다**

`config/settings.py`에서 `ai_exit_enabled` 바로 아래에, **그 필드와 똑같은 꼴로**:

```python
    # 추천 직후 웹검색으로 악재를 확인할지 (PRD 5.5-B '뉴스 검증'). 끄면 추천이 곧바로
    # 매수 대상이 된다 — 2026-10-01 이전과 같은 동작이다. 매일 호출 비용이 나가고 아침
    # 경로를 건드리는 기능이라 재배포 없이 끌 수 있어야 해서 `.env`로 뺐다.
    news_verify_enabled: bool = field(
        default_factory=lambda: os.getenv("NEWS_VERIFY_ENABLED", "1") == "1"
    )
```

`"1" == ` 비교가 기존 두 필드와 다르면 **기존 쪽에 맞춘다.**

그리고 `buy_time_hhmm`의 기본값 `"09:08"`(또는 현재 값)을 `"09:10"`으로 바꾸고, 그 옆 주석에 이유를 남긴다:

```python
    # 2026-10-01에 09:05 → 09:10. 추천 뒤 뉴스 검증(30초)과 탈락 시 재추천 1회가 들어갈
    # 자리를 만든 것이다. 2026-08-20·21에 09:10 → 09:08 → 09:05로 당겨온 것을 되돌리는
    # 셈이라, 진입 지연이 되돌림 구간을 놓치게 하는지 지켜봐야 한다 (PRD 10절).
```

- [ ] **Step 4: `.env.example`을 고친다**

`BUY_TIME` 값을 `09:10`으로 바꾸고, `AI_EXIT_ENABLED` 근처에 한 줄 더한다:

```
# 추천 직후 웹검색으로 악재를 확인한다 (1=켬). 끄면 추천이 곧바로 매수 대상이 된다
NEWS_VERIFY_ENABLED=1
```

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_config/test_settings.py -v
```

`buy_time` 기본값을 검사하던 **기존 테스트가 깨지면** 그 테스트의 기댓값도 `09:10`으로 고친다 — 기본값 변경이 의도한 것이므로.

- [ ] **Step 6: 커밋**

```bash
git add config/settings.py .env.example tests/test_config/test_settings.py
git commit -m "뉴스 검증 토글을 더하고 매수 시각 기본값을 09:10으로 옮긴다"
```

---

### Task 5: 검증 결과를 DB에 남긴다

**Files:**
- Modify: `src/logger/trade_store.py` (`RECOMMENDATION_SCHEMA_SQL`, `RECOMMENDATION_MIGRATIONS`, `save_recommendations`)
- Test: `tests/test_logger/test_trade_store.py`

**Interfaces:**
- Consumes: `NewsVerdict` (Task 3)
- Produces: `save_recommendations(day, recommendations, prompt_version, verdicts: Optional[Dict[str, NewsVerdict]] = None)` — `verdicts`는 종목코드 → 판정. 기본값이 `None`이라 **기존 호출부가 그대로 동작한다.**

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_save_recommendations_records_the_news_verdict(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    day = date(2026, 10, 1)
    rec = _recommendation(ticker="259960", name="크래프톤")
    verdicts = {
        "259960": NewsVerdict(
            ticker="259960", checked=True, blocking=True, reason="신작 부진"
        )
    }

    store.save_recommendations(day, [rec], "v1", verdicts)

    row = store.recommendations_for(day)[0]
    assert row.news_checked is True
    assert row.news_blocking is True
    assert row.news_reason == "신작 부진"


def test_save_recommendations_leaves_the_verdict_empty_when_not_verified(tmp_path):
    """검증을 돌리지 않은 날과 '악재 없음'을 구분해야 한다."""
    store = TradeStore(tmp_path / "t.db")
    day = date(2026, 10, 1)

    store.save_recommendations(day, [_recommendation(ticker="259960")], "v1")

    row = store.recommendations_for(day)[0]
    assert row.news_checked is None
```

`_recommendation` 헬퍼와 `recommendations_for`가 돌려주는 행 타입을 먼저 확인한다 (`grep -n "def recommendations_for" -A 20 src/logger/trade_store.py`). 행 dataclass에도 세 필드를 더해야 한다.

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_logger/test_trade_store.py -k news_verdict -v
```
Expected: FAIL

- [ ] **Step 3: 스키마와 마이그레이션을 더한다**

`RECOMMENDATION_SCHEMA_SQL`의 `review TEXT` 뒤(마지막 컬럼 뒤)에 세 줄을 넣는다:

```sql
    news_checked INTEGER,
    news_blocking INTEGER,
    news_reason TEXT
```

그리고 `RECOMMENDATION_MIGRATIONS`를 채운다:

```python
# 이미 만들어진 DB에 뒤늦게 추가된 컬럼 — 2026-10-01 뉴스 검증 (PRD 5.5-B)
RECOMMENDATION_MIGRATIONS: Tuple[Tuple[str, str], ...] = (
    ("news_checked", "ALTER TABLE recommendations ADD COLUMN news_checked INTEGER"),
    ("news_blocking", "ALTER TABLE recommendations ADD COLUMN news_blocking INTEGER"),
    ("news_reason", "ALTER TABLE recommendations ADD COLUMN news_reason TEXT"),
)
```

- [ ] **Step 4: `save_recommendations`가 판정을 쓰게 한다**

시그니처에 `verdicts: Optional[Dict[str, "NewsVerdict"]] = None`을 더하고, UPSERT의 컬럼·값 목록에 세 열을 더한다. 판정이 없는 종목은 세 값 모두 `None`이다 (= 검증 안 함).

```python
            verdict = (verdicts or {}).get(r.ticker)
            news_checked = int(verdict.checked) if verdict else None
            news_blocking = int(verdict.blocking) if verdict else None
            news_reason = verdict.reason if verdict else None
```

UPSERT의 `ON CONFLICT ... DO UPDATE SET`에도 세 열을 넣는다 — 재추천으로 같은 종목이 다시 들어오면 최신 판정으로 덮어써야 한다.

`recommendations_for`가 돌려주는 행 dataclass에도 `news_checked: Optional[bool]`, `news_blocking: Optional[bool]`, `news_reason: str`를 더하고, `INTEGER`를 불리언으로 되돌린다 (`None`은 `None`으로 둔다 — `bool(None)`은 `False`라 "검증 안 함"과 "악재 없음"이 뭉개진다).

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest tests/test_logger/test_trade_store.py -v
```

- [ ] **Step 6: 기존 DB로 마이그레이션이 도는지 확인한다**

```
.venv\Scripts\python.exe -c "import shutil, sqlite3; shutil.copy('data/trades.db', 'data/_mig_test.db'); from src.logger.trade_store import TradeStore; TradeStore('data/_mig_test.db'); c=sqlite3.connect('data/_mig_test.db'); print([r[1] for r in c.execute('PRAGMA table_info(recommendations)')]); c.close()"
```
Expected: 출력 목록 끝에 `news_checked`, `news_blocking`, `news_reason`

끝나면 지운다: `Remove-Item data\_mig_test.db`

- [ ] **Step 7: 커밋**

```bash
git add src/logger/trade_store.py tests/test_logger/test_trade_store.py
git commit -m "추천 기록에 뉴스 검증 결과 세 열을 더한다"
```

---

### Task 6: 워크플로 통합 — 검증과 재추천

**Files:**
- Modify: `src/llm/recommender.py` (`recommend`에 `exclude_tickers`)
- Modify: `src/core/daily_workflow.py` (`recommend_and_notify`)
- Modify: `src/core/runtime.py` (`NewsVerifier` 주입)
- Test: `tests/test_core/test_daily_workflow.py`, `tests/test_llm/test_recommender.py`

**Interfaces:**
- Consumes: `NewsVerifier.verify` (Task 3), `settings.news_verify_enabled` (Task 4), `save_recommendations(..., verdicts)` (Task 5)
- Produces: `Recommender.recommend(daily_data, timeout_seconds=None, exclude_tickers: Iterable[str] = ())`

- [ ] **Step 1: 제외 종목 테스트를 쓴다**

```python
def test_user_prompt_lists_the_excluded_tickers():
    """재추천에서 탈락 종목을 다시 고르면 루프가 된다 — 프롬프트가 알아야 한다."""
    prompt = build_user_prompt([_daily(ticker="005930")], target_count=2,
                               exclude_tickers=("259960", "241560"))
    assert "259960" in prompt and "241560" in prompt
    assert "다시 추천하지 마십시오" in prompt
```

- [ ] **Step 2: 실패 확인 → `build_user_prompt`에 인자를 더한다**

```python
def build_user_prompt(
    daily_data: List[DailyStockData],
    target_count: int = 3,
    exclude_tickers: Iterable[str] = (),
) -> str:
```

끝의 마무리 문장 **앞**에 넣는다:

```python
    excluded = tuple(exclude_tickers)
    if excluded:
        # 재추천이다. 이유를 적지 않으면 "왜 빼라는지" 모른 채 비슷한 종목을 다시 고른다
        lines.append(
            f"\n**아래 종목은 장중 확인 결과 악재가 있어 제외됐습니다. 다시 추천하지 마십시오: "
            f"{', '.join(excluded)}**"
        )
```

`recommend`에도 `exclude_tickers: Iterable[str] = ()`를 더해 `build_user_prompt`로 넘긴다.

- [ ] **Step 3: 워크플로 테스트를 쓴다**

`tests/test_core/test_daily_workflow.py`. 기존 `build_workflow` 헬퍼에 가짜 검증기를 붙인다 (`grep -n "def build_workflow" -A 40 tests/test_core/test_daily_workflow.py`로 구조 확인).

```python
class FakeVerifier:
    def __init__(self):
        self.calls = []
        self.verdicts = {}

    def verify(self, items, timeout_seconds):
        self.calls.append([t for t, _ in items])
        return {t: v for t, v in self.verdicts.items() if t in dict(items)}


def test_clean_verdicts_do_not_trigger_a_second_recommendation(tmp_path):
    workflow = build_workflow(tmp_path)
    workflow.news_verifier = FakeVerifier()
    workflow.recommender.results = [[_rec("005930"), _rec("000660")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert len(workflow.recommender.calls) == 1
    assert [r.ticker for r in workflow.strategy.recommendations] == ["005930", "000660"]


def test_a_blocking_verdict_drops_the_stock_and_recommends_once_more(tmp_path):
    workflow = build_workflow(tmp_path)
    verifier = FakeVerifier()
    verifier.verdicts = {
        "005930": NewsVerdict("005930", checked=True, blocking=True, reason="유상증자")
    }
    workflow.news_verifier = verifier
    workflow.recommender.results = [[_rec("005930"), _rec("000660")], [_rec("035420")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert len(workflow.recommender.calls) == 2
    assert workflow.recommender.calls[1]["exclude_tickers"] == ("005930",)
    assert sorted(r.ticker for r in workflow.strategy.recommendations) == ["000660", "035420"]


def test_the_second_round_is_the_last_one(tmp_path):
    """재추천 결과가 또 탈락해도 세 번째는 없다 — 시간이 폭발한다."""
    workflow = build_workflow(tmp_path)
    verifier = FakeVerifier()
    verifier.verdicts = {
        "005930": NewsVerdict("005930", checked=True, blocking=True, reason="x"),
        "035420": NewsVerdict("035420", checked=True, blocking=True, reason="y"),
    }
    workflow.news_verifier = verifier
    workflow.recommender.results = [[_rec("005930"), _rec("000660")], [_rec("035420")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert len(workflow.recommender.calls) == 2
    assert [r.ticker for r in workflow.strategy.recommendations] == ["000660"]


def test_verification_failure_passes_everything_through(tmp_path):
    """판정이 안 난 종목은 통과다 — 검증은 관문이 아니다."""
    workflow = build_workflow(tmp_path)

    class Boom:
        def verify(self, items, timeout_seconds):
            raise RuntimeError("API 실패")

    workflow.news_verifier = Boom()
    workflow.recommender.results = [[_rec("005930"), _rec("000660")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert len(workflow.recommender.calls) == 1
    assert len(workflow.strategy.recommendations) == 2


def test_verification_is_skipped_when_disabled(tmp_path):
    workflow = build_workflow(tmp_path)
    verifier = FakeVerifier()
    workflow.news_verifier = verifier
    workflow.news_verify_enabled = False
    workflow.recommender.results = [[_rec("005930")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert verifier.calls == []


def test_all_blocked_skips_the_day(tmp_path):
    """1차·2차 모두 전원 탈락이면 살 것이 없다 — 매수를 건너뛴다."""
    workflow = build_workflow(tmp_path)
    verifier = FakeVerifier()
    verifier.verdicts = {
        "005930": NewsVerdict("005930", checked=True, blocking=True, reason="x"),
        "035420": NewsVerdict("035420", checked=True, blocking=True, reason="y"),
    }
    workflow.news_verifier = verifier
    workflow.recommender.results = [[_rec("005930")], [_rec("035420")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert workflow.strategy.recommendations == []
```

기존 가짜 recommender가 `results` 큐와 `calls` 기록을 갖도록 고친다 — 지금은 한 번만 돌려주는 꼴일 수 있다. `calls`에는 `recommend`가 받은 kwargs를 담아야 `exclude_tickers` 검사가 된다.

**`build_workflow`가 `buy_time`을 넣어 줘야 한다.** 안 그러면 `_verify_news`가 "예산을 모름"으로 보고 전부 건너뛰어, 위 테스트 대부분이 **검증이 안 돌았는데 통과한 것처럼 보인다.**

그리고 `_verify_news`는 실제 남은 시간을 보므로, **테스트를 09:10 이후에 돌리면 조용히 건너뛴다.** 시계에 의존하는 테스트는 만들지 않는다 — `MIN_NEWS_VERIFY_SECONDS`를 모듈 상수로 두고 테스트에서 `monkeypatch.setattr`로 음수(예: `-10**9`)로 낮춰, 언제 돌려도 검증 경로가 타게 한다. 건너뛰는 쪽은 별도 테스트로 명시한다:

```python
def test_verification_is_skipped_when_the_buy_time_is_too_close(tmp_path, monkeypatch):
    """매수까지 시간이 없으면 반쯤 하다 마는 것보다 안 하는 것이 낫다."""
    monkeypatch.setattr(daily_workflow, "MIN_NEWS_VERIFY_SECONDS", 10**9)
    workflow = build_workflow(tmp_path)
    verifier = FakeVerifier()
    workflow.news_verifier = verifier
    workflow.recommender.results = [[_rec("005930")]]

    workflow.recommend_and_notify(date(2026, 10, 1))

    assert verifier.calls == []
    assert len(workflow.strategy.recommendations) == 1  # 건너뛰어도 매수는 간다
```

- [ ] **Step 4: 실패 확인**

```
.venv\Scripts\python.exe -m pytest tests/test_core/test_daily_workflow.py -k "verdict or recommendation or verification or blocked" -v
```

- [ ] **Step 5: `recommend_and_notify`를 고친다**

`recommendations = self.recommender.recommend(daily_data)` 아래, `self.strategy.set_recommendations(...)` **앞**에 검증 단계를 넣는다:

```python
        verdicts = self._verify_news(recommendations)
        blocked = [r for r in recommendations if self._is_blocked(r.ticker, verdicts)]
        if blocked:
            # 탈락분을 빼고 **한 번만** 다시 추천받는다. 두 번째 재추천은 시간이 폭발하고,
            # 세 번째 추천은 이미 두 번 걸러낸 뒤라 후보 질도 떨어진다 (스펙 4.1).
            kept = [r for r in recommendations if r not in blocked]
            excluded = tuple(r.ticker for r in blocked)
            logger.info(
                "뉴스 검증에서 %d종목이 탈락했습니다: %s — 제외하고 한 번 더 추천받습니다.",
                len(blocked), ", ".join(excluded),
            )
            replacements = self.recommender.recommend(daily_data, exclude_tickers=excluded) or []
            replacement_verdicts = self._verify_news(replacements)
            verdicts.update(replacement_verdicts)
            kept += [r for r in replacements if not self._is_blocked(r.ticker, replacement_verdicts)]
            recommendations = kept

        if not recommendations:
            logger.error("뉴스 검증에서 추천 종목이 전부 탈락했습니다 — 오늘 매수를 스킵합니다.")
            self.engine.notify("[경고] 추천 종목이 모두 악재로 탈락 — 오늘 매수를 스킵합니다.")
            self._save_recommendations(today, [], verdicts)
            return
```

`_save_recommendations` 호출에 `verdicts`를 넘기고, 메일 호출에도 넘긴다(Task 7).

새 헬퍼 둘을 더한다:

```python
    def _verify_news(self, recommendations) -> Dict[str, "NewsVerdict"]:
        """추천 종목의 악재를 확인한다. 어떤 실패도 빈 dict로 떨어진다 (= 전원 통과).

        검증은 추가 안전장치지 관문이 아니다 — 막으면 그날 매매가 통째로 빠지고,
        통과시키면 2026-10-01 이전과 같은 상태일 뿐이다.
        """
        if not self.news_verify_enabled or self.news_verifier is None or not recommendations:
            return {}
        if self.buy_time is None:
            return {}
        # `budget_seconds`를 쓰지 않는다 — 그쪽은 바닥이 MIN_BUDGET_SECONDS(120초)라
        # 매수 시각이 이미 지났어도 120을 돌려준다. 추천 호출에는 그 바닥이 맞지만
        # (늦더라도 추천은 나와야 한다), 검증은 늦으면 **안 하는 것이 맞다.**
        buy_at = datetime.combine(date.today(), self.buy_time)
        remaining = (buy_at - datetime.now()).total_seconds()
        if remaining < MIN_NEWS_VERIFY_SECONDS:
            logger.warning(
                "매수까지 %.0f초뿐이라 뉴스 검증을 건너뜁니다 (최소 %d초).",
                remaining, MIN_NEWS_VERIFY_SECONDS,
            )
            return {}
        budget = remaining
        try:
            return self.news_verifier.verify(
                [(r.ticker, r.name) for r in recommendations], timeout_seconds=budget
            )
        except Exception:
            logger.exception("뉴스 검증이 실패했습니다 — 추천을 그대로 씁니다.")
            return {}

    @staticmethod
    def _is_blocked(ticker: str, verdicts: Dict[str, "NewsVerdict"]) -> bool:
        """판정이 없으면 통과다 — 호출 실패·형식 오류가 매수를 막으면 안 된다."""
        verdict = verdicts.get(ticker)
        return bool(verdict and verdict.blocking)
```

모듈 상수를 파일 위쪽에 더한다:

```python
# 이보다 적게 남았으면 뉴스 검증을 건너뛴다 — 반쯤 하다 마는 것보다 안 하는 것이 낫다.
# 2종목 병렬 실측이 30초이고 꼬리가 120초다 (스펙 2절).
MIN_NEWS_VERIFY_SECONDS = 60
```

`DailyWorkflow.__init__`에 세 인자를 더한다 — 전부 기본값이 있어 기존 호출부가 깨지지 않는다:

```python
        news_verifier=None,
        news_verify_enabled: bool = True,
        buy_time: Optional[dt_time] = None,
```

**`DailyWorkflow`에는 `self.settings`가 없다** (생성자가 받는 것은 모듈과 스칼라뿐이다). 그래서 `buy_time`을 스칼라로 주입한다 — `tune_skip_monthly_return_ratio`·`buy_price_tolerance_ratio`와 같은 꼴이다. `buy_time`이 `None`이면 예산을 모르므로 검증을 건너뛴다(= 통과).

`from datetime import time as dt_time` import가 필요하다 (`recommender.py:47`이 같은 별칭을 쓴다).

- [ ] **Step 6: `runtime.py`에서 주입한다**

`build_runtime`에서 `Recommender`를 만드는 곳 근처에 `NewsVerifier(settings)`를 만들고, `DailyWorkflow(...)`에 세 인자를 넘긴다:

```python
        news_verifier=NewsVerifier(settings),
        news_verify_enabled=settings.news_verify_enabled,
        buy_time=settings.buy_time,
```

`ai_exit_drawdown_ratio=settings.ai_exit_drawdown_ratio`를 넘기는 자리(214행)가 같은 꼴이다.

`runtime.py`에는 `anthropic_api_key` 분기가 없다 — `LLMRecommender`·`tuner` 등을 조건 없이 만든다. `NewsVerifier`도 같게 조건 없이 만든다. 키가 없으면 추천이 이미 실패하므로 검증까지 갈 일이 없다.

- [ ] **Step 7: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest -q
```
Expected: 전부 통과

- [ ] **Step 8: 커밋**

```bash
git add src/llm/recommender.py src/core/daily_workflow.py src/core/runtime.py tests/
git commit -m "악재로 판정된 추천 종목을 빼고 한 번 다시 추천받는다"
```

---

### Task 7: 추천 메일에 검증 결과

**Files:**
- Modify: `src/notification/templates.py` (`recommendation_email`)
- Modify: `src/core/daily_workflow.py` (메일 호출부)
- Test: `tests/test_notification/test_templates.py`

**Interfaces:**
- Consumes: `Dict[str, NewsVerdict]` (Task 3)
- Produces: `recommendation_email(recommendations, today, investable_ratio, target_stock_count, verdicts=None, blocked=())` — 뒤 둘은 기본값이 있어 기존 호출부가 그대로 동작한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_recommendation_email_shows_the_blocked_stocks():
    blocked = [NewsVerdict("259960", checked=True, blocking=True, reason="신작 부진")]
    subject, body = recommendation_email(
        [_rec("005930", "삼성전자")], date(2026, 10, 1), 0.5, 2, blocked=blocked
    )
    assert "악재로 제외된 종목" in body
    assert "259960" in body and "신작 부진" in body


def test_recommendation_email_flags_an_unverified_stock():
    """'확인 못 함'과 '악재 없음'을 메일에서도 갈라야 한다."""
    verdicts = {"005930": NewsVerdict("005930", checked=False, blocking=False, reason="검색 실패")}
    _, body = recommendation_email(
        [_rec("005930", "삼성전자")], date(2026, 10, 1), 0.5, 2, verdicts=verdicts
    )
    assert "악재 확인 못 함" in body


def test_recommendation_email_is_unchanged_without_verdicts():
    """검증을 끈 날에는 메일 모양이 종전과 같아야 한다."""
    _, body = recommendation_email([_rec("005930", "삼성전자")], date(2026, 10, 1), 0.5, 2)
    assert "악재" not in body
```

- [ ] **Step 2: 실패 확인 → 템플릿을 고친다**

시그니처에 `verdicts: Optional[Dict[str, "NewsVerdict"]] = None`, `blocked: Sequence["NewsVerdict"] = ()`를 더한다.

종목 블록 안, `추천 근거` 줄 **앞**에 한 줄 넣는다:

```python
        verdict = (verdicts or {}).get(r.ticker)
        if verdict and not verdict.checked:
            lines.append(f"   ⚠ 악재 확인 못 함: {verdict.reason}")
```

본문 끝의 안내 문장 **앞**에 탈락 블록을 넣는다:

```python
    if blocked:
        lines.append("■ 악재로 제외된 종목")
        for v in blocked:
            lines.append(f"   {v.ticker}: {v.reason}")
        lines.append("")
```

본문의 `"※ 09:08에 위 목표 매수가로..."` 문구에 박힌 시각을 **설정값과 어긋나지 않게** 고친다 — 지금은 09:08로 하드코딩돼 있는데 기본값이 09:10으로 바뀐다. 가장 단순한 수정은 그 줄에서 시각을 빼는 것이다:

```python
    lines.append("※ 매수 시각에 위 목표 매수가로 지정가 주문을 넣고, 10:10까지 체결되지 않으면 취소합니다.")
```

- [ ] **Step 3: 워크플로의 메일 호출에 넘긴다**

`recommend_and_notify`에서:

```python
        blocked_verdicts = [v for v in verdicts.values() if v.blocking]
        subject, body = templates.recommendation_email(
            recommendations, today, self.strategy.investable_ratio,
            self.strategy.target_stock_count,
            verdicts=verdicts, blocked=blocked_verdicts,
        )
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

```
.venv\Scripts\python.exe -m pytest -q
```

- [ ] **Step 5: 커밋**

```bash
git add src/notification/templates.py src/core/daily_workflow.py tests/
git commit -m "추천 메일에 악재 검증 결과와 탈락 종목을 싣는다"
```

---

### Task 8: 문서

**Files:**
- Modify: `주식자동매매_PRD.md` (5.5-B, 10절)
- Modify: `CLAUDE.md` (전략 프레임워크 절)

- [ ] **Step 1: PRD 5.5-B에 '뉴스 검증' 항목을 더한다**

'추천 유형 제한' 항목 근처에 넣는다. 담을 것: 검증 대상(추천된 종목만), 호출 모양(종목별 1회·병렬·`max_uses=3`·`effort=low`), `checked`/`blocking` 구분과 그 이유(실측 실패 모드), **실패는 전부 통과**, 재추천 1회 제한, `MIN_NEWS_VERIFY_SECONDS=60`, `NEWS_VERIFY_ENABLED` 토글, `BUY_TIME` 09:05 → 09:10과 그 대가.

- [ ] **Step 2: PRD 5.5-B에 '추세 판단' 항목을 더한다**

5일 이평을 왜 함께 주는지, 왜 코드가 거르지 않고 프롬프트에만 싣는지, 왜 `judgment_criteria`가 아니라 잠긴 절인지.

- [ ] **Step 3: PRD 10절에 결정 기록을 더한다**

제목: `**추세 필터와 뉴스 검증 (확정 2026-10-01)**`. 담을 것: 크래프톤 2건의 표(스펙 1.2), 입력에 실적·뉴스가 없다는 사실, 웹검색 실측 표(스펙 2절) **전부** — 특히 238초 → 30초와 `max_uses` 실패 모드, 받아들인 위험(진입 5분 지연, 프롬프트만으로는 LLM이 추세를 무시할 수 있음, 월 $7~12).

- [ ] **Step 4: CLAUDE.md를 고친다**

'전략 프레임워크' 절의 추천 설명에 검증 단계를 끼우고, 매수 시각 09:08 언급을 **전부** 09:10으로 고친다 (`grep -n "09:08" CLAUDE.md`). '스레드 / 이벤트 루프 구조' 절의 매수 시각 기본값도 고친다.

- [ ] **Step 5: 시각이 어긋난 곳이 남았는지 훑는다**

```
grep -rn "09:08" --include=*.py --include=*.md .
```
문서와 주석의 옛 기본값을 전부 맞춘다. 단, **과거 이력을 서술한 문장**(예: "2026-08-20에 09:10 → 09:08")은 고치지 않는다 — 그건 그때의 사실이다.

- [ ] **Step 6: 커밋**

```bash
git add 주식자동매매_PRD.md CLAUDE.md
git commit -m "추세 필터와 뉴스 검증을 문서에 반영한다"
```

---

## 마무리 확인

- [ ] `.venv\Scripts\python.exe -m pytest -q` 전부 통과
- [ ] UI가 뜨는지 확인 (설정 탭이 `BUY_TIME` 라벨을 09:10으로 보여야 한다):

```
$env:QT_QPA_PLATFORM="offscreen"; .venv\Scripts\python.exe -c "import sys; from PyQt6.QtWidgets import QApplication; from src.ui.main_window import MainWindow; app=QApplication(sys.argv); w=MainWindow(); print(w._schedule_times.text())"
```

- [ ] `git status --short`가 비어 있는지
- [ ] **엔진 재시작이 필요하다**는 것을 사용자에게 알린다 — 코드 변경이라 `data/prompt/` 파일처럼 즉시 반영되지 않는다.
