import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.core.events import FillRecord, OrderResult, OrderSide, OrderStatus, format_stock
# 순환 참조 없음 — recommender는 config.settings와 src.data.collector만 본다.
from src.llm.recommender import StockRecommendation

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

# recommendations 테이블에 뒤늦게 추가될 컬럼들 — 지금은 비어 있다. trades와 같은 패턴을
# 갖춰 둬야 다음 컬럼 추가 때 CREATE TABLE IF NOT EXISTS가 조용히 no-op되는 함정을 피한다.
RECOMMENDATION_MIGRATIONS: Tuple[Tuple[str, str], ...] = ()

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
            conn.execute(RECOMMENDATION_SCHEMA_SQL)
            self._migrate(conn, "trades", MIGRATIONS)
            self._migrate(conn, "recommendations", RECOMMENDATION_MIGRATIONS)
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

    # ── 추천 기록 (PRD 5.5-B '추천 검증') ────────────────────
    def save_recommendations(
        self, day: date, recommendations: List[StockRecommendation], prompt_version: str
    ) -> None:
        """그날 추천 메일에 실린 종목을 남긴다.

        (day, ticker) UNIQUE로 UPSERT해 겹치는 종목은 나중 추천이 앞선 추천을 덮어쓴다.
        그것만으로는 부족하다 — UI ① 버튼을 다시 눌러 LLM이 **다른** 종목을 고르면, 이전
        실행에만 있던 종목의 행이 UPSERT 대상이 아니라 그대로 남아 그날 행 집합이 여러 번의
        실행을 합친 합집합이 되어버린다. 그래서 UPSERT에 앞서 그날 행 중 **이번 실행에
        없는 종목**을 먼저 지워, 재실행 후 남는 집합이 항상 최신 실행과 같아지게 만든다.
        단, 이미 검증까지 끝난 행(actual_close IS NOT NULL)은 지우지 않는다 — 그건 완결된
        기록이라 나중 실행이 건드리면 안 된다. recommendations가 빈 리스트면(오늘은
        `recommend_and_notify`가 그 전에 걸러 호출하지 않는 경우) 지울 기준도 없으므로
        아무것도 하지 않는다 — 빈 값 하나 때문에 그날 미검증 행을 통째로 날리는 것을 막는다.
        덮어쓸 때 검증 칸(actual_*, *_hit, review)은 건드리지 않는다 — 추천을 다시 돌린
        시점에는 아직 채워져 있지 않고, 채워져 있다면 그것이 더 나중 정보다.
        """
        with closing(self._connect()) as conn:
            if recommendations:
                tickers = [r.ticker for r in recommendations]
                placeholders = ",".join("?" * len(tickers))
                conn.execute(
                    f"""DELETE FROM recommendations
                        WHERE day = ? AND actual_close IS NULL
                          AND ticker NOT IN ({placeholders})""",
                    (day.isoformat(), *tickers),
                )
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


def _optional_bool(value) -> Optional[bool]:
    """SQLite의 0/1/NULL을 bool/None으로. NULL은 '판정하지 않음'이라 False와 구분해야 한다."""
    return None if value is None else bool(value)
