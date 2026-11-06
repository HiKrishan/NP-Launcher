/* NP Launcher frontend */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  settings: null,
  dataDir: '',
  instances: [],
  versions: [],
  versionsFiltered: [],
  discoverType: 'mod',
  lastLogs: [],
  logStick: true,
};

/* ─────────────────────────── helpers ─────────────────────────── */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    const msg = (data && data.error) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function toast(msg, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = msg;
  $('#toastStack').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function fmtDownloads(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function timeAgo(epochSec) {
  if (!epochSec) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function initials(name) {
  const parts = String(name).trim().split(/[\s_-]+/).filter(Boolean);
  if (!parts.length) return 'NP';
  return (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase();
}

function skinUrl(username, size) {
  const u = encodeURIComponent(username || 'Player');
  return size ? `https://mc-heads.net/body/${u}/${size}` : `https://mc-heads.net/body/${u}`;
}

function avatarUrl(seed, size = 52) {
  return `https://mc-heads.net/avatar/${encodeURIComponent(seed || 'Player')}/${size}`;
}

/* ─────────────────────────── navigation ─────────────────────────── */

function switchView(name) {
  $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === name));
  if (name === 'instances') loadInstances();
  if (name === 'discover') renderDiscoverChrome();
  if (name === 'settings') loadSettingsView();
}

$$('.nav-item').forEach(btn => btn.addEventListener('click', () => switchView(btn.dataset.view)));

/* ─────────────────────────── profile / skin ─────────────────────────── */

async function refreshProfile() {
  const username = ($('#profileUsername').value || 'Player').trim() || 'Player';
  $('#skinBig').src = skinUrl(username, 180);
  $('#skinMini').src = skinUrl(username, 90);
  $('#footUsername').textContent = username;
  try {
    const { uuid } = await api('/api/uuid?name=' + encodeURIComponent(username));
    $('#profileUuid').value = uuid;
  } catch {
    $('#profileUuid').value = 'unavailable (offline)';
  }
}

let skinDebounce;
$('#profileUsername').addEventListener('input', () => {
  clearTimeout(skinDebounce);
  skinDebounce = setTimeout(refreshProfile, 450);
});

$('#saveProfileBtn').addEventListener('click', async () => {
  const username = ($('#profileUsername').value || 'Player').trim() || 'Player';
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ offline_username: username }) });
    toast(`Profile saved. Sleep well, ${username}.`);
    refreshProfile();
    loadSettings();
  } catch (e) { toast(e.message, true); }
});

/* ─────────────────────────── news ─────────────────────────── */

async function loadNews() {
  const grid = $('#newsGrid');
  try {
    const { entries, disabled } = await api('/api/news');
    if (disabled) { grid.innerHTML = '<div class="news-empty">News feed disabled in Settings.</div>'; return; }
    if (!entries.length) { grid.innerHTML = '<div class="news-empty">Couldn’t reach minecraft.net — check your connection.</div>'; return; }
    grid.innerHTML = entries.map(n => `
      <a class="news-card" href="${esc(n.link)}" target="_blank" rel="noopener">
        <div class="news-thumb" ${n.image ? `style="background-image:url('${esc(n.image)}')"` : ''}></div>
        <div class="news-body">
          <span class="news-tag">${esc(n.tag || 'News')}</span>
          <span class="news-title">${esc(n.title)}</span>
          <span class="news-date">${esc(fmtDate(n.date))}</span>
        </div>
      </a>`).join('');
  } catch (e) {
    grid.innerHTML = `<div class="news-empty">News failed to load: ${esc(e.message)}</div>`;
  }
}

/* ─────────────────────────── instances ─────────────────────────── */

