"""Run the unchanged regression suite plus v2 tests and retain machine-readable evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO))
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--snapshot',type=Path);args=p.parse_args()
if args.snapshot:os.environ['SEAT_V2_SNAPSHOT']=str(args.snapshot.resolve())
class Result(unittest.TextTestResult):
    def __init__(self,*a,**kw):super().__init__(*a,**kw);self.passed=[]
    def addSuccess(self,test):super().addSuccess(test);self.passed.append(test.id())
r=unittest.TextTestRunner(verbosity=1,resultclass=Result).run(unittest.defaultTestLoader.discover(str(REPO/'tests')))
files=sorted((REPO/'subway_crawler/recommendation').glob('*.py'))+sorted((REPO/'tests').glob('*.py'))
report={'status':'passed' if r.wasSuccessful() else 'failed','run':r.testsRun,'passed_count':len(r.passed),'skipped':[(t.id(),why) for t,why in r.skipped],'passed_tests':r.passed,'failures':[t.id() for t,_ in r.failures+r.errors], 'code_and_test_sha256':hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()}
args.root.mkdir(parents=True,exist_ok=True);(args.root/'v2_test_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
raise SystemExit(0 if r.wasSuccessful() else 1)
