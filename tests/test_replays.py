import copy
import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server.app import handler_for
from server.domain import ValidationError
from server.repository import CourseRepository
from server.replays import ConflictError, QueueFullError, ReplayRepository, ReplayService
from server.subtitles import parse_replay, parse_subtitles
from server.summarizers import ExtractiveSummarizer, GenerationError, OpenAICompatibleSummarizer, validate_summary

ROOT = Path(__file__).resolve().parents[1]
SRT = (ROOT / 'examples/replay-demo.srt').read_text(encoding='utf-8')


def payload(course='se', content=SRT):
    return {'courseId': course, 'title': '合成示例课程回放', 'filename': 'sample.srt', 'content': content}


def wait_job(repo, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = repo.job(job_id)
        if job['status'] in ('succeeded', 'failed'):
            return job
        time.sleep(0.01)
    raise AssertionError('Job did not finish')


class SubtitleTests(unittest.TestCase):
    def test_srt_unicode_multiline_and_actual_times(self):
        cues = parse_subtitles('\ufeff' + SRT.replace('\n', '\r\n'), '字幕.srt')
        self.assertEqual(len(cues), 16)
        self.assertEqual(cues[0]['start'], 0)
        self.assertEqual(cues[0]['end'], 18.5)
        self.assertEqual(cues[-1]['end'], 385)

    def test_vtt_identifiers_notes_settings_and_markup(self):
        vtt = 'WEBVTT\n\nNOTE generated captions\nignore\n\nSTYLE\n::cue { color: red; }\n\nintro\n00:01.000 --> 00:03.500 align:start\n<v Teacher>课程 &amp; <b>知识</b>\n第二行\n'
        self.assertEqual(parse_subtitles(vtt, 'x.vtt'), [{'id': 'c00001', 'start': 1, 'end': 3.5, 'text': '课程 & 知识\n第二行'}])

    def test_malformed_empty_untimed_and_non_increasing_files_fail(self):
        inputs = ['', '普通文本', '1\n00:00:04,000 --> 00:00:01,000\n错误',
                  '1\n00:60:00,000 --> 00:61:00,000\n错误',
                  '1\n00:00:01,000 --> 00:00:02,000\n',
                  '1\n00:00:03,000 --> 00:00:04,000\nA\n\n2\n00:00:01,000 --> 00:00:02,000\nB']
        for value in inputs:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_subtitles(value, 'x.srt')
        with self.assertRaises(ValidationError): parse_subtitles(SRT, 'x.mp4')
        with self.assertRaises(ValidationError): parse_subtitles(SRT, 'x.vtt')

    def test_size_and_actual_media_duration_limits(self):
        with self.assertRaises(ValidationError): parse_subtitles('中' * (2 * 1024 * 1024), 'x.srt')
        for duration in [True, 1, '500', float('nan'), float('inf')]:
            with self.subTest(duration=duration), self.assertRaises(ValidationError):
                parse_replay(dict(payload(), durationSeconds=duration))
        self.assertEqual(parse_replay(dict(payload(), durationSeconds=400))['duration'], 400)

    def test_sources_and_filename_are_untrusted(self):
        for source in ['javascript:alert(1)', 'file:///c:/private', 'https://a.test/?token=secret']:
            with self.assertRaises(ValidationError): parse_replay(dict(payload(), sourceUrl=source))
        self.assertEqual(parse_replay(dict(payload(), filename='C:\\private\\sample.srt'))['filename'], 'sample.srt')

    def test_local_summary_uses_real_excerpts_and_covers_last_section(self):
        replay = parse_replay(payload())
        result = validate_summary(ExtractiveSummarizer().summarize(replay), replay['cues'])
        self.assertGreater(len(result['chapters']), 1)
        self.assertGreater(result['chapters'][-1]['end'], replay['duration'] * 0.75)
        for chapter in result['chapters']:
            self.assertTrue(chapter['citations'])
            for citation in chapter['citations']:
                self.assertIn(citation['text'], chapter['summary'])
                self.assertIn(citation, replay['cues'])

    def test_output_citations_cannot_invent_times_or_sources(self):
        cues = parse_replay(payload())['cues']
        chapter = {'title': '要点', 'summary': '内容', 'cueIds': ['c00001'], 'start': 9999, 'citations': [{'text': '伪造'}]}
        result = validate_summary({'chapters': [chapter]}, cues)
        self.assertEqual(result['chapters'][0]['citations'], [cues[0]])
        for ids in [[], ['unknown'], [True], ['c00001'] * 21]:
            with self.assertRaises(ValidationError): validate_summary({'chapters': [dict(chapter, cueIds=ids)]}, cues)


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.courses = CourseRepository(Path(self.temp.name) / 'courses.sqlite3')
        self.courses.create_course({'id': 'se', 'name': '软件工程'})
        self.repo = ReplayRepository(self.courses)
        self.rid = self.repo.import_replay(payload())['id']

    def summarized(self):
        replay = self.repo.get(self.rid)
        return self.repo.save_result(self.rid, ExtractiveSummarizer().summarize(replay), 'local')


class ReplayStoreTests(RepositoryFixture):
    def test_backup_and_restore_preserve_versions_tasks_and_course_metadata(self):
        from tools.backup import backup_database
        self.summarized()
        task = self.repo.confirm_task(self.rid, {'confirmed': True, 'version': 1, 'chapterIds': ['p1']})
        self.repo.set_task_done(task['id'], True)
        target = backup_database(self.courses.path, Path(self.temp.name) / 'backup.sqlite3')
        restored_courses = CourseRepository(target)
        restored = ReplayRepository(restored_courses)
        self.assertEqual(restored_courses.catalog(), self.courses.catalog())
        self.assertEqual(restored.get(self.rid), self.repo.get(self.rid))
        self.assertEqual(restored.tasks(), self.repo.tasks())
        with self.assertRaises(FileExistsError): backup_database(self.courses.path, target)

    def test_duplicate_normalized_subtitle_and_cross_course_isolation(self):
        same = self.repo.import_replay(dict(payload(content=SRT.replace('\n', '\r\n')), filename='renamed.srt', title='new title'))
        self.assertTrue(same['duplicate'])
        self.assertEqual(same['id'], self.rid)
        with self.assertRaises(ValidationError): self.repo.import_replay(payload(course='missing'))
        self.courses.create_course({'id': 'other', 'name': '另一门课'})
        other = self.repo.import_replay(payload(course='other'))
        self.assertNotEqual(other['id'], self.rid)
        self.assertEqual(len(self.repo.list()), 2)

    def test_reimport_never_overwrites_saved_summary_and_task(self):
        version = self.summarized()
        task = self.repo.confirm_task(self.rid, {'confirmed': True, 'version': version['version'], 'chapterIds': ['p1']})
        self.repo.set_task_done(task['id'], True)
        self.repo.import_replay(payload())
        reopened = ReplayRepository(CourseRepository(self.courses.path))
        self.assertEqual(reopened.get(self.rid)['result']['version'], 1)
        self.assertTrue(reopened.tasks()[0]['done'])

    def test_versions_are_immutable_and_stale_edits_conflict(self):
        first = self.summarized()
        changed = copy.deepcopy(first)
        changed['chapters'][0]['summary'] = '人工核对后的要点'
        second = self.repo.save_result(self.rid, changed, 'manual', expected_version=1)
        self.assertEqual(second['version'], 2)
        self.assertEqual(self.repo.version(self.rid, 1), first)
        with self.assertRaises(ConflictError): self.repo.save_result(self.rid, first, 'manual', expected_version=1)
        self.assertEqual(self.repo.get(self.rid)['result']['version'], 2)

    def test_confirmation_required_and_repeated_clicks_create_one_task(self):
        self.summarized()
        for body in [{}, {'confirmed': False}, {'confirmed': True, 'version': 1, 'chapterIds': ['unknown']}]:
            with self.assertRaises(ValidationError): self.repo.confirm_task(self.rid, body)
        self.assertEqual(self.repo.tasks(), [])
        body = {'confirmed': True, 'version': 1, 'chapterIds': ['p1']}
        self.assertFalse(self.repo.confirm_task(self.rid, body)['duplicate'])
        self.assertTrue(self.repo.confirm_task(self.rid, body)['duplicate'])
        self.assertEqual(len(self.repo.tasks()), 1)
        self.summarized()
        self.repo.confirm_task(self.rid, dict(body, version=2))
        self.assertEqual(self.repo.tasks()[0]['version'], 1)
        with self.assertRaises(KeyError): self.repo.set_task_done('missing', True)

    def test_course_creation_is_idempotent_and_does_not_rename_existing(self):
        self.courses.create_course({'id': 'se', 'name': '软件工程'})
        with self.assertRaises(ValidationError): self.courses.create_course({'id': 'se', 'name': '其他课程'})
        with self.assertRaises(ValidationError): self.courses.create_course({'id': 'bad id', 'name': '课程'})

    def test_restart_marks_interrupted_jobs_failed_and_retains_results(self):
        self.summarized()
        job, _ = self.repo.create_job(self.rid, 'local', 25)
        self.repo.running(job['id'])
        service = ReplayService(self.repo)
        self.addCleanup(service.close)
        self.assertEqual(self.repo.job(job['id'])['errorCode'], 'INTERRUPTED')
        self.assertEqual(self.repo.get(self.rid)['result']['version'], 1)


class ServiceTests(RepositoryFixture):
    def test_ten_jobs_use_at_most_five_default_workers(self):
        gate, lock = threading.Event(), threading.Lock()
        counts = {'active': 0, 'max': 0}
        class Blocking:
            label = 'Concurrency test'
            def summarize(self, replay):
                with lock:
                    counts['active'] += 1
                    counts['max'] = max(counts['max'], counts['active'])
                gate.wait(5)
                with lock: counts['active'] -= 1
                return ExtractiveSummarizer().summarize(replay)
        service = ReplayService(self.repo, ai=Blocking())
        self.addCleanup(service.close)
        self.addCleanup(gate.set)
        ids = [self.repo.import_replay(payload(content=SRT.replace('需求描述', f'并发课程{i}')))['id'] for i in range(10)]
        jobs = [service.submit(rid, 'ai') for rid in ids]
        deadline = time.monotonic() + 2
        while counts['max'] < 5 and time.monotonic() < deadline: time.sleep(0.01)
        self.assertEqual(counts['max'], 5)
        self.assertTrue(any(self.repo.job(job['id'])['status'] == 'queued' for job in jobs))
        gate.set()
        for job in jobs: self.assertEqual(wait_job(self.repo, job['id'])['status'], 'succeeded')
        self.assertEqual(counts['max'], 5)

    def test_async_success_does_not_create_task_implicitly(self):
        service = ReplayService(self.repo)
        self.addCleanup(service.close)
        job = service.submit(self.rid, 'local')
        self.assertEqual(wait_job(self.repo, job['id'])['status'], 'succeeded')
        self.assertEqual(self.repo.tasks(), [])
        self.assertEqual(self.repo.get(self.rid)['result']['provider'], 'local')

    def test_failure_retry_and_old_result_preservation(self):
        self.summarized()
        class Failing:
            label = 'Test provider'
            def summarize(self, replay): raise GenerationError('AI_TIMEOUT', '模型请求超时')
        service = ReplayService(self.repo, ai=Failing())
        self.addCleanup(service.close)
        failed = wait_job(self.repo, service.submit(self.rid, 'ai')['id'])
        self.assertEqual(failed['errorCode'], 'AI_TIMEOUT')
        self.assertEqual(self.repo.get(self.rid)['result']['version'], 1)
        succeeded = wait_job(self.repo, service.submit(self.rid, 'local')['id'])
        self.assertEqual(succeeded['status'], 'succeeded')
        self.assertEqual(self.repo.get(self.rid)['result']['version'], 2)

    def test_invalid_model_output_is_not_saved(self):
        class Invalid:
            label = 'Test provider'
            def summarize(self, replay): return {'chapters': [{'title': 'fake', 'summary': 'fake', 'cueIds': ['fake']}]}
        service = ReplayService(self.repo, ai=Invalid())
        self.addCleanup(service.close)
        job = wait_job(self.repo, service.submit(self.rid, 'ai')['id'])
        self.assertEqual(job['errorCode'], 'INVALID_RESULT')
        self.assertIsNone(self.repo.get(self.rid)['result'])

    def test_exception_does_not_expose_credentials(self):
        class Failing:
            label = 'Test provider'
            def summarize(self, replay): raise RuntimeError('SECRET_KEY private source text')
        service = ReplayService(self.repo, ai=Failing())
        self.addCleanup(service.close)
        job = wait_job(self.repo, service.submit(self.rid, 'ai')['id'])
        self.assertNotIn('SECRET', json.dumps(job))

    def test_bounded_concurrency_queue_and_same_target_dedup(self):
        gate, lock = threading.Event(), threading.Lock()
        counts = {'active': 0, 'max': 0}
        class Blocking:
            label = 'Test provider'
            def summarize(self, replay):
                with lock:
                    counts['active'] += 1
                    counts['max'] = max(counts['active'], counts['max'])
                gate.wait(5)
                with lock: counts['active'] -= 1
                return ExtractiveSummarizer().summarize(replay)
        service = ReplayService(self.repo, ai=Blocking(), workers=2, queue_size=1)
        self.addCleanup(service.close)
        self.addCleanup(gate.set)
        first = service.submit(self.rid, 'ai')
        duplicate = service.submit(self.rid, 'ai')
        self.assertEqual(first['id'], duplicate['id'])
        self.assertTrue(duplicate['duplicate'])
        ids = [self.repo.import_replay(payload(content=SRT.replace('需求描述', f'需求描述{i}')))['id'] for i in range(3)]
        jobs = [first] + [service.submit(rid, 'ai') for rid in ids[:2]]
        with self.assertRaises(QueueFullError): service.submit(ids[2], 'ai')
        gate.set()
        for job in jobs: self.assertEqual(wait_job(self.repo, job['id'])['status'], 'succeeded')
        self.assertLessEqual(counts['max'], 2)

    def test_unconfigured_provider_cannot_run(self):
        service = ReplayService(self.repo)
        self.addCleanup(service.close)
        self.assertFalse(service.capabilities()['aiConfigured'])
        for provider in ['ai', [], None]:
            with self.assertRaises(ValidationError): service.submit(self.rid, provider)


class ReplayApiTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.service = ReplayService(self.repo)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.courses, replay_service=self.service))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.service.close()

    def request(self, path, body=None, method='GET', headers=None):
        req = Request(f'http://127.0.0.1:{self.http.server_port}{path}',
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json', **(headers or {})}, method=method)
        try:
            with urlopen(req, timeout=5) as response: return response.status, json.load(response)
        except HTTPError as error:
            with error: return error.code, json.load(error)

    def test_complete_http_workflow_and_persistence(self):
        self.assertEqual(self.request('/api/replays/preview', payload(), 'POST')[1]['cueCount'], 16)
        self.assertEqual(self.request('/api/replays', payload(), 'POST')[1]['duplicate'], True)
        status, job = self.request(f'/api/replays/{self.rid}/jobs', {'provider': 'local'}, 'POST')
        self.assertEqual(status, 202)
        self.assertEqual(wait_job(self.repo, job['id'])['status'], 'succeeded')
        self.assertEqual(self.request(f'/api/jobs/{job["id"]}')[1]['status'], 'succeeded')
        detail = self.request(f'/api/replays/{self.rid}')[1]
        body = {'confirmed': True, 'version': 1, 'chapterIds': [detail['result']['chapters'][0]['id']]}
        task = self.request(f'/api/replays/{self.rid}/tasks', body, 'POST')[1]
        self.assertEqual(self.request(f'/api/tasks/{task["id"]}', {'done': True}, 'PATCH')[0], 200)
        self.assertTrue(self.request('/api/catalog')[1]['tasks'][0]['done'])
        changed = dict(detail['result'], baseVersion=1)
        changed['chapters'][0]['summary'] = '人工编辑'
        self.assertEqual(self.request(f'/api/replays/{self.rid}/summary', changed, 'PATCH')[0], 200)
        self.assertEqual(self.request(f'/api/replays/{self.rid}/summary', changed, 'PATCH')[0], 409)
        self.assertNotEqual(self.request(f'/api/replays/{self.rid}/versions/1')[1]['chapters'][0]['summary'], '人工编辑')

    def test_api_validates_wrong_payload_unknown_ids_and_cross_origin(self):
        self.assertEqual(self.request('/api/replays', [], 'POST')[0], 400)
        self.assertEqual(self.request('/api/replays', payload(), 'POST', {'Origin': 'https://evil.example'})[0], 400)
        self.assertEqual(self.request('/api/catalog', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('/api/replays/' + 'f' * 24)[0], 404)
        self.assertEqual(self.request(f'/api/replays/{self.rid}/jobs', {'provider': 'ai'}, 'POST')[0], 400)
        self.assertEqual(self.request(f'/api/replays/{self.rid}/tasks', {'confirmed': False}, 'POST')[0], 400)
        self.assertEqual(self.request('/api/tasks/' + 'f' * 24, {'done': 'yes'}, 'PATCH')[0], 400)
        self.assertEqual(self.request('/api/courses', {'id': 'other', 'name': '课程'}, 'POST')[0], 201)
        self.assertEqual(self.request('/api/replays')[0], 200)


class ModelAdapterTests(unittest.TestCase):
    def mock_server(self, status=200, content=None, delay=0):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append({'path': self.path, 'body': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
                time.sleep(delay)
                body = json.dumps(content).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
            def log_message(self, *args): pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        return f'http://127.0.0.1:{http.server_port}/v1', captured

    def test_protocol_request_and_valid_json_response(self):
        value = {'chapters': [{'title': '要点', 'summary': '课堂内容', 'cueIds': ['c00001']}]}
        url, captured = self.mock_server(content={'choices': [{'message': {'content': json.dumps(value)}}]})
        result = OpenAICompatibleSummarizer(url, 'test-key', 'test-model').summarize(parse_replay(payload()))
        self.assertEqual(result, value)
        self.assertEqual(captured[0]['path'], '/v1/chat/completions')
        self.assertEqual(captured[0]['body']['model'], 'test-model')
        self.assertIn('c00001', captured[0]['body']['messages'][1]['content'])

    def test_http_errors_are_sanitized_and_not_automatically_retried(self):
        url, captured = self.mock_server(status=429, content={'error': 'SECRET'})
        with self.assertRaises(GenerationError) as error:
            OpenAICompatibleSummarizer(url, 'test-key', 'test-model').summarize(parse_replay(payload()))
        self.assertEqual(error.exception.code, 'AI_HTTP_ERROR')
        self.assertNotIn('SECRET', str(error.exception))
        self.assertEqual(len(captured), 1)

    def test_invalid_json_and_timeout_have_explicit_failure(self):
        url, _ = self.mock_server(content={'choices': [{'message': {'content': 'not json'}}]})
        with self.assertRaises(GenerationError) as invalid:
            OpenAICompatibleSummarizer(url, 'key', 'model').summarize(parse_replay(payload()))
        self.assertEqual(invalid.exception.code, 'AI_INVALID_OUTPUT')
        url, _ = self.mock_server(delay=1.2)
        with self.assertRaises(GenerationError) as timeout:
            OpenAICompatibleSummarizer(url, 'key', 'model', timeout=1).summarize(parse_replay(payload()))
        self.assertEqual(timeout.exception.code, 'AI_TIMEOUT')

    def test_config_and_length_limit(self):
        with patch.dict('os.environ', {}, clear=True): self.assertIsNone(OpenAICompatibleSummarizer.from_environment())
        with patch.dict('os.environ', {'KEJI_AI_BASE_URL': 'http://remote.example/v1', 'KEJI_AI_API_KEY': 'x', 'KEJI_AI_MODEL': 'm'}, clear=True):
            with self.assertRaises(ValueError): OpenAICompatibleSummarizer.from_environment()
        replay = parse_replay(payload())
        replay['cues'] = [{'id': 'c00001', 'text': 'a' * 60001}]
        with self.assertRaises(GenerationError) as error: OpenAICompatibleSummarizer('http://localhost/v1', 'k', 'm').summarize(replay)
        self.assertEqual(error.exception.code, 'AI_INPUT_LIMIT')
