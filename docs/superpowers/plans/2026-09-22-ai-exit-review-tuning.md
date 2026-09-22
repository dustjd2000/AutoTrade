# AI 매도 판단 검증 + 매도 프롬프트 자동 수정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 매도 판단을 매일 순수익 잣대로 검증해 DB·메일로 남기고, 표본이 쌓이면 매도 프롬프트를 자동으로 고친다. 월 순수익이 기준 이상이면 추천·매도 두 프롬프트 모두 고치지 않는다.

**Architecture:** 판단·고점을 `data/trades.db`의 새 테이블에 영속화하고(재시작에도 남게), 15:35 큐에 "매도 판단 검증"과 "매도 프롬프트 수정" 두 단계를 추천 쪽 뒤에 붙인다. 추천 프롬프트 자동 수정(`tuner.py`/`PromptStore`)의 구조를 일반화해 재사용한다. 스펙: `docs/superpowers/specs/2026-09-22-ai-exit-review-tuning-design.md`.

**Tech Stack:** Python 3.14, sqlite3, anthropic SDK(structured output), PyQt6, pytest.

## Global Constraints

- 평가 잣대: **순손익(수수료·세금 차감)이 플러스로 확정됐는가.** "덜 잃음", "종가보다 나음"은 성공이 아니다.
- 어떤 기록·검증·튜닝 실패도 매매(매도·손절)를 막지 않는다 — 예외는 로그만 남기고 삼킨다.
- 튜너는 **한 번에 1절**만, 편집 가능한 절은 `loss_positions`/`time`/`criteria` 셋뿐, 본문 50자 미만은 버린다.
- 표본 게이트: 현재 매도 프롬프트 버전으로 검증된 종목(`outcome != "unknown"`)이 **10건 미만**이면 매도 튜너를 부르지 않는다.
- 월 순수익 게이트: `TUNE_SKIP_MONTHLY_RETURN_PERCENT`(기본 5, 1.0~10.0, 0.5 단위) 이상이면 **추천·매도 두 튜너 모두** LLM을 부르지 않는다. 잔고 조회 실패·분모 ≤ 0이면 고치지 않는다.
- 고점 영속화는 종목당 5초에 한 번으로 묶는다(`PEAK_FLUSH_SECONDS = 5.0`).
- 코드·식별자·로그 문자열 규약은 주변 코드 그대로(한국어 주석·로그, 영어 식별자). 커밋 메시지는 한국어 명령형 + `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Windows: 테스트는 `.venv/Scripts/python.exe -m pytest ...`(Bash) 로 돌린다. 커밋 메시지는 PowerShell 5.1에서 따옴표가 깨지므로 **Bash heredoc(`git commit -F -`)** 으로 넣는다.
- **엔진이 떠 있어도 테스트는 안전하다** (키움 API를 부르지 않는다). 진단 스크립트(`scripts/check_*.py`)는 돌리지 않는다.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `src/logger/trade_store.py` (수정) | 새 테이블 3개(`ai_exit_decisions`, `position_peaks`, `exit_reviews`)와 dataclass 2개(`AIExitDecisionRow`, `ExitReviewRow`), 읽기·쓰기 메서드 |
| `src/core/exit_drawdown.py` (수정) | `seed`(복원), `take_dirty`(영속화 대상 꺼내기) |
| `src/core/engine.py` (수정) | 고점 영속화(`flush_exit_peaks`)·복원, AI 매도 판단이 꺼져도 고점 추적 |
| `src/llm/prompt_store.py` (수정) | 절 목록·기본값·기본 버전·라벨을 생성자로 받게 일반화, `why_history()` |
| `src/llm/exit_advisor.py` (수정) | 시스템 프롬프트를 잠긴 절 + 편집 가능 절로 분리, 저장소에서 읽기, `prompt_version` |
| `src/core/runtime.py` (수정) | 판단 기록, 고점 flush, 15:35 스케줄 두 개 추가, 배선 |
| `config/settings.py` / `.env.example` / `src/ui/main_window.py` (수정) | 월 순수익 게이트 설정 |
| `src/core/exit_review.py` (신규) | 검증 행 만들기·분류 (순수 함수) |
| `src/llm/exit_reviewer.py` (신규) | 판단 평가문 LLM 모듈 |
| `src/llm/tuner.py` (수정) | `sanitize_sections`에 허용 키·최대 절 수 인자 |
| `src/llm/exit_tuner.py` (신규) | 매도 프롬프트 튜너 LLM 모듈, 버전별 집계 |
| `src/core/daily_workflow.py` (수정) | `review_exits`, `tune_exit_prompt`, 월 순수익 게이트, 리포트의 월 기준자산 헬퍼 공유 |
| `src/notification/templates.py` (수정) | `exit_review_email`, `exit_prompt_tuning_email` |
| `src/core/actions.py` (수정) | 스케줄 액션 두 개 |
| PRD / CLAUDE.md (수정) | 문서 |

---

### Task 1: 기록 테이블과 TradeStore 메서드

**Files:**
- Modify: `src/logger/trade_store.py` (스키마 상수 옆, `RecommendationRow` 아래, `TradeStore.__init__`, 클래스 끝)
- Test: `tests/test_logger/test_exit_records.py` (신규)

**Interfaces:**
- Produces:
  - `AIExitDecisionRow(day: date, at: datetime, ticker: str, name: str, sell: bool, ok: bool, reason: str, net_return: float, peak_return: Optional[float], current_price: float, exit_prompt_version: str)`
  - `ExitReviewRow(day: date, ticker: str, name: str, exit_prompt_version: str, net_pnl: Optional[float], net_return: Optional[float], peak_return: Optional[float], outcome: str, exit_reason: str, decision_count: int, review: str = "")` + `label` 프로퍼티
  - `TradeStore.save_ai_exit_decisions(rows: Iterable[AIExitDecisionRow]) -> None`
  - `TradeStore.ai_exit_decisions_for(day: date) -> List[AIExitDecisionRow]` (시각순)
  - `TradeStore.save_position_peak(day: date, ticker: str, peak_return: float, peak_at: datetime, peak_price: Optional[float]) -> None` (더 높을 때만 갱신)
  - `TradeStore.position_peaks_for(day: date) -> Dict[str, float]`
  - `TradeStore.last_exit_reasons(day: date) -> Dict[str, str]`
  - `TradeStore.save_exit_review(row: ExitReviewRow) -> None` (UPSERT)
  - `TradeStore.exit_reviews_for(day: date) -> List[ExitReviewRow]`
  - `TradeStore.recent_exit_reviews(day_count: int = 10) -> List[ExitReviewRow]`
  - `TradeStore.count_exit_reviews(exit_prompt_version: str) -> int` (`outcome != "unknown"`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_logger/test_exit_records.py`

```python
"""AI 매도 판단 검증의 기록 테이블 (스펙 2026-09-22 2·3절).

판단·고점이 메모리에만 있으면 엔진 재시작으로 사라진다 — 2026-09-22에 09:17·10:59
재시작으로 고점 추적이 초기화됐다. 여기서는 저장과 조회 계약만 본다.
"""
import sqlite3
from datetime import date, datetime

from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow, TradeStore

DAY = date(2026, 9, 22)


def decision(ticker="032830", at=datetime(2026, 9, 22, 10, 20), sell=False, ok=True, version="v2"):
    return AIExitDecisionRow(
        day=DAY,
        at=at,
        ticker=ticker,
        name="삼성생명",
        sell=sell,
        ok=ok,
        reason="손실 구간",
        net_return=-0.0274,
        peak_return=-0.0073,
        current_price=290_500.0,
        exit_prompt_version=version,
    )


def review(ticker="032830", day=DAY, version="v2", outcome="no_chance"):
    return ExitReviewRow(
        day=day,
        ticker=ticker,
        name="삼성생명",
        exit_prompt_version=version,
        net_pnl=-17_240.0,
        net_return=-0.0289,
        peak_return=-0.0073,
        outcome=outcome,
        exit_reason="day_end",
        decision_count=11,
    )


def insert_trade(store, ticker, side, timestamp, exit_reason=None, status="filled"):
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp, exit_reason)
               VALUES ('1', ?, ?, ?, 2, 2, 290000, 298000, -16000, ?, ?)""",
            (ticker, side, status, timestamp, exit_reason),
        )


def test_decisions_round_trip_in_time_order(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    late = decision(at=datetime(2026, 9, 22, 14, 31), sell=True)
    early = decision(at=datetime(2026, 9, 22, 9, 20))
    store.save_ai_exit_decisions([late, early])

    rows = store.ai_exit_decisions_for(DAY)

    assert [r.at for r in rows] == [early.at, late.at]
    assert rows[1].sell is True and rows[1].ok is True
    assert rows[0].peak_return == -0.0073
    assert rows[0].exit_prompt_version == "v2"


def test_decisions_of_another_day_are_not_returned(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_ai_exit_decisions([decision()])
    assert store.ai_exit_decisions_for(date(2026, 9, 23)) == []


def test_saving_no_decisions_is_a_no_op(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_ai_exit_decisions([])
    assert store.ai_exit_decisions_for(DAY) == []


def test_peak_only_moves_up(tmp_path):
    """재시작 직후 더 낮은 고점이 들어와도 기존 고점을 덮지 않는다."""
    store = TradeStore(tmp_path / "t.db")
    at = datetime(2026, 9, 22, 9, 10)
    store.save_position_peak(DAY, "032830", 0.017, at, 304_000.0)
    store.save_position_peak(DAY, "032830", -0.0291, at, 289_500.0)
    store.save_position_peak(DAY, "035420", 0.0002, at, 203_500.0)

    assert store.position_peaks_for(DAY) == {"032830": 0.017, "035420": 0.0002}


def test_peaks_are_per_day(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_position_peak(DAY, "032830", 0.01, datetime(2026, 9, 22, 9, 10), None)
    assert store.position_peaks_for(date(2026, 9, 23)) == {}


def test_last_exit_reason_wins_per_ticker(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    insert_trade(store, "032830", "buy", "2026-09-22T09:05:00")
    insert_trade(store, "032830", "sell", "2026-09-22T10:00:00", "ai_judgment")
    insert_trade(store, "032830", "sell", "2026-09-22T15:15:00", "day_end")
    insert_trade(store, "035420", "sell", "2026-09-22T14:00:46", "ai_judgment")
    insert_trade(store, "005930", "sell", "2026-09-22T14:00:00", "stop_loss", status="rejected")

    assert store.last_exit_reasons(DAY) == {"032830": "day_end", "035420": "ai_judgment"}


def test_exit_review_upserts_per_day_and_ticker(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_exit_review(review())
    updated = review()
    updated.review = "10:20 하방선 이탈 뒤 보유"
    store.save_exit_review(updated)

    rows = store.exit_reviews_for(DAY)
    assert len(rows) == 1
    assert rows[0].review == "10:20 하방선 이탈 뒤 보유"
    assert rows[0].outcome == "no_chance"


def test_recent_exit_reviews_are_the_last_n_days_oldest_first(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    for day in (date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)):
        store.save_exit_review(review(day=day))

    rows = store.recent_exit_reviews(day_count=2)

    assert [r.day for r in rows] == [date(2026, 9, 21), date(2026, 9, 22)]


def test_count_exit_reviews_ignores_unknown_and_other_versions(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_exit_review(review(ticker="A", version="v2"))
    store.save_exit_review(review(ticker="B", version="v2", outcome="unknown"))
    store.save_exit_review(review(ticker="C", version="20260929"))

    assert store.count_exit_reviews("v2") == 1
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_logger/test_exit_records.py -q`
Expected: FAIL — `ImportError: cannot import name 'AIExitDecisionRow'`

- [ ] **Step 3: 구현** — `src/logger/trade_store.py`

`RECOMMENDATION_MIGRATIONS` 정의 바로 아래에 스키마를 추가한다.

```python
# AI 매도 판단 한 건(종목 하나)마다 한 줄 (스펙 2026-09-22 2.1). 판단은 메모리와 로그 텍스트에만
# 남았어서 엔진이 재시작되면 사라졌다 — 15:35 매도 판단 검증의 원천이다.
AI_EXIT_DECISION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_exit_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    sell INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    reason TEXT,
    net_return REAL,
    peak_return REAL,
    current_price REAL,
    exit_prompt_version TEXT
);
"""

# 종목별 당일 순손익 고점 (스펙 2.2). DrawdownTracker가 메모리에만 들고 있어 재시작하면
# 초기화됐다(2026-09-22 11:00 판단이 실제 -0.73% 대신 -2.91%를 봤다). 엔진 시작 때 복원한다.
POSITION_PEAK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS position_peaks (
    day TEXT NOT NULL,
    ticker TEXT NOT NULL,
    peak_return REAL NOT NULL,
    peak_at TEXT,
    peak_price REAL,
    UNIQUE (day, ticker)
);
"""

# 15:35 매도 판단 검증 결과 — 종목당 한 줄 (스펙 3.4). 매도 프롬프트 튜너의 입력이다.
EXIT_REVIEW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS exit_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT,
    exit_prompt_version TEXT,
    net_pnl REAL,
    net_return REAL,
    peak_return REAL,
    outcome TEXT NOT NULL,
    exit_reason TEXT,
    decision_count INTEGER NOT NULL DEFAULT 0,
    review TEXT,
    UNIQUE (day, ticker)
);
"""
```

`RecommendationRow` 클래스 아래에 dataclass 두 개를 추가한다.

```python
@dataclass
class AIExitDecisionRow:
    """AI 매도 판단 한 건 — 종목 하나에 대한 판정과 그 시점의 수치 (스펙 2026-09-22 2.1).

    ok=False는 LLM 호출 실패·타임아웃·형식 오류로 판정을 받지 못한 주기다 (sell은 언제나 False).
    """

    day: date
    at: datetime
    ticker: str
    name: str
    sell: bool
    ok: bool
    reason: str
    net_return: float
    peak_return: Optional[float]
    current_price: float
    exit_prompt_version: str


@dataclass
class ExitReviewRow:
    """매도 판단 검증 한 줄 — 그날 매도한 종목 하나의 순손익과 분류 (스펙 2026-09-22 3.2).

    outcome은 `captured`/`missed`/`no_chance`/`unknown` 중 하나다 (`src.core.exit_review`).
    net_*가 None이면 평단을 몰라 순손익을 계산하지 못한 종목이다.
    """

    day: date
    ticker: str
    name: str
    exit_prompt_version: str
    net_pnl: Optional[float]
    net_return: Optional[float]
    peak_return: Optional[float]
    outcome: str
    exit_reason: str
    decision_count: int
    review: str = ""

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)
```

`TradeStore.__init__`의 `conn.execute(RECOMMENDATION_SCHEMA_SQL)` 다음 줄에 세 줄을 추가한다.

```python
            conn.execute(AI_EXIT_DECISION_SCHEMA_SQL)
            conn.execute(POSITION_PEAK_SCHEMA_SQL)
            conn.execute(EXIT_REVIEW_SCHEMA_SQL)
```

`has_unsettled_sells` 메서드 다음(클래스 끝)에 메서드를 추가한다.

