"""Former name of :mod:`fast_channels.functions.tools.library_store`, kept for existing imports (scripts, e2e tests).

The alias is the module itself, so state and monkeypatches are shared.
"""

import sys

from fast_channels.functions.tools import library_store

sys.modules[__name__] = library_store
