# AGENTS.md

코딩 에이전트가 변경 시 보존해야 할 제약과 검증 기준입니다. 설치·설정·운영 방법은 [README.md](README.md)에 둡니다. 문서의 설명을 사실로 가정하지 말고 작업 대상 코드와 설정을 대조하세요.

## 탐색 시작점

| 파일 | 책임 / 확인할 내용 |
| --- | --- |
| [app/main.py](app/main.py) | FastAPI lifespan, supervisor 시작·종료, 서버 실행 |
| [app/services/poller.py](app/services/poller.py) | 방송 상태 전이, 녹화 시작 작업, 수동 요청, 시작 복구·보관 정리 예약 |
| [app/services/soop_probe.py](app/services/soop_probe.py) | SOOP `broad` 응답 판정 |
| [app/services/recorder.py](app/services/recorder.py) | 프로세스·세션 관리, remux, 복구, 파일 충돌 방지 |
| [app/services/soop_subscription.py](app/services/soop_subscription.py) | 구독플러스 해석·인증, direct 로그인, 브라우저 헤더, 로컬 HLS 프록시 |
| [app/streamlink_plugins/sooprec.py](app/streamlink_plugins/sooprec.py) | 내장 SOOP 플러그인을 상속한 일반 방송 요청 경로 분리 |
| [app/db.py](app/db.py), [app/models/](app/models/) | 스키마·마이그레이션, 채널·설정·세션 저장, JSONL 이벤트와 보관 정책 |
| [app/schemas/](app/schemas/), [app/routers/](app/routers/), [app/templates/](app/templates/) | API 입력·출력, UI 요청과 표시 동작 |
| [app/services/filename_renderer.py](app/services/filename_renderer.py), [app/utils/](app/utils/) | 파일명 변수, 경로 문자 정리, 시간대 처리 |
| [app/config.py](app/config.py), [.env.example](.env.example) | 런타임 설정과 고정 데이터 경로 |
| [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml), [docker-entrypoint.sh](docker-entrypoint.sh) | 런타임 이미지, 데이터 마운트, 부팅 시 코드·환경 준비 |
| [pyproject.toml](pyproject.toml), [uv.lock](uv.lock) | Python·의존성·개발 도구·ruff 설정 |

SOOP endpoint·인증 단계·timeout·갱신 간격은 해당 서비스 코드가 원본입니다. Docker 버전은 Dockerfile, 마이그레이션 내역은 `app/db.py`에서 확인하며 여기에 값이나 과거 변경 목록을 복제하지 않습니다.

## 보존할 invariant

### 방송 판정과 수동 요청

- 성공한 `broad` 응답의 빈 body는 정상 `OFFLINE`이다. HTTP 오류나 파싱 실패인 `PROBE_ERROR`와 구분한다.
- `PROBE_ERROR`만으로 녹화를 종료하거나 오프라인 확인 횟수를 늘리지 않는다. 오프라인 종료 판단에는 전역 설정의 연속 확인 기준을 적용한다.
- 동일한 오류 내용·상태의 연속 `PROBE_ERROR`는 이벤트 로그에 반복 기록하지 않는다. 복구 또는 내용·상태 변경 시 다시 기록한다.
- 일반 방송의 URL 해석은 무한 대기하지 않는다. timeout·취소 시 자식 프로세스를 정리하고, URL 미확보는 `standby_no_stream`으로 다음 probe에서 재시도한다.
- 구독플러스 자동 건너뛰기는 probe의 힌트를 기준으로 세션 생성·인증·URL 해석 전에 적용한다. 활성 녹화가 없으면 채널은 `online`을 유지하며, 수동 녹화 요청은 이 옵션과 자동 녹화 비활성화를 무시한다.
- 수동 중단한 방송은 같은 방송 번호가 유지되는 동안 자동 재시작하지 않는다. 수동 재시도·오프라인·새 방송에서 제한을 해제한다. 이 제한은 실행 중 메모리 상태이며 영구 설정으로 바꾸지 않는다.

### 녹화 세션과 파일 보존

