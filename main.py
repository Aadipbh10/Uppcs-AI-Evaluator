import os
import io
import json
import base64
import tempfile
import requests
from pathlib import Path

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

app = FastAPI()

bot = (
    telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
    if BOT_TOKEN
    else None
)


# ============================================================
# FONT
# ============================================================

FONT_PATH = "/tmp/Kalam-Regular.ttf"

FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/"
    "ofl/kalam/Kalam-Regular.ttf"
)


# ============================================================
# TEMP STORAGE
# ============================================================

PENDING = {}


# ============================================================
# GEMINI MODELS
# ============================================================
# Primary model is first.
# These names are based on the models visible in your Render log.

MODELS = [
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]


# ============================================================
# GS RUBRICS
# ============================================================

RUBRICS = {

    "GS1": """
GS1: History-Art-Culture, Geography, Indian Society.

Focus:
- multidimensional analysis
- chronology and context
- historians/thinkers/quotes
- maps and diagrams
- geography examples
- society data/reports
- contemporary linkage
- case studies
- relevant constitutional/social dimensions

Value addition:
maps, timelines, diagrams, data, reports, thinkers,
cultural examples and contemporary examples.

Avoid generic essay-like writing.
""",

    "GS2": """
GS2: Constitution, Polity, Governance, Social Justice, International Relations.

Focus:
- Articles
- constitutional provisions
- amendments
- Supreme Court judgments
- committees
- ARC
- constitutional morality
- government initiatives
- balanced challenges and solutions
- federalism
- institutions
- governance mechanisms

For IR:
strategic, diplomatic, economic and security dimensions,
relevant examples and maps.

Way Forward is important.
""",

    "GS3": """
GS3: Economy, Agriculture, Science-Tech, Environment,
Disaster Management and Internal Security.

Use 3D:
Data + Diagram + Dynamics.

Look for:
Economic Survey
Budget
NITI Aayog
official reports
policy names
technical applications
disaster cycle
climate mitigation/adaptation
security institutions
relevant examples
""",

    "GS4": """
GS4: Ethics, Integrity and Aptitude.

Theory must be applied.

Look for:
- precise ethical definitions
- thinkers
- quotes
- real administrative examples
- ethical dilemmas
- stakeholders
- emotional intelligence
- constitutional morality
- good governance

Case studies:
stakeholders -> dilemmas -> options -> pros/cons ->
balanced decision -> implementation.
""",

    "GS5": """
GS5: Uttar Pradesh-specific History, Culture, Polity,
Governance, Security, Education, Health and Tourism.

Hyper-localization is central.

Look for:
- UP districts
- UP schemes
- UP portals
- UP-specific data
- Purvanchal
- Bundelkhand
- Western UP
- Awadh
- UP maps
- ODOP
- local culture
- Nepal-border districts
- UP security institutions

Generic all-India answers should score poorly when UP specificity is required.
""",

    "GS6": """
GS6: Uttar Pradesh Economy, Agriculture, Geography,
Environment, Science-Tech and Infrastructure.

Focus on:
- UP Budget
- UP Economic Survey
- UP data
- UP maps
- regional/sectoral analysis
- agro-climatic zones
- minerals
- expressways
- defence corridor
- Ramsar sites
- tiger reserves
- UP policies
- infrastructure

Generic answers without UP data/policy/map should be below average.
"""
}


# ============================================================
# FONT HELPERS
# ============================================================

def ensure_font():
    try:
        if (
            os.path.exists(FONT_PATH)
            and os.path.getsize(FONT_PATH) > 10000
        ):
            return True

        response = requests.get(
            FONT_URL,
            timeout=30
        )
        response.raise_for_status()

        with open(FONT_PATH, "wb") as f:
            f.write(response.content)

        return os.path.getsize(FONT_PATH) > 10000

    except Exception as e:
        print("FONT ERROR:", e)
        return False


ensure_font()


def font(size):
    try:
        return ImageFont.truetype(
            FONT_PATH,
            size
        )
    except Exception:
        return ImageFont.load_default()


# ============================================================
# PAPER NORMALIZATION
# ============================================================

def normalize_paper(text):

    t = (
        str(text)
        .upper()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )

    clean = str(text).replace(" ", "")

    for paper in (
        "GS1",
        "GS2",
        "GS3",
        "GS4",
        "GS5",
        "GS6"
    ):
        if paper in t:
            return paper

        if paper.replace("GS", "जीएस") in clean:
            return paper

    return None


# ============================================================
# FILE HELPERS
# ============================================================

def save_submission(
    data,
    suffix=".bin"
):

    fd, path = tempfile.mkstemp(
        prefix="prana_",
        suffix=suffix
    )

    with os.fdopen(fd, "wb") as f:
        f.write(data)

    return path


