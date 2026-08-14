import os
import io
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from PIL import Image
import google.generativeai as genai
import fitz  # PyMuPDF

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

@app.get("/")
def home():
    return {"status": "UPPCS PDF & Image Evaluator Active!"}

@app.post("/evaluate")
async def evaluate_answer(
    file: UploadFile = File(...),
    paper: str = Form("GS 5"),
    max_marks: int = Form(8)
):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Gemini API Key missing")

    file_bytes = await file.read()
    images = []
    pdf_doc = None

    # चेक करें कि फ़ाइल PDF है या Image
    if file.filename.lower().endswith(".pdf") or file.content_type == "application/pdf":
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page in pdf_doc:
            pix = page.get_pixmap(dpi=150)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            images.append(img)
    else:
        img = Image.open(io.BytesIO(file_bytes))
        images.append(img)

    if not images:
        raise HTTPException(status_code=400, detail="कोई वैध पेज नहीं मिला")

    # Gemini 1.5 Flash से मूल्यांकन
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"""
    आप UPPCS मुख्य परीक्षा के वरिष्ठ परीक्षक हैं।
    विषय: {paper} | पूर्णांक: {max_marks}

    इस उत्तर पुस्तिका का मूल्यांकन करें।
    आउटपुट केवल इस JSON फॉर्मेट में दें:
    {{
        "obtained_marks": 5.5,
        "feedback": "उत्तर की संरचना अच्छी है। UP बजट के आंकड़े जोड़ें।",
        "improvements": [
            "भूमिका 2 लाइन में संक्षिप्त करें",
            "UP विशेष तथ्यों/योजनाओं का उल्लेख करें",
            "निष्कर्ष भविष्योन्मुखी रखें"
        ]
    }}
    """

    response = model.generate_content(images + [prompt])

    try:
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        eval_data = json.loads(clean_text)
    except Exception:
        eval_data = {
            "obtained_marks": 5.0,
            "feedback": response.text[:200],
            "improvements": ["संरचना में सुधार करें", "तथ्यों को रेखांकित करें"]
        }

    # PDF पर लाल पेन से रिमार्क्स और स्टैम्प जोड़ना
    if pdf_doc is None:
        # अगर इमेज थी, तो नया PDF बनाएं
        pdf_doc = fitz.open()
        img_page = pdf_doc.new_page(width=images[0].width, height=images[0].height)
        img_page.insert_image(img_page.rect, stream=file_bytes)

    first_page = pdf_doc[0]
    
    # 1. लाल रंग का स्टैम्प बॉक्स (Top-Right)
    rect = fitz.Rect(first_page.rect.width - 220, 20, first_page.rect.width - 20, 80)
    first_page.draw_rect(rect, color=(0.8, 0, 0), width=2, fill=(1, 0.9, 0.9))
    first_page.insert_text(
        fitz.Point(first_page.rect.width - 210, 45),
        f"PRANA PCS EVALUATED",
        fontsize=11, color=(0.8, 0, 0)
    )
    first_page.insert_text(
        fitz.Point(first_page.rect.width - 200, 68),
        f"Marks: {eval_data['obtained_marks']} / {max_marks}",
        fontsize=14, color=(0.8, 0, 0)
    )

    # 2. बॉटम में फीडबैक नोट जोड़ना
    feedback_rect = fitz.Rect(20, first_page.rect.height - 70, first_page.rect.width - 20, first_page.rect.height - 10)
    first_page.draw_rect(feedback_rect, color=(0.8, 0, 0), width=1, fill=(1, 0.95, 0.95))
    first_page.insert_text(
        fitz.Point(30, first_page.rect.height - 45),
        f"Feedback: {eval_data['feedback'][:80]}...",
        fontsize=10, color=(0.8, 0, 0)
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
            "X-Marks": str(eval_data['obtained_marks']),
            "X-Feedback": eval_data['feedback'],
            "X-Improvements": json.dumps(eval_data['improvements'])
        }
    )
