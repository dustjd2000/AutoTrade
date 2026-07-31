# LLM 종목 추천 프롬프트 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `src/llm/recommender.py`의 LLM 종목 추천 프롬프트 품질을 개선한다 — 판단 기준·근거 작성 지침을 명시하고, 데이터 근거가 약할 때 3개 미만 추천을 허용하고, `temperature`를 낮게 고정한다.

**Architecture:** 단일 파일(`src/llm/recommender.py`) 변경. 출력 스키마(`RECOMMENDATION_SCHEMA`), `StockRecommendation` 데이터클래스, 데이터 수집 계층(`src/data/collector.py`)은 변경하지 않는다. 다운스트림(`LLMMomentumStrategy.build_buy_plans`, 이메일 템플릿)은 이미 3개 미만 추천을 처리하므로 추가 변경이 필요 없다.

**Tech Stack:** Python, Anthropic SDK (`anthropic.Anthropic.messages.create`), pytest.

## Global Constraints

- 변경 파일은 `src/llm/recommender.py`와 `tests/test_llm/test_recommender.py`로 한정한다 — 스키마·다운스트림 코드는 건드리지 않는다.
- `SYSTEM_PROMPT`·`build_user_prompt()`의 정확한 문구를 assert하는 기존 테스트는 없음(확인 완료) — 부분 일치(`in`) 테스트만 있으므로 문구는 자유롭게 바꿀 수 있다.
- 추천 개수가 3개 미만이 되는 것은 이미 지원되는 동작이다 — `parse_recommendations`의 "빈 배열은 예외" 규칙은 바꾸지 않는다(최소 1개까지만 프롬프트로 허용).

---

### Task 1: `temperature=0.3` 명시 + 판단 기준을 담은 `SYSTEM_PROMPT` 재작성 + 버전 bump

**Files:**
- Modify: `src/llm/recommender.py`
- Test: `tests/test_llm/test_recommender.py`

**Interfaces:**
- Consumes: 기존 `LLMRecommender`, `build_user_prompt(daily_data: List[DailyStockData]) -> str`, `_response(stop_reason, blocks)` 테스트 헬퍼 (변경 없음).
- Produces: `SYSTEM_PROMPT`(str, 내용만 변경 — 시그니처 없음), `PROMPT_TEMPLATE_VERSION = "v2"`, `recommend()`가 `messages.create(..., temperature=0.3, ...)`로 호출됨 (다른 파라미터는 기존과 동일: `model`, `max_tokens`, `system`, `messages`, `output_config`).

- [ ] **Step 1: temperature 검증용 실패하는 테스트 작성**

`tests/test_llm/test_recommender.py`의 `test_recommend_parses_successful_response` 아래에 추가:

```python
def test_recommend_uses_low_temperature_for_consistency():
    """매수 판단이므로 창의성보다 일관성이 우선 — 매번 결과가 크게 흔들리면 안 된다."""
    text = '{"recommendations": [{"ticker": "005930", "name": "삼성전자", "reason": "수급"}]}'
    response = _response("end_turn", [SimpleNamespace(type="text", text=text)])
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return response

    recommender = LLMRecommender.__new__(LLMRecommender)
    recommender.settings = SimpleNamespace(llm_model="claude-sonnet-5")
    recommender._client = SimpleNamespace(
        with_options=lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    )

    recommender.recommend([])

    assert captured.get("temperature") == 0.3
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python -m pytest tests/test_llm/test_recommender.py::test_recommend_uses_low_temperature_for_consistency -v`
Expected: FAIL — `assert None == 0.3` (아직 `temperature`를 안 넘기므로 `captured`에 키가 없음)

- [ ] **Step 3: `recommend()`에 `temperature=0.3` 추가**

`src/llm/recommender.py`의 `recommend()` 안 `messages.create(...)` 호출을 수정:

