import os
import io
import json
import base64
import tempfile
import uuid
import re
from datetime import datetime, timezone, date
import requests
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from fastapi import FastAPI, Request, UploadFile, File, BackgroundTasks, Form
import telebot
from PIL import Image, ImageDraw, ImageFont
import pymupdf as fitz

# PostgreSQL persistence layer (keeps the existing evaluator/rendering code intact)
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, DateTime, Text, Boolean,
    ForeignKey, JSON, Index, LargeBinary
)
from sqlalchemy.orm import declarative_base, sessionmaker


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Public HTTPS URL used by Telegram buttons/webhook. Cloud Run should set
# PUBLIC_BASE_URL to the deployed service URL (for example, https://...run.app).
# Keep the legacy Render variable only as a backwards-compatible fallback.
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
if not PUBLIC_BASE_URL:
    print("PUBLIC BASE URL WARNING: Set PUBLIC_BASE_URL in Cloud Run environment variables.")

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

# Noto Sans Devanagari — used ONLY on the summary/table page so Hindi renders
# correctly (base PDF fonts lack Devanagari glyphs -> otherwise "????").
NOTO_PATH = "/tmp/NotoSansDevanagari-Regular.ttf"

NOTO_URL = (
    "https://github.com/notofonts/devanagari/raw/main/"
    "fonts/NotoSansDevanagari/hinted/ttf/NotoSansDevanagari-Regular.ttf"
)


def ensure_noto_font():
    try:
        if os.path.exists(NOTO_PATH) and os.path.getsize(NOTO_PATH) > 10000:
            return True
        r = requests.get(NOTO_URL, timeout=30)
        r.raise_for_status()
        with open(NOTO_PATH, "wb") as f:
            f.write(r.content)
        return os.path.getsize(NOTO_PATH) > 10000
    except Exception as e:
        print("NOTO FONT ERROR:", e)
        return False


ensure_noto_font()


# ============================================================
# TEMP STORAGE
# ============================================================

PENDING = {}


# ============================================================
# POSTGRESQL DATABASE FOUNDATION
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

DB_ENABLED = bool(DATABASE_URL)
engine = None
SessionLocal = None
Base = declarative_base()

if DB_ENABLED:
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={"connect_timeout": 10},
        )
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        print("DATABASE CONFIGURED: PostgreSQL")
    except Exception as e:
        DB_ENABLED = False
        print("DATABASE CONFIG ERROR:", e)


class DBUser(Base):
    __tablename__ = "users"
    telegram_user_id = Column(String(64), primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    is_allowed = Column(Boolean, default=True, nullable=False)
    is_blocked = Column(Boolean, default=False, nullable=False)
    access_type = Column(String(20), default="none", nullable=False)  # none/full/trial
    trial_copies_limit = Column(Integer, default=3, nullable=False)
    trial_questions_limit = Column(Integer, default=10, nullable=False)
    trial_copies_used = Column(Integer, default=0, nullable=False)
    trial_questions_used = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)


class DBGroup(Base):
    __tablename__ = "telegram_groups"
    telegram_group_id = Column(String(64), primary_key=True)
    title = Column(String(255), nullable=True)
    group_type = Column(String(50), nullable=True)
    is_allowed = Column(Boolean, default=False, nullable=False)
    is_blocked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)


class DBSubmission(Base):
    __tablename__ = "submissions"
    id = Column(String(64), primary_key=True)
    telegram_user_id = Column(String(64), nullable=False, index=True)
    telegram_chat_id = Column(String(64), nullable=True, index=True)
    chat_type = Column(String(50), nullable=True)
    group_id = Column(String(64), nullable=True, index=True)
    paper = Column(String(30), nullable=False, index=True)
    exam = Column(String(30), nullable=True, index=True)
    evaluation_type = Column(String(30), nullable=True, index=True)
    source_id = Column(String(64), nullable=True, index=True)
    medium = Column(String(20), nullable=True)
    original_filename = Column(Text, nullable=False)
    evaluated_filename = Column(Text, nullable=False)
    copy_language = Column(String(20), nullable=True)
    total_obtained_marks = Column(Float, nullable=True)
    total_max_marks = Column(Float, nullable=True)
    overall_feedback = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="completed")
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DBSubmissionPDF(Base):
    __tablename__ = "submission_files"
    submission_id = Column(String(64), ForeignKey("submissions.id", ondelete="CASCADE"), primary_key=True)
    pdf_bytes = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DBQuestion(Base):
    __tablename__ = "submission_questions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    question_number = Column(Integer, nullable=False)
    start_page = Column(Integer, nullable=False)
    end_page = Column(Integer, nullable=False)
    pages_used = Column(Integer, nullable=False)
    max_marks = Column(Float, nullable=False)
    obtained_marks = Column(Float, nullable=False)
    intro_comment = Column(Text, nullable=True)
    body_comment = Column(Text, nullable=True)
    conclusion_comment = Column(Text, nullable=True)
    demand_parts = Column(JSON, nullable=True)
    fulfilled_parts = Column(JSON, nullable=True)
    skipped_parts = Column(JSON, nullable=True)
    end_page_comment = Column(Text, nullable=True)


class DBPageComment(Base):
    __tablename__ = "page_comments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    page = Column(Integer, nullable=False)
    color = Column(String(20), nullable=True)
    comment = Column(Text, nullable=False)
    placement_box = Column(JSON, nullable=True)
    anchor = Column(JSON, nullable=True)


class DBAnnotation(Base):
    __tablename__ = "annotations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True)
    page = Column(Integer, nullable=False)
    annotation_type = Column(String(20), nullable=False)
    color = Column(String(20), nullable=True)
    exact_text = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    box_2d = Column(JSON, nullable=True)


Index("ix_submissions_user_paper_date", DBSubmission.telegram_user_id, DBSubmission.paper, DBSubmission.created_at)


def init_database():
    if not DB_ENABLED or engine is None:
        print("DATABASE DISABLED: DATABASE_URL not configured")
        return False
    try:
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        print("DATABASE READY: PostgreSQL")
        return True
    except Exception as e:
        print("DATABASE INIT ERROR:", e)
        return False



# ============================================================
# FRESH CONTENT / ACCESS SCHEMA
# ============================================================

EVALUATION_EXAMS = ("UPPCS", "BPSC", "RO_ARO", "BEO")
EVALUATION_TYPES = ("GENERAL", "PYQ", "DAILY", "GROUP", "OTHER")
PAPER_OPTIONS = ("GS1", "GS2", "GS3", "GS4", "GS5", "GS6", "GENERAL_HINDI", "ESSAY")


def ensure_new_schema():
    """Create the clean, future-proof content/access schema on a fresh DB.

    This intentionally does not depend on the old daily_content table. The new
    content_sets/content_items model supports Daily Questions, PYQs, Groups,
    Hindi/English separation, dates/years, and a separate rubric per set.
    """
    if not DB_ENABLED or engine is None:
        return False
    ddl = [
        """CREATE TABLE IF NOT EXISTS content_sets (
            id VARCHAR(64) PRIMARY KEY,
            content_type VARCHAR(20) NOT NULL,
            exam VARCHAR(30) NOT NULL DEFAULT 'UPPCS',
            paper VARCHAR(30) NOT NULL,
            language VARCHAR(20) NOT NULL DEFAULT 'Hindi',
            content_date DATE NULL,
            year INTEGER NULL,
            target_group_id VARCHAR(64) NULL,
            title TEXT NULL,
            rubric TEXT NOT NULL DEFAULT '',
            source_filename TEXT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS content_items (
            id SERIAL PRIMARY KEY,
            set_id VARCHAR(64) NOT NULL REFERENCES content_sets(id) ON DELETE CASCADE,
            question_number INTEGER NOT NULL,
            question TEXT NOT NULL,
            model_answer TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(set_id, question_number)
        )""",
        """CREATE INDEX IF NOT EXISTS ix_content_sets_lookup
            ON content_sets(content_type, exam, paper, language, content_date, year)""",
        """CREATE TABLE IF NOT EXISTS access_grants (
            id SERIAL PRIMARY KEY,
            telegram_user_id VARCHAR(64) NULL,
            telegram_group_id VARCHAR(64) NULL,
            access_type VARCHAR(20) NOT NULL DEFAULT 'full',
            trial_copies_limit INTEGER NOT NULL DEFAULT 3,
            trial_questions_limit INTEGER NOT NULL DEFAULT 10,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CHECK (telegram_user_id IS NOT NULL OR telegram_group_id IS NOT NULL)
        )""",
    ]
    try:
        with engine.begin() as conn:
            for sql in ddl:
                conn.exec_driver_sql(sql)
        return True
    except Exception as e:
        print("NEW SCHEMA ERROR:", e)
        return False


def parse_qa_pairs(raw_text):
    """Parse Q1./ANS1. pairs from plain text or rich-text HTML.

    The marker itself defines the boundary: Q1. starts question 1 and
    ANS1. starts answer 1; the same rule works for Hindi and English.
    Rich-text HTML is retained inside the parsed question/model-answer.
    """
    raw = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if "<" in raw and ">" in raw:
        # Find markers anywhere in rich text, including <p>Q1...</p>.
        marker = re.compile(r"(?is)(?:^|>)\s*(Q\s*(\d+)\s*[.):-]|ANS\s*(\d+)\s*[.):-])")
        matches = list(marker.finditer(raw))
        if matches:
            qs = {}
            for i, mt in enumerate(matches):
                token = mt.group(1).upper()
                num = int(mt.group(2) or mt.group(3))
                kind = "q" if token.startswith("Q") else "a"
                body_start = mt.end()
                body_end = matches[i+1].start() if i+1 < len(matches) else len(raw)
                body = raw[body_start:body_end].strip()
                body = re.sub(r"^(?:\s|&nbsp;)+", "", body, flags=re.I)
                body = re.sub(r"(?is)</(?:p|div|section|h[1-6])>\s*$", "", body).strip()
                qs.setdefault(num, {})["question" if kind=="q" else "answer"] = body
            result=[]
            for num in sorted(qs):
                row=qs[num]
                if row.get("question"):
                    result.append({"question_number":num,"question":row["question"],"model_answer":row.get("answer","")})
            if result:
                return result

    text = re.sub(r"(?im)^\s*(?:प्रश्न|question)\s*[-.:]?\s*(\d+)\s*[).:-]?\s*", r"Q\1. ", raw)
    text = re.sub(r"(?im)^\s*(?:उत्तर|answer)\s*[-.:]?\s*(\d+)\s*[).:-]?\s*", r"ANS\1. ", text)
    qpat = re.compile(r"(?im)^\s*Q\s*(\d+)\s*[.):-]\s*(.*?)\s*$")
    apat = re.compile(r"(?im)^\s*ANS\s*(\d+)\s*[.):-]\s*(.*?)\s*$")
    rows=[]
    starts=[]
    plain=text.splitlines()
    for i,line in enumerate(plain):
        mq=qpat.match(line); ma=apat.match(line)
        if mq: starts.append((i,'q',int(mq.group(1)),mq.group(2).strip()))
        elif ma: starts.append((i,'a',int(ma.group(1)),ma.group(2).strip()))
    for i,item in enumerate(starts):
        line_no,kind,num,first=item
        end_line=starts[i+1][0] if i+1<len(starts) else len(plain)
        body="\n".join([first]+plain[line_no+1:end_line]).strip()
        rows.append((kind,num,body))
    qs={}
    for kind,num,body in rows: qs.setdefault(num,{})["question" if kind=="q" else "answer"]=body
    return [{"question_number":n,"question":v["question"],"model_answer":v.get("answer","")} for n,v in sorted(qs.items()) if v.get("question")]


