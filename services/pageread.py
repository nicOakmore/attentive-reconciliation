"""Read one page of any statement PDF by geometry.

The rule this module exists to enforce: a label identifies the row, the block identifies which side of the page,
and the x position identifies which column. Taking the first amount printed after a label is what put a premium
figure into a withholding line, because these pages carry two tables side by side and the neighbouring table's
figures sit on the same row.

Nothing here knows what a payroll statement is. The caller supplies label patterns; the page supplies its own
geometry: where the gutters are, where the amount columns are, and how tall a row is. A page whose geometry cannot
be established is reported as such rather than read on a guess.
"""
import io, os, re, math, statistics as stats
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict

EXTRACTOR_VERSION = '2026.09.15.4'      # bump when the reading changes; the store keys on it

NUM_RE = re.compile(r'^[\(\-\$]?\s*(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d{1,2})?\s*\)?[-]?$')
HAS_DIGIT = re.compile(r'\d')


@dataclass
class Word:
    id: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    @property
    def h(self):
        return self.y1 - self.y0

    @property
    def w(self):
        return self.x1 - self.x0


@dataclass
class Amount:
    value: float
    x0: float
    y0: float
    x1: float
    y1: float
    word_ids: List[int]
    text: str

    @property
    def right(self):
        return self.x1

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


@dataclass
class Column:
    index: int
    centre: float          # median right edge of the amounts in this column
    spread: float          # median absolute deviation of those right edges
    count: int


@dataclass
class Block:
    index: int
    x0: float
    x1: float
    columns: List[Column] = field(default_factory=list)


@dataclass
class FieldRead:
    value: Optional[float]
    label: str = ''
    label_word_ids: List[int] = field(default_factory=list)
    word_ids: List[int] = field(default_factory=list)
    block: Optional[int] = None
    column: Optional[int] = None
    method: str = ''
    confidence: float = 0.0
    note: str = ''
    box: Optional[List[float]] = None


@dataclass
class PageReading:
    words: List[Word]
    blocks: List[Block]
    row_height: float
    gutters: List[Dict]
    gutter_confidence: float
    fields: Dict[str, FieldRead]
    text: str                       # the page as lines, for the label patterns that are still read from text
    source: str = ''
    diagnostics: Dict = field(default_factory=dict)

    def value(self, name):
        f = self.fields.get(name)
        return f.value if f else None

    def as_record(self):
        return dict(
            row_height=self.row_height, gutters=self.gutters, gutter_confidence=self.gutter_confidence,
            blocks=[dict(index=b.index, x0=b.x0, x1=b.x1,
                         columns=[asdict(c) for c in b.columns]) for b in self.blocks],
            words=[asdict(w) for w in self.words],
            fields={k: asdict(v) for k, v in self.fields.items()},
            diagnostics=self.diagnostics, extractor_version=EXTRACTOR_VERSION)


# ------------------------------------------------------------------ number handling
def parse_amount(text):
    """A printed amount, or None. Accepts a bare .00, a parenthesised negative and a trailing minus."""
    t = (text or '').strip().replace('$', '').replace(' ', '')
    if not t or not HAS_DIGIT.search(t):
        return None
    neg = t.startswith('(') and t.endswith(')') or t.endswith('-') or t.startswith('-')
    t = t.strip('()-').replace(',', '')
    if not re.fullmatch(r'\d*(?:\.\d{1,2})?', t) or t in ('', '.'):
        return None
    try:
        v = float(t or 0)
    except ValueError:
        return None
    return -v if neg else v


def _numberish(word):
    t = word.text.strip()
    return bool(HAS_DIGIT.search(t)) and bool(NUM_RE.match(t.replace(' ', '')))


