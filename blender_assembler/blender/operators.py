# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender UI, preferences, and operator flow."""
from __future__ import annotations

import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty)
import bpy.utils.previews

from ..core import converter_pix as cpx
from ..core.converter_download import CONVERTER_BUILDS, is_pinned_digest, sha256_file
from ..core.dds import dds_to_png
from ..core.report import SkippedItem
from ..core.sii.errors import SiiError
from ..core.truck import scan_truck
from ..core.truck.dealer import parse_dealer_file, selection_from_dealer
from ..core.truck.plan import PlanOptions
from ..core.truck.rules import evaluate_options, reset_invalid_accessories, validity
from ..core.truck.selection import Selection, options_for_slot
from .assemble import AssemblyError, assemble
from .converter import Converter, ConverterError, ConverterSettings, default_archives, find_converter
from .pipeline import prepare
from .scs_bridge import ScsBusy, ensure_scs_enabled, scs_config_path, scs_globals

ADDON_ID = __package__.split(".")[0]
_ASSEMBLE_TIMER_PENDING = False
_ASSEMBLY_JOB_RUNNING = False
_ASSEMBLY_CLEANUP = None
_STARTUP_TRUCK_REFRESH_TIMER = None
_TRUCK_AUTOLOAD_PENDING = {}
_TRUCK_LOAD_SUPPRESS = False
_CONVERTER_HASH_CACHE = {}
_RULE_UPDATE_GUARD = False
_DEFAULTS_UPDATE_GUARD = False
_DEFN_CACHE = {}
_ICON_PREVIEWS = None
_ICON_VALUES = {}
_ICON_MISSING = set()
_ICON_ERRORS = {}
_MISSING_ICON_ID = 0
_ICON_QUEUE = []
_ICON_QUEUED = set()
_ICON_LOAD_TIMER = None
_LOCALE_MAP = {}
_LOCALE_CACHE_KEY = None
_LOCALE_LOCK = threading.Lock()
_LOCALE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blender-assembler-locale")
_LOCALE_REFRESH_PENDING = {}
_LOCALE_LABEL_MIGRATION_PENDING = set()
_LOCALE_DISPLAY_NAMES = {
    "bg_bg": "Bulgarian (Bulgaria)",
    "ca_es": "Catalan",
    "cs_cz": "Czech (Czechia)",
    "da_dk": "Danish (Denmark)",
    "de_de": "German (Germany)",
    "el_gr": "Greek (Greece)",
    "en_gb": "English (United Kingdom)",
    "en_us": "English (United States)",
    "es_es": "Spanish (Spain)",
    "es_la": "Spanish (Latin America)",
    "et_ee": "Estonian (Estonia)",
    "eu_es": "Basque",
    "fi_fi": "Finnish (Finland)",
    "fr_ca": "French (Canada)",
    "fr_fr": "French (France)",
    "gl_es": "Galician",
    "hr_hr": "Croatian (Croatia)",
    "hu_hu": "Hungarian (Hungary)",
    "it_it": "Italian (Italy)",
    "ja_jp": "Japanese (Japan)",
    "ka_ge": "Georgian (Georgia)",
    "ko_kr": "Korean (Korea)",
    "lt_lt": "Lithuanian (Lithuania)",
    "lv_lv": "Latvian (Latvia)",
    "mk_mk": "Macedonian (North Macedonia)",
    "nl_nl": "Dutch (Netherlands)",
    "no_no": "Norwegian (Norway)",
    "pl_pl": "Polish (Poland)",
    "pl_si": "Polish (Slovenia)",
    "pt_br": "Portuguese (Brazil)",
    "pt_pt": "Portuguese (Portugal)",
    "ro_ro": "Romanian (Romania)",
    "ru_ru": "Russian (Russia)",
    "sk_sk": "Slovak (Slovakia)",
    "sl_sl": "Slovenian (Slovenia)",
    "sr_sp": "Serbian (Latin)",
    "sr_sr": "Serbian (Cyrillic)",
    "sv_se": "Swedish (Sweden)",
    "tr_tr": "Turkish (Türkiye)",
    "uk_uk": "Ukrainian (Ukraine)",
    "vi_vn": "Vietnamese (Vietnam)",
    "zh_cn": "Chinese (Simplified)",
    "zh_tw": "Chinese (Traditional)",
}
_CONVERTER_AUTOFILL_PENDING = set()
_CONFIG_REFRESH_PENDING = {}
_TRUCK_LIST_REFRESH_PENDING = {}
_PREFERENCES_SAVE_PENDING = False


def addon_config_dir() -> Path:
    return Path(bpy.utils.user_resource("CONFIG", path="blender_assembler", create=True))


def _find_converter(preferred_path: str | None = None) -> Path | None:
    user_dir = Path(bpy.utils.resource_path("USER"))
    return find_converter(preferred_path, user_dir, addon_config_dir())


def default_cache_dir() -> Path:
    return Path(bpy.utils.user_resource("CONFIG", path="blender_assembler/cache", create=False))


def _game_directory(path: str) -> Path | None:
    """Resolve the configured game directory, treating an empty path as unset."""
    if not path or not path.strip():
        return None
    game = Path(bpy.path.abspath(path)).expanduser()
    return game if game.is_dir() else None


def _clear_loaded_truck_data(state, message: str) -> None:
    state.truck_options.clear()
    state.dealer_options.clear()
    state.truck = state.dealer_config = state.defs_path = ""
    state.dealer_config_loaded = False
    state.slots.clear()
    state.chassis_options.clear()
    state.cabin_options.clear()
    state.chassis = state.cabin = state.interior = ""
    state.active_slot_index = 0
    state.rule_message = ""
    state.status = message


def _prefs(context) -> "ETA_PG_Config":
    addon = context.preferences.addons.get(ADDON_ID)
    if addon is None:
        raise ConverterError("Blender Assembler preferences are unavailable.")
    return addon.preferences


def _stored_config() -> "ETA_PG_Config":
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is None:
        raise ConverterError("Blender Assembler preferences are unavailable.")
    return addon.preferences


def _schedule_preferences_save() -> None:
    """Persist path edits to Blender's user preferences after the field settles."""
    global _PREFERENCES_SAVE_PENDING
    if _PREFERENCES_SAVE_PENDING:
        return
    _PREFERENCES_SAVE_PENDING = True

    def save_preferences():
        global _PREFERENCES_SAVE_PENDING
        _PREFERENCES_SAVE_PENDING = False
        try:
            bpy.ops.wm.save_userpref()
        except (AttributeError, RuntimeError) as exc:
            print(f"Blender Assembler could not save user preferences: {exc}")
        return None

    bpy.app.timers.register(save_preferences, first_interval=1.0)


def _locale_preference_updated(config, context) -> None:
    _schedule_preferences_save()
    scene = context.scene if context and context.scene else bpy.context.scene
    if scene is not None:
        _schedule_locale_refresh(scene)


def _schedule_locale_refresh(scene) -> None:
    """Reload localized labels off-thread, then refresh Blender properties on its main thread."""
    try:
        key = scene.as_pointer()
        config = _stored_config()
        locale = _selected_locale_code(config)
    except Exception:
        return
    pending = _LOCALE_REFRESH_PENDING.get(key)
    if pending is not None:
        pending["locale"] = locale
        return
    pending = {"scene": scene, "locale": locale, "loading_locale": None, "future": None}
    _LOCALE_REFRESH_PENDING[key] = pending

    def refresh_locale_labels():
        global _LOCALE_MAP, _LOCALE_CACHE_KEY
        if _LOCALE_REFRESH_PENDING.get(key) is not pending:
            return None
        try:
            current_scene = pending["scene"]
            state = current_scene.eta_state
            if not state.truck or not state.defs_path:
                _LOCALE_REFRESH_PENDING.pop(key, None)
                return None
            current_config = _stored_config()
            current_locale = _selected_locale_code(current_config)
            pending["locale"] = current_locale
            if _ASSEMBLY_JOB_RUNNING:
                return 0.1

            future = pending["future"]
            if future is None:
                with bpy.context.temp_override(scene=current_scene):
                    conv = _converter(bpy.context)
                game_dir = Path(bpy.path.abspath(current_config.game_dir)).expanduser()
                pending["loading_locale"] = current_locale
                pending["future"] = _LOCALE_EXECUTOR.submit(
                    _read_accessory_locale, conv, game_dir, current_locale)
                return 0.05

            if not future.done():
                return 0.1
            try:
                cache_key, values = future.result()
            except Exception as exc:
                if pending["locale"] != pending["loading_locale"]:
                    pending["future"] = None
                    pending["loading_locale"] = None
                    return 0.05
                print(f"[Blender Assembler] Could not refresh accessory language: {exc}")
                _LOCALE_REFRESH_PENDING.pop(key, None)
                return None

            if pending["locale"] != pending["loading_locale"]:
                pending["future"] = None
                pending["loading_locale"] = None
                return 0.05
            with _LOCALE_LOCK:
                _LOCALE_CACHE_KEY = cache_key
                _LOCALE_MAP = values
            with bpy.context.temp_override(scene=current_scene):
                _refresh_rule_state(bpy.context, preserve_status=True)
            _redraw_view3d_areas()
            _LOCALE_REFRESH_PENDING.pop(key, None)
            return None
        except Exception as exc:
            print(f"[Blender Assembler] Could not refresh accessory language: {exc}")
            _LOCALE_REFRESH_PENDING.pop(key, None)
            return None

    try:
        pending["timer"] = refresh_locale_labels
        bpy.app.timers.register(refresh_locale_labels, first_interval=0.05)
    except (AttributeError, RuntimeError):
        _LOCALE_REFRESH_PENDING.pop(key, None)


def _schedule_config_refresh(scene, *, refresh_archives: bool) -> None:
    """Refresh game-derived UI data after a path edit, outside Blender's draw/update callback."""
    key = scene.as_pointer()
    if key in _CONFIG_REFRESH_PENDING:
        _CONFIG_REFRESH_PENDING[key]["archives"] |= refresh_archives
        return
    pending = {"archives": refresh_archives}
    _CONFIG_REFRESH_PENDING[key] = pending

    def refresh():
        refresh_default_archives = pending["archives"]
        _CONFIG_REFRESH_PENDING.pop(key, None)
        try:
            config = _stored_config()
            state = scene.eta_state
            game = _game_directory(config.game_dir)
            if game is None:
                config.archives.clear()
                config.locales.clear()
                config.locale = ""
                _clear_loaded_truck_data(state, "Set a valid Game Path to load languages and trucks")
                return None

            # Drop data belonging to the previous game before refreshing. If
            # ConverterPIX fails, the UI must not keep offering stale choices.
            config.locales.clear()
            config.locale = ""
            state.truck_options.clear()
            state.dealer_options.clear()
            state.truck = state.dealer_config = state.defs_path = ""
            state.dealer_config_loaded = False
            state.slots.clear()
            state.chassis_options.clear()
            state.cabin_options.clear()
            state.chassis = state.cabin = state.interior = ""
            with bpy.context.temp_override(scene=scene):
                if refresh_default_archives:
                    bpy.ops.eta.refresh_archives("EXEC_DEFAULT")
                bpy.ops.eta.refresh_locales("EXEC_DEFAULT")
                bpy.ops.eta.refresh_trucks("EXEC_DEFAULT")
            state.dealer_options.clear()
            state.dealer_config = ""
            state.dealer_config_loaded = False
            state.status = "Game data refreshed from the configured paths"
        except (ReferenceError, RuntimeError) as exc:
            print(f"Blender Assembler automatic path refresh failed: {exc}")
        return None

    bpy.app.timers.register(refresh, first_interval=0.5)


