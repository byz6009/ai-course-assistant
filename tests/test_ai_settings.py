import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server.ai_settings import AISettings
from server.app import handler_for
from server.domain import ValidationError
from server.repository import CourseRepository
from server.replays import ConflictError, ReplayRepository, ReplayService
from server.summarizers import ExtractiveSummarizer, GenerationError, normalize_base_url


class SettingsFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        environment = patch.dict('os.environ', {'KEJI_AI_BASE_URL': '', 'KEJI_AI_API_KEY': '', 'KEJI_AI_MODEL': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.courses = CourseRepository(self.folder / 'courses.sqlite3')
        self.repo = ReplayRepository(self.courses)
        self.service = ReplayService(self.repo, workers=1)
        self.addCleanup(self.service.close)
        self.path = self.folder / 'ai-config.json'
        self.settings = AISettings(self.path, self.service)

    def body(self, **values):
        return dict({'baseUrl': 'https://models.example/v1', 'model': 'text-model', 'apiKey': 'fake-test-key',
                     'revision': self.settings.status()['revision'], 'timeout': 60, 'jsonMode': True}, **values)

    def mock_model(self, bad_citation=False, status=200, redirect_to=None):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def send_json(self, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                if redirect_to:
                    self.send_header('Location', redirect_to)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                captured.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
                self.send_json({'data': [{'id': 'text-model'}, {'id': 'another-model'}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append({'path': self.path, 'body': body, 'authorization': self.headers.get('Authorization')})
                result = {'chapters': [{'title': '需求分析', 'summary': '先识别用户目标。',
                                        'cueIds': ['fabricated' if bad_citation else 'c00001']}]}
                self.send_json({'choices': [{'message': {'content': '```json\n' + json.dumps(result) + '\n```'}}]})

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join()
        self.addCleanup(close)
        return f'http://127.0.0.1:{server.server_port}/v1', captured


class AISettingsTests(SettingsFixture):
    def test_normalize_full_endpoint_and_reject_invalid_urls(self):
        self.assertEqual(normalize_base_url(' https://models.example/v1/chat/completions/ '), 'https://models.example/v1')
        for url in ['http://remote.example/v1', 'https://', 'file:///data', 'https://u:p@example.com',
                    'https://example.com?key=secret', 'https://example.com/#x', 'https://example.com:bad',
                    'https://example.com/\nsecret', 'https://example.com:0', 'https://example.com\\evil']:
            with self.subTest(url=url), self.assertRaises(ValidationError): normalize_base_url(url)

    def test_save_restart_and_no_secret_in_status_or_capabilities(self):
        result = self.settings.update(self.body())
        self.assertTrue(result['enabled'])
        self.assertTrue(result['hasApiKey'])
        self.assertNotIn('fake-test-key', json.dumps(result))
        self.assertNotIn('apiKey', result)
        self.assertTrue(self.service.capabilities()['aiConfigured'])
        restored = AISettings(self.path, self.service)
        self.assertEqual(restored.status()['model'], 'text-model')
        self.assertEqual(restored.config['apiKey'], 'fake-test-key')
        self.assertNotIn('fake-test-key', json.dumps(self.service.capabilities()))

    def test_blank_key_retention_and_new_endpoint_requires_new_key(self):
        self.settings.update(self.body())
        self.settings.update(self.body(baseUrl='https://models.example/v1/chat/completions', model='new-model', apiKey=''))
        self.assertEqual(self.settings.config['apiKey'], 'fake-test-key')
        with self.assertRaises(ValidationError): self.settings.update(self.body(baseUrl='https://another.example/v1', apiKey=''))
        self.assertEqual(self.settings.status()['baseUrl'], 'https://models.example/v1')

    def test_invalid_settings_do_not_change_saved_configuration(self):
        self.settings.update(self.body())
        original = self.path.read_bytes()
        for values in [{'timeout': 0}, {'timeout': 91}, {'timeout': True}, {'jsonMode': 'yes'},
                       {'model': 'bad model'}, {'apiKey': 'bad\nkey'}, {'apiKey': ['key']}, {'baseUrl': []}]:
            with self.subTest(values=values), self.assertRaises(ValidationError): self.settings.update(self.body(**values))
            self.assertEqual(self.path.read_bytes(), original)

    def test_stale_form_cannot_overwrite_or_test_new_configuration(self):
        stale = self.body()
        self.settings.update(stale)
        for call in [self.settings.update, self.settings.probe]:
            with self.assertRaises(ConflictError): call(stale)

    def test_failed_atomic_save_preserves_file_and_provider(self):
        self.settings.update(self.body())
        original = self.path.read_bytes()
        with patch('server.ai_settings.os.replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError): self.settings.update(self.body(model='new-model'))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.service.providers['ai'].model, 'text-model')
        self.assertEqual(list(self.folder.glob('.ai-settings-*.tmp')), [])

    def test_environment_override_disable_restart_and_restore(self):
        environment = {'KEJI_AI_BASE_URL': 'https://environment.example/v1',
                       'KEJI_AI_MODEL': 'env-model', 'KEJI_AI_API_KEY': 'fake-env-key'}
        with patch.dict('os.environ', environment):
            self.settings = AISettings(self.path, self.service)
            self.assertEqual(self.settings.status()['source'], 'environment')
            self.settings.update(self.body())
            self.assertEqual(self.settings.status()['source'], 'local')
            self.settings.update(self.body(action='disable'))
            self.assertNotIn('fake-test-key', self.path.read_text())
            self.settings = AISettings(self.path, self.service)
            self.assertFalse(self.settings.status()['enabled'])
            self.settings.update(self.body(action='environment'))
            self.assertEqual(self.settings.status()['model'], 'env-model')
            self.assertFalse(self.path.exists())

    def test_corrupt_file_keeps_local_mode_available_and_can_be_replaced(self):
        self.path.write_text('broken-secret-config', encoding='utf-8')
        self.settings = AISettings(self.path, self.service)
        self.assertEqual(self.settings.status()['source'], 'invalid')
        self.assertTrue(self.settings.status()['error'])
        self.assertFalse(self.service.capabilities()['aiConfigured'])
        self.assertNotIn('broken-secret', json.dumps(self.settings.status()))
        self.settings.update(self.body())
        self.assertTrue(self.service.capabilities()['aiConfigured'])

    def test_probe_actual_protocol_json_mode_and_citations_without_persistence(self):
        base, captured = self.mock_model()
        result = self.settings.probe(self.body(baseUrl=base, jsonMode=False))
        self.assertTrue(result['ok'])
        self.assertEqual(result['sample']['citations'][0]['start'], 0)
        self.assertEqual(captured[0]['path'], '/v1/chat/completions')
        self.assertEqual(captured[0]['authorization'], 'Bearer fake-test-key')
        self.assertNotIn('response_format', captured[0]['body'])
        self.assertFalse(captured[0]['body']['stream'])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.settings.status()['enabled'])
        self.assertEqual(self.repo.list(), [])
        self.assertEqual(self.repo.tasks(), [])

    def test_probe_rejects_fabricated_citation(self):
        base, _ = self.mock_model(bad_citation=True)
        with self.assertRaises(ValidationError): self.settings.probe(self.body(baseUrl=base))
        self.assertFalse(self.path.exists())

    def test_model_discovery_and_local_no_key(self):
        base, captured = self.mock_model()
        result = self.settings.probe(self.body(baseUrl=base, model='', apiKey=''), models=True)
        self.assertEqual(result['models'], ['another-model', 'text-model'])
        self.assertEqual(captured[0]['path'], '/v1/models')
        self.assertIsNone(captured[0]['authorization'])
        self.settings.update(self.body(baseUrl=base, apiKey=''))
        self.assertTrue(self.settings.status()['enabled'])
        self.assertFalse(self.settings.status()['hasApiKey'])

    def test_remote_failure_does_not_persist_or_expose_key(self):
        base, _ = self.mock_model(status=401)
        with self.assertRaises(GenerationError) as error: self.settings.probe(self.body(baseUrl=base))
        self.assertIn('401', str(error.exception))
        self.assertNotIn('fake-test-key', str(error.exception))
        self.assertFalse(self.path.exists())

    def test_redirect_does_not_forward_credentials_to_another_endpoint(self):
        target, target_calls = self.mock_model()
        base, calls = self.mock_model(status=302, redirect_to=target + '/chat/completions')
        with self.assertRaises(GenerationError): self.settings.probe(self.body(baseUrl=base))
        self.assertEqual(len(calls), 1)
        self.assertEqual(target_calls, [])

    def test_queued_jobs_keep_provider_selected_at_submission(self):
        self.courses.create_course({'id': 'se', 'name': '软件工程'})
        body = {'courseId': 'se', 'title': '示例', 'filename': 'sample.srt',
                'content': '1\n00:00:00,000 --> 00:00:10,000\n识别用户目标。'}
        first = self.repo.import_replay(body)['id']
        second = self.repo.import_replay(dict(body, content=body['content'] + '验证失败路径。'))['id']
        ready, release = threading.Event(), threading.Event()
        class Blocking(ExtractiveSummarizer):
            def summarize(self, replay):
                ready.set()
                if not release.wait(4): raise RuntimeError('Test did not release provider')
                result = super().summarize(replay)
                result['chapters'][0]['title'] = '原配置'
                return result
        self.service.configure_ai(Blocking())
        job1 = self.service.submit(first, 'ai')
        self.assertTrue(ready.wait(2))
        job2 = self.service.submit(second, 'ai')
        self.service.configure_ai(None)
        release.set()
        for job in [job1, job2]:
            deadline = time.monotonic() + 4
            while self.repo.job(job['id'])['status'] in ('queued', 'running') and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.repo.job(job['id'])['status'], 'succeeded')
        self.assertEqual(self.repo.get(second)['result']['chapters'][0]['title'], '原配置')


class AISettingsApiTests(SettingsFixture):
    def setUp(self):
        super().setUp()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.courses, replay_service=self.service, ai_settings=self.settings))
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        def close():
            self.http.shutdown(); self.http.server_close(); thread.join()
        self.addCleanup(close)

    def request(self, endpoint='/api/settings/ai', body=None, headers=None):
        req = Request(f'http://127.0.0.1:{self.http.server_port}{endpoint}',
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urlopen(req, timeout=5) as response: return response.status, json.load(response)
        except HTTPError as error:
            with error: return error.code, json.load(error)

    def test_config_api_save_test_enable_disable_and_secret_redaction(self):
        base, _ = self.mock_model()
        body = self.body(baseUrl=base)
        self.assertEqual(self.request('/api/settings/ai/test', body)[0], 200)
        result = self.request(body=body)
        self.assertEqual(result[0], 200)
        self.assertNotIn('fake-test-key', json.dumps(self.request()))
        self.assertTrue(self.request('/api/capabilities')[1]['aiConfigured'])
        self.assertEqual(self.request(body=body)[0], 409)
        self.assertEqual(self.request(body=self.body(action='disable'))[0], 200)
        self.assertFalse(self.request('/api/capabilities')[1]['aiConfigured'])

    def test_api_rejects_cross_origin_bad_hosts_and_invalid_payloads(self):
        self.assertEqual(self.request(body=self.body(), headers={'Origin': 'https://foreign.example'})[0], 400)
        self.assertEqual(self.request(headers={'Host': 'foreign.example'})[0], 403)
        self.assertEqual(self.request(body=[])[0], 400)
        self.assertEqual(self.request(body=self.body(apiKey=''))[0], 400)
        self.assertFalse(self.path.exists())

    def test_api_sanitizes_provider_and_filesystem_failures(self):
        base, _ = self.mock_model(status=429)
        result = self.request('/api/settings/ai/test', self.body(baseUrl=base))
        self.assertEqual(result[0], 502)
        self.assertEqual(result[1]['code'], 'AI_HTTP_ERROR')
        with patch.object(self.settings, '_write', side_effect=OSError('fake-test-key')):
            result = self.request(body=self.body())
        self.assertEqual(result[0], 500)
        self.assertNotIn('fake-test-key', json.dumps(result))
