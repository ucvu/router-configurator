from __future__ import annotations

import logging
import sys
from typing import TextIO

from .config import Config


class RedactingFormatter(logging.Formatter):
    def __init__(self, config: Config) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.config = config

    def format(self, record: logging.LogRecord) -> str:
        return self.config.redact(super().format(record))


def configure_logging(config: Config, stream: TextIO | None = None) -> None:
    """Send application events to stdout without enabling third-party debug logs."""
    logger = logging.getLogger("router_configurator")
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(RedactingFormatter(config))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