# ------------------------------------------------------------------ geometry
def row_bands(words, row_height=None):
    """Group words into printed lines by the vertical centre of their boxes.

    Each word joins the nearest line whose centre is within half a line height, and starts a new line otherwise.
    Comparing against the line's own centre rather than against the previous word matters: a word by word
    comparison drifts down the page and swallows the next line, and a fixed grid splits a line that straddles a
    boundary. Both mistakes put an amount from one line against a label on another.
    """
    if not words:
        return {}, 10.0
    h = row_height or stats.median([w.h for w in words]) or 10.0
    tol = max(3.0, h * 0.45)
    rows = []                                    # each: dict(centre, words)
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        best, best_d = None, None
        for r in rows:
            d = abs(w.cy - r['centre'])
            if d <= tol and (best_d is None or d < best_d):
                best, best_d = r, d
        if best is None:
            rows.append(dict(centre=w.cy, words=[w]))
        else:
            best['words'].append(w)
            best['centre'] = stats.median([x.cy for x in best['words']])
    rows.sort(key=lambda r: r['centre'])
    return {i: r['words'] for i, r in enumerate(rows)}, h


def occupancy(words, page_x0, page_x1, bins=400):
    """How much of the page's vertical extent is occupied at each x. Used to find the gutters between blocks."""
    width = max(1.0, page_x1 - page_x0)
    step = width / bins
    prof = [0.0] * bins
    rows, _ = row_bands(words)
    for key, ws in rows.items():
        for w in ws:
            a = int((w.x0 - page_x0) / step)
            b = int((w.x1 - page_x0) / step)
            for i in range(max(0, a), min(bins - 1, b) + 1):
                prof[i] += 1.0
    return prof, step


def _smooth(prof, k=5):
    out = []
    for i in range(len(prof)):
        lo, hi = max(0, i - k), min(len(prof), i + k + 1)
        out.append(sum(prof[lo:hi]) / (hi - lo))
    return out


def find_gutters(words, page_x0, page_x1, min_width_frac=0.02, min_rows_frac=0.30):
    """Vertical valleys with occupied material on both sides, spanning a large part of the page.

    A valley that spans only a few rows is an indent or a gap inside one table, not a gutter between two tables,
    which is why the vertical coverage test matters more than the width of the gap.
    """
    if len(words) < 20:
        return [], 0.0
    prof, step = occupancy(words, page_x0, page_x1)
    sm = _smooth(prof)
    peak = max(sm) or 1.0
    width = page_x1 - page_x0
    rows, h = row_bands(words)
    n_rows = max(1, len(rows))

    runs, i = [], 0
    while i < len(sm):
        if sm[i] <= peak * 0.06:
            j = i
            while j + 1 < len(sm) and sm[j + 1] <= peak * 0.06:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1

    out = []
    for a, b in runs:
        gx0, gx1 = page_x0 + a * step, page_x0 + (b + 1) * step
        if (gx1 - gx0) < width * min_width_frac:
            continue
        left = [w for w in words if w.x1 <= gx0]
        right = [w for w in words if w.x0 >= gx1]
        if len(left) < 10 or len(right) < 10:
            continue                                  # a margin, not a gutter
        straddling = sum(1 for ws in rows.values()
                         if any(w.x1 <= gx0 for w in ws) and any(w.x0 >= gx1 for w in ws))
        coverage = straddling / n_rows
        if coverage < min_rows_frac:
            continue                                  # an indent inside one table
        density_contrast = 1.0 - (sum(sm[a:b + 1]) / max(1, b - a + 1)) / peak
        conf = 0.5 * min(1.0, coverage / 0.6) + 0.3 * density_contrast + 0.2 * min(1.0, (gx1 - gx0) / (width * 0.08))
        out.append(dict(x0=gx0, x1=gx1, coverage=round(coverage, 3),
                        density_contrast=round(density_contrast, 3), confidence=round(conf, 3)))
    out.sort(key=lambda g: -g['confidence'])
    keep = [g for g in out if g['confidence'] >= 0.55][:3]
    keep.sort(key=lambda g: g['x0'])
    return keep, (max((g['confidence'] for g in keep), default=0.0))


