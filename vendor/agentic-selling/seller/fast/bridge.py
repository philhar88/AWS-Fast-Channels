"""The FAST Channels bridge: a container of the Prebid Sales Agent's task, on the seller's own ORM (Python 3.12).

It is the only code of ours inside the seller's image, and it changes nothing of the seller's. It:

1. seeds the stage's tenant once (``ADCP_MULTI_TENANT`` with the CloudFront host as its virtual host, the mock
   ad server, the reference buyer's principal with its token, a USD currency limit, the publisher's property), the
   same rows the seller's own seeders write;
2. keeps the tenant's products those the sync function answers (``core.selling.products``: the monetized channels
   with the catalog's genres), every ``FAST_PRODUCTS_SECONDS``;
3. every ``FAST_SYNC_SECONDS``, sends the tenant's media buys to the sync function, which publishes their own-book
   lines, and writes the delivery it answers where the seller's ``get_media_buy_delivery`` reads it.

OPTIMIZATION(oss): W-8 A11. The seller has no ad-server adapter for MediaTailor and the PDS own book, so buys
are booked on its mock adapter and read from its database here, and delivery is reported through the mock adapter's
seeded-delivery rows (``delivery_simulation_configs``, which it reads only in testing mode). Each row holds our
tracker's impressions, a seller-side server count: never the mock's simulation. A ``mediatailor_pds`` adapter
upstream would replace this file.
"""

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, "/app")  # the seller's application package, ``src``

import boto3  # the bridge layer adds it to the seller's environment
from sqlalchemy import inspect, select
from src.core.database.database_session import get_db_session, get_engine
from src.core.database.models import (
    AuthorizedProperty,
    CurrencyLimit,
    MediaBuy,
    MediaPackage,
    PricingOption,
    Product,
    PropertyTag,
    Tenant,
)
from src.core.database.repositories.delivery_simulation import DeliverySimulationConfigRepository
from src.core.database.repositories.principal import PrincipalRepository
from src.core.schemas.delivery import AdapterGetMediaBuyDeliveryResponse

TENANT = os.environ["FAST_TENANT"]
HOST = os.environ["FAST_SELLER_HOST"]
PRINCIPAL = os.environ["FAST_BUYER_PRINCIPAL"]
PUBLISHER_DOMAIN = os.environ["FAST_PUBLISHER_DOMAIN"]
SYNC_FUNCTION = os.environ["FAST_SYNC_FUNCTION"]
SYNC_SECONDS = int(os.environ.get("FAST_SYNC_SECONDS", "60"))
PRODUCTS_SECONDS = int(os.environ.get("FAST_PRODUCTS_SECONDS", "900"))
FORMAT_ID = "video_ctv_midroll_30s"
PROPERTY_TAG = "all_inventory"
SCHEMA_TABLE = "delivery_simulation_configs"
"""The newest table the bridge writes: once it exists, the seller's migrations have run."""

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fast-bridge")


def emit(event: str, **fields: Any) -> None:
    log.info(json.dumps({"bridge": event, "tenant": TENANT, **fields}, default=str))


def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    """The sync function's answer to ``payload``."""
    response = boto3.client("lambda").invoke(FunctionName=SYNC_FUNCTION, Payload=json.dumps(payload, default=str))
    answer = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise RuntimeError(f"sync function failed: {answer}")
    return answer


def wait_for_schema() -> None:
    """Until the seller's migrations have created its tables (the app container runs them at start)."""
    while not inspect(get_engine()).has_table(SCHEMA_TABLE):
        emit("waiting_for_migrations")
        time.sleep(10)