function instanceCard(inst) {
  const loader = (inst.loader || 'vanilla').toLowerCase();
  const lastPlayed = inst.last_played ? timeAgo(inst.last_played) : 'never played';
  const modsCount = (inst.mods || []).length;
  return `
  <article class="instance-card" data-id="${esc(inst.id)}">
    <div class="instance-top">
      <div class="instance-avatar">${esc(initials(inst.name))}</div>
      <div>
        <div class="instance-name">${esc(inst.name)}</div>
        <div class="instance-badges">
          <span class="badge">${esc(inst.mc_version || inst.version_id)}</span>
          <span class="badge badge-loader-${esc(loader)}">${esc(loader)}</span>
        </div>
      </div>
    </div>
    <div class="instance-meta">
      <span>Last played: ${esc(lastPlayed)}</span>
      ${loader !== 'vanilla' ? `<span class="instance-mods">${modsCount} mod${modsCount === 1 ? '' : 's'}</span>` : ''}
    </div>
    <div class="instance-actions">
      <button class="btn btn-primary play-btn" data-id="${esc(inst.id)}">
        <svg viewBox="0 0 24 24" class="btn-icon"><path d="M8 5.5v13l11-6.5-11-6.5Z" fill="currentColor"/></svg>Play
      </button>
      <button class="btn btn-ghost edit-btn" data-id="${esc(inst.id)}">Edit</button>
    </div>
  </article>`;
}

async function loadInstances() {
  try {
    const { instances } = await api('/api/instances');
    state.instances = instances;
    const grid = $('#instanceGrid');
    grid.innerHTML = instances.map(instanceCard).join('');
    $('#instancesEmpty').classList.toggle('hidden', instances.length !== 0);
    renderLastPlayed();
    renderDiscoverChrome();
  } catch (e) { toast(e.message, true); }
}

function renderLastPlayed() {
  const card = $('#lastPlayedCard');
  const hint = $('#lastPlayedHint');
  const withPlays = state.instances.filter(i => i.last_played).sort((a, b) => b.last_played - a.last_played);
  const inst = withPlays[0] || state.instances[0];
  if (!inst) {
    card.innerHTML = '<div class="lp-empty">No instances yet. Go make one — it takes ten seconds.</div>';
    hint.textContent = '';
    return;
  }
  hint.textContent = inst.last_played ? timeAgo(inst.last_played) : 'not played yet';
  card.innerHTML = `
    <div class="lp-card">
      <div class="lp-avatar">${esc(initials(inst.name))}</div>
      <div>
        <div class="lp-name">${esc(inst.name)}</div>
        <div class="lp-sub">${esc(inst.mc_version || inst.version_id)} · ${esc(inst.loader || 'vanilla')} · ${inst.max_mb || 4096} MB</div>
      </div>
    </div>`;
}

$('#instanceGrid').addEventListener('click', async ev => {
  const playBtn = ev.target.closest('.play-btn');
  if (playBtn) return playInstance(playBtn.dataset.id);
  const editBtn = ev.target.closest('.edit-btn');
  if (editBtn) return openEditInstance(editBtn.dataset.id);
});

async function playInstance(id) {
  try {
    const { started } = await api('/api/play', { method: 'POST', body: JSON.stringify({ instance_id: id }) });
    if (started) toast('Launching… watch the console drawer.');
    openConsole();
  } catch (e) { toast(e.message, true); }
}

$('#homePlayBtn').addEventListener('click', () => {
  const id = (state.settings && state.settings.last_instance) ||
             (state.instances.find(i => i.last_played) || state.instances[0] || {}).id;
  if (!id) { toast('No instances yet — create one first.', true); switchView('instances'); return; }
  playInstance(id);
});

/* ─────────────── new instance modal ─────────────── */

const newInstanceModal = $('#newInstanceModal');

function openNewInstance() {
  newInstanceModal.classList.remove('hidden');
  if (!state.versions.length) loadVersionOptions();
}
function closeNewInstance() { newInstanceModal.classList.add('hidden'); }

$('#newInstanceBtn').addEventListener('click', openNewInstance);
$('#emptyNewInstanceBtn').addEventListener('click', openNewInstance);
$('#closeNewInstance').addEventListener('click', closeNewInstance);
$('#cancelNewInstance').addEventListener('click', closeNewInstance);
newInstanceModal.addEventListener('click', e => { if (e.target === newInstanceModal) closeNewInstance(); });

async function loadVersionOptions() {
  try {
    const { versions } = await api('/api/versions');
    state.versions = versions;
    applyVersionFilter();
  } catch (e) { toast('Could not load version list: ' + e.message, true); }
}

