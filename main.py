
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


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://uppcs-ai-evaluator.onrender.com"
).rstrip("/")

app = FastAPI()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

FONT_PATH = "/tmp/NotoSansDevanagari-Regular.ttf"
FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/"
    "ofl/notosansdevanagari/"
    "NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf"
)

# chat_id -> pending uploaded copy
PENDING = {}

MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash"
]

RUBRICS = {
    "GS1": """
GS1: History-Art-Culture, Geography, Indian Society.
Focus: multidimensional analysis, chronology/context, historians/quotes,
maps and diagrams for geography, society data/reports, contemporary linkage.
Value addition: maps, timeline, diagrams, data, case studies, thinkers,
cultural examples. Avoid generic essay-like writing.
""",
    "GS2": """
GS2: Constitution, Polity, Governance, Social Justice, International Relations.
Focus: Articles, amendments, Supreme Court judgments, committees/ARC,
constitutional morality, government efforts, balanced challenges/solutions.
For IR use strategic/diplomatic dimensions and relevant maps. Differences
should preferably be tabular/T-format. Way Forward is important.
""",
    "GS3": """
GS3: Economy, Agriculture, Science-Tech, Environment, Disaster Management,
Internal Security. Use 3D: Data + Diagram + Dynamics. Look for Economic Survey,
Budget, NITI/official reports, policy names, technical applications,
disaster-cycle, climate mitigation/adaptation, security maps and institutions.
Generic statements should score poorly.
""",
    "GS4": """
GS4: Ethics, Integrity, Aptitude. Theory must be applied. Look for precise
ethical definitions, thinkers/quotes, keywords, real administrative/personal
examples, ethical dilemmas, stakeholder analysis, EI, constitutional morality,
good governance. Case studies: stakeholders -> dilemmas -> options ->
pros/cons -> balanced decision -> implementation.
""",
    "GS5": """
GS5: Uttar Pradesh-specific History, Culture, Polity, Governance, Security,
Education, Health, Tourism. Hyper-localization is central. Look for UP
districts, UP schemes/portals, UP-specific data, regional divisions
(Purvanchal, Bundelkhand, Western UP, Awadh), UP maps, ODOP, local culture,
Nepal-border districts and UP security institutions.
Generic all-India answers should score poorly when UP specificity is required.
""",
    "GS6": """
GS6: Uttar Pradesh Economy, Agriculture, Geography, Environment, Science-Tech,
Infrastructure. Focus on UP Budget/Economic Survey, data, UP maps, regional/
sectoral analysis, 9 agro-climatic zones, minerals, expressways, defence
corridor, Ramsar/tiger reserves, UP policies and infrastructure. Generic
answers without UP data/policy/map should be below average.
"""
}


def ensure_font():
    try:
        if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 10000:
            return True

        r = requests.get(FONT_URL, timeout=25)
        r.raise_for_status()

        with open(FONT_PATH, "wb") as f:
            f.write(r.content)

        return os.path.getsize(FONT_PATH) > 10000

    except Exception as e:
        print("FONT ERROR:", e)
        return False


ensure_font()


def font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def normalize_paper(text):
    t = text.upper().replace("-", "").replace("_", "").replace(" ", "")

    if "GS1" in t or "जीएस1" in text.replace(" ", ""):
        return "GS1"
    if "GS2" in t or "जीएस2" in text.replace(" ", ""):
        return "GS2"
    if "GS3" in t or "जीएस3" in text.replace(" ", ""):
        return "GS3"
    if "GS4" in t or "जीएस4" in text.replace(" ", ""):
        return "GS4"
    if "GS5" in t or "जीएस5" in text.replace(" ", ""):
        return "GS5"
    if "GS6" in t or "जीएस6" in text.replace(" ", ""):
        return "GS6"

    return None


def save_submission(data, suffix=".bin"):
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


@app.on_event("startup")
def startup():
    ensure_font()

    if bot:
        try:
            bot.remove_webhook()
            bot.set_webhook(
                url=f"{RENDER_EXTERNAL_URL}/webhook"
            )
        except Exception as e:
            print("WEBHOOK ERROR:", e)


