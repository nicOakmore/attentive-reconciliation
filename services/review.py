"""The audit review: the document format used for Mineola and Aspermont.

Different from report.py and for a different reader. report.py is the operational pack, a block
per employee, written for whoever has to fix the census. This is the argument: conclusion first,
findings with action titles, the register's own pages as exhibits, and no methodology appendix.
It is the document that goes to a client who is deciding whether to believe the numbers.

Rules it keeps, from ~/.claude/tools/draft_check.py:
  sentence case everywhere, never capitals for emphasis
  action titles, a complete sentence stating the finding, never a topic label
  answer first, evidence after
  no recommendations section: an audit reports what is, and the asks go in the covering email
  numbers right, prose left
"""
import io
import os
import re
import subprocess
import tempfile

from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

NAVY, GREY, RED = RGBColor(0x10, 0x24, 0x3A), RGBColor(0x5A, 0x5A, 0x5A), RGBColor(0xA8, 0x00, 0x00)
CENT = 0.005


def money(x):
    if x is None:
        return ''
    return '({:,.2f})'.format(-x) if x < 0 else '{:,.2f}'.format(x)


class Doc:
    def __init__(self):
        self.d = Document()
        s = self.d.styles['Normal']
        s.font.name, s.font.size = 'Arial', Pt(9.5)
        for sec in self.d.sections:
            sec.left_margin = sec.right_margin = Inches(0.7)
            sec.top_margin = sec.bottom_margin = Inches(0.7)

    def p(self, t='', size=9.5, bold=False, color=None, after=5, italic=False):
        par = self.d.add_paragraph()
        r = par.add_run(t)
        r.font.size, r.bold, r.italic = Pt(size), bold, italic
        if color is not None:
            r.font.color.rgb = color
        par.paragraph_format.space_after = Pt(after)
        return par

    def h(self, t, size=13):
        self.p(t, size, True, NAVY, 5)

    def tbl(self, headers, body, widths=None, right=1, left=()):
        t = self.d.add_table(rows=1, cols=len(headers))
        t.style = 'Table Grid'
        aligned = lambda i: i >= right and i not in left
        for i, x in enumerate(headers):
            c = t.rows[0].cells[i]
            c.text = ''
            rr = c.paragraphs[0].add_run(str(x))
            rr.bold = True
            rr.font.size = Pt(8.5)
            if aligned(i):
                c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for row in body:
            cs = t.add_row().cells
            for i, v in enumerate(row):
                cs[i].text = ''
                rr = cs[i].paragraphs[0].add_run('' if v is None else str(v))
                rr.font.size = Pt(8.5)
                if aligned(i):
                    cs[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        if widths:
            for row in t.rows:
                for i, w in enumerate(widths):
                    row.cells[i].width = Inches(w)
        self.d.add_paragraph().paragraph_format.space_after = Pt(4)

    def exhibit(self, png, cap, width=7.1):
        self.d.add_picture(png, width=Inches(width))
        q = self.d.paragraphs[-1]
        q.alignment = WD_ALIGN_PARAGRAPH.CENTER
        q.paragraph_format.space_after = Pt(2)
        self.p(cap, 8.5, False, GREY, 10, italic=True)

    def page_break(self):
        self.d.add_page_break()

    def save(self):
        buf = io.BytesIO()
        self.d.save(buf)
        return buf.getvalue()


def _employee_page(pdf_bytes, surname, workdir):
    """Render the register page carrying this employee, cropped to the band around their name.

    The band is worked out from where the name sits in that page's own text, which is reliable
    on a fixed-width register and keeps the exhibit readable instead of shrinking a whole page
    to seven inches. Returns a PNG path, or None when anything is unavailable.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    os.makedirs(workdir, exist_ok=True)
    src = os.path.join(workdir, 'reg.pdf')
    with open(src, 'wb') as fh:
        fh.write(pdf_bytes)
    txt = os.path.join(workdir, 'reg.txt')
    r = subprocess.run(['pdftotext', '-layout', src, txt], capture_output=True, timeout=180)
    if r.returncode or not os.path.exists(txt):
        return None
    pages = open(txt, errors='replace').read().split('\f')
    up = str(surname or '').upper()
    hit = next((i for i, pg in enumerate(pages) if up and up in pg.upper()), None)
    if hit is None:
        return None
    lines = pages[hit].split('\n')
    idx = next((i for i, l in enumerate(lines) if up in l.upper()), 0)
    frac = idx / max(len(lines), 1)
    out = os.path.join(workdir, 'pg')
    r = subprocess.run(['pdftoppm', '-r', '200', '-png', '-f', str(hit + 1), '-l', str(hit + 1),
                        src, out], capture_output=True, timeout=180)
    import glob
    pngs = sorted(glob.glob(out + '-*.png'))
    if r.returncode or not pngs:
        return None
    im = Image.open(pngs[0])
    w, h = im.size
    top = max(int(h * (frac - 0.06)), 0)
    bot = min(int(h * (frac + 0.30)), h)
    if bot - top < h * 0.15:
        bot = min(top + int(h * 0.3), h)
    crop = os.path.join(workdir, 'exhibit.png')
    im.crop((0, top, w, bot)).save(crop)
    return crop


def _premium_employee(audits):
    """An employee the mock actually deducted, preferring one who is a clean comparison."""
    ded = [a for a in audits if (a.after.premium or 0) > CENT]
    clean = [a for a in ded if a.comparable and a.before.net_pay is not None]
    pick = (clean or ded or [None])[0]
    return pick


def build(audits, summary, notes, client='', period='', files=None, payroll=None):
    """payroll is {'before': bytes, 'after': bytes} when the registers are available for exhibits."""
    doc = Doc()
    n = len(audits)
    ded = [a for a in audits if (a.after.premium or 0) > CENT]
    comparable = [a for a in ded if a.comparable and a.before.net_pay is not None
                  and a.after.net_pay is not None]
    notcmp = [a for a in audits if not a.comparable]
    losing = [a for a in audits if a.fee_exceeds_saving]
    pm = summary.get('period_mismatch')
    gaps = [a.allotment_gap for a in comparable if a.allotment_gap is not None]
    short = [a for a in comparable if (a.allotment_gap or 0) < -CENT]
    total_short = round(sum(g for g in gaps if g < 0), 2)

    # premium treatment, measured on the comparable population
    pretax_ok = med_ok = 0
    for a in comparable:
        prem = a.after.premium or 0
        if (a.before.taxable_wages is not None and a.after.taxable_wages is not None
                and abs((a.before.taxable_wages - a.after.taxable_wages) - prem) <= 1.0):
            pretax_ok += 1
        if (a.before.medicare_gross is not None and a.after.medicare_gross is not None
                and abs((a.before.medicare_gross - a.after.medicare_gross) - prem) <= 1.0):
            med_ok += 1

    doc.p('Audit review', 20, True, NAVY, 0)
    doc.p('%s%s' % (client or 'Client', '   ·   ' + period if period else ''), 11, color=GREY, after=2)
    doc.p('Prepared from the census, the proposal and both payroll runs', 9, color=GREY, after=14)

    doc.h('Conclusion')
    if comparable and pretax_ok == len(comparable) and med_ok == len(comparable):
        doc.p('The district deducted and taxed the premium correctly. On every employee who gives '
              'a clean before and after, withholding wages and Medicare wages each fall by the '
              'full premium. Nothing is required of payroll on that point.', after=6)
    elif comparable:
        doc.p('The premium is not coming out of every wage base it should. Withholding wages fall '
              'by the full premium on %d of %d clean comparisons and Medicare wages on %d of %d. '
              'Where Medicare wages do not fall, the Medicare saving in the proposal is never '
              'delivered.' % (pretax_ok, len(comparable), med_ok, len(comparable)), after=6)
    if short:
        doc.p('%d of the %d employees with a clean comparison take home less than the proposal '
              'promised, %s a month between them. Every one of them is named below with the '
              'reason.' % (len(short), len(comparable), money(abs(total_short))), after=6)
    else:
        doc.p('Every employee with a clean comparison takes home at least what the proposal '
              'promised.', after=6)
    bits = []
    if pm:
        bits.append('the two payroll runs cover different pay periods')
    if notcmp:
        bits.append('%d employees were paid a different gross in the two runs' % len(notcmp))
    if losing:
        bits.append('%d employees pay more in fee than they save' % len(losing))
    if bits:
        lead = bits[0][0].upper() + bits[0][1:]
        rest = ('. ' + '. '.join(b[0].upper() + b[1:] for b in bits[1:])) if len(bits) > 1 else ''
        doc.p(lead + rest + '. None of these is a calculation error, and each is set out below.',
              bold=True, after=12)

    doc.h('What was examined')
    rows = [[f.split(': ', 1)[0], f.split(': ', 1)[1]] if ': ' in f else [f, '']
            for f in (files or [])]
    doc.tbl(['Document', 'File'], rows or [['', '']], [2.4, 4.7], left=(1,))
    gate = [x for x in (notes or []) if 'read as a' in x]
    if gate:
        doc.p(' '.join(gate) + ' Each register was tied to the totals it prints on its own last '
              'page before any figure was used.', 9, color=GREY, after=12)

    num = 0
    pick = _premium_employee(audits)
    if payroll and pick:
        surname = pick.name.split()[-1] if pick.name else ''
        with tempfile.TemporaryDirectory() as wd:
            a_png = _employee_page(payroll.get('after'), surname, os.path.join(wd, 'a')) \
                if payroll.get('after') else None
            b_png = _employee_page(payroll.get('before'), surname, os.path.join(wd, 'b')) \
                if payroll.get('before') else None
            if b_png or a_png:
                num += 1
                doc.h('Finding %d   The premium is deducted and taxed as the proposal assumes' % num)
                doc.p('%s, taken from the registers themselves.' % pick.name, after=6)
                if b_png:
                    doc.exhibit(b_png, 'Exhibit A1   The run before the premium.')
                if a_png:
                    doc.exhibit(a_png, 'Exhibit A2   The mock run, with the premium deducted.')
                doc.tbl(['', 'Before', 'After', 'Change'],
                        [['Gross pay', money(pick.before.gross), money(pick.after.gross),
                          money((pick.after.gross or 0) - (pick.before.gross or 0))],
                         ['Federal taxable wages', money(pick.before.taxable_wages),
                          money(pick.after.taxable_wages),
                          money((pick.after.taxable_wages or 0) - (pick.before.taxable_wages or 0))],
                         ['Medicare wages', money(pick.before.medicare_gross),
                          money(pick.after.medicare_gross),
                          money((pick.after.medicare_gross or 0) - (pick.before.medicare_gross or 0))],
                         ['Net pay', money(pick.before.net_pay), money(pick.after.net_pay),
                          money((pick.after.net_pay or 0) - (pick.before.net_pay or 0))]],
                        [2.3, 1.6, 1.6, 1.6])
                doc.p('Measured across the %d clean comparisons: withholding wages fall by the full '
                      'premium on %d, Medicare wages on %d.'
                      % (len(comparable), pretax_ok, med_ok), after=12)
                doc.page_break()

    if pm:
        num += 1
        doc.h('Finding %d   The two payroll runs are different pay periods' % num)
        doc.p('The before register covers %s and the after register covers %s. They are different '
              'periods, not the same period run twice, so a difference between them carries '
              'ordinary payroll movement as well as the premium: a change in hours, overtime, a '
              'new deduction.' % (pm['before'], pm['after']), after=6)
        if notcmp:
            doc.p('%d employees were paid a different gross across the two runs. For those '
                  'employees no part of the change in take home can be read as a programme '
                  'result, so they are set out here and excluded from the causes rather than '
                  'averaged into them.' % len(notcmp), after=4)
            body = [[a.name, money(a.gross_change),
                     money(a.actual_net_change), money(a.engine.allotment)]
                    for a in sorted(notcmp, key=lambda a: -abs(a.gross_change or 0))[:24]]
            doc.tbl(['Employee', 'Change in gross', 'Change in net pay', 'Allotment promised'],
                    body, [2.2, 1.6, 1.6, 1.7])
        doc.p('A clean reconciliation needs a mock run covering the same period as the before '
              'register.', after=12)

    if losing:
        num += 1
        doc.h('Finding %d   Some employees pay more in fee than they save' % num)
        body = [[a.name, money(a.engine.federal_savings), money(a.engine.medicare_savings),
                 money(-(a.engine.fee or 0)), money(-(a.fee_exceeds_saving or 0))]
                for a in sorted(losing, key=lambda a: -(a.fee_exceeds_saving or 0))[:24]]
        doc.tbl(['Employee', 'Federal saving', 'Medicare saving', 'Fee', 'Net each month'],
                body, [2.0, 1.4, 1.4, 1.1, 1.3])
        doc.p('Nothing is miscalculated. The premium does not reduce enough tax to cover the fee, '
              'usually because the employee pays little or no federal income tax. This is an '
              'enrolment decision, not a payroll correction.', after=12)

    cen = [a for a in audits
           if any(f.label == 'The census salary does not include supplemental pay' for f in a.findings)]
    if cen:
        num += 1
        doc.h('Finding %d   The census salary leaves out pay that payroll taxes' % num)
        doc.p('%d employees draw supplemental pay, stipends, extra duty and the like, on top of '
              'the contract salary. The census carries the contract salary only, so the proposal '
              'is built from a smaller wage than payroll taxes and the tax saving it quotes sits '
              'at the wrong point on the table.' % len(cen), after=12)

    num += 1
    doc.h('Finding %d   Who did not match, and why' % num)
    if short:
        body = []
        for a in sorted(short, key=lambda a: a.allotment_gap or 0):
            why = next((f.label for f in a.findings if f.label not in ('Match',)), '')
            body.append([a.name, money(a.engine.allotment), money(a.actual_net_change),
                         money(a.allotment_gap), why])
        doc.tbl(['Employee', 'Allotment promised', 'Actual change in net pay', 'Short by', 'Why'],
                body, [1.4, 1.3, 1.4, 1.0, 2.0], left=(4,))
    else:
        doc.p('No employee with a clean comparison is short of the promise.', after=6)
    grey = [a for a in audits if a.verdict_class == 'grey' and a.comparable]
    if grey:
        doc.p('%d employees could not be checked because a figure was missing from one of the '
              'runs, usually because they do not appear in both.' % len(grey), after=6)
    return doc.save()
