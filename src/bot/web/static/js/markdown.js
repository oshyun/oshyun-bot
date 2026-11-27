// ─── 도구 실행 단계 & 마크다운 렌더링 ───
function filterToolSteps(steps) {
  if (!Array.isArray(steps)) return [];
  return steps.filter(s => s && s.kind !== 'tool_result');
}

function getStepIconSvg(s) {
  if (s && s.kind === 'thinking') return ICONS.brain || ICONS.spark;
  const tool = String((s && s.tool) || '').toLowerCase();
  const title = String((s && s.title) || '');
  if (title.startsWith('session-ctl') || title.startsWith('세션 목록 스캔') || title.startsWith('세션 지시 전송') || title.startsWith('새 세션 스폰')) return ICONS.spark;
  if (title.startsWith('세션 열람')) return ICONS.file;
  if (tool.includes('read') || tool.includes('view') || title.startsWith('파일 열람')) return ICONS.file;
  if (tool.includes('edit') || tool.includes('write') || tool.includes('replace') || title.startsWith('파일 편집') || title.startsWith('파일 작성')) return ICONS.edit;
  if (tool.includes('grep') || tool.includes('find') || tool.includes('glob') || title.startsWith('볼트 검색') || title.startsWith('파일 찾기')) return ICONS.search;
  if (tool.includes('bash') || tool.includes('command') || title.startsWith('명령어 실행')) return ICONS.terminal;
  if (tool.includes('web') || title.startsWith('웹 검색')) return ICONS.globe;
  if (tool.includes('dir') || title.startsWith('폴더 탐색')) return ICONS.folder;
  return ICONS.tool;
}

function buildStepItemHtml(s, isCompletedMsg = false) {
  const isThinking = (s && s.kind === 'thinking');
  let titleHtml = '';
  if (isThinking) {
    titleHtml = `<span class="step-action">thinking</span>`;
  } else {
    const rawTitle = String((s && (s.title || s.tool)) || 'tool');
    let toolName = (s && s.tool) ? String(s.tool).trim() : '';
    const colonIdx = rawTitle.indexOf(':');
    let targetPart = '';
    if (colonIdx > 0) {
      targetPart = rawTitle.slice(colonIdx + 1).trim();
      const actionPart = rawTitle.slice(0, colonIdx).trim();
      if (actionPart.startsWith('session-ctl')) {
        toolName = actionPart;
      } else if (actionPart.includes('열람')) {
        toolName = 'read_file';
      } else if (actionPart.includes('편집')) {
        toolName = 'edit_file';
      } else if (actionPart.includes('작성')) {
        toolName = 'write_file';
      } else if (actionPart.includes('검색')) {
        toolName = toolName || 'grep_search';
      } else if (actionPart.includes('명령어')) {
        toolName = 'run_command';
      } else if (actionPart.includes('탐색')) {
        toolName = 'list_dir';
      } else if (actionPart.includes('웹')) {
        toolName = 'search_web';
      } else if (actionPart.includes('세션 목록')) {
        toolName = 'session-ctl list';
        targetPart = 'all';
      } else if (actionPart.includes('세션 열람')) {
        toolName = 'session-ctl read';
      } else if (actionPart.includes('세션 지시')) {
        toolName = 'session-ctl send';
      } else if (actionPart.includes('세션 스폰')) {
        toolName = 'session-ctl spawn';
      } else if (!toolName) {
        toolName = actionPart;
      }
    } else {
      if (!toolName) toolName = rawTitle;
    }
    const displayAction = toolName || 'tool';
    titleHtml = `<span class="step-action">${escapeHtml(displayAction)}</span>${targetPart ? `<code class="step-target">${escapeHtml(targetPart)}</code>` : ''}`;
  }

  const durVal = (s && s.durationSeconds !== undefined) ? s.durationSeconds : (s && s.elapsedSeconds);
  const elapsed = durVal !== undefined && durVal !== '' ? `${escapeHtml(String(durVal))}s` : '';
  const status = isCompletedMsg
    ? (s && s.status === 'error' ? 'error' : 'done')
    : ((s && s.status) || (isThinking ? 'done' : 'running'));
  let statusBadge = '';
  if (status === 'done') {
    statusBadge = `<span class="step-status done" title="완료">✓</span>`;
  } else if (status === 'error') {
    statusBadge = `<span class="step-status error" title="오류">!</span>`;
  } else {
    statusBadge = `<span class="spinner sm"></span>`;
  }

  const rawDetail = (s && s.detail) || (s && s.input ? JSON.stringify(s.input, null, 2) : '');
  const detail = escapeHtml(rawDetail);
  const iconSvg = getStepIconSvg(s);
  return `
    <summary>${iconSvg}<span class="st">${titleHtml}</span>${elapsed ? `<span class="se">${elapsed}</span>` : ''}${statusBadge}</summary>
    ${detail ? `<pre class="${isThinking ? 'step-thinking' : ''}">${detail}</pre>` : ''}
  `;
}

