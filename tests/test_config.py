"""Tests for the config seam (#138): the GUI schema, the secrets split, the TOML
store, and env > stored > default precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from supacrawl.config import (
    SupacrawlConfig,
    SupacrawlSecrets,
    config_schema,
    load_config,
    save_config,
    set_config_value,
    stored_config,
)

# The x-ui keys every tunable field must carry, mirroring the ragify convention.
_REQUIRED_UI_KEYS = {"group", "order", "widget", "help"}


# x-ui schema contract — the dashboard renders the form from this


@pytest.mark.unit
def test_every_config_field_has_complete_x_ui() -> None:
    """Each tunable field must expose x-ui metadata with all required keys."""
    schema = config_schema()
    props = schema["properties"]
    assert set(props) == set(SupacrawlConfig.model_fields), "schema must cover every field"
    for name, prop in props.items():
        assert "x-ui" in prop, f"{name} is missing x-ui metadata"
        ui = prop["x-ui"]
        missing = _REQUIRED_UI_KEYS - set(ui)
        assert not missing, f"{name} x-ui missing keys: {missing}"
        assert isinstance(ui["order"], int)
        assert ui["help"], f"{name} has empty help text"


@pytest.mark.unit
def test_x_ui_survives_into_json_schema() -> None:
    """A representative field's widget hint reaches the emitted schema verbatim."""
    schema = config_schema()
    assert schema["properties"]["timeout"]["x-ui"]["widget"] == "slider"
    assert schema["properties"]["metrics_full_url"]["x-ui"]["visible_when"] == {"metrics": True}


@pytest.mark.unit
def test_secrets_model_carries_no_x_ui() -> None:
    """Secrets must be structurally absent from anything a GUI would render."""
    schema = SupacrawlSecrets.model_json_schema()
    for name, prop in schema["properties"].items():
        assert "x-ui" not in prop, f"secret {name} leaked x-ui metadata into the schema"


@pytest.mark.unit
def test_no_secret_field_appears_in_config_schema() -> None:
    """Credential names must never appear in the GUI config schema."""
    config_fields = set(config_schema()["properties"])
    secret_fields = set(SupacrawlSecrets.model_fields)
    assert config_fields.isdisjoint(secret_fields)


# Store round-trip


@pytest.mark.unit
def test_save_writes_only_non_defaults(tmp_path: Path) -> None:
    """The store stays minimal — only values that differ from the default."""
    path = tmp_path / "config.toml"
    save_config(SupacrawlConfig(engine="camoufox", timeout=60000), path=path)

    import tomllib

    with path.open("rb") as f:
        written = tomllib.load(f)
    assert written == {"engine": "camoufox", "timeout": 60000}


@pytest.mark.unit
def test_round_trip_preserves_values(tmp_path: Path) -> None:
    """A saved config reloads (from the file alone) with the same values."""
    path = tmp_path / "config.toml"
    save_config(SupacrawlConfig(headless=False, locale="en-AU", search_rate_limit=5.0), path=path)

    reloaded = stored_config(path=path)
    assert reloaded.headless is False
    assert reloaded.locale == "en-AU"
    assert reloaded.search_rate_limit == 5.0
    # Untouched fields fall back to the model default.
    assert reloaded.timeout == 30000


# Precedence: default < stored < env


@pytest.mark.unit
def test_default_when_nothing_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPACRAWL_TIMEOUT", raising=False)
    cfg = load_config(path=tmp_path / "absent.toml")
    assert cfg.timeout == 30000


@pytest.mark.unit
def test_stored_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPACRAWL_TIMEOUT", raising=False)
    path = tmp_path / "config.toml"
    save_config(SupacrawlConfig(timeout=12345), path=path)
    assert load_config(path=path).timeout == 12345


@pytest.mark.unit
def test_env_overrides_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    save_config(SupacrawlConfig(timeout=12345, engine="patchright"), path=path)
    monkeypatch.setenv("SUPACRAWL_TIMEOUT", "99000")
    cfg = load_config(path=path)
    assert cfg.timeout == 99000  # env wins
    assert cfg.engine == "patchright"  # stored still applies where env is absent


