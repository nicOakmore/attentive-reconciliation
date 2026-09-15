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

## How it reads a scanned statement, and when it reaches for more

A born digital PDF is read from its own text layer and costs nothing. A scan is rendered and read with RapidOCR,
keeping every word's box, because the geometry is the evidence: a label identifies the row, the block identifies
which side of the page, and the x position identifies the column, with the column learned from where that table's
own figures line up. Taking the first amount printed after a label is what used to put the premium or the fee into
the withholding line, since these pages print a second table across the same rows. A figure that is not in its own
column is reported unread rather than guessed.

The page's orientation is settled by whether it reads, once per pack, which is what lets a register from another
provider, scanned sideways, read correctly.

Net pay appears several times on one statement: its own line, the deposit total, the sum of the deposit rows, and
gross less total deductions. Where two agree the agreed figure is used as an extraction control, the labelled line
winning among equals; where the labelled line and the rest of the page disagree the difference is reported and the
employee is unverified; where nothing agrees no net pay is taken.

Everything above is the ordinary path and it is all a readable page needs. Only for an employee whose figures do
not tie does the tool reach further, in `services/estimate.py`, `services/crossdoc.py` and `services/fuzzy.py`: a
weighted solve over the statement's own identities to say which line is least consistent, a digit confusion
posterior for what it was likely printed as, Monte Carlo through to the gap, fuzzy ranges where a figure cannot be
pinned, and the census and the proposal's inputs as independent evidence. The proposal's own calculations are
never used that way: they are what the audit tests. None of it replaces a printed figure or closes an exception.

The model is the last resort, asked only for a page with no identity or with neither a withholding nor a net pay,
and it stops being asked once the key is rate limited. It never computes a figure and never states a cause. All
arithmetic is in `services/audit.py`.

## The page store

Every page read is stored under a key derived from a deterministic render of the page, with its words and boxes,
the column model, and each figure's provenance. A pack read once comes back in seconds. A reviewer's correction is
appended to the page, never overwrites the machine reading, and applies to every later run; `POST /correct` takes
one, `GET /page/<key>` shows what a page holds. An ambiguous identity is a miss, never a hit.

## Controls the report carries

Population and matching (population, matched to both statements, to one, to none, statements excluded), the
matching hierarchy, the extraction control disclosure, a note that cause counts are not mutually exclusive, and
the W-4 fields the statements do not print.

## Accuracy and speed

On the Tioga ISD sample (40 employees, 84 scanned statements) against figures verified by hand from the same
files: all 40 employees matched to both statements, and 39 of 40 agree to the cent on both the net pay change and
the allotment gap. The one difference is a dollar, on the employee whose statement carries a non-tax line. At the
page level, federal withholding reads 70 exact, 11 unread and none wrong.

A cold run of the 84 page pack takes about seven and a half minutes on a two CPU instance. The same pack read
again takes seconds, because the pages are in the store. `tools/score_pages.py` scores the reader page by page,
`tools/benchmark.py` compares the layers and injects deliberate corruptions, and `tools/calibrate.py` measures the
reading error on half the known pages and validates on the other half.

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
