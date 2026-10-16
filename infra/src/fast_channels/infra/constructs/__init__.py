"""Reusable FAST Channels constructs (CONTRACTS §F); import them from their modules, e.g.
``from fast_channels.infra.constructs.storage import MediaStorage``.

Planes: ``storage``, ``encoding`` (ladders, ingest pipeline, kits), ``channel_assembly`` (origin, ad insertion,
channels, as-run, reaper), ``delivery``, ``observability`` and ``operator``. Building blocks they share:
``functions`` (``FastFunction`` and the stage's runtime defaults), ``events`` (the domain event bus), ``alerts``
(topic and alarm recipes), ``logs`` (service log groups and vended-log deliveries), ``queues`` (dead-letter
queues) and ``_provider`` (custom-resource providers)."""
