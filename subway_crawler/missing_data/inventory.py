import csv
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def inspect_existing(root, pain_path):
    """Read actual files, never use a manifest row count as verification."""
    root=Path(root); rows=[]; manifests={}; snapshot=None
    try:
        snapshot=Path(json.loads((root/'latest.json').read_text())['path']).resolve()
        if not snapshot.is_relative_to(root.resolve()): raise ValueError('snapshot outside baseline')
        manifests=json.loads((snapshot/'manifest.json').read_text()).get('datasets',{})
    except (OSError,ValueError,KeyError) as e:
        rows.append(dict(dataset='baseline',path=str(root),status='NOT_VERIFIED',reason=type(e).__name__))
        snapshot=None
    if snapshot and snapshot.is_dir():
        for path in sorted((snapshot/'datasets').glob('*')):
            if path.suffix not in {'.csv','.parquet'}: continue
            entry=dict(dataset=path.stem,path=str(path),format=path.suffix,role='existing_read_only',status='NOT_VERIFIED')
            try:
                if path.suffix=='.parquet':
                    file=pq.ParquetFile(path); n=file.metadata.num_rows; columns=file.schema_arrow.names
                else:
                    with path.open(encoding='utf-8-sig',newline='') as f:
                        reader=csv.reader(f);columns=next(reader);n=sum(1 for _ in reader)
                entry.update(status='VERIFIED',rows=n,columns=columns,sha256=sha(path),bytes=path.stat().st_size)
                m=manifests.get(path.stem,{})
                entry.update(source_url=m.get('source_url'),manifest_dates=m.get('dates',[]),
                             manifest_line_rows=m.get('line_rows',{}),scope_note='CSV/Parquet representations overlap; do not sum')
            except (OSError,ValueError,UnicodeError) as e: entry['reason']=type(e).__name__
            rows.append(entry)
    # Capture auxiliary exports and raw-file presence without parsing archives again.
    for path in sorted(root.rglob('*')) if root.is_dir() else []:
        if not path.is_file() or (snapshot and path.is_relative_to(snapshot)): continue
        if path.suffix not in {'.csv','.parquet','.xlsx','.zip'}: continue
        try: rows.append(dict(dataset=path.stem,path=str(path),format=path.suffix,status='PRESENT_NOT_PROFILED',bytes=path.stat().st_size,role='archive_or_raw',reason='not added to current dataset counts'))
        except OSError: rows.append(dict(dataset=path.stem,path=str(path),status='NOT_VERIFIED'))
    urls=[]
    try:
        with Path(pain_path).open(encoding='utf-8-sig',newline='') as f: pain=list(csv.DictReader(f))
        urls=[r['source_url'] for r in pain]
        rows.append(dict(dataset='existing_user_pain_points',path=str(pain_path),status='VERIFIED',rows=len(pain),columns=list(pain[0]) if pain else [],sha256=sha(pain_path),role='existing_read_only'))
    except (OSError,KeyError,UnicodeError): rows.append(dict(dataset='existing_user_pain_points',path=str(pain_path),status='NOT_VERIFIED'))
    return rows,snapshot,urls


def unwrap_times(values):
    """HH:MM:SS to elapsed seconds; only observed midnight rollover is resolved."""
    result=[];offset=0;previous=None
    for text in values:
        try:
            bits=str(text).split(':')
            if len(bits)!=3: raise ValueError()
            h,m,s=map(int,bits)
            if not (0<=h<48 and 0<=m<60 and 0<=s<60): raise ValueError()
            sec=h*3600+m*60+s
            if previous is not None and sec+offset<previous-12*3600: offset+=86400
            value=sec+offset
            if previous is not None and value<previous: raise ValueError()
            result.append(value);previous=value
        except (ValueError,TypeError): result.append(None)
    return result


def derive_last_trains(path):
    cols=['trip_key','train_no','line','source_line_code','day_type','station_key','station_name',
          'origin_name','destination_name','direction','direction_path','stop_sequence','departure_time',
          'reference_date','effective_date','quality_valid','raw_sha256','source_id','source_row']
    frame=pd.read_csv(path,encoding='utf-8-sig',dtype=str,usecols=cols).fillna('')
    frame['seq']=pd.to_numeric(frame['stop_sequence'],errors='coerce')
    best={};rejected=[]
    for _,trip in frame.sort_values(['trip_key','seq']).groupby('trip_key',sort=False):
        records=trip.to_dict('records');times=unwrap_times([r['departure_time'] for r in records])
        for i,r in enumerate(records[:-1]):  # Terminal arrival is not a boarding departure.
            sec=times[i];nxt=records[i+1]
            if sec is None or r['quality_valid'].lower()!='true' or not r['station_key'] or not nxt['station_key']:
                rejected.append({'dataset':'derived_last_train','source_row':r['source_row'],'trip_key':r['trip_key'],'reason':'invalid_time_or_mapping_or_source_quality'})
                continue
            # Direction is an ordered adjacent station pair, not invented up/down.
            key=(r['source_line_code'],r['day_type'],r['station_key'],nxt['station_key'],r['destination_name'],r['reference_date'])
            issue=['effective_date_missing'] if not r['effective_date'] else []
            if sec<3*3600: issue.append('service_day_anchor_unknown')
            # No claim that a source snapshot is the current published timetable.
            issue.append('current_timetable_not_verified')
            row={k:r[k] for k in ['trip_key','train_no','line','source_line_code','day_type','station_key','station_name','origin_name','destination_name','reference_date','effective_date','raw_sha256','source_id']}
            row.update(direction=r['direction'],direction_path=r['direction_path'],next_station_key=nxt['station_key'],
                       next_station_name=nxt['station_name'],departure_time=r['departure_time'],service_seconds=sec,
                       calendar_day_offset=sec//86400 if sec>=3*3600 else None,is_short_turn=None,
                       derivation='latest departure per station/day_type/next_station/destination/reference_date in existing snapshot',
                       service_status='NOT_USABLE',issues=issue)
            if key not in best or sec>best[key]['service_seconds']: best[key]=row
    return list(best.values()),rejected
