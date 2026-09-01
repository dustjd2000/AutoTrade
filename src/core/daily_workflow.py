import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Optional

from src.api.account import AccountClient
from src.core.engine import TradingEngine
from src.core.events import (
    BuyExecution,
    BuyOutcome,
    BuyPlanView,
    BuyRecord,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    format_stock,
)
from src.data.collector import DataCollector
from src.llm.recommender import LLMRecommender, tick_size
from src.logger.trade_store import TradeStore
from src.notification.email import EmailNotifier
from src.notification import chart, templates
from src.risk.manager import exit_trigger_price
from src.strategy.llm_momentum import LLMMomentumStrategy

logger = logging.getLogger(__name__)

# 최종 리포트를 보낸 날짜를 남기는 마커 파일. 인메모리 필드로 두면 엔진 재시작
# (설정 저장·앱 재실행)마다 DailyWorkflow가 새로 만들어지면서 표시가 사라져,
# 오전에 이미 보낸 리포트를 15:35가 다시 보낸다.
DEFAULT_REPORT_MARK_PATH = Path("data") / "final_report_sent"

# 09:08 매수 결과를 10:10 마무리까지 넘기는 파일. 인메모리 필드로 두면 그 사이 엔진이
# 재시작될 때(설정 저장 등) 기록이 사라져, 취소는 체결내역 조회로 살아나도 매수 결과
# 메일만 조용히 빠진다 — 사용자는 미체결인지 장애인지 구분할 수 없다.
DEFAULT_BUY_RECORDS_PATH = Path("data") / "buy_records.json"

# 매수 시각 전, 아직 주문이 나가지 않은 추천 종목의 상태 문구 (UI '매수 예정' 표).
# 주문 이후의 문구는 매수 결과 메일과 같은 것을 쓴다 (templates.BUY_OUTCOME_LABELS) —
# 화면과 메일이 서로 다른 말을 쓰면 대조가 안 된다.
BUY_PENDING_STATUS = "매수 대기"

# '선택 삭제'가 다룰 수 있는 두 상태 (PRD 5.10 '선택 삭제'). 대기 행은 추천 목록에서 빼고,
# 접수 행은 미체결 주문을 취소한다 — 상태 문구는 표·메일과 같은 것을 써야 대조가 된다.
BUY_ORDERED_STATUS = templates.BUY_OUTCOME_LABELS[BuyOutcome.ORDERED]
BUY_DROPPABLE_STATUSES = (BUY_PENDING_STATUS, BUY_ORDERED_STATUS)

# 잔고 대조로 접수 행을 옮길 때 쓰는 상태 (`_settled_row`) — 부분체결은 표에 남지만
# 체크 열은 비운다. 남은 미체결분만 골라 취소하면 이미 체결된 몫까지 덮어쓰기 때문이다.
BUY_PARTIAL_STATUS = templates.BUY_OUTCOME_LABELS[BuyOutcome.PARTIALLY_FILLED]

# 리포트 메일에 인라인 첨부되는 누적 그래프의 Content-ID (월: 날짜별, 연: 달별).
MONTHLY_CHART_CID = "monthly-cumulative"
YEARLY_CHART_CID = "yearly-cumulative"


@dataclass
class BuyRecordState:
    """09:08이 남기고 10:10이 집어 가는 매수 실행 상태 (`DEFAULT_BUY_RECORDS_PATH`)."""

    cash: float                # 매수 산정에 쓴 예수금
    amount_per_stock: float    # 종목당 배정액
    records: List[BuyRecord] = field(default_factory=list)


