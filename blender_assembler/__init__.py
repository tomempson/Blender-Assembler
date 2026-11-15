# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender Assembler add-on entry point.

The package avoids importing bpy here so its pure-Python core can be used outside Blender.
"""

bl_info = {
    "name": "Blender Assembler",
    "author": "Tom Empson",
    "version": (1, 0, 0),
    "blender": (5, 3, 0),
    "location": "View3D > Sidebar > Blender Assembler",
    "description": "Assemble player truck configurations automatically from game definition files.",
    "tracker_url": "https://github.com/tomempson/Blender-Assembler",
    "category": "Import-Export",
}


def register() -> None:
    from .blender import operators
    operators.register()


def unregister() -> None:
    from .blender import operators
    operators.unregister()
