"""Register route: census + previous register + mock register -> the Proposal versus Payroll pack.

The four-upload route in app.py reads scanned payslips. This one takes a Texas ESC payroll
register (report 4packr01), which is a text PDF, and runs the deterministic comparison that
answers the FICA question: what each of the four taxes actually did, which Social Security and
Medicare setting matches this payroll, and what is left over.

The analysis lives in /Users/nico/attentive/tools so the CLI and this route stay one
implementation. Set ATTENTIVE_TOOLS to point elsewhere.
"""
import os, sys, tempfile

TOOLS = os.environ.get('ATTENTIVE_TOOLS', '/Users/nico/attentive/tools')
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

AVAILABLE = True
IMPORT_ERROR = ''
try:
    import proposal_vs_payroll as pvp
    from register import RegisterError
except Exception as e:                                    # the route degrades, the app does not
    AVAILABLE = False
    IMPORT_ERROR = '%s: %s' % (type(e).__name__, e)
    RegisterError = RuntimeError


def run(census_bytes, census_name, prev_bytes, mock_bytes, client,
        fee_employee, fee_employer, premium=None, state=None):
    """Returns (docx_bytes, xlsx_bytes, summary_dict). Raises RegisterError if a parse does not
    tie to the register's own totals page."""
    if not AVAILABLE:
        raise RuntimeError('register analysis unavailable (%s)' % IMPORT_ERROR)
    d = tempfile.mkdtemp()
    cp = os.path.join(d, census_name or 'census.xlsx')
    pp, mp = os.path.join(d, 'previous.pdf'), os.path.join(d, 'mock.pdf')
    for path, blob in ((cp, census_bytes), (pp, prev_bytes), (mp, mock_bytes)):
        with open(path, 'wb') as fh:
            fh.write(blob)

    a = pvp.analyse(cp, pp, mp, float(fee_employee), float(fee_employer),
                    float(premium) if premium else None, state or None)
    out = os.path.join(d, 'out')
    docx_path = pvp.report(a, client or 'Client', out)
    xlsx_path = pvp.workbook(a, client or 'Client', out)

    prog, pay, best = a['prog'], a['pay'], a['best']
    bt = a['runtot'][best]
    summary = dict(
        client=client, employees=len(a['census']), comparable=len(a['rows']),
        set_aside=len(a['excluded']), participants=prog['participants'],
        premium=a['premium'], premium_code=prog['premium'], premium_flag=prog['premium_flag'],
        fica_exempt=prog['fica_exempt'], fee_code=prog['fee'],
        payroll=dict(federal=round(pay['fed'], 2), state=round(pay['state_sav'], 2),
                     social_security=round(pay['ss'], 2), medicare=round(pay['med'], 2),
                     fee=round(-pay['fee'], 2), net=round(pay['act'], 2)),
        settings=[dict(setting=lab, federal=round(a['runtot'][lab]['fed'], 2),
                       social_security=round(a['runtot'][lab]['ss'], 2),
                       medicare=round(a['runtot'][lab]['med'], 2),
                       allotment=round(a['runtot'][lab]['net'], 2),
                       gap=round(a['runtot'][lab]['net'] - pay['act'], 2),
                       exact=a['runtot'][lab]['exact'],
                       matches_payroll=(lab == best), as_issued=(lab == a['reproduces']))
                  for lab, _, _ in pvp.COMBOS],
        best_setting=best, as_issued=a['reproduces'],
        residual=round(sum(x[2] for x in a['resid']), 2),
        residual_employees=len(a['resid']),
        causes=[dict(cause=k, employees=a['cause_n'][k], amount=round(v, 2))
                for k, v in a['cause'].most_common()],
        flags={c: f for c, f in sorted(a['mock'].flags.items())},
        gate=[dict(register=nm, control=ctl, parsed=round(got or 0, 2),
                   printed=(round(want, 2) if want is not None else None),
                   ok=(want is None or abs((got or 0) - want) <= 0.01))
              for nm, reg in (('previous', a['prev']), ('mock', a['mock'])) for ctl, got, want in reg.gate],
    )
    return open(docx_path, 'rb').read(), open(xlsx_path, 'rb').read(), summary
