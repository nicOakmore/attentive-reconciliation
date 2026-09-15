"""Read a page that is a table of people rather than one person's statement.

A payroll journal prints a header naming the columns and then one row per employee, with subtotals, section
headings and repeated headers mixed in. The reading rules here follow the two reviews this design went through:

  * A header is not "a line with three familiar words on it". It is a line whose labels sit in distinct x regions
    and under which the rows below actually print numbers. The evidence is scored and a weak score is not a table.
  * A column is learned from the page, not from the header's alignment: the header gives a prior, the right edges
    of the numbers beneath it give the column. Accounting columns are right aligned; header text often is not.
  * Every band below the header is classified, and the classes are reported. An employee row, a continuation, a
    repeated header, a subtotal and a row that cannot be read are five different things, and a row that cannot be
    read must stay visible rather than vanish.
  * A row without an identity may continue the row above it, never start a new record.
"""
import re, statistics as stats
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

from .pageread import Word, Amount, row_bands, amounts_from_words, label_runs, _numberish

TABLE_READER_VERSION = '2026.09.15.1'

# Canonical field names and the aliases providers print for them. Aliases only ever propose a column; whether the
# column is real is decided by the numbers printed beneath it.
STATES = ('al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|'
          'oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc')
STATE_NAMES = ('alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|'
               'idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|'
               'minnesota|mississippi|missouri|montana|nebraska|nevada|ohio|oklahoma|oregon|pennsylvania|'
               'tennessee|texas|utah|vermont|virginia|washington|wisconsin|wyoming')

# Aliases are provider neutral: the same economic field printed the many ways payroll systems print it. An alias
# only ever proposes a column; whether the column is real is decided by the numbers printed beneath it.
ALIASES = {
    'gross':            [r'gross\s*(pay|wage|wages|earnings)?', r'total\s*gross', r'standard\s*gross',
                         r'paid\s*gross', r'earnings'],
    'federal':          [r'fed(eral)?\s*(income)?\s*(tax|w[/]?h|whd?)\b', r'fitwh', r'\bfit\b',
                         r'federal\s*wh', r'w\w{0,6}holding\s*tax', r'\bfedwh\b'],
    'social_security':  [r'social\s*security', r'soc\s*sec', r'socsec\w*', r'\bsoc\b', r'\bss\b', r'oasdi',
                         r'\bfica\b'],
    'medicare':         [r'medicare', r'\bmed\s*ee\b', r'\bmedee\b', r'\bmed\b', r'\bmedi\b'],
    'state':            [r'state\s*(income)?\s*(tax|w[/]?h)?', r'\bsit\b',
                         rf'\b({STATES})\s*w[/]?h\b', rf'({STATE_NAMES})\s*w[/]?h'],
    'net_pay':          [r'net\s*(pay|check|amount)', r'\bnet\b', r'take\s*home', r'check\s*amount'],
    'taxable_wages':    [r'taxable\s*wages', r'fed\w*\s*taxable'],
    'medicare_gross':   [r'medicare\s*(gross|wages)'],
    'premium':          [r'pre\s*tax', r'pcm\s*pretax', r'cafeteria', r'section\s*125'],
    'fee':              [r'after\s*tax', r'pcm\s*aftertax', r'admin\s*fee', r'monthly\s*fee'],
    'reimbursement':    [r'simrp', r'reimburse'],
    'retirement':       [r'\btrs\b', r'403\s*\(?b\)?', r'\b457\b', r'retirement', r'pension'],
    'hours':            [r'\bhours\b', r'\bhrs\b'],
    # a proposal table rather than a payroll one; the same machinery reads it
    'allotment':        [r'(employee|ee)?\s*monthly\s*allotment', r'\ballotment\b'],
    'ee_savings':       [r'(ee|employee)\s*gross\s*monthly\s*savings', r'net\s*tax\s*savings',
                         r'monthly\s*savings'],
    'employee_id':      [r'client\s*(ee|employee)?\s*id', r'employee\s*id', r'emp\s*(#|no|nbr|id)'],
}

NON_EMPLOYEE = re.compile(r'\b(total|totals|subtotal|sub-total|grand|summary|department|dept|campus|company|'
                          r'page|report|continued|balance|count|employer)\b', re.I)
IDENTITY = re.compile(r'[A-Za-z]{2,}')
ID_NUMBER = re.compile(r'^\s*(\d{2,10}|[A-Z]{0,3}\d{2,10})\s*$')


@dataclass
class Column:
    name: str
    header_x0: float
    header_x1: float
    right_edge: Optional[float] = None       # learned from the numbers beneath the header
    spread: float = 0.0
    seen: int = 0


