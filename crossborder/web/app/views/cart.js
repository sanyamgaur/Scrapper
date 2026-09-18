import { api } from "../api.js";
import { $, $$, esc, usd, inr, icon, toast, empty, customerWarning, thumb } from "../ui.js";
import { store, carrier } from "../store.js";

export function render() {
  return `<div class="wrap">
    <h1 class="h1" style="margin-bottom:18px">Your cart</h1>
    <div id="cart-root"></div>
  </div>`;
}

export async function mount(_params, nav) {
  const draw = async () => {
    const root = $("#cart-root");
    if (store.isEmpty) {
      root.innerHTML = `${empty("Your cart is empty",
        "Shipping from India only makes sense with a few items, so build a basket.")}
        <div style="text-align:center"><button class="btn btn-pri" id="c-shop">Start shopping</button></div>`;
      $("#c-shop").onclick = () => nav("#/c");
      return;
    }

    root.innerHTML = `
      <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,380px);gap:28px"
           class="cart-cols">
        <div>
          <div class="panel" id="c-lines" style="padding:0"></div>
          <div id="c-basket" style="margin-top:20px"></div>
        </div>
        <div>
          <div class="panel" id="c-sum" style="position:sticky;top:calc(var(--header-h) + 16px)">
            <div class="skel" style="height:240px"></div>
          </div>
        </div>
      </div>`;

    $("#c-lines").innerHTML = Object.entries(store.cart).map(([id, c]) => `
      <div style="display:flex;gap:13px;padding:14px;border-bottom:1px solid var(--line)">
        ${thumb(c.item, { size: "62px" })}
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:14px;line-height:1.3">${esc(c.item.name)}</div>
          <div class="muted">${esc(c.item.unit || "")} · ${inr(c.item.price_inr)}</div>
          <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
            <button class="btn btn-sm" data-dec="${esc(id)}" aria-label="Decrease">−</button>
            <span style="min-width:22px;text-align:center;font-weight:600">${c.qty}</span>
            <button class="btn btn-sm" data-inc="${esc(id)}" aria-label="Increase">+</button>
            <button class="btn btn-sm" data-rm="${esc(id)}" style="margin-left:auto;color:var(--ink-3)">Remove</button>
          </div>
        </div>
      </div>`).join("");

    $$("[data-inc]").forEach(b => b.onclick = () => {
      const id = b.dataset.inc; store.setQty(id, store.cart[id].qty + 1); draw();
    });
    $$("[data-dec]").forEach(b => b.onclick = () => {
      const id = b.dataset.dec; store.setQty(id, store.cart[id].qty - 1); draw();
    });
    $$("[data-rm]").forEach(b => b.onclick = () => {
      store.remove(b.dataset.rm); toast("Removed"); draw();
    });

    await Promise.all([drawQuote(nav), drawBasket(draw)]);
  };

  await draw();
}

async function drawQuote(nav) {
  const el = $("#c-sum");
  if (!el) return;
  let d;
  try {
    d = await api.quote(store.lines, carrier.get());
  } catch {
    el.innerHTML = `<div class="note bad">We couldn’t price this cart. Try again in a moment.</div>`;
    return;
  }
  const p = d.pricing, s = d.shipping;
  const chosen = carrier.get() || p.carrier;

  el.innerHTML = `
    <div class="h2" style="font-size:15px;margin-bottom:10px">Choose your shipping</div>
    ${(s.options || []).map(o => `
      <label style="display:flex;gap:10px;align-items:center;padding:10px;border:1px solid
             ${o.carrier_code === chosen ? "var(--accent)" : "var(--line)"};
             border-radius:var(--r-sm);margin-bottom:7px;cursor:pointer;
             background:${o.carrier_code === chosen ? "var(--accent-soft)" : "transparent"}">
        <input type="radio" name="carrier" value="${esc(o.carrier_code)}"
               ${o.carrier_code === chosen ? "checked" : ""} style="accent-color:var(--accent)">
        <span style="flex:1;min-width:0">
          <span style="font-weight:600;font-size:13.5px">${esc(o.carrier_name)}</span>
          <span class="muted" style="display:block">${o.transit_days[0]}–${o.transit_days[1]} days · ${esc(o.tracking)} tracking</span>
        </span>
        <b style="white-space:nowrap">${usd(o.total_usd)}</b>
      </label>`).join("")}

    <div style="margin-top:16px">
      <div class="row"><span>Items</span><span>${usd(p.goods_usd)}</span></div>
      <div class="row"><span>Sourcing &amp; handling</span><span>${usd(p.sourcing_usd)}</span></div>
      <div class="row"><span>Shipping</span><span>${usd(p.freight_usd)}</span></div>
      <div class="row"><span>Duty &amp; customs</span><span>${usd((p.duty_usd||0)+(p.mpf_usd||0))}</span></div>
      <div class="row tot"><span>Total</span><span>${usd(p.list_price_usd)}</span></div>
      <div class="row muted"><span>Parcel weight</span>
        <span>${(s.weight.chargeable_g/1000).toFixed(2)} kg</span></div>
    </div>

    ${(p.warnings || []).map(customerWarning).filter(Boolean)
        .map(w => `<div class="note warn" style="margin-top:10px">${esc(w)}</div>`).join("")}

    <button class="btn btn-pri btn-blk" id="c-go" style="margin-top:14px"
            ${p.viable ? "" : "disabled"}>
      ${p.viable ? `Checkout · ${usd(p.list_price_usd)}` : "Add a little more to ship this"}
    </button>
    ${p.viable ? "" : `<p class="muted" style="text-align:center;margin-top:8px">
      Shipping costs more than these goods are worth. The suggestions below fix that.</p>`}`;

  $$('input[name="carrier"]').forEach(r => r.onchange = () => {
    carrier.set(r.value);
    drawQuote(nav);
  });
  const go = $("#c-go");
  if (go) go.onclick = () => nav("#/checkout");
}

