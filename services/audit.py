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
    emp.verdict, emp.verdict_class = _verdict(emp)
    return emp


def _attribute(emp: EmployeeAudit) -> list:
    """Name every cause the data supports, with its amount. Nothing is asserted without a number behind it."""
    out, b, a, e = [], emp.before, emp.after, emp.engine
    if emp.allotment_gap is not None and abs(emp.allotment_gap) <= CENT:
        out.append(Finding('Match', emp.allotment_gap,
                           'The engine allotment equals the actual net pay change.'))
        return out
    if emp.retirement_not_in_census and abs(emp.retirement_not_in_census) > CENT:
        out.append(Finding('Retirement deduction not in the census', emp.retirement_not_in_census,
                           'The paycheck reduces federal taxable wages by this amount each month and no census field carries it, '
                           'so the engine calculated on income the payroll does not tax.'))
    if emp.ti_before_gap is not None and abs(emp.ti_before_gap) > CENT and (emp.retirement_not_in_census or 0) == 0:
        out.append(Finding('Cafeteria deduction not in the census', emp.ti_before_gap,
                           'The engine Taxable Income Before differs from the paycheck Medicare Gross by this amount, '
                           'so a pre-tax deduction on the paycheck is missing from the census.'))
    # extra withholding shows as the same difference before and after the premium
    if b.federal is not None and a.federal is not None and abs(b.federal - a.federal) > CENT:
        if b.federal == a.federal:
            pass
    if b.federal is not None and a.federal is not None and b.federal == a.federal and b.federal > 0:
        out.append(Finding('Fixed federal withholding', per_month(b.federal, emp.pay_periods),
                           'Payroll withholds the same federal amount before and after the premium, so the deduction '
                           'produces no federal saving for this employee.'))
    if a.federal == 0 and b.federal not in (None, 0):
        out.append(Finding('Withholding exhausted', per_month(b.federal, emp.pay_periods),
                           'Federal withholding reaches zero after the premium: the deduction returns all of it and no more '
                           'is available.'))
    identity_broken = emp.identity_gap is not None and abs(emp.identity_gap) > 2.0
    # A broken identity means either a line was misread or something outside the premium moved. Tell them apart by
    # whether every line the identity needs was actually printed and read: if one was supplied from elsewhere or
    # dropped as implausible, the reading is what is in doubt, and the employee is reported as unverified.
    incomplete = (not getattr(emp, 'fee_from_statement', False)
                  or b.federal_unreliable is not None or a.federal_unreliable is not None
                  or any(getattr(pc, k) is None for pc in (b, a)
                         for k in ('gross', 'net_pay', 'federal', 'social_security', 'medicare')))
    if identity_broken and incomplete:
        out.append(Finding('Statement lines inconsistent', emp.identity_gap,
                           'The withholding savings plus the FICA savings less the fee does not equal the net pay change '
                           'on these statements, so at least one printed line could not be read reliably'
                           + ('' if getattr(emp, 'fee_from_statement', False) else ', and the employee fee was taken from the '
                              'proposal because no after-tax fee line was found') +
                           '. Treat this employee\'s figures as unverified and check the statement by hand.'))
    if not (identity_broken and incomplete) and emp.federal_gap is not None and abs(emp.federal_gap) > CENT:
        out.append(Finding('Federal withholding tables', emp.federal_gap,
                           'The engine and the payroll provider hold different withholding tables, which moves the saving '
                           'where the premium falls across a rate boundary. This is a configuration difference, not a '
                           'calculation difference.'))
    if emp.state_gap is not None and abs(emp.state_gap) > CENT:
        detail = 'State withholding differs from the engine calculation.'
        if emp.before.state_code == 'MO':
            detail = ('Missouri withholds in whole dollars each pay period while the engine calculates to the cent, '
                      'and the state parameter sets differ.')
        out.append(Finding('State withholding', emp.state_gap, detail))
    if emp.fica_gap is not None and abs(emp.fica_gap) > CENT:
        out.append(Finding('Social Security and Medicare', emp.fica_gap,
                           'Per-pay FICA rounding, or the employee is outside Social Security.'))
    if (emp.identity_gap is not None and abs(emp.identity_gap) > CENT
            and getattr(emp, 'fee_from_statement', False)
            and emp.payroll_state_savings is not None and emp.payroll_fica_savings is not None):
        out.append(Finding('Non-tax line in net pay', emp.identity_gap,
                           'The net pay change does not equal the tax savings less the fee. A deduction or an earning '
                           'other than the premium changed between the two paychecks.'))
    if not out and emp.before.net_pay is None and emp.after.net_pay is None:
        out.append(Finding('Statement not provided', None,
                           'No payroll statement in the uploaded packs matched this employee, so there is nothing to '
                           'reconcile the engine against. This is a coverage limit of the files, not a finding.'))
    elif not out:
        out.append(Finding('Unattributed', emp.allotment_gap,
                           'The available files do not explain this difference. The paycheck lines needed for the '
                           'reconciliation are missing or unreadable.'))
    return out


def _verdict(emp: EmployeeAudit):
    g = emp.allotment_gap
    if g is None:
        if emp.before.net_pay is None and emp.after.net_pay is None:
            return 'No statement in the uploaded packs', 'grey'
        return 'Not reconciled, data missing', 'grey'
    if abs(g) <= CENT:
        return 'Engine matches payroll', 'green'
    if any(f.label == 'Unattributed' for f in emp.findings):
        return 'Difference not attributed', 'red'
    if any(f.label == 'Statement lines inconsistent' for f in emp.findings):
        return 'Statement could not be read reliably', 'red'
    if emp.actual_net_change is not None and emp.actual_net_change < 0:
        return 'Net pay falls, cause identified', 'yellow'
    return 'Difference attributed', 'yellow'


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
            if f.label in ('Match', 'Statement not provided'):
                continue
            c = by_cause.setdefault(f.label, {'employees': 0, 'amount': 0.0})
            c['employees'] += 1
            c['amount'] = r2(c['amount'] + (f.amount or 0))
    covered = [a for a in audits if a.allotment_gap is not None]
    return dict(covered=len(covered), employees=n, matched=len(matched), attributed=len(attributed), unexplained=len(unexplained),
                data_missing=len(missing), total_gap=r2(sum(gaps)) if gaps else None,
                decreases=len([a for a in audits if (a.actual_net_change or 0) < 0]),
                causes=sorted(by_cause.items(), key=lambda kv: -abs(kv[1]['amount'])))
