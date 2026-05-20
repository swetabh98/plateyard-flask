/* static/yard_3d_view.js */
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer';

// Base navigation collapse and filter collapse are handled by base.html / page patch.
// Do not bind duplicate handlers here, otherwise one click toggles twice.

const YARD_WIDTH=3600, YARD_HEIGHT=2000;
const BIN_FLOOR_THICKNESS=25, BIN_WALL_HEIGHT=560;
const BIN_SCALE=5, BIN_PADDING=0;
const AC_DEPTH_MULTIPLIER = 1;

function effectiveDims(z){
  const w0 = Number(z.width)||0;
  const h0 = Number(z.height)||0;
  const isAC = /^AC/i.test(String(z.bin||''));
  return { w: w0, h: isAC ? h0 * AC_DEPTH_MULTIPLIER : h0 };
}
function toCenterEff(z){
  const { w, h } = effectiveDims(z);
  return {
    x: (z.x_pos + w/2) - (YARD_WIDTH/2),
    z: (z.y_pos + h/2) - (YARD_HEIGHT/2)
  };
}

const INNER_PAD = 40;
const PLATE_H    = 2;
const LIVE_SEARCH_DELAY = 300;

const normBin = (s) => String(s ?? '').toUpperCase().replace(/\s+/g,'').trim();
const assignedByUpper = {};
for (const k in assigned_bins) {
  const key = normBin(k);
  const list = Array.isArray(assigned_bins[k]) ? assigned_bins[k] : [];
  assignedByUpper[key] = list.filter(it => normBin(it?.bin ?? it?.bin_code ?? it?.location) === key);
}
const zones  = Array.isArray(layout_raw?.zones)  ? layout_raw.zones  : [];
const labels = Array.isArray(layout_raw?.labels) ? layout_raw.labels : [];

let scene,camera,renderer,controls,labelRenderer,raycaster,mouse,tooltipEl,pinned=null,currentHover=null;
const hoverables=[], itemMeshes=[];
window.itemMeshes = itemMeshes;

let __yardLabelsBuilt = false;
const __yardLabelObjects = [];

const binCenters={}, binMeshes={}, highlightRings={};
const idToMeshes={};

let shedGroup = null;
const PILLAR_START = 34;
const PILLAR_END   = 67;
const SHED_BAYS = ['EF','DE','CD','AC'];
const PILLAR_RADIUS = 16;
const PILLAR_HEIGHT = 1100;
const ROOF_THICK    = 40;
const ROOF_Y        = PILLAR_HEIGHT + (ROOF_THICK/2) + 30;
const ROOF_MARGIN   = 120;
const COL_W = 55;
const COL_D = 55;
const BEAM_H = 35;
const BEAM_W = 90;
const EAVE_Y = PILLAR_HEIGHT;
const RIDGE_RISE = 220;
const ROOF_OVERHANG_X = 140;
const ROOF_OVERHANG_Z = 220;

const pad2 = (n)=>String(n).padStart(2,'0');

let orthoCamera=null, orthoControls=null, isTopDown=false;
let perspCamera=null, perspControls=null;

const stackRelations = {};
const plateStackIndex = {};
const plateStackTotal = {};
const coilLevel = {};
const coilMaxLevel = {};
const normId = (id)=>String(id||'').trim().toUpperCase();

window.__getStackPositionForItem = getStackPositionForItem;

const statusColor = (s)=>{
  s = String(s||'').toLowerCase().trim();
  if (s === 'fg' || s.includes('finished')) return 0x2ecc71;
  if (s.includes('wip') || s.includes('work in progress')) return 0xf1c40f;
  if (s.includes('good for availability') || s.includes('gfa')) return 0x00c2ff;
  if (s.includes('psfs') || s.includes('prime stock')) return 0x8e44ad;
  if (s.includes('ready for dispatch') || s === 'rfd' || s.includes('dispatch')) return 0x16a085;
  if (s.includes('reject') || s.includes('rejected')) return 0x6b7280;
  return 0x1f85de;
};

const MATERIAL_STATUS_COLORS_RAW = {
  "Finished Status":                 0x7F1DFF,
  "To be Levelled":                  0x0B6E4F,
  "Levelling Completed":             0xB31237,
  "Hot Coil":                        0x2D1B00,
  "For Rework":                      0x4B0082,
  "Offer to PPC/MKTG/RPM-CUST_CLE":  0x7A4E00,
  "TPI completed":                   0x0057B8,
  "For Customer Inspection":         0x8B1E3F,
  "Stacked for WIP":                 0x1B5E20,
  "Normalizing completed":           0xC2185B,
  "Offer to QC - WIP":               0x37474F,
  "Under Testing":                   0x6A1B9A,
  "To be Normalized":                0x1E3A8A,
  "For TPI (3rd party Insp)":        0xA16207,
  "Certification + Trial Rollings":  0x0F766E,
  "Hot Plate":                       0x5B2C6F,
  "Offer to PFP/SSD":                0x7C2D12,
  "To be Normalized and Tempered":   0x14532D,
  "To be Quenched":                  0x0E7490,
  "Quenching done":                  0x9A3412,
};

