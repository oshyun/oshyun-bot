# AGENT.md — oshyun-bot 개발 가이드

> 이 파일은 **개발용**이다. oshyun-bot 을 수정할 때 Claude Code 가 읽는 문서.
> **봇 런타임 claude 는 이 파일을 읽지 않는다** — 봇은 Obsidian 볼트(`AGENT_WORKDIR`)를 cwd 로
> 실행되고 샌드박스가 이 리포 접근을 막는다. 따라서 런타임 지침은 claude 가 cwd(볼트)에서 자동
> 로드하는 볼트의 `AGENT.md`(및 하위 폴더별 `AGENT.md`)에 둔다.

## ⛔ 절대 원칙 1순위 — 워크트리에서만 작업한다 (예외 없음)

**`main`(원본 마스터)을 절대 직접 수정하지 않는다.** 이 규칙은 다른 모든 규칙·편의보다 우선한다.

모든 코드/문서 변경은 **예외 없이** 다음 흐름을 따른다:

1. **작업 시작 = 워크트리 생성.** 새 브랜치를 만들어 `git worktree` 에서 작업한다. `main` 작업 트리는 건드리지 않는다.
   **워크트리 경로는 반드시 브랜치명을 붙여 고유하게 짓는다** — `oshyun-bot-wt` 같은 고정 경로를 재사용하면 다른 세션이 같은 경로에서 작업 중일 때 충돌·유실이 난다.
   ```bash
   BR=<브랜치명>
   WT=/Users/user/repos/oshyun-bot-wt-$(echo "$BR" | tr '/' '-')
   test -e "$WT" && { echo "경로 이미 존재 — 다른 세션 작업일 수 있으니 중단"; exit 1; }
   git worktree add -b "$BR" "$WT" HEAD
   # 워크트리($WT)에서 편집·커밋
   ```
   **절대 워크트리 생성 전에 `rm -rf` 나 `git worktree prune` 으로 기존 경로를 밀지 않는다** — 다른 세션 작업을 통째로 날릴 수 있다. 경로가 이미 있으면 멈추고 확인한다.
2. **코딩 작업이 끝나면 merge 전에 반드시 `/simplify` 를 워크트리 안에서 실행**하고 그 정리 항목을 적용한다. 이는 **모든 코딩 작업 완료 시 자동**이다 — 사용자가 따로 요청하지 않아도 호출한다. `/simplify` 자신도 코딩 작업이므로 당연히 워크트리에서 돈다. (순수 문서/설정만 바뀐 작업은 `/simplify` 가 무의미하므로 생략 가능.)
3. 그 브랜치를 **`main` 에 merge** 한다.
   ```bash
   git -C /Users/user/repos/oshyun-bot merge "$BR"
   git worktree remove "$WT"   # 정리 — 내가 만든 그 경로만 지정해서 제거
   ```
   정리는 **내가 만든 워크트리 경로만** `remove` 한다. `worktree list` 의 다른 경로는 다른 세션 소유이므로 건드리지 않는다.
4. merge 후 `main` 체크아웃에서 `./scripts/restart.sh` 로 재시작해 반영한다.
5. 반영 및 동작 확인 후 작업 내용을 **GitHub 원격 저장소에 푸시(`git push origin main`)**하여 동기화한다.

**어떤 핑계로도 예외를 두지 않는다** — "간단한 한 줄 수정", "이미 하던 작업 마무리", "방금 만든 변경만 커밋", "문서/설정 파일이라서", "급해서" 전부 안 된다. `main` 에서 편집 도구를 쓰는 순간 규칙 위반이다.

**Why:** 봇은 `main` 체크아웃에서 실행된다. `main` 을 항상 동작하는 깨끗한 상태로 유지해야 진행 중 변경이 실행 중인 봇을 깨뜨리지 않는다. 격리된 워크트리에서 작업하고 검증이 끝난 것만 merge → restart 한다.

## 무엇인가