function applyVersionFilter() {
  const q = ($('#niVersionFilter').value || '').trim().toLowerCase();
  const sel = $('#niVersion');
  const rows = state.versions
    .filter(v => !q || v.id.toLowerCase().includes(q))
    .slice(0, 400);
  const typeRank = { release: 0, snapshot: 1, old_beta: 2, old_alpha: 3 };
  rows.sort((a, b) => (typeRank[a.type] ?? 9) - (typeRank[b.type] ?? 9) || (b.releaseTime || '').localeCompare(a.releaseTime || ''));
  sel.innerHTML = rows.map(v => `<option value="${esc(v.id)}" data-type="${esc(v.type)}">${esc(v.id)} — ${esc(v.type)}</option>`).join('');
  if (rows.length && !sel.value) sel.selectedIndex = 0;
}
$('#niVersionFilter').addEventListener('input', applyVersionFilter);
$('#refreshManifestBtn').addEventListener('click', async () => {
  try {
    await api('/api/manifest/refresh', { method: 'POST' });
    state.versions = [];
    await loadVersionOptions();
    toast('Version list refreshed.');
  } catch (e) { toast(e.message, true); }
});

$('#niLoader').addEventListener('change', refreshLoaderVersions);
$('#niVersion').addEventListener('change', refreshLoaderVersions);

async function refreshLoaderVersions() {
  const loader = $('#niLoader').value;
  const mc = $('#niVersion').value;
  const wrap = $('#niLoaderVersionWrap');
  const list = $('#niLoaderVersionList');
  const input = $('#niLoaderVersion');
  list.innerHTML = '';
  if (loader === 'vanilla' || !mc) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  input.value = '';
  input.placeholder = 'loading…';
  try {
    let versions = [];
    if (loader === 'fabric') {
      const data = await api('/api/fabric/loaders');
      versions = (data.loaders || []).map(l => l.version);
    } else if (loader === 'forge') {
      const data = await api('/api/forge/versions?mc=' + encodeURIComponent(mc));
      versions = data.versions || [];
    } else if (loader === 'neoforge') {
      const data = await api('/api/neoforge/versions');
      versions = data.versions || [];
    }
    list.innerHTML = versions.slice(0, 60).map(v => `<option value="${esc(v)}"></option>`).join('');
    input.placeholder = versions[0] ? `e.g. ${versions[0]}` : 'no versions found';
    if (versions.length && !input.value) input.value = versions[0];
  } catch (e) {
    input.placeholder = 'failed to load versions';
    toast('Loader version list failed: ' + e.message, true);
  }
}

$('#createInstanceBtn').addEventListener('click', async () => {
  const name = $('#niName').value.trim();
  const version_id = $('#niVersion').value;
  const loader = $('#niLoader').value;
  const loader_version = $('#niLoaderVersion').value.trim();
  const min_mb = parseInt($('#niMinMb').value, 10) || 1024;
  const max_mb = parseInt($('#niMaxMb').value, 10) || 4096;
  const jvm_args = $('#niJvmArgs').value.trim();
  if (!name) return toast('Give it a name first.', true);
  if (!version_id) return toast('Pick a Minecraft version.', true);
  if (loader !== 'vanilla' && !loader_version) return toast('Pick a loader version.', true);
  try {
    await api('/api/instances', {
      method: 'POST',
      body: JSON.stringify({ name, version_id, loader, loader_version, min_mb, max_mb, jvm_args }),
    });
    toast(loader === 'vanilla' ? 'Instance created.' : `Instance created — installing ${loader} in the background.`);
    closeNewInstance();
    $('#niName').value = '';
    loadInstances();
    openConsole();
  } catch (e) { toast(e.message, true); }
});

/* ─────────────── edit instance modal ─────────────── */

const editInstanceModal = $('#editInstanceModal');

function openEditInstance(id) {
  const inst = state.instances.find(i => i.id === id);
  if (!inst) return;
  $('#eiId').value = inst.id;
  $('#eiName').value = inst.name;
  $('#eiMinMb').value = inst.min_mb ?? 1024;
  $('#eiMaxMb').value = inst.max_mb ?? 4096;
  $('#eiJvmArgs').value = inst.jvm_args || '';
  $('#eiMeta').innerHTML = `Version <strong>${esc(inst.mc_version || inst.version_id)}</strong> · loader <strong>${esc(inst.loader)}</strong>${inst.loader_version ? ' ' + esc(inst.loader_version) : ''} · folder: ${esc(inst.game_dir || '')}`;
  editInstanceModal.classList.remove('hidden');
}
function closeEditInstance() { editInstanceModal.classList.add('hidden'); }

