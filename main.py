import os
import io
import json
import base64
import tempfile
import sqlite3
from datetime import datetime, timezone
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

# ============================================================
# ACCESS / SUBMISSION DATABASE
# ============================================================
# Phase 1: manual access control.
# You can manually activate a user/group from the Admin API/panel later.
# Payment fields are already present in the schema so a gateway can be
# connected without redesigning the database.
DB_PATH = os.getenv("DB_PATH", "/tmp/prana_evaluator.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            access_type TEXT NOT NULL DEFAULT 'none',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_group_id INTEGER UNIQUE NOT NULL,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL,
            plan TEXT NOT NULL DEFAULT 'manual',
            payment_status TEXT NOT NULL DEFAULT 'manual',
            payment_id TEXT,
            amount REAL DEFAULT 0,
            started_at TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_uuid TEXT UNIQUE NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            paper TEXT,
            language TEXT,
            original_filename TEXT,
            evaluated_filename TEXT,
            obtained_marks REAL,
            max_marks REAL,
            status TEXT NOT NULL DEFAULT 'received',
            overall_feedback TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS submission_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            question_number INTEGER,
            start_page INTEGER,
            end_page INTEGER,
            pages_used INTEGER,
            max_marks REAL,
            obtained_marks REAL,
            demand_parts TEXT,
            fulfilled_parts TEXT,
            skipped_parts TEXT,
            end_page_comment TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper TEXT NOT NULL,
            language TEXT NOT NULL,
            question TEXT NOT NULL,
            model_answer TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def upsert_user(tg_user):
    conn = db()
    ts = now_iso()
    conn.execute("""
        INSERT INTO users
        (telegram_user_id, username, first_name, last_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            updated_at=excluded.updated_at
    """, (
        tg_user.id,
        tg_user.username,
        tg_user.first_name,
        tg_user.last_name,
        ts,
        ts
    ))
    conn.commit()
    conn.close()


def user_has_access(telegram_user_id):
    conn = db()
    row = conn.execute("""
        SELECT status, access_type
        FROM users
        WHERE telegram_user_id = ?
    """, (telegram_user_id,)).fetchone()

    if row and row["status"] == "active":
        conn.close()
        return True

    # Group access is checked separately later when group membership is
    # explicitly synchronized/approved by the admin.
    conn.close()
    return False


def create_submission_record(
    telegram_user_id,
    paper,
    language,
    original_filename,
    evaluated_filename=None
):
    import uuid

    sid = str(uuid.uuid4())
    conn = db()
    cur = conn.execute("""
        INSERT INTO submissions
        (submission_uuid, telegram_user_id, paper, language,
         original_filename, evaluated_filename, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        sid,
        telegram_user_id,
        paper,
        language,
        original_filename,
        evaluated_filename,
        now_iso()
    ))
    submission_id = cur.lastrowid
    conn.commit()
    conn.close()
    return submission_id, sid


def complete_submission(
    submission_id,
    result,
    evaluated_filename
):
    conn = db()
    conn.execute("""
        UPDATE submissions
        SET evaluated_filename = ?,
            obtained_marks = ?,
            max_marks = ?,
            status = 'completed',
            overall_feedback = ?,
            completed_at = ?
        WHERE id = ?
    """, (
        evaluated_filename,
        result.get("total_obtained_marks", 0),
        result.get("total_max_marks", 0),
        result.get("overall_feedback", ""),
        now_iso(),
        submission_id
    ))

    for q in result.get("questions", []):
        conn.execute("""
            INSERT INTO submission_questions
            (submission_id, question_number, start_page, end_page,
             pages_used, max_marks, obtained_marks,
             demand_parts, fulfilled_parts, skipped_parts,
             end_page_comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            submission_id,
            q.get("question_number"),
            q.get("start_page"),
            q.get("end_page"),
            q.get("pages_used"),
            q.get("max_marks"),
            q.get("obtained_marks"),
            json.dumps(q.get("demand_parts", []), ensure_ascii=False),
            json.dumps(q.get("fulfilled_parts", []), ensure_ascii=False),
            json.dumps(q.get("skipped_parts", []), ensure_ascii=False),
            q.get("end_page_comment", "")
        ))

    conn.commit()
    conn.close()


def init_database():
    try:
        init_db()
        print("DATABASE READY:", DB_PATH)
    except Exception as e:
        print("DATABASE INIT ERROR:", e)


init_database()


FONT_PATH = "/tmp/Kalam-Regular.ttf"
FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/"
    "ofl/kalam/Kalam-Regular.ttf"
)

PENDING = {}  # chat_id -> temporary uploaded copy metadata

# Current production-ready Gemini models.
# Preferred models are only a ranking. At runtime we ask Gemini which
# models are actually available to THIS API key and which support
# generateContent. This prevents 404 failures from retired/unavailable
# model IDs.
PREFERRED_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
]

MODEL_CACHE = {
    "models": [],
    "expires_at": 0
}

