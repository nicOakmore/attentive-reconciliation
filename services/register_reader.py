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

TOOLS = os.environ.get('ATTENTIVE_TOOLS', '/Users/nico/attentive/tools')
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

SS_RATE, MED_RATE = 0.062, 0.0145
# Titles seen in the field. The Texas ESC report calls itself "Check Register for Payroll Run"
# on the before run and "Check Verification Register" on the mock, so match the family rather
# than one phrase, and fall back to the report id printed in the corner.
REGISTER_TITLE = re.compile(
    r'payroll\s+register'
    r'|check\s+(verification\s+)?register'
    r'|register\s+for\s+payroll\s+run'
    r'|pre\s*process\s+payroll'
    r'|4packr01', re.I)


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
         premium=None, fee=None, reimb=None, retirement=None, empid=None):
    last, first = ('', '')
    if name and ',' in name:
        last, first = [x.strip() for x in name.split(',', 1)]
    elif name:
        last = name.strip()
    return {k: v for k, v in dict(
        name=name, employee_last_name=last, employee_first_name=first, employee_id=empid,
        gross=gross, federal=fed, state=state, local=local, local_code=local_code,
        social_security=ss, medicare=med, net_pay=net, taxable_wages=taxable,
        medicare_gross=medgross, premium=premium, fee=fee, reimbursement=reimb,
        retirement=retirement, source=src).items() if v is not None}


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
    return read_scanned_register(path, label, roster=roster)
