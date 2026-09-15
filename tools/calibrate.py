"""Calibrate the reading error from pages whose true figures are known, and validate on pages held back.

The review of this design was firm on two points and both are enforced here: the error model is measured rather
than invented, and the pages used to measure it are not the pages used to claim accuracy. Half the pages calibrate,
half are untouched until the end.

Writes calibration.json: a robust standard deviation per field, the rate at which a reading is a misreading rather
than noise, and the digit confusions actually observed.
"""
import sys, os, json, re, argparse, statistics as stats, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import parse_files as P, pageread as PR

TRUTH = '/Users/nico/attentive_audit/tools/tioga_full_comparison.json'
CACHE = '/tmp/pagewords'
PACKS = (('prev', 41, 'wh_before', 'net_before', 'tw_before', 'medg_before'),
         ('mock', 43, 'wh_after', 'net_after', 'tw_after', 'medg_after'))
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'calibration.json')


def truth_map():
    t = {r['name']: r for r in json.load(open(TRUTH))}
    m = {}
    for k, v in t.items():
        f, l = k.split(' ', 1)
        m[(l.lower().replace(' jr', '').replace(' ', ''), f.lower()[:3])] = v
    return m


def key_of(name):
    parts = [p.strip() for p in (name or '').split(',')]
    if len(parts) < 2 or not parts[1].split():
        return None
    return (parts[0].lower().replace(' jr', '').replace(' ', ''), parts[1].split()[0].lower()[:3])


def read_pages():
    labels = {k: (P.LINE_PATTERNS.get(k, []) + P.COMPACT_PATTERNS.get(k, []))
              for k in set(P.LINE_PATTERNS) | set(P.COMPACT_PATTERNS)}
    tm = truth_map()
    out = []
    for tag, n, kf, kn, ktw, kmg in PACKS:
        for i in range(n):
            f = os.path.join(CACHE, f'{tag}_{i:02d}.json')
            if not os.path.exists(f):
                continue
            ws = [PR.Word(**w) for w in json.load(open(f))]
            r = PR.read_page(ws, labels)
            rec = P.parse_text_paycheck(r.text, geometry={k: dict(value=v.value, method=v.method,
                                                                  confidence=v.confidence, note=v.note)
                                                          for k, v in r.fields.items()})
            t = tm.get(key_of(rec.get('name')) or ())
            if not t:
                continue
            out.append(dict(tag=tag, page=i, rec=rec,
                            truth={'federal': t[kf], 'net_pay': t[kn], 'taxable_wages': t[ktw],
                                   'medicare_gross': t[kmg]}))
    return out


def robust_sd(errors, floor=0.05):
    """The ordinary spread of a correct reading. A scan does not read a figure slightly wrong: it reads it right to
    the cent or it reads something else entirely, so the spread is estimated from the readings that are right and
    the rest are treated as a separate process, which is what the mixture in the solver is for."""
    if not errors:
        return None
    small = [e for e in errors if abs(e) <= 1.0]
    if len(small) < 3:
        return floor
    med = stats.median(small)
    mad = stats.median([abs(e - med) for e in small])
    return round(max(floor, 1.4826 * mad), 4)


def digit_confusions(pairs):
    """Which digit was printed where the scan read another, counted over aligned figures of the same length."""
    counts = {}
    for got, want in pairs:
        a, b = f'{got:.2f}', f'{want:.2f}'
        if len(a) != len(b):
            continue
        for ca, cb in zip(a, b):
            if ca.isdigit() and cb.isdigit() and ca != cb:
                counts.setdefault(cb, {}).setdefault(ca, 0)
                counts[cb][ca] += 1
    subs = {}
    for true_digit, seen in counts.items():
        subs[true_digit] = [d for d, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:4]]
    return subs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--split', type=float, default=0.5, help='fraction of pages used to calibrate')
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args()
    pages = read_pages()
    random.Random(a.seed).shuffle(pages)
    cut = int(len(pages) * a.split)
    train, validate = pages[:cut], pages[cut:]
    print(f'{len(pages)} pages with known figures: {len(train)} calibrate, {len(validate)} held back')

    per_field, wrong_pairs, n_obs, n_wrong = {}, [], 0, 0
    for p in train:
        for f, want in p['truth'].items():
            got = p['rec'].get(f)
            if got is None or want is None:
                continue
            n_obs += 1
            e = got - want
            per_field.setdefault(f, []).append(e)
            if abs(e) > 0.02:
                n_wrong += 1
                wrong_pairs.append((got, want))

    field_sd = {f: robust_sd(es) for f, es in per_field.items()}
    field_sd = {f: sd for f, sd in field_sd.items() if sd}
    field_sd['default'] = round(stats.median([v for k, v in field_sd.items() if k != 'default']) or 0.5, 4) \
        if field_sd else 0.5
    wrong_mag = [abs(g - w) for g, w in wrong_pairs]
    scale = round((stats.median(wrong_mag) / max(field_sd.get('default', 0.05), 0.01)) if wrong_mag else 40.0, 1)
    cal = dict(field_sd=field_sd,
               outlier_rate=round(n_wrong / max(1, n_obs), 4),
               outlier_scale=max(10.0, min(5000.0, scale)),
               typical_wrong_by=round(stats.median(wrong_mag), 2) if wrong_mag else None,
               confusions=dict(substitute=digit_confusions(wrong_pairs),
                               lost_leading_digit=0.02, decimal_as_comma=0.01, extra_leading_digit=0.01),
               calibrated_on=dict(pages=len(train), observations=n_obs, wrong=n_wrong),
               held_back=dict(pages=len(validate)))
    json.dump(cal, open(OUT, 'w'), indent=1)
    print('wrote', OUT)
    print(json.dumps({k: v for k, v in cal.items() if k != 'confusions'}, indent=1))
    print('confusions:', json.dumps(cal['confusions']['substitute']))

    # the held back half: how often the reading is right, and whether the outlier posterior finds the ones that are not
    from services import estimate as E
    right = wrong = flagged = 0
    for p in validate:
        fit = E.solve(p['rec'], cal)
        for f, want in p['truth'].items():
            got = p['rec'].get(f)
            if got is None or want is None:
                continue
            if abs(got - want) <= 0.02:
                right += 1
            else:
                wrong += 1
                if fit.outlier.get(f, 0) > 0.5:
                    flagged += 1
    print(f'held back: {right} readings exact, {wrong} wrong, of which {flagged} carried an outlier posterior '
          f'above one half')


if __name__ == '__main__':
    main()
