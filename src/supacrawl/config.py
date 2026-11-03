"""Typed configuration for supacrawl: a GUI-tunable settings schema, an env-only
secrets model, and a local TOML store.

This is the control-plane seam (#138). A separate observability/control-plane
dashboard consumes ``config_schema()`` to render a settings form, and reads or
writes the store through ``load_config()`` / ``save_config()`` — one load/save
API the CLI and a GUI share. The CLI manages it with ``supacrawl config``.

Two models, by design:

- ``SupacrawlConfig`` — the tunable scrape/search knobs, each annotated with
  ``x-ui`` metadata (``group`` / ``order`` / ``widget`` / ``help`` and an
  optional ``visible_when``) so a GUI can render the form straight from
  ``model_json_schema()``. No secret ever lives in this model.
- ``SupacrawlSecrets`` — credentials, read from the environment, carrying **no**
  ``x-ui`` so they are structurally absent from the GUI schema. The store never
  persists them, and their values are never returned — only whether each is set.

Precedence, lowest to highest: model default < stored TOML < environment. A
caller's per-invocation argument (a CLI flag) overrides all three at the call
site; this module resolves the standing baseline beneath it.

The settings under ``mcp/config.py`` remain the MCP server's deployment-infra
loader (allowed origins, service name, log level); unifying the overlapping
scrape knobs with this model is the runtime-adoption step that belongs with the
dashboard build, not this seam.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

_ENV_PREFIX = "SUPACRAWL_"
_DEFAULT_CONFIG_PATH = Path("~/.supacrawl/config.toml")


def _ui(
    *,
    group: str,
    order: int,
    widget: str,
    help: str,
    visible_when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``json_schema_extra`` carrying GUI render metadata for one field.

    Mirrors the sister-project (ragify) ``x-ui`` convention so a dashboard can
    render a settings form directly from the emitted JSON schema.

    Args:
        group: Logical group the field belongs to (form section).
        order: Sort order within the group (gaps of 10 leave room to insert).
        widget: Suggested control — ``toggle`` / ``slider`` / ``number`` /
            ``dropdown`` / ``text`` / ``tags`` / ``path``.
        help: User-facing help text.
        visible_when: Optional conditional-visibility rule, e.g.
            ``{"metrics": True}``.

    Returns:
        A dict suitable for a Pydantic ``Field(json_schema_extra=...)``.
    """
    ui: dict[str, Any] = {"group": group, "order": order, "widget": widget, "help": help}
    if visible_when is not None:
        ui["visible_when"] = visible_when
    return {"x-ui": ui}


