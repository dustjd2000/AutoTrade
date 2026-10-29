"""2호 전략 규칙 자동 평가용 1분봉 저장소 (스펙 2026-10-29 5.1).

종목별 JSON 파일 하나에 {YYYYMMDD: [[HHMM, 종가], ...]}로 쌓는다. 같은 날짜는 덮어쓴다 — 장중에
받은 반쪽 날도 다음 수집이 통째로 바꾼다. `data/`는 gitignore 대상이다. API를 모르는 파일 입출력만 맡는다.
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

Bar = Tuple[str, float]           # (HHMM, 종가)
DayBars = Dict[str, List[Bar]]    # YYYYMMDD → 시각순 봉

DEFAULT_MINUTE_DIR = Path("data") / "minute"


class MinuteStore:
    def __init__(self, root: Path = DEFAULT_MINUTE_DIR):
        self.root = Path(root)

    def _path(self, ticker: str) -> Path:
        return self.root / f"{ticker}.json"

    def load(self, ticker: str) -> DayBars:
        """저장된 봉. 파일이 없거나 깨졌으면 빈 dict — 다음 수집이 처음부터 다시 채운다."""
        try:
            raw = json.loads(self._path(ticker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {day: [(str(t), float(p)) for t, p in bars] for day, bars in raw.items()}

    def last_day(self, ticker: str) -> Optional[str]:
        days = self.load(ticker)
        return max(days) if days else None

    def merge(self, ticker: str, bars: DayBars) -> int:
        """`bars`의 날짜를 덮어써 저장하고 저장한 날짜 수를 돌려준다. 임시 파일 → os.replace로 원자적으로 쓴다."""
        current = self.load(ticker)
        current.update(bars)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(ticker)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(current), encoding="utf-8")
        os.replace(temp, path)
        return len(bars)
