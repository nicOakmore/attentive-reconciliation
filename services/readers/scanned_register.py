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


# One engine per thread. RapidOCR holds onnxruntime session state that is not safe to share,
# and the pages are read in parallel below.
_LOCAL = __import__('threading').local()


def _ocr(png):
    """Text with boxes as (text, confidence, [x, y, w, h]), normalised, y measured from the BOTTOM.

    macOS Vision reads these scans better than anything else to hand, so it is used when present.
    It does not exist on Linux, which is where this runs in production, and with no fallback every
    scanned register failed there. RapidOCR is already a dependency of the app and runs on both.
    Its boxes are absolute polygons with y from the TOP, so they are converted to the Vision
    convention here rather than at each of the four call sites.

    Set SCANREG_OCR=rapid to force the fallback, which is how the Linux path gets tested on a Mac.
    """
    if os.environ.get('SCANREG_OCR', '').lower() not in ('rapid', 'rapidocr'):
        try:
            from ocrmac import ocrmac
            return [[t, c, list(b)]
                    for t, c, b in ocrmac.OCR(png, recognition_level='accurate').recognize()]
        except Exception:
            pass
    from PIL import Image
    eng = getattr(_LOCAL, 'rapid', None)
    if eng is None:
        from rapidocr_onnxruntime import RapidOCR
        eng = _LOCAL.rapid = RapidOCR()
    w, h = Image.open(png).size
    res, _ = eng(png)
    out = []
    for box, text, conf in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, x1 = min(xs) / w, max(xs) / w
        ytop, ybot = min(ys) / h, max(ys) / h
        # Vision's y is the box's lower edge measured up from the bottom of the page.
        out.append([text, float(conf), [x0, 1.0 - ybot, x1 - x0, ybot - ytop]])
    return out


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


# Page furniture and column headings. Anything here is never part of a person's name, and
# leaving the column words out let the mock register return sixteen employees called "Code".
PAGE_WORDS = {'payroll', 'register', 'with', 'ytd', 'the', 'breathing', 'association',
              'pre', 'process', 'period', 'ending', 'page', 'company', 'date', 'check',
              'employees', 'female', 'male', 'department', 'unknown', 'emp', 'id', 'totals',
              'code', 'hours', 'hrs', 'amount', 'amt', 'rate', 'type', 'earnings', 'deductions',
              'taxes', 'net', 'gross', 'dir', 'dep', 'current', 'qtd', 'descr', 'description',
              'memo', 'other', 'total', 'pay', 'hour', 'run', 'org', 'loc', 'cost', 'center'}
NAME_TOK = re.compile(r"^[A-Za-z][A-Za-z'\-,.]{1,}$")


def _name_of(blk):
    """The employee name row, found by the column header beneath it rather than by the top of
    the block.

    The block runs from this employee's Totals row up to the previous one, so its top edge is
    whatever sits above: on the first block of a page that is the report title, which is how
    employees arrived called "Payroll Register". The name is printed immediately above the
    "Code / Hours / Amount / YTD" header that opens the earnings grid, and that header is a
    reliable anchor on every page.
    """
    rows = {}
    for t in blk:
        rows.setdefault(round(t['y'], 3), []).append(t)
    hdr_y = None
    for y, ts in rows.items():
        words = {t['t'].lower() for t in ts}
        if 'code' in words and ({'amount', 'hours', 'hrs'} & words):
            hdr_y = y if hdr_y is None else max(hdr_y, y)
    bands = []
    for y, ts in rows.items():
        if hdr_y is not None and not (hdr_y + 0.002 <= y <= hdr_y + 0.035):
            continue
        got = [t for t in ts if t['x'] < 0.135 and NAME_TOK.match(t['t'])
               and t['t'].lower().strip(".,'-") not in PAGE_WORDS]
        if got:
            bands.append((y, got))
    if not bands:
        # No header anchor on this block: fall back to the topmost name-like row that is not
        # page furniture, which is still better than taking the top edge blind.
        for y, ts in sorted(rows.items(), key=lambda kv: -kv[0]):
            got = [t for t in ts if t['x'] < 0.135 and NAME_TOK.match(t['t'])
                   and t['t'].lower().strip(".,'-") not in PAGE_WORDS]
            if got:
                bands = [(y, got)]
                break
    if not bands:
        return ''
    y, got = sorted(bands, key=lambda b: -b[0])[0]
    return ' '.join(t['t'] for t in sorted(got, key=lambda t: t['x']))[:44]


