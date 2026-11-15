"""Serve command for the REST API."""

import click

from supacrawl.cli._common import app


@app.command("serve", help="Start the supacrawl REST API server.")
@click.option(
    "--host",
    default="0.0.0.0",
    envvar="SUPACRAWL_API_HOST",
    show_default=True,
    help="Host to bind to. Also reads SUPACRAWL_API_HOST env.",
)
@click.option(
    "--port",
    default=8308,
    envvar="SUPACRAWL_API_PORT",
    type=int,
    show_default=True,
    help="Port to listen on. Also reads SUPACRAWL_API_PORT env.",
)
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Enable auto-reload on code changes (development only).",
)
def serve(host: str, port: int, reload: bool) -> None:
    """Start the supacrawl REST API server using uvicorn.

    Requires the api extra: pip install supacrawl[api]

    Examples:
        supacrawl serve
        supacrawl serve --host 127.0.0.1 --port 8080
        supacrawl serve --reload
    """
    try:
        import uvicorn

        from supacrawl.api.app import create_app
    except ImportError:
        click.echo(
            "REST API requires extra dependencies. Install with: pip install supacrawl[api]",
            err=True,
        )
        raise SystemExit(1) from None

    if reload:
        uvicorn.run(
            "supacrawl.api.app:create_app",
            host=host,
            port=port,
            reload=True,
            factory=True,
        )
    else:
        uvicorn.run(create_app(), host=host, port=port)
