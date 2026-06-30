'use strict';

// ═══════════════════════════════════════════════════════════════════════════
// FIX (2026-06-28):
// 1. WS onopen → chỉ gửi set_viewport (bbox), KHÔNG gọi requestViewportData()
// 2. Fallback HTTP poll chỉ khi WS hoàn toàn chết
// 3. Không double-render: WS push → applyViewportData, HTTP poll → applyViewportData
//    nhưng không bao giờ cả 2 chạy cùng lúc
// 4. FIX: Đã thêm Debounce cho ResizeObserver để tránh crash trình duyệt
// 5. FIX: Đã sửa logic scheduleDbFlush để nó thực sự chạy mỗi 30s
// ═══════════════════════════════════════════════════════════════════════════

console.log('[TrafficSim] map.js loading...');

const CFG = {
  center:     [106.7009, 10.7769],
  initZoom:   15,
  minZoom:    11,
  maxZoom:    19,
  apiBase:    'http://localhost:8000',
  wsUrl:      'ws://localhost:8000/ws',
  mapTilerKey:'ldlQD22evshx3v4h2VCC',
  maxObj:     600,
  dbFlushMs:  30_000,
  vpDebounce: 1500,
};

function _getClientId() {
  try {
    let id = sessionStorage.getItem('trafficsim_client_id');
    if (!id) { id = 'c-' + Math.random().toString(36).slice(2, 10); sessionStorage.setItem('trafficsim_client_id', id); }
    return id;
  } catch { return 'c-anon'; }
}
const CLIENT_ID = _getClientId();

const STATE = {
  map: null, running: true, speed: 1, zoom: CFG.initZoom,
  layers: { vehicles: true, lights: true, signs: true, pedestrians: true, incidents: true, jams: true },
  markers: { vehicles: new Map(), lights: new Map(), signs: new Map(), pedestrians: new Map(), incidents: new Map(), jams: new Map() },
  ws: null, wsAlive: false,
  popup: null, tlTimer: null, vpTimer: null,
  lastData: null,
  backendOk: true,
  _fallbackPoll: null,
  _mapLoaded: false,
};

// ─── EMOJI / LABEL MAPS ──────────────────────────────────────────────────────
const TYPE_EMOJI  = { car:'🚗', truck:'🚚', bus:'🚌', motorcycle:'🛵', ambulance:'🚑', police:'🚓', firefighter:'🚒' };
const TYPE_LABEL  = { car:'Ô tô', truck:'Xe tải', bus:'Xe buýt', motorcycle:'Xe máy', ambulance:'Xe cứu thương', police:'Cảnh sát', firefighter:'Cứu hỏa' };
const STATE_LABEL = { cruising:'Đang đi', stopped:'Dừng đèn đỏ', braking:'Giảm tốc', yielding:'Nhường đường', overtaking:'Đang vượt xe', accelerating:'Tăng tốc', emergency:'Khẩn cấp', collided:'TAI NẠN', jammed:'KẸT XE' };
const PHASE_LABEL = { red:'ĐÈN ĐỎ 🔴', yellow:'ĐÈN VÀNG 🟡', green:'ĐÈN XANH 🟢' };
const SIGN_LABEL  = { speed_limit:'Giới hạn tốc độ', no_overtaking:'Cấm vượt', no_u_turn:'Cấm quay đầu', bridge_ahead:'Sắp lên cầu', roundabout:'Vòng xuyến', danger_zone:'Khu vực nguy hiểm', stop:'Dừng lại', yield:'Nhường đường', speed:'Giới hạn tốc độ', no_entry:'Cấm vào', pedestrian_crossing:'Vạch sang đường', school_zone:'Khu trường học', construction:'Công trường' };
const SIGN_EMOJI  = { speed_limit:'🔵', no_overtaking:'🚫', no_u_turn:'↩️', bridge_ahead:'🌉', roundabout:'🔄', danger_zone:'⚠️', stop:'🛑', yield:'⚠️', speed:'🔵', no_entry:'⛔', pedestrian_crossing:'🚸', school_zone:'🏫', construction:'🚧' };

function makeMarker(emoji, cls) {
  const el = document.createElement('div');
  el.className = cls; el.textContent = emoji;
  return el;
}

