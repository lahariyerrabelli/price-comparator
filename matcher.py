"""
matcher.py  –  NLP-based fuzzy product matching & price comparison
Groups products from Blinkit, Zepto, BigBasket by semantic similarity.
"""

import re
import unicodedata
from difflib import SequenceMatcher


# ── Text normalisation ────────────────────────────────────────────────────────

_STOP = {
    "oil", "the", "and", "with", "for", "in", "of", "a", "an",
    "refined", "extra", "virgin", "cold", "pressed", "filtered",
    "pure", "natural", "organic", "premium", "fresh", "regular",
    "pack", "combo", "offer", "new", "best", "top", "quality",
}

_QTY_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*'
    r'(kg|g|gm|gms|gram|grams|l|ltr|litre|litres|liter|liters|ml|pcs|pc|pieces?|units?|nos?|pouch|packet|bottle|box|bag|sachet)',
    re.IGNORECASE
)

_QTY_NORM = {
    "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "ltr": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
    "pcs": "pcs", "pc": "pcs", "pieces": "pcs", "piece": "pcs",
    "units": "pcs", "unit": "pcs", "nos": "pcs", "no": "pcs",
    "pouch": "pouch", "packet": "packet", "bottle": "bottle",
    "box": "box", "bag": "bag", "sachet": "sachet",
}


def _norm_unit(u: str) -> str:
    return _QTY_NORM.get(u.lower(), u.lower())


def _parse_qty(text: str) -> tuple[float, str] | None:
    """Extract (numeric_value_in_base_unit, canonical_unit) from a qty string."""
    m = _QTY_RE.search(text or "")
    if not m:
        return None
    val  = float(m.group(1))
    unit = _norm_unit(m.group(2))
    # Convert to base units for numeric comparison
    if unit == "g":
        return (val, "g")
    if unit == "l":
        return (val * 1000, "ml_equiv")  # compare ml vs l on same scale
    if unit == "ml":
        return (val, "ml_equiv")
    if unit == "kg":
        return (val * 1000, "g")
    return (val, unit)


def _qty_match(a: str, b: str) -> bool:
    """True if both have no qty, or same qty in compatible units (±5%)."""
    qa, qb = _parse_qty(a), _parse_qty(b)
    if qa is None and qb is None:
        return True
    if qa is None or qb is None:
        return False
    if qa[1] != qb[1]:
        return False
    ratio = qa[0] / qb[0] if qb[0] else 0
    return 0.95 <= ratio <= 1.05


def _clean(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokens(text: str) -> set[str]:
    return {w for w in _clean(text).split() if w not in _STOP and len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _seq_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _clean(a), _clean(b)).ratio()


def _similarity(name_a: str, qty_a: str, name_b: str, qty_b: str) -> float:
    """Combined similarity score [0..1]."""
    ta, tb = _tokens(name_a), _tokens(name_b)
    jacc = _jaccard(ta, tb)
    seq  = _seq_sim(name_a, name_b)
    score = 0.6 * jacc + 0.4 * seq

    # Penalise if quantities are incompatible
    combined_a = f"{name_a} {qty_a}"
    combined_b = f"{name_b} {qty_b}"
    if not _qty_match(combined_a, combined_b):
        score *= 0.45

    return score


# ── Price helpers ─────────────────────────────────────────────────────────────

def _parse_price(price_str: str) -> float | None:
    if not price_str or price_str == "N/A":
        return None
    cleaned = re.sub(r"[^\d.]", "", str(price_str))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _best_offer(offers: list[dict]) -> dict | None:
    priced = [(o, _parse_price(o.get("selling_price"))) for o in offers]
    priced = [(o, p) for o, p in priced if p is not None]
    if not priced:
        return None
    return min(priced, key=lambda x: x[1])[0]


def _savings_str(offers: list[dict]) -> str | None:
    prices = [_parse_price(o.get("selling_price")) for o in offers]
    prices = [p for p in prices if p is not None]
    if len(prices) < 2:
        return None
    diff = max(prices) - min(prices)
    if diff < 0.5:
        return None
    return f"Save ₹{diff:.0f} vs costliest"


# ── Canonical name / qty ──────────────────────────────────────────────────────

def _canonical_name(offers: list[dict]) -> str:
    """Pick the shortest clean name as canonical (usually least noisy)."""
    names = [o.get("name", "") for o in offers if o.get("name") and o["name"] != "N/A"]
    if not names:
        return "Unknown Product"
    return min(names, key=len)


def _canonical_qty(offers: list[dict]) -> str:
    """Extract quantity from name or quantity field; prefer explicit qty field."""
    for o in offers:
        qty = (o.get("quantity") or "").strip()
        if qty and qty != "N/A":
            return qty
    # fallback: extract from name
    for o in offers:
        m = _QTY_RE.search(o.get("name", ""))
        if m:
            return m.group(0).strip()
    return "N/A"


# ── Grouping ──────────────────────────────────────────────────────────────────

MATCH_THRESHOLD = 0.42   # tunable — lower = more aggressive grouping


def group_and_compare(
    blinkit: list[dict],
    zepto: list[dict],
    bigbasket: list[dict],
) -> list[dict]:
    """
    Cluster products from all 3 sources into comparison groups.
    Each group has: canonical_name, canonical_qty, offers[], best_deal, savings.
    """

    # Tag each product with its source
    all_products: list[dict] = []
    for p in blinkit:
        all_products.append({**p, "source": "blinkit"})
    for p in zepto:
        all_products.append({**p, "source": "zepto"})
    for p in bigbasket:
        all_products.append({**p, "source": "bigbasket"})

    if not all_products:
        return []

    # Greedy single-linkage clustering
    groups: list[list[dict]] = []

    for product in all_products:
        best_group_idx = None
        best_score = MATCH_THRESHOLD

        for idx, group in enumerate(groups):
            # Only one offer per source per group
            existing_sources = {o["source"] for o in group}
            if product["source"] in existing_sources:
                continue

            # Compare against every member; take max score
            for member in group:
                score = _similarity(
                    product.get("name", ""), product.get("quantity", ""),
                    member.get("name", ""),  member.get("quantity", ""),
                )
                if score > best_score:
                    best_score = score
                    best_group_idx = idx

        if best_group_idx is not None:
            groups[best_group_idx].append(product)
        else:
            groups.append([product])

    # Sort groups: cross-source first (most offers), then by price
    groups.sort(key=lambda g: (-len({o["source"] for o in g}), len(g)))

    result = []
    for group in groups:
        c_name = _canonical_name(group)
        c_qty  = _canonical_qty(group)
        best   = _best_offer(group)
        sav    = _savings_str(group)

        # Sort offers: best price first
        sorted_offers = sorted(
            group,
            key=lambda o: (_parse_price(o.get("selling_price")) or 9999)
        )

        result.append({
            "canonical_name": c_name,
            "canonical_qty":  c_qty,
            "offers":         sorted_offers,
            "best_deal":      best,
            "savings":        sav,
        })

    return result