"""File ingestion. Spreadsheets are read deterministically. Payroll PDFs are read as text when the PDF carries text,
and through Groq vision when the pages are scans. Nothing here computes a saving.
"""
import io, threading, re, os, unicodedata, hashlib, time as _time
import openpyxl
from . import groq_client
from .audit import Census, Engine, Paycheck, EmployeeAudit, r2

NUM = re.compile(r'-?\(?\$?\s*(?:[\d,]+\.\d{2}|\.\d{2})\)?')   # bare .00 appears on Ascender statements


def num(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('$', '').replace(',', '')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def norm(s):
    s = unicodedata.normalize('NFKD', str(s or '')).strip().lower()
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def name_key(first, last):
    return f"{norm(first)} {norm(last)}".strip()


# ---------------------------------------------------------------- spreadsheets
CENSUS_WANTED = ['employee_first_name', 'employee_last_name', 'employee_id', 'state', 'gross_annual_taxable_wages',
                 'pay_frequency', 'federal_w4_marital_status', 'w4_year', 'dependents', 'step2c', 'step3',
                 'group_health_monthly', 'other_monthly_pretax', 'retirement_401k_monthly', 'additional_federal',
                 'additional_state']
REPORT_WANTED = ['first_name', 'last_name', 'client_employee_id', 'annual_salary', 'federal_tax_before_premium',
                 'federal_savings', 'state_savings', 'social_security_savings', 'medicare_savings',
                 'ee_gross_monthly_savings', 'ee_monthly_fee', 'employee_monthly_allotment', 'taxable_income_before',
                 'taxable_income_after', 'wellness_program']


def sheets(data: bytes):
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        yield ws.title, rows


def _header_row(rows):
    for i, r in enumerate(rows[:12]):
        filled = [c for c in r if c not in (None, '')]
        if len(filled) >= 4 and any(isinstance(c, str) for c in filled):
            return i
    return 0


def read_table(data: bytes, wanted, sheet_hint=None, use_ai=True):
    """Return (mapping, records) for the sheet that best matches the requested fields."""
    best = None
    for title, rows in sheets(data):
        if not rows:
            continue
        hi = _header_row(rows)
        headers = [str(h).strip() if h is not None else '' for h in rows[hi]]
        body = [r for r in rows[hi + 1:] if any(c not in (None, '') for c in r)]
        if not headers or not body:
            continue
        score = sum(1 for w in wanted if _direct(w, headers))
        if sheet_hint and sheet_hint.lower() in title.lower():
            score += 5
        if best is None or score > best[0]:
            best = (score, title, headers, body)
    if not best:
        return {}, [], ''
    _, title, headers, body = best
    mapping = {w: _direct(w, headers) for w in wanted}
    missing = [w for w, h in mapping.items() if not h]
    if missing and use_ai:
        try:
            ai = groq_client.map_columns(headers, [dict(zip(headers, r)) for r in body[:3]], missing)
            for k, v in ai.items():
                if v:
                    mapping[k] = v
        except Exception:
            pass
    idx = {h: i for i, h in enumerate(headers)}
    recs = []
    for r in body:
        rec = {}
        for w, h in mapping.items():
            rec[w] = r[idx[h]] if h and idx.get(h) is not None and idx[h] < len(r) else None
        recs.append(rec)
    return mapping, recs, title


SYN = {
 'employee_first_name': ['employee first name', 'first name', 'firstname'],
 'employee_last_name': ['employee last name', 'last name', 'lastname'],
 'employee_id': ['employee id', 'client employee id', 'emp nbr', 'emp id', 'employee number'],
 'client_employee_id': ['client employee id', 'employee id'],
 'first_name': ['first name'], 'last_name': ['last name'],
 'state': ['state', 'work state'], 'gross_annual_taxable_wages': ['gross annual taxable wages', 'annual salary'],
 'annual_salary': ['annual salary', 'salary with buffer'],
 'pay_frequency': ['pay frequency', 'pay periods'],
 'federal_w4_marital_status': ['federal w 4 marital status', 'federal w4 marital status'],
 'w4_year': ['is w 4 2019 or earlier or 2020', 'is w4 2019 or earlier or 2020'],
 'dependents': ['federal w 4 dependents claimed', 'federal w4 dependents claimed'],
 'step2c': ['w 4 2020 is box in step 2c checked y or n', 'is box in step 2c checked'],
 'step3': ['w 4 2020 total of step 3', 'total of step 3'],
 'group_health_monthly': ['group health employee monthly contribution'],
 'other_monthly_pretax': ['other monthly pre taxed deduction amounts', 'other monthly pre taxed deduction'],
 'retirement_401k_monthly': ['401 k ira monthly amount', '401k ira monthly amount'],
 'additional_federal': ['additional federal'], 'additional_state': ['additional state'],
 'federal_tax_before_premium': ['federal tax before premium'], 'federal_savings': ['federal savings'],
 'state_savings': ['state savings'], 'social_security_savings': ['social security savings'],
 'medicare_savings': ['medicare savings'], 'ee_gross_monthly_savings': ['ee gross monthly savings'],
 'ee_monthly_fee': ['ee monthly fee'], 'employee_monthly_allotment': ['employee monthly allotment'],
 'taxable_income_before': ['taxable income before'], 'taxable_income_after': ['taxable income after'],
 'wellness_program': ['wellness program'],
}


def _direct(want, headers):
    for h in headers:
        if norm(h) == norm(want):
            return h
    for syn in SYN.get(want, []):
        for h in headers:
            if norm(h) == norm(syn):
                return h
    for syn in SYN.get(want, []):
        for h in headers:
            if norm(syn) and norm(syn) in norm(h):
                return h
    return None


# ---------------------------------------------------------------- payroll PDFs
LINE_PATTERNS = {
 'gross': [r'standard gross', r'^gross$', r'total gross', r'^gross pay'],
 'federal': [r'w\w{0,6}holding tax', r'federal (income )?tax', r'fitwh', r'fed (w/?h|tax)'],
 'state': [r'^state (income )?tax', r'\bmo\b', r'\bco\b', r'state w/?h'],
 'social_security': [r'fica tax', r'social security', r'^soc$', r'\bsoc\b', r'oasdi'],
 'medicare': [r'medicare tax', r'^med$', r'\bmed\b'],
 'taxable_wages': [r'taxable wages'],
 'medicare_gross': [r'medicare gross'],
 'fica_gross': [r'fica gross'],
 'net_pay': [r'n[eo0]t\s*pay', r'net\s*chec?k'],   # OCR reads Net Pay as Not Pay on scanned packs
 'retirement': [r'trs salary red', r'403\(?b\)?', r'457', r'retirement'],
 'premium': [r'pcm pretax', r'pcmpt', r'pcmp pre tax', r'premium'],
 'reimbursement': [r'simrp'],
 'fee': [r'pcm aftertax', r'pcmat', r'pcmp post tax'],
 'product': [r'\bsia\b', r'supp insurance', r'product'],
 'other_total': [r'total other deduc'],
 'retirement_insurance': [r'trs insurance'],
 'total_deductions': [r'-+total deductions', r'^total deductions'],
}


def pdf_pages_text(data: bytes):
    try:
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(data))
        return [(p.extract_text() or '') for p in rd.pages]
    except Exception:
        return []