// ─── INJECT CSS ──────────────────────────────────────────────────────────────
(function injectCSS() {
  const s = document.createElement('style');
  s.textContent = `
    .maplibregl-marker { transition: transform 0.6s linear !important; }
    .jam-marker { position: relative; font-size: 20px; cursor: pointer; filter: drop-shadow(0 2px 6px #000c); }
    .jam-count { position: absolute; top: -6px; right: -8px; background: #ef4444; color: #fff; font-size: 9px; font-weight: 700; font-family: 'JetBrains Mono', monospace; padding: 1px 4px; border-radius: 8px; line-height: 14px; }
    #backend-banner { position: absolute; top: 14px; left: 50%; transform: translateX(-50%); z-index: 1200; background: #7f1d1d; color: #fee2e2; padding: 6px 16px; border-radius: 8px; font-size: 12px; display: none; box-shadow: 0 4px 16px #000a; border: 1px solid #ef4444; white-space: nowrap; }
  `;
  document.head.appendChild(s);
})();

// ─── BACKEND STATUS ──────────────────────────────────────────────────────────
function ensureBanner() {
  let b = document.getElementById('backend-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'backend-banner';
    b.textContent = '⚠️ Mất kết nối backend — đang hiển thị dữ liệu DEMO';
    document.getElementById('map-wrap')?.appendChild(b);
  }
  return b;
}
function setBackendStatus(ok) {
  STATE.backendOk = ok;
  ensureBanner().style.display = ok ? 'none' : 'block';
  const dot = document.getElementById('dot-db');
  if (dot) dot.className = ok ? 'dot dot-ok' : 'dot dot-bad';
}

// ─── MAP INIT ────────────────────────────────────────────────────────────────
function initMap() {
  if (typeof maplibregl === 'undefined') { console.error('maplibregl not loaded'); return; }

  STATE.map = new maplibregl.Map({
    container: 'map',
    style: `https://api.maptiler.com/maps/streets-v2-dark/style.json?key=${CFG.mapTilerKey}`,
    center: CFG.center, zoom: CFG.initZoom,
    minZoom: CFG.minZoom, maxZoom: CFG.maxZoom,
    attributionControl: false,
  });

  STATE.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

  STATE.map.on('load', () => {
    STATE._mapLoaded = true;
    STATE.map.resize();
    setupRoadClick();
    if (STATE.wsAlive) {
      sendViewportOverWS();
    } else {
      requestViewportData();
    }
  });

  STATE.map.on('zoom', () => {
    STATE.zoom = Math.round(STATE.map.getZoom());
    const b = document.getElementById('zoom-badge');
    if (b) b.textContent = `Zoom: ${STATE.zoom}`;
  });

  STATE.map.on('moveend', () => {
    if (!STATE._mapLoaded) return;
    clearTimeout(STATE.vpTimer);
    STATE.vpTimer = setTimeout(() => {
      if (STATE.wsAlive) {
        sendViewportOverWS();
      } else {
        requestViewportData();
      }
    }, CFG.vpDebounce);
  });

  // FIX: Chống loop reload trình duyệt bằng cách thêm debounce cho ResizeObserver
  if (window.ResizeObserver) {
    let resizeTimer;
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => STATE.map?.resize(), 100);
    }).observe(document.getElementById('map-wrap'));
  }
}

// ─── GỬI BBOX QUA WS (thay thế HTTP /viewport khi WS alive) ─────────────────
function sendViewportOverWS() {
  if (!STATE.ws || STATE.ws.readyState !== WebSocket.OPEN || !STATE.map) return;
  const b = STATE.map.getBounds();
  STATE.ws.send(JSON.stringify({
    type: 'set_viewport',
    bbox: { s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast() },
  }));
}

// ─── ROAD CLICK ──────────────────────────────────────────────────────────────
function setupRoadClick() {
  STATE.map.on('click', async (e) => {
    const features = STATE.map.queryRenderedFeatures(e.point);
    const road = features.find(f =>
      f.sourceLayer === 'transportation' ||
      f.sourceLayer === 'transportation_name' ||
      ['motorway','primary','secondary','tertiary','residential','service','trunk','path'].includes(f.properties?.class) ||
      f.layer?.type === 'line'
    );
    if (road) await selectRoad(road, e.lngLat);
  });
}

