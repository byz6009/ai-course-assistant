"""Explicitly import synthetic demo data through the running local HTTP API."""
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='导入合成示例；不创建学习任务，不覆盖用户资料')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    base = f'http://127.0.0.1:{args.port}'

    def request(path, body=None):
        req = Request(base + path, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    bundle = json.loads((ROOT / 'examples/stage3-course.json').read_text(encoding='utf-8'))
    request('/api/courses', bundle['course'])
    request('/api/imports', bundle)
    replay = request('/api/replays', {'courseId': bundle['course']['id'],
        'title': '需求、设计与测试的可追溯关系（合成示例）', 'filename': 'replay-demo.srt',
        'content': (ROOT / 'examples/replay-demo.srt').read_text(encoding='utf-8')})
    detail = request('/api/replays/' + replay['id'])
    if not detail['result']:
        job = request(f'/api/replays/{replay["id"]}/jobs', {'provider': 'local'})
        for _ in range(100):
            state = request('/api/jobs/' + job['id'])
            if state['status'] == 'succeeded': break
            if state['status'] == 'failed': raise SystemExit(state['error'])
            time.sleep(0.1)
        else: raise SystemExit('任务仍在处理，请在页面查看。')
    print(f'合成示例已就绪：{base}/replay?id={replay["id"]}')
    print('学习任务尚未创建，请在页面核对摘要后确认。')


if __name__ == '__main__':
    main()
