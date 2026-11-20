import base64
import pytest
from app import store
from app.errors import DomainError
from app.workflow import run, load, parameters_for


def submit(client, sample):
    return client.post('/api/runs', json={'sample': sample})


def test_composite_retest_narrows_only_its_parcel(client):
    state = submit(client, 'composite.csv').get_json()
    parcels = {(p['sample_id'], p['parcel_id']): p for p in state['parcels']}
    # The B2 retest is a new conclusion for B2 only...
    assert parcels[('C001', 'B2')]['grade'] == 'PASS'
    assert next(p for p in parcels[('C001', 'B2')]['projects']
                if p['analyte'] == 'P01')['scope'] == 'parcel'
    # ...and may not release the other parcels of the composite sample.
    assert parcels[('C001', 'B1')]['grade'] == 'FAIL'
    assert parcels[('C001', 'B3')]['grade'] == 'FAIL'
    assert parcels[('C001', 'B4')]['grade'] == 'FAIL'
    assert state['business'] == {'total': 16, 'pass': 10, 'fail': 6, 'uncertain': 0,
                                 'unit': '地块检测项目'}


def test_untested_analyte_keeps_composite_scope(client):
    state = submit(client, 'composite.csv').get_json()
    p02 = [s for s in state['scopes'] if s['analyte'] == 'P02']
    assert p02 and all(s['scope'] == 'composite' and s['code'] == 'PASS' for s in p02)
    assert all(s['covers'] == ['B1', 'B2', 'B3', 'B4'] for s in p02)


def test_each_conclusion_names_actual_evidence(client):
    state = submit(client, 'composite.csv').get_json()
    by_key = {(s['sample_id'], s['analyte'], s['parcel_id']): s for s in state['scopes']}
    assert by_key[('C001', 'P01', 'B2')]['evidence'] == ['10001', '10002']
    assert by_key[('C001', 'P01', 'B1')]['evidence'] == ['10001']
    assert state['archive']['ready'] is True


def test_original_composite_and_retest_are_both_retained(client):
    state = submit(client, 'composite.csv').get_json()
    records = {r['id']: r for r in state['records']}
    assert records['10001']['disposition'] == 'ADOPTED'
    assert records['10002']['disposition'] == 'ADOPTED'
    # The initial report stays an adoptable record for the parcels it covers.
    assert records['10001']['covers'] == ['B1', 'B2', 'B3', 'B4']
    assert records['10001'] in state['records']
    from app.modules.m7_archive.export_out import bundle
    text = bundle(state)
    assert all(rid in text for rid in ('10001', '10002', '10003'))


def test_broken_composition_is_not_publishable(client):
    state = submit(client, 'composite_broken.csv').get_json()
    assert state['scope_summary']['publishable'] is False
    assert any(i['code'] == 'UNREGISTERED_PARCEL' for i in state['scope_summary']['issues'])
    assert state['archive']['ready'] is False
    assert all(p['status'] == 'BLOCKED' for p in state['parcels'])
    assert client.get('/api/archive/%s/export.csv' % state['run_id']).status_code == 409


def test_missing_composition_keeps_total_sample_conclusion(client):
    # A parcel-scoped retest without any composition sheet cannot narrow scope.
    raw = load('composite.csv').decode('utf-8-sig')
    state = client.post('/api/runs', json={'csv': raw, 'parameters': {}}).get_json()
    assert state['scope_summary']['publishable'] is False
    assert any(i['code'] == 'COMPOSITE_MISSING' for i in state['scope_summary']['issues'])
    assert state['archive']['ready'] is False


def test_failed_rerun_retains_previous_publication(client):
    good = submit(client, 'composite.csv').get_json()
    broken = submit(client, 'composite_broken.csv').get_json()
    assert good['publication']['published'] is True
    # The failed attempt is stored as evidence but not signed off as published.
    stored = client.get('/api/runs/%s' % broken['run_id']).get_json()
    assert stored['published'] is False
    assert stored['archive']['ready'] is False
    publication = store.publication('C001')
    assert publication['run_id'] == good['run_id']
    assert publication['parcels'] == ['B1', 'B2', 'B3', 'B4']
    assert publication['coverage'] == [
        'P01@B1', 'P01@B2', 'P01@B3', 'P01@B4',
        'P02@B1', 'P02@B2', 'P02@B3', 'P02@B4']
    # The workbench still shows the latest attempt, so the failure is visible.
    assert client.get('/api/runs').get_json()[0]['id'] == broken['run_id']


def test_same_source_cannot_add_parcels(client):
    first = submit(client, 'composite.csv').get_json()
    state = run(load('composite.csv'), parameters_for('composite.csv', {
        'composition_csv': 'sample_id,parcel_id\nC001,B1\nC001,B2\nC001,B3\nC001,B4\nC001,B5\n'
                           'C002,B1\nC002,B2\nC002,B3\nC002,B4\n'}))
    stored = store.save(state['run_id'], state)
    assert stored['publication']['published'] is False
    assert stored['publication']['conflicts'][0]['sample_id'] == 'C001'
    assert stored['publication']['conflicts'][0]['confirmed'] == ['B1', 'B2', 'B3', 'B4']
    assert store.publication('C001')['run_id'] == first['run_id']
    # The untouched sample still publishes normally.
    assert store.publication('C002')['parcels'] == ['B1', 'B2', 'B3', 'B4']


def test_same_source_rerun_is_idempotent(client):
    first = submit(client, 'composite.csv').get_json()
    second = submit(client, 'composite.csv').get_json()
    assert first['run_id'] == second['run_id']
    assert second['publication']['published'] is True
    runs = client.get('/api/runs').get_json()
    assert sum(r['id'] == first['run_id'] for r in runs) == 1
    assert store.publication('C001')['fingerprint'] == store.publication('C001')['fingerprint']


