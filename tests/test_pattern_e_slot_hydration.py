"""test_pattern_e_slot_hydration.py — 다중 비동기 작업 비순차 완료(3->1->5->2->4) 대응 '패턴 E: 사전 슬롯 예약 및 인플레이스 하이드레이션' 검증."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import AioHTTPTestCase

import bot.core.chat_db as chat_db
import bot.master.master_tasks as master_tasks
import bot.web.agent_reporting as agent_reporting
import bot.web.auth as web_auth
import bot.web.execution as web_execution
import bot.web.server as web_server


class TestPatternESlotHydrationBackend(AioHTTPTestCase):
    """백엔드 디스패치 시점 슬롯 사전 생성 및 비순차 완료 시 인플레이스 하이드레이션 검증."""

    async def get_application(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp_dir.name, "chat.db")
        self.tasks_path = os.path.join(self._tmp_dir.name, "master_tasks.json")
        self.mem_path = os.path.join(self._tmp_dir.name, "master_memory.json")
        self.jobs_path = os.path.join(self._tmp_dir.name, "web_jobs.json")

        self._patchers = [
            patch("bot.core.chat_db.CHAT_DB_FILE", self.db_path),
            patch("bot.master.master_tasks.MASTER_TASKS_FILE", self.tasks_path),
            patch("bot.master.master_memory.MASTER_MEMORY_FILE", self.mem_path),
            patch.object(web_execution, "WEB_JOBS_FILE", self.jobs_path),
            patch.object(web_server, "WEB_JOBS_FILE", self.jobs_path),
        ]
        for p in self._patchers:
            p.start()
        chat_db.init_db(self.db_path)
        web_auth.WEB_TOKEN = "test-slot-secret-token"
        web_auth.AUTH_DELAY_SECONDS = 0.0
        return web_server.create_web_app(static_dir="/nonexistent")

    async def tearDownAsync(self):
        await super().tearDownAsync()
        for p in reversed(getattr(self, "_patchers", [])):
            p.stop()
        if hasattr(self, "_tmp_dir"):
            self._tmp_dir.cleanup()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        agent_reporting._recent_reported_rooms.clear()
        cookie_val = web_server._create_session_cookie_value()
        self.client.session.cookie_jar.update_cookies(
            {web_server.AUTH_COOKIE_NAME: cookie_val}
        )

    async def test_out_of_order_completion_preserves_dispatch_slot_order_1_to_5(self):
        """작업 1~5를 순차 발주한 뒤 3->1->5->2->4 비순차로 완료되어도 타임라인 DB 순서는 1->2->3->4->5로 고정된다."""
        mock_enqueue = AsyncMock(return_value={"already_running": False, "queue_size": 0})
        mock_broadcast = AsyncMock()

        chat_db.get_or_create_room("__core__", title="마스터 에이전트", db_path=self.db_path)
        chat_db.add_message("__core__", "user", "5개 독립 작업 병렬 실행해줘", db_path=self.db_path)

        dispatched_tasks: list[dict] = []
        with (
            patch.object(web_execution, "enqueue_room_turn", mock_enqueue),
            patch.object(web_server, "enqueue_room_turn", mock_enqueue),
            patch.object(web_execution, "broadcast_ws_json", mock_broadcast),
            patch.object(web_server, "broadcast_ws_json", mock_broadcast),
            patch.object(agent_reporting, "broadcast_ws_json", mock_broadcast),
        ):
            # 1~5번 작업 순차 디스패치
            for i in range(1, 6):
                resp = await self.client.post(
                    "/api/orchestrator/dispatch",
                    json={
                        "action": "spawn",
                        "title": f"독립 작업 #{i}",
                        "prompt": f"{i}번째 모듈 점검 수행",
                        "parentAgentId": "__core__",
                        "originRoomId": "__core__",
                        "forceNew": True,
                        "wait": False,
                    },
                )
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertTrue(body["ok"])
                self.assertIsNotNone(body.get("slotMsgId"))
                dispatched_tasks.append(body)

            # 발주 직후 __core__ 방에는 유저 메시지 1개 + 사전 예약 슬롯 5개가 1~5 순서로 존재해야 함
            msgs_before = chat_db.get_messages("__core__", db_path=self.db_path)
            slot_msgs_before = [m for m in msgs_before if m["role"] == "assistant"]
            self.assertEqual(len(slot_msgs_before), 5)
            slot_ids_before = [m["id"] for m in slot_msgs_before]
            self.assertEqual(slot_ids_before, [t["slotMsgId"] for t in dispatched_tasks])
            for idx, sm in enumerate(slot_msgs_before, start=1):
                self.assertEqual(sm["text"], "")
                self.assertEqual(sm["channel"], "thread_bypass")
                self.assertEqual(sm["steps"][0]["title"], f"독립 작업 #{idx}")
                self.assertTrue(sm["steps"][0].get("isSlotPlaceholder"))

            # 비순차 완료 시뮬레이션: 3 -> 1 -> 5 -> 2 -> 4
            completion_order = [3, 1, 5, 2, 4]
            for num in completion_order:
                t_info = dispatched_tasks[num - 1]
                await agent_reporting._notify_master_agent(
                    room_id=t_info["roomId"],
                    task_id=t_info["taskId"],
                    result_text=f"작업 #{num} 최종 분석 완료 결론입니다.\n세부 항목 {num}-A\n세부 항목 {num}-B",
                    is_success=True,
                )

            # 모든 비순차 완료 후에도 새 메시지가 끝에 추가되지 않고 원래 1->2->3->4->5 슬롯 ID와 순서가 100% 유지되어야 함!
            msgs_after = chat_db.get_messages("__core__", db_path=self.db_path)
            slot_msgs_after = [m for m in msgs_after if m["role"] == "assistant"]
            self.assertEqual(len(slot_msgs_after), 5)
            self.assertEqual([m["id"] for m in slot_msgs_after], slot_ids_before)

            for idx, sm in enumerate(slot_msgs_after, start=1):
                self.assertIn(f"작업 #{idx} 최종 분석 완료 결론입니다.", sm["text"])
                self.assertEqual(sm["steps"][0]["status"], "done")

    async def test_reused_old_room_allocates_fresh_bottom_slot(self):
        """오래전에 생성해 꾸준히 재사용하는 하위 방에 새 작업을 지시(send)해도 위로 묻히지 않고 최신 발주 위치에 새 슬롯이 고정된다."""
        mock_enqueue = AsyncMock(return_value={"already_running": False, "queue_size": 0})
        mock_broadcast = AsyncMock()

        chat_db.get_or_create_room("__core__", title="마스터 에이전트", db_path=self.db_path)
        chat_db.get_or_create_room(
            "room-old-reused",
            title="꾸준히 재사용하는 볼트 동기화 방",
            parent_agent_id="__core__",
            db_path=self.db_path,
        )
        # 과거 대화 히스토리
        old_slot_id = chat_db.add_message(
            "__core__",
            "assistant",
            "과거 1차 동기화 완료 보고입니다.",
            channel="thread_bypass",
            steps=[{"kind": "subagent_bypass", "roomId": "room-old-reused", "status": "done"}],
            db_path=self.db_path,
        )
        chat_db.add_message("__core__", "user", "새로운 질문과 후속 지시", db_path=self.db_path)
        mid_asst_id = chat_db.add_message("__core__", "assistant", "네, 후속 작업을 위임하겠습니다.", db_path=self.db_path)

        with (
            patch.object(web_execution, "enqueue_room_turn", mock_enqueue),
            patch.object(web_server, "enqueue_room_turn", mock_enqueue),
            patch.object(web_execution, "broadcast_ws_json", mock_broadcast),
            patch.object(web_server, "broadcast_ws_json", mock_broadcast),
            patch.object(agent_reporting, "broadcast_ws_json", mock_broadcast),
        ):
            resp = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "send",
                    "target": "room-old-reused",
                    "prompt": "오늘자 2차 동기화 진행해줘",
                    "parentAgentId": "__core__",
                    "originRoomId": "__core__",
                    "wait": False,
                },
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["ok"])
            new_slot_id = body.get("slotMsgId")
            self.assertIsNotNone(new_slot_id)
            self.assertGreater(new_slot_id, mid_asst_id)
            self.assertNotEqual(new_slot_id, old_slot_id)

            await agent_reporting._notify_master_agent(
                room_id="room-old-reused",
                task_id=body["taskId"],
                result_text="오늘자 2차 동기화 완료 보고입니다.",
                is_success=True,
            )

            msgs = chat_db.get_messages("__core__", db_path=self.db_path)
            # 과거 슬롯(old_slot_id)은 그대로 보존되고, 최신 하단 슬롯(new_slot_id)에 2차 보고가 채워져야 함
            old_msg = next(m for m in msgs if m["id"] == old_slot_id)
            new_msg = next(m for m in msgs if m["id"] == new_slot_id)
            self.assertEqual(old_msg["text"], "과거 1차 동기화 완료 보고입니다.")
            self.assertEqual(new_msg["text"], "오늘자 2차 동기화 완료 보고입니다.")
            self.assertEqual(msgs[-1]["id"], new_slot_id)


class TestPatternESlotHydrationFrontend(unittest.TestCase):
    """Node.js VM을 통한 프론트엔드 패턴 E 슬롯 플레이스홀더, 아코디언 요약, 비순차 인플레이스 하이드레이션 검증."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent
        self.messages_js_path = self.repo_root / "src" / "bot" / "web" / "static" / "js" / "messages.js"
        self.ws_js_path = self.repo_root / "src" / "bot" / "web" / "static" / "js" / "ws.js"

    def test_frontend_pattern_e_slot_lifecycle_and_accordion(self):
        js_script = f"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class ClassList {{
  constructor() {{ this._classes = new Set(); }}
  add(...cls) {{ cls.forEach(c => this._classes.add(c)); }}
  remove(...cls) {{ cls.forEach(c => this._classes.delete(c)); }}
  toggle(c, force) {{
    if (force === undefined) {{
      if (this._classes.has(c)) this._classes.delete(c);
      else this._classes.add(c);
    }} else if (force) this._classes.add(c);
    else this._classes.delete(c);
  }}
  contains(c) {{ return this._classes.has(c); }}
}}

