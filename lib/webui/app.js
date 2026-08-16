const $ = id => document.getElementById(id);

// SVG fills cannot use var(), so resolve the design tokens from app.css once
// at load. app.css stays the single source of truth for every brand colour.
const C = (() => {
  const cs = getComputedStyle(document.documentElement);
  const tok = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
  return {
    bgDeep: tok('--bg-deep', '#101317'),
    textMain: tok('--text-main', '#FAFAFA'),
    textMuted: tok('--text-muted', 'rgba(250,250,250,0.48)'),
    textFaint: tok('--text-faint', 'rgba(250,250,250,0.30)'),
    accent: tok('--accent', '#6366F1'),
    ok: tok('--color-ok', '#67F264'),
    warn: tok('--color-warn', '#F2B75E'),
    warnSoft: tok('--color-warn-soft', '#F2A05E'),
    danger: tok('--color-danger', '#F2646B'),
    boost: tok('--color-boost', '#F2646B'),
    powersave: tok('--color-powersave', '#67F264'),
    silent: tok('--color-silent', '#8B7CF6'),
    seriesLoad: tok('--series-load', '#6366F1'),
    seriesTemp: tok('--series-temp', '#F2B75E'),
    seriesGpu: tok('--series-gpu', '#E879C7'),
    seriesBattery: tok('--series-battery', '#67F264'),
  };
})();
const SERIES = [C.seriesLoad, C.seriesTemp, C.seriesGpu, C.seriesBattery];

// The header is minimal until you scroll, then it picks up a blurred ground.
const _header = document.querySelector('.header');
const _syncHeader = () => _header && _header.classList.toggle('scrolled', window.scrollY > 8);
window.addEventListener('scroll', _syncHeader, {passive: true});
_syncHeader();
let _prevData = null;
let _failCount = 0;

function secondsText(s) {
  if (s >= 3600) return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
  if (s >= 60) return `${Math.floor(s/60)}m`;
  return `${s}s`;
}

function showToast(text, isError = false) {
  const c = $('toast-container');
  const t = document.createElement('div');
  t.className = isError ? 'toast error' : 'toast';
  t.textContent = text;
  c.appendChild(t);
  setTimeout(() => { t.classList.add('hide'); setTimeout(() => t.remove(), 300); }, 4000);
}

