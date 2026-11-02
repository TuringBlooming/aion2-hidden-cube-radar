import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import json
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import wave
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
        for path in ('/', '/app.js', '/style.css', '/candidates.json', '/maps/1110.webp', '/maps/1110/4_4.webp'):
            self.assertEqual(self.fetch(path)[0], 200, path)
        self.assertEqual(self.fetch('/maps/1110.webp')[1][:4], b'RIFF')
        self.assertEqual(self.fetch('/maps/1110/4_4.webp')[1][:4], b'RIFF')
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

    def test_imported_image_overrides_bundled_overview(self):
        from PIL import Image
        path = Path(self.tmp.name) / 'assets/maps/1110.webp'
        path.parent.mkdir(parents=True)
        Image.new('RGB', (16, 16), 'blue').save(path)
        self.assertEqual(self.fetch('/maps/1110.webp')[1], path.read_bytes())
        self.assertEqual(self.fetch('/maps/1110/4_4.webp')[0], 200)

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

    def test_alert_preferences_persist_and_invalid_updates_are_atomic(self):
        headers = {'X-Redbag-Key': self.app.token}
        changes = {'sound_profile': 'bubble', 'sound_volume': 22, 'overlay_on_alert': True}
        before = self.app.state.export_history()['records']
        self.assertEqual(self.fetch('/api/control', {'action': 'alert_settings', **changes}, headers)[0], 200)
        for key, value in changes.items():
            self.assertEqual(self.app.snapshot()[key], value)
        saved = (Path(self.tmp.name) / 'alert-settings.json').read_bytes()
        for invalid in ({'sound_volume': True}, {'sound_volume': -1}, {'sound_volume': 101},
                        {'sound_volume': 1.5}, {'sound_profile': '../alarm'},
                        {'overlay_on_alert': 'true'}, {'other': 2}, {}):
            self.assertEqual(self.fetch('/api/control', {'action': 'alert_settings', **invalid}, headers)[0], 400)
            self.assertEqual((Path(self.tmp.name) / 'alert-settings.json').read_bytes(), saved)
        from aion2_redbag_map.model import MapState
        restored = MapState(Path(self.tmp.name), self.app.state.maps, mode='offline', readonly=True)
        self.assertEqual(restored.alert_settings, changes)
        self.assertEqual(restored.export_history()['records'], before)
        self.assertEqual(self.app.state.alert_seq, 0)

    def test_sound_preview_does_not_save_or_emit_bag_alerts(self):
        headers = {'X-Redbag-Key': self.app.token}
        original = dict(self.app.state.alert_settings)
        body = {'action': 'preview_sound', 'sound_profile': 'sonar', 'sound_volume': 0}
        self.assertEqual(self.fetch('/api/control', body)[0], 403)
        code, raw, _ = self.fetch('/api/control', body, headers)
        self.assertEqual(code, 200)
        preview = json.loads(raw)['preview_url']
        code, wav, wav_headers = self.fetch(preview)
        self.assertEqual(code, 200)
        self.assertEqual(wav_headers['Content-Type'], 'audio/wav')
        with wave.open(io.BytesIO(wav)) as source:
            self.assertTrue(all(b == 0 for b in source.readframes(source.getnframes())))
        with patch.object(self.app.notifier, 'preview_sound', return_value=True) as play:
            self.app.notifier.status['available'] = True
            code, raw, _ = self.fetch('/api/control', {**body, 'sound_volume': 20}, headers)
            self.assertEqual(code, 200)
            self.assertNotIn('preview_url', json.loads(raw))
            play.assert_called_once_with('sonar', 20)
        self.assertEqual(self.app.state.alert_settings, original)
        self.assertEqual(self.app.state.alert_seq, 0)
        self.assertIsNone(self.app.state.latest_alert)
        self.assertFalse((Path(self.tmp.name) / 'alert-settings.json').exists())

    def test_sound_route_only_accepts_known_bounded_choices(self):
        for query in ('profile=chime&volume=35', 'profile=alarm&volume=100'):
            self.assertEqual(self.fetch('/api/alert-sound?' + query)[0], 200)
        for query in ('', 'profile=../alert&volume=30', 'profile=chime&volume=101',
                      'profile=chime&volume=-1', 'profile=chime&volume=35.5',
                      'profile=chime&volume=20&volume=30', 'profile=chime&volume=30&file=x'):
            self.assertEqual(self.fetch('/api/alert-sound?' + query)[0], 400)

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
