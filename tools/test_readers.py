"""Regression test for every PDF reading path the app has.

Four families reach this app and each has failed in a different way:

  payslips            one employee per page, read by parse_files (OCR where needed)
  text register       Texas ESC 4packr01, Mineola
  TxEIS register      Ascender HRS2200, Aspermont
  scanned register    Paylocity, The Breathing Association, no text layer, rotated

The scanned one is the trap. It used macOS Vision with no fallback, so it worked on a laptop
and failed on Linux, which is where the app actually runs. Run this with SCANREG_OCR=rapid to
exercise the Linux path on a Mac:

    SCANREG_OCR=rapid python3 tools/test_readers.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import register_reader as RR
from services import parse_files as P

CASES = [
    ('text register   ', '/Users/nico/Downloads/reproposal3/Updated Mineola Previous 11.1.26.pdf',
     'text-register', 350),
    ('text register   ', '/Users/nico/Downloads/reproposal3/Updated Mineola Mock 11.1.26.pdf',
     'text-register', 350),
    ('TxEIS register  ', '/Users/nico/Downloads/reproposal(4)/DHrs2200PyrlEarningsRpt_2026-08-11_092047315.pdf',
     'txeis-register', 42),
    ('TxEIS register  ', '/Users/nico/Downloads/reproposal(4)/DHrs2200PrepostPyrlEarningsRpt_2026-09-15_110450335.pdf',
     'txeis-register', 36),
    ('scanned register', '/Users/nico/Downloads/reproposal(2)/8.1 The Breathing Association - Previous.pdf',
     'scanned-register', None),
    ('scanned register', '/Users/nico/Downloads/reproposal(2)/8.1 The Breathing Ass. - Mock.pdf',
     'scanned-register', None),
    ('payslips        ', '/Users/nico/attentive_audit_app/samples/Tioga ISD (7.31 report)/9.1 Tioga ISD - Prev.pdf',
     'payslips', None),
]


def main():
    engine = os.environ.get('SCANREG_OCR', 'platform default')
    print('OCR engine: %s' % engine)
    print('%-17s %-16s %-7s %6s  %s' % ('family', 'detected as', 'result', 'secs', 'file'))
    bad = 0
    for family, path, expect_kind, expect_n in CASES:
        name = os.path.basename(path)[:46]
        if not os.path.exists(path):
            print('%-17s %-16s %-7s %6s  %s' % (family, '', 'SKIP', '', name + '  (not on this machine)'))
            continue
        t0 = time.time()
        try:
            kind, _ = RR.looks_like_register(open(path, 'rb').read())
            if kind == 'payslips':
                recs = P.paychecks_from_pdf(open(path, 'rb').read(), hint='test',
                                            source_name=name)
                n, note = len(recs), ''
            else:
                recs, rep = RR.read(open(path, 'rb').read(), 'test')
                n = len(recs or [])
                note = ''
            ok = (kind == expect_kind) and (expect_n is None or n == expect_n)
            if expect_n is not None and n != expect_n:
                note = 'expected %d employees' % expect_n
            if kind != expect_kind:
                note = ('detected %s, expected %s' % (kind, expect_kind)) + (', ' + note if note else '')
            bad += 0 if ok else 1
            print('%-17s %-16s %-7s %6.1f  %s  %d read%s'
                  % (family, kind, 'PASS' if ok else 'FAIL', time.time() - t0, name, n,
                     '  ' + note if note else ''))
        except Exception as e:
            bad += 1
            print('%-17s %-16s %-7s %6.1f  %s  %s: %s'
                  % (family, '', 'ERROR', time.time() - t0, name, type(e).__name__, str(e)[:90]))
    print()
    print('FAILURES: %d' % bad)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