oshyun-bot = "Personal Intelligent Bot". 텔레그램 메시지를 받아 로컬 PC 의 `claude -p` 로
실행하고 결과를 회신하는 봇. 롱폴링 방식(공개 URL/웹훅 불필요). 목적은 사용자의 Obsidian
볼트 마크다운 노트를 읽고 답하는 개인 비서.

코드(`src/`)와 운영 산출물(`data/`)을 분리한다. 코드는 서로 import 하지 않는 세 컴포넌트로
나뉜다. `.env`·`.venv`·`scripts/`·`requirements.txt` 는 리포 루트에서 공유한다.

**`src/bot/`** — 통합 봇 백엔드 & 웹/오케스트레이터 서버 (Telegram · Web API · AGY/Claude 엔진 · `instructions/` 지침 허브)
- `telegram_bot.py` — 텔레그램 핸들러 + 롱폴링 루프 + 응답 전송 + 스케줄러 (독립 프로세스). 첨부 파일 다운로드.
- `web_server.py` — 실시간 음성 비서(STT/TTS WebSocket) 및 옵시디언 볼트 파일 매니저 REST API / Web 정적 서빙 (독립 프로세스).
- `chat_db.py` — **통합 SQLite 대화 데이터베이스**(`data/state/chat.db`). 텔레그램과 웹이 공통으로 바라보는 단일 진실 원천(SSOT) 대화 세션/메시지 저장소 (WAL 모드로 프로세스 간 안전한 동시성 보장).
- `agent_engine.py` — `claude -p` 호출 래퍼. **sandbox-exec 래핑** + **시스템 프롬프트** 주입.
  두 가지 실행 경로: `run_agent`(PIPE 기반, 호출 끝까지 봇이 보유 — 스케줄러·정기작업 해석용),
  그리고 **`spawn_agent`/`watch_agent`**(분리 세션 실행 + 출력 파일 tail — 사용자 메시지용,
  재부착 가능). `detached_pid_alive`/`kill_job`(killpg)로 분리 프로세스를 다룬다.
- `jobs.py` — 진행 중 claude 작업(job) 레코드 영속화(`data/state/jobs.json`, chat 별). 봇 재시작 후
  **재부착(reattach)** 의 근거(`pid·out_file·전달 컨텍스트`). 원자적 저장 패턴.
- `tg_format.py` — claude 마크다운 → 텔레그램 HTML 변환 + 4096자 분할 + 표→카드. **출력 안전망**.
- `paths.py` — 운영 산출물 경로 해석. `DATA_DIR`(기본 `<루트>/data`) 및 하위 폴더(`logs/`·`state/`)를 한 곳에서 정의.
- `sandbox.sb` — claude 격리용 macOS Seatbelt 샌드박스 프로파일(코드의 일부).
- `scheduler.py` — **정기 작업(다이제스트) 실행 루프 & 토큰 버닝 감시**. 봇 프로세스 내 백그라운드 asyncio 태스크로
  매분 cron 을 매칭하고, 매시 정각 주간 쿼터 리셋 직전 잔여 토큰을 감시해 프로젝트 개선을 트리거한다.
- `schedules.py` — 정기 작업 정의 영속화(`data/state/schedules.json`). 원자적 저장. 매분 다시 읽음.
- `burn_runner.py` — **토큰 버닝 프로젝트 개선 실행기**. 주간 리셋 6시간 미만 & 쿼터 30% 이상 시 격리 워크트리에서 개선 작업 자동 수행.
- `burn_todo.py` — **토큰 버닝 개선 TODO 목록 영속화**(`data/state/burn_todo.json`). 테스트·리팩토링·감사·드리밍 등 백로그 관리.
- `cron.py` — 외부 의존성 없는 최소 cron 매칭기("분 시 일 월 요일"). cal-sync 의 순수 파이썬 관례.

