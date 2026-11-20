'use strict';
/* Software factory dashboard v2. No inline scripts or styles (runs under the content security policy).
   Text from models, repos, issues, logs and events reaches the page through textContent or the
   escape-first markdown renderer, never through innerHTML of raw text. */

const COLUMNS = [
  { key: 'queued', label: 'Queued', hint: 'waiting for a worker' },
  { key: 'triage', label: 'Triage', hint: 'size and route' },
  { key: 'spec', label: 'Spec', hint: 'spec-writer' },
  { key: 'build', label: 'Build', hint: 'builder · tests' },
  { key: 'review', label: 'Review', hint: 'security · ux · ui · code' },
  { key: 'approve', label: 'Approve', hint: 'approver · delivery' },
  { key: 'needs_human', label: 'Needs human', hint: 'approve · rework · reject' },
  { key: 'done', label: 'Done', hint: 'merged · closed' },
];
const ACTIVE_COLUMNS = new Set(['triage', 'spec', 'build', 'review', 'approve']);
const REVIEWERS = [
  { key: 'security', short: 'sec' },
  { key: 'ux', short: 'ux' },
  { key: 'ui', short: 'ui' },
  { key: 'code', short: 'code' },
];
const ROLE_RANK = { viewer: 0, submitter: 1, approver: 2, admin: 3 };
const TERMINAL = new Set(['merged', 'closed', 'cancelled']);
const MAX_REQUIREMENT_BYTES = 200 * 1024;
const BOARD_DEBOUNCE_MS = 250;
const POLL_MS = 3000;
const SSE_RETRY_MS = 30000;
const SSE_KINDS = [
  'message', 'job.created', 'job.stage', 'job.failed', 'job.stuck', 'job.superseded', 'job.expired',
  'human.waiting', 'human.action', 'decision.made', 'delivery.pr_opened', 'delivery.merged',
  'delivery.closed', 'delivery.rebased', 'run.started', 'run.finished', 'run.progress',
  'backlog.created', 'backlog.approved', 'backlog.paused', 'backlog.cancelled', 'backlog.done', 'backlog.resumed',
  'models.refreshed',
];

const $ = (s, r = document) => r.querySelector(s);

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === 'data') for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    else if (k === 'value') el.value = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const indentOf = (s) => s.match(/^\s*/)[0].replace(/\t/g, '    ').length;
const money = (v) => (v === null || v === undefined || v === '' || isNaN(Number(v)) ? '' : '$' + Number(v).toFixed(2));

