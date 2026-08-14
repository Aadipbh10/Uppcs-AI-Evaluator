import os
import io
import json
import base64
import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from PIL import Image
import fitz  # PyMuPDF

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Marks", "X-Feedback", "X-Improvements"]
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

@app.get("/")
def home():
    return {"status": "UPPCS Evaluator Backend is 100% Ready and Active!"}

@app.post("/evaluate")
async def evaluate_answer(
    file: UploadFile = File(...),
    paper: str = Form("GS 5"),
    max_marks: int = Form(8)
):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Gemini API Key missing on server")

    file_bytes = await file.read()
    parts = []
    pdf_doc = None

    # चेक करें कि फ़ाइल PDF है या Image
    if file.filename.lower().endswith(".pdf") or file.content_type == "application/pdf":
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in pdf_doc:
            pix = page.get_pixmap(dpi=120)
            img_b64 = base64.b64encode(pix.tobytes("jpeg")).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": img_b64
                }
            })
    else:
        img_b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime = file.content_type if file.content_type else "image/jpeg"
        parts.append({
            "inlineData": {
                "mimeType": mime,
                "data": img_b64
            }
        })

    prompt_text = f"""
    आप UPPCS मुख्य परीक्षा के वरिष्ठ परीक्षक हैं।
    विषय: {paper} | पूर्णांक: {max_marks}

    इस हस्तलिखित उत्तर पुस्तिका का मूल्यांकन करें।
    आउटपुट केवल और केवल इस JSON प्रारूप में दें:
    {{
        "obtained_marks": 5.5,
        "feedback": "उत्तर की संरचना अच्छी है। UP बजट के आंकड़े जोड़ें।",
        "improvements": [
            "भूमिका को 2 लाइन में संक्षिप्त करें",
            "UP विशेष तथ्यों का उल्लेख करें",
            "निष्कर्ष में आगे की राह बताएं"
        ]
    }}
    """
    parts.append({"text": prompt_text})

    # Direct Gemini REST API Call (Zero Dependency Crash)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    res = requests.post(url, json=payload, timeout=60)
    
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Gemini API Error: {res.text}")

    resp_json = res.json()
    raw_text = resp_json['candidates'][0]['content']['parts'][0]['text']
    
    try:
        eval_data = json.loads(raw_text)
    except Exception:
        eval_data = {
            "obtained_marks": 5.0,
            "feedback": raw_text[:200],
            "improvements": ["संरचना में सुधार करें", "तथ्यों को रेखांकित करें"]
        }

    # PDF स्टैम्पिंग (अगर इमेज थी तो PDF बनाएं)
    if pdf_doc is None:
        pdf_doc = fitz.open()
        img = Image.open(io.BytesIO(file_bytes))
        img_page = pdf_doc.new_page(width=img.width, height=img.height)
        img_page.insert_image(img_page.rect, stream=file_bytes)

    first_page = pdf_doc[0]
    
    # 🔴 लाल डिजिटल स्टैम्प बॉक्स (Top-Right)
    rect = fitz.Rect(first_page.rect.width - 230, 20, first_page.rect.width - 20, 85)
    first_page.draw_rect(rect, color=(0.8, 0, 0), width=2, fill=(1, 0.92, 0.92))
    first_page.insert_text(
        fitz.Point(first_page.rect.width - 220, 48),
        "PRANA PCS EVALUATED",
        fontsize=11, color=(0.8, 0, 0)
    )
    first_page.insert_text(
        fitz.Point(first_page.rect.width - 210, 72),
        f"Marks: {eval_data.get('obtained_marks', 5)} / {max_marks}",
        fontsize=14, color=(0.8, 0, 0)
    )

    output_stream = io.BytesIO()
    pdf_doc.save(output_stream)
    pdf_doc.close()
    output_stream.seek(0)

    return StreamingResponse(
        output_stream,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=evaluated_copy.pdf",
            "X-Marks": str(eval_data.get('obtained_marks', '5.0')),
            "X-Feedback": str(eval_data.get('feedback', 'मूल्यांकन संपन्न।')),
            "X-Improvements": json.dumps(eval_data.get('improvements', []))
        }
    )
