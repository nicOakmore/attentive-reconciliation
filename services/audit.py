"""Deterministic reconciliation. Every number in the report comes from here, never from the language model.

Per employee the audit answers four questions, in the order the Garra Ballinger case study answers them:
  1. what the census gave the engine, against what the paycheck shows
  2. what the payroll itself did, before and after the premium
  3. what the engine predicted, against what the employee actually received
  4. where any difference comes from, attributed to a named cause
"""
from dataclasses import dataclass, field, asdict
from typing import Optional

CENT = 0.02          # a match, in dollars
FICA_SS, FICA_MED = 0.062, 0.0145


def r2(x):
    return None if x is None else round(float(x) + 0.0, 2)


def per_month(amount, pay_periods):
    """Put a per-pay amount on the engine's monthly basis. 12 for monthly, 24, 26 or 52 otherwise."""
    if amount is None or not pay_periods:
        return None
    return r2(float(amount) * float(pay_periods) / 12.0)


@dataclass
class Paycheck:
    gross: Optional[float] = None
    federal: Optional[float] = None
    state: Optional[float] = None
    state_code: str = ''
    social_security: Optional[float] = None
    medicare: Optional[float] = None
    taxable_wages: Optional[float] = None
    medicare_gross: Optional[float] = None
    net_pay: Optional[float] = None
    premium: Optional[float] = None          # the pre-tax wellness deduction on the after paycheck
    reimbursement: Optional[float] = None    # the SIMRP line that returns the premium
    fee: Optional[float] = None              # the after-tax employee fee
    product: Optional[float] = None          # a product bought through payroll, if any
    retirement: Optional[float] = None       # TRS, 403(b), 457: reduces federal taxable wages
    cafeteria: Optional[float] = None        # health, dental, vision: reduces federal and FICA wages
    other_deductions: Optional[float] = None
    federal_unreliable: Optional[float] = None
    other_total: Optional[float] = None       # the statement's own total of other deductions
    total_deductions: Optional[float] = None
    w4_status: Optional[str] = None           # the filing status printed on the statement
    w4_multijob: Optional[str] = None
    w4_children: Optional[int] = None
    w4_extra: Optional[float] = None          # additional withholding per pay, W-4 Step 4(c)
    retirement_line: Optional[bool] = None    # the statement prints a retirement reduction line
    net_pay_corrected: Optional[str] = None
    net_pay_unreliable: Optional[list] = None
    net_pay_disputed: Optional[dict] = None
    source: str = ''


@dataclass
class Engine:
    """What the proposal report says for this employee."""
    federal_before: Optional[float] = None
    federal_savings: Optional[float] = None
    state_savings: Optional[float] = None
    ss_savings: Optional[float] = None
    medicare_savings: Optional[float] = None
    gross_savings: Optional[float] = None
    fee: Optional[float] = None
    allotment: Optional[float] = None
    taxable_income_before: Optional[float] = None
    taxable_income_after: Optional[float] = None
    premium: Optional[float] = None


@dataclass
class Census:
    gross_annual: Optional[float] = None
    pay_periods: Optional[int] = None
    pretax_other: Optional[float] = None     # census field Q
    group_health: Optional[float] = None     # census field O
    retirement_401k: Optional[float] = None  # census field R
    filing_status: str = ''
    w4_year: Optional[int] = None
    dependents: Optional[float] = None
    step2c: str = ''
    step3: Optional[float] = None
    additional_federal: Optional[float] = None
    additional_state: Optional[float] = None
    state: str = ''


@dataclass
class Finding:
    label: str
    amount: Optional[float]
    detail: str


@dataclass
class EmployeeAudit:
    name: str
    employee_id: str = ''
    census: Census = field(default_factory=Census)
    engine: Engine = field(default_factory=Engine)
    before: Paycheck = field(default_factory=Paycheck)
    after: Paycheck = field(default_factory=Paycheck)
    pay_periods: int = 12
    # payroll result
    payroll_federal_savings: Optional[float] = None
    payroll_state_savings: Optional[float] = None
    payroll_fica_savings: Optional[float] = None
    payroll_fee: Optional[float] = None
    expected_net_change: Optional[float] = None
    actual_net_change: Optional[float] = None
    identity_gap: Optional[float] = None
    # engine against payroll, monthly
    allotment_gap: Optional[float] = None
    federal_gap: Optional[float] = None
    state_gap: Optional[float] = None
    fica_gap: Optional[float] = None
    # census against paycheck, monthly
    census_pretax: Optional[float] = None
    paycheck_cafeteria: Optional[float] = None
    retirement_not_in_census: Optional[float] = None
    ti_before_gap: Optional[float] = None
    findings: list = field(default_factory=list)
    verdict: str = ''
    verdict_class: str = ''
    uncertainty: Optional[dict] = None
    narrative: str = ''
    fee_from_statement: bool = False

    def as_dict(self):
        d = asdict(self)
        d['findings'] = [asdict(f) if not isinstance(f, dict) else f for f in self.findings]
        return d


