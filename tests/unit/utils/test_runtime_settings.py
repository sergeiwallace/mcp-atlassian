"""Tests for the runtime settings overrides file."""

import os

import pytest

from mcp_atlassian.jira.config import JiraConfig
from mcp_atlassian.preprocessing.jira import JiraPreprocessor
from mcp_atlassian.utils import runtime_settings
from mcp_atlassian.utils.runtime_settings import (
    RUNTIME_ENV_FILE_VAR,
    RuntimeSettings,
    RuntimeSettingsError,
    get_runtime_override,
    reset_runtime_settings,
)


@pytest.fixture(autouse=True)
def _clear_cached_settings():
    """Keep the process-wide reader from leaking between tests."""
    reset_runtime_settings()
    yield
    reset_runtime_settings()


def _write(path, text):
    """Write `text` to `path` and return the path."""
    path.write_text(text)
    return path


def _bump_mtime(path, seconds=10):
    """Move a file's mtime forward so a change is visible regardless of granularity."""
    stat_result = path.stat()
    os.utime(
        path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + seconds * 10**9)
    )


class TestRuntimeSettings:
    """Test the overrides reader itself."""

    def test_reads_values_from_the_file(self, tmp_path):
        """A configured file's entries are returned as overrides."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=true\n"
        )

        assert RuntimeSettings(path).get("DISABLE_JIRA_MARKUP_TRANSLATION") == "true"

    def test_missing_file_yields_no_overrides(self, tmp_path):
        """A path that does not exist is the normal unused state, not an error."""
        settings = RuntimeSettings(tmp_path / "absent.env")

        assert settings.overrides() == {}
        assert settings.get("DISABLE_JIRA_MARKUP_TRANSLATION") is None

    def test_a_key_with_no_value_is_rejected(self, tmp_path):
        """A malformed entry fails closed and the error names the file."""
        path = _write(tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION\n")

        with pytest.raises(RuntimeSettingsError) as excinfo:
            RuntimeSettings(path).overrides()

        assert str(path) in str(excinfo.value)
        assert "DISABLE_JIRA_MARKUP_TRANSLATION" in str(excinfo.value)

    def test_an_unreadable_file_is_rejected(self, tmp_path):
        """A file that cannot be read fails closed and the error names it."""
        path = tmp_path / "overrides.env"
        path.mkdir()  # a directory cannot be read as a dotenv file

        with pytest.raises(RuntimeSettingsError) as excinfo:
            RuntimeSettings(path).overrides()

        assert str(path) in str(excinfo.value)

    def test_a_changed_file_is_picked_up(self, tmp_path):
        """Rewriting the file changes the value the same reader returns."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=false\n"
        )
        settings = RuntimeSettings(path)
        assert settings.get("DISABLE_JIRA_MARKUP_TRANSLATION") == "false"

        _write(path, "DISABLE_JIRA_MARKUP_TRANSLATION=true\n")
        _bump_mtime(path)

        assert settings.get("DISABLE_JIRA_MARKUP_TRANSLATION") == "true"

    def test_an_unchanged_file_is_parsed_once(self, tmp_path, monkeypatch):
        """Repeated reads of an unchanged file do not re-parse it."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=true\n"
        )
        calls = []
        real_dotenv_values = runtime_settings.dotenv_values

        def counting_dotenv_values(*args, **kwargs):
            calls.append(args)
            return real_dotenv_values(*args, **kwargs)

        monkeypatch.setattr(runtime_settings, "dotenv_values", counting_dotenv_values)
        settings = RuntimeSettings(path)

        for _ in range(5):
            assert settings.get("DISABLE_JIRA_MARKUP_TRANSLATION") == "true"

        assert len(calls) == 1

    def test_a_file_appearing_later_is_picked_up(self, tmp_path):
        """A reader created before the file exists sees it once it is written."""
        path = tmp_path / "overrides.env"
        settings = RuntimeSettings(path)
        assert settings.get("DISABLE_JIRA_MARKUP_TRANSLATION") is None

        _write(path, "DISABLE_JIRA_MARKUP_TRANSLATION=true\n")

        assert settings.get("DISABLE_JIRA_MARKUP_TRANSLATION") == "true"


class TestGetRuntimeOverride:
    """Test resolution through the launch-time path variable."""

    def test_no_configured_path_means_no_overrides(self, monkeypatch):
        """With the path variable unset, no file is consulted."""
        monkeypatch.delenv(RUNTIME_ENV_FILE_VAR, raising=False)

        assert get_runtime_override("DISABLE_JIRA_MARKUP_TRANSLATION") is None

    def test_reads_the_configured_path(self, tmp_path, monkeypatch):
        """The override comes from the file the path variable names."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=true\n"
        )
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(path))

        assert get_runtime_override("DISABLE_JIRA_MARKUP_TRANSLATION") == "true"

    def test_a_repointed_path_is_followed(self, tmp_path, monkeypatch):
        """Changing the path variable switches which file is read."""
        first = _write(tmp_path / "first.env", "DISABLE_JIRA_MARKUP_TRANSLATION=true\n")
        second = _write(
            tmp_path / "second.env", "DISABLE_JIRA_MARKUP_TRANSLATION=false\n"
        )

        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(first))
        assert get_runtime_override("DISABLE_JIRA_MARKUP_TRANSLATION") == "true"

        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(second))
        assert get_runtime_override("DISABLE_JIRA_MARKUP_TRANSLATION") == "false"


