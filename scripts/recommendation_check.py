"""Probe recommendation metadata on the existing KST schedule; never persist large raw in Git."""
import json
import os
from pathlib import Path
import subprocess
import sys

root=Path('../state/runtime/recommendation-metadata')
code=subprocess.call([sys.executable,'-m','subway_crawler.recommendation','check','--root',str(root)])
runs=sorted((root/'runs').glob('*.json'),key=lambda p:p.stat().st_mtime)
if runs:
    run=json.loads(runs[-1].read_text())
    lines=['\n## 추천 데이터 출처 갱신 확인\n',
           '원본·분석의 영구 외부 저장소는 미설정입니다. 이 단계는 메타데이터 확인이며 실제 데이터 수집 완료를 의미하지 않습니다.\n',
           '| 출처 | 확인 결과 | 변경 여부 |','|---|---|---|']
    for r in run.get('sources',[]):
        lines.append(f"| {r['source_id']} | {r['status']} | {r.get('changed','미확인')} |")
    with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:f.write('\n'.join(lines)+'\n')
    # Catalog pages can contain dynamic session values. Keep only hashes in Git; full evidence is local/artifact.
    import shutil
    shutil.rmtree(root/'evidence',ignore_errors=True)
raise SystemExit(code)
