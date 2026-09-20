"""Run the BACKEND only: the API + all engines, on port 8000.

    python run_backend.py                # http://127.0.0.1:8000  (API + docs)

CORS is enabled, so the separately-served website (run serve_frontend.py) can
call this from another port or machine. This process still serves the UI at the
same origin too, so if you ever want one-and-done, this alone is enough and you
can open http://127.0.0.1:8000/console directly.
"""
import argparse
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from crossborder.api import app          # storefront + ops API
import crossborder.console               # noqa: F401  -> /console + pipeline/demo routes

# Let the standalone website (another origin/port) call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    print(f"\n  Backend API : http://{a.host}:{a.port}")
    print(f"  API docs    : http://{a.host}:{a.port}/docs")
    print(f"  (this also serves the UI at http://{a.host}:{a.port}/console)\n")
    uvicorn.run(app, host=a.host, port=a.port)
