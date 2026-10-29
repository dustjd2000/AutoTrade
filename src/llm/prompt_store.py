import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION_FILE = "version"
# 파일이 어느 코드 프롬프트 버전을 바탕으로 고쳐졌는지 — 코드 쪽 버전과 다르면 파일을 쓰지 않는다.
BASE_FILE = "base"
HISTORY_DIR = "history"
WHY_FILE = "why.md"

# 버전은 고친 날짜(`20260909`)이고, 같은 날 두 번째부터 꼬리표가 붙는다(`20260909-2`).
_VERSION_PATTERN = re.compile(r"^(\d{8})(?:-(\d+))?$")


def next_version(current: str, today: Optional[date] = None) -> str:
    """새 버전은 **고친 날짜**(YYYYMMDD)다. 같은 날 다시 고치면 `-2`, `-3`으로 이어 간다.

    꼬리표가 필요한 이유는 이력이 `history/<version>/`에 버전 이름으로 쌓이기 때문이다 —
    같은 날 두 번 고치면서 이름이 겹치면 앞서 남긴 이력을 덮어써 되돌릴 것이 사라진다.

    옛 표기(`v11`)나 깨진 값이 들어와도 멈추지 않는다 — 오늘 날짜가 언제나 올바른 답이라,
    종전처럼 손상된 값을 이어받을지 따질 일이 없다.
    """
    stamp = (today or date.today()).strftime("%Y%m%d")
    match = _VERSION_PATTERN.match((current or "").strip())
    if match is None:
        if (current or "").strip():
            logger.info("프롬프트 버전 표기 %r을 날짜 표기로 바꿉니다.", current)
        return stamp
    if match.group(1) != stamp:
        return stamp
    return f"{stamp}-{int(match.group(2) or 1) + 1}"


