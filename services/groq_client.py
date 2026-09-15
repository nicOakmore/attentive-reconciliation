"""Groq calls. Two jobs only: read files the parser cannot read deterministically, and map unknown column headers.
The model never computes a saving, never states a cause and never sees the report text.
"""
import os, json, base64, re, time, threading
import requests

API = 'https://api.groq.com/openai/v1/chat/completions'
TEXT_MODEL = os.environ.get('GROQ_TEXT_MODEL', 'openai/gpt-oss-120b')
# The key is metered in tokens per minute, and one statement is a few thousand tokens. Several pages in flight just
# collect 429s whose Retry-After runs into minutes, so calls are queued one at a time and never waited on for long.
_SEM = threading.Semaphore(int(os.environ.get('GROQ_CONCURRENCY', '1')))
MAX_WAIT = float(os.environ.get('GROQ_MAX_WAIT', '25'))


class GroqUnavailable(RuntimeError):
    pass


def _key():
    k = os.environ.get('GROQ_API_KEY', '').strip()
    if not k:
        raise GroqUnavailable('GROQ_API_KEY is not set')
    return k


def _post(payload, tries=2):
    last = None
    with _SEM:
        for i in range(tries):
            try:
                r = requests.post(API, headers={'Authorization': f'Bearer {_key()}', 'Content-Type': 'application/json'},
                                  data=json.dumps(payload), timeout=60)
                if r.status_code == 429:
                    wait = float(r.headers.get('retry-after') or 0) or (2 + 3 * i)
                    last = f'rate limited, retry after {wait:.0f}s'
                    if wait > MAX_WAIT or i == tries - 1:
                        raise GroqUnavailable(f'Groq rate limit: {last}')
                    time.sleep(wait)
                    continue
                if r.status_code >= 400:
                    raise GroqUnavailable(f'Groq {r.status_code}: {r.text[:300]}')
                return r.json()['choices'][0]['message']['content']
            except requests.RequestException as e:
                last = str(e)
                if i == tries - 1:
                    break
                time.sleep(1 + i)
    raise GroqUnavailable(f'Groq unreachable: {last}')


def _json_from(text):
    m = re.search(r'\{.*\}|\[.*\]', text, re.S)
    if not m:
        raise GroqUnavailable('model did not return JSON')
    return json.loads(m.group(0))


def map_columns(headers, sample_rows, wanted):
    """Map a spreadsheet's headers onto the fields the audit needs. Returns {wanted_field: header or null}."""
    prompt = ('You map spreadsheet columns. Return JSON only: an object whose keys are the requested fields and whose '
              'values are the exact header string from the file, or null when the file has no such column. '
              'Never invent a header.\n\n'
              f'Requested fields: {json.dumps(wanted)}\n'
              f'Headers: {json.dumps(headers)}\n'
              f'First rows: {json.dumps(sample_rows[:3], default=str)}')
    out = _json_from(_post(dict(model=TEXT_MODEL, temperature=0, max_tokens=1200,
                               messages=[{'role': 'user', 'content': prompt}])))
    return {k: (v if v in headers else None) for k, v in out.items()}


PAYCHECK_FIELDS = ('employee_name', 'employee_id', 'pay_date', 'gross', 'federal_withholding', 'state_withholding',
                   'state_code', 'social_security', 'medicare', 'taxable_wages', 'medicare_gross', 'net_pay',
                   'premium_pretax', 'reimbursement', 'employee_fee_aftertax', 'product_sold', 'retirement',
                   'cafeteria_pretax', 'other_deductions')


def structure_paycheck_text(text, hint=''):
    """Turn one payroll statement's text (from the PDF text layer or from OCR) into records.
    The model copies printed amounts and returns null for anything absent. It performs no arithmetic."""
    instruction = (
        'The text below is a payroll statement, possibly from OCR so it may contain noise and the columns may be out of '
        'order. Return JSON only: an array with one object per employee, using these keys: '
        f'{list(PAYCHECK_FIELDS)}. Rules: copy amounts exactly as printed, as numbers, without currency symbols or '
        'commas; use null for any line the statement does not show; never calculate, total or infer a value; a '
        'this-period column and a year-to-date column often sit side by side, always take the this-period value, which '
        'is the smaller one and sits next to the label; retirement means a retirement reduction line such as TRS '
        'Salary Red, 403(b) or 457; cafeteria_pretax means the pre-tax health, dental and vision lines added together '
        'only if the statement itself prints a total, otherwise null; premium_pretax is a pre-tax wellness or PCM '
        'premium line; employee_fee_aftertax is an after-tax administration fee line such as PCM Aftertax; '
        'product_sold is a post-tax product line such as SIA.'
        + (f' Context: {hint}.' if hint else '') + '\n\nSTATEMENT TEXT:\n' + _tighten(text))
    data = _json_from(_post(dict(model=TEXT_MODEL, temperature=0, max_tokens=2000,
                                 messages=[{'role': 'user', 'content': instruction}])))
    return data if isinstance(data, list) else [data]


def _tighten(text, limit=5000):
    """Send the model the lines that carry a label and an amount, not the leave balances and the bank block. Fewer
    tokens per page is what keeps a pack inside the key's per-minute budget."""
    keep = []
    for line in (text or '').split('\n'):
        if re.search(r'\d', line) and re.search(r'[A-Za-z]{3}', line):
            keep.append(line.strip())
        elif re.search(r'employee name|emp nbr|filing status', line, re.I):
            keep.append(line.strip())
    out = '\n'.join(keep)
    return out[:limit] if out else (text or '')[:limit]


def summary_paragraph(facts):
    """One short paragraph for the front summary. The model receives computed numbers and may not add any."""
    prompt = ('Write one paragraph of at most 90 words for the front page of a payroll reconciliation report. '
              'Use only the numbers supplied. State the result first. Plain declarative sentences, no dashes, no '
              'hedging, no adjectives, no invented causes. Do not add any number that is not in the input.\n\n'
              + json.dumps(facts, default=str))
    return _post(dict(model=TEXT_MODEL, temperature=0.1, max_tokens=300,
                      messages=[{'role': 'user', 'content': prompt}])).strip()


def health():
    try:
        _post(dict(model=TEXT_MODEL, max_tokens=5, messages=[{'role': 'user', 'content': 'ok'}]))
        return True, TEXT_MODEL
    except Exception as e:
        return False, str(e)[:160]
