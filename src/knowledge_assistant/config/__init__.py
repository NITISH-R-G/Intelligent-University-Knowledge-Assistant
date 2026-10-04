"""Configuration package.

**Invariants enforced by CI:** this package may be imported by ``infrastructure``,
``interfaces``, ``workers`` and the composition root, and by nothing in ``domain`` or
``application``. Configuration is an input to the system, not part of its rules: a domain rule
that reads an environment variable is no longer testable by passing an argument.

Configuration classification (PUBLIC / NON_SENSITIVE / SECRET / SECURITY_SENSITIVE) is defined
in ``knowledge_assistant.config.categories`` and documented in
``docs/03-architecture/CONFIGURATION.md``.
"""