async function selectRoad(feature, lngLat) {
  const p    = feature.properties || {};
  const name = p.name || p['name:vi'] || p['name:en'] || `Đường #${p.osm_id || feature.id || '?'}`;
  const cls  = p.class || p.highway || p.subclass || '?';
  const osmId = String(p.osm_id || feature.id || '');

  let live = { vehicles: '—', flow: '—', hasIssue: false, mlNote: '', congestion: null, avgSpeed: null };
  try {
    const params = new URLSearchParams({ lat: lngLat.lat.toFixed(6), lng: lngLat.lng.toFixed(6) });
    const r = await fetch(`${CFG.apiBase}/road/${encodeURIComponent(osmId)}?${params}`, { signal: AbortSignal.timeout(3000) });
    if (r.ok) { live = await r.json(); setBackendStatus(true); }
    else setBackendStatus(false);
  } catch { setBackendStatus(false); }

  const CONG_COLOR = { free: '#22c55e', moderate: '#f59e0b', heavy: '#ef4444', gridlock: '#7c3aed' };
  const CONG_LABEL = { free: '🟢 Thông thoáng', moderate: '🟡 Bình thường', heavy: '🟠 Đông đúc', gridlock: '🔴 Kẹt cứng' };

  if (STATE.popup) STATE.popup.remove();
  STATE.popup = new maplibregl.Popup({ closeButton: true, maxWidth: '280px' })
    .setLngLat(lngLat)
    .setHTML(`
      <div class="road-popup">
        <h4>${name}</h4>
        <div class="rp-row"><span class="rp-k">Loại đường</span><span class="rp-v">${cls}</span></div>
        <div class="rp-row"><span class="rp-k">Một chiều</span><span class="rp-v">${p.oneway ? 'Có' : 'Không'}</span></div>
        <div class="rp-row"><span class="rp-k">Số làn</span><span class="rp-v">${p.lanes || '?'}</span></div>
        <div class="rp-row"><span class="rp-k">Xe quanh đây</span><span class="rp-v">${live.vehicles ?? '—'}</span></div>
        <div class="rp-row"><span class="rp-k">Tốc độ TB</span><span class="rp-v">${live.avgSpeed != null ? live.avgSpeed + ' km/h' : '—'}</span></div>
        <div class="rp-row"><span class="rp-k">Tắc nghẽn</span>
          <span class="rp-v" style="color:${CONG_COLOR[live.congestion]||'#94a3b8'}">${CONG_LABEL[live.congestion]||'—'}</span>
        </div>
      </div>`)
    .addTo(STATE.map);

  showDetail('road', name, `${cls} • OSM: ${osmId}`, [
    { k: 'Loại đường',    v: cls },
    { k: 'Một chiều',     v: p.oneway ? 'Có' : 'Không' },
    { k: 'Số làn',        v: `${p.lanes || '?'}` },
    { k: 'Xe quanh đây',  v: `${live.vehicles ?? '—'}` },
    { k: 'Tốc độ TB',     v: live.avgSpeed != null ? `${live.avgSpeed} km/h` : '—' },
    { k: 'Tắc nghẽn',     v: CONG_LABEL[live.congestion] || '—', cls: live.hasIssue ? 'bad' : 'ok' },
  ]);
  updateMLDecision(live.mlNote || `Đường "${name}" đang được giám sát AI.`);
  document.getElementById('tl-widget').style.display = 'none';
  document.getElementById('veh-hdr').style.display = 'none';
}

// ─── VIEWPORT HTTP (chỉ dùng khi WS chết) ────────────────────────────────────
async function requestViewportData() {
  if (!STATE.map || !STATE._mapLoaded) return;
  const loader = document.getElementById('vp-loader');
  if (loader) loader.classList.add('show');

  const b = STATE.map.getBounds();
  const params = new URLSearchParams({
    s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast(),
    zoom: Math.round(STATE.map.getZoom()), limit: CFG.maxObj, client_id: CLIENT_ID,
  });
  try {
    const r = await fetch(`${CFG.apiBase}/viewport?${params}`, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) throw new Error('bad');
    const data = await r.json();
    setBackendStatus(true);
    applyViewportData(data);
  } catch {
    setBackendStatus(false);
    applyViewportData(generateMockData());
  } finally {
    if (loader) loader.classList.remove('show');
  }
}

// ─── APPLY VIEWPORT DATA ─────────────────────────────────────────────────────
function applyViewportData(data) {
  if (!data || !STATE.map || !STATE._mapLoaded) return;
  STATE.lastData = data;
  if (STATE.layers.vehicles)    renderVehicles   (data.vehicles    || []);
  if (STATE.layers.lights)      renderLights     (data.lights      || []);
  if (STATE.layers.signs)       renderSigns      (data.signs       || []);
  if (STATE.layers.pedestrians) renderPeds       (data.pedestrians || []);
  if (STATE.layers.incidents)   renderIncidents  (data.incidents   || []);
  renderJamClusters(data.jamClusters || []);
  updateStats(data.stats || {});
  scheduleDbFlush(data);
}

