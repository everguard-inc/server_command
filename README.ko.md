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

파이프라인은 이름/`eg_pipeline_path`에 따라 분류됩니다. UI **타입** 배지·필터 라벨:

`SYSTEM`, `RTLS`, `DRIFT`, `EG`, `PLC`, `COBBLE`, `DETSEG`, `FORKLIFT`

- **PLC**: Kafka PLC(`plc-engine-kafka`)와 PLC-CV(`sign_monitor`)를 타입에서는 하나로 표시.
  SYS MONITOR 칩에서는 `PLC-KAFKA`, `PLC-CV-RND` 등으로 구분.
- **DRIFT**: Camera-Drift 서비스. SYS MONITOR 칩 라벨은 `CAM-DRIFT`.
- 상태는 `OK` / `WARN` / `ERR`로 표시합니다. EG는 카메라 스트림 feed,
  PLC-CV는 `/stream` + `/checkers`, Kafka는 `/plc` 태그,
  Drift는 `/get_drift`(+ `/api/cameras`)를 사용합니다.
  PLC-CV Signs 칩은 `http://<streaming_ip>:<streaming_port>/checkers`
  (`tags`의 `error` → err), 라이브 ROI는 `/stream`입니다.

`plc_status_url` / `drift_service_url`은 선택입니다. 없으면 `server_ip`와 기본 포트·경로로
만듭니다 (`22000/plc`, `8083/get_drift`). PLC-CV `/checkers`는 `/stream`과 같은
호스트·포트를 씁니다.

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
| `servers_cfg.py` | 공통 헬퍼 및 `servers.json` 로딩 (프록시, sync, CLI) |
| `pipeline_ip.py` | S3 설정에서 엣지/스트림 IP 해석 |
| `sync_servers_json.py` | S3 서버 DB에서 `servers.json` 재생성 |
| `setup_client.py` | 엣지 최초 설정: 의존성, systemd 유닛, git 크리덴셜, sudoers. 잘못된 `~/.git-credentials`만 삭제 |
| `deploy_clients.py` | SSH 배포: git pull/rsync + 재시작; `--deps`는 setup_client 전체(git credentials 포함) |
| `edge_sudoers.py` | sudoers/systemctl 공통 헬퍼 (`deploy_clients`, `setup_client` 공유) |
| `git_versions.py` | git 버전 조회 공통 헬퍼 (엣지·프록시) |
| `rename_systemd_services.py` | `__services__` 기준으로 systemd 유닛을 서버 ID로 리네임 |
| `send_command.py` | CLI: 파이프라인 단위 명령 전송 |
| `send_image_command.py` | CLI: 호스트 그룹 / 파이프라인 단위 명령 전송 |
| `servers.json` | 사이트별 파이프라인 설정 (git 미추적, `servers.json.template` 참고) |
| `servers.json.template` | 스키마 예시만 — **실제 사이트 설정으로 쓰지 말 것** |
| `systemd/server_command_proxy.service.example` | 중앙 프록시용 systemd 유닛 예시 |
| `templates/`, `templates/static/` | 웹 UI (`index.html`, `index.js`, `style.css`) |

경로와 서비스 사용자는 런타임에 결정됩니다(`~` / 현재 사용자). 따라서 한 사이트의 프록시
호스트와 엣지 호스트가 같은 사용자명을 쓰기만 하면, 사용자가 `everguard`든 `eg`든 코드
수정 없이 동작합니다.

## 새 사이트 배포

1. **프록시 호스트 준비**: repo 클론, `pip install -r requirements.txt`, 사설 패키지
   `eg_basics` 설치 확인.
2. 사이트 **`servers.json` 생성** (git 미추적). S3 sync를 권장합니다.
   템플릿은 **가짜 UUID/IP가 들어간 스키마 예시**일 뿐입니다:

   ```bash
   cp servers.json.template servers.json   # 선택: 시작 템플릿
   python3 sync_servers_json.py --xsite-id <사이트_UUID>
   ```

   다른 사이트의 `servers.json`을 복사하지 마세요. sync/수정 후에는 설정을
   다시 읽도록 **프록시를 재시작**해야 합니다.

