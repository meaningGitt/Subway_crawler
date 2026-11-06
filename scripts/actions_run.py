"""Use the workflow creation timestamp, not delayed runner start, for overlap detection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

repo = os.environ['GITHUB_REPOSITORY']
run_id = os.environ['GITHUB_RUN_ID']
request = urllib.request.Request(
    f'https://api.github.com/repos/{repo}/actions/runs/{run_id}',
    headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
with urllib.request.urlopen(request, timeout=30) as response:
    created_at = json.load(response)['created_at']
slot = {'0 0 * * *': '0900', '0 13 * * *': '2200'}.get(os.getenv('SCHEDULE', ''), 'manual')
command = [sys.executable, '-m', 'subway_crawler', 'run', '--root', '../state/runtime', '--slot', slot]
# Rerun is intentional recovery, so it must not be mistaken for an old queued run.
if os.environ.get('GITHUB_RUN_ATTEMPT', '1') == '1':
    command += ['--scheduled-at', created_at]
if os.getenv('RETRY_RUN'):
    command += ['--retry-run', os.environ['RETRY_RUN']]
if os.getenv('FORCE_REFRESH') == 'true':
    command += ['--force']
code = subprocess.call(command)
reports = sorted(Path('../state/runtime/reports/daily').glob('*.md'), key=lambda p: p.stat().st_mtime)
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write(f'Asia/Seoul / slot {slot} / event {created_at} / exit {code}\n\n')
    if reports:
        summary.write(reports[-1].read_text())
raise SystemExit(code)
