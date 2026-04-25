"""Allow `python -m src.cli` to behave like the installed CLI."""
import sys

from . import main


if __name__ == '__main__':
    sys.exit(main())
