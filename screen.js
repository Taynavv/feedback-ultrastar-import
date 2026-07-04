// Import UltraStar plugin — screen.js

(function () {
'use strict';

const PLUGIN_ID = 'ultrastar_import';
const API_BASE  = `/api/plugins/${PLUGIN_ID}`;
const WS_PROTO  = location.protocol === 'https:' ? 'wss' : 'ws';
const WS_BASE   = `${WS_PROTO}://${location.host}/ws/plugins/${PLUGIN_ID}`;

let _songs = [];       // last scan result
let _dir = '';
let _importing = false;

function esc(s) {
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function $(id) { return document.getElementById(id); }

// ── Status ────────────────────────────────────────────────────────────────

async function usStatus() {
    try {
        const s = await (await fetch(`${API_BASE}/status`)).json();
        if (!s.ffmpeg_available) {
            $('us-noffmpeg-reason').textContent = s.ffmpeg_hint || 'ffmpeg not found.';
            $('us-noffmpeg').classList.remove('hidden');
            $('us-scan-btn').disabled = true;
            $('us-scan-btn').classList.add('opacity-50', 'pointer-events-none');
        }
        if (!s.merge_available) {
            const lbl = $('us-merge-label');
            if (lbl) { lbl.classList.add('hidden'); $('us-merge-toggle').checked = false; }
        }
        if (s.last_dir && !$('us-dir').value) $('us-dir').value = s.last_dir;
    } catch (_) { /* scan will surface errors */ }
}

setTimeout(() => {
    usStatus();
    usLoadBackups();
    const t = $('us-merge-toggle');
    if (t) t.addEventListener('change', () => { if (!_importing) usRenderList(); });
}, 100);

// ── Scan ──────────────────────────────────────────────────────────────────

async function usScan() {
    if (_importing) return;   // a rescan mid-import would orphan the live progress state
    const dir = $('us-dir').value.trim();
    if (!dir) { $('us-scan-hint').textContent = 'Enter a folder path first.'; return; }

    $('us-scan-hint').textContent = 'Scanning…';
    $('us-list').classList.add('hidden');
    $('us-result').classList.add('hidden');
    $('us-progress').classList.add('hidden');
    try {
        const resp = await fetch(`${API_BASE}/scan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dir }),
        });
        const data = await resp.json();
        if (data.error) { $('us-scan-hint').textContent = data.error; return; }

        _songs = data.songs || [];
        _dir = data.dir || dir;
        _songs.forEach((s) => {
            s.selected = !s.imported && !s.merged && s.has_audio;
            s.state = null; // run-time import state
        });
        const errs = (data.errors || []).length;
        $('us-scan-hint').textContent =
            `${_songs.length} song${_songs.length === 1 ? '' : 's'} found`
            + (errs ? ` · ${errs} unreadable chart${errs === 1 ? '' : 's'} skipped` : '');
        usRenderList();
    } catch (err) {
        $('us-scan-hint').textContent = `Scan failed: ${err}`;
    }
}

// ── List rendering / selection ────────────────────────────────────────────

function usRenderList() {
    if (!_songs.length) { $('us-list').classList.add('hidden'); return; }
    $('us-list').classList.remove('hidden');

    const imported = _songs.filter((s) => s.imported).length;
    $('us-list-summary').textContent =
        `${_songs.length} songs · ${imported} already imported`;

    $('us-rows').innerHTML = _songs.map((s, i) => {
        let status;
        if (s.state === 'converting') status = '<span class="text-accent">converting…</span>';
        else if (s.state === 'merging') status = '<span class="text-accent">merging…</span>';
        else if (s.state === 'done-merged') status = `<span class="text-green-400" title="${esc(s.stateMsg || '')}">merged</span>`;
        else if (s.state === 'done') status = '<span class="text-green-400">imported</span>';
        else if (s.state === 'failed') status = `<span class="text-red-400" title="${esc(s.stateMsg || '')}">failed</span>`;
        else if (!s.has_audio) status = '<span class="text-red-400/70">no audio</span>';
        else if (s.merged) status = `<span class="text-gray-600" title="${esc(s.merge_into || '')}">merged</span>`;
        else if (s.imported) status = '<span class="text-gray-600">imported</span>';
        else if (s.merge_into && $('us-merge-toggle') && $('us-merge-toggle').checked)
            status = `<span class="text-accent/80" title="${esc(s.merge_into)}">merge&nbsp;→</span>`;
        else status = '<span class="text-gray-500">new</span>';
        const duet = s.is_duet ? ' <span class="text-xs text-gray-600">(duet)</span>' : '';
        return `<tr class="border-b border-gray-800/50 hover:bg-dark-600/40" data-row="${i}">
            <td class="px-4 py-1.5">
                <input type="checkbox" data-idx="${i}" ${s.selected ? 'checked' : ''}
                    ${!s.has_audio || _importing ? 'disabled' : ''} class="accent-accent">
            </td>
            <td class="px-2 py-1.5 text-gray-300">${esc(s.artist)}</td>
            <td class="px-2 py-1.5 text-gray-300">${esc(s.title)}${duet}</td>
            <td class="px-2 py-1.5 text-gray-500 text-xs">${esc(s.language)}</td>
            <td class="px-2 py-1.5 text-right pr-4 text-xs">${status}</td>
        </tr>`;
    }).join('');

    $('us-rows').querySelectorAll('input[type=checkbox]').forEach((cb) => {
        cb.addEventListener('change', () => {
            _songs[Number(cb.dataset.idx)].selected = cb.checked;
            usUpdateCount();
        });
    });
    usUpdateCount();
}

function usUpdateCount() {
    const n = _songs.filter((s) => s.selected).length;
    $('us-selected-count').textContent = n ? `${n} selected` : 'nothing selected';
    $('us-import-btn').classList.toggle('opacity-50', !n || _importing);
    $('us-import-btn').classList.toggle('pointer-events-none', !n || _importing);
    // Grey out Scan while importing (unless already disabled — e.g. no ffmpeg).
    const scanBtn = $('us-scan-btn');
    if (scanBtn && !scanBtn.disabled) {
        scanBtn.classList.toggle('opacity-50', _importing);
        scanBtn.classList.toggle('pointer-events-none', _importing);
    }
}

function usSelectNew()  { if (!_importing) { _songs.forEach((s) => { s.selected = !s.imported && !s.merged && s.has_audio; }); usRenderList(); } }
function usSelectAll()  { if (!_importing) { _songs.forEach((s) => { s.selected = s.has_audio; }); usRenderList(); } }
function usSelectNone() { if (!_importing) { _songs.forEach((s) => { s.selected = false; }); usRenderList(); } }

// ── Import ────────────────────────────────────────────────────────────────

function usLog(line, cls) {
    const div = document.createElement('div');
    if (cls) div.className = cls;
    div.textContent = line;
    $('us-log').appendChild(div);
    $('us-log').parentElement.scrollTop = $('us-log').parentElement.scrollHeight;
}

async function usImport() {
    if (_importing) return;
    const picked = _songs.filter((s) => s.selected);
    if (!picked.length) return;

    // Claim the run synchronously, before any await — a rapid second click must
    // not slip past the guard during the import_start round-trip and open a
    // second job. Two jobs race the same output pak/backup and can corrupt it.
    _importing = true;
    usRenderList();

    let start;
    try {
        const mergeOn = !!($('us-merge-toggle') && $('us-merge-toggle').checked);
        const resp = await fetch(`${API_BASE}/import_start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dir: _dir, folders: picked.map((s) => s.folder), merge: mergeOn }),
        });
        start = await resp.json();
    } catch (err) {
        _importing = false;
        usRenderList();
        $('us-scan-hint').textContent = `Could not start import: ${err}`;
        return;
    }
    if (start.error) {
        _importing = false;
        usRenderList();
        $('us-scan-hint').textContent = start.error;
        return;
    }

    usRenderList();
    $('us-progress').classList.remove('hidden');
    $('us-result').classList.add('hidden');
    $('us-log').innerHTML = '';
    $('us-bar').style.width = '0%';
    $('us-stage').textContent = `Importing ${picked.length} song${picked.length === 1 ? '' : 's'}…`;

    const byFolder = Object.fromEntries(_songs.map((s) => [s.folder, s]));
    const ws = new WebSocket(`${WS_BASE}/import?job=${encodeURIComponent(start.job)}`);

    // usFinish must run exactly once: a normal 'done' is followed by a server-side
    // close, and an abnormal drop can fire both onerror and onclose.
    let settled = false;
    const settle = (m) => { if (!settled) { settled = true; usFinish(m); } };

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);

        if (msg.error) { settle({ error: msg.error }); return; }

        if (msg.stage && msg.folder) {
            const s = byFolder[msg.folder];
            const label = `${msg.artist || ''} — ${msg.title || msg.folder}`;
            if (msg.stage === 'converting') {
                if (s) s.state = 'converting';
                $('us-stage').textContent = `(${msg.i + 1}/${msg.total}) ${label}`;
            } else if (msg.stage === 'merging') {
                if (s) s.state = 'merging';
                $('us-stage').textContent = `(${msg.i + 1}/${msg.total}) ${label} → ${msg.target || ''}`;
            } else if (msg.stage === 'kept-separate') {
                usLog(`≠ ${label}: not the same recording — importing separately (${msg.message || ''})`, 'text-yellow-400/70');
            } else if (msg.stage === 'done') {
                if (s) {
                    s.selected = false;
                    if (msg.merged_into) { s.state = 'done-merged'; s.stateMsg = msg.merged_into; s.merged = true; }
                    else { s.state = 'done'; s.imported = true; }
                }
                usLog(msg.merged_into ? `⇄ ${label} → merged into ${msg.merged_into}` : `✓ ${label}`,
                      'text-green-400/80');
                $('us-bar').style.width = `${Math.round(((msg.i + 1) / msg.total) * 100)}%`;
            } else if (msg.stage === 'failed') {
                if (s) { s.state = 'failed'; s.stateMsg = msg.message || ''; }
                usLog(`✗ ${msg.folder}: ${msg.message || 'failed'}`, 'text-red-400/80');
                $('us-bar').style.width = `${Math.round(((msg.i + 1) / msg.total) * 100)}%`;
            }
            usRenderList();
        }

        if (msg.done) settle(msg);
    };

    ws.onerror = () => settle({ error: 'Connection lost' });
    // A clean server close with no 'done'/'error' frame (app shutdown mid-import,
    // dropped socket) fires only onclose — recover instead of leaving the spinner
    // stuck and the controls disabled until reload.
    ws.onclose = () => settle({ error: 'Connection closed before the import finished' });
}

