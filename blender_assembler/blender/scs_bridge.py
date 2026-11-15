# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge Blender Assembler to SCS Blender Tools (`io_scs_tools`).

Import behavior handled here:
- Setting a `scs_globals` property rewrites the user's `io_scs_tools/config.txt` immediately, so the
  file is backed up before anything is changed and restored afterwards.
- `bpy.ops.scs_tools.import_pim` returns {'FINISHED'} even when the import failed. Success is judged
  by the objects it created: exactly one new SCS_Root.
- The importer links new objects to the active layer collection.
- Headless (`--background`): SCS Tools' async path init runs on timers that never fire, so it is
  pumped by hand; and its progress messenger calls `bpy.ops.wm.redraw_timer`, whose poll fails there
  and aborts any import longer than a few seconds, so the messenger is silenced.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import bpy

ADDON = "io_scs_tools"

# SCS global properties changed for an import and restored afterwards.
_RUN_SETTINGS = ("scs_project_path", "import_scale", "import_pim_file", "import_pit_file", "import_load_textures",
                 "import_pic_file", "import_pip_file", "import_pis_file", "import_pia_file")


class ScsToolsMissing(RuntimeError):
    """SCS Blender Tools is not installed or not enabled."""


class ScsBusy(RuntimeError):
    """SCS Tools is still initialising its paths (interactive Blender): try again in a moment."""


@dataclass
class ImportOutcome:
    root: bpy.types.Object | None
    objects: list[bpy.types.Object] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0


def ensure_scs_enabled() -> None:
    """Enable io_scs_tools if it is installed (scripts and tests); raise ScsToolsMissing otherwise."""
    import addon_utils
    if ADDON in bpy.context.preferences.addons:
        return
    # SCS Tools reads its globals through bpy.context.preferences.addons. With
    # default_set=False addon_utils loads the module but omits that preference
    # entry, which makes headless/factory-startup imports fail in scs_globals().
    # This does not save Blender user preferences; the SCS config file is still
    # byte-for-byte backed up and restored by ScsSession.
    if addon_utils.enable(ADDON, default_set=True, persistent=False) is None:
        raise ScsToolsMissing("SCS Blender Tools (io_scs_tools) is not installed or cannot be enabled")


def scs_enabled() -> bool:
    return ADDON in bpy.context.preferences.addons


def scs_globals():
    from io_scs_tools.utils import get_scs_globals
    return get_scs_globals()


def scs_config_path() -> Path:
    from io_scs_tools.internals.containers.config import get_config_filepath
    return Path(get_config_filepath())


def layer_collection_for(layer_coll, collection):
    """The LayerCollection wrapping `collection`, searched recursively."""
    if layer_coll.collection == collection:
        return layer_coll
    for child in layer_coll.children:
        found = layer_collection_for(child, collection)
        if found is not None:
            return found
    return None


