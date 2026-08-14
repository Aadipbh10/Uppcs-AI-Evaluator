import os
import io
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from PIL import Image
from google import genai
from google.genai import types
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
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

@app.get("/")
def home():
    return {"status": "UPPCS Evaluator Backend is 100% Active!"}

@app.post("/evaluate")
async def evaluate_answer(
    file: UploadFile = File(...),
    paper: str = Form("GS 5"),
    max_marks: int = Form(8)
):
    if not client:
        raise HTTPException(status_code=500, detail="Gemini API Key missing on server")

    file_bytes = await file.read()
    images = []
    pdf_doc = None

    # चेक करें PDF है या Image
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

    prompt = f"""
    आप UPPCS मुख्य परीक्षा के वरिष्ठ परीक्षक हैं।
    विषय: {paper} | पूर्णांक: {max_marks}

    इस हस्तलिखित उत्तर का संपूर्ण मूल्यांकन करें।
    आउटपुट केवल इस JSON फॉर्मेट में दें:
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

    # Gemini 2.5 Flash API Call
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[*images, prompt]
    )

    try:
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        eval_data = json.loads(clean_text)
    except Exception:
        eval_data = {
            "obtained_marks": 5.0,
            "feedback": response.text[:200] if response.text else "मूल्यांकन संपन्न।",
            "improvements": ["संरचना में सुधार करें", "तथ्यों को रेखांकित करें"]
        }

    # PDF स्टैम्पिंग
    if pdf_doc is None:
        pdf_doc = fitz.open()
        img_page = pdf_doc.new_page(width=images[0].width, height=images[0].height)
        img_page.insert_image(img_page.rect, stream=file_bytes)

    first_page = pdf_doc[0]
    
    # लाल डिजिटल स्टैम्प
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
