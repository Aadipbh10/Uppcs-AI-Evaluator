import os
import io
import json
import base64
import requests

from fastapi import FastAPI, Request
import telebot

from PIL import Image, ImageDraw, ImageFont
import pymupdf as fitz


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://uppcs-ai-evaluator.onrender.com"
).rstrip("/")


# ============================================================
# APP / BOT
# ============================================================

app = FastAPI()

bot = (
    telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
    if BOT_TOKEN
    else None
)


# ============================================================
# HINDI FONT
# ============================================================

HINDI_FONT_PATH = "/tmp/NotoSansDevanagari-Regular.ttf"

HINDI_FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/"
    "ofl/notosansdevanagari/"
    "NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf"
)


def download_hindi_font():
    """
    Noto Sans Devanagari font को Render server पर उपलब्ध कराता है.
    """

    try:
        if os.path.exists(HINDI_FONT_PATH):
            # Font खाली/corrupt तो नहीं है
            if os.path.getsize(HINDI_FONT_PATH) > 10000:
                print("Hindi font already available.")
                return True

        print("Downloading Hindi font...")

        response = requests.get(
            HINDI_FONT_URL,
            timeout=20
        )

        response.raise_for_status()

        with open(HINDI_FONT_PATH, "wb") as font_file:
            font_file.write(response.content)

        if os.path.getsize(HINDI_FONT_PATH) < 10000:
            raise Exception("Downloaded font appears invalid.")

        print("Hindi font downloaded successfully.")
        return True

    except Exception as e:
        print("Hindi font download error:", e)
        return False


FONT_READY = download_hindi_font()


def get_hindi_font(size):
    """
    Hindi font को safely load करता है.
    """

    try:
        if os.path.exists(HINDI_FONT_PATH):
            return ImageFont.truetype(
                HINDI_FONT_PATH,
                size=size
            )
    except Exception as e:
        print("Hindi font load error:", e)

    # अंतिम fallback
    return ImageFont.load_default()


# ============================================================
# STARTUP / WEBHOOK
# ============================================================

@app.on_event("startup")
def setup_webhook():

    download_hindi_font()

    if bot and BOT_TOKEN:

        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"

        try:
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url)

            print(
                f"Telegram webhook set successfully: "
                f"{webhook_url}"
            )

        except Exception as e:
            print("Webhook setup error:", e)


@app.get("/")
def home():

    return {
        "status": "PRANA PCS AI Mains Evaluator Active",
        "hindi_badge_renderer": FONT_READY
    }


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/webhook")
async def telegram_webhook(request: Request):

    if bot:

        json_data = await request.json()

        update = telebot.types.Update.de_json(
            json_data
        )

        bot.process_new_updates([update])

    return {"ok": True}


# ============================================================
# GEMINI MODELS
# ============================================================

ACTIVE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash"
]


# ============================================================
# GEMINI EVALUATION
# ============================================================

