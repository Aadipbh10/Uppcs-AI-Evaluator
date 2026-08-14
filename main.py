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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

app = FastAPI()

@app.get("/")
def health_check():
    return {"status": "PRANA PCS Dynamic AI Evaluator is 100% Active!"}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

def get_live_gemini_models():
    """आपकी API Key के लिए Google पर लाइव उपलब्ध सभी मॉडल्स की रीयल-टाइम लिस्ट"""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            models = [
                m['name'] for m in data.get('models', [])
                if 'generateContent' in m.get('supportedGenerationMethods', [])
            ]
            # Flash मॉडल्स को प्राथमिकता दें
            flash_models = [m for m in models if 'flash' in m.lower()]
            other_models = [m for m in models if 'flash' not in m.lower()]
            return flash_models + other_models
    except Exception:
        pass
    return ["models/gemini-2.5-flash", "models/gemini-2.0-flash", "models/gemini-1.5-flash"]

def evaluate_with_gemini(images_b64, total_pages):
    live_models = get_live_gemini_models()
    
    parts = []
    for b64 in images_b64:
        parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": b64
            }
        })
    
    prompt = f"""
    आप UPPCS मुख्य परीक्षा (Civil Services Mains) के वरिष्ठ परीक्षक हैं।
    कुल पृष्ठ संख्या: {total_pages}

    इस उत्तर पुस्तिका का अत्यंत निष्पक्ष, गंभीर और विस्तृत मूल्यांकन करें:
    1. उत्तरों को पढ़कर पूर्णांक (Max Marks) और प्राप्तांक (Obtained Marks) दें।
    2. UP विशेष तथ्य, बजट, योजनाएं, आंकड़े, संरचना (भूमिका, मुख्य भाग, निष्कर्ष) और प्रस्तुति के आधार पर अंक दें।
    
    आउटपुट केवल और केवल इस JSON प्रारूप में दें:
    {{
        "obtained_marks": 5.5,
        "max_marks": 8,
        "feedback": "उत्तर की संरचना सुव्यवस्थित है। भूमिका संक्षिप्त और सटीक है। मुख्य भाग में UP विशेष आंकड़ों का अच्छा समावेश किया गया है।",
        "improvements": [
            "निष्कर्ष को 2-3 पंक्तियों में और अधिक भविष्योन्मुखी (Way Forward) बनाएं।",
            "UP सरकार की नवीनतम योजनाओं का सटीक संदर्भ दें।",
            "मुख्य बिंदुओं को रेखांकित (underline) करें।"
        ]
    }}
    """
    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"}
    }

    last_err = ""
    for model_resource in live_models:
        clean_model = model_resource if model_resource.startswith("models/") else f"models/{model_resource}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{clean_model}:generateContent?key={GEMINI_API_KEY}"
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
                last_err = f"{clean_model}: {res.text[:80]}"
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
        bot.reply_to(
            message,
            "🏛️ <b>PRANA PCS AI Mains Evaluator</b>\n\n"
            "नमस्ते! अपनी उत्तर पुस्तिका की <b>PDF फ़ाइल</b> या <b>फ़ोटो</b> भेजें।"
        )

    @bot.message_handler(content_types=['document', 'photo'])
    def handle_answer_sheet(message):
        chat_id = message.chat.id
        status_msg = bot.reply_to(message, "⏳ <b>कॉपी प्राप्त हो गई है।</b>\nPRANA PCS AI द्वारा मूल्यांकन चल रहा है...")
        
        try:
            pdf_doc = fitz.open()
            images_b64 = []

            if message.content_type == 'document':
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.lower().endswith(".pdf"):
                    pdf_doc = fitz.open(stream=downloaded_file, filetype="pdf")
                    for page in pdf_doc:
                        # 60 DPI - सुपर लाइटवेट और फ़ास्ट प्रोसेसिंग
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
                f"⚠️ मूल्यांकन में समस्या: {str(e)[:150]}\nकृपया स्पष्ट PDF या फोटो पुनः भेजें।",
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
