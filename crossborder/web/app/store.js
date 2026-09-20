/* Cart state.

   Persisted to localStorage so a refresh does not lose a basket the customer
   spent time building — but every read is wrapped, because storage throws in
   private windows and returns nothing when site data is cleared. The app must
   render correctly with an empty cart in either case. */

const KEY = "sourced.cart.v1";
const listeners = new Set();

function read() {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function write(v) {
  try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* non-fatal */ }
}

let cart = read();

function emit() {
  write(cart);
  listeners.forEach((fn) => fn(cart));
}

export const store = {
  get cart() { return cart; },
  get lines() {
    return Object.entries(cart).map(([product_id, c]) => ({ product_id, qty: c.qty }));
  },
  get count() {
    return Object.values(cart).reduce((n, c) => n + c.qty, 0);
  },
  get isEmpty() { return this.count === 0; },

  add(item, qty = 1) {
    const id = String(item.product_id);
    if (!cart[id]) cart[id] = { item, qty: 0 };
    // Refresh the snapshot: price and stock may have moved since it was added.
    cart[id].item = { ...cart[id].item, ...item };
    cart[id].qty += qty;
    emit();
  },
  setQty(id, qty) {
    if (!cart[id]) return;
    if (qty <= 0) delete cart[id];
    else cart[id].qty = Math.min(99, qty);
    emit();
  },
  remove(id) { delete cart[id]; emit(); },
  clear() { cart = {}; emit(); },

  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
};

/* Carrier choice is a deliberate customer decision on this lane (3 days at
   $50 vs 3 weeks at $11), so it is remembered alongside the cart. */
const CKEY = "sourced.carrier.v1";
export const carrier = {
  get() { try { return localStorage.getItem(CKEY) || null; } catch { return null; } },
  set(v) { try { v ? localStorage.setItem(CKEY, v) : localStorage.removeItem(CKEY); } catch {} },
};
