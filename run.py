#!/usr/bin/env python3
"""Entry point.  python3 run.py  |  python3 run.py analyse"""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("analyse", "analyze"):
        from bot.analyzer import analyse
        analyse()
    else:
        from bot.copybot import run
        run()
