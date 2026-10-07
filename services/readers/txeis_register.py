"""TxEIS / Ascender payroll earnings register reader, program HRS2200.

The third register family after the 4packr01 text register and the scanned Paylocity ones.
Seen first at Aspermont ISD, county district 217-901. Two report variants, identical layout:

    DHrs2200PyrlEarningsRpt         the posted run
    DHrs2200PrepostPyrlEarningsRpt  the pre-process run, which is what a mock payroll is

Layout. Each employee is a block of seven lines under a repeated seven line header legend.
Every line ends with the SAME nine money columns, and the fields that precede them (employee
number, cheque number, marital letter, exemptions, remaining payments) are sometimes blank.
So read the last nine numbers of each line and never count from the left; a blank leading
field silently shifts every value one column if you do.

    line 1  Stand Grs  Suppl Pay  N-Tax Bus  Abs Ded  Abs Ref  Units Wrkd  Hrly Rate  Tot Gross  Net Pay
    line 2  Withld Grs Withld Tax EIC Amt    Cafe 125 Annuity  Dep Care    Emplr Cont Other Ded  Net Adjust
    line 3  Med Grs    Med Tax    TRS Grs    TRS Dep  TRS Ins  TRS Sal Red W/C Tx     Emp 457    Emplr 457
    line 4  FICA Grs   FICA Tax   TRS Fd Grs TRS Fd DP TRS Fd Car Unemp Grs Unemp Tax Ovtm Grs  Ovtm Units

Then a job code table and a deduction table: Ded Cd, Emple Amt, Emplr Amt, Caf-125 flag, Ref
flag, three codes to a line. The Caf-125 flag is this register's equivalent of the 4packr01
pre-tax flag, and it is the thing to check first on a wellness audit.

The reader gates itself against the District Totals block the report prints on its own last
page. Nothing downstream runs on an unvalidated parse.

    from txeis_register import read
    reg = read('previous.pdf')     # raises RegisterError unless every control ties
    reg.emps                       # {employee number: employee}
    reg.period                     # {'from','thru','pay_date','frequency'}
    reg.gate                       # [(control, parsed, printed), ...]
"""
import os
import re
import subprocess
import tempfile

NUM = r'-?[\d,]*\.\d\d'
f = lambda s: float(str(s).replace(',', ''))

# A block starts on the employee name line: SURNAME, FORENAMES then the nine money columns.
NAME = re.compile(r'^\s*([A-Z][A-Z\'\-. ]*,[A-Z\'\-. ]+?)\s{2,}(' + NUM + r'(?:\s+' + NUM + r'){8})\s*$')
PERIOD = re.compile(r'For Payroll Period\s+(\S+)\s+Thru\s+(\S+)\s+Pay Date\s+(\S+)'
                    r'(?:\s+Frequency:\s*(\S+))?')
DISTRICT = re.compile(r'Cnty Dist:\s*(\S+)\s+(.+?)\s{2,}')
# Ded Cd rows: code, employee amount, employer amount, Caf-125 flag, Ref flag, up to three sets.
DED = re.compile(r'(\d{3})\s+(' + NUM + r')\s+(' + NUM + r')\s+([YN])\s+([YN])')

# The District Totals block, label to the field it totals.
TOTALS = {
    'gross':      r'Total Gross:\s+(' + NUM + r')',
    'stand':      r'Standard Gross:\s+(' + NUM + r')',
    'suppl':      r'Supplemental Pay:\s+(' + NUM + r')',
    'net':        r'Net Pay:\s+(' + NUM + r')',
    'wh_gross':   r'Withholding Gross:\s+(' + NUM + r')',
    'wh_tax':     r'Withholding Tax:\s+(' + NUM + r')',
    'med_gross':  r'Medicare Gross:\s+(' + NUM + r')',
    'med_tax':    r'Medicare Tax:\s+(' + NUM + r')',
    'fica_gross': r'FICA Gross:\s+(' + NUM + r')',
    'cafe':       r'Cafeteria 125:\s+(' + NUM + r')',
    'other_ded':  r'Other Deductions:\s+(' + NUM + r')',
    'trs_gross':  r'TRS Gross:\s+(' + NUM + r')',
    'trs_dep':    r'TRS Deposit:\s+(' + NUM + r')',
    'annuity':    r'Annuity:\s+(' + NUM + r')',
}
# Which parsed field each printed total must equal.
GATE = [
    ('total gross', 'gross', 'gross'),
    ('standard gross', 'stand', 'stand'),
    ('supplemental pay', 'suppl', 'suppl'),
    ('net pay', 'net', 'net'),
    ('withholding gross', 'wh_gross', 'wh_gross'),
    ('withholding tax', 'wh_tax', 'wh_tax'),
    ('medicare gross', 'med_gross', 'med_gross'),
    ('medicare tax', 'med_tax', 'med_tax'),
    ('fica gross', 'fica_gross', 'fica_gross'),
    ('cafeteria 125', 'cafe', 'cafe'),
    ('other deductions', 'other_ded', 'other_ded'),
    ('trs gross', 'trs_gross', 'trs_gross'),
]
L1 = ['stand', 'suppl', 'ntax_bus', 'abs_ded', 'abs_ref', 'units', 'hrly', 'gross', 'net']
L2 = ['wh_gross', 'wh_tax', 'eic', 'cafe', 'annuity', 'dep_care', 'emplr_cont', 'other_ded', 'net_adj']
L3 = ['med_gross', 'med_tax', 'trs_gross', 'trs_dep', 'trs_ins', 'trs_sal_red', 'wc_tax', 'emp457', 'emplr457']
L4 = ['fica_gross', 'fica_tax', 'trsfd_gross', 'trsfd_dp', 'trsfd_car', 'unemp_gross', 'unemp_tax',
      'ovtm_gross', 'ovtm_units']


