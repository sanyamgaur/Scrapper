import { api } from "../api.js";
import { $, $$, esc, usd, inr, icon, toast, productCard, empty } from "../ui.js";
import { store } from "../store.js";
import { wireCards } from "./catalog.js";

export function render() {
  return `<div class="wrap" id="pdp">
    <div class="skel" style="height:420px"></div>
  </div>`;
}

export async function mount(params, nav) {
  let d;
  try {
    d = await api.product(params.id);
  } catch (e) {
    // A 403 here means the compliance engine refuses this SKU — say why.
    const why = e.body?.detail?.reason;
    $("#pdp").innerHTML = why
      ? `<div class="empty"><h3>We can’t ship this one</h3>
           <p>${esc(why)}</p>
           <p class="muted" style="margin-top:14px">Rule ${esc(e.body?.detail?.rule || "")}</p>
           <button class="btn" id="pdp-back" style="margin-top:18px">Back to shopping</button></div>`
      : empty("Product not found", "It may have been delisted.");
    const b = $("#pdp-back"); if (b) b.onclick = () => nav("#/c");
    return;
  }

  const p = d.product, pr = d.pricing, sh = d.shipping;
  const opts = sh.options || [];
  const fast = opts.reduce((a, b) => (a && a.transit_days[1] <= b.transit_days[1] ? a : b), null);
  const cheap = opts.reduce((a, b) => (a && a.total_usd <= b.total_usd ? a : b), null);
  const saving = p.mrp_inr && p.mrp_inr > p.price_inr
    ? Math.round((1 - p.price_inr / p.mrp_inr) * 100) : 0;

  $("#pdp").innerHTML = `
    <button class="btn btn-sm" id="pdp-back" style="margin-bottom:18px">
      ${icon("back")} Back</button>

    <div style="display:grid;grid-template-columns:minmax(0,460px) minmax(0,1fr);gap:40px"
         class="pdp-cols">
      <div>
        <div class="panel shot" style="padding:0;overflow:hidden;position:sticky;
             top:calc(var(--header-h) + 16px)">
          <div style="aspect-ratio:1;background:#fff;display:flex;align-items:center;
                      justify-content:center;padding:32px">
            ${p.image
              ? `<img src="${esc(p.image)}" alt="${esc(p.name)}"
                   style="max-height:100%;object-fit:contain"
                   onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'${esc((p.name || "?").trim().charAt(0).toUpperCase())}'}))">`
              : `<div class="ph">${esc((p.name || "?").trim().charAt(0).toUpperCase())}</div>`}
          </div>
        </div>
      </div>

      <div>
        <div class="muted">${esc(p.brand || "")}</div>
        <h1 class="h1" style="margin:4px 0 8px">${esc(p.name)}</h1>
        <div class="sub">${esc(p.unit || "")}</div>

        <div style="display:flex;align-items:baseline;gap:10px;margin:18px 0 4px">
          <span style="font-size:32px;font-weight:700;letter-spacing:-.03em">${usd(pr.list_price_usd)}</span>
          <span class="muted">${inr(p.price_inr)} on the shelf in Delhi</span>
        </div>
        ${saving ? `<span class="tag t-ok">${saving}% off Indian MRP</span>` : ""}

        <div style="margin:18px 0">
          <span class="tag ${p.in_stock ? "t-ok" : "t-bad"}" style="font-size:12px;padding:5px 10px">
            ${p.in_stock ? "Available in Delhi right now" : "Out of stock in Delhi"}
          </span>
        </div>

        ${p.in_stock ? `
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:14px">
            <select id="pdp-qty" class="iconbtn" style="padding:10px 12px">
              ${[1,2,3,4,5,6,8,10].map(n => `<option value="${n}">${n}</option>`).join("")}
            </select>
            <button class="btn btn-pri" id="pdp-add" style="flex:1">
              ${icon("plus")} Add to cart</button>
          </div>` : `
          <div class="note warn" style="margin-bottom:14px">
            We buy every order in Delhi after you place it, so we can’t promise this one
            until it’s back on the shelf.
          </div>`}

        <div class="panel" style="margin-top:8px">
          <div class="h2" style="font-size:14px;margin-bottom:10px">What you’ll pay</div>
          <div class="row"><span>Item</span><span>${usd(pr.goods_usd)}</span></div>
          <div class="row"><span>Sourcing &amp; handling in India</span><span>${usd(pr.sourcing_usd)}</span></div>
          <div class="row"><span>Shipping to the US</span><span>${usd(pr.freight_usd)}</span></div>
          <div class="row"><span>Duty &amp; customs</span><span>${usd((pr.duty_usd||0)+(pr.mpf_usd||0))}</span></div>
          <div class="row tot"><span>Total</span><span>${usd(pr.list_price_usd)}</span></div>
          ${pr.freight_ratio > 2 ? `
            <div class="note warn" style="margin-top:12px">
              Shipping is ${pr.freight_ratio.toFixed(1)}× what this item costs. Adding a few
              more items barely changes the shipping — the cart will show you what fits free.
            </div>` : ""}
        </div>

        ${opts.length ? `
        <div class="panel" style="margin-top:12px">
          <div class="h2" style="font-size:14px;margin-bottom:4px">How fast you get it</div>
          <p class="muted" style="margin:0 0 10px">You choose the carrier at checkout.</p>
          <div class="row"><span>Fastest — ${esc(fast.carrier_name)}</span>
            <span><b>${fast.transit_days[0]}–${fast.transit_days[1]} days</b> · ${usd(fast.total_usd)}</span></div>
          <div class="row"><span>Cheapest — ${esc(cheap.carrier_name)}</span>
            <span><b>${cheap.transit_days[0]}–${cheap.transit_days[1]} days</b> · ${usd(cheap.total_usd)}</span></div>
        </div>` : ""}

        <p class="muted" style="margin-top:16px;line-height:1.55">
          ${icon("box")} We buy this from a Delhi store after your order, consolidate it,
          and ship it to your door. Nothing is warehoused, so what you see is what
          the shelf in Delhi has today.
        </p>
      </div>
    </div>

    <div style="margin-top:46px">
      <h2 class="h2" style="margin-bottom:4px">Goes well in the same parcel</h2>
      <p class="muted" style="margin:0 0 14px">
        These are light for their value, so they add very little to your shipping.</p>
      <div id="pdp-rel">${""}</div>
    </div>`;

  $("#pdp-back").onclick = () => history.length > 1 ? history.back() : nav("#/c");
  const add = $("#pdp-add");
  if (add) add.onclick = () => {
    store.add({
      product_id: p.product_id, name: p.name, unit: p.unit, brand: p.brand,
      price_inr: p.price_inr, image: p.image, in_stock: p.in_stock,
    }, parseInt($("#pdp-qty").value, 10) || 1);
    toast("Added to cart");
  };

  try {
    const rel = await api.related(p.product_id);
    const items = rel.items.filter((x) => x.in_stock).slice(0, 6);
    $("#pdp-rel").innerHTML = items.length
      ? `<div class="grid">${items.map(productCard).join("")}</div>`
      : `<p class="muted">Nothing else on this shelf right now.</p>`;
    wireCards(nav);
  } catch {
    $("#pdp-rel").innerHTML = "";
  }
}