// ─── RENDER VEHICLES ─────────────────────────────────────────────────────────
function renderVehicles(vehicles) {
  const cur = new Set(vehicles.map(v => v.id));
  STATE.markers.vehicles.forEach((m, id) => {
    if (!cur.has(id)) { m.remove(); STATE.markers.vehicles.delete(id); }
  });
  vehicles.forEach(v => {
    const emoji = v.emergency ? (TYPE_EMOJI[v.type] || '🚗') :
                  (v.jam_severity > 0.5 ? '🚙' : (TYPE_EMOJI[v.type] || '🚗'));
    if (STATE.markers.vehicles.has(v.id)) {
      const m = STATE.markers.vehicles.get(v.id);
      m.setLngLat([v.lng, v.lat]);
      m.getElement().textContent = emoji;
    } else {
      const el = makeMarker(emoji, 'veh-marker');
      el.title = `${TYPE_LABEL[v.type]||v.type} • ${v.speed} km/h`;
      el.onclick = ev => { ev.stopPropagation(); selectVehicle(v); };
      STATE.markers.vehicles.set(v.id,
        new maplibregl.Marker({ element: el }).setLngLat([v.lng, v.lat]).addTo(STATE.map));
    }
  });
  const el = document.getElementById('cnt-vehicles');
  if (el) el.textContent = vehicles.length;
}

// ─── RENDER LIGHTS ───────────────────────────────────────────────────────────
function renderLights(lights) {
  const cur = new Set(lights.map(l => l.id));
  STATE.markers.lights.forEach((m, id) => {
    if (!cur.has(id)) { m.remove(); STATE.markers.lights.delete(id); }
  });
  lights.forEach(l => {
    const emoji = l.phase === 'green' ? '🟢' : l.phase === 'yellow' ? '🟡' : '🔴';
    if (STATE.markers.lights.has(l.id)) {
      STATE.markers.lights.get(l.id).getElement().textContent = emoji;
    } else {
      const el = makeMarker(emoji, 'light-marker');
      el.onclick = ev => { ev.stopPropagation(); selectLight(l); };
      STATE.markers.lights.set(l.id,
        new maplibregl.Marker({ element: el }).setLngLat([l.lng, l.lat]).addTo(STATE.map));
    }
  });
  const el = document.getElementById('cnt-lights');
  if (el) el.textContent = lights.length;
}

// ─── RENDER SIGNS ────────────────────────────────────────────────────────────
function renderSigns(signs) {
  const cur = new Set(signs.map(s => s.id));
  STATE.markers.signs.forEach((m, id) => {
    if (!cur.has(id)) { m.remove(); STATE.markers.signs.delete(id); }
  });
  signs.forEach(s => {
    if (!STATE.markers.signs.has(s.id)) {
      const el = makeMarker(SIGN_EMOJI[s.type] || '⚠️', 'sign-marker');
      el.title = SIGN_LABEL[s.type] || s.type;
      el.onclick = ev => { ev.stopPropagation(); selectSign(s); };
      STATE.markers.signs.set(s.id,
        new maplibregl.Marker({ element: el }).setLngLat([s.lng, s.lat]).addTo(STATE.map));
    }
  });
  const el = document.getElementById('cnt-signs');
  if (el) el.textContent = signs.length;
}

// ─── RENDER PEDS ─────────────────────────────────────────────────────────────
function renderPeds(peds) {
  const cur = new Set(peds.map(p => p.id));
  STATE.markers.pedestrians.forEach((m, id) => {
    if (!cur.has(id)) { m.remove(); STATE.markers.pedestrians.delete(id); }
  });
  peds.forEach(p => {
    if (STATE.markers.pedestrians.has(p.id)) {
      STATE.markers.pedestrians.get(p.id).setLngLat([p.lng, p.lat]);
    } else {
      const el = makeMarker(p.state === 'crossing' ? '🏃' : '🚶', 'ped-marker');
      el.onclick = ev => { ev.stopPropagation(); selectPed(p); };
      STATE.markers.pedestrians.set(p.id,
        new maplibregl.Marker({ element: el }).setLngLat([p.lng, p.lat]).addTo(STATE.map));
    }
  });
  const el = document.getElementById('cnt-ped');
  if (el) el.textContent = peds.length;
}