def audit_employee(emp: EmployeeAudit) -> EmployeeAudit:
    b, a, e, c = emp.before, emp.after, emp.engine, emp.census
    pp = emp.pay_periods or c.pay_periods or 12
    emp.pay_periods = pp

    # 2. what the payroll did, converted to the engine's monthly basis
    if b.federal is not None and a.federal is not None:
        emp.payroll_federal_savings = per_month(b.federal - a.federal, pp)
    if b.state is not None and a.state is not None:
        emp.payroll_state_savings = per_month(b.state - a.state, pp)
    sf = lambda p: (p.social_security or 0) + (p.medicare or 0)
    if b.social_security is not None or b.medicare is not None:
        emp.payroll_fica_savings = per_month(sf(b) - sf(a), pp)
    emp.payroll_fee = per_month(a.fee, pp) if a.fee is not None else e.fee
    emp.fee_from_statement = a.fee is not None

    if b.net_pay is not None and a.net_pay is not None:
        actual = (a.net_pay - b.net_pay) + (a.product or 0)
        emp.actual_net_change = per_month(actual, pp)
    parts = [emp.payroll_federal_savings, emp.payroll_state_savings, emp.payroll_fica_savings]
    if any(p is not None for p in parts) and emp.payroll_fee is not None:
        emp.expected_net_change = r2(sum(p or 0 for p in parts) - emp.payroll_fee)
    if emp.expected_net_change is not None and emp.actual_net_change is not None:
        emp.identity_gap = r2(emp.actual_net_change - emp.expected_net_change)

    # 3. engine against payroll
    if e.allotment is not None and emp.actual_net_change is not None:
        emp.allotment_gap = r2(emp.actual_net_change - e.allotment)
    if e.federal_savings is not None and emp.payroll_federal_savings is not None:
        emp.federal_gap = r2(emp.payroll_federal_savings - e.federal_savings)
    if e.state_savings is not None and emp.payroll_state_savings is not None:
        emp.state_gap = r2(emp.payroll_state_savings - e.state_savings)
    eng_fica = (e.ss_savings or 0) + (e.medicare_savings or 0)
    if emp.payroll_fica_savings is not None and (e.ss_savings is not None or e.medicare_savings is not None):
        emp.fica_gap = r2(emp.payroll_fica_savings - eng_fica)

    # 1. census against paycheck
    emp.census_pretax = r2((c.pretax_other or 0) + (c.group_health or 0) + (c.retirement_401k or 0))
    emp.paycheck_cafeteria = per_month(b.cafeteria, pp) if b.cafeteria is not None else None
    if b.medicare_gross is not None and b.taxable_wages is not None:
        emp.retirement_not_in_census = r2(per_month(b.medicare_gross - b.taxable_wages, pp) - (c.retirement_401k or 0))
    if e.taxable_income_before is not None and b.medicare_gross is not None:
        emp.ti_before_gap = r2(e.taxable_income_before - per_month(b.medicare_gross, pp))

    emp.findings = _attribute(emp)
    emp.uncertainty = _uncertainty(emp)
    if emp.uncertainty:
        for st in (emp.uncertainty.get('statements') or []):
            for nm, r in (st.get('residuals') or {}).items():
                if 'exceed gross less net pay' in nm:
                    emp.findings.append(Finding(
                        'The deductions add up to more than the pay', r2(r),
                        f"On the {st['statement']} payslip the deductions add up to {_m(r)} more than the gap "
                        f"between gross pay and take home pay, which cannot be right. One of the figures was "
                        f"probably read wrongly from the scan. This employee is marked as not verified: please "
                        f"check that payslip."))
    if emp.uncertainty and emp.uncertainty.get('cross_document'):
        for d in emp.uncertainty['cross_document'].get('disagreements', []):
            for item in d['items']:
                emp.findings.append(Finding(
                    'The statement and the proposal disagree',
                    r2(item['read'] - item['expected']),
                    f"On the {d['statement']} payslip the {item['field'].replace('_', ' ')} line shows "
                    f"{_m(item['read'])}, but {item['source']} says it should be {_m(item['expected'])}. One of the "
                    f"two is wrong and this tool does not assume which, so both figures are shown."))
    # Decided last, once every finding is in: deciding it earlier let an employee be called correct while carrying
    # a finding that says the payslip cannot be trusted.
    emp.verdict, emp.verdict_class = _verdict(emp)
    if len(emp.findings) > 1:
        emp.findings = [f for f in emp.findings if f.label != 'Match']
    return emp


