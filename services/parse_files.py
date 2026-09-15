"""File ingestion. Spreadsheets are read deterministically. Payroll PDFs are read as text when the PDF carries text,
and through Groq vision when the pages are scans. Nothing here computes a saving.
"""
import io, threading, re, os, unicodedata
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
 'federal': [r'withholding tax', r'federal (income )?tax', r'fitwh', r'fed (w/?h|tax)'],
 'state': [r'^state (income )?tax', r'\bmo\b', r'\bco\b', r'state w/?h'],
 'social_security': [r'fica tax', r'social security', r'^soc$', r'\bsoc\b', r'oasdi'],
 'medicare': [r'medicare tax', r'^med$', r'\bmed\b'],
 'taxable_wages': [r'taxable wages'],
 'medicare_gross': [r'medicare gross'],
 'net_pay': [r'net pay'],
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


def _ocr_once(png):
    try:
        import pytesseract
        from PIL import Image
        # oem 1 runs the LSTM engine only, which is roughly twice as fast as the default that also runs the legacy
        # engine, and psm 6 tells it the page is one block of text, which a payroll statement is.
        with Image.open(io.BytesIO(png)) as im:
            return pytesseract.image_to_string(im, config='--oem 1 --psm 6')
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


def ocr_page(data: bytes, index: int, scale=1.9, hint_box=None):
    """OCR one page. Scanned packs often contain rotated pages, so read each candidate rotation and keep the best.
    hint_box is a one-element list holding the angle that won on an earlier page of the same pack; packs are
    consistently oriented, so trying that angle first usually settles the page on the first pass."""
    from PIL import Image
    png = pdf_page_png(data, index, scale=scale)
    best, best_score, best_angle = '', -1, 0
    order = [0, 180, 90, 270]
    if hint_box and hint_box[0] in order:
        order = [hint_box[0]] + [a for a in order if a != hint_box[0]]
        trusted = True          # a pack is oriented the same way throughout, so one pass is normally enough
    else:
        trusted = False
    for angle in order:
        buf = io.BytesIO()
        with Image.open(io.BytesIO(png)) as im:
            rot = im if angle == 0 else im.rotate(angle, expand=True)
            rot.save(buf, 'PNG')
            if rot is not im:
                rot.close()
        try:
            text = _ocr_once(buf.getvalue())
        except Exception:
            continue
        finally:
            buf.close()
        sc = _orientation_score(text)
        if sc > best_score:
            best, best_score, best_angle = text, sc, angle
        if best_score >= 24:          # a clean upright statement scores well above this
            break
        if trusted and best_score >= 8:   # readable at the pack's known rotation: do not pay for three more passes
            break
    if best_score < 0:
        raise RuntimeError('no OCR engine available')
    if hint_box is not None:
        hint_box[0] = best_angle
    return best


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


def parse_text_paycheck(text):
    """Pull the lines the audit needs out of a statement's text, by label."""
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
    for pat in (r'employee name:?\s*\|?\s*([A-Z][A-Za-z ,.\'-]{3,40})', r'^([A-Z]{2,}, [A-Z][A-Za-z ,.\'-]{2,40})$'):
        m = re.search(pat, text, re.M)
        if m:
            out['name'] = m.group(1).strip(); break
    m = re.search(r'emp(?:loyee)?\s*(?:nbr|no|#|id)[:.]?\s*\|?\s*(\w{2,12})', text, re.I)
    if m:
        out['employee_id'] = m.group(1)
    return out


def _page_record(data, i, text, hint, rot):
    """Read one statement page. Returns a list of records (usually one)."""
    recs = []
    src_kind = 'text layer'
    page_text = text if (text and len(text.strip()) > 120) else ''
    if not page_text:
        try:
            page_text, src_kind = ocr_page(data, i, hint_box=rot), 'OCR'
        except Exception as e:
            return [dict(name=None, source=f'page {i+1}, unreadable: {str(e)[:70]}')]
    base = parse_text_paycheck(page_text)
    need = [k for k in ('name', 'federal', 'net_pay', 'taxable_wages', 'medicare_gross') if base.get(k) is None]
    used_model = False
    if need:
        for attempt in (1, 2):
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
                    for k, v in base.items():          # the regex reading wins wherever it found a value
                        if v is not None:
                            merged[k] = v
                    used_model = True
                    if merged.get('name') or merged.get('employee_id'):
                        merged['source'] = f'page {i+1}, {src_kind}, model filled'
                        merged['fee_from_statement'] = merged.get('fee') is not None
                        recs.append(merged)
                if used_model and recs and (recs[-1].get('name') or recs[-1].get('employee_id')):
                    break
            except Exception as e:
                if attempt == 2:
                    recs.append(dict(**base, source=f'page {i+1}, {src_kind}, model unavailable: {str(e)[:60]}'))
    if not used_model and (base.get('name') or base.get('employee_id')):
        base['source'] = f'page {i+1}, {src_kind}'
        base['fee_from_statement'] = base.get('fee') is not None
        recs.append(base)
    return recs


def paychecks_from_pdf(data: bytes, hint='', max_pages=200, progress=None, workers=None):
    """One record per statement page. The label regex runs first because it is deterministic; Groq fills what the
    regex could not find and names the employee when the layout hides it. Pages with no text layer are OCRd first.
    Pages are independent, so they are read concurrently: OCR waits on the shell and the model call waits on the
    network, and a pack of eighty statements is otherwise almost all waiting."""
    from concurrent.futures import ThreadPoolExecutor
    if workers is None:
        # Each page in flight holds a rendered bitmap and a tesseract process. Three at a time fits the 512 MiB
        # container; the instance was OOM killed at eight.
        workers = int(os.environ.get('PDF_WORKERS', '3'))
    pages = pdf_pages_text(data) or []
    if not pages:
        try:
            import pypdfium2 as pdfium
            pages = [''] * len(pdfium.PdfDocument(io.BytesIO(data)))
        except Exception:
            pages = []
    pages = pages[:max_pages]
    rot, done = [None], [0]   # filled by the first page that OCRs cleanly, then reused by the rest

    def work(arg):
        i, text = arg
        try:
            out = _page_record(data, i, text, hint, rot)
        except Exception as e:
            out = [dict(name=None, source=f'page {i+1}, failed: {str(e)[:70]}')]
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
    if g and td is not None and 0 < td < g:
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
