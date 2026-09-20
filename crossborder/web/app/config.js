/* ------------------------------------------------------------------ config
   Where the website finds the backend API.

   ""  (empty)  -> same origin: the backend is serving this site (run_console.py
                   or run_backend.py open both on one port). Nothing to change.

   To run the SITE SEPARATELY from the backend, set the backend's address here,
   e.g.  "http://127.0.0.1:8000"  (or your deployed backend URL). The frontend
   package ships with that value already filled in.

   You can also override without editing this file, from the browser console:
     localStorage.setItem('apiBase','http://127.0.0.1:8000'); location.reload();
   -------------------------------------------------------------------------- */
(function () {
  var DEFAULT = "";            // <-- EDIT for a standalone site
  var override = null;
  try { override = localStorage.getItem("apiBase"); } catch (e) {}
  window.API_BASE = (override || DEFAULT).replace(/\/+$/, "");
})();