def amounts_from_words(words, row_height, rows=None):
    """Merge adjacent numeric words into one amount, keeping the word ids so the figure can be traced back.
    Amounts are kept per printed line, because an amount belongs to the line it is printed on and to no other."""
    if rows is None:
        rows, h = row_bands(words, row_height)
    h = row_height
    per_row, out = {}, []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w.x0)
        before = len(out)
        run = []
        for w in ws:
            if _numberish(w) or (run and w.text.strip() in (',', '.', '-')):
                if run and w.x0 - run[-1].x1 > h * 0.6:
                    _close(run, out)
                    run = [w]
                else:
                    run.append(w)
            else:
                _close(run, out)
                run = []
        _close(run, out)
        per_row[key] = out[before:]
    return out, per_row


def _close(run, out):
    if not run:
        return
    text = ''.join(w.text.strip() for w in run)
    v = parse_amount(text)
    if v is None and len(run) > 1:                     # the run merged two columns: take the last word alone
        v = parse_amount(run[-1].text)
        if v is not None:
            run = [run[-1]]
            text = run[0].text.strip()
    if v is None:
        return
    out.append(Amount(value=v, x0=min(w.x0 for w in run), y0=min(w.y0 for w in run),
                      x1=max(w.x1 for w in run), y1=max(w.y1 for w in run),
                      word_ids=[w.id for w in run], text=text))


def column_model(amounts, row_height):
    """The amount columns of a block, from the right edges of its amounts, because money is printed right aligned."""
    if len(amounts) < 3:
        return []
    edges = sorted(a.right for a in amounts)
    gap = max(row_height * 1.2, 8.0)
    clusters, cur = [], [edges[0]]
    for e in edges[1:]:
        if e - cur[-1] <= gap:
            cur.append(e)
        else:
            clusters.append(cur); cur = [e]
    clusters.append(cur)
    cols = []
    for i, c in enumerate(sorted(clusters, key=lambda c: stats.median(c))):
        if len(c) < 2:
            continue
        m = stats.median(c)
        cols.append(Column(index=len(cols), centre=m,
                           spread=stats.median([abs(x - m) for x in c]) if len(c) > 1 else 0.0, count=len(c)))
    return cols


def blocks_from_gutters(words, gutters, page_x0, page_x1):
    bounds = [page_x0] + [g['x0'] for g in gutters] + [page_x1]
    rights = [g['x1'] for g in gutters]
    out = []
    for i in range(len(gutters) + 1):
        x0 = bounds[0] if i == 0 else rights[i - 1]
        x1 = bounds[i + 1] if i < len(gutters) else page_x1
        out.append(Block(index=i, x0=x0, x1=x1))
    return out


# ------------------------------------------------------------------ the reading itself
def read_page(words, label_patterns, page_x0=None, page_x1=None, prefer_column=0, source=''):
    """Read every requested field from one page.

    label_patterns maps a field name to a list of regular expressions matched against the text of a row. The row
    that matches gives the row band and the label's own x extent; the amount taken is the one in the same block
    whose right edge sits closest to that block's column `prefer_column`, provided it lies within a column's width
    of that centre. Anything else is reported unread with the reason.
    """
    if not words:
        return PageReading([], [], 10.0, [], 0.0, {}, '', source, dict(reason='no words on the page'))
    page_x0 = min(w.x0 for w in words) if page_x0 is None else page_x0
    page_x1 = max(w.x1 for w in words) if page_x1 is None else page_x1
    rows, h = row_bands(words)
    gutters, gconf = find_gutters(words, page_x0, page_x1)
    blocks = blocks_from_gutters(words, gutters, page_x0, page_x1)
    all_amounts, per_row = amounts_from_words(words, h, rows)
    for b in blocks:
        inside = [a for a in all_amounts if a.x0 >= b.x0 - 1 and a.x1 <= b.x1 + 1]
        b.columns = column_model(inside, h)

    lines = []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w.x0)
        # The text form of the page marks the column breaks, because the patterns that read it need to know where
        # one column ends and the next begins: a name followed by the next column's heading is not a name.
        parts, prev = [], None
        for w in ws:
            if prev is not None and (w.x0 - prev) > h * 1.6:
                parts.append('|')
            parts.append(w.text.strip())
            prev = w.x1
        lines.append((key, ws, ' '.join(parts), per_row.get(key, [])))
    text = '\n'.join(l[2] for l in lines)

    cands = {name: _candidates(name, pats, lines, blocks, h)
             for name, pats in label_patterns.items()}
    fields = _assign_by_consensus(cands, h, prefer_column)

    diag = dict(n_words=len(words), n_amounts=len(all_amounts), n_blocks=len(blocks),
                row_height=round(h, 2), page_x0=round(page_x0, 1), page_x1=round(page_x1, 1),
                columns_per_block=[[asdict(c) for c in b.columns] for b in blocks])
    return PageReading(words, blocks, h, gutters, gconf, fields, text, source, diag)