@dataclass
class Row:
    index: int
    kind: str                                 # employee, continuation, header repeat, subtotal, section, unread
    identity: str = ''
    identity_id: str = ''
    values: Dict[str, float] = field(default_factory=dict)
    provenance: Dict[str, List[int]] = field(default_factory=dict)
    source_rows: List[int] = field(default_factory=list)
    note: str = ''


@dataclass
class TableReading:
    is_table: bool
    score: float
    columns: List[Column]
    rows: List[Row]
    coverage: Dict[str, int]
    header_row: Optional[int] = None
    note: str = ''

    def employees(self):
        return [r for r in self.rows if r.kind == 'employee']

    def as_record(self):
        return dict(is_table=self.is_table, score=self.score, header_row=self.header_row,
                    columns=[asdict(c) for c in self.columns],
                    rows=[asdict(r) for r in self.rows], coverage=self.coverage, note=self.note,
                    table_reader_version=TABLE_READER_VERSION)


def _alias_hits(text):
    low = (text or '').lower()
    out = []
    for name, pats in ALIASES.items():
        if any(re.search(p, low) for p in pats):
            out.append(name)
    return out


def _named_runs(ws, row_height):
    out = []
    for r in label_runs(ws, row_height):
        hits = _alias_hits(' '.join(w.text for w in r))
        if hits:
            out.append((hits[0], min(w.x0 for w in r), max(w.x1 for w in r)))
    return out


def _header_candidates(lines, row_height):
    """Lines whose label runs name at least three different fields in distinct places on the line.

    A header is often printed on two lines, the field name split across them ("EE Gross Monthly" over "Savings"),
    so each line is also tried joined with the line below it. The header region reported is the last line of the
    pair, because the data begins after it.
    """
    out = []
    for n, (key, ws, text, amts) in enumerate(lines):
        named = _named_runs(ws, row_height)
        if len({nm for nm, _, _ in named}) >= 3:
            out.append((n, named))
        if n + 1 < len(lines):
            joined = named + _named_runs(lines[n + 1][1], row_height)
            merged = {}
            for nm, x0, x1 in joined:
                if nm in merged:
                    merged[nm] = (min(merged[nm][0], x0), max(merged[nm][1], x1))
                else:
                    merged[nm] = (x0, x1)
            if len(merged) >= 3 and len(merged) > len({nm for nm, _, _ in named}):
                out.append((n + 1, [(nm, v[0], v[1]) for nm, v in merged.items()]))
    return out


def _learn_columns(named, lines, start, row_height, look=40):
    """Give each header label the column of numbers printed beneath it.

    The header's own x range is only a prior: a heading may be centred or left aligned over a column of right
    aligned figures. The column is the median right edge of the numbers that fall under the heading's neighbourhood,
    which is what makes this work across providers.
    """
    cols = [Column(name=nm, header_x0=x0, header_x1=x1) for nm, x0, x1 in named]
    for c in cols:
        lo = c.header_x0 - row_height * 2
        hi = c.header_x1 + row_height * 4
        edges = []
        for key, ws, text, amts in lines[start + 1:start + 1 + look]:
            for a in amts:
                if lo <= a.x1 <= hi or (a.x0 >= lo and a.x1 <= hi):
                    edges.append(a.x1)
        if len(edges) >= 2:
            m = stats.median(edges)
            c.right_edge = m
            c.spread = stats.median([abs(e - m) for e in edges])
            c.seen = len(edges)
    return [c for c in cols if c.right_edge is not None]


def _classify(ws, amts, cols, row_height, prev_employee):
    """What kind of row this is. An employee row needs an identity and numbers; a row with numbers and no identity
    may continue the row above; a row whose identity is an aggregate word is not a person."""
    text = ' '.join(w.text for w in ws).strip()
    if not text:
        return 'section', '', ''
    left_cells = [w for w in ws if not _numberish(w)]
    leftmost = ' '.join(w.text for w in sorted(left_cells, key=lambda w: w.x0)[:4]).strip()
    id_words = [w.text.strip() for w in ws if ID_NUMBER.match(w.text.strip() or 'x')]
    hits = _alias_hits(text)
    populated = sum(1 for c in cols if any(abs(a.x1 - c.right_edge) <= max(row_height * 1.5, c.spread * 4, 10)
                                           for a in amts))
    if len(hits) >= 3 and populated <= 1:
        return 'header repeat', '', ''
    if NON_EMPLOYEE.search(leftmost or text[:40]):
        return 'subtotal', leftmost, ''
    has_identity = bool(IDENTITY.search(leftmost)) and len(re.sub(r'[^A-Za-z]', '', leftmost)) >= 3
    if has_identity and populated >= 2:
        return 'employee', leftmost, (id_words[0] if id_words else '')
    if not has_identity and populated >= 1 and prev_employee is not None:
        return 'continuation', '', ''
    if has_identity and populated <= 1:
        return 'unread', leftmost, (id_words[0] if id_words else '')
    return 'section', leftmost, ''


