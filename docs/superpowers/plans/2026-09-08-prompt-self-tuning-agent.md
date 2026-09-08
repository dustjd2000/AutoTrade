# 추천 프롬프트 자동 수정 에이전트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 15:35 검증 직후, 최근 추천 성과를 `prompt_version`별로 읽어 추천 프롬프트의 판단·서술 지침 다섯 절을 LLM이 스스로 고치게 하고, 고친 날만 변경 메일을 보낸다.

**Architecture:** 시스템 프롬프트의 다섯 절을 코드 상수에서 `data/prompt/` 파일로 뺀다 (파일이 없으면 코드 기본값으로 폴백). 새 모듈 셋이 생긴다 — `prompt_store.py`(파일 읽기/쓰기/이력/버전), `tuner.py`(LLM 호출과 안전장치), 그리고 이 둘을 엮는 `DailyWorkflow.tune_prompt`. 검증 단계와 같은 `ActionRunner` 큐를 타고 `REPORT_TIME`에 `review_recommendations` 다음으로 등록된다.

**Tech Stack:** Python 3.14, `anthropic` SDK, SQLite(`sqlite3` 표준 라이브러리), pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-prompt-self-tuning-agent-design.md`

## Global Constraints

- **매수·청산 로직은 건드리지 않는다.** 이 기능은 프롬프트 텍스트만 바꾼다.
- **잠긴 세 절(`역할`·`절대 규칙`·`추천 유형`)은 파일로 빼지 않고 코드에 남으며, 에이전트가 고칠 수 없다.** 이 절들은 `drop_unknown_tickers`/`drop_other_setups`/`normalize_target_price`/`warn_invalid_sell_targets`가 강제하는 규칙의 문장판이라, 어긋나면 조용히 추천이 0종목이 된다.
- **편집 가능한 절 키는 정확히 다섯 개다:** `judgment_criteria`, `buy_target`, `sell_target`, `outlook`, `reason`. 이 순서가 프롬프트 조립 순서다.
- **한 번에 최대 2절.** 걸러내기 **전** 원본 개수로 센다. 초과하면 전체를 버린다.
- 기존 메일 네 종류(추천 / 매수 결과 / 일일 리포트 / 추천 검증)는 그대로 둔다.
- 어떤 실패도 매매 흐름을 막지 않는다 — LLM 실패·파일 실패는 경고 로그만 남기고 넘어간다.
- 빈 값 규약: 문자열은 `""`, 숫자는 `0`이 "산출 안 됨"이다.
- `/data/`는 gitignore 대상이다 — 프롬프트 파일은 커밋되지 않고, **코드 기본값이 정본**이다.
- 코드·식별자·**커밋 메시지는 영어**, 주석과 프롬프트·메일 문자열은 한국어. 기존 파일의 주석 밀도와 서술 방식을 그대로 따른다.
- 커밋은 각 Task 끝에서 한 번. **push는 하지 않는다.**
- Windows / PowerShell 5.1 — `&&`, `||`, `head`, `tail` 등은 쓸 수 없다. Bash 도구를 쓰면 POSIX sh로 돌아간다.
- 저장소에 가상환경이 있으면(`.venv` 또는 `venv`) 그 인터프리터로 pytest를 돌린다.

---

### Task 1: 프롬프트 다섯 절을 상수로 분리 (동작 불변 리팩터)

**Files:**
- Modify: `src/llm/recommender.py:121-215` (`build_system_prompt`)
- Test: `tests/test_llm/test_recommender.py`

**Interfaces:**
- Produces:
  - `PROMPT_SECTION_ORDER: tuple[str, ...]` = `("judgment_criteria", "buy_target", "sell_target", "outlook", "reason")`
  - `DEFAULT_PROMPT_SECTIONS: Dict[str, str]` — 키는 위 다섯, 값은 `## ` 헤더 줄을 **포함한** 그 절의 전문
  - `build_system_prompt(target_count: int, sections: Optional[Dict[str, str]] = None) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_llm/test_recommender.py` 끝에 붙인다. import에 `DEFAULT_PROMPT_SECTIONS`, `PROMPT_SECTION_ORDER`를 더한다.

```python
def test_prompt_sections_have_exactly_the_five_editable_keys():
    assert PROMPT_SECTION_ORDER == (
        "judgment_criteria", "buy_target", "sell_target", "outlook", "reason"
    )
    assert set(DEFAULT_PROMPT_SECTIONS) == set(PROMPT_SECTION_ORDER)


def test_each_default_section_carries_its_own_header():
    """헤더를 코드가 따로 붙이지 않고 절 본문이 들고 있어야 에이전트가 헤더까지 고칠 수 있다."""
    for key, text in DEFAULT_PROMPT_SECTIONS.items():
        assert text.lstrip().startswith("## "), key


def test_default_prompt_keeps_locked_and_editable_sections_in_order():
    prompt = build_system_prompt(3)
    headers = [
        "## 역할",
        "## 절대 규칙",
        "## 추천 유형 (setup)",
        "## 판단 기준",
        "## 목표 매수가 작성 지침",
        "## 목표 매도가 작성 지침",
        "## 오늘 전망 작성 지침",
        "## 근거 작성 지침",
    ]
    positions = [prompt.find(h) for h in headers]
    assert all(p != -1 for p in positions), positions
    assert positions == sorted(positions)
    # 잠긴 절의 보간이 살아 있어야 한다
    assert "코스피 대형주 3종목" in prompt


def test_sections_argument_replaces_only_that_section():
    prompt = build_system_prompt(3, sections={"outlook": "## 오늘 전망 작성 지침\n바뀐 내용"})
    assert "바뀐 내용" in prompt
    # 넘기지 않은 절은 기본값이 그대로
    assert DEFAULT_PROMPT_SECTIONS["reason"] in prompt
    # 넘긴 절의 기본값은 사라진다
    assert DEFAULT_PROMPT_SECTIONS["outlook"] not in prompt


def test_missing_or_blank_section_falls_back_to_default():
    prompt = build_system_prompt(3, sections={"outlook": "   "})
    assert DEFAULT_PROMPT_SECTIONS["outlook"] in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm/test_recommender.py -k "section or locked" -v`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_PROMPT_SECTIONS'`

- [ ] **Step 3: 다섯 절을 상수로 옮긴다**

`build_system_prompt` **위**에 상수를 만든다. `src/llm/recommender.py:154-215`의 다섯 절 텍스트를 **한 글자도 바꾸지 말고** 그대로 옮긴다 — 이 Task는 순수 리팩터이고, 문구를 손대면 추천 결과가 달라진다.

```python
# 에이전트가 고칠 수 있는 다섯 절 (PRD '프롬프트 자동 수정'). 헤더 줄을 본문에 포함해 둔다 —
# 조립할 때 코드가 헤더를 붙이면 에이전트가 헤더를 고치고 싶어도 못 고친다.
# 잠긴 절(역할·절대 규칙·추천 유형)은 build_system_prompt 안에 그대로 남는다: 코드가 강제하는
# 규칙의 문장판이라, 문장만 바뀌면 조용히 추천이 0종목이 된다.
PROMPT_SECTION_ORDER = ("judgment_criteria", "buy_target", "sell_target", "outlook", "reason")

DEFAULT_PROMPT_SECTIONS: Dict[str, str] = {
    "judgment_criteria": """## 판단 기준 (제공된 데이터 범위 내에서, 우선순위 순)
...(원문 그대로)...""",
    "buy_target": """## 목표 매수가 작성 지침
...(원문 그대로)...""",
    "sell_target": """## 목표 매도가 작성 지침
...(원문 그대로)...""",
    "outlook": """## 오늘 전망 작성 지침
...(원문 그대로)...""",
    "reason": """## 근거 작성 지침
...(원문 그대로)...""",
}
```

주의: 이 다섯 절은 **f-string이 아닌 일반 문자열**이다 (`{target_count}` 보간이 없다). 원문에 중괄호가 있어도 그대로 리터럴이 된다.

