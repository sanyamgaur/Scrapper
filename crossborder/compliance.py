"""Listability engine: decide whether a Blinkit SKU may be listed on the US site.

Design commitments:

1. Deterministic first. A rule pack in YAML decides the vast majority of SKUs.
   The same input always yields the same verdict, and the verdict names the
   exact rule that produced it. When a parcel is held at the border you can
   point at a rule id and its citation instead of reconstructing a guess.

2. Unknown is not the same as safe. A SKU that matches nothing lands in
   REVIEW, never in ALLOWED. The catalogue grows by deliberate human clearance.

3. Worst verdict wins, but every match is kept. Ops triages the review queue by
   dimension, so the engine records all fired rules rather than short-circuiting.

4. Human overrides outrank rules. Once a person clears or kills a SKU, that
   decision is durable and survives rule-pack updates, with the reviewer and
   timestamp recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .packparse import parse_pack, Pack

RULES_PATH = Path(__file__).parent / "rules" / "compliance.yaml"

# Verdict severity. Higher is worse; `max` over fired rules gives the outcome.
SEVERITY = {"ALLOWED": 0, "REVIEW": 1, "BLOCKED": 2}
_BY_SEVERITY = {v: k for k, v in SEVERITY.items()}


@dataclass
class FiredRule:
    rule_id: str
    layer: str           # keyword | group | category | pack | default | override
    verdict: str
    dimension: Optional[str]
    reason: str
    authority: str
    matched_on: Optional[str] = None   # the literal text fragment that matched


@dataclass
class Verdict:
    product_id: str
    verdict: str
    dimensions: list[str] = field(default_factory=list)
    handling: list[str] = field(default_factory=list)
    fired: list[FiredRule] = field(default_factory=list)
    pack: Optional[Pack] = None
    needs_llm: bool = False

    @property
    def primary(self) -> Optional[FiredRule]:
        """The single rule that best explains the verdict, for UI display."""
        same = [f for f in self.fired if f.verdict == self.verdict]
        return same[0] if same else (self.fired[0] if self.fired else None)

    def explain(self) -> str:
        p = self.primary
        if not p:
            return f"{self.verdict}: no rule matched"
        return f"{self.verdict} [{p.rule_id}] {p.reason} ({p.authority})"

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "verdict": self.verdict,
            "dimensions": self.dimensions,
            "handling": self.handling,
            "needs_llm": self.needs_llm,
            "primary_rule": self.primary.rule_id if self.primary else None,
            "reason": self.primary.reason if self.primary else "",
            "authority": self.primary.authority if self.primary else "",
            "fired": [f.__dict__ for f in self.fired],
            "pack": self.pack.as_dict() if self.pack else None,
        }


class ComplianceEngine:
    def __init__(self, rules_path: Path | str = RULES_PATH,
                 overrides: Optional[dict[str, dict]] = None):
        self.rules = yaml.safe_load(Path(rules_path).read_text())
        self.meta = self.rules.get("meta", {})
        # Human decisions keyed by product_id, loaded from the DB by the caller.
        self.overrides = overrides or {}
        self._compile()

    def _compile(self) -> None:
        """Pre-compile every regex once. 31k SKUs x 39 patterns is hot enough
        that re-compiling per product dominates runtime."""
        self._kw = []
        for r in self.rules.get("keyword_rules", []):
            self._kw.append({
                **r,
                "_re": re.compile(r["pattern"], re.I),
                "_except": re.compile(r["except_pattern"], re.I) if r.get("except_pattern") else None,
                "_scope": set(r.get("scope_super") or []),
                "_notscope": set(r.get("scope_not_super") or []),
            })
        # Highest priority first so the primary rule is the most specific one.
        self._kw.sort(key=lambda r: -r.get("priority", 0))
        self._groups = self.rules.get("group_rules", {}) or {}
        self._cats = self.rules.get("category_rules", {}) or {}
        self._packs = self.rules.get("pack_rules", []) or []

    # -- individual layers ---------------------------------------------------

    def _match_keywords(self, haystack: str, super_cat: Optional[str] = None) -> list[FiredRule]:
        """Match name+brand against the keyword layer.

        Scoping matters more than it looks. The word "cream" means chilled
        dairy in a grocery aisle and a face moisturiser in a beauty aisle;
        "tablet" is a medicine in pharma and a computer in electronics. Without
        an aisle scope a single rule silently deletes thousands of listable
        SKUs, which is exactly the failure this guards against.
        """
        out = []
        sc = super_cat or ""
        for r in self._kw:
            if r["_scope"] and sc not in r["_scope"]:
                continue
            if r["_notscope"] and sc in r["_notscope"]:
                continue
            m = r["_re"].search(haystack)
            if not m:
                continue
            # An except_pattern rescues false positives: "milk" blocks fresh
            # dairy, but must not block "milk chocolate" or "cleansing milk".
            if r["_except"] and r["_except"].search(haystack):
                continue
            out.append(FiredRule(
                rule_id=r["id"], layer="keyword", verdict=r["verdict"],
                dimension=r.get("dimension"), reason=r["reason"],
                authority=r.get("authority", "-"), matched_on=m.group(0),
            ))
        return out

    def _match_table(self, table: dict, key: Optional[str], layer: str) -> list[FiredRule]:
        if not key:
            return []
        r = table.get(key)
        if not r:
            return []
        return [FiredRule(
            rule_id=f"{layer.upper()}:{key}", layer=layer, verdict=r["verdict"],
            dimension=r.get("dimension"), reason=r["reason"],
            authority=r.get("authority", "-"), matched_on=key,
        )]

    def _match_pack(self, pack: Pack) -> list[FiredRule]:
        out = []
        for r in self._packs:
            hit = False
            if r.get("on_unparseable_pack") and pack.confidence == "none":
                hit = True
            cap = r.get("max_billable_g")
            if cap is not None and (pack.billable_g or 0) > cap:
                hit = True
            if hit:
                out.append(FiredRule(
                    rule_id=r["id"], layer="pack", verdict=r["verdict"],
                    dimension=r.get("dimension"), reason=r["reason"],
                    authority=r.get("authority", "-"), matched_on=pack.raw,
                ))
        return out

    # -- public API ----------------------------------------------------------

    def classify(self, product: dict[str, Any]) -> Verdict:
        """Classify one product dict (the ingest schema). Never raises."""
        pid = str(product.get("product_id", ""))
        name = product.get("name") or ""
        brand = product.get("brand") or ""
        haystack = f"{name} {brand}"

        pack = parse_pack(product.get("unit"))
        fired: list[FiredRule] = []

        fired += self._match_keywords(haystack, product.get("super_category"))
        # Group is more specific than category; both are recorded but group is
        # what a reviewer reads first.
        group_hits = self._match_table(self._groups, product.get("group_name"), "group")
        fired += group_hits
        if not group_hits:
            fired += self._match_table(self._cats, product.get("category_name"), "category")
        fired += self._match_pack(pack)

        # Collect handling flags from every source that defines them.
        handling: list[str] = []
        for r in self._kw:
            if any(f.rule_id == r["id"] for f in fired):
                handling += r.get("handling", []) or []
        for tbl in (self._groups, self._cats):
            for key, r in tbl.items():
                if any(f.matched_on == key for f in fired):
                    handling += r.get("handling", []) or []

        if fired:
            worst = max(SEVERITY[f.verdict] for f in fired)
            verdict = _BY_SEVERITY[worst]
            needs_llm = False
        else:
            # Nothing matched. Conservative default plus a flag telling the
            # LLM tail-classifier that this is where it earns its keep.
            verdict = "REVIEW" if self.meta.get("review_default", True) else "ALLOWED"
            fired = [FiredRule(
                rule_id="DEFAULT-REVIEW", layer="default", verdict=verdict,
                dimension=None,
                reason="No rule matched this SKU. Unknown items are held for review rather than listed.",
                authority="Policy: conservative default",
            )]
            needs_llm = True

        # Ordering: worst-verdict rules first, then by layer specificity, so
        # `primary` is always the most actionable explanation.
        layer_rank = {"keyword": 0, "group": 1, "category": 2, "pack": 3, "default": 4}
        fired.sort(key=lambda f: (-SEVERITY[f.verdict], layer_rank.get(f.layer, 9)))

        v = Verdict(
            product_id=pid, verdict=verdict,
            dimensions=sorted({f.dimension for f in fired if f.dimension}),
            handling=sorted(set(handling)), fired=fired, pack=pack,
            needs_llm=needs_llm,
        )

        # A recorded human decision outranks everything above it.
        ov = self.overrides.get(pid)
        if ov:
            v.verdict = ov["verdict"]
            v.fired.insert(0, FiredRule(
                rule_id=f"OVERRIDE:{ov.get('reviewer', 'unknown')}",
                layer="override", verdict=ov["verdict"], dimension=None,
                reason=ov.get("note") or "Manual review decision.",
                authority=f"Reviewed {ov.get('decided_at', '-')}",
            ))
            v.needs_llm = False
        return v

    def classify_many(self, products: list[dict]) -> list[Verdict]:
        return [self.classify(p) for p in products]
