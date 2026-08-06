import logging
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from src.api.account import AccountClient
from src.core.engine import TradingEngine
from src.core.events import (
    BuyExecution,
    BuyOutcome,
    BuyRecord,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    format_stock,
)
from src.data.collector import DataCollector
from src.llm.recommender import LLMRecommender
from src.logger.trade_store import TradeStore
from src.notification.email import EmailNotifier
from src.notification import templates
from src.strategy.llm_momentum import LLMMomentumStrategy

logger = logging.getLogger(__name__)

# 최종 리포트를 보낸 날짜를 남기는 마커 파일. 인메모리 필드로 두면 엔진 재시작
# (설정 저장·앱 재실행)마다 DailyWorkflow가 새로 만들어지면서 표시가 사라져,
# 오전에 이미 보낸 리포트를 15:30이 다시 보낸다.
DEFAULT_REPORT_MARK_PATH = Path("data") / "final_report_sent"


class DailyWorkflow:
    """1호 전략의 하루 흐름을 스케줄러 트리거에 연결한다 (PRD 5.5-B, 5.11).

    추천 시각 recommend_and_notify → 09:00 execute_buys → 09:30 cancel_unfilled_buys
    → 15:30 send_final_report
    (보유 종목이 그 전에 전량 매도되면 15:30을 기다리지 않고 최종 리포트를 보낸다)
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
    ):
        self.collector = collector
        self.recommender = recommender
        self.strategy = strategy
        self.engine = engine
        self.account = account
        self.trade_store = trade_store
        self.email = email
        self.ws_client = ws_client
        # 최종 리포트를 보낸 날짜 — 전량 매도 완료와 15:30 스케줄이 중복 발송하지 않도록
        # 공유하는 표시다 (send_final_report). 매수로 보유가 다시 생기면 초기화된다.
        # 엔진 재시작을 견뎌야 하므로 파일에 남긴다 (DEFAULT_REPORT_MARK_PATH 참고).
        self.report_mark_path = Path(
            report_mark_path if report_mark_path is not None else DEFAULT_REPORT_MARK_PATH
        )
        # 09:00 매수 결과를 09:30 마무리(cancel_unfilled_buys)까지 들고 있는다 — 지정가
        # 주문은 접수 시점에 체결 여부를 알 수 없어, 결과 메일을 그때 보내야 확정된 값이 실린다.
        self._buy_records: List[BuyRecord] = []
        self._buy_cash: float = 0.0
        self._buy_amount_per_stock: float = 0.0

    def recommend_and_notify(self, today: Optional[date] = None) -> None:
        """08:45 — 당일 데이터 수집 → LLM 추천 → 결과를 이메일로 발송."""
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
        subject, body = templates.recommendation_email(
            recommendations, today, self.strategy.investable_ratio, self.strategy.target_stock_count
        )
        self.email.send(subject, body)
        logger.info("Recommendation email sent for %s", today)

    def execute_buys(self) -> None:
        """09:00 — 예수금 기준으로 자금을 배분해 추천 종목을 목표 매수가에 지정가 매수.

        체결 확인과 결과 메일은 여기서 하지 않는다 — 지정가 주문은 접수 직후에 체결 여부를
        알 수 없어, 09:30 `cancel_unfilled_buys`가 미체결분을 정리한 뒤에 알린다.
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
                price = float(plan.target_price)
                quantity = int(plan.amount // price)
                if quantity <= 0:
                    logger.warning(
                        "매수 건너뜀: %s — 목표가 1주 %s원이 종목당 배정액 %s원을 초과합니다.",
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
                            note=f"목표가 1주 {price:,.0f}원이 배정액 {plan.amount:,.0f}원을 초과",
                        )
                    )
                    continue

                logger.info(
                    "매수 산정: %s 목표가 %s원 × %d주 = %s원 (배정 %s원)",
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
            # 이 보유분이 전량 매도되면 리포트를 다시 보내고, 남으면 15:30이 보낸다.
            self._clear_report_mark()

        # 접수된 종목만 실시간 시세를 구독한다 — 익절/손절 감시(RiskManager.check_exit)의 전제.
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

        # 결과 메일은 09:30 cancel_unfilled_buys가 보낸다 — 지정가라 지금은 체결 여부를 모른다
        self._buy_records = records
        self._buy_cash = cash
        self._buy_amount_per_stock = plans[0].amount

    def cancel_unfilled_buys(self) -> None:
        """09:30 — 목표가에 닿지 않은 매수 주문을 취소하고 매수 결과를 알린다 (PRD 5.5-B 6단계).

        취소 대상은 인메모리 주문 목록이 아니라 **당일 체결내역 조회**에서 찾는다. 09:00과
        09:30 사이에 설정 저장 등으로 엔진이 재시작되면 인메모리 기록이 사라지는데, 그때도
        미체결 주문이 장 마감까지 방치되면 안 되기 때문이다.
        """
        cancelled_ids = self._cancel_unfilled_orders()

        if not self._buy_records:
            logger.info("이번 엔진 실행에서 접수한 매수 주문이 없어 결과 메일을 보내지 않습니다.")
            return

        records = self._buy_records
        self._buy_records = []

        # 체결 반영이 먼저다 — 부분체결분까지 채운 뒤에 남은 '접수' 상태만 취소로 확정한다
        fills_synced = self._fill_buy_prices(records)
        for record in records:
            if record.order_id in cancelled_ids and record.outcome == BuyOutcome.ORDERED:
                record.outcome = BuyOutcome.CANCELLED
                record.note = "목표 매수가에 닿지 않아 09:30에 미체결분을 취소했습니다"

        self._notify_buy_result(
            self._buy_cash, self._buy_amount_per_stock, records, fills_synced
        )

    def _cancel_unfilled_orders(self) -> set:
        """미체결 매수 주문을 취소하고, 취소에 성공한 주문번호 집합을 돌려준다."""
        try:
            fills = self.engine.order_client.get_today_fills()
        except Exception:
            logger.exception("미체결 매수 주문 조회 실패 — 취소를 건너뜁니다.")
            self.engine.notify("[경고] 미체결 매수 주문을 조회하지 못했습니다. 직접 확인하세요.")
            return set()

        targets = self._cancel_targets(fills)
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

    def _cancel_targets(self, fills) -> List[tuple]:
        """취소할 (주문번호, 종목코드, 종목명, 수량) 목록. 수량 0은 '잔량 전부'다.

        1순위는 체결내역 조회가 알려주는 미체결 잔량이다 — 이 경로는 인메모리 기록에
        의존하지 않아 엔진이 재시작돼도 동작한다.
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
            for record in self._buy_records
            if record.order_id
            and record.outcome == BuyOutcome.ORDERED
            and record.order_id not in known_ids
        )
        return targets

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
            take_profit_percent=self.engine.risk_manager.take_profit_ratio * 100,
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

        15:30 스케줄과 '보유 종목 전량 매도 완료'(runtime.watch_closeout_report)가 이 함수를
        공유한다. 먼저 온 쪽이 보내고 나머지는 건너뛰므로, 15:30 직전에 청산이 끝나도
        같은 리포트가 두 번 나가지 않는다. 발송 표시는 파일에 남아 엔진이 재시작돼도
        유지된다 (DEFAULT_REPORT_MARK_PATH 참고).

        발송에 실패하면 표시를 세우지 않는다 — 뒤에 오는 트리거가 다시 시도한다.
        ④ 즉시 실행 버튼은 사용자가 직접 누른 것이므로 이 표시와 무관하게 항상 발송한다.
        """
        today = today or date.today()
        trigger = "전량 매도 완료" if closed_out else "15:30 스케줄"
        if self._report_mark() == today:
            logger.info("최종 리포트를 이미 발송했습니다 — %s 발송을 건너뜁니다.", trigger)
            return

        logger.info("최종 리포트 발송 (%s)", trigger)
        self.send_daily_report(today, closed_out=closed_out)
        self._write_report_mark(today)

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
                "최종 리포트 발송 표시를 남기지 못했습니다 (%s) — 15:30에 다시 나갈 수 있습니다.",
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
        """당일 매매 결과와 월간 누적 실적을 이메일로 발송 (15:30 또는 전량 매도 직후)."""
        today = today or date.today()
        sync_failed = not self._sync_fills(today)

        summary = self.trade_store.daily_summary(today)
        monthly = self.trade_store.monthly_summary(today.year, today.month, up_to=today)

        snapshot = self.account.get_balance_snapshot()
        # 월초 자산 추정치 = 현재 총자산 - 이번 달 순손익. 수수료·세금도 계좌에서 빠져나간
        # 금액이므로 실현손익이 아니라 순손익을 되돌려야 월초 시점 자산에 맞는다.
        monthly.base_asset = snapshot.total_asset - monthly.net_pnl

        subject, body, html = templates.daily_report_email(
            summary,
            monthly,
            snapshot.cash,
            sync_failed=sync_failed,
            closed_out=closed_out,
            # 메일만 보는 상황에서도 매도되지 않고 남은 종목을 알 수 있어야 한다
            unsellable=self.engine.unsellable_snapshot(),
        )
        self.email.send(subject, body, html)
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
