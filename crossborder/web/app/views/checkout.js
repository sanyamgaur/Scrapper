import { api } from "../api.js";
import { $, $$, esc, usd, icon, toast } from "../ui.js";
import { store, carrier } from "../store.js";

export function render() {
  return `<div class="wrap" style="max-width:920px">
    <h1 class="h1" style="margin-bottom:6px">Checkout</h1>
    <p class="sub" style="margin:0 0 22px">
      We buy your items in Delhi after you order, then ship them to you.</p>
    <div id="co-root"></div>
  </div>`;
}

export async function mount(_params, nav) {
  if (store.isEmpty) { nav("#/cart"); return; }

  const root = $("#co-root");
  root.innerHTML = `
    <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,320px);gap:26px"
         class="cart-cols">
      <form id="co-form" class="panel" novalidate>
        <div class="h2" style="font-size:15px;margin-bottom:12px">Where it's going</div>
        <div style="display:grid;gap:12px">
          <label>Email
            <input name="email" type="email" required autocomplete="email"
                   placeholder="you@example.com" class="fld">
            <small class="muted">We'll send order updates here.</small>
          </label>
          <label>Full name
            <input name="name" required autocomplete="name" placeholder="Asha Rao" class="fld"></label>
          <label>Street address
            <input name="address1" required autocomplete="address-line1"
                   placeholder="12 W 21st St, Apt 4" class="fld"></label>
          <div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px">
            <label>City
              <input name="city" required autocomplete="address-level2" placeholder="New York" class="fld"></label>
            <label>State
              <input name="state" required autocomplete="address-level1" placeholder="NY"
                     maxlength="2" style="text-transform:uppercase" class="fld"></label>
            <label>ZIP
              <input name="customer_zip" required autocomplete="postal-code"
                     inputmode="numeric" maxlength="5" placeholder="10001" class="fld"></label>
          </div>
        </div>
        <div id="co-err"></div>
        <button class="btn btn-pri btn-blk" id="co-place" style="margin-top:18px">
          ${icon("check")} Place order</button>
        <p class="muted" style="margin-top:10px;line-height:1.5">
          Nothing is charged until we've confirmed every item is still on the shelf in Delhi.
          If something sold out, we tell you before taking payment.</p>
      </form>

      <div>
        <div class="panel" id="co-sum"><div class="skel" style="height:200px"></div></div>
      </div>
    </div>`;

  // Scoped field styling; the form is the only place the app takes typed input.
  if (!$("#co-style")) {
    const st = document.createElement("style");
    st.id = "co-style";
    st.textContent = `
      #co-form label{display:block;font-size:12.5px;font-weight:600;color:var(--ink-2)}
      #co-form .fld{width:100%;margin-top:5px;padding:10px 12px;border:1px solid var(--line-2);
        border-radius:var(--r-sm);background:var(--bg);font-size:14px}
      #co-form .fld:invalid:not(:placeholder-shown){border-color:var(--bad)}
      #co-form small{display:block;margin-top:4px;font-weight:400}`;
    document.head.appendChild(st);
  }

  let quote;
  try {
    quote = await api.quote(store.lines, carrier.get());
    const p = quote.pricing, s = quote.shipping;
    const opt = (s.options || []).find(o => o.carrier_code === p.carrier);
    $("#co-sum").innerHTML = `
      <div class="h2" style="font-size:15px;margin-bottom:10px">Order summary</div>
      ${Object.values(store.cart).map(c => `
        <div class="row"><span style="flex:1;min-width:0">
          ${c.qty}× ${esc(c.item.name)}</span></div>`).join("")}
      <div class="row" style="margin-top:10px"><span>Items</span><span>${usd(p.goods_usd)}</span></div>
      <div class="row"><span>Sourcing</span><span>${usd(p.sourcing_usd)}</span></div>
      <div class="row"><span>Shipping</span><span>${usd(p.freight_usd)}</span></div>
      <div class="row"><span>Duty &amp; customs</span><span>${usd((p.duty_usd||0)+(p.mpf_usd||0))}</span></div>
      <div class="row tot"><span>Total</span><span>${usd(p.list_price_usd)}</span></div>
      ${opt ? `<div class="note info" style="margin-top:12px">
        ${esc(opt.carrier_name)} · arrives in ${opt.transit_days[0]}–${opt.transit_days[1]} days
      </div>` : ""}`;
  } catch {
    $("#co-sum").innerHTML = `<div class="note bad">Couldn't price this order.</div>`;
  }

  $("#co-form").onsubmit = (e) => e.preventDefault();
  $("#co-place").onclick = async () => {
    const form = $("#co-form");
    const btn = $("#co-place");
    const err = $("#co-err");
    err.innerHTML = "";

    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    const fd = Object.fromEntries(new FormData(form).entries());
    fd.state = (fd.state || "").toUpperCase();

    btn.disabled = true;
    btn.innerHTML = "Checking Delhi stock…";
    try {
      const res = await api.order({ ...fd, lines: store.lines, carrier: carrier.get() });
      store.clear();
      nav(`#/order/${res.order_id}`);
    } catch (e) {
      // 409 is the live stock gate refusing. Name the item; never fail vaguely.
      const gate = e.body?.gate;
      if (e.status === 409 && gate) {
        const msgs = (gate.blocked || []).map(b => {
          const nm = store.cart[b.product_id]?.item?.name || b.product_id;
          return `<div style="margin-top:6px"><b>${esc(nm)}</b> — ${esc(b.message)}</div>`;
        }).join("");
        // "We'll ship the rest" is false when every line is blocked, which is
        // exactly what happens when the live stock check cannot reach Delhi.
        const blockedAll = (gate.blocked || []).length >= store.lines.length;
        const advice = blockedAll
          ? "Nothing was charged. Try again in a few minutes — we re-check Delhi stock continuously."
          : "Remove that item and we'll ship the rest of your order.";
        err.innerHTML = `<div class="note bad" style="margin-top:14px">
          <b>We didn't place this order.</b>${msgs}
          <div style="margin-top:8px">${esc(advice)}</div></div>`;
      } else {
        err.innerHTML = `<div class="note bad" style="margin-top:14px">
          ${esc(e.message || "Something went wrong.")}</div>`;
      }
      btn.disabled = false;
      btn.innerHTML = `${icon("check")} Place order`;
    }
  };
}
