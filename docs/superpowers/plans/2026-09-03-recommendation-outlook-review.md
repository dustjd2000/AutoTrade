# 추천 종목 전망과 장 마감 후 검증 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM 추천에 종목별 "오늘 전망"(`outlook`)을 추가하고, 추천 내용을 DB에 남긴 뒤 15:35에 실제 움직임과 대조한 검증 메일을 별도로 보낸다.

**Architecture:** 아침 경로는 기존 `LLMRecommender` 스키마에 필드 하나를 더하고 추천 메일에 한 줄을 붙이는 데서 끝난다. 새로 생기는 것은 `TradeStore`의 `recommendations` 테이블, 당일 봉을 읽는 `MarketDataClient.get_today_metrics`, 사후 평가 전용 `src/llm/reviewer.py`, 그리고 이 셋을 엮는 `DailyWorkflow.review_recommendations`다. 검증 단계는 `ActionRunner`의 기존 큐를 타고 `REPORT_TIME`(15:35)에 `daily_report` 다음으로 등록된다.

**Tech Stack:** Python 3.14, SQLite(`sqlite3` 표준 라이브러리), `anthropic` SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-recommendation-outlook-review-design.md`

## Global Constraints

- 주문·청산 로직은 건드리지 않는다. `outlook`과 검증 결과는 **표시와 기록 전용**이다.
- 15:35 일일 리포트의 내용과 조기 발송 로직(`_report_mark`, `_sells_settled`, `send_final_report`)은 그대로 둔다.
- 빈 값 규약을 따른다: 문자열은 `""`, 숫자는 `0`, DB의 미검증 칸은 `NULL`이 "산출 안 됨"이다.
- 코드·식별자·커밋 메시지는 영어, 주석과 사용자에게 보이는 문자열은 한국어. 기존 파일의 주석 밀도와 서술 방식을 그대로 따른다.
- 기록 저장이 실패해도 매매 흐름(추천 메일 발송, 매수)을 막지 않는다 — 경고 로그만 남긴다.
- 테스트는 `pytest`로 돌린다. lint/format 도구와 설정 파일은 없다.
- 커밋은 각 Task 끝에서 한다. **push는 하지 않는다.**

---

### Task 1: `outlook` 필드 — 스키마·파싱·프롬프트

**Files:**
- Modify: `src/llm/recommender.py`
- Test: `tests/test_llm/test_recommender.py`

**Interfaces:**
- Consumes: 없음 (첫 작업)
- Produces: `StockRecommendation.outlook: str` (기본 `""`), `PROMPT_TEMPLATE_VERSION == "v11"`

- [ ] **Step 1: Write the failing tests**

`tests/test_llm/test_recommender.py` 끝에 붙인다. import 목록에 `RECOMMENDATION_SCHEMA`를 더한다.

```python
def test_parse_reads_outlook():
    raw = """{"recommendations": [
        {"ticker": "005930", "name": "삼성전자", "target_price": 70000,
         "target_sell_price": 71400, "reason": "전일 등락률 +2.15%",
         "setup": "rebound",
         "outlook": "오전 중 이동평균 82,300원 회복 시도, 실패 시 77,000원까지 되밀림"}
    ]}"""
    recs = parse_recommendations(raw)
    assert recs[0].outlook == "오전 중 이동평균 82,300원 회복 시도, 실패 시 77,000원까지 되밀림"


def test_parse_without_outlook_keeps_recommendation():
    """전망이 빠져도 추천 자체는 버리지 않는다 — 주문에 쓰이지 않는 값이다."""
    raw = """{"recommendations": [
        {"ticker": "005930", "name": "삼성전자", "target_price": 70000,
         "target_sell_price": 71400, "reason": "전일 등락률 +2.15%", "setup": "rebound"}
    ]}"""
    recs = parse_recommendations(raw)
    assert len(recs) == 1
    assert recs[0].outlook == ""


def test_schema_requires_outlook():
    item = RECOMMENDATION_SCHEMA["properties"]["recommendations"]["items"]
    assert "outlook" in item["properties"]
    assert "outlook" in item["required"]


def test_system_prompt_mentions_outlook_rules():
    prompt = build_system_prompt(3)
    assert "오늘 전망" in prompt
    # 오후 시간대 단정을 금지하는 문구가 살아 있어야 한다
    assert "오후" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm/test_recommender.py -k "outlook" -v`
Expected: FAIL — `RECOMMENDATION_SCHEMA` import 후에는 `AttributeError: 'StockRecommendation' object has no attribute 'outlook'`

- [ ] **Step 3: 스키마에 필드 추가**

`src/llm/recommender.py`의 `RECOMMENDATION_SCHEMA` 안, `setup` 다음에 넣는다.

```python
                    "outlook": {
                        "type": "string",
                        "description": "오늘 남은 장중 주가 움직임 전망",
                    },
```

같은 객체의 `required` 목록에 `"outlook"`을 더한다.

- [ ] **Step 4: dataclass에 필드 추가**

`StockRecommendation`의 `setup` 아래에 붙인다.

```python
    # LLM이 본 오늘의 움직임 전망 (PRD 5.5-B '오늘 전망'). 추천 메일 표시와 15:35 검증에만
    # 쓰고 주문에는 쓰지 않는다 — `target_sell_price`와 같은 방침이다. ""는 '산출 안 됨'이며,
    # 그 경우 메일에서 줄이 통째로 빠지고 검증 평가에서도 제외된다.
    outlook: str = ""
```

- [ ] **Step 5: 파싱에 필드 추가**

`parse_recommendations`의 `StockRecommendation(...)` 생성부, `setup=` 다음 줄에 넣는다.

```python
                # 스키마가 요구하지만 빠져도 추천을 버리지 않는다 — 주문에 쓰이지 않는 값이다
                outlook=item.get("outlook", ""),
```

- [ ] **Step 6: 시스템 프롬프트에 지침 추가**

`build_system_prompt`의 "## 근거 작성 지침" 절 **앞에** 아래 절을 넣는다.

```
## 오늘 전망 작성 지침
outlook은 **오늘 남은 장중에 이 종목이 어떻게 움직일 것으로 보는지**를 두 문장으로 적는
칸입니다. reason이 "왜 골랐는가"(과거)라면 outlook은 "앞으로 어떻게 될 것인가"(미래)입니다.
reason에 쓴 내용을 말만 바꿔 다시 쓰지 마십시오.

- **첫 문장 — 오전 흐름.** 당일 현재가·당일 등락률·당일 거래량을 근거로, 오전 중 어디까지
  시도할 것으로 보는지 적으십시오. '당일 지표 없음'인 종목은 이 문장을 생략하십시오.
- **둘째 문장 — 조건부 분기.** 상방과 하방을 **가격과 함께** 조건부로 적으십시오.
  ("~를 회복하면 ~까지 열려 있고, 회복하지 못하면 ~ 부근까지 되밀릴 수 있습니다")

금지 사항:
- 오후의 움직임을 단정하지 마십시오. 제공된 것은 일봉과 현재 시각의 현재가뿐이며, 장중
  시간대별 흐름을 알 수 있는 데이터는 없습니다.
- 학습 데이터의 기억(종목 평판, 과거 주가)에 의존하지 마십시오 — 절대 규칙 2와 같습니다.