def _cross_document(emp):
    """What the census and the proposal say about this employee's statements, and where a reading disagrees.

    Only the figures those documents supply as inputs are used. The proposal's own withholding, taxable income,
    savings and allotment are the subject of the audit and cannot test themselves.
    """
    try:
        from . import crossdoc as X
    except Exception:
        return None, {}
    out, exps = {}, {}
    for tag, pc, which in (('before', emp.before, 'before'), ('after', emp.after, 'after')):
        exp = X.expectations(emp.census, emp.engine, emp.pay_periods, which=which)
        exps[which] = exp
        rec = {f: getattr(pc, f, None) for f in
               ('gross', 'federal', 'state', 'social_security', 'medicare', 'retirement', 'net_pay',
                'taxable_wages', 'medicare_gross', 'premium', 'fee', 'reimbursement')}
        dis = X.disagreements(rec, exp)
        if dis:
            out.setdefault('disagreements', []).append(dict(statement=tag, items=dis))
    return (out or None), exps


def _uncertainty(emp):
    """What the reading uncertainty implies, where the statements do not tie. Nothing here changes a figure: it
    says which printed line is most likely the one at fault, what it was most likely printed as, and how wide the
    conclusion is once the reading error is carried through."""
    if emp.identity_gap is None or abs(emp.identity_gap) <= 2.0:
        return None
    try:
        from . import estimate as E
    except Exception:
        return None
    out = {}
    cross, exps = _cross_document(emp)
    if cross:
        out['cross_document'] = cross
    try:
        for tag, pc in (('before', emp.before), ('after', emp.after)):
            rec = {f: getattr(pc, f, None) for f in E.FIELDS}
            fit = E.solve(rec)
            derived = E.fill_derived(rec, expectations=exps.get(tag))
            if derived:
                out.setdefault('derived', []).append(dict(statement=tag, fields={
                    k: dict(value=v['value'], identity=v['identity']) for k, v in derived.items()}))
            suspects = sorted(((f, p) for f, p in fit.outlier.items() if p >= 0.5), key=lambda t: -t[1])[:2]
            if not suspects:
                continue
            entry = dict(statement=tag, residuals={k: v for k, v in fit.residuals.items() if abs(v) > 0.05},
                         suspects=[dict(field=f, probability=p) for f, p in suspects])
            top = suspects[0][0]
            post = E.most_likely_reading(rec, top, expectations=exps.get(tag))
            if post.get('posterior'):
                entry['most_likely'] = dict(field=top, observed=post.get('observed'),
                                            readings=post['posterior'][:3])
            out.setdefault('statements', []).append(entry)
        # The conclusion as a fuzzy number: where a figure could not be pinned, the gap is a range with a degree
        # of support rather than a single number nobody can defend. Crisp readings give a crisp gap.
        try:
            from . import fuzzy as FZ
            fuzzy_fields = {}
            for tag, pc in (('before', emp.before), ('after', emp.after)):
                for st in (out.get('statements') or []):
                    if st['statement'] != tag:
                        continue
                    ml = st.get('most_likely') or {}
                    if ml.get('field') and ml.get('readings'):
                        fuzzy_fields[(tag, ml['field'])] = FZ.from_posterior(ml.get('observed'), ml['readings'])
            nb = fuzzy_fields.get(('before', 'net_pay')) or FZ.Fuzzy.crisp(emp.before.net_pay or 0.0)
            na = fuzzy_fields.get(('after', 'net_pay')) or FZ.Fuzzy.crisp(emp.after.net_pay or 0.0)
            if emp.before.net_pay is not None and emp.after.net_pay is not None and emp.engine.allotment is not None:
                change = (na - nb) * (emp.pay_periods / 12.0)
                gap = change - emp.engine.allotment
                out['fuzzy_gap'] = dict(gap.as_dict(),
                                        confidence=FZ.confidence(gap),
                                        support_that_the_gap_is_negative=FZ.support_for(gap, 0.0, 'below'),
                                        note='a range where a figure could not be pinned to one reading, with the '
                                             'degree to which the reading supports it')
        except Exception:
            pass
        if emp.engine.allotment is not None:
            b = {f: getattr(emp.before, f, None) for f in E.FIELDS}
            a = {f: getattr(emp.after, f, None) for f in E.FIELDS}
            a['product'] = emp.after.product
            # the chance each reading is the wrong one, taken from the fit, so the interval widens exactly where
            # the statement is in doubt and stays tight where it is not
            outl = {}
            for tag, pc in (('b', emp.before), ('a', emp.after)):
                f = E.solve({k: getattr(pc, k, None) for k in E.FIELDS})
                for name, p in f.outlier.items():
                    outl[(tag, name)] = p
            iv = E.gap_interval(b, a, emp.engine.allotment, pay_periods=emp.pay_periods, draws=4000,
                                outliers=outl)
            if iv.get('median') is not None:
                out['gap_interval'] = iv
    except Exception as e:
        return dict(note=f'uncertainty analysis unavailable: {type(e).__name__}')
    return out or None


