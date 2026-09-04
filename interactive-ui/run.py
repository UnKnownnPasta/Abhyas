#!/usr/bin/env python3
#   python run.py                     interactive menu on the sheets in data/
#   python run.py --travel a.xlsx --incidents b.xlsx
#   python run.py --serve             skip the menu, serve on :8000
#   python run.py --check             build + self test, then exit

import sys

from abhyas.cli import main

if __name__ == "__main__":
    sys.exit(main())
