"""Choose the Qt Quick backend before Qt creates any graphics surfaces."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


def configure_qt_quick_backend(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Default Linux to software rendering while honoring explicit Qt choices.

    Qt's GLX platform integration can abort before a QQuickWidget emits its
    recoverable sceneGraphError signal. Select the tested software backend at
    process startup instead of attempting recovery after graphics creation.
    """
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform.startswith("linux") and not any(
        environ.get(key, "").strip()
        for key in ("QT_QUICK_BACKEND", "QMLSCENE_DEVICE", "QSG_RHI_BACKEND")
    ):
        from PySide6.QtQuick import QQuickWindow

        # Keep this choice process-local: setting an environment default here
        # would also change the backend of Qt applications we launch.
        QQuickWindow.setSceneGraphBackend("software")
        return "software"
    return (
        environ.get("QT_QUICK_BACKEND")
        or environ.get("QMLSCENE_DEVICE")
        or environ.get("QSG_RHI_BACKEND")
        or ""
    )
