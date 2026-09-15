"""The decisive test: does each layer of evidence reduce the readings that need a human without accepting a wrong one.

Four systems are compared on the pages whose true figures are known, exactly as the review of this design asked:

  1. the reading alone
  2. the reading plus the statement's own identities
  3. the reading plus what the census and the proposal expect
  4. all of it

For each: how many readings are exact, how many are wrong and accepted anyway (the number that must be zero), and
how many are left for a human. Then four deliberate corruptions are injected into a clean page to see whether the
evidence recovers the printed figure or leaves it open. Nothing here is allowed to pass by guessing: a system that
accepts a wrong figure fails regardless of its accuracy.
"""
import sys, os, json, statistics as stats
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import parse_files as P, pageread as PR, estimate as E, crossdoc as X
from services.audit import Census, Engine

TRUTH = '/Users/nico/attentive_audit/tools/tioga_full_comparison.json'
CACHE = '/tmp/pagewords'
PACKS = (('prev', 41, 'wh_before', 'net_before'), ('mock', 43, 'wh_after', 'net_after'))


def pages():
    labels = {k: (P.LINE_PATTERNS.get(k, []) + P.COMPACT_PATTERNS.get(k, []))
              for k in set(P.LINE_PATTERNS) | set(P.COMPACT_PATTERNS)}
    truth = {t['name']: t for t in json.load(open(TRUTH))}
    tm = {}
    for k, v in truth.items():
        f, l = k.split(' ', 1)
        tm[(l.lower().replace(' jr', '').replace(' ', ''), f.lower()[:3])] = v
    out = []
    for tag, n, kf, kn in PACKS:
        for i in range(n):
            f = os.path.join(CACHE, f'{tag}_{i:02d}.json')
            if not os.path.exists(f):
                continue
            ws = [PR.Word(**w) for w in json.load(open(f))]
            r = PR.read_page(ws, labels)
            rec = P.parse_text_paycheck(r.text, geometry={k: dict(value=v.value, method=v.method,
                                                                  confidence=v.confidence, note=v.note)
                                                          for k, v in r.fields.items()})
            parts = [p.strip() for p in (rec.get('name') or '').split(',')]
            if len(parts) < 2 or not parts[1].split():
                continue
            t = tm.get((parts[0].lower().replace(' jr', '').replace(' ', ''), parts[1].split()[0].lower()[:3]))
            if not t:
                continue
            out.append(dict(tag=tag, page=i, rec=rec, truth={'federal': t[kf], 'net_pay': t[kn]}, t=t))
    return out


def expectations_for(p):
    t = p['t']
    census = Census(gross_annual=t.get('salary'), pay_periods=12, retirement_401k=0.0)
    engine = Engine(premium=1173.0, fee=t.get('fee'), federal_before=t.get('eng_fed_before'),
                    taxable_income_before=t.get('tw_before'))
    return X.expectations(census, engine, 12, which='before' if p['tag'] == 'prev' else 'after')


def run(system, ps):
    exact = wrong_accepted = left = 0
    accepted_wrong = []
    for p in ps:
        rec, exp = p['rec'], (expectations_for(p) if system in (3, 4) else None)
        fit = E.solve(rec) if system in (2, 4) else None
        for f, want in p['truth'].items():
            got = rec.get(f)
            if want is None:
                continue
            if got is None:
                left += 1
                continue
            suspect = bool(rec.get(f + '_disputed') or rec.get(f + '_unreliable'))
            if fit is not None and fit.outlier.get(f, 0) >= 0.6:
                suspect = True
            if exp is not None:
                dis = X.disagreements({f: got}, {k: v for k, v in exp.items() if k == f})
                if dis:
                    suspect = True
            if suspect:
                left += 1
                continue
            if abs(got - want) <= 0.02:
                exact += 1
            else:
                wrong_accepted += 1
                accepted_wrong.append((p['tag'], p['page'] + 1, p['rec'].get('name'), f, got, want))
    return dict(exact=exact, wrong_accepted=wrong_accepted, left_for_a_human=left, cases=accepted_wrong)


def corruptions(ps):
    """Take a page that reads correctly, break one figure the way a scan breaks it, and see what the evidence does."""
    clean = next((p for p in ps if p['rec'].get('federal') is not None
                  and abs(p['rec']['federal'] - p['truth']['federal']) <= 0.02
                  and p['rec'].get('total_deductions') is not None
                  and p['rec'].get('net_pay') is not None and p['rec'].get('gross') is not None), None)
    if clean is None:
        return []
    true = clean['rec']['federal']
    out = []
    for bad, label in ((1173.00, 'the premium from the table alongside'), (114.00, 'the fee from the table alongside'),
                       (round(true + 0.10, 2), 'a digit after the decimal point'),
                       (round(true * 10, 2), 'a lost decimal point')):
        rec = dict(clean['rec']); rec['federal'] = bad
        fit = E.solve(rec)
        post = E.most_likely_reading(rec, 'federal', expectations=expectations_for(clean))
        top = (post.get('posterior') or [{}])[0]
        out.append(dict(corruption=label, read=bad, truth=true,
                        outlier_posterior=fit.outlier.get('federal'),
                        recovered=bool(top.get('value') is not None and abs(top['value'] - true) <= 0.02),
                        decisive=post.get('decisive'), top=top))
    return out


def main():
    ps = pages()
    print(f'{len(ps)} statement pages with figures verified by hand\n')
    names = {1: 'the reading alone', 2: 'reading plus the statement identities',
             3: 'reading plus census and proposal expectations', 4: 'all of it'}
    for sysno in (1, 2, 3, 4):
        r = run(sysno, ps)
        total = r['exact'] + r['wrong_accepted'] + r['left_for_a_human']
        print(f"{names[sysno]:46s} exact {r['exact']:3d}  wrong accepted {r['wrong_accepted']:2d}  "
              f"left for a human {r['left_for_a_human']:3d}  of {total}")
        if sysno == 4:
            for c in r['cases']:
                print(f"      accepted but wrong: {c[0]} page {c[1]} {str(c[2])[:24]} {c[3]} read {c[4]} "
                      f"expected {c[5]}")
    print()
    for c in corruptions(ps):
        print(f"corrupted with {c['corruption']:38s} read {c['read']:>9.2f} truth {c['truth']:>9.2f} "
              f"outlier {c['outlier_posterior']} recovered {c['recovered']} decisive {c['decisive']}")


if __name__ == '__main__':
    main()
