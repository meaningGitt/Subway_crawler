from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import traceback
import uuid

from ..pipeline import now
from ..source import SourceError, encoded
from ..store import atomic_write
from .sources import AcquisitionError, acquire

REPO = Path(__file__).resolve().parents[2]


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else (default if default is not None else {})


def process(root, registry, run, inventory, force=False, as_of=None):
    from .analysis import Builder, Mapper
    from .report import generate
    states={s['id']:read(root/'state'/f"{s['id']}.json") for s in registry}
    code_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()
    from .holidays import Calendar
    as_of=as_of or now()[:10]
    config_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((REPO/'config').glob('*.json')))).hexdigest()
    fingerprint={'as_of':as_of,'config_hash':config_hash,'calendar':Calendar(root).metadata['sha256'],'code':code_hash,'registry':registry,'inventory_sha256':hashlib.sha256(inventory.read_bytes()).hexdigest(),
                 'raw':{sid:s.get('receipt',{}).get('sha256') for sid,s in states.items()}}
    snapshot_id=hashlib.sha256(encoded(fingerprint).encode()).hexdigest()[:20]
    out=root/'snapshots'/snapshot_id
    cached=read(out/'manifest.json')
    if cached.get('complete') and not force:
        run['snapshot']=str(out);run['processing']='unchanged_snapshot_reused'
        for s in run['sources']:
            s.update(cached.get('processing',{}).get(s['source_id'],{}))
        return out
    out.mkdir(parents=True,exist_ok=True)
    b=Builder(out,Mapper(inventory),registry,states)
    b.as_of=as_of
    for source in registry:
        sid=source['id'];state=states[sid]
        record=next((r for r in run['sources'] if r['source_id']==sid),None)
        if record is None:
            record={'source_id':sid,'adapter':source['adapter'],'status':state.get('status',source.get('status','미확보')),
                    'receipt':state.get('receipt',{}),'reason':source.get('reason','')};run['sources'].append(record)
        if source['adapter']=='review':
            record.update(processing_status='not_authorized',reason=source['reason']);continue
        if not state.get('raw_path'):
            record.update(processing_status='unavailable');continue
        raw=root/state['raw_path']
        before=set(b.datasets)
        try:
            if hashlib.sha256(raw.read_bytes()).hexdigest()!=state['receipt']['sha256']:
                raise SourceError('raw hash mismatch; use --force to reacquire')
            print('processing',sid,flush=True)
            method='ridership' if source['kind']=='ridership_types' else source['kind']
            getattr(b,method)(raw,sid)
            record.update(processing_status='success',processed_raw_sha256=state['receipt']['sha256'])
        except (Exception,) as exc:
            # Keep raw evidence and other sources. Failed intermediate files are not promoted.
            for name in set(b.datasets)-before:
                b.datasets.pop(name)
                for ext in ('.csv','.parquet'):
                    p=out/'datasets'/(name+ext)
                    if p.exists():
                        q=out/'failed'/sid/p.name;q.parent.mkdir(parents=True,exist_ok=True);p.replace(q)
            record.update(processing_status='failed',error_category='processing',error=f'{type(exc).__name__}: {exc}')
            atomic_write(out/'failed'/sid/'traceback.txt',traceback.format_exc())
            print('processing failed',sid,type(exc).__name__,str(exc),flush=True)
    from .segment_v2 import update_service_flags
    from .trips import build_trip_features
    from .calibration import build_calibration
    run['derived']=[]
    for task in (update_service_flags,build_trip_features,build_calibration):
        before=set(b.datasets)
        try:
            task(b);run['derived'].append({'task':task.__name__,'status':'success'})
        except Exception as exc:
            for name in set(b.datasets)-before:
                b.datasets.pop(name)
                for ext in ('.csv','.parquet'):
                    file=out/'datasets'/(name+ext)
                    if file.exists():
                        target=out/'failed'/task.__name__/file.name;target.parent.mkdir(parents=True,exist_ok=True);file.replace(target)
            run['derived'].append({'task':task.__name__,'status':'failed','error':f'{type(exc).__name__}: {exc}'})
            b.quality[task.__name__]={'status':'failed','error':str(exc)}
            atomic_write(out/'failed'/task.__name__/'traceback.txt',traceback.format_exc())
    from .v2_report import prepare
    prepare(b,root)
    run['ended_at']=now();run['snapshot']=str(out)
    atomic_write(out/'run.json',json.dumps(run,ensure_ascii=False,default=str))
    generate(b,run)
    atomic_write(out/'report_context.json',json.dumps(b.context,ensure_ascii=False,default=str))
    manifest={'schema_version':'seat-evidence-v2','snapshot_id':snapshot_id,'created_at':now(),'complete':all(r['status']=='success' for r in run['derived']) and all(r.get('processing_status')=='success' for r in run['sources'] if r.get('adapter')!='review'),
              'fingerprint':fingerprint,'datasets':b.datasets,
              'processing':{r['source_id']:{k:r[k] for k in ('processing_status','processed_raw_sha256','error') if k in r} for r in run['sources']},
              'lineage':'source_id -> source URL + raw_sha256 -> source_row/natural-key group -> transform_version/code hash'}
    atomic_write(out/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2,default=str))
    # New snapshots never delete older observations. Same fingerprint never appends twice.
    index=read(root/'snapshots'/'index.json',[])
    if snapshot_id not in index:index.append(snapshot_id)
    atomic_write(root/'snapshots'/'index.json',encoded(index))
    atomic_write(root/'latest.json',encoded({'snapshot':snapshot_id,'path':str(out),'complete':manifest['complete']}))
    return out


