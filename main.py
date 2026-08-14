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
# APP / TELEGRAM BOT
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
    Render server पर Hindi font उपलब्ध कराता है।
    """

    try:
        if os.path.exists(HINDI_FONT_PATH):

            if os.path.getsize(HINDI_FONT_PATH) > 10000:
                print("Hindi font already available.")
                return True

        print("Downloading Hindi font...")

        response = requests.get(
            HINDI_FONT_URL,
            timeout=25
        )

        response.raise_for_status()

        with open(HINDI_FONT_PATH, "wb") as f:
            f.write(response.content)

        if os.path.getsize(HINDI_FONT_PATH) < 10000:
            raise Exception(
                "Downloaded Hindi font is invalid."
            )

        print("Hindi font downloaded successfully.")

        return True

    except Exception as e:

        print(
            "Hindi font download error:",
            repr(e)
        )

        return False


FONT_READY = download_hindi_font()


def get_hindi_font(size):

    try:

        if os.path.exists(HINDI_FONT_PATH):

            return ImageFont.truetype(
                HINDI_FONT_PATH,
                size=size
            )

    except Exception as e:

        print(
            "Hindi font loading error:",
            repr(e)
        )

    return ImageFont.load_default()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def setup_webhook():

    download_hindi_font()

    if bot and BOT_TOKEN:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}/webhook"
        )

        try:

            bot.remove_webhook()

            bot.set_webhook(
                url=webhook_url
            )

            print(
                "Telegram webhook set:",
                webhook_url
            )

        except Exception as e:

            print(
                "Webhook setup error:",
                repr(e)
            )


# ============================================================
# HOME
# ============================================================

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
async def telegram_webhook(
    request: Request
):

    if bot:

        json_data = await request.json()

        update = (
            telebot.types.Update
            .de_json(json_data)
        )

        bot.process_new_updates(
            [update]
        )

    return {
        "ok": True
    }


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

def evaluate_with_gemini(
    images_b64,
    total_pages
):

    parts = []

    # --------------------------------------------------------
    # Answer sheet pages
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
    # MASTER PROMPT
    # --------------------------------------------------------

    prompt = f"""
आप UPPCS मुख्य परीक्षा के वरिष्ठ परीक्षक हैं।

आपके सामने एक अभ्यर्थी की उत्तर पुस्तिका है।
कुल पृष्ठ संख्या: {total_pages}

आपको इस कॉपी का वास्तविक UPPCS Mains examiner की तरह
गंभीर, निष्पक्ष और page-by-page मूल्यांकन करना है।

============================================================
सबसे महत्वपूर्ण MARKING RULES
============================================================

1. उत्तर पुस्तिका में प्रत्येक अलग प्रश्न पहचानें।

2. किसी प्रश्न का उत्तर यदि कुल 2 pages में लिखा गया है,
   तो उस प्रश्न के अधिकतम अंक 8 होंगे।

3. 2-page / 8-mark question में प्राप्तांक की HARD LIMIT:
   अधिकतम 5.5 अंक।

4. किसी प्रश्न का उत्तर यदि कुल 3 pages में लिखा गया है,
   तो उस प्रश्न के अधिकतम अंक 12 होंगे।

5. 3-page / 12-mark question में प्राप्तांक की HARD LIMIT:
   अधिकतम 8.5 अंक।

6. 8 marks वाले प्रश्न में कभी भी 5.5 से अधिक अंक न दें।

7. 12 marks वाले प्रश्न में कभी भी 8.5 से अधिक अंक न दें।

8. प्रश्न के उत्तर का जिस page पर वास्तविक अंत होता है,
   उसी page को उस प्रश्न का end_page मानें।

9. उसी end_page पर examiner marks दिखाई जाएंगे।

10. यदि कोई प्रश्न 2 pages में फैला है:
    start_page और end_page का अंतर उसी अनुसार रखें।

11. यदि कोई प्रश्न 3 pages में फैला है:
    start_page और end_page उसी अनुसार रखें।

============================================================
PAGE COMMENT RULE
============================================================

हर page के लिए examiner-style substantive comment दें।

महत्वपूर्ण:

- केवल 3-5 शब्द के comments बिल्कुल नहीं।
- "भूमिका स्पष्ट"
- "विश्लेषण अच्छा"
- "डेटा जोड़ें"

