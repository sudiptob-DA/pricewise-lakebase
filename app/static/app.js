// PriceWise frontend — talks to the FastAPI /api endpoints.
const $ = (s) => document.querySelector(s);
const money = (v) => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, {maximumFractionDigits:0});
const monthName = (m) => ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m];
const getMonth = () => parseInt($("#month").value, 10);

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText);
  return r.json();
}

// Curated LOCAL images by property type (3 variants each, rotated by id so cards vary but are
// always contextually relevant). Bundled in /img — no external CDN, no random photos.
const TYPE_IMAGES = {
  "Summer Getaway":   ["summer-1","summer-2","summer-3"],
  "Urban Year-Round": ["urban-1","urban-2","urban-3"],
  "Historical Place": ["historical-1","historical-2","historical-3"],
  "Ski Resort":       ["ski-1","ski-2","ski-3"],
};
function imageFor(r) {
  const set = TYPE_IMAGES[r.property_type] || TYPE_IMAGES["Summer Getaway"];
  const pick = set[(r.property_id || 0) % set.length];
  return `img/${pick}.jpg`;
}
// Warm gradient fallback, varied by id so cards aren't identical.
const GRADS = [
  "linear-gradient(135deg,#FF5F46,#FFAB00)","linear-gradient(135deg,#2272B4,#00A972)",
  "linear-gradient(135deg,#98102A,#FF3621)","linear-gradient(135deg,#00A972,#2272B4)",
  "linear-gradient(135deg,#FFAB00,#FF5F46)","linear-gradient(135deg,#1B3139,#2272B4)"
];
function gradientFor(r) { return GRADS[(r.property_id || 0) % GRADS.length]; }

function skeletonCards(n=8) {
  return Array.from({length:n}).map(()=>`
    <div class="card skel">
      <div class="cardimg sk"></div>
      <div class="cardbody">
        <div class="sk-line w70"></div><div class="sk-line w40"></div>
        <div class="sk-pill"></div><div class="sk-line w50"></div>
      </div>
    </div>`).join("");
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  document.querySelectorAll(".tabpane").forEach(p => p.classList.add("hidden"));
  $("#pane-" + t.dataset.tab).classList.remove("hidden");
  if (t.dataset.tab === "insights") loadMarket();
});

/* ---------- search ---------- */
async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return;
  const params = new URLSearchParams({
    q, month: getMonth(), semantic: $("#semantic").checked,
    limit: 24
  });
  if ($("#maxprice").value) params.set("max_price", $("#maxprice").value);
  if ($("#dest").value) params.set("destination", $("#dest").value);

  $("#searchStatus").innerHTML = `<div class="statusline">Searching Lakebase…</div>`;
  $("#results").innerHTML = skeletonCards(8);
  try {
    const data = await api("/api/search?" + params.toString());
    const legend = data.semantic ? `
      <div class="legend">
        <span class="lg"><span class="chip both">★ Top match</span> found by AI <i>and</i> exact words — the strongest results</span>
        <span class="lg"><span class="chip vec">✦ AI found this</span> matched by meaning; a plain keyword search would miss it</span>
        <span class="lg"><span class="chip kw">Exact words</span> matched the words in the listing</span>
      </div>` : `<div class="legend"><span class="lg">Keyword-only mode — turn on <b>Smart understanding</b> to see what AI surfaces.</span></div>`;
    $("#searchStatus").innerHTML =
      `<div class="statusline">${data.count} listings · ${data.semantic ? "AI + keyword search" : "keyword only"} · ${monthName(data.month)}</div>${legend}`;
    renderResults(data.results);
  } catch (e) {
    $("#searchStatus").innerHTML = `<div class="err">Search failed: ${e.message}</div>`;
  }
}

function tagFor(r) {
  if (r.in_vector && r.in_keyword) return `<span class="tag both">★ Top match</span>`;
  if (r.in_vector) return `<span class="tag vec">✦ AI found this</span>`;
  return `<span class="tag kw">Exact words</span>`;
}

