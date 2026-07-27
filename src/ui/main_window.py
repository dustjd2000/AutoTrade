from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QDoubleValidator, QFont, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

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


# ── 실전 계좌 확인 다이얼로그 ────────────────────────────────
class LiveConfirmDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("실전 매매 시작 확인")
        self.setFixedWidth(420)
        self.setStyleSheet(f"background-color: {COLOR_BG}; color: {COLOR_TEXT};")

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        warning = QLabel(
            "⚠️  실전 계좌로 자동매매를 시작합니다.\n\n"
            "모의투자를 지원하지 않으므로 실제 자금으로 주문이 집행됩니다.\n"
            "소액으로 시작하고, 초기에는 장중 동작을 직접 확인하세요.\n\n"
            "계속하려면 아래에 정확히 입력하세요:\n"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {COLOR_WARNING}; font-size: 13px;")
        layout.addWidget(warning)

        confirm_label = QLabel("YES_I_UNDERSTAND")
        confirm_label.setStyleSheet(f"color: {COLOR_DANGER}; font-weight: bold; font-size: 14px;")
        confirm_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(confirm_label)

        self.input = QLineEdit()
        self.input.setPlaceholderText("위 텍스트를 그대로 입력...")
        self.input.setStyleSheet(
            f"background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
            f"color: {COLOR_TEXT}; padding: 6px; border-radius: 4px;"
        )
        layout.addWidget(self.input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        buttons.setStyleSheet(f"color: {COLOR_TEXT};")
        layout.addWidget(buttons)

    def _check(self) -> None:
        if self.input.text().strip() == "YES_I_UNDERSTAND":
            self.accept()
        else:
            QMessageBox.warning(self, "입력 오류", "텍스트가 일치하지 않습니다.")


# ── 메인 윈도우 ──────────────────────────────────────────────
class MainWindow(QMainWindow):
    _log_signal = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoTrade")
        self.setMinimumSize(560, 780)
        self._setup_style()
        self._build_ui()
        self._setup_logging()
        self._load_settings()

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
                spacing: 6px;
            }}
            QRadioButton::indicator {{
                width: 14px;
                height: 14px;
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
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
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
        self._radio_paper.setStyleSheet(f"color: {COLOR_SUCCESS};")
        self._radio_live.setStyleSheet(f"color: {COLOR_DANGER};")

        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._radio_paper, 0)
        self._mode_group.addButton(self._radio_live, 1)
        self._radio_live.toggled.connect(self._on_live_toggled)

        mode_layout.addWidget(self._radio_paper)
        mode_layout.addWidget(self._radio_live)
        mode_layout.addStretch()
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

        # 로그 뷰
        log_box = QGroupBox("실행 로그")
        log_layout = QVBoxLayout(log_box)
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(180)
        log_layout.addWidget(self._log_view)
        root.addWidget(log_box)

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
        # 실전이 저장돼 있어도 확인 다이얼로그 없이 조용히 켜지지 않도록 모의로 시작한다
        self._radio_paper.setChecked(True)
        self._update_mode_hint()
        logger.info("설정 불러오기 완료 (저장된 mode=%s, 안전을 위해 모의투자로 시작)", mode)

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
        if self._radio_live.isChecked():
            self._mode_hint.setText("⚠️  실전 계좌입니다. 실제 자금으로 주문이 집행됩니다.")
            self._mode_hint.setStyleSheet(f"color: {COLOR_WARNING}; font-size: 11px;")
        else:
            self._mode_hint.setText("모의투자 서버(mockapi.kiwoom.com)로 연결됩니다. 실제 자금은 쓰이지 않습니다.")
            self._mode_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

    # ── 실전 전환 확인 ───────────────────────────────────────
    def _on_live_toggled(self, checked: bool) -> None:
        if not checked:
            self._update_mode_hint()
            return
        dlg = LiveConfirmDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._radio_paper.setChecked(True)
        else:
            logger.warning("실전 계좌 모드로 전환되었습니다.")
        self._update_mode_hint()

    # ── 엔진 시작/정지 ───────────────────────────────────────
    def _start_engine(self) -> None:
        mode = "live" if self._radio_live.isChecked() else "paper"
        # 실전은 실제 자금이 움직이므로 시작할 때마다 다시 확인받는다
        if mode == "live" and LiveConfirmDialog(self).exec() != QDialog.DialogCode.Accepted:
            logger.info("실전 매매 확인이 취소되어 엔진을 시작하지 않습니다.")
            return

        logger.info("엔진 시작 요청 (mode=%s)", mode)
        self._set_engine_running(True)
        # TODO: 실제 TradingEngine을 별도 QThread에서 실행

    def _stop_engine(self) -> None:
        logger.info("엔진 정지 요청")
        self._set_engine_running(False)
        # TODO: TradingEngine.stop() 호출

    def _set_engine_running(self, running: bool) -> None:
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
