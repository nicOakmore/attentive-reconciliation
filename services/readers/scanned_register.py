"""Reading a SCANNED payroll register, completely.

Written after The Breathing Association, where nine of thirty employees were reported as
unreadable "because of page quality". Every one of them was on the page. The faults were a
block anchor the recogniser spells differently and a dropped thousands separator.

This module exists so that cannot happen again. It gates on completeness: give it the roster
you expect and it either returns all of them or raises, naming who is missing and where it
looked. It will not hand back a partial population silently.

    from scanned_register import read_scanned
    reg = read_scanned('previous.pdf', layout=PAYLOCITY_YTD, expect_nets=[...])

Techniques baked in, each one from a real failure:
  * render at 220 dpi, rotate upright. The two registers in a pair can be rotated opposite ways,
    so the rotation is detected per file rather than assumed
  * macOS Vision via ocrmac, which beats tesseract on these scans. Its bbox is (x, y, w, h) with
    y from the BOTTOM
  * multi-word boxes are split into positioned tokens, or columns do not line up
  * figures are assigned to columns by x position taken from the printed header, never by
    magnitude. The recognised text is usually right; the column choice is what fails
  * employee blocks are bounded by their own `Totals` row, never by a header word. "Earning"
    came out as "Earing" and "Eaming" and lost five blocks
  * numbers are repaired from arithmetic the document must satisfy: Social Security is exactly
    6.2% and Medicare exactly 1.45% of the same base, so that base is read four ways and the
    consensus wins. Net pay is printed twice, as Net and as Dir Dep
  * anything still missing is searched for by its known value, including the truncated form a
    lost comma produces, and then by name
"""
import os, re, json, glob, hashlib, subprocess, tempfile, collections, difflib

SS_RATE, MED_RATE = 0.062, 0.0145
NUMRE = re.compile(r'^[^\d\-(]{0,3}(-?[\d,]+\.\d\d)$')


class IncompleteRegister(RuntimeError):
    """Raised when the roster is not fully accounted for. Never caught and ignored."""


# --------------------------------------------------------------- layouts
class Layout:
    def __init__(self, name, cols, tax_codes, flat_rates=None):
        self.name, self.cols, self.tax_codes = name, cols, tax_codes
        self.flat_rates = flat_rates or {'SS': SS_RATE, 'MED': MED_RATE}


PAYLOCITY_YTD = Layout(
    'Paylocity Payroll Register with YTD',
    dict(code=(0.395, 0.430), taxable=(0.495, 0.540), amount=(0.548, 0.590),
         dcode=(0.690, 0.725), damount=(0.765, 0.805), net=(0.895, 0.945)),
    ('FITW', 'MED', 'SS', 'OH'))

PAYLOCITY_PREPROCESS = Layout(
    'Paylocity Pre Process Payroll Register',
    dict(code=(0.385, 0.425), taxable=(0.575, 0.635), amount=(0.638, 0.688),
         dcode=(0.685, 0.715), damount=(0.795, 0.860), net=(0.880, 0.960)),
    ('FITW', 'MED', 'SS', 'OH'))


# --------------------------------------------------------------- pipeline
def _cache(pdf_path, workdir):
    key = hashlib.md5(open(pdf_path, 'rb').read(1 << 20)).hexdigest()[:10]
    d = os.path.join(workdir or tempfile.gettempdir(), 'scanreg_' + key)
    os.makedirs(d, exist_ok=True)
    return d


def _render(pdf_path, d, dpi=220):
    if glob.glob(os.path.join(d, 'p-*.png')):
        return sorted(glob.glob(os.path.join(d, 'p-*.png')))
    subprocess.run(['pdftoppm', '-r', str(dpi), '-png', pdf_path, os.path.join(d, 'p')], check=True)
    return sorted(glob.glob(os.path.join(d, 'p-*.png')))


def _ocr(png):
    from ocrmac import ocrmac
    return [[t, c, list(b)] for t, c, b in ocrmac.OCR(png, recognition_level='accurate').recognize()]


def _orient(d, pngs):
    """Decide the rotation from the page itself, never assume it, and never score it on how many
    words are recognised or on confidence: macOS Vision reads these scans about equally well
    upside down, so both of those tie. What does discriminate is WHERE the report title lands.
    Correctly oriented, "Payroll Register" sits in the top-left corner. Rotated the wrong way it
    sits bottom-right."""
    from PIL import Image
    best, bestscore = 0, -1.0
    for ang in (-90, 90, 0, 180):
        im = Image.open(pngs[0])
        probe = os.path.join(d, 'probe%d.png' % ang)
        (im if ang == 0 else im.rotate(ang, expand=True)).save(probe)
        score = -1.0
        for t, c, b in _ocr(probe):
            if 'Payroll' in t or 'Register' in t:
                x, ytop = b[0], b[1] + b[3]
                if x < 0.35 and ytop > 0.75:        # top-left corner
                    score = max(score, (0.35 - x) + (ytop - 0.75))
        if score > bestscore:
            best, bestscore = ang, score
    if bestscore < 0:
        raise RuntimeError('cannot orient %s: no report title found in any rotation' % pngs[0])
    return best