RUBRICS = {
    "GS1": """GS1: History-Art-Culture, Geography, Indian Society.
Multidimensional analysis, chronology/context, historians/quotes, maps and
diagrams for geography, society data/reports, contemporary linkage.
Value addition: maps, timeline, diagrams, data, case studies, thinkers,
cultural examples. Avoid generic essay-like writing.""",

    "GS2": """GS2: Constitution, Polity, Governance, Social Justice, IR.
Look for Articles, amendments, Supreme Court judgments, committees/ARC,
constitutional morality, government efforts, balanced challenges/solutions.
For IR use strategic/diplomatic dimensions and relevant maps. Differences
should preferably be T-format. Way Forward is important.""",

    "GS3": """GS3: Economy, Agriculture, Science-Tech, Environment, Disaster
Management, Internal Security. Use Data + Diagram + Dynamics. Look for
Budget/Economic Survey/NITI/official reports, policy names, applications,
disaster-cycle, mitigation/adaptation, security maps and institutions.
Generic statements should score poorly.""",

    "GS4": """GS4: Ethics, Integrity, Aptitude. Theory must be applied.
Look for precise definitions, thinkers/quotes, ethical keywords, real
administrative/personal examples, dilemmas, stakeholders, EI, constitutional
morality and good governance. Case studies: stakeholders -> dilemmas ->
options -> pros/cons -> balanced decision -> implementation.""",

    "GS5": """GS5: Uttar Pradesh-specific History, Culture, Polity, Governance,
Security, Education, Health, Tourism. Hyper-localization is central.
Look for UP districts, UP schemes/portals, UP data, regional divisions,
UP maps, ODOP, local culture, Nepal-border districts and UP institutions.
Generic all-India answers should score poorly when UP specificity is required.""",

    "GS6": """GS6: Uttar Pradesh Economy, Agriculture, Geography, Environment,
Science-Tech and Infrastructure. Focus on UP Budget/Economic Survey, data,
UP maps, regional/sectoral analysis, 9 agro-climatic zones, minerals,
expressways, defence corridor, Ramsar/tiger reserves, UP policies and
infrastructure. Generic answers without UP data/policy/map should be below
average."""
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
    clean = text.replace(" ", "")
    for n in range(1, 7):
        if f"GS{n}" in t or f"जीएस{n}" in clean:
            return f"GS{n}"
    return None


def save_submission(data, suffix=".bin"):
    fd, path = tempfile.mkstemp(prefix="prana_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def ask_paper(message):
    bot.reply_to(
        message,
        "📚 <b>कॉपी प्राप्त हो गई है।</b>\n\n"
        "मूल्यांकन शुरू करने से पहले <b>Paper Name</b> भेजें:\n\n"
        "• GS 1\n• GS 2\n• GS 3\n• GS 4\n• GS 5\n• GS 6\n\n"
        "उदाहरण: <b>GS 3</b>"
    )



def require_admin(request: Request):
    if not ADMIN_TOKEN:
        raise Exception("ADMIN_TOKEN is not configured.")

    supplied = request.headers.get("X-Admin-Token", "")
    if supplied != ADMIN_TOKEN:
        raise PermissionError("Invalid admin token.")


@app.get("/api/access/users")
async def api_users(request: Request):
    require_admin(request)
    conn = db()
    rows = conn.execute("""
        SELECT * FROM users
        ORDER BY updated_at DESC
    """).fetchall()
    conn.close()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/access/user/{telegram_user_id}/grant")
async def api_grant_user(telegram_user_id: int, request: Request):
    require_admin(request)
    body = await request.json()
    access_type = str(body.get("access_type", "manual"))
    conn = db()
    ts = now_iso()
    conn.execute("""
        INSERT INTO users
        (telegram_user_id, status, access_type, created_at, updated_at)
        VALUES (?, 'active', ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            status='active',
            access_type=excluded.access_type,
            updated_at=excluded.updated_at
    """, (telegram_user_id, access_type, ts, ts))
    conn.commit()
    conn.close()
    return {"ok": True, "telegram_user_id": telegram_user_id, "status": "active"}


@app.post("/api/access/user/{telegram_user_id}/revoke")
async def api_revoke_user(telegram_user_id: int, request: Request):
    require_admin(request)
    conn = db()
    conn.execute("""
        UPDATE users
        SET status='blocked', updated_at=?
        WHERE telegram_user_id=?
    """, (now_iso(), telegram_user_id))
    conn.commit()
    conn.close()
    return {"ok": True, "telegram_user_id": telegram_user_id, "status": "blocked"}


@app.post("/api/access/group/{telegram_group_id}/grant")
async def api_grant_group(telegram_group_id: int, request: Request):
    require_admin(request)
    body = await request.json()
    title = str(body.get("title", ""))
    conn = db()
    ts = now_iso()
    conn.execute("""
        INSERT INTO groups
        (telegram_group_id, title, status, created_at, updated_at)
        VALUES (?, ?, 'active', ?, ?)
        ON CONFLICT(telegram_group_id) DO UPDATE SET
            title=excluded.title,
            status='active',
            updated_at=excluded.updated_at
    """, (telegram_group_id, title, ts, ts))
    conn.commit()
    conn.close()
    return {"ok": True, "telegram_group_id": telegram_group_id, "status": "active"}


@app.post("/api/access/group/{telegram_group_id}/revoke")
async def api_revoke_group(telegram_group_id: int, request: Request):
    require_admin(request)
    conn = db()
    conn.execute("""
        UPDATE groups
        SET status='blocked', updated_at=?
        WHERE telegram_group_id=?
    """, (now_iso(), telegram_group_id))
    conn.commit()
    conn.close()
    return {"ok": True, "telegram_group_id": telegram_group_id, "status": "blocked"}


@app.get("/api/stats/overview")
async def api_stats(request: Request):
    require_admin(request)
    conn = db()

    users = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE status='active'"
    ).fetchone()["n"]

    submissions = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions"
    ).fetchone()["n"]

    completed = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE status='completed'"
    ).fetchone()["n"]

    avg = conn.execute("""
        SELECT AVG(obtained_marks * 100.0 / NULLIF(max_marks, 0)) AS pct
        FROM submissions
        WHERE status='completed'
    """).fetchone()["pct"]

    by_paper = conn.execute("""
        SELECT paper,
               COUNT(*) AS submissions,
               ROUND(AVG(obtained_marks * 100.0 /
                         NULLIF(max_marks, 0)), 2) AS avg_percentage
        FROM submissions
        WHERE status='completed'
        GROUP BY paper
        ORDER BY paper
    """).fetchall()

    conn.close()

    return {
        "active_users": users,
        "total_submissions": submissions,
        "completed_submissions": completed,
        "average_percentage": round(avg, 2) if avg is not None else 0,
        "by_paper": [dict(x) for x in by_paper]
    }