def ask_paper(message):

    bot.reply_to(
        message,

        "📚 <b>कॉपी प्राप्त हो गई है।</b>\n\n"
        "मूल्यांकन शुरू करने से पहले <b>Paper Name</b> भेजें:\n\n"
        "• GS 1\n"
        "• GS 2\n"
        "• GS 3\n"
        "• GS 4\n"
        "• GS 5\n"
        "• GS 6\n\n"
        "उदाहरण: <b>GS 3</b>"
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():

    ensure_font()

    print(
        "AVAILABLE GEMINI MODELS CONFIGURED:",
        MODELS
    )

    if bot:

        try:

            bot.remove_webhook()

            bot.set_webhook(
                url=f"{RENDER_EXTERNAL_URL}/webhook"
            )

        except Exception as e:

            print(
                "WEBHOOK ERROR:",
                e
            )


@app.get("/")
def home():

    return {
        "status": "PRANA PCS AI Evaluator Active",
        "font": os.path.exists(FONT_PATH),
        "models": MODELS
    }


@app.get("/api/model-status")
def model_status():

    return {
        "configured_models": MODELS
    }


@app.post("/webhook")
async def webhook(request: Request):

    if bot:

        data = await request.json()

        update = telebot.types.Update.de_json(
            data
        )

        bot.process_new_updates(
            [update]
        )

    return {"ok": True}


# ============================================================
# PDF -> IMAGES
# ============================================================

def image_pages_from_pdf(pdf):

    pages = []

    for page in pdf:

        pix = page.get_pixmap(
            dpi=120,
            alpha=False
        )

        pages.append(
            pix.tobytes(
                "jpeg",
                jpg_quality=88
            )
        )

    return pages


# ============================================================
# GEMINI PROMPT
# ============================================================

def build_prompt(
    paper,
    total_pages
):

    return f"""
आप PRANA PCS के वरिष्ठ UPPCS Mains examiner हैं।

Paper: {paper}
Total pages: {total_pages}

{RUBRICS[paper]}

============================================================
IMPORTANT: LANGUAGE RULE
============================================================

जिस भाषा में विद्यार्थी ने उत्तर लिखा है, उसी भाषा में evaluation करें।

यदि copy Hindi में है:
- सभी examiner comments Hindi में हों।
- overall feedback Hindi में हो।

यदि copy English में है:
- सभी examiner comments English में हों।
- overall feedback English में हो।

UI language या Telegram language का answer-copy evaluation language
पर कोई प्रभाव नहीं होना चाहिए।

============================================================
MARKING
============================================================

Question की actual demand को पहले identify करें।

केवल topic coverage देखकर marks न दें।

Check:
1. प्रश्न के कितने अलग components हैं?
2. कितने components पूरे हुए?
3. कौन partial है?
4. कौन skipped है?
5. command word satisfy हुआ या नहीं?
6. introduction/body/conclusion demand के अनुरूप हैं या नहीं?

Examples:

"कारण तथा उपाय" =
कारण + उपाय

"तुलना कीजिए" =
दोनों पक्ष + comparison

"मूल्यांकन कीजिए" =
merits + limitations + judgement

"प्रभाव एवं समाधान" =
impacts + solutions

"महत्व स्पष्ट करते हुए चुनौतियाँ बताइए" =
importance + challenges

यदि demanded part छूटा हो:
marks घटाएँ और RED comment दें।

============================================================
STRICT ANNOTATION RULE — ABSOLUTELY MANDATORY
============================================================

COPY पर examiner comments लिखना अनिवार्य है।

किसी भी परिस्थिति में page comments skip नहीं करने हैं।

NO BLANK SPACE DOES NOT MEAN NO COMMENT.

हर page पर comments render होने चाहिए।

------------------------------------------------------------
FULL PAGE
------------------------------------------------------------

हर full page:
- minimum 4 substantive page_comments
- ideally 4-5
- 4-6 checking annotations

------------------------------------------------------------
HALF PAGE
------------------------------------------------------------

Half-page answer:
- minimum 2 substantive page_comments
- ideally 2-3
- 2-3 checking annotations

------------------------------------------------------------
RED / GREEN
------------------------------------------------------------

RED comment:

- factual mistake
- conceptual mistake
- wrong terminology
- inappropriate word
- missing demand
- weak analysis
- missing example
- missing data
- missing dimension
- poor structure
- improvement opportunity

RED comment actionable होना चाहिए।

Examples:

"यहाँ प्रश्न की मांग के अनुसार चुनौतियों का उल्लेख अपेक्षित था।"

"यहाँ संबंधित आँकड़ा/रिपोर्ट जोड़ने से तर्क अधिक मजबूत होता।"

"यह बिंदु सही है, लेकिन इसके प्रभाव का विश्लेषण जोड़ना चाहिए था।"

"यहाँ उपयुक्त उदाहरण के रूप में ______ जोड़ा जा सकता था।"


GREEN comment:

- good fact
- correct data
- strong example
- relevant article/reference
- good analysis
- effective introduction
- strong conclusion
- useful diagram/map
- good presentation
- value addition

GREEN comment भी substantive होना चाहिए।

सिर्फ:
"अच्छा"
"सही"
"बेहतरीन"

जैसे छोटे comments पर्याप्त नहीं हैं।

Examples:

"प्रासंगिक उदाहरण से तर्क को प्रभावी आधार मिला है।"

"यह तथ्यात्मक value addition उत्तर को सामान्य उत्तरों से अलग करता है।"

"बिंदुवार प्रस्तुतीकरण से उत्तर की readability बेहतर हुई है।"

------------------------------------------------------------
COMMENT BALANCE
------------------------------------------------------------

यदि 4 comments हैं और वास्तविक सुधार की गुंजाइश है,
तो कम से कम 2 comments constructive RED होने चाहिए।

बाकी GREEN हो सकते हैं।

यदि वास्तविक गलती नहीं है तो गलती invent न करें।

लेकिन comments की संख्या कम न करें।

------------------------------------------------------------
COMMENT PLACEMENT
------------------------------------------------------------

Comments answer के ऊपर या लिखे हुए text पर नहीं होने चाहिए
जब तक कोई सुरक्षित खाली जगह उपलब्ध हो।

पहले ये जगह खोजें:

1. ऊपर का खाली margin
2. नीचे का खाली margin
3. left margin
4. right margin
5. paragraphs के बीच का blank area
6. page के किनारे
7. अन्य white space

Shadow, हल्की grey background, scan noise, faint lines और paper texture
को खाली जगह खोजने में बाधा न मानें।

Gemini को placement_box देना है, लेकिन final placement Python करेगा।

यदि preferred placement occupied है:
दूसरी खाली जगह खोजें।

यदि पूरी page पर पर्याप्त blank space नहीं है:

COMMENT SKIP करना STRICTLY FORBIDDEN है।

ऐसी स्थिति में:
- comment छोटा करें
- margin में compact करें
- सबसे कम occupied area में रखें
- आवश्यकता पड़ने पर controlled overlap करें

लेकिन comment render अवश्य करें।

------------------------------------------------------------
HANDWRITTEN STYLE
------------------------------------------------------------

Comments:
- Kalam font
- बड़े
- clearly visible
- red/green examiner ink
- transparent background
- no box
- no card
- no sticker
- no white rectangle
- no background panel

जहाँ संभव हो thin arrow से relevant answer point की ओर संकेत करें।

============================================================
ANNOTATIONS
============================================================

Good point:
type = "good"
color = "green"

Wrong/mistake:
type = "wrong"
color = "red"

गलत word/phrase पर tight bbox दें।

अच्छे point पर tick लगाने योग्य bbox दें।

अस्पष्ट handwriting को गलत न मानें।

============================================================
QUESTION DEMAND
============================================================

हर question में:

demand_parts
fulfilled_parts
skipped_parts

अनिवार्य हैं।

यदि कोई demanded part missing है:
skipped_parts में डालें।

और page_comment में साफ लिखें:

"प्रश्न की मांग का यह भाग छूट गया है — यहाँ ______ अपेक्षित था।"

यदि partial है:

"यह भाग आंशिक है; ______ जोड़ने से demand पूरी होती।"

============================================================
OVERALL FEEDBACK
============================================================

overall_feedback केवल 4-5 छोटी lines की समग्र टिप्पणी हो।

इसमें:
- भाषा एवं अभिव्यक्ति
- उत्तर की शैली/संरचना
- प्रस्तुतीकरण
- विश्लेषण/value addition
- आगे सुधार की आशावादी दिशा

का संतुलित उल्लेख हो।

कोई अलग heading, bullet list, score repeat या suggestions list नहीं।

============================================================
OUTPUT
============================================================

केवल valid JSON दें:

{{
  "total_obtained_marks": 0,
  "total_max_marks": 0,

  "questions": [
    {{
      "question_number": 1,
      "start_page": 1,
      "end_page": 2,
      "pages_used": 2,
      "max_marks": 8,
      "obtained_marks": 5.0,

      "demand_parts": [
        "प्रश्न की मांग का पहला भाग",
        "प्रश्न की मांग का दूसरा भाग"
      ],

      "fulfilled_parts": [
        "पूरा किया गया भाग"
      ],

      "skipped_parts": [
        "छूटा हुआ भाग"
      ],

      "end_page_comment":
      "15-40 शब्द की substantive examiner टिप्पणी"
    }}
  ],

  "page_comments": [
    {{
      "page": 1,
      "color": "green",
      "comment": "प्रासंगिक उदाहरण से तर्क को प्रभावी आधार मिला है।",
      "placement_box": [50, 700, 300, 995],
      "anchor": [400, 500, 550, 800]
    }},

    {{
      "page": 1,
      "color": "red",
      "comment": "यहाँ प्रश्न की मांग के अनुसार एक अतिरिक्त आयाम अपेक्षित था।",
      "placement_box": [300, 5, 520, 300],
      "anchor": [500, 250, 650, 700]
    }},

    {{
      "page": 1,
      "color": "green",
      "comment": "बिंदुवार प्रस्तुतीकरण से उत्तर की readability बेहतर हुई है।",
      "placement_box": [520, 700, 760, 995],
      "anchor": [650, 450, 800, 850]
    }},

    {{
      "page": 1,
      "color": "red",
      "comment": "यहाँ संबंधित आँकड़ा या आधिकारिक रिपोर्ट जोड़ने से विश्लेषण अधिक मजबूत होता।",
      "placement_box": [760, 5, 995, 300],
      "anchor": [750, 200, 900, 700]
    }}
  ],

  "annotations": [
    {{
      "page": 1,
      "type": "wrong",
      "color": "red",
      "exact_text": "गलत शब्द",
      "reason": "गलत terminology",
      "box_2d": [400, 500, 450, 650]
    }},
    {{
      "page": 1,
      "type": "good",
      "color": "green",
      "exact_text": "अच्छा तथ्य",
      "box_2d": [600, 300, 650, 450]
    }}
  ],

  "overall_feedback":
  "समग्र मूल्यांकन",

  "improvements": []
}}

IMPORTANT:
- Page numbers 1-based हैं।
- हर full page के लिए 4-6 annotations दें।
- Half page के लिए 2-3 annotations दें।
- हर full page के लिए minimum 4 page_comments दें।
- Half page के लिए minimum 2 page_comments दें।
- हर page_comment में color अनिवार्य है।
- placement_box अनिवार्य है।
- anchor अनिवार्य है।
- placement_box को actual blank space के लिए prefer करें।
- comment कभी skip न करें।
- केवल topic coverage नहीं, question demand भी marks में शामिल करें।
"""


# ============================================================
# GEMINI API
# ============================================================

def call_gemini(
    images,
    paper
):

    parts = []

    for image_bytes in images:

        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(
                        image_bytes
                    ).decode()
                }
            }
        )

    parts.append(
        {
            "text": build_prompt(
                paper,
                len(images)
            )
        }
    )

    payload = {
        "contents": [
            {
                "parts": parts
            }
        ],

        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.15
        }
    }

    last_error = ""

    for model in MODELS:

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
            f"?key={GEMINI_API_KEY}"
        )

        try:

            response = requests.post(
                url,
                json=payload,
                timeout=300
            )

            if response.status_code == 200:

                body = response.json()

                raw = (
                    body
                    ["candidates"][0]
                    ["content"]["parts"][0]
                    ["text"]
                )

                try:
                    data = json.loads(raw)
                except Exception:

                    # Sometimes JSON is wrapped in markdown.
                    raw = (
                        raw
                        .replace("```json", "")
                        .replace("```", "")
                        .strip()
                    )

                    data = json.loads(raw)

                print(
                    "GEMINI SUCCESS:",
                    model
                )

                return normalize_result(
                    data,
                    len(images)
                )

            last_error = (
                f"{model}: HTTP "
                f"{response.status_code} "
                f"{response.text[:500]}"
            )

            print(
                "GEMINI MODEL FAILED:",
                last_error
            )

            if response.status_code not in (
                400,
                404,
                429,
                500,
                502,
                503,
                504
            ):
                break

        except Exception as e:

            last_error = (
                f"{model}: {str(e)}"
            )

            print(
                "GEMINI REQUEST ERROR:",
                last_error
            )

    raise Exception(
        "Gemini evaluation failed: "
        + last_error
    )


