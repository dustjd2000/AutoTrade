import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
    """월초부터 기준일까지의 누적 실적 — 리포트의 '이번 달 누적' 블록.

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
            conn.execute(SCHEMA)
            self._migrate(conn)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        for column, ddl in MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)
                logger.info("trades 테이블에 %s 컬럼을 추가했습니다.", column)

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
            for fill in external:
                _insert_external_fill(conn, fill, day, start, end)
            conn.commit()

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

        매수는 보지 않는다 — 09:30에 취소된 미체결 매수가 그대로 남아 있어도 팔 것이
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
        start = datetime(year, month, 1).isoformat()
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
