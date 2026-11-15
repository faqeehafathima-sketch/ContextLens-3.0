from __future__ import annotations
import base64, json, os, re, shutil, subprocess, sys, tempfile, threading, time, uuid, zipfile
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from contextlens_app import ContextLensEngine, REQUIRED_FILES

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CONTEXTLENS_DATA_DIR', ROOT / 'data')).resolve()
REPOS = DATA / 'repositories'
WORKSPACE = DATA / 'workspace'
DATA.mkdir(parents=True, exist_ok=True); REPOS.mkdir(parents=True, exist_ok=True)
engine = ContextLensEngine()
active_repo_root: Path | None = None
lock = threading.RLock()
status: dict[str, Any] = {'state':'idle','message':'Ready to analyze a repository.','log':[],'repository_url':None,'started_at':None,'finished_at':None}

app = FastAPI(title='ContextLens 3.0', version='3.0.0', docs_url='/api/docs')
app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')

class ImportRequest(BaseModel):
    filename: str = 'unvibecode-context.zip'
    content_base64: str

class AnalyzeRequest(BaseModel):
    repository_url: str = Field(min_length=10, max_length=500)

class SearchRequest(BaseModel):
    query: str = ''
    language: str = ''
    kind: str = ''
    limit: int = Field(default=100, ge=1, le=500)

class UnresolvedRequest(BaseModel):
    query: str = ''
    reason: str = ''
    resolution_status: str = ''
    limit: int = Field(default=100, ge=1, le=500)

class ContextRequest(BaseModel):
    file: str
    scope: str = '0'
    format: str = 'markdown'

class InvestigationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    observation: str = ''
    source_evidence: str = ''
    assessment: str = ''
    repository_url: str = ''

def _append(line: str):
    status['log'].append(str(line)[-1200:])
    status['log'] = status['log'][-150:]

def _find_bundle(root: Path) -> Path | None:
    preferred = ['complete_repository_context_for_llm.zip','repository_context_for_llm.zip','repository-context.zip','flask-repository-context.zip']
    candidates=[]
    for folder in ['shipready_results','unvibecode_results']:
        base=root/folder
        if base.exists():
            for p in base.rglob('*.zip'):
                if 'repository' in p.name.lower() and 'context' in p.name.lower():
                    try: candidates.append((p.stat().st_mtime, preferred.index(p.name.lower()) if p.name.lower() in preferred else 99, p))
                    except OSError: pass
    if not candidates: return None
    candidates.sort(key=lambda x:(x[1],-x[0]))
    return candidates[0][2]

def _load_existing_workspace():
    if all((WORKSPACE/n).is_file() for n in REQUIRED_FILES):
        try: engine.load_workspace(WORKSPACE)
        except Exception: pass
_load_existing_workspace()

@app.get('/', response_class=HTMLResponse)
def home():
    return (ROOT/'templates'/'index.html').read_text(encoding='utf-8')

@app.get('/api/health')
def health():
    return {'ok':True,'app':'ContextLens 3.0','version':'3.0.0','unvibecode_available':_unvibecode_available(),'loaded':bool(engine.data_dir)}

def _unvibecode_available():
    try:
        p=subprocess.run([sys.executable,'-m','unvibecode','--help'],capture_output=True,text=True,timeout=15)
        return p.returncode == 0
    except Exception: return False

@app.get('/api/status')
def get_status():
    return {**status,'loaded':bool(engine.data_dir),'stats':engine.dashboard_stats() if engine.data_dir else {}}

@app.post('/api/import')
def import_bundle(req: ImportRequest):
    if not req.filename.lower().endswith('.zip'): raise HTTPException(400,'Upload a ZIP export from UnvibeCode.')
    try:
        raw=base64.b64decode(req.content_base64,validate=True)
        if len(raw)>100*1024*1024: raise HTTPException(413,'ZIP exceeds the 100 MB upload limit.')
        DATA.mkdir(parents=True,exist_ok=True)
        temp=DATA/f'upload-{uuid.uuid4().hex}.zip'; temp.write_bytes(raw)
        try:
            with lock: stats=engine.load_zip(temp,workspace=DATA)
        finally: temp.unlink(missing_ok=True)
        status.update(state='complete',message='Repository context imported and validated.',finished_at=time.time())
        _append(f'Imported context ZIP: {req.filename}')
        return {'ok':True,'message':'Repository context imported and validated.','stats':stats}
    except HTTPException: raise
    except Exception as e: raise HTTPException(400,str(e))

