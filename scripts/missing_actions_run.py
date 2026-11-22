"""Existing Actions entry point for missing supplements, with queued-run suppression."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

root = Path('../state/missing-data')
baseline = root / 'baseline.json'
if not baseline.is_file():
    raise SystemExit('NOT_VERIFIED: prepare and persist the read-only baseline inventory first')
request = urllib.request.Request(
    f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
with urllib.request.urlopen(request, timeout=30) as response:
    created_at = json.load(response)['created_at']
command = [sys.executable, '-m', 'subway_crawler.missing_data.pipeline', '--out', str(root), '--baseline', str(baseline)]
if os.environ.get('GITHUB_RUN_ATTEMPT', '1') == '1':
    command += ['--event-created-at', created_at]
if os.getenv('RETRY_FAILED') == 'true':
    command += ['--retry-failed']
code = subprocess.call(command)
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
    output.write(f'Asia/Seoul / event {created_at} / exit {code}\n\n')
    report = root / 'reports/collection_summary.json'
    if report.is_file():
        summary = json.loads(report.read_text())
        output.write('Pipeline completion does not mean service readiness. See CSV quality report.\n\n')
        output.write('```json\n' + json.dumps(summary, ensure_ascii=False, indent=2) + '\n```\n')
raise SystemExit(code)