def evaluate_with_gemini(images_b64, total_pages):

    parts = []

    # --------------------------------------------------------
    # Answer sheet images
    # --------------------------------------------------------

    for b64 in images_b64:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64
                }
            }
        )

    # --------------------------------------------------------
    # Evaluation Prompt
    # --------------------------------------------------------

    prompt = f"""
आप UPPCS मुख्य परीक्षा के वरिष्ठ एवं अनुभवी परीक्षक हैं।

यह उत्तर पुस्तिका कुल {total_pages} पृष्ठ की है।

आपको प्रत्येक पृष्ठ को ध्यानपूर्वक पढ़कर वास्तविक परीक्षा-जैसा
मूल्यांकन करना है।

महत्वपूर्ण निर्देश:

1. उत्तर में लिखी वास्तविक सामग्री के आधार पर ही अंक दें।
2. अनावश्यक रूप से अधिक अंक न दें।
3. उत्तर की:
   - भूमिका
   - तथ्य
   - विश्लेषण
   - उदाहरण
   - डेटा
   - UP-specific references
   - निष्कर्ष
   - प्रस्तुति
   - डायग्राम/मैप
   का मूल्यांकन करें।
4. प्रत्येक पृष्ठ के लिए 2-3 छोटे margin comments दें।
5. Margin comments बहुत छोटे होने चाहिए ताकि वे answer sheet के
   side margin में लगाए जा सकें।
6. Comments हिंदी में दें।
7. Comments में केवल उपयोगी examiner-style observations दें।

उदाहरण:

✓ भूमिका स्पष्ट
→ UP डेटा जोड़ें
✓ विश्लेषण अच्छा
→ उदाहरण बढ़ाएं
→ निष्कर्ष बेहतर करें
✓ डायग्राम उपयोगी
→ मानचित्र बनाएं

उत्तर केवल valid JSON में दें।

JSON structure:

{{
    "obtained_marks": 16.0,
    "max_marks": 24,
    "feedback": "उत्तर की संरचना अच्छी है लेकिन विश्लेषण में अधिक उदाहरणों की आवश्यकता है।",
    "improvements": [
        "उत्तर प्रदेश के विशेष संदर्भों का प्रयोग करें।",
        "प्रासंगिक आंकड़े और उदाहरण जोड़ें।"
    ],
    "page_annotations": [
        [
            "✓ भूमिका स्पष्ट",
            "→ UP डेटा जोड़ें",
            "✓ विश्लेषण अच्छा"
        ],
        [
            "✓ तथ्य सही",
            "→ उदाहरण जोड़ें"
        ]
    ]
}}

महत्वपूर्ण:
- page_annotations में जितने pages हैं, उनके अनुसार annotations दें।
- हर page के लिए अधिकतम 3 annotations दें।
- Annotation बहुत छोटे रखें।
- JSON के बाहर कोई text न दें।
"""

    parts.append(
        {
            "text": prompt
        }
    )

    payload = {
        "contents": [
            {
                "parts": parts
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json"
        }
    }

    last_error = ""

    # --------------------------------------------------------
    # Model fallback
    # --------------------------------------------------------

    for model_name in ACTIVE_MODELS:

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model_name}:generateContent"
            f"?key={GEMINI_API_KEY}"
        )

        try:

            response = requests.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json"
                },
                timeout=90
            )

            print(
                f"Gemini {model_name}: "
                f"HTTP {response.status_code}"
            )

            # Success
            if response.status_code == 200:

                response_json = response.json()

                candidates = response_json.get(
                    "candidates",
                    []
                )

                if not candidates:
                    raise Exception(
                        "Gemini returned no candidates."
                    )

                candidate = candidates[0]

                content = candidate.get(
                    "content",
                    {}
                )

                response_parts = content.get(
                    "parts",
                    []
                )

                if not response_parts:
                    raise Exception(
                        "Gemini returned empty content."
                    )

                raw_text = response_parts[0].get(
                    "text",
                    ""
                )

                if not raw_text:
                    raise Exception(
                        "Gemini returned empty text."
                    )

                clean_text = (
                    raw_text
                    .replace("```json", "")
                    .replace("```", "")
                    .strip()
                )

                result = json.loads(clean_text)

                return normalize_evaluation(
                    result,
                    total_pages
                )

            # Rate limit / temporary unavailable
            elif response.status_code in [429, 500, 502, 503, 504]:

                last_error = (
                    f"{model_name}: "
                    f"temporary HTTP {response.status_code}"
                )

                continue

            else:

                last_error = (
                    f"{model_name}: "
                    f"{response.text[:300]}"
                )

        except Exception as e:

            last_error = (
                f"{model_name}: {str(e)}"
            )

            print(last_error)

            continue

    raise Exception(
        f"AI मूल्यांकन में समस्या: {last_error}"
    )


# ============================================================
# NORMALIZE AI OUTPUT
# ============================================================

def normalize_evaluation(data, total_pages):

    try:
        obtained = float(
            data.get("obtained_marks", 0)
        )
    except Exception:
        obtained = 0.0

    try:
        max_marks = float(
            data.get("max_marks", 0)
        )
    except Exception:
        max_marks = 0.0

    feedback = str(
        data.get(
            "feedback",
            "मूल्यांकन संपन्न हुआ।"
        )
    )

    improvements = data.get(
        "improvements",
        []
    )

    if not isinstance(improvements, list):
        improvements = []

    page_annotations = data.get(
        "page_annotations",
        []
    )

    if not isinstance(page_annotations, list):
        page_annotations = []

    # हर page के लिए list ensure करना
    normalized_pages = []

    for index in range(total_pages):

        if index < len(page_annotations):
            notes = page_annotations[index]

            if isinstance(notes, list):
                notes = [
                    str(x).strip()
                    for x in notes
                    if str(x).strip()
                ]
            else:
                notes = []

        else:
            notes = []

        normalized_pages.append(
            notes[:3]
        )

    return {
        "obtained_marks": obtained,
        "max_marks": max_marks,
        "feedback": feedback,
        "improvements": [
            str(x)
            for x in improvements
        ],
        "page_annotations": normalized_pages
    }


