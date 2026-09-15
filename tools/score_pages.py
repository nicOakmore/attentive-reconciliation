"""Score the page reader against figures verified by hand.

The OCR words for each page are cached under /tmp so the reader can be changed and re-scored in seconds. Run with
--ocr once to build that cache, then without it.
"""
import sys, os, json, argparse, statistics
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import parse_files as P, pageread as PR

S = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'samples', 'Tioga ISD')
PACKS = (('prev', '9.1 Tioga ISD - Prev.pdf', 41, 'wh_before', 'net_before', 'tw_before', 'medg_before'),
         ('mock', '9.1 Tioga ISD - Mock.pdf', 43, 'wh_after', 'net_after', 'tw_after', 'medg_after'))
CACHE = '/tmp/pagewords'


def words_for(tag, path, i, do_ocr):
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, f'{tag}_{i:02d}.json')
    if os.path.exists(f) and not do_ocr:
        return [PR.Word(**w) for w in json.load(open(f))]
    data = open(os.path.join(S, path), 'rb').read()
    png = P.pdf_page_png(data, i, scale=2.8)
    ws = PR.words_from_ocr(png)
    json.dump([dict(id=w.id, text=w.text, x0=w.x0, y0=w.y0, x1=w.x1, y1=w.y1, conf=w.conf) for w in ws], open(f, 'w'))
    return ws


def key_of(name):
    parts = [p.strip() for p in (name or '').split(',')]
    if len(parts) < 2 or not parts[1].split():
        return None
    return (parts[0].lower().replace(' jr', '').replace(' ', ''), parts[1].split()[0].lower()[:3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ocr', action='store_true', help='re-run OCR and refresh the word cache')
    ap.add_argument('--show', type=int, default=12)
    a = ap.parse_args()
    truth = {t['name']: t for t in json.load(open('/Users/nico/attentive_audit/tools/tioga_full_comparison.json'))}
    tmap = {}
    for k, v in truth.items():
        f, l = k.split(' ', 1)
        tmap[(l.lower().replace(' jr', '').replace(' ', ''), f.lower()[:3])] = v
    labels = {k: (P.LINE_PATTERNS.get(k, []) + P.COMPACT_PATTERNS.get(k, []))
              for k in set(P.LINE_PATTERNS) | set(P.COMPACT_PATTERNS)}
    tally = {}
    misses = []
    for tag, path, n, kf, kn, ktw, kmg in PACKS:
        for i in range(n):
            ws = words_for(tag, path, i, a.ocr)
            if not ws:
                continue
            r = PR.read_page(ws, labels)
            nm = None
            for pat_field in ('name',):
                pass
            txt = r.text
            base = P.parse_text_paycheck(txt)
            k = key_of(base.get('name'))
            t = tmap.get(k)
            if not t:
                tally['unmatched page'] = tally.get('unmatched page', 0) + 1
                continue
            for field, want in (('federal', t[kf]), ('net_pay', t[kn]),
                                ('taxable_wages', t[ktw]), ('medicare_gross', t[kmg])):
                got = r.fields[field].value
                if want is None:
                    continue
                if got is None:
                    tally[field + ' unread'] = tally.get(field + ' unread', 0) + 1
                elif abs(got - want) < 0.02:
                    tally[field + ' exact'] = tally.get(field + ' exact', 0) + 1
                else:
                    tally[field + ' wrong'] = tally.get(field + ' wrong', 0) + 1
                    misses.append((tag, i + 1, base.get('name'), field, got, want,
                                   r.fields[field].method, (r.fields[field].note or '')[:60]))
    for k in sorted(tally):
        print(f'{k:28s} {tally[k]}')
    print()
    for m in misses[:a.show]:
        print('  ', m)
    print(f'\n{len(misses)} wrong readings in total')


if __name__ == '__main__':
    main()
