# Server Command

Everguard 엣지 사이트를 위한 Docker Image Manager입니다. 중앙 **프록시**가 각 호스트에서
동작하는 **엣지 에이전트**로부터 상태를 수집하고 명령을 전달하며, 파이프라인 모니터링·제어용
웹 UI를 제공합니다.

영문 버전: [README.md](README.md)

## 구성

```
                         +---------------------------+
   브라우저  <---HTTP---->  proxy_server.py (:5503)  |   중앙 호스트
                         |   - 웹 UI + 상태 API       |
                         |   - 상태 수집기            |
                         +-------------+--------------+
                                       |  POST /command (HTTP)
                 +---------------------+---------------------+
                 |                     |                     |
        +--------v-------+   +---------v------+   +----------v-----+
        | app.py (:5502) |   | app.py (:5502) |   | app.py (:5502) |   엣지 호스트
        | 엣지 에이전트  |   | 엣지 에이전트  |   | 엣지 에이전트  |
        +----------------+   +----------------+   +----------------+
```

- **프록시** (`proxy_server.py`, 포트 `5503`): UI 제공, 각 엣지 호스트의 파이프라인 상태를
  주기적으로 수집, 서비스/업데이트 명령 전달.
- **엣지 에이전트** (`app.py`, 포트 `5502`): 각 호스트에서 systemd 서비스
  (`server_command_client.service`)로 단독 실행. 파이프라인 제어(start/stop/restart),
  장치 상태 probe(카메라 / qlight / 스피커), 메모리 사용량 보고, git-pull + docker-build
  업데이트를 수행. `proxy_server`와 `servers.json`은 사용하지 않으며, 공통 헬퍼는
  `servers_cfg.py`를 import합니다.
- **`servers.json`**: 사이트별 설정(사이트 ID, probe 튜닝, 파이프라인 목록).
  `sync_servers_json.py`가 S3 서버 DB로부터 생성. **git 미추적** (`.gitignore`).

## 파이프라인 종류

파이프라인은 이름/설정에 따라 다음으로 분류됩니다: `SYSTEM`(시스템 모니터), `RTLS`,
`EG`(기본 카메라 파이프라인), `COBBLE`, `DETSEG`, `FORKLIFT`. 상태는 종류별로
`OK` / `WARN` / `ERR`로 계산됩니다(장치 개수, 스트림 상태, 컨테이너/서비스 상태).

## 요구 사항

Python 의존성 설치:

```bash
pip install -r requirements.txt
```

- **엣지 호스트**: `flask` + `httpx`만 필요 (`app.py`는 `eg_basics`를 선택적으로 import).
- **프록시 / CLI 호스트**: 추가로 `requests`(`requirements.txt`에 포함)와 사설 패키지
  `eg_basics`(S3 설정 로딩, 비밀번호 복호화)가 필요합니다.

## 파일 구성

| 파일 | 역할 |
|------|------|
| `proxy_server.py` | 중앙 프록시: 웹 UI, 상태 수집, 명령 전달 (포트 5503) |
| `app.py` | 엣지 에이전트: 파이프라인 제어 + 상태 probe (포트 5502) |
| `servers_cfg.py` | 공통 헬퍼 및 `servers.json` 로딩 (프록시/sync/CLI; 엣지 `app.py`는 import만, JSON 미로딩) |
| `git_versions.py` | Git 버전 조회 공통 헬퍼 (프록시 backfill, 엣지 agent) |
| `pipeline_ip.py` | S3 설정에서 엣지/스트림 IP 해석 |
| `sync_servers_json.py` | S3 서버 DB에서 `servers.json` 재생성 |
| `setup_client.py` | 엣지 최초 설정: 의존성, systemd 유닛, git 크리덴셜, sudoers. 잘못된 `~/.git-credentials`만 삭제 |
| `deploy_clients.py` | SSH 배포 (git pull/rsync; `--deps`: setup_client; 평소: sync + service restart) |
| `edge_sudoers.py` | sudoers/systemctl 공통 헬퍼 (`deploy_clients`, `setup_client` 공유) |
| `rename_systemd_services.py` | `__services__` 기준으로 systemd 유닛을 서버 ID로 리네임 |
| `send_command.py` | CLI: 파이프라인 단위 명령 전송 |
| `send_image_command.py` | CLI: 호스트 그룹 / 파이프라인 단위 명령 전송 |
| `servers.json` | 사이트별 파이프라인 설정 (git 미추적, `servers.json.template` 참고) |
| `templates/`, `templates/static/` | 웹 UI (`index.html`, `index.js`, `style.css`) |

경로와 서비스 사용자는 런타임에 결정됩니다(`~` / 현재 사용자). 따라서 한 사이트의 프록시
호스트와 엣지 호스트가 같은 사용자명을 쓰기만 하면, 사용자가 `everguard`든 `eg`든 코드
수정 없이 동작합니다.

