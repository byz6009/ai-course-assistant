"""Package the built local app without databases, credentials or development caches."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
output = ROOT.parent / '课迹第三阶段可运行版.zip'
if not (ROOT / 'dist/index.html').is_file():
    raise SystemExit('Run npm run build first')
folders = ['dist', 'server', 'browser-extension', 'examples', 'docs/stage3']
files = ['README.md', '.env.example', 'start-keji.cmd', 'tools/start.ps1', 'tools/seed_demo.py', 'tools/backup.py']
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for folder in folders:
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                archive.write(path, 'keji-stage3/' + path.relative_to(ROOT).as_posix())
    for name in files:
        archive.write(ROOT / name, 'keji-stage3/' + name)
print(output)