@pytest.mark.unit
def test_env_bool_coercion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A string env value coerces to the field's bool type."""
    monkeypatch.setenv("SUPACRAWL_HEADLESS", "false")
    monkeypatch.setenv("SUPACRAWL_STRATEGY_MEMORY", "0")
    cfg = load_config(path=tmp_path / "absent.toml")
    assert cfg.headless is False
    assert cfg.strategy_memory is False


# set_config_value


@pytest.mark.unit
def test_set_config_value_coerces_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    set_config_value("timeout", "45000", path=path)  # string in, int out
    assert stored_config(path=path).timeout == 45000


@pytest.mark.unit
def test_set_config_value_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        set_config_value("not_a_field", "x", path=tmp_path / "config.toml")


@pytest.mark.unit
def test_set_config_value_rejects_invalid_value(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        set_config_value("timeout", "not-a-number", path=tmp_path / "config.toml")


@pytest.mark.unit
def test_set_config_value_rejects_out_of_range(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        set_config_value("timeout", "10", path=tmp_path / "config.toml")  # below ge=1000


# Secrets


@pytest.mark.unit
def test_secrets_configured_reports_presence_not_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "super-secret-value")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    secrets = SupacrawlSecrets.from_env()
    configured = secrets.configured()
    assert configured["brave_api_key"] is True
    assert configured["tavily_api_key"] is False
    # The presence map must never carry the value itself.
    assert "super-secret-value" not in str(configured)


@pytest.mark.unit
def test_searxng_credentials_reported_in_secrets_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    """`config secrets` must answer "is my SearXNG credential picked up?".

    Reporting both halves is what makes a half-configured pair diagnosable from
    the CLI rather than only from a log line at request time.
    """
    monkeypatch.setenv("SEARXNG_USERNAME", "searxng-user-placeholder")
    monkeypatch.delenv("SEARXNG_PASSWORD", raising=False)
    secrets = SupacrawlSecrets.from_env()
    configured = secrets.configured()
    assert configured["searxng_username"] is True
    assert configured["searxng_password"] is False
    assert "searxng-user-placeholder" not in str(configured)


@pytest.mark.unit
def test_brokered_searxng_credential_is_not_a_secret_field() -> None:
    """The catalogue NAME is an identifier, not a credential.

    Listing it among the secrets would say a value is present when none is —
    the value never leaves the broker.
    """
    assert "searxng_portcullis_credential" not in SupacrawlSecrets.model_fields


@pytest.mark.unit
def test_config_secrets_explains_the_brokered_searxng_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker-configured operator must not be sent after a missing env var.

    With the MCP server fetching the pair from the broker, `SEARXNG_USERNAME` and
    `SEARXNG_PASSWORD` are absent on purpose; a bare "-" against each reads as a
    misconfiguration and sends the operator after the wrong problem.
    """
    from click.testing import CliRunner

    from supacrawl.cli.config import config_secrets

    monkeypatch.delenv("SEARXNG_USERNAME", raising=False)
    monkeypatch.delenv("SEARXNG_PASSWORD", raising=False)
    monkeypatch.setenv("SEARXNG_PORTCULLIS_CREDENTIAL", "searxng-credential-name-placeholder")

    result = CliRunner().invoke(config_secrets, [])

    assert result.exit_code == 0
    assert "searxng-credential-name-placeholder" in result.output
    assert "secrets broker" in result.output
    assert "expected to be unset" in result.output


@pytest.mark.unit
def test_config_secrets_says_nothing_about_the_broker_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default install must not be told about a path it does not use."""
    from click.testing import CliRunner

    from supacrawl.cli.config import config_secrets

    monkeypatch.delenv("SEARXNG_PORTCULLIS_CREDENTIAL", raising=False)

    result = CliRunner().invoke(config_secrets, [])

    assert result.exit_code == 0
    assert "searxng_portcullis_credential" not in result.output
    assert "broker" not in result.output
