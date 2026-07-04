"""
Shared pytest fixtures / path setup for the Image Toolbox test suite.

The app is not a package: its modules live flat in scripts/ and import each
other by bare name (`import db`, `import notifications`, ...). So the one thing
every test needs is scripts/ on sys.path. We add it here, once, before any test
module is collected.

The pod/ daemons (deadman.py) live outside scripts/, so we add the repo root too
and import them as `pod.deadman` (pod/ has no __init__.py, but a namespace
package import works on Python 3).
"""

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(_TESTS_DIR)
SCRIPTS_DIR = os.path.join(APP_ROOT, "scripts")

for _p in (SCRIPTS_DIR, APP_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
