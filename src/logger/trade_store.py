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
    tax REAL
);
"""

# 이미 만들어진 DB에 뒤늦게 추가된 컬럼들 — 없을 때만 붙인다
MIGRATIONS = (
    ("name", "ALTER TABLE trades ADD COLUMN name TEXT"),
    ("commission", "ALTER TABLE trades ADD COLUMN commission REAL"),
    ("tax", "ALTER TABLE trades ADD COLUMN tax REAL"),
)

# 부분체결도 실제 매매이므로 집계에 포함한다
FILLED_STATUSES = (OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value)


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

    5.7절 일별 손익 요약, 5.11절 15:30 성과 리포트(일별/월별 누적)의 데이터 원천.
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

    def record_fill(self, result: OrderResult, avg_price: Optional[float] = None) -> None:
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
                    filled_price, avg_price, realized_pnl, error_message, timestamp, name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
            conn.commit()

    def apply_fills(self, fills: Iterable[FillRecord], day: date) -> int:
        """체결내역 조회 결과를 당일 주문 기록에 주문번호로 매칭해 반영한다.

        UPDATE만 하므로 같은 날 여러 번 실행해도 결과가 같다 — 리포트를 다시 보내거나
        엔진을 재시작해도 중복 집계되지 않는다.
        """
        start, end = _day_range(day)
        updated = 0

        with closing(self._connect()) as conn:
            for fill in fills:
                row = conn.execute(
                    """SELECT id, side, avg_price FROM trades
                       WHERE order_id = ? AND timestamp BETWEEN ? AND ?""",
                    (fill.order_id, start, end),
                ).fetchone()
                if row is None:
                    # 이 프로그램이 내지 않은 주문(수동 매매 등)은 기록 대상이 아니다
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
            conn.commit()

        logger.info("체결 결과 %d건을 매매 기록에 반영했습니다.", updated)
        return updated

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
            pnl = sum(r["realized_pnl"] or 0.0 for r in sells)
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