function renderResults(rows) {
  const grid = $("#results");
  grid.innerHTML = "";
  rows.forEach(r => {
    const uplift = (r.suggested_price && r.base_price)
      ? Math.round(100 * (r.suggested_price - r.base_price) / r.base_price) : null;
    const drivers = [];
    if (r.season_uplift > 0) drivers.push("season");
    if (r.comp_uplift > 0) drivers.push("comps");
    if (r.fx_uplift > 0) drivers.push("FX");
    if (r.holiday_uplift > 0) drivers.push("holiday");
    const el = document.createElement("div");
    el.className = "card" + (r.in_vector && !r.in_keyword ? " semantic" : "");
    el.innerHTML = `
      <div class="cardimg" style="background:${gradientFor(r)}">
        <img src="${imageFor(r)}" alt="" loading="lazy" onload="this.classList.add('loaded')" onerror="this.remove()"/>
        <span class="typebadge">${r.property_type || ""}</span>
        ${r.in_vector && !r.in_keyword ? '<span class="cornertag">✦ AI found this</span>' : ''}
      </div>
      <div class="cardbody">
        <div class="title">${r.display_name || r.title || "Untitled"}</div>
        <div class="sub">${r.title || ""} · ${r.destination || ""} <span class="pid">#${r.property_id}</span></div>
        <div class="pricepill">
          <b>${money(r.suggested_price)}</b><span>/night</span>
          ${uplift != null ? `<span class="up">${uplift>=0?"+":""}${uplift}%</span>` : ""}
        </div>
        ${tagFor(r)}
        <div class="why">base ${money(r.base_price)}${drivers.length? " · ↑ "+drivers.join(", ") : ""}</div>
      </div>
    `;
    el.onclick = () => openStudio(r.property_id);
    grid.appendChild(el);
  });
}

$("#go").onclick = runSearch;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
let currentStudioId = null;   // track the open Pricing Studio listing so month changes refresh it
let currentPrice = null;      // the pricing row currently shown (for Accept/Override)
$("#month").onchange = () => {
  if (!$("#pane-search").classList.contains("hidden")) runSearch();
  else if (!$("#pane-studio").classList.contains("hidden") && currentStudioId) openStudio(currentStudioId);
  else if (!$("#pane-insights").classList.contains("hidden")) loadMarket();
};

/* ---------- pricing studio ---------- */
async function openStudio(pid) {
  currentStudioId = pid;
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelector('.tab[data-tab="studio"]').classList.add("active");
  document.querySelectorAll(".tabpane").forEach(p => p.classList.add("hidden"));
  $("#pane-studio").classList.remove("hidden");
  $("#studioEmpty").classList.add("hidden");
  $("#studioContent").classList.remove("hidden");

  const m = getMonth();
  try {
    const [price, curve, comps, detail] = await Promise.all([
      api(`/api/pricing/${pid}?month=${m}`),
      api(`/api/pricing/${pid}/curve`),
      api(`/api/comps/${pid}?radius_mi=20&month=${m}`),
      api(`/api/property/${pid}`)
    ]);
    currentPrice = price;
    $("#stTitle").textContent = price.display_name || price.title;
    $("#stSub").textContent = `${price.title} · ${price.destination} · ${price.property_type} · ${monthName(m)}`;
    renderDescription(detail);
    renderWaterfall(price);
    renderImpact(price);
    renderCurve(curve.curve);
    renderComps(comps.comps);
    $("#overridePrice").value = "";
    // show any prior decision for this listing/month
    try {
      const dec = await api(`/api/decision/${pid}?month=${m}`);
      renderApplied(dec && dec.applied_price ? dec : null);
    } catch (e) { renderApplied(null); }
  } catch (e) {
    $("#stTitle").textContent = "Could not load pricing";
    $("#stWaterfall").innerHTML = `<div class="err">${e.message}</div>`;
  }
}

function renderDescription(d) {
  const el = $("#stDescription");
  if (!el) return;
  // search_text = "title. description. Located in <dest>, <country>. Amenities: ...".
  // Show the descriptive prose: drop the leading title and the trailing "Located in"/"Amenities" tails.
  let text = d.description || d.search_text || "";
  text = text.split(/\.\s*Located in/i)[0];              // cut the location/amenity tail
  if (d.title && text.startsWith(d.title)) text = text.slice(d.title.length).replace(/^[.\s]+/, "");
  const amen = d.amenities ? `<div class="amen">${d.amenities.split(",").map(a=>`<span class="pill2">${a.trim()}</span>`).join("")}</div>` : "";
  const hero = `<div class="studiohero" style="background:${gradientFor(d)}">
      <img src="${imageFor(d)}" alt="" onload="this.classList.add('loaded')" onerror="this.remove()"/>
    </div>`;
  el.innerHTML = `${hero}<div class="desc">${text.trim()}</div>${amen}`;
}

// Estimated extra revenue per month vs. holding the flat base price.
// Assumes ~18 booked nights/month (a transparent demo assumption).
const NIGHTS_PER_MONTH = 18;
function renderImpact(p) {
  const el = $("#stImpact");
  if (!el || !p.suggested_price || !p.base_price) { if (el) el.innerHTML = ""; return; }
  const perNight = p.suggested_price - p.base_price;
  const monthly = Math.round(perNight * NIGHTS_PER_MONTH);
  if (perNight <= 0) { el.innerHTML = ""; return; }
  el.innerHTML = `<span class="impact-num">+${money(monthly)}</span>
    estimated extra revenue this month vs. your flat base price
    <span class="muted">(${money(perNight)}/night × ~${NIGHTS_PER_MONTH} booked nights)</span>`;
}