@app.post("/api/content/daily-question")
async def api_daily_question(request: Request):
    require_admin(request)
    body = await request.json()

    paper = str(body.get("paper", "")).strip().upper()
    language = str(body.get("language", "hi")).strip().lower()
    question = str(body.get("question", "")).strip()
    model_answer = str(body.get("model_answer", "")).strip()

    if paper not in {"GS1", "GS2", "GS3", "GS4", "GS5", "GS6"}:
        raise ValueError("Invalid paper.")
    if language not in {"hi", "en"}:
        raise ValueError("Language must be hi or en.")
    if not question:
        raise ValueError("Question is required.")

    conn = db()
    cur = conn.execute("""
        INSERT INTO daily_questions
        (paper, language, question, model_answer, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (paper, language, question, model_answer, now_iso()))
    conn.commit()
    item_id = cur.lastrowid
    conn.close()

    return {"ok": True, "id": item_id}

@app.on_event("startup")
def startup():
    init_database()
    ensure_font()
    if bot:
        try:
            bot.remove_webhook()
            bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/webhook")
        except Exception as e:
            print("WEBHOOK ERROR:", e)


@app.get("/")
def home():
    try:
        available_models = get_available_gemini_models()
        model_status = available_models[:8]
    except Exception as e:
        model_status = [f"discovery-error: {str(e)[:120]}"]

    return {
        "status": "PRANA PCS AI Evaluator Active",
        "font": os.path.exists(FONT_PATH),
        "engine": "bilingual-answer-language-v9-dynamic-models",
        "gemini_models": model_status
    }


@app.get("/api/model-status")
def model_status():
    try:
        models = get_available_gemini_models(force_refresh=True)
        return {
            "ok": True,
            "generate_content_models": models[:30]
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e)[:1000]
        }


@app.post("/webhook")
async def webhook(request: Request):
    if bot:
        data = await request.json()
        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])
    return {"ok": True}


def image_pages_from_pdf(pdf):
    pages = []
    for page in pdf:
        pix = page.get_pixmap(dpi=120, alpha=False)
        pages.append(pix.tobytes("jpeg", jpg_quality=88))
    return pages


def build_prompt(paper, total_pages):
    return f"""
आप PRANA PCS के वरिष्ठ UPPCS Mains examiner हैं।

Paper: {paper}
Total pages: {total_pages}

{RUBRICS[paper]}

============================================================
LANGUAGE RULE — ABSOLUTE
============================================================

UI language और evaluation language अलग हैं।

सबसे पहले answer की वास्तविक writing language detect करें।

- मुख्य उत्तर हिन्दी में है → answer_language = "Hindi"
- मुख्य उत्तर English में है → answer_language = "English"
- Mixed answer में जिस भाषा में substantive answer अधिक है, वही चुनें।

Question की भाषा, Telegram/Mini-App की भाषा या user preference देखकर
evaluation language तय न करें।

पूरे examiner comments, missing-point comments, overall feedback और
improvement comments answer_language में ही लिखें।

Output में:
"answer_language": "Hindi" या "English"

============================================================
MARKING
============================================================

यदि उत्तर 2 pages में है:
Max Marks = 8
Obtained HARD CAP = 5.5

यदि उत्तर 3 या अधिक pages में है:
Max Marks = 12
Obtained HARD CAP = 8.5

Question का वास्तविक अंतिम answer page ही end_page है।
Question के marks केवल end_page पर लगेंगे।

केवल page count देखकर marks न दें। Content quality, relevance,
question demand, analysis, facts, structure, value addition और
paper-specific rubric देखकर marks दें।

8-mark question कभी 5.5 से ऊपर नहीं।
12-mark question कभी 8.5 से ऊपर नहीं।

============================================================
QUESTION-DEMAND ENGINE
============================================================

हर question को पहले command-word और sub-demands में तोड़ें।

उदाहरण:
- कारण + उपाय = causes + measures
- तुलना = both sides + explicit comparison
- चर्चा = dimensions + balanced analysis
- मूल्यांकन = merits + limitations + judgement
- प्रभाव + समाधान = impacts + solutions
- महत्व + चुनौतियां = significance + challenges

हर question में:
1. सभी demanded components पहचानें।
2. fulfilled parts पहचानें।
3. partial parts पहचानें।
4. skipped parts पहचानें।
5. देखें कि introduction/body/conclusion actual demand को satisfy करते हैं।
6. केवल topic coverage को demand fulfilment न मानें।

यदि कोई demanded part छूटा है तो marks घटाएँ और संबंधित खाली margin में
स्पष्ट comment दें:
"यहाँ प्रश्न की मांग का ______ भाग छूट गया है; ______ अपेक्षित था।"

यदि point अधूरा है:
"यहाँ ______ को तथ्य/उदाहरण/विश्लेषण के साथ विकसित करना चाहिए था।"

============================================================
EXAMINER MARKS
============================================================

हर FULL page:
- 4-6 checking signs
- सामान्यतः 3-4 substantive comments

HALF PAGE या उससे कम:
- 2-3 checking signs
- 2-3 substantive comments

Signs:
- अच्छे point/fact/data/example/structure पर red tick
- गलत तथ्य/गलत terminology/inappropriate wording/conceptual error पर
  red circle
- गलत शब्द अनुमान से न बनाएं।

हर page के 4 comments में, यदि वास्तविक सुधार की गुंजाइश है, तो कम से कम
2 constructive होने चाहिए:
- क्या छूटा?
- क्या data/example जोड़ना चाहिए?
- कौन-सा analysis कमजोर है?
- question demand का कौन-सा हिस्सा अधूरा है?
- presentation कैसे बेहतर हो सकती थी?

सिर्फ "अच्छा", "सही", "बेहतरीन" जैसे छोटे comments पर्याप्त नहीं हैं।

हर substantive comment 15-40 शब्द का हो।

यदि कोई वास्तविक गलती नहीं है तो गलती invent न करें।

============================================================
PLACEMENT
============================================================

Comments बड़े handwritten Kalam-style red text में हों।
कोई box/card/sticker/background नहीं।

Comment लिखे हुए text, diagram या existing annotation के ऊपर नहीं होना चाहिए।

Gemini placement_box केवल preference है। Python बाद में ORIGINAL page पर
blank-space detection करके final safe location तय करेगा।

Priority:
1. खाली left/right margin
2. खाली upper/lower area
3. अन्य genuinely blank white area

जरूरत होने पर comment से संबंधित answer point तक पतला red arrow दें।

============================================================
OVERALL FEEDBACK
============================================================

"overall_feedback" केवल 4-5 छोटी lines का एक paragraph हो।

इसमें:
- भाषा एवं अभिव्यक्ति
- उत्तर की शैली/संरचना
- प्रस्तुतीकरण
- analysis/value addition
- आगे सुधार की आशावादी दिशा

का संतुलित उल्लेख हो।

कोई heading, bullet list, marks repeat या अलग suggestions list नहीं।

============================================================
OUTPUT — ONLY VALID JSON
============================================================

{{
  "answer_language": "Hindi",
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
      "demand_parts": ["भाग 1", "भाग 2"],
      "fulfilled_parts": ["भाग 1"],
      "skipped_parts": ["भाग 2"],
      "end_page_comment": "15-40 शब्द की substantive examiner टिप्पणी उसी भाषा में।"
    }}
  ],
  "page_comments": [
    {{
      "page": 1,
      "comment": "15-40 शब्द की substantive examiner टिप्पणी।",
      "placement_box": [50,700,300,995],
      "anchor": [400,500,550,800]
    }}
  ],
  "annotations": [
    {{
      "page": 1,
      "type": "wrong",
      "exact_text": "गलत शब्द",
      "reason": "क्यों गलत है",
      "box_2d": [400,500,450,650]
    }},
    {{
      "page": 1,
      "type": "good",
      "exact_text": "अच्छा तथ्य",
      "box_2d": [600,300,650,450]
    }}
  ],
  "overall_feedback": "4-5 lines का समग्र feedback।",
  "improvements": []
}}

