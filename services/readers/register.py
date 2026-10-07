"""Payroll register reader for report 4packr01, client agnostic.

The register names no vendor: its banner is just "4packr01.p-4" over "Check Register for
Payroll Run". Do not attribute it to Skyward or anyone else in client writing.

Reads a register PDF, and gates itself against the PAYROLL TOTALS page that the register
prints on its own last page. If the parse does not tie, it raises. Nothing downstream runs
on an unvalidated parse.

    from register import read
    reg = read('previous.pdf')      # raises RegisterError if it does not tie
    reg.emps                        # {code: employee}
    reg.totals                      # what the register printed
    reg.flags                       # {deduction code: 'D' | 'DS' | 'DSF'}

Why the gates exist, each one from a real failure:
  * extract with `pdftotext -layout`; a whitespace-collapsing dump destroys the fixed-width
    employee code and silently folds employees into their predecessor
  * the code is NOT fixed width (ADAMSANG000 is 11 chars, COX   BRY000 is 12), so normalise
    by stripping spaces
  * a dollar tie alone is not enough: assert the printed employee count too
  * the Medicare row prints a stray type letter ("TAX M"), so take the tail of a deduction
    line and pick out the DSF token rather than expecting the flag to end the line
"""
import os, re, subprocess, tempfile, collections

NUM = r'-?[\d,]+\.\d\d'
f = lambda s: float(str(s).replace(',', ''))

HDR = re.compile(r'^\s*(\d{9})\s+(\S.*?)\b([A-Z][A-Z\'\-]*(?:\s[A-Z][A-Z\'\-]*)*),\s+([A-Z][A-Z\'. \-]*?)(?:\s{2,}|\s*$)')
W4 = re.compile(r'2020 OR AFTER W-4:\s*(\w+)\s+STEP 3:\s*([\d,]+(?:\.\d\d)?)\s+STEP 4A:\s*(' + NUM +
                r')\s+STEP 4B:\s*(' + NUM + r')\s+STEP 2:\s*(\w+)')
GRS = re.compile(r'FED TX GRS\s+(' + NUM + r')\s+STA TX GRS\s+(' + NUM + r')\s+FICA GROSS\s+(' + NUM +
                 r')\s+MED GROSS\s+(' + NUM + r')\s+NET AMOUNT\s+(' + NUM + r')')
TOT = re.compile(r'^\s*TOTALS\s+(' + NUM + r')')
DED = re.compile(r'^\s*([0-9A-Z][0-9A-Z]{1,5})\s+(' + NUM + r')\s+(?:(' + NUM + r')\s+)?(' + NUM +
                 r')\s+(TAX|RET|OTH|MSC|TS2)\b(.*)$')
FLAG = re.compile(r'^[DSF]{1,3}$')

# the PAYROLL TOTALS page
T_ROWS = {
    'gross':  r'TOTAL GROSS PAY\s*:\s*(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
    'tsa':    r"TOTAL TSA'S\s*-?\s*BEFORE TAX\s*:\s*(" + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
    'ret':    r'TOTAL TAX SHELTERED RETIREMENT:\s*(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
    'oth':    r'TOTAL OTHER BEF TAX DEDUCTIONS:\s*(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
    'taxable': r'TOTAL TAXABLE GROSS\s*:\s*(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
}
T_EMPS = re.compile(r'TOTAL EMPLOYEES\s*:\s*(\d+)')
TSA_CODES_RE = re.compile(r'^\s*(4[0-9A-Z]{3})\s')


class RegisterError(RuntimeError):
    pass


class Register:
    def __init__(self, path, emps, totals, flags, tsa_codes, text):
        self.path, self.emps, self.totals = path, emps, totals
        self.flags, self.tsa_codes, self.text = flags, tsa_codes, text

    def __repr__(self):
        return '<Register %s: %d employees, %d cheques>' % (
            os.path.basename(self.path), len(self.emps), sum(v['checks'] for v in self.emps.values()))

    def sum(self, field):
        return round(sum(v[field] for v in self.emps.values()), 2)

    def ded(self, code):
        return round(sum(v['ded'].get(code, 0) for v in self.emps.values()), 2)

    def codes(self):
        return {c for v in self.emps.values() for c in v['ded']}

    def pretax(self, v, exclude=()):
        """Pre-tax deductions on one employee, excluding retirement, TSA and whatever else."""
        return sum(a for c, a in v['ded'].items()
                   if self.flags.get(c, '').startswith('D')
                   and c not in ('1WH', '1WX', '1TR') and c not in self.tsa_codes and c not in exclude)