const MATERIAL_STATUS_COLORS = {};
const __ms_norm = (v) => String(v ?? '').replace(/\u00A0/g, ' ').replace(/\r|\n/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();

for (const [k, v] of Object.entries(MATERIAL_STATUS_COLORS_RAW)) {
  MATERIAL_STATUS_COLORS[__ms_norm(k)] = v;
}

window.__msDebug = { total: 0, matched: 0, fallback: 0, samples: [] };

const materialStatusColor = (s)=>{
  const raw = String(s ?? '');
  const key = __ms_norm(raw);
  const hex = MATERIAL_STATUS_COLORS[key];
  window.__msDebug.total++;
  if (hex === undefined) {
    window.__msDebug.fallback++;
    if (window.__msDebug.samples.length < 30) {
      window.__msDebug.samples.push({ raw, norm: key });
    }
    return 0x111827;
  }
  window.__msDebug.matched++;
  return hex;
};

const plateMat = ()=> new THREE.MeshLambertMaterial({ color:0x1f85de, transparent:true, opacity:0.95 });
const coilMat  = ()=> new THREE.MeshLambertMaterial({ color:0xf1c40f, transparent:false, opacity:1.0 });

/* Performance: reuse geometry buffers instead of allocating a new large BufferGeometry
   for every plate / coil / floor. Each mesh still keeps its own material so existing
   per-item highlighting and filtering continue to work exactly as before. */
const __boxGeometryCache = new Map();
const __cylinderGeometryCache = new Map();
const __q = (n)=> Math.round((Number(n) || 0) * 10) / 10;
function getBoxGeometry(w, h, d){
  const key = `${__q(w)}|${__q(h)}|${__q(d)}`;
  let geo = __boxGeometryCache.get(key);
  if(!geo){
    geo = new THREE.BoxGeometry(w, h, d);
    __boxGeometryCache.set(key, geo);
  }
  return geo;
}
function getCylinderGeometry(radius, length, segments=8){
  const key = `${__q(radius)}|${__q(length)}|${segments}`;
  let geo = __cylinderGeometryCache.get(key);
  if(!geo){
    geo = new THREE.CylinderGeometry(radius, radius, length, segments);
    __cylinderGeometryCache.set(key, geo);
  }
  return geo;
}

const statusColorsToggle = document.getElementById('statusColorsToggle');
const materialStatusToggle = document.getElementById('materialStatusToggle');
const legendRowEl = document.getElementById('legendRow');

const LEGEND = {
  statusColorsOn: [
    { label: 'Finished Good (FG) — Status filter', color: '#2ecc71' },
    { label: 'Work In Progress (WIP) — Status filter', color: '#f1c40f' },
  ],
  statusColorsOff: [
    { label: 'FG (default item color)', color: '#1f85de' },
    { label: 'WIP (default item color)', color: '#f1c40f' },
  ],
  highlights: [
    { label: 'Search / Filter highlight', color: '#ff0000' },
    { label: 'FI Released (1) — FI filter highlight', color: '#ff8c00' },
    { label: 'FI Released / FI Released (2) — FI filter highlight', color: '#006400' },
    { label: 'Focused bin ring', color: '#00c2a8' },
  ]
};

function bindLegendCollapse(){
  const card = document.getElementById('tools-card');
  const btn  = document.getElementById('legendToggle');
  const hint = document.getElementById('legendToggleHint');
  if(!card || !btn || btn.__yardBound) return;
  btn.__yardBound = true;

  const setState = (collapsed)=>{
    card.classList.toggle('collapsed', collapsed);
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    if(hint) hint.textContent = collapsed ? 'Expand' : 'Click';
  };

  setState(false);

  const toggle = ()=>{
    const collapsed = card.classList.contains('collapsed');
    setState(!collapsed);
  };

  btn.addEventListener('click', (e)=>{
    e.preventDefault();
    e.stopPropagation();
    toggle();
  });

  btn.addEventListener('keydown', (e)=>{
    if(e.key === 'Enter' || e.key === ' '){
      e.preventDefault();
      toggle();
    }
  });
}

function renderLegend(){
  if(!legendRowEl) return;
  const items = [];
  const statusOn = !!statusColorsToggle?.checked;
  const base = statusOn ? LEGEND.statusColorsOn : LEGEND.statusColorsOff;
  items.push(...base);
  items.push(...LEGEND.highlights);
  legendRowEl.innerHTML = items.map(it => `
    <div class="legend-item" title="${String(it.label).replace(/"/g,'&quot;')}">
      <span class="swatch" style="background:${it.color};"></span>
      <span>${it.label}</span>
    </div>
  `).join('');
}

const colorLegendBox  = document.getElementById('color-legend-box');
const colorLegendBody = document.getElementById('colorLegendBody');
const colorLegendClose = document.getElementById('colorLegendClose');

function renderRightColorLegend(){
  if(!colorLegendBody) return;
  const selected = window.__matStatusSelected || new Set();
  const msOn = selected.size > 0;
  if(!msOn){
    if(colorLegendBox) colorLegendBox.style.display = 'none';
    return;
  }
  if(colorLegendBox) colorLegendBox.style.display = 'block';
  const items = [];
  items.push({ label: 'Material Status colors (active)', color: null, isHeader:true });
  const keys = Object.keys(MATERIAL_STATUS_COLORS_RAW || {});
  for(const k of keys){
    if(!selected.has(k)) continue;
    const hex = MATERIAL_STATUS_COLORS_RAW[k];
    const css = '#' + hex.toString(16).padStart(6,'0');
    items.push({ label: k, color: css });
  }
  colorLegendBody.innerHTML = items.map(it=>{
    if(it.isHeader){
      return `<div class="cl-muted" style="padding:6px 0 2px; font-weight:700;">${String(it.label).replace(/</g,'&lt;')}</div>`;
    }
    return `
      <div class="cl-item">
        <span class="cl-swatch" style="background:${it.color};"></span>
        <span>${String(it.label).replace(/</g,'&lt;')}</span>
      </div>
    `;
  }).join('');
}

if(colorLegendClose && !colorLegendClose.__bound){
  colorLegendClose.__bound = true;
  colorLegendClose.addEventListener('click', (e)=>{
    e.preventDefault();
    e.stopPropagation();
    if(colorLegendBox) colorLegendBox.style.display = 'none';
    window.__matStatusSelected = new Set();
    const _msCbs = document.querySelectorAll('#materialStatusOptionsList input[type="checkbox"]');
    _msCbs.forEach(cb => cb.checked = false);
    const _msBadge = document.getElementById('materialStatusBtnBadge');
    if(_msBadge){ _msBadge.style.display='none'; _msBadge.textContent=''; }
    if(materialStatusToggle) materialStatusToggle.checked = false;
    reapplyItemColors();
    applyHighlightOnly();
  }, true);
}

const timeline = document.getElementById('timeline');
const timelineClear = document.getElementById('timelineClear');
let timelineDate = null;

const bayFocusBtn         = document.getElementById('bayFocusBtn');
const bayFocusPanel       = document.getElementById('bayFocusPanel');
const bayFocusSelect      = document.getElementById('bayFocusSelect');
const bayFocusApplyBtn    = document.getElementById('bayFocusApplyBtn');
const bayFocusCancelBtn   = document.getElementById('bayFocusCancelBtn');
const bayFocusActiveChip  = document.getElementById('bayFocusActiveChip');
const bayFocusActiveLabel = document.getElementById('bayFocusActiveLabel');
const bayFocusCloseBtn    = document.getElementById('bayFocusCloseBtn');

let __focusedBay = null;

const BAY_OPTIONS = [
  { key: 'EF',     label: 'EF BAY'     },
  { key: 'DE',     label: 'DE BAY'     },
  { key: 'CD',     label: 'CD BAY'     },
  { key: 'AC',     label: 'AC BAY'     },
  { key: 'BWP G',  label: 'BWP-G'      },
  { key: 'BWP H',  label: 'BWP-H'      },
  { key: 'CTL DE', label: 'CTL DE BAY' },
  { key: 'CTL CD', label: 'CTL CD BAY' }
];

function getBayKeyFromBin(binRaw){
  const s = String(binRaw || '').toUpperCase().replace(/[\s\-_\/]+/g,'').trim();
  if(!s) return null;
  if (s.startsWith('BWPG') || s.includes('BWPG')) return 'BWP G';
  if (s.startsWith('BWPH') || s.includes('BWPH')) return 'BWP H';
  if (s.startsWith('CTLDE') || s.includes('CTLDE')) return 'CTL DE';
  if (s.startsWith('CTLCD') || s.includes('CTLCD')) return 'CTL CD';
  const p = s.slice(0,2);
  if (p === 'EF') return 'EF';
  if (p === 'DE') return 'DE';
  if (p === 'CD') return 'CD';
  if (p === 'AC') return 'AC';
  return null;
}

function getBinsForBay(bayKey){
  const keys = Object.keys(binCenters || {});
  if(!keys.length) return [];
  if (bayKey === 'BWP G') return keys.filter(k => k.toUpperCase().startsWith('BWPG'));
  if (bayKey === 'BWP H') return keys.filter(k => k.toUpperCase().startsWith('BWPH'));
  if (bayKey === 'CTL DE') return keys.filter(k => k.toUpperCase().includes('CTLDE'));
  if (bayKey === 'CTL CD') return keys.filter(k => k.toUpperCase().includes('CTLCD'));
  return keys.filter(k => k.startsWith(bayKey));
}

function focusCameraOnBay(bayKey){
  const bins = getBinsForBay(bayKey);
  if(!bins.length){ fitCamera(); return; }
  const pts = bins.map(b => binCenters[b]).filter(Boolean);
  if(!pts.length){ fitCamera(); return; }
  let minX=Infinity, minZ=Infinity, maxX=-Infinity, maxZ=-Infinity;
  for(const p of pts){
    minX = Math.min(minX, p.x - p.w/2);
    maxX = Math.max(maxX, p.x + p.w/2);
    minZ = Math.min(minZ, p.z - p.h/2);
    maxZ = Math.max(maxZ, p.z + p.h/2);
  }
  const cx=(minX+maxX)/2, cz=(minZ+maxZ)/2;
  const size=Math.max(maxX-minX, maxZ-minZ);
  const dist=Math.min(9000, Math.max(800, size * 2.0));
  tweenCamera(
    new THREE.Vector3(cx + dist*0.65, dist*1.15, cz + dist*0.65),
    new THREE.Vector3(cx, 0, cz),
    650
  );
}

function renderBayFocusUI(){
  if(bayFocusSelect && bayFocusSelect.options.length === 0){
    for(const opt of BAY_OPTIONS){
      const o = document.createElement('option');
      o.value = opt.key;
      o.textContent = opt.label;
      bayFocusSelect.appendChild(o);
    }
  }
  if(__focusedBay){
    if(bayFocusActiveLabel) bayFocusActiveLabel.textContent = __focusedBay;
    if(bayFocusActiveChip)  bayFocusActiveChip.style.display = 'inline-flex';
  }else{
    if(bayFocusActiveChip)  bayFocusActiveChip.style.display = 'none';
  }
}

function setBayFocus(bayKey){
  __focusedBay = bayKey || null;
  renderBayFocusUI();
  applyFilters();
  if(__focusedBay){
    focusCameraOnBay(__focusedBay);
  }else{
    fitCamera();
  }
}

const fiSel   = document.getElementById('fiRelFilter');
const sbuSel  = document.getElementById('sbuRelFilter');
const citySel = document.getElementById('customerCityFilter');
const custSel = document.getElementById('customerFilter');
const topStackSel = document.getElementById('topStackFilter');
let __customerCityMap = {};

function isCoilItem(it){ return String(it?.type||'').toLowerCase().includes('coil'); }
function isPlateItem(it){ return !isCoilItem(it); }

let lastMouseX = 0, lastMouseY = 0;

function makeFloorTextMesh(text, pxWidth=900, pxHeight=220) {
  const c = document.createElement('canvas');
  c.width = pxWidth; 
  c.height = pxHeight;
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.font = '900 170px system-ui, Segoe UI, Roboto, Arial';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 22;
  ctx.strokeStyle = 'rgba(255,255,255,0.85)';
  ctx.strokeText(text, c.width/2, c.height/2);
  ctx.fillStyle = '#0f2a43';
  ctx.fillText(text, c.width/2, c.height/2);
  const tex = new THREE.CanvasTexture(c);
  tex.anisotropy = 8;
  tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
  tex.needsUpdate = true;
  const mat = new THREE.MeshBasicMaterial({
    map: tex,
    transparent: true,
    opacity: 1.0,
    side: THREE.DoubleSide,
    depthTest: false,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: -4,
    polygonOffsetUnits: -4
  });
  const W = pxWidth  * 0.65;
  const H = pxHeight * 0.65;
  const geo = new THREE.PlaneGeometry(W, H);
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.renderOrder = 9999;
  return mesh;
}

function _asStr(v){ return String(v ?? '').trim(); }
function _normKey(v){ return _asStr(v); }
function _uniqSorted(arr){
  const s = new Set(arr.map(_normKey).filter(x => x !== ''));
  return Array.from(s).sort((a,b)=>a.localeCompare(b));
}

function getSelectedValues(sel){
  if(!sel) return [];
  return Array.from(sel.selectedOptions || [])
    .map(o => String(o.value || '').trim())
    .filter(v => v && v !== 'all');
}

function populateDropdowns(){
  const allItems = [];
  for(const b in assigned_bins){
    const list = Array.isArray(assigned_bins[b]) ? assigned_bins[b] : [];
    for(const it of list) allItems.push(it || {});
  }
  const fiVals   = _uniqSorted(allItems.map(it => it.FI_Rel_text ?? it.fi_rel_text ?? it.FI_REL_TEXT));
  const sbuVals  = _uniqSorted(allItems.map(it => it.SBU_RelStatus ?? it.sbu_rel_status ?? it.SBU_RELSTATUS));
  const cityVals = _uniqSorted(allItems.map(it => it.CustomerCity ?? it.customer_city ?? it.CUSTOMERCITY));
  const custVals = _uniqSorted(allItems.map(it => it.customer ?? it.Customer ?? it.CUSTOMER));
  __customerCityMap = {};
  for(const it of allItems){
    const cust = _asStr(it.customer ?? it.Customer ?? it.CUSTOMER);
    const city = _asStr(it.CustomerCity ?? it.customer_city ?? it.CUSTOMERCITY);
    if(!cust || !city) continue;
    (__customerCityMap[cust] ||= new Set()).add(city);
  }
  for(const k of Object.keys(__customerCityMap)){
    __customerCityMap[k] = Array.from(__customerCityMap[k]).sort((a,b)=>a.localeCompare(b));
  }
  const fill = (sel, vals)=>{
    if(!sel) return;
    while(sel.options.length > 1) sel.remove(1);
    for(const v of vals){
      const o=document.createElement('option');
      o.value=v; o.textContent=v;
      sel.appendChild(o);
    }
  };
  fill(fiSel, fiVals);
  fill(sbuSel, sbuVals);
  fill(custSel, custVals);

  function refreshCityOptions(){
    if(!citySel) return;
    const selectedCustomers = getSelectedValues(custSel);
    let citiesFor = [];
    if(selectedCustomers.length){
      const set = new Set();
      for(const c of selectedCustomers){
        const arr = __customerCityMap[c] || [];
        arr.forEach(x => set.add(x));
      }
      citiesFor = Array.from(set).sort((a,b)=>a.localeCompare(b));
    } else {
      citiesFor = cityVals;
    }
    const prevSelectedCities = getSelectedValues(citySel);
    fill(citySel, citiesFor);
    if(prevSelectedCities.length){
      const opts = Array.from(citySel.options);
      const ok = new Set(opts.map(o=>o.value));
      for(const o of opts){
        o.selected = prevSelectedCities.includes(o.value) && ok.has(o.value);
      }
    }
  }

  refreshCityOptions();
  if(custSel && !custSel.__linkedCityHandler){
    custSel.__linkedCityHandler = true;
    custSel.addEventListener('change', ()=>{
      if(citySel){
        Array.from(citySel.options).forEach(o => o.selected = (o.value === 'all'));
      }
      refreshCityOptions();
    });
  }
}

function init(){
  const container=document.getElementById('canvas-container'); tooltipEl=document.getElementById('tooltip');

  // FIX: Force fixed positioning so base.html wrappers don't double-shift the canvas
  container.style.position = 'fixed';

  scene=new THREE.Scene();
  scene.background=new THREE.Color(0xe9eef5);
  camera=new THREE.PerspectiveCamera(45,container.clientWidth/container.clientHeight,0.1,20000);
  renderer=new THREE.WebGLRenderer({
    antialias:false,
    powerPreference:'low-power',
    alpha:false,
    stencil:false,
    depth:true,
    premultipliedAlpha:true
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 0.55));
  renderer.setSize(container.clientWidth,container.clientHeight);
  renderer.shadowMap.enabled=false;
  renderer.localClippingEnabled = true;
  if('outputColorSpace' in renderer) renderer.outputColorSpace=THREE.SRGBColorSpace; else renderer.outputEncoding=THREE.sRGBEncoding;
  renderer.toneMapping = THREE.NoToneMapping;
  container.appendChild(renderer.domElement);

  labelRenderer=new CSS2DRenderer(); labelRenderer.setSize(container.clientWidth,container.clientHeight);
  labelRenderer.domElement.style.position='absolute'; labelRenderer.domElement.style.inset='0';
  labelRenderer.domElement.style.pointerEvents='none'; labelRenderer.domElement.style.zIndex='2';
  container.appendChild(labelRenderer.domElement);

  controls=new OrbitControls(camera,renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.12;
  controls.rotateSpeed   = 0.9;  controls.zoomSpeed       = 1.2; controls.panSpeed = 0.9;
  controls.screenSpacePanning = true; controls.enablePan = true;
  controls.minDistance   = 120; controls.maxDistance   = 22000; controls.maxPolarAngle = Math.PI*0.49;
  controls.mouseButtons.LEFT   = THREE.MOUSE.ROTATE;
  controls.mouseButtons.MIDDLE = THREE.MOUSE.DOLLY;
  controls.mouseButtons.RIGHT  = THREE.MOUSE.PAN;
  controls.touches.ONE = THREE.TOUCH.ROTATE;
  controls.touches.TWO = THREE.TOUCH.DOLLY_PAN;

  scene.add(new THREE.HemisphereLight(0xffffff,0x88aaff,0.9));
  const dir=new THREE.DirectionalLight(0xffffff,1.2); dir.position.set(1600,2000,1200);
  scene.add(dir);
  scene.add(new THREE.AmbientLight(0xffffff,0.45));

  const ground=new THREE.Mesh(new THREE.PlaneGeometry(16000,16000), new THREE.MeshBasicMaterial({color:0xf2f6ff}));
  ground.rotation.x=-Math.PI/2; scene.add(ground);

  zones.forEach(z => {
    const { w: effW, h: effH } = effectiveDims(z);
    const { x, z: zz } = toCenterEff(z);
    const w_shell = Math.max(1, effW * BIN_SCALE - BIN_PADDING);
    const d_shell = Math.max(1, effH * BIN_SCALE - BIN_PADDING);
    z._effW = effW; z._effH = effH;

    const floor = new THREE.Mesh(
      getBoxGeometry(w_shell, BIN_FLOOR_THICKNESS, d_shell),
      new THREE.MeshLambertMaterial({ color: 0xbfd4ff, transparent: true, opacity: 0.25, depthWrite: false })
    );
    floor.position.set(x, BIN_FLOOR_THICKNESS / 2, zz);
    scene.add(floor);

    const binCode = normBin(z.bin || '');
    floor.userData = { bin: binCode, items: assignedByUpper[binCode] || [] };
    hoverables.push(floor);

    if (binCode) {
      binCenters[binCode] = { x, y: BIN_FLOOR_THICKNESS / 2, z: zz, w: w_shell, h: d_shell, floor, code: binCode };
      (binMeshes[binCode] ||= []).push(floor);
    }

    const items = assignedByUpper[binCode] || [];
    if (items.length) { placeItemsInBin(items, z); }
  });

  // Build CSS2D labels lazily. Thousands of DOM labels are expensive, so keep
  // them out of the page until the user explicitly enables Labels.

  const showShedCb0 = document.getElementById('showShed');
  if (showShedCb0) showShedCb0.checked = false;

  const showLabelsCb0 = document.getElementById('showLabels');
  document.body.classList.toggle('labels-hidden', !(showLabelsCb0 && showLabelsCb0.checked));
  if(showLabelsCb0 && showLabelsCb0.checked) ensureYardLabelsBuilt();
  if (showLabelsCb0 && !showLabelsCb0.__bound) {
    showLabelsCb0.__bound = true;
    showLabelsCb0.addEventListener('change', () => {
      document.body.classList.toggle('labels-hidden', !showLabelsCb0.checked);
      if(showLabelsCb0.checked) ensureYardLabelsBuilt();
      else removeYardLabels();
    });
  }

  // Shed is heavy, so build it lazily only when the user enables it.
  if (shedGroup) shedGroup.visible = false;

  {
    let maxRight = -Infinity;
    const sums = {
      EF:    { sum: 0, n: 0 },
      DE:    { sum: 0, n: 0 },
      CD:    { sum: 0, n: 0 },
      AC:    { sum: 0, n: 0 },
      BWPG:  { sum: 0, n: 0 },
      BWPH:  { sum: 0, n: 0 },
    };

    for (const z of zones) {
      const { x, z: cz } = toCenterEff(z);
      const { w: effW } = effectiveDims(z);
      const w = effW * BIN_SCALE;
      maxRight = Math.max(maxRight, x + w / 2);

      const code = String(z.bin || '').toUpperCase();
      let key = null;
      if (code.startsWith('EF')) key = 'EF';
      else if (code.startsWith('DE')) key = 'DE';
      else if (code.startsWith('CD')) key = 'CD';
      else if (code.startsWith('AC')) key = 'AC';
      else if (code.startsWith('BWPG')) key = 'BWPG';
      else if (code.startsWith('BWPH')) key = 'BWPH';

      if (key) {
        sums[key].sum += cz;
        sums[key].n += 1;
      }
    }

    const rightX = maxRight + 220;

    const addBayLabel = (key, text) => {
      const rec = sums[key];
      if (!rec || !rec.n) return;
      const zPos = rec.sum / rec.n;
      const mesh = makeFloorTextMesh(text);
      mesh.position.set(rightX, 1.1, zPos);
      scene.add(mesh);
    };

    addBayLabel('BWPH', 'BWP-H');
    addBayLabel('BWPG', 'BWP-G');
    addBayLabel('EF',   'EF BAY');
    addBayLabel('DE',   'DE BAY');
    addBayLabel('CD',   'CD BAY');
    addBayLabel('AC',   'AC BAY');
  }

  {
    let maxRight = -Infinity;
    for (const z of zones) {
      const { x } = toCenterEff(z);
      const { w: effW } = effectiveDims(z);
      const w = effW * BIN_SCALE;
      maxRight = Math.max(maxRight, x + w / 2);
    }

    const rightX = maxRight + 220;
    const targets = [
      { text: 'CTL CD BAY', re: /\bCTL\s*DE\s*BAY\b/i, fallbackBay: 'DE' },
      { text: 'CTL DE BAY', re: /\bCTL\s*CD\s*BAY\b/i, fallbackBay: 'CD' },
    ];

    const acZ = (() => {
      const keys = Object.keys(binCenters).filter(k => k.startsWith('AC'));
      if (!keys.length) return null;
      let sz = 0, n = 0;
      for (const k of keys) {
        const c = binCenters[k];
        if (!c) continue;
        sz += c.z;
        n++;
      }
      return n ? (sz / n) : null;
    })();

    const CTL_CD_OFFSET = 520;
    const CTL_DE_OFFSET = 1060;

    const bayCenterZ = (bayPrefix) => {
      const keys = Object.keys(binCenters).filter(k => k.startsWith(bayPrefix));
      if (!keys.length) return null;
      let sz = 0, n = 0;
      for (const k of keys) {
        const c = binCenters[k];
        if (!c) continue;
        sz += c.z;
        n++;
      }
      return n ? (sz / n) : null;
    };

    const addAt = (zPos, text) => {
      const mesh = makeFloorTextMesh(text, 1500, 280);
      mesh.position.set(rightX, 1.1, zPos);
      mesh.renderOrder = 9999;
      if (mesh.material) {
        mesh.material.depthTest = false;
        mesh.material.depthWrite = false;
        mesh.material.transparent = true;
        mesh.material.opacity = 1.0;
        mesh.material.needsUpdate = true;
      }
      scene.add(mesh);
    };

    for (const t of targets) {
      let zPos = null;
      for (const l of (labels || [])) {
        const rawTxt = String(l?.text ?? '');
        if (!t.re.test(rawTxt)) continue;
        const ly = Number(l?.y_pos ?? l?.y);
        const h  = Number(l?.height || 0);
        if (!Number.isFinite(ly)) continue;
        zPos = (ly + (h ? h / 2 : 0)) - (YARD_HEIGHT / 2);
        break;
      }
      if (zPos === null) {
        zPos = bayCenterZ(t.fallbackBay);
      }
      if (acZ !== null) {
        zPos = acZ + (t.text === 'CTL DE BAY' ? CTL_DE_OFFSET : CTL_CD_OFFSET);
      }
      if (zPos !== null) addAt(zPos, t.text);
    }
  }

  fitCamera();

  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2();

  let _hoverTick = 0;
  const onMove=(e)=>{
    if (e.target && e.target.closest){
      if (e.target.closest('#search-details') ||
          e.target.closest('#mixed-lot-panel') ||
          e.target.closest('#dispatchDrawer') ||
          e.target.closest('#dispatchBackdrop') ||
          e.target.closest('#bin-detail-panel') ||
          e.target.closest('#filters') ||
          e.target.closest('#tools-card')) {
        return;
      }
    }
    const c=document.getElementById('canvas-container').getBoundingClientRect();
    lastMouseX = e.clientX;
    lastMouseY = e.clientY;
    mouse.set(((e.clientX-c.left)/c.width)*2-1, -((e.clientY-c.top)/c.height)*2+1);
    positionTooltip(e);
    if(pinned) return;
    if((_hoverTick++ & 1)===1) return;

    raycaster.setFromCamera(mouse,camera);
    const itemObjects = itemMeshes.map(o=>o.mesh).filter(m => m.visible !== false);
    const hits=raycaster.intersectObjects(itemObjects,true);

    if(hits.length){
      const obj=hits[0].object;
      if(currentHover!==obj){
        currentHover=obj;
        const u = obj.userData || {};
        if (passesCurrentFilters(u)) showItemTooltip(u);
        else hideTooltip();
      }
    }else{ currentHover=null; hideTooltip(); }
  };
  window.addEventListener('mousemove',onMove,{passive:true});
  window.addEventListener('click',onClick,{passive:true});
  window.addEventListener('resize',onResize,{passive:true});
  window.addEventListener('keydown',onKeyDown);

  // 1. Defensively check before adding listeners
  if (statusColorsToggle) {
    statusColorsToggle.addEventListener('change', () => {
      reapplyItemColors();
      applyHighlightOnly();
      renderLegend();
      renderRightColorLegend();
    });
  }

  // 2. Material Status Dropdown Logic
  window.__matStatusSelected = new Set();
  (function initMaterialStatusDropdown(){
    const btn      = document.getElementById('materialStatusBtn');
    const dropdown = document.getElementById('materialStatusDropdown');
    const optsList = document.getElementById('materialStatusOptionsList');
    const clearBtn = document.getElementById('materialStatusClearBtn');
    const badge    = document.getElementById('materialStatusBtnBadge');

    if(!btn || !dropdown || !optsList) return;

    // FIX: Move the dropdown directly to the body. 
    // This breaks it out of the #filters "backdrop-filter" CSS context 
    // so that our bounding rectangle calculation correctly positions it on screen.
    document.body.appendChild(dropdown);

    const keys = Object.keys(MATERIAL_STATUS_COLORS_RAW || {});
    optsList.innerHTML = keys.map(k => {
      const hex = MATERIAL_STATUS_COLORS_RAW[k];
      const css = '#' + hex.toString(16).padStart(6,'0');
      return `
        <label id="ms-opt-${k.replace(/[^a-zA-Z0-9]/g,'_')}" style="
            display:flex;align-items:center;gap:10px;
            padding:7px 14px;cursor:pointer;
            font:600 12.5px 'DM Sans',sans-serif;
            color:var(--text-primary);
            transition:background .12s;
        " onmouseover="this.style.background='var(--surface-2)'" onmouseout="this.style.background=''">
          <input type="checkbox" data-ms-key="${k}" style="accent-color:${css};width:14px;height:14px;cursor:pointer;" />
          <span style="display:inline-block;width:13px;height:13px;border-radius:3px;background:${css};border:1px solid rgba(0,0,0,.15);flex-shrink:0;"></span>
          <span style="flex:1;">${k}</span>
        </label>
      `;
    }).join('');

    function updateBadge(){
      const n = window.__matStatusSelected.size;
      if(n > 0){
        badge.style.display = '';
        badge.textContent = n;
      } else {
        badge.style.display = 'none';
      }
    }

    function applySelection(){
      window.__msDebug = { total: 0, matched: 0, fallback: 0, samples: [] };
      reapplyItemColors();
      applyHighlightOnly();
      renderRightColorLegend();
      updateBadge();
    }

    optsList.addEventListener('change', e => {
      const cb = e.target;
      if(!cb || cb.type !== 'checkbox') return;
      const key = cb.getAttribute('data-ms-key');
      if(!key) return;
      if(cb.checked){
        window.__matStatusSelected.add(key);
      } else {
        window.__matStatusSelected.delete(key);
      }
      applySelection();
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', e => {
        e.stopPropagation();
        window.__matStatusSelected.clear();
        optsList.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
        applySelection();
      });
    }

    // Toggle logic with preventDefault to ensure stable clicks
    btn.addEventListener('click', e => {
      e.preventDefault();
      e.stopImmediatePropagation();
      e.stopPropagation();

      const isOpen = dropdown.classList.contains('open') || dropdown.style.display === 'block';
      if(isOpen){
        dropdown.classList.remove('open');
        dropdown.style.display = 'none';
        return;
      }

      dropdown.classList.add('open');
      dropdown.style.visibility = 'hidden';
      dropdown.style.display = 'flex';
      dropdown.style.flexDirection = 'column';

      const btnRect = btn.getBoundingClientRect();
      const vw  = window.innerWidth;
      const vh  = window.innerHeight;
      const gap = 8;

      const maxDDW = Math.min(Math.max(dropdown.offsetWidth || 280, 280), vw - 16);
      dropdown.style.maxWidth = maxDDW + 'px';
      dropdown.style.width = maxDDW + 'px';

      let left = btnRect.left;
      if(left + maxDDW > vw - 8) left = btnRect.right - maxDDW;
      if(left < 8) left = 8;
      if(left + maxDDW > vw - 8) left = vw - maxDDW - 8;

      const maxDDH = Math.min(420, vh - 16);
      dropdown.style.maxHeight = maxDDH + 'px';

      let top = btnRect.bottom + gap;
      if(top + maxDDH > vh - 8){
        top = btnRect.top - Math.min(maxDDH, btnRect.top - 16) - gap;
      }
      if(top < 8) top = 8;

      dropdown.style.left = Math.round(left) + 'px';
      dropdown.style.top  = Math.round(top)  + 'px';
      dropdown.style.right = 'auto';
      dropdown.style.visibility = '';
    });

    document.addEventListener('click', e => {
      const wrap = document.getElementById('materialStatusDropdownWrap');
      // Because we moved the dropdown to the body, we also need to check if the click was inside the dropdown
      if((wrap && wrap.contains(e.target)) || dropdown.contains(e.target)){
        return;
      }
      dropdown.classList.remove('open');
      dropdown.style.display = 'none';
    });
  })();

  // 3. Defensively check before adding timeline listeners
  if (timeline) {
    timeline.addEventListener('change',()=>{ 
      timelineDate = timeline.value ? new Date(timeline.value) : null; 
      applyFilters(); 
      updateDashboard(); 
    });
  }
  
  if (timelineClear) {
    timelineClear.addEventListener('click',()=>{ 
      timeline.value=''; 
      timelineDate=null; 
      applyFilters(); 
      updateDashboard(); 
    });
  }

  renderBayFocusUI();

  if(bayFocusBtn){
    bayFocusBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopPropagation();
      if(!bayFocusPanel) return;
      const open = (bayFocusPanel.style.display !== 'block');
      bayFocusPanel.style.display = open ? 'block' : 'none';
    });
  }

  if(bayFocusCancelBtn){
    bayFocusCancelBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopPropagation();
      if(bayFocusPanel) bayFocusPanel.style.display = 'none';
    });
  }

  if(bayFocusApplyBtn){
    bayFocusApplyBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopPropagation();
      const v = bayFocusSelect ? String(bayFocusSelect.value || '').trim() : '';
      if(v){
        setBayFocus(v);
      }
      if(bayFocusPanel) bayFocusPanel.style.display = 'none';
    });
  }

  if(bayFocusCloseBtn){
    bayFocusCloseBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopPropagation();
      setBayFocus(null);
      if(bayFocusPanel) bayFocusPanel.style.display = 'none';
    });
  }

  const showShedCb = document.getElementById('showShed');
  if(showShedCb){
    showShedCb.addEventListener('change', ()=>{
      if(showShedCb.checked && !shedGroup) buildShedAndPillars();
      if(shedGroup) shedGroup.visible = !!showShedCb.checked;
    });
  }

  fiSel?.addEventListener('change',()=>{ applyHighlightOnly(); updateDashboard(); });
  sbuSel?.addEventListener('change',()=>{ applyHighlightOnly(); updateDashboard(); });
  citySel?.addEventListener('change',()=>{ applyHighlightOnly(); updateDashboard(); });
  custSel?.addEventListener('change',()=>{ applyHighlightOnly(); updateDashboard(); });
  topStackSel?.addEventListener('change',()=>{ applyHighlightOnly(); updateDashboard(); });

  const closeBtn = document.getElementById('detailsCloseBtn');
  if(closeBtn){
    closeBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopImmediatePropagation();
      e.stopPropagation();
      hideDetails();
    }, true);
  }

  const exportBtn = document.getElementById('detailsExportBtn');
  if(exportBtn){
    exportBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopImmediatePropagation();
      e.stopPropagation();
      if (window.exportCustomerWisePanelExcel) window.exportCustomerWisePanelExcel();
      else alert('Excel export module not loaded yet.');
    }, true);
  }

  const mlClose = document.getElementById('mixedLotCloseBtn');
  if(mlClose){
    mlClose.addEventListener('click', (e)=>{
      e.preventDefault(); e.stopImmediatePropagation(); e.stopPropagation();
      hideMixedLotPanel();
    }, true);
  }
  const mlExport = document.getElementById('mixedLotExportBtn');
  if(mlExport){
    mlExport.addEventListener('click', (e)=>{
      e.preventDefault(); e.stopImmediatePropagation(); e.stopPropagation();
      if(window.exportMixedLotExcel) window.exportMixedLotExcel();
      else alert('Excel export module not loaded yet.');
    }, true);
  }

  const detailsBox = document.getElementById('search-details');
  if(detailsBox){
    const stop = (ev)=>{ ev.stopPropagation(); };
    detailsBox.addEventListener('wheel', stop, { passive:true, capture:true });
    detailsBox.addEventListener('mousedown', stop, { passive:true, capture:true });
    detailsBox.addEventListener('mousemove', stop, { passive:true, capture:true });
    detailsBox.addEventListener('touchstart', stop, { passive:true, capture:true });
    detailsBox.addEventListener('touchmove', stop, { passive:true, capture:true });
    detailsBox.addEventListener('scroll', ()=>{ updateDetailsStickyVars(); }, { passive:true });
  }

  const mlBox = document.getElementById('mixed-lot-panel');
  if(mlBox){
    const stop = (ev)=>{ ev.stopPropagation(); };
    mlBox.addEventListener('wheel', stop, { passive:true, capture:true });
    mlBox.addEventListener('mousedown', stop, { passive:true, capture:true });
    mlBox.addEventListener('mousemove', stop, { passive:true, capture:true });
    mlBox.addEventListener('touchstart', stop, { passive:true, capture:true });
    mlBox.addEventListener('touchmove', stop, { passive:true, capture:true });
  }

  bindNavCube();
  injectTopDownToggle();

  perspCamera = camera;
  perspControls = controls;

  populateDropdowns();
  applyFilters();

  renderLegend();
  bindLegendCollapse();
  if(colorLegendBox) colorLegendBox.style.display = 'none';

  const mixedLotBtn = document.getElementById('mixedLotBtn');
  if(mixedLotBtn){
    mixedLotBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      e.stopImmediatePropagation();
      e.stopPropagation();
      showMixedLotPanel();
    }, true);
  }

  animate();
  updateDashboard();
  
  // FIX: Force a resize calculation immediately to snap the canvas perfectly to the screen
  setTimeout(onResize, 50);
}

