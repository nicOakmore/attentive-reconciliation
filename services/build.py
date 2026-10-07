"""Assemble the audit: census + proposal + payroll before + payroll after, matched by employee, then reconciled."""
from . import parse_files as P
from .audit import Census, Engine, EmployeeAudit, audit_employee, summarise, r2

FREQ = {'52': 52, '26': 26, '24': 24, '12': 12, 'weekly': 52, 'biweekly': 26, 'bi-weekly': 26,
        'semimonthly': 24, 'semi-monthly': 24, 'monthly': 12}


def pay_periods(v):
    if v in (None, ''):
        return 12
    s = str(v).strip().lower()
    n = P.num(s)
    if n and n in (12, 24, 26, 52):
        return int(n)
    return FREQ.get(s, 12)


def run(census_bytes=None, report_bytes=None, before=None, after=None, before_name='', after_name='', progress=None):
    """before/after are (bytes, filename). Returns (audits, summary, notes)."""
    notes = []
    census_recs, report_recs = [], []
    if census_bytes:
        _, census_recs, t = P.read_table(census_bytes, P.CENSUS_WANTED, sheet_hint='census')
        notes.append(f'Census: {len(census_recs)} rows from sheet "{t}"')
    if report_bytes:
        _, report_recs, t = P.read_table(report_bytes, P.REPORT_WANTED, sheet_hint='savings')
        notes.append(f'Proposal report: {len(report_recs)} rows from sheet "{t}"')

    store_stats = {}
    periods = {}

    def payroll(blob, label):
        if not blob:
            return []
        data, fname = blob
        if fname.lower().endswith(('.xlsx', '.xlsm', '.xls')):
            recs = P.paychecks_from_sheet(data)
            notes.append(f'{label}: {len(recs)} rows from spreadsheet {fname}')
        else:
            # A multi-employee payroll register is not a payslip pack and the payslip reader
            # returns nothing from one. Detect it from the document and read it properly.
            recs = None
            try:
                from . import register_reader as RR
                # The roster is the real employees only. A savings report carries Total and
                # Monthly Average rows with no name, and feeding those to the completeness gate
                # makes it fail on rows that were never employees.
                roster, seen = [], set()
                for r in (census_recs or []) + (report_recs or []):
                    ln = str(r.get('last_name') or r.get('employee_last_name') or '').strip()
                    if not ln or ln.lower() in ('total', 'monthly average', 'totals') or ln in seen:
                        continue
                    seen.add(ln)
                    roster.append((ln, None))
                recs, rep = RR.read(data, label, roster=roster or None)
                if recs is not None and rep.get('period'):
                    periods[label] = rep['period']
                if recs is not None:
                    notes.append('%s: read as a %s, %d employees, %s'
                                 % (label, rep.get('kind'), len(recs), rep.get('layout', '')))
            except Exception as e:
                notes.append('%s: register reader unavailable or failed (%s); fell back to the '
                             'payslip reader' % (label, str(e)[:160]))
                recs = None
            if recs is None:
                recs = P.paychecks_from_pdf(data, hint=label, progress=progress, source_name=fname,
                                            store_stats=store_stats)
            # Only look for a period in the raw text when the register reader did not already
            # give us one. It does, and P.period_of returns {} on register records because they
            # carry no raw_text, so running it unconditionally threw the real period away and
            # the pay-period check never fired.
            if not periods.get(label):
                try:
                    txt = ' '.join(str(getattr(r, 'raw_text', '') or '') for r in recs)[:200000]
                    periods[label] = P.period_of(txt)
                except Exception:
                    pass
            failed = sum(1 for r in recs if not (r.get('name') or r.get('employee_id')))
            if failed:
                notes.append(f'{label}: {failed} pages could not be read')
            modelled = sum(1 for r in recs if 'model' in (r.get('source') or ''))
            notes.append(f'{label}: {len(recs)} statements read from {fname}'
                         + (f', of which {modelled} needed the model to locate a line' if modelled else ''))
        return recs

    before_recs, after_recs = payroll(before, 'Payroll before'), payroll(after, 'Payroll after')
    b_id, b_name, b_last = P.fuzzy_index(before_recs)
    a_id, a_name, a_last = P.fuzzy_index(after_recs)

    cmap = {}
    for r in census_recs:
        k = P.name_key(r.get('employee_first_name'), r.get('employee_last_name'))
        if k.strip():
            cmap[k] = r

    audits = []
    rows = report_recs or census_recs
    for r in rows:
        first = r.get('first_name') or r.get('employee_first_name')
        last = r.get('last_name') or r.get('employee_last_name')
        key = P.name_key(first, last)
        if not key.strip():
            continue
        eid = str(r.get('client_employee_id') or r.get('employee_id') or '').strip()
        cen_row = cmap.get(key, {})
        c = Census(gross_annual=P.num(cen_row.get('gross_annual_taxable_wages') or r.get('annual_salary')),
                   pay_periods=pay_periods(cen_row.get('pay_frequency')),
                   pretax_other=P.num(cen_row.get('other_monthly_pretax')),
                   group_health=P.num(cen_row.get('group_health_monthly')),
                   retirement_401k=P.num(cen_row.get('retirement_401k_monthly')),
                   filing_status=str(cen_row.get('federal_w4_marital_status') or '').strip(),
                   w4_year=int(P.num(cen_row.get('w4_year')) or 0) or None,
                   dependents=P.num(cen_row.get('dependents')), step2c=str(cen_row.get('step2c') or '').strip(),
                   step3=P.num(cen_row.get('step3')), additional_federal=P.num(cen_row.get('additional_federal')),
                   additional_state=P.num(cen_row.get('additional_state')), state=str(cen_row.get('state') or '').strip(),
                   found=bool(cen_row),
                   socialsec=str(cen_row.get('census_socialsec') or '').strip().upper()[:1],
                   medicare=str(cen_row.get('census_medicare') or '').strip().upper()[:1])
        e = Engine(federal_before=P.num(r.get('federal_tax_before_premium')), federal_savings=P.num(r.get('federal_savings')),
                   state_savings=P.num(r.get('state_savings')), ss_savings=P.num(r.get('social_security_savings')),
                   medicare_savings=P.num(r.get('medicare_savings')), gross_savings=P.num(r.get('ee_gross_monthly_savings')),
                   fee=P.num(r.get('ee_monthly_fee')), allotment=P.num(r.get('employee_monthly_allotment')),
                   taxable_income_before=P.num(r.get('taxable_income_before')),
                   taxable_income_after=P.num(r.get('taxable_income_after')), premium=P.num(r.get('wellness_program')))
        brec = P.find(eid, first, last, b_id, b_name, b_last)
        arec = P.find(eid, first, last, a_id, a_name, a_last)
        anchor = (c.gross_annual / (c.pay_periods or 12)) if c.gross_annual else None
        for rec in (brec, arec):
            if rec is not None and rec.get('gated'):
                rec['_repaired'] = True      # read from a register that tied to its own totals
            if rec is not None and not rec.get('_repaired'):
                P.anchor_and_solve(rec, anchor)      # gross anchor first, so validation uses the right gross
                P.validate(rec)
                P.anchor_and_solve(rec, anchor)      # then solve the identity for anything validation removed
                rec['_repaired'] = True
        emp = EmployeeAudit(name=f"{str(first).strip()} {str(last).strip()}".strip(), employee_id=eid, census=c, engine=e,
                            before=P.to_paycheck(brec), after=P.to_paycheck(arec),
                            pay_periods=c.pay_periods or 12)
        audits.append(audit_employee(emp))

    used = set()
    for a in audits:
        if a.before.source: used.add(a.before.source)
        if a.after.source: used.add(a.after.source)
    orphan_b = [r for r in before_recs if r.get('source') not in used]
    orphan_a = [r for r in after_recs if r.get('source') not in used]
    if orphan_b or orphan_a:
        notes.append(f'{len(orphan_b)} before and {len(orphan_a)} after statements were read but matched no employee '
                     f'in the proposal report or census, and were excluded from the reconciliation')
    matched_both = sum(1 for a in audits if a.before.net_pay is not None and a.after.net_pay is not None)
    notes.append(f'{matched_both} of {len(audits)} employees were matched to both a before and an after statement')
    if store_stats:
        parts = []
        if store_stats.get('read'): parts.append(f"{store_stats['read']} read from the page")
        if store_stats.get('exact'): parts.append(f"{store_stats['exact']} served from the page store")
        if store_stats.get('equivalent'):
            parts.append(f"{store_stats['equivalent']} served from the store on an equivalent page match")
        if parts:
            notes.append('Statement pages: ' + ', '.join(parts))
    corrected = [a for a in audits if 'corrected by' in ((a.before.source or '') + (a.after.source or ''))]
    if corrected:
        notes.append(f'{len(corrected)} employees carry a recorded human correction to a figure on their statement')
    summary = summarise(audits)
    summary['population']['unmatched_statements'] = len(orphan_b) + len(orphan_a)

    # A before-and-after is only meaningful when both runs cover the SAME pay period. Where they
    # do not, every difference carries ordinary payroll movement as well as the premium, and the
    # reconciliation cannot be read as a programme result. Say so at the top rather than let it
    # pass.
    pb, pa = periods.get('Payroll before', {}), periods.get('Payroll after', {})
    summary['periods'] = dict(before=pb, after=pa)
    if pb.get('period') and pa.get('period') and pb['period'] != pa['period']:
        summary['period_mismatch'] = dict(
            before='%s to %s' % pb['period'], after='%s to %s' % pa['period'],
            check_before=pb.get('check_date'), check_after=pa.get('check_date'))
        notes.append('PAY PERIODS DO NOT MATCH: the before register covers %s to %s and the after '
                     'register covers %s to %s. Differences between them include ordinary payroll '
                     'movement as well as the premium.' % (pb['period'] + pa['period']))

    # Municipal income tax the proposal never counted.
    loc = [a for a in audits if (a.local_gap or 0) > 0.02]
    if loc:
        tot = round(sum(a.local_gap for a in loc), 2)
        summary['local_tax'] = dict(employees=len(loc), amount=tot)
        notes.append('%d employees had city or local income tax reduced by the premium, worth %.2f '
                     'a month, which the proposal does not count.' % (len(loc), tot))
    return audits, summary, notes
