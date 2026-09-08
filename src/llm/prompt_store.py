import logging
import os
import re
from pathlib import Path
from typing import Dict

from src.llm.recommender import (
    DEFAULT_PROMPT_SECTIONS,
    PROMPT_SECTION_ORDER,
    PROMPT_TEMPLATE_VERSION,
)

logger = logging.getLogger(__name__)

# 프롬프트 다섯 절과 버전이 사는 곳. /data/는 gitignore 대상이라 이 파일들은 커밋되지 않는다 —
# 코드의 DEFAULT_PROMPT_SECTIONS가 정본이고, 여기 파일은 에이전트가 덧쓴 런타임 상태다.
DEFAULT_PROMPT_DIR = Path("data") / "prompt"

VERSION_FILE = "version"
HISTORY_DIR = "history"
WHY_FILE = "why.md"

_VERSION_PATTERN = re.compile(r"^v(\d+)$")


def next_version(current: str) -> str:
    """`v11` → `v12`. 형식이 깨졌으면 코드 상수에서 이어 간다.

    버전 파일이 손상돼도 수정을 멈추지 않되, 손상된 값을 그대로 이어받지도 않는다.
    """
    match = _VERSION_PATTERN.match((current or "").strip())
    if match is None:
        logger.warning("프롬프트 버전 형식이 올바르지 않습니다 (%r) — 코드 상수에서 이어 갑니다.", current)
        match = _VERSION_PATTERN.match(PROMPT_TEMPLATE_VERSION)
    return f"v{int(match.group(1)) + 1}"


class PromptStore:
    """추천 프롬프트의 편집 가능한 다섯 절을 파일로 읽고 쓴다 (PRD '프롬프트 자동 수정').

    읽기는 항상 다섯 키를 채워 돌려준다 — 파일이 없거나 비었으면 코드 기본값으로 폴백한다.
    쓰기는 이력 보관 → 전체 쓰기 → 버전 증가 순서다.
    """

    def __init__(self, prompt_dir: Path = DEFAULT_PROMPT_DIR):
        self.prompt_dir = Path(prompt_dir)

    def load_sections(self) -> Dict[str, str]:
        sections = {}
        for key in PROMPT_SECTION_ORDER:
            sections[key] = self._read(self.prompt_dir / f"{key}.md") or DEFAULT_PROMPT_SECTIONS[key]
        return sections

    def load_version(self) -> str:
        return self._read(self.prompt_dir / VERSION_FILE) or PROMPT_TEMPLATE_VERSION

    def save(self, new_sections: Dict[str, str], reason: str) -> str:
        """고친 절만 받아 전체를 다시 쓰고, 이전 버전을 이력으로 남긴다. 새 버전을 돌려준다.

        이력을 **먼저** 남긴다 — 쓰기가 중간에 실패해도 되돌릴 것이 남는다.
        고치지 않은 절도 함께 쓴다: 파일 다섯 개가 항상 그 버전의 완전한 사본이어야
        이력에서 되돌릴 때 절이 섞이지 않는다.

        원자성이 미치는 범위: 파일 하나하나는 `_write_atomic`으로 교체되므로 쓰다가
        죽어도 그 파일 자체가 반쪽으로 남는 일은 없다. 다만 다섯 파일을 한 세트로
        바꾸는 것까지 원자적이지는 않다 — 쓰는 도중 죽으면 새 절 일부와 이전 절
        일부가 섞인 채로 남을 수 있다. 그래도 안전한 이유는 이력을 먼저 남기기
        때문이다: 무슨 일이 있어도 바로 직전 버전 전체는 history/<version>/에서
        온전하게 복구할 수 있다.
        """
        current_version = self.load_version()
        current_sections = self.load_sections()

        self._archive(current_version, current_sections, reason)

        merged = dict(current_sections)
        merged.update(new_sections)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        for key in PROMPT_SECTION_ORDER:
            self._write_atomic(self.prompt_dir / f"{key}.md", merged[key])

        new_version = next_version(current_version)
        self._write_atomic(self.prompt_dir / VERSION_FILE, new_version)
        logger.info("추천 프롬프트를 수정했습니다: %s → %s", current_version, new_version)
        return new_version

    def _archive(self, version: str, sections: Dict[str, str], reason: str) -> None:
        target = self.prompt_dir / HISTORY_DIR / version
        target.mkdir(parents=True, exist_ok=True)
        for key in PROMPT_SECTION_ORDER:
            self._write_atomic(target / f"{key}.md", sections[key])
        self._write_atomic(target / WHY_FILE, reason)

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        """같은 디렉터리에 임시 파일로 쓴 뒤 os.replace로 옮긴다.

        os.replace는 Windows·POSIX 모두에서 원자적이라, 쓰는 도중 프로세스가 죽어도
        원본 파일은 옛 내용 그대로거나 새 내용 그대로일 뿐 반쪽 상태가 되지 않는다.
        다섯 파일을 한 세트로 묶어 원자적으로 바꾸는 것은 범위 밖이다 (save()의
        docstring 참고).
        """
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)

    @staticmethod
    def _read(path: Path) -> str:
        """파일 내용. 없거나 읽을 수 없거나 공백뿐이면 빈 문자열 — 호출측이 폴백한다.

        UnicodeDecodeError도 함께 잡는다 — 쓰다가 죽어 반쪽만 남은 파일은 OSError가
        아니라 디코딩 실패로 나타난다. 여기서 놓치면 손상 파일 하나가 다음 날 추천
        전체를 멈춘다.
        """
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
