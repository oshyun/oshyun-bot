"""하위 에이전트 말풍선 클릭 시 해당 채팅방으로 이동하는 인터랙션 및 메타데이터 테스트.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import unittest

from bot.core.chat_db import (
    add_message,
    get_or_create_room,
    get_messages,
    init_db,
)


class TestChildAgentBubbleNavigation(unittest.TestCase):
    """하위 에이전트 말풍선 네비게이션 메타데이터 및 프론트엔드 로직 테스트."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent
        self.messages_js_path = (
            self.repo_root / "src" / "bot" / "web" / "static" / "js" / "messages.js"
        )
        self.chat_css_path = (
            self.repo_root / "src" / "bot" / "web" / "static" / "css" / "chat.css"
        )

    def test_css_contains_child_navigable_styles(self):
        """chat.css에 하위 에이전트 네비게이션용 클래스 및 스타일이 정의되어 있는지 검증."""
        css_text = self.chat_css_path.read_text(encoding="utf-8")
        self.assertIn(".msg.is-child-navigable", css_text)
        self.assertIn(".is-child-clickable", css_text)
        self.assertIn(".child-nav-hint", css_text)
        self.assertIn(".child-room-badge", css_text)
        self.assertIn("cursor: pointer", css_text)

    def test_frontend_node_vm_navigation(self):
        """Node.js 가상 환경에서 messages.js의 메시지 렌더링 및 안전 클릭 가드를 검증."""
        js_test_script = f"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

class ClassList {{
  constructor() {{ this._classes = new Set(); }}
  add(...cls) {{ cls.forEach(c => this._classes.add(c)); }}
  remove(...cls) {{ cls.forEach(c => this._classes.delete(c)); }}
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
  }}
  setAttribute(k, v) {{ this.attrs[k] = String(v); }}
  getAttribute(k) {{ return this.attrs[k] || null; }}
  hasAttribute(k) {{ return k in this.attrs; }}
  appendChild(c) {{ this.children.push(c); return c; }}
  addEventListener(ev, fn) {{
    if (!this.listeners[ev]) this.listeners[ev] = [];
    this.listeners[ev].push(fn);
  }}
  closest(selector) {{
    if (this._mockClosest) return this._mockClosest(selector);
    return null;
  }}
}}

let selectionText = '';
const fakeDoc = {{
  createElement: (tag) => new FakeElement(tag)
}};
const fakeWindow = {{
  document: fakeDoc,
  getSelection: () => ({{ toString: () => selectionText }})
}};

const sandbox = {{
  document: fakeDoc,
  window: fakeWindow,
  console: console,
  ICONS: {{ spark: 'spark', tool: 'tool', copy: 'copy', volume: 'volume', edit: 'edit', refresh: 'refresh', chevRight: 'chev' }},
  escapeHtml: (s) => s,
  formatMarkdown: (s) => s,
  filterToolSteps: (s) => s || [],
  buildStepItemHtml: () => '',
  buildMsgTimeHtml: () => '',
  CORE_ROOM_ID: '__core__',
  EXPERT_VAULT_ROOM_ID: '__expert_vault__',
  EXPERT_CODING_ROOM_ID: '__expert_coding__',
  EXPERT_GENERAL_ROOM_ID: '__expert_general__',
  ORCHESTRATOR_ROOM_IDS: ['__core__', '__expert_vault__', '__expert_coding__', '__expert_general__'],
  chatRooms: [
    {{ id: '__core__', title: '마스터 에이전트' }},
    {{ id: 'room-child-vault', title: '볼트 동기화 작업' }},
    {{ id: 'room-child-code', title: '코드 테스트 수행' }}
  ],
  currentRoomId: '__core__',
  getCurrentRoom: () => ({{ id: '__core__', title: '마스터 에이전트' }}),
  getRoomById: (id) => sandbox.chatRooms.find(r => r.id === id) || null,
  findChatRoom: (q) => sandbox.chatRooms.find(r => r.title === q) || null,
  getAgentConfigById: () => null,
  isMasterSubtaskRoom: () => false,
  latestMasterTasks: [],
  roomLiveStates: {{}}
}};

const code = fs.readFileSync('{self.messages_js_path}', 'utf8');
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

let navigatedTo = null;
sandbox.selectRoom = (id) => {{ navigatedTo = id; }};

// 1. 하위 에이전트 보고 메시지 테스트
const reportMsg = {{
  role: 'sub-agent',
  text: '[하위 작업 완료 보고] [볼트 동기화 작업]\\n• 대상 방: 볼트 동기화 작업 (Room ID: room-child-vault)\\n• 상태: 정상 완료\\n• 내용: 완료되었습니다.',
  createdAt: 1000
}};
const rowReport = sandbox.buildMessageRowDom(reportMsg, 0);
assert.strictEqual(rowReport.getAttribute('data-child-room-id'), 'room-child-vault');
assert.ok(rowReport.classList.contains('is-child-navigable'));

