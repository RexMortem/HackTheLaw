"""Shared 3b1b-style palette for the demo animations.

Colours mirror the web app's status semantics so the video and the UI read as
one product. Tweak here once and every scene follows.
"""

# Backdrop — near-black with a faint blue cast, like a 3b1b frame.
BG = "#0e1116"

# Proof-matrix status colours (supported / contested / undermined / missing).
STATUS = {
    "supported": "#2ecc71",   # green
    "contested": "#f1c40f",   # amber
    "undermined": "#e74c3c",  # red
    "missing": "#7f8c8d",     # grey
}

# Edge / relation colours in the case graph.
SUPPORT = "#2ecc71"     # evidence supports a proposition
UNDERMINE = "#e74c3c"   # evidence undermines a proposition
NEUTRAL = "#3a4250"     # bears on it but cuts neither way (usually hidden)
DEPENDS = "#5dade2"     # logical A-depends-on-B spine

# Node fills.
PROP_FILL = "#dfe6f0"   # propositions: the prominent spine
EV_FILL = "#566072"     # evidence: the faint supporting cloud
ACCENT = "#9b59b6"      # highlight (articulation point / single point of failure)

INK = "#e8edf4"         # primary text
MUTED = "#8b95a5"       # secondary text


def status_color(status: str) -> str:
    return STATUS.get((status or "missing").strip().lower(), STATUS["missing"])