जैसे बहुत छोटे comments अकेले न दें।

हर page की कुल टिप्पणी लगभग 15 से 40 हिंदी शब्दों की हो।

टिप्पणी में वास्तविक evaluation होना चाहिए।

उदाहरण:

"उत्तर में मूल अवधारणा स्पष्ट रूप से प्रस्तुत की गई है,
लेकिन तर्कों को समकालीन उदाहरणों से जोड़ने तथा उत्तर प्रदेश
के विशेष संदर्भ को शामिल करने से उत्तर अधिक प्रभावी बन सकता था।"

या:

"तथ्यात्मक सामग्री पर्याप्त है और उत्तर का क्रम व्यवस्थित है,
लेकिन कारण एवं परिणाम के बीच संबंध को अधिक स्पष्ट करने की
आवश्यकता है। निष्कर्ष में समाधानपरक दृष्टिकोण जोड़ा जा सकता है।"

============================================================
QUESTION END MARK
============================================================

हर question के लिए:

- question_number
- start_page
- end_page
- pages_used
- max_marks
- obtained_marks
- end_page_comment

देना है।

end_page पर marks दिखेंगे।

उदाहरण:

Q1
5.0 / 8

या

Q2
7.5 / 12

============================================================
FIRST PAGE SCORE
============================================================

पहले page के LEFT SIDE में एक circular examiner box में:

प्राप्तांक
14.5 / 20

जैसा total score दिखाया जाएगा।

============================================================
EXAMINER COMMENTS
============================================================

Comments हिंदी में हों।

Examiner की भाषा में लिखें।

सुझावों में आवश्यकता होने पर:

- UP-specific examples
- current data
- committee/report
- constitutional provision
- diagram
- map
- flowchart
- conclusion
- analytical depth

का उल्लेख करें।

लेकिन comment केवल वही दें जो actual answer को देखकर
उपयोगी हो।

============================================================
OUTPUT
============================================================

केवल valid JSON दें।

JSON structure:

{{
    "total_obtained_marks": 12.5,
    "total_max_marks": 20,

    "questions": [
        {{
            "question_number": 1,
            "start_page": 1,
            "end_page": 2,
            "pages_used": 2,
            "max_marks": 8,
            "obtained_marks": 5.0,
            "end_page_comment":
            "उत्तर की मूल अवधारणा स्पष्ट है और तर्क का क्रम उचित है, लेकिन उत्तर प्रदेश से संबंधित उदाहरण तथा अधिक विश्लेषणात्मक निष्कर्ष जोड़ने से इसकी गुणवत्ता बेहतर हो सकती थी।"
        }},
        {{
            "question_number": 2,
            "start_page": 3,
            "end_page": 5,
            "pages_used": 3,
            "max_marks": 12,
            "obtained_marks": 7.5,
            "end_page_comment":
            "उत्तर में विषय के प्रमुख आयाम शामिल किए गए हैं, लेकिन कारण-परिणाम संबंध को अधिक स्पष्ट करने और निष्कर्ष को समाधानपरक बनाने की आवश्यकता है।"
        }}
    ],

    "page_comments": [
        {{
            "page": 1,
            "comment":
            "भूमिका विषय के अनुरूप है और उत्तर की दिशा प्रारंभ से स्पष्ट दिखाई देती है, हालांकि मुख्य तर्क को अधिक प्रभावी बनाने के लिए प्रासंगिक तथ्य या उदाहरण का संक्षिप्त प्रयोग किया जा सकता था।"
        }},
        {{
            "page": 2,
            "comment":
            "उत्तर में तथ्यात्मक सामग्री अच्छी है, लेकिन विभिन्न बिंदुओं के बीच तार्किक संबंध और अधिक स्पष्ट किया जा सकता था। उत्तर प्रदेश का संदर्भ उत्तर को अधिक परीक्षा-उपयोगी बनाता।"
        }}
    ],

    "overall_feedback":
    "उत्तर की संरचना अच्छी है लेकिन विश्लेषणात्मक गहराई और उदाहरणों के प्रयोग में सुधार की आवश्यकता है।",

    "improvements": [
        "उत्तर प्रदेश के विशिष्ट उदाहरणों और समकालीन आंकड़ों का प्रयोग बढ़ाएं।",
        "निष्कर्ष को अधिक समाधानपरक और भविष्य की दिशा बताने वाला बनाएं।"
    ]
}}

