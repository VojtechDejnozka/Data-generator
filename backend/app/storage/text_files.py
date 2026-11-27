"""Read UTF-8 data while supporting files written in the old system encoding."""
import locale
from pathlib import Path


def read_saved_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding=locale.getencoding())