**`src/cal-sync/`** — 캘린더 동기화 사이드카
- `calendar_sync.py` — 구글 캘린더(비공개 iCal) → 볼트 노트 동기화. LLM 미사용 순수 파이썬.

**`src/bot/static/`** — 순수 HTML/CSS/JS 모던 클린 라이트(화이트/에메랄드) 테마 음성 비서 & 세컨브레인 볼트 매니저 프론트엔드
- 별도 빌드 단계 없이 `web_server.py`가 `src/bot/static/index.html`을 직접 서빙한다.

---

## 💻 다른 PC에서 이어서 작업하기 (Clone & Setup Guide)

새로운 PC에서 이 리포지토리를 이어받아 개발하거나 실행할 때는 다음 순서로 셋업한다.

### 1. 리포지토리 클론 & 환경 변수
```bash
git clone https://github.com/oshyun/oshyun-bot.git
cd oshyun-bot

# .env 설정 (.env.defaults 복사 후 필수 키 입력)
cp .env.defaults .env
# .env 파일에서 TELEGRAM_BOT_TOKEN 및 AGENT_WORKDIR(Obsidian 볼트 절대 경로) 설정
```

### 2. 백엔드 Python 가상환경 셋업
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 테스트 실행 (작업 전후 무결점 검증)
```bash
# 단위 테스트
PYTHONPATH=src/bot .venv/bin/python3 -m unittest discover -s tests
```

### 4. 서비스 기동 및 접속
```bash
./scripts/start.sh     # 백그라운드 기동 (웹 서버 + 캘린더 사이드카)
./scripts/status.sh    # 가동 상태 및 PID 확인
```
- 브라우저에서 **`http://127.0.0.1:8765/`** 로 접속하면 실시간 음성 비서, 세컨브레인 볼트 매니저, 대화방 목록을 바로 사용할 수 있다.

---

## 🎙️ 실시간 음성 비서 & 세컨브레인 웹 앱 아키텍처

텔레그램 봇과 함께 로컬 브라우저/모바일 환경에서 직접 옵시디언 볼트를 음성과 UI로 탐색하고 조작하는 통합 프론트엔드·백엔드 시스템이다.

### 1. 대화방(Chat Room) 목록 & 신규 세션 격리 (`src/web-app/`)
- **ChatGPT/Claude 스타일 사이드바**:
  - 좌측 사이드바에 대화방 목록(`방 제목`, `메시지 수`, `최근 시각`), `➕ 새 대화 (신규 세션)` 버튼, 개별 방 삭제 기능 제공.
  - 데스크톱/태블릿은 3-Pane 레이아웃(`대화방 목록(250px)` + `Voice Orb Stage(340px)` + `대화 타임라인`), 모바일은 상단 메뉴 드로어로 반응형 전환.
- **세션 격리 (Session Isolation)**:
  - `➕ 새 대화`를 누르면 신규 룸 ID 생성. 첫 메시지 전송 시 WebSocket으로 `newSession: true` 및 `roomId`를 전달하여 백엔드 에이전트 세션 ID(`sessionId`)를 강제 초기화.
  - 이전 대화방의 맥락이 오염되지 않고 완전히 독립된 새 대화 맥락이 보장됨.
- **대화방별 상태 보존 및 복원**:
  - 방마다 대화 메시지, 실시간 thinking/도구 실행 단계(`activeSteps`), 백엔드 세션 ID가 메모리에 보존되며, 목록에서 방을 전환하면 과거 질의응답 및 추론 과정이 온전히 복원됨.
  - 첫 질문 발화 시 질문 첫 문장으로 대화방 제목 자동 요약.

### 2. 실시간 Thinking & 도구 실행 스트리밍 (`web_server.py` ↔ WebSocket `/ws`)
- 에이전트(`claude` / `agy`)의 내부 추론(Thinking) 및 도구 호출(`read_file`, `grep_search`, `write_file` 등)을 실시간 스트리밍:
  - `started`: 작업 시작 알림 (`roomId`, `voiceMode`)
  - `agent_step`: 각 도구 실행 단계, 파라미터 미리보기, 경과 시간(초) 실시간 전달
  - `progress`: 실시간 진행 상태 한 줄 피드
  - `result`: 최종 텍스트 답변, TTS 음성 낭독용 정제 텍스트, 모델 정보 회신
  - `cancelled`: 사용자 취소 처리
