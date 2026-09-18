import { api } from "../api.js";
import { $, $$, esc, productCard, skeletonGrid, empty, debounce, toast } from "../ui.js";
import { store } from "../store.js";

const PAGE = 48;
let state = { items: [], total: 0, offset: 0, loading: false };

const SORTS = [
  ["relevance", "Most relevant"],
  ["price_asc", "Price: low to high"],
  ["price_desc", "Price: high to low"],
  ["value", "Best value for weight"],
  ["light", "Cheapest to ship"],
  ["name", "A–Z"],
];

export function render(params) {
  const q = params.q || "";
  const heading = q ? `“${q}”` : params.category || params.group || "Everything";
  return `
    <div class="wrap">
      <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px">
        <div>
          <h1 class="h1">${esc(heading)}</h1>
          <p class="sub" id="cat-count">Loading…</p>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <label class="muted" style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="f-stock" ${params.in_stock_only !== "0" ? "checked" : ""}>
            In stock only
          </label>
          <select id="f-sort" class="iconbtn" style="padding-right:10px">
            ${SORTS.map(([v, l]) =>
              `<option value="${v}" ${params.sort === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
      </div>
      <div class="note info" style="margin-bottom:18px;display:flex;gap:10px;align-items:flex-start">
        <span style="font-size:15px;line-height:1">📦</span>
        <span>Shipping is charged per parcel, not per item — so a basket of three or four
        things costs barely more to ship than one. Your cart shows exactly how much
        weight you have left to fill.</span>
      </div>
      <div id="cat-grid">${skeletonGrid()}</div>
      <div style="text-align:center;margin-top:28px">
        <button class="btn" id="cat-more" hidden>Load more</button>
      </div>
    </div>`;
}

export async function mount(params, nav) {
  state = { items: [], total: 0, offset: 0, loading: false };

  const load = async (append = false) => {
    if (state.loading) return;
    state.loading = true;
    const grid = $("#cat-grid");
    if (!append) grid.innerHTML = skeletonGrid();
    try {
      const d = await api.catalog({
        q: params.q || "",
        category: params.category || "",
        group: params.group || "",
        brand: params.brand || "",
        sort: $("#f-sort")?.value || params.sort || "relevance",
        in_stock_only: $("#f-stock")?.checked ?? true,
        limit: PAGE,
        offset: state.offset,
      });
      state.total = d.total;
      state.items = append ? state.items.concat(d.items) : d.items;
      state.offset += d.items.length;

      $("#cat-count").textContent =
        `${d.total.toLocaleString()} product${d.total === 1 ? "" : "s"} you can order`;

      grid.innerHTML = state.items.length
        ? `<div class="grid">${state.items.map(productCard).join("")}</div>`
        : empty("Nothing here yet",
                "Try a different search, or switch off “in stock only” to see what we can source.");

      const more = $("#cat-more");
      more.hidden = state.offset >= state.total;
      wireCards(nav);
    } catch {
      grid.innerHTML = empty("Could not load the catalogue",
                             "Check the server is running, then try again.");
    } finally {
      state.loading = false;
    }
  };

  $("#f-sort").onchange = () => { state.offset = 0; load(false); };
  $("#f-stock").onchange = () => { state.offset = 0; load(false); };
  $("#cat-more").onclick = () => load(true);
  await load(false);
}

/** Cards navigate; they deliberately have no inline "add" button. On this lane
    a customer needs the delivery window and the bundle economics before
    committing, and those only fit on the product page. */
export function wireCards(nav) {
  $$(".card[data-pid]").forEach((el) => {
    el.onclick = () => nav(`#/p/${el.dataset.pid}`);
  });
}
