"""Test bootstrap for plain ``unittest discover`` (no pytest dependency).

Importing this package inserts the src/ layout onto sys.path so the
loan_schedule package resolves without an installation.  Subprocess-based
tests still set PYTHONPATH explicitly inside their own call helpers.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
