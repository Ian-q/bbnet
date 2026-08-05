"""Put src/ on sys.path so the tests import bbnet as a package.

Unnecessary once bbnet is pip-installed; harmless then, and required now
because CI runs pytest without installing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
