from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDoubleValidator, QFont, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dotenv import load_dotenv

from config.settings import Settings
from src.core.runtime import MANUAL_ACTIONS, ORDER_ACTIONS
from src.ui.engine_thread import EngineThread
from src.ui.env_store import load_env, save_env

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).parent.parent.parent / ".env"

# ── 색상 상수 ────────────────────────────────────────────────
COLOR_BG = "#1e1e2e"
COLOR_SURFACE = "#2a2a3e"
COLOR_BORDER = "#3a3a5c"
COLOR_ACCENT = "#7c6af7"
COLOR_SUCCESS = "#50fa7b"
COLOR_DANGER = "#ff5555"
COLOR_WARNING = "#ffb86c"
COLOR_TEXT = "#cdd6f4"
COLOR_TEXT_DIM = "#6c7086"
# 국내 관행에 맞춰 수익은 빨강, 손실은 파랑으로 표기한다
COLOR_PROFIT = "#ff5555"
COLOR_LOSS = "#6ba3ff"

# 보유 종목 표 갱신 주기 — 캐시값만 읽으므로 API 호출이 발생하지 않는다
HOLDINGS_REFRESH_MS = 2000
HOLDINGS_COLUMNS = ("종목", "수량", "평단", "현재가", "손익")
# 매도하지 못한 종목 — 보유 목록에서 제외된 건은 보유 종목 표에 나타나지 않으므로
# 사유와 함께 따로 보여준다 (engine.UnsellableView)
UNSELLABLE_COLUMNS = ("종목", "사유", "시각")


def _table_style() -> str:
    """보유 종목·매도 불가 표가 같은 모양을 갖도록 스타일을 한 곳에서 만든다."""
    return f"""
        QTableWidget {{
            background: {COLOR_BG}; color: {COLOR_TEXT};
            border: 1px solid {COLOR_BORDER}; gridline-color: {COLOR_BORDER};
        }}
        QHeaderView::section {{
            background: {COLOR_SURFACE}; color: {COLOR_TEXT_DIM};
            border: none; border-bottom: 1px solid {COLOR_BORDER}; padding: 4px;
        }}
    """


def _radio_style(color: str, checked: bool) -> str:
    """선택 여부가 한눈에 보이도록 인디케이터와 글자 굵기를 함께 지정한다."""
    if checked:
        indicator = f"border: 2px solid {color}; background: {color};"
        text_style = f"color: {color}; font-weight: bold;"
    else:
        indicator = f"border: 2px solid {COLOR_TEXT_DIM}; background: transparent;"
        text_style = f"color: {COLOR_TEXT_DIM}; font-weight: normal;"
    return f"""
        QRadioButton {{ {text_style} spacing: 8px; padding: 4px 0; }}
        QRadioButton::indicator {{
            width: 14px; height: 14px;
            border-radius: 9px;   /* (14 + 2*2) / 2 = 9 → 완전한 원 */
            {indicator}
        }}
    """


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-weight: bold; letter-spacing: 1px;")
    return lbl


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {COLOR_BORDER};")
    return line


# ── 로그 핸들러 (UI TextEdit에 출력) ────────────────────────
class _QtLogHandler(logging.Handler):
    def __init__(self, signal):
        super().__init__()
        self._signal = signal

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._signal.emit(record.levelno, msg)


