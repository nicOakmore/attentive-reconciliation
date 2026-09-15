"""A store of what each page was read as, so a page read once is not read again and a correction survives.

Three rules, taken from the review this design went through:

  1. A cached reading is applied only where the page's identity is established. An ambiguous identity is a miss.
     Reading a page twice costs a few seconds; applying page A's figures to page B corrupts a reconciliation.
  2. The machine reading is immutable. A human correction is appended as its own entry and the effective value is
     the latest correction, so a later run can still say what the machine saw, who changed it and why.
  3. The reading is versioned. A page read by an older extractor is re-read by a newer one, and the corrections
     recorded against that page stay in force.

Identity is the sha256 of the page rendered deterministically to a small grayscale image, together with the
renderer, render and reader versions. Hashing the page re-serialised as its own PDF was tried first and is wrong:
the writer stamps a fresh document id each time, so the same page hashed differently on every run. A re-export
whose render differs falls back to a second test: the same identity tokens printed on the page (an employee
number, a date), the same page size, and a perceptual hash within a small distance. A perceptual hash is never
used on its own, because different pages are allowed to share one.
"""
import os, io, json, hashlib, time, re, tempfile
from typing import Optional

RENDER_VERSION = '2'          # bump when the page rendering changes
SCHEMA = '2'
KEY_SCALE = 1.5               # the page is rendered at this scale to be hashed: small, deterministic, enough detail


def store_dir():
    """Where records live. A Render disk mounted at /var/data keeps them across deploys; otherwise they live in a
    working directory, which still saves the second pass over the same pack inside one container."""
    for p in (os.environ.get('PAGE_STORE_DIR'), '/var/data/pagestore',
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.pagestore'),
              os.path.join(tempfile.gettempdir(), 'pagestore')):
        if not p:
            continue
        try:
            os.makedirs(p, exist_ok=True)
            t = os.path.join(p, '.writable')
            with open(t, 'w') as f:
                f.write('1')
            os.unlink(t)
            return p
        except Exception:
            continue
    return tempfile.gettempdir()


def canonical_render(data: bytes, index: int, scale=KEY_SCALE) -> bytes:
    """The page as a deterministic grayscale image. This is the page's identity.

    Extracting the page as its own PDF and hashing those bytes looked tidier but is wrong: the writer stamps a
    fresh document id each time, so the same page hashed differently on every run and the store missed at random.
    A render of the page content is stable for a given renderer, and the renderer version is part of the key.
    """
    import pypdfium2 as pdfium
    from .parse_files import PDF_LOCK
    with PDF_LOCK:
        doc = pdfium.PdfDocument(io.BytesIO(data))
        try:
            bmp = doc[index].render(scale=scale, grayscale=True)
            im = bmp.to_pil()
            buf = io.BytesIO()
            im.save(buf, format='PNG', optimize=False, compress_level=1)
            im.close()
            return buf.getvalue()
        finally:
            doc.close()


def renderer_version() -> str:
    """Part of the page key: a different renderer can produce a different image from the same page."""
    try:
        from importlib import metadata
        return 'pypdfium2 ' + metadata.version('pypdfium2')
    except Exception:
        try:
            import pypdfium2 as pdfium
            return str(getattr(pdfium, 'V_PYPDFIUM2', 'unknown'))
        except Exception:
            return 'unknown'