function setGauge(id, value, max = 100) {
  const circle = $(`${id}Circle`);
  const text = $(`${id}Text`);
  if (!circle || !text) return;
  const r = 54, circ = 2 * Math.PI * r;
  circle.style.strokeDasharray = circ;
  const pct = Math.min(Math.max(value, 0), max) / max;
  circle.style.strokeDashoffset = circ - pct * circ;
  text.textContent = Math.round(value);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function tempColor(temp) {
  if (temp >= 85) return C.danger;
  if (temp >= 75) return C.warn;
  if (temp >= 60) return C.warnSoft;
  return C.ok;
}

function loadColor(load) {
  if (load >= 90) return C.danger;
  if (load >= 70) return C.warn;
  if (load >= 40) return C.accent;
  return C.ok;
}

function drawChart(history) {
  const svg = $('historyChart');
  if (!history || !history.length) {
    svg.innerHTML = `<text x="50%" y="50%" text-anchor="middle" fill="${C.textMuted}" font-size="13">Waiting for telemetry data...</text>`;
    return;
  }
  const W = 1000, H = 200, pL = 45, pR = 15, pT = 15, pB = 25;
  const cW = W - pL - pR, cH = H - pT - pB;
  const n = history.length;

  function makePath(data, key, max, color) {
    let pts = [], areaPts = [], circles = [];
    for (let i = 0; i < n; i++) {
      const x = pL + (n > 1 ? (i/(n-1)) * cW : cW/2);
      const v = Math.min(parseFloat(data[i][key] || 0), max);
      const y = H - pB - (v/max) * cH;
      pts.push(`${x},${y}`);
      areaPts.push(`${x},${y}`);
      const timeStr = data[i].iso ? data[i].iso.split('T')[1].substring(0,5) : '';
      circles.push(`<circle cx="${x}" cy="${y}" r="3" fill="${color}" opacity="0">
                      <title>${timeStr} | ${key}: ${parseFloat(data[i][key]||0).toFixed(1)}</title>
                    </circle>`);
    }
    const lineD = `M ${pts.join(' L ')}`;
    const areaD = `${lineD} L ${pL + cW},${H - pB} L ${pL},${H - pB} Z`;
    return `<path d="${areaD}" fill="url(#grad-${color})" opacity="0.15"/>
            <path d="${lineD}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <g class="chart-points" style="pointer-events:all;cursor:crosshair">
              ${circles.join('')}
              <style>.chart-points circle:hover { opacity: 1 !important; stroke: ${C.textMain}; stroke-width: 1px; }</style>
            </g>`;
  }

  let grid = '';
  for (let p = 0; p <= 100; p += 25) {
    const y = H - pB - (p/100) * cH;
    grid += `<line x1="${pL}" y1="${y}" x2="${W-pR}" y2="${y}" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>`;
    grid += `<text x="${pL-8}" y="${y+4}" fill="${C.textFaint}" font-size="9" text-anchor="end">${p}</text>`;
  }

  // GPU power mapped to 0-200W scale shown as percentage
  let gpuData = history.map(r => ({...r, gpu_pct: String((parseFloat(r.gpu_power||0)/200)*100)}));

  // Profile transition bands — colored vertical strips when profile changes
  const profileColors = {performance: C.boost, balanced: C.powersave, 'power-saver': C.silent};
  let bands = '';
  let prevProfile = history[0]?.profile;
  for (let i = 1; i < n; i++) {
    const p = history[i].profile;
    if (p && p !== prevProfile) {
      const x = pL + (i/(n-1)) * cW;
      const color = profileColors[p] || C.textMuted;
      bands += `<line x1="${x}" y1="${pT}" x2="${x}" y2="${H-pB}" stroke="${color}" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.6">
                  <title>→ ${p}</title>
                </line>`;
      prevProfile = p;
    }
  }

  svg.innerHTML = `
    <defs>
      ${SERIES.map(s => `<linearGradient id="grad-${s}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${s}"/><stop offset="1" stop-color="${s}" stop-opacity="0"/></linearGradient>`).join('')}
    </defs>
    ${grid}
    ${bands}
    ${makePath(history, 'cpu_load', 100, C.seriesLoad)}
    ${makePath(history, 'cpu_temp', 100, C.seriesTemp)}
    ${makePath(gpuData, 'gpu_pct', 100, C.seriesGpu)}
    ${makePath(history, 'battery_pct', 100, C.seriesBattery)}
  `;
}

// ── Simple / Advanced view ────────────────────────────────────────────
function applyView(view) {
  document.body.dataset.view = view;
  $('viewSimple').classList.toggle('on', view === 'simple');
  $('viewAdvanced').classList.toggle('on', view === 'advanced');
  try { localStorage.setItem('boostView', view); } catch (e) { /* private mode */ }
}
$('viewSimple').addEventListener('click', () => applyView('simple'));
$('viewAdvanced').addEventListener('click', () => applyView('advanced'));
try { applyView(localStorage.getItem('boostView') === 'advanced' ? 'advanced' : 'simple'); }
catch (e) { applyView('simple'); }

// ── Component temperatures ────────────────────────────────────────────
const SPARK_COLUMN = {cpu: 'cpu_temp', gpu: 'gpu_temp', nvme: 'nvme_temp', ram: 'ram_temp', vrm: 'vrm_temp', board: 'board_temp'};
const STATE_COLOR = {ok: C.ok, warn: C.warn, critical: C.danger};

function sparkline(history, column, color) {
  if (!history || history.length < 2) return '';
  const values = history.map(r => parseFloat(r[column])).filter(v => !isNaN(v) && v > 0);
  if (values.length < 2) return '';
  const min = Math.min(...values), max = Math.max(...values);
  const span = (max - min) || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${28 - ((v - min) / span) * 24}`);
  return `<svg class="tc-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
            <polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
          </svg>`;
}

function renderSensors(data) {
  const grid = $('tempGrid');
  const groups = data.sensors || [];
  if (!groups.length) {
    grid.innerHTML = '<div style="color:var(--text-muted);font-size:13px">No hwmon sensors found. Run <code>auto doctor</code> for the missing kernel modules.</div>';
    return;
  }
  grid.innerHTML = groups.map(g => {
    const color = STATE_COLOR[g.state] || STATE_COLOR.ok;
    const column = SPARK_COLUMN[g.category];
    const spark = column ? sparkline(data.history, column, color) : '';
    const rows = g.sensors.map(s =>
      `<div class="tc-row"><span>${esc(s.label)}</span><span style="color:${STATE_COLOR[s.state] || ''}">${esc(s.temp)}°C</span></div>`).join('');
    const detail = g.sensors.length > 1
      ? `<details${g.bulk ? '' : ''}><summary>${g.sensors.length} sensors ▾</summary><div class="tc-list">${rows}</div></details>` : '';
    return `<div class="temp-card ${g.state === 'ok' ? '' : esc(g.state)}">
      <div class="tc-head"><span class="tc-name">${esc(g.label)}</span>
        <span class="tc-val" style="color:${color}">${esc(g.max)}<small>°C</small></span></div>
      <div class="tc-sub">warn ${esc(g.warn)}°C · critical ${esc(g.crit)}°C</div>
      ${spark}${detail}
    </div>`;
  }).join('');
  const hottest = groups.reduce((a, b) => (b.state === 'critical' ? b : a), null);
  $('tempHint').textContent = hottest
    ? `${hottest.label} is at ${hottest.max}°C, past its ${hottest.crit}°C critical mark.`
    : '';
}

// ── Silent/Eco interlock ──────────────────────────────────────────────
let _wasPending = false;
let _wasGuarded = false;

// Anything the machine decides on its own gets announced once, so an
// automatic override is never something the user discovers by accident.
function announceOverrides(data) {
  const lock = data.interlock || {};
  const guard = ((data.fans || {}).status || {}).guard || {};
  if (_wasPending && !lock.pending && !lock.silentBlocked) {
    showToast('Eco Mode was queued and has now been applied — the machine cooled down.');
  }
  if (!_wasGuarded && guard.active && guard.reason) {
    showToast(`Fans raised automatically: ${guard.reason}`);
  }
  _wasPending = !!lock.pending;
  _wasGuarded = !!guard.active;
}

function renderInterlock(data) {
  const lock = data.interlock || {};
  const banner = $('interlockBanner');
  const btn = $('btn-silent');
  announceOverrides(data);
  if (lock.silentBlocked || lock.pending) {
    banner.hidden = false;
    $('interlockText').textContent = lock.hint || '';
    if (btn) {
      btn.classList.add('blocked');
      btn.title = lock.hint || '';
    }
  } else {
    banner.hidden = true;
    if (btn) { btn.classList.remove('blocked'); btn.title = 'Strict thermal and noise constraints, best for night time'; }
  }
}

// ── Fan control ───────────────────────────────────────────────────────
const _fanEditor = {};      // fanId -> {profile, points, dirty}
let _fanCardsKey = '';
const fanSlug = id => 'f_' + id.replace(/[^a-zA-Z0-9]/g, '_');
const PROFILE_LABEL = {boost: 'Performance', balanced: 'Balanced', silent: 'Eco'};
const MODE_BADGE = {
  curve: ['ok', 'curve'], guard: ['hold', 'cooling'], test: ['info', 'test'],
  backoff: ['alert', 'conflict'], paused: ['info', 'paused'], bios: ['info', 'bios'],
};

function guardMin(temp, thresholds) {
  const hot = Number(thresholds.tempHot || 78), crit = Number(thresholds.tempCritical || 85);
  if (temp >= crit) return 100;
  if (temp >= hot) return 85;
  if (temp >= hot - 6) return 65;
  return 0;
}

function renderFans(data) {
  const fans = data.fans || {};
  const section = $('fanSection');
  if (!fans.available) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const status = fans.status || {};
  const badge = $('fanEngineBadge');
  badge.textContent = fans.enabled ? 'controlling fans' : 'off (BIOS controls fans)';
  badge.className = 'badge ' + (fans.enabled ? 'ok' : 'info');
  $('fanEnableBtn').classList.toggle('active-preset', !!fans.enabled);
  $('fanDisableBtn').classList.toggle('active-preset', !fans.enabled);

  const guard = status.guard || {};
  $('fanGuardBanner').hidden = !(guard.active && guard.reason);
  $('fanGuardText').textContent = guard.reason
    ? `Fans raised to at least ${guard.floor}% because ${guard.reason}. They go back to your curve on their own.`
    : '';
  const conflict = status.conflict || fans.configError;
  $('fanConflictBanner').hidden = !conflict;
  $('fanConflictText').textContent = conflict || '';

  const ids = Object.keys(fans.config || {});
  const key = ids.join('|');
  if (key !== _fanCardsKey) {
    _fanCardsKey = key;
    $('fanGrid').innerHTML = ids.map(id => fanCardHtml(id, fans)).join('');
    ids.forEach(id => wireFanCard(id, data));
  }

  const live = {};
  (status.fans || []).forEach(f => { live[f.id] = f; });
  ids.forEach(id => {
    const slug = fanSlug(id), f = live[id] || {};
    const set = (suffix, text) => { const el = $(`${suffix}${slug}`); if (el) el.textContent = text; };
    set('fanpwm', f.pwm ?? '—');
    set('fanrpm', f.rpm === null || f.rpm === undefined ? '—' : f.rpm);
    set('fansrc', f.sourceTemp ?? '—');
    const bar = $(`fanbar${slug}`);
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, f.pwm || 0))}%`;
    const modeBadge = $(`fanmode${slug}`);
    if (modeBadge) {
      const [cls, label] = MODE_BADGE[f.mode] || MODE_BADGE.bios;
      modeBadge.className = `badge ${cls}`;
      modeBadge.textContent = f.readOnly ? 'read-only' : label;
    }
    const note = $(`fannote${slug}`);
    if (note) note.textContent = f.note || '';
    drawCurve(id, data);
  });
}

