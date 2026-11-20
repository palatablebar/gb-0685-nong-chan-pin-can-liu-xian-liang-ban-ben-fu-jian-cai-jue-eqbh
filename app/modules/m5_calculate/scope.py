"""Per-parcel conclusion scoping for composite (合样) samples.

A composite sample is one laboratory sample made from the material of several
parcels (地块).  A report without a parcel reference covers the whole composite;
a report that names a parcel only covers that parcel.

The decisive rule is that a parcel-scoped retest may narrow the conclusion for
its own parcel, but it may never be promoted to a new conclusion for the other
parcels of the composite.  Batch grading and archiving therefore consume the
per-parcel conclusions produced here, each backed by the reports that actually
cover it.

Failure handling is deliberately conservative: a missing composition sheet, a
retest whose reference chain is broken or a parcel without covering evidence
marks the run unpublishable and reports no narrower conclusions, so the caller
can keep the previously published version.
"""

import csv
import hashlib
import io
import json
from collections import defaultdict
from decimal import Decimal
from app.errors import DomainError
from app.modules.m1_ingest.decode import decode

PARCEL_FIELDS = ('parcel', 'parcel_id', 'plot')
SAMPLE_FIELDS = ('sample_id', 'composite_id', 'total_sample_id')
META_FIELDS = ('product', 'batch', 'sampled')
RANK = {'PASS': 0, 'UNCERTAIN': 1, 'FAIL': 2}


def _issue(code, detail, sample_id='', parcel_id='', analyte=''):
    return {'code': code, 'sample_id': sample_id, 'parcel_id': parcel_id,
            'analyte': analyte, 'detail': detail}


def report_parcel(report):
    """The parcel a single report declares, or '' for a whole-composite report."""
    for field in PARCEL_FIELDS:
        value = str(report.get(field, '') or '').strip()
        if value:
            return value
    return ''


def _text(value):
    return str(value or '').strip()


def _rows_from_parameters(parameters):
    """Accept a composition sheet as CSV text, a list of rows or a mapping."""
    if not isinstance(parameters, dict):
        return [], [_issue('COMPOSITE', '参数必须是对象')]
    raw = parameters.get('composition_csv')
    if raw in (None, ''):
        raw = parameters.get('composition')
    if raw in (None, ''):
        return [], []
    if isinstance(raw, str):
        try:
            text, _ = decode(raw.encode('utf-8'))
        except DomainError:
            return [], [_issue('COMPOSITE', '合样单编码无法识别')]
        reader = csv.reader(io.StringIO(text, newline=''))
        try:
            header = [s.strip().lstrip('﻿') for s in next(reader)]
        except (StopIteration, csv.Error):
            return [], [_issue('COMPOSITE', '合样单缺少表头')]
        sample_key = next((f for f in SAMPLE_FIELDS if f in header), None)
        parcel_key = next((f for f in PARCEL_FIELDS if f in header), None)
        if not sample_key or not parcel_key:
            return [], [_issue('COMPOSITE', '合样单表头缺少 sample_id/parcel_id')]
        rows, issues = [], []
        try:
            for row in reader:
                if not row or not any(s.strip() for s in row):
                    continue
                if row == header:
                    continue
                if len(row) != len(header):
                    issues.append(_issue('WIDTH', '合样单列数不齐'))
                    continue
                rows.append(dict(zip(header, row), _line=reader.line_num))
        except csv.Error as exc:
            return [], [_issue('COMPOSITE', '合样单无法解析: %s' % exc)]
        return rows, issues
    if isinstance(raw, list):
        return [dict(r, _line=i + 2) for i, r in enumerate(raw) if isinstance(r, dict)], []
    if isinstance(raw, dict):
        rows = []
        for sample_id, parcels in raw.items():
            for parcel in parcels if isinstance(parcels, (list, tuple)) else [parcels]:
                rows.append({'sample_id': sample_id, 'parcel_id': parcel})
        return rows, []
    return [], [_issue('COMPOSITE', '合样单格式无效')]


