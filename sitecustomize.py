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

    # Fast, valid Flash models only. Avoid dead/slow fallback names.
    main.MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    # Lower rasterization cost for multi-page copies while retaining sufficient
    # resolution for handwriting/vision evaluation.
    def fast_image_pages_from_pdf(pdf):
        pages = []
        for page in pdf:
            pix = page.get_pixmap(dpi=96, alpha=False)
            pages.append(pix.tobytes("jpeg", jpg_quality=84))
        return pages

    main.image_pages_from_pdf = fast_image_pages_from_pdf

    # Lean Gemini transport: low thinking + bounded connect/read timeout + only
    # two Flash models. This replaces the original 90s x 4 sequential fallback.
    import base64
    import json
    import requests

    def fast_call_gemini(images, paper, evaluation_type="GENERAL", source_id=None, exam="UPPCS"):
        parts = [
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image).decode(),
                }
            }
            for image in images
        ]

        if str(evaluation_type).upper() == "DAILY":
            reference = main.get_daily_model_answer_reference(
                paper, source_id=source_id, exam=exam
            )
        else:
            reference = main.get_content_reference(
                evaluation_type,
                source_id=source_id,
                paper=paper,
                exam=exam,
            )

        parts.append(
            {
                "text": main.build_prompt(
                    paper,
                    len(images),
                    reference,
                    evaluation_type=evaluation_type,
                    exam=exam,
                )
            }
        )

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
            url = (
                "https://generativelanguage.googleapis.com/"
                f"v1beta/models/{model}:generateContent"
                f"?key={main.GEMINI_API_KEY}"
            )
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=(15, 75),
                )

                if response.status_code == 200:
                    body = response.json()
                    raw = body["candidates"][0]["content"]["parts"][0]["text"]
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = json.loads(
                            raw.replace("```json", "")
                            .replace("```", "")
                            .strip()
                        )
                    print("GEMINI SUCCESS:", model)
                    return main.normalize_result(data, len(images))

                last_error = (
                    f"{model}: HTTP {response.status_code} "
                    f"{response.text[:300]}"
                )
                print("GEMINI MODEL FAILED:", last_error)

                if response.status_code not in (
                    400, 404, 429, 500, 502, 503, 504
                ):
                    break

            except Exception as exc:
                last_error = f"{model}: {exc}"
                print("GEMINI REQUEST ERROR:", last_error)

        raise Exception("Gemini evaluation failed: " + last_error)

    main.call_gemini = fast_call_gemini

    # A little headroom above the bounded Gemini call prevents false stale-state
    # failures when a large copy needs PDF rendering + DB persistence.
    main.EVALUATION_STALE_SECONDS = 10 * 60

    print(
        "PRANA PRODUCTION PATCH ACTIVE: "
        "fast vision raster + low-thinking Gemini + stable Flash fallback"
    )


_patch()
