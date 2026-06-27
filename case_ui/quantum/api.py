"""Minimal backend for the quantum demo only.

Run:
    python -m uvicorn api:app --port 8000

Open:
    http://127.0.0.1:8000/quantum.html
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from quantum.bridge import from_spec, to_dict
from quantum.case_meridian import result as meridian_quantum


app = FastAPI(title="Quantum Demo")
WEB = os.path.join(os.path.dirname(__file__), "web")
DATA_CMS = os.path.join(os.path.dirname(__file__), "data_cms")


@app.get("/api/quantum")
def quantum_default():
    """Return the verified Meridian damages waterfall."""
    return to_dict(meridian_quantum())


@app.post("/api/quantum")
def quantum_from_spec(spec: dict):
    """Compute a damages waterfall from a structured figures spec."""
    return to_dict(from_spec(spec))


app.mount("/data_cms", StaticFiles(directory=DATA_CMS), name="data_cms")
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