$('#closeEditInstance').addEventListener('click', closeEditInstance);
$('#cancelEditInstance').addEventListener('click', closeEditInstance);
editInstanceModal.addEventListener('click', e => { if (e.target === editInstanceModal) closeEditInstance(); });

$('#saveEditInstanceBtn').addEventListener('click', async () => {
  const id = $('#eiId').value;
  try {
    await api('/api/instances/' + encodeURIComponent(id), {
      method: 'PUT',
      body: JSON.stringify({
        name: $('#eiName').value.trim(),
        min_mb: parseInt($('#eiMinMb').value, 10) || 1024,
        max_mb: parseInt($('#eiMaxMb').value, 10) || 4096,
        jvm_args: $('#eiJvmArgs').value.trim(),
      }),
    });
    toast('Instance updated.');
    closeEditInstance();
    loadInstances();
  } catch (e) { toast(e.message, true); }
});

$('#deleteInstanceBtn').addEventListener('click', async () => {
  const id = $('#eiId').value;
  const inst = state.instances.find(i => i.id === id);
  if (!confirm(`Delete instance “${inst ? inst.name : id}” and ALL its saves, mods and configs?`)) return;
  try {
    await api('/api/instances/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('Instance deleted.');
    closeEditInstance();
    loadInstances();
  } catch (e) { toast(e.message, true); }
});

/* ─────────────── discover ─────────────── */

function renderDiscoverChrome() {
  const isPack = state.discoverType === 'modpack';
  const sel = $('#modrinthInstance');
  const modded = state.instances.filter(i => i.loader !== 'vanilla');
  sel.innerHTML = modded.length
    ? modded.map(i => `<option value="${esc(i.id)}">${esc(i.name)} (${esc(i.mc_version || '')} · ${esc(i.loader)})</option>`).join('')
    : '<option value="">— no modded instances —</option>';
  sel.style.display = isPack ? 'none' : '';
  $('#modrinthLoader').style.display = '';
}

$$('.seg').forEach(btn => btn.addEventListener('click', () => {
  state.discoverType = btn.dataset.discover;
  $$('.seg').forEach(b => b.classList.toggle('active', b === btn));
  $('#modrinthResults').innerHTML = '';
  $('#discoverEmpty').classList.remove('hidden');
  renderDiscoverChrome();
}));

function resultCard(hit) {
  const isPack = state.discoverType === 'modpack';
  return `
  <div class="result-card">
    ${hit.icon_url ? `<img class="result-icon" src="${esc(hit.icon_url)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'" />` : '<div class="result-icon"></div>'}
    <div class="result-body">
      <div class="result-title">${esc(hit.title)}<span class="result-dl">⬇ ${fmtDownloads(hit.downloads)}</span></div>
      <div class="result-desc">${esc(hit.description)}</div>
      <div class="result-foot">
        <span class="result-author">by ${esc(hit.author || 'unknown')}</span>
        <button class="btn btn-primary btn-small install-btn"
                data-project="${esc(hit.project_id)}"
                data-title="${esc(hit.title)}"
                data-latest="${esc(hit.latest_version || '')}">${isPack ? 'Install pack' : 'Install'}</button>
      </div>
    </div>
  </div>`;
}

async function doSearch() {
  const q = $('#modrinthQuery').value.trim();
  const loader = $('#modrinthLoader').value;
  const gv = $('#modrinthGameVersion').value.trim();
  $('#discoverEmpty').classList.add('hidden');
  $('#modrinthResults').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const data = await api(`/api/modrinth/search?type=${state.discoverType}&q=${encodeURIComponent(q)}&loader=${encodeURIComponent(loader)}&game_version=${encodeURIComponent(gv)}&limit=30`);
    const hits = data.hits || [];
    $('#modrinthResults').innerHTML = hits.map(resultCard).join('');
    if (!hits.length) { $('#discoverEmpty').classList.remove('hidden'); $('#discoverEmpty').querySelector('h3').textContent = 'Nothing found'; }
  } catch (e) {
    $('#modrinthResults').innerHTML = '';
    toast('Search failed: ' + e.message, true);
  }
}

$('#modrinthSearchBtn').addEventListener('click', doSearch);
$('#modrinthQuery').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

