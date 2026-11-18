"""Server-owned AI settings. Secrets never appear in status responses or job records."""
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import urlsplit

from server.domain import ValidationError, text
from server.replays import ConflictError, QueueFullError
from server.summarizers import OpenAICompatibleSummarizer, normalize_base_url, validate_summary


class AISettings:
    def __init__(self, path, service):
        self.path, self.service = Path(path), service
        self.lock = threading.RLock()
        self.probes = threading.BoundedSemaphore(2)
        self.revision = secrets.token_hex(12)
        self.config, self.source, self.error = None, 'none', ''
        self._load()

    @staticmethod
    def _environment():
        values = [os.environ.get(key, '').strip() for key in
                  ('KEJI_AI_BASE_URL', 'KEJI_AI_API_KEY', 'KEJI_AI_MODEL')]
        if not all(values):
            return None
        return {'baseUrl': values[0], 'apiKey': values[1], 'model': values[2],
                'timeout': 90, 'jsonMode': True}

    def _load(self):
        try:
            if self.path.exists():
                if self.path.stat().st_size > 32768:
                    raise ValueError()
                stored = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(stored, dict) or stored.get('version') != 1 or type(stored.get('enabled')) is not bool:
                    raise ValueError()
                self.source = 'local' if stored['enabled'] else 'disabled'
                if stored['enabled']:
                    self.config = self._candidate(stored)
            elif self._environment():
                self.source = 'environment'
                self.config = self._candidate(self._environment())
        except (OSError, ValueError):
            # A corrupt config must not stop local summaries or silently select a different service.
            self.config, self.source = None, 'invalid'
            self.error = '原 AI 配置无效或无法读取，请重新填写保存；本地原文摘录仍可使用。'
        self.service.configure_ai(self._provider(self.config) if self.config else None)

    def status(self):
        with self.lock:
            config = self.config or {}
            return {'enabled': bool(self.config), 'source': self.source, 'revision': self.revision,
                    'baseUrl': config.get('baseUrl', ''), 'model': config.get('model', ''),
                    'timeout': config.get('timeout', 90), 'jsonMode': config.get('jsonMode', True),
                    'hasApiKey': bool(config.get('apiKey')), 'hasEnvironment': bool(self._environment()),
                    'error': self.error}

    def _check_revision(self, body):
        if not isinstance(body, dict):
            raise ValidationError('AI 设置必须为 JSON 对象')
        if body.get('revision') != self.revision:
            raise ConflictError('AI 设置已变化，请重新载入设置后再操作。')

    def _candidate(self, body, need_model=True):
        if not isinstance(body, dict):
            raise ValidationError('AI 设置必须为 JSON 对象')
        base_url = normalize_base_url(body.get('baseUrl'))
        key = text(body, 'apiKey', 4096, optional=True)
        if any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValidationError('API Key 不能包含空格、换行或非 ASCII 字符')
        if not key and self.config and self.config['baseUrl'] == base_url:
            key = self.config['apiKey']
        if not key and urlsplit(base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValidationError('请填写此服务的 API Key；更换 API 地址时需要重新输入密钥')
        model = text(body, 'model', 200, optional=not need_model)
        if any(c.isspace() or ord(c) < 32 for c in model):
            raise ValidationError('模型 ID 不能包含空格或换行，请从服务商控制台复制')
        timeout = body.get('timeout', 90)
        if type(timeout) is not int or not 5 <= timeout <= 90:
            raise ValidationError('请求超时必须为 5 至 90 秒的整数')
        json_mode = body.get('jsonMode', True)
        if type(json_mode) is not bool:
            raise ValidationError('JSON 模式必须为布尔值')
        return {'baseUrl': base_url, 'apiKey': key, 'model': model, 'timeout': timeout, 'jsonMode': json_mode}

    @staticmethod
    def _provider(config, probe=False):
        return OpenAICompatibleSummarizer(config['baseUrl'], config['apiKey'], config['model'],
                                         min(config['timeout'], 20) if probe else config['timeout'], config['jsonMode'])

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.ai-settings-', suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump(value, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def update(self, body):
        with self.lock:
            self._check_revision(body)
            action = body.get('action', 'save')
            if action == 'save':
                config, source = self._candidate(body), 'local'
                self._write(dict(config, version=1, enabled=True))
            elif action == 'disable':
                config, source = None, 'disabled'
                # Tombstone prevents inherited environment settings from unexpectedly re-enabling AI.
                self._write({'version': 1, 'enabled': False})
            elif action == 'environment':
                environment = self._environment()
                config = self._candidate(environment) if environment else None
                source = 'environment' if config else 'none'
                self.path.unlink(missing_ok=True)
            else:
                raise ValidationError('未知的 AI 设置操作')
            self.config, self.source, self.error = config, source, ''
            self.service.configure_ai(self._provider(config) if config else None)
            self.revision = secrets.token_hex(12)
            return self.status()

    def probe(self, body, models=False):
        with self.lock:
            self._check_revision(body)
            config = self._candidate(body, need_model=not models)
        if not self.probes.acquire(blocking=False):
            raise QueueFullError('已有连接测试正在进行，请稍后再试。')
        try:
            provider = self._provider(config, probe=True)
            if models:
                return {'models': provider.list_models(), 'baseUrl': config['baseUrl']}
            # Exercise the actual generation + citation validation path using synthetic data only.
            cues = [{'id': 'c00001', 'start': 0, 'end': 10, 'text': '需求分析先识别用户目标，再明确系统边界。'},
                    {'id': 'c00002', 'start': 10, 'end': 20, 'text': '验收测试需要覆盖正常情况与失败情况。'}]
            started = time.monotonic()
            result = validate_summary(provider.summarize({'cues': cues}), cues)
            return {'ok': True, 'model': config['model'], 'baseUrl': config['baseUrl'],
                    'latencyMs': round((time.monotonic() - started) * 1000),
                    'chapterCount': len(result['chapters']), 'sample': result['chapters'][0],
                    'message': '连接、JSON 摘要和字幕引用校验通过。测试未保存配置，也未创建课程或学习任务。'}
        finally:
            self.probes.release()