def _expected_other_change(b, a):
    """How much the statement's other deductions total should move when only the premium arrangement is added: the
    pre-tax premium, the reimbursement that returns it, the employee fee and any product line."""
    def d(attr):
        return (getattr(a, attr) or 0) - (getattr(b, attr) or 0)
    return r2(d('premium') + d('reimbursement') + d('fee') + d('product'))


def _m(v):
    return '' if v is None else (('-$%0.2f' % abs(v)) if v < 0 else ('$%0.2f' % v))


def _attribute(emp: EmployeeAudit) -> list:
    """Name every cause the data supports, with its amount. Nothing is asserted without a number behind it."""
    out, b, a, e = [], emp.before, emp.after, emp.engine
    if emp.allotment_gap is not None and abs(emp.allotment_gap) <= CENT:
        out.append(Finding('Match', emp.allotment_gap,
                           'The employee takes home exactly what the proposal promised.'))
        return out
    if emp.retirement_not_in_census and abs(emp.retirement_not_in_census) > CENT:
        # The arithmetic establishes a reduction of federal taxable wages that stays in Medicare wages. Only the
        # printed line establishes that the reduction is retirement, so the label follows the evidence.
        if b.retirement_line and b.retirement:
            named = per_month(b.retirement, emp.pay_periods)
            unnamed = r2(emp.retirement_not_in_census - named)
            # The amount is the whole reduction of federal taxable wages that the census does not carry. The
            # statement names part of it on a retirement line; any remainder is a further pre-tax deduction the
            # statement does not name, and saying so is the difference between a figure a reader can check and a
            # sentence that quotes one number while claiming another.
            detail = (f'The payslip takes {_m(emp.retirement_not_in_census)} a month off this employee\'s pay '
                       f'before federal tax is worked out, and the census does not show it. The proposal therefore '
                       f'calculated tax on more income than payroll actually taxes. ')
            if abs(unnamed) <= 0.02:
                detail += (f'The payslip shows it as a retirement deduction of {_m(named)} a month. ')
            else:
                detail += (f'The payslip shows {_m(named)} of it as a retirement deduction. The other '
                            f'{_m(unnamed)} a month is another deduction taken before federal tax, which the payslip '
                            f'does not name. Both are missing from the census. ')
            detail += ('This is about federal tax only: Social Security and Medicare are unaffected, as the '
                        'payslip itself shows.')
            out.append(Finding('Retirement deduction missing from the census', emp.retirement_not_in_census, detail))
        else:
            out.append(Finding('A pre-tax deduction is missing from the census', emp.retirement_not_in_census,
                               'The payslip takes this amount off the pay each month before federal tax is worked '
                               'out, and the census does not show it, so the proposal calculated tax on more income '
                               'than payroll taxes. The payslip does not say what the deduction is for, so it is '
                               'reported as it stands rather than guessed at.'))
    if emp.ti_before_gap is not None and abs(emp.ti_before_gap) > CENT and (emp.retirement_not_in_census or 0) == 0:
        out.append(Finding('A pre-tax deduction is missing from the census', emp.ti_before_gap,
                           'The income the proposal started from is higher than the income on the payslip by this '
                           'amount, which means a deduction taken before tax on the payslip is not in the census.'))
    # extra withholding shows as the same difference before and after the premium
    if b.federal is not None and a.federal is not None and abs(b.federal - a.federal) > CENT:
        if b.federal == a.federal:
            pass
    if b.federal is not None and a.federal is not None and b.federal == a.federal and b.federal > 0:
        out.append(Finding('Payroll withholds a fixed federal amount', per_month(b.federal, emp.pay_periods),
                           'Payroll takes the same federal tax before and after the premium, so this employee gets '
                           'no federal tax saving from it at all. The amount shown is the federal tax being withheld '
                           'each month regardless.'))
    if a.federal == 0 and b.federal not in (None, 0):
        out.append(Finding('No federal tax left to save', per_month(b.federal, emp.pay_periods),
                           'Federal tax falls to nothing once the premium is deducted. The employee gets back all '
                           'the federal tax there was, and no more, so the saving cannot reach the amount the '
                           'proposal promised.'))
    # W-4 instructions, compared with the census rather than inferred from a gap
    w4 = []
    cen_status = (emp.census.filing_status or '').strip().upper()[:1]
    if b.w4_status and cen_status and b.w4_status != cen_status:
        w4.append(f'the statement prints filing status {b.w4_status} and the census carries '
                  f'{cen_status or "none"}')
    if b.w4_extra and not emp.census.additional_federal:
        w4.append(f'the statement withholds an additional {_m(b.w4_extra)} a pay under W-4 Step 4(c) and the census '
                  f'carries no additional federal amount')
    if emp.census.additional_federal and not b.w4_extra:
        w4.append(f'the census carries additional federal withholding of {_m(emp.census.additional_federal)} and the '
                  f'statement prints none')
    if b.w4_multijob == 'Y' and not (emp.census.step2c or '').strip():
        w4.append('the statement marks the W-4 multiple jobs box and the census does not')
    if w4:
        out.append(Finding('The W-4 on payroll differs from the census', None,
                           'Payroll and the census hold different W-4 details: ' + '; '.join(w4) +
                           '. Tax worked out from different W-4 details will not agree.'))

    # something other than the premium moved between the two statements
    if b.gross is not None and a.gross is not None and abs(a.gross - b.gross) > CENT:
        out.append(Finding('Something else changed between the two payslips', per_month(a.gross - b.gross, emp.pay_periods),
                           'Gross pay is not the same on the two payslips, so something changed besides the '
                           'premium and the two are not comparable. Ask for a mock payslip that changes only the '
                           'premium. This check looks at gross pay only, so other changes may still be present.'))
    # A movement in the statement's own "other deductions" total is not used as evidence here: on a scanned pack
    # that total is one of the least reliably read figures, and an untied identity is reported as such instead.
    for tag, pc in (('before', b), ('after', a)):
        d = getattr(pc, 'net_pay_disputed', None)
        if d:
            out.append(Finding('The statement shows two different net pay figures', d.get('difference'),
                               f"The {tag} payslip shows take home pay twice and the two do not agree: the net pay "
                               f"line says {_m(d.get('line'))} and the rest of the payslip says "
                               f"{_m(d.get('corroborated'))}. The reconciliation uses "
                               f"{_m(d.get('corroborated'))}, and this employee is marked as not verified rather "
                               f"than the tool quietly picking one."))
    identity_broken = emp.identity_gap is not None and abs(emp.identity_gap) > 2.0
    explained = any(f.label == 'Something else changed between the two payslips'
                    and f.amount is not None and abs(abs(f.amount) - abs(emp.identity_gap or 0)) <= 2.0
                    for f in out)
    # An untied identity is reported as an untied identity. The tool does not decide which line is at fault: a
    # misreading, an unusual payroll treatment and an omitted line all produce the same arithmetic.
    federal_in_doubt = b.federal_unreliable is not None or a.federal_unreliable is not None
    if identity_broken and not explained:
        out.append(Finding('The statement does not add up', emp.identity_gap,
                           'The figures on the two payslips do not add up: the tax saved, less the employee fee, '
                           'does not come to the change in take home pay'
                           + ('' if getattr(emp, 'fee_from_statement', False) else ', and the employee fee was taken from the '
                              'proposal because the statement prints no after-tax fee line') +
                           '. This tool will not guess which line is wrong, so this employee is marked as not '
                           'verified. Someone should look at the two payslips.'))
    if not federal_in_doubt and emp.federal_gap is not None and abs(emp.federal_gap) > CENT:
        out.append(Finding('Payroll and the proposal use different tax tables', emp.federal_gap,
                           'Payroll and the proposal use different federal tax tables, so they work out slightly '
                           'different tax on the same pay, and the saving lands in a different place. This is a '
                           'settings difference between two systems, not a mistake in either calculation, and '
                           'nothing here says which set of tables is the right one.'))
    if emp.state_gap is not None and abs(emp.state_gap) > CENT:
        detail = ('State tax on the payslip differs from what the proposal worked out by this amount.')
        if emp.before.state_code == 'MO':
            detail = ('Missouri rounds state tax to whole dollars on each payslip while the proposal works to the '
                      'cent, so a small difference every pay period is expected.')
        out.append(Finding('State withholding', emp.state_gap, detail))
    if emp.fica_gap is not None and abs(emp.fica_gap) > CENT:
        out.append(Finding('Social Security and Medicare', emp.fica_gap,
                           'Social Security and Medicare on the payslips differ from the proposal by this amount. '
                           'Before treating it as a fault, check whether this employee pays into Social Security at '
                           'all, and allow for payroll rounding each pay period.'))
    if not out and emp.before.net_pay is None and emp.after.net_pay is None:
        out.append(Finding('No payslip found for this employee', None,
                           'None of the payslips uploaded belong to this employee, so there is nothing to compare '
                           'the proposal against. This is about the files provided, not about the employee.'))
    elif not out:
        out.append(Finding('No cause could be established', emp.allotment_gap,
                           'The files provided do not explain this difference. The payslip lines needed to work it '
                           'out are missing or could not be read.'))
    return out