- 프론트엔드 타임라인에서 Thinking 및 Tool Execution 내역을 펼침/접힘 카드로 확인 가능.

### 3. 세컨브레인 볼트 매니저 (`/api/vault/*`)
- 옵시디언 볼트(`WORKDIR`)를 웹에서 직접 탐색하고 관리하는 REST API & 전용 UI:
  - `GET /api/vault/tree`: 폴더/마크다운 파일 계층 트리 조회
  - `GET /api/vault/file?path=...`: 마크다운 원문 및 메타데이터 읽기
  - `POST /api/vault/save`: 파일 내용 수정 및 원자적 저장
  - `POST /api/vault/create`: 새 파일/폴더 생성
  - `GET /api/vault/recent`: 최근 수정 파일 목록
- UI 내에서 노트를 열람·편집할 수 있으며, 상단 `🤖 AI에게 이 노트 물어보기` 버튼 클릭 시 해당 파일 컨텍스트가 음성 비서 대화방으로 즉시 연계됨.

### 4. 엔진 & 모델 정책
- **기본 엔진**: **`agy` (Flash 3.8, Medium effort)** 고정.
- **설정 분리**: 헤더를 심플하게 유지하고, 상세 설정은 우측 상단 톱니바퀴 아이콘을 통한 **전용 설정 패널(`SettingsScreen`)**에서 변경 (엔진: `agy`/`claude`, 모델, reasoning effort, TTS 보이스 등).

### 5. 웹 UI 테마 및 디자인 시스템 절대 원칙 (⚠️ 임의 다크 테마 적용 절대 금지)
- **기본 테마 = 모던 클린 라이트(화이트/에메랄드) 테마**:
  - 웹 앱의 공식 기준 테마는 **밝고 깔끔한 화이트/라이트 모드(`color-scheme: light`)**이다.
  - 기본 배경: `#FFFFFF` (`var(--bg)`), 서브 패널/아코디언/카드: `var(--bg-subtle)` (`#F4F4F5`) 또는 `var(--bg-card)`, 테두리: `var(--border)` (`#E6E6E9`), 기본 텍스트: `var(--text)` (`#18181B`), 악센트: `var(--accent)` (`#10B981`, 에메랄드 그린).
- **🚫 임의의 다크 테마 색상 하드코딩 절대 금지 (재발 방지)**:
  - 새로운 UI, 모달, 아코디언, 폼, 콜백 HTML 등을 개발할 때 `#18181b`, `#09090b`, `#121215`, `#161B26` 등의 어두운 배경색을 인라인 스타일이나 전용 클래스에 **절대 하드코딩하지 않는다**.
  - 존재하지 않는 CSS 변수(예: `var(--bg-2)`)를 작성하고 폴백으로 어두운 색(`#18181b`)을 지정하여 화면 일부분이 시커멓게 얼룩지는 현상을 엄격히 금지한다.
- **CSS 토큰 SSOT 준수**:
  - 배경: `var(--bg)`, `var(--bg-subtle)`, `var(--bg-hover)`, `var(--bg-card)`
  - 테두리: `var(--border)`, `var(--border-strong)`
  - 텍스트: `var(--text)`, `var(--text-2)`, `var(--text-3)`
  - 버튼: 기본 버튼은 `.btn`, 긍정/주요 액션은 에메랄드 악센트(`.btn.accent`)를 사용하며, 라이트 톤을 깨뜨리는 무분별한 `.btn.primary`(검은색 솔리드 채움) 남발을 금지한다.

---