def parse_composition(parameters):
    """Return ``(map, issues)``.

    ``map`` maps composite sample id to ``{'parcels': [...], 'meta': {...}}``.
    Content problems never raise: they are reported as issues so the
    publication gate can refuse the run while the previous version stays.
    """
    rows, issues = _rows_from_parameters(parameters)
    if not rows and not issues:
        return {}, []
    sample_key = next((f for f in SAMPLE_FIELDS if any(f in r for r in rows)), SAMPLE_FIELDS[0])
    parcel_key = next((f for f in PARCEL_FIELDS if any(f in r for r in rows)), PARCEL_FIELDS[0])
    batches = defaultdict(list)
    seen = {}
    for row in rows:
        sample_id = _text(row.get(sample_key))
        parcel_id = _text(row.get(parcel_key))
        if not sample_id or not parcel_id:
            issues.append(_issue('COMPOSITE', '合样单缺少总样或地块编号', sample_id, parcel_id))
            continue
        meta = {field: _text(row.get(field)) for field in META_FIELDS
                if row.get(field) not in (None, '')}
        signature = json.dumps(meta, sort_keys=True, ensure_ascii=False)
        key = (sample_id, parcel_id)
        if key in seen:
            if seen[key] != signature:
                issues.append(_issue('COMPOSITE_CONFLICT', '同一地块在合样单中内容矛盾',
                                     sample_id, parcel_id))
            continue
        seen[key] = signature
        batches[sample_id].append({'parcel_id': parcel_id, 'meta': meta})
    result = {}
    for sample_id, entries in batches.items():
        parcels = [e['parcel_id'] for e in entries]
        meta = {}
        for entry in entries:
            for field, value in entry['meta'].items():
                if field in meta and meta[field] != value:
                    issues.append(_issue('COMPOSITE_CONFLICT', '合样单 %s 不一致' % field, sample_id))
                else:
                    meta[field] = value
        if not parcels:
            issues.append(_issue('COMPOSITE_EMPTY', '合样单未登记地块', sample_id))
            continue
        if len(set(parcels)) != len(parcels):
            issues.append(_issue('COMPOSITE_CONFLICT', '合样单地块重复', sample_id))
        result[sample_id] = {'parcels': parcels, 'meta': meta}
    # A sheet that names no usable sample at all is a content failure, not an
    # absent sheet; treat it as an issue so the run cannot publish.
    if not result and not issues:
        issues.append(_issue('COMPOSITE_EMPTY', '合样单未登记任何总样'))
    return result, issues


def chain_ids(report, lookup):
    """Root-to-leaf report ids; guards against malformed cycles."""
    ids = []
    current = report
    while current is not None:
        if current['id'] in ids:
            break
        ids.append(current['id'])
        relation = current.get('relation')
        current = lookup.get(relation) if relation else None
    return list(reversed(ids))


def covers_of(report, entry, sample_id):
    """Parcels a report actually covers."""
    parcel = report_parcel(report)
    if parcel:
        return [parcel]
    if entry is not None:
        return list(entry['parcels'])
    return [sample_id]


def _rank(report):
    return (report['reported'], Decimal(str(report['version'])), report['id'])


def _scope_entry(report, lookup, sample_id, analyte, parcel_id, covers, status):
    return {
        'sample_id': sample_id, 'batch': report['batch'], 'product': report['product'],
        'analyte': analyte, 'parcel_id': parcel_id,
        'scope': 'parcel' if report_parcel(report) else 'composite',
        'code': report['code'], 'report_id': report['id'], 'line': report['_line'],
        'method': report['method'], 'standard': report['standard_id'],
        'lower': report['lower'], 'upper': report['upper'],
        'upper_exclusive': report['upper_exclusive'], 'limit': report['limit'],
        'covers': covers, 'evidence': chain_ids(report, lookup), 'status': status,
    }


def _blocked_entry(sample_id, batch, product, analyte, parcel_id, root, lookup, status):
    return {
        'sample_id': sample_id, 'batch': batch, 'product': product, 'analyte': analyte,
        'parcel_id': parcel_id, 'scope': 'blocked', 'code': root['code'] if root else 'UNCERTAIN',
        'report_id': root['id'] if root else '', 'line': root['_line'] if root else '',
        'method': root['method'] if root else '', 'standard': root['standard_id'] if root else '',
        'lower': root['lower'] if root else '', 'upper': root['upper'] if root else '',
        'upper_exclusive': root['upper_exclusive'] if root else False,
        'limit': root['limit'] if root else '',
        'covers': ['*'], 'evidence': chain_ids(root, lookup) if root else [], 'status': status,
    }


