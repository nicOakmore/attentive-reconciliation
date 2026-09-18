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
    found: bool = True          # a census row actually matched this employee
    socialsec: str = ''         # census SocialSec column, Y or N
    medicare: str = ''          # census Medicare column, Y or N


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
    ss_note: str = ''

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
    # Where a payslip does not add up and the census plus the statement's own arithmetic say decisively what one
    # misread line must have been, the audit uses the corrected figure and reports the correction, instead of
    # accepting a printed number the page itself contradicts. One pass only: a correction never begets another.
    if emp.uncertainty and not getattr(emp, '_correction_pass', False):
        applied = []
        for st in (emp.uncertainty.get('statements') or []):
            ml = st.get('most_likely') or {}
            reads = ml.get('readings') or []
            if not (ml.get('decisive') and reads):
                continue
            top_v, fld, obs = reads[0].get('value'), ml.get('field'), ml.get('observed')
            if top_v is None or fld is None or obs is None or abs(top_v - obs) <= 0.02:
                continue
            pc = emp.before if st['statement'] == 'before' else emp.after
            setattr(pc, fld, top_v)
            applied.append((st['statement'], fld, obs, top_v, ml.get('source') or ''))
        if applied:
            emp._correction_pass = True
            emp.findings = []
            audit_employee(emp)
            for tag, fld, obs, new, src in applied:
                emp.findings.append(Finding(
                    'A misread figure was corrected from the census and the statement arithmetic', r2(new - obs),
                    f"On the {tag} payslip the {fld.replace('_', ' ')} line was read as {_m(obs)}, but the page "
                    f"does not add up with that figure and does with {_m(new)}"
                    + (f", which {src} also gives" if src else '')
                    + f". The audit uses {_m(new)}. The correction is reported so a reader can check that line "
                    f"on the scan."))
            return emp
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
                                            decisive=post.get('decisive'),
                                            source=post.get('cross_document_source'),
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


