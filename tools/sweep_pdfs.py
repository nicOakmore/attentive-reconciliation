"""Read every payroll PDF we hold and report what happened to each one.

test_readers.py checks seven known cases. This sweeps the whole corpus across every client we
have ever processed, so a change to a reader is measured against all of them and not just the
file in front of us. Duplicates are collapsed by content hash, because the same Tioga pack sits
in four directories.

    python3 tools/sweep_pdfs.py              # every file
    python3 tools/sweep_pdfs.py --quick      # skip the slow OCR ones
    SCANREG_OCR=rapid python3 tools/sweep_pdfs.py    # the Linux path

A row is a FAIL when the file cannot be read at all, or when a register parses but does not tie
to the totals it prints on its own last page. A register that ties is right by construction; a
payslip pack is judged on how many statements carried a name and a net.
"""
import hashlib
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOME = os.path.expanduser('~')
ROOTS = [
    os.path.join(HOME, 'attentive_audit', 'mock'),
    os.path.join(HOME, 'attentive_audit', 'cs1300'),
    os.path.join(HOME, 'attentive_audit_app', 'samples'),
    os.path.join(HOME, 'attentive_mineola'),
    os.path.join(HOME, 'attentive_tba'),
    os.path.join(HOME, 'attentive_aspermont'),
    os.path.join(HOME, 'Downloads', 'reproposal'),
    os.path.join(HOME, 'Downloads', 'reproposal(1)'),
    os.path.join(HOME, 'Downloads', 'reproposal(2)'),
    os.path.join(HOME, 'Downloads', 'reproposal3'),
    os.path.join(HOME, 'Downloads', 'reproposal(4)'),
]
# Our own output, not payroll input.
SKIP = ('audit_review', 'system_audit', '/build/', 'v12.pdf', 'v13.pdf', 'v2_ql.pdf',
        'exhibit', '/ex/', 'evidence')
# A proposal summary is our own savings report printed to PDF. It carries employee names and
# money, so it reads like payroll and fails like payroll, but it is an output and does not
# belong in a reader sweep. Rockport ISD - Updated.pdf is one and was counted as a failure.
NOT_PAYROLL = ('proposal summary', 'savings report', 'average allotment',
               'total allotment', 'employee monthly allotment')


def collect():
    seen, out = {}, []
    for root in ROOTS:
        for dirpath, _, names in os.walk(root):
            for nm in sorted(names):
                if not nm.lower().endswith('.pdf'):
                    continue
                p = os.path.join(dirpath, nm)
                low = p.lower()
                if any(s in low for s in SKIP):
                    continue
                try:
                    h = hashlib.md5(open(p, 'rb').read()).hexdigest()
                except Exception:
                    continue
                if h in seen:
                    continue
                seen[h] = p
                out.append(p)
    return out


def is_payroll(path):
    """Decided from the first page, not the filename."""
    import subprocess
    import tempfile
    try:
        o = os.path.join(tempfile.mkdtemp(), 'p.txt')
        subprocess.run(['pdftotext', '-layout', '-f', '1', '-l', '1', path, o],
                       capture_output=True, timeout=60)
        txt = open(o, errors='replace').read().lower() if os.path.exists(o) else ''
    except Exception:
        return True
    return not any(k in txt for k in NOT_PAYROLL)


def main():
    quick = '--quick' in sys.argv
    from services import register_reader as RR
    from services import parse_files as P

    files = collect()
    skipped = [f for f in files if not is_payroll(f)]
    files = [f for f in files if is_payroll(f)]
    for f in skipped:
        print('not payroll, excluded: %s' % os.path.basename(f))
    print('OCR engine: %s' % os.environ.get('SCANREG_OCR', 'platform default'))
    print('%d payroll PDFs, duplicates collapsed by content' % len(files))
    print()
    print('%-17s %-7s %5s %7s  %s' % ('read as', 'result', 'emps', 'secs', 'file'))
    bad, rows = [], []
    for p in files:
        name = os.path.basename(p)
        data = open(p, 'rb').read()
        t0 = time.time()
        try:
            kind, _ = RR.looks_like_register(data)
            if kind == 'payslips':
                if quick:
                    print('%-17s %-7s %5s %7s  %s' % ('payslips', 'SKIP', '', '', name[:52]))
                    continue
                recs = P.paychecks_from_pdf(data, hint='sweep', source_name=name)
                n = len(recs)
                named = sum(1 for r in recs if r.get('name') and r.get('net_pay') is not None)
                ok = n > 0 and named >= 0.8 * n
                note = '' if ok else '%d of %d statements carry a name and a net' % (named, n)
            else:
                if quick and kind == 'scanned-register':
                    print('%-17s %-7s %5s %7s  %s' % (kind, 'SKIP', '', '', name[:52]))
                    continue
                recs, rep = RR.read(data, 'sweep')
                n = len(recs or [])
                gate = rep.get('gate') or []
                offs = [g for g in gate
                        if abs((g.get('parsed') or 0) - (g.get('printed') or 0)) > 0.02]
                ok = n > 0 and not offs
                note = ('%d of %d printed controls do not tie' % (len(offs), len(gate))
                        if offs else ('no employees read' if not n else ''))
                if rep.get('names_unresolved'):
                    note = (note + '; ' if note else '') + \
                        '%d of %d names unresolved' % (rep['names_unresolved'], n)
            secs = time.time() - t0
            rows.append((kind, ok, n, secs, name, note))
            if not ok:
                bad.append(name)
            print('%-17s %-7s %5d %7.1f  %s%s'
                  % (kind, 'PASS' if ok else 'FAIL', n, secs, name[:52],
                     '  ' + note if note else ''))
        except Exception as e:
            bad.append(name)
            print('%-17s %-7s %5s %7.1f  %s  %s: %s'
                  % ('', 'ERROR', '', time.time() - t0, name[:52], type(e).__name__, str(e)[:70]))
            if os.environ.get('SWEEP_TRACE'):
                traceback.print_exc()
    print()
    by = {}
    for kind, ok, n, secs, name, note in rows:
        d = by.setdefault(kind, [0, 0])
        d[0] += 1
        d[1] += 1 if ok else 0
    for kind, (tot, good) in sorted(by.items()):
        print('  %-17s %d of %d read cleanly' % (kind, good, tot))
    print()
    print('FAILURES: %d%s' % (len(bad), ('  ' + ', '.join(bad[:6])) if bad else ''))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
