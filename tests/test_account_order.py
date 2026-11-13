import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock
import tempfile

spec = importlib.util.spec_from_file_location('agy_switch', Path(__file__).resolve().parents[1] / 'agy_switch.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class AccountOrderTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = root / 'accounts.json'
        self.prefs = root / 'prefs.json'
        self.store.write_text(json.dumps({'version': 1, 'current_id': 'a', 'accounts': [
            {'id': 'a', 'label': 'A'}, {'id': 'b', 'label': 'B'}, {'id': 'c', 'label': 'C'}]}), encoding='utf-8')
        self.prefs.write_text(json.dumps({'theme': 'dark', 'account_order': ['c', 'a', 'b']}), encoding='utf-8')
        self.patches = [mock.patch.object(app, 'STORE_FILE', self.store), mock.patch.object(app, 'PREFS_FILE', self.prefs),
                        mock.patch.object(app, 'live_email', return_value=None), mock.patch.object(app, 'ide_running', return_value=False),
                        mock.patch.object(app, 'find_ide_exe', return_value=None)]
        for p in self.patches: p.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def request(self, path, data=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}{path}',
            data=json.dumps(data).encode() if data is not None else None, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_persistence_and_read_only_normalization(self):
        original = self.store.read_bytes()
        self.assertEqual(self.request('/api/accounts/order', {'ids': ['b', 'unknown', 'b']})[1]['account_order'], ['b', 'c', 'a'])
        self.assertEqual(self.store.read_bytes(), original)
        self.assertEqual(json.loads(self.prefs.read_text())['theme'], 'dark')
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        before = self.prefs.read_bytes()
        self.assertEqual(self.request('/api/state')[1]['account_order'], ['b', 'c', 'a'])
        self.assertEqual(self.prefs.read_bytes(), before)
        store = json.loads(original); store['current_id'] = 'c'; store['accounts'] = [store['accounts'][0], store['accounts'][2], {'id': 'd'}]
        self.store.write_text(json.dumps(store), encoding='utf-8')
        self.assertEqual(self.request('/api/state')[1]['account_order'], ['c', 'a', 'd'])
        self.assertEqual(self.prefs.read_bytes(), before)
        self.assertEqual(self.request('/api/accounts/order', {'ids': []})[1]['account_order'], ['c', 'a', 'd'])

    def test_invalid_and_write_failure(self):
        before = self.store.read_bytes(); prefs = self.prefs.read_bytes()
        for ids in (None, 'a', [1], ['a', None]):
            self.assertEqual(self.request('/api/accounts/order', {'ids': ids})[0], 400)
        with mock.patch.object(app, '_atomic_write', side_effect=OSError('disk failure')):
            self.assertEqual(self.request('/api/accounts/order', {'ids': ['a']})[0], 500)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(self.prefs.read_bytes(), prefs)
