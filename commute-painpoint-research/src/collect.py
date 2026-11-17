"""Only approved search-result excerpts; no community page/API scraping.

The committed seed is an auditable short-excerpt import from this Codex web search
session. Live collection uses the official Brave Search API only after the user
configures a subscription permitting result storage. New results stay candidates.
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from common import RunLock, canonical_url, digest, write_json, write_csv

ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'

def request_json(url, token, attempts=3, sleeper=time.sleep):
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={'X-Subscription-Token': token, 'Accept': 'application/json', 'User-Agent': 'CommutePainpointResearch/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Auth, CAPTCHA/403 and ordinary client failures are never retried.
            if e.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1: raise
            retry = e.headers.get('Retry-After')
            if retry and (not retry.isdigit() or int(retry) > 60): raise
            sleeper(max(2 ** (attempt + 1), int(retry or 0)))
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1: raise
            sleeper(2 ** (attempt + 1))

def collect(config, out):
    from urllib.parse import urlencode
    token = os.getenv('BRAVE_SEARCH_API_KEY')
    if not token or os.getenv('BRAVE_STORAGE_RIGHTS_CONFIRMED') != 'true':
        raise SystemExit('Live search requires BRAVE_SEARCH_API_KEY and storage-rights confirmation. No requests made.')
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    state_path = out/'search_state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'done': [], 'candidates': {}, 'failures': []}
    for q in config['queries']:
        if q in state['done']: continue
        try:
            result = request_json(ENDPOINT + '?' + urlencode({'q': q, 'count': 20, 'search_lang': 'ko'}), token)
            for item in result.get('web', {}).get('results', []):
                try: url = canonical_url(item['url'])
                except (KeyError, ValueError): continue
                # Do not retain unreviewed snippets/titles: they can contain names,
                # handles or contacts. Source pages are never fetched by this tool.
                from urllib.parse import urlsplit
                if any(x in urlsplit(url).path for x in ['/user/', '/users/', '/u/', '/@', '/%40']): continue
                sid = digest(url)[:16]
                if sid not in state['candidates']:
                    state['candidates'][sid] = {'source_url': url, 'query': q,
                        'collected_date': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(), 'review_status': 'pending',
                        'collection_method': 'brave_search_result', 'original_page_verified': False}
            state['done'].append(q)
        except Exception as e:
            transient = isinstance(e, (urllib.error.URLError, TimeoutError)) and not isinstance(e, urllib.error.HTTPError)
            code = getattr(e, 'code', None)
            state['failures'].append({'query': q, 'error_type': type(e).__name__, 'http_status': code,
                                      'action': 'retry_later' if transient or code in (429,500,502,503,504) else 'review_credentials_or_access'})
            # Stop the batch on rate-limit/auth denial. A long Retry-After must
            # not be circumvented by starting the next query after two seconds.
            if code in (401, 403, 429):
                write_json(state_path, state)
                break
        write_json(state_path, state)
        time.sleep(max(2, config.get('request_interval_seconds', 2)))
    write_json(out/'candidates.json', list(state['candidates'].values()))
    write_csv(out/'candidates.csv', list(state['candidates'].values()), ['source_url','query','collected_date','review_status','collection_method','original_page_verified'])
    write_json(out/'collection_failures.json', state['failures'])
    write_csv(out/'collection_failures.csv', state['failures'], ['query','error_type','http_status','action'])
    pending = [q for q in config['queries'] if q not in state['done']]
    if pending: raise SystemExit(f'{len(pending)} queries incomplete; see collection_failures.csv and rerun after resolving the cause.')

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',default='config/keywords.json');p.add_argument('--out',default='data/live')
    a=p.parse_args()
    with RunLock(Path(a.out)/'.collection.lock'):
        collect(json.loads(Path(a.config).read_text()),a.out)
