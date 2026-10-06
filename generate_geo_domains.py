"""CLI entry point; generation never runs as part of an HTTP request."""

from router_configurator.generator import main

if __name__ == "__main__":
    raise SystemExit(main())