function fanCardHtml(id, fans) {
  const slug = fanSlug(id);
  const cfg = fans.config[id] || {};
  const cal = (fans.calibration || {})[id] || {};
  const presets = (fans.presets || []).map(p =>
    `<button class="btn" data-fan-preset="${esc(id)}" data-preset="${esc(p)}">${esc(p[0].toUpperCase() + p.slice(1))}</button>`).join('');
  const tabs = (fans.profileKeys || ['boost', 'balanced', 'silent']).map((k, i) =>
    `<button data-fan-tab="${esc(id)}" data-profile="${esc(k)}" class="${i === 1 ? 'on' : ''}">${esc(PROFILE_LABEL[k] || k)}</button>`).join('');
  return `<div class="fan-card" id="fan${slug}">
    <div class="fan-head"><span class="fan-name">${esc(cfg.name || id)}</span><span class="badge info" id="fanmode${slug}">bios</span></div>
    <div style="font-size:11px;color:var(--text-muted)">${esc(id)}${cal.max_rpm ? ` · measured max ${esc(cal.max_rpm)} RPM` : ''}</div>
    <div class="fan-metrics">
      <div>Speed <strong id="fanpwm${slug}">—</strong>%</div>
      <div>RPM <strong id="fanrpm${slug}">—</strong></div>
      <div>Source <strong id="fansrc${slug}">—</strong>°C</div>
    </div>
    <div class="fan-bar"><i id="fanbar${slug}" style="width:0%"></i></div>
    <div class="preset-row">${presets}
      <button class="btn" data-fan-test="${esc(id)}" title="Runs this fan for 10 s at the speed your curve asks for at 75 °C">Test 10s</button>
    </div>
    <div style="font-size:11px;color:var(--color-warn);margin-top:8px" id="fannote${slug}"></div>
    <details class="curve-editor advanced-only">
      <summary>✏️ Edit the curve</summary>
      <div class="curve-tabs">${tabs}</div>
      <svg class="curve-svg" id="curve${slug}" viewBox="0 0 320 190" data-fan="${esc(id)}"></svg>
      <div class="curve-fields">
        <label>Min %<input type="number" min="0" max="100" id="fanmin${slug}" value="${esc(cfg.min_pwm ?? 20)}"></label>
        <label>Hyst up<input type="number" min="0" max="20" id="fanhu${slug}" value="${esc(cfg.hyst_up ?? 2)}"></label>
        <label>Hyst down<input type="number" min="0" max="20" id="fanhd${slug}" value="${esc(cfg.hyst_down ?? 4)}"></label>
        <label>Delay s<input type="number" min="0" max="120" id="fandl${slug}" value="${esc(cfg.response_delay_s ?? 6)}"></label>
        <label>Step %<input type="number" min="1" max="100" id="fanst${slug}" value="${esc(cfg.step_limit ?? 12)}"></label>
      </div>
      <div class="actions" style="margin-top:10px">
        <button class="btn primary" data-fan-save="${esc(id)}">💾 Save curve</button>
        <button class="btn" data-fan-reset="${esc(id)}">↺ Reload</button>
      </div>
    </details>
  </div>`;
}