# ============================================================
# RESULT NORMALIZATION
# ============================================================

def normalize_result(
    data,
    pages
):

    questions = []

    for index, question in enumerate(
        data.get(
            "questions",
            []
        )
    ):

        if not isinstance(
            question,
            dict
        ):
            continue

        try:

            start_page = int(
                question.get(
                    "start_page",
                    1
                )
            )

            end_page = int(
                question.get(
                    "end_page",
                    start_page
                )
            )

        except Exception:

            start_page = 1
            end_page = 1

        start_page = max(
            1,
            min(
                pages,
                start_page
            )
        )

        end_page = max(
            start_page,
            min(
                pages,
                end_page
            )
        )

        pages_used = (
            end_page
            - start_page
            + 1
        )

        if pages_used <= 2:

            max_marks = 8
            hard_cap = 5.5

        else:

            max_marks = 12
            hard_cap = 8.5

        try:

            obtained = float(
                question.get(
                    "obtained_marks",
                    0
                )
            )

        except Exception:

            obtained = 0

        obtained = max(
            0,
            min(
                obtained,
                hard_cap
            )
        )

        questions.append(
            {
                "question_number": int(
                    question.get(
                        "question_number",
                        index + 1
                    )
                ),

                "start_page": start_page,

                "end_page": end_page,

                "pages_used": pages_used,

                "max_marks": max_marks,

                "obtained_marks": round(
                    obtained,
                    1
                ),

                "demand_parts": [
                    str(x)
                    for x in question.get(
                        "demand_parts",
                        []
                    )
                ],

                "fulfilled_parts": [
                    str(x)
                    for x in question.get(
                        "fulfilled_parts",
                        []
                    )
                ],

                "skipped_parts": [
                    str(x)
                    for x in question.get(
                        "skipped_parts",
                        []
                    )
                ],

                "end_page_comment": str(
                    question.get(
                        "end_page_comment",
                        ""
                    )
                ).strip()
            }
        )

    total_obtained = round(
        sum(
            q["obtained_marks"]
            for q in questions
        ),
        1
    )

    total_max = round(
        sum(
            q["max_marks"]
            for q in questions
        ),
        1
    )

    # --------------------------------------------------------
    # Normalize page comments.
    # Do NOT delete them because of placement problems.
    # --------------------------------------------------------

    page_comments = []

    for item in data.get(
        "page_comments",
        []
    ):

        if not isinstance(
            item,
            dict
        ):
            continue

        try:

            page = int(
                item.get(
                    "page",
                    1
                )
            )

        except Exception:

            page = 1

        page = max(
            1,
            min(
                pages,
                page
            )
        )

        text = str(
            item.get(
                "comment",
                ""
            )
        ).strip()

        if not text:
            continue

        color = str(
            item.get(
                "color",
                "red"
            )
        ).lower().strip()

        if color not in (
            "red",
            "green"
        ):
            color = "red"

        placement_box = item.get(
            "placement_box",
            [50, 700, 300, 995]
        )

        anchor = item.get(
            "anchor",
            [450, 400, 550, 600]
        )

        page_comments.append(
            {
                "page": page,
                "color": color,
                "comment": text,
                "placement_box": placement_box,
                "anchor": anchor
            }
        )

    # --------------------------------------------------------
    # Normalize annotations.
    # --------------------------------------------------------

    annotations = []

    for item in data.get(
        "annotations",
        []
    ):

        if not isinstance(
            item,
            dict
        ):
            continue

        try:

            page = int(
                item.get(
                    "page",
                    1
                )
            )

        except Exception:

            page = 1

        page = max(
            1,
            min(
                pages,
                page
            )
        )

        annotation_type = str(
            item.get(
                "type",
                "good"
            )
        ).lower().strip()

        if annotation_type not in (
            "good",
            "wrong"
        ):
            annotation_type = "good"

        color = (
            "green"
            if annotation_type == "good"
            else "red"
        )

        annotations.append(
            {
                "page": page,
                "type": annotation_type,
                "color": color,
                "exact_text": str(
                    item.get(
                        "exact_text",
                        ""
                    )
                ),
                "reason": str(
                    item.get(
                        "reason",
                        ""
                    )
                ),
                "box_2d": item.get(
                    "box_2d",
                    [0, 0, 0, 0]
                )
            }
        )

    overall_feedback = str(
        data.get(
            "overall_feedback",
            ""
        )
    ).strip()

    return {
        "total_obtained_marks":
            total_obtained,

        "total_max_marks":
            total_max,

        "questions":
            questions,

        "page_comments":
            page_comments,

        "annotations":
            annotations,

        "overall_feedback":
            overall_feedback,

        "improvements":
            [
                str(x)
                for x in data.get(
                    "improvements",
                    []
                )
            ][:6]
    }


