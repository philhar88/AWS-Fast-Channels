"""Constructs of the ops module (``modules.ops``): the per-stage lake and the AppConfig application.

Nothing here is deployed unless ``modules.ops.enabled`` is true; with the module off the stage is v3.0's.
"""

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

__all__ = [
    "ADS_LOG_FIELDS",
    "EXCLUDED_ACCESS_LOG_FIELDS",
    "EXCLUDED_ADS_LOG_FIELDS",
    "EXCLUDED_MANIFEST_LOG_FIELDS",
    "MANIFEST_LOG_FIELDS",
    "Lake",
    "LakeBucket",
    "OpsAppConfig",
    "lake_database_name",
]
