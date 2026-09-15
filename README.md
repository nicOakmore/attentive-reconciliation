# Attentive payroll reconciliation

Upload the payroll before the premium, the payroll after it, the census and the proposal savings report. The app
reconciles every employee and produces a docx with one block per employee: verdict, census input, paycheck comparison,
the arithmetic, engine reconciliation, cause, allotment against actual net pay, resolution.

The arithmetic is deterministic (`services/audit.py`). Groq reads payroll statements that have no text layer, fills
fields the label parser could not find, maps unfamiliar spreadsheet headers, and writes the one summary paragraph. It
never produces a number or a cause.

Scanned statements are OCRd (tesseract in the container), rotated pages are detected, and two identities repair noisy
readings: gross anchored to the census gross per pay period, and net pay against the printed total deductions. Any
value that is derived rather than printed is recorded in the employee's source note.

Environment: `GROQ_API_KEY`, optional `GROQ_TEXT_MODEL` (default openai/gpt-oss-120b), optional `SAMPLE_DIR`.