# ============================================================
# TEXT WRAPPING
# ============================================================

def wrap_text(
    draw,
    text,
    fnt,
    max_width
):

    words = str(text).split()

    lines = []
    current = ""

    for word in words:

        test = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            test,
            font=fnt
        )

        if (
            bbox[2] - bbox[0]
            <= max_width
        ):

            current = test

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

    return lines or [""]


# ============================================================
# COMMENT IMAGE
# ============================================================

def make_comment_badge(
    text,
    width=1600,
    font_size=92,
    color="red"
):

    fnt = font(font_size)

    if color == "green":

        ink = (
            0,
            110,
            45,
            255
        )

    else:

        ink = (
            145,
            0,
            0,
            255
        )

    padding = 6

    temp = Image.new(
        "RGBA",
        (width, 1800),
        (255, 255, 255, 0)
    )

    draw = ImageDraw.Draw(
        temp
    )

    lines = wrap_text(
        draw,
        text,
        fnt,
        width - 2 * padding
    )

    line_heights = []

    for line in lines:

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=fnt
        )

        line_heights.append(
            bbox[3] - bbox[1]
        )

    line_gap = 10

    height = max(
        90,
        sum(line_heights)
        + line_gap * max(
            0,
            len(lines) - 1
        )
        + 2 * padding
    )

    image = Image.new(
        "RGBA",
        (width, height),
        (255, 255, 255, 0)
    )

    draw = ImageDraw.Draw(
        image
    )

    y = padding

    for line, line_height in zip(
        lines,
        line_heights
    ):

        draw.text(
            (padding, y),
            line,
            font=fnt,
            fill=ink
        )

        y += (
            line_height
            + line_gap
        )

    output = io.BytesIO()

    image.save(
        output,
        "PNG"
    )

    return output.getvalue()


# ============================================================
# SCORE BADGE
# ============================================================

def make_score_badge(
    obtained,
    total
):

    size = 900

    image = Image.new(
        "RGBA",
        (size, size),
        (255, 255, 255, 0)
    )

    draw = ImageDraw.Draw(
        image
    )

    draw.ellipse(
        (
            12,
            12,
            size - 12,
            size - 12
        ),
        fill=(255, 250, 250),
        outline=(170, 0, 0),
        width=14
    )

    title_font = font(62)
    score_font = font(96)

    title = "प्राप्तांक"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=title_font
    )

    draw.text(
        (
            (
                size
                - (
                    bbox[2] - bbox[0]
                )
            ) // 2,
            150
        ),
        title,
        font=title_font,
        fill=(160, 0, 0)
    )

    score = (
        f"{obtained:g} / {total:g}"
    )

    bbox = draw.textbbox(
        (0, 0),
        score,
        font=score_font
    )

    draw.text(
        (
            (
                size
                - (
                    bbox[2] - bbox[0]
                )
            ) // 2,
            340
        ),
        score,
        font=score_font,
        fill=(160, 0, 0)
    )

    output = io.BytesIO()

    image.save(
        output,
        "PNG"
    )

    return output.getvalue()


# ============================================================
# QUESTION MARKS
# ============================================================

