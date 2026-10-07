"""Multi-employee payroll REGISTERS, as opposed to individual payslips.

The reader in parse_files.py expects one employee per page: a payslip. A payroll register puts
three or four employees on a landscape page and may be a scan with no text layer, rotated. Run
one through the payslip reader and it returns nothing at all, which is what happened on The
Breathing Association: thirty-two employees, none matched, no tax figures.

This module detects a register and reads it properly, emitting the same flat records
parse_files.to_paycheck already consumes, so nothing downstream changes.

Two families are handled:
  * a TEXT register, e.g. the Texas ESC 4packr01 used by ISD clients
  * a SCANNED register, e.g. Paylocity, where the page must be rendered, oriented and recognised

Both gate themselves. The text one ties to the totals the register prints on its own last page;
the scanned one is checked against the roster. Neither returns a partial population silently.
"""
import os, re, sys, tempfile, subprocess

# The readers are VENDORED at services/readers so the deployed image carries them. The old
# behaviour imported them from a laptop path that the Docker image never had, so every register
# fell back to the payslip reader and returned nothing. Sync with tools/sync_readers.py.
VENDORED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'readers')
TOOLS = os.environ.get('ATTENTIVE_TOOLS', VENDORED)
for _p in (TOOLS, VENDORED):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

SS_RATE, MED_RATE = 0.062, 0.0145
# Titles seen in the field. The Texas ESC report calls itself "Check Register for Payroll Run"
# on the before run and "Check Verification Register" on the mock, so match the family rather
# than one phrase, and fall back to the report id printed in the corner.
REGISTER_TITLE = re.compile(
    r'payroll\s+register'
    r'|check\s+(verification\s+)?register'
    r'|register\s+for\s+payroll\s+run'
    r'|pre\s*process\s+payroll'
    r'|payroll\s+earnings\s+register'
    r'|4packr01'
    r'|hrs2200', re.I)
# TxEIS / Ascender. Its own banner names the program, which is the safest marker: the words
# "Payroll Earnings Register" alone would also match other vendors.
TXEIS_TITLE = re.compile(r'Program:\s*HRS2200|Pre-Post Payroll Earnings Register'
                         r'|Payroll Earnings Register', re.I)


def _first_page_text(data):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'in.pdf')
    open(p, 'wb').write(data)
    out = os.path.join(d, 'out.txt')
    try:
        subprocess.run(['pdftotext', '-layout', '-f', '1', '-l', '2', p, out],
                       capture_output=True, timeout=60)
        return (open(out, errors='replace').read() if os.path.exists(out) else ''), p
    except Exception:
        return '', p


def looks_like_register(data):
    """A register, a payslip pack, or something else? Decided from the document, not the filename."""
    txt, path = _first_page_text(data)
    if txt.strip():
        if TXEIS_TITLE.search(txt):
            return 'txeis-register', path
        if REGISTER_TITLE.search(txt):
            return 'text-register', path
        return 'payslips', path
    # no text layer: recognise the first page and look for the same title
    try:
        from scanned_register import _render, _ocr
        d = os.path.dirname(path)
        pngs = _render(path, d, dpi=150)
        if not pngs:
            return 'payslips', path
        from PIL import Image
        for ang in (0, -90, 90):
            probe = os.path.join(d, 'probe%d.png' % ang)
            im = Image.open(pngs[0])
            (im if ang == 0 else im.rotate(ang, expand=True)).save(probe)
            words = ' '.join(t for t, c, b in _ocr(probe))
            if REGISTER_TITLE.search(words):
                return 'scanned-register', path
    except Exception:
        pass
    return 'payslips', path


