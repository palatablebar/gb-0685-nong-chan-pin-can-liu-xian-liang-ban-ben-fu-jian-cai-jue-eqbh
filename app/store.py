import sqlite3,json,os,hashlib,base64
from pathlib import Path
from app.errors import DomainError


def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS publications (sample_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL, parcels TEXT NOT NULL, grade TEXT NOT NULL, updated_at TEXT NOT NULL)')
    for column,declaration in [('source_key','TEXT'),('published','INTEGER NOT NULL DEFAULT 0')]:
        try:db.execute('ALTER TABLE runs ADD COLUMN %s %s'%(column,declaration))
        except sqlite3.OperationalError:pass
    try:db.execute('ALTER TABLE publications ADD COLUMN coverage TEXT NOT NULL DEFAULT "[]"')
    except sqlite3.OperationalError:pass
    try:db.execute('ALTER TABLE publications ADD COLUMN source_key TEXT NOT NULL DEFAULT ""')
    except sqlite3.OperationalError:pass
    return db


def source_key(body):
    """Identity of the raw report bytes (used for run idempotency and audit)."""
    raw=body.get('source_b64')
    if not raw:return ''
    return hashlib.sha256(base64.b64decode(raw)).hexdigest()


def _sample_summary(body):
    """Per-sample publication view: graded parcels and the scope fingerprint.

    The parcel range is what a confirmed result promises; the fingerprint also
    covers the conclusions so a legitimate new retest can be recorded while an
    attempt to add or drop parcels is refused.
    """
    summary=body.get('scope_summary') or {}
    if not summary.get('applicable'):
        return {}
    parcels_by_sample={}
    for parcel in body.get('parcels',[]):
        parcels_by_sample.setdefault(parcel['sample_id'],[]).append(parcel)
    scopes_by_sample={}
    for scope in body.get('scopes',[]):
        scopes_by_sample.setdefault(scope['sample_id'],[]).append(scope)
    affected=set(parcels_by_sample)|{i.get('sample_id') for i in summary.get('issues',[])
                                     if i.get('sample_id')}
    result={}
    for sample_id in sorted(affected):
        parcels=parcels_by_sample.get(sample_id,[])
        scopes=scopes_by_sample.get(sample_id,[])
        sample_issues=[i for i in summary.get('issues',[]) if i.get('sample_id')==sample_id]
        digest=hashlib.sha256(json.dumps([
            {'analyte':s['analyte'],'parcel_id':s['parcel_id'],'code':s['code'],
             'report_id':s['report_id'],'status':s['status']}
            for s in sorted(scopes,key=lambda x:(x['analyte'],x['parcel_id']))],
            sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        # A sample with composition or retest issues is never complete, even if
        # no parcel could be placed at all.
        complete=bool(parcels) and not sample_issues and \
            all(p['status']=='COMPLETE' for p in parcels)
        coverage=sorted('%s@%s'%(s['analyte'],s['parcel_id']) for s in scopes
                        if s['status']=='COMPLETE')
        result[sample_id]={'fingerprint':digest,'parcels':sorted(p['parcel_id'] for p in parcels),
                           'coverage':coverage,
                           'grade':max((p['grade'] for p in parcels),
                                       key=lambda c:{'PASS':0,'UNCERTAIN':1,'FAIL':2}.get(c,1))
                                   if parcels else 'UNCERTAIN',
                           'complete':complete,'issues':sample_issues}
    return result


def _publish(db,run_id,body,key=''):
    payload=_sample_summary(body)
    if not payload:
        return {'published':True,'applicable':False,'published_samples':[],
                'retained_samples':[],'conflicts':[],'reasons':[]}
    generated=(body.get('manifest') or {}).get('generated_at','')
    published,retained,conflicts,reasons=[],[],[],[]
    pending=[]
    for sample_id,entry in sorted(payload.items()):
        existing=db.execute('SELECT * FROM publications WHERE sample_id=?',(sample_id,)).fetchone()
        if not entry['complete']:
            # Incomplete composition or retest evidence: never replace a
            # confirmed result; keep the previously published version.
            if existing:retained.append({'sample_id':sample_id,'run_id':existing['run_id']})
            reasons.append({'sample_id':sample_id,'reason':'INCOMPLETE','issues':entry['issues']})
            continue
        same_source = bool(existing and key and existing['source_key'] == key)
        if same_source and (json.loads(existing['parcels'])!=entry['parcels']
                            or json.loads(existing['coverage'])!=entry['coverage']
                            or existing['fingerprint']!=entry['fingerprint']):
            # A same-source rerun may not add/drop parcels, swap a confirmed
            # range or change a confirmed conclusion; a genuinely new report
            # (new source) may still refine the coverage.
            retained.append({'sample_id':sample_id,'run_id':existing['run_id']})
            conflicts.append({'sample_id':sample_id,'run_id':existing['run_id'],
                              'confirmed':json.loads(existing['parcels']),
                              'confirmed_coverage':json.loads(existing['coverage']),
                              'requested':entry['parcels'],
                              'requested_coverage':entry['coverage']})
            reasons.append({'sample_id':sample_id,'reason':'SCOPE_CONFLICT',
                            'issues':entry['issues']})
            continue
        pending.append((sample_id,entry))
        published.append(sample_id)
    # The run is the adjudication unit: publish it only when every scoped sample
    # is complete and consistent.  Otherwise the whole attempt is evidence and
    # every affected sample keeps its previously published version, so the
    # workbench and the published ledger never disagree.
    ok = bool(pending) and not retained and not conflicts
    if not ok:
        return {'published':False,'applicable':True,'published_samples':[],
                'retained_samples':retained,'conflicts':conflicts,'reasons':reasons}
    for sample_id,entry in pending:
        db.execute('INSERT INTO publications(sample_id,run_id,fingerprint,parcels,grade,coverage,source_key,updated_at) '
                   'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(sample_id) DO UPDATE SET run_id=excluded.run_id,'
                   'fingerprint=excluded.fingerprint,parcels=excluded.parcels,grade=excluded.grade,'
                   'coverage=excluded.coverage,source_key=excluded.source_key,updated_at=excluded.updated_at',
                   (sample_id,run_id,entry['fingerprint'],json.dumps(entry['parcels'],ensure_ascii=False),
                    entry['grade'],json.dumps(entry['coverage'],ensure_ascii=False),key,generated))
    return {'published':True,'applicable':True,'published_samples':published,
            'retained_samples':retained,'conflicts':conflicts,'reasons':reasons}


def save(run_id, body):
    key=source_key(body)
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body,source_key) VALUES (?,?,?)',
                   (run_id,json.dumps(body,ensure_ascii=False),key))
        # Always decide from the immutable stored body: a repeated save with a
        # tampered payload must not change the run or the published ledger.
        stored=json.loads(db.execute('SELECT body FROM runs WHERE id=?',(run_id,)).fetchone()['body'])
        publication=_publish(db,run_id,stored,key)
        if publication['published']:
            db.execute('UPDATE runs SET published=1 WHERE id=?',(run_id,))
        # The workbench shows the latest attempt so a failure is visible, while
        # the publication ledger keeps the last adoptable version per sample.
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    state=get(run_id);state['publication']=publication;return state


def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'],published=bool(r['published']));return body


def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision,published FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def publication(sample_id):
    with connection() as db:r=db.execute('SELECT * FROM publications WHERE sample_id=?',(sample_id,)).fetchone()
    if not r:return None
    return {'sample_id':r['sample_id'],'run_id':r['run_id'],'fingerprint':r['fingerprint'],
            'parcels':json.loads(r['parcels']),'grade':r['grade'],
            'coverage':json.loads(r['coverage'])}


def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)
