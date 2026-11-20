from collections import defaultdict
from decimal import Decimal
from app.modules.m5_calculate import scope as scoping

PARCEL_FIELDS = scoping.PARCEL_FIELDS


def _parcel(report):
    return scoping.report_parcel(report)


def _relation_allowed(child, parent):
    """A retest may narrow a composite to a parcel, never broaden coverage."""
    child_parcel, parent_parcel = _parcel(child), _parcel(parent)
    if child_parcel and parent_parcel and child_parcel != parent_parcel:
        return False
    if not child_parcel and parent_parcel:
        return False
    return True


def select_reports(reports):
    versions={}
    for r in reports:
        if r['id'] not in versions or Decimal(str(r['version']))>Decimal(str(versions[r['id']]['version'])):versions[r['id']]=r
    reports=list(versions.values());lookup={r['id']:r for r in reports};bad={};valid=[]
    # A relation is a replacement chain; detect cycles before following any edge.
    for r in reports:
        chain=[];current=r
        while current.get('relation'):
            if current['id'] in chain:
                for rid in chain:bad[rid]='REFERENCE'
                break
            chain.append(current['id']);parent=lookup.get(current['relation'])
            if parent is None:bad[r['id']]='REFERENCE';break
            shared=('sample_id','batch','product','analyte','method','sampled')
            if any(current[k]!=parent[k] for k in shared) or current['reported']<=parent['reported']:
                bad[r['id']]='REFERENCE';break
            # Parcel scope may only narrow: no cross-parcel and no re-broadening.
            if not _relation_allowed(current,parent):
                bad[r['id']]='REFERENCE';break
            current=parent
        if current['id'] in bad:bad[r['id']]='REFERENCE'
    grouped=defaultdict(list)
    for r in reports:
        if r['id'] not in bad:grouped[(r['sample_id'],r['batch'],r['product'],r['analyte'])].append(r)
    for key,rows in grouped.items():
        roots=[r for r in rows if not r['relation']]
        if len(roots)!=1:
            for r in rows:bad[r['id']]='CONFLICT'
            continue
        candidates=[r for r in rows if r['id'] not in bad]
        # Conflicting results are judged per covered parcel: a retest of B2 is
        # not in conflict with a retest of B3 even when their values differ.
        scopes=defaultdict(list)
        for r in candidates:scopes[_parcel(r)].append(r)
        conflicted=False
        for members in scopes.values():
            rank=max((r['reported'],Decimal(str(r['version']))) for r in members)
            newest=[r for r in members if (r['reported'],Decimal(str(r['version'])))==rank]
            if len({(r['lower'],r['upper']) for r in newest})>1:conflicted=True
        if conflicted:
            for r in rows:bad[r['id']]='CONFLICT'
            continue
        rank=max((r['reported'],Decimal(str(r['version']))) for r in candidates)
        newest=[r for r in candidates if (r['reported'],Decimal(str(r['version'])))==rank]
        chosen=sorted(newest,key=lambda x:x['id'])[0];valid.append(chosen)
    return valid,bad


def _records(measurements, bad, scopes, composition, applicable):
    """Keep one disposition row per original report, including superseded ones."""
    deciding = {s['report_id'] for s in scopes if s['status'] == 'COMPLETE' and s['report_id']}
    records = []
    for r in measurements:
        parcel = _parcel(r)
        entry = composition.get(r['sample_id'])
        if parcel:
            covers = [parcel]
        elif entry is not None:
            covers = list(entry['parcels'])
        else:
            covers = ['*']
        if r['id'] in bad:
            disposition = 'EXCLUDED'
        elif r['id'] in deciding:
            disposition = 'ADOPTED'
        else:
            disposition = 'RETAINED'
        records.append({'id': r['id'], 'line': r['_line'], 'sample_id': r['sample_id'],
                        'batch': r['batch'], 'analyte': r['analyte'], 'parcel_id': parcel,
                        'result': r['result'], 'unit': r['unit'], 'lod': r['lod'],
                        'reported': r['reported'], 'version': r['version'],
                        'relation': r.get('relation') or '', 'code': r['code'],
                        'covers': covers, 'disposition': disposition})
    return records


def apply(state):
    chosen,bad=select_reports(state['measurements']);lines=set()
    for r in state['measurements']:
        if r['id'] in bad:
            lines.add(r['_line']);state['quarantine'].append({'line':r['_line'],'id':r['id'],'code':bad[r['id']],'raw':r['_raw']})
    state['rows']=[r for r in state['rows'] if r['_line'] not in lines]

    versions={}
    for r in state['measurements']:
        if r['id'] not in versions or Decimal(str(r['version']))>Decimal(str(versions[r['id']]['version'])):versions[r['id']]=r
    reports=list(versions.values());lookup={r['id']:r for r in reports}
    composition,composition_issues=scoping.parse_composition(state.get('parameters') or {})
    calculations,scopes,parcels,blocked,issues,applicable=scoping.resolve(
        reports,lookup,bad,chosen,composition,composition_issues)

    if applicable:
        state['calculations']=calculations
        state['scopes']=scopes
        state['parcels']=parcels
        state['scope_summary']=scoping.summarize(scopes,parcels,blocked,issues,applicable)
        state['business']={'total':len(scopes),'pass':sum(s['code']=='PASS' for s in scopes),
            'fail':sum(s['code']=='FAIL' for s in scopes),'uncertain':sum(s['code']=='UNCERTAIN' for s in scopes),
            'unit':'地块检测项目'}
    else:
        state['calculations']=[scoping._legacy_calculation(r,lookup) for r in chosen if r['id'] not in bad]
        state['scopes']=[];state['parcels']=[]
        state['scope_summary']={'applicable':False,'publishable':True,'scope_count':0,
            'parcel_count':0,'parcels':[],'blocked':[],'issues':[],
            'fingerprint':scoping.fingerprint([],[])}
        state['business']={'total':len(state['calculations']),
            'pass':sum(r['code']=='PASS' for r in state['calculations']),
            'fail':sum(r['code']=='FAIL' for r in state['calculations']),
            'uncertain':sum(r['code']=='UNCERTAIN' for r in state['calculations']),'unit':'检测项目'}
        scopes = [{'report_id': c['id'], 'status': 'COMPLETE'} for c in state['calculations']]
    state['records']=_records(state['measurements'],bad,scopes,composition,applicable)
    return state