function wireFanCard(id, data) {
  const fans = data.fans || {};
  const cfg = (fans.config || {})[id] || {};
  _fanEditor[id] = {
    profile: 'balanced',
    points: JSON.parse(JSON.stringify((cfg.profiles || {}).balanced || [])),
    minPwm: cfg.min_pwm ?? 20,
  };
  const svg = $(`curve${fanSlug(id)}`);
  if (svg) attachCurveDrag(svg, id);
}

function curveGeometry() {
  return {x0: 34, y0: 14, w: 272, h: 148, tMin: 20, tMax: 100};
}

function curveXY(temp, pwm) {
  const g = curveGeometry();
  return [
    g.x0 + ((temp - g.tMin) / (g.tMax - g.tMin)) * g.w,
    g.y0 + g.h - (pwm / 100) * g.h,
  ];
}

function curveInverse(px, py) {
  const g = curveGeometry();
  return [
    Math.round(g.tMin + ((px - g.x0) / g.w) * (g.tMax - g.tMin)),
    Math.round(((g.y0 + g.h - py) / g.h) * 100),
  ];
}

function drawCurve(id, data) {
  const svg = $(`curve${fanSlug(id)}`);
  const ed = _fanEditor[id];
  if (!svg || !ed) return;
  if (svg.dataset.dragging === '1') return;   // never redraw under the user's finger
  const g = curveGeometry();
  const thresholds = (data.auto && data.auto.thresholds) || {};
  const live = ((data.fans.status || {}).fans || []).find(f => f.id === id) || {};

  let grid = '';
  for (let p = 0; p <= 100; p += 25) {
    const y = g.y0 + g.h - (p / 100) * g.h;
    grid += `<line x1="${g.x0}" y1="${y}" x2="${g.x0 + g.w}" y2="${y}" stroke="rgba(255,255,255,0.06)"/>
             <text x="${g.x0 - 6}" y="${y + 3}" fill="${C.textFaint}" font-size="8" text-anchor="end">${p}</text>`;
  }
  for (let t = 20; t <= 100; t += 20) {
    const x = g.x0 + ((t - 20) / 80) * g.w;
    grid += `<line x1="${x}" y1="${g.y0}" x2="${x}" y2="${g.y0 + g.h}" stroke="rgba(255,255,255,0.06)"/>
             <text x="${x}" y="${g.y0 + g.h + 12}" fill="${C.textFaint}" font-size="8" text-anchor="middle">${t}°</text>`;
  }

  // Shaded "the engine will override you here" envelope.
  let envelope = '';
  const steps = [];
  for (let t = 20; t <= 100; t += 2) steps.push([t, Math.max(guardMin(t, thresholds), ed.minPwm)]);
  const envPts = steps.map(([t, p]) => curveXY(t, p).join(',')).join(' ');
  envelope = `<polyline points="${envPts}" fill="none" stroke="${C.danger}" stroke-width="1" stroke-dasharray="4,3" opacity="0.55"/>
              <polygon points="${curveXY(20, 0).join(',')} ${envPts} ${curveXY(100, 0).join(',')}" fill="rgba(242,100,107,0.08)"/>`;

  const pts = ed.points.map(([t, p]) => curveXY(t, p));
  const line = `<polyline points="${pts.map(p => p.join(',')).join(' ')}" fill="none" stroke="${C.accent}" stroke-width="2"/>`;
  // Points below the safety envelope are drawn as blocked: the engine will
  // override them at that temperature no matter what the curve says.
  const dots = ed.points.map(([t, p], i) => {
    const [x, y] = curveXY(t, p);
    const required = Math.max(guardMin(t, thresholds), ed.minPwm);
    const blocked = p < required;
    const title = blocked
      ? `${t}°C → ${p}% is ignored: this fan runs at ${required}% or more at ${t}°C`
      : `${t}°C → ${p}%`;
    return `<circle class="pt" data-i="${i}" cx="${x}" cy="${y}" r="6" fill="${blocked ? C.danger : C.accent}" stroke="${C.bgDeep}" stroke-width="2"><title>${title}</title></circle>`;
  }).join('');

  let marker = '';
  if (live.sourceTemp) {
    const [mx] = curveXY(Math.max(20, Math.min(100, live.sourceTemp)), 0);
    marker = `<line x1="${mx}" y1="${g.y0}" x2="${mx}" y2="${g.y0 + g.h}" stroke="${C.warn}" stroke-width="1.5" opacity="0.8"/>
              <text x="${mx + 3}" y="${g.y0 + 9}" fill="${C.warn}" font-size="8">now ${live.sourceTemp}°</text>`;
  }
  svg.innerHTML = `${grid}${envelope}${line}${dots}${marker}
    <text x="${g.x0}" y="${g.y0 + g.h + 24}" fill="${C.textMuted}" font-size="8">Red line = the minimum this fan will run at anyway</text>`;
}

