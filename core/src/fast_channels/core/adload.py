"""Ad-load price λ per pod slot: the viewer-retention cost of one more ad, in US dollars per insertion.

``/select`` runs a paid ad in slot *j* only if its accepted net value beats λ(j), and fills a slot with a house ad only
where λ(j) is 0 (:mod:`fast_channels.core.pds.pod`). The PDS and the offline simulator both import this module, so
both price ad load the same way. Until the ad-load model is built, λ is 0 for every slot: every slot is wanted, as in
a plain FAST channel.
"""

from decimal import Decimal


def slot_lambdas(slots: int) -> tuple[Decimal, ...]:
    """λ for each of ``slots`` slots of one pod: 0 for every slot."""
    if slots < 0:
        raise ValueError(f"slots must not be negative, got {slots}")
    return (Decimal(0),) * slots
