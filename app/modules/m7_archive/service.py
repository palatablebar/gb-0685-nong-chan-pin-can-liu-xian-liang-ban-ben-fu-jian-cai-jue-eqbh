from app.modules.m7_archive.export_out import checksum

def execute(state):
    summary=state.get('scope_summary',{})
    state['archive']={'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(state.get('records',[]))+len(state.get('parcels',[]))+len(state['quarantine']),
        'ready':state.get('reconcile',{}).get('balanced',False) and summary.get('publishable',True),
        'scope_publishable':summary.get('publishable',True)}
    return state
