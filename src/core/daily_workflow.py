import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from src.api.account import AccountClient
from src.core import exit_review
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
    TuningOutcome,
    format_stock,
)
from src.llm import exit_reviewer as exit_reviewer_module
from src.llm import exit_tuner as exit_tuner_module
from src.llm.exit_advisor import build_exit_locked_text, make_exit_prompt_store
from src.logger.trade_store import MonthlySummary, TradeStore
from src.notification.email import EmailNotifier
from src.notification import chart, templates
from src.strategy import etf_regime
from src.strategy.base import StockRecommendation
from src.strategy.etf_regime import ETFRegimeStrategy

logger = logging.getLogger(__name__)

# 2호 전략 시장가 매수의 증거금 배수 — 키움은 시장가 매수 증거금을 상한가(+30%)로 잡는다.
# 수량을 이 배수 안에서 정하지 않으면 투입 비율이 높을 때 증거금 부족으로 주문이 거부된다.
MARKET_BUY_MARGIN = 1.3

# 최종 리포트를 보낸 날짜를 남기는 마커 파일. 인메모리 필드로 두면 엔진 재시작
# (설정 저장·앱 재실행)마다 DailyWorkflow가 새로 만들어지면서 표시가 사라져,
# 오전에 이미 보낸 리포트를 15:35가 다시 보낸다.
DEFAULT_REPORT_MARK_PATH = Path("data") / "final_report_sent"

# 매수 결과를 10:10 마무리까지 넘기는 파일. 인메모리 필드로 두면 그 사이 엔진이
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

# 키움이 휴장일 주문을 거부할 때 돌려주는 문구 (2026-09-24 실측:
# "[2000](571489:장이 열리지않는 날입니다.)"). 띄어쓰기가 흔들려도 걸리도록 두 표기를 본다.
MARKET_CLOSED_REJECT_TEXTS = ("장이 열리지않는 날", "장이 열리지 않는 날")


def _is_market_closed_rejection(message: Optional[str]) -> bool:
    """주문 거부 사유가 '휴장'인지 (PRD 10절 '휴장일 자동 판정')."""
    text = message or ""
    return any(marker in text for marker in MARKET_CLOSED_REJECT_TEXTS)


def _no_change_outcome(result) -> TuningOutcome:
    """튜너가 고치지 않기로 했거나(change=False) 응답을 못 받은(None) 결과."""
    if result is None:
        return TuningOutcome(changed=False, reason="판단 실패")
    return TuningOutcome(changed=False, reason=getattr(result, "reason", "") or "고칠 필요 없음")


# 잔고 대조로 접수 행을 옮길 때 쓰는 상태 (`_settled_row`) — 부분체결은 표에 남지만
# 체크 열은 비운다. 남은 미체결분만 골라 취소하면 이미 체결된 몫까지 덮어쓰기 때문이다.
BUY_PARTIAL_STATUS = templates.BUY_OUTCOME_LABELS[BuyOutcome.PARTIALLY_FILLED]

# 리포트 메일에 인라인 첨부되는 누적 그래프의 Content-ID (월: 날짜별, 연: 달별).
MONTHLY_CHART_CID = "monthly-cumulative"
YEARLY_CHART_CID = "yearly-cumulative"


@dataclass
class BuyRecordState:
    """매수 시각이 남기고 10:10이 집어 가는 매수 실행 상태 (`DEFAULT_BUY_RECORDS_PATH`)."""

    cash: float                # 매수 산정에 쓴 예수금
    amount_per_stock: float    # 종목당 배정액
    records: List[BuyRecord] = field(default_factory=list)