function formatLiveStepsSummaryLabel(steps) {
  const toolSteps = filterToolSteps(steps);
  const count = toolSteps.length;
  if (count === 0) return '';
  const allDone = toolSteps.every(s => s && (s.status === 'done' || s.status === 'error' || s.kind === 'thinking'));
  const thinkingCount = toolSteps.filter(s => s && s.kind === 'thinking').length;
  const toolCount = count - thinkingCount;
  if (toolCount === 0 && thinkingCount > 0) {
    return allDone ? 'thinking 완료' : 'thinking…';
  }
  if (thinkingCount > 0 && toolCount > 0) {
    return allDone ? `thinking + ${toolCount} tools 완료` : `thinking + ${toolCount} tools 실행 중`;
  }
  return allDone ? `${count} tools 완료` : `${count} tools 실행 중`;
}

function appendLiveAgentStepToDom(step) {
  const liveDetails = document.getElementById('live-steps-details');
  const liveList = document.getElementById('live-steps-list');
  const liveCount = document.getElementById('live-steps-count');
  if (!liveList || !liveDetails) {
    renderMessagesForCurrentRoom();
    return;
  }
  liveDetails.style.display = '';
  const stepEl = document.createElement('details');
  stepEl.className = 'step';
  stepEl.innerHTML = buildStepItemHtml(step, false);
  liveList.appendChild(stepEl);
  if (liveCount) liveCount.innerText = `${liveList.children.length} tools 실행 중`;
  scrollToBottomIfNear();
}

function updateLiveAgentStepInDom(stepIndex, step, allSteps) {
  const liveList = document.getElementById('live-steps-list');
  const liveCount = document.getElementById('live-steps-count');
  if (!liveList) return;
  const targetEl = liveList.children[stepIndex];
  if (targetEl) {
    const wasOpen = targetEl.hasAttribute('open');
    targetEl.innerHTML = buildStepItemHtml(step, false);
    if (wasOpen) targetEl.setAttribute('open', '');
  }
  if (liveCount) {
    liveCount.innerText = formatLiveStepsSummaryLabel(allSteps);
  }
}

let _liveStreamTargetText = '';
let _liveStreamRenderedText = '';
let _liveStreamRafId = null;