- 식별자는 `recordings.id`다. 같은 `(user_id, broad_no)`에 재시작된 여러 세션이 존재할 수 있으므로 이 조합에 UNIQUE 제약이나 단일 세션 가정을 도입하지 않는다.
- 새 방송 시작은 이전 녹화 프로세스 종료까지만 기다린다. 이전 remux와 시작 복구는 백그라운드로 계속하며 새 녹화를 막지 않는다. 같은 방송이 remux 중 다시 감지되어도 새 세션을 시작할 수 있다.
- 늦게 끝난 시작 작업이나 이전 세션의 정리 작업이 새 방송·세션의 채널 상태를 덮어쓰지 않도록 현재 방송과 handle을 확인한다.
- 최종 출력 파일은 동시 저장 경쟁에서도 덮어쓰지 않는다. 파일명 후보의 존재 확인만으로 보장할 수 없으므로 최종 설치 단계의 배타적 생성도 유지한다.
- remux 성공 후 파일명 충돌은 완성된 결과물의 설치만 재시도한다. 파일 저장·정리는 이벤트 루프 밖에서 실행하며 취소되더라도 실제 스레드 작업 완료까지 기다린다.
- remux·이동 실패 시 데이터가 남은 임시 파일은 보존하고 `partial` / `temp_path`에 복구 경로를 남긴다. 복구 가능한 원본 녹화 파일은 최종 저장 성공을 확인한 뒤 정리한다.
- 시작 복구는 이전 활성 이력을 중단 처리한 뒤, `interrupted` 이력에 연결된 유효한 경로와 비어 있지 않은 임시 파일만 대상으로 한다. 복구는 순차 백그라운드 작업으로 관리하고 고아·0바이트 파일은 처리하거나 삭제하지 않는다.
- 녹화·remux 중인 채널 삭제를 UI/API 모두에서 거부한다. 녹화 이력의 보관 정리는 활성 이력을 제외하고 DB만 정리하며 실제 녹화 파일을 삭제하지 않는다.

### 인증과 네트워크 경로

- 전역 SOOP 로그인 비밀번호는 암호화 저장하고 조회 시 원문을 노출하지 않는다. 키는 `APP_SECRET_KEY`만 사용하며 과거 평문 값을 자동으로 수용하지 않는다. 형식과 처리는 [app/services/secrets.py](app/services/secrets.py)가 원본이다.
- 채널 `stream_password`는 전역 로그인 비밀번호와 다른 일반 평문 데이터다. 평문 저장과 UI/API 원문 조회·편집을 허용하는 정책을 유지한다.
- 재생 해석용 프록시는 DB 설정으로 관리하고 URL 인증 정보의 예약 문자를 정규화한다. 사용자 Streamlink 설정·sideload 플러그인·HTTP 환경변수가 앱의 해석 경로에 개입하지 않도록 한다.
- 일반 방송은 방송 정보·CDN 할당을 direct로 처리하고 재생 토큰 발급에만 DB 프록시를 사용한다. 해석별 Streamlink 프로세스와 토큰 요청 종료·실패 후 프록시 해제를 유지한다. SOOP 계정 로그인과 CDN 미디어 요청은 일반·구독플러스 모두 direct다.
- 일반 방송의 ffmpeg 입력에도 공통 SOOP 브라우저 헤더를 전달한다. URL과 토큰이 있어도 헤더 없이 재생 가능하다고 가정하지 않는다. CDN 호스트나 화질 표기만으로 실제 해상도를 단정하지 않는다.
- 구독플러스의 probe 힌트는 시청 권한 확인을 대신하지 않는다. 쿠키 우선 사용·direct 로그인 재시도와 권한이 확인된 웹 HLS 대체 경로를 유지하며, 인증 실패를 일반 Streamlink 경로로 우회하지 않는다.
- 로컬 HLS 프록시는 서명 쿠키를 미디어 요청에 붙이고 만료 전에 갱신한다. 쿠키가 응답 JSON에 담기는 경우도 처리하며, playlist 상대 경로와 태그의 `URI` 속성은 upstream playlist URL 기준으로 해석한다.
- Netscape 쿠키 파일의 `#HttpOnly_` 접두사는 주석으로 버리지 않는다.

### 실행 수명과 UI

