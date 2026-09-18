import { api } from "../api.js";
import { $, $$, esc, productCard, skeletonGrid, empty, debounce, toast } from "../ui.js";
import { store } from "../store.js";

const PAGE = 48;
let state = { items: [], total: 0, offset: 0, loading: false };

const SORTS = [
  // Labelled for what the query actually does. The default ranks by absolute
  // rupee saving, so calling it "most relevant" would be hiding the rule.
  ["relevance", "Biggest savings"],
  ["price_asc", "Price: low to high"],
  ["price_desc", "Price: high to low"],
  ["value", "Best value for weight"],
  ["light", "Cheapest to ship"],
  ["name", "A–Z"],
];

export function render(params) {
  const q = params.q || "";
  // A generic "Everything" as a 26px bold headline announced nothing and ate
  // the first screen. Real context (a search or a category) still earns a
  // heading; browsing the full catalogue does not need one — the toolbar
  // below carries the page instead.
  const heading = q ? `Results for “${q}”` : (params.category || params.group || "");

  return `
    <div class="wrap">
      ${heading ? `<h1 class="h1" style="margin-bottom:18px">${esc(heading)}</h1>` : ""}

      <div class="toolbar">
        <span class="toolbar-label">${heading ? "Refine" : "All products"}</span>
        <div class="toolbar-controls">
          <label class="field-inline">
            <span class="switch">
              <input type="checkbox" id="f-stock" ${params.in_stock_only !== "0" ? "checked" : ""}>
              <span class="track"></span><span class="knob"></span>
            </span>
            <span style="text-transform:none;font-weight:500;color:var(--ink-2);font-size:13px">
              In stock only</span>
          </label>
          <span class="toolbar-div"></span>
          <label class="field-inline">
            <span>Sort by</span>
            <select id="f-sort">
              ${SORTS.map(([v, l]) =>
                `<option value="${v}" ${params.sort === v ? "selected" : ""}>${l}</option>`).join("")}
            </select>
          </label>
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
  // Quick-add sits inside the card, so its click must not also navigate.
  $$("[data-quick]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      const card = b.closest(".card");
      const d = JSON.parse(card.dataset.item || "null");
      if (!d) return;
      store.add(d, 1);
      toast("Added to cart");
    };
  });
}