// ─── RENDER INCIDENTS ────────────────────────────────────────────────────────
function renderIncidents(incidents) {
  const cur = new Set(incidents.map(i => i.id));
  STATE.markers.incidents.forEach((m, id) => {
    if (!cur.has(id)) { m.remove(); STATE.markers.incidents.delete(id); }
  });
  incidents.forEach(inc => {
    if (!STATE.markers.incidents.has(inc.id)) {
      const el = makeMarker('🚨', 'inc-marker');
      el.onclick = ev => { ev.stopPropagation(); selectIncident(inc); };
      STATE.markers.incidents.set(inc.id,
        new maplibregl.Marker({ element: el }).setLngLat([inc.lng, inc.lat]).addTo(STATE.map));
    }
  });
  const el = document.getElementById('cnt-inc');
  if (el) el.textContent = incidents.length;
}

// ─── RENDER JAM CLUSTERS ─────────────────────────────────────────────────────
function renderJamClusters(clusters) {
  const cur = new Set(clusters.map(c => c.id));
  STATE.markers.jams.forEach((m, id) => {
    if (!cur.has(id)) { m.remove(); STATE.markers.jams.delete(id); }
  });
  clusters.forEach(c => {
    if (STATE.markers.jams.has(c.id)) {
      STATE.markers.jams.get(c.id).setLngLat([c.lng, c.lat]);
    } else {
      const el = document.createElement('div');
      el.className = 'jam-marker';
      el.innerHTML = `🔥<span class="jam-count">${c.count}</span>`;
      el.title = c.label;
      el.onclick = ev => { ev.stopPropagation(); selectJamCluster(c); };
      STATE.markers.jams.set(c.id,
        new maplibregl.Marker({ element: el }).setLngLat([c.lng, c.lat]).addTo(STATE.map));
    }
  });
}

// ─── SELECTION HANDLERS ──────────────────────────────────────────────────────
function selectVehicle(v) {
  showDetail('vehicle',
    `${TYPE_EMOJI[v.type]||'🚗'} ${TYPE_LABEL[v.type]||v.type} · ${v.id}`,
    `Biển số: ${v.plate||'—'}`,
    [
      { k: 'Tốc độ',     v: `${v.speed} km/h`,              cls: v.speed > 80 ? 'warn' : 'ok' },
      { k: 'Trạng thái', v: STATE_LABEL[v.state]||v.state,  cls: v.state==='collided'?'bad':v.state==='jammed'?'warn':'' },
      { k: 'Mức kẹt xe', v: `${Math.round((v.jam_severity||0)*100)}%`, cls:(v.jam_severity||0)>0.5?'bad':'' },
      { k: 'Gia tốc',    v: `${v.acceleration??'—'} m/s²` },
      { k: 'Kích thước', v: v.size||'—' },
      { k: 'Khẩn cấp',   v: v.emergency?'🚨 Có':'Không',   cls: v.emergency?'warn':'' },
      { k: 'ML',         v: v.ml_controlled?'✓ Có':'Không', cls: v.ml_controlled?'ok':'' },
    ]);
  document.getElementById('tl-widget').style.display = 'none';
  document.getElementById('veh-hdr').style.display = 'none';
  updateMLDecision(`${TYPE_LABEL[v.type]||v.type} ${v.id} đang ${STATE_LABEL[v.state]||v.state} tại ${v.speed} km/h.`);
}

function selectLight(l) {
  const displayName = l.name || `Đèn ${l.id}`;
  showDetail('light', `🚦 ${displayName}`, `ID: ${l.id} • ${l.location_type_vn||''}`, [
    { k: 'Pha hiện tại', v: l.phase_vn || PHASE_LABEL[l.phase] || l.phase },
    { k: 'Đếm ngược',    v: `${l.display_seconds ?? Math.round(l.countdown)}s` },
    { k: 'Chu kỳ đỏ',    v: `${l.cycle?.red??'?'}s` },
    { k: 'Chu kỳ xanh',  v: `${l.cycle?.green??'?'}s` },
    { k: 'Hàng đợi',     v: `${l.queue_length??0} xe` },
    { k: 'ML điều chỉnh',v: l.ml_adjusted?'✓ Có':'Không', cls: l.ml_adjusted?'ok':'' },
    { k: 'Preempted',    v: l.preempted?'🚨 Có':'Không',   cls: l.preempted?'warn':'' },
  ]);
  startTlWidget(l);
  updateMLDecision(`Đèn ${displayName}: ${PHASE_LABEL[l.phase]||l.phase}, còn ${l.display_seconds??Math.round(l.countdown)}s.`);
  renderVehiclePanel(l.nearbyVehicles || []);
}