$('#modrinthResults').addEventListener('click', async ev => {
  const btn = ev.target.closest('.install-btn');
  if (!btn) return;
  const projectId = btn.dataset.project;
  const title = btn.dataset.title;
  const loader = $('#modrinthLoader').value;
  const gv = $('#modrinthGameVersion').value.trim();
  btn.disabled = true;
  try {
    if (state.discoverType === 'modpack') {
      await api('/api/modrinth/install_pack', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, name: title, loader, game_version: gv }),
      });
      toast(`Installing “${title}” — watch the console.`);
    } else {
      const instanceId = $('#modrinthInstance').value;
      if (!instanceId) { toast('No modded instance selected — create one first.', true); btn.disabled = false; return; }
      await api('/api/modrinth/install_mod', { method: 'POST', body: JSON.stringify({ project_id: projectId, instance_id: instanceId }) });
      toast(`Installing “${title}”…`);
    }
    openConsole();
    setTimeout(() => { btn.disabled = false; }, 1500);
  } catch (e) {
    btn.disabled = false;
    toast(e.message, true);
  }
});

$('#mrpackImportBtn').addEventListener('click', async () => {
  const path = $('#mrpackPath').value.trim();
  if (!path) return toast('Paste the full path to the .mrpack file.', true);
  try {
    await api('/api/modrinth/import_mrpack', { method: 'POST', body: JSON.stringify({ path }) });
    toast('Importing modpack — watch the console.');
    openConsole();
  } catch (e) { toast(e.message, true); }
});

/* ─────────────── settings ─────────────── */

async function loadSettings() {
  try {
    const { settings, data_dir } = await api('/api/settings');
    state.settings = settings;
    state.dataDir = data_dir;
    $('#profileUsername').value = settings.offline_username || 'Player';
    $('#javaPath').value = settings.java_path || '';
    $('#defaultMinMb').value = settings.default_min_mb;
    $('#defaultMaxMb').value = settings.default_max_mb;
    $('#jvmArgs').value = settings.jvm_args || '';
    $('#updateRepo').value = settings.update_repo || '';
    $('#newsEnabled').checked = settings.news_enabled !== false;
    $('#dataDirPath').textContent = data_dir;
    $('#footVersion').textContent = 'v' + (await appVersion());
    refreshProfile();
  } catch (e) { toast(e.message, true); }
}

let cachedVersion = null;
async function appVersion() {
  if (cachedVersion) return cachedVersion;
  try { cachedVersion = (await api('/api/version')).version; } catch { cachedVersion = '?'; }
  return cachedVersion;
}

async function loadSettingsView() {
  await loadSettings();
  rescanJava();
}

async function rescanJava() {
  const list = $('#detectedJavaList');
  list.innerHTML = '<div class="detected-item"><code>scanning…</code></div>';
  try {
    const { javas } = await api('/api/java/detected');
    list.innerHTML = javas.length
      ? javas.map(j => `<div class="detected-item"><span class="detected-major">Java ${j.major}</span><code>${esc(j.path)}</code></div>`).join('')
      : '<div class="detected-item"><code>nothing detected — Temurin will be auto-downloaded</code></div>';
  } catch (e) {
    list.innerHTML = `<div class="detected-item"><code>rescan failed: ${esc(e.message)}</code></div>`;
  }
}
$('#rescanJavaBtn').addEventListener('click', rescanJava);

$('#saveSettingsBtn').addEventListener('click', async () => {
  const body = {
    offline_username: ($('#profileUsername').value || 'Player').trim() || 'Player',
    java_path: $('#javaPath').value.trim(),
    default_min_mb: parseInt($('#defaultMinMb').value, 10) || 1024,
    default_max_mb: parseInt($('#defaultMaxMb').value, 10) || 4096,
    jvm_args: $('#jvmArgs').value.trim(),
    update_repo: $('#updateRepo').value.trim(),
    news_enabled: $('#newsEnabled').checked,
  };
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
    toast('Settings saved.');
    loadSettings();
    loadNews();
  } catch (e) { toast(e.message, true); }
});

$('#openDataDirBtn').addEventListener('click', () => api('/api/open_data_dir', { method: 'POST' }).catch(e => toast(e.message, true)));