def export_current(root,dataset,destination):
    """Resolve revisions by natural key; keep old periods when the latest release is partial."""
    import pandas as pd
    frames=[];keys=None
    index=read(root/'snapshots'/'index.json',[])
    latest=read(root/'snapshots'/index[-1]/'manifest.json') if index else {}
    version=latest.get('schema_version','seat-evidence-v1')
    for snap in read(root/'snapshots'/'index.json',[]):
        folder=root/'snapshots'/snap;manifest=read(folder/'manifest.json');info=manifest.get('datasets',{}).get(dataset)
        if not info or manifest.get('schema_version','seat-evidence-v1')!=version:continue
        if not info.get('keys'):raise ValueError('dataset has no validated natural key')
        if keys and keys!=info['keys']:raise ValueError('natural key changed; migration required')
        keys=info['keys'];frame=pd.read_parquet(folder/'datasets'/(dataset+'.parquet'))
        frames.append(frame)
    if not frames:raise ValueError('unknown or unavailable dataset')
    result=pd.concat(frames,ignore_index=True).drop_duplicates(keys,keep='last')
    destination.parent.mkdir(parents=True,exist_ok=True)
    tmp=destination.with_suffix('.tmp')
    result.to_csv(tmp,index=False,encoding='utf-8-sig');os.replace(tmp,destination)
    print(json.dumps({'dataset':dataset,'current_records':len(result),'csv':str(destination)},ensure_ascii=False))


def run_failed(run):
    return any(r.get('adapter')!='review' and (r.get('status') in ('수집 불가','추가 권한 필요') or r.get('processing_status') in ('failed','unavailable')) for r in run['sources']) or any(r.get('status')=='failed' for r in run.get('derived',[]))


