"""The docx. Front summary once, then one fixed block per employee in the agreed order:
verdict, census input, paycheck comparison, arithmetic, proposal against payroll, cause, allotment, resolution.
Only lines the employee actually has are printed. No sentence states a cause without an amount behind it.
"""
import io
from datetime import date
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

NAVY, GREY, GREEN, RED = RGBColor(0x0F, 0x2A, 0x44), RGBColor(0x6B, 0x7D, 0x8E), RGBColor(0x2E, 0x7D, 0x32), RGBColor(0xB7, 0x1C, 0x1C)
money = lambda v: '' if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")
signed = lambda v: '' if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}" if v > 0 else "$0.00")


def _p(doc, text='', size=9.5, bold=False, color=None, space_after=4, italic=False):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)
    r = p.add_run(text)
    r.font.name, r.font.size, r.bold, r.italic = 'Arial', Pt(size), bold, italic
    if color is not None:
        r.font.color.rgb = color
    return p


def _keep(p):
    pPr = p._element.get_or_add_pPr()
    for tag in ('w:keepNext', 'w:keepLines'):
        el = OxmlElement(tag); pPr.insert(0, el)


def _table(doc, headers, rows, widths=None, right_from=1):
    t = doc.add_table(rows=1, cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.LEFT
    hdr = t.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ''
        para = hdr[i].paragraphs[0]
        r = para.add_run(h)
        r.font.name, r.font.size, r.bold = 'Arial', Pt(8.5), True
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shd = OxmlElement('w:shd'); shd.set(qn('w:fill'), '0F2A44'); hdr[i]._tc.get_or_add_tcPr().append(shd)
        if i >= right_from:
            para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ''
            para = cells[i].paragraphs[0]
            r = para.add_run('' if v is None else str(v))
            r.font.name, r.font.size = 'Arial', Pt(8.5)
            if i >= right_from:
                para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if widths:
        for i, w in enumerate(widths):
            for row in t.rows:
                row.cells[i].width = Inches(w)
    return t


def _census_sentence(a):
    c, b = a.census, a.before
    bits = []
    if c.group_health:
        bits.append(f"{money(c.group_health)} for group health in field O")
    if c.pretax_other:
        bits.append(f"{money(c.pretax_other)} of other pre-tax deductions in field Q")
    if c.retirement_401k:
        bits.append(f"{money(c.retirement_401k)} for retirement in field R")
    s = ('Census pre-tax: none.' if not bits else f"Census pre-tax: {', '.join(bits)}.")
    if a.retirement_not_in_census and abs(a.retirement_not_in_census) > 0.02:
        s += f" Payroll takes another {money(a.retirement_not_in_census)} a month pre-tax that the census does not carry."
    return s


def _paycheck_rows(a):
    b, af, pp = a.before, a.after, a.pay_periods
    per = f" (per pay, {pp} periods)" if pp != 12 else ""
    rows, lines = [], [
        ('Gross', b.gross, af.gross), ('Cafeteria deductions', b.cafeteria, af.cafeteria),
        ('Retirement', b.retirement, af.retirement), ('Premium, pre-tax', b.premium, af.premium),
        ('Reimbursement', b.reimbursement, af.reimbursement), ('Employee fee, after tax', b.fee, af.fee),
        ('Product bought through payroll', b.product, af.product),
        ('Taxable wages', b.taxable_wages, af.taxable_wages), ('Medicare gross', b.medicare_gross, af.medicare_gross),
        ('Federal withholding', b.federal, af.federal),
        (f'State withholding{" " + b.state_code if b.state_code else ""}', b.state, af.state),
        ('Social Security', b.social_security, af.social_security), ('Medicare', b.medicare, af.medicare),
        ('Net pay', b.net_pay, af.net_pay)]
    for label, x, y in lines:
        if x is None and y is None:
            continue
        chg = None if (x is None or y is None) else round(y - x, 2)
        rows.append([label + per if label == 'Gross' else label, money(x), money(y), signed(chg)])
    return rows


def _arithmetic(doc, a):
    b, af = a.before, a.after
    if b.federal is not None and af.federal is not None:
        _p(doc, f"Federal tax saved: {money(b.federal)} before the premium, {money(af.federal)} after, so "
                f"{money(round(b.federal - af.federal, 2))} a pay period and "
                f"{money(a.payroll_federal_savings)} a month.")
    if b.net_pay is not None and af.net_pay is not None:
        _p(doc, f"Take home pay: {money(b.net_pay)} before, {money(af.net_pay)} after, a change of "
                f"{money(round(af.net_pay - b.net_pay, 2))} a pay period and {money(a.actual_net_change)} a month.")
    parts = []
    if a.payroll_federal_savings is not None: parts.append(f"{money(a.payroll_federal_savings)} of federal tax")
    if a.payroll_state_savings: parts.append(f"{money(a.payroll_state_savings)} of state tax")
    if a.payroll_fica_savings: parts.append(f"{money(a.payroll_fica_savings)} of Social Security and Medicare")
    if parts and a.payroll_fee is not None:
        outcome = (f"{money(a.expected_net_change)} a month more in their pay"
                   if (a.expected_net_change or 0) >= 0 else
                   f"them {money(abs(a.expected_net_change))} a month worse off")
        _p(doc, f"Putting that together: the employee saves {' and '.join(parts)} a month and pays the "
                f"{money(a.payroll_fee)} fee, which should leave {outcome}.")


def _recon_rows(a):
    e, b, af, pp = a.engine, a.before, a.after, a.pay_periods
    m = lambda v: None if v is None else round(v * pp / 12.0, 2)
    rows = []
    for label, eng, pay in [
        ('Income taxed before the premium', e.taxable_income_before, m(b.taxable_wages)),
        ('Income taxed after the premium', e.taxable_income_after, m(af.taxable_wages)),
        ('Pay subject to Medicare, before', e.taxable_income_before, m(b.medicare_gross)),
        ('Federal tax before the premium', e.federal_before, m(b.federal)),
        ('Federal tax saved', e.federal_savings, a.payroll_federal_savings),
        ('State tax saved', e.state_savings, a.payroll_state_savings),
        ('Social Security and Medicare saved', (e.ss_savings or 0) + (e.medicare_savings or 0) or None, a.payroll_fica_savings),
    ]:
        if eng is None and pay is None:
            continue
        diff = None if (eng is None or pay is None) else round(pay - eng, 2)
        if diff is not None and abs(diff) <= 0.02 and label in ('Income taxed before the premium',
                                                                'Income taxed after the premium',
                                                                'Pay subject to Medicare, before'):
            rows.append([label, money(eng), money(pay), '$0.00'])
        else:
            rows.append([label, money(eng), money(pay), signed(diff)])
    return rows


def employee_block(doc, a, client=''):
    h = _p(doc, f"{a.name.upper()}{', ' + client.upper() if client else ''}", size=11, bold=True, color=NAVY, space_after=2)
    _keep(h)
    v = _p(doc, f"Verdict: {_verdict_sentence(a)}", size=9.5, bold=True,
           color=GREEN if a.verdict_class == 'green' else (RED if a.verdict_class == 'red' else NAVY), space_after=6)
    _keep(v)
    _keep(_p(doc, 'What the census told the proposal', size=9.5, bold=True, color=NAVY, space_after=2))
    _p(doc, _census_sentence(a), space_after=6)
    rows = _paycheck_rows(a)
    if rows:
        _keep(_p(doc, 'The two payslips side by side', size=9.5, bold=True, color=NAVY, space_after=2))
        _table(doc, ['Line on the payslip', 'Before', 'After', 'Change'], rows, [2.9, 1.2, 1.2, 1.1])
        _p(doc, '', space_after=2)
        _arithmetic(doc, a)
    rr = _recon_rows(a)
    if rr:
        _keep(_p(doc, 'What the proposal expected, against the payslips', size=9.5, bold=True, color=NAVY, space_after=2))
        _table(doc, ['Figure', 'Proposal', 'Payslips', 'Difference'], rr, [2.9, 1.2, 1.2, 1.1])
        _p(doc, '', space_after=2)
        if a.engine.taxable_income_before is not None and a.before.medicare_gross is not None:
            gap = a.ti_before_gap
            if gap is not None and abs(gap) <= 0.02:
                _p(doc, f"The proposal started from {money(a.engine.taxable_income_before)} a month of income, which "
                    f"matches the payslip.")
            else:
                _p(doc, f"The proposal started from {money(a.engine.taxable_income_before)} a month of income where "
                        f"the payslip taxes "
                        f"{money(round((a.before.taxable_wages or 0) * a.pay_periods / 12, 2))}, a difference of "
                        f"{money(gap)}.")
    if getattr(a, 'primary_action', ''):
        _keep(_p(doc, 'What to change', size=9.5, bold=True, color=NAVY, space_after=2))
        _p(doc, a.primary_action, bold=True, space_after=6)
    _keep(_p(doc, 'Why', size=9.5, bold=True, color=NAVY, space_after=2))
    named = [f for f in a.findings if f.label not in ('Match', 'No cause could be established', 'No payslip found for this employee')]
    if a.verdict_class == 'green':
        _p(doc, 'No shortfall: the employee takes home at least what was promised.')
    elif any(f.label == 'No payslip found for this employee' for f in a.findings):
        _p(doc, 'No payroll statement in the packs matched this employee, so the proposal figures stand unreconciled. '
                'This is a files gap, not a finding against the employee.')
    elif not named:
        _p(doc, f"The submitted data establishes a difference of {money(a.allotment_gap)} and does not establish its cause.")
    else:
        if len(named) > 1:
            _table(doc, ['Cause', 'Amount'], [[f.label, money(f.amount)] for f in named], [5.3, 1.6])
            _p(doc, '', space_after=2)
        for f in named:
            _p(doc, f"{f.label}: {money(f.amount)}. {f.detail}")
    if a.engine.allotment is not None and a.actual_net_change is not None:
        _keep(_p(doc, 'What was promised, against what the employee got', size=9.5, bold=True, color=NAVY, space_after=2))
        _table(doc, ['Figure', 'Amount'], [['Promised by the proposal', money(a.engine.allotment)],
                                           ['Actual change in take home pay', money(a.actual_net_change)],
                                           ['Gap', signed(a.allotment_gap)]], [5.3, 1.6])
        _p(doc, '', space_after=2)
        if abs(a.allotment_gap or 0) <= 0.02:
            _p(doc, f"The proposal promised {money(a.engine.allotment)} a month and that is what the payslips show.")
        else:
            # One finding is only presented as the cause of the whole gap when it is the only finding open. Where
            # several are open, the report says what is established and stops short of apportioning the gap.
            open_findings = [f for f in named]
            head = (f"The proposal promised {money(a.engine.allotment)} a month. The payslips show "
                    f"{money(a.actual_net_change)}. The difference is {money(a.allotment_gap)}. ")
            if len(open_findings) == 1:
                f = open_findings[0]
                _p(doc, head + f"The one thing the files show is "
                              f"{f.label.lower()}{', ' + money(f.amount) + ' a month' if f.amount is not None else ''}. "
                              f"How much of the difference that accounts for can only be seen by correcting it and "
                              f"running the proposal again.")
            elif open_findings:
                bits = ', '.join(f"{f.label.lower()}{' of ' + money(f.amount) if f.amount is not None else ''}"
                                 for f in open_findings)
                _p(doc, head + f"More than one thing is going on here: {bits}. This tool will not guess how much "
                              f"of the difference belongs to each, so correct the census, run the proposal again "
                              f"and see what is left.")
            else:
                _p(doc, head + 'The files provided do not show what caused it.')
    elif a.engine.allotment is None or a.actual_net_change is None:
        miss = 'the proposal allotment' if a.engine.allotment is None else 'net pay on both statements'
        _p(doc, f"Allotment against actual net pay cannot be reconciled because {miss} is unavailable.")
    _uncertainty_block(doc, a)
    res = _resolution(a)
    if res:
        _p(doc, res, bold=True, space_after=14)
    else:
        _p(doc, '', space_after=14)


def _uncertainty_block(doc, a):
    """One line per fact about reading reliability, and only when it matters: never on a verified employee,
    never when the modelled interval is immaterial. Modelled figures stay labelled as modelled."""
    if a.verdict_class == 'green':
        return
    u = getattr(a, 'uncertainty', None)
    if not u:
        return
    lines = []
    for st in (u.get('statements') or []):
        ml = st.get('most_likely') or {}
        best = (ml.get('readings') or [{}])[0]
        if best.get('value') is not None and ml.get('observed') is not None \
                and abs(best['value'] - ml['observed']) > 0.02:
            lines.append(f"{st['statement'].capitalize()} payslip: the {ml['field'].replace('_', ' ')} read "
                         f"({money(ml['observed'])}) is the least reliable line; most likely printed value "
                         f"{money(best['value'])}. The read value stands.")
    for d in (u.get('derived') or []):
        flds = ', '.join(f"{k.replace('_', ' ')} {money(v['value'])}" for k, v in (d.get('fields') or {}).items())
        if flds:
            lines.append(f"{d['statement'].capitalize()} payslip: derived from the page's arithmetic, not read: "
                         f"{flds}.")
    iv = u.get('gap_interval')
    if iv and (iv.get('probability_beyond_materiality') or 0) >= 0.05:
        lines.append(f"Reading error carried through: the gap could be {money(iv['low'])} to {money(iv['high'])}; "
                     f"chance it exceeds {money(iv['materiality'])} is "
                     f"{iv['probability_beyond_materiality']:.0%}. Modelled.")
    if not lines:
        return
    _keep(_p(doc, 'Reading reliability', size=9.5, bold=True, color=NAVY, space_after=2))
    for ln in lines:
        _p(doc, ln, space_after=2)


def _verdict_sentence(a):
    """The one line at the top of an employee's block: what happened to this person, in their own terms."""
    if a.verdict_class == 'grey':
        missing = []
        if a.before.federal is None: missing.append('the federal tax on the payslip from before the premium')
        if a.after.federal is None: missing.append('the federal tax on the payslip from after it')
        if a.engine.allotment is None: missing.append('what the proposal promised this employee')
        return (a.verdict + '. ' + (', '.join(missing).capitalize() or 'A figure needed for the comparison') +
                ' could not be found in the files provided.')
    if a.allotment_gap is not None and abs(a.allotment_gap) <= 0.02:
        return (f"{a.verdict.rstrip('.')}. The proposal promised {money(a.engine.allotment)} a month and the "
                f"payslips show {money(a.actual_net_change)}.")
    if a.allotment_gap is not None:
        return (f"{a.verdict.rstrip('.')}. The proposal promised {money(a.engine.allotment)} a month; the payslips "
                f"show {money(a.actual_net_change)}, a difference of {money(a.allotment_gap)}.")
    return a.verdict or 'This employee could not be compared with the files provided.'


def _resolution(a):
    """What to do next, in words the person holding the census can act on."""
    if a.verdict_class == 'green':
        return 'Nothing to do.'
    labels = {f.label for f in a.findings}
    if 'No cause could be established' in labels:
        return 'The difference is recorded; the files do not show its cause.'
    if a.verdict_class == 'red':
        return "Not verified: check this employee's payslips."
    return ''    # each cause above carries its own what-to-do


def build(audits, summary, notes, client='', files=None, ai_paragraph='', period=''):
    doc = Document()
    st = doc.styles['Normal']; st.font.name, st.font.size = 'Arial', Pt(9.5)
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(0.85)
        s.top_margin = s.bottom_margin = Inches(0.8)
    _p(doc, 'PAYROLL RECONCILIATION', size=20, bold=True, color=NAVY, space_after=0)
    _p(doc, f"{client or 'Client'}{'  ·  ' + period if period else ''}", size=10, color=GREY, space_after=16)
    _p(doc, 'Summary', size=12, bold=True, color=NAVY, space_after=4)
    if ai_paragraph:
        _p(doc, ai_paragraph, space_after=8)
    _table(doc, ['Employees', 'How many', 'Share'],
           [['Getting exactly what was promised', summary['matched'], f"{summary['matched']/max(summary['employees'],1):.1%}"],
            ['Different from the promise, with a cause we can show', summary['attributed'], f"{summary['attributed']/max(summary['employees'],1):.1%}"],
            ['Different, and we could not establish why', summary['unexplained'], f"{summary['unexplained']/max(summary['employees'],1):.1%}"],
            ['Could not be checked, something was missing', summary['data_missing'], f"{summary['data_missing']/max(summary['employees'],1):.1%}"],
            ['Total', summary['employees'], '100.0%']], [4.2, 1.4, 1.3])
    _p(doc, '', space_after=8)
    if summary['causes']:
        _p(doc, 'Causes', size=12, bold=True, color=NAVY, space_after=4)
        _p(doc, 'An employee can appear under more than one cause, so these numbers add up to more than the number of '
            'employees. A cause is only listed where a payslip line or a census field shows it; if something is not '
            'listed, it means this tool could not see it, not that it is not there.',
         size=8.5, color=GREY, space_after=4)
        _table(doc, ['Cause', 'Employees', 'Amount, monthly'],
               [[k, v['employees'], money(v['amount'])] for k, v in summary['causes']], [4.2, 1.4, 1.3])
        _p(doc, '', space_after=8)
    if summary.get('total_gap') is not None:
        unver = summary.get('unverified', 0)
        _p(doc, f"Across everyone checked, the difference between what the proposal promised and what the "
                f"payslips show is {money(summary['total_gap'])} a month. That covers the {summary['covered']} "
                f"employees with a usable payslip both before and after the premium, out of {summary['employees']}"
                + (f". {unver} of those {summary['covered']} are marked as not verified because their payslips do "
                   f"not add up; their figures are in this total and should be read with that in mind."
                   if unver else '.')
                + f" {summary['decreases']} employees take home less after the premium than before it, on their "
                  f"own payslips.", space_after=8)
    _p(doc, 'What was checked, and how', size=12, bold=True, color=NAVY, space_after=4)
    for n in (files or []) + notes:
        _p(doc, n, size=9, color=GREY, space_after=2)
    _p(doc, 'Census fields are the proposal inputs. The payroll before the premium is the baseline and the payroll after '
            'it is the comparison. Payroll federal withholding savings are the federal withholding on the before '
            'statement less the federal withholding on the after statement, converted to the report period by the '
            'pay frequency carried on the census. The employee allotment comes from the proposal report. Net pay is '
            'reconciled independently of the tax lines. A cause is reported only where the submitted data '
            'establishes it, and an employee the data does not settle is reported unverified rather than assigned a '
            'reconciliation.', size=9, color=GREY, space_after=8)
    pop0 = summary.get('population') or {}
    _p(doc, f"Of {summary['employees']} employees, {pop0.get('both', 0)} had a usable payslip both before and "
            f"after the premium. {summary.get('unverified', 0)} of those are marked as not verified because the "
            f"figures on their payslips do not add up. No figure was changed to make a comparison work.",
         space_after=10)
    _p(doc, 'Who was checked', size=12, bold=True, color=NAVY, space_after=4)
    pop = summary.get('population') or {}
    _table(doc, ['Control', 'Count'],
           [['Employees on the census and the proposal', summary['employees']],
            ['Had a payslip from before and after the premium', pop.get('both', 0)],
            ['Had only one of the two payslips', pop.get('one', 0)],
            ['Had no payslip at all', pop.get('none', 0)],
            ['Payslips that belong to nobody on the census, set aside', pop.get('unmatched_statements', 0)]],
           [5.2, 1.7])
    _p(doc, '', space_after=4)
    _p(doc, 'Statements match employees by payroll employee number, then by last name with the first three letters '
            'of the first name. A statement matching nobody is set aside, never assigned on a partial match.',
         size=9, color=GREY, space_after=8)
    _p(doc, 'Filing status, multiple jobs, dependents and additional withholding are compared with the census. The '
            'statements do not print an exemption indicator, so that field is not reviewed.',
         size=9, color=GREY, space_after=8)
    _p(doc, 'How the figures were read', size=12, bold=True, color=NAVY, space_after=4)
    _p(doc, 'The statements are scanned images. Each figure is read by its printed label and each page is read once '
            'and kept, so a figure traces back to the words it came from.', size=9, color=GREY, space_after=8)
    _p(doc, 'Net pay is printed four times on a statement: the net pay line, the deposit total, the sum of the '
            'deposit rows, and gross less total deductions. Where two agree, that figure is used. Where none agree, '
            'no net pay is taken and the employee is reported unverified.', size=9, color=GREY, space_after=14)
    for a in audits:
        employee_block(doc, a, client=client)
    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    return buf.read()