$('#checkUpdateBtn').addEventListener('click', async () => {
  const el = $('#updateStatus');
  el.textContent = 'Checking…';
  el.className = 'update-status';
  try {
    const r = await api('/api/update/check');
    if (r.error) { el.textContent = '⚠ ' + r.error; el.classList.add('bad'); return; }
    if (r.update_available) {
      el.textContent = `Update available: ${r.latest} (you have ${r.current})\n${r.url || ''}`;
      el.classList.add('ok');
    } else {
      el.textContent = `You’re on the latest version (${r.current}).`;
      el.classList.add('ok');
    }
  } catch (e) { el.textContent = '⚠ ' + e.message; el.classList.add('bad'); }
});

async function checkUpdateBanner() {
  try {
    const r = await api('/api/update/check');
    if (r.update_available) {
      $('#updateBanner').classList.remove('hidden');
      $('#updateLatest').textContent = r.latest;
      $('#updateNotes').textContent = (r.notes || '').split('\n')[0].slice(0, 140);
      $('#updateLink').href = r.asset_url || r.url || '#';
    }
  } catch { /* banner is optional; stay quiet */ }
}

/* ─────────────── console drawer ─────────────── */

function openConsole() { $('#console').classList.add('open'); }
$('#consoleToggle').addEventListener('click', () => $('#console').classList.toggle('open'));
$('#clearLogsBtn').addEventListener('click', () => { $('#consoleLog').textContent = ''; state.lastLogs = []; });
$('#stopGameBtn').addEventListener('click', async () => {
  try { const { stopped } = await api('/api/stop', { method: 'POST' }); toast(stopped ? 'Stop signal sent.' : 'Nothing to stop.'); }
  catch (e) { toast(e.message, true); }
});

const consoleLog = $('#consoleLog');
consoleLog.addEventListener('scroll', () => {
  const nearBottom = consoleLog.scrollHeight - consoleLog.scrollTop - consoleLog.clientHeight < 40;
  state.logStick = nearBottom;
});

function renderLogs(lines) {
  if (lines.length === state.lastLogs.length && lines.join() === state.lastLogs.join()) return;
  state.lastLogs = lines;
  consoleLog.textContent = lines.join('\n');
  if (state.logStick) consoleLog.scrollTop = consoleLog.scrollHeight;
}

function setConsoleStatus(snapshot) {
  const dot = $('#consoleDot');
  const statusEl = $('#consoleStatus');
  const phaseEl = $('#consolePhase');
  const pctEl = $('#consolePct');
  const fill = $('#progressFill');
  const stopBtn = $('#stopGameBtn');
  const job = snapshot.job, game = snapshot.game;

  dot.className = 'console-dot';
  phaseEl.textContent = '';
  pctEl.textContent = '';

  if (game.running) {
    dot.classList.add('running');
    statusEl.textContent = 'Game running';
    stopBtn.disabled = false;
  } else {
    stopBtn.disabled = true;
    if (job.status === 'running') {
      dot.classList.add('working');
      statusEl.textContent = 'Working…';
      phaseEl.textContent = job.message ? `— ${job.message}` : (job.phase || '');
      if (job.total > 1) {
        pctEl.textContent = job.percent + '%';
        fill.style.width = job.percent + '%';
      } else {
        pctEl.textContent = '';
        fill.style.width = '35%';
      }
    } else if (job.status === 'error') {
      dot.classList.add('error');
      statusEl.textContent = 'Error';
      phaseEl.textContent = '— ' + (job.error || 'unknown error');
      fill.style.width = '100%';
    } else if (job.status === 'done') {
      statusEl.textContent = 'Done';
      fill.style.width = '0%';
    } else {
      statusEl.textContent = 'Idle';
      fill.style.width = '0%';
    }
  }

  const merged = [...(game.lines || [])];
  if (job.status === 'running' && job.message) merged.push('… ' + job.message);
  if (job.status === 'error' && job.error) merged.push('✗ ' + job.error);
  renderLogs(merged);
}

let pollTimer = null;
async function poll() {
  try {
    const snap = await api('/api/job');
    setConsoleStatus(snap);
  } catch { /* server restarting? stay calm */ }
}
setInterval(poll, 1000);

/* ─────────────── boot ─────────────── */

(async function boot() {
  await loadSettings();
  await loadInstances();
  loadNews();
  checkUpdateBanner();
  poll();
})();