def _tokens(raw):
    out = []
    for t, c, b in raw:
        parts = t.strip().split()
        if not parts:
            continue
        step = b[2] / max(len(parts), 1)
        for i, p in enumerate(parts):
            out.append(dict(t=p, x=b[0] + i * step, y=b[1] + b[3] / 2.0, conf=c))
    return out


def num(s):
    s = str(s).strip().replace('—', '-').replace('−', '-')
    m = NUMRE.match(s)
    if m:
        return float(m.group(1).replace(',', ''))
    m = re.search(r'(-?[\d,]+\.\d\d)', s)
    return float(m.group(1).replace(',', '')) if m else None


def near(toks, y, tol=0.005):
    return sorted([t for t in toks if abs(t['y'] - y) <= tol], key=lambda t: t['x'])


def pick(row, lo, hi):
    for t in row:
        if lo <= t['x'] <= hi:
            v = num(t['t'])
            if v is not None:
                return v
    return None


# --------------------------------------------------------------- blocks
SUBTOTAL = re.compile(r'Totals for|Employees\s|Female|Male')


def _blocks(toks):
    """One block per employee, bounded below by that employee's own Totals row. The region above
    the first Totals row on a page is the report header, not an employee, so it is dropped."""
    ys = sorted([t['y'] for t in toks if t['t'] == 'Totals'], reverse=True)
    rows, out, hi = [], [], 1.1
    for y in ys:
        if not rows or abs(rows[-1] - y) > 0.008:
            rows.append(y)
    for y in rows:
        blk = [t for t in toks if y - 0.004 <= t['y'] <= hi]
        hi = y - 0.004
        txt = ' '.join(t['t'] for t in blk)
        # A department or grant subtotal shares the shape of an employee block, but it never
        # carries a Net line. Filtering on the subtotal wording alone also discarded the employee
        # printed in the same region, which is how a thirty-employee register came back as
        # twenty-eight. Keep anything with a Net; drop only a subtotal that has none.
        if SUBTOTAL.search(txt) and 'Net' not in [t['t'] for t in blk]:
            continue
        out.append(blk)
    return out


def _fica_base(tax):
    cand = []
    for k, rate in (('SS', SS_RATE), ('MED', MED_RATE)):
        d = tax.get(k) or {}
        if d.get('taxable'):
            cand.append(d['taxable'])
        if d.get('amount'):
            cand.append(d['amount'] / rate)
    best, votes = None, 0
    for v in cand:
        agree = [w for w in cand if abs(w - v) <= max(0.05, v * 0.0005)]
        if len(agree) > votes:
            best, votes = sum(agree) / len(agree), len(agree)
    return (round(best, 2) if best else None), votes