function usFinish(msg) {
    _importing = false;
    usRenderList();
    usLoadBackups();   // merges may have created new backups
    $('us-result').classList.remove('hidden');
    if (msg.error) {
        $('us-result').innerHTML = `
            <div class="bg-red-900/20 border border-red-800/30 rounded-xl p-5 text-center">
                <p class="text-red-400 font-semibold mb-1">Import failed</p>
                <p class="text-sm text-gray-400">${esc(msg.error)}</p>
            </div>`;
        return;
    }
    const merged = msg.merged
        ? ` (${msg.merged} merged into existing songs)` : '';
    const failed = msg.failed
        ? ` · <span class="text-red-400">${msg.failed} failed</span>` : '';
    const cancelled = msg.cancelled ? ' · stopped early' : '';
    $('us-bar').style.width = '100%';
    $('us-stage').textContent = 'Complete';
    $('us-result').innerHTML = `
        <div class="bg-green-900/20 border border-green-800/30 rounded-xl p-5 text-center">
            <p class="text-green-400 font-semibold mb-1">
                ${msg.imported} song${msg.imported === 1 ? '' : 's'} imported${merged}
            </p>
            <p class="text-xs text-gray-500">${failed}${cancelled}</p>
            <p class="text-xs text-gray-600 mt-2">They're in your library now — pick the "Vocals" arrangement.</p>
        </div>`;
}