function attachCurveDrag(svg, id) {
  let active = null;
  const point = evt => {
    const rect = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    return [
      ((evt.clientX - rect.left) / rect.width) * vb.width,
      ((evt.clientY - rect.top) / rect.height) * vb.height,
    ];
  };
  svg.addEventListener('pointerdown', evt => {
    const target = evt.target.closest('circle.pt');
    if (!target) return;
    active = Number(target.dataset.i);
    svg.dataset.dragging = '1';
    svg.setPointerCapture(evt.pointerId);
  });
  svg.addEventListener('pointermove', evt => {
    if (active === null) return;
    const ed = _fanEditor[id];
    const [px, py] = point(evt);
    let [temp, pwm] = curveInverse(px, py);
    const prev = ed.points[active - 1], next = ed.points[active + 1];
    temp = Math.max(20, Math.min(100, temp));
    if (prev) temp = Math.max(temp, prev[0] + 1);
    if (next) temp = Math.min(temp, next[0] - 1);
    pwm = Math.max(0, Math.min(100, pwm));
    if (prev) pwm = Math.max(pwm, prev[1]);
    if (next) pwm = Math.min(pwm, next[1]);
    ed.points[active] = [temp, pwm];
    svg.dataset.dragging = '0';
    drawCurve(id, _lastData || {auto: {}, fans: {status: {}}});
    svg.dataset.dragging = '1';
  });
  const release = evt => {
    if (active === null) return;
    active = null;
    svg.dataset.dragging = '0';
    if (evt.pointerId !== undefined && svg.hasPointerCapture?.(evt.pointerId)) svg.releasePointerCapture(evt.pointerId);
  };
  svg.addEventListener('pointerup', release);
  svg.addEventListener('pointercancel', release);
  svg.addEventListener('pointerleave', release);
}

document.addEventListener('click', evt => {
  const tab = evt.target.closest('[data-fan-tab]');
  if (tab) {
    const id = tab.dataset.fanTab, profile = tab.dataset.profile;
    const cfg = ((_lastData?.fans || {}).config || {})[id] || {};
    _fanEditor[id] = {
      profile,
      points: JSON.parse(JSON.stringify((cfg.profiles || {})[profile] || [])),
      minPwm: cfg.min_pwm ?? 20,
    };
    tab.parentElement.querySelectorAll('button').forEach(b => b.classList.remove('on'));
    tab.classList.add('on');
    drawCurve(id, _lastData);
    return;
  }
  const preset = evt.target.closest('[data-fan-preset]');
  if (preset) {
    sendAction('fan-preset', JSON.stringify({fan: preset.dataset.fanPreset, preset: preset.dataset.preset}));
    _fanCardsKey = '';   // force a rebuild so the editor picks up the new curve
    return;
  }
  const test = evt.target.closest('[data-fan-test]');
  if (test) {
    const id = test.dataset.fanTest;
    const ed = _fanEditor[id];
    const pwm = ed ? curveValueAt(ed.points, 75) : 60;
    sendAction('fan-test', JSON.stringify({fan: id, pwm, seconds: 10}));
    return;
  }
  const save = evt.target.closest('[data-fan-save]');
  if (save) {
    const id = save.dataset.fanSave, slug = fanSlug(id), ed = _fanEditor[id];
    if (!ed) return;
    sendAction('fan-config', JSON.stringify({
      fan: id,
      profiles: {[ed.profile]: ed.points},
      min_pwm: Number($(`fanmin${slug}`).value),
      hyst_up: Number($(`fanhu${slug}`).value),
      hyst_down: Number($(`fanhd${slug}`).value),
      response_delay_s: Number($(`fandl${slug}`).value),
      step_limit: Number($(`fanst${slug}`).value),
    }));
    _fanCardsKey = '';
    return;
  }
  const reset = evt.target.closest('[data-fan-reset]');
  if (reset) { _fanCardsKey = ''; refresh(); }
});

function curveValueAt(points, temp) {
  if (!points || !points.length) return 60;
  if (temp <= points[0][0]) return points[0][1];
  if (temp >= points[points.length - 1][0]) return points[points.length - 1][1];
  for (let i = 0; i < points.length - 1; i++) {
    const [t0, p0] = points[i], [t1, p1] = points[i + 1];
    if (temp >= t0 && temp <= t1) return Math.round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0));
  }
  return points[points.length - 1][1];
}

// ── GPU power limit ───────────────────────────────────────────────────
let _gpuProfile = 'boost';

