"""PRANA PCS production stability/latency patch.

Python loads sitecustomize before uvicorn imports main. This lets us keep the
large evaluator code intact while applying a small, auditable runtime patch.
"""

def _patch():
    try:
        import main
    except Exception as exc:
        print("PRANA PATCH IMPORT ERROR:", exc)
        return

    main.MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    def fast_image_pages_from_pdf(pdf):
        pages = []
        for page in pdf:
            pix = page.get_pixmap(dpi=96, alpha=False)
            pages.append(pix.tobytes("jpeg", jpg_quality=84))
        return pages

    main.image_pages_from_pdf = fast_image_pages_from_pdf

    import base64
    import json
    import requests

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
    print("PRANA PRODUCTION PATCH ACTIVE: fast vision raster + low-thinking Gemini + stable Flash fallback")

_patch()

# ---------------------------------------------------------------------------
# Mini App auth stability patch
# ---------------------------------------------------------------------------
# evaluation_access() closes its SQLAlchemy session before returning. The
# legacy /api/app/auth handler then reads trial fields from the returned ORM
# object, which can raise DetachedInstanceError. Wrap only that function so
# the existing 6k-line evaluator remains untouched: return a detached-safe
# plain object containing the four trial counters.
def _patch_detached_user():
    try:
        import main
        from types import SimpleNamespace
        original = main.evaluation_access

        def safe_evaluation_access(uid, question_count=0, consume=False):
            allowed, source, _row = original(uid, question_count=question_count, consume=consume)
            copies_used = copies_limit = questions_used = questions_limit = 0
            if getattr(main, "DB_ENABLED", False) and getattr(main, "SessionLocal", None) is not None:
                session = main.SessionLocal()
                try:
                    current = session.get(main.DBUser, str(uid))
                    if current is not None:
                        copies_used = int(current.trial_copies_used or 0)
                        copies_limit = int(current.trial_copies_limit or 3)
                        questions_used = int(current.trial_questions_used or 0)
                        questions_limit = int(current.trial_questions_limit or 10)
                finally:
                    session.close()
            safe_row = SimpleNamespace(
                trial_copies_used=copies_used,
                trial_copies_limit=copies_limit,
                trial_questions_used=questions_used,
                trial_questions_limit=questions_limit,
            )
            return allowed, source, safe_row

        main.evaluation_access = safe_evaluation_access
        print("PRANA MINI APP AUTH PATCH ACTIVE: detached ORM user protected")
    except Exception as exc:
        print("PRANA MINI APP AUTH PATCH ERROR:", repr(exc))

_patch_detached_user()