def _evidence(emp: EmployeeAudit) -> dict:
    """Every fact the cause rules are allowed to reason from, as one flat record. The decision of which
    cause applies, and its amount, is taken by the causeAttribution table in cause_rules.json, not here."""
    b, a, e, c = emp.before, emp.after, emp.engine, emp.census
    pp = emp.pay_periods
    yn = lambda t: 'Y' if t else 'N'
    ret_missing = emp.retirement_not_in_census
    ret_named_known = bool(b.retirement_line and b.retirement)
    ret_named = per_month(b.retirement, pp) if ret_named_known else 0.0
    ret_unnamed = r2((ret_missing or 0) - (ret_named or 0))
    ti_gap = emp.ti_before_gap
    slips_present = b.net_pay is not None and a.net_pay is not None
    ss_on_slips = (b.social_security or 0) > 0.005 or (a.social_security or 0) > 0.005
    eng_ss = e.ss_savings
    pay_ss_sav = per_month((b.social_security or 0) - (a.social_security or 0), pp)
    fee_pay = per_month(a.fee, pp) if a.fee is not None else None
    fee_known = fee_pay is not None and e.fee is not None
    fee_gap = r2(fee_pay - e.fee) if fee_known else None
    slip_prem = per_month(a.premium, pp) if a.premium is not None else None
    premium_known = slip_prem is not None and e.premium is not None
    premium_gap = r2(slip_prem - e.premium) if premium_known else None
    # payroll execution of the premium: pre-tax treatment, Medicare wages, reimbursement
    tw_change = (per_month(b.taxable_wages - a.taxable_wages, pp)
                 if b.taxable_wages is not None and a.taxable_wages is not None else None)
    pretax_known = tw_change is not None and slip_prem is not None
    premium_pretax_gap = r2(slip_prem - tw_change) if pretax_known else None
    medg_change = (per_month(b.medicare_gross - a.medicare_gross, pp)
                   if b.medicare_gross is not None and a.medicare_gross is not None else None)
    medg_known = medg_change is not None and slip_prem is not None
    medg_gap = r2(slip_prem - medg_change) if medg_known else None
    reimb_m = per_month(a.reimbursement, pp) if a.reimbursement is not None else None
    reimb_gap = r2(abs(reimb_m) - slip_prem) if reimb_m is not None and slip_prem is not None else None
    reimb_missing_sig = (reimb_m is None and slip_prem is not None and slip_prem > 0
                         and emp.identity_gap is not None
                         and abs(emp.identity_gap + slip_prem) <= max(2.0, 0.02 * slip_prem))
    med_read = b.medicare is not None or a.medicare is not None
    med_on_slips = (b.medicare or 0) > 0.005 or (a.medicare or 0) > 0.005
    eng_med = e.medicare_savings
    pay_med_sav = per_month((b.medicare or 0) - (a.medicare or 0), pp)
    med_mismatch = (slips_present and med_read
                    and (((eng_med or 0) > 0.02 and not med_on_slips)
                         or (med_on_slips and eng_med is not None and eng_med <= 0.02
                             and (pay_med_sav or 0) > 0.02)))
    ret_change = (per_month(b.retirement - a.retirement, pp)
                  if b.retirement is not None and a.retirement is not None else None)
    report_int_known = (e.gross_savings is not None and e.fee is not None and e.allotment is not None)
    report_int_gap = r2(e.gross_savings - e.fee - e.allotment) if report_int_known else None
    # The rerun instruction, composed from what this employee's own data says needs changing. Named fields only:
    # a reader must be able to act on it without reading the rest of the block.
    fixes = []
    if (ret_missing or 0) > CENT:
        fixes.append(f'retirement {_m(ret_missing)} in the 401-k/IRA column')
    if slips_present and not ss_on_slips and (c.socialsec or '') != 'N':
        fixes.append('SocialSec set to N')
    if slips_present and ss_on_slips and (c.socialsec or '') == 'N':
        fixes.append('SocialSec set to Y')
    if slips_present and med_read and not med_on_slips and (c.medicare or '') != 'N':
        fixes.append('Medicare set to N')
    if fee_known and abs(fee_gap or 0) > CENT:
        fixes.append(f'the employee fee at {_m(fee_pay)}')
    if premium_known and abs(premium_gap or 0) > CENT:
        fixes.append(f'the premium at {_m(slip_prem)}')
    if fixes:
        rerun_fix = 'Correct the census for this employee (' + ', '.join(fixes) + ') and rerun the proposal.'
    else:
        rerun_fix = 'Check this employee\'s census row against the payslip and rerun the proposal.'
    gross_moved = (per_month(a.gross - b.gross, pp)
                   if b.gross is not None and a.gross is not None else None)
    identity_broken = emp.identity_gap is not None and abs(emp.identity_gap) > 2.0
    identity_explained = (gross_moved is not None and abs(a.gross - b.gross) > CENT
                          and abs(abs(gross_moved) - abs(emp.identity_gap or 0)) <= 2.0)
    ss_mismatch = slips_present and (((eng_ss or 0) > 0.02 and not ss_on_slips)
                                     or (ss_on_slips and eng_ss is not None and eng_ss <= 0.02))
    w4 = []
    cen_status = (c.filing_status or '').strip().upper()[:1]
    if b.w4_status and cen_status and b.w4_status != cen_status:
        w4.append(f'the statement prints filing status {b.w4_status} and the census carries '
                  f'{cen_status or "none"}')
    if b.w4_extra and not c.additional_federal:
        w4.append(f'the statement withholds an additional {_m(b.w4_extra)} a pay under W-4 Step 4(c) and the census '
                  f'carries no additional federal amount')
    if c.additional_federal and not b.w4_extra:
        w4.append(f'the census carries additional federal withholding of {_m(c.additional_federal)} and the '
                  f'statement prints none')
    if b.w4_multijob == 'Y' and not (c.step2c or '').strip():
        w4.append('the statement marks the W-4 multiple jobs box and the census does not')
    return dict(
        gap=emp.allotment_gap,
        ret_missing=ret_missing, ret_missing_abs=abs(ret_missing or 0),
        ret_named=ret_named, ret_unnamed=ret_unnamed, ret_unnamed_abs=abs(ret_unnamed or 0),
        ret_named_known=yn(ret_named_known), ret_zero=yn((ret_missing or 0) == 0),
        ti_gap=ti_gap, ti_gap_abs=abs(ti_gap) if ti_gap is not None else None,
        wrong_col_match=yn(ti_gap is not None and (c.pretax_other or 0) >= abs(ti_gap) - 0.02),
        slips_present=yn(slips_present), ss_on_slips=yn(ss_on_slips),
        eng_ss=eng_ss, eng_ss_known=yn(eng_ss is not None), pay_ss_sav=pay_ss_sav,
        ss_mismatch=yn(ss_mismatch),
        fee_known=yn(fee_known), fee_eng=e.fee, fee_pay=fee_pay,
        fee_gap=fee_gap, fee_gap_abs=abs(fee_gap) if fee_gap is not None else None,
        premium_known=yn(premium_known), premium_gap=premium_gap,
        premium_gap_abs=abs(premium_gap) if premium_gap is not None else None,
        eng_ti_known=yn(e.taxable_income_before is not None),
        eng_ti_zero=yn(e.taxable_income_before is not None and abs(e.taxable_income_before) <= 0.02),
        census_gross=c.gross_annual or 0,
        fixed_fed=yn(b.federal is not None and a.federal is not None
                     and b.federal == a.federal and b.federal > 0),
        fed_before_m=per_month(b.federal, pp),
        fed_zero=yn(a.federal == 0 and b.federal not in (None, 0)),
        w4_diff=yn(bool(w4)), w4_text='; '.join(w4),
        gross_moved=gross_moved, gross_moved_abs=abs(gross_moved) if gross_moved is not None else None,
        identity_broken=yn(identity_broken), identity_explained=yn(identity_explained),
        identity_gap=emp.identity_gap, fee_from_stmt=yn(getattr(emp, 'fee_from_statement', False)),
        fed_in_doubt=yn(b.federal_unreliable is not None or a.federal_unreliable is not None),
        federal_gap=emp.federal_gap,
        federal_gap_abs=abs(emp.federal_gap) if emp.federal_gap is not None else None,
        state_gap=emp.state_gap,
        state_gap_abs=abs(emp.state_gap) if emp.state_gap is not None else None,
        state_is_mo=yn(b.state_code == 'MO'),
        fica_gap=emp.fica_gap,
        fica_gap_abs=abs(emp.fica_gap) if emp.fica_gap is not None else None,
        premium_m=slip_prem,
        pretax_known=yn(pretax_known), premium_pretax_gap=premium_pretax_gap,
        premium_pretax_gap_abs=abs(premium_pretax_gap) if premium_pretax_gap is not None else None,
        medg_known=yn(medg_known), medg_gap=medg_gap,
        medg_gap_abs=abs(medg_gap) if medg_gap is not None else None,
        reimb_known=yn(reimb_m is not None), reimb_gap=reimb_gap,
        reimb_gap_abs=abs(reimb_gap) if reimb_gap is not None else None,
        reimb_missing_sig=yn(reimb_missing_sig),
        med_read=yn(med_read), med_on_slips=yn(med_on_slips),
        eng_med=eng_med, eng_med_known=yn(eng_med is not None), pay_med_sav=pay_med_sav,
        med_mismatch=yn(med_mismatch),
        ret_change=ret_change, ret_change_abs=abs(ret_change) if ret_change is not None else None,
        report_int_known=yn(report_int_known), report_int_gap=report_int_gap,
        report_int_gap_abs=abs(report_int_gap) if report_int_gap is not None else None,
        census_found=yn(getattr(c, 'found', True)),
        census_ss=(c.socialsec or ''), census_med=(c.medicare or ''),
        eng_fed_sav=e.federal_savings, pay_fed_sav=emp.payroll_federal_savings,
        rerun_fix=rerun_fix,
    )


