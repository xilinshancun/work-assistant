#!/usr/bin/env python3
"""Source-checkout compatibility entry; the evaluator implementation ships in the package."""
from pathlib import Path
import sys

# This explicit source entry works without installing the checkout. Installed
# users use `wa evaluate`, which resolves only their loaded runtime package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from work_assistant.evaluation import main


if __name__ == "__main__":
    raise SystemExit(main(entry_point="source_script"))