@app.get("/")
def home():
    return {
        "status": "PRANA PCS AI Evaluator Active",
        "font": os.path.exists(FONT_PATH)
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


def build_prompt(paper, total_pages):

    return f"""
आप PRANA PCS के वरिष्ठ UPPCS Mains examiner हैं।

Paper: {paper}
Total pages: {total_pages}

{RUBRICS[paper]}

============================================================
MARKING RULES
============================================================

1. यदि उत्तर 2 pages में है:
   Max Marks = 8
   Obtained Marks HARD CAP = 5.5

2. यदि उत्तर 3 pages में है:
   Max Marks = 12
   Obtained Marks HARD CAP = 8.5

3. Question के answer का वास्तविक अंतिम page ही end_page होगा।

4. Question के marks केवल उसी end_page पर लगाए जाएंगे।

5. Marks को केवल page count देखकर नहीं दें।
   Content quality, relevance, analysis, facts, structure,
   value addition और paper-specific rubric के आधार पर दें।

6. 8 marks question में 5.5 से ऊपर कभी न दें।

7. 12 marks question में 8.5 से ऊपर कभी न दें।

============================================================
EXAMINER STYLE
============================================================

Evaluation ऐसा दिखना चाहिए जैसे किसी अनुभवी examiner ने
उत्तर पुस्तिका पर लाल पेन से checking की हो।

इसलिए:

✓ अच्छे तथ्य/उदाहरण/डेटा/argument पर red tick लगाएँ।
✓ जहाँ सामग्री पर्याप्त हो वहाँ प्रत्येक page पर सामान्यतः 3-4 substantive comments दें।
✓ गलत तथ्य, गलत terminology, inappropriate wording,
  unsupported claim या स्पष्ट conceptual error पर red circle लगाएँ।
✓ जहाँ सुधार आवश्यक है वहाँ छोटा लेकिन substantive examiner comment दें।
✓ केवल 3-5 शब्द के generic comments न दें।
✓ प्रत्येक page की comments कुल लगभग 15-40 शब्द की हों।
✓ Comments answer के relevant हिस्से के पास लगाने योग्य हों।
✓ अनावश्यक रूप से हर लाइन पर annotation न करें।
✓ Comments बड़े लाल text में सीधे margin में हों; कोई box, card, sticker या background न हो।
✓ केवल महत्वपूर्ण और वास्तविक mistakes/high-value points चुनें।

============================================================
WRONG WORD / INAPPROPRIATE WORD
============================================================

यदि कोई गलत/अनुचित शब्द या phrase दिखे:

type = "wrong"

exact_text = वही दिखने वाला शब्द/phrase

reason = संक्षेप में समस्या

box_2d = उस शब्द/phrase की location

उस location पर final PDF में लाल oval/circle बनेगा।

यदि कोई अच्छा point/fact/data/example दिखे:

type = "good"

उस location पर लाल tick बनेगा।

अस्पष्ट handwriting को अनुमान से गलत न मानें।

============================================================
BOUNDING BOX
============================================================

हर bbox:

[ymin, xmin, ymax, xmax]

format में दें।

सभी values 0-1000 normalized हों।

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
      "end_page_comment":
      "यहाँ 15-40 शब्द की substantive examiner टिप्पणी हो।"
    }}
  ],

  "page_comments": [
    {{
      "page": 1,
      "comment":
      "यहाँ 15-40 शब्द की substantive टिप्पणी हो।",
      "side": "right",
      "anchor": [400, 500, 550, 800]
    }}
  ],

  "annotations": [
    {{
      "page": 1,
      "type": "wrong",
      "exact_text": "गलत या अनुचित शब्द",
      "reason": "क्यों गलत/अनुचित है",
      "box_2d": [400, 500, 450, 650]
    }},
    {{
      "page": 1,
      "type": "good",
      "exact_text": "अच्छा तथ्य",
      "box_2d": [600, 300, 650, 450]
    }}
  ],

  "overall_feedback":
  "समग्र मूल्यांकन",

  "improvements": [
    "सुधार सुझाव 1",
    "सुधार सुझाव 2"
  ]
}}

महत्वपूर्ण:
- Page numbers 1-based हैं।
- Question end_page वास्तविक answer ending के आधार पर दें।
- गलत शब्द के आसपास circle लगाने योग्य tight bbox दें।
- Good point पर tick लगाने योग्य bbox दें।
"""


def call_gemini(images, paper):

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
            "temperature": 0.2
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
                timeout=180
            )

            if response.status_code == 200:

                raw = (
                    response
                    .json()
                    ["candidates"][0]
                    ["content"]["parts"][0]
                    ["text"]
                )

                return normalize_result(
                    json.loads(raw),
                    len(images)
                )

            last_error = (
                f"{model}: HTTP "
                f"{response.status_code} "
                f"{response.text[:200]}"
            )

            if response.status_code not in (
                429,
                500,
                502,
                503,
                504
            ):
                break

        except Exception as e:
            last_error = str(e)

    raise Exception(
        "Gemini evaluation failed: "
        + last_error
    )


