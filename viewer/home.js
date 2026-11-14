(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const KIND_COLORS = {
    fact: '#93c5fd',
    preference: '#f9a8d4',
    person: '#34d399',
    project: '#a78bfa',
    event: '#fbbf24',
    task: '#fb7185',
    document: '#22d3ee',
    procedure: '#fda4af',
    observation: '#94a3b8'
  };

  const KIND_GLYPHS = {
    fact: '▣',
    preference: '◆',
    person: '◍',
    project: '▤',
    event: '◇',
    task: '✓',
    document: '▥',
    procedure: '↻',
    observation: '·'
  };

  const captureText = $('capture-text');
  const captureKind = $('capture-kind');
  const capturePropose = $('capture-propose');
  const captureRemember = $('capture-remember');
  const needsList = $('needs-list');
  const needsCount = $('needs-count');
  const threadList = $('thread-list');
  const memoryList = $('memory-list');
  const activityList = $('activity-list');
  const briefHeadline = $('brief-headline');
  const briefDetail = $('brief-detail');
  const briefDate = $('brief-date');
  const briefStats = $('brief-stats');
  const statusChip = $('status-chip');
  const heroGreeting = $('hero-greeting');
  const railLane = $('rail-lane');
  const railTrack = $('rail-track');
  const connectorList = $('connector-list');
  const connectorCount = $('connector-count');
  const toastEl = $('toast');
  const palette = $('palette');
  const paletteInput = $('palette-input');
  const paletteList = $('palette-list');

  let toastTimer = 0;
  let paletteIndex = 0;
  let paletteMatches = [];
  let homeData = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showToast(message, isError = false) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.classList.toggle('error', Boolean(isError));
    toastEl.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      toastEl.hidden = true;
    }, 4200);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok) {
      const detail = data && (data.detail || data.error);
      throw new Error(detail || `A.S.H.R.A returned HTTP ${response.status}.`);
    }
    return data || {};
  }

  function relativeTime(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 45) return 'just now';
    if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
    if (delta < 604800) return `${Math.round(delta / 86400)}d ago`;
    return new Date(seconds * 1000).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric'
    });
  }

  function kindColor(kind) {
    return KIND_COLORS[kind] || KIND_COLORS.fact;
  }

  function emptyState(glyph, copy) {
    const wrap = element('div', 'empty');
    wrap.append(element('span', 'empty-glyph', glyph));
    wrap.append(element('p', '', copy));
    return wrap;
  }

  function setStatus(status) {
    if (!statusChip) return;
    const configured = Boolean(status && status.configured);
    statusChip.classList.toggle('offline', !configured);
    if (window.ASHRALane) window.ASHRALane.render(status);
    if (!configured) {
      statusChip.textContent = 'Model not configured';
      return;
    }
    const provider = status.provider || 'provider';
    const model = status.model || 'model';
    statusChip.textContent = `${provider} · ${model}`;
  }

  function renderStats(status, stats) {
    if (!briefStats) return;
    briefStats.replaceChildren();
    const confirmed = Number(stats.confirmed || 0);
    const proposed = Number(stats.proposed || 0);
    const items = [
      ['notes', Number(status.notes || 0)],
      ['memories', confirmed],
      ['proposals', proposed],
      ['pending', Number(status.approvals || 0)],
      ['threads', Number(status.sessions || 0)]
    ];
    items.forEach(([label, value]) => {
      const chip = element('span', 'stat-chip');
      chip.append(element('strong', '', String(value)));
      chip.append(document.createTextNode(label));
      briefStats.append(chip);
    });

    if (railLane) {
      const plural = confirmed === 1 ? 'memory' : 'memories';
      const waiting = proposed === 1 ? '1 awaiting review' : `${proposed} awaiting review`;
      railLane.textContent = `${confirmed} ${plural} · ${waiting}`;
    }
    if (railTrack) {
      const total = confirmed + proposed;
      const share = total ? Math.round((confirmed / total) * 100) : 6;
      railTrack.style.width = `${Math.max(6, Math.min(100, share))}%`;
    }
  }

  function renderGreeting() {
    if (!heroGreeting) return;
    const hour = new Date().getHours();
    const part = hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
    heroGreeting.replaceChildren(
      document.createTextNode('Good '),
      element('em', '', `${part}.`)
    );
  }

  function setHeadline(headline) {
    if (!briefHeadline) return;
    const text = String(headline || '');
    const leading = text.match(/^(\d+|One|Two|Three|Four|Five|Nothing)([\s\S]*)$/);
    briefHeadline.replaceChildren();
    if (leading) {
      briefHeadline.append(element('em', '', leading[1]));
      briefHeadline.append(document.createTextNode(leading[2]));
    } else {
      briefHeadline.textContent = text;
    }
  }

  function renderBrief(data) {
    if (briefDate) {
      briefDate.textContent = new Date().toLocaleDateString(undefined, {
        weekday: 'long',
        month: 'long',
        day: 'numeric'
      });
    }
    renderGreeting();
    setHeadline(data.brief.headline);
    if (briefDetail) briefDetail.textContent = data.brief.detail;
    renderStats(data.status, data.stats);
  }

  function memoryKicker(kind) {
    return `Memory proposal · ${String(kind || 'fact')}`;
  }

  function renderNeeds(data) {
    if (!needsList) return;
    const proposals = Array.isArray(data.needs_you.memories) ? data.needs_you.memories : [];
    const approvals = Array.isArray(data.needs_you.approvals) ? data.needs_you.approvals : [];
    const total = proposals.length + approvals.length;

    if (needsCount) {
      needsCount.textContent = String(total);
      needsCount.classList.toggle('zero', total === 0);
    }

    needsList.replaceChildren();
    if (!total) {
      needsList.append(emptyState('✓', 'Inbox zero. Nothing needs your approval right now.'));
      return;
    }

    proposals.forEach((item) => {
      const card = element('article', 'need');
      const kicker = element('p', 'need-kicker');
      const dot = element('span', 'need-dot');
      dot.style.background = kindColor(item.kind);
      kicker.append(dot, document.createTextNode(memoryKicker(item.kind)));
      card.append(kicker);
      card.append(element('p', 'need-text', item.content || item.label || 'Memory proposal'));
      const actions = element('div', 'need-actions');
      const confirm = element('button', 'btn approve', 'Confirm');
      confirm.type = 'button';
      confirm.dataset.kind = 'memory';
      confirm.dataset.id = item.id;
      confirm.dataset.action = 'confirm';
      const reject = element('button', 'btn deny', 'Reject');
      reject.type = 'button';
      reject.dataset.kind = 'memory';
      reject.dataset.id = item.id;
      reject.dataset.action = 'reject';
      actions.append(confirm, reject);
      card.append(actions);
      needsList.append(card);
    });

    approvals.forEach((item) => {
      const card = element('article', 'need approval');
      const kicker = element('p', 'need-kicker');
      const dot = element('span', 'need-dot');
      dot.style.background = '#fbbf24';
      kicker.append(dot, document.createTextNode(`Action approval · ${item.tool || 'tool'}`));
      card.append(kicker);

      const text = element('p', 'need-text');
      text.append(document.createTextNode('A.S.H.R.A wants to run '));
      text.append(element('code', '', item.tool || 'a tool'));
      text.append(document.createTextNode(' with '));
      let args = '{}';
      try {
        args = JSON.stringify(item.arguments || {});
      } catch (error) {
        args = '{}';
      }
      if (args.length > 180) args = `${args.slice(0, 177)}…`;
      text.append(element('code', '', args));
      text.append(document.createTextNode('.'));
      card.append(text);

      const actions = element('div', 'need-actions');
      const approve = element('button', 'btn approve', 'Approve');
      approve.type = 'button';
      approve.dataset.kind = 'approval';
      approve.dataset.id = item.id;
      approve.dataset.action = 'approve';
      const deny = element('button', 'btn deny', 'Deny');
      deny.type = 'button';
      deny.dataset.kind = 'approval';
      deny.dataset.id = item.id;
      deny.dataset.action = 'deny';
      actions.append(approve, deny);
      card.append(actions);
      needsList.append(card);
    });
  }

  async function actOnNeed(button) {
    const kind = button.dataset.kind;
    const id = button.dataset.id;
    const action = button.dataset.action;
    if (!kind || !id || !action) return;

    const card = button.closest('.need');
    if (card) {
      card.querySelectorAll('button').forEach((item) => { item.disabled = true; });
    }

    try {
      if (kind === 'memory') {
        await api(`/memory/${encodeURIComponent(id)}/${encodeURIComponent(action)}`, {
          method: 'POST'
        });
        showToast(action === 'confirm' ? 'Memory confirmed.' : 'Proposal rejected.');
      } else {
        const result = await api(`/approvals/${encodeURIComponent(id)}`, {
          method: 'POST',
          body: JSON.stringify({ approve: action === 'approve' })
        });
        const answer = result && typeof result.answer === 'string' && result.answer
          ? result.answer
          : (action === 'approve' ? 'Action approved.' : 'Action denied.');
        showToast(answer);
      }
      await loadHome();
    } catch (error) {
      showToast(error.message || 'That action failed.', true);
      if (card) {
        card.querySelectorAll('button').forEach((item) => { item.disabled = false; });
      }
    }
  }

  function renderThreads(data) {
    if (!threadList) return;
    const sessions = Array.isArray(data.recent.sessions) ? data.recent.sessions : [];
    threadList.replaceChildren();
    if (!sessions.length) {
      threadList.append(emptyState('◇', 'No conversations yet. Ask the Galaxy a question to start one.'));
      return;
    }
    sessions.forEach((session) => {
      const item = element('li');
      const button = element('button', 'thread');
      button.type = 'button';
      button.title = 'Open this thread in the Galaxy';
      const main = element('span', 'thread-main');
      const question = session.last_question || session.last_answer || 'Conversation';
      main.append(element('span', 'thread-question', question));
      const count = Number(session.messages || 0);
      main.append(element('span', 'thread-meta', `${count} ${count === 1 ? 'message' : 'messages'}`));
      button.append(main, element('span', 'thread-time', relativeTime(session.updated_at)));
      button.addEventListener('click', () => {
        try {
          sessionStorage.setItem('ashra-session', session.id);
        } catch (error) {
          /* Session storage is optional. */
        }
        window.location.href = 'galaxy.html';
      });
      item.append(button);
      threadList.append(item);
    });
  }

  function renderMemories(data) {
    if (!memoryList) return;
    const memories = Array.isArray(data.recent.memories) ? data.recent.memories : [];
    memoryList.replaceChildren();
    if (!memories.length) {
      memoryList.append(emptyState('◌', 'Nothing is confirmed yet. Capture something above or ingest your notes.'));
      return;
    }
    memories.forEach((memory) => {
      const item = element('li', 'memory-row');
      const dot = element('span', 'memory-dot', KIND_GLYPHS[memory.kind] || '◆');
      dot.style.color = kindColor(memory.kind);
      dot.setAttribute('aria-hidden', 'true');
      const copy = element('div', 'memory-copy');
      copy.append(element('span', 'memory-label', memory.label || memory.content || 'Memory'));
      const meta = element('div', 'memory-meta');
      meta.append(element('span', '', String(memory.kind || 'fact')));
      const confidence = element('span', 'confidence');
      const fill = element('span');
      const value = Math.max(0, Math.min(1, Number(memory.confidence || 0)));
      fill.style.width = `${Math.round(value * 100)}%`;
      fill.style.background = kindColor(memory.kind);
      confidence.append(fill);
      meta.append(confidence);
      meta.append(element('span', '', `${Math.round(value * 100)}%`));
      const when = relativeTime(memory.created_at);
      if (when) {
        meta.append(element('span', '', '·'));
        meta.append(element('span', '', when));
      }
      copy.append(meta);
      item.append(dot, copy);
      memoryList.append(item);
    });
  }

  function eventLabel(event) {
    const labels = {
      chat: 'Chat',
      memory: 'Memory',
      approval: 'Approval',
      tool: 'Tool',
      agent: 'Agent',
      vault: 'Vault'
    };
    return labels[event] || String(event || 'Event');
  }

  function renderActivity(data) {
    if (!activityList) return;
    const events = Array.isArray(data.recent.audit) ? data.recent.audit : [];
    activityList.replaceChildren();
    if (!events.length) {
      activityList.append(emptyState('·', 'No activity yet. The audit trail appears here as you use the assistant.'));
      return;
    }
    events.forEach((event) => {
      const item = element('li', 'activity-row');
      const glyphs = { chat: '✦', memory: '◆', approval: '⚖', tool: '⚙', agent: '◎', vault: '▤' };
      item.append(element('span', 'activity-icon', glyphs[event.event] || '·'));
      const text = element('span', 'activity-text');
      text.append(element('strong', '', eventLabel(event.event)));
      text.append(document.createTextNode(` ${event.action || ''} `));
      const status = element('span', event.status === 'error' ? 'error' : 'ok', event.status || 'ok');
      text.append(status);
      item.append(text, element('span', 'activity-time', relativeTime(event.created_at)));
      activityList.append(item);
    });
  }

  function renderConnectors(status) {
    if (!connectorList) return;
    const items = Array.isArray(status && status.integrations) ? status.integrations : [];
    const live = items.filter((item) => item.connected).length;

    if (connectorCount) {
      connectorCount.textContent = items.length ? `${live} / ${items.length} live` : 'local only';
      connectorCount.classList.toggle('l', items.length > 0);
    }

    connectorList.replaceChildren();
    if (!items.length) {
      connectorList.append(emptyState('◇', 'No connectors configured. A.S.H.R.A still works entirely on this machine.'));
      return;
    }

    items.forEach((item) => {
      const row = element('div', 'con');
      const dot = element('span', 's');
      if (item.connected) {
        dot.style.background = 'var(--lime)';
        dot.style.boxShadow = '0 0 8px rgba(182, 243, 106, 0.8)';
      } else if (item.enabled) {
        dot.style.background = 'var(--amber)';
        dot.style.boxShadow = '0 0 8px rgba(255, 200, 97, 0.8)';
      } else {
        dot.style.background = 'var(--faint)';
      }
      row.append(dot);
      row.append(element('span', 'nm', item.name || 'connector'));
      const tools = Number(item.tool_count || 0);
      row.append(element(
        'span',
        'meta',
        `${tools} ${tools === 1 ? 'tool' : 'tools'} · ${item.permission || 'read'}`
      ));
      if (item.error) row.title = String(item.error);
      connectorList.append(row);
    });
  }

  function renderAll(data) {
    homeData = data;
    setStatus(data.status);
    renderBrief(data);
    renderNeeds(data);
    renderThreads(data);
    renderMemories(data);
    renderActivity(data);
    renderConnectors(data.status);
  }

  async function loadHome() {
    try {
      const data = await api('/home');
      renderAll(data);
    } catch (error) {
      if (briefHeadline) briefHeadline.textContent = 'I cannot reach the server.';
      if (briefDetail) briefDetail.textContent = error.message || 'Check that python server.py is running.';
      showToast(error.message || 'Home could not load.', true);
    }
  }

  function setCaptureBusy(busy) {
    if (capturePropose) capturePropose.disabled = busy;
    if (captureRemember) captureRemember.disabled = busy;
  }

  async function capture(mode) {
    const text = captureText ? captureText.value.trim() : '';
    if (!text) {
      showToast('Write something to remember first.', true);
      if (captureText) captureText.focus();
      return;
    }

    setCaptureBusy(true);
    try {
      const remember = mode === 'remember';
      const data = await api('/memory', {
        method: 'POST',
        body: JSON.stringify({
          content: text,
          kind: captureKind ? captureKind.value : 'fact',
          status: remember ? 'confirmed' : 'proposed',
          source: 'home',
          confidence: remember ? 0.95 : undefined
        })
      });
      const item = data.memory || {};
      if (item.duplicate) {
        showToast('That was already known; I reinforced it.');
      } else {
        showToast(remember ? 'Remembered.' : 'Proposed for your approval.');
      }
      if (captureText) captureText.value = '';
      await loadHome();
    } catch (error) {
      showToast(error.message || 'That capture failed.', true);
    } finally {
      setCaptureBusy(false);
    }
  }

  async function ingestNotes() {
    const data = await api('/memory/ingest', { method: 'POST' });
    showToast(`Ingested notes: ${data.added || 0} new, ${data.updated || 0} updated.`);
    await loadHome();
  }

  async function consolidateMemory() {
    const data = await api('/memory/consolidate', { method: 'POST' });
    showToast(`Consolidated: ${data.merged || 0} merged, ${data.archived || 0} archived.`);
    await loadHome();
  }

  async function rebuildGalaxy() {
    const data = await api('/rebuild', { method: 'POST' });
    showToast(`Galaxy rebuilt: ${data.notes || 0} notes, ${data.links || 0} links.`);
  }

  const COMMANDS = [
    {
      id: 'ask',
      glyph: '✦',
      label: 'Ask A.S.H.R.A',
      hint: 'Open the Galaxy and ask a question',
      run: () => { window.location.href = 'galaxy.html'; }
    },
    {
      id: 'capture',
      glyph: '◆',
      label: 'Quick capture',
      hint: 'Focus the capture box',
      run: () => {
        closePalette();
        if (captureText) captureText.focus();
      }
    },
    {
      id: 'brain',
      glyph: '◉',
      label: 'Open the Brain',
      hint: 'Inspect, correct, and curate memory',
      run: () => { window.location.href = 'brain.html'; }
    },
    {
      id: 'galaxy',
      glyph: '✧',
      label: 'Open the Galaxy',
      hint: 'Map your notes as a knowledge graph',
      run: () => { window.location.href = 'galaxy.html'; }
    },
    {
      id: 'ingest',
      glyph: '⇣',
      label: 'Ingest notes',
      hint: 'Import note excerpts into memory',
      run: ingestNotes
    },
    {
      id: 'consolidate',
      glyph: '◇',
      label: 'Consolidate memory',
      hint: 'Merge duplicates and archive stale records',
      run: consolidateMemory
    },
    {
      id: 'rebuild',
      glyph: '↻',
      label: 'Rebuild the Galaxy',
      hint: 'Regenerate the note graph',
      run: rebuildGalaxy
    },
    {
      id: 'refresh',
      glyph: '⟳',
      label: 'Refresh Home',
      hint: 'Reload the brief and inbox',
      run: loadHome
    }
  ];

  function filterCommands(query) {
    const text = String(query || '').trim().toLowerCase();
    if (!text) return COMMANDS.slice();
    return COMMANDS.filter((command) => (
      command.label.toLowerCase().includes(text)
      || command.hint.toLowerCase().includes(text)
      || command.id.includes(text)
    ));
  }

  function renderPalette() {
    if (!paletteList) return;
    paletteList.replaceChildren();
    if (!paletteMatches.length) {
      paletteList.append(element('li', 'palette-empty', 'No command matches that.'));
      return;
    }
    paletteMatches.forEach((command, index) => {
      const item = element('li');
      const button = element('button', 'palette-item');
      button.type = 'button';
      button.classList.toggle('active', index === paletteIndex);
      button.append(element('span', 'palette-glyph', command.glyph));
      const copy = element('span', 'palette-copy');
      copy.append(element('strong', '', command.label));
      copy.append(element('span', '', command.hint));
      button.append(copy);
      button.addEventListener('click', () => runCommand(command));
      button.addEventListener('mouseenter', () => {
        paletteIndex = index;
        renderPalette();
      });
      item.append(button);
      paletteList.append(item);
    });
  }

  function openPalette() {
    if (!palette || !paletteInput) return;
    palette.hidden = false;
    paletteInput.value = '';
    paletteIndex = 0;
    paletteMatches = filterCommands('');
    renderPalette();
    window.setTimeout(() => paletteInput.focus(), 30);
  }

  function closePalette() {
    if (!palette) return;
    palette.hidden = true;
  }

  async function runCommand(command) {
    if (!command) return;
    closePalette();
    try {
      await command.run();
    } catch (error) {
      showToast(error.message || 'That command failed.', true);
    }
  }

  function movePalette(delta) {
    if (!paletteMatches.length) return;
    paletteIndex = (paletteIndex + delta + paletteMatches.length) % paletteMatches.length;
    renderPalette();
  }

  // ---------- wiring ----------

  if (needsList) {
    needsList.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-kind]');
      if (button) actOnNeed(button);
    });
  }

  if (capturePropose) {
    capturePropose.addEventListener('click', () => capture('propose'));
  }
  if (captureRemember) {
    captureRemember.addEventListener('click', () => capture('remember'));
  }
  if (captureText) {
    captureText.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        capture('remember');
      }
    });
  }

  const refreshButton = $('refresh-home');
  if (refreshButton) refreshButton.addEventListener('click', loadHome);

  const openPaletteButton = $('open-palette');
  if (openPaletteButton) openPaletteButton.addEventListener('click', openPalette);

  if (palette) {
    palette.addEventListener('click', (event) => {
      if (event.target.closest('[data-close-palette]')) closePalette();
    });
  }

  if (paletteInput) {
    paletteInput.addEventListener('input', () => {
      paletteMatches = filterCommands(paletteInput.value);
      paletteIndex = 0;
      renderPalette();
    });
    paletteInput.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        movePalette(1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        movePalette(-1);
      } else if (event.key === 'Enter') {
        event.preventDefault();
        runCommand(paletteMatches[paletteIndex]);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        closePalette();
      }
    });
  }

  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && String(event.key).toLowerCase() === 'k') {
      event.preventDefault();
      if (palette && !palette.hidden) closePalette();
      else openPalette();
      return;
    }
    if (event.key === 'Escape' && palette && !palette.hidden) {
      closePalette();
    }
  });

  loadHome();
})();