function ago(ts) {
  const t = Date.parse(ts);
  if (!ts || isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
function duration(a, b) {
  const s = Date.parse(a), e = Date.parse(b);
  if (isNaN(s) || isNaN(e)) return '';
  const sec = Math.max(0, Math.round((e - s) / 1000));
  return sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`;
}

/* ── state ──────────────────────────────────────── */
const state = {
  me: null,
  board: { jobs: [], apps: [], last_event_id: 0 },
  boardLoaded: false,
  app: '',
  loadError: null,
  signedOut: false,
  view: null,
  drawer: null, // { job_id, tab, file, actionsSig, updated }
  token: 0, // bumps on every route change; stops stale polls
};
const rank = () => ROLE_RANK[state.me?.role] ?? 0;
const can = (role) => rank() >= ROLE_RANK[role];

/* ── api ────────────────────────────────────────── */
async function api(path, opts = {}) {
  const init = { credentials: 'same-origin', cache: 'no-store', headers: {}, ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || 'POST';
    init.headers = { 'content-type': 'application/json' };
    init.body = JSON.stringify(opts.json);
    delete init.json;
  }
  const res = await fetch(path, init);
  if (res.status === 401) {
    state.signedOut = true;
    renderChrome();
    throw new Error('Signed out');
  }
  return res;
}
async function apiJson(path, opts) {
  const res = await api(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const d = body && body.detail;
    throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : `HTTP ${res.status}`);
  }
  return body;
}
const jobPath = (job, tail) => `/api/jobs/${encodeURIComponent(job.ref)}/${tail}`;
const artifactUrl = (job, path) => jobPath(job, 'artifacts/' + path.split('/').map(encodeURIComponent).join('/'));

/* ── board helpers ──────────────────────────────── */
const findJob = (id) => (state.board.jobs || []).find((j) => j.job_id === id || j.ref === id || j.id === id);
function splitId(id) { const [num, ...rest] = String(id).split('-'); return { num, slug: rest.join('-') }; }
function verdictOf(v) {
  const u = String(v || '').toUpperCase();
  return u.startsWith('PASS') ? 'pass' : u.startsWith('CHANGE') ? 'changes' : 'pending';
}
function roundsOf(job) {
  const rounds = (Array.isArray(job.history) ? job.history : [])
    .filter((r) => r && r.reviews).map((r) => ({ round: Number(r.round) || 0, reviews: r.reviews }));
  const cur = Number(job.round) || 0;
  const reviews = job.reviews || {};
  if (cur && !rounds.some((r) => r.round === cur) && Object.keys(reviews).length) rounds.push({ round: cur, reviews });
  return rounds.sort((a, b) => a.round - b.round);
}
function statusOf(job, rounds) {
  const r = Number(job.round) || 0;
  switch (job.stage) {
    case 'queued': return 'queued';
    case 'triage': return 'triaging';
    case 'spec': return 'writing spec';
    case 'build': return r > 1 ? `rework for round ${r}` : 'building';
    case 'verify': return 'testing';
    case 'review': {
      const cur = rounds.find((x) => x.round === r);
      const n = cur ? REVIEWERS.filter((v) => verdictOf(cur.reviews[v.key]) !== 'pending').length : 0;
      return `round ${r} · ${n}/4 in`;
    }
    case 'approve': return 'approver deciding';
    case 'delivering': return 'delivering';
    case 'needs_human': return 'needs you';
    case 'failed': return 'failed';
    case 'delivered': return 'delivered · PR open';
    case 'merged': return 'merged';
    case 'closed': return 'closed';
    case 'cancelled': return 'cancelled';
    default: return job.stage;
  }
}

/* ── chrome: header signal, notice, nav ─────────── */
function renderChrome() {
  const jobs = state.board.jobs || [];
  const needs = jobs.filter((j) => j.column === 'needs_human').length;
  const active = jobs.filter((j) => ACTIVE_COLUMNS.has(j.column)).length;
  const sig = $('#signal');
  let lamp = '', msg, cls = '';
  if (state.signedOut) { msg = 'Signed out'; cls = 'error'; }
  else if (state.loadError) { msg = "Can't read the board"; cls = 'error'; }
  else if (needs) { lamp = 'alarm'; cls = 'alarm'; msg = needs === 1 ? '1 job needs you' : `${needs} jobs need you`; }
  else if (active) { lamp = 'on'; msg = `${active} job${active === 1 ? '' : 's'} in progress`; }
  else msg = state.boardLoaded ? 'Line idle' : 'Connecting…';
  sig.className = 'signal ' + cls;
  $('.lamp', sig).className = 'lamp ' + lamp;
  $('.msg', sig).textContent = msg;
  const latest = Math.max(0, ...jobs.map((j) => Date.parse(j.updated) || 0));
  $('.read', sig).textContent = state.loadError ? 'retrying…' : latest ? `last change ${ago(new Date(latest).toISOString())}` : '';
  document.title = needs ? `(${needs}) Factory floor` : 'Factory floor';

  const notice = $('#notice');
  notice.replaceChildren();
  if (state.signedOut) {
    notice.append('Signed out: open the login link from ', h('code', { text: 'make up' }), ' or run ', h('code', { text: 'factory login-url' }));
    notice.hidden = false;
  } else if (state.loadError) {
    notice.textContent = `Can't load the board (${state.loadError}). Retrying.`;
    notice.hidden = false;
  } else notice.hidden = true;
  for (const a of document.querySelectorAll('#nav a')) {
    if (a.dataset.route === state.view) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
}

/* ── board view ─────────────────────────────────── */
function chip(job, round, rv, raw) {
  const v = verdictOf(raw);
  const word = v === 'pass' ? 'pass' : v === 'changes' ? 'changes requested' : 'pending';
  return h('span', { class: `chip ${v}`, title: `${rv.key}: ${word}` }, rv.short, h('span', { class: 'sr', text: `: ${word}` }));
}
function cardEl(job) {
  const { num, slug } = splitId(job.id);
  const rounds = roundsOf(job);
  const st = job.stage;
  const cls = 'card' + (job.column === 'needs_human' ? ' alarm' : '') + (job.column === 'done' ? ' done' : '');
  const roles = Object.entries(job.models || {});
  return h('a', { class: cls, href: `#/job/${encodeURIComponent(job.job_id)}`, data: { id: job.id } },
    h('span', { class: 'card-id' }, h('span', { class: 'num', text: num }), h('span', { class: 'slug', text: slug }), h('span', { class: 'app-tag', text: job.app })),
    h('span', { class: 'title', text: job.title || slug.replace(/-/g, ' ') }),
    job.stuck && h('span', { class: 'badge', text: 'No progress for 30 min' }),
    job.quality && h('span', { class: `quality ${job.quality.badge}`, title: `${job.quality.judges_passed} of ${job.quality.judges_total} judges passed`, text: `Quality: ${job.quality.badge}` }),
    job.kind === 'requirements' && h('span', { class: 'badge kind-tag', text: 'Requirements' }),
    job.backlog_ref && h('span', { class: 'backlog-link', data: { ref: job.backlog_ref }, text: `Backlog ${job.backlog_ref}`, role: 'link', tabindex: '-1',
      on: { click: (e) => { e.preventDefault(); e.stopPropagation(); location.hash = `#/backlog/${encodeURIComponent(job.backlog_ref)}`; } } }),
    roles.length > 0 && h('span', { class: 'model-chips' },
      roles.map(([role, m]) => h('span', { class: 'model-chip', data: { role }, title: `${m.engine} · ${m.mode}`, text: `${role} ${m.alias}` })),
      job.fallback && h('span', { class: 'fallback-icon', role: 'img', 'aria-label': 'Model fallback detected', text: '⚠' })),
    rounds.length > 0 && h('span', { class: 'rounds' }, rounds.map((r) =>
      h('span', { class: 'round' }, h('span', { class: 'r', text: `R${r.round}` }), REVIEWERS.map((v) => chip(job, r.round, v, r.reviews?.[v.key]))))),
    h('span', { class: 'card-foot' }, h('span', { class: 'cost', text: money(job.cost_usd) }), st === 'verify' || job.budget_usd ? h('span', { text: job.budget_usd ? `of ${money(job.budget_usd)}` : '' }) : null),
    h('span', { class: 'status' }, h('span', { class: 'what', text: statusOf(job, rounds) }), h('span', { data: { ts: job.updated || '' }, text: ago(job.updated) })));
}
function renderFloor() {
  const floor = $('#floor');
  if (!floor) return;
  const jobs = (state.board.jobs || []).slice().sort((a, b) => String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
  const focusedId = document.activeElement?.dataset?.id;
  floor.replaceChildren(...COLUMNS.map((c) => {
    const list = jobs.filter((j) => j.column === c.key);
    const alarm = c.key === 'needs_human' && list.length > 0;
    const lamp = alarm ? 'alarm' : ACTIVE_COLUMNS.has(c.key) && list.length ? 'on' : '';
    return h('div', { class: 'station' + (alarm ? ' alarm' : '') },
      h('header', { class: 'station-head' },
        h('div', { class: 'line' }, h('span', { class: `lamp ${lamp}` }), h('h2', { text: c.label }), h('span', { class: 'count', text: String(list.length) })),
        h('p', { text: c.hint })),
      h('div', { class: 'lane' }, list.map(cardEl)));
  }));
  if (focusedId) $(`.card[data-id="${CSS.escape(focusedId)}"]`)?.focus();
}
function updateAppFilter() {
  const sel = $('#app-filter');
  if (!sel) return;
  const apps = state.board.apps || [];
  const sig = apps.join('|');
  if (sel.dataset.sig !== sig) {
    sel.replaceChildren(h('option', { value: '', text: 'All apps' }), ...apps.map((a) => h('option', { value: a, text: a })));
    sel.dataset.sig = sig;
  }
  sel.value = state.app;
}
function showBoardView() {
  const main = $('#main');
  main.replaceChildren(
    h('div', { class: 'toolbar' },
      h('label', { for: 'app-filter', text: 'App' }),
      h('select', { id: 'app-filter', on: { change: (e) => { state.app = e.target.value; reloadBoardAndLive(); } } })),
    h('div', { class: 'floor', id: 'floor' }));
  updateAppFilter();
  renderFloor();
}

async function loadBoard() {
  const mine = ++loadBoard.seq;
  try {
    const q = state.app ? `?app=${encodeURIComponent(state.app)}` : '';
    const b = await apiJson('/api/board' + q);
    if (mine !== loadBoard.seq) return;
    if (!b || !Array.isArray(b.jobs)) throw new Error('no jobs in the response');
    state.board = b;
    state.boardLoaded = true;
    state.loadError = null;
    state.signedOut = false;
    if (state.view === 'board') { updateAppFilter(); renderFloor(); }
    updateDrawer();
  } catch (e) {
    if (mine !== loadBoard.seq || state.signedOut) return;
    state.loadError = e.message || 'network error';
  }
  renderChrome();
}
loadBoard.seq = 0;

/* ── live updates: SSE first, polling after an error ── */
const live = { es: null, poll: null, retry: null, debounce: null };
function stopPoll() { clearTimeout(live.poll); live.poll = null; }
function startPoll() {
  if (live.poll) return;
  const tick = async () => { await loadBoard(); if (live.poll) live.poll = setTimeout(tick, POLL_MS); };
  live.poll = setTimeout(tick, POLL_MS);
}
function closeSse() { if (live.es) { live.es.close(); live.es = null; } }
function openSse() {
  closeSse();
  clearTimeout(live.retry);
  const q = new URLSearchParams();
  if (state.app) q.set('app', state.app);
  q.set('after', String(state.board.last_event_id ?? 0));
  const es = new EventSource('/api/events?' + q);
  live.es = es;
  const onEvent = () => {
    clearTimeout(live.debounce);
    live.debounce = setTimeout(loadBoard, BOARD_DEBOUNCE_MS);
  };
  for (const k of SSE_KINDS) es.addEventListener(k, onEvent);
  for (const k of SSE_KINDS) if (k.startsWith('backlog.') || k === 'job.stage') es.addEventListener(k, (e) => backlogEvent(k, e));
  es.onopen = () => { document.body.dataset.live = 'sse'; stopPoll(); clearTimeout(live.retry); };
  es.onerror = () => {
    if (live.es !== es) return;
    closeSse();
    document.body.dataset.live = 'poll';
    startPoll();
    clearTimeout(live.retry);
    live.retry = setTimeout(openSse, SSE_RETRY_MS);
  };
}
async function reloadBoardAndLive() {
  await loadBoard();
  openSse();
}

setInterval(() => {
  for (const el of document.querySelectorAll('[data-ts]')) el.textContent = ago(el.dataset.ts);
}, 10000);

/* ── drawer ─────────────────────────────────────── */
const drawer = $('#drawer');
const TABS = ['Files', 'Runs', 'Timeline', 'Triage', 'Scores'];

function openDrawer(id) {
  const same = state.drawer && state.drawer.job_id === id;
  if (!same) state.drawer = { job_id: id, tab: 'Files', file: null, actionsSig: null, tabKey: null };
  if (!drawer.open) drawer.showModal();
  if (!same) $('#d-title').focus();
  updateDrawer(true);
}
function closeDrawer() {
  if (drawer.open) drawer.close();
}
drawer.addEventListener('close', () => {
  const d = state.drawer;
  state.drawer = null;
  $('#d-title').textContent = '';
  if (location.hash.startsWith('#/job/')) history.replaceState(null, '', location.pathname + location.search + '#/');
  const card = d && [...document.querySelectorAll('.card')].find((c) => c.dataset.id === findJobId(d.job_id));
  if (card) card.focus();
});
const findJobId = (jid) => findJob(jid)?.id;
drawer.addEventListener('click', (e) => { if (e.target === drawer) closeDrawer(); });
$('#d-close').addEventListener('click', closeDrawer);

function actionButtons(job) {
  const st = job.stage;
  const out = [];
  const note = h('textarea', { id: 'rework-note', 'aria-label': 'Rework note', placeholder: 'What should change?' });
  const msg = h('p', { class: 'status-line', role: 'status' });
  const send = async (action, extra, noteText) => {
    msg.className = 'status-line';
    msg.textContent = 'Sending…';
    try {
      await apiJson(jobPath(job, 'actions'), { json: { action, note: noteText ?? null, ...extra } });
      msg.textContent = `Sent: ${action}`;
      loadBoard();
    } catch (e) {
      msg.className = 'status-line err';
      msg.textContent = e.message;
    }
  };
  if (st === 'needs_human' && can('approver')) {
    const label = job.merge_policy === 'pr_manual' ? 'Approve and mark PR ready' : 'Approve and merge';
    const shown = job.head_sha || null;
    out.push(h('div', { class: 'row' },
      h('span', { class: 'mono muted head-sha', text: shown ? `Head ${shown.slice(0, 12)}` : 'Head unknown' }),
      h('button', { class: 'btn primary', type: 'button', text: label, on: { click: () => send('approve', { head_sha: shown }) } })));
    out.push(note);
    out.push(h('div', { class: 'row' },
      h('button', {
        class: 'btn', type: 'button', text: 'Send back for rework',
        on: { click: () => {
          const t = note.value.trim();
          if (!t) { msg.className = 'status-line err'; msg.textContent = 'Write a note for the rework.'; return; }
          send('rework', {}, t);
        } },
      }),
      h('button', { class: 'btn danger', type: 'button', text: 'Reject', on: { click: () => send('reject', {}, note.value.trim() || null) } }),
      job.retry_offered && h('button', { class: 'btn', type: 'button', text: 'Retry step', on: { click: () => send('retry') } })));
  }
  if (st === 'needs_human' && job.gate_reason === 'budget' && can('admin')) {
    const amount = h('input', { id: 'budget-amount', type: 'text', inputmode: 'decimal', 'aria-label': 'Amount (USD)', placeholder: 'USD' });
    out.push(h('div', { class: 'row' },
      h('span', { class: 'muted', text: `Budget $${job.budget_usd ?? '?'}, spent $${job.cost_usd ?? '?'}` }),
      amount,
      h('button', {
        class: 'btn primary', type: 'button', text: 'Add budget',
        on: { click: () => {
          const v = amount.value.trim();
          if (!/^[0-9]+([.][0-9]{1,6})?$/.test(v) || !(parseFloat(v) > 0)) { msg.className = 'status-line err'; msg.textContent = 'Enter an amount above zero.'; return; }
          send('add_budget', { amount_usd: v });
        } },
      })));
  }
  if (st === 'failed' && can('approver') && job.retry_offered) {
    out.push(h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', text: 'Retry job', on: { click: () => send('retry') } })));
  }
  if (st === 'delivered' && job.pr_url && can('approver')) {
    const fnote = h('textarea', { id: 'follow-up-note', 'aria-label': 'Follow-up note', placeholder: 'What should change on the pull request?' });
    out.push(fnote);
    out.push(h('div', { class: 'row' },
      h('button', {
        class: 'btn', type: 'button', text: 'Send back as a follow-up',
        on: { click: async () => {
          const t = fnote.value.trim();
          if (!t) { msg.className = 'status-line err'; msg.textContent = 'Write a note for the follow-up.'; return; }
          msg.className = 'status-line';
          msg.textContent = 'Sending…';
          try {
            const r = await apiJson(jobPath(job, 'follow-up'), { json: { note: t } });
            msg.textContent = `Queued ${r && r.ref ? r.ref : 'a follow-up'}`;
            loadBoard();
          } catch (e) {
            msg.className = 'status-line err';
            msg.textContent = e.message;
          }
        } },
      })));
  }
  if (can('submitter') && !TERMINAL.has(st)) {
    out.push(h('div', { class: 'row' },
      h('button', { class: 'btn danger', type: 'button', text: 'Cancel job', on: { click: () => send('cancel') } }),
      st === 'delivered' && h('span', { class: 'muted', text: 'Also closes the pull request.' })));
  }
  if (out.length) out.push(msg);
  return out;
}

function updateDrawer(force) {
  const d = state.drawer;
  if (!d) return;
  const job = findJob(d.job_id);
  const title = $('#d-title');
  if (!job) {
    $('#d-num').textContent = '';
    title.textContent = state.boardLoaded ? 'This job is not on the board' : 'Loading board…';
    for (const id of ['d-meta', 'd-actions', 'd-tabs', 'd-body']) $('#' + id).replaceChildren();
    $('#d-andon').hidden = true;
    $('#d-actions').hidden = true;
    d.actionsSig = d.tabKey = null;
    return;
  }
  const { num, slug } = splitId(job.id);
  $('#d-num').textContent = num;
  title.textContent = job.title || slug.replace(/-/g, ' ');
  $('#d-meta').replaceChildren(
    h('span', { text: job.app }),
    h('span', { text: job.branch }),
    h('span', { text: job.stage.replace('_', ' ') + (job.round ? ` · round ${job.round}` : '') }),
    job.decision && h('span', { text: `decision: ${job.decision}` }),
    job.pr_url && h('a', { href: job.pr_url, target: '_blank', rel: 'noopener noreferrer', text: 'pull request' }),
    h('span', { text: `${money(job.cost_usd)} of ${money(job.budget_usd)}` }),
    job.updated && h('span', { data: { ts: job.updated }, text: ago(job.updated) }));
  const andon = $('#d-andon');
  andon.hidden = !(job.column === 'needs_human');
  if (!andon.hidden) {
    andon.replaceChildren(h('strong', { text: job.stage === 'failed' ? 'Failed.' : 'Needs you.' }),
      ' ', job.gate_reason ? `Reason: ${job.gate_reason}` : 'Read the decision, then choose an action below.');
  }
  // the action panel is rebuilt only when what it offers changes, so a typed note survives refreshes
  const sig = [job.stage, job.head_sha, job.retry_offered, job.gate_reason, job.merge_policy, state.me?.role].join('|');
  if (force || d.actionsSig !== sig) {
    d.actionsSig = sig;
    const box = $('#d-actions');
    const kids = actionButtons(job);
    box.replaceChildren(...kids);
    box.hidden = kids.length === 0;
  }
  if (force || !$('#d-tabs').firstChild) {
    $('#d-tabs').replaceChildren(...TABS.map((t) => h('button', {
      class: 'tab', type: 'button', role: 'tab', id: `tab-${t}`, 'aria-selected': String(t === d.tab), text: t,
      on: { click: () => { d.tab = t; d.tabKey = null; updateDrawer(true); } },
    })));
  }
  for (const t of $('#d-tabs').children) t.setAttribute('aria-selected', String(t.textContent === d.tab));
  const key = `${job.job_id}|${d.tab}|${d.file}|${job.updated}`;
  if (force || d.tabKey !== key) {
    d.tabKey = key;
    loadTab(job, d);
  }
}

function defaultFile(files) {
  for (const f of ['decision.md', 'build.md', 'spec.md', 'feature.md']) if (files.includes(f)) return f;
  return files.find((f) => !isImage(f)) || null;
}
const isImage = (p) => /\.png$/i.test(p);

async function loadTab(job, d) {
  const body = $('#d-body');
  const mine = (d.loadSeq = (d.loadSeq || 0) + 1);
  const stale = () => state.drawer !== d || d.loadSeq !== mine;
  const fill = (...kids) => { if (!stale()) body.replaceChildren(...kids); };
  const fail = (e) => fill(h('p', { class: 'muted', text: `Couldn't load: ${e.message}` }));
  try {
    if (d.tab === 'Files') {
      const files = (job.files || []).filter(safePath);
      const docs = files.filter((f) => !isImage(f)), shots = files.filter(isImage);
      if (!d.file || !docs.includes(d.file)) d.file = defaultFile(docs);
      const nav = h('div', { class: 'd-files' }, docs.map((f) => h('button', {
        class: 'fbtn', type: 'button', text: f, 'aria-current': String(f === d.file),
        on: { click: () => { d.file = f; d.tabKey = null; updateDrawer(true); } },
      })));
      const gallery = shots.length ? h('div', { class: 'shots' }, shots.map((s) => h('img', { src: artifactUrl(job, s), alt: s, loading: 'lazy' }))) : null;
      const doc = h('div', {});
      fill(docs.length ? nav : null, gallery, docs.length || shots.length ? doc : h('p', { class: 'muted', text: 'No files yet.' }));
      if (d.file) {
        const text = await fetchText(artifactUrl(job, d.file));
        if (stale()) return;
        doc.replaceChildren(text === null ? h('p', { class: 'muted', text: 'Not written yet.' }) : renderFile(d.file, text));
      }
    } else if (d.tab === 'Runs') {
      const runs = await apiJson(jobPath(job, 'runs'));
      fill(runsTable(runs));
    } else if (d.tab === 'Timeline') {
      const events = await apiJson(jobPath(job, 'events'));
      fill(h('ol', { class: 'timeline' }, events.map((ev) => h('li', {},
        h('span', { class: 'muted', text: ago(ev.at) + ' ' }), h('span', { class: 'k', text: ev.kind }), ' ',
        h('span', { text: ev.data && Object.keys(ev.data).length ? JSON.stringify(ev.data) : '' })))));
    } else if (d.tab === 'Scores') {
      const out = await apiJson(jobPath(job, 'judges'));
      if (stale()) return;
      fill(scoresView(job, d, out.scores || []));
    } else if (d.tab === 'Triage') {
      const text = await fetchText(artifactUrl(job, 'triage.md'));
      if (stale()) return;
      const form = can('approver') ? correctionForm(job) : null;
      fill(text === null ? h('p', { class: 'muted', text: 'No triage yet.' }) : renderFile('triage.md', text), form);
    }
  } catch (e) {
    if (!stale()) fail(e);
  }
}
const safePath = (p) => typeof p === 'string' && p && !p.startsWith('/') && !p.split('/').some((s) => s === '' || s === '..');
async function fetchText(url) {
  const res = await api(url);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}
function renderFile(path, text) {
  if (/\.md$/i.test(path)) {
    const art = h('article', { class: 'md' });
    art.innerHTML = renderMarkdown(text, path === 'decision.md' || /review-[\w-]+\.md$/.test(path)); // escape-first renderer
    return art;
  }
  return h('pre', { class: 'log', text });
}
function runsTable(runs) {
  if (!runs.length) return h('p', { class: 'muted', text: 'No runs yet.' });
  const cols = ['Role', 'Round', 'Engine', 'Model (requested → used)', 'Mode', 'Tokens', 'Cost', 'Duration', 'Status'];
  return h('div', { class: 'table-wrap' }, h('table', { class: 'tbl runs' },
    h('thead', {}, h('tr', {}, cols.map((c) => h('th', { text: c })))),
    h('tbody', {}, runs.map((r) => h('tr', {},
      h('td', { text: r.role }), h('td', { text: String(r.round) }),
      h('td', { text: [r.engine, r.engine_version].filter(Boolean).join(' ') }),
      h('td', { text: `${r.requested_model || r.model_alias || '?'} → ${(r.used_models || []).join(', ') || '—'}` }),
      h('td', { text: r.selection_mode || '' }),
      h('td', { text: `${r.input_tokens ?? 0} / ${r.output_tokens ?? 0}` }),
      h('td', { text: money(r.cost_usd) }),
      h('td', { text: duration(r.started_at, r.finished_at) }),
      h('td', { text: r.status }))))));
}
function scoresView(job, d, rows) {
  if (!rows.length) return h('p', { class: 'muted', text: 'No judge scores yet.' });
  const msg = h('p', { class: 'status-line', role: 'status' });
  const save = async (scorer, value) => {
    if (!value) return;
    try {
      await apiJson(jobPath(job, `scores/${encodeURIComponent(scorer)}/label`), { method: 'PUT', json: { label: value } });
      d.tabKey = null;
      updateDrawer(true);
    } catch (err) { msg.className = 'status-line err'; msg.textContent = err.message; }
  };
  return h('div', { class: 'scores' }, rows.map((r) => {
    const sel = h('select', { 'aria-label': `Your label for ${r.scorer}`, disabled: !can('approver'),
      on: { change: (e) => save(r.scorer, e.target.value) } },
      h('option', { value: '', text: 'Choose' }),
      (r.labels || []).map((l) => h('option', { value: l, text: l, selected: l === r.your_label })));
    return h('div', { class: 'score-row', data: { scorer: r.scorer } },
      h('h3', { text: r.scorer }),
      h('span', { class: 'muted', text: r.judge_model }), ' ',
      h('span', { class: 'badge', text: r.calibration }),
      h('div', { class: 'field' }, h('label', { text: 'Your label ' }), sel),
      r.hidden ? h('p', { class: 'muted', text: 'judge result hidden until you label' })
        : h('p', {}, h('strong', { text: r.label }), r.reason ? ` ${r.reason}` : ''));
  }), msg);
}
function correctionForm(job) {
  const ta = h('textarea', { id: 'triage-correction', 'aria-label': 'Correction', placeholder: 'What did triage get wrong?' });
  const msg = h('p', { class: 'status-line', role: 'status' });
  return h('form', { class: 'form', on: { submit: async (e) => {
    e.preventDefault();
    if (!ta.value.trim()) return;
    try {
      await apiJson(jobPath(job, 'triage-correction'), { json: { correction: ta.value.trim() } });
      msg.className = 'status-line';
      msg.textContent = 'Correction sent';
    } catch (err) { msg.className = 'status-line err'; msg.textContent = err.message; }
  } } }, h('h3', { text: 'Correct this' }), ta, h('div', {}, h('button', { class: 'btn', type: 'submit', text: 'Send correction' })), msg);
}

/* ── pages ──────────────────────────────────────── */
function field(label, control, hint) {
  const id = control.id || (control.id = 'f-' + label.toLowerCase().replace(/[^a-z]+/g, '-'));
  return h('div', { class: 'field' }, h('label', { for: id, text: label }), control, hint && h('span', { class: 'hint', text: hint }));
}
function page(title, ...kids) { return h('section', { class: 'page' }, h('h2', { text: title }), ...kids); }

async function showNew() {
  const apps = state.board.apps?.length ? state.board.apps : (await apiJson('/api/apps').catch(() => [])).map((a) => a.slug);
  const sel = h('select', { id: 'new-app' }, h('option', { value: '', text: 'Choose an app' }), apps.map((a) => h('option', { value: a, text: a })));
  const feature = h('textarea', { id: 'new-feature' });
  const profile = h('input', { type: 'text', id: 'new-profile' });
  const budget = h('input', { type: 'number', id: 'new-budget', min: '0', step: '0.01' });
  const msg = h('p', { class: 'status-line', role: 'status' });
  const form = h('form', { class: 'form', on: { submit: async (e) => {
    e.preventDefault();
    if (!sel.value || !feature.value.trim()) { msg.className = 'status-line err'; msg.textContent = 'Choose an app and describe the feature.'; return; }
    const body = { app: sel.value, feature: feature.value, source: 'dashboard' };
    if (profile.value.trim()) body.profile = profile.value.trim();
    if (budget.value) body.budget_usd = budget.value;
    try {
      const out = await apiJson('/api/jobs', { json: body });
      msg.className = 'status-line';
      msg.textContent = `Created ${out.ref}`;
      await loadBoard();
      location.hash = `#/job/${encodeURIComponent(out.job_id)}`;
    } catch (err) { msg.className = 'status-line err'; msg.textContent = err.message; }
  } } },
  field('App', sel), field('Feature', feature), field('Profile (optional)', profile), field('Budget in dollars (optional)', budget),
  h('div', {}, h('button', { class: 'btn primary', type: 'submit', text: 'Create job' })), msg);
  $('#main').replaceChildren(page('New job', form));
}

const looksGithub = (r) => /^(https?:\/\/github\.com\/|git@github\.com:)/i.test(r) || /^[\w.-]+\/[\w.-]+$/.test(r);
function showRun() {
  const repo = h('input', { type: 'text', id: 'run-repo' });
  const text = h('textarea', { id: 'run-text' });
  const file = h('input', { type: 'file', id: 'run-file', accept: '.md,.txt,text/*' });
  const profile = h('input', { type: 'text', id: 'run-profile' });
  const budget = h('input', { type: 'number', id: 'run-budget', min: '0', step: '0.01' });
  const ci = h('input', { type: 'checkbox', id: 'run-ci' });
  const ciRow = h('div', { class: 'check', hidden: true }, ci, h('label', { for: 'run-ci', text: 'Registered with CI safe' }));
  const status = h('p', { class: 'status-line run-status', role: 'status' });
  const sync = () => { ciRow.hidden = !looksGithub(repo.value.trim()); };
  repo.addEventListener('input', sync);
  const token = state.token;
  const show = (cls, ...kids) => { status.className = 'status-line run-status' + cls; status.replaceChildren(...kids); };
  const poll = async (rid, tries) => {
    if (token !== state.token) return;
    try {
      const r = await apiJson(`/api/runs/${encodeURIComponent(rid)}`);
      if (r.status === 'started') {
        const ref = r.job_ref;
        return show('', 'Started: requirements job ', ref ? h('a', { href: `#/job/${encodeURIComponent(ref)}`, text: ref }) : '');
      }
      if (r.status === 'failed') return show(' err', r.detail || 'The run failed');
      if (r.pr_url || r.url) return show('', `Waiting for the definition PR to merge: ${r.pr_url || r.url}`);
      show('', 'Working…');
    } catch (e) { return show(' err', e.message); }
    if (tries < 600) setTimeout(() => poll(rid, tries + 1), 1000);
  };
  const form = h('form', { class: 'form', on: { submit: async (e) => {
    e.preventDefault();
    let requirement = text.value;
    const f = file.files && file.files[0];
    if (f) {
      if (f.size > MAX_REQUIREMENT_BYTES) return show(' err', 'The requirement file is over 200 KB.');
      requirement = await f.text();
    }
    if (!repo.value.trim() || !requirement.trim()) return show(' err', 'Give a repo and a requirement.');
    const body = { repo: repo.value.trim(), requirement, ci_safe: !ciRow.hidden && ci.checked };
    if (profile.value.trim()) body.profile = profile.value.trim();
    if (budget.value) body.budget_usd = budget.value;
    show('', 'Starting…');
    try {
      const out = await apiJson('/api/runs', { json: body });
      poll(out.request_id, 0);
    } catch (err) { show(' err', err.message); }
  } } },
  field('Repo', repo, 'A folder path, owner/name or a GitHub URL'),
  field('Requirement', text), field('Requirement file', file, 'Replaces the text box. Up to 200 KB.'),
  field('Profile (optional)', profile), field('Budget in dollars (optional)', budget), ciRow,
  h('div', {}, h('button', { class: 'btn primary', type: 'submit', text: 'Start run' })), status);
  $('#main').replaceChildren(page('New run', form));
}

async function showApps() {
  const apps = await apiJson('/api/apps');
  $('#main').replaceChildren(page('Apps', h('div', { class: 'table-wrap' }, h('table', { class: 'tbl' },
    h('thead', {}, h('tr', {}, ['App', 'Kind', 'Branch', 'Profile', 'Merge policy', 'Budget', 'Spent today'].map((c) => h('th', { text: c })))),
    h('tbody', {}, apps.map((a) => h('tr', {}, [a.slug, a.repo_kind, a.default_branch, a.profile, a.merge_policy || '', money(a.budget_usd), money(a.cost_today_usd)].map((c) => h('td', { text: c })))))))));
}

async function showModels() {
  const token = state.token;
  const status = h('p', { class: 'status-line models-status', role: 'status' });
  const holder = h('div', { class: 'table-wrap' });
  const render = async () => {
    const models = await apiJson('/api/models');
    holder.replaceChildren(h('table', { class: 'tbl' },
      h('thead', {}, h('tr', {}, ['Alias', 'Provider', 'Available', 'Tools', 'Price in / out per Mtok', 'Latency', 'Error'].map((c) => h('th', { text: c })))),
      h('tbody', {}, models.map((m) => h('tr', {}, [
        m.alias, m.provider, m.available ? 'yes' : 'no',
        m.supports_tools === null ? 'unknown' : m.supports_tools ? 'yes' : 'no',
        `${money(m.price_in_per_mtok)} / ${money(m.price_out_per_mtok)}`,
        m.latency_ms === null ? '' : `${m.latency_ms} ms`, m.error_class || '',
      ].map((c) => h('td', { text: c })))))));
  };
  const pollRefresh = async (rid) => {
    if (token !== state.token) return;
    try {
      const r = await apiJson(`/api/models/refresh/${encodeURIComponent(rid)}`);
      if (r.done) { status.textContent = 'Refresh done'; return render(); }
    } catch (e) { status.textContent = e.message; return; }
    setTimeout(() => pollRefresh(rid), 2000);
  };
  const btn = can('admin') ? h('button', { class: 'btn', type: 'button', text: 'Refresh', on: { click: async () => {
    try {
      const out = await apiJson('/api/models/refresh', { json: {} });
      status.className = 'status-line models-status';
      status.textContent = 'Refresh requested';
      setTimeout(() => pollRefresh(out.request_id), 2000);
    } catch (e) { status.className = 'status-line models-status err'; status.textContent = e.message; }
  } } }) : null;
  $('#main').replaceChildren(page('Models', btn, status, holder));
  await render();
}

async function showProfiles() {
  const [profiles, models] = await Promise.all([apiJson('/api/profiles'), apiJson('/api/models')]);
  const available = models.filter((m) => m.available).map((m) => m.alias);
  const cards = profiles.map((p) => {
    const msg = h('p', { class: 'status-line', role: 'status' });
    const selects = Object.entries(p.roles).map(([role, rm]) => {
      const sel = h('select', { 'aria-label': `${role} model`, disabled: !can('admin') },
        available.map((a) => h('option', { value: a, text: a })));
      sel.value = rm.model;
      return [role, rm, sel];
    });
    const save = can('admin') ? h('button', { class: 'btn primary', type: 'button', text: 'Save profile', on: { click: async () => {
      const roles = {};
      for (const [role, rm, sel] of selects) roles[role] = { ...rm, model: sel.value };
      const doc = { description: p.description, continuity: p.continuity, roles };
      try {
        await apiJson(`/api/profiles/${encodeURIComponent(p.name)}`, { method: 'PUT', json: { yaml: JSON.stringify(doc), message: `Update profile ${p.name}` } });
        msg.className = 'status-line';
        msg.textContent = 'Saved';
      } catch (e) { msg.className = 'status-line err'; msg.textContent = e.message; }
    } } }) : null;
    return h('div', { class: 'profile' },
      h('h3', { text: p.name }), h('p', { class: 'muted', text: p.description || '' }),
      p.unavailable?.length > 0 && h('p', { class: 'status-line err', text: `Unavailable models: ${p.unavailable.join(', ')}` }),
      h('table', { class: 'tbl' }, h('tbody', {}, selects.map(([role, rm, sel]) => h('tr', {},
        h('td', { text: role }), h('td', { class: 'mono', text: rm.engine }), h('td', {}, sel))))),
      h('div', {}, save), msg);
  });
  $('#main').replaceChildren(page('Profiles', ...cards));
}

async function showDefinition() {
  const d = await apiJson('/api/definition');
  const msg = h('p', { class: 'status-line', role: 'status' });
  const sync = can('admin') ? h('button', { class: 'btn', type: 'button', text: 'Sync now', on: { click: async () => {
    try {
      const out = await apiJson('/api/definition/sync', { json: {} });
      msg.className = 'status-line';
      msg.textContent = out.active_sha ? `Synced ${String(out.active_sha).slice(0, 12)}` : 'Sync requested';
    } catch (e) { msg.className = 'status-line err'; msg.textContent = e.message; }
  } } }) : null;
  const rej = d.last_rejected;
  $('#main').replaceChildren(page('Definition',
    h('p', {}, 'Active commit ', h('code', { class: 'active-sha', text: String(d.active_sha || '').slice(0, 12) })),
    d.source && h('p', { class: 'muted', text: `Source: ${d.source}` }),
    rej && h('div', { class: 'rejected' }, h('strong', { text: `Last rejected: ${String(rej.sha).slice(0, 12)}` }),
      h('ul', {}, (rej.errors || []).map((e) => h('li', { text: String(e) })))),
    sync, msg));
}

const pct = (v) => (v === null || v === undefined ? 'n/a' : `${(v * 100).toFixed(0)}%`);
function rateTable(title, rows, dimLabel) {
  return h('div', { class: 'metric-block' }, h('h3', { text: title }),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, h('th', { text: dimLabel }), h('th', { text: 'Runs' }), h('th', { text: 'Pass rate' }))),
      h('tbody', {}, rows.length ? rows.map((r) => h('tr', {}, h('td', { text: r.key }), h('td', { text: String(r.n) }), h('td', { text: pct(r.pass_rate) })))
        : h('tr', {}, h('td', { colspan: '3', class: 'muted', text: 'No counted scores yet' }))))));
}
function figure(label, value, note) {
  return h('div', { class: 'figure' }, h('span', { class: 'fig-label', text: label }), h('strong', { text: value }), note && h('span', { class: 'muted', text: note }));
}
async function showMetrics() {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  const app = q.get('app') || '';
  const days = q.get('days') || '30';
  const params = new URLSearchParams({ days });
  if (app) params.set('app', app);
  const m = await apiJson(`/api/metrics?${params}`);
  const go = (a, d) => { location.hash = `#/metrics?${new URLSearchParams({ ...(a ? { app: a } : {}), days: d })}`; };
  const apps = state.board.apps || [];
  const appSel = h('select', { id: 'metrics-app', on: { change: (e) => go(e.target.value, daySel.value) } },
    h('option', { value: '', text: 'All apps' }), apps.map((a) => h('option', { value: a, text: a, selected: a === app })));
  const daySel = h('select', { id: 'metrics-days', on: { change: (e) => go(appSel.value, e.target.value) } },
    ['7', '30', '90'].map((d) => h('option', { value: d, text: `Last ${d} days`, selected: d === days })));
  if (!m.available) {
    $('#main').replaceChildren(page('Metrics', h('p', { class: 'muted', text: m.message || 'Metrics are not available yet.' })));
    return;
  }
  const hist = m.rounds.histogram || [];
  const top = Math.max(1, ...hist.map((b) => b.jobs));
  const money6 = (v) => (v === null || v === undefined ? 'n/a' : '$' + Number(v).toFixed(2));
  $('#main').replaceChildren(page('Metrics',
    h('div', { class: 'filters' }, field('App', appSel), field('Window', daySel)),
    h('div', { class: 'figures' },
      figure('Jobs', String(m.jobs), `finished in ${m.window_days} days`),
      figure('Escalation rate', pct(m.escalation_rate), 'jobs that waited for a person'),
      figure('Cost per merged job', money6(m.cost_per_merged_usd), 'All spend divided by merged jobs'),
      figure('Fallback rate', pct(m.fallback_rate), 'runs on a different model'),
      figure('Mean rounds', m.rounds.mean === null ? 'n/a' : m.rounds.mean.toFixed(2))),
    h('div', { class: 'metric-block' }, h('h3', { text: 'Rounds per job' }),
      h('div', { class: 'hist' }, hist.length ? hist.map((b) => h('div', { class: 'bar', title: `${b.jobs} jobs` },
        h('span', { class: 'fill', style: `height:${Math.round((b.jobs / top) * 100)}%` }),
        h('span', { class: 'x', text: `${b.rounds}` }), h('span', { class: 'n', text: String(b.jobs) }))) : h('p', { class: 'muted', text: 'No finished jobs' }))),
    rateTable('Pass rate by role', m.pass_rate.role, 'Role'),
    rateTable('Pass rate by model', m.pass_rate.model, 'Model'),
    rateTable('Pass rate by engine', m.pass_rate.engine, 'Engine'),
    rateTable('Pass rate by profile', m.pass_rate.profile, 'Profile'),
    h('div', { class: 'metric-block' }, h('h3', { text: 'Judges' }),
      h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['Scorer', 'Judge model', 'Scores', 'Pass rate', 'Calibration'].map((c) => h('th', { text: c })))),
        h('tbody', {}, m.scorers.length ? m.scorers.map((r) => h('tr', { class: r.calibrated ? '' : 'uncal' },
          h('td', { text: r.scorer }), h('td', { text: r.judge_model }), h('td', { text: String(r.n) }), h('td', { text: pct(r.pass_rate) }),
          h('td', { text: r.calibrated ? `calibrated, kappa ${r.kappa === null ? 'n/a' : r.kappa.toFixed(2)}` : `uncalibrated, not counted (${r.labels} labels)` })))
          : h('tr', {}, h('td', { colspan: '5', class: 'muted', text: 'No judge scores yet' }))))))));
}

