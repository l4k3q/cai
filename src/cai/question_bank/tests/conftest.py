from __future__ import annotations

import sys
import types
from pathlib import Path


QUESTION_BANK_ROOT = Path(__file__).resolve().parents[1]
CAI_OPS_ROOT = QUESTION_BANK_ROOT.parent
if str(CAI_OPS_ROOT) not in sys.path:
    sys.path.insert(0, str(CAI_OPS_ROOT))

if "cai" not in sys.modules:
    cai_package = types.ModuleType("cai")
    cai_package.__path__ = [str(CAI_OPS_ROOT)]
    sys.modules["cai"] = cai_package