def save_content_set(content_type, exam, paper, language, rubric='', content_date=None, year=None, title='', source_filename='', items=None, target_group_id=None):
    ensure_new_schema()
    sid=str(uuid.uuid4()); now=_utcnow(); items=items or []
    with engine.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO content_sets
            (id,content_type,exam,paper,language,content_date,year,target_group_id,title,rubric,source_filename,is_active,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)""",
            (sid,content_type,exam,paper,language,content_date,year,target_group_id,title,rubric,source_filename,now,now))
        for item in items:
            conn.exec_driver_sql("""INSERT INTO content_items
                (set_id,question_number,question,model_answer,created_at) VALUES (%s,%s,%s,%s,%s)""",
                (sid,int(item['question_number']),str(item['question']),str(item.get('model_answer','')),now))
    return sid

def get_content_reference(evaluation_type, source_id=None, paper=None, exam='UPPCS', language=None):
    if not DB_ENABLED or engine is None:
        return ''
    ensure_new_schema()
    ctype={'DAILY':'daily','PYQ':'pyq','GROUP':'group'}.get(str(evaluation_type or '').upper())
    if not ctype:
        return ''
    try:
        with engine.connect() as conn:
            if source_id:
                rows=conn.exec_driver_sql("""SELECT cs.id,cs.rubric,ci.question_number,ci.question,ci.model_answer
                    FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id
                    WHERE cs.id=%s AND cs.content_type=%s AND cs.exam=%s AND cs.paper=%s AND cs.is_active=TRUE
                    ORDER BY ci.question_number""",(str(source_id),ctype,exam,paper)).mappings().all()
            else:
                rows=conn.exec_driver_sql("""SELECT cs.id,cs.rubric,ci.question_number,ci.question,ci.model_answer
                    FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id
                    WHERE cs.content_type=%s AND cs.exam=%s AND cs.paper=%s AND cs.is_active=TRUE
                    ORDER BY cs.created_at DESC,ci.question_number""",(ctype,exam,paper)).mappings().all()
        if not rows: return ''
        rubric=str(rows[0].get('rubric') or '')
        parts=[f"RUBRIC:\n{rubric}"] if rubric else []
        for r in rows:
            parts.append(f"QUESTION {r['question_number']}:\n{r['question']}\nMODEL ANSWER:\n{r['model_answer']}")
        return "\n\n---\n\n".join(parts)
    except Exception as e:
        print('CONTENT REFERENCE ERROR:',e); return ''


def resolve_admin_role(uid, session=None):
    """Single source of truth for admin identity.

    Checks all three configured sources in priority order:
      1. SUPER_ADMIN_TELEGRAM_ID   (env, single id)
      2. ADMIN_TELEGRAM_IDS        (env, comma list)
      3. admin_users table         (managed from the Admin Panel)
    Returns 'super_admin', 'admin', or None. Every access gate uses this so
    admin/super-admin status can never disagree between endpoints.
    """
    uid = str(uid)
    if SUPER_ADMIN_TELEGRAM_ID and uid == str(SUPER_ADMIN_TELEGRAM_ID):
        return "super_admin"
    if uid in ADMIN_TELEGRAM_IDS:
        return "admin"
    if not DB_ENABLED or SessionLocal is None:
        return None
    own = session is None
    s = session or SessionLocal()
    try:
        row = s.execute(__import__("sqlalchemy").text(
            "SELECT role,is_active FROM admin_users WHERE telegram_user_id=:uid LIMIT 1"
        ), {"uid": uid}).mappings().first()
        if row and row["is_active"] and str(row["role"]).lower() in ("admin", "super_admin"):
            return str(row["role"]).lower()
        return None
    except Exception as e:
        print("ADMIN ROLE LOOKUP ERROR:", e)
        return None
    finally:
        if own:
            s.close()


def _trial_snapshot(u=None):
    """Plain, session-independent view of trial counters (safe after close())."""
    from types import SimpleNamespace
    if u is None:
        return SimpleNamespace(trial_copies_used=0, trial_copies_limit=3,
                               trial_questions_used=0, trial_questions_limit=10)
    return SimpleNamespace(
        trial_copies_used=int(u.trial_copies_used or 0),
        trial_copies_limit=int(u.trial_copies_limit or 3),
        trial_questions_used=int(u.trial_questions_used or 0),
        trial_questions_limit=int(u.trial_questions_limit or 10),
    )


def evaluation_access(uid, question_count=0, consume=False):
    """Central evaluation gate. UI remains accessible; only evaluation is gated.

    Returns (allowed: bool, source: str, trial: SimpleNamespace). The third value
    is ALWAYS a plain snapshot (never a live ORM row), so callers can safely read
    it after the session is closed. This removes the DetachedInstanceError class of
    failures that produced the 503 on /api/app/auth for admin/super-admin users.
    """
    if not DB_ENABLED or SessionLocal is None:
        return False, 'database_unavailable', _trial_snapshot()
    s = SessionLocal()
    try:
        u = s.get(DBUser, str(uid))
        # Admins/super-admins resolve first and are never locked out of evaluation.
        role = resolve_admin_role(uid, session=s)
        if role is not None:
            if u is not None and (not u.is_allowed or u.access_type != 'full'):
                u.is_allowed = True
                u.access_type = 'full'
                s.commit()
            return True, 'full', _trial_snapshot(u)
        if u is None or u.is_blocked:
            return False, 'blocked' if (u and u.is_blocked) else 'not_authorized', _trial_snapshot(u)
        if u.is_allowed or u.access_type == 'full':
            return True, 'full', _trial_snapshot(u)
        if u.access_type == 'trial':
            snap = _trial_snapshot(u)
            if snap.trial_copies_used >= snap.trial_copies_limit:
                return False, 'trial_copies_exhausted', snap
            if snap.trial_questions_used >= snap.trial_questions_limit:
                return False, 'trial_questions_exhausted', snap
            if consume:
                u.trial_copies_used = snap.trial_copies_used + 1
                u.trial_questions_used = snap.trial_questions_used + int(question_count or 0)
                s.commit()
                snap = _trial_snapshot(u)
            return True, 'trial', snap
        # Authorized group membership is handled by the caller for Telegram.
        return False, 'not_authorized', _trial_snapshot(u)
    except Exception as e:
        s.rollback(); print('EVALUATION ACCESS ERROR:', e); return False, 'database_error', _trial_snapshot()
    finally:
        s.close()


def content_set_has_rubric(source_id):
    if not DB_ENABLED or engine is None or not source_id: return False
    ensure_new_schema()
    try:
        with engine.connect() as conn:
            row=conn.exec_driver_sql('SELECT rubric FROM content_sets WHERE id=%s AND is_active=TRUE',(str(source_id),)).mappings().first()
        return bool(row and str(row.get('rubric') or '').strip())
    except Exception:
        return False


def evaluation_catalog():
    return {
        'exams': [
            {'id':'UPPCS','label':'UPPCS','default':True},
            {'id':'BPSC','label':'BPSC'}, {'id':'RO_ARO','label':'RO/ARO'}, {'id':'BEO','label':'BEO'}
        ],
        'papers': [
            {'id':'GS1','label':'GS-I'}, {'id':'GS2','label':'GS-II'}, {'id':'GS3','label':'GS-III'},
            {'id':'GS4','label':'GS-IV'}, {'id':'GS5','label':'GS-V'}, {'id':'GS6','label':'GS-VI'},
            {'id':'GENERAL_HINDI','label':'General Hindi'}, {'id':'ESSAY','label':'Essay','mediums':['Hindi','English']}
        ],
        'types':['GENERAL','PYQ','DAILY','GROUP','OTHER']
    }

def _utcnow():
    return datetime.now(timezone.utc)


def save_user_and_chat(message):
    if not DB_ENABLED or SessionLocal is None:
        return
    session = SessionLocal()
    try:
        now = _utcnow()
        user = getattr(message, "from_user", None)
        user_id = str(getattr(user, "id", ""))
        if user_id:
            row = session.get(DBUser, user_id)
            if row is None:
                row = DBUser(telegram_user_id=user_id, is_allowed=False, is_blocked=False, created_at=now, last_seen_at=now)
                session.add(row)
            row.username = getattr(user, "username", None)
            row.first_name = getattr(user, "first_name", None)
            row.last_name = getattr(user, "last_name", None)
            row.last_seen_at = now

        chat = getattr(message, "chat", None)
        chat_id = str(getattr(chat, "id", ""))
        chat_type = getattr(chat, "type", None)
        if chat_id and chat_type in ("group", "supergroup"):
            group = session.get(DBGroup, chat_id)
            if group is None:
                group = DBGroup(
                    telegram_group_id=chat_id,
                    title=getattr(chat, "title", None),
                    group_type=chat_type,
                    created_at=now,
                    last_seen_at=now,
                )
                session.add(group)
            group.title = getattr(chat, "title", None)
            group.last_seen_at = now
        session.commit()
    except Exception as e:
        session.rollback()
        print("DATABASE USER SAVE ERROR:", e)
    finally:
        session.close()


def save_evaluation_to_database(message, item, paper, result, evaluated_filename, evaluated_pdf_bytes=None):
    # PDF delivery is never blocked by a database error.
    if not DB_ENABLED or SessionLocal is None:
        return None
    session = SessionLocal()
    submission_id = str(uuid.uuid4())
    try:
        now = _utcnow()
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        user_id = str(getattr(user, "id", "")) or "unknown"
        chat_id = str(getattr(chat, "id", "")) or None
        chat_type = getattr(chat, "type", None)
        group_id = chat_id if chat_type in ("group", "supergroup") else None

        submission = DBSubmission(
            id=submission_id,
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            chat_type=chat_type,
            group_id=group_id,
            paper=paper,
            exam=str(result.get("exam") or "UPPCS"),
            evaluation_type=str(result.get("evaluation_type") or "GENERAL"),
            source_id=str(result.get("source_id") or "") or None,
            medium=str(result.get("medium") or result.get("copy_language") or "") or None,
            original_filename=str(item.get("filename", "submission.pdf")),
            evaluated_filename=evaluated_filename,
            copy_language=str(result.get("copy_language") or result.get("language") or "")[:20] or None,
            total_obtained_marks=float(result.get("total_obtained_marks", 0) or 0),
            total_max_marks=float(result.get("total_max_marks", 0) or 0),
            overall_feedback=str(result.get("overall_feedback", "")),
            status="completed",
            created_at=now,
            completed_at=now,
        )
        session.add(submission)

        if evaluated_pdf_bytes:
            session.add(DBSubmissionPDF(submission_id=submission_id, pdf_bytes=evaluated_pdf_bytes, created_at=now))

        for q in result.get("questions", []):
            session.add(DBQuestion(
                submission_id=submission_id,
                question_number=int(q.get("question_number", 0)),
                start_page=int(q.get("start_page", 1)),
                end_page=int(q.get("end_page", 1)),
                pages_used=int(q.get("pages_used", 1)),
                max_marks=float(q.get("max_marks", 0) or 0),
                obtained_marks=float(q.get("obtained_marks", 0) or 0),
                intro_comment=str(q.get("intro_comment", "")),
                body_comment=str(q.get("body_comment", "")),
                conclusion_comment=str(q.get("conclusion_comment", "")),
                demand_parts=q.get("demand_parts", []),
                fulfilled_parts=q.get("fulfilled_parts", []),
                skipped_parts=q.get("skipped_parts", []),
                end_page_comment=str(q.get("end_page_comment", "")),
            ))

        for c in result.get("page_comments", []):
            session.add(DBPageComment(
                submission_id=submission_id,
                page=int(c.get("page", 1) or 1),
                color=str(c.get("color", "red")),
                comment=str(c.get("comment", "")),
                placement_box=c.get("placement_box"),
                anchor=c.get("anchor"),
            ))

        for a in result.get("annotations", []):
            session.add(DBAnnotation(
                submission_id=submission_id,
                page=int(a.get("page", 1) or 1),
                annotation_type=str(a.get("type", "good")),
                color=str(a.get("color", "green")),
                exact_text=str(a.get("exact_text", "")),
                reason=str(a.get("reason", "")),
                box_2d=a.get("box_2d"),
            ))

        session.commit()
        print(f"DATABASE SAVED: submission_id={submission_id}")
        return submission_id
    except Exception as e:
        session.rollback()
        print("DATABASE EVALUATION SAVE ERROR:", e)
        return None
    finally:
        session.close()


def get_database_summary():
    if not DB_ENABLED or SessionLocal is None:
        return {"database": "disabled"}
    session = SessionLocal()
    try:
        return {
            "database": "postgresql",
            "users": session.query(DBUser).count(),
            "groups": session.query(DBGroup).count(),
            "submissions": session.query(DBSubmission).count(),
            "completed_submissions": session.query(DBSubmission).filter(DBSubmission.status == "completed").count(),
        }
    except Exception as e:
        return {"database": "error", "error": str(e)[:200]}
    finally:
        session.close()


# ============================================================
# GEMINI MODELS
# ============================================================
# Primary model is first.
# These names are based on the models visible in your Render log.

MODELS = [
    "gemini-3.6-flash",
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
""",

    "GENERAL_HINDI": """General Hindi evaluation: grammar, spelling, syntax, vocabulary, clarity, précis/translation/official-language conventions as applicable to the uploaded paper. Evaluate strictly against the question demand and language accuracy.""",
    "ESSAY": """Essay evaluation: relevance to topic, thesis, structure, introduction, argumentation, multidimensional analysis, examples/data, coherence, balance, language/style, presentation and conclusion. Evaluate the essay in the medium actually used by the candidate: Hindi or English."""
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
        "GS1","GS2","GS3","GS4","GS5","GS6",
        "GENERAL_HINDI","ESSAY"
    ):
        if paper in t:
            return paper

        if paper.replace("GS", "जीएस") in clean:
            return paper
    if any(x in t for x in ("GENERALHINDI","GENHINDI","HINDI")):
        return "GENERAL_HINDI"
    if "ESSAY" in t or "निबंध" in clean:
        return "ESSAY"
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
    markup=telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Open Mini App", web_app=telebot.types.WebAppInfo(url=f"{PUBLIC_BASE_URL}/app")))
    bot.reply_to(
        message,
        "📄 <b>Copy Received.</b>\n\n"
        "Before evaluation, send the <b>Paper Name</b>:\n\n"
        "• GS 1\n• GS 2\n• GS 3\n• GS 4\n• GS 5\n• GS 6\n• General Hindi\n• Essay\n\n"
        "Example: <b>GS 3</b>",
        reply_markup=markup
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():

    ensure_font()
    init_database()
    ensure_new_schema()

    print(
        "AVAILABLE GEMINI MODELS CONFIGURED:",
        MODELS
    )

    if bot:

        try:
            if PUBLIC_BASE_URL:
                bot.remove_webhook()
                bot.set_webhook(url=f"{PUBLIC_BASE_URL}/webhook")
                print("TELEGRAM WEBHOOK SET:", f"{PUBLIC_BASE_URL}/webhook")
            else:
                print("TELEGRAM WEBHOOK SKIPPED: PUBLIC_BASE_URL is not configured.")
        except Exception as e:
            print("WEBHOOK ERROR:", e)


@app.get("/")
def home():

    data = {
        "status": "PRANA PCS AI Evaluator Active",
        "font": os.path.exists(FONT_PATH),
        "models": MODELS
    }
    data.update(get_database_summary())
    return data


@app.get("/api/model-status")
def model_status():
    return {"configured_models": MODELS}


@app.get("/api/health")
def health_check():
    db_ok, db_message = mini_app_db_ready()
    return {
        "ok": True,
        "service": "prana-pcs-ai-evaluator",
        "runtime": "google-cloud-run" if os.getenv("K_SERVICE") else "generic",
        "public_base_url_configured": bool(PUBLIC_BASE_URL),
        "public_base_url": PUBLIC_BASE_URL or None,
        "telegram_bot_configured": bool(bot),
        "gemini_configured": bool(GEMINI_API_KEY),
        "database_configured": bool(DB_ENABLED),
        "database_ready": db_ok,
        "database_message": db_message,
        "mini_app": True,
        "models": MODELS,
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
            dpi=96,
            alpha=False
        )

        pages.append(
            pix.tobytes(
                "jpeg",
                jpg_quality=84
            )
        )

    return pages


# ============================================================
# GEMINI PROMPT
# ============================================================
# ============================================================
# DAILY MODEL ANSWER REFERENCE
# ============================================================

def get_daily_model_answer_reference(paper, source_id=None, exam="UPPCS"):
    ref=get_content_reference("DAILY", source_id=source_id, paper=paper, exam=exam)
    if ref:
        return ref
    if not DB_ENABLED or engine is None:
        return ""
    try:
        ensure_admin_content_table()
        with engine.connect() as conn:
            rows=conn.exec_driver_sql("SELECT question,model_answer,language FROM daily_content WHERE is_active=TRUE AND paper=%s ORDER BY id DESC LIMIT 20",(paper.upper(),)).mappings().all()
        return "\n\n---\n\n".join(f"QUESTION ({r.get('language','')}):\n{r.get('question','')}\nMODEL ANSWER:\n{r.get('model_answer','')}" for r in rows)
    except Exception as e:
        print("DAILY MODEL ANSWER REFERENCE ERROR:",e); return ""

def build_prompt(
    paper,
    total_pages,
    model_answer_reference="",
    evaluation_type="GENERAL",
    exam="UPPCS"
):

    forced_language_override = ""
    if str(paper).upper() == "GENERAL_HINDI":
        forced_language_override = (
            "\n============================================================\n"
            "OVERRIDE (अनिवार्य): यह General Hindi paper है।\n"
            "इसका पूरा मूल्यांकन, सभी examiner comments, feedback और summary\n"
            "अनिवार्य रूप से केवल हिंदी में होंगे — किसी भी परिस्थिति में English में नहीं।\n"
            "============================================================\n"
        )

    return f"""
आप PRANA PCS के वरिष्ठ UPPCS Mains examiner हैं।
{forced_language_override}
Paper: {paper}
Total pages: {total_pages}

{RUBRICS.get(paper, RUBRICS.get("GS3", ""))}

EXAM: {exam}
EVALUATION TYPE: {evaluation_type}

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

GENERAL HINDI RULE:
- General Hindi paper is always assessed and commented in Hindi.
- Do not ask the candidate to choose a medium for General Hindi.

ESSAY RULE:
- Detect the language actually used in the uploaded essay yourself.
- If the essay is Hindi, all examiner comments, feedback and summary fields must be Hindi.
- If the essay is English, all examiner comments, feedback and summary fields must be English.
- Never rely on a UI medium selector for Essay.

============================================================
DAILY MODEL ANSWER BENCHMARK
============================================================

यदि नीचे PRANA PCS का Daily Question / Model Answer उपलब्ध है और
विद्यार्थी की copy उसी question का उत्तर देती है, तो उसे benchmark की
तरह इस्तेमाल करें। Model Answer को copy न करें और केवल उसके शब्दों की
नकल के आधार पर marks न दें। Question demand, correctness, structure,
analysis, examples, data, value addition और omissions को independently
check करें। Model Answer से छूटे हुए महत्वपूर्ण dimensions identify
करने में सहायता लें।

REFERENCE MODEL ANSWERS:
{model_answer_reference}

============================================================
MARKING
============================================================

Question की actual demand को पहले identify करें।

केवल topic coverage देखकर marks न दें।

MARK CALIBRATION: सामान्यतः अच्छे लेकिन imperfect उत्तरों को realistic average-to-good range में रखें (लगभग 55–75% of available marks, question difficulty के अनुसार)। 80%+ केवल genuinely exceptional answers को दें। केवल छात्र को अच्छा महसूस कराने के लिए marks inflate न करें और किसी भी उत्तर को default रूप से highest end पर न रखें। बहुत कमजोर/अपूर्ण उत्तर को भी वास्तविक performance के अनुसार कम marks दें।

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
- भाषा, शैली और प्रस्तुतीकरण (language, style, presentation) का स्पष्ट उल्लेख अनिवार्य है।

का संतुलित उल्लेख हो।

कोई अलग heading, bullet list, score repeat या suggestions list नहीं।

============================================================
OUTPUT
============================================================

केवल valid JSON दें:

{{
  "total_obtained_marks": 0,
  "total_max_marks": 0,
  "copy_language": "Hindi",

  "questions": [
    {{
      "question_number": 1,
      "start_page": 1,
      "end_page": 2,
      "pages_used": 2,
      "max_marks": 8,
      "obtained_marks": 5.0,
      "intro_comment": "प्रस्तावना का संक्षिप्त examiner summary",
      "body_comment": "मुख्य भाग का संक्षिप्त examiner summary",
      "conclusion_comment": "निष्कर्ष का संक्षिप्त examiner summary",

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
    paper,
    evaluation_type="GENERAL",
    source_id=None,
    exam="UPPCS"
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
                len(images),
                get_daily_model_answer_reference(paper, source_id=source_id, exam=exam) if str(evaluation_type).upper()=="DAILY" else get_content_reference(evaluation_type, source_id=source_id, paper=paper, exam=exam),
                evaluation_type=evaluation_type,
                exam=exam
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
            "thinkingConfig": {"thinkingLevel": "low"},
            "maxOutputTokens": 24000
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
                timeout=(15, 75)
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
                ).strip(),

                "intro_comment": str(
                    question.get("intro_comment", "")
                ).strip(),

                "body_comment": str(
                    question.get("body_comment", "")
                ).strip(),

                "conclusion_comment": str(
                    question.get("conclusion_comment", "")
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

    copy_language = str(
        data.get(
            "copy_language",
            ""
        )
    ).strip()
    if copy_language.lower() in ("english", "en"):
        copy_language = "English"
    elif copy_language.lower() in ("hindi", "hi", "हिंदी"):
        copy_language = "Hindi"
    else:
        copy_language = "Hindi"

    return {
        "total_obtained_marks":
            total_obtained,

        "copy_language":
            copy_language,

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
    font_size=94,
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

    title_font = font(64)
    score_font = font(98)

    title = "PRANA AI EVALUATOR"

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

    fnt = font(112)

    text = (
        f"Q{question_number}   "
        f"{obtained:g}/{total:g}"
    )

    width = 760
    height = 190

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
        width=1700,
        font_size=94,
        color=color
    )

    badge_image = Image.open(
        io.BytesIO(png)
    )

    img_w, img_h = (
        badge_image.size
    )

    desired_w = min(
        page_width * 0.34,
        290
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


def insert_evaluation_summary_page(pdf, result):
    """Insert the branded first page with Q-wise Intro/Body/Conclusion/Marks."""
    page = pdf.new_page(pno=0, width=595, height=842)
    try:
        if os.path.exists(NOTO_PATH):
            page.insert_font(fontname="noto", fontfile=NOTO_PATH)
            _hindi = "noto"
        else:
            _hindi = "helv"
    except Exception:
        _hindi = "helv"
    logo_path = STATIC_DIR / "branding" / "prana-logo.png" if 'STATIC_DIR' in globals() else None
    if logo_path and logo_path.exists():
        try: page.insert_image(fitz.Rect(42,22,78,58), filename=str(logo_path), overlay=True)
        except Exception: pass
    page.insert_text((88,47), "Prana PCS AI Mains Evaluator", fontsize=18, fontname="hebo", color=(0.10,0.12,0.16), overlay=True)
    page.insert_text((42,78), datetime.now().strftime("%d-%m-%Y"), fontsize=9, color=(0.35,0.35,0.35), overlay=True)
    total=result.get('total_obtained_marks',0); mx=result.get('total_max_marks',0)
    page.insert_text((420,48), f"Marks: {float(total):g}/{float(mx):g}", fontsize=11, fontname="hebo", color=(0.12,0.12,0.12), overlay=True)
    y=105; x0=38; widths=[42,125,175,145,50]
    headers=["Q.No.","Intro","Body","Conclusion","Marks"]
    for x,w,h in zip([x0,80,205,380,525],widths,headers):
        page.draw_rect(fitz.Rect(x,y,x+w,y+30), color=(0.25,0.27,0.31), fill=(0.94,0.95,0.97), width=0.6, overlay=True)
        page.insert_textbox(fitz.Rect(x+3,y+5,x+w-3,y+27), h, fontsize=8, fontname="hebo", color=(0.08,0.08,0.08), align=1, overlay=True)
    y+=30
    for q in result.get('questions',[]):
        vals=[str(q.get('question_number','')), str(q.get('intro_comment','') or '—'), str(q.get('body_comment','') or '—'), str(q.get('conclusion_comment','') or '—'), f"{float(q.get('obtained_marks',0)):g}/{float(q.get('max_marks',0)):g}"]
        row_h=52
        for x,w,val in zip([x0,80,205,380,525],widths,vals):
            page.draw_rect(fitz.Rect(x,y,x+w,y+row_h), color=(0.65,0.67,0.70), width=0.5, overlay=True)
            page.insert_textbox(fitz.Rect(x+3,y+4,x+w-3,y+row_h-4), val, fontsize=7.5, fontname=_hindi, color=(0.10,0.11,0.13), align=1 if x in (x0,525) else 0, overlay=True)
        y+=row_h
        if y>690:
            page.insert_text((42,715), "See evaluated copy pages for detailed examiner comments and annotations.", fontsize=8, color=(0.35,0.35,0.35), overlay=True); break
    page.draw_line((42,790),(553,790),color=(0.65,0.65,0.65),width=0.6,overlay=True)
    for txt,pos in [("Telegram",42),("Instagram",300),("YouTube",42),("WhatsApp",300)]:
        yy=806 if txt in ("Telegram","Instagram") else 822
        page.insert_text((pos,yy),txt,fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)
    page.insert_text((185,838),"Paid Batches & Content - 9984351085",fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)

def annotate_pdf(
    pdf,
    result
):

    # Summary page is inserted AFTER annotation (see end of function) so it is
    # never treated as an answer page -> Q1 comments no longer land on page 1.
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
                            width=1700,
                            font_size=90,
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
                    page_width - 155,
                    y - 44,
                    page_width - 5,
                    y
                )

                page.insert_image(
                    marks_rect,
                    stream=marks_png,
                    keep_proportion=True
                )

                y -= 50

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

    insert_evaluation_summary_page(pdf, result)

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
    paper,
    evaluation_type="GENERAL",
    source_id=None,
    exam="UPPCS",
    medium=None
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
        images, paper, evaluation_type=evaluation_type, source_id=source_id, exam=exam
    )
    result["exam"]=exam; result["evaluation_type"]=evaluation_type; result["source_id"]=source_id
    # Paper language is AI-detected from the uploaded copy. Medium is not forced by UI.
    if paper == "GENERAL_HINDI":
        result["copy_language"] = "Hindi"
    result["medium"] = result.get("copy_language") or ("Hindi" if paper=="GENERAL_HINDI" else None)

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

        mini_url = os.getenv(
            "MINI_APP_URL",
            f"{RENDER_EXTERNAL_URL}/app"
        ).rstrip("/")
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton(
                "📤 Evaluate Copy",
                web_app=telebot.types.WebAppInfo(url=mini_url)
            )
        )
        markup.row(
            telebot.types.InlineKeyboardButton(
                "📊 My Performance",
                web_app=telebot.types.WebAppInfo(url=mini_url + "?view=performance")
            )
        )
        bot.reply_to(
            message,

            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "<i>LET'S PRANA</i>\n\n"
            "Evaluate your answer copy using the Mini App.\n"
            "Hindi copy → Hindi evaluation | English copy → English evaluation",
            reply_markup=markup
        )




def telegram_chat_access_allowed(message):
    """Telegram chat access gate.

    Rules:
    1. Explicitly allowed student -> allowed.
    2. Blocked student -> denied everywhere.
    3. In an authorized group -> allowed if the group itself is allowed.
    4. In a private chat -> allowed when the student belongs to ANY admin-allowed group.
       Membership is verified live with Telegram getChatMember.
    5. Everyone else -> Access Denied.
    """
    if not DB_ENABLED or SessionLocal is None:
        return False, "database_unavailable"

    uid = str(getattr(getattr(message, "from_user", None), "id", ""))
    chat = getattr(message, "chat", None)
    chat_id = str(getattr(chat, "id", ""))
    chat_type = getattr(chat, "type", None)

    session = SessionLocal()
    try:
        user = session.get(DBUser, uid) if uid else None
        if user and user.is_blocked:
            return False, "blocked"
        is_privileged = resolve_admin_role(uid, session=session) is not None
        if is_privileged:
            if user:
                user.is_allowed=True; user.access_type="full"; session.commit()
            return True, "full"
        if user and (user.is_allowed or user.access_type in ("full", "trial")):
            if user.access_type == "trial" and int(user.trial_copies_used or 0) >= int(user.trial_copies_limit or 3):
                return False, "trial_copies_exhausted"
            if user.access_type == "trial" and int(user.trial_questions_used or 0) >= int(user.trial_questions_limit or 10):
                return False, "trial_questions_exhausted"
            return True, "trial" if user.access_type == "trial" else "user"

        # If this message is already inside an authorized group,
        # the group grant is sufficient for evaluation.
        if chat_type in ("group", "supergroup") and chat_id:
            group = session.get(DBGroup, chat_id)
            if group and group.is_allowed and not group.is_blocked:
                return True, "group"

        allowed_groups = session.query(DBGroup).filter(
            DBGroup.is_allowed == True,
            DBGroup.is_blocked == False
        ).all()
    except Exception as e:
        print("TELEGRAM ACCESS DB ERROR:", e)
        return False, "database_error"
    finally:
        session.close()

    # For a private chat, verify the user's live membership in any allowed group.
    if not bot or not uid:
        return False, "not_authorized"

    for group in allowed_groups:
        try:
            member = bot.get_chat_member(int(group.telegram_group_id), int(uid))
            status = str(getattr(member, "status", ""))
            is_member = bool(getattr(member, "is_member", False))
            if status in ("creator", "administrator", "member") or (status == "restricted" and is_member):
                return True, "group"
        except Exception as e:
            print(f"TELEGRAM GROUP MEMBERSHIP CHECK FAILED {group.telegram_group_id}: {str(e)[:160]}")
            continue

    return False, "not_authorized"


if bot:

    @bot.message_handler(
        content_types=[
            "document",
            "photo"
        ]
    )
    def receive_copy(message):

        try:

            save_user_and_chat(message)

            allowed, source = telegram_chat_access_allowed(message)
            if not allowed:
                markup=telebot.types.InlineKeyboardMarkup()
                markup.add(telebot.types.InlineKeyboardButton("Let’s Trial", web_app=telebot.types.WebAppInfo(url=f"{PUBLIC_BASE_URL}/app")))
                bot.reply_to(message, "🔒 <b>Access Denied</b>\n\nEvaluation access is not enabled. You can start your Trial from the Mini App.", reply_markup=markup)
                return

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

                "⚠️ Copy could not be received.\n"
                f"{str(e)[:180]}"
            )


    @bot.message_handler(
        content_types=[
            "text"
        ]
    )
    def paper_reply(message):

        allowed, source = telegram_chat_access_allowed(message)
        if not allowed:
            PENDING.pop(message.chat.id, None)
            bot.reply_to(message, "🔒 <b>Access Denied</b>\n\nYour Telegram account or authorized Telegram group does not have access.")
            return

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

                "❗ Paper not recognized.\n\n"
                "Please send <b>GS 1-6</b>, <b>General Hindi</b> or <b>Essay</b>."
            )

            return

        item = PENDING.pop(
            chat_id
        )

        status = bot.reply_to(
            message,

            f"⏳ <b>{paper} selected.</b>\n\n"
            "Page-by-page evaluation and examiner-style checking has started..."
        )

        import threading
        threading.Thread(
            target=_run_telegram_evaluation,
            args=(message, item, paper, status),
            daemon=True
        ).start()


    def _run_telegram_evaluation(message, item, paper, status, source="trial"):
            # chat_id and source were previously undefined free variables here,
            # which crashed the background thread with NameError. Bind them locally.
            chat_id = int(message.chat.id)
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
                    f"🎯 <b>Obtained Marks:</b> "
                    f"<code>"
                    f"{result['total_obtained_marks']:g} / "
                    f"{result['total_max_marks']:g}"
                    f"</code>\n\n"
                    f"📝 <b>Language • Style • Presentation:</b> "
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

                # Save complete evaluation for Mini App/Admin Panel.
                # If PostgreSQL fails, the student still receives the evaluated PDF.
                # Trial quota is consumed only after a successful evaluation.
                if source == "trial":
                    s_trial=SessionLocal()
                    try:
                        u_trial=s_trial.get(DBUser,str(message.from_user.id))
                        if u_trial and u_trial.access_type=="trial":
                            qcount=len(result.get("questions",[]))
                            if int(u_trial.trial_copies_used or 0) >= int(u_trial.trial_copies_limit or 3) or int(u_trial.trial_questions_used or 0)+qcount > int(u_trial.trial_questions_limit or 10):
                                bot.reply_to(message,"🔒 <b>Trial Limit Reached</b>\n\nTrial access is limited to 3 copies or 10 questions.")
                                return
                            u_trial.trial_copies_used += 1; u_trial.trial_questions_used += qcount; s_trial.commit()
                    finally:
                        s_trial.close()

                submission_id = save_evaluation_to_database(
                    message,
                    item,
                    paper,
                    result,
                    evaluated_filename,
                    final_pdf.getvalue()
                )

                markup=telebot.types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    telebot.types.InlineKeyboardButton("Open Mini App", web_app=telebot.types.WebAppInfo(url=f"{PUBLIC_BASE_URL}/app")),
                    telebot.types.InlineKeyboardButton("Performance", web_app=telebot.types.WebAppInfo(url=f"{PUBLIC_BASE_URL}/app?view=performance")),
                )
                markup.add(
                    telebot.types.InlineKeyboardButton("Evaluation History", web_app=telebot.types.WebAppInfo(url=f"{PUBLIC_BASE_URL}/app?view=history")),
                    telebot.types.InlineKeyboardButton("Evaluate Another Copy", web_app=telebot.types.WebAppInfo(url=f"{PUBLIC_BASE_URL}/app?view=evaluate")),
                )
                bot.send_document(
                    chat_id,
                    final_pdf,
                    visible_file_name=evaluated_filename,
                    caption=caption,
                    reply_markup=markup
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
                            "⚠️ <b>Evaluation Error</b>\n\n"
                            f"{str(e)[:300]}"
                        ),
                        chat_id=chat_id,
                        message_id=status.message_id
                    )

                except Exception:

                    bot.send_message(
                        chat_id,

                        "⚠️ Evaluation error.\n"
                        f"{str(e)[:300]}"
                    )

# ============================================================
# DATABASE HEALTH / STATISTICS API
# ============================================================

@app.get("/api/health")
def api_health():
    return get_database_summary()


@app.get("/api/health/db")
def api_health_db():
    """Explicit PostgreSQL/Supabase connectivity check for Cloud Run smoke tests."""
    if not DB_ENABLED or engine is None:
        return {"ok": False, "database": "disabled", "error": "DATABASE_URL is not configured."}
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return {"ok": True}
    except Exception as e:
        print("DATABASE HEALTH ERROR:", repr(e))
        return {"ok": False, "error": str(e)[:500]}


@app.get("/api/stats")
def api_stats():
    if not DB_ENABLED or SessionLocal is None:
        return {"ok": False, "database": "disabled"}
    session = SessionLocal()
    try:
        rows = session.query(DBSubmission).all()
        by_paper = {}
        total_obtained = 0.0
        total_max = 0.0
        for row in rows:
            bucket = by_paper.setdefault(row.paper, {"submissions": 0, "obtained": 0.0, "max": 0.0})
            bucket["submissions"] += 1
            bucket["obtained"] += float(row.total_obtained_marks or 0)
            bucket["max"] += float(row.total_max_marks or 0)
            total_obtained += float(row.total_obtained_marks or 0)
            total_max += float(row.total_max_marks or 0)
        return {
            "ok": True,
            "total_submissions": len(rows),
            "total_obtained": round(total_obtained, 1),
            "total_max": round(total_max, 1),
            "by_paper": by_paper,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        session.close()


# ============================================================
# ADMIN PANEL — FOUNDATION
# ============================================================

from fastapi.responses import HTMLResponse, Response
from sqlalchemy import desc
import hashlib
import html
import hmac
import time
import base64

SUPER_ADMIN_TELEGRAM_ID = os.getenv("SUPER_ADMIN_TELEGRAM_ID", "").strip()
ADMIN_TELEGRAM_IDS = {
    x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()
}
ADMIN_SESSION_COOKIE = "prana_admin_session"
ADMIN_SESSION_MAX_AGE = 60 * 60 * 24 * 7
ADMIN_AUTH_SECRET = hashlib.sha256((BOT_TOKEN + "|PRANA_PCS_ADMIN_AUTH").encode()).digest()


def ensure_admin_roles_table():
    if not DB_ENABLED or engine is None:
        return False
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    telegram_user_id VARCHAR(64) PRIMARY KEY,
                    role VARCHAR(30) NOT NULL DEFAULT 'admin',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    last_login_at TIMESTAMPTZ NULL
                )
            """)
        return True
    except Exception as e:
        print("ADMIN ROLE TABLE ERROR:", e)
        return False