예: "현재 +1.80%로 갭 상승 출발해 당일 거래량이 실려 있어 오전 중 이동평균 82,300원
회복을 시도할 것으로 봅니다. 회복하면 전일 고가 84,000원까지 열려 있고, 회복하지 못하면
최근 저가 77,000원 부근까지 되밀릴 수 있습니다."
```

- [ ] **Step 7: 프롬프트 버전을 올린다**

```python
PROMPT_TEMPLATE_VERSION = "v11"
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_llm/test_recommender.py -v`
Expected: PASS (기존 테스트 포함 전부)

- [ ] **Step 9: Commit**

```bash
git add src/llm/recommender.py tests/test_llm/test_recommender.py
git commit -m "feat: ask the LLM for an intraday outlook per recommendation"
```

---

### Task 2: 추천 메일에 전망 줄

**Files:**
- Modify: `src/notification/templates.py:26-73`
- Test: `tests/test_notification/test_templates.py`

**Interfaces:**
- Consumes: `StockRecommendation.outlook` (Task 1)
- Produces: `_outlook_line(r: StockRecommendation) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_notification/test_templates.py`의 추천 메일 테스트 근처(`target_sell_price` 테스트 옆)에 붙인다.

```python
def test_recommendation_email_shows_outlook():
    rec = StockRecommendation(
        ticker="005930",
        name="삼성전자",
        target_price=70_000,
        target_sell_price=71_400,
        reason="전일 등락률 +2.15%",
        setup="rebound",
        outlook="오전 중 이동평균 회복 시도, 실패 시 77,000원까지 되밀림",
    )
    _, body = templates.recommendation_email([rec], date(2026, 9, 3), 0.5, 3)
    assert "오늘 전망: 오전 중 이동평균 회복 시도, 실패 시 77,000원까지 되밀림" in body
    assert "※ 오늘 전망은 LLM의 참고 수치이며 주문에 사용되지 않습니다." in body


def test_recommendation_email_omits_empty_outlook():
    rec = StockRecommendation(
        ticker="005930",
        name="삼성전자",
        target_price=70_000,
        target_sell_price=71_400,
        reason="전일 등락률 +2.15%",
        setup="rebound",
    )
    _, body = templates.recommendation_email([rec], date(2026, 9, 3), 0.5, 3)
    assert "오늘 전망" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notification/test_templates.py -k outlook -v`
Expected: FAIL — "오늘 전망"이 본문에 없다

- [ ] **Step 3: 헬퍼와 본문 줄 추가**

`_sell_target_line` 아래에 헬퍼를 더한다.

```python
def _outlook_line(r: StockRecommendation) -> str:
    """추천 메일의 오늘 전망 한 줄. 산출되지 않았으면("") 빈 문자열이라 줄이 통째로 빠진다."""
    if not r.outlook:
        return ""
    return f"   오늘 전망: {r.outlook}"
```

`recommendation_email`의 종목 루프에서 추천 근거 **앞에** 넣는다 — 전망은 근거보다 짧고, 근거가 길어 줄이 접히면 그 아래 줄이 묻힌다.

```python
        outlook_line = _outlook_line(r)
        if outlook_line:
            lines.append(outlook_line)
        lines.extend([f"   추천 근거: {r.reason}", ""])
```

- [ ] **Step 4: 하단 주석 추가**

목표 매도가 주석 블록 바로 아래에 넣는다.

```python
    # 전망 줄을 한 줄도 싣지 못했으면 이 주석도 뺀다 — 메일에 없는 값을 설명하는 꼴이 된다
    if any(_outlook_line(r) for r in recommendations):
        lines.append("※ 오늘 전망은 LLM의 참고 수치이며 주문에 사용되지 않습니다.")
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_notification/test_templates.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/notification/templates.py tests/test_notification/test_templates.py
git commit -m "feat: show the intraday outlook in the recommendation email"
```

---

### Task 3: `recommendations` 테이블과 `TradeStore` 메서드

**Files:**
- Modify: `src/logger/trade_store.py`
- Test: `tests/test_logger/test_trade_store.py`

**Interfaces:**
- Consumes: `StockRecommendation` (Task 1)
- Produces:
  - `RecommendationRow` dataclass — `day: date`, `ticker: str`, `name: str`, `prompt_version: str`, `recommend_price: float`, `target_price: int`, `target_sell_price: int`, `setup: str`, `reason: str`, `outlook: str`, `actual_high: Optional[float]`, `actual_low: Optional[float]`, `actual_close: Optional[float]`, `actual_change_rate: Optional[float]`, `buy_target_hit: Optional[bool]`, `sell_target_hit: Optional[bool]`, `review: str`, 그리고 `label` 프로퍼티
  - `TradeStore.save_recommendations(day: date, recommendations: List[StockRecommendation], prompt_version: str) -> None`
  - `TradeStore.recommendations_for(day: date) -> List[RecommendationRow]`
  - `TradeStore.save_recommendation_outcome(day: date, ticker: str, actual_high: float, actual_low: float, actual_close: float, actual_change_rate: float, buy_target_hit: bool, sell_target_hit: Optional[bool]) -> None`
  - `TradeStore.save_recommendation_review(day: date, ticker: str, review: str) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_logger/test_trade_store.py` 끝에 붙인다. 기존 파일이 임시 DB를 만드는 방식(`TradeStore(db_path=tmp_path / "...")`)을 그대로 따른다. import에 `from src.llm.recommender import StockRecommendation`를 더한다.

```python
def _rec(ticker="005930", name="삼성전자", **kwargs):
    defaults = dict(
        target_price=70_000,
        target_sell_price=71_400,
        reason="전일 등락률 +2.15%",
        setup="rebound",
        outlook="오전 중 회복 시도",
        recommend_price=70_500.0,
    )
    defaults.update(kwargs)
    return StockRecommendation(ticker=ticker, name=name, **defaults)


def test_save_and_read_recommendations(tmp_path):
    store = TradeStore(db_path=tmp_path / "t.db")
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")

    rows = store.recommendations_for(day)
    assert len(rows) == 1
    row = rows[0]
    assert row.ticker == "005930"
    assert row.outlook == "오전 중 회복 시도"
    assert row.prompt_version == "v11"
    assert row.recommend_price == 70_500.0
    # 아직 검증 전이므로 실제값은 비어 있다
    assert row.actual_close is None
    assert row.buy_target_hit is None
    assert row.review == ""


def test_save_recommendations_is_idempotent_per_day_and_ticker(tmp_path):
    """① 버튼을 두 번 눌러도 행이 겹치지 않고, 나중 추천이 앞선 추천을 덮어쓴다."""
    store = TradeStore(db_path=tmp_path / "t.db")
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec(target_price=70_000)], "v11")
    store.save_recommendations(day, [_rec(target_price=68_000)], "v11")

    rows = store.recommendations_for(day)
    assert len(rows) == 1
    assert rows[0].target_price == 68_000


def test_recommendations_for_other_day_is_empty(tmp_path):
    store = TradeStore(db_path=tmp_path / "t.db")
    store.save_recommendations(date(2026, 9, 3), [_rec()], "v11")
    assert store.recommendations_for(date(2026, 9, 2)) == []


def test_save_recommendation_outcome(tmp_path):
    store = TradeStore(db_path=tmp_path / "t.db")
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")
    store.save_recommendation_outcome(
        day,
        "005930",
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
        buy_target_hit=True,
        sell_target_hit=True,
    )

    row = store.recommendations_for(day)[0]
    assert row.actual_high == 72_000.0
    assert row.actual_low == 69_500.0
    assert row.actual_close == 71_000.0
    assert row.actual_change_rate == 1.43
    assert row.buy_target_hit is True
    assert row.sell_target_hit is True


def test_save_recommendation_outcome_keeps_unknown_sell_hit_null(tmp_path):
    """목표 매도가가 산출되지 않은(0) 종목은 '미도달'이 아니라 '판정 안 함'이다."""
    store = TradeStore(db_path=tmp_path / "t.db")
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec(target_sell_price=0)], "v11")
    store.save_recommendation_outcome(
        day,
        "005930",
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
        buy_target_hit=True,
        sell_target_hit=None,
    )
    assert store.recommendations_for(day)[0].sell_target_hit is None


def test_save_recommendation_review(tmp_path):
    store = TradeStore(db_path=tmp_path / "t.db")
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")
    store.save_recommendation_review(day, "005930", "오전 회복 시도는 맞았습니다.")
    assert store.recommendations_for(day)[0].review == "오전 회복 시도는 맞았습니다."


def test_recommendations_do_not_touch_trades_table(tmp_path):
    """추천 기록은 체결 집계와 별개다 — 일일 리포트 숫자가 흔들리면 안 된다."""
    store = TradeStore(db_path=tmp_path / "t.db")
    day = date(2026, 9, 3)
    store.save_recommendations(day, [_rec()], "v11")
    assert store.daily_summary(day).trade_count == 0
