"""
matcher.py  –  NLP-based fuzzy product matching & price comparison
Groups products from Blinkit, Zepto, BigBasket by semantic similarity.
Uses TF-IDF vectors with cosine similarity for product name matching.
"""

import re
import math
import unicodedata
from collections import Counter


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
    m = _QTY_RE.search(text or "")
    if not m:
        return None
    val  = float(m.group(1))
    unit = _norm_unit(m.group(2))
    if unit == "g":
        return (val, "g")
    if unit == "l":
        return (val * 1000, "ml_equiv")
    if unit == "ml":
        return (val, "ml_equiv")
    if unit == "kg":
        return (val * 1000, "g")
    return (val, unit)


def _qty_match(a: str, b: str) -> bool:
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


def _tokens(text: str) -> list[str]:
    return [w for w in _clean(text).split() if w not in _STOP and len(w) > 1]


# ── TF-IDF corpus ─────────────────────────────────────────────────────────────

class TFIDFCorpus:
    """
    Lightweight TF-IDF engine built over all product names seen so far.
    Call .fit(documents) once, then .vector(text) to get a sparse TF-IDF dict.
    """

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._n_docs: int = 0

    def fit(self, documents: list[str]) -> None:
        """Compute IDF weights from a collection of product-name strings."""
        self._n_docs = len(documents)
        df: Counter = Counter()
        for doc in documents:
            df.update(set(_tokens(doc)))
        # Smoothed IDF: log((1 + N) / (1 + df)) + 1
        self._idf = {
            term: math.log((1 + self._n_docs) / (1 + count)) + 1
            for term, count in df.items()
        }

    def vector(self, text: str) -> dict[str, float]:
        """
        Return a TF-IDF vector as {term: weight}.
        TF = raw term count / doc length  (normalised term frequency)
        """
        toks = _tokens(text)
        if not toks:
            return {}
        tf_raw = Counter(toks)
        doc_len = len(toks)
        vec: dict[str, float] = {}
        for term, cnt in tf_raw.items():
            idf = self._idf.get(term, math.log((1 + self._n_docs) / 1) + 1)
            vec[term] = (cnt / doc_len) * idf
        return vec


def _cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Cosine similarity between two sparse TF-IDF vectors."""
    if not vec_a or not vec_b:
        return 0.0
    dot = sum(vec_a[t] * vec_b[t] for t in vec_a if t in vec_b)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# Module-level corpus — populated lazily in group_and_compare
_corpus = TFIDFCorpus()


# ── Similarity ────────────────────────────────────────────────────────────────

def _similarity(
    name_a: str, qty_a: str,
    name_b: str, qty_b: str,
    vec_a: dict[str, float] | None = None,
    vec_b: dict[str, float] | None = None,
) -> float:
    """
    TF-IDF cosine similarity score [0..1], penalised for quantity mismatch.
    Vectors are pre-computed and passed in to avoid redundant work during
    O(n^2) grouping.
    """
    if vec_a is None:
        vec_a = _corpus.vector(name_a)
    if vec_b is None:
        vec_b = _corpus.vector(name_b)

    score = _cosine(vec_a, vec_b)

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
    best = _best_offer(offers)
    if not best:
        return None

    selling = _parse_price(best.get("selling_price"))
    mrp     = _parse_price(best.get("mrp"))          # need mrp in offer dict

    if selling is None or mrp is None:
        return None
    diff = mrp - selling
    if diff < 0.5:
        return None

    source = best.get("source", "").capitalize()
    return f"Save ₹{diff:.0f} on {source}"


# ── Canonical name / qty ──────────────────────────────────────────────────────

def _canonical_name(offers: list[dict]) -> str:
    names = [o.get("name", "") for o in offers if o.get("name") and o["name"] != "N/A"]
    if not names:
        return "Unknown Product"
    return min(names, key=len)


def _canonical_qty(offers: list[dict]) -> str:
    for o in offers:
        qty = (o.get("quantity") or "").strip()
        if qty and qty != "N/A":
            return qty
    for o in offers:
        m = _QTY_RE.search(o.get("name", ""))
        if m:
            return m.group(0).strip()
    return "N/A"


# ── Grouping ──────────────────────────────────────────────────────────────────

MATCH_THRESHOLD = 0.35   # TF-IDF cosine scores; tune lower to merge more aggressively


def group_and_compare(
    blinkit: list[dict],
    zepto: list[dict],
    bigbasket: list[dict],
) -> list[dict]:
    """
    Cluster products from all 3 sources into comparison groups.
    Each group has: canonical_name, canonical_qty, offers[], best_deal, savings.

    Steps:
      1. Fit a TF-IDF corpus over all product names (computes IDF weights).
      2. Pre-compute one TF-IDF vector per product (avoids O(n^2) re-tokenising).
      3. Greedy single-linkage clustering using cosine similarity.
    """

    all_products: list[dict] = []
    for p in blinkit:
        all_products.append({**p, "source": "blinkit"})
    for p in zepto:
        all_products.append({**p, "source": "zepto"})
    for p in bigbasket:
        all_products.append({**p, "source": "bigbasket"})

    if not all_products:
        return []

    # Step 1 — fit corpus
    _corpus.fit([p.get("name", "") for p in all_products])

    # Step 2 — pre-compute vectors
    vectors: list[dict[str, float]] = [
        _corpus.vector(p.get("name", "")) for p in all_products
    ]

    # Step 3 — greedy single-linkage clustering
    groups: list[list[int]] = []

    for i, product in enumerate(all_products):
        best_group_idx = None
        best_score = MATCH_THRESHOLD

        for gidx, group_indices in enumerate(groups):
            existing_sources = {all_products[j]["source"] for j in group_indices}
            if product["source"] in existing_sources:
                continue

            for j in group_indices:
                score = _similarity(
                    product.get("name", ""),          product.get("quantity", ""),
                    all_products[j].get("name", ""),  all_products[j].get("quantity", ""),
                    vec_a=vectors[i],
                    vec_b=vectors[j],
                )
                if score > best_score:
                    best_score = score
                    best_group_idx = gidx

        if best_group_idx is not None:
            groups[best_group_idx].append(i)
        else:
            groups.append([i])

    # Assemble result
    resolved: list[list[dict]] = [
        [all_products[i] for i in idx_list] for idx_list in groups
    ]
    resolved.sort(key=lambda g: (-len({o["source"] for o in g}), len(g)))

    result = []
    for group in resolved:
        c_name = _canonical_name(group)
        c_qty  = _canonical_qty(group)
        best   = _best_offer(group)
        sav    = _savings_str(group)

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