## 새 사이트 배포

1. **프록시 호스트 준비**: repo 클론, `pip install -r requirements.txt`, 사설 패키지
   `eg_basics` 설치 확인.
2. 사이트 **`servers.json` 생성** (git 미추적; `servers.json.template` 참고):

   ```bash
   cp servers.json.template servers.json   # 선택: 시작 템플릿
   python3 sync_servers_json.py --xsite-id <사이트_UUID>
   ```

3. **각 엣지 호스트 설정** (엣지 로그인 사용자로 실행, `sudo` 직접 실행 아님):

   ```bash
   export xSiteId=<사이트_UUID>    # 또는 아래 --site-id 로 전달
   python3 setup_client.py         # 의존성 + systemd 서비스 + git 크리덴셜 + sudoers
   ```

   이전 사용자의 `~/.git-credentials`가 남아 있으면 setup이 해당 파일만 삭제한 뒤
   credentials를 다시 받습니다.
   GitHub 토큰은 `~/.eg/github_token`, `GITHUB_TOKEN`, 또는 `--github-token`으로 전달할 수
   있습니다.

4. 프록시 호스트에서 전체 엣지로 **코드 배포**:

   완전 새 엣지 호스트는 위 3단계 `setup_client.py`를 먼저 실행합니다. 그다음
   프록시에서:

   **git pull**

   ```bash
   python3 deploy_clients.py --deps              # 처음 (코드 + setup + 재시작)
   python3 deploy_clients.py                     # 평소 (코드 + 재시작)
   ```

   **rsync (`--local`)**

   ```bash
   python3 deploy_clients.py --local --deps      # 처음 (코드 + setup + 재시작)
   python3 deploy_clients.py --local             # 평소 (코드 + 재시작)
   ```

   `--deps` / `--local --deps`는 원격에서 `setup_client.py --skip-git`을 실행합니다
   (pip 의존성 + systemd 유닛 + sudoers + 서비스 재시작). sudo 비밀번호가 필요하며
   SSH와 같으면 `SSHPASS`로 충분합니다. 평소 모드는 코드 동기화 후
   `server_command_client.service`만 재시작합니다 (passwordless sudo).

   `--local`은 원격 `~/server_command`를 로컬 트리와 맞춥니다 (`rsync --delete`).
   `.git`과 `servers.json`은 제외됩니다. `rsync`와 `sshpass`가 필요합니다.

   기타 옵션:

   ```bash
   python3 deploy_clients.py --hosts 10.0.0.5     # 특정 호스트만
   python3 deploy_clients.py --dry-run                # 실행 명령만 출력
   python3 deploy_clients.py -j 4                     # 동시 SSH 작업 수 (기본 8)
   ```

   `SSHPASS`는 SSH 비밀번호, `SUDOPASS`는 `--deps` 사용 시 sudo 비밀번호입니다.

5. **프록시 실행**:

   ```bash
   python3 proxy_server.py          # http://<프록시>:5503 에서 UI 제공
   ```

## CLI 사용법

`send_command.py` — 파이프라인 단위 명령:

```bash
python3 send_command.py -c check                       # 기본값; 전체 파이프라인
python3 send_command.py -s RND-3090 -c service:restart
python3 send_command.py -s RND-3090 -c update
```

허용 명령: `check`, `update`, `stop`, `stream`, `watchdog`,
`service:start`, `service:stop`, `service:restart`, `service:status`.

`send_image_command.py` — 호스트 그룹 또는 파이프라인 단위 명령 (`-s` / 숨김 `-d`):

```bash
python3 send_image_command.py -c stream                # 기본값; 전체 파이프라인
python3 send_image_command.py -s RND-3090 -c check
```

허용 명령: `stream`, `watchdog`, `update`(호스트 그룹); `check`, `stop`(파이프라인 단위).

## 참고

- 엣지 에이전트(`app.py`)는 `proxy_server`를 import하거나 `servers.json`을 읽지
  않습니다. 파이프라인 경로와 서비스명은 `POST /command` JSON으로 전달받습니다.
  공통 상태 헬퍼와 상수는 `servers_cfg.py`에 있으며 repo와 함께 배포됩니다.
- 엣지의 `git`/`docker`는 서비스 사용자의 `$HOME` 아래에서 실행됩니다. HTTPS GitHub 인증은
  `~/.git-credentials`, `~/.eg/github_token`, 또는 `GITHUB_TOKEN` 환경변수를 사용합니다.
- `systemctl`은 비밀번호 없는 sudo로 호출됩니다. 3단계 `setup_client.py`로
  `/etc/sudoers.d/server_command`를 설정합니다.