============================================================
अत्यंत महत्वपूर्ण
============================================================

JSON के बाहर कोई explanation या markdown न दें।

गलत page numbering न करें।

Question का end_page वही रखें जहाँ उसका उत्तर वास्तव में समाप्त होता है।
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
    # MODEL FALLBACK
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
                    "Content-Type":
                    "application/json"
                },
                timeout=120
            )

            print(
                f"Gemini {model_name}: "
                f"HTTP {response.status_code}"
            )

            if response.status_code == 200:

                data = response.json()

                candidates = data.get(
                    "candidates",
                    []
                )

                if not candidates:
                    raise Exception(
                        "Gemini returned no candidates."
                    )

                content = candidates[0].get(
                    "content",
                    {}
                )

                response_parts = content.get(
                    "parts",
                    []
                )

                if not response_parts:
                    raise Exception(
                        "Gemini returned empty response."
                    )

                raw_text = response_parts[0].get(
                    "text",
                    ""
                )

                clean_text = (
                    raw_text
                    .replace("```json", "")
                    .replace("```", "")
                    .strip()
                )

                result = json.loads(
                    clean_text
                )

                return normalize_evaluation(
                    result,
                    total_pages
                )

            elif response.status_code in [
                429,
                500,
                502,
                503,
                504
            ]:

                last_error = (
                    f"{model_name}: "
                    f"HTTP {response.status_code}"
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

            print(
                "Gemini error:",
                last_error
            )

            continue

    raise Exception(
        "AI मूल्यांकन में समस्या: "
        + last_error
    )


# ============================================================
# MARKING LIMITS
# ============================================================

def apply_question_marking_rules(
    question
):

    try:

        pages_used = int(
            question.get(
                "pages_used",
                2
            )
        )

    except Exception:

        pages_used = 2

    # --------------------------------------------------------
    # 2 PAGE ANSWER
    # --------------------------------------------------------

    if pages_used == 2:

        max_marks = 8
        hard_limit = 5.5

    # --------------------------------------------------------
    # 3 PAGE ANSWER
    # --------------------------------------------------------

    elif pages_used == 3:

        max_marks = 12
        hard_limit = 8.5

    # --------------------------------------------------------
    # SAFETY FALLBACK
    # --------------------------------------------------------

    else:

        # User's requested system is specifically
        # 2-page / 8-mark and 3-page / 12-mark.
        # For unexpected page counts we infer the closest rule.

        if pages_used <= 2:

            max_marks = 8
            hard_limit = 5.5

        else:

            max_marks = 12
            hard_limit = 8.5

    try:

        obtained = float(
            question.get(
                "obtained_marks",
                0
            )
        )

    except Exception:

        obtained = 0.0

    obtained = max(
        0,
        min(
            obtained,
            hard_limit
        )
    )

    question["pages_used"] = pages_used
    question["max_marks"] = max_marks
    question["obtained_marks"] = round(
        obtained,
        1
    )

    return question


# ============================================================
# NORMALIZE GEMINI OUTPUT
# ============================================================

def normalize_evaluation(
    data,
    total_pages
):

    questions = data.get(
        "questions",
        []
    )

    if not isinstance(
        questions,
        list
    ):

        questions = []

    normalized_questions = []

    for index, question in enumerate(
        questions
    ):

        if not isinstance(
            question,
            dict
        ):
            continue

        # ----------------------------------------------------
        # Question number
        # ----------------------------------------------------

        try:

            q_no = int(
                question.get(
                    "question_number",
                    index + 1
                )
            )

        except Exception:

            q_no = index + 1

        question["question_number"] = q_no

        # ----------------------------------------------------
        # Start / End Page
        # ----------------------------------------------------

        try:

            start_page = int(
                question.get(
                    "start_page",
                    1
                )
            )

        except Exception:

            start_page = 1

        try:

            end_page = int(
                question.get(
                    "end_page",
                    start_page
                )
            )

        except Exception:

            end_page = start_page

        start_page = max(
            1,
            min(
                start_page,
                total_pages
            )
        )

        end_page = max(
            start_page,
            min(
                end_page,
                total_pages
            )
        )

        pages_used = (
            end_page
            - start_page
            + 1
        )

        # User's marking system is based
        # on 2 or 3 pages.
        question["start_page"] = start_page
        question["end_page"] = end_page
        question["pages_used"] = pages_used

        # ----------------------------------------------------
        # Apply hard marking limits
        # ----------------------------------------------------

        question = (
            apply_question_marking_rules(
                question
            )
        )

        # ----------------------------------------------------
        # Comment
        # ----------------------------------------------------

        comment = str(
            question.get(
                "end_page_comment",
                ""
            )
        ).strip()

        if not comment:

            comment = (
                "उत्तर की प्रस्तुति और विषयगत समझ संतोषजनक है, "
                "लेकिन अधिक विश्लेषण, प्रासंगिक उदाहरण तथा "
                "समकालीन संदर्भ जोड़ने से उत्तर की गुणवत्ता बेहतर हो सकती है।"
            )

        question[
            "end_page_comment"
        ] = comment

        normalized_questions.append(
            question
        )

    # --------------------------------------------------------
    # Page comments
    # --------------------------------------------------------

    raw_page_comments = data.get(
        "page_comments",
        []
    )

    if not isinstance(
        raw_page_comments,
        list
    ):

        raw_page_comments = []

    page_comments = {}

    for item in raw_page_comments:

        if not isinstance(
            item,
            dict
        ):
            continue

        try:

            page_number = int(
                item.get(
                    "page",
                    0
                )
            )

        except Exception:

            continue

        if (
            page_number < 1
            or page_number > total_pages
        ):
            continue

        comment = str(
            item.get(
                "comment",
                ""
            )
        ).strip()

        if comment:

            page_comments[
                page_number
            ] = comment

    # --------------------------------------------------------
    # Calculate totals ourselves
    # --------------------------------------------------------

    total_obtained = sum(
        float(
            q.get(
                "obtained_marks",
                0
            )
        )
        for q in normalized_questions
    )

    total_max = sum(
        float(
            q.get(
                "max_marks",
                0
            )
        )
        for q in normalized_questions
    )

    return {
        "total_obtained_marks": round(
            total_obtained,
            1
        ),

        "total_max_marks": round(
            total_max,
            1
        ),

        "questions":
            normalized_questions,

        "page_comments":
            page_comments,

        "overall_feedback":
            str(
                data.get(
                    "overall_feedback",
                    "मूल्यांकन संपन्न हुआ।"
                )
            ),

        "improvements":
            [
                str(x)
                for x in data.get(
                    "improvements",
                    []
                )
                if str(x).strip()
            ]
    }


# ============================================================
# HINDI TEXT WRAP
# ============================================================

def wrap_text(
    draw,
    text,
    font,
    max_width
):

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

        width = (
            bbox[2]
            - bbox[0]
        )

        if width <= max_width:

            current = test_line

        else:

            if current:
                lines.append(
                    current
                )

            current = word

    if current:
        lines.append(
            current
        )

    return lines


# ============================================================
# HINDI COMMENT BADGE
# ============================================================

def create_hindi_comment_badge(
    text,
    width=900,
    font_size=38,
    padding_x=35,
    padding_y=28
):
    """
    15-40 शब्द के examiner comments के लिए
    बड़ा readable Hindi image badge.
    """

    text = str(text).strip()

    if not text:

        text = (
            "उत्तर की प्रस्तुति संतोषजनक है, "
            "लेकिन विश्लेषण और उदाहरणों में और सुधार की आवश्यकता है।"
        )

    font = get_hindi_font(
        font_size
    )

    # Temporary canvas
    temp = Image.new(
        "RGB",
        (width, 1200),
        color=(255, 247, 247)
    )

    draw = ImageDraw.Draw(
        temp
    )

    max_text_width = (
        width
        - 2 * padding_x
    )

    lines = wrap_text(
        draw,
        text,
        font,
        max_text_width
    )

    line_heights = []

    for line in lines:

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        line_heights.append(
            bbox[3]
            - bbox[1]
        )

    line_spacing = 14

    text_height = (
        sum(line_heights)
        + line_spacing
        * max(
            0,
            len(lines) - 1
        )
    )

    height = (
        text_height
        + 2 * padding_y
    )

    height = max(
        height,
        180
    )

    img = Image.new(
        "RGB",
        (width, height),
        color=(255, 247, 247)
    )

    draw = ImageDraw.Draw(
        img
    )

    # --------------------------------------------------------
    # Red examiner border
    # --------------------------------------------------------

    draw.rounded_rectangle(
        [
            4,
            4,
            width - 5,
            height - 5
        ],
        radius=18,
        outline=(185, 0, 0),
        width=5
    )

    # --------------------------------------------------------
    # Hindi text
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
            bbox[2]
            - bbox[0]
        )

        x = max(
            padding_x,
            (
                width
                - text_width
            ) // 2
        )

        draw.text(
            (x, y),
            line,
            font=font,
            fill=(170, 0, 0)
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
# SCORE CIRCLE IMAGE
# ============================================================

def create_score_circle_image(
    obtained,
    total
):

    size = 520

    img = Image.new(
        "RGBA",
        (size, size),
        (255, 255, 255, 0)
    )

    draw = ImageDraw.Draw(
        img
    )

    # --------------------------------------------------------
    # Circle
    # --------------------------------------------------------

    draw.ellipse(
        [
            8,
            8,
            size - 8,
            size - 8
        ],
        outline=(175, 0, 0),
        width=10,
        fill=(255, 248, 248)
    )

    # --------------------------------------------------------
    # Fonts
    # --------------------------------------------------------

    small_font = get_hindi_font(
        42
    )

    big_font = get_hindi_font(
        68
    )

    # --------------------------------------------------------
    # "प्राप्तांक"
    # --------------------------------------------------------

    title = "प्राप्तांक"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=small_font
    )

    title_width = (
        bbox[2]
        - bbox[0]
    )

    draw.text(
        (
            (size - title_width) // 2,
            100
        ),
        title,
        font=small_font,
        fill=(150, 0, 0)
    )

    # --------------------------------------------------------
    # Score
    # --------------------------------------------------------

    score = (
        f"{obtained:g} / {total:g}"
    )

    bbox = draw.textbbox(
        (0, 0),
        score,
        font=big_font
    )

    score_width = (
        bbox[2]
        - bbox[0]
    )

    draw.text(
        (
            (size - score_width) // 2,
            185
        ),
        score,
        font=big_font,
        fill=(150, 0, 0)
    )

    output = io.BytesIO()

    img.save(
        output,
        format="PNG"
    )

    return output.getvalue()


