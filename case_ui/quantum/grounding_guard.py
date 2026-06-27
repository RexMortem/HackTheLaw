"""
grounding_guard.py  —  Crucible's citation verifier ("no hallucinated citations")

WHAT IT DOES
------------
After Claude returns a structured per-proposition schema, every claim carries a
citation: a source id (a paragraph like "¶80" or a document like "RLIT0000039")
plus the quoted text it relied on. This guard checks, for EVERY citation:

  1. Does the cited source id actually EXIST in the bundle?      -> else: FABRICATED_SOURCE
  2. Does the quoted text actually APPEAR in that source?        -> else: MISQUOTE
     (exact match preferred; a fuzzy match tolerates OCR / whitespace noise
      but is flagged so a human can glance at it)

Anything that fails is QUARANTINED — pulled out of what the UI shows — so a judge
can never click a citation that doesn't hold. Passing claims go through untouched.

This is the correctness layer. It does not trust the model; it checks the model.

MULTI-DOCUMENT NOTE
-------------------
Paragraph numbers repeat across statements (every witness has a ¶80). So always
cite with a COMPOSITE id, e.g. "WITN10490100 ¶80", and build the bundle with the
same composite ids (see engine.build_bundle). _canon_id normalises both sides
identically, keeping each paragraph unique to its document.

HOW TO WIRE IT IN
-----------------
    bundle = Bundle.from_dict(my_paragraphs, my_documents)
    result = guard(claude_json, bundle)
    show_in_ui(result.verified)       # safe to display
    log_for_review(result.flagged)    # never shown raw; surfaced as "needs review"

Run `python grounding_guard.py` to see it catch a fabricated citation on the
Post Office demo data.
"""

from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
#  Verdicts
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    EXACT = "exact"                      # quote found verbatim in the source
    FUZZY = "fuzzy"                       # close match (OCR/whitespace) — flag for a glance
    MISQUOTE = "misquote"                # source exists, but the quote is not in it
    FABRICATED_SOURCE = "fabricated_src" # the cited paragraph / doc id does not exist
    NO_QUOTE = "no_quote"                # a source id was cited but no quote was given


PASS = {Status.EXACT, Status.FUZZY}


# --------------------------------------------------------------------------- #
#  Bundle: the ground truth the model is checked against
# --------------------------------------------------------------------------- #
@dataclass
class Bundle:
    """The source of truth. Keys are citation ids; values are the source text."""
    sources: dict[str, str]

    @classmethod
    def from_dict(cls, paragraphs: dict[str, str], documents: dict[str, str] | None = None) -> "Bundle":
        merged: dict[str, str] = {}
        for k, v in paragraphs.items():
            merged[_canon_id(k)] = v
        for k, v in (documents or {}).items():
            merged[_canon_id(k)] = v
        return cls(merged)

    def get(self, raw_id: str) -> Optional[str]:
        return self.sources.get(_canon_id(raw_id))


# --------------------------------------------------------------------------- #
#  Normalisation helpers
# --------------------------------------------------------------------------- #
def _canon_id(raw: str) -> str:
    """
    Canonicalise a citation id so '¶80', 'para 80', 'Paragraph 80', '80'
    all resolve to the same key, while doc ids like 'RLIT0000039' and composite
    ids like 'WITN10490100 ¶80' are kept distinct.
    """
    s = unicodedata.normalize("NFKC", str(raw)).strip().lower()
    s = s.replace("¶", "para ").replace("paragraph", "para").replace("§", "para ")
    m = re.fullmatch(r"para\s*0*([0-9]+)", s)
    if m:
        return f"para:{int(m.group(1))}"
    if re.fullmatch(r"0*([0-9]+)", s):                 # bare number -> paragraph
        return f"para:{int(s)}"
    return re.sub(r"\s+", "", s)                        # doc / composite ids: strip spaces


