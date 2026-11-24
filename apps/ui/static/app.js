/* EQDS Research UI. Plain JS, no build step. Talks only to /api/* on the same origin (apps/ui/server.py). */
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const PALETTE = ['#0033FF', '#8A5D24', '#1E7B4F', '#071D49', '#B3261E', '#5B3FA6', '#007C89', '#9A3F76', '#4F6D00'];
const STAGES = [['who_opens', 'Who opens'], ['opening_round', 'Opening round'], ['map', 'Map the debate'], ['deep_dive', 'Dig into disagreements'], ['verdicts', 'Verdicts'], ['answer', 'Answer']];

let ME = { developer: false };
let CFG = {};
let SPECS = [];
let HOLDINGS = [];
let S = null;            // current run state
let ES = null;           // current EventSource
let SPEC_MODE = false;
const openMsgs = new Set();

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) throw new Error((await r.text()).slice(0, 300) || r.statusText);
  return r.json();
}
function toast(m, ms = 3500) { const t = $('toast'); t.textContent = m; t.style.display = 'block'; clearTimeout(toast._t); toast._t = setTimeout(() => (t.style.display = 'none'), ms); }
const colorOf = (key) => { const i = Math.max(0, SPECS.findIndex((s) => s.key === key)); return key === 'moderator' ? '#040F25' : PALETTE[i % PALETTE.length]; };
const nameOf = (key) => (key === 'moderator' ? 'Moderator' : (SPECS.find((s) => s.key === key) || {}).display_name || key);
const initials = (key) => (key === 'moderator' ? 'MO' : nameOf(key).split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase());
const av = (key) => `<span class="av" style="background:${colorOf(key)}">${esc(initials(key))}</span>`;

/* ---------------- workspace links ---------------- */
function setting(k) { return new URLSearchParams(location.search).get(k) || localStorage.getItem('eqds.' + k) || CFG[k] || ''; }
function wsUrl() { return (setting('ws') || '').replace(/\/$/, ''); }
function appName(n) { return String(n).replace(/^eqds/, CFG.namePrefix || 'eqds') + (CFG.appSuffix || ''); }
function linkFor(kind, ref = '') {
  const ws = wsUrl(); if (!ws) return null;
  const cat = setting('catalog'), sch = setting('schema');
  switch (kind) {
    case 'app': return `${ws}/apps/${encodeURIComponent(appName(ref))}`;
    case 'table': case 'uc_function': return `${ws}/explore/data/${cat}/${sch}/${ref.split('.').pop()}`;
    case 'schema': return `${ws}/explore/data/${cat}/${sch}`;
    case 'catalog': return `${ws}/explore/data/${cat}`;
    case 'foreign': return `${ws}/explore/data/${CFG.foreignCatalog}/${ref}`;
    case 'connection': return `${ws}/explore/connections/${CFG.connection}`;
    case 'lineage': return `${ws}/explore/data/${cat}/${sch}/${ref}?activeTab=lineage`;
    case 'endpoints': return `${ws}/ml/endpoints`;
    case 'endpoint': return `${ws}/ml/endpoints/${ref}`;
    case 'lakebase': return `${ws}/lakebase/projects`;
    case 'experiment': { const id = ref === 'moderator' ? setting('modExp') : setting('exp'); return id ? `${ws}/ml/experiments/${id}/traces` : null; }
    case 'genie': return setting('genie') ? `${ws}/genie/rooms/${setting('genie')}` : null;
    case 'github': return setting('github') || null;
    default: return null;
  }
}
function linkFromSpec(spec) {
  const [kind, a, b] = spec.split(':');
  if (kind === 'job') { const id = CFG[a]; return id ? `${wsUrl()}/jobs/${id}` : null; }
  if (kind === 'app') return linkFor('app', a);
  return linkFor(kind, [a, b].filter(Boolean).join(':'));
}
function applyLinks() {
  document.querySelectorAll('[data-link]').forEach((el) => {
    const u = linkFromSpec(el.dataset.link);
    if (u) { el.href = u; el.target = '_blank'; el.rel = 'noopener'; el.classList.remove('needs-config'); } else { el.removeAttribute('href'); el.classList.add('needs-config'); el.title = 'Set the workspace in ⚙'; }
  });
}
const SET_FIELDS = [['ws', 'Workspace URL'], ['catalog', 'Catalog'], ['schema', 'Schema'], ['exp', 'MLflow experiment id'], ['genie', 'Genie space id'], ['github', 'GitHub repo URL']];
function openSettings() { $('form').innerHTML = SET_FIELDS.map(([k, l]) => `<label>${l}</label><input id="set-${k}" value="${esc(setting(k))}">`).join(''); $('settings').classList.add('on'); }
function closeSettings() { $('settings').classList.remove('on'); }
function saveSettings() { SET_FIELDS.forEach(([k]) => { const v = $('set-' + k).value.trim(); v ? localStorage.setItem('eqds.' + k, v) : localStorage.removeItem('eqds.' + k); }); closeSettings(); applyLinks(); toast('Saved'); }
function toggleSpecMode() { SPEC_MODE = !SPEC_MODE; document.body.classList.toggle('spec', SPEC_MODE); $('specBtn').classList.toggle('on', SPEC_MODE); }