```python
    # ── AI 매도 판단 검증 (스펙 2026-09-22) ─────────────────
    def save_ai_exit_decisions(self, rows: Iterable[AIExitDecisionRow]) -> None:
        """한 주기의 판정을 남긴다. 빈 목록이면 아무것도 하지 않는다."""
        rows = list(rows)
        if not rows:
            return
        with closing(self._connect()) as conn:
            conn.executemany(
                """INSERT INTO ai_exit_decisions
                   (day, at, ticker, name, sell, ok, reason, net_return, peak_return,
                    current_price, exit_prompt_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        r.day.isoformat(),
                        r.at.isoformat(),
                        r.ticker,
                        r.name,
                        int(r.sell),
                        int(r.ok),
                        r.reason,
                        r.net_return,
                        r.peak_return,
                        r.current_price,
                        r.exit_prompt_version,
                    )
                    for r in rows
                ],
            )
            conn.commit()

    def ai_exit_decisions_for(self, day: date) -> List[AIExitDecisionRow]:
        """그날 판정 전부 (시각순)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM ai_exit_decisions WHERE day = ? ORDER BY at, id",
                (day.isoformat(),),
            ).fetchall()
        return [
            AIExitDecisionRow(
                day=date.fromisoformat(row["day"]),
                at=datetime.fromisoformat(row["at"]),
                ticker=row["ticker"],
                name=row["name"] or "",
                sell=bool(row["sell"]),
                ok=bool(row["ok"]),
                reason=row["reason"] or "",
                net_return=row["net_return"] or 0.0,
                peak_return=row["peak_return"],
                current_price=row["current_price"] or 0.0,
                exit_prompt_version=row["exit_prompt_version"] or "",
            )
            for row in rows
        ]

    def save_position_peak(
        self,
        day: date,
        ticker: str,
        peak_return: float,
        peak_at: datetime,
        peak_price: Optional[float],
    ) -> None:
        """당일 고점을 남긴다 — 기존 값보다 **높을 때만** 갱신한다.

        재시작 직후에는 트래커가 복원 전 값이나 낮은 현재값으로 시작할 수 있어, 덮어쓰기로
        두면 기록된 고점이 내려간다.
        """
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO position_peaks (day, ticker, peak_return, peak_at, peak_price)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(day, ticker) DO UPDATE SET
                       peak_return = excluded.peak_return,
                       peak_at = excluded.peak_at,
                       peak_price = excluded.peak_price
                   WHERE excluded.peak_return > position_peaks.peak_return""",
                (day.isoformat(), ticker, peak_return, peak_at.isoformat(), peak_price),
            )
            conn.commit()

    def position_peaks_for(self, day: date) -> Dict[str, float]:
        """그날 종목별 고점 순손익률 (비율)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT ticker, peak_return FROM position_peaks WHERE day = ?",
                (day.isoformat(),),
            ).fetchall()
        return {row["ticker"]: row["peak_return"] for row in rows}

    def last_exit_reasons(self, day: date) -> Dict[str, str]:
        """그날 종목별 **마지막** 체결 매도의 청산 사유 (없으면 빈 문자열)."""
        start, end = _day_range(day)
        placeholders = ", ".join("?" for _ in FILLED_STATUSES)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT ticker, exit_reason FROM trades
                    WHERE side = ? AND status IN ({placeholders})
                          AND timestamp BETWEEN ? AND ?
                    ORDER BY timestamp, id""",
                (OrderSide.SELL.value, *FILLED_STATUSES, start, end),
            ).fetchall()
        reasons: Dict[str, str] = {}
        for row in rows:
            reasons[row["ticker"]] = row["exit_reason"] or ""
        return reasons

    def save_exit_review(self, row: ExitReviewRow) -> None:
        """검증 한 줄을 남긴다. 같은 날 같은 종목은 덮어쓴다 (검증을 다시 돌려도 한 줄)."""
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO exit_reviews
                   (day, ticker, name, exit_prompt_version, net_pnl, net_return, peak_return,
                    outcome, exit_reason, decision_count, review)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, ticker) DO UPDATE SET
                       name = excluded.name,
                       exit_prompt_version = excluded.exit_prompt_version,
                       net_pnl = excluded.net_pnl,
                       net_return = excluded.net_return,
                       peak_return = excluded.peak_return,
                       outcome = excluded.outcome,
                       exit_reason = excluded.exit_reason,
                       decision_count = excluded.decision_count,
                       review = excluded.review""",
                (
                    row.day.isoformat(),
                    row.ticker,
                    row.name,
                    row.exit_prompt_version,
                    row.net_pnl,
                    row.net_return,
                    row.peak_return,
                    row.outcome,
                    row.exit_reason,
                    row.decision_count,
                    row.review,
                ),
            )
            conn.commit()

    def exit_reviews_for(self, day: date) -> List[ExitReviewRow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM exit_reviews WHERE day = ? ORDER BY id", (day.isoformat(),)
            ).fetchall()
        return [_exit_review_row(row) for row in rows]

    def recent_exit_reviews(self, day_count: int = 10) -> List[ExitReviewRow]:
        """최근 `day_count` 거래일의 검증 (오래된 날부터) — 매도 프롬프트 튜너의 입력이다."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM exit_reviews
                   WHERE day IN (
                       SELECT DISTINCT day FROM exit_reviews ORDER BY day DESC LIMIT ?
                   )
                   ORDER BY day, id""",
                (day_count,),
            ).fetchall()
        return [_exit_review_row(row) for row in rows]

    def count_exit_reviews(self, exit_prompt_version: str) -> int:
        """그 버전으로 검증이 끝난 종목 수 — 판정 불가(unknown)는 세지 않는다 (표본 게이트)."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM exit_reviews
                   WHERE exit_prompt_version = ? AND outcome != 'unknown'""",
                (exit_prompt_version,),
            ).fetchone()
        return int(row[0])
```

파일 끝(`_recommendation_row` 다음)에 변환 함수를 추가한다.

```python
def _exit_review_row(row) -> ExitReviewRow:
    """`exit_reviews` 한 행을 dataclass로."""
    return ExitReviewRow(
        day=date.fromisoformat(row["day"]),
        ticker=row["ticker"],
        name=row["name"] or "",
        exit_prompt_version=row["exit_prompt_version"] or "",
        net_pnl=row["net_pnl"],
        net_return=row["net_return"],
        peak_return=row["peak_return"],
        outcome=row["outcome"],
        exit_reason=row["exit_reason"] or "",
        decision_count=row["decision_count"] or 0,
        review=row["review"] or "",
    )
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_logger -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/logger/trade_store.py tests/test_logger/test_exit_records.py
git commit -F - <<'EOF'
AI 매도 판단·당일 고점·매도 판단 검증을 DB에 남길 테이블을 추가한다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 2: 당일 고점 영속화·복원 (엔진)

**Files:**
- Modify: `src/core/exit_drawdown.py` (`__init__`, `update`, `clear`, 새 메서드 `seed`/`take_dirty`)
- Modify: `src/core/engine.py` (모듈 상수, `__init__`, `start`, `force_close_all_positions`, `_track_exit_drawdown`, 새 메서드 `flush_exit_peaks`/`_restore_exit_peaks`)
- Test: `tests/test_core/test_exit_drawdown.py` (추가), `tests/test_core/test_peak_persistence.py` (신규)

**Interfaces:**
- Consumes: Task 1의 `TradeStore.save_position_peak`, `TradeStore.position_peaks_for`
- Produces:
  - `DrawdownTracker.seed(peaks: Dict[str, float]) -> None`
  - `DrawdownTracker.take_dirty() -> Dict[str, float]`
  - `TradingEngine.flush_exit_peaks(positions: Optional[Dict[str, Position]] = None, force: bool = False) -> None`
  - `engine.PEAK_FLUSH_SECONDS = 5.0`

- [ ] **Step 1: 트래커 테스트 추가** — `tests/test_core/test_exit_drawdown.py` 끝에

```python
# ── 영속화·복원 (스펙 2026-09-22 2.2) ────────────────────────
def test_new_peaks_are_marked_dirty_until_taken():
    t = tracker()
    t.update({TICKER: 0.01})
    t.update({TICKER: 0.02})
    t.update({TICKER: 0.015})

    assert t.take_dirty() == {TICKER: 0.02}
    assert t.take_dirty() == {}


def test_seed_restores_a_higher_peak_only():
    t = tracker()
    t.update({TICKER: 0.01, OTHER: 0.05})
    t.seed({TICKER: 0.0446, OTHER: 0.02})

    assert t.retracement(TICKER, 0.0).peak == 0.0446
    assert t.retracement(OTHER, 0.0).peak == 0.05


def test_seeded_peak_is_armed_for_the_giveback_trigger():
    """재시작 전에 발동했어야 할 반납이면 복원 직후 한 번 앞당겨지는 것이 의도된 동작이다."""
    t = tracker()
    t.seed({TICKER: 0.0446})

    assert t.update({TICKER: 0.0141}) == [TICKER]


def test_clear_drops_dirty_peaks():
    t = tracker()
    t.update({TICKER: 0.01})
    t.clear()
    assert t.take_dirty() == {}
```

- [ ] **Step 2: 엔진 테스트 작성** — `tests/test_core/test_peak_persistence.py`

```python
"""당일 고점 영속화·복원 (스펙 2026-09-22 2.2).

2026-09-22 09:17·10:59 재시작으로 DrawdownTracker가 초기화돼, 11:00 판단이 실제 고점
-0.73% 대신 -2.91%를 봤다. 새 고점은 DB에 남기고, 엔진 시작 때 되살린다.
"""
from datetime import date, datetime, timedelta

from src.api.account import Position
from src.core.events import MarketData
from src.logger.trade_store import TradeStore

from tests.test_core.test_portfolio_exit import make_engine

TICKER = "005930"


def one_holding(price=1000.0):
    return {
        TICKER: Position(
            ticker=TICKER, quantity=100, avg_price=1000.0, current_price=price, name="삼성전자"
        )
    }


def today():
    return datetime.now().date()


def test_a_new_peak_is_written_to_the_store(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)

    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))

    assert abs(store.position_peaks_for(today())[TICKER] - 0.03) < 1e-9