@app.post('/api/analyze')
def analyze(req: AnalyzeRequest):
    global status, active_repo_root
    url=req.repository_url.strip()
    # Only public GitHub HTTPS repositories are accepted; no local paths or arbitrary hosts.
    m=re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?',url,flags=re.I)
    if not m: raise HTTPException(400,'Enter a public GitHub repository URL, e.g. https://github.com/pallets/flask')
    owner,repo=m.group(1),m.group(2)
    if repo.lower() in {'.','..'}: raise HTTPException(400,'Invalid repository name.')
    if not _unvibecode_available():
        raise HTTPException(503,'UnvibeCode is not installed in this Python environment. Install it with: python -m pip install --upgrade unvibecode')
    target=REPOS/f'{owner}__{repo}'
    # Reserve the single analysis slot atomically. Without this critical section,
    # two near-simultaneous requests could both pass the state check and delete
    # or clone into the same target directory.
    with lock:
        if status.get('state')=='running':
            raise HTTPException(409,'An analysis is already running.')
        active_repo_root=target
        status={'state':'running','message':f'Cloning {owner}/{repo}…','log':[],'repository_url':url,'started_at':time.time(),'finished_at':None}
    try:
        if target.exists():
            if target.is_symlink(): target.unlink()
            else: shutil.rmtree(target)
        clone=subprocess.run(['git','clone','--depth','1','--filter=blob:none',url,str(target)],capture_output=True,text=True,timeout=180)
        if clone.returncode: raise RuntimeError((clone.stderr or clone.stdout)[-2500:] or 'Git clone failed.')
        _append('Repository cloned successfully.')
        status['message']='Running UnvibeCode review…'
        run=subprocess.Popen([sys.executable,'-m','unvibecode','review','--repository',str(target),'--no-open-browser'],cwd=target,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
        def collect_output():
            if run.stdout is not None:
                for line in run.stdout:
                    _append(line.rstrip())
                    status['message']='UnvibeCode is analyzing the repository…'
        reader=threading.Thread(target=collect_output,daemon=True)
        reader.start()
        try:
            code=run.wait(timeout=900)
        except subprocess.TimeoutExpired:
            run.kill(); run.wait(); reader.join(timeout=3)
            raise RuntimeError('UnvibeCode review exceeded the 15-minute safety limit.')
        reader.join(timeout=5)
        if code: raise RuntimeError(f'UnvibeCode exited with code {code}. See run log for details.')
        bundle=_find_bundle(target)
        if not bundle: raise RuntimeError('UnvibeCode completed, but no repository-context ZIP was found in shipready_results or unvibecode_results.')
        with lock: stats=engine.load_zip(bundle,workspace=DATA)
        status.update(state='complete',message='Analysis complete. ContextLens has loaded the generated repository context.',finished_at=time.time())
        _append(f'Imported generated context bundle: {bundle.name}')
        return {'ok':True,'message':status['message'],'stats':stats,'bundle':bundle.name}
    except Exception as e:
        status.update(state='error',message=str(e),finished_at=time.time()); _append(f'ERROR: {e}')
        raise HTTPException(500,str(e))

@app.post('/api/search')
def search(req: SearchRequest):
    if not engine.data_dir: raise HTTPException(409,'Load a repository context first.')
    return {'results':engine.search(req.query,language=req.language,kind=req.kind,limit=req.limit)}

@app.post('/api/unresolved')
def unresolved(req: UnresolvedRequest):
    if not engine.data_dir: raise HTTPException(409,'Load a repository context first.')
    return {'results':engine.unresolved_search(req.query,req.reason,req.resolution_status,req.limit)}

@app.post('/api/context')
async def context(request: Request):
    # Accept aliases from older cached frontends instead of exposing a raw 422.
    if not engine.data_dir:
        raise HTTPException(409, 'Load a repository context first.')
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, 'The context request body must be valid JSON.')
    if not isinstance(payload, dict):
        raise HTTPException(400, 'The context request must be a JSON object.')

    file_value = (payload.get('file') or payload.get('file_path') or
                  payload.get('target_file') or payload.get('path'))
    if isinstance(file_value, dict):
        file_value = (file_value.get('path') or file_value.get('file') or
                      file_value.get('file_path') or file_value.get('value'))
    if not isinstance(file_value, str) or not file_value.strip():
        raise HTTPException(400, 'No target file was received. Select a file and try again.')

    scope_value = payload.get('scope', payload.get('depth', '0'))
    if isinstance(scope_value, dict):
        scope_value = scope_value.get('value', '0')
    scope = str(scope_value if scope_value is not None else '0')
    fmt = str(payload.get('format', payload.get('output_format', 'markdown'))).lower()
    if fmt not in {'markdown', 'json'}:
        fmt = 'markdown'
    try:
        raw = engine.export_context(file_value.strip(), scope, fmt)
        return {'content': raw, 'format': fmt}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get('/api/files')
