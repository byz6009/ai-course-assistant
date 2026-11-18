"""Durable replay workflow: import -> queued job -> version -> confirmed task."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import threading
import uuid

from server.domain import ValidationError
from server.subtitles import parse_replay
from server.summarizers import ExtractiveSummarizer, GenerationError, validate_summary


def now():
    return datetime.now(timezone.utc).isoformat()


class ConflictError(Exception):
    pass


class QueueFullError(Exception):
    pass


class ReplayRepository:
    def __init__(self, courses):
        self.courses = courses
        with closing(courses.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS replays (
                    id TEXT PRIMARY KEY, course_id TEXT NOT NULL, data TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS replay_versions (
                    replay_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
                    provider TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(replay_id,version));
                CREATE TABLE IF NOT EXISTS replay_jobs (
                    id TEXT PRIMARY KEY, replay_id TEXT NOT NULL, provider TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, error_code TEXT, result_version INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_replay_job ON replay_jobs(replay_id)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS learning_tasks (
                    id TEXT PRIMARY KEY, replay_id TEXT NOT NULL UNIQUE, course_id TEXT NOT NULL,
                    data TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            ''')

    def import_replay(self, body):
        replay = parse_replay(body)
        with closing(self.courses.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM courses WHERE id=?', (replay['courseId'],)).fetchone():
                raise ValidationError('课程不存在，请先创建或导入课程')
            existing = db.execute('SELECT id FROM replays WHERE id=?', (replay['id'],)).fetchone()
            if not existing:
                db.execute('INSERT INTO replays VALUES (?,?,?,?)',
                           (replay['id'], replay['courseId'], json.dumps(replay, ensure_ascii=False), now()))
        return {'id': replay['id'], 'duplicate': bool(existing), 'cueCount': len(replay['cues'])}

    def list(self):
        with closing(self.courses.connect()) as db:
            rows = db.execute('SELECT * FROM replays ORDER BY created_at DESC,id').fetchall()
            result = []
            for row in rows:
                replay = json.loads(row['data'])
                replay['cueCount'] = len(replay.pop('cues'))
                replay['createdAt'] = row['created_at']
                replay['latestVersion'] = db.execute('SELECT COALESCE(MAX(version),0) FROM replay_versions WHERE replay_id=?', (row['id'],)).fetchone()[0]
                replay['job'] = self._last_job(db, row['id'])
                result.append(replay)
            return result

    def _last_job(self, db, replay_id):
        row = db.execute('SELECT * FROM replay_jobs WHERE replay_id=? ORDER BY rowid DESC LIMIT 1', (replay_id,)).fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _job(row):
        return {'id': row['id'], 'replayId': row['replay_id'], 'provider': row['provider'],
                'status': row['status'], 'error': row['error'], 'errorCode': row['error_code'],
                'resultVersion': row['result_version'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}

    def get(self, replay_id):
        with closing(self.courses.connect()) as db:
            row = db.execute('SELECT * FROM replays WHERE id=?', (replay_id,)).fetchone()
            if not row:
                raise KeyError('回放不存在')
            versions = [{'version': v['version'], 'provider': v['provider'], 'createdAt': v['created_at']}
                        for v in db.execute('SELECT version,provider,created_at FROM replay_versions WHERE replay_id=? ORDER BY version DESC', (replay_id,))]
            result = dict(json.loads(row['data']), createdAt=row['created_at'], versions=versions,
                          job=self._last_job(db, replay_id))
            result['result'] = self.version(replay_id, versions[0]['version']) if versions else None
            task = db.execute('SELECT id FROM learning_tasks WHERE replay_id=?', (replay_id,)).fetchone()
            result['taskId'] = task['id'] if task else None
            return result

    def version(self, replay_id, version):
        with closing(self.courses.connect()) as db:
            row = db.execute('SELECT * FROM replay_versions WHERE replay_id=? AND version=?', (replay_id, version)).fetchone()
            if not row:
                raise KeyError('摘要版本不存在')
            return dict(json.loads(row['data']), version=row['version'], provider=row['provider'], createdAt=row['created_at'])

    def recover_jobs(self):
        with closing(self.courses.connect()) as db, db:
            db.execute("UPDATE replay_jobs SET status='failed', error=?, error_code='INTERRUPTED', updated_at=? WHERE status IN ('queued','running')",
                       ('服务上次退出时任务未完成，请手动重试；已有摘要已保留。', now()))

    def create_job(self, replay_id, provider, capacity):
        with closing(self.courses.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM replays WHERE id=?', (replay_id,)).fetchone():
                raise KeyError('回放不存在')
            active = db.execute("SELECT * FROM replay_jobs WHERE replay_id=? AND status IN ('queued','running')", (replay_id,)).fetchone()
            if active:
                return self._job(active), False
            total = db.execute("SELECT COUNT(*) FROM replay_jobs WHERE status IN ('queued','running')").fetchone()[0]
            if total >= capacity:
                raise QueueFullError('处理队列已满，请等待现有任务完成后再试')
            job_id, timestamp = uuid.uuid4().hex, now()
            db.execute('INSERT INTO replay_jobs VALUES (?,?,?,?,?,?,?,?,?)',
                       (job_id, replay_id, provider, 'queued', None, None, None, timestamp, timestamp))
            row = db.execute('SELECT * FROM replay_jobs WHERE id=?', (job_id,)).fetchone()
            return self._job(row), True

    def job(self, job_id):
        with closing(self.courses.connect()) as db:
            row = db.execute('SELECT * FROM replay_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise KeyError('处理任务不存在')
            return self._job(row)

    def running(self, job_id):
        with closing(self.courses.connect()) as db, db:
            db.execute("UPDATE replay_jobs SET status='running',updated_at=? WHERE id=? AND status='queued'", (now(), job_id))

    def fail(self, job_id, code, message):
        with closing(self.courses.connect()) as db, db:
            db.execute("UPDATE replay_jobs SET status='failed',error=?,error_code=?,updated_at=? WHERE id=?", (message, code, now(), job_id))

    def save_result(self, replay_id, result, provider, job_id=None, expected_version=None):
        replay = self.get(replay_id)
        result = validate_summary(result, replay['cues'])
        with closing(self.courses.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT COALESCE(MAX(version),0) FROM replay_versions WHERE replay_id=?', (replay_id,)).fetchone()[0]
            if expected_version is not None and current != expected_version:
                raise ConflictError('摘要已有新版本，请先刷新再编辑；本次编辑尚未保存')
            version, timestamp = current + 1, now()
            db.execute('INSERT INTO replay_versions VALUES (?,?,?,?,?)', (replay_id, version, json.dumps(result, ensure_ascii=False), provider, timestamp))
            if job_id:
                db.execute("UPDATE replay_jobs SET status='succeeded',result_version=?,updated_at=? WHERE id=?", (version, timestamp, job_id))
        return self.version(replay_id, version)

    def confirm_task(self, replay_id, body):
        if not isinstance(body, dict) or body.get('confirmed') is not True:
            raise ValidationError('请核对摘要并明确确认后再加入学习任务')
        version = body.get('version')
        ids = body.get('chapterIds')
        if type(version) is not int or not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValidationError('请选择有效的摘要版本和至少一个知识点')
        replay, result = self.get(replay_id), self.version(replay_id, version)
        by_id = {chapter['id']: chapter for chapter in result['chapters']}
        if any(key not in by_id for key in ids):
            raise ValidationError('选择的知识点不属于此摘要版本')
        task_id = hashlib.sha256(('replay-task:' + replay_id).encode()).hexdigest()[:24]
        task = {'id': task_id, 'replayId': replay_id, 'courseId': replay['courseId'],
                'title': '复习：' + replay['title'], 'version': version,
                'chapters': [by_id[key] for key in dict.fromkeys(ids)], 'sourceTitle': replay['filename']}
        with closing(self.courses.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            exists = db.execute('SELECT id FROM learning_tasks WHERE replay_id=?', (replay_id,)).fetchone()
            if not exists:
                db.execute('INSERT INTO learning_tasks VALUES (?,?,?,?,?,?)',
                           (task_id, replay_id, replay['courseId'], json.dumps(task, ensure_ascii=False), 0, now()))
        return {'id': task_id, 'duplicate': bool(exists)}

    def tasks(self):
        with closing(self.courses.connect()) as db:
            return [dict(json.loads(row['data']), done=bool(row['done']), createdAt=row['created_at'])
                    for row in db.execute('SELECT * FROM learning_tasks ORDER BY created_at DESC')]

    def set_task_done(self, task_id, done):
        with closing(self.courses.connect()) as db, db:
            cursor = db.execute('UPDATE learning_tasks SET done=? WHERE id=?', (int(done), task_id))
            if cursor.rowcount != 1:
                raise KeyError('学习任务不存在')
        return {'id': task_id, 'done': done}


class ReplayService:
    def __init__(self, repository, ai=None, workers=5, queue_size=20):
        self.repository = repository
        self.providers = {'local': ExtractiveSummarizer()}
        if ai:
            self.providers['ai'] = ai
        self.capacity = workers + queue_size
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='replay')
        self.lock = threading.Lock()
        repository.recover_jobs()

    def capabilities(self):
        with self.lock:
            return {'providers': [{'id': key, 'label': value.label} for key, value in self.providers.items()],
                    'aiConfigured': 'ai' in self.providers, 'subtitleFormats': ['srt', 'vtt'],
                    'maxSubtitleBytes': 5 * 1024 * 1024, 'audioTranscription': False}

    def configure_ai(self, provider):
        with self.lock:
            if provider:
                self.providers['ai'] = provider
            else:
                self.providers.pop('ai', None)

    def submit(self, replay_id, provider):
        with self.lock:
            if not isinstance(provider, str) or provider not in self.providers:
                raise ValidationError('所选摘要服务未配置，请使用本地原文摘录或打开 AI 服务设置')
            job, created = self.repository.create_job(replay_id, provider, self.capacity)
            if created:
                # Pin the provider when accepted; later configuration changes only affect new jobs.
                self.pool.submit(self._run, job, self.providers[provider])
        return dict(job, duplicate=not created)

    def _run(self, job, provider):
        try:
            self.repository.running(job['id'])
            replay = self.repository.get(job['replayId'])
            result = provider.summarize(replay)
            self.repository.save_result(job['replayId'], result, job['provider'], job_id=job['id'])
        except GenerationError as error:
            self.repository.fail(job['id'], error.code, str(error))
        except ValidationError as error:
            self.repository.fail(job['id'], 'INVALID_RESULT', str(error))
        except Exception:
            # Provider exceptions may contain credentials or source text: do not expose them.
            self.repository.fail(job['id'], 'PROCESSING_FAILED', '本次处理失败，旧版本已保留，请稍后重试。')

    def close(self):
        self.pool.shutdown(wait=True)
