# soop-autorec

SOOP 채널 라이브를 자동 감지해 `ffmpeg`로 녹화/정리(remux)하는 FastAPI 서비스입니다.

이 프로젝트는 OpenAI Codex로 만들어졌습니다.

관련 잡담은 [https://blog.joyfui.com/1315](https://blog.joyfui.com/1315)

## 주요 기능

- 채널/인증/프록시 탭형 관리 UI (`/channels`)
- SOOP 방송 상태 폴링 기반 자동 녹화
- 녹화 종료 후 tmp에서 `ffmpeg -c copy` remux 후 recordings로 이동
- 강제 종료 후 재시작 시 DB에 연결된 녹화 임시 파일 자동 복구
- 같은 방송 번호가 remux 중 다시 라이브로 감지되면 새 녹화 세션 시작
- 녹화 이력/이벤트 로그 조회 API
- 이벤트 로그 JSONL 파일 저장 (`./data/logs/events.jsonl`)
- 웹 UI 상태/이벤트 실시간 갱신 (SSE 기반)
- 웹 UI에서 서버 재시작 요청 지원
- 인증 방식 2종 지원 (`username/password`, `cookies.txt`)
- 채널별 stream password 지원
- 채널별 구독플러스 자동 녹화 건너뛰기 지원
- 선택적 프록시 지원

## 요구사항

- Python 3.14+
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
      TZ: Asia/Seoul
      POLL_INTERVAL_SEC: 10
      OFFLINE_CONFIRM_COUNT: 6
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
- 최초 clone 전에 bind mount에 남아 있는 빈 `data/.gitkeep` placeholder는 checkout 충돌 방지를 위해 제거됩니다.
- `/workspace`는 컨테이너 내부 writable layer를 사용하므로, 컨테이너 재생성 시 코드/.venv는 초기화됩니다.

## 인증 설정

- UI: `/channels` 상단의 전역 인증 설정 폼
- API: `GET /api/settings/auth`, `PUT /api/settings/auth`
- 브라우저에서 내보낸 `cookies.txt` 또는 `username/password`를 사용할 수 있습니다.
- Netscape 형식 `cookies.txt`의 `#HttpOnly_` 쿠키도 인증 쿠키로 읽습니다.
- Docker에서 `cookies.txt`를 사용할 때는 컨테이너 내부 경로(`/workspace/data/cookies/...`)를 설정해야 합니다.

## 구독플러스 건너뛰기

- `/channels`에서 채널별 `구독플러스 건너뛰기` 옵션을 설정할 수 있습니다.
- 옵션이 켜진 채널은 자동 녹화 중 `broad` 응답의 `subscriptionOnly > 0`이 확인되면 녹화 세션 생성, 로그인, 재생 URL 해석을 시도하지 않고 건너뜁니다.
- 수동 녹화 요청은 이 옵션을 무시하고 구독플러스 녹화를 시도합니다.

## 프록시 설정

- UI: `/channels`의 `프록시 설정` 폼
- API: `GET /api/settings/proxy`, `PUT /api/settings/proxy`
- 프록시 URL의 username/password에 포함된 예약 문자(`&`, `(`, `)`, `@` 등)는 저장 시 percent-encoding으로 정규화됩니다.
- `username/password` 로그인은 프록시 없이 direct로 수행하고, 재생 URL 해석 요청에만 프록시를 적용합니다.

## output_template 변수

`/channels`에서 채널별 `output_template`에 아래 변수를 사용할 수 있습니다.

- `${displayName}`: 채널 표시 이름(없으면 `user_id`)
- `${userId}`: SOOP `user_id`
- `${title}`: 방송 제목
- `${broadNo}`: 방송 번호
- `${YY}`: 방송 시작 시각 기준 연도 2자리(`yy`)
- `${MM}`: 방송 시작 시각 기준 월 2자리(`MM`)
- `${DD}`: 방송 시작 시각 기준 일 2자리(`dd`)
- `${HH}`: 방송 시작 시각 기준 시 2자리(`HH`)
- `${mm}`: 방송 시작 시각 기준 분 2자리(`mm`)
- `${ss}`: 방송 시작 시각 기준 초 2자리(`ss`)
- 최종 파일 경로가 이미 있으면 자동으로 ` (1)`, ` (2)` 접미사를 붙여 다른 파일명으로 저장합니다.
- 여러 remux가 동시에 같은 파일명을 선택해도 기존 파일을 덮어쓰지 않습니다.

예시:

```text
${displayName}/${YY}${MM}${DD} ${title} [${broadNo}].mp4
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

진행 중인 녹화 또는 remux가 있는 채널 삭제 요청은 `409 Conflict`로 거부됩니다.

## 강제 종료 후 자동 복구

- 앱 시작 시 이전 실행에서 `starting`, `recording`, `stopping`, `remuxing` 상태로 남은 녹화 이력을 `interrupted`로 정리합니다.
- 새로 중단 처리했거나 이전 시작에서 이미 `interrupted`였던 이력의 `temp_path`와 `final_path`가 기록되어 있고 임시 파일이 0바이트보다 크면, 순차 백그라운드 작업으로 `ffmpeg -c copy` remux를 다시 시도합니다.
- 복구 성공 시 이력을 `completed`로 변경하고 사용한 임시 파일을 삭제합니다.
- 복구 실패 시 데이터가 남은 파일은 `partial`과 `temp_path`에 보존하고, 복구 가능한 파일이 없으면 `failed`로 기록합니다.
- 자동 복구는 라이브 폴링과 새 녹화 시작을 막지 않습니다. 같은 방송이 계속 중이면 복구 중에도 별도 녹화 세션을 시작할 수 있습니다.
- DB 녹화 이력에 연결되지 않은 파일과 0바이트 파일은 자동으로 처리하거나 삭제하지 않습니다.
- 강제 종료 전에 디스크에 기록된 구간만 복구할 수 있으며, 종료 이후 누락된 구간은 복원할 수 없습니다.

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
- `TZ`
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
