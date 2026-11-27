"""Entry point: `zabbcomm` (the Docker ENTRYPOINT runs it) or `python -m zabbcomm`."""

import logging

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Proxy headers are left at uvicorn's defaults (on, trusting FORWARDED_ALLOW_IPS,
    # default 127.0.0.1): site detection depends on seeing the real client IP.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
