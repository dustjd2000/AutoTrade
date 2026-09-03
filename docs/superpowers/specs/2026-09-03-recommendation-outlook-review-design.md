# 추천 종목 전망과 장 마감 후 검증 — 설계

작성 2026-09-03

## 배경

1호 전략의 LLM 추천은 종목마다 목표 매수가·목표 매도가·추천 근거·셋업 유형을 낸다.
추천 근거(`reason`)는 **왜 골랐는가**를 과거 데이터로 설명할 뿐, **오늘 어떻게 움직일
것으로 보는가**는 말하지 않는다.

또한 추천 자체가 어디에도 저장되지 않는다. 메일과 로그, 그리고 엔진 메모리
(`strategy.set_recommendations`)에만 남고, `data/trades.db`의 `trades` 테이블에는 실제
체결만 들어간다. 미체결로 사지 못한 종목은 하루가 지나면 흔적이 사라져, "목표 매수가가
현실적이었는가"를 되짚을 표본이 없다.

이 설계는 둘을 함께 해결한다. LLM에게 **오늘 전망**을 받아 추천 메일에 싣고, 추천 내용을
DB에 남긴 뒤, 장 마감 후 **실제 움직임과 대조한 검증 메일**을 보낸다.

## 범위 밖

- 주문·청산 로직은 건드리지 않는다. `outlook`과 검증 결과는 **표시와 기록 전용**이며,
  매수 여부는 종전대로 `setup=rebound` 필터와 09:08 갭 판정이 정한다.
  (`target_sell_price`를 "받되 주문에 쓰지 않는다"로 둔 PRD 5.5-B 확정 2026-08-07과 같은 방침)
- 15:35 일일 리포트의 내용과 조기 발송 로직(`_report_mark`, `_sells_settled`)은 그대로 둔다.
- 지난 날짜의 검증을 나중에 채우는 보정 기능은 만들지 않는다.
- UI에 검증 단계 즉시 실행 버튼을 더하지 않는다.

---

## 1. `outlook` — 오늘 전망

### 스키마

`RECOMMENDATION_SCHEMA`의 종목 객체에 문자열 필드 `outlook`을 더하고 `required`에 넣는다.

```
"outlook": {"type": "string", "description": "오늘 남은 장중 주가 움직임 전망"}
```

`StockRecommendation`에 `outlook: str = ""`를 더한다. `""`는 "산출 안 됨"이며, 기존
`target_sell_price=0`·`setup=""`과 같은 규약이다. `parse_recommendations`는
`item.get("outlook", "")`로 읽어, 값이 빠져도 추천 자체를 버리지 않는다.

### 프롬프트

`build_system_prompt`에 "오늘 전망 작성 지침" 절을 더한다. 요구 사항은 둘이다.

1. **앞 문장 — 오전 흐름.** 추천 시각까지의 당일 데이터(현재가·당일 등락률·당일 거래량)를
   근거로 오전 중 어디를 시도할 것으로 보는지 적는다. 당일 지표가 없는 종목은 이 문장을
   생략한다.
2. **뒷 문장 — 조건부 분기.** 상방과 하방을 **가격과 함께** 조건부로 적는다.
   ("~를 회복하면 ~까지 열려 있고, 실패하면 ~ 부근까지 되밀릴 수 있음")

금지 사항도 함께 명시한다.

- 오후 시간대의 움직임을 단정하지 않는다 — 일봉과 추천 시각 현재가만 주어지므로 근거가 없다.
- 학습 데이터의 기억(종목 평판, 과거 주가)에 의존하지 않는다 — 절대 규칙 2와 같다.
- `reason`의 내용을 다시 쓰지 않는다. `reason`은 과거(왜 골랐나), `outlook`은 앞으로
  (오늘 어떻게 움직일 것인가)를 말한다.

`PROMPT_TEMPLATE_VERSION`을 `v10` → `v11`로 올린다.

예시:

> "09:05 현재 +1.80%로 갭 상승 출발해 당일 거래량이 실려 있어 오전 중 이동평균
> 82,300원 회복을 시도할 것으로 봅니다. 회복하면 전일 고가 84,000원까지 열려 있고,
> 회복에 실패하면 최근 저가 77,000원 부근까지 되밀릴 수 있습니다."

### 추천 메일

`recommendation_email`에서 추천 근거 다음에 `   오늘 전망: {outlook}` 한 줄을 붙인다.
`outlook`이 비면 줄이 통째로 빠진다 — `_sell_target_line`과 같은 방식이다.

메일 하단 주석에 한 줄을 더한다.

> ※ 오늘 전망은 LLM의 참고 수치이며 주문에 사용되지 않습니다.

목표 매도가 주석과 마찬가지로, 본문에 전망 줄이 하나도 없으면 이 주석도 뺀다.

---

## 2. `recommendations` 테이블

`TradeStore`에 테이블을 하나 더한다. 기존 `trades`의 `SCHEMA` + `MIGRATIONS` 패턴을
그대로 쓴다.

