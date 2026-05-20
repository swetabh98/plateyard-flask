(function(){
  const $ = (id) => document.getElementById(id);

  const cardsEl = $("cards");
  const metaEl = $("metaLine");

  const maxStopsEl = $("maxStops");
  const maxBinsEl = $("maxBins");
  const customerFilterEl = $("customerFilter");

  const btnRefresh = $("btnRefresh");
  const btnCopy = $("btnCopy");

  // Modal elements
  const lpModal = $("lpModal");
  const modalContent = $("modalContent");
  const modalTitle = $("modalTitle");
  const btnCloseModal = $("btnCloseModal");

  const LS_KEY = "vehicle_seq_ui_state_v1";
  const MAX_STOPS_FIXED = 10; // fixed as per requirement

  const IST_TZ = "Asia/Kolkata";

  let lastPayload = null;
  let lastLocks = new Map();

  const LP_CARD_CLASS = "lpCard";
  const VEH_CARD_CLASS = "vehicleCard";
  const VEH_COLLAPSED_CLASS = "collapsed";

  function fmt(n, d=2){
    const x = Number(n);
    if (!Number.isFinite(x)) return "-";
    return x.toFixed(d);
  }

  function esc(s){
    return String(s ?? "")
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#039;");
  }

  function q(v){
    const s = String(v ?? "");
    return `"${s.replaceAll('"','""')}"`;
  }

  function num(v){
    const x = Number(v);
    return Number.isFinite(x) ? x : "";
  }

  function materialId(stop){
    return stop?.plate_id ?? stop?.material_id ?? stop?.id ?? "";
  }

  function customerText(stop){
    const name = stop?.customer ?? "";
    const city = stop?.customer_city ?? stop?.CustomerCity ?? "";
    if (name && city) return `${name} (${city})`;
    return name || city || "";
  }

  function priorityLabel(p){
    const n = Number(p);
    if (n === 1) return "Priority 1";
    if (n === 2) return "Priority 2";
    if (n === 3) return "Priority 3";
    return "No Priority found";
  }

  function lpBinsLabel(lpId){
  const id = String(lpId || "").trim();
  if (id === "lp1") return "EF34 – EF40";
  if (id === "lp2") return "EF45 – EF54";
  if (id === "lp3") return "EF55 – EF67";
  if (id === "lp4") return "DE34 – DE39";
  if (id === "lp7") return "DE66 – DE67";
  if (id === "lp9") return "AC66 – AC67FE";
  if (id === "lp11") return "CTLDE";
  if (id === "lp12") return "CTLCD";
  return "";
}

  function capLabel(cap){
    const x = Number(cap);
    return Number.isFinite(x) && x>0 ? `${fmt(x,0)}T` : "";
  }

  function cleanIntOrEmpty(v){
    const s = String(v ?? "").trim();
    if (!s) return "";
    const n = Number(s);
    if (!Number.isFinite(n)) return "";
    return String(Math.max(0, Math.floor(n)));
  }

  function formatIST(isoUtc){
    if (!isoUtc) return "";
    const d = new Date(isoUtc);
    if (Number.isNaN(d.getTime())) return String(isoUtc);
    const f = new Intl.DateTimeFormat("en-IN", {
      timeZone: IST_TZ,
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true
    });
    return f.format(d) + " IST";
  }

  function loadState(){
    try{
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    }catch{
      return null;
    }
  }

  function saveState(){
    const st = {
      maxBins: cleanIntOrEmpty(maxBinsEl.value),
      customer: (customerFilterEl?.value || "").trim()
    };
    try{ localStorage.setItem(LS_KEY, JSON.stringify(st)); }catch{}
  }

  function applyState(){
    const st = loadState();
    const defaults = { maxBins:"", customer:"" };
    const s = Object.assign({}, defaults, st || {});
    maxBinsEl.value = s.maxBins || "";
    if (customerFilterEl) customerFilterEl.value = s.customer || "";
  }

  async function apiJson(url, opts={}){
    const resp = await fetch(url, {
      headers: Object.assign({ "Accept":"application/json", "Content-Type":"application/json" }, (opts.headers||{})),
      ...opts
    });
    const txt = await resp.text();
    let j = null;
    try{ j = txt ? JSON.parse(txt) : {}; }catch{ j = { raw: txt }; }
    if(!resp.ok){
      const msg = j?.error ? j.error : (txt || `${resp.status} ${resp.statusText}`);
      throw new Error(msg);
    }
    return j;
  }

  function toast(msg){
    const el = document.createElement("div");
    el.style.position = "fixed";
    el.style.right = "16px";
    el.style.bottom = "48px";
    el.style.zIndex = 10000;
    el.style.background = "rgba(15,42,67,.92)";
    el.style.color = "#fff";
    el.style.border = "1px solid rgba(255,255,255,.18)";
    el.style.borderRadius = "12px";
    el.style.padding = "10px 12px";
    el.style.fontWeight = "900";
    el.style.maxWidth = "520px";
    el.style.boxShadow = "0 10px 30px rgba(15,42,67,.20)";
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(()=> el.remove(), 3200);
  }

  async function refreshLocks(){
    try{
      const data = await apiJson("/api/vehicle_sequencing/locks");
      lastLocks = new Map();
      (data?.locks || []).forEach(l => {
        if (l?.plate_id) lastLocks.set(l.plate_id, l);
      });
    }catch{
      lastLocks = new Map();
    }
  }

  function renderMeta(payload, requested){
    const tsUtc = payload?.generated_at_utc ? payload.generated_at_utc : "";
    const tsIst = formatIST(tsUtc);
    const params = payload?.params || {};
    const effStops = params.max_stops_per_vehicle ?? requested.maxStops;
    const effBins = params.max_bins_considered ?? "";
    const reqBinsText = requested.maxBins ? requested.maxBins : "AUTO";
    const effBinsText = effBins ? String(effBins) : "AUTO";
    const cust = requested.customer ? requested.customer : (params.customer || "");
    const custText = cust ? cust : "—";
    const cached = payload?.cached ? "YES" : "NO";
    const locksCount = payload?.locks?.count ?? 0;

    metaEl.innerHTML = `
      <span class="chip" title="UTC: ${esc(tsUtc)}">Generated (IST): <b>${esc(tsIst)}</b></span>
      <span class="chip">Cached: <b>${esc(cached)}</b></span>
      <span class="chip">Active locks: <b>${esc(String(locksCount))}</b></span>
      <span class="chip">Bins/lot cap: <b>${esc(effStops)}</b></span>
      <span class="chip">Max bins (requested): <b>${esc(reqBinsText)}</b></span>
      <span class="chip">Max bins (effective): <b>${esc(effBinsText)}</b></span>
      <span class="chip">Customer: <b>${esc(custText)}</b></span>
    `;
  }

  function vehiclePlateIds(v){
    const stops = v?.stops || [];
    const ids = [];
    for (const s of stops){
      const pid = materialId(s);
      if (pid) ids.push(pid);
    }
    return ids;
  }

  function lockSummaryForVehicle(v){
    const ids = vehiclePlateIds(v);
    if (!ids.length) return { locked: 0, total: 0 };
    let locked = 0;
    for (const pid of ids){
      if (lastLocks.has(pid)) locked += 1;
    }
    return { locked, total: ids.length };
  }

  async function lockVehicle(v){
    const plate_ids = vehiclePlateIds(v);
    if (!plate_ids.length){ toast("No items to lock."); return; }
    await apiJson("/api/vehicle_sequencing/lock", {
      method: "POST",
      body: JSON.stringify({ plate_ids, locked_by: "vehicle_sequencing_ui", ttl_min: 60 })
    });
    toast(`Locked ${plate_ids.length} item(s).`);
    await refresh();
  }

  async function releaseVehicle(v){
    const plate_ids = vehiclePlateIds(v);
    if (!plate_ids.length){ toast("No items to release."); return; }
    await apiJson("/api/vehicle_sequencing/release", {
      method: "POST",
      body: JSON.stringify({ plate_ids })
    });
    toast(`Released ${plate_ids.length} item(s).`);
    await refresh();
  }

  function buildLPInternalHtml(lp) {
    const anchor = lp.anchor_bins?.length ? lp.anchor_bins.join(", ") : "—";
    const kpi = `${lp.vehicle_count} vehicle(s) • bins considered: ${lp.candidate_bins_considered} • items scanned: ${lp.candidate_items_considered} • locked filtered: ${lp.filtered_locked_items}`;
    const vehicles = lp.vehicles || [];

    const vehiclesHtml = vehicles.map((v, idx) => {
      const items = v.stops || [];
      const totalWt = items.reduce((a,s)=>a + (Number(s.weight)||0), 0);
      const vCust = v?.vehicle_customer ?? v?.customer ?? "";
      const vCity = v?.vehicle_city ?? "";
      const custLine = (vCust && vCity) ? `${vCust} (${vCity})` : (vCust || vCity || "");
      const srcKind = v?.source_kind ? `• ${v.source_kind}` : "";
      const lockStat = lockSummaryForVehicle(v);
      const collapsed = (idx === 0) ? "" : ` ${VEH_COLLAPSED_CLASS}`;

      const head = `
        <div class="vehicleHead">
          <div class="left" style="display:flex; gap:10px; align-items:flex-start;">
            <span class="chev vehChev">${idx === 0 ? "▾" : "▸"}</span>
            <div>
              <div style="font-weight:900;">
                Vehicle ${esc(v.vehicle_no)}
                ${custLine ? `<span class="pill" style="margin-left:8px;">${esc(custLine)}</span>` : ""}
                ${v?.lot_kind ? `<span class="pill" style="margin-left:8px;">${esc(v.lot_kind)}${esc(srcKind)}</span>` : ""}
                ${`<span class="pill priorityPill" style="margin-left:8px;">${esc(priorityLabel(v.priority))}</span>`}
                ${(v?.truck_capacity_tons) ? `<span class="pill" style="margin-left:8px;">Cap: <b>${esc(capLabel(v.truck_capacity_tons))}</b></span>` : ""}
                <span class="pill" style="margin-left:8px;">Locks: <b>${esc(String(lockStat.locked))}</b> / ${esc(String(lockStat.total))}</span>
              </div>
              <div class="kpi">${esc(items.length)} item(s) • total wt: ${fmt(totalWt,2)}</div>
            </div>
          </div>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn primary" data-vact="lock" data-lp="${esc(lp.id)}" data-vno="${esc(v.vehicle_no)}">Lock</button>
            <button class="btn danger" data-vact="release" data-lp="${esc(lp.id)}" data-vno="${esc(v.vehicle_no)}">Release</button>
          </div>
        </div>
      `;

      if (!items.length){
        return `
          <div class="vehicle ${VEH_CARD_CLASS}${collapsed}">
            ${head}
            <div class="vehicleBody">
              <div class="cardBody"><div class="empty">No eligible items assigned.</div></div>
            </div>
          </div>`;
      }

      const rows = items.map((s, i) => `
        <tr>
          <td>${i+1}</td>
          <td><b>${esc(s.bin_id)}</b><br><span style="color:#94a3b8; font-weight:800;">dist: ${fmt(s.distance,2)}</span></td>
          <td>${esc(materialId(s))}<br><span style="color:#94a3b8; font-weight:800;">${esc(s.material_type || "")}</span></td>
          <td>${esc(customerText(s)) || "—"}</td>
          <td>${fmt(s.weight,2)}</td>
          <td>${Number(s.rehandles||0)===0 ? `<span class="badge good">Pickable</span>` : `<span class="badge warn">Rehandles: ${esc(String(s.rehandles))}</span>`}</td>
          <td>${lastLocks.has(materialId(s)) ? `<span class="badge bad">LOCKED</span>` : `<span class="badge good">Free</span>`}</td>
          <td><div class="statusCell"><span class="badge">${esc(s.FG_text || "")}</span><span class="badge">${esc(s.FI_Rel_text || "")}</span></div></td>
        </tr>
      `).join("");

      return `
        <div class="vehicle ${VEH_CARD_CLASS}${collapsed}">
          ${head}
          <div class="vehicleBody">
            <div class="lpScroll">
              <table style="min-width:980px;">
                <thead>
                  <tr>
                    <th style="width:38px;">#</th>
                    <th style="min-width:210px;">Bin (distance from LP)</th>
                    <th style="min-width:180px;">Material</th>
                    <th style="min-width:230px;">Customer</th>
                    <th style="width:90px;">Weight</th>
                    <th style="min-width:120px;">Rehandles</th>
                    <th style="min-width:110px;">Lock</th>
                    <th style="min-width:170px;">Status</th>
                  </tr>
                </thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
          </div>
        </div>`;
    }).join("");

    return `
      <div style="margin-bottom:20px;">
        <span class="pill" style="margin-bottom:10px; display:inline-block;">${kpi}</span>
        <div style="color:var(--muted); font-size:12px; font-weight:800;">Anchor bins: ${esc(anchor)}</div>
      </div>
      ${vehiclesHtml || `<div class="empty">No vehicles.</div>`}
    `;
  }

  function render(payload, requested){
    lastPayload = payload;
    renderMeta(payload, requested);

    const points = payload?.loading_points || [];
    if (!points.length){
      cardsEl.innerHTML = `<div class="card"><div class="cardBody"><div class="empty">No loading points configured.</div></div></div>`;
      return;
    }

    cardsEl.innerHTML = points.map(lp => {
      const ps = lp?.priorities_summary || {};
      const p1Found = (ps.P1 || 0) > 0;
      const p2Found = (ps.P2 || 0) > 0;
      const p3Found = (ps.P3 || 0) > 0;

      const kpiShort = `${lp.vehicle_count} vehicle(s) • ${lp.candidate_items_considered} items`;

      const p1Text = `Priority 1 - ${p1Found ? "Found" : "Not Found"}`;
      const p2Text = `Priority 2 - ${p2Found ? "Found" : "Not Found"}`;
      const p3Text = `Priority 3 - ${p3Found ? "Found" : "Not Found"}`;

      return `
        <section class="card ${LP_CARD_CLASS}" data-lp-id="${esc(lp.id)}">
          <div class="cardHead">
            <div class="lpToggle">
              <span class="chev">▸</span>
              <div>
                <div class="title">${esc(lp.name)}</div>
                <div class="kpi" style="margin-top:4px;">${esc(kpiShort)}</div>
                <div class="kpi" style="margin-top:2px;">Bins: <b>${esc(lpBinsLabel(lp.id) || "—")}</b></div>
              </div>
            </div>
            <div class="lpRight">
              <span class="pill priorityPill">${esc(p1Text)}</span>
              <span class="pill priorityPill">${esc(p2Text)}</span>
              <span class="pill priorityPill">${esc(p3Text)}</span>
              <div class="pill">Click to Open Full View</div>
            </div>
          </div>
        </section>`;
    }).join("");
  }

  async function refresh(){
    metaEl.innerHTML = `<span class="chip">Loading…</span>`;
    cardsEl.innerHTML = "";
    const requested = {
      maxStops: String(MAX_STOPS_FIXED),
      maxBins: cleanIntOrEmpty(maxBinsEl.value),
      customer: customerFilterEl ? (customerFilterEl.value || "").trim() : ""
    };
    saveState();
    const url = new URL("/api/vehicle_sequencing", window.location.origin);
    url.searchParams.set("max_stops_per_vehicle", String(requested.maxStops || MAX_STOPS_FIXED));
    if (requested.maxBins) url.searchParams.set("max_bins_considered", String(requested.maxBins));
    if (requested.customer) url.searchParams.set("customer", requested.customer);

    try{
      await refreshLocks();
      const resp = await fetch(url.toString(), { headers: { "Accept": "application/json" } });
      if(!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
      const payload = await resp.json();
      if (!requested.maxBins && payload?.params?.max_bins_considered) maxBinsEl.value = String(payload.params.max_bins_considered);
      saveState();
      await refreshLocks();
      render(payload, requested);
    }catch(err){
      metaEl.innerHTML = `<span class="chip">Error</span>`;
      cardsEl.innerHTML = `<div class="card"><div class="cardBody"><div class="empty">${esc(err.message || String(err))}</div></div></div>`;
    }
  }

  function copyCSV(){
    if(!lastPayload){ alert("Nothing to copy yet."); return; }
    const rows = [["generated_at_utc","generated_at_ist","loading_point","vehicle_no","vehicle_customer","vehicle_city","lot_kind","source_kind","item_no","bin_id","plate_id","material_type","customer","customer_city","weight","distance","FG_text","FI_Rel_text","locked"].join(",")];
    const genUtc = lastPayload?.generated_at_utc || "";
    const genIst = formatIST(genUtc);
    for(const lp of (lastPayload.loading_points || [])){
      for(const v of (lp.vehicles || [])){
        (v.stops || []).forEach((s, idx) => {
          const pid = materialId(s);
          rows.push([q(genUtc), q(genIst), q(lp.name), v.vehicle_no, q(v?.vehicle_customer ?? ""), q(v?.vehicle_city ?? ""), q(v?.lot_kind ?? ""), q(v?.source_kind ?? ""), idx+1, q(s.bin_id), q(pid), q(s.material_type), q(s.customer), q(s.customer_city), num(s.weight), num(s.distance), q(s.FG_text), q(s.FI_Rel_text), q(lastLocks.has(pid) ? "1" : "0")].join(","));
        });
      }
    }
    navigator.clipboard.writeText(rows.join("\n")).then(()=> alert("Copied CSV.")).catch(()=> alert("Blocked."));
  }

  // Close Modal
  btnCloseModal.addEventListener("click", () => {
    lpModal.classList.remove("active");
    modalContent.innerHTML = "";
  });

  // LP Click -> Open Full Screen Modal
  document.addEventListener("click", (e) => {
    const head = e.target.closest("." + LP_CARD_CLASS + " .cardHead");
    if (!head || !lastPayload) return;

    const card = head.closest("." + LP_CARD_CLASS);
    const lpId = card.getAttribute("data-lp-id");
    const lp = (lastPayload.loading_points || []).find(x => x.id === lpId);
    if (!lp) return;

    modalTitle.textContent = `Loading Point: ${lp.name}`;
    modalContent.innerHTML = buildLPInternalHtml(lp);
    lpModal.classList.add("active");
  });

  // Vehicle Accordion (inside Modal)
  modalContent.addEventListener("click", (e) => {
    const head = e.target.closest("." + VEH_CARD_CLASS + " .vehicleHead");
    if (!head) return;
    if (e.target.closest("button")) return;

    const card = head.closest("." + VEH_CARD_CLASS);
    const isCollapsed = card.classList.contains(VEH_COLLAPSED_CLASS);
    card.classList.toggle(VEH_COLLAPSED_CLASS);
    const chev = card.querySelector(".vehChev");
    if (chev) chev.textContent = isCollapsed ? "▾" : "▸";
  });

  // Lock/Release within Modal
  modalContent.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-vact]");
    if (!b || !lastPayload) return;

    const act = b.getAttribute("data-vact");
    const lpId = b.getAttribute("data-lp");
    const vNo = Number(b.getAttribute("data-vno") || 0);

    const lp = (lastPayload.loading_points || []).find(x => x.id === lpId);
    const v = (lp?.vehicles || []).find(x => Number(x.vehicle_no) === vNo);
    if (!v) return;

    try{
      const n = vehiclePlateIds(v).length;
      const cust = (v.vehicle_customer || "") + (v.vehicle_city ? ` (${v.vehicle_city})` : "");
      if (act === "lock") {
        if (confirm(`Lock vehicle ${v.vehicle_no}?\nCustomer: ${cust}\nItems: ${n}`)) {
          await lockVehicle(v);
          lpModal.classList.remove("active"); // Refresh UI
        }
      } else if (act === "release") {
        if (confirm(`Release vehicle ${v.vehicle_no}?`)) {
          await releaseVehicle(v);
          lpModal.classList.remove("active");
        }
      }
    } catch(err) { toast("Action failed: " + err.message); }
  });

  btnRefresh.addEventListener("click", refresh);
  btnCopy.addEventListener("click", copyCSV);
  if (customerFilterEl) customerFilterEl.addEventListener("keydown", (e) => { if (e.key === "Enter") refresh(); });
  [maxStopsEl, maxBinsEl, customerFilterEl].forEach(el => { if (el) el.addEventListener("change", saveState); });

  applyState();
  refresh();
})();
