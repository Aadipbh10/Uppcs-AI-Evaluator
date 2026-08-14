import os
import io
import json
import base64
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
    return {"status": "PRANA PCS Telegram Chat Evaluator is 100% Running!"}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

def get_best_available_model():
    """आपकी API Key पर उपलब्ध एक्टिव Flash मॉडल को ऑटो-डिटेक्ट करना"""
    try:
        models_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        r = requests.get(models_url, timeout=10)
        if r.status_code == 200:
            available = [m['name'] for m in r.json().get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
            # Flash मॉडल्स की प्राथमिकता सूची
            for target in ["models/gemini-2.0-flash", "models/gemini-1.5-flash", "models/gemini-1.5-flash-latest", "models/gemini-1.5-pro", "models/gemini-pro"]:
                if target in available:
                    return target
            if available:
                return available[0]
    except Exception:
        pass
    return "models/gemini-1.5-flash-latest"

def evaluate_with_gemini(images_b64, total_pages):
    active_model = get_best_available_model()
    # अगर नाम में पहले से 'models/' लगा है तो सही URL बनाना
    model_name = active_model.replace("models/", "")
    
    parts = []
    for b64 in images_b64:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": b64
            }
        })
    
    prompt = f"""
    आप UPPCS मुख्य परीक्षा के वरिष्ठ एवं मुख्य परीक्षक हैं।
    उत्तर पुस्तिका के कुल पृष्ठ: {total_pages}

    इस उत्तर पुस्तिका का अत्यंत निष्पक्ष और गहन मूल्यांकन करें:
    1. उत्तरों को पढ़कर पूर्णांक (Max Marks) और प्राप्तांक (Obtained Marks) तय करें।
    2. UP विशेष तथ्य, बजट, योजनाएं, आंकड़े, संरचना (भूमिका, मुख्य भाग, निष्कर्ष) और प्रस्तुति के आधार पर अंक दें।
    
    आउटपुट केवल और केवल इस JSON प्रारूप में दें:
    {{
        "obtained_marks": 5.5,
        "max_marks": 8,
        "feedback": "उत्तर की संरचना सुव्यवस्थित है। भूमिका संक्षिप्त और सटीक है। मुख्य भाग में UP विशेष आंकड़ों का समावेश ठीक है।",
        "improvements": [
            "निष्कर्ष को 2-3 पंक्तियों में और अधिक भविष्योन्मुखी बनाएं।",
            "UP सरकार की नवीनतम योजनाओं का सटीक संदर्भ दें।",
            "मुख्य बिंदुओं को रेखांकित (underline) करें।"
        ]
    }}
    """
    parts.append({"text": prompt})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=120)
    
    if res.status_code == 200:
        resp_json = res.json()
        raw_text = resp_json['candidates'][0]['content']['parts'][0]['text']
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    else:
        raise Exception(f"Gemini API Error ({res.status_code}): {res.text[:120]}")

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
        welcome_text = (
            "🏛️ <b>PRANA PCS - AI Mains Answer Evaluator</b>\n\n"
            "नमस्ते! UPPCS मुख्य परीक्षा उत्तर पुस्तिका मूल्यांकन प्रणाली में आपका स्वागत है।\n\n"
            "📌 <b>उत्तर पुस्तिका जांचने के लिए:</b>\n"
            "👉 सीधे अपनी उत्तर पुस्तिका की <b>PDF फ़ाइल</b> या <b>फ़ोटो</b> यहाँ भेजें।"
        )
        bot.reply_to(message, welcome_text)

    @bot.message_handler(content_types=['document', 'photo'])
    def handle_answer_sheet(message):
        chat_id = message.chat.id
        status_msg = bot.reply_to(message, "⏳ <b>आपकी उत्तर पुस्तिका प्राप्त हो गई है।</b>\nPRANA PCS AI परीक्षक द्वारा मूल्यांकन किया जा रहा है, कृपया प्रतीक्षा करें...")
        
        try:
            pdf_doc = fitz.open()
            images_b64 = []

            if message.content_type == 'document':
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.lower().endswith(".pdf"):
                    pdf_doc = fitz.open(stream=downloaded_file, filetype="pdf")
                    for page in pdf_doc:
                        pix = page.get_pixmap(dpi=100)
                        images_b64.append(base64.b64encode(pix.tobytes("jpeg")).decode("utf-8"))
                else:
                    img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                    img_stream = io.BytesIO()
                    img.save(img_stream, format="JPEG", quality=85)
                    images_b64.append(base64.b64encode(img_stream.getvalue()).decode("utf-8"))
                    page = pdf_doc.new_page(width=img.width, height=img.height)
                    page.insert_image(page.rect, stream=img_stream.getvalue())

            elif message.content_type == 'photo':
                file_info = bot.get_file(message.photo[-1].file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                img_stream = io.BytesIO()
                img.save(img_stream, format="JPEG", quality=85)
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
                f"📝 <b>परीक्षक की समीक्षा:</b>\n{feedback}\n\n"
                f"💡 <b>सुधार के मुख्य बिंदु:</b>\n{imp_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>जांची हुई प्रमाणित कॉपी नीचे संलग्न है 👇</i>"
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
                f"⚠️ मूल्यांकन के दौरान समस्या आई: {str(e)[:150]}\nकृपया स्पष्ट PDF या फोटो पुनः भेजें।",
                chat_id=chat_id,
                message_id=status_msg.message_id
            )

def run_telebot():
    if bot:
        bot.infinity_polling(timeout=20, long_polling_timeout=20)

threading.Thread(target=run_telebot, daemon=True).start()
