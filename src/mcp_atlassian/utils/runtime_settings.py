"""Runtime-reloadable setting overrides read from an optional dotenv file.

Most settings are read from the environment once, when the server process
starts. A stdio server is launched by its client and lives for the whole
session, so changing one of those settings means restarting the client.

Pointing ``MCP_ATLASSIAN_RUNTIME_ENV_FILE`` at a dotenv file makes the
settings that consult this module resolvable per request instead. The file
uses the same key names as the environment variables it overrides, and takes
precedence over them. The file is re-read only when its modification time or
size changes, so a request that changes nothing costs a single ``stat``.

A configured file that cannot be read or parsed raises
:class:`RuntimeSettingsError` rather than falling back to the environment:
silently serving the opposite of a configured value is worse than refusing
the request.
"""

import logging
import os
from collections.abc import Mapping
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger("mcp-atlassian.runtime-settings")

RUNTIME_ENV_FILE_VAR = "MCP_ATLASSIAN_RUNTIME_ENV_FILE"

_EMPTY: Mapping[str, str] = {}


class RuntimeSettingsError(RuntimeError):
    """Raised when a configured runtime overrides file cannot be used."""


class RuntimeSettings:
    """Overrides read from a dotenv file, re-read when the file changes.

    The file is optional: a path that does not exist yields no overrides, which
    is the normal state on a host that never configured one.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Initialize the overrides reader.

        Args:
            path: Path to the dotenv file holding the overrides.
        """
        self._path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._overrides: Mapping[str, str] = _EMPTY

    @property
    def path(self) -> Path:
        """Path of the dotenv file this instance reads."""
        return self._path

    def overrides(self) -> Mapping[str, str]:
        """Return the current overrides, re-reading the file if it changed.

        Returns:
            Mapping of override key to raw string value. Empty when the file
            does not exist.

        Raises:
            RuntimeSettingsError: If the file exists but cannot be read or
                parsed.
        """
        try:
            stat_result = self._path.stat()
        except FileNotFoundError:
            self._stamp = None
            self._overrides = _EMPTY
            return self._overrides
        except OSError as exc:
            raise RuntimeSettingsError(
                f"Cannot read runtime settings file {self._path}: {exc}"
            ) from exc

        stamp = (stat_result.st_mtime_ns, stat_result.st_size)
        if stamp != self._stamp:
            self._overrides = self._parse()
            self._stamp = stamp
        return self._overrides

    def get(self, key: str) -> str | None:
        """Return the override for `key`, or None when it is not set.

        Args:
            key: Override key, named like the environment variable it
                overrides.

        Returns:
            The raw string value, or None when the key has no override.

        Raises:
            RuntimeSettingsError: If the file exists but cannot be read or
                parsed.
        """
        return self.overrides().get(key)

    def _parse(self) -> Mapping[str, str]:
        """Parse the dotenv file, rejecting entries without a value.

        The file is read here rather than by path, because ``dotenv_values``
        reports an unreadable path as an empty result. That would quietly drop
        every override instead of refusing the request.
        """
        try:
            content = self._path.read_text()
        except OSError as exc:
            raise RuntimeSettingsError(
                f"Cannot read runtime settings file {self._path}: {exc}"
            ) from exc

        parsed = dotenv_values(stream=StringIO(content))
        valueless = sorted(key for key, value in parsed.items() if value is None)
        if valueless:
            raise RuntimeSettingsError(
                f"Malformed runtime settings file {self._path}: "
                f"no value for {', '.join(valueless)}"
            )

        logger.debug("Loaded %d override(s) from %s", len(parsed), self._path)
        return dict(parsed)  # type: ignore[arg-type]


_default: RuntimeSettings | None = None


def get_runtime_settings() -> RuntimeSettings | None:
    """Return the process-wide overrides reader, or None when none is configured.

    Returns:
        A :class:`RuntimeSettings` for the path in
        ``MCP_ATLASSIAN_RUNTIME_ENV_FILE``, or None when that variable is
        unset or empty.
    """
    global _default

    path = os.getenv(RUNTIME_ENV_FILE_VAR)
    if not path:
        _default = None
        return None
    if _default is None or str(_default.path) != str(Path(path)):
        _default = RuntimeSettings(path)
    return _default


def get_runtime_override(key: str) -> str | None:
    """Return the configured override for `key`, or None when there is none.

    Args:
        key: Override key, named like the environment variable it overrides.

    Returns:
        The raw string value, or None when no overrides file is configured or
        the key has no override.

    Raises:
        RuntimeSettingsError: If a configured file cannot be read or parsed.
    """
    settings = get_runtime_settings()
    if settings is None:
        return None
    return settings.get(key)


def reset_runtime_settings() -> None:
    """Discard the cached overrides reader, forcing the next read to re-resolve."""
    global _default

    _default = None
