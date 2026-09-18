"""Start the whole system on one port, with the control tower attached.

    python run_console.py            # then open http://127.0.0.1:8000/console

This is the only thing that needs to run. Importing crossborder.console attaches
the pipeline/demo endpoints and the /console page onto the existing app; nothing
in the original project is modified.
"""

import argparse
import webbrowser
import threading
import time

import uvicorn

from crossborder.api import app          # the real storefront + ops API
import crossborder.console               # noqa: F401  (attaches /console + routes)


def _open_browser(url: str) -> None:
    time.sleep(1.5)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    a = ap.parse_args()

    url = f"http://{a.host}:{a.port}/console"
    print("\n  Control tower :", url)
    print("  Storefront    :", f"http://{a.host}:{a.port}/")
    print("  Ops console   :", f"http://{a.host}:{a.port}/ops")
    print("  API docs      :", f"http://{a.host}:{a.port}/docs\n")
    if not a.no_open:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
