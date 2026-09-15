"""Attentive payroll reconciliation. Four uploads in, one reconciliation per employee out, docx download.

The arithmetic is deterministic (services/audit.py). Groq reads payroll statements that have no text layer and maps
unfamiliar spreadsheet headers, and writes the one summary paragraph. It never produces a number or a cause.
"""
import os, io, json, uuid, time, traceback
from flask import Flask, request, jsonify, send_file, render_template, abort

from services import build as builder
from services import report as reporter
from services import groq_client

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['MAX_CONTENT_LENGTH'] = 80 * 1024 * 1024
JOBS = {}
SAMPLE_DIR = os.environ.get('SAMPLE_DIR', '')


def _file(field):
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    return (f.read(), f.filename)


@app.get('/')
def index():
    ok, model = groq_client.health()
    return render_template('index.html', groq_ok=ok, groq_model=model if ok else '',
                           samples=_samples())


def _samples():
    if not SAMPLE_DIR or not os.path.isdir(SAMPLE_DIR):
        return []
    out = []
    for name in sorted(os.listdir(SAMPLE_DIR)):
        d = os.path.join(SAMPLE_DIR, name)
        if os.path.isdir(d):
            files = {k: None for k in ('census', 'report', 'before', 'after')}
            for f in os.listdir(d):
                l = f.lower()
                if 'census' in l or 'test' in l and l.endswith('.xlsx'):
                    files['census'] = files['census'] or f
                if 'test' in l and l.endswith('.xlsx'):
                    files['report'] = files['report'] or f
                if 'prev' in l or 'before' in l or 'previous' in l:
                    files['before'] = f
                if 'mock' in l or 'after' in l:
                    files['after'] = f
            out.append(dict(name=name, files=files))
    return out


@app.post('/audit')
def audit():
    client = (request.form.get('client') or '').strip()
    period = (request.form.get('period') or '').strip()
    sample = (request.form.get('sample') or '').strip()
    try:
        if sample:
            d = os.path.join(SAMPLE_DIR, sample)
            pick = lambda pred: next((os.path.join(d, f) for f in sorted(os.listdir(d)) if pred(f.lower())), None)
            census_p = pick(lambda l: l.endswith('.xlsx'))
            report_p = census_p
            before_p = pick(lambda l: ('prev' in l or 'previous' in l or 'before' in l) and l.endswith('.pdf'))
            after_p = pick(lambda l: ('mock' in l or 'after' in l) and l.endswith('.pdf'))
            rd = lambda p: (open(p, 'rb').read(), os.path.basename(p)) if p else None
            census, rep, before, after = rd(census_p), rd(report_p), rd(before_p), rd(after_p)
            client = client or sample
        else:
            census, rep = _file('census'), _file('proposal')
            before, after = _file('payroll_before'), _file('payroll_after')
        if not (census or rep):
            return jsonify(error='Upload the census or the proposal report.'), 400
        t0 = time.time()
        audits, summary, notes = builder.run(census_bytes=census[0] if census else None,
                                             report_bytes=rep[0] if rep else None,
                                             before=before, after=after)
        files = [f"{label}: {blob[1]}" for label, blob in
                 (('Census', census), ('Proposal report', rep), ('Payroll before', before), ('Payroll after', after)) if blob]
        para = ''
        try:
            para = groq_client.summary_paragraph(dict(client=client or 'the client', employees=summary['employees'],
                                                      reconciled=summary['matched'], attributed=summary['attributed'],
                                                      no_cause=summary['unexplained'], missing=summary['data_missing'],
                                                      monthly_gap=summary['total_gap'], decreases=summary['decreases'],
                                                      causes=[[k, v['employees'], v['amount']] for k, v in summary['causes'][:4]]))
        except Exception as e:
            notes.append(f'Summary paragraph unavailable: {str(e)[:120]}')
        job = uuid.uuid4().hex[:12]
        JOBS[job] = dict(audits=audits, summary=summary, notes=notes, client=client, period=period,
                         files=files, para=para, created=time.time())
        return jsonify(job=job, seconds=round(time.time() - t0, 1), client=client, period=period,
                       summary=summary, notes=notes, files=files, paragraph=para,
                       employees=[_row(a) for a in audits])
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f'{type(e).__name__}: {e}'), 500


def _row(a):
    d = a.as_dict()
    return dict(name=a.name, employee_id=a.employee_id, verdict=a.verdict, verdict_class=a.verdict_class,
                engine_allotment=a.engine.allotment, actual_net_change=a.actual_net_change,
                allotment_gap=a.allotment_gap, payroll_federal_savings=a.payroll_federal_savings,
                engine_federal_savings=a.engine.federal_savings, federal_gap=a.federal_gap,
                retirement_not_in_census=a.retirement_not_in_census,
                findings=[dict(label=f.label, amount=f.amount, detail=f.detail) for f in a.findings],
                before=d['before'], after=d['after'], engine=d['engine'], census=d['census'],
                pay_periods=a.pay_periods, expected_net_change=a.expected_net_change, identity_gap=a.identity_gap)


@app.get('/report/<job>.docx')
def download(job):
    j = JOBS.get(job)
    if not j:
        abort(404)
    data = reporter.build(j['audits'], j['summary'], j['notes'], client=j['client'], files=j['files'],
                          ai_paragraph=j['para'], period=j['period'])
    name = f"{(j['client'] or 'payroll').replace(' ', '_')}_reconciliation.docx"
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


@app.get('/healthz')
def healthz():
    ok, model = groq_client.health()
    return jsonify(status='ok', groq=ok, model=model, jobs=len(JOBS))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=bool(os.environ.get('DEBUG')))
