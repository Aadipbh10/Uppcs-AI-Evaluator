# Additive compatibility layer. main.py remains the master backend.
import io,re,uuid
from datetime import datetime,date
import pymupdf as fitz
from fastapi import Request
from fastapi.responses import Response

def parse_qa(text):
    text=str(text or '').replace('\r\n','\n').replace('\r','\n')
    text=re.sub(r'(?im)^\s*(?:प्रश्न|question)\s*[-.:]?\s*(\d+)\s*[).:-]?\s*',r'Q\1. ',text)
    text=re.sub(r'(?im)^\s*(?:उत्तर|answer)\s*[-.:]?\s*(\d+)\s*[).:-]?\s*',r'ANS\1. ',text)
    lines=text.splitlines(); pat=re.compile(r'(?im)^\s*(Q|ANS)\s*(\d+)\s*[.):-]\s*(.*?)\s*$'); found=[]
    for i,line in enumerate(lines):
        m=pat.match(line)
        if m: found.append((i,m.group(1).upper(),int(m.group(2)),m.group(3).strip()))
    out={}
    for j,(i,k,n,first) in enumerate(found):
        end=found[j+1][0] if j+1<len(found) else len(lines); body='\n'.join([first]+lines[i+1:end]).strip(); out.setdefault(n,{})['question' if k=='Q' else 'model_answer']=body
    return [{'question_number':n,'question':v['question'],'model_answer':v.get('model_answer','')} for n,v in sorted(out.items()) if v.get('question')]

def install(main):
    app=main['app']; engine=main.get('engine'); SessionLocal=main.get('SessionLocal')
    if not engine or not SessionLocal:return
    oldnorm=main['normalize_result']
    def normalize(data,pages):
        r=oldnorm(data,pages); src={int(q.get('question_number',i+1)):q for i,q in enumerate(data.get('questions',[]) or []) if isinstance(q,dict)}
        for q in r.get('questions',[]):
            s=src.get(int(q.get('question_number',0)),{}); q['intro_comment']=str(s.get('intro_comment','')).strip(); q['body_comment']=str(s.get('body_comment','')).strip(); q['conclusion_comment']=str(s.get('conclusion_comment','')).strip()
        return r
    main['normalize_result']=normalize; main['parse_qa_pairs']=parse_qa
    @app.get('/api/health/db')
    def db_health():
        try:
            with engine.connect() as c:c.exec_driver_sql('SELECT 1')
            return {'ok':True}
        except Exception as e:return {'ok':False,'error':str(e)[:300]}
    @app.get('/api/admin/content-sets')
    def content_sets(request:Request):
        if not main['admin_authorized'](request):return main['admin_denied']()
        main['ensure_new_schema']()
        with engine.connect() as c: rows=c.exec_driver_sql('SELECT id,content_type,exam,paper,language,content_date,year,title,rubric FROM content_sets WHERE is_active=TRUE ORDER BY created_at DESC').mappings().all()
        return {'ok':True,'items':[dict(x) for x in rows]}
    @app.post('/api/admin/daily/upload')
    async def daily_upload(request:Request):
        if not main['admin_authorized'](request):return main['admin_denied']()
        b=await request.json(); items=parse_qa(b.get('qa_text',''))
        if not items:return {'ok':False,'error':'Q1./ANS1. format में valid questions नहीं मिले।'}
        lang='English' if str(b.get('language','Hindi')).lower().startswith('en') else 'Hindi'; paper=str(b.get('paper','GS1')).upper(); d=b.get('date') or str(date.today()); sid=str(uuid.uuid4()); now=main['_utcnow']()
        with engine.begin() as c:
            c.exec_driver_sql('INSERT INTO content_sets(id,content_type,exam,paper,language,content_date,title,rubric,is_active,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)',(sid,'daily','UPPCS',paper,lang,d,f'Daily {d}','',now,now))
            for q in items:c.exec_driver_sql('INSERT INTO content_items(set_id,question_number,question,model_answer,created_at) VALUES (%s,%s,%s,%s,%s)',(sid,q['question_number'],q['question'],q['model_answer'],now))
        return {'ok':True,'set_id':sid,'count':len(items),'language':lang,'date':d}
    @app.post('/api/admin/pyq/upload')
    async def pyq_upload(request:Request):
        if not main['admin_authorized'](request):return main['admin_denied']()
        b=await request.json(); items=parse_qa(b.get('qa_text',''))
        if not items:return {'ok':False,'error':'Q1./ANS1. format में valid PYQ नहीं मिले।'}
        if len(items)>20:return {'ok':False,'error':'PYQ paper maximum 20 questions तक supported है।'}
        exam=str(b.get('exam','UPPCS')).upper(); paper=str(b.get('paper','GS1')).upper(); lang='English' if str(b.get('language','Hindi')).lower().startswith('en') else 'Hindi'; year=int(b.get('year') or 0) or None; sid=str(uuid.uuid4()); now=main['_utcnow']()
        with engine.begin() as c:
            c.exec_driver_sql('INSERT INTO content_sets(id,content_type,exam,paper,language,year,title,rubric,is_active,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)',(sid,'pyq',exam,paper,lang,year,f'{exam} {year or ""} {paper}','',now,now))
            for q in items:c.exec_driver_sql('INSERT INTO content_items(set_id,question_number,question,model_answer,created_at) VALUES (%s,%s,%s,%s,%s)',(sid,q['question_number'],q['question'],q['model_answer'],now))
        return {'ok':True,'set_id':sid,'count':len(items)}
    @app.post('/api/admin/content-rubric')
    async def content_rubric(request:Request):
        if not main['admin_authorized'](request):return main['admin_denied']()
        b=await request.json(); sid=str(b.get('set_id','')).strip(); rubric=str(b.get('rubric','')).strip()
        if not sid or not rubric:return {'ok':False,'error':'Set ID और rubric required हैं।'}
        with engine.begin() as c:
            r=c.exec_driver_sql('UPDATE content_sets SET rubric=%s,updated_at=%s WHERE id=%s AND is_active=TRUE',(rubric,main['_utcnow'](),sid))
            if r.rowcount==0:return {'ok':False,'error':'Content set नहीं मिला।'}
        return {'ok':True}