def _verdict(emp: EmployeeAudit):
    """One line a reader can act on. Plain words on purpose: the people who run this are not auditors."""
    g = emp.allotment_gap
    labels = {f.label for f in emp.findings}
    if g is None:
        if emp.before.net_pay is None and emp.after.net_pay is None:
            return 'No payslip found for this employee', 'grey'
        return 'Could not be checked, a figure is missing', 'grey'
    if 'The statement shows two different net pay figures' in labels:
        return 'Not verified: the payslip shows two different take home figures', 'red'
    if 'The deductions add up to more than the pay' in labels:
        return 'Not verified: the deductions on the payslip do not add up', 'red'
    if 'The statement does not add up' in labels:
        return 'Not verified: the payslip figures do not add up', 'red'
    if abs(g) <= CENT:
        return 'Correct: the employee takes home what was promised', 'green'
    if 'No cause could be established' in labels:
        return 'Difference found, cause not established', 'red'
    side = 'less' if g < 0 else 'more'
    if emp.actual_net_change is not None and emp.actual_net_change < 0:
        return (f'Take home pay falls, and it is {_m(abs(g))} a month {side} than promised. See the cause below',
                'yellow')
    return f'Takes home {_m(abs(g))} a month {side} than promised. See the cause below', 'yellow'