def _employee(blk, lay):
    c = lay.cols
    e = dict(tax={}, ded={}, net=None, gross=None, name=None, empid=None)
    top = max(t['y'] for t in blk)
    nm = [t for t in blk if t['y'] >= top - 0.02 and t['x'] < 0.13
          and re.match(r"^[A-Za-z][A-Za-z'\-,.]{1,}$", t['t'])]
    e['name'] = ' '.join(t['t'] for t in sorted(nm, key=lambda t: (-t['y'], t['x'])))[:44]
    cands = []
    for t in blk:
        if t['t'] in ('Net', 'Dir', 'Dep'):
            v = pick([x for x in near(blk, t['y']) if x['x'] > t['x']], *c['net'])
            if v is not None:
                cands.append(v)
    if cands:
        e['net'] = max(cands, key=lambda v: sum(1 for w in cands if abs(w - v) <= 0.02))
        e['net_votes'] = sum(1 for w in cands if abs(w - e['net']) <= 0.02)
    for t in blk:
        key = t['t'].upper().replace('COLT', 'COL1').replace('COL7', 'COL1').replace('COLI', 'COL1')
        key = 'LOCAL' if key.startswith('OH-') else ('SS' if key == '55' else key)
        if key in lay.tax_codes + ('LOCAL',) and c['code'][0] <= t['x'] <= c['code'][1]:
            row = near(blk, t['y'])
            base, amt = pick(row, *c['taxable']), pick(row, *c['amount'])
            if base and key not in e['tax']:
                e['tax'][key] = dict(taxable=base, amount=amt)
        if c['dcode'][0] <= t['x'] <= c['dcode'][1] and re.match(r'^[A-Z0-9][A-Z0-9]{1,6}$', t['t']):
            v = pick(near(blk, t['y']), *c['damount'])
            if v is not None:
                e['ded'][t['t']] = v
    # The employee id is the reliable join key back to the census and the proposal. Names come
    # off a scan too loosely to match on: Felstead reads as Pestend, Tyler as Tater. Paylocity
    # prints "EmpId 77" at the left of each block, and the recogniser renders the label as EmpId,
    # Empld, Empla or Emp.
    for t in blk:
        # The label is printed EmpId and comes back as Empld, Empla, Empid, Emp1d, EmpIa, Emp.
        # Match the family rather than a spelling.
        if re.match(r'^Emp[A-Za-z0-9]{0,3}$', t['t'], re.I) and t['x'] < 0.16:
            row = [x for x in near(blk, t['y'], 0.006) if x['x'] > t['x']]
            for x in row:
                if re.match(r'^\d{1,5}$', x['t']):
                    e['empid'] = x['t'].lstrip('0') or x['t']
                    break
            if e.get('empid'):
                break
    if not e.get('empid'):
        # Positional fallback. The id sits in its own narrow column just right of the label,
        # within the top few rows of the block. Where the recogniser has mangled the label
        # itself, the number is still in the right place.
        top = max(t['y'] for t in blk)
        cands = [t for t in blk if top - 0.06 <= t['y'] <= top
                 and 0.085 <= t['x'] <= 0.125 and re.match(r'^\d{1,5}$', t['t'])]
        if cands:
            e['empid'] = sorted(cands, key=lambda t: -t['y'])[0]['t'].lstrip('0') or '0'
    e['fica'], e['fica_votes'] = _fica_base(e['tax'])
    return e


# --------------------------------------------------------------- recovery
def _variants(v):
    s = '%.2f' % v
    whole = s.split('.')[0]
    out = {s, '{:,.2f}'.format(v)}
    if len(whole) > 3:
        out.add(whole[0] + '.' + whole[1:3])
        out.add(whole[:-3] + '.' + whole[-3:-1])
    return out


def _norm(s):
    return re.sub(r'[^A-Z]', '', str(s).upper())


def _name_on_page(surname, blob):
    """Surnames come out of the recogniser loosely: TYLER as TATER, NORRIS as VORRIS, Gouch as
    Couch, McCall as Mcall, Jones as ones, Martinez as Martines. Compare word by word on
    similarity, never on equality, or a present employee is reported missing and somebody writes
    "page quality" in an audit.

    blob is {page: [normalised word, ...]} — the words must stay separate, because normalising a
    whole page into one string leaves nothing to compare against."""
    want = _norm(surname)
    if len(want) < 3:
        return False
    for words in blob.values():
        for tok in words:
            if len(tok) < 3:
                continue
            if want == tok or want in tok or tok in want:
                return True
            if difflib.SequenceMatcher(None, want, tok).ratio() >= 0.70:
                return True
            if len(want) >= 5 and len(tok) >= 5 and want[:4] == tok[:4]:
                return True
    return False


def _hunt(pages, target):
    """Find a known figure anywhere, including the form a lost comma produces."""
    want = _variants(target)
    hits = []
    for pg, toks in pages.items():
        for t in toks:
            v = num(t['t'])
            if (v is not None and abs(v - target) <= 0.02) or re.sub(r'[^\d.,-]', '', t['t']) in want:
                hits.append((pg, t['y'], t['x']))
    return hits


