"""The viewer-IP rule on the three chain shapes measured live, with and without CloudFront-Viewer-Address.

Addresses are arbitrary public ones (the rule refuses non-global addresses, so documentation ranges cannot stand in
for the viewer); the CloudFront ones are inside the origin-facing networks below.
"""

from ipaddress import IPv4Address, IPv6Address, ip_network

import pytest

from fast_channels.core.pds.xff import IpRule, ViewerIp, origin_facing_networks, viewer_ip

ORIGIN_FACING = (ip_network("130.176.0.0/18"), ip_network("2600:9000:1000::/36"))
VIEWER = "81.2.69.160"
CF_VIEWER_SIDE = "130.176.12.7"  # the CDN's CloudFront, which MediaTailor saw as its client
MT_EGRESS = "3.81.20.30"  # one of MediaTailor's many EC2 egress addresses
CF_PBS_SIDE = "130.176.40.9"  # the PBS CloudFront, which the load balancer saw as its client
CFVA = f"{MT_EGRESS}:46532"

THROUGH_CDN = f"{VIEWER}, {CF_VIEWER_SIDE}, {MT_EGRESS}, {CF_PBS_SIDE}"
SPOOFED = f"203.0.113.7, {VIEWER}, {CF_VIEWER_SIDE}, {MT_EGRESS}, {CF_PBS_SIDE}"
DIRECT = f"{VIEWER}, {MT_EGRESS}, {CF_PBS_SIDE}"


@pytest.mark.parametrize("chain", [THROUGH_CDN, SPOOFED, DIRECT], ids=["cdn", "spoofed", "direct"])
@pytest.mark.parametrize("cfva", [CFVA, ""], ids=["by-viewer-address", "by-position"])
def test_the_viewer_in_every_measured_shape(chain: str, cfva: str) -> None:
    assert viewer_ip(chain, cfva, ORIGIN_FACING) == ViewerIp(IPv4Address(VIEWER), IpRule.OK)


def test_session_client_ip_would_have_taken_the_spoofed_entry() -> None:
    leftmost = SPOOFED.split(",", maxsplit=1)[0]
    assert viewer_ip(SPOOFED, CFVA, ORIGIN_FACING).ip != IPv4Address(leftmost)


def test_viewer_address_finds_the_egress_whatever_follows_it() -> None:
    # a further hop after the PBS CloudFront's: the viewer address still locates MediaTailor's egress
    extra = f"{THROUGH_CDN}, 10.0.1.17"
    assert viewer_ip(extra, CFVA, ORIGIN_FACING).ip == IPv4Address(VIEWER)
    # a client-supplied copy of the egress address on the left does not move it: the rightmost occurrence counts
    decoy = f"{MT_EGRESS}, {THROUGH_CDN}"
    assert viewer_ip(decoy, CFVA, ORIGIN_FACING).ip == IPv4Address(VIEWER)


@pytest.mark.parametrize(
    ("chain", "cfva", "rule"),
    [
        ("", CFVA, IpRule.NO_CHAIN),
        ("  ", "", IpRule.NO_CHAIN),
        (f"{MT_EGRESS}, {CF_PBS_SIDE}", "", IpRule.SHORT_CHAIN),
        (f"{MT_EGRESS}, {CF_PBS_SIDE}", CFVA, IpRule.SHORT_CHAIN),
        (f"{CF_VIEWER_SIDE}, {MT_EGRESS}, {CF_PBS_SIDE}", CFVA, IpRule.SHORT_CHAIN),
        (CF_PBS_SIDE, "", IpRule.SHORT_CHAIN),
        (f"{VIEWER}, 3.81.20.31, {CF_PBS_SIDE}", CFVA, IpRule.EGRESS_NOT_FOUND),
        (THROUGH_CDN, "not-an-address", IpRule.INVALID_ENTRY),
        (f"unknown, {MT_EGRESS}, {CF_PBS_SIDE}", CFVA, IpRule.INVALID_ENTRY),
        (f", {MT_EGRESS}, {CF_PBS_SIDE}", "", IpRule.INVALID_ENTRY),
        (f"10.1.2.3, {MT_EGRESS}, {CF_PBS_SIDE}", CFVA, IpRule.NOT_GLOBAL),
        (f"192.0.2.1, {CF_VIEWER_SIDE}, {MT_EGRESS}, {CF_PBS_SIDE}", "", IpRule.NOT_GLOBAL),
    ],
)
def test_chains_that_hold_no_viewer(chain: str, cfva: str, rule: IpRule) -> None:
    assert viewer_ip(chain, cfva, ORIGIN_FACING) == ViewerIp(None, rule)


def test_without_origin_facing_networks_the_viewer_side_cloudfront_is_not_skipped() -> None:
    assert viewer_ip(THROUGH_CDN, CFVA, ()).ip == IPv4Address(CF_VIEWER_SIDE)


def test_ipv6_viewers_and_viewer_addresses() -> None:
    viewer = "2a02:8010:1234::17"
    chain = f"{viewer}, 2600:9000:1000::5, {MT_EGRESS}, {CF_PBS_SIDE}"
    assert viewer_ip(chain, CFVA, ORIGIN_FACING) == ViewerIp(IPv6Address(viewer), IpRule.OK)
    egress = "2600:1f18:abcd::9"
    v6_egress = f"{VIEWER}, {CF_VIEWER_SIDE}, {egress}, {CF_PBS_SIDE}"
    for cfva in (f"{egress}:46532", f"[{egress}]:46532", f"[{egress}]", egress):
        assert viewer_ip(v6_egress, cfva, ORIGIN_FACING).ip == IPv4Address(VIEWER), cfva
    for bad in (f"[{egress}", f"[{egress}]x", f"[{egress}]:port"):
        assert viewer_ip(v6_egress, bad, ORIGIN_FACING).rule is IpRule.INVALID_ENTRY, bad


def test_ipv4_mapped_entries_compare_as_ipv4() -> None:
    chain = f"::ffff:{VIEWER}, {CF_VIEWER_SIDE}, ::ffff:{MT_EGRESS}, {CF_PBS_SIDE}"
    assert viewer_ip(chain, CFVA, ORIGIN_FACING) == ViewerIp(IPv4Address(VIEWER), IpRule.OK)
    assert viewer_ip(THROUGH_CDN, MT_EGRESS, ORIGIN_FACING).ip == IPv4Address(VIEWER)  # no port


def test_origin_facing_networks_from_ip_ranges() -> None:
    document = {
        "prefixes": [
            {"ip_prefix": "130.176.0.0/18", "service": "CLOUDFRONT_ORIGIN_FACING"},
            {"ip_prefix": "3.80.0.0/12", "service": "EC2"},
        ],
        "ipv6_prefixes": [
            {"ipv6_prefix": "2600:9000:1000::/36", "service": "CLOUDFRONT_ORIGIN_FACING"},
            {"ipv6_prefix": "2600:1f18::/33", "service": "EC2"},
        ],
    }
    assert origin_facing_networks(document) == ORIGIN_FACING
    assert origin_facing_networks({}) == ()
