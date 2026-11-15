# SPDX-License-Identifier: GPL-3.0-or-later
"""Assembly placement and matching constants."""

# Chassis and cabin share one truck frame, so the cabin root uses the chassis origin.
CABIN_LOCATOR = None
CABIN_AT_CHASSIS_ORIGIN = True

# Worklist loop: stop after this many passes of placing accessories on accessories.
MAX_DEPTH = 8

# Locator names match a slot exactly or begin with its name.
SLOT_MATCH_RULE = "exact_or_prefix"

# Exterior fallback: cabin, chassis, then placed accessories.
OWNER_ORDER = {"cabin": 0, "chassis": 1, "accessory": 2}