function _runSmoothStreamTick() {
  _liveStreamRafId = null;
  let streamEl = document.getElementById('live-stream-md');
  if (!streamEl) {
    if (typeof renderMessagesForCurrentRoom === 'function') {
      renderMessagesForCurrentRoom();
    }
    streamEl = document.getElementById('live-stream-md');
    if (!streamEl) return;
  }

  const targetLen = _liveStreamTargetText.length;
  const renderedLen = _liveStreamRenderedText.length;
  const remaining = targetLen - renderedLen;

  if (remaining <= 0) {
    if (_liveStreamRenderedText !== _liveStreamTargetText) {
      _liveStreamRenderedText = _liveStreamTargetText;
      streamEl.style.display = _liveStreamRenderedText ? '' : 'none';
      streamEl.innerHTML = formatMarkdown(_liveStreamRenderedText);
      scrollToBottomIfNear();
    }
    return;
  }

  // 적응형 글자 수 계산 (Adaptive Easing):
  // 60fps 기준 부드러운 타이핑 느낌을 주되, 버퍼가 쌓이면 신속히 캐치업
  let step = 1;
  if (remaining > 60) {
    step = Math.min(24, Math.ceil(remaining / 4));
  } else if (remaining > 25) {
    step = Math.min(8, Math.ceil(remaining / 5));
  } else if (remaining > 8) {
    step = 3;
  } else if (remaining > 3) {
    step = 2;
  } else {
    step = 1;
  }

  const nextLen = Math.min(targetLen, renderedLen + step);
  _liveStreamRenderedText = _liveStreamTargetText.slice(0, nextLen);

  streamEl.style.display = '';
  streamEl.innerHTML = formatMarkdown(_liveStreamRenderedText);
  scrollToBottomIfNear();

  if (_liveStreamRenderedText.length < targetLen) {
    _liveStreamRafId = requestAnimationFrame(_runSmoothStreamTick);
  }
}

function updateLiveStreamMarkdownDom(streamText) {
  _liveStreamTargetText = streamText || '';
  if (!_liveStreamRafId) {
    _liveStreamRafId = requestAnimationFrame(_runSmoothStreamTick);
  }
}

function flushLiveStreamBuffer() {
  if (_liveStreamRafId) {
    cancelAnimationFrame(_liveStreamRafId);
    _liveStreamRafId = null;
  }
  _liveStreamRenderedText = _liveStreamTargetText;
  const streamEl = document.getElementById('live-stream-md');
  if (streamEl && _liveStreamTargetText) {
    streamEl.style.display = '';
    streamEl.innerHTML = formatMarkdown(_liveStreamTargetText);
    scrollToBottomIfNear();
  }
  _liveStreamTargetText = '';
  _liveStreamRenderedText = '';
}

function decodeSafeUrlPart(val) {
  let out = String(val || '');
  for (let k = 0; k < 2; k++) {
    try {
      const next = decodeURIComponent(out);
      if (next === out) break;
      out = next;
    } catch (_) {
      break;
    }
  }
  return out;
}