def normalize_result(data, pages):

    questions = []

    for index, question in enumerate(
        data.get("questions", [])
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

    return {
        "total_obtained_marks":
            total_obtained,

        "total_max_marks":
            total_max,

        "questions":
            questions,

        "page_comments":
            data.get(
                "page_comments",
                []
            ),

        "annotations":
            data.get(
                "annotations",
                []
            ),

        "overall_feedback":
            str(
                data.get(
                    "overall_feedback",
                    ""
                )
            ),

        "improvements":
            [
                str(x)
                for x in data.get(
                    "improvements",
                    []
                )
            ][:6]
    }


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


def make_comment_badge(
    text,
    width=1500,
    font_size=58
):
    """
    Drishti-style examiner comment:
    No box, no background, large red text.
    """
    fnt = font(font_size)
    padding = 8

    temp = Image.new("RGBA", (width, 1600), (255, 255, 255, 0))
    draw = ImageDraw.Draw(temp)

    lines = wrap_text(draw, text, fnt, width - 2 * padding)

    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=fnt)
        line_heights.append(bbox[3] - bbox[1])

    line_gap = 14
    height = max(
        110,
        sum(line_heights) + line_gap * max(0, len(lines) - 1)
        + 2 * padding
    )

    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    y = padding
    for line, line_height in zip(lines, line_heights):
        draw.text(
            (padding, y),
            line,
            font=fnt,
            fill=(145, 0, 0, 255)
        )
        y += line_height + line_gap

    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


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
            (size - (
                bbox[2] - bbox[0]
            )) // 2,
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
            (size - (
                bbox[2] - bbox[0]
            )) // 2,
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
            (width - (
                bbox[2] - bbox[0]
            )) // 2,
            (height - (
                bbox[3] - bbox[1]
            )) // 2
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
        width=1.7
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


def add_circle(
    page,
    box,
    page_width,
    page_height
):

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

    return rect


def add_tick(
    page,
    box,
    page_width,
    page_height
):

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
        color=(0.65, 0, 0),
        width=2.4
    )


