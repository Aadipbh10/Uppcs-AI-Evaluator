"""Production entrypoint for PRANA PCS evaluator."""
import base64
import builtins
import hashlib
import hmac
import html
import json
import os
import time
import threading
from urllib.parse import parse_qs
import requests
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import desc
builtins.base64=base64; builtins.hashlib=hashlib; builtins.hmac=hmac; builtins.html=html; builtins.json=json; builtins.os=os; builtins.time=time; builtins.parse_qs=parse_qs; builtins.Response=Response; builtins.HTMLResponse=HTMLResponse; builtins.desc=desc
import main
_PROD_TELEGRAM_WEBAPP_PUBLIC_KEY=bytes.fromhex("e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d")
def _check_string(values,exclude_signature=False):
    return "\n".join(f"{k}={values[k][0]}" for k in sorted(values) if k!="hash" and not(exclude_signature and k=="signature"))
def robust_telegram_webapp_validate(init_data):
    if not main.BOT_TOKEN or not init_data:return None
    try:
        values=parse_qs(str(init_data),keep_blank_values=True); rh=values.get("hash",[""])[0]; rs=values.get("signature",[""])[0]; ad=int(values.get("auth_date",["0"])[0]); user_raw=values.get("user",[""])[0]
        if not ad or not user_raw:return None
        age=int(time.time())-ad
        if age < -300 or age > 86400:return None
        check=_check_string(values); secret=hmac.new(b"WebAppData",main.BOT_TOKEN.encode(),hashlib.sha256).digest(); calc=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest(); valid=bool(rh) and hmac.compare_digest(calc,rh)
        if not valid and rs:
            try:
                bot_id=main.BOT_TOKEN.split(":",1)[0]; sigcheck=f"{bot_id}:WebAppData\n"+_check_string(values,True); sig=base64.urlsafe_b64decode(rs+"="*(-len(rs)%4)); Ed25519PublicKey.from_public_bytes(_PROD_TELEGRAM_WEBAPP_PUBLIC_KEY).verify(sig,sigcheck.encode()); valid=True
            except Exception: pass
        if not valid:return None
        u=json.loads(user_raw); uid=str(u.get("id",""));
        if not uid:return None
        return {"id":uid,"username":u.get("username"),"first_name":u.get("first_name"),"last_name":u.get("last_name"),"language_code":u.get("language_code")}
    except Exception:return None
main.telegram_webapp_validate=robust_telegram_webapp_validate
_original_app_user_payload=main.app_user_payload
def safe_app_user_payload(uid):
    try:return _original_app_user_payload(uid)
    except Exception:return {"id":str(uid),"name":"Student","username":None,"submissions":0,"obtained":0,"max":0,"average_percentage":0}
main.app_user_payload=safe_app_user_payload
main.MODELS=["gemini-3.6-flash","gemini-3.5-flash-lite"]
def fast_image_pages_from_pdf(pdf):
    return [page.get_pixmap(dpi=96,alpha=False).tobytes("jpeg",jpg_quality=84) for page in pdf]
main.image_pages_from_pdf=fast_image_pages_from_pdf
def fast_call_gemini(images,paper,evaluation_type="GENERAL",source_id=None,exam="UPPCS"):
    parts=[{"inline_data":{"mime_type":"image/jpeg","data":base64.b64encode(image).decode()}} for image in images]
    reference=main.get_daily_model_answer_reference(paper,source_id=source_id,exam=exam) if str(evaluation_type).upper()=="DAILY" else main.get_content_reference(evaluation_type,source_id=source_id,paper=paper,exam=exam)
    parts.append({"text":main.build_prompt(paper,len(images),reference,evaluation_type=evaluation_type,exam=exam)})
    payload={"contents":[{"parts":parts}],"generationConfig":{"response_mime_type":"application/json","thinkingConfig":{"thinkingLevel":"low"},"maxOutputTokens":24000}}
    last=""
    for model in main.MODELS:
        try:
            r=requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={main.GEMINI_API_KEY}",json=payload,timeout=(15,75))
            if r.status_code==200:
                raw=r.json()["candidates"][0]["content"]["parts"][0]["text"]
                try:d=json.loads(raw)
                except Exception:d=json.loads(raw.replace("```json","").replace("```","").strip())
                return main.normalize_result(d,len(images))
            last=f"{model}: HTTP {r.status_code} {r.text[:300]}"
        except Exception as e:last=f"{model}: {e}"
    raise Exception("Gemini evaluation failed: "+last)
main.call_gemini=fast_call_gemini; main.EVALUATION_STALE_SECONDS=600
_original_run_webapp_evaluation=main.run_webapp_evaluation
def threaded_run_webapp_evaluation(*args,**kwargs):
    threading.Thread(target=_original_run_webapp_evaluation,args=args,kwargs=kwargs,daemon=True,name="prana-evaluation-worker").start()
main.run_webapp_evaluation=threaded_run_webapp_evaluation
@main.app.get("/api/health")
def health_check():return {"ok":True,"runtime":"google-cloud-run","database_configured":bool(main.DB_ENABLED and main.SessionLocal is not None),"telegram_bot_configured":bool(main.bot),"gemini_configured":bool(main.GEMINI_API_KEY),"mini_app":True,"models":list(main.MODELS)}
if __name__=="__main__":uvicorn.run(main.app,host="0.0.0.0",port=int(os.getenv("PORT","10000")))
