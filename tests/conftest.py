"""Shared test configuration.

src/main.py is written to run as a script: it inserts the repo root on
sys.path and then does `from db import connection`, which only resolves when
src/ is itself on the path. Putting src/ on the path here makes
`import src.main` work in tests without every test file repeating it.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, REPO_ROOT)
