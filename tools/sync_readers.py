"""Copy the register readers into the app so the deployed image carries them.

services/register_reader.py used to import these from /Users/nico/attentive/tools, a path that
exists on the laptop and nowhere else. The Dockerfile does `COPY . .`, so the deployed image
never had them and every register silently fell back to the payslip reader, which returns
nothing from a multi-employee register. Vendoring them is the fix.

Run after changing anything in ~/attentive/tools:

    python3 tools/sync_readers.py
"""
import filecmp
import os
import shutil

SRC = os.environ.get('ATTENTIVE_TOOLS', '/Users/nico/attentive/tools')
DST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'services', 'readers')
FILES = ['register.py', 'assignments.py', 'txeis_register.py', 'scanned_register.py']


def main():
    os.makedirs(DST, exist_ok=True)
    init = os.path.join(DST, '__init__.py')
    if not os.path.exists(init):
        open(init, 'w').write(
            '"""Vendored payroll register readers, synced from ~/attentive/tools.\n\n'
            'Do not edit here. Edit the source and run tools/sync_readers.py.\n"""\n')
    for f in FILES:
        s = os.path.join(SRC, f)
        if not os.path.exists(s):
            print('  MISSING %s' % s)
            continue
        d = os.path.join(DST, f)
        same = os.path.exists(d) and filecmp.cmp(s, d, shallow=False)
        shutil.copy2(s, d)
        print('  %-22s %s' % (f, 'unchanged' if same else 'updated'))


if __name__ == '__main__':
    main()