def test_writes_are_batched_within_the_flush_interval(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.on_market_data(MarketData(ticker=TICKER, price=1010.0, volume=1))

    engine.on_market_data(MarketData(ticker=TICKER, price=1020.0, volume=1))
    assert abs(store.position_peaks_for(today())[TICKER] - 0.01) < 1e-9

    engine.flush_exit_peaks(force=True)
    assert abs(store.position_peaks_for(today())[TICKER] - 0.02) < 1e-9


def test_peak_is_flushed_once_the_interval_elapses(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.on_market_data(MarketData(ticker=TICKER, price=1010.0, volume=1))
    engine._peaks_flushed_at = datetime.now() - timedelta(seconds=6)

    engine.on_market_data(MarketData(ticker=TICKER, price=1020.0, volume=1))

    assert abs(store.position_peaks_for(today())[TICKER] - 0.02) < 1e-9


def test_engine_start_restores_todays_peak(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_position_peak(today(), TICKER, 0.0446, datetime.now(), 1044.6)

    engine, _, _ = make_engine(one_holding(), trade_store=store)

    assert engine.exit_drawdown.retracement(TICKER, 0.0).peak == 0.0446


def test_yesterdays_peak_is_not_restored(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    store.save_position_peak(today() - timedelta(days=1), TICKER, 0.0446, datetime.now(), None)

    engine, _, _ = make_engine(one_holding(), trade_store=store)

    assert engine.exit_drawdown.retracement(TICKER, 0.0) is None


def test_peak_is_tracked_even_when_ai_exit_is_off(tmp_path):
    """"순이익 기회"는 AI를 끈 날에도 의미가 있다. 앞당김 표시는 남기지 않는다."""
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.ai_exit_enabled = False
    engine.exit_drawdown.threshold_ratio = 0.01

    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))
    engine.on_market_data(MarketData(ticker=TICKER, price=1015.0, volume=1))

    assert abs(store.position_peaks_for(today())[TICKER] - 0.03) < 1e-9
    assert engine.exit_drawdown.urgent_pending is False


def test_forced_close_flushes_pending_peaks(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)
    engine.on_market_data(MarketData(ticker=TICKER, price=1010.0, volume=1))
    engine.on_market_data(MarketData(ticker=TICKER, price=1020.0, volume=1))

    engine.force_close_all_positions(reason="day_end")

    assert abs(store.position_peaks_for(today())[TICKER] - 0.02) < 1e-9


def test_store_failure_does_not_break_the_tick(tmp_path):
    store = TradeStore(tmp_path / "t.db")
    engine, _, _ = make_engine(one_holding(), trade_store=store)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    store.save_position_peak = boom
    engine.on_market_data(MarketData(ticker=TICKER, price=1030.0, volume=1))  # 예외가 새지 않는다
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_exit_drawdown.py tests/test_core/test_peak_persistence.py -q`
Expected: FAIL — `AttributeError: 'DrawdownTracker' object has no attribute 'take_dirty'` 등

- [ ] **Step 4: 트래커 구현** — `src/core/exit_drawdown.py`

`__init__` 끝에 추가:

```python
        # 마지막으로 꺼내 간 뒤 새로 찍은 고점 — 엔진이 DB에 남긴다 (스펙 2026-09-22 2.2)
        self._dirty: Dict[str, float] = {}
```

`update`의 새 고점 분기를 바꾼다:

```python
            if peak is None or current > peak:
                self._peaks[ticker] = current
                self._armed[ticker] = True
                self._dirty[ticker] = current
                continue
```

`retracement` 앞(“프롬프트 재료” 절 위)에 추가:

```python
    # ── 영속화·복원 (스펙 2026-09-22 2.2) ────────────────────
    def seed(self, peaks: Dict[str, float]) -> None:
        """재시작 전에 남긴 고점을 되살린다 — 지금 들고 있는 값보다 높을 때만 덮는다.

        복원한 고점은 무장(armed)한 채로 둔다. 복원 직후 현재값이 임계값 이상 낮으면 한 번
        앞당겨지는데, 그것은 재시작 전에 발동했어야 할 반납이다.
        """
        for ticker, peak in peaks.items():
            current = self._peaks.get(ticker)
            if current is None or peak > current:
                self._peaks[ticker] = peak
                self._armed[ticker] = True

    def take_dirty(self) -> Dict[str, float]:
        """마지막으로 꺼내 간 뒤 새로 찍은 고점을 꺼낸다 (종목당 가장 높은 값 하나)."""
        dirty = self._dirty
        self._dirty = {}
        return dirty
```

`clear`에 한 줄 추가: `self._dirty.clear()`

- [ ] **Step 5: 엔진 구현** — `src/core/engine.py`

import 아래 모듈 상수:

```python
# 당일 고점을 DB에 남기는 최소 간격 (초). 대형주는 틱이 초당 여러 번 와서 매번 쓰면 루프
# 스레드가 디스크를 기다린다. 사이에 찍힌 더 높은 고점은 다음 쓰기에 실린다 — 그래서
# 기록되는 고점 시각·가격은 최대 이만큼 늦다 (스펙 2026-09-22 2.2).
PEAK_FLUSH_SECONDS = 5.0
```

`__init__`의 `self.exit_drawdown = DrawdownTracker(...)` 아래:

```python
        # 고점을 마지막으로 DB에 남긴 시각 (`flush_exit_peaks`)
        self._peaks_flushed_at: Optional[datetime] = None
```

`start()`의 `self._open_tickers = ...` 다음 줄에 `self._restore_exit_peaks()`를 넣고, `start` 아래에 메서드를 추가한다:

```python
    def _restore_exit_peaks(self) -> None:
        """오늘 남긴 당일 고점을 트래커에 되살린다 (스펙 2026-09-22 2.2).

        재시작하면 트래커가 비어, AI 매도 판단이 재시작 뒤 고점만 보게 된다 — 2026-09-22
        11:00 판단이 실제 -0.73% 대신 -2.91%를 봤다. 엔진이 꺼져 있던 구간의 고점은 여전히 없다.
        """
        if self.trade_store is None:
            return
        try:
            peaks = self.trade_store.position_peaks_for(datetime.now().date())
        except Exception:
            logger.warning("당일 고점 복원 실패 — 재시작 뒤 고점부터 다시 잽니다.", exc_info=True)
            return
        if peaks:
            self.exit_drawdown.seed(peaks)
            logger.info(
                "당일 고점을 복원했습니다: %s",
                {ticker: f"{peak * 100:+.2f}%" for ticker, peak in peaks.items()},
            )

    def flush_exit_peaks(
        self, positions: Optional[Dict[str, Position]] = None, force: bool = False
    ) -> None:
        """새로 찍은 당일 고점을 DB에 남긴다. `force`가 아니면 `PEAK_FLUSH_SECONDS`에 한 번.

        실패해도 예외를 올리지 않는다 — 시세 콜백(손절 판정과 같은 경로)에서 불린다.
        """
        if self.trade_store is None:
            return
        now = datetime.now()
        if (
            not force
            and self._peaks_flushed_at is not None
            and (now - self._peaks_flushed_at).total_seconds() < PEAK_FLUSH_SECONDS
        ):
            return
        dirty = self.exit_drawdown.take_dirty()
        self._peaks_flushed_at = now
        source = positions if positions is not None else self._positions
        for ticker, peak in dirty.items():
            position = source.get(ticker)
            try:
                self.trade_store.save_position_peak(
                    now.date(),
                    ticker,
                    peak,
                    now,
                    position.current_price if position is not None else None,
                )
            except Exception:
                logger.warning("당일 고점 기록 실패: %s", ticker, exc_info=True)
```

`force_close_all_positions`의 docstring 바로 다음 첫 줄에:

```python
        # 매도 전에 남은 고점을 비운다 — 15:35 검증의 "순이익 기회"가 여기서 끊기면 안 된다
        self.flush_exit_peaks(force=True)
```

`_track_exit_drawdown`을 통째로 바꾼다:

```python
    def _track_exit_drawdown(self, positions: Dict[str, Position]) -> None:
        """당일 순손익 고점을 따라가 DB에 남기고, 반납폭이 임계치를 넘으면 AI 판단을 앞당긴다.

        여기서는 팔지 않는다 — `DrawdownTracker`에 표시만 남기고, 실제 호출은
        `runtime.ai_exit_due`가 그 표시를 보고 다음 폴링(최대 30초)에서 앞당긴다.
        판단 자체는 종목마다 이뤄지고(확정 2026-09-19), 앞당기는 것은 호출 시점뿐이다.

        **고점 추적은 AI 매도 판단이 꺼져 있어도 돈다** (스펙 2026-09-22 2.2) — 15:35 매도 판단
        검증의 "순이익 기회"는 AI를 끈 날에도 의미가 있다. 꺼져 있으면 앞당김 표시만 버린다.
        """
        holdings = [p for t, p in positions.items() if t not in self._exiting]
        if not holdings:
            return

        per_ticker = {p.ticker: self.position_net_return(p) for p in holdings}
        crossed = self.exit_drawdown.update(per_ticker)
        self.flush_exit_peaks(positions)
        if not self.ai_exit_enabled:
            # 앞당길 호출이 없다 — 남겨 두면 다시 켜는 순간 낡은 표시로 호출이 앞당겨진다
            self.exit_drawdown.take_urgent()
            return
        for ticker in crossed:
            retracement = self.exit_drawdown.retracement(ticker, per_ticker[ticker])
            if retracement is None:
                continue
            logger.warning(
                "이익 반납: %s 당일 고점 %+.2f%% → 현재 %+.2f%% (%.2f%%p 반납, 고점 이익의 %.0f%%) "
                "— AI 매도 판단을 앞당깁니다.",
                ticker,
                retracement.peak * 100,
                retracement.current * 100,
                retracement.given_back * 100,
                retracement.given_back_share * 100,
            )
```

- [ ] **Step 6: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core -q`
Expected: 전부 PASS. 기존 테스트 중 "AI 매도 판단이 꺼지면 트래커가 갱신되지 않는다"를 단정하는 것이 있으면 새 동작(추적은 하되 앞당김 표시는 없음)에 맞춰 단정만 바꾼다.

- [ ] **Step 7: 커밋**

```bash
git add src/core/exit_drawdown.py src/core/engine.py tests/test_core/test_exit_drawdown.py tests/test_core/test_peak_persistence.py
git commit -F - <<'EOF'
당일 고점을 DB에 남기고 엔진 재시작 때 되살린다

2026-09-22 재시작으로 고점 추적이 초기화돼 11:00 AI 판단이 실제 -0.73% 대신
-2.91%를 봤다. AI 매도 판단이 꺼져 있어도 고점은 계속 잰다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 3: PromptStore 일반화

**Files:**
- Modify: `src/llm/prompt_store.py`
- Modify: `src/core/daily_workflow.py:1306-1322` (`_why_history`를 저장소 메서드로 위임)
- Test: `tests/test_llm/test_prompt_store.py` (추가)

**Interfaces:**
- Produces:
  - `PromptStore(prompt_dir=DEFAULT_PROMPT_DIR, section_order=PROMPT_SECTION_ORDER, defaults=DEFAULT_PROMPT_SECTIONS, default_version=PROMPT_TEMPLATE_VERSION, label="추천 프롬프트")`
  - 속성 `section_order: Tuple[str, ...]`, `defaults: Dict[str, str]`
  - `PromptStore.why_history(limit: int = 5) -> str`

- [ ] **Step 1: 테스트 추가** — `tests/test_llm/test_prompt_store.py` 끝에

```python
# ── 일반화 (스펙 2026-09-22 4.1 — 매도 프롬프트도 같은 저장소를 쓴다) ──
ORDER = ("alpha", "beta")
DEFAULTS = {"alpha": "## A\n기본 A", "beta": "## B\n기본 B"}


def custom_store(tmp_path):
    return PromptStore(tmp_path / "other", ORDER, DEFAULTS, "x1", label="시험 프롬프트")


def test_custom_store_falls_back_to_its_own_defaults(tmp_path):
    store = custom_store(tmp_path)
    assert store.load_sections() == DEFAULTS
    assert store.load_version() == "x1"


def test_custom_store_saves_only_its_own_sections(tmp_path):
    store = custom_store(tmp_path)
    store.save({"beta": "## B\n새 B"}, reason="시험", today=date(2026, 9, 29))

    assert store.load_sections() == {"alpha": "## A\n기본 A", "beta": "## B\n새 B"}
    assert sorted(p.name for p in (tmp_path / "other").glob("*.md")) == ["alpha.md", "beta.md"]
    assert (tmp_path / "other" / "history" / "x1" / "alpha.md").exists()


def test_why_history_lists_recent_reasons(tmp_path):
    store = custom_store(tmp_path)
    store.save({"beta": "## B\n1"}, reason="첫 이유", today=date(2026, 9, 29))
    store.save({"beta": "## B\n2"}, reason="둘째 이유", today=date(2026, 9, 30))

    text = store.why_history()
    assert "- x1: 첫 이유" in text
    assert "- 20260929: 둘째 이유" in text


def test_why_history_is_empty_before_any_change(tmp_path):
    assert custom_store(tmp_path).why_history() == ""
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm/test_prompt_store.py -q`
Expected: FAIL — `TypeError: PromptStore.__init__() takes from 1 to 2 positional arguments`

- [ ] **Step 3: 구현** — `src/llm/prompt_store.py`

import에 `from typing import Dict, Mapping, Optional, Sequence, Tuple`로 바꾸고, 클래스를 다음처럼 고친다(바뀌는 부분만 보인다 — `next_version`, `_write_atomic`, `_read`는 그대로).

```python
class PromptStore:
    """프롬프트의 편집 가능한 절을 파일로 읽고 쓴다 (PRD '프롬프트 자동 수정').

    추천 프롬프트(다섯 절, `data/prompt/`)와 매도 프롬프트(세 절, `data/exit_prompt/`,
    스펙 2026-09-22)가 함께 쓴다 — 절 목록·기본값·기본 버전을 생성자로 받는다. 인자를
    주지 않으면 추천 프롬프트다.

    읽기는 항상 모든 키를 채워 돌려준다 — 파일이 없거나 비었으면 코드 기본값으로 폴백한다.
    쓰기는 이력 보관 → 전체 쓰기 → 버전 증가 순서다.
    """

    def __init__(
        self,
        prompt_dir: Path = DEFAULT_PROMPT_DIR,
        section_order: Sequence[str] = PROMPT_SECTION_ORDER,
        defaults: Mapping[str, str] = DEFAULT_PROMPT_SECTIONS,
        default_version: str = PROMPT_TEMPLATE_VERSION,
        label: str = "추천 프롬프트",
    ):
        self.prompt_dir = Path(prompt_dir)
        self.section_order: Tuple[str, ...] = tuple(section_order)
        self.defaults: Dict[str, str] = dict(defaults)
        self.default_version = default_version
        self.label = label

    def load_sections(self) -> Dict[str, str]:
        sections = {}
        for key in self.section_order:
            sections[key] = self._read(self.prompt_dir / f"{key}.md") or self.defaults[key]
        return sections

    def load_version(self) -> str:
        return self._read(self.prompt_dir / VERSION_FILE) or self.default_version
```

`save` 안의 `for key in PROMPT_SECTION_ORDER:` → `for key in self.section_order:`, 로그를
`logger.info("%s를 수정했습니다: %s → %s", self.label, current_version, new_version)`로,
`_archive` 안의 `for key in PROMPT_SECTION_ORDER:` → `for key in self.section_order:`로 바꾼다.
`_archive` 아래에 추가:

```python
    def why_history(self, limit: int = 5) -> str:
        """직전 변경들의 이유 (최근 `limit`개). 에이전트가 자기 수정을 되돌리는 것을 막는 입력이다.

        이력 폴더가 없으면 빈 문자열 — 첫 실행이 그 상태다.
        """
        history_dir = self.prompt_dir / HISTORY_DIR
        try:
            versions = sorted(path for path in history_dir.iterdir() if path.is_dir())
        except OSError:
            return ""
        entries = []
        for path in versions[-limit:]:
            try:
                entries.append(f"- {path.name}: {(path / WHY_FILE).read_text(encoding='utf-8')}")
            except OSError:
                continue
        return "\n".join(entries)
```

`src/core/daily_workflow.py`의 `_why_history` 본문을 위임으로 바꾼다:

```python
    def _why_history(self) -> str:
        """직전 변경들의 이유 — `PromptStore.why_history`로 옮겼다 (매도 프롬프트와 공유)."""
        return self.prompt_store.why_history()
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm tests/test_core/test_daily_workflow.py -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add src/llm/prompt_store.py src/core/daily_workflow.py tests/test_llm/test_prompt_store.py
git commit -F - <<'EOF'
PromptStore가 절 목록과 기본값을 받게 해 매도 프롬프트와 함께 쓴다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 4: 매도 프롬프트를 절로 나누고 저장소에서 읽는다

**Files:**
- Modify: `src/llm/exit_advisor.py` (`build_exit_system_prompt`, `ExitAdvisor.__init__`/`decide`, 새 상수·함수)
- Test: `tests/test_llm/test_exit_advisor.py` (추가, `ExitAdvisor.__new__`를 쓰는 두 테스트 수정)

**Interfaces:**
- Consumes: Task 3의 `PromptStore(...)`
- Produces:
  - `EXIT_PROMPT_SECTION_ORDER = ("loss_positions", "time", "criteria")`
  - `DEFAULT_EXIT_PROMPT_SECTIONS: Dict[str, str]`
  - `EXIT_PROMPT_DIR = Path("data") / "exit_prompt"`
  - `build_exit_system_prompt(sections: Optional[Dict[str, str]] = None) -> str`
  - `build_exit_locked_text() -> str`
  - `make_exit_prompt_store(prompt_dir: Path = EXIT_PROMPT_DIR) -> PromptStore`
  - `ExitAdvisor(settings, prompt_store: Optional[PromptStore] = None)`, 프로퍼티 `ExitAdvisor.prompt_version -> str`

- [ ] **Step 1: 현재 프롬프트 스냅숏 저장** (리팩터 전후 문자열이 같은지 확인용)

Run:
```bash
.venv/Scripts/python.exe -c "from src.llm.exit_advisor import build_exit_system_prompt as b; open('$SCRATCH/exit_prompt_before.txt','w',encoding='utf-8').write(b())"
```
(`$SCRATCH`는 세션 scratchpad 경로로 바꾼다.)

- [ ] **Step 2: 테스트 추가** — `tests/test_llm/test_exit_advisor.py` 끝에

```python
# ── 절 파일화 (스펙 2026-09-22 4.1) ─────────────────────────
from src.llm.exit_advisor import (
    DEFAULT_EXIT_PROMPT_SECTIONS,
    EXIT_PROMPT_SECTION_ORDER,
    EXIT_PROMPT_TEMPLATE_VERSION,
    build_exit_locked_text,
    make_exit_prompt_store,
)


def test_only_three_sections_are_editable():
    assert EXIT_PROMPT_SECTION_ORDER == ("loss_positions", "time", "criteria")
    assert set(DEFAULT_EXIT_PROMPT_SECTIONS) == set(EXIT_PROMPT_SECTION_ORDER)


def test_system_prompt_keeps_section_order():
    prompt = build_exit_system_prompt()
    headers = ["## 역할", "## 기본은 보유입니다", "## 손실 중인 종목", "## 시간", "## 판단 기준", "## reason 작성 지침"]
    positions = [prompt.index(h) for h in headers]
    assert positions == sorted(positions)


def test_edited_section_replaces_only_that_section():
    edited = "## 손실 중인 종목\n" + "바뀐 지침 " * 10
    prompt = build_exit_system_prompt({"loss_positions": edited})

    assert edited in prompt
    assert DEFAULT_EXIT_PROMPT_SECTIONS["loss_positions"] not in prompt
    assert DEFAULT_EXIT_PROMPT_SECTIONS["criteria"] in prompt


def test_locked_sections_cannot_be_overridden():
    """잠긴 절 키가 섞여 와도 무시한다 — 순수익 목적과 '기본은 보유'는 코드가 지킨다."""
    prompt = build_exit_system_prompt({"role": "## 역할\n아무거나"})
    assert "당일 순수익" in prompt


def test_locked_text_has_the_three_locked_sections():
    text = build_exit_locked_text()
    assert "## 역할" in text and "## 기본은 보유입니다" in text and "## reason 작성 지침" in text
    assert "## 손실 중인 종목" not in text


def test_exit_store_defaults(tmp_path):
    store = make_exit_prompt_store(tmp_path / "exit_prompt")
    assert store.load_sections() == DEFAULT_EXIT_PROMPT_SECTIONS
    assert store.load_version() == EXIT_PROMPT_TEMPLATE_VERSION


def test_advisor_reads_the_store_on_every_call(tmp_path):
    """파일을 고치면 엔진 재시작 없이 다음 판단에 반영된다."""
    store = make_exit_prompt_store(tmp_path / "exit_prompt")
    advisor = ExitAdvisor.__new__(ExitAdvisor)
    advisor.prompt_store = store
    store.save({"time": "## 시간\n" + "새 시간 지침 " * 10}, reason="시험", today=date(2026, 9, 29))

    assert advisor.prompt_version == "20260929"
    assert "새 시간 지침" in build_exit_system_prompt(advisor.prompt_store.load_sections())
```

파일 상단 import에 `from datetime import date`가 없으면 추가한다. 기존 두 테스트(87행 `test_decide_returns_none_when_api_raises`, 156행)의 `advisor = ExitAdvisor.__new__(ExitAdvisor)` 바로 다음 줄에 `advisor.prompt_store = make_exit_prompt_store(tmp_path / "exit_prompt")`를 넣고, 두 테스트 함수 인자에 `tmp_path`를 추가한다.

- [ ] **Step 3: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm/test_exit_advisor.py -q`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_EXIT_PROMPT_SECTIONS'`

- [ ] **Step 4: 구현** — `src/llm/exit_advisor.py`

import 추가: `from pathlib import Path`, `from typing import Dict`(기존 typing import에 합친다), `from src.llm.prompt_store import PromptStore`.

`build_exit_system_prompt` 함수 전체를 아래로 바꾼다. **각 절 문자열의 본문은 현재 함수의 해당 부분을 한 글자도 바꾸지 않고 옮긴다** (헤더 줄 `## ...`부터 다음 빈 줄 전까지).

```python
# 매도 프롬프트 편집 가능 절이 사는 곳 (스펙 2026-09-22 4.1). /data/는 gitignore 대상이라
# 코드의 DEFAULT_EXIT_PROMPT_SECTIONS가 정본이고, 여기 파일은 15:35 튜너가 덧쓴 런타임 상태다.
EXIT_PROMPT_DIR = Path("data") / "exit_prompt"

_EXIT_PROMPT_HEADER = "당신은 한국 주식시장(코스피) 단기 매매의 장중 청산 여부를 판단하는 트레이더입니다."

# ── 잠긴 절 — 튜너가 고칠 수 없다 ─────────────────────────────
# 역할: 순수익 목적은 사용자가 정한 것이다 (2026-09-22).
EXIT_ROLE_SECTION = """## 역할
(현재 함수의 "## 역할" 절 본문 그대로)"""

# 기본은 보유: 주기마다 물으면 매도 쪽으로 기우는 것을 막는 안전장치다.
EXIT_DEFAULT_HOLD_SECTION = """## 기본은 보유입니다
(현재 함수의 "## 기본은 보유입니다" 절 본문 그대로)"""

# reason 작성 지침: 출력 형식 계약이다.
EXIT_REASON_FORMAT_SECTION = """## reason 작성 지침
(현재 함수의 "## reason 작성 지침" 절 본문 그대로)"""

# ── 편집 가능한 절 — 15:35 튜너가 한 번에 하나씩 고친다 ─────────
EXIT_PROMPT_SECTION_ORDER = ("loss_positions", "time", "criteria")
DEFAULT_EXIT_PROMPT_SECTIONS: Dict[str, str] = {
    "loss_positions": """## 손실 중인 종목
(현재 함수의 "## 손실 중인 종목" 절 본문 그대로 — 절 안의 빈 줄 포함)""",
    "time": """## 시간
(현재 함수의 "## 시간" 절 본문 그대로)""",
    "criteria": """## 판단 기준
(현재 함수의 "## 판단 기준" 절 본문 그대로 — 1~3번 전부)""",
}


def build_exit_system_prompt(sections: Optional[Dict[str, str]] = None) -> str:
    """잠긴 절과 편집 가능한 절을 이어 붙인다. `sections`에서는 편집 가능한 키만 쓴다.

    잠긴 절 키(`role` 등)가 섞여 와도 무시한다 — 튜너의 안전장치(`sanitize_sections`)를
    지나온 값만 들어오지만, 파일을 손으로 고친 경우까지 코드가 한 번 더 막는다.
    """
    merged = dict(DEFAULT_EXIT_PROMPT_SECTIONS)
    for key, text in (sections or {}).items():
        if key in EXIT_PROMPT_SECTION_ORDER and text:
            merged[key] = text
    parts = [_EXIT_PROMPT_HEADER, EXIT_ROLE_SECTION, EXIT_DEFAULT_HOLD_SECTION]
    parts.extend(merged[key] for key in EXIT_PROMPT_SECTION_ORDER)
    parts.append(EXIT_REASON_FORMAT_SECTION)
    return "\n\n".join(parts)


def build_exit_locked_text() -> str:
    """튜너에게 참고용으로 보여줄 잠긴 절 전문."""
    return "\n\n".join([EXIT_ROLE_SECTION, EXIT_DEFAULT_HOLD_SECTION, EXIT_REASON_FORMAT_SECTION])


def make_exit_prompt_store(prompt_dir: Path = EXIT_PROMPT_DIR) -> PromptStore:
    """매도 프롬프트용 저장소 — 파일이 없으면 코드 기본값과 `EXIT_PROMPT_TEMPLATE_VERSION`."""
    return PromptStore(
        prompt_dir,
        EXIT_PROMPT_SECTION_ORDER,
        DEFAULT_EXIT_PROMPT_SECTIONS,
        EXIT_PROMPT_TEMPLATE_VERSION,
        label="매도 프롬프트",
    )
```

`ExitAdvisor`를 고친다:

```python
    def __init__(self, settings: Settings, prompt_store: Optional[PromptStore] = None):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        # 편집 가능한 세 절을 매 호출마다 여기서 읽는다 — 15:35 튜너가 고치면 재시작 없이 반영된다
        self.prompt_store = prompt_store if prompt_store is not None else make_exit_prompt_store()

    @property
    def prompt_version(self) -> str:
        """지금 쓰는 매도 프롬프트 버전 — 판단 기록(`ai_exit_decisions`)에 함께 남는다."""
        return self.prompt_store.load_version()
```

`decide` 안의 로그 인자 `EXIT_PROMPT_TEMPLATE_VERSION` → `self.prompt_version`, 호출 인자 `system=build_exit_system_prompt()` → `system=build_exit_system_prompt(self.prompt_store.load_sections())`.

- [ ] **Step 5: 스냅숏 비교**

Run:
```bash
.venv/Scripts/python.exe -c "from src.llm.exit_advisor import build_exit_system_prompt as b; import sys; before=open('$SCRATCH/exit_prompt_before.txt',encoding='utf-8').read(); sys.exit(0 if b()==before else 1)" && echo SAME
```
Expected: `SAME`. 다르면 절 본문 옮기기에서 글자가 바뀐 것이다 — 고쳐서 같게 만든다.

- [ ] **Step 6: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm -q`
Expected: 전부 PASS

- [ ] **Step 7: 커밋**

```bash
git add src/llm/exit_advisor.py tests/test_llm/test_exit_advisor.py
git commit -F - <<'EOF'
매도 프롬프트를 잠긴 절과 편집 가능한 세 절로 나누고 파일에서 읽는다

본문은 v2 그대로다. 파일이 없으면 코드 기본값으로 떨어진다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 5: AI 매도 판단 주기마다 판정을 기록한다

**Files:**
- Modify: `src/core/runtime.py` (`run_ai_exit_cycle`, 새 함수 `_record_ai_exit_decisions`, import)
- Test: `tests/test_core/test_ai_exit.py` (가짜 엔진·어드바이저 보강 + 테스트 추가)

**Interfaces:**
- Consumes: Task 1 `AIExitDecisionRow`/`save_ai_exit_decisions`, Task 2 `flush_exit_peaks`, Task 4 `ExitAdvisor.prompt_version`
- Produces: 판단 기록 규칙 — 판정을 받은 직후(매도 실행 전), 보유 종목 전부 한 줄씩. 실패 주기는 `ok=False`, 응답 누락 종목은 `reason="(응답 없음 — 보유로 처리)"`.

- [ ] **Step 1: 가짜 보강** — `tests/test_core/test_ai_exit.py`

`FakeEngine.__init__`의 `self.trade_store = SimpleNamespace(recommendations_for=lambda day: [])`를 바꾼다:

```python
        self.saved_decisions = []  # save_ai_exit_decisions로 넘어온 행
        self.trade_store = SimpleNamespace(
            recommendations_for=lambda day: [],
            save_ai_exit_decisions=lambda rows: self.saved_decisions.extend(rows),
        )
        self.peak_flushes = []  # flush_exit_peaks(force=...) 기록
```

`FakeEngine`에 메서드 추가:

```python
    def flush_exit_peaks(self, positions=None, force=False):
        self.peak_flushes.append(force)
```

`SpyAdvisor.__init__`에 `self.prompt_version = "vtest"` 추가.

- [ ] **Step 2: 테스트 추가** — 같은 파일 끝에

```python
# ── 판단 기록 (스펙 2026-09-22 2.1) ─────────────────────────
def test_every_held_ticker_is_recorded_with_its_verdict():
    a, b = holding("005930", "삼성전자"), holding("000660", "SK하이닉스")
    runtime, engine, _ = make_runtime(
        holdings=[a, b],
        decide_result=ExitDecision(
            decisions=[
                PositionExit(ticker="005930", sell=True, reason="목표가 도달"),
                PositionExit(ticker="000660", sell=False, reason="시나리오 유효"),
            ]
        ),
    )

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    rows = {r.ticker: r for r in engine.saved_decisions}
    assert rows["005930"].sell is True and rows["005930"].reason == "목표가 도달"
    assert rows["000660"].sell is False and rows["000660"].ok is True
    assert rows["005930"].at == IN_WINDOW and rows["005930"].day == IN_WINDOW.date()
    assert rows["005930"].exit_prompt_version == "vtest"


def test_failed_cycle_is_recorded_as_not_ok():
    runtime, engine, _ = make_runtime(holdings=[holding()], decide_result=None)

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    [row] = engine.saved_decisions
    assert row.ok is False and row.sell is False


def test_ticker_missing_from_the_response_is_recorded_as_hold():
    a, b = holding("005930"), holding("000660")
    runtime, engine, _ = make_runtime(
        holdings=[a, b],
        decide_result=ExitDecision(decisions=[PositionExit(ticker="005930", sell=False, reason="유효")]),
    )

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    rows = {r.ticker: r for r in engine.saved_decisions}
    assert rows["000660"].sell is False
    assert rows["000660"].reason == "(응답 없음 — 보유로 처리)"


def test_record_failure_does_not_block_the_sell():
    runtime, engine, _ = make_runtime(
        holdings=[holding()],
        decide_result=ExitDecision(decisions=[PositionExit(ticker="005930", sell=True, reason="r")]),
    )

    def boom(rows):
        raise OSError("disk full")

    engine.trade_store.save_ai_exit_decisions = boom

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert len(engine.executed) == 1


def test_cycle_flushes_pending_peaks_first():
    runtime, engine, _ = make_runtime(holdings=[holding()], decide_result=None)

    asyncio.run(run_ai_exit_cycle(runtime, IN_WINDOW))

    assert engine.peak_flushes == [True]
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_ai_exit.py -q -k "recorded or record_failure or flushes"`
Expected: FAIL — `saved_decisions`가 비어 있다

- [ ] **Step 4: 구현** — `src/core/runtime.py`

import에 `from src.logger.trade_store import AIExitDecisionRow, TradeStore`로 합치고(기존 `TradeStore` import 줄을 바꾼다), `run_ai_exit_cycle` 위에 추가:

```python
AI_EXIT_FAILURE_TEXT = "LLM 호출 실패·타임아웃·형식 오류 (이번 주기는 매도하지 않습니다)"
AI_EXIT_MISSING_TEXT = "(응답 없음 — 보유로 처리)"


def _record_ai_exit_decisions(runtime: Runtime, now: datetime, holdings_view, decision) -> None:
    """이번 주기의 판정을 보유 종목마다 한 줄씩 남긴다 (스펙 2026-09-22 2.1).

    매도 실행 **전에** 부른다 — 무엇을 판단했는지가 기록의 대상이고, 무엇이 팔렸는지는
    trades가 따로 남긴다. 기록 실패가 매도를 막으면 안 되므로 예외를 삼킨다.
    """
    store = runtime.engine.trade_store
    if store is None:
        return
    version = getattr(runtime.exit_advisor, "prompt_version", "") or ""
    verdicts = {}
    if decision is not None:
        for d in decision.decisions:
            verdicts.setdefault(d.ticker, d)
    rows = []
    for h in holdings_view:
        verdict = verdicts.get(h.ticker)
        if decision is None:
            sell, ok, reason = False, False, AI_EXIT_FAILURE_TEXT
        elif verdict is None:
            sell, ok, reason = False, True, AI_EXIT_MISSING_TEXT
        else:
            sell, ok, reason = verdict.sell, True, verdict.reason
        rows.append(
            AIExitDecisionRow(
                day=now.date(),
                at=now,
                ticker=h.ticker,
                name=h.name,
                sell=sell,
                ok=ok,
                reason=reason,
                net_return=h.net_return,
                peak_return=h.peak_return,
                current_price=h.current_price,
                exit_prompt_version=version,
            )
        )
    try:
        store.save_ai_exit_decisions(rows)
    except Exception:
        logger.warning("AI 매도 판단 기록 실패 — 매도 흐름은 그대로 진행합니다.", exc_info=True)
```

`run_ai_exit_cycle` 수정:
1. `engine.exit_drawdown.take_urgent()` 바로 다음 줄에:
   ```python
    # 판단 전에 남은 고점을 비운다 — 15:35 검증이 이 주기의 고점까지 보게 한다
    engine.flush_exit_peaks(force=True)
   ```
2. `except Exception: ... return` 블록 바로 다음(= `if decision is None:` 앞)에:
   ```python
    _record_ai_exit_decisions(runtime, now, holdings_view, decision)
   ```
3. `if decision is None:` 블록의 `reason_text = "LLM 호출 ..."` → `reason_text = AI_EXIT_FAILURE_TEXT`.

- [ ] **Step 5: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core -q`
Expected: 전부 PASS (`test_intraday_disclosure.py`도 같은 `make_runtime`을 쓴다)

- [ ] **Step 6: 커밋**

```bash
git add src/core/runtime.py tests/test_core/test_ai_exit.py
git commit -F - <<'EOF'
AI 매도 판단 주기마다 종목별 판정을 DB에 남긴다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 6: 월 순수익 게이트 설정 (Settings · .env.example · UI)

**Files:**
- Modify: `config/settings.py` (모듈 상수, 필드·프로퍼티, `validate`)
- Modify: `.env.example`
- Modify: `src/ui/main_window.py` (설정 폼 위젯, `_load_settings`, `_save_settings`)
- Test: `tests/test_config/test_settings.py` (추가)

**Interfaces:**
- Produces:
  - `TUNE_SKIP_MONTHLY_RETURN_CHOICES: Tuple[float, ...] = (1.0, 1.5, ..., 10.0)`
  - `Settings.tune_skip_monthly_return_percent: float` (기본 5.0), `Settings.tune_skip_monthly_return_ratio -> float`

- [ ] **Step 1: 테스트 추가** — `tests/test_config/test_settings.py` 끝에 (import에 `TUNE_SKIP_MONTHLY_RETURN_CHOICES` 추가)

```python
# ── 프롬프트 수정 중지 기준 (스펙 2026-09-22 4-A) ──────────────
def test_tune_skip_defaults_to_5_percent(monkeypatch):
    monkeypatch.delenv("TUNE_SKIP_MONTHLY_RETURN_PERCENT", raising=False)
    settings = Settings()
    assert settings.tune_skip_monthly_return_percent == 5.0
    assert settings.tune_skip_monthly_return_ratio == 0.05


def test_tune_skip_reads_env(monkeypatch):
    monkeypatch.setenv("TUNE_SKIP_MONTHLY_RETURN_PERCENT", "7.5")
    assert Settings().tune_skip_monthly_return_ratio == 0.075


def test_tune_skip_choices_run_from_1_to_10_by_half():
    assert TUNE_SKIP_MONTHLY_RETURN_CHOICES[0] == 1.0
    assert TUNE_SKIP_MONTHLY_RETURN_CHOICES[-1] == 10.0
    assert len(TUNE_SKIP_MONTHLY_RETURN_CHOICES) == 19


@pytest.mark.parametrize("value", [0.5, 10.5, 3.3])
def test_validate_rejects_a_tune_skip_value_off_the_choices(monkeypatch, value):
    settings = Settings()
    settings.tune_skip_monthly_return_percent = value
    with pytest.raises(ValueError, match="TUNE_SKIP_MONTHLY_RETURN_PERCENT"):
        settings.validate()
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config/test_settings.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: 구현** — `config/settings.py`

`AI_EXIT_INTERVAL_CHOICES` 정의 옆에:

```python
# 이번 달 순수익률이 이 값(%) 이상이면 15:35 추천·매도 프롬프트 자동 수정을 둘 다 건너뛴다
# (스펙 2026-09-22 4-A). UI 콤보가 주는 값만 허용한다 — 1~10%, 0.5 단위.
TUNE_SKIP_MONTHLY_RETURN_CHOICES = tuple(step / 2 for step in range(2, 21))
```

`gap_down_tolerance_ratio` 프로퍼티 다음에:

```python
    # 월 순수익 게이트 (스펙 2026-09-22 4-A) — 잘 되고 있을 때는 프롬프트를 건드리지 않는다.
    # 추천 프롬프트 수정(tune_prompt)과 매도 프롬프트 수정(tune_exit_prompt)에 함께 걸린다.
    tune_skip_monthly_return_percent: float = field(
        default_factory=lambda: float(os.getenv("TUNE_SKIP_MONTHLY_RETURN_PERCENT", "5"))
    )

    @property
    def tune_skip_monthly_return_ratio(self) -> float:
        return self.tune_skip_monthly_return_percent / 100
```

`validate()`의 `AI_EXIT_INTERVAL_MINUTES` 검사 다음에:

```python
        if self.tune_skip_monthly_return_percent not in TUNE_SKIP_MONTHLY_RETURN_CHOICES:
            raise ValueError(
                "TUNE_SKIP_MONTHLY_RETURN_PERCENT는 1~10 사이 0.5 단위여야 합니다: "
                f"{self.tune_skip_monthly_return_percent}"
            )
```

`.env.example`의 `AI_EXIT_INTERVAL_MINUTES` 줄 근처에:

```
# 이번 달 순수익률(%)이 이 값 이상이면 15:35 추천·매도 프롬프트 자동 수정을 건너뛴다 (1~10, 0.5 단위)
TUNE_SKIP_MONTHLY_RETURN_PERCENT=5
```

- [ ] **Step 4: UI** — `src/ui/main_window.py`

import에 `TUNE_SKIP_MONTHLY_RETURN_CHOICES` 추가(`config.settings`에서 `AI_EXIT_INTERVAL_CHOICES`를 가져오는 줄). 설정 폼의 `fund_form.addRow("추천 프롬프트", self._prompt_version)` 바로 다음에:

```python
        # 이번 달 순수익이 이 값 이상이면 15:35 프롬프트 자동 수정(추천·매도)을 건너뛴다
        # (스펙 2026-09-22 4-A). `.env`에 저장하는 값이라 바뀌면 엔진이 재시작된다.
        self._tune_skip_return = _NoScrollComboBox()
        for percent in TUNE_SKIP_MONTHLY_RETURN_CHOICES:
            self._tune_skip_return.addItem(f"월 순수익 {percent:.1f}% 이상이면 중지", percent)
        self._tune_skip_return.setToolTip(
            "이번 달 순수익률이 이 값 이상이면 장 마감 뒤 추천·매도 프롬프트를 자동으로 고치지 않습니다. "
            "검증 메일은 그대로 나갑니다."
        )
        fund_form.addRow("프롬프트 자동 수정", self._tune_skip_return)
```

`_load_settings`의 `_select_combo_value(self._ai_exit_interval, ...)` 다음에:

```python
        # 콤보 값이 실수라 _select_combo_value(정수 전용)를 쓰지 않는다
        try:
            skip = float(env.get("TUNE_SKIP_MONTHLY_RETURN_PERCENT") or 5)
        except ValueError:
            skip = 5.0
        index = self._tune_skip_return.findData(skip)
        self._tune_skip_return.setCurrentIndex(
            index if index >= 0 else self._tune_skip_return.findData(5.0)
        )
```

`_save_settings`의 `values` 딕셔너리에 `"AI_EXIT_INTERVAL_MINUTES"` 다음 줄로:

```python
            # 프롬프트 자동 수정 중지 기준 — 이 저장 경로를 타므로 바뀌면 재시작한다
            "TUNE_SKIP_MONTHLY_RETURN_PERCENT": f"{self._tune_skip_return.currentData():g}",
```

- [ ] **Step 5: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config tests/test_ui -q`
Expected: 전부 PASS

- [ ] **Step 6: 커밋**

```bash
git add config/settings.py .env.example src/ui/main_window.py tests/test_config/test_settings.py
git commit -F - <<'EOF'
월 순수익 기준 이상이면 프롬프트 자동 수정을 멈추는 설정을 추가한다

UI에서 1~10%를 0.5 단위로 고른다. 기본 5%.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 7: 검증 행 만들기와 LLM 평가 모듈

**Files:**
- Create: `src/core/exit_review.py`
- Create: `src/llm/exit_reviewer.py`
- Test: `tests/test_core/test_exit_review.py`, `tests/test_llm/test_exit_reviewer.py` (둘 다 신규)

**Interfaces:**
- Consumes: Task 1 `ExitReviewRow`, `AIExitDecisionRow`, 기존 `TradeRow`(`pnl`, `fees`, `cost`, `sell_price`)
- Produces:
  - `exit_review.CAPTURED/MISSED/NO_CHANCE/UNKNOWN`, `OUTCOME_LABELS: Dict[str, str]`
  - `exit_review.classify_outcome(net_return: Optional[float], peak_return: Optional[float]) -> str`
  - `exit_review.build_exit_review_rows(day: date, trades: List[TradeRow], peaks: Dict[str, float], decisions: List[AIExitDecisionRow], exit_reasons: Dict[str, str]) -> List[ExitReviewRow]`
  - `exit_reviewer.ExitReviewInput(row: ExitReviewRow, decisions: List[AIExitDecisionRow], outlook: str, target_sell_price: int)`
  - `exit_reviewer.ExitReviewer(settings).review(items: List[ExitReviewInput], timeout_seconds: float = 120.0) -> Optional[Dict[str, str]]`
  - `exit_reviewer.build_exit_review_system_prompt() -> str`, `build_exit_review_user_prompt(items) -> str`

분류 규칙(스펙 3.2): 순손익 > 0이면 고점을 몰라도 `captured`. 순손익을 모르면 `unknown`. 순손익 ≤ 0이고 고점을 모르면 `unknown`, 고점 > 0이면 `missed`, 아니면 `no_chance`.

- [ ] **Step 1: 테스트 작성** — `tests/test_core/test_exit_review.py`

```python
"""매도 판단 검증의 분류 (스펙 2026-09-22 3.2) — 잣대는 순수익이다.

덜 잃은 것은 성공이 아니다. 분류는 코드가 정하고 LLM은 평가문만 쓴다.
"""
from datetime import date, datetime

import pytest

from src.core.exit_review import (
    CAPTURED,
    MISSED,
    NO_CHANCE,
    UNKNOWN,
    build_exit_review_rows,
    classify_outcome,
)
from src.logger.trade_store import AIExitDecisionRow, TradeRow

DAY = date(2026, 9, 22)


@pytest.mark.parametrize(
    "net, peak, expected",
    [
        (0.012, 0.02, CAPTURED),
        (0.012, None, CAPTURED),
        (-0.029, 0.017, MISSED),
        (0.0, 0.017, MISSED),
        (-0.029, -0.0073, NO_CHANCE),
        (-0.029, 0.0, NO_CHANCE),
        (-0.029, None, UNKNOWN),
        (None, 0.02, UNKNOWN),
    ],
)
def test_classify_outcome(net, peak, expected):
    assert classify_outcome(net, peak) == expected


def decision(ticker, hour, version="v2"):
    return AIExitDecisionRow(
        day=DAY, at=datetime(2026, 9, 22, hour, 0), ticker=ticker, name="", sell=False, ok=True,
        reason="r", net_return=-0.01, peak_return=None, current_price=0.0, exit_prompt_version=version,
    )


def samsung_life():
    """2026-09-22 삼성생명 — 298,000원 2주, 290,000원 강제청산, 수수료·세금 1,320원."""
    return TradeRow(
        ticker="032830", name="삼성생명", quantity=2, buy_price=298_000.0, sell_price=290_000.0,
        pnl=-16_000.0, fees=1_320.0,
    )


def test_rows_use_net_pnl_after_fees():
    [row] = build_exit_review_rows(DAY, [samsung_life()], {"032830": -0.0073}, [], {"032830": "day_end"})

    assert row.net_pnl == -17_320.0
    assert abs(row.net_return - (-17_320.0 / 596_000.0)) < 1e-12
    assert row.outcome == NO_CHANCE
    assert row.exit_reason == "day_end"
    assert row.peak_return == -0.0073


def test_unsold_positions_are_not_reviewed():
    held = TradeRow(ticker="005930", name="", quantity=1, buy_price=1.0, sell_price=None, pnl=None)
    assert build_exit_review_rows(DAY, [held], {}, [], {}) == []


def test_unknown_pnl_is_unknown():
    manual = TradeRow(ticker="005930", name="", quantity=1, buy_price=0.0, sell_price=1.0, pnl=None)
    [row] = build_exit_review_rows(DAY, [manual], {"005930": 0.02}, [], {})
    assert row.net_pnl is None and row.outcome == UNKNOWN


def test_version_and_count_come_from_that_tickers_decisions():
    decisions = [decision("032830", 10, "v2"), decision("035420", 11, "v1"), decision("032830", 14, "20260929")]
    [row] = build_exit_review_rows(DAY, [samsung_life()], {}, decisions, {})

    assert row.decision_count == 2
    assert row.exit_prompt_version == "20260929"


def test_no_decisions_means_empty_version():
    [row] = build_exit_review_rows(DAY, [samsung_life()], {}, [], {})
    assert row.exit_prompt_version == "" and row.decision_count == 0
```

- [ ] **Step 2: 테스트 작성** — `tests/test_llm/test_exit_reviewer.py`

```python
from datetime import date, datetime
from types import SimpleNamespace

from src.llm.exit_reviewer import (
    ExitReviewer,
    ExitReviewInput,
    build_exit_review_system_prompt,
    build_exit_review_user_prompt,
)
from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow

DAY = date(2026, 9, 22)


def item():
    row = ExitReviewRow(
        day=DAY, ticker="032830", name="삼성생명", exit_prompt_version="v2", net_pnl=-17_320.0,
        net_return=-0.0291, peak_return=-0.0073, outcome="no_chance", exit_reason="day_end",
        decision_count=1,
    )
    d = AIExitDecisionRow(
        day=DAY, at=datetime(2026, 9, 22, 10, 20), ticker="032830", name="삼성생명", sell=False,
        ok=True, reason="손실은 손절선이 관리", net_return=-0.0274, peak_return=-0.0073,
        current_price=290_500.0, exit_prompt_version="v2",
    )
    return ExitReviewInput(row=row, decisions=[d], outlook="못 하면 293,500원까지 되밀림", target_sell_price=304_000)


def test_system_prompt_fixes_the_net_profit_yardstick():
    prompt = build_exit_review_system_prompt()
    assert "순손익" in prompt
    assert "덜 잃" in prompt
    assert "그 시점" in prompt


def test_user_prompt_carries_numbers_and_every_decision():
    prompt = build_exit_review_user_prompt([item()])
    assert "032830 삼성생명" in prompt
    assert "-17,320원" in prompt
    assert "-2.91%" in prompt
    assert "고점 -0.73%" in prompt
    assert "10:20" in prompt and "보유" in prompt and "손실은 손절선이 관리" in prompt
    assert "293,500원" in prompt
    assert "304,000원" in prompt


def test_review_returns_none_when_api_raises():
    reviewer = ExitReviewer.__new__(ExitReviewer)
    reviewer.settings = SimpleNamespace(llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    reviewer._client = Boom()
    assert reviewer.review([item()]) is None


def test_review_of_nothing_is_none():
    reviewer = ExitReviewer.__new__(ExitReviewer)
    assert reviewer.review([]) is None
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_exit_review.py tests/test_llm/test_exit_reviewer.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: 구현** — `src/core/exit_review.py`

```python
"""15:35 매도 판단 검증의 순수 계산 (스펙 2026-09-22 3.2).

잣대는 순수익이다 — 순손익(수수료·세금 차감)이 플러스로 확정됐는가. 덜 잃은 것, 종가보다
나은 값에 판 것은 성공이 아니다. 분류는 여기서 코드가 정한다 — LLM이 자기 판단을 채점하면서
분류까지 정하면 잣대가 흔들린다.

보유 중 고점은 `position_peaks`(슬리피지를 뺀 순손익률)이고 최종 순손익률은 체결 기준이라
슬리피지만큼 기준이 다르다. 분류 경계(0)에는 영향이 거의 없어 그대로 쓴다.
"""
from datetime import date
from typing import Dict, List, Optional

from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow, TradeRow

CAPTURED = "captured"
MISSED = "missed"
NO_CHANCE = "no_chance"
UNKNOWN = "unknown"

OUTCOME_LABELS: Dict[str, str] = {
    CAPTURED: "순이익 확정",
    MISSED: "순이익 기회를 놓침",
    NO_CHANCE: "순이익 기회 없음",
    UNKNOWN: "판정 불가",
}


def classify_outcome(net_return: Optional[float], peak_return: Optional[float]) -> str:
    """순손익이 플러스면 고점과 무관하게 확정이다. 아니면 고점으로 기회가 있었는지 가른다."""
    if net_return is None:
        return UNKNOWN
    if net_return > 0:
        return CAPTURED
    if peak_return is None:
        return UNKNOWN
    if peak_return > 0:
        return MISSED
    return NO_CHANCE


def build_exit_review_rows(
    day: date,
    trades: List[TradeRow],
    peaks: Dict[str, float],
    decisions: List[AIExitDecisionRow],
    exit_reasons: Dict[str, str],
) -> List[ExitReviewRow]:
    """그날 매도한 종목마다 검증 한 줄. 팔지 않은 종목(sell_price None)은 뺀다.

    순손익 = 실현손익 − 그 종목의 당일 수수료·세금(매수분 포함, `TradeRow.fees`).
    버전은 그 종목의 **마지막** 판단에 쓰인 매도 프롬프트 버전이다.
    """
    by_ticker: Dict[str, List[AIExitDecisionRow]] = {}
    for d in decisions:
        by_ticker.setdefault(d.ticker, []).append(d)

    rows = []
    for trade in trades:
        if trade.sell_price is None:
            continue
        net_pnl = trade.pnl - trade.fees if trade.pnl is not None else None
        net_return = net_pnl / trade.cost if net_pnl is not None and trade.cost > 0 else None
        mine = by_ticker.get(trade.ticker, [])
        peak = peaks.get(trade.ticker)
        rows.append(
            ExitReviewRow(
                day=day,
                ticker=trade.ticker,
                name=trade.name or "",
                exit_prompt_version=mine[-1].exit_prompt_version if mine else "",
                net_pnl=net_pnl,
                net_return=net_return,
                peak_return=peak,
                outcome=classify_outcome(net_return, peak),
                exit_reason=exit_reasons.get(trade.ticker, ""),
                decision_count=len(mine),
            )
        )
    return rows
```

- [ ] **Step 5: 구현** — `src/llm/exit_reviewer.py`

```python
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.llm.recommender import MAX_TOKENS
from src.llm.reviewer import REVIEW_SCHEMA, parse_reviews
from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow

logger = logging.getLogger(__name__)

# 매도 판단 평가 프롬프트 버전 — 추천 검증(REVIEW_PROMPT_TEMPLATE_VERSION)과 따로 움직인다.
EXIT_REVIEW_PROMPT_TEMPLATE_VERSION = "v1"

# 사람이 읽을 청산 사유 (trades.exit_reason 값 → 문구)
EXIT_REASON_LABELS: Dict[str, str] = {
    "ai_judgment": "AI 매도 판단",
    "stop_loss": "손절",
    "day_end": "15:15 강제청산",
    "manual": "수동 전량 매도",
    "manual_selected": "수동 선택 매도",
}


@dataclass
class ExitReviewInput:
    """평가 한 건 — 그날 매도한 종목 하나의 결과와 판단 이력, 아침 시나리오."""

    row: ExitReviewRow
    decisions: List[AIExitDecisionRow]
    outlook: str
    target_sell_price: int


def _pct(ratio: Optional[float]) -> str:
    return "모름" if ratio is None else f"{ratio * 100:+.2f}%"


def build_exit_review_system_prompt() -> str:
    return """당신은 한국 주식시장(코스피) 단기 매매의 장중 매도 판단을 사후 검증하는 분석가입니다.

## 역할
오늘 보유 종목마다 AI가 내린 매도/보유 판단의 이력과, 그 종목의 최종 순손익을 대조해
판단이 어디서 옳았고 어디서 틀렸는지를 종목마다 두세 문장으로 적습니다.

## 평가 잣대
1. 성공은 **순손익(수수료·세금을 뺀 값)이 플러스로 확정된 것**뿐입니다. 덜 잃은 것,
   종가보다 높게 판 것, 다른 시점보다 나았던 것은 성공이 아닙니다.
2. 순이익 기회(보유 중 순손익 고점이 플러스)가 있었다면 그것을 확정했는지가 핵심입니다.
   기회가 없었다면, 시나리오가 깨진 뒤 손실을 얼마나 일찍 끊었는지를 봅니다.
3. 판단마다 **그 시점에 알 수 있던 정보로** 평가하십시오. 나중 가격을 근거로 그 시점 판단을
   탓하지 마십시오. 다만 그 시점 데이터(순손익률·고점·아침 전망의 가격)가 이미 매도 근거를
   보여줬는데 보유했다면 그것은 지적하십시오.

## 규칙
1. 제공된 수치만 근거로 삼고, 반드시 수치를 인용하십시오. ("10:20 순손익 -2.74%로 아침 전망의
   하방선 293,500원을 이미 깼는데 보유했다"처럼) 모호한 표현은 금지합니다.
2. 프롬프트를 어떻게 고치라는 제안은 하지 마십시오. 오늘 무슨 일이 있었는지만 적습니다.
3. 제공된 모든 종목에 대해 하나씩 적고, 종목코드를 그대로 돌려주십시오."""


def build_exit_review_user_prompt(items: List[ExitReviewInput]) -> str:
    lines = ["오늘 매도한 종목의 최종 결과와, 보유 중 AI가 내린 판단 이력입니다.", f"\n## 종목 ({len(items)}종목)"]
    for it in items:
        row = it.row
        pnl = "모름" if row.net_pnl is None else f"{row.net_pnl:+,.0f}원"
        lines.append(
            f"- {row.ticker} {row.name}: 최종 순손익 {pnl} ({_pct(row.net_return)}), "
            f"보유 중 순손익 고점 {_pct(row.peak_return)}, "
            f"청산 {EXIT_REASON_LABELS.get(row.exit_reason, row.exit_reason or '모름')}"
        )
        if it.target_sell_price > 0:
            lines.append(f"  아침 목표 매도가: {it.target_sell_price:,}원")
        if it.outlook:
            lines.append(f"  아침 전망: {it.outlook}")
        if not it.decisions:
            lines.append("  AI 판단 이력: 없음")
        for d in it.decisions:
            verdict = "매도" if d.sell else ("보유 (판단 실패)" if not d.ok else "보유")
            lines.append(
                f"  - {d.at:%H:%M} 순손익 {_pct(d.net_return)} (고점 {_pct(d.peak_return)}), "
                f"현재가 {d.current_price:,.0f}원 → {verdict}: {d.reason}"
            )
    lines.append("\n종목마다 판단을 평가하세요.")
    return "\n".join(lines)


class ExitReviewer:
    """매도 판단 사후 평가 모듈 — 추천 검증(`LLMReviewer`)과 같은 모델·스키마, 별도 프롬프트."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def review(
        self, items: List[ExitReviewInput], timeout_seconds: float = 120.0
    ) -> Optional[Dict[str, str]]:
        """{종목코드: 평가문}. 실패하면 None — 검증 메일은 수치만으로 나간다."""
        if not items:
            return None
        user_prompt = build_exit_review_user_prompt(items)
        logger.info(
            "매도 판단 검증 요청 (exit_review_prompt_version=%s, %d종목):\n%s",
            EXIT_REVIEW_PROMPT_TEMPLATE_VERSION,
            len(items),
            user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_exit_review_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            )
        except Exception:
            logger.exception("매도 판단 검증 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("매도 판단 검증 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("매도 판단 검증 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_reviews(raw_text)
        except Exception:
            logger.exception("매도 판단 검증 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
```

- [ ] **Step 6: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_exit_review.py tests/test_llm/test_exit_reviewer.py -q`
Expected: 전부 PASS

- [ ] **Step 7: 커밋**

```bash
git add src/core/exit_review.py src/llm/exit_reviewer.py tests/test_core/test_exit_review.py tests/test_llm/test_exit_reviewer.py
git commit -F - <<'EOF'
매도 판단 검증의 순수익 분류와 LLM 평가 모듈을 추가한다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 8: 15:35 매도 판단 검증 단계와 메일

**Files:**
- Modify: `src/core/daily_workflow.py` (생성자, 새 메서드 `review_exits`/`_fill_exit_reviews`, import)
- Modify: `src/notification/templates.py` (새 함수 `exit_review_email`)
- Test: `tests/test_core/test_exit_review_workflow.py` (신규)

**Interfaces:**
- Consumes: Task 1 저장소 메서드, Task 7 `build_exit_review_rows`/`ExitReviewInput`/`OUTCOME_LABELS`/`EXIT_REASON_LABELS`
- Produces:
  - `DailyWorkflow(..., exit_reviewer=None, exit_tuner=None, exit_prompt_store=None, tune_skip_monthly_return_ratio: float = 0.05)` — 뒤 세 인자는 Task 9에서 쓰지만 생성자 변경은 여기서 한 번에 한다
  - `DailyWorkflow.review_exits(today: Optional[date] = None) -> None`
  - `templates.exit_review_email(rows: List[ExitReviewRow], decisions: List[AIExitDecisionRow], today: date) -> tuple[str, str]`

- [ ] **Step 1: 테스트 작성** — `tests/test_core/test_exit_review_workflow.py`

```python
"""15:35 매도 판단 검증 (스펙 2026-09-22 3절)."""
import sqlite3
from datetime import date, datetime

from src.llm.exit_advisor import make_exit_prompt_store
from src.logger.trade_store import AIExitDecisionRow

from tests.test_core.test_daily_workflow import build_workflow

DAY = date(2026, 9, 22)


class FakeExitReviewer:
    def __init__(self):
        self.result = None
        self.calls = []

    def review(self, items, timeout_seconds=120.0):
        self.calls.append(items)
        return self.result


def build_exit_workflow(tmp_path):
    workflow = build_workflow(tmp_path)
    workflow.exit_reviewer = FakeExitReviewer()
    workflow.exit_prompt_store = make_exit_prompt_store(tmp_path / "exit_prompt")
    return workflow


def insert_trade(workflow, ticker, side, hhmm, price, avg=None, pnl=None, fee=0.0, reason=None, name="삼성생명"):
    with sqlite3.connect(workflow.trade_store.db_path) as conn:
        conn.execute(
            """INSERT INTO trades (order_id, ticker, side, status, quantity, filled_quantity,
                   filled_price, avg_price, realized_pnl, timestamp, name, commission, tax, exit_reason)
               VALUES (?, ?, ?, 'filled', 2, 2, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (f"{ticker}{side}{hhmm}", ticker, side, price, avg, pnl, f"2026-09-22T{hhmm}:00", name, fee, reason),
        )


def samsung_life_day(workflow):
    insert_trade(workflow, "032830", "buy", "09:05", 298_000.0, fee=80.0)
    insert_trade(workflow, "032830", "sell", "15:15", 290_000.0, avg=298_000.0, pnl=-16_000.0, fee=1_240.0, reason="day_end")
    workflow.trade_store.save_position_peak(DAY, "032830", -0.0073, datetime(2026, 9, 22, 9, 20), 297_000.0)
    workflow.trade_store.save_ai_exit_decisions([
        AIExitDecisionRow(
            day=DAY, at=datetime(2026, 9, 22, 10, 20), ticker="032830", name="삼성생명", sell=False,
            ok=True, reason="손실은 손절선이 관리", net_return=-0.0274, peak_return=-0.0073,
            current_price=290_500.0, exit_prompt_version="v2",
        )
    ])


def test_review_saves_rows_and_sends_mail(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    samsung_life_day(workflow)
    workflow.exit_reviewer.result = {"032830": "10:20 하방선 이탈 뒤 보유했다."}

    workflow.review_exits(DAY)

    [row] = workflow.trade_store.exit_reviews_for(DAY)
    assert row.net_pnl == -17_320.0
    assert row.outcome == "no_chance"
    assert row.exit_reason == "day_end"
    assert row.exit_prompt_version == "v2"
    assert row.decision_count == 1
    assert row.review == "10:20 하방선 이탈 뒤 보유했다."
    subject, body, _ = workflow.email.sent[-1]
    assert "매도 판단 검증" in subject and "-17,320원" in subject
    assert "순이익 기회 없음" in body
    assert "10:20" in body and "손실은 손절선이 관리" in body
    assert "10:20 하방선 이탈 뒤 보유했다." in body


def test_review_passes_the_morning_outlook_to_the_llm(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    samsung_life_day(workflow)
    workflow.trade_store.save_recommendations(DAY, [], "v11")  # 추천 없음 — 전망 빈 문자열
    workflow.review_exits(DAY)

    [items] = workflow.exit_reviewer.calls
    assert items[0].row.ticker == "032830"
    assert items[0].outlook == ""
    assert [d.reason for d in items[0].decisions] == ["손실은 손절선이 관리"]


def test_no_mail_on_a_day_without_sells(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    workflow.review_exits(DAY)
    assert workflow.email.sent == []
    assert workflow.exit_reviewer.calls == []


def test_reviewer_failure_still_sends_numbers(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    samsung_life_day(workflow)

    def boom(items, timeout_seconds=120.0):
        raise RuntimeError("down")

    workflow.exit_reviewer.review = boom
    workflow.review_exits(DAY)

    assert workflow.trade_store.exit_reviews_for(DAY)[0].review == ""
    assert "매도 판단 검증" in workflow.email.sent[-1][0]


def test_unknown_rows_are_not_sent_to_the_llm(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    insert_trade(workflow, "005930", "sell", "11:00", 70_000.0, avg=None, pnl=None, reason="manual", name="삼성전자")

    workflow.review_exits(DAY)

    assert workflow.exit_reviewer.calls == []
    assert workflow.trade_store.exit_reviews_for(DAY)[0].outcome == "unknown"


def test_review_works_without_a_reviewer(tmp_path):
    workflow = build_exit_workflow(tmp_path)
    workflow.exit_reviewer = None
    samsung_life_day(workflow)

    workflow.review_exits(DAY)

    assert workflow.trade_store.exit_reviews_for(DAY)[0].outcome == "no_chance"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_exit_review_workflow.py -q`
Expected: FAIL — `AttributeError: 'DailyWorkflow' object has no attribute 'review_exits'`

- [ ] **Step 3: 템플릿 구현** — `src/notification/templates.py`

파일 상단 import에 `from src.core.exit_review import OUTCOME_LABELS`, `from src.llm.exit_reviewer import EXIT_REASON_LABELS`, `from src.logger.trade_store import AIExitDecisionRow, ExitReviewRow`(기존 trade_store import 줄에 합친다)를 추가한다. 순환 import가 생기면(`templates`가 `src.core`를 import하는 것이 처음이면 확인) `OUTCOME_LABELS`·`EXIT_REASON_LABELS`를 함수 안에서 import한다. `prompt_tuning_email` 다음에 추가:

```python
def _ratio_text(ratio: Optional[float]) -> str:
    return "모름" if ratio is None else f"{ratio * 100:+.2f}%"


def exit_review_email(
    rows: List[ExitReviewRow], decisions: List[AIExitDecisionRow], today: date
) -> tuple[str, str]:
    """15:35 매도 판단 검증 이메일 (스펙 2026-09-22 3.4).

    잣대는 순수익이다 — 분류(순이익 확정/놓침/기회 없음)가 맨 앞에 온다. 표가 없어
    HTML을 함께 만들지 않는다. (제목, 평문)만 돌려준다.
    """
    known = [r.net_pnl for r in rows if r.net_pnl is not None]
    total = sum(known)
    subject = f"[AutoTrade] {today:%Y-%m-%d} 매도 판단 검증 {len(rows)}종목 (순손익 {total:+,.0f}원)"

    counts = {key: sum(1 for r in rows if r.outcome == key) for key in OUTCOME_LABELS}
    lines = [
        f"{today:%Y-%m-%d} 매도한 종목의 순손익과 AI 매도 판단 이력입니다.",
        f"합계 순손익 {total:+,.0f}원 — "
        + " / ".join(f"{OUTCOME_LABELS[key]} {counts[key]}" for key in OUTCOME_LABELS),
        "",
    ]
    for i, row in enumerate(rows, start=1):
        pnl = "모름" if row.net_pnl is None else f"{row.net_pnl:+,.0f}원"
        lines.append(f"{i}. {row.label} — {OUTCOME_LABELS.get(row.outcome, row.outcome)}")
        lines.append(f"   순손익 {pnl} ({_ratio_text(row.net_return)}), 보유 중 고점 {_ratio_text(row.peak_return)}")
        lines.append(
            f"   청산: {EXIT_REASON_LABELS.get(row.exit_reason, row.exit_reason or '모름')}"
            f" · 매도 프롬프트 {row.exit_prompt_version or '-'}"
        )
        mine = [d for d in decisions if d.ticker == row.ticker]
        if not mine:
            lines.append("   AI 판단 이력: 없음")
        for d in mine:
            verdict = "매도" if d.sell else ("보유(판단 실패)" if not d.ok else "보유")
            lines.append(
                f"   - {d.at:%H:%M} {_ratio_text(d.net_return)} (고점 {_ratio_text(d.peak_return)}) "
                f"{verdict}: {d.reason}"
            )
        if row.review:
            lines.append(f"   평가: {row.review}")
        lines.append("")

    lines.append("※ 순손익은 수수료·세금을 뺀 값입니다. 고점은 엔진이 켜져 있던 동안만 잽니다.")
    return subject, "\n".join(lines)
```

`typing` import에 `Optional`이 없으면 추가한다.

- [ ] **Step 4: 워크플로 구현** — `src/core/daily_workflow.py`

import 추가:

```python
from src.core import exit_review
from src.llm import exit_reviewer as exit_reviewer_module
from src.llm.exit_advisor import make_exit_prompt_store
```

생성자 시그니처의 `prompt_store=None,` 다음에 네 인자를 추가:

```python
        exit_reviewer=None,
        exit_tuner=None,
        exit_prompt_store=None,
        tune_skip_monthly_return_ratio: float = 0.05,
```

생성자 본문의 `self.prompt_store = ...` 다음에:

```python
        # 15:35 매도 판단 검증의 LLM 평가 모듈 (스펙 2026-09-22 3.3). None이면 수치만 남긴다.
        self.exit_reviewer = exit_reviewer
        # 매도 프롬프트 자동 수정 (스펙 4절). None이면 단계 자체를 건너뛴다.
        self.exit_tuner = exit_tuner
        self.exit_prompt_store = (
            exit_prompt_store if exit_prompt_store is not None else make_exit_prompt_store()
        )
        # 이번 달 순수익률이 이 값 이상이면 두 프롬프트 모두 고치지 않는다 (스펙 4-A)
        self.tune_skip_monthly_return_ratio = tune_skip_monthly_return_ratio
```

`tune_prompt` 메서드 바로 앞에 추가:

```python
    def review_exits(self, today: Optional[date] = None) -> None:
        """15:35 — 오늘 매도한 종목의 순손익을 AI 매도 판단 이력과 대조해 남기고 메일로 보낸다.

        잣대는 순수익이다 (스펙 2026-09-22 1절). 분류는 코드가(`exit_review`), 평가문은 LLM이
        쓴다. 매도가 없던 날은 아무것도 하지 않는다. 어떤 실패도 매매에 영향이 없다.
        """
        today = today or date.today()
        self._sync_fills(today)
        decisions = self.trade_store.ai_exit_decisions_for(today)
        rows = exit_review.build_exit_review_rows(
            today,
            self.trade_store.daily_summary(today).trades,
            self.trade_store.position_peaks_for(today),
            decisions,
            self.trade_store.last_exit_reasons(today),
        )
        if not rows:
            logger.info("오늘 매도한 종목이 없습니다 — 매도 판단 검증을 건너뜁니다 (%s).", today)
            return

        self._fill_exit_reviews(today, rows, decisions)
        for row in rows:
            try:
                self.trade_store.save_exit_review(row)
            except Exception:
                logger.exception("매도 판단 검증 저장 실패: %s", row.label)

        subject, body = templates.exit_review_email(rows, decisions, today)
        self.email.send(subject, body)
        logger.info("매도 판단 검증 메일 발송 (%s, %d종목)", today, len(rows))

    def _fill_exit_reviews(self, today: date, rows, decisions) -> None:
        """LLM 평가문을 받아 rows에 채운다. 판정 불가(unknown) 종목은 보내지 않는다."""
        if self.exit_reviewer is None:
            return
        recommendations = {r.ticker: r for r in self.trade_store.recommendations_for(today)}
        items = [
            exit_reviewer_module.ExitReviewInput(
                row=row,
                decisions=[d for d in decisions if d.ticker == row.ticker],
                outlook=recommendations[row.ticker].outlook if row.ticker in recommendations else "",
                target_sell_price=(
                    recommendations[row.ticker].target_sell_price
                    if row.ticker in recommendations
                    else 0
                ),
            )
            for row in rows
            if row.outcome != exit_review.UNKNOWN
        ]
        if not items:
            return
        try:
            reviews = self.exit_reviewer.review(items)
        except Exception:
            logger.exception("매도 판단 평가 호출이 실패했습니다 — 수치만으로 메일을 보냅니다.")
            return
        for row in rows:
            row.review = (reviews or {}).get(row.ticker, "")
```

- [ ] **Step 5: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core tests/test_notification -q`
Expected: 전부 PASS

- [ ] **Step 6: 커밋**

```bash
git add src/core/daily_workflow.py src/notification/templates.py tests/test_core/test_exit_review_workflow.py
git commit -F - <<'EOF'
15:35 매도 판단 검증 단계와 검증 메일을 추가한다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 9: 매도 프롬프트 튜너와 월 순수익 게이트

**Files:**
- Modify: `src/llm/tuner.py` (`sanitize_sections` 인자)
- Create: `src/llm/exit_tuner.py`
- Modify: `src/core/daily_workflow.py` (`tune_exit_prompt`, `_tuning_blocked_by_monthly_return`, `_monthly_summary_with_base`, `tune_prompt`에 게이트, `send_daily_report`가 헬퍼 사용)
- Modify: `src/notification/templates.py` (`exit_prompt_tuning_email`)
- Test: `tests/test_llm/test_exit_tuner.py` (신규), `tests/test_core/test_exit_review_workflow.py` (추가), `tests/test_core/test_daily_workflow.py` (게이트 테스트 추가)

**Interfaces:**
- Consumes: Task 1 `recent_exit_reviews`/`count_exit_reviews`, Task 3 `why_history`, Task 4 `build_exit_locked_text`/`EXIT_PROMPT_SECTION_ORDER`, Task 7 분류 상수, Task 8 생성자 인자
- Produces:
  - `tuner.sanitize_sections(raw, allowed: Sequence[str] = PROMPT_SECTION_ORDER, max_sections: int = MAX_SECTIONS_PER_CHANGE) -> Dict[str, str]`
  - `exit_tuner.MAX_SECTIONS_PER_CHANGE = 1`, `exit_tuner.MIN_REVIEWS_FOR_TUNING = 10`
  - `exit_tuner.ExitVersionStats(version, count, captured, missed, no_chance, net_pnl_total, avg_net_return, avg_missed_gap)`
  - `exit_tuner.group_by_version(rows: List[ExitReviewRow]) -> List[ExitVersionStats]`
  - `exit_tuner.ExitPromptTuner(settings).tune(stats, rows, sections, locked_text, why_history="", timeout_seconds=120.0) -> Optional[tuner.TuneResult]`
  - `DailyWorkflow.tune_exit_prompt(today: Optional[date] = None) -> None`
  - `templates.exit_prompt_tuning_email(today, old_version, new_version, reason, stats, before, after) -> tuple[str, str]`

- [ ] **Step 1: 튜너 테스트** — `tests/test_llm/test_exit_tuner.py`

```python
from datetime import date
from types import SimpleNamespace

from src.llm.exit_advisor import EXIT_PROMPT_SECTION_ORDER
from src.llm.exit_tuner import (
    MAX_SECTIONS_PER_CHANGE,
    ExitPromptTuner,
    build_exit_tune_system_prompt,
    build_exit_tune_user_prompt,
    group_by_version,
)
from src.llm.tuner import sanitize_sections
from src.logger.trade_store import ExitReviewRow

LONG = "## 시간\n" + "지침 " * 20


def row(version, outcome, net_pnl=1000.0, net_return=0.01, peak=0.02, day=date(2026, 9, 22)):
    return ExitReviewRow(
        day=day, ticker="005930", name="삼성전자", exit_prompt_version=version, net_pnl=net_pnl,
        net_return=net_return, peak_return=peak, outcome=outcome, exit_reason="ai_judgment",
        decision_count=3, review="평가문",
    )


def test_group_by_version_counts_outcomes_and_skips_unknown():
    stats = group_by_version([
        row("v2", "captured", 1000.0, 0.01, 0.02),
        row("v2", "missed", -500.0, -0.005, 0.015),
        row("v2", "unknown", None, None, None),
        row("20260929", "no_chance", -2000.0, -0.02, -0.001),
    ])
    by = {s.version: s for s in stats}
    v2 = by["v2"]
    assert (v2.count, v2.captured, v2.missed, v2.no_chance) == (2, 1, 1, 0)
    assert v2.net_pnl_total == 500.0
    assert abs(v2.avg_net_return - 0.0025) < 1e-12
    # 놓친 폭은 고점이 플러스였던 종목만: (0.02-0.01 + 0.015-(-0.005)) / 2
    assert abs(v2.avg_missed_gap - 0.015) < 1e-12
    assert by["20260929"].avg_missed_gap is None


def test_exit_sanitize_allows_one_editable_section_only():
    order = EXIT_PROMPT_SECTION_ORDER
    assert MAX_SECTIONS_PER_CHANGE == 1
    assert sanitize_sections({"time": LONG}, order, MAX_SECTIONS_PER_CHANGE) == {"time": LONG}
    assert sanitize_sections({"time": LONG, "criteria": LONG}, order, MAX_SECTIONS_PER_CHANGE) == {}
    assert sanitize_sections({"role": LONG}, order, MAX_SECTIONS_PER_CHANGE) == {}
    assert sanitize_sections({"time": "짧음"}, order, MAX_SECTIONS_PER_CHANGE) == {}


def test_system_prompt_states_the_rules():
    prompt = build_exit_tune_system_prompt()
    assert "순손익" in prompt
    assert "1절" in prompt
    assert "loss_positions" in prompt and "time" in prompt and "criteria" in prompt
    assert "되돌리는" in prompt


def test_user_prompt_carries_stats_rows_sections_and_history():
    stats = group_by_version([row("v2", "missed", -500.0, -0.005, 0.015)])
    prompt = build_exit_tune_user_prompt(
        stats,
        [row("v2", "missed", -500.0, -0.005, 0.015)],
        {key: f"## {key}\n본문" for key in EXIT_PROMPT_SECTION_ORDER},
        "## 역할\n잠김",
        "- v1: 이전 이유",
    )
    assert "v2: 1건" in prompt and "놓침 1" in prompt
    assert "평가문" in prompt
    assert "### loss_positions" in prompt
    assert "## 역할\n잠김" in prompt
    assert "- v1: 이전 이유" in prompt


def test_tune_returns_none_when_api_raises():
    tuner = ExitPromptTuner.__new__(ExitPromptTuner)
    tuner.settings = SimpleNamespace(llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("down")

    tuner._client = Boom()
    assert tuner.tune([], [], {}, "") is None
```

- [ ] **Step 2: 워크플로 테스트** — `tests/test_core/test_exit_review_workflow.py` 끝에

```python
# ── 매도 프롬프트 자동 수정 (스펙 4절) · 월 순수익 게이트 (4-A) ──
from types import SimpleNamespace

from src.llm.exit_advisor import EXIT_PROMPT_TEMPLATE_VERSION
from src.logger.trade_store import ExitReviewRow, MonthlySummary

LONG_TIME = "## 시간\n" + "새 시간 지침 " * 10


class FakeExitTuner:
    def __init__(self):
        self.result = None
        self.calls = []

    def tune(self, stats, rows, sections, locked_text, why_history="", timeout_seconds=120.0):
        self.calls.append(rows)
        return self.result


def seed_reviews(workflow, count, version=EXIT_PROMPT_TEMPLATE_VERSION, outcome="missed"):
    for i in range(count):
        workflow.trade_store.save_exit_review(
            ExitReviewRow(
                day=date(2026, 9, 1 + i), ticker="005930", name="삼성전자", exit_prompt_version=version,
                net_pnl=-500.0, net_return=-0.005, peak_return=0.015, outcome=outcome,
                exit_reason="day_end", decision_count=5,
            )
        )


def tuning_workflow(tmp_path, reviews=10):
    workflow = build_exit_workflow(tmp_path)
    workflow.exit_tuner = FakeExitTuner()
    seed_reviews(workflow, reviews)
    return workflow


def monthly(workflow, net_pnl):
    workflow.trade_store.monthly_summary = lambda year, month, up_to: MonthlySummary(
        realized_pnl=net_pnl, fees=0.0
    )


def test_exit_tuning_applies_one_section_and_mails(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(change=True, reason="놓침 10건", sections={"time": LONG_TIME})

    workflow.tune_exit_prompt(date(2026, 9, 29))

    assert workflow.exit_prompt_store.load_sections()["time"] == LONG_TIME
    assert workflow.exit_prompt_store.load_version() == "20260929"
    assert "매도 프롬프트 수정" in workflow.email.sent[-1][0]


def test_exit_tuning_waits_for_ten_reviews(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=9)
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_unknown_reviews_do_not_count_toward_the_gate(tmp_path):
    workflow = tuning_workflow(tmp_path, reviews=9)
    workflow.trade_store.save_exit_review(
        ExitReviewRow(
            day=date(2026, 9, 20), ticker="000660", name="", exit_prompt_version=EXIT_PROMPT_TEMPLATE_VERSION,
            net_pnl=None, net_return=None, peak_return=None, outcome="unknown", exit_reason="", decision_count=0,
        )
    )
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_rejects_two_sections(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner.result = SimpleNamespace(
        change=True, reason="r", sections={"time": LONG_TIME, "criteria": "## 판단 기준\n" + "x" * 60}
    )
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_prompt_store.load_version() == EXIT_PROMPT_TEMPLATE_VERSION
    assert workflow.email.sent == []


def test_exit_tuning_is_skipped_when_the_month_is_good(tmp_path):
    """총자산 12,000,000원에 이번 달 순손익 600,000원 → 기준자산 11,400,000원 대비 +5.26%."""
    workflow = tuning_workflow(tmp_path)
    monthly(workflow, 600_000.0)
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_runs_below_the_monthly_bar(tmp_path):
    workflow = tuning_workflow(tmp_path)
    monthly(workflow, 500_000.0)  # +4.35%
    workflow.exit_tuner.result = SimpleNamespace(change=False, reason="표본 부족", sections={})
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert len(workflow.exit_tuner.calls) == 1


def test_exit_tuning_is_skipped_when_the_balance_is_unknown(tmp_path):
    workflow = tuning_workflow(tmp_path)

    def boom():
        raise RuntimeError("token")

    workflow.account.get_balance_snapshot = boom
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.exit_tuner.calls == []


def test_exit_tuning_skips_without_a_tuner(tmp_path):
    workflow = tuning_workflow(tmp_path)
    workflow.exit_tuner = None
    workflow.tune_exit_prompt(date(2026, 9, 29))
    assert workflow.email.sent == []
```

`tests/test_core/test_daily_workflow.py`의 tune_prompt 테스트 묶음 끝에 추가(추천 튜너에도 게이트가 걸리는지):

```python
def test_tune_prompt_is_skipped_when_the_month_is_good(tmp_path):
    """월 순수익 게이트는 추천 프롬프트 수정에도 걸린다 (스펙 2026-09-22 4-A)."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 8)
    _verified_recommendation(workflow, day)
    workflow.trade_store.monthly_summary = lambda year, month, up_to: MonthlySummary(
        realized_pnl=600_000.0, fees=0.0
    )

    workflow.tune_prompt(day)

    assert workflow.tuner.calls == []
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_llm/test_exit_tuner.py tests/test_core/test_exit_review_workflow.py tests/test_core/test_daily_workflow.py -q -k "tun or group or sanitize or prompt"`
Expected: FAIL — `ModuleNotFoundError: src.llm.exit_tuner`

- [ ] **Step 4: `tuner.sanitize_sections` 일반화** — `src/llm/tuner.py`

`from typing import Dict, List, Optional, Sequence`로 바꾸고 함수 시그니처와 두 곳을 바꾼다:

```python
def sanitize_sections(
    raw: Dict[str, str],
    allowed: Sequence[str] = PROMPT_SECTION_ORDER,
    max_sections: int = MAX_SECTIONS_PER_CHANGE,
) -> Dict[str, str]:
```

본문의 `MAX_SECTIONS_PER_CHANGE` 두 곳 → `max_sections`, `if key not in PROMPT_SECTION_ORDER:` → `if key not in allowed:`. docstring 첫 줄 다음에 "매도 프롬프트 튜너(스펙 2026-09-22)도 허용 키·최대 절 수만 바꿔 같은 규약을 쓴다." 한 줄을 넣는다.

- [ ] **Step 5: `src/llm/exit_tuner.py` 구현**

```python
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.core.exit_review import CAPTURED, MISSED, NO_CHANCE, UNKNOWN
from src.llm.exit_advisor import EXIT_PROMPT_SECTION_ORDER
from src.llm.recommender import MAX_TOKENS
from src.llm.tuner import TUNE_KEY_SECTIONS, TuneResult, parse_tune_response
from src.logger.trade_store import ExitReviewRow

logger = logging.getLogger(__name__)

EXIT_TUNE_PROMPT_TEMPLATE_VERSION = "v1"

# 한 번에 고칠 수 있는 절 수. 매도 판단은 결과에 시장 운이 크게 섞여, 두 절을 함께 바꾸면
# 어느 쪽이 효과였는지 추천 쪽보다도 가리기 어렵다 (스펙 2026-09-22 4.3).
MAX_SECTIONS_PER_CHANGE = 1
# 현재 버전으로 검증된 종목(판정 불가 제외)이 이만큼 쌓이기 전에는 튜너를 부르지 않는다.
# 하루 2종목이면 약 5거래일이다 — 하루치 우연으로 고치지 않기 위한 코드 게이트다.
MIN_REVIEWS_FOR_TUNING = 10

EXIT_TUNE_SCHEMA = {
    "type": "object",
    "properties": {
        "change": {"type": "boolean", "description": "프롬프트를 고칠지 여부"},
        "reason": {"type": "string", "description": "고치는/고치지 않는 이유와 근거 수치"},
        TUNE_KEY_SECTIONS: {
            "type": "object",
            "properties": {
                key: {"type": "string", "description": f"{key} 절의 전문 (## 헤더 포함)"}
                for key in EXIT_PROMPT_SECTION_ORDER
            },
            "additionalProperties": False,
        },
    },
    "required": ["change", "reason", TUNE_KEY_SECTIONS],
    "additionalProperties": False,
}


@dataclass
class ExitVersionStats:
    """매도 프롬프트 한 버전의 성과 — 판정 불가(unknown)는 뺀다."""

    version: str
    count: int
    captured: int
    missed: int
    no_chance: int
    net_pnl_total: float
    avg_net_return: float
    avg_missed_gap: Optional[float]  # 고점이 플러스였던 종목의 평균 (고점 − 최종), 없으면 None


def group_by_version(rows: List[ExitReviewRow]) -> List[ExitVersionStats]:
    buckets: Dict[str, List[ExitReviewRow]] = {}
    for row in rows:
        if row.outcome == UNKNOWN:
            continue
        buckets.setdefault(row.exit_prompt_version or "(없음)", []).append(row)

    stats = []
    for version, group in buckets.items():
        returns = [r.net_return for r in group if r.net_return is not None]
        gaps = [
            r.peak_return - r.net_return
            for r in group
            if r.peak_return is not None and r.peak_return > 0 and r.net_return is not None
        ]
        stats.append(
            ExitVersionStats(
                version=version,
                count=len(group),
                captured=sum(1 for r in group if r.outcome == CAPTURED),
                missed=sum(1 for r in group if r.outcome == MISSED),
                no_chance=sum(1 for r in group if r.outcome == NO_CHANCE),
                net_pnl_total=sum(r.net_pnl or 0.0 for r in group),
                avg_net_return=sum(returns) / len(returns) if returns else 0.0,
                avg_missed_gap=sum(gaps) / len(gaps) if gaps else None,
            )
        )
    return sorted(stats, key=lambda s: s.version)


def build_exit_tune_system_prompt() -> str:
    return f"""당신은 장중 매도 판단 프롬프트를 성과 데이터에 근거해 다듬는 편집자입니다.

## 역할
아래 프롬프트로 내린 매도 판단의 실제 결과를 보고, 프롬프트의 편집 가능한 절을 고칠지
판단합니다. 고칠 필요가 없으면 고치지 않는 것이 정상입니다.

## 잣대
성과는 **순손익(수수료·세금을 뺀 값)**으로만 봅니다. "순이익 확정"이 늘고 "순이익 기회를
놓침"과 순손실이 줄어야 개선입니다. 덜 잃은 것은 개선이 아닙니다.

## 고칠 수 있는 절
`loss_positions`(손실 중인 종목), `time`(시간), `criteria`(판단 기준) — 이 셋뿐입니다.
`역할`·`기본은 보유입니다`·`reason 작성 지침`은 **고칠 수 없습니다.** 참고용으로 보여줄
뿐이니, 고치는 절이 그 절들과 모순되지 않게 하는 데만 쓰십시오.

## 규칙
1. **한 번에 {MAX_SECTIONS_PER_CHANGE}절만** 고치십시오. 더 많이 고치면 전체가 폐기됩니다.
2. 고치는 절은 `##` 헤더 줄을 포함한 **전문**을 주십시오.
3. `reason`에는 **근거로 삼은 수치를 인용**하십시오. ("v2 12건 중 놓침 5건, 평균 놓친 폭 1.8%p"처럼)
4. 표본이 부족하거나 신호가 뚜렷하지 않으면 고치지 마십시오 (`change: false`와 그 이유).
5. 이전 변경 이력이 함께 주어집니다. 직전에 고친 것을 되돌리는 방향으로 다시 고치지
   마십시오 — 그러면 프롬프트가 왔다 갔다 하기만 합니다.
6. 데이터가 말하지 않는 것을 지어내지 마십시오."""


def _pct(ratio: Optional[float]) -> str:
    return "모름" if ratio is None else f"{ratio * 100:+.2f}%"


def build_exit_tune_user_prompt(
    stats: List[ExitVersionStats],
    rows: List[ExitReviewRow],
    sections: Dict[str, str],
    locked_text: str,
    why_history: str,
) -> str:
    lines = ["## 버전별 성과 (판정 불가 제외)"]
    for s in stats:
        gap = "-" if s.avg_missed_gap is None else f"{s.avg_missed_gap * 100:.2f}%p"
        lines.append(
            f"- {s.version}: {s.count}건 | 확정 {s.captured} / 놓침 {s.missed} / 기회 없음 {s.no_chance} | "
            f"순손익 합계 {s.net_pnl_total:+,.0f}원 | 평균 순손익률 {s.avg_net_return * 100:+.2f}% | "
            f"평균 놓친 폭 {gap}"
        )
    if not stats:
        lines.append("- 아직 검증된 매도가 없습니다.")

    lines.append("\n## 개별 결과")
    for r in rows:
        pnl = "모름" if r.net_pnl is None else f"{r.net_pnl:+,.0f}원"
        lines.append(
            f"- {r.day} {r.ticker} {r.name} ({r.exit_prompt_version or '-'}): {r.outcome} | "
            f"순손익 {pnl} ({_pct(r.net_return)}) | 고점 {_pct(r.peak_return)} | "
            f"청산 {r.exit_reason or '-'} | 판단 {r.decision_count}회"
        )
        if r.review:
            lines.append(f"  평가: {r.review}")

    lines.append("\n## 고칠 수 없는 절 (참고용)")
    lines.append(locked_text)

    lines.append("\n## 현재 고칠 수 있는 절")
    for key in EXIT_PROMPT_SECTION_ORDER:
        lines.append(f"\n### {key}\n{sections.get(key, '')}")

    lines.append("\n## 이전 변경 이력")
    lines.append(why_history or "(없음 — 아직 고친 적이 없습니다)")

    lines.append("\n위 결과를 근거로 프롬프트를 고칠지 판단하고, 고친다면 그 절의 전문을 주십시오.")
    return "\n".join(lines)


class ExitPromptTuner:
    """매도 프롬프트를 검증 결과로 다듬는 모듈 — 추천 튜너(`PromptTuner`)와 같은 꼴."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def tune(
        self,
        stats: List[ExitVersionStats],
        rows: List[ExitReviewRow],
        sections: Dict[str, str],
        locked_text: str,
        why_history: str = "",
        timeout_seconds: float = 120.0,
    ) -> Optional[TuneResult]:
        """수정 판단. 실패하면 None — 그날은 아무것도 고치지 않는다."""
        user_prompt = build_exit_tune_user_prompt(stats, rows, sections, locked_text, why_history)
        logger.info(
            "매도 프롬프트 수정 요청 (exit_tune_prompt_version=%s, %d건):\n%s",
            EXIT_TUNE_PROMPT_TEMPLATE_VERSION, len(rows), user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_exit_tune_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": EXIT_TUNE_SCHEMA}},
            )
        except Exception:
            logger.exception("매도 프롬프트 수정 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("매도 프롬프트 수정 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("매도 프롬프트 수정 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_tune_response(raw_text)
        except Exception:
            logger.exception("매도 프롬프트 수정 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
```

- [ ] **Step 6: 메일 템플릿** — `src/notification/templates.py`, `exit_review_email` 다음에

```python
def exit_prompt_tuning_email(
    today: date,
    old_version: str,
    new_version: str,
    reason: str,
    stats: List["ExitVersionStats"],
    before: Dict[str, str],
    after: Dict[str, str],
) -> tuple[str, str]:
    """매도 프롬프트를 자동 수정한 날 나가는 이메일 (스펙 2026-09-22 4.3). 고친 날만 발송한다."""
    subject = f"[AutoTrade] {today:%Y-%m-%d} 매도 프롬프트 수정 ({old_version} → {new_version})"
    lines = [
        f"{today:%Y-%m-%d} 매도 프롬프트를 자동으로 수정했습니다 ({old_version} → {new_version}).",
        "",
        "## 수정 이유",
        reason or "(없음)",
        "",
        "## 근거 — 버전별 성과 (순손익 기준)",
    ]
    for s in stats:
        lines.append(
            f" - {s.version}: {s.count}건 | 확정 {s.captured} / 놓침 {s.missed} / 기회 없음 {s.no_chance} | "
            f"순손익 합계 {s.net_pnl_total:+,.0f}원"
        )
    if not stats:
        lines.append(" - (비교할 성과 데이터 없음)")
    for key in sorted(after):
        lines.extend(["", f"## 바뀐 절: {key}", "", "[이전]", before.get(key, "(없음)"), "", "[이후]", after[key]])
    lines.extend([
        "",
        f"※ 되돌리려면 data/exit_prompt/history/{old_version}/ 의 파일들을 data/exit_prompt/ 로 복사하십시오.",
        "※ 수정된 프롬프트는 다음 AI 매도 판단부터 적용됩니다 (엔진 재시작 불필요).",
    ])
    return subject, "\n".join(lines)
```

- [ ] **Step 7: 워크플로 구현** — `src/core/daily_workflow.py`

import 추가: `from src.llm import exit_tuner as exit_tuner_module`, `from src.llm.exit_advisor import EXIT_PROMPT_SECTION_ORDER, build_exit_locked_text, make_exit_prompt_store`(Task 8에서 넣은 줄과 합친다), `from src.logger.trade_store import MonthlySummary, TradeStore`(기존 줄에 합친다).

`send_daily_report`의 월 기준자산 두 줄을 헬퍼로 바꾼다:

```python
        snapshot = self.account.get_balance_snapshot()
        monthly = self._monthly_summary_with_base(today, snapshot)
        # 연 단위 기간초 자산 추정치 — 월과 같은 식이다. **연 단위는 그만큼 더 거칠다** —
        # 연중 입출금이 있으면 그 금액만큼 어긋난다 (PRD 5.11).
        yearly.base_asset = snapshot.total_asset - yearly.net_pnl
```
(기존 `monthly = self.trade_store.monthly_summary(...)` 줄과 `monthly.base_asset = ...` 줄은 지운다. 기존 주석은 헬퍼 docstring으로 옮긴다.)

`review_exits` 앞(또는 `_why_history` 다음)에 헬퍼와 게이트를 추가한다:

```python
    def _monthly_summary_with_base(self, today: date, snapshot) -> MonthlySummary:
        """이번 달 누적 실적 + 월초 자산 추정치 — 일일 리포트와 월 순수익 게이트가 함께 쓴다.

        월초 자산 추정치 = 현재 총자산 - 이번 달 순손익. 수수료·세금도 계좌에서 빠져나간
        금액이므로 실현손익이 아니라 순손익을 되돌려야 월초 자산에 맞는다 (PRD 5.11).
        """
        monthly = self.trade_store.monthly_summary(today.year, today.month, up_to=today)
        monthly.base_asset = snapshot.total_asset - monthly.net_pnl
        return monthly

    def _tuning_blocked_by_monthly_return(self, today: date) -> bool:
        """이번 달 순수익률이 기준 이상이면 True — 두 프롬프트 모두 고치지 않는다 (스펙 4-A).

        수익률을 알 수 없으면(잔고 조회 실패·기준자산 0 이하) 역시 True — 아무것도 바꾸지
        않는 것이 기본 상태다.
        """
        try:
            snapshot = self.account.get_balance_snapshot()
        except Exception:
            logger.warning("잔고 조회 실패 — 월 순수익을 알 수 없어 프롬프트를 고치지 않습니다.", exc_info=True)
            return True
        monthly = self._monthly_summary_with_base(today, snapshot)
        if monthly.base_asset <= 0:
            logger.warning("월초 자산 추정치가 0 이하라 월 순수익을 알 수 없어 프롬프트를 고치지 않습니다.")
            return True
        ratio = monthly.net_pnl / monthly.base_asset
        if ratio >= self.tune_skip_monthly_return_ratio:
            logger.info(
                "이번 달 순수익 %+.2f%% ≥ 기준 %.1f%% — 프롬프트를 고치지 않습니다 (%s).",
                ratio * 100, self.tune_skip_monthly_return_ratio * 100, today,
            )
            return True
        return False
```

`tune_prompt`의 `if self.tuner is None: return` 다음 줄에:

```python
        if self._tuning_blocked_by_monthly_return(today):
            return
```

`tune_prompt` 다음에 추가:

```python
    def tune_exit_prompt(self, today: Optional[date] = None) -> None:
        """15:35 — 매도 판단 검증 결과를 보고 매도 프롬프트의 편집 가능한 절을 고친다 (스펙 4절).

        매도 판단 검증 **다음**에 돈다. 게이트는 순서대로 월 순수익(4-A) → 표본 10건(4.3)이고,
        둘 다 LLM 호출 전에 코드가 본다. 고친 날만 메일이 나가고, 고치지 않는 날이 정상이다.
        """
        today = today or date.today()
        if self.exit_tuner is None:
            return
        if self._tuning_blocked_by_monthly_return(today):
            return

        old_version = self.exit_prompt_store.load_version()
        verified = self.trade_store.count_exit_reviews(old_version)
        if verified < exit_tuner_module.MIN_REVIEWS_FOR_TUNING:
            logger.info(
                "매도 프롬프트 %s로 검증된 종목이 %d건이라(기준 %d건) 고치지 않습니다 (%s).",
                old_version, verified, exit_tuner_module.MIN_REVIEWS_FOR_TUNING, today,
            )
            return

        rows = self.trade_store.recent_exit_reviews()
        stats = exit_tuner_module.group_by_version(rows)
        before = self.exit_prompt_store.load_sections()
        result = self.exit_tuner.tune(
            stats, rows, before, build_exit_locked_text(), self.exit_prompt_store.why_history()
        )
        if result is None or not result.change:
            logger.info(
                "매도 프롬프트를 고치지 않습니다 (%s): %s", today, getattr(result, "reason", "판단 실패")
            )
            return

        sections = tuner_module.sanitize_sections(
            result.sections, EXIT_PROMPT_SECTION_ORDER, exit_tuner_module.MAX_SECTIONS_PER_CHANGE
        )
        if not sections:
            logger.warning("매도 프롬프트 수정안이 안전장치에 전부 걸렸습니다 — 그대로 둡니다 (%s).", today)
            return

        try:
            new_version = self.exit_prompt_store.save(sections, result.reason, today)
        except OSError:
            logger.exception("매도 프롬프트 파일 쓰기에 실패했습니다 — 그대로 둡니다.")
            return

        after = self.exit_prompt_store.load_sections()
        subject, body = templates.exit_prompt_tuning_email(
            today,
            old_version,
            new_version,
            result.reason,
            stats,
            {key: before[key] for key in sections},
            {key: after[key] for key in sections},
        )
        self.email.send(subject, body)
        logger.info("매도 프롬프트 수정 메일 발송 (%s, %s → %s)", today, old_version, new_version)
```

- [ ] **Step 8: 통과 확인**

Run: `.venv/Scripts/python.exe -m pytest tests -q`
Expected: 전부 PASS (일일 리포트 테스트가 월 수익률 값을 단정하면 그대로 통과해야 한다 — 헬퍼는 같은 식이다)

- [ ] **Step 9: 커밋**

```bash
git add src/llm/tuner.py src/llm/exit_tuner.py src/core/daily_workflow.py src/notification/templates.py tests/test_llm/test_exit_tuner.py tests/test_core/test_exit_review_workflow.py tests/test_core/test_daily_workflow.py
git commit -F - <<'EOF'
매도 프롬프트 자동 수정과 월 순수익 게이트를 추가한다

현재 버전으로 검증된 종목이 10건 이상일 때만, 한 번에 한 절만 고친다. 이번 달
순수익이 기준 이상이면 추천·매도 두 프롬프트 모두 고치지 않는다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 10: 15:35 스케줄·실행 큐·런타임 배선

**Files:**
- Modify: `src/core/actions.py` (`SCHEDULED_ACTIONS`, `step_factories`)
- Modify: `src/core/runtime.py` (`build_runtime`의 `DailyWorkflow(...)`·`ExitAdvisor(...)`·스케줄 등록, import)
- Test: `tests/test_core/test_actions.py` (가짜 런타임 보강 + 테스트 추가)

**Interfaces:**
- Consumes: Task 8 `review_exits`, Task 9 `tune_exit_prompt`, Task 4 `make_exit_prompt_store`/`ExitAdvisor(settings, prompt_store)`, Task 7 `ExitReviewer`, Task 9 `ExitPromptTuner`, Task 6 `settings.tune_skip_monthly_return_ratio`
- Produces: 스케줄 액션 `"review_exits"`, `"tune_exit_prompt"` (둘 다 `touches_orders=False`)

- [ ] **Step 1: 테스트** — `tests/test_core/test_actions.py`

`make_runtime`의 `workflow = SimpleNamespace(...)`에 두 줄 추가:

```python
        review_exits=lambda: calls.append("review_exits"),
        tune_exit_prompt=lambda: calls.append("tune_exit_prompt"),
```

파일 끝에:

```python
@pytest.mark.parametrize("action", ["review_exits", "tune_exit_prompt"])
def test_exit_review_steps_are_scheduled_only_and_off_the_loop(action):
    """LLM 호출이 걸리므로 루프 스레드를 쓰지 않는다. 주문을 내지 않는다 (스펙 2026-09-22 3.1)."""
    assert action in SCHEDULED_ACTIONS
    assert action not in MANUAL_ACTIONS
    assert action not in ORDER_ACTIONS
    calls = []
    steps = manual_steps(make_runtime(calls), action)
    assert [s.touches_orders for s in steps] == [False]
    for step in steps:
        step.run()
    assert calls == [action]
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/Scripts/python.exe -m pytest tests/test_core/test_actions.py -q`
Expected: FAIL — `KeyError: 'review_exits'` 또는 assert 실패

- [ ] **Step 3: actions 구현** — `src/core/actions.py`

`SCHEDULED_ACTIONS`에 두 줄 추가(`"tune_prompt"` 다음):

```python
    "review_exits": "매도 판단 검증 메일 (스케줄)",
    "tune_exit_prompt": "매도 프롬프트 자동 수정 (스케줄)",
```

`step_factories`의 `"tune_prompt"` 항목 다음에:

```python
        # 체결 동기화·LLM 호출이 걸리므로 루프 스레드를 쓰지 않는다. 주문을 내지 않는다.
        "review_exits": lambda: [
            ManualStep(SCHEDULED_ACTIONS["review_exits"], runtime.workflow.review_exits)
        ],
        # LLM 호출과 파일 쓰기가 걸리므로 루프 스레드를 쓰지 않는다. 주문을 내지 않는다.
        "tune_exit_prompt": lambda: [
            ManualStep(SCHEDULED_ACTIONS["tune_exit_prompt"], runtime.workflow.tune_exit_prompt)
        ],
```

- [ ] **Step 4: runtime 배선** — `src/core/runtime.py`

import 추가:

```python
from src.llm.exit_advisor import ExitAdvisor, make_exit_prompt_store  # 기존 ExitAdvisor import 줄을 바꾼다
from src.llm.exit_reviewer import ExitReviewer
from src.llm.exit_tuner import ExitPromptTuner
```

`build_runtime`에서 `workflow = DailyWorkflow(` 바로 위에:

```python
    # 매도 프롬프트 저장소 하나를 AI 매도 판단과 15:35 튜너가 함께 본다 — 튜너가 고친 절이
    # 재시작 없이 다음 판단에 반영된다 (스펙 2026-09-22 4.1)
    exit_prompt_store = make_exit_prompt_store()
```

`DailyWorkflow(...)` 인자에 `tuner=PromptTuner(settings),` 다음으로:

```python
        exit_reviewer=ExitReviewer(settings),
        exit_tuner=ExitPromptTuner(settings),
        exit_prompt_store=exit_prompt_store,
        tune_skip_monthly_return_ratio=settings.tune_skip_monthly_return_ratio,
```

`exit_advisor = ExitAdvisor(settings)` → `exit_advisor = ExitAdvisor(settings, prompt_store=exit_prompt_store)`.

스케줄 등록 튜플의 `(REPORT_TIME, "tune_prompt"),` 다음에:

```python
        # 추천 쪽 다음에 등록한다 — 같은 시각의 잡은 등록 순서대로 큐에 들어간다.
        # 검증이 먼저 끝나야 튜너가 오늘 결과까지 본다 (스펙 2026-09-22 3.1)
        (REPORT_TIME, "review_exits"),
        (REPORT_TIME, "tune_exit_prompt"),
```

- [ ] **Step 5: 통과 확인 + 조립 확인**

Run: `.venv/Scripts/python.exe -m pytest tests -q`
Expected: 전부 PASS

Run(조립만 — API를 부르지 않는다):
```bash
.venv/Scripts/python.exe -c "import src.core.runtime, src.ui.main_window; print('import ok')"
```
Expected: `import ok`

- [ ] **Step 6: 커밋**

```bash
git add src/core/actions.py src/core/runtime.py tests/test_core/test_actions.py
git commit -F - <<'EOF'
15:35에 매도 판단 검증과 매도 프롬프트 수정을 추천 쪽 뒤에 이어 돌린다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 11: 문서

**Files:**
- Modify: `주식자동매매_PRD.md` (5.5-B "AI 매도 판단"·"이익 반납 감시", 5.12/5.13 근처 새 절, 10절 확정 항목)
- Modify: `CLAUDE.md` (15:35 단계 목록, `data/` 예외, 이익 반납 감시 서술)
- Modify: `docs/superpowers/specs/2026-09-22-ai-exit-review-tuning-design.md` (구현 중 확정된 세부 반영)

- [ ] **Step 1: PRD** — 다음을 반영한다.
  - 5.5-B "AI 매도 판단": 판단마다 `ai_exit_decisions`에 기록, 매도 프롬프트 편집 가능 절이 `data/exit_prompt/`에 있고 매 판단마다 읽는다(재시작 불필요), 버전은 날짜 표기.
  - 5.5-B "이익 반납 감시": 고점을 `position_peaks`에 5초 단위로 남기고 엔진 시작 때 복원, AI 매도 판단이 꺼져도 고점은 잰다(앞당김은 안 함).
  - 새 절 "매도 판단 검증 (15:35)": 대상(그날 매도 체결 종목), 분류 4종과 규칙(순손익 > 0이면 고점 몰라도 확정), 평가문 잣대, 메일.
  - 새 절 "매도 프롬프트 자동 수정 (15:35)": 편집 가능 3절·잠긴 3절, 게이트 순서(월 순수익 → 표본 10건), 1절 제한, 되돌리는 방법.
  - 5.13 "프롬프트 자동 수정": 월 순수익 게이트(`TUNE_SKIP_MONTHLY_RETURN_PERCENT`, 기본 5, UI 1~10% 0.5 단위, 조회 실패 시 안 고침)가 추천 쪽에도 걸린다.
  - 10절: "AI 매도 판단 검증과 매도 프롬프트 자동 수정 (확정 2026-09-22)" — 계기(2026-09-22), 받아들인 위험(엔진이 꺼진 구간 고점은 없음, 고점과 최종 순손익의 슬리피지 기준 차이, 자동 수정이 표본 우연에 끌려갈 위험과 그 대책).

- [ ] **Step 2: CLAUDE.md**
  - 스레드/이벤트 루프 절의 15:35 설명을 "리포트 → 추천 검증 → 추천 프롬프트 자동 수정 → 매도 판단 검증 → 매도 프롬프트 자동 수정 다섯 단계"로 고친다.
  - "설정은 `.env` 하나로 통일"의 `data/prompt/` 예외 문단에 `data/exit_prompt/`(매도 프롬프트 편집 가능 3절)를 함께 적는다.
  - 1호 전략 문단의 15:35 설명 끝에 매도 판단 검증·매도 프롬프트 수정과 월 순수익 게이트를 한두 문장으로 추가한다.
  - "이익 반납 감시" 항목에 "고점은 DB(`position_peaks`)에 남기고 재시작 때 복원한다"를 추가한다.

- [ ] **Step 3: 스펙 갱신** — 3.2 분류 규칙에 "순손익 > 0이면 고점을 몰라도 `captured`"를 명시하고, 4.2 튜너 입력에서 "판단 이력 요약"을 "검증 평가문(판단 이력을 요약한다)"으로 고친다.

- [ ] **Step 4: 전체 테스트**

Run: `.venv/Scripts/python.exe -m pytest tests -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add 주식자동매매_PRD.md CLAUDE.md docs/superpowers/specs/2026-09-22-ai-exit-review-tuning-design.md
git commit -F - <<'EOF'
AI 매도 판단 검증과 매도 프롬프트 자동 수정을 문서에 반영한다

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

## Self-Review (작성자 확인 결과)

- **스펙 대비 누락**: 2.1(Task 1·5), 2.2(Task 1·2), 3.1(Task 10), 3.2·3.3(Task 7), 3.4(Task 1·8), 4.1(Task 3·4), 4.2·4.3(Task 9), 4-A(Task 6·9), 5(각 Task의 예외 삼킴 테스트), 6(각 Task), 7(Task 11). 누락 없음.
- **스펙과 다르게 확정한 두 가지** (Task 11 Step 3에서 스펙에 반영):
  1. 순손익 > 0이면 고점을 몰라도 `captured` — 확정했으면 고점은 판정에 필요 없다.
  2. 튜너 입력의 "판단 이력 요약"은 검증 평가문으로 대신한다 — 평가문이 이미 판단 이력을 수치로 요약하고, 원 이력까지 넣으면 10거래일치 입력이 커진다.
- **타입 일관성**: `ExitReviewRow`/`AIExitDecisionRow` 필드명, `make_exit_prompt_store`, `flush_exit_peaks(positions=None, force=False)`, `sanitize_sections(raw, allowed, max_sections)`, `tune_skip_monthly_return_ratio`를 모든 Task에서 같은 이름으로 쓴다.
