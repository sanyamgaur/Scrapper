import { api } from "../api.js";
import { $, $$, esc, usd, productCard, skeletonGrid, icon, deptIcon } from "../ui.js";
import { wireCards } from "./catalog.js";

/* Curated rails. Each is a real query against the catalogue rather than a
   hand-picked list, so they stay correct as stock and prices move.

   Rails span the WHOLE catalogue, not just the food end of it. Only 33% of
   listable SKUs are food; Home & Lifestyle and Stationery & Games are the two
   largest departments by a distance. An earlier version showed six food rails
   and described a shop this catalogue is not. */
const RAILS = [
  { key: "deals",  title: "Biggest savings today",
    sub: "Ranked by what you actually save against Indian MRP, across every department.",
    params: { sort: "relevance", in_stock_only: true, limit: 20 } },
  { key: "home",   title: "Home & living",
    sub: "Bedding, kitchen, decor and the everyday things that are hard to find abroad.",
    params: { category: "Home & Lifestyle", sort: "price_asc",
              in_stock_only: true, limit: 20 } },
  { key: "snack",  title: "Snacks & namkeen",
    sub: "Chips, mixtures and everything in between.",
    params: { category: "Chips & Namkeen", in_stock_only: true, limit: 20 } },
  { key: "spice",  title: "Spices & masala",
    sub: "The best thing to ship from India: tiny, light, and almost free to add to a parcel.",
    params: { category: "Oil, Ghee & Masala", sort: "value",
              in_stock_only: true, limit: 20 } },
  { key: "stat",   title: "Stationery & games",
    sub: "Pens, notebooks, craft supplies and board games.",
    params: { category: "Stationery & Games", in_stock_only: true, limit: 20 } },
  { key: "pantry", title: "Pantry staples",
    sub: "Dals, flours and rice that keep for months.",
    params: { category: "Atta, Rice & Dal", in_stock_only: true, limit: 20 } },
  { key: "chai",   title: "Chai & coffee",
    sub: "Loose leaf, masala chai and South Indian filter coffee.",
    params: { category: "Tea, Coffee & Milk Drinks", in_stock_only: true, limit: 20 } },
];

/* Trust row: what the SERVICE promises, never a number pulled off a database
   row count. Counts age the moment stock moves; these claims don't. */
const TRUST = [
  { ic: "box",   t: "Bought after you order — nothing sits in a warehouse" },
  { ic: "check", t: "Duty & customs already included in the price you see" },
  { ic: "cart",  t: "You choose the carrier: days matter as much as dollars" },
];

export function render() {
  return `
    <section class="hero">
      <div class="wrap hero-in">
        <div>
          <h1>India's shelves,<br><em>delivered to your door.</em></h1>
          <p>Snacks and spices, bedding and kitchenware, stationery and skincare —
             we buy it from real Delhi shops the day you order, consolidate it,
             and ship it to you in the US. No substitutions you didn't ask for.</p>
          <div class="hero-cta">
            <a class="btn btn-pri" href="#/c">Browse everything</a>
            <a class="btn" href="#/c?sort=value&in_stock_only=1">Cheapest things to ship</a>
          </div>
          <div class="trust-row">
            ${TRUST.map(t => `
              <div><span class="ic">${icon(t.ic)}</span><span>${esc(t.t)}</span></div>`).join("")}
          </div>
        </div>
      </div>
    </section>

    <div class="wrap">
      <section style="margin-bottom:40px">
        <div class="section-h"><h2 class="h2">Shop by department</h2></div>
        <div class="depts" id="depts"></div>
      </section>
      <div id="rails">${skeletonGrid(6)}</div>
    </div>`;
}

export async function mount(_params, nav) {
  // Department tiles — in catalogue order, so no department is implied to
  // matter more than another.
  try {
    const f = await api.facets();
    const top = f.categories.filter(c => c.name).slice(0, 8);
    $("#depts").innerHTML = top.map((c, i) => `
      <a class="dept" href="#/c?category=${encodeURIComponent(c.name)}" style="--d:${i * 37}">
        <span class="dept-ic">${deptIcon(c.name)}</span>
        <span class="dept-n">${esc(c.name)}</span>
        <span class="dept-go">${icon("back")}</span>
      </a>`).join("");
  } catch { $("#depts").innerHTML = ""; }

  // Rails, loaded together so the page settles in one paint.
  const results = await Promise.all(RAILS.map(r =>
    api.catalog(r.params).then(d => ({ ...r, items: d.items })).catch(() => null)));

  // Categories overlap, so the same SKU surfaced in several rails. Keep the
  // first appearance only; a front page that repeats itself looks broken.
  const seen = new Set();
  for (const r of results) {
    if (!r) continue;
    r.items = r.items.filter(i => !seen.has(i.product_id) && seen.add(i.product_id));
  }

  $("#rails").innerHTML = results.filter(r => r && r.items.length).map(r => `
    <section class="rail">
      <div class="rail-h">
        <div>
          <h2 class="h2">${esc(r.title)}</h2>
          <p class="muted" style="margin:3px 0 0">${esc(r.sub)}</p>
        </div>
        <a class="btn btn-sm" href="#/c?${new URLSearchParams(
            Object.fromEntries(Object.entries(r.params)
              .filter(([k]) => ["category", "sort"].includes(k)))).toString()}">
          See all</a>
      </div>
      <div class="railscroll">${r.items.slice(0, 12).map(productCard).join("")}</div>
    </section>`).join("");

  wireCards(nav);
}