def _game_path_updated(config, context) -> None:
    _schedule_preferences_save()
    if context and context.scene:
        _schedule_config_refresh(context.scene, refresh_archives=True)


def _converter_path_updated(config, context) -> None:
    _schedule_preferences_save()
    if context and context.scene:
        _schedule_config_refresh(context.scene, refresh_archives=False)


def _schedule_truck_list_refresh(scene) -> None:
    """Debounce source edits, refresh trucks, then reload the currently selected truck."""
    if scene is None:
        return
    key = scene.as_pointer()
    pending = _TRUCK_LIST_REFRESH_PENDING.get(key)
    if pending is not None:
        pending["last_change"] = time.monotonic()
        return
    pending = {"last_change": time.monotonic()}
    _TRUCK_LIST_REFRESH_PENDING[key] = pending

    def refresh():
        if key not in _TRUCK_LIST_REFRESH_PENDING:
            return None
        if time.monotonic() - pending["last_change"] < 0.5:
            return 0.1
        _TRUCK_LIST_REFRESH_PENDING.pop(key, None)
        try:
            state = scene.eta_state
            config = _stored_config()
            game = _game_directory(config.game_dir)
            if game is None:
                state.truck_options.clear()
                return None
            selected_truck = state.truck
            _DEFN_CACHE.clear()
            with bpy.context.temp_override(scene=scene):
                result = bpy.ops.eta.refresh_trucks("EXEC_DEFAULT")
                state = scene.eta_state
                if "FINISHED" in result and selected_truck:
                    if (state.truck == selected_truck
                            and selected_truck in {item.name for item in state.truck_options}):
                        _DEFN_CACHE.pop((state.defs_path, selected_truck), None)
                        bpy.ops.eta.load_truck("EXEC_DEFAULT", refresh_current=True)
                    else:
                        state.defs_path = ""
                        state.dealer_config = ""
                        state.dealer_config_loaded = False
                        state.dealer_options.clear()
                        state.chassis = state.cabin = state.interior = ""
                        state.chassis_options.clear()
                        state.cabin_options.clear()
                        state.slots.clear()
        except (ConverterError, OSError, ReferenceError, RuntimeError) as exc:
            print(f"Blender Assembler could not refresh trucks after source changes: {exc}")
        return None

    try:
        bpy.app.timers.register(refresh, first_interval=0.5)
    except (AttributeError, RuntimeError):
        _TRUCK_LIST_REFRESH_PENDING.pop(key, None)


def _archive_source_updated(self, context) -> None:
    _schedule_preferences_save()
    if context and context.scene:
        _schedule_truck_list_refresh(context.scene)


def _custom_top_level_path_updated(self, context) -> None:
    _schedule_preferences_save()
    if context and context.scene:
        _schedule_truck_list_refresh(context.scene)


def _schedule_truck_autoload(scene, truck: str) -> None:
    """Load the latest truck selection after Blender finishes updating the picker."""
    key = scene.as_pointer()
    if key in _TRUCK_AUTOLOAD_PENDING:
        _TRUCK_AUTOLOAD_PENDING[key]["truck"] = truck
        return
    pending = {"truck": truck}
    _TRUCK_AUTOLOAD_PENDING[key] = pending

    def load_selected_truck():
        _TRUCK_AUTOLOAD_PENDING.pop(key, None)
        try:
            state = scene.eta_state
            if not pending["truck"] or state.truck != pending["truck"]:
                return None
            with bpy.context.temp_override(scene=scene):
                bpy.ops.eta.load_truck("EXEC_DEFAULT")
        except (ReferenceError, RuntimeError) as exc:
            print(f"Blender Assembler automatic truck loading failed: {exc}")
        return None

    bpy.app.timers.register(load_selected_truck, first_interval=0.1)


def _truck_selected_updated(state, context) -> None:
    if _TRUCK_LOAD_SUPPRESS or not context or not context.scene or not state.truck:
        return
    _schedule_truck_autoload(context.scene, state.truck)


def _set_truck_without_autoload(state, truck: str) -> None:
    global _TRUCK_LOAD_SUPPRESS
    previous = _TRUCK_LOAD_SUPPRESS
    _TRUCK_LOAD_SUPPRESS = True
    try:
        state.truck = truck
    finally:
        _TRUCK_LOAD_SUPPRESS = previous


def _locale_display_name(code: str) -> str:
    return _LOCALE_DISPLAY_NAMES.get(code.lower(), code.replace("_", "-"))


def _schedule_locale_label_migration(scene) -> None:
    """Update language entries saved by older add-on versions after panel drawing."""
    key = scene.as_pointer()
    if key in _LOCALE_LABEL_MIGRATION_PENDING:
        return
    _LOCALE_LABEL_MIGRATION_PENDING.add(key)

    def migrate_labels():
        _LOCALE_LABEL_MIGRATION_PENDING.discard(key)
        try:
            config = _stored_config()
            selected_code = (config.locale or "").lower()
            for item in config.locales:
                code = item.label.lower() if item.label.lower() in _LOCALE_DISPLAY_NAMES else item.name.lower()
                item.name = _locale_display_name(code)
                item.label = code
                if selected_code == code:
                    config.locale = item.name
            if not config.locales:
                config.locale = ""
            for screen in bpy.data.screens:
                for area in screen.areas:
                    area.tag_redraw()
        except ReferenceError:
            pass
        return None

    bpy.app.timers.register(migrate_labels, first_interval=0.05)


def _selected_locale_code(config) -> str:
    selected = (config.locale or "").strip()
    for item in config.locales:
        if selected == item.name or selected.lower() == item.label.lower():
            return item.label
    return selected.lower() if re.fullmatch(r"[a-z]{2}_[a-z]{2}", selected.lower()) else "en_gb"


def _schedule_converter_path_autofill(scene, executable: Path) -> None:
    """Write a detected executable after Blender finishes drawing the panel."""
    key = scene.as_pointer()
    if key in _CONVERTER_AUTOFILL_PENDING:
        return
    _CONVERTER_AUTOFILL_PENDING.add(key)

    def apply_path():
        _CONVERTER_AUTOFILL_PENDING.discard(key)
        try:
            config = _stored_config()
            if not config.converter_path:
                config.converter_path = str(executable)
            for screen in bpy.data.screens:
                for area in screen.areas:
                    area.tag_redraw()
        except ReferenceError:
            pass
        return None

    bpy.app.timers.register(apply_path, first_interval=0.05)


_ARCHIVE_PRIORITY_VERSION = 1


def _archive_path_key(path: str | Path) -> str:
    value = str(path)
    if value.startswith("//"):
        value = bpy.path.abspath(value)
    return os.path.normcase(os.path.abspath(value))


def _remove_duplicate_archives(config) -> int:
    """Keep the first row for each archive path, preserving its priority."""
    seen = set()
    duplicates = []
    for index, item in enumerate(config.archives):
        if not item.path:
            continue
        key = _archive_path_key(item.path)
        if key in seen:
            duplicates.append(index)
        else:
            seen.add(key)
    for index in reversed(duplicates):
        config.archives.remove(index)
    if duplicates:
        _schedule_preferences_save()
    return len(duplicates)


def _ensure_archive_priority_order(config, game_dir: Path) -> None:
    """Migrate saved default archive rows to the new highest-priority-first UI order."""
    _remove_duplicate_archives(config)
    if config.archive_priority_version >= _ARCHIVE_PRIORITY_VERSION or not Path(game_dir).is_dir():
        return

    # Earlier versions stored game archives in ConverterPIX mount order (low to high).
    # Reverse only those known default rows. Custom archives stay where the user placed
    # them, especially mods that were already inserted at the top of the list.
    default_mount_order = default_archives(game_dir)
    ranks = {_archive_path_key(path): index for index, path in enumerate(default_mount_order)}
    items = [(item.path, item.enabled) for item in config.archives]
    positions = [index for index, (path, _) in enumerate(items)
                 if _archive_path_key(path) in ranks]
    defaults = [items[index] for index in positions]
    defaults.sort(key=lambda item: ranks[_archive_path_key(item[0])], reverse=True)
    for index, item in zip(positions, defaults):
        items[index] = item

    if items != [(item.path, item.enabled) for item in config.archives]:
        config.archives.clear()
        for path, enabled in items:
            item = config.archives.add()
            item.path, item.enabled = path, enabled
    config.archive_priority_version = _ARCHIVE_PRIORITY_VERSION


def _converter(context) -> Converter:
    prefs = _prefs(context)
    game = _game_directory(prefs.game_dir)
    if game is None:
        raise ConverterError("Set a valid game path.")
    _ensure_archive_priority_order(prefs, game)
    exe = _find_converter(prefs.converter_path or None)
    if exe is None:
        raise ConverterError("ConverterPIX cannot be found.")
    # Presence in the mounted archive list now means the archive is active;
    # remove a row to exclude it.
    priority_paths = [bpy.path.abspath(a.path) for a in prefs.archives if a.path]
    if priority_paths:
        archives = [Path(p) for p in cpx.converter_mount_order(priority_paths)]
    else:
        # Preserve the game install's established low-to-high ConverterPIX mount order.
        archives = default_archives(game)
        if not archives:
            raise ConverterError("No supported game archives were found in the configured Game Path.")
    missing = [str(p) for p in archives if not p.is_file()]
    if missing:
        raise ConverterError("Missing archive(s) detected: " + ", ".join(missing[:3]))
    cache = Path(bpy.path.abspath(prefs.cache_dir)).expanduser()
    if not str(cache):
        cache = default_cache_dir()
    return Converter(ConverterSettings(
        [str(exe)], archives, cache, jobs=prefs.jobs,
        custom_top_level_paths=[item.path for item in prefs.custom_top_level_paths if item.path.strip()],
    ))


def _prepare_with_progress(context, conv, **kwargs):
    """Show conversion progress; the converter invokes callbacks on Blender's main thread."""
    wm = context.window_manager
    started = False
    def update(done, total, key):
        nonlocal started
        if total <= 0:
            return
        if not started:
            wm.progress_begin(0, total)
            started = True
        wm.progress_update(done)
    try:
        return prepare(conv, progress=update, **kwargs)
    finally:
        if started:
            wm.progress_end()


def _fill_options(collection, accessories) -> None:
    collection.clear()
    for acc in accessories:
        item = collection.add()
        item.name = acc.unit_name
        item.label = _localized_name(acc.display_name)
        item.icon = acc.icon or ""
        item.valid = True
        item.reason = ""


def _localized_name(name: str) -> str:
    """Replace SCS @@localization_key@@ markers when an en_gb value is available."""
    return re.sub(r"@@([^@]+)@@", lambda match: _LOCALE_MAP.get(match.group(1), match.group(0)), name)