- 녹화 시작·종료·remux·복구의 수명은 lifespan의 supervisor/recorder가 관리한다. HTTP handler는 요청을 전달하며 장시간 작업을 독립적으로 소유하지 않는다. UI 재시작에서 녹화·remux 정리가 필요할 때도 background task가 supervisor 종료를 기다린다.
- URL 해석·프로세스 시작은 채널별 task와 제한된 동시성으로 처리해 probe 순회를 막지 않는다. 종료 시 시작 task 취소와 자식 프로세스 정리를 보존한다.
- worker는 1개를 유지한다. supervisor·녹화 handle·JSONL 잠금은 프로세스 로컬 상태이며, 여러 worker는 중복 폴링·녹화와 SQLite 쓰기 경합을 유발할 수 있다.
- DB·JSONL·쿠키·파일의 동기 I/O와 잠금 대기는 이벤트 루프 밖에서 처리한다. SQLite 연결은 해당 작업 스레드 안에서 열고 닫으며, 방송·handle·task 상태는 이벤트 루프에서 관리한다. 보관 정리는 supervisor가 추적하고 종료 시 완료를 기다린다.
- 대시보드는 SSE로 갱신하지만 채널 관리 페이지는 입력 중 자동 새로고침하지 않는다. 탭 상태는 URL hash와 폼의 hidden `tab`으로 유지한다.
- 최근 이벤트 캐시는 로그 추가·정리·파일 변경을 반영하고 조회 시 복사본을 반환한다. SSE와 상태 API는 DB 조회 결과를 공유해 중복 읽기를 줄이되 방송 확인 시각과 활성 녹화 수의 변경을 반영한다.
- UI 재시작은 프로세스 종료다. 녹화 중에는 명시적인 강제 확인이 필요하고, 강제 재시작이나 remux만 남은 재시작은 supervisor 정리를 기다린 뒤 종료한다. 활성 녹화 수와 remux 수를 같은 지표로 취급하지 않는다.

## 테스트와 검증

개발 도구 설치는 README의 [Development](README.md#development)를 따른다. 다음 명령은 저장소 루트에서 실행하며 `--no-sync`로 준비된 환경을 사용한다.

```bash
uv run --no-sync ruff check .
uv run --no-sync python -m compileall -q app main.py
uv run --no-sync pytest
```

- 버그 수정이나 외부 동작·상태 전이에 영향을 주는 변경에는 관련된 최소 targeted regression test를 추가하거나 갱신하는 것을 기본으로 한다. 모든 기존 코드에 테스트를 추가하라는 의미는 아니다.
- 특히 방송 응답 파싱, 시작·종료·복구, 파일명 충돌·기존 파일 보호, poller/recorder 동기화 변경은 해당 실패 조건을 재현하는 테스트로 확인한다. 네트워크·프로세스·파일 처리는 가능한 범위에서 격리하며 실제 SOOP 계정이나 운영 데이터를 사용하지 않는다.
- 문서·formatting·명백히 동작을 바꾸지 않는 refactor에는 불필요한 테스트 추가를 강제하지 않는다. 테스트가 현실적으로 어렵다면 이유와 대신 실행한 검증을 기록한다.
- pytest의 테스트 수집 없음은 테스트 통과를 의미하지 않는다. 테스트를 추가한 변경은 `uv run --no-sync pytest <테스트 경로>`로 대상 검증 후 기존 관련 테스트도 실행한다.
- 문서 변경은 `git diff --check`와 링크·코드 블록·명령의 실제 설정 일치 여부를 확인한다. Docker 설정 변경은 `docker compose config --quiet`와 관련 빌드·부팅 경로도 확인한다.
- 검사 실패와 실행하지 못한 검증은 이유를 남긴다. ruff·문법 검사 통과만으로 녹화·복구 동작까지 검증됐다고 보고하지 않는다.

## 작업·문서·커밋 원칙

- 코드 수정과 커밋은 사용자가 요청한 범위에서만 수행한다. 요청 없이 자동 커밋하지 않는다.
- 코드·테스트·문서로 의도를 합리적으로 결정할 수 없는 제품 동작을 임의로 새로 정의하지 않는다. 출시 전 단계에서는 불필요한 하위호환보다 코드 단순성을 우선한다.
- 외부 동작·중요한 invariant·개발/검증 절차가 바뀌면 관련 문서도 함께 갱신한다. 사용법은 README, 변경 제약은 AGENTS에 두며 구현값·완료된 마이그레이션·backlog는 복제하지 않는다.
- 커밋 메시지는 Conventional Commits의 `type: 한국어 요약` 형식으로 작성한다. 영문 타입은 `feat`, `fix`, `refactor`, `style`, `docs`, `chore`, `test` 중 선택하고, 콜론 뒤 공백 1칸과 짧고 구체적인 한국어 요약을 사용한다.
