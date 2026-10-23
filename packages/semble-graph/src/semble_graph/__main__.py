"""Allow ``python -m semble_graph`` to run the CLI."""

from semble_graph.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