# ============================================================
# TEXT WRAPPING FOR HINDI
# ============================================================

def wrap_text(draw, text, font, max_width):

    words = str(text).split()

    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:

        test_line = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            test_line,
            font=font
        )

        width = bbox[2] - bbox[0]

        if width <= max_width:

            current = test_line

        else:

            if current:
                lines.append(current)

            current = word

    if current:
        lines.append(current)

    return lines


# ============================================================
# HINDI IMAGE BADGE
# ============================================================

def create_hindi_badge_image(
    text,
    width=520,
    min_height=105
):
    """
    Hindi text को Pillow image में render करता है।

    PDF में Hindi Unicode text insert नहीं किया जाता।
    इसलिए PyMuPDF के '?' glyph problem से बचते हैं।
    """

    text = str(text).strip()

    if not text:
        text = "टिप्पणी"

    font_size = 34

    font = get_hindi_font(font_size)

    # Temporary canvas
    temp_img = Image.new(
        "RGB",
        (width, 500),
        color=(255, 247, 247)
    )

    draw = ImageDraw.Draw(temp_img)

    padding_x = 24
    padding_y = 18

    max_text_width = (
        width - 2 * padding_x
    )

    lines = wrap_text(
        draw,
        text,
        font,
        max_text_width
    )

    # Line height
    line_heights = []

    for line in lines:

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        line_heights.append(
            bbox[3] - bbox[1]
        )

    line_spacing = 8

    text_height = (
        sum(line_heights)
        + line_spacing * max(0, len(lines) - 1)
    )

    height = max(
        min_height,
        text_height + 2 * padding_y
    )

    img = Image.new(
        "RGB",
        (width, height),
        color=(255, 247, 247)
    )

    draw = ImageDraw.Draw(img)

    # --------------------------------------------------------
    # Red border
    # --------------------------------------------------------

    draw.rounded_rectangle(
        [
            2,
            2,
            width - 3,
            height - 3
        ],
        radius=12,
        outline=(190, 0, 0),
        width=4
    )

    # --------------------------------------------------------
    # Text
    # --------------------------------------------------------

    y = padding_y

    for line, line_height in zip(
        lines,
        line_heights
    ):

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        text_width = (
            bbox[2] - bbox[0]
        )

        x = max(
            padding_x,
            (width - text_width) // 2
        )

        draw.text(
            (x, y),
            line,
            font=font,
            fill=(175, 0, 0)
        )

        y += (
            line_height
            + line_spacing
        )

    output = io.BytesIO()

    img.save(
        output,
        format="PNG"
    )

    return output.getvalue()


# ============================================================
# GENERIC BADGE
# ============================================================

def create_badge_image(
    text,
    width=520,
    height=100,
    font_size=28
):

    font = get_hindi_font(font_size)

    img = Image.new(
        "RGB",
        (width, height),
        color=(255, 247, 247)
    )

    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        [
            2,
            2,
            width - 3,
            height - 3
        ],
        radius=12,
        outline=(190, 0, 0),
        width=4
    )

    text = str(text)

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = max(
        10,
        (width - text_width) // 2
    )

    y = max(
        10,
        (height - text_height) // 2
    )

    draw.text(
        (x, y),
        text,
        font=font,
        fill=(175, 0, 0)
    )

    output = io.BytesIO()

    img.save(
        output,
        format="PNG"
    )

    return output.getvalue()


# ============================================================
# CREATE ANNOTATED PDF
# ============================================================