def _block_of(x, blocks):
    for b in blocks:
        if b.x0 - 1 <= x <= b.x1 + 1:
            return b
    return None


def label_runs(ws, row_height):
    """Every label on the row, not just the first. A row crosses several tables, so it carries several labels: the
    tax label at the left and the deduction plan name further along. Each run ends at an amount or a wide gap, and
    each is a candidate label with its own right edge, which is what decides where its figure should sit."""
    runs, cur = [], []
    for w in ws:
        if _numberish(w):
            if cur:
                runs.append(cur); cur = []
            continue
        if cur and (w.x0 - cur[-1].x1) > row_height * 2.5:
            runs.append(cur); cur = []
        cur.append(w)
    if cur:
        runs.append(cur)
    return [r for r in runs if any(re.search(r'[A-Za-z]{2}', w.text) for w in r)]


def _candidates(name, pats, lines, blocks, row_height):
    """Find the label and every amount printed on the label's own line after it, without choosing between them."""
    for key, ws, line, row_amounts in lines:
        low = line.lower()
        compact = re.sub(r'[^a-z0-9]', '', low)
        if not any(re.search(p, low) or re.search(p, compact) for p in pats):
            continue
        label_words = None
        for run in label_runs(ws, row_height):
            t = ' '.join(w.text for w in run).lower()
            if any(re.search(p, t) or re.search(p, re.sub(r'[^a-z0-9]', '', t)) for p in pats):
                label_words = run
                break
        if not label_words:
            continue
        lx0, lx1 = min(w.x0 for w in label_words), max(w.x1 for w in label_words)
        band_lo, band_hi = min(w.y0 for w in ws), max(w.y1 for w in ws)
        blk = _block_of(lx0, blocks)
        rowc = [a for a in row_amounts
                if (blk is None or (a.x0 >= blk.x0 - 1 and a.x1 <= blk.x1 + 1))
                and a.x0 >= lx1 - row_height]
        return dict(label=line[:60], label_words=[w.id for w in label_words], lx0=lx0, lx1=lx1,
                    block=blk.index if blk else None, amounts=rowc)
    return None


