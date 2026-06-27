"""Check that quantum citations still point to text in the bundled documents."""
from __future__ import annotations

from pathlib import Path

from grounding_guard import Bundle, verify_citation
from quantum.case_meridian import CAUSATION, CLAUSES, FRAUD, HEADS


ROOT = Path(__file__).resolve().parent
DOCS = {
    "GREENHALGH": ROOT / "data_cms/20_Expert_Report_Greenhalgh_Quantum.txt",
    "PoC": ROOT / "data_cms/02_Particulars_of_Claim.txt",
    "MSA": ROOT / "data_cms/03_Master_Services_Agreement.txt",
}


def main() -> None:
    bundle = Bundle.from_dict({}, {key: path.read_text() for key, path in DOCS.items()})
    checks = []
    checks += [(f"head:{h.name}", h.value.doc_id, h.value.quote)
               for h in HEADS if h.value.grounded and h.value.doc_id]
    checks += [(f"causation:{a.reason}", a.source.doc_id, a.source.quote)
               for a in CAUSATION]
    checks += [(f"clause:{c.clause_id}", c.source.doc_id, c.source.quote)
               for c in CLAUSES]
    checks += [("fraud", FRAUD.source.doc_id, FRAUD.source.quote)]

    ok = True
    for label, source_id, quote in checks:
        result = verify_citation(source_id, quote, bundle)
        ok = ok and result.ok
        print(f"{label}: {result.status.value} score={result.score:.3f} source={source_id}")
    if not ok:
        raise SystemExit("Some quantum citations did not ground in the source documents.")
    print("ok - all quantum citations are grounded")


if __name__ == "__main__":
    main()