class RegisterError(RuntimeError):
    pass


class Register:
    def __init__(self, path, emps, totals, period, district, gate, text):
        self.path, self.emps, self.totals = path, emps, totals
        self.period, self.district, self.gate, self.text = period, district, gate, text

    def __repr__(self):
        return '<TxEIS %s: %d employees, period %s to %s>' % (
            os.path.basename(self.path), len(self.emps),
            self.period.get('from'), self.period.get('thru'))

    def sum(self, field):
        return round(sum(v.get(field, 0.0) for v in self.emps.values()), 2)

    def by_name(self):
        return {v['name']: v for v in self.emps.values()}

    def ded(self, code):
        return round(sum(v['ded'].get(code, {}).get('emple', 0.0) for v in self.emps.values()), 2)

    def codes(self):
        return {c for v in self.emps.values() for c in v['ded']}


def to_text(pdf_path):
    out = os.path.join(tempfile.mkdtemp(), 'reg.txt')
    r = subprocess.run(['pdftotext', '-layout', pdf_path, out], capture_output=True)
    if r.returncode or not os.path.exists(out):
        raise RegisterError('pdftotext failed on %s: %s' % (pdf_path, r.stderr.decode()[:300]))
    return open(out, errors='replace').read()


def _tail9(line):
    """The last nine money columns of a line. Never count from the left: the employee number,
    cheque number, marital letter and exemption count are each sometimes blank, and counting
    forwards shifts every value one column when they are."""
    nums = re.findall(NUM, line)
    return [f(x) for x in nums[-9:]] if len(nums) >= 9 else None


def _key(name):
    """Census-style join key, as register.census_key builds for the 4packr01 family."""
    last, first = (name.split(',', 1) + [''])[:2]
    return (re.sub(r'[^A-Z]', '', last.upper())[:5] +
            re.sub(r'[^A-Z]', '', first.upper())[:3])


def _parse(text):
    emps, lines = {}, text.split('\n')
    cur = None
    for i, ln in enumerate(lines):
        m = NAME.match(ln)
        if m:
            name = re.sub(r'\s+', ' ', m.group(1)).strip()
            if name.startswith('DISTRICT') or 'TOTALS' in name:
                cur = None
                continue
            vals = _tail9(ln)
            if vals is None:
                continue
            cur = dict(name=name, key=_key(name), ded={}, emp_nbr='', marital='')
            cur.update(dict(zip(L1, vals)))
            # the employee number opens the next line; the marital letter opens the one after
            nxt = lines[i + 1] if i + 1 < len(lines) else ''
            mm = re.match(r'\s*(\d{4,8})\b', nxt)
            cur['emp_nbr'] = mm.group(1) if mm else name
            for off, names in ((1, L2), (2, L3), (3, L4)):
                if i + off < len(lines):
                    v = _tail9(lines[i + off])
                    if v:
                        cur.update(dict(zip(names, v)))
            mm = re.match(r'\s*([MSH])\b', lines[i + 2] if i + 2 < len(lines) else '')
            cur['marital'] = mm.group(1) if mm else ''
            emps[cur['emp_nbr']] = cur
            continue
        if cur is not None:
            for code, emple, emplr, caf, ref in DED.findall(ln):
                cur['ded'][code] = dict(emple=f(emple), emplr=f(emplr), caf125=caf, ref=ref)
    return emps


def _printed(text):
    tail = text[text.rfind('District Totals:'):] if 'District Totals:' in text else ''
    out = {}
    for k, pat in TOTALS.items():
        m = re.search(pat, tail)
        if m:
            out[k] = f(m.group(1))
    return out


def read(pdf_path, strict=True):
    """Parse and gate. Raises RegisterError unless every control ties to the printed totals."""
    text = to_text(pdf_path)
    emps = _parse(text)
    printed = _printed(text)
    if not printed.get('gross'):
        raise RegisterError('no District Totals block found in %s' % pdf_path)

    gate, bad = [], []
    for label, field, pkey in GATE:
        if pkey not in printed:
            continue
        parsed = round(sum(v.get(field, 0.0) for v in emps.values()), 2)
        gate.append((label, parsed, printed[pkey]))
        if abs(parsed - printed[pkey]) > 0.02:
            bad.append('%s: parsed %.2f against printed %.2f' % (label, parsed, printed[pkey]))
    if bad and strict:
        raise RegisterError('%s did not tie on %d of %d controls:\n  %s'
                            % (os.path.basename(pdf_path), len(bad), len(gate), '\n  '.join(bad)))

    m = PERIOD.search(text)
    period = dict(zip(('from', 'thru', 'pay_date', 'frequency'), m.groups())) if m else {}
    m = DISTRICT.search(text)
    district = dict(cnty_dist=m.group(1), name=m.group(2).strip()) if m else {}
    return Register(pdf_path, emps, printed, period, district, gate, text)