# The macOS Vision engine aborts the process when it is called from several threads at once, so every Vision call
# is serialised. Tesseract is a subprocess and stays parallel, which is what runs in the container.
_VISION_LOCK = threading.Lock()


def _layout_from_items(items):
    """Rebuild lines from (centre y, x start, x end, text, height) boxes: group by vertical position, order by x and
    mark column breaks, so a label keeps the amount printed beside it."""
    if not items:
        return ''
    heights = sorted(i[4] for i in items)
    h = heights[len(heights) // 2] or 12
    band, gap = max(4, h * 0.7), max(10, h * 1.6)
    rows = {}
    for cy, x0, x1, t, _ in items:
        rows.setdefault(int(cy / band), []).append((x0, x1, t))
    lines = []
    for k in sorted(rows):
        line, prev = [], None
        for x0, x1, t in sorted(rows[k]):
            if prev is not None and x0 - prev > gap:
                line.append('|')
            line.append(t)
            prev = x1
        lines.append(' '.join(line))
    return '\n'.join(lines)


def _layout_from_words(d):
    """The tesseract reading, in the same line form as the RapidOCR one."""
    items = []
    for i, txt in enumerate(d.get('text', [])):
        if not (txt or '').strip():
            continue
        items.append((d['top'][i] + d['height'][i] / 2, d['left'][i], d['left'][i] + d['width'][i],
                      txt.strip(), d['height'][i]))
    return _layout_from_items(items)


def ocr_selftest():
    """What the container can actually do: which engine, which version, and how long one page takes."""
    import time
    out = {'engine': 'rapidocr' if _rapid_engine() else 'tesseract', 'rapidocr_error': _RAPID_ERROR[0]}
    try:
        import pytesseract
        out['tesseract_version'] = str(pytesseract.get_tesseract_version())
    except Exception as e:
        out['tesseract_version'] = f'unavailable: {type(e).__name__}: {e}'
    try:
        from PIL import Image, ImageDraw
        im = Image.new('L', (700, 120), 'white')
        ImageDraw.Draw(im).text((10, 40), 'FEDERAL W/H 549.35 NET PAY 5141.43', fill=0)
        buf = io.BytesIO(); im.save(buf, 'PNG')
        t = time.time()
        out['sample_text'] = (_ocr_once(buf.getvalue()) or '').strip()[:80]
        out['sample_seconds'] = round(time.time() - t, 2)
    except Exception as e:
        out['sample_text'] = f'failed: {type(e).__name__}: {e}'
    return out


TESS_CONFIG = '--oem 1 --psm 6 -c preserve_interword_spaces=1'
_RAPID = [None]
_RAPID_ERROR = ['']
_RAPID_LOCK = threading.Lock()


_RAPID_LOCAL = threading.local()


def _rapid_engine():
    """The OCR engine, one per thread.

    One shared engine across threads segmentation faults: the runtime session is not safe to call concurrently.
    A session per thread costs memory and nothing else, and the number of threads is bounded by the number of
    pages read at once, which is bounded by the CPUs.
    """
    eng = getattr(_RAPID_LOCAL, 'engine', None)
    if eng is not None:
        return eng or None
    try:
        from rapidocr_onnxruntime import RapidOCR
        n = int(os.environ.get('OCR_THREADS', '1'))
        # The engine's own text angle classifier runs on every box and costs more than the reading itself: on these
        # pages it more than doubled the time per page and read nothing better, and the orientation of a page is
        # settled once for the whole pack anyway. Detection at a smaller side length is the other large saving,
        # measured to read the same figures.
        opts = dict(intra_op_num_threads=n, inter_op_num_threads=n,
                    use_cls=os.environ.get('OCR_USE_CLS', '0') == '1',
                    det_limit_side_len=int(os.environ.get('OCR_DET_SIDE', '576')))
        try:
            eng = RapidOCR(**opts)
        except TypeError:
            eng = RapidOCR()
        _RAPID_ERROR[0] = ''
    except Exception as e:
        _RAPID_ERROR[0] = f'{type(e).__name__}: {e}'[:300]
        eng = False
    _RAPID_LOCAL.engine = eng
    return eng or None


def _rapid_read(png):
    import numpy as np
    from PIL import Image
    eng = _rapid_engine()
    if eng is None:
        return None
    with Image.open(io.BytesIO(png)) as im:
        arr = np.array(im.convert('RGB'))
    res, _ = eng(arr)
    if not res:
        return ''
    items = []
    for box, txt, _score in res:
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        items.append((min(ys) + (max(ys) - min(ys)) / 2, min(xs), max(xs), (txt or '').strip(), max(ys) - min(ys)))
    return _layout_from_items(items)


def _ocr_once(png):
    out = None
    try:
        out = _rapid_read(png)
    except Exception:
        out = None
    if out:
        return out
    try:
        import pytesseract
        from PIL import Image
        # Read words with their positions and rebuild the lines, because a payroll statement is columnar: the flat
        # reading runs the deduction table into the tax lines and merges the amounts.
        with Image.open(io.BytesIO(png)) as im:
            d = pytesseract.image_to_data(im, config=TESS_CONFIG, output_type=pytesseract.Output.DICT)
        return _layout_from_words(d)
    except Exception:
        pass
    with _VISION_LOCK:
        return _vision_ocr(png)


def _vision_ocr(png):
    import tempfile
    from ocrmac import ocrmac
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as t:
        t.write(png); path = t.name
    try:
        res = ocrmac.OCR(path, recognition_level='accurate').recognize()
        items = sorted(res, key=lambda x: (-round(x[2][1] + x[2][3], 3), x[2][0]))
        lines, cur, cy = [], [], None
        for txt, conf, (x, y, w, h) in items:
            top = y + h
            if cy is None or abs(top - cy) > 0.006:
                if cur: lines.append(cur)
                cur, cy = [], top
            cur.append((x, txt))
        if cur: lines.append(cur)
        return '\n'.join('  |  '.join(t for _, t in sorted(l)) for l in lines)
    finally:
        try: os.unlink(path)
        except OSError: pass


def _orientation_score(text):
    """How much a payroll statement this reading looks like. Used to pick the right rotation."""
    low = (text or '').lower()
    score = sum(2 for pats in LINE_PATTERNS.values() for p in pats if re.search(p, low))
    for kw in ('employee name', 'net pay', 'taxable wages', 'medicare', 'withholding', 'gross'):
        score += 3 if kw in low else 0
    return score


def ocr_page(data: bytes, index: int, scale=2.8, hint_box=None):
    """OCR one page. Nearly every pack is upright, so read it that way first and accept the reading when it comes
    back looking like a payroll statement. Only a page that reads as noise is worth paying to rotate, and the angle
    that rescues it is remembered for the rest of the pack."""
    png = pdf_page_png(data, index, scale=scale)
    first = hint_box[0] if (hint_box and hint_box[0] is not None) else 0
    text = _ocr_rotated(png, first)
    if _orientation_score(text) >= 8:
        return text
    best, best_score, best_angle = text, _orientation_score(text), first
    for angle in (0, 180, 90, 270):
        if angle == first:
            continue
        t = _ocr_rotated(png, angle)
        sc = _orientation_score(t)
        if sc > best_score:
            best, best_score, best_angle = t, sc, angle
        if best_score >= 24:
            break
    if hint_box is not None and best_score >= 8 and best_angle != 0:
        hint_box[0] = best_angle          # a pack that is rotated is rotated throughout
    return best


def _ocr_rotated(png, angle):
    from PIL import Image
    buf = io.BytesIO()
    with Image.open(io.BytesIO(png)) as im:
        rot = im if angle == 0 else im.rotate(angle, expand=True)
        rot.save(buf, 'PNG')
        if rot is not im:
            rot.close()
    try:
        return _ocr_once(buf.getvalue())
    except Exception:
        return ''
    finally:
        buf.close()


def _detect_rotation(data, index):
    """Which way up is this page. Tesseract's own orientation detection answers this directly and costs a fraction
    of a read, so ask it first; fall back to reading a mid-size render at each rotation and scoring how much each
    reading looks like a payroll statement. Upright has to be beaten by a clear margin, because a wrong rotation
    turns the whole statement into noise."""
    osd = _osd_rotation(data, index)
    if osd is not None:
        return osd
    mid = pdf_page_png(data, index, scale=1.4)
    scores = {angle: _orientation_score(_ocr_rotated(mid, angle)) for angle in (0, 180, 90, 270)}
    best = max(scores, key=lambda a: scores[a])
    if best != 0 and scores[best] - scores[0] < 4:
        best = 0
    return best


def _osd_rotation(data, index):
    """Tesseract orientation and script detection. Returns the PIL rotation that makes the page upright, or None."""
    try:
        import pytesseract
        from PIL import Image
        png = pdf_page_png(data, index, scale=1.2)
        with Image.open(io.BytesIO(png)) as im:
            osd = pytesseract.image_to_osd(im, output_type=pytesseract.Output.DICT)
        deg = int(osd.get('rotate', 0)) % 360          # degrees clockwise needed to upright the page
        conf = float(osd.get('orientation_conf', 0) or 0)
        if conf < 1.0:
            return None
        return (360 - deg) % 360                        # PIL rotates counter-clockwise
    except Exception:
        return None


def pdf_page_png(data: bytes, index: int, scale=2.0):
    """Render one page as a grayscale PNG. Grayscale is a third of the memory of RGB and OCR reads it just as well,
    which matters because the container has 512 MiB and several pages are in flight at once."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(io.BytesIO(data))
    try:
        bmp = doc[index].render(scale=scale, grayscale=True)
        im = bmp.to_pil()
        buf = io.BytesIO()
        im.save(buf, format='PNG')
        im.close()
        return buf.getvalue()
    finally:
        doc.close()


COMPACT_PATTERNS = {
 'gross': [r'standardgross', r'totalgross', r'grosspay'],
 'federal': [r'w[a-z]{0,4}ho[il1]d[il1]ngtax', r'federa[li](income)?tax', r'fedw[/]?h', r'f[il1]twh'],
 'state': [r'statetax', r'statew[/]?h'],
 'social_security': [r'f[il1]catax', r'soc[il1]a[li]secur[il1]ty', r'oasd[il1]'],
 'medicare': [r'med[il1]caretax', r'med[il1]?cerotax', r'med[a-z]{0,3}caretax'],
 'taxable_wages': [r'taxab[li]ewages'],
 'medicare_gross': [r'med[il1]caregross', r'med[a-z]{0,3}caregross'],
 'fica_gross': [r'f[il1]cagross'],
 'net_pay': [r'n[eo]t?pay', r'netchec?k'],
 'retirement': [r'trssa[li]aryred', r'403b', r'457', r'ret[il1]rement'],
 'retirement_insurance': [r'trs[il1]nsurance'],
 'premium': [r'pcmpretax', r'pcmpt', r'pcmppretax'],
 'reimbursement': [r's[il1]mrp'],
 'fee': [r'pcmaftertax', r'pcmat', r'pcmpposttax'],
 'product': [r'^s[il1]a$', r'suppinsurance'],
 'other_total': [r'totalotherdeduct[il1]?ons'],
 'total_deductions': [r'totaldeduct[il1]?ons'],
}


def _compact(line):
    return re.sub(r'[^a-z0-9]', '', line.lower())


def _first_amount(line, pats=None):
    """The amount printed immediately after this label.

    Splitting the line on its first colon is wrong when a line crosses two tables: the first colon can belong to
    another table's label and the amount taken then belongs to that table. So the label itself is located in the
    line, by matching the patterns against a squeezed form of a sliding window, and the amount taken is the first
    one after where the label ends.
    """
    start = 0
    if pats:
        squeeze = lambda t: re.sub(r'[^a-z0-9]', '', t.lower())
        best = None
        for m in re.finditer(r'[A-Za-z][A-Za-z ./()\-]{2,40}', line):
            if any(re.search(p, squeeze(m.group(0))) for p in pats):
                best = m.end()
                break
        if best is not None:
            start = best
    body = line[start:]
    if start == 0 and ':' in body:
        body = body.split(':', 1)[1]
    for m in NUM.finditer(body):
        v = num(m.group(0))
        if v is not None:
            return v
    return None


def _amount_for(line, label_match):
    """Take the amount adjacent to the label. A statement prints this period next to the label and year to date beyond
    it. OCR of a rotated page reverses the order, so the value can sit immediately to the left instead."""
    hits = [(m.start(), num(m.group(0))) for m in NUM.finditer(line)]
    hits = [(pos, v) for pos, v in hits if v is not None]
    if not hits:
        return None
    lstart, lend = label_match.start(), label_match.end()
    after = [(pos, v) for pos, v in hits if pos >= lend]
    before = [(pos, v) for pos, v in hits if pos < lstart]
    if after:
        return after[0][1]
    if before:
        return before[-1][1]          # closest to the label on a reversed line
    return hits[0][1]


def parse_text_paycheck(text, geometry=None):
    """Pull the lines the audit needs out of a statement's text, by label, then corroborate them.

    `geometry` is the reading produced from the word boxes. Where geometry establishes a figure from the label's own
    row, block and amount column, that figure is the evidence and it wins: reading a line from the text alone
    cannot tell an amount in the neighbouring table from the amount belonging to the label.
    """
    out = {}
    for field, pats in LINE_PATTERNS.items():
        for line in text.split('\n'):
            l = line.lower()
            m = next((mm for p in pats for mm in [re.search(p, l)] if mm), None)
            if not m:
                continue
            v = _amount_for(line, m)
            if v is not None:
                out.setdefault(field, v)
                break
    for field, pats in COMPACT_PATTERNS.items():
        if out.get(field) is not None:
            continue
        for line in text.split('\n'):
            c = _compact(line)
            if not any(re.search(p, c) for p in pats):
                continue
            v = _first_amount(line, pats)
            if v is not None:
                out[field] = v
                break
    for pat in (r'employee\s*name\s*:?\s*\|?\s*([^|\n]{3,60})',
                r'^([A-Z][A-Z\'-]+(?: [A-Z]{2,})?, [A-Z][A-Za-z ,.\'-]{2,40})$'):
        m = re.search(pat, text, re.I | re.M)
        if m:
            nm = re.split(r'\s*pay\s*[cg]am?pus|\s{3,}', m.group(1))[0].strip().rstrip(':,').strip()
            if nm and not re.search(r'campus|status|filing|multi|depend|exempt|w-?4|period|date', nm, re.I):
                out['name'] = nm
                break
    m = re.search(r'emp(?:loyee)?\s*(?:nbr|no|#|id)[:.]?\s*\|?\s*(\w{2,12})', text, re.I)
    if m:
        out['employee_id'] = m.group(1)
    # Geometry before text: where the boxes establish the figure in the label's own block and column, take it.
    for name, gf in (geometry or {}).items():
        val, method = gf.get('value'), gf.get('method')
        if val is None or method != 'label_row_block_column':
            continue
        if out.get(name) is not None and abs(out[name] - val) > 0.02:
            out.setdefault('geometry_corrections', []).append(
                f"{name.replace('_', ' ')} read from the line as {out[name]:.2f}, taken as {val:.2f} from the "
                f"amount column of the block the label sits in")
        out[name] = val
        out.setdefault('from_geometry', []).append(name)
    out.update(_w4_from_statement(text))
    # Whether the wage reduction is retirement is a question for the printed line, not for the arithmetic.
    if re.search(r'trs\s*salary\s*red|403\s*\(?b|457\b|retirement', text, re.I):
        out['retirement_line'] = True
    # Net pay appears several times on one statement: its own line, the direct deposit total, the sum of the
    # individual deposit rows, and gross less total deductions. They are representations of one document, not
    # independent sources, so agreement between two of them is an extraction control and nothing more. Where two
    # agree, the agreed figure is taken, and the figure on the line labelled net pay is preferred among equals:
    # a deposit total can legitimately differ from net pay, because a deposit can be split or partly withheld.
    line_v = out.get('net_pay')
    dep = _deposit_total(text)
    rows = _deposit_rows(text)
    row_sum = r2(sum(rows)) if rows else None
    derived = (r2(out['gross'] - out['total_deductions'])
               if out.get('gross') is not None and out.get('total_deductions') is not None else None)
    cands = [('the line labelled net pay', line_v), ('gross less total deductions', derived),
             ('the deposit total', dep), ('the sum of the deposit rows', row_sum)]
    cands = [(k, v) for k, v in cands if v is not None]
    agreed = None
    for i, (k, v) in enumerate(cands):
        if any(abs(v - w) <= 0.02 for j, (_, w) in enumerate(cands) if j != i):
            agreed = v
            break                          # the list is in order of preference, so the first agreeing one wins
    if agreed is not None:
        if line_v is not None and abs(line_v - agreed) > 0.02:
            # The labelled line and the rest of the page disagree. Taking the majority would be a silent choice
            # between two printed figures, so the majority is used for the arithmetic and the disagreement is
            # reported: the employee is not presented as reconciled on a figure the page itself disputes.
            out['net_pay_disputed'] = dict(line=line_v, corroborated=agreed,
                                           difference=r2(line_v - agreed),
                                           sources=[k for k, v in cands if abs(v - agreed) <= 0.02])
            out['net_pay_corrected'] = (f"the line labelled net pay reads {line_v:.2f} while the rest of the page "
                                        f"gives {agreed:.2f}; the figure used is {agreed:.2f} and the disagreement "
                                        f"is reported")
        out['net_pay'] = agreed
    elif len(cands) > 1:
        out['net_pay_unreliable'] = [v for _, v in cands]
        out['net_pay'] = None
    fed = _federal_from_deductions(out)
    if fed is not None:
        if out.get('federal') is None:
            out['federal'] = fed
            out['federal_derived'] = True
        elif (abs(out['federal'] - fed) > 0.02 and _is_another_line(out, out['federal'])
              and abs(fed) > 0.02 and not _shares_value(out, out['federal'])):
            # The withholding line on these statements is printed beside the deduction table, and the scan sometimes
            # reads a figure from that table instead. Where the value read is exactly one of the other lines on the
            # same statement, the reading is rejected in favour of the corroborated deduction arithmetic.
            out['federal_corrected'] = (f"federal withholding read as {out['federal']:.2f} is the figure printed on "
                                        f"another line of the same statement; the deduction total, which ties to "
                                        f"gross less net pay, leaves {fed:.2f}")
            out['federal'] = fed
        elif abs(out['federal'] - fed) > 0.02:
            out['federal_check'] = f'deduction total implies {fed:.2f}'
        # A disagreement between the withholding line and the printed deduction total is not by itself evidence of
        # a misreading: the totals block prints its own subtotals and the scan reads those columns unevenly. The
        # net pay identity is the check that decides whether an employee is reported as verified.
    if out.get('taxable_wages') is None and out.get('medicare_gross') is not None and out.get('retirement'):
        # A retirement reduction lowers federal taxable wages and leaves Medicare wages alone, so the taxable wages
        # line can be recovered when the scan loses it.
        out['taxable_wages'] = r2(out['medicare_gross'] - out['retirement'])
        out['taxable_wages_derived'] = True
    return out


def _w4_from_statement(text):
    """What the statement itself prints about the employee's W-4, so a withholding instruction can be compared with
    the census rather than inferred from a gap."""
    out = {}
    m = re.search(r'w-?4\s*filing\s*status\s*:?\s*\|?\s*([shmj])', text, re.I)
    if m:
        out['w4_status'] = m.group(1).upper()
    m = re.search(r'w-?4\s*mult[il1]-?\s*job\s*:?\s*\|?\s*([yn])', text, re.I)
    if m:
        out['w4_multijob'] = m.group(1).upper()
    m = re.search(r'w-?4\s*nbr\s*ch[il1]{1,2}dren\s*under\s*17\s*:?\s*\|?\s*(\d{1,2})', text, re.I)
    if m:
        out['w4_children'] = int(m.group(1))
    m = re.search(r'add[a-z]{0,2}\s*w[il1]?thho[il1]d[il1]ng\s*:?\s*\|?\s*([\d,.]+)', text, re.I)
    if m:
        out['w4_extra'] = num(m.group(1))
    return out


def _shares_value(out, v):
    """Is this figure also sitting in one of the deduction components the derivation subtracts.

    When it is, the derivation is circular: the same misread amount appears on both sides, so a derived value of
    nothing is an artefact of the misreading, not evidence about the withholding line.
    """
    for k in ('social_security', 'medicare', 'retirement', 'retirement_insurance', 'other_total'):
        w = out.get(k)
        if w is not None and abs(w - v) <= 0.02:
            return True
    return False


def _is_another_line(out, v):
    """Is this figure one of the other amounts printed on the statement, which is how a column misread shows up."""
    for k in ('premium', 'fee', 'reimbursement', 'product', 'retirement', 'retirement_insurance', 'other_total',
              'medicare', 'social_security'):
        w = out.get(k)
        if w is not None and w != 0 and abs(abs(w) - abs(v)) <= 0.02:
            return True
    return False


def _federal_from_deductions(out):
    """Federal withholding is the printed deduction total less the other printed deductions. The derivation is only
    used where the printed total itself ties to gross less net pay, which means the total and the net pay corroborate
    each other and the remainder is the withholding. On these scans the withholding line often sits beside the
    deduction table and the scan reads a figure from the wrong column, so this arithmetic is the better evidence."""
    total, gross, net = out.get('total_deductions'), out.get('gross'), out.get('net_pay')
    if total is None or out.get('medicare') is None or out.get('other_total') is None:
        return None
    if gross is None or net is None or abs((gross - net) - total) > 0.02:
        return None                      # the printed total is not corroborated, so do not derive from it
    parts = [out.get(k) for k in ('social_security', 'medicare', 'retirement', 'retirement_insurance', 'other_total')]
    v = r2(total - sum(x or 0 for x in parts))
    return v if -0.01 <= v <= gross * 0.45 else None


def _deposit_rows(text):
    """The amounts on the individual bank rows of the deposit block. They sum to the deposit total, which makes the
    total checkable against its own line items rather than against a derived figure."""
    lines = text.split('\n')
    start = next((i for i, l in enumerate(lines) if re.search(r'account\s*(number|type)', l, re.I)), None)
    if start is None:
        return []
    out = []
    for l in lines[start + 1:start + 10]:
        if re.search(r'^\s*-?\s*total\s*:?', l, re.I):
            break
        if re.search(r'checking|savings|account|bank|\(\d{3}\)', l, re.I):
            vals = [num(m.group(0)) for m in NUM.finditer(l)]
            vals = [v for v in vals if v and v > 1]
            if vals:
                out.append(vals[-1])          # the amount column sits at the end of the row
    return out


def _deposit_total(text):
    """The direct deposit block ends in a total, and that total is the net pay. Used when the printed net pay line
    itself did not survive the scan."""
    lines = text.split('\n')
    start = next((i for i, l in enumerate(lines) if re.search(r'account (number|type)', l, re.I)), None)
    if start is None:
        return None
    for l in lines[start + 1:start + 12]:
        m = re.search(r'^\s*-?\s*total\s*:?', l, re.I)
        if m:
            v = _amount_for(l, m)
            if v:
                return v
    return None


_GROQ_OPEN = [True]


def _groq_open():
    """Whether the model is still worth asking. One rate limited call closes it for the rest of the run: waiting
    on a metered key page after page costs minutes and reads nothing the page did not already give."""
    return _GROQ_OPEN[0]


_MODEL_BUDGET = [int(os.environ.get('GROQ_PAGE_BUDGET', '15'))]   # how many pages a single run may send to the model
_BUDGET_LOCK = threading.Lock()


def _take_model_budget():
    with _BUDGET_LOCK:
        if _MODEL_BUDGET[0] <= 0:
            return False
        _MODEL_BUDGET[0] -= 1
        return True


def _geometry_read(data, i, text_layer_words=None, png=None):
    """Read the page by geometry: label gives the row, block gives the side, x position gives the column.

    Returns (record fields, PageReading) or (None, None) when the page yields no words at all. The label patterns
    are the same configurable vocabulary the text reader uses, so nothing about the layout is hard coded.
    """
    from . import pageread as PR
    words = text_layer_words
    if not words:
        if png is None:
            return None, None
        words = PR.words_from_ocr(png)
    if not words:
        return None, None
    labels = {k: [p for p in (LINE_PATTERNS.get(k, []) + COMPACT_PATTERNS.get(k, []))]
              for k in set(LINE_PATTERNS) | set(COMPACT_PATTERNS)}
    reading = PR.read_page(words, labels, prefer_column=0)
    out = {k: f.value for k, f in reading.fields.items() if f.value is not None}
    return out, reading


def _page_record(data, i, text, hint, rot, source_name='', source_sha='', store_stats=None):
    """Read one statement page once, and only once.

    Order of business: establish the page's identity, ask the store whether this exact page has been read before,
    and only then spend anything on reading it. A stored reading comes back with any human correction applied on
    top of the machine values, and the machine values are never overwritten.
    """
    from . import pageread as PR
    from . import pagestore as PS

    key = PS.page_key(data, i, PR.EXTRACTOR_VERSION)
    hit = PS.load(key)
    kind = 'exact'
    png = None
    text_words = PR.words_from_text_layer(data, i)
    if hit is None:
        png = pdf_page_png(data, i, scale=2.8)
        dh = PS.dhash(png)
        dims = _png_dims(png)
        toks = PS.identity_tokens(text or '')
        hit = PS.find_equivalent(dh, toks, dims, PR.EXTRACTOR_VERSION)
        kind = 'equivalent' if hit else 'miss'
    if hit is not None:
        rec = dict((hit.get('reading') or {}).get('record') or {})
        eff = PS.effective_fields(hit)
        for name, e in eff.items():
            if e.get('verification') == 'human':
                rec[name] = e.get('effective_value')
                rec.setdefault('corrections_applied', []).append(
                    f"{name.replace('_', ' ')} was corrected by {e['correction'].get('user')} to "
                    f"{e.get('effective_value')} ({e['correction'].get('reason') or 'no reason recorded'}); the "
                    f"machine read {e.get('machine_value')}")
        if rec.get('name') or rec.get('employee_id'):
            rec['source'] = (rec.get('source') or f'page {i+1}') + f', read from the page store ({kind} match)'
            if store_stats is not None:
                store_stats[kind] = store_stats.get(kind, 0) + 1
            return [rec]

    # not in the store: read it, once. The words carry their boxes, and the page's text is those words in lines,
    # so the page is never put through the reader twice.
    # Whether the page reads at all, which is all the orientation question needs. Asking whether the audit's own
    # fields were found is a much stronger test, and paying four OCR passes on every page that happens to print
    # fewer of them is how a five minute run became a twenty minute one.
    def reads_ok(ws):
        if not ws:
            return False
        wordish = sum(1 for w in ws if re.search(r'[A-Za-z]{3}', w.text))
        amounts = sum(1 for w in ws if re.search(r'\d[\d,]*\.\d{2}', w.text))
        return wordish >= 15 and amounts >= 3

    try:
        hint = rot[0] if rot and rot[0] is not None else None
        # Only the first page of a pack pays for settling the orientation; the rest of the pack is the same way up,
        # and a page that then reads as nothing is reported as such rather than re-read at every angle.
        words, angle, wsrc = (text_words, 0, 'text layer') if text_words else PR.words_for_page(
            data, i, prefer=hint, reads_ok=(reads_ok if hint is None else None))
    except Exception as e:
        return [dict(name=None, source=f'page {i+1}, unreadable: {str(e)[:70]}')]
    if rot is not None:
        rot[0] = angle                     # a pack is oriented the same way throughout, zero included
    src_kind = wsrc
    geo_fields, reading = _geometry_read(data, i, text_layer_words=words, png=None)
    page_text = reading.text if reading is not None else (text or '')
    if len((page_text or '').strip()) < 40:
        return [dict(name=None, source=f'page {i+1}, no text recovered from the page')]
    geo = {k: dict(value=v.value, method=v.method, confidence=v.confidence, note=v.note)
           for k, v in (reading.fields.items() if reading else [])}
    base = parse_text_paycheck(page_text, geometry=geo)
    # The model is the last resort for a page the reader could not make sense of, not a way to top up a page that
    # read. Calling it whenever any single figure is missing is what turned a five minute run into eight: the key
    # is metered, the calls queue behind one another, and a rate limited call waits. So it is asked only when the
    # page has no identity, or when neither the withholding nor the net pay came out of it.
    unreadable = ((base.get('name') is None and base.get('employee_id') is None)
                  or (base.get('federal') is None and base.get('net_pay') is None))
    need = [k for k in ('name', 'federal', 'net_pay') if base.get(k) is None]
    recs, used_model = [], False
    if unreadable and _groq_open() and _take_model_budget():
        try:
            for r in groq_client.structure_paycheck_text(page_text, hint=hint):
                cand = dict(name=r.get('employee_name'), employee_id=r.get('employee_id'),
                            gross=num(r.get('gross')), federal=num(r.get('federal_withholding')),
                            state=num(r.get('state_withholding')), state_code=r.get('state_code'),
                            social_security=num(r.get('social_security')), medicare=num(r.get('medicare')),
                            taxable_wages=num(r.get('taxable_wages')), medicare_gross=num(r.get('medicare_gross')),
                            net_pay=num(r.get('net_pay')), premium=num(r.get('premium_pretax')),
                            reimbursement=num(r.get('reimbursement')), fee=num(r.get('employee_fee_aftertax')),
                            product=num(r.get('product_sold')), retirement=num(r.get('retirement')),
                            cafeteria=num(r.get('cafeteria_pretax')), other_deductions=num(r.get('other_deductions')))
                merged = dict(cand)
                for k, v in base.items():                 # anything read from the page itself wins
                    if v is not None:
                        merged[k] = v
                used_model = True
                if merged.get('name') or merged.get('employee_id'):
                    merged['source'] = f'page {i+1}, {src_kind}, model filled'
                    recs.append(merged)
        except Exception as e:
            if 'rate limit' in str(e).lower():
                _GROQ_OPEN[0] = False      # the key is metered; stop queueing behind it for the rest of the run
            recs.append(dict(**base, source=f'page {i+1}, {src_kind}, model unavailable: {str(e)[:60]}'))
    if not recs and (base.get('name') or base.get('employee_id')):
        base['source'] = f'page {i+1}, {src_kind}' + (', geometry' if geo_fields else '')
        recs.append(base)
    table = None
    if not recs:
        # The page is not one person's statement. Try it as a table of people, and use that reading only if it
        # holds together arithmetically: gross less the amounts withheld should equal net pay on each row. A table
        # reading that does not tie is not reported as figures, it is reported as a page that could not be read.
        table, gate = _table_read(words)
        if table is not None and gate['accepted']:
            for r in table.employees():
                rec = _record_from_table_row(r, i, table)
                if rec:
                    recs.append(rec)
        elif table is not None:
            return [dict(name=None, source=f'page {i+1}, {wsrc}, read as a table of {gate["rows"]} rows but the '
                                          f'reading does not tie on {gate["does_not_tie"]} of them, so no figures '
                                          f'are taken from it')]
    for r in recs:
        r['fee_from_statement'] = r.get('fee') is not None
        r['page_key'] = key

    # store what was read, with the boxes behind it, so this page is never read again
    if recs and reading is not None:
        try:
            if png is None:
                png = pdf_page_png(data, i, scale=2.8)
            rd = reading.as_record()
            rd['record'] = {k: v for k, v in recs[0].items() if not k.startswith('_')}
            rd['page_text'] = reading.text[:20000]
            PS.save(PS.new_record(key, source_name, i, source_sha, PS.dhash(png), _png_dims(png),
                                  PS.identity_tokens(page_text), ('text layer' if text_words else 'RapidOCR'),
                                  PR.EXTRACTOR_VERSION, rd, ocr_version=_ocr_version()))
            if store_stats is not None:
                store_stats['read'] = store_stats.get('read', 0) + 1
        except Exception as e:
            recs[0]['source'] = (recs[0].get('source') or '') + f' | not stored: {str(e)[:60]}'
    return recs


def _ocr_version():
    try:
        import rapidocr_onnxruntime as R
        return 'rapidocr ' + str(getattr(R, '__version__', 'unknown'))
    except Exception:
        try:
            import pytesseract
            return 'tesseract ' + str(pytesseract.get_tesseract_version())
        except Exception:
            return 'unknown'


def _looks_read(reading):
    """Did this reading find the page at all: a couple of the requested figures and something that reads as a name.
    Used to decide whether to try the other orientation, not to judge the figures themselves."""
    got = sum(1 for f in reading.fields.values() if f.value is not None)
    has_words = len([w for w in reading.words if re.search(r'[A-Za-z]{3}', w.text)]) > 20
    return got >= 3 and has_words


def _table_read(words):
    """Read the page as a table of people, upright and transposed, and say whether the reading may be trusted.

    A matrix that prints its labels down the left and one person per column becomes, transposed, a table with a
    header row, so both orientations are tried and the better scoring one is taken. The gate is the page's own
    arithmetic: a reading whose rows do not tie is not used.
    """
    from . import tableread as TR
    from . import pageread as PR
    if not words:
        return None, dict(accepted=False, rows=0, does_not_tie=0)
    best = None
    for ws, how in ((words, 'as printed'), (PR.transpose_words(words), 'transposed')):
        try:
            t = TR.read_table(ws)
        except Exception:
            continue
        if t.is_table and (best is None or t.score > best.score):
            t.note = (t.note or '') + f' read {how}'
            best = t
    if best is None:
        return None, dict(accepted=False, rows=0, does_not_tie=0)
    coh = TR.coherence(best)
    rows = coh.get('rows', 0)
    ties = coh.get('ties', 0)
    accepted = rows >= 2 and ties >= max(2, int(rows * 0.6))
    return best, dict(accepted=accepted, rows=rows, ties=ties,
                      does_not_tie=coh.get('does_not_tie', rows - ties))


def _record_from_table_row(row, i, table):
    """One employee row of a table, as a paycheck record."""
    name = (row.identity or '').strip()
    if not name and not row.identity_id:
        return None
    rec = {k: v for k, v in row.values.items()
           if k in ('gross', 'federal', 'state', 'social_security', 'medicare', 'net_pay', 'taxable_wages',
                    'medicare_gross', 'premium', 'fee', 'reimbursement', 'retirement')}
    rec['name'] = name
    if row.identity_id:
        rec['employee_id'] = row.identity_id
    rec['fee_from_statement'] = rec.get('fee') is not None
    rec['source'] = (f'page {i+1}, table row {row.index}, columns '
                     + ', '.join(c.name for c in table.columns))
    return rec


def _png_dims(png):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(png)) as im:
            return list(im.size)
    except Exception:
        return [0, 0]


def paychecks_from_pdf(data: bytes, hint='', max_pages=200, progress=None, workers=None, source_name='',
                       store_stats=None):
    """One record per statement page. The label regex runs first because it is deterministic; Groq fills what the
    regex could not find and names the employee when the layout hides it. Pages with no text layer are OCRd first.
    Pages are independent, so they are read concurrently: OCR waits on the shell and the model call waits on the
    network, and a pack of eighty statements is otherwise almost all waiting."""
    from concurrent.futures import ThreadPoolExecutor
    if workers is None:
        # One page in flight per CPU, taken from the machine rather than assumed: the OCR engine is compute bound
        # and pinned to one thread, so more pages at once only adds contention, and fewer wastes a core.
        workers = int(os.environ.get('PDF_WORKERS', '0')) or max(2, min(4, (os.cpu_count() or 2)))
    pages = pdf_pages_text(data) or []
    if not pages:
        try:
            import pypdfium2 as pdfium
            pages = [''] * len(pdfium.PdfDocument(io.BytesIO(data)))
        except Exception:
            pages = []
    pages = pages[:max_pages]
    src_sha = hashlib.sha256(data).hexdigest()
    _MODEL_BUDGET[0] = int(os.environ.get('GROQ_PAGE_BUDGET', '15'))
    _GROQ_OPEN[0] = True
    rot, done = [None], [0]   # filled by the first page that OCRs cleanly, then reused by the rest
    if progress:
        progress(0, len(pages))   # say how many pages there are before the first one finishes

    def work(arg):
        i, text = arg
        t0 = _time.time()
        try:
            out = _page_record(data, i, text, hint, rot, source_name=source_name, source_sha=src_sha,
                               store_stats=store_stats)
        except Exception as e:
            out = [dict(name=None, source=f'page {i+1}, failed: {str(e)[:70]}')]
        print(f'[read] {hint or "pack"} page {i+1}/{len(pages)} {_time.time() - t0:.1f}s '
              f'{(out[0].get("name") if out else None) or "no name"}', flush=True)
        done[0] += 1
        if progress:
            progress(done[0], len(pages))
        return out

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pages) or 1))) as ex:
        batches = list(ex.map(work, list(enumerate(pages))))
    recs = [r for b in batches for r in b]
    return [r for r in recs if r.get('name') or r.get('employee_id') or r.get('federal') is not None]


def paychecks_from_sheet(data: bytes):
    """A payroll journal exported as a spreadsheet: one row per employee."""
    wanted = ['employee_first_name', 'employee_last_name', 'employee_id', 'gross', 'federal_withholding',
              'state_withholding', 'social_security', 'medicare', 'taxable_wages', 'medicare_gross', 'net_pay']
    mapping, recs, title = read_table(data, wanted)
    out = []
    for r in recs:
        nm = name_key(r.get('employee_first_name'), r.get('employee_last_name')) or (r.get('employee_id') or '')
        if not nm.strip():
            continue
        out.append(dict(name=nm, employee_id=r.get('employee_id'), gross=num(r.get('gross')),
                        federal=num(r.get('federal_withholding')), state=num(r.get('state_withholding')),
                        social_security=num(r.get('social_security')), medicare=num(r.get('medicare')),
                        taxable_wages=num(r.get('taxable_wages')), medicare_gross=num(r.get('medicare_gross')),
                        net_pay=num(r.get('net_pay')), source=f'sheet {title}'))
    return out


PLAUSIBLE = {  # field: (low, high) as a fraction of gross. A structured value outside this range is dropped.
 'federal': (0, 0.45), 'state': (0, 0.20), 'social_security': (0, 0.07), 'medicare': (0, 0.03),
 'taxable_wages': (0.10, 1.05), 'medicare_gross': (0.20, 1.05), 'net_pay': (0.25, 1.00),
 'retirement': (0, 0.30), 'cafeteria': (0, 0.40), 'premium': (0, 0.60), 'fee': (0, 0.20)}


def anchor_and_solve(rec, gross_anchor=None):
    """Two deterministic repairs for a noisy scan.
    Gross: a statement prints this period beside year to date, so anchor on the census gross per pay period.
    Federal withholding: where the printed value breaks the net pay identity, solve the identity for it.
    Both repairs are recorded in the record source so the report can say the value was derived."""
    notes = []
    g, net = num(rec.get('gross')), num(rec.get('net_pay'))
    if gross_anchor:
        if g is None or abs(g - gross_anchor) > max(1.0, 0.02 * gross_anchor):
            if g is not None and abs(g / gross_anchor - round(g / gross_anchor)) < 0.02 and g > gross_anchor:
                notes.append(f'gross {g} looked like a year to date column, replaced with {round(gross_anchor, 2)} from the census')
            elif g is None:
                notes.append(f'gross taken from the census as {round(gross_anchor, 2)}')
            else:
                notes.append(f'gross {g} differs from the census {round(gross_anchor, 2)}, census used')
            rec['gross'] = round(gross_anchor, 2)
            g = rec['gross']
    td = num(rec.get('total_deductions'))
    if rec.get('net_pay_unreliable'):
        # The three printings of net pay disagreed, so nothing here may stand in for the figure: the employee is
        # reported unverified instead of reconciled against a derived number.
        notes.append('net pay printings disagreed (' + ', '.join(f'{v:.2f}' for v in rec['net_pay_unreliable'])
                     + '); no net pay taken')
    elif g and td is not None and 0 < td < g:
        implied_net = round(g - td, 2)
        if net is None or not (g * 0.25 <= net <= g):
            notes.append(f'net pay {net} replaced with {implied_net}, gross minus the printed total deductions')
            rec['net_pay'] = implied_net
            net = implied_net
    comps = ['state', 'social_security', 'medicare', 'retirement', 'retirement_insurance', 'other_total']
    known = {k: num(rec.get(k)) for k in comps}
    if g and net is not None and all(v is not None for k, v in known.items() if k in ('medicare', 'other_total')):
        implied = g - net - sum(v or 0 for v in known.values())
        implied = round(implied, 2)
        read = num(rec.get('federal'))
        read_ok = read is not None and 0 <= read <= g * 0.45
        if read_ok and -1 <= implied <= g * 0.35 and abs(read - implied) > 1.0:
            rec['federal_unreliable'] = round(abs(read - implied), 2)
            notes.append(f'federal withholding {read} does not satisfy the net pay identity, which implies about '
                         f'{max(implied, 0.0)}; the line is reported as unreadable rather than assumed')
        if -1 <= implied <= g * 0.35 and not read_ok:      # never overwrite a printed value that is itself plausible
                notes.append(f'federal withholding {read} did not satisfy the net pay identity, derived {max(implied, 0.0)} instead')
                rec['federal'] = max(implied, 0.0)
    if notes:
        rec['source'] = (rec.get('source') or '') + ' | ' + '; '.join(notes)
    return rec, notes


def validate(rec):
    """Drop any structured amount that cannot be right for this gross. Records what was dropped."""
    g = num(rec.get('gross'))
    dropped = []
    if not g or g <= 0:
        return rec, dropped
    for k, (lo, hi) in PLAUSIBLE.items():
        v = num(rec.get(k))
        if v is None:
            continue
        if v < 0:
            continue
        if not (g * lo - 0.01 <= abs(v) <= g * hi + 0.01):
            dropped.append(f"{k}={v}")
            rec[k] = None
    if dropped:
        rec['source'] = (rec.get('source') or '') + ' | dropped as implausible: ' + ', '.join(dropped)
    return rec, dropped


def to_paycheck(rec) -> Paycheck:
    if not rec:
        return Paycheck()
    g = lambda k: num(rec.get(k))
    pc = Paycheck(gross=g('gross'), federal=g('federal'), state=g('state'), state_code=(rec.get('state_code') or ''),
                    social_security=g('social_security'), medicare=g('medicare'), taxable_wages=g('taxable_wages'),
                    medicare_gross=g('medicare_gross'), net_pay=g('net_pay'), premium=g('premium'),
                    reimbursement=g('reimbursement'), fee=g('fee'), product=g('product'), retirement=g('retirement'),
                    cafeteria=g('cafeteria'), other_deductions=g('other_deductions'), source=rec.get('source', ''))
    pc.federal_unreliable = rec.get('federal_unreliable')
    for k in ('w4_status', 'w4_multijob', 'w4_children', 'w4_extra', 'retirement_line', 'other_total',
              'total_deductions', 'net_pay_corrected', 'net_pay_unreliable', 'net_pay_disputed'):
        setattr(pc, k, rec.get(k))
    return pc


def fuzzy_index(records):
    """Index payroll records by employee id and by name, so a first name printed as GARRAL still finds GARRA."""
    by_id, by_name, by_last = {}, {}, {}
    for r in records:
        eid = str(r.get('employee_id') or '').strip().lstrip('0')
        if eid:
            by_id.setdefault(eid, r)
        k = match_key(r)
        if k:
            by_name.setdefault(k, r)
            parts = k.split()
            if len(parts) > 1:
                by_last.setdefault((parts[-1], parts[0][:3]), r)
    return by_id, by_name, by_last


def find(rec_id, first, last, by_id, by_name, by_last):
    eid = str(rec_id or '').strip().lstrip('0')
    if eid and eid in by_id:
        return by_id[eid]
    k = name_key(first, last)
    if k in by_name:
        return by_name[k]
    parts = k.split()
    if len(parts) > 1:
        return by_last.get((parts[-1], parts[0][:3]))
    return None


def match_key(rec):
    nm = norm(rec.get('name') or '')
    if ',' in (rec.get('name') or ''):
        last, first = [p.strip() for p in rec['name'].split(',', 1)]
        nm = name_key(first.split()[0] if first.split() else '', last)
    else:
        parts = nm.split()
        nm = f"{parts[0]} {parts[-1]}" if len(parts) > 1 else nm
    return nm