def make_marks_badge(
    question_number,
    obtained,
    total
):

    fnt = font(46)

    text = (
        f"Q{question_number}   "
        f"{obtained:g}/{total:g}"
    )

    width = 650
    height = 150

    image = Image.new(
        "RGB",
        (width, height),
        (255, 250, 250)
    )

    draw = ImageDraw.Draw(
        image
    )

    draw.rounded_rectangle(
        (
            5,
            5,
            width - 6,
            height - 6
        ),
        radius=18,
        outline=(170, 0, 0),
        width=7
    )

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=fnt
    )

    draw.text(
        (
            (
                width
                - (
                    bbox[2] - bbox[0]
                )
            ) // 2,
            (
                height
                - (
                    bbox[3] - bbox[1]
                )
            ) // 2
        ),
        text,
        font=fnt,
        fill=(160, 0, 0)
    )

    output = io.BytesIO()

    image.save(
        output,
        "PNG"
    )

    return output.getvalue()


# ============================================================
# ARROW
# ============================================================

def draw_arrow(
    page,
    x1,
    y1,
    x2,
    y2
):

    page.draw_line(
        fitz.Point(x1, y1),
        fitz.Point(x2, y2),
        color=(0.65, 0, 0),
        width=1.4
    )

    dx = x1 - x2
    dy = y1 - y2

    length = max(
        (dx * dx + dy * dy) ** 0.5,
        1
    )

    ux = dx / length
    uy = dy / length

    p = fitz.Point(
        x2 + ux * 8,
        y2 + uy * 8
    )

    q = fitz.Point(
        x2 - uy * 6 + ux * 8,
        y2 + ux * 6 + uy * 8
    )

    r = fitz.Point(
        x2 + uy * 6 + ux * 8,
        y2 - ux * 6 + uy * 8
    )

    page.draw_polyline(
        [p, q, r, p],
        color=(0.65, 0, 0),
        fill=(0.65, 0, 0)
    )


# ============================================================
# CIRCLE
# ============================================================

def add_circle(
    page,
    box,
    page_width,
    page_height
):

    try:

        ymin, xmin, ymax, xmax = [
            max(
                0,
                min(
                    1000,
                    int(v)
                )
            )
            for v in box
        ]

        x1 = (
            page_width
            * xmin
            / 1000
        )

        x2 = (
            page_width
            * xmax
            / 1000
        )

        y1 = (
            page_height
            * ymin
            / 1000
        )

        y2 = (
            page_height
            * ymax
            / 1000
        )

        pad_x = max(
            3,
            (x2 - x1) * 0.10
        )

        pad_y = max(
            3,
            (y2 - y1) * 0.25
        )

        rect = fitz.Rect(
            max(
                0,
                x1 - pad_x
            ),
            max(
                0,
                y1 - pad_y
            ),
            min(
                page_width,
                x2 + pad_x
            ),
            min(
                page_height,
                y2 + pad_y
            )
        )

        page.draw_oval(
            rect,
            color=(0.65, 0, 0),
            width=2.2
        )

    except Exception as e:

        print(
            "CIRCLE ERROR:",
            e
        )


# ============================================================
# TICK
# ============================================================

def add_tick(
    page,
    box,
    page_width,
    page_height,
    color=(0, 0.55, 0)
):

    try:

        ymin, xmin, ymax, xmax = [
            max(
                0,
                min(
                    1000,
                    int(v)
                )
            )
            for v in box
        ]

        x = (
            page_width
            * xmax
            / 1000
        ) + 5

        y = (
            page_height
            * ymin
            / 1000
        )

        page.draw_polyline(
            [
                fitz.Point(
                    x,
                    y + 6
                ),
                fitz.Point(
                    x + 5,
                    y + 12
                ),
                fitz.Point(
                    x + 15,
                    y
                )
            ],
            color=color,
            width=2.8
        )

    except Exception as e:

        print(
            "TICK ERROR:",
            e
        )


# ============================================================
# PAGE IMAGE
# ============================================================

def _page_rgb_image(
    page,
    dpi=72
):

    pix = page.get_pixmap(
        dpi=dpi,
        alpha=False
    )

    return Image.frombytes(
        "RGB",
        [pix.width, pix.height],
        pix.samples
    )


def _dark_ratio(
    crop
):

    gray = crop.convert(
        "L"
    )

    gray.thumbnail(
        (180, 180)
    )

    pixels = list(
        gray.getdata()
    )

    if not pixels:
        return 1.0

    dark = sum(
        1
        for value in pixels
        if value < 235
    )

    return (
        dark
        / len(pixels)
    )


# ============================================================
# BLANK SPACE SEARCH
# ============================================================