def resolve(reports, lookup, bad, chosen, composition, composition_issues=()):
    """Resolve reports into per-parcel conclusions for composite samples.

    ``reports`` are the highest-version report per id, ``bad`` is the
    reference validation map from :mod:`app.modules.m5_calculate.rules` and
    ``chosen`` is the legacy single-result selection used for samples that are
    not composites.  Returns ``(calculations, scopes, parcels, blocked, issues,
    applicable)``.
    """
    issues = list(composition_issues)
    scoped_samples = set(composition)
    for report in reports:
        if report_parcel(report):
            scoped_samples.add(report['sample_id'])
    applicable = bool(scoped_samples)
    if not applicable:
        return [], [], [], [], [], False

    chosen_by_key = {(r['sample_id'], r['batch'], r['product'], r['analyte']): r for r in chosen}
    grouped = defaultdict(list)
    for report in reports:
        grouped[(report['sample_id'], report['batch'], report['product'], report['analyte'])].append(report)

    calculations, scopes, blocked = [], [], []
    for (sample_id, batch, product, analyte), rows in grouped.items():
        if sample_id not in scoped_samples:
            # Ordinary sample in a batch that also contains composites: keep the
            # legacy whole-sample conclusion, still expressed as one scope.
            report = chosen_by_key.get((sample_id, batch, product, analyte))
            if report is None or report['id'] in bad:
                continue
            calculations.append(_legacy_calculation(report, lookup))
            scopes.append(_scope_entry(report, lookup, sample_id, analyte, '', ['*'], 'COMPLETE'))
            continue

        entry = composition.get(sample_id)
        group_bad = sorted({r['id'] for r in rows if r['id'] in bad})
        roots = [r for r in rows if not r.get('relation') and r['id'] not in bad]
        conflict = False

        if entry is None:
            issues.append(_issue('COMPOSITE_MISSING', '缺少合样单，无法确定地块范围',
                                 sample_id, '', analyte))
            conflict = True
        else:
            meta = entry.get('meta', {})
            for field in META_FIELDS:
                if meta.get(field) and any(str(r.get(field, '')) != meta[field] for r in rows):
                    issues.append(_issue('COMPOSITE_CONFLICT', '合样单 %s 与报告不一致' % field,
                                         sample_id, '', analyte))
                    conflict = True
        for report in rows:
            parcel = report_parcel(report)
            if not parcel:
                continue
            if entry is None:
                issues.append(_issue('COMPOSITE_MISSING', '复检声明地块但缺少合样关系',
                                     sample_id, parcel, analyte))
            elif parcel not in entry['parcels']:
                issues.append(_issue('UNREGISTERED_PARCEL', '复检地块未登记在合样单',
                                     sample_id, parcel, analyte))
                conflict = True

        if group_bad:
            issues.append(_issue('RETEST_EVIDENCE', '复检引用不完整，保留总样结论',
                                 sample_id, '', analyte))
            conflict = True
        if len(roots) != 1:
            if not group_bad:
                issues.append(_issue('CONFLICT', '总样原始报告根不唯一', sample_id, '', analyte))
            conflict = True

        if conflict:
            # Never publish a narrower conclusion from incomplete evidence.
            # Keep the riskiest surviving root as a blocked placeholder for the
            # whole composite so no parcel is silently released; the publication
            # gate then retains the previously published version.
            pool = roots or [r for r in rows if r['id'] not in bad] or rows
            root = max(pool, key=lambda r: (RANK.get(r['code'], 1), r['id'])) if pool else None
            blocked.append({'sample_id': sample_id, 'batch': batch, 'product': product,
                            'analyte': analyte, 'report_id': root['id'] if root else '',
                            'code': root['code'] if root else 'UNCERTAIN',
                            'reason': 'INCOMPLETE'})
            parcels = entry['parcels'] if entry is not None else ['']
            for parcel in parcels:
                scopes.append(_blocked_entry(sample_id, batch, product, analyte, parcel,
                                             root, lookup, 'BLOCKED'))
            continue

        for parcel in entry['parcels']:
            candidates = [r for r in rows if r['id'] not in bad
                          and parcel in covers_of(r, entry, sample_id)]
            if not candidates:
                issues.append(_issue('UNCOVERED', '地块没有实际覆盖的报告',
                                     sample_id, parcel, analyte))
                scopes.append(_blocked_entry(sample_id, batch, product, analyte, parcel,
                                             roots[0], lookup, 'UNCOVERED'))
                continue
            top = max(_rank(r) for r in candidates)
            newest = [r for r in candidates if _rank(r) == top]
            if len({(r['lower'], r['upper']) for r in newest}) > 1:
                issues.append(_issue('CONFLICT', '地块候选复检结论矛盾', sample_id, parcel, analyte))
                report = sorted(newest, key=lambda r: r['id'])[0]
                scopes.append(_scope_entry(report, lookup, sample_id, analyte, parcel,
                                           covers_of(report, entry, sample_id), 'CONFLICT'))
                continue
            report = sorted(newest, key=lambda r: r['id'])[0]
            scopes.append(_scope_entry(report, lookup, sample_id, analyte, parcel,
                                       covers_of(report, entry, sample_id), 'COMPLETE'))

    parcels = _aggregate(scopes)
    # Every scoped conclusion becomes one calculation, including blocked ones so
    # the workbench and archive show why the run could not publish.
    calculations.extend(_calculation(scope, lookup, report) for scope, report in
                        _scope_calculations(scopes, reports))
    return calculations, scopes, parcels, blocked, issues, True


