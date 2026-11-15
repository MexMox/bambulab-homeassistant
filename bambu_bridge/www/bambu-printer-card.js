/*
 * MexMox Bambu Printer Card – eigene Lovelace-Karte für die MexMox Bambu Bridge
 * Installation: /config/www/bambu/bambu-printer-card.js
 * Ressource:    /local/bambu/bambu-printer-card.js  (JavaScript-Modul)
 *
 * type: custom:bambu-printer-card
 * entity: sensor.bambu_p1s_status     # Statussensor der Bridge (Pflicht)
 * camera: camera.bambu_p1s_camera     # optional – wird sonst automatisch gefunden
 * show_camera: true                   # optional
 * show_fans: true                     # optional
 * show_ams: true                      # optional
 * show_controls: true                 # optional
 * camera_refresh: 2                   # optional, Sekunden
 * full_width: true                    # optional – volle Breite in der Sections-Ansicht
 * show_last_print: true               # optional – bei ausgeschaltetem Drucker Bild vom letzten Druck zeigen
 * last_print_camera: camera.bambu_p1s_last_print   # optional – wird sonst automatisch gefunden
 * offline_image: preview              # optional – preview (3D-Vorschau aus dem Slicer) oder camera (Screenshot)
 * preview_camera: camera.bambu_p1s_preview         # optional – wird sonst automatisch gefunden
 */
const CARD_VERSION = '1.5.0';
const SPEEDS = [[1, 'Leise'], [2, 'Standard'], [3, 'Sport'], [4, 'Ludicrous']];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtMin = m => { if (!m) return '–'; const h = Math.floor(m / 60), mm = m % 60; return h ? `${h} h ${String(mm).padStart(2, '0')} min` : `${mm} min`; };
const temp = v => v == null ? '–' : (Math.round(v * 10) / 10).toLocaleString('de-DE') + '°';
function fmtEta(iso) {
  if (!iso) return '–';
  const d = new Date(iso), now = new Date();
  const t = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 864e5);
  return days === 0 ? t : days === 1 ? 'morgen ' + t : d.toLocaleDateString('de-DE', { weekday: 'short' }) + ' ' + t;
}

