from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog


def configure_logging(
    level: str = "INFO",
    log_file: str = "logs/bot.log",
    rotate_daily: bool = True,
) -> None:
    """
    Set up structlog + stdlib logging.

    Console: colored key=value output in dev (when stderr is a TTY).
    File: always JSON, one event per line, daily rotation.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Ensure log directory exists
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = []

    # File handler — always JSON
    if rotate_daily:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            utc=True,
            encoding="utf-8",
        )
    else:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(log_level)
    handlers.append(file_handler)

    # Console handler — human-friendly when attached to a TTY
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    handlers.append(console_handler)

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        handlers=handlers,
        force=True,
    )

    is_tty = sys.stderr.isatty()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    # File formatter: always JSON
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    file_handler.setFormatter(file_formatter)

    # Console formatter: colored dev output or JSON
    if is_tty:
        console_processor: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        console_processor = structlog.processors.JSONRenderer()

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=console_processor,
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