```sql
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,               -- ISO 날짜
    ticker TEXT NOT NULL,
    name TEXT,
    prompt_version TEXT,
    recommend_price REAL,            -- 추천 시각 현재가 (0 = 모름)
    target_price INTEGER,
    target_sell_price INTEGER,       -- 0 = 산출 안 됨
    setup TEXT,
    reason TEXT,
    outlook TEXT,
    actual_high REAL,                -- 이하 15:35에 채운다. NULL = 미검증
    actual_low REAL,
    actual_close REAL,
    actual_change_rate REAL,         -- 전일 종가 대비 %
    buy_target_hit INTEGER,          -- 0/1, NULL = 미검증
    sell_target_hit INTEGER,
    review TEXT,                     -- LLM 평가문. NULL = 없음
    UNIQUE (day, ticker)
);
```

`UNIQUE (day, ticker)`를 두는 이유: UI ① 버튼으로 추천을 두 번 돌려도 행이 겹치지 않게
한다. 저장은 UPSERT(`INSERT ... ON CONFLICT(day, ticker) DO UPDATE`)이며, 나중 추천이
앞선 추천을 덮어쓴다 — 그날 실제로 쓰인 것이 마지막 추천이기 때문이다.

### 저장 시점

`recommend_and_notify`에서 **추천 메일을 보내는 것과 같은 목록**을 저장한다. 즉
`drop_unknown_tickers` → `drop_other_setups` → 가드레일 보정을 모두 거친 뒤의 값이다.
셋업 필터에 걸려 빠진 종목은 주문 경로에 들어가지 않았으므로 추천으로 치지 않는다.

저장은 **추천 메일 발송 직전**에 한다. 저장에 실패해도 예외를 삼키고 경고 로그만 남긴
채 메일 발송과 매수 흐름을 그대로 진행한다 — 기록은 사후 분석용이고, 매매가 그것 때문에
멈출 이유가 없다. `prompt_version`에는 `PROMPT_TEMPLATE_VERSION`의 그때 값을 넣는다.

### 새 메서드

- `save_recommendations(day, recommendations, prompt_version)` — UPSERT.
- `recommendations_for(day) -> List[RecommendationRow]` — 검증 단계가 읽는다.
- `save_recommendation_outcome(day, ticker, ...)` — 실제값·도달 여부·평가문 UPDATE.

`RecommendationRow`는 `TradeRow`와 같은 자리에 두는 dataclass다.

---

## 3. 15:35 검증 단계

### 실행 통로

`SCHEDULED_ACTIONS`에 단계를 더해 `ActionRunner`의 같은 큐를 탄다. `REPORT_TIME`에
`daily_report` **다음으로** 등록한다 — `TimeScheduler.due_jobs`가 등록 순서대로 돌려주고
`ActionRunner` 큐가 FIFO이므로, 최종 리포트가 먼저 나가고 검증이 뒤따른다. `touches_orders=False`이므로 별도 스레드에서
실행되어, 일봉 조회와 LLM 호출이 이벤트 루프를 막지 않는다.

리포트와 별개의 단계이므로, 그날 최종 리포트가 전량 매도로 이미 조기 발송됐더라도
검증은 15:35에 정상적으로 돈다.

### 15:35인 이유

15:30 정각에는 마감 동시호가(15:20~15:30) 체결이 아직 일봉에 반영되지 않아 종가·고가가
틀린 값으로 저장될 수 있다. 일일 리포트를 마감 뒤로 5분 물린 것과 같은 이유다
(`runtime.REPORT_TIME`).

### 절차

1. `trade_store.recommendations_for(today)`로 오늘 추천을 읽는다. **DB에서 읽으므로
   엔진이 중간에 재시작돼도 살아남는다.** 비어 있으면 조용히 끝낸다 — 메일도 보내지 않는다.
2. 종목별로 `ka10086`을 한 번씩 조회해 **당일 봉**의 고가·저가·종가를 뽑는다. 아침 수집이
   당일 봉을 날짜로 걸러내는 것과 정반대로, 여기서는 당일 봉만 쓴다. 추천은 최대 3종목이라
   호출 3회다. 조회에 실패한 종목은 실제값을 NULL로 남기고 나머지 종목만 진행한다.
3. 도달 여부를 판정한다.
   - `buy_target_hit` = `actual_low <= target_price`
   - `sell_target_hit` = `actual_high >= target_sell_price` (`target_sell_price`가 0이면 NULL)
   - `actual_change_rate` = (당일 종가 − 전일 종가) ÷ 전일 종가 × 100.
     전일 종가는 같은 일봉 응답의 직전 거래일 봉에서 얻는다.
4. 실제값과 도달 여부를 UPDATE한다.
5. LLM에 넘겨 종목별 평가문을 받아 `review`에 UPDATE한다 (아래 참고).
   실패하면 여기서 멈추고 수치까지만 남긴다 — 메일은 그대로 나간다.
