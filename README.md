# Attentive payroll reconciliation

Reconciles a wellness-premium proposal against the payroll that was actually run, employee by employee, and names
the cause of every difference the submitted files establish.

Live: https://attentive-reconciliation-oak.onrender.com

## What it takes

Four files, dropped on the page: the payroll register before the premium, the mock register after it, the client
census, and the proposal tool's savings report. Registers may be scanned PDFs or spreadsheets; the census and the
report are spreadsheets.

## What it gives back

A per-employee reconciliation on screen and the same thing as a Word report: the census input the engine received,
the two paychecks side by side, the arithmetic in sentences, the engine against the payroll, the cause, the engine
allotment against the actual net pay change, and a resolution candidate.

## How it reads a scanned statement

Pages are rendered at 2.8x, read with RapidOCR, and the words are regrouped into lines by position, because a
payroll statement is columnar and a flat reading merges the deduction table into the tax lines. Figures are then
located by their printed labels, matched on a squeezed form of the line because OCR drops spaces and swaps i, l
and 1. Net pay appears four times on one statement (its own line, the deposit total, the sum of the deposit rows,
and gross less total deductions); where two printings agree the agreed figure is accepted, and where none agree no
net pay is accepted and the employee is reported unverified. Nothing is inferred to make a reconciliation tie.

Groq is a fallback only: it locates a line the label reader could not find, one page at a time, within a per-run
budget. It never computes a figure and never states a cause. All arithmetic is in `services/audit.py`.

## Controls the report carries

Population and matching (population, matched to both statements, to one, to none, statements excluded), the
matching hierarchy, the extraction control disclosure, a note that cause counts are not mutually exclusive, and
the W-4 fields the statements do not print.

## Accuracy

On the Tioga ISD sample (40 employees, 84 scanned statements) against figures verified by hand from the same
files: 34 employees carry an accepted net pay on both statements, 32 of those agree to the cent, 2 are reported
unverified because the statement identity does not tie. No employee is reported with a figure the evidence does
not support. A full run takes seven to eleven minutes.

## Running it

    pip install -r requirements.txt
    GROQ_API_KEY=... python app.py        # http://localhost:5000

Docker is what Render builds: one gunicorn worker (a run lives in process memory), `PDF_WORKERS` pages in flight,
`OCR_THREADS` inference threads per page.

## Layout

    app.py                     Flask: upload, run as a background job, status polling, docx download, diagnostics
    services/parse_files.py    spreadsheets, OCR, label reading, the extraction controls
    services/audit.py          the arithmetic, the cause attribution, the verdicts
    services/report.py         the Word report
    services/build.py          assembling census, proposal and both registers into one run
    services/groq_client.py    the fallback reader and the summary paragraph