class PromptStore:
    """프롬프트의 편집 가능한 절을 파일로 읽고 쓴다 — AI 매도 프롬프트(`data/exit_prompt/`)가 쓴다.

    읽기는 항상 모든 키를 채워 돌려준다 — 파일이 없거나 비었으면 코드 기본값으로 폴백한다.
    쓰기는 이력 보관 → 전체 쓰기 → 버전 증가 순서다 (PRD 5.15).

    **코드 버전이 파일보다 앞선다.** 저장할 때 `base` 파일에 그 수정이 바탕으로 삼은 코드 프롬프트
    버전(`default_version`)을 함께 남기고, 읽을 때 `base`가 없거나 코드 버전과 다르면 저장된 절
    파일과 버전을 **무시하고** 코드 기본값·코드 버전을 쓴다(경고는 인스턴스당 한 번). 사람이 코드
    기본값을 고쳤을 때(예: 2026-10-29 v6) 그보다 오래된 자동 수정 파일이 그 수정을 조용히 덮어쓰면
    안 되기 때문이다. 무시된 파일은 다음 `save`가 코드 기본값 위에서 새로 덮어쓴다.
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
        self._warned_stale = False

    def _files_usable(self) -> bool:
        """저장된 파일을 읽어도 되는가. 파일이 아예 없으면 True(폴백이 같은 결과), 낡았으면 False."""
        if self._read(self.prompt_dir / BASE_FILE) == self.default_version:
            return True
        has_files = any(
            self._read(self.prompt_dir / name)
            for name in (VERSION_FILE, *(f"{key}.md" for key in self.section_order))
        )
        if not has_files:
            return True
        if not self._warned_stale:
            self._warned_stale = True
            logger.warning(
                "%s의 코드 버전(%s)이 저장된 파일의 기반 버전(%s)과 달라 자동 수정 파일을 무시하고 "
                "코드 기본값을 씁니다: %s",
                self.label,
                self.default_version,
                self._read(self.prompt_dir / BASE_FILE) or "기록 없음",
                self.prompt_dir,
            )
        return False

    def load_sections(self) -> Dict[str, str]:
        if not self._files_usable():
            return dict(self.defaults)
        sections = {}
        for key in self.section_order:
            sections[key] = self._read(self.prompt_dir / f"{key}.md") or self.defaults[key]
        return sections

    def load_version(self) -> str:
        if not self._files_usable():
            return self.default_version
        return self._read(self.prompt_dir / VERSION_FILE) or self.default_version

    def save(self, new_sections: Dict[str, str], reason: str, today: Optional[date] = None) -> str:
        """고친 절만 받아 전체를 다시 쓰고, 이전 버전을 이력으로 남긴다. 새 버전을 돌려준다.

        `today`는 새 버전의 날짜다 — 호출측이 그날의 검증 결과를 보고 고치는 것이므로,
        달력 날짜가 아니라 **판단 근거가 된 거래일**을 그대로 버전으로 삼는다.

        이력을 **먼저** 남긴다 — 쓰기가 중간에 실패해도 되돌릴 것이 남는다.
        고치지 않은 절도 함께 쓴다: 파일이 항상 그 버전의 완전한 사본이어야
        이력에서 되돌릴 때 절이 섞이지 않는다.

        원자성이 미치는 범위: 파일 하나하나는 `_write_atomic`으로 교체되므로 쓰다가
        죽어도 그 파일 자체가 반쪽으로 남는 일은 없다. 다만 여러 파일을 한 세트로
        바꾸는 것까지 원자적이지는 않다 — 쓰는 도중 죽으면 새 절 일부와 이전 절
        일부가 섞인 채로 남을 수 있다. 그래도 안전한 이유는 이력을 먼저 남기기
        때문이다: 무슨 일이 있어도 바로 직전 버전 전체는 history/<version>/에서
        온전하게 복구할 수 있다. `base`는 절·버전 파일을 다 쓴 **뒤에** 쓴다 — 도중에 죽으면
        `base`가 낡은 채로 남아 반쯤 쓰인 세트가 읽히지 않는다.
        """
        current_version = self.load_version()
        current_sections = self.load_sections()

        self._archive(current_version, current_sections, reason)

        merged = dict(current_sections)
        merged.update(new_sections)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        for key in self.section_order:
            self._write_atomic(self.prompt_dir / f"{key}.md", merged[key])

        new_version = next_version(current_version, today)
        self._write_atomic(self.prompt_dir / VERSION_FILE, new_version)
        self._write_atomic(self.prompt_dir / BASE_FILE, self.default_version)
        logger.info("%s를 수정했습니다: %s → %s", self.label, current_version, new_version)
        return new_version

    def _archive(self, version: str, sections: Dict[str, str], reason: str) -> None:
        target = self.prompt_dir / HISTORY_DIR / version
        target.mkdir(parents=True, exist_ok=True)
        for key in self.section_order:
            self._write_atomic(target / f"{key}.md", sections[key])
        self._write_atomic(target / WHY_FILE, reason)

    def why_history(self, limit: int = 5) -> str:
        """직전 변경들의 이유 (최근 `limit`개). 에이전트가 자기 수정을 되돌리는 것을 막는 입력이다.

        이력 폴더가 없으면 빈 문자열 — 첫 실행이 그 상태다.
        """
        history_dir = self.prompt_dir / HISTORY_DIR
        try:
            versions = sorted(path for path in history_dir.iterdir() if path.is_dir())
        except OSError:
            return ""
        entries = []
        for path in versions[-limit:]:
            try:
                entries.append(f"- {path.name}: {(path / WHY_FILE).read_text(encoding='utf-8')}")
            except OSError:
                continue
        return "\n".join(entries)

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        """같은 디렉터리에 임시 파일로 쓴 뒤 os.replace로 옮긴다.

        os.replace는 Windows·POSIX 모두에서 원자적이라, 쓰는 도중 프로세스가 죽어도
        원본 파일은 옛 내용 그대로거나 새 내용 그대로일 뿐 반쪽 상태가 되지 않는다.
        여러 파일을 한 세트로 묶어 원자적으로 바꾸는 것은 범위 밖이다 (save()의
        docstring 참고).
        """
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)

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
