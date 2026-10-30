import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.core.events import FillRecord, OrderResult, OrderSide, OrderStatus, format_stock

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL,
    filled_price REAL,
    avg_price REAL,
    realized_pnl REAL,
    error_message TEXT,
    timestamp TEXT NOT NULL,
    name TEXT,
    commission REAL,
    tax REAL,
    exit_reason TEXT
);
"""

# 이미 만들어진 DB에 뒤늦게 추가된 컬럼들 — 없을 때만 붙인다
MIGRATIONS = (
    ("name", "ALTER TABLE trades ADD COLUMN name TEXT"),
    ("commission", "ALTER TABLE trades ADD COLUMN commission REAL"),
    ("tax", "ALTER TABLE trades ADD COLUMN tax REAL"),
    ("exit_reason", "ALTER TABLE trades ADD COLUMN exit_reason TEXT"),
)

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

# 금요일 규칙 자동 평가 기록 — 하루 한 줄 (스펙 2026-10-29 5.5). 연속·휴지 판정의 재료다.
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

# 부분체결도 실제 매매이므로 집계에 포함한다
FILLED_STATUSES = (OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value)

# 결과가 확정된 매도 — 체결됐거나(집계에 들어감) 거부·취소로 끝났거나(들어갈 것이 없음).
# 그 밖의 상태(접수만 된 pending)는 아직 결과를 모르는 주문이다.
SETTLED_STATUSES = FILLED_STATUSES + (OrderStatus.REJECTED.value, OrderStatus.CANCELLED.value)


def _day_range(day: date) -> Tuple[str, str]:
    """하루의 시작/끝 타임스탬프 — timestamp가 ISO 문자열이라 문자열 비교로 걸러진다."""
    return (
        datetime.combine(day, datetime.min.time()).isoformat(),
        datetime.combine(day, datetime.max.time()).isoformat(),
    )


@dataclass
class TradeRow:
    """리포트 표의 한 줄 — 종목 하나의 당일 매수/매도를 묶은 것."""

    ticker: str
    name: Optional[str]
    quantity: int
    buy_price: float
    sell_price: Optional[float]  # None이면 당일 청산되지 않았다
    pnl: Optional[float]
    fees: float = 0.0

    @property
    def label(self) -> str:
        return format_stock(self.ticker, self.name)

    @property
    def cost(self) -> float:
        return self.buy_price * self.quantity

    @property
    def return_pct(self) -> Optional[float]:
        if self.sell_price is None or self.buy_price <= 0:
            return None
        return (self.sell_price - self.buy_price) / self.buy_price * 100


@dataclass
class DailySummary:
    day: date
    buy_count: int
    sell_count: int
    realized_pnl: float
    trades: List[TradeRow] = field(default_factory=list)
    cost: float = 0.0            # 청산된 건의 투입원가 합 — 수익률의 분모
    fees: float = 0.0            # 당일 수수료 + 세금 (매수분 포함)
    rejected_count: int = 0

    @property
    def return_pct(self) -> float:
        return self.realized_pnl / self.cost * 100 if self.cost else 0.0

    @property
    def net_pnl(self) -> float:
        """수수료·세금을 뺀 실수령 손익."""
        return self.realized_pnl - self.fees

    @property
    def net_return_pct(self) -> float:
        return self.net_pnl / self.cost * 100 if self.cost else 0.0


@dataclass
class MonthlySummary:
    """기간 누적 실적 — 리포트의 '이번 달 누적'과 '올해 누적' 블록이 함께 쓴다.

    이름은 월 블록만 있던 시절의 것이다. 필드도 파생값도 기간 길이와 무관해 연 집계가
    그대로 들어맞아, 같은 모양의 dataclass를 하나 더 만드는 대신 재사용한다 (2026-09-01).

    수익률의 분모(base_asset)는 계좌 잔고를 봐야 알 수 있어 TradeStore가 채우지 못한다.
    호출부(DailyWorkflow)가 월초 자산 추정치를 넣어준다.
    """

    realized_pnl: float
    fees: float
    base_asset: float = 0.0

    @property
    def net_pnl(self) -> float:
        """수수료·세금을 뺀 실수령 손익 — 이번 달 실제 차익."""
        return self.realized_pnl - self.fees

    @property
    def return_pct(self) -> float:
        return self.realized_pnl / self.base_asset * 100 if self.base_asset else 0.0

    @property
    def net_return_pct(self) -> float:
        return self.net_pnl / self.base_asset * 100 if self.base_asset else 0.0


@dataclass
class DailyPoint:
    """누적 꺾은선 그래프의 한 점 — 매매가 있었던 하루.

    연 그래프에서는 **매매가 있었던 한 달**을 뜻하고 `day`에 그 달의 1일이 들어간다
    (2026-09-01). 눈금 문구만 다를 뿐 그리는 방식이 같아 점 타입을 나누지 않았다.
    """

    day: date
    net_pnl: float      # 그 구간의 순손익 (실현손익 - 수수료·세금)
    cumulative: float   # 기간 시작부터 그 점까지 누적 순손익


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


class TradeStore:
    """매수/매도 체결 내역을 SQLite에 영속 저장한다.

    5.7절 일별 손익 요약, 5.11절 15:35 성과 리포트(일별/월별 누적)의 데이터 원천.
    개인 프로젝트 규모(파일 하나, 별도 서버 불필요)에 맞춰 파일 로그 대신 SQLite로 결정 (10절 Open Question).

    주문 접수 시점에는 체결 여부를 알 수 없어 pending으로 들어가므로, 집계 전에
    apply_fills()로 체결 결과를 덮어써야 한다.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            # 1호 전략의 추천·수급 테이블(recommendations, investor_flow)은 2026-10-23 폐지와 함께
            # 더 만들지 않는다 — 기존 DB에 있는 표와 데이터는 그대로 남는다. exit_reviews는 매도 판단
            # 검증이 2호용으로 복구되면서(2026-10-29) 다시 만든다.
            conn.execute(SCHEMA)
            conn.execute(AI_EXIT_DECISION_SCHEMA_SQL)
            conn.execute(POSITION_PEAK_SCHEMA_SQL)
            conn.execute(EXIT_REVIEW_SCHEMA_SQL)
            conn.execute(RULE_EVALUATION_SCHEMA_SQL)
            self._migrate(conn, "trades", MIGRATIONS)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(
        conn: sqlite3.Connection, table: str, migrations: Tuple[Tuple[str, str], ...]
    ) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in migrations:
            if column not in existing:
                conn.execute(ddl)
                logger.info("%s 테이블에 %s 컬럼을 추가했습니다.", table, column)

    def record_fill(
        self,
        result: OrderResult,
        avg_price: Optional[float] = None,
        exit_reason: Optional[str] = None,
    ) -> None:
        """체결(또는 거부) 결과를 남긴다.

        `exit_reason`은 매도가 어느 경로로 나갔는지다 — `take_profit`/`stop_loss`(실시간
        감시), `day_end`(15:15 강제청산), `manual`/`manual_selected`(UI 즉시 실행). 매수와
        전략 신호 매도는 None이다. 없으면 성과를 되짚을 때 규칙이 판 것인지 사람이 판 것인지
        구분하려고 로그를 파싱해야 한다 (확정 2026-08-12).
        """
        realized_pnl = None
        if (
            result.side == OrderSide.SELL
            and result.status == OrderStatus.FILLED
            and result.filled_price is not None
            and avg_price is not None
        ):
            realized_pnl = (result.filled_price - avg_price) * result.filled_quantity

        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO trades
                   (order_id, ticker, side, status, quantity, filled_quantity,
                    filled_price, avg_price, realized_pnl, error_message, timestamp, name,
                    exit_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.order_id,
                    result.ticker,
                    result.side.value,
                    result.status.value,
                    result.quantity,
                    result.filled_quantity,
                    result.filled_price,
                    avg_price,
                    realized_pnl,
                    result.error_message,
                    result.timestamp.isoformat(),
                    result.name,
                    exit_reason,
                ),
            )
            conn.commit()

    def apply_fills(self, fills: Iterable[FillRecord], day: date) -> int:
        """체결내역 조회 결과를 당일 주문 기록에 주문번호로 매칭해 반영한다.

        주문번호가 당일 기록에 없으면 **새 행으로 넣는다** (PRD 5.7, 확정 2026-08-11).
        종전에는 건너뛰어서 키움 앱에서 직접 판 물량이 리포트 손익에 통째로 빠졌다 —
        리포트가 계좌와 어긋나면 성과 판단의 근거 자체가 무너진다.

        주문번호로 먼저 찾으므로 같은 날 여러 번 실행해도 결과가 같다(두 번째부터는 UPDATE로
        흐른다) — 리포트를 다시 보내거나 엔진을 재시작해도 중복 집계되지 않는다.
        """
        start, end = _day_range(day)
        updated = 0
        external: List[FillRecord] = []

        with closing(self._connect()) as conn:
            for fill in fills:
                row = conn.execute(
                    """SELECT id, side, avg_price FROM trades
                       WHERE order_id = ? AND timestamp BETWEEN ? AND ?""",
                    (fill.order_id, start, end),
                ).fetchone()
                if row is None:
                    external.append(fill)
                    continue

                realized_pnl = None
                avg_price = row["avg_price"]
                if row["side"] == OrderSide.SELL.value and avg_price:
                    realized_pnl = (fill.filled_price - avg_price) * fill.filled_quantity

                conn.execute(
                    """UPDATE trades
                       SET status = ?, filled_quantity = ?, filled_price = ?,
                           realized_pnl = ?, commission = ?, tax = ?,
                           name = COALESCE(name, ?)
                       WHERE id = ?""",
                    (
                        fill.status.value,
                        fill.filled_quantity,
                        fill.filled_price,
                        realized_pnl,
                        fill.commission,
                        fill.tax,
                        fill.name,
                        row["id"],
                    ),
                )
                updated += 1

            # 외부 체결은 매칭 건을 모두 반영한 뒤에 넣는다 — 수동 매도의 평단 대용으로 같은 날
            # 매수 체결가를 쓰는데, 그 매수 행은 위 UPDATE를 거쳐야 체결가가 채워지기 때문이다
            stale = [fill for fill in external if _is_known_earlier_fill(conn, fill, start)]
            external = [fill for fill in external if fill not in stale]
            for fill in external:
                _insert_external_fill(conn, fill, day, start, end)
            conn.commit()

        if stale:
            logger.warning(
                "이전 거래일 체결 %d건이 다시 조회돼 기록하지 않습니다 (휴장일 추정): %s",
                len(stale),
                [f.label for f in stale],
            )

        if external:
            logger.warning(
                "이 프로그램이 내지 않은 체결 %d건을 함께 기록했습니다 (수동 매매 추정): %s",
                len(external),
                [f.label for f in external],
            )
        logger.info("체결 결과 %d건을 매매 기록에 반영했습니다.", updated + len(external))
        return updated + len(external)

    def has_unsettled_sells(self, day: date) -> bool:
        """접수만 되고 체결도 거부도 확인되지 않은 당일 매도가 남아 있는지 (PRD 5.11).

        청산 직후 리포트는 이 값이 True면 보내지 않고 미룬다. pending 행은 집계에서
        통째로 빠지므로(`_build_summary`), 그대로 보내면 방금 판 종목이 '보유중'으로
        실리고 손익·수수료가 0으로 나간다 (2026-08-12 실제 발생).

        매수는 보지 않는다 — 10:10에 취소된 미체결 매수가 그대로 남아 있어도 팔 것이
        없으니 리포트를 막을 이유가 없다.
        """
        start, end = _day_range(day)
        placeholders = ", ".join("?" for _ in SETTLED_STATUSES)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT 1 FROM trades
                    WHERE side = ? AND status NOT IN ({placeholders})
                          AND timestamp BETWEEN ? AND ? LIMIT 1""",
                (OrderSide.SELL.value, *SETTLED_STATUSES, start, end),
            ).fetchone()
        return row is not None

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

    def program_order_ids(self, day: date) -> set:
        """그날 이 프로그램이 낸 주문의 주문번호.

        `apply_fills`가 넣은 외부 체결 행(타임스탬프가 그날 0시)은 뺀다 — 사용자 직접 매도를
        알아보는 데 쓰는 값인데, 리포트 동기화가 먼저 돌았다고 해서 사용자 매도가 프로그램 주문으로
        바뀌면 안 된다.
        """
        start, end = _day_range(day)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT order_id FROM trades WHERE timestamp BETWEEN ? AND ? AND timestamp != ?",
                (start, end, start),
            ).fetchall()
        return {row["order_id"] for row in rows}

    def has_buy_order(self, day: date, tickers: Iterable[str]) -> bool:
        """그날 `tickers` 중 하나라도 거부되지 않은 매수 주문 기록이 있는가 (2026-10-30).

        2호 전략 매수 중복 방지의 두 번째 표시다. 매수 기록 파일은 매수 결과 메일을 보내면서
        지워지는데(`cancel_unfilled_buys`), 2026-10-30에는 결과 메일이 09:10에 먼저 나가 파일이
        사라진 뒤 09:13 매수 시각 안전망이 같은 ETF를 한 번 더 샀다. 이 기록은 지워지지 않는다.
        """
        codes = list(tickers)
        if not codes:
            return False
        start, end = _day_range(day)
        marks = ",".join("?" for _ in codes)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT 1 FROM trades
                    WHERE timestamp BETWEEN ? AND ? AND side = 'buy' AND status != 'rejected'
                      AND ticker IN ({marks})
                    LIMIT 1""",
                (start, end, *codes),
            ).fetchone()
        return row is not None

    def same_day_buy_price(self, ticker: str, day: date) -> Optional[float]:
        """그날 그 종목 매수 체결의 가중평균가. 없으면 None (전일 이월분 등)."""
        start, end = _day_range(day)
        with closing(self._connect()) as conn:
            return _same_day_buy_price(conn, ticker, start, end)

    def tickers_with_unknown_pnl(self, day: date) -> Set[str]:
        """그날 체결된 매도 중 `realized_pnl`을 모르는(NULL) 종목코드 (F2, 2026-09-22 최종 리뷰).

        `_pair_by_ticker`는 종목의 매도가 여러 건일 때 **알려진** `realized_pnl`만 합산한다 —
        일부만 모르는 종목도 나머지 합계가 그대로 나와 '알고 있다'로 보인다. 15:35 매도 판단
        검증은 순손익이 부분적으로라도 불명이면 그 종목 전체를 "순손익 모름"으로 다뤄야
        하므로(스펙 3.2), 그 판정에 쓸 종목 목록을 따로 조회한다.
        """
        start, end = _day_range(day)
        placeholders = ", ".join("?" for _ in FILLED_STATUSES)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT ticker FROM trades
                    WHERE side = ? AND status IN ({placeholders})
                          AND realized_pnl IS NULL
                          AND timestamp BETWEEN ? AND ?""",
                (OrderSide.SELL.value, *FILLED_STATUSES, start, end),
            ).fetchall()
        return {row["ticker"] for row in rows}

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

    def recent_exit_reviews(
        self, day_count: int = 10, tickers: Optional[Sequence[str]] = None
    ) -> List[ExitReviewRow]:
        """최근 `day_count` 거래일의 검증 (오래된 날부터) — 매도 프롬프트 튜너의 입력이다.

        `tickers`를 주면 그 종목만 센다 — 옛 1호 주식 행이 같은 표에 남아 있어, 2호(ETF)
        튜너에는 ETF 행만 넘겨야 한다. 거래일 수도 그 종목이 있는 날로 센다.
        """
        marks, params = _ticker_filter(tickers)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT * FROM exit_reviews
                   WHERE day IN (
                       SELECT DISTINCT day FROM exit_reviews WHERE 1 = 1{marks}
                       ORDER BY day DESC LIMIT ?
                   ){marks}
                   ORDER BY day, id""",
                (*params, day_count, *params),
            ).fetchall()
        return [_exit_review_row(row) for row in rows]

    def count_exit_reviews(
        self, exit_prompt_version: str, tickers: Optional[Sequence[str]] = None
    ) -> int:
        """그 버전으로 검증이 끝난 종목 수 — 판정 불가(unknown)는 세지 않는다 (표본 게이트).

        `tickers`를 주면 그 종목만 센다 (옛 1호 주식 행 제외, `recent_exit_reviews` 참고).
        """
        marks, params = _ticker_filter(tickers)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) FROM exit_reviews
                   WHERE exit_prompt_version = ? AND outcome != 'unknown'{marks}""",
                (exit_prompt_version, *params),
            ).fetchone()
        return int(row[0])

    def daily_summary(self, day: date) -> DailySummary:
        start, end = _day_range(day)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT ticker, name, side, status, filled_quantity, filled_price,
                          avg_price, realized_pnl, commission, tax
                   FROM trades WHERE timestamp BETWEEN ? AND ?
                   ORDER BY id""",
                (start, end),
            ).fetchall()
        return _build_summary(day, rows)

    def monthly_summary(self, year: int, month: int, up_to: date) -> MonthlySummary:
        """월초부터 up_to 날짜까지의 누적 실현손익과 수수료·세금.

        수수료·세금은 일간 집계와 같은 기준으로 매수분까지 더한다 — 매수 수수료도
        이번 달에 계좌에서 빠져나간 돈이므로 차익에서 빼야 한다.

        월 누적 기준일(매월 1일 vs 실전 전환일)은 아직 미확정(10절)이므로,
        우선 달력상 매월 1일 기준으로 계산한다.
        """
        return self._range_summary(datetime(year, month, 1), up_to)

    def yearly_summary(self, year: int, up_to: date) -> MonthlySummary:
        """연초부터 up_to 날짜까지의 누적 실현손익과 수수료·세금 (PRD 5.11, 2026-09-01).

        기준은 월 집계와 같고 기간만 1월 1일부터다 — 두 블록의 숫자가 같은 규칙으로
        나와야 "이번 달"과 "올해"를 나란히 놓고 읽을 수 있다.
        """
        return self._range_summary(datetime(year, 1, 1), up_to)

    def _range_summary(self, start_at: datetime, up_to: date) -> MonthlySummary:
        """기간 누적 실현손익과 수수료·세금 — 월·연 집계가 공유한다."""
        start = start_at.isoformat()
        end = datetime.combine(up_to, datetime.max.time()).isoformat()
        placeholders = ", ".join("?" for _ in FILLED_STATUSES)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT COALESCE(SUM(realized_pnl), 0),
                           COALESCE(SUM(COALESCE(commission, 0) + COALESCE(tax, 0)), 0)
                    FROM trades
                    WHERE status IN ({placeholders}) AND timestamp BETWEEN ? AND ?""",
                (*FILLED_STATUSES, start, end),
            ).fetchone()
        return MonthlySummary(realized_pnl=row[0] or 0.0, fees=row[1] or 0.0)

    def closed_trade_returns(
        self, start_day: Optional[date], up_to: date, tickers: Iterable[str]
    ) -> List[float]:
        """기간 안 청산된 매매의 날짜·종목별 순수익률(%) — 2호 전략 자동 중단 장치의 입력 (PRD 5.5-C).

        순손익 = 매도 실현손익 − 그날 그 종목의 수수료·세금(매수분 포함, 일간 집계와 같은 기준),
        분모 = 매도 수량 × 평단. 매도가 없는 날(미청산)은 뺀다. `start_day`가 None이면 처음부터.
        """
        wanted = tuple(tickers)
        if not wanted:
            return []
        start = datetime.combine(start_day, datetime.min.time()).isoformat() if start_day else ""
        end = datetime.combine(up_to, datetime.max.time()).isoformat()
        status_marks = ", ".join("?" for _ in FILLED_STATUSES)
        ticker_marks = ", ".join("?" for _ in wanted)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT date(timestamp) AS d, ticker,
                           COALESCE(SUM(CASE WHEN side = ? THEN realized_pnl END), 0),
                           COALESCE(SUM(COALESCE(commission, 0) + COALESCE(tax, 0)), 0),
                           COALESCE(SUM(CASE WHEN side = ? THEN avg_price * filled_quantity END), 0)
                    FROM trades
                    WHERE status IN ({status_marks}) AND ticker IN ({ticker_marks})
                          AND timestamp BETWEEN ? AND ?
                    GROUP BY d, ticker ORDER BY d""",
                (OrderSide.SELL.value, OrderSide.SELL.value, *FILLED_STATUSES, *wanted, start, end),
            ).fetchall()
        return [
            ((realized or 0.0) - (fees or 0.0)) / cost * 100
            for _day, _ticker, realized, fees, cost in rows
            if cost
        ]

    def monthly_cumulative_series(
        self, year: int, month: int, up_to: date
    ) -> List[DailyPoint]:
        """월초부터 up_to까지 날짜별 누적 순손익 — 리포트 메일의 꺾은선 그래프 입력.

        매매가 없던 날은 점을 만들지 않는다 (휴장일까지 평평하게 이어 붙이면 실제
        거래일 간격이 왜곡된다). 수수료·세금 기준은 monthly_summary와 같아서,
        마지막 점의 누적값은 리포트에 적히는 '누적 순손익'과 일치한다.
        """
        start = datetime(year, month, 1).isoformat()
        end = datetime.combine(up_to, datetime.max.time()).isoformat()
        placeholders = ", ".join("?" for _ in FILLED_STATUSES)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT date(timestamp) AS d,
                           COALESCE(SUM(realized_pnl), 0),
                           COALESCE(SUM(COALESCE(commission, 0) + COALESCE(tax, 0)), 0)
                    FROM trades
                    WHERE status IN ({placeholders}) AND timestamp BETWEEN ? AND ?
                    GROUP BY d ORDER BY d""",
                (*FILLED_STATUSES, start, end),
            ).fetchall()

        points: List[DailyPoint] = []
        running = 0.0
        for day_text, realized, fees in rows:
            net = (realized or 0.0) - (fees or 0.0)
            running += net
            points.append(
                DailyPoint(day=date.fromisoformat(day_text), net_pnl=net, cumulative=running)
            )
        return points

    def yearly_cumulative_series(self, year: int, up_to: date) -> List[DailyPoint]:
        """연초부터 up_to까지 **달별** 누적 순손익 — 연 그래프 입력 (2026-09-01).

        점 하나가 한 달이고 `day`에는 그 달의 1일이 들어간다. 매매가 없던 달은 점을 만들지
        않는다 — 날짜별 계열이 휴장일을 건너뛰는 것과 같은 규약이다. 마지막 점의 누적값은
        `yearly_summary`의 누적 순손익과 일치한다.
        """
        start = datetime(year, 1, 1).isoformat()
        end = datetime.combine(up_to, datetime.max.time()).isoformat()
        placeholders = ", ".join("?" for _ in FILLED_STATUSES)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT strftime('%m', timestamp) AS m,
                           COALESCE(SUM(realized_pnl), 0),
                           COALESCE(SUM(COALESCE(commission, 0) + COALESCE(tax, 0)), 0)
                    FROM trades
                    WHERE status IN ({placeholders}) AND timestamp BETWEEN ? AND ?
                    GROUP BY m ORDER BY m""",
                (*FILLED_STATUSES, start, end),
            ).fetchall()

        points: List[DailyPoint] = []
        running = 0.0
        for month_text, realized, fees in rows:
            net = (realized or 0.0) - (fees or 0.0)
            running += net
            points.append(
                DailyPoint(day=date(year, int(month_text), 1), net_pnl=net, cumulative=running)
            )
        return points