def _assign_by_consensus(cands, row_height, prefer_column):
    """Decide which amount belongs to each label by the alignment the page itself shows.

    Labels that start at the same x belong to the same table. Within such a group, the figures belonging to those
    labels line up in one column, so the column is the x at which the largest number of those labels have an
    amount. A label with no amount at that x is reported unread: the amount printed further along its row belongs
    to the table alongside, not to this label.
    """
    out = {}
    groups = {}
    for name, c in cands.items():
        if not c:
            out[name] = FieldRead(None, method='label_row_block_column', note='label not found on the page')
            continue
        placed = False
        for gx in list(groups):
            if abs(gx - c['lx0']) <= row_height * 3:
                groups[gx].append((name, c)); placed = True; break
        if not placed:
            groups[c['lx0']] = [(name, c)]

    for gx, members in groups.items():
        edges = sorted({round(a.right, 1) for _, c in members for a in c['amounts']})
        tol = max(row_height * 1.5, 10.0)
        scored = [(e, sum(1 for _, c in members if any(abs(a.right - e) <= tol for a in c['amounts'])))
                  for e in edges]
        top = max((sc for _, sc in scored), default=0)
        # The figure belonging to a label is in the first of its table's columns; the year to date column beside it
        # usually carries more amounts, so the densest column is not the right one. Take the leftmost column that
        # still accounts for most of this table's labels.
        strong = [e for e, sc in scored if sc >= max(2, 0.75 * top)]
        best_centre, best_score = (min(strong) if strong else None), top
        if best_centre is not None and prefer_column > 0:
            later = sorted({e for e in edges if e > best_centre + tol})
            for _ in range(prefer_column):
                if not later:
                    break
                nxt, later = later[0], [e for e in later if e > later[0] + tol]
                best_centre = nxt
        for name, c in members:
            if best_centre is None:
                out[name] = FieldRead(None, label=c['label'], block=c['block'],
                                      method='label_row_block_column',
                                      note='no amount is printed after any label of this table')
                continue
            near = [a for a in c['amounts'] if abs(a.right - best_centre) <= tol]
            if not near:
                out[name] = FieldRead(None, label=c['label'], block=c['block'],
                                      method='label_row_block_column',
                                      note=f'this row prints no amount in the column the other figures of its own '
                                           f'table line up in; the nearest amount on the row is '
                                           f'{min((abs(a.right - best_centre) for a in c["amounts"]), default=0):.0f} '
                                           f'away, tolerance {tol:.0f}')
                continue
            pick = min(near, key=lambda a: abs(a.right - best_centre))
            dist = abs(pick.right - best_centre)
            out[name] = FieldRead(pick.value, label=c['label'], label_word_ids=c['label_words'],
                                  word_ids=pick.word_ids, block=c['block'], column=0,
                                  method='label_row_block_column',
                                  confidence=round(max(0.4, 1.0 - dist / max(tol, 1.0)), 3),
                                  box=[pick.x0, pick.y0, pick.x1, pick.y1])
    return out


def _read_field(name, pats, lines, amounts, blocks, row_height, prefer_column, window=6):
    """Find the label, then take the amount that sits in the label's own column.

    The column model is built from the rows around the label rather than from the whole page, because one page
    carries several tables and a header, and each has its own columns. Within that neighbourhood the label's column
    is the first amount column printed after the label. If this row has no amount in that column, the figure is
    reported unread: an amount from the table alongside is not this label's figure.
    """
    best = None
    keys = [l[0] for l in lines]
    for n, (key, ws, line) in enumerate(lines):
        low = line.lower()
        compact = re.sub(r'[^a-z0-9]', '', low)
        if not any(re.search(p, low) or re.search(p, compact) for p in pats):
            continue
        label_words = None
        for run in label_runs(ws, row_height):
            t = ' '.join(w.text for w in run).lower()
            if any(re.search(p, t) or re.search(p, re.sub(r'[^a-z0-9]', '', t)) for p in pats):
                label_words = run
                break
        if not label_words:
            continue                      # the pattern matched the row but no label run on it
        lx1 = max(w.x1 for w in label_words)
        lx0 = min(w.x0 for w in label_words)
        band_lo = min(w.y0 for w in ws)
        band_hi = max(w.y1 for w in ws)
        blk = _block_of(lx0, blocks)
        in_block = lambda a: blk is None or (a.x0 >= blk.x0 - 1 and a.x1 <= blk.x1 + 1)

        row_cands = [a for a in amounts
                     if band_lo - row_height * 0.4 <= a.cy <= band_hi + row_height * 0.4
                     and in_block(a) and a.x0 >= lx1 - row_height]
        near_keys = set(keys[max(0, n - window):n + window + 1])
        y_lo = min((min(w.y0 for w in l[1]) for l in lines if l[0] in near_keys), default=band_lo)
        y_hi = max((max(w.y1 for w in l[1]) for l in lines if l[0] in near_keys), default=band_hi)
        near_amounts = [a for a in amounts if y_lo - 1 <= a.cy <= y_hi + 1 and in_block(a) and a.x1 > lx1]
        cols = column_model(near_amounts, row_height)
        if not row_cands:
            best = best or FieldRead(None, label=line[:60], block=blk.index if blk else None,
                                     method='label_row_block_column',
                                     note='the label was found but no amount is printed after it on that row')
            continue
        if len(cols) > prefer_column:
            col = cols[prefer_column]
            tol = max(row_height * 1.5, col.spread * 4, 10.0)
            pick = min(row_cands, key=lambda a: abs(a.right - col.centre))
            dist = abs(pick.right - col.centre)
            if dist > tol:
                best = best or FieldRead(None, label=line[:60], block=blk.index if blk else None,
                                         column=col.index, method='label_row_block_column',
                                         note=f'this row prints no amount in the column its own table uses '
                                              f'(nearest amount is {dist:.0f} from the column, tolerance {tol:.0f})')
                continue
            return FieldRead(pick.value, label=line[:60], label_word_ids=[w.id for w in label_words],
                             word_ids=pick.word_ids, block=blk.index if blk else None, column=col.index,
                             method='label_row_block_column',
                             confidence=round(max(0.4, 1.0 - dist / max(tol, 1.0)), 3),
                             box=[pick.x0, pick.y0, pick.x1, pick.y1])
        pick = min(row_cands, key=lambda a: a.x0)
        return FieldRead(pick.value, label=line[:60], label_word_ids=[w.id for w in label_words],
                         word_ids=pick.word_ids, block=blk.index if blk else None,
                         method='label_row_first_amount', confidence=0.5,
                         note='the rows around this one print too few amounts to establish a column, so the first '
                              'amount after the label was taken',
                         box=[pick.x0, pick.y0, pick.x1, pick.y1])
    return best or FieldRead(None, method='label_row_block_column', note='label not found on the page')


