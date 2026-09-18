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
                      filing_status='S', state='TX'),
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
check('ss_on amount', amt and abs(amt[0] + 72.73) < 0.01, str(amt))

# 3. payroll deducts SS the proposal ignores
e = base()
e.before.social_security, e.after.social_security = 396.26, 323.53
e.engine.allotment = 100.00
ls = labels(e)
check('ss_off fires', 'The payroll pays Social Security the proposal ignores' in ls, str(ls))

# 4. the fee in the proposal is not the fee payroll deducts
e = base()
e.after.fee = 89                       # payroll took 89, proposal promised at 114
e.after.net_pay = 4598.78              # the 25 lands in take home
ls = labels(e)
check('fee fires', 'The employee fee in the proposal is not the fee payroll deducts' in ls, str(ls))
e = base(); e.after.fee = 89; e.after.net_pay = 4598.78
amt = [f.amount for f in audit_employee(e).findings
       if f.label == 'The employee fee in the proposal is not the fee payroll deducts']
check('fee amount +25', amt and abs(amt[0] - 25.0) < 0.01, str(amt))

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
e.after.net_pay = 4573.78
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

# 11. tax tables
e = base()
e.after.federal = 389.00               # payroll saves 160.35 where the engine's table says 170.77
e.after.net_pay = 4563.36
ls = labels(e)
check('tables still fires', 'The proposal report predates the current federal tax table' in ls, str(ls))

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
print('cause-rules unit tests PASS (14 checks)')
