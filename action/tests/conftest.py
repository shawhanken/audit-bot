"""Make the action's `src/` importable as top-level packages in tests."""

import os
import sys

_ACTION_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ACTION_SRC not in sys.path:
    sys.path.insert(0, _ACTION_SRC)
