"""Built-in sample scenarios.

``composite.csv`` is the co-arrival case: the laboratory report and the
composition sheet (合样单) arrive together.  One composite sample is made from
four parcels; the initial report covers all four and exceeds the limit, and a
later retest covers only parcel B2.  The retest must narrow the conclusion for
B2 without releasing B1/B3/B4.

``composite_broken.csv`` carries a retest that names a parcel absent from the
composition sheet.  Its run is intentionally not publishable so that the
previously published version is retained.
"""

COMPOSITIONS = {
    'composite.csv': (
        'sample_id,parcel_id,product,batch,sampled\n'
        'C001,B1,苹果,BC01,2026-08-01\n'
        'C001,B2,苹果,BC01,2026-08-01\n'
        'C001,B3,苹果,BC01,2026-08-01\n'
        'C001,B4,苹果,BC01,2026-08-01\n'
        'C002,B1,苹果,BC02,2026-08-01\n'
        'C002,B2,苹果,BC02,2026-08-01\n'
        'C002,B3,苹果,BC02,2026-08-01\n'
        'C002,B4,苹果,BC02,2026-08-01\n'
    ),
    'composite_broken.csv': (
        'sample_id,parcel_id,product,batch,sampled\n'
        'C001,B1,苹果,BC01,2026-08-01\n'
        'C001,B2,苹果,BC01,2026-08-01\n'
        'C001,B3,苹果,BC01,2026-08-01\n'
        'C001,B4,苹果,BC01,2026-08-01\n'
    ),
}


def default_parameters(sample):
    text = COMPOSITIONS.get(sample)
    return {'composition_csv': text} if text else {}
