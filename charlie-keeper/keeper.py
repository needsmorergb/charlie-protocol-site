"""Entry point for the production copy. Everything it does lives in the
`indexer` package shipped next to it; this file only makes `python keeper.py`
work from this folder, wherever the folder is."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indexer.keeper import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