// 클릭 인터랙션 테스트 (정상 클릭)
navigatedTo = null;
selectionText = '';
rowReport.listeners['click'][0]({{
  target: rowReport,
  preventDefault: () => {{}},
  stopPropagation: () => {{}},
  metaKey: false, ctrlKey: false, shiftKey: false
}});
assert.strictEqual(navigatedTo, 'room-child-vault', '말풍선 클릭 시 대상 채팅방으로 selectRoom 호출');

// 텍스트 드래그(선택) 중인 경우 방 이동 방지
navigatedTo = null;
selectionText = '드래그된 텍스트 복사 시도';
rowReport.listeners['click'][0]({{
  target: rowReport,
  preventDefault: () => {{}},
  stopPropagation: () => {{}},
  metaKey: false, ctrlKey: false, shiftKey: false
}});
assert.strictEqual(navigatedTo, null, '텍스트 드래그 중에는 selectRoom이 호출되지 않아야 함');

// 내부 버튼/링크 클릭 시 방 이동 방지 (자체 이벤트 우선)
navigatedTo = null;
selectionText = '';
const fakeBtn = new FakeElement('button');
fakeBtn._mockClosest = (sel) => sel.includes('button') ? fakeBtn : null;
rowReport.listeners['click'][0]({{
  target: fakeBtn,
  preventDefault: () => {{}},
  stopPropagation: () => {{}},
  metaKey: false, ctrlKey: false, shiftKey: false
}});
assert.strictEqual(navigatedTo, null, '내부 버튼 클릭 시에는 전체 말풍선 네비게이션이 동작하지 않아야 함');

// 2. 바이패스된 어시스턴트 메시지 테스트
const bypassMsg = {{
  role: 'assistant',
  channel: 'thread_bypass',
  bypassedFromRoomId: 'room-child-code',
  text: '코드 테스트가 통과했습니다.',
  createdAt: 1002
}};
const rowBypass = sandbox.buildMessageRowDom(bypassMsg, 1);
assert.strictEqual(rowBypass.getAttribute('data-child-room-id'), 'room-child-code');
assert.ok(rowBypass.classList.contains('is-child-navigable'));

navigatedTo = null;
rowBypass.listeners['click'][0]({{
  target: rowBypass,
  preventDefault: () => {{}},
  stopPropagation: () => {{}},
  metaKey: false, ctrlKey: false, shiftKey: false
}});
assert.strictEqual(navigatedTo, 'room-child-code', '바이패스 메시지 클릭 시 해당 하위 채팅방으로 이동');

// 3. 현재 방이 이미 해당 하위 방인 경우 이동 제외 테스트
sandbox.currentRoomId = 'room-child-vault';
const rowSameRoom = sandbox.buildMessageRowDom(reportMsg, 2);
assert.strictEqual(rowSameRoom.getAttribute('data-child-room-id'), null, '이미 해당 방에 머무는 경우 네비게이션 비활성화');

console.log('ALL_JS_TESTS_PASSED');
"""
        proc = subprocess.run(
            ["node", "-e", js_test_script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"Node tests failed: {proc.stderr}\n{proc.stdout}")
        self.assertIn("ALL_JS_TESTS_PASSED", proc.stdout)

    def test_backend_bypass_message_contains_subagent_step_and_metadata(self):
        """백엔드 DB에 바이패스 메시지 저장 시 하위 방 정보가 정상 보존되는지 검증."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db") as tf:
            init_db(tf.name)
            get_or_create_room("room-parent", title="마스터 방", db_path=tf.name)
            get_or_create_room("room-child-1", title="하위 작업 1", parent_agent_id="room-parent", db_path=tf.name)

            steps = [{
                "kind": "subagent_bypass",
                "tool": "bypass",
                "title": "하위 작업 1",
                "roomId": "room-child-1",
                "status": "done",
                "bypassedFromRoomId": "room-child-1",
            }]

            msg_id = add_message(
                room_id="room-parent",
                role="assistant",
                text="하위 작업 1이 완료되었습니다.",
                channel="thread_bypass",
                steps=steps,
                db_path=tf.name,
            )
            self.assertGreater(msg_id, 0)

            msgs = get_messages("room-parent", db_path=tf.name)
            self.assertEqual(len(msgs), 1)
            saved_msg = msgs[0]
            self.assertEqual(saved_msg["channel"], "thread_bypass")
            saved_steps = saved_msg.get("steps")
            self.assertIsInstance(saved_steps, list)
            self.assertEqual(len(saved_steps), 1)
            self.assertEqual(saved_steps[0]["roomId"], "room-child-1")
            self.assertEqual(saved_steps[0]["bypassedFromRoomId"], "room-child-1")


if __name__ == "__main__":
    unittest.main()
