"""Flask CLI entry point without web-server startup initialization."""

from app import create_cli_app

app = create_cli_app()