def norm_code(raw):
    s = re.sub(r'\s+', '', raw)
    m = re.match(r"^([A-Z'\-]+)(\d{3})$", s)
    return (m.group(1), m.group(2)) if m else (s, '')


def census_key(last, first):
    """A census row rendered in the register's own code form."""
    return (re.sub(r'\s+', '', str(last).strip().upper()[:5]) +
            re.sub(r'\s+', '', str(first).strip().upper()[:3]))


def to_text(pdf_path):
    """pdftotext -layout. Never a whitespace-collapsing extractor."""
    out = os.path.join(tempfile.mkdtemp(), 'reg.txt')
    r = subprocess.run(['pdftotext', '-layout', pdf_path, out], capture_output=True)
    if r.returncode or not os.path.exists(out):
        raise RegisterError('pdftotext failed on %s: %s' % (pdf_path, r.stderr.decode()[:300]))
    return open(out, errors='replace').read()


def _parse_text(text):
    emps, flags, tsa, cur = {}, {}, set(), None
    for ln in text.split('\n'):
        m = HDR.match(ln)
        if m:
            code, seq = norm_code(m.group(2))
            cur = emps.setdefault(code + seq, {
                'name': '%s, %s' % (m.group(3).strip(), m.group(4).strip()), 'code': code, 'seq': seq,
                'ded': collections.defaultdict(float), 'base': collections.defaultdict(float),
                'flag': {}, 'checks': 0, 'net': 0.0,
                'gross': 0.0, 'fed_grs': 0.0, 'med_grs': 0.0, 'fica_grs': 0.0, 'sta_grs': 0.0})
            mm = re.search(r'MAR:(\w?)\s+FED:\s*([\w/]*)', ln)
            if mm:
                cur['mar_state'], cur['mar_fed'] = mm.group(1), mm.group(2)
            continue
        mm = TSA_CODES_RE.match(ln)
        if mm and 'TSA' in text[:0] or (mm and re.search(r'\d+\.\d\d\s*$', ln)):
            tsa.add(mm.group(1))
        if cur is None:
            continue
        mm = W4.search(ln)
        if mm:
            cur['w4_2020'], cur['step3'] = mm.group(1), f(mm.group(2))
            cur['step4a'], cur['step4b'], cur['step2c'] = f(mm.group(3)), f(mm.group(4)), mm.group(5)
            continue
        mm = GRS.search(ln)
        if mm:
            cur['checks'] += 1
            for i, k in enumerate(('fed_grs', 'sta_grs', 'fica_grs', 'med_grs', 'net'), 1):
                cur[k] += f(mm.group(i))
            continue
        mm = TOT.match(ln)
        if mm:
            cur['gross'] += f(mm.group(1))
        parts = ln.split('|')
        if len(parts) >= 2:
            mm = DED.match(parts[1])
            if mm:
                cur['ded'][mm.group(1)] += f(mm.group(2))
                # CURR BASE: the wage THIS line was computed on. It is not the same as the
                # cheque's FED TX GRS. The payroll withholds per pay assignment, and an assignment
                # can carry no 1WH line at all, so ded['1WH'] is computed on base['1WH'] while
                # the excluded assignment's gross still lands in FED TX GRS. Reading this is the
                # only way to explain federal withholding on a multi-assignment cheque.
                if mm.group(3):
                    cur['base'][mm.group(1)] += f(mm.group(3))
                for tok in mm.group(6).split():
                    if FLAG.match(tok):
                        cur['flag'][mm.group(1)] = tok
                        flags[mm.group(1)] = tok
                        break
    return emps, flags, tsa