// ── Merge backups ─────────────────────────────────────────────────────────

function fmtSize(bytes) {
    const mb = (bytes || 0) / (1024 * 1024);
    return mb >= 100 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
}

async function usLoadBackups() {
    let data;
    try {
        data = await (await fetch(`${API_BASE}/backups`)).json();
    } catch (_) { return; }
    const wrap = $('us-backups');
    const backups = data.backups || [];
    if (!backups.length) { wrap.classList.add('hidden'); return; }

    wrap.classList.remove('hidden');
    $('us-bak-summary').textContent =
        `· ${backups.length} pak${backups.length === 1 ? '' : 's'} · ${fmtSize(data.total_size)}`;
    $('us-bak-rows').innerHTML = backups.map((b) => `
        <div class="flex items-center gap-3 px-4 py-2 text-sm">
            <span class="flex-1 text-gray-300">${esc(b.display)}</span>
            <span class="text-xs text-gray-600">${fmtSize(b.size)}</span>
            <button data-bak-restore="${esc(b.rel)}"
                class="text-xs text-gray-500 hover:text-white transition">Restore</button>
            <button data-bak-delete="${esc(b.rel)}"
                class="text-xs text-gray-500 hover:text-red-400 transition">Delete</button>
        </div>`).join('');

    $('us-bak-rows').querySelectorAll('[data-bak-restore]').forEach((btn) => {
        btn.addEventListener('click', () => usBakRestore(btn.dataset.bakRestore));
    });
    $('us-bak-rows').querySelectorAll('[data-bak-delete]').forEach((btn) => {
        btn.addEventListener('click', () => usBakDelete(btn.dataset.bakDelete));
    });
}

async function usBakPost(path, body) {
    try {
        const r = await (await fetch(`${API_BASE}/${path}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })).json();
        if (r.error) alert(r.error);
        return r;
    } catch (err) {
        alert(`Request failed: ${err}`);
        return {};
    }
}

async function usBakRestore(rel) {
    const r = await usBakPost('backup_restore', { rel });
    if (r.ok) {
        usLoadBackups();
        if (_dir && !_importing) usScan();   // merged badges just changed
    }
}

async function usBakDelete(rel) {
    if (!confirm('Delete this merge backup? The pak keeps its merged vocals — you just lose the ability to undo this merge.')) return;
    const r = await usBakPost('backup_delete', { rel });
    if (r.ok) usLoadBackups();
}

async function usBakDeleteAll() {
    if (!confirm('Delete every merge backup? Merged paks keep their vocals; you just lose the ability to undo.')) return;
    const r = await usBakPost('backup_delete', { all: true });
    if (r.ok) usLoadBackups();
}

// Expose for onclick handlers in screen.html
window.usScan = usScan;
window.usImport = usImport;
window.usSelectNew = usSelectNew;
window.usSelectAll = usSelectAll;
window.usSelectNone = usSelectNone;
window.usBakDeleteAll = usBakDeleteAll;

})();
