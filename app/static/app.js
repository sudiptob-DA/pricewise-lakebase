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

  $("#searchStatus").textContent = "Searching Lakebase…";
  $("#results").innerHTML = "";
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
      <div class="title">${r.display_name || r.title || "Untitled"}</div>
      <div class="sub">${r.title || ""} · ${r.destination || ""} <span class="pid">#${r.property_id}</span></div>
      <div class="pricepill">
        <b>${money(r.suggested_price)}</b><span>/night</span>
        ${uplift != null ? `<span class="up">${uplift>=0?"+":""}${uplift}%</span>` : ""}
      </div>
      ${tagFor(r)}
      <div class="why">base ${money(r.base_price)}${drivers.length? " · ↑ "+drivers.join(", ") : ""}</div>
    `;
    el.onclick = () => openStudio(r.property_id);
    grid.appendChild(el);
  });
}

$("#go").onclick = runSearch;
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
let currentStudioId = null;   // track the open Pricing Studio listing so month changes refresh it
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
    $("#stTitle").textContent = price.display_name || price.title;
    $("#stSub").textContent = `${price.title} · ${price.destination} · ${price.property_type} · ${monthName(m)}`;
    renderDescription(detail);
    renderWaterfall(price);
    renderCurve(curve.curve);
    renderComps(comps.comps);
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
  el.innerHTML = `<div class="desc">${text.trim()}</div>${amen}`;
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