def read_table(words: List[Word], row_height=None) -> TableReading:
    """Read the page as a table of people. Returns is_table False when the evidence does not support it."""
    if not words:
        return TableReading(False, 0.0, [], [], dict(pages=1, rows_encountered=0), note='no words on the page')
    rows_map, h = row_bands(words, row_height)
    all_amounts, per_row = amounts_from_words(words, h, rows_map)
    lines = [(k, sorted(rows_map[k], key=lambda w: w.x0),
              ' '.join(w.text for w in sorted(rows_map[k], key=lambda w: w.x0)), per_row.get(k, []))
             for k in sorted(rows_map)]

    best = None
    for n, named in _header_candidates(lines, h):
        cols = _learn_columns(named, lines, n, h)
        if len(cols) < 3:
            continue
        # how many of the rows below actually populate these columns
        populated_rows = 0
        for key, ws, text, amts in lines[n + 1:n + 41]:
            hit = sum(1 for c in cols if any(abs(a.x1 - c.right_edge) <= max(h * 1.5, c.spread * 4, 10)
                                             for a in amts))
            if hit >= 3:
                populated_rows += 1
        score = min(1.0, len(cols) / 5) * 0.4 + min(1.0, populated_rows / 5) * 0.6
        if best is None or score > best[0]:
            best = (score, n, cols, populated_rows)

    if best is None or best[0] < 0.5 or best[3] < 2:
        return TableReading(False, (best[0] if best else 0.0), [], [],
                            dict(pages=1, rows_encountered=len(lines)),
                            note='no header with columns the rows below populate')

    score, hn, cols, _ = best
    out_rows, prev = [], None
    for n, (key, ws, text, amts) in enumerate(lines):
        if n <= hn:
            continue
        kind, ident, ident_id = _classify(ws, amts, cols, h, prev)
        row = Row(index=n, kind=kind, identity=ident, identity_id=ident_id, source_rows=[n])
        if kind in ('employee', 'continuation'):
            for c in cols:
                tol = max(h * 1.5, c.spread * 4, 10)
                near = [a for a in amts if abs(a.x1 - c.right_edge) <= tol]
                if not near:
                    continue
                pick = min(near, key=lambda a: abs(a.x1 - c.right_edge))
                row.values[c.name] = pick.value
                row.provenance[c.name] = pick.word_ids
        if kind == 'continuation' and prev is not None:
            for k, v in row.values.items():
                if k not in prev.values:
                    prev.values[k] = v
                    prev.provenance[k] = row.provenance.get(k, [])
            prev.source_rows.append(n)
            row.note = f'joined to the row above ({prev.identity})'
        if kind == 'employee':
            prev = row
        out_rows.append(row)

    counts = dict(pages=1, rows_encountered=len(out_rows))
    for k in ('employee', 'continuation', 'header repeat', 'subtotal', 'section', 'unread'):
        counts[k.replace(' ', '_')] = sum(1 for r in out_rows if r.kind == k)
    counts['accounted'] = sum(counts[k] for k in ('employee', 'continuation', 'header_repeat', 'subtotal',
                                                  'section', 'unread'))
    return TableReading(True, round(score, 3), cols, out_rows, counts, header_row=hn)


def coherence(reading: TableReading, tolerance=0.02):
    """Does the table reading hold together arithmetically: gross less the withheld amounts should equal net pay.

    This does not prove the figures are the right ones, and it is not evidence about the payroll. It is a check on
    the reading itself, which is the only thing available on a provider whose figures have not been verified.
    """
    rows = [r for r in reading.employees() if 'gross' in r.values and 'net_pay' in r.values]
    if not rows:
        return dict(rows=0, note='no row carries both a gross and a net pay')
    ok = off = 0
    worst = None
    for r in rows:
        withheld = sum(r.values.get(k, 0.0) for k in ('federal', 'social_security', 'medicare', 'state',
                                                      'retirement', 'premium', 'fee'))
        gap = round(r.values['gross'] - withheld - r.values['net_pay'], 2)
        if abs(gap) <= max(tolerance, 0.01):
            ok += 1
        else:
            off += 1
            if worst is None or abs(gap) > abs(worst[1]):
                worst = (r.identity, gap)
    return dict(rows=len(rows), ties=ok, does_not_tie=off,
                worst=(dict(row=worst[0], gap=worst[1]) if worst else None))
