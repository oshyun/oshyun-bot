# oshyun-bot

> **Obsidian 볼트 기반의 개인용 실시간 음성 비서 & 세컨브레인 웹 허브**  
> (Personal Intelligent Web Assistant Hub for Obsidian Vaults)

`oshyun-bot`은 로컬 PC의 LLM CLI(`agy`, `claude -p`)를 백엔드로 연결하여, **모바일 및 데스크톱 웹 브라우저(PWA 지원)**에서 실시간 음성 대화, 에이전트 추론 과정 관찰, Obsidian 노트 관리, 자동화 스케줄링을 통합 제공하는 개인 비서 시스템입니다.

---

## 🌟 주요 기능 (Key Features)

### 1. 🎙️ 실시간 음성 비서 & 반응형 웹 채팅 UI (`src/bot/static/`)
* **웹 오디오 기반 실시간 음성 상호작용**: Voice Orb 스테이지를 통한 음성 입력(STT) 및 Microsoft Edge TTS(고품질 신경망 한국어 음성) 자동 낭독 지원.
* **실시간 Thinking & 도구 실행 스트리밍**: 에이전트의 내부 추론(Thinking) 과정과 도구 호출 단계(`read_file`, `grep`, `write_file` 등 `activeSteps`)를 실시간 카드 UI로 시각화.
* **ChatGPT 스타일 멀티 대화방 & 세션 격리**: 좌측 사이드바에서 대화방 생성·전환·삭제 지원 및 신규 룸 생성 시 독립 세션 보장.
* **PWA (Progressive Web App) 지원**: 모바일(iOS/Android) 홈 화면에 설치하여 앱처럼 독립 전체 화면으로 사용 가능.

### 2. 🧠 세컨브레인 볼트 매니저 (`/api/vault/*`)
* **웹 기반 볼트 탐색 및 편집**: Obsidian 볼트의 폴더/노트 트리 목록을 탐색하고 웹에서 직접 노트를 열람·수정(원자적 저장).
* **노트 컨텍스트 즉시 연계**: 노트 열람 중 상단 `🤖 AI에게 이 노트 물어보기` 클릭 시 해당 파일 내용이 대화방 컨텍스트로 즉시 연동.

### 3. ⚡ 마스터 에이전트 & 멀티 세션 오케스트레이션 (`session-ctl`, `__core__`)
* **마스터 에이전트(`__core__`)**: 전체 대화방을 총괄하며, 코딩·대량 노트 작업 등 복잡한 태스크를 하위 에이전트 세션으로 비동기 위임(`session-ctl spawn/send`)하고 완료 보고 수신.
* **4계층 메모리 관리**: `hot` / `warm` / `cold` / `freeze` 다계층 메모리 아키텍처 및 자동 리밸런싱.

### 4. ⏰ 스케줄러 & 토큰 버닝 자동화 (`scheduler.py`, `burn_runner.py`)
* **정기 작업(다이제스트)**: Cron 기반으로 지정된 시각에 일일 브리핑이나 정리 작업을 백그라운드에서 자동 실행.
* **토큰 버닝(Token Burning)**: 주간 쿼터 리셋 직전 잔여 토큰을 감시하여 테스트 보강, 코드 리팩토링, 볼트 드리밍 등 프로젝트 개선 작업을 격리된 워크트리에서 자동 수행.

### 5. 🗓️ 구글 캘린더 자동 동기화 사이드카 (`calendar_sync.py`)
* 구글 캘린더 비공개 iCal(`.ics`)을 백그라운드에서 주기적으로 읽어 볼트 내 단일 마크다운 노트(`_bot/캘린더.md`)에 일정 자동 기록 (LLM 미사용 순수 파이썬 데몬).

### 6. 🔒 macOS 샌드박스 격리 & 보안 (`sandbox.sb`)
* 에이전트 실행 시 **macOS `sandbox-exec`(Seatbelt)**를 강제 적용하여, 지정된 볼트(`AGENT_WORKDIR`) 및 허용된 작업 경로 외의 시스템 파일 및 개인 디렉토리 접근을 원천 차단.
* 웹 접속 시 비밀 토큰(`WEB_TOKEN`) 및 HMAC 서명 세션 쿠키 인증 지원.

---

## 🏗️ 시스템 아키텍처 (Architecture)

```text
[ 모바일 / 데스크톱 브라우저 (PWA / Web Audio / WebSocket) ]
                           │
                           ▼ (WebSocket /ws  &  REST API /api/*)
┌─────────────────────────────────────────────────────────────────┐
│                     oshyun-bot Web Server                       │
│                     (src/bot/web_server.py)                     │
├─────────────────┬──────────────────────────────┬────────────────┤
│  Chat / Rooms   │      Vault File Manager      │   Scheduler    │
│  (SQLite WAL)   │        (/api/vault/*)        │  & Token Burn  │
└────────┬────────┴──────────────┬───────────────┴────────┬───────┘
         │                       │                        │
         ▼                       ▼                        ▼
┌──────────────────┐  ┌──────────────────────┐  ┌─────────────────┐
│  agent_engine.py │  │    Obsidian Vault    │  │  calendar_sync  │
│ (macOS Seatbelt  │  │   (AGENT_WORKDIR)    │  │    (사이드카)   │
│  sandbox-exec)   │  │  *.md, _bot/캘린더.md │  └────────┬────────┘
└────────┬─────────┘  └──────────────────────┘           │
         │                                               ▼
         ▼                                      [ Google Calendar ]
[ agy / claude CLI ]                                (비공개 iCal)
```