function selectSign(s) {
  showDetail('sign', `${SIGN_EMOJI[s.type]||'⚠️'} ${SIGN_LABEL[s.type]||s.type}`, `ID: ${s.id}`, [
    { k: 'Loại',    v: SIGN_LABEL[s.type]||s.type },
    { k: 'Giá trị', v: s.value?`${s.value} km/h`:'—' },
    { k: 'Đường',   v: s.road_name||'—' },
    { k: 'Mô tả',   v: s.description||'—' },
  ]);
  document.getElementById('tl-widget').style.display = 'none';
  document.getElementById('veh-hdr').style.display = 'none';
  updateMLDecision(s.description || `Biển "${SIGN_LABEL[s.type]||s.type}" đang hoạt động.`);
}

function selectPed(p) {
  const stateLabel = { walking:'🚶 Đang đi bộ', waiting:'⏳ Chờ qua đường', crossing:'🏃 Đang qua đường' };
  showDetail('pedestrian', '🚶 Người đi bộ', `ID: ${p.id}`, [
    { k: 'Trạng thái', v: stateLabel[p.state]||p.state },
    { k: 'Tốc độ',     v: `${p.speed} m/s` },
    { k: 'Hướng',      v: `${Math.round(p.heading)}°` },
  ]);
  document.getElementById('tl-widget').style.display = 'none';
  document.getElementById('veh-hdr').style.display = 'none';
  updateMLDecision(`Người đi bộ ${p.id}: ${stateLabel[p.state]||p.state}.`);
}

function selectIncident(inc) {
  const sev = { low:'🟡 Nhẹ', medium:'🟠 Trung bình', high:'🔴 Nghiêm trọng' };
  showDetail('incident', `🚨 ${inc.type}`, inc.description||'', [
    { k: 'Mức độ',    v: sev[inc.severity]||inc.severity, cls: inc.severity==='high'?'bad':'warn' },
    { k: 'Thời gian', v: inc.time||'—' },
    { k: 'Phong tỏa', v: inc.blocked?'⛔ Có':'Không' },
    { k: 'Cảnh sát',  v: inc.policeDispatched?'✓ Đã điều động':'Chưa', cls: inc.policeDispatched?'ok':'' },
  ]);
  document.getElementById('tl-widget').style.display = 'none';
  document.getElementById('veh-hdr').style.display = 'none';
  updateMLDecision(`Phát hiện ${inc.type} mức ${sev[inc.severity]||inc.severity}. RL đang điều phối đèn khu vực.`);
}

function selectJamCluster(c) {
  STATE.map.flyTo({ center: [c.lng, c.lat], zoom: Math.max(STATE.map.getZoom(), 17), speed: 1.4 });
  showDetail('jam', '🔥 Điểm nóng kẹt xe', c.label, [
    { k: 'Số xe ùn ứ', v: `${c.count}` },
    { k: 'Mức kẹt',   v: `${Math.round(c.severity*100)}%`, cls: c.severity>0.7?'bad':'warn' },
  ]);
  document.getElementById('tl-widget').style.display = 'none';
  document.getElementById('veh-hdr').style.display = 'none';
  updateMLDecision(`Cụm ${c.count} xe đang ùn ứ (${Math.round(c.severity*100)}%). RL đang phân tích.`);
}

// ─── TL WIDGET ───────────────────────────────────────────────────────────────
function startTlWidget(l) {
  document.getElementById('tl-widget').style.display = 'block';
  ['red','yellow','green'].forEach(c => {
    const el = document.getElementById(`lamp-${c}`);
    el.className = 'tl-lamp' + (l.phase===c ? ` active-${c}` : '');
  });
  document.getElementById('tl-countdown').textContent = Math.round(l.display_seconds ?? l.countdown);
  document.getElementById('tl-phase').textContent = PHASE_LABEL[l.phase] || l.phase;
  clearInterval(STATE.tlTimer);
  let secs = l.display_seconds ?? l.countdown;
  STATE.tlTimer = setInterval(() => {
    if (!STATE.running) return;
    secs = Math.max(0, secs - STATE.speed);
    document.getElementById('tl-countdown').textContent = Math.round(secs);
    if (secs <= 0) clearInterval(STATE.tlTimer);
  }, 1000);
}

