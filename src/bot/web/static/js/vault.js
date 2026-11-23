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
