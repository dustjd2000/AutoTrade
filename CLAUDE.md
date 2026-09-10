# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

키움증권 REST API 기반 개인용 주식 자동매매 프로그램. 인증·시세·주문·리스크관리·로깅 공통
프레임워크 위에 교체 가능한 전략 모듈을 얹는 구조이며, 첫 번째(현재 유일한) 전략은
**1호 전략**(LLM 기반 급등 예상 대형주 매수)이다.

**`주식자동매매_PRD.md`가 이 프로젝트 동작 규칙의 단일 소스다.** 자금 배분 비율,
익절/손절 기준, 매도 방식 같은 동작 규칙을 바꿀 때는 PRD를 먼저 갱신하고 코드를 맞춘다 —
코드와 PRD가 어긋나 있으면 어느 쪽이 최신인지 먼저 확인한다.

## 명령어

```bash
pip install -r requirements.txt
cp .env.example .env        # 값을 채운다 (.env는 커밋 금지)

python scripts/run_ui.py    # 유일한 진입점 — 설정 UI + 엔진 시작/정지 제어 (PyQt6)

pytest                                                   # 전체 테스트
pytest tests/test_strategy/test_llm_momentum.py          # 파일 단위
pytest tests/test_strategy/test_llm_momentum.py -k name  # 테스트 단위 (-k 패턴 매칭)
```

- Windows에서는 `AutoTrade.bat`(콘솔창 없이 `pythonw.exe`로 실행)를 쓴다 — `.venv`/`venv`
  폴더가 있으면 그 인터프리터를, 없으면 시스템 PATH의 `python`을 쓴다.
- lint/format 도구나 `pytest.ini`/`pyproject.toml` 설정은 없다 — pytest는 기본 discovery로
  `tests/` 아래를 그대로 찾는다.
- `scripts/check_balance.py`, `scripts/check_fills.py`: 키움 API 응답 구조(잔고 TR, 체결
  내역)를 진단하는 조회 전용 스크립트 — 주문을 내지 않는다.
- **엔진이 떠 있는 동안에는 이 스크립트들을 돌리지 않는다.** 키움은 앱키당 토큰을 하나만
  유지해서, 별도 프로세스가 토큰을 발급하면 실행 중인 엔진의 토큰이 즉시 무효화된다.
  2026-08-06에 이걸로 손절 청산이 22분간 거부됐다 (PRD 10절 "토큰 무효화와 자동 재발급").

## 아키텍처

### 스레드 / 이벤트 루프 구조 (가장 헷갈리기 쉬운 부분)
- **엔진은 단독 실행하지 않는다.** `scripts/run_ui.py` → `MainWindow`(PyQt6)가 유일한
  진입점이고, "▶ 시작" 버튼을 눌러야 `EngineThread`(QThread)가 뜬다. 창을 닫으면 엔진도
  함께 정지한다.
- `EngineThread`가 자신만의 asyncio 이벤트 루프를 새로 만들어 소유하고, 그 위에서
  `src/core/runtime.py`의 `TimeScheduler`(시간 기반 08:40/추천 시각/매수 시각/10:10/15:15/
  15:35 — 15:35에는 리포트 → 추천 검증 → 프롬프트 자동 수정 세 단계가 등록 순서대로 돈다)와
  `WebSocketClient` 콜백(실시간 시세 기반)이 함께 돈다. 이 중 **추천 시각과 매수 시각만 설정값**이고
  (`settings.recommend_time`/`buy_time`, `.env`의 `RECOMMEND_TIME`·`BUY_TIME`, 기본 09:05·09:08)
  나머지는 코드 상수다. 두 값은 UI에서 고를 수 없고 라벨로 보여주기만 하며, "추천은 개장 후,
  매수는 추천 +3분 이상"을 `Settings.validate()`가 엔진 시작 단계에서 강제한다.
- 데이터 수집·LLM 호출·메일 발송처럼 오래 걸리는 동기 작업은 별도 스레드에 넘긴다 — 안
  그러면 그 시간 동안 WebSocket PING에 응답하지 못해 서버가 연결을 끊는다.
- 반대로 매수/청산 주문은 **루프 스레드에서 그대로** 실행해, 실시간 익절/손절 감시와
  같은 종목을 동시에 건드리는 경쟁 상태가 생기지 않게 직렬화한다.