अनिवार्य:
- हर full page पर 4-6 annotations/signs.
- half-page पर 2-3.
- full page पर 3-4 page_comments.
- half-page पर 2-3.
- हर page_comment का placement_box अनिवार्य।
- हर question में demand_parts, fulfilled_parts, skipped_parts।
- Missing demand पर actionable comment।
- comments 15-40 शब्द के substantive examiner remarks हों।
- comments केवल वास्तविक खाली जगह में लगेंगे।
"""


def get_available_gemini_models(force_refresh=False):
    """
    Discover models available to the current GEMINI_API_KEY.

    Google exposes GET /v1beta/models and returns the supported actions for
    each model. We only select models that support generateContent. This is
    deliberately dynamic so a deprecated/region/account-specific model does
    not break the evaluator.
    """
    import time

    now = time.time()
    if (
        not force_refresh
        and MODEL_CACHE["models"]
        and MODEL_CACHE["expires_at"] > now
    ):
        return MODEL_CACHE["models"]

    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY is missing.")

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models"
    )

    response = requests.get(
        url,
        params={"key": GEMINI_API_KEY, "pageSize": 1000},
        timeout=30
    )

    if response.status_code != 200:
        raise Exception(
            "Gemini model discovery failed: "
            f"HTTP {response.status_code} "
            f"{response.text[:300]}"
        )

    payload = response.json()
    available = []

    for item in payload.get("models", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue

        model_id = name.split("/", 1)[-1]

        methods = item.get("supportedGenerationMethods", [])
        methods = [str(x) for x in methods]

        # Only models that can process the standard generateContent request.
        if "generateContent" not in methods:
            continue

        available.append(model_id)

    # Rank known preferred models first, then keep any other compatible
    # generateContent model as a final emergency fallback.
    ranked = []
    for preferred in PREFERRED_MODELS:
        if preferred in available and preferred not in ranked:
            ranked.append(preferred)

    for model_id in available:
        if model_id not in ranked:
            ranked.append(model_id)

    MODEL_CACHE["models"] = ranked
    MODEL_CACHE["expires_at"] = now + 900  # 15 minutes

    print("AVAILABLE GEMINI MODELS:", ranked[:20])
    return ranked


def invalidate_model_cache():
    MODEL_CACHE["models"] = []
    MODEL_CACHE["expires_at"] = 0


def call_gemini(images, paper):
    parts = []

    for image_bytes in images:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image_bytes).decode()
            }
        })

    parts.append({
        "text": build_prompt(
            paper,
            len(images)
        )
    })

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

    # First attempt: dynamically discover models available to this API key.
    try:
        models = get_available_gemini_models()
    except Exception as discovery_error:
        # If discovery itself fails, use only modern known IDs.
        # This fallback is intentionally free of gemini-2.5-flash.
        print("MODEL DISCOVERY ERROR:", discovery_error)
        models = list(PREFERRED_MODELS)

    if not models:
        raise Exception(
            "इस Gemini API key के लिए generateContent वाला कोई "
            "available model नहीं मिला।"
        )

    errors = []

    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
            f"?key={GEMINI_API_KEY}"
        )

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=240
            )

            if response.status_code == 200:
                body = response.json()

                try:
                    raw = (
                        body["candidates"][0]
                        ["content"]["parts"][0]["text"]
                    )
                except (KeyError, IndexError, TypeError) as e:
                    raise Exception(
                        f"{model}: unexpected Gemini response: "
                        f"{str(e)}"
                    )

                return normalize_result(
                    json.loads(raw),
                    len(images)
                )

            error_text = response.text[:500]
            errors.append(
                f"{model}: HTTP {response.status_code} {error_text}"
            )

            # 404/400 for one model means this particular model is not
            # usable for this key/request. Continue to the next discovered
            # compatible model instead of aborting the whole evaluation.
            if response.status_code in (
                400, 404, 409, 429, 500, 502, 503, 504
            ):
                continue

            # Authentication/permission errors are worth surfacing, but
            # continue once in case the API has model-specific permissions.
            if response.status_code in (401, 403):
                continue

        except requests.RequestException as e:
            errors.append(f"{model}: network error: {str(e)[:250]}")
            continue

        except json.JSONDecodeError as e:
            errors.append(
                f"{model}: invalid JSON returned by model: {str(e)}"
            )
            continue

        except Exception as e:
            errors.append(f"{model}: {str(e)[:300]}")
            continue

    # One fresh discovery pass can recover from a model being retired while
    # the cache is still warm.
    invalidate_model_cache()

    try:
        fresh_models = get_available_gemini_models(force_refresh=True)
    except Exception:
        fresh_models = []

    already_tried = set(models)

    for model in fresh_models:
        if model in already_tried:
            continue

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
            f"?key={GEMINI_API_KEY}"
        )

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=240
            )

            if response.status_code == 200:
                raw = (
                    response.json()["candidates"][0]
                    ["content"]["parts"][0]["text"]
                )
                return normalize_result(
                    json.loads(raw),
                    len(images)
                )

            errors.append(
                f"{model}: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )

        except Exception as e:
            errors.append(f"{model}: {str(e)[:300]}")

    # Keep the error readable in Telegram while preserving enough detail
    # to diagnose an account/model access issue.
    summary = " | ".join(errors[-6:])
    raise Exception(
        "Gemini evaluation failed after trying all available models. "
        + summary[:1800]
    )


def normalize_result(data, pages):
    questions = []

    for index, q in enumerate(data.get("questions", [])):
        if not isinstance(q, dict):
            continue

        try:
            start_page = int(q.get("start_page", 1))
            end_page = int(q.get("end_page", start_page))
        except Exception:
            start_page, end_page = 1, 1

        start_page = max(1, min(pages, start_page))
        end_page = max(start_page, min(pages, end_page))

        pages_used = end_page - start_page + 1

        if pages_used <= 2:
            max_marks, hard_cap = 8, 5.5
        else:
            max_marks, hard_cap = 12, 8.5

        try:
            obtained = float(q.get("obtained_marks", 0))
        except Exception:
            obtained = 0

        obtained = max(0, min(obtained, hard_cap))

        questions.append({
            "question_number": int(q.get("question_number", index + 1)),
            "start_page": start_page,
            "end_page": end_page,
            "pages_used": pages_used,
            "max_marks": max_marks,
            "obtained_marks": round(obtained, 1),
            "demand_parts": [str(x) for x in q.get("demand_parts", [])],
            "fulfilled_parts": [str(x) for x in q.get("fulfilled_parts", [])],
            "skipped_parts": [str(x) for x in q.get("skipped_parts", [])],
            "end_page_comment": str(q.get("end_page_comment", "")).strip()
        })

    total_obtained = round(sum(q["obtained_marks"] for q in questions), 1)
    total_max = round(sum(q["max_marks"] for q in questions), 1)

    language = str(data.get("answer_language", "Hindi")).strip()
    if language.lower() not in ("hindi", "english"):
        language = "Hindi"

    return {
        "answer_language": language,
        "total_obtained_marks": total_obtained,
        "total_max_marks": total_max,
        "questions": questions,
        "page_comments": data.get("page_comments", []),
        "annotations": data.get("annotations", []),
        "overall_feedback": str(data.get("overall_feedback", "")).strip(),
        "improvements": [str(x) for x in data.get("improvements", [])][:6]
    }


def wrap_text(draw, text, fnt, max_width):
    words = str(text).split()
    lines, current = [], ""

    for word in words:
        test = word if not current else current + " " + word
        bbox = draw.textbbox((0, 0), test, font=fnt)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines or [""]


def make_comment_badge(text, width=1600, font_size=92):
    # Transparent image: red handwritten-style text only.
    fnt = font(font_size)
    padding = 8

    temp = Image.new("RGBA", (width, 1600), (255, 255, 255, 0))
    draw = ImageDraw.Draw(temp)

    lines = wrap_text(draw, text, fnt, width - 2 * padding)
    heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=fnt)
        heights.append(bbox[3] - bbox[1])

    gap = 14
    height = max(110, sum(heights) + gap * max(0, len(lines) - 1) + 2 * padding)

    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    y = padding
    for line, h in zip(lines, heights):
        draw.text((padding, y), line, font=fnt, fill=(145, 0, 0, 255))
        y += h + gap

    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def make_score_badge(obtained, total):
    size = 900
    image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse((12, 12, size - 12, size - 12),
                 fill=(255, 250, 250),
                 outline=(170, 0, 0),
                 width=14)

    title_font = font(62)
    score_font = font(96)

    title = "प्राप्तांक"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((size - (bbox[2]-bbox[0]))//2, 150),
              title, font=title_font, fill=(160, 0, 0))

    score = f"{obtained:g} / {total:g}"
    bbox = draw.textbbox((0, 0), score, font=score_font)
    draw.text(((size - (bbox[2]-bbox[0]))//2, 340),
              score, font=score_font, fill=(160, 0, 0))

    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def make_marks_badge(question_number, obtained, total):
    fnt = font(46)
    text = f"Q{question_number}   {obtained:g}/{total:g}"
    width, height = 650, 150

    image = Image.new("RGB", (width, height), (255, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, width-6, height-6),
                           radius=18, outline=(170, 0, 0), width=7)

    bbox = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((width-(bbox[2]-bbox[0]))//2,
               (height-(bbox[3]-bbox[1]))//2),
              text, font=fnt, fill=(160, 0, 0))

    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def draw_arrow(page, x1, y1, x2, y2):
    page.draw_line(fitz.Point(x1, y1), fitz.Point(x2, y2),
                   color=(0.65, 0, 0), width=1.4)

    dx, dy = x1-x2, y1-y2
    length = max((dx*dx + dy*dy) ** 0.5, 1)
    ux, uy = dx/length, dy/length

    p = fitz.Point(x2 + ux*8, y2 + uy*8)
    q = fitz.Point(x2 - uy*6 + ux*8, y2 + ux*6 + uy*8)
    r = fitz.Point(x2 + uy*6 + ux*8, y2 - ux*6 + uy*8)

    page.draw_polyline([p, q, r, p],
                       color=(0.65, 0, 0),
                       fill=(0.65, 0, 0))


def add_circle(page, box, page_width, page_height):
    ymin, xmin, ymax, xmax = [max(0, min(1000, int(v))) for v in box]

    x1 = page_width * xmin / 1000
    x2 = page_width * xmax / 1000
    y1 = page_height * ymin / 1000
    y2 = page_height * ymax / 1000

    pad_x = max(3, (x2-x1)*0.10)
    pad_y = max(3, (y2-y1)*0.25)

    rect = fitz.Rect(max(0, x1-pad_x), max(0, y1-pad_y),
                     min(page_width, x2+pad_x), min(page_height, y2+pad_y))
    page.draw_oval(rect, color=(0.65, 0, 0), width=2.2)


def add_tick(page, box, page_width, page_height):
    ymin, xmin, ymax, xmax = [max(0, min(1000, int(v))) for v in box]

    x = page_width * xmax / 1000 + 5
    y = page_height * ymin / 1000

    page.draw_polyline([
        fitz.Point(x, y+6),
        fitz.Point(x+5, y+12),
        fitz.Point(x+15, y)
    ], color=(0.65, 0, 0), width=2.4)


def _page_rgb_image(page, dpi=72):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _dark_ratio(crop):
    gray = crop.convert("L")
    gray.thumbnail((180, 180))
    pixels = list(gray.getdata())
    if not pixels:
        return 1.0
    return sum(1 for value in pixels if value < 235) / len(pixels)


def find_blank_comment_rect(page, desired_w, desired_h, anchor_box,
                            occupied, placement_box=None):
    """
    Safe placement:
    - checks the ORIGINAL page pixels
    - avoids dark/text regions
    - avoids previously placed comments
    - prefers margins/edges
    """
    try:
        image = _page_rgb_image(page, dpi=72)
    except Exception:
        return None

    iw, ih = image.size
    sx = page.rect.width / iw
    sy = page.rect.height / ih

    rw = max(50, int(desired_w / sx))
    rh = max(40, int(desired_h / sy))

    # Do not make comments microscopic.
    rw = min(rw, int(iw * 0.36))
    rh = min(rh, int(ih * 0.26))

    preferred = None
    if placement_box:
        try:
            py1, px1, py2, px2 = [max(0, min(1000, int(v)))
                                  for v in placement_box]
            preferred = (int(iw*px1/1000), int(ih*py1/1000),
                         int(iw*px2/1000), int(ih*py2/1000))
        except Exception:
            pass

    try:
        ymin, xmin, ymax, xmax = anchor_box
        ax = int(iw*((xmin+xmax)/2)/1000)
        ay = int(ih*((ymin+ymax)/2)/1000)
    except Exception:
        ax, ay = iw//2, ih//2

    occupied_px = [
        (int(r.x0/sx), int(r.y0/sy), int(r.x1/sx), int(r.y1/sy))
        for r in occupied
    ]

    def overlaps_old(x, y):
        for ox1, oy1, ox2, oy2 in occupied_px:
            if not (x+rw <= ox1 or x >= ox2 or y+rh <= oy1 or y >= oy2):
                return True
        return False

    def valid_blank(x, y):
        if x < 2 or y < 2 or x+rw >= iw-2 or y+rh >= ih-2:
            return False
        if overlaps_old(x, y):
            return False
        crop = image.crop((x, y, x+rw, y+rh))
        # Strict white-space requirement.
        return _dark_ratio(crop) <= 0.028

    candidates = []

    # Preferred box first, but only if actually blank.
    if preferred:
        px1, py1, px2, py2 = preferred
        px1 = max(0, min(iw-rw, px1))
        py1 = max(0, min(ih-rh, py1))
        px2 = min(iw, max(px1+rw, px2))
        py2 = min(ih, max(py1+rh, py2))

        for y in range(py1, max(py1+1, py2-rh+1), max(18, rh//5)):
            for x in range(px1, max(px1+1, px2-rw+1), max(18, rw//5)):
                candidates.append((x, y, 0))

    # Strong preference for side margins / empty edges.
    for y in range(8, max(9, ih-rh-8), max(18, rh//5)):
        candidates.append((8, y, 1))
        candidates.append((max(8, iw-rw-8), y, 1))

    for x in range(8, max(9, iw-rw-8), max(18, rw//5)):
        candidates.append((x, 8, 1))
        candidates.append((x, max(8, ih-rh-8), 1))

    # General fallback.
    for y in range(8, max(9, ih-rh-8), max(25, rh//3)):
        for x in range(8, max(9, iw-rw-8), max(25, rw//4)):
            candidates.append((x, y, 2))

    best = None
    for x, y, priority in candidates:
        if not valid_blank(x, y):
            continue

        distance = ((x+rw/2-ax)**2 + (y+rh/2-ay)**2) ** 0.5
        score = priority * 100000 + distance

        if best is None or score < best[0]:
            best = (score, x, y)

    if best is None:
        return None

    _, x, y = best
    return fitz.Rect(x*sx, y*sy, (x+rw)*sx, (y+rh)*sy)


def place_comment(page, text, anchor_box, placement_box,
                  page_width, page_height, occupied):
    if not text.strip():
        return

    # Large Kalam text. No box/background.
    png = make_comment_badge(text, width=1600, font_size=92)
    badge = Image.open(io.BytesIO(png))
    img_w, img_h = badge.size

    desired_w = min(page_width * 0.31, 250)
    desired_h = min(desired_w * img_h / img_w, page_height * 0.25)

    chosen = None
    for scale in (1.0, 0.90, 0.80, 0.70, 0.60):
        try:
            chosen = find_blank_comment_rect(
                page, desired_w*scale, desired_h*scale,
                anchor_box, occupied, placement_box
            )
        except Exception as e:
            print("BLANK DETECTION ERROR:", e)
            chosen = None
        if chosen:
            break

    # Never put a comment over answer text.
    if chosen is None:
        print("SAFE ANNOTATION SKIPPED: no blank area found")
        return

    page.insert_image(chosen, stream=png, keep_proportion=True, overlay=True)

    try:
        ymin, xmin, ymax, xmax = anchor_box
        anchor_x = ((xmin+xmax)/2)/1000*page_width
        anchor_y = ((ymin+ymax)/2)/1000*page_height

        if anchor_x < chosen.x0:
            start_x = chosen.x0
        elif anchor_x > chosen.x1:
            start_x = chosen.x1
        else:
            start_x = chosen.x0 + chosen.width/2

        if anchor_y < chosen.y0:
            start_y = chosen.y0
        elif anchor_y > chosen.y1:
            start_y = chosen.y1
        else:
            start_y = chosen.y0 + chosen.height/2

        if ((start_x-anchor_x)**2 + (start_y-anchor_y)**2) ** 0.5 <= page_width*0.55:
            draw_arrow(page, start_x, start_y, anchor_x, anchor_y)
    except Exception:
        pass

    occupied.append(chosen)


def annotate_pdf(pdf, result):
    page_annotations = {}
    for a in result.get("annotations", []):
        try:
            p = int(a.get("page", 1))
        except Exception:
            continue
        page_annotations.setdefault(p, []).append(a)

    marks_by_page = {}
    for q in result["questions"]:
        marks_by_page.setdefault(q["end_page"], []).append(q)

    comments_by_page = {}
    for c in result.get("page_comments", []):
        try:
            p = int(c.get("page", 0))
        except Exception:
            continue
        comments_by_page.setdefault(p, []).append(c)

    for page_index, page in enumerate(pdf):
        page_number = page_index + 1
        pw, ph = page.rect.width, page.rect.height
        occupied = []

        # First page: score circle on left side.
        if page_number == 1:
            score_png = make_score_badge(
                result["total_obtained_marks"],
                result["total_max_marks"]
            )
            score_rect = fitz.Rect(
                8, 12,
                min(110, pw*0.16),
                min(114, ph*0.12)
            )
            page.insert_image(score_rect, stream=score_png, keep_proportion=True)

        # Red circles/ticks. Up to 6, as requested.
        for a in page_annotations.get(page_number, [])[:6]:
            box = a.get("box_2d", [0,0,0,0])
            if a.get("type") == "wrong":
                add_circle(page, box, pw, ph)
            elif a.get("type") == "good":
                add_tick(page, box, pw, ph)

        # 3-4 large substantive comments per full page.
        comments = comments_by_page.get(page_number, [])
        for c in comments[:4]:
            text = str(c.get("comment", "")).strip()
            if not text:
                continue
            place_comment(
                page,
                text,
                c.get("anchor", [500,450,550,550]),
                c.get("placement_box", [50,700,300,995]),
                pw, ph, occupied
            )

        # Marks ONLY where the question ends.
        questions = marks_by_page.get(page_number, [])
        if questions:
            y = ph - 48
            for q in reversed(questions):
                png = make_marks_badge(
                    q["question_number"],
                    q["obtained_marks"],
                    q["max_marks"]
                )
                rect = fitz.Rect(pw-120, y-30, pw-5, y)
                page.insert_image(rect, stream=png, keep_proportion=True)
                y -= 36

        # End-page comment for question, if not already a page comment.
        for q in questions:
            text = q.get("end_page_comment", "").strip()
            if not text:
                continue

            duplicate = any(
                str(c.get("comment","")).strip() == text
                for c in comments
            )
            if duplicate:
                continue

            place_comment(
                page, text,
                [800,450,930,900],
                [700,700,995,995],
                pw, ph, occupied
            )

    output = io.BytesIO()
    pdf.save(output, garbage=4, deflate=True)
    pdf.close()
    output.seek(0)
    return output


def process_submission(path, paper):
    extension = Path(path).suffix.lower()

    if extension == ".pdf":
        pdf = fitz.open(path)
        images = image_pages_from_pdf(pdf)
    else:
        image = Image.open(path).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88)
        image_bytes = buffer.getvalue()

        pdf = fitz.open()
        page = pdf.new_page(width=image.width, height=image.height)
        page.insert_image(page.rect, stream=image_bytes)
        images = [image_bytes]

    if not images:
        raise Exception("कोई page नहीं मिला।")

    result = call_gemini(images, paper)
    final_pdf = annotate_pdf(pdf, result)
    return final_pdf, result


if bot:

    @bot.message_handler(commands=["start", "help"])
    def welcome(message):
        bot.reply_to(
            message,
            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "अपनी answer copy की PDF/फोटो भेजें।\n"
            "Copy receive होने के तुरंत बाद Paper Name पूछा जाएगा।"
        )

    @bot.message_handler(content_types=["document", "photo"])
    def receive_copy(message):
        try:
            if message.content_type == "document":
                info = bot.get_file(message.document.file_id)
                data = bot.download_file(info.file_path)
                filename = message.document.file_name or "submission.pdf"
                suffix = Path(filename).suffix.lower() or ".bin"
            else:
                info = bot.get_file(message.photo[-1].file_id)
                data = bot.download_file(info.file_path)
                filename = "submission.jpg"
                suffix = ".jpg"

            old = PENDING.pop(message.chat.id, None)
            if old:
                try:
                    os.remove(old["path"])
                except Exception:
                    pass

            path = save_submission(data, suffix)
            PENDING[message.chat.id] = {
                "path": path,
                "filename": filename
            }
            ask_paper(message)

        except Exception as e:
            bot.reply_to(
                message,
                "⚠️ कॉपी receive नहीं हो सकी:\n" + str(e)[:180]
            )

    @bot.message_handler(content_types=["text"])
    def paper_reply(message):
        chat_id = message.chat.id

        if chat_id not in PENDING:
            return

        paper = normalize_paper(message.text.strip())

        if not paper:
            bot.reply_to(
                message,
                "❗ Paper पहचान नहीं पाया।\n\n"
                "केवल <b>GS 1</b>, <b>GS 2</b>, <b>GS 3</b>, "
                "<b>GS 4</b>, <b>GS 5</b> या <b>GS 6</b> भेजें।"
            )
            return

        item = PENDING.pop(chat_id)

        status = bot.reply_to(
            message,
            f"⏳ <b>{paper} selected.</b>\n\n"
            "अब copy का page-by-page evaluation और "
            "examiner-style checking शुरू हो रही है..."
        )

        try:
            final_pdf, result = process_submission(item["path"], paper)

            try:
                os.remove(item["path"])
            except Exception:
                pass

            try:
                bot.delete_message(chat_id, status.message_id)
            except Exception:
                pass

            # Telegram caption kept deliberately short.
            feedback = str(result.get("overall_feedback", "")).strip()
            caption = (
                f"🏛️ <b>PRANA PCS — {paper} Evaluation</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>प्राप्तांक:</b> "
                f"<code>{result['total_obtained_marks']:g} / "
                f"{result['total_max_marks']:g}</code>\n\n"
                f"{feedback}"
            )
            caption = caption[:900]

            original_name = item.get("filename", "submission.pdf")
            original_stem = Path(original_name).stem or "submission"
            evaluated_filename = f"{original_stem}_Evaluated.pdf"

            bot.send_document(
                chat_id,
                final_pdf,
                visible_file_name=evaluated_filename,
                caption=caption
            )

        except Exception as e:
            try:
                os.remove(item["path"])
            except Exception:
                pass

            try:
                bot.edit_message_text(
                    "⚠️ <b>मूल्यांकन में समस्या</b>\n\n" + str(e)[:300],
                    chat_id=chat_id,
                    message_id=status.message_id
                )
            except Exception:
                bot.send_message(
                    chat_id,
                    "⚠️ मूल्यांकन में समस्या:\n" + str(e)[:300]
                )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