def telegram_login_valid(data):
    try:
        received = str(data.get("hash", ""))
        auth_date = int(data.get("auth_date", 0))
        if not received or not auth_date or abs(int(time.time()) - auth_date) > 86400:
            return False
        check = "\n".join(
            f"{k}={data[k]}" for k in sorted(data)
            if k != "hash" and data.get(k) is not None
        )
        secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
        calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calculated, received)
    except Exception:
        return False


def make_admin_session(uid, role):
    payload = {"uid": str(uid), "role": role, "exp": int(time.time()) + ADMIN_SESSION_MAX_AGE}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(ADMIN_AUTH_SECRET, raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def current_admin(request: Request):
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(ADMIN_AUTH_SECRET, raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        uid = str(payload.get("uid", ""))
        if SUPER_ADMIN_TELEGRAM_ID and uid == SUPER_ADMIN_TELEGRAM_ID:
            return {"id": uid, "role": "super_admin"}
        if not DB_ENABLED:
            return None
        s = SessionLocal()
        try:
            row = s.execute(__import__("sqlalchemy").text(
                "SELECT role,is_active FROM admin_users WHERE telegram_user_id=:uid"
            ), {"uid": uid}).mappings().first()
            if row and row["is_active"]:
                return {"id": uid, "role": str(row["role"])}
        finally:
            s.close()
    except Exception:
        return None
    return None


def admin_authorized(request: Request) -> bool:
    return current_admin(request) is not None


def super_admin_authorized(request: Request) -> bool:
    a = current_admin(request)
    return bool(a and a["role"] == "super_admin")


def admin_denied(): return {"ok": False, "error": "Admin authorization required"}
def admin_forbidden(): return {"ok": False, "error": "Super Admin permission required"}

ensure_admin_roles_table()


@app.post("/api/admin/telegram-login")
async def admin_telegram_login(request: Request):
    data = await request.json()
    if not telegram_login_valid(data):
        return Response(content=b'{"ok":false,"error":"Invalid Telegram authentication"}', status_code=401, media_type="application/json")
    uid = str(data.get("id", "")).strip()
    if not uid:
        return Response(content=b'{"ok":false,"error":"Telegram User ID missing"}', status_code=400, media_type="application/json")

    role = resolve_admin_role(uid)

    if not role:
        return Response(content=b'{"ok":false,"error":"Telegram account is not an authorized admin"}', status_code=403, media_type="application/json")

    if DB_ENABLED:
        s=SessionLocal()
        try:
            now=_utcnow()
            u=s.get(DBUser,uid)
            if u:
                u.username=data.get("username"); u.first_name=data.get("first_name"); u.last_name=data.get("last_name"); u.last_seen_at=now
            else:
                s.add(DBUser(telegram_user_id=uid,username=data.get("username"),first_name=data.get("first_name"),last_name=data.get("last_name"),is_allowed=True,is_blocked=False,created_at=now,last_seen_at=now))
            if role != "super_admin":
                s.execute(__import__("sqlalchemy").text("UPDATE admin_users SET last_login_at=:now WHERE telegram_user_id=:uid"),{"now":now,"uid":uid})
            s.commit()
        finally:s.close()

    response=Response(content=json.dumps({"ok":True,"role":role,"telegram_user_id":uid}),media_type="application/json")
    response.set_cookie(ADMIN_SESSION_COOKIE,make_admin_session(uid,role),max_age=ADMIN_SESSION_MAX_AGE,httponly=True,secure=True,samesite="lax",path="/")
    return response

@app.post("/api/admin/logout")
def admin_logout():
    response=Response(content=b'{"ok":true}',media_type="application/json")
    response.delete_cookie(ADMIN_SESSION_COOKIE,path="/")
    return response

@app.get("/api/admin/me")
def admin_me(request: Request):
    a=current_admin(request)
    return {"ok":True,"authenticated":bool(a),"telegram_user_id":a["id"] if a else None,"role":a["role"] if a else None}

def ensure_admin_content_table():
    if not DB_ENABLED or engine is None: return False
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""CREATE TABLE IF NOT EXISTS daily_content (id SERIAL PRIMARY KEY,paper VARCHAR(10) NOT NULL,language VARCHAR(20) NOT NULL DEFAULT 'Hindi',question TEXT NOT NULL,model_answer TEXT NOT NULL DEFAULT '',is_active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""")
        return True
    except Exception as e: print("ADMIN CONTENT TABLE ERROR:",e); return False

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    bot_username = os.getenv("BOT_USERNAME", "").strip()
    if bot and not bot_username:
        try:
            bot_username = bot.get_me().username or ""
        except Exception:
            pass

    html = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PRANA PCS Admin</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#172033;--muted:#667085;--line:#e5e7eb;--blue:#2563eb;--green:#059669;--red:#dc2626;--purple:#7c3aed}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,select,textarea{font:inherit}.top{position:sticky;top:0;z-index:20;background:#111827;color:#fff;padding:14px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;box-shadow:0 3px 15px #0002}.brand{font-weight:800}.topright{display:flex;align-items:center;gap:10px;font-size:13px}.wrap{max-width:1500px;margin:auto;padding:18px}.hidden{display:none!important}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 2px 10px #10182808}.login{max-width:480px;margin:80px auto;text-align:center}.tgbox{display:flex;justify-content:center;margin:28px 0}.btn{padding:9px 13px;border:0;border-radius:10px;background:#111827;color:#fff;cursor:pointer;font-weight:650}.btn:hover{filter:brightness(.96)}.green{background:var(--green)}.red{background:var(--red)}.blue{background:var(--blue)}.purple{background:var(--purple)}.gray{background:#667085}.ghost{background:#eef2f7;color:#172033}.tabs{display:flex;gap:8px;overflow:auto;margin:14px 0;padding-bottom:3px}.tab{white-space:nowrap}.tab.active{background:var(--blue)}.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:16px 0}.stat{min-height:92px}.stat small{color:var(--muted)}.stat b{display:block;font-size:27px;margin-top:6px}.section{margin-top:18px}.sectionhead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}.tablewrap{overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:14px}.table{width:100%;border-collapse:collapse;min-width:850px}.table th,.table td{padding:10px 12px;border-bottom:1px solid #eee;text-align:left;font-size:13px;vertical-align:top}.table th{background:#f8fafc;position:sticky;top:0}.pill{display:inline-block;padding:4px 8px;border-radius:999px;font-size:11px;background:#eef2ff}.ok{background:#dcfce7;color:#166534}.bad{background:#fee2e2;color:#991b1b}.role{background:#ede9fe;color:#6d28d9}.input{padding:10px;border:1px solid #d0d5dd;border-radius:10px;background:white;outline:none}.input:focus{border-color:#7aa2ff;box-shadow:0 0 0 3px #2563eb15}.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.search{min-width:250px}.formgrid{display:grid;grid-template-columns:150px 150px 1fr 1fr auto;gap:8px}.modal{position:fixed;inset:0;background:#11182799;z-index:50;display:flex;align-items:center;justify-content:center;padding:15px}.modalbox{background:#fff;border-radius:18px;max-width:1050px;width:100%;max-height:90vh;overflow:auto;padding:20px}.close{float:right}.metricgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metric{background:#f8fafc;border:1px solid var(--line);padding:12px;border-radius:12px}.metric b{font-size:22px;display:block}.bar{height:9px;background:#e5e7eb;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:var(--blue)}.empty{padding:30px;text-align:center;color:var(--muted)}pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:12px;border-radius:10px}.dangertext{color:var(--red)}.rich-toolbar{display:flex;gap:5px;flex-wrap:wrap}.bulk-answer:empty:before{content:attr(data-placeholder);color:#98a2b3}@media(max-width:1050px){.grid{grid-template-columns:repeat(3,1fr)}}@media(max-width:700px){.wrap{padding:10px}.grid{grid-template-columns:repeat(2,1fr)}.metricgrid{grid-template-columns:repeat(2,1fr)}.formgrid{grid-template-columns:1fr}.search{min-width:0;width:100%}.top{align-items:flex-start}.topright{flex-wrap:wrap;justify-content:flex-end}}
</style></head><body>
<div id="login" class="login card"><div style="font-size:40px">🏛️</div><h1>PRANA PCS</h1><h2>Admin Panel</h2><p>अपने Telegram Admin account से login करें।</p>
<div class="tgbox"><script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="__BOT_USERNAME__" data-size="large" data-userpic="false" data-request-access="write" data-onauth="onTelegramAuth(user)"></script></div>
<p id="err" class="dangertext"></p><p style="font-size:12px;color:#667085">केवल authorized Telegram accounts को access मिलेगा।</p></div>
<div id="app" class="hidden"><div class="top"><div class="brand">🏛️ PRANA PCS — Admin Panel</div><div class="topright"><span id="who"></span><button class="btn gray" onclick="logout()">Logout</button></div></div>
<div class="wrap"><div class="tabs"><button class="btn tab active" data-tab="dashboard" onclick="tab('dashboard',this)">📊 Dashboard</button><button class="btn tab" data-tab="users" onclick="tab('users',this)">👥 Students</button><button class="btn tab" data-tab="groups" onclick="tab('groups',this)">👥 Groups</button><button class="btn tab" data-tab="submissions" onclick="tab('submissions',this)">📄 Evaluations</button><button class="btn tab" data-tab="content" onclick="tab('content',this)">📝 Daily Content</button><button class="btn tab" data-tab="pyqs" onclick="tab('pyqs',this)">📚 PYQs</button><button class="btn tab" data-tab="admins" id="adminTab" onclick="tab('admins',this)">👑 Admins</button></div>
<section id="dashboard" class="tabsec"><div id="stats" class="grid"></div><div class="card"><div class="sectionhead"><h2>📈 Paper-wise Performance</h2><button class="btn ghost" onclick="refreshAll()">↻ Refresh</button></div><div id="paperStats" class="metricgrid"></div></div><div class="section card"><div class="sectionhead"><h2>🕘 Recent Evaluations</h2></div><div id="recent"></div></div></section>
<section id="users" class="tabsec hidden"><div class="card"><div class="sectionhead"><div><h2>➕ Add Student</h2><p style="color:#667085;margin-top:-8px">केवल Telegram User ID डालकर student जोड़ें और access तुरंत enable करें।</p></div></div><div class="formgrid" style="grid-template-columns:1fr auto"><input id="newStudentId" class="input" placeholder="Telegram User ID"><select id="newStudentAccess" class="input"><option value="full">Full Access</option><option value="trial">Trial — 3 copies / 10 questions</option><option value="none">No Evaluation Access</option></select><button class="btn green" onclick="addStudent()">➕ Add Student</button></div></div><div class="sectionhead"><div><h2>👥 Students / Users</h2><p style="color:#667085;margin-top:-8px">Access, submissions और individual performance manage करें।</p></div><div class="toolbar"><input id="userSearch" class="input search" placeholder="Telegram ID / name / username" oninput="filterUsers()"><button class="btn ghost" onclick="loadUsers()">↻</button></div></div><div class="tablewrap"><table class="table"><thead><tr><th>User</th><th>Status</th><th>Copies</th><th>Average</th><th>Last Seen</th><th>Access</th><th>Performance</th></tr></thead><tbody id="usersBody"></tbody></table></div></section>
<section id="groups" class="tabsec hidden"><div class="card"><div class="sectionhead"><div><h2>➕ Add Telegram Group</h2><p style="color:#667085;margin-top:-8px">केवल Telegram Group ID डालकर group जोड़ें और access enable करें।</p></div></div><div class="formgrid" style="grid-template-columns:1fr auto"><input id="newGroupId" class="input" placeholder="Telegram Group ID (जैसे -100...)"><button class="btn green" onclick="addGroup()">➕ Add Group</button></div></div><div class="sectionhead"><div><h2>👥 Telegram Groups</h2><p style="color:#667085;margin-top:-8px">पूरे Telegram group को access दे या हटाएँ।</p></div><button class="btn ghost" onclick="loadGroups()">↻ Refresh</button></div><div class="tablewrap"><table class="table"><thead><tr><th>Group</th><th>Type</th><th>Status</th><th>Last Seen</th><th>Access</th></tr></thead><tbody id="groupsBody"></tbody></table></div></section>
<section id="submissions" class="tabsec hidden"><div class="sectionhead"><div><h2>📄 Evaluated Copies</h2><p style="color:#667085;margin-top:-8px">हर evaluation की details, marks और PDF.</p></div><div class="toolbar"><select id="subPaper" class="input" onchange="loadSubmissions()"><option value="">All Papers</option><option>GS1</option><option>GS2</option><option>GS3</option><option>GS4</option><option>GS5</option><option>GS6</option></select><input id="subSearch" class="input search" placeholder="User ID / filename" oninput="filterSubs()"><button class="btn ghost" onclick="loadSubmissions()">↻</button></div></div><div class="tablewrap"><table class="table"><thead><tr><th>Date</th><th>User</th><th>Paper</th><th>Marks</th><th>Language</th><th>Filename</th><th>Actions</th></tr></thead><tbody id="subsBody"></tbody></table></div></section>
<section id="content" class="tabsec hidden"><div class="card" style="border:2px solid #dbe7ff"><h2>📥 Daily Questions — Questions + Model Answers + Rubric</h2><p style="color:#667085">Questions + Model Answers एक file में Q1./ANS1., Q2./ANS2. ... format में upload करें। Parser automatically question और answer अलग करेगा। Hindi और English date-wise अलग रहेंगे।</p><div class="formgrid"><input id="dqDate" class="input" type="date"><select id="dqLang" class="input"><option>Hindi</option><option>English</option></select><select id="dqPaper" class="input"><option>GS1</option><option>GS2</option><option>GS3</option><option>GS4</option><option>GS5</option><option>GS6</option><option>GENERAL_HINDI</option><option>ESSAY</option></select></div><label>1. Questions + Model Answers — paste or upload</label>
<div id="dqPaste" class="input" contenteditable="true" style="min-height:180px;line-height:1.55;margin-top:6px" data-placeholder="Q1. Question...&#10;ANS1. Model Answer...&#10;Q2. Question...&#10;ANS2. Model Answer..."></div>
<input id="dqFile" class="input" type="file" accept=".txt,.text,.md,.html,.htm" style="margin-top:8px">
<small style="display:block;color:#667085;margin-top:5px">Q1./ANS1. parser. Rich-text HTML is supported and preserved.</small>
<label style="display:block;margin-top:10px">2. Rubric</label><input id="dqRubricFile" class="input" type="file" accept=".txt,.text,.md,.html,.htm" required><div class="toolbar" style="margin-top:10px"><button class="btn blue" onclick="uploadDQ()">💾 Save Daily Questions + Rubric</button><button class="btn green" onclick="sendDQ()">📨 Send DQ PDF on Chat</button></div><small id="dqStatus" style="color:#667085"></small></div><div class="card"><div class="sectionhead"><div><h2>📝 Daily Questions + Model Answers</h2><p style="color:#667085;margin-top:-8px">Manual bulk entry भी उपलब्ध है।</p></div><div class="toolbar"><button class="btn blue" onclick="sendContentPdf()">📨 Send PDF on Chat</button><button class="btn ghost" onclick="addQuestionRow()">➕ Add Question</button></div></div><div id="bulkQuestions"></div><div class="toolbar" style="margin-top:12px"><button class="btn green" onclick="saveBulkContent()">💾 Save All Questions</button><button class="btn ghost" onclick="clearQuestionRows()">Clear Draft</button></div></div><div class="section tablewrap"><table class="table"><thead><tr><th>ID</th><th>Paper</th><th>Language</th><th>Question</th><th>Created</th><th>Action</th></tr></thead><tbody id="contentBody"></tbody></table></div></section>
<section id="pyqs" class="tabsec hidden"><div class="card" style="border:2px solid #dbe7ff"><h2>📚 PYQ Upload</h2><p style="color:#667085">Exam + Year + Paper + Language चुनें। Questions + Model Answers file Q1./ANS1. format में और Rubric अलग file में upload करें। Maximum 20 questions। केवल uploaded और rubric-complete PYQs evaluation में selectable होंगे।</p><div class="formgrid"><select id="pyqExam" class="input"><option value="UPPCS">UPPCS</option><option value="BPSC">BPSC</option><option value="RO_ARO">RO/ARO</option><option value="BEO">BEO</option></select><input id="pyqYear" class="input" type="number" placeholder="Year"><select id="pyqPaper" class="input"><option>GS1</option><option>GS2</option><option>GS3</option><option>GS4</option><option>GS5</option><option>GS6</option><option>GENERAL_HINDI</option><option>ESSAY</option></select><select id="pyqLang" class="input"><option>Hindi</option><option>English</option></select></div><label>1. Questions + Model Answers — paste or upload (maximum 20)</label>
<div id="pyqPaste" class="input" contenteditable="true" style="min-height:180px;line-height:1.55;margin-top:6px" data-placeholder="Q1. Question...&#10;ANS1. Model Answer..."></div>
<input id="pyqFile" class="input" type="file" accept=".txt,.text,.md,.html,.htm" style="margin-top:8px">
<small style="display:block;color:#667085;margin-top:5px">Q1./ANS1. parser. Rich-text HTML is supported and preserved.</small>
<label style="display:block;margin-top:10px">2. Rubric</label><input id="pyqRubricFile" class="input" type="file" accept=".txt,.text,.md,.html,.htm" required><div class="toolbar" style="margin-top:10px"><button class="btn blue" onclick="uploadPYQ()">💾 Save PYQ + Rubric</button></div><small id="pyqStatus" style="color:#667085"></small></div><div class="section tablewrap"><table class="table"><thead><tr><th>Exam</th><th>Year</th><th>Paper</th><th>Language</th><th>Questions</th><th>Rubric</th><th>Action</th></tr></thead><tbody id="pyqBody"></tbody></table></div></section>
<section id="admins" class="tabsec hidden"><div class="card"><h2>👑 Admin Management</h2><p>केवल Super Admin दूसरे Admin accounts जोड़/हटा सकता है।</p><div class="toolbar"><input id="adminId" class="input" placeholder="Telegram User ID"><button class="btn blue" onclick="addAdmin()">➕ Add Admin</button></div></div><div class="section tablewrap"><table class="table"><thead><tr><th>Telegram ID</th><th>Role</th><th>Status</th><th>Last Login</th><th>Action</th></tr></thead><tbody id="adminsBody"></tbody></table></div></section>
</div></div>
<div id="modal" class="modal hidden" onclick="if(event.target===this)closeModal()"><div class="modalbox"><button class="btn gray close" onclick="closeModal()">Close</button><div id="modalBody"></div></div></div>
<script>
let USER_ROWS=[],SUB_ROWS=[],CONTENT_DRAFT=[];
function esc(s){return String(s??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[x]))}
function fmtDate(s){if(!s)return '-';try{return new Date(s).toLocaleString('en-IN',{dateStyle:'medium',timeStyle:'short'})}catch{return s}}
async function api(p,o={}){o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});let r=await fetch(p,o),d=await r.json().catch(()=>({}));if(r.status==401||r.status==403)throw Error(d.error||'Unauthorized');if(!r.ok||d.ok===false)throw Error(d.error||'Request failed');return d}
async function onTelegramAuth(user){try{let d=await api('/api/admin/telegram-login',{method:'POST',body:JSON.stringify(user)});if(d.ok)show()}catch(e){document.getElementById('err').textContent='❌ '+e.message}}
async function show(){let m=await api('/api/admin/me');if(!m.authenticated){login.classList.remove('hidden');app.classList.add('hidden');return}login.classList.add('hidden');app.classList.remove('hidden');who.textContent=(m.role==='super_admin'?'👑 Super Admin':'👤 Admin')+' · '+m.telegram_user_id;adminTab.classList.toggle('hidden',m.role!=='super_admin');refreshAll()}
async function logout(){await fetch('/api/admin/logout',{method:'POST'});location.reload()}
function tab(id,el){document.querySelectorAll('.tabsec').forEach(x=>x.classList.add('hidden'));document.getElementById(id).classList.remove('hidden');document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));el.classList.add('active');if(id==='users')loadUsers();if(id==='groups')loadGroups();if(id==='submissions')loadSubmissions();if(id==='content'){if(!document.querySelector('.bulk-q-row'))addQuestionRow();loadContent();}if(id==='pyqs')loadPYQs();if(id==='admins')loadAdmins()}
async function refreshAll(){try{let [s,u,p]=await Promise.all([api('/api/admin/stats'),api('/api/admin/users?limit=500'),api('/api/admin/submissions?limit=100')]);stats.innerHTML=`<div class="card stat"><small>Students</small><b>${s.users}</b></div><div class="card stat"><small>Groups</small><b>${s.groups}</b></div><div class="card stat"><small>Evaluations</small><b>${s.submissions}</b></div><div class="card stat"><small>Average</small><b>${s.average_percentage}%</b></div><div class="card stat"><small>Marks</small><b>${s.total_obtained}/${s.total_max}</b></div><div class="card stat"><small>Completion</small><b>${s.completed_submissions}</b></div>`;renderPaperStats(s.paper_stats||{});renderRecent(p.items||[]);USER_ROWS=u.items||[];SUB_ROWS=p.items||[]}catch(e){if(e.message==='Unauthorized')location.reload();else alert(e.message)}}
function renderPaperStats(ps){paperStats.innerHTML=Object.entries(ps).map(([k,v])=>`<div class="metric"><b>${esc(k)}</b><span>${v.submissions} copies · ${v.average_percentage}%</span><div class="bar" style="margin-top:8px"><i style="width:${Math.min(100,Math.max(0,v.average_percentage))}%"></i></div></div>`).join('')||'<div class="empty">अभी कोई evaluation नहीं है।</div>'}
function renderRecent(rows){recent.innerHTML=rows.slice(0,10).map(x=>`<div style="padding:12px 0;border-bottom:1px solid #eee"><b>${esc(x.paper)} · ${esc(x.obtained)}/${esc(x.max)}</b> · ${esc(x.user_id)} <span style="color:#667085">${fmtDate(x.created_at)}</span><br><small>${esc(x.filename||'')}</small></div>`).join('')||'<div class="empty">अभी कोई evaluation नहीं है।</div>'}
function filterUsers(){let q=userSearch.value.toLowerCase();renderUsers(USER_ROWS.filter(x=>(x.id+' '+x.name+' '+(x.username||'')).toLowerCase().includes(q)))}
function renderUsers(rows){usersBody.innerHTML=rows.map(x=>`<tr><td><b>${esc(x.name||'Unknown')}</b><br><small>${esc(x.username?'@'+x.username:'')}<br>${esc(x.id)}</small></td><td>${x.blocked?'<span class="pill bad">Blocked</span>':x.access_type==='trial'?`<span class="pill ok">Trial</span><br><small>${x.trial_copies_used}/${x.trial_copies_limit} copies · ${x.trial_questions_used}/${x.trial_questions_limit} Q</small>`:x.allowed?'<span class="pill ok">Full Access</span>':'<span class="pill">No Evaluation Access</span>'}</td><td>${x.submissions}</td><td><b>${x.average_percentage||0}%</b><br><small>${x.obtained||0}/${x.max||0}</small></td><td>${fmtDate(x.last_seen)}</td><td><button class="btn ${x.blocked?'green':'red'}" onclick="userAccess('${esc(x.id)}',${x.blocked?'false':'true'})">${x.blocked?'Allow':'Block'}</button></td><td><button class="btn blue" onclick="userDetail('${esc(x.id)}')">View</button></td></tr>`).join('')||'<tr><td colspan="7" class="empty">कोई user नहीं मिला।</td></tr>'}
async function loadUsers(){try{let d=await api('/api/admin/users?limit=500');USER_ROWS=d.items||[];filterUsers()}catch(e){alert(e.message)}}
async function addStudent(){let id=newStudentId.value.trim();if(!/^-?\\d+$/.test(id))return alert('Valid Telegram User ID डालें');try{await api('/api/admin/users/create',{method:'POST',body:JSON.stringify({telegram_user_id:id,access_type:(document.getElementById('newStudentAccess')||{}).value||'full'})});newStudentId.value='';alert('✅ Student added और access enabled');await loadUsers();refreshAll()}catch(e){alert(e.message)}}

async function userAccess(id,blocked){await api('/api/admin/users/'+encodeURIComponent(id)+'/access',{method:'PATCH',body:JSON.stringify({blocked})});await loadUsers();refreshAll()}
async function userDetail(id){try{let d=await api('/api/admin/users/'+encodeURIComponent(id)+'/performance');let u=d.user,p=d.paper_stats||{};modalBody.innerHTML=`<h2>👤 ${esc(u.name||'Student')}</h2><p><b>Telegram ID:</b> ${esc(u.id)} ${u.username?' · @'+esc(u.username):''}</p><div class="metricgrid"><div class="metric">Copies<b>${u.submissions}</b></div><div class="metric">Average<b>${u.average_percentage}%</b></div><div class="metric">Obtained<b>${u.obtained}/${u.max}</b></div><div class="metric">Last Seen<b style="font-size:14px">${fmtDate(u.last_seen)}</b></div></div><h3 style="margin-top:22px">GS-wise Performance</h3><div class="metricgrid">${Object.entries(p).map(([k,v])=>`<div class="metric"><b>${esc(k)}</b><span>${v.submissions} copies · ${v.average_percentage}%</span><div class="bar" style="margin-top:8px"><i style="width:${Math.min(100,v.average_percentage)}%"></i></div></div>`).join('')}</div><h3 style="margin-top:22px">Recent Copies</h3><div class="tablewrap"><table class="table"><thead><tr><th>Date</th><th>Paper</th><th>Marks</th><th>Language</th><th>PDF</th></tr></thead><tbody>${(d.recent||[]).map(x=>`<tr><td>${fmtDate(x.created_at)}</td><td>${esc(x.paper)}</td><td><b>${x.obtained}/${x.max}</b></td><td>${esc(x.language||'-')}</td><td><button class="btn blue" onclick="pdf('${esc(x.id)}')">Open</button></td></tr>`).join('')}</tbody></table></div>`;modal.classList.remove('hidden')}catch(e){alert(e.message)}}
function filterSubs(){let q=subSearch.value.toLowerCase();renderSubs(SUB_ROWS.filter(x=>(x.user_id+' '+(x.filename||'')).toLowerCase().includes(q)))}
function renderSubs(rows){subsBody.innerHTML=rows.map(x=>`<tr><td>${fmtDate(x.created_at)}</td><td>${esc(x.user_id)}</td><td><b>${esc(x.paper)}</b></td><td><b>${esc(x.obtained)}/${esc(x.max)}</b></td><td>${esc(x.language||'-')}</td><td>${esc(x.filename||'-')}</td><td><button class="btn blue" onclick="submissionDetail('${esc(x.id)}')">Details</button> <button class="btn ghost" onclick="pdf('${esc(x.id)}')">PDF</button></td></tr>`).join('')||'<tr><td colspan="7" class="empty">कोई evaluation नहीं मिला।</td></tr>'}

async function loadGroups(){try{let d=await api('/api/admin/groups');groupsBody.innerHTML=(d.items||[]).map(x=>`<tr><td><b>${esc(x.title||'Untitled')}</b><br><small>${esc(x.id)}</small></td><td>${esc(x.type||'-')}</td><td>${x.blocked?'<span class="pill bad">Blocked</span>':x.allowed?'<span class="pill ok">Allowed</span>':'<span class="pill">Not Allowed</span>'}</td><td>-</td><td><button class="btn ${x.allowed?'red':'green'}" onclick="groupAccess('${esc(x.id)}',${x.allowed?'false':'true'})">${x.allowed?'Remove Access':'Allow Access'}</button></td></tr>`).join('')||'<tr><td colspan="5" class="empty">अभी कोई Telegram group registered नहीं है।</td></tr>'}catch(e){alert(e.message)}}
async function addGroup(){let id=newGroupId.value.trim();if(!/^-?\\d+$/.test(id))return alert('Valid Telegram Group ID डालें');try{await api('/api/admin/groups/create',{method:'POST',body:JSON.stringify({telegram_group_id:id,})});newGroupId.value='';alert('✅ Group added और access enabled');await loadGroups();refreshAll()}catch(e){alert(e.message)}}

async function groupAccess(id,allowed){await api('/api/admin/groups/'+encodeURIComponent(id),{method:'PATCH',body:JSON.stringify({allowed})});loadGroups()}
async function loadSubmissions(){try{let paper=subPaper.value;let d=await api('/api/admin/submissions?limit=300'+(paper?'&paper='+encodeURIComponent(paper):''));SUB_ROWS=d.items||[];filterSubs()}catch(e){alert(e.message)}}
async function submissionDetail(id){try{let d=await api('/api/admin/submissions/'+encodeURIComponent(id));let s=d.submission;modalBody.innerHTML=`<h2>📄 ${esc(s.paper)} Evaluation</h2><p><b>User:</b> ${esc(s.user)} · <b>Language:</b> ${esc(s.language||'-')} · <b>Marks:</b> ${esc(s.obtained)}/${esc(s.max)}</p><p><b>Original:</b> ${esc(s.original_filename||'-')}<br><b>Evaluated:</b> ${esc(s.filename||'-')}</p><div class="card"><b>Overall Feedback</b><p>${esc(s.feedback||'-')}</p></div><h3>Question-wise</h3><div class="tablewrap"><table class="table"><thead><tr><th>Q</th><th>Pages</th><th>Marks</th><th>Demand</th><th>Skipped</th><th>Comment</th></tr></thead><tbody>${(d.questions||[]).map(q=>`<tr><td>${q.number}</td><td>${q.start_page}-${q.end_page}</td><td>${q.obtained}/${q.max}</td><td>${esc((q.fulfilled||[]).join(' • '))}</td><td>${esc((q.skipped||[]).join(' • ')||'—')}</td><td>${esc(q.comment||'')}</td></tr>`).join('')}</tbody></table></div><h3>Examiner Comments / Annotations</h3><div>${(d.comments||[]).map(c=>`<div style="padding:8px;border-bottom:1px solid #eee"><b>Page ${c.page}</b> · ${esc(c.color||'red')}<br>${esc(c.comment)}</div>`).join('')||'<div class="empty">No comments</div>'}</div><div style="margin-top:15px"><button class="btn blue" onclick="pdf('${esc(id)}')">Open Evaluated PDF</button></div>`;modal.classList.remove('hidden')}catch(e){alert(e.message)}}
async function pdf(id){let r=await fetch('/api/admin/submissions/'+encodeURIComponent(id)+'/pdf');if(!r.ok)return alert('PDF access denied');let b=await r.blob(),u=URL.createObjectURL(b);window.open(u,'_blank')}
async function loadContent(){try{let d=await api('/api/admin/content');contentBody.innerHTML=(d.items||[]).map(x=>`<tr><td>${x.id}</td><td>${esc(x.paper)}</td><td>${esc(x.language)}</td><td>${esc(x.question).slice(0,260)}</td><td>${fmtDate(x.created_at)}</td><td><button class="btn red" onclick="deleteContent(${x.id})">Delete</button></td></tr>`).join('')||'<tr><td colspan="6" class="empty">अभी कोई daily content नहीं है।</td></tr>'}catch(e){alert(e.message)}}
async function sendContentPdf(){
  try{
    let r=await fetch('/api/admin/content/send-pdf',{method:'POST'});
    let d=await r.json().catch(()=>({}));
    if(!r.ok||d.ok===false)throw Error(d.error||'PDF send failed');
    alert('✅ '+(d.message||'Branded PDF sent to your Telegram chat.'));
  }catch(e){alert(e.message)}
}
function richCmd(btn,cmd){const ed=btn.closest('.bulk-q-row').querySelector('.bulk-answer');ed.focus();document.execCommand(cmd,false,null)}
function insertTable(btn){const ed=btn.closest('.bulk-q-row').querySelector('.bulk-answer');ed.focus();document.execCommand('insertHTML',false,'<table border="1" style="border-collapse:collapse;width:100%"><tr><th>Heading</th><th>Heading</th></tr><tr><td>Cell</td><td>Cell</td></tr></table><p><br></p>')}
function addQuestionRow(data={}){const box=document.getElementById('bulkQuestions');const row=document.createElement('div');row.className='card bulk-q-row';row.style.cssText='margin-top:10px;border:1px solid #dbe2ea';row.innerHTML=`<div class="toolbar" style="margin-bottom:8px"><b>Question #${box.children.length+1}</b><button class="btn red" style="margin-left:auto" onclick="this.closest('.bulk-q-row').remove();renumberRows()">Remove</button></div><div class="formgrid" style="grid-template-columns:140px 140px 1fr"><select class="input bulk-paper"><option>GS1</option><option>GS2</option><option>GS3</option><option>GS4</option><option>GS5</option><option>GS6</option></select><select class="input bulk-lang"><option>Hindi</option><option>English</option></select><textarea class="input bulk-question" rows="4" placeholder="Daily Question"></textarea></div><div class="rich-toolbar" style="margin-top:8px"><button type="button" class="btn ghost" onclick="richCmd(this,'bold')"><b>B</b></button><button type="button" class="btn ghost" onclick="richCmd(this,'italic')"><i>I</i></button><button type="button" class="btn ghost" onclick="richCmd(this,'insertUnorderedList')">• List</button><button type="button" class="btn ghost" onclick="richCmd(this,'insertOrderedList')">1. List</button><button type="button" class="btn ghost" onclick="insertTable(this)">▦ Table</button></div><div class="input bulk-answer" contenteditable="true" style="width:100%;min-height:180px;margin-top:6px;line-height:1.5" data-placeholder="Model Answer — rich text / table support"></div>`;box.appendChild(row);row.querySelector('.bulk-paper').value=data.paper||'GS1';row.querySelector('.bulk-lang').value=data.language||'Hindi';row.querySelector('.bulk-question').value=data.question||'';row.querySelector('.bulk-answer').innerHTML=data.model_answer||'';renumberRows()}
function renumberRows(){document.querySelectorAll('.bulk-q-row').forEach((r,i)=>{const b=r.querySelector('b');if(b)b.textContent='Question #'+(i+1)})}
function clearQuestionRows(){document.getElementById('bulkQuestions').innerHTML='';addQuestionRow()}
async function saveBulkContent(){const rows=[...document.querySelectorAll('.bulk-q-row')];if(!rows.length)return alert('कम से कम एक question जोड़ें');const items=rows.map(r=>({paper:r.querySelector('.bulk-paper').value,language:r.querySelector('.bulk-lang').value,question:r.querySelector('.bulk-question').value.trim(),model_answer:r.querySelector('.bulk-answer').innerHTML.trim()}));const invalid=items.findIndex(x=>!x.question);if(invalid>=0)return alert(`Question #${invalid+1} में question लिखें`);try{const d=await api('/api/admin/content/bulk',{method:'POST',body:JSON.stringify({items})});alert(`✅ ${d.inserted} Daily Questions save हुए${d.skipped?`\n⚠️ ${d.skipped} rows skipped`:''}`);clearQuestionRows();await loadContent()}catch(e){alert(e.message)}}
async function deleteContent(id){if(!confirm('यह content delete करना है?'))return;await api('/api/admin/content/'+id,{method:'DELETE'});loadContent()}
async function loadAdmins(){try{let d=await api('/api/admin/admins');adminsBody.innerHTML=(d.items||[]).map(x=>`<tr><td>${esc(x.id)}</td><td><span class="pill role">${esc(x.role)}</span></td><td>${x.active?'<span class="pill ok">Active</span>':'<span class="pill bad">Disabled</span>'}</td><td>${fmtDate(x.last_login)}</td><td>${x.role==='super_admin'?'—':`<button class="btn red" onclick="removeAdmin('${esc(x.id)}')">Remove</button>`}</td></tr>`).join('')}catch(e){alert(e.message)}}
async function addAdmin(){let id=adminId.value.trim();if(!/^\\d+$/.test(id))return alert('Numeric Telegram User ID डालें');await api('/api/admin/admins',{method:'POST',body:JSON.stringify({telegram_user_id:id})});adminId.value='';loadAdmins()}
async function removeAdmin(id){if(!confirm('इस Admin का access हटाना है?'))return;await api('/api/admin/admins/'+encodeURIComponent(id),{method:'DELETE'});loadAdmins()}
async function readTextFile(input){let f=input&&input.files&&input.files[0];return f?await f.text():''}
function richInput(id,fileId){let box=document.getElementById(id),html=(box?.innerHTML||'').trim();return html&&html!=='<br>'?html:readTextFile(document.getElementById(fileId))}
async function uploadDQ(){try{let qa=await richInput('dqPaste','dqFile');let rubric=await readTextFile(document.getElementById('dqRubricFile'));if(!qa)return alert('Questions + Model Answers paste or upload करें');if(!rubric)return alert('Rubric file upload करें');let d=await api('/api/admin/daily/upload',{method:'POST',body:JSON.stringify({date:dqDate.value||new Date().toISOString().slice(0,10),language:dqLang.value,paper:dqPaper.value,qa_text:qa,rubric:rubric})});dqStatus.textContent='Saved: '+d.count+' questions + Rubric. Set: '+d.set_id;loadContent()}catch(e){dqStatus.textContent='Error: '+e.message}}
async function sendDQ(){try{let d=await api('/api/admin/dq/send-pdf?content_date='+(encodeURIComponent(dqDate.value||''))+'&language='+encodeURIComponent(dqLang.value),{method:'POST'});alert(d.message||'DQ PDF sent.')}catch(e){alert(e.message)}}
async function uploadPYQ(){try{let qa=await richInput('pyqPaste','pyqFile');let rubric=await readTextFile(document.getElementById('pyqRubricFile'));if(!qa)return alert('Questions + Model Answers paste or upload करें');if(!rubric)return alert('Rubric file upload करें');let d=await api('/api/admin/pyq/upload',{method:'POST',body:JSON.stringify({exam:pyqExam.value,year:pyqYear.value,paper:pyqPaper.value,language:pyqLang.value,qa_text:qa,rubric:rubric})});pyqStatus.textContent='Saved: '+d.count+' questions + Rubric. Set: '+d.set_id;window.lastPYQSet=d.set_id;loadPYQs()}catch(e){pyqStatus.textContent='Error: '+e.message}}
async function loadPYQs(){try{let d=await api('/api/admin/content-sets');let rows=(d.items||[]).filter(x=>x.content_type==='pyq');pyqBody.innerHTML=rows.map(x=>`<tr><td>${esc(x.exam)}</td><td>${esc(x.year||'-')}</td><td>${esc(x.paper)}</td><td>${esc(x.language)}</td><td>${esc(x.question_count||0)}</td><td>${x.rubric?'✅ Uploaded':'❌ Missing'}</td><td><button class="btn ghost" onclick="prompt('Set ID', '${esc(x.id)}')">ID</button></td></tr>`).join('')||'<tr><td colspan="7" class="empty">No PYQ uploaded.</td></tr>'}catch(e){pyqStatus.textContent='❌ '+e.message}}
function closeModal(){modal.classList.add('hidden');modalBody.innerHTML=''}
show();
</script></body></html>
"""
    html = html.replace("__BOT_USERNAME__", bot_username)
    return HTMLResponse(content=html)

@app.post("/api/admin/daily/upload")
async def admin_daily_upload(request: Request):
    admin = current_admin(request)
    if not admin: return admin_denied()
    ensure_new_schema()
    body = await request.json()
    content_date = str(body.get("date") or datetime.now().date()).strip()
    language = "English" if str(body.get("language") or "Hindi").lower().startswith("en") else "Hindi"
    paper = str(body.get("paper") or "GS1").upper().strip()
    qa_text = str(body.get("qa_text") or "")
    rubric = str(body.get("rubric") or "").strip()
    if not rubric: return app_error("Daily Questions के लिए Rubric upload करना जरूरी है।",400)
    if paper not in PAPER_OPTIONS[:-1]:
        return app_error("Valid GS paper select करें।", 400)
    items = parse_qa_pairs(qa_text)
    if not items: return app_error("Q1./ANS1. format में valid questions नहीं मिले।", 400)
    sid = save_content_set("daily", "UPPCS", paper, language, rubric=rubric, content_date=content_date, title=f"Daily {content_date} {language}", items=items)
    return {"ok":True,"set_id":sid,"count":len(items),"language":language,"content_date":content_date,"rubric_saved":bool(rubric)}


@app.post("/api/admin/pyq/upload")
async def admin_pyq_upload(request: Request):
    admin = current_admin(request)
    if not admin: return admin_denied()
    ensure_new_schema()
    body = await request.json()
    exam = str(body.get("exam") or "UPPCS").upper().strip()
    year_raw = str(body.get("year") or "").strip()
    paper = str(body.get("paper") or "GS1").upper().strip()
    language = "English" if str(body.get("language") or "Hindi").lower().startswith("en") else "Hindi"
    qa_text = str(body.get("qa_text") or "")
    rubric = str(body.get("rubric") or "").strip()
    if not rubric: return app_error("PYQ evaluation के लिए Rubric upload करना जरूरी है।",400)
    if exam not in EVALUATION_EXAMS: return app_error("Invalid exam selected.",400)
    if paper not in PAPER_OPTIONS: return app_error("Invalid paper selected.",400)
    try: year = int(year_raw) if year_raw else None
    except ValueError: return app_error("Valid year required.",400)
    items = parse_qa_pairs(qa_text)
    if not items: return app_error("Q1./ANS1. format में valid questions नहीं मिले।",400)
    if len(items) > 20: return app_error("एक PYQ paper में अधिकतम 20 questions allowed हैं।",400)
    sid = save_content_set("pyq", exam, paper, language, rubric=rubric, year=year, title=f"{exam} {year or ''} {paper}", items=items)
    return {"ok":True,"set_id":sid,"count":len(items),"rubric_saved":bool(rubric)}


@app.post("/api/admin/content-rubric")
async def admin_content_rubric(request: Request):
    admin = current_admin(request)
    if not admin: return admin_denied()
    ensure_new_schema()
    body = await request.json()
    sid = str(body.get("set_id") or "").strip()
    rubric = str(body.get("rubric") or "").strip()
    if not sid or not rubric: return app_error("Set ID और Rubric दोनों required हैं।",400)
    with engine.begin() as conn:
        result = conn.exec_driver_sql("UPDATE content_sets SET rubric=%s,updated_at=%s WHERE id=%s AND is_active=TRUE", (rubric,_utcnow(),sid))
    if not result.rowcount: return app_error("Content set नहीं मिला।",404)
    return {"ok":True,"set_id":sid,"rubric_saved":True}


@app.get("/api/admin/content-sets")
def admin_content_sets(request: Request):
    if not admin_authorized(request):
        return admin_denied()
    ensure_new_schema()
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("""
            SELECT cs.id,cs.content_type,cs.exam,cs.paper,cs.language,cs.content_date,cs.year,
                   cs.title,cs.source_filename,cs.rubric,COUNT(ci.id) AS question_count
            FROM content_sets cs
            LEFT JOIN content_items ci ON ci.set_id=cs.id
            WHERE cs.is_active=TRUE
            GROUP BY cs.id
            ORDER BY cs.created_at DESC
        """).mappings().all()
    return {"ok":True,"items":[dict(r) for r in rows]}

@app.get("/api/evaluation/catalog")
def public_evaluation_catalog():
    return {"ok":True, **evaluation_catalog()}

@app.get("/api/content/catalog")
def public_content_catalog():
    ensure_new_schema()
    with engine.connect() as conn:
        rows=conn.exec_driver_sql("SELECT id,content_type,exam,paper,language,content_date,year,title FROM content_sets WHERE is_active=TRUE ORDER BY created_at DESC").mappings().all()
    return {"ok":True,"items":[dict(r) for r in rows]}

@app.get("/api/admin/stats")
def admin_stats(request: Request):
    if not admin_authorized(request): return admin_denied()
    s=SessionLocal()
    try:
        rows=s.query(DBSubmission).order_by(desc(DBSubmission.created_at)).all()
        ob=sum(float(x.total_obtained_marks or 0) for x in rows); mx=sum(float(x.total_max_marks or 0) for x in rows)
        paper_stats={}
        for x in rows:
            b=paper_stats.setdefault(x.paper,{"submissions":0,"obtained":0.0,"max":0.0})
            b["submissions"]+=1; b["obtained"]+=float(x.total_obtained_marks or 0); b["max"]+=float(x.total_max_marks or 0)
        for b in paper_stats.values():
            b["average_percentage"]=round(b["obtained"]/b["max"]*100,1) if b["max"] else 0
            b["obtained"]=round(b["obtained"],1); b["max"]=round(b["max"],1)
        return {"ok":True,"users":s.query(DBUser).count(),"groups":s.query(DBGroup).count(),"submissions":len(rows),"completed_submissions":sum(1 for x in rows if x.status=="completed"),"total_obtained":round(ob,1),"total_max":round(mx,1),"average_percentage":round(ob/mx*100,1) if mx else 0,"paper_stats":paper_stats}
    finally:s.close()

@app.get("/api/admin/admins")
def admin_admins(request: Request):
    if not super_admin_authorized(request): return admin_forbidden()
    s=SessionLocal()
    try:
        rows=s.execute(__import__("sqlalchemy").text("SELECT telegram_user_id,role,is_active,last_login_at FROM admin_users ORDER BY created_at DESC")).mappings().all()
        items=[]
        if SUPER_ADMIN_TELEGRAM_ID: items.append({"id":SUPER_ADMIN_TELEGRAM_ID,"role":"super_admin","active":True,"last_login":None})
        items += [{"id":str(r["telegram_user_id"]),"role":str(r["role"]),"active":bool(r["is_active"]),"last_login":r["last_login_at"].isoformat() if r["last_login_at"] else None} for r in rows if str(r["telegram_user_id"]) != SUPER_ADMIN_TELEGRAM_ID]
        return {"ok":True,"items":items}
    finally:s.close()

@app.post("/api/admin/admins")
async def admin_add_admin(request: Request):
    if not super_admin_authorized(request): return admin_forbidden()
    body=await request.json(); uid=str(body.get("telegram_user_id","")).strip()
    if not uid:return {"ok":False,"error":"Telegram User ID required"}
    if uid==SUPER_ADMIN_TELEGRAM_ID:return {"ok":False,"error":"Already Super Admin"}
    s=SessionLocal()
    try:
        now=_utcnow()
        s.execute(__import__("sqlalchemy").text("INSERT INTO admin_users(telegram_user_id,role,is_active,created_at) VALUES (:uid,'admin',TRUE,:now) ON CONFLICT (telegram_user_id) DO UPDATE SET role='admin',is_active=TRUE"),{"uid":uid,"now":now})
        u=s.get(DBUser,uid)
        if u is None:
            u=DBUser(telegram_user_id=uid,is_allowed=True,is_blocked=False,access_type='full',created_at=now,last_seen_at=now)
            s.add(u)
        else:
            u.is_allowed=True; u.is_blocked=False; u.access_type='full'; u.last_seen_at=now
        s.commit(); return {"ok":True}
    finally:s.close()

@app.delete("/api/admin/admins/{user_id}")
def admin_remove_admin(user_id: str, request: Request):
    if not super_admin_authorized(request): return admin_forbidden()
    if str(user_id)==SUPER_ADMIN_TELEGRAM_ID:return {"ok":False,"error":"Super Admin cannot be removed"}
    s=SessionLocal()
    try:
        s.execute(__import__("sqlalchemy").text("UPDATE admin_users SET is_active=FALSE WHERE telegram_user_id=:uid"),{"uid":str(user_id)}); s.commit(); return {"ok":True}
    finally:s.close()

@app.get("/api/admin/users")
def admin_users(request: Request, limit: int=500):
    if not admin_authorized(request): return admin_denied()
    s=SessionLocal()
    try:
        rows=s.query(DBUser).order_by(desc(DBUser.last_seen_at)).limit(min(max(limit,1),1000)).all()
        items=[]
        for u in rows:
            subs=s.query(DBSubmission).filter(DBSubmission.telegram_user_id==u.telegram_user_id).all()
            ob=sum(float(x.total_obtained_marks or 0) for x in subs); mx=sum(float(x.total_max_marks or 0) for x in subs)
            items.append({"id":u.telegram_user_id,"name":" ".join(x for x in [u.first_name,u.last_name] if x),"username":u.username,"allowed":u.is_allowed,"blocked":u.is_blocked,"access_type":getattr(u,"access_type","none"),"trial_copies_used":int(getattr(u,"trial_copies_used",0) or 0),"trial_copies_limit":int(getattr(u,"trial_copies_limit",3) or 3),"trial_questions_used":int(getattr(u,"trial_questions_used",0) or 0),"trial_questions_limit":int(getattr(u,"trial_questions_limit",10) or 10),"submissions":len(subs),"obtained":round(ob,1),"max":round(mx,1),"average_percentage":round(ob/mx*100,1) if mx else 0,"last_seen":u.last_seen_at.isoformat() if u.last_seen_at else None})
        return {"ok":True,"items":items}
    finally:s.close()

@app.post("/api/admin/users/create")
async def admin_user_create(request: Request):
    if not admin_authorized(request):
        return admin_denied()
    body = await request.json()
    uid = str(body.get("telegram_user_id", "")).strip()
    access_type = str(body.get("access_type", "full")).strip().lower() or "full"
    if access_type not in ("full","trial","none"): access_type="full"
    username = str(body.get("username", "")).strip().lstrip("@") or None
    first_name = str(body.get("first_name", "")).strip() or None
    last_name = str(body.get("last_name", "")).strip() or None
    if not re.fullmatch(r"-?\d+", uid):
        return {"ok": False, "error": "Valid Telegram User ID required"}
    s = SessionLocal()
    try:
        now = _utcnow(); u = s.get(DBUser, uid)
        if u is None:
            u = DBUser(telegram_user_id=uid, username=username, first_name=first_name, last_name=last_name, is_allowed=(access_type=="full"), is_blocked=False, access_type=access_type, created_at=now, last_seen_at=now)
            s.add(u)
        else:
            if username is not None: u.username=username
            if first_name is not None: u.first_name=first_name
            if last_name is not None: u.last_name=last_name
            u.is_allowed=(access_type=="full"); u.access_type=access_type; u.is_blocked=False; u.last_seen_at=now
        s.commit(); return {"ok":True,"id":uid,"access_type":access_type,"message":"Student added"}
    except Exception as e:
        s.rollback(); return {"ok":False,"error":str(e)[:200]}
    finally: s.close()

@app.patch("/api/admin/users/{user_id}/access")
async def admin_user_access(user_id: str, request: Request):
    if not admin_authorized(request): return admin_denied()
    body=await request.json(); s=SessionLocal()
    try:
        u=s.get(DBUser,user_id)
        if not u:return {"ok":False,"error":"User not found"}
        if "blocked" in body:u.is_blocked=bool(body["blocked"])
        if "allowed" in body:u.is_allowed=bool(body["allowed"])
        if "access_type" in body:
            at=str(body["access_type"]).lower().strip()
            if at not in ("full","trial","none"): return {"ok":False,"error":"access_type must be full, trial or none"}
            u.access_type=at; u.is_allowed=(at=="full")
        if body.get("reset_trial"):
            u.trial_copies_used=0; u.trial_questions_used=0
        if "trial_copies_limit" in body:u.trial_copies_limit=max(1,int(body["trial_copies_limit"]))
        if "trial_questions_limit" in body:u.trial_questions_limit=max(1,int(body["trial_questions_limit"]))
        s.commit();return {"ok":True,"access_type":u.access_type}
    finally:s.close()

@app.get("/api/admin/groups")
def admin_groups(request: Request):
    if not admin_authorized(request): return admin_denied()
    s=SessionLocal()
    try:
        rows=s.query(DBGroup).order_by(desc(DBGroup.last_seen_at)).limit(500).all();return {"ok":True,"items":[{"id":g.telegram_group_id,"title":g.title,"type":g.group_type,"allowed":g.is_allowed,"blocked":g.is_blocked} for g in rows]}
    finally:s.close()

@app.post("/api/admin/groups/create")
async def admin_group_create(request: Request):
    if not admin_authorized(request):
        return admin_denied()
    body=await request.json(); gid=str(body.get("telegram_group_id","")).strip(); title=str(body.get("title","")).strip() or None; group_type=str(body.get("group_type","supergroup")).strip() or "supergroup"
    if not re.fullmatch(r"-?\d+",gid): return {"ok":False,"error":"Valid Telegram Group ID required"}
    s=SessionLocal()
    try:
        now=_utcnow(); g=s.get(DBGroup,gid)
        if g is None:
            g=DBGroup(telegram_group_id=gid,title=title,group_type=group_type,is_allowed=True,is_blocked=False,created_at=now,last_seen_at=now); s.add(g)
        else:
            if title is not None: g.title=title
            g.group_type=group_type; g.is_allowed=True; g.is_blocked=False; g.last_seen_at=now
        s.commit(); return {"ok":True,"id":gid,"message":"Group added and access enabled"}
    except Exception as e:
        s.rollback(); return {"ok":False,"error":str(e)[:200]}
    finally: s.close()

@app.patch("/api/admin/groups/{group_id}")
async def admin_group_update(group_id: str, request: Request):
    if not admin_authorized(request): return admin_denied()
    body=await request.json(); s=SessionLocal()
    try:
        g=s.get(DBGroup,group_id)
        if not g:return {"ok":False,"error":"Group not found"}
        if "allowed" in body:g.is_allowed=bool(body["allowed"])
        if "blocked" in body:g.is_blocked=bool(body["blocked"])
        s.commit();return {"ok":True}
    finally:s.close()

@app.get("/api/admin/submissions")
def admin_submissions(request: Request, limit: int=100, paper: str=""):
    if not admin_authorized(request): return admin_denied()
    s=SessionLocal()
    try:
        q=s.query(DBSubmission).order_by(desc(DBSubmission.created_at))
        if paper:q=q.filter(DBSubmission.paper==paper.upper())
        rows=q.limit(min(max(limit,1),500)).all();return {"ok":True,"items":[{"id":x.id,"user_id":x.telegram_user_id,"paper":x.paper,"exam":x.exam,"evaluation_type":x.evaluation_type,"source_id":x.source_id,"obtained":x.total_obtained_marks,"max":x.total_max_marks,"language":x.copy_language,"filename":x.evaluated_filename,"created_at":x.created_at.isoformat() if x.created_at else None} for x in rows]}
    finally:s.close()

@app.get("/api/admin/submissions/{submission_id}")
def admin_submission_detail(submission_id: str, request: Request):
    if not admin_authorized(request): return admin_denied()
    s=SessionLocal()
    try:
        x=s.get(DBSubmission,submission_id)
        if not x:return {"ok":False,"error":"Submission not found"}
        qs=s.query(DBQuestion).filter(DBQuestion.submission_id==submission_id).all(); cs=s.query(DBPageComment).filter(DBPageComment.submission_id==submission_id).all(); aa=s.query(DBAnnotation).filter(DBAnnotation.submission_id==submission_id).all()
        return {"ok":True,"submission":{"id":x.id,"user":x.telegram_user_id,"user_id":x.telegram_user_id,"paper":x.paper,"exam":x.exam,"evaluation_type":x.evaluation_type,"source_id":x.source_id,"original_filename":x.original_filename,"filename":x.evaluated_filename,"obtained":x.total_obtained_marks,"max":x.total_max_marks,"language":x.copy_language,"feedback":x.overall_feedback,"created_at":x.created_at.isoformat() if x.created_at else None},"questions":[{"number":q.question_number,"start_page":q.start_page,"end_page":q.end_page,"obtained":q.obtained_marks,"max":q.max_marks,"demand":q.demand_parts,"fulfilled":q.fulfilled_parts,"skipped":q.skipped_parts,"intro_comment":q.intro_comment,"body_comment":q.body_comment,"conclusion_comment":q.conclusion_comment,"comment":q.end_page_comment} for q in qs],"comments":[{"page":c.page,"color":c.color,"comment":c.comment} for c in cs],"annotations":[{"page":a.page,"type":a.annotation_type,"color":a.color,"text":a.exact_text,"reason":a.reason,"box":a.box_2d} for a in aa]}
    finally:s.close()

@app.get("/api/admin/submissions/{submission_id}/pdf")
def admin_submission_pdf(submission_id: str, request: Request):
    if not admin_authorized(request): return Response(content=b"Unauthorized",status_code=401)
    s=SessionLocal()
    try:
        f=s.get(DBSubmissionPDF,submission_id); sub=s.get(DBSubmission,submission_id)
        if not f or not sub:return Response(content=b"Not found",status_code=404)
        return Response(content=bytes(f.pdf_bytes),media_type="application/pdf",headers={"Content-Disposition":f'inline; filename="{Path(sub.evaluated_filename).name}"'})
    finally:s.close()

@app.get("/api/admin/content/pdf")
def admin_content_pdf(request: Request, language: str = ""):
    if not admin_authorized(request):
        return admin_denied()
    ensure_admin_content_table()
    lang = str(language).strip()
    with engine.connect() as conn:
        if lang:
            rows = conn.exec_driver_sql(
                "SELECT id,paper,language,question,model_answer,created_at "
                "FROM daily_content WHERE is_active=TRUE AND language=%s ORDER BY id ASC",
                (lang,)
            ).mappings().all()
        else:
            rows = conn.exec_driver_sql(
                "SELECT id,paper,language,question,model_answer,created_at "
                "FROM daily_content WHERE is_active=TRUE ORDER BY id ASC"
            ).mappings().all()

    if not rows:
        return app_error("कोई Daily Question उपलब्ध नहीं है।", 404)

    W,H = 1240,1754
    pages=[]
    font_big=get_font(34)
    font_title=get_font(48)
    font_small=get_font(24)
    logo_path = str(STATIC_DIR / "branding" / "prana-logo.png")
    for idx,r in enumerate(rows,1):
        img=Image.new("RGB",(W,H),"white")
        draw=ImageDraw.Draw(img)
        if os.path.exists(logo_path):
            try:
                logo=Image.open(logo_path).convert("RGBA")
                logo.thumbnail((110,110))
                img.paste(logo,(70,55),logo)
            except Exception:
                pass
        draw.text((210,65),"PRANA PCS",font=font_title,fill=(20,20,20))
        draw.text((210,125),"LET'S PRANA • DAILY QUESTION + MODEL ANSWER",font=font_small,fill=(110,110,110))
        draw.text((70,190),f"{idx}. {r['paper']} • {r['language']}",font=font_big,fill=(20,20,20))
        y=260
        # Simple word wrapping for the branded PDF.
        def wrap_text(text, width_chars=52):
            words=str(text or "").split()
            lines=[]; cur=""
            for w in words:
                if len(cur)+len(w)+1 > width_chars:
                    lines.append(cur); cur=w
                else:
                    cur=(cur+" "+w).strip()
            if cur: lines.append(cur)
            return "\n".join(lines)
        draw.text((70,y),"QUESTION",font=font_small,fill=(120,90,0)); y+=42
        draw.multiline_text((70,y),wrap_text(r["question"],55),font=font_big,fill=(25,25,25),spacing=12)
        y += 360
        draw.text((70,y),"MODEL ANSWER",font=font_small,fill=(120,90,0)); y+=42
        draw.multiline_text((70,y),wrap_text(r["model_answer"],55),font=font_big,fill=(25,25,25),spacing=12)
        draw.text((70,H-70),f"PRANA PCS • Page {idx}/{len(rows)}",font=font_small,fill=(130,130,130))
        pages.append(img.convert("RGB"))

    out=io.BytesIO()
    doc=fitz.open()
    try:
        for img in pages:
            b=io.BytesIO(); img.save(b,format="PNG")
            page=doc.new_page(width=W*72/150,height=H*72/150)
            page.insert_image(page.rect,stream=b.getvalue())
        doc.save(out,garbage=4,deflate=True)
    finally:
        doc.close()

    response=Response(content=out.getvalue(),media_type="application/pdf")
    response.headers["Content-Disposition"]='attachment; filename="PRANA_PCS_Daily_Questions_Model_Answers.pdf"'
    return response


@app.post("/api/admin/content/bulk")
async def admin_content_bulk(request: Request):
    if not admin_authorized(request): return admin_denied()
    ensure_admin_content_table(); body=await request.json(); items=body.get("items",[])
    if not isinstance(items,list) or not items: return {"ok":False,"error":"At least one question is required"}
    now=_utcnow(); inserted=0; skipped=0; errors=[]
    with engine.begin() as conn:
        for idx,item in enumerate(items,1):
            paper=str(item.get("paper","GS1")).upper().strip(); language=str(item.get("language","Hindi")).strip() or "Hindi"; question=str(item.get("question","")).strip(); answer=str(item.get("model_answer","")).strip()
            if paper not in {"GS1","GS2","GS3","GS4","GS5","GS6"} or not question:
                skipped+=1; errors.append(f"Row {idx}: paper and question are required"); continue
            conn.exec_driver_sql("""INSERT INTO daily_content(paper,language,question,model_answer,is_active,created_at,updated_at) VALUES (%s,%s,%s,%s,TRUE,%s,%s)""",(paper,language,question,answer,now,now)); inserted+=1
    return {"ok":True,"inserted":inserted,"skipped":skipped,"errors":errors[:20],"message":f"{inserted} Daily Questions added"}

def build_daily_content_pdf(rows, language):
    """Build a branded, rich-text-friendly Daily Q&A PDF using PyMuPDF Story."""
    today = str(rows[0].get("content_date") or datetime.now().date()) if rows else datetime.now().strftime("%d %B %Y")
    socials = "Telegram                         Instagram\nYouTube                           WhatsApp"
    story = fitz.Story()
    css = """<style>body{font-family:sans-serif;color:#151922;font-size:11pt}h1{font-size:20pt;margin:0}h2{font-size:14pt;margin-top:18pt;color:#7b1e1e}h3{font-size:11pt;margin-top:12pt}table{border-collapse:collapse;width:100%}td,th{border:0.7pt solid #b8bec8;padding:5pt} .meta{color:#666;font-size:9pt}.footer{font-size:8pt;color:#666;border-top:0.7pt solid #bbb;padding-top:5pt}</style>"""
    parts=[css, f'<h1>PRANA PCS Mains AI</h1><div class="meta">{today} • {language}</div><hr>']
    seen_rubrics = set()
    for i,r in enumerate(rows,1):
        q=html.escape(str(r.get("question") or ""))
        ans=str(r.get("model_answer") or "")
        if not re.search(r'<(p|div|table|ul|ol|strong|em|h[1-6])\b', ans, re.I):
            ans='<p>'+html.escape(ans).replace('\n','<br>')+'</p>'
        parts.append(f'<h2>{i}. {html.escape(str(r["paper"]))} — Daily Question</h2><p><b>Question:</b> {q}</p><h3>Model Answer</h3>{ans}')
        rubric = str(r.get("rubric") or "").strip()
        if rubric:
            key = (str(r.get("set_id") or r.get("paper") or ""), rubric)
            if key not in seen_rubrics:
                seen_rubrics.add(key)
                rubric_html = rubric if re.search(r'<(p|div|table|ul|ol|strong|em|h[1-6])\b', rubric, re.I) else '<p>'+html.escape(rubric).replace('\n','<br>')+'</p>'
                parts.append(f'<h3>Rubric</h3>{rubric_html}')
    parts.append(f'<p class="footer">Telegram                         Instagram<br>YouTube                           WhatsApp<br><b>Paid Batches &amp; Content - 9984351085</b></p>')
    story.write(''.join(parts))
    writer=fitz.Document(); page_no=0
    while True:
        page_no += 1
        page=writer.new_page(width=595,height=842)
        more,filled=story.place(fitz.Rect(45,72,550,790))
        story.draw(page)
        logo_path = STATIC_DIR / "branding" / "prana-logo.png"
        if logo_path.exists():
            try: page.insert_image(fitz.Rect(45,18,76,49), filename=str(logo_path), overlay=True)
            except Exception: pass
        page.insert_text((84,38), "PRANA PCS Mains AI", fontsize=13, fontname="hebo", color=(0.12,0.12,0.12), overlay=True)
        page.insert_text((430,38), today, fontsize=8, fontname="hebo", color=(0.35,0.35,0.35), overlay=True)
        page.draw_line((45,800),(550,800),color=(0.65,0.65,0.65),width=0.6,overlay=True)
        page.insert_text((45,818),"Telegram",fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)
        page.insert_text((300,818),"Instagram",fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)
        page.insert_text((45,830),"YouTube",fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)
        page.insert_text((300,830),"WhatsApp",fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)
        page.insert_text((185,841),"Paid Batches & Content - 9984351085",fontsize=7.5,color=(0.35,0.35,0.35),overlay=True)
        if not more: break
    out=io.BytesIO(); writer.save(out,garbage=4,deflate=True); writer.close()
    return out.getvalue()





def daily_pdf_filename(stamp, language):
    lang = "English" if str(language).lower().startswith("en") else "Hindi"
    stamp = str(stamp)
    try:
        stamp = datetime.strptime(stamp[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        pass
    return f"DQ_{stamp}_{lang}.pdf" if lang == "English" else f"Daily_Questions_{stamp}_{lang}.pdf"

@app.get("/api/admin/dq/pdf")
def admin_dq_pdf(request: Request, content_date: str = "", language: str = "Hindi"):
    if not admin_authorized(request): return admin_denied()
    ensure_new_schema(); lang='English' if str(language).lower().startswith('en') else 'Hindi'
    with engine.connect() as conn:
        rows=conn.exec_driver_sql("""SELECT cs.id AS set_id,cs.paper,cs.language,cs.content_date,cs.rubric,ci.question_number,ci.question,ci.model_answer\n            FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id\n            WHERE cs.content_type='daily' AND cs.is_active=TRUE AND cs.language=%s AND (%s='' OR CAST(cs.content_date AS TEXT)=%s)\n            ORDER BY cs.content_date DESC,cs.paper,ci.question_number""",(lang,content_date,content_date)).mappings().all()
    if not rows:return app_error('इस date/language के लिए Daily Questions नहीं मिले।',404)
    out=build_daily_content_pdf([dict(r) for r in rows],lang)
    response=Response(content=out,media_type='application/pdf'); stamp=content_date or str(rows[0].get('content_date') or datetime.now().date()); filename = daily_pdf_filename(stamp, lang); response.headers['Content-Disposition']=f'attachment; filename="{filename}"'; return response

@app.post("/api/admin/dq/send-pdf")
def admin_dq_send_pdf(request: Request, content_date: str = "", language: str = "Hindi"):
    admin=current_admin(request)
    if not admin:return admin_denied()
    if not bot:return app_error('Telegram bot is not configured.',500)
    ensure_new_schema(); lang='English' if str(language).lower().startswith('en') else 'Hindi'
    with engine.connect() as conn:
        rows=conn.exec_driver_sql("""SELECT cs.id AS set_id,cs.paper,cs.language,cs.content_date,cs.rubric,ci.question_number,ci.question,ci.model_answer\n            FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id\n            WHERE cs.content_type='daily' AND cs.is_active=TRUE AND cs.language=%s AND (%s='' OR CAST(cs.content_date AS TEXT)=%s)\n            ORDER BY cs.content_date DESC,cs.paper,ci.question_number""",(lang,content_date,content_date)).mappings().all()
    if not rows:return app_error('इस date/language के लिए Daily Questions नहीं मिले।',404)
    out=build_daily_content_pdf([dict(r) for r in rows],lang); bio=io.BytesIO(out); stamp=content_date or str(rows[0].get('content_date') or datetime.now().date()); bio.name=daily_pdf_filename(stamp, lang); bot.send_document(str(admin['id']),bio,caption=f'📚 DQ {stamp} • {lang}'); return {'ok':True,'message':'DQ PDF Telegram chat में भेज दी गई है।','filename':bio.name}

@app.post("/api/admin/content/send-pdf")
def admin_content_send_pdf(request: Request, language: str = ""):
    admin = current_admin(request)
    if not admin:
        return admin_denied()
    if not bot:
        return app_error("Telegram bot is not configured.", 500)
    ensure_admin_content_table()
    lang = str(language or "").strip()
    with engine.connect() as conn:
        if lang:
            rows = conn.exec_driver_sql(
                "SELECT id,paper,language,question,model_answer,created_at FROM daily_content WHERE is_active=TRUE AND language=%s ORDER BY id ASC",
                (lang,)
            ).mappings().all()
        else:
            rows = conn.exec_driver_sql(
                "SELECT id,paper,language,question,model_answer,created_at FROM daily_content WHERE is_active=TRUE ORDER BY id ASC"
            ).mappings().all()
    if not rows:
        return app_error("कोई Daily Question उपलब्ध नहीं है।", 404)
    try:
        out = build_daily_content_pdf(rows, lang or "Hindi/English")
        bio = io.BytesIO(out)
        bio.name = "PRANA_PCS_Daily_Questions_Model_Answers.pdf"
        bot.send_document(str(admin["id"]), bio, caption="📚 <b>PRANA PCS Mains AI</b>\nDaily Questions + Model Answers")
        return {"ok": True, "message": "Branded PDF आपके Telegram chat में भेज दी गई है।"}
    except Exception as e:
        print("ADMIN CONTENT PDF SEND ERROR:", e)
        return app_error("PDF send failed: " + str(e), 500)


@app.post("/api/app/daily/send-pdf")
def app_send_daily_pdf(request: Request, language: str = "Hindi", content_date: str = "", paper: str = ""):
    uid=require_app_user(request)
    if not uid:return app_error("Unauthorized",401)
    if not bot:return app_error("Telegram bot is not configured.",500)
    ensure_new_schema(); lang="English" if str(language).lower().startswith("en") else "Hindi"
    with engine.connect() as conn:
        rows=conn.exec_driver_sql("""SELECT cs.id AS set_id,cs.paper,cs.language,cs.content_date,cs.rubric,ci.question_number,ci.question,ci.model_answer
            FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id
            WHERE cs.content_type='daily' AND cs.is_active=TRUE AND cs.language=%s AND (%s='' OR CAST(cs.content_date AS TEXT)=%s) AND (%s='' OR cs.paper=%s)
            ORDER BY cs.content_date DESC,cs.paper,ci.question_number""",(lang,content_date,content_date,paper.upper(),paper.upper())).mappings().all()
    if not rows:return app_error("No Daily Questions available.",404)
    try:
        out=build_daily_content_pdf([dict(r) for r in rows],lang); stamp=content_date or str(rows[0].get('content_date') or datetime.now().date()); bio=io.BytesIO(out); bio.name=daily_pdf_filename(stamp, lang)
        bot.send_document(str(uid),bio,caption=f"📚 <b>DQ {stamp} • {lang}</b>\nPRANA PCS Mains AI")
        return {"ok":True,"message":"Daily Questions PDF sent to Telegram chat.","filename":bio.name}
    except Exception as e:
        print("SEND DAILY PDF ERROR:",e); return app_error("Daily Questions PDF भेजने में समस्या हुई।",500)


@app.get("/api/admin/content")
def admin_content(request: Request):
    if not admin_authorized(request):return admin_denied()
    ensure_admin_content_table()
    with engine.connect() as conn: rows=conn.exec_driver_sql("SELECT id,paper,language,question,model_answer,created_at FROM daily_content ORDER BY id DESC").mappings().all()
    return {"ok":True,"items":[dict(r) for r in rows]}

@app.post("/api/admin/content")
async def admin_content_create(request: Request):
    if not admin_authorized(request):return admin_denied()
    ensure_admin_content_table(); body=await request.json(); paper=str(body.get("paper","GS1")).upper(); language=str(body.get("language","Hindi")); question=str(body.get("question","")).strip(); answer=str(body.get("model_answer","")).strip(); now=_utcnow()
    if paper not in {"GS1","GS2","GS3","GS4","GS5","GS6"} or not question:return {"ok":False,"error":"Paper and question are required"}
    with engine.begin() as conn: row=conn.exec_driver_sql("INSERT INTO daily_content(paper,language,question,model_answer,is_active,created_at,updated_at) VALUES (%s,%s,%s,%s,TRUE,%s,%s) RETURNING id",(paper,language,question,answer,now,now)).scalar()
    return {"ok":True,"id":row}


@app.get("/api/admin/users/{user_id}/performance")
def admin_user_performance(user_id: str, request: Request):
    if not admin_authorized(request): return admin_denied()
    s=SessionLocal()
    try:
        u=s.get(DBUser,user_id)
        if not u:return {"ok":False,"error":"User not found"}
        rows=s.query(DBSubmission).filter(DBSubmission.telegram_user_id==user_id).order_by(desc(DBSubmission.created_at)).all()
        ob=sum(float(x.total_obtained_marks or 0) for x in rows); mx=sum(float(x.total_max_marks or 0) for x in rows)
        paper_stats={}
        for x in rows:
            b=paper_stats.setdefault(x.paper,{"submissions":0,"obtained":0.0,"max":0.0})
            b["submissions"]+=1; b["obtained"]+=float(x.total_obtained_marks or 0); b["max"]+=float(x.total_max_marks or 0)
        for b in paper_stats.values():
            b["average_percentage"]=round(b["obtained"]/b["max"]*100,1) if b["max"] else 0
            b["obtained"]=round(b["obtained"],1); b["max"]=round(b["max"],1)
        return {"ok":True,"user":{"id":u.telegram_user_id,"name":" ".join(x for x in [u.first_name,u.last_name] if x),"username":u.username,"allowed":u.is_allowed,"blocked":u.is_blocked,"submissions":len(rows),"obtained":round(ob,1),"max":round(mx,1),"average_percentage":round(ob/mx*100,1) if mx else 0,"last_seen":u.last_seen_at.isoformat() if u.last_seen_at else None},"paper_stats":paper_stats,"recent":[{"id":x.id,"paper":x.paper,"obtained":x.total_obtained_marks,"max":x.total_max_marks,"language":x.copy_language,"filename":x.evaluated_filename,"created_at":x.created_at.isoformat() if x.created_at else None} for x in rows[:25]]}
    finally:s.close()

@app.delete("/api/admin/content/{content_id}")
def admin_content_delete(content_id: int, request: Request):
    if not admin_authorized(request): return admin_denied()
    ensure_admin_content_table()
    with engine.begin() as conn:
        result=conn.exec_driver_sql("DELETE FROM daily_content WHERE id=%s",(int(content_id),))
        if result.rowcount==0:return {"ok":False,"error":"Content not found"}
    return {"ok":True}

try:
    if DB_ENABLED: ensure_admin_content_table()
except Exception as e: print("ADMIN INIT WARNING:",e)



# ============================================================
# PRANA PCS STUDENT MINI APP
# ============================================================

from fastapi.staticfiles import StaticFiles
from urllib.parse import parse_qs
from types import SimpleNamespace

APP_SESSION_COOKIE = "prana_student_session"
APP_SESSION_MAX_AGE = 60 * 60 * 6
APP_AUTH_SECRET = hashlib.sha256((BOT_TOKEN + "|PRANA_PCS_MINI_APP").encode()).digest()
APP_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

try:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
except Exception as e:
    print("STATIC MOUNT WARNING:", e)


def make_app_session(uid):
    payload = {
        "uid": str(uid),
        "exp": int(time.time()) + APP_SESSION_MAX_AGE,
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(APP_AUTH_SECRET, raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def current_app_uid(request: Request):
    token = request.cookies.get(APP_SESSION_COOKIE, "")
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(APP_AUTH_SECRET, raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(
                raw + "=" * (-len(raw) % 4)
            ).decode()
        )
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return str(payload.get("uid", "")) or None
    except Exception:
        return None


# Telegram's production public key for the Ed25519 initData signature scheme.
_TELEGRAM_WEBAPP_PUBLIC_KEY = bytes.fromhex(
    "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
)


def _tg_check_string(values, exclude_signature=False):
    return "\n".join(
        f"{k}={values[k][0]}"
        for k in sorted(values)
        if k != "hash" and not (exclude_signature and k == "signature")
    )


def telegram_webapp_validate(init_data: str):
    """Validate Telegram Mini App initData.

    Accepts the standard HMAC-SHA256 scheme and falls back to Telegram's newer
    Ed25519 signature scheme, so launches from different contexts (inline button,
    menu button, direct link) all validate. Returns the user dict or None.
    """
    if not BOT_TOKEN or not init_data:
        return None
    try:
        values = parse_qs(str(init_data), keep_blank_values=True)
        received_hash = values.get("hash", [""])[0]
        received_sig = values.get("signature", [""])[0]
        auth_date = int(values.get("auth_date", ["0"])[0])
        user_raw = values.get("user", [""])[0]
        if not auth_date or not user_raw:
            return None
        age = int(time.time()) - auth_date
        if age < -300 or age > 86400:
            return None

        # Primary: HMAC-SHA256 with the bot token.
        check = _tg_check_string(values)
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        valid = bool(received_hash) and hmac.compare_digest(calc, received_hash)

        # Fallback: Ed25519 signature verification.
        if not valid and received_sig:
            try:
                bot_id = BOT_TOKEN.split(":", 1)[0]
                sig_check = f"{bot_id}:WebAppData\n" + _tg_check_string(values, True)
                sig = base64.urlsafe_b64decode(received_sig + "=" * (-len(received_sig) % 4))
                Ed25519PublicKey.from_public_bytes(_TELEGRAM_WEBAPP_PUBLIC_KEY).verify(
                    sig, sig_check.encode()
                )
                valid = True
            except Exception:
                pass

        if not valid:
            return None
        user = json.loads(user_raw)
        uid = str(user.get("id", ""))
        if not uid:
            return None
        return {
            "id": uid,
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "language_code": user.get("language_code"),
        }
    except Exception as e:
        print("MINI APP AUTH ERROR:", e)
        return None


def ensure_app_user(user):
    if not DB_ENABLED or SessionLocal is None:
        return
    session = SessionLocal()
    try:
        now = _utcnow()
        uid = str(user["id"])
        row = session.get(DBUser, uid)
        if row is None:
            # New Mini App users start pending. Admin/group access grants entry.
            row = DBUser(
                telegram_user_id=uid,
                username=user.get("username"),
                first_name=user.get("first_name"),
                last_name=user.get("last_name"),
                is_allowed=False,
                is_blocked=False,
                created_at=now,
                last_seen_at=now,
            )
            session.add(row)
        else:
            row.username = user.get("username") or row.username
            row.first_name = user.get("first_name") or row.first_name
            row.last_name = user.get("last_name") or row.last_name
            row.last_seen_at = now
        session.commit()
    except Exception as e:
        session.rollback()
        print("MINI APP USER SAVE ERROR:", e)
    finally:
        session.close()


def app_user_allowed(uid: str):
    """Return (allowed, source). Direct user access wins; otherwise check allowed groups."""
    if not DB_ENABLED or SessionLocal is None:
        return False, "database_unavailable"
    session = SessionLocal()
    try:
        user = session.get(DBUser, str(uid))
        if user and user.is_blocked:
            return False, "blocked"
        if user and user.is_allowed:
            return True, "user"
        groups = session.query(DBGroup).filter(
            DBGroup.is_allowed == True,
            DBGroup.is_blocked == False
        ).all()
    except Exception as e:
        print("MINI APP ACCESS DB ERROR:", e)
        return False, "database_error"
    finally:
        session.close()

    if not bot:
        return False, "no_bot"

    for group in groups:
        try:
            member = bot.get_chat_member(
                int(group.telegram_group_id),
                int(uid)
            )
            status = str(getattr(member, "status", ""))
            if status in ("creator", "administrator", "member"):
                return True, "group"
        except Exception as e:
            # Bot may not be able to inspect a group; simply try the next one.
            print(
                f"MINI APP GROUP CHECK FAILED {group.telegram_group_id}: {str(e)[:120]}"
            )
            continue
    return False, "not_authorized"


def require_app_user(request: Request):
    # Prefer the signed session cookie. Telegram Mini Apps/WebViews can be
    # conservative about cookies, so also accept Telegram's signed initData
    # header as a stateless fallback. This is especially useful on Cloud Run.
    uid = current_app_uid(request)
    if uid:
        return uid
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = telegram_webapp_validate(init_data)
    if user:
        try:
            ensure_app_user(user)
        except Exception as exc:
            print("MINI APP HEADER AUTH USER SAVE ERROR:", repr(exc))
        return str(user["id"])
    return None


def mini_app_db_ready():
    """Return a precise DB readiness result for Mini App diagnostics."""
    if not DB_ENABLED or engine is None or SessionLocal is None:
        return False, "DATABASE_URL is not configured on the Cloud Run service."
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True, "ok"
    except Exception as exc:
        print("MINI APP DB HEALTH ERROR:", repr(exc))
        return False, f"Database connection failed: {str(exc)[:180]}"


def app_error(message, status=403):
    return Response(
        content=json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        status_code=status,
        media_type="application/json"
    )


def upsert_app_user_record(user):
    ensure_app_user(user)
    uid = str(user["id"])
    session = SessionLocal()
    try:
        row = session.get(DBUser, uid)
        if not row:
            return None
        return row
    finally:
        session.close()


def app_user_payload(uid):
    if not DB_ENABLED or SessionLocal is None:
        return {
            "id": str(uid), "name": "Student", "username": None,
            "submissions": 0, "obtained": 0, "max": 0, "average_percentage": 0,
        }
    session = SessionLocal()
    try:
        u = session.get(DBUser, str(uid))
        if not u:
            return None
        rows = session.query(DBSubmission).filter(
            DBSubmission.telegram_user_id == str(uid),
            DBSubmission.status == "completed"
        ).order_by(desc(DBSubmission.created_at)).all()
        obtained = sum(float(x.total_obtained_marks or 0) for x in rows)
        maximum = sum(float(x.total_max_marks or 0) for x in rows)
        return {
            "id": u.telegram_user_id,
            "name": " ".join(x for x in [u.first_name, u.last_name] if x) or "Student",
            "username": u.username,
            "submissions": len(rows),
            "obtained": round(obtained, 1),
            "max": round(maximum, 1),
            "average_percentage": round(obtained / maximum * 100, 1) if maximum else 0,
        }
    except Exception as exc:
        print("MINI APP USER PAYLOAD ERROR:", repr(exc))
        return {
            "id": str(uid), "name": "Student", "username": None,
            "submissions": 0, "obtained": 0, "max": 0, "average_percentage": 0,
        }
    finally:
        session.close()


@app.get("/app", response_class=HTMLResponse)
@app.get("/miniapp", response_class=HTMLResponse)
def student_mini_app():
    """
    Serve the Telegram Mini App without depending on one fragile filesystem path.

    Render normally runs the repository from /opt/render/project/src, but this
    route also checks the current working directory and a few common static
    locations.  This prevents a valid GitHub file from producing the generic
    "Mini App build missing" 500 merely because the deployment path differs.
    """
    base_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()

    candidates = [
        base_dir / "static" / "branding" / "mini_app.html",
        cwd / "static" / "branding" / "mini_app.html",
        base_dir / "static" / "mini_app.html",
        cwd / "static" / "mini_app.html",
        base_dir / "branding" / "mini_app.html",
        cwd / "branding" / "mini_app.html",
        base_dir / "mini_app.html",
        cwd / "mini_app.html",
    ]

    html_path = next((p for p in candidates if p.is_file()), None)

    # Last-resort lookup for deployment files whose parent directory casing
    # or layout differs from the expected path.
    if html_path is None:
        for root in (base_dir, cwd):
            try:
                matches = list(root.rglob("mini_app.html"))
            except Exception:
                matches = []
            if matches:
                html_path = next((p for p in matches if p.is_file()), None)
                if html_path is not None:
                    break

    if html_path is None:
        # Keep the failure informative instead of hiding the real deployment
        # problem behind a generic 500 response.
        return HTMLResponse(
            "<h2>Mini App unavailable</h2>"
            "<p>The Mini App file could not be found in this deployment.</p>"
            "<p>Expected: static/branding/mini_app.html</p>",
            status_code=404,
        )

    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as exc:
        print("MINI APP READ ERROR:", repr(exc))
        return HTMLResponse(
            "<h2>Mini App unavailable</h2>"
            "<p>The Mini App file was found but could not be read.</p>",
            status_code=500,
        )

    return HTMLResponse(content=html, media_type="text/html")


@app.post("/api/app/start-trial")
def app_start_trial(request: Request):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    if not DB_ENABLED or SessionLocal is None:
        return app_error("Database unavailable.", 503)
    s=SessionLocal()
    try:
        u=s.get(DBUser,str(uid))
        if not u:
            return app_error("User not found.",404)
        if u.is_blocked:
            return app_error("This account is blocked.",403)
        if SUPER_ADMIN_TELEGRAM_ID and str(uid)==str(SUPER_ADMIN_TELEGRAM_ID):
            u.is_allowed=True; u.access_type="full"
        else:
            admin=s.execute(__import__("sqlalchemy").text(
                "SELECT role,is_active FROM admin_users WHERE telegram_user_id=:uid LIMIT 1"
            ),{"uid":str(uid)}).mappings().first()
            if admin and admin["is_active"]:
                u.is_allowed=True; u.access_type="full"
            elif u.access_type in ("none",""):
                u.access_type="trial"; u.is_allowed=False
                u.trial_copies_limit=int(u.trial_copies_limit or 3)
                u.trial_questions_limit=int(u.trial_questions_limit or 10)
                u.trial_copies_used=int(u.trial_copies_used or 0)
                u.trial_questions_used=int(u.trial_questions_used or 0)
        s.commit()
        return {"ok":True,"access_type":u.access_type,"trial_copies_used":int(u.trial_copies_used or 0),"trial_copies_limit":int(u.trial_copies_limit or 3),"trial_questions_used":int(u.trial_questions_used or 0),"trial_questions_limit":int(u.trial_questions_limit or 10)}
    finally:
        s.close()

@app.post("/api/app/auth")
async def app_auth(request: Request):
    # Never expose a Python traceback as HTTP 500 to Telegram. Return a precise
    # service/dependency status instead, and log the real exception in Cloud Run.
    try:
        body = await request.json()
    except Exception:
        return app_error("Invalid authentication request.", 400)

    init_data = str(body.get("initData", ""))
    user = telegram_webapp_validate(init_data)
    if not user:
        return app_error("Telegram authentication invalid or expired. Please reopen the Mini App from Telegram.", 401)

    db_ok, db_message = mini_app_db_ready()
    if not db_ok:
        print("MINI APP AUTH DB NOT READY:", db_message)
        return app_error("Mini App database is unavailable. Cloud Run में DATABASE_URL check करें.", 503)

    try:
        ensure_app_user(user)
        allowed, source = app_user_allowed(user["id"])
        access_ok, access_source, urow = evaluation_access(user["id"])
        if allowed:
            access_ok = True
            access_source = source

        payload = {
            "ok": True,
            "user": app_user_payload(user["id"]),
            "access_source": source,
            "evaluation_access": access_ok,
            "evaluation_access_source": access_source,
            "trial_copies_used": int(getattr(urow, "trial_copies_used", 0) or 0) if urow else 0,
            "trial_copies_limit": int(getattr(urow, "trial_copies_limit", 3) or 3) if urow else 3,
            "trial_questions_used": int(getattr(urow, "trial_questions_used", 0) or 0) if urow else 0,
            "trial_questions_limit": int(getattr(urow, "trial_questions_limit", 10) or 10) if urow else 10,
        }
        response = Response(
            content=json.dumps(payload, ensure_ascii=False),
            media_type="application/json"
        )
        response.set_cookie(
            APP_SESSION_COOKIE,
            make_app_session(user["id"]),
            max_age=APP_SESSION_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/"
        )
        return response
    except Exception as exc:
        print("MINI APP AUTH ERROR:", repr(exc))
        return app_error("Mini App authentication service error. Check Cloud Run logs and DATABASE_URL/schema.", 503)


@app.post("/api/app/admin-auth")
async def app_admin_auth(request: Request):
    body = await request.json()
    init_data = str(body.get("initData", ""))
    user = telegram_webapp_validate(init_data)
    if not user:
        return app_error("Telegram authentication invalid or expired.", 401)

    uid = str(user["id"])
    role = resolve_admin_role(uid)
    if not role:
        return app_error("यह Telegram account authorized admin नहीं है।", 403)

    response = Response(
        content=json.dumps({"ok": True, "role": role, "telegram_user_id": uid}),
        media_type="application/json"
    )
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        make_admin_session(uid, role),
        max_age=ADMIN_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/"
    )
    return response


@app.post("/api/app/send-to-telegram")
async def app_send_to_telegram(
    request: Request,
    initData: str = Form(""),
    files: list[UploadFile] = File(...)
):
    user = telegram_webapp_validate(initData)
    if not user:
        return app_error("Telegram authentication invalid or expired.", 401)
    if not bot:
        return app_error("Telegram bot is not configured.", 500)
    if not files:
        return app_error("कोई file upload नहीं हुई।", 400)

    try:
        pdf_files = []
        image_files = []
        for upload in files:
            filename = Path(upload.filename or "submission").name
            data = await upload.read()
            if not data:
                continue
            if len(data) > 20 * 1024 * 1024:
                return app_error("हर file 20 MB से छोटी होनी चाहिए।", 400)
            ext = Path(filename).suffix.lower()
            ctype = (upload.content_type or "").lower()
            if ext == ".pdf" or ctype == "application/pdf":
                pdf_files.append((filename, data))
            elif ext in (".jpg",".jpeg",".png",".webp",".bmp") or ctype.startswith("image/"):
                image_files.append((filename, data))
            else:
                return app_error(f"Unsupported file type: {filename}", 400)

        if pdf_files and image_files:
            return app_error("एक बार में PDF या images में से एक प्रकार भेजें।", 400)
        if len(pdf_files) > 1:
            return app_error("एक बार में केवल एक PDF भेजें।", 400)

        if pdf_files:
            output_name, pdf_bytes = pdf_files[0]
            if not output_name.lower().endswith(".pdf"):
                output_name = Path(output_name).stem + ".pdf"
        elif image_files:
            doc = fitz.open()
            try:
                for filename, data in image_files:
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                    tmp = io.BytesIO()
                    img.save(tmp, format="JPEG", quality=94)
                    page = doc.new_page(width=img.width, height=img.height)
                    page.insert_image(page.rect, stream=tmp.getvalue())
                out = io.BytesIO()
                doc.save(out, garbage=4, deflate=True)
                pdf_bytes = out.getvalue()
            finally:
                doc.close()
            output_name = Path(image_files[0][0]).stem + ".pdf"
        else:
            return app_error("Valid PDF या image file नहीं मिली।", 400)

        bio = io.BytesIO(pdf_bytes)
        bio.name = output_name
        bot.send_document(
            str(user["id"]),
            bio,
            caption=f"📄 {output_name}\nPRANA PCS AI Evaluator"
        )
        return {"ok": True, "filename": output_name, "message": "PDF Telegram chat में भेज दी गई है।"}
    except Exception as e:
        print("SEND PDF TELEGRAM ERROR:", e)
        return app_error("Telegram पर PDF भेजने में समस्या हुई।", 500)


@app.get("/api/app/me")
def app_me(request: Request):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    return {"ok": True, "user": app_user_payload(uid)}


@app.get("/api/app/dashboard")
def app_dashboard(request: Request):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    session = SessionLocal()
    try:
        rows = session.query(DBSubmission).filter(
            DBSubmission.telegram_user_id == uid,
            DBSubmission.status == "completed"
        ).order_by(desc(DBSubmission.created_at)).all()
        by_paper = {}
        for x in rows:
            b = by_paper.setdefault(x.paper, {"submissions": 0, "obtained": 0.0, "max": 0.0})
            b["submissions"] += 1
            b["obtained"] += float(x.total_obtained_marks or 0)
            b["max"] += float(x.total_max_marks or 0)
        for b in by_paper.values():
            b["percentage"] = round(b["obtained"] / b["max"] * 100, 1) if b["max"] else 0
            b["obtained"] = round(b["obtained"], 1)
            b["max"] = round(b["max"], 1)
        recent = [{
            "id": x.id,
            "paper": x.paper,
            "exam": x.exam,
            "evaluation_type": x.evaluation_type,
            "source_id": x.source_id,
            "obtained": x.total_obtained_marks,
            "max": x.total_max_marks,
            "language": x.copy_language,
            "created_at": x.created_at.isoformat() if x.created_at else None,
            "status": x.status,
        } for x in rows[:8]]
        return {
            "ok": True,
            "user": app_user_payload(uid),
            "paper_stats": by_paper,
            "recent": recent,
        }
    finally:
        session.close()


@app.get("/api/app/daily")
def app_daily(request: Request, language: str = "Hindi", content_date: str = "", paper: str = ""):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    ensure_new_schema()
    lang = "English" if str(language).lower().startswith("en") else "Hindi"
    params=[lang, str(content_date or ""), str(content_date or ""), str(paper or "").upper()]
    with engine.connect() as conn:
        rows=conn.exec_driver_sql("""SELECT cs.id,cs.id AS set_id,cs.paper,cs.language,cs.content_date,ci.question_number,ci.question,ci.model_answer
            FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id
            WHERE cs.content_type='daily' AND cs.is_active=TRUE AND cs.language=%s
              AND (%s='' OR CAST(cs.content_date AS TEXT)=%s)
              AND (%s='' OR cs.paper=%s)
            ORDER BY cs.content_date DESC NULLS LAST,cs.paper,ci.question_number""",(lang,str(content_date or ""),str(content_date or ""),str(paper or "").upper(),str(paper or "").upper())).mappings().all()
    return {"ok":True,"items":[dict(r) for r in rows]}


@app.get("/api/app/performance")
def app_performance(request: Request, paper: str = ""):
    uid=require_app_user(request)
    if not uid: return app_error("Unauthorized",401)
    paper=str(paper or "").upper().strip()
    s=SessionLocal()
    try:
        q=s.query(DBSubmission).filter(DBSubmission.telegram_user_id==uid,DBSubmission.status=="completed")
        if paper:q=q.filter(DBSubmission.paper==paper)
        rows=q.order_by(desc(DBSubmission.created_at)).all()
        total_ob=sum(float(x.total_obtained_marks or 0) for x in rows); total_max=sum(float(x.total_max_marks or 0) for x in rows)
        mistakes=[]; strengths=[]
        for sub in rows[:20]:
            qs=s.query(DBQuestion).filter(DBQuestion.submission_id==sub.id).all()
            for qrow in qs:
                if qrow.skipped_parts: mistakes.append(f"{sub.paper} Q{qrow.question_number}: " + ", ".join(map(str,qrow.skipped_parts)))
                if float(qrow.max_marks or 0) and float(qrow.obtained_marks or 0) >= float(qrow.max_marks or 0)*.75:
                    strengths.append(f"{sub.paper} Q{qrow.question_number}: strong coverage")
        return {"ok":True,"paper":paper,"percentage":round(total_ob/total_max*100,1) if total_max else 0,"submissions":len(rows),
                "recent":[{"id":x.id,"paper":x.paper,"exam":x.exam,"evaluation_type":x.evaluation_type,"obtained":x.total_obtained_marks,"max":x.total_max_marks,"language":x.copy_language} for x in rows[:20]],
                "mistakes":mistakes[:20],"strengths":strengths[:20]}
    finally:s.close()

@app.get("/api/app/evaluations")
def app_evaluations(request: Request, paper: str = "", exam: str = "", evaluation_type: str = "", limit: int = 50):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    session = SessionLocal()
    try:
        q = session.query(DBSubmission).filter(
            DBSubmission.telegram_user_id == uid,
            DBSubmission.status == "completed"
        ).order_by(desc(DBSubmission.created_at))
        if paper: q = q.filter(DBSubmission.paper == paper.upper())
        if exam: q = q.filter(DBSubmission.exam == exam.upper())
        if evaluation_type: q = q.filter(DBSubmission.evaluation_type == evaluation_type.upper())
        rows = q.limit(min(max(int(limit), 1), 200)).all()
        return {"ok": True, "items": [{
            "id": x.id,
            "paper": x.paper,
            "obtained": x.total_obtained_marks,
            "max": x.total_max_marks,
            "percentage": round(float(x.total_obtained_marks or 0) / float(x.total_max_marks or 1) * 100, 1),
            "language": x.copy_language,
            "filename": x.original_filename,
            "evaluated_filename": x.evaluated_filename,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        } for x in rows]}
    finally:
        session.close()


@app.get("/api/app/evaluations/{submission_id}")
def app_evaluation_detail(submission_id: str, request: Request):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    session = SessionLocal()
    try:
        x = session.get(DBSubmission, submission_id)
        if not x or x.telegram_user_id != uid:
            return app_error("Evaluation not found", 404)
        qs = session.query(DBQuestion).filter(DBQuestion.submission_id == submission_id).order_by(DBQuestion.question_number).all()
        comments = session.query(DBPageComment).filter(DBPageComment.submission_id == submission_id).order_by(DBPageComment.page, DBPageComment.id).all()
        annotations = session.query(DBAnnotation).filter(DBAnnotation.submission_id == submission_id).order_by(DBAnnotation.page, DBAnnotation.id).all()
        return {
            "ok": True,
            "submission": {
                "id": x.id,
                "paper": x.paper,
                "language": x.copy_language,
                "obtained": x.total_obtained_marks,
                "max": x.total_max_marks,
                "percentage": round(float(x.total_obtained_marks or 0) / float(x.total_max_marks or 1) * 100, 1),
                "feedback": x.overall_feedback,
                "filename": x.evaluated_filename,
                "created_at": x.created_at.isoformat() if x.created_at else None,
            },
            "questions": [{
                "number": q.question_number,
                "start_page": q.start_page,
                "end_page": q.end_page,
                "obtained": q.obtained_marks,
                "max": q.max_marks,
                "demand": q.demand_parts or [],
                "fulfilled": q.fulfilled_parts or [],
                "skipped": q.skipped_parts or [],
                "intro_comment": q.intro_comment or "",
                "body_comment": q.body_comment or "",
                "conclusion_comment": q.conclusion_comment or "",
                "comment": q.end_page_comment or "",
            } for q in qs],
            "comments": [{
                "page": c.page,
                "color": c.color,
                "comment": c.comment,
            } for c in comments],
            "annotations": [{
                "page": a.page,
                "type": a.annotation_type,
                "color": a.color,
                "text": a.exact_text,
                "reason": a.reason,
            } for a in annotations],
        }
    finally:
        session.close()


@app.get("/api/app/evaluations/{submission_id}/pdf")
def app_evaluation_pdf(submission_id: str, request: Request):
    uid = require_app_user(request)
    if not uid:
        return Response(content=b"Unauthorized", status_code=401)
    session = SessionLocal()
    try:
        sub = session.get(DBSubmission, submission_id)
        f = session.get(DBSubmissionPDF, submission_id)
        if not sub or sub.telegram_user_id != uid or not f:
            return Response(content=b"Not found", status_code=404)
        return Response(
            content=bytes(f.pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{Path(sub.evaluated_filename).name}"'}
        )
    finally:
        session.close()


@app.post("/api/app/evaluations/{submission_id}/send-to-telegram")
def app_send_evaluated_to_telegram(submission_id: str, request: Request):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    if not bot:
        return app_error("Telegram bot is not configured.", 500)
    session = SessionLocal()
    try:
        sub = session.get(DBSubmission, submission_id)
        f = session.get(DBSubmissionPDF, submission_id)
        if not sub or sub.telegram_user_id != uid or not f:
            return app_error("Evaluation not found", 404)
        bio = io.BytesIO(bytes(f.pdf_bytes))
        bio.name = Path(sub.evaluated_filename).name
        feedback = str(sub.overall_feedback or "").strip()
        caption = (
            f"📄 <b>PRANA PCS AI Evaluated Copy</b>\n"
            f"<b>Obtained Marks:</b> {float(sub.total_obtained_marks or 0):g} / {float(sub.total_max_marks or 0):g}\n"
            f"<b>Language • Style • Presentation:</b> {feedback or 'Assessed as part of the evaluation.'}"
        )[:900]
        bot.send_document(str(uid), bio, caption=caption)
        return {"ok": True, "message": "Evaluated copy Telegram chat में भेज दी गई है।"}
    except Exception as e:
        print("SEND EVALUATED PDF ERROR:", e)
        return app_error("Evaluated copy भेजने में समस्या हुई।", 500)
    finally:
        session.close()


def create_webapp_submission(uid, paper, filename, exam="UPPCS", evaluation_type="GENERAL", source_id=None, medium=None):
    submission_id = str(uuid.uuid4())
    now = _utcnow()
    session = SessionLocal()
    try:
        row = DBSubmission(
            id=submission_id,
            telegram_user_id=str(uid),
            telegram_chat_id=None,
            chat_type="web_app",
            group_id=None,
            paper=paper,
            exam=exam,
            evaluation_type=evaluation_type,
            source_id=source_id,
            medium=medium,
            original_filename=filename,
            evaluated_filename=f"{Path(filename).stem}_Evaluated.pdf",
            copy_language=None,
            total_obtained_marks=None,
            total_max_marks=None,
            overall_feedback=None,
            status="processing",
            created_at=now,
            completed_at=None,
        )
        session.add(row)
        session.commit()
        return submission_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_webapp_evaluation(submission_id, uid, path, paper, original_filename, exam="UPPCS", evaluation_type="GENERAL", source_id=None, medium=None):
    try:
        final_pdf, result = process_submission(path, paper, evaluation_type=evaluation_type, source_id=source_id, exam=exam, medium=medium)
        evaluated_filename = f"{Path(original_filename).stem or 'submission'}_Evaluated.pdf"
        session = SessionLocal()
        try:
            row = session.get(DBSubmission, submission_id)
            if not row:
                raise Exception("Submission record not found")
            trial_guard=SessionLocal()
            try:
                tu=trial_guard.get(DBUser,str(uid))
                if tu and tu.access_type=="trial":
                    qcount=len(result.get("questions",[]))
                    if int(tu.trial_copies_used or 0) >= int(tu.trial_copies_limit or 3) or int(tu.trial_questions_used or 0)+qcount > int(tu.trial_questions_limit or 10):
                        row.status="failed"; row.overall_feedback="Trial limit reached: 3 copies or 10 questions."; row.completed_at=_utcnow(); session.commit(); return
            finally:
                trial_guard.close()
            row.evaluated_filename = evaluated_filename
            row.copy_language = result.get("copy_language") or None
            row.total_obtained_marks = float(result.get("total_obtained_marks", 0) or 0)
            row.total_max_marks = float(result.get("total_max_marks", 0) or 0)
            row.overall_feedback = str(result.get("overall_feedback", ""))
            row.status = "completed"
            row.completed_at = _utcnow()
            session.add(DBSubmissionPDF(
                submission_id=submission_id,
                pdf_bytes=final_pdf.getvalue(),
                created_at=_utcnow()
            ))
            for q in result.get("questions", []):
                session.add(DBQuestion(
                    submission_id=submission_id,
                    question_number=int(q.get("question_number", 0)),
                    start_page=int(q.get("start_page", 1)),
                    end_page=int(q.get("end_page", 1)),
                    pages_used=int(q.get("pages_used", 1)),
                    max_marks=float(q.get("max_marks", 0) or 0),
                    obtained_marks=float(q.get("obtained_marks", 0) or 0),
                    intro_comment=str(q.get("intro_comment", "")),
                    body_comment=str(q.get("body_comment", "")),
                    conclusion_comment=str(q.get("conclusion_comment", "")),
                    demand_parts=q.get("demand_parts", []),
                    fulfilled_parts=q.get("fulfilled_parts", []),
                    skipped_parts=q.get("skipped_parts", []),
                    end_page_comment=str(q.get("end_page_comment", "")),
                ))
            for c in result.get("page_comments", []):
                session.add(DBPageComment(
                    submission_id=submission_id,
                    page=int(c.get("page", 1) or 1),
                    color=str(c.get("color", "red")),
                    comment=str(c.get("comment", "")),
                    placement_box=c.get("placement_box"),
                    anchor=c.get("anchor"),
                ))
            for a in result.get("annotations", []):
                session.add(DBAnnotation(
                    submission_id=submission_id,
                    page=int(a.get("page", 1) or 1),
                    annotation_type=str(a.get("type", "good")),
                    color=str(a.get("color", "green")),
                    exact_text=str(a.get("exact_text", "")),
                    reason=str(a.get("reason", "")),
                    box_2d=a.get("box_2d"),
                ))
            session.commit()
            # Trial usage is consumed only after a successful evaluation.
            s3=SessionLocal()
            try:
                access_user_row=s3.get(DBUser,str(uid))
            finally:
                s3.close()
            if access_user_row is not None and getattr(access_user_row, "access_type", "") == "trial":
                qs_count=len(result.get("questions", []))
                s2=SessionLocal()
                try:
                    u2=s2.get(DBUser,str(uid))
                    if u2 and u2.access_type=="trial":
                        u2.trial_copies_used=int(u2.trial_copies_used or 0)+1
                        u2.trial_questions_used=int(u2.trial_questions_used or 0)+qs_count
                        s2.commit()
                finally:
                    s2.close()
            print(f"MINI APP EVALUATION COMPLETE: {submission_id}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        print("MINI APP EVALUATION ERROR:", e)
        session = SessionLocal()
        try:
            row = session.get(DBSubmission, submission_id)
            if row:
                row.status = "failed"
                row.overall_feedback = str(e)[:500]
                row.completed_at = _utcnow()
                session.commit()
        except Exception as db_error:
            session.rollback()
            print("MINI APP FAILURE STATUS ERROR:", db_error)
        finally:
            session.close()
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@app.post("/api/app/evaluate")
async def app_evaluate(
    request: Request,
    background_tasks: BackgroundTasks,
    paper: str = "",
    files: list[UploadFile] = File(...),
):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    # Evaluation metadata is accepted as query parameters for compatibility with the existing Mini App.
    paper = str(paper).upper().strip()
    exam = str(getattr(request.state, "exam", "UPPCS") or "UPPCS")
    source_id = None
    medium = None
    # The Mini App may send these as query parameters while legacy clients send only paper.
    qp=request.query_params
    exam=str(qp.get("exam", "UPPCS")).upper().strip() or "UPPCS"
    evaluation_type=str(qp.get("evaluation_type", "GENERAL")).upper().strip() or "GENERAL"
    source_id=qp.get("source_id")
    medium=qp.get("medium")
    if exam not in EVALUATION_EXAMS: return app_error("Invalid exam selected.",400)
    if evaluation_type not in EVALUATION_TYPES: return app_error("Invalid evaluation type.",400)
    if paper not in PAPER_OPTIONS:
        return app_error("Valid paper select करें।", 400)
    access_ok, access_source, access_user = evaluation_access(uid)
    if not access_ok:
        # A group grant is a full evaluation grant after live Telegram membership verification.
        grp_ok, grp_source = app_user_allowed(uid)
        if grp_ok:
            access_ok=True; access_source=grp_source
        else:
            return app_error("Access Denied — evaluation access नहीं है।", 403)
    trial_question_estimate = 1
    if evaluation_type in ("DAILY","PYQ","GROUP"):
        if not source_id:
            return app_error("इस evaluation type के लिए uploaded content select करना जरूरी है।",400)
        if not get_content_reference(evaluation_type, source_id=source_id, paper=paper, exam=exam):
            return app_error("Selected uploaded content/model answer/rubric उपलब्ध नहीं है।",400)
        if evaluation_type in ("DAILY","PYQ") and not content_set_has_rubric(source_id):
            return app_error("इस content का Rubric अभी upload नहीं हुआ है।",400)
        try:
            with engine.connect() as conn:
                trial_question_estimate = int(conn.exec_driver_sql("SELECT COUNT(*) FROM content_items WHERE set_id=%s", (str(source_id),)).scalar() or 1)
        except Exception:
            trial_question_estimate = 1
    if not files:
        return app_error("Copy upload करें।", 400)

    total_bytes = 0
    prepared = []
    try:
        for upload in files[:12]:
            data = await upload.read()
            total_bytes += len(data)
            if total_bytes > APP_MAX_UPLOAD_BYTES:
                return app_error("Copy size 20 MB से अधिक नहीं होनी चाहिए।", 413)
            if not data:
                continue
            name = upload.filename or "copy"
            suffix = Path(name).suffix.lower()
            if suffix not in {".pdf", ".jpg", ".jpeg", ".png", ".webp"}:
                return app_error("केवल PDF/JPG/JPEG/PNG/WEBP files allowed हैं।", 400)
            prepared.append((name, data))
    finally:
        for upload in files:
            try:
                await upload.close()
            except Exception:
                pass

    if not prepared:
        return app_error("Valid copy file नहीं मिला।", 400)

    # Trial quota is consumed only after a successful evaluation in the background task.
    # For source-backed evaluations we can reject an oversized paper before starting Gemini.
    if access_source == "trial":
        try:
            session_trial = SessionLocal()
            try:
                tu = session_trial.get(DBUser, str(uid))
                if tu and int(tu.trial_questions_used or 0) + int(trial_question_estimate or 1) > int(tu.trial_questions_limit or 10):
                    return app_error("Trial limit reached — अधिकतम 3 copies OR 10 questions allowed हैं।", 403)
            finally:
                session_trial.close()
        except Exception:
            pass

    # One PDF is used directly. Multiple images are merged into one PDF.
    if len(prepared) == 1 and Path(prepared[0][0]).suffix.lower() == ".pdf":
        original_filename = prepared[0][0]
        path = save_submission(prepared[0][1], ".pdf")
    else:
        images = []
        for name, data in prepared:
            try:
                image = Image.open(io.BytesIO(data)).convert("RGB")
                images.append(image)
            except Exception:
                return app_error(f"Image पढ़ी नहीं जा सकी: {name}", 400)
        pdf_buffer = io.BytesIO()
        images[0].save(
            pdf_buffer,
            format="PDF",
            save_all=True,
            append_images=images[1:]
        )
        original_filename = f"Prana_Copy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        path = save_submission(pdf_buffer.getvalue(), ".pdf")

    submission_id = create_webapp_submission(uid, paper, original_filename, exam=exam, evaluation_type=evaluation_type, source_id=source_id, medium=medium)
    background_tasks.add_task(
        run_webapp_evaluation, submission_id, uid, path, paper, original_filename, exam, evaluation_type, source_id, medium
    )
    return {
        "ok": True,
        "submission_id": submission_id,
        "status": "processing",
        "paper": paper,
        "filename": original_filename,
    }


@app.get("/api/app/evaluation-status/{submission_id}")
def app_evaluation_status(submission_id: str, request: Request):
    uid = require_app_user(request)
    if not uid:
        return app_error("Unauthorized", 401)
    session = SessionLocal()
    try:
        row = session.get(DBSubmission, submission_id)
        if not row or row.telegram_user_id != uid:
            return app_error("Evaluation not found", 404)
        return {
            "ok": True,
            "id": row.id,
            "status": row.status,
            "paper": row.paper,
            "exam": row.exam,
            "evaluation_type": row.evaluation_type,
            "source_id": row.source_id,
            "language": row.copy_language,
            "obtained": row.total_obtained_marks,
            "max": row.total_max_marks,
        }
    finally:
        session.close()


# ============================================================
# MINI APP BRANDING / STATIC FILES
# ============================================================

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