---

## 🚀 시작하기 (Quick Start)

### 1. 사전 준비 (Requirements)
* **OS**: macOS 권장 (샌드박스 격리 지원)
* **Python**: Python 3.9 이상
* **AI CLI**: Antigravity (`agy`) 또는 Claude Code CLI (`claude`)

### 2. 저장소 복제 및 가상환경 구성
```bash
git clone https://github.com/oshyun/oshyun-bot.git
cd oshyun-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 환경 설정 (`.env`)
`.env.defaults`를 복사하여 `.env`를 생성하고 필수 경로를 설정합니다:

```bash
cp .env.defaults .env
```

`.env` 주요 설정 항목:
```bash
# [필수] 에이전트가 작업할 Obsidian 볼트의 절대 경로
AGENT_WORKDIR=/path/to/your/obsidian/vault

# [선택] 구글 캘린더 비공개 iCal 주소 (설정 시 캘린더 동기화 활성화)
GOOGLE_ICAL_URL=https://calendar.google.com/calendar/ical/.../basic.ics

# [웹 서버 설정]
WEB_ENABLED=1
WEB_HOST=127.0.0.1
WEB_PORT=8765
WEB_AUTH_REQUIRED=1
WEB_TOKEN=your-custom-secret-token   # 미지정 시 24자리 난수 자동 생성
```

### 4. 서비스 기동
운영 스크립트를 통해 서비스를 백그라운드로 실행합니다:

```bash
./scripts/start.sh     # 전체 서비스 기동 (웹 서버 + 캘린더 사이드카)
./scripts/status.sh    # 가동 상태 및 PID 확인
```

브라우저에서 **`http://127.0.0.1:8765/`** 로 접속하면 웹 음성 비서와 볼트 매니저를 바로 사용할 수 있습니다.

---

## 🛠️ 서비스 관리 명령어 (CLI Management)

모든 서비스는 `scripts/` 디렉토리의 셸 스크립트로 개별/일괄 제어할 수 있습니다:

```bash
# 전체 서비스 제어
./scripts/start.sh              # 전체 기동 (web, cal)
./scripts/status.sh             # 서비스 상태, PID, 포트, 로그 경로 확인
./scripts/restart.sh            # 전체 재시작
./scripts/stop.sh               # 전체 종료

# 개별 서비스 독립 제어
./scripts/restart.sh web        # 웹 서버만 단독 재시작
./scripts/restart.sh cal        # 캘린더 사이드카만 단독 재시작
./scripts/start.sh web          # 웹 서버 단독 기동
./scripts/stop.sh web           # 웹 서버 단독 종료

# 로그 실시간 모니터링
./scripts/logs.sh web           # 웹 서버 로그 (data/logs/web-server.log)
./scripts/logs.sh cal           # 캘린더 사이드카 로그 (data/logs/cal-sync.log)
./scripts/logs.sh err           # stderr 에러 로그
```

---

## 📂 디렉토리 구조 (Repository Layout)

```text
oshyun-bot/
├── scripts/                    # 서비스 운영 및 관리 셸/CLI 스크립트
│   ├── start.sh / stop.sh      # 서비스 기동/종료
│   ├── restart.sh / status.sh  # 재시작/상태 조회
│   ├── logs.sh                 # 실시간 로그 확인
│   └── session-ctl             # 세션 제어 및 에이전트 오케스트레이션 CLI
├── src/
│   ├── bot/                    # 통합 웹 백엔드 & 에이전트 시스템
│   │   ├── web_server.py       # Aiohttp 기반 REST API & WebSocket 오케스트레이터
│   │   ├── agent_engine.py     # LLM CLI 실행기 (샌드박스 격리 래핑)
│   │   ├── chat_db.py          # SQLite 대화 세션/메시지 SSOT 데이터베이스
│   │   ├── sandbox.sb          # macOS Seatbelt 샌드박스 프로파일
│   │   ├── scheduler.py        # Cron 정기작업 루프
│   │   ├── burn_runner.py      # 토큰 버닝 자동화 실행기
│   │   └── static/             # 순수 HTML/CSS/JS 다크 테마 웹앱 프론트엔드
│   │       ├── index.html      # 음성 비서 & 세컨브레인 단일 페이지 웹앱
│   │       ├── manifest.json   # PWA 설정
│   │       └── sw.js           # PWA Service Worker
│   └── cal-sync/               # 캘린더 사이드카 (순수 Python)
│       └── calendar_sync.py    # Google Calendar iCal → Obsidian 마크다운 동기화
├── tests/                      # 270+ 단위 테스트 스위트
├── .env.defaults               # 기본 환경변수 템플릿
└── requirements.txt            # 파이썬 의존성 패키지 목록
```

---

## 🧪 테스트 실행 (Testing)

```bash
PYTHONPATH=src/bot:src/cal-sync .venv/bin/python3 -m unittest discover -s tests
```

---

## 📄 라이선스 (License)

This project is licensed under the MIT License - see the LICENSE file for details.
