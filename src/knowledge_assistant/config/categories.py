"""Configuration classification.

Every setting belongs to exactly one category, and the category determines *how it is handled*,
not merely how sensitive it looks. The four categories exist because two distinct risks are
routinely conflated:

* ``SECRET`` covers values that grant access (passwords, tokens, keys). Leaking one is an
  incident. They are never logged, never serialised, never echoed in an error message.
* ``SECURITY_SENSITIVE`` covers values that are *not* credentials but whose disclosure tells an
  attacker about the deployment (which database, which bind address, whether debug is on, which
  auth mode). Leaking one is reconnaissance. These must never appear in a **public** response,
  and in production must not appear in logs either.

The mistake this taxonomy prevents: a settings dump that redacts passwords and happily prints
the database hostname, and a health endpoint that reports the full configuration.

The classification is a frozen mapping, not a convention, so it can be enumerated and tested.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = ["ConfigCategory", "CATEGORY_RULES"]


class ConfigCategory(enum.StrEnum):
    """How a configuration value may be handled."""

    #: Safe to return from an unauthenticated endpoint. Today this is the service name and
    #: environment reported by ``/version`` - the only values an anonymous caller learns.
    #: Adding to this set is a deliberate act: it is an unauthenticated disclosure decision.
    PUBLIC = "public"

    #: Safe to log. Operational facts with no security consequence, e.g. log level, whether
    #: readiness includes the database probe.
    NON_SENSITIVE = "non_sensitive"

    #: Grants access. Never logged, never serialised, never returned by any endpoint.
    SECRET = "secret"  # noqa: S105 - this IS the word "secret"; a category name, not a credential

    #: Not a credential, but disclosure is reconnaissance. Never returned from any endpoint;
    #: suppressed in logs when the environment is production.
    SECURITY_SENSITIVE = "security_sensitive"


#: Handling rules per category, stated once so tests and docs agree.
CATEGORY_RULES: Final[dict[ConfigCategory, dict[str, bool | str]]] = {
    ConfigCategory.PUBLIC: {
        "may_be_returned_by_public_endpoint": True,
        "may_be_logged": True,
        "may_be_serialised_in_dumps": True,
        "masked_in_dumps": False,
    },
    ConfigCategory.NON_SENSITIVE: {
        "may_be_returned_by_public_endpoint": False,
        "may_be_logged": True,
        "may_be_serialised_in_dumps": True,
        "masked_in_dumps": False,
    },
    ConfigCategory.SECRET: {
        "may_be_returned_by_public_endpoint": False,
        "may_be_logged": False,
        "may_be_serialised_in_dumps": True,
        "masked_in_dumps": True,
    },
    ConfigCategory.SECURITY_SENSITIVE: {
        "may_be_returned_by_public_endpoint": False,
        "may_be_logged": False,
        "may_be_serialised_in_dumps": True,
        "masked_in_dumps": True,
    },
}

#: Placeholder rendered instead of a masked value. Must not be a plausible value.
MASK = "***"


def mask_value(category: ConfigCategory, value: object) -> object:
    """Return ``value`` or a mask, according to the category's dump rule.

    Args:
        category: Category of the value.
        value: Value to expose.

    Returns:
        The original value if the category permits serialisation unmasked, otherwise ``MASK``.

    """
    if CATEGORY_RULES[category]["masked_in_dumps"]:
        return MASK
    return value
