"""Radio-owned CAT, PTT, and audio assignment editor."""

from __future__ import annotations

import sys
from typing import Any, Mapping

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.hardware_inventory import HardwareInventorySnapshot, discover_hardware_inventory
from freqinout.core.hardware_reconciliation import reconcile_radio_hardware
from freqinout.core.multi_radio_store import MultiRadioStore
from freqinout.core.radio_hardware_binding import HardwareBindingError, normalize_hardware_binding
from freqinout.gui.theme import active_app_theme, button_style


_ROLE_PRESENTATION = (
    ("cat", "Radio control (CAT)", "Controls frequency and mode and provides radio status to CommsPlan."),
    ("ptt", "Transmit keying (PTT)", "A separate CommsPlan PTT interface is optional. Applications may have their own keying path; CommsPlan never keys the transmitter during setup."),
    ("audio_rx", "Receive audio", "Audio received from this radio by decoding applications."),
    ("audio_tx", "Transmit audio", "Audio sent from radio applications to this radio. A missing CommsPlan-managed device blocks transmit audio."),
)

_OBSERVER_ROLE_PRESENTATION = (
    (
        "cat",
        "Receiver control (optional)",
        "Optional receive-only tuning or receiver control. This profile cannot key PTT or transmit.",
    ),
    ("audio_rx", "Receive audio input", "The decoder input assigned to this receive-only radio."),
)

_VIRTUAL_AUDIO_HINTS = ("monitor", "virtual", "loopback", "null sink", "mix")


class _InventoryWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(discover_hardware_inventory())
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            self.failed.emit(str(exc) or exc.__class__.__name__)