6. 검증 메일을 발송한다.

### LLM 평가 호출

아침 추천과 같은 모델·같은 `anthropic` 클라이언트를 쓰되, 프롬프트와 스키마는 따로 둔다.
`src/llm/reviewer.py`를 새로 만든다 — `recommender.py`에 섞으면 한 파일이 추천과 사후
평가 둘을 지게 되고, 프롬프트 버전도 서로 다른 주기로 움직인다.

- 입력: 종목별로 `outlook` 원문, 목표 매수가·매도가, 추천 시각 현재가, 실제 고가·저가·
  종가·등락률, 도달 여부.
- 출력 스키마: `{"reviews": [{"ticker": str, "review": str}]}`. 종목당 한두 문장.
- 프롬프트 요지: 전망이 어디까지 맞았고 어디서 어긋났는지를 **주어진 수치만 인용해**
  적는다. 잘 맞았다는 평가든 빗나갔다는 평가든 그대로 적고, 다음 추천에 대한 조언이나
  전략 제안은 하지 않는다.
- `REVIEW_PROMPT_TEMPLATE_VERSION = "v1"`로 시작한다.

### 검증 메일

제목: `[AutoTrade] 2026-09-03 추천 검증 3종목`

본문 (평문만 — 표가 없어 HTML을 함께 만들 이유가 없다. 추천 메일과 같은 꼴이다):

```
2026-09-03 추천 종목의 실제 움직임입니다.

1. 삼성전자(005930)
   추천 시각가: 78,500원 → 종가: 79,800원 (+2.31%)
   당일 고가/저가: 80,200원 / 78,100원
   목표 매수가: 78,000원 — 미도달 (매수 무산)
   목표 매도가: 80,000원 — 도달
   전망: 09:05 현재 +1.80%로 갭 상승 출발해 ...
   평가: 오전 회복 시도는 맞았고 이동평균 82,300원 대신 80,200원에서 멈췄습니다. ...
```

- 실제값을 얻지 못한 종목은 `조회 실패`로 적고 나머지 줄을 뺀다.
- `review`가 비면 평가 줄이 빠진다 — 다른 빈 값과 같은 규약이다.
- 하단 주석: `※ 목표 매수가·매도가와 전망은 참고 수치이며 주문에 사용되지 않습니다.`

---

## 4. 문서

`주식자동매매_PRD.md`를 갱신한다.

- 5.5-B 3단계 표: JSON 필드 목록에 `outlook` 추가.
- 5.5-B에 "오늘 전망은 참고 표시용" 항목 추가 — 목표 매도가와 같은 방침임을 밝힌다.
- 5.11 또는 그 뒤에 "추천 검증 (15:35)" 절 추가 — 테이블, 절차, 별도 메일인 이유
  (최종 리포트의 조기 발송과 충돌하지 않게).
- 일정 표에 15:35 검증 단계 추가.

## 5. 테스트

| 파일 | 내용 |
|---|---|
| `tests/test_llm/test_recommender.py` | `outlook` 파싱, 필드 누락 시 `""`로 통과 |
| `tests/test_llm/test_reviewer.py` (신규) | 평가 응답 파싱, 호출 실패 시 `None` |
| `tests/test_notification/test_templates.py` | 추천 메일 전망 줄(있음/없음), 검증 메일 본문 |
| `tests/test_logger/test_trade_store.py` | UPSERT로 중복 추천이 한 행, 미검증 행 조회, outcome UPDATE |
| `tests/test_core/test_daily_workflow.py` | 검증 단계 — 추천 없는 날 무동작, 일봉 조회 실패, LLM 실패 시 수치까지만 저장하고 메일 발송 |

## 결정 요약

| 항목 | 결정 | 이유 |
|---|---|---|
| 전망 형태 | 서술형 한 필드 (`outlook`) | 필드를 나눠도 문장은 기계 대조가 안 되고, 두 칸을 채우려 같은 말을 반복한다 |
| 전망 내용 | 오전 흐름 + 조건부 분기(가격 포함) | 오전은 추천 시각 데이터로 근거가 서고, 이후는 조건부라야 검증 가능하다 |
| 전망 용도 | 표시·기록 전용 | 문장은 코드가 판단에 쓸 수 없다. 목표 매도가와 같은 방침 |
| 저장소 | SQLite 새 테이블 | 표본을 쌓아 SQL로 집계하는 것이 목적. `TradeStore` 패턴 재사용 |
| 검증 요약 | 수치 저장 + LLM 평가문 | 수치가 원본이고 평가문은 읽기용. 평가 실패가 수치를 막지 않는다 |
| 검증 발송 | 15:35 별도 메일 | 최종 리포트는 전량 매도 시 15:30 이전에 조기 발송될 수 있어, 그 안에 넣으면 그날 검증을 못 본다 |
| 검증 대상 | 추천 메일에 실린 종목 전부 | 사지 못한 종목이 "목표 매수가가 현실적이었나"의 핵심 표본이다 |
