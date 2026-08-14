import os
import io
import json
import time
import base64
import threading
import requests
from fastapi import FastAPI
import telebot
from PIL import Image
import pymupdf as fitz

app = FastAPI()

@app.get("/")
def home():
    return {"status": "PRANA PCS Hindi Font Evaluator Active!"}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

# हिंदी (देवनागरी) फ़ॉन्ट डाउनलोड करना
HINDI_FONT_PATH = "/tmp/NotoSansDevanagari-Regular.ttf"

def ensure_hindi_font():
    if not os.path.exists(HINDI_FONT_PATH):
        try:
            url = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf"
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                with open(HINDI_FONT_PATH, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            print("Font Download Error:", e)

ensure_hindi_font()

ACTIVE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash"
]

def evaluate_with_gemini(images_b64, total_pages):
    parts = []
    for b64 in images_b64:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": b64
            }
        })
    
    prompt = f"""
    आप UPPCS मुख्य परीक्षा के वरिष्ठ परीक्षक हैं।
    उत्तर पुस्तिका के कुल पृष्ठ संख्या: {total_pages}

    इस उत्तर पुस्तिका का अत्यंत गंभीर और पृष्ठ-वार (Page by Page) मूल्यांकन करें:
    1. उत्तरों को पढ़कर पूर्णांक (Max Marks) और प्राप्तांक (Obtained Marks) दें।
    2. प्रत्येक पृष्ठ के लिए 2 से 3 संक्षिप्त लाल-पेन निर्देश/टिप्पणियां (Margin Comments) दें जिन्हें कॉपी के साइड मार्जिन में लिखा जा सके (उदा. '✓ भूमिका स्पष्ट', '→ UP डेटा जोड़ें', '✓ विश्लेषण सही', '→ मैप/फ्लोचार्ट बनाएं')।
    
    आउटपुट केवल और केवल इस JSON प्रारूप में दें:
    {{
        "obtained_marks": 16.0,
        "max_marks": 24,
        "feedback": "उत्तरों की संरचना अत्यधिक सुव्यवस्थित है। सरकारी योजनाओं का सटीक समावेश किया गया है।",
        "improvements": [
            "उत्तर प्रदेश (UP) के विशेष संदर्भों का उल्लेख करें।",
            "सिंचाई और वर्षा के प्रश्न में UP का मानचित्र बनाएं।"
        ],
        "page_annotations": [
            ["✓ भूमिका स्पष्ट व सटीक", "→ UP विशेष डेटा जोड़ें"],
            ["✓ योजनाओं का अच्छा समावेश", "✓ प्रवाह ठीक है"]
        ]
    }}
    """
    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    last_err = ""
    for model_name in ACTIVE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=40)
            if res.status_code == 200:
                raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                clean_text = raw_text.replace("```json", "").replace("```", "").strip()
                return json.loads(clean_text)
            elif res.status_code in [503, 429]:
                time.sleep(1)
                continue
            else:
                last_err = f"{model_name}: {res.text[:80]}"
        except Exception as e:
            last_err = str(e)
            continue

    raise Exception(f"AI मूल्यांकन में समस्या: {last_err}")

def create_rich_annotated_pdf(pdf_doc, eval_data):
    """हिंदी फ़ॉन्ट के साथ साइड मार्जिन में लाल पेन से निर्देश लिखना"""
    obtained = eval_data.get("obtained_marks", 5.5)
    max_m = eval_data.get("max_marks", 8)
    page_notes = eval_data.get("page_annotations", [])
    
    font_file = HINDI_FONT_PATH if os.path.exists(HINDI_FONT_PATH) else None
    
    total_p = len(pdf_doc)
    
    for idx in range(total_p):
        page = pdf_doc[idx]
        w, h = page.rect.width, page.rect.height
        
        # 1. पहले पेज पर हेडर स्टैम्प
        if idx == 0:
            header_rect = fitz.Rect(w - 240, 20, w - 20, 85)
            page.draw_rect(header_rect, color=(0.85, 0, 0), width=1.5, fill=(1, 0.95, 0.95))
            page.insert_text(fitz.Point(w - 230, 44), "PRANA PCS EVALUATED", fontsize=11, color=(0.85, 0, 0))
            page.insert_text(fitz.Point(w - 225, 70), f"Marks: {obtained} / {max_m}", fontsize=14, color=(0.85, 0, 0))

        # 2. दाएँ साइड मार्जिन में हिंदी नोट्स
        notes = page_notes[idx] if idx < len(page_notes) else ["✓ विश्लेषण सही है", "→ प्रस्तुति में सुधार करें"]
        
        y_pos = 140
        for note in notes[:3]:
            box = fitz.Rect(w - 150, y_pos, w - 10, y_pos + 50)
            page.draw_rect(box, color=(0.85, 0, 0), width=0.8, fill=(1, 0.96, 0.96))
            
            # हिंदी फ़ॉन्ट के साथ टेक्स्ट लिखें
            page.insert_textbox(
                box,
                note,
                fontsize=9,
                fontfile=font_file,
                color=(0.85, 0, 0),
                align=fitz.TEXT_ALIGN_CENTER
            )
            y_pos += 65

        # 3. पेज के नीचे लाल अंक घेरा
        circle_center = fitz.Point(w - 60, h - 70)
        page.draw_circle(circle_center, 22, color=(0.85, 0, 0), width=1.5)
        page.insert_text(fitz.Point(w - 74, h - 64), f"✓ {obtained}", fontsize=11, color=(0.85, 0, 0))

    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    return out