`typing` import에 `Dict`가 없으면 더한다.

- [ ] **Step 4: `build_system_prompt`를 조립 방식으로 바꾼다**

잠긴 부분만 f-string으로 남기고 다섯 절을 이어 붙인다.

```python
def build_system_prompt(target_count: int, sections: Optional[Dict[str, str]] = None) -> str:
    """시스템 프롬프트를 조립한다. `sections`로 편집 가능한 절을 갈아끼울 수 있다.

    넘기지 않았거나 비어 있는 절은 `DEFAULT_PROMPT_SECTIONS`로 폴백한다 — 파일이 깨져도
    추천이 멈추지 않아야 한다 (PRD '프롬프트 자동 수정').
    """
    provided = sections or {}
    locked = f"""당신은 한국 주식시장(코스피) 단기 모멘텀을 분석하는 애널리스트입니다.

## 역할
...(역할·절대 규칙·추천 유형 원문 그대로, {target_count} 보간 유지)..."""

    parts = [locked]
    for key in PROMPT_SECTION_ORDER:
        text = (provided.get(key) or "").strip()
        parts.append(text or DEFAULT_PROMPT_SECTIONS[key])
    return "\n\n".join(parts)
```

원래 프롬프트에서 절과 절 사이는 빈 줄 하나였다. `"\n\n".join`이 그것을 재현하므로, 각 절 상수의 **앞뒤 공백을 남기지 않는다**(끝에 개행을 붙이지 않는다).

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_llm/test_recommender.py -v`
Expected: PASS (기존 테스트 포함 전부 — 특히 `test_system_prompt_mentions_outlook_rules`가 그대로 통과해야 원문이 보존된 것이다)

- [ ] **Step 6: 전체 테스트**

Run: `pytest`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/llm/recommender.py tests/test_llm/test_recommender.py
git commit -m "refactor: split the editable prompt sections into constants"
```

---

### Task 2: `src/llm/prompt_store.py` — 파일 저장소

**Files:**
- Create: `src/llm/prompt_store.py`
- Create: `tests/test_llm/test_prompt_store.py`

**Interfaces:**
- Consumes: `DEFAULT_PROMPT_SECTIONS`, `PROMPT_SECTION_ORDER`, `PROMPT_TEMPLATE_VERSION` (Task 1 / 기존)
- Produces:
  - `DEFAULT_PROMPT_DIR = Path("data") / "prompt"`
  - `PromptStore(prompt_dir: Path = DEFAULT_PROMPT_DIR)`
  - `PromptStore.load_sections() -> Dict[str, str]` — 다섯 키가 항상 채워진다 (파일 없으면 기본값)
  - `PromptStore.load_version() -> str`
  - `PromptStore.save(new_sections: Dict[str, str], reason: str) -> str` — 이력 보관 → 쓰기 → 버전 증가, 새 버전을 돌려준다
  - `next_version(current: str) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_llm/test_prompt_store.py`를 새로 만든다.

```python
from pathlib import Path

from src.llm.prompt_store import PromptStore, next_version
from src.llm.recommender import (
    DEFAULT_PROMPT_SECTIONS,
    PROMPT_SECTION_ORDER,
    PROMPT_TEMPLATE_VERSION,
)


def test_next_version_increments():
    assert next_version("v11") == "v12"
    assert next_version("v9") == "v10"


def test_next_version_falls_back_on_garbage():
    """버전 파일이 깨져도 멈추지 않는다 — 코드 상수에서 이어 간다."""
    assert next_version("쓰레기") == next_version(PROMPT_TEMPLATE_VERSION)


def test_load_sections_without_files_returns_defaults(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    assert store.load_sections() == DEFAULT_PROMPT_SECTIONS


def test_load_version_without_file_returns_code_constant(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    assert store.load_version() == PROMPT_TEMPLATE_VERSION


def test_save_writes_files_archives_history_and_bumps_version(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    new_version = store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")

    assert new_version == next_version(PROMPT_TEMPLATE_VERSION)
    assert store.load_version() == new_version
    sections = store.load_sections()
    assert sections["outlook"] == "## 오늘 전망 작성 지침\n새 내용"
    # 고치지 않은 절은 기본값 그대로 파일에 쓰인다
    assert sections["reason"] == DEFAULT_PROMPT_SECTIONS["reason"]

    history = tmp_path / "prompt" / "history" / PROMPT_TEMPLATE_VERSION
    assert (history / "outlook.md").read_text(encoding="utf-8") == DEFAULT_PROMPT_SECTIONS["outlook"]
    assert "시험" in (history / "why.md").read_text(encoding="utf-8")


def test_second_save_archives_the_first_version(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    v_first = store.save({"outlook": "## 오늘 전망 작성 지침\n첫 번째 수정본입니다"}, reason="1회")
    store.save({"reason": "## 근거 작성 지침\n두 번째 수정본입니다"}, reason="2회")

    archived = tmp_path / "prompt" / "history" / v_first / "outlook.md"
    assert "첫 번째 수정본입니다" in archived.read_text(encoding="utf-8")


def test_blank_file_falls_back_to_default(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")
    (tmp_path / "prompt" / "outlook.md").write_text("   ", encoding="utf-8")

    assert store.load_sections()["outlook"] == DEFAULT_PROMPT_SECTIONS["outlook"]


def test_load_sections_always_has_all_five_keys(tmp_path):
    store = PromptStore(tmp_path / "prompt")
    store.save({"outlook": "## 오늘 전망 작성 지침\n새 내용"}, reason="시험")
    assert set(store.load_sections()) == set(PROMPT_SECTION_ORDER)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm/test_prompt_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.llm.prompt_store'`

- [ ] **Step 3: 모듈 작성**

```python
import logging
import re
import shutil
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
        """
        current_version = self.load_version()
        current_sections = self.load_sections()

        self._archive(current_version, current_sections, reason)

        merged = dict(current_sections)
        merged.update(new_sections)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        for key in PROMPT_SECTION_ORDER:
            (self.prompt_dir / f"{key}.md").write_text(merged[key], encoding="utf-8")

        new_version = next_version(current_version)
        (self.prompt_dir / VERSION_FILE).write_text(new_version, encoding="utf-8")
        logger.info("추천 프롬프트를 수정했습니다: %s → %s", current_version, new_version)
        return new_version

    def _archive(self, version: str, sections: Dict[str, str], reason: str) -> None:
        target = self.prompt_dir / HISTORY_DIR / version
        target.mkdir(parents=True, exist_ok=True)
        for key in PROMPT_SECTION_ORDER:
            (target / f"{key}.md").write_text(sections[key], encoding="utf-8")
        (target / WHY_FILE).write_text(reason, encoding="utf-8")

    @staticmethod
    def _read(path: Path) -> str:
        """파일 내용. 없거나 읽을 수 없거나 공백뿐이면 빈 문자열 — 호출측이 폴백한다."""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
```

`shutil` import는 쓰지 않으면 넣지 않는다.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm/test_prompt_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/llm/prompt_store.py tests/test_llm/test_prompt_store.py
git commit -m "feat: store the editable prompt sections in files with history"
```

---

### Task 3: 추천 경로가 파일 프롬프트와 파일 버전을 쓴다

**Files:**
- Modify: `src/llm/recommender.py` (`LLMRecommender.__init__`, `recommend`)
- Modify: `src/core/daily_workflow.py` (`_save_recommendations`)
- Test: `tests/test_llm/test_recommender.py`, `tests/test_core/test_daily_workflow.py`

**Interfaces:**
- Consumes: `PromptStore.load_sections()`, `PromptStore.load_version()` (Task 2)
- Produces: `LLMRecommender(settings, prompt_store=None)` — `None`이면 `PromptStore()`를 만든다; `LLMRecommender.prompt_version` 프로퍼티가 현재 파일 버전을 돌려준다

- [ ] **Step 1: Write the failing tests**

`tests/test_llm/test_recommender.py`에 붙인다.

```python
def test_recommender_uses_prompt_store_sections(tmp_path):
    """파일에 저장된 절이 실제 요청의 시스템 프롬프트에 들어간다."""
    from src.llm.prompt_store import PromptStore

    store = PromptStore(tmp_path / "prompt")
    store.save({"outlook": "## 오늘 전망 작성 지침\n파일에서 온 내용입니다"}, reason="시험")

    recommender = LLMRecommender.__new__(LLMRecommender)
    recommender.settings = SimpleNamespace(llm_model="claude-opus-5", target_stock_count=3)
    recommender.prompt_store = store

    assert "파일에서 온 내용입니다" in recommender.build_prompt_for_test()
    assert recommender.prompt_version == store.load_version()
