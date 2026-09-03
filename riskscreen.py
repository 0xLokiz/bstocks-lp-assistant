#!/usr/bin/env python3
"""bStocks LP Assistant -- thin CLI entry point.

The implementation lives in the bstocks_lp/ package next to this file (see README.md's
"Code layout" for a module-by-module map). MODEL.md derives the risk-adjusted model in full;
SKILL.md and README.md cover usage. Run `python riskscreen.py <command> --help` for CLI help.
"""

from bstocks_lp.cli import main

if __name__ == "__main__":
    main()