class SupacrawlConfig(BaseModel):
    """GUI-tunable scrape and search defaults, annotated for a settings form.

    Every field carries ``x-ui`` metadata. No secrets — credentials live in
    ``SupacrawlSecrets``. These are standing defaults; a per-request CLI flag or
    API argument still overrides them at the call site.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # --- Browser ---------------------------------------------------------
    timeout: int = Field(
        default=30000,
        ge=1000,
        le=300000,
        title="Page timeout (ms)",
        json_schema_extra=_ui(
            group="browser",
            order=10,
            widget="slider",
            help="How long to wait for a page to load before giving up, in milliseconds.",
        ),
    )
    headless: bool = Field(
        default=True,
        title="Headless",
        json_schema_extra=_ui(
            group="browser",
            order=20,
            widget="toggle",
            help="Run the browser without a visible window. Turn off only for debugging.",
        ),
    )
    wait_until: Literal["domcontentloaded", "load", "networkidle"] = Field(
        default="domcontentloaded",
        title="Wait until",
        json_schema_extra=_ui(
            group="browser",
            order=30,
            widget="dropdown",
            help="Page readiness signal to wait for. 'networkidle' is the most thorough but the slowest.",
        ),
    )
    user_agent: str | None = Field(
        default=None,
        title="User agent",
        json_schema_extra=_ui(
            group="browser",
            order=40,
            widget="text",
            help="Override the browser User-Agent string. Leave blank to use the engine default.",
        ),
    )

    # --- Locale ----------------------------------------------------------
    locale: str = Field(
        default="en-US",
        title="Locale",
        json_schema_extra=_ui(
            group="locale",
            order=10,
            widget="text",
            help="Browser locale, e.g. en-AU or de-DE. Maps to the Accept-Language header.",
        ),
    )
    timezone: str = Field(
        default="UTC",
        title="Timezone",
        json_schema_extra=_ui(
            group="locale",
            order=20,
            widget="text",
            help="Browser timezone, e.g. Australia/Brisbane or Europe/Berlin.",
        ),
    )

    # --- Anti-bot --------------------------------------------------------
    engine: Literal["playwright", "patchright", "camoufox"] | None = Field(
        default=None,
        title="Engine",
        json_schema_extra=_ui(
            group="anti_bot",
            order=10,
            widget="dropdown",
            help="Pin a browser engine. Leave unset to let supacrawl auto-escalate from the cheapest that works.",
        ),
    )
    stealth: bool = Field(
        default=False,
        title="Stealth",
        json_schema_extra=_ui(
            group="anti_bot",
            order=20,
            widget="toggle",
            help="Start with enhanced anti-bot evasion (Patchright). Usually unnecessary — escalation handles it.",
        ),
    )
    escalate: bool = Field(
        default=True,
        title="Auto-escalate",
        json_schema_extra=_ui(
            group="anti_bot",
            order=30,
            widget="toggle",
            help="Automatically climb the stealth/engine ladder when a result looks blocked or thin.",
        ),
    )
    solve_captcha: bool = Field(
        default=False,
        title="Solve CAPTCHAs",
        json_schema_extra=_ui(
            group="anti_bot",
            order=40,
            widget="toggle",
            help="Attempt CAPTCHA solving (needs a CAPTCHA API key). Each solve costs money.",
        ),
    )

    # --- Content ---------------------------------------------------------
    only_main_content: bool = Field(
        default=True,
        title="Main content only",
        json_schema_extra=_ui(
            group="content",
            order=10,
            widget="toggle",
            help="Strip navigation, headers, and footers, keeping the main article body.",
        ),
    )

    # --- Search ----------------------------------------------------------
    search_providers: str | None = Field(
        default=None,
        title="Search providers",
        json_schema_extra=_ui(
            group="search",
            order=10,
            widget="tags",
            help="Ordered, comma-separated provider fallback chain, e.g. brave,tavily,serper,duckduckgo.",
        ),
    )
    search_provider: Literal["duckduckgo", "brave"] = Field(
        default="brave",
        title="Legacy single provider",
        json_schema_extra=_ui(
            group="search",
            order=20,
            widget="dropdown",
            help="Single provider used when no fallback chain is set. Prefer the providers chain above.",
        ),
    )
    search_rate_limit: float | None = Field(
        default=None,
        ge=0.1,
        le=100.0,
        title="Search rate limit (req/s)",
        json_schema_extra=_ui(
            group="search",
            order=30,
            widget="number",
            help="Throttle search requests per second. Leave blank to use each provider's default.",
        ),
    )

    # --- Memory ----------------------------------------------------------
    strategy_memory: bool = Field(
        default=True,
        title="Per-domain memory",
        json_schema_extra=_ui(
            group="memory",
            order=10,
            widget="toggle",
            help="Remember the cheapest working strategy per domain and seed the next visit with it.",
        ),
    )

    # --- Telemetry -------------------------------------------------------
    metrics: bool = Field(
        default=True,
        title="Field telemetry",
        json_schema_extra=_ui(
            group="telemetry",
            order=10,
            widget="toggle",
            help="Record one quality/usage event per scrape and search to a local log.",
        ),
    )
    metrics_full_url: bool = Field(
        default=False,
        title="Log full URLs",
        json_schema_extra=_ui(
            group="telemetry",
            order=20,
            widget="toggle",
            visible_when={"metrics": True},
            help="Log full URLs and search queries instead of just the registrable domain. Off by default for privacy.",
        ),
    )
    # --- Cache -----------------------------------------------------------
    cache_dir: str | None = Field(
        default=None,
        title="Cache directory",
        json_schema_extra=_ui(
            group="cache",
            order=10,
            widget="path",
            help="Directory for cached scraped content. Leave blank to use the default location.",
        ),
    )


# The credential names supacrawl honours, mapped to their environment variable.
# Kept out of SupacrawlConfig so they never enter the GUI schema or the store.
_SECRET_ENV: dict[str, str] = {
    "captcha_api_key": "CAPTCHA_API_KEY",
    "brave_api_key": "BRAVE_API_KEY",
    "tavily_api_key": "TAVILY_API_KEY",
    "serper_api_key": "SERPER_API_KEY",
    "serpapi_api_key": "SERPAPI_API_KEY",
    "exa_api_key": "EXA_API_KEY",
    "searxng_username": "SEARXNG_USERNAME",
    "searxng_password": "SEARXNG_PASSWORD",
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "proxy": "SUPACRAWL_PROXY",
}


class SupacrawlSecrets(BaseModel):
    """Credentials, read from the environment only. Never rendered, never stored.

    This model deliberately carries no ``x-ui`` metadata, so it is invisible to
    the GUI settings schema. Use :meth:`configured` to report which secrets are
    present without ever returning a value.
    """

    model_config = ConfigDict(extra="ignore")

    captcha_api_key: str | None = None
    brave_api_key: str | None = None
    tavily_api_key: str | None = None
    serper_api_key: str | None = None
    serpapi_api_key: str | None = None
    exa_api_key: str | None = None
    searxng_username: str | None = None
    searxng_password: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    proxy: str | None = None

    @classmethod
    def from_env(cls) -> SupacrawlSecrets:
        """Build from the process environment."""
        return cls(**{field: os.environ.get(env) or None for field, env in _SECRET_ENV.items()})

    def configured(self) -> dict[str, bool]:
        """Report which secrets are set, by name, without exposing any value.

        Safe for a dashboard to surface; the values themselves never leave this
        process through this method.
        """
        return {field: getattr(self, field) is not None for field in _SECRET_ENV}


def config_path(path: str | Path | None = None) -> Path:
    """Resolve the config file path.

    Args:
        path: Explicit path override. When ``None``, uses
            ``SUPACRAWL_CONFIG_PATH`` if set, else ``~/.supacrawl/config.toml``.

    Returns:
        The expanded, absolute config file path.
    """
    if path is not None:
        return Path(path).expanduser()
    env_override = os.environ.get("SUPACRAWL_CONFIG_PATH")
    if env_override:
        return Path(env_override).expanduser()
    return _DEFAULT_CONFIG_PATH.expanduser()


def _read_stored(path: str | Path | None = None) -> dict[str, Any]:
    """Read the stored TOML config, or an empty dict when none exists.

    Fail-safe: a missing, unreadable, or malformed file degrades to defaults
    rather than crashing a scrape — config resolution runs on every command.
    """
    p = config_path(path)
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logging.getLogger(__name__).warning("Ignoring unreadable config %s: %s", p, exc)
        return {}


def _env_overrides() -> dict[str, str]:
    """Collect ``SUPACRAWL_<FIELD>`` environment overrides that are actually set.

    Only known config fields are read, and only when the variable is present, so
    an unset variable falls through to the stored value or the model default.
    """
    overrides: dict[str, str] = {}
    for name in SupacrawlConfig.model_fields:
        value = os.environ.get(f"{_ENV_PREFIX}{name.upper()}")
        if value is not None:
            overrides[name] = value
    return overrides


def load_config(path: str | Path | None = None) -> SupacrawlConfig:
    """Resolve the effective config: model default < stored TOML < environment.

    Args:
        path: Optional config file path override.

    Returns:
        A validated ``SupacrawlConfig`` with environment overriding the stored
        file overriding the built-in defaults. Pydantic coerces string values
        (from TOML or the environment) to each field's type.
    """
    merged = {**_read_stored(path), **_env_overrides()}
    return SupacrawlConfig.model_validate(merged)


def stored_config(path: str | Path | None = None) -> SupacrawlConfig:
    """Resolve config from defaults and the stored file only, ignoring the env.

    This is the layer the CLI mutates: ``config set`` persists into it without
    baking in transient environment overrides.
    """
    return SupacrawlConfig.model_validate(_read_stored(path))


def save_config(config: SupacrawlConfig, path: str | Path | None = None) -> Path:
    """Persist a config to the TOML store, writing only non-default values.

    Args:
        config: The config to persist.
        path: Optional config file path override.

    Returns:
        The path written.
    """
    p = config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in config.model_dump(exclude_defaults=True, mode="json").items() if v is not None}
    with p.open("wb") as f:
        tomli_w.dump(data, f)
    return p


def set_config_value(key: str, value: Any, path: str | Path | None = None) -> SupacrawlConfig:
    """Set one stored config value, validating it, and persist the stored layer.

    Operates on the stored layer only (defaults + file), so the environment is
    not baked into the file.

    Args:
        key: A field name of ``SupacrawlConfig``.
        value: The new value (a string is coerced to the field's type).
        path: Optional config file path override.

    Returns:
        The resulting stored ``SupacrawlConfig``.

    Raises:
        KeyError: When ``key`` is not a known config field.
        pydantic.ValidationError: When ``value`` is invalid for the field.
    """
    if key not in SupacrawlConfig.model_fields:
        raise KeyError(key)
    stored = _read_stored(path)
    # Validate the new value in context to coerce it and reject bad input.
    probe = SupacrawlConfig.model_validate({**stored, key: value})
    stored[key] = getattr(probe, key)
    config = SupacrawlConfig.model_validate(stored)
    save_config(config, path)
    return config


def config_schema() -> dict[str, Any]:
    """Return the JSON schema a GUI renders the settings form from.

    The ``x-ui`` metadata on each field survives verbatim into the schema. Only
    ``SupacrawlConfig`` is introspected, so no secret appears here.
    """
    return SupacrawlConfig.model_json_schema()