**`data/`** — 운영 중 생기는 파일만 모은다(전부 gitignore). **`DATA_DIR` 환경변수로 리포 밖 임의 위치로 통째 분리(relocate) 가능** — 파이썬(`paths.py`)과 셸(`scripts/common.sh`)이 같은 규칙을 따른다.

- `logs/` — 관찰용 산출물(봇 동작에 무관). `telegram-bot.log`/`.pid`/`.err.log`, `cal-sync.log`/`.pid`/`.err.log`, `session_log.jsonl`.
- `state/` — 영속 상태(재시작 후 봇이 읽어야 정상 동작). `chat.db`(통합 SQLite 대화 세션/메시지 DB), `bot_settings.json`(session_id·설정), `schedules.json`(정기작업), `restart_notice.json`(재시작 말풍선 추적), `jobs.json`(job 레코드, chat 별 pid·out_file·컨텍스트), `burn_todo.json`(토큰버닝 TODO), `burn_state.json`(버닝 실행상태).
- `jobs/` — 분리 실행 claude 출력 파일. `<id>.jsonl`(stream-json 출력).
- `attached_files/` — 텔레그램에서 받은 첨부 파일.
- `claude-home/` — claude CLI 전용 디렉토리. 설정·세션·캐시(`AGENT_CONFIG_DIR`)와 스크래치(`tmp/`, `AGENT_CODE_TMPDIR`)를 한 곳에.

## 분리 실행과 재부착 (claude 가 봇 재시작에서 살아남는다)

사용자 메시지는 claude 를 **봇과 분리된 세션(`start_new_session`)** 으로 띄우고 stream-json 을
`data/jobs/<id>.jsonl` 에 기록한다(`spawn_agent`). 봇은 PIPE 대신 이 파일을 tail 해 진행/결과를
읽는다(`watch_agent`). 동시에 `data/state/jobs.json` 에 job 레코드(`pid·out_file·전달 컨텍스트`)를 남긴다(`jobs.py`).

- **봇 종료(SIGTERM)**: watch 태스크만 취소되고 claude 는 **죽이지 않는다(detach)** — 레코드·프로세스 보존.
- **재시작 후(`_reattach_jobs`)**: PID 가 살아 있으면 같은 말풍선에 이어서 배달하고, 다운타임 중 이미
  끝났으면(파일에 `result` 존재) 그 답을 즉시 배달하며, 진짜 죽고 미완결이면 진행 내용 보존 + '중단됨' 안내.
- **죽이는 시점은 둘뿐**: 사용자 ✖ 취소(`on_cancel_callback` → `kill_job`=killpg)와 폭주 timeout(`watch_agent`).
- 스케줄러·정기작업 해석은 재부착이 불필요하므로 기존 `run_agent`(종료 시 함께 죽음)를 그대로 쓴다.

claude 의 임시/설정도 `DATA_DIR` 하위로 분리한다(`agent_engine._build_invocation` 의 env 주입):
- **`AGENT_CODE_TMPDIR=data/claude-home/tmp`** — claude 스크래치(`claude-<uid>`). 인증 무관, 자동 생성.
- **`AGENT_CONFIG_DIR=data/claude-home`** — 설정·세션·히스토리(`~/.claude` 대체). 사용자 개인 `~/.claude`
  와 분리된다. 설정·봇세션은 `_seed_claude_home` 이 1회 시드하지만 **인증은 못 옮긴다**(토큰 stale →
  401). 인증은 한 번 `./scripts/claude-login.sh` 로 대화형 `/login` 해야 잡힌다(이후 Keychain·securityd
  경유라 샌드박스에서도 갱신됨). 둘 다 `sandbox.sb` 에 read/write 허용(HOME 하위라 기본 차단되므로).

**루트**
- `scripts/` — 운영 스크립트(`common.sh` 가 `.env` 로드 + `DATA_DIR` 해석, start/stop 이 두 컴포넌트를 함께 관리).

## 격리 (중요)

