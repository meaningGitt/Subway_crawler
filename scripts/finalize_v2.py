"""Validate the latest immutable datasets and refresh their report with test evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from subway_crawler.recommendation.analysis import Mapper
from subway_crawler.recommendation.report import generate
from subway_crawler.store import atomic_write

p=argparse.ArgumentParser()
p.add_argument('--root',type=Path,required=True)
args=p.parse_args()
root=args.root.resolve()
latest=json.loads((root/'latest.json').read_text())
snapshot=root/'snapshots'/latest['snapshot']
result=subprocess.run([sys.executable,str(REPO/'scripts/validate_v2.py'),'--root',str(root),'--snapshot',str(snapshot)])
manifest=json.loads((snapshot/'manifest.json').read_text())
quality=json.loads((snapshot/'quality.json').read_text())
context=json.loads((snapshot/'report_context.json').read_text())
context['v2_tests']=json.loads((root/'v2_test_results.json').read_text())
mapper=Mapper(snapshot/'station_reference.csv')
import pandas as pd
mapper.audit=pd.read_csv(snapshot/'station_mapping.csv').fillna('').to_dict('records')
builder=SimpleNamespace(out=snapshot,datasets=manifest['datasets'],quality=quality['sources'],context=context,mapper=mapper)
generate(builder,json.loads((snapshot/'run.json').read_text()))
atomic_write(snapshot/'report_context.json',json.dumps(context,ensure_ascii=False,default=str))
atomic_write(snapshot/'test_results.json',json.dumps(context['v2_tests'],ensure_ascii=False,indent=2))
atomic_write(snapshot/'report_generation.json',json.dumps({'snapshot':snapshot.name,'data_transform_code_sha256':manifest['fingerprint']['code'],'report_module_sha256':hashlib.sha256((REPO/'subway_crawler/recommendation/report.py').read_bytes()).hexdigest(),'finalizer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'dataset_mutations':False},indent=2))
print('validated report',snapshot/'report.html')
raise SystemExit(result.returncode)