```

`build_prompt_for_test`를 새로 만들지 말고, 아래 Step 3에서 도입하는 `_system_prompt()` 헬퍼를 테스트가 직접 부르도록 이름을 맞춘다 — 즉 위 테스트의 `build_prompt_for_test()`를 `_system_prompt()`로 쓴다.

`tests/test_core/test_daily_workflow.py`에 붙인다.

```python
def test_recommend_and_notify_saves_the_file_prompt_version(tmp_path):
    """저장되는 prompt_version은 코드 상수가 아니라 recommender가 실제로 쓴 버전이다."""
    workflow = build_workflow(tmp_path)
    workflow.recommender.prompt_version = "v99"

    workflow.recommend_and_notify(date(2026, 9, 8))

    rows = workflow.trade_store.recommendations_for(date(2026, 9, 8))
    assert rows[0].prompt_version == "v99"
```

`build_workflow`의 가짜 recommender에 `prompt_version = PROMPT_TEMPLATE_VERSION` 속성을 더해 기존 테스트가 그대로 통과하게 한다.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm/test_recommender.py -k prompt_store tests/test_core/test_daily_workflow.py -k file_prompt_version -v`
Expected: FAIL — `AttributeError: 'LLMRecommender' object has no attribute 'prompt_store'`

- [ ] **Step 3: `LLMRecommender`가 저장소를 쓴다**

생성자에 선택 인자를 더한다.

```python
    def __init__(self, settings: Settings, prompt_store=None):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        # 편집 가능한 다섯 절의 출처. 파일이 없으면 코드 기본값으로 폴백하므로 첫 실행도 정상이다.
        self.prompt_store = prompt_store if prompt_store is not None else PromptStore()
```

같은 클래스에 헬퍼와 프로퍼티를 더한다.

```python
    @property
    def prompt_version(self) -> str:
        """이번 추천에 실제로 쓰인 프롬프트 버전 — 검증 집계의 기준이다."""
        return self.prompt_store.load_version()

    def _system_prompt(self) -> str:
        return build_system_prompt(
            self.settings.target_stock_count, self.prompt_store.load_sections()
        )
```

`recommend`에서 `system=build_system_prompt(target_count)`를 `system=self._system_prompt()`로 바꾸고, 로그의 `PROMPT_TEMPLATE_VERSION`을 `self.prompt_version`으로 바꾼다 (요청 로그와 결과 로그 두 군데).

파일 상단 import에 `from src.llm.prompt_store import PromptStore`를 더한다.

**순환 참조 주의:** `prompt_store.py`가 `recommender.py`에서 상수를 가져오고, `recommender.py`가 `prompt_store.py`에서 클래스를 가져온다. 모듈 최상단에서 서로를 import하면 순환이 된다. `recommender.py` 쪽 import를 **함수 안(`__init__`)으로 내려** 지연 import한다.

```python
    def __init__(self, settings: Settings, prompt_store=None):
        # 순환 import를 피해 여기서 가져온다 — prompt_store가 이 모듈의 상수를 참조한다
        from src.llm.prompt_store import PromptStore
        ...
```

- [ ] **Step 4: 워크플로가 recommender의 버전을 저장한다**

`src/core/daily_workflow.py`의 `_save_recommendations`에서 `PROMPT_TEMPLATE_VERSION` 대신 `self.recommender.prompt_version`을 넘긴다.

```python
            self.trade_store.save_recommendations(
                today, recommendations, self.recommender.prompt_version
            )
```

`PROMPT_TEMPLATE_VERSION` import가 더 이상 쓰이지 않으면 지운다.

- [ ] **Step 5: Run tests**

Run: `pytest`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/llm/recommender.py src/core/daily_workflow.py tests/
git commit -m "feat: build the system prompt from the file-backed sections"
```

---

### Task 4: `TradeStore.recent_recommendations`

**Files:**
- Modify: `src/logger/trade_store.py`
- Test: `tests/test_logger/test_trade_store.py`

**Interfaces:**
- Consumes: `RecommendationRow` (기존)
- Produces: `TradeStore.recent_recommendations(day_count: int = 10) -> List[RecommendationRow]` — 검증이 끝난(`actual_close IS NOT NULL`) 행만, 가장 최근 `day_count`개의 **서로 다른 날짜**에 속한 것만, 날짜 오름차순

- [ ] **Step 1: Write the failing tests**

`tests/test_logger/test_trade_store.py`에 붙인다. 이 파일의 기존 `make_store(tmp_path)`와 `_rec(...)` 헬퍼를 쓴다.

```python
def _verify(store, day, ticker, close=71_000.0):
    store.save_recommendation_outcome(
        day, ticker,
        actual_high=72_000.0, actual_low=69_500.0, actual_close=close,
        actual_change_rate=1.43, buy_target_hit=True, sell_target_hit=False,
    )


def test_recent_recommendations_returns_only_verified_rows(tmp_path):
    store = make_store(tmp_path)
    day = date(2026, 9, 4)
    store.save_recommendations(day, [_rec("005930"), _rec("000660", name="SK하이닉스")], "v11")
    _verify(store, day, "005930")

    rows = store.recent_recommendations(10)
    assert [r.ticker for r in rows] == ["005930"]


def test_recent_recommendations_limits_to_the_latest_distinct_days(tmp_path):
    store = make_store(tmp_path)
    for offset, day in enumerate([date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]):
        store.save_recommendations(day, [_rec("005930")], "v11")
        _verify(store, day, "005930")

    rows = store.recent_recommendations(2)
    assert sorted({r.day for r in rows}) == [date(2026, 9, 2), date(2026, 9, 3)]


def test_recent_recommendations_is_ordered_oldest_first(tmp_path):
    store = make_store(tmp_path)
    for day in [date(2026, 9, 3), date(2026, 9, 1), date(2026, 9, 2)]:
        store.save_recommendations(day, [_rec("005930")], "v11")
        _verify(store, day, "005930")

    rows = store.recent_recommendations(10)
    assert [r.day for r in rows] == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


def test_recent_recommendations_empty_when_nothing_verified(tmp_path):
    store = make_store(tmp_path)
    store.save_recommendations(date(2026, 9, 4), [_rec("005930")], "v11")
    assert store.recent_recommendations(10) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_logger/test_trade_store.py -k recent_recommendations -v`
Expected: FAIL — `AttributeError: 'TradeStore' object has no attribute 'recent_recommendations'`

- [ ] **Step 3: 메서드 추가**

`recommendations_for` 아래에 넣는다. `RecommendationRow`를 만드는 부분이 `recommendations_for`와 겹치므로, 그 변환을 모듈 수준 헬퍼로 빼서 둘이 공유한다.

```python
    def recent_recommendations(self, day_count: int = 10) -> List[RecommendationRow]:
        """최근 `day_count` 거래일의 **검증이 끝난** 추천 (오래된 날부터).

        프롬프트 자동 수정 에이전트의 입력이다 (PRD '프롬프트 자동 수정'). 검증 전 행은
        대조할 실제값이 없어 제외한다 — 오늘 추천은 15:35 검증 뒤에야 여기에 들어온다.

        `recommendations` 테이블에는 거래일만 들어가므로 '최근 N개의 서로 다른 날짜'가
        곧 최근 N거래일이다.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM recommendations
                   WHERE actual_close IS NOT NULL
                     AND day IN (
                         SELECT DISTINCT day FROM recommendations
                         WHERE actual_close IS NOT NULL
                         ORDER BY day DESC LIMIT ?
                     )
                   ORDER BY day, id""",
                (day_count,),
            ).fetchall()
        return [_recommendation_row(row) for row in rows]