// ─── VEHICLE PANEL ───────────────────────────────────────────────────────────
function renderVehiclePanel(vehicles) {
  const hdr  = document.getElementById('veh-hdr');
  const list = document.getElementById('vehicle-list');
  if (!vehicles.length) { hdr.style.display = 'none'; return; }
  hdr.style.display = 'block';
  list.innerHTML = vehicles.slice(0, 10).map(v => `
    <div class="veh-item">
      <span class="veh-icon">${TYPE_EMOJI[v.type]||'🚗'}</span>
      <div class="veh-info"><div>${TYPE_LABEL[v.type]||v.type}</div><div class="veh-id">${v.id}</div></div>
      <span class="veh-speed">${v.speed} km/h</span>
    </div>`).join('');
}

// ─── DETAIL / ML PANEL ───────────────────────────────────────────────────────
function showDetail(type, title, sub, kvs) {
  document.getElementById('d-title').textContent = title;
  document.getElementById('d-sub').textContent   = sub;
  document.getElementById('d-kvlist').innerHTML  = kvs.map(({ k, v, cls }) =>
    `<div class="kv"><span class="k">${k}</span><span class="v ${cls||''}">${v}</span></div>`).join('');
}
function updateMLDecision(text) {
  document.getElementById('ml-decision-box').innerHTML = `<div class="ml-lbl">PHÂN TÍCH ML</div>${text}`;
}
function updateStats(stats) {
  const set = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val ?? 0; };
  set('s-total', stats.total);
  set('s-speed', stats.avgSpeed);
  set('s-density', stats.density);
  set('s-inc', stats.incidents);
}

// ─── ML LOG ──────────────────────────────────────────────────────────────────
function addMLLog(msg, type = 'info') {
  const log = document.getElementById('ml-log');
  if (!log) return;
  const el = document.createElement('div');
  el.className = `ml-entry ${type}`;
  el.innerHTML = `<span class="ts">${new Date().toLocaleTimeString('vi-VN')}</span> ${msg}`;
  log.prepend(el);
  while (log.children.length > 60) log.removeChild(log.lastChild);
}

// ─── WEBSOCKET ───────────────────────────────────────────────────────────────
function initWS() {
  if (STATE.ws && STATE.ws.readyState === WebSocket.CONNECTING) return;
  try {
    STATE.ws = new WebSocket(CFG.wsUrl);

    STATE.ws.onopen = () => {
      STATE.wsAlive = true;
      setBackendStatus(true);
      if (STATE._mapLoaded) sendViewportOverWS();
      if (STATE._fallbackPoll) {
        clearInterval(STATE._fallbackPoll);
        STATE._fallbackPoll = null;
      }
    };

    STATE.ws.onclose = () => {
      STATE.wsAlive = false;
      setBackendStatus(false);
      setTimeout(initWS, 3000);
      if (!STATE._fallbackPoll) {
        STATE._fallbackPoll = setInterval(() => {
          if (STATE.wsAlive) {
            clearInterval(STATE._fallbackPoll);
            STATE._fallbackPoll = null;
            return;
          }
          if (STATE.running && STATE._mapLoaded) requestViewportData();
        }, 5000);
      }
    };

    STATE.ws.onerror = () => {};

    STATE.ws.onmessage = e => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'oop_update')  applyViewportData(msg.data);
        if (msg.type === 'ml_log')      addMLLog(msg.text, msg.level || 'info');
        if (msg.type === 'ml_decision') updateMLDecision(msg.text);
        if (msg.type === 'stats')       updateStats(msg.data);
        if (msg.type === 'pong')        {}
      } catch {}
    };
  } catch {
    setTimeout(initWS, 3000);
  }
}

// ─── LAYER TOGGLE ────────────────────────────────────────────────────────────
function toggleLayer(name) {
  const checkbox = document.getElementById(`layer-${name}`);
  if (checkbox) STATE.layers[name] = checkbox.checked;
  if (!STATE.layers[name]) {
    STATE.markers[name]?.forEach(m => m.remove());
    STATE.markers[name]?.clear();
  } else if (STATE.lastData) {
    applyViewportData(STATE.lastData);
  }
}

// ─── CONTROLS ────────────────────────────────────────────────────────────────
function toggleSimulation() {
  STATE.running = !STATE.running;
  document.getElementById('btn-sim').textContent = STATE.running ? '⏸ Tạm dừng' : '▶ Tiếp tục';
  fetch(`${CFG.apiBase}/sim/toggle`, { method: 'POST' }).catch(() => {});
}
function cycleSpeed() {
  const speeds = [1, 2, 5, 0.5];
  STATE.speed = speeds[(speeds.indexOf(STATE.speed) + 1) % speeds.length];
  document.getElementById('btn-speed').textContent = `${STATE.speed}× tốc độ`;
  fetch(`${CFG.apiBase}/sim/speed`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ speed: STATE.speed }) }).catch(() => {});
}
function resetView() {
  STATE.map.flyTo({ center: CFG.center, zoom: CFG.initZoom, speed: 1.5 });
}