if bot:
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(
            message,
            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "नमस्ते! अपनी उत्तर पुस्तिका की <b>PDF फ़ाइल</b> या <b>फ़ोटो</b> भेजें।"
        )

    @bot.message_handler(content_types=['document', 'photo'])
    def handle_answer_sheet(message):
        chat_id = message.chat.id
        status_msg = bot.reply_to(message, "⏳ <b>कॉपी प्राप्त हो गई है।</b>\nPRANA PCS AI द्वारा लाल-पेन मार्जिन मूल्यांकन चल रहा है...")
        
        try:
            pdf_doc = fitz.open()
            images_b64 = []

            if message.content_type == 'document':
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.lower().endswith(".pdf"):
                    pdf_doc = fitz.open(stream=downloaded_file, filetype="pdf")
                    for page in pdf_doc:
                        pix = page.get_pixmap(dpi=60)
                        images_b64.append(base64.b64encode(pix.tobytes("jpeg")).decode("utf-8"))
                else:
                    img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                    img_stream = io.BytesIO()
                    img.save(img_stream, format="JPEG", quality=70)
                    images_b64.append(base64.b64encode(img_stream.getvalue()).decode("utf-8"))
                    page = pdf_doc.new_page(width=img.width, height=img.height)
                    page.insert_image(page.rect, stream=img_stream.getvalue())

            elif message.content_type == 'photo':
                file_info = bot.get_file(message.photo[-1].file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                img_stream = io.BytesIO()
                img.save(img_stream, format="JPEG", quality=70)
                images_b64.append(base64.b64encode(img_stream.getvalue()).decode("utf-8"))
                page = pdf_doc.new_page(width=img.width, height=img.height)
                page.insert_image(page.rect, stream=img_stream.getvalue())

            total_pages = len(pdf_doc)

            eval_result = evaluate_with_gemini(images_b64, total_pages)
            
            obtained = eval_result.get("obtained_marks", 5.5)
            max_m = eval_result.get("max_marks", 8)
            feedback = eval_result.get("feedback", "मूल्यांकन संपन्न हुआ।")
            improvements = eval_result.get("improvements", [])

            imp_text = "\n".join([f"• {item}" for item in improvements])
            result_caption = (
                f"🏛️ <b>PRANA PCS - मूल्यांकन रिपोर्ट</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>कुल प्राप्तांक:</b> <code>{obtained} / {max_m}</code>\n\n"
                f"📝 <b>समीक्षा:</b> {feedback}\n\n"
                f"💡 <b>सुझाव:</b>\n{imp_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>जांची हुई प्रमाणित कॉपी (हिंदी मार्जिन नोट्स सहित) नीचे संलग्न है 👇</i>"
            )

            stamped_pdf = create_rich_annotated_pdf(pdf_doc, eval_result)

            bot.delete_message(chat_id, status_msg.message_id)
            bot.send_document(
                chat_id=chat_id,
                document=stamped_pdf,
                visible_file_name="Evaluated_Copy_PranaPCS.pdf",
                caption=result_caption
            )

        except Exception as e:
            bot.edit_message_text(
                f"⚠️ मूल्यांकन में समस्या: {str(e)[:150]}\nकृपया स्पष्ट PDF या फोटो पुनः भेजें।",
                chat_id=chat_id,
                message_id=status_msg.message_id
            )

def run_telebot():
    if bot:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        time.sleep(3)
        bot.infinity_polling(timeout=15, long_polling_timeout=15, skip_pending=True)

threading.Thread(target=run_telebot, daemon=True).start()