def create_rich_annotated_pdf(
    pdf_doc,
    eval_data
):

    obtained = eval_data.get(
        "obtained_marks",
        0
    )

    max_marks = eval_data.get(
        "max_marks",
        0
    )

    page_notes = eval_data.get(
        "page_annotations",
        []
    )

    total_pages = len(pdf_doc)

    for page_index in range(total_pages):

        page = pdf_doc[page_index]

        page_width = page.rect.width
        page_height = page.rect.height

        # ====================================================
        # RIGHT MARGIN AREA
        # ====================================================

        margin_width = min(
            155,
            page_width * 0.20
        )

        x_right = page_width - 8
        x_left = x_right - margin_width

        # ====================================================
        # PAGE 1 - SCORE BADGE
        # ====================================================

        if page_index == 0:

            score_text = (
                f"Marks: {obtained:g} / {max_marks:g}"
            )

            score_png = create_badge_image(
                score_text,
                width=650,
                height=120,
                font_size=32
            )

            score_rect = fitz.Rect(
                max(5, page_width - 175),
                15,
                page_width - 5,
                55
            )

            page.insert_image(
                score_rect,
                stream=score_png,
                keep_proportion=True
            )

        # ====================================================
        # PAGE ANNOTATIONS
        # ====================================================

        if page_index < len(page_notes):
            notes = page_notes[page_index]
        else:
            notes = []

        # Fallback annotations
        if not notes:

            notes = [
                "✓ विश्लेषण देखें",
                "→ प्रस्तुति सुधारें"
            ]

        notes = notes[:3]

        y_position = 90

        for note in notes:

            badge_png = create_hindi_badge_image(
                note,
                width=700,
                min_height=130
            )

            # Badge aspect ratio
            badge_image = Image.open(
                io.BytesIO(badge_png)
            )

            badge_width, badge_height = (
                badge_image.size
            )

            # Margin badge height proportional
            target_width = (
                margin_width * 0.95
            )

            scale = (
                target_width / badge_width
            )

            target_height = (
                badge_height * scale
            )

            # Prevent overflow
            if (
                y_position + target_height
                > page_height - 60
            ):
                break

            target_rect = fitz.Rect(
                x_left,
                y_position,
                x_right,
                y_position + target_height
            )

            page.insert_image(
                target_rect,
                stream=badge_png,
                keep_proportion=True
            )

            y_position += (
                target_height + 8
            )

        # ====================================================
        # SCORE CIRCLE
        # ====================================================

        # Circle itself is vector, no Hindi involved.
        circle_center = fitz.Point(
            page_width - 40,
            page_height - 40
        )

        page.draw_circle(
            circle_center,
            20,
            color=(0.75, 0, 0),
            width=1.5
        )

        # Score text as IMAGE
        page_score_png = create_badge_image(
            f"{obtained:g}",
            width=180,
            height=100,
            font_size=26
        )

        score_rect = fitz.Rect(
            page_width - 55,
            page_height - 53,
            page_width - 25,
            page_height - 25
        )

        page.insert_image(
            score_rect,
            stream=page_score_png,
            keep_proportion=True
        )

    # ========================================================
    # SAVE
    # ========================================================

    output = io.BytesIO()

    pdf_doc.save(
        output,
        garbage=4,
        deflate=True
    )

    pdf_doc.close()

    output.seek(0)

    return output


# ============================================================
# TELEGRAM START
# ============================================================

if bot:

    @bot.message_handler(
        commands=["start", "help"]
    )
    def send_welcome(message):

        bot.reply_to(
            message,
            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "नमस्ते! अपनी उत्तर पुस्तिका की "
            "<b>PDF फ़ाइल</b> या <b>फ़ोटो</b> भेजें।"
        )


# ============================================================
# TELEGRAM ANSWER SHEET HANDLER
# ============================================================

