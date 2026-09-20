"""Parse Blinkit's free-text pack strings into normalized physical quantities.

Blinkit's `unit` field is human copy, not data: "2 x 100 g", "1 pair",
"500 ml (Pack of 2)", "1 kg", "6 pcs". Everything downstream that touches
money or freight needs real numbers, because shipping is billed on weight
and duty is assessed on value per unit.

The contract:
    parse_pack("2 x 100 g") -> Pack(net_g=200.0, multiplier=2, unit_qty=100.0, ...)

Failure is explicit: an unparseable pack yields Pack(confidence="none"),
which the shippability engine treats as a REVIEW trigger rather than
silently assuming a weight and under-quoting freight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional

# Mass/volume conversion to the two canonical units we keep: grams and millilitres.
_MASS_TO_G = {
    "g": 1.0, "gm": 1.0, "gms": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kgs": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "mg": 0.001,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592,
    "oz": 28.3495, "ounce": 28.3495,
}
_VOLUME_TO_ML = {
    "ml": 1.0, "millilitre": 1.0, "milliliter": 1.0,
    "l": 1000.0, "ltr": 1000.0, "litre": 1000.0, "liter": 1000.0, "lt": 1000.0,
}
# Countable units: no intrinsic mass, so these need a density/weight assumption
# supplied elsewhere (rules/shipping.yaml category defaults).
_PIECE_WORDS = {
    "pc", "pcs", "piece", "pieces", "unit", "units", "no", "nos",
    "pair", "pairs", "set", "sets", "pack", "packs", "packet", "packets",
    "box", "boxes", "bottle", "bottles", "can", "cans", "jar", "jars",
    "sachet", "sachets", "tablet", "tablets", "capsule", "capsules",
    "strip", "strips", "roll", "rolls", "sheet", "sheets", "bar", "bars",
    "combo", "kit", "dozen", "tube", "tubes", "pouch", "pouches",
    # Product-specific count words Blinkit uses as the unit of sale.
    "wipe", "wipes", "pull", "pulls", "tab", "tabs", "cap", "caps",
    "napkin", "napkins", "pad", "pads", "diaper", "diapers", "stick", "sticks",
    "leaf", "leaves", "ply", "bag", "bags", "cone", "cones", "gm.", "n",
    "sheet", "sheets", "wick", "wicks", "candle", "candles", "page", "pages",
}
# A "pair" is two physical objects; a "dozen" is twelve. Everything else in
# _PIECE_WORDS counts as one object per unit of quantity.
_PIECE_FACTOR = {"pair": 2, "pairs": 2, "dozen": 12}

_NUM = r"(\d+(?:\.\d+)?)"


@dataclass
class Pack:
    """Normalized physical description of one sellable pack."""

    raw: str
    net_g: Optional[float] = None       # total net mass of the pack, grams
    net_ml: Optional[float] = None      # total net volume of the pack, millilitres
    pieces: Optional[int] = None        # total countable items in the pack
    multiplier: int = 1                 # the "2" in "2 x 100 g"
    unit_qty: Optional[float] = None    # the "100" in "2 x 100 g"
    unit_uom: Optional[str] = None      # canonical uom: g | ml | pc
    piece_word: Optional[str] = None    # the literal count word ("pulls", "pcs")
    confidence: str = "none"            # high | medium | none

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def billable_g(self) -> Optional[float]:
        """Mass used for freight. Volume is converted at water density (1 ml = 1 g).

        That is an approximation, but it errs the safe way for the liquids we
        actually ship (sauces, oils, shampoos all sit between 0.9 and 1.1).
        """
        if self.net_g is not None:
            return self.net_g
        if self.net_ml is not None:
            return self.net_ml * 1.0
        return None


def _canon_uom(token: str) -> Optional[str]:
    t = token.lower().strip(" .")
    if t in _MASS_TO_G:
        return "g"
    if t in _VOLUME_TO_ML:
        return "ml"
    if t in _PIECE_WORDS:
        return "pc"
    return None


def _to_base(qty: float, token: str) -> tuple[Optional[str], float]:
    """Convert (qty, raw uom token) into (canonical uom, base-unit quantity)."""
    t = token.lower().strip(" .")
    if t in _MASS_TO_G:
        return "g", qty * _MASS_TO_G[t]
    if t in _VOLUME_TO_ML:
        return "ml", qty * _VOLUME_TO_ML[t]
    if t in _PIECE_WORDS:
        return "pc", qty * _PIECE_FACTOR.get(t, 1)
    return None, qty


def parse_pack(raw: Optional[str]) -> Pack:
    """Parse a Blinkit pack string. Never raises; returns confidence='none' instead."""
    if not raw or not str(raw).strip():
        return Pack(raw=raw or "")

    s = str(raw).strip().lower()
    # Normalize the several multiplication glyphs Blinkit uses interchangeably.
    s = s.replace("×", " x ").replace("*", " x ")
    s = re.sub(r"\s+", " ", s)

    multiplier = 1

    # "(pack of 3)" / "combo of 2" is a multiplier expressed as a suffix.
    m_packof = re.search(r"(?:pack|combo|set|box|pkt)\s+of\s+" + _NUM, s)
    if m_packof:
        multiplier = int(float(m_packof.group(1)))
        s = s[: m_packof.start()] + " " + s[m_packof.end():]
        s = s.strip(" ()-,")

    # "2 x 100 g" -> multiplier 2, then fall through to parse "100 g".
    m_mult = re.match(r"^" + _NUM + r"\s*x\s*(.+)$", s)
    if m_mult:
        multiplier *= int(float(m_mult.group(1)))
        s = m_mult.group(2).strip()

    # Trailing "x 2" form: "100 g x 2".
    m_tail = re.match(r"^(.+?)\s*x\s*" + _NUM + r"$", s)
    if m_tail:
        multiplier *= int(float(m_tail.group(2)))
        s = m_tail.group(1).strip()

    # Core "<number> <uom>" match, anchored at the start of what remains.
    m = re.match(r"^" + _NUM + r"\s*([a-z]+)", s)
    if not m:
        # Bare number with no uom ("6") reads as a piece count in this catalogue.
        m_bare = re.match(r"^" + _NUM + r"$", s)
        if m_bare:
            qty = float(m_bare.group(1))
            total = int(round(qty * multiplier))
            return Pack(raw=raw, pieces=total, multiplier=multiplier,
                        unit_qty=qty, unit_uom="pc", piece_word="pc",
                        confidence="medium")
        return Pack(raw=raw, multiplier=multiplier)

    qty = float(m.group(1))
    uom_raw = m.group(2)
    uom, base_qty = _to_base(qty, uom_raw)

    if uom is None:
        return Pack(raw=raw, multiplier=multiplier)

    # A trailing unrecognized word ("1 pc assorted") lowers confidence but the
    # quantity itself is still trustworthy.
    leftover = s[m.end():].strip(" .,()-")
    confidence = "high" if not leftover else "medium"

    pack = Pack(raw=raw, multiplier=multiplier, unit_qty=qty,
                unit_uom=uom, confidence=confidence,
                piece_word=uom_raw.lower().strip(" .") if uom == "pc" else None)
    if uom == "g":
        pack.net_g = base_qty * multiplier
    elif uom == "ml":
        pack.net_ml = base_qty * multiplier
    else:
        pack.pieces = int(round(base_qty * multiplier))
    return pack
