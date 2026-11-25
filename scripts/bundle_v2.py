"""Bundle actual CSV outputs, reports and research receipts without copying raw into Git."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
root=a.root.resolve();snapshot=root/'snapshots'/json.loads((root/'latest.json').read_text())['snapshot']
files=list((snapshot/'datasets').glob('*.csv'))+[f for f in snapshot.iterdir() if f.is_file()]
research=root/'research/v2'
if research.exists():files.extend(f for f in research.rglob('*') if f.is_file())
a.output.parent.mkdir(parents=True,exist_ok=True)
manifest=[]
with zipfile.ZipFile(a.output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    for f in sorted(files):
        relative=f.relative_to(root)
        archive.write(f,str(relative));manifest.append({'path':str(relative),'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()})
    archive.writestr('BUNDLE_MANIFEST.json',json.dumps({'snapshot':snapshot.name,'contents':'all exported CSVs, reports and research raw; production raw/Parquet remain in seat-data or full Actions artifact','files':manifest},ensure_ascii=False,indent=2))
with zipfile.ZipFile(a.output) as archive:assert archive.testzip() is None
print(json.dumps({'zip':str(a.output.resolve()),'bytes':a.output.stat().st_size,'files':len(files)},ensure_ascii=False))
