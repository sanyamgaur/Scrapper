import { api } from "../api.js";
import { $, esc, usd, icon, empty, thumb } from "../ui.js";

export function render() {
  return `<div class="wrap" style="max-width:760px" id="ord">
    <div class="skel" style="height:300px"></div></div>`;
}

export async function mount(params, nav) {
  let o;
  try { o = await api.orderStatus(params.id); }
  catch { $("#ord").innerHTML = empty("Order not found", "Check the link and try again."); return; }

  const stages = o.stage_labels || [];
  const short = o.status === "PROCUREMENT_SHORT";

  $("#ord").innerHTML = `
    <div style="text-align:center;padding:10px 0 28px">
      <div style="width:56px;height:56px;border-radius:var(--r-full);background:var(--ok-soft);
                  color:var(--ok);display:flex;align-items:center;justify-content:center;
                  margin:0 auto 14px">${icon("check")}</div>
      <h1 class="h1">Order confirmed</h1>
      <p class="sub">${esc(o.order_id)} · ${usd(o.total_usd)}</p>
    </div>

    <div class="panel">
      <div class="h2" style="font-size:15px;margin-bottom:16px">Where it is</div>
      ${stages.map((label, i) => {
        const done = i < o.stage, now = i === o.stage - 1;
        return `<div style="display:flex;gap:12px;align-items:flex-start;padding-bottom:${i < stages.length-1 ? "18px" : "0"};
                    position:relative">
          ${i < stages.length - 1 ? `<div style="position:absolute;left:11px;top:24px;bottom:0;
              width:2px;background:${done ? "var(--ok)" : "var(--line)"}"></div>` : ""}
          <div style="width:24px;height:24px;border-radius:var(--r-full);flex:none;z-index:1;
                      display:flex;align-items:center;justify-content:center;font-size:11px;
                      font-weight:700;
                      background:${done ? "var(--ok)" : "var(--surface-2)"};
                      color:${done ? "#fff" : "var(--ink-3)"};
                      border:1px solid ${done ? "var(--ok)" : "var(--line)"}">
            ${done ? "✓" : i + 1}</div>
          <div>
            <div style="font-weight:${now ? "700" : "500"};font-size:14px;
                        color:${done ? "var(--ink)" : "var(--ink-3)"}">${esc(label)}</div>
            ${now ? `<div class="muted">Happening now</div>` : ""}
          </div>
        </div>`;
      }).join("")}
    </div>

    ${short ? `<div class="note warn" style="margin-top:14px">
      One or more items sold out in Delhi before we could buy them. We'll refund those
      lines and ship the rest.</div>` : ""}

    <div class="panel" style="margin-top:14px">
      <div class="h2" style="font-size:15px;margin-bottom:12px">What you ordered</div>
      ${(o.lines || []).map(l => `
        <div style="display:flex;gap:12px;align-items:center;padding:9px 0;
                    border-bottom:1px solid var(--line)">
          ${thumb(l, { size: "44px" })}
          <div style="flex:1;min-width:0">
            <div style="font-size:13.5px;font-weight:600">${esc(l.name)}</div>
            <div class="muted">${esc(l.unit || "")} · ${l.qty} ordered</div>
          </div>
          ${o.stage >= 2 ? `<span class="tag ${l.qty_filled >= l.qty ? "t-ok" : "t-warn"}">
            ${l.qty_filled >= l.qty ? "Bought" : `${l.qty_filled}/${l.qty}`}</span>` : ""}
        </div>`).join("")}
    </div>

    <div style="text-align:center;margin-top:24px">
      <button class="btn" id="ord-shop">Keep shopping</button>
    </div>`;

  $("#ord-shop").onclick = () => nav("#/c");
}
