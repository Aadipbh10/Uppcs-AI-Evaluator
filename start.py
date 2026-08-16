"""Production entrypoint for PRANA PCS evaluator.

Intentionally thin: expose the small set of names main.py references at import
time, import the app, and serve it on $PORT (Cloud Run injects PORT=8080). All
real behaviour (auth, access, trial, evaluation, Gemini, PDF raster) lives in
main.py. No runtime monkeypatching happens here anymore.
"""
import base64
import builtins
import hashlib
import hmac
import html
import json
import os
import time
from urllib.parse import parse_qs
import uvicorn
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import desc

# main.py imports these itself, but late in the file. Expose them up front so any
# module-level use during import is always satisfied regardless of import order.
builtins.base64 = base64
builtins.hashlib = hashlib
builtins.hmac = hmac
builtins.html = html
builtins.json = json
builtins.os = os
builtins.time = time
builtins.parse_qs = parse_qs
builtins.Response = Response
builtins.HTMLResponse = HTMLResponse
builtins.desc = desc

import main

# NOTE: evaluation runs via FastAPI BackgroundTasks inside main.py. Do NOT
# reintroduce a detached daemon thread here â on Cloud Run the CPU is throttled
# once the HTTP response returns, which would strand the job in "processing".


@main.app.get("/api/health")
def health_check():
    return {
        "ok": True,
        "runtime": "google-cloud-run" if os.getenv("K_SERVICE") else "generic",
        "database_configured": bool(main.DB_ENABLED and main.SessionLocal is not None),
        "telegram_bot_configured": bool(main.bot),
        "gemini_configured": bool(main.GEMINI_API_KEY),
        "mini_app": True,
        "models": list(main.MODELS),
    }


if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