# ------------------------------------------------------------------ word sources
def words_from_text_layer(data: bytes, index: int):
    """A born digital page: its own word boxes are exact and cost nothing, so they are used when the text layer is
    good. A bad text layer (a scan with a sprinkling of characters) is rejected in favour of OCR."""
    try:
        import pdfplumber
    except Exception:
        return None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if index >= len(pdf.pages):
                return None
            page = pdf.pages[index]
            ws = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if len(ws) < 40:
                return None
            alnum = sum(1 for w in ws if re.search(r'[A-Za-z0-9]', w['text']))
            if alnum / max(1, len(ws)) < 0.8:
                return None
            return [Word(id=i, text=w['text'], x0=float(w['x0']), y0=float(w['top']),
                         x1=float(w['x1']), y1=float(w['bottom']), conf=1.0) for i, w in enumerate(ws)]
    except Exception:
        return None


def words_from_ocr(png: bytes):
    """A scanned page: the OCR engine's own word boxes. RapidOCR first, tesseract second."""
    from . import parse_files as P
    eng = P._rapid_engine()
    if eng is not None:
        import numpy as np
        from PIL import Image
        with Image.open(io.BytesIO(png)) as im:
            arr = np.array(im.convert('RGB'))
        res, _ = eng(arr)
        out = []
        for i, item in enumerate(res or []):
            box, txt, score = item[0], item[1], (item[2] if len(item) > 2 else 1.0)
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            out.append(Word(id=i, text=(txt or '').strip(), x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys),
                            conf=float(score or 0)))
        if out:
            return _split_wide_words(out)
    try:
        import pytesseract
        from PIL import Image
        with Image.open(io.BytesIO(png)) as im:
            d = pytesseract.image_to_data(im, config=P.TESS_CONFIG, output_type=pytesseract.Output.DICT)
        out = []
        for i, txt in enumerate(d.get('text', [])):
            if not (txt or '').strip():
                continue
            out.append(Word(id=len(out), text=txt.strip(), x0=float(d['left'][i]), y0=float(d['top'][i]),
                            x1=float(d['left'][i] + d['width'][i]), y1=float(d['top'][i] + d['height'][i]),
                            conf=float(d.get('conf', [0])[i] or 0) / 100.0))
        return out
    except Exception:
        return []