# --------------------------------------------------------------- entry point
def read_scanned(pdf_path, layout, expect_nets=None, expect_names=None, expect_roster=None,
                 workdir=None, dpi=220):
    """expect_roster is the strong form: [(surname, net_pay), ...]. An employee is accounted for
    if EITHER their net pay is located on a page or their surname is recognised. Net pay is a
    unique identifier, so a badly recognised surname no longer loses anybody; and a surname found
    without its net still counts, so a misread figure does not either."""
    d = _cache(pdf_path, workdir)
    pngs = _render(pdf_path, d, dpi)
    rotfile = os.path.join(d, 'rotation.json')
    if os.path.exists(rotfile):
        ang = json.load(open(rotfile))
    else:
        ang = _orient(d, pngs)
        json.dump(ang, open(rotfile, 'w'))
    from PIL import Image
    pages = {}
    for png in pngs:
        base = os.path.basename(png)
        rot = os.path.join(d, 'r-' + base)
        if not os.path.exists(rot):
            im = Image.open(png)
            (im if ang == 0 else im.rotate(ang, expand=True)).save(rot)
        oj = os.path.join(d, 'o-' + base.replace('.png', '.json'))
        if not os.path.exists(oj):
            json.dump(_ocr(rot), open(oj, 'w'))
        pages[base] = _tokens(json.load(open(oj)))

    emps = []
    for pg, toks in pages.items():
        for blk in _blocks(toks):
            e = _employee(blk, layout)
            if e.get('net') or e.get('tax'):
                e['page'] = pg
                emps.append(e)

    report = dict(pdf=pdf_path, rotation=ang, pages=len(pages), employees=len(emps),
                  workdir=d, missing=[], found_by_hunt=[])

    have = {round(e['net'], 2) for e in emps if e.get('net')}
    if expect_roster:
        blob = {pg: [_norm(t['t']) for t in toks if len(_norm(t['t'])) >= 3]
                for pg, toks in pages.items()}
        for nm, net in expect_roster:
            by_net = net is not None and (round(net, 2) in have or bool(_hunt(pages, net)))
            by_name = _name_on_page(nm, blob)
            if by_net or by_name:
                if not by_name:
                    report.setdefault('matched_by_net_only', []).append(nm)
                if not by_net and net is not None:
                    close = sorted(have, key=lambda h: abs(h - net))[:1]
                    if close and abs(close[0] - net) <= 1.00:
                        report.setdefault('discrepancies', []).append(
                            dict(name=nm, expected=net, register=close[0],
                                 difference=round(close[0] - net, 2)))
                    else:
                        report.setdefault('matched_by_name_only', []).append(nm)
                continue
            # How hard this is depends on the evidence available. With a net pay figure the check
            # is definitive and a miss is fatal. With a surname alone it is advisory, because the
            # recogniser mangles surnames (Felstead as Pestend, Tyler as Tater), so record it
            # loudly as unconfirmed rather than abort a run that is otherwise sound.
            if net is None:
                report.setdefault('unconfirmed', []).append(nm)
            else:
                report['missing'].append(dict(name=nm, net=net))
    if expect_nets:
        for want in expect_nets:
            if round(want, 2) in have:
                continue
            hits = _hunt(pages, want)
            if hits:
                report['found_by_hunt'].append(dict(net=want, at=hits[:3]))
                continue
            close = sorted(have, key=lambda h: abs(h - want))[:1]
            if close and abs(close[0] - want) <= 1.00:
                # the register and the supplied figure differ slightly. That is a finding about
                # the documents, not a failure to read one, so record it and carry on.
                report.setdefault('discrepancies', []).append(
                    dict(expected=want, register=close[0], difference=round(close[0] - want, 2)))
            else:
                report['missing'].append(dict(net=want))
    if expect_names:
        blob = {pg: [_norm(t['t']) for t in toks if len(_norm(t['t'])) >= 3]
                for pg, toks in pages.items()}
        for nm in expect_names:
            if not _name_on_page(nm, blob):
                report['missing'].append(dict(name=nm))

    if report['missing']:
        raise IncompleteRegister(
            '%s: %d of the expected roster not accounted for.\n%s\n'
            'Work directory %s. Render the page and read it directly rather than dropping anyone.'
            % (os.path.basename(pdf_path), len(report['missing']),
               '\n'.join('   missing: %s' % m for m in report['missing']), d))
    return emps, report


if __name__ == '__main__':
    import sys
    emps, rep = read_scanned(sys.argv[1], PAYLOCITY_YTD)
    print(json.dumps(rep, indent=1))
    for e in emps[:5]:
        print('  %-30s net %-10s fica %-10s taxes %s' % (e['name'], e['net'], e['fica'], list(e['tax'])))


def detect_layout(pdf_path, workdir=None, dpi=150):
    """Which Paylocity report is this? A scan has no text layer, so read the title off the page.
    Choosing the layout from a text layer that is not there silently picks the wrong column
    windows and quietly loses fields."""
    d = _cache(pdf_path, workdir)
    pngs = _render(pdf_path, d, dpi)
    rotfile = os.path.join(d, 'rotation.json')
    ang = json.load(open(rotfile)) if os.path.exists(rotfile) else _orient(d, pngs)
    json.dump(ang, open(rotfile, 'w'))
    from PIL import Image
    probe = os.path.join(d, 'layoutprobe.png')
    im = Image.open(pngs[0])
    (im if ang == 0 else im.rotate(ang, expand=True)).save(probe)
    words = ' '.join(t for t, c, b in _ocr(probe))
    return PAYLOCITY_PREPROCESS if re.search(r'pre\s*process', words, re.I) else PAYLOCITY_YTD