if bot:

    @bot.message_handler(
        content_types=[
            "document",
            "photo"
        ]
    )
    def handle_answer_sheet(message):

        chat_id = message.chat.id

        status_msg = bot.reply_to(
            message,
            "⏳ <b>कॉपी प्राप्त हो गई है।</b>\n\n"
            "PRANA PCS AI द्वारा पृष्ठ-वार मूल्यांकन चल रहा है..."
        )

        pdf_doc = None

        try:

            # =================================================
            # NEW PDF
            # =================================================

            pdf_doc = fitz.open()

            images_b64 = []

            # =================================================
            # TELEGRAM DOCUMENT
            # =================================================

            if message.content_type == "document":

                file_info = bot.get_file(
                    message.document.file_id
                )

                downloaded_file = bot.download_file(
                    file_info.file_path
                )

                filename = (
                    message.document.file_name
                    or ""
                ).lower()

                # ---------------------------------------------
                # PDF
                # ---------------------------------------------

                if filename.endswith(".pdf"):

                    pdf_doc.close()

                    pdf_doc = fitz.open(
                        stream=downloaded_file,
                        filetype="pdf"
                    )

                    for page in pdf_doc:

                        # 100 DPI is safer for OCR/readability
                        pix = page.get_pixmap(
                            dpi=100,
                            alpha=False
                        )

                        jpeg_bytes = pix.tobytes(
                            "jpeg",
                            jpg_quality=82
                        )

                        images_b64.append(
                            base64.b64encode(
                                jpeg_bytes
                            ).decode("utf-8")
                        )

                # ---------------------------------------------
                # IMAGE DOCUMENT
                # ---------------------------------------------

                else:

                    img = Image.open(
                        io.BytesIO(downloaded_file)
                    ).convert("RGB")

                    img_stream = io.BytesIO()

                    img.save(
                        img_stream,
                        format="JPEG",
                        quality=82
                    )

                    jpeg_data = (
                        img_stream.getvalue()
                    )

                    images_b64.append(
                        base64.b64encode(
                            jpeg_data
                        ).decode("utf-8")
                    )

                    page = pdf_doc.new_page(
                        width=img.width,
                        height=img.height
                    )

                    page.insert_image(
                        page.rect,
                        stream=jpeg_data
                    )

            # =================================================
            # TELEGRAM PHOTO
            # =================================================

            elif message.content_type == "photo":

                file_info = bot.get_file(
                    message.photo[-1].file_id
                )

                downloaded_file = bot.download_file(
                    file_info.file_path
                )

                img = Image.open(
                    io.BytesIO(downloaded_file)
                ).convert("RGB")

                img_stream = io.BytesIO()

                img.save(
                    img_stream,
                    format="JPEG",
                    quality=82
                )

                jpeg_data = (
                    img_stream.getvalue()
                )

                images_b64.append(
                    base64.b64encode(
                        jpeg_data
                    ).decode("utf-8")
                )

                page = pdf_doc.new_page(
                    width=img.width,
                    height=img.height
                )

                page.insert_image(
                    page.rect,
                    stream=jpeg_data
                )

            else:

                raise Exception(
                    "Unsupported Telegram file type."
                )

            # =================================================
            # CHECK PAGE COUNT
            # =================================================

            total_pages = len(pdf_doc)

            if total_pages == 0:
                raise Exception(
                    "PDF में कोई page नहीं मिला।"
                )

            # =================================================
            # GEMINI
            # =================================================

            eval_result = evaluate_with_gemini(
                images_b64,
                total_pages
            )

            obtained = eval_result.get(
                "obtained_marks",
                0
            )

            max_marks = eval_result.get(
                "max_marks",
                0
            )

            feedback = eval_result.get(
                "feedback",
                "मूल्यांकन संपन्न हुआ।"
            )

            improvements = eval_result.get(
                "improvements",
                []
            )

            # =================================================
            # TELEGRAM RESULT CAPTION
            # =================================================

            if improvements:

                imp_text = "\n".join(
                    [
                        f"• {item}"
                        for item in improvements[:6]
                    ]
                )

            else:

                imp_text = (
                    "• कोई अतिरिक्त सुझाव उपलब्ध नहीं।"
                )

            result_caption = (
                "🏛️ <b>PRANA PCS - मूल्यांकन रिपोर्ट</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>कुल प्राप्तांक:</b> "
                f"<code>{obtained:g} / {max_marks:g}</code>\n\n"
                f"📝 <b>समीक्षा:</b> "
                f"{feedback}\n\n"
                f"💡 <b>सुधार सुझाव:</b>\n"
                f"{imp_text}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>जाँची हुई कॉपी नीचे संलग्न है 👇</i>"
            )

            # =================================================
            # ANNOTATED PDF
            # =================================================

            stamped_pdf = create_rich_annotated_pdf(
                pdf_doc,
                eval_result
            )

            pdf_doc = None

            # =================================================
            # REMOVE STATUS
            # =================================================

            try:

                bot.delete_message(
                    chat_id,
                    status_msg.message_id
                )

            except Exception:
                pass

            # =================================================
            # SEND PDF
            # =================================================

            bot.send_document(
                chat_id=chat_id,
                document=stamped_pdf,
                visible_file_name=(
                    "Evaluated_Copy_PranaPCS.pdf"
                ),
                caption=result_caption
            )

        # =====================================================
        # ERROR
        # =====================================================

        except Exception as e:

            print(
                "Answer evaluation error:",
                repr(e)
            )

            if pdf_doc is not None:

                try:
                    pdf_doc.close()
                except Exception:
                    pass

            error_message = str(e)

            if len(error_message) > 250:
                error_message = (
                    error_message[:250]
                    + "..."
                )

            try:

                bot.edit_message_text(
                    (
                        "⚠️ <b>मूल्यांकन में समस्या</b>\n\n"
                        f"{error_message}\n\n"
                        "कृपया स्पष्ट PDF या फोटो पुनः भेजें।"
                    ),
                    chat_id=chat_id,
                    message_id=status_msg.message_id
                )

            except Exception:

                bot.send_message(
                    chat_id,
                    (
                        "⚠️ मूल्यांकन में समस्या हुई।\n"
                        "कृपया PDF/फोटो पुनः भेजें।"
                    )
                )