def _rec(name, src, gross=None, fed=None, state=None, local=None, local_code='',
         ss=None, med=None, net=None, taxable=None, medgross=None,
         premium=None, fee=None, reimb=None, retirement=None, empid=None, supplemental=None):
    last, first = ('', '')
    if name and ',' in name:
        last, first = [x.strip() for x in name.split(',', 1)]
    elif name:
        last = name.strip()
    # A register prints the full legal name, "ALBRIGHT, JAMES L" or "BELL, MELANIE JANE", while
    # a census carries "James" and "Melanie". Keying on the whole first-name field never matches,
    # and the fallback then matches on surname alone, which collapses the two Albrights and the
    # two Potts into one another. Key on the first forename and keep the printed name for display.
    first = first.split()[0] if first.split() else first
    return {k: v for k, v in dict(
        # Figures from a register that tied to its own printed totals page are already correct.
        # The payslip repair path (anchor to the census gross, then solve the identity) exists
        # for OCR'd statements and must not touch these: anchoring overwrites the register's real
        # gross with a census-derived one, which erases the very pay change the audit needs to see.
        gated=True,
        name=name, employee_last_name=last, employee_first_name=first, employee_id=empid,
        gross=gross, federal=fed, state=state, local=local, local_code=local_code,
        social_security=ss, medicare=med, net_pay=net, taxable_wages=taxable,
        medicare_gross=medgross, premium=premium, fee=fee, reimbursement=reimb,
        retirement=retirement, supplemental=supplemental, source=src).items() if v is not None}


def read_text_register(path, label):
    """Texas ESC and anything else pdftotext -layout can carry."""
    from register import read, programme_codes
    reg = read(path)
    out = []
    for code, v in reg.emps.items():
        d = v['ded']
        out.append(_rec(v['name'], '%s %s' % (label, os.path.basename(path)),
                        gross=v['gross'] or None,
                        fed=(d.get('1WH', 0) + d.get('1WX', 0)) or None,
                        ss=None, med=d.get('1MC') or None,
                        net=v['net'] or None, taxable=v['fed_grs'] or None,
                        medgross=v['med_grs'] or None,
                        retirement=d.get('1TR') or None, empid=v.get('code')))
    return out, dict(kind='text-register', employees=len(out),
                     period=__import__('services.parse_files', fromlist=['x']).period_of(reg.text[:200000]),
                     gate=[dict(control=c, parsed=g, printed=w) for c, g, w in reg.gate])


def _txeis_programme_codes(reg):
    """Find the premium, reimbursement and fee codes across the WHOLE register.

    Not from one cheque. On a single cheque any ordinary pre-tax benefit also carries Caf-125 Y,
    so picking the first one found returns a cancer policy instead of the premium. Two signatures
    identify the programme, and both need the whole population:

      premium and reimbursement are the SAME amount, the premium flagged Caf-125 Y and the
      reimbursement flagged Ref Y. At Aspermont that is 161 and 162, both 1,173.00.

      the fee is deducted from exactly the same employees as the premium and is flagged neither
      pre-tax nor refund. At Aspermont that is 163 at 114.00.
    """
    who, amounts, flags = {}, {}, {}
    for v in reg.emps.values():
        for c, d in (v.get('ded') or {}).items():
            if d.get('emple'):
                who.setdefault(c, set()).add(v['name'])
                amounts.setdefault(c, set()).add(round(d['emple'], 2))
                flags[c] = (d.get('caf125'), d.get('ref'))
    prem = reimb = fee = None
    for c, (caf, ref) in flags.items():
        if ref != 'Y' or len(amounts[c]) != 1:
            continue
        amt = next(iter(amounts[c]))
        for c2, (caf2, ref2) in flags.items():
            if c2 != c and caf2 == 'Y' and amounts[c2] == {amt} and who[c2] == who[c]:
                prem, reimb = c2, c
                break
        if prem:
            break
    if prem:
        cand = [c for c, (caf, ref) in flags.items()
                if c not in (prem, reimb) and caf != 'Y' and ref != 'Y'
                and who[c] == who[prem] and len(amounts[c]) == 1]
        if cand:
            fee = min(cand, key=lambda c: next(iter(amounts[c])))
    return prem, reimb, fee