```

마지막 테스트의 `trade_count`는 이 파일의 다른 테스트가 `DailySummary`에서 실제로 읽는 필드 이름으로 맞춘다.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_logger/test_trade_store.py -k recommendation -v`
Expected: FAIL — `AttributeError: 'TradeStore' object has no attribute 'save_recommendations'`

- [ ] **Step 3: 스키마 상수 추가**

`src/logger/trade_store.py`의 `SCHEMA` 아래에 더한다. `TradeStore.__init__`이 `conn.execute(SCHEMA)` 한 줄만 돌리므로 테이블마다 상수를 따로 둔다.

```python
# 추천 기록 — 추천 시각에 앞부분을 넣고, 15:35 검증이 뒷부분(actual_*, *_hit, review)을 채운다.
# trades와 달리 체결이 아니라 '무엇을 추천했고 실제로 어떻게 움직였나'를 남기는 표다.
# 미체결로 사지 못한 종목도 여기에는 남아, "목표 매수가가 현실적이었나"를 되짚는 표본이 된다.
RECOMMENDATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    prompt_version TEXT,
    recommend_price REAL,
    target_price INTEGER,
    target_sell_price INTEGER,
    setup TEXT,
    reason TEXT,
    outlook TEXT,
    actual_high REAL,
    actual_low REAL,
    actual_close REAL,
    actual_change_rate REAL,
    buy_target_hit INTEGER,
    sell_target_hit INTEGER,
    review TEXT,
    UNIQUE (day, ticker)
);
"""
```

- [ ] **Step 4: dataclass와 헬퍼 추가**

`DailyPoint` 아래에 붙인다.

```python
@dataclass
class RecommendationRow:
    """추천 한 건과 그날의 실제 결과 (PRD 5.5-B '추천 검증').

    actual_* 와 *_hit 이 None이면 아직 검증 전이거나 당일 봉 조회에 실패한 종목이다.
    review가 ""면 LLM 평가를 받지 못한 것이며, 둘 다 메일에서 해당 줄이 빠진다.
    """

    day: date
    ticker: str
    name: str
    prompt_version: str
    recommend_price: float
    target_price: int
    target_sell_price: int
    setup: str
    reason: str
    outlook: str
    actual_high: Optional[float] = None
    actual_low: Optional[float] = None
    actual_close: Optional[float] = None
    actual_change_rate: Optional[float] = None
    buy_target_hit: Optional[bool] = None
    sell_target_hit: Optional[bool] = None
    review: str = ""

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)
```

모듈 하단 헬퍼 영역(`_weighted_average` 근처)에 더한다.

```python
def _optional_bool(value) -> Optional[bool]:
    """SQLite의 0/1/NULL을 bool/None으로. NULL은 '판정하지 않음'이라 False와 구분해야 한다."""
    return None if value is None else bool(value)
```

파일 상단 import에 `from src.llm.recommender import StockRecommendation`를 더한다 (순환 참조 없음 — `recommender`는 `config.settings`와 `src.data.collector`만 본다).

- [ ] **Step 5: `__init__`에서 테이블을 만든다**

```python
        with closing(self._connect()) as conn:
            conn.execute(SCHEMA)
            conn.execute(RECOMMENDATION_SCHEMA_SQL)
            self._migrate(conn)
            conn.commit()
```

- [ ] **Step 6: 저장·조회 메서드 추가**

`TradeStore` 안, `record_fill` 위에 넣는다 (추천이 체결보다 먼저 생기는 순서다).

```python
    # ── 추천 기록 (PRD 5.5-B '추천 검증') ────────────────────
    def save_recommendations(
        self, day: date, recommendations: List[StockRecommendation], prompt_version: str
    ) -> None:
        """그날 추천 메일에 실린 종목을 남긴다.

        (day, ticker) UNIQUE로 UPSERT한다 — UI ① 버튼을 두 번 눌러도 행이 겹치지 않고,
        나중 추천이 앞선 추천을 덮어쓴다. 그날 실제로 쓰인 것이 마지막 추천이기 때문이다.
        덮어쓸 때 검증 칸(actual_*, *_hit, review)은 건드리지 않는다 — 추천을 다시 돌린
        시점에는 아직 채워져 있지 않고, 채워져 있다면 그것이 더 나중 정보다.
        """
        with closing(self._connect()) as conn:
            conn.executemany(
                """INSERT INTO recommendations
                   (day, ticker, name, prompt_version, recommend_price, target_price,
                    target_sell_price, setup, reason, outlook)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, ticker) DO UPDATE SET
                       name = excluded.name,
                       prompt_version = excluded.prompt_version,
                       recommend_price = excluded.recommend_price,
                       target_price = excluded.target_price,
                       target_sell_price = excluded.target_sell_price,
                       setup = excluded.setup,
                       reason = excluded.reason,
                       outlook = excluded.outlook""",
                [
                    (
                        day.isoformat(),
                        r.ticker,
                        r.name,
                        prompt_version,
                        r.recommend_price,
                        r.target_price,
                        r.target_sell_price,
                        r.setup,
                        r.reason,
                        r.outlook,
                    )
                    for r in recommendations
                ],
            )
            conn.commit()

    def recommendations_for(self, day: date) -> List[RecommendationRow]:
        """그날 추천 목록. 추천이 없었으면 빈 리스트."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM recommendations WHERE day = ? ORDER BY id",
                (day.isoformat(),),
            ).fetchall()
        return [
            RecommendationRow(
                day=day,
                ticker=row["ticker"],
                name=row["name"] or "",
                prompt_version=row["prompt_version"] or "",
                recommend_price=row["recommend_price"] or 0.0,
                target_price=row["target_price"] or 0,
                target_sell_price=row["target_sell_price"] or 0,
                setup=row["setup"] or "",
                reason=row["reason"] or "",
                outlook=row["outlook"] or "",
                actual_high=row["actual_high"],
                actual_low=row["actual_low"],
                actual_close=row["actual_close"],
                actual_change_rate=row["actual_change_rate"],
                buy_target_hit=_optional_bool(row["buy_target_hit"]),
                sell_target_hit=_optional_bool(row["sell_target_hit"]),
                review=row["review"] or "",
            )
            for row in rows
        ]

    def save_recommendation_outcome(
        self,
        day: date,
        ticker: str,
        actual_high: float,
        actual_low: float,
        actual_close: float,
        actual_change_rate: float,
        buy_target_hit: bool,
        sell_target_hit: Optional[bool],
    ) -> None:
        """장 마감 후 실제 움직임을 같은 행에 채운다.

        sell_target_hit이 None인 것은 목표 매도가가 산출되지 않아(0) 판정할 것이 없는
        경우다 — 0/1이 아니라 NULL로 남겨 '도달 못함'과 구분한다.
        """
        with closing(self._connect()) as conn:
            conn.execute(
                """UPDATE recommendations
                   SET actual_high = ?, actual_low = ?, actual_close = ?,
                       actual_change_rate = ?, buy_target_hit = ?, sell_target_hit = ?
                   WHERE day = ? AND ticker = ?""",
                (
                    actual_high,
                    actual_low,
                    actual_close,
                    actual_change_rate,
                    int(buy_target_hit),
                    None if sell_target_hit is None else int(sell_target_hit),
                    day.isoformat(),
                    ticker,
                ),
            )
            conn.commit()

    def save_recommendation_review(self, day: date, ticker: str, review: str) -> None:
        """LLM 평가문을 같은 행에 채운다. 평가를 받지 못한 종목은 부르지 않는다."""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE recommendations SET review = ? WHERE day = ? AND ticker = ?",
                (review, day.isoformat(), ticker),
            )
            conn.commit()
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_logger/test_trade_store.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/logger/trade_store.py tests/test_logger/test_trade_store.py
git commit -m "feat: persist daily recommendations and their outcomes"
```

---

### Task 4: 추천 시각에 저장 연결

**Files:**
- Modify: `src/core/daily_workflow.py:352-375` (`recommend_and_notify`)
- Test: `tests/test_core/test_daily_workflow.py`

