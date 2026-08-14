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

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# FastAPI App (Render Web Service को जिंदा रखने के लिए)
app = FastAPI()

@app.get("/")
def health_check():
    return {"status": "PRANA PCS Telegram Chat Evaluator is 100% Running!"}

# Telegram Bot Setup
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

def evaluate_with_gemini(images_b64, total_pages):
    """Gemini 2.5 Flash से गहन मूल्यांकन प्राप्त करना"""
    parts = []
    for b64 in images_b64:
        parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": b64
            }
        })
    
    prompt = f"""
    आप UPPCS मुख्य परीक्षा (Civil Services Mains) के वरिष्ठ एवं मुख्य परीक्षक हैं।
    उत्तर पुस्तिका के कुल पृष्ठ: {total_pages}

    इस उत्तर पुस्तिका का अत्यंत निष्पक्ष, गंभीर और विस्तृत मूल्यांकन करें:
    1. पहले जांचें कि कितने प्रश्नों का उत्तर दिया गया है और कुल पूर्णांक (Max Marks) तय करें (जैसे 1 प्रश्न = 8 या 12 अंक, पूरा पेपर = 100/200 अंक)।
    2. मूल्यांकन के मानक:
       - प्रश्न की मांग (Demand of Question)
       - संरचना (भूमिका, मुख्य भाग, निष्कर्ष)
       - UP विशेष तथ्य, बजट, योजनाएं, आंकड़े, मैप और फ्लोचार्ट्स
       - प्रस्तुतीकरण व स्पष्टता
    
    आउटपुट केवल और केवल इस JSON प्रारूप में दें:
    {{
        "obtained_marks": 5.5,
        "max_marks": 8,
        "feedback": "उत्तर की संरचना सुव्यवस्थित है। भूमिका संक्षिप्त और सटीक है। मुख्य भाग में UP विशेष आंकड़ों का अच्छा समावेश किया गया है।",
        "improvements": [
            "निष्कर्ष को 2-3 पंक्तियों में और अधिक भविष्योन्मुखी (Way Forward) बनाएं।",
            "UP सरकार की नवीनतम योजनाओं/नीतियों का उल्लेख करें।",
            "मुख्य बिंदुओं को रेखांकित (underline) करें।"
        ]
    }}
    """
    parts.append({"text": prompt})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=120)
    
    if res.status_code == 200:
        resp_json = res.json()
        raw_text = resp_json['candidates'][0]['content']['parts'][0]['text']
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    else:
        raise Exception(f"Gemini API Error: {res.text[:100]}")

def create_stamped_pdf(pdf_doc, obtained, max_m):
    """पहले पेज पर लाल रंग का PRANA PCS स्टैम्प लगाना"""
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
            "👉 सीधे अपनी उत्तर पुस्तिका की <b>PDF फ़ाइल</b> या <b>फ़ोटो</b> यहाँ भेजें।\n\n"
            "<i>(सिस्टम 1 प्रश्न से लेकर पूरी 55 पेज की कॉपी तक का विस्तृत मूल्यांकन करता है)</i>"
        )
        bot.reply_to(message, welcome_text)

    @bot.message_handler(content_types=['document', 'photo'])
    def handle_answer_sheet(message):
        chat_id = message.chat.id
        status_msg = bot.reply_to(message, "⏳ <b>आपकी उत्तर पुस्तिका प्राप्त हो गई है।</b>\nPRANA PCS AI परीक्षक द्वारा मूल्यांकन किया जा रहा है, कृपया प्रतीक्षा करें...")
        
        try:
            pdf_doc = fitz.open()
            images_b64 = []

            # 1. PDF फ़ाइल हैंडलिंग
            if message.content_type == 'document':
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                if message.document.file_name.lower().endswith(".pdf"):
                    pdf_doc = fitz.open(stream=downloaded_file, filetype="pdf")
                    for page in pdf_doc:
                        pix = page.get_pixmap(dpi=100)
                        images_b64.append(base64.b64encode(pix.tobytes("jpeg")).decode("utf-8"))
                else:
                    # यदि डॉक्यूमेंट के रूप में इमेज भेजी गई हो
                    img = Image.open(io.BytesIO(downloaded_file)).convert("RGB")
                    img_stream = io.BytesIO()
                    img.save(img_stream, format="JPEG", quality=85)
                    images_b64.append(base64.b64encode(img_stream.getvalue()).decode("utf-8"))
                    page = pdf_doc.new_page(width=img.width, height=img.height)
                    page.insert_image(page.rect, stream=img_stream.getvalue())

            # 2. फ़ोटो हैंडलिंग
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

            # AI Evaluation Call
            eval_result = evaluate_with_gemini(images_b64, total_pages)
            
            obtained = eval_result.get("obtained_marks", 5.5)
            max_m = eval_result.get("max_marks", 8)
            feedback = eval_result.get("feedback", "मूल्यांकन संपन्न हुआ।")
            improvements = eval_result.get("improvements", [])

            # Formatting Reply Text
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

            # स्टैम्प्ड PDF जनरेट करना
            stamped_pdf = create_stamped_pdf(pdf_doc, obtained, max_m)

            # छात्र को परिणाम और जांची हुई PDF भेजना
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

# Telegram Bot Polling को बैकग्राउंड थ्रेड में चलाना
def run_telebot():
    if bot:
        bot.infinity_polling(timeout=20, long_polling_timeout=20)

threading.Thread(target=run_telebot, daemon=True).start()
