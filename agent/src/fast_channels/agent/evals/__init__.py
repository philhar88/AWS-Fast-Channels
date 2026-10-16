"""Model conformance suite for the scheduling agent (``scripts/eval_model.py``).

Ported from the v3 model benchmark (models-ui.md §2): the same agentic scheduling tasks, now on the v3 tool
surface where tools do the arithmetic and the model only chooses. A model should pass every run before it is
allowed into ``ALLOWED_MODELS`` / the ModelSpec. Runs go through the real agent (Strands loop, skills, approval
hooks, AG-UI translation) against either a local fake tool server or a live Gateway.
"""