class FakeElement {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.classList = new ClassList();
    this.attrs = {{}};
    this.children = [];
    this.listeners = {{}};
    this.textContent = '';
    this.innerHTML = '';
    this.style = {{}};
    this.open = false;
  }}
  setAttribute(k, v) {{ this.attrs[k] = String(v); }}
  getAttribute(k) {{ return this.attrs[k] || null; }}
  hasAttribute(k) {{ return k in this.attrs; }}
  removeAttribute(k) {{ delete this.attrs[k]; }}
  appendChild(c) {{ this.children.push(c); return c; }}
  addEventListener(ev, fn) {{
    if (!this.listeners[ev]) this.listeners[ev] = [];
    this.listeners[ev].push(fn);
  }}
  querySelector(sel) {{ return null; }}
  querySelectorAll(sel) {{ return []; }}
}}

const containerEl = new FakeElement('div');
const fakeDoc = {{
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => id === 'messages-container' ? containerEl : null,
  querySelectorAll: () => []
}};

const masterRoom = {{
  id: '__core__',
  title: '마스터 에이전트',
  historyLoaded: true,
  messages: [
    {{ id: 100, role: 'user', sender: 'user', text: '5개 작업 시켜줘', createdAt: 1000 }}
  ]
}};

const sandbox = {{
  document: fakeDoc,
  window: {{ document: fakeDoc, getSelection: () => ({{ toString: () => '' }}) }},
  console: console,
  setInterval: () => 1,
  clearInterval: () => {{}},
  setTimeout: () => 1,
  clearTimeout: () => {{}},
  fetch: () => Promise.resolve({{ json: () => Promise.resolve({{ ok: true }}) }}),
  ICONS: {{ spark: 'spark', tool: 'tool', copy: 'copy', volume: 'volume', edit: 'edit', refresh: 'refresh', chevRight: 'chev', brain: 'brain', terminal: 'term' }},
  escapeHtml: (s) => String(s || ''),
  formatMarkdown: (s) => `<p>${{s}}</p>`,
  filterToolSteps: (s) => (s || []).filter(x => x && x.kind !== 'subagent_bypass'),
  buildStepItemHtml: () => '',
  buildMsgTimeHtml: () => '',
  getMessageDateKey: () => '2026-11-28',
  formatDateDividerLabel: () => '2026년 11월 28일',
  CORE_ROOM_ID: '__core__',
  CORE_ROOM_TITLE: '마스터 에이전트',
  EXPERT_VAULT_ROOM_ID: '__expert_vault__',
  EXPERT_CODING_ROOM_ID: '__expert_coding__',
  EXPERT_GENERAL_ROOM_ID: '__expert_general__',
  ORCHESTRATOR_ROOM_IDS: ['__core__', '__expert_vault__', '__expert_coding__', '__expert_general__'],
  chatRooms: [
    masterRoom,
    {{ id: 'room-sub-1', title: '작업 1', parentAgentId: '__core__' }},
    {{ id: 'room-sub-2', title: '작업 2', parentAgentId: '__core__' }},
    {{ id: 'room-sub-3', title: '작업 3', parentAgentId: '__core__' }}
  ],
  currentRoomId: '__core__',
  ensureCoreRoom: () => masterRoom,
  getCurrentRoom: () => masterRoom,
  getRoomById: (id) => sandbox.chatRooms.find(r => r.id === id) || null,
  findChatRoom: (q) => sandbox.chatRooms.find(r => r.title === q) || null,
  getAgentConfigById: () => null,
  isMasterSubtaskRoom: () => true,
  latestMasterTasks: [],
  roomLiveStates: {{
    '__core__': {{ isRunning: false, steps: [] }},
    'room-sub-1': {{ isRunning: true, feed: 'grep_search 실행 중…' }},
    'room-sub-2': {{ isRunning: true, feed: '생각하는 중…' }},
    'room-sub-3': {{ isRunning: true, feed: 'read_file 실행 중…' }}
  }},
  ensureRoomLiveState: (id) => {{
    if (!sandbox.roomLiveStates[id]) sandbox.roomLiveStates[id] = {{ isRunning: false, steps: [] }};
    return sandbox.roomLiveStates[id];
  }},
  saveChatRoomsToStorage: () => {{}},
  renderRoomList: () => {{}},
  sortChatRooms: () => {{}},
  updateRoomConfigBar: () => {{}},
  updateComposerStatusBar: () => {{}},
  updateSendButtonState: () => {{}},
  updateSessionBar: () => {{}},
  syncLiveElapsedTimer: () => {{}},
  setAgentAvatarStatus: () => {{}},
  setOrbState: () => {{}},
  getTtsMode: () => 'off',
  location: {{ protocol: 'http:', host: 'localhost' }},
  getGatewayToken: () => ''
}};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('{self.ws_js_path}', 'utf8'), sandbox);
vm.runInContext(fs.readFileSync('{self.messages_js_path}', 'utf8'), sandbox);