```

모듈 하단 헬퍼 영역에 변환 함수를 더하고, `recommendations_for`도 이것을 쓰도록 바꾼다. 그 함수는 `day`를 행에서 읽는다(`recommendations_for`는 인자로 받은 `day`를 쓰고 있었다).

```python
def _recommendation_row(row) -> RecommendationRow:
    """`recommendations` 한 행을 dataclass로. 두 조회가 공유한다."""
    return RecommendationRow(
        day=date.fromisoformat(row["day"]),
        ticker=row["ticker"],
        name=row["name"] or "",
        prompt_version=row["prompt_version"] or "",
        recommend_price=row["recommend_price"] or 0.0,
        target_price=row["target_price"] or 0,
        target_sell_price=row["target_sell_price"] or 0,
        setup=row["setup"] or "",
        reason=row["reason"] or "",
        outlook=row["outlook"] or "",
        actual_high=row["actual_high"],
        actual_low=row["actual_low"],
        actual_close=row["actual_close"],
        actual_change_rate=row["actual_change_rate"],
        buy_target_hit=_optional_bool(row["buy_target_hit"]),
        sell_target_hit=_optional_bool(row["sell_target_hit"]),
        review=row["review"] or "",
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_logger/test_trade_store.py -v`
Expected: PASS (기존 `recommendations_for` 테스트 포함 — 공유 헬퍼로 바꾼 뒤에도 그대로 통과해야 한다)

- [ ] **Step 5: Commit**

```bash
git add src/logger/trade_store.py tests/test_logger/test_trade_store.py
git commit -m "feat: read the verified recommendations of the last N trading days"
```

---

### Task 5: `src/llm/tuner.py` — 에이전트와 안전장치

**Files:**
- Create: `src/llm/tuner.py`
- Create: `tests/test_llm/test_tuner.py`

**Interfaces:**
- Consumes: `RecommendationRow` (기존), `PROMPT_SECTION_ORDER`/`DEFAULT_PROMPT_SECTIONS` (Task 1), `MAX_TOKENS`/`_extract_json` (기존)
- Produces:
  - `TUNE_PROMPT_TEMPLATE_VERSION = "v1"`, `MAX_SECTIONS_PER_CHANGE = 2`, `MIN_SECTION_LENGTH = 50`
  - `VersionStats` dataclass — `version: str`, `count: int`, `buy_hit: int`, `sell_hit: int`, `avg_change_rate: float`
  - `group_by_version(rows: List[RecommendationRow]) -> List[VersionStats]`
  - `sanitize_sections(raw: Dict[str, str]) -> Dict[str, str]` — 안전장치
  - `TuneResult` dataclass — `change: bool`, `reason: str`, `sections: Dict[str, str]`
  - `parse_tune_response(raw_text: str) -> TuneResult`
  - `PromptTuner(settings)` / `PromptTuner.tune(stats, rows, sections, why_history, timeout_seconds=120.0) -> Optional[TuneResult]`

- [ ] **Step 1: Write the failing tests**

`tests/test_llm/test_tuner.py`를 새로 만든다.

```python
from datetime import date
from types import SimpleNamespace

from src.llm.recommender import DEFAULT_PROMPT_SECTIONS
from src.llm.tuner import (
    MAX_SECTIONS_PER_CHANGE,
    PromptTuner,
    group_by_version,
    parse_tune_response,
    sanitize_sections,
)
from src.logger.trade_store import RecommendationRow


def row(version="v11", buy=True, sell=False, rate=1.0, ticker="005930"):
    return RecommendationRow(
        day=date(2026, 9, 4), ticker=ticker, name="종목", prompt_version=version,
        recommend_price=100.0, target_price=100, target_sell_price=110,
        setup="rebound", reason="근거", outlook="전망",
        actual_high=110.0, actual_low=90.0, actual_close=105.0, actual_change_rate=rate,
        buy_target_hit=buy, sell_target_hit=sell, review="평가",
    )


LONG = "## 오늘 전망 작성 지침\n" + ("가" * 80)


def test_group_by_version_counts_hits_and_average():
    stats = group_by_version([
        row("v11", buy=True, sell=False, rate=2.0),
        row("v11", buy=False, sell=False, rate=0.0),
        row("v12", buy=True, sell=True, rate=4.0),
    ])
    by = {s.version: s for s in stats}
    assert by["v11"].count == 2 and by["v11"].buy_hit == 1 and by["v11"].sell_hit == 0
    assert by["v11"].avg_change_rate == 1.0
    assert by["v12"].count == 1 and by["v12"].sell_hit == 1


def test_sanitize_keeps_a_valid_section():
    assert sanitize_sections({"outlook": LONG}) == {"outlook": LONG}


def test_sanitize_drops_unknown_and_locked_keys():
    assert sanitize_sections({"역할": LONG}) == {}
    assert sanitize_sections({"absolute_rules": LONG}) == {}


def test_sanitize_drops_short_or_blank_body():
    assert sanitize_sections({"outlook": "짧다"}) == {}
    assert sanitize_sections({"outlook": "   "}) == {}


def test_sanitize_discards_everything_when_over_the_limit():
    """걸러내기 전 원본 개수로 센다 — 잘못된 절을 섞어 제한을 우회할 수 없어야 한다."""
    raw = {"outlook": LONG, "reason": LONG, "역할": "x"}
    assert len(raw) > MAX_SECTIONS_PER_CHANGE
    assert sanitize_sections(raw) == {}


def test_sanitize_allows_exactly_the_limit():
    raw = {"outlook": LONG, "reason": LONG}
    assert set(sanitize_sections(raw)) == {"outlook", "reason"}


def test_parse_reads_change_and_sections():
    result = parse_tune_response(
        '{"change": true, "reason": "이유", "sections": {"outlook": "%s"}}' % LONG.replace("\n", "\\n")
    )
    assert result.change is True
    assert result.reason == "이유"
    assert "outlook" in result.sections


def test_parse_no_change():
    result = parse_tune_response('{"change": false, "reason": "표본이 얇다", "sections": {}}')
    assert result.change is False
    assert result.sections == {}


def test_tune_returns_none_when_api_raises():
    tuner = PromptTuner.__new__(PromptTuner)
    tuner.settings = SimpleNamespace(anthropic_api_key="k", llm_model="claude-opus-5")

    class Boom:
        def with_options(self, **kwargs):
            raise RuntimeError("network down")

    tuner._client = Boom()
    assert tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm/test_tuner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.llm.tuner'`

- [ ] **Step 3: 모듈 작성**

`src/llm/tuner.py`를 만든다. `reviewer.py`의 구조(스키마 → 프롬프트 빌더 → 파서 → 클라이언트 클래스, 실패는 전부 `None`)를 그대로 따른다.

```python
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import anthropic

from config.settings import Settings
from src.llm.recommender import (
    DEFAULT_PROMPT_SECTIONS,
    MAX_TOKENS,
    PROMPT_SECTION_ORDER,
    _extract_json,
)
from src.logger.trade_store import RecommendationRow

logger = logging.getLogger(__name__)

TUNE_PROMPT_TEMPLATE_VERSION = "v1"

# 한 번에 고칠 수 있는 절 수. 다섯 절을 한꺼번에 바꾸면 무엇이 효과가 있었는지 영원히 모른다.
MAX_SECTIONS_PER_CHANGE = 2
# 이보다 짧은 본문은 지침 구실을 못 한다 — 실수로 빈 절을 쓰는 것을 막는다.
MIN_SECTION_LENGTH = 50

TUNE_KEY_SECTIONS = "sections"
TUNE_SCHEMA = {
    "type": "object",
    "properties": {
        "change": {"type": "boolean", "description": "프롬프트를 고칠지 여부"},
        "reason": {"type": "string", "description": "고치는/고치지 않는 이유와 근거 수치"},
        TUNE_KEY_SECTIONS: {
            "type": "object",
            "properties": {
                key: {"type": "string", "description": f"{key} 절의 전문 (## 헤더 포함)"}
                for key in PROMPT_SECTION_ORDER
            },
            "additionalProperties": False,
        },
    },
    "required": ["change", "reason", TUNE_KEY_SECTIONS],
    "additionalProperties": False,
}


@dataclass
class VersionStats:
    """한 프롬프트 버전의 성과 — 에이전트가 자기 수정 전후를 비교하는 근거다."""

    version: str
    count: int
    buy_hit: int
    sell_hit: int
    avg_change_rate: float


@dataclass
class TuneResult:
    change: bool
    reason: str
    sections: Dict[str, str] = field(default_factory=dict)


def group_by_version(rows: List[RecommendationRow]) -> List[VersionStats]:
    """검증된 추천을 `prompt_version`별로 묶는다. 버전 이름 순으로 돌려준다."""
    buckets: Dict[str, List[RecommendationRow]] = {}
    for row in rows:
        buckets.setdefault(row.prompt_version or "(없음)", []).append(row)

    stats = []
    for version, group in buckets.items():
        rates = [r.actual_change_rate for r in group if r.actual_change_rate is not None]
        stats.append(
            VersionStats(
                version=version,
                count=len(group),
                buy_hit=sum(1 for r in group if r.buy_target_hit),
                sell_hit=sum(1 for r in group if r.sell_target_hit),
                avg_change_rate=sum(rates) / len(rates) if rates else 0.0,
            )
        )
    return sorted(stats, key=lambda s: s.version)


def sanitize_sections(raw: Dict[str, str]) -> Dict[str, str]:
    """적용 전 안전장치 (PRD '프롬프트 자동 수정').

    개수 검사가 **가장 먼저**이고 걸러내기 전 원본 개수를 센다 — 걸러낸 뒤에 세면
    잘못된 절을 섞어 보내는 것으로 제한을 우회할 수 있다.

    스키마가 이미 키를 다섯 개로 제한하지만 코드에서 한 번 더 자른다. 스키마는 모델이
    지키는 소프트 제약이고, 잠긴 절이 실제로 반영되는 것은 고장이기 때문이다
    (`drop_other_setups`가 유형 제한을 코드로 다시 자르는 것과 같은 이유다).
    """
    if not isinstance(raw, dict):
        return {}
    if len(raw) > MAX_SECTIONS_PER_CHANGE:
        logger.warning(
            "한 번에 고칠 수 있는 절은 %d개입니다 — %d개가 와서 전체를 버립니다: %s",
            MAX_SECTIONS_PER_CHANGE, len(raw), list(raw),
        )
        return {}

    kept = {}
    for key, text in raw.items():
        if key not in PROMPT_SECTION_ORDER:
            logger.warning("고칠 수 없는 절이라 무시합니다: %s", key)
            continue
        body = (text or "").strip()
        if len(body) < MIN_SECTION_LENGTH:
            logger.warning("본문이 너무 짧아 무시합니다: %s (%d자)", key, len(body))
            continue
        kept[key] = body
    return kept


def build_tune_system_prompt() -> str:
    return f"""당신은 주식 추천 프롬프트를 성과 데이터에 근거해 다듬는 편집자입니다.

## 역할
아래 프롬프트로 만들어진 추천의 실제 결과를 보고, 프롬프트의 **판단·서술 지침**을 고칠지
판단합니다. 고칠 필요가 없으면 고치지 않는 것이 정상입니다.

## 고칠 수 있는 절
`judgment_criteria`(판단 기준), `buy_target`(목표 매수가 작성 지침),
`sell_target`(목표 매도가 작성 지침), `outlook`(오늘 전망 작성 지침),
`reason`(근거 작성 지침) — 이 다섯뿐입니다.

`역할`·`절대 규칙`·`추천 유형` 세 절은 **고칠 수 없습니다.** 프로그램 코드가 그 규칙을
강제하고 있어, 문장만 바꾸면 코드와 어긋나 추천이 통째로 버려집니다. 참고용으로 보여줄
뿐이니, 나머지 다섯 절이 그 규칙과 모순되지 않게 하는 데만 쓰십시오.

## 규칙
1. **한 번에 최대 {MAX_SECTIONS_PER_CHANGE}절만** 고치십시오. 더 많이 고치면 무엇이 효과가
   있었는지 알 수 없어 전체가 폐기됩니다.
2. 고치는 절은 `##` 헤더 줄을 포함한 **전문**을 주십시오. 일부만 주면 나머지가 사라집니다.
3. `reason`에는 **근거로 삼은 수치를 인용**하십시오. ("v11 12건 중 목표 매도가 도달 2건"처럼)
4. **표본이 부족하면 고치지 마십시오.** 버전별 건수가 한 자릿수면 우연과 신호를 구분할 수
   없습니다. 그럴 때는 `change: false`로 두고 그 이유를 적으십시오.
5. 이전 변경 이력이 함께 주어집니다. 직전에 고친 것을 되돌리는 방향으로 다시 고치지
   마십시오 — 그러면 프롬프트가 왔다 갔다 하기만 합니다.
6. 데이터가 말하지 않는 것을 지어내지 마십시오. 주어진 집계와 개별 결과만 근거로 삼습니다."""


def build_tune_user_prompt(
    stats: List[VersionStats],
    rows: List[RecommendationRow],
    sections: Dict[str, str],
    locked_text: str,
    why_history: str,
) -> str:
    lines = ["## 버전별 성과 (검증이 끝난 추천만)"]
    if stats:
        for s in stats:
            lines.append(
                f"- {s.version}: {s.count}건 | 목표 매수가 도달 {s.buy_hit}건 | "
                f"목표 매도가 도달 {s.sell_hit}건 | 평균 등락률 {s.avg_change_rate:+.2f}%"
            )
    else:
        lines.append("- 아직 검증된 추천이 없습니다.")

    lines.append("\n## 개별 결과")
    for r in rows:
        lines.append(
            f"- {r.day} {r.ticker} {r.name} ({r.prompt_version}): "
            f"추천가 {r.recommend_price:,.0f} / 목표매수 {r.target_price:,} / "
            f"목표매도 {r.target_sell_price:,} → 고 {r.actual_high:,.0f} / 저 {r.actual_low:,.0f} / "
            f"종 {r.actual_close:,.0f} ({r.actual_change_rate:+.2f}%)"
        )
        lines.append(f"  전망: {r.outlook}")
        if r.review:
            lines.append(f"  평가: {r.review}")

    lines.append("\n## 고칠 수 없는 절 (참고용)")
    lines.append(locked_text)

    lines.append("\n## 현재 고칠 수 있는 절")
    for key in PROMPT_SECTION_ORDER:
        lines.append(f"\n### {key}\n{sections[key]}")

    lines.append("\n## 이전 변경 이력")
    lines.append(why_history or "(없음 — 아직 고친 적이 없습니다)")

    lines.append("\n위 결과를 근거로 프롬프트를 고칠지 판단하고, 고친다면 그 절의 전문을 주십시오.")
    return "\n".join(lines)


def parse_tune_response(raw_text: str) -> TuneResult:
    """응답을 `TuneResult`로. 형식이 어긋나면 예외를 던진다 — 호출측이 잡아 무변경으로 끝낸다."""
    data = json.loads(_extract_json(raw_text))
    if not isinstance(data, dict):
        raise ValueError("tune response must be a JSON object")
    sections = data.get(TUNE_KEY_SECTIONS) or {}
    return TuneResult(
        change=bool(data.get("change")),
        reason=str(data.get("reason", "")).strip(),
        sections=sections if isinstance(sections, dict) else {},
    )


class PromptTuner:
    """추천 프롬프트를 성과 데이터로 다듬는 모듈 — 추천·검증과 같은 모델, 별도 프롬프트."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def tune(
        self,
        stats: List[VersionStats],
        rows: List[RecommendationRow],
        sections: Dict[str, str],
        locked_text: str,
        why_history: str = "",
        timeout_seconds: float = 120.0,
    ) -> Optional[TuneResult]:
        """수정 판단을 받아 돌려준다. 실패하면 None — 그날은 아무것도 고치지 않는다."""
        user_prompt = build_tune_user_prompt(stats, rows, sections, locked_text, why_history)
        logger.info(
            "프롬프트 수정 요청 (tune_prompt_version=%s, %d건):\n%s",
            TUNE_PROMPT_TEMPLATE_VERSION, len(rows), user_prompt,
        )
        try:
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                system=build_tune_system_prompt(),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema", "schema": TUNE_SCHEMA}},
            )
        except Exception:
            logger.exception("프롬프트 수정 호출이 실패했거나 타임아웃되었습니다.")
            return None

        if response.stop_reason in ("max_tokens", "refusal"):
            logger.error("프롬프트 수정 응답이 정상 종료되지 않았습니다: %s", response.stop_reason)
            return None

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        if not raw_text.strip():
            logger.error("프롬프트 수정 응답에 텍스트가 없습니다. stop_reason=%s", response.stop_reason)
            return None

        try:
            return parse_tune_response(raw_text)
        except Exception:
            logger.exception("프롬프트 수정 응답 파싱 실패. 원문(앞 500자): %s", raw_text[:500])
            return None