claude 는 `--dangerously-skip-permissions` 로 돌지만(headless 라 대화형 승인 불가),
파일시스템 경계는 `src/bot/sandbox.sb`(sandbox-exec) 가 강제한다. claude 와 그 Bash 하위
프로세스는 **볼트 + `~/.claude` + 첨부폴더(`data/attached_files/`) + 인증(Keychain) + 시스템 경로**
만 접근 가능하고 그 외 사용자 홈은 읽기·쓰기 모두 차단된다. macOS 전용. `OSHYUN_BOT_SANDBOX=0` 로 끔.
claude 는 단일 Mach-O 바이너리라 심링크 우회 위해 `realpath` 로 실제 경로를 실행한다.

## 출력 정책

텔레그램은 claude 의 마크다운/HTML 을 그대로 렌더링하지 못한다(특히 색상 `<span>`, 넓은 표).
- 1차: `agent_engine.SYSTEM_PROMPT` 가 claude 에게 평문 출력을 지시(`--append-system-prompt`).
- 2차(안전망): `tg_format.to_telegram_html` 이 stray HTML 제거 + `&<>` 이스케이프(전송 실패 방지).

## 정기 작업 (다이제스트)

사용자가 묻지 않아도 정해진 시각에 claude 를 돌려 정리 결과를 **먼저** 보내주는 기능.
- **등록은 자연어**: `/schedule 매일 아침 8시에 오늘 일정 정리해줘`.
  관리: `/schedule 목록`, `/schedule 삭제 <id>`.
- **claude 는 텍스트만 다룬다(역할 분리)**: 등록 시 claude 는 자연어를 `{cron, prompt, description}`
  JSON 으로 *해석만* 하고(볼트·`data/` 접근 안 함), 그 JSON 을 `data/state/schedules.json` 에 저장하는 것은
  샌드박스 밖 **봇 프로세스**다(claude 는 샌드박스로 `data/` 차단). cal-sync 트리거와 같은 원리.
  저장 위치: `data/state/schedules.json`.
- **실행 시 결합**: `scheduler` 가 cron 매칭되면 저장된 `prompt` 로 `run_agent(workdir=볼트)` 호출 —
  사용자가 직접 보낸 메시지와 동일 환경(볼트 cwd·샌드박스·시스템 프롬프트). claude 가 볼트 노트를
  자기 도구로 읽어 정리한다. 단 **새 세션**(대화 맥락 비오염)으로 돈다.
- 추가/삭제는 파일을 매분 다시 읽으므로 **재시작 불필요**.

## 운영 (서비스 독립 제어 및 격리)

모든 서비스(`bot`, `web`, `cal`)는 **완전 독립된 프로세스**로 실행되며, 다른 서비스에 전혀 영향을 주지 않고 단독으로 기동·종료·재시작할 수 있다.

```bash
# 전체 서비스 일괄 관리
./scripts/start.sh              # 전체 서비스 백그라운드 기동 (bot, web, cal)
./scripts/status.sh             # 전체 서비스 상태 및 PID, 포트, 로그 확인
./scripts/restart.sh            # 전체 서비스 재시작
./scripts/stop.sh               # 전체 서비스 종료

# 개별 서비스 독립 관리 (상호 영향 없음)
./scripts/restart.sh web        # ⚡ 웹 서버만 재시작 (텔레그램 봇 무영향! 챗 세션 유지)
./scripts/restart.sh bot        # 🤖 텔레그램 봇만 재시작 (웹 서버 무영향!)
./scripts/restart.sh cal        # 🗓️ 캘린더 사이드카만 재시작

./scripts/start.sh [bot|web|cal] # 특정 서비스만 단독 기동
./scripts/stop.sh [bot|web|cal]  # 특정 서비스만 단독 종료

# 로그 실시간 확인
./scripts/logs.sh               # 텔레그램 봇 로그 (data/logs/telegram-bot.log)
./scripts/logs.sh web           # 웹 서버 로그 (data/logs/web-server.log)
./scripts/logs.sh cal           # 캘린더 사이드카 로그 (data/logs/cal-sync.log)
./scripts/logs.sh err           # stderr 에러 로그
./scripts/claude-login.sh       # 봇 전용 config(data/claude-home)로 claude 열기 — 최초/재로그인(/login)용
```