function renderGpuLimit(data) {
  const gl = data.gpuLimit || {};
  const section = $('gpuLimitSection');
  section.hidden = !gl.supported;
  if (!gl.supported) return;
  $('gpuRangeText').textContent = `${gl.minW}–${gl.maxW} W`;
  const slider = $('gpuLimitSlider');
  slider.min = gl.minW;
  slider.max = gl.maxW;
  const requested = (gl.requested || {})[_gpuProfile] || 0;
  if (document.activeElement !== slider) slider.value = requested || gl.maxW;
  $('gpuLimitValue').textContent = requested ? `${requested} W` : `auto (${slider.value} W)`;
  $('gpuLimitNow').textContent = `${data.gpu.limit} W applied · ${data.gpu.power} W draw · ${data.gpu.temp} °C`;
}

$('gpuLimitSlider')?.addEventListener('input', () => {
  $('gpuLimitValue').textContent = `${$('gpuLimitSlider').value} W`;
});
$('gpuProfileTabs')?.addEventListener('click', evt => {
  const btn = evt.target.closest('[data-gpu-profile]');
  if (!btn) return;
  _gpuProfile = btn.dataset.gpuProfile;
  $('gpuProfileTabs').querySelectorAll('button').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  if (_lastData) renderGpuLimit(_lastData);
});
$('gpuLimitSave')?.addEventListener('click', () => {
  sendAction('gpu-limit', JSON.stringify({profile: _gpuProfile, watts: $('gpuLimitSlider').value}));
});
$('gpuLimitAuto')?.addEventListener('click', () => {
  sendAction('gpu-limit', JSON.stringify({profile: _gpuProfile, watts: ''}));
});

let _lastData = null;

