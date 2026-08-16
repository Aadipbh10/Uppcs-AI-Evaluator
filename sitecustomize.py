"""PRANA PCS production stability/latency patch."""

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
    from pathlib import Path

    original_normalize_result = main.normalize_result

    def normalized_result_with_structure(data, pages):
        result = original_normalize_result(data, pages)
        raw_questions = {
            int(q.get("question_number", i + 1)): q
            for i, q in enumerate(data.get("questions", []))
            if isinstance(q, dict)
        }
        for q in result.get("questions", []):
            raw = raw_questions.get(int(q.get("question_number", 0)), {})
            q["intro_comment"] = str(raw.get("intro_comment", "")).strip()
            q["body_comment"] = str(raw.get("body_comment", "")).strip()
            q["conclusion_comment"] = str(raw.get("conclusion_comment", "")).strip()
        return result

    main.normalize_result = normalized_result_with_structure

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

    def stable_telegram_worker(message, item, paper, status, source="trial"):
        chat_id = int(message.chat.id)
        try:
            final_pdf, result = main.process_submission(item["path"], paper)
        except Exception as exc:
            try:
                main.bot.edit_message_text(
                    f"⚠️ <b>Evaluation Error</b>\n\n{str(exc)[:300]}",
                    chat_id=chat_id, message_id=status.message_id,
                )
            except Exception:
                try: main.bot.send_message(chat_id, f"⚠️ Evaluation error.\n{str(exc)[:300]}")
                except Exception: pass
            try: Path(item["path"]).unlink(missing_ok=True)
            except Exception: pass
            return
        try: Path(item["path"]).unlink(missing_ok=True)
        except Exception: pass
        try: main.bot.delete_message(chat_id=chat_id, message_id=status.message_id)
        except Exception: pass
        original_name = item.get("filename", "submission.pdf")
        evaluated_filename = f"{Path(original_name).stem or 'submission'}_Evaluated.pdf"
        feedback = str(result.get("overall_feedback", "")).strip()
        caption = (f"🏛️ <b>PRANA PCS — {paper} Evaluation</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                   f"🎯 <b>Obtained Marks:</b> <code>{result['total_obtained_marks']:g} / {result['total_max_marks']:g}</code>\n\n"
                   f"📝 <b>Language • Style • Presentation:</b> {feedback}")[:900]
        try:
            main.save_evaluation_to_database(message, item, paper, result, evaluated_filename, final_pdf.getvalue())
        except Exception as exc:
            print("TELEGRAM DB SAVE WARNING:", repr(exc))
        try:
            markup = main.telebot.types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                main.telebot.types.InlineKeyboardButton("Open Mini App", web_app=main.telebot.types.WebAppInfo(url=f"{main.PUBLIC_BASE_URL}/app")),
                main.telebot.types.InlineKeyboardButton("Performance", web_app=main.telebot.types.WebAppInfo(url=f"{main.PUBLIC_BASE_URL}/app?view=performance")),
            )
            markup.add(
                main.telebot.types.InlineKeyboardButton("Evaluation History", web_app=main.telebot.types.WebAppInfo(url=f"{main.PUBLIC_BASE_URL}/app?view=history")),
                main.telebot.types.InlineKeyboardButton("Evaluate Another Copy", web_app=main.telebot.types.WebAppInfo(url=f"{main.PUBLIC_BASE_URL}/app?view=evaluate")),
            )
            main.bot.send_document(chat_id, final_pdf, visible_file_name=evaluated_filename, caption=caption, reply_markup=markup)
        except Exception as exc:
            print("TELEGRAM SEND EVALUATED PDF ERROR:", repr(exc))
            try: main.bot.send_message(chat_id, f"⚠️ Evaluated PDF तैयार हुआ, लेकिन Telegram पर भेजने में समस्या हुई: {str(exc)[:220]}")
            except Exception: pass

    main._run_telegram_evaluation = stable_telegram_worker
    print("PRANA TELEGRAM PATCH ACTIVE: stable chat-id scoped worker")

_patch()

# Mini App access/auth stability patch.
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
                        # Automatically activate Trial for a normal first-time
                        # Mini App user. Admin/full/group access remains untouched.
                        if (not allowed and not current.is_blocked and
                                str(current.access_type or "none") in ("", "none")):
                            current.access_type = "trial"
                            current.is_allowed = False
                            current.trial_copies_limit = int(current.trial_copies_limit or 3)
                            current.trial_questions_limit = int(current.trial_questions_limit or 10)
                            current.trial_copies_used = int(current.trial_copies_used or 0)
                            current.trial_questions_used = int(current.trial_questions_used or 0)
                            session.commit()
                            allowed = True
                            source = "trial"
                            print(f"MINI APP TRIAL AUTO-ACTIVATED: {uid}")
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
        print("PRANA MINI APP AUTH PATCH ACTIVE: detached ORM protected + auto trial")
    except Exception as exc:
        print("PRANA MINI APP AUTH PATCH ERROR:", repr(exc))

_patch_detached_user()
