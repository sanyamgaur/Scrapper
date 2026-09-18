import { api } from "../api.js";
import { $, $$, esc, usd, productCard, skeletonGrid, icon } from "../ui.js";
import { wireCards } from "./catalog.js";

/* Curated rails. Each is a real query against the catalogue rather than a
   hand-picked list, so they stay correct as stock and prices move. */
/* Every rail is scoped to a food category. An unscoped "high value per gram"
   query is mathematically correct and commercially absurd — it returned ball
   pens and a Titan watch on the front page of a grocery store. */
const RAILS = [
  { key: "deals",  title: "Biggest savings today",
    sub: "Real discounts off Indian MRP, not a markup dressed up as a sale.",
    params: { sort: "relevance", in_stock_only: true, limit: 12 } },
  { key: "spice",  title: "Big flavour, barely any weight",
    sub: "Spices are the best thing to ship from India: tiny, light, and they "
       + "cost almost nothing to add to a parcel.",
    params: { category: "Oil, Ghee & Masala", sort: "value",
              in_stock_only: true, limit: 12 } },
  { key: "pantry", title: "Pantry staples",
    sub: "Dals, flours and rice that keep for months.",
    params: { category: "Atta, Rice & Dal", in_stock_only: true, limit: 12 } },
  { key: "snack",  title: "Snacks worth the airmail",
    sub: "Namkeen, chips and everything in between.",
    params: { category: "Chips & Namkeen", in_stock_only: true, limit: 12 } },
  { key: "sweet",  title: "Something sweet",
    sub: "Shelf-stable sweets and chocolate that survive the journey.",
    params: { category: "Sweets & Chocolates", in_stock_only: true, limit: 12 } },
  { key: "chai",   title: "Chai and coffee",
    sub: "Loose leaf, masala chai and South Indian filter coffee.",
    params: { category: "Tea, Coffee & Milk Drinks", in_stock_only: true, limit: 12 } },
];

export function render() {
  return `
    <section class="hero">
      <div class="wrap hero-in">
        <div>
          <h1>The Indian grocery aisle,<br><em>delivered to your door.</em></h1>
          <p>We buy from real Delhi shelves the day you order, consolidate it,
             and ship it to you in the US. No warehouse, no substitutions you
             didn't ask for.</p>
          <div class="hero-cta">
            <a class="btn btn-pri" href="#/c">Browse everything</a>
            <a class="btn" href="#/c?category=Oil%2C%20Ghee%20%26%20Masala">Start with spices</a>
          </div>
          <div class="hero-stats" id="hero-stats"></div>
        </div>
      </div>
    </section>

    <div class="wrap">
      <div class="depts" id="depts"></div>
      <div id="rails">${skeletonGrid(6)}</div>
    </div>`;
}

export async function mount(_params, nav) {
  // Department tiles
  try {
    const f = await api.facets();
    // Food departments first. The facet list is ordered by size, which put
    // "Home & Lifestyle" (2,923 items) at the front of a grocery store.
    const FOOD = ["Oil, Ghee & Masala", "Atta, Rice & Dal", "Chips & Namkeen",
                  "Sweets & Chocolates", "Tea, Coffee & Milk Drinks",
                  "Dry Fruits & Cereals", "Instant Food", "Bakery & Biscuits",
                  "Sauces & Spreads", "Drinks & Juices"];
    const rank = (n) => { const i = FOOD.indexOf(n); return i === -1 ? 99 : i; };
    const top = f.categories.filter(c => c.name)
      .sort((a, b) => rank(a.name) - rank(b.name) || b.n - a.n)
      .slice(0, 8);
    $("#depts").innerHTML = top.map((c, i) => `
      <a class="dept" href="#/c?category=${encodeURIComponent(c.name)}" style="--d:${i * 37}">
        <span class="dept-n">${esc(c.name)}</span>
        <span class="dept-c">${c.n.toLocaleString()} items</span>
      </a>`).join("");
    // "products you can order" must mean what is actually buyable today. The
    // facet count is the whole listable catalogue (6,173) but only ~2,179 are
    // in stock in Delhi right now, and this shop's whole pitch is not
    // overstating what it can get.
    const live = await api.catalog({ in_stock_only: true, limit: 1 });
    const total = f.categories.reduce((n, c) => n + c.n, 0);
    $("#hero-stats").innerHTML = `
      <div><b>${live.total.toLocaleString()}</b><span>in stock in Delhi today</span></div>
      <div><b>${total.toLocaleString()}</b><span>products in the catalogue</span></div>
      <div><b>3–21</b><span>days to your door</span></div>`;
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
      <div class="railscroll">${r.items.map(productCard).join("")}</div>
    </section>`).join("");

  wireCards(nav);
}