function buildPrettyLinkHtml(rawUrl, explicitLabel = '') {
  const cleanUrl = String(rawUrl || '').trim()
    .replace(/&amp;/g, '&')
    .replace(/ /g, '%20');
  const isHttp = /^https?:\/\//i.test(cleanUrl);
  const isFile = /^file:\/\//i.test(cleanUrl);
  const isObsidian = /^obsidian:\/\//i.test(cleanUrl);
  const isRoom = /^(?:room|chat):/i.test(cleanUrl);
  if (isRoom) {
    const target = cleanUrl.replace(/^(?:room|chat):\/\/?/i, '').trim();
    const matched = findChatRoom(target) || findChatRoom(explicitLabel);
    return buildChatRoomChipHtml(matched, explicitLabel || (matched ? matched.title : target));
  }
  if (!isHttp && !isFile && !isObsidian) {
    return escapeHtml(explicitLabel || rawUrl);
  }

  const cleanExplicit = String(explicitLabel || '')
    .trim()
    .replace(/^link:\s*/i, '')
    .replace(/^`+|`+$/g, '')
    .trim();

  const renderChip = (iconSvg, labelText, metaText, titleText, extraClass = '') => {
    const metaHtml = metaText
      ? `<span class="md-link-meta">· ${escapeHtml(metaText)}</span>`
      : '';
    const cls = extraClass ? `md-link-chip ${extraClass}` : 'md-link-chip';
    return `<a class="${cls}" href="${escapeHtml(cleanUrl)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(titleText)}">${iconSvg}<span class="md-link-label">${escapeHtml(labelText)}</span>${metaHtml}${ICONS.externalLink}</a>`;
  };

  let parsedUrl = null;
  try {
    parsedUrl = new URL(cleanUrl);
  } catch (_) {
    return isFile
      ? escapeHtml(cleanExplicit || rawUrl)
      : renderChip(ICONS.link, cleanExplicit || cleanUrl, '', cleanUrl);
  }

  if (isFile) {
    const decodedPath = decodeSafeUrlPart(parsedUrl.pathname || cleanUrl.replace(/^file:\/\//i, ''));
    const hashLabel = String(parsedUrl.hash || '').replace(/^#/, '').trim();
    const pathParts = decodedPath.split('/').filter(Boolean);
    const fileName = pathParts.length ? pathParts[pathParts.length - 1] : decodedPath;
    const parentDir = pathParts.length > 1 ? pathParts[pathParts.length - 2] : '';
    const isGenericFileLabel = !cleanExplicit || /^file:\/\//i.test(cleanExplicit);
    const displayLabel = isGenericFileLabel ? (fileName || decodedPath) : cleanExplicit;
    const metaText = hashLabel || (parentDir && !displayLabel.includes(parentDir) ? parentDir : '');
    const fullPathWithLine = hashLabel ? `${decodedPath}#${hashLabel}` : decodedPath;
    const metaHtml = metaText
      ? `<span class="md-link-meta">· ${escapeHtml(metaText)}</span>`
      : '';
    return `<button type="button" class="md-link-chip is-file" data-path="${escapeHtml(fullPathWithLine)}" onclick="copyFileLinkPath(this)" title="${escapeHtml(fullPathWithLine)} (클릭하여 경로 복사)">${ICONS.file}<span class="md-link-label">${escapeHtml(displayLabel)}</span>${metaHtml}</button>`;
  }

  if (isObsidian) {
    const fileParam = parsedUrl.searchParams.get('file') || '';
    const decodedFile = decodeSafeUrlPart(fileParam).replace(/^\/+|\/+$/g, '');
    const displayLabel = cleanExplicit || decodedFile || 'Obsidian 노트';
    const targetFile = decodedFile || cleanUrl;
    const jsTarget = targetFile.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    return `<button type="button" class="md-link-chip is-vault" onclick="onWikilinkClick('${escapeHtml(jsTarget)}')" title="${escapeHtml(targetFile)} (클릭 시 우측 사이드 패널에서 열람)">${ICONS.file}<span class="md-link-label">${escapeHtml(displayLabel)}</span><svg class="i ext-arrow" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>`;
  }

  const hostLower = parsedUrl.hostname.toLowerCase();
  const isObsidDomain = hostLower === 'obsid.net' || hostLower.endsWith('.obsid.net');
  const fileParam = parsedUrl.searchParams.get('file');
  const vaultParam = parsedUrl.searchParams.get('vault');
  if (fileParam && (isObsidDomain || vaultParam)) {
    const decodedPath = decodeSafeUrlPart(fileParam).replace(/^\/+|\/+$/g, '');
    const pathParts = decodedPath.split('/').filter(Boolean);
    const rawFileName = pathParts.length ? pathParts[pathParts.length - 1] : decodedPath;
    const noteNameWithExt = /\.[a-zA-Z0-9]{1,8}$/.test(rawFileName)
      ? rawFileName
      : (rawFileName ? `${rawFileName}.md` : '노트.md');
    const folderLabel = pathParts.length > 1 ? pathParts.slice(0, -1).join('/') : '';
    const fullDisplayPath = folderLabel ? `${folderLabel}/${noteNameWithExt}` : noteNameWithExt;

    const isGenericLabel = !cleanExplicit
      || /^https?:\/\//i.test(cleanExplicit)
      || /^(링크|확인\s*링크|공유\s*링크|노트\s*링크|바로가기|여기|link|url)$/i.test(cleanExplicit);
    const displayLabel = isGenericLabel ? noteNameWithExt : cleanExplicit;
    const metaText = (folderLabel && !displayLabel.includes(folderLabel)) ? folderLabel : '';
    const metaHtml = metaText
      ? `<span class="md-link-meta">· ${escapeHtml(metaText)}</span>`
      : '';
    const jsTarget = fullDisplayPath.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    return `<button type="button" class="md-link-chip is-vault" onclick="onWikilinkClick('${escapeHtml(jsTarget)}')" title="${escapeHtml(fullDisplayPath)} (클릭 시 우측 사이드 패널에서 열람)">${ICONS.file}<span class="md-link-label">${escapeHtml(displayLabel)}</span>${metaHtml}<svg class="i ext-arrow" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>`;
  }

  const host = parsedUrl.hostname.replace(/^www\./i, '');
  const isBareOrUrlLabel = !cleanExplicit || /^https?:\/\//i.test(cleanExplicit);
  let displayLabel = cleanExplicit;
  let metaText = '';
  if (!isBareOrUrlLabel) {
    if (host && !displayLabel.toLowerCase().includes(host.toLowerCase())) {
      metaText = host;
    }
  } else {
    displayLabel = host || cleanUrl;
    const decodedPath = decodeSafeUrlPart(parsedUrl.pathname).replace(/\/+$/, '');
    if (decodedPath && decodedPath !== '/') {
      metaText = decodedPath.length > 28 ? `${decodedPath.slice(0, 27)}…` : decodedPath;
    }
  }
  return renderChip(ICONS.globe, displayLabel, metaText, cleanUrl);
}

function sanitizeSpanOpenTag(attrStr) {
  const attrs = [];
  const attrRe = /([a-zA-Z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/g;
  let m;
  while ((m = attrRe.exec(attrStr || '')) !== null) {
    const name = m[1].toLowerCase();
    const rawVal = m[2] ?? m[3] ?? m[4] ?? '';
    if (name === 'style') {
      const safeDecls = [];
      for (const decl of rawVal.split(';')) {
        const colonIdx = decl.indexOf(':');
        if (colonIdx <= 0) continue;
        const prop = decl.slice(0, colonIdx).trim().toLowerCase();
        const val = decl.slice(colonIdx + 1).trim();
        if (!/^(?:color|background-color|font-weight|font-style|text-decoration|opacity|font-size)$/.test(prop)) continue;
        if (!val || /(?:url\s*\(|expression\s*\(|javascript:|behavior:|@import|-moz-binding|\/\*|\\|[<>"'])/i.test(val)) continue;
        safeDecls.push(`${prop}:${val}`);
      }
      if (safeDecls.length) {
        attrs.push(`style="${escapeHtml(safeDecls.join(';'))}"`);
      }
    } else if (name === 'class') {
      const cleanClass = rawVal.replace(/[^a-zA-Z0-9_-]+/g, ' ').trim();
      if (cleanClass) {
        attrs.push(`class="${escapeHtml(cleanClass)}"`);
      }
    }
  }
  return attrs.length ? `<span ${attrs.join(' ')}>` : '<span>';
}

function renderInline(str) {
  if (!str) return '';
  const tokens = [];
  const stashToken = html => {
    tokens.push(html);
    return `\x00T${tokens.length - 1}\x00`;
  };

  // 1. 백틱으로 감싸진 마크다운 링크나 obsid.net 공유 링크는 일반 링크로 풀어서 렌더링
  let s = String(str)
    .replace(/`\[([^\]]+)\]\(((?:https?|file|obsidian):\/\/[^`\n]+)\)`/g, '[$1]($2)')
    .replace(/`(https?:\/\/obsid\.net\/[^\s`]+)`/g, '$1');

  // 2. 마크다운 링크 ([label](url)) 우선 추출 및 프리티 링크 칩 변환 (괄호 균형 파싱 지원)
  {
    let out = '';
    let i = 0;
    while (i < s.length) {
      if (s[i] === '[') {
        const labelStart = i + 1;
        let labelEnd = -1;
        let bracketDepth = 1;
        for (let j = labelStart; j < s.length; j++) {
          if (s[j] === '\\') { j++; continue; }
          if (s[j] === '[') bracketDepth++;
          else if (s[j] === ']') {
            bracketDepth--;
            if (bracketDepth === 0) {
              labelEnd = j;
              break;
            }
          }
        }
        if (labelEnd !== -1 && s[labelEnd + 1] === '(') {
          const urlStart = labelEnd + 2;
          let urlEnd = -1;
          let parenDepth = 1;
          for (let k = urlStart; k < s.length; k++) {
            if (s[k] === '\\') { k++; continue; }
            if (s[k] === '(') parenDepth++;
            else if (s[k] === ')') {
              parenDepth--;
              if (parenDepth === 0) {
                urlEnd = k;
                break;
              }
            }
          }
          if (urlEnd !== -1) {
            const label = s.slice(labelStart, labelEnd);
            const url = s.slice(urlStart, urlEnd).trim();
            if (/^(?:(?:https?|file|obsidian|room|chat):\/\/|(?:https?|file|obsidian|room|chat):)/i.test(url)) {
              out += stashToken(buildPrettyLinkHtml(url, label));
              i = urlEnd + 1;
              continue;
            }
          }
        }
      }
      out += s[i];
      i++;
    }
    s = out;
  }

  // 3. 남은 인라인 코드 보호
  s = s.replace(/`([^`\n]+)`/g, (_, c) => stashToken(`<code>${escapeHtml(c)}</code>`));

  // 4. 꺾쇠 자동 링크 (<https://...>, <file:///...>)
  s = s.replace(/<((?:https?|file|obsidian):\/\/[^\s<>]+)>/g, (_, url) => stashToken(buildPrettyLinkHtml(url, '')));

  // 5. 평문 Bare URL (https://..., file:///...) 자동 추출 및 프리티 링크 칩 변환
  s = s.replace(/(?<![\w/])((?:https?|file):\/\/[^\s<>"]+)/g, match => {
    let url = match;
    let trailing = '';
    while (url.length > 0) {
      const punctMatch = url.match(/[.,;:!?'"]+$/);
      if (punctMatch) {
        trailing = punctMatch[0] + trailing;
        url = url.slice(0, -punctMatch[0].length);
        continue;
      }
      const unbalancedPair = [['(', ')'], ['[', ']']].find(([openCh, closeCh]) => {
        if (!url.endsWith(closeCh)) return false;
        const opens = url.split(openCh).length - 1;
        const closes = url.split(closeCh).length - 1;
        return closes > opens;
      });
      if (unbalancedPair) {
        trailing = unbalancedPair[1] + trailing;
        url = url.slice(0, -1);
        continue;
      }
      break;
    }
    if (!/^(?:https?:\/\/[^/\s]+|file:\/\/\/[^\s]+)/i.test(url)) {
      return match;
    }
    return stashToken(buildPrettyLinkHtml(url, '')) + trailing;
  });

  // 6. 위키링크 ([[노트명|별칭]])
  s = s.replace(/\[\[([^\]|]+)(?:\\?\|([^\]]+))?\]\]/g, (_, target, alias) => {
    const cleanTarget = String(target || '').split('#')[0].trim();
    const jsEscaped = cleanTarget.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    return stashToken(`<span class="wikilink" onclick="onWikilinkClick('${escapeHtml(jsEscaped)}')">${escapeHtml(alias || target)}</span>`);
  });

  // 6.25. 대괄호 대화방/채팅 세션 참조 ([방제목] 또는 [room:roomId]) 칩 변환
  s = s.replace(/(?<!\[)\[([^\[\]\r\n]{2,100})\](?!\])/g, (fullMatch, candidate) => {
    const trimmed = String(candidate || '').trim();
    if (!trimmed) return fullMatch;

    // 시스템/마크다운 예약 태그, 체크박스, 각주, 일반 상태 태그 필터링
    if (/^(?:[ xX\^_]|\d+|!?(?:NOTE|TIP|IMPORTANT|WARNING|WARN|CAUTION|TODO|DONE|PASS|FAIL|ERROR|INFO)|하위 작업 완료 보고|하위 작업 중단\/오류 보고|System Event.*)$/i.test(trimmed)) {
      return fullMatch;
    }

    // 명시적 접두사 room: 또는 chat: 형태 ([room:room-xxx])
    if (/^(?:room|chat):/i.test(trimmed)) {
      const idOrName = trimmed.replace(/^(?:room|chat):\/\/?/i, '').trim();
      const matched = findChatRoom(idOrName);
      return stashToken(buildChatRoomChipHtml(matched, matched ? matched.title : idOrName));
    }

    // 방 ID 형태 (room-...)
    if (/^room-[a-zA-Z0-9_\-]+$/i.test(trimmed)) {
      const matched = findChatRoom(trimmed);
      return stashToken(buildChatRoomChipHtml(matched, matched ? matched.title : trimmed));
    }

    // [방제목 (Room ID: room-xxx)] 또는 [방제목 (ID: room-xxx)] 패턴
    const idPatternMatch = trimmed.match(/^(.*?)\s*\((?:Room\s+)?ID:\s*(room-[a-zA-Z0-9_\-]+)\)$/i);
    if (idPatternMatch) {
      const titlePart = idPatternMatch[1].trim();
      const idPart = idPatternMatch[2].trim();
      const matched = findChatRoom(idPart) || findChatRoom(titlePart);
      return stashToken(buildChatRoomChipHtml(matched || { id: idPart, title: titlePart }, titlePart || idPart));
    }

    // 대화방 검색 (제목/ID/별칭 매칭)
    const matchedRoom = findChatRoom(trimmed);
    if (matchedRoom) {
      return stashToken(buildChatRoomChipHtml(matchedRoom, trimmed));
    }

    return fullMatch;
  });

  // 6.5. 허용된 인라인 <span ...> / </span> 및 <br> 줄바꿈 태그 보호
  s = s.replace(/<span(\s+[^<>]*)?>/gi, (_, attrStr) => stashToken(sanitizeSpanOpenTag(attrStr || '')));
  s = s.replace(/<\/span\s*>/gi, () => stashToken('</span>'));
  s = s.replace(/<br\s*\/?>/gi, () => stashToken('<br>'));

  // 7. 남은 일반 텍스트 HTML 이스케이프 및 인라인 서식 적용
  s = escapeHtml(s)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>');

  // 8. 토큰 복원
  return s.replace(/\x00T(\d+)\x00/g, (_, i) => tokens[Number(i)]);
}

async function onWikilinkClick(noteTarget) {
  const target = String(noteTarget || '').trim();
  if (!target) return;
  if (!vaultTreeCache || vaultTreeCache.length === 0) {
    try {
      const res = await fetch('/api/vault/tree');
      const data = await res.json();
      vaultTreeCache = data.items || data.tree || [];
    } catch (_) {}
  }
  const files = flattenVaultFiles(vaultTreeCache);
  const norm = target.toLowerCase().replace(/\.md$/, '');
  const matched = files.find(f => {
    const fname = String(f.name || '').toLowerCase().replace(/\.md$/, '');
    const fpath = String(f.path || '').toLowerCase().replace(/\.md$/, '');
    return fname === norm || fpath === norm || fpath.endsWith('/' + norm);
  });
  if (matched && matched.path) {
    if (typeof currentView !== 'undefined' && currentView === 'vault' && typeof openVaultFile === 'function') {
      openVaultFile(matched.path);
    } else if (typeof openNoteViewer === 'function') {
      await openNoteViewer(matched.path);
    } else {
      switchView('vault');
      openVaultFile(matched.path);
    }
  } else {
    if (typeof openNoteViewer === 'function' && (target.includes('/') || target.endsWith('.md'))) {
      await openNoteViewer(target);
      return;
    }
    const input = document.getElementById('chat-input');
    if (input) {
      setChatInputText(`[[${target}]] 노트의 내용을 확인해서 요약해줘.`);
      onChatInput();
      try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
      showToast(`'${target}' 노트를 질문 입력창에 담았습니다.`);
    }
  }
}

function formatMarkdown(text) {
  if (!text) return '';
  const blocks = [];
  const normalized = String(text)
    .replace(/\r\n/g, '\n')
    .replace(/\[([^\]]+)\]\s*\n\s*\(((?:https?|file|obsidian):\/\/[^)\n]+)\)/g, '[$1]($2)');
  const src = normalized.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)(?:```|$)/g, (_, lang, code) => {
    const idx = blocks.length;
    const clean = escapeHtml(code.replace(/\n$/, ''));
    const langLabel = escapeHtml(lang || 'code');
    blocks.push(`<pre><div class="code-head"><span>${langLabel}</span><button type="button" onclick="copyCodeBlock(this)">복사</button></div><code>${clean}</code></pre>`);
    return `\n\x00B${idx}\x00\n`;
  });

  const lines = src.split('\n');
  const out = [];
  let i = 0;

  const isHeadingLine = l => /^\s*(#{1,6})(?:\s+(.*))?$/.test(l);
  const isListLine = l => /^\s*([-*•]|\d+\.)\s+/.test(l);
  const isTableStart = idx => (
    idx + 1 < lines.length &&
    lines[idx].trim().startsWith('|') &&
    /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[idx + 1])
  );

  while (i < lines.length) {
    const startIdx = i;
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) { i++; continue; }

    const blockMatch = trimmed.match(/^\x00B(\d+)\x00$/);
    if (blockMatch) { out.push(blocks[Number(blockMatch[1])]); i++; continue; }

    if (/^([-*_])\1{2,}$/.test(trimmed)) { out.push('<hr>'); i++; continue; }

    const hMatch = line.match(/^\s*(#{1,6})(?:\s+(.*))?$/);
    if (hMatch) {
      const lv = hMatch[1].length;
      const hText = (hMatch[2] || '').trim();
      if (hText) {
        out.push(`<h${lv}>${renderInline(hText)}</h${lv}>`);
      }
      i++; continue;
    }

    if (isTableStart(i)) {
      const parseRow = r => r.trim().replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map(c => renderInline(c.replace(/\\\|/g, '|').trim()));
      const headers = parseRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        rows.push(parseRow(lines[i]));
        i++;
      }
      const thHtml = headers.map(h => `<th>${h}</th>`).join('');
      const tbHtml = rows.map(r => `<tr>${headers.map((_, idx) => `<td>${r[idx] || ''}</td>`).join('')}</tr>`).join('');
      out.push(`<div class="table-wrap"><table><thead><tr>${thHtml}</tr></thead><tbody>${tbHtml}</tbody></table></div>`);
      continue;
    }

    if (trimmed.startsWith('>')) {
      const qLines = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        qLines.push(renderInline(lines[i].trim().replace(/^>\s?/, '')));
        i++;
      }
      out.push(`<blockquote>${qLines.join('<br>')}</blockquote>`);
      continue;
    }

    if (isListLine(line)) {
      const isOl = /^\s*\d+\.\s+/.test(line);
      const tag = isOl ? 'ol' : 'ul';
      const items = [];
      while (i < lines.length && isListLine(lines[i])) {
        const rawItem = lines[i].replace(/^\s*([-*•]|\d+\.)\s+/, '');
        const taskMatch = rawItem.match(/^\[([ xX])\]\s+(.*)$/);
        if (taskMatch) {
          const done = taskMatch[1].toLowerCase() === 'x';
          items.push(`<li class="task ${done ? 'done' : ''}"><span class="cb"></span>${renderInline(taskMatch[2])}</li>`);
        } else {
          items.push(`<li>${renderInline(rawItem)}</li>`);
        }
        i++;
      }
      out.push(`<${tag}>${items.join('')}</${tag}>`);
      continue;
    }

    const pLines = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\x00B\d+\x00$/.test(lines[i].trim()) &&
      !isHeadingLine(lines[i]) &&
      !/^([-*_])\1{2,}$/.test(lines[i].trim()) &&
      !lines[i].trim().startsWith('>') &&
      !isListLine(lines[i]) &&
      !isTableStart(i)
    ) {
      pLines.push(renderInline(lines[i]));
      i++;
    }
    if (i === startIdx) {
      pLines.push(renderInline(lines[i]));
      i++;
    }
    if (pLines.length) out.push(`<p>${pLines.join('<br>')}</p>`);
  }

  return out.join('');
}

function copyCodeBlock(btn) {
  const pre = btn && btn.closest('pre');
  const code = pre && pre.querySelector('code');
  if (!code) return;
  copyToClipboard(code.innerText, '코드가');
}