**Interfaces:**
- Consumes: `TradeStore.save_recommendations` (Task 3), `PROMPT_TEMPLATE_VERSION` (Task 1)
- Produces: `DailyWorkflow._save_recommendations(today: date, recommendations) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_core/test_daily_workflow.py` 끝에 붙인다. 이 파일이 이미 쓰는 워크플로 조립 헬퍼(가짜 collector/recommender/email/trade_store)를 그대로 재사용한다. 그런 헬퍼가 없으면 이 파일이 쓰는 조립 방식을 그대로 따라 `build_workflow(tmp_path)`를 만들고 아래 테스트들이 공유한다. 가짜 recommender는 `_recommendation()` 한 건을 돌려주고, `trade_store`는 **진짜** `TradeStore(db_path=tmp_path / "t.db")`를 쓴다 (저장 결과를 그대로 읽어 확인해야 한다).

```python
def _recommendation():
    return StockRecommendation(
        ticker="005930",
        name="삼성전자",
        target_price=70_000,
        target_sell_price=71_400,
        reason="전일 등락률 +2.15%",
        setup="rebound",
        outlook="오전 중 회복 시도",
        recommend_price=70_500.0,
    )


def test_recommend_and_notify_saves_recommendations(tmp_path):
    """추천 메일에 실린 것과 같은 목록이 DB에 남는다."""
    workflow = build_workflow(tmp_path)
    workflow.recommend_and_notify(date(2026, 9, 3))

    rows = workflow.trade_store.recommendations_for(date(2026, 9, 3))
    assert [row.ticker for row in rows] == ["005930"]
    assert rows[0].prompt_version == PROMPT_TEMPLATE_VERSION
    assert rows[0].outlook == "오전 중 회복 시도"


def test_recommend_and_notify_survives_save_failure(tmp_path):
    """기록 저장이 실패해도 추천 메일은 나가고 매수 흐름이 멈추지 않는다."""
    workflow = build_workflow(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    workflow.trade_store.save_recommendations = boom
    workflow.recommend_and_notify(date(2026, 9, 3))

    assert workflow.email.sent, "추천 메일이 나가야 한다"
```