def _read_accessory_locale(conv: Converter, game_dir: Path, locale: str = "en_gb"):
    """Read merged localization files; callers install the returned map on Blender's main thread."""
    locale_archive = game_dir / "locale.scs"
    locale = (locale or "en_gb").strip().lower()
    try:
        stat = locale_archive.stat()
        locale_archive_fingerprint = (str(locale_archive), stat.st_size, stat.st_mtime_ns)
    except OSError:
        locale_archive_fingerprint = (str(locale_archive), None, None)
    mounted_fingerprint = tuple(
        (item["path"], item["size"], item["mtime_ns"]) for item in conv.fingerprint())
    cache_key = (locale_archive_fingerprint, locale, mounted_fingerprint)
    with _LOCALE_LOCK:
        if cache_key == _LOCALE_CACHE_KEY:
            return cache_key, dict(_LOCALE_MAP)

    default_archive_keys = {_archive_path_key(path) for path in default_archives(game_dir)}
    locale_archive_key = _archive_path_key(locale_archive)
    # conv.s.archives is ConverterPIX mount order (low priority to high). Keep
    # custom archives separate so localization files from multiple mods can be
    # merged, while consecutive game archives can be read in one pass.
    sources = []
    game_archive_group = []
    for path in conv.s.archives:
        key = _archive_path_key(path)
        if key == locale_archive_key:
            continue
        if key in default_archive_keys:
            game_archive_group.append(path)
        else:
            if game_archive_group:
                sources.append(game_archive_group)
                game_archive_group = []
            sources.append([path])
    if game_archive_group:
        sources.append(game_archive_group)
    if locale_archive.is_file():
        sources.append([locale_archive])

    values = {}
    locale_dir = f"/locale/{locale}"
    conv.root.mkdir(parents=True, exist_ok=True)
    for source_index, archives in enumerate(sources):
        scratch = Path(tempfile.mkdtemp(prefix=f"locale-{source_index}-", dir=conv.root))
        try:
            result = conv.run(cpx.extract_dir_args(archives, locale_dir, scratch))
            produced_dir = scratch.joinpath(*locale_dir.lstrip("/").split("/"))
            if result.failure and not produced_dir.is_dir():
                # Most mounted mods do not contain localization files for this
                # language, so a missing locale directory is expected.
                continue
            if not produced_dir.is_dir():
                continue
            for source in sorted(produced_dir.rglob("*")):
                if not source.is_file() or source.suffix.lower() not in {".sii", ".sui"}:
                    continue
                text = source.read_text(encoding="utf-8", errors="replace")
                pairs = re.finditer(
                    r'^\s*(key|val)\[\]\s*:\s*"((?:\\.|[^"\\])*)"', text, re.MULTILINE)
                pending_key = None
                for match in pairs:
                    kind, value = match.groups()
                    value = value.replace(r'\"', '"').replace(r'\\', '\\')
                    if kind == "key":
                        pending_key = value
                    elif pending_key is not None:
                        values[pending_key] = value
                        pending_key = None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    return cache_key, values


def _load_accessory_locale(conv: Converter, game_dir: Path, locale: str = "en_gb") -> None:
    """Refresh the in-memory locale map synchronously for truck/dealer loading."""
    global _LOCALE_CACHE_KEY, _LOCALE_MAP
    cache_key, values = _read_accessory_locale(conv, game_dir, locale)
    with _LOCALE_LOCK:
        _LOCALE_CACHE_KEY = cache_key
        _LOCALE_MAP = values


