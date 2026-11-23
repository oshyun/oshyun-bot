// ─── 볼트 파일 매니저 ───
async function refreshVaultTree() {
  const container = document.getElementById('vault-tree-container');
  try {
    const res = await fetch('/api/vault/tree');
    const data = await res.json();
    const items = data.items || data.tree;
    if (Array.isArray(items)) {
      vaultTreeCache = items;
      onVaultSearch();
    } else if (container) {
      container.innerHTML = `<div class="sb-empty">불러오지 못했습니다: ${escapeHtml(data.error || '오류')}</div>`;
    }
  } catch (err) {
    if (container) container.innerHTML = '<div class="sb-empty">볼트 로드 실패</div>';
  }
}

function flattenVaultFiles(items, out = []) {
  for (const item of (items || [])) {
    if (item.type === 'directory') flattenVaultFiles(item.children || [], out);
    else out.push(item);
  }
  return out;
}

function onVaultSearch() {
  const container = document.getElementById('vault-tree-container');
  const q = (document.getElementById('vault-search-input')?.value || '').trim().toLowerCase();
  if (!container) return;
  if (!vaultTreeCache.length) {
    container.innerHTML = '<div class="sb-empty">볼트가 비어 있습니다.</div>';
    return;
  }
  if (q) {
    const matched = flattenVaultFiles(vaultTreeCache).filter(f =>
      (f.name && f.name.toLowerCase().includes(q)) || (f.path && f.path.toLowerCase().includes(q))
    ).slice(0, 80);
    if (!matched.length) {
      container.innerHTML = '<div class="sb-empty">검색 결과가 없습니다.</div>';
      return;
    }
    container.innerHTML = matched.map(item => {
      const folder = item.path.includes('/') ? item.path.slice(0, item.path.lastIndexOf('/')) : '';
      return `
        <div class="tree-node file ${item.path === currentEditingPath ? 'active' : ''}" style="padding-left:10px" onclick="openVaultFile('${escapeHtml(item.path)}')">
          ${ICONS.file}<span class="nm">${escapeHtml(item.name)}</span>
          ${folder ? `<span class="tree-path">${escapeHtml(folder)}</span>` : ''}
        </div>
      `;
    }).join('');
  } else {
    container.innerHTML = renderTreeItems(vaultTreeCache);
  }
}

function renderTreeItems(items, level = 0) {
  if (!items || !items.length) return '';
  return items.map(item => {
    const indent = level * 14 + 8;
    if (item.type === 'directory') {
      return `
        <div class="tree-node dir" style="padding-left:${indent}px" onclick="toggleDirNode(this)">
          ${ICONS.chevRight}${ICONS.folder}<span class="nm">${escapeHtml(item.name)}</span>
        </div>
        <div class="dir-children" style="display:none">${renderTreeItems(item.children || [], level + 1)}</div>
      `;
    }
    return `
      <div class="tree-node file ${item.path === currentEditingPath ? 'active' : ''}" style="padding-left:${indent + 16}px" onclick="openVaultFile('${escapeHtml(item.path)}')">
        ${ICONS.file}<span class="nm">${escapeHtml(item.name)}</span>
      </div>
    `;
  }).join('');
}

function toggleDirNode(el) {
  const next = el.nextElementSibling;
  if (next && next.classList.contains('dir-children')) {
    const open = next.style.display === 'none';
    next.style.display = open ? 'block' : 'none';
    el.classList.toggle('open', open);
  }
}

let currentVaultMode = 'preview'; // 'preview' | 'edit'

function switchVaultMode(mode = 'preview') {
  currentVaultMode = mode;
  const previewEl = document.getElementById('vault-preview');
  const editorEl = document.getElementById('vault-editor');
  const btnPrev = document.getElementById('btn-vault-mode-preview');
  const btnEdit = document.getElementById('btn-vault-mode-edit');

  if (btnPrev) btnPrev.classList.toggle('active', mode === 'preview');
  if (btnEdit) btnEdit.classList.toggle('active', mode === 'edit');

  if (mode === 'preview') {
    if (editorEl && previewEl) {
      const rawText = editorEl.value || '';
      const html = (typeof formatMarkdown === 'function')
        ? formatMarkdown(rawText)
        : (typeof escapeHtml === 'function' ? escapeHtml(rawText).replace(/\n/g, '<br>') : rawText);
      previewEl.innerHTML = html || '<div class="sb-empty" style="padding:40px 0;">내용이 없습니다.</div>';
    }
    if (previewEl) previewEl.style.display = 'block';
    if (editorEl) editorEl.style.display = 'none';
  } else {
    if (previewEl) previewEl.style.display = 'none';
    if (editorEl) {
      editorEl.style.display = 'block';
      try { editorEl.focus(); } catch (_) {}
    }
  }
}