def find_blank_comment_rect(
    page,
    desired_w,
    desired_h,
    anchor_box,
    occupied,
    placement_box=None
):
    """
    Search for a place where a comment can be written.

    IMPORTANT:
    This function is only a preference finder.
    It is NEVER allowed to cause comment skipping.
    """

    try:

        image = _page_rgb_image(
            page,
            dpi=72
        )

    except Exception:

        return None

    iw, ih = image.size

    sx = (
        page.rect.width
        / iw
    )

    sy = (
        page.rect.height
        / ih
    )

    rw = max(
        40,
        int(
            desired_w
            / sx
        )
    )

    rh = max(
        35,
        int(
            desired_h
            / sy
        )
    )

    rw = min(
        rw,
        int(iw * 0.34)
    )

    rh = min(
        rh,
        int(ih * 0.25)
    )

    preferred = None

    if placement_box:

        try:

            py1, px1, py2, px2 = [
                max(
                    0,
                    min(
                        1000,
                        int(v)
                    )
                )
                for v in placement_box
            ]

            preferred = (
                int(
                    iw
                    * px1
                    / 1000
                ),
                int(
                    ih
                    * py1
                    / 1000
                ),
                int(
                    iw
                    * px2
                    / 1000
                ),
                int(
                    ih
                    * py2
                    / 1000
                )
            )

        except Exception:

            preferred = None

    try:

        ymin, xmin, ymax, xmax = anchor_box

        ax = int(
            iw
            * (
                (xmin + xmax)
                / 2
            )
            / 1000
        )

        ay = int(
            ih
            * (
                (ymin + ymax)
                / 2
            )
            / 1000
        )

    except Exception:

        ax = iw // 2
        ay = ih // 2

    occupied_px = []

    for old in occupied:

        occupied_px.append(
            (
                int(
                    old.x0
                    / sx
                ),
                int(
                    old.y0
                    / sy
                ),
                int(
                    old.x1
                    / sx
                ),
                int(
                    old.y1
                    / sy
                )
            )
        )

    def overlaps_old(
        x,
        y
    ):

        for ox1, oy1, ox2, oy2 in occupied_px:

            if not (
                x + rw <= ox1
                or x >= ox2
                or y + rh <= oy1
                or y >= oy2
            ):

                return True

        return False

    def valid_blank(
        x,
        y
    ):

        if x < 2 or y < 2:
            return False

        if (
            x + rw
            >= iw - 2
        ):
            return False

        if (
            y + rh
            >= ih - 2
        ):
            return False

        if overlaps_old(
            x,
            y
        ):
            return False

        crop = image.crop(
            (
                x,
                y,
                x + rw,
                y + rh
            )
        )

        # RELAXED:
        # shadow/noise/faint scan marks do not automatically
        # make the region invalid.
        return _dark_ratio(
            crop
        ) <= 0.12

    candidates = []

    # --------------------------------------------------------
    # PREFERRED MODEL PLACEMENT
    # --------------------------------------------------------

    if preferred:

        px1, py1, px2, py2 = preferred

        px1 = max(
            0,
            min(
                iw - rw,
                px1
            )
        )

        py1 = max(
            0,
            min(
                ih - rh,
                py1
            )
        )

        px2 = min(
            iw,
            max(
                px1 + rw,
                px2
            )
        )

        py2 = min(
            ih,
            max(
                py1 + rh,
                py2
            )
        )

        step_x = max(
            20,
            rw // 5
        )

        step_y = max(
            20,
            rh // 5
        )

        y = py1

        while y <= max(
            py1,
            py2 - rh
        ):

            x = px1

            while x <= max(
                px1,
                px2 - rw
            ):

                candidates.append(
                    (
                        x,
                        y,
                        0
                    )
                )

                x += step_x

            y += step_y

    # --------------------------------------------------------
    # MARGINS
    # --------------------------------------------------------

    for y in range(
        8,
        max(
            9,
            ih - rh - 8
        ),
        max(
            20,
            rh // 4
        )
    ):

        candidates.append(
            (
                8,
                y,
                1
            )
        )

        candidates.append(
            (
                max(
                    8,
                    iw - rw - 8
                ),
                y,
                1
            )
        )

    for x in range(
        8,
        max(
            9,
            iw - rw - 8
        ),
        max(
            20,
            rw // 5
        )
    ):

        candidates.append(
            (
                x,
                8,
                2
            )
        )

        candidates.append(
            (
                x,
                max(
                    8,
                    ih - rh - 8
                ),
                2
            )
        )

    # --------------------------------------------------------
    # GENERAL GRID
    # --------------------------------------------------------

    for y in range(
        8,
        max(
            9,
            ih - rh - 8
        ),
        max(
            30,
            rh // 3
        )
    ):

        for x in range(
            8,
            max(
                9,
                iw - rw - 8
            ),
            max(
                30,
                rw // 4
            )
        ):

            candidates.append(
                (
                    x,
                    y,
                    3
                )
            )

    best = None

    for x, y, priority in candidates:

        if not valid_blank(
            x,
            y
        ):
            continue

        distance = (
            (
                x
                + rw / 2
                - ax
            ) ** 2
            +
            (
                y
                + rh / 2
                - ay
            ) ** 2
        ) ** 0.5

        score = (
            priority * 10000
            + distance
        )

        if (
            best is None
            or score < best[0]
        ):

            best = (
                score,
                x,
                y
            )

    if best is None:
        return None

    _, x, y = best

    return fitz.Rect(
        x * sx,
        y * sy,
        (x + rw) * sx,
        (y + rh) * sy
    )


# ============================================================
# FORCE COMMENT RECT
# ============================================================

def force_comment_rect(
    page,
    desired_w,
    desired_h,
    occupied
):
    """
    ABSOLUTE FALLBACK.

    This function guarantees a rectangle.
    It does not return None.
    """

    page_width = page.rect.width
    page_height = page.rect.height

    w = min(
        desired_w,
        page_width * 0.30
    )

    h = min(
        desired_h,
        page_height * 0.20
    )

    candidates = [

        # right upper
        fitz.Rect(
            page_width * 0.68,
            8,
            page_width - 5,
            8 + h
        ),

        # left upper
        fitz.Rect(
            5,
            8,
            min(
                page_width * 0.32,
                5 + w
            ),
            8 + h
        ),

        # right middle
        fitz.Rect(
            page_width * 0.68,
            page_height * 0.35,
            page_width - 5,
            min(
                page_height - 5,
                page_height * 0.35 + h
            )
        ),

        # left middle
        fitz.Rect(
            5,
            page_height * 0.35,
            min(
                page_width * 0.32,
                5 + w
            ),
            min(
                page_height - 5,
                page_height * 0.35 + h
            )
        ),

        # bottom
        fitz.Rect(
            page_width * 0.34,
            max(
                5,
                page_height - h - 5
            ),
            min(
                page_width - 5,
                page_width * 0.34 + w
            ),
            page_height - 5
        )
    ]

    # Prefer candidate with least overlap.
    best = None

    for rect in candidates:

        overlap_area = 0

        for old in occupied:

            inter = rect & old

            if not inter.is_empty:

                overlap_area += (
                    inter.width
                    * inter.height
                )

        score = overlap_area

        if (
            best is None
            or score < best[0]
        ):

            best = (
                score,
                rect
            )

    if best:

        return best[1]

    # FINAL absolute rectangle
    return fitz.Rect(
        5,
        5,
        min(
            page_width - 5,
            5 + w
        ),
        min(
            page_height - 5,
            5 + h
        )
    )


# ============================================================
# PLACE COMMENT
# ============================================================