class DailyWorkflow:
    """1호 전략의 하루 흐름을 스케줄러 트리거에 연결한다 (PRD 5.5-B, 5.11).

    추천 시각 recommend_and_notify → 09:08 execute_buys → 10:10 cancel_unfilled_buys
    → 15:35 send_final_report
    (보유 종목이 그 전에 전량 매도되고 체결까지 확인되면 15:35를 기다리지 않고 최종 리포트를 보낸다)
    """

    def __init__(
        self,
        collector: DataCollector,
        recommender: LLMRecommender,
        strategy: LLMMomentumStrategy,
        engine: TradingEngine,
        account: AccountClient,
        trade_store: TradeStore,
        email: EmailNotifier,
        ws_client=None,
        report_mark_path: Optional[Path] = None,
        buy_records_path: Optional[Path] = None,
        buy_price_tolerance_ratio: float = 0.02,
        gap_down_tolerance_ratio: float = 0.01,
    ):
        self.collector = collector
        self.recommender = recommender
        self.strategy = strategy
        self.engine = engine
        self.account = account
        self.trade_store = trade_store
        self.email = email
        self.ws_client = ws_client
        # 최종 리포트를 보낸 날짜 — 전량 매도 완료와 15:35 스케줄이 중복 발송하지 않도록
        # 공유하는 표시다 (send_final_report). 매수로 보유가 다시 생기면 초기화된다.
        # 엔진 재시작을 견뎌야 하므로 파일에 남긴다 (DEFAULT_REPORT_MARK_PATH 참고).
        self.report_mark_path = Path(
            report_mark_path if report_mark_path is not None else DEFAULT_REPORT_MARK_PATH
        )
        # 09:08 매수 결과를 10:10 마무리(cancel_unfilled_buys)까지 넘기는 파일 — 지정가
        # 주문은 접수 시점에 체결 여부를 알 수 없어, 결과 메일을 그때 보내야 확정된 값이 실린다.
        # 인메모리가 아니라 파일인 이유는 DEFAULT_BUY_RECORDS_PATH 주석 참고.
        self.buy_records_path = Path(
            buy_records_path if buy_records_path is not None else DEFAULT_BUY_RECORDS_PATH
        )
        # UI '매수 예정' 표가 읽는 스냅샷 — (날짜, 행 목록) 한 쌍을 통째로 갈아끼운다
        # (`buy_plan_snapshot` 참고). 주문 기록 파일은 10:10에 지워지므로 표의 출처가 될 수 없다.
        self._buy_board: tuple = (None, ())
        # 09:08 현재가가 목표 매수가보다 이만큼 넘게 높으면 그 종목은 건너뛴다 (_gap_note 참고).
        # 동시에 이 값이 주문 지정가를 정한다 — 밴드 상단이 곧 주문가다 (_order_price 참고)
        self.buy_price_tolerance_ratio = buy_price_tolerance_ratio
        # 09:08 현재가가 추천 시점 가격보다 이만큼 넘게 낮으면 건너뛴다. 0이면 끈다 (_gap_note 참고)
        self.gap_down_tolerance_ratio = gap_down_tolerance_ratio

    # ── '매수 예정' 표 (PRD 5.10) ────────────────────────────
    def buy_plan_snapshot(self, today: Optional[date] = None) -> List[BuyPlanView]:
        """오늘의 매수 예정 종목과 진행 상태 (UI 스레드에서 호출 — API를 호출하지 않는다).

        날짜와 행 목록을 한 튜플로 묶어 통째로 갈아끼우므로, 참조를 한 번만 집으면
        일관된 사본이 된다 (`TradingEngine.position_snapshot`과 같은 규약). 기록된 날짜가
        오늘이 아니면 빈 목록이라, 08:40 초기화 훅 없이도 전날 행이 남지 않는다.

        현재가만은 기록해 둔 값이 아니라 호출 시점의 마지막 시세를 얹는다 — 표는 2초마다
        다시 그려지므로, 행을 만들 때 박아두면 추천 시각의 가격에서 멈춘다. 접수 행의 체결
        여부도 같은 이유로 여기서 잔고와 대조한다 (`_settled_row`).
        """
        day, rows = self._buy_board
        if day != (today or date.today()):
            return []
        held = {p.ticker: p.quantity for p in self.engine.position_snapshot()}
        settled = (self._settled_row(r, held) for r in rows)
        return [
            replace(r, current_price=self.engine.last_price(r.ticker))
            for r in settled
            if r is not None
        ]

    @staticmethod
    def _settled_row(row: BuyPlanView, held: dict) -> Optional[BuyPlanView]:
        """접수 행을 보유 수량과 대조해 체결 상태로 옮긴다. 표에서 뺄 행은 None (PRD 5.10).

        체결 반영(`_fill_buy_prices`)은 10:10·15:15에만 도는데, 지정가가 허용 밴드 상단으로
        올라간 뒤로는 접수 직후 체결되는 것이 보통이다. 그 사이 표가 '접수'로 얼어붙어 같은
        종목이 보유 종목 표와 겹쳐 실린다 (2026-08-26 13:40 실측).

        **표시만 바꾸고 기록은 건드리지 않는다** — 기록과 결과 메일을 확정하는 것은 체결내역
        조회를 보는 10:10·15:15의 일이고, 잔고 대조는 표를 맞추기 위한 근사치다. 전일부터
        들고 있던 종목이 추천되면 체결로 오판하는 한계가 있다 (당일 청산이라 실무상 드물다).
        """
        if row.status != BUY_ORDERED_STATUS or row.quantity <= 0:
            return row
        quantity = held.get(row.ticker, 0)
        if quantity <= 0:
            return row
        if quantity >= row.quantity:
            return None  # 완전 체결 — '보유 종목' 표로 옮겨간다
        return replace(row, status=BUY_PARTIAL_STATUS, quantity=quantity)

    def drop_buy_plans(self, tickers: Iterable[str], today: Optional[date] = None) -> List[str]:
        """고른 종목을 오늘 매수 대상에서 뺀다 — UI '매수 예정' 표의 선택 삭제 (PRD 5.10).

        상태에 따라 하는 일이 다르다 (확대 2026-08-26):

        - **매수 대기** (매수 시각 전): 추천 목록에서 빼 그날 주문이 나가지 않게 한다.
          뺀 몫은 남은 종목에 재분배되지 않고 현금으로 남는다 (`build_buy_plans`는 종목당
          금액을 고정).
        - **접수** (매수 시각 이후): 미체결 매수 주문을 그 자리에서 취소한다 — 10:10
          `cancel_unfilled_buys`를 기다리지 않는다. 행은 지우지 않고 '미체결 취소'로 바꾼다.

        나머지 상태(건너뜀·실패·미체결 취소·부분체결)는 취소할 살아 있는 주문이 없어 대상이
        아니다. 엔진 루프 스레드에서 실행된다 — 매수 시각 작업과 같은 자료를 건드린다.
        """
        today = today or date.today()
        day, rows = self._buy_board
        if day != today:
            return []

        wanted = set(tickers)
        pending = {
            r.ticker for r in rows if r.ticker in wanted and r.status == BUY_PENDING_STATUS
        }
        ordered = {
            r.ticker for r in rows if r.ticker in wanted and r.status == BUY_ORDERED_STATUS
        }
        if not pending and not ordered:
            logger.warning("매수 예정에서 제외할 종목이 없습니다: %s", sorted(wanted))
            return []

        # 취소가 먼저다 — 실패하면 그 종목은 빠지지 않으므로 표에 그대로 남아야 한다
        cancelled = self._cancel_ordered_plans(today, ordered) if ordered else []

        if pending:
            self.strategy.drop_recommendations(pending)
        # 취소 경로가 표를 갈아끼웠을 수 있어 여기서 다시 집는다
        _, rows = self._buy_board
        self._set_buy_board(today, [r for r in rows if r.ticker not in pending])

        dropped = sorted(pending | set(cancelled))
        if dropped:
            logger.warning("매수 예정에서 제외했습니다: %s", dropped)
        return dropped

    def _cancel_ordered_plans(self, today: date, tickers: set) -> List[str]:
        """접수된 매수 주문을 그 자리에서 취소하고, 취소된 종목코드를 돌려준다.

        기록 파일(`buy_records_path`)의 해당 건을 '미체결 취소'로 바꿔 다시 쓴다 — 그래야
        10:10이 같은 주문을 또 취소하려 들지 않고 결과 메일에도 취소로 실린다. 취소에
        실패하면 기록도 표도 건드리지 않는다: 주문이 살아 있는데 화면만 정리된 상태가
        가장 위험하다.
        """
        state = self._read_buy_records(today)
        if state is None:
            logger.warning("오늘 매수 주문 기록이 없어 취소할 수 없습니다: %s", sorted(tickers))
            return []

        # 취소 거절이 '이미 체결됨'인지 가리는 데 쓴다 — 잔고 캐시라 API를 부르지 않는다
        held = {p.ticker: p.quantity for p in self.engine.position_snapshot()}
        cancelled = []
        for record in state.records:
            if record.ticker not in tickers:
                continue
            if record.outcome != BuyOutcome.ORDERED or not record.order_id:
                continue

            label = format_stock(record.ticker, record.name)
            if self.engine.order_client.cancel_order(record.order_id, record.ticker, 0):
                record.outcome = BuyOutcome.CANCELLED
                record.note = "매수 예정에서 빼면서 미체결분을 취소했습니다"
                cancelled.append(record.ticker)
                logger.warning(
                    "매수 주문 취소 (선택 삭제): %s 잔량 전부 (주문번호 %s)",
                    label,
                    record.order_id,
                )
            elif held.get(record.ticker, 0) >= record.quantity > 0:
                # 키움은 이미 전량 체결된 주문의 취소를 '취소가능수량이 없습니다'로 거절한다.
                # 이것을 실패로 알리면 "주문이 살아 있습니다"라는 정반대 안내가 나간다
                # (2026-08-26 실측). 표에서는 `_settled_row`가 이미 이 행을 빼고 있다.
                logger.info(
                    "매수 주문 취소 불필요 (선택 삭제): %s — 이미 체결된 주문입니다 (주문번호 %s)",
                    label,
                    record.order_id,
                )
            else:
                logger.error(
                    "매수 주문 취소 실패 (선택 삭제): %s (주문번호 %s)", label, record.order_id
                )
                self.engine.notify(
                    f"[실패] 매수 주문 취소 실패: {label} — 주문이 살아 있습니다. 직접 확인하세요."
                )

        if cancelled:
            self._write_buy_records(state.cash, state.amount_per_stock, state.records)
            self._set_buy_board(today, self._board_from_records(state.records))
        return cancelled

    def _set_buy_board(self, day: date, rows: List[BuyPlanView]) -> None:
        self._buy_board = (day, tuple(rows))

    def _take_profit_price(self, price: float) -> float:
        """익절선에 닿는 가격 — 표의 '매도예상가'. 익절이 꺼져 있으면 0이라 칸이 빈다.

        LLM의 목표 매도가가 아니라 익절(%) 설정을 순손익 기준으로 역산한 값이다. 목표
        매도가는 주문에 쓰이지 않는 참고 수치라(PRD 5.5-B) "언제 팔리나"에 답하지 못한다.
        단순익절은 익절선이 0이므로 그 경우를 먼저 본다 — 두 익절은 배타적이다(PRD 5.5-B).
        """
        risk = self.engine.risk_manager
        if price <= 0:
            return 0.0
        if risk.simple_take_profit_enabled:
            target_ratio = 0.0
        elif risk.take_profit_enabled:
            target_ratio = risk.take_profit_ratio
        else:
            return 0.0
        return exit_trigger_price(
            price, target_ratio, risk.commission_rate, risk.tax_rate, risk.slippage_rate
        )

    def _board_from_recommendations(self, recommendations) -> List[BuyPlanView]:
        """추천 직후의 표 — 실제로 매수할 상위 몇 종목만 담는다 (build_buy_plans와 같은 기준)."""
        return [
            BuyPlanView(
                ticker=r.ticker,
                label=format_stock(r.ticker, r.name),
                status=BUY_PENDING_STATUS,
                buy_price=float(r.target_price),
                sell_price=self._take_profit_price(float(r.target_price)),
            )
            for r in recommendations[: self.strategy.target_stock_count]
        ]

    def _board_from_records(self, records: List[BuyRecord]) -> List[BuyPlanView]:
        """매수 시각 이후의 표 — 매수하지 못한 종목은 수량·매도예상가를 비우고 사유만 남긴다.

        완전 체결된 종목은 뺀다 — 이미 산 것은 '매수 예정'이 아니고, 바로 아래 '보유 종목'
        표에 현재가·손익과 함께 실린다. 부분체결은 남은 수량이 아직 미체결이라 남긴다.
        """
        return [
            BuyPlanView(
                ticker=r.ticker,
                label=r.label,
                status=templates.BUY_OUTCOME_LABELS[r.outcome],
                quantity=(r.filled_quantity or r.quantity) if r.outcome.is_ordered else 0,
                buy_price=r.price,
                sell_price=self._take_profit_price(r.price) if r.outcome.is_ordered else 0.0,
                note=r.note or "",
            )
            for r in records
            if r.outcome != BuyOutcome.FILLED
        ]

    def recommend_and_notify(self, today: Optional[date] = None) -> None:
        """09:05 — 전일·당일 데이터 수집 → LLM 추천 → 결과를 이메일로 발송."""
        today = today or date.today()
        daily_data = self.collector.collect()
        if not daily_data:
            logger.error("No daily data collected. Skipping today's recommendation.")
            self.engine.notify("[경고] 당일 데이터 수집 실패 — 오늘 매수를 스킵합니다.")
            return

        recommendations = self.recommender.recommend(daily_data)
        if not recommendations:
            logger.error("LLM recommendation unavailable. Skipping today's buys.")
            self.engine.notify("[경고] LLM 추천 실패/타임아웃 — 오늘 매수를 스킵합니다.")
            return

        self.strategy.set_recommendations(recommendations)
        board = self._board_from_recommendations(recommendations)
        self._set_buy_board(today, board)
        self._watch_plan_prices([plan.ticker for plan in board])
        subject, body = templates.recommendation_email(
            recommendations, today, self.strategy.investable_ratio, self.strategy.target_stock_count
        )
        self.email.send(subject, body)
        logger.info("Recommendation email sent for %s", today)

    def _watch_plan_prices(self, tickers: List[str]) -> None:
        """추천 종목의 실시간 시세를 미리 구독한다 — UI '매수 예정' 표 현재가의 출처.

        매수 접수분 구독(`execute_buys`)은 익절/손절 감시가 목적이라 매수 시각에야 걸린다.
        추천~매수 사이에 현재가를 보려면(=어느 종목을 뺄지 판단하려면) 여기서 미리 걸어야 한다.
        """
        if not tickers or self.ws_client is None:
            return
        self.ws_client.subscribe(tickers)
        logger.info("추천 종목 시세 구독: %s", tickers)

    def execute_buys(self) -> None:
        """09:08 — 예수금 기준으로 자금을 배분해 추천 종목을 허용 밴드 상단에 지정가 매수.

        체결 확인과 결과 메일은 여기서 하지 않는다 — 지정가 주문은 접수 직후에 체결 여부를
        알 수 없어, 10:10 `cancel_unfilled_buys`가 미체결분을 정리한 뒤에 알린다.
        """
        cash = self.account.get_cash()
        plans = self.strategy.build_buy_plans(cash)
        if not plans:
            logger.warning("매수 계획이 없습니다 (추천 종목 없음). 주문가능금액 %s원", f"{cash:,.0f}")
            return

        logger.info(
            "매수 시작 — 주문가능금액 %s원, 대상 %d종목, 종목당 배정 %s원",
            f"{cash:,.0f}",
            len(plans),
            f"{plans[0].amount:,.0f}",
        )

        positions = self.account.get_positions()
        ordered, skipped, failed = [], [], []
        records: List[BuyRecord] = []
        for plan in plans:
            label = format_stock(plan.ticker, plan.name)
            try:
                target = float(plan.target_price)

                gap_note, current = self._gap_note(
                    plan.ticker, label, target, plan.recommend_price
                )
                if gap_note is not None:
                    skipped.append(plan.ticker)
                    records.append(
                        BuyRecord(
                            ticker=plan.ticker,
                            name=plan.name,
                            outcome=BuyOutcome.SKIPPED,
                            reference_price=target,
                            note=gap_note,
                        )
                    )
                    continue

                price = self._order_price(target, current)
                if price != target:
                    logger.info(
                        "주문가 = 허용 밴드 상단: %s 목표가 %s원 → 지정가 %s원 "
                        "(현재가 %s원, 상승 허용치 %.1f%%)",
                        label,
                        f"{target:,.0f}",
                        f"{price:,.0f}",
                        f"{current:,.0f}",
                        self.buy_price_tolerance_ratio * 100,
                    )

                quantity = int(plan.amount // price)
                if quantity <= 0:
                    logger.warning(
                        "매수 건너뜀: %s — 주문가 1주 %s원이 종목당 배정액 %s원을 초과합니다.",
                        label,
                        f"{price:,.0f}",
                        f"{plan.amount:,.0f}",
                    )
                    # 별도 알림 메일은 보내지 않는다 — 매수 실행 결과 메일의
                    # '매수하지 못한 종목'에 사유까지 그대로 실린다
                    skipped.append(plan.ticker)
                    records.append(
                        BuyRecord(
                            ticker=plan.ticker,
                            name=plan.name,
                            outcome=BuyOutcome.SKIPPED,
                            reference_price=price,
                            note=f"주문가 1주 {price:,.0f}원이 배정액 {plan.amount:,.0f}원을 초과",
                        )
                    )
                    continue

                logger.info(
                    "매수 산정: %s 주문가 %s원 × %d주 = %s원 (배정 %s원)",
                    label,
                    f"{price:,.0f}",
                    quantity,
                    f"{price * quantity:,.0f}",
                    f"{plan.amount:,.0f}",
                )

                request = OrderRequest(
                    ticker=plan.ticker,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=quantity,
                    price=price,
                    name=plan.name,
                )
                if not self.engine.risk_manager.approve(request, positions, reference_price=price):
                    logger.warning("매수 거부 (리스크 관리): %s x%d주", label, quantity)
                    self.engine.notify(f"매수 거부 (리스크 관리): {label}")
                    failed.append(plan.ticker)
                    records.append(
                        BuyRecord(
                            ticker=plan.ticker,
                            name=plan.name,
                            outcome=BuyOutcome.FAILED,
                            quantity=quantity,
                            reference_price=price,
                            note="리스크 관리 규칙에 걸려 주문하지 않았습니다",
                        )
                    )
                    continue

                result = self.engine.order_client.send_order(request)
                self.engine.risk_manager.record_order(result)
                self.trade_store.record_fill(result)

                # 거부된 주문을 접수된 것처럼 남기면 매수 실패를 알아챌 수 없다
                if result.status == OrderStatus.REJECTED:
                    logger.error(
                        "매수 주문 거부됨: %s x%d주 — %s", label, quantity, result.error_message
                    )
                    self.engine.notify(f"[실패] 매수 거부: {label} x{quantity}주 — {result.error_message}")
                    failed.append(plan.ticker)
                    records.append(
                        BuyRecord(
                            ticker=plan.ticker,
                            name=plan.name,
                            outcome=BuyOutcome.FAILED,
                            quantity=quantity,
                            reference_price=price,
                            order_id=result.order_id,
                            note=f"주문 거부: {result.error_message}",
                        )
                    )
                    continue

                ordered.append(plan.ticker)
                records.append(
                    BuyRecord(
                        ticker=plan.ticker,
                        name=plan.name,
                        outcome=BuyOutcome.ORDERED,
                        quantity=quantity,
                        reference_price=price,
                        order_id=result.order_id,
                    )
                )
                # 체결 통보를 기다리지 않고 감시 대상으로 표시한다 (창 종료 경고의 근거)
                self.engine.note_open_position(plan.ticker)
                logger.info(
                    "지정가 매수 접수: %s %s원 x%d주 (상태 %s, 주문번호 %s)",
                    label,
                    f"{price:,.0f}",
                    quantity,
                    result.status.value,
                    result.order_id,
                )
            except Exception:
                logger.exception("매수 처리 중 오류: %s", label)
                self.engine.notify(f"[오류] 매수 실패: {label}")
                failed.append(plan.ticker)
                records.append(
                    BuyRecord(
                        ticker=plan.ticker,
                        name=plan.name,
                        outcome=BuyOutcome.FAILED,
                        note="매수 처리 중 오류가 발생했습니다 (로그 확인 필요)",
                    )
                )

        logger.info(
            "매수 종료 — 접수 %d종목%s, 건너뜀 %d종목%s, 실패 %d종목%s",
            len(ordered),
            f" {ordered}" if ordered else "",
            len(skipped),
            f" {skipped}" if skipped else "",
            len(failed),
            f" {failed}" if failed else "",
        )
        if not ordered:
            self.engine.notify("[경고] 매수가 한 건도 접수되지 않았습니다. 로그를 확인하세요.")
        else:
            # 보유가 다시 생겼으므로 앞서 보낸 최종 리포트는 더 이상 최종이 아니다.
            # 이 보유분이 전량 매도되면 리포트를 다시 보내고, 남으면 15:35가 보낸다.
            self._clear_report_mark()

        # 접수된 종목만 실시간 시세를 구독한다 — 익절/손절 감시(RiskManager.check_portfolio_exit)의 전제.
        # 거부된 종목까지 구독하면 보유하지도 않은 종목의 시세를 받는다.
        if ordered and self.ws_client is not None:
            self.ws_client.subscribe(ordered)
            if getattr(self.ws_client, "is_connected", True):
                logger.info("실시간 시세 구독: %s", ordered)
            else:
                # 구독 목록에는 담기지만 재접속까지 시세가 오지 않는다 = 그동안 익절/손절 공백
                logger.error(
                    "실시간 시세 구독 보류 — WebSocket 미연결 상태입니다. "
                    "재접속까지 익절/손절 감시가 동작하지 않습니다: %s",
                    ordered,
                )
                self.engine.notify(
                    "[경고] 실시간 시세 미연결 — 재접속까지 익절/손절 감시가 멈춥니다. "
                    f"대상: {ordered}"
                )

        self._set_buy_board(date.today(), self._board_from_records(records))
        # 결과 메일은 10:10 cancel_unfilled_buys가 보낸다 — 지정가라 지금은 체결 여부를 모른다
        self._write_buy_records(cash, plans[0].amount, records)

    def _order_price(self, target_price: float, current_price: float) -> float:
        """주문 지정가 = 허용 밴드 상단 (PRD 5.5-B '주문 방식', 범위 지정 2026-08-26).

        목표 매수가는 밴드의 기준점일 뿐 주문가가 아니다. 지정가 매수는 그 가격 **이하**의
        매도호가에 체결되므로, 상단에 걸어야 밴드 안 어떤 가격이든 받아낼 수 있다 — 목표가
        한 점에 걸면 현재가가 조금이라도 위일 때 되밀리지 않는 한 영원히 미체결이다
        (2026-08-26 015760 한국전력, 그날 투입 0원).

        **밴드의 위쪽 절반에서만 올린다.** 현재가가 목표가 이하면 목표가 지정가가 이미 시장가
        위에 있어 즉시 체결되므로, 상단으로 올려봤자 수량만 줄어든다.

        현재가를 모르면(0 이하) 밴드를 확인할 수 없으므로 종전대로 목표가로 낸다.
        """
        if current_price <= target_price:
            return target_price

        ceiling = target_price * (1 + self.buy_price_tolerance_ratio)
        tick = tick_size(ceiling)
        price = float(int(ceiling // tick) * tick)
        # 호가 단위 내림이 현재가 아래로 떨어지면 다시 미체결이 된다. 현재가는 체결된 값이라
        # 이미 호가 단위에 맞으므로 그대로 쓴다 — 현재가 ≤ 상단은 갭 상승 판정이 보장한다
        return max(price, current_price)

    def _gap_note(
        self, ticker: str, label: str, target_price: float, reference_price: float = 0.0
    ) -> tuple:
        """갭 판정 결과를 `(건너뛸 사유 또는 None, 판정에 쓴 현재가)`로 돌려준다.

        현재가를 함께 넘기는 것은 `_order_price`가 같은 값을 다시 조회하지 않게 하기 위함이다
        — 조회에 실패했으면 0.0이다.

        위아래 두 방향을 본다 (PRD 5.5-B '주문 방식').

        - **갭 상승** (확정 2026-08-07): 목표 매수가는 추천 시점 가격을 근거로 잡은 값이다.
          현재가가 그보다 크게 높으면 그 전제가 이미 깨진 것이므로 그날은 참여하지 않는다.
          이 허용치는 **주문 지정가도 함께 정한다**(`_order_price`) — 밴드 안이면 목표가보다
          이만큼 비싸게 체결될 수 있다는 뜻이라, 손절선보다 크게 잡으면 진입 직후 손절 구간에서
          시작한다.
        - **갭 하락** (확정 2026-08-11, 기준값 변경 2026-08-14): 기준이 목표가가 아니라
          **추천 시점(09:05)의 현재가**다. 목표가 자체가 눌림을 노려 기준가보다 낮게 잡히므로,
          목표가 기준으로 하한을 두면 얼마나 낮게 출발했는지를 잡지 못한다. 전일 종가 대비
          판정은 09:05 후보 선정이 이미 맡았고(PRD 5.5-B '당일 지표 병행 수집'), 여기서는
          추천한 뒤 무너진 종목을 잡는다. 지정가 매수는 가격이 목표가까지 내려온 종목만 잡는
          역선택이 있어(오르는 종목은 미체결) 이 판정이 없으면 갭 하락 종목만 남는다.

        시세 조회에 실패하면 매수를 막지 않고 그대로 진행한다 — 다만 밴드를 모르므로
        `_order_price`가 목표가 지정가로 되돌아간다. 기준가를 모르면(0 이하) 갭 하락 판정만
        건너뛴다.
        """
        try:
            current = self.engine.market_data.get_current_price(ticker).price
        except Exception:
            logger.warning("현재가 조회 실패 — 갭 판정을 건너뛰고 매수합니다: %s", label, exc_info=True)
            return None, 0.0

        if current <= 0:
            return None, 0.0

        limit = target_price * (1 + self.buy_price_tolerance_ratio)
        if current > limit:
            logger.info(
                "매수 건너뜀: %s — 현재가 %s원이 목표가 %s원의 허용 상한 %s원을 넘었습니다.",
                label,
                f"{current:,.0f}",
                f"{target_price:,.0f}",
                f"{limit:,.0f}",
            )
            return (
                f"갭 상승 — 현재가 {current:,.0f}원이 목표가 {target_price:,.0f}원 대비 "
                f"허용치 {self.buy_price_tolerance_ratio * 100:.1f}%를 초과"
            ), current

        # 허용치 0은 '끔'이다 — 갭 상승 쪽(0 = 가장 엄격)과 반대 규약이라 PRD 5.5-B에 명시했다
        if self.gap_down_tolerance_ratio <= 0 or reference_price <= 0:
            return None, current

        floor = reference_price * (1 - self.gap_down_tolerance_ratio)
        if current >= floor:
            return None, current

        logger.info(
            "매수 건너뜀: %s — 현재가 %s원이 추천 시점 %s원의 허용 하한 %s원을 밑돕니다.",
            label,
            f"{current:,.0f}",
            f"{reference_price:,.0f}",
            f"{floor:,.0f}",
        )
        return (
            f"갭 하락 — 현재가 {current:,.0f}원이 추천 시점 {reference_price:,.0f}원 대비 "
            f"허용치 {self.gap_down_tolerance_ratio * 100:.1f}%를 초과 하락"
        ), current

    def cancel_unfilled_buys(self, today: Optional[date] = None) -> None:
        """10:10 — 목표가에 닿지 않은 매수 주문을 취소하고 매수 결과를 알린다 (PRD 5.5-B 6단계).

        취소 대상은 **당일 체결내역 조회**에서 찾는다. 09:08과 10:10 사이에 설정 저장 등으로
        엔진이 재시작돼도 미체결 주문이 장 마감까지 방치되면 안 되기 때문이다.

        15:15 마감 정리(`runtime.close_out`)가 한 번 더 부른다 — 10:10을 놓친 날의 그물이다.
        메일을 보내고 나면 기록 파일을 지우므로 같은 메일이 두 번 나가지는 않는다.
        """
        today = today or date.today()
        state = self._read_buy_records(today)
        cancelled_ids = self._cancel_unfilled_orders(state.records if state else [])

        if state is None:
            logger.info("오늘 접수한 매수 주문 기록이 없어 결과 메일을 보내지 않습니다.")
            return

        records = state.records

        # 체결 반영이 먼저다 — 부분체결분까지 채운 뒤에 남은 '접수' 상태만 취소로 확정한다
        fills_synced = self._fill_buy_prices(records)
        for record in records:
            if record.order_id in cancelled_ids and record.outcome == BuyOutcome.ORDERED:
                record.outcome = BuyOutcome.CANCELLED
                record.note = "목표 매수가에 닿지 않아 미체결분을 취소했습니다"

        # 메일이 실패해도 (_notify_buy_result가 예외를 삼킨다) 기록은 지운다 — 남겨두면
        # 15:15 마감 정리가 같은 메일을 다시 시도하며 매번 취소 로그까지 되풀이한다.
        self._clear_buy_records()
        # 기록 파일은 지워지므로 표의 마지막 상태(체결/미체결 취소)는 여기서 메모리에 남긴다
        self._set_buy_board(today, self._board_from_records(records))
        self._notify_buy_result(state.cash, state.amount_per_stock, records, fills_synced)

    def _cancel_unfilled_orders(self, records: List[BuyRecord]) -> set:
        """미체결 매수 주문을 취소하고, 취소에 성공한 주문번호 집합을 돌려준다."""
        try:
            fills = self.engine.order_client.get_today_fills()
        except Exception:
            logger.exception("미체결 매수 주문 조회 실패 — 취소를 건너뜁니다.")
            self.engine.notify("[경고] 미체결 매수 주문을 조회하지 못했습니다. 직접 확인하세요.")
            return set()

        targets = self._cancel_targets(fills, records)
        if not targets:
            logger.info("취소할 미체결 매수 주문이 없습니다.")
            return set()

        cancelled = set()
        for order_id, ticker, name, quantity in targets:
            label = format_stock(ticker, name)
            amount = f"{quantity}주" if quantity else "잔량 전부"
            if self.engine.order_client.cancel_order(order_id, ticker, quantity):
                cancelled.add(order_id)
                logger.info("미체결 매수 취소: %s %s (주문번호 %s)", label, amount, order_id)
            else:
                logger.error("미체결 매수 취소 실패: %s %s (주문번호 %s)", label, amount, order_id)
                self.engine.notify(
                    f"[실패] 미체결 매수 취소 실패: {label} — 주문이 살아 있습니다. 직접 확인하세요."
                )

        logger.info("미체결 매수 주문 %d건 중 %d건을 취소했습니다.", len(targets), len(cancelled))
        return cancelled

    def _cancel_targets(self, fills, records: List[BuyRecord]) -> List[tuple]:
        """취소할 (주문번호, 종목코드, 종목명, 수량) 목록. 수량 0은 '잔량 전부'다.

        1순위는 체결내역 조회가 알려주는 미체결 잔량이다 — 이 경로는 저장된 기록에
        의존하지 않아 기록을 읽지 못해도 동작한다.
        2순위는 **조회 결과에 흔적조차 없는 접수 주문**이다. 체결내역 TR(ka10076)이 아직
        한 주도 체결되지 않은 대기 주문을 싣는지 확인하지 못했는데, 싣지 않는다면 1순위만으로는
        그 주문이 장 마감까지 살아남는다. 체결된 주문은 조회 결과에 잡히므로 여기 걸리지 않는다.
        """
        targets = [
            (fill.order_id, fill.ticker, fill.name, fill.unfilled_quantity)
            for fill in fills
            if fill.side == OrderSide.BUY and fill.unfilled_quantity > 0
        ]

        known_ids = {fill.order_id for fill in fills}
        targets.extend(
            (record.order_id, record.ticker, record.name, 0)
            for record in records
            if record.order_id
            and record.outcome == BuyOutcome.ORDERED
            and record.order_id not in known_ids
        )
        return targets

    # ── 매수 기록 파일 (09:08 → 10:10 인계) ────────────────────
    def _write_buy_records(
        self, cash: float, amount_per_stock: float, records: List[BuyRecord]
    ) -> None:
        """매수 기록을 파일에 남긴다. 실패해도 주문은 이미 나갔으므로 흐름을 막지 않는다."""
        payload = {
            "date": date.today().isoformat(),
            "cash": cash,
            "amount_per_stock": amount_per_stock,
            "records": [
                {**asdict(record), "outcome": record.outcome.value} for record in records
            ],
        }
        try:
            self.buy_records_path.parent.mkdir(parents=True, exist_ok=True)
            self.buy_records_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.warning(
                "매수 기록을 남기지 못했습니다 (%s) — 엔진이 재시작되면 매수 결과 메일이 "
                "빠질 수 있습니다.",
                self.buy_records_path,
                exc_info=True,
            )

    def _read_buy_records(self, today: date) -> Optional[BuyRecordState]:
        """오늘 저장된 매수 기록. 없거나·깨졌거나·다른 날짜면 None.

        읽기에 실패하면 '기록 없음'으로 본다 — 메일이 빠지는 편이, 깨진 값으로 취소 판정을
        하거나 어제 결과를 오늘 메일로 보내는 것보다 낫다.
        """
        try:
            payload = json.loads(self.buy_records_path.read_text(encoding="utf-8"))
            if date.fromisoformat(payload["date"]) != today:
                logger.info("저장된 매수 기록이 오늘 것이 아니라 무시합니다: %s", payload["date"])
                return None
            return BuyRecordState(
                cash=float(payload["cash"]),
                amount_per_stock=float(payload["amount_per_stock"]),
                records=[
                    BuyRecord(**{**row, "outcome": BuyOutcome(row["outcome"])})
                    for row in payload["records"]
                ],
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning(
                "매수 기록을 읽지 못했습니다 (%s) — 결과 메일을 건너뜁니다. 취소는 그대로 진행합니다.",
                self.buy_records_path,
                exc_info=True,
            )
            return None

    def _clear_buy_records(self) -> None:
        """인계가 끝난 기록을 지운다 — 남겨두면 15:15 마감 정리가 같은 메일을 또 보낸다."""
        try:
            self.buy_records_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "매수 기록 파일을 지우지 못했습니다 (%s) — 매수 결과 메일이 중복될 수 있습니다.",
                self.buy_records_path,
                exc_info=True,
            )

    def _notify_buy_result(
        self,
        cash: float,
        amount_per_stock: float,
        records: List[BuyRecord],
        fills_synced: bool,
    ) -> None:
        """매수 실행 결과를 이메일로 알린다 (PRD 5.5-B 6단계).

        렌더링·발송 실패가 매수 흐름을 되돌릴 수는 없으므로 예외를 밖으로 올리지 않는다.
        """
        execution = BuyExecution(
            at=datetime.now(),
            cash=cash,
            amount_per_stock=amount_per_stock,
            records=records,
            # 단순익절이 켜져 있으면 그날의 익절선은 설정값(%)이 아니라 0이다 — 메일의
            # 종목별 익절가가 실제 트리거(손익분기 가격)와 어긋나지 않게 0을 넘긴다
            take_profit_percent=(
                0.0
                if self.engine.risk_manager.simple_take_profit_enabled
                else self.engine.risk_manager.take_profit_ratio * 100
            ),
            simple_take_profit=self.engine.risk_manager.simple_take_profit_enabled,
            stop_loss_percent=self.engine.risk_manager.stop_loss_ratio * 100,
            commission_percent=self.engine.risk_manager.commission_rate * 100,
            tax_percent=self.engine.risk_manager.tax_rate * 100,
            slippage_percent=self.engine.risk_manager.slippage_rate * 100,
            fills_synced=fills_synced,
        )
        try:
            subject, body, html = templates.buy_result_email(execution)
            self.email.send(subject, body, html)
            logger.info(
                "매수 결과 메일 발송 — 접수/체결 %d종목, 투입 %s원",
                len(execution.ordered),
                f"{execution.invested:,.0f}",
            )
        except Exception:
            logger.exception("매수 결과 메일 발송 실패")

    def _fill_buy_prices(self, records: List[BuyRecord]) -> bool:
        """접수된 주문의 체결가를 조회해 기록에 채운다. 조회에 실패하면 False.

        여기서도 체결이 잡히지 않은 종목은 '접수' 상태로 남는다 — 호출측
        (cancel_unfilled_buys)이 취소된 주문번호와 대조해 '취소'로 확정한다.
        """
        # 거부된 주문도 주문번호를 갖고 있으므로 접수된 건만 조회 대상에 넣는다
        pending = {r.order_id: r for r in records if r.order_id and r.outcome.is_ordered}
        if not pending:
            return True

        try:
            fills = self.engine.order_client.get_today_fills()
        except Exception:
            logger.warning("매수 직후 체결 조회 실패 — 접수 기준으로 알립니다.", exc_info=True)
            return False

        for fill in fills:
            record = pending.get(fill.order_id)
            if record is None or fill.side != OrderSide.BUY or fill.filled_quantity <= 0:
                continue
            record.filled_quantity = fill.filled_quantity
            record.filled_price = fill.filled_price
            record.name = record.name or fill.name
            record.outcome = (
                BuyOutcome.PARTIALLY_FILLED if fill.unfilled_quantity > 0 else BuyOutcome.FILLED
            )
        return True

    def send_final_report(self, today: Optional[date] = None, closed_out: bool = False) -> None:
        """하루의 마지막 결과 리포트 — 어느 트리거가 먼저 오든 한 번만 보낸다.

        15:35 스케줄과 '보유 종목 전량 매도 완료'(runtime.watch_closeout_report)가 이 함수를
        공유한다. 먼저 온 쪽이 보내고 나머지는 건너뛰므로, 15:35 직전에 청산이 끝나도
        같은 리포트가 두 번 나가지 않는다. 발송 표시는 파일에 남아 엔진이 재시작돼도
        유지된다 (DEFAULT_REPORT_MARK_PATH 참고).

        발송에 실패하면 표시를 세우지 않는다 — 뒤에 오는 트리거가 다시 시도한다.
        ④ 즉시 실행 버튼은 사용자가 직접 누른 것이므로 이 표시와 무관하게 항상 발송한다.
        """
        today = today or date.today()
        trigger = "전량 매도 완료" if closed_out else "15:35 스케줄"
        if self._report_mark() == today:
            logger.info("최종 리포트를 이미 발송했습니다 — %s 발송을 건너뜁니다.", trigger)
            return

        if closed_out and not self._sells_settled(today):
            logger.info(
                "청산 매도의 체결이 아직 확인되지 않아 최종 리포트를 미룹니다 — 15:35 스케줄에 맡깁니다."
            )
            return

        logger.info("최종 리포트 발송 (%s)", trigger)
        self.send_daily_report(today, closed_out=closed_out)
        self._write_report_mark(today)

    def _sells_settled(self, today: date) -> bool:
        """당일 매도가 전부 체결(또는 거부)로 확정됐는지 — 청산 즉시 발송의 전제 (PRD 5.11).

        청산은 주문 **접수** 시점에 완료로 표시되므로(`TradingEngine._mark_exited`), 체결이
        늦으면 그 매도가 pending으로 남아 집계에서 빠진다 — 판 종목이 '보유중'으로, 손익이
        0으로 나간 채 리포트가 확정된다.

        판단 전에 체결 결과를 한 번 당겨온다. 그러지 않으면 방금 낸 주문이 언제나 pending으로
        보여 청산 즉시 발송이 영영 걸리지 않는다. 뒤이어 `send_daily_report`가 같은 동기화를
        한 번 더 하지만, 이 경로는 하루 한 번뿐이라 그대로 둔다.
        """
        self._sync_fills(today)
        return not self.trade_store.has_unsettled_sells(today)

    def _report_mark(self) -> Optional[date]:
        """최종 리포트를 마지막으로 보낸 날짜. 마커가 없거나 읽을 수 없으면 None.

        읽기에 실패하면 '아직 안 보냈다'로 본다 — 리포트가 중복되는 편이 아예 빠지는
        것보다 낫다.
        """
        try:
            return date.fromisoformat(self.report_mark_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _write_report_mark(self, today: date) -> None:
        """발송 표시를 남긴다. 실패해도 리포트는 이미 나갔으므로 흐름을 막지 않는다."""
        try:
            self.report_mark_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_mark_path.write_text(today.isoformat(), encoding="utf-8")
        except OSError:
            logger.warning(
                "최종 리포트 발송 표시를 남기지 못했습니다 (%s) — 15:35에 다시 나갈 수 있습니다.",
                self.report_mark_path,
                exc_info=True,
            )

    def _clear_report_mark(self) -> None:
        """발송 표시를 지운다 — 재매수로 앞선 리포트가 더 이상 최종이 아닐 때."""
        try:
            self.report_mark_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "최종 리포트 발송 표시를 지우지 못했습니다 (%s) — 이번 보유분의 청산 "
                "리포트가 생략될 수 있습니다.",
                self.report_mark_path,
                exc_info=True,
            )

    def send_daily_report(self, today: Optional[date] = None, closed_out: bool = False) -> None:
        """당일 매매 결과와 월간 누적 실적을 이메일로 발송 (15:35 또는 전량 매도 직후)."""
        today = today or date.today()
        sync_failed = not self._sync_fills(today)

        summary = self.trade_store.daily_summary(today)
        monthly = self.trade_store.monthly_summary(today.year, today.month, up_to=today)
        yearly = self.trade_store.yearly_summary(today.year, up_to=today)

        snapshot = self.account.get_balance_snapshot()
        # 기간초 자산 추정치 = 현재 총자산 - 그 기간의 순손익. 수수료·세금도 계좌에서 빠져나간
        # 금액이므로 실현손익이 아니라 순손익을 되돌려야 기간 시작 시점 자산에 맞는다.
        # **연 단위는 그만큼 더 거칠다** — 연중 입출금이 있으면 그 금액만큼 어긋난다 (PRD 5.11).
        monthly.base_asset = snapshot.total_asset - monthly.net_pnl
        yearly.base_asset = snapshot.total_asset - yearly.net_pnl

        # 그래프는 못 그려도(그 기간 매매가 없는 등) 리포트는 그대로 나간다
        chart_png = chart.render_monthly_cumulative(
            self.trade_store.monthly_cumulative_series(today.year, today.month, up_to=today)
        )
        yearly_chart_png = chart.render_yearly_cumulative(
            self.trade_store.yearly_cumulative_series(today.year, up_to=today)
        )

        subject, body, html = templates.daily_report_email(
            summary,
            monthly,
            yearly,
            snapshot.cash,
            sync_failed=sync_failed,
            closed_out=closed_out,
            # 메일만 보는 상황에서도 매도되지 않고 남은 종목을 알 수 있어야 한다
            unsellable=self.engine.unsellable_snapshot(),
            chart_cid=MONTHLY_CHART_CID if chart_png else None,
            yearly_chart_cid=YEARLY_CHART_CID if yearly_chart_png else None,
        )
        images = {
            cid: png
            for cid, png in (
                (MONTHLY_CHART_CID, chart_png),
                (YEARLY_CHART_CID, yearly_chart_png),
            )
            if png
        }
        self.email.send(subject, body, html, images=images or None)
        logger.info("Daily report email sent for %s", today)

    def _sync_fills(self, today: date) -> bool:
        """집계 전에 체결 결과를 반영한다. 실패해도 리포트 발송 자체는 막지 않는다.

        주문 접수 기록은 pending으로 남아 있어, 이 동기화를 건너뛰면 당일 매매가
        한 건도 없는 것처럼 집계된다.
        """
        try:
            fills = self.engine.order_client.get_today_fills()
            self.trade_store.apply_fills(fills, today)
            return True
        except Exception:
            logger.exception("체결 내역 동기화 실패 — 접수 기준으로 리포트를 발송합니다.")
            return False