function render(data) {
  _lastData = data;
  // Connection status
  $('connBar').classList.remove('show');
  _failCount = 0;

  // Service status
  const isActive = data.auto.service === 'active';
  $('serviceDot').className = `status-indicator ${isActive ? 'active' : 'inactive'}`;
  $('serviceText').textContent = `Auto: ${data.auto.service} • Web: ${data.web.service}`;
  $('updatedText').textContent = `Live • ${data.time}`;

  // Gauges with dynamic colors
  setGauge('cpuLoad', data.cpu.load);
  setGauge('cpuTemp', data.cpu.temp);

  const loadCircle = $('cpuLoadCircle');
  const lc = loadColor(data.cpu.load);
  loadCircle.style.stroke = lc;
  loadCircle.style.filter = `drop-shadow(0 0 8px ${lc}40)`;

  const tempCircle = $('cpuTempCircle');
  const tc = tempColor(data.cpu.temp);
  tempCircle.style.stroke = tc;
  tempCircle.style.filter = `drop-shadow(0 0 8px ${tc}40)`;

  // Temp danger state
  const tempCard = $('cpuTempCard');
  if (data.cpu.temp >= 80) { tempCard.classList.add('temp-alert'); }
  else { tempCard.classList.remove('temp-alert'); }

  // GPU
  $('gpuPowerText').textContent = data.gpu.power;
  $('gpuLimitText').textContent = data.gpu.limit;
  $('gpuTempText').textContent = data.gpu.temp;


  // Battery
  const bat = data.battery;
  if (bat && bat.pct !== null && bat.pct !== undefined) {
    $('batteryPctText').textContent = bat.pct;
    $('batteryStatusText').textContent = bat.status;
    $('acOnlineText').textContent = bat.acOnline === 1 ? 'Connected' : bat.acOnline === 0 ? 'On Battery' : '—';
    const batEl = $('batteryPctText');
    if (bat.pct <= bat.criticalPct) batEl.style.color = C.danger;
    else if (bat.pct <= bat.lowPct) batEl.style.color = C.warn;
    else batEl.style.color = '';
    // Show drain rate and estimated time remaining when on battery
    const timeRow = $('batteryTimeRow');
    if (bat.acOnline === 0 && bat.drainRatePctPerHour && bat.pct) {
      const hoursLeft = bat.pct / bat.drainRatePctPerHour;
      const h = Math.floor(hoursLeft);
      const m = Math.round((hoursLeft - h) * 60);
      const rateStr = `${bat.drainRatePctPerHour.toFixed(1)}%/h`;
      $('batteryTimeText').textContent = h > 0
        ? `~${h}h ${m}m remaining (${rateStr})`
        : `~${m}m remaining (${rateStr})`;
      timeRow.hidden = false;
    } else {
      timeRow.hidden = true;
    }
  } else {
    $('batteryPctText').textContent = '—';
    $('batteryStatusText').textContent = 'No battery';
    $('acOnlineText').textContent = '—';
    $('batteryTimeRow').hidden = true;
  }
  // System config
  $('profile').textContent = data.friendlyProfile;
  $('autoMode').textContent = data.auto.mode;
  $('limits').textContent = `${data.limits.pl1}/${data.limits.pl2} W`;
  $('turbo').textContent = data.system.turbo;
  $('thp').textContent = data.system.thp || '—';

  // Pause
  const p = data.auto.pause;
  $('pauseState').textContent = p.snoozed ? '⏸ Snoozed' : p.todayOff ? '⏸ Today off' : p.quietActive ? '🌙 Quiet hours' : '✅ Available';
  $('pauseReason').textContent = p.reason;

  // Quiet hours
  $('quietStart').value = data.auto.quietStart;
  $('quietEnd').value = data.auto.quietEnd;
  $('summerNights').textContent = data.auto.summerSilentNights.toUpperCase();

  // Decision
  $('decisionReasonCard').textContent = data.auto.decision;
  $('decisionReason').textContent = data.auto.decision;
  const t = data.auto.thresholds;
  $('tempHot').textContent = `${t.tempHot}°C`;
  $('tempCritical').textContent = `${t.tempCritical}°C`;
  $('boostLimit').textContent = `${t.boostTempLimit}°C`;
  $('busyTrigger').textContent = `${t.loadHigh}% / ${secondsText(t.loadHighDuration)}`;
  $('idleTrigger').textContent = `${t.loadIdle}% / ${secondsText(t.loadIdleDuration)}`;
  $('cooldown').textContent = secondsText(t.promptCooldown);

  // Summary
  $('avgCpu').textContent = `${Math.round(data.summary.avg_cpu)}%`;
  $('maxTemp').textContent = `${Math.round(data.summary.max_temp)}°C`;
  $('avgGpu').textContent = `${Number(data.summary.avg_gpu).toFixed(1)} W`;
  $('epp').textContent = `${data.system.governor} (${data.system.epp})`;
  $('reportPath').textContent = data.report.latestExists ? data.report.path : 'No report generated yet';

  // Chart & History Table
  const hChanged = !_prevData || !_prevData.history || data.history.length !== _prevData.history.length || (data.history.length > 0 && data.history[data.history.length-1].iso !== _prevData.history[_prevData.history.length-1].iso);
  if (hChanged) {
    drawChart(data.history);
    // Profile switch log
    const switchLog = $('profileSwitchLog');
    if (switchLog && data.profileSwitches && data.profileSwitches.length) {
      const profileLabels = {performance: 'Boost', balanced: 'Balanced', 'power-saver': 'Eco'};
      const profileDots = {performance: C.boost, balanced: C.powersave, 'power-saver': C.silent};
      switchLog.innerHTML = data.profileSwitches.slice().reverse().map(s => {
        const label = esc(profileLabels[s.profile] || s.profile);
        const color = profileDots[s.profile] || C.textMuted;
        const time = esc(s.iso ? s.iso.split('T')[1].substring(0,5) : '—');
        return `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:12px;color:${C.textMuted}"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color}"></span>${time} → <strong style="color:${C.textMain}">${label}</strong></span>`;
      }).join('');
    } else if (switchLog) {
      switchLog.innerHTML = `<span style="color:${C.textFaint};font-size:12px">No profile changes in current window</span>`;
    }
    $('history').innerHTML = data.history.slice().reverse().map(r => `
      <tr>
        <td>${esc(r.iso ? r.iso.split('T')[1].substring(0,8) : '—')}</td>
        <td style="text-transform:capitalize">${esc({performance:'Boost',balanced:'Balanced','power-saver':'Silent'}[r.profile]||r.profile)}</td>
        <td><strong>${esc(r.cpu_load||0)}%</strong></td>
        <td style="color:${tempColor(parseInt(r.cpu_temp||0))}">${esc(r.cpu_temp||0)}°C</td>
        <td>${esc(r.gpu_temp||0)}°C / ${esc(r.gpu_power||0)}W</td>
        <td>${esc(r.pl1||0)}/${esc(r.pl2||0)}W</td>
      </tr>`).join('');
  }

  // Active profile highlight
  ['boost','powersave','silent'].forEach(a => { const b = $(`btn-${a}`); if(b) b.classList.remove('active-preset'); });
  if (data.profile === 'performance') $('btn-boost')?.classList.add('active-preset');
  else if (data.profile === 'balanced') $('btn-powersave')?.classList.add('active-preset');
  else if (data.profile === 'power-saver') $('btn-silent')?.classList.add('active-preset');

  // Active auto mode highlight
  ['dynamic','gaming','creator','quiet','off'].forEach(m => { const b = $(`mode-${m}`); if(b) b.classList.remove('active-preset'); });
  $(`mode-${data.auto.mode}`)?.classList.add('active-preset');

  // Summer nights
  if (data.auto.summerSilentNights === 'yes') {
    $('summer-nights-on').classList.add('active-preset');
    $('summer-nights-off').classList.remove('active-preset');
  } else {
    $('summer-nights-on').classList.remove('active-preset');
    $('summer-nights-off').classList.add('active-preset');
  }

  // Component temps, fan engine, GPU limit, interlock
  renderSensors(data);
  renderInterlock(data);
  renderFans(data);
  renderGpuLimit(data);

  // Modes table
  const mChanged = !_prevData || _prevData.auto.mode !== data.auto.mode || JSON.stringify(_prevData.auto.modes) !== JSON.stringify(data.auto.modes);
  if (mChanged) {
    $('modes').innerHTML = data.auto.modes.map(m => `
      <tr class="${data.auto.mode === m.mode ? 'active-preset' : ''}">
        <td style="font-weight:600;text-transform:capitalize">${esc(m.mode)}</td>
        <td>${esc(m.tempHot)}°C</td><td>${esc(m.tempCritical)}°C</td><td>${esc(m.boostTempLimit)}°C</td>
        <td>${esc(m.loadHigh)}% / ${esc(secondsText(m.loadHighDuration))}</td>
        <td>${esc(m.loadIdle)}% / ${esc(secondsText(m.loadIdleDuration))}</td>
        <td>${esc(secondsText(m.promptCooldown))}</td>
      </tr>`).join('');
  }

  _prevData = data;
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
  } catch (e) {
    _failCount++;
    if (_failCount >= 3) $('connBar').classList.add('show');
  }
}