3. **각 엣지 호스트 설정** (엣지 로그인 사용자로 실행, `sudo` 직접 실행 아님):

   ```bash
   export xSiteId=<사이트_UUID>    # 또는 아래 --site-id 로 전달
   python3 setup_client.py         # 의존성 + systemd 서비스 + git 크리덴셜 + sudoers
   ```

   이전 사용자의 `~/.git-credentials`가 남아 있으면 setup이 해당 파일만 삭제한 뒤
   credentials를 다시 받습니다. 토큰은 **CN(배포 호스트)에서만** 준비하면 됩니다
   (`~/.git-credentials`, `GITHUB_TOKEN`, 또는 `--github-token`). 엣지에는 deploy `--deps` 또는
   3단계 setup이 `~/.git-credentials`를 생성합니다.

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

   `--deps` / `--local --deps`는 원격에서 `setup_client.py` 전체를 실행합니다
   (git credentials + pip + systemd + sudoers + 재시작). sudo 비밀번호가 필요하며
   SSH와 같으면 `SSHPASS`로 충분합니다. 엣지에 credentials가 없으면 CN의
   `~/.git-credentials`(또는 `GITHUB_TOKEN`)에서 토큰을 읽어 각 엣지에
   `~/.git-credentials`를 만듭니다. 엣지에 `~/.eg/github_token`은 필요 없습니다.
   `server_command_client.service`만 재시작합니다 (passwordless sudo).

   `--local`은 원격 `~/server_command`를 로컬 트리와 맞춥니다 (`rsync --delete`).
   `.git`도 포함되어 엣지 Current(git HEAD)가 이 호스트와 같아집니다.
   `servers.json`만 제외됩니다. `rsync`와 `sshpass`가 필요합니다.

   기타 옵션:

   ```bash
   python3 deploy_clients.py --hosts 10.0.0.5     # 특정 호스트만
   python3 deploy_clients.py --dry-run                # 실행 명령만 출력
   python3 deploy_clients.py -j 4                     # 동시 SSH 작업 수 (기본 8)
   ```

   `SSHPASS`는 SSH 비밀번호, `SUDOPASS`는 `--deps` 사용 시 sudo 비밀번호입니다.

5. **프록시 실행** (중앙 호스트만; 엣지는 `setup_client.py`):

   ```bash
   python3 proxy_server.py          # http://<프록시>:5503 에서 UI 제공
   ```

   또는 예시 systemd 유닛 설치 (`User` / 경로 / Python 필요 시 수정):

   ```bash
   sudo cp systemd/server_command_proxy.service.example \
     /etc/systemd/system/server_command_proxy.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now server_command_proxy.service
   ```

   프록시와 엣지 로그인 사용자는 같아야 `servers.json`의 `~/…` 경로가 맞습니다.
   UI/포트(`5502`/`5503`)는 사설망에 두세요 — 인증이 없습니다.

## CLI 사용법

`send_command.py` — 파이프라인 단위 명령:

```bash
python3 send_command.py -c check                       # 기본값; 전체 파이프라인
python3 send_command.py -s SITE-cam1 -c service:restart
python3 send_command.py -s SITE-cam1 -c update
python3 send_command.py -s SITE-cam1 -c update \
  --git-ref system_monitor=abc1234 --git-ref eg_pipeline=v1.2.3
```

허용 명령: `check`, `update`, `stop`, `stream`, `watchdog`,
`service:start`, `service:stop`, `service:restart`, `service:status`.

`update`의 `--git-ref REPO=REF`는 repo별 브랜치/태그/커밋을 지정합니다.
비우면(또는 옵션 생략) 현재처럼 latest(`git pull`)입니다. UI Update 확인창에서도
repo별로 동일하게 지정할 수 있습니다.

`send_image_command.py` — 호스트 그룹 또는 파이프라인 단위 명령 (`-s` / `-d` 동일):

```bash
python3 send_image_command.py -c stream                # 기본값; 전체 파이프라인
python3 send_image_command.py -s SITE-cam1 -c check     # send_command.py -s 와 동일
```

허용 명령: `stream`, `watchdog`, `update`(호스트 그룹); `check`, `stop`(파이프라인 단위).

## 참고

- 엣지 에이전트(`app.py`)는 `proxy_server`를 import하거나 `servers.json`을 읽지
  않습니다. 파이프라인 경로와 서비스명은 `POST /command` JSON으로 전달받습니다.
  공통 상태 헬퍼와 상수는 `servers_cfg.py`에 있으며 repo와 함께 배포됩니다.
- 엣지의 `git`/`docker`는 서비스 사용자의 `$HOME` 아래에서 실행됩니다. GitHub HTTPS 인증은
  **`~/.git-credentials`**(deploy `--deps` 또는 3단계 setup으로 생성)를 사용합니다.
  `~/.eg/github_token`은 수동 설정용 선택 사항입니다.
- `systemctl`은 비밀번호 없는 sudo로 호출됩니다. 3단계 `setup_client.py`로
  `/etc/sudoers.d/server_command`를 설정합니다.
