"""
Case UI — Proof Matrix web server.
Pure stdlib: no third-party dependencies.

Run:
    python case_ui/app.py

Then open: http://localhost:8001

Data file read (relative to the repo root, i.e. one level above this file):
    out/matrix.json — full matrix (from build_matrix.py)

Goals file written to:
    case_ui/data/goals.json
"""

import json
import pathlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = 8001

# Resolve paths relative to the repo root (parent of this file's directory).
_HERE = pathlib.Path(__file__).parent
REPO_ROOT = _HERE.parent
OUT_DIR = REPO_ROOT / "out"
STATIC_DIR = _HERE
DATA_DIR = _HERE / "data"

GOALS_FILE = DATA_DIR / "goals.json"

# In-memory goals cache — populated on first read or on POST /api/goals.
_GOALS: dict | None = None


def _out(name: str) -> pathlib.Path:
    return OUT_DIR / name


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------
def _load_matrix() -> tuple[list, str | None]:
    """Returns (matrix_list, error_string)."""
    path = _out("matrix.json")
    if not path.exists():
        return [], (
            "out/matrix.json not found. Run the pipeline first:\n"
            "  python extract.py\n"
            "  python build_matrix.py"
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), None


def _load_goals() -> dict:
    """Load goals from disk (lazy), with in-memory cache. Returns the goals dict."""
    global _GOALS
    if _GOALS is not None:
        return _GOALS
    if GOALS_FILE.exists():
        try:
            with open(GOALS_FILE, encoding="utf-8") as fh:
                _GOALS = json.load(fh)
            return _GOALS
        except (json.JSONDecodeError, OSError):
            pass
    # No file or unreadable — return sentinel indicating no goals saved yet.
    return {}


def _save_goals(payload: dict) -> dict:
    """Persist goals to disk and update in-memory cache."""
    global _GOALS
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(GOALS_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _GOALS = payload
    return _GOALS


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/matrix":
            self._api_matrix()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/goals":
            self._api_goals_get()
        else:
            self._404()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/goals":
            self._api_goals_post()
        else:
            self._404()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ---- static files -------------------------------------------------------

    def _serve_file(self, filepath: pathlib.Path, content_type: str):
        if not filepath.exists():
            self._404()
            return
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    # ---- API ----------------------------------------------------------------

    def _api_matrix(self):
        """GET /api/matrix — full matrix with summary stats."""
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return

        # Compute summary
        counts = {"supported": 0, "contested": 0, "undermined": 0, "MISSING": 0}
        for row in matrix:
            s = row.get("status", "MISSING")
            counts[s] = counts.get(s, 0) + 1

        total = sum(counts.values()) or 1
        # Trial readiness: supported=1.0, contested=0.5, undermined=0.0, MISSING=0.0
        readiness = round(
            (counts["supported"] * 1.0 + counts["contested"] * 0.5) / total * 100,
            1,
        )

        self._json_response(200, json.dumps({
            "ok": True,
            "summary": {
                "total": total,
                "counts": counts,
                "trial_readiness": readiness,
            },
            "matrix": matrix,
        }))

    def _api_status(self):
        """GET /api/status — lightweight health check."""
        self._json_response(200, json.dumps({
            "ok": True,
            "matrix_json_exists": _out("matrix.json").exists(),
            "goals_exist": GOALS_FILE.exists(),
        }))

    def _api_goals_get(self):
        """GET /api/goals — return current saved goals (or empty sentinel)."""
        goals = _load_goals()
        self._json_response(200, json.dumps({
            "ok": True,
            "has_goals": bool(goals),
            "goals": goals,
        }))

    def _api_goals_post(self):
        """POST /api/goals — accept and persist goals JSON."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response(400, json.dumps({"ok": False, "error": "Empty body"}))
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json_response(400, json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}))
            return

        goals_list = payload.get("goals")
        if not isinstance(goals_list, list):
            self._json_response(400, json.dumps({"ok": False, "error": "'goals' must be a list"}))
            return

        # Normalise: ensure each entry has id + text.
        normalised = []
        for i, g in enumerate(goals_list):
            if isinstance(g, str):
                normalised.append({"id": f"g{i + 1}", "text": g.strip()})
            elif isinstance(g, dict) and g.get("text"):
                normalised.append({
                    "id": g.get("id", f"g{i + 1}"),
                    "text": g["text"].strip(),
                })
        # Filter empties.
        normalised = [g for g in normalised if g["text"]]

        if not normalised:
            self._json_response(400, json.dumps({"ok": False, "error": "No non-empty goals provided"}))
            return

        record = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "goals": normalised,
        }
        _save_goals(record)

        self._json_response(200, json.dumps({"ok": True, "saved": record}))

    # ---- helpers ------------------------------------------------------------

    def _json_response(self, status: int, payload: str):
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _404(self):
        self._json_response(404, json.dumps({"error": "Not found"}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    server = HTTPServer(("", PORT), Handler)
    print(f"Proof Matrix running at http://localhost:{PORT}")
    print(f"Data directory: {OUT_DIR.resolve()}")
    print(f"Goals file: {GOALS_FILE.resolve()}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
