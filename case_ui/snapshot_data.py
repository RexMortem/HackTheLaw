"""
Snapshot the pipeline output for deploy.

out/ is gitignored and absent on the Render host, so the served app reads a
committed copy at case_ui/data/matrix.json. Run this after the pipeline
(extract.py -> build_matrix.py) regenerates out/matrix.json, then commit:

    python case_ui/snapshot_data.py
    git add case_ui/data/matrix.json && git commit -m "Refresh matrix snapshot"
"""

import shutil
import sys
import pathlib

_HERE = pathlib.Path(__file__).parent
SRC = _HERE.parent / "out" / "matrix.json"
DST = _HERE / "data" / "matrix.json"


def main() -> None:
    if not SRC.exists():
        sys.exit(f"ERROR: {SRC} not found. Run extract.py then build_matrix.py first.")
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)
    print(f"Snapshotted {SRC} -> {DST} ({DST.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
