"""The agents module (M9, ADR-0023): the roster's agents act inside a declared autonomy envelope.

:mod:`.envelope` defines the envelope (what each agent may do, how often, and what needs a human) and the deterministic
verdict on one action; the Gateway's Cedar policies and the agent runner both follow it.
"""