def seed() -> None:
    """The tenant and what booking needs, once; the virtual host follows the CloudFront distribution."""
    now = datetime.now(UTC)
    with get_db_session() as session:
        tenant = session.scalars(select(Tenant).filter_by(tenant_id=TENANT)).first()
        if tenant is None:
            session.add(
                Tenant(
                    tenant_id=TENANT,
                    name=f"FAST Channels ({PUBLISHER_DOMAIN})",
                    subdomain=TENANT,
                    virtual_host=HOST,
                    billing_plan="test",
                    ad_server="mock",
                    enable_axe_signals=False,
                    is_active=True,
                    authorized_emails=[f"operator@{PUBLISHER_DOMAIN}"],
                    authorized_domains=None,
                    policy_settings=None,
                    signals_agent_config=None,
                    ai_policy=None,
                    auto_approve_format_ids=[FORMAT_ID],
                    # Booking is committed unattended: it is a sandbox seller (testing mode, simulated money).
                    human_review_required=False,
                    auth_setup_mode=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            emit("tenant_created", virtual_host=HOST)
        elif tenant.virtual_host != HOST:
            tenant.virtual_host = HOST
            session.commit()
            emit("tenant_host_updated", virtual_host=HOST)
        principals = PrincipalRepository(session, TENANT)
        if principals.get(PRINCIPAL) is None:
            principals.create_with_token(
                os.environ["FAST_BUYER_TOKEN"],
                principal_id=PRINCIPAL,
                name="AWS reference buyer",
                platform_mappings={"mock": {"advertiser_id": PRINCIPAL}},
            )
            session.add(
                CurrencyLimit(
                    tenant_id=TENANT, currency_code="USD", min_package_budget=0.0, max_daily_package_spend=50000.0
                )
            )
            session.add(
                PropertyTag(
                    tag_id=PROPERTY_TAG,
                    tenant_id=TENANT,
                    name="All inventory",
                    description="Every monetized FAST channel",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AuthorizedProperty(
                    property_id=PUBLISHER_DOMAIN.replace(".", "_"),
                    tenant_id=TENANT,
                    property_type="website",
                    name=PUBLISHER_DOMAIN,
                    identifiers=[{"type": "domain", "value": PUBLISHER_DOMAIN}],
                    tags=[PROPERTY_TAG],
                    publisher_domain=PUBLISHER_DOMAIN,
                    verification_status="verified",
                )
            )
            session.commit()
            emit("principal_created", principal=PRINCIPAL)


def upsert_products(products: list[dict[str, Any]]) -> None:
    """The tenant's products as the sync function answers them: a row each and one fixed CPM pricing option."""
    with get_db_session() as session:
        for spec in products:
            columns = {
                "name": spec["name"],
                "description": spec["description"],
                "format_ids": spec["format_ids"],
                "delivery_type": spec["delivery_type"],
                "channels": spec["channels"],
                "placements": spec["placements"],
                "reporting_capabilities": spec["reporting_capabilities"],
                # OPTIMIZATION(oss): W-8 A10. Named explicitly: a product authorized by property tags only
                # publishes the tenant's host (the CloudFront domain) as its publisher domain.
                "properties": [{"publisher_domain": PUBLISHER_DOMAIN, "selection_type": "all"}],
                "property_tags": None,  # a product names its properties one way only (ck_product_authorization)
            }
            row = session.scalars(select(Product).filter_by(tenant_id=TENANT, product_id=spec["product_id"])).first()
            rate = Decimal(str(spec["cpm"]))
            if row is None:
                session.add(
                    Product(
                        tenant_id=TENANT,
                        product_id=spec["product_id"],
                        targeting_template={},
                        **columns,
                    )
                )
                session.add(
                    PricingOption.create(
                        tenant_id=TENANT,
                        product_id=spec["product_id"],
                        pricing_model="cpm",
                        rate=rate,
                        currency=spec["currency"],
                        is_fixed=True,
                        price_guidance=None,
                    )
                )
                emit("product_created", product_id=spec["product_id"], cpm=str(rate))
                continue
            changed = {key: value for key, value in columns.items() if getattr(row, key) != value}
            for key, value in changed.items():
                setattr(row, key, value)
            for option in session.scalars(
                select(PricingOption).filter_by(tenant_id=TENANT, product_id=spec["product_id"])
            ):
                if option.rate != rate:
                    option.rate = rate
                    changed["rate"] = str(rate)
            if changed:
                emit("product_updated", product_id=spec["product_id"], fields=sorted(changed))
        session.commit()


def buys() -> list[dict[str, Any]]:
    """The tenant's media buys with their packages (``core.selling.booking.Buy``)."""
    with get_db_session() as session:
        rows = session.scalars(select(MediaBuy).filter_by(tenant_id=TENANT)).all()
        stored: dict[str, list[MediaPackage]] = {}
        if rows:
            ids = [row.media_buy_id for row in rows]
            for package in session.scalars(select(MediaPackage).where(MediaPackage.media_buy_id.in_(ids))):
                stored.setdefault(package.media_buy_id, []).append(package)
        out = []
        for row in rows:
            request = row.raw_request or {}
            brand = (request.get("brand") or {}).get("domain") or (
                ((request.get("account") or {}).get("brand") or {}).get("domain")
            )
            starts = row.start_time or datetime.combine(row.start_date, datetime.min.time(), UTC)
            ends = row.end_time or datetime.combine(row.end_date, datetime.max.time(), UTC)
            out.append(
                {
                    "media_buy_id": row.media_buy_id,
                    "status": row.status,
                    "paused": bool(row.is_paused),
                    "created_at": row.created_at,
                    "starts": starts,
                    "ends": ends,
                    "currency": row.currency or "USD",
                    "advertiser": row.advertiser_name,
                    "brand": brand,
                    "packages": packages_of(stored.get(row.media_buy_id, []), request.get("packages") or []),
                }
            )
        return out


def packages_of(stored: list[Any], requested: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A buy's packages (``core.selling.booking.Package``), in the order they were booked.

    The seller stores a package's budget in the request it kept (``raw_request.packages``), not on the package's
    row, so each stored package takes the budget of the requested package of its product, in order.
    """
    budgets: dict[str, list[Any]] = {}
    for package in requested:
        budgets.setdefault(package.get("product_id", ""), []).append(package.get("budget"))
    out = []
    for package in sorted(stored, key=lambda p: p.package_id):
        config = package.package_config or {}
        pricing = config.get("pricing_info") or {}
        product = config.get("product_id", "")
        budget = package.budget if package.budget is not None else config.get("budget")
        if budget is None and budgets.get(product):
            budget = budgets[product].pop(0)
        out.append(
            {
                "package_id": package.package_id,
                "product_id": product,
                "budget": str(budget) if budget is not None else None,
                "cpm": str(pricing["rate"]) if pricing.get("is_fixed") and pricing.get("rate") else None,
                "pricing_model": pricing.get("pricing_model"),
            }
        )
    return out


def write_delivery(reports: list[dict[str, Any]], read: list[dict[str, Any]]) -> None:
    """Each booked buy's delivery, where the mock adapter's ``get_media_buy_delivery`` reads it."""
    starts = {buy["media_buy_id"]: buy["starts"] for buy in read}
    now = datetime.now(UTC)
    with get_db_session() as session:
        rows = DeliverySimulationConfigRepository(session, TENANT)
        for report in reports:
            response = AdapterGetMediaBuyDeliveryResponse.model_validate(
                {
                    "media_buy_id": report["media_buy_id"],
                    "reporting_period": {"start": starts[report["media_buy_id"]], "end": now},
                    "totals": {"impressions": report["impressions"], "spend": float(report["spend"])},
                    "by_package": [
                        {"package_id": p["package_id"], "impressions": p["impressions"], "spend": float(p["spend"])}
                        for p in report["packages"]
                    ],
                    "currency": report["currency"],
                }
            )
            rows.upsert(report["media_buy_id"], response.model_dump(mode="json"))
        session.commit()


def main() -> None:
    wait_for_schema()
    seed()
    products_due = 0.0
    while True:
        started = time.monotonic()
        try:
            if started >= products_due:
                upsert_products(invoke({"action": "products"})["products"])
                products_due = started + PRODUCTS_SECONDS
            read = buys()
            answer = invoke({"action": "sync", "buys": read})
            write_delivery(answer["delivery"], read)
            emit(
                "synced",
                buys=len(read),
                lines=answer["lines"],
                unserved=answer["unserved"],
                impressions={r["media_buy_id"]: r["impressions"] for r in answer["delivery"]},
            )
        except Exception as error:  # logged; the next round retries
            log.exception(json.dumps({"bridge": "sync_failed", "error": type(error).__name__}))
        time.sleep(max(1.0, SYNC_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