```

`tune`의 시그니처가 테스트의 `tuner.tune([], [], DEFAULT_PROMPT_SECTIONS, "")`와 맞는지 확인한다 — 네 번째 위치 인자가 `locked_text`다.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_llm/test_tuner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/llm/tuner.py tests/test_llm/test_tuner.py
git commit -m "feat: add the prompt tuner with its safety filters"
```

---

### Task 6: 변경 메일 템플릿

**Files:**
- Modify: `src/notification/templates.py`
- Test: `tests/test_notification/test_templates.py`

**Interfaces:**
- Consumes: `VersionStats` (Task 5)
- Produces: `prompt_tuning_email(today: date, old_version: str, new_version: str, reason: str, stats: List[VersionStats], before: Dict[str, str], after: Dict[str, str]) -> tuple[str, str]` — (제목, 평문)

- [ ] **Step 1: Write the failing tests**

import에 `VersionStats`(`src.llm.tuner`)를 더한다.

```python
def test_prompt_tuning_email_shows_versions_reason_and_diff():
    subject, body = templates.prompt_tuning_email(
        date(2026, 9, 8), "v11", "v12", "목표 매도가 도달이 12건 중 2건뿐이라 완화했습니다.",
        [VersionStats("v11", 12, 9, 2, 1.25)],
        before={"outlook": "## 오늘 전망 작성 지침\n예전 내용"},
        after={"outlook": "## 오늘 전망 작성 지침\n새 내용"},
    )
    assert "2026-09-08 추천 프롬프트 수정 (v11 → v12)" in subject
    assert "목표 매도가 도달이 12건 중 2건뿐이라 완화했습니다." in body
    assert "v11: 12건" in body
    assert "예전 내용" in body
    assert "새 내용" in body
    assert "data/prompt/history/v11" in body


def test_prompt_tuning_email_lists_every_changed_section():
    _, body = templates.prompt_tuning_email(
        date(2026, 9, 8), "v11", "v12", "이유",
        [VersionStats("v11", 12, 9, 2, 1.25)],
        before={"outlook": "## 오늘 전망\n전", "reason": "## 근거\n전2"},
        after={"outlook": "## 오늘 전망\n후", "reason": "## 근거\n후2"},
    )
    assert "outlook" in body and "reason" in body
    assert "후" in body and "후2" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notification/test_templates.py -k prompt_tuning -v`
