"""`python -m gwstock [run|bind-account|smoke|status|health]`; import nie robi niczego (bez sieci, bez plików)."""

from gwstock.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