def _attribute(emp: EmployeeAudit) -> list:
    """Name every cause the data supports, with its amount. Nothing is asserted without a number behind it.
    The decisions live in the causeAttribution decision table; this assembles the evidence, runs the table,
    and puts the matched rows into words."""
    from . import oakmore_rules as OR
    out, b, a = [], emp.before, emp.after
    if emp.allotment_gap is not None and emp.allotment_gap >= -CENT:
        # The payment is not down: green, and that is it. No cause hunt on an employee who takes
        # home at least what was promised.
        out.append(Finding('Match', emp.allotment_gap, 'Takes home at least what was promised.'))
        return out
    ev = _evidence(emp)
    fmt = {k: (_m(v) if isinstance(v, (int, float)) else v) for k, v in ev.items()}
    fmt['ret_unnamed'] = _m(abs(ev['ret_unnamed'] or 0))
    templates = OR.details()
    for row in OR.solve('causeAttribution', ev):
        detail = templates.get(row.get('detail'), row.get('detail') or '')
        try:
            detail = detail.format(**fmt)
        except (KeyError, IndexError):
            pass
        out.append(Finding(row['label'], r2(row['amount']), detail))
    # The statement's own inconsistencies are reported from the reading layer, not the rules table
    for tag, pc in (('before', b), ('after', a)):
        d = getattr(pc, 'net_pay_disputed', None)
        if d:
            out.append(Finding('The statement shows two different net pay figures', d.get('difference'),
                               f"The {tag} payslip shows take home pay twice and the two do not agree: the net pay "
                               f"line says {_m(d.get('line'))} and the rest of the payslip says "
                               f"{_m(d.get('corroborated'))}. The reconciliation uses "
                               f"{_m(d.get('corroborated'))}, and this employee is marked as not verified rather "
                               f"than the tool quietly picking one."))
    if not out and emp.before.net_pay is None and emp.after.net_pay is None:
        out.append(Finding('No payslip found for this employee', None,
                           'No uploaded payslip belongs to this employee; nothing to compare. A files gap, not an employee finding.'))
    elif not out:
        out.append(Finding('No cause could be established', emp.allotment_gap,
                           'The files provided do not explain this difference: the payslip lines needed are missing or unreadable.'))
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
    if g >= -CENT:
        return 'Correct: takes home what was promised', 'green'
    if 'No cause could be established' in labels:
        return 'Difference found, cause not established', 'red'
    if emp.actual_net_change is not None and emp.actual_net_change < 0:
        return (f'Take home pay falls {_m(abs(emp.actual_net_change))} a month, {_m(abs(g))} short of the promise',
                'yellow')
    return f'Takes home {_m(abs(g))} a month less than promised', 'yellow'


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
