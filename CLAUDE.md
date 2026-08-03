# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

키움증권 REST API 기반 개인용 주식 자동매매 프로그램. 인증·시세·주문·리스크관리·로깅 공통
프레임워크 위에 교체 가능한 전략 모듈을 얹는 구조이며, 첫 번째(현재 유일한) 전략은
**1호 전략**(LLM 기반 급등 예상 대형주 매수)이다.

**`주식자동매매_PRD.md`가 이 프로젝트의 유일한 설계 문서(단일 소스)다.** 자금 배분 비율,
익절/손절 기준, 매도 방식 같은 동작 규칙을 바꿀 때는 PRD를 먼저 갱신하고 코드를 맞춘다 —
코드와 PRD가 어긋나 있으면 어느 쪽이 최신인지 먼저 확인한다.

**별도 `docs/` 설계 문서(spec/plan)는 이 프로젝트에서 쓰지 않는다.** 기능을 설계할 때도
별도 spec 파일을 새로 만들지 말고 PRD.md의 해당 절을 직접 갱신한다 (superpowers
브레인스토밍 스킬을 쓰더라도 설계 문서 저장 단계는 건너뛰고 PRD.md에 반영한다).

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

## 아키텍처

### 스레드 / 이벤트 루프 구조 (가장 헷갈리기 쉬운 부분)
- **엔진은 단독 실행하지 않는다.** `scripts/run_ui.py` → `MainWindow`(PyQt6)가 유일한
  진입점이고, "▶ 시작" 버튼을 눌러야 `EngineThread`(QThread)가 뜬다. 창을 닫으면 엔진도
  함께 정지한다.
- `EngineThread`가 자신만의 asyncio 이벤트 루프를 새로 만들어 소유하고, 그 위에서
  `src/core/runtime.py`의 `TimeScheduler`(시간 기반 08:40/08:45/09:00/15:20/15:30)와
  `WebSocketClient` 콜백(실시간 시세 기반)이 함께 돈다.
- 데이터 수집·LLM 호출·메일 발송처럼 오래 걸리는 동기 작업은 `runtime._off_loop`로 별도
  스레드에 넘긴다 — 안 그러면 그 시간 동안 WebSocket PING에 응답하지 못해 서버가 연결을
  끊는다.
- 반대로 매수/청산 주문은 **루프 스레드에서 그대로** 실행해, 실시간 익절/손절 감시와
  같은 종목을 동시에 건드리는 경쟁 상태가 생기지 않게 직렬화한다.
- UI의 "즉시 실행" 버튼(①~④)은 스케줄러가 호출하는 것과 **동일한 함수**를 그 자리에서
  호출한다 — 별도 코드 경로가 아니다.

### 전략 프레임워크
- `src/strategy/base.py`의 `BaseStrategy`(`generate_signal(MarketData) -> Signal`)가 실시간
  시세 기반 전략의 공통 인터페이스다.
- 1호 전략(`src/strategy/llm_momentum.py`의 `LLMMomentumStrategy`)은 시간 기반 전략이라
  `generate_signal`은 항상 `HOLD`만 반환한다. 실제 진입은 `DailyWorkflow`가 08:45/09:00
  스케줄에서 `set_recommendations` → `build_buy_plans`를 직접 호출해 트리거한다. 청산은
  `RiskManager.check_exit`(실시간 시세 콜백)와 15:20 강제청산이 담당한다.
- 새 전략을 추가할 때는 `BaseStrategy`를 구현하는 새 모듈만 추가하면 되고, 나머지
  (주문 실행/리스크/로깅)는 그대로 재사용된다 — 단, 시간 기반 전략이라면 1호 전략처럼
  `DailyWorkflow`류의 오케스트레이션을 별도로 붙여야 한다.

### 설정은 `.env` 하나로 통일
- `config/settings.py`의 `Settings` dataclass가 `os.getenv` 기본값 조합으로 모든 설정을
  담는다. 별도 JSON/YAML 설정 파일은 쓰지 않는다.
- UI가 다루는 값은 `src/ui/env_store.py`(`load_env`/`save_env`)로 `.env`를 직접 파싱해
  읽고 쓴다 — `python-dotenv`는 파일 쓰기를 지원하지 않아 자체 구현한 것.
- 퍼센트 단위 설정(`TAKE_PROFIT_PERCENT` 등)은 `_percent` 필드(원값, `.env`에 저장)와
  `_ratio` 프로퍼티(0~1 환산, 계산에 사용) 쌍으로 두는 패턴을 따른다 — 새 설정을 추가할
  때도 이 패턴을 따른다.
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
- 익절/손절(`check_exit`)은 키움 REST가 스탑오더(조건부 예약주문)를 지원하지 않아, 이
  프로그램이 떠 있는 동안의 실시간 시세 감시가 **1차이자 사실상 유일한 청산 수단**이다
  — 앱이 꺼지거나 WebSocket이 끊기면 그 사이 손절도 멈춘다.

### 이메일이 유일한 알림 채널
텔레그램은 검토 후 제거됐다. 운영 알림(`AlertNotifier`)과 `notification/templates.py`의
추천 결과·매수 실행 결과·일일/월간 리포트 이메일이 전부 `EmailNotifier`(SMTP) 하나를
거쳐 나간다.

## 문서

**`주식자동매매_PRD.md`**가 요구사항과 확정된 설계 결정의 유일한 설계 문서다. 새 기능을
설계할 때도 별도 문서를 만들지 말고 이 문서를 갱신하는 것으로 끝낸다 (`docs/` 폴더는
쓰지 않는다 — 2026-08-03에 정리됨).