def _split_wide_words(words):
    """RapidOCR sometimes returns one box spanning a label and the figures beside it, and a box that crosses a
    gutter cannot be assigned to a block. Such a box is split on its spaces, apportioning the width by character
    count, which keeps every token inside one block. The split is recorded in the text so it stays traceable."""
    out, nid = [], 0
    for w in words:
        parts = [p for p in re.split(r'\s+', w.text.strip()) if p]
        if len(parts) <= 1 or w.w <= 0:
            out.append(Word(id=nid, text=w.text.strip(), x0=w.x0, y0=w.y0, x1=w.x1, y1=w.y1, conf=w.conf)); nid += 1
            continue
        total = sum(len(p) for p in parts) + (len(parts) - 1)
        x = w.x0
        for p in parts:
            frac = (len(p) + 1) / total
            span = w.w * frac
            out.append(Word(id=nid, text=p, x0=x, y0=w.y0, x1=min(w.x1, x + span * 0.98), y1=w.y1, conf=w.conf))
            nid += 1
            x += span
    return out


def line_structure_score(words):
    """How much this reading looks like lines of text: a correctly oriented page has many words per line and few
    lines relative to the page; a page read sideways has the opposite. No vocabulary, so it works on any document."""
    if not words:
        return 0.0
    rows, h = row_bands(words)
    if not rows:
        return 0.0
    per_row = sorted(len(r) for r in rows.values())
    med = per_row[len(per_row) // 2]
    multi = sum(1 for r in rows.values() if len(r) >= 4) / max(1, len(rows))
    return med + 4.0 * multi


def text_plausibility(words):
    """Whether the reading looks like language and money rather than noise. Distinguishes upright from upside down,
    which line structure cannot: a page rotated by 180 still has lines."""
    if not words:
        return 0.0
    letters = digits = ok = 0
    for w in words:
        t = w.text.strip()
        if not t:
            continue
        letters += sum(c.isalpha() for c in t)
        digits += sum(c.isdigit() for c in t)
        if re.fullmatch(r"[A-Za-z][A-Za-z'\-.]{2,}", t) or re.fullmatch(r'[\d,]+\.\d{2}', t):
            ok += 1
    return ok / max(1, len(words)) + 0.001 * (letters + digits)


def words_for_page(data: bytes, index: int, scale=None, prefer=None, reads_ok=None):
    """The words on a page, the right way up.

    Orientation is settled by whether the page actually reads. A structural probe was tried first and is wrong:
    a statement's column of figures becomes one long line when the page is turned, so "more words per line" scores
    the sideways reading higher than the upright one. Instead the page is read at the angle the pack has been using
    (upright to begin with) and only if the caller says that reading found nothing are the other angles tried.
    `reads_ok` is a callable given the words; it decides what "found nothing" means for the document at hand.
    """
    text_words = words_from_text_layer(data, index)
    if text_words:
        return text_words, 0, 'text layer'
    from .parse_files import pdf_page_png
    scale = scale or float(os.environ.get('OCR_SCALE', '2.4'))
    png = pdf_page_png(data, index, scale=scale)
    first = prefer if prefer in (0, 90, 180, 270) else 0
    ws = _words_rotated(png, first)
    if reads_ok is None or reads_ok(ws):
        return ws, first, 'OCR'
    best = (ws, first, text_plausibility(ws))
    for angle in (90, 180, 270, 0):
        if angle == first:
            continue
        alt = _words_rotated(png, angle)
        if reads_ok(alt):
            return alt, angle, f'OCR, page turned {angle} degrees to read it'
        p = text_plausibility(alt)
        if p > best[2]:
            best = (alt, angle, p)
    return best[0], best[1], 'OCR, no orientation read cleanly'


def _words_rotated(png, angle):
    from PIL import Image
    if angle % 360 == 0:
        return words_from_ocr(png)
    buf = io.BytesIO()
    with Image.open(io.BytesIO(png)) as im:
        im.rotate(-angle, expand=True).save(buf, 'PNG')
    return words_from_ocr(buf.getvalue())


def transpose_words(words):
    """Swap the axes of a page. A matrix that prints its labels down the left and one entity per column becomes,
    when transposed, a table with a header row and one entity per row, which is a shape the table reader handles.
    Widths and heights swap with the coordinates so the row and column statistics stay meaningful."""
    return [Word(id=w.id, text=w.text, x0=w.y0, y0=w.x0, x1=w.y1, y1=w.x1, conf=w.conf) for w in words]
