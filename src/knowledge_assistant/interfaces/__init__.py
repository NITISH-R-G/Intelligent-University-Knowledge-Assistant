"""Interface layer: HTTP transport and the developer CLI.

**Invariants enforced by CI:** may import ``application``, ``domain``, ``config`` and
``observability``. Never imports ``infrastructure`` adapters directly - it receives use cases
from the composition root. That is what keeps the transport replaceable and keeps SQL out of
the web layer.

Interface-layer responsibilities and nothing else:

* Transport concerns: routing, content negotiation, request identity, body limits, error
  serialisation, security headers.
* Translating HTTP into application-level calls, and application errors into HTTP responses.

It does not contain business rules. Every ``if`` statement about what is true of the world
belongs in ``application`` or ``domain``.
"""