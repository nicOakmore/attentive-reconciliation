"""The audit document's opening, written by the model from facts this app computed.

The model never sees a payslip and never sees the rules. It receives a fact sheet in which the money is
already written as money, the register it must write in, and a list of words it may not use. Two gates run
before the text is used: banned wording, and any figure that does not appear in the fact sheet. A text that
fails either gate is dropped and the document carries no opening rather than an unverified sentence.
"""
import json
import re

REGISTER = """WRITING REGISTER (follow exactly):
- Answer first. The first sentence states the result, not the background.
- Plain declarative sentences. Short words. No adjectives, no adverbs of emphasis, no hedging.
- Every claim carries its number. A sentence that asserts something without a figure is wrong.
- No dashes of any kind. No throat-clearing, no summary of the summary.
- Name the field, the amount and the action. Say what to change and where.
- Never say that something is correct or consistent: report only what is wrong and what to do.
- No narrative about tools, engines, versions, tables, models or software. Write about payroll,
  the census and the money.
"""

BANNED = ('tax table', 'out of date', 'outdated', 'predates', 'current engine', 'the engine',
          'as an ai', 'i cannot', 'it seems', 'appears to be', 'may be', 'might be', 'possibly')

LAST_REASON = ''


def _facts(summary, audits, client, period):
    """Everything the model is allowed to know. Money arrives already written as money, so the model copies a
    figure rather than formatting one, and each cause carries its own fix rather than a bare total."""
    from .audit import _ACTIONS
    m = lambda v: '' if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")
    short = [a for a in audits if a.allotment_gap is not None and a.allotment_gap < -0.02]
    by_cause = {}
    for a in short:
        for f in a.findings:
            c = by_cause.setdefault(f.label, {'employees': 0, 'action': _ACTIONS.get(f.label, '')})
            c['employees'] += 1
    causes = sorted(({'cause': k, 'employees_affected': v['employees'], 'the_fix': v['action']}
                     for k, v in by_cause.items()), key=lambda c: -c['employees_affected'])[:6]
    worst = sorted(short, key=lambda a: a.allotment_gap)[:3]
    gap = summary['total_gap'] or 0
    return dict(
        client=client or 'the client', period=period or '',
        employees_reconciled=summary['employees'],
        employees_taking_home_at_least_promised=summary['matched'],
        employees_short_of_the_promise=len(short),
        employees_not_verified=summary['unexplained'],
        total_monthly_shortfall=m(gap),
        total_annual_shortfall=m(gap * 12),
        causes_by_employees_affected=causes,
        three_largest_shortfalls=[dict(employee=a.name, short_by_a_month=m(a.allotment_gap),
                                       promised=m(a.engine.allotment), actual=m(a.actual_net_change))
                                  for a in worst],
    )


def _numbers(text):
    """Numbers as values, not as strings: 2967.30 and 2967.3 are the same figure."""
    out = set()
    for token in re.findall(r'\d+(?:\.\d+)?', (text or '').replace(',', '')):
        try:
            out.add(round(float(token), 2))
        except ValueError:
            pass
    return out


def audit_paragraphs(summary, audits, client='', period=''):
    """The document's opening. Returns '' rather than anything unverified, and records why."""
    global LAST_REASON
    LAST_REASON = ''
    from . import groq_client as G
    facts = _facts(summary, audits, client, period)
    allowed = _numbers(json.dumps(facts, default=str))
    prompt = (
        f"Write the opening of a payroll reconciliation report for {facts['client']}"
        f"{' for ' + facts['period'] if facts['period'] else ''}. The reader runs the programme and has to "
        f"decide what to fix this week.\n\n"
        f"{REGISTER}\n"
        "Write exactly three short paragraphs, at most 120 words in total.\n"
        "Paragraph 1: how many employees were reconciled, how many take home at least what was promised, how "
        "many are short, and the total monthly shortfall. Copy the money exactly as it is written here.\n"
        "Paragraph 2: the two or three causes affecting the most employees, each with the number of employees "
        "affected, said as what is wrong in the data rather than as a category name.\n"
        "Paragraph 3: what to do first, taken from the_fix of the cause affecting the most employees, naming "
        "the same field it names.\n\n"
        "Rules you must not break: use only figures that appear in this fact sheet, copied exactly, including "
        "the dollar sign. Do not add a percentage, do not work out a total of your own, do not name an employee "
        "who is not listed, and do not name a census field that the fact sheet does not name.\n\n"
        + json.dumps(facts, indent=1, default=str))
    try:
        text = G._text(prompt, max_tokens=1400)
        extra = sorted(_numbers(text) - allowed)
        if text and extra:
            text = G._text(prompt + '\n\nYour previous answer used figures that are not in the fact sheet: '
                           + ', '.join(str(x) for x in extra[:8])
                           + '. Write it again using only figures that appear above, copied exactly.',
                           max_tokens=1400)
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
    extra = sorted(_numbers(text) - allowed)
    if extra:
        LAST_REASON = f'rejected, figures not in the fact sheet: {", ".join(str(x) for x in extra[:6])}'
        return ''
    LAST_REASON = 'accepted'
    return text
