# 항공기 입항 시간 예측 (FlightAware Firehose)

WE · 8M · AS · AA · WS 의 **인천(ICN) 도착편**을 FlightAware Firehose 로 실시간 수집해
도착 예정시각(ETA) 변경 이력과 실제 도착시각을 DB 에 적재하고, 조회 API 로 제공합니다.

인천공항 의전 텔레그램 봇(`src/incheon_bot/`)과는 **완전히 별개인 프로젝트**입니다.
같은 저장소에 있을 뿐 코드·의존성·실행을 공유하지 않습니다.

## 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # FIREHOSE_USERNAME, FIREHOSE_PASSWORD 입력
PYTHONPATH=src python -m eta_ingest ingest    # 수집기
PYTHONPATH=src python -m eta_ingest api       # 조회 API
```

`FIREHOSE_PASSWORD` 에는 계정 비밀번호가 아니라 **Firehose API Key** 를 넣습니다.
인증 정보는 저장소에 커밋하지 마세요 — `.env` 는 이미 `.gitignore` 에 있습니다.

| 변수 | 설명 |
|---|---|
| `ETA_AIRLINES` | 수집 대상 항공사 IATA 코드 (기본 `WE,8M,AS,AA,WS`) |
| `ETA_AIRPORT` | 도착 공항 (기본 `ICN` — 내부적으로 ICAO `RKSI` 로 변환) |
| `ETA_DB_PATH` | SQLite 경로 (기본 `data/eta.db`) |
| `ETA_API_HOST` / `ETA_API_PORT` | 조회 API 바인딩 (기본 `127.0.0.1:8800`) |

## 조회 API

| 경로 | 설명 |
|---|---|
| `GET /arrivals?hours=12` | 앞으로 N시간 내 도착 예정편 (최대 72시간) |
| `GET /flight?ident=THA657` | 편명별 최신 상태 |
| `GET /history?flight_id=...` | 해당 편의 ETA 변경 이력 전체 |
| `GET /health` | 마지막으로 처리한 `pitr` 확인 |

인증이 없으므로 **사내망에만 바인딩**하세요 (기본값이 `127.0.0.1` 인 이유입니다).

## 수집 방식

Firehose 는 HTTP 가 아니라 **TLS 소켓(1501)** 위의 개행 구분 JSON 스트림입니다.
접속 직후 초기화 명령 한 줄을 평문으로 보내고, 그 이후 서버→클라이언트 구간만 압축됩니다.

```
live username <user> password <apikey> useragent bestturn-eta keepalive 60 \
  compression gzip events "flifo arrival departure cancellation position" airport_filter "RKSI"
```

`airport_filter` 는 출발·도착을 모두 통과시키므로 `dest` 로 도착편만 한 번 더 거르고,
항공사는 편명 앞자리로 거릅니다. 같은 항공사가 IATA(`WE501`)와 ICAO(`THA501`) 양쪽으로
들어오므로 `protocol.ICAO_BY_IATA` 로 두 표기를 함께 받습니다.

### 시각 필드가 메시지마다 다른 문제

같은 값을 `flifo` 는 긴 이름으로, `flightplan`/`position` 은 축약형으로 보냅니다.
`protocol.ALIASES` 에서 긴 이름으로 통일합니다.

| 축약형 | 긴 이름 | 뜻 |
|---|---|---|
| `eta` | `estimated_on` | 활주로 도착 예정 |
| `aat` | `actual_on` | 활주로 실제 도착 |
| `edt` / `adt` / `fdt` | `estimated_off` / `actual_off` / `scheduled_off` | 이륙 계열 |

게이트 기준 시각(`estimated_in` / `actual_in`)은 `extendedFlightInfo` 와 `onblock` 에서 옵니다.
`onblock` 은 시각을 `clock` 으로만 보내므로 `actual_in` 으로 옮겨 담습니다.

대표 ETA 는 `protocol.ETA_PRIORITY` 순서로 고릅니다 —
실제 게이트 도착 → Foresight 예측 → 예정 → 활주로 기준. 값이 바뀔 때마다 `eta_history` 에 한 줄씩
쌓이므로, **예측 모델 학습에 쓸 "시간에 따른 ETA 변화 vs 실제 도착시각" 데이터가 그대로 남습니다.**

`predicted_on` / `predicted_in` 은 FlightAware Foresight 계약이 있어야 내려옵니다.
없어도 정상 동작하며 `estimated_*` 로 자동 대체됩니다. 체험 계정에서 실제로 내려오는지 확인이 필요합니다.

### 끊김 복구

모든 메시지에 스트림 위치 토큰 `pitr` 이 붙습니다. 끊기면 `live` 대신 `pitr <마지막값>` 으로
재접속해 누락 없이 이어받습니다. 마지막 `pitr` 은 `stream_state` 테이블에 남으므로
프로세스를 재시작해도 이어집니다. keepalive 의 `pitr` 이 연속 5회 제자리면
바이트가 흐르더라도 멈춘 것으로 보고 재접속합니다.

## 저장 구조

- `flights` — 편별 최신 상태 1행 (예정/예측/실제 시각, 게이트·터미널·수하물 수취대, 결항 여부)
- `eta_history` — 도착 예정시각이 **바뀐 시점만** 기록 (예측 학습용)
- `stream_state` — 재개용 `pitr`

## 테스트

```bash
python -m pytest
```

FlightAware 공개 저장소(`firestarter`, `firehose_examples`)의 실제 메시지 픽스처를 기준으로
필드명·타입을 고정했습니다. 소켓은 가짜 객체로 대체해 네트워크 없이 전부 실행됩니다.

## 확인이 필요한 부분

공식 문서 페이지에 접근하지 못해 1차 자료(FlightAware 공개 코드·실제 캡처)로만 확인했습니다.
운영 투입 전 실제 스트림으로 검증하세요.

- `offblock` / `onblock` 과 구독 이벤트명 `surface_offblock` / `surface_onblock` 의 관계
- `flightplan`·`extendedFlightInfo` 가 `flifo` 와 같은 접속에서 함께 구독 가능한지
- 계정당 동시 접속 수 제한, Foresight(`predicted_*`) 포함 여부
- 알 수 없는 `type` 은 버리지 않고 로그만 남기도록 되어 있으니, 초기 운영 시 로그를 확인하세요