// 1. 1~3번 작업 순차 디스패치 이벤트 수신 -> 슬롯 201, 202, 203번 생성
[1, 2, 3].forEach(num => {{
  sandbox.handleWsEvent({{
    type: 'orchestrator_dispatch',
    action: 'spawn',
    roomId: `room-sub-${{num}}`,
    title: `작업 ${{num}}`,
    text: `작업 ${{num}} 지시`,
    parentAgentId: '__core__',
    originRoomId: '__core__',
    targetUiRoomId: '__core__',
    taskId: `mt-${{num}}`,
    slotMsgId: 200 + num,
    slotStatus: 'running',
    wait: false
  }});
}});

assert.strictEqual(masterRoom.messages.length, 4, '유저 메시지 1개 + 예약 슬롯 3개가 생성되어야 함');
assert.deepStrictEqual(masterRoom.messages.slice(1).map(m => m.id), [201, 202, 203]);

// 실행 중 슬롯 DOM 렌더링 검증 (msg-empty-card 가 아닌 msg-slot-placeholder 로 렌더링되어야 함)
sandbox.renderMessagesForCurrentRoom();
const slot1Dom = sandbox.buildMessageRowDom(masterRoom.messages[1], 1);
assert.ok(slot1Dom.classList.contains('sub-agent-slot-row'), '슬롯 행 클래스가 있어야 함');
assert.strictEqual(slot1Dom.getAttribute('data-slot-order'), '1', '첫 번째 슬롯 번호가 #1이어야 함');
const placeholderCard = slot1Dom.children.find(c => c.className && c.className.includes('msg-slot-placeholder'));
assert.ok(placeholderCard, '미완료 슬롯은 빈 응답 에러 카드가 아닌 msg-slot-placeholder 스켈레톤 카드로 렌더링되어야 함');