function renderApplied(dec) {
  const el = $("#stApplied");
  if (!el) return;
  if (!dec) { el.innerHTML = ""; return; }
  const when = dec.decided_at ? new Date(dec.decided_at).toLocaleString() : "";
  el.innerHTML = `✓ You ${dec.action === "override" ? "set" : "accepted"} <b>${money(dec.applied_price)}</b>
    <span class="muted">· saved to Lakebase ${when}</span>`;
}

async function applyPrice(price, action) {
  if (!currentStudioId || !currentPrice) return;
  try {
    const r = await fetch("/api/apply-price", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ property_id: currentStudioId, month: getMonth(),
                             applied_price: price, action })
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    renderApplied({ applied_price: d.applied_price, action: d.action, decided_at: new Date().toISOString() });
  } catch (e) {
    $("#stApplied").innerHTML = `<span class="err">Could not save: ${e.message}</span>`;
  }
}

function wfRow(label, val, positive=true) {
  const cls = positive && val > 0 ? "pos" : "";
  const sign = val > 0 ? "+" : "";
  return `<div class="wf"><span>${label}</span><span class="${cls}">${val>0?sign:""}${money(val)}</span></div>`;
}
function renderWaterfall(p) {
  // Ordered big-lever first: peak season, then local competition, then holidays, then currency.
  $("#stWaterfall").innerHTML =
    `<div class="wf"><span>Your base price</span><span>${money(p.base_price)}</span></div>`
    + wfRow("Peak-season demand", p.season_uplift)
    + wfRow("Local competition", p.comp_uplift)
    + wfRow("Holiday demand", p.holiday_uplift)
    + wfRow("Currency (international guests)", p.fx_uplift)
    + `<div class="wf total"><span>Suggested nightly price</span><span>${money(p.suggested_price)}</span></div>`;
}
function renderCurve(curve) {
  const max = Math.max(...curve.map(c => c.suggested_price || 0), 1);
  $("#stCurve").innerHTML = curve.map(c => {
    const h = Math.round(100 * (c.suggested_price || 0) / max);
    const peak = (c.suggested_price || 0) >= 0.92 * max;
    return `<div class="bar ${peak?"peak":""}" style="height:${h}%" title="${monthName(c.target_month)}: ${money(c.suggested_price)}">
              <span>${monthName(c.target_month)}</span></div>`;
  }).join("");
}
function renderComps(comps) {
  if (!comps.length) { $("#stComps").innerHTML = `<div class="muted">No comps within 20 miles.</div>`; return; }
  $("#stComps").innerHTML = comps.map(c =>
    `<div class="comprow"><span>${c.display_name || c.title} <span class="muted">· ${c.title} · ${c.miles} mi</span></span>
     <span>${money(c.suggested_price || c.base_price)}</span></div>`).join("");
}
$("#acceptBtn").onclick = () => { if (currentPrice) applyPrice(currentPrice.suggested_price, "accept"); };
$("#overrideBtn").onclick = () => {
  const v = parseFloat($("#overridePrice").value);
  if (!isNaN(v) && v > 0) applyPrice(v, "override");
};
$("#studioRandom").onclick = (e) => { e.preventDefault(); loadSampleStudio(); };
async function loadSampleStudio() {
  try {
    const data = await api("/api/search?q=phuket beach villa pool&destination=Phuket&month=" + getMonth() + "&limit=1");
    if (data.results.length) openStudio(data.results[0].property_id);
  } catch (e) { /* ignore */ }
}

/* ---------- insights ---------- */
async function loadMarket() {
  try {
    const m = await api("/api/market?month=" + getMonth());
    $("#marketKpi").innerHTML = `
      <div class="box"><b>${(m.listings||0).toLocaleString()}</b>listings priced</div>
      <div class="box"><b>${money(m.avg_uplift)}</b>avg uplift / night</div>
      <div class="box"><b>${m.avg_uplift_pct ?? "—"}%</b>avg uplift</div>`;
  } catch (e) {
    $("#marketKpi").innerHTML = `<div class="err">${e.message}</div>`;
  }
}

/* ---------- init ---------- */
async function init() {
  try {
    const dests = await api("/api/destinations");
    $("#dest").innerHTML = `<option value="">All destinations</option>` +
      dests.map(d => `<option value="${d.destination}">${d.destination} (${d.n})</option>`).join("");
  } catch (e) { /* dropdown optional */ }
  runSearch();
}
init();
