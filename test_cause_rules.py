"""Regression net for the causeAttribution decision table. Synthetic employees, one per rule,
plus the full Tioga sample pack run end to end. Run:  python test_cause_rules.py"""
import sys
from services.audit import (EmployeeAudit, Paycheck, Engine, Census, audit_employee)

FAIL = []


def check(label, cond, extra=''):
    if not cond:
        FAIL.append(f'{label} {extra}')


def base():
    """A Garra-shaped employee that ties to the cent."""
    return EmployeeAudit(
        name='CASE', pay_periods=12,
        census=Census(gross_annual=79935.83, pay_periods=12, pretax_other=270, retirement_401k=549.58,
                      filing_status='S', state='TX', socialsec='N', medicare='Y'),
        engine=Engine(federal_before=549.35, federal_savings=170.77, state_savings=0, ss_savings=0,
                      medicare_savings=17.01, gross_savings=187.78, fee=114, allotment=73.78,
                      taxable_income_before=6391.32, taxable_income_after=5218.32, premium=1173),
        before=Paycheck(gross=6661.31, federal=549.35, state=0, state_code='TX', medicare=92.67,
                        taxable_wages=5841.75, medicare_gross=6391.31, net_pay=4500.00,
                        retirement=549.56, retirement_line=True),
        after=Paycheck(gross=6661.31, federal=378.58, state=0, state_code='TX', medicare=75.67,
                       taxable_wages=4668.75, medicare_gross=5218.31, net_pay=4573.78,
                       premium=1173, reimbursement=1173, fee=114, retirement=549.56, retirement_line=True))


def labels(e):
    return [f.label for f in audit_employee(e).findings]


# 1. an exact tie is a Match and nothing else
e = base()
e.before.net_pay, e.after.net_pay = 4500.00, 4573.78
ls = labels(e)
check('match only', ls == ['Match'], str(ls))

# 2. Social Security savings claimed on a payroll with no SS line
e = base()
e.engine.ss_savings = 72.73
e.engine.allotment = 146.51            # promise inflated by the phantom SS saving
ls = labels(e)
check('ss_on fires', 'The proposal counts Social Security savings this payroll never pays' in ls, str(ls))

# amount is the negative of the claimed saving
e = base(); e.engine.ss_savings = 72.73; e.engine.allotment = 146.51
a = audit_employee(e)
amt = [f.amount for f in a.findings
       if f.label == 'The proposal counts Social Security savings this payroll never pays']
check('ss_on amount is positive', amt and abs(amt[0] - 72.73) < 0.01, str(amt))

# 3. payroll deducts SS the proposal ignores
e = base()
e.before.social_security, e.after.social_security = 396.26, 323.53
e.engine.allotment = 100.00
ls = labels(e)
check('ss_off fires', 'The payroll pays Social Security the proposal ignores' in ls, str(ls))

# 4. the fee in the proposal is not the fee payroll deducts
e = base()
e.after.fee = 130                      # payroll deducts 130, proposal promised at 114
e.after.net_pay = 4557.78              # the 16 comes out of take home
ls = labels(e)
check('fee fires', 'The employee fee in the proposal is not the fee payroll deducts' in ls, str(ls))
e = base(); e.after.fee = 130; e.after.net_pay = 4557.78
amt = [f.amount for f in audit_employee(e).findings
       if f.label == 'The employee fee in the proposal is not the fee payroll deducts']
check('fee amount is positive', amt and abs(amt[0] - 16.0) < 0.01, str(amt))

# the green gate: an employee who takes home MORE than promised is green, no cause hunt
e = base()
e.after.fee = 89
e.after.net_pay = 4598.78
a = audit_employee(e)
check('above promise is green', a.verdict_class == 'green', a.verdict_class)
check('above promise carries no causes', [f.label for f in a.findings] == ['Match'], str([f.label for f in a.findings]))

# 5. a deduction sits in the wrong census column
e = base()
e.engine.taxable_income_before = 5841.74   # engine started 549.57 lower: TRS went into Other pre-tax
e.census.pretax_other = 819.58
e.census.retirement_401k = 0
e.engine.allotment = 75.00             # the shifted base moved the promise off the payslip
ls = labels(e)
check('wrong column fires', 'A deduction sits in the wrong census column' in ls, str(ls))

