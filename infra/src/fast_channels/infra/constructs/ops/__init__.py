"""Constructs of the ops module (``modules.ops``): the per-stage lake, the hash-chained ledger, the alarms and
detectors, the runbooks, the probe canary and the AppConfig application with its feature flags.

Nothing here is deployed unless ``modules.ops.enabled`` is true; with the module off the stage is v3.0's.
"""

from fast_channels.infra.constructs.ops.canary import ProbeCanary, canary_name, off_air_alarm_name
from fast_channels.infra.constructs.ops.detectors import (
    AccountQuotaAlarms,
    AlertsPoller,
    ConfigurationAlarms,
    ZeroAdsDetector,
)
from fast_channels.infra.constructs.ops.flags import OpsAppConfig
from fast_channels.infra.constructs.ops.lake import (
    ADS_LOG_FIELDS,
    EXCLUDED_ACCESS_LOG_FIELDS,
    EXCLUDED_ADS_LOG_FIELDS,
    EXCLUDED_MANIFEST_LOG_FIELDS,
    MANIFEST_LOG_FIELDS,
    Lake,
    LakeBucket,
    lake_database_name,
)
from fast_channels.infra.constructs.ops.ledger import ARCHIVE_PREFIX, ARCHIVE_TAG, LedgerStorage, LedgerWriter
from fast_channels.infra.constructs.ops.runbooks import Runbooks

__all__ = [
    "ADS_LOG_FIELDS",
    "ARCHIVE_PREFIX",
    "ARCHIVE_TAG",
    "EXCLUDED_ACCESS_LOG_FIELDS",
    "EXCLUDED_ADS_LOG_FIELDS",
    "EXCLUDED_MANIFEST_LOG_FIELDS",
    "MANIFEST_LOG_FIELDS",
    "AccountQuotaAlarms",
    "AlertsPoller",
    "ConfigurationAlarms",
    "Lake",
    "LakeBucket",
    "LedgerStorage",
    "LedgerWriter",
    "OpsAppConfig",
    "ProbeCanary",
    "Runbooks",
    "ZeroAdsDetector",
    "canary_name",
    "lake_database_name",
    "off_air_alarm_name",
]
