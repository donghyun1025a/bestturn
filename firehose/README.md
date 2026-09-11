# 항공기 입항 시간 예측 (FlightAware Firehose)

WE · 8M · AS · AA · WS 의 **인천(ICN) 도착편**을 FlightAware Firehose 로 실시간 수집해
도착 예정시각(ETA) 변경 이력과 실제 도착시각을 DB 에 적재하고, 조회 API 로 제공합니다.

인천공항 의전 텔레그램 봇(`src/incheon_bot/`)과는 **완전히 별개인 프로젝트**입니다.
같은 저장소에 있을 뿐 코드·의존성·실행을 공유하지 않습니다.

## 실행

### 윈도우

먼저 [Python](https://www.python.org/downloads/) 설치 시 **"Add python.exe to PATH"** 를 체크하고,
[Git](https://git-scm.com/download/win) 을 설치합니다. 그다음 명령 프롬프트에서:

```bat
cd %USERPROFILE%
git clone -b claude/sharp-ritchie-7ejt2a https://github.com/donghyun1025a/bestturn.git
cd bestturn\firehose
start.bat
```

`start.bat` 은 가상환경 생성·의존성 설치·대시보드 실행까지 알아서 합니다.
다음부터는 `start.bat` 을 더블클릭만 하면 됩니다.

### 맥 · 리눅스

```bash
git clone -b claude/sharp-ritchie-7ejt2a https://github.com/donghyun1025a/bestturn.git
cd bestturn/firehose
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

브라우저가 열리면 **Firehose 사용자명과 API Key 만 넣고 「저장하고 수집 시작」**을 누르면 됩니다.
나머지(수집 시작·DB 적재·조회·공공데이터 대조)는 모두 화면에서 합니다.

- `FIREHOSE_PASSWORD` 자리에는 계정 비밀번호가 아니라 **Firehose API Key** 를 넣습니다.
- 입력값은 DB 파일(`app_config` 테이블, 권한 0600)에만 저장되고 화면으로 다시 내려오지 않습니다.
- 공공데이터 서비스키는 **대조 탭에서만** 씁니다. 비워 두면 나머지 기능은 그대로 동작합니다.

### 화면 구성

- **도착 예정편** — 편명·예정·ETA·지연·게이트·수취대. 행을 누르면 그 편의 ETA 변경 이력이 뜹니다.
- **공공데이터 대조** — 같은 편의 Firehose ETA 와 인천공항 공공데이터 예상시각을 나란히 놓고 분 단위 차이를 보여줍니다.
  기재 등록번호로 먼저 맞추고, 없으면 편명으로 맞춥니다. 대조 근거를 행마다 표시합니다.
- 상태 배지에 접속 실패 사유가 그대로 뜹니다 (잘못된 API Key, 방화벽 차단 등).

UI 없이 수집만 돌리려면 `python run.py ingest` 로 실행하고 자격증명을 환경변수로 넘깁니다 (서버 상주용).

| 변수 | 설명 |
|---|---|
| `ETA_AIRLINES` | 수집 대상 항공사 IATA 코드 (기본 `WE,8M,AS,AA,WS`) |
| `ETA_AIRPORT` | 도착 공항 (기본 `ICN` — 내부적으로 ICAO `RKSI` 로 변환) |
| `ETA_DB_PATH` | SQLite 경로. 기본값은 실행 위치와 무관하게 `firehose/data/eta.db` 입니다 |
| `ETA_API_HOST` / `ETA_API_PORT` | 대시보드 바인딩 (기본 `127.0.0.1:8800`) |

## HTTP API

UI 가 쓰는 엔드포인트이며 다른 사내 시스템에서도 그대로 호출할 수 있습니다.

| 경로 | 설명 |
|---|---|
| `GET /api/arrivals?hours=12` | 앞으로 N시간 내 도착 예정편 (최대 72시간) |
| `GET /api/flight?ident=THA657` | 편명별 최신 상태 |
| `GET /api/history?flight_id=...` | 해당 편의 ETA 변경 이력 전체 |
| `GET /api/compare?hours=12&date=YYYYMMDD` | 공공데이터와 대조 |
| `GET /api/status` | 수집 상태·접속 오류·마지막 `pitr` |
| `POST /api/ingest/start` · `/api/ingest/stop` | 수집 제어 |

**인증이 없습니다.** 자격증명을 다루므로 기본값대로 `127.0.0.1` 에만 바인딩하세요.
DNS 리바인딩과 다른 사이트에서 오는 요청을 막으려고 `Host`·`Origin` 헤더를 검사하고,
POST 는 `application/json` 만 받습니다. 외부에 열어야 한다면 앞단에 인증을 두세요.

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
없어도 정상 동작하며 `estimated_*` 로 자동 대체됩니다.
**체험 계정에서 실제로 내려오는 것을 확인했습니다** — 대표 ETA 가 `predicted_in` 으로 잡힙니다.

## 접속이 안 될 때

```bat
python run.py doctor
```

DNS → TCP → TLS 순으로 짚어 어디서 막히는지 알려줍니다. 흔한 두 가지는 이렇습니다.

**`1501 포트가 방화벽에 막혀 있습니다`** — Firehose 는 웹(443)이 아니라 1501 포트를 씁니다.
사내 방화벽에서 기본 차단되는 경우가 많습니다. 전산팀에 `206.253.80.0/21` 대역의
`1501/TCP` 아웃바운드 허용을 요청하세요.

**`TLS 인증서 검증 실패`** — 원인이 둘이라 `doctor` 가 구분해 줍니다.

- 윈도우의 파이썬은 시스템 루트 저장소를 온전히 읽지 못해 멀쩡한 사이트도 검증에 실패합니다.
  그래서 `certifi` 번들을 함께 신뢰하도록 해 두었습니다 (대부분 이걸로 해결됩니다).
- 사내 방화벽이 통신을 가로채 자체 인증서를 제시하는 환경이라면, 전산팀에서 받은
  사내 루트 CA 파일을 `.env` 의 `FIREHOSE_CA_BUNDLE` 에 지정하세요.

인증서 검증을 끄는 선택지는 두지 않았습니다. 그 경로로는 Firehose API Key 가 그대로 노출됩니다.

### 접속 타임아웃

`firehose.flightaware.com` 은 A 레코드가 8개입니다. 파이썬 기본 동작(`socket.create_connection`)은
주소마다 타임아웃을 처음부터 다시 쓰기 때문에, 방화벽에 막히면 8배 시간 동안 아무 표시 없이 멈춥니다.
그래서 주소 목록 전체에 15초 마감시간을 걸고, 실패한 주소를 문구에 담아 UI 로 올립니다.

### 끊김 복구

모든 메시지에 스트림 위치 토큰 `pitr` 이 붙습니다. 끊기면 `live` 대신 `pitr <마지막값>` 으로
재접속해 누락 없이 이어받습니다. 마지막 `pitr` 은 `stream_state` 테이블에 남으므로
프로세스를 재시작해도 이어집니다. keepalive 의 `pitr` 이 연속 5회 제자리면
바이트가 흐르더라도 멈춘 것으로 보고 재접속합니다.

## 공공데이터 대조

인천공항 도착 정보(`getFltArrivalsDeOdp`)를 불러와 같은 편끼리 맞춥니다.
공공데이터 시각은 KST 문자열(`YYYYMMDDHHMM`), Firehose 는 POSIX epoch 이라 epoch 으로 통일해 비교합니다.

편명 표기가 서로 달라서(Firehose `THA657` ↔ 공공데이터 `WE 0657`) 매칭은 두 단계입니다.

1. **기재 등록번호** — `HS-TBA` ↔ `HSTBA`. 가장 확실합니다.
2. **편명** — ICAO 콜사인을 IATA 로 되돌려 후보를 만들고, 코드셰어 마스터 편명까지 봅니다.

맞추지 못한 편도 버리지 않고 `대조 근거: 매칭 실패` 로 표시합니다.

## 저장 구조

수집한 데이터는 전부 **`firehose/data/eta.db`** 파일 하나에 들어갑니다 (SQLite).
윈도우에서 `start.bat` 으로 실행했다면 `C:\Users\<사용자>\bestturn\firehose\data\eta.db` 입니다.
서버가 아니라 이 PC 안에만 있으므로, 백업은 이 파일을 복사하면 끝입니다.
`.gitignore` 에 있어 저장소에 올라가지 않습니다 (자격증명이 들어 있습니다).


- `flights` — 편별 최신 상태 1행 (예정/예측/실제 시각, 게이트·터미널·수하물 수취대, 결항 여부)
- `eta_history` — 도착 예정시각이 **바뀐 시점만** 기록 (예측 학습용)
- `stream_state` — 재개용 `pitr`
- `app_config` — UI 에서 입력한 자격증명·대상 항공사 (권한 0600)

## 테스트

```bash
python -m pytest
```

FlightAware 공개 저장소(`firestarter`, `firehose_examples`)의 실제 메시지 픽스처를 기준으로
필드명·타입을 고정했습니다. 소켓은 가짜 객체로 대체해 네트워크 없이 전부 실행됩니다.

## DB 내용을 직접 보려면

`eta.db` 는 SQLite 파일이라 메모장으로 열면 깨져 보입니다. 정상입니다.
[DB Browser for SQLite](https://sqlitebrowser.org/) 같은 도구로 열거나, 파이썬으로 봅니다.

```bash
python - <<'PY'
import sqlite3, datetime
conn = sqlite3.connect("data/eta.db"); conn.row_factory = sqlite3.Row
kst = datetime.timezone(datetime.timedelta(hours=9))
for r in conn.execute("SELECT ident, orig, eta, eta_source FROM flights ORDER BY eta"):
    t = datetime.datetime.fromtimestamp(r["eta"], kst) if r["eta"] else None
    print(r["ident"], r["orig"], t, r["eta_source"])
PY
```

### 화면은 비었는데 DB 에는 쌓여 있다면

「도착 예정편」은 **현재 시각 기준** 창만 보여줍니다. 받은 편이 모두 그 밖이면
표 대신 실제로 받은 도착시각 범위를 알려줍니다. 이 범위가 과거로 나오면
Firehose 계정이 실시간이 아니라 **과거 데이터를 재생(replay)** 하고 있는 것입니다.
체험 계정에서 실제로 관측된 적이 있으니, FlightAware 에 계약 범위를 확인하세요.

## 확인이 필요한 부분

공식 문서 페이지에 접근하지 못해 1차 자료(FlightAware 공개 코드·실제 캡처)로만 확인했습니다.
운영 투입 전 실제 스트림으로 검증하세요.

- `offblock` / `onblock` 과 구독 이벤트명 `surface_offblock` / `surface_onblock` 의 관계
- `flightplan`·`extendedFlightInfo` 가 `flifo` 와 같은 접속에서 함께 구독 가능한지
- 계정당 동시 접속 수 제한, Foresight(`predicted_*`) 포함 여부
- 알 수 없는 `type` 은 버리지 않고 로그만 남기도록 되어 있으니, 초기 운영 시 로그를 확인하세요
