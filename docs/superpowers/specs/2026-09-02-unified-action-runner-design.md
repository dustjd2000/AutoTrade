# 실행 통로 통합과 매수 결과 메일 조기 발송

작성일: 2026-09-02

## 배경

두 가지 요구가 같은 자리에서 만난다.

1. **자동 수행도 버튼 클릭과 같은 로직으로 돌아야 한다.** 지금은 스케줄러가
   `workflow` 메서드를 직접 부르고(`src/core/runtime.py`의 `build_runtime`),
   버튼은 `EngineThread.run_action` → `manual_steps`를 탄다. 결국 같은 메서드에
   닿지만 감싸는 껍데기가 달라, 두 경로가 서로의 실행 여부를 모른다. 09:07에
   ① 버튼을 눌러 LLM 호출이 도는 중에 09:08 매수가 발동하면 추천이 덜 끝난 채
   매수가 돌 수 있고, 자동 실행 중에도 버튼이 열려 있어 사용자가 겹쳐 누를 수 있다.
2. **매수예정 목록의 매수가 끝나면 바로 결과 메일이 나가야 한다.** 지금은
   10:10 `cancel_unfilled_buys`가 마무리하며 보낸다. 2026-08-26에 주문 지정가를
   허용 밴드 상단으로 올린 뒤로 접수 직후 전량 체결되는 날이 대부분인데,
   메일만 한 시간 늦게 나간다.

2번은 한 번 시도했다가 되돌렸다(cb93b2b → 057ba1d). 그때는 **잔고 대조**로
"전부 체결"을 판정했고, 2026-09-01 실매매에서 09:06:00에 오판해 살아 있던
(161390) 주문 0092505를 1시간 4분 일찍 취소했다. 잔고는 "이 종목을 들고 있는가"에
답할 뿐 "내 주문이 체결됐는가"에 답하지 못한다. 되돌린 커밋은 재시도 조건을
명시해 두었다 — **판정을 체결내역 조회(주문번호 대조)로 세울 것.**

## 목표

- 스케줄 실행과 버튼 실행이 하나의 실행 통로를 공유한다. 잠금·스레드 배분·로그·
  UI 표시가 양쪽에 똑같이 붙는다.
- 충돌하면 **순차 대기**한다. 스케줄 작업이 조용히 빠지는 일이 없어야 한다.
- 접수한 매수가 전부 체결되면 10:10을 기다리지 않고 그 시점에 결과 메일을 보낸다.
  판정 근거는 체결내역 조회의 주문번호 대조다.

## 비목표

- 단계 정의를 통일하지 않는다. 버튼 ⑤와 15:35 스케줄이 서로 다른 함수를 부르는 것은
  의도된 설계이며(아래 참고) 그대로 둔다.
- 익절/손절, 매수 가격 산정, 메일 본문 형식은 건드리지 않는다.

---

## 1. 실행 통로 통합

### 1.1 새 모듈 `src/core/actions.py`

지금 `runtime.py`에 있는 `ManualStep` / `MANUAL_ACTIONS` / `ORDER_ACTIONS` /
`CONFIRM_ACTIONS` / `manual_steps`와, `EngineThread`가 들고 있던 잠금·스레드 배분을
한 모듈로 모은다. `runtime.py`는 기존에 내보내던 이름을 그대로 다시 내보내
`src/ui/main_window.py`와 기존 테스트의 import를 건드리지 않는다.

### 1.2 `ActionRunner`

루프 태스크 하나가 큐를 소비한다.

- `submit(action, tickers=()) -> bool` — 접수하면 True.
  **같은 액션이 이미 큐에 있거나 실행 중이면 넣지 않고 False**를 돌려준다.
  큐는 중복이 없으므로 길이가 액션 종류 수를 넘지 않는다.
- 실행 중이면 뒤에 붙어 순차 대기한다. FIFO다.
- 단계별 스레드 배분은 현행 `EngineThread._run_action`의 규칙 그대로다 —
  `touches_orders=True`인 단계는 루프 스레드에서 직접 실행해 실시간 익절·손절
  콜백과 직렬화하고, 아니면 executor로 넘겨 WebSocket PING을 막지 않는다.
  이 배분이 `_off_loop`를 대체하므로 스케줄 등록에서 `_off_loop`는 빠진다.
- 생명주기를 콜백으로 알린다: `on_started(action)`,
  `on_finished(action, ok, message)`. Qt를 모른다 — `EngineThread`가 이 콜백을
  받아 기존 `action_started` / `action_finished` 시그널로 옮긴다.
- `wait_idle(timeout)` — 큐가 비고 실행 중인 것이 없을 때까지 기다린다.

한 단계가 예외를 던지면 그 액션은 실패로 끝나고(`on_finished(ok=False)`),
러너는 다음 큐 항목으로 넘어간다. 러너 태스크 자체는 죽지 않는다.

### 1.3 스케줄러 등록

