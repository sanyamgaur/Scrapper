/* Thin fetch layer. Every network call in the app goes through here, so error
   handling and the base path live in exactly one place. */

const BASE = "";

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty or non-JSON */ }
  if (!res.ok) {
    const err = new Error((body && (body.detail || body.message)) || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

const qs = (o) =>
  Object.entries(o)
    .filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== false)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

export const api = {
  catalog: (p = {}) => req(`/api/catalog?${qs(p)}`),
  facets:  () => req("/api/facets"),
  product: (id) => req(`/api/product/${encodeURIComponent(id)}`),
  related: (id) => req(`/api/product/${encodeURIComponent(id)}/related`),
  quote:   (lines, carrier) =>
    req("/api/quote", { method: "POST", body: JSON.stringify({ lines, carrier }) }),
  advise:  (lines, carrier) =>
    req("/api/basket/advise", { method: "POST", body: JSON.stringify({ lines, carrier }) }),
  order:   (payload) =>
    req("/api/order", { method: "POST", body: JSON.stringify(payload) }),
  orderStatus: (id) => req(`/api/order/${encodeURIComponent(id)}`),
};
