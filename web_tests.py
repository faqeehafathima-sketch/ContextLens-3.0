import base64, tempfile, unittest
from pathlib import Path
from fastapi.testclient import TestClient
import server

class WebsiteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(server.app)
        cls.sample=(Path(__file__).parent/'sample_evidently_context.zip').read_bytes()

    def setUp(self):
        r=self.client.post('/api/import',json={'filename':'evidently-repository-context.zip','content_base64':base64.b64encode(self.sample).decode()})
        self.assertEqual(r.status_code,200,r.text)

    def test_health_endpoint(self):
        r=self.client.get('/api/health')
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json()['ok'])

    def test_homepage_serves_web_interface(self):
        r=self.client.get('/')
        self.assertEqual(r.status_code,200)
        self.assertIn('ContextLens 3.0',r.text)
        self.assertIn('Analyze a repository',r.text)
        self.assertIn('Generate Track 2 proposal',r.text)
        self.assertIn('Evidence review',r.text)

    def test_import_sample_bundle(self):
        r=self.client.post('/api/import',json={'filename':'evidently-repository-context.zip','content_base64':base64.b64encode(self.sample).decode()})
        self.assertEqual(r.status_code,200,r.text)
        self.assertGreater(r.json()['stats']['chunks'],0)

    def test_search_and_graph_after_import(self):
        r=self.client.post('/api/search',json={'query':'import','limit':20})
        self.assertEqual(r.status_code,200,r.text)
        self.assertIsInstance(r.json()['results'],list)
        g=self.client.get('/api/graph?limit=20')
        self.assertEqual(g.status_code,200,g.text)
        self.assertGreater(len(g.json()['nodes']),0)

    def test_context_export(self):
        files=self.client.get('/api/files').json()['files']
        self.assertTrue(files)
        r=self.client.post('/api/context',json={'file':files[0],'scope':'0','format':'markdown'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertIn('AI Context Export',r.json()['content'])

    def test_report_preview_and_path_traversal_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'shipready_results').mkdir(); (root/'shipready_results'/'02_business_workflow_map.html').write_text('<h1>Workflow</h1>',encoding='utf-8')
            server.active_repo_root=root
            listing=self.client.get('/api/reports').json()['reports']
            self.assertEqual(listing[0]['category'],'workflow')
            view=self.client.get('/api/report',params={'name':'shipready_results/02_business_workflow_map.html'})
            self.assertEqual(view.status_code,200)
            self.assertIn('Workflow',view.text)
            bad=self.client.get('/api/report',params={'name':'../../etc/passwd.html'})
            self.assertEqual(bad.status_code,404)
            server.active_repo_root=None

    def test_save_and_restore_investigation(self):
        r=self.client.post('/api/investigations',json={'title':'Test finding','observation':'Observed behavior','source_evidence':'src/module.py:10','assessment':'Expected X, actual Y'})
        self.assertEqual(r.status_code,200,r.text)
        items=self.client.get('/api/investigations').json()['investigations']
        self.assertTrue(any(x['title']=='Test finding' and x['source_evidence']=='src/module.py:10' for x in items))

    def test_non_github_url_rejected(self):
        r=self.client.post('/api/analyze',json={'repository_url':'https://example.com/evil/repo'})
        self.assertEqual(r.status_code,400)

    def test_invalid_zip_rejected(self):
        r=self.client.post('/api/import',json={'filename':'bad.zip','content_base64':base64.b64encode(b'not a zip').decode()})
        self.assertEqual(r.status_code,400)

    def test_invalid_base64_rejected(self):
        r=self.client.post('/api/import',json={'filename':'bad.zip','content_base64':'%%%not-base64%%%'})
        self.assertEqual(r.status_code,400)

    def test_non_zip_filename_rejected(self):
        r=self.client.post('/api/import',json={'filename':'payload.txt','content_base64':base64.b64encode(b'not a zip').decode()})
        self.assertEqual(r.status_code,400)

    def test_graph_limit_is_bounded(self):
        r=self.client.get('/api/graph?limit=999999')
        self.assertEqual(r.status_code,200,r.text)
        self.assertLessEqual(len(r.json()['nodes']),250)

if __name__=='__main__': unittest.main(verbosity=2)
