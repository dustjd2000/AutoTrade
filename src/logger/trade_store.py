import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.core.events import OrderResult, OrderSide, OrderStatus

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
    timestamp TEXT NOT NULL
);
"""


@dataclass
class DailySummary:
    day: date
    buy_count: int
    sell_count: int
    realized_pnl: float


class TradeStore:
    """매수/매도 체결 내역을 SQLite에 영속 저장한다.

    5.7절 일별 손익 요약, 5.11절 15:30 성과 리포트(일별/월별 누적)의 데이터 원천.
    개인 프로젝트 규모(파일 하나, 별도 서버 불필요)에 맞춰 파일 로그 대신 SQLite로 결정 (10절 Open Question).
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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
                    filled_price, avg_price, realized_pnl, error_message, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
            conn.commit()

    def daily_summary(self, day: date) -> DailySummary:
        start = datetime.combine(day, datetime.min.time()).isoformat()
        end = datetime.combine(day, datetime.max.time()).isoformat()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT
                       SUM(CASE WHEN side = ? THEN 1 ELSE 0 END),
                       SUM(CASE WHEN side = ? THEN 1 ELSE 0 END),
                       COALESCE(SUM(realized_pnl), 0)
                   FROM trades
                   WHERE status = ? AND timestamp BETWEEN ? AND ?""",
                (OrderSide.BUY.value, OrderSide.SELL.value, OrderStatus.FILLED.value, start, end),
            ).fetchone()
        buy_count, sell_count, realized_pnl = row
        return DailySummary(
            day=day,
            buy_count=buy_count or 0,
            sell_count=sell_count or 0,
            realized_pnl=realized_pnl or 0.0,
        )

    def monthly_realized_pnl(self, year: int, month: int, up_to: date) -> float:
        """월초부터 up_to 날짜까지의 누적 실현손익 합계.

        월 누적 기준일(매월 1일 vs 실전 전환일)은 아직 미확정(10절)이므로,
        우선 달력상 매월 1일 기준으로 계산한다.
        """
        start = datetime(year, month, 1).isoformat()
        end = datetime.combine(up_to, datetime.max.time()).isoformat()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0)
                   FROM trades
                   WHERE status = ? AND timestamp BETWEEN ? AND ?""",
                (OrderStatus.FILLED.value, start, end),
            ).fetchone()
        return row[0] or 0.0
