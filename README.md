# LLM × Law — Pleading-to-Proof pipeline

Turns a litigation bundle (PDFs) into a **pleading-to-proof matrix**: every
pleaded allegation / denial is extracted and each is mapped to the available
evidence as **supportive**, **adverse**, **neutral**, or **MISSING** (gap).

## Pipeline

```
PDFs ─convert_pdfs.py─▶ converted/*.md ─extract.py─▶ out/propositions.json
                                          │                out/evidence.json
                                          └─build_matrix.py─▶ out/matrix.{json,csv,md}
```

1. **convert_pdfs.py** — PDF → AI-readable Markdown (frontmatter + page markers +
   preserved paragraph numbering). Already run; output in `converted/`.
2. **extract.py** — classifies each doc (pleading / witness statement / exhibit),
   extracts atomic propositions from pleadings and evidence units from the rest.
   Every record carries provenance (doc_id, page, paragraph) and a **verbatim
   quote that is verified against the source** (anti-hallucination gate).
3. **build_matrix.py** — BM25-retrieves candidate evidence per proposition, has
   Claude classify each candidate, and rolls up the four-bucket matrix.

## Why this design

- **Two layers.** Allegations/denials come from the *pleadings*; the witness
  statements are *evidence*. The value is the mapping between them.
- **Atomic + provenance.** One claim per record, each with a source paragraph and
  a quote checked against the text — so every cell of the matrix is auditable.
- **Structured outputs** (`output_config.format`) for schema-valid records.
- **Batch API** (50% cheaper) for the per-document extraction; **prompt caching**
  on the shared instruction prefix.
- **Model:** `claude-opus-4-8`, adaptive thinking, `effort: high`.

## Setup & run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # PowerShell: $env:ANTHROPIC_API_KEY="..."

python convert_pdfs.py                      # PDFs -> converted/  (done)
python extract.py                           # -> out/propositions.json, out/evidence.json
python build_matrix.py                      # -> out/matrix.{json,csv,md}
```

Cheap dry run first: `python extract.py --mode sync --limit 3`.

## Notes & knobs

- **Pleadings:** the current `converted/` set is all witness statements, so
  `extract.py` will report *no pleadings detected* and propositions will be empty
  until the Particulars of Claim / Defence are added to the input folder (or
  designated with `--pleadings "<glob>"`). Evidence extraction runs regardless.
- **Recall vs cost:** `build_matrix.py --top-k N` controls how many candidate
  evidence units are considered per proposition (default 25). Raising it improves
  recall of adverse evidence at higher cost. The number considered is logged — no
  silent truncation.
- **Retrieval method:** `--retriever bm25` (default, no extra deps) | `embeddings`
  | `hybrid`. Embeddings/hybrid use **Voyage AI** (`voyage-law-2`, legal-domain
  tuned) and need `VOYAGE_API_KEY`; vectors are cached in `out/emb_*.npz` so
  re-runs don't re-embed. **Hybrid** fuses BM25 + embeddings via reciprocal-rank
  fusion — best for catching adverse evidence worded differently from the
  allegation (lexical retrieval alone can miss contradictions):
  ```bash
  $env:VOYAGE_API_KEY="pa-..."
  python build_matrix.py --retriever hybrid --top-k 40
  ```
- **Confidence:** `--min-confidence` (default 0.5) gates which links count toward
  a proposition's status and appear in the report.
- **Two corrupt source PDFs** (`witn04520100_2.pdf`, `witn04600300.pdf`) are
  0-byte and were skipped during conversion.