/** The basket builder: free weight the customer has already paid for. */
async function drawBasket(refresh) {
  const el = $("#c-basket");
  if (!el) return;
  let a;
  try {
    a = await api.advise(store.lines, carrier.get());
  } catch { el.innerHTML = ""; return; }
  if (!a.suggestions?.length) { el.innerHTML = ""; return; }

  const free = a.free_headroom_g, billed = a.billed_g || 1, used = a.chargeable_g;
  el.innerHTML = `
    <div class="panel">
      <div class="h2" style="font-size:15px">${icon("spark")} Make this parcel worth sending</div>
      <p class="sub" style="margin:6px 0 0">${esc(a.headline)}</p>

      ${free > 20 ? `
        <div style="margin:14px 0 16px">
          <div style="height:10px;border-radius:var(--r-full);overflow:hidden;display:flex;background:var(--line)">
            <div style="width:${(used/billed*100).toFixed(1)}%;background:var(--accent)"></div>
            <div style="width:${(free/billed*100).toFixed(1)}%;
                        background:color-mix(in srgb,var(--ok) 45%,transparent)"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:11.5px;
                      color:var(--ink-3);margin-top:6px">
            <span>${(used/1000).toFixed(2)} kg packed</span>
            <b style="color:var(--ok)">${free.toFixed(0)} g still free</b>
          </div>
        </div>` : ""}

      <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:11px">
        ${a.suggestions.map(s => `
          <div class="panel" style="padding:9px;box-shadow:none;
               ${s.fits_free_headroom ? "border-color:color-mix(in srgb,var(--ok) 45%,transparent);background:var(--ok-soft)" : ""}">
            <div style="margin-bottom:8px">${thumb(s, { size: "100%" })
              .replace('height:100%', 'height:64px')}</div>
            <div style="font-size:12.5px;font-weight:600;line-height:1.3;
                        display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
                        overflow:hidden;min-height:32px">${esc(s.name)}</div>
            <div style="font-size:11px;color:${s.fits_free_headroom ? "var(--ok)" : "var(--ink-3)"};
                        margin:4px 0 8px;line-height:1.35">${esc(s.reason)}</div>
            <div style="display:flex;align-items:center;gap:7px">
              <b style="font-size:13.5px">${usd(s.list_price_usd)}</b>
              <button class="btn btn-sm" data-add='${esc(JSON.stringify({
                product_id: s.product_id, name: s.name, unit: s.unit,
                price_inr: s.price_inr, image: s.image, in_stock: 1 }))}'
                style="margin-left:auto">Add</button>
            </div>
          </div>`).join("")}
      </div>
    </div>`;

  $$("[data-add]").forEach(b => b.onclick = () => {
    try {
      store.add(JSON.parse(b.dataset.add), 1);
      toast("Added — shipping unchanged");
      refresh();
    } catch { /* malformed payload, ignore */ }
  });
}