`build_runtime`의 등록 표가 액션 키를 쓴다.

| 시각 | 액션 키 |
|---|---|
| `DAILY_RESET_TIME` (08:40) | `daily_reset` |
| `settings.recommend_time` | `recommend` |
| `settings.buy_time` | `buy` |
| `CANCEL_UNFILLED_TIME` (10:10) | `cancel_unfilled` |
| `FORCE_CLOSE_TIME` (15:15) | `close_out` |
| `REPORT_TIME` (15:35) | `daily_report` |

각 잡은 `_trading_days_only(lambda: runner.submit(key), name)`이다. 거래일 가드는
스케줄 쪽에만 남고, 버튼은 지금처럼 거래일과 무관하게 눌린다.

### 1.4 스케줄 전용 액션

버튼에 없는 세 가지를 별도 dict `SCHEDULED_ACTIONS`에 둔다. `MANUAL_ACTIONS`에는
넣지 않으므로 버튼 그리드에 나타나지 않는다.

- `daily_reset` → `engine.reset_for_new_day`. 주문을 내지는 않지만 현행 스케줄러가
  루프 스레드에서 그대로 돌리므로 `touches_orders=True`로 두어 동작을 보존한다
  (즉시 끝나는 상태 초기화라 루프를 막지 않는다).
- `close_out` → 현행 `runtime.close_out`이 만드는 묶음:
  미체결 매수 취소(실패해도 계속) → `force_close_all_positions(reason="day_end")`.
  순서를 뒤집으면 청산 뒤에 살아 있는 매수가 체결되어 오버나이트 포지션이 남는다.
  `touches_orders=True`.
- `daily_report` → `workflow.send_final_report` (하루 한 번, 발송 표시로 거름)

**버튼 ⑤ `report`는 종전대로 `send_daily_report`다** — 사용자가 직접 누른 것이므로
발송 표시와 무관하게 항상 보낸다. 15:35 스케줄과 함수가 갈린 것은 의도된 설계이며
이번 통합에서 바꾸지 않는다.

`manual_steps(runtime, action, tickers)`는 두 dict를 모두 풀 수 있게 넓힌다.
모르는 키는 지금처럼 `ValueError`다.

### 1.5 UI 변화

`MainWindow._on_action_started`가 상태바 문구만 바꾸던 것에서 **버튼 잠금까지**
맡는다(`_set_actions_enabled(False)`). 지금은 버튼 경로에서만 `_run_action`이
잠갔기 때문에, 스케줄 실행 중에는 버튼이 열려 있었다.

결과적으로 09:05 추천이 도는 동안 ①~⑤ 버튼이 잠기고 상태바에 "즉시 실행 중"이
뜬다. **이것이 사용자 눈에 보이는 유일한 동작 변화다.**

`MANUAL_ACTIONS`에 없는 스케줄 전용 키가 시그널로 올라오므로, 라벨 조회는 현행
`MANUAL_ACTIONS.get(action, action)` 대신 두 dict를 합쳐 본다.

### 1.6 정지 처리

`EngineThread.stop`의 `_await_action`이 단일 future 대신
`runner.wait_idle(timeout=ACTION_WAIT_SECONDS)`를 기다린다. 기다리지 못하면
현행처럼 경고를 남기고 정지를 계속한다.

---

## 2. 매수 결과 메일 조기 발송

### 2.1 `DailyWorkflow.buy_orders_filled(today=None) -> bool`

접수한 매수 주문이 남김없이 전량 체결됐는지 — 결과 메일을 앞당길 근거다.

1. `_read_buy_records(today)`가 None이면 False. **여기서 API를 부르지 않는다.**
   메일이 나가면 기록 파일이 지워지므로, 발송 뒤에는 이 단계에서 걸려 하루 종일
   체결 조회가 도는 일이 없다.
2. `outcome.is_ordered`이고 주문번호가 있는 기록이 하나도 없으면 False.
3. `engine.order_client.get_today_fills()`를 부른다. 위에서 모은 주문번호가
   **전부** 매수 체결로 잡히고 `filled_quantity > 0`이며 `unfilled_quantity == 0`
   이어야 True.
4. 조회가 예외를 던지면 로그만 남기고 False — 10:10에 맡긴다.

조회 결과에 흔적조차 없는 주문은 조건이 서지 않아 그대로 기다린다.
체결내역 TR(ka10076)이 한 주도 체결되지 않은 대기 주문을 싣는지는 확인되지
않았는데(`_cancel_targets` 주석 참고), 싣지 않더라도 이 판정은 안전한 쪽으로
틀린다 — "모르면 기다린다"가 되기 때문이다.

잔고를 보지 않으므로 이월 보유 종목이나 다른 인스턴스가 산 물량에 속지 않는다.
057ba1d가 지목한 결함이 여기서 사라진다.

`_fill_buy_prices`와 **같은 조회를 같은 방식으로** 읽으므로, 조건이 서면
`cancel_unfilled_buys` 안에서 체결가가 그대로 채워진다. 체결이 잔고에는 잡혔지만
체결내역에는 아직 안 잡힌 시차를 흡수하려던 `BUY_RESULT_SETTLE_SECONDS` 같은
유예 시간은 필요 없다 — 판정과 발송이 같은 소스를 본다.

### 2.2 `watch_buy_result` (runtime.py 복원)

60초 주기로 `workflow.buy_orders_filled()`를 확인하고, True면
`runner.submit("cancel_unfilled")`.

`cancel_unfilled_buys`를 직접 부르지 않고 큐에 넣는 것이 핵심이다. 09:08 매수가
아직 도는 중이면 자연히 뒤에서 기다리고, 실시간 익절·손절 감시와도 직렬화된다.
이미 큐에 있으면 `submit`이 False를 돌려주므로 중복 발송이 생기지 않는다.

이 시점에는 취소할 주문이 없다(전부 체결이 조건이므로). 하는 일은 체결가를 채우고
결과 메일을 보내고 기록 파일을 지우는 것이다. 기록이 지워지므로 10:10 스케줄과
15:15 마감 정리가 같은 메일을 다시 보내지 않는다 — 현행과 같은 방식이다.

`runtime.run`이 `watch_closeout_report` / `watch_cash_refresh`와 나란히 태스크로
띄우고, 종료 시 함께 취소한다.

### 2.3 API 호출 비용

09:08부터 전량 체결이 확인될 때까지만 60초에 한 번 조회한다. 대개 몇 회로 끝나고,
전부 체결되지 않는 날에도 10:10까지 최대 60여 회다. 되돌린 구현이 잔고 대조를 쓴
이유가 이 호출을 피하려던 것이었는데, 정확도를 사는 대가로 받아들인다.

---

## 3. 테스트

- `tests/test_core/test_actions.py` (신규)
  - 큐가 순차로 실행된다 (앞 액션이 끝나야 다음이 시작된다).
  - 이미 큐에 있는 액션은 `submit`이 False를 돌려주고 두 번 실행되지 않는다.
  - `touches_orders=True`인 단계는 루프 스레드에서, False인 단계는 executor에서 돈다.
  - 한 액션이 예외로 실패해도 러너가 살아남아 다음 액션을 실행한다.
  - 스케줄 전용 키 3개가 각각 올바른 단계로 풀린다.
  - `close_out` 단계에서 미체결 취소가 실패해도 청산이 실행된다 (현행 보장 유지).
- `tests/test_core/test_buy_result_timing.py` (복원·개작)
  - 전량 체결이면 True.
  - 부분체결·미체결이 남으면 False.
  - 접수한 주문이 체결내역에 없으면 False.
  - 조회가 예외를 던지면 False.
  - 기록 파일이 없으면 False이고 **체결 조회를 부르지 않는다.**
- 기존 `tests/test_core/test_manual_actions.py`는 `MANUAL_ACTIONS`만 순회하므로
  그대로 통과해야 한다.

## 4. 문서

`주식자동매매_PRD.md`를 갱신한다.

- 5.5-B 6단계: 결과 메일 발송 시점에 "접수분 전량 체결 시 조기 발송"을 추가한다.
- 5.11: 조기 발송 조건과 판정 근거를 적는다.
- 10절: 조기 발송 재도입 경위를 남긴다 — 2026-09-01에 잔고 대조로 오판해 되돌렸고,
  판정을 체결내역 주문번호 대조로 바꿔 다시 넣었다는 것.
- 실행 통로 통합은 내부 구조 변경이라 동작 규칙을 바꾸지 않는다. 다만 자동 실행 중
  버튼이 잠기는 것은 사용자에게 보이므로 5.10(UI)에 한 줄 적는다.

`CLAUDE.md`의 "UI의 '즉시 실행' 버튼(①~④)은 스케줄러가 호출하는 것과 동일한 함수를
그 자리에서 호출한다"는 서술도 통합된 구조에 맞게 고친다.

## 5. 건드리는 파일

| 파일 | 내용 |
|---|---|
| `src/core/actions.py` | 신규 — `ManualStep`, 액션 dict, `manual_steps`, `ActionRunner` |
| `src/core/runtime.py` | 스케줄 등록을 액션 키로, `watch_buy_result` 복원, 이름 재수출 |
| `src/core/daily_workflow.py` | `buy_orders_filled` 추가 |
| `src/ui/engine_thread.py` | 잠금·실행을 `ActionRunner`에 위임, `stop`이 `wait_idle` |
| `src/ui/main_window.py` | `_on_action_started`에서 버튼 잠금, 라벨 조회 확장 |
| `tests/test_core/test_actions.py` | 신규 |
| `tests/test_core/test_buy_result_timing.py` | 복원·개작 |
| `주식자동매매_PRD.md` | 5.5-B, 5.10, 5.11, 10절 |
| `CLAUDE.md` | 실행 통로 서술 갱신 |
