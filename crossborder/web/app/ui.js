/* Shared rendering helpers. */

export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Escape anything that reaches innerHTML. Product names come from a scraped
    third-party catalogue and are never trusted as markup. */
export function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

export const usd = (n) =>
  n == null ? "—" : "$" + Number(n).toFixed(2);
export const inr = (n) =>
  n == null ? "" : "₹" + Math.round(Number(n)).toLocaleString("en-IN");

/** Keyword-matched icon for a department tile. Emoji rather than an SVG set,
    so a new category the catalogue introduces still gets a reasonable icon
    (via the fallback) instead of a missing glyph. */
export function deptIcon(name) {
  const n = (name || "").toLowerCase();
  const table = [
    [/home|lifestyle|decor|furnishing/, "🏠"],
    [/stationery|game|book/, "✏️"],
    [/chips|namkeen|snack/, "🍿"],
    [/oil|ghee|masala|spice/, "🌶️"],
    [/atta|rice|dal|grain|cereal/, "🌾"],
    [/tea|coffee|milk drink/, "☕"],
    [/feminine|hygiene/, "💗"],
    [/instant food|noodle|ready/, "🍜"],
    [/kitchenware|appliance/, "🍳"],
    [/beauty|cosmetic|skin|hair/, "💄"],
    [/bakery|biscuit|sweet|chocolate/, "🍪"],
    [/dairy|bread|egg/, "🥛"],
    [/fruit|vegetable/, "🥦"],
    [/drink|juice|beverage/, "🥤"],
    [/cleaner|repellent/, "🧴"],
    [/health|pharma|wellness/, "🩹"],
  ];
  for (const [re, ic] of table) if (re.test(n)) return ic;
  return "🛍️";
}

export function icon(name) {
  const p = {
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    cart:   '<circle cx="9" cy="20" r="1.4"/><circle cx="18" cy="20" r="1.4"/><path d="M2 3h2.5l2.6 12.4a1.5 1.5 0 0 0 1.5 1.2h8.6a1.5 1.5 0 0 0 1.5-1.2L21 7H6"/>',
    back:   '<path d="M15 19l-7-7 7-7"/>',
    check:  '<path d="M20 6 9 17l-5-5"/>',
    plus:   '<path d="M12 5v14M5 12h14"/>',
    box:    '<path d="M21 8v8a2 2 0 0 1-1 1.7l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.7l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
    spark:  '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
  }[name] || "";
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
}

let toastTimer;
export function toast(msg) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast"; el.className = "toast";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 2100);
}

/** A product thumbnail that degrades to an initial.

    Every image in this catalogue is a third-party CDN URL that can 404, be
    blocked, or be missing entirely, so no image is ever rendered without a
    fallback. Defined once because four different views need identical
    behaviour. */
/** Deterministic hue from a product id, so a given product always gets the
    same placeholder colour instead of flickering between renders. */
function hueOf(seed) {
  const s = String(seed || "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

/** Monogram placeholder. Rendered whenever there is no usable image — which
    is a real state, not an edge case, so it is designed rather than left grey. */
export function placeholder(p) {
  const letter = esc((p.name || "?").trim().charAt(0).toUpperCase());
  const h = hueOf(p.product_id || p.name);
  return `<div class="ph" style="--ph-h:${h}">${letter}</div>`;
}

export function imgSrc(p) {
  // Served from our own origin: /img/<id> hits the local cache and falls back
  // to the source CDN, so images survive hotlink protection and get faster as
  // the cache warms. Raw p.image is only used when there is no id to route on.
  return p.product_id ? `/img/${encodeURIComponent(p.product_id)}` : (p.image || "");
}

export function thumb(p, { size = "100%", radius = "var(--r-sm)" } = {}) {
  const src = imgSrc(p);
  // The monogram is always rendered underneath. Previously it only appeared via
  // onerror, so a tile stayed blank for as long as the request was pending —
  // and permanently blank if the image resolved to something undisplayable.
  return `<div class="thumb" style="width:${size};height:${size};border-radius:${radius}">
    ${placeholder(p)}
    ${src ? `<img loading="lazy" src="${esc(src)}" alt="" class="over"
              onerror="this.remove()">` : ""}
  </div>`;
}

/** Product card. Used by the catalogue grid, related rails and suggestions. */
export function productCard(p) {
  // Deliberately no "best in a bundle" badge. The median single-item freight
  // ratio on this lane is ~6x, so 92% of cards would carry it and it would
  // convey nothing. Parcel economics are a CART fact and are stated once, at
  // the top of the catalogue, instead of shouted on every tile.
  const off = p.mrp_inr && p.mrp_inr > p.price_inr
    ? Math.round((1 - p.price_inr / p.mrp_inr) * 100) : 0;
  return `
    <article class="card" data-pid="${esc(p.product_id)}"
      data-item='${esc(JSON.stringify({ product_id: p.product_id, name: p.name,
        unit: p.unit, brand: p.brand, price_inr: p.price_inr, image: p.image,
        in_stock: p.in_stock ? 1 : 0 }))}'>
      <div class="shot">
        ${placeholder(p)}
        ${imgSrc(p) ? `<img loading="lazy" src="${esc(imgSrc(p))}" alt="" class="over"
             onerror="this.remove()">` : ""}
        ${off >= 10 ? `<span class="off">${off}% off</span>` : ""}
        ${p.in_stock ? `<button class="quick" data-quick="${esc(p.product_id)}"
            aria-label="Add ${esc(p.name)} to cart" title="Add to cart">+</button>` : ""}
      </div>
      <div class="body">
        <div class="nm">${esc(p.name)}</div>
        <div class="meta">${esc(p.brand || "")}${p.brand && p.unit ? " · " : ""}${esc(p.unit || "")}</div>
        <span class="tag ${p.in_stock ? "t-ok" : "t-bad"}">${p.in_stock ? "Available" : "Out of stock"}</span>
        <div class="price">
          <span class="usd">${usd(p.list_price_usd)}</span>
          <span class="inr">${inr(p.price_inr)} in India</span>
        </div>
      </div>
    </article>`;
}

export function skeletonGrid(n = 12) {
  return `<div class="grid">${Array.from({ length: n },
    () => `<div class="skel" style="aspect-ratio:.74"></div>`).join("")}</div>`;
}

export function empty(title, body) {
  return `<div class="empty"><h3>${esc(title)}</h3><p>${esc(body)}</p></div>`;
}

/** Debounce, for the search box. */
export function debounce(fn, ms = 260) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}


/** Translate engine warnings into shopper language.

    pricing.py writes for an operator — "Unsellable as a standalone order,
    bundle it or delist" is a merchandising instruction, not something to show
    someone buying lentils. Anything without a customer-facing equivalent is
    dropped rather than leaked, because the cart already communicates the same
    fact through the disabled CTA and the headroom bar. */
export function customerWarning(w) {
  const s = String(w || "");
  if (/freight is ([\d.]+)x/i.test(s)) {
    const x = s.match(/freight is ([\d.]+)x/i)[1];
    return `Shipping costs ${x}× what these items are worth. Add a few more and the `
         + `shipping barely moves — see the suggestions below.`;
  }
  if (/below the .*floor|not worth shipping alone/i.test(s)) {
    return "This is too small to ship on its own. Add a couple more items.";
  }
  if (/weight partly estimated/i.test(s)) return null;   // internal precision note
  if (/negative margin/i.test(s)) return null;           // strictly our problem
  return null;
}
