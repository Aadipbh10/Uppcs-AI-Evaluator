"""Production entrypoint for PRANA PCS evaluator."""
import base64
import builtins
import hashlib
import hmac
import html
import json
import os
import time
import threading
from urllib.parse import parse_qs

import requests
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import desc

# main.py is a large legacy module whose later sections use these names.
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

# Telegram Mini App public key for the Ed25519 signature validation path.
_PROD_TELEGRAM_WEBAPP_PUBLIC_KEY = bytes.fromhex(
    "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
)


def _check_string(values, exclude_signature=False):
    """Build Telegram's sorted data-check-string from parsed initData."""
    keys = []
    for key in values:
        if key == "hash" or (exclude_signature and key == "signature"):
            continue
        keys.append(key)
    return "\n".join(f"{key}={values[key][0]}" for key in sorted(keys))


def robust_telegram_webapp_validate(init_data: str):
    """Strictly validate Telegram Mini App initData.

    HMAC validation is primary. When Telegram supplies the newer Ed25519
    signature, it is verified using Telegram's published public key. The two
    schemes intentionally use different exclusion rules:
      - HMAC: exclude only `hash`.
      - Ed25519: exclude both `hash` and `signature`.
    """
    if not main.BOT_TOKEN or not init_data:
        print("MINI APP AUTH FAIL: missing bot token or initData")
        return None
    try:
        values = parse_qs(str(init_data), keep_blank_values=True)
        received_hash = values.get("hash", [""])[0]
        received_signature = values.get("signature", [""])[0]
        auth_date = int(values.get("auth_date", ["0"])[0])
        if not auth_date:
            print("MINI APP AUTH FAIL: auth_date missing")
            return None
        age = int(time.time()) - auth_date
        if age < -300 or age > 86400:
            print(f"MINI APP AUTH FAIL: auth_date invalid age={age}s")
            return None

        user_raw = values.get("user", [""])[0]
        if not user_raw:
            print("MINI APP AUTH FAIL: user missing")
            return None

        # Telegram HMAC: all received fields except `hash` participate.
        hmac_check = _check_string(values, exclude_signature=False)
        secret_key = hmac.new(
            b"WebAppData", main.BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, hmac_check.encode(), hashlib.sha256
        ).hexdigest()
        valid = bool(received_hash) and hmac.compare_digest(calculated, received_hash)

        # Telegram Ed25519: exclude both hash and signature.
        if not valid and received_signature:
            try:
                bot_id = main.BOT_TOKEN.split(":", 1)[0]
                signature_check = f"{bot_id}:WebAppData\n" + _check_string(values, exclude_signature=True)
                signature_bytes = base64.urlsafe_b64decode(
                    received_signature + "=" * (-len(received_signature) % 4)
                )
                Ed25519PublicKey.from_public_bytes(
                    _PROD_TELEGRAM_WEBAPP_PUBLIC_KEY
                ).verify(signature_bytes, signature_check.encode())
                valid = True
                print("MINI APP AUTH: Ed25519 signature accepted")
            except Exception as sig_exc:
                print("MINI APP AUTH: Ed25519 signature rejected:", repr(sig_exc))

        if not valid:
            print(
                "MINI APP AUTH FAIL: hash/signature mismatch "
                f"hash_present={bool(received_hash)} signature_present={bool(received_signature)}"
            )
            return None

        user = json.loads(user_raw)
        uid = str(user.get("id", ""))
        if not uid:
            print("MINI APP AUTH FAIL: user.id missing")
            return None
        return {
            "id": uid,
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "language_code": user.get("language_code"),
        }
    except Exception as exc:
        print("MINI APP AUTH FAIL:", repr(exc))
        return None


main.telegram_webapp_validate = robust_telegram_webapp_validate

# Protect Mini App profile rendering from detached/unavailable DB failures.
_original_app_user_payload = main.app_user_payload

def safe_app_user_payload(uid):
    try:
        return _original_app_user_payload(uid)
    except Exception as exc:
        print("MINI APP USER PAYLOAD ERROR:", repr(exc))
        return {
            "id": str(uid), "name": "Student", "username": None,
            "submissions": 0, "obtained": 0, "max": 0,
            "average_percentage": 0,
        }

main.app_user_payload = safe_app_user_payload

# Make evaluation workers fast and use the same models everywhere.
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

# Cloud Run request-based instances can throttle background work after the
# HTTP response. main.app_evaluate already schedules run_webapp_evaluation as
# a Starlette BackgroundTask; this wrapper immediately hands the heavy work to
# a daemon thread so the HTTP request itself stays short.
_original_run_webapp_evaluation = main.run_webapp_evaluation

def threaded_run_webapp_evaluation(*args, **kwargs):
    t = threading.Thread(
        target=_original_run_webapp_evaluation,
        args=args,
        kwargs=kwargs,
        daemon=True,
        name="prana-evaluation-worker",
    )
    t.start()
    print("MINI APP EVALUATION WORKER STARTED:", args[0] if args else "unknown")

main.run_webapp_evaluation = threaded_run_webapp_evaluation

@main.app.get("/api/health")
def health_check():
    return {
        "ok": True,
        "runtime": "google-cloud-run",
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
