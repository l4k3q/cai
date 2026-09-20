/* ══════════════════════════════════════════════════════════════
   CAI//OPS — front-end controller
   Talks to the CAI FastAPI backend (same origin).
   No dependencies. Vanilla JS.
   ══════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API_KEY_STORE = 'cai_api_key';
  const DEFAULT_BASE_URL = 'https://api.deepseek.com';

  /* ── DOM refs ─────────────────────────────────────────────── */
  const $ = (id) => document.getElementById(id);
  const els = {
    healthBadge: $('health-badge'), healthDot: $('health-dot'), healthText: $('health-text'),
    btnNew: $('btn-new-session'), btnSettings: $('btn-settings'), btnBank: $('btn-bank'),
    sessionList: $('session-list'),
    agentCount: $('agent-count'), modelCount: $('model-count'),
    agentList: $('agent-list'), modelList: $('model-list'),
    chatHeadAgent: $('chat-agent'), chatHeadModel: $('chat-model'), chatState: $('chat-state'),
    chatLog: $('chat-log'),
    composer: $('composer'), input: $('input'), btnSend: $('btn-send'), btnStop: $('btn-stop'),
    composerHint: $('composer-hint'),
    btnAttach: $('btn-attach'), chatFiles: $('chat-files'), composerAttachments: $('composer-attachments'),
    traceLog: $('trace-log'), traceLive: $('trace-live'),
    modelStateCard: $('model-state-card'), modelStateBadge: $('model-state-badge'),
    modelStateLabel: $('model-state-label'), modelStateThinking: $('model-state-thinking'),
    modelStateThinkingMeta: $('model-state-thinking-meta'), modelStateAgent: $('model-state-agent'),
    modelStateModel: $('model-state-model'), modelStateElapsed: $('model-state-elapsed'),
    modalNew: $('modal-new'), selAgent: $('sel-agent'), selModel: $('sel-model'),
    chkStateful: $('chk-stateful'), btnCreate: $('btn-create-session'), btnCancelModal: $('btn-cancel-modal'),
    inpBaseUrl: $('inp-base-url'), inpProviderKey: $('inp-provider-key'),
    btnFetchModels: $('btn-fetch-models'), fetchStatus: $('fetch-status'),
    modalSettings: $('modal-settings'), inpApiKey: $('inp-api-key'),
    btnSaveSettings: $('btn-save-settings'), btnCloseSettings: $('btn-close-settings'),
    modalBank: $('modal-bank'), bankAgent: $('bank-agent'), bankModel: $('bank-model'),
    bankBaseUrl: $('bank-base-url'), bankApiKey: $('bank-api-key'),
    btnBankFetch: $('btn-bank-fetch'), bankFetchStatus: $('bank-fetch-status'),
    bankProblem: $('bank-problem'), bankRefs: $('bank-refs'), bankStatus: $('bank-status'),
    btnCancelBank: $('btn-cancel-bank'), btnBuildBank: $('btn-build-bank'),
    modalBankResult: $('modal-bank-result'), bankResultTitle: $('bank-result-title'),
    bankResultBody: $('bank-result-body'), btnCloseBankResult: $('btn-close-bank-result'),
    btnQBank: $('btn-qbank'), modalQBank: $('modal-qbank'),
    qbankStats: $('qbank-stats'), qbankList: $('qbank-list'),
    qbankStatement: $('qbank-statement'), qbankCriteria: $('qbank-criteria'),
    qbankAgent: $('qbank-agent'), qbankModel: $('qbank-model'), qbankBaseUrl: $('qbank-base-url'), qbankApiKey: $('qbank-api-key'),
    btnQBankFetch: $('btn-qbank-fetch'), qbankFetchStatus: $('qbank-fetch-status'),
    btnQBankCreate: $('btn-qbank-create'), qbankCreateStatus: $('qbank-create-status'),
    btnQBankRefresh: $('btn-qbank-refresh'), btnCancelQBank: $('btn-cancel-qbank'),
    qbankFiles: $('qbank-files'),
    modalQBankDetail: $('modal-qbank-detail'), qbankDetailTitle: $('qbank-detail-title'),
    qbankDetailBody: $('qbank-detail-body'), btnCloseQBankDetail: $('btn-close-qbank-detail'),
    spectatorWrap: $('spectator-wrap'), spectatorClose: $('spectator-close'),
    spectatorTopic: $('spectator-topic'), spectatorCard: $('spectator-card'),
    spectatorBaseUrl: $('spectator-base-url'), spectatorApiKey: $('spectator-api-key'),
    spectatorModel: $('spectator-model'), spectatorStatus: $('spectator-status'),
    spectatorFetch: $('btn-spectator-fetch'), spectatorFetchStatus: $('spectator-fetch-status'),
    chkSpectatorClearKey: $('chk-spectator-clear-key'),
    spectatorClearAll: $('btn-spectator-clear-all'),
  };

  /* ── state ────────────────────────────────────────────────── */
  const state = {
    sessions: [],
    agents: [],
    models: [],
    currentId: null,
    running: false,
    abort: null,
    modelState: {
      state: 'idle',
      label: '空闲',
      agent: '—',
      model: '—',
      elapsed_seconds: 0,
      thinking: { mode: 'not_applicable', effort: '', source: '' },
    },
    modelStateStartedAt: null,
    modelStateTimer: null,
    modelStateServerSeen: false,
    cancelRequested: false,
    sessionResolved: false,  // §6.1: whether question resolution has run for current session
    attachments: [],         // 对话框待上传的附件（File 对象）
    // ── 旁观 agent（specs/001-spectator-agent）──
    spectatorCardData: null,     // 当前渲染的卡片
    spectatorDismissed: new Set(), // 本会话内已本地忽略的 card_id（不再渲染）
    spectatorPollTimer: null,
    spectatorPollCount: 0,
  };

  /* ══════════════════ API client ══════════════════ */
  function headers() {
    const h = { 'Content-Type': 'application/json' };
    const key = localStorage.getItem(API_KEY_STORE);
    if (key) h['X-CAI-API-Key'] = key;
    return h;
  }
  async function api(path, opts = {}) {
    let res;
    try {
      res = await fetch(path, Object.assign({ headers: headers() }, opts));
    } catch (e) {
      throw new Error('网络请求失败：无法连接到服务器，请检查 API 是否已启动（' + e.message + '）');
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const d = (await res.json()).detail;
        if (Array.isArray(d)) {
          // FastAPI 422 returns detail as an array of {loc, msg, type}
          detail = d.map((e) => {
            const field = (e.loc || []).slice(1).join('.');
            return (field ? field + '：' : '') + (e.msg || '格式错误');
          }).join('；');
        } else if (d != null && d !== '') {
          detail = d;
        }
      } catch (_) {}
      // Actionable messages per status code (clarify + harden)
      const hint = { 401: '请检查 API 密钥是否正确', 403: '权限不足', 404: '资源不存在', 429: '请求过于频繁，请稍后重试', 500: '服务器内部错误，请查看后端日志', 502: '网关错误，模型服务可能不可用', 503: '服务暂时不可用，请稍后重试' };
      const suffix = hint[res.status] ? ' — ' + hint[res.status] : '';
      throw new Error(`${res.status} ${detail}${suffix}`);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    return ct.includes('json') ? res.json() : res.text();
  }

  /* ══════════════════ helpers ══════════════════ */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function contentToText(content) {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content
        .map((p) => {
          if (!p) return '';
          if (p.type === 'text' || p.type === 'input_text' || p.type === 'output_text') return p.text;
          return '';
        })
        .filter(Boolean)
        .join('\n');
    }
    return '';
  }
  function renderAssistantMarkdown(element, markdown) {
    const source = String(markdown == null ? '' : markdown);
    if (window.marked && typeof window.marked.parse === 'function') {
      element.innerHTML = window.marked.parse(source);
    } else {
      element.textContent = source;
    }
  }
  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleString('zh-CN', { hour12: false });
  }
  function agentDisplay(idOrName) {
    if (!idOrName) return 'agent';
    const hit = state.agents.find((a) => (a.id || a.name) === idOrName);
    return hit ? hit.name : idOrName;
  }
  function newEl(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* ══════════════════ model state ═══════════════════════════ */
  const MODEL_STATE_VALUES = new Set([
    'preparing', 'thinking', 'generating', 'tool_running',
    'completed', 'failed', 'cancelled', 'idle',
  ]);
  const ACTIVE_MODEL_STATE_VALUES = new Set(['preparing', 'thinking', 'generating', 'tool_running']);
  const MODEL_STATE_TITLES = Object.freeze({
    preparing: '准备中',
    thinking: '思考中',
    generating: '生成中',
    tool_running: '工具运行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    idle: '空闲',
  });
  const THINKING_MODE_VALUES = new Set(['enabled', 'disabled', 'not_applicable']);
  const THINKING_MODE_TITLES = Object.freeze({
    enabled: '已开启',
    disabled: '已关闭',
    not_applicable: '不适用',
  });

  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function boundedText(value, fallback, maxLength) {
    if (value == null) return fallback;
    const text = String(value).trim();
    if (!text) return fallback;
    if (maxLength && text.length > maxLength) return text.slice(0, maxLength - 1) + '…';
    return text;
  }

  function parseElapsedSeconds(value) {
    if (value == null || value === '') return null;
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return null;
    return Math.floor(seconds);
  }

  function currentSessionMeta() {
    const session = state.sessions.find((item) => item.id === state.currentId);
    if (session) {
      return {
        agent: boundedText(session.agent ? agentDisplay(session.agent) : '', '—', 120),
        model: boundedText(session.model, '—', 160),
      };
    }
    const chatAgent = els.chatHeadAgent && els.chatHeadAgent.textContent.trim();
    const chatModel = els.chatHeadModel && els.chatHeadModel.textContent.trim();
    return {
      agent: chatAgent && chatAgent !== '未选择 Agent' && chatAgent !== '—' ? boundedText(chatAgent, '—', 120) : '—',
      model: chatModel && chatModel !== '—' ? boundedText(chatModel, '—', 160) : '—',
    };
  }

  function normalizeThinking(thinking, previousThinking) {
    const incoming = thinking && typeof thinking === 'object' ? thinking : {};
    const previous = previousThinking && typeof previousThinking === 'object' ? previousThinking : {};
    const mode = THINKING_MODE_VALUES.has(incoming.mode)
      ? incoming.mode
      : (THINKING_MODE_VALUES.has(previous.mode) ? previous.mode : 'not_applicable');
    return {
      mode: mode,
      effort: boundedText(hasOwn(incoming, 'effort') ? incoming.effort : previous.effort, '', 40),
      source: boundedText(hasOwn(incoming, 'source') ? incoming.source : previous.source, '', 80),
    };
  }

  function formatElapsed(seconds) {
    const total = Math.max(0, parseElapsedSeconds(seconds) || 0);
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    return minutes ? `${minutes}m ${String(remainder).padStart(2, '0')}s` : `${total}s`;
  }

  function activeElapsedSeconds() {
    const current = state.modelState || {};
    const base = parseElapsedSeconds(current.elapsed_seconds) || 0;
    if (!ACTIVE_MODEL_STATE_VALUES.has(current.state) || state.modelStateStartedAt == null) return base;
    return Math.max(base, Math.floor((Date.now() - state.modelStateStartedAt) / 1000));
  }

  function stopModelStateTimer() {
    if (state.modelStateTimer) {
      clearInterval(state.modelStateTimer);
      state.modelStateTimer = null;
    }
  }

  function refreshModelStateElapsed() {
    if (!ACTIVE_MODEL_STATE_VALUES.has(state.modelState.state)) {
      stopModelStateTimer();
      return;
    }
    if (els.modelStateElapsed) els.modelStateElapsed.textContent = formatElapsed(activeElapsedSeconds());
  }

  function syncModelStateTimer() {
    if (ACTIVE_MODEL_STATE_VALUES.has(state.modelState.state)) {
      if (!state.modelStateTimer) state.modelStateTimer = setInterval(refreshModelStateElapsed, 1000);
    } else {
      stopModelStateTimer();
    }
  }

  function renderModelState() {
    if (!els.modelStateCard) return;
    const current = state.modelState;
    const stateName = MODEL_STATE_VALUES.has(current.state) ? current.state : 'idle';
    const thinking = normalizeThinking(current.thinking);
    const stateTitle = MODEL_STATE_TITLES[stateName];
    els.modelStateCard.dataset.state = stateName;
    els.modelStateBadge.className = 'model-state-badge model-state-' + stateName;
    els.modelStateBadge.textContent = stateTitle;
    els.modelStateLabel.textContent = boundedText(current.label, stateTitle, 100);
    els.modelStateAgent.textContent = boundedText(current.agent, '—', 120);
    els.modelStateModel.textContent = boundedText(current.model, '—', 160);
    els.modelStateThinking.className = 'model-state-thinking model-state-thinking-' + thinking.mode;
    els.modelStateThinking.dataset.mode = thinking.mode;
    els.modelStateThinking.textContent = THINKING_MODE_TITLES[thinking.mode];
    const thinkingMeta = [];
    if (thinking.effort) thinkingMeta.push('强度：' + thinking.effort);
    if (thinking.source) thinkingMeta.push('来源：' + thinking.source);
    els.modelStateThinkingMeta.textContent = thinkingMeta.join(' · ');
    els.modelStateElapsed.textContent = formatElapsed(activeElapsedSeconds());
    syncModelStateTimer();
  }

  function resetModelState(agent, model) {
    stopModelStateTimer();
    const meta = currentSessionMeta();
    state.modelStateServerSeen = false;
    state.cancelRequested = false;
    state.modelStateStartedAt = null;
    state.modelState = {
      state: 'idle',
      label: '空闲',
      agent: boundedText(agent, meta.agent, 120),
      model: boundedText(model, meta.model, 160),
      elapsed_seconds: 0,
      thinking: { mode: 'not_applicable', effort: '', source: '' },
    };
    renderModelState();
  }

  function applyModelState(data, authoritative) {
    const incoming = data && typeof data === 'object' && !Array.isArray(data) ? data : {};
    const previous = state.modelState;
    const meta = currentSessionMeta();
    const nextState = MODEL_STATE_VALUES.has(incoming.state) ? incoming.state : null;
    if (!nextState) return false;
    const now = Date.now();
    const explicitElapsed = parseElapsedSeconds(incoming.elapsed_seconds);
    const previousElapsed = activeElapsedSeconds();
    const elapsed = explicitElapsed == null ? previousElapsed : explicitElapsed;
    const nextThinking = normalizeThinking(
      hasOwn(incoming, 'thinking') ? incoming.thinking : previous.thinking,
      previous.thinking,
    );
    const nextAgent = boundedText(
      hasOwn(incoming, 'agent') ? incoming.agent : previous.agent,
      meta.agent,
      120,
    );
    const nextModel = boundedText(
      hasOwn(incoming, 'model') ? incoming.model : previous.model,
      meta.model,
      160,
    );
    state.modelStateServerSeen = authoritative || state.modelStateServerSeen;
    state.modelState = {
      state: nextState,
      label: boundedText(incoming.label, MODEL_STATE_TITLES[nextState], 100),
      agent: nextAgent,
      model: nextModel,
      elapsed_seconds: elapsed,
      thinking: nextThinking,
    };
    if (ACTIVE_MODEL_STATE_VALUES.has(nextState)) {
      if (explicitElapsed != null || !ACTIVE_MODEL_STATE_VALUES.has(previous.state) || state.modelStateStartedAt == null) {
        state.modelStateStartedAt = now - (elapsed * 1000);
      }
    } else {
      state.modelStateStartedAt = null;
    }
    renderModelState();
    return true;
  }

  function fallbackThinkingState() {
    const mode = state.modelState.thinking && state.modelState.thinking.mode;
    return mode === 'disabled'
      ? { state: 'generating', label: '模型正在生成回答' }
      : { state: 'thinking', label: '模型正在思考' };
  }

  function applyFallbackModelState(stateName, label, fields) {
    applyModelState(Object.assign({ state: stateName, label: label }, fields || {}), false);
  }

  function consumeModelStateEvent(event, data) {
    if (event === 'model_state') {
      applyModelState(data, true);
      return;
    }
    if (event === 'status') {
      if (state.modelStateServerSeen) return;
      const statusType = data && typeof data === 'object' ? data.type : '';
      if (data && typeof data === 'object' && MODEL_STATE_VALUES.has(data.state)) {
        applyFallbackModelState(data.state, data.label || MODEL_STATE_TITLES[data.state], data);
      } else if (statusType === 'started') {
        applyFallbackModelState('preparing', '正在准备模型请求');
      } else {
        applyFallbackModelState('thinking', '模型正在处理中');
      }
      return;
    }
    if (event === 'reasoning_step') {
      const step = data && typeof data === 'object' ? data : {};
      if (step.type === 'tool_call') {
        applyFallbackModelState('tool_running', 'Agent 正在调用工具', { agent: step.agent });
      } else if (step.type === 'tool_output') {
        const next = fallbackThinkingState();
        applyFallbackModelState(next.state, next.label, { agent: step.agent });
      } else if (step.type === 'message') {
        applyFallbackModelState('generating', '模型正在生成回答', { agent: step.agent });
      } else if (step.type === 'agent_switched') {
        applyFallbackModelState('thinking', '模型正在思考', { agent: step.agent });
      } else if (step.type === 'handoff') {
        applyFallbackModelState('thinking', '模型正在思考', { agent: step.to_agent });
      }
      return;
    }
    if (event === 'final') {
      applyFallbackModelState('completed', '回答已完成');
      return;
    }
    if (event === 'error') {
      const message = data && typeof data === 'object' ? data.message : String(data || '');
      const cancelled = state.cancelRequested || state.modelState.state === 'cancelled' || /取消|cancel/i.test(message);
      applyFallbackModelState(cancelled ? 'cancelled' : 'failed', cancelled ? '任务已取消' : '模型调用失败');
    }
  }

  /* ══════════════════ health ══════════════════ */
  async function loadHealth() {
    try {
      const h = await api('/api/v1/health');
      els.healthBadge.className = 'health-badge ok';
      els.healthText.textContent = '已连接 · v' + h.version;
    } catch (e) {
      els.healthBadge.className = 'health-badge err';
      els.healthText.textContent = '未连接 — 请检查服务是否启动';
    }
  }

  /* ══════════════════ catalogs ══════════════════ */
  async function loadAgents() {
    try {
      const { agents } = await api('/api/v1/agents');
      state.agents = agents;
      els.agentCount.textContent = agents.length;
      els.agentList.innerHTML = '';
      if (!agents.length) els.agentList.appendChild(newEl('li', 'empty-hint', '无可用 agent'));
      agents.forEach((a) => {
        const li = newEl('li', 'agent-item');
        li.appendChild(newEl('div', 'agent-name', a.name || '(unnamed)'));
        if (a.description) li.appendChild(newEl('div', 'agent-desc', a.description));
        if (a.tools && a.tools.length) {
          const tools = newEl('div', 'agent-tools');
          a.tools.forEach((t) => tools.appendChild(newEl('span', 'tool-chip', t.name)));
          li.appendChild(tools);
        }
        els.agentList.appendChild(li);
      });
    } catch (e) {
      els.agentCount.textContent = '!';
      els.agentList.innerHTML = `<li class="empty-hint">${esc(e.message)}</li>`;
    }
  }
  async function loadModels() {
    try {
      const { models } = await api('/api/v1/models');
      state.models = models;
      els.modelCount.textContent = models.length;
      els.modelList.innerHTML = '';
      if (!models.length) els.modelList.appendChild(newEl('li', 'empty-hint', '无可用模型'));
      models.forEach((m) => {
        const li = newEl('li', 'model-item');
        li.appendChild(newEl('div', 'model-provider', (m.provider || 'provider').toUpperCase()));
        li.appendChild(newEl('div', 'model-name', m.name));
        els.modelList.appendChild(li);
      });
    } catch (e) {
      els.modelCount.textContent = '!';
      els.modelList.innerHTML = `<li class="empty-hint">${esc(e.message)}</li>`;
    }
  }

  /* ══════════════════ sessions ══════════════════ */
  async function loadSessions() {
    try {
      const { sessions } = await api('/api/v1/sessions');
      state.sessions = sessions;
      renderSessionList();
      if (!state.currentId && sessions.length) selectSession(sessions[0].id);
    } catch (e) {
      els.sessionList.innerHTML = `<li class="empty-hint">${esc(e.message)}</li>`;
    }
  }
  function renderSessionList() {
    els.sessionList.innerHTML = '';
    if (!state.sessions.length) {
      els.sessionList.appendChild(newEl('li', 'empty-hint', '暂无会话 · 点击新建会话'));
      return;
    }
    state.sessions.forEach((s) => {
      const li = newEl('li', 'session-item' + (s.id === state.currentId ? ' active' : ''));
      const row = newEl('div', 'session-row');
      const info = newEl('div', 'session-info');
      info.appendChild(newEl('div', 'session-name', agentDisplay(s.agent)));
      const meta = `${s.model || ''} · ${s.history_length} msgs\n${fmtTime(s.updated_at)}`;
      info.appendChild(newEl('div', 'session-meta', meta));
      row.appendChild(info);
      const del = newEl('button', 'session-del', '×');
      del.title = '删除会话';
      del.addEventListener('click', (ev) => { ev.stopPropagation(); deleteSession(s.id); });
      row.appendChild(del);
      li.appendChild(row);
      li.addEventListener('click', () => selectSession(s.id));
      els.sessionList.appendChild(li);
    });
  }
  async function createSession(agent, model, stateful, baseUrl, apiKey) {
    try {
      const s = await api('/api/v1/sessions', {
        method: 'POST',
        body: JSON.stringify({ agent, model, stateful, base_url: baseUrl, api_key: apiKey }),
      });
      await loadSessions();
      selectSession(s.id);
      closeModal();
    } catch (e) {
      alert('创建会话失败：' + e.message);
    }
  }
  async function deleteSession(id) {
    if (!confirm('确定删除该会话及其历史？')) return;
    try {
      await api('/api/v1/sessions/' + encodeURIComponent(id), { method: 'DELETE' });
      if (state.currentId === id) {
        state.currentId = null;
        clearChat();
      }
      await loadSessions();
    } catch (e) {
      alert('删除失败：' + e.message);
    }
  }
  async function selectSession(id) {
    if (state.running) { alert('任务运行中，请先点击停止按钮'); return; }
    if (state.currentId === id) return;
    state.currentId = id;
    state.sessionResolved = false;
    spectatorReset(); // 旁观状态随会话切换清空（冷却是会话级的）
    clearChat();
    renderSessionList();
    try {
      const { session } = await api('/api/v1/sessions/' + encodeURIComponent(id) + '/history');
      els.chatHeadAgent.textContent = agentDisplay(session.agent);
      els.chatHeadModel.textContent = session.model || '—';
      els.chatHeadModel.title = session.model || '';
      (session.history || []).forEach((m) => renderMessage(m));
      els.chatState.textContent = session.stateful ? 'stateful' : 'stateless';
      resetModelState(agentDisplay(session.agent), session.model || '—');
      setComposerState();
      spectatorStartPolling();
    } catch (e) {
      els.chatState.textContent = '加载历史失败';
    }
  }
  function clearChat() {
    els.chatLog.innerHTML = '';
    els.traceLog.innerHTML = '<div class="empty-hint">运行任务后，Agent 的工具调用与推理步骤将实时显示在这里。</div>';
    els.traceLive.classList.add('hidden');
    els.chatHeadAgent.textContent = '—';
    els.chatHeadModel.textContent = '—';
    els.chatState.textContent = '';
    resetModelState();
  }

  /* ══════════════════ message rendering ══════════════════ */
  function renderMessage(m) {
    const role = m.role || 'assistant';
    const text = contentToText(m.content);
    const wrap = newEl('div', 'msg msg-' + role);
    wrap.appendChild(newEl('div', 'msg-role', role === 'user' ? '你' : 'Agent'));
    const bubble = newEl('div', 'msg-bubble');
    if (role === 'assistant') {
      renderAssistantMarkdown(bubble, text || '…');
    } else {
      bubble.textContent = text;
    }
    wrap.appendChild(bubble);
    els.chatLog.appendChild(wrap);
    els.chatLog.scrollTop = els.chatLog.scrollHeight;
    return bubble;
  }

  /* ══════════════════ trace rendering ══════════════════ */
  const TRACE_ICON = {
    status: '◌', tool_call: '▤', tool_output: '▦', handoff: '⇄', agent_switched: '◉', message: '◈', error: '✖',
    question_resolution: '⬡',
  };
  const TRACE_LABEL = {
    status: '状态',
    tool_call: '工具调用', tool_output: '工具输出', handoff: '交接',
    agent_switched: 'Agent 切换', message: '消息', error: '错误',
    question_resolution: '题库定位',
  };
  function renderTrace(step) {
    const type = step.type;
    const item = newEl('div', 'trace-item ' + type);
    const head = newEl('div', 'trace-head-row');
    head.appendChild(newEl('span', 'trace-icon', TRACE_ICON[type] || '▸'));
    head.appendChild(newEl('span', 'trace-label', TRACE_LABEL[type] || type.toUpperCase()));

    if (type === 'tool_call') {
      head.appendChild(newEl('span', 'trace-tool', step.tool || '工具'));
      if (step.agent) head.appendChild(newEl('span', 'trace-agent', step.agent));
      item.appendChild(head);
      if (step.arguments) {
        const det = newEl('details', 'trace-collapse');
        det.appendChild(newEl('summary', '', '参数'));
        det.appendChild(newEl('pre', 'trace-body', fmtJSON(step.arguments)));
        item.appendChild(det);
      }
    } else if (type === 'tool_output') {
      if (step.agent) head.appendChild(newEl('span', 'trace-agent', step.agent));
      item.appendChild(head);
      const det = newEl('details', 'trace-collapse');
      det.appendChild(newEl('summary', '', '输出'));
      det.appendChild(newEl('pre', 'trace-body', fmtJSON(step.output)));
      item.appendChild(det);
    } else if (type === 'handoff') {
      head.appendChild(newEl('span', 'trace-tool', `${step.from_agent || '?'} → ${step.to_agent || '?'}`));
      item.appendChild(head);
    } else if (type === 'agent_switched') {
      head.appendChild(newEl('span', 'trace-tool', step.agent || '?'));
      item.appendChild(head);
    } else if (type === 'message') {
      head.appendChild(newEl('span', 'trace-agent', step.agent || 'agent'));
      item.appendChild(head);
      if (step.text) item.appendChild(newEl('div', 'trace-body', step.text));
    } else if (type === 'question_resolution') {
      head.appendChild(newEl('span', 'trace-tool', '§6.1 题目定位'));
      item.appendChild(head);
      var resolutionBody = '';
      if (step.status === 'matched') {
        resolutionBody = '✅ 命中题库题目 · 置信度: ' + (step.confidence ? (step.confidence * 100).toFixed(0) + '%' : '—');
        if (step.method_status === 'usable') resolutionBody += '\n📖 有可用解法 · 进入讲解模式';
        else resolutionBody += '\n⏳ 尚无可用解法 · 进入自由探索';
      } else if (step.status === 'insufficient') {
        resolutionBody = '🔍 信息不足，无法唯一匹配 · 候选: ' + (step.candidate_ids ? step.candidate_ids.length + ' 个' : '0');
      } else if (step.status === 'unmatched') {
        resolutionBody = '🆕 未命中题库 · 继续自由对话';
      } else if (step.status === 'skipped') {
        resolutionBody = '⚠ 定位跳过 · ' + (step.reasons ? step.reasons.join(', ') : '未知原因');
      }
      if (step.reasons && step.reasons.length) {
        resolutionBody += '\n原因: ' + step.reasons.join(', ');
      }
      item.appendChild(newEl('div', 'trace-body', resolutionBody));
    } else if (type === 'status') {
      item.appendChild(head);
      if (step.message) item.appendChild(newEl('div', 'trace-body', step.message));
    } else if (type === 'error') {
      head.appendChild(newEl('span', 'trace-tool', 'agent 运行失败'));
      item.appendChild(head);
      item.appendChild(newEl('div', 'trace-body', step.message || '未知错误'));
    }
    els.traceLog.appendChild(item);
    els.traceLog.scrollTop = els.traceLog.scrollHeight;
  }
  function fmtJSON(x) {
    if (x == null) return '';
    if (typeof x === 'string') {
      try { return JSON.stringify(JSON.parse(x), null, 2); } catch (_) { return x; }
    }
    try { return JSON.stringify(x, null, 2); } catch (_) { return String(x); }
  }

  function toolActivityLabel(step) {
    const t = step.tool || '工具';
    let label = '▊ 正在调用 ' + t;
    let args = step.arguments;
    if (typeof args === 'string') { try { args = JSON.parse(args); } catch (_) { args = null; } }
    if (args && typeof args === 'object') {
      if (t === 'execute_code' || t === 'generic_linux_command') {
        const cmd = args.command || args.full_command || (args.code ? '代码执行' : '');
        if (cmd) label += ' · ' + String(cmd).split('\n')[0].slice(0, 70);
      }
    }
    return label;
  }

  /* ══════════════════ send / stream ══════════════════ */
  function setComposerState() {
    const hasSession = !!state.currentId;
    els.input.disabled = !hasSession;
    els.btnSend.disabled = !hasSession || state.running;
    els.input.placeholder = hasSession
      ? '输入指令 / 描述目标 · Ctrl+Enter 发送'
      : '请先在左侧新建一个会话';
    els.composerHint.textContent = hasSession
      ? (state.running ? 'Agent 运行中…' : '')
      : '';
  }

  function setRunning(on) {
    state.running = on;
    els.btnSend.classList.toggle('hidden', on);
    els.btnStop.classList.toggle('hidden', !on);
    els.traceLive.classList.toggle('hidden', !on);
    setComposerState();
  }

  /* ── composer attachments ─────────────────────────────── */
  function renderAttachments() {
    const box = els.composerAttachments;
    box.innerHTML = '';
    box.classList.toggle('hidden', !state.attachments.length);
    state.attachments.forEach(function (f, i) {
      const chip = newEl('div', 'attach-chip');
      chip.appendChild(newEl('span', 'attach-name', f.name));
      const x = newEl('button', 'attach-del', '×');
      x.type = 'button';
      x.title = '移除附件';
      x.addEventListener('click', function () { removeAttachment(i); });
      chip.appendChild(x);
      box.appendChild(chip);
    });
  }
  function removeAttachment(i) {
    state.attachments.splice(i, 1);
    renderAttachments();
  }
  function clearAttachments() {
    state.attachments = [];
    els.chatFiles.value = '';
    renderAttachments();
  }
  async function uploadAttachments() {
    const fd = new FormData();
    state.attachments.forEach(function (f) { fd.append('files', f, f.name); });
    const res = await fetch('/api/v1/chat-attachments', { method: 'POST', body: fd });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(`${res.status} ${detail}`);
    }
    return await res.json();
  }

  async function sendMessage(text) {
    if (!state.currentId || state.running) return;
    state.modelStateServerSeen = false;
    state.cancelRequested = false;
    const sessionMeta = currentSessionMeta();
    applyModelState({
      state: 'preparing',
      label: '正在准备模型请求',
      agent: sessionMeta.agent,
      model: sessionMeta.model,
      elapsed_seconds: 0,
    }, false);
    // 新消息开始 → 停止旁观轮询（本轮完成后重启, 契约 api.md §4）
    spectatorStopPolling();
    // Upload attachments first (if any) — agent reads them + they join bank matching
    let materials = null;
    if (state.attachments.length) {
      els.composerHint.textContent = '上传附件中…';
      try {
        materials = await uploadAttachments();
      } catch (e) {
        applyFallbackModelState('failed', '请求准备失败');
        els.composerHint.textContent = '附件上传失败：' + e.message;
        return;
      }
    }
    // user bubble
    renderMessage({ role: 'user', content: text });
    // assistant placeholder
    const wrap = newEl('div', 'msg msg-assistant streaming');
    wrap.appendChild(newEl('div', 'msg-role', 'AGENT'));
    const bubble = newEl('div', 'msg-bubble', '▊ 思考中…');
    wrap.appendChild(bubble);
    els.chatLog.appendChild(wrap);
    els.chatLog.scrollTop = els.chatLog.scrollHeight;

    setRunning(true);
    const controller = new AbortController();
    state.abort = controller;

    let assistantText = '';
    try {
      const res = await fetch(
        '/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/messages/stream',
        {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({
            input: text,
            content_kind: 'chat',
            materials: materials
          }),
          signal: controller.signal,
        }
      );
      if (!res.ok || !res.body) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        throw new Error(`${res.status} ${detail}`);
      }
      const gotTerminal = await readSSE(res, (event, data) => {
        consumeModelStateEvent(event, data);
        if (event === 'question_resolution') {
          // §6.1: 题目定位结果
          state.sessionResolved = true;
          renderTrace({ type: 'question_resolution', ...data });
          if (data.status === 'matched' && data.method_status === 'usable') {
            els.composerHint.textContent = '题库命中 · 讲解模式';
          } else if (data.status === 'matched') {
            els.composerHint.textContent = '题库命中 · 无可用解法';
          } else if (data.status === 'insufficient' || data.status === 'unmatched') {
            els.composerHint.textContent = '未命中题库 · 自由探索';
          }
        } else if (event === 'status') {
          // Long model/tool calls can legitimately be quiet. Show the server
          // heartbeat so the user can distinguish waiting from a dead UI.
          if (data && data.message) {
            els.composerHint.textContent = data.message;
            renderTrace({ type: 'status', message: data.message });
            // Mirror the heartbeat into the chat bubble so the user sees live
            // progress without having to watch the trace panel.
            if (!assistantText) bubble.textContent = '▊ ' + data.message;
          }
        } else if (event === 'reasoning_step') {
          renderTrace(data);
          if (data.type === 'tool_call') {
            if (!assistantText) bubble.textContent = toolActivityLabel(data);
            els.chatLog.scrollTop = els.chatLog.scrollHeight;
          } else if (data.type === 'tool_output') {
            if (!assistantText) bubble.textContent = '▊ 工具执行完成，继续分析…';
            els.chatLog.scrollTop = els.chatLog.scrollHeight;
          } else if (data.type === 'message' && data.text) {
            assistantText = data.text;
            wrap.classList.remove('streaming');
            renderAssistantMarkdown(bubble, assistantText);
            els.chatLog.scrollTop = els.chatLog.scrollHeight;
          }
        } else if (event === 'final') {
          if (data.final_message && assistantText !== data.final_message) {
            assistantText = data.final_message;
            wrap.classList.remove('streaming');
            renderAssistantMarkdown(bubble, assistantText);
            els.chatLog.scrollTop = els.chatLog.scrollHeight;
          }
        } else if (event === 'error') {
          throw new Error(data.message || 'agent 运行出错');
        }
      });
      // Stream ended without a final/error event — surface it instead of hanging.
      if (!gotTerminal) {
        throw new Error('流式响应意外中断（未收到 final/error 事件）');
      }
    } catch (e) {
      const wasCancelled = e.name === 'AbortError'
        || state.cancelRequested
        || state.modelState.state === 'cancelled'
        || /取消|cancel/i.test(e.message || '');
      if (wasCancelled) {
        applyFallbackModelState('cancelled', '任务已取消');
        wrap.classList.remove('streaming');
        bubble.textContent = assistantText || '（已停止）';
        els.composerHint.textContent = '任务已停止';
      } else {
        applyFallbackModelState('failed', '模型调用失败');
        renderTrace({ type: 'error', message: e.message });
        wrap.classList.remove('streaming');
        const errMsg = e.message || '';
        if (/503|SERVICE_BUSY|网关繁忙/.test(errMsg)) {
          bubble.textContent = assistantText || '⚠ 网关繁忙，请稍后重试';
          els.composerHint.textContent = '上游网关暂时不可用，请稍后重试';
        } else {
          bubble.textContent = assistantText || '⚠ 运行出错：' + errMsg;
        }
      }
    } finally {
      state.abort = null;
      setRunning(false);
      clearAttachments();
      // refresh session list (history_length / timestamps changed)
      loadSessions();
      // 旁观 agent: 本轮完成后开始轮询推荐卡片（specs/001-spectator-agent）
      spectatorStartPolling();
      state.cancelRequested = false;
    }
  }

  /* ── SSE parser (fetch ReadableStream) ── */
  // Returns true if a terminal event (final/error) was received, false otherwise.
  async function readSSE(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // Be tolerant of CRLF emitted or rewritten by proxies.
        buf = buf.replace(/\r\n/g, '\n');
        let idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const evt = parseSSEBlock(block, onEvent);
          if (evt === 'final' || evt === 'error') {
            // A terminal SSE event is the protocol boundary. Do not wait for
            // the HTTP connection to close: cleanup after `final` must never
            // keep the UI stuck in its running state.
            await reader.cancel();
            return true;
          }
        }
      }
      if (buf.trim()) {
        const evt = parseSSEBlock(buf, onEvent);
        return evt === 'final' || evt === 'error';
      }
      return false;
    } finally {
      try { await reader.cancel(); } catch (_) {}
      reader.releaseLock();
    }
  }
  function parseSSEBlock(block, onEvent) {
    let event = 'message';
    const dataLines = [];
    block.split('\n').forEach((line) => {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return event;
    const raw = dataLines.join('\n');
    let data;
    try { data = JSON.parse(raw); } catch (_) { data = raw; }
    onEvent(event, data);
    return event;
  }

  async function cancelRun() {
    if (!state.currentId || !state.running) return;
    state.cancelRequested = true;
    applyFallbackModelState('cancelled', '任务已取消');
    if (state.abort) state.abort.abort();
    try {
      await api('/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/cancel', {
        method: 'POST',
        body: JSON.stringify({}),
      });
    } catch (e) {
      /* backend cancel is best-effort */
    }
  }

  /* ══════════════════ modals & forms ══════════════════ */
  function openModal(m) { m.classList.remove('hidden'); }
  function closeModal() {
    els.modalNew.classList.add('hidden');
    els.modalSettings.classList.add('hidden');
    els.modalBank.classList.add('hidden');
    els.modalBankResult.classList.add('hidden');
    els.modalQBank.classList.add('hidden');
    els.modalQBankDetail.classList.add('hidden');
  }
  function populateSelects() {
    // Agent dropdown — populated from the static catalog.
    const selA = els.selAgent;
    selA.innerHTML = '';
    (state.agents.length ? state.agents : [{ name: '', id: '', description: '(加载 agent 目录失败)' }]).forEach((a) => {
      const o = newEl('option', '', a.name || a.description || 'agent');
      o.value = a.id || a.name || '';  // 传给后端的是内部 id
      selA.appendChild(o);
    });
    // Model dropdown starts empty — populated by FETCH MODELS for the chosen provider.
    els.selModel.innerHTML = '<option value="">— 先点击「获取模型」加载列表 —</option>';
  }

  // Normalize a raw model id from a provider into a CAI model string.
  // CAI routes models through litellm, which needs a "provider/" prefix to know
  // which endpoint to hit. Bare names like "claude-..." or "deepseek-chat" fail
  // with "LLM Provider NOT provided", so we infer the prefix from the id and the
  // base URL, and fall back to a provider list for well-known gateways.
  const PROVIDER_BY_HOST = /(?:^|\.)(deepseek|openai|anthropic|mistral|openrouter|groq|together|perplexity|moonshot|kimi|zhipu|ollama)\./i;
  function normalizeModelId(id, baseUrl) {
    if (!id) return '';
    if (String(id).includes('/')) return id;  // already has provider prefix
    const lower = String(id).toLowerCase();
    // Known model-name signals
    if (lower.includes('claude') || lower.includes('anthropic')) return 'anthropic/' + id;
    if (lower.startsWith('deepseek')) return 'deepseek/' + id;
    if (lower.includes('gpt-') || lower.includes('o1') || lower.includes('o3') || lower.includes('o4')) return 'openai/' + id;
    if (lower.includes('gemini')) return 'gemini/' + id;
    if (lower.includes('mistral') || lower.startsWith('mixtral')) return 'mistral/' + id;
    // Infer from the host in base URL
    const host = (baseUrl || '').replace(/^https?:\/\//, '').split('/')[0].toLowerCase();
    const m = host.match(PROVIDER_BY_HOST);
    if (m) return m[1].toLowerCase() + '/' + id;
    return id;  // default: leave bare, rely on injected api_base
  }

  async function fetchProviderModels() {
    const baseUrl = els.inpBaseUrl.value.trim();
    const apiKey = els.inpProviderKey.value.trim();
    els.fetchStatus.className = 'fetch-status';
    if (!baseUrl) {
      els.fetchStatus.textContent = '请先填写 BASE URL';
      els.fetchStatus.className = 'fetch-status err';
      return;
    }
    els.btnFetchModels.disabled = true;
    els.fetchStatus.textContent = '获取中…';
    try {
      const { models } = await api('/api/v1/models/fetch', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      });
      const selM = els.selModel;
      selM.innerHTML = '';
      if (!models.length) {
        selM.innerHTML = '<option value="">— 该端点未返回任何模型，请检查 BASE URL —</option>';
      } else {
        models.forEach((m) => {
          const o = newEl('option', '', m.name || m.id);
          o.value = normalizeModelId(m.id, baseUrl);
          selM.appendChild(o);
        });
      }
      els.fetchStatus.textContent = `✔ ${models.length} 个模型已加载`;
      els.fetchStatus.className = 'fetch-status ok';
    } catch (e) {
      els.fetchStatus.textContent = '✖ 获取失败：' + e.message;
      els.fetchStatus.className = 'fetch-status err';
      els.selModel.innerHTML = '<option value="">— 拉取失败，可重试 —</option>';
    } finally {
      els.btnFetchModels.disabled = false;
    }
  }

  /* ══════════════════ question bank ══════════════════ */
  function populateBankSelects() {
    const selA = els.bankAgent;
    selA.innerHTML = '';
    (state.agents.length ? state.agents : [{ name: '', id: '', description: '(加载 agent 目录失败)' }]).forEach((a) => {
      const o = newEl('option', '', a.name || a.description || 'agent');
      o.value = a.id || a.name || '';
      selA.appendChild(o);
    });
    els.bankModel.innerHTML = '<option value="">— 先点击「获取模型」加载列表 —</option>';
  }

  async function fetchBankModels() {
    const baseUrl = els.bankBaseUrl.value.trim();
    const apiKey = els.bankApiKey.value.trim();
    els.bankFetchStatus.className = 'fetch-status';
    if (!baseUrl) {
      els.bankFetchStatus.textContent = '请先填写 BASE URL';
      els.bankFetchStatus.className = 'fetch-status err';
      return;
    }
    els.btnBankFetch.disabled = true;
    els.bankFetchStatus.textContent = '获取中…';
    try {
      const { models } = await api('/api/v1/models/fetch', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      });
      const selM = els.bankModel;
      selM.innerHTML = '';
      if (!models.length) {
        selM.innerHTML = '<option value="">— 该端点未返回任何模型，请检查 BASE URL —</option>';
      } else {
        models.forEach((m) => {
          const o = newEl('option', '', m.name || m.id);
          o.value = normalizeModelId(m.id, baseUrl);
          selM.appendChild(o);
        });
      }
      els.bankFetchStatus.textContent = `✔ ${models.length} 个模型已加载`;
      els.bankFetchStatus.className = 'fetch-status ok';
    } catch (e) {
      els.bankFetchStatus.textContent = '✖ 获取失败：' + e.message;
      els.bankFetchStatus.className = 'fetch-status err';
      els.bankModel.innerHTML = '<option value="">— 拉取失败，可重试 —</option>';
    } finally {
      els.btnBankFetch.disabled = false;
    }
  }

  async function buildQuestionBank() {
    const problem = els.bankProblem.value.trim();
    const refsRaw = els.bankRefs.value.trim();
    const agent = els.bankAgent.value;
    const model = els.bankModel.value;
    const baseUrl = els.bankBaseUrl.value.trim();
    const apiKey = els.bankApiKey.value.trim();
    if (!problem) { els.bankStatus.textContent = '请填写题目'; els.bankStatus.className = 'bank-status err'; return; }
    if (!agent) { els.bankStatus.textContent = '请选择 agent'; els.bankStatus.className = 'bank-status err'; return; }

    const references = refsRaw
      ? refsRaw.split(/\n+/).map((s) => s.trim()).filter(Boolean)
      : [];

    els.btnBuildBank.disabled = true;
    els.bankStatus.className = 'bank-status';
    els.bankStatus.textContent = '构建中…（解题复现 + 思路总结，约需 1-2 分钟）';
    try {
      const resp = await api('/api/v1/question-bank/build', {
        method: 'POST',
        body: JSON.stringify({
          problem, references, agent,
          model: model || null,
          base_url: baseUrl || null,
          api_key: apiKey || null,
          max_turns: 20,
        }),
      });
      renderBankResult(resp);
      els.bankStatus.textContent = `✔ 构建完成 (${resp.status})`;
      els.bankStatus.className = 'bank-status ok';
      openModal(els.modalBankResult);
    } catch (e) {
      els.bankStatus.textContent = '✖ ' + e.message;
      els.bankStatus.className = 'bank-status err';
    } finally {
      els.btnBuildBank.disabled = false;
    }
  }

  function renderBankResult(resp) {
    els.bankResultTitle.textContent = '构建结果 · ' + (resp.status === 'completed' ? '已完成' : '未完成');
    els.bankResultBody.innerHTML = '';
    const escRow = (label, val) => {
      const row = newEl('div', 'row');
      row.appendChild(newEl('b', '', label));
      row.appendChild(newEl('span', '', val || '—'));
      return row;
    };
    if (resp.status === 'incomplete') {
      const box = newEl('div', 'bank-incomplete');
      box.textContent = '复现未完成：' + (resp.incomplete_reason || '未知原因') +
        '。已保留当前过程，未生成解法。';
      els.bankResultBody.appendChild(box);
      return;
    }
    const approach = newEl('div', 'bank-approach', resp.overall_approach || '（无整体思路）');
    els.bankResultBody.appendChild(approach);
    (resp.steps || []).forEach((s, i) => {
      const step = newEl('div', 'bank-step');
      step.appendChild(newEl('div', 'bank-step-title', `${i + 1}. ${s.title || '步骤'}`));
      step.appendChild(escRow('目标', s.goal));
      step.appendChild(escRow('原因', s.reason));
      step.appendChild(escRow('原理', s.principle));
      step.appendChild(escRow('动作', s.action));
      step.appendChild(escRow('结果', s.result));
      els.bankResultBody.appendChild(step);
    });
    const repro = resp.reproduction || {};
    const msgs = (repro.messages || []).length;
    const summary = newEl('div', 'bank-approach');
    summary.textContent = `真实复现：${msgs} 条过程记录 · 最终结果: ` +
      String(repro.final_output || '').slice(0, 120);
    els.bankResultBody.appendChild(summary);
  }

  /* ══════════════════ events ══════════════════ */
  els.btnNew.addEventListener('click', () => {
    populateSelects();
    els.inpBaseUrl.value = DEFAULT_BASE_URL;
    els.inpProviderKey.value = '';
    els.fetchStatus.textContent = '';
    els.fetchStatus.className = 'fetch-status';
    openModal(els.modalNew);
  });
  els.btnCancelModal.addEventListener('click', closeModal);
  els.modalNew.addEventListener('click', (e) => { if (e.target === els.modalNew) closeModal(); });
  els.btnFetchModels.addEventListener('click', fetchProviderModels);
  els.btnCreate.addEventListener('click', () => {
    const agent = els.selAgent.value;
    const model = els.selModel.value;
    if (!agent) { alert('请选择 agent'); return; }
    if (!model) { alert('请先 FETCH MODELS 并选择模型'); return; }
    const baseUrl = els.inpBaseUrl.value.trim() || null;
    const apiKey = els.inpProviderKey.value.trim() || null;
    createSession(agent, model, els.chkStateful.checked, baseUrl, apiKey);
  });

  els.btnSettings.addEventListener('click', () => {
    els.inpApiKey.value = localStorage.getItem(API_KEY_STORE) || '';
    openModal(els.modalSettings);
    spectatorLoadConfig();
  });
  els.btnCloseSettings.addEventListener('click', closeModal);
  els.modalSettings.addEventListener('click', (e) => { if (e.target === els.modalSettings) closeModal(); });
  els.btnSaveSettings.addEventListener('click', async () => {
    localStorage.setItem(API_KEY_STORE, els.inpApiKey.value.trim());
    await spectatorSaveConfig();
    closeModal();
    loadHealth();
  });

  /* ── 旁观 provider 配置（specs/001-spectator-agent T033: 手动配置模式）──
     GET  /api/v1/spectator/config → 回显（密钥脱敏 + configured/missing）
     POST /api/v1/spectator/config → 保存并热重建评估器
     手动模式: 不回退 .env/会话 provider——三项必填, 缺任一项评估不运行。
     字段语义: 缺省=不变; 空串=清除; 非空=设置（密钥留空提交=保持不变） */

  async function spectatorLoadConfig() {
    const inputs = [els.spectatorBaseUrl, els.spectatorApiKey, els.spectatorModel];
    els.spectatorStatus.textContent = '加载旁观配置…';
    try {
      const res = await fetch('/api/v1/spectator/config', { headers: headers() });
      if (res.status === 404) {
        els.spectatorStatus.textContent =
          '⚠ 旁观组件未部署（需在 WSL 应用 patch_spectator_app.py 后重启）';
        inputs.forEach((i) => { i.disabled = true; });
        return;
      }
      if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
      const cfg = await res.json();
      inputs.forEach((i) => { i.disabled = false; });
      // 手动模式: BASE URL/密钥输入框永远不回填; 已配置项显示在 placeholder
      els.spectatorBaseUrl.value = '';
      els.spectatorApiKey.value = '';
      els.spectatorBaseUrl.placeholder = cfg.api_base
        ? '已配置: ' + cfg.api_base + ' · 留空保持不变'
        : DEFAULT_BASE_URL;
      els.spectatorApiKey.placeholder = cfg.api_key_configured
        ? '已配置（' + cfg.api_key_masked + '）· 留空保持不变'
        : '必填';
      // 模型下拉: 已配置 → 以唯一 option 呈现并选中; 未配置 → 提示先 fetch
      els.spectatorModel.innerHTML = '';
      if (cfg.model) {
        const opt = document.createElement('option');
        opt.value = cfg.model;
        opt.textContent = cfg.model + '（已配置）';
        opt.selected = true;
        els.spectatorModel.appendChild(opt);
      } else {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '— 先填 BASE URL + 密钥，点击「获取模型」 —';
        els.spectatorModel.appendChild(opt);
      }
      els.spectatorFetchStatus.textContent = '';
      els.chkSpectatorClearKey.checked = false;
      const base = cfg.enabled
        ? '旁观功能：已开启 · 每 ' + cfg.interval_rounds + ' 轮评估'
        : '旁观功能：未开启（服务端设 CAI_SPECTATOR_ENABLED=true 启用）';
      if (!cfg.configured) {
        els.spectatorStatus.textContent =
          base + ' · ⚠ 评估 API 未配置完整（缺: ' + cfg.missing.join(', ') +
          '）——填写全部三项并保存后评估才会运行';
      } else {
        els.spectatorStatus.textContent = base + ' · ✔ 评估 API 已配置（模型 ' + cfg.model + '）';
      }
    } catch (e) {
      els.spectatorStatus.textContent = '旁观配置加载失败：' + e.message;
      inputs.forEach((i) => { i.disabled = true; });
    }
  }

  async function spectatorFetchModels() {
    const baseUrl = els.spectatorBaseUrl.value.trim();
    const apiKey = els.spectatorApiKey.value.trim();
    if (!baseUrl) {
      els.spectatorFetchStatus.textContent = '⚠ 请先填写 BASE URL';
      els.spectatorBaseUrl.focus();
      return;
    }
    els.spectatorFetchStatus.textContent = '获取中…';
    els.spectatorFetch.disabled = true;
    try {
      const res = await fetch('/api/v1/models/fetch', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const data = await res.json();
      const models = (data.models || []).map(function (m) { return m.id || m.name; }).filter(Boolean);
      if (!models.length) throw new Error('网关返回空模型列表');
      // 填充下拉（沿用「新建会话」的 normalizeModelId 逻辑补 provider 前缀）
      els.spectatorModel.innerHTML = '';
      models.forEach(function (id) {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = id;
        els.spectatorModel.appendChild(opt);
      });
      els.spectatorFetchStatus.textContent = '✔ 获取到 ' + models.length + ' 个模型，请选择';
    } catch (e) {
      els.spectatorFetchStatus.textContent = '✖ ' + e.message;
    } finally {
      els.spectatorFetch.disabled = false;
    }
  }

  els.spectatorFetch.addEventListener('click', spectatorFetchModels);

  async function spectatorSaveConfig() {
    const body = {};
    // 三态: BASE URL/密钥留空(且未勾选清除) → 不发送该字段 = 保持现状
    const baseUrl = els.spectatorBaseUrl.value.trim();
    const apiKey = els.spectatorApiKey.value.trim();
    // 模型为 select: 选中合法项才提交; 选中占位项（value=''）→ 不变
    const model = els.spectatorModel.value;
    if (baseUrl) body.api_base = baseUrl;
    if (model) body.model = model;
    if (els.chkSpectatorClearKey.checked) {
      body.api_key = '';
    } else if (apiKey) {
      body.api_key = apiKey;
    }
    if (!Object.keys(body).length) {
      els.spectatorStatus.textContent = '未填写任何变更（留空 = 保持现状）';
      return;
    }
    await spectatorPostConfig(body);
  }

  async function spectatorPostConfig(body) {
    try {
      const res = await fetch('/api/v1/spectator/config', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify(body),
      });
      if (res.status === 404) {
        els.spectatorStatus.textContent = '⚠ 旁观组件未部署，配置未保存';
        return;
      }
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        els.spectatorStatus.textContent = '旁观配置保存失败：' + detail;
        return;
      }
      const cfg = await res.json();
      els.spectatorApiKey.value = '';
      els.spectatorBaseUrl.value = '';
      els.spectatorModel.value = '';
      els.chkSpectatorClearKey.checked = false;
      if (!cfg.configured) {
        els.spectatorStatus.textContent =
          '已保存, 但配置仍不完整（缺: ' + cfg.missing.join(', ') +
          '）——评估不会运行, 请补全后保存';
      } else {
        els.spectatorStatus.textContent =
          '✔ 旁观 API 已配置（模型 ' + cfg.model + '）· 评估即刻生效';
      }
      spectatorLoadConfig();
    } catch (e) {
      els.spectatorStatus.textContent = '旁观配置保存失败：' + e.message;
    }
  }

  els.spectatorClearAll.addEventListener('click', async function () {
    if (!confirm('确定清除全部旁观 API 配置？\n（BASE URL / 密钥 / 模型全部清空，评估将停止直至重新配置）')) return;
    await spectatorPostConfig({ api_base: '', api_key: '', model: '' });
  });

  // Question bank modal
  els.btnBank.addEventListener('click', () => {
    populateBankSelects();
    els.bankProblem.value = '';
    els.bankRefs.value = '';
    els.bankBaseUrl.value = DEFAULT_BASE_URL;
    els.bankApiKey.value = '';
    els.bankFetchStatus.textContent = '';
    els.bankFetchStatus.className = 'fetch-status';
    els.bankStatus.textContent = '';
    els.bankStatus.className = 'bank-status';
    openModal(els.modalBank);
  });
  els.btnCancelBank.addEventListener('click', closeModal);
  els.modalBank.addEventListener('click', (e) => { if (e.target === els.modalBank) closeModal(); });
  els.btnBankFetch.addEventListener('click', fetchBankModels);
  els.btnBuildBank.addEventListener('click', buildQuestionBank);
  els.btnCloseBankResult.addEventListener('click', closeModal);
  els.modalBankResult.addEventListener('click', (e) => { if (e.target === els.modalBankResult) closeModal(); });

  els.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = els.input.value.trim();
    if (!text) return;
    els.input.value = '';
    autoGrow();
    sendMessage(text);
  });
  els.btnAttach.addEventListener('click', () => els.chatFiles.click());
  els.chatFiles.addEventListener('change', () => {
    Array.prototype.forEach.call(els.chatFiles.files, function (f) {
      state.attachments.push(f);
    });
    renderAttachments();
  });
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      els.composer.requestSubmit();
    }
  });
  els.btnStop.addEventListener('click', cancelRun);
  els.input.addEventListener('input', autoGrow);
  function autoGrow() {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(els.input.scrollHeight, 180) + 'px';
  }

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!els.modalQBankDetail.classList.contains('hidden')) closeQBankDetail();
    else closeModal();
  });

  /* ══════════════════ init ══════════════════ */
  async function init() {
    resetModelState();
    loadHealth();
    setInterval(loadHealth, 20000);
    await Promise.all([loadAgents(), loadModels(), loadSessions()]);
    setComposerState();
  }
  /* ══════════════════ question bank management (PostgreSQL) ══════════════════ */
  function openQBank() {
    els.modalQBank.classList.remove('hidden');
    els.qbankBaseUrl.value = DEFAULT_BASE_URL;
    populateQBankSelects();
    loadQBankList();
  }
  function closeQBank() { els.modalQBank.classList.add('hidden'); }

  function populateQBankSelects() {
    const selA = els.qbankAgent;
    selA.innerHTML = '';
    (state.agents.length ? state.agents : [{ name: '', id: '', description: '(加载 agent 目录失败)' }]).forEach((a) => {
      const o = newEl('option', '', a.name || a.description || 'agent');
      o.value = a.id || a.name || '';
      selA.appendChild(o);
    });
    els.qbankModel.innerHTML = '<option value="">— 先点击「获取模型」加载列表 —</option>';
  }

  async function fetchQBankModels() {
    const baseUrl = els.qbankBaseUrl.value.trim();
    const apiKey = els.qbankApiKey.value.trim();
    els.qbankFetchStatus.className = 'fetch-status';
    if (!baseUrl) {
      els.qbankFetchStatus.textContent = '请先填写 BASE URL';
      els.qbankFetchStatus.className = 'fetch-status err';
      return;
    }
    els.btnQBankFetch.disabled = true;
    els.qbankFetchStatus.textContent = '获取中…';
    try {
      const { models } = await api('/api/v1/models/fetch', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      });
      const selM = els.qbankModel;
      selM.innerHTML = '';
      if (!models.length) {
        selM.innerHTML = '<option value="">— 该端点未返回任何模型，请检查 BASE URL —</option>';
      } else {
        models.forEach((m) => {
          const o = newEl('option', '', m.name || m.id);
          o.value = normalizeModelId(m.id, baseUrl);
          selM.appendChild(o);
        });
      }
      els.qbankFetchStatus.textContent = `✔ ${models.length} 个模型已加载`;
      els.qbankFetchStatus.className = 'fetch-status ok';
    } catch (e) {
      els.qbankFetchStatus.textContent = '✖ 获取失败：' + e.message;
      els.qbankFetchStatus.className = 'fetch-status err';
      els.qbankModel.innerHTML = '<option value="">— 拉取失败，可重试 —</option>';
    } finally {
      els.btnQBankFetch.disabled = false;
    }
  }

  async function loadQBankList() {
    try {
      const resp = await api('/api/v1/question-bank/questions?limit=50');
      renderQBankList(resp);
    } catch (e) { els.qbankList.innerHTML = '<div class="empty-hint">⚠ 题库不可用：' + e.message + '</div>'; }
  }

  function renderQBankList(data) {
    const items = data.items || [];
    els.qbankStats.textContent = '共 ' + data.total + ' 道题 | ' +
      (items.filter(function(q){return q.has_usable_method;}).length) + ' 可用解法';
    if (!items.length) { els.qbankList.innerHTML = '<div class="empty-hint">暂无题目 · 在上面创建第一道</div>'; return; }
    els.qbankList.innerHTML = '';
    items.forEach(function(q) {
      const card = newEl('div', 'qbank-card');
      card.dataset.id = q.question_id;
      card.title = '点击查看详情';
      const head = newEl('div', 'qbank-card-head');
      head.appendChild(newEl('span', 'qbank-status qbank-status-' + q.status, q.status));
      head.appendChild(newEl('span', 'qbank-badge', q.instance_fingerprint.slice(0, 20) + '…'));
      card.appendChild(head);
      card.appendChild(newEl('div', 'qbank-preview', q.statement_preview));
      card.appendChild(newEl('div', 'qbank-meta',
        '版本 ' + q.version + ' · 资料 ' + q.material_count + ' 件' +
        (q.has_usable_method ? ' · ✅ 可用解法' : ' · ⏳ 待构建')));
      els.qbankList.appendChild(card);
    });
  }

  function openQBankDetail() { els.modalQBankDetail.classList.remove('hidden'); }
  function closeQBankDetail() { els.modalQBankDetail.classList.add('hidden'); }

  async function viewQBankQuestion(id) {
    openQBankDetail();
    els.qbankDetailTitle.textContent = '题目详情';
    els.qbankDetailBody.innerHTML = '<div class="empty-hint">加载中…</div>';
    let q;
    try {
      q = await api('/api/v1/question-bank/questions/' + encodeURIComponent(id));
    } catch (e) {
      els.qbankDetailBody.innerHTML = '<div class="empty-hint">⚠ 加载题目失败：' + esc(e.message) + '</div>';
      return;
    }
    let sol = null;
    try {
      sol = await api('/api/v1/question-bank/questions/' + encodeURIComponent(id) + '/solution');
    } catch (e) { /* 409 尚无可用解法等 → 视为无解法 */ }
    renderQBankDetail(q, sol);
  }

  function renderQBankDetail(q, sol) {
    const body = els.qbankDetailBody;
    body.innerHTML = '';
    els.qbankDetailTitle.textContent = '题目详情 · ' + (q.status || '');

    function section(title, text, mono) {
      const sec = newEl('div', 'qbank-detail-section');
      sec.appendChild(newEl('div', 'qbank-detail-section-title', title));
      sec.appendChild(newEl('div', mono ? 'qbank-detail-mono' : 'qbank-detail-text', text || '（空）'));
      return sec;
    }
    function escRow(label, val) {
      const row = newEl('div', 'row');
      row.appendChild(newEl('b', '', label));
      row.appendChild(newEl('span', '', val || '—'));
      return row;
    }
    function fmtBytes(n) {
      if (n == null) return '';
      if (n < 1024) return n + ' B';
      if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
      return (n / 1048576).toFixed(1) + ' MB';
    }

    // 题面
    body.appendChild(section('题面', q.statement_raw));

    // 成功判据
    let crit = '（无）';
    if (q.success_criteria && Object.keys(q.success_criteria).length) {
      try { crit = JSON.stringify(q.success_criteria, null, 2); } catch (_) { crit = String(q.success_criteria); }
    }
    body.appendChild(section('成功判据', crit, true));

    // 元信息
    const meta = newEl('div', 'qbank-detail-meta');
    meta.appendChild(newEl('span', 'qbank-badge', '版本 ' + q.version));
    meta.appendChild(newEl('span', 'qbank-badge', '指纹 ' + (q.instance_fingerprint || '').slice(0, 24) + '…'));
    meta.appendChild(newEl('span', 'qbank-badge', '资料 ' + (q.materials ? q.materials.length : 0) + ' 件'));
    body.appendChild(meta);

    // 附件列表
    if (q.materials && q.materials.length) {
      const matSec = newEl('div', 'qbank-detail-section');
      matSec.appendChild(newEl('div', 'qbank-detail-section-title', '附件'));
      q.materials.forEach(function(m) {
        const row = newEl('div', 'qbank-material');
        row.appendChild(newEl('span', '', (m.material_type || 'writeup') + ' · ' + (m.mime_type || '') + ' · ' + fmtBytes(m.size_bytes)));
        row.appendChild(newEl('span', 'qbank-badge', 'sha256 ' + (m.sha256 || '').slice(0, 12) + '…'));
        matSec.appendChild(row);
      });
      body.appendChild(matSec);
    }

    // 解法
    if (sol && sol.overall_approach) {
      body.appendChild(section('整体思路', sol.overall_approach));
      (sol.steps || []).forEach(function(s, i) {
        const step = newEl('div', 'bank-step');
        step.appendChild(newEl('div', 'bank-step-title', (i + 1) + '. ' + (s.goal || s.action || '步骤')));
        step.appendChild(escRow('目标', s.goal));
        step.appendChild(escRow('原因', s.why));
        step.appendChild(escRow('原理', s.principle));
        step.appendChild(escRow('动作', s.action));
        step.appendChild(escRow('结果', s.result));
        body.appendChild(step);
      });
    } else {
      body.appendChild(newEl('div', 'bank-incomplete',
        '⏳ 尚无可用解法（status=' + q.status + '），可在题目列表或「题库构建」中触发构建。'));
    }
  }

  async function createQBankQuestion() {
    var stmt = els.qbankStatement.value.trim();
    if (!stmt) { els.qbankCreateStatus.textContent = '请输入题面'; els.qbankCreateStatus.className = 'bank-status err'; return; }
    els.qbankCreateStatus.textContent = '创建中…'; els.qbankCreateStatus.className = 'bank-status';
    try {
      var fd = new FormData();
      fd.append('statement', stmt);
      fd.append('success_criteria', els.qbankCriteria.value.trim() || '{}');
      fd.append('force_new_version', 'false');
      var files = els.qbankFiles.files;
      for (var i = 0; i < files.length; i++) {
        fd.append('materials', files[i], files[i].name);
      }
      var agent = els.qbankAgent.value.trim();
      var model = els.qbankModel.value.trim();
      var baseUrl = els.qbankBaseUrl.value.trim();
      var apiKey = els.qbankApiKey.value.trim();
      if (agent) fd.append('agent', agent);
      if (model) fd.append('model', model);
      if (baseUrl) fd.append('base_url', baseUrl);
      if (apiKey) fd.append('api_key', apiKey);
      var resp = await fetch('/api/v1/question-bank/questions', { method: 'POST', body: fd });
      if (!resp.ok) { var errData = await resp.json(); throw new Error(errData.detail || resp.statusText); }
      var data = await resp.json();
      var msg = data.build_job_id ? '✔ 已创建 · 构建已自动入队' : '✔ 已创建';
      els.qbankCreateStatus.textContent = msg;
      els.qbankCreateStatus.className = 'bank-status ok';
      els.qbankStatement.value = '';
      els.qbankFiles.value = '';
      loadQBankList();
    } catch (e) { els.qbankCreateStatus.textContent = '✖ ' + e.message; els.qbankCreateStatus.className = 'bank-status err'; }
  }

  /* ══════════════════ 旁观 Agent（specs/001-spectator-agent）══════════════════
     契约（contracts/api.md §4）:
     - 每轮 final 后轮询卡片（2s × 8 次），204 继续等，200 渲染
     - 每候选一个「开始学习」；卡片级「不感兴趣」与「×」本地忽略
     - accept 成功 → 自动发送 teaching_prompt_hint 走既有教学流
     - 任何失败静默，绝不影响主对话（宪法 VI）
  ═══════════════════════════════════════════════════════════════════════════ */

  const SPECTATOR_POLL_INTERVAL_MS = 2000;
  const SPECTATOR_POLL_MAX = 30;

  function spectatorReset() {
    spectatorStopPolling();
    state.spectatorCardData = null;
    state.spectatorDismissed = new Set();
    els.spectatorWrap.classList.add('hidden');
    els.spectatorCard.innerHTML = '';
    els.spectatorTopic.textContent = '';
  }

  function spectatorStopPolling() {
    if (state.spectatorPollTimer) {
      clearTimeout(state.spectatorPollTimer);
      state.spectatorPollTimer = null;
    }
    state.spectatorPollCount = 0;
  }

  function spectatorStartPolling() {
    if (!state.currentId) return;
    spectatorStopPolling();
    state.spectatorPollCount = 0;
    state.spectatorPollTimer = setTimeout(spectatorPollOnce, SPECTATOR_POLL_INTERVAL_MS);
  }

  async function spectatorPollOnce() {
    if (!state.currentId || state.running) { spectatorStopPolling(); return; }
    state.spectatorPollCount += 1;
    try {
      const res = await fetch(
        '/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/spectator/card',
        { headers: headers() }
      );
      if (res.status === 204) {
        // 无卡片: 继续轮询直至上限
        if (state.spectatorPollCount < SPECTATOR_POLL_MAX) {
          state.spectatorPollTimer = setTimeout(spectatorPollOnce, SPECTATOR_POLL_INTERVAL_MS);
        } else {
          spectatorStopPolling();
        }
        return;
      }
      if (res.status === 404) { spectatorStopPolling(); return; } // 会话没了
      if (!res.ok) { spectatorStopPolling(); return; }            // 静默降级
      const card = await res.json();
      if (card && card.kind === 'failure') {
        spectatorRenderFailure(card);
        spectatorStopPolling();
        return;
      }
      if (card && card.card_id && !state.spectatorDismissed.has(card.card_id)) {
        spectatorRenderCard(card);
        spectatorStopPolling();
        return;
      }
      // 已本地忽略的卡片: 视同无卡片继续等（后端替换旧卡后 card_id 变化）
      if (state.spectatorPollCount < SPECTATOR_POLL_MAX) {
        state.spectatorPollTimer = setTimeout(spectatorPollOnce, SPECTATOR_POLL_INTERVAL_MS);
      } else {
        spectatorStopPolling();
      }
    } catch (_) {
      spectatorStopPolling(); // 网络错误静默
    }
  }

  function spectatorRenderFailure(card) {
    state.spectatorCardData = card;
    els.spectatorTopic.textContent = '';
    const box = els.spectatorCard;
    box.innerHTML = '';

    const failure = newEl('div', 'spectator-failure');
    failure.setAttribute('role', 'alert');
    failure.appendChild(newEl('div', 'spectator-failure-title', '⚠ 旁观 Agent 连接失败'));
    failure.appendChild(newEl(
      'div',
      'spectator-failure-message',
      card.error_message || '评估服务暂时不可用，请稍后重试。',
    ));

    const actions = newEl('div', 'spectator-failure-actions');
    const retry = newEl('button', 'btn btn-accent', '重新连接');
    retry.disabled = card.retryable === false;
    if (retry.disabled) {
      retry.title = '当前错误不可重试';
    } else {
      retry.addEventListener('click', function () {
        spectatorRetry(retry);
      });
    }
    const abandon = newEl('button', 'btn btn-ghost', '放弃');
    abandon.addEventListener('click', spectatorAbandon);
    actions.appendChild(retry);
    actions.appendChild(abandon);
    failure.appendChild(actions);
    box.appendChild(failure);
    els.spectatorWrap.classList.remove('hidden');
  }

  function spectatorRenderCard(card) {
    state.spectatorCardData = card;
    els.spectatorTopic.textContent = '检测到你在了解：' + (card.topic_summary || '相关知识点');
    const box = els.spectatorCard;
    box.innerHTML = '';
    (card.candidates || []).forEach(function (c) {
      const item = newEl('div', 'spectator-candidate');
      const notLearnable = c.disabled === true || c.learnable === false;
      if (notLearnable) item.classList.add('spectator-candidate-disabled');
      const title = newEl('div', 'spectator-candidate-title', '《' + (c.title_preview || '题目') + '》');
      title.title = c.title_preview || '';
      const reasonText = notLearnable
        ? (c.disabled_reason || '仅召回测试：尚无 usable 解法，不可学习')
        : (c.reason || '');
      const reason = newEl('div', 'spectator-candidate-reason', reasonText);
      const actions = newEl('div', 'spectator-actions');
      const btn = newEl(
        'button',
        'btn ' + (notLearnable ? 'btn-disabled' : 'btn-accent'),
        notLearnable ? '⛔ 仅召回测试' : '▶ 开始学习'
      );
      btn.disabled = notLearnable;
      if (!notLearnable) {
        btn.addEventListener('click', function () { spectatorAccept(c.question_id); });
      }
      actions.appendChild(btn);
      item.appendChild(title);
      item.appendChild(reason);
      item.appendChild(actions);
      box.appendChild(item);
    });
    const decline = newEl('button', 'spectator-decline', '✕ 不感兴趣（不再推荐）');
    decline.addEventListener('click', spectatorDecline);
    box.appendChild(decline);
    els.spectatorWrap.classList.remove('hidden');
  }

  async function spectatorRetry(button) {
    const card = state.spectatorCardData;
    if (!card || !state.currentId || card.retryable === false) return;

    if (button) {
      button.disabled = true;
      button.textContent = '连接中…';
    }

    try {
      const res = await fetch(
        '/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/spectator/card/retry',
        {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({ card_id: card.card_id }),
        }
      );
      if (res.status === 204) {
        spectatorReset();
        els.composerHint.textContent = '旁观 Agent 已重新连接，本轮暂无匹配推荐';
        return;
      }
      if (!res.ok) {
        let detail = '评估服务暂时不可用，请稍后重试。';
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        spectatorRenderFailure(Object.assign({}, card, {
          error_message: '重新连接失败：' + detail,
          retryable: card.retryable !== false,
        }));
        return;
      }

      const nextCard = await res.json();
      spectatorStopPolling();
      if (nextCard && nextCard.kind === 'failure') {
        spectatorRenderFailure(nextCard);
        return;
      }
      if (nextCard && typeof nextCard === 'object') {
        spectatorRenderCard(nextCard);
        return;
      }
      spectatorReset();
    } catch (error) {
      spectatorRenderFailure(Object.assign({}, card, {
        error_message: '重新连接失败：' + (error.message || '网络错误'),
        retryable: true,
      }));
    }
  }

  async function spectatorAbandon() {
    const card = state.spectatorCardData;
    if (!card || !state.currentId) return;

    try {
      await fetch(
        '/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/spectator/card/abandon',
        {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({ card_id: card.card_id }),
        }
      );
    } catch (_) {}
    spectatorReset();
  }

  async function spectatorAccept(questionId) {
    const card = state.spectatorCardData;
    if (!card || !state.currentId) return;
    const candidate = (card.candidates || []).find(function (item) {
      return item.question_id === questionId;
    });
    if (candidate && (candidate.disabled === true || candidate.learnable === false)) return;
    try {
      const res = await fetch(
        '/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/spectator/card/accept',
        {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({ card_id: card.card_id, question_id: questionId }),
        }
      );
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        els.composerHint.textContent = '旁观推荐确认失败：' + detail;
        spectatorReset();
        return;
      }
      const data = await res.json();
      // 会话已绑定（服务端 resolution_locked=true）→ 本地同样锁定，
      // 后续消息按 chat 发送
      state.sessionResolved = true;
      spectatorReset();
      els.composerHint.textContent = '已进入讲解模式';
      // 自动发送教学提示消息 → 走既有 explain 教学路径（宪法 IV/VII）
      sendMessage(data.teaching_prompt_hint || '开始学习这道题');
    } catch (e) {
      spectatorReset(); // 网络错误静默
    }
  }

  async function spectatorDecline() {
    const card = state.spectatorCardData;
    if (!card || !state.currentId) return;
    try {
      await fetch(
        '/api/v1/sessions/' + encodeURIComponent(state.currentId) + '/spectator/card/decline',
        {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({ card_id: card.card_id }),
        }
      );
    } catch (_) { /* 静默降级 */ }
    spectatorReset();
  }

  els.spectatorClose.addEventListener('click', function () {
    // 本地忽略: 记录 card_id, 后端将在下一次评估替换时落 ignored 冷却
    if (state.spectatorCardData) {
      state.spectatorDismissed.add(state.spectatorCardData.card_id);
    }
    spectatorStopPolling();
    state.spectatorCardData = null;
    els.spectatorWrap.classList.add('hidden');
  });

  els.btnQBank.addEventListener('click', openQBank);
  els.btnCancelQBank.addEventListener('click', closeQBank);
  els.btnQBankRefresh.addEventListener('click', loadQBankList);
  els.btnQBankFetch.addEventListener('click', fetchQBankModels);
  els.btnQBankCreate.addEventListener('click', createQBankQuestion);
  els.btnCloseQBankDetail.addEventListener('click', closeQBankDetail);
  els.modalQBankDetail.addEventListener('click', (e) => { if (e.target === els.modalQBankDetail) closeQBankDetail(); });
  els.qbankList.addEventListener('click', function(e) {
    const card = e.target.closest('.qbank-card');
    if (card && card.dataset.id) viewQBankQuestion(card.dataset.id);
  });
  els.modalQBank.addEventListener('click', function(e) { if (e.target === els.modalQBank) closeQBank(); });

  init();
})();
