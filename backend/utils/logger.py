"""
utils/logger.py — Structured, colored logging with file rotation.
"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime

LOG_DIR = Path("../logs")
LOG_DIR.mkdir(exist_ok=True)

DATE_STR = datetime.now().strftime("%Y%m%d")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger                  # Already configured

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (colored via ANSI)
    class ColorFormatter(logging.Formatter):
        COLORS = {
            logging.DEBUG:    "\033[36m",    # Cyan
            logging.INFO:     "\033[32m",    # Green
            logging.WARNING:  "\033[33m",    # Yellow
            logging.ERROR:    "\033[31m",    # Red
            logging.CRITICAL: "\033[35m",    # Magenta
        }
        RESET = "\033[0m"

        def format(self, record):
            color = self.COLORS.get(record.levelno, "")
            record.levelname = f"{color}{record.levelname}{self.RESET}"
            return super().format(record)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ColorFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    ))

    # File handler with rotation
    file_handler = RotatingFileHandler(
        LOG_DIR / f"algotrader_{DATE_STR}.log",
        maxBytes=10 * 1024 * 1024,    # 10 MB
        backupCount=7,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