`workflow.email.sent`는 이 파일의 가짜 `EmailNotifier`가 이미 쓰는 이름에 맞춘다.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_core/test_daily_workflow.py -k "saves_recommendations or save_failure" -v`
Expected: FAIL — 저장된 행이 없다

- [ ] **Step 3: 저장 호출 추가**

`recommend_and_notify`의 메일 발송 **직전**에 넣는다.

```python
        self._save_recommendations(today, recommendations)
        subject, body = templates.recommendation_email(
```

같은 클래스에 헬퍼를 더한다.

```python
    def _save_recommendations(self, today: date, recommendations) -> None:
        """추천을 DB에 남긴다 — 15:35 검증이 읽는 유일한 출처다 (PRD 5.5-B '추천 검증').

        실패해도 삼킨다. 기록은 사후 분석용이고, 매매가 그것 때문에 멈출 이유가 없다.
        """
        try:
            self.trade_store.save_recommendations(
                today, recommendations, PROMPT_TEMPLATE_VERSION
            )
        except Exception:
            logger.exception("추천 기록 저장에 실패했습니다 — 오늘 검증 메일이 비게 됩니다.")
```

`src/core/daily_workflow.py` 상단 import에 `PROMPT_TEMPLATE_VERSION`을 더한다 (`src.llm.recommender`에서 이미 무언가를 import하고 있으면 같은 줄에 합친다).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_core/test_daily_workflow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/daily_workflow.py tests/test_core/test_daily_workflow.py
git commit -m "feat: record each day's recommendations when the email goes out"
```

---

### Task 5: `MarketDataClient.get_today_metrics`

**Files:**
- Modify: `src/api/market_data.py`
- Test: `tests/test_api/test_market_data.py`

**Interfaces:**
- Consumes: `get_ohlcv`, `_first_present`, `DAILY_CANDLE_COUNT`, `to_float` (기존)
- Produces: `TodayMetrics` dataclass — `ticker: str`, `high: float`, `low: float`, `close: float`, `change_rate: float`; `MarketDataClient.get_today_metrics(ticker: str, today: Optional[date] = None) -> Optional[TodayMetrics]`

- [ ] **Step 1: Write the failing tests**

`tests/test_api/test_market_data.py`에 붙인다. 이 파일이 이미 `get_previous_day_metrics`를 테스트하며 쓰는 가짜 클라이언트 방식(응답 봉 목록을 지정하는 방식)을 그대로 따른다. 그 헬퍼 이름이 다르면 그 이름을 쓴다.

```python
def test_get_today_metrics_reads_todays_candle():
    client = make_client(
        candles=[
            {"dt": "20260903", "high_pric": "+72000", "low_pric": "69500",
             "close_pric": "+71000"},
            {"dt": "20260902", "close_pric": "70000"},
        ]
    )
    metrics = client.get_today_metrics("005930", today=date(2026, 9, 3))
    assert metrics.high == 72_000.0
    assert metrics.low == 69_500.0
    assert metrics.close == 71_000.0
    assert metrics.change_rate == pytest.approx(1.4286, abs=1e-3)


def test_get_today_metrics_returns_none_without_todays_candle():
    """장 마감 전이거나 휴장일이라 당일 봉이 없으면 None — 0으로 채우지 않는다."""
    client = make_client(candles=[{"dt": "20260902", "close_pric": "70000"}])
    assert client.get_today_metrics("005930", today=date(2026, 9, 3)) is None


def test_get_today_metrics_without_previous_close_leaves_change_rate_zero():
    client = make_client(
        candles=[{"dt": "20260903", "high_pric": "72000", "low_pric": "69500",
                  "close_pric": "71000"}]
    )
    metrics = client.get_today_metrics("005930", today=date(2026, 9, 3))
    assert metrics.change_rate == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api/test_market_data.py -k today_metrics -v`
Expected: FAIL — `AttributeError: 'MarketDataClient' object has no attribute 'get_today_metrics'`

- [ ] **Step 3: dataclass 추가**

`PreviousDayMetrics` 아래에 넣는다.

```python
@dataclass
class TodayMetrics:
    """당일 일봉(ka10086)에서 뽑은 그날의 실제 움직임 (PRD 5.5-B '추천 검증').

    `PreviousDayMetrics`가 당일 봉을 걸러내는 것과 정반대로, 여기서는 당일 봉만 쓴다.
    장 마감(15:30) 뒤에만 부른다 — 장중에 부르면 진행 중인 값이 잡힌다.
    """

    ticker: str
    high: float
    low: float
    close: float
    change_rate: float  # 전일 종가 대비 %. 전일 봉이 없으면 0 (산출 안 됨)
```

- [ ] **Step 4: 메서드 추가**

`get_previous_day_metrics` 아래에 넣는다.

```python
    def get_today_metrics(
        self, ticker: str, today: Optional[date] = None
    ) -> Optional[TodayMetrics]:
        """당일 봉의 고가·저가·종가와 전일 종가 대비 등락률. 당일 봉이 없으면 None.

        마감 동시호가(15:20~15:30) 체결이 반영된 뒤에 불러야 종가가 확정된 값이다
        (runtime.REPORT_TIME 주석 참고).
        """
        candles = self.get_ohlcv(ticker, period="D", count=DAILY_CANDLE_COUNT)
        today_str = (today or date.today()).strftime("%Y%m%d")
        current = None
        previous = None
        for candle in candles:
            day = str(_first_present(candle, "date", "dt") or "").strip()
            if current is None:
                if day == today_str:
                    current = candle
                continue
            previous = candle
            break
        if current is None:
            logger.warning("당일 일봉을 찾지 못했습니다: %s", ticker)
            return None

        # 키움은 가격에 등락 방향 부호를 붙여 보내므로 절댓값을 취한다
        close = abs(to_float(_first_present(current, "close_pric", "cur_prc")))
        prev_close = (
            abs(to_float(_first_present(previous, "close_pric", "cur_prc")))
            if previous is not None
            else 0.0
        )
        return TodayMetrics(
            ticker=ticker,
            high=abs(to_float(_first_present(current, "high_pric"))),
            low=abs(to_float(_first_present(current, "low_pric"))),
            close=close,
            change_rate=(
                (close - prev_close) / prev_close * 100
                if close > 0 and prev_close > 0
                else 0.0
            ),
        )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_api/test_market_data.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/api/market_data.py tests/test_api/test_market_data.py
git commit -m "feat: read today's daily candle for post-close verification"
```

---

### Task 6: `src/llm/reviewer.py` — 사후 평가

**Files:**
- Create: `src/llm/reviewer.py`
- Create: `tests/test_llm/test_reviewer.py`

**Interfaces:**
- Consumes: `Settings` (`llm_model`, `anthropic_api_key`), `src.llm.recommender.MAX_TOKENS`, `src.llm.recommender._extract_json`
- Produces:
  - `REVIEW_PROMPT_TEMPLATE_VERSION = "v1"`, `REVIEW_KEY`, `REVIEW_SCHEMA`
  - `ReviewInput` dataclass — `ticker: str`, `name: str`, `outlook: str`, `target_price: int`, `target_sell_price: int`, `recommend_price: float`, `actual_high: float`, `actual_low: float`, `actual_close: float`, `actual_change_rate: float`
  - `build_review_system_prompt() -> str`
  - `build_review_user_prompt(items: List[ReviewInput]) -> str`
  - `parse_reviews(raw_text: str) -> Dict[str, str]` — `{ticker: review}`
  - `LLMReviewer(settings)` / `LLMReviewer.review(items, timeout_seconds=120.0) -> Optional[Dict[str, str]]`

- [ ] **Step 1: Write the failing tests**

`tests/test_llm/test_reviewer.py`를 새로 만든다.

```python
from types import SimpleNamespace

from src.llm.reviewer import (
    LLMReviewer,
    ReviewInput,
    build_review_system_prompt,
    build_review_user_prompt,
    parse_reviews,
)


def item(ticker="005930", **kwargs):
    defaults = dict(
        name="삼성전자",
        outlook="오전 중 이동평균 82,300원 회복 시도",
        target_price=70_000,
        target_sell_price=71_400,
        recommend_price=70_500.0,
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
    )
    defaults.update(kwargs)
    return ReviewInput(ticker=ticker, **defaults)


def test_parse_reviews_maps_by_ticker():
    raw = '{"reviews": [{"ticker": "005930", "review": "오전 회복 시도는 맞았습니다."}]}'
    assert parse_reviews(raw) == {"005930": "오전 회복 시도는 맞았습니다."}


def test_parse_reviews_accepts_bare_array():
    raw = '[{"ticker": "005930", "review": "맞았습니다."}]'
    assert parse_reviews(raw) == {"005930": "맞았습니다."}


def test_parse_reviews_skips_items_without_ticker():
    """어느 종목의 평가인지 모르는 항목 하나가 나머지 평가를 잃게 하면 안 된다."""
    raw = '{"reviews": [{"review": "종목이 없다"}, {"ticker": "005930", "review": "ok"}]}'
    assert parse_reviews(raw) == {"005930": "ok"}


def test_user_prompt_includes_actual_numbers_and_outlook():
    prompt = build_review_user_prompt([item()])
    assert "005930" in prompt
    assert "72,000" in prompt                        # 실제 고가
    assert "이동평균 82,300원 회복 시도" in prompt      # 전망 원문


def test_system_prompt_forbids_advice():
    assert "조언" in build_review_system_prompt()


def test_review_returns_none_when_api_raises():
    reviewer = LLMReviewer.__new__(LLMReviewer)
    reviewer.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    reviewer._client = Boom()
    assert reviewer.review([item()]) is None


def test_review_returns_none_for_empty_items():
    reviewer = LLMReviewer.__new__(LLMReviewer)
    reviewer.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")
    reviewer._client = None
    assert reviewer.review([]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm/test_reviewer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.llm.reviewer'`

- [ ] **Step 3: 모듈 작성**

`src/llm/reviewer.py`를 만든다.

```python
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.llm.recommender import MAX_TOKENS, _extract_json

logger = logging.getLogger(__name__)

# 평가 프롬프트 버전 — 추천 프롬프트(PROMPT_TEMPLATE_VERSION)와 따로 움직인다.
REVIEW_PROMPT_TEMPLATE_VERSION = "v1"

REVIEW_KEY = "reviews"
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        REVIEW_KEY: {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "6자리 종목코드"},
                    "review": {
                        "type": "string",
                        "description": "전망과 실제 움직임을 대조한 한두 문장 평가",
                    },
                },
                "required": ["ticker", "review"],
                "additionalProperties": False,
            },
        }
    },
    "required": [REVIEW_KEY],
    "additionalProperties": False,
}


@dataclass
class ReviewInput:
    """평가 한 건의 입력 — 아침의 전망과 그날의 실제 움직임 한 쌍."""

    ticker: str
    name: str
    outlook: str
    target_price: int
    target_sell_price: int
    recommend_price: float
    actual_high: float
    actual_low: float
    actual_close: float
    actual_change_rate: float


def build_review_system_prompt() -> str:
    return """당신은 한국 주식시장(코스피) 단기 매매의 사후 검증을 담당하는 분석가입니다.

## 역할
오늘 아침에 제시된 종목별 **전망**과, 장 마감 후 확정된 **실제 움직임**을 대조해
전망이 어디까지 맞았고 어디서 어긋났는지를 종목마다 한두 문장으로 적습니다.

## 규칙
1. 오직 사용자 메시지로 제공되는 수치만 근거로 삼으십시오. 학습 데이터의 기억(종목 평판,
   과거 주가, 뉴스)에 의존하지 마십시오.
2. 반드시 **제공된 수치를 인용**하십시오. ("이동평균 82,300원 회복을 예상했으나 고가가
   72,000원에 그쳤습니다"처럼) "대체로 맞았다", "아쉬웠다" 같은 모호한 표현은 금지합니다.
3. 잘 맞았으면 맞았다고, 빗나갔으면 빗나갔다고 그대로 적으십시오. 어느 쪽으로도 포장하지
   마십시오.
4. **다음 추천에 대한 조언이나 전략 제안을 하지 마십시오.** 오늘 무슨 일이 있었는지만
   적습니다.
5. 제공된 모든 종목에 대해 하나씩 적으십시오. 종목코드를 그대로 돌려주십시오."""


def build_review_user_prompt(items: List[ReviewInput]) -> str:
    lines = [
        "오늘 아침 추천한 종목의 전망과, 장 마감 후 확정된 실제 움직임입니다.",
        "'목표 매수가'는 아침에 지정가 매수를 시도한 가격이고, '목표 매도가'는 오늘 중",
        "닿을 것으로 봤던 가격입니다.",
        f"\n## 종목 ({len(items)}종목)",
    ]
    for it in items:
        lines.append(
            f"- {it.ticker} {it.name}: "
            f"추천 시각 현재가 {it.recommend_price:,.0f}원, "
            f"목표 매수가 {it.target_price:,}원, "
            f"목표 매도가 {it.target_sell_price:,}원 → "
            f"실제 고가 {it.actual_high:,.0f}원 / 저가 {it.actual_low:,.0f}원 / "
            f"종가 {it.actual_close:,.0f}원 ({it.actual_change_rate:+.2f}%)"
        )
        lines.append(f"  아침 전망: {it.outlook}")

    lines.append("\n각 종목의 전망이 실제와 어떻게 달랐는지 한두 문장으로 평가하세요.")
    return "\n".join(lines)


def parse_reviews(raw_text: str) -> Dict[str, str]:
    """평가 응답을 {종목코드: 평가문}으로 파싱한다.

    스키마상 최상위는 {"reviews": [...]}이지만 배열만 와도 받아들인다.
    종목코드가 없는 항목은 어느 종목의 평가인지 알 수 없으므로 버린다 — 예외를 던지면
    나머지 종목의 평가까지 잃는다.
    """
    data = json.loads(_extract_json(raw_text))
    if isinstance(data, dict):
        data = data.get(REVIEW_KEY, data)
    if not isinstance(data, list):
        raise ValueError("LLM review response must be a JSON array")

    reviews: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip()
        review = str(item.get("review", "")).strip()
        if ticker and review:
            reviews[ticker] = review
    return reviews


class LLMReviewer:
    """추천 사후 평가 모듈 — 아침 추천과 같은 모델을 쓰되 프롬프트와 스키마는 따로 둔다.

    `recommender.py`에 섞지 않은 이유는, 한 파일이 추천과 사후 평가를 함께 지면
    서로 다른 주기로 움직이는 두 프롬프트가 한 버전 상수를 나눠 쓰게 되기 때문이다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def review(
        self, items: List[ReviewInput], timeout_seconds: float = 120.0
    ) -> Optional[Dict[str, str]]:
        """평가를 받아 {종목코드: 평가문}으로 돌려준다. 실패하면 None — 검증 메일은 그대로 나간다."""
        if not items:
            return None
        user_prompt = build_review_user_prompt(items)
        logger.info(
            "LLM 검증 요청 (review_prompt_version=%s, %d종목):\n%s",
            REVIEW_PROMPT_TEMPLATE_VERSION,
            len(items),
            user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_review_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            )
        except Exception:
            logger.exception("LLM 검증 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("LLM 검증 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("LLM 검증 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_reviews(raw_text)
        except Exception:
            logger.exception("LLM 검증 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm/test_reviewer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/llm/reviewer.py tests/test_llm/test_reviewer.py
git commit -m "feat: add an LLM reviewer that compares outlook against the day's move"
```

---

### Task 7: 검증 메일 템플릿

**Files:**
- Modify: `src/notification/templates.py`
- Test: `tests/test_notification/test_templates.py`

**Interfaces:**
- Consumes: `RecommendationRow` (Task 3)
- Produces: `recommendation_review_email(rows: List[RecommendationRow], today: date) -> tuple[str, str]` — (제목, 평문 본문)

- [ ] **Step 1: Write the failing tests**

import에 `RecommendationRow`를 더한다 (`src.logger.trade_store`).

```python
def _row(**kwargs):
    defaults = dict(
        day=date(2026, 9, 3),
        ticker="005930",
        name="삼성전자",
        prompt_version="v11",
        recommend_price=70_500.0,
        target_price=70_000,
        target_sell_price=71_400,
        setup="rebound",
        reason="전일 등락률 +2.15%",
        outlook="오전 중 회복 시도",
        actual_high=72_000.0,
        actual_low=69_500.0,
        actual_close=71_000.0,
        actual_change_rate=1.43,
        buy_target_hit=True,
        sell_target_hit=True,
        review="오전 회복 시도는 맞았습니다.",
    )
    defaults.update(kwargs)
    return RecommendationRow(**defaults)


def test_review_email_shows_actuals_and_hits():
    subject, body = templates.recommendation_review_email([_row()], date(2026, 9, 3))
    assert "2026-09-03 추천 검증 1종목" in subject
    assert "추천 시각가: 70,500원 → 종가: 71,000원 (+1.43%)" in body
    assert "당일 고가/저가: 72,000원 / 69,500원" in body
    assert "목표 매수가: 70,000원 — 도달" in body
    assert "목표 매도가: 71,400원 — 도달" in body
    assert "전망: 오전 중 회복 시도" in body
    assert "평가: 오전 회복 시도는 맞았습니다." in body


def test_review_email_marks_missed_targets():
    body = templates.recommendation_review_email(
        [_row(buy_target_hit=False, sell_target_hit=False)], date(2026, 9, 3)
    )[1]
    assert "목표 매수가: 70,000원 — 미도달 (매수 무산)" in body
    assert "목표 매도가: 71,400원 — 미도달" in body


def test_review_email_omits_sell_target_when_not_produced():
    body = templates.recommendation_review_email(
        [_row(target_sell_price=0, sell_target_hit=None)], date(2026, 9, 3)
    )[1]
    assert "목표 매도가" not in body


def test_review_email_marks_lookup_failure():
    body = templates.recommendation_review_email(
        [
            _row(
                actual_high=None,
                actual_low=None,
                actual_close=None,
                actual_change_rate=None,
                buy_target_hit=None,
                sell_target_hit=None,
                review="",
            )
        ],
        date(2026, 9, 3),
    )[1]
    assert "당일 봉 조회 실패" in body
    assert "당일 고가/저가" not in body
    assert "평가:" not in body


def test_review_email_omits_empty_review():
    body = templates.recommendation_review_email([_row(review="")], date(2026, 9, 3))[1]
    assert "평가:" not in body
    # 수치는 그대로 남는다
    assert "당일 고가/저가" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notification/test_templates.py -k review_email -v`
Expected: FAIL — `AttributeError: module 'src.notification.templates' has no attribute 'recommendation_review_email'`

- [ ] **Step 3: 템플릿 함수 작성**

`_outlook_line` 아래에 넣는다.

```python
def recommendation_review_email(
    rows: List[RecommendationRow], today: date
) -> tuple[str, str]:
    """15:35 추천 검증 이메일 (PRD 5.5-B '추천 검증').

    일일 리포트와 별개의 메일이다 — 리포트는 보유 종목이 전부 매도되면 15:30 이전에 조기
    발송될 수 있고, 그때는 당일 고가·저가·종가가 아직 확정되지 않는다.

    표가 없어 HTML을 함께 만들지 않는다. (제목, 평문)만 돌려준다.
    """
    subject = f"[AutoTrade] {today:%Y-%m-%d} 추천 검증 {len(rows)}종목"

    lines = [f"{today:%Y-%m-%d} 추천 종목의 실제 움직임입니다.", ""]
    for i, row in enumerate(rows, start=1):
        lines.append(f"{i}. {row.label}")
        if row.actual_close is None:
            lines.extend(["   당일 봉 조회 실패 — 실제 움직임을 확인하지 못했습니다.", ""])
            continue
        lines.append(
            f"   추천 시각가: {row.recommend_price:,.0f}원 → "
            f"종가: {row.actual_close:,.0f}원 ({row.actual_change_rate:+.2f}%)"
        )
        lines.append(f"   당일 고가/저가: {row.actual_high:,.0f}원 / {row.actual_low:,.0f}원")
        lines.append(
            f"   목표 매수가: {row.target_price:,}원 — "
            + ("도달" if row.buy_target_hit else "미도달 (매수 무산)")
        )
        if row.target_sell_price > 0 and row.sell_target_hit is not None:
            lines.append(
                f"   목표 매도가: {row.target_sell_price:,}원 — "
                + ("도달" if row.sell_target_hit else "미도달")
            )
        if row.outlook:
            lines.append(f"   전망: {row.outlook}")
        if row.review:
            lines.append(f"   평가: {row.review}")
        lines.append("")

    lines.append("※ 목표 매수가·매도가와 전망은 참고 수치이며 주문에 사용되지 않습니다.")
    lines.append("※ 목표 매수가 '도달'은 당일 저가가 그 가격까지 내려왔다는 뜻이며, 실제 매수")
    lines.append("   여부는 09:08 갭 판정과 10:10 미체결 취소가 따로 정합니다.")
    return subject, "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_notification/test_templates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/notification/templates.py tests/test_notification/test_templates.py
git commit -m "feat: add the recommendation verification email template"
```

---

### Task 8: `review_recommendations` 단계와 스케줄 등록

**Files:**
- Modify: `src/core/daily_workflow.py`, `src/core/actions.py:64-68,155-160`, `src/core/runtime.py:130-160,210-217`
- Test: `tests/test_core/test_daily_workflow.py`, `tests/test_core/test_actions.py`

**Interfaces:**
- Consumes: `TradeStore.recommendations_for` / `save_recommendation_outcome` / `save_recommendation_review` (Task 3), `MarketDataClient.get_today_metrics` (Task 5), `LLMReviewer` + `ReviewInput` (Task 6), `templates.recommendation_review_email` (Task 7)
- Produces: `DailyWorkflow.review_recommendations(today: Optional[date] = None) -> None`; `SCHEDULED_ACTIONS["review_recommendations"]`

- [ ] **Step 1: Write the failing tests**

`tests/test_core/test_daily_workflow.py`에 붙인다. `build_workflow` 헬퍼의 가짜 `market_data`에 아래를 더한다.

```python
    def get_today_metrics(self, ticker, today=None):
        return self.today_metrics.get(ticker)
```

가짜 reviewer는 `review(self, items, timeout_seconds=120.0)`가 `self.result`를 돌려준다. `build_workflow`는 그것을 `reviewer=` 인자로 넘긴다.

```python
def _metrics():
    return TodayMetrics(
        ticker="005930", high=72_000.0, low=69_500.0, close=71_000.0, change_rate=1.43
    )


def test_review_recommendations_saves_outcome_and_sends_mail(tmp_path):
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}
    workflow.reviewer.result = {"005930": "오전 회복 시도는 맞았습니다."}

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close == 71_000.0
    assert row.buy_target_hit is True     # 저가 69,500 <= 목표 70,000
    assert row.sell_target_hit is True    # 고가 72,000 >= 목표 71,400
    assert row.review == "오전 회복 시도는 맞았습니다."
    assert "추천 검증" in workflow.email.sent[-1][0]


def test_review_recommendations_does_nothing_without_recommendations(tmp_path):
    workflow = build_workflow(tmp_path)
    workflow.review_recommendations(date(2026, 9, 3))
    assert workflow.email.sent == []


def test_review_recommendations_survives_candle_lookup_failure(tmp_path):
    """당일 봉을 못 받은 종목은 실제값을 비워 두고 나머지 흐름은 계속한다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {}   # 전부 None

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close is None
    assert "당일 봉 조회 실패" in workflow.email.sent[-1][1]


def test_review_recommendations_sends_mail_when_llm_fails(tmp_path):
    """평가 호출이 실패해도 수치까지는 저장하고 메일은 나간다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {"005930": _metrics()}
    workflow.reviewer.result = None

    workflow.review_recommendations(day)

    row = workflow.trade_store.recommendations_for(day)[0]
    assert row.actual_close == 71_000.0
    assert row.review == ""
    assert workflow.email.sent, "평가가 없어도 검증 메일은 나가야 한다"


def test_review_recommendations_skips_llm_for_unverified_stock(tmp_path):
    """실제값이 없는 종목은 대조할 것이 없으므로 평가 입력에서 빠진다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 3)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.collector.market_data.today_metrics = {}

    workflow.review_recommendations(day)

    assert workflow.reviewer.calls == [], "평가 호출 자체가 없어야 한다"
```

가짜 reviewer는 `review`가 불릴 때 `self.calls.append(items)`를 남긴다.

`tests/test_core/test_actions.py`에 단계 등록 테스트를 더한다 (이 파일의 기존 가짜 런타임 헬퍼를 그대로 쓴다).

```python
def test_review_recommendations_step_is_scheduled_only():
    assert "review_recommendations" in SCHEDULED_ACTIONS
    assert "review_recommendations" not in MANUAL_ACTIONS
    assert "review_recommendations" not in ORDER_ACTIONS


def test_review_recommendations_step_runs_off_the_loop_thread():
    runtime = FakeRuntime()
    steps = manual_steps(runtime, "review_recommendations")
    assert len(steps) == 1
    assert steps[0].touches_orders is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_core/test_daily_workflow.py -k review_recommendations -v`
Expected: FAIL — `AttributeError: 'DailyWorkflow' object has no attribute 'review_recommendations'`

- [ ] **Step 3: `DailyWorkflow`에 reviewer 주입**

생성자 시그니처의 `ws_client=None` **앞에** 더한다.

```python
        reviewer=None,
```

본문에 붙인다.

```python
        # 15:35 추천 검증의 LLM 평가 모듈 (PRD 5.5-B '추천 검증'). None이면 평가 없이
        # 수치만 저장하고 메일을 보낸다 — 평가는 읽기용이고 수치가 원본이다.
        self.reviewer = reviewer
```

- [ ] **Step 4: `review_recommendations` 구현**

`send_daily_report` 아래에 넣는다.

```python
    def review_recommendations(self, today: Optional[date] = None) -> None:
        """15:35 — 오늘 추천한 종목의 실제 움직임을 대조해 저장하고 별도 메일로 보낸다.

        추천 목록은 메모리가 아니라 DB에서 읽는다 — 엔진이 장중에 재시작돼도 그날 검증이
        빠지지 않는다. 최종 리포트와 별개의 메일인 이유는 `recommendation_review_email`
        주석 참고 (리포트는 전량 매도 시 15:30 이전에 조기 발송될 수 있다).
        """
        today = today or date.today()
        rows = self.trade_store.recommendations_for(today)
        if not rows:
            logger.info("오늘 추천 기록이 없습니다 — 검증을 건너뜁니다 (%s).", today)
            return

        self._fill_actual_moves(today, rows)
        self._fill_reviews(today, rows)

        subject, body = templates.recommendation_review_email(rows, today)
        self.email.send(subject, body)
        logger.info("추천 검증 메일 발송 (%s, %d종목)", today, len(rows))

    def _fill_actual_moves(self, today: date, rows) -> None:
        """당일 봉을 조회해 실제 움직임과 목표가 도달 여부를 저장한다 (rows도 제자리 갱신).

        조회에 실패한 종목은 실제값을 비운 채 남긴다 — 한 종목의 일시적 실패로 나머지
        종목의 검증까지 잃지 않는다 (아침 수집의 같은 규약).

        시세 클라이언트는 collector가 이미 들고 있는 것을 그대로 쓴다. 워크플로가 따로
        참조를 하나 더 들면 두 경로가 다른 클라이언트를 볼 수 있다.
        """
        for row in rows:
            try:
                metrics = self.collector.market_data.get_today_metrics(row.ticker, today)
            except Exception:
                logger.exception("당일 봉 조회 실패: %s %s", row.ticker, row.name)
                metrics = None
            if metrics is None or metrics.close <= 0:
                continue

            row.actual_high = metrics.high
            row.actual_low = metrics.low
            row.actual_close = metrics.close
            row.actual_change_rate = metrics.change_rate
            # 저가가 목표 매수가까지 내려왔으면 그 지정가는 체결될 수 있었다는 뜻이다
            row.buy_target_hit = metrics.low > 0 and metrics.low <= row.target_price
            row.sell_target_hit = (
                metrics.high >= row.target_sell_price if row.target_sell_price > 0 else None
            )
            try:
                self.trade_store.save_recommendation_outcome(
                    today,
                    row.ticker,
                    actual_high=row.actual_high,
                    actual_low=row.actual_low,
                    actual_close=row.actual_close,
                    actual_change_rate=row.actual_change_rate,
                    buy_target_hit=row.buy_target_hit,
                    sell_target_hit=row.sell_target_hit,
                )
            except Exception:
                logger.exception("추천 검증 결과 저장 실패: %s %s", row.ticker, row.name)

    def _fill_reviews(self, today: date, rows) -> None:
        """LLM 평가문을 받아 저장한다 (rows도 제자리 갱신). 실패해도 메일 발송을 막지 않는다."""
        if self.reviewer is None:
            return
        items = [
            reviewer_module.ReviewInput(
                ticker=row.ticker,
                name=row.name,
                outlook=row.outlook,
                target_price=row.target_price,
                target_sell_price=row.target_sell_price,
                recommend_price=row.recommend_price,
                actual_high=row.actual_high,
                actual_low=row.actual_low,
                actual_close=row.actual_close,
                actual_change_rate=row.actual_change_rate,
            )
            # 실제값이 없는 종목은 대조할 것이 없다. 전망이 없는 종목도 평가 대상이 아니다.
            for row in rows
            if row.actual_close is not None and row.outlook
        ]
        if not items:
            return

        reviews = self.reviewer.review(items)
        if not reviews:
            logger.warning("LLM 검증 평가를 받지 못했습니다 — 수치만으로 메일을 보냅니다.")
            return

        for row in rows:
            review = reviews.get(row.ticker, "")
            if not review:
                continue
            row.review = review
            try:
                self.trade_store.save_recommendation_review(today, row.ticker, review)
            except Exception:
                logger.exception("추천 평가문 저장 실패: %s %s", row.ticker, row.name)
```

상단 import에 더한다.

```python
from src.llm import reviewer as reviewer_module
```

- [ ] **Step 5: 액션 등록**

`src/core/actions.py`의 `SCHEDULED_ACTIONS`에 더한다.

```python
    "review_recommendations": "추천 검증 메일 (스케줄)",
```

`step_factories`의 `daily_report` 아래에 더한다.

```python
        # 일봉 조회와 LLM 호출이 걸리므로 루프 스레드를 쓰지 않는다 (touches_orders=False).
        # 주문을 내지 않으므로 실시간 감시와 직렬화할 이유도 없다.
        "review_recommendations": lambda: [
            ManualStep(
                SCHEDULED_ACTIONS["review_recommendations"],
                runtime.workflow.review_recommendations,
            )
        ],
```

- [ ] **Step 6: 스케줄과 조립 연결**

`src/core/runtime.py`의 `build_runtime`에서 `DailyWorkflow(...)` 인자에 더한다.

```python
        reviewer=LLMReviewer(settings),
```

import에 `from src.llm.reviewer import LLMReviewer`를 더한다.

스케줄 등록 튜플의 `(REPORT_TIME, "daily_report")` **다음 줄**에 더한다 — `TimeScheduler.due_jobs`가 등록 순서대로 돌려주고 `ActionRunner` 큐가 FIFO이므로 리포트가 먼저 나가고 검증이 뒤따른다.

```python
        # 리포트 다음에 등록한다 — 같은 시각의 두 잡은 등록 순서대로 큐에 들어간다
        (REPORT_TIME, "review_recommendations"),
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_core/ -v`
Expected: PASS

- [ ] **Step 8: 전체 테스트**

Run: `pytest`
Expected: PASS (전부)

- [ ] **Step 9: Commit**

```bash
git add src/core/daily_workflow.py src/core/actions.py src/core/runtime.py tests/test_core/
git commit -m "feat: verify each day's recommendations after the close and mail the result"
```

---

### Task 9: 문서 갱신

**Files:**
- Modify: `주식자동매매_PRD.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1~8의 최종 동작
- Produces: 없음 (문서)

- [ ] **Step 1: 5.5-B 3단계 표의 JSON 필드 목록 갱신**

`주식자동매매_PRD.md:123`의 "(JSON — 종목코드, 종목명, 목표 매수가, 목표 매도가, 추천 사유)"를 "(JSON — 종목코드, 종목명, 목표 매수가, 목표 매도가, 추천 사유, 오늘 전망)"으로 바꾼다.

- [ ] **Step 2: 5.5-B에 "오늘 전망" 항목 추가**

`주식자동매매_PRD.md:359`의 "목표 매도가는 받되 주문에는 쓰지 않는다" 항목 **바로 아래**에, 같은 꼴로 항목을 더한다. 담을 내용:

- LLM이 종목마다 `outlook`("오늘 남은 장중 주가 움직임 전망")을 함께 낸다
- 첫 문장은 추천 시각까지의 당일 데이터를 근거로 한 오전 흐름, 둘째 문장은 가격을 포함한 조건부 분기다
- 추천 메일 표시와 15:35 검증에만 쓰고 **주문에는 쓰지 않는다** (목표 매도가와 같은 방침, 확정 2026-09-03)
- 오후 시간대의 움직임을 단정하지 못하게 막았다 — 일봉과 추천 시각 현재가 외에 장중 시계열 데이터가 없어 근거가 없는 창작이 된다

- [ ] **Step 3: "추천 검증 (15:35)" 절 추가**

5.11(일일 리포트) 다음에 새 절을 넣는다. 담을 내용:

- **무엇을 남기나** — `data/trades.db`의 `recommendations` 테이블. 추천 시각에 종목·목표 매수가·목표 매도가·`outlook`·추천 시각 현재가·프롬프트 버전을 넣고, 15:35에 실제 고가·저가·종가·등락률·목표가 도달 여부·LLM 평가문을 같은 행에 채운다. `(day, ticker)` UNIQUE로 UPSERT라 ① 버튼을 두 번 눌러도 행이 겹치지 않는다
- **대상** — 추천 메일에 실린 종목 전부. 미체결로 사지 못한 종목과 갭 하락으로 건너뛴 종목도 포함한다. 사지 못한 종목이 "목표 매수가가 현실적이었나"의 핵심 표본이기 때문이다. 셋업 필터에 걸려 빠진 종목은 주문 경로에 들어가지 않았으므로 제외한다
- **도달 여부의 뜻** — 목표 매수가 '도달'은 당일 저가가 그 가격까지 내려왔다는 뜻이지 실제로 체결됐다는 뜻이 아니다. 실제 매수는 09:08 갭 판정과 10:10 미체결 취소가 따로 정한다
- **왜 15:35인가** — 15:30 정각에는 마감 동시호가(15:20~15:30) 체결이 일봉에 반영되지 않아 종가·고가가 틀린 값으로 저장된다. 일일 리포트를 마감 뒤로 5분 물린 것과 같은 이유다
- **왜 일일 리포트와 별개의 메일인가** — 리포트는 보유 종목이 전부 매도되면 15:30 이전에 조기 발송될 수 있고(`watch_closeout_report`), 그날은 리포트 안에 검증을 넣을 수 없다. 분리하면 리포트의 조기 발송 로직을 건드리지 않는다
- **실패 시 동작** — 당일 봉 조회에 실패한 종목은 실제값을 비운 채 남기고 나머지는 진행한다. LLM 평가가 실패하면 수치까지만 저장하고 메일은 그대로 나간다. 엔진이 15:35 전에 꺼진 날은 검증 칸이 비어 있고, 나중에 채우는 보정 기능은 없다
- **용도** — 표본을 쌓아 나중에 되짚기 위한 기록이다. `outlook`도 목표 매도가도 주문에 쓰이지 않는다 (확정 2026-09-03)

- [ ] **Step 4: 일정 표에 검증 단계 추가**

PRD의 하루 흐름/스케줄 표에 15:35 "추천 검증 메일" 행을 더한다. 같은 15:35의 일일 리포트 **다음**에 돈다는 것을 함께 적는다.

- [ ] **Step 5: `CLAUDE.md` 갱신**

"전략 프레임워크" 절에서 1호 전략 흐름을 설명하는 문단에 15:35 검증 단계 한 문장을 더한다. "스레드 / 이벤트 루프 구조" 절이 스케줄 시각을 나열하고 있으므로(`08:40/추천 시각/매수 시각/10:10/15:15/15:35`) 그 줄도 함께 확인해 어긋나지 않게 한다.

- [ ] **Step 6: Commit**

```bash
git add 주식자동매매_PRD.md CLAUDE.md
git commit -m "docs: record the intraday outlook and post-close verification rules"
```

---

## Self-Review

**Spec coverage**

| 스펙 항목 | Task |
|---|---|
| `outlook` 스키마·dataclass·파싱 | 1 |
| 프롬프트 지침(오전 흐름 + 조건부 분기 + 금지 사항), `v11` | 1 |
| 추천 메일 전망 줄과 하단 주석 | 2 |
| `recommendations` 테이블, UPSERT, 메서드 4종 | 3 |
| 추천 시각 저장(메일 직전, 실패해도 흐름 유지) | 4 |
| 당일 봉 조회 | 5 |
| `src/llm/reviewer.py`, `REVIEW_PROMPT_TEMPLATE_VERSION` | 6 |
| 검증 메일(평문, 조회 실패·평가 없음 처리) | 7 |
| 15:35 단계, `SCHEDULED_ACTIONS`, `daily_report` 다음 등록, `touches_orders=False` | 8 |
| 추천 없는 날 무동작 | 8 |
| PRD·`CLAUDE.md` 갱신 | 9 |

**범위 밖 확인** — 주문·청산 로직, `send_final_report`/`_report_mark`/`_sells_settled`, UI 버튼 추가는 어느 Task에서도 건드리지 않는다.

**타입 일관성** — `RecommendationRow`(Task 3)의 필드명이 Task 7 템플릿과 Task 8 워크플로에서 같은 이름으로 쓰인다. `TodayMetrics`(Task 5)의 `high/low/close/change_rate`가 Task 8에서 같은 이름으로 읽힌다. `ReviewInput`(Task 6)의 필드가 Task 8의 생성부와 일치한다. `LLMReviewer.review(items, timeout_seconds=120.0) -> Optional[Dict[str, str]]`가 Task 8에서 `{ticker: review}`로 소비된다. `_optional_bool`은 Task 3 안에서만 쓰인다.