def summarise(audits: list) -> dict:
    """The front summary. Everything a reader needs before the per-employee blocks."""
    n = len(audits)
    matched = [a for a in audits if a.verdict_class == 'green']
    attributed = [a for a in audits if a.verdict_class == 'yellow']
    unexplained = [a for a in audits if a.verdict_class == 'red']
    missing = [a for a in audits if a.verdict_class == 'grey']
    gaps = [a.allotment_gap for a in audits if a.allotment_gap is not None]
    by_cause = {}
    for a in audits:
        for f in a.findings:
            if f.label in ('Match', 'No payslip found for this employee'):
                continue
            c = by_cause.setdefault(f.label, {'employees': 0, 'amount': 0.0})
            c['employees'] += 1
            c['amount'] = r2(c['amount'] + (f.amount or 0))
    covered = [a for a in audits if a.allotment_gap is not None]
    both = sum(1 for a in audits if a.before.net_pay is not None and a.after.net_pay is not None)
    one = sum(1 for a in audits if (a.before.net_pay is None) != (a.after.net_pay is None))
    none = n - both - one
    unverified = [a for a in covered if a.verdict_class == 'red']
    return dict(covered=len(covered), unverified=len(unverified),
                population=dict(both=both, one=one, none=none, unmatched_statements=0),
                employees=n, matched=len(matched), attributed=len(attributed), unexplained=len(unexplained),
                data_missing=len(missing), total_gap=r2(sum(gaps)) if gaps else None,
                decreases=len([a for a in audits if (a.actual_net_change or 0) < 0]),
                causes=sorted(by_cause.items(), key=lambda kv: -abs(kv[1]['amount'])))