/* ── benchmarks ─────────────────────────────────── */
const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  for (const kid of kids.flat()) if (kid) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
function benchChart(points) {
  const W = 480, H = 260, L = 44, R = 20, T = 14, B = 38;
  const maxCost = Math.max(0.01, ...points.map((p) => Number(p.cost_usd)));
  const x = (c) => L + (Number(c) / maxCost) * (W - L - R - 60);
  const y = (q) => T + (1 - q) * (H - T - B);
  const svg = svgEl('svg', { class: 'bench-chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Cost per task against quality, one point per configuration' });
  svg.append(svgEl('line', { x1: L, y1: T, x2: L, y2: H - B, class: 'axis' }), svgEl('line', { x1: L, y1: H - B, x2: W - R, y2: H - B, class: 'axis' }));
  for (const q of [0, 0.5, 1]) svg.append(svgEl('text', { x: L - 6, y: y(q) + 4, class: 'tick', 'text-anchor': 'end' }, q.toFixed(1)));
  svg.append(svgEl('text', { x: L, y: H - 8, class: 'tick' }, '$0'),
    svgEl('text', { x: (L + W - R) / 2, y: H - 8, class: 'tick', 'text-anchor': 'middle' }, `cost per task (to $${maxCost.toFixed(2)})`),
    svgEl('text', { x: 12, y: T + 8, class: 'tick' }, 'quality'));
  for (const p of points) {
    const cx = x(p.cost_usd), cy = y(p.quality);
    svg.append(svgEl('circle', { cx, cy, r: 6, class: p.pareto ? 'pt pareto' : 'pt' }, svgEl('title', {}, `${p.config}: quality ${p.quality.toFixed(2)} at $${Number(p.cost_usd).toFixed(2)} per task`)),
      svgEl('text', { x: cx + 9, y: cy + 4, class: 'lbl' }, p.config));
  }
  return svg;
}
const f2 = (v) => (v === null || v === undefined ? '-' : Number(v).toFixed(2));
function configRow(c) {
  const iv = c.hidden_pass_interval;
  const hidden = c.hidden_pass_rate === null || !iv ? '-' : `${f2(c.hidden_pass_rate)} (n=${c.completed}, ${f2(iv[0])}–${f2(iv[1])})`;
  const unc = c.judge_pass_rate_uncalibrated;
  return h('tr', {},
    h('td', { text: c.name }), h('td', { text: hidden }),
    h('td', {}, f2(c.judge_pass_rate), unc !== null && unc !== undefined && h('span', { class: 'muted', text: ` ${f2(unc)} not counted` })),
    h('td', { text: c.mean_rounds === null ? '-' : Number(c.mean_rounds).toFixed(1) }),
    h('td', { text: c.mean_cost_usd === null ? '-' : money(c.mean_cost_usd) }),
    h('td', { text: c.median_duration_s === null ? '-' : `${Math.round(c.median_duration_s)}s` }),
    h('td', { text: String(c.not_run) }));
}
async function showBenchRun(id) {
  const r = await apiJson(`/api/bench/runs/${encodeURIComponent(id)}`);
  $('#main').replaceChildren(page(`Benchmark ${r.suite}`,
    h('p', { class: 'muted', text: `${r.status} · spent ${money(r.cost_usd)} of ${money(r.budget_usd)} · definition ${String(r.definition_sha).slice(0, 12)}` }),
    h('p', { class: 'recommended' }, h('strong', { text: `Recommended: ${r.recommended || 'none'}` }), h('span', { class: 'muted', text: ` ${r.recommendation_reason}` })),
    r.chart.length ? benchChart(r.chart) : h('p', { class: 'muted', text: 'No completed trials to chart yet.' }),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data bench-table' },
      h('thead', {}, h('tr', {}, ['Configuration', 'Hidden pass', 'Judge pass (not ranked)', 'Rounds', 'Cost per task', 'Time', 'Not run'].map((c) => h('th', { text: c })))),
      h('tbody', {}, r.configs.map(configRow)))),
    r.invalid_tasks.length > 0 && h('div', { class: 'metric-block' }, h('h3', { text: 'Invalid tasks' }),
      h('ul', {}, r.invalid_tasks.map((t) => h('li', { text: r.invalid_reasons?.[t] ? `${t}: ${r.invalid_reasons[t]}` : t })))),
    h('p', {}, h('a', { href: '#/benchmarks', text: 'All benchmarks' }))));
}
function benchStartForm(suites) {
  const sel = h('select', { id: 'bench-suite', 'aria-label': 'Suite' }, h('option', { value: '', text: 'Choose a suite' }), suites.map((su) => h('option', { value: su.name, text: su.name })));
  const reps = h('input', { id: 'bench-reps', type: 'number', min: '1', value: '1', 'aria-label': 'Repetitions' });
  const out = h('p', { class: 'status-line', id: 'bench-estimate' });
  const go = h('button', { class: 'btn primary', type: 'button', disabled: true, text: 'Start benchmark run' });
  const json = (body) => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const repetitions = () => Math.max(1, Number(reps.value) || 1);
  let est = null, seq = 0;
  const refresh = async () => {
    est = null; go.disabled = true; out.className = 'status-line';
    if (!sel.value) { out.textContent = ''; return; }
    const mine = ++seq;
    out.textContent = 'Estimating…';
    try {
      const e = await apiJson('/api/bench/estimate', json({ suite: sel.value, repetitions: repetitions() }));
      if (mine !== seq) return;
      est = e;
      out.textContent = `Estimated ${money(e.estimate_usd)} (up to ${money(e.max_usd)})` + (e.budget_usd ? ` · budget cap ${money(e.budget_usd)}` : ' · no budget cap') + ` · ${e.trials} trials`;
      go.disabled = false;
    } catch (err) { if (mine === seq) { out.className = 'status-line err'; out.textContent = err.message; } }
  };
  sel.addEventListener('change', refresh);
  reps.addEventListener('change', refresh);
  go.addEventListener('click', async () => {
    if (!est) return;
    go.disabled = true;
    try {
      const res = await apiJson('/api/bench/runs', json({ suite: sel.value, repetitions: repetitions(), accept_estimate_usd: est.estimate_usd }));
      location.hash = `#/benchmarks/${encodeURIComponent(res.run_id)}`;
    } catch (err) { await refresh(); out.className = 'status-line err'; out.textContent = err.message; }
  });
  return h('div', { class: 'metric-block bench-start' }, h('h3', { text: 'Start a run' }), h('div', { class: 'filters' }, field('Suite', sel), field('Repetitions', reps)), out, go);
}
async function showBenchmarks() {
  const id = decodeURIComponent((location.hash.slice(1).split('/').filter(Boolean)[1] || '').split('?')[0]);
  if (id) return showBenchRun(id);
  const [suites, runs] = await Promise.all([apiJson('/api/bench/suites'), apiJson('/api/bench/runs')]);
  const kids = [h('div', { class: 'metric-block' }, h('h3', { text: 'Suites' }),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['Suite', 'Tasks', 'Configurations', 'Budget'].map((c) => h('th', { text: c })))),
      h('tbody', {}, suites.length ? suites.map((su) => h('tr', {}, h('td', { text: su.name }), h('td', { text: String(su.tasks) }),
        h('td', { text: su.configurations.join(', ') }), h('td', { text: money(su.budget_usd) })))
        : h('tr', {}, h('td', { colspan: '4', class: 'muted', text: 'No benchmark suites yet' }))))))];
  if (can('admin') && suites.length) kids.push(benchStartForm(suites));
  kids.push(h('div', { class: 'metric-block' }, h('h3', { text: 'Runs' }),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['Run', 'Suite', 'Status', 'Spend'].map((c) => h('th', { text: c })))),
      h('tbody', {}, runs.length ? runs.map((r) => h('tr', {}, h('td', {}, h('a', { href: `#/benchmarks/${encodeURIComponent(r.id)}`, text: String(r.id).slice(0, 8) })),
        h('td', { text: r.suite }), h('td', { text: r.status }), h('td', { text: `${money(r.cost_usd)} of ${r.budget_usd ? money(r.budget_usd) : 'no cap'}` })))
        : h('tr', {}, h('td', { colspan: '4', class: 'muted', text: 'No benchmark runs yet' })))))));
  $('#main').replaceChildren(page('Benchmarks', ...kids));
}

