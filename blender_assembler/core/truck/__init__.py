# SPDX-License-Identifier: GPL-3.0-or-later
"""Truck definition model and scanner."""
from .model import (Accessory, DefPathRef, Fallback, ModelRefs, Slot, SlotRef, TruckDefinition,
                    UnitRef)
from .scanner import scan_truck

__all__ = ["Accessory", "DefPathRef", "Fallback", "ModelRefs", "Slot", "SlotRef", "TruckDefinition",
           "UnitRef", "scan_truck"]
