"""LLM tail-classifier: the ~28% of SKUs that no deterministic rule covers.

Why this is small and late in the pipeline:

  - Rules handle ~72% of the catalogue for free, reproducibly, and with a
    citation a customs broker can check. That is the right default.
  - The LLM is for genuine ambiguity ("Rangoli Colour Powder" -- is that a
    pigment, a food dye, a hazmat?), where a rule would be guesswork anyway.
  - Its output is never trusted blind. It returns a verdict AND a confidence;
    anything below the threshold still lands in the human queue. The LLM
    shrinks the queue, it does not replace it.

Cost control: SKUs are batched, and identical (group, category) pairs are
deduplicated so one call covers every SKU on that shelf. The 8,909-SKU tail
collapses to a few hundred distinct shelf signatures.

No API key present -> `classify_tail` returns nothing and the tail stays in
REVIEW. The pipeline degrades to rules-only rather than failing.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

MODEL = "claude-opus-5"
CONFIDENCE_FLOOR = 0.80

SYSTEM_PROMPT = """You classify Indian grocery/retail SKUs for export from India to the United States.

For each product decide whether it can be listed on a US-facing storefront that
buys the item in India and air-ships it to a US consumer.

Verdicts:
  ALLOWED - lawful to import and physically survives 5-15 days of air transit.
  REVIEW  - plausibly listable but needs a human (FDA/EPA/CPSC registration,
            labelling, unclear composition, or transit risk you cannot resolve).
  BLOCKED - unlawful to import, or cannot arrive intact at any price.

Consider: USDA/APHIS animal and plant restrictions; FDA food, drug, cosmetic
and device rules; EPA pesticide registration; CPSC/CPSIA safety; IATA dangerous
goods (aerosols, flammables, lithium cells); cold chain; shelf life; and
220V/110V mains incompatibility for Indian appliances.

Be conservative. If you are not confident, return REVIEW with a low confidence
rather than guessing ALLOWED. A wrong ALLOWED causes a seized shipment; a wrong
REVIEW costs one minute of a human's time.

Return ONLY a JSON array, one object per input product, in the same order:
[{"idx":0,"verdict":"ALLOWED","dimension":"US_IMPORT","confidence":0.93,"reason":"one sentence"}]
`dimension` is one of US_IMPORT, HAZMAT, TEMPERATURE, PERISHABLE, FRAGILITY,
FUNCTIONAL, DUTY, IP_RISK, or null when ALLOWED."""


@dataclass
class LLMVerdict:
    verdict: str
    dimension: Optional[str]
    confidence: float
    reason: str
    model: str = MODEL


def _signature(p: dict) -> tuple:
    """SKUs sharing a shelf and a product-name shape get one classification."""
    name = (p.get("name") or "").lower()
    # Strip pack sizes and numbers so "Diya 6 pcs" and "Diya 12 pcs" collapse.
    stem = re.sub(r"\d+\s*(g|kg|ml|l|pc|pcs|pack)?\b", "", name).strip()
    stem = " ".join(stem.split()[:3])
    return (p.get("group_name"), p.get("category_name"), stem)


def classify_tail(products: list[dict[str, Any]], batch_size: int = 40,
                  api_key: Optional[str] = None) -> dict[str, LLMVerdict]:
    """Classify SKUs no rule matched. Returns {product_id: LLMVerdict}.

    Degrades to {} when no API key or SDK is available, leaving the tail in
    REVIEW. That is a safe failure: the catalogue just grows more slowly.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not products:
        return {}
    try:
        import anthropic
    except ImportError:
        return {}

    client = anthropic.Anthropic(api_key=key)

    # Deduplicate: classify one representative per shelf signature.
    reps: dict[tuple, dict] = {}
    members: dict[tuple, list[str]] = {}
    for p in products:
        sig = _signature(p)
        reps.setdefault(sig, p)
        members.setdefault(sig, []).append(str(p["product_id"]))

    sigs = list(reps)
    out: dict[str, LLMVerdict] = {}

    for start in range(0, len(sigs), batch_size):
        chunk = sigs[start:start + batch_size]
        payload = [{
            "idx": i,
            "name": reps[s].get("name"),
            "brand": reps[s].get("brand"),
            "pack": reps[s].get("unit"),
            "shelf": reps[s].get("group_name"),
            "category": reps[s].get("category_name"),
        } for i, s in enumerate(chunk)]

        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            )
            text = resp.content[0].text.strip()
            m = re.search(r"\[.*\]", text, re.S)
            results = json.loads(m.group(0) if m else text)
        except Exception:
            continue   # a failed batch leaves those SKUs in REVIEW

        for r in results:
            i = r.get("idx")
            if not isinstance(i, int) or i >= len(chunk):
                continue
            conf = float(r.get("confidence", 0))
            verdict = r.get("verdict", "REVIEW")
            # Low confidence is not a verdict. Send it to the humans.
            if conf < CONFIDENCE_FLOOR:
                verdict = "REVIEW"
            v = LLMVerdict(verdict=verdict, dimension=r.get("dimension"),
                           confidence=conf, reason=r.get("reason", ""))
            for pid in members[chunk[i]]:
                out[pid] = v
    return out


def apply_to_db(db_path="crossborder.db", limit: Optional[int] = None) -> dict:
    """Classify the DEFAULT-REVIEW tail and write back any confident verdicts."""
    from .db import connect
    conn = connect(db_path)
    q = """SELECT p.* FROM products p JOIN classifications c USING(product_id)
           WHERE c.primary_rule='DEFAULT-REVIEW' AND c.source='rules'"""
    if limit:
        q += f" LIMIT {int(limit)}"
    tail = [dict(r) for r in conn.execute(q)]
    if not tail:
        conn.close()
        return {"tail": 0, "resolved": 0}

    verdicts = classify_tail(tail)
    for pid, v in verdicts.items():
        conn.execute("""UPDATE classifications SET verdict=?, dimensions=?,
                        reason=?, authority=?, source='llm', confidence=?
                        WHERE product_id=?""",
                     (v.verdict, v.dimension or "", v.reason,
                      f"LLM {v.model} (conf {v.confidence:.2f})", v.confidence, pid))
    conn.commit()
    resolved = sum(1 for v in verdicts.values() if v.verdict != "REVIEW")
    conn.close()
    return {"tail": len(tail), "classified": len(verdicts), "resolved": resolved}