async function openVaultFile(filePath) {
  if (vaultDirty && !confirm('저장하지 않은 변경사항이 있습니다. 다른 노트를 여시겠습니까?')) return;
  currentEditingPath = filePath;
  setVaultDirty(false);

  const parts = filePath.split('/');
  const name = parts.pop();
  const dir = parts.length ? parts.join(' / ') + ' / ' : '';
  const titleEl = document.getElementById('current-file-title');
  if (titleEl) titleEl.innerHTML = `${escapeHtml(dir)}<b>${escapeHtml(name)}</b>`;

  document.getElementById('vault-layout')?.classList.add('has-file');
  document.getElementById('editor-empty').style.display = 'none';
  const modeSwitch = document.getElementById('vault-mode-switch');
  if (modeSwitch) modeSwitch.style.display = 'inline-flex';
  document.getElementById('btn-ask-file').disabled = false;
  document.getElementById('btn-save-file').disabled = false;
  onVaultSearch();

  try {
    const res = await fetch('/api/vault/file?path=' + encodeURIComponent(filePath));
    const data = await res.json();
    if (data.content !== undefined) {
      const editor = document.getElementById('vault-editor');
      if (editor) editor.value = data.content || '';
      switchVaultMode(currentVaultMode || 'preview');
    } else {
      showToast('파일을 열지 못했습니다.');
    }
  } catch (_) {
    showToast('파일 읽기 오류');
  }
}

function onVaultEditorInput() {
  setVaultDirty(true);
}

function setVaultDirty(dirty) {
  vaultDirty = dirty;
  document.getElementById('vault-editor-pane')?.classList.toggle('dirty', dirty);
}

function closeMobileFile() {
  document.getElementById('vault-layout')?.classList.remove('has-file');
}

async function saveCurrentVaultFile() {
  if (!currentEditingPath) return;
  const content = document.getElementById('vault-editor').value;
  try {
    const res = await fetch('/api/vault/file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: currentEditingPath, content })
    });
    const data = await res.json();
    if (data.success || data.ok) {
      setVaultDirty(false);
      if (currentVaultMode === 'preview') switchVaultMode('preview');
      showToast('저장되었습니다.');
    } else {
      showToast('저장 실패: ' + (data.error || '오류'));
    }
  } catch (_) {
    showToast('저장 중 통신 오류');
  }
}

function askCurrentFileToAI() {
  if (!currentEditingPath) return;
  const fileName = currentEditingPath.split('/').pop().replace(/\.md$/, '');
  switchView('chat');
  const input = document.getElementById('chat-input');
  if (input) {
    setChatInputText(`[[${fileName}]] 노트를 바탕으로 `);
    onChatInput();
    try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
  }
}

/* ═══════════════════════════════════════════════════════════════
   우측 사이드 패널: 노트 뷰어 (Note Viewer)
   채팅 중 화면 이동 없이 문서를 즉시 열람 및 미리보기
   ═══════════════════════════════════════════════════════════════ */
let currentNoteViewerPath = null;
let noteViewerHistory = [];
let _noteViewerReqSeq = 0;

