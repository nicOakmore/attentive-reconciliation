"""The docx. Front summary once, then one fixed block per employee in the agreed order:
verdict, census input, paycheck comparison, arithmetic, engine reconciliation, cause, allotment, resolution.
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
        bits.append(f"census field O contains {money(c.group_health)} for group health")
    if c.pretax_other:
        bits.append(f"census field Q contains {money(c.pretax_other)}")
    if c.retirement_401k:
        bits.append(f"census field R contains {money(c.retirement_401k)} for retirement")
    s = ('The engine received no pre-tax deduction from the census.' if not bits else
         f"On the census, {', '.join(bits)}. The engine receives {money(a.census_pretax)} as the total pre-tax "
         f"deduction.")
    if a.retirement_not_in_census and abs(a.retirement_not_in_census) > 0.02:
        s += (f" The payroll reduces federal taxable wages by a further {money(a.retirement_not_in_census)} each month "
              f"for retirement, and no census field carries it.")
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
        _p(doc, f"Federal withholding savings are {money(b.federal)} minus {money(af.federal)} = "
                f"{money(round(b.federal - af.federal, 2))} per pay, {money(a.payroll_federal_savings)} a month.")
    if b.net_pay is not None and af.net_pay is not None:
        _p(doc, f"Net pay change is {money(af.net_pay)} minus {money(b.net_pay)} = "
                f"{money(round(af.net_pay - b.net_pay, 2))} per pay, {money(a.actual_net_change)} a month.")
    parts = []
    if a.payroll_federal_savings is not None: parts.append(f"federal savings of {money(a.payroll_federal_savings)}")
    if a.payroll_state_savings: parts.append(f"state savings of {money(a.payroll_state_savings)}")
    if a.payroll_fica_savings: parts.append(f"Social Security and Medicare savings of {money(a.payroll_fica_savings)}")
    if parts and a.payroll_fee is not None:
        _p(doc, f"Monthly net pay change is {' plus '.join(parts)} minus the {money(a.payroll_fee)} employee fee = "
                f"{money(a.expected_net_change)}.")


def _recon_rows(a):
    e, b, af, pp = a.engine, a.before, a.after, a.pay_periods
    m = lambda v: None if v is None else round(v * pp / 12.0, 2)
    rows = []
    for label, eng, pay in [
        ('Taxable income before', e.taxable_income_before, m(b.taxable_wages)),
        ('Taxable income after', e.taxable_income_after, m(af.taxable_wages)),
        ('Medicare gross before', e.taxable_income_before, m(b.medicare_gross)),
        ('Federal withholding before', e.federal_before, m(b.federal)),
        ('Federal savings', e.federal_savings, a.payroll_federal_savings),
        ('State savings', e.state_savings, a.payroll_state_savings),
        ('Social Security and Medicare savings', (e.ss_savings or 0) + (e.medicare_savings or 0) or None, a.payroll_fica_savings),
    ]:
        if eng is None and pay is None:
            continue
        diff = None if (eng is None or pay is None) else round(pay - eng, 2)
        if diff is not None and abs(diff) <= 0.02 and label in ('Taxable income before', 'Taxable income after', 'Medicare gross before'):
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
    _keep(_p(doc, 'Census input', size=9.5, bold=True, color=NAVY, space_after=2))
    _p(doc, _census_sentence(a), space_after=6)
    rows = _paycheck_rows(a)
    if rows:
        _keep(_p(doc, 'Paycheck comparison', size=9.5, bold=True, color=NAVY, space_after=2))
        _table(doc, ['Payroll line', 'Before', 'After', 'Change'], rows, [2.9, 1.2, 1.2, 1.1])
        _p(doc, '', space_after=2)
        _arithmetic(doc, a)
    rr = _recon_rows(a)
    if rr:
        _keep(_p(doc, 'Engine reconciliation', size=9.5, bold=True, color=NAVY, space_after=2))
        _table(doc, ['Metric', 'Engine', 'Payroll', 'Difference'], rr, [2.9, 1.2, 1.2, 1.1])
        _p(doc, '', space_after=2)
        if a.engine.taxable_income_before is not None and a.before.medicare_gross is not None:
            gap = a.ti_before_gap
            if gap is not None and abs(gap) <= 0.02:
                _p(doc, f"Engine taxable income before of {money(a.engine.taxable_income_before)} matches the payroll "
                        f"medicare gross of {money(round(a.before.medicare_gross * a.pay_periods / 12, 2))}.")
            else:
                _p(doc, f"Engine taxable income before of {money(a.engine.taxable_income_before)} differs from payroll "
                        f"taxable wages of {money(round((a.before.taxable_wages or 0) * a.pay_periods / 12, 2))} by {money(gap)}.")
    _keep(_p(doc, 'Cause', size=9.5, bold=True, color=NAVY, space_after=2))
    named = [f for f in a.findings if f.label not in ('Match', 'Unattributed', 'Statement not provided')]
    if a.verdict_class == 'green':
        _p(doc, 'No discrepancy identified. The engine allotment equals the actual net pay change.')
    elif any(f.label == 'Statement not provided' for f in a.findings):
        _p(doc, 'No payroll statement in the packs supplied matched this employee, so the engine figures stand '
                'unreconciled. This is a coverage limit of the files, not a discrepancy.')
    elif not named:
        _p(doc, f"The submitted data establishes a difference of {money(a.allotment_gap)} and does not establish its cause.")
    else:
        if len(named) > 1:
            _table(doc, ['Cause', 'Amount'], [[f.label, money(f.amount)] for f in named], [5.3, 1.6])
            _p(doc, '', space_after=2)
        for f in named:
            _p(doc, f"{f.label}: {money(f.amount)}. {f.detail}")
    if a.engine.allotment is not None and a.actual_net_change is not None:
        _keep(_p(doc, 'Allotment against actual net pay', size=9.5, bold=True, color=NAVY, space_after=2))
        _table(doc, ['Metric', 'Amount'], [['Engine allotment', money(a.engine.allotment)],
                                           ['Actual net pay change', money(a.actual_net_change)],
                                           ['Gap', signed(a.allotment_gap)]], [5.3, 1.6])
        _p(doc, '', space_after=2)
        if abs(a.allotment_gap or 0) <= 0.02:
            _p(doc, f"Engine allotment is {money(a.engine.allotment)} and the actual net pay change is "
                    f"{money(a.actual_net_change)}. They agree.")
        else:
            # One finding is only presented as the cause of the whole gap when it is the only finding open. Where
            # several are open, the report says what is established and stops short of apportioning the gap.
            open_findings = [f for f in named]
            head = (f"Engine allotment is {money(a.engine.allotment)}. Actual net pay change is "
                    f"{money(a.actual_net_change)}. The difference is {money(a.allotment_gap)}. ")
            if len(open_findings) == 1:
                f = open_findings[0]
                _p(doc, head + f"The only finding established on the submitted data is "
                              f"{f.label.lower()}{' of ' + money(f.amount) if f.amount is not None else ''}. "
                              f"Quantifying its effect on the difference requires a rerun with that input corrected.")
            elif open_findings:
                bits = ', '.join(f"{f.label.lower()}{' of ' + money(f.amount) if f.amount is not None else ''}"
                                 for f in open_findings)
                _p(doc, head + f"The submitted data establishes more than one finding on this employee: {bits}. "
                              f"The tool does not apportion the difference between them; correct the census input, "
                              f"rerun the calculation and reassess what remains.")
            else:
                _p(doc, head + 'The submitted data does not establish a cause.')
    elif a.engine.allotment is None or a.actual_net_change is None:
        miss = 'the proposal allotment' if a.engine.allotment is None else 'net pay on both statements'
        _p(doc, f"Allotment against actual net pay cannot be reconciled because {miss} is unavailable.")
    _uncertainty_block(doc, a)
    _p(doc, f"Resolution: {_resolution(a)}", bold=True, space_after=14)


def _uncertainty_block(doc, a):
    """Where the statements do not tie, say what the reading uncertainty implies. Every figure here is modelled and
    labelled as such: it does not replace anything printed on the payroll, and it does not close the exception."""
    u = getattr(a, 'uncertainty', None)
    if not u or (not u.get('statements') and not u.get('gap_interval')):
        return
    _keep(_p(doc, 'Uncertainty analysis, modelled', size=9.5, bold=True, color=NAVY, space_after=2))
    for st in (u.get('statements') or []):
        names = ', '.join(f"{s['field'].replace('_', ' ')} at {s['probability']:.0%}" for s in st['suspects'])
        _p(doc, f"On the {st['statement']} statement the line least consistent with the others is {names}. "
                f"This is a probability under the reading error model measured on statements whose figures are "
                f"known, not a statement about the payroll.")
        ml = st.get('most_likely')
        if ml and ml.get('readings'):
            best = ml['readings'][0]
            _p(doc, f"Read as {money(ml.get('observed'))}. Under the same model the most likely printed value is "
                    f"{money(best['value'])} with probability {best['probability']:.0%}. The figure used in this "
                    f"report remains the one read from the page.")
    iv = u.get('gap_interval')
    if iv:
        _p(doc, f"Carrying the reading error through to the conclusion, the gap between allotment and net pay "
                f"change has a modelled 95 per cent interval of {money(iv['low'])} to {money(iv['high'])}, median "
                f"{money(iv['median'])}, from {iv['draws']:,} draws. The chance the gap exceeds "
                f"{money(iv['materiality'])} either way is {iv['probability_beyond_materiality']:.0%}. This "
                f"interval is modelled from the reading uncertainty and is not a payroll figure.", space_after=6)


def _verdict_sentence(a):
    if a.verdict_class == 'grey':
        missing = []
        if a.before.federal is None: missing.append('federal withholding on the before statement')
        if a.after.federal is None: missing.append('federal withholding on the after statement')
        if a.engine.allotment is None: missing.append('the proposal allotment')
        return ('Payroll comparison is unavailable because ' + (', '.join(missing) or 'the required payroll lines') +
                ' is missing from the submitted data.')
    if a.engine.federal_savings is not None and a.payroll_federal_savings is not None:
        if abs((a.federal_gap or 0)) <= 0.02:
            return (f"Engine federal savings of {money(a.engine.federal_savings)} reconciles to the payroll withholding "
                    f"change of {money(a.payroll_federal_savings)}.")
        return (f"Engine federal savings of {money(a.engine.federal_savings)} differs from the payroll withholding change "
                f"of {money(a.payroll_federal_savings)} by {money(a.federal_gap)}.")
    if a.allotment_gap is not None:
        return (f"Engine allotment of {money(a.engine.allotment)} differs from the actual net pay change of "
                f"{money(a.actual_net_change)} by {money(a.allotment_gap)}.")
    return 'Payroll comparison is unavailable from the submitted data.'


def _resolution(a):
    if a.verdict_class == 'green':
        return 'No data correction required. The engine and the payroll reconcile to the cent.'
    for f in a.findings:
        if f.label in ('Retirement deduction not in the census', 'Federal taxable wage reduction not in the census'):
            base = (a.census.pretax_other or 0)
            return (f"Resolution candidate: verify {money(f.amount)} a month against the census input specification. "
                    f"If field Q is the designated input for it, the field becomes "
                    f"{money(round(base + (f.amount or 0), 2))}; rerun the calculation and compare again.")
        if f.label == 'Cafeteria deduction not in the census':
            return (f"Resolution candidate: verify the pre-tax deduction of {money(f.amount)} against the census "
                    f"input specification, add it to the designated field and rerun the calculation.")
        if f.label == 'W-4 withholding instruction':
            return ('Resolution candidate: reconcile the W-4 held by payroll with the W-4 data on the census, then '
                    'rerun the calculation on the agreed instruction.')
    labels = {f.label for f in a.findings}
    if 'Statement identity does not tie' in labels:
        return ('No correction is supported. The statement lines do not reconcile to the net pay change, so this '
                'employee is reported unverified pending a manual check of the statement.')
    if 'Other changed earning or deduction' in labels:
        return ('No census correction applies. Something other than the premium changed between the two statements, '
                'so the pair is not a like for like comparison. Obtain a mock statement that changes only the premium.')
    if 'Fixed federal withholding' in labels:
        return 'No data correction available. Payroll withholds a fixed federal amount for this employee.'
    if labels & {'Withholding method or configuration difference', 'State withholding'}:
        return ('No census correction applies. The difference arises from the withholding parameters each system '
                'holds, not from the census input.')
    if 'Unattributed' in labels:
        return 'The difference is documented. No corrective action is supported by the submitted data.'
    return 'The difference is documented.'


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
    _table(doc, ['Population', 'Employees', 'Share'],
           [['Fully reconciled', summary['matched'], f"{summary['matched']/max(summary['employees'],1):.1%}"],
            ['Difference with an established cause', summary['attributed'], f"{summary['attributed']/max(summary['employees'],1):.1%}"],
            ['Difference with no established cause', summary['unexplained'], f"{summary['unexplained']/max(summary['employees'],1):.1%}"],
            ['Source data missing', summary['data_missing'], f"{summary['data_missing']/max(summary['employees'],1):.1%}"],
            ['Total', summary['employees'], '100.0%']], [4.2, 1.4, 1.3])
    _p(doc, '', space_after=8)
    if summary['causes']:
        _p(doc, 'Causes', size=12, bold=True, color=NAVY, space_after=4)
        _p(doc, 'Cause counts are not mutually exclusive. An employee may carry more than one cause, so these counts '
                'do not sum to the population. A cause is reported only where a reliably extracted payroll line or '
                'census field establishes it; the absence of a cause is not evidence that the underlying condition '
                'is absent.', size=8.5, color=GREY, space_after=4)
        _table(doc, ['Cause', 'Employees', 'Amount, monthly'],
               [[k, v['employees'], money(v['amount'])] for k, v in summary['causes']], [4.2, 1.4, 1.3])
        _p(doc, '', space_after=8)
    if summary.get('total_gap') is not None:
        unver = summary.get('unverified', 0)
        _p(doc, f"The aggregate difference between engine allotment and reported net pay change is "
                f"{money(summary['total_gap'])} a month. It is calculated over the {summary['covered']} employees "
                f"who carry an accepted net pay on both statements and an allotment in the proposal report, in a "
                f"population of {summary['employees']}"
                + (f", and {unver} of those {summary['covered']} carry an untied statement identity and are "
                   f"reported unverified; their figures are included in this total and should be read with that "
                   f"qualification." if unver else '.')
                + f" {summary['decreases']} employees show lower reported net pay on the statement after the "
                  f"premium than on the statement before it.", space_after=8)
    _p(doc, 'Scope and method', size=12, bold=True, color=NAVY, space_after=4)
    for n in (files or []) + notes:
        _p(doc, n, size=9, color=GREY, space_after=2)
    _p(doc, 'Census fields are the engine inputs. The payroll before the premium is the baseline and the payroll after '
            'it is the comparison. Payroll federal withholding savings are the federal withholding on the before '
            'statement less the federal withholding on the after statement, converted to the report period by the '
            'pay frequency carried on the census. The employee allotment comes from the proposal report. Net pay is '
            'reconciled independently of the tax lines. A cause is reported only where the submitted data '
            'establishes it, and an employee the data does not settle is reported unverified rather than assigned a '
            'reconciliation.', size=9, color=GREY, space_after=8)
    pop0 = summary.get('population') or {}
    _p(doc, f"On this payroll pack, {pop0.get('both', 0)} of {summary['employees']} employees have an accepted net "
            f"pay value on both statements; {summary.get('unverified', 0)} of those {pop0.get('both', 0)} are "
            f"reported unverified because the independent payroll identity does not tie, and no payroll value is "
            f"overridden solely to force a reconciliation.", space_after=10)
    _p(doc, 'Population and matching', size=12, bold=True, color=NAVY, space_after=4)
    pop = summary.get('population') or {}
    _table(doc, ['Control', 'Count'],
           [['Employees in the proposal report and census, the defined population', summary['employees']],
            ['Matched to both a before and an after statement', pop.get('both', 0)],
            ['Matched to one statement only', pop.get('one', 0)],
            ['Matched to no statement', pop.get('none', 0)],
            ['Statements in the packs not matched to any employee, excluded', pop.get('unmatched_statements', 0)]],
           [5.2, 1.7])
    _p(doc, '', space_after=4)
    _p(doc, 'Statements are matched to employees by payroll employee number first, then by last name with the first '
            'three letters of the first name. A statement that matches no employee in the population is excluded and '
            'counted above; it is never assigned to an employee on a partial match.', size=9, color=GREY, space_after=8)
    _p(doc, 'The statements print the employee\'s filing status, multiple jobs indicator, children under 17, other '
            'dependents, other income, other deductions and additional withholding, and these are compared with the '
            'census. The statements reviewed do not print an exemption from withholding indicator. Accordingly this '
            'review does not compare or conclude on that W-4 field; its presence or value is outside the available '
            'statement evidence.', size=9, color=GREY, space_after=8)
    _p(doc, 'Reading the statements', size=12, bold=True, color=NAVY, space_after=4)
    _p(doc, 'Each page is read once and the reading is kept, with the words and their positions behind every '
            'figure, so a page that appears in a later pack is not read again and a figure can be traced to the '
            'words it came from. A figure corrected by a reviewer is recorded against that page as a correction: '
            'the original machine reading is kept, the correction is applied on top of it, and both are reported.',
         size=9, color=GREY, space_after=8)
    _p(doc, 'The statements in these packs are scanned images, so every figure is read by optical character '
            'recognition and then located by its printed label. Net pay appears four times on the same statement: '
            'the net pay line, the direct deposit total, the sum of the individual deposit rows, and gross pay less '
            'total deductions. These are four representations of one document, not four independent sources. Where '
            'at least two of them agree, the agreed figure is accepted as the extracted net pay for the '
            'reconciliation. That is an extraction control only: it establishes that the readings are consistent '
            'across repeated printings of the same figure, not that the payroll statement itself is correct. Where '
            'no two agree, no net pay is accepted and the employee is reported unverified. Every figure the tool takes from '
            'anywhere other than its own printed line is recorded against that employee in the run notes.',
         size=9, color=GREY, space_after=14)
    for a in audits:
        employee_block(doc, a, client=client)
    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    return buf.read()