def place_comment(
    page,
    text,
    anchor_box,
    side,
    page_width,
    page_height,
    occupied
):
    """
    Drishti-style margin annotation:
    large red text directly on the page, no box/card.
    """
    png = make_comment_badge(
        text,
        width=1500,
        font_size=58
    )

    box_width = min(210, page_width * 0.29)

    words = len(str(text).split())
    line_count = max(1, (words + 5) // 6)
    box_height = min(175, max(72, 42 * line_count))

    if side == "left":
        x1 = 4
        x2 = x1 + box_width
    else:
        x2 = page_width - 4
        x1 = x2 - box_width

    anchor_y = anchor_box[0] / 1000 * page_height

    preferred = [
        anchor_y - box_height / 2,
        anchor_y - 105,
        anchor_y + 105,
        anchor_y - 210,
        anchor_y + 210
    ]

    chosen = None
    for candidate in preferred:
        candidate = max(
            8,
            min(page_height - box_height - 8, candidate)
        )

        rect = fitz.Rect(
            x1, candidate,
            x2, candidate + box_height
        )

        if all(not rect.intersects(old) for old in occupied):
            chosen = candidate
            break

    if chosen is None:
        chosen = max(
            8,
            min(
                page_height - box_height - 8,
                anchor_y - box_height / 2
            )
        )

    rect = fitz.Rect(
        x1, chosen,
        x2, chosen + box_height
    )

    page.insert_image(
        rect,
        stream=png,
        keep_proportion=True,
        overlay=True
    )

    ymin, xmin, ymax, xmax = anchor_box

    anchor_x = (
        (xmin + xmax) / 2 / 1000 * page_width
    )
    anchor_y = (
        (ymin + ymax) / 2 / 1000 * page_height
    )

    start_x = x2 if side == "left" else x1
    start_y = chosen + box_height / 2

    draw_arrow(
        page,
        start_x,
        start_y,
        anchor_x,
        anchor_y
    )

    occupied.append(rect)


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

    for question in result["questions"]:

        marks_by_page.setdefault(
            question["end_page"],
            []
        ).append(
            question
        )

    for page_index, page in enumerate(
        pdf
    ):

        page_number = (
            page_index + 1
        )

        page_width = page.rect.width
        page_height = page.rect.height

        occupied = []

        # ----------------------------------------------------
        # FIRST PAGE SCORE CIRCLE
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
                stream=score_png,
                keep_proportion=True
            )

        # ----------------------------------------------------
        # WRONG / GOOD MARKS
        # ----------------------------------------------------

        for annotation in page_annotations.get(
            page_number,
            []
        )[:6]:

            box = annotation.get(
                "box_2d",
                [0, 0, 0, 0]
            )

            if annotation.get(
                "type"
            ) == "wrong":

                add_circle(
                    page,
                    box,
                    page_width,
                    page_height
                )

            elif annotation.get(
                "type"
            ) == "good":

                add_tick(
                    page,
                    box,
                    page_width,
                    page_height
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

        for comment in comments[:4]:

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

            side = comment.get(
                "side",
                "right"
            )

            place_comment(
                page,
                text,
                anchor,
                side,
                page_width,
                page_height,
                occupied
            )

        # ----------------------------------------------------
        # QUESTION END MARKS
        # ----------------------------------------------------

        questions = marks_by_page.get(
            page_number,
            []
        )

        if questions:

            y = page_height - 48

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
        # QUESTION END COMMENT
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

            place_comment(
                page,
                text,
                [800, 450, 930, 900],
                "right",
                page_width,
                page_height,
                occupied
            )

    output = io.BytesIO()

    pdf.save(
        output,
        garbage=4,
        deflate=True
    )

    pdf.close()

    output.seek(0)

    return output


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

        pdf = fitz.open(path)

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

    return final_pdf, result


# ============================================================
# TELEGRAM
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

            # Replace an older unanswered submission.
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

            improvements = "\n".join(
                "• " + x
                for x in result[
                    "improvements"
                ]
            )

            if not improvements:
                improvements = (
                    "• कोई अतिरिक्त सुझाव नहीं।"
                )

            caption = (
                f"🏛️ <b>PRANA PCS — {paper} Evaluation</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>प्राप्तांक:</b> "
                f"<code>"
                f"{result['total_obtained_marks']:g}"
                f" / "
                f"{result['total_max_marks']:g}"
                f"</code>\n\n"
                f"📝 <b>समग्र समीक्षा:</b>\n"
                f"{result['overall_feedback']}\n\n"
                f"💡 <b>सुधार:</b>\n"
                f"{improvements}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Examiner-style evaluated copy 👇</i>"
            )

            bot.send_document(
                chat_id,
                final_pdf,
                visible_file_name=(
                    "Evaluated_Copy_PranaPCS.pdf"
                ),
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
