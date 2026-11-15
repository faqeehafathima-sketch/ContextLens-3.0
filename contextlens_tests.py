import json, tempfile, unittest, zipfile
from pathlib import Path
from contextlens_app import ContextLensEngine, safe_extract_zip, SecurityError, DataValidationError, REQUIRED_FILES

class ContextLensTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp()); self.data=self.tmp/'data'; self.data.mkdir()
        (self.data/'manifest.json').write_text('{}')
        (self.data/'README.md').write_text('fixture')
        (self.data/'chunks.jsonl').write_text('\n'.join(json.dumps(x) for x in [
            {'chunk_id':'a','file':'src/a.py','language':'python','kind':'function','qualified_name':'a.foo','numbered_code':'1 def foo(): pass','estimated_tokens':12},
            {'chunk_id':'b','file':'src/b.py','language':'python','kind':'class','qualified_name':'b.Bar','numbered_code':'1 class Bar: pass','estimated_tokens':20}]))
        (self.data/'connections.jsonl').write_text('\n'.join(json.dumps(x) for x in [
            {'source_chunk':'a','target_chunk':'b','relationship':'calls','confidence':'high'},
            {'source_chunk':'a','target_chunk':'b','relationship':'imports','confidence':'medium'}]))
        (self.data/'selections.jsonl').write_text(json.dumps({'file':'src/a.py','scopes':{'0':{'chunk_ids':['a','b','missing'],'estimated_tokens':35,'maximum_tokens':40,'included_files':['src/a.py'],'connected_files_not_included':['src/c.py']}}}))
        (self.data/'unresolved_relationships.jsonl').write_text(json.dumps({'source_file':'src/a.py','symbol':'missing_fn','reason':'not_found','resolution_status':'unresolved'}))
        self.engine=ContextLensEngine(self.data)
    def test_load_and_dashboard(self):
        s=self.engine.dashboard_stats(); self.assertEqual(s['chunks'],2); self.assertEqual(s['files'],2); self.assertEqual(s['connections'],2)
    def test_duplicate_jsonl_retains_first(self):
        p=self.tmp/'dup.jsonl'; p.write_text('{"id":"x","v":1}\n{"id":"x","v":2}\n')
        d,_,e=__import__('contextlens_app').read_jsonl(p,'id'); self.assertEqual(d['x']['v'],1); self.assertTrue(e)
    def test_non_object_jsonl_is_reported(self):
        p=self.tmp/'bad.jsonl'; p.write_text('[]\n')
        _,records,e=__import__('contextlens_app').read_jsonl(p); self.assertEqual(records,[]); self.assertTrue(e)
    def test_context_resolves_and_reports_missing(self):
        ctx,err=self.engine.build_context('src/a.py','0'); self.assertIsNone(err); self.assertEqual(len(ctx['resolved']),2); self.assertEqual(ctx['missing'],['missing'])
    def test_audit_uses_chunk_metadata_and_budget(self):
        a=self.engine.audit_context('src/a.py','0'); self.assertEqual(a.estimated_tokens,32); self.assertEqual(a.maximum_tokens,40); self.assertEqual(a.within_budget,True)
    def test_multi_edge_graph_preserves_relationships(self):
        g=self.engine.chunk_graph(); self.assertEqual(len(g['edges']),2); self.assertEqual({e['relationship'] for e in g['edges']},{'calls','imports'})
    def test_graph_can_focus_on_file_and_include_direct_neighbor(self):
        g=self.engine.chunk_graph(file_path='src/a.py',max_nodes=10)
        self.assertEqual(set(g['nodes']),{'a','b'}); self.assertEqual(len(g['edges']),2)
    def test_unresolved_filters(self):
        r=self.engine.unresolved_search('missing_fn',reason='not_found'); self.assertEqual(len(r),1)
    def test_search_filters(self):
        r=self.engine.search('foo',language='python',kind='function'); self.assertEqual(len(r),1)
    def test_json_export(self):
        d=json.loads(self.engine.export_context('src/a.py','0','json')); self.assertEqual(d['missing_chunk_ids'],['missing']); self.assertEqual(len(d['chunks']),2)
    def test_sessions_persist(self):
        p=self.tmp/'sessions.json'; self.engine.save_session('case-1',{'note':'check'},p); self.assertEqual(self.engine.load_sessions(p)['case-1']['payload']['note'],'check')
    def test_sessions_restore_when_workspace_reopens(self):
        self.engine.save_session('reopen-check', {'note':'must survive reload'})
        reopened = ContextLensEngine(self.data)
        self.assertEqual(reopened.sessions['reopen-check']['payload']['note'], 'must survive reload')
    def test_zip_traversal_rejected(self):
        z=self.tmp/'bad.zip'
        with zipfile.ZipFile(z,'w') as f: f.writestr('../escape.txt','bad')
        with self.assertRaises(SecurityError): safe_extract_zip(z,self.tmp/'out')
    def test_safe_zip_extracts(self):
        z=self.tmp/'safe.zip'
        with zipfile.ZipFile(z,'w') as f: f.writestr('nested/ok.txt','yes')
        names=safe_extract_zip(z,self.tmp/'safeout'); self.assertIn('nested/ok.txt',names); self.assertEqual((self.tmp/'safeout/nested/ok.txt').read_text(),'yes')
    def test_missing_required_file_fails(self):
        p=self.tmp/'incomplete'; p.mkdir(); (p/'manifest.json').write_text('{}')
        with self.assertRaises(DataValidationError): ContextLensEngine(p)
    def test_load_complete_zip(self):
        z=self.tmp/'complete.zip'
        with zipfile.ZipFile(z,'w') as f:
            for name in REQUIRED_FILES: f.write(self.data/name,name)
        e=ContextLensEngine(); stats=e.load_zip(z,self.tmp/'persisted')
        self.assertEqual(stats['chunks'],2); self.assertEqual(e.data_dir.name,'workspace')
    def test_sessions_survive_repository_zip_reload(self):
        z=self.tmp/'reload.zip'
        with zipfile.ZipFile(z,'w') as f:
            for name in REQUIRED_FILES: f.write(self.data/name,name)
        root=self.tmp/'reload-root'
        e=ContextLensEngine(); e.load_zip(z,root)
        e.save_session('keep-me', {'note':'persist across ZIP reload'})
        e.load_zip(z,root)
        self.assertEqual(e.sessions['keep-me']['payload']['note'], 'persist across ZIP reload')
    def test_zip_missing_required_member_fails(self):
        z=self.tmp/'partial.zip'
        with zipfile.ZipFile(z,'w') as f: f.writestr('manifest.json','{}')
        with self.assertRaises(DataValidationError): ContextLensEngine().load_zip(z,self.tmp/'partial')

    def test_failed_zip_upload_preserves_previous_workspace_and_sessions(self):
        root = self.tmp / 'transactional-root'
        good = self.tmp / 'good.zip'
        with zipfile.ZipFile(good, 'w') as f:
            for name in REQUIRED_FILES:
                f.write(self.data / name, name)
        engine = ContextLensEngine()
        engine.load_zip(good, root)
        engine.save_session('important', {'note': 'keep this'})
        bad = self.tmp / 'bad-replacement.zip'
        with zipfile.ZipFile(bad, 'w') as f:
            f.writestr('manifest.json', '{}')
        with self.assertRaises(DataValidationError):
            engine.load_zip(bad, root)
        self.assertEqual(engine.dashboard_stats()['chunks'], 2)
        self.assertEqual(engine.sessions['important']['payload']['note'], 'keep this')
        self.assertTrue((root / 'workspace' / 'chunks.jsonl').is_file())

if __name__=='__main__': unittest.main(verbosity=2)
