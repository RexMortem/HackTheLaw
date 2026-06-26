#!/usr/bin/env python3
"""
PDF converter with robust fallback to OCR when pdfplumber fails.
"""
import argparse
import json
from pathlib import Path

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from pdf2image import convert_from_path
    import pytesseract
except Exception:
    convert_from_path = None
    pytesseract = None


def extract_text_from_pdf(path, ocr_if_needed=True, dpi=300):
    path = str(path)
    # Try pdfplumber if available
    if pdfplumber is not None:
        try:
            with pdfplumber.open(path) as pdf:
                for i, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    used_ocr = False
                    if (not text.strip()) and ocr_if_needed and convert_from_path and pytesseract:
                        images = convert_from_path(path, dpi=dpi, first_page=i, last_page=i)
                        if images:
                            text = pytesseract.image_to_string(images[0])
                            used_ocr = True
                    yield i, text, used_ocr
            return
        except Exception:
            pass

    # Fallback: full-file OCR if possible
    if ocr_if_needed and convert_from_path and pytesseract:
        images = convert_from_path(path, dpi=dpi)
        for i, img in enumerate(images, start=1):
            text = pytesseract.image_to_string(img)
            yield i, text, True
        return

    # Last resort: single empty page
    yield 1, "", False


def save_jsonl(doc_id, pages, outpath):
    with open(outpath, "w", encoding="utf-8") as f:
        for page_num, text, used_ocr in pages:
            obj = {"doc_id": doc_id, "page": page_num, "text": text.strip(), "used_ocr": bool(used_ocr)}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def process_file(path, outdir, fmt):
    path = Path(path)
    doc_id = path.stem
    pages = list(extract_text_from_pdf(path))
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    outpath = outdir / f"{doc_id}.jsonl"
    save_jsonl(doc_id, pages, outpath)
    return outpath


def find_pdfs(root):
    p = Path(root)
    if p.is_file() and p.suffix.lower() == ".pdf":
        return [p]
    if p.is_dir():
        return sorted([x for x in p.rglob("*.pdf")])
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--outdir", "-o", default="out")
    args = ap.parse_args()
    pdfs = find_pdfs(args.input)
    if not pdfs:
        print("No PDFs found")
        return
    for p in pdfs:
        print("Processing", p)
        out = process_file(p, args.outdir, "jsonl")
        print("Wrote", out)


if __name__ == "__main__":
    main()