def _legacy_calculation(report, lookup):
    return {'id': report['id'], 'line': report['_line'], 'sample_id': report['sample_id'],
            'batch': report['batch'], 'analyte': report['analyte'], 'method': report['method'],
            'standard': report['standard_id'], 'lower': report['lower'], 'upper': report['upper'],
            'upper_exclusive': report['upper_exclusive'], 'limit': report['limit'],
            'code': report['code'], 'replaces': report.get('relation') or '',
            'parcel_id': '', 'scope': 'composite', 'covers': ['*'],
            'evidence': chain_ids(report, lookup), 'status': 'COMPLETE'}


def _calculation(scope, lookup, report):
    return {'id': report['id'], 'line': report['_line'], 'sample_id': scope['sample_id'],
            'batch': scope['batch'], 'analyte': scope['analyte'], 'method': report['method'],
            'standard': report['standard_id'], 'lower': scope['lower'], 'upper': scope['upper'],
            'upper_exclusive': scope['upper_exclusive'], 'limit': scope['limit'],
            'code': scope['code'], 'replaces': report.get('relation') or '',
            'parcel_id': scope['parcel_id'], 'scope': scope['scope'],
            'covers': scope['covers'], 'evidence': scope['evidence'], 'status': scope['status']}


def _scope_calculations(scopes, reports):
    """One adopted calculation per distinct (parcel, deciding report)."""
    lookup = {r['id']: r for r in reports}
    seen = {}
    for scope in scopes:
        if not scope['report_id'] or scope['report_id'] not in lookup:
            continue
        key = (scope['sample_id'], scope['analyte'], scope['parcel_id'], scope['report_id'])
        if key in seen:
            continue
        seen[key] = (scope, lookup[scope['report_id']])
    order = {(s['sample_id'], s['analyte'], s['parcel_id']): i for i, s in enumerate(scopes)}
    return [seen[key] for key in sorted(seen, key=lambda k: order.get(k[:3], 0))]


def _aggregate(scopes):
    by_parcel = defaultdict(list)
    for scope in scopes:
        # Ordinary non-composite samples are not parcels; their conclusion is
        # already carried by the legacy calculation.
        if not scope['parcel_id']:
            continue
        by_parcel[(scope['sample_id'], scope['batch'], scope['product'], scope['parcel_id'])].append(scope)
    parcels = []
    for (sample_id, batch, product, parcel_id), rows in by_parcel.items():
        projects = [{'analyte': s['analyte'], 'code': s['code'], 'report_id': s['report_id'],
                     'scope': s['scope'], 'status': s['status']} for s in rows]
        grade = max((p['code'] for p in projects), key=lambda c: RANK.get(c, 1))
        status = 'COMPLETE' if all(p['status'] == 'COMPLETE' for p in projects) else 'BLOCKED'
        parcels.append({'sample_id': sample_id, 'batch': batch, 'product': product,
                        'parcel_id': parcel_id, 'grade': grade, 'status': status,
                        'projects': projects})
    parcels.sort(key=lambda p: (p['batch'], p['sample_id'], p['parcel_id']))
    return parcels


def fingerprint(scopes, parcels):
    payload = [
        {'sample_id': s['sample_id'], 'analyte': s['analyte'], 'parcel_id': s['parcel_id'],
         'code': s['code'], 'report_id': s['report_id'], 'lower': str(s['lower']),
         'upper': str(s['upper']), 'limit': str(s['limit']), 'status': s['status']}
        for s in sorted(scopes, key=lambda x: (x['sample_id'], x['analyte'], x['parcel_id']))
    ]
    parcel_order = [
        {'sample_id': p['sample_id'], 'parcel_id': p['parcel_id'], 'grade': p['grade'],
         'status': p['status'],
         'projects': sorted([(x['analyte'], x['code'], x['report_id']) for x in p['projects']])}
        for p in sorted(parcels, key=lambda x: (x['sample_id'], x['parcel_id']))
    ]
    raw = json.dumps({'scopes': payload, 'parcels': parcel_order}, sort_keys=True,
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def summarize(scopes, parcels, blocked, issues, applicable):
    complete = bool(applicable) and not issues and not blocked \
        and all(p['status'] == 'COMPLETE' for p in parcels) \
        and all(s['status'] == 'COMPLETE' for s in scopes)
    return {
        'applicable': applicable,
        'publishable': (not applicable) or complete,
        'scope_count': len(scopes),
        'parcel_count': len(parcels),
        'parcels': sorted({p['parcel_id'] for p in parcels}),
        'blocked': blocked,
        'issues': issues,
        'fingerprint': fingerprint(scopes, parcels),
    }
