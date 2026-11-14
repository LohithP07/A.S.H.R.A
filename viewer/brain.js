/* A.S.H.R.A — The Brain
   A dependency-free memory surface: force graph, timeline, and library over
   the local /memory API. */
(() => {
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
  const KIND_ORDER = [
    'fact', 'preference', 'person', 'project', 'event',
    'task', 'document', 'procedure', 'observation'
  ];
  const FALLBACK_COLOR = '#93c5fd';

  const state = {
    view: 'graph',
    status: '',
    kind: '',
    query: '',
    memories: [],
    stats: null,
    graphData: { nodes: [], links: [] },
    selectedId: null,
    detail: null,
    forgetArmed: false,
    busy: false,
    loading: false,
    loaded: false
  };

  /* ---------------------------------------------------------------- utils */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function colorFor(kind) {
    return KIND_COLORS[kind] || FALLBACK_COLOR;
  }

  function truncate(value, limit) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    if (text.length <= limit) return text;
    const cut = text.slice(0, limit).replace(/\s+\S*$/, '');
    return `${cut || text.slice(0, limit)}…`;
  }

  function pct(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '—';
  }

  function dateOf(seconds) {
    const number = Number(seconds);
    return Number.isFinite(number) ? new Date(number * 1000) : null;
  }

  function formatWhen(seconds) {
    const date = dateOf(seconds);
    if (!date) return '';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function formatDateFull(seconds) {
    const date = dateOf(seconds);
    if (!date) return '—';
    return date.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  }

  function dayLabel(date) {
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (date.toDateString() === today.toDateString()) return 'Today';
    if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return date.toLocaleDateString(undefined, {
      weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'
    });
  }

  let toastTimer = null;
  function toast(message, isError = false) {
    const node = $('toast');
    node.textContent = message;
    node.classList.toggle('error', Boolean(isError));
    node.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { node.hidden = true; }, 4200);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options
    });
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) {
      const message = (data && (data.detail || data.error)) || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return data;
  }

  function kindChip(kind) {
    const chip = el('span', 'chip kind', kind);
    chip.style.setProperty('--kind-color', colorFor(kind));
    return chip;
  }

  function statusChip(status) {
    return el('span', `chip status-${status}`, status);
  }

  function confidenceEl(value) {
    const wrap = el('span', 'confidence');
    const bar = el('span', 'confidence-bar');
    const fill = el('i');
    const amount = Math.max(0, Math.min(1, Number(value) || 0));
    fill.style.width = `${Math.round(amount * 100)}%`;
    bar.append(fill);
    wrap.append(bar, el('span', '', pct(value)));
    return wrap;
  }

  /* ---------------------------------------------------------------- data */

  async function fetchList() {
    if (state.query) {
      const params = new URLSearchParams({ q: state.query, limit: '80' });
      params.set('status', state.status || '');
      if (state.kind) params.set('kind', state.kind);
      const data = await api(`/memory/search?${params.toString()}`);
      return { memories: data.memories || [], stats: state.stats };
    }

    const params = new URLSearchParams({ limit: '500' });
    if (state.status) params.set('status', state.status);
    if (state.kind) params.set('kind', state.kind);
    const data = await api(`/memory?${params.toString()}`);
    return { memories: data.memories || [], stats: data.stats || null };
  }

  async function fetchGraph() {
    const params = new URLSearchParams({ limit: '500' });
    if (state.status) params.set('status', state.status);
    const data = await api(`/memory/graph?${params.toString()}`);
    return { nodes: data.nodes || [], links: data.links || [] };
  }

  async function refresh() {
    state.loading = true;
    renderLoading();
    try {
      const [listing, graph] = await Promise.all([fetchList(), fetchGraph()]);
      state.memories = listing.memories;
      if (listing.stats) state.stats = listing.stats;

      let nodes = graph.nodes;
      let links = graph.links;
      if (state.kind) {
        nodes = nodes.filter((node) => node.kind === state.kind);
        const allowed = new Set(nodes.map((node) => node.id));
        links = links.filter((link) => allowed.has(link.source) && allowed.has(link.target));
      }
      state.graphData = { nodes, links };

      if (state.selectedId && !state.memories.some((item) => item.id === state.selectedId)) {
        const stillThere = nodes.some((node) => node.id === state.selectedId);
        if (!stillThere) clearSelection();
      }

      renderStats();
      renderKinds();
      renderHealth();
      renderStatusFilters();
      renderLibrary();
      renderTimeline();
      renderGraph();
      updateSelectionClasses();
      state.loading = false;
      state.loaded = true;
      return true;
    } catch (error) {
      state.loading = false;
      if (state.loaded) {
        renderLibrary();
        renderTimeline();
      } else {
        renderLoadError();
      }
      toast(error.message || 'Could not load memory.', true);
      return false;
    }
  }

  /* -------------------------------------------------------------- loading */

  function skeletonRow(index) {
    const row = el('div', 'skeleton-row');
    row.style.setProperty('--w', `${92 - (index % 4) * 9}%`);
    const meta = el('div', 'skeleton-meta');
    meta.append(el('span', 'skeleton-chip'), el('span', 'skeleton-chip short'));
    row.append(el('span', 'skeleton-line'), meta);
    return row;
  }

  function renderSkeleton(view, label, rows) {
    view.replaceChildren();
    view.setAttribute('aria-busy', 'true');
    view.append(el('p', 'sr-only', label));
    const list = el('div', 'skeleton-list');
    list.setAttribute('aria-hidden', 'true');
    for (let index = 0; index < rows; index += 1) list.append(skeletonRow(index));
    view.append(list);
  }

  function renderLoading() {
    renderSkeleton($('view-library'), 'Loading memories…', 4);
    renderSkeleton($('view-timeline'), 'Loading timeline…', 4);
  }

  function renderLoadError() {
    const copy = 'Memory did not load. Check the local server and try again.';
    for (const id of ['view-library', 'view-timeline']) {
      const view = $(id);
      view.replaceChildren();
      view.removeAttribute('aria-busy');
      view.append(el('p', 'list-empty', copy));
    }
  }

  /* --------------------------------------------------------------- render */

  function renderStats() {
    const stats = state.stats || {};
    $('stat-total').textContent = stats.total ?? 0;
    $('stat-proposed').textContent = stats.proposed ?? 0;
    $('stat-links').textContent = stats.links ?? 0;

    const confirmed = (stats.by_status && stats.by_status.confirmed) || 0;
    const total = stats.total ?? 0;
    const lane = $('rail-lane');
    if (lane) {
      lane.textContent = `${total} ${total === 1 ? 'memory' : 'memories'} · ${stats.links ?? 0} links`;
    }
    const track = $('rail-track');
    if (track) {
      const share = total ? Math.round((confirmed / total) * 100) : 0;
      track.style.width = `${Math.max(6, Math.min(100, share))}%`;
    }
  }

  function renderKinds() {
    const list = $('kind-list');
    list.replaceChildren();
    const stats = state.stats || {};
    const byKind = stats.by_kind || {};
    const total = stats.total ?? 0;

    const allRow = el('li');
    const allButton = el('button', 'kind-row', '');
    allButton.type = 'button';
    if (!state.kind) allButton.classList.add('active');
    const allSwatch = el('span', 'kind-swatch');
    allSwatch.style.color = '#c4b5fd';
    allButton.append(allSwatch, el('span', 'kind-name', 'All kinds'), el('span', 'kind-count', String(total)));
    allButton.addEventListener('click', () => {
      state.kind = '';
      refresh();
    });
    allRow.append(allButton);
    list.append(allRow);

    const kinds = KIND_ORDER
      .filter((kind) => byKind[kind])
      .concat(Object.keys(byKind).filter((kind) => !KIND_ORDER.includes(kind)));

    for (const kind of kinds) {
      const row = el('li');
      const button = el('button', 'kind-row', '');
      button.type = 'button';
      if (state.kind === kind) button.classList.add('active');
      const swatch = el('span', 'kind-swatch');
      swatch.style.color = colorFor(kind);
      button.append(
        swatch,
        el('span', 'kind-name', kind),
        el('span', 'kind-count', String(byKind[kind]))
      );
      button.addEventListener('click', () => {
        state.kind = state.kind === kind ? '' : kind;
        refresh();
      });
      row.append(button);
      list.append(row);
    }
  }

  function renderHealth() {
    const list = $('health-list');
    list.replaceChildren();
    const health = (state.stats && state.stats.health) || {};
    const items = [
      ['Stale', health.stale || 0],
      ['Orphaned', health.orphaned || 0],
      ['Low confidence', health.low_confidence || 0],
      ['Duplicate groups', health.duplicate_groups || 0]
    ];
    for (const [label, value] of items) {
      const row = el('li');
      row.dataset.tone = value > 0 ? 'warn' : 'ok';
      row.append(el('span', '', label), el('b', '', String(value)));
      list.append(row);
    }
  }

  function renderStatusFilters() {
    document.querySelectorAll('#status-filters button').forEach((button) => {
      button.classList.toggle('active', button.dataset.status === state.status);
    });
  }

  function renderLibrary() {
    const view = $('view-library');
    view.replaceChildren();
    view.removeAttribute('aria-busy');

    const count = state.memories.length;
    const head = el('div', 'library-head');
    const label = state.query
      ? `${count} result${count === 1 ? '' : 's'} for “${truncate(state.query, 40)}”`
      : `${count} memor${count === 1 ? 'y' : 'ies'}`;
    head.append(el('span', '', label));
    head.append(el('span', '', state.status || 'all statuses'));
    view.append(head);

    if (!count) {
      view.append(el(
        'p',
        'list-empty',
        state.query ? 'Nothing in memory matches that search.' : 'No memories match these filters.'
      ));
      return;
    }

    const list = el('div', 'library-list');
    for (const memory of state.memories) list.append(memoryCard(memory));
    view.append(list);
  }

  function memoryCard(memory) {
    const card = el('button', 'memory-card');
    card.type = 'button';
    card.dataset.id = memory.id;
    if (memory.id === state.selectedId) card.classList.add('selected');

    card.append(el('p', 'memory-text', memory.label || memory.content));

    const meta = el('div', 'memory-meta');
    meta.append(kindChip(memory.kind), statusChip(memory.status));
    if (memory.pinned) meta.append(el('span', 'chip pinned', 'Pinned'));
    if (memory.source) meta.append(el('span', 'chip', memory.source));
    if (typeof memory.score === 'number') {
      meta.append(el('span', 'chip score', memory.score.toFixed(1)));
    }
    card.append(meta);

    const side = el('div', 'memory-side');
    side.append(el('span', 'memory-time', formatWhen(memory.created_at)));
    side.append(confidenceEl(memory.confidence));
    card.append(side);

    card.addEventListener('click', () => selectMemory(memory.id));
    return card;
  }

  function renderTimeline() {
    const view = $('view-timeline');
    view.replaceChildren();
    view.removeAttribute('aria-busy');
    const rows = [...state.memories].sort((a, b) => b.created_at - a.created_at);
    if (!rows.length) {
      view.append(el('p', 'list-empty', 'Nothing on the timeline yet.'));
      return;
    }

    const groups = new Map();
    for (const memory of rows) {
      const date = dateOf(memory.created_at);
      if (!date) continue;
      const key = date.toDateString();
      if (!groups.has(key)) groups.set(key, { date, items: [] });
      groups.get(key).items.push(memory);
    }

    for (const group of groups.values()) {
      const day = el('section', 'timeline-day');
      const head = el('div', 'timeline-day-head');
      head.append(
        el('h3', '', dayLabel(group.date)),
        el('span', '', `${group.items.length} ${group.items.length === 1 ? 'memory' : 'memories'}`)
      );
      const rail = el('div', 'timeline-rail');
      for (const memory of group.items) rail.append(timelineItem(memory));
      day.append(head, rail);
      view.append(day);
    }
  }

  function timelineItem(memory) {
    const item = el('button', 'timeline-item');
    item.type = 'button';
    item.dataset.id = memory.id;
    item.style.setProperty('--kind-color', colorFor(memory.kind));
    if (memory.id === state.selectedId) item.classList.add('selected');

    const meta = el('div', 'timeline-meta');
    meta.append(kindChip(memory.kind), statusChip(memory.status));
    if (memory.pinned) meta.append(el('span', 'chip pinned', 'Pinned'));
    const date = dateOf(memory.created_at);
    if (date) {
      meta.append(el('span', 'chip', date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })));
    }
    item.append(meta, el('p', 'timeline-text', memory.label || memory.content));

    item.addEventListener('click', () => selectMemory(memory.id));
    return item;
  }

  function updateSelectionClasses() {
    document.querySelectorAll('[data-id]').forEach((node) => {
      node.classList.toggle('selected', node.dataset.id === state.selectedId);
    });
  }

  /* --------------------------------------------------------------- detail */

  async function selectMemory(id, options = {}) {
    if (!id) {
      clearSelection();
      return;
    }
    state.selectedId = id;
    resetForget();
    updateSelectionClasses();
    kickGraph(0.3);
    drawGraph();
    try {
      const data = await api(`/memory/${encodeURIComponent(id)}`);
      if (state.selectedId !== id) return;
      renderDetail(data);
    } catch (error) {
      toast(options.deepLink ? 'That memory could not be found.' : error.message, true);
      if (options.deepLink && state.selectedId === id) clearSelection();
    }
  }

  /* ------------------------------------------------------------ deep links */

  function removeMemoryParam() {
    const url = new URL(window.location.href);
    if (!url.searchParams.has('memory')) return;
    url.searchParams.delete('memory');
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }

  async function applyDeepLink() {
    const params = new URLSearchParams(window.location.search);
    if (!params.has('memory')) return;
    const id = String(params.get('memory') || '').trim();
    if (!id) {
      toast('That memory link is missing an id.', true);
      removeMemoryParam();
      return;
    }
    await selectMemory(id, { deepLink: true });
  }

  function clearSelection() {
    state.selectedId = null;
    state.detail = null;
    resetForget();
    removeMemoryParam();
    $('detail-body').hidden = true;
    $('detail-empty').hidden = false;
    closeDetail();
    updateSelectionClasses();
    drawGraph();
  }

  function openDetail() {
    const panel = $('detail');
    if (!panel) return;
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
  }

  function closeDetail() {
    const panel = $('detail');
    if (!panel) return;
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }

  function renderDetail(data) {
    const memory = data.memory;
    if (!memory) return;
    state.detail = memory;
    resetForget();

    const body = $('detail-body');
    body.hidden = false;
    $('detail-empty').hidden = true;
    body.style.setProperty('--kind-color', colorFor(memory.kind));
    openDetail();

    $('detail-kind').textContent = memory.kind;
    $('detail-status').textContent = memory.status;
    $('detail-status').className = `detail-status status-${memory.status}`;
    $('detail-title').textContent = memory.label || truncate(memory.content, 90);
    $('detail-content').textContent = memory.content;
    $('detail-content').hidden = false;
    $('detail-edit').hidden = true;

    renderDetailMeta(memory);
    renderEntities(memory.entities || []);
    renderDetailActions(memory);
    renderRelated(data.related || []);
  }

  function renderDetailMeta(memory) {
    const meta = $('detail-meta');
    meta.replaceChildren();
    const entries = [
      ['Created', formatDateFull(memory.created_at)],
      ['Last seen', formatDateFull(memory.last_seen_at)],
      ['Source', memory.source + (memory.source_ref ? ` · ${memory.source_ref}` : '')],
      ['Confidence', `${pct(memory.confidence)} · reinforced ${memory.reinforcement}×`],
      ['Sensitivity', memory.sensitivity]
    ];
    if (memory.metadata && memory.metadata.title) {
      entries.push(['Title', memory.metadata.title]);
    }
    for (const [label, value] of entries) {
      meta.append(el('dt', '', label), el('dd', '', String(value ?? '—')));
    }
  }

  function renderEntities(entities) {
    const wrap = $('detail-entities');
    wrap.replaceChildren();
    for (const entity of entities) {
      const chip = el('span', 'chip', entity);
      wrap.append(chip);
    }
  }

  function renderDetailActions(memory) {
    $('action-confirm').disabled = memory.status === 'confirmed';
    $('action-reject').disabled = memory.status === 'rejected';
    $('action-pin').textContent = memory.pinned ? 'Unpin' : 'Pin';
    $('action-forget').textContent = 'Forget';
    $('action-forget').classList.remove('armed');
  }

  function renderRelated(related) {
    const list = $('detail-related');
    list.replaceChildren();
    $('related-count').textContent = related.length ? `· ${related.length}` : '';
    if (!related.length) {
      const empty = el('li', 'related-empty', 'No connections yet.');
      list.append(empty);
      return;
    }
    for (const item of related) {
      const row = el('li');
      const button = el('button', '');
      button.type = 'button';
      button.style.setProperty('--kind-color', colorFor(item.kind));
      const dot = el('span', 'kind-dot');
      button.append(dot, el('span', '', item.label || item.content));
      button.addEventListener('click', () => selectMemory(item.id));
      row.append(button);
      list.append(row);
    }
  }

  function resetForget() {
    state.forgetArmed = false;
    const button = $('action-forget');
    if (button) {
      button.textContent = 'Forget';
      button.classList.remove('armed');
    }
  }

  /* --------------------------------------------------------------- actions */

  async function afterChange(id, message) {
    toast(message);
    await refresh();
    if (id) await selectMemory(id);
  }

  async function confirmSelected() {
    if (!state.selectedId || state.busy) return;
    state.busy = true;
    try {
      await api(`/memory/${encodeURIComponent(state.selectedId)}/confirm`, { method: 'POST' });
      await afterChange(state.selectedId, 'Memory confirmed.');
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
  }

  async function rejectSelected() {
    if (!state.selectedId || state.busy) return;
    state.busy = true;
    try {
      await api(`/memory/${encodeURIComponent(state.selectedId)}/reject`, { method: 'POST' });
      await afterChange(state.selectedId, 'Memory rejected.');
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
  }

  async function togglePin() {
    if (!state.selectedId || state.busy || !state.detail) return;
    state.busy = true;
    try {
      const pinned = !state.detail.pinned;
      await api(`/memory/${encodeURIComponent(state.selectedId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ pinned })
      });
      await afterChange(state.selectedId, pinned ? 'Memory pinned.' : 'Memory unpinned.');
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
  }

  function startEdit() {
    if (!state.detail) return;
    $('detail-edit-input').value = state.detail.content;
    $('detail-content').hidden = true;
    $('detail-edit').hidden = false;
    $('detail-edit-input').focus();
  }

  function cancelEdit() {
    $('detail-edit').hidden = true;
    $('detail-content').hidden = false;
  }

  async function saveEdit(event) {
    event.preventDefault();
    if (!state.selectedId || state.busy) return;
    const content = $('detail-edit-input').value.trim();
    if (content.length < 3) {
      toast('A memory needs at least three characters.', true);
      return;
    }
    state.busy = true;
    try {
      await api(`/memory/${encodeURIComponent(state.selectedId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ content })
      });
      cancelEdit();
      await afterChange(state.selectedId, 'Memory updated.');
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
  }

  async function forgetSelected() {
    if (!state.selectedId || state.busy) return;
    if (!state.forgetArmed) {
      state.forgetArmed = true;
      const button = $('action-forget');
      button.textContent = 'Confirm forget';
      button.classList.add('armed');
      return;
    }

    const id = state.selectedId;
    state.busy = true;
    try {
      await api(`/memory/${encodeURIComponent(id)}/forget`, {
        method: 'POST',
        body: JSON.stringify({ confirm: true })
      });
      state.selectedId = null;
      state.detail = null;
      resetForget();
      toast('Memory forgotten.');
      await refresh();
      clearSelection();
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
  }

  /* -------------------------------------------------------------- composer */

  function openComposer() {
    $('composer').hidden = false;
    const input = $('composer-text');
    input.value = '';
    input.focus();
  }

  function closeComposer() {
    $('composer').hidden = true;
  }

  async function proposeFromComposer() {
    const text = $('composer-text').value.trim();
    if (!text) {
      toast('Write something first.', true);
      return;
    }
    try {
      const data = await api('/memory/propose', {
        method: 'POST',
        body: JSON.stringify({ text, source: 'manual' })
      });
      closeComposer();
      state.status = 'proposed';
      state.query = '';
      $('search-input').value = '';
      $('search-clear').hidden = true;
      toast(`${data.count} proposal${data.count === 1 ? '' : 's'} added for review.`);
      setView('library');
      await refresh();
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function rememberFromComposer() {
    const text = $('composer-text').value.trim();
    if (!text) {
      toast('Write something first.', true);
      return;
    }
    const kind = $('composer-kind').value;
    try {
      const data = await api('/memory', {
        method: 'POST',
        body: JSON.stringify({ content: text, kind, status: 'confirmed', source: 'manual' })
      });
      closeComposer();
      state.status = '';
      state.query = '';
      $('search-input').value = '';
      $('search-clear').hidden = true;
      toast('Memory stored.');
      setView('library');
      await refresh();
      if (data.memory && data.memory.id) selectMemory(data.memory.id);
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* ------------------------------------------------------------ maintenance */

  async function ingestNotes() {
    const button = $('ingest-notes');
    button.disabled = true;
    try {
      const data = await api('/memory/ingest', { method: 'POST' });
      toast(`Notes ingested: ${data.added} added, ${data.updated} refreshed.`);
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function consolidate() {
    const button = $('consolidate');
    button.disabled = true;
    try {
      const data = await api('/memory/consolidate', { method: 'POST' });
      toast(`Consolidated: ${data.merged_groups} merged, ${data.archived} archived, ${data.decayed} decayed.`);
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  /* ----------------------------------------------------------------- graph */

  const canvas = $('graph-canvas');
  const ctx = canvas.getContext('2d');
  const viewGraph = $('view-graph');

  let width = 1;
  let height = 1;
  let dpr = 1;
  let nodes = [];
  let links = [];
  let transform = { x: 0, y: 0, k: 1 };
  let alpha = 0;
  let raf = null;
  let hoverId = null;
  let autoFit = true;

  const pointer = {
    down: false,
    moved: false,
    mode: null,
    node: null,
    startX: 0,
    startY: 0,
    panX: 0,
    panY: 0,
    panStartX: 0,
    panStartY: 0
  };

  function resizeCanvas() {
    const rect = viewGraph.getBoundingClientRect();
    width = Math.max(1, rect.width);
    height = Math.max(1, rect.height);
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    drawGraph();
  }

  function renderGraph() {
    const data = state.graphData;
    const existing = new Map(nodes.map((node) => [node.id, node]));
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) * 0.32;

    nodes = data.nodes.map((node, index) => {
      const previous = existing.get(node.id);
      const angle = (index / Math.max(1, data.nodes.length)) * Math.PI * 2;
      const jitter = ((index * 37) % 11) / 11;
      return {
        ...node,
        x: previous ? previous.x : cx + Math.cos(angle) * radius * (0.55 + jitter * 0.6),
        y: previous ? previous.y : cy + Math.sin(angle) * radius * (0.55 + jitter * 0.6),
        vx: 0,
        vy: 0,
        r: 6 + Math.min(Number(node.degree) || 0, 8) * 1.5 + (node.pinned ? 2 : 0)
      };
    });

    const byId = new Map(nodes.map((node) => [node.id, node]));
    links = data.links
      .map((link) => ({
        ...link,
        source: byId.get(link.source),
        target: byId.get(link.target)
      }))
      .filter((link) => link.source && link.target);

    const empty = $('graph-empty');
    empty.hidden = nodes.length > 0;
    canvas.style.visibility = nodes.length > 0 ? 'visible' : 'hidden';

    renderGraphLegend();
    kickGraph(1);
  }

  function renderGraphLegend() {
    const legend = $('graph-legend');
    legend.replaceChildren();
    const kinds = new Set(nodes.map((node) => node.kind));
    for (const kind of KIND_ORDER) {
      if (!kinds.has(kind)) continue;
      const item = el('span', '');
      const dot = el('i');
      dot.style.color = colorFor(kind);
      item.style.color = colorFor(kind);
      item.append(dot, document.createTextNode(kind));
      legend.append(item);
    }
  }

  function kickGraph(amount = 0.6) {
    alpha = Math.max(alpha, amount);
    if (!raf) raf = window.requestAnimationFrame(graphLoop);
  }

  function fitGraph(padding = 44) {
    if (!nodes.length) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      minX = Math.min(minX, node.x - node.r);
      minY = Math.min(minY, node.y - node.r);
      maxX = Math.max(maxX, node.x + node.r);
      maxY = Math.max(maxY, node.y + node.r);
    }
    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    const fit = Math.min((width - padding * 2) / spanX, (height - padding * 2) / spanY);
    const k = Math.max(0.42, Math.min(1.1, fit));
    transform = {
      k,
      x: width / 2 - ((minX + maxX) / 2) * k,
      y: height / 2 - ((maxY + minY) / 2) * k
    };
  }

  function graphLoop() {
    raf = null;
    if (alpha > 0.004) {
      tickGraph();
      alpha *= 0.985;
      if (autoFit) fitGraph();
      drawGraph();
      raf = window.requestAnimationFrame(graphLoop);
    } else {
      if (autoFit) fitGraph();
      drawGraph();
    }
  }

  function tickGraph() {
    const cx = width / 2;
    const cy = height / 2;

    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distance2 = dx * dx + dy * dy;
        if (distance2 < 1) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          distance2 = 1;
        }
        if (distance2 > 420000) continue;
        const force = 1500 / distance2;
        const distance = Math.sqrt(distance2);
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }

    for (const link of links) {
      const a = link.source;
      const b = link.target;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.hypot(dx, dy) || 1;
      const force = (distance - 92) * 0.006;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    for (const node of nodes) {
      if (pointer.node === node) {
        node.vx = 0;
        node.vy = 0;
        continue;
      }
      node.vx += (cx - node.x) * 0.0014;
      node.vy += (cy - node.y) * 0.0014;
      node.vx *= 0.86;
      node.vy *= 0.86;
      node.vx = Math.max(-6, Math.min(6, node.vx));
      node.vy = Math.max(-6, Math.min(6, node.vy));
      node.x += node.vx;
      node.y += node.vy;
    }
  }

  function drawGraph() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!nodes.length) return;

    const selected = state.selectedId;
    const neighbours = new Set();
    if (selected) {
      for (const link of links) {
        if (link.source.id === selected) neighbours.add(link.target.id);
        if (link.target.id === selected) neighbours.add(link.source.id);
      }
    }

    ctx.save();
    ctx.translate(transform.x, transform.y);
    ctx.scale(transform.k, transform.k);

    for (const link of links) {
      const active = selected && (link.source.id === selected || link.target.id === selected);
      ctx.strokeStyle = active ? 'rgba(167, 139, 250, 0.6)' : 'rgba(151, 174, 207, 0.13)';
      ctx.lineWidth = (active ? 1.5 : 1) / transform.k;
      ctx.beginPath();
      ctx.moveTo(link.source.x, link.source.y);
      ctx.lineTo(link.target.x, link.target.y);
      ctx.stroke();
    }

    // only the busiest few keep a permanent label; the rest appear on hover,
    // selection, or when zoomed in far enough to read them
    const topLabels = new Set(
      [...nodes]
        .sort((a, b) => (Number(b.degree) || 0) - (Number(a.degree) || 0))
        .slice(0, 6)
        .map((node) => node.id)
    );
    const labelNodes = [];

    for (const node of nodes) {
      const isSelected = node.id === selected;
      const isHover = node.id === hoverId;
      const dim = Boolean(selected) && !isSelected && !neighbours.has(node.id);
      const color = colorFor(node.kind);

      ctx.globalAlpha = dim ? 0.26 : 1;
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.shadowColor = color;
      ctx.shadowBlur = isSelected ? 26 : isHover ? 18 : 10;
      ctx.fill();
      ctx.shadowBlur = 0;

      if (node.pinned) {
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.55)';
        ctx.lineWidth = 1.2 / transform.k;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.r + 3, 0, Math.PI * 2);
        ctx.stroke();
      }

      if (isSelected) {
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.6 / transform.k;
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.r + 5.5, 0, Math.PI * 2);
        ctx.stroke();
      }

      if (
        isSelected ||
        isHover ||
        (selected && neighbours.has(node.id)) ||
        transform.k > 1.25 ||
        topLabels.has(node.id)
      ) {
        labelNodes.push({ node, dim, isSelected });
      }
    }

    // labels sit above every node so they never disappear behind a sphere
    const priority = (entry) => (entry.isSelected ? 2 : entry.isHover ? 1 : 0);
    labelNodes.sort((a, b) => priority(b) - priority(a));

    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    const placed = [];
    for (const entry of labelNodes) {
      const { node, dim, isSelected } = entry;
      const size = (isSelected ? 11 : 10) / transform.k;
      const text = truncate(node.label, isSelected ? 42 : 26);
      const y = node.y - node.r - 8 / transform.k - size * 0.5;

      ctx.font = `${isSelected ? 500 : 400} ${size}px Inter, ui-sans-serif, sans-serif`;
      const boxWidth = ctx.measureText(text).width * transform.k + 10;
      const boxHeight = size * transform.k * 1.6;
      const boxX = (node.x * transform.k + transform.x) - boxWidth / 2;
      const boxY = (y * transform.k + transform.y) - boxHeight / 2;
      const collides = placed.some(
        (box) =>
          boxX < box.x + box.w &&
          box.x < boxX + boxWidth &&
          boxY < box.y + box.h &&
          box.y < boxY + boxHeight
      );
      if (collides) continue;
      placed.push({ x: boxX, y: boxY, w: boxWidth, h: boxHeight });

      ctx.globalAlpha = dim ? 0.25 : 1;
      ctx.strokeStyle = 'rgba(6, 5, 14, 0.82)';
      ctx.lineWidth = 3.2 / transform.k;
      ctx.strokeText(text, node.x, y);
      ctx.fillStyle = isSelected ? '#ffffff' : 'rgba(214, 224, 242, 0.9)';
      ctx.fillText(text, node.x, y);
    }

    ctx.globalAlpha = 1;
    ctx.restore();
  }

  function worldPoint(event) {
    const rect = canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    return {
      x: (sx - transform.x) / transform.k,
      y: (sy - transform.y) / transform.k
    };
  }

  function hitTest(x, y) {
    for (let index = nodes.length - 1; index >= 0; index -= 1) {
      const node = nodes[index];
      const reach = node.r + 6 / transform.k;
      if ((node.x - x) ** 2 + (node.y - y) ** 2 <= reach * reach) return node;
    }
    return null;
  }

  canvas.addEventListener('pointerdown', (event) => {
    canvas.setPointerCapture(event.pointerId);
    const point = worldPoint(event);
    const hit = hitTest(point.x, point.y);
    pointer.down = true;
    pointer.moved = false;
    pointer.startX = event.clientX;
    pointer.startY = event.clientY;
    if (hit) {
      pointer.mode = 'node';
      pointer.node = hit;
      hit.vx = 0;
      hit.vy = 0;
      canvas.classList.add('grabbing-node');
    } else {
      pointer.mode = 'pan';
      pointer.panX = transform.x;
      pointer.panY = transform.y;
      pointer.panStartX = event.clientX;
      pointer.panStartY = event.clientY;
    }
  });

  canvas.addEventListener('pointermove', (event) => {
    const point = worldPoint(event);
    if (!pointer.down) {
      const hit = hitTest(point.x, point.y);
      const nextHover = hit ? hit.id : null;
      canvas.style.cursor = hit ? 'pointer' : 'grab';
      if (nextHover !== hoverId) {
        hoverId = nextHover;
        drawGraph();
      }
      return;
    }

    if (Math.hypot(event.clientX - pointer.startX, event.clientY - pointer.startY) > 3) {
      pointer.moved = true;
    }

    if (pointer.mode === 'node' && pointer.node) {
      autoFit = false;
      pointer.node.x = point.x;
      pointer.node.y = point.y;
      pointer.node.vx = 0;
      pointer.node.vy = 0;
      kickGraph(0.35);
    } else if (pointer.mode === 'pan') {
      autoFit = false;
      transform.x = pointer.panX + (event.clientX - pointer.panStartX);
      transform.y = pointer.panY + (event.clientY - pointer.panStartY);
      drawGraph();
    }
  });

  function endPointer(event) {
    const clicked = !pointer.moved && pointer.node;
    const node = pointer.node;
    pointer.down = false;
    pointer.mode = null;
    pointer.node = null;
    canvas.classList.remove('grabbing-node');
    if (event && event.pointerId !== undefined && canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    if (clicked && node) selectMemory(node.id);
  }

  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', endPointer);

  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    autoFit = false;
    const rect = canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    const factor = Math.exp(-event.deltaY * 0.0012);
    const next = Math.max(0.3, Math.min(3.4, transform.k * factor));
    const wx = (sx - transform.x) / transform.k;
    const wy = (sy - transform.y) / transform.k;
    transform.k = next;
    transform.x = sx - wx * next;
    transform.y = sy - wy * next;
    drawGraph();
  }, { passive: false });

  canvas.addEventListener('dblclick', () => {
    autoFit = true;
    transform = { x: 0, y: 0, k: 1 };
    fitGraph();
    drawGraph();
  });

  /* ----------------------------------------------------------------- views */

  function setView(view) {
    state.view = view;
    document.querySelectorAll('.tabs button').forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    $('view-graph').hidden = view !== 'graph';
    $('view-timeline').hidden = view !== 'timeline';
    $('view-library').hidden = view !== 'library';
    if (view === 'graph') {
      resizeCanvas();
      kickGraph(0.5);
    }
  }

  /* ---------------------------------------------------------------- events */

  function bindEvents() {
    document.querySelectorAll('.tabs button').forEach((button) => {
      button.addEventListener('click', () => setView(button.dataset.view));
    });

    document.querySelectorAll('#status-filters button').forEach((button) => {
      button.addEventListener('click', () => {
        state.status = button.dataset.status;
        renderStatusFilters();
        refresh();
      });
    });

    $('search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const value = $('search-input').value.trim();
      state.query = value;
      $('search-clear').hidden = !value;
      if (value) setView('library');
      refresh();
    });

    $('search-input').addEventListener('input', () => {
      $('search-clear').hidden = !$('search-input').value;
    });

    $('search-clear').addEventListener('click', () => {
      $('search-input').value = '';
      $('search-clear').hidden = true;
      state.query = '';
      refresh();
      $('search-input').focus();
    });

    $('new-memory').addEventListener('click', openComposer);
    $('composer-cancel').addEventListener('click', closeComposer);
    $('composer-propose').addEventListener('click', proposeFromComposer);
    $('composer-remember').addEventListener('click', rememberFromComposer);
    $('composer').addEventListener('click', (event) => {
      if (event.target === $('composer')) closeComposer();
    });

    $('ingest-notes').addEventListener('click', ingestNotes);
    $('consolidate').addEventListener('click', consolidate);

    $('action-confirm').addEventListener('click', confirmSelected);
    $('action-reject').addEventListener('click', rejectSelected);
    $('action-pin').addEventListener('click', togglePin);
    $('action-edit').addEventListener('click', startEdit);
    $('detail-edit-cancel').addEventListener('click', cancelEdit);
    $('detail-edit').addEventListener('submit', saveEdit);
    $('action-forget').addEventListener('click', forgetSelected);
    $('detail-close').addEventListener('click', clearSelection);

    document.addEventListener('keydown', (event) => {
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
      if (event.key === 'Escape') {
        if (!$('composer').hidden) {
          closeComposer();
        } else if (!$('detail-edit').hidden) {
          cancelEdit();
        } else if (state.selectedId) {
          clearSelection();
        }
        return;
      }
      if (event.key === '/' && !typing) {
        event.preventDefault();
        $('search-input').focus();
      }
    });

    const observer = new ResizeObserver(() => resizeCanvas());
    observer.observe(viewGraph);
  }

  /* ------------------------------------------------------------------ init */

  async function init() {
    bindEvents();
    resizeCanvas();
    if (window.ASHRALane) window.ASHRALane.load();
    const loaded = await refresh();
    if (loaded) await applyDeepLink();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