# ── 메인 윈도우 ──────────────────────────────────────────────
class MainWindow(QMainWindow):
    _log_signal = pyqtSignal(int, str)

    def __init__(self, auto_start: bool = False):
        super().__init__()
        self.setWindowTitle("AutoTrade")
        # 로그와 보유 종목을 좌우로 나누므로 기본 폭을 넓게 잡는다
        self.setMinimumSize(880, 640)
        self.resize(1020, 900)
        self._engine_thread: Optional[EngineThread] = None
        # 보유 종목 표를 만드는 도중에도 갱신이 한 번 돌기 때문에, 아직 없을 수 있음을 표시해 둔다
        self._unsellable_box: Optional[QGroupBox] = None
        self._setup_style()
        self._build_ui()
        self._setup_logging()
        self._load_settings()

        if auto_start:
            # 창이 완전히 뜬 뒤 "▶ 시작" 버튼을 누른 것과 동일하게 동작해야 하므로
            # 생성자 안에서 바로 부르지 않고 이벤트 루프가 돈 다음(0ms 지연)으로 미룬다.
            QTimer.singleShot(0, self._start_engine)

    # ── 스타일 ──────────────────────────────────────────────
    def _setup_style(self) -> None:
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {COLOR_BG};
                color: {COLOR_TEXT};
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
            }}
            QGroupBox {{
                border: 1px solid {COLOR_BORDER};
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 12px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                color: {COLOR_TEXT_DIM};
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
            }}
            QLineEdit {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                padding: 6px 8px;
                color: {COLOR_TEXT};
            }}
            QLineEdit:focus {{
                border: 1px solid {COLOR_ACCENT};
            }}
            QPushButton {{
                border-radius: 5px;
                padding: 7px 18px;
                font-weight: bold;
            }}
            QRadioButton {{
                spacing: 8px;
                padding: 4px 0;
            }}
            QTextEdit {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 4px;
                color: {COLOR_TEXT};
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }}
            QStatusBar {{
                background: {COLOR_SURFACE};
                color: {COLOR_TEXT_DIM};
                font-size: 11px;
            }}
        """)

    # ── UI 조립 ──────────────────────────────────────────────
    def _build_ui(self) -> None:
        # 설정·제어·즉시실행·로그를 모두 세로로 쌓으므로 작은 화면에서도 잘리지 않게 스크롤에 담는다
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # 계좌/API 설정
        api_box = QGroupBox("계좌 / API 설정")
        api_form = QFormLayout(api_box)
        api_form.setSpacing(8)
        api_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._app_key = QLineEdit()
        self._app_key.setPlaceholderText("키움 앱키")
        self._app_secret = QLineEdit()
        self._app_secret.setPlaceholderText("키움 시크릿")
        self._app_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._account = QLineEdit()
        self._account.setPlaceholderText("계좌번호 (예: 1234567890)")

        api_form.addRow("App Key", self._app_key)
        api_form.addRow("App Secret", self._app_secret)
        api_form.addRow("계좌번호", self._account)
        root.addWidget(api_box)

        # 매매 모드
        mode_box = QGroupBox("매매 모드")
        mode_layout = QHBoxLayout(mode_box)
        mode_layout.setSpacing(24)

        self._radio_paper = QRadioButton("모의투자 (Paper)")
        self._radio_live = QRadioButton("실전 (Live)")
        self._radio_paper.setChecked(True)

        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._radio_paper, 0)
        self._mode_group.addButton(self._radio_live, 1)
        self._radio_live.toggled.connect(self._on_live_toggled)

        mode_layout.addWidget(self._radio_paper)
        mode_layout.addWidget(self._radio_live)
        mode_layout.addStretch()

        # 현재 선택된 모드를 글자로도 한 번 더 보여준다
        self._mode_badge = QLabel()
        mode_layout.addWidget(self._mode_badge)
        root.addWidget(mode_box)

        self._mode_hint = QLabel()
        self._mode_hint.setWordWrap(True)
        self._mode_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        root.addWidget(self._mode_hint)
        self._update_mode_hint()

        # 리스크 관리 (익절 / 손절)
        risk_box = QGroupBox("리스크 관리 (익절 / 손절)")
        risk_form = QFormLayout(risk_box)
        risk_form.setSpacing(8)
        risk_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._take_profit = QLineEdit()
        self._take_profit.setPlaceholderText("예: 2 (매수가 대비 +2%)")
        self._take_profit.setValidator(QDoubleValidator(0.0, 100.0, 2))
        self._stop_loss = QLineEdit()
        self._stop_loss.setPlaceholderText("예: 2 (매수가 대비 -2%)")
        self._stop_loss.setValidator(QDoubleValidator(0.0, 100.0, 2))

        risk_form.addRow("익절 (%)", self._take_profit)
        risk_form.addRow("손절 (%)", self._stop_loss)
        root.addWidget(risk_box)

        # 이메일 알림 — 발송/수신 주소만 UI에서 관리하고, SMTP 서버·포트·비밀번호는 .env로만 다룬다
        email_box = QGroupBox("이메일 알림")
        email_form = QFormLayout(email_box)
        email_form.setSpacing(8)
        email_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._email_from = QLineEdit()
        self._email_from.setPlaceholderText("보내는 메일 주소")
        self._email_to = QLineEdit()
        self._email_to.setPlaceholderText("받는 메일 주소")

        email_form.addRow("발송 메일", self._email_from)
        email_form.addRow("수신 메일", self._email_to)
        root.addWidget(email_box)

        # 설정 저장 버튼
        self._btn_save = QPushButton("설정 저장")
        self._btn_save.setStyleSheet(
            f"background: {COLOR_SURFACE}; color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER};"
        )
        self._btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_save.clicked.connect(self._save_settings)
        root.addWidget(self._btn_save)

        root.addWidget(_separator())

        # 엔진 제어
        ctrl_box = QGroupBox("엔진 제어")
        ctrl_layout = QVBoxLayout(ctrl_box)

        status_row = QHBoxLayout()
        status_lbl = QLabel("상태")
        status_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        self._status_dot = QLabel("●  정지")
        self._status_dot.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-weight: bold;")
        status_row.addWidget(status_lbl)
        status_row.addWidget(self._status_dot)
        status_row.addStretch()
        ctrl_layout.addLayout(status_row)

        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("▶  시작")
        self._btn_start.setStyleSheet(
            f"background: {COLOR_ACCENT}; color: white; border: none;"
        )
        self._btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_start.clicked.connect(self._start_engine)

        self._btn_stop = QPushButton("■  정지")
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet(
            f"background: {COLOR_SURFACE}; color: {COLOR_TEXT_DIM}; border: 1px solid {COLOR_BORDER};"
        )
        self._btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_stop.clicked.connect(self._stop_engine)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        ctrl_layout.addLayout(btn_row)
        root.addWidget(ctrl_box)

        # 즉시 실행 — 스케줄 시각을 기다리지 않고 하루 흐름의 각 단계를 바로 돌린다
        run_box = QGroupBox("즉시 실행 (시간 무시)")
        run_layout = QVBoxLayout(run_box)
        run_layout.setSpacing(8)

        run_hint = QLabel(
            "스케줄(08:45 / 09:00 / 15:20 / 15:30)과 무관하게 지금 바로 실행합니다. "
            "엔진이 실행 중일 때만 동작하며, 장 시간 외에는 주문이 거부될 수 있습니다.\n"
            "일괄 수행은 ①② (추천→매수)만 돌립니다. 매수 후에는 설정된 익절/손절 라인이 자동 감시되며, "
            "청산(15:20)·리포트(15:30)는 스케줄에 맡깁니다."
        )
        run_hint.setWordWrap(True)
        run_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        run_layout.addWidget(run_hint)

        grid = QGridLayout()
        grid.setSpacing(8)
        self._action_buttons: dict[str, QPushButton] = {}
        self._action_accent: dict[str, bool] = {}
        for index, action in enumerate(("recommend", "buy", "sell_all", "report")):
            btn = self._make_action_button(action)
            grid.addWidget(btn, index // 2, index % 2)
        run_layout.addLayout(grid)

        btn_full = self._make_action_button("full", accent=True)
        run_layout.addWidget(btn_full)
        root.addWidget(run_box)

        # 하단 — 실행 로그(좌)와 보유 종목(우)을 반반으로 나눈다
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)

        log_box = QGroupBox("실행 로그")
        log_layout = QVBoxLayout(log_box)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(140)
        log_layout.addWidget(self._log_view)
        bottom_row.addWidget(log_box, 1)

        bottom_row.addWidget(self._build_holdings_box(), 1)
        root.addLayout(bottom_row)

        # 사유가 길어 좌우로 나누지 않고 전체 폭을 쓴다 (매도 불가 건이 있을 때만 보인다)
        root.addWidget(self._build_unsellable_box())

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(scroll)

        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("준비")

    # ── 로깅 연결 ────────────────────────────────────────────
    def _setup_logging(self) -> None:
        self._log_signal.connect(self._append_log)
        handler = _QtLogHandler(self._log_signal)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    def _append_log(self, level: int, msg: str) -> None:
        if level >= logging.ERROR:
            color = COLOR_DANGER
        elif level >= logging.WARNING:
            color = COLOR_WARNING
        else:
            color = COLOR_TEXT
        self._log_view.append(f'<span style="color:{color};">{msg}</span>')
        self._log_view.verticalScrollBar().setValue(
            self._log_view.verticalScrollBar().maximum()
        )

    # ── 보유 종목 ────────────────────────────────────────────
    def _build_holdings_box(self) -> QGroupBox:
        box = QGroupBox("보유 종목")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self._holdings_hint = QLabel()
        self._holdings_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self._holdings_hint)

        table = QTableWidget(0, len(HOLDINGS_COLUMNS))
        table.setHorizontalHeaderLabels(HOLDINGS_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setMinimumHeight(140)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(HOLDINGS_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        table.setStyleSheet(_table_style())
        self._holdings_view = table
        layout.addWidget(table)

        # 표 갱신은 엔진 캐시만 읽으므로 API 호출이 발생하지 않는다 (engine.position_snapshot)
        self._holdings_timer = QTimer(self)
        self._holdings_timer.setInterval(HOLDINGS_REFRESH_MS)
        self._holdings_timer.timeout.connect(self._refresh_holdings)

        self._refresh_holdings()
        return box

    def _refresh_holdings(self) -> None:
        """매수된 종목만 표에 남긴다 — 매도된 건은 엔진 잔고에서 빠지며 함께 사라진다."""
        thread = self._engine_thread
        rows = sorted(thread.position_snapshot(), key=lambda p: p.ticker) if thread else []

        table = self._holdings_view
        table.setRowCount(len(rows))
        for row, held in enumerate(rows):
            color = COLOR_PROFIT if held.pnl > 0 else COLOR_LOSS if held.pnl < 0 else COLOR_TEXT
            cells = (
                (held.label, Qt.AlignmentFlag.AlignLeft, COLOR_TEXT),
                (f"{held.quantity:,}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT),
                (f"{held.avg_price:,.0f}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT),
                (f"{held.current_price:,.0f}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT),
                (
                    f"{held.pnl:+,.0f} ({held.pnl_percent:+.2f}%)",
                    Qt.AlignmentFlag.AlignRight,
                    color,
                ),
            )
            for col, (text, align, cell_color) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
                item.setForeground(QColor(cell_color))
                table.setItem(row, col, item)

        self._holdings_hint.setText(self._holdings_summary(rows))
        # 표를 만드는 도중에 불리는 첫 호출에서는 매도 불가 표가 아직 없다
        if self._unsellable_box is not None:
            self._refresh_unsellable()

    def _holdings_summary(self, rows: list) -> str:
        if self._engine_thread is None:
            return "엔진을 시작하면 매수된 종목이 표시됩니다."
        if not rows:
            return "보유 종목이 없습니다."

        total_pnl = sum(held.pnl for held in rows)
        cost = sum(held.avg_price * held.quantity for held in rows)
        percent = f" ({total_pnl / cost * 100:+.2f}%)" if cost > 0 else ""
        return f"{len(rows)}종목 · 평가손익 {total_pnl:+,.0f}원{percent}"

    # ── 매도 불가 ────────────────────────────────────────────
    def _build_unsellable_box(self) -> QGroupBox:
        """오늘 매도하지 못한 종목과 사유. 해당 건이 없으면 박스 자체를 숨긴다."""
        box = QGroupBox("매도 불가")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self._unsellable_hint = QLabel()
        self._unsellable_hint.setStyleSheet(f"color: {COLOR_WARNING}; font-size: 11px;")
        self._unsellable_hint.setWordWrap(True)
        layout.addWidget(self._unsellable_hint)

        table = QTableWidget(0, len(UNSELLABLE_COLUMNS))
        table.setHorizontalHeaderLabels(UNSELLABLE_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.setMinimumHeight(90)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # 사유가 가장 길다
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.setStyleSheet(_table_style())
        self._unsellable_view = table
        layout.addWidget(table)

        # 갱신은 _refresh_holdings가 이어서 호출한다 (같은 타이머·같은 캐시)
        self._unsellable_box = box
        self._refresh_unsellable()
        return box

    def _refresh_unsellable(self) -> None:
        """매도하지 못한 종목을 사유와 함께 표에 채운다 (engine.unsellable_snapshot)."""
        thread = self._engine_thread
        rows = thread.unsellable_snapshot() if thread else []

        # 평소에는 숨겨두고 매도 불가 건이 생겼을 때만 드러낸다
        self._unsellable_box.setVisible(bool(rows))
        if not rows:
            return

        table = self._unsellable_view
        table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            # 제외된 건은 계좌에 남는데 보유 종목 표에서는 사라지므로 더 강한 색으로 표기한다
            reason_color = COLOR_DANGER if item.excluded else COLOR_WARNING
            cells = (
                (item.label, Qt.AlignmentFlag.AlignLeft, COLOR_TEXT),
                (item.reason, Qt.AlignmentFlag.AlignLeft, reason_color),
                (f"{item.at:%H:%M:%S}", Qt.AlignmentFlag.AlignRight, COLOR_TEXT_DIM),
            )
            for col, (text, align, cell_color) in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
                cell.setForeground(QColor(cell_color))
                table.setItem(row, col, cell)

        self._unsellable_hint.setText(self._unsellable_summary(rows))

    def _unsellable_summary(self, rows: list) -> str:
        excluded = sum(1 for item in rows if item.excluded)
        if not excluded:
            return f"오늘 매도하지 못한 종목 {len(rows)}건 — 다음 청산 시각에 다시 시도합니다."
        return (
            f"오늘 매도하지 못한 종목 {len(rows)}건 (보유 목록 제외 {excluded}건) — "
            "제외된 종목은 계좌에 남아 있으며 자동 청산되지 않습니다. 직접 확인하세요."
        )

    # ── 설정 저장/불러오기 ───────────────────────────────────
    def _load_settings(self) -> None:
        env = load_env(ENV_PATH)
        self._app_key.setText(env.get("KIWOOM_APP_KEY", ""))
        self._app_secret.setText(env.get("KIWOOM_APP_SECRET", ""))
        self._account.setText(env.get("KIWOOM_ACCOUNT", ""))
        self._email_from.setText(env.get("EMAIL_FROM", ""))
        self._email_to.setText(env.get("EMAIL_TO", ""))
        self._take_profit.setText(env.get("TAKE_PROFIT_PERCENT", "2"))
        self._stop_loss.setText(env.get("STOP_LOSS_PERCENT", "2"))
        mode = env.get("TRADE_MODE", "paper")
        if mode == "live":
            self._radio_live.setChecked(True)
        else:
            self._radio_paper.setChecked(True)
        self._update_mode_hint()
        logger.info("설정 불러오기 완료 (mode=%s)", mode)

    def _save_settings(self) -> None:
        mode = "live" if self._radio_live.isChecked() else "paper"
        values = {
            "TRADE_MODE": mode,
            "KIWOOM_APP_KEY": self._app_key.text().strip(),
            "KIWOOM_APP_SECRET": self._app_secret.text().strip(),
            "KIWOOM_ACCOUNT": self._account.text().strip(),
            # SMTP 서버/포트/비밀번호는 UI에서 다루지 않으므로 저장 대상에서 제외한다
            # (save_env는 전달된 키만 갱신하므로 .env의 기존 값은 그대로 보존된다)
            "SMTP_USER": self._email_from.text().strip(),  # 로그인 계정 = 발송 주소
            "EMAIL_FROM": self._email_from.text().strip(),
            "EMAIL_TO": self._email_to.text().strip(),
            "TAKE_PROFIT_PERCENT": self._take_profit.text().strip() or "2",
            "STOP_LOSS_PERCENT": self._stop_loss.text().strip() or "2",
        }
        if mode == "live":
            values["LIVE_TRADE_CONFIRMED"] = "YES_I_UNDERSTAND"
        else:
            values["LIVE_TRADE_CONFIRMED"] = ""

        save_env(ENV_PATH, values)
        logger.info("설정 저장 완료 (mode=%s)", mode)
        self._statusbar.showMessage("설정이 저장되었습니다.", 3000)

    def _update_mode_hint(self) -> None:
        is_live = self._radio_live.isChecked()

        # 선택된 쪽만 색이 켜지고 굵어지며, 인디케이터도 채워진다
        self._radio_paper.setStyleSheet(_radio_style(COLOR_SUCCESS, not is_live))
        self._radio_live.setStyleSheet(_radio_style(COLOR_DANGER, is_live))

        badge_color = COLOR_DANGER if is_live else COLOR_SUCCESS
        badge_text = "✔ 실전 선택됨" if is_live else "✔ 모의투자 선택됨"
        self._mode_badge.setText(badge_text)
        self._mode_badge.setStyleSheet(
            f"color: {badge_color}; font-weight: bold; font-size: 12px;"
        )

        if is_live:
            self._mode_hint.setText("⚠️  실전 계좌입니다. 실제 자금으로 주문이 집행됩니다.")
            self._mode_hint.setStyleSheet(f"color: {COLOR_WARNING}; font-size: 11px;")
        else:
            self._mode_hint.setText("모의투자 서버(mockapi.kiwoom.com)로 연결됩니다. 실제 자금은 쓰이지 않습니다.")
            self._mode_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

    # ── 매매 모드 전환 ───────────────────────────────────────
    def _on_live_toggled(self, checked: bool) -> None:
        if checked:
            logger.warning("실전 계좌 모드로 전환되었습니다.")
        self._update_mode_hint()

    # ── 엔진 시작/정지 ───────────────────────────────────────
    def _start_engine(self) -> None:
        if self._engine_thread is not None:
            logger.warning("엔진이 이미 실행 중입니다.")
            return

        # 화면의 현재 입력값을 그대로 반영해 시작한다
        self._save_settings()
        load_dotenv(ENV_PATH, override=True)

        try:
            settings = Settings()
            settings.validate()
        except Exception as e:
            logger.error("설정이 올바르지 않아 엔진을 시작할 수 없습니다: %s", e)
            self._statusbar.showMessage("설정 오류로 시작하지 못했습니다.", 5000)
            return

        logger.info("엔진 시작 요청 (mode=%s)", settings.mode)
        self._btn_start.setEnabled(False)

        thread = EngineThread(settings, parent=self)
        thread.started_ok.connect(lambda: self._set_engine_running(True))
        thread.failed.connect(self._on_engine_failed)
        thread.finished_run.connect(self._on_engine_finished)
        thread.action_started.connect(self._on_action_started)
        thread.action_finished.connect(self._on_action_finished)
        self._engine_thread = thread
        thread.start()

    def _stop_engine(self) -> None:
        if self._engine_thread is None:
            return
        logger.info("엔진 정지 요청")
        self._btn_stop.setEnabled(False)
        self._set_actions_enabled(False)
        if self._engine_thread.action_busy:
            # 주문 시퀀스를 중간에 끊지 않으려고 완료를 기다리므로 잠시 멈춘 것처럼 보인다
            self._statusbar.showMessage("즉시 실행이 끝날 때까지 기다린 뒤 정지합니다...")
        else:
            self._statusbar.showMessage("엔진을 정지하는 중...")
        self._engine_thread.stop()

    # ── 즉시 실행 ────────────────────────────────────────────
    def _make_action_button(self, action: str, accent: bool = False) -> QPushButton:
        btn = QPushButton(MANUAL_ACTIONS[action])
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _checked=False, a=action: self._run_action(a))
        self._action_buttons[action] = btn
        self._action_accent[action] = accent
        self._style_action_button(action, enabled=False)  # 엔진이 돌기 전에는 잠가둔다
        return btn

    def _style_action_button(self, action: str, enabled: bool) -> None:
        btn = self._action_buttons[action]
        btn.setEnabled(enabled)
        if not enabled:
            style = f"background: {COLOR_SURFACE}; color: {COLOR_TEXT_DIM}; border: 1px solid {COLOR_BORDER};"
        elif self._action_accent[action]:
            style = f"background: {COLOR_ACCENT}; color: white; border: none;"
        else:
            style = f"background: {COLOR_SURFACE}; color: {COLOR_TEXT}; border: 1px solid {COLOR_ACCENT};"
        btn.setStyleSheet(style)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for action in self._action_buttons:
            self._style_action_button(action, enabled)

    def _run_action(self, action: str) -> None:
        thread = self._engine_thread
        if thread is None:
            logger.warning("엔진이 정지 상태입니다. 먼저 시작한 뒤 즉시 실행하세요.")
            self._statusbar.showMessage("엔진을 먼저 시작하세요.", 4000)
            return

        # 주문이 나가는 액션은 실수 클릭을 막기 위해 한 번 확인한다
        if action in ORDER_ACTIONS and not self._confirm_action(action):
            return

        if thread.run_action(action):
            self._set_actions_enabled(False)
            self._statusbar.showMessage(f"즉시 실행 요청: {MANUAL_ACTIONS[action]}")

    def _confirm_action(self, action: str) -> bool:
        detail = {
            "buy": "추천 종목을 시장가로 매수합니다.",
            "sell_all": "보유 중인 모든 포지션을 시장가로 청산합니다.",
            "full": (
                "LLM 추천 + 메일 → 시장가 매수를 순서대로 실행합니다.\n"
                f"매수 후에는 익절 +{self._take_profit.text().strip() or '2'}% / "
                f"손절 -{self._stop_loss.text().strip() or '2'}% 라인이 자동 감시됩니다 "
                "(엔진이 켜져 있는 동안만).\n"
                "청산(15:20)과 최종 리포트(15:30)는 지금 실행하지 않고 예정 시각에 맡깁니다."
            ),
        }[action]
        is_live = self._radio_live.isChecked()
        head = "⚠️  실전 계좌입니다. 실제 자금으로 주문이 집행됩니다.\n\n" if is_live else ""

        box = QMessageBox(self)
        box.setWindowTitle("즉시 실행 확인")
        box.setIcon(QMessageBox.Icon.Warning if is_live else QMessageBox.Icon.Question)
        box.setText(f"{head}{MANUAL_ACTIONS[action]}\n\n{detail}\n\n지금 실행할까요?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setStyleSheet(f"""
            QLabel {{ color: {COLOR_TEXT}; }}
            QPushButton {{
                background: {COLOR_SURFACE}; color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER}; padding: 6px 18px; min-width: 64px;
            }}
        """)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _on_action_started(self, action: str) -> None:
        self._statusbar.showMessage(f"즉시 실행 중: {MANUAL_ACTIONS.get(action, action)} …")

    def _on_action_finished(self, action: str, ok: bool, message: str) -> None:
        label = MANUAL_ACTIONS.get(action, action)
        if ok:
            logger.info("[즉시 실행] %s — 종료", label)
            self._statusbar.showMessage(f"즉시 실행 완료: {label}", 5000)
        else:
            self._statusbar.showMessage(f"즉시 실행 실패: {label} — {message}", 8000)
        # 엔진이 계속 돌고 있다면 버튼을 다시 열어준다
        self._set_actions_enabled(self._engine_thread is not None)

    def _on_engine_failed(self, message: str) -> None:
        logger.error("엔진 오류: %s", message)
        self._statusbar.showMessage(f"엔진 오류: {message}", 8000)

    def _on_engine_finished(self) -> None:
        self._engine_thread = None
        self._set_engine_running(False)
        self._statusbar.showMessage("엔진이 정지되었습니다.", 3000)

    def closeEvent(self, event) -> None:
        # 창을 닫으면 엔진도 함께 정리한다 (UI가 유일한 제어 지점이므로)
        if self._engine_thread is not None:
            # 익절/손절은 이 프로그램이 떠 있는 동안에만 동작한다 (키움 REST 스탑오더 미지원).
            # 보유 종목을 남긴 채 닫으면 손절이 사라지므로 반드시 확인을 받는다.
            held = self._engine_thread.open_tickers()
            if held and not self._confirm_close_with_positions(held):
                event.ignore()
                return
            logger.info("창 종료 — 엔진을 정지합니다.")
            self._engine_thread.stop()
        super().closeEvent(event)

    def _confirm_close_with_positions(self, held: list) -> bool:
        logger.warning("보유 종목이 있는 상태에서 창 종료를 시도했습니다: %s", held)
        box = QMessageBox(self)
        box.setWindowTitle("보유 종목이 있습니다")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"아직 청산되지 않은 보유 종목이 {len(held)}개 있습니다.\n{', '.join(held)}\n\n"
            "익절/손절 감시는 이 프로그램이 실행 중일 때만 동작합니다.\n"
            "지금 닫으면 손절이 걸리지 않고 장 마감 강제청산(15:20)도 실행되지 않아\n"
            "포지션이 다음 영업일로 넘어갑니다.\n\n"
            "그래도 종료할까요?"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setStyleSheet(f"""
            QLabel {{ color: {COLOR_TEXT}; }}
            QPushButton {{
                background: {COLOR_SURFACE}; color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER}; padding: 6px 18px; min-width: 64px;
            }}
        """)
        confirmed = box.exec() == QMessageBox.StandardButton.Yes
        if not confirmed:
            self._statusbar.showMessage("종료를 취소했습니다. 청산 후 종료하세요.", 5000)
        return confirmed

    def _set_engine_running(self, running: bool) -> None:
        self._set_actions_enabled(running)
        # 정지 상태에서는 읽을 잔고가 없으므로 표 갱신도 멈춘다
        if running:
            self._holdings_timer.start()
        else:
            self._holdings_timer.stop()
        self._refresh_holdings()

        if running:
            self._status_dot.setText("●  실행 중")
            self._status_dot.setStyleSheet(f"color: {COLOR_SUCCESS}; font-weight: bold;")
            self._btn_start.setEnabled(False)
            self._btn_start.setStyleSheet(
                f"background: {COLOR_SURFACE}; color: {COLOR_TEXT_DIM}; border: 1px solid {COLOR_BORDER};"
            )
            self._btn_stop.setEnabled(True)
            self._btn_stop.setStyleSheet(
                f"background: {COLOR_DANGER}; color: white; border: none;"
            )
        else:
            self._status_dot.setText("●  정지")
            self._status_dot.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-weight: bold;")
            self._btn_start.setEnabled(True)
            self._btn_start.setStyleSheet(
                f"background: {COLOR_ACCENT}; color: white; border: none;"
            )
            self._btn_stop.setEnabled(False)
            self._btn_stop.setStyleSheet(
                f"background: {COLOR_SURFACE}; color: {COLOR_TEXT_DIM}; border: 1px solid {COLOR_BORDER};"
            )