class DailyWorkflow:
    """2호 전략의 하루 흐름을 스케줄러 트리거에 연결한다 (PRD 5.5-C, 5.11).

    결정 시각 recommend_and_notify(결정 → 곧바로 execute_buys로 시장가 매수) → 매수 시각
    execute_buys(재시작 대비 안전망) → 10:10 cancel_unfilled_buys → 15:35 send_final_report
    (보유 종목이 그 전에 전량 매도되고 체결까지 확인되면 15:35를 기다리지 않고 최종 리포트를 보낸다)
    → 15:40 review_exits (매도 판단 검증 + 매도 프롬프트 자동 수정)

    1호 전략(LLM 대형주 추천·뉴스 검증·추천 검증과 추천 프롬프트 자동 수정)은 2026-10-23에 폐지됐다.
    매도 판단 검증·매도 프롬프트 자동 수정은 2026-10-29에 2호(ETF)용으로 복구했다.
    """

    def __init__(
        self,
        strategy: ETFRegimeStrategy,
        engine: TradingEngine,
        account: AccountClient,
        trade_store: TradeStore,
        email: EmailNotifier,
        exit_reviewer=None,
        exit_tuner=None,
        exit_prompt_store=None,
        tune_skip_monthly_return_ratio: float = 0.05,
        ws_client=None,
        report_mark_path: Optional[Path] = None,
        buy_records_path: Optional[Path] = None,
        etf_kill_loss_percent: float = 5.0,
        etf_start_date: Optional[date] = None,
        signal_band_percent: float = 0.0,
        flip_enabled: bool = True,
    ):
        self.strategy = strategy
        self.engine = engine
        self.account = account
        self.trade_store = trade_store
        self.email = email
        # 15:40 매도 판단 검증의 LLM 평가 모듈 (PRD 5.14). None이면 수치만 남긴다.
        self.exit_reviewer = exit_reviewer
        # 매도 프롬프트 자동 수정 (PRD 5.15). None이면 단계 자체를 건너뛴다.
        self.exit_tuner = exit_tuner
        self.exit_prompt_store = (
            exit_prompt_store if exit_prompt_store is not None else make_exit_prompt_store()
        )
        # 이번 달 순수익률이 이 값 이상이면 매도 프롬프트를 고치지 않는다 (스펙 4-A)
        self.tune_skip_monthly_return_ratio = tune_skip_monthly_return_ratio
        self.ws_client = ws_client
        # 최종 리포트를 보낸 날짜 — 전량 매도 완료와 15:35 스케줄이 중복 발송하지 않도록
        # 공유하는 표시다 (send_final_report). 매수로 보유가 다시 생기면 초기화된다.
        # 엔진 재시작을 견뎌야 하므로 파일에 남긴다 (DEFAULT_REPORT_MARK_PATH 참고).
        self.report_mark_path = Path(
            report_mark_path if report_mark_path is not None else DEFAULT_REPORT_MARK_PATH
        )
        # 매수 결과를 10:10 마무리(cancel_unfilled_buys)까지 넘기는 파일 — 지정가
        # 주문은 접수 시점에 체결 여부를 알 수 없어, 결과 메일을 그때 보내야 확정된 값이 실린다.
        # 인메모리가 아니라 파일인 이유는 DEFAULT_BUY_RECORDS_PATH 주석 참고.
        self.buy_records_path = Path(
            buy_records_path if buy_records_path is not None else DEFAULT_BUY_RECORDS_PATH
        )
        # UI '매수 예정' 표가 읽는 스냅샷 — (날짜, 행 목록) 한 쌍을 통째로 갈아끼운다
        # (`buy_plan_snapshot` 참고). 주문 기록 파일은 10:10에 지워지므로 표의 출처가 될 수 없다.
        self._buy_board: tuple = (None, ())
        # 오늘 '전량 체결'을 한 번이라도 관찰한 종목. 잔고 대조만으로는 매도 뒤에
        # 판정이 되돌아가 매수예정 표로 행이 살아 돌아온다 (2026-09-01 실측).
        # _buy_board와 같이 (날짜, 값) 한 튜플로 통째 갈아끼워 UI 스레드가 일관되게 읽는다.
        self._settled_board: tuple = (None, frozenset())
        # 자동 중단 장치 (PRD 5.5-C) — `_etf_kill_reason`
        self.etf_kill_loss_percent = etf_kill_loss_percent
        self.etf_start_date = etf_start_date
        self.signal_band_percent = signal_band_percent
        self.flip_enabled = flip_enabled
        # 오늘 2호 결정(매수/쉼/중단)을 마친 날짜 — 매수 시각 안전망이 결정을 되풀이하지 않게 한다.
        # 인메모리라 엔진이 재시작되면 비고, 그때는 안전망이 다시 정한다 (`_execute_etf_buy`).
        self._etf_decided_on: Optional[date] = None

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
        done = self._observe_settled(day, rows, held)
        settled = (self._settled_row(r, held, done) for r in rows)
        return [
            replace(r, current_price=self.engine.last_price(r.ticker))
            for r in settled
            if r is not None
        ]

    def _observe_settled(self, day: date, rows, held: dict) -> frozenset:
        """지금 전량 체결로 보이는 종목을 기억에 더하고, 오늘의 기억 전체를 돌려준다.

        판 뒤에는 잔고가 0이라 '아직 체결 안 됨'과 구분되지 않는다. 한 번 본 체결을
        기억해 두지 않으면 매도한 종목이 매수예정 표에 '접수'로 되살아난다.
        기억은 그날 것이다 — 날짜가 넘어가면 버린다 (`_buy_board`와 같은 규약).
        """
        remembered_day, done = self._settled_board
        if remembered_day != day:
            done = frozenset()
        seen = {
            r.ticker
            for r in rows
            if r.status == BUY_ORDERED_STATUS
            and r.quantity > 0
            and held.get(r.ticker, 0) >= r.quantity
        }
        if not seen <= done:
            done = done | seen
            self._settled_board = (day, done)   # 통째로 갈아끼운다 (제자리 수정 금지)
        return done

    @staticmethod
    def _settled_row(row: BuyPlanView, held: dict, done: frozenset = frozenset()) -> Optional[BuyPlanView]:
        """접수 행을 보유 수량과 대조해 체결 상태로 옮긴다. 표에서 뺄 행은 None (PRD 5.10).

        체결 반영(`_fill_buy_prices`)은 10:10·15:15에만 도는데, 지정가가 허용 밴드 상단으로
        올라간 뒤로는 접수 직후 체결되는 것이 보통이다. 그 사이 표가 '접수'로 얼어붙어 같은
        종목이 보유 종목 표와 겹쳐 실린다 (2026-08-26 13:40 실측).

        **표시만 바꾸고 기록은 건드리지 않는다** — 기록과 결과 메일을 확정하는 것은 체결내역
        조회를 보는 10:10·15:15의 일이고, 잔고 대조는 표를 맞추기 위한 근사치다. 전일부터
        들고 있던 종목이 추천되면 체결로 오판하는 한계가 있다 (당일 청산이라 실무상 드물다).

        `done`은 오늘 전량 체결을 이미 관찰한 종목이다 (`_observe_settled`). 잔고만 보면
        매도 뒤 판정이 되돌아가 판 종목이 '접수'로 살아 돌아온다 (2026-09-01 실측).
        부분체결 뒤 매도한 경우는 아직 되돌아간다 — 관찰한 수량까지 기억하지는 않는다.
        """
        if row.status != BUY_ORDERED_STATUS or row.quantity <= 0:
            return row
        if row.ticker in done:
            return None  # 체결을 이미 봤다 — 그 뒤 팔렸어도 매수예정으로 돌아가지 않는다
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

    def _board_from_recommendations(self, recommendations) -> List[BuyPlanView]:
        """추천 직후의 표 — 실제로 매수할 상위 몇 종목만 담는다 (build_buy_plans와 같은 기준)."""
        return [
            BuyPlanView(
                ticker=r.ticker,
                label=format_stock(r.ticker, r.name),
                status=BUY_PENDING_STATUS,
                buy_price=float(r.target_price),
            )
            for r in recommendations[: self.strategy.target_stock_count]
        ]

    def _board_from_records(self, records: List[BuyRecord]) -> List[BuyPlanView]:
        """매수 시각 이후의 표 — 매수하지 못한 종목은 수량을 비우고 사유만 남긴다.

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
                note=r.note or "",
            )
            for r in records
            if r.outcome != BuyOutcome.FILLED
        ]

    # ── 2호 전략 (PRD 5.5-C) ────────────────────────────────
    def recommend_and_notify(self, today: Optional[date] = None) -> None:
        """결정 시각(버튼 ①) — 오늘 살 ETF를 정해 메일로 알린다. 매수는 같은 실행의 다음 단계다
        (`actions.manual_steps`의 'recommend')."""
        self._recommend_etf(today or date.today())

    def execute_buys(self) -> None:
        """매수(버튼 ②, 매수 시각 안전망) — 정한 ETF를 시장가로 즉시 산다 (`_execute_etf_buy`)."""
        self._execute_etf_buy(date.today())

    def _recommend_etf(self, today: date) -> None:
        """결정 시각 — 자동 중단 확인 → 휴장 확인 → 국면 신호 → 오늘 살 ETF 1종목을 정해 메일로 알린다.

        결정은 메모리에만 둔다 — 엔진이 재시작되면 매수 시각 안전망이 다시 정한다
        (`_execute_etf_buy`). 판단 재료가 전날까지의 일봉이라 다시 정해도 같다.
        """
        blocked = self._etf_kill_reason(today)
        if blocked:
            logger.warning("2호 전략 자동 중단: %s", blocked)
            self.strategy.set_recommendations([])
            self._etf_decided_on = today
            self._send_etf_mail(today, None, None, blocked)
            return

        market_data = self.engine.market_data
        ticker = etf_regime.LONG_ETF[0]
        try:
            # 한 번 조회가 20거래일치라 당일 봉이 섞이면 지난 봉이 19개뿐이다 — 기준일을 하루
            # 당긴 조회를 합쳐 20개를 채운다 (`intraday_returns`가 같은 날짜를 한 번만 센다)
            recent = market_data.get_ohlcv(ticker, base_date=today)
            earlier = market_data.get_ohlcv(ticker, base_date=today - timedelta(days=1))
        except Exception:
            logger.exception("2호 전략 신호용 일봉 조회 실패 — 오늘 매수를 스킵합니다.")
            self.engine.notify("[경고] 2호 전략 일봉 조회 실패 — 오늘 매수를 스킵합니다.")
            return

        if not etf_regime.traded_today(recent, today):
            self.engine.note_market_closed("KODEX 200 일봉에 당일 거래가 없습니다")
            return

        signal = etf_regime.regime_signal(list(recent) + list(earlier), today)
        day_return = etf_regime.today_return(recent, today)
        pick = etf_regime.pick_etf(signal, day_return, self.signal_band_percent, self.flip_enabled)
        # 신호만으로 정한 방향과 다르면 오늘 장중 흐름 때문에 뒤집힌 것이다
        flipped = pick is not None and pick != etf_regime.pick_etf(signal, None, self.signal_band_percent)
        if pick is None:
            if signal is None:
                logger.info("2호 전략: 오늘은 쉽니다 (신호 계산 불가 — 일봉 부족)")
            else:
                logger.info(
                    "2호 전략: 오늘은 쉽니다 (신호 %+.3f%% — 문턱 ±%g%% 안쪽)", signal, self.signal_band_percent
                )
            self.strategy.set_recommendations([])
            self._etf_decided_on = today
            self._send_etf_mail(today, signal, None, None)
            return

        pick_ticker, pick_name = pick
        try:
            price = market_data.get_current_price(pick_ticker).price
        except Exception:
            logger.exception("2호 전략 현재가 조회 실패 — 오늘 매수를 스킵합니다: %s", pick_ticker)
            self.engine.notify(f"[경고] 2호 전략 현재가 조회 실패 ({pick_name}) — 오늘 매수를 스킵합니다.")
            return
        if price <= 0:
            logger.error("2호 전략 현재가가 0입니다 — 오늘 매수를 스킵합니다: %s", pick_ticker)
            return

        # 기준가 = 결정 시점 현재가 — 실제 주문은 시장가라, 표·메일에 보이는 참고값이다
        recommendation = StockRecommendation(
            ticker=pick_ticker,
            name=pick_name,
            target_price=int(price),
            reason=(
                f"직전 {etf_regime.LOOKBACK_DAYS}거래일 장중 평균 {signal:+.3f}%"
                + (f" — 오늘 KODEX 200 시가 대비 {day_return:+.2f}%라 방향을 뒤집었다" if flipped else "")
            ),
            recommend_price=price,
            setup=etf_regime.SETUP,
            # AI 매도 판단이 '아침 전망'으로 읽는 칸 — 2호는 가격 전망이 없으므로 무엇이 시나리오인지 적는다
            outlook=(
                (
                    "2호 전략(지수 ETF 국면 추종): 가격 목표 없이 방향만 정했다. 20일 신호와 반대로 "
                    f"오늘 KODEX 200이 시가 대비 {day_return:+.2f}% 움직여 오늘 장중 방향을 따라 뒤집었다. "
                    "시나리오는 '오늘 장중 흐름이 이어진다'이며, 그 흐름이 다시 꺾였다면 시나리오가 깨진 것이다."
                    if flipped
                    else "2호 전략(지수 ETF 국면 추종): 가격 목표 없이 방향만 정했다. 시나리오는 '최근 "
                    f"{etf_regime.LOOKBACK_DAYS}거래일의 장중 흐름이 오늘도 이어진다'이며, 오늘 장중 "
                    "흐름이 그 반대로 뚜렷하게 돌아섰다면 시나리오가 깨진 것이다."
                )
            ),
        )
        self.strategy.set_recommendations([recommendation])
        self._etf_decided_on = today
        board = self._board_from_recommendations([recommendation])
        self._set_buy_board(today, board)
        self._watch_plan_prices([pick_ticker])
        logger.info(
            "2호 전략: %s 매수 결정 (신호 %+.3f%%, 오늘 %s, 기준가 %s원%s) — 곧바로 시장가로 삽니다",
            pick_name, signal,
            "시가 대비 모름" if day_return is None else f"시가 대비 {day_return:+.2f}%",
            f"{price:,.0f}", ", 방향 뒤집음" if flipped else "",
        )
        self._send_etf_mail(today, signal, recommendation, None)

    def _execute_etf_buy(self, today: date) -> None:
        """2호 전략 매수 — 결정한 ETF를 **시장가로 즉시** 산다 (PRD 5.5-C '매수', 확정 2026-10-23).

        추천 단계 직후(같은 실행)와 매수 시각(안전망) 두 번 불린다. 오늘 이미 주문했으면
        아무것도 하지 않는다 — 매수 기록 파일이 그 표시다. 결정이 없으면(추천 뒤 엔진 재시작
        — 2026-10-23 09:07에 결정이 사라져 매수가 통째로 빠졌다) 그 자리에서 다시 정한다.
        판단 재료가 전날까지의 일봉이라 다시 정해도 결과가 같다.

        지정가 밴드를 쓰지 않는 이유: 2호는 가격 예측이 아니라 그날 방향에 올라타는 규칙이라,
        기준가보다 올랐다고 건너뛰면 오르는 날만 놓친다.
        """
        if self._read_buy_records(today) is not None:
            logger.info("2호 전략: 오늘 이미 매수 주문을 냈습니다 — 건너뜁니다.")
            return
        if self._etf_decided_on != today:
            self._recommend_etf(today)

        cash = self.account.get_cash()
        plans = self.strategy.build_buy_plans(cash)
        if not plans:
            logger.info("2호 전략: 오늘 매수할 종목이 없습니다. 주문가능금액 %s원", f"{cash:,.0f}")
            return

        plan = plans[0]
        label = format_stock(plan.ticker, plan.name)
        try:
            price = self.engine.market_data.get_current_price(plan.ticker).price
        except Exception:
            logger.exception("2호 전략 매수 직전 현재가 조회 실패 — 기준가로 수량을 정합니다: %s", label)
            price = float(plan.recommend_price or plan.target_price)
        if price <= 0:
            logger.error("2호 전략 매수 가격을 알 수 없습니다 — 매수를 건너뜁니다: %s", label)
            return

        # 키움은 시장가 매수 증거금을 상한가(+30%) 기준으로 잡는다 — 그 한도 안에서 수량을 정한다
        quantity = int(min(plan.amount // price, cash // (price * MARKET_BUY_MARGIN)))
        record = BuyRecord(ticker=plan.ticker, name=plan.name, quantity=quantity, reference_price=price)
        if quantity <= 0:
            record = replace(record, outcome=BuyOutcome.SKIPPED,
                             note=f"1주 {price:,.0f}원을 살 금액이 없습니다 (배정 {plan.amount:,.0f}원)")
            logger.warning("2호 전략 매수 건너뜀: %s — %s", label, record.note)
        else:
            request = OrderRequest(
                ticker=plan.ticker, side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=quantity, price=None, name=plan.name,
            )
            positions = self.account.get_positions()
            if not self.engine.risk_manager.approve(request, positions, reference_price=price):
                record = replace(record, outcome=BuyOutcome.FAILED, note="리스크 관리 규칙에 걸려 주문하지 않았습니다")
                self.engine.notify(f"매수 거부 (리스크 관리): {label}")
            else:
                result = self.engine.order_client.send_order(request)
                self.engine.risk_manager.record_order(result)
                self.trade_store.record_fill(result)
                if result.status == OrderStatus.REJECTED:
                    if _is_market_closed_rejection(result.error_message):
                        self.engine.note_market_closed("주문이 '장이 열리지않는 날'로 거부됐습니다")
                    self.engine.notify(f"[실패] 2호 전략 매수 거부: {label} x{quantity}주 — {result.error_message}")
                    record = replace(record, outcome=BuyOutcome.FAILED, order_id=result.order_id,
                                     note=f"주문 거부: {result.error_message}")
                else:
                    record = replace(record, order_id=result.order_id)
                    self.engine.note_open_position(plan.ticker)
                    self._clear_report_mark()
                    if self.ws_client is not None:
                        self.ws_client.subscribe([plan.ticker])
                    logger.info(
                        "2호 전략 시장가 매수 접수: %s %d주 (현재가 %s원, 주문번호 %s)",
                        label, quantity, f"{price:,.0f}", result.order_id,
                    )

        self._set_buy_board(today, self._board_from_records([record]))
        # 접수된 주문만 남긴다 — 이 파일이 "오늘 이미 주문함" 표시라, 실패까지 남기면 매수 시각
        # 안전망이 재시도하지 못한다
        if record.outcome == BuyOutcome.ORDERED:
            self._write_buy_records(cash, plan.amount, [record])

    def _etf_kill_reason(self, today: date) -> Optional[str]:
        """자동 중단 장치 — 2호 ETF 매매 건별 순수익률 누적합이 문턱 이하면 사유를, 아니면 None.

        집계가 실패하면 막지 않는다 — 장치는 손실을 끊는 그물이고, 기록 문제로 매매가 멈추면
        원인을 모른 채 하루가 빈다. 대신 로그를 남긴다.
        """
        if self.etf_kill_loss_percent <= 0:
            return None
        try:
            returns = self.trade_store.closed_trade_returns(
                self.etf_start_date, today, etf_regime.ETF_TICKERS
            )
        except Exception:
            logger.exception("2호 전략 누적 손익 집계 실패 — 자동 중단 판정을 건너뜁니다.")
            return None
        total = sum(returns)
        if total > -self.etf_kill_loss_percent:
            return None
        since = self.etf_start_date.isoformat() if self.etf_start_date else "처음"
        return (
            f"{since}부터 {len(returns)}건 누적 순수익률 {total:+.2f}%가 "
            f"한도 -{self.etf_kill_loss_percent:g}%에 닿았습니다. 다시 시작하려면 .env의 "
            f"ETF_START_DATE 또는 ETF_KILL_LOSS_PERCENT를 바꾸고 엔진을 재시작하세요."
        )

    def _send_etf_mail(
        self, today: date, signal: Optional[float], recommendation, blocked: Optional[str]
    ) -> None:
        """2호 전략의 아침 결정을 알린다 — 매수 / 쉼 / 자동 중단 세 경우를 한 꼴로."""
        signal_text = "계산 불가" if signal is None else f"{signal:+.3f}%"
        if blocked:
            subject = f"[2호 전략] {today} 자동 중단"
            body = blocked
        elif recommendation is None:
            subject = f"[2호 전략] {today} 오늘은 쉽니다"
            reason = (
                "일봉이 모자라 신호를 계산하지 못해"
                if signal is None
                else f"신호가 문턱 ±{self.signal_band_percent:g}% 안쪽이라"
            )
            body = (
                f"직전 {etf_regime.LOOKBACK_DAYS}거래일 장중 평균 {signal_text} — "
                f"{reason} 매수하지 않습니다."
            )
        else:
            subject = f"[2호 전략] {today} {recommendation.name} 시장가 매수"
            body = (
                f"직전 {etf_regime.LOOKBACK_DAYS}거래일 장중 평균 {signal_text}\n"
                f"매수 종목: {format_stock(recommendation.ticker, recommendation.name)} — 곧바로 시장가 주문\n"
                f"결정 시점 현재가: {recommendation.target_price:,}원\n"
                f"투입: 예수금의 {self.strategy.investable_ratio * 100:.0f}%, "
                f"청산: 손절 또는 15:15 (AI 매도 판단 없음)"
            )
        self.email.send(subject, body)

    def _watch_plan_prices(self, tickers: List[str]) -> None:
        """추천 종목의 실시간 시세를 미리 구독한다 — UI '매수 예정' 표 현재가의 출처.

        매수 접수분 구독(`execute_buys`)은 청산 감시가 목적이라 매수 시각에야 걸린다.
        추천~매수 사이에 현재가를 보려면(=어느 종목을 뺄지 판단하려면) 여기서 미리 걸어야 한다.
        """
        if not tickers or self.ws_client is None:
            return
        self.ws_client.subscribe(tickers)
        logger.info("추천 종목 시세 구독: %s", tickers)

    def buy_orders_filled(self, today: Optional[date] = None) -> bool:
        """오늘 접수한 매수 주문이 남김없이 전량 체결됐는지 — 결과 메일을 앞당길 근거.

        판정은 **당일 체결내역 조회의 주문번호 대조**다. 2026-09-01에 되돌린 구현(cb93b2b)은
        잔고를 대조했는데, 잔고는 "이 종목을 들고 있는가"에 답할 뿐 "내 주문이 체결됐는가"에
        답하지 못한다 — 이월 보유 종목이나 다른 인스턴스가 산 물량을 자기 체결로 오인해
        살아 있는 주문을 1시간 4분 일찍 취소했다 (PRD 10절).

        `_fill_buy_prices`와 같은 조회를 같은 방식으로 읽지만, 판정은 그보다 엄격하다.
        ka10076이 한 주문번호에 행을 몇 개 실어 보내는지는 확인된 바가 없다(PRD 10절,
        `_cancel_targets`의 주석) — 한 주문에 완전체결 행과 미체결 잔량이 남은 행이 함께
        잡히는 경우, 완전체결 행 하나만 보고 True로 판정하면 살아 있는 주문을 취소해
        버릴 수 있다. 그래서 **같은 주문번호의 모든 BUY 행을 함께 본다**: 그중 하나라도
        미체결 잔량(`unfilled_quantity > 0`)이 남아 있으면 그 주문은 아직 진행 중이고,
        전부 잔량이 없어도 **가장 큰 체결수량이 주문수량(`BuyRecord.quantity`) 이상**이어야
        완료로 본다 — `unfilled_quantity`는 키움 응답의 `oso_qty`를 파싱한 값인데, 필드가
        비어 있으면 0으로 읽혀(`to_int`) HTS에서 취소된 부분체결도 "미체결 없음"으로
        보이기 때문이다. 이 이중 조건이 전부 충족된 주문만 정산 완료로 센다.

        기록이 없거나(메일이 이미 나갔거나 주문이 없던 날) 접수 행이 없으면 **API를 부르지
        않고** False다. 부분체결·미체결이 남았거나, 체결수량이 주문수량에 못 미치거나,
        조회 결과에 흔적조차 없는 주문이 있거나, 조회가 실패하면 역시 False — 그런 날은
        종전대로 10:10이 마무리한다.

        ka10076이 한 주문에 체결마다 행을 하나씩 나눠 보내는 경우라면, 마지막 체결 전까지는
        그 주문의 어느 한 행이 `unfilled_quantity > 0`을 계속 들고 있어 하루 종일 False로
        남는다 — 조기 발송 대상에서 조용히 빠지고 10:10 마무리로 넘어갈 뿐이니 의도된
        안전한 쪽이다. 고치려 들지 말 것.
        """
        state = self._read_buy_records(today or date.today())
        if state is None:
            return False

        pending = {r.order_id: r for r in state.records if r.order_id and r.outcome.is_ordered}
        if not pending:
            return False

        try:
            fills = self.engine.order_client.get_today_fills()
        except Exception:
            logger.warning(
                "매수 체결 확인 조회 실패 — 결과 메일을 10:10에 맡깁니다.", exc_info=True
            )
            return False

        # 같은 주문번호에 행이 여럿 있을 수 있어(확인되지 않음), 주문번호별로 모아서 본다.
        unfilled_seen = set()
        max_filled = {}
        for fill in fills:
            if fill.side != OrderSide.BUY or fill.order_id not in pending:
                continue
            if fill.unfilled_quantity > 0:
                unfilled_seen.add(fill.order_id)
            if fill.filled_quantity > 0:
                max_filled[fill.order_id] = max(max_filled.get(fill.order_id, 0), fill.filled_quantity)

        for order_id, record in pending.items():
            if order_id in unfilled_seen:
                return False
            if max_filled.get(order_id, 0) < record.quantity:
                return False
        return True

    def cancel_unfilled_buys(self, today: Optional[date] = None) -> None:
        """10:10 — 목표가에 닿지 않은 매수 주문을 취소하고 매수 결과를 알린다 (PRD 5.5-B 6단계).

        취소 대상은 **당일 체결내역 조회**에서 찾는다. 매수 시각과 10:10 사이에 설정 저장 등으로
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

    # ── 매수 기록 파일 (매수 시각 → 10:10 인계) ────────────────────
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
        yearly = self.trade_store.yearly_summary(today.year, up_to=today)

        snapshot = self.account.get_balance_snapshot()
        monthly = self._monthly_summary_with_base(today, snapshot)
        # 연 단위 기간초 자산 추정치 — 월과 같은 식이다. **연 단위는 그만큼 더 거칠다** —
        # 연중 입출금이 있으면 그 금액만큼 어긋난다 (PRD 5.11).
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

    def _monthly_summary_with_base(self, today: date, snapshot) -> MonthlySummary:
        """이번 달 누적 실적 + 월초 자산 추정치 — 일일 리포트와 월 순수익 게이트가 함께 쓴다.

        월초 자산 추정치 = 현재 총자산 - 이번 달 순손익. 수수료·세금도 계좌에서 빠져나간
        금액이므로 실현손익이 아니라 순손익을 되돌려야 월초 자산에 맞는다 (PRD 5.11).
        """
        monthly = self.trade_store.monthly_summary(today.year, today.month, up_to=today)
        monthly.base_asset = snapshot.total_asset - monthly.net_pnl
        return monthly

    def _monthly_return_block_reason(self, today: date) -> Optional[str]:
        """이번 달 순수익률이 기준 이상이면 그 사유 — 매도 프롬프트를 고치지 않는다 (스펙 4-A).

        수익률을 알 수 없으면(잔고 조회 실패·기준자산 0 이하) 역시 사유를 돌려준다 — 아무것도
        바꾸지 않는 것이 기본 상태다. 막지 않으면 None. 사유는 검증 메일에 그대로 실린다.
        """
        try:
            snapshot = self.account.get_balance_snapshot()
        except Exception:
            logger.warning("잔고 조회 실패 — 월 순수익을 알 수 없어 프롬프트를 고치지 않습니다.", exc_info=True)
            return "잔고 조회 실패로 이번 달 순수익을 알 수 없음"
        monthly = self._monthly_summary_with_base(today, snapshot)
        if monthly.base_asset <= 0:
            logger.warning("월초 자산 추정치가 0 이하라 월 순수익을 알 수 없어 프롬프트를 고치지 않습니다.")
            return "월초 자산 추정치가 0 이하라 이번 달 순수익을 알 수 없음"
        ratio = monthly.net_pnl / monthly.base_asset
        if ratio >= self.tune_skip_monthly_return_ratio:
            logger.info(
                "이번 달 순수익 %+.2f%% ≥ 기준 %.1f%% — 프롬프트를 고치지 않습니다 (%s).",
                ratio * 100, self.tune_skip_monthly_return_ratio * 100, today,
            )
            return (
                f"이번 달 순수익 {ratio * 100:+.2f}% ≥ 기준 "
                f"{self.tune_skip_monthly_return_ratio * 100:.1f}%"
            )
        return None

    @staticmethod
    def _tune_for_review_mail(tune, today: date) -> Optional[TuningOutcome]:
        """검증 메일에 실을 자동 수정 결과를 받는다. 예상 못 한 예외도 메일을 막지 않는다.

        자동 수정은 검증과 한 단계로 묶여 있어, 여기서 삼키지 않으면 검증 메일까지 함께 사라진다.
        """
        try:
            return tune(today)
        except Exception:
            logger.exception("프롬프트 자동 수정이 실패했습니다 — 검증 메일은 그대로 보냅니다.")
            return TuningOutcome(changed=False, reason="판단 실패")

    def review_exits(self, today: Optional[date] = None) -> None:
        """15:40 — 오늘 매도한 ETF의 순손익을 AI 매도 판단 이력과 대조해 남기고, 매도
        프롬프트 자동 수정을 돌린 뒤 그 결과까지 실어 메일로 보낸다 (PRD 5.14·5.15).

        잣대는 순수익이다 (스펙 2026-09-22 1절). 분류는 코드가(`exit_review`), 평가문은 LLM이
        쓴다. 매도가 없던 날은 메일이 없지만 자동 수정은 돈다 (쌓인 표본이 재료다).
        어떤 실패도 매매에 영향이 없다. 자동 수정을 여기서 부르는 이유는 수정 여부를 검증
        메일에 싣기 위해서다. 2호(ETF) 종목(`etf_regime.ETF_TICKERS`)만 대상이다.
        """
        today = today or date.today()
        self._sync_fills(today)
        decisions = [
            d for d in self.trade_store.ai_exit_decisions_for(today)
            if d.ticker in etf_regime.ETF_TICKERS
        ]
        trades = [
            t for t in self.trade_store.daily_summary(today).trades
            if t.ticker in etf_regime.ETF_TICKERS
        ]
        rows = exit_review.build_exit_review_rows(
            today,
            trades,
            self.trade_store.position_peaks_for(today),
            decisions,
            self.trade_store.last_exit_reasons(today),
            self.trade_store.tickers_with_unknown_pnl(today),
        )
        if rows:
            self._fill_exit_reviews(today, rows, decisions)
            for row in rows:
                try:
                    self.trade_store.save_exit_review(row)
                except Exception:
                    logger.exception("매도 판단 검증 저장 실패: %s", row.label)
        else:
            logger.info("오늘 매도한 ETF가 없습니다 — 매도 판단 검증을 건너뜁니다 (%s).", today)

        # 검증 **다음**에 돈다 — 오늘 저장한 검증 결과까지 표본에 든다 (스펙 2026-09-22 3.1)
        tuning = self._tune_for_review_mail(self.tune_exit_prompt, today)
        if not rows:
            return

        subject, body = templates.exit_review_email(rows, decisions, today, tuning)
        self.email.send(subject, body)
        logger.info("매도 판단 검증 메일 발송 (%s, %d종목)", today, len(rows))

    def _fill_exit_reviews(self, today: date, rows, decisions) -> None:
        """LLM 평가문을 받아 rows에 채운다. 판정 불가(unknown) 종목은 보내지 않는다."""
        if self.exit_reviewer is None:
            return
        # 아침 시나리오는 2호의 오늘 결정(메모리)에서 읽는다 — 엔진이 재시작돼 비었으면 빈 문자열이다
        recommendations = {
            r.ticker: r for r in getattr(self.strategy, "_recommendations", None) or []
        }
        items = [
            exit_reviewer_module.ExitReviewInput(
                row=row,
                decisions=[d for d in decisions if d.ticker == row.ticker],
                outlook=recommendations[row.ticker].outlook if row.ticker in recommendations else "",
                target_sell_price=0,  # 2호는 목표 매도가가 없다
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

    def tune_exit_prompt(
        self, today: Optional[date] = None, force: bool = False
    ) -> Optional[TuningOutcome]:
        """15:40 — 매도 판단 검증 결과를 보고 매도 프롬프트의 편집 가능한 절을 고친다 (PRD 5.15).

        매도 판단 검증(`review_exits`)이 그 끝에서 부른다. 게이트는 순서대로 월 순수익(4-A) →
        표본 `MIN_REVIEWS_FOR_TUNING`건(4.3)이고, 둘 다 LLM 호출 전에 코드가 본다. 고친 날만 수정
        메일이 나가고, 고치지 않는 날이 정상이다. 결과는 검증 메일에 싣도록 돌려준다.
        표본은 ETF(2호) 행만 센다 — `exit_reviews`에 남은 옛 1호 주식 행은 들어오지 않는다.

        `force`는 월 순수익 게이트만 건너뛴다 (지금은 부르는 버튼이 없다). 표본 요건은 그대로 본다.
        """
        today = today or date.today()
        if self.exit_tuner is None:
            return None
        if not force:
            blocked = self._monthly_return_block_reason(today)
            if blocked:
                return TuningOutcome(changed=False, reason=blocked)

        old_version = self.exit_prompt_store.load_version()
        verified = self.trade_store.count_exit_reviews(old_version, etf_regime.ETF_TICKERS)
        if verified < exit_tuner_module.MIN_REVIEWS_FOR_TUNING:
            logger.info(
                "매도 프롬프트 %s로 검증된 종목이 %d건이라(기준 %d건) 고치지 않습니다 (%s).",
                old_version, verified, exit_tuner_module.MIN_REVIEWS_FOR_TUNING, today,
            )
            return TuningOutcome(
                changed=False,
                reason=(
                    f"현 버전 {old_version} 검증 {verified}건 < 기준 "
                    f"{exit_tuner_module.MIN_REVIEWS_FOR_TUNING}건"
                ),
            )

        rows = self.trade_store.recent_exit_reviews(tickers=etf_regime.ETF_TICKERS)
        stats = exit_tuner_module.group_by_version(rows)
        before = self.exit_prompt_store.load_sections()
        # 사람이 코드로 고친 이력(v5·v6)을 앞에 붙여 튜너가 그 수정을 되돌리지 않게 한다
        result = self.exit_tuner.tune(
            stats,
            rows,
            before,
            build_exit_locked_text(),
            exit_tuner_module.with_code_history(self.exit_prompt_store.why_history()),
        )
        if result is None or not result.change:
            logger.info(
                "매도 프롬프트를 고치지 않습니다 (%s): %s", today, getattr(result, "reason", "판단 실패")
            )
            return _no_change_outcome(result)

        sections = exit_tuner_module.sanitize_sections(result.sections)
        if not sections:
            logger.warning("매도 프롬프트 수정안이 안전장치에 전부 걸렸습니다 — 그대로 둡니다 (%s).", today)
            return TuningOutcome(changed=False, reason="수정안이 안전장치에 전부 걸림")

        try:
            new_version = self.exit_prompt_store.save(sections, result.reason, today)
        except OSError:
            logger.exception("매도 프롬프트 파일 쓰기에 실패했습니다 — 그대로 둡니다.")
            return TuningOutcome(changed=False, reason="프롬프트 파일 쓰기 실패")

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
        return TuningOutcome(
            changed=True, reason=result.reason, old_version=old_version, new_version=new_version
        )
