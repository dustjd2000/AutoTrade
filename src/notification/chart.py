"""리포트 이메일의 '이번 달 누적' 꺾은선 그래프.

Gmail은 메일 본문의 인라인 <svg>를 통째로 지운다. 그래서 그래프는 PNG로 그려
CID 인라인 첨부로 실어 보낸다 (확정 2026-08-24).

그래프를 못 그려도 리포트 메일 자체는 나가야 하므로, 실패하면 None을 돌려주고
호출부가 숫자만 담은 기존 본문을 보내게 한다.
"""

import io
import logging
from typing import List, Optional, Sequence

from src.logger.trade_store import DailyPoint

logger = logging.getLogger(__name__)

# 이익은 빨강, 손실은 파랑 (templates.py와 같은 국내 증권 관례)
COLOR_PROFIT = "#d32f2f"
COLOR_LOSS = "#1565c0"
COLOR_FLAT = "#555555"
COLOR_INK = "#222222"
COLOR_MUTED = "#777777"
COLOR_GRID = "#eeeeee"
COLOR_AXIS = "#cccccc"

# 한글이 두부(□)로 깨지지 않게 — 설치된 것 중 앞에서부터 하나를 쓴다
FONT_CANDIDATES = ("Malgun Gothic", "NanumGothic", "AppleGothic", "DejaVu Sans")

WIDTH_PX = 640
_FIGSIZE = (6.4, 2.6)
_DPI = 110
MAX_X_LABELS = 10

# 눈금 문구 — 월 그래프는 점 하나가 하루, 연 그래프는 한 달이다
DAY_LABEL = "%m/%d"
MONTH_LABEL = "%m월"


def render_monthly_cumulative(points: Sequence[DailyPoint]) -> Optional[bytes]:
    """날짜별 누적 순손익을 꺾은선 PNG로 그린다. 못 그리면 None.

    **점이 하나여도 그린다 (2026-09-01)**. 매달 첫 거래일에는 이번 달 거래일이 하루뿐이라
    그래프가 통째로 빠졌다 — 꺾은선은 아니지만 그날의 위치를 0선 대비로 보여주는 값은 있다.
    """
    return _render(points, DAY_LABEL)


def render_yearly_cumulative(points: Sequence[DailyPoint]) -> Optional[bytes]:
    """달별 누적 순손익을 꺾은선 PNG로 그린다 (2026-09-01). 못 그리면 None.

    입력은 `TradeStore.yearly_cumulative_series` — 점 하나가 한 달이다. 그리는 방식은
    월 그래프와 같고 x축 눈금만 날짜에서 달로 바뀐다.
    """
    return _render(points, MONTH_LABEL)


def _render(points: Sequence[DailyPoint], label_format: str) -> Optional[bytes]:
    if not points:
        return None  # 그 기간에 거래가 아직 없다
    try:
        fig = _figure(points, label_format)
    except Exception:
        logger.warning("월 누적 그래프를 그리지 못했습니다 — 숫자만 보냅니다.", exc_info=True)
        return None

    try:
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=_DPI, bbox_inches="tight", facecolor="white")
        return buffer.getvalue()
    except Exception:
        # 여기서 예외를 올리면 호출부(daily_workflow)가 메일 본문을 만들기도 전에 죽어
        # 리포트가 통째로 빠진다 — 그래프 하나 때문에 잃을 것이 아니다
        logger.warning("누적 그래프를 PNG로 굽지 못했습니다 — 숫자만 보냅니다.", exc_info=True)
        return None
    finally:
        _close(fig)


def _figure(points: Sequence[DailyPoint], label_format: str = DAY_LABEL):
    import matplotlib

    matplotlib.use("Agg")  # 헤드리스 렌더링 — GUI 백엔드를 잡으면 UI 스레드와 충돌한다
    from matplotlib import pyplot as plt
    from matplotlib.font_manager import findfont, FontProperties

    plt.rcParams["font.family"] = _pick_font(findfont, FontProperties)
    plt.rcParams["axes.unicode_minus"] = False  # 마이너스 기호가 깨지는 것을 막는다

    values = [p.cumulative for p in points]
    x = range(len(values))
    tone = COLOR_PROFIT if values[-1] > 0 else COLOR_LOSS if values[-1] < 0 else COLOR_FLAT

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.plot(x, values, color=tone, linewidth=2, marker="o", markersize=4.5, zorder=3)
    ax.fill_between(x, 0, values, color=tone, alpha=0.08, zorder=1)
    ax.plot([x[-1]], [values[-1]], color=tone, marker="o", markersize=7, zorder=4)
    ax.axhline(0, color=COLOR_AXIS, linewidth=1, zorder=2)

    _annotate_last(ax, len(values) - 1, values[-1])
    _style_axes(ax, points, values, label_format)

    fig.tight_layout()
    return fig


def _pick_font(findfont, FontProperties) -> str:
    """설치돼 있는 첫 후보를 고른다 — 없는 이름을 지정하면 경고와 함께 두부가 찍힌다.

    fallback_to_default=False라야 없는 폰트에서 예외가 난다. 기본값(True)은 조용히
    DejaVu Sans를 돌려줘서 후보를 골라내지 못한다.
    """
    for name in FONT_CANDIDATES:
        try:
            findfont(FontProperties(family=name), fallback_to_default=False)
            return name
        except Exception:
            continue
    return FONT_CANDIDATES[-1]


def _annotate_last(ax, index: int, value: float) -> None:
    """마지막 점에만 값을 적는다 — 모든 점에 숫자를 붙이면 선이 안 보인다."""
    ax.annotate(
        f"{value:+,.0f}원",
        xy=(index, value),
        xytext=(10, -4),
        textcoords="offset points",
        ha="left",
        fontsize=10,
        fontweight="bold",
        color=COLOR_INK,
    )


def _style_axes(
    ax, points: Sequence[DailyPoint], values: List[float], label_format: str = DAY_LABEL
) -> None:
    ax.set_title("누적 순손익(원)", fontsize=10, color=COLOR_MUTED, loc="left", pad=8)
    ax.grid(axis="y", color=COLOR_GRID, linewidth=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(COLOR_AXIS)

    # 거래일이 많으면 눈금을 솎아낸다 — 올림이라야 MAX_X_LABELS를 넘지 않는다
    step = max(1, -(-len(points) // MAX_X_LABELS))
    ticks = list(range(0, len(points), step))
    if ticks[-1] != len(points) - 1:
        ticks.append(len(points) - 1)
    ax.set_xticks(ticks)
    ax.set_xticklabels([format(points[i].day, label_format) for i in ticks])
    ax.tick_params(axis="both", colors=COLOR_MUTED, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")

    margin = max(abs(min(values)), abs(max(values)), 1) * 0.25
    ax.set_ylim(min(min(values), 0) - margin, max(max(values), 0) + margin)
    # 오른쪽 여백은 마지막 점 옆에 붙는 값 라벨 자리다
    if len(points) == 1:
        # 같은 식을 쓰면 홀로 찍힌 점이 왼쪽 끝으로 몰린다 (여백이 폭의 8할)
        ax.set_xlim(-0.6, 1.2)
    else:
        ax.set_xlim(-0.4, len(points) + 0.8)


def _close(fig) -> None:
    from matplotlib import pyplot as plt

    plt.close(fig)  # 매일 도는 프로세스라 닫지 않으면 figure가 쌓인다
