"""Small, dependency-free research IO. Never store full pages or author profiles."""
import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote

def canonical_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('public HTTPS source URL required')
    query = sorted((k, v) for k, v in parse_qsl(p.query) if k in {'id', 'no', 'CNTN_CD', 'qstnNo', 'seq', 'articleNo', 'mode', 'section', 'bn', 'num', 'bo_table', 'wr_id', 'b'})
    path = quote(p.path, safe="/%:@!$&'()*+,;=-._~").rstrip('/')
    path = re.sub(r'%[0-9a-fA-F]{2}', lambda m: m.group().upper(), path)
    # The same Linkareer article appears under several category routes.
    # Keep the established honeytips URL, so existing evidence IDs remain stable.
    if p.hostname == 'community.linkareer.com':
        path = re.sub(r'^/job-materials/(\d+)$', r'/honeytips/\1', path)
        path = re.sub(r'^/mentor-employed/(\d+)$', r'/mentor-qna/\1', path)
    if p.hostname.endswith('.tistory.com'):
        path = re.sub(r'^/m/(?=\d+$|entry/)', '/', path)
    if p.hostname == 'www.82cook.com' and path == '/entiz/read.php':
        query = [(k,v) for k,v in query if k == 'num']
    if p.hostname == 'orbi.kr':
        m = re.match(r'/\d+', path)
        if m: path = m.group()
    return urlunsplit(('https', p.netloc.lower(), path, urlencode(query), ''))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def atomic_text(path, text):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def write_json(path, rows):
    atomic_text(path, json.dumps(rows, ensure_ascii=False, indent=2) + '\n')

def write_csv(path, rows, fields=None):
    import io
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=fields, extrasaction='raise')
    w.writeheader()
    for r in rows:
        clean = {}
        for k, v in r.items():
            if isinstance(v, (list, dict)): v = json.dumps(v, ensure_ascii=False)
            if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')): v = "'" + v
            clean[k] = v
        w.writerow(clean)
    atomic_text(path, '\ufeff' + buf.getvalue())

def contains_contact(text):
    return bool(re.search(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\b01[016789][ -]?\d{3,4}[ -]?\d{4}\b|/u/|u/[A-Za-z0-9_]', text))

class RunLock:
    """Local macOS/Linux lock. OS releases it on exit, including SIGKILL."""
    def __init__(self, path): self.path = Path(path)
    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a')
        try: fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise SystemExit('Another run is active; duplicate run skipped.')
        return self
    def __exit__(self, *exc): self.file.close()