```python
            response = self._client.with_options(timeout=timeout_seconds).messages.create(
                model=self.settings.llm_model,
                max_tokens=MAX_TOKENS,
                temperature=0.3,  # 매수 판단이므로 일관성 우선 — 창의성 불필요
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_prompt(daily_data)}],
                # 응답 형식을 API가 스키마로 강제한다 (설명이 섞이거나 코드펜스가 붙는 것을 방지)
                output_config={
                    "format": {"type": "json_schema", "schema": RECOMMENDATION_SCHEMA}
                },
            )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m pytest tests/test_llm/test_recommender.py::test_recommend_uses_low_temperature_for_consistency -v`
Expected: PASS

- [ ] **Step 5: `SYSTEM_PROMPT`를 판단 기준·근거 작성 지침 포함 버전으로 재작성**

`src/llm/recommender.py`의 기존 `SYSTEM_PROMPT` 정의를 통째로 교체:

```python
SYSTEM_PROMPT = """당신은 한국 주식시장(코스피/코스닥) 단기 모멘텀을 분석하는 애널리스트입니다.

## 역할
사용자가 제공하는 당일 데이터만을 근거로, 오늘 장중 상대적으로 강한 상승 흐름을 보일 가능성이 높은
코스피 대형주(시가총액 상위 100종목) 최대 3종목을 선별합니다.

## 절대 규칙
1. 반드시 제공된 데이터에 있는 종목 중에서만 선택하십시오. 목록에 없는 종목을 추천하지 마십시오.
2. 당신의 학습 데이터에 있는 과거 정보나 기억(종목에 대한 일반적 평판 등)에 의존하지 마십시오. 오직 아래
   제공된 당일 데이터만 근거로 삼으십시오.
3. 서로 다른 종목만 선택하십시오 (중복 불가).
4. 근거가 확실한 종목이 3개 미만이면 억지로 채우지 말고, 확신이 서는 종목만 그 수만큼(최소 1개) 추천하십시오.

## 판단 기준 (제공된 데이터 범위 내에서, 우선순위 순)
- 시가 갭의 방향성과 크기
- 전일 종가 대비 등락률
- 거래량 — 절대적인 평균 대비 급증 여부는 판단할 수 없으니, 함께 제공된 다른 종목과의 상대적 규모로만
  참고하십시오
- 뉴스/공시 헤드라인의 구체성 (실적·수주·계약 등 구체적 재료인지, 단순 언급인지)

## 근거 작성 지침
reason은 반드시 제공된 데이터의 구체적 수치를 인용해 작성하십시오.
("등락률 +2.15%, 시가갭 +1.8%"처럼 구체적으로. "긍정적 모멘텀", "상승 여력" 같은 모호한 표현은 금지합니다.)"""
```

- [ ] **Step 6: `build_user_prompt()` 마지막 지시문을 3개 미만 허용 문구로 수정**

`src/llm/recommender.py`의 `build_user_prompt()` 마지막 줄:

```python
    lines.append("\n위 데이터를 참고해 급등 예상 종목을 최대 3개까지, 근거가 확실한 만큼만 JSON 배열로 추천하세요.")
```

(기존 `"\n위 데이터를 참고해 급등 예상 종목 3개를 JSON 배열로 추천하세요."` 줄을 대체)

- [ ] **Step 7: `PROMPT_TEMPLATE_VERSION`을 `"v2"`로 bump**

```python
PROMPT_TEMPLATE_VERSION = "v2"
```

- [ ] **Step 8: 전체 회귀 테스트 실행**

Run: `python -m pytest tests/test_llm/test_recommender.py tests/test_strategy/test_llm_momentum.py tests/test_notification/test_templates.py tests/test_core/test_daily_workflow.py -v`
Expected: 모두 PASS (문구 변경은 `in` 부분 일치 테스트에만 영향을 주며, `test_build_user_prompt_includes_all_stock_data`는 `005930`/`삼성전자`/`+1.50%`/`신규 수주 공시` 포함 여부만 확인하므로 영향 없음)

- [ ] **Step 9: 커밋**

```bash
git add src/llm/recommender.py tests/test_llm/test_recommender.py
git commit -m "$(cat <<'EOF'
LLM 종목 추천 프롬프트에 판단 기준·근거 작성 지침 추가, temperature 고정

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
