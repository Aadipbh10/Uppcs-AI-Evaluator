import os
import io
import json
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from google import genai

app = FastAPI()

# Frontend (Mini App) को कनेक्ट करने की अनुमति
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

@app.get("/")
def home():
    return {"status": "UPPCS AI Evaluator Backend Active!"}

@app.post("/evaluate")
async def evaluate_answer(
    file: UploadFile = File(...),
    paper: str = Form("GS 5"),
    max_marks: int = Form(8)
):
    image_bytes = await file.read()
    image = Image.open(io.BytesIO(image_bytes))

    prompt = f"""
    आप UPPCS मुख्य परीक्षा के सख्त और अनुभवी परीक्षक हैं।
    विषय: {paper} | पूर्णांक: {max_marks}

    इस हस्तलिखित उत्तर पुस्तिका का मूल्यांकन करें।
    विशेष रूप से जांचें:
    1. प्रश्न की मांग पूरी हुई या नहीं
    2. संरचना (भूमिका, मुख्य भाग, निष्कर्ष)
    3. UP विशेष तथ्य (जिलों के नाम, योजनाएं, डेटा)

    आउटपुट केवल और केवल इस JSON फॉर्मेट में दें:
    {{
        "obtained_marks": 5.5,
        "feedback": "उत्तर की संरचना अच्छी है। बुंदेलखंड के जिलों का उल्लेख करें।",
        "improvements": [
            "भूमिका को 2 लाइन में संक्षिप्त करें",
            "UP बजट या ODOP योजना का संदर्भ जोड़ें",
            "निष्कर्ष में भविष्योन्मुखी सुझाव दें"
        ]
    }}
    """

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[image, prompt]
    )

    try:
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_text)
    except Exception:
        result = {
            "obtained_marks": 5.0,
            "feedback": response.text,
            "improvements": ["संरचना और डेटा में सुधार करें"]
        }

    return result