def _same_day_buy_price(
    conn: sqlite3.Connection, ticker: str, start: str, end: str
) -> Optional[float]:
    """같은 날 같은 종목 매수 체결의 가중평균가 — 수동 매도의 평단 대용 (PRD 5.7).

    전일 이월분을 판 경우에는 매수 행이 없어 None이 된다.
    """
    placeholders = ", ".join("?" for _ in FILLED_STATUSES)
    rows = conn.execute(
        f"""SELECT filled_price, filled_quantity FROM trades
            WHERE ticker = ? AND side = ? AND status IN ({placeholders})
                  AND timestamp BETWEEN ? AND ?""",
        (ticker, OrderSide.BUY.value, *FILLED_STATUSES, start, end),
    ).fetchall()
    return _weighted_average([(r["filled_price"], r["filled_quantity"]) for r in rows]) or None


def _is_known_earlier_fill(conn: sqlite3.Connection, fill: FillRecord, start: str) -> bool:
    """같은 체결이 **이전 날짜**에 이미 기록돼 있는지 (확정 2026-09-24).

    휴장일에 체결내역 조회(`ka10076`)가 직전 거래일 체결을 그대로 돌려준다 — 2026-09-24에
    9월 23일 체결 4건이 오늘 것으로 다시 들어왔다. 주문번호가 그날 주문 기록과 맞지 않아
    '외부 체결'(수동 매매)로 새 행이 생기고, 수수료·세금이 이중 계상됐다.

    주문번호만 보지 않고 종목·방향·수량·체결가까지 함께 본다 — 키움 주문번호는 날마다 다시
    매겨져 언젠가 겹칠 수 있는데, 다섯 값이 모두 같은 별개의 체결은 사실상 없다.
    """
    row = conn.execute(
        """SELECT 1 FROM trades
           WHERE order_id = ? AND ticker = ? AND side = ?
                 AND filled_quantity = ? AND filled_price = ? AND timestamp < ?
           LIMIT 1""",
        (
            fill.order_id,
            fill.ticker,
            fill.side.value,
            fill.filled_quantity,
            fill.filled_price,
            start,
        ),
    ).fetchone()
    return row is not None