가상환경: `.venv` (의존성 `requirements.txt`). 로그·PID 는 `data/logs/`, 영속 상태는 `data/state/` 에 모인다.
**코드/.env 변경 후에는 반드시 `./scripts/restart.sh`** 로 적용.

## 주요 .env 키 (도메인__키 네이밍 규약)

환경변수의 소속과 역할을 한눈에 구분할 수 있도록 `도메인__키=값` 형식을 사용하며, 기존 레거시 키(단일 언더스코어)도 100% 하위 호환된다.

- **텔레그램 (`TELEGRAM__`)**: `TELEGRAM__BOT_TOKEN`(봇 토큰) · `TELEGRAM__ALLOWED_CHAT_IDS`(접근 허용 chat_id)
- **에이전트 (`AGENT__`)**: `AGENT__WORKDIR`(볼트 경로) · `AGENT__CODEDIR`(코드 리포지토리) · `AGENT__MODEL`(기본 모델) · `AGENT__TIMEOUT`(에이전트 타임아웃) · `AGENT__EXTRA_ARGS`
- **LLM/프록시 (`ANTHROPIC__`)**: `ANTHROPIC__MODEL` · `ANTHROPIC__AUTH_TOKEN` · `ANTHROPIC__BASE_URL` · `ANTHROPIC__API_TIMEOUT_MS` · `ANTHROPIC__DEFAULT_{HAIKU,SONNET,OPUS}_MODEL`
- **캘린더 (`CALENDAR__`)**: `CALENDAR__ICAL_URL`(구글 캘린더 iCal 주소) · `CALENDAR__NOTE_PATH`
- **웹 서버 (`WEB__`)**: `WEB__ENABLED` · `WEB__HOST` · `WEB__PORT` · `WEB__AUTH_REQUIRED` · `WEB__TOKEN`
- **도메인 (`DUCKDNS__`)**: `DUCKDNS__DOMAIN` · `DUCKDNS__TOKEN`
- **시스템 (`SYSTEM__` / `DATA_DIR`)**: `SYSTEM__DATA_DIR` 또는 `DATA_DIR`(운영 산출물 위치, 기본 `<루트>/data`) · `AGENT__SANDBOX`(0=격리 끔) · `AGENT__SYSTEM_PROMPT`(시스템 프롬프트 덮어쓰기).

## 텔레그램 명령어 추가 절차

텔레그램 봇 명령어(`/xxx`)를 새로 추가할 때는 반드시 세 곳을 함께 수정한다.

1. **핸들러 함수** — `telegram_bot.py` 에 `async def xxx_command(...)` 구현
2. **`BOT_COMMANDS` 리스트** — `telegram_bot.py` 의 `BOT_COMMANDS` 에 `BotCommand("xxx", "설명")` 추가
3. **`app.add_handler`** — `main()` 안에 `CommandHandler("xxx", xxx_command)` 등록
4. **`HELP` 문자열** — 도움말 텍스트에 설명 추가

> `BOT_COMMANDS` 는 `_post_init` 에서 `bot.set_my_commands()` 로 텔레그램 서버에 등록한다.
> 재시작(`./scripts/restart.sh`) 후 자동으로 서버에 반영된다 — 수동 API 호출 불필요.

## 관례

- 주석/문서는 한국어. 비밀(토큰)은 로그에 남기지 않는다(httpx 로깅 WARNING 으로 낮춤).
- 사용자 응답·UI 문자열도 한국어.
- 작업 완료(merge/restart) 후에는 로컬 커밋을 원격에 동기화(`git push origin main`)한다.
