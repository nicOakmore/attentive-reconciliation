"""The audit document's prose, written by the model from facts this app computed.

The model never sees a payslip, never sees the rules engine, and cannot introduce a number:
it receives a fact sheet, the register, and a hard list of what it may not say. Every number in
the output is one that appeared in the input, and the caller verifies that before using the text.
"""
import json
import re

REGISTER = """WRITING REGISTER (follow exactly):
- Answer first. The first sentence states the result, not the background.
- Plain declarative sentences. Short words. No adjectives, no adverbs of emphasis, no hedging.
- Every claim carries its number. A sentence that asserts something without a figure is wrong.
- No dashes of any kind. No bullet padding, no throat-clearing, no summary of the summary.
- Name the field, the amount and the action. Say what to change and where.
- Never explain that something is correct or consistent: report only what is wrong and what to do.
- No narrative about tools, engines, versions, tables, models or software. Write about payroll,
  the census and the money.
"""

BANNED = ('tax table', 'out of date', 'outdated', 'predates', 'current engine', 'the engine',
          'as an ai', 'i cannot', 'it seems', 'appears to be', 'may be', 'might be', 'possibly')


def _facts(summary, audits, client, period):
    """Everything the model is allowed to know, as numbers."""
    n = max(summary['employees'], 1)
    pct = lambda v: round(100.0 * v / n)
    causes = [dict(cause=k, employees=v['employees'], monthly_amount=round(v['amount'], 2),
                   share_percent=pct(v['employees']))
              for k, v in (summary.get('causes') or [])]
    worst = sorted([a for a in audits if a.allotment_gap is not None and a.allotment_gap < 0],
                   key=lambda a: a.allotment_gap)[:5]
    gap = summary['total_gap'] or 0
    return dict(
        client=client or 'the client', period=period or '',
        employees=summary['employees'],
        taking_home_at_least_promised=summary['matched'],
        taking_home_at_least_promised_percent=pct(summary['matched']),
        short_of_promise_with_a_named_cause=summary['attributed'],
        short_of_promise_with_a_named_cause_percent=pct(summary['attributed']),
        short_of_promise_cause_not_established=summary['unexplained'],
        short_of_promise_cause_not_established_percent=pct(summary['unexplained']),
        not_comparable_files_missing=summary['data_missing'],
        total_monthly_shortfall=round(gap, 2),
        total_annual_shortfall=round(gap * 12, 2),
        employees_whose_take_home_falls=summary['decreases'],
        causes_by_size=causes,
        five_largest_shortfalls=[dict(employee=a.name, monthly_gap=a.allotment_gap,
                                      promised=a.engine.allotment, actual=a.actual_net_change,
                                      causes=[f.label for f in a.findings]) for a in worst],
    )


def _numbers(text):
    return set(re.findall(r'\d+(?:\.\d+)?', (text or '').replace(',', '')))


LAST_REASON = ''


def audit_paragraphs(summary, audits, client='', period='', paragraphs=3):
    """The document's opening. Returns '' rather than anything unverified, and records why."""
    global LAST_REASON
    LAST_REASON = ''

    from . import groq_client as G
    facts = _facts(summary, audits, client, period)
    prompt = (
        f"Write {paragraphs} short paragraphs opening a payroll reconciliation report for "
        f"{facts['client']}. The reader runs the programme and must decide what to fix this week.\n\n"
        f"{REGISTER}\n"
        "CONTENT, in this order:\n"
        "1. How many employees were reconciled, how many take home at least what was promised, and the "
        "total monthly shortfall across the rest.\n"
        "2. The causes in order of size, each with its employee count and monthly amount, said as what is "
        "wrong in the data rather than as a category name.\n"
        "3. What to change first, taken from the largest cause, naming the census field.\n\n"
        "You may use only the numbers in this fact sheet. Do not compute new numbers. Do not name any "
        "employee who is not listed. Write at most 130 words in total.\n\n"
        + json.dumps(facts, default=str))
    try:
        text = G._post(dict(model=G.TEXT_MODEL, temperature=0.1, max_tokens=500,
                            messages=[{'role': 'user', 'content': prompt}])).strip()
    except Exception as exc:
        LAST_REASON = f'model call failed: {type(exc).__name__}: {str(exc)[:120]}'
        return ''
    if not text:
        LAST_REASON = 'model returned nothing'
        return ''
    low = text.lower()
    hit = [b for b in BANNED if b in low]
    if hit:
        LAST_REASON = f'rejected, banned wording: {hit[0]}'
        return ''
    # Every number in the prose must have come from the fact sheet: the model may not invent one.
    extra = sorted(_numbers(text) - _numbers(json.dumps(facts, default=str)))
    if extra:
        LAST_REASON = f'rejected, numbers not in the fact sheet: {", ".join(extra[:6])}'
        return ''
    LAST_REASON = 'accepted'
    return text
