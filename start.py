"""Production entrypoint for PRANA PCS evaluator."""
import base64
import builtins
import hashlib
import hmac
import html
import json
import os
import time
from urllib.parse import parse_qs

import requests
import uvicorn
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import desc

# main.py is a large legacy module whose later sections use these names.
# Expose them before importing it so the production entrypoint is resilient.
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

# Prevent an unavailable/unreachable DB from turning Mini App authentication
# into an opaque HTTP 500. Evaluation remains access-controlled server-side.
_original_app_user_payload = main.app_user_payload

def safe_app_user_payload(uid):
    try:
        return _original_app_user_payload(uid)
    except Exception as exc:
        print("MINI APP USER PAYLOAD ERROR:", repr(exc))
        return {
            "id": str(uid),
            "name": "Student",
            "username": None,
            "submissions": 0,
            "obtained": 0,
            "max": 0,
            "average_percentage": 0,
        }

main.app_user_payload = safe_app_user_payload

main.MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

def fast_image_pages_from_pdf(pdf):
    pages = []
    for page in pdf:
        pix = page.get_pixmap(dpi=96, alpha=False)
        pages.append(pix.tobytes("jpeg", jpg_quality=84))
    return pages

main.image_pages_from_pdf = fast_image_pages_from_pdf

def fast_call_gemini(images, paper, evaluation_type="GENERAL", source_id=None, exam="UPPCS"):
    parts = [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}}
        for image in images
    ]
    if str(evaluation_type).upper() == "DAILY":
        reference = main.get_daily_model_answer_reference(paper, source_id=source_id, exam=exam)
    else:
        reference = main.get_content_reference(evaluation_type, source_id=source_id, paper=paper, exam=exam)
    parts.append({"text": main.build_prompt(paper, len(images), reference, evaluation_type=evaluation_type, exam=exam)})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"},
            "maxOutputTokens": 24000,
        },
    }
    last_error = ""
    for model in main.MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={main.GEMINI_API_KEY}"
        try:
            response = requests.post(url, json=payload, timeout=(15, 75))
            if response.status_code == 200:
                body = response.json()
                raw = body["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    data = json.loads(raw)
                except Exception:
                    data = json.loads(raw.replace("```json", "").replace("```", "").strip())
                print("GEMINI SUCCESS:", model)
                return main.normalize_result(data, len(images))
            last_error = f"{model}: HTTP {response.status_code} {response.text[:300]}"
            print("GEMINI MODEL FAILED:", last_error)
            if response.status_code not in (400, 404, 429, 500, 502, 503, 504):
                break
        except Exception as exc:
            last_error = f"{model}: {exc}"
            print("GEMINI REQUEST ERROR:", last_error)
    raise Exception("Gemini evaluation failed: " + last_error)

main.call_gemini = fast_call_gemini
main.EVALUATION_STALE_SECONDS = 10 * 60

@main.app.get("/api/health")
def health_check():
    return {
        "ok": True,
        "database_configured": bool(main.DB_ENABLED and main.SessionLocal is not None),
        "telegram_bot_configured": bool(main.bot),
        "gemini_configured": bool(main.GEMINI_API_KEY),
        "mini_app": True,
        "models": list(main.MODELS),
    }

print("PRANA PRODUCTION ENTRYPOINT ACTIVE")
print("MODELS:", main.MODELS)

if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
