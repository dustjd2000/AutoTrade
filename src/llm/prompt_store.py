import logging
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION_FILE = "version"


class PromptStore:
    """프롬프트의 편집 가능한 절을 파일에서 읽는다 — AI 매도 판단 프롬프트(`data/exit_prompt/`)가 쓴다.

    1호 전략 시절에는 15:35 자동 수정 에이전트가 이 파일들을 덧썼다(PRD 5.13·5.15). 1호와 함께
    자동 수정이 폐지돼(2026-10-23) 지금은 읽기만 한다 — 그때 고쳐진 절이 파일로 남아 있으면 그대로
    쓰고, 없으면 코드 기본값으로 폴백한다.
    """

    def __init__(
        self,
        prompt_dir: Path,
        section_order: Sequence[str],
        defaults: Mapping[str, str],
        default_version: str,
        label: str = "프롬프트",
    ):
        self.prompt_dir = Path(prompt_dir)
        self.section_order: Tuple[str, ...] = tuple(section_order)
        self.defaults: Dict[str, str] = dict(defaults)
        self.default_version = default_version
        self.label = label

    def load_sections(self) -> Dict[str, str]:
        sections = {}
        for key in self.section_order:
            sections[key] = self._read(self.prompt_dir / f"{key}.md") or self.defaults[key]
        return sections

    def load_version(self) -> str:
        return self._read(self.prompt_dir / VERSION_FILE) or self.default_version

    @staticmethod
    def _read(path: Path) -> str:
        """파일 내용. 없거나 읽을 수 없거나 공백뿐이면 빈 문자열 — 호출측이 폴백한다.

        UnicodeDecodeError도 함께 잡는다 — 쓰다가 죽어 반쪽만 남은 파일은 OSError가
        아니라 디코딩 실패로 나타난다. 여기서 놓치면 손상 파일 하나가 매도 판단을 멈춘다.
        """
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