Expected: FAIL — `AttributeError: module 'src.notification.templates' has no attribute 'prompt_tuning_email'`

- [ ] **Step 3: 템플릿 함수 작성**

`recommendation_review_email` 아래에 넣는다.

```python
def prompt_tuning_email(
    today: date,
    old_version: str,
    new_version: str,
    reason: str,
    stats: List["VersionStats"],
    before: Dict[str, str],
    after: Dict[str, str],
) -> tuple[str, str]:
    """추천 프롬프트를 자동 수정한 날 나가는 이메일 (PRD '프롬프트 자동 수정').

    고친 날만 발송한다 — 고치지 않은 날은 호출되지 않는다. 사람이 개입할 유일한 지점이므로
    되돌리는 방법을 본문에 함께 적는다.

    표가 없어 HTML을 함께 만들지 않는다. (제목, 평문)만 돌려준다.
    """
    subject = f"[AutoTrade] {today:%Y-%m-%d} 추천 프롬프트 수정 ({old_version} → {new_version})"

    lines = [
        f"{today:%Y-%m-%d} 추천 프롬프트를 자동으로 수정했습니다 ({old_version} → {new_version}).",
        "",
        "## 수정 이유",
        reason or "(없음)",
        "",
        "## 근거 — 버전별 성과",
    ]
    for s in stats:
        lines.append(
            f" - {s.version}: {s.count}건 | 목표 매수가 도달 {s.buy_hit}건 | "
            f"목표 매도가 도달 {s.sell_hit}건 | 평균 등락률 {s.avg_change_rate:+.2f}%"
        )

    for key in sorted(after):
        lines.extend(["", f"## 바뀐 절: {key}", "", "[이전]", before.get(key, "(없음)"), "", "[이후]", after[key]])

    lines.extend([
        "",
        f"※ 되돌리려면 data/prompt/history/{old_version}/ 의 파일들을 data/prompt/ 로 복사하십시오.",
        "※ 수정된 프롬프트는 다음 거래일 추천부터 적용됩니다 (엔진 재시작 불필요).",
    ])
    return subject, "\n".join(lines)
```

`VersionStats`는 타입 힌트에만 쓰므로 문자열 힌트로 두고 import하지 않는다 — `templates.py`가 `src.llm.tuner`를 import하면 알림 계층이 LLM 계층을 끌어온다.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_notification/test_templates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/notification/templates.py tests/test_notification/test_templates.py
git commit -m "feat: add the prompt tuning notification email"
```

---

### Task 7: `DailyWorkflow.tune_prompt` + 스케줄 등록

**Files:**
- Modify: `src/core/daily_workflow.py`, `src/core/actions.py`, `src/core/runtime.py`
- Test: `tests/test_core/test_daily_workflow.py`, `tests/test_core/test_actions.py`

**Interfaces:**
- Consumes: `TradeStore.recent_recommendations` (Task 4), `PromptStore` (Task 2), `PromptTuner`/`group_by_version`/`sanitize_sections` (Task 5), `templates.prompt_tuning_email` (Task 6)
- Produces: `DailyWorkflow.tune_prompt(today: Optional[date] = None) -> None`; `SCHEDULED_ACTIONS["tune_prompt"]`

- [ ] **Step 1: Write the failing tests**

`tests/test_core/test_daily_workflow.py`에 붙인다. `build_workflow`에 가짜 `PromptTuner`(`result` 속성을 돌려주고 `calls`를 남긴다)와 임시 경로의 진짜 `PromptStore`를 배선한다.

```python
LONG_OUTLOOK = "## 오늘 전망 작성 지침\n" + ("가" * 80)


def test_tune_prompt_applies_change_and_sends_mail(tmp_path):
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 8)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.trade_store.save_recommendation_outcome(
        day, "005930", actual_high=72_000.0, actual_low=69_500.0, actual_close=71_000.0,
        actual_change_rate=1.43, buy_target_hit=True, sell_target_hit=False,
    )
    workflow.tuner.result = SimpleNamespace(
        change=True, reason="근거", sections={"outlook": LONG_OUTLOOK}
    )

    workflow.tune_prompt(day)

    assert workflow.prompt_store.load_sections()["outlook"] == LONG_OUTLOOK
    assert workflow.prompt_store.load_version() == "v12"
    assert "추천 프롬프트 수정" in workflow.email.sent[-1][0]