def place_comment(
    page,
    text,
    anchor_box,
    placement_box,
    page_width,
    page_height,
    occupied,
    color="red"
):

    text = str(
        text
    ).strip()

    if not text:
        return

    png = make_comment_badge(
        text,
        width=1600,
        font_size=92,
        color=color
    )

    badge_image = Image.open(
        io.BytesIO(png)
    )

    img_w, img_h = (
        badge_image.size
    )

    desired_w = min(
        page_width * 0.31,
        250
    )

    desired_h = (
        desired_w
        * img_h
        / img_w
    )

    desired_h = min(
        desired_h,
        page_height * 0.25
    )

    # --------------------------------------------------------
    # 1. REAL BLANK SPACE
    # --------------------------------------------------------

    chosen_rect = None

    try:

        chosen_rect = find_blank_comment_rect(
            page,
            desired_w,
            desired_h,
            anchor_box,
            occupied,
            placement_box
        )

    except Exception as e:

        print(
            "BLANK SEARCH ERROR:",
            e
        )

    # --------------------------------------------------------
    # 2. PROGRESSIVELY RELAX SIZE
    # --------------------------------------------------------

    if chosen_rect is None:

        for scale in (
            0.90,
            0.80,
            0.70,
            0.60,
            0.50
        ):

            try:

                chosen_rect = (
                    find_blank_comment_rect(
                        page,
                        desired_w * scale,
                        desired_h * scale,
                        anchor_box,
                        occupied,
                        placement_box
                    )
                )

            except Exception as e:

                print(
                    "RELAXED BLANK ERROR:",
                    e
                )

                chosen_rect = None

            if chosen_rect is not None:
                break

    # --------------------------------------------------------
    # 3. ABSOLUTE FALLBACK — NEVER SKIP
    # --------------------------------------------------------

    if chosen_rect is None:

        chosen_rect = force_comment_rect(
            page,
            desired_w,
            desired_h,
            occupied
        )

        print(
            "FORCED COMMENT PLACEMENT:",
            color,
            text[:80]
        )

    # --------------------------------------------------------
    # 4. INSERT COMMENT
    # --------------------------------------------------------

    page.insert_image(
        chosen_rect,
        stream=png,
        keep_proportion=True,
        overlay=True
    )

    # --------------------------------------------------------
    # 5. ARROW
    # --------------------------------------------------------

    try:

        ymin, xmin, ymax, xmax = anchor_box

        anchor_x = (
            (xmin + xmax)
            / 2
            / 1000
            * page_width
        )

        anchor_y = (
            (ymin + ymax)
            / 2
            / 1000
            * page_height
        )

        if anchor_x < chosen_rect.x0:

            start_x = chosen_rect.x0

        elif anchor_x > chosen_rect.x1:

            start_x = chosen_rect.x1

        else:

            start_x = (
                chosen_rect.x0
                + chosen_rect.width / 2
            )

        if anchor_y < chosen_rect.y0:

            start_y = chosen_rect.y0

        elif anchor_y > chosen_rect.y1:

            start_y = chosen_rect.y1

        else:

            start_y = (
                chosen_rect.y0
                + chosen_rect.height / 2
            )

        distance = (
            (
                start_x
                - anchor_x
            ) ** 2
            +
            (
                start_y
                - anchor_y
            ) ** 2
        ) ** 0.5

        if distance <= page_width * 0.70:

            draw_arrow(
                page,
                start_x,
                start_y,
                anchor_x,
                anchor_y
            )

    except Exception as e:

        print(
            "ARROW ERROR:",
            e
        )

    occupied.append(
        chosen_rect
    )

    print(
        f"COMMENT RENDERED: "
        f"{color.upper()} | "
        f"{text[:100]}"
    )


# ============================================================
# ANNOTATE PDF
# ============================================================

def annotate_pdf(
    pdf,
    result
):

    page_annotations = {}

    for annotation in result.get(
        "annotations",
        []
    ):

        try:

            page_number = int(
                annotation.get(
                    "page",
                    1
                )
            )

        except Exception:

            continue

        page_annotations.setdefault(
            page_number,
            []
        ).append(
            annotation
        )

    marks_by_page = {}

    for question in result.get(
        "questions",
        []
    ):

        marks_by_page.setdefault(
            question["end_page"],
            []
        ).append(
            question
        )

    # --------------------------------------------------------
    # EACH PAGE
    # --------------------------------------------------------

    for page_index, page in enumerate(
        pdf
    ):

        page_number = (
            page_index + 1
        )

        page_width = (
            page.rect.width
        )

        page_height = (
            page.rect.height
        )

        occupied = []

        # ----------------------------------------------------
        # FIRST PAGE SCORE
        # ----------------------------------------------------

        if page_number == 1:

            score_png = make_score_badge(
                result[
                    "total_obtained_marks"
                ],
                result[
                    "total_max_marks"
                ]
            )

            score_rect = fitz.Rect(
                8,
                12,
                min(
                    110,
                    page_width * 0.16
                ),
                min(
                    114,
                    page_height * 0.12
                )
            )

            page.insert_image(
                score_rect,
                stream=score_png
            )

        # ----------------------------------------------------
        # RED / GREEN CHECKING SIGNS
        # ----------------------------------------------------

        for annotation in page_annotations.get(
            page_number,
            []
        )[:8]:

            box = annotation.get(
                "box_2d",
                [0, 0, 0, 0]
            )

            annotation_type = str(
                annotation.get(
                    "type",
                    "good"
                )
            ).lower()

            if annotation_type == "wrong":

                add_circle(
                    page,
                    box,
                    page_width,
                    page_height
                )

            else:

                add_tick(
                    page,
                    box,
                    page_width,
                    page_height,
                    color=(0, 0.55, 0)
                )

        # ----------------------------------------------------
        # PAGE COMMENTS
        # ----------------------------------------------------

        comments = [
            item
            for item in result.get(
                "page_comments",
                []
            )
            if int(
                item.get(
                    "page",
                    0
                ) or 0
            ) == page_number
        ]

        # Full page = 4 preferred.
        # Half page = Gemini decides and should return 2-3.
        # We never silently discard valid comments.
        for comment in comments:

            text = str(
                comment.get(
                    "comment",
                    ""
                )
            ).strip()

            if not text:
                continue

            anchor = comment.get(
                "anchor",
                [500, 450, 550, 550]
            )

            placement_box = comment.get(
                "placement_box",
                [50, 700, 300, 995]
            )

            color = str(
                comment.get(
                    "color",
                    "red"
                )
            ).lower().strip()

            if color not in (
                "red",
                "green"
            ):
                color = "red"

            try:

                place_comment(
                    page,
                    text,
                    anchor,
                    placement_box,
                    page_width,
                    page_height,
                    occupied,
                    color=color
                )

            except Exception as e:

                print(
                    "COMMENT ERROR:",
                    e
                )

                # --------------------------------------------
                # EMERGENCY RENDER
                # --------------------------------------------

                try:

                    emergency_png = (
                        make_comment_badge(
                            text,
                            width=1300,
                            font_size=72,
                            color=color
                        )
                    )

                    emergency_rect = (
                        force_comment_rect(
                            page,
                            page_width * 0.25,
                            page_height * 0.14,
                            occupied
                        )
                    )

                    page.insert_image(
                        emergency_rect,
                        stream=emergency_png,
                        keep_proportion=True,
                        overlay=True
                    )

                    occupied.append(
                        emergency_rect
                    )

                    print(
                        "EMERGENCY COMMENT RENDERED"
                    )

                except Exception as emergency_error:

                    print(
                        "EMERGENCY COMMENT ERROR:",
                        emergency_error
                    )

        # ----------------------------------------------------
        # QUESTION MARKS
        # ----------------------------------------------------

        questions = marks_by_page.get(
            page_number,
            []
        )

        if questions:

            y = (
                page_height
                - 48
            )

            for question in reversed(
                questions
            ):

                marks_png = make_marks_badge(
                    question[
                        "question_number"
                    ],
                    question[
                        "obtained_marks"
                    ],
                    question[
                        "max_marks"
                    ]
                )

                marks_rect = fitz.Rect(
                    page_width - 120,
                    y - 30,
                    page_width - 5,
                    y
                )

                page.insert_image(
                    marks_rect,
                    stream=marks_png,
                    keep_proportion=True
                )

                y -= 36

        # ----------------------------------------------------
        # END PAGE QUESTION COMMENT
        # ----------------------------------------------------

        for question in questions:

            text = question.get(
                "end_page_comment",
                ""
            ).strip()

            if not text:
                continue

            already = any(
                str(
                    c.get(
                        "comment",
                        ""
                    )
                ).strip()
                == text
                for c in comments
            )

            if already:
                continue

            try:

                place_comment(
                    page,
                    text,
                    [800, 450, 930, 900],
                    [700, 700, 995, 995],
                    page_width,
                    page_height,
                    occupied,
                    color="red"
                )

            except Exception as e:

                print(
                    "END COMMENT ERROR:",
                    e
                )

                # Never let this stop PDF generation.

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    output = io.BytesIO()

    pdf.save(
        output,
        garbage=4,
        deflate=True
    )

    pdf.close()

    output.seek(0)

    return output