- 스케줄 실행과 UI의 "즉시 실행" 버튼(①~⑤)은 **같은 실행 큐**(`src/core/actions.py`의
  `ActionRunner`)를 탄다 — 별도 코드 경로가 아니다. 큐는 중복 없는 FIFO라, 실행이 겹치면
  거부하지 않고 순서대로 기다린다. 단계별로 `touches_orders`를 보고 루프 스레드/별도
  스레드를 정한다. 단, 버튼 ⑤와 15:35 스케줄은 **다른 함수**를 부른다 — 버튼은
  `send_daily_report`(항상 발송), 스케줄은 `send_final_report`(하루 한 번).

### 전략 프레임워크
- `src/strategy/base.py`의 `BaseStrategy`(`generate_signal(MarketData) -> Signal`)가 실시간
  시세 기반 전략의 공통 인터페이스다.
- 데이터 수집은 **전일 일봉 한 번이 전부**다 (`src/data/collector.py`). 대형주 약 98종목에
  `ka10086`을 한 번씩 돌려 전일 종가·고가·저가·등락률·거래량과 거래량 급증 배수를 뽑고,
  급증 배수 상위 25종목만 LLM에 넘긴다. **전 종목 스캔은 당일 지표(등락률·시가갭)를 쓰지
  않는다** — 09:00 이전에는 키움이 그 값을 주지 않는 것이 실측으로 확인됐다(PRD 10절 "장 전
  당일 지표 부재"). 응답에 당일 봉이 섞여 오므로 날짜로 걸러내야 장 전·장중 결과가 같아진다.
  급증 배수 상위 40종목에는 `ka10001`로 당일 현재가·등락률·거래량을 추가로 조회해 갭 하락
  배제와 LLM 프롬프트(오늘 전망 포함)에 쓴다.
- 1호 전략(`src/strategy/llm_momentum.py`의 `LLMMomentumStrategy`)은 시간 기반 전략이라
  `generate_signal`은 항상 `HOLD`만 반환한다. 실제 진입은 `DailyWorkflow`가 추천 시각/09:08
  스케줄에서 `set_recommendations` → `build_buy_plans`를 직접 호출해 트리거한다. 매수는
  LLM이 함께 제시한 **목표 매수가로 지정가** 주문이고, 10:10에 미체결분을 취소하면서
  매수 결과 메일을 보낸다(`cancel_unfilled_buys`). 청산은 `RiskManager.check_portfolio_exit`
  (실시간 시세 콜백, **보유 종목 합산** 순손익 ±설정값 기준 익절/손절)와 15:15 강제청산이
  담당한다. 15:35에는 일일 리포트 다음으로 `DailyWorkflow.review_recommendations`가 그날
  추천 종목의 목표가 도달 여부와 실제 고가·저가·종가를 대조해 `data/trades.db`의
  `recommendations` 테이블에 남기고 별도 메일로 보낸다 — 주문에는 쓰이지 않는 사후 기록이다
  (PRD 5.12절). 그 **다음**으로 `DailyWorkflow.tune_prompt`가 최근 10거래일의 검증 결과를
  버전별로 묶어 LLM에 넘기고, 추천 프롬프트의 판단·서술 지침 다섯 절을 **사람 승인 없이**
  고친다 (PRD 5.13절). 고친 날만 메일이 나가고, 고치지 않는 날이 정상이다.
- 새 전략을 추가할 때는 `BaseStrategy`를 구현하는 새 모듈만 추가하면 되고, 나머지
  (주문 실행/리스크/로깅)는 그대로 재사용된다 — 단, 시간 기반 전략이라면 1호 전략처럼
  `DailyWorkflow`류의 오케스트레이션을 별도로 붙여야 한다.

### 설정은 `.env` 하나로 통일
- `config/settings.py`의 `Settings` dataclass가 `os.getenv` 기본값 조합으로 모든 설정을
  담는다. 별도 JSON/YAML 설정 파일은 쓰지 않는다.
- **예외가 하나 있다: `data/prompt/`.** 추천 프롬프트의 판단·서술 지침 다섯 절이 여기 파일로
  있고, 15:35 자동 수정 에이전트가 덧쓴다 (PRD 5.13절). 사용자가 정하는 **설정이 아니라
  에이전트가 갱신하는 런타임 상태**라 `.env`가 아니라 `data/` 아래에 둔다. 매 추천마다 파일을
  읽으므로 **엔진 재시작 없이** 다음 추천에 반영되고, 파일이 없으면 코드의
  `DEFAULT_PROMPT_SECTIONS`로 폴백한다 — `/data/`는 gitignore 대상이라 코드 쪽이 정본이다.
- UI가 다루는 값은 `src/ui/env_store.py`(`load_env`/`save_env`)로 `.env`를 직접 파싱해
  읽고 쓴다 — `python-dotenv`는 파일 쓰기를 지원하지 않아 자체 구현한 것.
- 엔진은 **시작 시점에 읽은 `Settings`를 그대로 들고 돈다.** 그래서 "설정 저장" 시 값이
  실제로 바뀌었으면 UI가 엔진을 자동으로 재시작한다(`MainWindow._needs_restart_for_changed_settings`
  → `_restart_engine`) — 저장 완료 팝업을 먼저 띄우고 그 뒤에 재시작하며, 보유 종목이 있으면
  감시 공백을 알리고 확인을 받는다.
- 퍼센트 단위 설정(`STOP_LOSS_PERCENT` 등)은 `_percent` 필드(원값, `.env`에 저장)와
  `_ratio` 프로퍼티(0~1 환산, 계산에 사용) 쌍으로 두는 패턴을 따른다 — 새 설정을 추가할
  때도 이 패턴을 따른다. 시각 설정(`RECOMMEND_TIME`·`BUY_TIME`)도 같은 꼴로 `_hhmm`
  필드(`"HH:MM"` 원값)와 프로퍼티(`datetime.time` 환산) 쌍이며, 환산은 `_parse_hhmm`이 맡는다.
- `mode`(`paper`/`live`)에 따라 `api_base_url`/`websocket_url`이 자동 분기된다. 실전
  전환은 `.env`에 `LIVE_TRADE_CONFIRMED=YES_I_UNDERSTAND`가 없으면 `Settings.validate()`가
  막는다.

### 리스크 관리는 이중 구조
- **전략 레벨**: 각 전략이 애초에 예수금의 일부만 쓰도록 스스로 설계됨 (예: 1호 전략의
  투입 비율 × 추천 종목 수 배분).
- **시스템 레벨**: `RiskManager`가 일일 손실 한도·총노출 비중을 전략과 무관하게 강제한다
  — 전략 로직 버그로 과도하게 매수되는 경우를 걸러내는 이중 안전장치다. 종목당 한도
  (`calc_buy_quantity`/`max_position_ratio`)는 실시간 시세 기반 신호 경로
  (`generate_signal` → `Signal.BUY`)에서만 쓰이며, `generate_signal`이 항상 `HOLD`인
  1호 전략에는 적용되지 않는다.
- 손절(`check_portfolio_exit`)은 키움 REST가 스탑오더(조건부 예약주문)를 지원하지 않아,
  이 프로그램이 떠 있는 동안의 실시간 시세 감시가 **1차이자 사실상 유일한 하방 청산 수단**이다
  — 앱이 꺼지거나 WebSocket이 끊기면 그 사이 손절도 멈춘다.
- 손절 판정은 가격 변동률이 아니라 **순손익률**(수수료·세금·슬리피지를 뺀 값) 기준이다.
  `STOP_LOSS_PERCENT`를 UI에서 조정하며, `.env.example`의 값과 사용자의 실제 `.env` 값이
  다를 수 있으니 동작을 따질 때는 **`.env`를 직접 확인한다**.
- **익절 자동 청산은 없다** (2026-09-09에 걷어냄). 단순익절(종목별 0% 익절)도 함께 사라졌다.
  근거는 PRD 10절의 2026-08-12 분석이다 — 실매매 27건을 당일 고가와 대조해 익절선
  0%·0.5%·1%·2%·2.3%·3%·4% **어느 값도 "익절 없음"보다 못했다**. 고정 선이 상방을 자르는 것이
  문제였으므로, 상황을 보고 정하는 판단으로 대체했다.
- **그 자리를 AI 매도 판단이 대신한다** (`runtime.watch_ai_exit` → `src/llm/exit_advisor.py`).
  보유 종목이 있을 때만, 거래일에, 매수 시각 +15분 ~ 15:00 사이에 설정 주기(15/30/60분,
  `AI_EXIT_INTERVAL_MINUTES`)마다 보유 종목 전체를 LLM에 보여 주고 **지금 전량 정리할지**를
  묻는다. AI는 매도 여부만 내고 **가격은 내지 않는다** — 실행은 기존 `_execute_portfolio_exit`
  경로의 전량 시장가이며 `exit_reason`에 `ai_judgment`로 남는다.
  - **손절이 항상 먼저다.** 실시간 시세 콜백에서 도므로 AI를 기다리지 않는다. AI는 손절선에
    닿기 전 구간에서만 청산을 앞당긴다.
  - **`TAKE_PROFIT_PERCENT`는 남아 있지만 자동 청산에 쓰이지 않는다.** AI에게 "이 정도면 만족"
    이라는 기준선으로만 넘어가고, 손절과 입력란을 계속 공유한다(2026-08-21 통합 유지).
  - **실패는 언제나 "팔지 않음"으로 떨어진다.** LLM 호출 실패·타임아웃·형식 오류 전부. 프롬프트도
    "확실한 근거가 없으면 보유"를 못 박는다 — 주기마다 물으면 매도 쪽으로 기울기 쉽다.
  - LLM 호출은 별도 스레드, 매도 주문은 루프 스레드다. 응답을 기다리는 동안(최대 120초) 손절이
    먼저 정리했을 수 있어 매도 직전에 보유 종목을 다시 읽는다.
  - **AI가 유일한 이익 실현 수단이다** — 호출이 하루 종일 실패하면 +10%까지 간 포지션도 15:15
    강제청산까지 간다. 상세와 나머지 위험은 PRD 5.5-B "AI 매도 판단" 참고.
  - **이익 반납 감시** (`src/core/exit_drawdown.py` → `TradingEngine._track_exit_drawdown`).
    **실시간 시세 콜백에서** 종목별 당일 순손익 고점을 따라가고, 고점 대비
    `AI_EXIT_DRAWDOWN_PERCENT`(%p, 기본 1, `.env` 전용) 이상 반납하면 호출 주기를 기다리지 않고
    AI 판단을 앞당긴다. 고점은 15분 주기 사이를 스쳐 지나가므로 궤적(`ExitTrace`)만으로는 잡히지
    않는다 — 2026-09-10 HD현대중공업이 고점 +4.46%에서 +1.41%까지 반납하고 본전으로 끝난 것이
    계기다. **판정만 종목별이고 팔 때는 그대로 전량이며, 여기서 팔지는 않는다** — 손절과 달리
    AI를 부를 시점만 앞당긴다 (PRD 5.5-B "이익 반납 감시").
  - **장중 공시도 함께 본다** (`runtime.watch_disclosures` → `src/core/disclosure_watch.py`).
    보유 종목의 당일 DART 공시를 5분마다 받아, 처음 조회 시점을 기준선으로 삼아 **그 뒤에
    새로 나타난 제목**을 `[신규]`로 프롬프트에 싣는다 — `rcept_dt`가 날짜 단위라 시각으로는
    고를 수 없다. 새 공시가 `BLOCKING_KEYWORDS`에 걸리면 호출 주기와 하루 상한을 건너뛰고
    AI 판단을 앞당기지만, **곧바로 팔지는 않는다** — 파는 단위가 보유 목록 전체라 키워드
    하나로 결정할 일이 아니다 (PRD 5.5-B "장중 공시").
- **기본 상태는 AI 매도 판단 + 손절이다** (2026-09-09에 바꿈). 다만 그 기본값이 쓰이는 것은
  `.env`에 값이 없을 때뿐이다 — 체크박스 둘 다 **켜고 끄는 즉시** `.env`
  (`AI_EXIT_ENABLED`/`STOP_LOSS_ENABLED`, 1=켬)에 저장되고, 엔진이 새로 뜰 때 `Settings`가 그
  값을 읽어 `TradingEngine.ai_exit_enabled`/`RiskManager.stop_loss_enabled`로 넣는다. 즉 **재시작
  해도 마지막 상태가 이어지고**, 되돌리려면 체크박스를 다시 켜야 한다 (2026-09-09에 저장 추가 —
  종전에는 재시작만으로 화면과 엔진의 상태가 갈렸다). **저장하되 재시작시키지는 않는다**: 두 키는
  `_save_settings`가 아니라 토글 핸들러가 직접 쓰므로 재시작 판정 대상이 아니고, 돌고 있는 엔진에는
  `set_exit_flags`로 바로 반영된다 — 손절을 끄려는 순간에 재시작으로 감시 공백이 생기면 안 된다.
  호출 주기는 반대로 `_save_settings` 경로라 바꾸면 엔진이 재시작된다.
- **판정 단위는 종목이 아니라 보유 목록 전체다** (2026-08-10에 종목별에서 바꿈). 손절도 AI 판단도
  보유 종목을 **전량** 매도한다. 종목별 청산은 없으므로, 한 종목이 크게 무너져도 다른 종목이
  상쇄하면 15:15 강제청산까지 간다 — 의도된 동작이다 (PRD 5.5-B).

### 이메일이 유일한 알림 채널
텔레그램은 검토 후 제거됐다. 운영 알림(`AlertNotifier`)과 `notification/templates.py`의
추천 결과·매수 실행 결과·일일/월간 리포트 이메일이 전부 `EmailNotifier`(SMTP) 하나를
거쳐 나간다.

## 문서

**`주식자동매매_PRD.md`**가 요구사항과 확정된 설계 결정을 기록하는 문서다. 동작 규칙이
바뀌면 이 문서를 갱신한다.