def test_tune_prompt_does_nothing_when_change_is_false(tmp_path):
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 8)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.trade_store.save_recommendation_outcome(
        day, "005930", actual_high=72_000.0, actual_low=69_500.0, actual_close=71_000.0,
        actual_change_rate=1.43, buy_target_hit=True, sell_target_hit=False,
    )
    workflow.tuner.result = SimpleNamespace(change=False, reason="표본이 얇다", sections={})

    workflow.tune_prompt(day)

    assert workflow.prompt_store.load_version() == PROMPT_TEMPLATE_VERSION
    assert workflow.email.sent == []


def test_tune_prompt_skips_when_nothing_verified(tmp_path):
    """검증된 추천이 없으면 LLM을 부르지도 않는다."""
    workflow = build_workflow(tmp_path)
    workflow.tune_prompt(date(2026, 9, 8))
    assert workflow.tuner.calls == []
    assert workflow.email.sent == []


def test_tune_prompt_discards_a_change_that_fails_sanitizing(tmp_path):
    """안전장치에 전부 걸리면 change=true여도 아무것도 바뀌지 않고 메일도 안 나간다."""
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 8)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.trade_store.save_recommendation_outcome(
        day, "005930", actual_high=72_000.0, actual_low=69_500.0, actual_close=71_000.0,
        actual_change_rate=1.43, buy_target_hit=True, sell_target_hit=False,
    )
    workflow.tuner.result = SimpleNamespace(change=True, reason="근거", sections={"역할": LONG_OUTLOOK})

    workflow.tune_prompt(day)

    assert workflow.prompt_store.load_version() == PROMPT_TEMPLATE_VERSION
    assert workflow.email.sent == []


def test_tune_prompt_survives_llm_failure(tmp_path):
    workflow = build_workflow(tmp_path)
    day = date(2026, 9, 8)
    workflow.trade_store.save_recommendations(day, [_recommendation()], "v11")
    workflow.trade_store.save_recommendation_outcome(
        day, "005930", actual_high=72_000.0, actual_low=69_500.0, actual_close=71_000.0,
        actual_change_rate=1.43, buy_target_hit=True, sell_target_hit=False,
    )
    workflow.tuner.result = None

    workflow.tune_prompt(day)

    assert workflow.prompt_store.load_version() == PROMPT_TEMPLATE_VERSION
    assert workflow.email.sent == []
```

`tests/test_core/test_actions.py`에 붙인다.

```python
def test_tune_prompt_step_is_scheduled_only():
    assert "tune_prompt" in SCHEDULED_ACTIONS
    assert "tune_prompt" not in MANUAL_ACTIONS
    assert "tune_prompt" not in ORDER_ACTIONS


def test_tune_prompt_step_runs_off_the_loop_thread():
    runtime = make_runtime([])
    steps = manual_steps(runtime, "tune_prompt")
    assert len(steps) == 1
    assert steps[0].touches_orders is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_core/test_daily_workflow.py -k tune_prompt -v`
Expected: FAIL — `AttributeError: 'DailyWorkflow' object has no attribute 'tune_prompt'`

- [ ] **Step 3: 생성자에 두 협력자를 더한다**

`DailyWorkflow.__init__`의 `reviewer=None` **다음**에 더한다.

```python
        tuner=None,
        prompt_store=None,
```

본문에 붙인다.

```python
        # 프롬프트 자동 수정 (PRD '프롬프트 자동 수정'). tuner가 None이면 단계 자체를 건너뛴다.
        self.tuner = tuner
        self.prompt_store = prompt_store if prompt_store is not None else PromptStore()
```

상단 import에 `from src.llm.prompt_store import PromptStore`, `from src.llm import tuner as tuner_module`를 더한다.

- [ ] **Step 4: `tune_prompt` 구현**

`review_recommendations` 아래에 넣는다.

```python
    def tune_prompt(self, today: Optional[date] = None) -> None:
        """15:35 — 최근 추천 성과를 보고 추천 프롬프트의 다섯 절을 자동으로 고친다.

        추천 검증 **다음**에 돈다 — 그날 검증이 끝나야 판단 재료가 완성된다. 고친 날만
        메일이 나가고, 고치지 않는 날이 정상 동작이다 (PRD '프롬프트 자동 수정').

        어떤 실패도 매매 흐름을 막지 않는다. 고치지 못하면 다음 거래일 추천은 이전
        프롬프트로 정상 동작한다.
        """
        today = today or date.today()
        if self.tuner is None:
            return

        rows = self.trade_store.recent_recommendations()
        if not rows:
            logger.info("검증된 추천이 없습니다 — 프롬프트 수정을 건너뜁니다 (%s).", today)
            return

        stats = tuner_module.group_by_version(rows)
        before = self.prompt_store.load_sections()
        old_version = self.prompt_store.load_version()
        locked_text = build_locked_prompt_text(self.strategy.target_stock_count)

        result = self.tuner.tune(stats, rows, before, locked_text, self._why_history())
        if result is None or not result.change:
            logger.info(
                "프롬프트를 고치지 않습니다 (%s): %s",
                today, getattr(result, "reason", "판단 실패"),
            )
            return

        sections = tuner_module.sanitize_sections(result.sections)
        if not sections:
            logger.warning("수정안이 안전장치에 전부 걸렸습니다 — 그대로 둡니다 (%s).", today)
            return

        try:
            new_version = self.prompt_store.save(sections, result.reason)
        except OSError:
            logger.exception("프롬프트 파일 쓰기에 실패했습니다 — 그대로 둡니다.")
            return

        after = self.prompt_store.load_sections()
        subject, body = templates.prompt_tuning_email(
            today, old_version, new_version, result.reason, stats,
            {key: before[key] for key in sections},
            {key: after[key] for key in sections},
        )
        self.email.send(subject, body)
        logger.info("프롬프트 수정 메일 발송 (%s, %s → %s)", today, old_version, new_version)

    def _why_history(self) -> str:
        """직전 변경들의 이유. 에이전트가 자기 수정을 되돌리는 것을 막는 입력이다.

        이력 폴더가 없으면 빈 문자열 — 첫 실행이 그 상태다.
        """
        history_dir = self.prompt_store.prompt_dir / "history"
        try:
            versions = sorted(p for p in history_dir.iterdir() if p.is_dir())
        except OSError:
            return ""
        entries = []
        for path in versions[-5:]:
            try:
                entries.append(f"- {path.name}: {(path / 'why.md').read_text(encoding='utf-8')}")
            except OSError:
                continue
        return "\n".join(entries)
```

- [ ] **Step 5: 잠긴 절 본문을 꺼낼 수 있게 한다**

`tune_prompt`가 쓰는 `build_locked_prompt_text`를 `src/llm/recommender.py`에 더하고, `build_system_prompt`가 그것을 쓰도록 바꾼다 — 잠긴 절 텍스트가 두 군데에 복사되면 어긋난다.

```python
def build_locked_prompt_text(target_count: int) -> str:
    """고칠 수 없는 세 절(역할·절대 규칙·추천 유형). 자동 수정 에이전트에게 참고용으로 준다."""
    return f"""당신은 한국 주식시장(코스피) 단기 모멘텀을 분석하는 애널리스트입니다.
...(기존 잠긴 부분 원문 그대로)..."""


def build_system_prompt(target_count: int, sections: Optional[Dict[str, str]] = None) -> str:
    provided = sections or {}
    parts = [build_locked_prompt_text(target_count)]
    for key in PROMPT_SECTION_ORDER:
        text = (provided.get(key) or "").strip()
        parts.append(text or DEFAULT_PROMPT_SECTIONS[key])
    return "\n\n".join(parts)