/* ---------------- navigation ---------------- */
function showView(v) { document.querySelectorAll('.view').forEach((e) => e.classList.toggle('active', e.id === 'view-' + v)); document.querySelectorAll('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === v)); if (v === 'wiki') loadWiki(); if (v === 'hood') showSub(currentSub); }
let currentSub = 'arch';
function showSub(s) {
  currentSub = s;
  document.querySelectorAll('.sub').forEach((e) => (e.style.display = e.id === 'sub-' + s ? '' : 'none'));
  document.querySelectorAll('#subnav button').forEach((b) => b.classList.toggle('on', b.dataset.sub === s));
  const loaders = { arch: renderArch, trace: loadTrace, models: loadModels, data: loadSeed, reg: loadRegistry, qa: () => { loadEval(); loadCI(); }, caps: renderCaps };
  (loaders[s] || (() => {}))();
}
function guardDev(el) { if (!ME.developer) { document.querySelectorAll('#subnav button').forEach((b) => { if (!['arch', 'caps'].includes(b.dataset.sub)) b.style.display = 'none'; }); } }

/* ---------------- specialists / chips / history ---------------- */
async function loadSpecs() { try { SPECS = (await api('/api/specialists')).filter((s) => s.enabled); } catch (e) { SPECS = []; toast('Registry unavailable: ' + e.message); } renderChips(); renderArch(); }
function renderChips() {
  const auto = $('autoSel').checked;
  $('chips').innerHTML = SPECS.map((s) => `<span class="chip ${auto ? 'auto' : ''} ${s._on ? 'on' : ''}" data-k="${s.key}" title="${esc(s.description)}">${av(s.key)}${esc(s.display_name)}</span>`).join('') || '<span class="muted small">No specialists registered.</span>';
  $('chips').querySelectorAll('.chip').forEach((c) => (c.onclick = () => { if ($('autoSel').checked) { $('autoSel').checked = false; } const s = SPECS.find((x) => x.key === c.dataset.k); s._on = !s._on; renderChips(); }));
}
async function loadHistory() {
  try {
    const h = await api('/api/history');
    $('hist').innerHTML = h.length ? '<div class="section-t" style="margin:0 0 6px">Your recent questions</div>' + h.map((r) => `<a data-r="${r.run_id}">${esc(r.question)} <span class="muted tiny">· ${esc(r.status)} · ${new Date(r.when).toLocaleString()}</span></a>`).join('') : '';
    $('hist').querySelectorAll('a').forEach((a) => (a.onclick = () => openRun(a.dataset.r)));
  } catch (e) { /* history is optional */ }
}

/* ---------------- run lifecycle ---------------- */
function newState(runId, conv, question) { return { runId, conv, question, stages: {}, who: null, messages: [], points: [], verdicts: {}, answer: null, next: [], usage: null, status: 'RUNNING', sources: {}, activity: [], error: null, banners: [], t0: Date.now(), traceId: null, fb: null }; }
async function ask(q) {
  const question = (typeof q === 'string' ? q : $('q').value).trim();
  if (question.length < 3) { toast('Type a question first'); return; }
  $('askBtn').disabled = true;
  const body = { question, rounds: +$('rounds').value, conversation_id: S && S.conv ? S.conv : undefined };
  if (!$('autoSel').checked) { const sel = SPECS.filter((s) => s._on).map((s) => s.key); if (!sel.length) { toast('Pick at least one specialist'); $('askBtn').disabled = false; return; } body.specialists = sel; }
  try {
    const r = await api('/api/ask', { method: 'POST', body });
    S = newState(r.run_id, r.conversation_id, question);
    $('evlog').innerHTML = ''; lightOff(); renderRun(); stream(r.run_id); history.replaceState(null, '', '?run=' + r.run_id);
  } catch (e) { toast('Could not start the run: ' + e.message); }
  $('askBtn').disabled = false;
}
function stream(runId) {
  if (ES) ES.close();
  ES = new EventSource(`/api/runs/${runId}/stream`);
  ['stage', 'who_opens', 'message', 'map', 'verdict', 'answer', 'ask_next', 'usage', 'activity', 'status', 'done', 'error', 'deep_dive'].forEach((t) => ES.addEventListener(t, (e) => { try { onEvent(t, JSON.parse(e.data)); } catch (x) { console.warn(x); } }));
  ES.onerror = () => { if (S && S.status !== 'RUNNING') ES.close(); };
}
let rafPending = false;
function renderSoon() { if (rafPending) return; rafPending = true; requestAnimationFrame(() => { rafPending = false; renderRun(); }); }
function onEvent(t, d) {
  if (!S) return;
  switch (t) {
    case 'stage': S.stages[d.stage] = d.status; break;
    case 'who_opens': S.who = d; break;
    case 'message': S.messages.push(d); break;
    case 'map': S.points = d.points; break;
    case 'verdict': S.verdicts[d.point_id] = d; break;
    case 'answer': S.answer = d; break;
    case 'ask_next': S.next = d.questions; break;
    case 'usage': S.usage = d; break;
    case 'activity': addActivity(d); break;
    case 'status': S.status = d.status; if (d.status === 'RESUMED') S.banners.push({ k: 'ok', m: 'The run was interrupted and resumed from its last checkpoint.' }); break;
    case 'error': S.error = d; break;
    case 'done': S.traceId = d.trace_id; S.status = 'DONE'; ES && ES.close(); finishRun(); break;
  }
  renderSoon();
}
async function finishRun() {
  try { const r = await api('/api/runs/' + S.runId); if (r.result) { S.sources = r.result.sources || {}; S.result = r.result; S.traceId = r.trace_id; } } catch (e) { /* ignore */ }
  renderRun(); loadHistory(); lightOff();
}
async function openRun(id) {
  try {
    const r = await api('/api/runs/' + id);
    S = newState(id, r.conversation_id, r.question); S.status = r.status; S.traceId = r.trace_id;
    $('q').value = r.question;
    if (r.result) { loadResult(r.result); } else { $('evlog').innerHTML = ''; if (r.status === 'RUNNING') stream(id); }
    renderRun(); window.scrollTo({ top: $('runArea').offsetTop - 90, behavior: 'smooth' });
  } catch (e) { toast('Could not load the run: ' + e.message); }
}
function loadResult(res) {
  S.result = res; S.sources = res.sources || {};
  STAGES.forEach(([k]) => (S.stages[k] = 'done'));
  S.messages = (res.messages || []).map((m) => ({ ...m, first_line: (m.text || '').split('\n')[0], tool_calls: [] }));
  S.points = (res.points || []).map((p) => ({ id: p.id, topic: p.topic, claim: p.claim, verdict: p.verdict, status: p.status, stances: p.stances }));
  (res.points || []).forEach((p) => (S.verdicts[p.id] = { point_id: p.id, text: p.text, verdict: p.verdict, status: p.status }));
  S.answer = { stance: res.answer.stance, paragraphs: res.answer.paragraphs, validation: res.answer.validation };
  S.next = res.ask_next || []; S.usage = { ...res.usage, cost_usd: res.usage.cost_usd, duration_s: res.usage.duration_s }; S.status = 'DONE';
}

/* ---------------- rendering the run ---------------- */
const tagify = (txt) => esc(txt).replace(/\[([A-Z]{1,3}\d+)\]/g, (m, id) => `<span class="tag" data-tag="${id}">${id}</span>`);
const STANCE_ICON = { support: '✓', oppose: '✕', partial: '≈', not_addressed: '–', gap: '?' };
function renderRun() {
  if (!S) { $('runArea').innerHTML = ''; return; }
  const u = S.usage;
  const stages = `<div class="stages">${STAGES.map(([k, l]) => `<span class="stage ${S.stages[k] || ''}">${l}</span>`).join('')}</div>`;
  const meta = `<div class="meta">${S.who ? `<b>${S.who.selected.length}</b> specialists · ` : ''}${u ? `<b>${u.duration_s}s</b> · <b>$${(+u.cost_usd).toFixed(2)}</b> · ${(u.tokens_in || 0) + (u.tokens_out || 0)} tokens` : ''} ${S.status === 'RUNNING' ? '<span class="spinner"></span>' : ''}</div>`;
  const banners = [];
  const degraded = S.messages.filter((m) => m.from !== 'moderator' && m.claims && m.claims.length && m.claims.every((c) => c.stance === 'not_addressed') && /did not respond|unavailable|degrad/i.test(m.text || ''));
  if (degraded.length) banners.push({ k: 'warn', m: `${degraded.map((m) => nameOf(m.from)).join(', ')} did not respond. The answer is built from the other specialists and says so.` });
  if (S.answer && S.answer.validation && !S.answer.validation.passed) banners.push({ k: 'warn', m: `Citation check: ${Math.round((S.answer.validation.citation_coverage || 0) * 100)}% of statements carry a source tag. Treat uncited statements with care.` });
  if (S.error) banners.push({ k: 'err', m: `${S.error.message || 'The run failed'} ${S.error.retryable ? '(it can be resumed)' : ''}` });
  S.banners.forEach((b) => banners.push(b));
  const who = S.who ? `<div class="section-t">Who opens</div><div class="small" style="margin-bottom:8px"><b>Reformulated:</b> ${esc(S.who.reformulated)}</div><div class="chips">${S.who.selected.map((k) => `<span class="chip on">${av(k)}${esc(nameOf(k))}</span>`).join('')}</div>` : '';
  const msgs = S.messages.length ? `<div class="section-t">The debate</div>${S.messages.map(renderMsg).join('')}` : '';
  const pts = S.points.length ? `<div class="section-t">Where they agree and disagree</div>${S.points.map(renderPoint).join('')}` : '';
  const ans = S.answer ? `<div class="answer" data-spec="§5.2.5 citation validator (NFR-12)"><div class="kicker">Answer</div><p class="stance">${tagify(S.answer.stance || '')}</p>${S.answer.paragraphs.map((p) => `<p>${tagify(p)}</p>`).join('')}
      <div class="row small" style="margin-top:8px"><a href="#" id="srcOpen">View sources (${Object.keys(S.sources).length || '…'})</a></div>
      ${S.status === 'DONE' ? `<div class="feedback">Was this useful? <button data-fb="up" class="${S.fb === 'up' ? 'sel' : ''}">👍</button><button data-fb="down" class="${S.fb === 'down' ? 'sel' : ''}">👎</button></div>` : ''}</div>` : '';
  const next = S.next.length ? `<div class="section-t">Ask next</div><div class="asknext">${S.next.map((q) => `<button data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>` : '';
  const dev = ME.developer && S.status === 'RUNNING' ? `<div class="row" style="margin-top:12px"><button class="btn ghost sm" id="crashBtn">Simulate replica crash</button><button class="btn ghost sm" id="resumeBtn">Resume now</button></div>` : '';
  $('runArea').innerHTML = `<div class="card runhead" style="margin-top:24px"><div class="kicker">Question</div><h2>${esc(S.question)}</h2>${meta}${stages}${banners.map((b) => `<div class="banner ${b.k}">${esc(b.m)}</div>`).join('')}${dev}${who}${msgs}${pts}${ans}${next}</div>`;
  wireRun();
}
function renderMsg(m, i) {
  const id = `m${m.round}-${m.from}-${i}`;
  const calls = (m.tool_calls || []).map((t) => `${esc(t.tool)}${t.template ? '@' + esc(t.template) : ''} (${(t.latency_ms / 1000).toFixed(1)}s)`).join(' · ');
  const claims = (m.claims || []).map((c) => `<div>${STANCE_ICON[c.stance] || ''} ${tagify(c.text)} ${(c.citations || []).map((t) => `<span class="tag" data-tag="${t}">${t}</span>`).join('')}</div>`).join('');
  return `<div class="msg">${av(m.from)}<div><div class="who">${esc(nameOf(m.from))} <span class="to">→ ${esc((m.to || []).map(nameOf).join(', '))} · round ${m.round}${m.cached ? ' · cached' : ''}</span></div>
    <details data-id="${id}" ${openMsgs.has(id) ? 'open' : ''}><summary>${tagify(m.first_line || '')}</summary><div class="body">${m.from === 'moderator' ? esc(m.text) : claims || tagify(m.text)}</div>${calls ? `<div class="toolline">Tools: ${calls}</div>` : ''}</details></div></div>`;
}
function renderPoint(p) {
  const v = S.verdicts[p.id] || p;
  return `<div class="point ${v.verdict}"><span class="badge b-${v.verdict}">${esc(v.verdict.replace('_', ' '))}</span><span class="badge b-${v.status}">${esc(v.status)}</span><span class="claim">${tagify(p.claim)}</span>
    <div class="stances">${Object.entries(p.stances || {}).map(([k, s]) => `<span title="${esc(nameOf(k))}: ${s}">${av(k)}${STANCE_ICON[s] || s}</span>`).join('')}</div>${v.text ? `<div class="verdict">${tagify(v.text)}</div>` : ''}</div>`;
}
function wireRun() {
  $('runArea').querySelectorAll('details[data-id]').forEach((d) => (d.ontoggle = () => (d.open ? openMsgs.add(d.dataset.id) : openMsgs.delete(d.dataset.id))));
  $('runArea').querySelectorAll('[data-tag]').forEach((t) => (t.onclick = () => openDrawer(t.dataset.tag)));
  $('runArea').querySelectorAll('[data-q]').forEach((b) => (b.onclick = () => { $('q').value = b.dataset.q; ask(b.dataset.q); }));
  $('runArea').querySelectorAll('[data-fb]').forEach((b) => (b.onclick = () => sendFeedback(b.dataset.fb)));
  const so = $('srcOpen'); if (so) so.onclick = (e) => { e.preventDefault(); openDrawer(); };
  const cb = $('crashBtn'); if (cb) cb.onclick = () => api(`/api/runs/${S.runId}/crash`, { method: 'POST' }).then(() => toast('Crash requested. The reaper resumes the run within about a minute, or press Resume now.')).catch((e) => toast(e.message));
  const rb = $('resumeBtn'); if (rb) rb.onclick = () => api(`/api/runs/${S.runId}/resume`, { method: 'POST' }).then(() => { toast('Resuming'); if (!ES || ES.readyState === 2) stream(S.runId); }).catch((e) => toast(e.message));
}
async function sendFeedback(v) {
  try { await api('/api/feedback', { method: 'POST', body: { run_id: S.runId, trace_id: S.traceId, value: v } }); S.fb = v; renderRun(); toast('Thanks. Feedback is logged on the trace.'); } catch (e) { toast('Feedback failed: ' + e.message); }
}
/* sources drawer */
function openDrawer(tag) {
  const ids = Object.keys(S.sources || {});
  $('srcCount').textContent = ids.length + ' sources';
  $('srcList').innerHTML = ids.map((id) => { const s = S.sources[id]; return `<div class="src ${id === tag ? 'hl' : ''}" id="src-${id}"><span class="id">${id}</span><span class="ty">${esc(s.type)}${s.system ? ' · ' + esc(s.system) : ''}</span><div><b>${esc(s.title || s.ref || '')}</b></div>${s.snippet ? `<div class="muted">${esc(s.snippet)}</div>` : ''}<div class="tiny muted">${esc(s.ref || '')}${s.template ? ' · template ' + esc(s.template) : ''}${s.as_of ? ' · as of ' + esc(s.as_of) : ''}</div></div>`; }).join('') || '<div class="muted small">No sources.</div>';
  $('drawer').classList.add('open');
  if (tag) { const el = $('src-' + tag); if (el) el.scrollIntoView({ block: 'center' }); }
}
function closeDrawer() { $('drawer').classList.remove('open'); }

/* ---------------- live activity ---------------- */
const COMP_MAP = { 'eqds-moderator': 'moderator', 'vector-search': 'mcp-vs', 'unity-catalog': 'mcp-uc', 'genie': 'mcp-genie' };
function compFor(c, zone) {
  if (c.startsWith('eqds-sp-')) return 'sp-' + c.slice(8);
  if (COMP_MAP[c]) return COMP_MAP[c];
  return zone === 'model' ? 'gateway' : zone === 'state' ? 'lakebase' : null;
}
function lightOff() { document.querySelectorAll('.comp.lit').forEach((e) => e.classList.remove('lit')); }
function addActivity(d) {
  const t = Math.round((Date.now() - S.t0) / 1000);
  const url = d.link ? linkFor(d.link.kind, d.link.ref) : null;
  const zone = { serverless: 'serverless', classic: 'classic', external: 'external', model: 'model', state: 'state' }[d.zone] || 'serverless';
  const el = document.createElement('div'); el.className = 'ev';
  el.innerHTML = `<span class="t">${t}s</span><div><span class="c c-${zone}">${zone.toUpperCase()}</span> <b>${esc(d.component)}</b><div>${url ? `<a class="ext" href="${url}" target="_blank" rel="noopener">${esc(d.label)}</a>` : esc(d.label)}</div><div class="muted">${esc(d.detail || '')}</div></div>`;
  const log = $('evlog'); log.prepend(el);
  const c = compFor(d.component, d.zone); if (c) { document.querySelectorAll(`[data-comp="${c}"]`).forEach((e) => { e.classList.add('lit'); setTimeout(() => e.classList.remove('lit'), 4000); }); }
  if (d.zone === 'external') ['worker', 'foreign', 'azure'].forEach((k) => document.querySelectorAll(`[data-comp="${k}"]`).forEach((e) => { e.classList.add('lit'); setTimeout(() => e.classList.remove('lit'), 4000); }));
}

/* ---------------- Company Wiki ---------------- */
let WIKI = [], sectorSel = 'All';
async function loadWiki() {
  try { WIKI = await api('/api/wiki?top=' + $('topn').value); } catch (e) { $('wikiBody').innerHTML = `<div class="banner err">Wiki unavailable: ${esc(e.message)}</div>`; return; }
  renderWiki(); loadQueue();
}
function renderWiki() {
  const qs = $('wsearch').value.toLowerCase(), top = +$('topn').value;
  const rows = WIKI.filter((c) => (!qs || (c.name + ' ' + (c.ticker || '')).toLowerCase().includes(qs)) && (c.pages || (c.holding_rank && c.holding_rank <= top)));
  const sectors = ['All', ...[...new Set(rows.map((c) => c.sector))].sort()];
  if (!sectors.includes(sectorSel)) sectorSel = 'All';
  $('sectors').innerHTML = sectors.map((s) => `<button class="${s === sectorSel ? 'on' : ''}" data-s="${esc(s)}">${esc(s)}${s === 'All' ? ` (${rows.length})` : ` (${rows.filter((c) => c.sector === s).length})`}</button>`).join('');
  $('sectors').querySelectorAll('button').forEach((b) => (b.onclick = () => { sectorSel = b.dataset.s; renderWiki(); }));
  const shown = rows.filter((c) => sectorSel === 'All' || c.sector === sectorSel);
  const withWiki = WIKI.filter((c) => c.pages).length;
  $('wikiCount').textContent = `${withWiki} wikis · ${WIKI.filter((c) => !c.pages && c.holding_rank && c.holding_rank <= top).length} of the top ${top} holdings without one`;
  $('wikiBody').innerHTML = `<div class="wcards">${shown.map((c) => `<div class="wcard ${c.pages ? 'has' : ''}" data-c="${c.company_id}"><h4>${c.holding_rank ? `<span class="rank">#${c.holding_rank}</span>` : ''}${esc(c.name)}</h4><div class="muted small">${esc(c.ticker || '')} · ${esc(c.sector)}</div>
    ${c.pages ? `<div class="small" style="margin-top:6px">${c.pages} pages · v${esc(c.version || '1')} · built ${new Date(c.built_at).toLocaleDateString()}</div>${dueText(c.pending_triggers) ? `<div class="due">Refresh due: ${esc(dueText(c.pending_triggers))}</div>` : ''}` : `<div class="small muted" style="margin-top:6px">No wiki yet.</div><button class="btn ghost sm" style="margin-top:8px" data-req="${c.company_id}" data-name="${esc(c.name)}">Request wiki</button>`}</div>`).join('')}</div>`;
  $('wikiBody').querySelectorAll('.wcard.has').forEach((el) => (el.onclick = () => openWiki(el.dataset.c)));
  $('wikiBody').querySelectorAll('[data-req]').forEach((b) => (b.onclick = (e) => { e.stopPropagation(); requestWiki(b.dataset.req, b.dataset.name); }));
}
const DUE_LABEL = { earnings: 'results reported (+2 days)', broker_6_reports: '6+ new broker reports', broker_3_changes: '3+ rating or target changes', broker_3_in_2_days: '3 reports within 2 days', stale30: '30 days with new material', full_rebuild_120d: 'full rebuild (120 days)' };
function dueText(p) { try { const a = JSON.parse(p || '[]'); return a.map((t) => DUE_LABEL[t] || t).join(', '); } catch (e) { return ''; } }
async function requestWiki(id, name) { try { await api('/api/wiki/request', { method: 'POST', body: { company_id: id, company_name: name } }); toast('Wiki request queued'); loadQueue(); } catch (e) { toast(e.message); } }
function requestWikiPrompt() { const id = prompt('Company id (for example C0042)'); if (id && /^C\d{4}$/.test(id)) requestWiki(id, ''); else if (id) toast('Company ids look like C0042'); }
async function loadQueue() {
  try {
    const q = await api('/api/wiki-queue'); const active = q.filter((r) => r.status === 'QUEUED' || r.status === 'BUILDING');
    $('queue').innerHTML = active.length ? active.map((r) => `${esc(r.company_name || r.company_id)} <span class="muted">· ${esc(r.status)} · ${new Date(r.created_at).toLocaleTimeString()}</span>`).join('<br>') : 'Nothing is building.';
  } catch (e) { /* optional */ }
}
function md(s) {
  return esc(s).replace(/^### (.*)$/gm, '<h4>$1</h4>').replace(/^## (.*)$/gm, '<h3>$1</h3>').replace(/^# (.*)$/gm, '<h3>$1</h3>').replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/^(?:- |\* )(.*)$/gm, '<li>$1</li>').replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`).replace(/\[([A-Z]{1,3}\d+)\]/g, '<span class="tag">$1</span>').replace(/\n{2,}/g, '<br><br>');
}
async function openWiki(id) {
  const c = WIKI.find((x) => x.company_id === id);
  $('wmTitle').textContent = c ? c.name : id; $('wmTabs').innerHTML = ''; $('wmBody').innerHTML = '<span class="spinner"></span>'; $('wikiModal').classList.add('on');
  try {
    const pages = await api('/api/wiki/' + id);
    const show = (i) => { $('wmTabs').querySelectorAll('button').forEach((b, j) => b.classList.toggle('on', i === j)); $('wmBody').innerHTML = md(pages[i].content_md); };
    $('wmTabs').innerHTML = pages.map((p, i) => `<button data-i="${i}">${esc(p.page)}</button>`).join('');
    $('wmTabs').querySelectorAll('button').forEach((b) => (b.onclick = () => show(+b.dataset.i)));
    if (pages.length) show(0); else $('wmBody').textContent = 'No pages yet.';
  } catch (e) { $('wmBody').innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}

/* ---------------- Under the hood ---------------- */
function renderArch() {
  $('archSpecs').innerHTML = SPECS.map((s) => `<a class="comp" data-comp="sp-${s.key}" data-link="app:eqds-sp-${s.key}"><b>${esc(s.display_name)}</b>${esc(s.tools || '')}</a>`).join('');
  applyLinks();
}
const fmtJson = (o) => JSON.stringify(o, null, 2);
let INV = [];
async function loadTrace() {
  if (!S) return;
  $('traceId').textContent = S.traceId || '';
  try {
    const tr = await api(`/api/dev/runs/${S.runId}/trace`);
    $('spans').innerHTML = tr.flatMap((t) => t.spans).map((s) => `<div class="sp" style="margin-left:${s.level * 18}px"><span>${esc(s.name)} <span class="muted tiny">${esc(String(s.type).replace('SpanType.', ''))}</span></span><span class="d">${s.ms} ms</span></div>`).join('') || '<div class="muted small">No spans found yet (traces are written when a run finishes).</div>';
    $('traceTags').textContent = tr[0] ? fmtJson(tr[0].tags) : '—';
  } catch (e) { $('spans').innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
  try { INV = await api(`/api/dev/runs/${S.runId}/invocations`); $('invSel').innerHTML = INV.map((x, i) => `<option value="${i}">${esc(nameOf(x.key))} · round ${x.round}</option>`).join(''); renderInv(); } catch (e) { /* ignore */ }
  try {
    const m = await api(`/api/dev/runs/${S.runId}/memory`);
    $('memConv').innerHTML = m.turns.map((t) => `<div><b>Turn ${t.turn}</b> ${esc(t.question)}<div class="muted">${esc(t.answer_summary || '')}</div></div>`).join('') || 'No turns yet.';
    $('memSpec').innerHTML = m.specialist.map((r) => `<div><b>${esc(r.specialist)}</b> <span class="muted tiny">${esc(r.idempotency_key)}</span><div class="muted">${esc(r.brief)}</div></div>`).join('') || 'No interactions yet.';
    $('skills').innerHTML = m.skills.map((k) => `<div><code>${esc(k.path)}</code></div>`).join('');
  } catch (e) { /* ignore */ }
}
function renderInv() { const x = INV[+$('invSel').value]; if (!x) return; $('invReq').textContent = fmtJson(x.req); $('invRes').textContent = fmtJson(x.res); }

async function netServerless() {
  $('netSOut').innerHTML = '<p><span class="spinner"></span> Running on a serverless SQL warehouse…</p>';
  try {
    const r = await api('/api/dev/net/serverless-probe', { method: 'POST' });
    $('netSOut').innerHTML = r.ok ? `<div class="banner ok">Query succeeded in ${(r.ms / 1000).toFixed(1)} s on ${esc(r.warehouse)}${r.serverless ? ' (serverless)' : ''}. Specialists query the external database directly from serverless: the firewall admits serverless here, and in GIC once the whitelist is in place (see docs/validation-demo.md).</div>${tbl(r.rows)}` : `<div class="banner err">Blocked as expected after ${(r.ms / 1000).toFixed(1)} s.<br>${esc(r.error)}</div>`;
  } catch (e) { $('netSOut').innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}
function tbl(rows) {
  if (!rows || !rows.length) return '';
  const cols = Object.keys(rows[0]);
  return `<table class="t" style="margin-top:8px"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${esc(r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

async function loadModels() {
  try {
    const m = await api('/api/dev/models');
    $('tierBody').innerHTML = m.tiers.map((t) => `<tr><td>${esc(t.tier)}</td><td>${esc(t.primary)}<div class="muted tiny">log: ${esc(t.inference_table || '—')}</div></td><td>${esc(t.state)} · ${esc((t.destinations || []).join(', ') || t.error || '')}</td><td>${esc([...(t.gateway_fallback || []), t.fallback].filter(Boolean).join(' → ') || '—')}</td></tr>`).join('') + `<tr><td>embed</td><td>${esc(m.embed.endpoint)}<div class="muted tiny">Vector Search embedding endpoint</div></td><td>${esc(m.embed.state)}</td><td>—</td></tr>`;
    $('tierYaml').textContent = m.yaml;
    $('capSnap').innerHTML = m.tiers.map((t) => `${esc(t.tier)}: ${esc(t.primary)} is ${esc(t.state)}`).join('<br>');
  } catch (e) { $('tierBody').innerHTML = `<tr><td colspan="4">${esc(e.message)}</td></tr>`; }
}
async function inject429() {
  $('modelLog').textContent = 'Injecting 429 on the standard tier primary and asking for a reply…';
  try {
    const r = await api('/api/dev/models/fault-test', { method: 'POST' });
    $('modelLog').innerHTML = [`<span class="w">injected: ${esc(r.injected || '')}</span>`, ...(r.log || []).map((l) => esc(l)), r.ok ? `<span class="o">answered by ${esc(r.answered_by)} in ${(r.ms / 1000).toFixed(1)} s${r.fell_back ? ' (fell back)' : ''}</span>` : `<span class="e">${esc(r.error)}</span>`].join('\n');
  } catch (e) { $('modelLog').innerHTML = `<span class="e">${esc(e.message)}</span>`; }
}

const PLACEMENT = [['Companies, holdings, prices, EFM, quant, earnings calendar', 'Unity Catalog Delta', 'rowcount:ref_companies', 'UC grants'], ['Broker, expert, earnings, news chunks', 'Vector Search indexes', 'rowcount:broker_chunks', 'Index CAN_QUERY'], ['Trades, positions, accounts', 'Azure SQL (oms)', 'rowcount:oms.trades', 'Bridge only'], ['Analyst views, notes', 'Azure SQL (research)', 'rowcount:research.notes', 'Bridge only'], ['News wire, web index', 'Azure SQL (feeds), news landed to UC', 'rowcount:feeds.news_wire', 'Bridge + news_items']];
async function loadSeed() {
  try {
    const r = await api('/api/dev/seed/validation');
    const by = Object.fromEntries(r.checks.map((c) => [c.check_id, c]));
    $('dataBody').innerHTML = PLACEMENT.map((p) => `<tr><td>${p[0]}</td><td>${p[1]}</td><td>${by[p[2]] ? esc(by[p[2]].actual) : '—'}</td><td>${p[3]}</td></tr>`).join('');
    const checks = r.checks.filter((c) => !c.check_id.startsWith('rowcount:'));
    $('checks').innerHTML = checks.map((c) => `<div class="check ${c.passed ? 'ok' : ''}"><span class="st">${c.passed ? '✓' : '✕'}</span><span>${esc(c.check_id)}</span><span>${esc(c.name)}<div class="muted tiny">${esc(c.detail || '')}</div></span><span class="muted tiny">${esc(c.actual)}</span></div>`).join('') || '<div class="muted small">Validation has not run yet.</div>';
  } catch (e) { $('checks').innerHTML = esc(e.message); }
}
async function runValidate() { $('valMsg').textContent = 'Starting…'; try { await api('/api/dev/seed/validate', { method: 'POST' }); $('valMsg').textContent = 'Job started. Refresh in a minute.'; } catch (e) { $('valMsg').textContent = e.message; } }

async function loadRegistry() {
  try {
    const r = await api('/api/dev/registry');
    $('regBody').innerHTML = r.map((s) => `<tr${s.enabled ? '' : ' style="opacity:.6"'}><td>${esc(s.key)}</td><td>${esc(s.display_name)}</td><td>${esc(s.tag_prefix)}</td><td>${esc(s.tools || '')}</td><td>${esc(s.app || '')}</td><td>${esc(s.version)}</td><td>${esc(s.owner)}</td><td><span class="chip ${s.enabled ? 'on' : ''}">${s.enabled ? 'Enabled · visible' : 'Disabled · hidden'}</span></td><td><button class="btn ghost sm" data-act="${s.enabled ? 'disable' : 'enable'}" data-k="${s.key}">${s.enabled ? 'Disable' : 'Enable'}</button></td></tr>`).join('');
    $('regBody').querySelectorAll('button').forEach((b) => (b.onclick = async () => { const o = await api('/api/dev/registry', { method: 'POST', body: { action: b.dataset.act, key: b.dataset.k } }); toast(o.applied ? (b.dataset.act === 'disable' ? 'Disabled. Hidden from the moderator and the specialist list now.' : 'Enabled. Available to the moderator now.') : (o.note || 'Updated.')); loadRegistry(); loadSpecs(); }));
  } catch (e) { $('regBody').innerHTML = `<tr><td colspan="9">${esc(e.message)}</td></tr>`; }
}
async function submitRegister() {
  const key = $('regKey').value.trim();
  if (!key) { toast('Enter the agent key'); return; }
  $('regSubmit').disabled = true; $('regMsg').textContent = `Starting registration of ${key}…`;
  try { const r = await api('/api/dev/registry/submit', { method: 'POST', body: { key } }); pollRegister(r.run_id, key); }
  catch (e) { $('regSubmit').disabled = false; $('regMsg').textContent = e.message; }
}
async function pollRegister(runId, key) {
  let s;
  try { s = await api(`/api/dev/registry/runs/${runId}`); }
  catch (e) { $('regSubmit').disabled = false; $('regMsg').textContent = e.message; return; }
  if (!['TERMINATED', 'INTERNAL_ERROR', 'SKIPPED'].includes(s.life)) { $('regMsg').textContent = `Registering ${key}… (${s.life})`; setTimeout(() => pollRegister(runId, key), 4000); return; }
  $('regSubmit').disabled = false;
  if (s.result === 'SUCCESS') {
    $('regMsg').textContent = `${key} is registered. Making one live call to confirm it answers…`; $('regSubmit').disabled = true;
    try { await api('/api/dev/registry/activate', { method: 'POST', body: { key } }); $('regMsg').textContent = `${key} is registered and enabled. The moderator uses it now.`; toast(`${key} registered`); }
    catch (e) { $('regMsg').innerHTML = `<b>${esc(key)} is registered but stays disabled:</b> ${esc(e.message)}`; }
    $('regSubmit').disabled = false; loadRegistry(); loadSpecs();
  }
  else { $('regMsg').innerHTML = `<b>Registration failed.</b> ${esc(s.message || '')}${s.logs ? `<pre class="tiny" style="white-space:pre-wrap;max-height:160px;overflow:auto">${esc(s.logs.slice(-700))}</pre>` : ''}`; }
}

async function loadEval() {
  try {
    const r = await api('/api/dev/eval/latest');
    if (!r.run) { $('evalBars').innerHTML = '<div class="muted small">No evaluation run recorded yet.</div>'; return; }
    $('evalNote').textContent = `Latest run ${r.name || r.run} · ${r.started} · ${r.n || '?'} questions`;
    $('evalBars').innerHTML = Object.entries(r.metrics).map(([k, v]) => { const th = (r.thresholds || {})[k]; const bad = th !== undefined && v < th; return `<div style="margin:8px 0"><div class="row small"><b>${esc(k)}</b><span style="margin-left:auto">${v.toFixed(2)}${th !== undefined ? ` / ${th}` : ''}</span></div><div class="bar ${bad ? 'fail' : ''}"><i style="width:${Math.min(100, v * 100)}%"></i></div></div>`; }).join('');
  } catch (e) { $('evalBars').innerHTML = esc(e.message); }
}
async function runEval() { $('evalBtn').disabled = true; $('evalMsg').textContent = 'Starting…'; try { await api('/api/dev/eval/run', { method: 'POST' }); $('evalMsg').textContent = 'Eval job started. Refresh in a few minutes.'; } catch (e) { $('evalMsg').textContent = e.message; } $('evalBtn').disabled = false; }
async function loadCI() {
  try {
    const r = await api('/api/dev/ci');
    if (!r.configured) { $('pipe').innerHTML = '<div class="muted small">Set github_repo_url in the environment profile to show pipeline runs.</div>'; return; }
    $('pipe').innerHTML = (r.runs || []).map((x) => `<a class="st ${x.conclusion === 'success' ? 'ok' : x.status !== 'completed' ? 'run' : ''}" href="${x.url}" target="_blank" rel="noopener"><b>${esc(x.name)}</b>${esc(x.title)}<div class="s">${esc(x.conclusion || x.status)} · ${esc(x.branch)}</div></a>`).join('') || esc(r.error || 'No runs.');
  } catch (e) { $('pipe').innerHTML = esc(e.message); }
}
function renderCaps() {
  const cols = [['Available and used', '#1E7B4F', ['Databricks Apps (UI, moderator, specialists)', 'Lakebase Autoscaling (state, checkpoints, queue)', 'Managed MCP: Vector Search + Unity Catalog functions', 'MLflow 3 tracing, feedback and evaluation', 'Lakehouse Federation to Azure SQL (classic compute)', 'Unity AI Gateway model services + inference tables']],
    ['To confirm in the target workspace', '#8A5D24', ['Provisioned throughput quota for the embedding endpoint', 'Genie space provisioning permissions', 'Serverless access to the foreign catalog (firewall)']],
    ['Not available or deferred', '#B3261E', ['Private connectivity to Azure SQL', 'Apollo integration (later phase)', 'Cross-cloud p95 under 6 s (measured 6 to 10 s)']]];
  $('caps').innerHTML = cols.map(([t, c, items]) => `<div class="capcol"><h4 style="background:${c}">${t}</h4><ul>${items.map((i) => `<li>${i}</li>`).join('')}</ul></div>`).join('');
}

/* ---------------- boot ---------------- */
async function boot() {
  document.querySelectorAll('#nav button').forEach((b) => (b.onclick = () => showView(b.dataset.view)));
  document.querySelectorAll('#subnav button').forEach((b) => (b.onclick = () => showSub(b.dataset.sub)));
  try { ME = await api('/api/me'); CFG = await api('/api/config/links'); } catch (e) { toast('Backend unreachable: ' + e.message); }
  if (ME.app_name) { document.querySelector('.appname').textContent = ME.app_name; document.title = ME.app_name + ' · Multi-agent research platform (synthetic data)'; }
  if (ME.subtitle) document.querySelector('.brand .sub').textContent = ME.subtitle;
  guardDev(); applyLinks();
  if (!ME.developer) document.querySelector('#nav [data-view="hood"]').style.display = 'none';
  $('evlog').innerHTML = '<div class="small muted">No run yet. Ask a question.</div>';
  await loadSpecs(); loadHistory();
  const rid = new URLSearchParams(location.search).get('run'); if (rid) openRun(rid);
  $('q').addEventListener('keydown', (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') ask(); });
}
Object.assign(window, { ask, renderChips, closeDrawer, openSettings, closeSettings, saveSettings, toggleSpecMode, renderWiki, requestWikiPrompt, renderInv, netServerless, inject429, runValidate, submitRegister, runEval, loadCI });
boot();