# 6. the proposal calculated on no income at all
e = base()
e.engine.taxable_income_before = 0.0
e.engine.allotment = 0.0
e.after.net_pay = 4490.00              # take home actually falls
ls = labels(e)
check('salary zero fires', 'The proposal calculated on no income at all' in ls, str(ls))

# 7. the premium on the payslip is not the premium in the proposal
e = base()
e.after.premium = 1000
e.after.net_pay = 4560.00
ls = labels(e)
check('premium fires', 'The premium on the payslip is not the premium in the proposal' in ls, str(ls))

# 8. the old rules still fire: retirement missing from the census
e = base()
e.census.retirement_401k = 0
e.engine.allotment = 161.07            # promised without knowing the TRS deduction
ls = labels(e)
check('retirement missing still fires', 'Retirement deduction missing from the census' in ls, str(ls))

# 9. fixed federal amount
e = base()
e.after.federal = e.before.federal     # no federal saving at all
e.after.net_pay = 4420.00
ls = labels(e)
check('fixed fed still fires', 'Payroll withholds a fixed federal amount' in ls, str(ls))

# 10. W-4 differs
e = base()
e.before.w4_extra = 350.0
e.engine.allotment = 80.00
ls = labels(e)
check('w4 still fires', 'The W-4 on payroll differs from the census' in ls, str(ls))

# 11. federal figures do not match: the report is out of date
e = base()
e.after.federal = 389.00               # payroll saves 160.35 where the report promises 170.77
e.after.net_pay = 4563.36
ls = labels(e)
check('federal mismatch fires', "The proposal's federal saving does not match payroll" in ls, str(ls))

# 11b. the residual is suppressed while an upstream census cause is open
e = base()
e.census.retirement_401k = 0           # retirement missing: an upstream, actionable cause
e.census.socialsec = ''                # and the SocialSec setting is open too
e.after.federal = 389.00
e.after.net_pay = 4563.36
ls = labels(e)
check('upstream causes present', 'Retirement deduction missing from the census' in ls, str(ls))
check('residual suppressed while upstream open',
      "The proposal's federal saving does not match payroll" not in ls, str(ls))

# 11c. the SocialSec rule fires when the census does not carry N
e = base()
e.census.socialsec = ''
e.engine.allotment = 90.0
ls = labels(e)
check('SocialSec N rule fires', 'The census does not have Social Security set to N' in ls, str(ls))

# 12. no payslip at all
e = base()
e.before.net_pay = e.after.net_pay = None
ls = labels(e)
check('no payslip', 'No payslip found for this employee' in ls, str(ls))

if FAIL:
    print(f'FAIL ({len(FAIL)})')
    for f in FAIL:
        print('  ' + f)
    sys.exit(1)
print(f'cause-rules unit tests PASS')

# ---- per-field rules, one synthetic case each (kept from the catalogue build) --------
def base2():
    return EmployeeAudit(name='CASE', pay_periods=12,
        census=Census(gross_annual=79935.83, pay_periods=12, pretax_other=270, retirement_401k=549.58,
                      filing_status='S', state='TX', socialsec='N', medicare='Y'),
        engine=Engine(federal_savings=170.77, state_savings=0, ss_savings=0, medicare_savings=17.01,
                      gross_savings=187.78, fee=114, allotment=73.78, taxable_income_before=6391.32, premium=1173),
        before=Paycheck(gross=6661.31, federal=549.35, state=0, medicare=92.67, taxable_wages=5841.75,
                        medicare_gross=6391.31, net_pay=4500.00, retirement=549.56, retirement_line=True),
        after=Paycheck(gross=6661.31, federal=378.58, state=0, medicare=75.67, taxable_wages=4668.75,
                       medicare_gross=5218.31, net_pay=4560.00, premium=1173, reimbursement=1173, fee=114,
                       retirement=549.56, retirement_line=True))


def L2(e):
    return [f.label for f in audit_employee(e).findings]