def _norm_text(t: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation that OCR tends to mangle."""
    t = unicodedata.normalize("NFKC", str(t)).lower()
    t = t.replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[^\w\s]", " ", t)        # punctuation -> space (robust to OCR)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _best_window_ratio(quote: str, source: str) -> float:
    """
    Highest similarity between the quote and any same-length window of the source.
    Lets us tolerate a few wrong characters without accepting a whole different quote.
    """
    q, s = _norm_text(quote), _norm_text(source)
    if not q:
        return 0.0
    if q in s:
        return 1.0
    qlen = len(q)
    if qlen >= len(s):
        return SequenceMatcher(None, q, s).ratio()
    best = 0.0
    step = max(1, qlen // 8)
    for i in range(0, len(s) - qlen + 1, step):
        r = SequenceMatcher(None, q, s[i:i + qlen]).ratio()
        if r > best:
            best = r
            if best == 1.0:
                break
    return best


# --------------------------------------------------------------------------- #
#  Single-citation check
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    source_id: str
    quote: str
    status: Status
    score: float = 0.0          # 0..1 quote-match confidence
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in PASS


def verify_citation(source_id: str, quote: str, bundle: Bundle,
                    fuzzy_threshold: float = 0.86) -> CheckResult:
    src = bundle.get(source_id)
    if src is None:
        return CheckResult(source_id, quote, Status.FABRICATED_SOURCE,
                           detail=f"cited id '{source_id}' is not in the bundle")
    if not quote or not quote.strip():
        return CheckResult(source_id, quote, Status.NO_QUOTE, 1.0,
                           detail="source exists but no quote was provided to verify")
    ratio = _best_window_ratio(quote, src)
    if ratio >= 0.999:
        return CheckResult(source_id, quote, Status.EXACT, ratio, "verbatim match")
    if ratio >= fuzzy_threshold:
        return CheckResult(source_id, quote, Status.FUZZY, ratio,
                           f"close match ({ratio:.0%}) — likely OCR/whitespace; glance to confirm")
    return CheckResult(source_id, quote, Status.MISQUOTE, ratio,
                       f"quote not found in '{source_id}' (best match {ratio:.0%})")


# --------------------------------------------------------------------------- #
#  Guarding a whole Claude response
# --------------------------------------------------------------------------- #
@dataclass
class GuardReport:
    verified: list[dict] = field(default_factory=list)   # safe to show in UI
    flagged: list[dict] = field(default_factory=list)    # quarantined / needs review
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def passed(self) -> int:
        return sum(c.ok for c in self.checks)

    def summary(self) -> str:
        if not self.total:
            return "no citations to verify"
        bad = self.total - self.passed
        head = f"{self.passed}/{self.total} citations grounded"
        return head if bad == 0 else f"{head} — {bad} quarantined"


def _iter_citations(claim: dict):
    """
    Accepts a few shapes so it slots into whatever schema Claude emits:
      {"citations": [{"source_id": "¶80", "quote": "..."}]}
      {"evidence":  [{"source": "¶80", "text":  "..."}]}
      {"source_id": "¶80", "quote": "..."}              (single)
    Yields (source_id, quote).
    """
    cites = claim.get("citations") or claim.get("evidence")
    if isinstance(cites, list):
        for c in cites:
            sid = c.get("source_id") or c.get("source") or c.get("id") or ""
            q = c.get("quote") or c.get("text") or c.get("excerpt") or ""
            yield str(sid), str(q)
    elif claim.get("source_id") or claim.get("source"):
        yield str(claim.get("source_id") or claim.get("source")), str(claim.get("quote") or claim.get("text") or "")


def guard(claude_output: dict, bundle: Bundle, fuzzy_threshold: float = 0.86) -> GuardReport:
    """
    Verify every citation in a Claude response.

    Expects something like:
        {"propositions": [ { "id": "P1", "text": "...",
                             "citations": [ {"source_id": "¶80", "quote": "..."} ] }, ... ]}

    A proposition is VERIFIED only if all its citations pass. If any citation
    fails, the whole proposition is quarantined (flagged) so the UI never shows
    a claim that leans on a broken citation.
    """
    report = GuardReport()
    props = claude_output.get("propositions") or claude_output.get("claims") or []
    for prop in props:
        results = [verify_citation(sid, q, bundle, fuzzy_threshold)
                   for sid, q in _iter_citations(prop)]
        report.checks.extend(results)
        enriched = dict(prop)
        enriched["_citation_checks"] = [c.__dict__ for c in results]
        if results and all(r.ok for r in results):
            report.verified.append(enriched)
        else:
            reasons = [f"{r.source_id}: {r.status.value}" for r in results if not r.ok] or ["no citations supplied"]
            enriched["_quarantine_reason"] = "; ".join(reasons)
            report.flagged.append(enriched)
    return report


# --------------------------------------------------------------------------- #
#  Demo — run this file directly
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # A tiny slice of bundle "ground truth" (stand-in for the real Gilchrist statement).
    paragraphs = {
        "¶34": "An audit was carried out at the branch in February 2012 and a shortfall "
               "was attributed to the subpostmaster on the basis of the Horizon figures.",
        "¶80": "Prosecutions continued during a period when the integrity of Horizon was "
               "being challenged in some cases.",
    }
    documents = {
        "RLIT0000039": "The Court of Appeal quashed the conviction, holding that the Horizon "
                       "evidence was unreliable and could not safely support the charge.",
    }
    bundle = Bundle.from_dict(paragraphs, documents)

    # A pretend Claude response: two honest claims and ONE with a fabricated source
    # plus ONE with a wording that drifts from the source (misquote).
    claude_output = {
        "propositions": [
            {   # GOOD — exact
                "id": "P1", "text": "The shortfall was based on Horizon data, not a manual count.",
                "citations": [{"source_id": "para 34",
                               "quote": "a shortfall was attributed to the subpostmaster on the basis of the Horizon figures"}],
            },
            {   # GOOD — fuzzy (OCR-style noise, should still pass)
                "id": "P2", "text": "Prosecutions continued while Horizon's integrity was disputed.",
                "citations": [{"source_id": "¶80",
                               "quote": "Prosecutions  continued during a period when the integrety of Horizon was being challenged"}],
            },
            {   # BAD — fabricated paragraph that does not exist
                "id": "P3", "text": "A manager admitted the system was knowingly defective.",
                "citations": [{"source_id": "¶112",
                               "quote": "I knew the system was producing false figures and said nothing"}],
            },
            {   # BAD — real source, but the quote is not actually in it (misquote)
                "id": "P4", "text": "The court blamed the subpostmaster.",
                "citations": [{"source_id": "RLIT0000039",
                               "quote": "the subpostmaster was found to have stolen the money"}],
            },
        ]
    }

    report = guard(claude_output, bundle)

    print("=" * 64)
    print("GROUNDING GUARD —", report.summary())
    print("=" * 64)
    print(f"\n✅ VERIFIED  (safe to show in the UI): {len(report.verified)}")
    for p in report.verified:
        chk = p["_citation_checks"][0]
        print(f"   • {p['id']}  [{chk['status']}, {chk['score']:.0%}]  {p['text']}")

    print(f"\n⛔ QUARANTINED (never shown raw):       {len(report.flagged)}")
    for p in report.flagged:
        print(f"   • {p['id']}  reason: {p['_quarantine_reason']}")
        print(f"      ↳ \"{p['text']}\"")

    print("\nPer-citation detail:")
    for c in report.checks:
        mark = "ok " if c.ok else "FAIL"
        print(f"   [{mark}] {c.source_id:<14} {c.status.value:<16} {c.detail}")