# ============================================================
# QUESTION MARK BADGE
# ============================================================

def create_question_marks_badge(
    question_number,
    obtained,
    max_marks
):

    text = (
        f"Q{question_number}   "
        f"{obtained:g} / {max_marks:g}"
    )

    font = get_hindi_font(
        30
    )

    width = 420
    height = 125

    img = Image.new(
        "RGB",
        (width, height),
        color=(255, 247, 247)
    )

    draw = ImageDraw.Draw(
        img
    )

    draw.rounded_rectangle(
        [
            4,
            4,
            width - 5,
            height - 5
        ],
        radius=15,
        outline=(175, 0, 0),
        width=5
    )

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    text_width = (
        bbox[2]
        - bbox[0]
    )

    text_height = (
        bbox[3]
        - bbox[1]
    )

    x = (
        width
        - text_width
    ) // 2

    y = (
        height
        - text_height
    ) // 2

    draw.text(
        (x, y),
        text,
        font=font,
        fill=(165, 0, 0)
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

    total_obtained = eval_data.get(
        "total_obtained_marks",
        0
    )

    total_max = eval_data.get(
        "total_max_marks",
        0
    )

    questions = eval_data.get(
        "questions",
        []
    )

    page_comments = eval_data.get(
        "page_comments",
        {}
    )

    total_pages = len(
        pdf_doc
    )

    # ========================================================
    # QUESTION MARKS MAP
    # ========================================================

    question_marks_by_page = {}

    for question in questions:

        end_page = int(
            question.get(
                "end_page",
                1
            )
        )

        if end_page < 1:
            end_page = 1

        if end_page > total_pages:
            end_page = total_pages

        question_marks_by_page.setdefault(
            end_page,
            []
        ).append(
            question
        )

    # ========================================================
    # PROCESS EVERY PAGE
    # ========================================================

    for page_index in range(
        total_pages
    ):

        page = pdf_doc[
            page_index
        ]

        page_width = (
            page.rect.width
        )

        page_height = (
            page.rect.height
        )

        # ====================================================
        # FIRST PAGE LEFT SCORE CIRCLE
        # ====================================================

        if page_index == 0:

            score_png = (
                create_score_circle_image(
                    total_obtained,
                    total_max
                )
            )

            score_rect = fitz.Rect(
                10,
                15,
                78,
                83
            )

            page.insert_image(
                score_rect,
                stream=score_png,
                keep_proportion=True
            )

        # ====================================================
        # PAGE COMMENT
        # ====================================================

        page_number = (
            page_index + 1
        )

        comment = page_comments.get(
            page_number,
            ""
        )

        if comment:

            comment_png = (
                create_hindi_comment_badge(
                    comment,
                    width=1000,
                    font_size=38
                )
            )

            # -----------------------------------------------
            # Right-side margin badge
            # -----------------------------------------------

            margin_width = min(
                175,
                page_width * 0.22
            )

            x_right = (
                page_width - 5
            )

            x_left = (
                x_right
                - margin_width
            )

            # Make badge tall enough for 15-40 words
            target_top = 95

            target_height = min(
                220,
                page_height * 0.32
            )

            target_rect = fitz.Rect(
                x_left,
                target_top,
                x_right,
                target_top + target_height
            )

            page.insert_image(
                target_rect,
                stream=comment_png,
                keep_proportion=True
            )

        # ====================================================
        # QUESTION END MARKS
        # ====================================================

        page_questions = (
            question_marks_by_page.get(
                page_number,
                []
            )
        )

        if page_questions:

            # Put marks toward lower-right area,
            # i.e. near answer ending.
            y = (
                page_height
                - 70
                - (
                    len(page_questions)
                    * 42
                )
            )

            for question in page_questions:

                q_no = question.get(
                    "question_number",
                    1
                )

                obtained = question.get(
                    "obtained_marks",
                    0
                )

                max_marks = question.get(
                    "max_marks",
                    8
                )

                marks_png = (
                    create_question_marks_badge(
                        q_no,
                        obtained,
                        max_marks
                    )
                )

                rect = fitz.Rect(
                    page_width - 105,
                    y,
                    page_width - 8,
                    y + 35
                )

                page.insert_image(
                    rect,
                    stream=marks_png,
                    keep_proportion=True
                )

                y += 40

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
# TELEGRAM START / HELP
# ============================================================

if bot:

    @bot.message_handler(
        commands=[
            "start",
            "help"
        ]
    )
    def send_welcome(message):

        bot.reply_to(
            message,

            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "नमस्ते! अपनी उत्तर पुस्तिका की "
            "<b>PDF फ़ाइल</b> या <b>फ़ोटो</b> भेजें।\n\n"
            "AI आपकी कॉपी का प्रश्नवार एवं पृष्ठवार "
            "मूल्यांकन करेगा।"
        )


# ============================================================
# ANSWER SHEET HANDLER
# ============================================================

if bot:

    @bot.message_handler(
        content_types=[
            "document",
            "photo"
        ]
    )
    def handle_answer_sheet(
        message
    ):

        chat_id = (
            message.chat.id
        )

        status_msg = bot.reply_to(
            message,

            "⏳ <b>कॉपी प्राप्त हो गई है।</b>\n\n"
            "PRANA PCS AI द्वारा "
            "<b>प्रश्नवार एवं पृष्ठवार मूल्यांकन</b> "
            "चल रहा है..."
        )

        pdf_doc = None

        try:

            # =================================================
            # CREATE PDF
            # =================================================

            pdf_doc = fitz.open()

            images_b64 = []

            # =================================================
            # DOCUMENT
            # =================================================

            if (
                message.content_type
                == "document"
            ):

                file_info = bot.get_file(
                    message.document.file_id
                )

                downloaded_file = (
                    bot.download_file(
                        file_info.file_path
                    )
                )

                filename = (
                    message.document.file_name
                    or ""
                ).lower()

                # ---------------------------------------------
                # PDF
                # ---------------------------------------------

                if filename.endswith(
                    ".pdf"
                ):

                    pdf_doc.close()

                    pdf_doc = fitz.open(
                        stream=downloaded_file,
                        filetype="pdf"
                    )

                    for page in pdf_doc:

                        pix = page.get_pixmap(
                            dpi=100,
                            alpha=False
                        )

                        jpeg_bytes = (
                            pix.tobytes(
                                "jpeg",
                                jpg_quality=82
                            )
                        )

                        images_b64.append(
                            base64.b64encode(
                                jpeg_bytes
                            ).decode(
                                "utf-8"
                            )
                        )

                # ---------------------------------------------
                # IMAGE FILE
                # ---------------------------------------------

                else:

                    img = Image.open(
                        io.BytesIO(
                            downloaded_file
                        )
                    ).convert(
                        "RGB"
                    )

                    img_stream = (
                        io.BytesIO()
                    )

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
                        ).decode(
                            "utf-8"
                        )
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
            # PHOTO
            # =================================================

            elif (
                message.content_type
                == "photo"
            ):

                file_info = bot.get_file(
                    message.photo[-1].file_id
                )

                downloaded_file = (
                    bot.download_file(
                        file_info.file_path
                    )
                )

                img = Image.open(
                    io.BytesIO(
                        downloaded_file
                    )
                ).convert(
                    "RGB"
                )

                img_stream = (
                    io.BytesIO()
                )

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
                    ).decode(
                        "utf-8"
                    )
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
                    "Unsupported file type."
                )

            # =================================================
            # PAGE COUNT
            # =================================================

            total_pages = len(
                pdf_doc
            )

            if total_pages == 0:

                raise Exception(
                    "PDF में कोई page नहीं मिला।"
                )

            # =================================================
            # GEMINI
            # =================================================

            eval_result = (
                evaluate_with_gemini(
                    images_b64,
                    total_pages
                )
            )

            total_obtained = (
                eval_result.get(
                    "total_obtained_marks",
                    0
                )
            )

            total_max = (
                eval_result.get(
                    "total_max_marks",
                    0
                )
            )

            feedback = (
                eval_result.get(
                    "overall_feedback",
                    "मूल्यांकन संपन्न हुआ।"
                )
            )

            improvements = (
                eval_result.get(
                    "improvements",
                    []
                )
            )

            # =================================================
            # IMPROVEMENTS
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
                    "• अतिरिक्त सुझाव उपलब्ध नहीं।"
                )

            # =================================================
            # TELEGRAM CAPTION
            # =================================================

            result_caption = (
                "🏛️ <b>PRANA PCS - मूल्यांकन रिपोर्ट</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>कुल प्राप्तांक:</b> "
                f"<code>{total_obtained:g} / "
                f"{total_max:g}</code>\n\n"
                f"📝 <b>समग्र समीक्षा:</b>\n"
                f"{feedback}\n\n"
                f"💡 <b>सुधार सुझाव:</b>\n"
                f"{imp_text}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>जाँची हुई प्रमाणित कॉपी नीचे संलग्न है 👇</i>"
            )

            # =================================================
            # CREATE FINAL PDF
            # =================================================

            stamped_pdf = (
                create_rich_annotated_pdf(
                    pdf_doc,
                    eval_result
                )
            )

            pdf_doc = None

            # =================================================
            # DELETE STATUS
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
        # ERROR HANDLING
        # =====================================================

        except Exception as e:

            print(
                "Evaluation error:",
                repr(e)
            )

            if pdf_doc is not None:

                try:
                    pdf_doc.close()

                except Exception:
                    pass

            error_text = str(e)

            if len(error_text) > 250:

                error_text = (
                    error_text[:250]
                    + "..."
                )

            try:

                bot.edit_message_text(
                    (
                        "⚠️ <b>मूल्यांकन में समस्या</b>\n\n"
                        f"{error_text}\n\n"
                        "कृपया स्पष्ट PDF या फोटो पुनः भेजें।"
                    ),

                    chat_id=chat_id,

                    message_id=(
                        status_msg.message_id
                    )
                )

            except Exception:

                try:

                    bot.send_message(
                        chat_id,
                        (
                            "⚠️ मूल्यांकन में समस्या हुई।\n"
                            "कृपया PDF/फोटो पुनः भेजें।"
                        )
                    )

                except Exception:
                    pass