async function sendAction(action, value = null) {
  // Optimistic UI Updates
  if (['boost', 'powersave', 'silent', 'restore'].includes(action)) {
    ['boost', 'powersave', 'silent'].forEach(a => { const b = $(`btn-${a}`); if(b) b.classList.remove('active-preset'); });
    if (action !== 'restore') {
      $(`btn-${action}`)?.classList.add('active-preset');
      const profileNames = {boost: 'Performance', powersave: 'Balanced', silent: 'Power-Saver'};
      if ($('profile')) $('profile').textContent = profileNames[action] || action;
    }
  } else if (action === 'auto-mode') {
    ['dynamic','gaming','creator','quiet','off'].forEach(m => { const b = $(`mode-${m}`); if(b) b.classList.remove('active-preset'); });
    $(`mode-${value}`)?.classList.add('active-preset');
    if ($('autoMode')) $('autoMode').textContent = value;
    // Also update table row highlight
    document.querySelectorAll('#modes tr').forEach(tr => tr.classList.remove('active-preset'));
    const matchedRow = Array.from(document.querySelectorAll('#modes tr')).find(tr => tr.firstElementChild?.textContent?.toLowerCase() === value);
    if (matchedRow) matchedRow.classList.add('active-preset');
  } else if (action === 'snooze') {
    if ($('pauseState')) $('pauseState').textContent = '⏸ Snoozed';
  } else if (action === 'today-off') {
    if ($('pauseState')) $('pauseState').textContent = '⏸ Today off';
  } else if (action === 'resume') {
    if ($('pauseState')) $('pauseState').textContent = '✅ Available';
  } else if (action === 'summer-nights') {
    if (value === 'on') {
      $('summer-nights-on')?.classList.add('active-preset');
      $('summer-nights-off')?.classList.remove('active-preset');
      if ($('summerNights')) $('summerNights').textContent = 'YES';
    } else {
      $('summer-nights-on')?.classList.remove('active-preset');
      $('summer-nights-off')?.classList.add('active-preset');
      if ($('summerNights')) $('summerNights').textContent = 'NO';
    }
  }

  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, value})
    });
    const result = await r.json();
    showToast(result.message || (result.ok ? 'Applied' : 'Error'), !result.ok);
    await refresh();
  } catch (e) { showToast(e.message, true); }
}

// Config UI
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.ok) return;
    const cfg = data.config;
    for (const [key, val] of Object.entries(cfg)) {
      const el = document.getElementById('cfg_' + key);
      if (el) {
        if (el.tagName === 'SELECT') {
          el.value = val;
        } else {
          el.value = val;
        }
      }
    }
  } catch (e) { /* silent */ }
}

$('saveConfigBtn')?.addEventListener('click', async () => {
  const updates = {};
  const fields = ['TEMP_CRITICAL','TEMP_HOT','BOOST_TEMP_LIMIT','LOAD_HIGH','LOAD_HIGH_DURATION','LOAD_IDLE','LOAD_IDLE_DURATION','PROMPT_COOLDOWN','POLL_INTERVAL','STATS_INTERVAL','ALLOW_CRITICAL_AUTO','AC_PROFILE','BATTERY_PROFILE','BATTERY_LOW_PCT','BATTERY_CRITICAL_PCT','BATTERY_LOW_NOTIFY','BOOST_EPP','BOOST_PL1_PCT','BOOST_PL2_PCT','SCREEN_LOCK_POWERSAVE','BATTERY_CHARGE_LIMIT','SLOW_CHARGE_THRESHOLD_W','SLOW_CHARGE_BATTERY_PCT','SLOW_CHARGE_RECOVERY_PCT'];
  for (const key of fields) {
    const el = document.getElementById('cfg_' + key);
    if (el) updates[key] = el.value;
  }
  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'save-config', value: JSON.stringify(updates)})
    });
    const result = await r.json();
    showToast(result.message || (result.ok ? 'Configuration saved' : 'Error'), !result.ok);
  } catch (e) { showToast(e.message, true); }
});

// Load config on startup
loadConfig();

// Event delegation
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  e.preventDefault();
  sendAction(btn.dataset.action, btn.dataset.value || null);
});

$('saveQuiet').addEventListener('click', () => {
  sendAction('quiet-hours', JSON.stringify({start: $('quietStart').value, end: $('quietEnd').value}));
});

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const key = e.key.toLowerCase();
  if (key === '1') sendAction('boost');
  else if (key === '2') sendAction('powersave');
  else if (key === '3') sendAction('silent');
  else if (key === '4') sendAction('restore');
  else if (key === 'r') refresh();
});

// ── Live updates ──────────────────────────────────────────────────────
// The server pushes a payload whenever the daemon's snapshot changes, so an
// idle machine costs no periodic wake-ups in either the browser or the
// server. Polling stays as the fallback for browsers without EventSource
// and for a stream that drops.
let _stream = null;
let _pollTimer = null;

function startPolling() {
  if (_pollTimer) return;
  _pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, 3000);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

function startStream() {
  if (!window.EventSource) { startPolling(); return; }
  _stream = new EventSource('/api/stream');
  _stream.onopen = () => { stopPolling(); };
  _stream.onmessage = event => {
    try { render(JSON.parse(event.data)); } catch (e) { /* keep the stream alive */ }
  };
  _stream.onerror = () => {
    _stream.close();
    _stream = null;
    startPolling();
    setTimeout(() => { if (!_stream) startStream(); }, 15000);
  };
}

refresh();
startStream();
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