/* ── backlogs ───────────────────────────────────── */
const backlogView = { ref: null, reload: null, jobRefs: new Set(), timer: null };
function backlogEvent(kind, e) {
  if (!backlogView.ref || state.view !== 'backlog') return;
  let d = {};
  try { d = JSON.parse(e.data) || {}; } catch { /* not JSON */ }
  let hit;
  if (kind === 'job.stage') {
    const known = findJob(d.job_id)?.ref;
    const refs = [d.ref, d.job_ref, known].filter(Boolean);
    hit = refs.length ? refs.some((r) => backlogView.jobRefs.has(r)) : true;
  } else hit = !d.ref || d.ref === backlogView.ref;
  if (!hit) return;
  clearTimeout(backlogView.timer);
  backlogView.timer = setTimeout(() => backlogView.reload && backlogView.reload(), 150);
}
async function showBacklogs() {
  const rows = await apiJson('/api/backlogs');
  $('#main').replaceChildren(page('Backlogs', rows.length === 0 ? h('p', { class: 'muted', text: 'No backlogs yet.' }) : h('div', { class: 'table-wrap' }, h('table', { class: 'tbl backlogs' },
    h('thead', {}, h('tr', {}, ['Title', 'Ref', 'Status', 'Progress', 'Requirements job', 'Updated'].map((c) => h('th', { text: c })))),
    h('tbody', {}, rows.map((b) => h('tr', {},
      h('td', {}, h('a', { href: `#/backlog/${encodeURIComponent(b.ref)}`, text: b.title })),
      h('td', { class: 'mono', text: b.ref }),
      h('td', { text: b.status }),
      h('td', { text: `${b.jobs_merged} of ${b.jobs_total} merged` }),
      h('td', { class: 'mono', text: b.requirements_job_ref || '' }),
      h('td', { text: ago(b.updated_at) }))))))));
}
async function showBacklog() {
  const ref = decodeURIComponent(location.hash.slice(1)).split('/').filter(Boolean)[1];
  const token = state.token;
  const msg = h('p', { class: 'status-line err', role: 'alert', hidden: true });
  const body = h('div', { class: 'backlog-body' });
  const wrap = page('Backlog', body);
  $('#main').replaceChildren(wrap);
  let note = '';
  const fail = (text) => { msg.hidden = false; msg.textContent = text; };
  const post = async (path, json) => {
    msg.hidden = true;
    try { await apiJson(path, { json }); await load(); } catch (e) { fail(e.message); }
  };
  async function load() {
    const b = await apiJson('/api/backlogs/' + encodeURIComponent(ref));
    if (token !== state.token) return;
    let req = null;
    if (b.status === 'awaiting_approval' && b.requirements_job) {
      try { req = await fetchText(`/api/jobs/${encodeURIComponent(b.requirements_job.ref)}/artifacts/requirements.md`); } catch { req = null; }
      if (token !== state.token) return;
    }
    backlogView.ref = ref;
    backlogView.jobRefs = new Set((b.items || []).map((i) => i.job_ref));
    backlogView.reload = () => load().catch((e) => fail(e.message));
    wrap.querySelector('h2').textContent = b.title;
    const kids = [h('p', { class: 'row' }, h('span', { class: 'mono', text: b.ref }), ' ', h('span', { class: 'backlog-status', text: b.status }), ' ',
      h('span', { class: 'muted', text: `${b.jobs_merged} of ${b.jobs_total} merged` }))];
    const act = (label, cls, fn) => h('button', { class: 'btn ' + cls, type: 'button', text: label, on: { click: fn } });
    const baPath = `/api/backlogs/${encodeURIComponent(b.ref)}/actions`;
    if (b.status === 'paused') {
      kids.push(h('p', { class: 'blocked' }, b.blocked_job ? `${b.blocked_job} is ${(b.items.find((i) => i.job_ref === b.blocked_job) || {}).stage || 'blocked'}` : 'Paused'));
    }
    if (b.status === 'awaiting_approval' && b.requirements_job) {
      kids.push(h('h3', { text: 'Requirements' }));
      kids.push(req === null ? h('p', { class: 'muted', text: 'No requirements document yet.' }) : h('pre', { class: 'log requirements-md', text: req }));
      if (can('approver')) {
        const jp = `/api/jobs/${encodeURIComponent(b.requirements_job.ref)}/actions`;
        const noteEl = h('textarea', { id: 'backlog-note', 'aria-label': 'Note', placeholder: 'What should change?', on: { input: (e) => { note = e.target.value; } } });
        noteEl.value = note;
        const hint = h('p', { class: 'status-line err', hidden: true });
        kids.push(h('div', { class: 'field' }, h('label', { for: 'backlog-note', text: 'Note' }), noteEl, hint));
        kids.push(h('div', { class: 'row actions' },
          act('Approve backlog', 'primary', () => post(jp, { action: 'approve', note: null, head_sha: null })),
          act('Send back for rework', '', () => {
            const t = noteEl.value.trim();
            if (!t) { hint.hidden = false; hint.textContent = 'Say what to change'; return; }
            hint.hidden = true;
            post(jp, { action: 'rework', note: t, head_sha: null });
          }),
          act('Reject backlog', 'danger', () => post(jp, { action: 'reject', note: noteEl.value.trim() || null, head_sha: null }))));
      }
    }
    if (can('approver') && (b.status === 'paused' || b.status === 'running')) {
      const cancel = act('Cancel backlog', 'danger', () => { if (confirm(`Cancel backlog ${b.ref}? Its queued jobs are cancelled.`)) post(baPath, { action: 'cancel' }); });
      kids.push(h('div', { class: 'row actions' }, b.status === 'paused' && [
        act('Resume', 'primary', () => post(baPath, { action: 'resume' })),
        act('Skip blocked job', '', () => post(baPath, { action: 'skip' })),
      ], cancel));
    }
    kids.push(msg);
    kids.push(h('ol', { class: 'backlog-items' }, (b.items || []).map((i) => h('li', {},
      h('span', { class: 'item-title', text: `${i.pos}  ${i.title}` }),
      h('span', { class: 'item-meta' }, h('a', { href: `#/job/${encodeURIComponent(i.job_ref)}`, text: i.job_ref }), ' ', h('span', { class: 'item-stage', text: i.stage }), ' ', h('span', { class: 'cost', text: money(i.cost_usd) }))))));
    body.replaceChildren(...kids);
  }
  await load();
}

