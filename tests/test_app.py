import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.app import Application, Handler


class LocalApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        args = argparse.Namespace(data_dir=self.tmp.name, port=0, empty=False, replay=None, no_capture=True)
        self.app = Application(args)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, application=self.app))
        self.app.server = self.server
        self.app.url = f'http://127.0.0.1:{self.server.server_port}'
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.app.close()
        self.tmp.cleanup()

    def fetch(self, path, body=None, headers=None):
        req = Request(self.app.url + path, headers=headers or {},
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(req, timeout=2) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def test_pages_state_and_assets(self):
        for path in ('/', '/app.js', '/style.css', '/candidates.json'):
            self.assertEqual(self.fetch(path)[0], 200, path)
        self.assertEqual(self.fetch('/maps/1110.webp')[0], 404, "proprietary artwork is not bundled")
        self.assertEqual(self.fetch('/style.css')[2]['Cache-Control'], 'no-cache')
        code, data, headers = self.fetch('/api/state')
        state = json.loads(data)
        self.assertEqual(state['records'], [], "a new installation must not seed someone else's observations")
        self.assertEqual(state['application_id'], 'aion2-redbag-map')
        self.assertEqual(state['alert_seq'], 0)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIsNone(headers.get('Access-Control-Allow-Origin'))
        candidates = json.loads(self.fetch('/candidates.json')[1])
        self.assertEqual(candidates['source_count'], 3722)
        self.assertEqual(candidates['unique_count'], 3721)
        self.assertEqual(candidates['maps']['1110']['unique_count'], 1370)
        self.assertEqual(state['alert_seq'], 0)

    def test_local_origin_and_control_token_required(self):
        self.assertEqual(self.fetch('/api/control', {'action': 'pause'})[0], 403)
        headers = {'X-Redbag-Key': self.app.token, 'Origin': 'https://unrelated.invalid'}
        self.assertEqual(self.fetch('/api/control', {'action': 'pause'}, headers)[0], 403)
        self.assertEqual(self.fetch('/api/state', headers={'Host': 'unrelated.invalid'})[0], 403)
        headers['Origin'] = self.app.url
        self.app.args.no_capture = False  # Control-path unit check; no Collector is instantiated.
        self.assertEqual(self.fetch('/api/control', {'action': 'pause'}, headers)[0], 200)
        self.assertTrue(self.app.state.paused)
        self.assertEqual(self.fetch('/api/control', {'action': 'resume'}, headers)[0], 200)
        self.assertFalse(self.app.state.paused)

    def test_preferences_and_unknown_input(self):
        headers = {'X-Redbag-Key': self.app.token}
        self.assertEqual(self.fetch('/api/control', {'action': 'sound', 'enabled': False}, headers)[0], 200)
        self.assertFalse(self.app.state.sound)
        self.assertEqual(self.fetch('/api/control', {'action': 'sound', 'enabled': 'true'}, headers)[0], 400)
        self.assertEqual(self.fetch('/api/control', {'action': 'map', 'map_id': 1010}, headers)[0], 200)
        self.assertEqual(self.app.state.current_map_id, 1010)
        self.assertEqual(self.fetch('/api/control', {'action': 'map', 'map_id': -1}, headers)[0], 400)

    def test_history_and_python_sources_are_not_served(self):
        for path in ('/data/history.json', '/app.py', '/../model.py', '/maps/'):
            self.assertEqual(self.fetch(path)[0], 404, path)

    def test_export_download_contains_records_without_runtime_control_credentials(self):
        code, raw, headers = self.fetch('/api/history-export')
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data['schema'], 1)
        self.assertEqual(data['records'], [])
        self.assertTrue(headers['Content-Disposition'].startswith('attachment; filename="AION2-backpack-history-'))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('control_token', data)
        self.assertNotIn('capture', data)
        self.assertEqual(self.fetch('/api/history-export', headers={'Origin': 'https://unrelated.invalid'})[0], 403)
        self.assertEqual(self.fetch('/api/history-export', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_open_folder_can_only_open_configured_tool_archive(self):
        with patch('aion2_redbag_map.app.os.startfile', create=True) as open_folder:
            self.assertEqual(self.fetch('/api/control', {'action': 'open_data_folder'})[0], 403)
            open_folder.assert_not_called()
            headers = {'X-Redbag-Key': self.app.token}
            self.assertEqual(self.fetch('/api/control', {'action': 'open_data_folder', 'path': 'ignored'}, headers)[0], 200)
            open_folder.assert_called_once_with(str(self.app.directory))

    def test_git_worker_only_follows_real_default_archive_and_never_capture_tests(self):
        with patch('aion2_redbag_map.app.subprocess.Popen') as process:
            self.app.start_archive_versions()
            process.assert_not_called()
            self.app.args.no_capture = False
            self.app.start_archive_versions()
            command = process.call_args.args[0]
            self.assertIn('--worker', command)
            self.assertEqual(command[command.index('--worker') + 1], 'archive')
            self.assertEqual(command[command.index('--data-dir') + 1], str(self.app.directory))
            self.assertIn('--parent-pid', command)
            self.assertEqual(command[command.index('--wait-for-lock') + 1], '10')
            self.assertNotIn('collector.py', command)
            self.app.archive_process = None

    def test_offline_mode_cannot_start_receiving(self):
        self.assertEqual(self.app.state.mode, 'offline')
        headers = {'X-Redbag-Key': self.app.token}
        self.assertEqual(self.fetch('/api/control', {'action': 'resume'}, headers)[0], 400)

    def test_real_capture_reaches_state_model_and_one_alert(self):
        sample = Path(__file__).resolve().parent / 'private' / 'red-focus-01.pcap'
        if not sample.exists():
            self.skipTest('Private source capture is intentionally excluded from the package')
        from aion2_redbag_map.collector import replay_pcap
        from aion2_redbag_map.model import MapState
        self.app.state = MapState(Path(self.tmp.name) / 'replay', self.app.state.maps, mode='replay')
        self.app.args.replay = str(sample)
        replay_pcap(sample, self.app.event)
        result = self.app.snapshot()
        self.assertEqual(result['alert_seq'], 1)
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['status'], 'opened')
        self.assertEqual(result['records'][0]['entity_id'], 42895)
        self.assertEqual(result['records'][0]['x'], 106518.438)
        self.assertAlmostEqual(result['records'][0]['opened_at'], 1792749952.829333, places=5)
        self.assertEqual(self.app.notifier.status['sent'], 0)


if __name__ == '__main__':
    unittest.main()
