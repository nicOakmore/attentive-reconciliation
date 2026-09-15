"""Figures that are not crisp, and conclusions that say so.

Reading a scan does not always give a number. It gives a number the page probably says, a couple of alternatives,
and sometimes only a range the other documents make plausible. Forcing that into one figure loses what the audit
most needs to communicate, and dropping the employee altogether wastes evidence that is good enough to act on.

So an uncertain figure is carried as a triangular fuzzy number: the value the page most likely prints, and the
lowest and highest it could reasonably be. The arithmetic of the reconciliation is done at several levels of
confidence (alpha cuts), which gives a conclusion with a degree attached: the gap is this, with this much support
in the reading. Crisp figures stay crisp, and every fuzzy result is labelled as modelled.
"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Fuzzy:
    """A triangular fuzzy number: low is the least it could be, mid the most likely, high the most it could be."""
    low: float
    mid: float
    high: float

    @staticmethod
    def crisp(v):
        return Fuzzy(v, v, v)

    @property
    def is_crisp(self):
        return abs(self.high - self.low) < 1e-9

    @property
    def spread(self):
        return self.high - self.low

    def at(self, alpha):
        """The interval of values whose membership is at least alpha."""
        a = max(0.0, min(1.0, alpha))
        return (self.low + (self.mid - self.low) * a, self.high - (self.high - self.mid) * a)

    def membership(self, v):
        if v <= self.low or v >= self.high:
            return 0.0
        if v == self.mid:
            return 1.0
        if v < self.mid:
            return (v - self.low) / max(self.mid - self.low, 1e-9)
        return (self.high - v) / max(self.high - self.mid, 1e-9)

    def __add__(self, o):
        o = o if isinstance(o, Fuzzy) else Fuzzy.crisp(float(o))
        return Fuzzy(self.low + o.low, self.mid + o.mid, self.high + o.high)

    def __sub__(self, o):
        o = o if isinstance(o, Fuzzy) else Fuzzy.crisp(float(o))
        return Fuzzy(self.low - o.high, self.mid - o.mid, self.high - o.low)

    def __mul__(self, k):
        k = float(k)
        lo, hi = sorted((self.low * k, self.high * k))
        return Fuzzy(lo, self.mid * k, hi)

    __rmul__ = __mul__

    def as_dict(self):
        return dict(low=round(self.low, 2), mid=round(self.mid, 2), high=round(self.high, 2))


def from_posterior(observed, posterior, floor=0.02):
    """A fuzzy number from the candidate readings of one figure.

    The most likely reading is the peak; the alternatives that carry real probability set the bounds. A figure with
    no credible alternative comes back crisp, which is the common case and must stay cheap.
    """
    if not posterior:
        return Fuzzy.crisp(observed)
    vals = [p['value'] for p in posterior if p.get('probability', 0) >= 0.05] or [observed]
    mid = posterior[0]['value'] if posterior[0].get('probability', 0) >= 0.3 else observed
    lo, hi = min(vals + [observed]), max(vals + [observed])
    if hi - lo < floor:
        return Fuzzy.crisp(observed)
    return Fuzzy(lo, mid, hi)


def support_for(value: Fuzzy, threshold=0.0, direction='below'):
    """How much the reading supports a statement about the conclusion, in [0, 1].

    The degree is the largest alpha at which every value in the alpha cut satisfies the statement, which is the
    standard necessity measure: a degree of one means the reading cannot be read any other way, a degree of zero
    means the reading does not support the statement at all.
    """
    for i in range(100, -1, -1):
        a = i / 100.0
        lo, hi = value.at(a)
        ok = (hi < threshold) if direction == 'below' else (lo > threshold)
        if ok:
            return round(a, 2)
    return 0.0


def confidence(value: Fuzzy, tolerance=1.0):
    """How crisp a conclusion is: one when the reading pins it to within the tolerance, falling away as the spread
    grows. Reported alongside the figure so a reader can see how much of the conclusion is reading uncertainty."""
    if value.is_crisp:
        return 1.0
    return round(max(0.0, min(1.0, tolerance / max(value.spread, 1e-9))), 2)