/* ── routing ────────────────────────────────────── */
const VIEWS = { backlogs: showBacklogs, backlog: showBacklog, new: showNew, run: showRun, apps: showApps, models: showModels, profiles: showProfiles, definition: showDefinition, metrics: showMetrics, benchmarks: showBenchmarks };
async function route() {
  let hash = '';
  try { hash = decodeURIComponent(location.hash.slice(1)); } catch { /* malformed hash */ }
  state.token++;
  backlogView.ref = null;
  backlogView.reload = null;
  const parts = hash.split('/').filter(Boolean);
  const head = (parts[0] || '').split('?')[0];
  if (head === 'job' && parts[1]) {
    if (state.view !== 'board') { state.view = 'board'; showBoardView(); }
    renderChrome();
    openDrawer(parts.slice(1).join('/'));
    return;
  }
  closeDrawer();
  if (head && VIEWS[head]) {
    state.view = head;
    renderChrome();
    try { await VIEWS[head](); } catch (e) { if (!state.signedOut) $('#main').replaceChildren(page(head, h('p', { class: 'status-line err', text: e.message }))); }
    return;
  }
  if (state.view !== 'board') { state.view = 'board'; showBoardView(); }
  renderChrome();
}
window.addEventListener('hashchange', route);

/* ── markdown renderer: escapes everything first ── */
const RE = {
  fence: /^\s{0,3}(`{3,}|~{3,})(.*)$/,
  heading: /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/,
  hr: /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/,
  quote: /^\s{0,3}>\s?(.*)$/,
  list: /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/,
  tableSep: /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/,
};
const isTableStart = (l, next) => l.includes('|') && next !== undefined && next.includes('-') && RE.tableSep.test(next);
const startsBlock = (l, next) =>
  RE.fence.test(l) || RE.heading.test(l) || RE.hr.test(l) || RE.quote.test(l) || RE.list.test(l) || isTableStart(l, next);

function inline(s) {
  return String(s).split(/(`+[^`]*?`+)/).map((part, i) => {
    if (i % 2) return `<code>${esc(part.replace(/^`+|`+$/g, ''))}</code>`;
    return esc(part)
      // links: only http(s) (any case) and relative #/ targets become anchors; every other link stays text
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/gi, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\[([^\]]+)\]\((#\/[^\s)]*)\)/g, '<a href="$2">$1</a>')
      .replace(/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, '<strong>$1</strong>')
      .replace(/__(?=\S)(.+?)(?<=\S)__/g, '<strong>$1</strong>')
      .replace(/(^|[^\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\w)/g, '$1<em>$2</em>')
      .replace(/(^|[^\w])_(?=\S)(.+?)(?<=\S)_(?!\w)/g, '$1<em>$2</em>')
      .replace(/~~(.+?)~~/g, '<del>$1</del>');
  }).join('');
}
function itemHTML(lines) {
  while (lines.length > 1 && !lines[lines.length - 1].trim()) lines.pop();
  let first = lines[0], task = null;
  const t = first.match(/^\[([ xX])\]\s+(.*)$/);
  if (t) { task = t[1] !== ' '; first = t[2]; }
  const rest = lines.slice(1);
  const ind = Math.min(...rest.filter((s) => s.trim()).map(indentOf));
  const body = inline(first) + (rest.length ? blocks(rest.map((s) => s.slice(Math.min(indentOf(s), isFinite(ind) ? ind : 0)))) : '');
  const box = task === null ? '' : `<input type="checkbox" disabled${task ? ' checked' : ''}>`;
  return `<li${task === null ? '' : ' class="task"'}>${box}${body}</li>`;
}
function blocks(lines) {
  const out = [];
  let i = 0, m;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    if ((m = l.match(RE.fence))) {
      const mark = m[1], buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(mark)) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    if ((m = l.match(RE.heading))) { const n = Math.min(m[1].length + 2, 5); out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++; continue; }
    if (RE.hr.test(l)) { out.push('<hr>'); i++; continue; }
    if (RE.quote.test(l)) {
      const buf = [];
      while (i < lines.length && RE.quote.test(lines[i])) buf.push(lines[i++].match(RE.quote)[1]);
      out.push(`<blockquote>${blocks(buf)}</blockquote>`);
      continue;
    }
    if (isTableStart(l, lines[i + 1])) {
      const row = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = row(l), body = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) body.push(row(lines[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    if ((m = l.match(RE.list))) {
      const base = m[1].length, ordered = /\d/.test(m[2]), start = ordered ? parseInt(m[2], 10) : 1, items = [];
      while (i < lines.length) {
        const cur = lines[i], mm = cur.match(RE.list);
        if (mm && mm[1].length <= base + 1) { items.push([mm[3]]); i++; continue; }
        if (!cur.trim()) {
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j++;
          const nx = lines[j];
          if (nx !== undefined && (indentOf(nx) > base + 1 || (RE.list.test(nx) && indentOf(nx) <= base + 1))) { items[items.length - 1].push(''); i++; continue; }
          break;
        }
        if (indentOf(cur) > base && items.length) { items[items.length - 1].push(cur); i++; continue; }
        break;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}${ordered && start !== 1 ? ` start="${start}"` : ''}>${items.map(itemHTML).join('')}</${tag}>`);
      continue;
    }
    const buf = [l.trim()];
    i++;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i], lines[i + 1])) buf.push(lines[i++].trim());
    out.push(`<p>${inline(buf.join(' '))}</p>`);
  }
  return out.join('');
}
function renderMarkdown(src, stampable) {
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  let stamp = '';
  const k = lines.findIndex((l) => l.trim());
  if (stampable && k >= 0) {
    const first = lines[k].trim().replace(/^[#>*_\s]+|[*_\s]+$/g, '');
    const m = first.match(/^VERDICT:\s*(PASS|CHANGES)\b/i) || first.match(/^(APPROVE|ESCALATE)\b/);
    if (m) { const w = m[1].toUpperCase(); stamp = `<div class="stamp ${w.toLowerCase()}">${w}</div>`; lines.splice(k, 1); }
  }
  return stamp + blocks(lines);
}

/* ── boot ───────────────────────────────────────── */
async function boot() {
  try { state.me = await apiJson('/api/me'); } catch { /* a 401 shows the signed-out notice */ }
  renderChrome();
  await route();
  await loadBoard();
  openSse();
}
boot();