// 2. 비순차 완료: #3번(slotMsgId: 203)이 먼저 6줄짜리 긴 보고로 완료됨
const longReport3 = [
  '결론: 3번 모듈 보안 감사 이상 없음.',
  '핵심 요약 2번째 줄입니다.',
  '상세 내역 3번째 줄',
  '상세 내역 4번째 줄',
  '상세 내역 5번째 줄',
  '상세 내역 6번째 줄'
].join('\\n');

sandbox.handleWsEvent({{
  type: 'result',
  roomId: '__core__',
  msgId: 203,
  slotMsgId: 203,
  taskId: 'mt-3',
  inPlaceHydrated: true,
  text: longReport3,
  bypassedFromRoomId: 'room-sub-3',
  subagentTitle: '작업 3',
  turnType: 'delegation_result',
  silentNotification: true,
  isSuccess: true,
  steps: [{{ kind: 'subagent_bypass', tool: 'bypass', roomId: 'room-sub-3', taskId: 'mt-3', status: 'done', title: '작업 3' }}]
}});

// 메시지 개수는 여전히 4개이고 순서도 [100, 201, 202, 203] 유지!
assert.strictEqual(masterRoom.messages.length, 4);
assert.deepStrictEqual(masterRoom.messages.map(m => m.id), [100, 201, 202, 203]);
assert.strictEqual(masterRoom.messages[3].text, longReport3);