def read_txeis_register(path, label):
    """TxEIS / Ascender, program HRS2200. Seen at Aspermont ISD.

    Two things this family gives that the others do not, and both matter:
      * `Cafe 125` is the pre-tax total, and the per-employee deduction table carries a Caf-125
        flag per code, which is this family's equivalent of the 4packr01 pre-tax flag.
      * `Withld Grs` is the federal taxable wage and `Med Grs` the Medicare one, printed
        separately, so no flag interpretation is needed to tell whether the premium came out of
        each base.
    """
    import txeis_register as X
    reg = X.read(path)
    pc, rc, fc = _txeis_programme_codes(reg)
    out = []
    for code, v in reg.emps.items():
        ded = v.get('ded', {})
        g = lambda c: (ded.get(c, {}).get('emple') if c else None) or None
        out.append(_rec(v['name'], '%s %s' % (label, os.path.basename(path)),
                        gross=v.get('gross') or None,
                        fed=v.get('wh_tax') or None,
                        ss=v.get('fica_tax') or None,
                        med=v.get('med_tax') or None,
                        net=v.get('net') or None,
                        taxable=v.get('wh_gross') or None,
                        medgross=v.get('med_gross') or None,
                        premium=g(pc), fee=g(fc), reimb=g(rc),
                        retirement=v.get('trs_dep') or None,
                        # Stipends, extra duty and the like. A census carries the contract
                        # salary only, so this is the usual reason a payslip gross exceeds the
                        # figure the proposal was built from.
                        supplemental=v.get('suppl') or None,
                        empid=code))
    period = reg.period or {}
    rep = dict(kind='txeis-register', employees=len(out),
               layout='HRS2200 %s' % (reg.district or {}).get('name', ''),
               codes=dict(premium=pc, reimbursement=rc, fee=fc),
               period=({'period': (period.get('from'), period.get('thru')),
                        'check_date': period.get('pay_date')} if period.get('from') else {}),
               gate=[dict(control=c, parsed=p, printed=w) for c, p, w in reg.gate])
    return out, rep


def read_scanned_register(path, label, roster=None):
    """Paylocity and similar: rendered, oriented, recognised, and gated on the roster."""
    import scanned_register as S
    # The layout must come off the page, not off a text layer a scan does not have.
    lay = S.detect_layout(path)
    emps, rep = S.read_scanned(path, lay, expect_roster=roster)
    out = []
    for e in emps:
        t = e['tax']
        g = lambda k, w='amount': (t.get(k) or {}).get(w)
        base = e.get('fica')
        out.append(_rec(e.get('name'), '%s %s %s' % (label, os.path.basename(path), e.get('page', '')),
                        gross=e.get('gross'), fed=g('FITW'), state=g('OH'),
                        local=g('LOCAL'), local_code='city income tax',
                        # Social Security and Medicare are exact percentages of the same base, so
                        # derive them from the base rather than trust a recognised amount
                        ss=round(base * SS_RATE, 2) if base else g('SS'),
                        med=round(base * MED_RATE, 2) if base else g('MED'),
                        net=e.get('net'), taxable=g('FITW', 'taxable'), medgross=base,
                        premium=e['ded'].get('PCMPR'), fee=e['ded'].get('PCMPA'),
                        reimb=e['ded'].get('SIMRP'), empid=e.get('empid')))
    rep['kind'] = 'scanned-register'
    rep['layout'] = lay.name
    rep['period'] = _period_from_pages(path)
    return out, rep


def _period_from_pages(path):
    """The pay period a register prints on every page. A before-and-after only means anything
    when both runs cover the SAME period, so it has to be read, not assumed."""
    try:
        import scanned_register as S
        from PIL import Image
        import glob, json as _json
        d = S._cache(path, None)
        pngs = sorted(glob.glob(os.path.join(d, 'p-*.png')))
        ang = _json.load(open(os.path.join(d, 'rotation.json')))
        probe = os.path.join(d, 'periodprobe.png')
        im = Image.open(pngs[0])
        (im if ang == 0 else im.rotate(ang, expand=True)).save(probe)
        words = ' '.join(t for t, c, b in S._ocr(probe))
        from . import parse_files as P
        return P.period_of(words)
    except Exception:
        return {}


def read(data, label, roster=None):
    """Returns (records, report) or (None, None) when this is a payslip pack after all."""
    kind, path = looks_like_register(data)
    if kind == 'payslips':
        return None, None
    if kind == 'text-register':
        return read_text_register(path, label)
    if kind == 'txeis-register':
        return read_txeis_register(path, label)
    return read_scanned_register(path, label, roster=roster)
