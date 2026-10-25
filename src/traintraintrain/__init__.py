"""Region-by-region production deployment train with configurable time windows."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("traintraintrain")  # the one source of truth is pyproject.toml
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