// 완료된 #3번 슬롯 DOM 렌더링 시 2줄 요약(.sub-agent-preview) + 접힌 아코디언(.sub-agent-details) 구조인지 확인
const slot3Dom = sandbox.buildMessageRowDom(masterRoom.messages[3], 3);
const mdContainer = slot3Dom.children.find(c => c.className && c.className.includes('sub-agent-md'));
assert.ok(mdContainer, '완료된 슬롯은 sub-agent-md 컨테이너를 가져야 함');
const previewEl = mdContainer.children.find(c => c.className === 'sub-agent-preview');
const detailsEl = mdContainer.children.find(c => c.className === 'sub-agent-details');
assert.ok(previewEl, '긴 완료 보고는 2줄 요약 프리뷰를 가져야 함');
assert.ok(detailsEl, '긴 완료 보고는 전체 보고 보기 아코디언(details)을 가져야 함');
assert.strictEqual(detailsEl.open, false, '초기 하이드레이션 시 아코디언은 접힌 상태(CLS 방지)여야 함');

console.log('PATTERN_E_FRONTEND_PASSED');
"""
        proc = subprocess.run(["node", "-e", js_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node test failed: {proc.stderr}\n{proc.stdout}")
        self.assertIn("PATTERN_E_FRONTEND_PASSED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
