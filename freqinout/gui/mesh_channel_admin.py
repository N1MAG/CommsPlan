"""Reusable, protocol-neutral mesh channel administration widget.

The widget deliberately emits intent signals.  Adapters and persistence remain
owned by the integrating window/controller.
"""
from __future__ import annotations

import re
from typing import Mapping, Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from freqinout.core.mesh import MeshChannel, MeshChannelCapabilities, MeshChannelPolicy


class MeshChannelAdminWidget(QGroupBox):
    """Compact channel list plus details/actions for one mesh adapter."""

    refresh_requested = Signal(str)
    cancel_requested = Signal(str, str)
    configure_requested = Signal(str, str, dict)
    policy_update_requested = Signal(str, str, dict)
    remove_from_device_requested = Signal(str, str)
    remove_from_fio_requested = Signal(str, str)
    # Emitted before remove_from_device_requested so a host can show its own
    # confirmation UI.  This component never calls a device API.
    remove_from_device_confirmation_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Mesh channels", parent)
        self.setObjectName("meshChannelAdmin")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._adapter_id = ""
        self._channels: list[MeshChannel] = []
        self._device_channel_ids: set[str] = set()
        self._policies: dict[str, MeshChannelPolicy] = {}
        self._capabilities = MeshChannelCapabilities()
        self._sync_state = "Not synchronized"
        self._sync_summary = ""
        self._sync_active = False

        root = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.adapter_label = QLabel("No adapter selected")
        self.sync_label = QLabel(self._sync_state)
        self.refresh_button = QPushButton("Refresh device channels")
        self.refresh_button.setToolTip(
            "Request the channels currently configured on this device. "
            "This does not change CommsPlan policy."
        )
        self.refresh_button.clicked.connect(self._refresh)
        toolbar.addWidget(self.adapter_label)
        toolbar.addStretch(1)
        toolbar.addWidget(self.sync_label)
        toolbar.addWidget(self.refresh_button)
        root.addLayout(toolbar)

        splitter = QSplitter()
        self.channel_list = QListWidget()
        self.channel_list.setMinimumWidth(150)
        self.channel_list.currentRowChanged.connect(self._show_row)
        splitter.addWidget(self.channel_list)

        details = QWidget()
        detail_layout = QVBoxLayout(details)
        self.detail_title = QLabel("Select a channel")
        self.detail_title.setStyleSheet("font-weight: 700;")
        detail_layout.addWidget(self.detail_title)
        self.state_group = QGroupBox("Device state")
        state_form = QFormLayout(self.state_group)
        self.index_label = QLabel("—")
        self.privacy_label = QLabel("—")
        self.membership_label = QLabel("—")
        self.last_seen_label = QLabel("—")
        for label, widget in (
            ("Channel / index", self.index_label),
            ("Privacy / key", self.privacy_label),
            ("Membership", self.membership_label),
            ("Last seen", self.last_seen_label),
        ):
            state_form.addRow(label, widget)
        detail_layout.addWidget(self.state_group)

        self.policy_group = QGroupBox("CommsPlan policy")
        policy_form = QFormLayout(self.policy_group)
        self.review_label = QLabel("—")
        self.category_combo = QComboBox()
        self.category_combo.addItems(["auto", "social", "ignore"])
        self.retention_combo = QComboBox()
        self.retention_combo.addItems(["24h", "7d", "30d", "keep pinned", "none"])
        self.updated_label = QLabel("—")
        self.groups_edit = QLineEdit()
        self.groups_edit.setPlaceholderText("Optional operating groups, comma separated")
        surfaces = QWidget()
        surfaces_layout = QHBoxLayout(surfaces)
        surfaces_layout.setContentsMargins(0, 0, 0, 0)
        self.inbox_check = QCheckBox("Inbox")
        self.ops_check = QCheckBox("Ops")
        self.map_check = QCheckBox("Map")
        self.topics_check = QCheckBox("Topics")
        for check in (self.inbox_check, self.ops_check, self.map_check, self.topics_check):
            surfaces_layout.addWidget(check)
        surfaces_layout.addStretch(1)
        policy_form.addRow("Review", self.review_label)
        policy_form.addRow("Category", self.category_combo)
        policy_form.addRow("Retention", self.retention_combo)
        policy_form.addRow("Groups", self.groups_edit)
        policy_form.addRow("Use in", surfaces)
        policy_form.addRow("Updated", self.updated_label)
        detail_layout.addWidget(self.policy_group)

        edit_group = QGroupBox("Device configuration")
        edit_form = QFormLayout(edit_group)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(120)
        self.role_combo = QComboBox()
        self.role_combo.addItems(["public", "private", "direct", "telemetry", "unknown"])
        edit_form.addRow("Name", self.name_edit)
        edit_form.addRow("Role", self.role_combo)
        detail_layout.addWidget(edit_group)
        self.guidance_label = QLabel()
        self.guidance_label.setWordWrap(True)
        detail_layout.addWidget(self.guidance_label)

        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        self._actions_layout = actions
        self.accept_button = QPushButton("Accept")
        self.accept_button.setToolTip("Accept this channel for CommsPlan handling; the device is not changed.")
        self.accept_button.clicked.connect(lambda: self._set_review("accepted"))
        self.ignore_button = QPushButton("Ignore")
        self.ignore_button.setToolTip("Ignore this channel in CommsPlan; the device is not changed.")
        self.ignore_button.clicked.connect(lambda: self._set_review("ignored"))
        self.configure_button = QPushButton("Configure device")
        self.configure_button.setToolTip("Change supported channel fields on the connected device after review.")
        self.configure_button.clicked.connect(self._configure)
        self.remove_device_button = QPushButton("Remove from device")
        self.remove_device_button.setToolTip("Remove this channel from the connected device after confirmation.")
        self.remove_device_button.clicked.connect(self._remove_device)
        self.remove_fio_button = QPushButton("Remove from CommsPlan")
        self.remove_fio_button.setToolTip("Archive CommsPlan policy for this channel; the connected device is unchanged.")
        self.remove_fio_button.clicked.connect(self._remove_fio)
        self._action_buttons = (
            self.accept_button,
            self.ignore_button,
            self.configure_button,
            self.remove_device_button,
            self.remove_fio_button,
        )
        for index, button in enumerate(self._action_buttons):
            actions.addWidget(button, index // 2, index % 2)
        detail_layout.addLayout(actions)
        detail_layout.addStretch(1)
        splitter.addWidget(details)
        splitter.setSizes([220, 500])
        root.addWidget(splitter)
        self._update_actions()

    def set_adapter_context(self, adapter_id: str, *, display_name: str = "") -> None:
        self._adapter_id = str(adapter_id or "")
        self.adapter_label.setText(str(display_name or "").strip() or self._adapter_id or "No device selected")
        self.adapter_label.setToolTip(f"Internal adapter: {self._adapter_id}" if self._adapter_id else "")
        self._update_actions()

    def set_policies(self, policies: Sequence[MeshChannelPolicy] | Mapping[str, MeshChannelPolicy]) -> None:
        values = policies.values() if isinstance(policies, Mapping) else policies
        self._policies = {str(p.channel_id): p for p in values if isinstance(p, MeshChannelPolicy)}
        self._rebuild_list()

    def set_channels(self, channels: Sequence[MeshChannel]) -> None:
        self._channels = [c for c in channels if isinstance(c, MeshChannel)]
        self._rebuild_list()

    def set_device_channel_ids(self, channel_ids: Sequence[str] | set[str] | None) -> None:
        """Identify channels currently reported by the device.

        The channel model is intentionally protocol-neutral and does not carry
        provenance.  Hosts can provide that provenance separately so staged
        CommsPlan-only channels never bury channels that are actually on the device.
        """

        self._device_channel_ids = {str(value) for value in (channel_ids or ()) if str(value)}
        self._rebuild_list()

    def set_capabilities(self, capabilities: MeshChannelCapabilities | None) -> None:
        self._capabilities = capabilities if isinstance(capabilities, MeshChannelCapabilities) else MeshChannelCapabilities()
        self.guidance_label.setText(self._capabilities.guidance if not (self._capabilities.can_configure and self._capabilities.can_remove_from_device) else "Device actions are available for this adapter.")
        self._update_actions()

    def set_sync_state(self, state: object) -> None:
        self._sync_state = str(state or "Not synchronized")
        self.sync_label.setText(self._sync_state)
        self._sync_active = self._sync_state.strip().lower().startswith(
            ("syncing", "refreshing", "queued", "cancelling")
        )
        self.refresh_button.setText("Cancel" if self._sync_active else "Refresh device channels")
        self.refresh_button.setEnabled(not self._sync_state.strip().lower().startswith("cancelling"))

    def set_sync_summary(self, summary: object) -> None:
        """Update policy totals without replacing live refresh/cancel feedback."""

        self._sync_summary = str(summary or "")
        if not self._sync_active:
            self.set_sync_state(self._sync_summary or "Not synchronized")

    def resizeEvent(self, event: QEvent) -> None:
        """Keep details usable in a narrow tab and with enlarged fonts."""
        super().resizeEvent(event)
        splitter = self.findChild(QSplitter)
        if splitter is not None:
            splitter.setOrientation(Qt.Vertical if self.width() < 760 else Qt.Horizontal)
        # Two columns at normal widths; a single column avoids cramped labels.
        for index, button in enumerate(self._action_buttons):
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._actions_layout.removeWidget(button)
            columns = 1 if self.width() < 560 else 2
            self._actions_layout.addWidget(button, index // columns, index % columns)

    def _rebuild_list(self) -> None:
        current_item = self.channel_list.currentItem()
        current_id = str(current_item.data(32) or "") if current_item is not None else ""
        self.channel_list.clear()
        channels = sorted(self._channels, key=self._channel_sort_key)
        self._channels = channels
        for channel in channels:
            policy = self._policies.get(channel.channel_id or str(channel.index))
            name = channel.name or (policy.display_name if policy else "Channel")
            item = QListWidgetItem(f"{name}  ·  {channel.index}")
            item.setData(32, channel.channel_id or str(channel.index))
            self.channel_list.addItem(item)
        if self.channel_list.count():
            selected_row = next(
                (
                    row
                    for row in range(self.channel_list.count())
                    if str(self.channel_list.item(row).data(32) or "") == current_id
                ),
                0,
            )
            self.channel_list.setCurrentRow(selected_row)
        else:
            self._show_row(-1)

    @staticmethod
    def _natural_text_key(value: object) -> tuple[tuple[int, object], ...]:
        """Sort labels as users expect (Channel 2 before Channel 10)."""

        parts = re.split(r"(\d+)", str(value or "").strip().casefold())
        return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts if part)

    def _channel_sort_key(self, channel: MeshChannel) -> tuple[object, ...]:
        channel_id = channel.channel_id or str(channel.index)
        device_rank = 0 if channel_id in self._device_channel_ids else 1
        # Device channels have meaningful indexes.  Staged channels usually do
        # not, so their stable natural label/id order is used instead.
        index = int(channel.index) if isinstance(channel.index, int) else -1
        index_key = (0, index) if index >= 0 else (1, 0)
        return (device_rank, index_key, self._natural_text_key(channel.name), self._natural_text_key(channel_id))

    def _selected(self) -> tuple[MeshChannel | None, MeshChannelPolicy | None]:
        row = self.channel_list.currentRow()
        if row < 0 or row >= len(self._channels):
            return None, None
        channel = self._channels[row]
        return channel, self._policies.get(channel.channel_id or str(channel.index))

    def _show_row(self, row: int) -> None:
        channel, policy = self._selected()
        if channel is None:
            self.detail_title.setText("Select a channel")
            self._update_actions()
            return
        channel_id = channel.channel_id or str(channel.index)
        self.detail_title.setText(channel.name or f"Channel {channel_id}")
        self.index_label.setText(f"{channel_id} / {channel.index}")
        self.privacy_label.setText(f"{channel.privacy or 'unknown'} · {policy.key_display_text if policy else 'Key status unavailable'}")
        if policy is None:
            membership = "Unknown"
        elif policy.requires_key:
            membership = "Joined" if policy.key_available else "Key needed"
        else:
            membership = "Available on device"
        self.membership_label.setText(membership)
        self.last_seen_label.setText("Device snapshot")
        self.review_label.setText(policy.review_state if policy else "pending")
        self.category_combo.setCurrentText(policy.default_category if policy else "auto")
        self.retention_combo.setCurrentText(policy.retention_window if policy else "7d")
        self.groups_edit.setText(", ".join(policy.mapped_groups) if policy else "")
        self.inbox_check.setChecked(policy.inbox_enabled if policy else True)
        self.ops_check.setChecked(policy.ops_enabled if policy else True)
        self.map_check.setChecked(policy.map_enabled if policy else True)
        self.topics_check.setChecked(policy.topic_scan_enabled if policy else True)
        self.updated_label.setText(policy.updated_utc if policy and policy.updated_utc else "Not recorded")
        self.name_edit.setText(channel.name)
        self.role_combo.setCurrentText(channel.role or "unknown")
        self._update_actions()

    def _update_actions(self) -> None:
        selected = self._selected()[0] is not None
        self.configure_button.setEnabled(selected and self._capabilities.can_configure)
        self.remove_device_button.setEnabled(selected and self._capabilities.can_remove_from_device)
        self.remove_fio_button.setEnabled(selected)
        self.accept_button.setEnabled(selected)
        self.ignore_button.setEnabled(selected)

    def _refresh(self) -> None:
        if not self._adapter_id:
            return
        if self._sync_active:
            self.set_sync_state("Cancelling channel refresh…")
            self.cancel_requested.emit(self._adapter_id, "channel-sync")
        else:
            # Give the operator an immediate cancel affordance even if the
            # worker's first lifecycle update is delayed by a slow BLE device.
            self.set_sync_state("Refreshing device channels…")
            self.refresh_requested.emit(self._adapter_id)

    def _configure(self) -> None:
        channel, _ = self._selected()
        if channel and self._capabilities.can_configure:
            self.configure_requested.emit(
                self._adapter_id,
                channel.channel_id or str(channel.index),
                {"name": self.name_edit.text().strip(), "role": self.role_combo.currentText()},
            )

    def _remove_device(self) -> None:
        channel, _ = self._selected()
        if channel and self._capabilities.can_remove_from_device:
            ident = channel.channel_id or str(channel.index)
            self.remove_from_device_confirmation_requested.emit(self._adapter_id, ident)

    def confirm_remove_from_device(self, adapter_id: str | None = None, channel_id: str | None = None) -> None:
        """Emit the destructive request after the host has obtained approval."""
        ident_adapter = self._adapter_id if adapter_id is None else str(adapter_id)
        channel, _ = self._selected()
        ident_channel = (channel.channel_id or str(channel.index)) if channel_id is None and channel else channel_id
        if ident_adapter and ident_channel is not None and self._capabilities.can_remove_from_device:
            self.remove_from_device_requested.emit(ident_adapter, str(ident_channel))

    def _remove_fio(self) -> None:
        channel, _ = self._selected()
        if channel:
            self.remove_from_fio_requested.emit(
                self._adapter_id,
                channel.channel_id or str(channel.index),
            )

    def _set_review(self, state: str) -> None:
        channel, policy = self._selected()
        if channel and policy:
            groups = tuple(
                value.strip().upper()
                for value in self.groups_edit.text().split(",")
                if value.strip()
            )
            self.policy_update_requested.emit(
                self._adapter_id,
                channel.channel_id or str(channel.index),
                {
                    "review_state": state,
                    "default_category": self.category_combo.currentText(),
                    "retention_window": self.retention_combo.currentText(),
                    "mapped_groups": groups,
                    "inbox_enabled": self.inbox_check.isChecked(),
                    "ops_enabled": self.ops_check.isChecked(),
                    "map_enabled": self.map_check.isChecked(),
                    "topic_scan_enabled": self.topics_check.isChecked(),
                },
            )


# Short alias for hosts that prefer the component name without the suffix.
MeshChannelAdmin = MeshChannelAdminWidget
