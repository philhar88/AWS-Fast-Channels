"""The viewer-IP rule: the viewer's address from the X-Forwarded-For chain Prebid Server received.

MediaTailor's ``{{session.client_ip}}`` is the leftmost X-Forwarded-For entry, and a client sets it by sending its own
header (a client-sent ``X-Forwarded-For: 203.0.113.7`` was reported as the client IP), so it is never used. Each hop
appends its TCP peer instead, which a client cannot forge. By the time an ad request reaches Prebid Server the chain is

    [client-supplied, ...] viewer [, viewer-side CloudFront] , MediaTailor egress , PBS-side CloudFront

- the rightmost entry is what the PBS load balancer appended: the PBS CloudFront's origin-facing address (the
  load balancer admits only CloudFront, and only with the CloudFront's secret header);
- left of it, the PBS CloudFront appended its own viewer, which is MediaTailor's egress. That address is also the
  ``CloudFront-Viewer-Address`` of the request when the PBS CloudFront forwards that header. MediaTailor's egress
  cannot be recognised by address range (it uses many EC2 addresses with no service tag), so it is found by position,
  or by matching ``CloudFront-Viewer-Address``;
- left of that, MediaTailor appended its own TCP peer: the viewer itself for a direct session, or the viewer-side
  CloudFront for a session through the CDN. CloudFront's origin-facing addresses are published
  (``CLOUDFRONT_ORIGIN_FACING`` in ip-ranges.json), so that hop is recognised by range and skipped;
- the next entry is the viewer.

Anything that does not fit gives no address, never a guess: the module then removes the header-derived ``device.ip``.
The address is not authenticated; it is what the chain says the viewer's connection came from.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Any, Final

type IpAddress = IPv4Address | IPv6Address
type IpNetwork = IPv4Network | IPv6Network

ORIGIN_FACING_SERVICE: Final = "CLOUDFRONT_ORIGIN_FACING"
"""ip-ranges.json service of the addresses CloudFront uses towards origins."""


class IpRule(StrEnum):
    """Outcome of the viewer-IP rule."""

    OK = "ok"
    NO_CHAIN = "no_chain"
    """No X-Forwarded-For."""
    SHORT_CHAIN = "short_chain"
    """Fewer hops than the path has: no entry is left for the viewer."""
    EGRESS_NOT_FOUND = "egress_not_found"
    """CloudFront-Viewer-Address is not in the chain."""
    UNEXPECTED_HOP = "unexpected_hop"
    """The hop right of MediaTailor's egress is not a CloudFront origin-facing address: the request did not come
    the way the rule expects."""
    INVALID_ENTRY = "invalid_entry"
    """The entry in the viewer's position, or CloudFront-Viewer-Address, is not an IP address."""
    NOT_GLOBAL = "not_global"
    """The entry in the viewer's position is not a globally routable address."""


@dataclass(frozen=True, slots=True)
class ViewerIp:
    """The viewer's address, or ``None`` with the reason the chain gave none."""

    ip: IpAddress | None
    rule: IpRule


def origin_facing_networks(ip_ranges: Mapping[str, Any]) -> tuple[IpNetwork, ...]:
    """CloudFront's origin-facing networks from an ip-ranges.json document (IPv4 and IPv6)."""
    entries: Iterable[Mapping[str, Any]] = [*ip_ranges.get("prefixes", ()), *ip_ranges.get("ipv6_prefixes", ())]
    return tuple(
        ip_network(entry.get("ip_prefix") or entry["ipv6_prefix"])
        for entry in entries
        if entry.get("service") == ORIGIN_FACING_SERVICE
    )


def _address(text: str) -> IpAddress | None:
    try:
        address = ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _viewer_address(header: str) -> IpAddress | None:
    """The address in ``CloudFront-Viewer-Address`` (``198.51.100.10:46532``, IPv6 likewise or bracketed)."""
    text = header.strip()
    if text.startswith("["):
        host, closed, rest = text[1:].partition("]")
        return _address(host) if closed and (not rest or (rest[:1] == ":" and rest[1:].isdigit())) else None
    host, colon, port = text.rpartition(":")
    if colon and port.isdigit() and (address := _address(host)) is not None:
        return address
    return _address(text)


def _origin_facing(address: IpAddress, networks: Sequence[IpNetwork]) -> bool:
    return any(address in network for network in networks)


def _egress(entries: list[str], cfva: str) -> int | IpRule:
    """Index of MediaTailor's egress: the rightmost occurrence of ``CloudFront-Viewer-Address`` when given, else the
    second entry from the right (left of the load balancer's CloudFront hop)."""
    if not cfva.strip():
        if len(entries) < 2:
            return IpRule.SHORT_CHAIN
        return len(entries) - 2 if _address(entries[-2]) is not None else IpRule.INVALID_ENTRY
    if (egress := _viewer_address(cfva)) is None:
        return IpRule.INVALID_ENTRY
    matches = [index for index, entry in enumerate(entries) if _address(entry) == egress]
    return matches[-1] if matches else IpRule.EGRESS_NOT_FOUND


def _left_of_egress(entries: list[str], cfva: str, origin_facing: Sequence[IpNetwork]) -> list[str] | IpRule:
    """The entries left of MediaTailor's egress, once the hop right of it is confirmed as the PBS CloudFront's."""
    index = _egress(entries, cfva)
    if isinstance(index, IpRule):
        return index
    hop = _address(entries[index + 1]) if index + 1 < len(entries) else None
    if hop is None or not _origin_facing(hop, origin_facing):
        return IpRule.UNEXPECTED_HOP
    return entries[:index]


def _viewer(entry: str) -> ViewerIp:
    if (viewer := _address(entry)) is None:
        return ViewerIp(None, IpRule.INVALID_ENTRY)
    if not viewer.is_global:
        return ViewerIp(None, IpRule.NOT_GLOBAL)
    return ViewerIp(viewer, IpRule.OK)


def viewer_ip(xff: str, cfva: str, origin_facing: Sequence[IpNetwork]) -> ViewerIp:
    """Apply the viewer-IP rule to Prebid Server's ``X-Forwarded-For`` and ``CloudFront-Viewer-Address``.

    Args:
        xff: the X-Forwarded-For header as Prebid Server received it.
        cfva: its CloudFront-Viewer-Address header, or an empty string when the PBS CloudFront does not forward it.
        origin_facing: CloudFront's origin-facing networks (:func:`origin_facing_networks`).
    """
    entries = [entry.strip() for entry in xff.split(",")] if xff.strip() else []
    if not entries:
        return ViewerIp(None, IpRule.NO_CHAIN)
    left = _left_of_egress(entries, cfva, origin_facing)
    if isinstance(left, IpRule):
        return ViewerIp(None, left)
    if left and (hop := _address(left[-1])) is not None and _origin_facing(hop, origin_facing):
        left = left[:-1]  # the viewer-side CloudFront
    return _viewer(left[-1]) if left else ViewerIp(None, IpRule.SHORT_CHAIN)
