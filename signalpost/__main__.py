import os
import sys

from .cli import main

code = main()
sys.stdout.flush()
sys.stderr.flush()
# Results are already written; do not wait for research threads that are still inside a network timeout.
os._exit(code)
