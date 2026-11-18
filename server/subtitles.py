"""Parse immutable SRT/WebVTT sources; timestamps always come from the file."""
from html import unescape
import hashlib
import json
import re

from server.domain import ValidationError, source_of, text

MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
MAX_CUES = 10000
STAMP = r'(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}'
TIMING = re.compile(rf'^({STAMP})\s+-->\s+({STAMP})(?:\s+[^\r\n]*)?$')


def seconds(value):
    parts = value.replace(',', '.').split(':')
    hours, minutes, sec = ([0] + parts) if len(parts) == 2 else parts
    if int(minutes) > 59 or float(sec) >= 60:
        raise ValidationError('字幕包含无效的分或秒')
    return round(int(hours) * 3600 + int(minutes) * 60 + float(sec), 3)


def parse_subtitles(content, filename):
    if not isinstance(content, str) or not content.strip():
        raise ValidationError('字幕不能为空')
    if len(content.encode('utf-8')) > MAX_SUBTITLE_BYTES:
        raise ValidationError('字幕文件不能超过 5 MiB')
    if not filename.lower().endswith(('.srt', '.vtt')):
        raise ValidationError('请上传 UTF-8 编码的 SRT 或 VTT 字幕；音视频转写尚未接入')
    normalized = content.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    blocks = re.split(r'\n[ \t]*\n', normalized)
    vtt = filename.lower().endswith('.vtt')
    if vtt:
        if not re.match(r'^WEBVTT(?:[ \t].*)?(?:\n|$)', blocks[0]):
            raise ValidationError('VTT 文件必须以 WEBVTT 开头，并用空行分隔字幕')
        blocks = blocks[1:]
    cues = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        if vtt and re.match(r'^(NOTE(?:[ \t]|$)|STYLE$|REGION$)', lines[0]):
            continue
        index = 0 if '-->' in lines[0] else 1
        match = TIMING.fullmatch(lines[index].strip()) if len(lines) > index else None
        if not match or len(lines) <= index + 1:
            raise ValidationError(f'第 {len(cues) + 1} 段字幕的时间或正文无效，请检查空行分段')
        start, end = seconds(match[1]), seconds(match[2])
        if not 0 <= start < end <= 86400 or (cues and start < cues[-1]['start']):
            raise ValidationError('字幕时间须递增、结束晚于开始，且在 24 小时内')
        cue_text = unescape(re.sub(r'<[^>]*>', '', '\n'.join(lines[index + 1:]))).strip()
        if not cue_text or len(cue_text) > 10000 or '\x00' in cue_text:
            raise ValidationError('字幕正文为空、含无效字符或单段超过 10000 字')
        cues.append({'id': f'c{len(cues) + 1:05d}', 'start': start, 'end': end, 'text': cue_text})
        if len(cues) > MAX_CUES:
            raise ValidationError('字幕最多支持 10000 段，请按课次拆分')
    if not cues:
        raise ValidationError('文件中没有可解析的字幕')
    return cues


def parse_replay(body):
    if not isinstance(body, dict):
        raise ValidationError('回放导入必须为 JSON 对象')
    course_id = text(body, 'courseId', 80)
    title = text(body, 'title', 200)
    filename = text(body, 'filename', 200).replace('\\', '/').split('/')[-1]
    cues = parse_subtitles(body.get('content'), filename)
    duration = body.get('durationSeconds')
    last = max(cue['end'] for cue in cues)
    if duration is not None and (type(duration) not in (int, float) or not last <= duration <= 86400):
        raise ValidationError('回放时长必须不少于最后字幕结束时间，且不超过 24 小时')
    source_url = text(body, 'sourceUrl', 2000, optional=True)
    if source_url:
        source_url = source_of({'source': {'title': title, 'url': source_url}})['url']
    encoded = json.dumps([course_id, cues], ensure_ascii=False, sort_keys=True)
    replay_id = hashlib.sha256(encoded.encode()).hexdigest()[:24]
    return {'id': replay_id, 'courseId': course_id, 'title': title, 'filename': filename,
            'sourceUrl': source_url, 'duration': duration or last,
            'durationFromSubtitles': duration is None, 'cues': cues}