def _config(**overrides):
    """Build a minimal JiraConfig for resolution tests."""
    defaults = {
        "url": "https://example.atlassian.net",
        "auth_type": "basic",
        "username": "user@example.com",
        "api_token": "token",
    }
    return JiraConfig(**{**defaults, **overrides})


def _config_from_env(monkeypatch):
    """Build a JiraConfig through from_env, supplying the required connection vars."""
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USERNAME", "user@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    return JiraConfig.from_env()


class TestMarkupTranslationPrecedence:
    """Test file > environment > default for the markup translation setting."""

    def test_default_is_translation_enabled(self, monkeypatch):
        """With nothing configured, translation stays on."""
        monkeypatch.delenv(RUNTIME_ENV_FILE_VAR, raising=False)
        monkeypatch.delenv("DISABLE_JIRA_MARKUP_TRANSLATION", raising=False)

        assert _config().resolve_disable_jira_markup_translation() is False

    def test_environment_is_used_when_no_file_is_configured(self, monkeypatch):
        """The environment still applies when no overrides file is configured."""
        monkeypatch.delenv(RUNTIME_ENV_FILE_VAR, raising=False)
        monkeypatch.setenv("DISABLE_JIRA_MARKUP_TRANSLATION", "true")

        assert (
            _config_from_env(monkeypatch).resolve_disable_jira_markup_translation()
            is True
        )

    def test_missing_file_falls_through_to_the_environment(self, tmp_path, monkeypatch):
        """A configured but absent file leaves the environment in charge."""
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(tmp_path / "absent.env"))
        monkeypatch.setenv("DISABLE_JIRA_MARKUP_TRANSLATION", "true")

        assert (
            _config_from_env(monkeypatch).resolve_disable_jira_markup_translation()
            is True
        )

    def test_the_file_overrides_the_environment(self, tmp_path, monkeypatch):
        """The file wins over an opposing environment variable."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=false\n"
        )
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(path))
        monkeypatch.setenv("DISABLE_JIRA_MARKUP_TRANSLATION", "true")

        assert (
            _config_from_env(monkeypatch).resolve_disable_jira_markup_translation()
            is False
        )

    def test_the_file_overrides_the_default(self, tmp_path, monkeypatch):
        """The file wins over the built-in default."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=true\n"
        )
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(path))
        monkeypatch.delenv("DISABLE_JIRA_MARKUP_TRANSLATION", raising=False)

        assert (
            _config_from_env(monkeypatch).resolve_disable_jira_markup_translation()
            is True
        )

    def test_a_malformed_file_is_rejected_rather_than_falling_back(
        self, tmp_path, monkeypatch
    ):
        """Resolution fails closed instead of silently serving the opposite value."""
        path = _write(tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION\n")
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(path))
        monkeypatch.setenv("DISABLE_JIRA_MARKUP_TRANSLATION", "true")

        config = _config_from_env(monkeypatch)

        with pytest.raises(RuntimeSettingsError) as excinfo:
            config.resolve_disable_jira_markup_translation()

        assert str(path) in str(excinfo.value)


class TestLiveChangeIsObserved:
    """Test that a rewritten file changes behaviour without rebuilding anything."""

    def test_resolution_follows_the_file_without_rebuilding_the_config(
        self, tmp_path, monkeypatch
    ):
        """One JiraConfig instance reports the file's current value."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=false\n"
        )
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(path))
        config = _config()
        assert config.resolve_disable_jira_markup_translation() is False

        _write(path, "DISABLE_JIRA_MARKUP_TRANSLATION=true\n")
        _bump_mtime(path)

        assert config.resolve_disable_jira_markup_translation() is True

    def test_the_preprocessor_follows_the_file_without_being_rebuilt(
        self, tmp_path, monkeypatch
    ):
        """One preprocessor instance translates, then stops, as the file changes."""
        path = _write(
            tmp_path / "overrides.env", "DISABLE_JIRA_MARKUP_TRANSLATION=false\n"
        )
        monkeypatch.setenv(RUNTIME_ENV_FILE_VAR, str(path))
        config = _config()
        preprocessor = JiraPreprocessor(
            base_url=config.url,
            disable_translation=config.resolve_disable_jira_markup_translation,
        )

        assert preprocessor.markdown_to_jira("**bold**") == "*bold*"

        _write(path, "DISABLE_JIRA_MARKUP_TRANSLATION=true\n")
        _bump_mtime(path)

        assert preprocessor.markdown_to_jira("**bold**") == "**bold**"

    def test_a_static_value_still_works(self):
        """A plain bool keeps its existing meaning for callers that pass one."""
        preprocessor = JiraPreprocessor(
            base_url="https://example.atlassian.net", disable_translation=True
        )

        assert preprocessor.disable_translation is True
        assert preprocessor.markdown_to_jira("**bold**") == "**bold**"