const STYLE = `
:host{--bp-accent:#22c55e;--bp-accent2:#16a34a;--bp-warn:#f59e0b;--bp-err:#ef4444;--bp-info:#3b82f6;
  --bp-text:var(--primary-text-color,#e8eaf0);--bp-muted:var(--secondary-text-color,#8b93a5);
  --bp-line:var(--divider-color,rgba(127,127,127,.2));--bp-tile:rgba(127,127,127,.1);display:block}
ha-card{overflow:hidden;container-type:inline-size;color:var(--bp-text)}
button{font:inherit;color:inherit;cursor:pointer;border:0;background:none}
.head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--bp-line);flex-wrap:wrap;cursor:pointer}
.head h2{margin:0;font-size:16px;font-weight:650;line-height:1.2}
.head .model{color:var(--bp-muted);font-size:12px}
.pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;background:var(--bp-tile);color:var(--bp-muted)}
.pill.run{background:rgba(34,197,94,.15);color:var(--bp-accent)}
.pill.pause{background:rgba(245,158,11,.15);color:var(--bp-warn)}
.pill.err{background:rgba(239,68,68,.15);color:var(--bp-err)}
.pill.done{background:rgba(59,130,246,.15);color:var(--bp-info)}
.meta{margin-left:auto;color:var(--bp-muted);font-size:12px;display:flex;gap:12px}
.body{display:grid;grid-template-columns:1fr}
.colA,.colB{display:grid;gap:14px;align-content:start}
@container (min-width:620px){.body{grid-template-columns:minmax(0,1.2fr) minmax(0,1fr)}.body .cam{aspect-ratio:auto;min-height:260px}}
@container (min-width:620px){.amsBox .ams{grid-template-columns:repeat(4,minmax(0,1fr))}}
@container (min-width:1100px){
  .body{grid-template-columns:minmax(0,1fr) minmax(0,1.5fr)}
  .body .cam{min-height:280px}
  .side{grid-template-columns:1fr 1fr;column-gap:20px;padding:20px}
  .amsBox{padding:16px 20px}
}
@container (min-width:1600px){.ring{width:120px;height:120px}}
.cam{position:relative;background:#000;aspect-ratio:16/9;display:grid;place-items:center;overflow:hidden}
.cam img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:none;cursor:zoom-in}
.cam img.on{display:block}
.cam .noimg{color:#8b93a5;font-size:13px}
.ovl{position:absolute;right:8px;bottom:8px;display:flex;gap:6px}
.ovl button{background:rgba(0,0,0,.6);backdrop-filter:blur(4px);color:#fff;padding:6px 10px;border-radius:9px;font-size:12.5px}
.ovl button.lit{color:#fde047}
.ovl button:disabled{opacity:.5;cursor:default}
.side{padding:16px;display:grid;gap:14px;align-content:start}
.job{display:flex;gap:16px;align-items:center}
.ring{position:relative;width:104px;height:104px;flex:none}
.ring svg{transform:rotate(-90deg);display:block}
.ring .val{position:absolute;inset:0;display:grid;place-items:center;font-size:24px;font-weight:700}
.ring .val small{font-size:12px;color:var(--bp-muted);font-weight:500}
.jobinfo{min-width:0;display:grid;gap:5px}
.file{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kv{color:var(--bp-muted);font-size:13px}
.kv b{color:var(--bp-text);font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.tile{background:var(--bp-tile);border-radius:12px;padding:10px 12px}
.tile .lbl{color:var(--bp-muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.tile .big{font-size:20px;font-weight:700}
.tile .big small{font-size:12px;color:var(--bp-muted);font-weight:500}
.tile.hot .big{color:#fb923c}
.nz{display:block;font-size:17px;line-height:1.3;white-space:nowrap}
.nz small{font-size:11px;color:var(--bp-muted);font-weight:600;margin-right:4px}
.fans{display:grid;gap:6px}
.fan{display:grid;grid-template-columns:100px 1fr 38px;align-items:center;gap:8px;font-size:12.5px;color:var(--bp-muted)}
.bar{height:6px;background:var(--bp-tile);border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--bp-info);border-radius:99px;transition:width .5s}
.fan span:last-child{text-align:right;color:var(--bp-text)}
.controls{display:grid;gap:8px}
.seg{display:grid;grid-template-columns:repeat(4,1fr);background:var(--bp-tile);border-radius:11px;padding:3px;gap:3px}
.seg button{padding:7px 2px;border-radius:8px;font-size:12.5px;color:var(--bp-muted)}
.seg button.on{background:var(--bp-accent);color:#04120a;font-weight:650}
.seg button:disabled:not(.on){opacity:.4;cursor:default}
.btns{display:grid;grid-template-columns:1fr 1fr auto;gap:8px}
.btn{padding:10px;border-radius:11px;background:var(--bp-tile);font-weight:600;display:flex;align-items:center;justify-content:center}
.btn:disabled{opacity:.4;cursor:default}
.btn.primary{background:var(--bp-accent);color:#04120a}
.btn.danger{background:rgba(239,68,68,.15);color:var(--bp-err)}
.btn.danger.confirm{background:var(--bp-err);color:#fff}
.section{padding:14px 16px;border-top:1px solid var(--bp-line)}
.section h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--bp-muted);display:flex;gap:10px}
.section h3 .sub{text-transform:none;letter-spacing:0;font-weight:500}
.ams{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.slot{background:var(--bp-tile);border-radius:11px;padding:10px;display:grid;grid-template-columns:auto 1fr;gap:3px 10px;align-items:center;border:2px solid transparent}
.slot.active{border-color:var(--bp-accent)}
.sw{width:30px;height:30px;align-self:center;border-radius:50%;grid-row:1/3;border:2px solid rgba(127,127,127,.3)}
.sw.none{background:repeating-linear-gradient(45deg,transparent 0 4px,var(--bp-line) 4px 8px);border-style:dashed}
.slot .t{font-weight:650;font-size:13.5px}
.slot .b{color:var(--bp-muted);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.slot .bar{grid-column:span 2;margin-top:5px}
.slot .bar i{background:var(--bp-accent)}
.hms{display:grid;gap:6px}
.hms a{padding:9px 11px;background:rgba(239,68,68,.1);color:var(--bp-err);border-radius:10px;text-decoration:none;font-size:13px}
.offline .side,.offline .section{opacity:.45}
.lastbadge{position:absolute;left:8px;top:8px;right:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;pointer-events:none}
.lastbadge span{background:rgba(0,0,0,.65);backdrop-filter:blur(4px);color:#fff;padding:5px 10px;border-radius:9px;font-size:12.5px;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lastbadge .res{font-weight:650}
.lastbadge .res.ok{color:#4ade80}.lastbadge .res.bad{color:#f87171}.lastbadge .res.mid{color:#fbbf24}
.cam.last img{filter:saturate(.85)}
.cam.preview{background:radial-gradient(ellipse at 50% 60%,#3a3f4a 0%,#1d2027 70%)}
.cam.preview img{padding:46px 16px 12px;filter:drop-shadow(0 10px 18px rgba(0,0,0,.45))}
.lastbadge .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px;border:1px solid rgba(255,255,255,.4)}
.warn{padding:16px;color:var(--bp-err)}
`;

class BambuPrinterCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error('Bitte "entity" angeben, z. B. sensor.bambu_p1s_status');
    this._config = { show_camera: true, show_fans: true, show_ams: true, show_controls: true, camera_refresh: 2, full_width: false, show_last_print: true, offline_image: 'preview', ...config };
    if (this._root) this._build();
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass?.states || {}).find(e => e.startsWith('sensor.') && hass.states[e].attributes.bambu_bridge);
    return { entity: id || 'sensor.bambu_p1s_status' };
  }

  getCardSize() { return this._config?.show_camera ? 9 : 6; }
  getGridOptions() {
    // Sections-Ansicht: "full" = Karte über die komplette Breite der Ansicht
    return this._config?.full_width ? { columns: 'full', rows: 'auto' } : { columns: 12, min_columns: 6, rows: 'auto' };
  }

  connectedCallback() {
    clearInterval(this._camTimer); clearInterval(this._etaTimer);
    this._camTimer = setInterval(() => this._refreshCamera(), Math.max(1, this._config?.camera_refresh || 2) * 1000);
    this._etaTimer = setInterval(() => this._update(), 30000);
  }
  disconnectedCallback() { clearInterval(this._camTimer); clearInterval(this._etaTimer); }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) { this._root = this.attachShadow({ mode: 'open' }); this._build(); }
    const st = hass.states[this._config.entity];
    const camId = this._cameraId();
    const cam = camId ? hass.states[camId] : undefined;
    const lpId = this._lastPrintId(), pvId = this._previewId();
    const lp = lpId ? hass.states[lpId] : undefined;
    const pv = pvId ? hass.states[pvId] : undefined;
    if (st === this._lastState && cam === this._lastCam && lp === this._lastLp && pv === this._lastPv) return;   // nur bei Änderungen rendern
    this._lastState = st; this._lastCam = cam; this._lastLp = lp; this._lastPv = pv;
    this._update();
    this._refreshCamera();
  }

  _lastPrintId() { return this._roleId('last_print', this._config.last_print_camera); }
  _previewId() { return this._roleId('preview', this._config.preview_camera); }

  _roleId(role, override) {
    if (override) return override;
    const states = this._hass?.states || {};
    const serial = states[this._config.entity]?.attributes?.serial || this._serial;
    const m = /^sensor\.bambu_(.+)_status$/.exec(this._config.entity);
    const slug = m && m[1];
    return Object.keys(states).find(id => {
      const a = id.startsWith('camera.') && states[id].attributes;
      return a && a.bambu_role === role && ((serial && a.bambu_serial === serial) || (slug && a.bambu_slug === slug));
    });
  }

  _cameraId() {
    if (this._config.camera) return this._config.camera;
    const serial = this._hass?.states[this._config.entity]?.attributes?.serial || this._serial;
    if (!serial) return undefined;
    this._serial = serial;
    return Object.keys(this._hass.states).find(id => id.startsWith('camera.') && this._hass.states[id].attributes.bambu_serial === serial
      && (this._hass.states[id].attributes.bambu_role || 'live') === 'live');
  }

  _build() {
    const c = this._config;
    this._root.innerHTML = `<style>${STYLE}</style>
    <ha-card>
      <div class="head" data-a="more">
        <div><h2 data-f="name">Drucker</h2><div class="model" data-f="model"></div></div>
        <span class="pill" data-f="pill">–</span>
        <div class="meta"><span data-f="wifi"></span><span data-f="filament"></span></div>
      </div>
      <div class="body">
        ${c.show_camera ? `
        <div class="cam" data-f="cam">
          <img alt="Kamerabild" data-f="img" data-a="more-cam">
          <div class="noimg" data-f="noimg">Kein Kamerabild</div>
          <div class="lastbadge" data-f="lastBadge" hidden></div>
          <div class="ovl"><button data-a="light">💡 Licht</button></div>
        </div>` : ''}
        <div class="side">
          <div class="colA">
          <div class="job">
            <div class="ring">
              <svg width="104" height="104" viewBox="0 0 104 104">
                <circle cx="52" cy="52" r="45" stroke="var(--bp-tile)" stroke-width="9" fill="none"/>
                <circle data-f="arc" cx="52" cy="52" r="45" stroke="var(--bp-accent)" stroke-width="9" fill="none" stroke-linecap="round" stroke-dasharray="282.7" stroke-dashoffset="282.7" style="transition:stroke-dashoffset .6s"/>
              </svg>
              <div class="val"><div><span data-f="pct">0</span><small>%</small></div></div>
            </div>
            <div class="jobinfo">
              <div class="file" data-f="file">Kein Druckauftrag</div>
              <div class="kv">Schicht <b data-f="layer">–</b></div>
              <div class="kv">Restzeit <b data-f="remain">–</b></div>
              <div class="kv">Fertig um <b data-f="eta">–</b></div>
            </div>
          </div>
          <div class="tiles">
            <div class="tile" data-f="tNozzle"><div class="lbl">Düse</div><div class="big"><span data-f="nozzle">–</span><small data-f="nozzleT"></small></div></div>
            <div class="tile" data-f="tBed"><div class="lbl">Druckbett</div><div class="big"><span data-f="bed">–</span><small data-f="bedT"></small></div></div>
            <div class="tile" data-f="tChamber" hidden><div class="lbl">Bauraum</div><div class="big"><span data-f="chamber">–</span></div></div>
          </div>
          </div>
          <div class="colB">
          ${c.show_fans ? `
          <div class="fans">
            <div class="fan"><span>Bauteillüfter</span><div class="bar"><i data-f="fPart"></i></div><span data-f="fPartV">–</span></div>
            <div class="fan"><span>Hilfslüfter</span><div class="bar"><i data-f="fAux"></i></div><span data-f="fAuxV">–</span></div>
            <div class="fan"><span>Kammerlüfter</span><div class="bar"><i data-f="fCh"></i></div><span data-f="fChV">–</span></div>
          </div>` : ''}
          ${c.show_controls ? `
          <div class="controls">
            <div class="seg">${SPEEDS.map(([l, n]) => `<button data-a="speed" data-v="${l}">${n}</button>`).join('')}</div>
            <div class="btns">
              <button class="btn primary" data-a="pauseResume">❚❚ Pausieren</button>
              <button class="btn danger" data-a="stop">Stoppen</button>
              <button class="btn" data-a="refresh" title="Status neu laden">⟳</button>
            </div>
          </div>` : ''}
          </div>
        </div>
      </div>
      ${c.show_ams ? `<div class="section amsBox" data-f="amsSec" hidden><h3>AMS <span class="sub" data-f="amsEnv"></span></h3><div class="ams" data-f="ams"></div></div>` : ''}
      <div class="section" data-f="hmsSec" hidden><h3>Meldungen</h3><div class="hms" data-f="hms"></div></div>
    </ha-card>`;
    this._root.querySelector('ha-card').addEventListener('click', ev => this._onAction(ev));
    this._lastState = undefined;
    if (this._hass) this._update();
  }

  _f(n) { return this._root.querySelector(`[data-f="${n}"]`); }
  _set(n, prop, val) { const el = this._f(n); if (el) el[prop] = val; }

  async _cmd(action, payload = 'PRESS', ok) {
    const d = this._data; if (!d?.cmd_topic) return;
    try {
      await this._hass.callService('mqtt', 'publish', { topic: `${d.cmd_topic}/${action}`, payload: String(payload) });
      this._toast(ok || 'Befehl gesendet');
    } catch (e) { this._toast('Fehler: ' + (e.message || e)); }
  }

  _toast(message) {
    this.dispatchEvent(new CustomEvent('hass-notification', { detail: { message }, bubbles: true, composed: true }));
  }

  _moreInfo(entityId) {
    this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId }, bubbles: true, composed: true }));
  }

  _onAction(ev) {
    const b = ev.composedPath().find(el => el.dataset && el.dataset.a);
    if (!b || b.disabled) return;
    const d = this._data || {}, a = b.dataset.a;
    if (a === 'light') this._cmd('light', d.light === 'on' ? 'OFF' : 'ON', d.light === 'on' ? 'Licht aus' : 'Licht an');
    else if (a === 'speed') this._cmd('speed', b.dataset.v, 'Geschwindigkeit: ' + SPEEDS[b.dataset.v - 1][1]);
    else if (a === 'refresh') this._cmd('refresh', 'PRESS', 'Status wird aktualisiert');
    else if (a === 'pauseResume') d.gcode_state === 'PAUSE' ? this._cmd('resume', 'PRESS', 'Druck wird fortgesetzt') : this._cmd('pause', 'PRESS', 'Druck wird pausiert');
    else if (a === 'more') this._moreInfo(this._config.entity);
    else if (a === 'more-cam') { const id = this._showingId || this._cameraId(); if (id) this._moreInfo(id); }
    else if (a === 'stop') {
      if (!b.classList.contains('confirm')) {
        b.classList.add('confirm'); b.textContent = 'Wirklich stoppen?';
        clearTimeout(b._h); b._h = setTimeout(() => { b.classList.remove('confirm'); b.textContent = 'Stoppen'; }, 4000);
      } else {
        clearTimeout(b._h); b.classList.remove('confirm'); b.textContent = 'Stoppen';
        this._cmd('stop', 'PRESS', 'Druck wird gestoppt');
      }
    }
  }

  _update() {
    if (!this._hass || !this._root) return;
    const card = this._root.querySelector('ha-card');
    const st = this._hass.states[this._config.entity];
    if (!st) {
      card.innerHTML = `<div class="warn">Entität <b>${esc(this._config.entity)}</b> nicht gefunden – läuft die Bambu Bridge?</div>`;
      card.dataset.broken = '1';
      return;
    }
    if (card.dataset.broken) { this._build(); return; }
    if (st.attributes.bambu_bridge) this._data = { ...st.attributes };
    const d = this._data || {};
    const available = st.state !== 'unavailable';
    const gs = d.gcode_state || 'UNKNOWN';
    card.classList.toggle('offline', !available);

    const lpState = this._lastPrintId() && this._hass.states[this._lastPrintId()];
    this._set('name', 'textContent', d.name || lpState?.attributes?.printer_name || st.attributes.friendly_name || 'Drucker');
    this._set('model', 'textContent', [d.model, d.serial].filter(Boolean).join(' · '));
    const pill = this._f('pill');
    pill.textContent = available ? (d.status || '–') + ((d.hms || []).length ? ' ⚠' : '') : 'Offline';
    pill.className = 'pill ' + (!available || gs === 'FAILED' ? 'err'
      : gs === 'RUNNING' || gs === 'PREPARE' ? 'run' : gs === 'PAUSE' ? 'pause' : gs === 'FINISH' ? 'done' : '');
    this._set('wifi', 'textContent', d.wifi != null ? `📶 ${d.wifi} dBm` : '');
    this._set('filament', 'textContent', d.active_filament && d.active_filament !== '—' ? '🧵 ' + d.active_filament : '');

    const pct = Math.max(0, Math.min(100, d.progress || 0));
    this._set('pct', 'textContent', pct);
    const arc = this._f('arc');
    arc.style.strokeDashoffset = 282.7 * (1 - pct / 100);
    arc.style.stroke = gs === 'PAUSE' ? 'var(--bp-warn)' : gs === 'FAILED' ? 'var(--bp-err)' : gs === 'FINISH' ? 'var(--bp-info)' : 'var(--bp-accent)';
    this._set('file', 'textContent', d.file || 'Kein Druckauftrag');
    this._set('file', 'title', d.file || '');
    this._set('layer', 'textContent', d.total_layers ? `${d.layer ?? 0} / ${d.total_layers}` : '–');
    this._set('remain', 'textContent', d.printing ? fmtMin(d.remaining_min) : '–');
    this._set('eta', 'textContent', d.printing ? fmtEta(d.end_time) : '–');

    this._set('nozzle', 'textContent', temp(d.nozzle_temp));
    this._set('nozzleT', 'textContent', d.nozzle_target ? ` / ${d.nozzle_target}°` : '');
    if ((d.nozzles || []).length > 1) {   // H2D/H2C: zwei Düsen (0 = rechts, 1 = links)
      this._f('nozzle').innerHTML = d.nozzles.map((n, i) =>
        `<span class="nz"><small>${i === 0 ? 'R' : 'L'}</small>${esc(temp(n.temp))}${n.target ? `<small> / ${esc(n.target)}°</small>` : ''}</span>`).join('');
      this._set('nozzleT', 'textContent', '');
    }
    this._f('tChamber').hidden = d.chamber_temp == null;
    this._set('chamber', 'textContent', temp(d.chamber_temp));
    this._set('bed', 'textContent', temp(d.bed_temp));
    this._set('bedT', 'textContent', d.bed_target ? ` / ${d.bed_target}°` : '');
    this._f('tNozzle').classList.toggle('hot', (d.nozzle_temp || 0) > 50);
    this._f('tBed').classList.toggle('hot', (d.bed_temp || 0) > 45);

    if (this._config.show_fans) for (const [k, n] of [['fan_part', 'fPart'], ['fan_aux', 'fAux'], ['fan_chamber', 'fCh']]) {
      this._f(n).style.width = (d[k] || 0) + '%';
      this._set(n + 'V', 'textContent', d[k] == null ? '–' : d[k] + '%');
    }

    const active = ['RUNNING', 'PREPARE', 'PAUSE'].includes(gs);
    this._root.querySelectorAll('[data-a="speed"]').forEach(b => { b.classList.toggle('on', +b.dataset.v === d.speed_level); b.disabled = !available || !active; });
    const pr = this._root.querySelector('[data-a="pauseResume"]');
    if (pr) { pr.textContent = gs === 'PAUSE' ? '▶ Fortsetzen' : '❚❚ Pausieren'; pr.disabled = !available || !active; }
    const sb = this._root.querySelector('[data-a="stop"]'); if (sb) sb.disabled = !available || !active;
    const rb = this._root.querySelector('[data-a="refresh"]'); if (rb) rb.disabled = !available;
    const lb = this._root.querySelector('[data-a="light"]');
    if (lb) { lb.classList.toggle('lit', d.light === 'on'); lb.disabled = !available; lb.hidden = !available; }

    if (this._config.show_ams) {
      const units = d.ams || [], slots = [];
      units.forEach(u => (u.trays || []).forEach(t => slots.push({ ...t,
        label: units.length > 1 || u.id >= 128 ? `${u.label || 'AMS ' + (u.id + 1)} · Slot ${t.slot}` : `Slot ${t.slot}` })));
      if (d.external_spool && !d.external_spool.empty) slots.push({ ...d.external_spool, label: 'Extern' });
      this._f('amsSec').hidden = !slots.length;
      const u0 = units[0];
      // 0 = kein Messwert (z. B. AMS Lite / ältere Firmware) -> ausblenden
      this._set('amsEnv', 'textContent', u0 ? [u0.humidity > 0 ? `💧 ${u0.humidity} %` : u0.humidity_level > 0 ? `💧 Stufe ${u0.humidity_level}/5` : '',
        u0.temp > 0 ? `🌡 ${temp(u0.temp)}` : ''].filter(Boolean).join('   ') : '');
      const html = slots.map(t => `
        <div class="slot${t.active ? ' active' : ''}" title="${esc(t.label)}">
          <div class="sw${t.empty ? ' none' : ''}" style="${t.color && !t.empty ? 'background:' + esc(t.color) : ''}"></div>
          <div class="t">${t.empty ? 'Leer' : esc(t.type)}</div>
          <div class="b">${esc(t.label)}${t.brand ? ' · ' + esc(t.brand) : ''}</div>
          ${t.remain != null && !t.empty && t.index !== 254 ? `<div class="bar"><i style="width:${+t.remain}%"></i></div>` : ''}
        </div>`).join('');
      const ams = this._f('ams'); if (ams._html !== html) { ams.innerHTML = html; ams._html = html; }
    }

    const errs = (d.hms || []).map(h => `<a href="${esc(h.url)}" target="_blank" rel="noopener">⚠️ HMS ${esc(h.code)} – im Bambu-Wiki öffnen</a>`);
    if (d.print_error) errs.push(`<a href="https://wiki.bambulab.com" target="_blank" rel="noopener">⚠️ Druckfehler ${esc(d.print_error)}</a>`);
    this._f('hmsSec').hidden = !errs.length;
    const hh = errs.join(''), hms = this._f('hms'); if (hms._html !== hh) { hms.innerHTML = hh; hms._html = hh; }

    if (!this._camStarted) { this._camStarted = true; this._refreshCamera(); }
  }

  _refreshCamera() {
    if (!this._hass || !this._config?.show_camera || !this._root || document.hidden) return;
    const img = this._f('img'), no = this._f('noimg'), badge = this._f('lastBadge'), box = this._f('cam'); if (!img) return;
    const id = this._cameraId(), cam = id && this._hass.states[id];
    const available = this._hass.states[this._config.entity]?.state !== 'unavailable';
    const liveOk = cam && cam.attributes.entity_picture && cam.state !== 'unavailable' && available;

    let src = liveOk ? cam : null;
    this._showingLast = false;
    this._showingId = liveOk ? id : null;
    let mode = null;   // 'preview' | 'shot'
    if (!liveOk && this._config.show_last_print) {
      const ok = s => s && s.state !== 'unavailable' && s.attributes.entity_picture;
      const lpId = this._lastPrintId(), lp = lpId && this._hass.states[lpId];
      const pvId = this._previewId(), pv = pvId && this._hass.states[pvId];
      const order = this._config.offline_image === 'camera' ? [['shot', lp, lpId], ['preview', pv, pvId]] : [['preview', pv, pvId], ['shot', lp, lpId]];
      const hit = order.find(([, s]) => ok(s));
      if (hit) { [mode, src, this._showingId] = hit; this._showingLast = true; }
      this._lpAttrs = lp?.attributes;
    }
    box.classList.toggle('last', mode === 'shot');
    box.classList.toggle('preview', mode === 'preview');
    if (mode) {
      const a = src.attributes;
      // Ergebnis/Zeit vom letzten Druck übernehmen, wenn er zur Vorschau passt
      const lpa = mode === 'shot' ? a : (this._lpAttrs && (!a.file || this._lpAttrs.file === a.file) ? this._lpAttrs : null);
      const parts = [`<span>${mode === 'preview' ? '🧊 3D-Vorschau' : '🕓 Letzter Druck'}</span>`];
      if (lpa?.result) {
        const cls = lpa.result === 'Fertig' ? 'ok' : lpa.result === 'Fehlgeschlagen' ? 'bad' : 'mid';
        parts.push(`<span class="res ${cls}">${esc(lpa.result)}</span>`);
      }
      const whenIso = lpa?.finished_at;
      if (whenIso) parts.push(`<span>${esc(new Date(whenIso).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }))} Uhr</span>`);
      if (a.file) parts.push(`<span title="${esc(a.file)}">${esc(a.file)}</span>`);
      if (lpa?.duration_min) parts.push(`<span>⏱ ${esc(fmtMin(lpa.duration_min))}</span>`);
      else if (a.prediction_min) parts.push(`<span>⏱ ca. ${esc(fmtMin(a.prediction_min))}</span>`);
      if (a.weight_g) parts.push(`<span>⚖ ${esc(String(a.weight_g).replace('.', ','))} g</span>`);
      (a.filaments || []).forEach(fl => parts.push(`<span><i class="dot" style="background:${esc(fl.color || '#888')}"></i>${esc(fl.type || '')}${fl.used_g ? ' · ' + esc(String(Math.round(fl.used_g))) + ' g' : ''}</span>`));
      const html = parts.join('');
      if (badge._html !== html) { badge.innerHTML = html; badge._html = html; }
      badge.hidden = false;
    } else badge.hidden = true;

    if (!src) {
      img.classList.remove('on'); no.style.display = ''; no.textContent = available ? 'Kein Kamerabild' : 'Drucker offline'; return;
    }
    const pic = src.attributes.entity_picture;
    // Bild vom letzten Druck ändert sich selten -> nur neu laden, wenn sich die Quelle ändert
    if (this._showingLast && this._lastSrc === pic && img.classList.contains('on')) return;
    if (this._camLoading) return;
    this._camLoading = true;
    this._lastSrc = this._showingLast ? pic : null;
    const base = typeof this._hass.hassUrl === 'function' ? this._hass.hassUrl(pic) : pic;
    const pre = new Image();
    pre.onload = () => { img.src = pre.src; img.classList.add('on'); no.style.display = 'none'; this._camLoading = false; };
    pre.onerror = () => { this._camLoading = false; this._lastSrc = null; };
    pre.src = base + (pic.includes('?') ? '&' : '?') + '_=' + Date.now();
  }
}

customElements.define('bambu-printer-card', BambuPrinterCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'bambu-printer-card',
  name: 'MexMox Bambu Drucker',
  description: 'Status, Kamera, AMS und Steuerung für die MexMox Bambu Bridge',
  preview: true,
});
console.info(`%c BAMBU-PRINTER-CARD %c v${CARD_VERSION} `, 'background:#22c55e;color:#04120a;font-weight:700', 'background:#1e222b;color:#e8eaf0');