def _insert_external_fill(
    conn: sqlite3.Connection, fill: FillRecord, day: date, start: str, end: str
) -> None:
    """이 프로그램이 내지 않은 체결을 새 행으로 남긴다 (PRD 5.7, 확정 2026-08-11).

    평단을 알 수 없으므로 매도는 같은 날 같은 종목의 매수 체결가로 대신한다. 그마저 없으면
    손익을 비워 두고 체결 자체만 남긴다 — 틀린 손익을 적는 것보다 빈 값이 낫고, 수수료·세금은
    어느 쪽이든 그대로 집계된다.

    타임스탬프는 체결내역 조회(`ka10076`)가 시각을 주지 않아 그날 0시로 둔다. 집계는 날짜로만
    걸러지므로(`_day_range`) 그날 범위 안이라는 것만 보장하면 된다.
    """
    avg_price = None
    realized_pnl = None
    if fill.side == OrderSide.SELL:
        avg_price = _same_day_buy_price(conn, fill.ticker, start, end)
        if avg_price:
            realized_pnl = (fill.filled_price - avg_price) * fill.filled_quantity

    conn.execute(
        """INSERT INTO trades
           (order_id, ticker, side, status, quantity, filled_quantity,
            filled_price, avg_price, realized_pnl, error_message, timestamp,
            name, commission, tax)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
        (
            fill.order_id,
            fill.ticker,
            fill.side.value,
            fill.status.value,
            fill.filled_quantity + fill.unfilled_quantity,
            fill.filled_quantity,
            fill.filled_price,
            avg_price,
            realized_pnl,
            datetime.combine(day, datetime.min.time()).isoformat(),
            fill.name,
            fill.commission,
            fill.tax,
        ),
    )


def _build_summary(day: date, rows: Iterable[sqlite3.Row]) -> DailySummary:
    """당일 주문 행들을 종목 단위로 묶어 리포트용 요약을 만든다."""
    rows = list(rows)
    filled = [r for r in rows if r["status"] in FILLED_STATUSES]
    rejected_count = sum(1 for r in rows if r["status"] == OrderStatus.REJECTED.value)

    trades = _pair_by_ticker(filled)
    return DailySummary(
        day=day,
        buy_count=sum(1 for r in filled if r["side"] == OrderSide.BUY.value),
        sell_count=sum(1 for r in filled if r["side"] == OrderSide.SELL.value),
        realized_pnl=sum(r["realized_pnl"] or 0.0 for r in filled),
        trades=trades,
        cost=sum(t.cost for t in trades if t.sell_price is not None),
        fees=sum((r["commission"] or 0.0) + (r["tax"] or 0.0) for r in filled),
        rejected_count=rejected_count,
    )


def _pair_by_ticker(filled: List[sqlite3.Row]) -> List[TradeRow]:
    """종목별로 매수/매도를 묶어 표의 한 줄로 만든다.

    매수가는 손익 계산에 실제로 쓰인 계좌 평단(avg_price)을 그대로 쓴다 — 전일 이월분이
    섞이면 당일 매수 체결가와 달라지는데, 표의 수익률과 실현손익이 어긋나면 안 된다.
    """
    grouped: Dict[str, List[sqlite3.Row]] = {}
    for row in filled:
        grouped.setdefault(row["ticker"], []).append(row)

    trades: List[TradeRow] = []
    for ticker, group in grouped.items():
        buys = [r for r in group if r["side"] == OrderSide.BUY.value]
        sells = [r for r in group if r["side"] == OrderSide.SELL.value]
        name = next((r["name"] for r in group if r["name"]), None)
        fees = sum((r["commission"] or 0.0) + (r["tax"] or 0.0) for r in group)

        if sells:
            quantity = sum(r["filled_quantity"] for r in sells)
            sell_price = _weighted_average([(r["filled_price"], r["filled_quantity"]) for r in sells])
            buy_price = _weighted_average([(r["avg_price"], r["filled_quantity"]) for r in sells])
            # 평단이 비어 있으면(예: 조회 실패) 당일 매수 체결가로 대신한다
            if not buy_price and buys:
                buy_price = _weighted_average(
                    [(r["filled_price"], r["filled_quantity"]) for r in buys]
                )
            # 평단을 모르는 수동 매도만 있으면 손익은 '모름'이다 — 0원(본전)으로 보이면 안 된다
            known = [r["realized_pnl"] for r in sells if r["realized_pnl"] is not None]
            pnl = sum(known) if known else None
        else:
            quantity = sum(r["filled_quantity"] for r in buys)
            sell_price = None
            buy_price = _weighted_average([(r["filled_price"], r["filled_quantity"]) for r in buys])
            pnl = None

        trades.append(
            TradeRow(
                ticker=ticker,
                name=name,
                quantity=quantity,
                buy_price=buy_price,
                sell_price=sell_price,
                pnl=pnl,
                fees=fees,
            )
        )
    return trades


def _weighted_average(pairs: List[Tuple[Optional[float], Optional[int]]]) -> float:
    total_qty = sum(q or 0 for _, q in pairs)
    if total_qty <= 0:
        return 0.0
    return sum((p or 0.0) * (q or 0) for p, q in pairs) / total_qty


def _ticker_filter(tickers: Optional[Sequence[str]]) -> Tuple[str, Tuple[str, ...]]:
    """`AND ticker IN (?, ?)` 조각과 그 인자. `tickers`가 None이면 거르지 않는다."""
    if tickers is None:
        return "", ()
    values = tuple(tickers)
    return f" AND ticker IN ({', '.join('?' for _ in values)})", values


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