def _preview_resource(conv: Converter, game_path: str) -> Path:
    """Extract one UI resource into a dedicated preview cache, without polluting defs/."""
    normalized = cpx.normalize_game_path(game_path)
    if not normalized:
        raise ConverterError(f"unsafe accessory icon resource: {game_path!r}")
    rel = Path(*normalized[1:].split("/"))
    dest = conv.root / "icon_previews" / rel
    if dest.is_file():
        return dest
    conv.root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="icon-", dir=conv.root))
    try:
        result = conv.run(cpx.extract_file_args(conv.s.archives, normalized, scratch))
        produced = scratch.joinpath(*normalized[1:].split("/"))
        if result.failure or not produced.is_file():
            raise ConverterError(f"could not extract icon resource {normalized}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(produced, dest)
        return dest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _icon_reference_path(material_path: str, reference: str) -> str:
    """Resolve a material texture reference, which is often relative to the .mat file."""
    reference = reference.replace("\\", "/")
    if reference.startswith("/"):
        return reference
    return material_path.rsplit("/", 1)[0] + "/" + reference


def _icon_preview(context, icon_path: str) -> int:
    """Resolve the SCS .mat icon to its referenced image and return a Blender preview icon."""
    global _ICON_PREVIEWS
    if not icon_path:
        return 0
    key = icon_path.casefold()
    if key in _ICON_VALUES:
        return _ICON_VALUES[key]
    if key in _ICON_MISSING:
        return 0
    try:
        conv = _converter(context)
        material_path = icon_path.strip().replace("\\", "/").lstrip("/")
        if not material_path.lower().startswith("material/ui/accessory/"):
            material_path = "material/ui/accessory/" + material_path
        if not material_path.lower().endswith(".mat"):
            material_path += ".mat"
        material = _preview_resource(conv, "/" + material_path)
        text = material.read_text(encoding="utf-8", errors="ignore")
        references = re.findall(r"(?i)(?:/[\w./-]+|[\w.-]+)\.(?:tobj|dds|png|tga)", text)
        image_path = next((p for p in references if p.lower().endswith((".dds", ".png", ".tga"))), None)
        if image_path is None:
            tobj_path = next((p for p in references if p.lower().endswith(".tobj")), None)
            if not tobj_path:
                raise ConverterError("icon material did not reference a texture object")
            tobj_path = _icon_reference_path("/" + material_path, tobj_path)
            export_dir = conv.root / "icon_previews" / "converted"
            export_dir.mkdir(parents=True, exist_ok=True)
            result = conv.run(cpx.tobj_args(conv.s.archives, tobj_path, export_dir))
            image_path = tobj_path.rsplit(".", 1)[0] + ".dds"
            dds_file = export_dir.joinpath(*image_path.lstrip("/").split("/"))
            if result.failure or not dds_file.is_file():
                raise ConverterError(f"ConverterPIX could not convert accessory icon texture {tobj_path}")
            image_file = dds_file
        else:
            image_path = _icon_reference_path("/" + material_path, image_path)
            image_file = _preview_resource(conv, image_path)
        # Blender's preview loader does not reliably support DDS, so decode it to PNG.
        if Path(image_file).suffix.lower() == ".dds":
            png_file = Path(image_file).with_suffix(".full.preview.png")
            if not png_file.is_file():
                dds_to_png(image_file, png_file)
        else:
            png_file = Path(image_file)
        if _ICON_PREVIEWS is None:
            _ICON_PREVIEWS = bpy.utils.previews.new()
        preview = _ICON_PREVIEWS.load(key, str(png_file), "IMAGE")
        _ICON_VALUES[key] = preview.icon_id
        return preview.icon_id
    except Exception as exc:
        _ICON_MISSING.add(key)
        _ICON_ERRORS[key] = str(exc)
        print(f"[Blender Assembler] Could not render accessory icon {icon_path!r}: {exc}")
        return 0


def _redraw_view3d_areas() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except (AttributeError, ReferenceError, RuntimeError):
        pass


def _process_icon_preview_queue():
    global _ICON_LOAD_TIMER
    if not _ICON_QUEUE:
        _ICON_LOAD_TIMER = None
        return None
    key, icon_path = _ICON_QUEUE.pop(0)
    _ICON_QUEUED.discard(key)
    if key not in _ICON_VALUES and key not in _ICON_MISSING:
        _icon_preview(bpy.context, icon_path)
    _redraw_view3d_areas()
    if _ICON_QUEUE:
        return 0.05
    _ICON_LOAD_TIMER = None
    return None


def _queue_icon_preview(icon_path: str) -> int:
    global _ICON_LOAD_TIMER
    key = icon_path.casefold()
    if key in _ICON_VALUES:
        return _ICON_VALUES[key]
    if key in _ICON_MISSING:
        return 0
    if key not in _ICON_QUEUED:
        _ICON_QUEUE.append((key, icon_path))
        _ICON_QUEUED.add(key)
    if _ICON_LOAD_TIMER is None:
        _ICON_LOAD_TIMER = _process_icon_preview_queue
        try:
            bpy.app.timers.register(_ICON_LOAD_TIMER, first_interval=0.05)
        except (AttributeError, RuntimeError):
            _ICON_LOAD_TIMER = None
    return 0


def _missing_icon_preview() -> int:
    global _ICON_PREVIEWS, _MISSING_ICON_ID
    if _MISSING_ICON_ID:
        return _MISSING_ICON_ID
    path = Path(__file__).resolve().parents[1] / "assets" / "icon_missing.png"
    if not path.is_file():
        return 0
    if _ICON_PREVIEWS is None:
        _ICON_PREVIEWS = bpy.utils.previews.new()
    _MISSING_ICON_ID = _ICON_PREVIEWS.load("__icon_missing__", str(path), "IMAGE").icon_id
    return _MISSING_ICON_ID


def _draw_selected_option(layout, context, selected) -> None:
    if selected is None:
        return
    icon_id = _queue_icon_preview(selected.icon) if selected.icon else 0
    if not icon_id:
        icon_id = _missing_icon_preview()
    layout.label(text=selected.label)
    preview_row = layout.row(align=True)
    preview_row.alignment = "CENTER"
    if icon_id:
        preview_row.template_icon(icon_value=icon_id, scale=8.0)
    else:
        preview_row.label(text="", icon="IMAGE_DATA")


def _state_defn(state):
    if not state.defs_path or not state.truck:
        return None
    key = (state.defs_path, state.truck)
    if key not in _DEFN_CACHE:
        _DEFN_CACHE[key] = scan_truck(Path(state.defs_path), state.truck)
    return _DEFN_CACHE[key]


def _refresh_rule_state(context, preserve_status: bool = False):
    global _RULE_UPDATE_GUARD
    state = context.scene.eta_state
    defn = _state_defn(state)
    if defn is None:
        return
    _RULE_UPDATE_GUARD = True
    try:
        selection = _selection(state)
        resets = []
        units = {a.unit_name: a for a in defn.all_accessories()}
        # A cabin can itself be incompatible with the chosen chassis.
        if state.strict and state.cabin and state.chassis and state.cabin in units:
            selected_without_cabin = {state.chassis, *selection.accessories.values()}
            verdict = validity(units[state.cabin], selected_without_cabin, units)
            if not verdict.valid:
                resets.append(f"Cabin {state.cabin}: {'; '.join(verdict.reasons)}")
                state.cabin = ""
                selection = _selection(state)
        if state.strict:
            selection, dropped = reset_invalid_accessories(defn, selection)
            resets.extend(dropped)
            for slot in state.slots:
                slot.chosen = selection.accessories.get(slot.name, "")

        verdicts = evaluate_options(defn, _selection(state))
        for collection, accessories in ((state.chassis_options, defn.chassis),
                                        (state.cabin_options, defn.cabins)):
            if state.strict:
                _fill_options(collection, [a for a in accessories
                                           if verdicts.get(a.unit_name) is None or verdicts[a.unit_name].valid])
            else:
                _fill_options(collection, accessories)
            for option in collection:
                verdict = verdicts.get(option.name)
                option.valid = verdict.valid if verdict else True
                option.reason = "; ".join(verdict.reasons) if verdict else ""
                option.label = ("⚠ " if not option.valid and not state.strict else "") + \
                    _localized_name(next((a.display_name for a in defn.all_accessories()
                                           if a.unit_name == option.name), option.name))

        for slot in state.slots:
            slot_def = defn.slots.get(slot.name)
            accessories = slot_def.accessories if slot_def else []
            _fill_options(slot.options, [a for a in accessories
                                         if not state.strict or verdicts.get(a.unit_name, None) is None
                                         or verdicts[a.unit_name].valid])
            for option in slot.options:
                verdict = verdicts.get(option.name)
                option.valid = verdict.valid if verdict else True
                option.reason = "; ".join(verdict.reasons) if verdict else ""
                if not option.valid and not state.strict:
                    option.label = "⚠ " + option.label
        if resets:
            state.rule_message = "Strict rules reset: " + " | ".join(resets)
        elif not state.strict:
            state.rule_message = "Compatibility enforcement is disabled — expect compatibility problems!"
        elif not preserve_status:
            state.rule_message = ""
    finally:
        _RULE_UPDATE_GUARD = False


def _rules_update(self, context):
    if not _RULE_UPDATE_GUARD:
        try:
            _refresh_rule_state(context)
        except Exception as exc:
            context.scene.eta_state.rule_message = f"Rules could not be refreshed: {exc}"
def _clear_slot_default_tracking(slot) -> None:
    slot.defaulted = False
    slot.default_value = ""
    slot.default_source = ""
    slot.default_original = ""


def _clear_cabin_default_tracking(state) -> None:
    state.cabin_defaulted = False
    state.cabin_default_value = ""
    state.cabin_default_source = ""
    state.cabin_default_original = ""


def _restore_slot_defaults(state) -> None:
    for slot in state.slots:
        if not slot.defaulted:
            continue
        if slot.chosen == slot.default_value:
            slot.chosen = slot.default_original
        slot.last_chosen = slot.chosen
        _clear_slot_default_tracking(slot)


def _restore_cabin_default(state) -> None:
    if not state.cabin_defaulted:
        return
    if state.cabin == state.cabin_default_value:
        state.cabin = state.cabin_default_original
    _clear_cabin_default_tracking(state)


def _remove_defaults_from_sources(state, source_units: set[str]) -> None:
    """Restore choices supplied by removed auto-selected accessories, including their children."""
    pending = list(source_units)
    visited = set()
    while pending:
        source = pending.pop()
        if not source or source in visited:
            continue
        visited.add(source)
        for slot in state.slots:
            if not slot.defaulted or slot.default_source != source:
                continue
            child = slot.default_value
            if slot.chosen == child:
                slot.chosen = slot.default_original
                slot.last_chosen = slot.chosen
            _clear_slot_default_tracking(slot)
            if child:
                pending.append(child)


def _targets_from_defaults(owner, defn, units_by_name, units_by_path):
    """Yield scanned units defined by the resolved files named in an accessory's defaults[]."""
    for ref in owner.defaults:
        if ref.status != "resolved":
            continue
        targets = []
        for name in ref.units:
            targets.extend(acc for acc in units_by_name.get(name, ()) if acc.def_path == ref.path)
        if not targets:
            targets = units_by_path.get(ref.path, ())
        for target in targets:
            yield target


def _set_defaulted_slot(slot, target, source_name) -> None:
    original = slot.default_original if slot.defaulted else slot.chosen
    slot.chosen = target.unit_name
    slot.last_chosen = target.unit_name
    slot.defaulted = True
    slot.default_value = target.unit_name
    slot.default_source = source_name
    slot.default_original = original


def _set_defaulted_cabin(state, target, source_name) -> None:
    original = state.cabin_default_original if state.cabin_defaulted else state.cabin
    state.cabin = target.unit_name
    state.cabin_defaulted = True
    state.cabin_default_value = target.unit_name
    state.cabin_default_source = source_name
    state.cabin_default_original = original


def _apply_one_accessory_default_owner(state, defn, owner, units_by_name, units_by_path):
    """Apply one owner's first declared default per slot and return selected accessory units."""
    applied_slots = set()
    children = []
    for target in _targets_from_defaults(owner, defn, units_by_name, units_by_path):
        if target.slot in ("chassis", "cabin", "interior"):
            continue
        if target.slot in applied_slots or target.slot not in defn.slots:
            continue
        slot = next((item for item in state.slots if item.name == target.slot), None)
        if slot is None:
            continue
        applied_slots.add(target.slot)
        _set_defaulted_slot(slot, target, owner.unit_name)
        children.append(target)
    return children


def _apply_accessory_defaults(state, defn, roots, units_by_name, units_by_path) -> None:
    pending = list(roots)
    visited = set()
    while pending:
        owner = pending.pop(0)
        if owner.unit_name in visited:
            continue
        visited.add(owner.unit_name)
        pending.extend(_apply_one_accessory_default_owner(
            state, defn, owner, units_by_name, units_by_path))


def _apply_selection_defaults(state, defn, *, choose_chassis_cabin=True) -> None:
    """Apply chassis, cabin, then accessory defaults, with later groups taking priority."""
    units = defn.all_accessories()
    units_by_name = {}
    units_by_path = {}
    for acc in units:
        units_by_name.setdefault(acc.unit_name, []).append(acc)
        units_by_path.setdefault(acc.def_path, []).append(acc)

    chassis = next((a for a in units_by_name.get(state.chassis, ()) if a.slot == "chassis"), None)
    cabin = next((a for a in units_by_name.get(state.cabin, ()) if a.slot == "cabin"), None)
    if chassis:
        for target in _targets_from_defaults(chassis, defn, units_by_name, units_by_path):
            if target.slot == "cabin" and choose_chassis_cabin:
                _set_defaulted_cabin(state, target, chassis.unit_name)
                break
    # Chassis defaults can select the cabin, so resolve the current cabin afterwards.
    cabin = next((a for a in units_by_name.get(state.cabin, ()) if a.slot == "cabin"), None)
    if chassis:
        _apply_one_accessory_default_owner(state, defn, chassis, units_by_name, units_by_path)
    if cabin:
        _apply_one_accessory_default_owner(state, defn, cabin, units_by_name, units_by_path)

    accessory_roots = []
    for slot in state.slots:
        selected = next((a for a in units_by_name.get(slot.chosen, ()) if a.slot == slot.name), None)
        if selected:
            accessory_roots.append(selected)
    _apply_accessory_defaults(state, defn, accessory_roots, units_by_name, units_by_path)


def _prune_default_tracking(state) -> None:
    for slot in state.slots:
        if slot.defaulted and slot.chosen != slot.default_value:
            _clear_slot_default_tracking(slot)
            slot.last_chosen = slot.chosen
        elif not slot.defaulted:
            slot.last_chosen = slot.chosen
    if state.cabin_defaulted and state.cabin != state.cabin_default_value:
        _clear_cabin_default_tracking(state)


def _apply_defaults_and_refresh(context, *, restore=True, choose_chassis_cabin=True) -> None:
    global _DEFAULTS_UPDATE_GUARD, _RULE_UPDATE_GUARD
    state = context.scene.eta_state
    defn = _state_defn(state)
    if defn is None:
        _refresh_rule_state(context)
        return
    old_defaults_guard, old_rules_guard = _DEFAULTS_UPDATE_GUARD, _RULE_UPDATE_GUARD
    _DEFAULTS_UPDATE_GUARD = True
    _RULE_UPDATE_GUARD = True
    try:
        if restore:
            _restore_slot_defaults(state)
            if choose_chassis_cabin:
                _restore_cabin_default(state)
        _apply_selection_defaults(state, defn, choose_chassis_cabin=choose_chassis_cabin)
    finally:
        _RULE_UPDATE_GUARD = old_rules_guard
        _DEFAULTS_UPDATE_GUARD = old_defaults_guard
    _refresh_rule_state(context)
    _prune_default_tracking(state)


def _defaults_toggle_update(self, context):
    global _DEFAULTS_UPDATE_GUARD
    if _DEFAULTS_UPDATE_GUARD or _RULE_UPDATE_GUARD:
        return
    state = context.scene.eta_state
    if state.enforce_defaults:
        # Enabling the toggle only affects subsequent part selections. Existing
        # chassis, cabin and accessory choices must remain untouched.
        return

    # Disabling the toggle leaves the values in place and only stops future changes.
    _DEFAULTS_UPDATE_GUARD = True
    try:
        for slot in state.slots:
            _clear_slot_default_tracking(slot)
        _clear_cabin_default_tracking(state)
    finally:
        _DEFAULTS_UPDATE_GUARD = False
    _refresh_rule_state(context)


def _chassis_update(self, context):
    if _RULE_UPDATE_GUARD or _DEFAULTS_UPDATE_GUARD:
        return
    state = context.scene.eta_state
    if state.enforce_defaults:
        try:
            _apply_defaults_and_refresh(context, choose_chassis_cabin=True)
        except Exception as exc:
            state.rule_message = f"Defaults could not be applied: {exc}"
    else:
        _refresh_rule_state(context)


def _cabin_update(self, context):
    global _DEFAULTS_UPDATE_GUARD, _RULE_UPDATE_GUARD
    if _RULE_UPDATE_GUARD or _DEFAULTS_UPDATE_GUARD:
        return
    state = context.scene.eta_state
    if not state.enforce_defaults:
        _refresh_rule_state(context)
        return

    # A user-selected cabin takes precedence over the chassis' cabin default.
    _DEFAULTS_UPDATE_GUARD = True
    _RULE_UPDATE_GUARD = True
    try:
        _clear_cabin_default_tracking(state)
        _restore_slot_defaults(state)
    finally:
        _RULE_UPDATE_GUARD = False
        _DEFAULTS_UPDATE_GUARD = False
    try:
        _apply_defaults_and_refresh(context, restore=False, choose_chassis_cabin=False)
    except Exception as exc:
        state.rule_message = f"Defaults could not be applied: {exc}"


def _accessory_choice_update(self, context):
    global _DEFAULTS_UPDATE_GUARD, _RULE_UPDATE_GUARD
    if _RULE_UPDATE_GUARD or _DEFAULTS_UPDATE_GUARD:
        return
    state = context.scene.eta_state
    old_selected = self.default_value if self.defaulted else self.last_chosen
    changed = old_selected != self.chosen
    _DEFAULTS_UPDATE_GUARD = True
    _RULE_UPDATE_GUARD = True
    try:
        if self.defaulted and changed:
            _clear_slot_default_tracking(self)
        if changed and old_selected and state.enforce_defaults:
            _remove_defaults_from_sources(state, {old_selected})
        if state.enforce_defaults and self.chosen:
            defn = _state_defn(state)
            if defn is not None:
                selected = next((a for a in defn.all_accessories()
                                 if a.unit_name == self.chosen and a.slot == self.name), None)
                if selected:
                    by_name = {}
                    by_path = {}
                    for acc in defn.all_accessories():
                        by_name.setdefault(acc.unit_name, []).append(acc)
                        by_path.setdefault(acc.def_path, []).append(acc)
                    _apply_accessory_defaults(state, defn, [selected], by_name, by_path)
    except Exception as exc:
        state.rule_message = f"Defaults could not be applied: {exc}"
    finally:
        self.last_chosen = self.chosen
        _RULE_UPDATE_GUARD = False
        _DEFAULTS_UPDATE_GUARD = False
    _refresh_rule_state(context)
    _prune_default_tracking(state)


def _accessory_filter_updated(state, context):
    query = state.filter_text.strip().casefold()
    if not query:
        return
    state.active_slot_index = next(
        (index for index, slot in enumerate(state.slots) if query in slot.name.casefold()), 0)


def _fill_state_impl(state, defn, selection: Selection | None = None) -> None:
    _fill_options(state.chassis_options, defn.chassis)
    _fill_options(state.cabin_options, defn.cabins)
    _clear_cabin_default_tracking(state)
    for attr, options in (("chassis", defn.chassis), ("cabin", defn.cabins)):
        current = getattr(state, attr)
        names = {x.unit_name for x in options}
        wanted = getattr(selection, attr) if selection else None
        setattr(state, attr, wanted if wanted in names else current if current in names else "")
    interior_names = {x.unit_name for x in defn.interiors}
    wanted_interior = selection.interior if selection else None
    state.interior = wanted_interior if wanted_interior in interior_names else ""
    state.slots.clear()
    for name, slot_def in sorted(defn.slots.items()):
        slot = state.slots.add()
        slot.name = name
        slot.legacy = slot_def.legacy
        _fill_options(slot.options, options_for_slot(defn, name))
        chosen = selection.accessories.get(name, "") if selection else ""
        slot.chosen = chosen if chosen and any(a.unit_name == chosen for a in slot_def.accessories) else ""
        slot.last_chosen = slot.chosen
    query = state.filter_text.strip().casefold()
    state.active_slot_index = next(
        (index for index, slot in enumerate(state.slots) if not query or query in slot.name.casefold()), 0)
    if selection:
        state.drive_side = "UK" if selection.rhd else "EU"
    state.status = (f"{defn.brand_model}: {len(defn.chassis)} chassis, {len(defn.cabins)} cabins, "
                    f"{len(defn.slots)} slots, {len(defn.all_accessories())} accessories, "
                    f"{len(defn.warnings)} warnings")


def _fill_state(state, defn, selection: Selection | None = None) -> None:
    global _RULE_UPDATE_GUARD
    old_guard = _RULE_UPDATE_GUARD
    _RULE_UPDATE_GUARD = True
    try:
        _fill_state_impl(state, defn, selection)
    finally:
        _RULE_UPDATE_GUARD = old_guard
    if not old_guard:
        _refresh_rule_state(bpy.context)


def _selection(state) -> Selection:
    return Selection(state.truck, state.chassis or None, state.cabin or None, state.interior or None,
                     {s.name: s.chosen for s in state.slots if s.chosen}, state.drive_side == "UK")


def _required_options_error(state) -> str | None:
    if not state.truck:
        return "Select a truck before assembling."
    try:
        defn = _state_defn(state)
    except (OSError, ValueError, SiiError) as exc:
        return f"Could not check required options: {str(exc).splitlines()[0]}"
    if defn is None:
        return "Load the selected truck before assembling."
    if not state.chassis and not state.cabin:
        return "Select a cabin or chassis before assembling."
    return None


class ETA_PG_Option(bpy.types.PropertyGroup):
    name: StringProperty()
    label: StringProperty()
    icon: StringProperty()
    valid: BoolProperty(default=True)
    reason: StringProperty()


class ETA_PG_Archive(bpy.types.PropertyGroup):
    path: StringProperty(subtype="FILE_PATH", description="Show path to archive.",
                         update=_archive_source_updated)
    enabled: BoolProperty(default=True,
                          description="Include or exclude this archive from game data searches.",
                          update=_archive_source_updated)


class ETA_PG_Locale(bpy.types.PropertyGroup):
    name: StringProperty()
    label: StringProperty()


class ETA_PG_CustomTopLevelPath(bpy.types.PropertyGroup):
    path: StringProperty(name="Top-Level Path", default="",
                         description="Top-level folder to search (e.g. /base or /dlc_*).",
                         update=_custom_top_level_path_updated)


class ETA_PG_Config(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    game_dir: StringProperty(name="Game Path", subtype="DIR_PATH",
                             description="Path of Euro Truck Simulator 2 installation.",
                             update=_game_path_updated)
    cache_dir: StringProperty(name="Cache Path", subtype="DIR_PATH",
                              description="Path of cached data.",
                              default=str(Path(bpy.utils.user_resource("CONFIG", path="blender_assembler/cache"))))
    converter_path: StringProperty(name="ConverterPIX Path", subtype="FILE_PATH",
                                   description="Path of ConverterPIX installation (may be detected automatically).",
                                   update=_converter_path_updated)
    jobs: IntProperty(name="Parallel Conversions", default=1, min=1, max=4,
                      description="Number of model conversions that can occur simultaneously.")
    archives: CollectionProperty(type=ETA_PG_Archive)
    archive_index: IntProperty()
    archive_priority_version: IntProperty(default=0, options={"HIDDEN"})
    custom_top_level_paths: CollectionProperty(type=ETA_PG_CustomTopLevelPath)
    custom_top_level_path_index: IntProperty()
    locale: StringProperty(name="Accessory Language", default="",
                           description="Language used to display accessory names.",
                           update=_locale_preference_updated)
    locales: CollectionProperty(type=ETA_PG_Locale)

    def draw(self, context):
        _draw_config(self.layout, context, self)


class ETA_PG_Slot(bpy.types.PropertyGroup):
    name: StringProperty()
    chosen: StringProperty(name="Chosen Accessory", description="Select an accessory to use.",
                           update=_accessory_choice_update)
    legacy: BoolProperty()
    defaulted: BoolProperty(options={"HIDDEN"})
    default_value: StringProperty(options={"HIDDEN"})
    default_source: StringProperty(options={"HIDDEN"})
    default_original: StringProperty(options={"HIDDEN"})
    last_chosen: StringProperty(options={"HIDDEN"})
    options: CollectionProperty(type=ETA_PG_Option)


class ETA_PG_State(bpy.types.PropertyGroup):
    truck: StringProperty(name="Truck", description="Select a truck to search game data for.",
                           update=_truck_selected_updated)
    defs_path: StringProperty(options={"HIDDEN"})
    truck_options: CollectionProperty(type=ETA_PG_Option)
    chassis: StringProperty(name="Chassis", description="Select a chassis.", update=_chassis_update)
    chassis_options: CollectionProperty(type=ETA_PG_Option)
    cabin: StringProperty(name="Cabin", description="Select a cabin.", update=_cabin_update)
    cabin_options: CollectionProperty(type=ETA_PG_Option)
    cabin_defaulted: BoolProperty(options={"HIDDEN"})
    cabin_default_value: StringProperty(options={"HIDDEN"})
    cabin_default_source: StringProperty(options={"HIDDEN"})
    cabin_default_original: StringProperty(options={"HIDDEN"})
    interior: StringProperty(options={"HIDDEN"})  # Dealer compatibility context; no interior model is imported.
    slots: CollectionProperty(type=ETA_PG_Slot)
    active_slot_index: IntProperty(name="Accessory", default=0, min=0)
    dealer_config: StringProperty(name="Dealer Configuration",
                                  description="Select a dealer configuration file.")
    dealer_config_loaded: BoolProperty(default=False)
    dealer_options: CollectionProperty(type=ETA_PG_Option)
    drive_side: EnumProperty(
        name="Drive Side",
        items=(("EU", "EU", "Use the EU configuration."),
               ("UK", "UK", "Use the UK configuration.")),
        default="EU",
    )
    strict: BoolProperty(name="Enforce Compatibility", default=True,
                         description="Filter incompatible options.",
                         update=_rules_update)
    enforce_defaults: BoolProperty(
        name="Enforce Defaults", default=False,
        description="Apply defaults after selecting a new chassis, cabin or accessory.",
        update=_defaults_toggle_update)
    filter_text: StringProperty(name="Search", description="Filter accessories by name.",
                                update=_accessory_filter_updated)
    rule_message: StringProperty()
    status: StringProperty()


class ETA_OT_RefreshArchives(bpy.types.Operator):
    bl_idname = "eta.refresh_archives"
    bl_label = "Mount Default Archives"

    def execute(self, context):
        prefs = _prefs(context)
        game = _game_directory(prefs.game_dir)
        if game is None:
            self.report({"ERROR"}, "Set a valid game path.")
            return {"CANCELLED"}
        archives = default_archives(game)
        if not archives:
            self.report({"WARNING"}, "No supported game archives were found in the configured Game Path.")
            return {"CANCELLED"}
        prefs.archives.clear()
        # The UI is highest-priority first; ConverterPIX's mount order is the reverse.
        for path in reversed(archives):
            item = prefs.archives.add()
            item.path, item.enabled = str(path), True
        prefs.archive_priority_version = _ARCHIVE_PRIORITY_VERSION
        self.report({"INFO"}, f"Found {len(prefs.archives)} archives")
        return {"FINISHED"}


class ETA_OT_RefreshLocales(bpy.types.Operator):
    bl_idname = "eta.refresh_locales"
    bl_label = "Find Game Languages"
    bl_description = "Refresh languages available for use."

    def execute(self, context):
        prefs = _prefs(context)
        previous_locale = prefs.locale
        prefs.locales.clear()
        prefs.locale = ""
        try:
            conv = _converter(context)
            game = Path(bpy.path.abspath(prefs.game_dir)).expanduser()
            locale_archive = game / "locale.scs"
            if not locale_archive.is_file():
                raise ConverterError("Localisation files cannot be found in the game path.")
            archives = [*conv.s.archives]
            if locale_archive not in archives:
                archives.append(locale_archive)
            locale_conv = Converter(ConverterSettings(conv.s.command, archives, conv.root,
                                                      jobs=conv.s.jobs, timeout=conv.s.timeout,
                                                      custom_top_level_paths=conv.s.custom_top_level_paths))
            entries = locale_conv.list_dir("/locale")
        except (ConverterError, OSError) as exc:
            self.report({"ERROR"}, str(exc).splitlines()[0])
            return {"CANCELLED"}
        for entry in entries:
            if entry.is_dir:
                code = entry.path.rstrip("/").rsplit("/", 1)[-1]
                item = prefs.locales.add()
                item.name = _locale_display_name(code)
                item.label = code
        selected = next((item for item in prefs.locales
                         if previous_locale == item.name or previous_locale.lower() == item.label.lower()), None)
        if selected is None:
            selected = next((item for item in prefs.locales if item.label.lower() == "en_gb"),
                            prefs.locales[0] if prefs.locales else None)
        prefs.locale = selected.name if selected else ""
        self.report({"INFO"}, f"Found {len(prefs.locales)} game languages")
        return {"FINISHED"}


class ETA_OT_AddCustomTopLevelPath(bpy.types.Operator):
    bl_idname = "eta.add_custom_top_level_path"
    bl_label = "Add Custom Top-Level Path"
    bl_description = "Add a custom top-level path to search."

    def execute(self, context):
        config = _prefs(context)
        item = config.custom_top_level_paths.add()
        item.path = ""
        config.custom_top_level_path_index = len(config.custom_top_level_paths) - 1
        _schedule_preferences_save()
        _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_RemoveCustomTopLevelPath(bpy.types.Operator):
    bl_idname = "eta.remove_custom_top_level_path"
    bl_label = "Remove Custom Top-Level Path"
    bl_description = "Remove this path."
    index: IntProperty()

    def execute(self, context):
        config = _prefs(context)
        paths = config.custom_top_level_paths
        if 0 <= self.index < len(paths):
            paths.remove(self.index)
            config.custom_top_level_path_index = min(max(0, self.index - 1), len(paths) - 1)
            _schedule_preferences_save()
            _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_MoveCustomTopLevelPathUp(bpy.types.Operator):
    bl_idname = "eta.move_custom_top_level_path_up"
    bl_label = "Move Custom Top-Level Path Up"
    bl_description = "Move this path earlier in the search order."
    index: IntProperty()

    def execute(self, context):
        paths = _prefs(context).custom_top_level_paths
        target = self.index - 1
        if 0 <= self.index < len(paths) and 0 <= target < len(paths):
            paths.move(self.index, target)
            _prefs(context).custom_top_level_path_index = target
            _schedule_preferences_save()
            _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_MoveCustomTopLevelPathDown(bpy.types.Operator):
    bl_idname = "eta.move_custom_top_level_path_down"
    bl_label = "Move Custom Top-Level Path Down"
    bl_description = "Move this path later in the search order."
    index: IntProperty()

    def execute(self, context):
        paths = _prefs(context).custom_top_level_paths
        target = self.index + 1
        if 0 <= self.index < len(paths) and 0 <= target < len(paths):
            paths.move(self.index, target)
            _prefs(context).custom_top_level_path_index = target
            _schedule_preferences_save()
            _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_AddArchive(bpy.types.Operator):
    bl_idname = "eta.add_archive"
    bl_label = "Mount Archive"
    bl_description = "Select a .scs or .zip archive to mount for use."
    # Keep the picker single-file: do not add a `files` CollectionProperty here.
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.scs;*.SCS;*.zip;*.ZIP", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        path = Path(bpy.path.abspath(self.filepath))
        if not path.is_file():
            self.report({"ERROR"}, "Archive cannot be found.")
            return {"CANCELLED"}
        config = _prefs(context)
        _ensure_archive_priority_order(config, Path(bpy.path.abspath(config.game_dir)).expanduser())
        archives = config.archives
        new_key = _archive_path_key(path)
        if any(item.path and _archive_path_key(item.path) == new_key for item in archives):
            self.report({"WARNING"}, "Archive is already mounted.")
            return {"CANCELLED"}
        item = archives.add()
        item.path = str(path)
        item.enabled = True
        archives.move(len(archives) - 1, 0)
        _schedule_preferences_save()
        _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_RemoveArchive(bpy.types.Operator):
    bl_idname = "eta.remove_archive"
    bl_label = "Remove Archive"
    bl_description = "Remove this archive from the mounted archive list."
    index: IntProperty()

    def execute(self, context):
        archives = _prefs(context).archives
        if 0 <= self.index < len(archives):
            archives.remove(self.index)
            _schedule_preferences_save()
            _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_ArchiveMoveUp(bpy.types.Operator):
    bl_idname = "eta.archive_move_up"
    bl_label = "Move Archive Up"
    bl_description = "Increase archive priority."
    index: IntProperty()

    def execute(self, context):
        config = _prefs(context)
        archives = config.archives
        selected = None
        occurrence = 0
        if 0 <= self.index < len(archives):
            selected = (archives[self.index].path, archives[self.index].enabled)
            occurrence = sum(1 for item in list(archives)[:self.index]
                             if (item.path, item.enabled) == selected)
        _ensure_archive_priority_order(config, Path(bpy.path.abspath(config.game_dir)).expanduser())
        if selected is not None:
            matches = [i for i, item in enumerate(archives)
                       if (item.path, item.enabled) == selected]
            self.index = matches[occurrence] if occurrence < len(matches) else self.index
        target = self.index - 1
        if 0 <= self.index < len(archives) and 0 <= target < len(archives):
            archives.move(self.index, target)
            _schedule_preferences_save()
            _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_ArchiveMoveDown(bpy.types.Operator):
    bl_idname = "eta.archive_move_down"
    bl_label = "Move Archive Down"
    bl_description = "Lower archive priority."
    index: IntProperty()

    def execute(self, context):
        config = _prefs(context)
        archives = config.archives
        selected = None
        occurrence = 0
        if 0 <= self.index < len(archives):
            selected = (archives[self.index].path, archives[self.index].enabled)
            occurrence = sum(1 for item in list(archives)[:self.index]
                             if (item.path, item.enabled) == selected)
        _ensure_archive_priority_order(config, Path(bpy.path.abspath(config.game_dir)).expanduser())
        if selected is not None:
            matches = [i for i, item in enumerate(archives)
                       if (item.path, item.enabled) == selected]
            self.index = matches[occurrence] if occurrence < len(matches) else self.index
        target = self.index + 1
        if 0 <= self.index < len(archives) and 0 <= target < len(archives):
            archives.move(self.index, target)
            _schedule_preferences_save()
            _schedule_truck_list_refresh(context.scene)
        return {"FINISHED"}


class ETA_OT_DownloadConverter(bpy.types.Operator):
    bl_idname = "eta.download_converter"
    bl_label = "Download ConverterPIX"
    bl_description = "Download ConverterPIX."

    def execute(self, context):
        build = CONVERTER_BUILDS.get(sys.platform)
        if build is None:
            self.report({"ERROR"}, "Automatic downloads are configured improperly.")
            return {"CANCELLED"}
        target = addon_config_dir() / build["filename"]
        temporary_path = None
        try:
            request = urllib.request.Request(build["url"], headers={"User-Agent": "BlenderAssembler"})
            digest = hashlib.sha256()
            byte_count = 0
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="converter_pix_", dir=target.parent, delete=False) as temp_file:
                temporary_path = Path(temp_file.name)
                with urllib.request.urlopen(request, timeout=60) as response:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        temp_file.write(block)
                        digest.update(block)
                        byte_count += len(block)
            actual_digest = digest.hexdigest()
            if byte_count < 100_000:
                raise OSError(f"download was unexpectedly small ({byte_count} bytes)")
            if actual_digest != build["sha256"]:
                raise OSError("Integrity verification failed; the downloaded executable was rejected.")
            if sys.platform == "linux":
                temporary_path.chmod(temporary_path.stat().st_mode | 0o111)
            os.replace(temporary_path, target)
            temporary_path = None
            _CONVERTER_HASH_CACHE.pop(str(target.resolve()), None)
            _prefs(context).converter_path = str(target)
        except Exception as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.report({"ERROR"}, f"ConverterPIX download failed: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, "Downloaded and verified ConverterPIX.")
        return {"FINISHED"}


class ETA_OT_RefreshTrucks(bpy.types.Operator):
    bl_idname = "eta.refresh_trucks"
    bl_label = "Refresh Trucks"
    bl_description = "Refresh trucks available for use."

    def execute(self, context):
        prefs = _prefs(context)
        state = context.scene.eta_state
        if _game_directory(prefs.game_dir) is None:
            message = "Set a valid Game Path to load trucks."
            _clear_loaded_truck_data(state, message)
            self.report({"WARNING"}, message)
            return {"CANCELLED"}
        try:
            trucks = _converter(context).list_trucks()
        except (ConverterError, OSError, ValueError) as exc:
            self.report({"ERROR"}, str(exc).splitlines()[0])
            return {"CANCELLED"}
        state.truck_options.clear()
        for truck in trucks:
            item = state.truck_options.add()
            item.name = item.label = truck
        if state.truck not in trucks:
            _set_truck_without_autoload(state, "")
        self.report({"INFO"}, f"Found {len(trucks)} trucks")
        return {"FINISHED"}


class ETA_OT_LoadTruck(bpy.types.Operator):
    bl_idname = "eta.load_truck"
    bl_label = "Load Truck"
    refresh_current: BoolProperty(options={"HIDDEN"})

    def execute(self, context):
        state = context.scene.eta_state
        previous_selection = _selection(state) if self.refresh_current else None
        previous_dealer_config = state.dealer_config if self.refresh_current else ""
        previous_dealer_loaded = state.dealer_config_loaded if self.refresh_current else False
        state.dealer_config_loaded = False
        if not state.truck:
            self.report({"ERROR"}, "Select a truck.")
            return {"CANCELLED"}
        try:
            conv = _converter(context)
            _load_accessory_locale(conv, Path(bpy.path.abspath(_prefs(context).game_dir)).expanduser(),
                                   _selected_locale_code(_prefs(context)))
            result = conv.extract_truck(state.truck)
            if result.error:
                raise ConverterError(result.error)
            defn = scan_truck(conv.defs, state.truck)
            state.defs_path = str(conv.defs)
            _fill_state(state, defn, previous_selection)
            state.dealer_options.clear()
            state.dealer_config = ""
            brand = state.truck.split(".", 1)[0]
            truck_configs = []
            unreadable_configs = []
            for path in conv.list_dealer_configs(brand):
                try:
                    dealer_path = conv.extract_file(path)
                    dealer = parse_dealer_file(dealer_path, conv.defs)
                except (ConverterError, OSError, SiiError):
                    unreadable_configs.append(path)
                    continue
                # Read the truck definition from the config itself. Filenames
                # can be arbitrary (for example renault_8.sii for renault.t).
                if dealer.brand_model == state.truck:
                    truck_configs.append(path)
            for path in truck_configs:
                item = state.dealer_options.add()
                item.name = item.label = path
            if self.refresh_current and previous_dealer_config in {x.name for x in state.dealer_options}:
                state.dealer_config = previous_dealer_config
                state.dealer_config_loaded = previous_dealer_loaded
            if unreadable_configs:
                state.status += (f"\nCould not inspect {len(unreadable_configs)} dealer config(s); "
                                 f"first: {Path(unreadable_configs[0]).name}")
            if not truck_configs and not unreadable_configs:
                state.status += "\nNo dealer configurations matched this truck model."
            stale = conv.stale_reasons()
            state.status = ("Stale cache: converted models/defs may be out of date. Clear cache to rebuild.\n"
                            if stale else "") + state.status
            self.report({"WARNING"} if stale else {"INFO"}, state.status.splitlines()[0])
        except (ConverterError, ValueError, OSError) as exc:
            self.report({"ERROR"}, str(exc).splitlines()[0])
            return {"CANCELLED"}
        return {"FINISHED"}


class ETA_OT_LoadDealer(bpy.types.Operator):
    bl_idname = "eta.load_dealer"
    bl_label = "Load Dealer Configuration"
    bl_description = "Load selected dealer configuration file and apply cabin, chassis and accessory options."

    def execute(self, context):
        state = context.scene.eta_state
        if not state.dealer_config:
            self.report({"ERROR"}, "Select a dealer configuration file.")
            return {"CANCELLED"}
        try:
            conv = _converter(context)
            _load_accessory_locale(conv, Path(bpy.path.abspath(_prefs(context).game_dir)).expanduser(),
                                   _selected_locale_code(_prefs(context)))
            path = conv.extract_file(state.dealer_config)
            cfg = parse_dealer_file(path, conv.defs)
            if not cfg.brand_model:
                raise ConverterError("Dealer configuration file doesn't identify a truck.")
            result = conv.extract_truck(cfg.brand_model)
            if result.error:
                raise ConverterError(result.error)
            defn = scan_truck(conv.defs, cfg.brand_model)
            dealer = selection_from_dealer(cfg, defn)
            _set_truck_without_autoload(state, cfg.brand_model)
            state.defs_path = str(conv.defs)
            _fill_state(state, defn, dealer.selection)
            if state.enforce_defaults:
                _apply_defaults_and_refresh(context)
            state.dealer_config_loaded = True
            state.status += f"\nDealer config: {Path(state.dealer_config).name}; {len(dealer.skipped)} dealer entries deferred"
        except (ConverterError, ValueError, OSError) as exc:
            self.report({"ERROR"}, str(exc).splitlines()[0])
            return {"CANCELLED"}
        self.report({"INFO"}, "Dealer configuration loaded")
        return {"FINISHED"}


class ETA_OT_ResetSelection(bpy.types.Operator):
    bl_idname = "eta.reset_selection"
    bl_label = "Reset"
    bl_description = "Clear the selected truck, dealer configuration, cabin, chassis and accessories."

    def execute(self, context):
        global _RULE_UPDATE_GUARD
        state = context.scene.eta_state
        previous_guard = _RULE_UPDATE_GUARD
        _RULE_UPDATE_GUARD = True
        try:
            _set_truck_without_autoload(state, "")
            state.dealer_config = ""
            state.dealer_config_loaded = False
            state.dealer_options.clear()
            state.defs_path = ""
            state.chassis = ""
            state.cabin = ""
            _clear_cabin_default_tracking(state)
            state.interior = ""
            state.chassis_options.clear()
            state.cabin_options.clear()
            state.slots.clear()
            state.active_slot_index = 0
            state.rule_message = ""
            state.status = "Selection reset."
        finally:
            _RULE_UPDATE_GUARD = previous_guard
        self.report({"INFO"}, "Selection reset.")
        return {"FINISHED"}


def _restore_assembly_scs_state() -> None:
    global _ASSEMBLY_CLEANUP
    cleanup, _ASSEMBLY_CLEANUP = _ASSEMBLY_CLEANUP, None
    if cleanup is not None:
        cleanup()


def _assembly_notice(level: str, message: str) -> None:
    try:
        bpy.ops.eta.assembly_notice("EXEC_DEFAULT", level=level, message=message[:1000])
    except Exception as exc:
        print(f"Blender Assembler {level.lower()}: {message}; notice failed: {exc}")


def _assembly_did_complete(state) -> bool:
    return state.status.endswith("Assembly complete!")


class ETA_OT_AssemblyNotice(bpy.types.Operator):
    bl_idname = "eta.assembly_notice"
    bl_label = "Assembly Status"
    bl_options = {"INTERNAL"}
    level: EnumProperty(items=(
        ("INFO", "Info", ""), ("WARNING", "Warning", ""), ("ERROR", "Error", "")), default="INFO")
    message: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        self.report({self.level}, self.message)
        return {"FINISHED"}


class ETA_OT_Assemble(bpy.types.Operator):
    bl_idname = "eta.assemble"
    bl_label = "Assemble"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}
    replace_existing: BoolProperty(options={"HIDDEN"})
    scs_prepared: BoolProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        """Wait for SCS Tools path setup, then run assembly as a redraw-friendly modal operator."""
        global _ASSEMBLE_TIMER_PENDING, _ASSEMBLY_CLEANUP
        if self.scs_prepared:
            return self.execute(context)
        state = context.scene.eta_state
        required_error = _required_options_error(state)
        if required_error:
            self.report({"ERROR"}, required_error)
            return {"CANCELLED"}
        if _ASSEMBLE_TIMER_PENDING or _ASSEMBLY_JOB_RUNNING:
            self.report({"WARNING"}, "An assembly is already running or waiting for SCS Tools initialization.")
            return {"CANCELLED"}
        state.status = ""
        try:
            conv = _converter(context)
            ensure_scs_enabled()
            from io_scs_tools.internals.containers.config import AsyncPathsInit
            globals_ = scs_globals()
            old_project_path = globals_.scs_project_path
            config_path = scs_config_path()
            config_bytes = config_path.read_bytes() if config_path.is_file() else None
            wanted = str(conv.base)
            if os.path.normcase(os.path.normpath(old_project_path)) != os.path.normcase(os.path.normpath(wanted)):
                globals_.scs_project_path = wanted

            def restore_scs_config():
                try:
                    globals_["scs_project_path"] = old_project_path
                finally:
                    if config_bytes is not None:
                        config_path.write_bytes(config_bytes)

            _ASSEMBLY_CLEANUP = restore_scs_config
            if not AsyncPathsInit.is_running():
                return self.execute(context)

            window, area, region, scene = context.window, context.area, context.region, context.scene
            replace_existing = self.replace_existing
            _ASSEMBLE_TIMER_PENDING = True
            self.report({"INFO"}, "Waiting for SCS Tools path initialization; assembly will start automatically")

            def finish_when_ready():
                global _ASSEMBLE_TIMER_PENDING
                if AsyncPathsInit.is_running():
                    return 0.2
                try:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        result = bpy.ops.eta.assemble(
                            "EXEC_DEFAULT", replace_existing=replace_existing, scs_prepared=True)
                        if "FINISHED" in result and _assembly_did_complete(scene.eta_state):
                            bpy.ops.eta.assembly_notice(
                                "EXEC_DEFAULT", level="INFO", message="Assembly complete!")
                            _redraw_view3d_areas()
                    if "CANCELLED" in result:
                        _restore_assembly_scs_state()
                except Exception as exc:
                    _restore_assembly_scs_state()
                    _assembly_notice("ERROR", str(exc).splitlines()[0])
                finally:
                    _ASSEMBLE_TIMER_PENDING = False
                return None

            bpy.app.timers.register(finish_when_ready, first_interval=0.2)
            return {"FINISHED"}
        except Exception as exc:
            _restore_assembly_scs_state()
            self.report({"ERROR"}, str(exc).splitlines()[0])
            return {"CANCELLED"}

    def execute(self, context):
        global _ASSEMBLY_CLEANUP, _ASSEMBLY_JOB_RUNNING
        state = context.scene.eta_state
        required_error = _required_options_error(state)
        if required_error:
            _restore_assembly_scs_state()
            self.report({"ERROR"}, required_error)
            return {"CANCELLED"}
        if _ASSEMBLY_JOB_RUNNING:
            _restore_assembly_scs_state()
            self.report({"WARNING"}, "An assembly is already running.")
            return {"CANCELLED"}
        state.status = ""
        cleanup = None
        try:
            _refresh_rule_state(context, preserve_status=True)
            rule_message = state.rule_message
            conv = _converter(context)
            opts = PlanOptions(rhd=state.drive_side == "UK", strict=state.strict)
            prep = _prepare_with_progress(context, conv, selection=_selection(state), options=opts)
            dealer_skipped = []
            if state.dealer_config_loaded and state.dealer_config:
                dealer_path = conv.extract_file(state.dealer_config)
                dealer_cfg = parse_dealer_file(dealer_path, conv.defs)
                dealer_result = selection_from_dealer(dealer_cfg, prep.defn)
                dealer_skipped = list(dealer_result.skipped)
            cleanup = _ASSEMBLY_CLEANUP
            _ASSEMBLY_CLEANUP = None
            _ASSEMBLY_JOB_RUNNING = True
            workspace = context.workspace
            wm = context.window_manager
            def update_import_status(done, total, label):
                workspace.status_text_set(f"Importing {label} ({done}/{total})")
                try:
                    bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
                except (AttributeError, RuntimeError):
                    for area in context.screen.areas:
                        area.tag_redraw()

            _, report = assemble(prep, conv.base, context.scene, progress=update_import_status,
                                 reuse_existing=self.replace_existing,
                                 archive_fingerprint={
                                     "archives": conv.fingerprint(),
                                     "custom_top_level_paths": list(conv.s.custom_top_level_paths),
                                 })
            if rule_message:
                report.notes.append(rule_message)
            for entry in dealer_skipped:
                report.skipped.append(SkippedItem(
                    entry.local.lstrip("."), entry.data_path or None, None,
                    f"dealer entry ({entry.class_name}): {entry.reason}"))
            message = f"Stale cache: {report.summary()}" if prep.stale else report.summary()
            state.status = f"{message}\nAssembly complete!"
            self.report({"WARNING"} if prep.stale else {"INFO"}, message[:1000])
            return {"FINISHED"}
        except ScsBusy as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except (AssemblyError, ConverterError, ValueError, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            state.status = f"Assembly failed: {exc}"
            self.report({"ERROR"}, str(exc).splitlines()[0] or type(exc).__name__)
            return {"CANCELLED"}
        finally:
            try:
                context.workspace.status_text_set(None)
            except (AttributeError, ReferenceError, RuntimeError):
                pass
            _ASSEMBLY_JOB_RUNNING = False
            if cleanup is not None:
                try:
                    cleanup()
                except Exception as exc:
                    print(f"Blender Assembler could not restore SCS Tools settings: {exc}")
            elif _ASSEMBLY_CLEANUP is not None:
                _restore_assembly_scs_state()


def _remove_truck_assembly(truck: str) -> bool:
    collection = bpy.data.collections.get(f"Blender Assembler - {truck}")
    if collection is None:
        return False
    for obj in list(collection.all_objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    tree = []

    def collect(item):
        for child in list(item.children):
            collect(child)
        tree.append(item)

    collect(collection)
    for item in tree:
        bpy.data.collections.remove(item)
    return True


class ETA_OT_ConfirmAssemble(bpy.types.Operator):
    bl_idname = "eta.confirm_assemble"
    bl_label = "Assemble"
    bl_description = "Convert and assemble the truck."
    bl_options = {"INTERNAL"}
    replace_existing: BoolProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        state = context.scene.eta_state
        required_error = _required_options_error(state)
        if required_error:
            self.report({"ERROR"}, required_error)
            return {"CANCELLED"}
        truck = state.truck
        collection = bpy.data.collections.get(f"Blender Assembler - {truck}")
        if collection is None:
            result = bpy.ops.eta.assemble("INVOKE_DEFAULT", replace_existing=False)
            if ("FINISHED" in result and not _ASSEMBLE_TIMER_PENDING
                    and _assembly_did_complete(state)):
                self.report({"INFO"}, "Assembly complete!")
            return result
        self.replace_existing = True
        return context.window_manager.invoke_confirm(
            self, event, title="Truck Already Exists",
            message=f"Do you want to replace {truck}?",
            confirm_text="Replace", icon="WARNING",
        )

    def execute(self, context):
        state = context.scene.eta_state
        result = bpy.ops.eta.assemble("INVOKE_DEFAULT", replace_existing=self.replace_existing)
        if ("FINISHED" in result and not _ASSEMBLE_TIMER_PENDING
                and _assembly_did_complete(state)):
            self.report({"INFO"}, "Assembly complete!")
        return result


class ETA_OT_ClearCache(bpy.types.Operator):
    bl_idname = "eta.clear_cache"
    bl_label = "Clear Cache"
    bl_description = "Delete cached definitions and converted model data. They will be rebuilt when needed."
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            _converter(context).clear_cache()
        except (ConverterError, OSError) as exc:
            self.report({"ERROR"}, str(exc).splitlines()[0])
            return {"CANCELLED"}
        self.report({"INFO"}, "Cache cleared.")
        return {"FINISHED"}


class ETA_PT_Setup(bpy.types.Panel):
    bl_label = "Blender Assembler"
    bl_idname = "ETA_PT_setup"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blender Assembler"

    def draw(self, context):
        pass


def _draw_config(layout, context, config, *, show_tool_only_controls=False):
    game_row = layout.row()
    game_row.alert = not Path(bpy.path.abspath(config.game_dir)).expanduser().is_dir()
    game_row.prop(config, "game_dir")

    exe = _find_converter(config.converter_path or None)
    scene = getattr(context, "scene", None)
    if exe is not None and config.converter_path != str(exe) and scene is not None:
        _schedule_converter_path_autofill(scene, exe)
    row = layout.row(align=True)
    configured_exe = Path(bpy.path.abspath(config.converter_path)).expanduser() if config.converter_path else None
    row.alert = not (configured_exe.is_file() if configured_exe else exe is not None)
    row.prop(config, "converter_path")
    selected_exe = configured_exe if configured_exe and configured_exe.is_file() else exe
    expected_build = CONVERTER_BUILDS.get(sys.platform)
    installed_digest = None
    if selected_exe is not None and selected_exe.is_file():
        try:
            stat = selected_exe.stat()
            cache_key = str(selected_exe.resolve())
            signature = (stat.st_size, stat.st_mtime_ns)
            cached = _CONVERTER_HASH_CACHE.get(cache_key)
            if cached is None or cached[0] != signature:
                cached = (signature, sha256_file(selected_exe))
                _CONVERTER_HASH_CACHE[cache_key] = cached
            installed_digest = cached[1]
        except OSError:
            installed_digest = None
    if expected_build is not None and not is_pinned_digest(installed_digest or "", sys.platform):
        row.operator("eta.download_converter", text="Download", icon="IMPORT")

    cache_row = layout.row()
    cache_row.alert = not Path(bpy.path.abspath(config.cache_dir)).expanduser().is_dir()
    cache_row.prop(config, "cache_dir")
    if show_tool_only_controls:
        layout.operator("eta.clear_cache", icon="FILE_REFRESH")

    row = layout.row(align=True)
    row.prop_search(config, "locale", config, "locales", text="Language")
    row.operator("eta.refresh_locales", text="", icon="FILE_REFRESH")
    layout.prop(config, "jobs")

    if show_tool_only_controls:
        custom_header, custom_body = layout.panel("eta_custom_top_level_paths", default_closed=True)
        custom_header.label(text="Custom Top-Level Paths")
        if custom_body:
            for i, item in enumerate(config.custom_top_level_paths):
                row = custom_body.row(align=True)
                row.prop(item, "path", text="")
                op = row.operator("eta.move_custom_top_level_path_up", text="", icon="TRIA_UP")
                op.index = i
                op = row.operator("eta.move_custom_top_level_path_down", text="", icon="TRIA_DOWN")
                op.index = i
                op = row.operator("eta.remove_custom_top_level_path", text="", icon="REMOVE")
                op.index = i
            row = custom_body.row(align=True)
            row.operator("eta.add_custom_top_level_path", text="Add Path", icon="ADD")

        archive_header, archive_body = layout.panel("eta_archive_list", default_closed=True)
        archive_header.label(text="Archives")
        if archive_body:
            row = archive_body.row(align=True)
            row.operator("eta.add_archive", text="Mount Archive", icon="ADD")
            for i, archive in enumerate(config.archives):
                row = archive_body.row(align=True)
                row.prop(archive, "path", text="")
                op = row.operator("eta.archive_move_up", text="", icon="TRIA_UP")
                op.index = i
                op = row.operator("eta.archive_move_down", text="", icon="TRIA_DOWN")
                op.index = i
                op = row.operator("eta.remove_archive", text="", icon="REMOVE")
                op.index = i


class ETA_PT_ToolConfig(bpy.types.Panel):
    bl_label = "Blender Assembler"
    bl_idname = "ETA_PT_tool_config"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Tool"

    def draw(self, context):
        config = _prefs(context)
        _schedule_locale_label_migration(context.scene)
        _draw_config(self.layout, context, config, show_tool_only_controls=True)


class ETA_PT_Base(bpy.types.Panel):
    bl_label = "Setup"
    bl_idname = "ETA_PT_base"
    bl_space_type = "VIEW_3D"; bl_region_type = "UI"; bl_category = "Blender Assembler"
    bl_parent_id = "ETA_PT_setup"

    def draw(self, context):
        layout=self.layout; s=context.scene.eta_state
        has_selection = any((s.truck, s.dealer_config, s.chassis, s.cabin, s.interior)) or any(
            slot.chosen for slot in s.slots)
        reset_row = layout.row()
        reset_row.enabled = bool(has_selection)
        reset_row.operator("eta.reset_selection", text="Reset")
        truck_row = layout.row(align=True)
        truck_row.prop_search(s, "truck", s, "truck_options", text="Truck")
        truck_row.operator("eta.refresh_trucks", text="", icon="FILE_REFRESH")
        drive_row = layout.row(align=True)
        drive_row.prop_enum(s, "drive_side", "EU", text="EU")
        drive_row.prop_enum(s, "drive_side", "UK", text="UK")
        enforcement_column = layout.column(align=True)
        enforcement_column.prop(s, "strict", text="Enforce Compatibility", toggle=True)
        enforcement_column.prop(s, "enforce_defaults", text="Enforce Defaults", toggle=True)
        if not s.strict:
            layout.label(text="Compatibility enforcement is disabled — expect compatibility problems!",
                         icon="INFO")
        layout.prop_search(s, "dealer_config", s, "dealer_options", text="Dealer Configuration")
        layout.operator("eta.load_dealer", text="Load Dealer Configuration")


class ETA_PT_Cabin(bpy.types.Panel):
    bl_label = "Cabin"
    bl_idname = "ETA_PT_cabin"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blender Assembler"
    bl_parent_id = "ETA_PT_setup"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        state = context.scene.eta_state
        layout.prop_search(state, "cabin", state, "cabin_options", text="")
        selected = next((item for item in state.cabin_options if item.name == state.cabin), None)
        _draw_selected_option(layout, context, selected)


class ETA_PT_Chassis(bpy.types.Panel):
    bl_label = "Chassis"
    bl_idname = "ETA_PT_chassis"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blender Assembler"
    bl_parent_id = "ETA_PT_setup"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        state = context.scene.eta_state
        layout.prop_search(state, "chassis", state, "chassis_options", text="")
        selected = next((item for item in state.chassis_options if item.name == state.chassis), None)
        _draw_selected_option(layout, context, selected)


class ETA_UL_Accessories(bpy.types.UIList):
    bl_idname = "ETA_UL_accessories"
    use_filter_show = True

    def draw_filter(self, context, layout):
        layout.prop(context.scene.eta_state, "filter_text", text="Search")

    def filter_items(self, context, data, propname):
        slots = getattr(data, propname)
        query = context.scene.eta_state.filter_text.strip().casefold()
        flags = [self.bitflag_filter_item if not query or query in slot.name.casefold() else 0
                 for slot in slots]
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index, flt_flag=0):
        selected = next((option for option in item.options if option.name == item.chosen), None)
        status_icon = ("ERROR" if selected and not selected.valid else
                       "CHECKMARK" if item.chosen else "DOT")
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.label(text=item.name + (" *" if item.legacy else ""), icon=status_icon)
            count = row.row(align=True)
            count.alignment = "RIGHT"
            count.label(text=str(len(item.options)))
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.name, icon=status_icon)


class ETA_PT_Accessories(bpy.types.Panel):
    bl_label = "Accessories"
    bl_idname = "ETA_PT_accessories"
    bl_space_type="VIEW_3D"; bl_region_type="UI"; bl_category="Blender Assembler"
    bl_parent_id="ETA_PT_setup"; bl_options={"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        state = context.scene.eta_state
        layout.template_list("ETA_UL_accessories", "", state, "slots", state,
                             "active_slot_index", rows=8, maxrows=12)
        if not state.slots:
            return
        index = min(state.active_slot_index, len(state.slots) - 1)
        slot = state.slots[index]
        query = state.filter_text.strip().casefold()
        if query and query not in slot.name.casefold():
            return
        box = layout.box()
        box.label(text=slot.name + (" *" if slot.legacy else ""))
        box.prop_search(slot, "chosen", slot, "options", text="")
        selected = next((option for option in slot.options if option.name == slot.chosen), None)
        if selected is None:
            box.label(text="No accessory selected")
        else:
            _draw_selected_option(box, context, selected)


class ETA_PT_Assemble(bpy.types.Panel):
    bl_label="Assemble"; bl_idname="ETA_PT_assemble"
    bl_space_type="VIEW_3D"; bl_region_type="UI"; bl_category="Blender Assembler"
    bl_parent_id="ETA_PT_setup"; bl_options={"DEFAULT_CLOSED"}

    def draw(self,context):
        layout=self.layout; s=context.scene.eta_state
        button = layout.row(align=True)
        button.scale_y = 2
        button.operator("eta.confirm_assemble", text="Assemble")


CLASSES=(ETA_PG_Option,ETA_PG_Archive,ETA_PG_Locale,ETA_PG_CustomTopLevelPath,ETA_PG_Config,ETA_PG_Slot,ETA_PG_State,
         ETA_OT_RefreshArchives,ETA_OT_RefreshLocales,
         ETA_OT_AddCustomTopLevelPath,ETA_OT_RemoveCustomTopLevelPath,
         ETA_OT_MoveCustomTopLevelPathUp,ETA_OT_MoveCustomTopLevelPathDown,
         ETA_OT_AddArchive,ETA_OT_RemoveArchive,ETA_OT_DownloadConverter,
         ETA_OT_ArchiveMoveUp,ETA_OT_ArchiveMoveDown,
         ETA_OT_RefreshTrucks,ETA_OT_LoadTruck,ETA_OT_LoadDealer,ETA_OT_ResetSelection,
         ETA_OT_AssemblyNotice,ETA_OT_Assemble,ETA_OT_ConfirmAssemble,
         ETA_OT_ClearCache,
         ETA_PT_Setup,ETA_PT_ToolConfig,ETA_PT_Base,ETA_PT_Cabin,ETA_PT_Chassis,
         ETA_UL_Accessories,ETA_PT_Accessories,ETA_PT_Assemble)


def _schedule_startup_truck_refresh() -> None:
    """Refresh the active scene's truck list after Blender finishes starting up."""
    global _STARTUP_TRUCK_REFRESH_TIMER
    attempts = 0

    def refresh_trucks_after_startup():
        nonlocal attempts
        attempts += 1
        context = bpy.context
        scene = getattr(context, "scene", None)
        if scene is None or not hasattr(scene, "eta_state"):
            return 0.5 if attempts < 10 else None
        try:
            config = _prefs(context)
            if not config.game_dir.strip():
                return None
            game_dir = Path(bpy.path.abspath(config.game_dir)).expanduser()
            if not game_dir.is_dir():
                return None
            trucks = _converter(context).list_trucks()
            state = scene.eta_state
            state.truck_options.clear()
            for truck in trucks:
                item = state.truck_options.add()
                item.name = item.label = truck
            if state.truck not in trucks:
                _set_truck_without_autoload(state, "")
        except (ConverterError, OSError, ReferenceError, RuntimeError) as exc:
            print(f"Blender Assembler could not refresh trucks at startup: {exc}")
        return None

    _STARTUP_TRUCK_REFRESH_TIMER = refresh_trucks_after_startup
    bpy.app.timers.register(refresh_trucks_after_startup, first_interval=1.0)


def register() -> None:
    for cls in CLASSES: bpy.utils.register_class(cls)
    if hasattr(bpy.types.Scene, "eta_config"):
        del bpy.types.Scene.eta_config
    bpy.types.Scene.eta_state=bpy.props.PointerProperty(type=ETA_PG_State)
    if not bpy.app.background:
        _schedule_startup_truck_refresh()


def unregister() -> None:
    global _ICON_PREVIEWS, _MISSING_ICON_ID, _STARTUP_TRUCK_REFRESH_TIMER, _ICON_LOAD_TIMER
    if (_STARTUP_TRUCK_REFRESH_TIMER is not None
            and bpy.app.timers.is_registered(_STARTUP_TRUCK_REFRESH_TIMER)):
        bpy.app.timers.unregister(_STARTUP_TRUCK_REFRESH_TIMER)
    _STARTUP_TRUCK_REFRESH_TIMER = None
    if _ICON_LOAD_TIMER is not None and bpy.app.timers.is_registered(_ICON_LOAD_TIMER):
        bpy.app.timers.unregister(_ICON_LOAD_TIMER)
    _ICON_LOAD_TIMER = None
    _ICON_QUEUE.clear()
    _ICON_QUEUED.clear()
    for pending in list(_LOCALE_REFRESH_PENDING.values()):
        timer = pending.get("timer")
        if timer is not None and bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    _LOCALE_REFRESH_PENDING.clear()
    if hasattr(bpy.types.Scene,"eta_state"): del bpy.types.Scene.eta_state
    if hasattr(bpy.types.Scene,"eta_config"): del bpy.types.Scene.eta_config
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
    if _ICON_PREVIEWS is not None:
        bpy.utils.previews.remove(_ICON_PREVIEWS)
        _ICON_PREVIEWS = None
    _ICON_VALUES.clear()
    _ICON_MISSING.clear()
    _ICON_ERRORS.clear()
    _MISSING_ICON_ID = 0