// ─── DB FLUSH ────────────────────────────────────────────────────────────────
// FIX: Dùng Throttle để luôn ghi định kỳ nhưng không bị kẹt vì nhận data liên tục
let _flushTimer = null;
let _pendingFlushData = null;

function scheduleDbFlush(data) {
  _pendingFlushData = data; // Luôn cập nhật bộ đệm lấy data mới nhất

  if (_flushTimer) return;  // Nếu đang đếm ngược 30s rồi thì không đè timer mới

  _flushTimer = setTimeout(() => {
    _flushTimer = null; // Xóa cờ để chạy vòng lặp mới sau khi flush
    if (!STATE.map || !_pendingFlushData) return;

    const b = STATE.map.getBounds();
    fetch(`${CFG.apiBase}/db/viewport/snapshot`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bbox: { s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast() },
        zoom: Math.round(STATE.map.getZoom()),
        data: _pendingFlushData
      }),
    }).catch(() => {});
  }, CFG.dbFlushMs);
}

// ─── MOCK DATA ───────────────────────────────────────────────────────────────
function generateMockData() {
  const b   = STATE.map.getBounds();
  const rnd = (a, b) => a + Math.random() * (b - a);
  const cnt = Math.floor(20 + STATE.zoom * 2);
  const vT  = ['car','car','car','motorcycle','motorcycle','motorcycle','truck','bus','ambulance','police'];
  const ph  = ['red','green','yellow'];
  const signTypes = ['speed_limit','no_overtaking','no_u_turn','bridge_ahead','roundabout','danger_zone'];
  return {
    vehicles: Array.from({ length: cnt }, (_, i) => ({
      id: `V${i}`, type: vT[i%vT.length],
      lat: rnd(b.getSouth(), b.getNorth()), lng: rnd(b.getWest(), b.getEast()),
      speed: Math.floor(rnd(0, 80)), state: 'cruising', jam_severity: 0,
      size: '4.5m × 1.8m', plate: `51A-${10000+i}`, emergency: false, ml_controlled: false, acceleration: 0,
    })),
    lights: Array.from({ length: Math.floor(cnt/4) }, (_, i) => ({
      id: `TL${i}`, name: `Đèn giao thông #${i}`,
      lat: rnd(b.getSouth(), b.getNorth()), lng: rnd(b.getWest(), b.getEast()),
      phase: ph[i%3], countdown: Math.floor(rnd(3,45)), display_seconds: Math.floor(rnd(3,45)),
      phase_vn: ['ĐÈN ĐỎ 🔴','ĐÈN XANH 🟢','ĐÈN VÀNG 🟡'][i%3], location_type_vn: 'Ngã tư đường',
      cycle: { red:30, yellow:4, green:26 }, queue_length: Math.floor(rnd(0,12)),
      ml_adjusted: Math.random()>0.7, preempted: false, nearbyVehicles: [],
    })),
    signs: Array.from({ length: Math.floor(cnt/3) }, (_, i) => ({
      id: `S${i}`, lat: rnd(b.getSouth(), b.getNorth()), lng: rnd(b.getWest(), b.getEast()),
      type: signTypes[i%signTypes.length], description: '', value: [30,40,60,80][i%4], road_name: 'Đường DEMO',
    })),
    pedestrians: Array.from({ length: Math.floor(cnt/3) }, (_, i) => ({
      id: `P${i}`, lat: rnd(b.getSouth(), b.getNorth()), lng: rnd(b.getWest(), b.getEast()),
      speed: +(Math.random()*1.5+0.5).toFixed(1), state: ['walking','waiting','crossing'][i%3], heading: Math.floor(rnd(0,360)),
    })),
    jamClusters: [],
    incidents: Math.random()>0.8 ? [{
      id:'INC1', lat: rnd(b.getSouth(), b.getNorth()), lng: rnd(b.getWest(), b.getEast()),
      type:'Tai nạn', description:'Va chạm (DEMO)', severity:'medium',
      time: new Date().toLocaleTimeString(), blocked: false, policeDispatched: true,
    }] : [],
    stats: { total: cnt, avgSpeed: Math.floor(rnd(20,55)), density: Math.floor(rnd(20,80)), incidents: 0 },
  };
}

// ─── MAIN ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initMap();
  initWS();
});