# ============================================================
# PROCESS SUBMISSION
# ============================================================

def process_submission(
    path,
    paper
):

    extension = (
        Path(path)
        .suffix
        .lower()
    )

    if extension == ".pdf":

        pdf = fitz.open(
            path
        )

        images = image_pages_from_pdf(
            pdf
        )

    else:

        image = Image.open(
            path
        ).convert(
            "RGB"
        )

        buffer = io.BytesIO()

        image.save(
            buffer,
            "JPEG",
            quality=88
        )

        image_bytes = (
            buffer.getvalue()
        )

        pdf = fitz.open()

        page = pdf.new_page(
            width=image.width,
            height=image.height
        )

        page.insert_image(
            page.rect,
            stream=image_bytes
        )

        images = [
            image_bytes
        ]

    if not images:

        raise Exception(
            "कोई page नहीं मिला।"
        )

    result = call_gemini(
        images,
        paper
    )

    final_pdf = annotate_pdf(
        pdf,
        result
    )

    return (
        final_pdf,
        result
    )


# ============================================================
# TELEGRAM BOT
# ============================================================

if bot:

    @bot.message_handler(
        commands=[
            "start",
            "help"
        ]
    )
    def welcome(message):

        bot.reply_to(
            message,

            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "अपनी answer copy की PDF/फोटो भेजें।\n"
            "Copy receive होने के तुरंत बाद "
            "Paper Name पूछा जाएगा।"
        )


    @bot.message_handler(
        content_types=[
            "document",
            "photo"
        ]
    )
    def receive_copy(message):

        try:

            if message.content_type == "document":

                file_info = bot.get_file(
                    message.document.file_id
                )

                data = bot.download_file(
                    file_info.file_path
                )

                filename = (
                    message.document.file_name
                    or "submission.pdf"
                )

                suffix = (
                    Path(filename)
                    .suffix
                    .lower()
                    or ".bin"
                )

            else:

                file_info = bot.get_file(
                    message.photo[-1].file_id
                )

                data = bot.download_file(
                    file_info.file_path
                )

                filename = "submission.jpg"

                suffix = ".jpg"

            # Replace older unanswered submission.
            old = PENDING.pop(
                message.chat.id,
                None
            )

            if old:

                try:

                    os.remove(
                        old["path"]
                    )

                except Exception:
                    pass

            path = save_submission(
                data,
                suffix
            )

            PENDING[
                message.chat.id
            ] = {
                "path": path,
                "filename": filename
            }

            ask_paper(
                message
            )

        except Exception as e:

            bot.reply_to(
                message,

                "⚠️ कॉपी receive नहीं हो सकी:\n"
                f"{str(e)[:180]}"
            )


    @bot.message_handler(
        content_types=[
            "text"
        ]
    )
    def paper_reply(message):

        chat_id = (
            message.chat.id
        )

        if chat_id not in PENDING:
            return

        paper = normalize_paper(
            message.text.strip()
        )

        if not paper:

            bot.reply_to(
                message,

                "❗ Paper पहचान नहीं पाया।\n\n"
                "केवल <b>GS 1</b>, <b>GS 2</b>, "
                "<b>GS 3</b>, <b>GS 4</b>, "
                "<b>GS 5</b> या <b>GS 6</b> भेजें।"
            )

            return

        item = PENDING.pop(
            chat_id
        )

        status = bot.reply_to(
            message,

            f"⏳ <b>{paper} selected.</b>\n\n"
            "अब copy का page-by-page evaluation "
            "और examiner-style checking शुरू हो रही है..."
        )

        try:

            final_pdf, result = (
                process_submission(
                    item["path"],
                    paper
                )
            )

            try:

                os.remove(
                    item["path"]
                )

            except Exception:
                pass

            try:

                bot.delete_message(
                    chat_id,
                    status.message_id
                )

            except Exception:
                pass

            # ------------------------------------------------
            # SHORT TELEGRAM CAPTION
            # ------------------------------------------------

            feedback = str(
                result.get(
                    "overall_feedback",
                    ""
                )
            ).strip()

            caption = (
                f"🏛️ <b>PRANA PCS — {paper} Evaluation</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>प्राप्तांक:</b> "
                f"<code>"
                f"{result['total_obtained_marks']:g} / "
                f"{result['total_max_marks']:g}"
                f"</code>\n\n"
                f"{feedback}"
            )

            # Telegram caption safety.
            caption = caption[:900]

            # ------------------------------------------------
            # ORIGINAL NAME + _Evaluated
            # ------------------------------------------------

            original_name = item.get(
                "filename",
                "submission.pdf"
            )

            original_stem = (
                Path(original_name).stem
                or "submission"
            )

            evaluated_filename = (
                f"{original_stem}_Evaluated.pdf"
            )

            bot.send_document(
                chat_id,
                final_pdf,
                visible_file_name=evaluated_filename,
                caption=caption
            )

        except Exception as e:

            try:

                os.remove(
                    item["path"]
                )

            except Exception:
                pass

            try:

                bot.edit_message_text(
                    (
                        "⚠️ <b>मूल्यांकन में समस्या</b>\n\n"
                        f"{str(e)[:300]}"
                    ),
                    chat_id=chat_id,
                    message_id=status.message_id
                )

            except Exception:

                bot.send_message(
                    chat_id,

                    "⚠️ मूल्यांकन में समस्या:\n"
                    f"{str(e)[:300]}"
                )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )
