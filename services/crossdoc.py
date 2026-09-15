"""What the census and the proposal say a figure on the statement should be.

The three documents are not three independent systems. The proposal is calculated from the census, so a proposal
figure that the engine computed carries no information the census did not already carry, and the figures the audit
exists to test cannot be used to test themselves. Each expectation therefore records its source and whether it may
serve as evidence at all: a proposal input such as the premium or the employee fee may, a proposal output such as
the federal withholding or the taxable income may not.

The statement's own identities can say a reading does not fit, but not which reading is wrong: every figure in a
failing identity looks equally guilty, and choosing the reading that makes the identity tie is circular. The census
and the proposal are different documents produced by different systems, so they are independent evidence about what
was printed, and that is what this module supplies.

Two rules keep it honest, and they come from the review of this design:

  * An expectation about the thing under audit is not evidence. The proposal's federal withholding is the number the
    audit exists to test, so it never validates the statement's federal withholding. It is recorded as context only.
  * The census can be wrong. That is usually the finding. So a census expectation carries a wide tolerance and is
    never used to overwrite a reading, only to choose between readings the scan could have produced and to say that
    a reading disagrees with another document.
"""
from dataclasses import dataclass, asdict
from typing import Dict, Optional


@dataclass
class Expect:
    field: str
    value: float
    sd: float                 # how far a printed figure may legitimately sit from this expectation
    source: str               # the document and the field it came from
    auditable: bool = True    # False where the expectation is the thing under audit, so it cannot be evidence

    def as_dict(self):
        return asdict(self)


def expectations(census, engine, pay_periods=12, which='before') -> Dict[str, Expect]:
    """Expectations for one statement, from the census and the proposal report.

    `census` and `engine` are the dataclasses the audit already builds. `which` selects the statement: the before
    statement carries no premium, reimbursement or fee, the after statement carries all three.
    """
    out = {}
    pp = pay_periods or 12

    def per_pay(monthly):
        return monthly * 12.0 / pp

    if census is not None and census.gross_annual:
        out['gross'] = Expect('gross', round(census.gross_annual / pp, 2), max(1.0, census.gross_annual * 0.02 / pp),
                              f'census annual taxable wages {census.gross_annual:,.2f} over {pp} pay periods')
    if census is not None and census.retirement_401k:
        out['retirement'] = Expect('retirement', round(per_pay(census.retirement_401k), 2),
                                   max(5.0, per_pay(census.retirement_401k) * 0.25),
                                   f'census monthly retirement {census.retirement_401k:,.2f}')
    if engine is not None and engine.premium:
        v = round(per_pay(engine.premium), 2)
        if which == 'after':
            out['premium'] = Expect('premium', v, max(1.0, v * 0.02), f'proposal premium {engine.premium:,.2f}')
            out['reimbursement'] = Expect('reimbursement', -v, max(1.0, v * 0.02),
                                          f'proposal premium returned as a reimbursement')
        else:
            out['premium'] = Expect('premium', 0.0, 0.01, 'the statement before the premium carries no premium line')
    if engine is not None and engine.fee is not None:
        v = round(per_pay(engine.fee), 2)
        out['fee'] = Expect('fee', v if which == 'after' else 0.0, max(1.0, abs(v) * 0.05),
                            f'proposal employee fee {engine.fee:,.2f}' if which == 'after'
                            else 'the statement before the premium carries no fee line')
    if engine is not None:
        ti = engine.taxable_income_before if which == 'before' else engine.taxable_income_after
        if ti:
            v = round(per_pay(ti), 2)
            # The engine's taxable income is calculated from the census, so it is not independent of it, and it is
            # the figure whose correctness the audit is testing. Recorded for comparison, never as evidence.
            out['medicare_gross'] = Expect('medicare_gross', v, max(5.0, v * 0.08),
                                           f'proposal taxable income {ti:,.2f}', auditable=False)
    if engine is not None and engine.federal_before:
        out['federal'] = Expect('federal', round(per_pay(engine.federal_before), 2),
                                max(10.0, per_pay(engine.federal_before) * 0.5),
                                f'proposal federal withholding before the premium {engine.federal_before:,.2f}',
                                auditable=False)
    return out


def disagreements(rec, exp: Dict[str, Expect], k=3.0):
    """Read figures that sit far outside what another document expects, with the document named.

    Only expectations that are not themselves under audit are reported. A disagreement is a finding about the
    documents, not proof that the reading is wrong: the census is often the thing at fault.
    """
    out = []
    for f, e in (exp or {}).items():
        if not e.auditable:
            continue
        v = rec.get(f) if isinstance(rec, dict) else getattr(rec, f, None)
        if v is None:
            continue
        if abs(v - e.value) > k * e.sd:
            out.append(dict(field=f, read=round(float(v), 2), expected=e.value, tolerance=round(k * e.sd, 2),
                            source=e.source))
    return out


def score_candidate(field_name, value, exp: Dict[str, Expect]):
    """How well a candidate reading agrees with the other documents, as a log likelihood contribution.

    Zero where there is no independent expectation, so a field the other documents say nothing about is decided by
    the statement alone.
    """
    e = (exp or {}).get(field_name)
    if e is None or not e.auditable:
        return 0.0, ''
    z = (value - e.value) / max(e.sd, 0.01)
    return -0.5 * z * z, e.source
