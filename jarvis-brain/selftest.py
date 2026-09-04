from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise SystemExit(1)
print("\nJarvis Brain v0.3.0 autonomous ingestion contracts: PASS")
