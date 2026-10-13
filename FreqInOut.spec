# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import json
import runpy
import sys

import PySide6


ROOT = Path(SPECPATH)
QT_ROOT = Path(PySide6.__file__).resolve().parent / "Qt"
# Validate the same release inputs used by workflow/package builders before freezing.
APP_VERSION = runpy.run_path(str(ROOT / "packaging" / "verify_release_inputs.py"))[
    "validated_version"
](ROOT)
# PyInstaller injects the build directory as lowercase `workpath`.
PRODUCT_METADATA = Path(workpath) / "commsplan-product.json"
PRODUCT_METADATA.parent.mkdir(parents=True, exist_ok=True)
PRODUCT_METADATA.write_text(
    json.dumps({"product": "CommsPlan", "version": APP_VERSION, "legacy_product": "FreqInOut"}) + "\n",
    encoding="utf-8",
)
MACOS_CODESIGN_IDENTITY = os.environ.get("FIO_CODESIGN_IDENTITY") or None
MACOS_ENTITLEMENTS = (
    str(ROOT / "packaging" / "macos" / "entitlements.plist")
    if MACOS_CODESIGN_IDENTITY
    else None
)
MACOS_ICON = ROOT / "assets" / "CommsPlan.icns"


def qml_module_files(module_name):
    """Collect only the root files of a Qt QML module unless it is tiny."""
    module_dir = QT_ROOT / "qml" / module_name
    if not module_dir.exists():
        return []
    if module_name in {"QtLocation", "QtPositioning", "QtQuick/Controls", "QtQuick/Templates"}:
        return [(str(module_dir), f"PySide6/Qt/qml/{module_name}")]
    return [
        (str(path), f"PySide6/Qt/qml/{module_name}")
        for path in module_dir.iterdir()
        if path.is_file()
    ]


qt_qml_datas = []
for qml_module in (
    "QtLocation",
    "QtPositioning",
    "QtQml",
    "QtQuick",
    "QtQuick/Controls",
    "QtQuick/Templates",
):
    qt_qml_datas.extend(qml_module_files(qml_module))

qt_plugin_binaries = []
for plugin_group in ("geoservices", "position"):
    plugin_dir = QT_ROOT / "plugins" / plugin_group
    if plugin_dir.exists():
        plugin_files = [path for path in plugin_dir.iterdir() if path.is_file()]
        if plugin_group == "geoservices":
            # The Map contract is provider-free. Ship only Qt's coordinate
            # item-overlay backend, never an online tile-provider fallback.
            plugin_files = [path for path in plugin_files if "itemsoverlay" in path.name.lower()]
        qt_plugin_binaries.extend(
            (str(path), f"PySide6/Qt/plugins/{plugin_group}")
            for path in plugin_files
        )


a = Analysis(
    ['freqinout/main.py'],
    pathex=['.'],
    binaries=qt_plugin_binaries,
    datas=[
        (str(PRODUCT_METADATA), "."),
        ('freqinout/gui/qml', 'freqinout/gui/qml'),
        ('freqinout/resources/spotter_forms', 'freqinout/resources/spotter_forms'),
        ('config/leaflet', 'config/leaflet'),
        ('config/net_resources', 'config/net_resources'),
        ('config/propagation/prop_climatology.db', 'config/propagation'),
        ('config/propagation/prop_profiles.json', 'config/propagation'),
        ('config/resource_catalog', 'config/resource_catalog'),
        ('config/shortwave/eibi', 'config/shortwave/eibi'),
        ('assets', 'assets'),
        ('docs/guide.html', 'docs'),
        ('docs/CommsPlan-migration.md', 'docs'),
        ('third_party/js8net/js8net-main/js8net.py', 'third_party/js8net/js8net-main'),
        ('third_party/js8net/js8net-main/LICENSE', 'third_party/js8net/js8net-main'),
    ] + qt_qml_datas,
    hiddenimports=[
        "PySide6.QtLocation",
        "PySide6.QtPositioning",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets",
        "tzdata",
        "keyring.backends.chainer",
        "keyring.backends.fail",
        "keyring.backends.kwallet",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.Windows",
        "freqinout.core.mesh.linux_ble_supervisor",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['packaging/pyinstaller_runtime_qt.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CommsPlan',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=MACOS_CODESIGN_IDENTITY,
    entitlements_file=MACOS_ENTITLEMENTS,
    icon=(
        'assets/CommsPlan.ico'
        if sys.platform == 'win32'
        else str(MACOS_ICON) if sys.platform == 'darwin' and MACOS_ICON.is_file() else None
    ),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CommsPlan',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='CommsPlan.app',
        icon=str(MACOS_ICON) if MACOS_ICON.is_file() else None,
        bundle_identifier='org.n1mag.freqinout',
        version=APP_VERSION,
        codesign_identity=MACOS_CODESIGN_IDENTITY,
        entitlements_file=MACOS_ENTITLEMENTS,
        info_plist={
            'CFBundleDisplayName': 'CommsPlan',
            'CFBundleName': 'CommsPlan',
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'LSApplicationCategoryType': 'public.app-category.utilities',
            'NSHighResolutionCapable': True,
        },
    )