def files():
    if not engine.data_dir: return {'files':[]}
    return {'files':sorted(engine.selections.keys())}

@app.get('/api/graph')
def graph(file: str | None = None, limit: int = 120):
    if not engine.data_dir: raise HTTPException(409,'Load a repository context first.')
    return engine.chunk_graph(file_path=file,max_nodes=max(10,min(limit,250)))

@app.get('/api/chunk/{chunk_id}')
def chunk(chunk_id: str):
    if not engine.data_dir: raise HTTPException(409,'Load a repository context first.')
    item=engine.chunk_details(chunk_id)
    if not item: raise HTTPException(404,'Chunk not found.')
    return item

@app.get('/api/investigations')
def list_investigations():
    if not engine.data_dir: return {'investigations': []}
    return {'investigations': [{'title':name, **(record.get('payload') or {})} for name,record in engine.sessions.items()]}

@app.post('/api/investigations')
def save_investigation(req: InvestigationRequest):
    if not engine.data_dir: raise HTTPException(409,'Load a repository context before saving an investigation.')
    payload={'observation':req.observation,'source_evidence':req.source_evidence,'assessment':req.assessment,'repository_url':req.repository_url or status.get('repository_url') or ''}
    try:
        with lock: engine.save_session(req.title,payload)
        return {'ok':True,'message':'Investigation saved to this repository workspace.','investigation':{'title':req.title,**payload}}
    except Exception as e: raise HTTPException(400,str(e))


@app.get('/api/reports')
def reports():
    if active_repo_root is None or not active_repo_root.exists(): return {'reports': []}
    roots=[active_repo_root/'shipready_results',active_repo_root/'unvibecode_results']
    found=[]
    priority=[('workflow','Business Workflow Map'),('connected','Connected Code Map'),('risk','Business Risk Findings'),('full','Complete Repository Report')]
    for base in roots:
        if not base.exists(): continue
        for p in base.rglob('*.html'):
            try:
                if not p.is_file() or p.stat().st_size<=0: continue
                name=p.name.lower()
                category='other'; label=p.stem.replace('_',' ').replace('-',' ').title()
                for token,title in priority:
                    if token in name:
                        category=token; label=title; break
                if 'business' in name and 'workflow' in name: category='workflow'; label='Business Workflow Map'
                if 'business' in name and 'risk' in name: category='risk'; label='Business Risk Findings'
                if 'connected' in name and 'code' in name: category='connected'; label='Connected Code Map'
                rel=p.relative_to(active_repo_root).as_posix()
                found.append({'id':rel,'name':p.name,'label':label,'category':category,'size_bytes':p.stat().st_size,'modified':p.stat().st_mtime})
            except OSError: continue
    found.sort(key=lambda x:(['workflow','connected','risk','full','other'].index(x['category']),-x['modified']))
    return {'reports':found[:40]}

@app.get('/api/report')
def report(name: str):
    if active_repo_root is None: raise HTTPException(404,'No repository report is available in this session.')
    candidate=(active_repo_root/name).resolve()
    if not _within(candidate,active_repo_root.resolve()) or not candidate.is_file() or candidate.suffix.lower()!='.html':
        raise HTTPException(404,'Report not found.')
    return HTMLResponse(candidate.read_text(encoding='utf-8',errors='replace'))

def _within(path: Path, root: Path) -> bool:
    try: path.relative_to(root); return True
    except ValueError: return False

@app.get('/api/manifest')
def manifest():
    if not engine.data_dir: raise HTTPException(409,'Load a repository context first.')
    return engine.manifest

if __name__=='__main__':
    import uvicorn
    uvicorn.run('server:app',host=os.environ.get('HOST','127.0.0.1'),port=int(os.environ.get('PORT','7863')),reload=False)

@app.get('/api/sample')
def sample():
    return FileResponse(ROOT/'sample_evidently_context.zip',filename='evidently-repository-context.zip',media_type='application/zip')
