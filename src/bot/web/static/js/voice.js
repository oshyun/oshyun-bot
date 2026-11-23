// ─── 세션 중단 및 음성 입력 (Web Speech API & TTS) ───
function abortCurrentSession(roomId = currentRoomId) {
  currentSpeechTranscript = '';
  hasDispatchedSpeech = true;
  stopVoiceRecording(true);
  const inputEl = document.getElementById('chat-input');
  if (inputEl) { isImeComposing = false; onChatInput(); }

  if (currentAudio) {
    try { currentAudio.pause(); } catch (_) {}
    currentAudio = null;
  }

  if (socket && socket.readyState === WebSocket.OPEN) {
    try {
      socket.send(JSON.stringify({ type: 'cancel', roomId: roomId, terminateSession: false }));
    } catch (_) {}
  }

  const targetRoom = getRoomById(roomId) || getCurrentRoom();
  if (targetRoom && Array.isArray(targetRoom.messages)) {
    targetRoom.messages = targetRoom.messages.filter(m => !(m && m.queued));
    saveChatRoomsToStorage();
  }

  const liveState = roomLiveStates[roomId];
  if (liveState) {
    liveState.isRunning = false;
    liveState.feed = '';
    liveState.steps = [];
    liveState.streamText = '';
    liveState.startedAt = 0;
    liveState.queueCount = 0;
  }
  syncLiveElapsedTimer();

  setAgentAvatarStatus('idle', '대기 중');
  setOrbState('idle', '중단됨');
  renderMessagesForCurrentRoom();
  renderRoomList();
  updateSendButtonState();
  showToast('생성을 중단했습니다.');
}

function tryStartVoice(silentFailure = false) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    if (!silentFailure) showToast('이 브라우저는 웹 음성 인식을 지원하지 않습니다. 키보드 받아쓰기를 이용해 주세요.');
    return;
  }
  if (currentAudio) {
    try { currentAudio.pause(); } catch (_) {}
    currentAudio = null;
  }
  startVoiceRecording(SpeechRecognition, silentFailure);
}

function toggleVoiceRecording() {
  // 음성 녹음 토글 시 가상 키보드가 강제로 올라오지 않도록 포커스 강제 호출 제거
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    showToast('키보드의 마이크(받아쓰기) 버튼을 눌러 말씀해 주세요.');
    return;
  }
  if (isVoiceRecording) stopVoiceRecording(false);
  else startVoiceRecording(SpeechRecognition);
}

function startVoiceRecording(SpeechCtor, silentFailure = false) {
  try {
    const liveState = ensureRoomLiveState(currentRoomId);
    if (liveState && liveState.isRunning) {
      if (!silentFailure) showToast('답변 생성 중입니다. 잠시 후 말씀해 주세요.');
      return;
    }
    if (recognition) {
      stopVoiceRecording(true);
    }

    currentSpeechTranscript = '';
    hasDispatchedSpeech = false;

    recognition = new SpeechCtor();
    recognition.lang = 'ko-KR';
    recognition.continuous = false;
    recognition.interimResults = true;

    recognition.onstart = () => {
      isVoiceRecording = true;
      currentSpeechTranscript = '';
      hasDispatchedSpeech = false;
      document.getElementById('btn-mic')?.classList.add('active');
      setChatInputPlaceholder('듣고 있습니다… 말씀해 주세요');
      setOrbState('listening', '듣고 있습니다…');
    };

    recognition.onresult = (e) => {
      if (hasDispatchedSpeech || !isVoiceRecording) return;
      let finalText = '', interimText = '';
      for (let i = 0; i < e.results.length; ++i) {
        const piece = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += piece;
        else interimText += piece;
      }
      const rawText = (finalText || interimText).trim();
      if (rawText) {
        currentSpeechTranscript = rawText;
        setChatInputText(rawText);
        onChatInput();
      }
    };

    recognition.onend = () => {
      const val = (currentSpeechTranscript.trim() || getChatInputText().trim());
      currentSpeechTranscript = '';
      stopVoiceRecording(true);
      if (hasDispatchedSpeech) {
        setChatInputText(''); onChatInput();
        return;
      }
      if (val) {
        hasDispatchedSpeech = true;
        setChatInputText(''); onChatInput();
        sendUserMessage(val, true);
      }
    };

    recognition.onerror = (e) => {
      stopVoiceRecording(true);
      const errCode = (e && e.error) ? e.error : '';
      if (errCode === 'not-allowed') {
        showToast('마이크 접근 권한이 필요합니다.');
      }
    };

    recognition.start();
  } catch (e) {
    stopVoiceRecording(true);
    if (!silentFailure) showToast('마이크 시작 실패');
  }
}

function stopVoiceRecording(discardHandlers = false) {
  isVoiceRecording = false;
  document.getElementById('btn-mic')?.classList.remove('active');
  setChatInputPlaceholder('무엇이든 물어보세요');
  if (recognition) {
    const rec = recognition;
    if (discardHandlers) {
      recognition = null;
      rec.onstart = null;
      rec.onresult = null;
      rec.onend = null;
      rec.onerror = null;
    }
    try { rec.stop(); } catch (_) {}
    if (!discardHandlers) recognition = null;
  }
  const room = getCurrentRoom();
  const liveState = room ? roomLiveStates[room.id] : null;
  if (!liveState || !liveState.isRunning) {
    setOrbState('idle', '준비 완료');
  }
}

async function playTts(text, overrideVoice) {
  if (!text || !text.trim()) return;
  if (currentAudio) {
    try { currentAudio.pause(); } catch (_) {}
  }
  const restoreOrbAfterTts = () => {
    const curRoom = getCurrentRoom();
    const curLive = curRoom ? roomLiveStates[curRoom.id] : null;
    if (curLive && curLive.isRunning) {
      setOrbState('thinking', curLive.feed || '생각하는 중…');
    } else {
      setOrbState('idle', '준비 완료');
    }
  };
  try {
    setOrbState('speaking', '음성으로 답변 중…');
    const voiceSelect = document.getElementById('select-tts-voice');
    const selectedVoice = overrideVoice || (voiceSelect ? voiceSelect.value : '') || 'ko-KR-SunHiNeural';
    const res = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice: selectedVoice })
    });
    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      currentAudio = new Audio(url);
      currentAudio.onended = restoreOrbAfterTts;
      currentAudio.onerror = restoreOrbAfterTts;
      currentAudio.play();
    } else {
      restoreOrbAfterTts();
    }
  } catch (_) {
    restoreOrbAfterTts();
  }
}
