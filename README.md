# soop-autorec

SOOP 채널 라이브를 자동 감지해 `streamlink`로 스트림 URL을 해석하고 `ffmpeg`로 녹화/정리(remux)하는 FastAPI 서비스입니다.

이 프로젝트의 초기 구현과 현재 코드베이스는 OpenAI Codex만 사용해 작성되었습니다.

## 주요 기능

- 채널/인증/프록시 탭형 관리 UI (`/channels`)
- SOOP 방송 상태 폴링 기반 자동 녹화
- 녹화 종료 후 `ffmpeg -c copy` remux
- 녹화 이력/이벤트 로그 조회 API
- 이벤트 로그 JSONL 파일 저장 (`./data/logs/events.jsonl`)
- 웹 UI 상태/이벤트 실시간 갱신 (SSE 기반)
- 웹 UI에서 서버 재시작 요청 지원
- 인증 방식 2종 지원 (`username/password`, `cookies.txt`)
- 채널별 stream password 지원
- 선택적 프록시 지원 (stream URL 해석 1회에만 프록시)

## 요구사항

- Python 3.12+
- `uv`
- `ffmpeg` (PATH 등록 또는 `FFMPEG_BINARY`로 경로 지정)

참고:
- `streamlink`는 Python 의존성으로 포함되어 `uv sync` 시 자동 설치됩니다.
- Windows에서 timezone DB가 없는 경우(`tzdata` 미설치), 기본값 `Asia/Seoul`은 KST(UTC+9) 폴백으로 처리됩니다.

## 빠른 시작 (로컬)

```bash
uv sync --group dev
cp .env.example .env
uv run python -m app.main
```

웹 UI: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)

## 빠른 시작 (Docker 이미지)

### Docker CLI

```bash
docker run -d \
  --name soop-autorec \
  -p 8000:8000 \
  -e APP_SECRET_KEY=change-me \
  -v ./data:/workspace/data \
  ghcr.io/joyfui/soop-autorec:latest
```

### Docker Compose

`docker-compose.yml`에 아래처럼 `image`를 지정한 뒤 실행합니다.

```yaml
services:
  app:
    image: ghcr.io/joyfui/soop-autorec:latest
    container_name: soop-autorec
    environment:
      HOST: 0.0.0.0
      PORT: 8000
      TIMEZONE: Asia/Seoul
      POLL_INTERVAL_SEC: 10
      OFFLINE_CONFIRM_COUNT: 2
      FFMPEG_BINARY: ffmpeg
      APP_SECRET_KEY: ${APP_SECRET_KEY:-}
      BOOTSTRAP_REPO_URL: https://github.com/joyfuI/soop-autorec.git
      BOOTSTRAP_REPO_BRANCH: main
    ports:
      - "8000:8000"
    volumes:
      - ./data:/workspace/data
    restart: unless-stopped
    stop_grace_period: 90s
```

```bash
docker compose up -d
```

- 런타임 데이터 경로: `./data -> /workspace/data`
- 업데이트에 실패하면 기존 상태로 실행을 시도합니다.
- `/workspace`는 컨테이너 내부 writable layer를 사용하므로, 컨테이너 재생성 시 코드/.venv는 초기화됩니다.

## 인증 설정

- UI: `/channels` 상단의 전역 인증 설정 폼
- API: `GET /api/settings/auth`, `PUT /api/settings/auth`

## 프록시 설정

- UI: `/channels`의 `프록시 설정` 폼
- API: `GET /api/settings/proxy`, `PUT /api/settings/proxy`

## output_template 변수

`/channels`에서 채널별 `output_template`에 아래 변수를 사용할 수 있습니다.

- `${displayName}`: 채널 표시 이름(없으면 `user_id`)
- `${userId}`: SOOP `user_id`
- `${title}`: 방송 제목
- `${broadNo}`: 방송 번호
- `${YYMMDD}`: 방송 시작 시각 기준 날짜(`yyMMdd`)
- `${HHmmss}`: 방송 시작 시각 기준 시간(`HHmmss`)

예시:

```text
${displayName}/${YYMMDD} ${title} [${broadNo}].mp4
```

## 운영/자동화 API

JSON API를 통해 UI 없이도 채널/설정/상태를 자동화할 수 있습니다.

- 시스템
  - `GET /api/system/health`
  - `GET /api/system/status`
  - `GET /api/system/stream` (SSE)
- 채널
  - `GET /api/channels`
  - `GET /api/channels/{channel_id}`
  - `POST /api/channels`
  - `PUT /api/channels/{channel_id}`
  - `DELETE /api/channels/{channel_id}`
- 녹화/이벤트 조회
  - `GET /api/recordings?limit=20`
  - `GET /api/events?limit=50`
- 설정
  - `GET /api/settings` (저장된 인증/프록시 설정 조회)
  - `GET /api/settings/auth`
  - `PUT /api/settings/auth`
  - `GET /api/settings/proxy`
  - `PUT /api/settings/proxy`

간단한 사용 예시:

```bash
# 현재 상태 확인
curl http://127.0.0.1:8000/api/system/status

# 채널 목록 조회
curl http://127.0.0.1:8000/api/channels

# 채널 추가
curl -X POST http://127.0.0.1:8000/api/channels \
  -H "Content-Type: application/json" \
  -d '{"user_id":"dlsn9911","display_name":"제갈금자","enabled":true,"preferred_quality":"best"}'
```

## 보관 정책

- 이벤트 로그: `./data/logs/events.jsonl`(JSONL) 기준으로 30일 초과 또는 20,000줄 초과 내역을 자동 정리
- 녹화 이력(`recordings`): 90일 초과 레코드를 DB에서 자동 정리 (실제 녹화 파일은 삭제하지 않음)

## 주요 환경변수

기본값은 `.env.example` 참고.

- `HOST`
- `PORT`
- `TIMEZONE`
- `POLL_INTERVAL_SEC`
- `OFFLINE_CONFIRM_COUNT`
- `FFMPEG_BINARY`
- `APP_SECRET_KEY`

Docker bootstrap 변수(`BOOTSTRAP_REPO_URL`, `BOOTSTRAP_REPO_BRANCH`)는
컨테이너 entrypoint 옵션이며, `docker-compose.yml`에서 기본값이 이미 설정되어 있습니다.

## 개발 검증

```bash
uv run ruff check .
uv run python -m compileall app main.py
```
