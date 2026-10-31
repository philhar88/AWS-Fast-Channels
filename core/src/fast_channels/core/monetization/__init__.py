"""Desired state of MediaTailor Monetization Functions and the sibling playback configurations (the release module).

- :mod:`.functions`: ``PutFunction`` documents, FunctionIds (stems plus ``_rNNNN``), the PRE_SESSION session
  controls (``ps_ctl``) and executor builders.
- :mod:`.playback`: sibling playback configurations and the templated OpenRTB POST to Prebid Server.
- :mod:`.bundle`: the release bundle (every document, mapping and expected channel policy), the
  :class:`~.bundle.BundleContribution` interface other modules add trees through, the rollout strategies, the
  digest.
- :mod:`.validators`: rules R-F1 … R-S1 (DESIGN §7) and R-A1/R-A2; :mod:`.expressions` reads JSONata for them,
  against :mod:`.jsonata_allowlist`.

Pure: pydantic and the standard library. CDK renders the bundle at synth; the Release Controller validates and
applies it.
"""
