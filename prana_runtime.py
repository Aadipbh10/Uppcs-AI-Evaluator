# Runtime-only additive fixes; original main.py stays untouched.
import io
from datetime import date
import pymupdf as fitz
from fastapi import Request
from fastapi.responses import Response,HTMLResponse

def install(main):
    app=main['app']; engine=main.get('engine')
    if not engine:return
    def daily_rows(lang,stamp=''):
        q='''SELECT cs.paper,cs.language,cs.content_date,ci.question_number,ci.question,ci.model_answer,cs.rubric FROM content_sets cs JOIN content_items ci ON ci.set_id=cs.id WHERE cs.content_type='daily' AND cs.is_active=TRUE AND cs.language=%s AND (%s='' OR CAST(cs.content_date AS TEXT)=%s) ORDER BY cs.content_date DESC,cs.paper,ci.question_number'''
        with engine.connect() as c:return c.exec_driver_sql(q,(lang,stamp,stamp)).mappings().all()
    def daily_pdf(rows,lang):
        doc=fitz.open(); stamp=str(rows[0].get('content_date') or date.today())
        for i,r in enumerate(rows,1):
            p=doc.new_page(width=595,height=842); p.insert_text((45,45),'PRANA PCS Mains AI',fontsize=18,fontname='hebo'); p.insert_text((45,65),f'Daily Questions • {lang} • {stamp}',fontsize=9,color=(.35,.35,.35)); y=100
            for label,text,size in [('QUESTION',r.get('question',''),11),('MODEL ANSWER',r.get('model_answer',''),10)]:
                p.insert_text((45,y),label,fontsize=8,fontname='hebo',color=(.48,.34,0)); y+=18; p.insert_textbox(fitz.Rect(45,y,550,min(780,y+250)),str(text or ''),fontsize=size,lineheight=1.35); y+=290
            if i==len(rows) and r.get('rubric'):p.insert_textbox(fitz.Rect(45,760,550,820),'RUBRIC\n'+str(r['rubric']),fontsize=8)
            p.insert_text((45,825),'Telegram • Instagram • YouTube • WhatsApp',fontsize=7,color=(.35,.35,.35))
        out=io.BytesIO();doc.save(out,garbage=4,deflate=True);doc.close();out.seek(0);return out.getvalue(),stamp
    for r in app.router.routes:
        if getattr(r,'path',None)=='/api/admin/dq/pdf' and 'GET' in getattr(r,'methods',set()):
            def admin_dq(request:Request,content_date='',language='Hindi'):
                if not main['admin_authorized'](request):return main['admin_denied']()
                lang='English' if str(language).lower().startswith('en') else 'Hindi'; rows=daily_rows(lang,content_date)
                if not rows:return main['app_error']('No Daily Questions available.',404)
                data,stamp=daily_pdf(rows,lang); resp=Response(content=data,media_type='application/pdf');resp.headers['Content-Disposition']=f'attachment; filename="DQ_{stamp}_{lang}.pdf"';return resp
            r.endpoint=admin_dq
        if getattr(r,'path',None)=='/api/app/daily/send-pdf' and 'POST' in getattr(r,'methods',set()):
            def app_dq(request:Request,content_date='',language='Hindi'):
                uid=main['require_app_user'](request)
                if not uid:return main['app_error']('Unauthorized',401)
                lang='English' if str(language).lower().startswith('en') else 'Hindi'; rows=daily_rows(lang,content_date)
                if not rows:return main['app_error']('No Daily Questions available.',404)
                data,stamp=daily_pdf(rows,lang);bio=io.BytesIO(data);bio.name=f'DQ_{stamp}_{lang}.pdf';main['bot'].send_document(str(uid),bio,caption=f'📚 DQ {stamp} • {lang}');return {'ok':True,'filename':bio.name,'message':'Daily Questions PDF sent to Telegram chat.'}
            r.endpoint=app_dq
    for r in app.router.routes:
        if getattr(r,'path',None) in ('/app','/miniapp') and 'GET' in getattr(r,'methods',set()):
            original=r.endpoint
            def mini(_original=original):
                resp=_original(); body=getattr(resp,'body',b'')
                if not body:return resp
                text=body.decode('utf-8'); patch="""<script>window.__PRANA_ACCESS_PATCH=1;</script>"""
                return HTMLResponse(text.replace('</body>',patch+'</body>'))
            r.endpoint=mini