function fitCamera(){
  const dist=Math.max(YARD_WIDTH,YARD_HEIGHT)*1.1;
  camera.position.set(0,dist*0.9,dist*0.42);
  controls.target.set(0,0,0); controls.update();
}
function onResize(){
  const c=document.getElementById('canvas-container');
  camera.aspect=c.clientWidth/c.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(c.clientWidth,c.clientHeight);
  labelRenderer.setSize(c.clientWidth,c.clientHeight);
  if(isTopDown && orthoCamera){
    updateOrthoFrustumToContent();
  }
  updateDetailsStickyVars();
}
let __lastRenderTs = 0;
function animate(ts=0){
  requestAnimationFrame(animate);
  // Keep interaction smooth enough while reducing GPU/CPU pressure on large yards.
  // This is the biggest runtime win without changing existing behaviour or UI.
  if(ts - __lastRenderTs < 50) return;
  __lastRenderTs = ts;
  controls.update();
  renderer.render(scene,camera);
  if(!document.body.classList.contains('labels-hidden') && __yardLabelsBuilt){
    labelRenderer.render(scene,camera);
  }
}

function tweenCamera(toPos, toTarget, ms=550){
  const fromPos = camera.position.clone();
  const fromTgt = controls.target.clone();
  const t0 = performance.now();
  function step(now){
    const t = Math.min(1, (now - t0) / ms);
    const ease = t<0.5 ? 2*t*t : -1+(4-2*t)*t;
    camera.position.lerpVectors(fromPos,toPos,ease);
    controls.target.lerpVectors(fromTgt,toTarget,ease);
    controls.update();
    if(t<1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
function goView(dir){
  const dist=camera.position.distanceTo(controls.target);
  const d=dir.clone().normalize().multiplyScalar(dist);
  const toPos = controls.target.clone().add(d);
  tweenCamera(toPos, controls.target.clone(), 550);
}

function bindNavCube(){
  const cv  = document.getElementById('navcube');
  const ctx = cv.getContext('2d');

  const draw = ()=>{
    const w=cv.width, h=cv.height;
    ctx.clearRect(0,0,w,h);
    ctx.strokeStyle='#cbd5e1'; ctx.lineWidth=10;
    ctx.beginPath(); ctx.arc(w/2,h/2, w*0.35, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle='#94a3b8';
    ctx.font='10px system-ui';
    ctx.fillText('N', w/2-4, 14);
    ctx.fillText('S', w/2-4, h-6);
    ctx.fillText('W', 6, h/2+3);
    ctx.fillText('E', w-14, h/2+3);
    ctx.fillStyle='#dce6f9'; ctx.fillRect(w/2-23, h/2-23, 46, 46);
    ctx.fillStyle='#bcd4fb'; ctx.beginPath(); ctx.moveTo(w/2+23,h/2+23); ctx.lineTo(w/2+23,h/2-6); ctx.lineTo(w/2+36,h/2-18); ctx.lineTo(w/2+36,h/2+11); ctx.closePath(); ctx.fill();
    ctx.fillStyle='#9ec2fb'; ctx.beginPath(); ctx.moveTo(w/2-23,h/2+23); ctx.lineTo(w/2+23,h/2+23); ctx.lineTo(w/2+36,h/2+11); ctx.lineTo(w/2-10,h/2+11); ctx.closePath(); ctx.fill();
    ctx.fillStyle='#0f172a'; ctx.font='11px system-ui'; ctx.fillText('UP', w/2-11, h/2-27);
  };

  const region = (x,y)=>{
    const w=cv.width, h=cv.height, cx=w/2, cy=h/2, r=w*0.35;
    const dx=x-cx, dy=y-cy, d=Math.hypot(dx,dy);
    if(d>r-8 && d<r+12){
      const ang=Math.atan2(dy,dx);
      if(ang>-Math.PI*3/4 && ang<-Math.PI/4) return 'N';
      if(ang> Math.PI/4  && ang< Math.PI*3/4) return 'S';
      if(ang<=-Math.PI*3/4 || ang>= Math.PI*3/4) return 'W';
      return 'E';
    }
    if(x> w/2-23 && x< w/2+23 && y> h/2-23 && y< h/2+23) return 'CENTER';
    return 'NONE';
  };

  let dragging=false, lx=0, ly=0;
  const orbitBy=(dx,dy)=>{
    const off = new THREE.Vector3().subVectors(camera.position, controls.target);
    const sph = new THREE.Spherical().setFromVector3(off);
    const rotSpeed=0.005;
    sph.theta -= dx*rotSpeed;
    sph.phi   -= dy*rotSpeed;
    sph.phi = Math.max(0.01, Math.min(Math.PI-0.01, sph.phi));
    off.setFromSpherical(sph);
    camera.position.copy(controls.target.clone().add(off));
    controls.update();
  };

  cv.addEventListener('mousedown',e=>{ dragging=true; lx=e.clientX; ly=e.clientY; });
  window.addEventListener('mouseup',()=>dragging=false);
  window.addEventListener('mousemove',e=>{ if(!dragging) return; orbitBy(e.clientX-lx, e.clientY-ly); lx=e.clientX; ly=e.clientY; });

  cv.addEventListener('click',e=>{
    const rect=cv.getBoundingClientRect();
    const x=e.clientX-rect.left, y=e.clientY-rect.top;
    const r=region(x,y);
    if(r==='N') goView(new THREE.Vector3(0,1,0));
    else if(r==='S') goView(new THREE.Vector3(0,0,1));
    else if(r==='W') goView(new THREE.Vector3(-1,0,0));
    else if(r==='E') goView(new THREE.Vector3(1,0,0));
    else if(r==='CENTER') goView(new THREE.Vector3(1,1,1));
  });
  cv.addEventListener('dblclick',()=>{ controls.target.set(0,0,0); goView(new THREE.Vector3(0.6,0.8,0.6)); });

  draw();
}

function placeItemsInBin(items, zone) {
  const center = toCenterEff(zone);
  const cx = center.x;
  const cz = center.z;

  const innerW = Math.max(40, (zone._effW ?? zone.width) - 2 * INNER_PAD);
  const innerD = Math.max(40, (zone._effH ?? zone.height) - 2 * INNER_PAD);

  const isCoil = (it) => String(it?.type || '').toLowerCase().includes('coil');
  const coils  = items.filter(isCoil).slice(0, 18);
  const plates = items.filter(it => !isCoil(it));

  const binCode = normBin(zone?.bin || '');

  if (coils.length) {
    const COLS = 7;
    const r = Math.min(innerW / (COLS + 0.5), innerD / 5) * 0.45;
    const coilLenX_base = innerW * 0.275;
    const gap = r * 0.2;
    const y1 = BIN_FLOOR_THICKNESS + r;
    const y2 = y1 + Math.sqrt(3) * r;
    const y3 = y2 + Math.sqrt(3) * r;
    const dz = 2 * r + gap;
    const zStart = cz - (3 * dz);

    const bottomRow = [];
    const midRow    = [];
    const topRow    = [];

    for (let i = 0; i < 7 && i < coils.length; i++) {
      const it = coils[i];
      const xPos = cx;
      const zPos = zStart + i * dz;
      const mesh = new THREE.Mesh(
        getCylinderGeometry(r, coilLenX_base, 8),
        coilMat()
      );
      mesh.rotation.z = Math.PI / 2;
      mesh.position.set(xPos, y1, zPos);
      finalizePlacedMesh(mesh, it, zone);
      const pid = normId(it.plate_id);
      coilLevel[pid] = 0;
      coilMaxLevel[binCode] = 3;
      bottomRow.push({ id: pid, z: zPos, bin: binCode });
    }

    for (let i = 0; i < 6 && (7 + i) < coils.length; i++) {
      const it = coils[7 + i];
      const xPos = cx;
      const zPos = zStart + (i + 0.5) * dz;
      const mesh = new THREE.Mesh(
        getCylinderGeometry(r, coilLenX_base * 0.95, 8),
        coilMat()
      );
      mesh.rotation.z = Math.PI / 2;
      mesh.position.set(xPos, y2, zPos);
      finalizePlacedMesh(mesh, it, zone);
      const pid = normId(it.plate_id);
      coilLevel[pid] = 1;
      coilMaxLevel[binCode] = 3;
      midRow.push({ id: pid, z: zPos, bin: binCode });
    }

    for (let i = 0; i < 5 && (13 + i) < coils.length; i++) {
      const it = coils[13 + i];
      const xPos = cx;
      const zPos = zStart + (i + 1) * dz;
      const mesh = new THREE.Mesh(
        getCylinderGeometry(r, coilLenX_base * 0.9, 8),
        coilMat()
      );
      mesh.rotation.z = Math.PI / 2;
      mesh.position.set(xPos, y3, zPos);
      finalizePlacedMesh(mesh, it, zone);
      const pid = normId(it.plate_id);
      coilLevel[pid] = 2;
      coilMaxLevel[binCode] = 3;
      topRow.push({ id: pid, z: zPos, bin: binCode });
    }

    const linkNearest = (upper, lower) => {
      for (const u of upper) {
        let nearest = null, best = Infinity;
        for (const l of lower) {
          const d = Math.abs(u.z - l.z);
          if (d < best) { best = d; nearest = l; }
        }
        if (nearest) {
          (stackRelations[u.id] ||= { over: null, under: [] }).over = nearest.id;
          const rec = (stackRelations[nearest.id] ||= { over: null, under: [] });
          if (!rec.under.includes(u.id)) rec.under.push(u.id);
        }
      }
    };
    if (bottomRow.length && midRow.length) linkNearest(midRow, bottomRow);
    if (midRow.length && topRow.length)   linkNearest(topRow,  midRow);
  }

  if (plates.length) {
    const padX = Math.max(6, innerW * 0.02);
    const padZ = Math.max(6, innerD * 0.02);
    const MAX_L = Math.max(40, innerW - 2 * padX);
    const MAX_W = Math.max(40, innerD - 2 * padZ);
    const yStart = BIN_FLOOR_THICKNESS + PLATE_H / 2;

    let stackIndex = 0;
    for (const it of plates) {
      const thickness = Number(it?.thickness) && isFinite(it.thickness)
        ? Math.max(2, Math.min(PLATE_H, +it.thickness))
        : PLATE_H;

      let L = MAX_L;
      let W = L / 2;
      if (W > MAX_W) { W = MAX_W; L = Math.min(MAX_L, 2 * W); }
      L = Math.max(40, L);
      W = Math.max(20, W);

      const mesh = new THREE.Mesh(getBoxGeometry(L, thickness, W), plateMat());
      const jitter = (stackIndex % 3) * 1.2;
      const xPos = cx + (stackIndex % 2 === 0 ? jitter : -jitter);
      const zPos = cz + ((stackIndex + 1) % 2 === 0 ? jitter : -jitter);
      const yPos = yStart + stackIndex * (thickness + 1.5);

      mesh.position.set(xPos, yPos, zPos);
      finalizePlacedMesh(mesh, it, zone);

      const pid = normId(it.plate_id);
      plateStackIndex[pid] = stackIndex;
      plateStackTotal[binCode] = (plateStackTotal[binCode] || 0) + 1;

      if (stackIndex > 0) {
        const below = normId(plates[stackIndex - 1]?.plate_id);
        if (pid && below) {
          (stackRelations[pid] ||= { over: null, under: [] }).over = below;
          (stackRelations[below] ||= { over: null, under: [] }).under.push(pid);
        }
      }
      stackIndex++;
    }
  }
}

function finalizePlacedMesh(mesh, item, zone){
  const binCode=normBin(zone?.bin||'');
  mesh.userData={
    Material_Status: (
      item.Material_Status ?? item.material_status ?? item.MATERIAL_STATUS ??
      item["Material_Status"] ?? item["Material Status"] ?? item["MATERIAL STATUS"] ?? ''
    ),
    bin:binCode, items:assignedByUpper[binCode]||[],
    plate_id:item.plate_id, type:item.type, status:item.status,
    length:item.length, width:item.width, thickness:item.thickness,
    pieces:item.pieces, grade:item.grade, customer:item.customer, weight:item.weight,
    FI_Rel_text: item.FI_Rel_text ?? item.fi_rel_text ?? '',
    SBU_RelStatus: item.SBU_RelStatus ?? item.sbu_rel_status ?? '',
    CustomerCity: item.CustomerCity ?? item.customer_city ?? '',
    customer: item.customer ?? item.Customer ?? '',
    added_at: item.added_at, updated_at: item.updated_at, created_at: item.created_at, date: item.date
  };
  indexIdToMesh(item.plate_id, mesh);
  itemMeshes.push({mesh,data:item});
  hoverables.push(mesh);

  if(statusColorsToggle?.checked && mesh.material?.color){
    mesh.material.color.setHex(statusColor(item.status));
  }
  scene.add(mesh);
}

function indexIdToMesh(idRaw, mesh) {
  const full = String(idRaw || '').trim();
  if (!full) return;
  const U = full.toUpperCase();
  const COMPACT = U.replace(/[^A-Z0-9]+/g, '');
  const DIGITS = U.replace(/[^0-9]+/g, '');
  const push = (k) => { if (!k) return; (idToMeshes[k] ||= []).push(mesh); };
  push('U:' + U);
  push('C:' + COMPACT);
  if (DIGITS) push('D:' + DIGITS);
}

function trackYardLabelObject(obj){
  if(!obj) return obj;
  __yardLabelObjects.push(obj);
  scene.add(obj);
  return obj;
}
function ensureYardLabelsBuilt(){
  if(__yardLabelsBuilt) return;
  addAxisLabels(labels);
  addBinLabels(zones);
  __yardLabelsBuilt = true;
}
function removeYardLabels(){
  if(!__yardLabelsBuilt) return;
  while(__yardLabelObjects.length){
    const obj = __yardLabelObjects.pop();
    if(obj?.parent) obj.parent.remove(obj);
    if(obj?.element?.remove) obj.element.remove();
  }
  __yardLabelsBuilt = false;
}
function addAxisLabels(labelsArr){
  const chip=(text)=>{ const el=document.createElement('div'); el.className='lbl'; el.setAttribute('data-role','label'); el.textContent=text; return el; };
  labelsArr.forEach(l=>{
    const text=String(l?.text??'').trim(); if(!text) return;
    const lx=Number(l?.x_pos??l?.x), ly=Number(l?.y_pos??l?.y);
    const w =Number(l?.width||0), h =Number(l?.height||0);
    if(!Number.isFinite(lx)||!Number.isFinite(ly)) return;
    const x=(lx+(w?w/2:0))-(YARD_WIDTH/2);
    const z=(ly+(h?h/2:0))-(YARD_HEIGHT/2);
    const y=BIN_WALL_HEIGHT+36;
    const obj=new CSS2DObject(chip(text)); obj.position.set(x,y,z);
    trackYardLabelObject(obj);
  });
}
function addBinLabels(zs){
  const chip=(t)=>{ const el=document.createElement('div'); el.className='lbl'; el.setAttribute('data-role','label'); el.textContent=t; return el; };
  zs.forEach(z=>{
    const raw=String(z?.bin||'').trim(); if(!raw) return;
    const bin=normBin(raw);
    const {x,z:zz}=toCenterEff(z);
    const el = chip(`${bin}`);
    el.dataset.bin = bin;
    el.classList.add('__bin_clickable');
    const obj=new CSS2DObject(el);
    obj.position.set(x, BIN_WALL_HEIGHT+20, zz);
    trackYardLabelObject(obj);
  });
}

function buildShedAndPillars(){
  if(shedGroup){
    scene.remove(shedGroup);
    shedGroup = null;
  }

  shedGroup = new THREE.Group();
  shedGroup.name = 'shedGroup';

  const steelMat = new THREE.MeshStandardMaterial({ color: 0x3b4452, metalness: 0.65, roughness: 0.35 });
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x9aa3af, metalness: 0.25, roughness: 0.65, transparent: true, opacity: 0.75 });

  const colGeo = new THREE.BoxGeometry(COL_W, PILLAR_HEIGHT, COL_D);
  const placedCols  = new Set();
  const placedBeams = new Set();
  const snap = (v)=> Math.round(v * 10) / 10;
  const colKey = (x,z)=> `${snap(x)}|${snap(z)}`;

  function addColumnAt(x, z){
    const k = colKey(x, z);
    if(placedCols.has(k)) return null;
    placedCols.add(k);
    const m = new THREE.Mesh(colGeo, steelMat);
    m.position.set(x, PILLAR_HEIGHT/2, z);
    shedGroup.add(m);
    return m;
  }

  const addBoxBetween = (p1, p2, thicknessY, widthZ, y, mat)=>{
    const x1 = snap(p1.x), z1 = snap(p1.z);
    const x2 = snap(p2.x), z2 = snap(p2.z);
    const a = `${x1},${z1}`, b = `${x2},${z2}`;
    const k = (a < b)
      ? `${a}__${b}__y=${snap(y)}__t=${thicknessY}__w=${widthZ}`
      : `${b}__${a}__y=${snap(y)}__t=${thicknessY}__w=${widthZ}`;

    if(placedBeams.has(k)) return;
    placedBeams.add(k);
    const dx = x2 - x1;
    const dz = z2 - z1;
    const len = Math.sqrt(dx*dx + dz*dz);
    if(len < 1) return;

    const geo = new THREE.BoxGeometry(len, thicknessY, widthZ);
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set((x1+x2)/2, y, (z1+z2)/2);
    const ang = Math.atan2(dz, dx);
    mesh.rotation.y = -ang;
    shedGroup.add(mesh);
  };

  const bayData = [];
  for (const bay of SHED_BAYS) {
    const bayKeys = Object.keys(binCenters).filter(k => k.startsWith(bay));
    if (!bayKeys.length) continue;
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const k of bayKeys) {
      const c = binCenters[k];
      if (!c) continue;
      minX = Math.min(minX, c.x - c.w / 2);
      maxX = Math.max(maxX, c.x + c.w / 2);
      minZ = Math.min(minZ, c.z - c.h / 2);
      maxZ = Math.max(maxZ, c.z + c.h / 2);
    }
    const colXs = [];
    for (let num = PILLAR_START; num <= PILLAR_END; num++) {
      const colPrefix = `${bay}${pad2(num)}`;
      let sample = null;
      for (const k of bayKeys) {
        if (k.startsWith(colPrefix)) { sample = binCenters[k]; break; }
      }
      if (sample) colXs.push(sample.x);
    }
    colXs.sort((a, b) => a - b);
    if (bay === 'AC') {
      colXs.push(maxX);
      colXs.sort((a, b) => a - b);
    }
    const colXsUnique = [];
    for (const x of colXs) {
      if (!colXsUnique.length || Math.abs(x - colXsUnique[colXsUnique.length - 1]) > 0.5) {
        colXsUnique.push(x);
      }
    }
    bayData.push({ bay, bayKeys, minX, maxX, minZ, maxZ, colXs: colXsUnique });
  }

  bayData.sort((a, b) => a.minZ - b.minZ);
  for (let i = 0; i < bayData.length - 1; i++) {
    const A = bayData[i];
    const B = bayData[i + 1];
    const boundaryZ = snap((A.maxZ + B.minZ) / 2);
    A.maxZ = boundaryZ;
    B.minZ = boundaryZ;
  }

  for (const info of bayData) {
    const { bay, bayKeys, minZ, maxZ, colXs } = info;
    const frontZ = minZ;
    const backZ  = maxZ;
    const frames = [];

    for (const x of colXs) {
      addColumnAt(x, frontZ);
      addColumnAt(x, backZ);
      frames.push({ x, front: { x, z: frontZ }, back:  { x, z: backZ  } });
    }

    for (let i = 0; i < frames.length - 1; i++) {
      const a = frames[i], b = frames[i + 1];
      addBoxBetween(a.front, b.front, BEAM_H, BEAM_W, EAVE_Y, steelMat);
      addBoxBetween(a.back,  b.back,  BEAM_H, BEAM_W, EAVE_Y, steelMat);
    }

    for (const f of frames) {
      addBoxBetween(f.front, f.back, BEAM_H, BEAM_W, EAVE_Y, steelMat);
    }

    const ridgeZ = (frontZ + backZ) / 2;
    const ridgeY = EAVE_Y + RIDGE_RISE;
    const roofMinX = Math.min(...colXs) - ROOF_OVERHANG_X;
    const roofMaxX = Math.max(...colXs) + ROOF_OVERHANG_X;
    const leftZ  = frontZ - ROOF_OVERHANG_Z;
    const rightZ = backZ  + ROOF_OVERHANG_Z;

    {
      const wX = (roofMaxX - roofMinX);
      const wZ = (ridgeZ - leftZ);
      const geo = new THREE.PlaneGeometry(wX, wZ);
      const roof = new THREE.Mesh(geo, roofMat);
      roof.position.set((roofMinX + roofMaxX) / 2, (EAVE_Y + ridgeY) / 2, (leftZ + ridgeZ) / 2);
      roof.rotation.x = -Math.PI / 2;
      const rise = ridgeY - EAVE_Y;
      const run  = (ridgeZ - leftZ);
      roof.rotation.z = Math.atan2(rise, run);
      shedGroup.add(roof);
    }
    {
      const wX = (roofMaxX - roofMinX);
      const wZ = (rightZ - ridgeZ);
      const geo = new THREE.PlaneGeometry(wX, wZ);
      const roof = new THREE.Mesh(geo, roofMat);
      roof.position.set((roofMinX + roofMaxX) / 2, (EAVE_Y + ridgeY) / 2, (ridgeZ + rightZ) / 2);
      roof.rotation.x = -Math.PI / 2;
      const rise = ridgeY - EAVE_Y;
      const run  = (rightZ - ridgeZ);
      roof.rotation.z = -Math.atan2(rise, run);
      shedGroup.add(roof);
    }
  }

  scene.add(shedGroup);
  const cb = document.getElementById('showShed');
  if(cb) shedGroup.visible = !!cb.checked;
}

function onClick(e){
  if(e.target && e.target.closest){
    if(e.target.closest('#search-details') ||
       e.target.closest('#search-mini-details') ||
       e.target.closest('#mixed-lot-panel') ||
       e.target.closest('#dispatchDrawer') ||
       e.target.closest('#dispatchDrawerBtn') ||
       e.target.closest('#dispatchBackdrop') ||
       e.target.closest('#bin-detail-panel') ||
       e.target.closest('#filters') ||
       e.target.closest('#tools-card')){
      return;
    }
  }

  raycaster.setFromCamera(mouse,camera);
  const hits=raycaster.intersectObjects(hoverables,true);
  if(hits.length){
    const obj=hits[0].object;
    if(pinned===obj){ pinned=null; hideTooltip(); }
    else{
      pinned=obj;
      const bin=String(obj.userData.bin||'').toUpperCase();
      const items=obj.userData.items||assignedByUpper[bin]||[];
      showTooltip(bin,items);
    }
    return;
  }

  const els = document.elementsFromPoint(lastMouseX,lastMouseY);
  const RX = /(EF|AC)\d{2}[A-G]/i;
  for(const el of els){
    if(el && el.getAttribute && el.getAttribute('data-role')==='label'){
      const txt = (el.textContent || '').trim();
      const m = txt.match(RX);
      if(m && window.__openBinDetail){
        window.__openBinDetail(m[0].toUpperCase());
        return;
      }
    }
  }
  pinned=null; hideTooltip();
}

function positionTooltip(evt){
  const el = document.getElementById('tooltip'); if(!el) return;
  const pad = 16;
  const vw = window.innerWidth, vh = window.innerHeight;
  const rect = el.getBoundingClientRect();
  let x = evt.clientX + 12, y = evt.clientY + 12;
  x = Math.max(pad, Math.min(x, vw - rect.width - pad));
  y = Math.max(pad, Math.min(y, vh - rect.height - pad));
  el.style.left = x + 'px';
  el.style.top  = y + 'px';
}

function fmtDim(r){
  const L=r.length||'', W=r.width||'', T=r.thickness||'';
  if (!L && !W && !T) return '';
  return [L,W,T].filter(Boolean).join('×');
}

function showItemTooltip(u){
  const el=document.getElementById('tooltip');
  const bin = (u.bin || '').toString().toUpperCase();
  const rowHtml = `
    <tr>
      <td>${bin ? bin.slice(-1) : ''}</td>
      <td>${(u.plate_id||'').toString().toUpperCase()}</td>
      <td>${u.type||''}</td>
      <td>${u.status||''}</td>
      <td>${u.customer||''}</td>
      <td>${fmtDim(u)}</td>
      <td>${u.pieces ?? ''}</td>
      <td>${u.weight ?? ''}</td>
    </tr>`;

  el.innerHTML = `
    <h4>Item: ${(u.plate_id||'').toString().toUpperCase()}</h4>
    <div class="meta">Bin: ${bin} • Row: ${bin ? bin.slice(-1) : '—'}</div>
    <div class="sheet">
      <table>
        <thead>
          <tr>
            <th>Row</th><th>ID</th><th>Type</th><th>Status</th>
            <th>Customer</th><th>L×W×T</th><th>Pieces</th><th>Weight</th>
          </tr>
        </thead>
        <tbody>${rowHtml}</tbody>
      </table>
    </div>`;
  el.style.display='block';
  el.setAttribute('aria-hidden','false');
}

function showTooltip(bin, items){
  const el = document.getElementById('tooltip');
  const filtered = (items || []).filter(passesCurrentFilters);
  const sorted = filtered.slice().sort((a, b) => {
    const pa = getStackPositionForItem({ ...a, bin, item_id: a.item_id ?? a.plate_id ?? a.id ?? '' });
    const pb = getStackPositionForItem({ ...b, bin, item_id: b.item_id ?? b.plate_id ?? b.id ?? '' });
    const ta = parseInt(pa?.top, 10);
    const tb = parseInt(pb?.top, 10);
    const A = Number.isFinite(ta) ? ta : 1e9;
    const B = Number.isFinite(tb) ? tb : 1e9;
    if (A !== B) return A - B;
    const ida = String(a.plate_id ?? a.item_id ?? a.id ?? '').toUpperCase();
    const idb = String(b.plate_id ?? b.item_id ?? b.id ?? '').toUpperCase();
    return ida.localeCompare(idb);
  });

  const rows = sorted.map(it => `
    <tr>
      <td>${bin.slice(-1)}</td>
      <td>${(it.plate_id||'').toString().toUpperCase()}</td>
      <td>${(it.type||'')}</td>
      <td>${(it.status||'')}</td>
      <td>${(it.customer||'')}</td>
      <td>${fmtDim(it)}</td>
      <td>${it.pieces||''}</td>
      <td>${it.weight||''}</td>
    </tr>
  `).join('') || `<tr><td colspan="8" style="color:var(--text-muted)">No items in this bin.</td></tr>`;

  el.innerHTML = `
    <h4>Bin: ${bin}</h4>
    <div class="meta">Bay: ${bin.slice(0,2)} • Row: ${bin.slice(-1)} • Items: ${filtered.length}</div>
    <div class="sheet">
      <table>
        <thead>
          <tr>
            <th>Row</th>
            <th>ID</th>
            <th>Type</th>
            <th>Status</th>
            <th>Customer</th>
            <th>L×W×T</th>
            <th>Pieces</th>
            <th>Weight</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
  el.style.display = 'block';
  el.setAttribute('aria-hidden','false');
}

function hideTooltip(){
  const el=document.getElementById('tooltip');
  el.style.display='none';
  el.setAttribute('aria-hidden','true');
}

const detailsBox=document.getElementById('search-details');
const detailsBody=document.getElementById('search-details-body');
const detailsCountEl=document.getElementById('detailsCount');
const searchInput=document.getElementById('searchIds');

const mixedLotBox = document.getElementById('mixed-lot-panel');
let __mixedLotOpen = false;
const mixedLotBody = document.getElementById('mixed-lot-body');
const mixedLotCountEl = document.getElementById('mixedLotCount');

function updateDetailsStickyVars(){
  const box = document.getElementById('search-details');
  if(!box || box.style.display === 'none') return;
  const h3 = box.querySelector('h3');
  if(h3){
    const h = Math.max(1, Math.round(h3.offsetHeight || h3.getBoundingClientRect().height));
    box.style.setProperty('--details-hdr-h', h + 'px');
  }
  const tiles = box.querySelectorAll('.cust-tile');
  tiles.forEach(tile=>{
    const hdr = tile.querySelector('.cust-hdr');
    if(!hdr) return;
    const hh = Math.max(1, Math.round(hdr.offsetHeight || hdr.getBoundingClientRect().height));
    tile.style.setProperty('--cust-hdr-h', hh + 'px');
  });
}

let __detailsResizeObs = null;
function bindDetailsResizeObservers(){
  const box = document.getElementById('search-details');
  if(!box || typeof ResizeObserver === 'undefined') return;
  if(__detailsResizeObs){ __detailsResizeObs.disconnect(); __detailsResizeObs = null; }
  __detailsResizeObs = new ResizeObserver(()=>{ updateDetailsStickyVars(); });
  const h3 = box.querySelector('h3');
  if(h3) __detailsResizeObs.observe(h3);
  box.querySelectorAll('.cust-hdr').forEach(hdr=>__detailsResizeObs.observe(hdr));
}

function bindCustomerTableHeaderSync(){
  const box = document.getElementById('search-details');
  if(!box || box.style.display === 'none') return;
  box.querySelectorAll('.cust-tile').forEach(tile=>{
    const bodyWrap = tile.querySelector('.cust-table-wrap');
    const hdrScroll = tile.querySelector('.cust-colhdr-scroll');
    const hdrInnerTable = tile.querySelector('.cust-colhdr-table');
    if(!bodyWrap || !hdrScroll || !hdrInnerTable) return;
    if(bodyWrap.__hdrSyncBound) return;
    bodyWrap.__hdrSyncBound = true;
    const syncWidth = ()=>{
      try{ hdrInnerTable.style.width = (bodyWrap.scrollWidth) ? bodyWrap.scrollWidth + 'px' : '100%'; }catch(_){}
    };
    let lock = false;
    bodyWrap.addEventListener('scroll', ()=>{
      if(lock) return;
      lock = true;
      hdrScroll.scrollLeft = bodyWrap.scrollLeft;
      lock = false;
    }, { passive:true });
    if(typeof ResizeObserver !== 'undefined'){
      const ro = new ResizeObserver(()=>{ syncWidth(); hdrScroll.scrollLeft = bodyWrap.scrollLeft; });
      ro.observe(bodyWrap);
      ro.observe(hdrInnerTable);
    }
    syncWidth();
    hdrScroll.scrollLeft = bodyWrap.scrollLeft;
  });
}

function setAllShellOpacity(op){
  for(const key in binMeshes){
    for(const m of binMeshes[key]){
      if(!m?.material) continue;
      m.material.opacity = op;
      m.material.transparent = true;
      m.material.depthWrite = false;
      m.material.needsUpdate = true;
    }
  }
}
function restoreShellOpacity(){
  for(const key in binMeshes){
    const arr = binMeshes[key] || [];
    if(arr[0]?.material){
      arr[0].material.opacity = 0.25;
      arr[0].material.depthWrite = false;
      arr[0].material.needsUpdate = true;
    }
  }
}
function hideWallsForFocus(){
  for(const key in binMeshes){
    const arr=binMeshes[key]||[];
    if(arr[0]?.material){ arr[0].material.opacity=0.05; arr[0].material.needsUpdate=true; }
  }
}
function showWallsNormal(){
  restoreShellOpacity();
}

function resetItemStyles(){
  for(const {mesh,data} of itemMeshes){
    const coil = isCoilItem(data);
    mesh.material = coil ? coilMat() : plateMat();
    if(statusColorsToggle?.checked && mesh.material?.color) mesh.material.color.setHex(statusColor(data.status));
    mesh.renderOrder = 0;
    if(mesh.material?.depthTest!==undefined) mesh.material.depthTest=true;
    if(mesh.material?.depthWrite!==undefined) mesh.material.depthWrite=true;
    mesh.material.needsUpdate = true;
  }
}

function reapplyItemColors(){
  const useMaterialStatus = window.__matStatusSelected && window.__matStatusSelected.size > 0;
  if(materialStatusToggle) materialStatusToggle.checked = useMaterialStatus;
  for(const {mesh,data} of itemMeshes){
    if(mesh.material?.color){
      if(useMaterialStatus){
        const ms = (mesh.userData?.Material_Status ?? '').toString();
        const normKey = __ms_norm(ms);
        const selectedKeys = Array.from(window.__matStatusSelected).map(__ms_norm);
        if(selectedKeys.includes(normKey)){
          mesh.material.color.setHex(materialStatusColor(ms));
          mesh.material.opacity = 0.95;
        } else {
          mesh.material.color.setHex(0x888888);
          mesh.material.opacity = 0.18;
        }
        mesh.material.transparent = true;
      }else{
        mesh.material.transparent = true;
        mesh.material.opacity = 0.95;
        mesh.material.color.setHex(
          statusColorsToggle?.checked
            ? statusColor(data.status)
            : (String(data?.status||'').toLowerCase().includes('wip') ? 0xf1c40f : 0x1f85de)
        );
      }
    }
    mesh.material.needsUpdate = true;
  }
}

function highlightMeshes(meshes) {
  const matched = [];
  const seen = new Set();
  const add = (m) => {
    if (!m || seen.has(m)) return;
    seen.add(m);
    matched.push(m);
    if (m.material?.color) m.material.color.setHex(0xff0000);
    if (m.material?.opacity !== undefined) m.material.opacity = 1.0;
    if ('depthTest' in m.material)  m.material.depthTest  = false;
    if ('depthWrite' in m.material) m.material.depthWrite = false;
    m.renderOrder = 9999;
  };
  meshes.forEach(add);
  return matched;
}

function meshesByCustomer(query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return [];
  const out = [];
  for (const { mesh, data } of itemMeshes) {
    const c = (data?.customer || '').toString().toLowerCase();
    if (c && c.includes(q)) out.push(mesh);
  }
  return out;
}

function foundListFromMeshes(meshes) {
  const seen = new Set();
  const out  = [];
  for (const m of meshes) {
    const u = m.userData || {};
    const key = (u.plate_id || '') + '|' + (u.bin || '');
    if (seen.has(key)) continue; seen.add(key);
    out.push({
      item_id:   u.plate_id,
      plate_id:  u.plate_id,
      type:      u.type,
      status:    u.status,
      bin:       u.bin,
      length:    u.length,
      width:     u.width,
      thickness: u.thickness,
      pieces:    u.pieces,
      grade:     u.grade,
      customer:  u.customer,
      weight:    u.weight,
      FI_Rel_text: u.FI_Rel_text,
      SBU_RelStatus: u.SBU_RelStatus,
      CustomerCity: u.CustomerCity,
      added_at: u.added_at,
      created_at: u.created_at,
      updated_at: u.updated_at,
      date: u.date,
    });
  }
  return out;
}

async function doSearch() {
  const raw = (searchInput?.value || '').trim();
  if (!raw) {
    clearHighlights();
    hideDetails();
    hideMixedLotPanel();
    fitCamera();
    showWallsNormal();
    updateDashboard();
    return;
  }

  const tokens = raw.split(/[,\s]+/).map(t => t.trim()).filter(Boolean);
  const isIdToken = (t) => /\d/.test(t);
  const idTokens       = tokens.filter(isIdToken);
  const customerTokens = tokens.filter(t => !isIdToken(t));

  let serverFound = [];
  let serverMissing = [];
  try {
    const res = await fetch('/api/locate?q=' + encodeURIComponent(raw), { credentials: 'same-origin' });
    if (res.ok) {
      const data = await res.json();
      serverFound   = Array.isArray(data.found)   ? data.found   : [];
      serverMissing = Array.isArray(data.missing) ? data.missing : [];
    }
  } catch (_) { }

  clearHighlights();
  hideWallsForFocus();
  setAllShellOpacity(0.05);

  const bins = [];
  const idStrings = [];

  for (const f of serverFound) {
    const b = String(f.bin || '').toUpperCase();
    if (b) { bins.push(b); markBin(b); }
    const idAny = f.item_id ?? f.plate_id ?? f.id;
    if (idAny) idStrings.push(String(idAny));
  }

  if (!idStrings.length && idTokens.length) {
    idTokens.forEach(t => idStrings.push(t));
  }

  let focused = false;
  let allMatchedMeshes = [];

  if (idStrings.length) {
    const matchedById = highlightIDs(idStrings);
    allMatchedMeshes.push(...matchedById);
    if (matchedById.length) {
      focusOnItems(matchedById);
      focused = true;
    }
  }

  let customerMeshes = [];
  for (const cust of customerTokens) {
    const m = meshesByCustomer(cust);
    customerMeshes.push(...m);
    m.forEach(mesh => {
      const b = String(mesh?.userData?.bin || '').toUpperCase();
      if (b) markBin(b);
    });
  }
  if (customerMeshes.length) {
    customerMeshes = highlightMeshes(customerMeshes);
    allMatchedMeshes.push(...customerMeshes);
    if (!focused) {
      focusOnItems(customerMeshes);
      focused = true;
    }
  }

  if (!focused && bins.length) {
    focusOnBins([...new Set(bins)]);
  }

  let detailFound = serverFound.slice();
  if (!detailFound.length && allMatchedMeshes.length) {
    detailFound = foundListFromMeshes(allMatchedMeshes);
  }

  const missing = serverMissing || [];
  if (!detailFound.length && customerTokens.length && !allMatchedMeshes.length) {
    missing.push(...customerTokens);
  }

  hideMixedLotPanel();
  hideDetails();
  showSearchMiniDetails(detailFound);
  updateDashboard();
}

const miniBox  = document.getElementById('search-mini-details');
const miniBody = document.getElementById('search-mini-body');
const miniTitleEl = document.getElementById('miniTitle');

function hideSearchMiniDetails(){
  if(miniBody) miniBody.innerHTML = '';
  if(miniBox) miniBox.style.display = 'none';
}

function showSearchMiniDetails(foundList){
  const list = Array.isArray(foundList) ? foundList : [];
  if(!list.length){
    hideSearchMiniDetails();
    return;
  }
  const it = list[0] || {};
  const id   = (it.item_id || it.plate_id || it.id || '').toString().toUpperCase();
  const bin  = String(it.bin || '').toUpperCase();
  const type = String(it.type || '');
  const cust = String(it.customer ?? it.Customer ?? it.CUSTOMER ?? 'Unknown');
  const city = String(it.CustomerCity ?? it.customer_city ?? it.CUSTOMERCITY ?? '');
  const pos  = (typeof getStackPositionForItem === 'function') ? getStackPositionForItem({ ...it, bin }) : { top:'—', bottom:'—' };

  if(miniTitleEl) miniTitleEl.textContent = `Details (Search)`;

  const esc = (s)=>String(s ?? '').replace(/</g,'&lt;');

  if(miniBody){
    miniBody.innerHTML = `
      <div class="mini-table-wrap">
        <table class="mini-details-table">
          <thead>
            <tr>
              <th>ID</th><th>Customer</th><th>City</th><th>Bin</th><th>Type</th><th>From Top</th><th>From Bottom</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class="mono">${esc(id)}</td>
              <td>${esc(cust)}</td>
              <td>${esc(city || '—')}</td>
              <td class="mono">${esc(bin)}</td>
              <td>${esc(type || '—')}</td>
              <td class="mono">${esc(pos.top)}</td>
              <td class="mono">${esc(pos.bottom)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  }
  if(miniBox) miniBox.style.display = 'block';
}

function hideDetails(){
  if(detailsBody) detailsBody.innerHTML='';
  if(detailsBox) detailsBox.style.display='none';
  if(detailsCountEl) detailsCountEl.textContent='';
  window.__lastCustomerWiseFound = [];
  hideSearchMiniDetails();
}

(function bindMiniClose(){
  const b = document.getElementById('miniCloseBtn');
  if(!b || b.__bound) return;
  b.__bound = true;
  const closeOnly = (e)=>{
    e.preventDefault();
    e.stopImmediatePropagation();
    e.stopPropagation();
    hideSearchMiniDetails();
    return false;
  };
  b.addEventListener('pointerdown', closeOnly, true);
  b.addEventListener('mousedown', closeOnly, true);
  b.addEventListener('click', closeOnly, true);
})();

function computeMixedLotBins(limit = 100){
  const binMap = new Map();
  const custBinCounts = new Map();
  const normBin = (v)=>String(v||'').toUpperCase().trim();
  const normCust = (u)=>String(u.customer ?? u.Customer ?? u.CUSTOMER ?? '').trim() || 'Unknown';

  for (const { mesh } of itemMeshes){
    if (!mesh || mesh.visible !== true) continue;
    const u = mesh.userData || {};
    const bin = normBin(u.bin);
    if (!bin) continue;
    const cust = normCust(u);
    let rec = binMap.get(bin);
    if (!rec){
      rec = { bin, customers: new Set(), items: [] };
      binMap.set(bin, rec);
    }
    rec.customers.add(cust);
    rec.items.push({
      bin, plate_id: u.plate_id, type: u.type, status: u.status, customer: cust,
      CustomerCity: u.CustomerCity, FI_Rel_text: u.FI_Rel_text, SBU_RelStatus: u.SBU_RelStatus,
      pieces: u.pieces, weight: u.weight, length: u.length, width: u.width, thickness: u.thickness,
      added_at: u.added_at, created_at: u.created_at, updated_at: u.updated_at, date: u.date
    });

    let bm = custBinCounts.get(cust);
    if (!bm){
      bm = new Map();
      custBinCounts.set(cust, bm);
    }
    bm.set(bin, (bm.get(bin) || 0) + 1);
  }

  function dominantBinForCustomer(cust){
    const bm = custBinCounts.get(cust);
    if (!bm) return null;
    let bestBin = null, bestCount = -1;
    for (const [b, c] of bm.entries()){
      if (c > bestCount){
        bestCount = c; bestBin = b;
      } else if (c === bestCount && bestBin && String(b).localeCompare(bestBin) < 0){
        bestBin = b;
      }
    }
    return { bestBin, bestCount, bm };
  }

  let out = [];
  for (const rec of binMap.values()){
    const customers = Array.from(rec.customers).filter(x => String(x).trim() !== '');
    const customerCount = new Set(customers).size;
    if (customerCount > 1){
      const suggestions = [];
      for (const cust of customers){
        const dom = dominantBinForCustomer(cust);
        if (!dom || !dom.bestBin) continue;
        const currentCount = (dom.bm.get(rec.bin) || 0);
        const bestBin = dom.bestBin;
        const bestCount = dom.bestCount;
        if (bestBin !== rec.bin && bestCount > currentCount){
          suggestions.push({
            customer: cust, fromBin: rec.bin, toBin: bestBin, currentCount, targetCount: bestCount
          });
        }
      }
      out.push({ bin: rec.bin, customerCount, itemCount: rec.items.length, customers, items: rec.items, suggestions });
    }
  }

  out.sort((a,b)=> (b.customerCount - a.customerCount) || (b.itemCount - a.itemCount) || a.bin.localeCompare(b.bin));
  return out.slice(0, limit);
}

function hideMixedLotPanel(){
  __mixedLotOpen = false;
  if(mixedLotBody) mixedLotBody.innerHTML = '';
  if(mixedLotBox) mixedLotBox.style.display = 'none';
  if(mixedLotCountEl) mixedLotCountEl.textContent = '';
  window.__lastMixedLotBins = [];
}

function showMixedLotPanel(){
  __mixedLotOpen = true;
  hideDetails();
  const bins = computeMixedLotBins();
  window.__lastMixedLotBins = bins.slice();

  if(!bins.length){
    if(mixedLotCountEl) mixedLotCountEl.textContent = `(0 bins • 0 items)`;
    if(mixedLotBody){
      mixedLotBody.innerHTML = `
        <div class="ml-bin">
          <div class="ml-hdr">
            <div>
              <strong>No mixed-lot bins found</strong>
              <div class="meta">No bin has more than 1 customer (based on current visible/base filters).</div>
            </div>
          </div>
        </div>
      `;
    }
    if(mixedLotBox) mixedLotBox.style.display = 'block';
    return;
  }

  const totalItems = bins.reduce((a,b)=>a + (b.itemCount||0), 0);
  if(mixedLotCountEl) mixedLotCountEl.textContent = `(${bins.length} bins • ${totalItems} items)`;

  const esc = (s)=>String(s??'').replace(/</g,'&lt;');

  const html = bins.map((b, idx)=>{
    const customers = (b.customers||[]).slice().sort((a,c)=>a.localeCompare(c));
    const MAX_CHIPS = 8;
    const chips = customers.slice(0, MAX_CHIPS).map(c => `<span class="ml-chip" title="${esc(c)}">${esc(c)}</span>`).join('');
    const remaining = Math.max(0, customers.length - MAX_CHIPS);
    const hiddenChips = customers.slice(MAX_CHIPS).map(c => `<span class="ml-chip" title="${esc(c)}">${esc(c)}</span>`).join('');

    const customersUI = customers.length ? `
      <div class="ml-cust-wrap">
        <span class="ml-muted" style="font-weight:700;">Customers:</span>
        ${chips}
        ${remaining ? `<button type="button" class="ml-more-btn" data-ml-act="custmore" aria-expanded="false">+${remaining} more</button>` : ``}
        <div class="ml-cust-hidden">
          ${hiddenChips}
        </div>
      </div>
    ` : `
      <div class="ml-cust-wrap">
        <span class="ml-muted" style="font-weight:700;">Customers:</span>
        <span class="ml-muted">—</span>
      </div>
    `;

    const MAX_ROWS = 400;
    const rows = (b.items||[]).slice(0, MAX_ROWS).map(it=>{
      const id = esc((it.plate_id||'').toString());
      const ty = esc(it.type||'');
      const st = esc(it.status||'');
      const cu = esc(it.customer||'Unknown');
      const city = esc(it.CustomerCity||'');
      const fi = esc(it.FI_Rel_text||'');
      const sbu = esc(it.SBU_RelStatus||'');
      const wt = esc(it.weight ?? '');
      const pcs = esc(it.pieces ?? '');
      const dim = esc([it.length,it.width,it.thickness].filter(v=>v!==undefined && v!==null && String(v).trim()!=='').join('×'));
      const pos = getStackPositionForItem({ ...it, item_id: it.plate_id, bin: b.bin });
      return `
        <tr>
          <td class="mono">${id}</td>
          <td>${cu}</td>
          <td>${city}</td>
          <td>${ty}</td>
          <td>${st}</td>
          <td>${fi}</td>
          <td>${sbu}</td>
          <td class="mono">${esc(pos.top)}</td>
          <td class="mono">${esc(pos.bottom)}</td>
          <td>${dim}</td>
          <td class="mono">${pcs}</td>
          <td class="mono">${wt}</td>
        </tr>
      `;
    }).join('');

    const extraNote = (b.items||[]).length > MAX_ROWS
      ? `<div class="ml-muted" style="padding:10px 12px;">Showing first ${MAX_ROWS} items only for performance.</div>`
      : '';
    const ids = (b.items||[]).map(x=>x.plate_id).filter(Boolean).join(' ');

    return `
      <div class="ml-bin" data-ml-bin="${esc(b.bin)}">
        <div class="ml-hdr">
          <div class="ml-head-main">
            <div class="ml-bin-title">${esc(b.bin)}</div>
            <div class="ml-bin-summary"><b>${b.customerCount}</b> customers • <b>${b.itemCount}</b> items</div>
            ${customersUI}
          </div>
          <div class="ml-actions">
            <button class="primary" data-ml-act="focus" data-bin="${esc(b.bin)}">Focus Bin</button>
            <button data-ml-act="openbin" data-bin="${esc(b.bin)}">Open Details</button>
            <button data-ml-act="locate" data-ids="${esc(ids)}">Locate IDs</button>
            <button data-ml-act="copy" data-ids="${esc(ids)}">Copy IDs</button>
          </div>
        </div>
        ${(()=>{
          const sug = (b.suggestions || []);
          if(!sug.length){
            return `
              <div class="ml-suggest">
                <b>Suggestions:</b>
                <div class="ml-sug-row why">No stronger single-bin match found for any customer in this bin (based on current visible/base filters).</div>
              </div>
            `;
          }
          const rows = sug.slice().sort((x,y)=> (y.targetCount - x.targetCount) || String(x.customer).localeCompare(String(y.customer))).map(s => `
            <div class="ml-sug-row">
              <div class="ml-sug-text">
                Move <b>${esc(s.customer)}</b> plates to <span class="to">${esc(s.toBin)}</span>
                <span class="why">(there: ${esc(s.targetCount)} plates, here: ${esc(s.currentCount)})</span>
              </div>
              <button class="ml-sug-btn" data-ml-act="focus" data-bin="${esc(s.toBin)}" title="Focus camera on target bin ${esc(s.toBin)}">
                Focus target bin
              </button>
            </div>
          `).join('');
          return `<div class="ml-suggest"><b>Suggestions:</b>${rows}</div>`;
        })()}
        <div class="ml-table-wrap">
          <table class="ml-table">
            <thead>
              <tr>
                <th style="width:140px;">ID</th><th style="width:220px;">Customer</th><th style="width:140px;">City</th>
                <th style="width:110px;">Type</th><th style="width:150px;">Status</th><th style="width:160px;">FI</th>
                <th style="width:140px;">SBU</th><th style="width:90px;">From Top</th><th style="width:110px;">From Bottom</th>
                <th style="width:140px;">L×W×T</th><th style="width:70px;">Pcs</th><th style="width:90px;">Wt</th>
              </tr>
            </thead>
            <tbody>${rows || `<tr><td colspan="12" class="ml-muted" style="padding:10px 12px;">No items.</td></tr>`}</tbody>
          </table>
        </div>
        ${extraNote}
      </div>
    `;
  }).join('');

  if(mixedLotBody) mixedLotBody.innerHTML = html;
  if(mixedLotBox) mixedLotBox.style.display = 'block';

  if(mixedLotBody && !mixedLotBody.__mlBound){
    mixedLotBody.__mlBound = true;
    const copyToClipboard = (text)=>{
      try{ navigator.clipboard.writeText(text); }
      catch(e){
        const ta=document.createElement('textarea');
        ta.value=text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      }
    };

    mixedLotBody.addEventListener('click', (ev)=>{
      const btn = ev.target.closest('button[data-ml-act]');
      if(!btn) return;
      ev.preventDefault();
      ev.stopPropagation();

      const act = btn.getAttribute('data-ml-act');
      const bin = (btn.getAttribute('data-bin') || '').toUpperCase();
      const ids = btn.getAttribute('data-ids') || '';

      if(act === 'custmore'){
        const binCard = btn.closest('.ml-bin');
        if(!binCard) return;
        const hidden = binCard.querySelector('.ml-cust-hidden');
        if(!hidden) return;
        const isOpen = hidden.classList.toggle('open');
        btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        btn.textContent = isOpen ? 'Show less' : btn.textContent;
        if(!isOpen){
          const total = (binCard.querySelectorAll('.ml-cust-hidden .ml-chip') || []).length;
          btn.textContent = total ? `+${total} more` : 'Show more';
        }
        return;
      }
      if(act === 'focus'){
        if(bin){
          clearHighlights();
          hideDetails();
          setAllShellOpacity(0.05);
          hideWallsForFocus();
          markBin(bin);
          focusOnBins([bin]);
        }
      }else if(act === 'openbin'){
        if(bin && window.__openBinDetail){
          window.__openBinDetail(bin);
        }
      }else if(act === 'locate'){
        const searchBox = document.getElementById('searchIds');
        const searchBtn = document.getElementById('searchBtn');
        if(searchBox && searchBtn){
          searchBox.value = ids;
          searchBtn.click();
        }else{
          copyToClipboard(ids);
          alert('Search box not found; IDs copied instead.');
        }
      }else if(act === 'copy'){
        copyToClipboard(ids);
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(()=>btn.textContent = orig, 900);
      }
    }, { passive:false });
  }
}

function getStackPositionForItem(it){
  const pid = normId(it?.item_id ?? it?.plate_id ?? it?.id ?? '');
  const bin = String(it?.bin || '').toUpperCase();
  const ty = String(it?.type || '').toLowerCase();
  const isCoil = ty.includes('coil');

  if(isCoil){
    const lvl = (coilLevel[pid] !== undefined) ? coilLevel[pid] : null;
    const max = (coilMaxLevel[bin] !== undefined) ? coilMaxLevel[bin] : null;
    if(lvl === null || max === null) return { top:'—', bottom:'—' };
    return { bottom: String(lvl + 1), top: String(max - lvl) };
  }

  const idx = (plateStackIndex[pid] !== undefined) ? plateStackIndex[pid] : null;
  const total = (plateStackTotal[bin] !== undefined) ? plateStackTotal[bin] : null;
  if(idx === null || total === null) return { top:'—', bottom:'—' };
  return { bottom: String(idx + 1), top: String(total - idx) };
}

function getCurrentFilterSnapshot(){
  const sVals = getSelectedValues(document.getElementById('statusFilter'));
  const dVals = getSelectedValues(document.getElementById('dateFilter'));
  const from  = document.getElementById('startDate')?.value ?? '';
  const to    = document.getElementById('endDate')?.value ?? '';
  const fi    = getSelectedValues(document.getElementById('fiRelFilter'));
  const sbu   = getSelectedValues(document.getElementById('sbuRelFilter'));
  const cust  = getSelectedValues(document.getElementById('customerFilter'));
  const city  = getSelectedValues(document.getElementById('customerCityFilter'));
  const top   = getSelectedValues(document.getElementById('topStackFilter'));
  const tl    = document.getElementById('timeline')?.value ?? '';
  const q     = document.getElementById('searchIds')?.value ?? '';
  return { status: sVals, dateBucket: dVals, from, to, fi, sbu, customer: cust, city, topStack: top, timeline: tl, search: q };
}

function showDetailsCustomerWise(found=[], missing=[], titlePrefix='Details'){
  const list = Array.isArray(found) ? found : [];
  if((!list.length) && (!missing || !missing.length)){
    hideDetails();
    return;
  }
  window.__lastCustomerWiseFound = list.slice();
  window.__lastCustomerWiseMissing = Array.isArray(missing) ? missing.slice() : [];
  window.__lastCustomerWiseTitle = titlePrefix;
  window.__lastCustomerWiseFilters = getCurrentFilterSnapshot();
  window.__lastCustomerWiseExportedAt = new Date().toISOString();

  const groups = {};
  for(const it of list){
    const cust = String(it.customer ?? it.Customer ?? it.CUSTOMER ?? 'Unknown').trim() || 'Unknown';
    (groups[cust] ||= []).push(it);
  }

  const customers = Object.keys(groups).sort((a,b)=>a.localeCompare(b));
  const totalItems = list.length;
  if(detailsCountEl) detailsCountEl.textContent = `(${customers.length} customers • ${totalItems} items)`;

  let html = '';
  const headerCols = `
    <colgroup>
      <col class="col-id"><col class="col-bin"><col class="col-type"><col class="col-status">
      <col class="col-fi"><col class="col-sbu"><col class="col-top"><col class="col-bottom">
      <col class="col-dim"><col class="col-pcs"><col class="col-wt">
    </colgroup>
  `;
  const headerRow = `
    <thead>
      <tr>
        <th>ID</th><th>Bin</th><th>Type</th><th>Status</th><th>FI</th><th>SBU</th>
        <th>From Top</th><th>From Bottom</th><th>L×W×T</th><th>Pcs</th><th>Wt</th>
      </tr>
    </thead>
  `;

  for(const cust of customers){
    const items = groups[cust] || [];
    const count = items.length;
    const cities = Array.from(new Set(items.map(x=>String(x.CustomerCity||'').trim()).filter(Boolean))).sort((a,b)=>a.localeCompare(b));
    const cityText = cities.length ? cities.join(', ') : '—';
    const rows = items.map(it=>{
      const id = (it.item_id||it.plate_id||'').toString();
      const bin = String(it.bin||'').toUpperCase();
      const st  = String(it.status||'');
      const ty  = String(it.type||'');
      const fi  = String(it.FI_Rel_text||it.fi_text||'');
      const sbu = String(it.SBU_RelStatus||'');
      const wt  = (it.weight ?? '');
      const dim = [it.length, it.width, it.thickness].filter(v=>v!==undefined && v!==null && String(v).trim()!=='').join('×') || '';
      const pcs = (it.pieces ?? '');
      const pos = getStackPositionForItem(it);
      return `
        <tr>
          <td class="mono">${id.replace(/</g,'&lt;')}</td>
          <td class="mono">${bin.replace(/</g,'&lt;')}</td>
          <td>${ty.replace(/</g,'&lt;')}</td>
          <td>${st.replace(/</g,'&lt;')}</td>
          <td>${fi.replace(/</g,'&lt;')}</td>
          <td>${sbu.replace(/</g,'&lt;')}</td>
          <td class="mono">${pos.top}</td>
          <td class="mono">${pos.bottom}</td>
          <td>${dim.replace(/</g,'&lt;')}</td>
          <td>${pcs}</td>
          <td>${wt}</td>
        </tr>
      `;
    }).join('');
    html += `
      <div class="cust-tile">
        <div class="cust-hdr">
          <div>
            <strong>${cust.replace(/</g,'&lt;')}</strong>
            <div class="meta">Cities: ${cityText.replace(/</g,'&lt;')}</div>
          </div>
          <div class="meta"><b>${count}</b> item${count===1?'':'s'}</div>
        </div>
        <div class="cust-body">
          <div class="cust-table-wrap">
            <table class="cust-table">
              ${headerCols}
              ${headerRow}
              <tbody>${rows}</tbody>
            </table>
          </div>
          <div class="muted" style="margin-top:8px; font-size:11px;">
            Stack position is computed per-bin from the 3D stacking (plates) / coil tiers (coils).
          </div>
        </div>
      </div>
    `;
  }

  if(missing?.length){
    html += `<div class="result"><em class="muted">Not found: ${missing.join(', ').replace(/</g,'&lt;')}</em></div>`;
  }
  if(detailsBody) detailsBody.innerHTML = html;
  if(detailsBox) detailsBox.style.display = 'block';

  requestAnimationFrame(()=>{
    requestAnimationFrame(()=>{
      updateDetailsStickyVars();
      bindDetailsResizeObservers();
      bindCustomerTableHeaderSync();
    });
  });
}

function highlightIDs(idsRaw) {
  const matchedMeshes = [];
  const seen = new Set();
  const add = (m) => {
    if (seen.has(m)) return;
    seen.add(m);
    matchedMeshes.push(m);
    if (m.material?.color) m.material.color.setHex(0xff0000);
    if (m.material?.opacity !== undefined) m.material.opacity = 1.0;
    if ('depthTest' in m.material)  m.material.depthTest  = false;
    if ('depthWrite' in m.material) m.material.depthWrite = false;
    m.renderOrder = 9999;
  };
  for (const raw of idsRaw) {
    const s = String(raw || '').trim();
    if (!s) continue;
    const isDigits = /^\d+$/.test(s);
    if (isDigits) {
      const arr = idToMeshes['D:' + s] || [];
      arr.forEach(add);
    } else {
      const U = s.toUpperCase();
      const C = U.replace(/[^A-Z0-9]+/g, '');
      (idToMeshes['U:' + U] || []).forEach(add);
      (idToMeshes['C:' + C] || []).forEach(add);
    }
  }
  return matchedMeshes;
}

function markBin(bin){
  const B=String(bin||'').toUpperCase(); if(!B || highlightRings[B]) return;
  const c=binCenters[B]; if(!c) return;
  const outer=Math.min(c.w,c.h)*0.45, inner=outer*0.68;
  const ring=new THREE.Mesh(new THREE.RingGeometry(inner,outer,24), new THREE.MeshBasicMaterial({color:0x00c2a8,transparent:true,opacity:0.95,side:THREE.DoubleSide}));
  ring.rotation.x=-Math.PI/2; ring.position.set(c.x,(c.y||1)+2,c.z); scene.add(ring); highlightRings[B]=ring;
}

function clearHighlights(){
  for(const k in highlightRings){ scene.remove(highlightRings[k]); delete highlightRings[k]; }
  resetItemStyles();
  showWallsNormal();
  hideDetails();
}

function focusOnItems(meshes){
  if(!meshes.length) return;
  let minX=Infinity,minY=Infinity,minZ=Infinity,maxX=-Infinity,maxY=-Infinity,maxZ=-Infinity;
  for(const m of meshes){ const box=new THREE.Box3().setFromObject(m);
    minX=Math.min(minX,box.min.x); maxX=Math.max(maxX,box.max.x);
    minY=Math.min(minY,box.min.y); maxY=Math.max(maxY,box.max.y);
    minZ=Math.min(minZ,box.min.z); maxZ=Math.max(maxZ,box.max.z); }
  const cx=(minX+maxX)/2, cy=(minY+maxY)/2, cz=(minZ+maxZ)/2;
  const size=Math.max(maxX-minX, maxY-minY, maxZ-minZ);
  const dist=Math.min(8000, Math.max(700, size*2.2));
  tweenCamera(new THREE.Vector3(cx + dist*0.6, cy + dist*0.8, cz + dist*0.6), new THREE.Vector3(cx,cy,cz), 600);
}

function focusOnBins(bins){
  const pts=bins.map(b=>binCenters[String(b).toUpperCase()]).filter(Boolean); if(!pts.length) return;
  let minX=Infinity, minZ=Infinity, maxX=-Infinity, maxZ=-Infinity;
  for(const p of pts){
    minX=Math.min(minX, p.x-p.w/2);
    maxX=Math.max(maxX, p.x+p.w/2);
    minZ=Math.min(minZ, p.z-p.h/2);
    maxZ=Math.max(maxZ, p.z+p.h/2);
  }
  const cx=(minX+maxX)/2, cz=(minZ+maxZ)/2; const size=Math.max(maxX-minX, maxZ-minZ);
  const dist=Math.min(8000, Math.max(700, size * 1.8));
  tweenCamera(new THREE.Vector3(cx + dist*0.66, dist*1.1, cz + dist*0.66), new THREE.Vector3(cx,0,cz), 600);
}

document.getElementById('searchBtn')?.addEventListener('click',doSearch);
document.getElementById('applyBtn')?.addEventListener('click',()=>{ applyFilters(); updateDashboard(); });
document.getElementById('resetBtn')?.addEventListener('click', () => {
  const ms = document.getElementById('materialStatusToggle');
  if(ms) ms.checked = false;
  window.__matStatusSelected = new Set();
  const msOptsList = document.getElementById('materialStatusOptionsList');
  if(msOptsList) msOptsList.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
  const msBadge = document.getElementById('materialStatusBtnBadge');
  if(msBadge){ msBadge.style.display='none'; msBadge.textContent=''; }
  if (typeof setBayFocus === 'function') setBayFocus(null);
  if (typeof __focusedBay !== 'undefined') __focusedBay = null;
  const bfPanel = document.getElementById('bayFocusPanel');
  if (bfPanel) bfPanel.style.display = 'none';
  const bfChip = document.getElementById('bayFocusActiveChip');
  if (bfChip) bfChip.style.display = 'none';
  const searchInput = document.getElementById('searchIds');
  if (searchInput) searchInput.value = '';
  const resetMultiToAll = (sel) => {
    if (!sel) return;
    Array.from(sel.options).forEach(o => { o.selected = (o.value === 'all'); });
    if (!Array.from(sel.options).some(o => o.selected)) {
      if (sel.options[0]) sel.options[0].selected = true;
    }
  };
  resetMultiToAll(document.getElementById('topStackFilter'));
  resetMultiToAll(document.getElementById('statusFilter'));
  resetMultiToAll(document.getElementById('dateFilter'));
  resetMultiToAll(document.getElementById('fiRelFilter'));
  resetMultiToAll(document.getElementById('sbuRelFilter'));
  resetMultiToAll(document.getElementById('customerFilter'));
  resetMultiToAll(document.getElementById('customerCityFilter'));
  const start = document.getElementById('startDate');
  const end   = document.getElementById('endDate');
  if (start) start.value = '';
  if (end) end.value = '';
  if (typeof syncRangeVisibility === 'function') syncRangeVisibility();
  if (typeof timeline !== 'undefined' && timeline) timeline.value = '';
  if (typeof timelineDate !== 'undefined') timelineDate = null;
  if (typeof __customerCityMap !== 'undefined' && typeof populateDropdowns === 'function') {
    populateDropdowns();
    resetMultiToAll(document.getElementById('customerFilter'));
    resetMultiToAll(document.getElementById('customerCityFilter'));
  }
  clearHighlights();
  hideDetails();
  hideMixedLotPanel();
  if (typeof applyFilters === 'function') applyFilters();
  fitCamera();
  updateDashboard();
});

let liveSearchTimer=null;
const searchInputEl=document.getElementById('searchIds');
searchInputEl?.addEventListener('input', () => { clearTimeout(liveSearchTimer); liveSearchTimer = setTimeout(()=>{ doSearch(); updateDashboard(); }, LIVE_SEARCH_DELAY); });
searchInputEl?.addEventListener('keydown',(e)=>{ if(e.key==='Enter'){ clearTimeout(liveSearchTimer); doSearch(); updateDashboard(); } });

const _statusSel=document.getElementById('statusFilter'),
      _dateSel=document.getElementById('dateFilter'),
      _start=document.getElementById('startDate'),
      _end=document.getElementById('endDate');

const norm=(v)=>String(v??'').trim().toLowerCase();
const extractDateString=(it)=>it.date||it.added_at||it.updated_at||it.created_at||it.received_at||null;

function startOf(unit){
  const now=new Date(), t=new Date(now);
  if(unit==='day') t.setHours(0,0,0,0);
  else if(unit==='week'){ const d=(t.getDay()+6)%7; t.setHours(0,0,0,0); t.setDate(t.getDate()-d); }
  else if(unit==='month'){ t.setDate(1); t.setHours(0,0,0,0); }
  else if(unit==='year'){ t.setMonth(0,1); t.setHours(0,0,0,0); }
  return t;
}
function inBucket(dateStr, bucketsArr){
  if(!dateStr) return true;
  if(!Array.isArray(bucketsArr) || bucketsArr.length === 0) return true;
  const d=new Date(dateStr); if(isNaN(d)) return true;
  for(const bucket of bucketsArr){
    if(bucket==='today' && d>=startOf('day')) return true;
    if(bucket==='week'  && d>=startOf('week')) return true;
    if(bucket==='month' && d>=startOf('month')) return true;
    if(bucket==='year'  && d>=startOf('year')) return true;
    if(bucket==='custom') continue;
    if(bucket==='all') return true;
  }
  return false;
}

function inCustomRange(dateStr,fromStr,toStr){
  if(!dateStr) return false;
  const d=new Date(dateStr); if(isNaN(d)) return false;
  let ok=true;
  if(fromStr){ const f=new Date(fromStr); if(!isNaN(f)) ok=ok&&(d>=f); }
  if(toStr){
    const t=new Date(toStr);
    if(!isNaN(t)) ok=ok&&(d<=new Date(t.getFullYear(),t.getMonth(),t.getDate(),23,59,59,999));
  }
  return ok;
}
function statusMatches(itemStatus, wantedArr){
  if(!Array.isArray(wantedArr) || wantedArr.length === 0) return true;
  const s=norm(itemStatus);
  const isFG=s==='fg'||s.includes('finished');
  const isWIP=s.includes('wip')||s.includes('work in progress');
  const isGFA=s.includes('gfa')||s.includes('good for availability');
  const isPSF=s.includes('psfs')||s.includes('prime stock for sale');
  const isRFD=s.includes('ready for dispatch')||s==='rfd'||s.includes('dispatch ready');
  const isREJ=s.includes('rejected')||s==='rej';
  for(const wanted of wantedArr){
    if(wanted==='finished good' && isFG) return true;
    if(wanted==='wip' && isWIP) return true;
    if(wanted==='gfa' && isGFA) return true;
    if(wanted==='psfs' && isPSF) return true;
    if(wanted==='ready for dispatch' && isRFD) return true;
    if(wanted==='rejected' && isREJ) return true;
    if(wanted==='others' && !(isFG||isWIP||isGFA||isPSF||isRFD||isREJ)) return true;
  }
  return false;
}

function extraFiltersMatch(it){
  const wantFiArr   = getSelectedValues(fiSel);
  const wantSbuArr  = getSelectedValues(sbuSel);
  const wantCityArr = getSelectedValues(citySel);
  const wantCustArr = getSelectedValues(custSel);
  const fi   = String(it?.FI_Rel_text ?? it?.fi_rel_text ?? '').trim();
  const sbu  = String(it?.SBU_RelStatus ?? it?.sbu_rel_status ?? '').trim();
  const city = String(it?.CustomerCity ?? it?.customer_city ?? '').trim();
  const cust = String(it?.customer ?? it?.Customer ?? '').trim();
  if(wantFiArr.length   && !wantFiArr.includes(fi))     return false;
  if(wantSbuArr.length  && !wantSbuArr.includes(sbu))   return false;
  if(wantCityArr.length && !wantCityArr.includes(city)) return false;
  if(wantCustArr.length && !wantCustArr.includes(cust)) return false;
  return true;
}

function baseFiltersMatch(it){
  const sVals = getSelectedValues(_statusSel);
  const dVals = getSelectedValues(_dateSel);
  const from=_start?.value||'';
  const to=_end?.value||'';
  const okS = statusMatches(it?.status, sVals);
  const ds  = extractDateString(it || {});
  const wantsCustom = Array.isArray(dVals) && dVals.includes('custom');
  const okD = wantsCustom ? ((from||to) ? inCustomRange(ds,from,to) : true) : inBucket(ds, dVals);
  let okT = true;
  if(timelineDate && ds){
    const d=new Date(ds);
    okT = !isNaN(d) ? (d<=timelineDate) : true;
  }
  return okS && okD && okT;
}

function applyFilters(){
  for(const {mesh} of itemMeshes){
    const u = mesh.userData || {};
    const okBase = baseFiltersMatch(u);
    let okBay = true;
    if(__focusedBay){
      const bay = getBayKeyFromBin(u.bin);
      okBay = (bay === __focusedBay);
    }
    mesh.visible = (okBase && okBay);
  }
  resetItemStyles();
  reapplyItemColors();
  applyHighlightOnly();
  if(__mixedLotOpen){
    showMixedLotPanel();
  }
}

function buildTopPlateIndexForVisible(){
  const topByBin = {};
  for(const {mesh} of itemMeshes){
    if(mesh.visible !== true) continue;
    const u = mesh.userData || {};
    if(!isPlateItem(u)) continue;
    const bin = String(u.bin||'').toUpperCase();
    if(!bin) continue;
    const pid = normId(u.plate_id);
    const idx = (plateStackIndex[pid] !== undefined) ? plateStackIndex[pid] : -Infinity;
    if(topByBin[bin] === undefined || idx > topByBin[bin]) topByBin[bin] = idx;
  }
  return topByBin;
}

function isTopPlateMesh(mesh, topByBin){
  const u = mesh.userData || {};
  if(!isPlateItem(u)) return false;
  const bin = String(u.bin||'').toUpperCase();
  if(!bin) return false;
  const maxIdx = topByBin?.[bin];
  if(maxIdx === undefined) return false;
  const pid = normId(u.plate_id);
  const idx = (plateStackIndex[pid] !== undefined) ? plateStackIndex[pid] : -Infinity;
  return idx === maxIdx;
}

function isFGStatus(it){
  const s = String(it?.status||'').toLowerCase().trim();
  return (s === 'fg' || s.includes('finished'));
}

function parseFiReleased(it){
  const raw = String(it?.FI_Rel_text ?? it?.fi_rel_text ?? '').trim();
  const m = raw.match(/FI\s*RELEASED(?:\s*\((\d+)\))?/i);
  if(!m) return { isReleased:false, version:null, raw };
  const v = (m[1] !== undefined) ? parseInt(m[1],10) : null;
  return { isReleased:true, version: (Number.isFinite(v) ? v : null), raw };
}

function fiRelToHighlightColor(fiText){
  const v = String(fiText || '').trim();
  if (/FI\s*RELEASED\s*\(\s*1\s*\)/i.test(v)) return 0xFF8C00;
  if (/^FI\s*RELEASED(\s*\(\s*2\s*\))?$/i.test(v)) return 0x006400;
  return null;
}

function fiTopModeMatches(it, mode){
  const p = parseFiReleased(it);
  if(!p.isReleased) return false;
  if(mode === 'fg_fi')  return p.version === null;
  if(mode === 'fg_fi2') return p.version === 2;
  return true;
}

function applyHighlightOnly(){
  const wantFiArr   = getSelectedValues(fiSel);
  const wantSbuArr  = getSelectedValues(sbuSel);
  const wantCityArr = getSelectedValues(citySel);
  const wantCustArr = getSelectedValues(custSel);
  const wantTopArr  = getSelectedValues(topStackSel);

  const anyExtra = (wantFiArr.length || wantSbuArr.length || wantCityArr.length || wantCustArr.length || wantTopArr.length);
  resetItemStyles();
  reapplyItemColors();
  if(!anyExtra){ hideDetails(); return; }

  const matches = [];
  const topPlateByBin = (wantTopArr.length) ? buildTopPlateIndexForVisible() : null;

  for(const {mesh} of itemMeshes){
    if(mesh.visible !== true) continue;
    const u = mesh.userData || {};
    if(!extraFiltersMatch(u)) continue;
    if(wantTopArr.length){
      if(!isPlateItem(u)) continue;
      if(!isTopPlateMesh(mesh, topPlateByBin)) continue;
      if(!isFGStatus(u)) continue;
      let okMode = false;
      for(const mode of wantTopArr){
        if(fiTopModeMatches(u, mode)){ okMode = true; break; }
      }
      if(!okMode) continue;
    }
    matches.push(mesh);
  }

  let highlighted = [];
  let fiColor = null;
  if(wantFiArr.length){
    const colors = wantFiArr.map(v => fiRelToHighlightColor(v)).filter(v => v !== null);
    if(colors.length){
      const uniq = Array.from(new Set(colors));
      fiColor = (uniq.length === 1) ? uniq[0] : null;
    }
  }

  if (fiColor !== null) {
    const seen = new Set();
    for (const m of matches) {
      if (!m || seen.has(m)) continue;
      seen.add(m);
      highlighted.push(m);
      if (m.material?.color) m.material.color.setHex(fiColor);
      if (m.material?.opacity !== undefined) m.material.opacity = 1.0;
      if ('depthTest' in m.material)  m.material.depthTest  = false;
      if ('depthWrite' in m.material) m.material.depthWrite = false;
      m.renderOrder = 9999;
      if (m.material) m.material.needsUpdate = true;
    }
  } else {
    highlighted = highlightMeshes(matches);
  }

  const details = foundListFromMeshes(highlighted);
  showDetailsCustomerWise(details, [], (wantTopArr.length) ? 'Top-of-Stack' : 'Filtered');
}

function passesCurrentFilters(it){
  if(!baseFiltersMatch(it)) return false;
  const wantFiArr   = getSelectedValues(fiSel);
  const wantSbuArr  = getSelectedValues(sbuSel);
  const wantCityArr = getSelectedValues(citySel);
  const wantCustArr = getSelectedValues(custSel);
  const wantTopArr  = getSelectedValues(topStackSel);
  const anyExtra = (wantFiArr.length || wantSbuArr.length || wantCityArr.length || wantCustArr.length || wantTopArr.length);
  if(!anyExtra) return true;
  if(!extraFiltersMatch(it)) return false;
  if(wantTopArr.length){
    if(!isPlateItem(it)) return false;
    if(!isFGStatus(it)) return false;
    const topPlateByBin = buildTopPlateIndexForVisible();
    const bin = String(it.bin||'').toUpperCase();
    if(topPlateByBin?.[bin] === undefined) return false;
    let okMode = false;
    for(const mode of wantTopArr){
      if(fiTopModeMatches(it, mode)){ okMode = true; break; }
    }
    if(!okMode) return false;
  }
  return true;
}

function syncRangeVisibility(){
  const rb=document.getElementById('customRange');
  const ds=document.getElementById('dateFilter');
  if(!rb || !ds) return;
  const selected = getSelectedValues(ds);
  rb.style.display = selected.includes('custom') ? 'flex' : 'none';
}

document.getElementById('statusFilter')?.addEventListener('change',()=>{ applyFilters(); updateDashboard(); });
document.getElementById('dateFilter')?.addEventListener('change',()=>{ syncRangeVisibility(); applyFilters(); updateDashboard(); });
document.getElementById('startDate')?.addEventListener('change',()=>{ applyFilters(); updateDashboard(); });
document.getElementById('endDate')?.addEventListener('change',()=>{ applyFilters(); updateDashboard(); });
syncRangeVisibility();

init();
window.__yard3d = { scene, camera, renderer, controls };

function onKeyDown(e){
  const step = 80;
  const zoomStep = 0.9;
  const upStep = 60;
  const pan = (dx, dz)=>{
    const offset = new THREE.Vector3();
    const te = camera.matrix.elements;
    const xAxis = new THREE.Vector3(te[0], te[1], te[2]).normalize();
    const zAxis = new THREE.Vector3(te[8], te[9], te[10]).normalize();
    offset.copy(xAxis).multiplyScalar(dx).add(zAxis.multiplyScalar(dz));
    controls.target.add(offset);
    camera.position.add(offset);
    controls.update();
  };
  switch(e.key){
    case 'ArrowUp': case 'w': case 'W': pan(0,-step); break;
    case 'ArrowDown': case 's': case 'S': pan(0, step); break;
    case 'ArrowLeft': case 'a': case 'A': pan(-step,0); break;
    case 'ArrowRight': case 'd': case 'D': pan( step,0); break;
    case 'q': case 'Q': controls.target.y-=upStep; camera.position.y-=upStep; controls.update(); break;
    case 'e': case 'E': controls.target.y+=upStep; camera.position.y+=upStep; controls.update(); break;
    case 'r': case 'R': {
      const dir=new THREE.Vector3().subVectors(camera.position,controls.target).multiplyScalar(zoomStep);
      camera.position.copy(controls.target.clone().add(dir)); controls.update(); break;
    }
    case 'f': case 'F': {
      const dir=new THREE.Vector3().subVectors(camera.position,controls.target).multiplyScalar(1/zoomStep);
      camera.position.copy(controls.target.clone().add(dir)); controls.update(); break;
    }
    case '0': controls.target.set(0,0,0); goView(new THREE.Vector3(0.6,0.8,0.6)); break;
    case 'l': case 'L': {
      const cb=document.getElementById('showLabels');
      if(cb){ cb.checked=!cb.checked; document.body.classList.toggle('labels-hidden', !cb.checked); if(cb.checked) ensureYardLabelsBuilt(); else removeYardLabels(); }
      break;
    }
    case 'm': case 'M': {
      showMixedLotPanel();
      break;
    }
    default: return;
  }
}

function updateDashboard(){ }

/* ------------------------------------------------------------------
    Top-down 2D orthographic mode
-------------------------------------------------------------------*/
function injectTopDownToggle(){
  const filters=document.getElementById('filters');
  if(!filters) return;
  const wrap=document.createElement('label');
  wrap.className='toggle-labels';
  wrap.style.marginLeft='8px';
  wrap.innerHTML='<input type="checkbox" id="topDown2DToggle"><span>Top-down 2D</span>';
  filters.appendChild(wrap);

  const cb=wrap.querySelector('#topDown2DToggle');
  cb.addEventListener('change',()=>{ cb.checked ? enterTopDown() : exitTopDown(); });
}

function computeContentBounds(){
  let box=null;
  for(const key in binMeshes){
    const arr=binMeshes[key]||[];
    const floor=arr[0];
    if(!floor) continue;
    const b=new THREE.Box3().setFromObject(floor);
    box = box ? box.union(b) : b.clone();
  }
  if(!box){
    box = new THREE.Box3(new THREE.Vector3(-8000,0,-8000), new THREE.Vector3(8000,0,8000));
  }
  return box;
}

function updateOrthoFrustumToContent(){
  const container=document.getElementById('canvas-container');
  if(!container || !orthoCamera) return;
  const bounds=computeContentBounds();
  const w=bounds.max.x - bounds.min.x;
  const h=bounds.max.z - bounds.min.z;
  const margin=1.08;
  const aspect=container.clientWidth/container.clientHeight;
  let viewW=w*margin, viewH=h*margin;
  if(viewW/viewH < aspect){ viewW = viewH*aspect; } else { viewH = viewW/aspect; }
  orthoCamera.left   = -viewW/2;
  orthoCamera.right  =  viewW/2;
  orthoCamera.top    =  viewH/2;
  orthoCamera.bottom = -viewH/2;
  orthoCamera.near = -5000;
  orthoCamera.far  =  5000;
  orthoCamera.updateProjectionMatrix();

  const cx = (bounds.min.x + bounds.max.x)/2;
  const cz = (bounds.min.z + bounds.max.z)/2;
  orthoCamera.position.set(cx, 2000, cz);
  orthoCamera.lookAt(cx,0,cz);
  if(orthoControls){
    orthoControls.target.set(cx,0,cz);
    orthoControls.update();
  }
}

function createOrthoIfNeeded(){
  if(orthoCamera) return;
  orthoCamera=new THREE.OrthographicCamera(-1,1,1,-1,-5000,5000);
  orthoCamera.up.set(0,0,-1);

  orthoControls=new OrbitControls(orthoCamera, renderer.domElement);
  orthoControls.enableDamping = true;
  orthoControls.dampingFactor = 0.12;
  orthoControls.enableRotate = false;
  orthoControls.screenSpacePanning = true;
  orthoControls.enablePan = true;
  orthoControls.mouseButtons.LEFT   = THREE.MOUSE.PAN;
  orthoControls.mouseButtons.MIDDLE = THREE.MOUSE.DOLLY;
  orthoControls.mouseButtons.RIGHT  = THREE.MOUSE.PAN;
  orthoControls.touches.ONE = THREE.TOUCH.PAN;
  orthoControls.touches.TWO = THREE.TOUCH.DOLLY_PAN;

  updateOrthoFrustumToContent();
}

function enterTopDown(){
  if(isTopDown) return;
  isTopDown=true;

  createOrthoIfNeeded();
  updateOrthoFrustumToContent();

  camera = orthoCamera;
  controls = orthoControls;

  for(const key in binMeshes){
    const arr=binMeshes[key]||[];
    if(arr[0]?.material){
      arr[0].material.opacity = 0.12;
      arr[0].material.needsUpdate = true;
    }
  }

  const nc=document.getElementById('navcube');
  if(nc) nc.style.pointerEvents='none';
}

function exitTopDown(){
  if(!isTopDown) return;
  isTopDown=false;

  camera = perspCamera;
  controls = perspControls;

  showWallsNormal();

  const nc=document.getElementById('navcube');
  if(nc) nc.style.pointerEvents='';
}

// --- Labels Toggle Script ---
(function(){
  const cb=document.getElementById('showLabels'); if(!cb) return;
  const apply=()=>{ document.body.classList.toggle('labels-hidden', !cb.checked); };
  cb.addEventListener('change',apply); apply();
})();

// --- Reference Getter Script ---
const getRefs=()=>new Promise(resolve=>{ let tries=0; const t=setInterval(()=>{ if(window.__yard3d){ clearInterval(t); resolve(window.__yard3d); } if(++tries>200){ clearInterval(t); resolve(null); } },25); });
(async()=>{
  const refs=await getRefs(); if(!refs) return;
})();

// --- Bin Detail Panel Script ---
(function () {
  const panel = document.createElement('div');
  panel.id = 'bin-detail-panel';
  panel.style.cssText = `
    position: fixed; top: 0; right: 0; height: 100vh; width: 420px;
    background: #fff; border-left: 1px solid #ddd; box-shadow: -4px 0 16px rgba(0,0,0,.08);
    transform: translateX(100%); transition: transform .25s ease; z-index: 12000; overflow:auto;
    font-family:'DM Sans',sans-serif;`;
  panel.innerHTML = `
    <div style="position:sticky;top:0;background:var(--surface);border-bottom:1px solid #eee;padding:12px 16px;display:flex;align-items:center;gap:8px;">
      <strong id="bin-title" style="font-size:16px;">Bin</strong>
      <span id="bin-count" style="margin-left:auto;color:#666;"></span>
      <button id="bin-close" data-action="close-bin-panel"
              style="border:0;background:#f3f4f6;padding:6px 10px;border-radius:var(--r-sm);cursor:pointer;">Close</button>
    </div>
    <div id="bin-body" style="padding:12px 16px;"></div>
  `;
  document.body.appendChild(panel);

  function showPanel() { panel.style.transform = 'translateX(0)'; }
  function hidePanel() { panel.style.transform = 'translateX(100%)'; }

  panel.addEventListener('click', (e)=>{
    if (e.target.closest('[data-action="close-bin-panel"]')) {
      e.preventDefault(); e.stopPropagation(); hidePanel();
    }
  });

  const BIN_RX = /(?:^|[\s(])((EF|AC)\d{2}[A-G])(?:$|[\s)%])/i;
  const insidePanel = (node)=> node && panel.contains(node);

  function extractBinCode(el) {
    if (!el) return null;
    const d = el.dataset || {};
    const fromData = d.bin || d.code || null;
    if (fromData && BIN_RX.test(fromData)) return fromData.toUpperCase();
    const fromAria = el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title'));
    if (fromAria && BIN_RX.test(fromAria)) return BIN_RX.exec(fromAria)[1].toUpperCase();
    const fromId = el.id || '';
    if (BIN_RX.test(fromId)) return BIN_RX.exec(fromId)[1].toUpperCase();
    const t = (el.textContent || '').trim();
    if (BIN_RX.test(t)) return BIN_RX.exec(t)[1].toUpperCase();
    return null;
  }

  function markClickable(el, code) {
    if (!el) return;
    el.classList.add('__bin_clickable');
    if (el.style) { el.style.pointerEvents = 'auto'; }
    if (!el.dataset.bin && code) el.dataset.bin = code;
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.setAttribute('title', `Show details for ${code}`);
  }

  async function openBin(binCode) {
    try {
      const res = await fetch(`/api/bin?code=${encodeURIComponent(binCode)}`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();

      document.getElementById('bin-title').textContent = `Bin: ${data.bin}`;
      document.getElementById('bin-count').textContent = `${data.count} item${data.count === 1 ? '' : 's'}`;

      const body = document.getElementById('bin-body');
      if (!data.items || !data.items.length) {
        body.innerHTML = `<div style="color:#6b7280;">No items in this bin.</div>`;
      } else {
        body.innerHTML = data.items.map(renderItemCard).join('');
      }
      showPanel();
    } catch (e) {
      console.error('[bin-panel] fetch failed:', e);
      alert('Failed to load bin details (see console).');
    }
  }

  function kvRow(k, v) {
    return `<tr><td style="padding:6px 8px;color:#6b7280;white-space:nowrap;">${k}</td>
              <td style="padding:6px 8px;font-weight:600;">${v ?? ''}</td></tr>`;
  }

  function renderItemCard(item) {
    const core = [
      ['Plate ID', item.plate_id],
      ['Type', item.type],
      ['Status', item.status],
      ['Customer', item.customer],
      ['Grade', item.grade],
      ['Bin', item.bin],
      ['Seq (stack)', item.seq],
      ['Pieces', item.pieces],
      ['Length', item.length],
      ['Width', item.width],
      ['Thickness', item.thickness],
      ['Weight', item.weight],
      ['Urgency', item.urgency],
      ['Dispatch Mode', item.dispatch_mode],
      ['Added At', item.added_at],
      ['Created At', item.created_at],
      ['Updated At', item.updated_at]
    ];
    const raw = item.raw_json_expanded || {};
    const rawRows = Object.keys(raw).sort().map(k => kvRow(k, raw[k])).join('');
    return `
      <div style="border:1px solid #eee;border-radius:var(--r-md);margin-bottom:12px;overflow:hidden;">
        <div style="padding:10px 12px;background:#f9fafb;border-bottom:1px solid #eee;">
          <strong>${item.plate_id || '(no id)'}</strong>
          <span style="color:#6b7280;"> &middot; ${item.type || ''} &middot; ${item.status || ''}</span>
        </div>
        <div style="padding:8px 12px;">
          <table style="width:100%;border-collapse:collapse;">${core.map(([k,v]) => kvRow(k, v)).join('')}</table>
          <details style="margin-top:8px;">
            <summary style="cursor:pointer;color:#2563eb;">Show SAP fields</summary>
            <div style="margin-top:6px;">
              <table style="width:100%;border-collapse:collapse;">${rawRows || '<tr><td style="padding:6px 8px;color:#6b7280;">(none)</td></tr>'}</table>
            </div>
          </details>
        </div>
      </div>`;
  }

  function scanAndTagLabels(root = document) {
    const candidates = root.querySelectorAll('[data-bin], [data-code], .bin-label, .label, [id*="EF"], [id*="AC"], div, span');
    candidates.forEach(el => {
      if (el.closest('#bin-detail-panel')) return;
      if (el.closest('#search-details')) return;   // never tag customer-wise panel
      if (el.closest('#search-mini-details')) return; // never tag search mini panel
      if (el.closest('#mixed-lot-panel')) return;  // never tag mixed-lot panel
      if (el.childElementCount > 8 && !el.dataset.bin) return;
      const code = extractBinCode(el);
      if (code) { markClickable(el, code); }
    });
    const layers = document.querySelectorAll('.labels, .bins, .zones, [data-layer="labels"]');
    layers.forEach(l => { if (l && l.style) l.style.pointerEvents = 'auto'; });
  }

  scanAndTagLabels();

  const mo = new MutationObserver(muts => {
    let changed = false;
    for (const m of muts) if (m.addedNodes && m.addedNodes.length) { changed = true; break; }
    if (changed) scanAndTagLabels();
  });
  mo.observe(document.body, { childList: true, subtree: true });

  document.addEventListener('click', (ev) => {
    if (ev.target && ev.target.closest && ev.target.closest('#search-details')) return;
    if (ev.target && ev.target.closest && ev.target.closest('#search-mini-details')) return;
    if (ev.target && ev.target.closest && ev.target.closest('#mixed-lot-panel')) return;
    if (ev.target && ev.target.closest && ev.target.closest('#dispatchDrawer')) return;

    if (insidePanel(ev.target)) return;
    const target = ev.target.closest('.__bin_clickable,[data-bin]');
    if (!target) return;
    const code = (target.dataset && (target.dataset.bin || target.dataset.code)) || extractBinCode(target);
    if (code) { ev.preventDefault(); ev.stopPropagation(); openBin(code); }
  }, true);

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') hidePanel();
    const t = ev.target;
    if ((ev.key === 'Enter' || ev.key === ' ') && t && (t.classList?.contains('__bin_clickable') || t.dataset?.bin)) {
      if (insidePanel(t)) return;
      if (t.closest && (t.closest('#search-details') || t.closest('#search-mini-details') || t.closest('#mixed-lot-panel'))) return; 
      const code = t.dataset.bin || extractBinCode(t);
      if (code) { ev.preventDefault(); openBin(code); }
    }
  });

  window.__openBinDetail = openBin;
  window.__bindBinClicks = scanAndTagLabels;
})();