class RadioHardwareConnectionsDialog(QDialog):
    """Edit one radio's canonical physical connection bundle.

    Discovery runs only on explicit refresh in a worker. The dialog itself
    renders saved/cached values and never opens a serial or audio device.
    """

    def __init__(
        self,
        store: MultiRadioStore,
        radio_profile: Mapping[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.radio_profile = dict(radio_profile)
        self.radio_id = int(self.radio_profile.get("id", 0) or 0)
        self.radio_name = str(self.radio_profile.get("name", "") or "Radio").strip() or "Radio"
        self.is_receive_only = (
            str(self.radio_profile.get("device_class", "tx_rx") or "tx_rx").strip().lower()
            == "observer"
        )
        self._role_presentation = _OBSERVER_ROLE_PRESENTATION if self.is_receive_only else _ROLE_PRESENTATION
        self.saved_bundle = self.store.get_radio_hardware_bundle(self.radio_id)
        self._bundle_generation = int((self.saved_bundle or {}).get("generation", 0) or 0)
        if self.saved_bundle is not None and str(self.saved_bundle.get("lifecycle_state", "")) == "retired":
            self.saved_bundle = None
        self._inventory: HardwareInventorySnapshot | None = None
        self._inventory_thread: QThread | None = None
        self._inventory_worker: _InventoryWorker | None = None
        self._closing_requested = False
        self._role_combos: dict[str, QComboBox] = {}
        self._manual_edits: dict[str, QLineEdit] = {}
        self._manual_confirmations: dict[str, QCheckBox] = {}

        self.setWindowTitle(f"Radio connections — {self.radio_name}")
        self.setObjectName("radioHardwareConnectionsDialog")
        self.setModal(True)
        self.resize(760, 650)
        self.setMinimumSize(580, 460)
        self._build_ui()
        self._load_saved_choices()
        self._refresh_review()

    def _build_ui(self) -> None:
        theme = active_app_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        mode_label = "Observer / SDR — receive-only" if self.is_receive_only else "Transceiver"
        title = QLabel(f"Connections for {self.radio_name} · {mode_label}")
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        if self.is_receive_only:
            intro = QLabel("Review which physical devices belong to this radio and who manages each connection. "
                           "For supported devices with stable identity, CommsPlan can remember assignments across restarts and USB port changes. "
                           "Scanning and saving do not tune, key, transmit, or restart radio software.")
        else:
            intro = QLabel("Review which physical devices belong to this radio and who manages each connection. "
                           "For supported devices with stable identity, CommsPlan can remember assignments across restarts and USB port changes. "
                           "Scanning and saving do not tune, key, transmit, or restart radio software.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        status_row = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("hardwareConnectionStatus")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        self.refresh_button = QPushButton("Scan connected devices")
        self.refresh_button.setObjectName("hardwareInventoryRefresh")
        self.refresh_button.setAccessibleName("Scan connected devices")
        self.refresh_button.setStyleSheet(button_style("info", theme))
        self.refresh_button.clicked.connect(self._start_inventory_refresh)
        status_row.addWidget(self.refresh_button)
        layout.addLayout(status_row)

        scroll = QScrollArea()
        scroll.setObjectName("hardwareConnectionsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(9)
        for role, title_text, detail_text in self._role_presentation:
            group = QGroupBox(title_text)
            group.setObjectName(f"hardwareRole_{role}")
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(10, 10, 10, 10)
            group_layout.setSpacing(6)
            detail = QLabel(detail_text)
            detail.setWordWrap(True)
            group_layout.addWidget(detail)
            combo = QComboBox()
            combo.setObjectName(f"hardwareChoice_{role}")
            combo.setAccessibleName(f"{title_text} assignment")
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            combo.currentIndexChanged.connect(lambda _index, selected_role=role: self._choice_changed(selected_role))
            group_layout.addWidget(combo)
            manual_widget = QWidget()
            manual_layout = QFormLayout(manual_widget)
            manual_layout.setContentsMargins(0, 0, 0, 0)
            manual_edit = QLineEdit()
            manual_edit.setObjectName(f"hardwareManualLocator_{role}")
            manual_edit.setPlaceholderText(
                "/dev/serial/by-id/..."
                if role in {"cat", "ptt"}
                else "Exact audio endpoint/node identity"
            )
            manual_edit.textChanged.connect(self._refresh_review)
            manual_confirm = QCheckBox("I understand this locator may require confirmation after reconnect")
            manual_confirm.setObjectName(f"hardwareManualConfirm_{role}")
            manual_confirm.stateChanged.connect(self._refresh_review)
            manual_layout.addRow("Exact locator:", manual_edit)
            manual_layout.addRow("", manual_confirm)
            manual_widget.setObjectName(f"hardwareManual_{role}")
            manual_widget.setVisible(False)
            group_layout.addWidget(manual_widget)
            combo.setProperty("manual_widget", manual_widget)
            self._role_combos[role] = combo
            self._manual_edits[role] = manual_edit
            self._manual_confirmations[role] = manual_confirm
            body_layout.addWidget(group)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        review_group = QGroupBox("Current behavior")
        self.review_group = review_group
        review_layout = QVBoxLayout(review_group)
        self.review_label = QLabel()
        self.review_label.setObjectName("hardwareConnectionsReview")
        self.review_label.setWordWrap(True)
        self.review_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        review_layout.addWidget(self.review_label)
        layout.addWidget(review_group)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save assignments")
        self.save_button.setAccessibleName("Save radio hardware assignments")
        self.save_button.setStyleSheet(button_style("primary", theme))
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.remove_button = QPushButton("Remove assignments…")
        self.remove_button.setAccessibleName(f"Review removal of {self.radio_name} hardware assignments")
        self.remove_button.setStyleSheet(button_style("danger", theme))
        self.remove_button.clicked.connect(self._remove_assignments)
        self.remove_button.setVisible(self.saved_bundle is not None)
        self.remove_button.setEnabled(self.saved_bundle is not None)
        layout.addWidget(self.remove_button)

    def _unassigned_label(self, role: str) -> str:
        if role == "ptt":
            return "No separate CommsPlan PTT hardware assignment"
        if role == "cat" and self.is_receive_only:
            return "No receiver control"
        if role == "cat" and str(self.radio_profile.get("control_backend", "")).strip().casefold() == "flrig":
            return "FLRig uses its saved CAT connection — current"
        if role == "cat":
            return "Radio application manages CAT — current"
        if role == "audio_rx":
            return "Each radio application uses its saved input device — current"
        if role == "audio_tx":
            return "Each radio application uses its saved output device — current"
        return "Application manages this connection"

    @staticmethod
    def _is_virtual_audio_candidate(candidate: Mapping[str, Any]) -> bool:
        endpoint_kind = str(
            candidate.get("endpoint_kind", "")
            or (candidate.get("observed_locator", {}) or {}).get("endpoint_kind", "")
        ).strip().casefold()
        transport = str(candidate.get("transport", "") or "").strip().casefold()
        if endpoint_kind in {"monitor", "virtual"} or transport == "pulse-monitor":
            return True
        if str(candidate.get("provider", "") or "").strip().lower() not in {"pulse", "pipewire"}:
            return False
        evidence = " ".join(
            str(value or "")
            for value in (
                candidate.get("label"),
                (candidate.get("observed_locator", {}) or {}).get("node_name"),
                (candidate.get("selector", {}) or {}).get("description"),
            )
        ).casefold()
        return any(hint in evidence for hint in _VIRTUAL_AUDIO_HINTS)

    @staticmethod
    def _identity_label(binding: Mapping[str, Any]) -> str:
        stability = str(
            binding.get("stability_class", "")
            or (binding.get("selector", {}) or {}).get("stability_class", "")
        ).lower()
        if stability == "port_bound":
            return "Port-bound identity"
        if stability == "unstable_confirmed":
            return "Manual current locator"
        return "Stable hardware identity"

    @staticmethod
    def _serial_role_label(binding: Mapping[str, Any], role: str) -> str:
        selector = binding.get("selector", {}) or {}
        function = str(selector.get("function", "") or "").strip().casefold()
        # These descriptors provide recommendations, not universal proof of a
        # radio role. Operators retain both choices.
        if "enhanced" in function:
            return (
                "Enhanced serial — recommended CAT control"
                if role == "cat"
                else "Enhanced serial — alternate PTT/secondary function"
            )
        if "standard" in function:
            return (
                "Standard serial — recommended secondary PTT/CW"
                if role == "ptt"
                else "Standard serial — alternate CAT control"
            )
        label = str(binding.get("label", "") or "USB serial").strip()
        return f"{label} — CAT/PTT role not confirmed" if role in {"cat", "ptt"} else label

    def _candidate_display(self, role: str, candidate: Mapping[str, Any]) -> str:
        label = str(candidate.get("label", "") or "Connected device").strip()
        if str(candidate.get("transport", "") or "").strip().lower() == "usb-serial":
            label = self._serial_role_label(candidate, role)
        elif role == "audio_rx" and self._is_virtual_audio_candidate(candidate):
            label = (
                "Monitor / virtual source — receive-only monitoring, "
                f"not a physical radio input — {label}"
            )
        elif role == "audio_rx":
            label = f"Physical receiver input — {label}"
        locator = dict(candidate.get("observed_locator", {}) or {})
        current = str(
            locator.get("resolved_path", "")
            or locator.get("path", "")
            or locator.get("node_name", "")
            or locator.get("value", "")
        ).strip()
        identity = self._identity_label(candidate)
        return f"{label} · {identity}" + (f" · current: {current}" if current else "")

    def _reset_combo(self, role: str, *, preserve_key: str = "") -> None:
        combo = self._role_combos[role]
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self._unassigned_label(role), {"kind": "unassigned"})
        saved = self._saved_binding(role)
        if saved is not None:
            label = str(saved.get("label", "") or "Saved assignment").strip() or "Saved assignment"
            combo.addItem(f"{label} · saved", {"kind": "binding", "binding": saved})
        if self._inventory is not None:
            for candidate in self._inventory.candidates:
                capabilities = tuple(candidate.get("capabilities", (candidate.get("role"),)))
                if role not in capabilities:
                    continue
                if (
                    role == "audio_rx"
                    and self._is_virtual_audio_candidate(candidate)
                    and not self.is_receive_only
                ):
                    continue
                display = self._candidate_display(role, candidate)
                combo.addItem(display, {"kind": "binding", "binding": dict(candidate)})
        combo.addItem("Manual current locator…", {"kind": "manual"})
        selected_index = 0
        for index in range(combo.count()):
            data = combo.itemData(index)
            if not isinstance(data, dict):
                continue
            binding = data.get("binding", {})
            key = str(binding.get("selector_fingerprint", "") or "") if isinstance(binding, Mapping) else ""
            if preserve_key and key == preserve_key:
                selected_index = index
                break
            if not preserve_key and saved is not None and binding is saved:
                selected_index = index
        combo.setCurrentIndex(selected_index)
        combo.blockSignals(False)
        self._choice_changed(role)

    def _saved_binding(self, role: str) -> dict[str, Any] | None:
        for binding in (self.saved_bundle or {}).get("bindings", []):
            if str(binding.get("role", "") or "") == role and bool(binding.get("active", True)):
                return dict(binding)
        return None

    def _load_saved_choices(self) -> None:
        for role in self._role_combos:
            self._reset_combo(role)
        if self.saved_bundle is None:
            if str(self.radio_profile.get("control_backend", "")).strip().casefold() == "flrig":
                self.status_label.setText("Current setup: FLRig and the radio applications manage their own connections. Scan if you want CommsPlan to remember supported hardware assignments. Nothing changes until you save.")
            else:
                self.status_label.setText("Current setup: Radio applications manage their own connections. Scan if you want CommsPlan to remember supported hardware assignments. Nothing changes until you save.")
        else:
            self.status_label.setText("Saved hardware assignments · this screen has not checked current device availability")

    def _choice_changed(self, role: str) -> None:
        combo = self._role_combos[role]
        data = combo.currentData()
        manual = isinstance(data, dict) and data.get("kind") == "manual"
        widget = combo.property("manual_widget")
        if isinstance(widget, QWidget):
            widget.setVisible(manual)
        self._refresh_review()

    def _start_inventory_refresh(self) -> None:
        if self._inventory_thread is not None and self._inventory_thread.isRunning():
            return
        self.refresh_button.setEnabled(False)
        if self._inventory is not None:
            self.refresh_button.setText("Scan again")
        self.status_label.setText("Looking for connected serial and audio devices…")
        thread = QThread(self)
        worker = _InventoryWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._inventory_finished)
        worker.failed.connect(self._inventory_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._inventory_released)
        self._inventory_thread = thread
        self._inventory_worker = worker
        thread.start()

    @Slot(object)
    def _inventory_finished(self, snapshot: object) -> None:
        if not isinstance(snapshot, HardwareInventorySnapshot):
            self._inventory_failed("Hardware discovery returned an invalid result.")
            return
        preserve = {
            role: str(
                ((self._role_combos[role].currentData() or {}).get("binding", {}) or {}).get(
                    "selector_fingerprint", ""
                )
            )
            for role in self._role_combos
        }
        self._inventory = snapshot
        for role in self._role_combos:
            self._reset_combo(role, preserve_key=preserve[role])
        count = len(snapshot.candidates)
        detail = f"Found {count} connection candidate{'s' if count != 1 else ''}."
        if self.saved_bundle is not None:
            reconciled = reconcile_radio_hardware(
                self.saved_bundle,
                snapshot,
                expected_bundle_generation=int(self.saved_bundle.get("generation", 0) or 0),
            )
            detail += f" {reconciled.reason}"
        if snapshot.warnings:
            detail += " " + " ".join(snapshot.warnings)
        self.status_label.setText(detail)

    @Slot(str)
    def _inventory_failed(self, detail: str) -> None:
        self.status_label.setText(
            f"Connected-device refresh could not finish safely: {detail}. Saved application-managed connections were not changed."
        )

    @Slot()
    def _inventory_released(self) -> None:
        thread = self._inventory_thread
        self._inventory_thread = None
        self._inventory_worker = None
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Scan again" if self._inventory is not None else "Scan connected devices")
        if thread is not None:
            thread.deleteLater()
        if self._closing_requested:
            super().reject()

    def _manual_binding(self, role: str) -> dict[str, Any]:
        locator = self._manual_edits[role].text().strip()
        if not locator:
            raise HardwareBindingError(f"Enter the exact {role.replace('_', ' ')} locator or choose another option.")
        platform = "windows" if sys.platform.startswith("win") else "macos" if sys.platform == "darwin" else "linux"
        selector: dict[str, Any]
        stable_linux_serial = (
            platform == "linux"
            and role in {"cat", "ptt"}
            and locator.startswith("/dev/serial/by-id/")
        )
        if stable_linux_serial:
            selector = {"by_id": locator, "stability_class": "stable"}
        else:
            if not self._manual_confirmations[role].isChecked():
                raise HardwareBindingError(
                    f"Confirm that the manual {role.replace('_', ' ')} locator may need review after reconnect."
                )
            selector = {
                "unstable_locator": locator,
                "operator_confirmed": True,
                "stability_class": "unstable_confirmed",
            }
        return normalize_hardware_binding(
            {
                "role": role,
                "platform": platform,
                # A manually pasted Linux by-id path is still the same udev
                # identity emitted by discovery. Preserve that provider and
                # transport so a later inventory refresh can reconcile it.
                "provider": "udev" if stable_linux_serial else "manual",
                "transport": (
                    "usb-serial"
                    if stable_linux_serial
                    else "audio"
                    if role.startswith("audio_")
                    else "serial"
                ),
                "label": f"Manual {role.replace('_', ' ')} connection",
                "selector": selector,
                "observed_locator": {"value": locator},
            }
        )

    def _proposed_bindings(self) -> list[dict[str, Any]]:
        proposed: list[dict[str, Any]] = []
        for role, combo in self._role_combos.items():
            data = combo.currentData()
            if not isinstance(data, dict) or data.get("kind") == "unassigned":
                continue
            if data.get("kind") == "manual":
                proposed.append(self._manual_binding(role))
                continue
            binding = dict(data.get("binding", {}) or {})
            binding["role"] = role
            # Claims are derived from normalized identity at the transaction
            # boundary; never preserve a stale or caller-supplied claim.
            binding.pop("claim_key", None)
            proposed.append(normalize_hardware_binding(binding))
        return proposed

    def _refresh_review(self) -> None:
        lines: list[str] = []
        valid = True
        proposed = []
        try:
            proposed = self._proposed_bindings()
        except (HardwareBindingError, KeyError, ValueError):
            valid = False
        unchanged = self._draft_matches_saved(proposed) if valid else False
        self.review_group.setTitle("Current behavior" if unchanged else "After saving")
        if self.is_receive_only:
            lines.append("Profile: Observer / SDR — receive-only.")
        for role, title, _detail in self._role_presentation:
            combo = self._role_combos.get(role)
            if combo is None:
                continue
            data = combo.currentData()
            label = combo.currentText().strip() or self._unassigned_label(role)
            if isinstance(data, dict) and data.get("kind") in {"binding", "candidate", "manual"}:
                lines.append(f"{title}: CommsPlan records this device identity for the radio. This dialog does not apply it to applications.")
            elif role == "ptt":
                lines.append("PTT ownership: Not reviewed. Applications may still key through their own radio-control path; CommsPlan has no separate PTT hardware assignment.")
            elif role == "cat" and str(self.radio_profile.get("control_backend", "")).strip().casefold() == "flrig":
                lines.append("CAT: FLRig uses its saved radio connection.")
            elif role == "cat":
                lines.append("CAT: The radio application manages its saved connection.")
            elif role == "audio_rx":
                lines.append("Receive audio: Each application uses its saved input.")
            elif role == "audio_tx":
                lines.append("Transmit audio: Each application uses its saved output.")
            else:
                lines.append(f"{title}: {label}")
            if isinstance(data, dict) and data.get("kind") == "manual":
                locator = self._manual_edits[role].text().strip()
                stable_linux_serial = sys.platform.startswith("linux") and locator.startswith("/dev/serial/by-id/")
                if not locator or (not stable_linux_serial and not self._manual_confirmations[role].isChecked()):
                    valid = False
        lines.append("Saving records device identity only. It does not restart applications or change live routing.")
        if self._saved_active_bindings() and not proposed:
            lines.append("All assignments are cleared. Use Remove assignments… to review and retire the saved bundle.")
        self.review_label.setText("\n".join(lines))
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(valid and not unchanged and bool(proposed))
        if hasattr(self, "remove_button"):
            self.remove_button.setEnabled(bool(self._saved_active_bindings()))

    def _saved_active_bindings(self) -> list[dict[str, Any]]:
        return [dict(binding) for binding in (self.saved_bundle or {}).get("bindings", []) if bool(binding.get("active", True))]

    def _draft_matches_saved(self, proposed: list[dict[str, Any]]) -> bool:
        saved = self._saved_active_bindings()
        saved_keys = {(str(row.get("role", "")), str(row.get("selector_fingerprint", ""))) for row in saved}
        proposed_keys = {(str(row.get("role", "")), str(row.get("selector_fingerprint", ""))) for row in proposed}
        return saved_keys == proposed_keys

    @Slot()
    def _remove_assignments(self) -> None:
        if self.saved_bundle is None or not self._saved_active_bindings():
            return
        answer = QMessageBox.question(
            self,
            "Remove radio assignments?",
            f"Remove the saved hardware assignments for {self.radio_name}? Applications keep their own connection settings. CommsPlan retains recovery evidence, and the radio returns to application-managed connections.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.saved_bundle = self.store.retire_radio_hardware_bundle(
                self.radio_id,
                expected_generation=int(self.saved_bundle.get("generation", 0) or 0),
            )
        except (HardwareBindingError, KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Assignments need attention", str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            QMessageBox.critical(self, "Assignments were not removed", str(exc) or exc.__class__.__name__)
            return
        self.accept()

    @Slot()
    def _save(self) -> None:
        try:
            proposed = self._proposed_bindings()
            if self.is_receive_only and any(
                binding["role"] in {"ptt", "audio_tx"} for binding in proposed
            ):
                raise HardwareBindingError("Observer / SDR profiles cannot save PTT or transmit-audio connections.")
            if proposed:
                saved = self.store.save_radio_hardware_bundle(
                    self.radio_id,
                    proposed,
                    expected_generation=self._bundle_generation or None,
                    source="operator_confirmed",
                )
                self.saved_bundle = saved
                self._bundle_generation = int(saved.get("generation", 0) or 0)
            else:
                raise HardwareBindingError("No assignments to save. Use the reviewed removal action to retire a saved bundle.")
        except (HardwareBindingError, KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Connections need attention", str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            QMessageBox.critical(self, "Connections were not saved", str(exc) or exc.__class__.__name__)
            return
        self.accept()

    def reject(self) -> None:
        if self._inventory_thread is not None and self._inventory_thread.isRunning():
            self._closing_requested = True
            self.refresh_button.setEnabled(False)
            self.buttons.setEnabled(False)
            self.status_label.setText("Finishing the bounded device refresh before closing…")
            return
        super().reject()
