"""Pay-assignment detail inside a 4packr01 register cheque.

register.py reads the cheque: one gross, one FED TX GRS, one 1WH. That is enough for most
work and it is what the payroll totals page ties to. It is NOT enough to explain federal
withholding, because the payroll does not withhold on the cheque. It withholds on each PAY
ASSIGNMENT separately, and prints a 1WH line under each one with its own taxable base.

Three consequences, all of them things we got wrong before reading this detail:

1. An assignment can carry NO 1WH line at all. Its gross still lands in FED TX GRS, so the
   cheque looks as though the whole taxable gross was withheld on when it was not. Bowker is
   the clean example: FED TX GRS 6,725.70, but the sum of the 1WH bases is 5,975.70. The
   750.00 difference is an XDCCR assignment with no federal line. Annualise the withheld
   base, 5,975.70 x 12 = 71,708.40, and the published table gives the district's 578.82
   exactly. Annualise the full taxable gross and you get our 743.82.

2. Because the withheld base sets the annualised wage, it sets the BRACKET, and therefore the
   marginal rate the premium saving is worth. That is why these employees miss by tens of
   dollars while employees with a single assignment land to the cent.

3. The per-assignment rate is uniform across a cheque (Bowker: 9.685 to 9.690 per cent on all
   nine withheld lines). The payroll computes one effective rate from the annualised withheld base
   and prorates it. So the exclusion is the whole story; there is no second mechanism.

The census has one gross column, so the tool cannot see any of this. The fix is a census
column for the portion of pay that carries no federal withholding, or the district adding the
federal line to those assignments.
"""
import re
import collections

import register as R

NUM = r'-?[\d,]+\.\d{2}'
# The assignment line: ** <type> <paycode> <description> <rate> <factor> <gross> <accounts>
ASG = re.compile(r'^\s*\*\*\s+\w\s+(\S+)\s+.*?\s(?:' + NUM + r'|[\d,]+\.\d{4})'
                 r'\s+([\d.]+)\s+(' + NUM + r')\s')
# FRQ and PAYS, printed just before the two posting dates at the end of the assignment line.
# PAYS is the number of pay periods the assignment is spread over, and it is the figure
# the payroll annualises the taxable base with. It is NOT always 12 on a monthly payroll: Mineola
# runs a mix of 12 and 13, and a 13 raises the annualised wage by a thirteenth, which can push
# the employee into a higher bracket and change what the premium saving is worth.
PAYS = re.compile(r'\s(\d{1,2})\s+(\d{1,2})\s+\d\d/\d\d/\d{4}')
# A tax or deduction line under an assignment. The trailing figure is that line's own base.
TAX = re.compile(r'^\s+[DF]\s+(\w+)\s+\S+.*?\s(' + NUM + r')\s+\d.*?\s(' + NUM + r')\s*$')
# Segment on exactly the same header register.py does. Its own pattern is deliberately
# tolerant of the collapsed whitespace and short employee codes that a strict
# [A-Z]{8}\d{3} misses; with a strict one, 33 of the 350 headers go unmatched and every
# assignment under them is silently attributed to the previous employee, which inflates that
# employee's withheld base by a whole second cheque.
HDR = R.HDR


def _f(s):
    return float(s.replace(',', ''))


def read(pdf_path, text=None):
    """{employee code: {'name', 'asg': [{'pay','gross','fed','fed_base','n_fed'}, ...]}}

    Keyed on the eight-character census-style code, matching register.read.
    """
    t = text if text is not None else R.to_text(pdf_path)
    emps = collections.OrderedDict()
    cur = None
    for ln in t.split('\n'):
        m = HDR.match(ln)
        if m:
            code, seq = R.norm_code(m.group(2))
            cur = emps.setdefault(code + seq, dict(
                code=code, name='%s, %s' % (m.group(3).strip(), m.group(4).strip()), asg=[]))
            continue
        if cur is None:
            continue
        a = ASG.match(ln)
        if a:
            p = PAYS.search(ln)
            cur['asg'].append(dict(pay=a.group(1), gross=_f(a.group(3)),
                                   frq=int(p.group(1)) if p else None,
                                   pays=int(p.group(2)) if p else None,
                                   fed=0.0, fed_base=0.0, n_fed=0))
            continue
        x = TAX.match(ln)
        if x and cur['asg'] and x.group(1) == '1WH':
            cur['asg'][-1]['fed'] += _f(x.group(2))
            cur['asg'][-1]['fed_base'] += _f(x.group(3))
            cur['asg'][-1]['n_fed'] += 1
    return emps


def withheld_base(rec):
    """The part of the cheque that federal withholding was actually computed on."""
    return round(sum(a['fed_base'] for a in rec['asg']), 2)


def excluded(rec):
    """Assignments that paid gross but carry no 1WH line: [(paycode, gross), ...]."""
    return [(a['pay'], a['gross']) for a in rec['asg'] if a['gross'] and not a['n_fed']]


def pays(rec):
    """The pay-period count the payroll annualised with, weighted by withheld base.

    Nearly every cheque is uniform, because the whole point is that the payroll derives one
    effective rate per cheque. Weighting rather than taking the first value means a cheque
    that genuinely mixes 12 and 13 still gets a single defensible figure.
    """
    num = den = 0.0
    for a in rec['asg']:
        if a['pays'] and a['fed_base']:
            num += a['pays'] * a['fed_base']
            den += a['fed_base']
    if den:
        return num / den
    seen = [a['pays'] for a in rec['asg'] if a['pays']]
    return float(seen[0]) if seen else 12.0


def effective_rate(rec):
    b = withheld_base(rec)
    return (sum(a['fed'] for a in rec['asg']) / b) if b else 0.0


def check(rec, printed_1wh, printed_fed_grs):
    """Self-gate: the assignment lines must add back to what the cheque prints."""
    fed = round(sum(a['fed'] for a in rec['asg']), 2)
    gross = round(sum(a['gross'] for a in rec['asg']), 2)
    return dict(fed_ok=abs(fed - printed_1wh) < 0.02, fed=fed,
                gross=gross, base=withheld_base(rec),
                gap=round(printed_fed_grs - withheld_base(rec), 2))
