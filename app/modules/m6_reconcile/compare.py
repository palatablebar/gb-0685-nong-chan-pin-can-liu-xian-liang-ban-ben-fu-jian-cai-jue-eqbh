from collections import defaultdict
RANK={'PASS':0,'UNCERTAIN':1,'FAIL':2}

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine']);accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    batches=defaultdict(list)
    for r in state['calculations']:batches[r['batch']].append(r)
    parcels=state.get('parcels',[])
    scopes=state.get('scopes',[])
    if scopes:
        # Scoped conclusions are authoritative: count each conclusion once, so a
        # whole-composite report repeated across parcels is not double counted,
        # and ordinary samples in the same batch still contribute their projects.
        grade_by_batch=defaultdict(list)
        for s in scopes:grade_by_batch[s['batch']].append(s)
        grades=[{'batch':b,'code':max((s['code'] for s in rows),key=lambda c:RANK[c]),
                 'projects':len(rows),
                 'samples':len({s['sample_id'] for s in rows}),
                 'parcels':len({(s['sample_id'],s['parcel_id']) for s in rows if s['parcel_id']})}
                for b,rows in sorted(grade_by_batch.items())]
    else:
        grades=[{'batch':b,'code':max((r['code'] for r in rows),key=lambda c:RANK[c]),
                 'projects':len(rows),'samples':len({r['sample_id'] for r in rows})}
                for b,rows in sorted(batches.items())]
    parcel_grades=[{'batch':p['batch'],'sample_id':p['sample_id'],'parcel_id':p['parcel_id'],
                    'code':p['grade'],'status':p['status'],
                    'projects':len(p['projects'])} for p in parcels]
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
            'blank':state['blank_count'],'headers':state['header_count'],
            'duplicates':state['duplicate_count'],'accounted':accounted,
            'balanced':accounted==state['input_count'],'business':state['business'],
            'grades':grades,'parcel_grades':parcel_grades,
            'scope_applicable':bool(state.get('scope_summary',{}).get('applicable')),
            'scope_publishable':bool(state.get('scope_summary',{}).get('publishable',True))}