def test_stale_review_still_rejected(client):
    state = submit(client, 'composite.csv').get_json()
    assert client.post('/api/runs/%s/review' % state['run_id'],
                       json={'version': 1, 'decision': '通过'}).status_code == 200
    assert client.post('/api/runs/%s/review' % state['run_id'],
                       json={'version': 1, 'decision': '退回'}).status_code == 409


def test_cross_parcel_retest_reference_rejected():
    from app.modules.m5_calculate.rules import select_reports
    state = run(load('composite.csv'), {}, 4, sample='composite.csv')
    # A retest may not point at a report that itself belongs to another parcel.
    parent = next(m for m in state['measurements'] if m['id'] == '10001')
    parent['parcel'] = 'B1'
    _, bad = select_reports(state['measurements'])
    assert bad.get('10002') == 'REFERENCE'


def test_retest_may_not_rebroaden_to_whole_composite():
    from app.modules.m5_calculate.rules import select_reports
    state = run(load('composite.csv'), {}, 4, sample='composite.csv')
    parent = next(m for m in state['measurements'] if m['id'] == '10001')
    parent['parcel'] = 'B2'
    retest = next(m for m in state['measurements'] if m['id'] == '10002')
    retest['parcel'] = ''  # a parcel retest cannot widen back to the total sample
    _, bad = select_reports(state['measurements'])
    assert bad.get('10002') == 'REFERENCE'


def test_new_source_may_refine_scope(client):
    import csv, io
    from app.workflow import load
    first = submit(client, 'composite.csv').get_json()
    rows = list(csv.reader(io.StringIO(load('composite.csv').decode('utf-8-sig'))))
    # A genuinely new report: B3 retest arrives as a new source file.
    rows.append(['10007', 'C001', 'BC01', '苹果', 'P01', 'LC-MS/MS', '3',
                 '2026-08-01', '2026-08-04', '0.004', 'mg/kg', '0.001',
                 '10001', 'B3地块复检', 'B3'])
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    state = client.post('/api/runs', json={
        'csv': out.getvalue(), 'parameters': {'composition_csv': __import__(
            'app.scenarios', fromlist=['COMPOSITIONS']).COMPOSITIONS['composite.csv']}}).get_json()
    assert state['publication']['published'] is True
    parcels = {(p['sample_id'], p['parcel_id']): p for p in state['parcels']}
    assert parcels[('C001', 'B3')]['grade'] == 'PASS'
    assert parcels[('C001', 'B1')]['grade'] == 'FAIL'
    assert store.publication('C001')['run_id'] == state['run_id']


def test_same_source_scope_conflict_retains_previous(client):
    first = submit(client, 'composite.csv').get_json()
    fewer = ('sample_id,parcel_id,product,batch,sampled\n'
             'C001,B1,苹果,BC01,2026-08-01\nC001,B2,苹果,BC01,2026-08-01\n'
             'C001,B4,苹果,BC01,2026-08-01\n'
             'C002,B1,苹果,BC02,2026-08-01\nC002,B2,苹果,BC02,2026-08-01\n'
             'C002,B3,苹果,BC02,2026-08-01\nC002,B4,苹果,BC02,2026-08-01\n')
    state = run(load('composite.csv'), parameters_for('composite.csv',
                {'composition_csv': fewer}))
    stored = store.save(state['run_id'], state)
    assert stored['publication']['published'] is False
    assert [r['reason'] for r in stored['publication']['reasons']] == ['SCOPE_CONFLICT']
    # The run is the adjudication unit: nothing is half-published.
    assert stored['publication']['published_samples'] == []
    assert store.publication('C001')['parcels'] == ['B1', 'B2', 'B3', 'B4']
    assert store.publication('C001')['run_id'] == first['run_id']
    assert store.publication('C002')['run_id'] == first['run_id']


def test_batch_grading_uses_parcel_conclusions(client):
    state = submit(client, 'composite.csv').get_json()
    assert state['reconcile']['scope_applicable'] is True
    grades = {g['batch']: g for g in state['reconcile']['grades']}
    # B2/B4 pass, but their batches still carry FAIL parcels B1/B3.
    assert grades['BC01']['code'] == 'FAIL'
    assert grades['BC02']['code'] == 'FAIL'
    parcel_grades = {(g['sample_id'], g['parcel_id']): g['code']
                     for g in state['reconcile']['parcel_grades']}
    assert parcel_grades[('C001', 'B2')] == 'PASS'
    assert parcel_grades[('C001', 'B1')] == 'FAIL'
    # A whole-composite report is counted once per conclusion, not per parcel.
    assert grades['BC01']['projects'] == state['business']['total'] // 2


def test_archive_carries_per_parcel_provenance(client):
    state = submit(client, 'composite.csv').get_json()
    rows = client.get('/api/archive/%s/export.csv' % state['run_id']).data.decode('utf-8-sig')
    assert '范围裁决' in rows and '地块' in rows
    # Both the deciding report and the covered report appear per parcel.
    assert '"parcel_id"": ""B2"' in rows
    assert '10001' in rows and '10002' in rows


def test_undecided_composite_cannot_be_signed(client):
    state = submit(client, 'composite_broken.csv').get_json()
    export = client.get('/api/archive/%s/export.csv' % state['run_id'])
    assert export.status_code == 409
    assert export.get_json()['code'] == 'UNPUBLISHABLE'
    review = client.post('/api/runs/%s/review' % state['run_id'],
                         json={'version': 1, 'decision': '通过'})
    assert review.status_code == 409
    assert review.get_json()['code'] == 'UNPUBLISHABLE'