def dhash(png: bytes, size=8) -> str:
    """A difference hash of the rendered page, kept as a corroborating signal only."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(png)) as im:
            g = im.convert('L').resize((size + 1, size), Image.LANCZOS)
            px = list(g.getdata())
        bits = []
        for row in range(size):
            for col in range(size):
                left = px[row * (size + 1) + col]
                right = px[row * (size + 1) + col + 1]
                bits.append('1' if left > right else '0')
        return f'{int("".join(bits), 2):016x}'
    except Exception:
        return ''


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return bin(int(a, 16) ^ int(b, 16)).count('1')


IDENT_PATTERNS = (
    r'\b(?:emp(?:loyee)?\s*(?:nbr|no|number|id|#))\s*[:.]?\s*([A-Za-z0-9\-]{2,16})\b',
    r'\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b',
    r'\b(\d{4}-\d{2}-\d{2})\b',
)


def identity_tokens(text: str):
    """Tokens that identify the page to a reader: a person or record number, and the dates printed on it. Generic
    on purpose: any statement pack carries something of this shape, and a pack that carries none falls back to the
    page bytes alone."""
    out = []
    low = (text or '')
    for pat in IDENT_PATTERNS:
        for m in re.finditer(pat, low, re.I):
            t = m.group(1).strip().lower()
            if t and t not in out:
                out.append(t)
    return sorted(out)[:8]


def _sha(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
        h.update(b'\x1f')
    return h.hexdigest()


def page_key(data: bytes, index: int, extractor_version: str) -> str:
    """The identity of a page: what it looks like, under this renderer and this reader."""
    png = canonical_render(data, index)
    return _sha(hashlib.sha256(png).hexdigest(), RENDER_VERSION, renderer_version(), extractor_version, SCHEMA)


def _path(key: str) -> str:
    d = os.path.join(store_dir(), key[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, key + '.json')


def load(key: str) -> Optional[dict]:
    p = _path(key)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def save(record: dict) -> str:
    key = record['page_key']
    tmp = _path(key) + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(record, f)
    os.replace(tmp, _path(key))
    _index(record)
    return key


def _index_path():
    return os.path.join(store_dir(), 'index.jsonl')


def _index(record: dict):
    """A small side index so a re-export of the same page can be recognised without opening every record."""
    row = dict(page_key=record['page_key'], dhash=record.get('dhash', ''),
               tokens=record.get('identity_tokens', []), dims=record.get('dims', []),
               extractor_version=record.get('extractor_version', ''), saved=record.get('saved', ''))
    with open(_index_path(), 'a') as f:
        f.write(json.dumps(row) + '\n')


def find_equivalent(dh: str, tokens, dims, extractor_version: str, max_distance=4):
    """The same page arriving with different bytes. Every test must agree: the identity tokens printed on the page,
    the page size, the extractor version, and a perceptual hash within a small distance. Anything less is a miss."""
    if not dh or not tokens:
        return None
    p = _index_path()
    if not os.path.exists(p):
        return None
    best = None
    try:
        with open(p) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('extractor_version') != extractor_version:
                    continue
                if sorted(row.get('tokens') or []) != sorted(tokens):
                    continue
                if list(row.get('dims') or []) != list(dims):
                    continue
                d = hamming(row.get('dhash', ''), dh)
                if d <= max_distance and (best is None or d < best[0]):
                    best = (d, row['page_key'])
    except Exception:
        return None
    if not best:
        return None
    rec = load(best[1])
    if rec is None:
        return None
    rec = dict(rec)
    rec['match'] = dict(kind='equivalent', perceptual_distance=best[0])
    return rec


def new_record(page_key_: str, source_name: str, index: int, source_sha: str, dh: str, dims, tokens,
               engine: str, extractor_version: str, reading: dict, ocr_version: str = '') -> dict:
    return dict(schema=SCHEMA, page_key=page_key_, source_file=source_name, source_sha256=source_sha,
                source_page_number=index + 1, dhash=dh, dims=list(dims), identity_tokens=tokens,
                ocr_engine=engine, ocr_version=ocr_version, renderer_version=renderer_version(),
                render_version=RENDER_VERSION, extractor_version=extractor_version,
                reading=reading, corrections=[], saved=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


def effective_fields(record: dict) -> dict:
    """The machine reading with any corrections applied on top. The machine values stay in the record untouched."""
    fields = dict((record.get('reading') or {}).get('fields') or {})
    out = {}
    for name, f in fields.items():
        val = f.get('value')
        entry = dict(machine_value=val, effective_value=val, verification='machine',
                     method=f.get('method'), confidence=f.get('confidence'), note=f.get('note'),
                     block=f.get('block'), column=f.get('column'), word_ids=f.get('word_ids'))
        out[name] = entry
    for c in record.get('corrections') or []:
        name = c.get('field')
        if not name:
            continue
        e = out.setdefault(name, dict(machine_value=None, effective_value=None, verification='machine'))
        e['effective_value'] = c.get('value')
        e['verification'] = 'human'
        e['correction'] = dict(user=c.get('user'), reason=c.get('reason'), at=c.get('at'),
                               previous=e.get('machine_value'))
    return out


def add_correction(page_key_: str, field_name: str, value, user: str, reason: str):
    """Record a human correction against this page and field. Never touches the machine reading."""
    rec = load(page_key_)
    if rec is None:
        return None
    rec.setdefault('corrections', []).append(
        dict(field=field_name, value=value, user=user or 'unknown', reason=reason or '',
             at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    save(rec)
    return rec


def stats_summary():
    p = _index_path()
    n = 0
    if os.path.exists(p):
        with open(p) as f:
            n = sum(1 for _ in f)
    return dict(directory=store_dir(), indexed_pages=n)
