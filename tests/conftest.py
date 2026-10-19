import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _pkg  # noqa: E402  (loads the plugin as a package before any test imports it)
