import os
import io
import json
import time
import threading
import requests
from fastapi import FastAPI
import telebot
from PIL import Image
import pymupdf as fitz

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

app = FastAPI()

@app.get("/")
def health_check():
    return {"status": "PRANA PCS Fast Files-API Evaluator Active!"}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

def upload_to_gemini_files(file_bytes, mime_type="application/pdf"):
    """Google Files API पर सीधे फाइल अपलोड (0% RAM यूसेज)"""
    upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}"
    headers = {
        "X-Goog-Upload-Command": "start, upload, finalize",
        "X-Goog-Upload-Header-Content-Length": str(len(file_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": mime_type
    }
    r = requests.post(upload_url, data=file_bytes, headers=headers, timeout=30)
    if r.status_code == 200:
        return r.json()['file']['uri']
    raise Exception(f"File Upload Failed: {r.text[:80]}")

def evaluate_with_gemini(file_uri, total_pages):
    """सीधे अपलोडेड फाइल URI से मूल्यांकन"""
    prompt = f"""
    आप UPPCS मुख्य परीक्षा (Civil Services Mains) के वरिष्ठ परीक्षक हैं।
    कुल पृष्ठ: {total_pages}

    इस उत्तर पुस्तिका का अत्यंत निष्पक्ष और गहन मूल्यांकन करें:
    1. उत्तरों को पढ़कर पूर्णांक (Max Marks) और प्राप्तांक (Obtained Marks) दें।
    2. UP विशेष तथ्य, संरचना (भूमिका, मुख्य भाग, निष्कर्ष) और प्रस्तुति के आधार पर अंक दें।
    
    आउटपुट केवल और केवल इस JSON प्रारूप में दें:
    {{
        "obtained_marks": 5.5,
        "max_marks": 8,
        "feedback": "उत्तर की संरचना सुव्यवस्थित है। मुख्य भाग में UP विशेष तथ्यों का समावेश ठीक है।",
        "improvements": [
            "निष्कर्ष को और अधिक भविष्योन्मुखी बनाएं।",
            "UP सरकार की नवीनतम योजनाओं का सटीक संदर्भ दें।"
        ]
    }}
    """

    payload = {
        "contents": [{
            "parts": [
                {"file_data": {"mime_type": "application/pdf", "file_uri": file_uri}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    models = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"]
    last_err = ""

    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
            if res.status_code == 200:
                raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                clean_text = raw_text.replace("```json", "").replace("```", "").strip()
                return json.loads(clean_text)
            elif res.status_code in [503, 429]:
                continue
            else:
                last_err = res.text[:80]
        except Exception as e:
            last_err = str(e)
            continue

    raise Exception(f"AI मूल्यांकन में समस्या: {last_err}")

def create_stamped_pdf(pdf_doc, obtained, max_m):
    first_page = pdf_doc[0]
    rect = fitz.Rect(first_page.rect.width - 250, 20, first_page.rect.width - 20, 95)
    first_page.draw_rect(rect, color=(0.8, 0, 0), width=2, fill=(1, 0.93, 0.93))
    first_page.insert_text(
        fitz.Point(first_page.rect.width - 240, 48),
        "PRANA PCS EVALUATED",
        fontsize=11, color=(0.8, 0, 0)
    )
    first_page.insert_text(
        fitz.Point(first_page.rect.width - 235, 75),
        f"Marks: {obtained} / {max_m}",
        fontsize=15, color=(0.8, 0, 0)
    )
    out = io.BytesIO()
    pdf_doc.save(out)
    pdf_doc.close()
    out.seek(0)
    return out

if bot:
    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(message, "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\nअपनी उत्तर पुस्तिका की <b>PDF फ़ाइल</b> या <b>फ़ोटो</b> भेजें।")

    @bot.message_handler(content_types=['document', 'photo'])
    def handle_answer_sheet(message):
        chat_id = message.chat.id
        status_msg = bot.reply_to(message, "⏳ <b>कॉपी प्राप्त हो गई है।</b>\nAI मूल्यांकन चल रहा है (लगभग 8-12 सेकंड)...")
        
        try:
            pdf_doc = fitz.open()

            if message.content_type == 'document':
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.lower().endswith(".pdf"):
                    pdf_doc = fitz.open(stream=downloaded_file, filetype="pdf")
                else:
                    img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                    img_stream = io.BytesIO()
                    img.save(img_stream, format="JPEG", quality=80)
                    page = pdf_doc.new_page(width=img.width, height=img.height)
                    page.insert_image(page.rect, stream=img_stream.getvalue())

            elif message.content_type == 'photo':
                file_info = bot.get_file(message.photo[-1].file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                img_stream = io.BytesIO()
                img.save(img_stream, format="JPEG", quality=80)
                page = pdf_doc.new_page(width=img.width, height=img.height)
                page.insert_image(page.rect, stream=img_stream.getvalue())

            total_pages = len(pdf_doc)
            raw_pdf_bytes = pdf_doc.tobytes()

            # 1. Google Files API पर अपलोड
            file_uri = upload_to_gemini_files(raw_pdf_bytes)

            # 2. AI से त्वरित मूल्यांकन
            eval_result = evaluate_with_gemini(file_uri, total_pages)
            
            obtained = eval_result.get("obtained_marks", 5.5)
            max_m = eval_result.get("max_marks", 8)
            feedback = eval_result.get("feedback", "मूल्यांकन संपन्न हुआ।")
            improvements = eval_result.get("improvements", [])

            imp_text = "\n".join([f"• {item}" for item in improvements])
            result_caption = (
                f"🏛️ <b>PRANA PCS - मूल्यांकन रिपोर्ट</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>कुल प्राप्तांक:</b> <code>{obtained} / {max_m}</code>\n\n"
                f"📝 <b>समीक्षा:</b>\n{feedback}\n\n"
                f"💡 <b>सुझाव:</b>\n{imp_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>जांची हुई कॉपी नीचे संलग्न है 👇</i>"
            )

            stamped_pdf = create_stamped_pdf(pdf_doc, obtained, max_m)

            bot.delete_message(chat_id, status_msg.message_id)
            bot.send_document(
                chat_id=chat_id,
                document=stamped_pdf,
                visible_file_name="Evaluated_Copy_PranaPCS.pdf",
                caption=result_caption
            )

        except Exception as e:
            bot.edit_message_text(
                f"⚠️ मूल्यांकन में समस्या: {str(e)[:120]}\nकृपया पुनः प्रयास करें।",
                chat_id=chat_id,
                message_id=status_msg.message_id
            )

def run_telebot():
    if bot:
        try:
            bot.remove_webhook()
            time.sleep(2)
        except Exception:
            pass
        bot.infinity_polling(timeout=15, long_polling_timeout=15, skip_pending=True)

threading.Thread(target=run_telebot, daemon=True).start()
