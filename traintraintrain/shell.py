"""Filling placeholders into shell commands without letting a value run as shell code.

Values written in train.yaml (the train, platform and stop names, and platform vars) go in
as written, so a var can hold several arguments. Every other value can come from outside
the config (a CLI argument, the web UI, a recorded live version, an API response read by an
earlier step) and is shell-quoted: `v1.2.0` stays `v1.2.0`, `$(rm -rf /)` becomes
`'$(rm -rf /)'`. Empty values stay empty. Every value is also in the environment as `DT_*`.
"""
from __future__ import annotations

import shlex
from collections.abc import Iterable

CONFIG_PLACEHOLDERS = frozenset({"train", "platform", "stop", "region", "wave"})
# Built-in placeholders that are never config values, even if a platform var shares the name.
EXTERNAL_PLACEHOLDERS = frozenset({"version", "previous_version", "sha", "previous_sha", "departure", "status"})


def quote(value: str) -> str:
    return shlex.quote(value) if value else value


def quote_values(values: dict[str, str], config_vars: Iterable[str] = ()) -> dict[str, str]:
    """`values` with everything that doesn't come from train.yaml shell-quoted."""
    names = set(config_vars)
    trusted = (CONFIG_PLACEHOLDERS | names | {f"vars.{k}" for k in names}) - EXTERNAL_PLACEHOLDERS
    return {k: v if k in trusted else quote(v) for k, v in values.items()}
