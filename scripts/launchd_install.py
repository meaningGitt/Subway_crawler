"""Generate a launchd plist; install explicitly with launchctl bootstrap."""
import argparse
from pathlib import Path
import plistlib
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--output', default='deploy/kr.subway.collector.plist')
p.add_argument('--python', default=str(Path('.venv/bin/python').absolute()))
a = p.parse_args()
zone = subprocess.check_output(['readlink', '/etc/localtime'], text=True).strip()
if not zone.endswith('/Asia/Seoul'):
    raise SystemExit('launchd calendar follows the macOS system timezone. Set macOS timezone to Asia/Seoul first.')
root = Path.cwd()
(root / 'runtime').mkdir(exist_ok=True)
plist = {'Label': 'kr.subway.collector', 'WorkingDirectory': str(root),
         'ProgramArguments': [a.python, '-m', 'subway_crawler', 'run'],
         'EnvironmentVariables': {'TZ': 'Asia/Seoul'},
         'StartCalendarInterval': [{'Hour': 9, 'Minute': 0}, {'Hour': 22, 'Minute': 0}],
         'StandardOutPath': str(root / 'runtime/launchd.stdout.log'),
         'StandardErrorPath': str(root / 'runtime/launchd.stderr.log')}
Path(a.output).write_bytes(plistlib.dumps(plist))
print(Path(a.output).resolve())