def _printed_totals(text):
    """Read the register's own PAYROLL TOTALS page. federal / state / medicare per row."""
    t = {}
    for k, pat in T_ROWS.items():
        m = re.search(pat, text)
        if m:
            t[k] = dict(federal=f(m.group(1)), state=f(m.group(2)), medicare=f(m.group(3)))
    m = T_EMPS.search(text)
    if m:
        t['employees'] = int(m.group(1))
    # the TSA block lists its own codes above the totals
    t['tsa_codes'] = set(re.findall(r'^\s*(4[0-9A-Z]{3})\s+\S', text, re.M))
    return t


def read(pdf_path, strict=True):
    """Parse and gate. Raises RegisterError unless the parse ties to the printed totals."""
    text = to_text(pdf_path)
    emps, flags, _ = _parse_text(text)
    pt = _printed_totals(text)
    if not pt.get('taxable'):
        raise RegisterError('%s: no PAYROLL TOTALS page found; is this a 4packr01 register?' % pdf_path)
    tsa = pt.get('tsa_codes', set())
    reg = Register(pdf_path, emps, pt, flags, tsa, text)

    def flagsum(letter):
        return round(sum(a for v in emps.values() for c, a in v['ded'].items()
                         if letter in flags.get(c, '') and c not in ('1WH', '1WX', '1TR') and c not in tsa), 2)

    checks = [
        ('employees',              len(emps),                       pt.get('employees')),
        ('total gross pay',        reg.sum('gross'),                pt['gross']['federal']),
        ('taxable gross federal',  reg.sum('fed_grs'),              pt['taxable']['federal']),
        ('taxable gross state',    reg.sum('sta_grs'),              pt['taxable']['state']),
        ('taxable gross medicare', reg.sum('med_grs'),              pt['taxable']['medicare']),
        ('tax sheltered retirement', reg.ded('1TR'),                pt.get('ret', {}).get('federal')),
        ('other bef tax, federal',  flagsum('D'),                   pt.get('oth', {}).get('federal')),
        ('other bef tax, state',    flagsum('S'),                   pt.get('oth', {}).get('state')),
        ('other bef tax, medicare', flagsum('F'),                   pt.get('oth', {}).get('medicare')),
    ]
    bad = [(n, got, want) for n, got, want in checks
           if want is not None and abs((got or 0) - want) > 0.01]
    reg.gate = checks
    if bad and strict:
        raise RegisterError('%s does not tie to its own totals page:\n%s' % (
            os.path.basename(pdf_path),
            '\n'.join('   %-26s parsed %14.2f  printed %14.2f  diff %.2f' % (n, g, w, g - w)
                      for n, g, w in bad)))
    reg.gate_ok = not bad
    return reg


def programme_codes(prev, mock):
    """Work out this client's premium, fee and reimbursement codes by comparing the two runs.

    The premium is the new pre-tax code with the largest total; the reimbursement is the new
    code whose total is its negative; the fee is the remaining new code. Nothing is hardcoded.
    """
    new = sorted(mock.codes() - prev.codes())
    tot = {c: mock.ded(c) for c in new}
    pos = {c: v for c, v in tot.items() if v > 0}
    if not pos:
        raise RegisterError('no new deduction code in the mock run; is this the right pair of files?')
    premium = max(pos, key=lambda c: pos[c])
    reimb = next((c for c, v in tot.items() if abs(v + tot[premium]) < 0.01 and c != premium), None)
    fee = next((c for c in pos if c not in (premium, reimb)), None)
    n = sum(1 for v in mock.emps.values() if v['ded'].get(premium))
    return dict(premium=premium, fee=fee, reimbursement=reimb, participants=n,
                premium_total=tot[premium], fee_total=tot.get(fee, 0.0),
                premium_each=round(tot[premium] / n, 2) if n else 0.0,
                fee_each=round(tot.get(fee, 0.0) / n, 2) if n else 0.0,
                premium_flag=mock.flags.get(premium, ''),
                fica_exempt=('F' in mock.flags.get(premium, '')))


if __name__ == '__main__':
    import sys
    for p in sys.argv[1:]:
        r = read(p, strict=False)
        print(r)
        for n, got, want in r.gate:
            print('   %-26s parsed %14.2f  printed %14s  %s'
                  % (n, got or 0, '%.2f' % want if want is not None else 'n/a',
                     'ok' if want is None or abs((got or 0) - want) <= 0.01 else 'MISMATCH'))
        print('   flags:', dict(collections.Counter(r.flags.values())))