// --- Dispatch Drawer Script ---
(function(){
  const drawer = document.getElementById('dispatchDrawer');
  const backdrop = document.getElementById('dispatchBackdrop');
  const openBtn = document.getElementById('dispatchDrawerBtn');
  const closeBtn = document.getElementById('dispatchCloseBtn');
  const bellBtn = document.getElementById('dispatchNotifyBtn');
  const exportBtn = document.getElementById('dispatchExportExcelBtn');
  const dsBody  = document.getElementById('dsBody');
  const dsCount = document.getElementById('dsCount');

  const custInput  = document.getElementById('dsCustomerSearch');
  const custSearchBtn = document.getElementById('dsCustomerSearchBtn');
  const custClearBtn  = document.getElementById('dsCustomerClearBtn');
  const ddWrap   = document.getElementById('dsCustomerDropdown');
  const ddList   = document.getElementById('dsCustomerDropdownList');

  let __allUnits = [];
  let __allCustomers = [];
  let __selectedCustomer = '';

  function openDrawer(){
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden','false');
    document.body.classList.add('dispatch-open');
    if(backdrop){ backdrop.classList.add('open'); backdrop.setAttribute('aria-hidden','false'); }
  }
  function closeDrawer(){
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden','true');
    document.body.classList.remove('dispatch-open');
    if(backdrop){ backdrop.classList.remove('open'); backdrop.setAttribute('aria-hidden','true'); }
  }

  if(openBtn) openBtn.addEventListener('click', (e)=>{ e.preventDefault(); openDrawer(); });
  if(closeBtn) closeBtn.addEventListener('click', (e)=>{ e.preventDefault(); closeDrawer(); });
  if(backdrop) backdrop.addEventListener('click', (e)=>{ e.preventDefault(); closeDrawer(); });

  if(exportBtn){
    exportBtn.addEventListener('click', (e)=>{
      e.preventDefault();
      if(window.exportDispatchDrawerExcel) window.exportDispatchDrawerExcel();
      else alert('Excel export module not loaded yet.');
    });
  }

  document.addEventListener('keydown', (e)=>{ if(e.key==='Escape') closeDrawer(); });

  function isNotifyEnabled(){ return (localStorage.getItem('dispatch_notify') === 'true'); }

  async function notifyDispatchPlan() {
    try {
      const resp = await fetch('/api/notifications/dispatch_plan', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({city: 'All', customer: 'All', q: ''})
      });
      const data = await resp.json().catch(()=> ({}));
      alert((data && data.ok) ? `Notification sent: ${data.units||0} units.` : `Notification sent (simulated).`);
    } catch (e) {
      alert('Notification sent (simulated fallback).');
    }
  }

  async function toggleNotify() {
    const wasEnabled = isNotifyEnabled();
    localStorage.setItem('dispatch_notify', (!wasEnabled).toString());
    if (!wasEnabled && window.Notification && Notification.permission === 'default') {
      try { await Notification.requestPermission(); } catch(e){}
    }
    if(!wasEnabled){
      notifyDispatchPlan();
      if(bellBtn) bellBtn.style.background = 'rgba(16, 185, 129, 0.4)';
    } else {
      if(bellBtn) bellBtn.style.background = '';
    }
  }

  if(bellBtn) bellBtn.addEventListener('click', toggleNotify);
  if(isNotifyEnabled() && bellBtn) bellBtn.style.background = 'rgba(16, 185, 129, 0.4)';

  function copyToClipboard(text){
    try{ navigator.clipboard.writeText(text); }
    catch(e){
      const ta=document.createElement('textarea');
      ta.value=text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
  }

  function normStr(s){ return String(s || '').trim().toLowerCase(); }

  function buildCustomerListFromUnits(units){
    const set = new Set();
    (units || []).forEach(u=>{ const c = String(u.customer || '').trim(); if(c) set.add(c); });
    return Array.from(set).sort((a,b)=>a.localeCompare(b));
  }

  function openDropdown(items){
    if(!ddList) return;
    if(!items.length){
      ddList.innerHTML = `<div class="mutedline">No matching customers</div>`;
      ddList.classList.add('open');
      return;
    }
    ddList.innerHTML = items.map(c => {
      const esc = String(c).replace(/"/g,'&quot;').replace(/</g,'&lt;');
      return `<button type="button" role="option" data-cust="${esc}">${esc}</button>`;
    }).join('');
    ddList.classList.add('open');
  }

  function closeDropdown(){ if(ddList) ddList.classList.remove('open'); }

  function applyCustomerFilter(customer){
    __selectedCustomer = String(customer || '').trim();
    if(custInput) custInput.value = __selectedCustomer;
    const want = normStr(__selectedCustomer);
    let filtered = __allUnits.slice();
    if(want){
      const exact = filtered.filter(u => normStr(u.customer) === want);
      filtered = exact.length ? exact : filtered.filter(u => normStr(u.customer).includes(want));
    }
    render(filtered);
  }

  function clearCustomerFilter(){
    __selectedCustomer = '';
    if(custInput) custInput.value = '';
    closeDropdown();
    render(__allUnits);
  }

  if(ddList){
    ddList.addEventListener('click', (e)=>{
      const btn = e.target.closest('button[data-cust]');
      if(!btn) return;
      e.preventDefault();
      closeDropdown();
      applyCustomerFilter(btn.getAttribute('data-cust') || '');
    });
  }

  document.addEventListener('click', (e)=>{
    if(!ddWrap) return;
    if(ddWrap.contains(e.target) || (custInput && custInput.contains(e.target))) return;
    closeDropdown();
  });

  let _ddTimer = null;
  function onCustomerTyping(){
    clearTimeout(_ddTimer);
    _ddTimer = setTimeout(()=>{
      const q = normStr(custInput ? custInput.value : '');
      if(!q){ closeDropdown(); return; }
      const matches = __allCustomers.filter(c => normStr(c).includes(q)).slice(0, 30);
      openDropdown(matches);
    }, 120);
  }

  if(custInput){
    custInput.addEventListener('input', onCustomerTyping);
    custInput.addEventListener('keydown', (e)=>{
      if(e.key === 'Enter'){ e.preventDefault(); closeDropdown(); applyCustomerFilter(custInput.value); }
      if(e.key === 'Escape'){ closeDropdown(); }
    });
    custInput.addEventListener('focus', ()=>{
      const q = normStr(custInput.value);
      if(q){ openDropdown(__allCustomers.filter(c => normStr(c).includes(q)).slice(0, 30)); }
    });
  }

  if(custSearchBtn) custSearchBtn.addEventListener('click', (e)=>{ e.preventDefault(); closeDropdown(); applyCustomerFilter(custInput ? custInput.value : ''); });
  if(custClearBtn) custClearBtn.addEventListener('click', (e)=>{ e.preventDefault(); clearCustomerFilter(); });

  function render(units){
    const filtered = Array.isArray(units) ? units : [];
    window.__lastDispatchRenderedUnits = filtered;
    const totalItems = filtered.reduce((a,u)=>a+(u.items?u.items.length:0),0);
    if(dsCount) dsCount.textContent = `(${filtered.length} units • ${totalItems} items)`;
    if(dsBody) dsBody.innerHTML = '';
    if(!filtered.length){
      if(dsBody) dsBody.innerHTML = `<div class="ds-card"><div class="title">No dispatch units found</div><div class="sub">No units available currently.</div></div>`;
      return;
    }

    filtered.forEach(u=>{
      const card = document.createElement('div'); card.className='ds-card';
      const title = document.createElement('div'); title.className='title';
      title.innerHTML = `
        <div class="ds-title-main">
          <div class="ds-customer-name" title="${String(u.customer || 'Unknown Customer').replace(/"/g,'&quot;')}">${u.customer || 'Unknown Customer'}</div>
          <div class="sub ds-location-line">${u.city || 'Unknown City'} • <span class="pill">${u.target_mode || u.dispatch_mode || 'Truck'}</span></div>
        </div>
        <div class="sub ds-weight-box">
          <div><b>${(u.unit_weight || u.total_weight || 0).toFixed ? (u.unit_weight || u.total_weight || 0).toFixed(3) : (u.unit_weight || u.total_weight)}</b> t</div>
          <div class="muted">${u.count || (u.items||[]).length} items</div>
        </div>`;
      
      const sub = document.createElement('div'); sub.className='sub ds-status-panel';
      const suggestions = (u.move_suggestions || []).length;
      sub.innerHTML = `
        <div class="ds-status-row">
          <span class="ds-status-label">Status</span>
          <span class="ds-status-badge">Ready</span>
          ${suggestions ? `<span class="ds-suggestion-badge">${suggestions} moves suggested</span>` : ``}
        </div>`;

      const actions = document.createElement('div'); actions.className='ds-actions';
      const ids = (u.items||[]).map(x=>x.plate_id || x.id).join(' ');
      actions.innerHTML = `<button class="primary" data-act="load" data-ids="${ids}">Locate IDs</button><button data-act="copy" data-ids="${ids}">Copy IDs</button>`;

      const table = document.createElement('table'); table.className='ds-table';
      const rows = (u.items||[]).slice(0,120).map(it=>`<tr><td class="mono">${it.plate_id||it.id||''}</td><td class="mono">${it.bin||''}</td><td>${(it.FI_Rel_text||it.fi_text||'').replace(/</g,'&lt;')}</td><td>${(it.weight||0).toFixed ? Number(it.weight).toFixed(3) : it.weight}</td></tr>`).join('');
      table.innerHTML = `<thead><tr><th>ID</th><th>Bin</th><th>FI</th><th>Wt (t)</th></tr></thead><tbody>${rows}</tbody>`;

      card.appendChild(title); card.appendChild(sub); card.appendChild(actions);
      if((u.move_suggestions || []).length){
        const movesWrap = document.createElement('div'); movesWrap.className = 'sub';
        const mvRows = (u.move_suggestions || []).map(m => `<tr><td class="mono">${(m.plate_id||'').toString().replace(/</g,'&lt;')}</td><td class="mono">${(m.from_bin||'').toString().replace(/</g,'&lt;')}</td><td class="mono">${(m.to_bin||'').toString().replace(/</g,'&lt;')}</td><td>${(m.reason||'').toString().replace(/</g,'&lt;')}</td></tr>`).join('');
        movesWrap.innerHTML = `<div class="ds-moves-title">Suggested Moves</div><table class="ds-table" style="margin-top:6px;"><thead><tr><th>ID</th><th>From</th><th>To</th><th>Reason</th></tr></thead><tbody>${mvRows}</tbody></table>`;
        card.appendChild(movesWrap);
      }
      card.appendChild(table);
      if(dsBody) dsBody.appendChild(card);
    });

    if(dsBody) dsBody.querySelectorAll('button[data-act]').forEach(btn=>{
      btn.addEventListener('click', ()=>{
        const act = btn.getAttribute('data-act'); const ids = btn.getAttribute('data-ids') || '';
        if(act==='copy'){
          copyToClipboard(ids);
          const orig = btn.textContent; btn.textContent='Copied!'; setTimeout(()=>btn.textContent=orig, 1200);
        }else if(act==='load'){
          const searchBox = document.getElementById('searchIds'); const searchBtn = document.getElementById('searchBtn');
          if(searchBox && searchBtn){ searchBox.value = ids; searchBtn.click(); closeDrawer(); } else { alert('Search box not found, IDs copied instead.'); copyToClipboard(ids); }
        }
      });
    });
  }

  let lastCount = 0;
  async function fetchSuggestions(){
    try {
      const res = await fetch(`/api/dispatch_suggestions_ai`, { headers: { 'X-Requested-With': 'fetch' } });
      if(!res.ok) throw new Error('API Error');
      const data = await res.json();

      const aiBox = document.getElementById('dsAiSummary');
      const aiText = document.getElementById('dsAiSummaryText');
      if (data.ai_summary && aiBox && aiText) {
        aiBox.style.display = 'block';
        const esc = (s) => String(s ?? '').replace(/</g,'&lt;');
        let formattedText = esc(data.ai_summary)
          .replace(/\*\*(.*?)\*\*/g, '<strong style="color: #0f2a43; font-weight: 800;">$1</strong>')
          .replace(/(?:\r\n|\r|\n)/g, '<br>')
          .replace(/<br>\*\s+/g, '<br><span style="color:#1abc9c; margin-right:8px; font-size:16px; line-height:1;">•</span> ');
        if (formattedText.startsWith('* ')) {
          formattedText = '<span style="color:#1abc9c; margin-right:8px; font-size:16px; line-height:1;">•</span> ' + formattedText.substring(2);
        }
        aiText.innerHTML = formattedText;
      } else if (aiBox) {
        aiBox.style.display = 'none';
      }

      const units = (data && Array.isArray(data.units)) ? data.units : [];
      __allUnits = units.slice();
      __allCustomers = buildCustomerListFromUnits(__allUnits);
      if(__selectedCustomer) applyCustomerFilter(__selectedCustomer); else render(__allUnits);

      const currentCount = units.reduce((a,u)=>a+(u.items?u.items.length:0),0);
      if(isNotifyEnabled() && currentCount > lastCount && ('Notification' in window) && Notification.permission === 'granted'){
        try{ new Notification('Dispatch Suggestions updated', { body: `${currentCount} units available (was ${lastCount}).` }); }catch(e){}
      }
      lastCount = currentCount;
    } catch(e) {
      __allUnits = []; __allCustomers = []; render([]);
    }
  }

  fetchSuggestions();
  setInterval(fetchSuggestions, 30000);
})();

// --- Excel Export Script ---
(function(){
  function getXLSX(){ return window.XLSX || null; }
  function pad2(n){ return String(n).padStart(2,'0'); }
  function stamp(){ const d = new Date(); return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}_${pad2(d.getHours())}-${pad2(d.getMinutes())}`; }
  function safeStr(v){ if(v === null || v === undefined) return ''; return String(v).replace(/\s+/g,' ').trim(); }
  
  function computeColsFromAOA(aoa, minW=10, maxW=55){
    const widths = [];
    for(let r=0;r<aoa.length;r++){
      const row = aoa[r] || [];
      for(let c=0;c<row.length;c++){
        const s = safeStr(row[c]); const len = s.length;
        const w = Math.max(minW, Math.min(maxW, len <= 12 ? len+2 : Math.round(len*0.9)));
        widths[c] = Math.max(widths[c] || minW, w);
      }
    }
    return widths.map(w => ({ wch: w }));
  }

  function styleWrapAndHeader(ws, headerRows=1){
    if(!ws || !ws['!ref']) return;
    const XLSX = getXLSX();
    const range = XLSX.utils.decode_range(ws['!ref']);
    for(let R=range.s.r; R<=range.e.r; R++){
      for(let C=range.s.c; C<=range.e.c; C++){
        const addr = XLSX.utils.encode_cell({r:R,c:C});
        const cell = ws[addr];
        if(!cell) continue;
        cell.s = cell.s || {};
        cell.s.alignment = Object.assign({}, cell.s.alignment, { wrapText:true, vertical:'top' });
        if(R < headerRows){
          cell.s.font = Object.assign({}, cell.s.font, { bold:true });
          cell.s.alignment = Object.assign({}, cell.s.alignment, { horizontal:'center', vertical:'center', wrapText:true });
        }
      }
    }
    ws['!freeze'] = { xSplit: 0, ySplit: headerRows };
  }

  function aoaToSheet(aoa, headerRows=1){
    const XLSX = getXLSX();
    const ws = XLSX.utils.aoa_to_sheet(aoa);
    ws['!cols'] = computeColsFromAOA(aoa, 10, 55);
    styleWrapAndHeader(ws, headerRows);
    return ws;
  }

  function addMetaSheet(wb, metaObj){
    const XLSX = getXLSX();
    const rows = [['Key','Value']];
    Object.keys(metaObj || {}).forEach(k=>{ const v = metaObj[k]; rows.push([k, (typeof v === 'object' ? JSON.stringify(v) : safeStr(v))]); });
    const ws = aoaToSheet(rows, 1);
    XLSX.utils.book_append_sheet(wb, ws, 'Meta');
  }

  function safeWriteFile(XLSX, wb, filename){
    try{ if (typeof XLSX.writeFile === 'function') { XLSX.writeFile(wb, filename); return; } }catch(err){}
    try{
      const out = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
      const blob = new Blob([out], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
      const a = document.createElement('a');
      const url = URL.createObjectURL(blob);
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click();
      setTimeout(()=>{ URL.revokeObjectURL(url); a.remove(); }, 1000);
    }catch(err){
      alert('Export failed. Please open DevTools Console and share the error shown under [export].');
    }
  }

  window.exportCustomerWisePanelExcel = function(){
    const XLSX = getXLSX(); if(!XLSX){ alert('Excel export library not available.'); return; }
    const list = Array.isArray(window.__lastCustomerWiseFound) ? window.__lastCustomerWiseFound : [];
    if(!list.length){ alert('No Customer-wise data to export. Open the panel first.'); return; }
    const filters = window.__lastCustomerWiseFilters || {};
    const exportedAt = window.__lastCustomerWiseExportedAt || new Date().toISOString();
    const title = window.__lastCustomerWiseTitle || 'Customer_Wise_Details';
    const header = ['Customer','City','ID','Bin','Type','Status','FI','SBU','From Top','From Bottom','Length','Width','Thickness','L×W×T','Pieces','Weight','Grade','Date','Added At','Updated At','Created At'];
    const aoa = [header];
    const posFn = (typeof window.__getStackPositionForItem === 'function') ? window.__getStackPositionForItem : null;
    for(const it of list){
      const cust = safeStr(it.customer ?? it.Customer ?? it.CUSTOMER ?? 'Unknown');
      const city = safeStr(it.CustomerCity ?? it.customer_city ?? it.CUSTOMERCITY ?? '');
      const id   = safeStr(it.item_id ?? it.plate_id ?? it.id ?? '');
      const bin  = safeStr(it.bin ?? '').toUpperCase();
      const ty   = safeStr(it.type ?? ''); const st = safeStr(it.status ?? '');
      const fi   = safeStr(it.FI_Rel_text ?? it.fi_rel_text ?? it.fi_text ?? '');
      const sbu  = safeStr(it.SBU_RelStatus ?? it.sbu_rel_status ?? '');
      const grade = safeStr(it.grade ?? '');
      const L = safeStr(it.length ?? ''); const W = safeStr(it.width ?? ''); const T = safeStr(it.thickness ?? '');
      const dim = [L,W,T].filter(x=>x!=='').join('×');
      const pcs = safeStr(it.pieces ?? ''); const wt = safeStr(it.weight ?? '');
      const date = safeStr(it.date ?? ''); const added_at = safeStr(it.added_at ?? ''); const updated_at = safeStr(it.updated_at ?? ''); const created_at = safeStr(it.created_at ?? '');
      let fromTop='—', fromBottom='—';
      if(posFn){ const pos = posFn(it) || {}; fromTop = safeStr(pos.top ?? '—') || '—'; fromBottom = safeStr(pos.bottom ?? '—') || '—'; }
      aoa.push([cust, city, id, bin, ty, st, fi, sbu, fromTop, fromBottom, L, W, T, dim, pcs, wt, grade, date, added_at, updated_at, created_at]);
    }
    const wb = XLSX.utils.book_new(); const ws = aoaToSheet(aoa, 1);
    XLSX.utils.book_append_sheet(wb, ws, 'Customer_Wise');
    addMetaSheet(wb, { exported_at: exportedAt, panel: 'Details (Customer-wise)', title_prefix: title, filters_applied: filters, total_rows: aoa.length - 1 });
    safeWriteFile(XLSX, wb, `${title.replace(/\s+/g,'_')}_${stamp()}.xlsx`);
  };

  window.exportMixedLotExcel = function(){
    const XLSX = getXLSX(); if(!XLSX){ alert('Excel export library not available.'); return; }
    const bins = Array.isArray(window.__lastMixedLotBins) ? window.__lastMixedLotBins : [];
    if(!bins.length){ alert('No Mixed Lot data to export. Click "Mixed Lot" first.'); return; }
    const filters = (typeof window.__lastCustomerWiseFilters === 'object' && window.__lastCustomerWiseFilters) ? window.__lastCustomerWiseFilters : {};
    const wb = XLSX.utils.book_new();
    const summaryAOA = [['Bin','Customer Count','Item Count','Customers']];
    const itemsAOA = [['Bin','ID','Customer','City','Type','Status','FI','SBU','From Top','From Bottom','L×W×T','Pieces','Weight']];
    const posFn = (typeof window.__getStackPositionForItem === 'function') ? window.__getStackPositionForItem : null;
    bins.forEach(b=>{
      summaryAOA.push([safeStr(b.bin), safeStr(b.customerCount), safeStr(b.itemCount), safeStr((b.customers||[]).slice().sort((a,c)=>String(a).localeCompare(String(c))).join(', '))]);
      (b.items||[]).forEach(it=>{
        const pos = posFn ? (posFn({ ...it, item_id: it.plate_id, bin: b.bin }) || {}) : {};
        const dim = [it.length,it.width,it.thickness].filter(v=>v!==undefined && v!==null && safeStr(v)!=='').join('×');
        itemsAOA.push([safeStr(b.bin), safeStr(it.plate_id || ''), safeStr(it.customer || 'Unknown'), safeStr(it.CustomerCity || ''), safeStr(it.type || ''), safeStr(it.status || ''), safeStr(it.FI_Rel_text || ''), safeStr(it.SBU_RelStatus || ''), safeStr(pos.top ?? '—'), safeStr(pos.bottom ?? '—'), safeStr(dim), safeStr(it.pieces ?? ''), safeStr(it.weight ?? '')]);
      });
    });
    XLSX.utils.book_append_sheet(wb, aoaToSheet(summaryAOA, 1), 'MixedLot_Summary');
    XLSX.utils.book_append_sheet(wb, aoaToSheet(itemsAOA, 1), 'MixedLot_Items');
    addMetaSheet(wb, { exported_at: new Date().toISOString(), panel: 'Mixed Lot', filters_applied: filters, bins_exported: bins.length, items_exported: itemsAOA.length - 1 });
    safeWriteFile(XLSX, wb, `Mixed_Lot_${stamp()}.xlsx`);
  };

  window.exportDispatchDrawerExcel = function(){
    const XLSX = getXLSX(); if(!XLSX){ alert('Excel export library not available.'); return; }
    const units = Array.isArray(window.__lastDispatchRenderedUnits) ? window.__lastDispatchRenderedUnits : [];
    if(!units.length){ alert('No Dispatch Suggestions to export yet.'); return; }
    const wb = XLSX.utils.book_new();
    const sumAOA = [['Customer','City','Target/Mode','Unit Weight (t)','Items Count','Moves Suggested']];
    const itemsAOA = [['Customer','City','ID','Bin','FI','Weight (t)']];
    const movesAOA = [['Customer','City','ID','From Bin','To Bin','Reason']];
    units.forEach(u=>{
      const uc = safeStr(u.customer || 'Unknown Customer'); const ucity = safeStr(u.city || 'Unknown City'); const mode = safeStr(u.target_mode || u.dispatch_mode || 'Truck');
      const unitWt = (u.unit_weight ?? u.total_weight ?? ''); const cnt = (u.count ?? (u.items ? u.items.length : 0)); const mvCnt = (u.move_suggestions || []).length;
      sumAOA.push([uc, ucity, mode, safeStr(unitWt), safeStr(cnt), safeStr(mvCnt)]);
      (u.items || []).forEach(it=>{ itemsAOA.push([uc, ucity, safeStr(it.plate_id || it.id || ''), safeStr(it.bin || ''), safeStr(it.FI_Rel_text || it.fi_text || ''), safeStr((it.weight ?? ''))]); });
      (u.move_suggestions || []).forEach(m=>{ movesAOA.push([uc, ucity, safeStr(m.plate_id || ''), safeStr(m.from_bin || ''), safeStr(m.to_bin || ''), safeStr(m.reason || '')]); });
    });
    XLSX.utils.book_append_sheet(wb, aoaToSheet(sumAOA, 1), 'Units_Summary');
    XLSX.utils.book_append_sheet(wb, aoaToSheet(itemsAOA, 1), 'Items');
    XLSX.utils.book_append_sheet(wb, aoaToSheet(movesAOA, 1), 'Suggested_Moves');
    addMetaSheet(wb, { exported_at: new Date().toISOString(), panel: 'Dispatch Suggestions', units_exported: units.length, items_exported: itemsAOA.length - 1, moves_exported: movesAOA.length - 1 });
    safeWriteFile(XLSX, wb, `Dispatch_Suggestions_${stamp()}.xlsx`);
  };
})();

// --- Footer Marquee Script ---
function __getBayFromBin(bin){
  const b = String(bin || '').toUpperCase().trim();
  if(!b) return 'UNK';
  const m = b.match(/^[A-Z]+/);
  return (m && m[0]) ? m[0] : 'UNK';
}
function __num(x){ const n = Number(x); return Number.isFinite(n) ? n : 0; }
function __fmt2(n){ return (__num(n)).toFixed(2); }
function __buildBayWiseHTML(label, map, headingClass){
  const bays = Object.keys(map || {}).sort();
  if(!bays.length){ return `<span><span class="marq-h ${headingClass}">${label}:</span> —</span>`; }
  const parts = bays.map(b => `${b} ${map[b].qty} (${__fmt2(map[b].wt)})`);
  return `<span><span class="marq-h ${headingClass}">${label}:</span> ${parts.join(' | ')}</span>`;
}
function __setMarqueeDuration(){
  const inner = document.getElementById('marqueeInner');
  if(!inner) return;
  const totalWidth = inner.scrollWidth;
  if(!totalWidth) return;
  const speedPxPerSec = 90;
  const oneSegmentWidth = totalWidth / 2;
  const durationSec = Math.max(18, Math.ceil(oneSegmentWidth / speedPxPerSec));
  inner.style.setProperty('--marquee-duration', `${durationSec}s`);
}
function updateFooterMarqueeStats(){
  const arr = Array.isArray(window.itemMeshes) ? window.itemMeshes : [];
  const acc = { WIP: {}, FG: {}, FG_FI: {}, PLATES: {}, COILS: {}, TOTAL: {} };
  function add(map, bay, wt){
    if(!map[bay]) map[bay] = { qty: 0, wt: 0 };
    map[bay].qty += 1; map[bay].wt += __num(wt);
  }
  for(const obj of arr){
    const mesh = obj && obj.mesh; const u = (mesh && mesh.userData) ? mesh.userData : null;
    if(!u) continue;
    const bin = u.bin || ''; const bay = __getBayFromBin(bin);
    const type = String(u.type || '').toUpperCase(); const status = String(u.status || '').toUpperCase(); const fiText = String(u.FI_Rel_text || '').toUpperCase();
    const wt = u.weight;
    add(acc.TOTAL, bay, wt);
    if(status.includes('WIP')) add(acc.WIP, bay, wt);
    if(status.includes('FG'))  add(acc.FG, bay, wt);
    if(type.includes('PLATE')) add(acc.PLATES, bay, wt);
    if(type.includes('COIL'))  add(acc.COILS, bay, wt);
    if(status.includes('FG') && (fiText.includes('FI') || fiText.includes('RELEASED (2)'))){ add(acc.FG_FI, bay, wt); }
  }
  const htmlParts = [
    __buildBayWiseHTML('Total WIP Qty (Wt)', acc.WIP, 'h-wip'),
    __buildBayWiseHTML('Total FG Qty (Wt)', acc.FG, 'h-fg'),
    __buildBayWiseHTML('FG with FI & FI Released (2) Qty (Wt)', acc.FG_FI, 'h-fi'),
    __buildBayWiseHTML('Total Plates Qty (Wt)', acc.PLATES, 'h-pl'),
    __buildBayWiseHTML('Total Coils Qty (Wt)', acc.COILS, 'h-co'),
    __buildBayWiseHTML('Total Items Qty (Wt)', acc.TOTAL, 'h-ti'),
    `<span class="marq-madeby">Made by Swetabh Shekhar Sinha</span>`
  ];
  const seg = document.getElementById('marqueeSegment');
  if(seg) seg.innerHTML = htmlParts.join(` <span class="marq-sep">•</span> `);
  const clone = document.getElementById('marqueeSegmentClone');
  if(clone && seg) clone.innerHTML = seg.innerHTML;
  __setMarqueeDuration();
}
// FIX: Module scripts defer execution, so DOMContentLoaded is already fired. Call directly!
updateFooterMarqueeStats();
setTimeout(updateFooterMarqueeStats, 1500);
window.addEventListener('resize', updateFooterMarqueeStats);