def main():
    parser=argparse.ArgumentParser(description='Official seat-recommendation evidence collection and analysis')
    parser.add_argument('command',choices=['run','process','check','export','query'])
    parser.add_argument('--root',type=Path,default=REPO.parent/'seat-data')
    parser.add_argument('--inventory',type=Path,default=REPO/'runtime/exports/kric_station_inventory.csv')
    parser.add_argument('--config',type=Path,default=REPO/'config/recommendation_sources.json')
    parser.add_argument('--force',action='store_true')
    parser.add_argument('--as-of',help='YYYY-MM-DD 기준 stale 판정과 스냅샷 재현')
    parser.add_argument('--source',action='append')
    parser.add_argument('--retry-run')
    parser.add_argument('--dataset');parser.add_argument('--output',type=Path)
    for name in ('boarding-station','alighting-station','line','direction','day-type','from-time','to-time'):parser.add_argument('--'+name)
    args=parser.parse_args();root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
    if args.command!='check' and root.is_relative_to(REPO):
        parser.error('large-data root must be outside the Git checkout')
    if args.command=='query':
        if not all([args.boarding_station,args.alighting_station,args.line,args.output]):parser.error('query requires --boarding-station --alighting-station --line --output')
        import pandas as pd
        from .trips import query_trips
        latest=read(root/'latest.json');folder=Path(latest.get('path',''))
        file=folder/'datasets/trip_stop_congestion.parquet'
        if not file.exists():parser.error('run v2 process before query')
        query={'id':'user_query','line':args.line,'boarding_station':args.boarding_station,'alighting_station':args.alighting_station,
               'day_type':args.day_type or '평일','from_time':args.from_time or '00:00','to_time':args.to_time or '29:59','direction':args.direction}
        result=query_trips(pd.read_parquet(file),query);args.output.parent.mkdir(parents=True,exist_ok=True)
        result.to_csv(args.output,index=False,encoding='utf-8-sig');print(json.dumps({'rows':len(result),'output':str(args.output),'query':query},ensure_ascii=False));return 0
    if args.command=='export':
        if not args.dataset or not args.output:parser.error('export requires --dataset and --output')
        export_current(root,args.dataset,args.output.resolve());return 0
    lock=(root/'.seat-collection.lock').open('a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        atomic_write(root/'runs'/f'skipped-{uuid.uuid4().hex}.json',encoded({'status':'skipped_overlap','at':now()}))
        print('skipped_overlap');lock.close();return 0
    sources=read(args.config)['sources'];selected=args.source
    if args.retry_run:
        previous=read(root/'runs'/(args.retry_run+'.json'))
        if not previous:parser.error('unknown retry run')
        selected=[s['source_id'] for s in previous['sources'] if s.get('status')!='수집 완료' or s.get('processing_status')=='failed']
    if selected and set(selected)-{s['id'] for s in sources}:parser.error('unknown source')
    running=root/'running.json'
    if running.exists():
        previous=read(running);previous.update(status='interrupted',ended_at=now())
        atomic_write(root/'runs'/(previous['id']+'.json'),encoded(previous))
    run={'id':uuid.uuid4().hex[:12],'started_at':now(),'status':'running','mode':args.command,'sources':[]}
    atomic_write(running,encoded(run))
    def interrupted(signum,frame):raise KeyboardInterrupt(f'signal {signum}')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,interrupted)
    try:
        if args.command in ('run','check'):
            from .holidays import refresh_calendar
            run['holiday_acquisition']=refresh_calendar(root)
            for source in sources:
                if selected is not None and source['id'] not in selected:continue
                try:
                    result=acquire(source,root,check_only=args.command=='check',force=args.force)
                except Exception as exc:
                    result={'source_id':source['id'],'status':'추가 권한 필요' if getattr(exc,'category','')=='user_action' else '수집 불가',
                            'error_category':getattr(exc,'category','retryable' if isinstance(exc,SourceError) else 'schema'),
                            'error':str(exc),'checked_at':now()}
                result['adapter']=source['adapter'];run['sources'].append(result)
                print(source['id'],result['status'],flush=True);atomic_write(running,encoded(run))
        if args.command in ('run','process'):
            out=process(root,sources,run,args.inventory.resolve(),args.force,args.as_of)
            print('report',str(out/'report.html'),flush=True)
        run['status']='partial' if run_failed(run) else 'finished_with_documented_gaps'
    except BaseException as exc:
        run.update(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        run['ended_at']=now();atomic_write(root/'runs'/(run['id']+'.json'),encoded(run))
        running.unlink(missing_ok=True);lock.close()
    return 1 if run['status']=='partial' else 0


if __name__=='__main__':sys.exit(main())