async function openNoteViewer(targetPathOrName, isHistoryNav = false) {
  const cleanTarget = String(targetPathOrName || '').trim().replace(/^\[\[|\]\]$/g, '').trim();
  if (!cleanTarget) return;

  // 0. 타 패널 닫기 (우측 패널 상호 배타적 단일 활성화 보장)
  if (typeof closeRoomDrawer === 'function') closeRoomDrawer();
  if (typeof toggleMasterSidebar === 'function') toggleMasterSidebar(false);
  if (typeof closeThreadPanel === 'function') closeThreadPanel();

  // 1. 패널 및 백드롭 즉시 표시 (UX 지연 최소화)
  const panel = document.getElementById('note-viewer-panel');
  const backdrop = document.getElementById('note-drawer-backdrop');
  if (panel) panel.classList.add('open');
  if (backdrop && window.innerWidth <= 1024) backdrop.classList.add('open');

  // 2. 볼트 파일 트리 캐시 확인 및 경로 정밀 매칭
  if (!vaultTreeCache || vaultTreeCache.length === 0) {
    try {
      const res = await fetch('/api/vault/tree');
      const data = await res.json();
      vaultTreeCache = data.items || data.tree || [];
    } catch (_) {}
  }
  const files = flattenVaultFiles(vaultTreeCache);
  const norm = cleanTarget.toLowerCase().replace(/\.md$/, '');
  const matched = files.find(f => {
    const fname = String(f.name || '').toLowerCase().replace(/\.md$/, '');
    const fpath = String(f.path || '').toLowerCase().replace(/\.md$/, '');
    return fname === norm || fpath === norm || fpath.endsWith('/' + norm);
  });
  const resolvedPath = matched ? matched.path : (cleanTarget.endsWith('.md') ? cleanTarget : `${cleanTarget}.md`);

  // 3. 히스토리 스택 관리
  if (!isHistoryNav && currentNoteViewerPath && currentNoteViewerPath !== resolvedPath) {
    noteViewerHistory.push(currentNoteViewerPath);
  }
  const backBtn = document.getElementById('btn-note-nav-back');
  if (backBtn) {
    backBtn.style.display = noteViewerHistory.length > 0 ? 'inline-flex' : 'none';
  }

  currentNoteViewerPath = resolvedPath;
  const parts = resolvedPath.split('/');
  const fileName = parts.pop();
  const dir = parts.length ? parts.join('/') : '';
  const titleEl = document.getElementById('note-viewer-title');
  const pathEl = document.getElementById('note-viewer-path');
  if (titleEl) {
    titleEl.textContent = fileName.replace(/\.md$/, '');
    titleEl.title = resolvedPath;
  }
  if (pathEl) {
    pathEl.textContent = dir || '루트';
    pathEl.title = dir || '볼트 루트';
    pathEl.style.display = dir ? 'inline-block' : 'none';
  }

  // 4. 본문 비동기 로드
  const bodyEl = document.getElementById('note-viewer-body');
  if (!bodyEl) return;

  bodyEl.innerHTML = '<div class="thread-loading"><span class="run-spinner"></span> 노트를 불러오는 중…</div>';
  const seq = ++_noteViewerReqSeq;

  try {
    const res = await fetch('/api/vault/file?path=' + encodeURIComponent(resolvedPath));
    const data = await res.json();
    if (_noteViewerReqSeq !== seq) return;

    if (data && data.content !== undefined) {
      const raw = data.content || '';
      const html = typeof formatMarkdown === 'function' ? formatMarkdown(raw) : escapeHtml(raw);
      bodyEl.innerHTML = `<div class="note-viewer-content md">${html || '<div class="thread-empty">내용이 비어 있습니다.</div>'}</div>`;
      bodyEl.scrollTop = 0;
    } else {
      bodyEl.innerHTML = `
        <div class="thread-empty" style="padding:32px 16px;text-align:center;">
          <p style="font-weight:600;margin-bottom:6px;color:var(--text);">${escapeHtml(fileName)}</p>
          <p style="font-size:12.5px;color:var(--text-3);margin-bottom:14px;">볼트에서 노트를 찾을 수 없습니다.</p>
          <button type="button" class="btn sm primary" onclick="askNoteViewerToAI()">AI에게 질문하기</button>
        </div>
      `;
    }
  } catch (err) {
    if (_noteViewerReqSeq !== seq) return;
    bodyEl.innerHTML = `<div class="thread-empty" style="color:var(--danger,#ef4444);padding:24px 16px;">노트 로드 실패: ${escapeHtml(err.message || '네트워크 오류')}</div>`;
  }
}

function closeNoteViewer() {
  const panel = document.getElementById('note-viewer-panel');
  const backdrop = document.getElementById('note-drawer-backdrop');
  if (panel) panel.classList.remove('open');
  if (backdrop) backdrop.classList.remove('open');
  currentNoteViewerPath = null;
  noteViewerHistory = [];
}

function navigateNoteViewerHistory(delta) {
  if (delta < 0 && noteViewerHistory.length > 0) {
    const prev = noteViewerHistory.pop();
    if (prev) {
      openNoteViewer(prev, true);
    }
  }
}

function openNoteInVaultFullEditor() {
  if (!currentNoteViewerPath) return;
  const target = currentNoteViewerPath;
  closeNoteViewer();
  if (typeof switchView === 'function') switchView('vault');
  if (typeof openVaultFile === 'function') openVaultFile(target);
}

function askNoteViewerToAI() {
  if (!currentNoteViewerPath) return;
  const target = currentNoteViewerPath;
  const fileName = target.split('/').pop().replace(/\.md$/, '');
  closeNoteViewer();
  if (typeof switchView === 'function' && currentView !== 'chat') switchView('chat');
  const input = document.getElementById('chat-input');
  if (input) {
    setChatInputText(`[[${fileName}]] 노트를 바탕으로 `);
    onChatInput();
    try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
  }
}

