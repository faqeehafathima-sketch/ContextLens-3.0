"""ContextLens 3.0 — repository context intelligence for UnvibeCode exports.
Run: python contextlens_app.py [--data-dir PATH] [--import-zip PATH] [--workspace-root PATH] [--port 7863]
The core engine uses the Python standard library. Gradio/Plotly are optional UI dependencies.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, stat, tempfile, zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

REQUIRED_FILES = ("chunks.jsonl", "connections.jsonl", "selections.jsonl", "manifest.json", "unresolved_relationships.jsonl", "README.md")
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_MEMBER_BYTES = 150 * 1024 * 1024

class ContextLensError(Exception): pass
class SecurityError(ContextLensError): pass
class DataValidationError(ContextLensError): pass

def _is_within(path: Path, root: Path) -> bool:
    try: path.resolve().relative_to(root.resolve()); return True
    except ValueError: return False

def safe_extract_zip(zip_path: str | Path, destination: str | Path, *, max_total: int = MAX_ARCHIVE_BYTES) -> list[str]:
    """Extract a ZIP only after validating every member; reject traversal, symlinks and bombs."""
    zip_path, root = Path(zip_path), Path(destination)
    root.mkdir(parents=True, exist_ok=True); root = root.resolve()
    extracted, total = [], 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            p = PurePosixPath(name)
            mode = info.external_attr >> 16
            if not name or p.is_absolute() or any(part in ('..', '') for part in p.parts) or re.match(r'^[A-Za-z]:', name):
                raise SecurityError(f"Unsafe archive path: {info.filename}")
            if stat.S_ISLNK(mode): raise SecurityError(f"Symlink member rejected: {name}")
            if info.file_size > MAX_MEMBER_BYTES: raise SecurityError(f"Archive member too large: {name}")
            total += info.file_size
            if total > max_total: raise SecurityError("Archive exceeds total uncompressed size limit")
            target = (root / Path(*p.parts)).resolve()
            if not _is_within(target, root): raise SecurityError(f"Archive path escapes destination: {name}")
        # Validation completed before writing any member.
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            if info.is_dir(): (root / Path(*PurePosixPath(name).parts)).mkdir(parents=True, exist_ok=True); continue
            target = root / Path(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open('wb') as dst: shutil.copyfileobj(src, dst)
            extracted.append(name)
    return extracted

def read_jsonl(path: Path, key_field: str | None = None) -> tuple[dict, list, list[str]]:
    indexed, records, errors = {}, [], []
    with path.open(encoding='utf-8') as f:
        for line_no, raw in enumerate(f, 1):
            if not raw.strip(): continue
            try: item = json.loads(raw)
            except json.JSONDecodeError as e: errors.append(f"{path.name}:{line_no}: invalid JSON ({e.msg})"); continue
            if not isinstance(item, dict): errors.append(f"{path.name}:{line_no}: record must be a JSON object"); continue
            records.append(item)
            if key_field:
                key = item.get(key_field)
                if key is None: errors.append(f"{path.name}:{line_no}: missing {key_field}")
                elif key in indexed: errors.append(f"{path.name}:{line_no}: duplicate {key_field}={key!r}; first record retained")
                else: indexed[key] = item
    return indexed, records, errors

@dataclass
class Audit:
    selected_chunks: int
    resolved_chunks: int
    missing_chunk_ids: list[str]
    estimated_tokens: int | None
    declared_tokens: int | None
    maximum_tokens: int | None
    within_budget: bool | None
    file_coverage: float | None

class ContextLensEngine:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else None
        self.chunks: dict[str, dict] = {}; self.connections: list[dict] = []
        self.selections: dict[str, dict] = {}; self.manifest: dict = {}; self.unresolved: list[dict] = []
        self.errors: list[str] = []; self.sessions: dict[str, dict] = {}
        if self.data_dir: self.load_workspace(self.data_dir)

    def load_workspace(self, data_dir: str | Path) -> dict:
        root = Path(data_dir)
        missing = [n for n in REQUIRED_FILES if not (root / n).is_file()]
        if missing: raise DataValidationError("Missing required files: " + ", ".join(missing))
        try: manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e: raise DataValidationError(f"manifest.json is invalid: {e}")
        if not isinstance(manifest, dict): raise DataValidationError("manifest.json must contain a JSON object")
        chunks, _, e1 = read_jsonl(root/'chunks.jsonl', 'chunk_id')
        _, connections, e2 = read_jsonl(root/'connections.jsonl')
        selections, _, e3 = read_jsonl(root/'selections.jsonl', 'file')
        _, unresolved, e4 = read_jsonl(root/'unresolved_relationships.jsonl')
        self.data_dir=root; self.chunks=chunks; self.connections=connections; self.selections=selections
        self.manifest=manifest; self.unresolved=unresolved; self.errors=e1+e2+e3+e4
        # Restore saved investigations from this repository workspace on every load.
        self.load_sessions()
        return self.dashboard_stats()

    def load_zip(self, zip_path: str | Path, workspace: str | Path | None = None) -> dict:
        """Load a repository ZIP transactionally; a bad upload never destroys the last workspace."""
        base = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix='contextlens_'))
        base.mkdir(parents=True, exist_ok=True)
        extracted_dir = base / 'workspace'
        previous_sessions = {}
        previous_store = extracted_dir / 'contextlens_sessions.json'
        if previous_store.is_file():
            try:
                loaded_sessions = json.loads(previous_store.read_text(encoding='utf-8'))
                if isinstance(loaded_sessions, dict):
                    previous_sessions = loaded_sessions
            except (json.JSONDecodeError, UnicodeDecodeError):
                previous_sessions = {}

        staging = Path(tempfile.mkdtemp(prefix='.contextlens-staging-', dir=base))
        backup = base / '.contextlens-workspace-backup'
        try:
            safe_extract_zip(zip_path, staging)
            candidates = [staging] + [p for p in staging.iterdir() if p.is_dir()]
            chosen = next((p for p in candidates if all((p / n).is_file() for n in REQUIRED_FILES)), None)
            if chosen is None:
                raise DataValidationError('ZIP must contain all six UnvibeCode files: ' + ', '.join(REQUIRED_FILES))
            if previous_sessions:
                (chosen / 'contextlens_sessions.json').write_text(
                    json.dumps(previous_sessions, ensure_ascii=False, indent=2), encoding='utf-8'
                )

            # Validate all required data before replacing the current workspace.
            ContextLensEngine(chosen)
            prepared = chosen
            if chosen != staging:
                prepared = staging / '.prepared-workspace'
                shutil.move(str(chosen), str(prepared))

            if backup.exists():
                shutil.rmtree(backup)
            if extracted_dir.exists():
                os.replace(extracted_dir, backup)
            try:
                os.replace(prepared, extracted_dir)
            except Exception:
                if backup.exists() and not extracted_dir.exists():
                    os.replace(backup, extracted_dir)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            self.load_workspace(extracted_dir)
            return self.dashboard_stats()
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def dashboard_stats(self) -> dict:
        files = {c.get('file') for c in self.chunks.values() if c.get('file')}
        return {'files':len(files),'chunks':len(self.chunks),'connections':len(self.connections),
                'unresolved_relationships':len(self.unresolved),'languages':dict(Counter(c.get('language','unknown') for c in self.chunks.values())),
                'relationship_types':dict(Counter(c.get('relationship','unknown') for c in self.connections)),
                'confidence':dict(Counter(str(c.get('confidence','unknown')) for c in self.connections)),
                'loader_warnings':len(self.errors)}

    def search(self, query: str, *, language: str = '', kind: str = '', limit: int = 500) -> list[dict]:
        q=query.casefold().strip(); out=[]
        for c in self.chunks.values():
            if language and str(c.get('language','')).casefold()!=language.casefold(): continue
            if kind and str(c.get('kind','')).casefold()!=kind.casefold(): continue
            hay=' '.join(str(c.get(k,'')) for k in ('file','entity','qualified_name','numbered_code','role_hints')).casefold()
            if not q or q in hay:
                out.append({'chunk_id':c.get('chunk_id'),'file':c.get('file'),'entity':c.get('entity'),
                            'qualified_name':c.get('qualified_name'),'language':c.get('language'),'kind':c.get('kind'),
                            'line_range':c.get('line_range'),'estimated_tokens':c.get('estimated_tokens')})
                if len(out)>=limit: break
        return out

    def chunk_graph(self, file_path: str | None = None, max_nodes: int = 300) -> dict:
        """Return an induced chunk graph; when a file is chosen, include its direct neighbors.

        A bounded graph keeps large repositories responsive while preserving all parallel
        relationship records among the selected nodes.
        """
        all_nodes={cid:{'chunk_id':cid,'file':c.get('file'),'name':c.get('qualified_name') or c.get('entity'),
                    'kind':c.get('kind'),'language':c.get('language'),'line_range':c.get('line_range')} for cid,c in self.chunks.items()}
        missing_targets=0
        if file_path:
            seeds=[cid for cid,node in all_nodes.items() if node.get('file')==file_path]
            selected=list(seeds[:max_nodes]); selected_set=set(selected)
            for e in self.connections:
                src,tgt=e.get('source_chunk'),e.get('target_chunk')
                if src in selected_set and tgt in all_nodes and tgt not in selected_set and len(selected)<max_nodes:
                    selected.append(tgt); selected_set.add(tgt)
                if tgt in selected_set and src in all_nodes and src not in selected_set and len(selected)<max_nodes:
                    selected.append(src); selected_set.add(src)
        else:
            selected=list(all_nodes)[:max_nodes]; selected_set=set(selected)
        nodes={cid:all_nodes[cid] for cid in selected}
        edges=[]
        for i,e in enumerate(self.connections):
            src,tgt=e.get('source_chunk'),e.get('target_chunk')
            if src in selected_set and tgt in selected_set:
                edges.append({'id':i,'source':src,'target':tgt,'relationship':e.get('relationship'),
                              'confidence':e.get('confidence'),'source_file':e.get('source_file'),'target_file':e.get('target_file')})
            elif src not in all_nodes or tgt not in all_nodes: missing_targets+=1
        return {'nodes':nodes,'edges':edges,'unresolved_edge_records':missing_targets,
                'total_nodes':len(all_nodes),'total_edges':len(self.connections),
                'truncated':len(nodes)<len(all_nodes)}

    def chunk_details(self, chunk_id: str) -> dict | None:
        c=self.chunks.get(chunk_id)
        if not c: return None
        related=[e for e in self.connections if e.get('source_chunk')==chunk_id or e.get('target_chunk')==chunk_id]
        return {'chunk':c,'relationships':related}

    def build_context(self, file_path: str, scope: str | int) -> tuple[dict, str | None]:
        selection=self.selections.get(file_path)
        if not selection: return {}, 'File not found in selections.'
        scope_data=selection.get('scopes',{}).get(str(scope))
        if not isinstance(scope_data,dict): return {}, f'Scope {scope} is not available for {file_path}.'
        ids=scope_data.get('chunk_ids',[]); resolved=[self.chunks[i] for i in ids if i in self.chunks]
        missing=[i for i in ids if i not in self.chunks]
        return {'file':file_path,'scope':str(scope),'selection':scope_data,'resolved':resolved,'missing':missing}, None

    def audit_context(self, file_path: str, scope: str | int) -> Audit | None:
        ctx,err=self.build_context(file_path,scope)
        if err: return None
        sel=ctx['selection']; token_values=[c.get('estimated_tokens') for c in ctx['resolved']]
        estimated=sum(v for v in token_values if isinstance(v,(int,float))) if token_values and all(isinstance(v,(int,float)) for v in token_values) else None
        declared=sel.get('estimated_tokens'); maximum=sel.get('maximum_tokens')
        included=set(sel.get('included_files',[])); connected=set(sel.get('connected_files_not_included',[]))
        coverage=(len(included)/(len(included)+len(connected))) if included or connected else None
        budget=(declared <= maximum) if isinstance(declared,(int,float)) and isinstance(maximum,(int,float)) else None
        return Audit(len(sel.get('chunk_ids',[])),len(ctx['resolved']),ctx['missing'],estimated,
                     declared if isinstance(declared,int) else None,maximum if isinstance(maximum,int) else None,budget,coverage)

    def unresolved_search(self, query: str = '', reason: str = '', status: str = '', limit: int = 1000) -> list[dict]:
        q=query.casefold().strip(); out=[]
        for r in self.unresolved:
            if reason and str(r.get('reason','')).casefold()!=reason.casefold(): continue
            if status and str(r.get('resolution_status','')).casefold()!=status.casefold(): continue
            if q and q not in json.dumps(r,ensure_ascii=False).casefold(): continue
            out.append(r)
            if len(out)>=limit: break
        return out

    def export_context(self, file_path: str, scope: str | int, fmt: str = 'markdown') -> str:
        ctx,err=self.build_context(file_path,scope)
        if err: raise DataValidationError(err)
        audit=self.audit_context(file_path,scope)
        payload={'file':file_path,'scope':str(scope),'selection':ctx['selection'],'chunks':ctx['resolved'],
                 'missing_chunk_ids':ctx['missing'],'audit':asdict(audit) if audit else None}
        if fmt=='json': return json.dumps(payload,ensure_ascii=False,indent=2)
        lines=[f"# AI Context Export: {file_path} — Scope {scope}",'','## Instructions',
               '- Treat supplied source as evidence; do not invent missing implementations.',
               '- Preserve file paths, line ranges, and symbol names.',
               '- Missing chunk IDs are listed explicitly and must not be inferred.','',
               '## Context audit',f"- Selected chunks: {audit.selected_chunks}",f"- Resolved chunks: {audit.resolved_chunks}",
               f"- Missing IDs: {len(audit.missing_chunk_ids)}",f"- Estimated tokens (sum of chunk metadata): {audit.estimated_tokens}",
               f"- Selection declared tokens: {audit.declared_tokens}",f"- Maximum tokens: {audit.maximum_tokens}",'','## Source code']
        for c in ctx['resolved']:
            lines += ['',f"### {c.get('file','unknown')} — {c.get('qualified_name') or c.get('entity') or c.get('chunk_id')}",
                      f"Lines: {c.get('line_range','unknown')} | Chunk: {c.get('chunk_id')}",
                      f"```{c.get('language','')}",str(c.get('numbered_code','')),'```']
        if ctx['missing']: lines += ['', '## Missing chunk IDs', *[f'- `{x}`' for x in ctx['missing']]]
        return '\n'.join(lines)

    def save_session(self, name: str, payload: dict, path: str | Path | None = None) -> None:
        name = str(name).strip()
        if not name: raise ValueError('Session name cannot be empty')
        store = Path(path) if path else (
            self.data_dir / 'contextlens_sessions.json' if self.data_dir
            else Path.cwd() / 'contextlens_sessions.json'
        )
        store.parent.mkdir(parents=True, exist_ok=True)
        current = self.load_sessions(store)
        current[name] = {'payload': payload}
        temporary = store.with_suffix(store.suffix + '.tmp')
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, store)
        self.sessions = current

    def load_sessions(self, path: str | Path | None = None) -> dict:
        store = Path(path) if path else (
            self.data_dir / 'contextlens_sessions.json' if self.data_dir
            else Path.cwd() / 'contextlens_sessions.json'
        )
        if not store.exists():
            self.sessions = {}
            return self.sessions
        try:
            loaded = json.loads(store.read_text(encoding='utf-8'))
            self.sessions = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.sessions = {}
        return self.sessions


def graph_figure(engine: ContextLensEngine, file_path: str | None = None, max_nodes: int = 300):
    """Render a bounded graph with two Plotly traces, not thousands of edge traces."""
    import plotly.graph_objects as go
    graph=engine.chunk_graph(file_path=file_path, max_nodes=max_nodes)
    nodes=list(graph['nodes'].values()); edges=graph['edges']
    fig=go.Figure()
    if not nodes:
        fig.update_layout(title='Choose a file with indexed chunks to inspect its graph'); return fig
    import math
    n=len(nodes); pos={node['chunk_id']:(math.cos(2*math.pi*i/n),math.sin(2*math.pi*i/n)) for i,node in enumerate(nodes)}
    edge_x=[]; edge_y=[]; edge_hover=[]
    for edge in edges:
        x1,y1=pos[edge['source']]; x2,y2=pos[edge['target']]
        edge_x.extend([x1,x2,None]); edge_y.extend([y1,y2,None])
        edge_hover.extend([f"{edge.get('relationship')} ({edge.get('confidence')})"]*2+[None])
    if edges:
        fig.add_trace(go.Scatter(x=edge_x,y=edge_y,mode='lines',line={'width':1},
            hoverinfo='text',text=edge_hover,showlegend=False,name='Relationships'))
    fig.add_trace(go.Scatter(x=[pos[x['chunk_id']][0] for x in nodes],y=[pos[x['chunk_id']][1] for x in nodes],
        mode='markers+text',text=[(x.get('name') or x['chunk_id'])[:28] for x in nodes],textposition='top center',
        customdata=[x['chunk_id'] for x in nodes],hovertext=[f"{x.get('file')}<br>{x.get('line_range')}<br>{x['chunk_id']}" for x in nodes],
        hoverinfo='text',marker={'size':10},showlegend=False,name='Code sections'))
    cap_note=' (bounded view)' if graph['truncated'] else ''
    fig.update_layout(title=f"Chunk relationship graph — {len(nodes)} shown / {graph['total_nodes']} chunks, {len(edges)} links{cap_note}",
        xaxis={'visible':False},yaxis={'visible':False},showlegend=False,margin={'l':10,'r':10,'t':45,'b':10})
    return fig

def launch_ui(engine: ContextLensEngine | None = None, port: int = 7863, server_name: str = '127.0.0.1'):
    try:
        import gradio as gr
        import plotly.express as px
        import pandas
    except ImportError as e:
        raise RuntimeError('Install UI dependencies with: pip install gradio plotly pandas') from e
    engine=engine or ContextLensEngine()
    # Reopen the last extracted repository automatically after an app-process restart.
    workspace_root = Path(os.environ.get('CONTEXTLENS_WORKSPACE_ROOT', str(Path.cwd() / 'contextlens_colab_workspace')))
    persisted_workspace = workspace_root / 'workspace'
    if engine.data_dir is None and all((persisted_workspace / name).is_file() for name in REQUIRED_FILES):
        engine.load_workspace(persisted_workspace)
    with gr.Blocks(theme=gr.themes.Soft(), title='ContextLens 3.0') as demo:
        gr.Markdown('# ContextLens 3.0\nRepository intelligence and context engineering for UnvibeCode exports.')
        with gr.Row():
            zip_input=gr.File(label='Upload UnvibeCode ZIP',file_types=['.zip'],type='filepath')
            load_btn=gr.Button('Load repository',variant='primary')
        initial_status = 'UnvibeCode repository context loaded. Saved investigations restored.' if engine.data_dir else 'Upload a ZIP containing all six UnvibeCode export files.'
        status=gr.Markdown(initial_status)
        stats=gr.JSON(label='Repository dashboard', value=engine.dashboard_stats() if engine.data_dir else {})
        def load_zip_ui(path):
            if not path: return 'Please upload a ZIP file.', {}, engine.load_sessions()
            try:
                s=engine.load_zip(path, workspace=workspace_root)
                return 'Repository loaded successfully. Saved investigations restored.', s, engine.load_sessions()
            except Exception as e:
                return f'**Load failed:** {e}', {}, engine.load_sessions()
        with gr.Tabs():
            with gr.Tab('Search & chunk explorer'):
                q=gr.Textbox(label='Search files, symbols, code'); lang=gr.Textbox(label='Language filter (optional)'); kind=gr.Textbox(label='Kind filter (optional)')
                search_btn=gr.Button('Search'); results=gr.Dataframe(label='Matching chunks',interactive=False)
                detail_id=gr.Textbox(label='Chunk ID for details'); detail_btn=gr.Button('Inspect chunk'); detail=gr.JSON(label='Chunk details')
                def search_for_ui(query, language, kind):
                    import pandas as pd
                    rows = engine.search(query, language=language, kind=kind)
                    columns = ['chunk_id', 'file', 'entity', 'qualified_name', 'language', 'kind', 'line_range', 'estimated_tokens']
                    return pd.DataFrame(rows, columns=columns)
                search_btn.click(search_for_ui,[q,lang,kind],[results])
                detail_btn.click(lambda x: engine.chunk_details(x) or {'error':'Chunk not found'},[detail_id],[detail])
            with gr.Tab('Context builder & exporter'):
                file=gr.Dropdown(label='Target file',choices=[]); scope=gr.Radio(['0','1','2'],value='0',label='Scope')
                refresh=gr.Button('Load file choices'); build=gr.Button('Build context'); audit=gr.JSON(label='Context quality audit'); output=gr.Textbox(label='Exported context',lines=18)
                refresh.click(lambda: gr.update(choices=sorted(engine.selections),value=None),outputs=[file])
                build.click(lambda f,s: (asdict(engine.audit_context(f,s)) if engine.audit_context(f,s) else {},engine.export_context(f,s)),[file,scope],[audit,output])
                export_json=gr.Button('Switch output to JSON'); export_json.click(lambda f,s: engine.export_context(f,s,'json'),[file,scope],[output])
            with gr.Tab('Unresolved relationship investigator'):
                uq=gr.Textbox(label='Search unresolved records'); reason=gr.Textbox(label='Exact reason filter'); resolution=gr.Textbox(label='Resolution status filter')
                ub=gr.Button('Find records'); uout=gr.JSON(label='Unresolved relationships')
                ub.click(lambda a,b,c: engine.unresolved_search(a,b,c),[uq,reason,resolution],[uout])
            with gr.Tab('Relationship graph'):
                graph_file=gr.Dropdown(label='Focus file (shows its chunks and direct connections)',choices=[])
                graph_refresh=gr.Button('Load file choices'); graph_btn=gr.Button('Build focused graph',variant='primary')
                graph_plot=gr.Plot(label='Chunk-level relationship graph'); graph_out=gr.JSON(label='Graph data / multi-edge relationships')
                graph_refresh.click(lambda: gr.update(choices=sorted(engine.selections),value=None),outputs=[graph_file])
                graph_btn.click(lambda f: (graph_figure(engine,f),engine.chunk_graph(file_path=f)),[graph_file],[graph_plot,graph_out])
            with gr.Tab('Saved investigations'):
                sname=gr.Textbox(label='Session name')
                spayload=gr.Textbox(label='Notes or investigation JSON (either is accepted)',lines=4)
                with gr.Row():
                    save=gr.Button('Save session',variant='primary')
                    refresh_sessions=gr.Button('Refresh saved sessions')
                sessions=gr.JSON(label='Saved sessions', value=engine.load_sessions())
                def save_ui(n,p):
                    try:
                        raw = (p or '').strip()
                        if not str(n or '').strip():
                            raise ValueError('Enter a session name before saving.')
                        if not raw:
                            payload = {}
                        else:
                            try: payload = json.loads(raw)
                            except json.JSONDecodeError: payload = {'notes': raw}
                        engine.save_session(n,payload)
                        return engine.load_sessions()
                    except Exception as e:
                        return {'error':str(e)}
                save.click(save_ui,[sname,spayload],[sessions])
                refresh_sessions.click(lambda: engine.load_sessions(),outputs=[sessions])
            with gr.Tab('Diagnostics & tests'):
                test=gr.Button('Run tests'); tout=gr.JSON(label='Test results')
                def tests_ui():
                    import unittest, importlib
                    import contextlens_tests
                    contextlens_tests = importlib.reload(contextlens_tests)
                    suite=unittest.defaultTestLoader.loadTestsFromTestCase(contextlens_tests.ContextLensTests)
                    result=unittest.TestResult(); suite.run(result)
                    return {'run':result.testsRun,'failures':[str(x[1]) for x in result.failures],
                            'errors':[str(x[1]) for x in result.errors], 'success':result.wasSuccessful()}
                test.click(tests_ui,outputs=[tout])
        load_btn.click(load_zip_ui,[zip_input],[status,stats,sessions])
    demo.launch(server_name=server_name,server_port=port,share=False)

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--data-dir')
    ap.add_argument('--import-zip', help='Load an UnvibeCode repository-context ZIP before opening the UI.')
    ap.add_argument('--workspace-root', help='Persistent ContextLens storage directory for the extracted workspace and investigations.')
    ap.add_argument('--port',type=int,default=7863)
    ap.add_argument('--server-name',default='127.0.0.1')
    args=ap.parse_args()
    eng=ContextLensEngine(args.data_dir) if args.data_dir else ContextLensEngine()
    if args.import_zip:
        workspace_root = Path(args.workspace_root) if args.workspace_root else Path.cwd() / 'contextlens_colab_workspace'
        workspace_root.mkdir(parents=True, exist_ok=True)
        stats = eng.load_zip(args.import_zip, workspace=workspace_root)
        os.environ['CONTEXTLENS_WORKSPACE_ROOT'] = str(workspace_root.resolve())
        print('ContextLens imported UnvibeCode repository context:', json.dumps(stats, ensure_ascii=False), flush=True)
    elif args.workspace_root:
        os.environ['CONTEXTLENS_WORKSPACE_ROOT'] = str(Path(args.workspace_root).resolve())
    launch_ui(eng,args.port,args.server_name)