```

`daily_workflow.py`의 import에 `build_locked_prompt_text`를 더한다.

- [ ] **Step 6: 액션과 스케줄 등록**

`src/core/actions.py`의 `SCHEDULED_ACTIONS`에 더한다.

```python
    "tune_prompt": "추천 프롬프트 자동 수정 (스케줄)",
```

`step_factories`의 `review_recommendations` 아래에 더한다.

```python
        # LLM 호출과 파일 쓰기가 걸리므로 루프 스레드를 쓰지 않는다. 주문을 내지 않는다.
        "tune_prompt": lambda: [
            ManualStep(SCHEDULED_ACTIONS["tune_prompt"], runtime.workflow.tune_prompt)
        ],
```

`src/core/runtime.py`의 `build_runtime`에서 `DailyWorkflow(...)` 인자에 더한다.

```python
        tuner=PromptTuner(settings),
```

import에 `from src.llm.tuner import PromptTuner`를 더한다. 스케줄 등록 튜플의 `(REPORT_TIME, "review_recommendations")` **다음 줄**에 더한다.

```python
        # 검증 다음에 등록한다 — 그날 검증 결과가 이 단계의 판단 재료다
        (REPORT_TIME, "tune_prompt"),
```

- [ ] **Step 7: Run tests**

Run: `pytest`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/core/ src/llm/recommender.py tests/test_core/
git commit -m "feat: tune the recommendation prompt from verified results after the close"
```

---

### Task 8: 문서 갱신

**Files:**
- Modify: `주식자동매매_PRD.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1~7의 최종 동작
- Produces: 없음 (문서)

- [ ] **Step 1: PRD에 "추천 프롬프트 자동 수정" 절을 더한다**

추천 검증 절(5.12) 다음에 새 절을 넣는다. 이웃 절의 번호·구조·서술 방식을 그대로 따른다. 담을 내용:

- **무엇이 파일로 나갔나** — 판단·서술 지침 다섯 절(`judgment_criteria`/`buy_target`/`sell_target`/`outlook`/`reason`)이 `data/prompt/` 아래 파일이 됐다. `/data/`는 gitignore 대상이라 **코드의 `DEFAULT_PROMPT_SECTIONS`가 정본**이고, 파일이 없거나 비면 그 값으로 폴백한다
- **왜 세 절은 잠갔나** — `역할`·`절대 규칙`·`추천 유형`은 `drop_unknown_tickers`/`drop_other_setups`/`normalize_target_price`/`warn_invalid_sell_targets`가 강제하는 규칙의 문장판이다. 문장만 바뀌면 코드와 어긋나 조용히 추천이 0종목이 된다
- **언제 도나** — 15:35, 추천 검증 다음. 거래일에만
- **무엇을 보고 판단하나** — 최근 10거래일의 **검증이 끝난** 추천을 `prompt_version`별로 묶은 집계와 개별 결과, 현재 다섯 절 전문, 잠긴 세 절(참고용), 이전 변경 이유
- **안전장치** — 걸러내기 전 원본 기준 2절 초과면 전체 폐기, 허용된 다섯 키가 아니면 무시, 50자 미만이면 무시, 남은 것이 없으면 무변경
- **고치지 않는 것이 정상** — 표본이 얇으면 `change: false`로 넘어간다
- **반영 시점** — 다음 거래일 추천부터. **엔진 재시작이 필요 없다** (`.env` 설정과 다른 동작)
- **버전** — `data/prompt/version` 파일이 실제 버전을 들고 `recommendations.prompt_version`에 저장된다. 코드 상수 `PROMPT_TEMPLATE_VERSION`은 폴백용이다
- **이력과 되돌리기** — 고치기 전 다섯 파일 사본과 `why.md`가 `data/prompt/history/<이전 버전>/`에 남는다. 되돌리려면 그 파일들을 `data/prompt/`로 복사한다. 자동 되돌리기는 없다
- **알림** — 고친 날만 메일 한 통. 사람이 개입할 유일한 지점이다
- **받아들인 위험** — 표본이 얇다는 것(하루 2~3종목), 표류 위험, 승인 절차 없이 실전 계좌에 영향이 간다는 것. 설계 문서의 "받아들인 위험" 절을 요약해 옮긴다

- [ ] **Step 2: PRD의 일정 표에 15:35 단계를 더한다**

일일 리포트 → 추천 검증 → **프롬프트 자동 수정** 순서임을 밝힌다.

- [ ] **Step 3: `CLAUDE.md`의 "설정은 `.env` 하나로 통일" 서술을 고친다**

그 절은 "별도 JSON/YAML 설정 파일은 쓰지 않는다"고 적고 있는데, 이제 `data/prompt/`가 `.env` 밖의 상태 저장소다. 그 문장을 지우지 말고 **예외를 덧붙인다** — 프롬프트는 사용자가 정하는 설정이 아니라 에이전트가 갱신하는 런타임 상태이고, 그래서 `.env`가 아니라 `data/` 아래에 있으며, 엔진 재시작 없이 반영된다는 점을 밝힌다.

- [ ] **Step 4: `CLAUDE.md`의 전략/스케줄 서술을 확인한다**

"스레드 / 이벤트 루프 구조" 절의 시각 나열과 "전략 프레임워크" 절에 15:35 프롬프트 자동 수정 단계를 반영한다. 15:35에 세 단계(리포트 → 검증 → 수정)가 순서대로 돈다는 것을 적는다.

- [ ] **Step 5: 전체 테스트**

Run: `pytest`
Expected: PASS (문서 변경이라 무영향이어야 한다 — 확인용)

- [ ] **Step 6: Commit**

```bash
git add 주식자동매매_PRD.md CLAUDE.md
git commit -m "docs: record the automatic prompt tuning rules"
```

---

## Self-Review

**Spec coverage**

| 스펙 항목 | Task |
|---|---|
| 다섯 절 분리, 헤더 포함, 폴백 | 1 |
| `data/prompt/` 파일 저장소, 버전, 이력 + `why.md` | 2 |
| 추천이 파일 프롬프트를 쓰고 파일 버전을 저장 | 3 |
| 재시작 없이 다음 추천에 반영 | 3 (파일을 매 호출 시 읽음) |
| 최근 10거래일 검증분 조회 | 4 |
| 버전별 집계 | 5 (`group_by_version`) |
| 에이전트 입력 4종(집계·개별·현재 절·잠긴 절·이력) | 5 (`build_tune_user_prompt`) + 7 (`_why_history`, `build_locked_prompt_text`) |
| 출력 스키마, `change:false` | 5 |
| 안전장치 5종 | 5 (`sanitize_sections`) + 7 (남은 것이 없으면 무변경) |
| 고친 날만 메일, before/after, 되돌리는 법 | 6 |
| 15:35 검증 다음, `touches_orders=False`, 거래일만 | 7 |
| 실패가 매매를 막지 않음 | 7 (LLM `None`, `OSError` 처리) |
| PRD·`CLAUDE.md` 갱신 | 8 |

**범위 밖 확인** — 매수·청산 로직, `build_user_prompt`, `reviewer.py`, 기존 메일 네 종류, 자동 되돌리기는 어느 Task에서도 건드리지 않는다.

**타입 일관성** — `PROMPT_SECTION_ORDER`/`DEFAULT_PROMPT_SECTIONS`(Task 1)를 Task 2·5가 같은 이름으로 쓴다. `PromptStore.load_sections/load_version/save`(Task 2)의 시그니처가 Task 3·7의 호출과 일치한다. `VersionStats`(Task 5)의 필드가 Task 6 템플릿과 일치한다. `TuneResult.change/reason/sections`가 Task 7의 소비와 일치한다. `build_locked_prompt_text(target_count)`(Task 7 Step 5)를 Task 7 Step 4가 부른다 — 같은 Task 안이라 순서에 주의한다.

**알려진 순서 의존** — Task 7의 Step 5(잠긴 절 추출)를 Step 4(`tune_prompt` 구현)보다 **먼저** 해도 된다. Step 4가 `build_locked_prompt_text`를 부르므로, 테스트를 돌리기 전에 Step 5가 끝나 있어야 한다.
