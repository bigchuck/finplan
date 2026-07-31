"""Launch the CLI without `-m`:  python run_cli.py <file> [scenario]

cli.py uses package-relative imports, which only resolve when it's loaded as
`finplan.cli` (not run as a bare script). This shim puts …/src on the path,
imports the package properly, and hands off to cli.main — so an IDE Run button
pointed here works, and file-path args stay relative to the repo root.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent          # …/src (finplan importable)
sys.path.insert(0, str(SRC))

from finplan.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())