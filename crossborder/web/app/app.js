/* Shell: hash router, header wiring, view lifecycle.

   Hash routing rather than the History API so the whole app is a single static
   page FastAPI can serve without catch-all rewrites. */

import { api } from "./api.js";
import { $, $$, esc, debounce } from "./ui.js";
import { store } from "./store.js";

import * as home     from "./views/home.js";
import * as catalog  from "./views/catalog.js";
import * as product  from "./views/product.js";
import * as cart     from "./views/cart.js";
import * as checkout from "./views/checkout.js";
import * as order    from "./views/order.js";

const ROUTES = [
  [/^#\/p\/(?<id>.+)$/,      product],
  [/^#\/cart$/,              cart],
  [/^#\/checkout$/,          checkout],
  [/^#\/order\/(?<id>.+)$/,  order],
  [/^#\/c(?:\?(?<query>.*))?$/, catalog],
  [/^#\/?$/,                 home],
];

function parse(hash) {
  for (const [re, view] of ROUTES) {
    const m = hash.match(re);
    if (!m) continue;
    const params = { ...(m.groups || {}) };
    if (params.query) {
      new URLSearchParams(params.query).forEach((v, k) => (params[k] = v));
      delete params.query;
    }
    return { view, params };
  }
  return { view: home, params: {} };
}

function nav(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

let current = null;

async function render() {
  const { view, params } = parse(location.hash || "#/");
  current = view;
  const host = $("#view");
  host.innerHTML = view.render(params);
  // Land at the top on every navigation, except when only the query changed.
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  try {
    await view.mount(params, nav);
  } catch (err) {
    host.innerHTML = `<div class="wrap"><div class="empty">
      <h3>Something broke on this page</h3>
      <p>${esc(err.message || "Unknown error")}</p></div></div>`;
  }
  syncNav(params);
}

/* ------------------------------------------------------------------ chrome */

function syncCart() {
  const n = store.count;
  $("#cartn").textContent = n;
  $("#cartbtn").style.opacity = n ? "1" : ".9";
}

async function buildNav() {
  try {
    const f = await api.facets();
    const cats = f.categories.filter(c => c.name).slice(0, 12);
    $("#nav").innerHTML =
      `<a href="#/c" data-cat="">All products</a>` +
      cats.map(c => `<a href="#/c?category=${encodeURIComponent(c.name)}"
        data-cat="${esc(c.name)}">${esc(c.name)}</a>`).join("");
    syncNav(parse(location.hash || "#/").params);
  } catch { /* nav is progressive enhancement; the app works without it */ }
}

function syncNav(params) {
  const active = params?.category || "";
  $$("#nav a").forEach(a => a.classList.toggle("on", (a.dataset.cat || "") === active));
  const q = $("#q");
  if (q && document.activeElement !== q) q.value = params?.q || "";
}

/* Theme: respect the OS by default, remember an explicit choice. */
const TKEY = "sourced.theme";
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem(TKEY)); } catch {}
$("#theme").onclick = () => {
  const cur = document.documentElement.dataset.theme;
  const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
  applyTheme(next);
  try { next ? localStorage.setItem(TKEY, next) : localStorage.removeItem(TKEY); } catch {}
};

$("#q").addEventListener("input", debounce((e) => {
  const v = e.target.value.trim();
  nav(v ? `#/c?q=${encodeURIComponent(v)}` : "#/c");
}, 300));

store.subscribe(syncCart);
window.addEventListener("hashchange", render);

syncCart();
buildNav();
render();
