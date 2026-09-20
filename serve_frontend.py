"""Serve the WEBSITE only, as its own static site on port 3000.

    python serve_frontend.py             # http://127.0.0.1:3000

It talks to the backend over the network — edit web/app/config.js to point at
your backend (ships set to http://127.0.0.1:8000). No Python packages needed;
this uses only the standard library.

Extensionless routes are mapped to the right file so the same links work here
as when the backend serves the pages:
    /            -> app/index.html      (storefront)
    /console     -> console.html
    /ops         -> ops.html
    /operator    -> operator.html
    /legacy      -> storefront.html
Everything else (/app/*.js, /app/config.js, styles, …) is served from web/.
"""
import argparse
import http.server
import functools
import os

ROUTES = {
    "/": "app/index.html",
    "/console": "console.html",
    "/ops": "ops.html",
    "/operator": "operator.html",
    "/legacy": "storefront.html",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean in ROUTES:
            return os.path.join(self.directory, ROUTES[clean])
        return super().translate_path(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--dir", default="web")
    a = ap.parse_args()
    handler = functools.partial(Handler, directory=a.dir)
    print(f"\n  Website     : http://{a.host}:{a.port}/          (storefront)")
    print(f"  Control     : http://{a.host}:{a.port}/console")
    print(f"  Ops queue   : http://{a.host}:{a.port}/ops")
    print(f"  Backend it calls: edit web/app/config.js (now: see that file)\n")
    http.server.ThreadingHTTPServer((a.host, a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
