# AGENTS.md

## 목적

새 에이전트가 작업을 이어받을 때, 코드만으로 파악하기 어려운 운영 맥락/설계 의도/주의점을 전달합니다.

## 빠른 온보딩

- 서비스: FastAPI + 관리 UI + 백그라운드 supervisor
- 감지 소스: SOOP `broad` 폴링 API
- 녹화 파이프라인: `streamlink --stream-url` 해석 -> 내부 HLS relay -> `ffmpeg` 녹화 -> `ffmpeg -c copy` remux
- 저장소: SQLite(`channels`, `settings`, `recordings`) + JSONL 이벤트 로그(`./data/logs/events.jsonl`)

## 코드로 바로 안 보이는 운영 규칙

- `broad` probe 응답이 빈 body면 오류가 아니라 정상 `OFFLINE`이다.
- `PROBE_ERROR`만으로 녹화를 즉시 중단하지 않는다.
- 중복 녹화/세션 식별 키는 `(userId, broadNo)`다.
- 최종 출력 파일명이 충돌하면 remux 단계에서 ` (1)`, ` (2)` 접미사를 붙여 저장하고 기존 파일은 덮어쓰지 않는다.
- 재생 URL은 `https://play.sooplive.co.kr/{userId}` 고정이다.
- 프록시 설정은 환경변수가 아니라 DB(`control_proxy_url`)로만 관리한다.
- 프록시는 `streamlink --stream-url` 해석 1회에만 적용하고, 이후 manifest/key/segment 요청은 direct다.
- 수동 중단된 채널이 같은 `broadNo`로 계속 라이브 상태면 자동 녹화를 재시작하지 않고 `online` 상태를 유지한다(재시도/오프라인/새 방송 번호에서 해제).
- 장시간 작업은 request-context가 아니라 lifespan supervisor에서만 처리한다.
- SQLite 단일 writer 제약 때문에 Uvicorn worker는 1을 유지한다.

## 인증/보안 규칙

- 전역 SOOP 로그인 password만 암호화 저장한다.
- 채널 `stream_password`는 평문 저장한다.
- 전역 password는 암호화 포맷(`enc:v1:`)만 허용한다.
- 평문으로 저장된 과거 전역 password는 자동 호환하지 않는다(재저장 필요).
- 암호화 키는 `APP_SECRET_KEY` 단일 사용이다.

## UI 동작 규칙

- 대시보드는 SSE(`/api/system/stream`)를 구독해 변경 시 자동 새로고침한다.
- 채널 관리 페이지(`/channels`)는 입력 중 초기화를 막기 위해 자동 새로고침하지 않는다.
- 채널 관리 탭(`채널`, `인증`, `프록시`) 상태는 URL hash(`#tab-*`)와 hidden `tab` 필드로 유지한다.
- 웹 UI 재시작 버튼은 프로세스 종료(`os._exit(0)`)를 트리거한다.
- 녹화 중(`active_recorder_count > 0`)에는 `force=1` 확인 없이는 재시작하지 않는다.
- `force=1` 재시작 시에는 진행 중 녹화를 먼저 중단하고 remux 완료를 기다린 뒤 종료한다.

## 보관 정책

- 이벤트 로그(JSONL): 30일 초과 또는 20,000줄 초과 내역 자동 정리
- 녹화 이력(`recordings`): 90일 초과 레코드 DB 정리(실제 녹화 파일은 삭제하지 않음)

## Docker 운영 모델

- 이미지에는 런타임 도구(`git`, `python`, `uv`, `ffmpeg`)만 포함한다.
- 앱 코드는 컨테이너 부팅 시 `/workspace`로 clone/pull 한다.
- 부팅 순서: `ensure_repo` -> `git pull` -> `uv sync` -> `uv run --no-sync python -m app.main`
- `git pull` 실패 시 경고 로그 후 기존 코드로 계속한다.
- `uv sync` 실패 시 경고 로그 후 기존 `.venv`로 계속한다.
- 단, 최초 기동에서 clone/sync가 모두 실패해 실행 가능한 코드/환경이 없으면 기동 실패한다.
- Compose는 `./data -> /workspace/data`만 bind mount한다.
- `/workspace`는 컨테이너 writable layer이므로 컨테이너 재생성 시 코드/.venv는 초기화된다.

## 경로/설정 고정값

- `DB_PATH=./data/app.db`
- `OUTPUT_ROOT_DIR=./data/recordings`
- `TEMP_ROOT_DIR=./data/tmp`
- `COOKIES_DIR=./data/cookies`
- `STREAMLINK_BINARY=streamlink`
- 폴링 간격은 채널별 필드 없이 전역 `POLL_INTERVAL_SEC`만 사용한다.

## DB 마이그레이션 메모

- 앱 기동 시 스키마 정리 단계에서 구형 `settings.updated_at` 구조를 자동 제거한다.
- 앱 기동 시 `idx_recordings_stopped_at` 인덱스를 자동 제거한다.

## 플랫폼/시간대 메모

- `ZoneInfo` 조회 실패 시(`tzdata` 부재 등) `Asia/Seoul`은 KST(UTC+9)로 폴백한다.
- 그 외 timezone 키 실패 시 UTC로 폴백한다.

## 보류 항목(요청 시만 구현)

- remux 안정성 운영 검증
- 관리자 UI 인증 정책 정리
- 수동 poll / 수동 녹화 재시도 버튼
- 디스크 공간 경고

## 문서 운영 규칙

- `README.md`: 외부 사용자 대상 소개/설치/사용법
- `AGENTS.md`: 에이전트 핸드오버용 내부 맥락/주의점
- 코드 변경 시 영향 받는 문서를 즉시 함께 갱신한다.

## 작업 원칙

- 불확실한 항목은 임의 구현하지 말고 사용자에게 확인 후 진행한다.
- 출시 전 단계이므로 하위호환보다 코드 단순성을 우선한다.
- 테스트 코드는 현재 필수 요구사항이 아니며 요청 시에만 추가한다.