e = base2(); e.after.taxable_wages = 5000.00
check('premium_not_pretax', 'The premium was not taken pre-tax in full' in L2(e), str(L2(e)))
e = base2(); e.after.medicare_gross = 6000.00
check('premium_not_medicare', 'The premium did not come out of Medicare wages in full' in L2(e), str(L2(e)))
e = base2(); e.after.reimbursement = 900.00
check('reimb_partial', 'The reimbursement does not return the whole premium' in L2(e), str(L2(e)))
e = base2(); e.after.retirement = 430.00
check('ret_changed', 'The retirement deduction changed with the premium' in L2(e), str(L2(e)))
e = base2(); e.before.medicare = 0.0; e.after.medicare = 0.0
check('med_on', 'The proposal counts Medicare savings this payroll never pays' in L2(e), str(L2(e)))
e = base2(); e.engine.allotment = 90.00
check('report_internal', 'The proposal report does not add up internally' in L2(e), str(L2(e)))
e = base2(); e.census.found = False
check('census_missing', 'No census row matches this employee' in L2(e), str(L2(e)))

if FAIL:
    print(f'per-field FAIL ({len(FAIL)})')
    for f in FAIL:
        print('  ' + f)
    sys.exit(1)
print('per-field rule tests PASS (7)')

# ---- round three: attribution, not blanket suppression ------------------------------
# A census defect absorbs only what it can explain. An independent federal defect on the
# same employee must still be reported.
e = base2()
e.census.retirement_401k = 0            # upstream defect worth at most 37% of $549.56 = $203
e.engine.federal_savings = 900.00       # a difference far beyond what that can explain
e.engine.allotment = 200.0
ls = L2(e)
check('independent federal defect survives attribution',
      "The proposal's federal saving does not match payroll" in ls, str(ls))

# The same defect alone, with a difference inside what it explains, stays suppressed.
e = base2()
e.census.retirement_401k = 0
e.engine.federal_savings = 200.00       # within 37% of the missing $549.56
e.engine.allotment = 90.0
ls = L2(e)
check('explained federal difference stays suppressed',
      "The proposal's federal saving does not match payroll" not in ls, str(ls))

# A ceiling is only a finding when the promise exceeds it.
e = base2()
e.before.federal, e.after.federal = 55.23, 0.0
e.engine.federal_savings = 170.77       # promise far above the $55.23 available
e.after.net_pay = 4400.00
ls = L2(e)
check('ceiling fires when the promise exceeds it',
      'The promised federal saving is more than the federal tax available' in ls, str(ls))

e = base2()
e.before.federal, e.after.federal = 55.23, 0.0
e.engine.federal_savings = 55.23        # promise equals what was available
e.engine.allotment = 20.0
e.after.net_pay = 4500.00
ls = L2(e)
check('ceiling silent when the promise fits',
      'The promised federal saving is more than the federal tax available' not in ls, str(ls))

# Taxable wages falling by more than the premium is its own finding, with a positive amount.
e = base2()
e.after.taxable_wages = 4000.00         # fell more than the $1,173 premium
a = audit_employee(e)
lab = [f.label for f in a.findings]
check('two-sided pre-tax check', 'Taxable wages fell by more than the premium' in lab, str(lab))
amt = [f.amount for f in a.findings if f.label == 'Taxable wages fell by more than the premium']
check('and its amount is positive', amt and amt[0] > 0, str(amt))

# Dependency order: evidence validity first, inputs next, residuals last.
e = base2()
e.census.retirement_401k = 0
e.before.gross = 7000.00                # gross moved: an evidence-validity finding
a = audit_employee(e)
order = [f.label for f in a.findings]
if 'Something else changed between the two payslips' in order and 'Retirement deduction missing from the census' in order:
    check('evidence validity is listed before census inputs',
          order.index('Something else changed between the two payslips')
          < order.index('Retirement deduction missing from the census'), str(order))

if FAIL:
    print(f'round-three FAIL ({len(FAIL)})')
    for f in FAIL:
        print('  ' + f)
    sys.exit(1)
print('round-three tests PASS (7)')
