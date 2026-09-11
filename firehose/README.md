# 항공기 입항 시간 예측 (FlightAware Firehose)

WE · 8M · AS · AA · WS 의 **인천(ICN) 도착편**을 FlightAware Firehose 로 실시간 수집해
도착 예정시각(ETA) 변경 이력과 실제 도착시각을 DB 에 적재하고, 조회 API 로 제공합니다.

인천공항 의전 텔레그램 봇(`src/incheon_bot/`)과는 **완전히 별개인 프로젝트**입니다.
같은 저장소에 있을 뿐 코드·의존성·실행을 공유하지 않습니다.