def _employee(blk, lay):
    c = lay.cols
    e = dict(tax={}, ded={}, net=None, gross=None, name=None, empid=None)
    e['name'] = _name_of(blk)
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
JUNK = ('payroll register', 'employees female', 'employees male', 'company totals',
        'grand total', 'totals', 'department', 'check register', 'period ending',
        'emp id', 'employee name', 'continued')


def _clean_name(raw):
    """Strip the column labels the recogniser sweeps up with the name."""
    t = re.sub(r'\s+', ' ', str(raw or '')).strip()
    t = re.sub(r'\bemp\s*[i1lt][da]\b.*$', '', t, flags=re.I).strip()
    t = re.sub(r'\bpayroll\s+register(\s+with(\s+ytd)?)?\b', '', t, flags=re.I).strip()
    t = re.sub(r'\bemployees?\s+(fe)?male\b', '', t, flags=re.I).strip()
    t = re.sub(r'[^A-Za-z,.\'\- ]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' .,-')
    return t


def _is_junk(e, nets=None, median=None):
    """A header or a totals block is not a person. A person whose NAME was misread is.

    Telling them apart on the name alone throws away real employees: the block finder sweeps the
    page title into the name field, so "Payroll Register" with its own employee number and a
    plausible net is a person, not a header. What actually distinguishes a totals block is the
    money: its net repeats across blocks and dwarfs an individual cheque.
    """
    n = _clean_name(e.get('name')).lower()
    net = e.get('net')
    if net and median and net > 5 * median:
        return True                              # a company or department total
    if net and nets and nets.get(round(net, 2), 0) > 1 and not e.get('empid'):
        return True                              # the same total printed twice
    if len(n) < 3 and not e.get('empid') and not net:
        return True
    return any(j in n for j in JUNK) and not e.get('empid')


# What the recogniser actually confuses on these photocopies, measured on the Breathing
# Association scans: l/I/1/t, O/0/D, rn/m, S/5, B/8, G/6. Folding them before comparing lets
# "Haltston" reach "Hairston" and "Mariner" reach "Martinez".
_FOLD = str.maketrans({'1': 'l', 'i': 'l', 't': 'l', '0': 'o', 'd': 'o', '5': 's', '8': 'b',
                       '6': 'g', 'q': 'g'})


def _fold(s):
    return re.sub(r'rn', 'm', str(s or '').lower()).translate(_FOLD)


def _surname(n):
    n = str(n or '').strip()
    return re.sub(r'[^a-z]', '', (n.split(',')[0] if ',' in n else n.split(' ')[0]).lower())


def _similar(a, b):
    """Best of the plain comparison and the OCR-folded one."""
    return max(difflib.SequenceMatcher(None, a, b).ratio(),
               difflib.SequenceMatcher(None, _fold(a), _fold(b)).ratio())


def _forename(n):
    n = str(n or '').strip()
    rest = n.split(',', 1)[1] if ',' in n else ' '.join(n.split(' ')[1:])
    return re.sub(r'[^a-z]', '', rest.lower())


def _name_score(raw, cand):
    """Score a block's reading against a roster name on surname AND forename.

    Surname alone is not enough on these scans: "BARES. PAMELA" scores badly against "Bailey"
    but its forename is a clean "PAMELA", and "Shonod, Jue queline" is only recognisable as
    "Sherrod, Jacqueline" once the forename is read too.
    """
    sl, sr = _surname(raw), _surname(cand)
    fl, fr = _forename(raw), _forename(cand)
    last = _similar(sl, sr) if sl and sr else 0.0
    first = _similar(fl, fr) if fl and fr else 0.0
    whole = _similar(re.sub(r'[^a-z]', '', str(raw).lower()),
                     re.sub(r'[^a-z]', '', str(cand).lower()))
    return max(whole, 0.6 * last + 0.4 * first, last if last > 0.85 else 0.0)


def _adopt_roster_names(emps, roster):
    """Give each block the roster spelling of the name it most resembles, one roster entry at
    most once. Matching is on the surname, which the recogniser gets closest to right, and a
    block that resembles nothing keeps its own reading and is marked unmatched so a reader can
    see it rather than having a wrong name asserted."""
    pool = list(roster)
    for e in emps:
        mine = e.get('name') or ''
        if not _surname(mine) or not pool:
            e['name_matched'] = False
            continue
        best, bestr = None, 0.0
        for cand in pool:
            r = _name_score(mine, cand)
            if r > bestr:
                best, bestr = cand, r
        if best is not None and bestr >= 0.70:
            e['name'] = best
            e['name_matched'] = True
            e['name_score'] = round(bestr, 3)
            pool.remove(best)
        else:
            e['name_matched'] = False
            e['name_score'] = round(bestr, 3)


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
    from concurrent.futures import ThreadPoolExecutor

    def one(png):
        """Rotate and recognise a single page, reusing anything already cached on disk."""
        base = os.path.basename(png)
        rot = os.path.join(d, 'r-' + base)
        if not os.path.exists(rot):
            im = Image.open(png)
            (im if ang == 0 else im.rotate(ang, expand=True)).save(rot)
        oj = os.path.join(d, 'o-' + base.replace('.png', '.json'))
        if not os.path.exists(oj):
            tmp = oj + '.part'
            json.dump(_ocr(rot), open(tmp, 'w'))
            os.replace(tmp, oj)          # never leave a half written cache file behind
        return base, _tokens(json.load(open(oj)))

    # Recognising forty pages one after another is what made a scanned register a twenty minute
    # job on the server. The pages are independent, and the engine is per thread, so read them
    # together. Two workers by default: the box is small and onnxruntime is already threaded.
    workers = max(1, int(os.environ.get('SCANREG_WORKERS', '2')))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages = dict(ex.map(one, pngs))

    emps = []
    for pg, toks in pages.items():
        for blk in _blocks(toks):
            e = _employee(blk, layout)
            if e.get('net') or e.get('tax'):
                e['page'] = pg
                e['name_raw'] = e.get('name') or ''
                e['name'] = _clean_name(e.get('name'))
                emps.append(e)
    # A page header and a totals block both look like an employee to the block finder: they sit
    # in the same place and carry money. Dropping them here rather than downstream keeps the
    # population honest, and the count is what every later gate is measured against.
    _nets = collections.Counter(round(e['net'], 2) for e in emps if e.get('net'))
    _vals = sorted(e['net'] for e in emps if e.get('net'))
    _median = _vals[len(_vals) // 2] if _vals else None
    emps = [e for e in emps if not _is_junk(e, _nets, _median)]

    # OCR will never spell these names reliably; the scans are photocopies. The census already
    # says who is on the register, so match each block to the roster and adopt the roster's
    # spelling. Without this the names reach the reconciliation as "FoStead. Lintsex M. Emp id"
    # and match nothing, which is exactly what happened on the first deployed run.
    roster_names = [nm for nm, _ in (expect_roster or [])] or list(expect_names or [])
    if roster_names:
        _adopt_roster_names(emps, roster_names)

    named = sum(1 for e in emps if e.get('name_matched'))
    report = dict(pdf=pdf_path, rotation=ang, pages=len(pages), employees=len(emps),
                  workdir=d, missing=[], found_by_hunt=[],
                  # How many names the recogniser produced that could be tied to a real person.
                  # A block whose name could not be resolved still carries its money and its
                  # employee number, so it is counted, but it will not match by name downstream
                  # and a reader is entitled to know how many of those there are.
                  names_resolved=named, names_unresolved=len(emps) - named)

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