class ScsSession:
    """Context manager around a series of imports.

        with ScsSession(project_base) as scs:
            outcome = scs.import_pim(pim_path, collection)

    On entry: back up config.txt, snapshot the settings it changes, apply the run settings, make sure
    path initialisation has finished. On exit: restore everything (interactive Blender: the in-memory
    values too; always: the exact config.txt bytes).
    """

    def __init__(self, project_base: Path, load_textures: bool = True, import_collision: bool = True):
        self.project_base = Path(project_base)
        self.load_textures = load_textures
        self.import_collision = import_collision
        self._config_path: Path | None = None
        self._config_bytes: bytes | None = None
        self._snapshot: dict[str, object] = {}
        self._patched_messenger = None
        self.imports = 0
        self.import_seconds = 0.0

    # Lifecycle
    def __enter__(self) -> "ScsSession":
        ensure_scs_enabled()
        try:
            self._config_path = scs_config_path()
            self._config_bytes = self._config_path.read_bytes() if self._config_path.is_file() else None
            g = scs_globals()
            self._snapshot = {name: getattr(g, name) for name in _RUN_SETTINGS}
            self._patch_headless()
            wanted = {
                "scs_project_path": str(self.project_base),
                "import_scale": 1.0, "import_pim_file": True, "import_pit_file": True,
                "import_load_textures": self.load_textures, "import_pic_file": self.import_collision,
                "import_pip_file": False, "import_pis_file": False, "import_pia_file": False,
            }
            for name, value in wanted.items():
                if name == "scs_project_path":
                    if os.path.normcase(os.path.normpath(g.scs_project_path)) == os.path.normcase(
                            os.path.normpath(value)):
                        continue            # unchanged: don't restart path initialisation
                if getattr(g, name) != value:
                    setattr(g, name, value)
            self.wait_for_paths()
        except BaseException:
            self._restore()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._restore()

    def _restore(self) -> None:
        try:
            if not bpy.app.background and self._snapshot and scs_enabled():
                g = scs_globals()
                for name, value in self._snapshot.items():      # also rewrites config.txt, fixed below
                    if getattr(g, name) != value:
                        setattr(g, name, value)
        finally:
            if self._patched_messenger is not None:
                from io_scs_tools.utils import printout
                printout.immediate_messenger.report_message_and_redraw = self._patched_messenger
                self._patched_messenger = None
            if self._config_path is not None and self._config_bytes is not None:
                if not self._config_path.is_file() or self._config_path.read_bytes() != self._config_bytes:
                    self._config_path.write_bytes(self._config_bytes)

    def _patch_headless(self) -> None:
        if not bpy.app.background:
            return
        from io_scs_tools.utils import printout
        self._patched_messenger = printout.immediate_messenger.report_message_and_redraw
        printout.immediate_messenger.report_message_and_redraw = lambda message: None

    def wait_for_paths(self, timeout: float = 120.0) -> None:
        from io_scs_tools.internals.containers.config import AsyncPathsInit
        if not AsyncPathsInit.is_running():
            return
        if not bpy.app.background:
            raise ScsBusy("SCS Tools initialization hasn't finished yet.")
        deadline = time.time() + timeout
        # Blender timers do not run in --background, so pump SCS Tools' timer
        # callback there. Interactive use is deferred by ETA_OT_Assemble.invoke
        # so Blender can run the timer normally.
        old_report_progress = AsyncPathsInit._report_progress
        AsyncPathsInit._report_progress = staticmethod(lambda *args, **kwargs: None)
        try:
            while AsyncPathsInit.is_running() and time.time() < deadline:
                AsyncPathsInit._process_paths()
        finally:
            AsyncPathsInit._report_progress = staticmethod(old_report_progress)
        if AsyncPathsInit.is_running():
            raise ScsBusy(f"SCS Tools path initialisation did not finish within {timeout:.0f}s.")

    # Importing
    def import_pim(self, pim_path: Path, collection: bpy.types.Collection) -> ImportOutcome:
        """Import one .pim into `collection`. The outcome has exactly one SCS_Root, or an error and
        nothing left behind."""
        pim_path = Path(pim_path)
        t0 = time.perf_counter()
        if not pim_path.is_file():
            return ImportOutcome(None, error=f"file not found: {pim_path}")
        view_layer = bpy.context.view_layer
        layer_coll = layer_collection_for(view_layer.layer_collection, collection)
        if layer_coll is None:
            return ImportOutcome(None, error=f"collection {collection.name!r} is not in the view layer")
        view_layer.active_layer_collection = layer_coll

        before = set(bpy.data.objects)
        try:
            bpy.ops.scs_tools.import_pim(directory=str(pim_path.parent) + os.sep, files=[{"name": pim_path.name}])
        except Exception as exc:   # noqa: BLE001 - report, never abort the run
            error = f"operator raised {type(exc).__name__}: {exc}"
        else:
            error = None
        new = [o for o in bpy.data.objects if o not in before]
        roots = [o for o in new if o.type == "EMPTY" and o.scs_props.empty_object_type == "SCS_Root"]
        seconds = time.perf_counter() - t0
        self.imports += 1
        self.import_seconds += seconds
        if error is None and len(roots) != 1:
            error = (f"import created {len(new)} object(s) and {len(roots)} SCS root(s), expected exactly one "
                     f"root (see the Blender console for the SCS Tools message)")
        if error is not None:
            for obj in new:
                bpy.data.objects.remove(obj, do_unlink=True)
            return ImportOutcome(None, error=error, seconds=seconds)
        return ImportOutcome(roots[0], new, None, seconds)


def object_part(obj) -> str:
    return obj.scs_props.scs_part


def is_model_locator(obj) -> bool:
    return (obj.type == "EMPTY" and obj.scs_props.empty_object_type == "Locator"
            and obj.scs_props.locator_type == "Model")


def hookup_of(obj) -> str:
    return obj.scs_props.locator_model_hookup
