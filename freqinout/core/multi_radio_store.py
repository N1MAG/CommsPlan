from __future__ import annotations

import datetime
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from freqinout.core.config_paths import get_config_dir
from freqinout.core.js8_defaults import coerce_js8_offset_hz
from freqinout.core.guided_varac_configuration import normalize_varac_path
from freqinout.core.js8_storage import (
    STORAGE_MODES,
    canonicalize_storage_path,
    normalize_rig_name,
    normalize_variant_family,
)
from freqinout.core.logger import log
from freqinout.core.receiver_control import receiver_control_verification_matches
from freqinout.core.radio_hardware_binding import (
    HARDWARE_BINDING_SCHEMA_VERSION,
    HARDWARE_ROLES,
    HardwareBindingError,
    match_hardware_selector,
    normalize_hardware_binding,
)
from freqinout.core.receiver_software_stack import RECEIVE_ONLY_EXECUTION_SCOPE
from freqinout.core.software_instance_manifest import (
    find_manifest_conflicts,
    manifest_from_mapping,
    manifest_to_record,
)
from freqinout.core.sqlite_utils import connect_sqlite_readonly
from freqinout.core.multi_rig_guardrails import (
    collect_multi_rig_guardrail_warnings,
    format_multi_rig_guardrail_warnings,
)


DEFAULT_DEVICE_SYSTEM_KEY = "default_device"
DEFAULT_DEVICE_NAME = "Default Radio"
DEFAULT_OPERATING_SYSTEM_KEY = "default_operating"
DEFAULT_OPERATING_NAME = "Standard transceiver operations"
DEFAULT_RECEIVE_ONLY_OPERATING_SYSTEM_KEY = "receive_only_sdr"
DEFAULT_RECEIVE_ONLY_OPERATING_NAME = "Receive-only monitoring"
DEFAULT_FREQUENCY_PLAN_SYSTEM_KEY = "default_frequency_plan"
DEFAULT_FREQUENCY_PLAN_NAME = "Default Frequency Plan"
DEFAULT_JS8_INSTANCE_SYSTEM_KEY = "default_js8_instance"
DEFAULT_JS8_INSTANCE_NAME = "Primary JS8"
DEFAULT_FAST_LIGHT_SYSTEM_KEY = "default_fast_light"
DEFAULT_FAST_LIGHT_NAME = "Primary Fast Light"
DEFAULT_VARAC_NODE_SYSTEM_KEY = "default_varac_node"
DEFAULT_VARAC_NODE_NAME = "Primary VarAC"
MULTI_RIG_MIGRATION_VERSION_KEY = "multi_rig_migration_version"
MULTI_RIG_MIGRATION_DEFERRED_KEY = "multi_rig_migration_deferred"
MULTI_RIG_MIGRATION_COMPLETED_AT_KEY = "multi_rig_migration_completed_at_utc"
MULTI_RIG_MIGRATION_SUMMARY_PREFIX = "multi_rig_migration_summary_v"
CURRENT_MULTI_RIG_MIGRATION_VERSION = 3
RADIO_HARDWARE_SCHEMA_VERSION_KEY = "radio_hardware_schema_version"

SUPPORTED_DEVICE_CONTROL_BACKENDS = frozenset({"flrig", "js8call", "manual", "rigctld"})
# Keep the runtime compatibility projection aligned with 1.2.2.
SUPPORTED_RUNTIME_CONTROL_BACKENDS = frozenset({"flrig", "js8call", "manual", "rigctld"})
SUPPORTED_SOFTWARE_ROLES = frozenset({"js8call", "fast_light", "varac", "flamp", "flmsg", "js8spotter", "commstat"})
SUPPORTED_DEVICE_CLASSES = frozenset({"tx_rx", "observer", "gateway"})
SUPPORTED_RECEIVER_ADAPTERS = frozenset({"manual", "sdrpp_rigctl"})
SUPPORTED_RECEIVER_VERIFICATION_STATES = frozenset({"manual", "unverified", "verified", "failed"})
SUPPORTED_DEPLOYMENT_MODES = frozenset({"full", "minimal"})
SUPPORTED_ASSIGNMENT_STATES = frozenset({"active", "temporary_override", "scheduled", "inactive", "superseded"})
EFFECTIVE_ASSIGNMENT_STATES = frozenset({"active", "temporary_override"})
SUPPORTED_SCHEDULER_MODES = frozenset({"full", "simple"})
SUPPORTED_FREQUENCY_PLAN_CATEGORIES = frozenset(
    {
        "normal",
        "event",
        "portable",
        "exercise",
        "emergency",
        "ad_hoc",
        "rx_watch",
        "sop_schedule",
        "hf_daily_schedule",
        "hf_net_schedule",
    }
)
SOURCE_ONLY_FREQUENCY_PLAN_CATEGORIES = frozenset({"hf_daily_schedule", "hf_net_schedule"})
SUPPORTED_FREQUENCY_PLAN_STATUSES = frozenset({"draft", "saved", "archived"})
DEFAULT_TIMER_ENFORCEMENT_MODE = "On Schedule Change"
DEFAULT_TIMER_PROMPT_INTERVAL = "Hourly"
DEFAULT_HOLD_DURATION_MINUTES = 30
SUPPORTED_HOLD_DURATION_MINUTES = frozenset({30, 60, 90, 120})
SHARED_PTT_POLICY_TYPE = "shared_ptt"
SHARED_PTT_POLICY_PRIORITY = 20
RF_CONFLICT_POLICY_TYPE = "rf_conflict"
RF_CONFLICT_POLICY_PRIORITY = 30
SDR_FOLLOW_POLICY_TYPE = "sdr_follow"
SDR_FOLLOW_POLICY_PRIORITY = 60
GATEWAY_EXCLUSIVE_POLICY_TYPE = "gateway_exclusive"
GATEWAY_EXCLUSIVE_POLICY_PRIORITY = 70
PROFILE_SWAP_POLICY_TYPE = "profile_swap"
PROFILE_SWAP_POLICY_PRIORITY = 40
SUPPORTED_PROFILE_SWAP_MODES = frozenset({"use_target_profile", "carry_primary_profile"})
SUPPORTED_VARAC_NATIVE_MANAGEMENT_STATES = frozenset(
    {"operator", "prepared", "managed", "recovery_required"}
)
SUPPORTED_VARAC_NATIVE_JOURNAL_STATES = frozenset(
    {
        "pending",
        "backup_ready",
        "promoting",
        "external_applied",
        "fio_committed",
        "complete",
        "rolled_back",
        "recovery_required",
    }
)

MIRRORED_LEGACY_KEYS = frozenset(
    {
        "control_via",
        "rig_host",
        "rig_port",
        "flrig_host",
        "flrig_port",
        "fldigi_host",
        "fldigi_port",
        "fldigi_log_path",
        "fldigi_checkin_dir",
        "js8_host",
        "js8_port",
        "js8_offset_hz",
        "js8_profile_path",
        "js8_directed_path",
        "js8_forms_path",
        "path_flrig",
        "path_fldigi",
        "path_flmsg",
        "path_flamp",
        "path_js8call",
        "path_js8spotter",
        "path_commstat",
        "varac_path",
        "varac_db_path",
        "varac_ini_path",
        "varac_launch_cmd",
        "varac_incoming_path",
        "varac_outbox_dir",
        "varac_bbs_dir",
        "varac_bbs_archive_dir",
        "varac_bbs_enabled",
        "varac_bbs_limit_access_enabled",
        "varac_bbs_allowed_callsigns",
        "varac_bbs_allowed_group_sources",
        "varac_guard_access_policy_v1",
        "varac_guard_allow_bbs_allowed_callsigns",
        "varac_guard_allow_operator_trusted",
        "varac_bbs_announce_enabled",
        "varac_bbs_vault_enabled",
        "varac_bbs_vault_managed_root",
        "varac_bbs_vault_default_location_id",
        "varac_bbs_vault_global_code_policy",
        "varac_bbs_vault_trigger_mode",
        "varac_bbs_vault_return_mode",
        "varac_bbs_vault_failed_attempt_limit",
        "varac_bbs_vault_failed_attempt_window_seconds",
        "varac_bbs_vault_cooldown_seconds",
        "varac_bbs_vault_idle_timeout_seconds",
        "varac_bbs_vault_flamp_enabled",
        "varac_bbs_vault_flamp_relay_dir",
        "varac_bbs_vault_flamp_listing_max_age_days",
        "varac_bbs_vault_locations_v1",
        "varac_bbs_sweeper_rules_v1",
        "varac_bbs_vault_runtime_state_v1",
        "varac_bbs_vault_last_summary",
        "message_paths",
        "gpg_verify_flamp_k2s_enabled",
        "hash_verify_flamp_k2s_enabled",
        "js8_msg_auth_enabled",
        "gpg_executable_path",
        "gpg_trusted_signers",
        "gpg_compose_signing_key_fingerprint",
        "trusted_file_hashes",
        "js8spotter_import_db_path",
        "launch_control_enabled",
        "use_scheduler",
        "schedule_hold_minutes_default",
        "freq_enforcement_mode",
        "freq_prompt_interval",
        "fldigi_enforcement_mode",
        "fldigi_prompt_interval",
        "js8_enforcement_mode",
        "js8_prompt_interval",
    }
)

FIRST_RUN_ONBOARDING_ACK_KEY = "first_run_onboarding_acknowledged_v1"

FIO_EXISTING_USE_IGNORED_KEYS = frozenset(
    {
        MULTI_RIG_MIGRATION_VERSION_KEY,
        MULTI_RIG_MIGRATION_DEFERRED_KEY,
        FIRST_RUN_ONBOARDING_ACK_KEY,
        "multi_rig_shared_state_schema_version",
        RADIO_HARDWARE_SCHEMA_VERSION_KEY,
        "autoquery_keys_purged_v1",
        "timezone",
    }
)

FALLBACK_RADIO_NAMES = frozenset({"", "radio", "my radio", "default radio", "device profile"})


def _needs_operator_radio_name(name: Any, explicit_value: Any = None) -> int:
    if explicit_value not in (None, ""):
        return _coerce_bool_int(explicit_value, False)
    normalized = _coerce_text(name, "").strip().lower()
    return 1 if normalized in FALLBACK_RADIO_NAMES else 0


@dataclass(frozen=True)
class MigrationResult:
    already_current: bool
    applied: bool
    deferred: bool
    from_version: int
    to_version: int
    created_device_profile_id: Optional[int] = None
    created_operating_profile_id: Optional[int] = None
    created_frequency_plan_id: Optional[int] = None
    created_js8_instance_id: Optional[int] = None
    created_fast_light_config_id: Optional[int] = None
    created_varac_node_id: Optional[int] = None
    enabled_software_roles: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

SETTINGS_TABLE_SPECS: Dict[str, Dict[str, object]] = {
    "device_profiles": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS device_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_key TEXT UNIQUE,
            name TEXT NOT NULL,
            radio_catalog_id TEXT,
            radio_manufacturer TEXT,
            radio_model TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            needs_operator_name INTEGER NOT NULL DEFAULT 0,
            runtime_active INTEGER NOT NULL DEFAULT 0,
            runtime_primary INTEGER NOT NULL DEFAULT 0,
            display_order INTEGER NOT NULL DEFAULT 0,
            device_class TEXT NOT NULL DEFAULT 'tx_rx',
            deployment_mode TEXT NOT NULL DEFAULT 'full',
            control_backend TEXT NOT NULL DEFAULT 'flrig',
            use_flrig INTEGER NOT NULL DEFAULT 0,
            use_fldigi INTEGER NOT NULL DEFAULT 0,
            use_flmsg INTEGER NOT NULL DEFAULT 0,
            use_flamp INTEGER NOT NULL DEFAULT 0,
            use_js8call INTEGER NOT NULL DEFAULT 0,
            use_js8spotter INTEGER NOT NULL DEFAULT 0,
            use_external_js8spotter INTEGER,
            use_commstat INTEGER NOT NULL DEFAULT 0,
            use_varac INTEGER NOT NULL DEFAULT 0,
            rig_host TEXT,
            rig_port INTEGER,
            flrig_host TEXT,
            flrig_port INTEGER,
            fldigi_host TEXT,
            fldigi_port INTEGER,
            fldigi_log_path TEXT,
            fldigi_checkin_dir TEXT,
            flmsg_path TEXT,
            flmsg_message_path TEXT,
            flamp_path TEXT,
            flamp_message_path TEXT,
            js8_host TEXT,
            js8_port INTEGER,
            js8_instance_id INTEGER,
            js8_profile_path TEXT,
            js8_directed_path TEXT,
            js8_forms_path TEXT,
            fast_light_config_id INTEGER,
            varac_install_path TEXT,
            varac_db_path TEXT,
            varac_ini_path TEXT,
            varac_node_id INTEGER,
            varac_outbox_dir TEXT,
            varac_bbs_dir TEXT,
            varac_bbs_archive_dir TEXT,
            varac_bbs_enabled INTEGER NOT NULL DEFAULT 0,
            varac_bbs_limit_access_enabled INTEGER NOT NULL DEFAULT 0,
            varac_bbs_allowed_callsigns TEXT,
            varac_bbs_allowed_group_sources TEXT,
            varac_guard_access_policy_v1 TEXT,
            varac_guard_allow_bbs_allowed_callsigns INTEGER,
            varac_guard_allow_operator_trusted INTEGER,
            varac_bbs_announce_enabled INTEGER NOT NULL DEFAULT 0,
            varac_bbs_auto_archive_enabled INTEGER NOT NULL DEFAULT 0,
            varac_bbs_auto_archive_days INTEGER NOT NULL DEFAULT 14,
            varac_bbs_vault_enabled INTEGER NOT NULL DEFAULT 0,
            varac_bbs_vault_managed_root TEXT,
            varac_bbs_vault_default_location_id TEXT,
            varac_bbs_vault_global_code_policy TEXT,
            varac_bbs_vault_trigger_mode TEXT,
            varac_bbs_vault_return_mode TEXT,
            varac_bbs_vault_failed_attempt_limit INTEGER NOT NULL DEFAULT 3,
            varac_bbs_vault_failed_attempt_window_seconds INTEGER NOT NULL DEFAULT 900,
            varac_bbs_vault_cooldown_seconds INTEGER NOT NULL DEFAULT 1800,
            varac_bbs_vault_idle_timeout_seconds INTEGER NOT NULL DEFAULT 600,
            varac_bbs_vault_flamp_enabled INTEGER NOT NULL DEFAULT 0,
            varac_bbs_vault_flamp_relay_dir TEXT,
            varac_bbs_vault_flamp_listing_max_age_days INTEGER NOT NULL DEFAULT 14,
            varac_bbs_vault_locations_v1 TEXT,
            varac_bbs_sweeper_rules_v1 TEXT,
            varac_bbs_vault_runtime_state_v1 TEXT,
            varac_bbs_vault_last_summary TEXT,
            varac_cluster_member_enabled INTEGER DEFAULT 0,
            scheduler_enabled INTEGER NOT NULL DEFAULT 1,
            schedule_hold_minutes_default INTEGER NOT NULL DEFAULT 30,
            freq_enforcement_mode TEXT NOT NULL DEFAULT 'On Schedule Change',
            freq_prompt_interval TEXT NOT NULL DEFAULT 'Hourly',
            fldigi_enforcement_mode TEXT NOT NULL DEFAULT 'On Schedule Change',
            fldigi_prompt_interval TEXT NOT NULL DEFAULT 'Hourly',
            js8_enforcement_mode TEXT NOT NULL DEFAULT 'On Schedule Change',
            js8_prompt_interval TEXT NOT NULL DEFAULT 'Hourly',
            launch_enabled INTEGER NOT NULL DEFAULT 0,
            launch_path TEXT,
            launch_cmd TEXT,
            ptt_group TEXT,
            antenna_group TEXT,
            frontend_group TEXT,
            amplifier_group TEXT,
            antenna_supported_bands_json TEXT NOT NULL DEFAULT '[]',
            antenna_band_guard_mode TEXT NOT NULL DEFAULT 'warn',
            band_overlap_guard_group TEXT,
            band_overlap_guard_mode TEXT NOT NULL DEFAULT 'warn',
            advanced_frequency_guard_group TEXT,
            advanced_frequency_guard_mode TEXT NOT NULL DEFAULT 'warn',
            advanced_frequency_guard_window_hz INTEGER NOT NULL DEFAULT 0,
            sdr_host TEXT,
            sdr_port INTEGER,
            sdr_application TEXT,
            sdr_adapter TEXT NOT NULL DEFAULT 'manual',
            sdr_target TEXT,
            sdr_control_enabled INTEGER NOT NULL DEFAULT 0,
            sdr_verification_state TEXT NOT NULL DEFAULT 'manual',
            sdr_verification_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "system_key": "TEXT",
            "name": "TEXT NOT NULL",
            "radio_catalog_id": "TEXT",
            "radio_manufacturer": "TEXT",
            "radio_model": "TEXT",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "needs_operator_name": "INTEGER NOT NULL DEFAULT 0",
            "runtime_active": "INTEGER NOT NULL DEFAULT 0",
            "runtime_primary": "INTEGER NOT NULL DEFAULT 0",
            "display_order": "INTEGER NOT NULL DEFAULT 0",
            "device_class": "TEXT NOT NULL DEFAULT 'tx_rx'",
            "deployment_mode": "TEXT NOT NULL DEFAULT 'full'",
            "control_backend": "TEXT NOT NULL DEFAULT 'flrig'",
            "use_flrig": "INTEGER NOT NULL DEFAULT 0",
            "use_fldigi": "INTEGER NOT NULL DEFAULT 0",
            "use_flmsg": "INTEGER NOT NULL DEFAULT 0",
            "use_flamp": "INTEGER NOT NULL DEFAULT 0",
            "use_js8call": "INTEGER NOT NULL DEFAULT 0",
            "use_js8spotter": "INTEGER NOT NULL DEFAULT 0",
            "use_external_js8spotter": "INTEGER",
            "use_commstat": "INTEGER NOT NULL DEFAULT 0",
            "use_varac": "INTEGER NOT NULL DEFAULT 0",
            "rig_host": "TEXT",
            "rig_port": "INTEGER",
            "flrig_host": "TEXT",
            "flrig_port": "INTEGER",
            "fldigi_host": "TEXT",
            "fldigi_port": "INTEGER",
            "fldigi_log_path": "TEXT",
            "fldigi_checkin_dir": "TEXT",
            "flmsg_path": "TEXT",
            "flmsg_message_path": "TEXT",
            "flamp_path": "TEXT",
            "flamp_message_path": "TEXT",
            "js8_host": "TEXT",
            "js8_port": "INTEGER",
            "js8_instance_id": "INTEGER",
            "js8_profile_path": "TEXT",
            "js8_directed_path": "TEXT",
            "js8_forms_path": "TEXT",
            "fast_light_config_id": "INTEGER",
            "varac_install_path": "TEXT",
            "varac_db_path": "TEXT",
            "varac_ini_path": "TEXT",
            "varac_node_id": "INTEGER",
            "varac_outbox_dir": "TEXT",
            "varac_bbs_dir": "TEXT",
            "varac_bbs_archive_dir": "TEXT",
            "varac_bbs_enabled": "INTEGER NOT NULL DEFAULT 0",
            "varac_bbs_limit_access_enabled": "INTEGER NOT NULL DEFAULT 0",
            "varac_bbs_allowed_callsigns": "TEXT",
            "varac_bbs_allowed_group_sources": "TEXT",
            "varac_guard_access_policy_v1": "TEXT",
            "varac_guard_allow_bbs_allowed_callsigns": "INTEGER",
            "varac_guard_allow_operator_trusted": "INTEGER",
            "varac_bbs_announce_enabled": "INTEGER NOT NULL DEFAULT 0",
            "varac_bbs_auto_archive_enabled": "INTEGER NOT NULL DEFAULT 0",
            "varac_bbs_auto_archive_days": "INTEGER NOT NULL DEFAULT 14",
            "varac_bbs_vault_enabled": "INTEGER NOT NULL DEFAULT 0",
            "varac_bbs_vault_managed_root": "TEXT",
            "varac_bbs_vault_default_location_id": "TEXT",
            "varac_bbs_vault_global_code_policy": "TEXT",
            "varac_bbs_vault_trigger_mode": "TEXT",
            "varac_bbs_vault_return_mode": "TEXT",
            "varac_bbs_vault_failed_attempt_limit": "INTEGER NOT NULL DEFAULT 3",
            "varac_bbs_vault_failed_attempt_window_seconds": "INTEGER NOT NULL DEFAULT 900",
            "varac_bbs_vault_cooldown_seconds": "INTEGER NOT NULL DEFAULT 1800",
            "varac_bbs_vault_idle_timeout_seconds": "INTEGER NOT NULL DEFAULT 600",
            "varac_bbs_vault_flamp_enabled": "INTEGER NOT NULL DEFAULT 0",
            "varac_bbs_vault_flamp_relay_dir": "TEXT",
            "varac_bbs_vault_flamp_listing_max_age_days": "INTEGER NOT NULL DEFAULT 14",
            "varac_bbs_vault_locations_v1": "TEXT",
            "varac_bbs_sweeper_rules_v1": "TEXT",
            "varac_bbs_vault_runtime_state_v1": "TEXT",
            "varac_bbs_vault_last_summary": "TEXT",
            "varac_cluster_member_enabled": "INTEGER DEFAULT 0",
            "scheduler_enabled": "INTEGER NOT NULL DEFAULT 1",
            "schedule_hold_minutes_default": "INTEGER NOT NULL DEFAULT 30",
            "freq_enforcement_mode": "TEXT NOT NULL DEFAULT 'On Schedule Change'",
            "freq_prompt_interval": "TEXT NOT NULL DEFAULT 'Hourly'",
            "fldigi_enforcement_mode": "TEXT NOT NULL DEFAULT 'On Schedule Change'",
            "fldigi_prompt_interval": "TEXT NOT NULL DEFAULT 'Hourly'",
            "js8_enforcement_mode": "TEXT NOT NULL DEFAULT 'On Schedule Change'",
            "js8_prompt_interval": "TEXT NOT NULL DEFAULT 'Hourly'",
            "launch_enabled": "INTEGER NOT NULL DEFAULT 0",
            "launch_path": "TEXT",
            "launch_cmd": "TEXT",
            "ptt_group": "TEXT",
            "antenna_group": "TEXT",
            "frontend_group": "TEXT",
            "amplifier_group": "TEXT",
            "antenna_supported_bands_json": "TEXT NOT NULL DEFAULT '[]'",
            "antenna_band_guard_mode": "TEXT NOT NULL DEFAULT 'warn'",
            "band_overlap_guard_group": "TEXT",
            "band_overlap_guard_mode": "TEXT NOT NULL DEFAULT 'warn'",
            "advanced_frequency_guard_group": "TEXT",
            "advanced_frequency_guard_mode": "TEXT NOT NULL DEFAULT 'warn'",
            "advanced_frequency_guard_window_hz": "INTEGER NOT NULL DEFAULT 0",
            "sdr_host": "TEXT",
            "sdr_port": "INTEGER",
            "sdr_application": "TEXT",
            "sdr_adapter": "TEXT NOT NULL DEFAULT 'manual'",
            "sdr_target": "TEXT",
            "sdr_control_enabled": "INTEGER NOT NULL DEFAULT 0",
            "sdr_verification_state": "TEXT NOT NULL DEFAULT 'manual'",
            "sdr_verification_json": "TEXT NOT NULL DEFAULT '{}'",
            "notes": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_profiles_system_key ON device_profiles(system_key)",
            "CREATE INDEX IF NOT EXISTS idx_device_profiles_display_order ON device_profiles(display_order)",
            "CREATE INDEX IF NOT EXISTS idx_device_profiles_runtime_active ON device_profiles(runtime_active)",
            "CREATE INDEX IF NOT EXISTS idx_device_profiles_runtime_primary ON device_profiles(runtime_primary)",
        ),
    },
    "runtime_policies": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS runtime_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            radio_profile_id INTEGER NOT NULL UNIQUE,
            scheduler_enabled INTEGER NOT NULL DEFAULT 1,
            background_ingest_enabled INTEGER NOT NULL DEFAULT 1,
            messages_enabled INTEGER NOT NULL DEFAULT 1,
            map_enabled INTEGER NOT NULL DEFAULT 1,
            launch_enabled INTEGER NOT NULL DEFAULT 0,
            net_control_enabled INTEGER NOT NULL DEFAULT 1,
            operator_suppressed INTEGER NOT NULL DEFAULT 0,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "radio_profile_id": "INTEGER NOT NULL UNIQUE",
            "scheduler_enabled": "INTEGER NOT NULL DEFAULT 1",
            "background_ingest_enabled": "INTEGER NOT NULL DEFAULT 1",
            "messages_enabled": "INTEGER NOT NULL DEFAULT 1",
            "map_enabled": "INTEGER NOT NULL DEFAULT 1",
            "launch_enabled": "INTEGER NOT NULL DEFAULT 0",
            "net_control_enabled": "INTEGER NOT NULL DEFAULT 1",
            "operator_suppressed": "INTEGER NOT NULL DEFAULT 0",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_policies_radio_profile_id ON runtime_policies(radio_profile_id)",
            "CREATE INDEX IF NOT EXISTS idx_runtime_policies_operator_suppressed ON runtime_policies(operator_suppressed)",
        ),
    },
    "scheduler_manual_control_states": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS scheduler_manual_control_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            radio_profile_id INTEGER NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'on_schedule',
            manual_target_json TEXT NOT NULL DEFAULT '{}',
            hold_until_utc TEXT,
            reason_code TEXT,
            operator_source TEXT NOT NULL DEFAULT 'scheduler',
            latest_event_id TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "radio_profile_id": "INTEGER NOT NULL UNIQUE",
            "state": "TEXT NOT NULL DEFAULT 'on_schedule'",
            "manual_target_json": "TEXT NOT NULL DEFAULT '{}'",
            "hold_until_utc": "TEXT",
            "reason_code": "TEXT",
            "operator_source": "TEXT NOT NULL DEFAULT 'scheduler'",
            "latest_event_id": "TEXT",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_manual_control_radio_profile_id ON scheduler_manual_control_states(radio_profile_id)",
            "CREATE INDEX IF NOT EXISTS idx_scheduler_manual_control_state ON scheduler_manual_control_states(state)",
        ),
    },
    "busy_evidence": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS busy_evidence (
            id TEXT PRIMARY KEY,
            radio_profile_id INTEGER NOT NULL,
            source_family TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            severity TEXT NOT NULL,
            evidence_timestamp_utc TEXT NOT NULL,
            expiration_timestamp_utc TEXT,
            description TEXT,
            latest_event_id TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "id": "TEXT PRIMARY KEY",
            "radio_profile_id": "INTEGER NOT NULL",
            "source_family": "TEXT NOT NULL DEFAULT 'unknown'",
            "reason_code": "TEXT NOT NULL DEFAULT 'unknown'",
            "severity": "TEXT NOT NULL DEFAULT 'soft'",
            "evidence_timestamp_utc": "TEXT NOT NULL DEFAULT ''",
            "expiration_timestamp_utc": "TEXT",
            "description": "TEXT",
            "latest_event_id": "TEXT",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_busy_evidence_radio_profile_id ON busy_evidence(radio_profile_id)",
            "CREATE INDEX IF NOT EXISTS idx_busy_evidence_severity ON busy_evidence(severity)",
            "CREATE INDEX IF NOT EXISTS idx_busy_evidence_expiration ON busy_evidence(expiration_timestamp_utc)",
        ),
    },
    "ptt_conflict_evidence": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS ptt_conflict_evidence (
            id TEXT PRIMARY KEY,
            ptt_group TEXT NOT NULL,
            requested_radio_id INTEGER NOT NULL,
            blocking_radio_id INTEGER,
            severity TEXT NOT NULL DEFAULT 'hard',
            source TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(requested_radio_id) REFERENCES device_profiles(id) ON DELETE CASCADE,
            FOREIGN KEY(blocking_radio_id) REFERENCES device_profiles(id) ON DELETE SET NULL
        )
        """,
        "columns": {
            "id": "TEXT PRIMARY KEY",
            "ptt_group": "TEXT NOT NULL DEFAULT ''",
            "requested_radio_id": "INTEGER NOT NULL",
            "blocking_radio_id": "INTEGER",
            "severity": "TEXT NOT NULL DEFAULT 'hard'",
            "source": "TEXT",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_ptt_conflict_group ON ptt_conflict_evidence(ptt_group)",
            "CREATE INDEX IF NOT EXISTS idx_ptt_conflict_requested_radio ON ptt_conflict_evidence(requested_radio_id)",
            "CREATE INDEX IF NOT EXISTS idx_ptt_conflict_blocking_radio ON ptt_conflict_evidence(blocking_radio_id)",
        ),
    },
    "js8_instances": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS js8_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_key TEXT UNIQUE,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            host TEXT NOT NULL DEFAULT '127.0.0.1',
            port INTEGER NOT NULL DEFAULT 2442,
            offset_hz INTEGER NOT NULL DEFAULT 0,
            profile_path TEXT,
            directed_path TEXT,
            inbox_path TEXT,
            forms_path TEXT,
            install_path TEXT,
            variant_family TEXT NOT NULL DEFAULT 'unknown',
            variant_version TEXT,
            rig_name TEXT,
            rig_name_source TEXT,
            application_data_root TEXT,
            all_path TEXT,
            save_dir TEXT,
            storage_mode TEXT NOT NULL DEFAULT 'unverified',
            storage_verified_utc TEXT,
            storage_evidence TEXT,
            spotter_launch_path TEXT,
            commstat_launch_path TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "system_key": "TEXT",
            "name": "TEXT NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "host": "TEXT NOT NULL DEFAULT '127.0.0.1'",
            "port": "INTEGER NOT NULL DEFAULT 2442",
            "offset_hz": "INTEGER NOT NULL DEFAULT 0",
            "profile_path": "TEXT",
            "directed_path": "TEXT",
            "inbox_path": "TEXT",
            "forms_path": "TEXT",
            "install_path": "TEXT",
            "variant_family": "TEXT NOT NULL DEFAULT 'unknown'",
            "variant_version": "TEXT",
            "rig_name": "TEXT",
            "rig_name_source": "TEXT",
            "application_data_root": "TEXT",
            "all_path": "TEXT",
            "save_dir": "TEXT",
            "storage_mode": "TEXT NOT NULL DEFAULT 'unverified'",
            "storage_verified_utc": "TEXT",
            "storage_evidence": "TEXT",
            "spotter_launch_path": "TEXT",
            "commstat_launch_path": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_js8_instances_system_key ON js8_instances(system_key)",
        ),
    },
    "fast_light_configs": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS fast_light_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_key TEXT UNIQUE,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            flrig_path TEXT,
            flrig_host TEXT NOT NULL DEFAULT '127.0.0.1',
            flrig_port INTEGER NOT NULL DEFAULT 12345,
            fldigi_path TEXT,
            fldigi_host TEXT,
            fldigi_port INTEGER NOT NULL DEFAULT 7362,
            fldigi_log_path TEXT,
            fldigi_checkin_dir TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "system_key": "TEXT",
            "name": "TEXT NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "flrig_path": "TEXT",
            "flrig_host": "TEXT NOT NULL DEFAULT '127.0.0.1'",
            "flrig_port": "INTEGER NOT NULL DEFAULT 12345",
            "fldigi_path": "TEXT",
            "fldigi_host": "TEXT",
            "fldigi_port": "INTEGER NOT NULL DEFAULT 7362",
            "fldigi_log_path": "TEXT",
            "fldigi_checkin_dir": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fast_light_configs_system_key ON fast_light_configs(system_key)",
        ),
    },
    "varac_nodes": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS varac_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_key TEXT UNIQUE,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            install_path TEXT,
            db_path TEXT,
            ini_path TEXT,
            vara_runtime_path TEXT,
            vara_ini_path TEXT,
            launch_cmd TEXT,
            incoming_path TEXT,
            native_management_state TEXT NOT NULL DEFAULT 'operator',
            native_writer_key TEXT,
            desired_fingerprint TEXT,
            observed_fingerprint TEXT,
            last_native_verified_utc TEXT,
            native_verification_summary TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "system_key": "TEXT",
            "name": "TEXT NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "install_path": "TEXT",
            "db_path": "TEXT",
            "ini_path": "TEXT",
            "vara_runtime_path": "TEXT",
            "vara_ini_path": "TEXT",
            "launch_cmd": "TEXT",
            "incoming_path": "TEXT",
            "native_management_state": "TEXT NOT NULL DEFAULT 'operator'",
            "native_writer_key": "TEXT",
            "desired_fingerprint": "TEXT",
            "observed_fingerprint": "TEXT",
            "last_native_verified_utc": "TEXT",
            "native_verification_summary": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_varac_nodes_system_key ON varac_nodes(system_key)",
        ),
    },
    "software_instance_manifests": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS software_instance_manifests (
            instance_key TEXT PRIMARY KEY,
            family_key TEXT NOT NULL,
            application_system_key TEXT,
            management_mode TEXT NOT NULL DEFAULT 'operator',
            provenance TEXT NOT NULL DEFAULT 'manual',
            executable_path TEXT,
            configuration_path TEXT,
            configuration_root TEXT,
            data_root TEXT,
            launch_command TEXT,
            host TEXT NOT NULL DEFAULT '127.0.0.1',
            ports_json TEXT NOT NULL DEFAULT '[]',
            resource_claims_json TEXT NOT NULL DEFAULT '[]',
            desired_fingerprint TEXT,
            observed_fingerprint TEXT,
            verification_state TEXT NOT NULL DEFAULT 'configured',
            verification_summary TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            last_discovered_utc TEXT,
            last_verified_utc TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "family_key": "TEXT NOT NULL",
            "application_system_key": "TEXT",
            "management_mode": "TEXT NOT NULL DEFAULT 'operator'",
            "provenance": "TEXT NOT NULL DEFAULT 'manual'",
            "executable_path": "TEXT",
            "configuration_path": "TEXT",
            "configuration_root": "TEXT",
            "data_root": "TEXT",
            "launch_command": "TEXT",
            "host": "TEXT NOT NULL DEFAULT '127.0.0.1'",
            "ports_json": "TEXT NOT NULL DEFAULT '[]'",
            "resource_claims_json": "TEXT NOT NULL DEFAULT '[]'",
            "desired_fingerprint": "TEXT",
            "observed_fingerprint": "TEXT",
            "verification_state": "TEXT NOT NULL DEFAULT 'configured'",
            "verification_summary": "TEXT",
            "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "last_discovered_utc": "TEXT",
            "last_verified_utc": "TEXT",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_software_manifests_family ON software_instance_manifests(family_key)",
            "CREATE INDEX IF NOT EXISTS idx_software_manifests_application ON software_instance_manifests(family_key, application_system_key)",
            "CREATE INDEX IF NOT EXISTS idx_software_manifests_verification ON software_instance_manifests(verification_state)",
        ),
    },
    "radio_software_identity_sets": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS radio_software_identity_sets (
            radio_profile_id INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL DEFAULT 1,
            generation INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "generation": "INTEGER NOT NULL DEFAULT 0",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (),
    },
    "radio_software_identity_records": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS radio_software_identity_records (
            radio_profile_id INTEGER NOT NULL,
            family_key TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0,
            fingerprint TEXT NOT NULL,
            record_json TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY(radio_profile_id, family_key),
            UNIQUE(radio_profile_id, identity_key),
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "identity_key": "TEXT NOT NULL DEFAULT ''",
            "bundle_id": "TEXT NOT NULL DEFAULT ''",
            "display_order": "INTEGER NOT NULL DEFAULT 0",
            "fingerprint": "TEXT NOT NULL DEFAULT ''",
            "record_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_radio_software_identity_order ON radio_software_identity_records(radio_profile_id, display_order)",
            "CREATE INDEX IF NOT EXISTS idx_radio_software_identity_bundle ON radio_software_identity_records(bundle_id)",
        ),
    },
    "radio_hardware_bundles": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS radio_hardware_bundles (
            radio_profile_id INTEGER PRIMARY KEY,
            bundle_key TEXT NOT NULL UNIQUE,
            schema_version INTEGER NOT NULL DEFAULT 1,
            generation INTEGER NOT NULL DEFAULT 0,
            lifecycle_state TEXT NOT NULL DEFAULT 'configured',
            aggregate_state TEXT NOT NULL DEFAULT 'unverified',
            source TEXT NOT NULL DEFAULT 'operator_confirmed',
            recovery_json TEXT NOT NULL DEFAULT '{}',
            last_verified_utc TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "bundle_key": "TEXT NOT NULL DEFAULT ''",
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "generation": "INTEGER NOT NULL DEFAULT 0",
            "lifecycle_state": "TEXT NOT NULL DEFAULT 'configured'",
            "aggregate_state": "TEXT NOT NULL DEFAULT 'unverified'",
            "source": "TEXT NOT NULL DEFAULT 'operator_confirmed'",
            "recovery_json": "TEXT NOT NULL DEFAULT '{}'",
            "last_verified_utc": "TEXT",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_radio_hardware_bundle_key ON radio_hardware_bundles(bundle_key)",
            "CREATE INDEX IF NOT EXISTS idx_radio_hardware_bundle_state ON radio_hardware_bundles(lifecycle_state, aggregate_state)",
        ),
    },
    "radio_hardware_bindings": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS radio_hardware_bindings (
            binding_key TEXT PRIMARY KEY,
            bundle_key TEXT NOT NULL,
            radio_profile_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            generation INTEGER NOT NULL,
            platform TEXT NOT NULL,
            provider TEXT NOT NULL,
            transport TEXT NOT NULL,
            selector_json TEXT NOT NULL,
            selector_fingerprint TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            label TEXT,
            observed_locator_json TEXT NOT NULL DEFAULT '{}',
            match_state TEXT NOT NULL DEFAULT 'missing',
            stability_class TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            transmit_capable INTEGER NOT NULL DEFAULT 0,
            desired_fingerprint TEXT,
            observed_fingerprint TEXT,
            verified_fingerprint TEXT,
            verification_state TEXT NOT NULL DEFAULT 'unverified',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            last_verified_utc TEXT,
            last_error TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            retired_utc TEXT,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE,
            FOREIGN KEY(bundle_key) REFERENCES radio_hardware_bundles(bundle_key) ON DELETE CASCADE
        )
        """,
        "columns": {
            "bundle_key": "TEXT NOT NULL DEFAULT ''",
            "radio_profile_id": "INTEGER NOT NULL DEFAULT 0",
            "role": "TEXT NOT NULL DEFAULT ''",
            "generation": "INTEGER NOT NULL DEFAULT 0",
            "platform": "TEXT NOT NULL DEFAULT ''",
            "provider": "TEXT NOT NULL DEFAULT ''",
            "transport": "TEXT NOT NULL DEFAULT ''",
            "selector_json": "TEXT NOT NULL DEFAULT '{}'",
            "selector_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "claim_key": "TEXT NOT NULL DEFAULT ''",
            "label": "TEXT",
            "observed_locator_json": "TEXT NOT NULL DEFAULT '{}'",
            "match_state": "TEXT NOT NULL DEFAULT 'missing'",
            "stability_class": "TEXT NOT NULL DEFAULT 'stable'",
            "required": "INTEGER NOT NULL DEFAULT 1",
            "transmit_capable": "INTEGER NOT NULL DEFAULT 0",
            "desired_fingerprint": "TEXT",
            "observed_fingerprint": "TEXT",
            "verified_fingerprint": "TEXT",
            "verification_state": "TEXT NOT NULL DEFAULT 'unverified'",
            "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "last_verified_utc": "TEXT",
            "last_error": "TEXT",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
            "retired_utc": "TEXT",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_radio_hardware_active_role ON radio_hardware_bindings(radio_profile_id, role) WHERE active=1",
            "CREATE INDEX IF NOT EXISTS idx_radio_hardware_active_claim ON radio_hardware_bindings(claim_key, active)",
            "CREATE INDEX IF NOT EXISTS idx_radio_hardware_bundle_generation ON radio_hardware_bindings(bundle_key, generation)",
        ),
    },
    "operating_profiles": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS operating_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_key TEXT UNIQUE,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            category TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'saved',
            scheduler_enabled INTEGER NOT NULL DEFAULT 1,
            scheduler_mode TEXT NOT NULL DEFAULT 'full',
            preferred_band_set_json TEXT NOT NULL DEFAULT '[]',
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            schedule_refs_json TEXT NOT NULL DEFAULT '[]',
            frequency_refs_json TEXT NOT NULL DEFAULT '[]',
            group_refs_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            use_messages INTEGER NOT NULL DEFAULT 1,
            use_map INTEGER NOT NULL DEFAULT 1,
            use_background_ingest INTEGER NOT NULL DEFAULT 1,
            use_launch_control INTEGER NOT NULL DEFAULT 0,
            use_net_control_tabs INTEGER NOT NULL DEFAULT 1,
            receive_only INTEGER NOT NULL DEFAULT 0,
            allow_profile_swap INTEGER NOT NULL DEFAULT 0,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "system_key": "TEXT",
            "name": "TEXT NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "description": "TEXT",
            "category": "TEXT NOT NULL DEFAULT 'normal'",
            "status": "TEXT NOT NULL DEFAULT 'saved'",
            "scheduler_enabled": "INTEGER NOT NULL DEFAULT 1",
            "scheduler_mode": "TEXT NOT NULL DEFAULT 'full'",
            "preferred_band_set_json": "TEXT NOT NULL DEFAULT '[]'",
            "source_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "schedule_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "frequency_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "group_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "notes": "TEXT",
            "use_messages": "INTEGER NOT NULL DEFAULT 1",
            "use_map": "INTEGER NOT NULL DEFAULT 1",
            "use_background_ingest": "INTEGER NOT NULL DEFAULT 1",
            "use_launch_control": "INTEGER NOT NULL DEFAULT 0",
            "use_net_control_tabs": "INTEGER NOT NULL DEFAULT 1",
            "receive_only": "INTEGER NOT NULL DEFAULT 0",
            "allow_profile_swap": "INTEGER NOT NULL DEFAULT 0",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_operating_profiles_system_key ON operating_profiles(system_key)",
        ),
    },
    "operating_profile_assignments": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS operating_profile_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_profile_id INTEGER NOT NULL,
            operating_profile_id INTEGER NOT NULL,
            assignment_state TEXT NOT NULL DEFAULT 'active',
            starts_utc TEXT,
            ends_utc TEXT,
            reason TEXT,
            created_by TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "device_profile_id": "INTEGER NOT NULL",
            "operating_profile_id": "INTEGER NOT NULL",
            "assignment_state": "TEXT NOT NULL DEFAULT 'active'",
            "starts_utc": "TEXT",
            "ends_utc": "TEXT",
            "reason": "TEXT",
            "created_by": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_assignments_device_state ON operating_profile_assignments(device_profile_id, assignment_state)",
            "CREATE INDEX IF NOT EXISTS idx_assignments_operating_state ON operating_profile_assignments(operating_profile_id, assignment_state)",
        ),
    },
    "frequency_plans": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS frequency_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_key TEXT UNIQUE,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            category TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'saved',
            receive_only INTEGER NOT NULL DEFAULT 0,
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            schedule_refs_json TEXT NOT NULL DEFAULT '[]',
            frequency_refs_json TEXT NOT NULL DEFAULT '[]',
            group_refs_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "system_key": "TEXT",
            "name": "TEXT NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "description": "TEXT",
            "category": "TEXT NOT NULL DEFAULT 'normal'",
            "status": "TEXT NOT NULL DEFAULT 'saved'",
            "receive_only": "INTEGER NOT NULL DEFAULT 0",
            "source_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "schedule_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "frequency_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "group_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "notes": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_frequency_plans_system_key ON frequency_plans(system_key)",
        ),
    },
    "assigned_plans": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS assigned_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_profile_id INTEGER NOT NULL,
            frequency_plan_id INTEGER NOT NULL,
            assignment_state TEXT NOT NULL DEFAULT 'active',
            assignment_category TEXT NOT NULL DEFAULT 'normal',
            scheduler_mode TEXT NOT NULL DEFAULT 'full',
            starts_utc TEXT,
            ends_utc TEXT,
            reason TEXT,
            created_by TEXT,
            validation_status_json TEXT NOT NULL DEFAULT '{}',
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "device_profile_id": "INTEGER NOT NULL",
            "frequency_plan_id": "INTEGER NOT NULL",
            "assignment_state": "TEXT NOT NULL DEFAULT 'active'",
            "assignment_category": "TEXT NOT NULL DEFAULT 'normal'",
            "scheduler_mode": "TEXT NOT NULL DEFAULT 'full'",
            "starts_utc": "TEXT",
            "ends_utc": "TEXT",
            "reason": "TEXT",
            "created_by": "TEXT",
            "validation_status_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_assigned_plans_device_state ON assigned_plans(device_profile_id, assignment_state)",
            "CREATE INDEX IF NOT EXISTS idx_assigned_plans_plan_state ON assigned_plans(frequency_plan_id, assignment_state)",
        ),
    },
    "rf_guard_events": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS rf_guard_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            guard_name TEXT,
            guard_mode TEXT,
            device_profile_id INTEGER,
            peer_device_profile_id INTEGER,
            frequency_plan_id INTEGER,
            band TEXT,
            frequency TEXT,
            action_source TEXT,
            decision TEXT,
            message TEXT,
            created_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "event_type": "TEXT NOT NULL",
            "guard_name": "TEXT",
            "guard_mode": "TEXT",
            "device_profile_id": "INTEGER",
            "peer_device_profile_id": "INTEGER",
            "frequency_plan_id": "INTEGER",
            "band": "TEXT",
            "frequency": "TEXT",
            "action_source": "TEXT",
            "decision": "TEXT",
            "message": "TEXT",
            "created_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_rf_guard_events_created ON rf_guard_events(created_utc)",
            "CREATE INDEX IF NOT EXISTS idx_rf_guard_events_device ON rf_guard_events(device_profile_id)",
        ),
    },
    "station_coordination_policies": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS station_coordination_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            policy_type TEXT NOT NULL,
            source_device_id INTEGER,
            target_device_id INTEGER,
            priority INTEGER NOT NULL DEFAULT 100,
            trigger_json TEXT NOT NULL DEFAULT '{}',
            action_json TEXT NOT NULL DEFAULT '{}',
            safety_mode TEXT NOT NULL DEFAULT 'warn',
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "name": "TEXT NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "policy_type": "TEXT NOT NULL",
            "source_device_id": "INTEGER",
            "target_device_id": "INTEGER",
            "priority": "INTEGER NOT NULL DEFAULT 100",
            "trigger_json": "TEXT NOT NULL DEFAULT '{}'",
            "action_json": "TEXT NOT NULL DEFAULT '{}'",
            "safety_mode": "TEXT NOT NULL DEFAULT 'warn'",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_coordination_policies_enabled ON station_coordination_policies(enabled)",
            "CREATE INDEX IF NOT EXISTS idx_coordination_policies_type ON station_coordination_policies(policy_type)",
        ),
    },
    "varac_clusters": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS varac_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            shared_db_path TEXT,
            shared_bbs_path TEXT,
            shared_bbs_archive_path TEXT,
            counters_refresh_sec INTEGER NOT NULL DEFAULT 30,
            ptt_lock_enabled INTEGER NOT NULL DEFAULT 0,
            gateway_handler_device_id INTEGER,
            email_gateway_sender_device_id INTEGER,
            native_management_state TEXT NOT NULL DEFAULT 'operator',
            native_writer_key TEXT,
            desired_fingerprint TEXT,
            observed_fingerprint TEXT,
            resource_claims_json TEXT NOT NULL DEFAULT '[]',
            last_native_verified_utc TEXT,
            native_verification_summary TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """,
        "columns": {
            "name": "TEXT NOT NULL",
            "cluster_id": "TEXT NOT NULL",
            "shared_db_path": "TEXT",
            "shared_bbs_path": "TEXT",
            "shared_bbs_archive_path": "TEXT",
            "counters_refresh_sec": "INTEGER NOT NULL DEFAULT 30",
            "ptt_lock_enabled": "INTEGER NOT NULL DEFAULT 0",
            "gateway_handler_device_id": "INTEGER",
            "email_gateway_sender_device_id": "INTEGER",
            "native_management_state": "TEXT NOT NULL DEFAULT 'operator'",
            "native_writer_key": "TEXT",
            "desired_fingerprint": "TEXT",
            "observed_fingerprint": "TEXT",
            "resource_claims_json": "TEXT NOT NULL DEFAULT '[]'",
            "last_native_verified_utc": "TEXT",
            "native_verification_summary": "TEXT",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_varac_clusters_cluster_id ON varac_clusters(cluster_id)",
        ),
    },
    "varac_cluster_members": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS varac_cluster_members (
            cluster_id INTEGER NOT NULL,
            device_profile_id INTEGER NOT NULL,
            instance_number INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY (cluster_id, device_profile_id)
        )
        """,
        "columns": {
            "cluster_id": "INTEGER NOT NULL",
            "device_profile_id": "INTEGER NOT NULL",
            "instance_number": "INTEGER NOT NULL",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "created_utc": "TEXT NOT NULL",
            "updated_utc": "TEXT NOT NULL",
        },
        "indexes": (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_varac_cluster_members_instance ON varac_cluster_members(cluster_id, instance_number)",
        ),
    },
    "varac_native_apply_journal": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS varac_native_apply_journal (
            id TEXT PRIMARY KEY,
            plan_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            operation TEXT NOT NULL,
            writer_key TEXT NOT NULL,
            targets_json TEXT NOT NULL DEFAULT '[]',
            desired_json TEXT NOT NULL DEFAULT '{}',
            observed_json TEXT NOT NULL DEFAULT '{}',
            backup_manifest_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL,
            completed_utc TEXT
        )
        """,
        "columns": {
            "plan_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT 'pending'",
            "operation": "TEXT NOT NULL DEFAULT ''",
            "writer_key": "TEXT NOT NULL DEFAULT ''",
            "targets_json": "TEXT NOT NULL DEFAULT '[]'",
            "desired_json": "TEXT NOT NULL DEFAULT '{}'",
            "observed_json": "TEXT NOT NULL DEFAULT '{}'",
            "backup_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
            "error": "TEXT",
            "created_utc": "TEXT NOT NULL DEFAULT ''",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
            "completed_utc": "TEXT",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_varac_native_journal_state ON varac_native_apply_journal(state)",
            "CREATE INDEX IF NOT EXISTS idx_varac_native_journal_fingerprint ON varac_native_apply_journal(plan_fingerprint)",
        ),
    },
    "radio_launch_bundles": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS radio_launch_bundles (
            radio_profile_id INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL DEFAULT 1,
            launch_enabled INTEGER NOT NULL DEFAULT 0,
            migrated_from_legacy INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL,
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "launch_enabled": "INTEGER NOT NULL DEFAULT 0",
            "migrated_from_legacy": "INTEGER NOT NULL DEFAULT 0",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (),
    },
    "radio_launch_bundle_items": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS radio_launch_bundle_items (
            radio_profile_id INTEGER NOT NULL,
            instance_key TEXT NOT NULL,
            app_name TEXT NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            launch_at_startup INTEGER NOT NULL DEFAULT 0,
            monitor_health INTEGER NOT NULL DEFAULT 1,
            command_override TEXT,
            path_override TEXT,
            dependencies_json TEXT NOT NULL DEFAULT '[]',
            readiness_json TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL,
            PRIMARY KEY(radio_profile_id, instance_key),
            FOREIGN KEY(radio_profile_id) REFERENCES device_profiles(id) ON DELETE CASCADE
        )
        """,
        "columns": {
            "app_name": "TEXT NOT NULL DEFAULT ''",
            "display_order": "INTEGER NOT NULL DEFAULT 0",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "launch_at_startup": "INTEGER NOT NULL DEFAULT 0",
            "monitor_health": "INTEGER NOT NULL DEFAULT 1",
            "command_override": "TEXT",
            "path_override": "TEXT",
            "dependencies_json": "TEXT NOT NULL DEFAULT '[]'",
            "readiness_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_utc": "TEXT NOT NULL DEFAULT ''",
        },
        "indexes": (
            "CREATE INDEX IF NOT EXISTS idx_radio_launch_items_order ON radio_launch_bundle_items(radio_profile_id, display_order)",
        ),
    },
    "launch_bundle_migration_audit": {
        "ddl": """
        CREATE TABLE IF NOT EXISTS launch_bundle_migration_audit (
            migration_key TEXT PRIMARY KEY,
            occurred_utc TEXT NOT NULL,
            source_key TEXT NOT NULL,
            target_radio_profile_id INTEGER,
            target_reason TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            item_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            backup_path TEXT,
            result_json TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "columns": {
            "occurred_utc": "TEXT NOT NULL DEFAULT ''",
            "source_key": "TEXT NOT NULL DEFAULT ''",
            "target_radio_profile_id": "INTEGER",
            "target_reason": "TEXT NOT NULL DEFAULT ''",
            "source_sha256": "TEXT NOT NULL DEFAULT ''",
            "item_count": "INTEGER NOT NULL DEFAULT 0",
            "state": "TEXT NOT NULL DEFAULT ''",
            "backup_path": "TEXT",
            "result_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "indexes": (),
    },
}


def settings_db_path() -> Path:
    return get_config_dir() / "config" / "freqinout.db"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _coerce_text(value: Any, default: str = "") -> str:
    try:
        return str(value if value is not None else default).strip()
    except Exception:
        return str(default or "").strip()


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except Exception:
        return int(default)


def _normalize_hold_duration_minutes(value: Any) -> int:
    minutes = _coerce_int(value, DEFAULT_HOLD_DURATION_MINUTES)
    return minutes if minutes in SUPPORTED_HOLD_DURATION_MINUTES else DEFAULT_HOLD_DURATION_MINUTES


def _coerce_optional_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _coerce_bool_int(value: Any, default: bool = False) -> int:
    if value in (None, ""):
        return 1 if default else 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) != 0 else 0
    return 1 if str(value).strip().lower() in {"1", "true", "yes", "on"} else 0


def _normalize_system_key(raw: Any, fallback: str) -> str:
    text = _coerce_text(raw, fallback).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or fallback


def _settings_text(values: Mapping[str, Any], key: str, default: str = "") -> str:
    return _coerce_text(values.get(key, default), default)


def _settings_int(values: Mapping[str, Any], key: str, default: int) -> int:
    return _coerce_int(values.get(key, default), default)


def _settings_optional_int(values: Mapping[str, Any], key: str, default: Optional[int] = None) -> Optional[int]:
    return _coerce_optional_int(values.get(key), default)


def _settings_bool(values: Mapping[str, Any], key: str, default: bool) -> bool:
    return bool(_coerce_bool_int(values.get(key), default))


def _fetchone_dict(cursor: sqlite3.Cursor) -> Optional[Dict[str, Any]]:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [str(col[0]) for col in (cursor.description or [])]
    return {columns[idx]: row[idx] for idx in range(min(len(columns), len(row)))}


def _fetchall_dicts(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    cursor = conn.execute(query, tuple(params))
    columns = [str(col[0]) for col in (cursor.description or [])]
    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        if isinstance(row, sqlite3.Row):
            rows.append(dict(row))
        else:
            rows.append({columns[idx]: row[idx] for idx in range(min(len(columns), len(row)))})
    return rows


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table),),
    ).fetchone()
    return row is not None


def _conn_database_path(conn: sqlite3.Connection) -> Optional[Path]:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None
        path_text = str(row[2] if not isinstance(row, sqlite3.Row) else row["file"] or "").strip()
        return Path(path_text) if path_text else None
    except Exception:
        return None


def _load_kv_settings(conn: sqlite3.Connection) -> Dict[str, Any]:
    loaded: Dict[str, Any] = {}
    for key, value in conn.execute("SELECT key, value FROM kv").fetchall():
        try:
            loaded[str(key)] = json.loads(value)
        except Exception:
            loaded[str(key)] = value
    return loaded


def _write_kv_settings(conn: sqlite3.Connection, updates: Mapping[str, Any]) -> None:
    if not updates:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
        [(str(key), json.dumps(value)) for key, value in updates.items()],
    )


def _next_system_key(conn: sqlite3.Connection, table: str, requested: Any, *, exclude_id: Optional[int] = None) -> str:
    base = _normalize_system_key(requested, "record")
    candidate = base
    suffix = 2
    while True:
        if exclude_id is None:
            row = conn.execute(f"SELECT id FROM {table} WHERE system_key=?", (candidate,)).fetchone()
        else:
            row = conn.execute(
                f"SELECT id FROM {table} WHERE system_key=? AND id<>?",
                (candidate, int(exclude_id)),
            ).fetchone()
        if not row:
            return candidate
        candidate = f"{base}_{suffix}"
        suffix += 1


def _normalize_control_backend(settings_values: Mapping[str, Any]) -> str:
    raw = _coerce_text(settings_values.get("control_via", "FLRig"), "FLRig").upper()
    if raw == "FLRIG":
        return "flrig"
    if raw == "JS8CALL":
        return "js8call"
    if raw == "RIGCTLD":
        return "rigctld"
    return "manual"


def _legacy_control_via(control_backend: str) -> str:
    backend = _coerce_text(control_backend, "manual").lower()
    if backend == "flrig":
        return "FLRig"
    if backend == "js8call":
        return "JS8Call"
    if backend == "rigctld":
        return "RIGCTLD"
    return "Manual"


def _normalize_assignment_state(value: Any, default: str = "active") -> str:
    state = _coerce_text(value, default).strip().lower() or default
    return state if state in SUPPORTED_ASSIGNMENT_STATES else default


def _normalize_frequency_plan_category(value: Any, default: str = "normal") -> str:
    category = _coerce_text(value, default).strip().lower().replace(" ", "_").replace("-", "_") or default
    return category if category in SUPPORTED_FREQUENCY_PLAN_CATEGORIES else default


def _normalize_frequency_plan_status(value: Any, default: str = "saved") -> str:
    status = _coerce_text(value, default).strip().lower() or default
    return status if status in SUPPORTED_FREQUENCY_PLAN_STATUSES else default


def normalize_ptt_group(value: Any) -> str:
    text = _coerce_text(value, "")
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().upper()


def normalize_resource_group(value: Any) -> str:
    text = _coerce_text(value, "")
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().upper()


def normalize_rf_guard_mode(value: Any, default: str = "warn") -> str:
    text = _coerce_text(value, default).strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "warn-only": "warn",
        "warning": "warn",
        "require-confirmation": "confirm",
        "confirmation": "confirm",
        "confirm": "confirm",
        "block": "block",
        "blocked": "block",
    }
    return aliases.get(text, text if text in {"warn", "confirm", "block"} else default)


def rf_guard_mode_label(value: Any) -> str:
    labels = {
        "warn": "Warn only",
        "confirm": "Require confirmation",
        "block": "Block",
    }
    return labels.get(normalize_rf_guard_mode(value), "Warn only")


def stricter_rf_guard_mode(left: Any, right: Any) -> str:
    order = {"warn": 0, "confirm": 1, "block": 2}
    left_mode = normalize_rf_guard_mode(left, "warn")
    right_mode = normalize_rf_guard_mode(right, "warn")
    return left_mode if order.get(left_mode, 0) >= order.get(right_mode, 0) else right_mode


def _normalize_band_token(value: Any) -> str:
    return re.sub(r"\s+", "", _coerce_text(value, "").upper())


COMMON_AMATEUR_BANDS = (
    "160M",
    "80M",
    "60M",
    "40M",
    "30M",
    "20M",
    "17M",
    "15M",
    "12M",
    "10M",
    "6M",
    "2M",
    "1.25M",
    "70CM",
    "33CM",
    "23CM",
)

WEEKDAY_ALIASES = {
    "MON": "MON",
    "MONDAY": "MON",
    "TUE": "TUE",
    "TUES": "TUE",
    "TUESDAY": "TUE",
    "WED": "WED",
    "WEDNESDAY": "WED",
    "THU": "THU",
    "THUR": "THU",
    "THURS": "THU",
    "THURSDAY": "THU",
    "FRI": "FRI",
    "FRIDAY": "FRI",
    "SAT": "SAT",
    "SATURDAY": "SAT",
    "SUN": "SUN",
    "SUNDAY": "SUN",
    "ALL": "ALL",
    "DAILY": "ALL",
    "EVERYDAY": "ALL",
}
WEEKDAY_ORDER = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
WEEK_MINUTES = 7 * 24 * 60


@dataclass(frozen=True)
class FrequencyPlanScheduleWindow:
    band: str
    day: str
    start_minute: int
    end_minute: int
    source: str = ""


def _extract_band_tokens_from_text(value: Any) -> List[str]:
    text = _coerce_text(value, "").upper()
    if not text:
        return []
    out: List[str] = []
    for band in COMMON_AMATEUR_BANDS:
        pattern = re.escape(band).replace(r"\.", r"[\.\s]?")
        if re.search(rf"(?<![A-Z0-9]){pattern}(?![A-Z0-9])", text):
            out.append(band)
    return list(dict.fromkeys(out))


def _parse_string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    out = [_normalize_band_token(item) for item in parsed if _normalize_band_token(item)]
    return list(dict.fromkeys(out))


def _coerce_json_array_text(value: Any) -> str:
    return json.dumps(_parse_string_list(value))


def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return list(value)
    if value in (None, ""):
        return []
    try:
        loaded = json.loads(str(value))
    except Exception:
        return []
    return list(loaded) if isinstance(loaded, list) else []


def _coerce_json_list_text(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps(value, sort_keys=True)
    if value in (None, ""):
        return "[]"
    return json.dumps(_parse_json_list(value), sort_keys=True)


def _parse_ref_list(value: Any) -> List[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    refs = [_coerce_text(item, "").strip() for item in parsed]
    return list(dict.fromkeys(ref for ref in refs if ref))


def _parse_ref_items(value: Any) -> List[Any]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    if isinstance(parsed, list):
        return [item for item in parsed if item not in (None, "")]
    return []


def _coerce_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(0, parsed)


def _coerce_ref_list_json_text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            parsed: List[Any] = []
        else:
            try:
                loaded = json.loads(text)
                parsed = list(loaded) if isinstance(loaded, list) else []
            except Exception:
                parsed = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    out: List[Any] = []
    seen: set[str] = set()
    for item in parsed:
        if item in (None, ""):
            continue
        if isinstance(item, Mapping):
            clean_item = {str(k): v for k, v in item.items() if v not in (None, "")}
        else:
            clean_item = _coerce_text(item, "").strip()
        if clean_item in ({}, ""):
            continue
        key = json.dumps(clean_item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean_item)
    return json.dumps(out, sort_keys=True, default=str)


def _normalize_varac_cluster_id(value: Any, fallback: str = "CLUSTER") -> str:
    text = _coerce_text(value, fallback).upper() or fallback
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Z0-9_.-]+", "-", text)
    text = text.strip("._-")
    return text or fallback


def _is_observer_device_class(value: Any) -> bool:
    raw = value.get("device_class", "tx_rx") if isinstance(value, Mapping) else value
    return _coerce_text(raw, "tx_rx").lower() == "observer"


def _is_receive_only_operating_profile(value: Any) -> bool:
    raw = value.get("receive_only", 0) if isinstance(value, Mapping) else value
    return bool(_coerce_bool_int(raw, False))


def _is_receive_only_frequency_plan(value: Any) -> bool:
    raw = value.get("receive_only", 0) if isinstance(value, Mapping) else value
    return bool(_coerce_bool_int(raw, False))


def _validate_assignment_plan_compatibility(device: Mapping[str, Any], operating: Mapping[str, Any]) -> None:
    if _is_observer_device_class(device) and not _is_receive_only_operating_profile(operating):
        raise ValueError("Observer / SDR radios can only be assigned receive-only operating models.")


def _validate_schedule_assignment_compatibility(device: Mapping[str, Any], frequency_plan: Mapping[str, Any]) -> None:
    if _is_observer_device_class(device) and not _is_receive_only_frequency_plan(frequency_plan):
        raise ValueError("Observer / SDR radios can only be assigned receive-only schedule plans.")


def _band_tokens_from_plan_ref_item(item: Any) -> List[str]:
    bands: List[str] = []
    if isinstance(item, Mapping):
        for field in ("band", "band_name", "band_label", "target_band"):
            bands.extend(_extract_band_tokens_from_text(item.get(field, "")))
        for field in (
            "frequency",
            "freq",
            "freq_mhz",
            "frequency_mhz",
            "freq_hz",
            "frequency_hz",
        ):
            freq_hz = _parse_frequency_hz_value(item.get(field, ""))
            inferred = _band_from_frequency_hz(freq_hz)
            if inferred:
                bands.append(inferred)
        # Keep a text fallback so older plans with ad-hoc refs remain covered.
        bands.extend(_extract_band_tokens_from_text(" ".join(_coerce_text(v, "") for v in item.values())))
        return bands
    bands.extend(_extract_band_tokens_from_text(item))
    freq_hz = _parse_frequency_hz_value(item)
    inferred = _band_from_frequency_hz(freq_hz)
    if inferred:
        bands.append(inferred)
    return bands


def _frequency_plan_bands(frequency_plan: Mapping[str, Any]) -> List[str]:
    bands: List[str] = []
    for key in ("frequency_refs_json", "schedule_refs_json", "group_refs_json", "source_refs_json", "notes", "description", "name"):
        value = frequency_plan.get(key, "") if isinstance(frequency_plan, Mapping) else ""
        if str(key).endswith("_json"):
            for item in _parse_ref_items(value):
                bands.extend(_band_tokens_from_plan_ref_item(item))
        else:
            bands.extend(_extract_band_tokens_from_text(value))
    for freq_hz in _frequency_plan_frequency_hz_values(frequency_plan):
        inferred = _band_from_frequency_hz(freq_hz)
        if inferred:
            bands.append(inferred)
    return list(dict.fromkeys(_normalize_band_token(band) for band in bands if _normalize_band_token(band)))


def _parse_frequency_hz_value(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number <= 0:
            return None
        if number < 1000:
            return int(round(number * 1_000_000))
        if number < 1_000_000:
            return int(round(number * 1000))
        return int(round(number))
    text = _coerce_text(value, "").strip()
    if not text:
        return None
    matches = re.findall(
        r"(?<![\d.])(\d{1,3}\.\d+|\d+(?:\.\d+)?\s*(?:MHZ|KHZ|HZ))(?![\d.])",
        text,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    preferred = [match for match in matches if "." in str(match)]
    raw_text = str(preferred[-1] if preferred else matches[-1]).strip().upper().replace(" ", "")
    suffix = ""
    for candidate_suffix in ("MHZ", "KHZ", "HZ"):
        if raw_text.endswith(candidate_suffix):
            suffix = candidate_suffix
            raw_text = raw_text[: -len(candidate_suffix)]
            break
    try:
        number = float(raw_text)
    except Exception:
        return None
    if number <= 0:
        return None
    if suffix == "HZ":
        return int(round(number))
    if suffix == "KHZ":
        return int(round(number * 1000))
    if suffix == "MHZ" or number < 1000:
        return int(round(number * 1_000_000))
    return None


def _frequency_plan_frequency_hz_values(frequency_plan: Mapping[str, Any]) -> List[int]:
    values: List[int] = []
    if not isinstance(frequency_plan, Mapping):
        return values
    for key in ("frequency_refs_json", "schedule_refs_json"):
        for item in _parse_ref_items(frequency_plan.get(key, "[]")):
            if isinstance(item, Mapping):
                candidates = (
                    item.get("frequency_hz"),
                    item.get("freq_hz"),
                    item.get("frequency"),
                    item.get("freq"),
                    item.get("name"),
                )
                for candidate in candidates:
                    parsed = _parse_frequency_hz_value(candidate)
                    if parsed:
                        values.append(parsed)
            else:
                parsed = _parse_frequency_hz_value(item)
                if parsed:
                    values.append(parsed)
    return list(dict.fromkeys(values))


def _band_from_frequency_hz(freq_hz: Optional[int]) -> str:
    if not freq_hz:
        return ""
    try:
        mhz = float(freq_hz) / 1_000_000.0
    except Exception:
        return ""
    bands = [
        ("160M", 1.8, 2.0),
        ("80M", 3.5, 4.0),
        ("60M", 5.0, 5.5),
        ("40M", 7.0, 7.3),
        ("30M", 10.1, 10.15),
        ("20M", 14.0, 14.35),
        ("17M", 18.068, 18.168),
        ("15M", 21.0, 21.45),
        ("12M", 24.89, 24.99),
        ("10M", 28.0, 29.7),
        ("6M", 50.0, 54.0),
        ("2M", 144.0, 148.0),
    ]
    for name, lo, hi in bands:
        if lo <= mhz <= hi:
            return name
    return ""


def _parse_schedule_day(value: Any) -> str:
    text = _coerce_text(value, "").strip().upper()
    if not text:
        return "ALL"
    return WEEKDAY_ALIASES.get(re.sub(r"[^A-Z]", "", text), text)


def _parse_hhmm_minutes(value: Any) -> Optional[int]:
    text = _coerce_text(value, "").strip()
    if not text:
        return None
    match = re.search(r"\b([0-2]?\d):([0-5]\d)\b", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        return hour * 60 + minute if hour < 24 else None
    match = re.search(r"\b([0-2]\d)([0-5]\d)\b", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        return hour * 60 + minute if hour < 24 else None
    return None


def _extract_schedule_field(text: str, *names: str) -> str:
    for name in names:
        match = re.search(rf"\b{name}\s*[:=]\s*([A-Za-z0-9:._+-]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _schedule_window_from_mapping(item: Mapping[str, Any]) -> Optional[FrequencyPlanScheduleWindow]:
    band_values = (
        item.get("band"),
        item.get("amateur_band"),
        item.get("band_name"),
        item.get("freq"),
        item.get("frequency"),
        item.get("name"),
    )
    bands: List[str] = []
    for value in band_values:
        bands.extend(_extract_band_tokens_from_text(value))
    band = _normalize_band_token(bands[0]) if bands else ""
    if not band:
        for value in (item.get("frequency_hz"), item.get("freq_hz"), item.get("frequency"), item.get("freq")):
            inferred = _band_from_frequency_hz(_parse_frequency_hz_value(value))
            if inferred:
                band = _normalize_band_token(inferred)
                break
    start = _parse_hhmm_minutes(
        item.get("start_utc", item.get("start_local", item.get("start", item.get("begin", item.get("time", "")))))
    )
    end = _parse_hhmm_minutes(item.get("end_utc", item.get("end_local", item.get("end", item.get("stop", "")))))
    if end is None and start is not None:
        duration = _coerce_optional_int(item.get("duration_minutes", item.get("duration_min", item.get("duration", ""))))
        if duration:
            end = (start + int(duration)) % (24 * 60)
    if not band or start is None or end is None:
        return None
    day = _parse_schedule_day(item.get("day_utc", item.get("day_local", item.get("day", item.get("weekday", "ALL")))))
    return FrequencyPlanScheduleWindow(band=band, day=day, start_minute=start, end_minute=end, source=_coerce_text(item, ""))


def _schedule_window_from_text(value: Any) -> Optional[FrequencyPlanScheduleWindow]:
    text = _coerce_text(value, "").strip()
    if not text:
        return None
    bands = _extract_band_tokens_from_text(text)
    band = _normalize_band_token(bands[0]) if bands else ""
    if not band:
        band = _normalize_band_token(_band_from_frequency_hz(_parse_frequency_hz_value(text)))
    if not band:
        return None
    start_text = _extract_schedule_field(text, "start_utc", "start_local", "start", "begin")
    end_text = _extract_schedule_field(text, "end_utc", "end_local", "end", "stop")
    start = _parse_hhmm_minutes(start_text)
    end = _parse_hhmm_minutes(end_text)
    if start is None or end is None:
        time_matches = re.findall(r"\b(?:[0-2]?\d:[0-5]\d|[0-2]\d[0-5]\d)\b", text)
        if len(time_matches) >= 2:
            start = _parse_hhmm_minutes(time_matches[0])
            end = _parse_hhmm_minutes(time_matches[1])
    if start is None or end is None:
        return None
    day_text = _extract_schedule_field(text, "day_utc", "day_local", "day", "weekday")
    if not day_text:
        day_match = re.search(
            r"\b(MON(?:DAY)?|TUE(?:S|SDAY)?|WED(?:NESDAY)?|THU(?:R|RS|RSDAY)?|FRI(?:DAY)?|SAT(?:URDAY)?|SUN(?:DAY)?|ALL|DAILY|EVERYDAY)\b",
            text,
            flags=re.IGNORECASE,
        )
        day_text = day_match.group(1) if day_match else "ALL"
    return FrequencyPlanScheduleWindow(
        band=band,
        day=_parse_schedule_day(day_text),
        start_minute=start,
        end_minute=end,
        source=text,
    )


def _frequency_plan_schedule_windows(frequency_plan: Mapping[str, Any]) -> List[FrequencyPlanScheduleWindow]:
    windows: List[FrequencyPlanScheduleWindow] = []
    if not isinstance(frequency_plan, Mapping):
        return windows
    for key in ("schedule_refs_json", "frequency_refs_json"):
        for item in _parse_ref_items(frequency_plan.get(key, "[]")):
            window = _schedule_window_from_mapping(item) if isinstance(item, Mapping) else _schedule_window_from_text(item)
            if window is not None:
                windows.append(window)
    unique: Dict[tuple[str, str, int, int], FrequencyPlanScheduleWindow] = {}
    for window in windows:
        unique.setdefault((window.band, window.day, window.start_minute, window.end_minute), window)
    return list(unique.values())


def _weekly_segments(window: FrequencyPlanScheduleWindow) -> List[tuple[int, int]]:
    days = range(7) if window.day == "ALL" else (WEEKDAY_ORDER.index(window.day),) if window.day in WEEKDAY_ORDER else range(7)
    segments: List[tuple[int, int]] = []
    for day_index in days:
        start = day_index * 24 * 60 + window.start_minute
        end = day_index * 24 * 60 + window.end_minute
        if window.end_minute <= window.start_minute:
            end += 24 * 60
        segments.append((start, end))
    return segments


def _schedule_windows_overlap(left: FrequencyPlanScheduleWindow, right: FrequencyPlanScheduleWindow) -> bool:
    for left_start, left_end in _weekly_segments(left):
        for right_start, right_end in _weekly_segments(right):
            for offset in (-WEEK_MINUTES, 0, WEEK_MINUTES):
                shifted_start = right_start + offset
                shifted_end = right_end + offset
                if left_start < shifted_end and shifted_start < left_end:
                    return True
    return False


def _frequency_plan_overlapping_bands(left: Mapping[str, Any], right: Mapping[str, Any]) -> List[str]:
    broad_overlap = set(_frequency_plan_bands(left)).intersection(_frequency_plan_bands(right))
    left_windows = _frequency_plan_schedule_windows(left)
    right_windows = _frequency_plan_schedule_windows(right)
    if left_windows and right_windows:
        bands = {
            left_window.band
            for left_window in left_windows
            for right_window in right_windows
            if left_window.band == right_window.band and _schedule_windows_overlap(left_window, right_window)
        }
        left_window_bands = set(window.band for window in left_windows)
        right_window_bands = set(window.band for window in right_windows)
        unresolved_bands = {
            band
            for band in broad_overlap
            if band not in left_window_bands or band not in right_window_bands
        }
        return sorted(bands.union(unresolved_bands))
    return sorted(broad_overlap)


def _frequency_plan_schedule_times_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_windows = _frequency_plan_schedule_windows(left)
    right_windows = _frequency_plan_schedule_windows(right)
    if not left_windows or not right_windows:
        return True
    return any(
        _schedule_windows_overlap(left_window, right_window)
        for left_window in left_windows
        for right_window in right_windows
    )


def _device_supported_bands(device: Mapping[str, Any]) -> List[str]:
    if not isinstance(device, Mapping):
        return []
    return _parse_string_list(device.get("antenna_supported_bands_json", "[]"))


def _rf_guard_event_conn(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    guard_name: str = "",
    guard_mode: str = "",
    device_profile_id: Optional[int] = None,
    peer_device_profile_id: Optional[int] = None,
    frequency_plan_id: Optional[int] = None,
    band: str = "",
    frequency: str = "",
    action_source: str = "schedule_assignment",
    decision: str = "",
    message: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO rf_guard_events (
            event_type, guard_name, guard_mode, device_profile_id, peer_device_profile_id,
            frequency_plan_id, band, frequency, action_source, decision, message, created_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _coerce_text(event_type, "rf_guard"),
            _coerce_text(guard_name, ""),
            normalize_rf_guard_mode(guard_mode, "warn"),
            int(device_profile_id) if device_profile_id not in (None, "") else None,
            int(peer_device_profile_id) if peer_device_profile_id not in (None, "") else None,
            int(frequency_plan_id) if frequency_plan_id not in (None, "") else None,
            _normalize_band_token(band),
            _coerce_text(frequency, ""),
            _coerce_text(action_source, "schedule_assignment") or "schedule_assignment",
            _coerce_text(decision, ""),
            _coerce_text(message, ""),
            _utc_now_iso(),
        ),
    )


def _schedule_assignment_validation_status_conn(
    conn: sqlite3.Connection,
    device: Mapping[str, Any],
    frequency_plan: Mapping[str, Any],
    *,
    emit_events: bool = True,
    assignment_plan_overrides: Optional[Mapping[int, int]] = None,
) -> Dict[str, Any]:
    plan_bands = _frequency_plan_bands(frequency_plan)
    supported_bands = _device_supported_bands(device)
    messages: List[str] = []
    warnings: List[str] = []
    blocked: List[str] = []
    events: List[Dict[str, Any]] = []
    device_id = int(device.get("id", 0) or 0)
    plan_id = int(frequency_plan.get("id", 0) or 0)
    plan_overrides: Dict[int, int] = {}
    for key, value in dict(assignment_plan_overrides or {}).items():
        try:
            override_device_id = int(key or 0)
            override_plan_id = int(value or 0)
        except Exception:
            continue
        if override_device_id > 0:
            plan_overrides[override_device_id] = override_plan_id

    antenna_mode = normalize_rf_guard_mode(device.get("antenna_band_guard_mode", "warn"))
    unsupported = [band for band in plan_bands if supported_bands and band not in supported_bands]
    for band in unsupported:
        message = (
            f"Antenna and schedule mismatch: {str(device.get('name', '') or 'Radio')} antenna support "
            f"does not include {band} for {str(frequency_plan.get('name', '') or 'Frequency Plan')}. "
            "Confirm the antenna path before transmitting."
        )
        target = blocked if antenna_mode == "block" else warnings
        target.append(message)
        events.append(
            {
                "event_type": "antenna_band_support",
                "guard_name": "Antenna Supports These Bands",
                "guard_mode": antenna_mode,
                "device_profile_id": device_id,
                "frequency_plan_id": plan_id,
                "band": band,
                "decision": "blocked" if antenna_mode == "block" else "warning",
                "message": message,
            }
        )

    overlap_group = normalize_resource_group(device.get("band_overlap_guard_group", ""))
    overlap_mode = normalize_rf_guard_mode(device.get("band_overlap_guard_mode", "warn"))
    if overlap_group and plan_bands:
        placeholders = ", ".join(["?"] * len(EFFECTIVE_ASSIGNMENT_STATES))
        rows = _fetchall_dicts(
            conn,
            f"""
            SELECT devices.*, assigned_plans.frequency_plan_id AS assigned_frequency_plan_id
              FROM assigned_plans
              JOIN device_profiles AS devices
                ON devices.id = assigned_plans.device_profile_id
             WHERE assigned_plans.device_profile_id<>?
               AND assigned_plans.assignment_state IN ({placeholders})
               AND COALESCE(devices.enabled, 1)=1
               AND UPPER(COALESCE(devices.band_overlap_guard_group, ''))=?
            """,
            (device_id, *tuple(EFFECTIVE_ASSIGNMENT_STATES), overlap_group),
        )
        for peer in rows:
            peer_id = int(peer.get("id", 0) or 0)
            peer_plan_id = int(plan_overrides.get(peer_id, peer.get("assigned_frequency_plan_id", 0)) or 0)
            if peer_plan_id <= 0:
                continue
            peer_plan = _record_by_id(conn, "frequency_plans", peer_plan_id)
            if not peer_plan:
                continue
            peer_overlap_mode = normalize_rf_guard_mode(peer.get("band_overlap_guard_mode", "warn"))
            effective_overlap_mode = stricter_rf_guard_mode(overlap_mode, peer_overlap_mode)
            for band in _frequency_plan_overlapping_bands(frequency_plan, peer_plan):
                message = (
                    f"{str(device.get('name', '') or 'Radio')} and {str(peer.get('name', '') or 'another radio')} "
                    f"would both be assigned on {band} in Prevent Band Overlap group {overlap_group}."
                )
                target = blocked if effective_overlap_mode == "block" else warnings
                target.append(message)
                events.append(
                    {
                        "event_type": "prevent_band_overlap",
                        "guard_name": overlap_group,
                        "guard_mode": effective_overlap_mode,
                        "device_profile_id": device_id,
                        "peer_device_profile_id": int(peer.get("id", 0) or 0),
                        "frequency_plan_id": plan_id,
                        "band": band,
                        "decision": "blocked" if effective_overlap_mode == "block" else "warning",
                        "message": message,
                    }
                )

    advanced_group = normalize_resource_group(device.get("advanced_frequency_guard_group", ""))
    advanced_window_hz = _coerce_nonnegative_int(device.get("advanced_frequency_guard_window_hz", 0))
    advanced_mode = normalize_rf_guard_mode(device.get("advanced_frequency_guard_mode", "warn"))
    plan_frequencies = _frequency_plan_frequency_hz_values(frequency_plan)
    if advanced_group and advanced_window_hz > 0 and plan_frequencies:
        placeholders = ", ".join(["?"] * len(EFFECTIVE_ASSIGNMENT_STATES))
        rows = _fetchall_dicts(
            conn,
            f"""
            SELECT devices.*, assigned_plans.frequency_plan_id AS assigned_frequency_plan_id
              FROM assigned_plans
              JOIN device_profiles AS devices
                ON devices.id = assigned_plans.device_profile_id
             WHERE assigned_plans.device_profile_id<>?
               AND assigned_plans.assignment_state IN ({placeholders})
               AND COALESCE(devices.enabled, 1)=1
               AND UPPER(COALESCE(devices.advanced_frequency_guard_group, ''))=?
               AND COALESCE(devices.advanced_frequency_guard_window_hz, 0)>0
            """,
            (device_id, *tuple(EFFECTIVE_ASSIGNMENT_STATES), advanced_group),
        )
        for peer in rows:
            peer_id = int(peer.get("id", 0) or 0)
            peer_plan_id = int(plan_overrides.get(peer_id, peer.get("assigned_frequency_plan_id", 0)) or 0)
            if peer_plan_id <= 0:
                continue
            peer_plan = _record_by_id(conn, "frequency_plans", peer_plan_id)
            if not peer_plan:
                continue
            peer_frequencies = _frequency_plan_frequency_hz_values(peer_plan)
            if not peer_frequencies:
                continue
            if not _frequency_plan_schedule_times_overlap(frequency_plan, peer_plan):
                continue
            peer_window_hz = _coerce_nonnegative_int(peer.get("advanced_frequency_guard_window_hz", 0))
            threshold_hz = max(advanced_window_hz, peer_window_hz)
            if threshold_hz <= 0:
                continue
            peer_mode = normalize_rf_guard_mode(peer.get("advanced_frequency_guard_mode", "warn"))
            effective_mode = stricter_rf_guard_mode(advanced_mode, peer_mode)
            for freq_hz in plan_frequencies:
                for peer_freq_hz in peer_frequencies:
                    delta_hz = abs(int(freq_hz) - int(peer_freq_hz))
                    if delta_hz > threshold_hz:
                        continue
                    band = _normalize_band_token(_band_from_frequency_hz(freq_hz))
                    message = (
                        f"{str(device.get('name', '') or 'Radio')} and {str(peer.get('name', '') or 'another radio')} "
                        f"would be within {threshold_hz} Hz in Advanced Guard group {advanced_group} "
                        f"({freq_hz} Hz vs {peer_freq_hz} Hz)."
                    )
                    target = blocked if effective_mode == "block" else warnings
                    target.append(message)
                    events.append(
                        {
                            "event_type": "advanced_frequency_guard",
                            "guard_name": advanced_group,
                            "guard_mode": effective_mode,
                            "device_profile_id": device_id,
                            "peer_device_profile_id": int(peer.get("id", 0) or 0),
                            "frequency_plan_id": plan_id,
                            "band": band,
                            "frequency": str(freq_hz),
                            "decision": "blocked" if effective_mode == "block" else "warning",
                            "message": message,
                        }
                    )
                    break

    if emit_events:
        for event in events:
            _rf_guard_event_conn(conn, **event)

    if blocked:
        state = "blocked"
    elif warnings:
        state = "warning"
    else:
        state = "ok"
        messages.append("RF guard validation completed.")
    return {
        "state": state,
        "rf_guard_validation": "enforced",
        "plan_bands": plan_bands,
        "supported_bands": supported_bands,
        "warnings": warnings,
        "blocked": blocked,
        "messages": messages + warnings + blocked,
        "device_profile_id": device_id,
        "frequency_plan_id": plan_id,
    }


def _operating_profile_has_observer_assignments(conn: sqlite3.Connection, operating_profile_id: int) -> bool:
    placeholders = ", ".join("?" for _ in EFFECTIVE_ASSIGNMENT_STATES)
    row = conn.execute(
        f"""
        SELECT assignments.id
          FROM operating_profile_assignments AS assignments
          JOIN device_profiles AS devices
            ON devices.id = assignments.device_profile_id
         WHERE assignments.operating_profile_id=?
           AND assignments.assignment_state IN ({placeholders})
           AND LOWER(COALESCE(devices.device_class, 'tx_rx'))='observer'
         LIMIT 1
        """,
        (int(operating_profile_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
    ).fetchone()
    return row is not None


def _frequency_plan_has_observer_assignments(conn: sqlite3.Connection, frequency_plan_id: int) -> bool:
    placeholders = ", ".join("?" for _ in EFFECTIVE_ASSIGNMENT_STATES)
    row = conn.execute(
        f"""
        SELECT assignments.id
          FROM assigned_plans AS assignments
          JOIN device_profiles AS devices
            ON devices.id = assignments.device_profile_id
         WHERE assignments.frequency_plan_id=?
           AND assignments.assignment_state IN ({placeholders})
           AND LOWER(COALESCE(devices.device_class, 'tx_rx'))='observer'
         LIMIT 1
        """,
        (int(frequency_plan_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
    ).fetchone()
    return row is not None


def _normalize_profile_swap_mode(value: Any, default: str = "use_target_profile") -> str:
    mode = _coerce_text(value, default).lower() or default
    return mode if mode in SUPPORTED_PROFILE_SWAP_MODES else default


def _parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    try:
        loaded = json.loads(str(value))
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _coerce_json_object_text(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if value in (None, ""):
        return "{}"
    parsed = _parse_json_object(value)
    return json.dumps(parsed, sort_keys=True)


def _coordination_policy_from_row(row: sqlite3.Row | Mapping[str, Any] | Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    data["policy_type"] = _coerce_text(data.get("policy_type", SHARED_PTT_POLICY_TYPE), SHARED_PTT_POLICY_TYPE).lower()
    data["safety_mode"] = _coerce_text(data.get("safety_mode", "warn"), "warn").lower() or "warn"
    data["trigger"] = _parse_json_object(data.get("trigger_json", "{}"))
    data["action"] = _parse_json_object(data.get("action_json", "{}"))
    return data


def _active_profile_swap_policy_conn(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
          FROM station_coordination_policies
         WHERE policy_type=?
           AND enabled=1
      ORDER BY priority ASC, id DESC
         LIMIT 1
        """,
        (PROFILE_SWAP_POLICY_TYPE,),
    ).fetchone()
    if row is None:
        return None
    return _coordination_policy_from_row(row)


def _assignment_snapshot_from_row(row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(row, Mapping):
        return {}
    operating_profile_id = row.get("operating_profile_id")
    return {
        "operating_profile_id": (
            int(operating_profile_id or 0) if operating_profile_id not in (None, "") else None
        ),
        "assignment_state": _normalize_assignment_state(row.get("assignment_state", "active"), "active"),
        "reason": _coerce_text(row.get("reason", ""), ""),
        "created_by": _coerce_text(row.get("created_by", "settings_ui"), "settings_ui") or "settings_ui",
        "starts_utc": _coerce_text(row.get("starts_utc", ""), ""),
        "ends_utc": _coerce_text(row.get("ends_utc", ""), ""),
    }


def _enrich_profile_swap_policy(conn: sqlite3.Connection, policy: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(policy, Mapping):
        return None
    data = dict(policy)
    trigger = dict(data.get("trigger") or {})
    action = dict(data.get("action") or {})
    source_id = int(data.get("source_device_id", 0) or 0)
    target_id = int(data.get("target_device_id", 0) or 0)
    source_device = _record_by_id(conn, "device_profiles", source_id) if source_id > 0 else None
    target_device = _record_by_id(conn, "device_profiles", target_id) if target_id > 0 else None
    data["source_device_name"] = _coerce_text((source_device or {}).get("name", ""), "")
    data["target_device_name"] = _coerce_text((target_device or {}).get("name", ""), "")
    data["mode"] = _normalize_profile_swap_mode(trigger.get("mode", "use_target_profile"))
    carried_profile_id = action.get("applied_operating_profile_id")
    if carried_profile_id not in (None, ""):
        carried_profile = _record_by_id(conn, "operating_profiles", int(carried_profile_id))
        data["applied_operating_profile_name"] = _coerce_text((carried_profile or {}).get("name", ""), "")
    else:
        data["applied_operating_profile_name"] = ""
    restore_assignment = dict(action.get("restore_target_assignment") or {})
    restore_operating_id = restore_assignment.get("operating_profile_id")
    if restore_operating_id not in (None, ""):
        restore_profile = _record_by_id(conn, "operating_profiles", int(restore_operating_id))
        data["restore_target_operating_profile_name"] = _coerce_text((restore_profile or {}).get("name", ""), "")
    else:
        data["restore_target_operating_profile_name"] = ""
    return data


def _varac_cluster_by_id(conn: sqlite3.Connection, cluster_db_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute("SELECT * FROM varac_clusters WHERE id=?", (int(cluster_db_id),))
    return _fetchone_dict(cur)


def _varac_cluster_membership_row(
    conn: sqlite3.Connection,
    cluster_db_id: int,
    device_profile_id: int,
) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT *
          FROM varac_cluster_members
         WHERE cluster_id=?
           AND device_profile_id=?
        """,
        (int(cluster_db_id), int(device_profile_id)),
    )
    return _fetchone_dict(cur)


def _varac_enabled_membership_for_device(
    conn: sqlite3.Connection,
    device_profile_id: int,
    *,
    exclude_cluster_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    params: List[Any] = [int(device_profile_id)]
    where = ""
    if exclude_cluster_id is not None:
        where = " AND cluster_id<>?"
        params.append(int(exclude_cluster_id))
    cur = conn.execute(
        f"""
        SELECT *
          FROM varac_cluster_members
         WHERE device_profile_id=?
           AND enabled=1{where}
      ORDER BY cluster_id ASC
         LIMIT 1
        """,
        params,
    )
    return _fetchone_dict(cur)


def _device_has_varac_cluster_membership(conn: sqlite3.Connection, device_profile_id: int) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM varac_cluster_members WHERE device_profile_id=? LIMIT 1",
        (int(device_profile_id),),
    )
    return cur.fetchone() is not None


def _sync_varac_cluster_member_enabled_flags_conn(conn: sqlite3.Connection) -> None:
    ensure_multi_radio_settings_schema(conn)
    conn.execute(
        """
        UPDATE device_profiles
           SET varac_cluster_member_enabled = CASE
                WHEN EXISTS (
                    SELECT 1
                      FROM varac_cluster_members
                     WHERE device_profile_id=device_profiles.id
                       AND enabled=1
                ) THEN 1 ELSE 0 END
        """
    )


def _list_varac_clusters_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    rows = conn.execute(
        """
        SELECT
            c.*,
            COUNT(m.device_profile_id) AS member_count,
            COALESCE(SUM(CASE WHEN m.enabled=1 THEN 1 ELSE 0 END), 0) AS enabled_member_count,
            g.name AS gateway_handler_name,
            eg.name AS email_gateway_sender_name
          FROM varac_clusters c
          LEFT JOIN varac_cluster_members m
            ON m.cluster_id = c.id
          LEFT JOIN device_profiles g
            ON g.id = c.gateway_handler_device_id
          LEFT JOIN device_profiles eg
            ON eg.id = c.email_gateway_sender_device_id
      GROUP BY c.id
      ORDER BY LOWER(c.name) ASC, c.id ASC
        """
    ).fetchall()
    clusters: List[Dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        cluster_id = int(data.get("id", 0) or 0)
        member_rows = conn.execute(
            """
            SELECT
                m.device_profile_id,
                m.instance_number,
                m.enabled,
                d.name AS device_name
              FROM varac_cluster_members m
              LEFT JOIN device_profiles d
                ON d.id = m.device_profile_id
             WHERE m.cluster_id=?
          ORDER BY m.enabled DESC, m.instance_number ASC, LOWER(COALESCE(d.name, '')) ASC, m.device_profile_id ASC
            """,
            (cluster_id,),
        ).fetchall()
        enabled_members = [
            dict(member)
            for member in member_rows
            if int(dict(member).get("enabled", 1) or 0) == 1
        ]
        data["member_count"] = int(data.get("member_count", 0) or 0)
        data["enabled_member_count"] = int(data.get("enabled_member_count", 0) or 0)
        data["gateway_handler_device_id"] = (
            int(data.get("gateway_handler_device_id", 0) or 0)
            if data.get("gateway_handler_device_id") not in (None, "")
            else None
        )
        data["gateway_handler_name"] = _coerce_text(data.get("gateway_handler_name", ""), "")
        data["email_gateway_sender_device_id"] = (
            int(data.get("email_gateway_sender_device_id", 0) or 0)
            if data.get("email_gateway_sender_device_id") not in (None, "")
            else None
        )
        data["email_gateway_sender_name"] = _coerce_text(
            data.get("email_gateway_sender_name", ""), ""
        )
        data["member_device_ids"] = [int(dict(member).get("device_profile_id", 0) or 0) for member in enabled_members]
        data["member_names"] = [
            _coerce_text(dict(member).get("device_name", ""), "")
            for member in enabled_members
            if _coerce_text(dict(member).get("device_name", ""), "")
        ]
        data["gateway_handler_ready"] = (
            data.get("gateway_handler_device_id") in data["member_device_ids"]
            if data.get("gateway_handler_device_id") is not None
            else False
        )
        data["email_gateway_sender_ready"] = (
            data.get("email_gateway_sender_device_id") in data["member_device_ids"]
            if data.get("email_gateway_sender_device_id") is not None
            else False
        )
        data["legacy_gateway_evidence_requires_review"] = (
            data.get("gateway_handler_device_id") is not None
            and data.get("email_gateway_sender_device_id") is None
        )
        clusters.append(data)
    return clusters


def _list_varac_cluster_members_conn(
    conn: sqlite3.Connection,
    *,
    cluster_id: Optional[int] = None,
    device_profile_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    where: List[str] = []
    params: List[Any] = []
    if cluster_id is not None:
        where.append("m.cluster_id=?")
        params.append(int(cluster_id))
    if device_profile_id is not None:
        where.append("m.device_profile_id=?")
        params.append(int(device_profile_id))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT
            m.cluster_id,
            m.device_profile_id,
            m.instance_number,
            m.enabled,
            m.created_utc,
            m.updated_utc,
            c.name AS cluster_name,
            c.cluster_id AS cluster_public_id,
            c.shared_db_path,
            c.shared_bbs_path,
            c.shared_bbs_archive_path,
            c.counters_refresh_sec,
            c.ptt_lock_enabled,
            c.gateway_handler_device_id,
            c.email_gateway_sender_device_id,
            d.name AS device_name,
            d.device_class,
            d.enabled AS device_enabled,
            d.runtime_active,
            d.runtime_primary
          FROM varac_cluster_members m
          JOIN varac_clusters c
            ON c.id = m.cluster_id
          JOIN device_profiles d
            ON d.id = m.device_profile_id
          {where_sql}
      ORDER BY LOWER(c.name) ASC, m.instance_number ASC, LOWER(d.name) ASC, m.device_profile_id ASC
        """,
        params,
    ).fetchall()
    members: List[Dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        gateway_id = (
            int(data.get("gateway_handler_device_id", 0) or 0)
            if data.get("gateway_handler_device_id") not in (None, "")
            else None
        )
        data["cluster_db_id"] = int(data.get("cluster_id", 0) or 0)
        data["cluster_public_id"] = _coerce_text(data.get("cluster_public_id", ""), "")
        data["device_class"] = _coerce_text(data.get("device_class", "tx_rx"), "tx_rx").lower() or "tx_rx"
        data["gateway_handler_device_id"] = gateway_id
        data["is_gateway_handler"] = gateway_id is not None and int(data.get("device_profile_id", 0) or 0) == gateway_id
        email_sender_id = (
            int(data.get("email_gateway_sender_device_id", 0) or 0)
            if data.get("email_gateway_sender_device_id") not in (None, "")
            else None
        )
        data["email_gateway_sender_device_id"] = email_sender_id
        data["is_email_gateway_sender"] = (
            email_sender_id is not None
            and int(data.get("device_profile_id", 0) or 0) == email_sender_id
        )
        members.append(data)
    return members


def _sync_pair_coordination_policies_conn(
    conn: sqlite3.Connection,
    *,
    policy_type: str,
    expected: Mapping[tuple[int, int], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    existing_rows = _fetchall_dicts(
        conn,
        """
        SELECT *
          FROM station_coordination_policies
         WHERE policy_type=?
        """,
        (str(policy_type),),
    )
    existing_by_pair: Dict[tuple[int, int], Dict[str, Any]] = {}
    for row in existing_rows:
        data = dict(row)
        pair = (
            int(data.get("source_device_id", 0) or 0),
            int(data.get("target_device_id", 0) or 0),
        )
        existing_by_pair[pair] = data

    now_iso = _utc_now_iso()
    for pair, record in expected.items():
        current = existing_by_pair.pop(pair, None)
        if current is None:
            conn.execute(
                """
                INSERT INTO station_coordination_policies (
                    name, enabled, policy_type, source_device_id, target_device_id,
                    priority, trigger_json, action_json, safety_mode, created_utc, updated_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["name"],
                    int(record["enabled"]),
                    record["policy_type"],
                    int(record["source_device_id"]),
                    int(record["target_device_id"]),
                    int(record["priority"]),
                    record["trigger_json"],
                    record["action_json"],
                    record["safety_mode"],
                    now_iso,
                    now_iso,
                ),
            )
            continue
        conn.execute(
            """
            UPDATE station_coordination_policies
               SET name=?, enabled=?, priority=?, trigger_json=?, action_json=?, safety_mode=?, updated_utc=?
             WHERE id=?
            """,
            (
                record["name"],
                int(record["enabled"]),
                int(record["priority"]),
                record["trigger_json"],
                record["action_json"],
                record["safety_mode"],
                now_iso,
                int(current.get("id", 0) or 0),
            ),
        )

    stale_ids = [int(row.get("id", 0) or 0) for row in existing_by_pair.values() if int(row.get("id", 0) or 0) > 0]
    if stale_ids:
        placeholders = ", ".join("?" for _ in stale_ids)
        conn.execute(f"DELETE FROM station_coordination_policies WHERE id IN ({placeholders})", tuple(stale_ids))
    conn.commit()
    refreshed = _fetchall_dicts(
        conn,
        """
        SELECT *
          FROM station_coordination_policies
         WHERE policy_type=?
      ORDER BY priority ASC, source_device_id ASC, target_device_id ASC, id ASC
        """,
        (str(policy_type),),
    )
    return [_coordination_policy_from_row(row) for row in refreshed]


def _sync_shared_ptt_policies_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    rows = _fetchall_dicts(
        conn,
        """
        SELECT id, name, enabled, device_class, ptt_group
          FROM device_profiles
         WHERE enabled=1
      ORDER BY id ASC
        """
    )
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        device = dict(row)
        if _coerce_text(device.get("device_class", "tx_rx"), "tx_rx").lower() == "observer":
            continue
        group = normalize_ptt_group(device.get("ptt_group", ""))
        if not group:
            continue
        groups.setdefault(group, []).append(device)

    expected: Dict[tuple[int, int], Dict[str, Any]] = {}
    for group, members in groups.items():
        sorted_members = sorted(members, key=lambda item: (str(item.get("name", "")).lower(), int(item.get("id", 0) or 0)))
        for idx, left in enumerate(sorted_members):
            left_id = int(left.get("id", 0) or 0)
            left_name = _coerce_text(left.get("name", f"Device {left_id}"), f"Device {left_id}")
            for right in sorted_members[idx + 1 :]:
                right_id = int(right.get("id", 0) or 0)
                right_name = _coerce_text(right.get("name", f"Device {right_id}"), f"Device {right_id}")
                source_id, target_id = sorted((left_id, right_id))
                name_a, name_b = (left_name, right_name) if source_id == left_id else (right_name, left_name)
                expected[(source_id, target_id)] = {
                    "name": f"Shared PTT {group}: {name_a} <-> {name_b}",
                    "enabled": 1,
                    "policy_type": SHARED_PTT_POLICY_TYPE,
                    "source_device_id": source_id,
                    "target_device_id": target_id,
                    "priority": SHARED_PTT_POLICY_PRIORITY,
                    "trigger_json": _coerce_json_object_text({"ptt_group": group}),
                    "action_json": _coerce_json_object_text(
                        {"interlock": "block_primary_frequency_control", "scope": "primary_runtime"}
                    ),
                    "safety_mode": "auto",
                }
    return _sync_pair_coordination_policies_conn(
        conn,
        policy_type=SHARED_PTT_POLICY_TYPE,
        expected=expected,
    )


def _sync_rf_conflict_policies_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    rows = _fetchall_dicts(
        conn,
        """
        SELECT id, name, enabled, device_class, antenna_group, frontend_group, amplifier_group,
               band_overlap_guard_group, band_overlap_guard_mode,
               advanced_frequency_guard_group, advanced_frequency_guard_mode,
               advanced_frequency_guard_window_hz
          FROM device_profiles
         WHERE enabled=1
      ORDER BY id ASC
        """,
    )
    devices: List[Dict[str, Any]] = []
    transmit_devices: List[Dict[str, Any]] = []
    overlap_devices_by_id: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        device = dict(row)
        device["name"] = _coerce_text(device.get("name", f"Device {int(device.get('id', 0) or 0)}"))
        device["antenna_group"] = normalize_resource_group(device.get("antenna_group", ""))
        device["frontend_group"] = normalize_resource_group(device.get("frontend_group", ""))
        device["amplifier_group"] = normalize_resource_group(device.get("amplifier_group", ""))
        device["band_overlap_guard_group"] = normalize_resource_group(device.get("band_overlap_guard_group", ""))
        device["band_overlap_guard_mode"] = normalize_rf_guard_mode(device.get("band_overlap_guard_mode", "warn"))
        device["advanced_frequency_guard_group"] = normalize_resource_group(device.get("advanced_frequency_guard_group", ""))
        device["advanced_frequency_guard_mode"] = normalize_rf_guard_mode(
            device.get("advanced_frequency_guard_mode", "warn")
        )
        device["advanced_frequency_guard_window_hz"] = _coerce_nonnegative_int(
            device.get("advanced_frequency_guard_window_hz", 0)
        )
        if device["band_overlap_guard_group"] or (
            device["advanced_frequency_guard_group"] and device["advanced_frequency_guard_window_hz"] > 0
        ):
            overlap_devices_by_id[int(device.get("id", 0) or 0)] = device
        devices.append(device)
        if _coerce_text(device.get("device_class", "tx_rx"), "tx_rx").lower() != "observer":
            transmit_devices.append(device)

    groups_by_field: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "antenna_group": {},
        "frontend_group": {},
        "amplifier_group": {},
        "band_overlap_guard_group": {},
        "advanced_frequency_guard_group": {},
    }
    # Receiver Guard shares the same central coordination-policy boundary as
    # RF Guard.  Observer antenna/front-end claims must therefore participate
    # in the same pair graph; excluding observers here would persist a guard
    # that the scheduler could never enforce.  Amplifiers remain a transmit-
    # radio resource and are deliberately not inferred for observers.
    for device in devices:
        for field_name in ("antenna_group", "frontend_group"):
            group_value = str(device.get(field_name, "") or "").strip()
            if not group_value:
                continue
            groups_by_field[field_name].setdefault(group_value, []).append(device)
    for device in transmit_devices:
        group_value = str(device.get("amplifier_group", "") or "").strip()
        if group_value:
            groups_by_field["amplifier_group"].setdefault(group_value, []).append(device)
    for device in overlap_devices_by_id.values():
        group_value = str(device.get("band_overlap_guard_group", "") or "").strip()
        if group_value:
            groups_by_field["band_overlap_guard_group"].setdefault(group_value, []).append(device)
        advanced_group_value = str(device.get("advanced_frequency_guard_group", "") or "").strip()
        if advanced_group_value and int(device.get("advanced_frequency_guard_window_hz", 0) or 0) > 0:
            groups_by_field["advanced_frequency_guard_group"].setdefault(advanced_group_value, []).append(device)

    pair_map: Dict[tuple[int, int], Dict[str, Any]] = {}
    group_columns = (
        ("antenna_group", "antenna_groups"),
        ("amplifier_group", "amplifier_groups"),
        ("frontend_group", "frontend_groups"),
        ("band_overlap_guard_group", "band_overlap_groups"),
        ("advanced_frequency_guard_group", "advanced_frequency_groups"),
    )
    for field_name, trigger_key in group_columns:
        for group_name, members in groups_by_field[field_name].items():
            sorted_members = sorted(
                members,
                key=lambda item: (str(item.get("name", "")).lower(), int(item.get("id", 0) or 0)),
            )
            for idx, left in enumerate(sorted_members):
                left_id = int(left.get("id", 0) or 0)
                left_name = _coerce_text(left.get("name", f"Device {left_id}"), f"Device {left_id}")
                for right in sorted_members[idx + 1 :]:
                    right_id = int(right.get("id", 0) or 0)
                    right_name = _coerce_text(right.get("name", f"Device {right_id}"), f"Device {right_id}")
                    source_id, target_id = sorted((left_id, right_id))
                    source_name, target_name = (
                        (left_name, right_name) if source_id == left_id else (right_name, left_name)
                    )
                    pair = (source_id, target_id)
                    pair_entry = pair_map.setdefault(
                        pair,
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "source_name": source_name,
                            "target_name": target_name,
                            "antenna_groups": set(),
                            "frontend_groups": set(),
                            "amplifier_groups": set(),
                            "band_overlap_groups": set(),
                            "advanced_frequency_groups": set(),
                            "guard_modes": set(),
                            "advanced_frequency_windows_hz": {},
                            "observer_ids": set(),
                        },
                    )
                    for member in (left, right):
                        if _is_observer_device_class(member):
                            pair_entry["observer_ids"].add(int(member.get("id", 0) or 0))
                    pair_entry[str(trigger_key)].add(group_name)
                    if field_name == "band_overlap_guard_group":
                        pair_entry["guard_modes"].add(left.get("band_overlap_guard_mode", "warn"))
                        pair_entry["guard_modes"].add(right.get("band_overlap_guard_mode", "warn"))
                    if field_name == "advanced_frequency_guard_group":
                        pair_entry["guard_modes"].add(left.get("advanced_frequency_guard_mode", "warn"))
                        pair_entry["guard_modes"].add(right.get("advanced_frequency_guard_mode", "warn"))
                        pair_entry["advanced_frequency_windows_hz"][str(left_id)] = _coerce_nonnegative_int(
                            left.get("advanced_frequency_guard_window_hz", 0)
                        )
                        pair_entry["advanced_frequency_windows_hz"][str(right_id)] = _coerce_nonnegative_int(
                            right.get("advanced_frequency_guard_window_hz", 0)
                        )

    expected: Dict[tuple[int, int], Dict[str, Any]] = {}
    for pair, info in pair_map.items():
        trigger = {
            "antenna_groups": sorted(str(group) for group in info["antenna_groups"]),
            "frontend_groups": sorted(str(group) for group in info["frontend_groups"]),
            "amplifier_groups": sorted(str(group) for group in info["amplifier_groups"]),
            "band_overlap_groups": sorted(str(group) for group in info["band_overlap_groups"]),
            "advanced_frequency_groups": sorted(str(group) for group in info["advanced_frequency_groups"]),
            "advanced_frequency_windows_hz": {
                str(key): int(value)
                for key, value in sorted(dict(info.get("advanced_frequency_windows_hz", {})).items())
                if int(value or 0) > 0
            },
            "guard_modes": sorted(str(mode) for mode in info["guard_modes"]),
            "receiver_guard": bool(
                info.get("observer_ids")
                and (info.get("antenna_groups") or info.get("frontend_groups"))
            ),
        }
        if not any(value for key, value in trigger.items() if key not in {"guard_modes", "advanced_frequency_windows_hz"}):
            continue
        guard_modes = {normalize_rf_guard_mode(mode) for mode in trigger.get("guard_modes", [])}
        if trigger.get("receiver_guard"):
            # An automatic observer retune cannot stop for an interactive RF
            # prompt.  A declared shared receive resource is therefore a hard
            # hold until the peer evidence is clear; unrelated radios remain
            # outside this pair-scoped policy.
            safety_mode = "block"
        elif trigger.get("band_overlap_groups") or trigger.get("advanced_frequency_groups"):
            safety_mode = "warn"
            for mode in guard_modes:
                safety_mode = stricter_rf_guard_mode(safety_mode, mode)
        else:
            safety_mode = "prompt"
        expected[pair] = {
            "name": f"RF Conflict: {info['source_name']} <-> {info['target_name']}",
            "enabled": 1,
            "policy_type": RF_CONFLICT_POLICY_TYPE,
            "source_device_id": int(info["source_id"]),
            "target_device_id": int(info["target_id"]),
            "priority": RF_CONFLICT_POLICY_PRIORITY,
            "trigger_json": _coerce_json_object_text(trigger),
            "action_json": _coerce_json_object_text(
                {"warning": "primary_runtime_rf_overlap", "scope": "primary_runtime", "guard_mode": safety_mode}
            ),
            "safety_mode": safety_mode,
        }
    return _sync_pair_coordination_policies_conn(
        conn,
        policy_type=RF_CONFLICT_POLICY_TYPE,
        expected=expected,
    )


def _sync_sdr_follow_policies_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    rows = _fetchall_dicts(
        conn,
        """
        SELECT id, name, enabled, device_class, antenna_group, frontend_group, sdr_host, sdr_port
          FROM device_profiles
         WHERE enabled=1
      ORDER BY id ASC
        """,
    )
    observers: List[Dict[str, Any]] = []
    transceivers: List[Dict[str, Any]] = []
    for row in rows:
        device = dict(row)
        device["name"] = _coerce_text(device.get("name", f"Device {int(device.get('id', 0) or 0)}"))
        device["antenna_group"] = normalize_resource_group(device.get("antenna_group", ""))
        device["frontend_group"] = normalize_resource_group(device.get("frontend_group", ""))
        if _is_observer_device_class(device):
            observers.append(device)
        else:
            transceivers.append(device)

    expected: Dict[tuple[int, int], Dict[str, Any]] = {}
    for source in transceivers:
        source_id = int(source.get("id", 0) or 0)
        if source_id <= 0:
            continue
        source_name = _coerce_text(source.get("name", f"Device {source_id}"), f"Device {source_id}")
        source_antenna = str(source.get("antenna_group", "") or "").strip()
        source_frontend = str(source.get("frontend_group", "") or "").strip()
        for observer in observers:
            target_id = int(observer.get("id", 0) or 0)
            if target_id <= 0 or target_id == source_id:
                continue
            observer_name = _coerce_text(observer.get("name", f"Device {target_id}"), f"Device {target_id}")
            observer_antenna = str(observer.get("antenna_group", "") or "").strip()
            observer_frontend = str(observer.get("frontend_group", "") or "").strip()
            expected[(source_id, target_id)] = {
                "name": f"SDR Follow: {source_name} -> {observer_name}",
                "enabled": 1,
                "policy_type": SDR_FOLLOW_POLICY_TYPE,
                "source_device_id": source_id,
                "target_device_id": target_id,
                "priority": SDR_FOLLOW_POLICY_PRIORITY,
                "trigger_json": _coerce_json_object_text(
                    {
                        "source_scope": "primary_runtime",
                        "shared_antenna_groups": [source_antenna] if source_antenna and source_antenna == observer_antenna else [],
                        "shared_frontend_groups": [source_frontend] if source_frontend and source_frontend == observer_frontend else [],
                    }
                ),
                "action_json": _coerce_json_object_text(
                    {
                        "guidance": "observer_follow_advisory",
                        "park_strategy": "alternate_preferred_band",
                        "fallback": "follow_primary_band",
                    }
                ),
                "safety_mode": "warn",
            }
    return _sync_pair_coordination_policies_conn(
        conn,
        policy_type=SDR_FOLLOW_POLICY_TYPE,
        expected=expected,
    )


def _sync_gateway_exclusive_policies_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    expected: Dict[tuple[int, int], Dict[str, Any]] = {}
    for cluster in _list_varac_clusters_conn(conn):
        cluster_db_id = int(cluster.get("id", 0) or 0)
        gateway_id = (
            int(cluster.get("gateway_handler_device_id", 0) or 0)
            if cluster.get("gateway_handler_device_id") not in (None, "")
            else 0
        )
        if cluster_db_id <= 0 or gateway_id <= 0:
            continue
        gateway_row = next(
            (
                row
                for row in _list_varac_cluster_members_conn(conn, cluster_id=cluster_db_id)
                if int(row.get("device_profile_id", 0) or 0) == gateway_id and int(row.get("enabled", 1) or 0) == 1
            ),
            None,
        )
        if not gateway_row:
            continue
        gateway_name = _coerce_text(gateway_row.get("device_name", f"Device {gateway_id}"), f"Device {gateway_id}")
        cluster_name = _coerce_text(cluster.get("name", f"Cluster {cluster_db_id}"), f"Cluster {cluster_db_id}")
        cluster_public_id = _coerce_text(cluster.get("cluster_id", ""), "")
        for member in _list_varac_cluster_members_conn(conn, cluster_id=cluster_db_id):
            member_id = int(member.get("device_profile_id", 0) or 0)
            if member_id <= 0 or member_id == gateway_id or int(member.get("enabled", 1) or 0) != 1:
                continue
            member_name = _coerce_text(member.get("device_name", f"Device {member_id}"), f"Device {member_id}")
            expected[(gateway_id, member_id)] = {
                "name": f"Gateway Exclusive: {cluster_name} {gateway_name} -> {member_name}",
                "enabled": 1,
                "policy_type": GATEWAY_EXCLUSIVE_POLICY_TYPE,
                "source_device_id": gateway_id,
                "target_device_id": member_id,
                "priority": GATEWAY_EXCLUSIVE_POLICY_PRIORITY,
                "trigger_json": _coerce_json_object_text(
                    {
                        "cluster_db_id": cluster_db_id,
                        "cluster_id": cluster_public_id,
                        "cluster_name": cluster_name,
                    }
                ),
                "action_json": _coerce_json_object_text(
                    {
                        "interlock": "gateway_handler_exclusive",
                        "cluster_db_id": cluster_db_id,
                        "cluster_id": cluster_public_id,
                        "cluster_name": cluster_name,
                        "gateway_handler_device_id": gateway_id,
                        "gateway_handler_name": gateway_name,
                        "source_instance_number": int(gateway_row.get("instance_number", 0) or 0),
                        "target_instance_number": int(member.get("instance_number", 0) or 0),
                    }
                ),
                "safety_mode": "warn",
            }
    return _sync_pair_coordination_policies_conn(
        conn,
        policy_type=GATEWAY_EXCLUSIVE_POLICY_TYPE,
        expected=expected,
    )


def _sync_derived_coordination_policies_conn(conn: sqlite3.Connection) -> None:
    _sync_varac_cluster_member_enabled_flags_conn(conn)
    _sync_shared_ptt_policies_conn(conn)
    _sync_rf_conflict_policies_conn(conn)
    _sync_sdr_follow_policies_conn(conn)
    _sync_gateway_exclusive_policies_conn(conn)


def ensure_multi_radio_settings_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    for table_name, spec in SETTINGS_TABLE_SPECS.items():
        conn.execute(str(spec["ddl"]))
        existing = _table_columns(conn, table_name)
        for column_name, column_type in dict(spec["columns"]).items():
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        for index_sql in tuple(spec["indexes"]):
            conn.execute(str(index_sql))
    conn.execute(
        """
        INSERT INTO kv(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (RADIO_HARDWARE_SCHEMA_VERSION_KEY, json.dumps(HARDWARE_BINDING_SCHEMA_VERSION)),
    )
    from freqinout.core.operator_schedule_authorization import ensure_schedule_authorization_schema
    ensure_schedule_authorization_schema(conn)
    from freqinout.core.schedule_approval_sources import install_source_revisions
    install_source_revisions(conn)
    conn.commit()


def get_multi_rig_migration_version(conn: sqlite3.Connection) -> int:
    ensure_multi_radio_settings_schema(conn)
    cur = conn.execute("SELECT value FROM kv WHERE key=?", (MULTI_RIG_MIGRATION_VERSION_KEY,))
    row = cur.fetchone()
    if row is None:
        return 0
    try:
        return int(json.loads(row[0]))
    except Exception:
        return _coerce_int(row[0], 0)


def is_multi_rig_migration_current(conn: sqlite3.Connection) -> bool:
    return get_multi_rig_migration_version(conn) >= CURRENT_MULTI_RIG_MIGRATION_VERSION


def set_multi_rig_migration_version(
    conn: sqlite3.Connection,
    version: int = CURRENT_MULTI_RIG_MIGRATION_VERSION,
    *,
    summary: Optional[Mapping[str, Any]] = None,
) -> None:
    ensure_multi_radio_settings_schema(conn)
    updates: Dict[str, Any] = {
        MULTI_RIG_MIGRATION_VERSION_KEY: int(version),
        MULTI_RIG_MIGRATION_COMPLETED_AT_KEY: _utc_now_iso(),
    }
    if summary is not None:
        updates[f"{MULTI_RIG_MIGRATION_SUMMARY_PREFIX}{int(version)}"] = dict(summary)
    _write_kv_settings(conn, updates)
    conn.commit()


def set_multi_rig_migration_deferred(conn: sqlite3.Connection, deferred: bool) -> None:
    ensure_multi_radio_settings_schema(conn)
    _write_kv_settings(conn, {MULTI_RIG_MIGRATION_DEFERRED_KEY: bool(deferred)})
    conn.commit()


def get_multi_rig_migration_deferred(conn: sqlite3.Connection) -> bool:
    ensure_multi_radio_settings_schema(conn)
    cur = conn.execute("SELECT value FROM kv WHERE key=?", (MULTI_RIG_MIGRATION_DEFERRED_KEY,))
    row = cur.fetchone()
    if row is None:
        return False
    try:
        value = json.loads(row[0])
    except Exception:
        value = row[0]
    return bool(_coerce_bool_int(value, False))


def detect_existing_fio_usage(
    conn: sqlite3.Connection,
    settings_values: Mapping[str, Any],
    *,
    legacy_config_exists: bool = False,
) -> bool:
    ensure_multi_radio_settings_schema(conn)
    if legacy_config_exists:
        return True
    ignored_keys = set(FIO_EXISTING_USE_IGNORED_KEYS) | {MULTI_RIG_MIGRATION_COMPLETED_AT_KEY}
    meaningful_keys = {
        str(key)
        for key in settings_values.keys()
        if str(key) not in ignored_keys and not str(key).startswith(MULTI_RIG_MIGRATION_SUMMARY_PREFIX)
    }
    if meaningful_keys:
        return True
    for table in ("device_profiles", "operating_profiles", "js8_instances", "fast_light_configs", "varac_nodes"):
        try:
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except Exception:
            row = None
        if row is not None:
            return True
    return False


def _record_by_id(conn: sqlite3.Connection, table: str, record_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute(f"SELECT * FROM {table} WHERE id=?", (int(record_id),))
    return _fetchone_dict(cur)


def _record_by_system_key(conn: sqlite3.Connection, table: str, system_key: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute(f"SELECT * FROM {table} WHERE system_key=?", (str(system_key),))
    return _fetchone_dict(cur)


def _save_simple_record(
    conn: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
    *,
    default_system_key: str,
    default_name: str,
    fields: Iterable[str],
) -> Dict[str, Any]:
    payload = dict(values)
    record_id = _coerce_optional_int(payload.get("id"))
    existing = _record_by_id(conn, table, record_id) if record_id is not None else None
    now_iso = _utc_now_iso()
    system_key = _next_system_key(
        conn,
        table,
        payload.get("system_key", (existing or {}).get("system_key", default_system_key)),
        exclude_id=record_id,
    )
    record: Dict[str, Any] = {
        "system_key": system_key,
        "name": _coerce_text(payload.get("name", (existing or {}).get("name", default_name)), default_name) or default_name,
        "enabled": _coerce_bool_int(payload.get("enabled", (existing or {}).get("enabled", 1)), True),
        "created_utc": (existing or {}).get("created_utc", now_iso),
        "updated_utc": now_iso,
    }
    for field in fields:
        default_value = (existing or {}).get(field)
        if field.endswith("_port") or field == "offset_hz":
            record[field] = _coerce_optional_int(payload.get(field, default_value), default_value)
        else:
            record[field] = payload.get(field, default_value)

    columns = list(record.keys())
    if existing:
        assignments = ", ".join(f"{name}=?" for name in columns)
        params = [record[name] for name in columns] + [int(record_id)]
        conn.execute(f"UPDATE {table} SET {assignments} WHERE id=?", params)
        return _record_by_id(conn, table, int(record_id)) or {}

    placeholders = ", ".join(["?"] * len(columns))
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [record[name] for name in columns],
    )
    return _record_by_system_key(conn, table, system_key) or {}


def _save_js8_instance_conn(conn: sqlite3.Connection, values: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(values)
    payload.setdefault("host", "127.0.0.1")
    payload.setdefault("port", 2442)
    record_id = _coerce_optional_int(payload.get("id"))
    existing = _record_by_id(conn, "js8_instances", record_id) if record_id is not None else None
    if not existing:
        payload.setdefault("variant_family", "unknown")
        payload.setdefault("storage_mode", "unverified")
    if "offset_hz" in payload or not existing:
        payload["offset_hz"] = coerce_js8_offset_hz(payload.get("offset_hz"))
    if "variant_family" in payload or not existing:
        payload["variant_family"] = normalize_variant_family(
            payload.get("variant_family", "unknown"), payload.get("variant_version", "")
        )
    if "rig_name" in payload:
        payload["rig_name"] = normalize_rig_name(payload.get("rig_name"))
    if "storage_mode" in payload:
        storage_mode = _coerce_text(payload.get("storage_mode", "unverified"), "unverified").lower()
        if storage_mode not in STORAGE_MODES:
            raise ValueError(f"Unsupported JS8 message storage mode: {storage_mode}")
        payload["storage_mode"] = storage_mode
    if "application_data_root" in payload:
        payload["application_data_root"] = canonicalize_storage_path(payload.get("application_data_root"))
    return _save_simple_record(
        conn,
        "js8_instances",
        payload,
        default_system_key=DEFAULT_JS8_INSTANCE_SYSTEM_KEY,
        default_name=DEFAULT_JS8_INSTANCE_NAME,
        fields=(
            "host",
            "port",
            "offset_hz",
            "profile_path",
            "directed_path",
            "inbox_path",
            "forms_path",
            "install_path",
            "variant_family",
            "variant_version",
            "rig_name",
            "rig_name_source",
            "application_data_root",
            "all_path",
            "save_dir",
            "storage_mode",
            "storage_verified_utc",
            "storage_evidence",
            "spotter_launch_path",
            "commstat_launch_path",
        ),
    )


def _save_fast_light_config_conn(conn: sqlite3.Connection, values: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(values)
    payload.setdefault("flrig_host", "127.0.0.1")
    payload.setdefault("flrig_port", 12345)
    payload.setdefault("fldigi_port", 7362)
    return _save_simple_record(
        conn,
        "fast_light_configs",
        payload,
        default_system_key=DEFAULT_FAST_LIGHT_SYSTEM_KEY,
        default_name=DEFAULT_FAST_LIGHT_NAME,
        fields=(
            "flrig_path",
            "flrig_host",
            "flrig_port",
            "fldigi_path",
            "fldigi_host",
            "fldigi_port",
            "fldigi_log_path",
            "fldigi_checkin_dir",
        ),
    )


def _save_varac_node_conn(conn: sqlite3.Connection, values: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(values or {})
    existing = (
        _record_by_id(conn, "varac_nodes", int(payload["id"]))
        if _coerce_optional_int(payload.get("id")) is not None
        else None
    )
    native_state = _coerce_text(
        payload.get("native_management_state", (existing or {}).get("native_management_state", "operator")),
        "operator",
    ).lower()
    if native_state not in SUPPORTED_VARAC_NATIVE_MANAGEMENT_STATES:
        raise ValueError("Unsupported VarAC native management state.")
    payload["native_management_state"] = native_state
    return _save_simple_record(
        conn,
        "varac_nodes",
        payload,
        default_system_key=DEFAULT_VARAC_NODE_SYSTEM_KEY,
        default_name=DEFAULT_VARAC_NODE_NAME,
        fields=(
            "install_path",
            "db_path",
            "ini_path",
            "vara_runtime_path",
            "vara_ini_path",
            "launch_cmd",
            "incoming_path",
            "native_management_state",
            "native_writer_key",
            "desired_fingerprint",
            "observed_fingerprint",
            "last_native_verified_utc",
            "native_verification_summary",
        ),
    )


def _validate_software_application_claims_conn(
    conn: sqlite3.Connection,
    family_key: str,
    values: Mapping[str, Any],
    *,
    allowed_varac_shared_db_path: str = "",
    reviewed_existing_id: Optional[int] = None,
) -> None:
    """Reject collisions in legacy application rows that predate manifests."""

    payload = dict(values or {})
    record_id = _coerce_optional_int(payload.get("id"))
    family = str(family_key or "").strip().lower()
    if family == "js8call":
        host = _coerce_text(payload.get("host", "127.0.0.1"), "127.0.0.1").casefold()
        host_key = '127.0.0.1' if host == 'localhost' else host
        port = _coerce_int(payload.get("port", 2442), 2442)
        prior = _record_by_id(conn, 'js8_instances', int(reviewed_existing_id)) if reviewed_existing_id else None
        same_claim = bool(prior and record_id == reviewed_existing_id
                          and host_key == ('127.0.0.1' if str(prior.get('host') or '127.0.0.1').strip().lower() == 'localhost' else str(prior.get('host') or '127.0.0.1').strip().lower())
                          and port == int(prior.get('port') or 2442))
        row = conn.execute(
            """
            SELECT name FROM js8_instances
             WHERE (CASE WHEN LOWER(TRIM(COALESCE(host, '127.0.0.1')))='localhost'
                         THEN '127.0.0.1' ELSE LOWER(TRIM(COALESCE(host, '127.0.0.1'))) END)=? AND port=?
               AND (enabled<>0 OR EXISTS (SELECT 1 FROM device_profiles d WHERE d.js8_instance_id=js8_instances.id))
               AND (? IS NULL OR id<>?) LIMIT 1
            """,
            (host_key, port, record_id, record_id),
        ).fetchone()
        if same_claim:
            # Retained unassigned rows cannot veto review of an unchanged
            # endpoint already owned by this radio. Another radio still can.
            row = conn.execute('''SELECT j.name FROM js8_instances j
                JOIN device_profiles d ON d.js8_instance_id=j.id
                WHERE j.id<>? AND
                (CASE WHEN LOWER(TRIM(COALESCE(j.host,'127.0.0.1')))='localhost' THEN '127.0.0.1'
                 ELSE LOWER(TRIM(COALESCE(j.host,'127.0.0.1'))) END)=? AND j.port=? LIMIT 1''',
                (record_id, host_key, port)).fetchone()
        if row is not None:
            raise ValueError(f"JS8Call TCP endpoint {host}:{port} is already used by {row[0]}.")
        return
    if family == "fast_light":
        claims = (
            (
                "FLRig",
                _coerce_text(payload.get("flrig_host", "127.0.0.1"), "127.0.0.1").casefold(),
                _coerce_int(payload.get("flrig_port", 12345), 12345),
                "flrig_host",
                "flrig_port",
            ),
            (
                "FLDigi",
                _coerce_text(
                    payload.get("fldigi_host", payload.get("flrig_host", "127.0.0.1")),
                    "127.0.0.1",
                ).casefold(),
                _coerce_int(payload.get("fldigi_port", 7362), 7362),
                "fldigi_host",
                "fldigi_port",
            ),
        )
        normalized_endpoints = [('127.0.0.1' if host == 'localhost' else host, port)
                                for _, host, port, _, _ in claims]
        if normalized_endpoints[0] == normalized_endpoints[1]:
            raise ValueError("FLRig and FLDigi must use different local TCP endpoints.")
        for label, host, port, host_column, port_column in claims:
            host_key = '127.0.0.1' if host == 'localhost' else host
            prior = _record_by_id(conn, 'fast_light_configs', int(reviewed_existing_id)) if reviewed_existing_id else None
            same_claim = bool(prior and record_id == reviewed_existing_id
                              and host_key == ('127.0.0.1' if str(prior.get(host_column) or '127.0.0.1').strip().lower() == 'localhost' else str(prior.get(host_column) or '127.0.0.1').strip().lower())
                              and port == int(prior.get(port_column) or (12345 if host_column == 'flrig_host' else 7362)))
            row = conn.execute(
                f"""
                SELECT name FROM fast_light_configs
                 WHERE (CASE WHEN LOWER(TRIM(COALESCE({host_column}, '127.0.0.1')))='localhost'
                             THEN '127.0.0.1' ELSE LOWER(TRIM(COALESCE({host_column}, '127.0.0.1'))) END)=? AND {port_column}=?
                   AND (enabled<>0 OR EXISTS (SELECT 1 FROM device_profiles d WHERE d.fast_light_config_id=fast_light_configs.id))
                   AND (? IS NULL OR id<>?) LIMIT 1
                """,
                (host_key, port, record_id, record_id),
            ).fetchone()
            if same_claim:
                row = conn.execute(f'''SELECT a.name FROM fast_light_configs a
                    JOIN device_profiles d ON d.fast_light_config_id=a.id
                    WHERE a.id<>? AND
                    (CASE WHEN LOWER(TRIM(COALESCE(a.{host_column},'127.0.0.1')))='localhost' THEN '127.0.0.1'
                     ELSE LOWER(TRIM(COALESCE(a.{host_column},'127.0.0.1'))) END)=? AND a.{port_column}=? LIMIT 1''',
                    (record_id, host_key, port)).fetchone()
            if row is not None:
                raise ValueError(f"{label} endpoint {host}:{port} is already used by {row[0]}.")
        return
    if family == "varac":
        for label, column, value in (
            ("VarAC configuration", "ini_path", payload.get("ini_path")),
            ("VarAC database", "db_path", payload.get("db_path")),
            ("VarAC incoming folder", "incoming_path", payload.get("incoming_path")),
        ):
            path = _coerce_text(value, "")
            if not path:
                continue
            if column == "db_path" and allowed_varac_shared_db_path:
                if normalize_varac_path(path, label) == normalize_varac_path(
                    allowed_varac_shared_db_path,
                    "VarAC shared database path",
                ):
                    # Native cluster members intentionally share one effective
                    # database.  The caller separately proves that it belongs
                    # to this selected/new cluster, so legacy node uniqueness
                    # must not reject the reviewed cluster contract.
                    continue
            row = conn.execute(
                f"SELECT name FROM varac_nodes WHERE {column}=? AND (? IS NULL OR id<>?) "
                "AND (enabled<>0 OR EXISTS (SELECT 1 FROM device_profiles d WHERE d.varac_node_id=varac_nodes.id)) LIMIT 1",
                (path, record_id, record_id),
            ).fetchone()
            if row is not None:
                raise ValueError(f"{label} is already assigned to {row[0]}.")


def _software_instance_manifest_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    for source_key, target_key, fallback in (
        ("ports_json", "ports", []),
        ("resource_claims_json", "resource_claims", []),
        ("evidence_json", "evidence", {}),
    ):
        try:
            parsed = json.loads(str(data.get(source_key, "") or ""))
        except (TypeError, ValueError):
            parsed = fallback
        data[target_key] = parsed
    return data


def _list_software_instance_manifests_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM software_instance_manifests ORDER BY family_key, instance_key"
    ).fetchall()
    return [_software_instance_manifest_row(dict(row)) for row in rows]


_VARAC_CLUSTER_SHARED_RESOURCE_KINDS = frozenset(
    {
        "varac_executable",
        "working_directory",
        "varac_database",
        "varac_bbs",
        "varac_bbs_archive",
    }
)
_VARAC_MEMBER_EXCLUSIVE_RESOURCE_KINDS = frozenset(
    {
        "varac_ini",
        "vara_runtime",
        "vara_ini",
        "varac_incoming",
        "varac_outbox",
        "varac_cluster_instance",
    }
)


def _scope_varac_cluster_manifest(
    values: Mapping[str, Any],
    application_values: Mapping[str, Any],
    *,
    cluster_id: str,
    instance_number: Optional[int],
    shared_db_path: str,
    shared_bbs_path: str,
    shared_bbs_archive_path: str,
) -> Dict[str, Any]:
    """Return one canonical VarAC member manifest for a reviewed cluster.

    A VarAC cluster intentionally shares the installed program, its install
    working directory, database, and BBS publication roots.  The native INI,
    cloned VARA runtime, mailboxes, endpoint ports, and member number remain
    member-owned.  Rebuilding these known claims at the persistence boundary
    prevents an older standalone manifest from vetoing a safe cluster
    conversion merely because it described shared infrastructure as exclusive.
    Unknown claims are preserved exactly so this normalization cannot silently
    weaken a future resource contract.
    """

    payload = dict(values or {})
    claims_by_kind: Dict[str, Dict[str, Any]] = {}
    for raw in payload.get("resource_claims", payload.get("resource_claims_json", ())) or ():
        if not isinstance(raw, Mapping):
            continue
        kind = _coerce_text(raw.get("kind", ""), "").lower()
        value = _coerce_text(raw.get("value", ""), "")
        if kind and value:
            claims_by_kind[kind] = {
                "kind": kind,
                "value": value,
                "exclusive": bool(raw.get("exclusive", True)),
            }

    executable = _coerce_text(payload.get("executable_path", ""), "") or _coerce_text(
        claims_by_kind.get("varac_executable", {}).get("value", ""), ""
    ) or _coerce_text(application_values.get("install_path", ""), "")
    known_values = {
        "varac_executable": executable,
        "varac_database": _coerce_text(shared_db_path, ""),
        "varac_bbs": _coerce_text(shared_bbs_path, ""),
        "varac_bbs_archive": _coerce_text(shared_bbs_archive_path, ""),
        "varac_ini": _coerce_text(application_values.get("ini_path", ""), ""),
        "vara_runtime": _coerce_text(application_values.get("vara_runtime_path", ""), ""),
        "vara_ini": _coerce_text(application_values.get("vara_ini_path", ""), ""),
        "varac_incoming": _coerce_text(application_values.get("incoming_path", ""), ""),
        "varac_outbox": _coerce_text(application_values.get("outbox_path", ""), ""),
    }
    for kind, value in known_values.items():
        if value:
            claims_by_kind[kind] = {
                "kind": kind,
                "value": value,
                "exclusive": kind in _VARAC_MEMBER_EXCLUSIVE_RESOURCE_KINDS,
            }

    # Working directory is supplied by the reviewed launch identity.  Keep its
    # exact spelling (including Wine paths) and only correct its ownership.
    working_directory = claims_by_kind.get("working_directory", {}).get("value", "")
    if working_directory:
        claims_by_kind["working_directory"] = {
            "kind": "working_directory",
            "value": str(working_directory),
            "exclusive": False,
        }
    member_number = int(instance_number or 0)
    public_cluster_id = _coerce_text(cluster_id, "")
    if public_cluster_id and member_number > 0:
        claims_by_kind["varac_cluster_instance"] = {
            "kind": "varac_cluster_instance",
            "value": f"{public_cluster_id}:{member_number}",
            "exclusive": True,
        }

    for kind in _VARAC_CLUSTER_SHARED_RESOURCE_KINDS:
        if kind in claims_by_kind:
            claims_by_kind[kind]["exclusive"] = False
    for kind in _VARAC_MEMBER_EXCLUSIVE_RESOURCE_KINDS:
        if kind in claims_by_kind:
            claims_by_kind[kind]["exclusive"] = True
    payload["resource_claims"] = list(claims_by_kind.values())
    payload.pop("resource_claims_json", None)
    return payload


def _save_software_instance_manifest_conn(
    conn: sqlite3.Connection,
    values: Mapping[str, Any],
) -> Dict[str, Any]:
    manifest = manifest_from_mapping(values)
    if not manifest.application_system_key:
        raise ValueError("A software instance manifest must link to a saved application instance.")
    expected_table = {
        "js8call": "js8_instances",
        "fast_light": "fast_light_configs",
        "varac": "varac_nodes",
    }[manifest.family_key]
    linked = _record_by_system_key(conn, expected_table, manifest.application_system_key)
    if linked is None:
        raise ValueError("The linked application instance does not exist.")

    existing = []
    for row in _list_software_instance_manifests_conn(conn):
        candidate = manifest_from_mapping(row)
        candidate_table = {
            "js8call": "js8_instances",
            "fast_light": "fast_light_configs",
            "varac": "varac_nodes",
        }[candidate.family_key]
        candidate_owner = _record_by_system_key(
            conn,
            candidate_table,
            candidate.application_system_key,
        )
        if candidate_owner is None:
            # Interrupted saves from older builds can leave FIO-only manifest
            # evidence after their application row was rolled back or removed.
            # Retain that evidence for diagnostics, but it has no live owner
            # and therefore cannot reserve ports or paths against a reviewed
            # recovery transaction.
            continue
        if not int(candidate_owner.get('enabled', 1) or 0):
            link_column = _SOFTWARE_INSTANCE_ASSIGNMENTS[candidate.family_key][0]
            if conn.execute(f'SELECT 1 FROM device_profiles WHERE {link_column}=? LIMIT 1',
                            (candidate_owner['id'],)).fetchone() is None:
                # Retired unassigned evidence remains available but cannot
                # reserve an endpoint against an explicit reviewed assignment.
                continue
        existing.append(candidate)
    conflicts = find_manifest_conflicts(manifest, existing)
    if conflicts:
        raise ValueError(conflicts[0].message)

    record = manifest_to_record(manifest)
    now_iso = _utc_now_iso()
    prior = conn.execute(
        "SELECT created_utc FROM software_instance_manifests WHERE instance_key=?",
        (manifest.instance_key,),
    ).fetchone()
    record["created_utc"] = str(prior[0]) if prior is not None else now_iso
    record["updated_utc"] = now_iso
    columns = tuple(record.keys())
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "instance_key")
    conn.execute(
        f"""
        INSERT INTO software_instance_manifests ({', '.join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(instance_key) DO UPDATE SET {updates}
        """,
        tuple(record[column] for column in columns),
    )
    saved = conn.execute(
        "SELECT * FROM software_instance_manifests WHERE instance_key=?",
        (manifest.instance_key,),
    ).fetchone()
    return _software_instance_manifest_row(dict(saved)) if saved is not None else {}


_SOFTWARE_INSTANCE_ASSIGNMENTS: Dict[str, tuple[str, str, str]] = {
    "js8call": ("js8_instance_id", "js8_instances", "JS8Call"),
    "fast_light": ("fast_light_config_id", "fast_light_configs", "Fast Light"),
    "varac": ("varac_node_id", "varac_nodes", "VarAC"),
}
_SOFTWARE_INSTANCE_USE_FLAGS: Dict[str, tuple[str, ...]] = {
    "js8call": ("use_js8call",),
    "fast_light": ("use_flrig", "use_fldigi", "use_flmsg", "use_flamp"),
    "varac": ("use_varac",),
}
_SOFTWARE_INSTANCE_EXPECTATION_UNSET = object()
_UNASSIGNED_SOFTWARE_SUMMARY = "Unassigned from all radios; FIO launch disabled."


def _validate_software_instance_radio_ownership_conn(
    conn: sqlite3.Connection,
    *,
    radio_profile_id: Optional[int],
    family_key: str,
    application_id: Optional[int],
) -> None:
    """Reject a new application-to-radio link that would share a runtime.

    This deliberately validates links at their mutation boundaries rather than
    adding a unique database index.  Older installations may already contain
    shared links; unchanged legacy rows remain readable and editable until an
    operator deliberately reassigns them.
    """

    if application_id is None:
        return
    family = str(family_key or "").strip().lower()
    try:
        link_column, _table_name, label = _SOFTWARE_INSTANCE_ASSIGNMENTS[family]
    except KeyError as exc:
        raise ValueError(f"Unsupported software instance family: {family or 'blank'}") from exc
    params: List[Any] = [int(application_id)]
    where = ""
    if radio_profile_id is not None:
        where = " AND id<>?"
        params.append(int(radio_profile_id))
    owner = conn.execute(
        f"SELECT name FROM device_profiles WHERE {link_column}=?{where} ORDER BY id ASC LIMIT 1",
        params,
    ).fetchone()
    if owner is not None:
        raise ValueError(
            f"This {label} runtime instance is already assigned to {owner[0]}; "
            "each independently controlled radio needs its own instance identity."
        )


def _receive_only_manifest_evidence_conn(
    conn: sqlite3.Connection,
    *,
    family_key: str,
    application_id: int,
) -> Dict[str, Any]:
    """Return reviewed receive-only evidence for one saved application row."""

    family = str(family_key or "").strip().lower()
    try:
        _link_column, table_name, _label = _SOFTWARE_INSTANCE_ASSIGNMENTS[family]
    except KeyError:
        return {}
    application = _record_by_id(conn, table_name, int(application_id))
    if application is None:
        return {}
    manifest_row = conn.execute(
        """
        SELECT evidence_json
          FROM software_instance_manifests
         WHERE family_key=? AND application_system_key=?
      ORDER BY updated_utc DESC
         LIMIT 1
        """,
        (family, str(application.get("system_key", "") or "")),
    ).fetchone()
    if manifest_row is None:
        return {}
    try:
        evidence = json.loads(str(manifest_row[0] or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(evidence) if isinstance(evidence, Mapping) else {}


def _validate_observer_software_link_conn(
    conn: sqlite3.Connection,
    *,
    family_key: str,
    application_id: int,
) -> None:
    """Fail closed when a direct observer link lacks reviewed RX-only evidence."""

    family = str(family_key or "").strip().lower()
    if family == "varac":
        raise ValueError("Observer / SDR device profiles cannot use VarAC or VarAC clusters.")
    if family not in {"js8call", "fast_light"}:
        return
    evidence = _receive_only_manifest_evidence_conn(
        conn,
        family_key=family,
        application_id=int(application_id),
    )
    if not (
        bool(evidence.get("receive_only_ingest"))
        and evidence.get("transmit_authority") is False
        and str(evidence.get("execution_scope") or "receive_only").strip().lower() == "receive_only"
    ):
        label = "JS8Call" if family == "js8call" else "Fast Light"
        raise ValueError(
            f"Observer / SDR {label} assignments require reviewed receive-only evidence. "
            "Use the guided Software instance workflow."
        )


def _remove_instance_launch_links_conn(
    conn: sqlite3.Connection,
    *,
    radio_profile_id: int,
    family_key: str,
    application_system_key: str,
) -> None:
    """Remove only launch items that FIO created for one assigned runtime."""

    manifest_rows = conn.execute(
        """
        SELECT instance_key FROM software_instance_manifests
         WHERE family_key=? AND application_system_key=?
        """,
        (str(family_key), str(application_system_key or "")),
    ).fetchall()
    manifest_keys = {str(row[0] or "").strip() for row in manifest_rows if str(row[0] or "").strip()}
    if not manifest_keys:
        # Do not guess at pre-manifest/legacy launch rows.  They can represent
        # an operator-managed shared launcher and must survive this operation.
        return
    item_rows = conn.execute(
        "SELECT instance_key FROM radio_launch_bundle_items WHERE radio_profile_id=?",
        (int(radio_profile_id),),
    ).fetchall()
    stale_keys = [
        str(row[0] or "")
        for row in item_rows
        if any(str(row[0] or "") == key or str(row[0] or "").startswith(f"{key}:") for key in manifest_keys)
    ]
    if stale_keys:
        conn.executemany(
            "DELETE FROM radio_launch_bundle_items WHERE radio_profile_id=? AND instance_key=?",
            [(int(radio_profile_id), key) for key in stale_keys],
        )


_COMMSTAT_SHARED_INSTANCE_KEY = "commstat:station-shared"


def _sync_station_shared_commstat_binding_conn(
    conn: sqlite3.Connection,
    *,
    radio_profile_id: int,
) -> None:
    """Project one radio/JS8 binding onto the shared CommStat launch identity."""

    radio_id = int(radio_profile_id)
    profile = _record_by_id(conn, "device_profiles", radio_id) or {}
    js8_instance_id = _coerce_optional_int(profile.get("js8_instance_id"))
    enabled = bool(_coerce_bool_int(profile.get("use_commstat"), False))
    if not enabled or js8_instance_id is None:
        conn.execute(
            "DELETE FROM radio_launch_bundle_items WHERE radio_profile_id=? AND instance_key=?",
            (radio_id, _COMMSTAT_SHARED_INSTANCE_KEY),
        )
        return

    js8_row = _record_by_id(conn, "js8_instances", int(js8_instance_id)) or {}
    shared_row = conn.execute(
        """
        SELECT command_override, path_override, launch_at_startup
          FROM radio_launch_bundle_items
         WHERE instance_key=? AND radio_profile_id<>?
      ORDER BY launch_at_startup DESC, updated_utc DESC
         LIMIT 1
        """,
        (_COMMSTAT_SHARED_INSTANCE_KEY, radio_id),
    ).fetchone()
    command_override = str(shared_row[0] or "").strip() if shared_row is not None else ""
    path_override = str(shared_row[1] or "").strip() if shared_row is not None else ""
    launch_at_startup = bool(int(shared_row[2] or 0)) if shared_row is not None else False
    if not path_override:
        path_override = str(js8_row.get("commstat_launch_path", "") or "").strip()
    if not path_override:
        discovered = conn.execute(
            """
            SELECT commstat_launch_path FROM js8_instances
             WHERE TRIM(COALESCE(commstat_launch_path, ''))<>''
          ORDER BY updated_utc DESC, id ASC LIMIT 1
            """
        ).fetchone()
        path_override = str(discovered[0] or "").strip() if discovered is not None else ""

    now_iso = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO radio_launch_bundles
            (radio_profile_id, schema_version, launch_enabled, migrated_from_legacy, updated_utc)
        VALUES (?, 1, 0, 0, ?)
        ON CONFLICT(radio_profile_id) DO UPDATE SET updated_utc=excluded.updated_utc
        """,
        (radio_id, now_iso),
    )
    readiness = {
        "kind": "station_process",
        "execution_scope": "station_shared_utility",
        "bound_js8_instance_id": int(js8_instance_id),
        "bound_js8_host": str(js8_row.get("host", "127.0.0.1") or "127.0.0.1"),
        "bound_js8_port": int(js8_row.get("port", 2442) or 2442),
    }
    conn.execute(
        """
        INSERT INTO radio_launch_bundle_items (
            radio_profile_id, instance_key, app_name, display_order, enabled,
            launch_at_startup, monitor_health, command_override, path_override,
            dependencies_json, readiness_json, updated_utc
        ) VALUES (?, ?, 'CommStat', 60, 1, ?, 1, ?, ?, ?, ?, ?)
        ON CONFLICT(radio_profile_id, instance_key) DO UPDATE SET
            app_name=excluded.app_name,
            display_order=excluded.display_order,
            enabled=1,
            command_override=CASE
                WHEN TRIM(excluded.command_override)<>'' THEN excluded.command_override
                ELSE radio_launch_bundle_items.command_override
            END,
            path_override=CASE
                WHEN TRIM(excluded.path_override)<>'' THEN excluded.path_override
                ELSE radio_launch_bundle_items.path_override
            END,
            dependencies_json=excluded.dependencies_json,
            readiness_json=excluded.readiness_json,
            updated_utc=excluded.updated_utc
        """,
        (
            radio_id,
            _COMMSTAT_SHARED_INSTANCE_KEY,
            1 if launch_at_startup else 0,
            command_override,
            path_override,
            json.dumps(["JS8Call"]),
            json.dumps(readiness, sort_keys=True),
            now_iso,
        ),
    )


def _remove_varac_cluster_links_for_device_conn(
    conn: sqlite3.Connection,
    *,
    radio_profile_id: int,
    preserve_cluster_id: Optional[int] = None,
) -> None:
    """Detach a radio from VarAC clusters without touching VarAC application data."""

    params: List[Any] = [_utc_now_iso(), int(radio_profile_id)]
    where = "gateway_handler_device_id=?"
    if preserve_cluster_id is not None:
        where += " AND id<>?"
        params.append(int(preserve_cluster_id))
    conn.execute(
        f"UPDATE varac_clusters SET gateway_handler_device_id=NULL, updated_utc=? WHERE {where}",
        params,
    )
    conn.execute(
        "DELETE FROM varac_cluster_members WHERE device_profile_id=?",
        (int(radio_profile_id),),
    )
    _sync_varac_cluster_member_enabled_flags_conn(conn)


def _disable_unowned_software_application_conn(
    conn: sqlite3.Connection,
    *,
    family_key: str,
    application_id: Optional[int],
) -> None:
    """Keep an orphaned application record, but make it non-operational in FIO."""

    if application_id is None:
        return
    family = str(family_key or "").strip().lower()
    link_column, application_table, _label = _SOFTWARE_INSTANCE_ASSIGNMENTS[family]
    in_use = conn.execute(
        f"SELECT 1 FROM device_profiles WHERE {link_column}=? LIMIT 1",
        (int(application_id),),
    ).fetchone()
    if in_use is not None:
        return
    application = _record_by_id(conn, application_table, int(application_id))
    if application is None:
        return
    now_iso = _utc_now_iso()
    conn.execute(
        f"UPDATE {application_table} SET enabled=0, updated_utc=? WHERE id=?",
        (now_iso, int(application_id)),
    )
    conn.execute(
        """
        UPDATE software_instance_manifests
           SET verification_state='needs_attention', verification_summary=?, updated_utc=?
         WHERE family_key=? AND application_system_key=?
        """,
        (_UNASSIGNED_SOFTWARE_SUMMARY, now_iso, family, str(application.get("system_key", "") or "")),
    )


def _activate_software_application_conn(
    conn: sqlite3.Connection,
    *,
    family_key: str,
    application_id: int,
) -> Dict[str, Any]:
    """Reactivate a retained record once it is deliberately assigned again."""

    family = str(family_key or "").strip().lower()
    _link_column, application_table, _label = _SOFTWARE_INSTANCE_ASSIGNMENTS[family]
    application = _record_by_id(conn, application_table, int(application_id))
    if application is None:
        raise KeyError(f"Unknown {family.replace('_', ' ')} application instance id: {application_id}")
    now_iso = _utc_now_iso()
    conn.execute(
        f"UPDATE {application_table} SET enabled=1, updated_utc=? WHERE id=?",
        (now_iso, int(application_id)),
    )
    conn.execute(
        """
        UPDATE software_instance_manifests
           SET verification_state='configured', verification_summary='', updated_utc=?
         WHERE family_key=? AND application_system_key=? AND verification_summary=?
        """,
        (now_iso, family, str(application.get("system_key", "") or ""), _UNASSIGNED_SOFTWARE_SUMMARY),
    )
    return _record_by_id(conn, application_table, int(application_id)) or application


def _save_operating_profile_conn(conn: sqlite3.Connection, values: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(values)
    record_id = _coerce_optional_int(payload.get("id"))
    existing = _record_by_id(conn, "operating_profiles", record_id) if record_id is not None else None
    now_iso = _utc_now_iso()
    system_key = _next_system_key(
        conn,
        "operating_profiles",
        payload.get("system_key", (existing or {}).get("system_key", DEFAULT_OPERATING_SYSTEM_KEY)),
        exclude_id=record_id,
    )
    scheduler_mode = _coerce_text(payload.get("scheduler_mode", (existing or {}).get("scheduler_mode", "full")), "full").lower() or "full"
    if scheduler_mode not in SUPPORTED_SCHEDULER_MODES:
        scheduler_mode = "full"
    record = {
        "system_key": system_key,
        "name": _coerce_text(payload.get("name", (existing or {}).get("name", DEFAULT_OPERATING_NAME)), DEFAULT_OPERATING_NAME) or DEFAULT_OPERATING_NAME,
        "enabled": _coerce_bool_int(payload.get("enabled", (existing or {}).get("enabled", 1)), True),
        "description": _coerce_text(payload.get("description", (existing or {}).get("description", "")), ""),
        "category": _normalize_frequency_plan_category(payload.get("category", (existing or {}).get("category", "normal"))),
        "status": _normalize_frequency_plan_status(payload.get("status", (existing or {}).get("status", "saved"))),
        "scheduler_enabled": _coerce_bool_int(payload.get("scheduler_enabled", (existing or {}).get("scheduler_enabled", 1)), True),
        "scheduler_mode": scheduler_mode,
        "preferred_band_set_json": _coerce_json_array_text(
            payload.get(
                "preferred_band_set",
                payload.get("preferred_band_set_json", (existing or {}).get("preferred_band_set_json", "[]")),
            )
        ),
        "source_refs_json": _coerce_ref_list_json_text(
            payload.get("source_refs", payload.get("source_refs_json", (existing or {}).get("source_refs_json", "[]")))
        ),
        "schedule_refs_json": _coerce_ref_list_json_text(
            payload.get("schedule_refs", payload.get("schedule_refs_json", (existing or {}).get("schedule_refs_json", "[]")))
        ),
        "frequency_refs_json": _coerce_ref_list_json_text(
            payload.get("frequency_refs", payload.get("frequency_refs_json", (existing or {}).get("frequency_refs_json", "[]")))
        ),
        "group_refs_json": _coerce_ref_list_json_text(
            payload.get("group_refs", payload.get("group_refs_json", (existing or {}).get("group_refs_json", "[]")))
        ),
        "notes": _coerce_text(payload.get("notes", (existing or {}).get("notes", "")), ""),
        "use_messages": _coerce_bool_int(payload.get("use_messages", (existing or {}).get("use_messages", 1)), True),
        "use_map": _coerce_bool_int(payload.get("use_map", (existing or {}).get("use_map", 1)), True),
        "use_background_ingest": _coerce_bool_int(payload.get("use_background_ingest", (existing or {}).get("use_background_ingest", 1)), True),
        "use_launch_control": _coerce_bool_int(payload.get("use_launch_control", (existing or {}).get("use_launch_control", 0)), False),
        "use_net_control_tabs": _coerce_bool_int(payload.get("use_net_control_tabs", (existing or {}).get("use_net_control_tabs", 1)), True),
        "receive_only": _coerce_bool_int(payload.get("receive_only", (existing or {}).get("receive_only", 0)), False),
        "allow_profile_swap": _coerce_bool_int(
            payload.get("allow_profile_swap", (existing or {}).get("allow_profile_swap", 0)),
            False,
        ),
        "created_utc": (existing or {}).get("created_utc", now_iso),
        "updated_utc": now_iso,
    }
    columns = list(record.keys())
    if existing:
        assignments = ", ".join(f"{name}=?" for name in columns)
        conn.execute(
            f"UPDATE operating_profiles SET {assignments} WHERE id=?",
            [record[name] for name in columns] + [int(record_id)],
        )
        conn.commit()
        return _record_by_id(conn, "operating_profiles", int(record_id)) or {}
    conn.execute(
        f"INSERT INTO operating_profiles ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})",
        [record[name] for name in columns],
    )
    conn.commit()
    return _record_by_system_key(conn, "operating_profiles", system_key) or {}


def _save_frequency_plan_conn(conn: sqlite3.Connection, values: Mapping[str, Any]) -> Dict[str, Any]:
    payload = dict(values)
    record_id = _coerce_optional_int(payload.get("id"))
    existing = _record_by_id(conn, "frequency_plans", record_id) if record_id is not None else None
    now_iso = _utc_now_iso()
    system_key = _next_system_key(
        conn,
        "frequency_plans",
        payload.get("system_key", (existing or {}).get("system_key", DEFAULT_FREQUENCY_PLAN_SYSTEM_KEY)),
        exclude_id=record_id,
    )
    record = {
        "system_key": system_key,
        "name": _coerce_text(
            payload.get("name", (existing or {}).get("name", DEFAULT_FREQUENCY_PLAN_NAME)),
            DEFAULT_FREQUENCY_PLAN_NAME,
        )
        or DEFAULT_FREQUENCY_PLAN_NAME,
        "enabled": _coerce_bool_int(payload.get("enabled", (existing or {}).get("enabled", 1)), True),
        "description": _coerce_text(payload.get("description", (existing or {}).get("description", "")), ""),
        "category": _normalize_frequency_plan_category(payload.get("category", (existing or {}).get("category", "normal"))),
        "status": _normalize_frequency_plan_status(payload.get("status", (existing or {}).get("status", "saved"))),
        "receive_only": _coerce_bool_int(payload.get("receive_only", (existing or {}).get("receive_only", 0)), False),
        "source_refs_json": _coerce_ref_list_json_text(
            payload.get("source_refs", payload.get("source_refs_json", (existing or {}).get("source_refs_json", "[]")))
        ),
        "schedule_refs_json": _coerce_ref_list_json_text(
            payload.get("schedule_refs", payload.get("schedule_refs_json", (existing or {}).get("schedule_refs_json", "[]")))
        ),
        "frequency_refs_json": _coerce_ref_list_json_text(
            payload.get("frequency_refs", payload.get("frequency_refs_json", (existing or {}).get("frequency_refs_json", "[]")))
        ),
        "group_refs_json": _coerce_ref_list_json_text(
            payload.get("group_refs", payload.get("group_refs_json", (existing or {}).get("group_refs_json", "[]")))
        ),
        "notes": _coerce_text(payload.get("notes", (existing or {}).get("notes", "")), ""),
        "created_utc": (existing or {}).get("created_utc", now_iso),
        "updated_utc": now_iso,
    }
    columns = list(record.keys())
    if existing:
        assignments = ", ".join(f"{name}=?" for name in columns)
        conn.execute(
            f"UPDATE frequency_plans SET {assignments} WHERE id=?",
            [record[name] for name in columns] + [int(record_id)],
        )
        conn.commit()
        return _record_by_id(conn, "frequency_plans", int(record_id)) or {}
    conn.execute(
        f"INSERT INTO frequency_plans ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})",
        [record[name] for name in columns],
    )
    conn.commit()
    return _record_by_system_key(conn, "frequency_plans", system_key) or {}


def _set_assigned_plan_conn(
    conn: sqlite3.Connection,
    device_profile_id: int,
    frequency_plan_id: int,
    *,
    assignment_state: str = "active",
    assignment_category: str = "normal",
    scheduler_mode: str = "full",
    reason: str = "",
    starts_utc: str = "",
    ends_utc: str = "",
    created_by: str = "settings_ui",
    assignment_plan_overrides: Optional[Mapping[int, int]] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    device = _record_by_id(conn, "device_profiles", int(device_profile_id))
    if not device:
        raise KeyError(f"Unknown device profile id: {device_profile_id}")
    frequency_plan = _record_by_id(conn, "frequency_plans", int(frequency_plan_id))
    if not frequency_plan:
        raise KeyError(f"Unknown Frequency Plan id: {frequency_plan_id}")
    if int(frequency_plan.get("enabled", 1) or 0) != 1:
        raise ValueError("Cannot assign a disabled Frequency Plan.")
    if str(frequency_plan.get("category") or "").strip().lower() in SOURCE_ONLY_FREQUENCY_PLAN_CATEGORIES:
        raise ValueError("Source schedules must be blended into a Frequency Plan before assignment to a radio.")
    _validate_schedule_assignment_compatibility(device, frequency_plan)
    validation = _schedule_assignment_validation_status_conn(
        conn,
        device,
        frequency_plan,
        assignment_plan_overrides=assignment_plan_overrides,
    )
    if validation.get("state") == "blocked":
        if commit:
            conn.commit()
        messages = validation.get("blocked") or validation.get("messages") or ["RF Safety Guard blocked this assignment."]
        raise ValueError(str(messages[0]))
    state = _normalize_assignment_state(assignment_state)
    scheduler_mode_value = _coerce_text(scheduler_mode, "full").lower() or "full"
    if scheduler_mode_value not in SUPPORTED_SCHEDULER_MODES:
        scheduler_mode_value = "full"
    if state in EFFECTIVE_ASSIGNMENT_STATES:
        placeholders = ", ".join(["?"] * len(EFFECTIVE_ASSIGNMENT_STATES))
        conn.execute(
            f"""
            UPDATE assigned_plans
               SET assignment_state='superseded', updated_utc=?
             WHERE device_profile_id=?
               AND assignment_state IN ({placeholders})
            """,
            (_utc_now_iso(), int(device_profile_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
        )
    now_iso = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO assigned_plans (
            device_profile_id, frequency_plan_id, assignment_state, assignment_category,
            scheduler_mode, starts_utc, ends_utc, reason, created_by, validation_status_json,
            created_utc, updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(device_profile_id),
            int(frequency_plan_id),
            state,
            _coerce_text(assignment_category, "normal") or "normal",
            scheduler_mode_value,
            _coerce_text(starts_utc, ""),
            _coerce_text(ends_utc, ""),
            _coerce_text(reason, ""),
            _coerce_text(created_by, "settings_ui") or "settings_ui",
            json.dumps(validation, sort_keys=True),
            now_iso,
            now_iso,
        ),
    )
    if commit:
        conn.commit()
    return _fetchone_dict(conn.execute("SELECT * FROM assigned_plans WHERE id=last_insert_rowid()")) or {}


def _effective_assigned_plan_for_device(conn: sqlite3.Connection, device_profile_id: int) -> Optional[Dict[str, Any]]:
    if int(device_profile_id or 0) <= 0:
        return None
    placeholders = ", ".join(["?"] * len(EFFECTIVE_ASSIGNMENT_STATES))
    cur = conn.execute(
        f"""
        SELECT *
          FROM assigned_plans
         WHERE device_profile_id=?
           AND assignment_state IN ({placeholders})
      ORDER BY id ASC
         LIMIT 1
        """,
        (int(device_profile_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
    )
    assignment = _fetchone_dict(cur)
    if not assignment:
        return None
    device = _record_by_id(conn, "device_profiles", int(device_profile_id)) or {}
    frequency_plan = _record_by_id(conn, "frequency_plans", int(assignment.get("frequency_plan_id", 0) or 0)) or {}
    device_name = _coerce_text(device.get("name", ""), "")
    plan_name = _coerce_text(frequency_plan.get("name", ""), "")
    if device_name:
        assignment.setdefault("device_name", device_name)
        assignment.setdefault("radio_name", device_name)
    if plan_name:
        assignment.setdefault("frequency_plan_name", plan_name)
        assignment.setdefault("plan_name", plan_name)
    return assignment


def _refresh_assigned_plan_validation_for_device_conn(
    conn: sqlite3.Connection,
    device_profile_id: int,
    *,
    emit_events: bool = False,
) -> Optional[Dict[str, Any]]:
    """Recompute stored RF Guard status for the device's effective Frequency Plan."""
    if int(device_profile_id or 0) <= 0:
        return None
    device = _record_by_id(conn, "device_profiles", int(device_profile_id))
    assignment = _effective_assigned_plan_for_device(conn, int(device_profile_id))
    if not device or not assignment:
        return None
    plan_id = int(assignment.get("frequency_plan_id", 0) or 0)
    if plan_id <= 0:
        return None
    frequency_plan = _record_by_id(conn, "frequency_plans", plan_id)
    if not frequency_plan:
        return None
    validation = _schedule_assignment_validation_status_conn(
        conn,
        device,
        frequency_plan,
        emit_events=emit_events,
    )
    conn.execute(
        """
        UPDATE assigned_plans
           SET validation_status_json=?, updated_utc=?
         WHERE id=?
        """,
        (json.dumps(validation, sort_keys=True), _utc_now_iso(), int(assignment.get("id", 0) or 0)),
    )
    refreshed = _effective_assigned_plan_for_device(conn, int(device_profile_id))
    return dict(refreshed) if refreshed else None


def _effective_assignment_for_device(conn: sqlite3.Connection, device_profile_id: int) -> Optional[Dict[str, Any]]:
    if int(device_profile_id or 0) <= 0:
        return None
    placeholders = ", ".join(["?"] * len(EFFECTIVE_ASSIGNMENT_STATES))
    cur = conn.execute(
        f"""
        SELECT a.*, o.name AS operating_profile_name
          FROM operating_profile_assignments a
          JOIN operating_profiles o ON o.id=a.operating_profile_id
         WHERE a.device_profile_id=?
           AND a.assignment_state IN ({placeholders})
      ORDER BY a.id ASC
         LIMIT 1
        """,
        (int(device_profile_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
    )
    return _fetchone_dict(cur)


def _set_device_operating_profile_conn(
    conn: sqlite3.Connection,
    device_profile_id: int,
    operating_profile_id: int,
    *,
    assignment_state: str = "active",
    reason: str = "",
    created_by: str = "settings_ui",
    starts_utc: Optional[str] = None,
    ends_utc: Optional[str] = None,
    allow_active_swap_edit: bool = False,
) -> Dict[str, Any]:
    desired_state = _normalize_assignment_state(assignment_state, "active")
    if desired_state not in EFFECTIVE_ASSIGNMENT_STATES:
        raise ValueError("Only active or temporary override assignments can become the effective radio assignment.")

    device = _record_by_id(conn, "device_profiles", int(device_profile_id))
    if not device:
        raise KeyError(f"Unknown device profile id: {device_profile_id}")
    operating = _record_by_id(conn, "operating_profiles", int(operating_profile_id))
    if not operating:
        raise KeyError(f"Unknown Operating Model id: {operating_profile_id}")
    if int(operating.get("enabled", 1) or 0) != 1:
        raise ValueError("Cannot assign a disabled Operating Model.")
    _validate_assignment_plan_compatibility(device, operating)
    active_swap = _active_profile_swap_policy_conn(conn)
    if active_swap is not None and not allow_active_swap_edit:
        source_id = int(active_swap.get("source_device_id", 0) or 0)
        target_id = int(active_swap.get("target_device_id", 0) or 0)
        if int(device_profile_id) in {source_id, target_id}:
            raise ValueError("Restore the active Temporary Plan Swap before editing assignments on the source/target radios.")

    current = _effective_assignment_for_device(conn, int(device_profile_id))
    now_iso = _utc_now_iso()
    starts_value = _coerce_text(starts_utc, now_iso) or now_iso
    ends_value = _coerce_text(ends_utc, "")
    reason_value = _coerce_text(reason, "")
    created_by_value = _coerce_text(created_by, "settings_ui") or "settings_ui"

    if current:
        current_operating_id = int(current.get("operating_profile_id", 0) or 0)
        current_state = _normalize_assignment_state(current.get("assignment_state", "active"), "active")
        current_reason = _coerce_text(current.get("reason", ""), "")
        current_ends = _coerce_text(current.get("ends_utc", ""), "")
        if (
            current_operating_id == int(operating_profile_id)
            and current_state == desired_state
            and current_reason == reason_value
            and current_ends == ends_value
        ):
            return dict(current)
        if current_operating_id == int(operating_profile_id) and current_state == desired_state:
            conn.execute(
                """
                UPDATE operating_profile_assignments
                   SET reason=?, ends_utc=?, updated_utc=?
                 WHERE id=?
                """,
                (
                    reason_value,
                    ends_value or None,
                    now_iso,
                    int(current.get("id", 0) or 0),
                ),
            )
            conn.commit()
            return _effective_assignment_for_device(conn, int(device_profile_id)) or {}

        conn.execute(
            """
            UPDATE operating_profile_assignments
               SET assignment_state='superseded', ends_utc=?, updated_utc=?
             WHERE id=?
            """,
            (
                _coerce_text(current.get("ends_utc", ""), "") or now_iso,
                now_iso,
                int(current.get("id", 0) or 0),
            ),
        )

    conn.execute(
        """
        INSERT INTO operating_profile_assignments (
            device_profile_id,
            operating_profile_id,
            assignment_state,
            starts_utc,
            ends_utc,
            reason,
            created_by,
            created_utc,
            updated_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(device_profile_id),
            int(operating_profile_id),
            desired_state,
            starts_value,
            ends_value or None,
            reason_value,
            created_by_value,
            now_iso,
            now_iso,
        ),
    )
    conn.commit()
    return _effective_assignment_for_device(conn, int(device_profile_id)) or {}


def _restore_default_operating_profile_conn(
    conn: sqlite3.Connection,
    device_profile_id: int,
    *,
    reason: str = "Restored default Operating Model.",
    created_by: str = "settings_ui",
    allow_active_swap_edit: bool = False,
) -> Dict[str, Any]:
    device = _record_by_id(conn, "device_profiles", int(device_profile_id))
    if not device:
        raise KeyError(f"Unknown device profile id: {device_profile_id}")
    if _is_observer_device_class(device):
        operating = _ensure_receive_only_operating_profile_conn(conn, commit=False)
    else:
        operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
        if not operating:
            operating = _save_operating_profile_conn(conn, _seed_operating_defaults(_load_kv_settings(conn)))
    return _set_device_operating_profile_conn(
        conn,
        int(device_profile_id),
        int(operating.get("id", 0) or 0),
        assignment_state="active",
        reason=reason,
        created_by=created_by,
        allow_active_swap_edit=allow_active_swap_edit,
    )


def _restore_assignment_snapshot_conn(
    conn: sqlite3.Connection,
    device_profile_id: int,
    snapshot: Optional[Mapping[str, Any]],
    *,
    fallback_reason: str = "Restored previous Operating Model after Temporary Model Swap.",
    created_by: str = "settings_ui",
    allow_active_swap_edit: bool = False,
) -> Dict[str, Any]:
    data = dict(snapshot or {})
    operating_profile_id = data.get("operating_profile_id")
    if operating_profile_id in (None, ""):
        return _restore_default_operating_profile_conn(
            conn,
            int(device_profile_id),
            reason=fallback_reason,
            created_by=created_by,
            allow_active_swap_edit=allow_active_swap_edit,
        )
    return _set_device_operating_profile_conn(
        conn,
        int(device_profile_id),
        int(operating_profile_id),
        assignment_state=_normalize_assignment_state(data.get("assignment_state", "active"), "active"),
        reason=_coerce_text(data.get("reason", fallback_reason), fallback_reason),
        created_by=_coerce_text(data.get("created_by", created_by), created_by) or created_by,
        starts_utc=_coerce_text(data.get("starts_utc", ""), "") or None,
        ends_utc=_coerce_text(data.get("ends_utc", ""), "") or None,
        allow_active_swap_edit=allow_active_swap_edit,
    )


def _runtime_primary_device_profile(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
          FROM device_profiles
         WHERE runtime_active=1
           AND runtime_primary=1
      ORDER BY display_order ASC, id ASC
         LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return _resolve_device_profile_links_conn(conn, dict(row))


def _runtime_active_device_profiles(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
          FROM device_profiles
         WHERE runtime_active=1
      ORDER BY display_order ASC, id ASC
        """
    ).fetchall()
    return [_resolve_device_profile_links_conn(conn, dict(row)) for row in rows]


def _runtime_active_device_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
          FROM device_profiles
         WHERE runtime_active=1
        """
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0] or 0)
    except Exception:
        return 0


def _ensure_default_assignment(conn: sqlite3.Connection, device_id: int, operating_profile_id: int) -> None:
    if _effective_assignment_for_device(conn, int(device_id)):
        return
    device = _record_by_id(conn, "device_profiles", int(device_id))
    if not device:
        raise KeyError(f"Unknown device profile id: {device_id}")
    operating = _record_by_id(conn, "operating_profiles", int(operating_profile_id))
    if not operating:
        raise KeyError(f"Unknown Operating Model id: {operating_profile_id}")
    _validate_assignment_plan_compatibility(device, operating)
    row = conn.execute(
        """
        SELECT id
          FROM operating_profile_assignments
         WHERE device_profile_id=?
           AND operating_profile_id=?
           AND assignment_state='active'
         LIMIT 1
        """,
        (int(device_id), int(operating_profile_id)),
    ).fetchone()
    if row:
        return
    now_iso = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO operating_profile_assignments (
            device_profile_id,
            operating_profile_id,
            assignment_state,
            created_utc,
            updated_utc
        ) VALUES (?, ?, 'active', ?, ?)
        """,
        (int(device_id), int(operating_profile_id), now_iso, now_iso),
    )
    conn.commit()


def _ensure_effective_assignment_for_device(conn: sqlite3.Connection, device_id: int) -> Dict[str, Any]:
    assignment = _effective_assignment_for_device(conn, int(device_id))
    if assignment:
        return dict(assignment)
    device = _record_by_id(conn, "device_profiles", int(device_id))
    if not device:
        raise KeyError(f"Unknown device profile id: {device_id}")
    if _is_observer_device_class(device):
        operating = _ensure_receive_only_operating_profile_conn(conn, commit=False)
    else:
        operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
        if not operating:
            operating = _save_operating_profile_conn(conn, _seed_operating_defaults(_load_kv_settings(conn)))
    _ensure_default_assignment(conn, int(device_id), int(operating.get("id", 0) or 0))
    return _effective_assignment_for_device(conn, int(device_id)) or {}


def _resolve_device_profile_links_conn(conn: sqlite3.Connection, profile: Mapping[str, Any]) -> Dict[str, Any]:
    data = dict(profile)
    js8_instance_id = _coerce_optional_int(data.get("js8_instance_id"))
    if js8_instance_id is not None:
        js8_row = _record_by_id(conn, "js8_instances", int(js8_instance_id))
        if js8_row:
            data["js8_instance_system_key"] = _coerce_text(js8_row.get("system_key", ""), "")
            data["js8_instance_name"] = _coerce_text(js8_row.get("name", ""), "")
            data["js8_host"] = _coerce_text(js8_row.get("host", ""), "127.0.0.1") or "127.0.0.1"
            data["js8_port"] = _coerce_optional_int(js8_row.get("port"), 2442)
            data["js8_offset_hz"] = _coerce_optional_int(js8_row.get("offset_hz"), 0)
            data["js8_profile_path"] = _coerce_text(js8_row.get("profile_path", ""), "")
            data["js8_directed_path"] = _coerce_text(js8_row.get("directed_path", ""), "")
            data["js8_inbox_path"] = _coerce_text(js8_row.get("inbox_path", ""), "")
            data["js8_forms_path"] = _coerce_text(js8_row.get("forms_path", ""), "")
            data["js8_install_path"] = _coerce_text(js8_row.get("install_path", ""), "")
            data["js8_variant_family"] = _coerce_text(js8_row.get("variant_family", "unknown"), "unknown")
            data["js8_variant_version"] = _coerce_text(js8_row.get("variant_version", ""), "")
            data["js8_rig_name"] = _coerce_text(js8_row.get("rig_name", ""), "")
            data["js8_rig_name_source"] = _coerce_text(js8_row.get("rig_name_source", ""), "")
            data["js8_message_storage_root"] = _coerce_text(js8_row.get("application_data_root", ""), "")
            data["js8_all_path"] = _coerce_text(js8_row.get("all_path", ""), "")
            data["js8_save_dir"] = _coerce_text(js8_row.get("save_dir", ""), "")
            data["js8_storage_mode"] = _coerce_text(js8_row.get("storage_mode", "unverified"), "unverified")
            data["js8_storage_verified_utc"] = _coerce_text(js8_row.get("storage_verified_utc", ""), "")
            data["js8_storage_evidence"] = _coerce_text(js8_row.get("storage_evidence", ""), "")
            data["spotter_launch_path"] = _coerce_text(js8_row.get("spotter_launch_path", ""), "")
            data["commstat_launch_path"] = _coerce_text(js8_row.get("commstat_launch_path", ""), "")
            if _coerce_text(data.get("control_backend", ""), "").lower() == "js8call":
                data["launch_path"] = data["js8_install_path"]

    fast_light_config_id = _coerce_optional_int(data.get("fast_light_config_id"))
    if fast_light_config_id is not None:
        fast_light_row = _record_by_id(conn, "fast_light_configs", int(fast_light_config_id))
        if fast_light_row:
            data["flrig_host"] = _coerce_text(fast_light_row.get("flrig_host", ""), "127.0.0.1") or "127.0.0.1"
            data["flrig_port"] = _coerce_optional_int(fast_light_row.get("flrig_port"), 12345)
            data["fldigi_host"] = _coerce_text(
                fast_light_row.get("fldigi_host", ""),
                data.get("flrig_host", "127.0.0.1"),
            ) or _coerce_text(data.get("flrig_host", ""), "127.0.0.1")
            data["fldigi_port"] = _coerce_optional_int(fast_light_row.get("fldigi_port"), 7362)
            data["fldigi_log_path"] = _coerce_text(fast_light_row.get("fldigi_log_path", ""), "")
            data["fldigi_checkin_dir"] = _coerce_text(fast_light_row.get("fldigi_checkin_dir", ""), "")
            data["flrig_path"] = _coerce_text(fast_light_row.get("flrig_path", ""), "")
            data["fldigi_path"] = _coerce_text(fast_light_row.get("fldigi_path", ""), "")
            if _coerce_text(data.get("control_backend", ""), "").lower() == "flrig":
                data["launch_path"] = data["flrig_path"]

    varac_node_id = _coerce_optional_int(data.get("varac_node_id"))
    if varac_node_id is not None:
        varac_row = _record_by_id(conn, "varac_nodes", int(varac_node_id))
        if varac_row:
            if not _coerce_text(data.get("varac_install_path", ""), ""):
                data["varac_install_path"] = _coerce_text(varac_row.get("install_path", ""), "")
            if not _coerce_text(data.get("varac_db_path", ""), ""):
                data["varac_db_path"] = _coerce_text(varac_row.get("db_path", ""), "")
            if not _coerce_text(data.get("varac_ini_path", ""), ""):
                data["varac_ini_path"] = _coerce_text(varac_row.get("ini_path", ""), "")
            data["varac_vara_runtime_path"] = _coerce_text(
                varac_row.get("vara_runtime_path", ""), ""
            )
            data["varac_vara_ini_path"] = _coerce_text(varac_row.get("vara_ini_path", ""), "")
            data["varac_native_management_state"] = _coerce_text(
                varac_row.get("native_management_state", "operator"), "operator"
            )
            data["varac_native_writer_key"] = _coerce_text(
                varac_row.get("native_writer_key", ""), ""
            )
            data["varac_native_desired_fingerprint"] = _coerce_text(
                varac_row.get("desired_fingerprint", ""), ""
            )
            data["varac_native_observed_fingerprint"] = _coerce_text(
                varac_row.get("observed_fingerprint", ""), ""
            )
            data["varac_last_native_verified_utc"] = _coerce_text(
                varac_row.get("last_native_verified_utc", ""), ""
            )
            data["varac_native_verification_summary"] = _coerce_text(
                varac_row.get("native_verification_summary", ""), ""
            )
            if not _coerce_text(data.get("launch_cmd", ""), ""):
                data["launch_cmd"] = _coerce_text(varac_row.get("launch_cmd", ""), "")
            if not _coerce_text(data.get("varac_incoming_path", ""), ""):
                data["varac_incoming_path"] = _coerce_text(varac_row.get("incoming_path", ""), "")

    data["varac_bbs_vault_locations_v1"] = _parse_json_list(data.get("varac_bbs_vault_locations_v1", "[]"))
    data["varac_bbs_sweeper_rules_v1"] = _parse_json_list(data.get("varac_bbs_sweeper_rules_v1", "[]"))
    data["varac_bbs_vault_runtime_state_v1"] = _parse_json_object(
        data.get("varac_bbs_vault_runtime_state_v1", "{}")
    )

    assignment = _effective_assignment_for_device(conn, _coerce_int(data.get("id"), 0))
    if assignment:
        operating = _record_by_id(conn, "operating_profiles", int(assignment["operating_profile_id"]))
        if operating:
            data["operating_profile_id"] = operating["id"]
            data["operating_scheduler_enabled"] = _coerce_bool_int(operating.get("scheduler_enabled", 1), True)
            if data.get("scheduler_enabled") is None:
                data["scheduler_enabled"] = data["operating_scheduler_enabled"]
            data["use_launch_control"] = _coerce_bool_int(operating.get("use_launch_control", 0), False)
    if data.get('use_external_js8spotter') is None:
        data['use_external_js8spotter'] = int(bool(str(data.get('spotter_launch_path') or '').strip()))

    return data


def _legacy_settings_projection_from_device(
    device_profile: Mapping[str, Any],
    existing_settings: Mapping[str, Any],
) -> Dict[str, Any]:
    control_backend = _coerce_text(device_profile.get("control_backend", "manual"), "manual").lower() or "manual"
    use_flrig = bool(_coerce_bool_int(device_profile.get("use_flrig"), control_backend == "flrig"))
    use_fldigi = bool(_coerce_bool_int(device_profile.get("use_fldigi"), False))
    use_flmsg = bool(_coerce_bool_int(device_profile.get("use_flmsg"), False))
    use_flamp = bool(_coerce_bool_int(device_profile.get("use_flamp"), False))
    use_js8call = bool(_coerce_bool_int(device_profile.get("use_js8call"), control_backend == "js8call"))
    use_js8spotter = bool(_coerce_bool_int(device_profile.get("use_js8spotter"), False))
    use_commstat = bool(_coerce_bool_int(device_profile.get("use_commstat"), False))
    use_varac = bool(_coerce_bool_int(device_profile.get("use_varac"), False))
    flrig_host = _coerce_text(device_profile.get("flrig_host", ""), "127.0.0.1") or "127.0.0.1"
    fldigi_host = _coerce_text(device_profile.get("fldigi_host", ""), "") or flrig_host or "127.0.0.1"
    js8_host = _coerce_text(device_profile.get("js8_host", ""), "127.0.0.1") or "127.0.0.1"
    message_paths = dict(existing_settings.get("message_paths", {}) or {})
    flmsg_message_path = _coerce_text(device_profile.get("flmsg_message_path", ""), "")
    if use_flmsg and flmsg_message_path:
        message_paths["flmsg"] = flmsg_message_path
    else:
        message_paths.pop("flmsg", None)
    flamp_message_path = _coerce_text(device_profile.get("flamp_message_path", ""), "")
    if use_flamp and flamp_message_path:
        message_paths["flamp"] = flamp_message_path
    else:
        message_paths.pop("flamp", None)
    varac_incoming = _coerce_text(device_profile.get("varac_incoming_path", ""), "")
    if use_varac and varac_incoming:
        message_paths["varac"] = varac_incoming
    else:
        message_paths.pop("varac", None)
    updates: Dict[str, Any] = {
        "control_via": _legacy_control_via(control_backend),
        "rig_host": _coerce_text(device_profile.get("rig_host", ""), ""),
        "rig_port": _coerce_optional_int(device_profile.get("rig_port"), 4532),
        "flrig_host": flrig_host if use_flrig else "",
        "flrig_port": _coerce_optional_int(device_profile.get("flrig_port"), 12345) if use_flrig else None,
        "fldigi_host": fldigi_host if use_fldigi else "",
        "fldigi_port": _coerce_optional_int(device_profile.get("fldigi_port"), 7362) if use_fldigi else None,
        "fldigi_log_path": _coerce_text(device_profile.get("fldigi_log_path", ""), "") if use_fldigi else "",
        "fldigi_checkin_dir": _coerce_text(device_profile.get("fldigi_checkin_dir", ""), "") if use_fldigi else "",
        "varac_outbox_dir": _coerce_text(device_profile.get("varac_outbox_dir", ""), "") if use_varac else "",
        "varac_bbs_dir": _coerce_text(device_profile.get("varac_bbs_dir", ""), "") if use_varac else "",
        "varac_bbs_archive_dir": _coerce_text(device_profile.get("varac_bbs_archive_dir", ""), "") if use_varac else "",
        "varac_bbs_enabled": bool(_coerce_bool_int(device_profile.get("varac_bbs_enabled", 0), False)) if use_varac else False,
        "varac_bbs_limit_access_enabled": bool(
            _coerce_bool_int(device_profile.get("varac_bbs_limit_access_enabled", 0), False)
        )
        if use_varac
        else False,
        "varac_bbs_allowed_callsigns": _coerce_text(device_profile.get("varac_bbs_allowed_callsigns", ""), "") if use_varac else "",
        "varac_guard_access_policy_v1": _coerce_text(device_profile.get("varac_guard_access_policy_v1", ""), ""),
        "varac_guard_allow_bbs_allowed_callsigns": _coerce_bool_int(
            device_profile.get("varac_guard_allow_bbs_allowed_callsigns") if device_profile.get("varac_guard_allow_bbs_allowed_callsigns") is not None
            else existing_settings.get("varac_guard_allow_bbs_allowed_callsigns", True), True),
        "varac_guard_allow_operator_trusted": _coerce_bool_int(
            device_profile.get("varac_guard_allow_operator_trusted") if device_profile.get("varac_guard_allow_operator_trusted") is not None
            else existing_settings.get("varac_guard_allow_operator_trusted", True), True),
        "varac_bbs_allowed_group_sources": _coerce_text(
            device_profile.get("varac_bbs_allowed_group_sources", ""),
            "",
        )
        if use_varac
        else "",
        "varac_bbs_announce_enabled": bool(
            _coerce_bool_int(device_profile.get("varac_bbs_announce_enabled", 0), False)
        )
        if use_varac
        else False,
        "varac_bbs_auto_archive_enabled": bool(
            _coerce_bool_int(device_profile.get("varac_bbs_auto_archive_enabled", 0), False)
        )
        if use_varac
        else False,
        "varac_bbs_auto_archive_days": _coerce_optional_int(device_profile.get("varac_bbs_auto_archive_days"), 14)
        if use_varac
        else 14,
        "varac_bbs_vault_enabled": bool(
            _coerce_bool_int(device_profile.get("varac_bbs_vault_enabled", 0), False)
        )
        if use_varac
        else False,
        "varac_bbs_vault_managed_root": _coerce_text(device_profile.get("varac_bbs_vault_managed_root", ""), "")
        if use_varac
        else "",
        "varac_bbs_vault_default_location_id": _coerce_text(
            device_profile.get("varac_bbs_vault_default_location_id", ""),
            "",
        )
        if use_varac
        else "",
        "varac_bbs_vault_global_code_policy": _coerce_text(
            device_profile.get("varac_bbs_vault_global_code_policy", ""),
            "",
        )
        if use_varac
        else "",
        "varac_bbs_vault_trigger_mode": _coerce_text(device_profile.get("varac_bbs_vault_trigger_mode", ""), "")
        if use_varac
        else "",
        "varac_bbs_vault_return_mode": _coerce_text(device_profile.get("varac_bbs_vault_return_mode", ""), "")
        if use_varac
        else "",
        "varac_bbs_vault_failed_attempt_limit": _coerce_optional_int(
            device_profile.get("varac_bbs_vault_failed_attempt_limit"),
            3,
        )
        if use_varac
        else 3,
        "varac_bbs_vault_failed_attempt_window_seconds": _coerce_optional_int(
            device_profile.get("varac_bbs_vault_failed_attempt_window_seconds"),
            900,
        )
        if use_varac
        else 900,
        "varac_bbs_vault_cooldown_seconds": _coerce_optional_int(
            device_profile.get("varac_bbs_vault_cooldown_seconds"),
            1800,
        )
        if use_varac
        else 1800,
        "varac_bbs_vault_idle_timeout_seconds": _coerce_optional_int(
            device_profile.get("varac_bbs_vault_idle_timeout_seconds"),
            600,
        )
        if use_varac
        else 600,
        "varac_bbs_vault_flamp_enabled": bool(
            _coerce_bool_int(device_profile.get("varac_bbs_vault_flamp_enabled", 0), False)
        )
        if use_varac
        else False,
        "varac_bbs_vault_flamp_relay_dir": _coerce_text(
            device_profile.get("varac_bbs_vault_flamp_relay_dir", ""),
            "",
        )
        if use_varac
        else "",
        "varac_bbs_vault_flamp_listing_max_age_days": _coerce_optional_int(
            device_profile.get("varac_bbs_vault_flamp_listing_max_age_days"),
            14,
        )
        if use_varac
        else 14,
        "varac_bbs_vault_locations_v1": _parse_json_list(device_profile.get("varac_bbs_vault_locations_v1", "[]"))
        if use_varac
        else [],
        "varac_bbs_sweeper_rules_v1": _parse_json_list(device_profile.get("varac_bbs_sweeper_rules_v1", "[]"))
        if use_varac
        else [],
        "varac_bbs_vault_runtime_state_v1": _parse_json_object(
            device_profile.get("varac_bbs_vault_runtime_state_v1", "{}")
        )
        if use_varac
        else {},
        "varac_bbs_vault_last_summary": _coerce_text(device_profile.get("varac_bbs_vault_last_summary", ""), "")
        if use_varac
        else "",
        "js8_host": js8_host if use_js8call else "",
        "js8_port": _coerce_optional_int(device_profile.get("js8_port"), 2442) if use_js8call else None,
        "js8_offset_hz": (_coerce_optional_int(device_profile.get("js8_offset_hz"), 0) or 0) if use_js8call else 0,
        "js8_profile_path": _coerce_text(device_profile.get("js8_profile_path", ""), "") if use_js8call else "",
        "js8_directed_path": _coerce_text(device_profile.get("js8_directed_path", ""), "") if use_js8call else "",
        "js8_forms_path": _coerce_text(device_profile.get("js8_forms_path", ""), "") if use_js8call else "",
        "varac_path": _coerce_text(device_profile.get("varac_install_path", ""), "") if use_varac else "",
        "varac_db_path": _coerce_text(device_profile.get("varac_db_path", ""), "") if use_varac else "",
        "varac_ini_path": _coerce_text(device_profile.get("varac_ini_path", ""), "") if use_varac else "",
        "varac_launch_cmd": _coerce_text(device_profile.get("launch_cmd", ""), "") if use_varac else "",
        "message_paths": message_paths,
        "launch_control_enabled": bool(_coerce_bool_int(device_profile.get("launch_enabled", 0), False)),
    }
    flrig_path = _coerce_text(device_profile.get("flrig_path", ""), "")
    if use_flrig and flrig_path:
        updates["path_flrig"] = flrig_path
    elif use_flrig and control_backend == "flrig" and not flrig_path:
        updates["path_flrig"] = _coerce_text(device_profile.get("launch_path", ""), "")
    else:
        updates["path_flrig"] = ""
    fldigi_path = _coerce_text(device_profile.get("fldigi_path", ""), "")
    if use_fldigi and fldigi_path:
        updates["path_fldigi"] = fldigi_path
    else:
        updates["path_fldigi"] = ""
    updates["path_flmsg"] = _coerce_text(device_profile.get("flmsg_path", ""), "") if use_flmsg else ""
    updates["path_flamp"] = _coerce_text(device_profile.get("flamp_path", ""), "") if use_flamp else ""
    js8_install_path = _coerce_text(device_profile.get("js8_install_path", ""), "")
    if use_js8call and js8_install_path:
        updates["path_js8call"] = js8_install_path
    elif use_js8call and control_backend == "js8call" and not js8_install_path:
        updates["path_js8call"] = _coerce_text(device_profile.get("launch_path", ""), "")
    else:
        updates["path_js8call"] = ""
    spotter_launch = _coerce_text(device_profile.get("spotter_launch_path", ""), "")
    external_spotter_selected = device_profile.get("use_external_js8spotter")
    if (bool(external_spotter_selected) if external_spotter_selected is not None else use_js8spotter) and spotter_launch:
        updates["path_js8spotter"] = spotter_launch
    else:
        updates["path_js8spotter"] = ""
    commstat_launch = _coerce_text(device_profile.get("commstat_launch_path", ""), "")
    if use_commstat and commstat_launch:
        updates["path_commstat"] = commstat_launch
    else:
        updates["path_commstat"] = ""
    scheduler_enabled = device_profile.get("scheduler_enabled")
    if scheduler_enabled is not None:
        updates["use_scheduler"] = bool(_coerce_bool_int(scheduler_enabled, True))
    updates["schedule_hold_minutes_default"] = _normalize_hold_duration_minutes(
        device_profile.get("schedule_hold_minutes_default", DEFAULT_HOLD_DURATION_MINUTES),
    )
    updates["freq_enforcement_mode"] = _coerce_text(
        device_profile.get("freq_enforcement_mode", DEFAULT_TIMER_ENFORCEMENT_MODE),
        DEFAULT_TIMER_ENFORCEMENT_MODE,
    )
    updates["freq_prompt_interval"] = _coerce_text(
        device_profile.get("freq_prompt_interval", DEFAULT_TIMER_PROMPT_INTERVAL),
        DEFAULT_TIMER_PROMPT_INTERVAL,
    )
    updates["fldigi_enforcement_mode"] = _coerce_text(
        device_profile.get("fldigi_enforcement_mode", DEFAULT_TIMER_ENFORCEMENT_MODE),
        DEFAULT_TIMER_ENFORCEMENT_MODE,
    )
    updates["fldigi_prompt_interval"] = _coerce_text(
        device_profile.get("fldigi_prompt_interval", DEFAULT_TIMER_PROMPT_INTERVAL),
        DEFAULT_TIMER_PROMPT_INTERVAL,
    )
    updates["js8_enforcement_mode"] = _coerce_text(
        device_profile.get("js8_enforcement_mode", DEFAULT_TIMER_ENFORCEMENT_MODE),
        DEFAULT_TIMER_ENFORCEMENT_MODE,
    )
    updates["js8_prompt_interval"] = _coerce_text(
        device_profile.get("js8_prompt_interval", DEFAULT_TIMER_PROMPT_INTERVAL),
        DEFAULT_TIMER_PROMPT_INTERVAL,
    )
    return updates


def _normalize_runtime_primary_device(
    conn: sqlite3.Connection,
    *,
    allow_pre_migration: bool = False,
) -> Optional[int]:
    if not allow_pre_migration and not is_multi_rig_migration_current(conn):
        return None
    rows = conn.execute(
        """
        SELECT id, enabled, runtime_active, runtime_primary, device_class
          FROM device_profiles
      ORDER BY display_order ASC, id ASC
        """
    ).fetchall()
    if not rows:
        return None
    enabled_rows = [row for row in rows if int(row[1] or 0) == 1]
    candidates = [row for row in enabled_rows if _coerce_text(row[4], "tx_rx").lower() != "observer"]
    if not candidates:
        # A receive-only station may have active observers, but it has no
        # compatibility-primary radio. Never promote an SDR merely because it
        # is the first or only configured device.
        conn.execute("UPDATE device_profiles SET runtime_primary=0 WHERE runtime_primary<>0")
        conn.commit()
        return None
    active_candidates = [row for row in candidates if int(row[2] or 0) == 1]
    chosen = next((row for row in active_candidates if int(row[3] or 0) == 1), None)
    if chosen is None and active_candidates:
        chosen = active_candidates[0]
    if chosen is None and not allow_pre_migration:
        conn.execute("UPDATE device_profiles SET runtime_primary=0 WHERE runtime_primary<>0")
        conn.commit()
        return None
    if chosen is None:
        chosen = candidates[0]
    chosen_id = int(chosen[0])
    active_ids = {int(row[0]) for row in enabled_rows if int(row[2] or 0) == 1}
    if not active_ids:
        active_ids = {chosen_id}
    else:
        active_ids.add(chosen_id)
    conn.executemany(
        "UPDATE device_profiles SET runtime_active=?, runtime_primary=? WHERE id=?",
        [
            (
                1 if int(row[0]) in active_ids else 0,
                1 if int(row[0]) == chosen_id else 0,
                int(row[0]),
            )
            for row in rows
        ],
    )
    conn.commit()
    return chosen_id


def _seed_js8_defaults(settings_values: Mapping[str, Any]) -> Dict[str, Any]:
    legacy_directed = _settings_text(settings_values, "js8_directed_path", "")
    return {
        "system_key": DEFAULT_JS8_INSTANCE_SYSTEM_KEY,
        "name": DEFAULT_JS8_INSTANCE_NAME,
        "host": _settings_text(settings_values, "js8_host", "127.0.0.1") or "127.0.0.1",
        "port": _settings_int(settings_values, "js8_port", 2442),
        "offset_hz": coerce_js8_offset_hz(_settings_int(settings_values, "js8_offset_hz", 0)),
        "profile_path": _settings_text(settings_values, "js8_profile_path", ""),
        "directed_path": legacy_directed,
        "inbox_path": _settings_text(settings_values, "js8_inbox_path", ""),
        "forms_path": _settings_text(settings_values, "js8_forms_path", ""),
        "install_path": _settings_text(settings_values, "path_js8call", ""),
        "variant_family": "unknown",
        "storage_mode": "unverified",
        "storage_evidence": "legacy_explicit_path" if legacy_directed else "",
        "spotter_launch_path": _settings_text(settings_values, "path_js8spotter", ""),
        "commstat_launch_path": _settings_text(settings_values, "path_commstat", ""),
    }


def _seed_fast_light_defaults(settings_values: Mapping[str, Any]) -> Dict[str, Any]:
    flrig_host = _settings_text(settings_values, "flrig_host", "127.0.0.1") or "127.0.0.1"
    fldigi_host = _settings_text(settings_values, "fldigi_host", "") or flrig_host or "127.0.0.1"
    return {
        "system_key": DEFAULT_FAST_LIGHT_SYSTEM_KEY,
        "name": DEFAULT_FAST_LIGHT_NAME,
        "flrig_path": _settings_text(settings_values, "path_flrig", ""),
        "flrig_host": flrig_host,
        "flrig_port": _settings_int(settings_values, "flrig_port", 12345),
        "fldigi_path": _settings_text(settings_values, "path_fldigi", ""),
        "fldigi_host": fldigi_host,
        "fldigi_port": _settings_int(settings_values, "fldigi_port", 7362),
        "fldigi_log_path": _settings_text(settings_values, "fldigi_log_path", ""),
        "fldigi_checkin_dir": _settings_text(settings_values, "fldigi_checkin_dir", ""),
    }


def _settings_varac_incoming_path(settings_values: Mapping[str, Any]) -> str:
    direct = _settings_text(settings_values, "varac_incoming_path", "")
    if direct:
        return direct
    message_paths = settings_values.get("message_paths", {}) or {}
    if isinstance(message_paths, Mapping):
        return _coerce_text(message_paths.get("varac", ""), "")
    return ""


def _seed_varac_defaults(settings_values: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "system_key": DEFAULT_VARAC_NODE_SYSTEM_KEY,
        "name": DEFAULT_VARAC_NODE_NAME,
        "install_path": _settings_text(settings_values, "varac_path", ""),
        "db_path": _settings_text(settings_values, "varac_db_path", ""),
        "ini_path": _settings_text(settings_values, "varac_ini_path", ""),
        "launch_cmd": _settings_text(settings_values, "varac_launch_cmd", ""),
        "incoming_path": _settings_varac_incoming_path(settings_values),
    }


def _seed_operating_defaults(settings_values: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "system_key": DEFAULT_OPERATING_SYSTEM_KEY,
        "name": DEFAULT_OPERATING_NAME,
        "category": "normal",
        "status": "saved",
        "scheduler_enabled": _settings_bool(settings_values, "use_scheduler", True),
        "scheduler_mode": "full",
        "preferred_band_set_json": "[]",
        "source_refs_json": "[]",
        "schedule_refs_json": "[]",
        "frequency_refs_json": "[]",
        "group_refs_json": "[]",
        "notes": "",
        "use_messages": 1,
        "use_map": 1,
        "use_background_ingest": 1,
        "use_launch_control": _settings_bool(settings_values, "launch_control_enabled", False),
        "use_net_control_tabs": 1,
        "receive_only": 0,
        "allow_profile_swap": 0,
    }


def _seed_receive_only_operating_defaults() -> Dict[str, Any]:
    """Return the safe built-in operating model for observer/SDR profiles."""

    return {
        "system_key": DEFAULT_RECEIVE_ONLY_OPERATING_SYSTEM_KEY,
        "name": DEFAULT_RECEIVE_ONLY_OPERATING_NAME,
        "description": (
            "Receive-only monitoring for observer / SDR radios. No transmit, PTT, "
            "QSY, net-control, scheduler, or profile-swap authority."
        ),
        "category": "rx_watch",
        "status": "saved",
        "scheduler_enabled": 0,
        "scheduler_mode": "simple",
        "preferred_band_set_json": "[]",
        "source_refs_json": "[]",
        "schedule_refs_json": "[]",
        "frequency_refs_json": "[]",
        "group_refs_json": "[]",
        "notes": "Built-in safe operating model for receive-only SDR profiles.",
        "use_messages": 1,
        "use_map": 1,
        "use_background_ingest": 1,
        "use_launch_control": 1,
        "use_net_control_tabs": 0,
        "receive_only": 1,
        "allow_profile_swap": 0,
    }


def _ensure_receive_only_operating_profile_conn(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> Dict[str, Any]:
    """Create or safety-repair the built-in receiver model idempotently."""

    existing = _record_by_system_key(
        conn,
        "operating_profiles",
        DEFAULT_RECEIVE_ONLY_OPERATING_SYSTEM_KEY,
    )
    if existing is None:
        record = dict(_seed_receive_only_operating_defaults())
        now_iso = _utc_now_iso()
        record.update({"enabled": 1, "created_utc": now_iso, "updated_utc": now_iso})
        columns = tuple(record)
        conn.execute(
            f"INSERT INTO operating_profiles ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(record[column] for column in columns),
        )
        if commit:
            conn.commit()
        return (
            _record_by_system_key(
                conn,
                "operating_profiles",
                DEFAULT_RECEIVE_ONLY_OPERATING_SYSTEM_KEY,
            )
            or {}
        )

    # These fields are capability boundaries, not preferences. Preserve the
    # operator-facing name and optional feature preferences while repairing an
    # older or manually altered row back to its non-transmitting contract.
    conn.execute(
        """
        UPDATE operating_profiles
           SET enabled=1,
               category='rx_watch',
               scheduler_enabled=0,
               scheduler_mode='simple',
               use_net_control_tabs=0,
               receive_only=1,
               allow_profile_swap=0,
               updated_utc=?
         WHERE id=?
        """,
        (_utc_now_iso(), int(existing["id"])),
    )
    if commit:
        conn.commit()
    return _record_by_id(conn, "operating_profiles", int(existing["id"])) or existing


def _seed_device_defaults(
    settings_values: Mapping[str, Any],
    *,
    js8_instance_id: int,
    fast_light_config_id: int,
    varac_node_id: int,
) -> Dict[str, Any]:
    control_backend = _normalize_control_backend(settings_values)
    flrig_host = _settings_text(settings_values, "flrig_host", "127.0.0.1") or "127.0.0.1"
    fldigi_host = _settings_text(settings_values, "fldigi_host", "") or flrig_host or "127.0.0.1"
    js8_host = _settings_text(settings_values, "js8_host", "127.0.0.1") or "127.0.0.1"
    message_paths = settings_values.get("message_paths", {}) or {}
    launch_path = ""
    if control_backend == "flrig":
        launch_path = _settings_text(settings_values, "path_flrig", "")
    elif control_backend == "js8call":
        launch_path = _settings_text(settings_values, "path_js8call", "")
    return {
        "system_key": DEFAULT_DEVICE_SYSTEM_KEY,
        "name": DEFAULT_DEVICE_NAME,
        "needs_operator_name": _needs_operator_radio_name(DEFAULT_DEVICE_NAME),
        "enabled": 1,
        "runtime_active": 1,
        "runtime_primary": 1,
        "display_order": 0,
        "device_class": "tx_rx",
        "deployment_mode": "full",
        "control_backend": control_backend,
        "use_flrig": _coerce_bool_int(
            control_backend == "flrig" or bool(_settings_text(settings_values, "path_flrig", "")),
            False,
        ),
        "use_fldigi": _coerce_bool_int(
            bool(_settings_text(settings_values, "path_fldigi", "") or _settings_text(settings_values, "fldigi_log_path", "")),
            False,
        ),
        "use_flmsg": _coerce_bool_int(bool(_settings_text(settings_values, "path_flmsg", "")), False),
        "use_flamp": _coerce_bool_int(bool(_settings_text(settings_values, "path_flamp", "")), False),
        "use_js8call": _coerce_bool_int(
            control_backend == "js8call" or bool(_settings_text(settings_values, "path_js8call", "")),
            False,
        ),
        "use_js8spotter": _coerce_bool_int(bool(_settings_text(settings_values, "path_js8spotter", "")), False),
        "use_commstat": _coerce_bool_int(bool(_settings_text(settings_values, "path_commstat", "")), False),
        "use_varac": _coerce_bool_int(
            bool(
                _settings_text(settings_values, "varac_path", "")
                or _settings_text(settings_values, "varac_launch_cmd", "")
                or _settings_text(settings_values, "varac_db_path", "")
                or _settings_text(settings_values, "varac_ini_path", "")
                or _settings_varac_incoming_path(settings_values)
                or _settings_text(settings_values, "varac_outbox_dir", "")
            ),
            False,
        ),
        "rig_host": _settings_text(settings_values, "rig_host", ""),
        "rig_port": _settings_optional_int(settings_values, "rig_port"),
        "flrig_host": flrig_host,
        "flrig_port": _settings_int(settings_values, "flrig_port", 12345),
        "fldigi_host": fldigi_host,
        "fldigi_port": _settings_int(settings_values, "fldigi_port", 7362),
        "fldigi_log_path": _settings_text(settings_values, "fldigi_log_path", ""),
        "fldigi_checkin_dir": _settings_text(settings_values, "fldigi_checkin_dir", ""),
        "flmsg_path": _settings_text(settings_values, "path_flmsg", ""),
        "flmsg_message_path": _coerce_text(message_paths.get("flmsg", ""), ""),
        "flamp_path": _settings_text(settings_values, "path_flamp", ""),
        "flamp_message_path": _coerce_text(message_paths.get("flamp", ""), ""),
        "js8_host": js8_host,
        "js8_port": _settings_int(settings_values, "js8_port", 2442),
        "js8_instance_id": int(js8_instance_id),
        "js8_profile_path": _settings_text(settings_values, "js8_profile_path", ""),
        "js8_directed_path": _settings_text(settings_values, "js8_directed_path", ""),
        "js8_forms_path": _settings_text(settings_values, "js8_forms_path", ""),
        "fast_light_config_id": int(fast_light_config_id),
        "varac_install_path": _settings_text(settings_values, "varac_path", ""),
        "varac_db_path": _settings_text(settings_values, "varac_db_path", ""),
        "varac_ini_path": _settings_text(settings_values, "varac_ini_path", ""),
        "varac_node_id": int(varac_node_id),
        "varac_outbox_dir": _settings_text(settings_values, "varac_outbox_dir", ""),
        "varac_bbs_dir": _settings_text(settings_values, "varac_bbs_dir", ""),
        "varac_bbs_archive_dir": _settings_text(settings_values, "varac_bbs_archive_dir", ""),
        "varac_bbs_enabled": _coerce_bool_int(settings_values.get("varac_bbs_enabled"), False),
        "varac_bbs_limit_access_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_limit_access_enabled"),
            False,
        ),
        "varac_bbs_allowed_callsigns": _settings_text(settings_values, "varac_bbs_allowed_callsigns", ""),
        "varac_bbs_allowed_group_sources": _settings_text(settings_values, "varac_bbs_allowed_group_sources", ""),
        "varac_guard_access_policy_v1": _settings_text(settings_values, "varac_guard_access_policy_v1", ""),
        "varac_guard_allow_bbs_allowed_callsigns": _coerce_bool_int(settings_values["varac_guard_allow_bbs_allowed_callsigns"], True) if "varac_guard_allow_bbs_allowed_callsigns" in settings_values else None,
        "varac_guard_allow_operator_trusted": _coerce_bool_int(settings_values["varac_guard_allow_operator_trusted"], True) if "varac_guard_allow_operator_trusted" in settings_values else None,
        "varac_bbs_announce_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_announce_enabled"),
            False,
        ),
        "varac_bbs_auto_archive_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_auto_archive_enabled"),
            False,
        ),
        "varac_bbs_auto_archive_days": _settings_int(settings_values, "varac_bbs_auto_archive_days", 14),
        "varac_bbs_vault_enabled": _coerce_bool_int(settings_values.get("varac_bbs_vault_enabled"), False),
        "varac_bbs_vault_managed_root": _settings_text(settings_values, "varac_bbs_vault_managed_root", ""),
        "varac_bbs_vault_default_location_id": _settings_text(
            settings_values,
            "varac_bbs_vault_default_location_id",
            "",
        ),
        "varac_bbs_vault_global_code_policy": _settings_text(
            settings_values,
            "varac_bbs_vault_global_code_policy",
            "",
        ),
        "varac_bbs_vault_trigger_mode": _settings_text(settings_values, "varac_bbs_vault_trigger_mode", ""),
        "varac_bbs_vault_return_mode": _settings_text(settings_values, "varac_bbs_vault_return_mode", ""),
        "varac_bbs_vault_failed_attempt_limit": _settings_int(
            settings_values,
            "varac_bbs_vault_failed_attempt_limit",
            3,
        ),
        "varac_bbs_vault_failed_attempt_window_seconds": _settings_int(
            settings_values,
            "varac_bbs_vault_failed_attempt_window_seconds",
            900,
        ),
        "varac_bbs_vault_cooldown_seconds": _settings_int(
            settings_values,
            "varac_bbs_vault_cooldown_seconds",
            1800,
        ),
        "varac_bbs_vault_idle_timeout_seconds": _settings_int(
            settings_values,
            "varac_bbs_vault_idle_timeout_seconds",
            600,
        ),
        "varac_bbs_vault_flamp_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_vault_flamp_enabled"),
            False,
        ),
        "varac_bbs_vault_flamp_relay_dir": _settings_text(
            settings_values,
            "varac_bbs_vault_flamp_relay_dir",
            "",
        ),
        "varac_bbs_vault_flamp_listing_max_age_days": _settings_int(
            settings_values,
            "varac_bbs_vault_flamp_listing_max_age_days",
            14,
        ),
        "varac_bbs_vault_locations_v1": _coerce_json_list_text(
            settings_values.get("varac_bbs_vault_locations_v1", [])
        ),
        "varac_bbs_sweeper_rules_v1": _coerce_json_list_text(
            settings_values.get("varac_bbs_sweeper_rules_v1", [])
        ),
        "varac_bbs_vault_runtime_state_v1": _coerce_json_object_text(
            settings_values.get("varac_bbs_vault_runtime_state_v1", {})
        ),
        "varac_bbs_vault_last_summary": _settings_text(settings_values, "varac_bbs_vault_last_summary", ""),
        "scheduler_enabled": _settings_bool(settings_values, "use_scheduler", True),
        "schedule_hold_minutes_default": _normalize_hold_duration_minutes(
            settings_values.get("schedule_hold_minutes_default", DEFAULT_HOLD_DURATION_MINUTES)
        ),
        "freq_enforcement_mode": _settings_text(
            settings_values,
            "freq_enforcement_mode",
            DEFAULT_TIMER_ENFORCEMENT_MODE,
        ),
        "freq_prompt_interval": _settings_text(
            settings_values,
            "freq_prompt_interval",
            DEFAULT_TIMER_PROMPT_INTERVAL,
        ),
        "fldigi_enforcement_mode": _settings_text(
            settings_values,
            "fldigi_enforcement_mode",
            DEFAULT_TIMER_ENFORCEMENT_MODE,
        ),
        "fldigi_prompt_interval": _settings_text(
            settings_values,
            "fldigi_prompt_interval",
            DEFAULT_TIMER_PROMPT_INTERVAL,
        ),
        "js8_enforcement_mode": _settings_text(
            settings_values,
            "js8_enforcement_mode",
            DEFAULT_TIMER_ENFORCEMENT_MODE,
        ),
        "js8_prompt_interval": _settings_text(
            settings_values,
            "js8_prompt_interval",
            DEFAULT_TIMER_PROMPT_INTERVAL,
        ),
        "launch_enabled": _coerce_bool_int(settings_values.get("launch_control_enabled"), False),
        "launch_path": launch_path,
        "launch_cmd": _settings_text(settings_values, "varac_launch_cmd", ""),
        "sdr_host": _settings_text(settings_values, "sdr_host", ""),
        "sdr_port": _settings_optional_int(settings_values, "sdr_port"),
    }


def _schedule_row_ref(row: Mapping[str, Any], *, source_table: str, source: str) -> Dict[str, Any]:
    def _value(key: str, default: str = "") -> str:
        return _coerce_text(row.get(key, default), default)

    ref: Dict[str, Any] = {
        "source": source,
        "source_table": source_table,
        "source_row_id": int(row.get("id") or 0),
        "day_utc": _value("day_utc", "ALL") or "ALL",
        "band": _value("band", "").upper(),
        "mode": _value("mode", ""),
        "vfo": _value("vfo", "A").upper() or "A",
        "frequency": _value("frequency", ""),
        "start_utc": _value("start_utc", ""),
        "end_utc": _value("end_utc", ""),
        "auto_tune": bool(row.get("auto_tune") or 0),
    }
    for key in (
        "recurrence",
        "biweekly_offset_weeks",
        "month_weeks",
        "group_name",
        "early_checkin",
        "primary_js8call_group",
        "comment",
        "net_name",
        "fldigi_mode",
        "fldigi_offset",
        "resource_id",
        "target_scope",
        "target_device_profile_id",
        "target_operating_profile_id",
    ):
        value = row.get(key)
        if value not in (None, ""):
            ref[key] = value
    return ref


def _legacy_schedule_refs_from_conn(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    if _table_exists(conn, "daily_schedule_tab"):
        for row in _fetchall_dicts(
            conn,
            """
            SELECT *
              FROM daily_schedule_tab
          ORDER BY day_utc ASC, start_utc ASC, id ASC
            """,
        ):
            refs.append(_schedule_row_ref(row, source_table="daily_schedule_tab", source="HF"))
    return refs


def _legacy_schedule_refs_from_path(db_path: Optional[Path]) -> List[Dict[str, Any]]:
    if db_path is None or not db_path.exists():
        return []
    refs: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            if _table_exists(conn, "daily_schedule_tab"):
                for row in _fetchall_dicts(
                    conn,
                    """
                    SELECT *
                      FROM daily_schedule_tab
                  ORDER BY day_utc ASC, start_utc ASC, id ASC
                    """,
                ):
                    refs.append(_schedule_row_ref(row, source_table="daily_schedule_tab", source="HF"))
            for table in ("net_schedule_tab", "net_schedule"):
                if not _table_exists(conn, table):
                    continue
                for row in _fetchall_dicts(
                    conn,
                    f"""
                    SELECT *
                      FROM {table}
                  ORDER BY day_utc ASC, start_utc ASC, id ASC
                    """,
                ):
                    refs.append(_schedule_row_ref(row, source_table=table, source="NET"))
                if any(str(row.get("source_table") or "") in {"net_schedule_tab", "net_schedule"} for row in refs):
                    break
        finally:
            conn.close()
    except Exception as exc:
        log.debug("MultiRadioStore: legacy schedule migration scan skipped: %s", exc)
    return refs


def _frequency_refs_from_schedule_refs(schedule_refs: Iterable[Mapping[str, Any]]) -> List[str]:
    refs: List[str] = []
    for row in schedule_refs:
        band = _coerce_text(row.get("band", ""), "").upper()
        freq = _coerce_text(row.get("frequency", ""), "")
        if band and freq:
            refs.append(f"{band}:{freq}")
        elif freq:
            refs.append(freq)
        elif band:
            refs.append(band)
    return list(dict.fromkeys(refs))


def _group_refs_from_schedule_refs(schedule_refs: Iterable[Mapping[str, Any]]) -> List[str]:
    groups: List[str] = []
    for row in schedule_refs:
        group = _coerce_text(row.get("group_name") or row.get("group") or row.get("primary_js8call_group"), "").upper()
        if group:
            groups.append(group)
    return list(dict.fromkeys(groups))


def _dedupe_schedule_refs(schedule_refs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in schedule_refs:
        data = dict(row)
        key = json.dumps(
            {
                "source_table": data.get("source_table"),
                "source_row_id": data.get("source_row_id"),
                "day_utc": data.get("day_utc"),
                "band": data.get("band"),
                "frequency": data.get("frequency"),
                "start_utc": data.get("start_utc"),
                "end_utc": data.get("end_utc"),
                "net_name": data.get("net_name"),
                "group_name": data.get("group_name"),
            },
            sort_keys=True,
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(data)
    return out


def _ensure_migrated_single_rig_frequency_plan(
    conn: sqlite3.Connection,
    *,
    device_id: int,
    operating_plan_name: str = "",
) -> Optional[Dict[str, Any]]:
    if _effective_assigned_plan_for_device(conn, int(device_id)):
        return None
    existing = _record_by_system_key(conn, "frequency_plans", DEFAULT_FREQUENCY_PLAN_SYSTEM_KEY)
    if existing:
        _set_assigned_plan_conn(
            conn,
            int(device_id),
            int(existing["id"]),
            reason="Assigned migrated single-rig Frequency Plan.",
            created_by="migration",
            commit=False,
        )
        return existing

    schedule_refs = _legacy_schedule_refs_from_conn(conn)
    settings_db = _conn_database_path(conn)
    nets_db = settings_db.with_name("freqinout_nets.db") if settings_db else get_config_dir() / "config" / "freqinout_nets.db"
    schedule_refs.extend(_legacy_schedule_refs_from_path(nets_db))
    schedule_refs = _dedupe_schedule_refs(schedule_refs)
    if not schedule_refs:
        return None

    has_daily = any(str(row.get("source_table") or "") == "daily_schedule_tab" for row in schedule_refs)
    has_net = any(str(row.get("source_table") or "") in {"net_schedule_tab", "net_schedule"} for row in schedule_refs)
    source_refs: List[str] = []
    if has_daily:
        source_refs.append("hf_daily")
    if has_net:
        source_refs.append("hf_nets")
    plan_name = _coerce_text(operating_plan_name, "") or "Migrated Single-Rig Schedule"
    plan = _save_frequency_plan_conn(
        conn,
        {
            "system_key": DEFAULT_FREQUENCY_PLAN_SYSTEM_KEY,
            "name": plan_name,
            "description": "Created from existing single-rig station schedule rows during Multi-Rig migration.",
            "category": "normal",
            "status": "saved",
            "source_refs": source_refs,
            "schedule_refs": schedule_refs,
            "frequency_refs": _frequency_refs_from_schedule_refs(schedule_refs),
            "group_refs": _group_refs_from_schedule_refs(schedule_refs),
            "notes": f"Migrated from single-rig schedules {_utc_now_iso()}.",
        },
    )
    _set_assigned_plan_conn(
        conn,
        int(device_id),
        int(plan["id"]),
        reason="Assigned migrated single-rig schedule.",
        created_by="migration",
        commit=False,
    )
    return plan


def ensure_default_multi_radio_records(conn: sqlite3.Connection, settings_values: Mapping[str, Any]) -> None:
    """Create the default multi-rig baseline.

    This helper intentionally writes device, operating, software-instance, assignment,
    and runtime-primary state. It must not be used as a startup convenience for an
    existing FIO install before the migration marker is current. Use
    ``ensure_multi_rig_migration`` for fresh-install/default migration flows.
    """
    ensure_multi_radio_settings_schema(conn)
    js8 = _record_by_system_key(conn, "js8_instances", DEFAULT_JS8_INSTANCE_SYSTEM_KEY)
    if not js8:
        js8 = _save_js8_instance_conn(conn, _seed_js8_defaults(settings_values))
    fast_light = _record_by_system_key(conn, "fast_light_configs", DEFAULT_FAST_LIGHT_SYSTEM_KEY)
    if not fast_light:
        fast_light = _save_fast_light_config_conn(conn, _seed_fast_light_defaults(settings_values))
    varac = _record_by_system_key(conn, "varac_nodes", DEFAULT_VARAC_NODE_SYSTEM_KEY)
    if not varac:
        varac = _save_varac_node_conn(conn, _seed_varac_defaults(settings_values))
    operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
    if not operating:
        operating = _save_operating_profile_conn(conn, _seed_operating_defaults(settings_values))
    _ensure_receive_only_operating_profile_conn(conn, commit=False)
    device = _record_by_system_key(conn, "device_profiles", DEFAULT_DEVICE_SYSTEM_KEY)
    if not device:
        device = MultiRadioStore._save_device_profile_conn(
            conn,
            _seed_device_defaults(
                settings_values,
                js8_instance_id=int(js8["id"]),
                fast_light_config_id=int(fast_light["id"]),
                varac_node_id=int(varac["id"]),
            ),
        )
    else:
        now_iso = _utc_now_iso()
        conn.execute(
            """
            UPDATE device_profiles
               SET js8_instance_id=COALESCE(js8_instance_id, ?),
                   fast_light_config_id=COALESCE(fast_light_config_id, ?),
                   varac_node_id=COALESCE(varac_node_id, ?),
                   updated_utc=?
             WHERE id=?
            """,
            (int(js8["id"]), int(fast_light["id"]), int(varac["id"]), now_iso, int(device["id"])),
        )
        conn.commit()
        device = _record_by_id(conn, "device_profiles", int(device["id"])) or device
    _ensure_default_assignment(conn, int(device["id"]), int(operating["id"]))
    _normalize_runtime_primary_device(conn, allow_pre_migration=True)
    log.debug("MultiRadioStore: ensured default multi-radio records for the compatibility baseline.")


def _normalize_software_roles(value: Optional[Iterable[str]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    valid: List[str] = []
    unknown: List[str] = []
    for role in value:
        normalized = _coerce_text(role, "").lower()
        if not normalized:
            continue
        if normalized in SUPPORTED_SOFTWARE_ROLES:
            valid.append(normalized)
        else:
            unknown.append(normalized)
    return tuple(sorted(set(valid))), tuple(sorted(set(unknown)))


def multi_rig_guardrail_warnings(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return text-formatted non-fatal warnings for compatibility callers."""
    ensure_multi_radio_settings_schema(conn)
    return format_multi_rig_guardrail_warnings(collect_multi_rig_guardrail_warnings(conn))


def _apply_launch_safety_migration_v2(conn: sqlite3.Connection) -> None:
    ensure_multi_radio_settings_schema(conn)
    now_iso = _utc_now_iso()
    fallback_names = tuple(sorted(FALLBACK_RADIO_NAMES))
    fallback_placeholders = ", ".join("?" for _ in fallback_names)
    conn.execute("UPDATE device_profiles SET launch_enabled=0, updated_utc=?", (now_iso,))
    conn.execute(
        f"""
        UPDATE device_profiles
           SET needs_operator_name=1,
               updated_utc=?
         WHERE LOWER(TRIM(COALESCE(name, ''))) IN ({fallback_placeholders})
        """,
        (now_iso, *fallback_names),
    )
    conn.execute(
        f"""
        UPDATE device_profiles
           SET needs_operator_name=0,
               updated_utc=?
         WHERE LOWER(TRIM(COALESCE(name, ''))) NOT IN ({fallback_placeholders})
        """,
        (now_iso, *fallback_names),
    )
    conn.execute("UPDATE operating_profiles SET use_launch_control=0, updated_utc=?", (now_iso,))
    conn.execute("UPDATE runtime_policies SET launch_enabled=0, updated_utc=?", (now_iso,))
    conn.commit()


def ensure_multi_rig_migration(
    conn: sqlite3.Connection,
    settings_values: Mapping[str, Any],
    *,
    radio_name: str = "",
    radio_model: str = "",
    radio_manufacturer: str = "",
    operating_plan_name: str = "",
    enabled_software_roles: Optional[Iterable[str]] = None,
    target_version: int = CURRENT_MULTI_RIG_MIGRATION_VERSION,
    defer: bool = False,
    dry_run: bool = False,
) -> MigrationResult:
    """Migrate a single-rig configuration into the multi-rig baseline.

    This function intentionally delegates to helpers that may commit during default
    record creation and marker updates. Callers should treat it as a complete
    migration operation, not as a nested transaction participant.
    """
    ensure_multi_radio_settings_schema(conn)
    from_version = get_multi_rig_migration_version(conn)
    to_version = int(target_version or CURRENT_MULTI_RIG_MIGRATION_VERSION)
    if from_version >= to_version:
        return MigrationResult(
            already_current=True,
            applied=False,
            deferred=False,
            from_version=from_version,
            to_version=to_version,
        )
    if defer:
        if not dry_run:
            set_multi_rig_migration_deferred(conn, True)
        return MigrationResult(
            already_current=False,
            applied=False,
            deferred=True,
            from_version=from_version,
            to_version=to_version,
        )

    roles, unknown_roles = _normalize_software_roles(enabled_software_roles)
    warnings = tuple(f"Unknown software role ignored: {role}" for role in unknown_roles)
    if dry_run:
        return MigrationResult(
            already_current=False,
            applied=True,
            deferred=False,
            from_version=from_version,
            to_version=to_version,
            enabled_software_roles=roles,
            warnings=warnings,
        )

    try:
        # Version 0 is the legacy single-rig conversion and owns creation of
        # the compatibility baseline. Incremental migrations must never invent
        # a transceiver/default assignment in an already migrated blank-slate
        # or observer-only station.
        if from_version <= 0:
            ensure_default_multi_radio_records(conn, settings_values)
        device = _record_by_system_key(conn, "device_profiles", DEFAULT_DEVICE_SYSTEM_KEY)
        operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
        js8 = _record_by_system_key(conn, "js8_instances", DEFAULT_JS8_INSTANCE_SYSTEM_KEY)
        fast_light = _record_by_system_key(conn, "fast_light_configs", DEFAULT_FAST_LIGHT_SYSTEM_KEY)
        varac = _record_by_system_key(conn, "varac_nodes", DEFAULT_VARAC_NODE_SYSTEM_KEY)

        if device:
            updates: Dict[str, Any] = {"updated_utc": _utc_now_iso()}
            display_name = _coerce_text(radio_name, "")
            if display_name:
                updates["name"] = display_name
                updates["needs_operator_name"] = 0
            else:
                updates["needs_operator_name"] = _needs_operator_radio_name(device.get("name", DEFAULT_DEVICE_NAME), device.get("needs_operator_name"))
            manufacturer = _coerce_text(radio_manufacturer, "")
            if manufacturer:
                updates["radio_manufacturer"] = manufacturer
            model = _coerce_text(radio_model, "")
            if model:
                updates["radio_model"] = model
            if roles:
                updates.update(
                    {
                        "use_js8call": 1 if "js8call" in roles else 0,
                        "use_flrig": 1 if "fast_light" in roles else 0,
                        "use_fldigi": 1 if "fast_light" in roles else 0,
                        "use_varac": 1 if "varac" in roles else 0,
                        "use_flamp": 1 if "flamp" in roles else 0,
                        "use_flmsg": 1 if "flmsg" in roles else 0,
                        "use_js8spotter": 1 if "js8spotter" in roles else 0,
                        "use_commstat": 1 if "commstat" in roles else 0,
                    }
                )
            assignments = ", ".join(f"{key}=?" for key in updates)
            conn.execute(
                f"UPDATE device_profiles SET {assignments} WHERE id=?",
                [updates[key] for key in updates] + [int(device["id"])],
            )

        if operating:
            explicit_plan_name = _coerce_text(operating_plan_name, "")
            existing_plan_name = _coerce_text(operating.get("name", ""), "")
            fallback_names = {
                DEFAULT_OPERATING_NAME,
                "Default Operating Profile",
                "Daily HF Schedule",
                "Migrated Single-Rig Plan",
                "",
            }
            if explicit_plan_name or existing_plan_name in fallback_names:
                plan_name = explicit_plan_name or DEFAULT_OPERATING_NAME
                conn.execute(
                    "UPDATE operating_profiles SET name=?, updated_utc=? WHERE id=?",
                    (plan_name, _utc_now_iso(), int(operating["id"])),
                )
        migrated_frequency_plan = None
        if device:
            migrated_frequency_plan = _ensure_migrated_single_rig_frequency_plan(
                conn,
                device_id=int(device["id"]),
                operating_plan_name=operating_plan_name,
            )

        if from_version < 2 <= to_version:
            _apply_launch_safety_migration_v2(conn)

        receiver_operating = None
        if from_version < 3 <= to_version:
            receiver_operating = _ensure_receive_only_operating_profile_conn(conn, commit=False)

        guardrail_warnings = multi_rig_guardrail_warnings(conn)
        all_warnings = warnings + tuple(warn for warn in guardrail_warnings if warn not in warnings)
        set_multi_rig_migration_deferred(conn, False)
        set_multi_rig_migration_version(
            conn,
            to_version,
            summary={
                "from_version": from_version,
                "to_version": to_version,
                "created_device_profile_id": int(device["id"]) if device else None,
                "created_operating_profile_id": int(operating["id"]) if operating else None,
                "created_frequency_plan_id": int(migrated_frequency_plan["id"]) if migrated_frequency_plan else None,
                "receive_only_operating_profile_id": (
                    int(receiver_operating["id"]) if receiver_operating else None
                ),
                "enabled_software_roles": list(roles),
                "warnings": list(all_warnings),
            },
        )
        conn.commit()
        return MigrationResult(
            already_current=False,
            applied=True,
            deferred=False,
            from_version=from_version,
            to_version=to_version,
            created_device_profile_id=int(device["id"]) if device else None,
            created_operating_profile_id=int(operating["id"]) if operating else None,
            created_frequency_plan_id=int(migrated_frequency_plan["id"]) if migrated_frequency_plan else None,
            created_js8_instance_id=int(js8["id"]) if js8 else None,
            created_fast_light_config_id=int(fast_light["id"]) if fast_light else None,
            created_varac_node_id=int(varac["id"]) if varac else None,
            enabled_software_roles=roles,
            warnings=all_warnings,
        )
    except Exception as exc:
        log.error("MultiRadioStore: multi-rig migration failed: %s", exc)
        return MigrationResult(
            already_current=False,
            applied=False,
            deferred=False,
            from_version=from_version,
            to_version=to_version,
            enabled_software_roles=roles,
            warnings=warnings + (f"Migration failed: {exc}",),
        )


def project_runtime_active_device_to_legacy_settings(
    conn: sqlite3.Connection,
    device_profile_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    if not is_multi_rig_migration_current(conn):
        return None
    if device_profile_id is None:
        device_profile_id = _normalize_runtime_primary_device(conn)
    if device_profile_id is None:
        return None
    raw = _record_by_id(conn, "device_profiles", int(device_profile_id))
    if not raw:
        return None
    resolved = _resolve_device_profile_links_conn(conn, raw)
    existing_settings = _load_kv_settings(conn)
    updates = _legacy_settings_projection_from_device(resolved, existing_settings)
    _write_kv_settings(conn, updates)
    conn.commit()
    return updates


def project_runtime_active_device_to_legacy_settings_if_single_active(
    conn: sqlite3.Connection,
    device_profile_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    ensure_multi_radio_settings_schema(conn)
    active_count = _runtime_active_device_count(conn)
    if active_count > 1:
        log.debug(
            "MultiRadioStore: skipped legacy settings projection because %s radios are active; "
            "per-radio endpoint/settings saves remain scoped to device_profiles.",
            active_count,
        )
        return None
    return project_runtime_active_device_to_legacy_settings(conn, device_profile_id)


def mirror_legacy_settings_into_runtime_active_device(
    conn: sqlite3.Connection,
    settings_values: Mapping[str, Any],
    *,
    keys_changed: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, Any]]:
    # The caller may hold a stale whole-station snapshot. Only the explicitly
    # changed keys have write authority; runtime observations never have it.
    changed = {str(key) for key in (settings_values if keys_changed is None else keys_changed)}
    changed &= MIRRORED_LEGACY_KEYS & settings_values.keys()
    changed -= {"varac_bbs_vault_runtime_state_v1", "varac_bbs_vault_last_summary"}
    if not changed:
        return None
    settings_values = {key: settings_values[key] for key in changed}
    # Schema/migration belongs to startup, not an ordinary settings write. Those
    # helpers commit internally and would split the caller's atomic transaction.
    version_row = conn.execute("SELECT value FROM kv WHERE key=?", (MULTI_RIG_MIGRATION_VERSION_KEY,)).fetchone()
    if not version_row:
        return None
    try:
        version = json.loads(version_row[0])
    except (ValueError, TypeError):
        version = version_row[0]
    if _coerce_int(version, 0) < CURRENT_MULTI_RIG_MIGRATION_VERSION:
        return None
    active_count = _runtime_active_device_count(conn)
    if active_count > 1:
        log.debug(
            "MultiRadioStore: skipped legacy settings mirror because %s radios are active; "
            "per-radio endpoint/settings saves must be explicit.",
            active_count,
        )
        return None
    # Compatibility writes must not normalize selection/primary ownership.
    active_row = conn.execute(
        "SELECT id FROM device_profiles WHERE enabled=1 AND runtime_active=1 "
        "AND device_class<>'observer' ORDER BY runtime_primary DESC, display_order, id LIMIT 1"
    ).fetchone()
    active_id = int(active_row[0]) if active_row else None
    if active_id is None:
        return None
    existing = _record_by_id(conn, "device_profiles", int(active_id))
    if not existing:
        return None

    control_backend = (
        _normalize_control_backend(settings_values) if "control_via" in changed
        else _coerce_text(existing.get("control_backend"), "manual")
    )
    flrig_host = _settings_text(settings_values, "flrig_host", "")
    fldigi_host = _settings_text(settings_values, "fldigi_host", "")
    js8_host = _settings_text(settings_values, "js8_host", "")
    linked = _resolve_device_profile_links_conn(conn, dict(existing))
    launch_path = _coerce_text(existing.get("launch_path", ""), "")
    if control_backend == "flrig":
        launch_path = _settings_text(settings_values, "path_flrig", linked.get("flrig_path", ""))
    elif control_backend == "js8call":
        launch_path = _settings_text(settings_values, "path_js8call", linked.get("js8_install_path", ""))
    message_paths = settings_values.get("message_paths", {}) or {}
    if not isinstance(message_paths, Mapping):
        message_paths = {}
    updates = {
        "control_backend": control_backend,
        "rig_host": _settings_text(settings_values, "rig_host", ""),
        "rig_port": _settings_optional_int(settings_values, "rig_port"),
        "flrig_host": flrig_host,
        "flrig_port": _settings_int(settings_values, "flrig_port", 12345),
        "fldigi_host": fldigi_host,
        "fldigi_port": _settings_int(settings_values, "fldigi_port", 7362),
        "fldigi_log_path": _settings_text(settings_values, "fldigi_log_path", ""),
        "fldigi_checkin_dir": _settings_text(settings_values, "fldigi_checkin_dir", ""),
        "flmsg_path": _settings_text(settings_values, "path_flmsg", ""),
        "flmsg_message_path": _coerce_text(message_paths.get("flmsg", ""), ""),
        "flamp_path": _settings_text(settings_values, "path_flamp", ""),
        "flamp_message_path": _coerce_text(message_paths.get("flamp", ""), ""),
        "js8_host": js8_host,
        "js8_port": _settings_int(settings_values, "js8_port", 2442),
        "js8_profile_path": _settings_text(settings_values, "js8_profile_path", ""),
        "js8_directed_path": _settings_text(settings_values, "js8_directed_path", ""),
        "js8_forms_path": _settings_text(settings_values, "js8_forms_path", ""),
        "varac_install_path": _settings_text(settings_values, "varac_path", ""),
        "varac_db_path": _settings_text(settings_values, "varac_db_path", ""),
        "varac_ini_path": _settings_text(settings_values, "varac_ini_path", ""),
        "varac_outbox_dir": _settings_text(settings_values, "varac_outbox_dir", ""),
        "varac_bbs_dir": _settings_text(settings_values, "varac_bbs_dir", ""),
        "varac_bbs_archive_dir": _settings_text(settings_values, "varac_bbs_archive_dir", ""),
        "varac_bbs_enabled": _coerce_bool_int(settings_values.get("varac_bbs_enabled"), False),
        "varac_bbs_limit_access_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_limit_access_enabled"),
            False,
        ),
        "varac_bbs_allowed_callsigns": _settings_text(settings_values, "varac_bbs_allowed_callsigns", ""),
        "varac_bbs_allowed_group_sources": _settings_text(settings_values, "varac_bbs_allowed_group_sources", ""),
        "varac_guard_access_policy_v1": _settings_text(settings_values, "varac_guard_access_policy_v1", ""),
        "varac_guard_allow_bbs_allowed_callsigns": _coerce_bool_int(settings_values["varac_guard_allow_bbs_allowed_callsigns"], True) if "varac_guard_allow_bbs_allowed_callsigns" in settings_values else None,
        "varac_guard_allow_operator_trusted": _coerce_bool_int(settings_values["varac_guard_allow_operator_trusted"], True) if "varac_guard_allow_operator_trusted" in settings_values else None,
        "varac_bbs_announce_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_announce_enabled"),
            False,
        ),
        "varac_bbs_auto_archive_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_auto_archive_enabled"),
            False,
        ),
        "varac_bbs_auto_archive_days": _settings_int(settings_values, "varac_bbs_auto_archive_days", 14),
        "varac_bbs_vault_enabled": _coerce_bool_int(settings_values.get("varac_bbs_vault_enabled"), False),
        "varac_bbs_vault_managed_root": _settings_text(settings_values, "varac_bbs_vault_managed_root", ""),
        "varac_bbs_vault_default_location_id": _settings_text(
            settings_values,
            "varac_bbs_vault_default_location_id",
            "",
        ),
        "varac_bbs_vault_global_code_policy": _settings_text(
            settings_values,
            "varac_bbs_vault_global_code_policy",
            "",
        ),
        "varac_bbs_vault_trigger_mode": _settings_text(settings_values, "varac_bbs_vault_trigger_mode", ""),
        "varac_bbs_vault_return_mode": _settings_text(settings_values, "varac_bbs_vault_return_mode", ""),
        "varac_bbs_vault_failed_attempt_limit": _settings_int(
            settings_values,
            "varac_bbs_vault_failed_attempt_limit",
            3,
        ),
        "varac_bbs_vault_failed_attempt_window_seconds": _settings_int(
            settings_values,
            "varac_bbs_vault_failed_attempt_window_seconds",
            900,
        ),
        "varac_bbs_vault_cooldown_seconds": _settings_int(
            settings_values,
            "varac_bbs_vault_cooldown_seconds",
            1800,
        ),
        "varac_bbs_vault_idle_timeout_seconds": _settings_int(
            settings_values,
            "varac_bbs_vault_idle_timeout_seconds",
            600,
        ),
        "varac_bbs_vault_flamp_enabled": _coerce_bool_int(
            settings_values.get("varac_bbs_vault_flamp_enabled"),
            False,
        ),
        "varac_bbs_vault_flamp_relay_dir": _settings_text(
            settings_values,
            "varac_bbs_vault_flamp_relay_dir",
            "",
        ),
        "varac_bbs_vault_flamp_listing_max_age_days": _settings_int(
            settings_values,
            "varac_bbs_vault_flamp_listing_max_age_days",
            14,
        ),
        "varac_bbs_vault_locations_v1": _coerce_json_list_text(
            settings_values.get("varac_bbs_vault_locations_v1", [])
        ),
        "varac_bbs_sweeper_rules_v1": _coerce_json_list_text(
            settings_values.get("varac_bbs_sweeper_rules_v1", [])
        ),
        "varac_bbs_vault_runtime_state_v1": _coerce_json_object_text(
            settings_values.get("varac_bbs_vault_runtime_state_v1", {})
        ),
        "varac_bbs_vault_last_summary": _settings_text(settings_values, "varac_bbs_vault_last_summary", ""),
        "scheduler_enabled": _settings_bool(settings_values, "use_scheduler", True),
        "schedule_hold_minutes_default": _normalize_hold_duration_minutes(
            settings_values.get("schedule_hold_minutes_default", DEFAULT_HOLD_DURATION_MINUTES)
        ),
        "freq_enforcement_mode": _settings_text(
            settings_values,
            "freq_enforcement_mode",
            DEFAULT_TIMER_ENFORCEMENT_MODE,
        ),
        "freq_prompt_interval": _settings_text(
            settings_values,
            "freq_prompt_interval",
            DEFAULT_TIMER_PROMPT_INTERVAL,
        ),
        "fldigi_enforcement_mode": _settings_text(
            settings_values,
            "fldigi_enforcement_mode",
            DEFAULT_TIMER_ENFORCEMENT_MODE,
        ),
        "fldigi_prompt_interval": _settings_text(
            settings_values,
            "fldigi_prompt_interval",
            DEFAULT_TIMER_PROMPT_INTERVAL,
        ),
        "js8_enforcement_mode": _settings_text(
            settings_values,
            "js8_enforcement_mode",
            DEFAULT_TIMER_ENFORCEMENT_MODE,
        ),
        "js8_prompt_interval": _settings_text(
            settings_values,
            "js8_prompt_interval",
            DEFAULT_TIMER_PROMPT_INTERVAL,
        ),
        "launch_enabled": _coerce_bool_int(settings_values.get("launch_control_enabled"), False),
        "launch_path": launch_path,
        "launch_cmd": _settings_text(settings_values, "varac_launch_cmd", ""),
        "updated_utc": _utc_now_iso(),
    }
    # Source -> destination mappings are deliberately field-scoped. A batch may
    # edit several domains, but cannot refill their unchanged fields from cache.
    aliases = {
        "control_backend": "control_via", "scheduler_enabled": "use_scheduler",
        "launch_enabled": "launch_control_enabled", "launch_cmd": "varac_launch_cmd",
        "varac_install_path": "varac_path", "flmsg_path": "path_flmsg",
        "flamp_path": "path_flamp", "flmsg_message_path": "message_paths",
        "flamp_message_path": "message_paths",
    }
    updates = {column: value for column, value in updates.items()
               if aliases.get(column, column) in changed}
    path_key = {"flrig": "path_flrig", "js8call": "path_js8call"}.get(control_backend)
    if "control_via" in changed or (path_key and path_key in changed):
        updates["launch_path"] = launch_path

    js8_fields = {
        "js8_host": "host", "js8_port": "port", "js8_offset_hz": "offset_hz",
        "js8_profile_path": "profile_path", "js8_directed_path": "directed_path",
        "js8_forms_path": "forms_path", "path_js8call": "install_path",
        "path_js8spotter": "spotter_launch_path", "path_commstat": "commstat_launch_path",
    }
    fast_fields = {
        "path_flrig": "flrig_path", "flrig_host": "flrig_host", "flrig_port": "flrig_port",
        "path_fldigi": "fldigi_path", "fldigi_host": "fldigi_host", "fldigi_port": "fldigi_port",
        "fldigi_log_path": "fldigi_log_path", "fldigi_checkin_dir": "fldigi_checkin_dir",
    }
    varac_fields = {
        "varac_path": "install_path", "varac_db_path": "db_path",
        "varac_ini_path": "ini_path", "varac_launch_cmd": "launch_cmd",
    }
    js8_updates = {column: _settings_text(settings_values, key, "") for key, column in js8_fields.items() if key in changed}
    if "js8_host" in changed:
        js8_updates["host"] = js8_host
    if "js8_port" in changed:
        js8_updates["port"] = _settings_int(settings_values, "js8_port", 2442)
    if "js8_offset_hz" in changed:
        js8_updates["offset_hz"] = coerce_js8_offset_hz(_settings_int(settings_values, "js8_offset_hz", 0))
    fast_updates = {column: _settings_text(settings_values, key, "") for key, column in fast_fields.items() if key in changed}
    for key in ("flrig_host", "flrig_port", "fldigi_host", "fldigi_port", "fldigi_log_path", "fldigi_checkin_dir"):
        if key in changed:
            fast_updates[key] = updates[key]
    varac_updates = {column: _settings_text(settings_values, key, "") for key, column in varac_fields.items() if key in changed}
    if "varac_incoming_path" in changed:
        varac_updates["incoming_path"] = _settings_text(settings_values, "varac_incoming_path", "")
    elif "message_paths" in changed:
        varac_updates["incoming_path"] = _coerce_text(message_paths.get("varac", ""), "")

    assignment = _effective_assignment_for_device(conn, active_id)
    operating_updates = {}
    if "use_scheduler" in changed:
        operating_updates["scheduler_enabled"] = updates["scheduler_enabled"]
    if "launch_control_enabled" in changed:
        operating_updates["use_launch_control"] = updates["launch_enabled"]

    targets = [("device_profiles", active_id, updates)]
    for table, link, patch in (
        ("js8_instances", "js8_instance_id", js8_updates),
        ("fast_light_configs", "fast_light_config_id", fast_updates),
        ("varac_nodes", "varac_node_id", varac_updates),
    ):
        record_id = _coerce_optional_int(existing.get(link))
        if record_id is None or not patch:
            continue
        # Even an inactive sibling owns its saved configuration. Legacy global
        # editing cannot choose which consumer of a shared identity to change.
        if conn.execute(f"SELECT 1 FROM device_profiles WHERE {link}=? AND id<>? LIMIT 1",
                        (record_id, active_id)).fetchone():
            log.warning("MultiRadioStore: skipped legacy mirror of shared %s; use scoped configuration", table)
            return None
        targets.append((table, record_id, patch))
    if assignment and operating_updates:
        operating_id = int(assignment["operating_profile_id"])
        if conn.execute(
            "SELECT 1 FROM operating_profile_assignments WHERE operating_profile_id=? "
            "AND device_profile_id<>? AND assignment_state IN (?, ?) LIMIT 1",
            (operating_id, active_id, *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
        ).fetchone():
            log.warning("MultiRadioStore: skipped legacy mirror of shared Operating Model; use scoped configuration")
            return None
        targets.append(("operating_profiles", operating_id, operating_updates))
    for table, record_id, patch in targets:
        current = _record_by_id(conn, table, record_id)
        if current is None:
            continue
        actual = {key: value for key, value in patch.items() if current.get(key) != value}
        if not actual:
            continue
        actual["updated_utc"] = _utc_now_iso()
        assignments = ", ".join(f"{column}=?" for column in actual)
        conn.execute(f"UPDATE {table} SET {assignments} WHERE id=?",
                     [*actual.values(), record_id])
    # The SettingsManager caller owns commit/rollback, including its kv write.
    return _resolve_device_profile_links_conn(conn, _record_by_id(conn, "device_profiles", active_id) or existing)


class _GuidedTransactionConnection:
    """Connection facade that keeps nested store helpers in one transaction.

    Existing store methods intentionally own their normal transaction boundary.
    Guided radio save is the one workflow that must compose several of those
    methods atomically.  While that workflow is active on this thread, commits,
    rollbacks, and nested BEGIN statements are deferred to the outer owner.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> "_GuidedTransactionConnection":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        statement = str(sql or "").strip().upper().rstrip(";")
        if statement.startswith("BEGIN"):
            return self._connection.execute("SELECT 1")
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
        return self._connection.executemany(sql, parameters)

    def executescript(self, sql: str) -> sqlite3.Cursor:
        return self._connection.executescript(sql)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class GuidedSaveTransaction:
    """Outer transaction for one reviewed Add/Edit Radio activation."""

    def __init__(self, store: "MultiRadioStore") -> None:
        self._store = store
        self._connection: sqlite3.Connection | None = None
        self._complete = False

    def __enter__(self) -> "GuidedSaveTransaction":
        if getattr(self._store._transaction_local, "connection", None) is not None:
            raise RuntimeError("A guided radio save transaction is already active on this thread.")
        connection = self._store._open_connection()
        connection.execute("BEGIN IMMEDIATE")
        self._connection = connection
        self._store._transaction_local.connection = connection
        return self

    def complete(self) -> None:
        """Mark the full reviewed plan successful and eligible to commit."""

        self._complete = True

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        connection = self._connection
        self._store._transaction_local.connection = None
        if connection is None:
            return False
        try:
            if exc_type is None and self._complete:
                try:
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            else:
                connection.rollback()
        finally:
            connection.close()
            self._connection = None
        return False


class _ConfigurationSavepoint:
    """Join an existing CP save without an independent commit or partial failure."""
    def __init__(self, connection):
        self.connection = connection
        self.name = 'configuration_' + uuid.uuid4().hex
        self.done = False

    def __enter__(self):
        self.connection.execute('SAVEPOINT ' + self.name)
        return self

    def complete(self):
        self.done = True

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None or not self.done:
            self.connection.execute('ROLLBACK TO SAVEPOINT ' + self.name)
        self.connection.execute('RELEASE SAVEPOINT ' + self.name)
        return False


class MultiRadioStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else settings_db_path()
        # Startup/explicit administration owns migration. Compatibility callers
        # may still arrive first, so assure once per store instead of walking
        # every settings table and column on every runtime operation.
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._transaction_local = threading.local()

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        if not self._schema_ready:
            with self._schema_lock:
                if not self._schema_ready:
                    ensure_multi_radio_settings_schema(conn)
                    self._schema_ready = True
        return conn

    def _connect(self) -> sqlite3.Connection:
        active = getattr(self._transaction_local, "connection", None)
        if isinstance(active, sqlite3.Connection):
            return _GuidedTransactionConnection(active)  # type: ignore[return-value]
        return self._open_connection()

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open a migrated settings snapshot without repair or normalization."""

        active = getattr(self._transaction_local, "connection", None)
        if isinstance(active, sqlite3.Connection):
            return _GuidedTransactionConnection(active)  # type: ignore[return-value]

        return connect_sqlite_readonly(
            self.db_path,
            timeout=2.0,
            row_factory=sqlite3.Row,
            busy_timeout_ms=2000,
        )

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def connect_readonly(self) -> sqlite3.Connection:
        """Open a runtime read connection without schema assurance or repair.

        Startup owns schema migration.  Periodic schedulers, status services, and
        UI projections must use this path so a read can never turn into DDL or
        contend as a writer merely by opening the database.
        """

        return self._connect_readonly()

    @contextmanager
    def configuration_read_transaction(self):
        """Reuse a caller snapshot, otherwise bind store reads to a read-only transaction."""
        if getattr(self._transaction_local, 'connection', None) is not None:
            yield
            return
        connection = self._connect_readonly()
        try:
            connection.execute('BEGIN')
            self._transaction_local.connection = connection
            yield
        finally:
            self._transaction_local.connection = None
            connection.close()

    def configuration_save_transaction(self):
        """Compose operator app/radio saves under one existing atomic owner."""
        active = getattr(self._transaction_local, 'connection', None)
        return _ConfigurationSavepoint(active) if active is not None else self.guided_save_transaction()

    def guided_save_transaction(self) -> GuidedSaveTransaction:
        """Return the atomic boundary for one reviewed guided radio save.

        The context rolls back unless ``complete()`` is called.  Consequently,
        validation failures and early returns cannot leave a partial radio,
        assignment, software instance, manifest, schedule, or launch bundle.
        """

        return GuidedSaveTransaction(self)

    def save_radio_launch_bundle(
        self,
        radio_profile_id: int,
        *,
        launch_enabled: bool,
        items: Any,
    ) -> None:
        """Persist a receiver launch bundle inside the active save boundary.

        ``LaunchBundleStore`` remains the normal administration API.  This
        narrow equivalent lives here because its rows are part of guided radio
        activation and therefore must share the same SQLite connection as the
        new radio, assignments, and managed software instances.
        """

        radio_id = int(radio_profile_id)
        raw_items = items if isinstance(items, (list, tuple)) else ()
        normalized: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name", "") or "").strip()
            instance_key = str(raw.get("instance_key", name) or name).strip()
            if not name or not instance_key or instance_key.casefold() in seen:
                continue
            seen.add(instance_key.casefold())
            dependencies = raw.get("dependencies", ())
            if not isinstance(dependencies, (list, tuple)):
                dependencies = ()
            readiness = raw.get("readiness_policy", {})
            if not isinstance(readiness, Mapping):
                readiness = {}
            normalized_readiness = dict(readiness)
            execution_scope = str(raw.get("execution_scope", "") or "").strip()
            if execution_scope:
                normalized_readiness["execution_scope"] = execution_scope
            normalized.append(
                {
                    "name": name,
                    "instance_key": instance_key,
                    "enabled": _coerce_bool_int(raw.get("enabled", True), True),
                    "startup": _coerce_bool_int(raw.get("startup", False), False),
                    "monitor_health": _coerce_bool_int(
                        raw.get("monitor_health", raw.get("enabled", True)), True
                    ),
                    "command": str(raw.get("launch_command_override", "") or "").strip(),
                    "path": str(raw.get("launch_path_override", "") or "").strip(),
                    "dependencies": [
                        str(value).strip() for value in dependencies if str(value).strip()
                    ],
                    "readiness": normalized_readiness,
                }
            )

        now_iso = _utc_now_iso()
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM device_profiles WHERE id=?", (radio_id,)
            ).fetchone() is None:
                raise KeyError(f"Unknown radio profile id: {radio_id}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO radio_launch_bundles
                        (radio_profile_id, schema_version, launch_enabled,
                         migrated_from_legacy, updated_utc)
                    VALUES (?, 1, ?, 0, ?)
                    ON CONFLICT(radio_profile_id) DO UPDATE SET
                        schema_version=1,
                        launch_enabled=excluded.launch_enabled,
                        updated_utc=excluded.updated_utc
                    """,
                    (radio_id, 1 if launch_enabled else 0, now_iso),
                )
                conn.execute(
                    "DELETE FROM radio_launch_bundle_items WHERE radio_profile_id=?",
                    (radio_id,),
                )
                for order, item in enumerate(normalized):
                    conn.execute(
                        """
                        INSERT INTO radio_launch_bundle_items (
                            radio_profile_id, instance_key, app_name, display_order,
                            enabled, launch_at_startup, monitor_health,
                            command_override, path_override, dependencies_json,
                            readiness_json, updated_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            radio_id,
                            item["instance_key"],
                            item["name"],
                            order,
                            int(bool(item["enabled"])),
                            int(bool(item["startup"])),
                            int(bool(item["monitor_health"])),
                            item["command"],
                            item["path"],
                            json.dumps(item["dependencies"], sort_keys=True),
                            json.dumps(item["readiness"], sort_keys=True),
                            now_iso,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_radio_launch_bundle(self, radio_profile_id: int) -> Dict[str, Any]:
        """Return one exact persisted structured launch bundle."""

        radio_id = int(radio_profile_id)
        with self._connect() as conn:
            bundle = conn.execute(
                "SELECT * FROM radio_launch_bundles WHERE radio_profile_id=?",
                (radio_id,),
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM radio_launch_bundle_items WHERE radio_profile_id=? "
                "ORDER BY display_order, instance_key",
                (radio_id,),
            ).fetchall()
        if bundle is None:
            return {"radio_profile_id": radio_id, "generation": 0, "items": []}
        items: List[Dict[str, Any]] = []
        for raw in rows:
            item = dict(raw)
            for source, target, fallback in (
                ("dependencies_json", "dependencies", []),
                ("readiness_json", "readiness", {}),
            ):
                try:
                    parsed = json.loads(str(item.get(source, "") or ""))
                except (TypeError, ValueError):
                    parsed = fallback
                item[target] = parsed
            items.append(item)
        result = dict(bundle)
        result["items"] = items
        return result

    def get_all_kv_settings(self) -> Dict[str, Any]:
        with self._connect() as conn:
            return _load_kv_settings(conn)

    def read_runtime_status_inputs(
        self,
        *,
        settings_values: Optional[Mapping[str, Any]] = None,
        existing_fio_usage: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Read the runtime-status inputs from one database snapshot."""
        with self._connect() as conn:
            values = dict(settings_values) if settings_values is not None else _load_kv_settings(conn)
            migration_version = get_multi_rig_migration_version(conn)
            migration_deferred = get_multi_rig_migration_deferred(conn)
            migration_current = migration_version >= CURRENT_MULTI_RIG_MIGRATION_VERSION
            if existing_fio_usage is not None:
                detected_usage = bool(existing_fio_usage)
            elif migration_current:
                detected_usage = detect_existing_fio_usage(conn, values)
            else:
                detected_usage = detect_existing_fio_usage(conn, values)
            primary_row: Optional[Dict[str, Any]] = None
            active_rows: List[Dict[str, Any]] = []
            if migration_current:
                primary_row = _runtime_primary_device_profile(conn)
                active_rows = _runtime_active_device_profiles(conn)
            return {
                "settings_values": values,
                "migration_version": migration_version,
                "migration_deferred": migration_deferred,
                "migration_current": migration_current,
                "existing_fio_usage": detected_usage,
                "primary_row": primary_row,
                "active_rows": active_rows,
            }

    @staticmethod
    def _save_device_profile_conn(conn: sqlite3.Connection, values: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(values)
        requested_id = _coerce_optional_int(payload.get("id"))
        existing = _record_by_id(conn, "device_profiles", requested_id) if requested_id is not None else None
        now_iso = _utc_now_iso()
        system_key = _next_system_key(
            conn,
            "device_profiles",
            payload.get("system_key", (existing or {}).get("system_key", DEFAULT_DEVICE_SYSTEM_KEY)),
            exclude_id=requested_id,
        )
        control_backend = _coerce_text(payload.get("control_backend", (existing or {}).get("control_backend", "flrig")), "flrig").lower()
        if control_backend not in SUPPORTED_DEVICE_CONTROL_BACKENDS:
            raise ValueError(f"Unsupported control backend: {control_backend}")
        device_class = _coerce_text(payload.get("device_class", (existing or {}).get("device_class", "tx_rx")), "tx_rx").lower()
        if device_class not in SUPPORTED_DEVICE_CLASSES:
            raise ValueError(f"Unsupported device class: {device_class}")
        deployment_mode = _coerce_text(payload.get("deployment_mode", (existing or {}).get("deployment_mode", "full")), "full").lower()
        if deployment_mode not in SUPPORTED_DEPLOYMENT_MODES:
            raise ValueError(f"Unsupported deployment mode: {deployment_mode}")
        if existing and int(existing.get("runtime_primary", 0) or 0) == 1 and device_class == "observer":
            raise ValueError("Observer / SDR device profiles cannot become the compatibility runtime device.")
        if device_class == "observer" and requested_id is not None and _device_has_varac_cluster_membership(conn, int(requested_id)):
            raise ValueError("Observer / SDR device profiles cannot participate in VarAC clusters.")

        js8_instance_id = _coerce_optional_int(payload.get("js8_instance_id", (existing or {}).get("js8_instance_id")))
        fast_light_config_id = _coerce_optional_int(
            payload.get("fast_light_config_id", (existing or {}).get("fast_light_config_id"))
        )
        varac_node_id = _coerce_optional_int(payload.get("varac_node_id", (existing or {}).get("varac_node_id")))
        if js8_instance_id is not None and not _record_by_id(conn, "js8_instances", int(js8_instance_id)):
            raise KeyError(f"Unknown JS8 instance id: {js8_instance_id}")
        if fast_light_config_id is not None and not _record_by_id(conn, "fast_light_configs", int(fast_light_config_id)):
            raise KeyError(f"Unknown Fast Light config id: {fast_light_config_id}")
        if varac_node_id is not None and not _record_by_id(conn, "varac_nodes", int(varac_node_id)):
            raise KeyError(f"Unknown VarAC node id: {varac_node_id}")

        if device_class == "observer":
            if _coerce_bool_int(payload.get("use_varac", (existing or {}).get("use_varac", 0)), False):
                raise ValueError("Observer / SDR device profiles cannot use VarAC or VarAC clusters.")
            if _coerce_bool_int(payload.get("use_flrig", (existing or {}).get("use_flrig", 0)), False):
                raise ValueError("Observer / SDR Fast Light workflows cannot enable FLRig, CAT, or PTT control.")
            for family, application_id in (
                ("js8call", js8_instance_id),
                ("fast_light", fast_light_config_id),
                ("varac", varac_node_id),
            ):
                if application_id is not None:
                    _validate_observer_software_link_conn(
                        conn,
                        family_key=family,
                        application_id=int(application_id),
                    )

        requested_links = {
            "js8call": js8_instance_id,
            "fast_light": fast_light_config_id,
            "varac": varac_node_id,
        }
        for family, proposed_id in requested_links.items():
            link_column = _SOFTWARE_INSTANCE_ASSIGNMENTS[family][0]
            current_id = _coerce_optional_int((existing or {}).get(link_column))
            if existing is not None and current_id != proposed_id:
                if proposed_id is None:
                    raise ValueError(
                        f"Use disassociate_software_instance() to remove the {family.replace('_', ' ')} "
                        "assignment and its CommsPlan launch links."
                    )
                raise ValueError(
                    f"Use adopt_software_instance(..., replace_existing=True) to explicitly replace "
                    f"the {family.replace('_', ' ')} assignment."
                )
            if current_id != proposed_id:
                _validate_software_instance_radio_ownership_conn(
                    conn,
                    radio_profile_id=requested_id,
                    family_key=family,
                    application_id=proposed_id,
                )
                if proposed_id is not None:
                    _activate_software_application_conn(
                        conn,
                        family_key=family,
                        application_id=proposed_id,
                    )

        flrig_host = _coerce_text(payload.get("flrig_host", (existing or {}).get("flrig_host", "127.0.0.1")), "127.0.0.1") or "127.0.0.1"
        fldigi_host = _coerce_text(payload.get("fldigi_host", (existing or {}).get("fldigi_host", "")), "") or flrig_host or "127.0.0.1"
        js8_host = _coerce_text(payload.get("js8_host", (existing or {}).get("js8_host", "127.0.0.1")), "127.0.0.1") or "127.0.0.1"
        default_use_flrig = (existing or {}).get("use_flrig")
        if default_use_flrig is None:
            default_use_flrig = control_backend == "flrig"
        default_use_fldigi = (existing or {}).get("use_fldigi")
        if default_use_fldigi is None:
            default_use_fldigi = fast_light_config_id is not None or any(
                payload.get(key)
                for key in (
                    "fldigi_path",
                    "fldigi_host",
                    "fldigi_port",
                    "fldigi_log_path",
                    "fldigi_checkin_dir",
                )
            )
        default_use_flmsg = (existing or {}).get("use_flmsg")
        if default_use_flmsg is None:
            default_use_flmsg = bool(payload.get("flmsg_path") or payload.get("flmsg_message_path"))
        default_use_flamp = (existing or {}).get("use_flamp")
        if default_use_flamp is None:
            default_use_flamp = bool(payload.get("flamp_path") or payload.get("flamp_message_path"))
        default_use_js8call = (existing or {}).get("use_js8call")
        if default_use_js8call is None:
            default_use_js8call = (
                control_backend == "js8call"
                or js8_instance_id is not None
                or any(
                    payload.get(key)
                    for key in (
                        "js8_host",
                        "js8_port",
                        "js8_profile_path",
                        "js8_directed_path",
                        "js8_forms_path",
                    )
                )
            )
        default_use_js8spotter = (existing or {}).get("use_js8spotter")
        if default_use_js8spotter is None:
            # FIO Spotter is an explicit capability selection.  A JS8Call
            # assignment or a discovered companion path is evidence for the
            # chooser, not permission to select the service for the operator.
            default_use_js8spotter = False
        default_use_commstat = (existing or {}).get("use_commstat")
        if default_use_commstat is None:
            # CommStat is likewise explicit.  Do not infer station-service
            # ownership merely because this radio has a JS8 endpoint.
            default_use_commstat = False
        default_use_varac = (existing or {}).get("use_varac")
        if default_use_varac is None:
            default_use_varac = varac_node_id is not None
        existing_name = _coerce_text((existing or {}).get("name", ""), "")
        display_name = _coerce_text(payload.get("name", existing_name or "Device Profile"), "Device Profile") or "Device Profile"
        sdr_adapter = _coerce_text(
            payload.get("sdr_adapter", (existing or {}).get("sdr_adapter", "manual")),
            "manual",
        ).lower().replace("-", "_")
        if sdr_adapter not in SUPPORTED_RECEIVER_ADAPTERS:
            raise ValueError(f"Unsupported receiver adapter: {sdr_adapter}")
        sdr_verification_state = _coerce_text(
            payload.get(
                "sdr_verification_state",
                (existing or {}).get("sdr_verification_state", "manual"),
            ),
            "manual",
        ).lower()
        if sdr_verification_state not in SUPPORTED_RECEIVER_VERIFICATION_STATES:
            raise ValueError(f"Unsupported receiver verification state: {sdr_verification_state}")
        needs_operator_name_value = payload.get("needs_operator_name")
        if needs_operator_name_value in (None, "") and display_name == existing_name:
            needs_operator_name_value = (existing or {}).get("needs_operator_name")
        record = {
            "system_key": system_key,
            "name": display_name,
            "radio_catalog_id": _coerce_text(payload.get("radio_catalog_id", (existing or {}).get("radio_catalog_id", "")), ""),
            "radio_manufacturer": _coerce_text(
                payload.get("radio_manufacturer", (existing or {}).get("radio_manufacturer", "")),
                "",
            ),
            "radio_model": _coerce_text(payload.get("radio_model", (existing or {}).get("radio_model", "")), ""),
            "enabled": _coerce_bool_int(payload.get("enabled", (existing or {}).get("enabled", 1)), True),
            "needs_operator_name": _needs_operator_radio_name(
                display_name,
                needs_operator_name_value,
            ),
            "runtime_active": _coerce_bool_int(payload.get("runtime_active", (existing or {}).get("runtime_active", 0)), False),
            "runtime_primary": _coerce_bool_int(payload.get("runtime_primary", (existing or {}).get("runtime_primary", 0)), False),
            "display_order": _coerce_int(payload.get("display_order", (existing or {}).get("display_order", 0)), 0),
            "device_class": device_class,
            "deployment_mode": deployment_mode,
            "control_backend": control_backend,
            "use_flrig": _coerce_bool_int(payload.get("use_flrig", default_use_flrig), control_backend == "flrig"),
            "use_fldigi": _coerce_bool_int(payload.get("use_fldigi", default_use_fldigi), False),
            "use_flmsg": _coerce_bool_int(payload.get("use_flmsg", default_use_flmsg), False),
            "use_flamp": _coerce_bool_int(payload.get("use_flamp", default_use_flamp), False),
            "use_js8call": _coerce_bool_int(payload.get("use_js8call", default_use_js8call), control_backend == "js8call"),
            "use_js8spotter": _coerce_bool_int(payload.get("use_js8spotter", default_use_js8spotter), False),
            "use_external_js8spotter": (
                None if payload.get('use_external_js8spotter', (existing or {}).get('use_external_js8spotter')) is None
                else _coerce_bool_int(payload.get('use_external_js8spotter', (existing or {}).get('use_external_js8spotter')),False)
            ),
            "use_commstat": _coerce_bool_int(payload.get("use_commstat", default_use_commstat), False),
            "use_varac": _coerce_bool_int(payload.get("use_varac", default_use_varac), False),
            "rig_host": _coerce_text(payload.get("rig_host", (existing or {}).get("rig_host", "")), ""),
            "rig_port": _coerce_optional_int(payload.get("rig_port", (existing or {}).get("rig_port"))),
            "flrig_host": flrig_host,
            "flrig_port": _coerce_optional_int(payload.get("flrig_port", (existing or {}).get("flrig_port")), 12345),
            "fldigi_host": fldigi_host,
            "fldigi_port": _coerce_optional_int(payload.get("fldigi_port", (existing or {}).get("fldigi_port")), 7362),
            "fldigi_log_path": _coerce_text(payload.get("fldigi_log_path", (existing or {}).get("fldigi_log_path", "")), ""),
            "fldigi_checkin_dir": _coerce_text(payload.get("fldigi_checkin_dir", (existing or {}).get("fldigi_checkin_dir", "")), ""),
            "flmsg_path": _coerce_text(payload.get("flmsg_path", (existing or {}).get("flmsg_path", "")), ""),
            "flmsg_message_path": _coerce_text(
                payload.get("flmsg_message_path", (existing or {}).get("flmsg_message_path", "")),
                "",
            ),
            "flamp_path": _coerce_text(payload.get("flamp_path", (existing or {}).get("flamp_path", "")), ""),
            "flamp_message_path": _coerce_text(
                payload.get("flamp_message_path", (existing or {}).get("flamp_message_path", "")),
                "",
            ),
            "js8_host": js8_host,
            "js8_port": _coerce_optional_int(payload.get("js8_port", (existing or {}).get("js8_port")), 2442),
            "js8_instance_id": js8_instance_id,
            "js8_profile_path": _coerce_text(payload.get("js8_profile_path", (existing or {}).get("js8_profile_path", "")), ""),
            "js8_directed_path": _coerce_text(payload.get("js8_directed_path", (existing or {}).get("js8_directed_path", "")), ""),
            "js8_forms_path": _coerce_text(payload.get("js8_forms_path", (existing or {}).get("js8_forms_path", "")), ""),
            "fast_light_config_id": fast_light_config_id,
            "varac_install_path": _coerce_text(payload.get("varac_install_path", (existing or {}).get("varac_install_path", "")), ""),
            "varac_db_path": _coerce_text(payload.get("varac_db_path", (existing or {}).get("varac_db_path", "")), ""),
            "varac_ini_path": _coerce_text(payload.get("varac_ini_path", (existing or {}).get("varac_ini_path", "")), ""),
            "varac_node_id": varac_node_id,
            "varac_outbox_dir": _coerce_text(
                payload.get("varac_outbox_dir", (existing or {}).get("varac_outbox_dir", "")),
                "",
            ),
            "varac_bbs_dir": _coerce_text(payload.get("varac_bbs_dir", (existing or {}).get("varac_bbs_dir", "")), ""),
            "varac_bbs_archive_dir": _coerce_text(
                payload.get("varac_bbs_archive_dir", (existing or {}).get("varac_bbs_archive_dir", "")),
                "",
            ),
            "varac_bbs_enabled": _coerce_bool_int(
                payload.get("varac_bbs_enabled", (existing or {}).get("varac_bbs_enabled", 0)),
                False,
            ),
            "varac_bbs_limit_access_enabled": _coerce_bool_int(
                payload.get(
                    "varac_bbs_limit_access_enabled",
                    (existing or {}).get("varac_bbs_limit_access_enabled", 0),
                ),
                False,
            ),
            "varac_bbs_allowed_callsigns": _coerce_text(
                payload.get(
                    "varac_bbs_allowed_callsigns",
                    (existing or {}).get("varac_bbs_allowed_callsigns", ""),
                ),
                "",
            ),
            "varac_guard_access_policy_v1": _coerce_text(
                payload.get("varac_guard_access_policy_v1", (existing or {}).get("varac_guard_access_policy_v1", "")), "",
            ),
            "varac_guard_allow_bbs_allowed_callsigns": (
                (_coerce_bool_int(payload["varac_guard_allow_bbs_allowed_callsigns"], True) if payload["varac_guard_allow_bbs_allowed_callsigns"] is not None else None)
                if "varac_guard_allow_bbs_allowed_callsigns" in payload
                else (existing or {}).get("varac_guard_allow_bbs_allowed_callsigns")
            ),
            "varac_guard_allow_operator_trusted": (
                (_coerce_bool_int(payload["varac_guard_allow_operator_trusted"], True) if payload["varac_guard_allow_operator_trusted"] is not None else None)
                if "varac_guard_allow_operator_trusted" in payload
                else (existing or {}).get("varac_guard_allow_operator_trusted")
            ),
            "varac_bbs_allowed_group_sources": _coerce_text(
                payload.get(
                    "varac_bbs_allowed_group_sources",
                    (existing or {}).get("varac_bbs_allowed_group_sources", ""),
                ),
                "",
            ),
            "varac_bbs_announce_enabled": _coerce_bool_int(
                payload.get(
                    "varac_bbs_announce_enabled",
                    (existing or {}).get("varac_bbs_announce_enabled", 0),
                ),
                False,
            ),
            "varac_bbs_auto_archive_enabled": _coerce_bool_int(
                payload.get(
                    "varac_bbs_auto_archive_enabled",
                    (existing or {}).get("varac_bbs_auto_archive_enabled", 0),
                ),
                False,
            ),
            "varac_bbs_auto_archive_days": _coerce_optional_int(
                payload.get(
                    "varac_bbs_auto_archive_days",
                    (existing or {}).get("varac_bbs_auto_archive_days", 14),
                ),
                14,
            ),
            "varac_bbs_vault_enabled": _coerce_bool_int(
                payload.get("varac_bbs_vault_enabled", (existing or {}).get("varac_bbs_vault_enabled", 0)),
                False,
            ),
            "varac_bbs_vault_managed_root": _coerce_text(
                payload.get("varac_bbs_vault_managed_root", (existing or {}).get("varac_bbs_vault_managed_root", "")),
                "",
            ),
            "varac_bbs_vault_default_location_id": _coerce_text(
                payload.get(
                    "varac_bbs_vault_default_location_id",
                    (existing or {}).get("varac_bbs_vault_default_location_id", ""),
                ),
                "",
            ),
            "varac_bbs_vault_global_code_policy": _coerce_text(
                payload.get(
                    "varac_bbs_vault_global_code_policy",
                    (existing or {}).get("varac_bbs_vault_global_code_policy", ""),
                ),
                "",
            ),
            "varac_bbs_vault_trigger_mode": _coerce_text(
                payload.get("varac_bbs_vault_trigger_mode", (existing or {}).get("varac_bbs_vault_trigger_mode", "")),
                "",
            ),
            "varac_bbs_vault_return_mode": _coerce_text(
                payload.get("varac_bbs_vault_return_mode", (existing or {}).get("varac_bbs_vault_return_mode", "")),
                "",
            ),
            "varac_bbs_vault_failed_attempt_limit": _coerce_optional_int(
                payload.get(
                    "varac_bbs_vault_failed_attempt_limit",
                    (existing or {}).get("varac_bbs_vault_failed_attempt_limit", 3),
                ),
                3,
            ),
            "varac_bbs_vault_failed_attempt_window_seconds": _coerce_optional_int(
                payload.get(
                    "varac_bbs_vault_failed_attempt_window_seconds",
                    (existing or {}).get("varac_bbs_vault_failed_attempt_window_seconds", 900),
                ),
                900,
            ),
            "varac_bbs_vault_cooldown_seconds": _coerce_optional_int(
                payload.get(
                    "varac_bbs_vault_cooldown_seconds",
                    (existing or {}).get("varac_bbs_vault_cooldown_seconds", 1800),
                ),
                1800,
            ),
            "varac_bbs_vault_idle_timeout_seconds": _coerce_optional_int(
                payload.get(
                    "varac_bbs_vault_idle_timeout_seconds",
                    (existing or {}).get("varac_bbs_vault_idle_timeout_seconds", 600),
                ),
                600,
            ),
            "varac_bbs_vault_flamp_enabled": _coerce_bool_int(
                payload.get(
                    "varac_bbs_vault_flamp_enabled",
                    (existing or {}).get("varac_bbs_vault_flamp_enabled", 0),
                ),
                False,
            ),
            "varac_bbs_vault_flamp_relay_dir": _coerce_text(
                payload.get(
                    "varac_bbs_vault_flamp_relay_dir",
                    (existing or {}).get("varac_bbs_vault_flamp_relay_dir", ""),
                ),
                "",
            ),
            "varac_bbs_vault_flamp_listing_max_age_days": _coerce_optional_int(
                payload.get(
                    "varac_bbs_vault_flamp_listing_max_age_days",
                    (existing or {}).get("varac_bbs_vault_flamp_listing_max_age_days", 14),
                ),
                14,
            ),
            "varac_bbs_vault_locations_v1": _coerce_json_list_text(
                payload.get(
                    "varac_bbs_vault_locations_v1",
                    (existing or {}).get("varac_bbs_vault_locations_v1", "[]"),
                )
            ),
            "varac_bbs_sweeper_rules_v1": _coerce_json_list_text(
                payload.get(
                    "varac_bbs_sweeper_rules_v1",
                    (existing or {}).get("varac_bbs_sweeper_rules_v1", "[]"),
                )
            ),
            "varac_bbs_vault_runtime_state_v1": _coerce_json_object_text(
                payload.get(
                    "varac_bbs_vault_runtime_state_v1",
                    (existing or {}).get("varac_bbs_vault_runtime_state_v1", "{}"),
                )
            ),
            "varac_bbs_vault_last_summary": _coerce_text(
                payload.get("varac_bbs_vault_last_summary", (existing or {}).get("varac_bbs_vault_last_summary", "")),
                "",
            ),
            "scheduler_enabled": _coerce_bool_int(
                payload.get("scheduler_enabled", (existing or {}).get("scheduler_enabled", 1)),
                True,
            ),
            "schedule_hold_minutes_default": _normalize_hold_duration_minutes(
                payload.get(
                    "schedule_hold_minutes_default",
                    (existing or {}).get("schedule_hold_minutes_default", DEFAULT_HOLD_DURATION_MINUTES),
                ),
            ),
            "freq_enforcement_mode": _coerce_text(
                payload.get(
                    "freq_enforcement_mode",
                    (existing or {}).get("freq_enforcement_mode", DEFAULT_TIMER_ENFORCEMENT_MODE),
                ),
                DEFAULT_TIMER_ENFORCEMENT_MODE,
            ),
            "freq_prompt_interval": _coerce_text(
                payload.get(
                    "freq_prompt_interval",
                    (existing or {}).get("freq_prompt_interval", DEFAULT_TIMER_PROMPT_INTERVAL),
                ),
                DEFAULT_TIMER_PROMPT_INTERVAL,
            ),
            "fldigi_enforcement_mode": _coerce_text(
                payload.get(
                    "fldigi_enforcement_mode",
                    (existing or {}).get("fldigi_enforcement_mode", DEFAULT_TIMER_ENFORCEMENT_MODE),
                ),
                DEFAULT_TIMER_ENFORCEMENT_MODE,
            ),
            "fldigi_prompt_interval": _coerce_text(
                payload.get(
                    "fldigi_prompt_interval",
                    (existing or {}).get("fldigi_prompt_interval", DEFAULT_TIMER_PROMPT_INTERVAL),
                ),
                DEFAULT_TIMER_PROMPT_INTERVAL,
            ),
            "js8_enforcement_mode": _coerce_text(
                payload.get(
                    "js8_enforcement_mode",
                    (existing or {}).get("js8_enforcement_mode", DEFAULT_TIMER_ENFORCEMENT_MODE),
                ),
                DEFAULT_TIMER_ENFORCEMENT_MODE,
            ),
            "js8_prompt_interval": _coerce_text(
                payload.get(
                    "js8_prompt_interval",
                    (existing or {}).get("js8_prompt_interval", DEFAULT_TIMER_PROMPT_INTERVAL),
                ),
                DEFAULT_TIMER_PROMPT_INTERVAL,
            ),
            "launch_enabled": _coerce_bool_int(payload.get("launch_enabled", (existing or {}).get("launch_enabled", 0)), False),
            "launch_path": _coerce_text(payload.get("launch_path", (existing or {}).get("launch_path", "")), ""),
            "launch_cmd": _coerce_text(payload.get("launch_cmd", (existing or {}).get("launch_cmd", "")), ""),
            "ptt_group": normalize_ptt_group(payload.get("ptt_group", (existing or {}).get("ptt_group", ""))),
            "antenna_group": normalize_resource_group(payload.get("antenna_group", (existing or {}).get("antenna_group", ""))),
            "frontend_group": normalize_resource_group(payload.get("frontend_group", (existing or {}).get("frontend_group", ""))),
            "amplifier_group": normalize_resource_group(payload.get("amplifier_group", (existing or {}).get("amplifier_group", ""))),
            "antenna_supported_bands_json": _coerce_json_array_text(
                payload.get(
                    "antenna_supported_bands",
                    payload.get(
                        "antenna_supported_bands_json",
                        (existing or {}).get("antenna_supported_bands_json", "[]"),
                    ),
                )
            ),
            "antenna_band_guard_mode": normalize_rf_guard_mode(
                payload.get("antenna_band_guard_mode", (existing or {}).get("antenna_band_guard_mode", "warn"))
            ),
            "band_overlap_guard_group": normalize_resource_group(
                payload.get("band_overlap_guard_group", (existing or {}).get("band_overlap_guard_group", ""))
            ),
            "band_overlap_guard_mode": normalize_rf_guard_mode(
                payload.get("band_overlap_guard_mode", (existing or {}).get("band_overlap_guard_mode", "warn"))
            ),
            "advanced_frequency_guard_group": normalize_resource_group(
                payload.get(
                    "advanced_frequency_guard_group",
                    (existing or {}).get("advanced_frequency_guard_group", ""),
                )
            ),
            "advanced_frequency_guard_mode": normalize_rf_guard_mode(
                payload.get(
                    "advanced_frequency_guard_mode",
                    (existing or {}).get("advanced_frequency_guard_mode", "warn"),
                )
            ),
            "advanced_frequency_guard_window_hz": _coerce_nonnegative_int(
                payload.get(
                    "advanced_frequency_guard_window_hz",
                    (existing or {}).get("advanced_frequency_guard_window_hz", 0),
                )
            ),
            "sdr_host": _coerce_text(payload.get("sdr_host", (existing or {}).get("sdr_host", "")), ""),
            "sdr_port": _coerce_optional_int(payload.get("sdr_port", (existing or {}).get("sdr_port"))),
            "sdr_application": _coerce_text(
                payload.get("sdr_application", (existing or {}).get("sdr_application", "")),
                "",
            ),
            "sdr_adapter": sdr_adapter,
            "sdr_target": _coerce_text(
                payload.get("sdr_target", (existing or {}).get("sdr_target", "")),
                "",
            ),
            "sdr_control_enabled": _coerce_bool_int(
                payload.get("sdr_control_enabled", (existing or {}).get("sdr_control_enabled", 0)),
                False,
            ),
            "sdr_verification_state": sdr_verification_state,
            "sdr_verification_json": _coerce_json_object_text(
                payload.get(
                    "sdr_verification",
                    payload.get(
                        "sdr_verification_json",
                        (existing or {}).get("sdr_verification_json", "{}"),
                    ),
                )
            ),
            "notes": _coerce_text(payload.get("notes", (existing or {}).get("notes", "")), ""),
            "created_utc": (existing or {}).get("created_utc", now_iso),
            "updated_utc": now_iso,
        }
        if bool(record["sdr_control_enabled"]):
            if device_class != "observer":
                raise ValueError("Receiver control can only be enabled for observer / SDR device profiles.")
            if record["sdr_adapter"] == "manual":
                raise ValueError("Receiver control requires a receive-only adapter.")
            if not record["sdr_host"] or record["sdr_port"] is None or not record["sdr_target"]:
                raise ValueError("Receiver control requires host, port, and a selected receiver target.")
            if record["sdr_verification_state"] != "verified":
                raise ValueError("Receiver control cannot be enabled until tune/readback verification passes.")
            if not receiver_control_verification_matches(record):
                raise ValueError(
                    "Receiver control verification does not match this exact adapter, host, port, and target. "
                    "Run Test control again."
                )
        if device_class == "observer":
            if record["runtime_primary"]:
                raise ValueError("Observer / SDR device profiles cannot become the compatibility runtime device.")
            if requested_id is None:
                if record["runtime_active"]:
                    raise ValueError("Observer / SDR radios require receive-only operating models before activation.")
            else:
                assignment = _effective_assignment_for_device(conn, int(requested_id))
                if assignment:
                    operating = _record_by_id(conn, "operating_profiles", int(assignment.get("operating_profile_id", 0) or 0))
                    if operating:
                        _validate_assignment_plan_compatibility(record, operating)
                elif record["runtime_active"]:
                    raise ValueError("Observer / SDR radios require receive-only operating models before activation.")
        columns = list(record.keys())
        if existing:
            assignments = ", ".join(f"{name}=?" for name in columns)
            conn.execute(
                f"UPDATE device_profiles SET {assignments} WHERE id=?",
                [record[name] for name in columns] + [int(requested_id)],
            )
            conn.commit()
            saved = _record_by_id(conn, "device_profiles", int(requested_id)) or {}
        else:
            conn.execute(
                f"INSERT INTO device_profiles ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))})",
                [record[name] for name in columns],
            )
            conn.commit()
            saved = _record_by_system_key(conn, "device_profiles", system_key) or {}

        if saved:
            _sync_station_shared_commstat_binding_conn(
                conn,
                radio_profile_id=int(saved.get("id", 0) or 0),
            )
        _sync_derived_coordination_policies_conn(conn)
        if saved:
            _refresh_assigned_plan_validation_for_device_conn(conn, int(saved.get("id", 0) or 0), emit_events=False)
        if device_class == "observer":
            _ensure_receive_only_operating_profile_conn(conn, commit=False)
        conn.commit()
        if _coerce_bool_int(payload.get("runtime_active"), False):
            if record["runtime_primary"]:
                return MultiRadioStore._set_runtime_primary_device_conn(
                    conn,
                    int(saved["id"]),
                    deactivate_others=False,
                )
            conn.execute("UPDATE device_profiles SET runtime_active=1 WHERE id=?", (int(saved["id"]),))
            conn.commit()
            _normalize_runtime_primary_device(conn)
            project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(saved["id"]))
            refreshed = _record_by_id(conn, "device_profiles", int(saved["id"])) or saved
            return _resolve_device_profile_links_conn(conn, refreshed)
        _normalize_runtime_primary_device(conn)
        return _resolve_device_profile_links_conn(conn, saved)

    @staticmethod
    def _set_runtime_primary_device_conn(
        conn: sqlite3.Connection,
        device_profile_id: int,
        *,
        deactivate_others: bool = False,
    ) -> Dict[str, Any]:
        device = _record_by_id(conn, "device_profiles", int(device_profile_id))
        if not device:
            raise KeyError(f"Unknown device profile id: {device_profile_id}")
        if _coerce_text(device.get("device_class", "tx_rx"), "tx_rx").lower() == "observer":
            raise ValueError("Observer / SDR device profiles cannot become the compatibility runtime device.")
        backend = _coerce_text(device.get("control_backend", "manual"), "manual").lower()
        if backend not in SUPPORTED_RUNTIME_CONTROL_BACKENDS:
            raise ValueError(f"Cannot activate backend until runtime support exists: {backend}")
        assignment = _effective_assignment_for_device(conn, int(device_profile_id))
        if not assignment:
            operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
            if not operating:
                operating = _save_operating_profile_conn(conn, _seed_operating_defaults(_load_kv_settings(conn)))
            _ensure_default_assignment(conn, int(device_profile_id), int(operating["id"]))
        if deactivate_others:
            conn.execute(
                "UPDATE device_profiles SET runtime_active=CASE WHEN id=? THEN 1 ELSE 0 END, runtime_primary=CASE WHEN id=? THEN 1 ELSE 0 END",
                (int(device_profile_id), int(device_profile_id)),
            )
        else:
            conn.execute(
                "UPDATE device_profiles SET runtime_primary=CASE WHEN id=? THEN 1 ELSE 0 END, runtime_active=CASE WHEN id=? THEN 1 ELSE runtime_active END",
                (int(device_profile_id), int(device_profile_id)),
            )
        conn.commit()
        _normalize_runtime_primary_device(conn)
        project_runtime_active_device_to_legacy_settings(conn, int(device_profile_id))
        return _resolve_device_profile_links_conn(conn, _record_by_id(conn, "device_profiles", int(device_profile_id)) or device)

    @staticmethod
    def _set_runtime_active_device_conn(
        conn: sqlite3.Connection,
        device_profile_id: int,
        *,
        deactivate_others: bool = True,
    ) -> Dict[str, Any]:
        if deactivate_others:
            return MultiRadioStore._set_runtime_primary_device_conn(
                conn,
                int(device_profile_id),
                deactivate_others=True,
            )
        device = _record_by_id(conn, "device_profiles", int(device_profile_id))
        if not device:
            raise KeyError(f"Unknown device profile id: {device_profile_id}")
        if _coerce_text(device.get("device_class", "tx_rx"), "tx_rx").lower() == "observer":
            raise ValueError("Observer / SDR device profiles cannot become active transmit/receive radios.")
        backend = _coerce_text(device.get("control_backend", "manual"), "manual").lower()
        if backend not in SUPPORTED_RUNTIME_CONTROL_BACKENDS:
            raise ValueError(f"Cannot activate backend until runtime support exists: {backend}")
        assignment = _effective_assignment_for_device(conn, int(device_profile_id))
        if not assignment:
            operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
            if not operating:
                operating = _save_operating_profile_conn(conn, _seed_operating_defaults(_load_kv_settings(conn)))
            _ensure_default_assignment(conn, int(device_profile_id), int(operating["id"]))
        conn.execute("UPDATE device_profiles SET runtime_active=1 WHERE id=?", (int(device_profile_id),))
        conn.commit()
        _normalize_runtime_primary_device(conn)
        project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(device_profile_id))
        return _resolve_device_profile_links_conn(conn, _record_by_id(conn, "device_profiles", int(device_profile_id)) or device)

    def get_runtime_active_device_profile(self) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            row = conn.execute(
                """
                SELECT * FROM device_profiles
                 WHERE runtime_active=1 AND runtime_primary=1
              ORDER BY display_order ASC, id ASC LIMIT 1
                """
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM device_profiles
                     WHERE runtime_active=1
                  ORDER BY display_order ASC, id ASC LIMIT 1
                    """
                ).fetchone()
            if not row:
                return None
            return _resolve_device_profile_links_conn(conn, dict(row))

    def get_runtime_primary_device_profile(self) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            return _runtime_primary_device_profile(conn)

    def list_runtime_active_device_profiles(self) -> List[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            return _runtime_active_device_profiles(conn)

    def list_device_profiles(self) -> List[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            rows = conn.execute("SELECT * FROM device_profiles ORDER BY display_order ASC, id ASC").fetchall()
            return [_resolve_device_profile_links_conn(conn, dict(row)) for row in rows]

    def get_device_profile(self, device_profile_id: int) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            row = _record_by_id(conn, "device_profiles", int(device_profile_id))
            if not row:
                return None
            return _resolve_device_profile_links_conn(conn, row)

    def list_operating_profiles(self) -> List[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            rows = conn.execute("SELECT * FROM operating_profiles ORDER BY id ASC").fetchall()
            return [dict(row) for row in rows]

    def ensure_builtin_operating_profiles(self) -> List[Dict[str, Any]]:
        """Ensure Add Radio can select persisted default and receive-only models.

        A fresh multi-rig blank slate intentionally has no radio, but its guided
        setup still needs real Operating Model rows before the first radio is
        reviewed. This helper creates only the protected built-in models; it
        does not create a radio, assignment, or runtime-primary projection.
        """

        with self._connect() as conn:
            operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
            if not operating:
                operating = _save_operating_profile_conn(
                    conn,
                    _seed_operating_defaults(_load_kv_settings(conn)),
                )
            elif int(operating.get("enabled", 1) or 0) != 1:
                # The default model is a protected invariant in current builds,
                # but early development databases could retain a disabled row.
                # Repair that stale state so Guided Add Radio never presents an
                # empty model picker for an otherwise valid transceiver.
                conn.execute(
                    "UPDATE operating_profiles SET enabled=1, updated_utc=? WHERE id=?",
                    (_utc_now_iso(), int(operating["id"])),
                )
                operating = _record_by_id(conn, "operating_profiles", int(operating["id"])) or operating
            receiver = _ensure_receive_only_operating_profile_conn(conn, commit=False)
            conn.commit()
            return [dict(operating), dict(receiver)]

    def get_operating_profile(self, operating_profile_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            return _record_by_id(conn, "operating_profiles", int(operating_profile_id))

    def ensure_receive_only_operating_profile(self) -> Dict[str, Any]:
        """Return the assignable built-in observer/SDR operating model."""

        with self._connect() as conn:
            return _ensure_receive_only_operating_profile_conn(conn)

    def save_operating_profile(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(values)
        requested_id = _coerce_optional_int(payload.get("id"))
        with self._connect() as conn:
            existing = _record_by_id(conn, "operating_profiles", int(requested_id)) if requested_id is not None else None
            active_swap = _active_profile_swap_policy_conn(conn)
            enabled = _coerce_bool_int(payload.get("enabled", (existing or {}).get("enabled", 1)), True)
            if existing and str(existing.get("system_key", "") or "").strip() == DEFAULT_OPERATING_SYSTEM_KEY and enabled != 1:
                raise ValueError("Cannot disable the default Operating Model.")
            receive_only = _coerce_bool_int(payload.get("receive_only", (existing or {}).get("receive_only", 0)), False)
            if (
                existing
                and str(existing.get("system_key", "") or "").strip()
                == DEFAULT_RECEIVE_ONLY_OPERATING_SYSTEM_KEY
            ):
                if enabled != 1:
                    raise ValueError("Cannot disable the built-in receive-only SDR Operating Model.")
                if receive_only != 1:
                    raise ValueError("The built-in SDR Operating Model must remain receive-only.")
                if _coerce_bool_int(
                    payload.get("scheduler_enabled", existing.get("scheduler_enabled", 0)),
                    False,
                ):
                    raise ValueError("The built-in SDR Operating Model cannot own the scheduler.")
                if _coerce_bool_int(
                    payload.get("use_net_control_tabs", existing.get("use_net_control_tabs", 0)),
                    False,
                ):
                    raise ValueError("The built-in SDR Operating Model cannot enable transmit/net controls.")
                if _coerce_bool_int(
                    payload.get("allow_profile_swap", existing.get("allow_profile_swap", 0)),
                    False,
                ):
                    raise ValueError("The built-in SDR Operating Model cannot participate in profile swaps.")
            if (
                existing
                and receive_only != 1
                and _operating_profile_has_observer_assignments(conn, int(requested_id))
            ):
                raise ValueError(
                    "This Operating Model is assigned to observer / SDR radios. Remove those assignments or keep the model receive-only."
                )
            if existing and enabled != 1:
                placeholders = ", ".join("?" for _ in EFFECTIVE_ASSIGNMENT_STATES)
                assigned = conn.execute(
                    f"""
                    SELECT id
                      FROM operating_profile_assignments
                     WHERE operating_profile_id=?
                       AND assignment_state IN ({placeholders})
                     LIMIT 1
                    """,
                    (int(requested_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
                ).fetchone()
                if assigned is not None:
                    raise ValueError("Cannot disable an Operating Model while it is assigned to a radio.")
                if active_swap is not None:
                    restore_target = dict((active_swap.get("action") or {}).get("restore_target_assignment") or {})
                    restore_target_id = restore_target.get("operating_profile_id")
                    if restore_target_id not in (None, "") and int(restore_target_id) == int(requested_id):
                        raise ValueError(
                            "Cannot disable this Operating Model while it is captured as the restore target for an active Temporary Model Swap."
                        )
            return _save_operating_profile_conn(conn, payload)

    def delete_operating_profile(self, operating_profile_id: int) -> None:
        with self._connect() as conn:
            operating = _record_by_id(conn, "operating_profiles", int(operating_profile_id))
            if not operating:
                raise KeyError(f"Unknown Operating Model id: {operating_profile_id}")
            system_key = str(operating.get("system_key", "") or "").strip()
            if system_key == DEFAULT_OPERATING_SYSTEM_KEY:
                raise ValueError("Cannot delete the default Operating Model.")
            if system_key == DEFAULT_RECEIVE_ONLY_OPERATING_SYSTEM_KEY:
                raise ValueError("Cannot delete the built-in receive-only SDR Operating Model.")
            active_swap = _active_profile_swap_policy_conn(conn)
            if active_swap is not None:
                restore_target = dict((active_swap.get("action") or {}).get("restore_target_assignment") or {})
                restore_target_id = restore_target.get("operating_profile_id")
                if restore_target_id not in (None, "") and int(restore_target_id) == int(operating_profile_id):
                    raise ValueError(
                        "Cannot delete this Operating Model while it is captured as the restore target for an active Temporary Model Swap."
                    )
            placeholders = ", ".join("?" for _ in EFFECTIVE_ASSIGNMENT_STATES)
            assigned = conn.execute(
                f"""
                SELECT id
                  FROM operating_profile_assignments
                 WHERE operating_profile_id=?
                   AND assignment_state IN ({placeholders})
                 LIMIT 1
                """,
                (int(operating_profile_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
            ).fetchone()
            if assigned is not None:
                raise ValueError("Cannot delete an Operating Model while it is assigned to a radio.")
            conn.execute("DELETE FROM operating_profile_assignments WHERE operating_profile_id=?", (int(operating_profile_id),))
            conn.execute("DELETE FROM operating_profiles WHERE id=?", (int(operating_profile_id),))
            conn.commit()

    def list_frequency_plans(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM frequency_plans ORDER BY id ASC").fetchall()
            return [dict(row) for row in rows]

    def get_frequency_plan(self, frequency_plan_id: int) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            return _record_by_id(conn, "frequency_plans", int(frequency_plan_id))

    def save_frequency_plan(self, values: Mapping[str, Any], *,
                            expected: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            if expected is not None:
                # Source materialization must not overwrite a plan edited while
                # its expensive projection was computed. Hold only the short
                # compare/write transaction, never the projection itself.
                conn.execute("BEGIN IMMEDIATE")
                current = _record_by_id(conn, "frequency_plans", int(values.get("id") or 0))
                if current != dict(expected):
                    raise ValueError("Frequency Plan changed during source projection; retry required")
            payload = dict(values)
            record_id = _coerce_optional_int(payload.get("id"))
            if record_id is not None:
                existing = _record_by_id(conn, "frequency_plans", int(record_id))
                if existing and not _is_receive_only_frequency_plan(
                    payload.get("receive_only", existing.get("receive_only", 0))
                ):
                    if _frequency_plan_has_observer_assignments(conn, int(record_id)):
                        raise ValueError(
                            "This Frequency Plan is assigned to observer / SDR radios. Remove those assignments or keep the plan receive-only."
                        )
            return _save_frequency_plan_conn(conn, payload)

    def delete_frequency_plan(self, frequency_plan_id: int) -> None:
        with self._connect() as conn:
            plan = _record_by_id(conn, "frequency_plans", int(frequency_plan_id))
            if not plan:
                raise KeyError(f"Unknown Frequency Plan id: {frequency_plan_id}")
            placeholders = ", ".join("?" for _ in EFFECTIVE_ASSIGNMENT_STATES)
            assigned = conn.execute(
                f"""
                SELECT id
                  FROM assigned_plans
                 WHERE frequency_plan_id=?
                   AND assignment_state IN ({placeholders})
                 LIMIT 1
                """,
                (int(frequency_plan_id), *tuple(EFFECTIVE_ASSIGNMENT_STATES)),
            ).fetchone()
            if assigned is not None:
                raise ValueError("Cannot delete a Frequency Plan while it is assigned to a radio.")
            conn.execute("DELETE FROM assigned_plans WHERE frequency_plan_id=?", (int(frequency_plan_id),))
            conn.execute("DELETE FROM frequency_plans WHERE id=?", (int(frequency_plan_id),))
            conn.commit()

    def list_assigned_plans(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM assigned_plans ORDER BY device_profile_id ASC, id ASC").fetchall()
            return [dict(row) for row in rows]

    def list_effective_assigned_plans(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM device_profiles ORDER BY display_order ASC, id ASC").fetchall()
            assignments: List[Dict[str, Any]] = []
            for row in rows:
                assignment = _effective_assigned_plan_for_device(conn, int(row[0]))
                if assignment:
                    assignments.append(dict(assignment))
            return assignments

    def get_effective_assigned_plan_for_device(self, device_profile_id: int) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            return _effective_assigned_plan_for_device(conn, int(device_profile_id))

    def set_assigned_plan(
        self,
        device_profile_id: int,
        frequency_plan_id: int,
        *,
        assignment_state: str = "active",
        assignment_category: str = "normal",
        scheduler_mode: str = "full",
        reason: str = "",
        starts_utc: str = "",
        ends_utc: str = "",
        created_by: str = "settings_ui",
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            return _set_assigned_plan_conn(
                conn,
                int(device_profile_id),
                int(frequency_plan_id),
                assignment_state=assignment_state,
                assignment_category=assignment_category,
                scheduler_mode=scheduler_mode,
                reason=reason,
                starts_utc=starts_utc,
                ends_utc=ends_utc,
                created_by=created_by,
            )

    def swap_assigned_frequency_plans(
        self,
        first_device_profile_id: int,
        second_device_profile_id: int,
        *,
        reason: str = "Swapped Frequency Plans in Settings.",
    ) -> List[Dict[str, Any]]:
        first_id = int(first_device_profile_id or 0)
        second_id = int(second_device_profile_id or 0)
        if first_id <= 0 or second_id <= 0 or first_id == second_id:
            raise ValueError("Select two different radios with assigned Frequency Plans.")
        with self._connect() as conn:
            first_assignment = _effective_assigned_plan_for_device(conn, first_id)
            second_assignment = _effective_assigned_plan_for_device(conn, second_id)
            if not first_assignment or not second_assignment:
                raise ValueError("Both selected radios must already have assigned Frequency Plans before swapping.")
            first_plan_id = int(first_assignment.get("frequency_plan_id") or 0)
            second_plan_id = int(second_assignment.get("frequency_plan_id") or 0)
            if first_plan_id <= 0 or second_plan_id <= 0:
                raise ValueError("Both selected radios must already have assigned Frequency Plans before swapping.")
            if first_plan_id == second_plan_id:
                raise ValueError("The selected radios already use the same Frequency Plan.")
            overrides = {
                first_id: second_plan_id,
                second_id: first_plan_id,
            }
            first_row = _set_assigned_plan_conn(
                conn,
                first_id,
                second_plan_id,
                assignment_state="active",
                reason=reason,
                created_by="settings_ui_swap",
                assignment_plan_overrides=overrides,
                commit=False,
            )
            second_row = _set_assigned_plan_conn(
                conn,
                second_id,
                first_plan_id,
                assignment_state="active",
                reason=reason,
                created_by="settings_ui_swap",
                assignment_plan_overrides=overrides,
                commit=False,
            )
            conn.commit()
            return [first_row, second_row]

    def validate_frequency_plan_for_device(
        self,
        device_profile_id: int,
        frequency_plan: Mapping[str, Any],
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            device = _record_by_id(conn, "device_profiles", int(device_profile_id))
            if not device:
                raise KeyError(f"Unknown device profile id: {device_profile_id}")
            _validate_schedule_assignment_compatibility(device, frequency_plan)
            return _schedule_assignment_validation_status_conn(conn, device, frequency_plan, emit_events=False)

    def validate_frequency_plan_for_device_payload(
        self,
        device_profile: Mapping[str, Any],
        frequency_plan: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Validate a not-yet-saved radio profile draft against a Frequency Plan."""
        device = dict(device_profile or {})
        if "antenna_supported_bands_json" not in device:
            device["antenna_supported_bands_json"] = _coerce_json_array_text(
                device.get("antenna_supported_bands", [])
            )
        device["antenna_band_guard_mode"] = normalize_rf_guard_mode(
            device.get("antenna_band_guard_mode", "warn")
        )
        device["band_overlap_guard_mode"] = normalize_rf_guard_mode(
            device.get("band_overlap_guard_mode", "warn")
        )
        device["advanced_frequency_guard_mode"] = normalize_rf_guard_mode(
            device.get("advanced_frequency_guard_mode", "warn")
        )
        with self._connect() as conn:
            _validate_schedule_assignment_compatibility(device, frequency_plan)
            return _schedule_assignment_validation_status_conn(conn, device, frequency_plan, emit_events=False)

    def list_assignments(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM operating_profile_assignments ORDER BY device_profile_id ASC, id ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_effective_assignments(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM device_profiles ORDER BY display_order ASC, id ASC").fetchall()
            assignments: List[Dict[str, Any]] = []
            for row in rows:
                assignment = _effective_assignment_for_device(conn, int(row[0]))
                if assignment:
                    assignments.append(dict(assignment))
            return assignments

    def get_effective_assignment_for_device(self, device_profile_id: int) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            assignment = _effective_assignment_for_device(conn, int(device_profile_id))
            return dict(assignment) if isinstance(assignment, dict) else None

    def set_device_operating_profile(
        self,
        device_profile_id: int,
        operating_profile_id: int,
        *,
        assignment_state: str = "active",
        reason: str = "",
        created_by: str = "settings_ui",
        starts_utc: Optional[str] = None,
        ends_utc: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            return _set_device_operating_profile_conn(
                conn,
                int(device_profile_id),
                int(operating_profile_id),
                assignment_state=assignment_state,
                reason=reason,
                created_by=created_by,
                starts_utc=starts_utc,
                ends_utc=ends_utc,
            )

    def restore_default_operating_profile(
        self,
        device_profile_id: int,
        *,
        reason: str = "Restored default Operating Model.",
        created_by: str = "settings_ui",
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            return _restore_default_operating_profile_conn(
                conn,
                int(device_profile_id),
                reason=reason,
                created_by=created_by,
            )

    def get_active_profile_swap(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            policy = _active_profile_swap_policy_conn(conn)
            return _enrich_profile_swap_policy(conn, policy)

    def start_temporary_profile_swap(
        self,
        target_device_profile_id: int,
        *,
        mode: str = "use_target_profile",
        reason: str = "",
        created_by: str = "settings_ui",
        ends_utc: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            if _active_profile_swap_policy_conn(conn) is not None:
                raise ValueError("A Temporary Plan Swap is already active. Restore it before starting another swap.")

            source_id = int(_normalize_runtime_primary_device(conn) or 0)
            if source_id <= 0:
                raise ValueError("A primary radio is required before starting a Temporary Plan Swap.")
            if int(target_device_profile_id) == source_id:
                raise ValueError("Select a different active radio as the Temporary Plan Swap target.")

            source_row = _record_by_id(conn, "device_profiles", source_id)
            target_row = _record_by_id(conn, "device_profiles", int(target_device_profile_id))
            if not source_row:
                raise ValueError("The current primary radio could not be resolved.")
            if not target_row:
                raise KeyError(f"Unknown target radio id: {target_device_profile_id}")
            if int(target_row.get("enabled", 1) or 0) != 1:
                raise ValueError("The selected Temporary Plan Swap target is disabled.")
            if int(target_row.get("runtime_active", 0) or 0) != 1:
                raise ValueError("The selected Temporary Plan Swap target must already be active.")
            if _coerce_text(target_row.get("device_class", "tx_rx"), "tx_rx").lower() == "observer":
                raise ValueError("Observer / SDR radios cannot be used as Temporary Plan Swap targets.")

            source_assignment = _ensure_effective_assignment_for_device(conn, source_id)
            target_assignment = _ensure_effective_assignment_for_device(conn, int(target_device_profile_id))
            if not source_assignment:
                raise ValueError("The current primary radio does not have an assigned Operating Model.")

            mode_value = _normalize_profile_swap_mode(mode, "use_target_profile")
            reason_value = _coerce_text(reason, "")
            if not reason_value:
                reason_value = f"Temporary swap from {str(source_row.get('name', '') or 'primary device').strip()}."
            created_by_value = _coerce_text(created_by, "settings_ui") or "settings_ui"
            ends_value = _coerce_text(ends_utc, "")

            action: Dict[str, Any] = {
                "restore_primary_device_id": source_id,
                "restore_target_assignment": _assignment_snapshot_from_row(target_assignment),
                "target_assignment_changed": False,
                "source_assignment": _assignment_snapshot_from_row(source_assignment),
            }
            if mode_value == "carry_primary_profile":
                source_operating_profile = _record_by_id(
                    conn,
                    "operating_profiles",
                    int(source_assignment.get("operating_profile_id", 0) or 0),
                )
                if not source_operating_profile:
                    raise ValueError("The current primary radio does not have a valid Operating Model to carry.")
                if int(source_operating_profile.get("allow_profile_swap", 0) or 0) != 1:
                    raise ValueError("The current primary Operating Model does not allow Temporary Model Swap coordination.")
                applied_target = _set_device_operating_profile_conn(
                    conn,
                    int(target_device_profile_id),
                    int(source_operating_profile.get("id", 0) or 0),
                    assignment_state="temporary_override",
                    reason=reason_value,
                    created_by=created_by_value,
                    ends_utc=ends_value or None,
                )
                action["target_assignment_changed"] = True
                action["applied_operating_profile_id"] = int(applied_target.get("operating_profile_id", 0) or 0)
                action["applied_assignment_state"] = str(applied_target.get("assignment_state", "") or "").strip().lower()

            self._set_runtime_primary_device_conn(conn, int(target_device_profile_id), deactivate_others=False)

            now_iso = _utc_now_iso()
            trigger = {
                "mode": mode_value,
                "reason": reason_value,
                "ends_utc": ends_value,
                "created_by": created_by_value,
            }
            conn.execute(
                """
                INSERT INTO station_coordination_policies (
                    name, enabled, policy_type, source_device_id, target_device_id,
                    priority, trigger_json, action_json, safety_mode, created_utc, updated_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"Temporary Swap: {str(source_row.get('name', '') or f'Device {source_id}')} -> {str(target_row.get('name', '') or f'Device {int(target_device_profile_id)}')}",
                    1,
                    PROFILE_SWAP_POLICY_TYPE,
                    source_id,
                    int(target_device_profile_id),
                    PROFILE_SWAP_POLICY_PRIORITY,
                    _coerce_json_object_text(trigger),
                    _coerce_json_object_text(action),
                    "prompt",
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()
            return _enrich_profile_swap_policy(conn, _active_profile_swap_policy_conn(conn)) or {}

    def restore_temporary_profile_swap(
        self,
        *,
        reason: str = "Restored Temporary Plan Swap.",
        created_by: str = "settings_ui",
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            policy = _active_profile_swap_policy_conn(conn)
            if policy is None:
                raise ValueError("No Temporary Plan Swap is currently active.")

            source_id = int(policy.get("source_device_id", 0) or 0)
            target_id = int(policy.get("target_device_id", 0) or 0)
            source_row = _record_by_id(conn, "device_profiles", source_id)
            if not source_row:
                raise ValueError("Cannot restore the Temporary Plan Swap because the original primary radio no longer exists.")
            if int(source_row.get("enabled", 1) or 0) != 1:
                raise ValueError("Cannot restore the Temporary Plan Swap while the original primary radio is disabled.")

            action = dict(policy.get("action") or {})
            created_by_value = _coerce_text(created_by, "settings_ui") or "settings_ui"
            if bool(action.get("target_assignment_changed")):
                _restore_assignment_snapshot_conn(
                    conn,
                    target_id,
                    action.get("restore_target_assignment"),
                    fallback_reason=reason,
                    created_by=created_by_value,
                    allow_active_swap_edit=True,
                )

            self._set_runtime_primary_device_conn(conn, source_id, deactivate_others=False)

            updated_action = dict(action)
            updated_action["restored_utc"] = _utc_now_iso()
            updated_action["restore_reason"] = _coerce_text(reason, "")
            updated_utc = _utc_now_iso()
            conn.execute(
                """
                UPDATE station_coordination_policies
                   SET enabled=0, action_json=?, updated_utc=?
                 WHERE id=?
                """,
                (
                    _coerce_json_object_text(updated_action),
                    updated_utc,
                    int(policy.get("id", 0) or 0),
                ),
            )
            conn.commit()
            restored = dict(policy)
            restored["enabled"] = 0
            restored["action"] = updated_action
            restored["updated_utc"] = updated_utc
            return _enrich_profile_swap_policy(conn, restored) or restored

    def list_js8_instances(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM js8_instances ORDER BY id ASC").fetchall()
            return [dict(row) for row in rows]

    def list_software_instance_manifests(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return _list_software_instance_manifests_conn(conn)

    def get_software_instance_manifest(self, instance_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM software_instance_manifests WHERE instance_key=?",
                (str(instance_key or "").strip(),),
            ).fetchone()
            return _software_instance_manifest_row(dict(row)) if row is not None else None

    def save_software_instance_manifest(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            try:
                saved = _save_software_instance_manifest_conn(conn, values)
                conn.commit()
                return saved
            except Exception:
                conn.rollback()
                raise

    def delete_software_instance_manifest(self, instance_key: str) -> None:
        """Delete lifecycle metadata only; application/radio records remain intact."""

        with self._connect() as conn:
            conn.execute(
                "DELETE FROM software_instance_manifests WHERE instance_key=?",
                (str(instance_key or "").strip(),),
            )
            conn.commit()

    def list_radio_software_identity_records(
        self,
        radio_profile_id: Optional[int] = None,
    ) -> tuple[Any, ...]:
        """Load the exact GRS-13 identity records without legacy reconstruction."""

        from freqinout.core.software_identity_bundle import identity_record_from_mapping

        parameters: tuple[Any, ...] = ()
        where = ""
        if radio_profile_id is not None:
            where = " WHERE radio_profile_id=?"
            parameters = (int(radio_profile_id),)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM radio_software_identity_records"
                + where
                + " ORDER BY radio_profile_id, display_order, family_key",
                parameters,
            ).fetchall()
        records: List[Any] = []
        for raw in rows:
            row = dict(raw)
            try:
                payload = json.loads(str(row.get("record_json", "") or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("Stored software identity record is not valid JSON.") from exc
            if not isinstance(payload, Mapping):
                raise ValueError("Stored software identity record must be a JSON object.")
            record = identity_record_from_mapping(payload)
            if (
                str(row.get("family_key", "") or "") != record.family_key
                or str(row.get("identity_key", "") or "") != record.identity_key
                or str(row.get("bundle_id", "") or "") != record.bundle_id
                or str(row.get("fingerprint", "") or "") != record.fingerprint
            ):
                raise ValueError("Stored software identity projection does not match its canonical record.")
            records.append(record)
        return tuple(records)

    def radio_software_identity_generation(self, radio_profile_id: int) -> int:
        """Return the committed canonical identity generation for one radio."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT generation FROM radio_software_identity_sets WHERE radio_profile_id=?",
                (int(radio_profile_id),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def validate_radio_software_identity_projections(
        self,
        radio_profile_id: int,
    ) -> Dict[str, tuple[str, ...]]:
        """Compare canonical identities with their persisted app/launch projections.

        This is a bounded settings-database read.  It does not inspect the
        filesystem, start a process, or contact an endpoint.  A mismatch is
        evidence for ``Needs attention``; it never rewrites either side.
        """

        from freqinout.core.software_identity_bundle import identity_record_from_mapping

        radio_id = int(radio_profile_id)

        def _json(value: object, fallback: object) -> object:
            try:
                parsed = json.loads(str(value or ""))
            except (TypeError, ValueError):
                return fallback
            return parsed

        def _strings(value: object) -> set[str]:
            found: set[str] = set()
            if isinstance(value, Mapping):
                for item in value.values():
                    found.update(_strings(item))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    found.update(_strings(item))
            elif value not in (None, ""):
                found.add(str(value))
            return found

        with self._connect_readonly() as conn:
            profile_row = _record_by_id(conn, "device_profiles", radio_id)
            if profile_row is None:
                return {"radio": ("radio profile is missing",)}
            profile = _resolve_device_profile_links_conn(conn, profile_row)
            identity_rows = conn.execute(
                "SELECT family_key, record_json FROM radio_software_identity_records "
                "WHERE radio_profile_id=? ORDER BY display_order, family_key",
                (radio_id,),
            ).fetchall()
            identity_set_row = conn.execute(
                "SELECT generation FROM radio_software_identity_sets WHERE radio_profile_id=?",
                (radio_id,),
            ).fetchone()
            manifest_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM software_instance_manifests ORDER BY instance_key"
                ).fetchall()
            ]
            launch_rows: list[Dict[str, Any]] = []
            for raw in conn.execute(
                "SELECT * FROM radio_launch_bundle_items WHERE radio_profile_id=? "
                "ORDER BY display_order, instance_key",
                (radio_id,),
            ).fetchall():
                row = dict(raw)
                row["dependencies"] = _json(row.get("dependencies_json"), [])
                row["readiness"] = _json(row.get("readiness_json"), {})
                launch_rows.append(row)

        manifest_by_key = {
            str(row.get("instance_key") or ""): row for row in manifest_rows
        }
        issues_by_family: Dict[str, tuple[str, ...]] = {}
        selected_flags = {
            "js8call": bool(int(profile.get("use_js8call", 0) or 0)),
            "fast_light": any(
                bool(int(profile.get(key, 0) or 0))
                for key in ("use_flrig", "use_fldigi", "use_flmsg", "use_flamp")
            ),
            "varac": bool(int(profile.get("use_varac", 0) or 0)),
            "fio_spotter": bool(int(profile.get("use_js8spotter", 0) or 0)),
            "commstat": bool(int(profile.get("use_commstat", 0) or 0)),
            "external_js8spotter": bool(profile.get("use_external_js8spotter")) if profile.get("use_external_js8spotter") is not None else bool(str(profile.get("spotter_launch_path", "") or "").strip()),
            "sdrpp": str(profile.get("device_class", "") or "").strip().lower() == "observer",
        }
        if identity_set_row is not None and int(identity_set_row[0] or 0) > 0:
            persisted_families = {str(row[0] or "") for row in identity_rows}
            for selected_family, selected in selected_flags.items():
                if selected and selected_family not in persisted_families:
                    issues_by_family[selected_family] = (
                        "canonical identity record is missing",
                    )
        for raw in identity_rows:
            family = str(raw[0] or "")
            try:
                record = identity_record_from_mapping(json.loads(str(raw[1] or "{}")))
            except (TypeError, ValueError, json.JSONDecodeError):
                issues_by_family[family or "unknown"] = ("canonical identity JSON is invalid",)
                continue
            family_issues: list[str] = []
            if not selected_flags.get(record.family_key, True):
                family_issues.append("radio selection no longer includes this identity")
            if record.family_key in {"js8call", "fast_light", "varac"}:
                manifest = manifest_by_key.get(record.bundle_id)
                if manifest is None:
                    family_issues.append("software instance manifest is missing")
                elif str(manifest.get("family_key") or "") != record.family_key:
                    family_issues.append("software instance manifest family differs")
            else:
                manifest = None
            if manifest is not None:
                canonical_executable_tokens = {
                    str(token)
                    for component in record.components
                    for token in component.argv
                    if str(token).strip()
                }
                manifest_executable = str(manifest.get("executable_path") or "").strip()
                if (
                    manifest_executable
                    and manifest_executable not in canonical_executable_tokens
                ):
                    family_issues.append("application executable differs from its manifest projection")
                manifest_ports = _json(manifest.get("ports_json"), [])
                manifest_ports = manifest_ports if isinstance(manifest_ports, list) else []

                def _endpoint_key(value: Mapping[str, Any]) -> tuple[str, str, int]:
                    try:
                        port = int(value.get("port") or 0)
                    except (TypeError, ValueError):
                        port = 0
                    return (
                        re.sub(
                            r"[^a-z0-9]+",
                            "-",
                            str(
                                value.get("endpoint_key")
                                or value.get("name")
                                or ""
                            ).strip().casefold(),
                        ).strip("-"),
                        str(value.get("host") or "127.0.0.1").strip(),
                        port,
                    )

                canonical_endpoints = {
                    _endpoint_key(item) for item in record.endpoints if isinstance(item, Mapping)
                }
                projected_endpoints = {
                    _endpoint_key(item) for item in manifest_ports if isinstance(item, Mapping)
                }
                if canonical_endpoints != projected_endpoints:
                    family_issues.append("application endpoints differ from its manifest projection")
            component_executable_fields = {
                "js8call": {"js8call": profile.get("js8_install_path")},
                "fast_light": {
                    "flrig": profile.get("flrig_path"),
                    "fldigi": profile.get("fldigi_path"),
                    "flmsg": profile.get("flmsg_path"),
                    "flamp": profile.get("flamp_path"),
                },
                "external_js8spotter": {
                    "external-js8spotter": profile.get("spotter_launch_path")
                },
            }.get(record.family_key, {})
            for component in record.components:
                if component.component_id not in component_executable_fields:
                    continue
                expected_app_executable = str(
                    component_executable_fields.get(component.component_id) or ""
                ).strip()
                if record.family_key == 'js8call' and manifest is not None:
                    saved_evidence = _json(manifest.get('evidence_json'), {})
                    if isinstance(saved_evidence, Mapping) and saved_evidence.get('settings_identity_mode') == 'custom_launcher':
                        recipe = saved_evidence.get('launch_recipe') or {}
                        parts = recipe.get('components', ()) if isinstance(recipe, Mapping) else ()
                        reviewed = next((part for part in parts if isinstance(part, Mapping)
                                         and part.get('component_key') == component.component_id), None)
                        if reviewed is not None:
                            # An explicitly reviewed launcher can differ from
                            # the installed JS8 application. Native paths above
                            # and the launch projection below remain checked.
                            expected_app_executable = str(reviewed.get('executable') or '').strip()
                canonical_executable = str(component.argv[0] if component.argv else "").strip()
                if expected_app_executable != canonical_executable:
                    family_issues.append(
                        f"application component {component.component_id} executable differs"
                    )
            configuration_path = str(record.paths.get("configuration_path", "") or "")
            data_path = str(record.paths.get("data_path", "") or "")
            manifest_resource_values: Dict[str, str] = {}
            if manifest is not None:
                raw_manifest_resources = _json(
                    manifest.get("resource_claims_json"),
                    [],
                )
                if isinstance(raw_manifest_resources, list):
                    manifest_resource_values = {
                        str(item.get("kind") or "").strip(): str(
                            item.get("value") or ""
                        ).strip()
                        for item in raw_manifest_resources
                        if isinstance(item, Mapping)
                        and str(item.get("kind") or "").strip()
                        and str(item.get("value") or "").strip()
                    }
            family_projection_fields = {
                "js8call": {
                    "configuration path": (
                        profile.get("js8_profile_path"),
                        (manifest or {}).get("configuration_path"),
                    ),
                    "data path": (
                        profile.get("js8_message_storage_root"),
                        (manifest or {}).get("data_root"),
                    ),
                    "message paths": (
                        profile.get("js8_directed_path"),
                        profile.get("js8_all_path"),
                        profile.get("js8_inbox_path"),
                        profile.get("js8_save_dir"),
                        profile.get("js8_forms_path"),
                    ),
                },
                "fast_light": {
                    "configuration path": ((manifest or {}).get("configuration_path"),),
                    "data path": (
                        profile.get("fldigi_log_path"),
                        (manifest or {}).get("data_root"),
                    ),
                    "message paths": (
                        profile.get("fldigi_checkin_dir"),
                        profile.get("flmsg_message_path"),
                        profile.get("flamp_message_path"),
                        manifest_resource_values.get("flmsg_root"),
                        manifest_resource_values.get("flmsg_messages"),
                        manifest_resource_values.get("flmsg_templates"),
                        manifest_resource_values.get("flmsg_auto"),
                        manifest_resource_values.get("flamp_receive"),
                        manifest_resource_values.get("flamp_outgoing"),
                    ),
                },
                "varac": {
                    "configuration path": (
                        profile.get("varac_ini_path"),
                        (manifest or {}).get("configuration_path"),
                    ),
                    "data path": (
                        profile.get("varac_db_path"),
                        (manifest or {}).get("data_root"),
                    ),
                    "message paths": (
                        profile.get("varac_incoming_path"),
                        profile.get("varac_outbox_dir"),
                        profile.get("varac_bbs_dir"),
                        profile.get("varac_bbs_archive_dir"),
                    ),
                },
            }.get(record.family_key, {})
            for label, canonical_value in (
                ("configuration path", configuration_path),
                ("data path", data_path),
            ):
                if not canonical_value:
                    continue
                projected_values = tuple(
                    str(value)
                    for value in family_projection_fields.get(label, ())
                    if str(value or "").strip()
                )
                if not projected_values:
                    family_issues.append(f"{label} is missing from persisted application data")
                elif any(value != canonical_value for value in projected_values):
                    family_issues.append(f"{label} differs from persisted application data")
            persisted_message_paths = {
                str(value)
                for value in family_projection_fields.get("message paths", ())
                if str(value or "").strip()
            }
            for path in record.paths.get("message_paths", ()) or ():
                if str(path) and str(path) not in persisted_message_paths:
                    family_issues.append("message path differs from persisted application data")
                    break
            for component in record.components:
                if not component.argv or not str(component.argv[0]).strip():
                    if not (
                        bool(component.launch.get("operator_starts", False))
                        or bool(component.launch.get("built_in", False))
                        or record.scope == "built_in"
                    ):
                        family_issues.append(
                            f"launch component {component.component_id} executable is missing"
                        )
                    continue
                normalized_id = component.component_id.casefold().replace("_", "-")
                candidates = [
                    row
                    for row in launch_rows
                    if str(row.get("instance_key") or "").casefold().endswith(
                        ":" + normalized_id
                    )
                    or str(row.get("app_name") or "").casefold().replace(" ", "-")
                    == normalized_id
                    or (
                        record.family_key == "commstat"
                        and str(row.get("instance_key") or "") == _COMMSTAT_SHARED_INSTANCE_KEY
                    )
                    or (
                        record.family_key == "sdrpp"
                        and str(row.get("instance_key") or "").startswith("receiver:")
                    )
                ]
                exact_key = f"{record.bundle_id}:{component.component_id}".casefold()
                exact = [row for row in candidates
                         if str(row.get("instance_key") or "").casefold() == exact_key]
                if exact:
                    # Disabled historical rows are retained evidence, not a
                    # second launch identity. Enabled peers remain a conflict.
                    candidates = exact + [row for row in candidates if row not in exact
                                          and bool(row.get("enabled"))]
                if len(candidates) != 1:
                    family_issues.append(
                        f"launch component {component.component_id} is missing or duplicated"
                    )
                    continue
                launch_row = candidates[0]
                readiness = launch_row.get("readiness")
                readiness = readiness if isinstance(readiness, Mapping) else {}
                executable = str(
                    readiness.get("executable")
                    or launch_row.get("path_override")
                    or launch_row.get("command_override")
                    or ""
                ).strip()
                if component.argv[0] and component.argv[0] != executable:
                    family_issues.append(
                        f"launch component {component.component_id} executable differs"
                    )
                launch_arguments = tuple(
                    str(item) for item in readiness.get("launch_arguments", ()) or ()
                )
                if tuple(component.argv[1:]) != launch_arguments:
                    family_issues.append(
                        f"launch component {component.component_id} arguments differ"
                    )
                working_directory = str(readiness.get("working_directory") or "").strip()
                if component.cwd != working_directory:
                    family_issues.append(
                        f"launch component {component.component_id} working directory differs"
                    )
                persisted_environment = readiness.get("environment", {})
                if not isinstance(persisted_environment, Mapping):
                    persisted_environment = {}
                expected_environment = {
                    str(key): str(value) for key, value in component.env.items()
                }
                actual_environment = {
                    str(key): str(value) for key, value in persisted_environment.items()
                }
                if expected_environment != actual_environment:
                    family_issues.append(
                        f"launch component {component.component_id} environment differs"
                    )
                expected_dependencies = tuple(
                    str(item).strip().casefold().replace("_", "-")
                    for item in component.dependencies
                    if str(item).strip()
                )
                cross_family = record.launch.get("cross_family_dependencies", {})
                cross_family = cross_family if isinstance(cross_family, Mapping) else {}
                cross_items = cross_family.get(component.component_id, ())
                cross_items = cross_items if isinstance(cross_items, (tuple, list)) else ()
                expected_dependencies += tuple(
                    str(item.get("family_key") or "").strip().casefold().replace("_", "-")
                    for item in cross_items
                    if isinstance(item, Mapping) and str(item.get("family_key") or "").strip()
                )
                actual_dependencies = tuple(
                    str(item).strip().casefold().replace("_", "-")
                    for item in launch_row.get("dependencies", ()) or ()
                    if str(item).strip()
                )
                if expected_dependencies != actual_dependencies:
                    family_issues.append(
                        f"launch component {component.component_id} dependencies differ"
                    )
                # Launch-at-startup and health-monitoring are operator-owned
                # Launch Control preferences, not immutable software identity.
                # They may legitimately change after Add Radio and must not be
                # reported as canonical application/recipe drift.
                for key, expected_value in component.readiness.items():
                    if key not in readiness or str(readiness.get(key) or "").strip() != str(expected_value or "").strip():
                        family_issues.append(
                            f"launch component {component.component_id} readiness differs"
                        )
                        break
            if family_issues:
                issues_by_family[record.family_key] = tuple(dict.fromkeys(family_issues))
        return issues_by_family

    def save_radio_software_identity_records(
        self,
        radio_profile_id: int,
        records: Iterable[Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> tuple[Any, ...]:
        """Atomically replace one radio's complete canonical identity set.

        This method participates in ``guided_save_transaction`` when one is
        active.  No family row is independently patched: omission means the
        family is no longer selected, and a stale generation rejects the whole
        replacement.
        """

        from freqinout.core.software_identity_bundle import (
            identity_record_from_mapping,
            identity_record_to_mapping,
            validate_identity_parity,
        )

        radio_id = int(radio_profile_id)
        normalized = tuple(
            identity_record_from_mapping(identity_record_to_mapping(record))
            for record in records
        )
        family_keys = [record.family_key for record in normalized]
        identity_keys = [record.identity_key for record in normalized]
        if len(set(family_keys)) != len(family_keys):
            raise ValueError("Duplicate software family in one canonical radio identity set.")
        if len(set(identity_keys)) != len(identity_keys):
            raise ValueError("Duplicate software identity key in one canonical radio identity set.")

        now_iso = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                profile_row = conn.execute(
                    "SELECT system_key FROM device_profiles WHERE id=?",
                    (radio_id,),
                ).fetchone()
                if profile_row is None:
                    raise KeyError(f"Unknown radio profile id: {radio_id}")
                radio_key = str(profile_row[0] or "").strip()
                normalized_radio_key = _normalize_system_key(radio_key, "radio")
                station_families = {"fio_spotter", "commstat"}
                for record in normalized:
                    if (
                        record.family_key not in station_families
                        and _normalize_system_key(record.owner, "owner") != normalized_radio_key
                    ):
                        raise ValueError(
                            "Radio-scoped software identity owner does not match the target radio."
                        )
                    mismatched_bindings = [
                        binding.binding_id
                        for binding in record.bindings
                        if binding.radio_key
                        and _normalize_system_key(binding.radio_key, "binding")
                        != normalized_radio_key
                    ]
                    if mismatched_bindings:
                        raise ValueError(
                            "Software identity binding does not match the target radio: "
                            + ", ".join(mismatched_bindings)
                        )
                generation_row = conn.execute(
                    "SELECT generation FROM radio_software_identity_sets WHERE radio_profile_id=?",
                    (radio_id,),
                ).fetchone()
                current_generation = int(generation_row[0]) if generation_row is not None else 0
                if (
                    expected_generation is not None
                    and int(expected_generation) != current_generation
                ):
                    raise ValueError(
                        "The software identity draft has a stale generation; refresh and review it again."
                    )
                next_generation = current_generation + 1
                conn.execute(
                    """
                    INSERT INTO radio_software_identity_sets
                        (radio_profile_id, schema_version, generation, updated_utc)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(radio_profile_id) DO UPDATE SET
                        schema_version=1,
                        generation=excluded.generation,
                        updated_utc=excluded.updated_utc
                    """,
                    (radio_id, next_generation, now_iso),
                )
                conn.execute(
                    "DELETE FROM radio_software_identity_records WHERE radio_profile_id=?",
                    (radio_id,),
                )
                for display_order, record in enumerate(normalized):
                    payload = identity_record_to_mapping(record)
                    conn.execute(
                        """
                        INSERT INTO radio_software_identity_records (
                            radio_profile_id, family_key, identity_key, bundle_id,
                            display_order, fingerprint, record_json, updated_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            radio_id,
                            record.family_key,
                            record.identity_key,
                            record.bundle_id,
                            display_order,
                            record.fingerprint,
                            json.dumps(payload, sort_keys=True, separators=(",", ":")),
                            now_iso,
                        ),
                    )
                rows = conn.execute(
                    "SELECT record_json FROM radio_software_identity_records "
                    "WHERE radio_profile_id=? ORDER BY display_order, family_key",
                    (radio_id,),
                ).fetchall()
                reloaded = tuple(
                    identity_record_from_mapping(json.loads(str(row[0])))
                    for row in rows
                )
                parity_issues = validate_identity_parity(normalized, reloaded)
                if parity_issues:
                    raise ValueError(
                        "Canonical software identity readback failed: " + "; ".join(parity_issues)
                    )
                conn.commit()
                return reloaded
            except Exception:
                conn.rollback()
                raise

    def prepare_fast_light_message_component_repair(
        self,
        radio_profile_id: int,
        *,
        platform: object | None = None,
        storage_home: Path | None = None,
    ) -> Any:
        """Prepare a reviewed FLMsg/FLAmp-only repair for one saved radio.

        Legacy Fast Light records can predate qualified radio-scoped FLMsg and
        FLAmp launch identities.  This method is read-only: it allocates a
        deterministic distinct ARQ port from persisted station evidence and
        returns the complete merged transaction for operator review.
        """

        from freqinout.core.fast_light_component_repair import (
            build_fast_light_component_repair_plan,
            build_legacy_fast_light_repair_inputs,
        )

        radio_id = int(radio_profile_id)
        profile = self.get_device_profile(radio_id)
        if not isinstance(profile, Mapping):
            raise KeyError(f"Unknown radio profile id: {radio_id}")
        records = self.list_radio_software_identity_records(radio_id)
        fast_light = next((record for record in records if record.family_key == "fast_light"), None)
        legacy_bootstrap = fast_light is None
        manifest: Mapping[str, Any] | None = None
        application: Mapping[str, Any] | None = None
        source_fingerprint = ""
        if fast_light is not None:
            manifest = self.get_software_instance_manifest(fast_light.bundle_id)
            if not isinstance(manifest, Mapping):
                raise ValueError("The selected Fast Light identity has no saved manifest to repair.")
        else:
            application_id = _coerce_int(profile.get("fast_light_config_id"), 0)
            application = self.get_fast_light_config(application_id) if application_id else None
            if not isinstance(application, Mapping):
                raise ValueError(
                    "The radio has no unambiguous linked Fast Light configuration to repair."
                )
            matching = [
                row
                for row in self.list_software_instance_manifests()
                if isinstance(row, Mapping)
                and _coerce_text(row.get("family_key"), "") == "fast_light"
                and _coerce_text(row.get("application_system_key"), "")
                == _coerce_text(application.get("system_key"), "")
            ]
            if len(matching) > 1:
                raise ValueError(
                    "The linked Fast Light configuration has multiple saved identities; resolve that ambiguity before repair."
                )
            manifest = matching[0] if matching else None

        manifests = self.list_software_instance_manifests()
        occupied_ports: set[int] = set()
        explicit_arq_by_application: Dict[str, int] = {}
        for row in manifests:
            if not isinstance(row, Mapping):
                continue
            application_key = _coerce_text(row.get("application_system_key", ""), "")
            for raw_endpoint in row.get("ports", ()) or ():
                if not isinstance(raw_endpoint, Mapping):
                    continue
                port = _coerce_int(raw_endpoint.get("port"), 0)
                if 1 <= port <= 65535:
                    occupied_ports.add(port)
                    label = _coerce_text(raw_endpoint.get("name", ""), "").casefold()
                    if row.get("family_key") == "fast_light" and "arq" in label:
                        explicit_arq_by_application[application_key] = port

        target_application_key = _coerce_text(
            (manifest or {}).get("application_system_key", "")
            or (application or {}).get("system_key", ""),
            "",
        )
        arq_port = explicit_arq_by_application.get(target_application_key, 0)
        if not arq_port:
            # Older manifests have no ARQ claim.  Reserve one deterministic
            # legacy slot per earlier FLAmp radio so a second instance does not
            # inherit the first pair's conventional 7322 listener.
            with self._connect_readonly() as conn:
                flamp_radios = conn.execute(
                    """
                    SELECT d.id, f.system_key
                      FROM device_profiles d
                      JOIN fast_light_configs f ON f.id=d.fast_light_config_id
                     WHERE d.use_flamp=1
                     ORDER BY d.id
                    """
                ).fetchall()
            candidate = 7322
            for row in flamp_radios:
                application_key = str(row[1] or "").strip()
                explicit = explicit_arq_by_application.get(application_key, 0)
                if explicit:
                    if int(row[0]) == radio_id:
                        arq_port = explicit
                        break
                    continue
                while candidate in occupied_ports:
                    candidate += 1
                    if candidate > 65535:
                        raise ValueError("No distinct FLAmp ARQ port is available.")
                reserved = candidate
                occupied_ports.add(reserved)
                candidate += 1
                if int(row[0]) == radio_id:
                    arq_port = reserved
                    break
        if not arq_port:
            raise ValueError("CommsPlan could not allocate a distinct FLAmp ARQ port for this radio.")

        launch_bundle = self.get_radio_launch_bundle(radio_id)
        if legacy_bootstrap:
            assert isinstance(application, Mapping)
            manifest, records, source_fingerprint = build_legacy_fast_light_repair_inputs(
                profile=profile,
                application=application,
                launch_bundle=launch_bundle,
                arq_port=arq_port,
                platform=platform,
                storage_home=storage_home,
                existing_manifest=manifest,
            )
        assert isinstance(manifest, Mapping)
        plan = build_fast_light_component_repair_plan(
            profile=profile,
            manifest=manifest,
            identity_records=records,
            identity_generation=self.radio_software_identity_generation(radio_id),
            launch_bundle=launch_bundle,
            arq_port=arq_port,
            platform=platform,
            storage_home=storage_home,
        )
        return replace(
            plan,
            bootstrap_legacy=legacy_bootstrap,
            source_fingerprint=source_fingerprint,
        )

    def fast_light_message_component_repair_needed(self, radio_profile_id: int) -> bool:
        """Return whether launch-critical legacy FLMsg/FLAmp wiring is repairable.

        This is intentionally based only on persisted FIO state.  It performs
        no discovery, filesystem writes, or application launch.
        """

        radio_id = int(radio_profile_id or 0)
        profile = self.get_device_profile(radio_id) if radio_id > 0 else None
        if not isinstance(profile, Mapping):
            return False
        selected_flmsg = bool(_coerce_int(profile.get("use_flmsg"), 0))
        selected_flamp = bool(_coerce_int(profile.get("use_flamp"), 0))
        if not (selected_flmsg or selected_flamp):
            return False
        records = self.list_radio_software_identity_records(radio_id)
        record = next((item for item in records if item.family_key == "fast_light"), None)
        if record is None:
            return bool(_coerce_int(profile.get("fast_light_config_id"), 0))
        components = {item.component_id.casefold(): item for item in record.components}
        flmsg = components.get("flmsg")
        flamp = components.get("flamp")
        if selected_flmsg and (flmsg is None or (
            not flmsg.launch.get("operator_starts", False) and "--auto-dir" in flmsg.argv
        )):
            return True
        if selected_flamp and (
            flamp is None
            or (not flamp.launch.get("operator_starts", False) and (
                "--config-dir" not in flamp.argv
                or "--arq-server-port" not in flamp.argv
                or not flamp.cwd
            ))
        ):
            return True
        if flamp is not None and flamp.launch.get("operator_starts", False):
            # An explicitly operator/remote-managed application uses its own
            # native settings. Do not demand a CP-generated profile or argv.
            return False
        return any(
            str(item.get("app_name") or "").strip().casefold() == "flamp"
            and "station-shared" in str(item.get("instance_key") or "").casefold()
            for item in self.get_radio_launch_bundle(radio_id).get("items", ()) or ()
            if isinstance(item, Mapping)
        )

    def apply_fast_light_message_component_repair(self, plan: Any) -> Dict[str, Any]:
        """Persist one reviewed component repair as an optimistic transaction."""

        from freqinout.core.software_identity_bundle import identity_record_to_mapping

        radio_id = int(getattr(plan, "radio_profile_id", 0) or 0)
        if radio_id <= 0:
            raise ValueError("The FLMsg/FLAmp repair has no saved radio identity.")
        before_records = self.list_radio_software_identity_records(radio_id)
        before_by_family = {
            record.family_key: identity_record_to_mapping(record) for record in before_records
        }
        bootstrap_legacy = bool(getattr(plan, "bootstrap_legacy", False))
        before_fast_light = before_by_family.get("fast_light")
        if not bootstrap_legacy and not isinstance(before_fast_light, Mapping):
            raise ValueError("The selected radio no longer has a Fast Light identity.")
        before_components = {
            str(item.get("component_id") or "").casefold(): dict(item)
            for item in (before_fast_light or {}).get("components", ()) or ()
            if isinstance(item, Mapping)
        }

        with self.guided_save_transaction() as transaction:
            if self.radio_software_identity_generation(radio_id) != int(plan.identity_generation):
                raise ValueError(
                    "The Fast Light identity changed after review. Refresh Software Administration and review the repair again."
                )
            current_manifest = self.get_software_instance_manifest(plan.manifest_instance_key)
            if str((current_manifest or {}).get("updated_utc") or "") != str(
                plan.manifest_updated_utc or ""
            ):
                raise ValueError(
                    "The Fast Light manifest changed after review. Refresh Software Administration and review the repair again."
                )
            if bootstrap_legacy:
                from freqinout.core.fast_light_component_repair import (
                    legacy_repair_source_fingerprint,
                )

                if any(record.family_key == "fast_light" for record in before_records):
                    raise ValueError(
                        "The Fast Light identity changed after review. Refresh Software Administration and review the repair again."
                    )
                current_profile = self.get_device_profile(radio_id)
                application_id = _coerce_int((current_profile or {}).get("fast_light_config_id"), 0)
                current_application = (
                    self.get_fast_light_config(application_id) if application_id else None
                )
                if not isinstance(current_profile, Mapping) or not isinstance(
                    current_application, Mapping
                ):
                    raise ValueError(
                        "The linked Fast Light configuration changed after review. Refresh and review the repair again."
                    )
                current_source = legacy_repair_source_fingerprint(
                    current_profile,
                    current_application,
                    self.get_radio_launch_bundle(radio_id),
                    current_manifest,
                )
                if current_source != str(plan.source_fingerprint or ""):
                    raise ValueError(
                        "The legacy Fast Light configuration changed after review. Refresh Software Administration and review the repair again."
                    )

            saved_profile = self.save_device_profile(plan.profile)
            saved_manifest = self.save_software_instance_manifest(plan.manifest)
            saved_records = self.save_radio_software_identity_records(
                radio_id,
                plan.identity_records,
                expected_generation=int(plan.identity_generation),
            )
            self.save_radio_launch_bundle(
                radio_id,
                launch_enabled=bool(plan.launch_enabled),
                items=plan.launch_items,
            )

            after_by_family = {
                record.family_key: identity_record_to_mapping(record) for record in saved_records
            }
            for family_key, prior in before_by_family.items():
                if family_key != "fast_light" and after_by_family.get(family_key) != prior:
                    raise ValueError(
                        f"Component repair changed unrelated {family_key} identity; nothing was saved."
                    )
            after_fast_light = after_by_family.get("fast_light", {})
            after_components = {
                str(item.get("component_id") or "").casefold(): dict(item)
                for item in after_fast_light.get("components", ()) or ()
                if isinstance(item, Mapping)
            }
            if not bootstrap_legacy and after_components.get("flrig") != before_components.get("flrig"):
                raise ValueError("Component repair changed FLRig identity; nothing was saved.")
            prior_fldigi = dict(before_components.get("fldigi") or {})
            next_fldigi = dict(after_components.get("fldigi") or {})
            prior_fldigi.pop("argv", None)
            next_fldigi.pop("argv", None)
            # Argument-derived readiness mirrors the repaired argv. Everything
            # else remains part of the retained FLDigi identity.
            for component in (prior_fldigi, next_fldigi):
                component["readiness"] = {
                    key: value for key, value in (component.get("readiness") or {}).items()
                    if key not in {"launch_arguments", "effective_command", "effective_command_text"}
                }
            if not bootstrap_legacy and next_fldigi != prior_fldigi:
                raise ValueError(
                    "Component repair changed FLDigi outside its required FLMsg/FLAmp pairing arguments; nothing was saved."
                )
            parity_issues = self.validate_radio_software_identity_projections(radio_id)
            if parity_issues.get("fast_light"):
                raise ValueError(
                    "Component repair did not pass canonical readback: "
                    + "; ".join(parity_issues["fast_light"])
                )
            transaction.complete()

        return {
            "radio": saved_profile,
            "manifest": saved_manifest,
            "identity_records": saved_records,
            "arq_port": int(plan.arq_port),
        }

    def adopt_software_instance(
        self,
        *,
        family_key: str,
        radio_profile_id: int,
        application_values: Mapping[str, Any],
        manifest_values: Mapping[str, Any],
        replace_existing: bool = False,
        expected_current_instance_id: Any = _SOFTWARE_INSTANCE_EXPECTATION_UNSET,
        launch_at_startup: bool = False,
        varac_cluster_db_id: Optional[int] = None,
        varac_cluster_instance_number: Optional[int] = None,
        varac_create_cluster_values: Optional[Mapping[str, Any]] = None,
        varac_existing_standalone_node_id: Optional[int] = None,
        require_observer_receive_only: bool = False,
    ) -> Dict[str, Any]:
        """Persist one reviewed application instance and radio link atomically.

        This method only changes FIO's settings database. ``fio_managed`` means
        FIO owns the durable launch recipe and radio assignment; it does not
        imply that FIO rewrote a third-party application's native settings.
        """

        family = str(family_key or "").strip().lower()
        if family not in {"js8call", "fast_light", "varac"}:
            raise ValueError(f"Unsupported software instance family: {family or 'blank'}")
        radio_id = int(radio_profile_id or 0)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                profile = _record_by_id(conn, "device_profiles", radio_id)
                if profile is None:
                    raise KeyError(f"Unknown radio profile id: {radio_id}")
                observer_profile = _is_observer_device_class(profile)
                if require_observer_receive_only and (
                    family not in {"js8call", "fast_light"} or not observer_profile
                ):
                    raise ValueError(
                        "Receive-only software instances can only be assigned to observer / SDR profiles."
                    )
                requested_manifest = dict(manifest_values or {})
                requested_evidence = requested_manifest.get(
                    "evidence",
                    requested_manifest.get("evidence_json", {}),
                )
                if not isinstance(requested_evidence, Mapping):
                    requested_evidence = {}
                requested_evidence = dict(requested_evidence)
                if observer_profile and family == "varac":
                    raise ValueError("Observer / SDR device profiles cannot use VarAC or VarAC clusters.")
                if observer_profile and family == "fast_light":
                    if str(application_values.get("flrig_path", "") or "").strip():
                        raise ValueError(
                            "Observer / SDR Fast Light workflows cannot include FLRig, CAT, or PTT control."
                        )
                    if not (
                        bool(requested_evidence.get("receive_only_ingest"))
                        and requested_evidence.get("transmit_authority") is False
                    ):
                        raise ValueError(
                            "Observer / SDR Fast Light requires reviewed receive-only evidence."
                        )
                    requested_evidence.update(
                        {
                            "receive_only_ingest": True,
                            "transmit_authority": False,
                            "execution_scope": RECEIVE_ONLY_EXECUTION_SCOPE,
                        }
                    )
                if family == "fast_light" and bool(requested_evidence.get("advanced_tx_requested")):
                    if observer_profile:
                        raise ValueError("Advanced Fast Light TX is not available for an observer / SDR.")
                    if not bool(requested_evidence.get("advanced_tx_acknowledged")):
                        raise ValueError(
                            "Advanced Fast Light TX requires explicit acknowledgement before saving."
                        )
                cluster_db_id_value = (
                    int(varac_cluster_db_id)
                    if varac_cluster_db_id is not None
                    else None
                )
                create_cluster_values = dict(varac_create_cluster_values or {})
                if create_cluster_values and family != "varac":
                    raise ValueError("Only VarAC instances can create a VarAC cluster.")
                if create_cluster_values and cluster_db_id_value is not None:
                    raise ValueError("Choose either a new VarAC cluster or an existing cluster, not both.")
                existing_standalone_node_id = (
                    int(varac_existing_standalone_node_id)
                    if varac_existing_standalone_node_id is not None
                    else _coerce_optional_int(
                        create_cluster_values.get(
                            "existing_standalone_node_id",
                            create_cluster_values.get("existing_varac_node_id", create_cluster_values.get("existing_node_id")),
                        )
                    )
                )
                gateway_for_new_cluster = _coerce_bool_int(
                    create_cluster_values.get("gateway_for_new_cluster", False),
                    False,
                )
                gateway_existing_standalone = _coerce_bool_int(
                    create_cluster_values.get("gateway_existing_standalone", False),
                    False,
                )
                email_gateway_sender_choice = _coerce_text(
                    create_cluster_values.get("email_gateway_sender_choice", "none"),
                    "none",
                ).lower()
                if email_gateway_sender_choice not in {"none", "existing_member", "new_member"}:
                    raise ValueError(
                        "Choose No email gateway, existing member, or new member for the native VarAC cluster."
                    )
                create_cluster_native_state = _coerce_text(
                    create_cluster_values.get("native_management_state", "operator"),
                    "operator",
                ).lower()
                if create_cluster_native_state not in SUPPORTED_VARAC_NATIVE_MANAGEMENT_STATES:
                    raise ValueError("Unsupported VarAC native management state.")
                if gateway_for_new_cluster and gateway_existing_standalone:
                    raise ValueError("Choose exactly one VarAC cluster gateway policy.")
                if gateway_existing_standalone and existing_standalone_node_id is None:
                    raise ValueError(
                        "An existing standalone node is required for the selected gateway policy."
                    )
                if existing_standalone_node_id is not None and cluster_db_id_value is not None:
                    raise ValueError("Choose either an existing VarAC cluster or an existing standalone node to seed a new cluster.")
                if existing_standalone_node_id is not None and not create_cluster_values:
                    raise ValueError("An existing standalone VarAC node can only be included when creating a new cluster.")
                existing_standalone_device_id: Optional[int] = None
                if existing_standalone_node_id is not None:
                    if family != "varac":
                        raise ValueError("Only VarAC instances can include an existing standalone node.")
                    existing_standalone = _record_by_id(conn, "varac_nodes", existing_standalone_node_id)
                    if existing_standalone is None:
                        raise KeyError(f"Unknown existing standalone VarAC node id: {existing_standalone_node_id}")
                    existing_standalone_device = conn.execute(
                        "SELECT id, device_class FROM device_profiles WHERE varac_node_id=? LIMIT 1",
                        (existing_standalone_node_id,),
                    ).fetchone()
                    if existing_standalone_device is None:
                        raise ValueError("The selected VarAC node is not assigned to a radio and cannot seed a cluster.")
                    existing_standalone_device_id = int(existing_standalone_device[0] or 0)
                    if str(existing_standalone_device[1] or "").strip().lower() == "observer":
                        raise ValueError("An observer / SDR profile cannot seed a VarAC cluster.")
                    if _device_has_varac_cluster_membership(conn, existing_standalone_device_id):
                        raise ValueError("The selected VarAC node already belongs to a cluster; choose a standalone node.")
                family_link_column = _SOFTWARE_INSTANCE_ASSIGNMENTS[family][0]
                expected_current = (
                    _coerce_optional_int(expected_current_instance_id)
                    if expected_current_instance_id is not _SOFTWARE_INSTANCE_EXPECTATION_UNSET
                    else _SOFTWARE_INSTANCE_EXPECTATION_UNSET
                )
                actual_current = _coerce_optional_int(profile.get(family_link_column))
                if (
                    expected_current is not _SOFTWARE_INSTANCE_EXPECTATION_UNSET
                    and actual_current != expected_current
                ):
                    raise ValueError("The radio's software assignment changed. Refresh and review it before replacing it.")
                app_values = dict(application_values or {})
                allowed_varac_shared_db_path = ""
                if (
                    family == "varac"
                    and (str(app_values.get("native_management_state") or "operator").strip().lower()
                         == "managed"
                         or (cluster_db_id_value is not None
                             and _coerce_optional_int(app_values.get("id")) == actual_current
                             and actual_current is not None
                             and conn.execute('''SELECT 1 FROM varac_cluster_members
                                 WHERE cluster_id=? AND device_profile_id=? AND enabled=1''',
                                 (cluster_db_id_value, radio_id)).fetchone() is not None))
                ):
                    if create_cluster_values:
                        allowed_varac_shared_db_path = _coerce_text(
                            create_cluster_values.get("shared_db_path", ""), ""
                        )
                    elif cluster_db_id_value is not None:
                        selected_cluster = _varac_cluster_by_id(conn, int(cluster_db_id_value)) or {}
                        allowed_varac_shared_db_path = _coerce_text(
                            selected_cluster.get("shared_db_path", ""), ""
                        )
                        # An existing cluster is proof for
                        # its own members only, never an unrelated node.
                        for other_node in conn.execute(
                            "SELECT id, name, db_path FROM varac_nodes WHERE id<>? AND "
                            "(enabled<>0 OR EXISTS (SELECT 1 FROM device_profiles d WHERE d.varac_node_id=varac_nodes.id))",
                            (_coerce_optional_int(app_values.get('id')) or -1,),
                        ).fetchall():
                            if (allowed_varac_shared_db_path
                                    and _coerce_text(other_node['db_path'], '')
                                    and normalize_varac_path(other_node['db_path'], 'VarAC database')
                                    == normalize_varac_path(allowed_varac_shared_db_path, 'VarAC shared database')):
                                member = conn.execute('''SELECT 1 FROM device_profiles d
                                    JOIN varac_cluster_members m ON m.device_profile_id=d.id
                                    WHERE d.varac_node_id=? AND m.cluster_id=? AND m.enabled=1''',
                                    (other_node['id'], cluster_db_id_value)).fetchone()
                                if member is None:
                                    raise ValueError(
                                        f"VarAC database is already assigned to {other_node['name']} outside this cluster."
                                    )
                _validate_software_application_claims_conn(
                    conn,
                    family,
                    app_values,
                    allowed_varac_shared_db_path=allowed_varac_shared_db_path,
                    reviewed_existing_id=(actual_current if _coerce_optional_int(app_values.get('id')) == actual_current else None),
                )
                if family == "js8call":
                    saved_app = _save_js8_instance_conn(conn, app_values)
                    link_column = "js8_instance_id"
                    updates = {
                        "js8_instance_id": int(saved_app["id"]),
                        "use_js8call": 1,
                        "js8_host": str(saved_app.get("host", "127.0.0.1") or "127.0.0.1"),
                        "js8_port": int(saved_app.get("port", 2442) or 2442),
                        "js8_profile_path": str(saved_app.get("profile_path", "") or ""),
                        "js8_directed_path": str(saved_app.get("directed_path", "") or ""),
                        "js8_forms_path": str(saved_app.get("forms_path", "") or ""),
                    }
                elif family == "fast_light":
                    saved_app = _save_fast_light_config_conn(conn, app_values)
                    link_column = "fast_light_config_id"
                    requested_resources = {
                        str(item.get("kind", "") or "").strip(): str(item.get("value", "") or "").strip()
                        for item in requested_manifest.get("resource_claims", ()) or ()
                        if isinstance(item, Mapping)
                    }
                    recipe = requested_evidence.get("launch_recipe", {})
                    if isinstance(recipe, Mapping):
                        recipe_claim_specs = {
                            "flrig": (("flrig_configuration", "configuration_roots", 0),),
                            "fldigi": (
                                ("fldigi_configuration", "configuration_roots", 0),
                                ("fldigi_logs", "data_roots", 0),
                                ("fldigi_checkins", "data_roots", 1),
                            ),
                            "flmsg": (
                                ("flmsg_root", "configuration_roots", 0),
                                ("flmsg_messages", "data_roots", 0),
                                ("flmsg_templates", "data_roots", 1),
                                ("flmsg_auto", "data_roots", 2),
                            ),
                            "flamp": (
                                ("flamp_receive", "data_roots", 0),
                                ("flamp_outgoing", "data_roots", 1),
                            ),
                        }
                        for raw_component in recipe.get("components", ()) or ():
                            if not isinstance(raw_component, Mapping):
                                continue
                            component_key = str(
                                raw_component.get("component_key") or ""
                            ).strip().lower()
                            executable = str(raw_component.get("executable") or "").strip()
                            if executable and component_key in {"flmsg", "flamp"}:
                                requested_resources.setdefault(
                                    f"{component_key}_application",
                                    executable,
                                )
                            for kind, root_key, index in recipe_claim_specs.get(component_key, ()):
                                roots = raw_component.get(root_key, ()) or ()
                                value = str(roots[index] if index < len(roots) else "").strip()
                                if value:
                                    requested_resources.setdefault(kind, value)
                    exclusive_kinds = {
                        "flrig_configuration",
                        "fldigi_configuration",
                        "fldigi_logs",
                        "fldigi_checkins",
                        "flmsg_root",
                        "flmsg_messages",
                        "flmsg_templates",
                        "flmsg_auto",
                    }
                    requested_manifest["resource_claims"] = [
                        {
                            "kind": kind,
                            "value": value,
                            "exclusive": kind in exclusive_kinds,
                        }
                        for kind, value in requested_resources.items()
                        if kind and value
                    ]
                    flmsg_path = requested_resources.get("flmsg_application", "")
                    flamp_path = requested_resources.get("flamp_application", "")
                    flmsg_message_path = requested_resources.get("flmsg_messages", "")
                    flamp_message_path = requested_resources.get("flamp_receive", "")
                    updates = {
                        "fast_light_config_id": int(saved_app["id"]),
                        "use_flrig": 0 if observer_profile else (1 if str(saved_app.get("flrig_path") or "").strip() else 0),
                        "use_fldigi": 1 if str(saved_app.get("fldigi_path") or "").strip() else 0,
                        "use_flmsg": 1 if flmsg_path else 0,
                        "use_flamp": 1 if flamp_path else 0,
                        "flmsg_path": flmsg_path,
                        "flmsg_message_path": flmsg_message_path,
                        "flamp_path": flamp_path,
                        "flamp_message_path": flamp_message_path,
                        "flrig_host": str(saved_app.get("flrig_host", "127.0.0.1") or "127.0.0.1"),
                        "flrig_port": int(saved_app.get("flrig_port", 12345) or 12345),
                        "fldigi_host": str(saved_app.get("fldigi_host", "127.0.0.1") or "127.0.0.1"),
                        "fldigi_port": int(saved_app.get("fldigi_port", 7362) or 7362),
                        "fldigi_log_path": str(saved_app.get("fldigi_log_path", "") or ""),
                        "fldigi_checkin_dir": str(saved_app.get("fldigi_checkin_dir", "") or ""),
                    }
                else:
                    saved_app = _save_varac_node_conn(conn, app_values)
                    link_column = "varac_node_id"
                    updates = {
                        "varac_node_id": int(saved_app["id"]),
                        "use_varac": 1,
                        "varac_install_path": str(saved_app.get("install_path", "") or ""),
                        "varac_db_path": str(saved_app.get("db_path", "") or ""),
                        "varac_ini_path": str(saved_app.get("ini_path", "") or ""),
                        "varac_outbox_dir": str(app_values.get("outbox_path", "") or ""),
                        "varac_bbs_dir": str(app_values.get("bbs_path", "") or ""),
                        "varac_bbs_archive_dir": str(
                            app_values.get("bbs_archive_path", "") or ""
                        ),
                    }
                    if create_cluster_values:
                        cluster_name = _coerce_text(
                            create_cluster_values.get("name", create_cluster_values.get("cluster_id", "")),
                            "VarAC Cluster",
                        ) or "VarAC Cluster"
                        public_cluster_id = _normalize_varac_cluster_id(
                            create_cluster_values.get("cluster_id", cluster_name),
                            _normalize_varac_cluster_id(cluster_name),
                        )
                        duplicate_cluster = conn.execute(
                            "SELECT id FROM varac_clusters WHERE UPPER(cluster_id)=? LIMIT 1",
                            (public_cluster_id,),
                        ).fetchone()
                        if duplicate_cluster is not None:
                            raise ValueError(f"VarAC cluster ID {public_cluster_id} is already in use.")
                        shared_db_path = _coerce_text(create_cluster_values.get("shared_db_path", ""), "")
                        shared_bbs_path = _coerce_text(
                            create_cluster_values.get("shared_bbs_path", ""), ""
                        )
                        shared_bbs_archive_path = _coerce_text(
                            create_cluster_values.get("shared_bbs_archive_path", ""), ""
                        )
                        normalized_shared_db = (
                            normalize_varac_path(shared_db_path, "VarAC shared database path")
                            if shared_db_path
                            else ""
                        )
                        new_node_db = _coerce_text(app_values.get("db_path", ""), "")
                        normalized_new_node_db = (
                            normalize_varac_path(new_node_db, "VarAC db_path")
                            if new_node_db
                            else ""
                        )
                        if create_cluster_native_state == "managed":
                            if not normalized_shared_db:
                                raise ValueError(
                                    "A native-managed VarAC cluster requires one effective shared database."
                                )
                            if normalized_new_node_db != normalized_shared_db:
                                raise ValueError(
                                    "A native-managed VarAC member must use the cluster's effective shared database."
                                )
                        node_local_paths = {
                            normalize_varac_path(
                                _coerce_text(app_values.get(key, ""), ""),
                                f"VarAC {key}",
                            )
                            for key in (
                                "ini_path",
                                *(("db_path",) if create_cluster_native_state != "managed" else ()),
                                "incoming_path",
                                "outbox_path",
                            )
                            if _coerce_text(app_values.get(key, ""), "")
                        }
                        if normalized_shared_db and normalized_shared_db in node_local_paths:
                            raise ValueError(
                                "A VarAC cluster shared database cannot replace a node-local INI, database, incoming, or outbox path."
                            )
                        if normalized_shared_db:
                            existing_cluster_paths = {
                                normalize_varac_path(str(row[0]), "VarAC shared database path")
                                for row in conn.execute(
                                    "SELECT shared_db_path FROM varac_clusters WHERE shared_db_path IS NOT NULL AND TRIM(shared_db_path)<>''"
                                ).fetchall()
                                if str(row[0] or "").strip()
                            }
                            if normalized_shared_db in existing_cluster_paths:
                                raise ValueError(
                                    "The VarAC cluster shared database is already owned by another cluster."
                                )
                            existing_node_paths = {
                                normalize_varac_path(str(value), "VarAC node-local path")
                                for row in conn.execute(
                                    "SELECT id, ini_path, db_path, incoming_path FROM varac_nodes"
                                ).fetchall()
                                if int(row[0] or 0)
                                not in {
                                    int(existing_standalone_node_id or 0),
                                    int(saved_app.get("id") or 0),
                                }
                                for value in row[1:]
                                if str(value or "").strip()
                            }
                            existing_node_paths.update(
                                normalize_varac_path(str(row[0]), "VarAC node outbox path")
                                for row in conn.execute(
                                    "SELECT varac_outbox_dir FROM device_profiles WHERE TRIM(COALESCE(varac_outbox_dir, ''))<>''"
                                ).fetchall()
                            )
                            if normalized_shared_db in existing_node_paths:
                                raise ValueError(
                                    "The VarAC cluster shared database conflicts with an existing node-local path."
                                )
                        if existing_standalone_node_id is not None:
                            # Converting a reviewed standalone node is one
                            # transaction: update its native identity facts and
                            # re-scope its manifest before the second member is
                            # checked.  Otherwise the old standalone manifest
                            # falsely vetoes the common executable, install
                            # working directory, database, and BBS roots.
                            existing_updates = create_cluster_values.get(
                                "existing_standalone_application_values", {}
                            )
                            if isinstance(existing_updates, Mapping) and existing_updates:
                                existing_standalone = _save_varac_node_conn(
                                    conn,
                                    {
                                        **dict(existing_standalone or {}),
                                        **dict(existing_updates),
                                        "id": int(existing_standalone_node_id),
                                    },
                                )
                            existing_manifest_row = conn.execute(
                                """
                                SELECT * FROM software_instance_manifests
                                 WHERE family_key='varac' AND application_system_key=?
                                 LIMIT 1
                                """,
                                (str((existing_standalone or {}).get("system_key") or ""),),
                            ).fetchone()
                            if existing_manifest_row is not None:
                                existing_manifest = _software_instance_manifest_row(
                                    dict(existing_manifest_row)
                                )
                                existing_manifest_updates = create_cluster_values.get(
                                    "existing_standalone_manifest_values", {}
                                )
                                if isinstance(existing_manifest_updates, Mapping):
                                    existing_manifest.update(
                                        dict(existing_manifest_updates)
                                    )
                                existing_manifest = _scope_varac_cluster_manifest(
                                    existing_manifest,
                                    existing_standalone or {},
                                    cluster_id=public_cluster_id,
                                    instance_number=_coerce_int(
                                        create_cluster_values.get(
                                            "existing_standalone_instance_number", 1
                                        ),
                                        1,
                                    ),
                                    shared_db_path=shared_db_path,
                                    shared_bbs_path=shared_bbs_path,
                                    shared_bbs_archive_path=shared_bbs_archive_path,
                                )
                                saved_existing_manifest = _save_software_instance_manifest_conn(
                                    conn,
                                    existing_manifest,
                                )
                                existing_launch_row = conn.execute(
                                    """
                                    SELECT MAX(launch_at_startup)
                                      FROM radio_launch_bundle_items
                                     WHERE radio_profile_id=?
                                       AND (
                                            instance_key=?
                                            OR substr(instance_key, 1, length(?) + 1)=? || ':'
                                       )
                                    """,
                                    (
                                        int(existing_standalone_device_id or 0),
                                        str(saved_existing_manifest.get("instance_key") or ""),
                                        str(saved_existing_manifest.get("instance_key") or ""),
                                        str(saved_existing_manifest.get("instance_key") or ""),
                                    ),
                                ).fetchone()
                                existing_launch_at_startup = bool(
                                    int(existing_launch_row[0] or 0)
                                ) if existing_launch_row is not None else False
                                _remove_instance_launch_links_conn(
                                    conn,
                                    radio_profile_id=int(
                                        existing_standalone_device_id or 0
                                    ),
                                    family_key="varac",
                                    application_system_key=str(
                                        (existing_standalone or {}).get("system_key") or ""
                                    ),
                                )
                                self._upsert_instance_launch_items_conn(
                                    conn,
                                    radio_profile_id=int(
                                        existing_standalone_device_id or 0
                                    ),
                                    family_key="varac",
                                    saved_app=existing_standalone or {},
                                    manifest=saved_existing_manifest,
                                    launch_at_startup=existing_launch_at_startup,
                                )
                        requested_manifest = _scope_varac_cluster_manifest(
                            requested_manifest,
                            app_values,
                            cluster_id=public_cluster_id,
                            instance_number=varac_cluster_instance_number,
                            shared_db_path=shared_db_path,
                            shared_bbs_path=shared_bbs_path,
                            shared_bbs_archive_path=shared_bbs_archive_path,
                        )
                        now_iso = _utc_now_iso()
                        conn.execute(
                            """
                            INSERT INTO varac_clusters (
                                name, cluster_id, shared_db_path, shared_bbs_path,
                                shared_bbs_archive_path, counters_refresh_sec,
                                ptt_lock_enabled, gateway_handler_device_id,
                                email_gateway_sender_device_id, native_management_state,
                                native_writer_key, desired_fingerprint, observed_fingerprint,
                                resource_claims_json, native_verification_summary,
                                created_utc, updated_utc
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                cluster_name,
                                public_cluster_id,
                                shared_db_path or None,
                                shared_bbs_path or None,
                                shared_bbs_archive_path or None,
                                max(5, min(600, _coerce_int(create_cluster_values.get("counters_refresh_sec", 30), 30))),
                                _coerce_bool_int(create_cluster_values.get("ptt_lock_enabled", 0), False),
                                create_cluster_native_state,
                                _coerce_text(create_cluster_values.get("native_writer_key", ""), "") or None,
                                _coerce_text(create_cluster_values.get("desired_fingerprint", ""), "") or None,
                                _coerce_text(create_cluster_values.get("observed_fingerprint", ""), "") or None,
                                json.dumps(
                                    ([{
                                        "kind": "database",
                                        "path": shared_db_path,
                                        "owner_type": "varac_cluster",
                                        "owner_id": public_cluster_id,
                                        "exclusive": False,
                                    }] if shared_db_path else []),
                                    sort_keys=True,
                                ),
                                _coerce_text(create_cluster_values.get("native_verification_summary", ""), "") or None,
                                now_iso,
                                now_iso,
                            ),
                        )
                        cluster_db_id_value = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                        if existing_standalone_device_id is not None:
                            existing_member_number = _coerce_int(
                                create_cluster_values.get("existing_standalone_instance_number", 1),
                                1,
                            )
                            new_member_number = _coerce_int(varac_cluster_instance_number, 0)
                            if existing_member_number <= 0 or new_member_number <= 0:
                                raise ValueError("VarAC cluster instance numbers must be positive integers.")
                            if existing_member_number == new_member_number:
                                raise ValueError(
                                    f"VarAC cluster instance {new_member_number} is already assigned. Choose another instance number before saving."
                                )
                            try:
                                conn.execute(
                                    """
                                    INSERT INTO varac_cluster_members
                                        (cluster_id, device_profile_id, instance_number, enabled, created_utc, updated_utc)
                                    VALUES (?, ?, ?, 1, ?, ?)
                                    """,
                                    (
                                        int(cluster_db_id_value),
                                        int(existing_standalone_device_id),
                                        existing_member_number,
                                        now_iso,
                                        now_iso,
                                    ),
                                )
                            except sqlite3.IntegrityError as exc:
                                raise ValueError("Unable to retain the existing VarAC standalone node as a cluster member.") from exc
                            conn.execute(
                                """
                                UPDATE device_profiles
                                   SET varac_bbs_dir=?, varac_bbs_archive_dir=?, updated_utc=?
                                 WHERE id=?
                                """,
                                (
                                    shared_bbs_path or None,
                                    shared_bbs_archive_path or None,
                                    now_iso,
                                    int(existing_standalone_device_id),
                                ),
                            )
                current_link = _coerce_optional_int(profile.get(link_column))
                if current_link is not None and current_link != int(saved_app["id"]) and not replace_existing:
                    raise ValueError(
                        f"{profile.get('name') or 'This radio'} already has a {family.replace('_', ' ')} "
                        "instance. Confirm replacement before changing the assignment."
                    )
                _validate_software_instance_radio_ownership_conn(
                    conn,
                    radio_profile_id=radio_id,
                    family_key=family,
                    application_id=int(saved_app["id"]),
                )
                replacing = current_link is not None and current_link != int(saved_app["id"])
                saved_app = _activate_software_application_conn(
                    conn,
                    family_key=family,
                    application_id=int(saved_app["id"]),
                )

                if family == "varac" and cluster_db_id_value is not None and not create_cluster_values:
                    selected_cluster = _varac_cluster_by_id(conn, int(cluster_db_id_value)) or {}
                    requested_manifest = _scope_varac_cluster_manifest(
                        requested_manifest,
                        app_values,
                        cluster_id=str(selected_cluster.get("cluster_id") or ""),
                        instance_number=varac_cluster_instance_number,
                        shared_db_path=str(selected_cluster.get("shared_db_path") or ""),
                        shared_bbs_path=str(selected_cluster.get("shared_bbs_path") or ""),
                        shared_bbs_archive_path=str(
                            selected_cluster.get("shared_bbs_archive_path") or ""
                        ),
                    )
                manifest_payload = requested_manifest
                manifest_payload["family_key"] = family
                manifest_payload["application_system_key"] = str(saved_app.get("system_key", "") or "")
                if family == "js8call" and observer_profile:
                    evidence = manifest_payload.get("evidence", manifest_payload.get("evidence_json", {}))
                    if not isinstance(evidence, Mapping):
                        evidence = {}
                    manifest_payload["evidence"] = {
                        **dict(evidence),
                        "receive_only_ingest": True,
                        "transmit_authority": False,
                        "execution_scope": RECEIVE_ONLY_EXECUTION_SCOPE,
                    }
                elif family == "fast_light":
                    manifest_payload["evidence"] = requested_evidence
                manifest_payload.setdefault(
                    "instance_key",
                    f"{family}:{str(saved_app.get('system_key', '') or '')}",
                )
                if family == "varac" and cluster_db_id_value is not None:
                    # Membership is the authoritative owner of the cluster
                    # number.  Check it before generic manifest collision
                    # reporting so Add Radio names the operator-correctable
                    # choice rather than an implementation resource key.
                    preflight_instance_number = _coerce_int(
                        varac_cluster_instance_number, 0
                    )
                    if preflight_instance_number <= 0:
                        raise ValueError(
                            "VarAC cluster instance number must be a positive integer."
                        )
                    occupied = conn.execute(
                        """
                        SELECT device_profile_id FROM varac_cluster_members
                         WHERE cluster_id=? AND instance_number=?
                           AND device_profile_id<>? AND enabled=1
                         LIMIT 1
                        """,
                        (
                            int(cluster_db_id_value),
                            preflight_instance_number,
                            radio_id,
                        ),
                    ).fetchone()
                    if occupied is not None:
                        raise ValueError(
                            f"VarAC cluster instance {preflight_instance_number} is already assigned."
                        )
                saved_manifest = _save_software_instance_manifest_conn(conn, manifest_payload)
                if family == "varac" and cluster_db_id_value is not None:
                    cluster = _varac_cluster_by_id(conn, int(cluster_db_id_value))
                    if cluster is None:
                        raise KeyError(f"Unknown VarAC cluster id: {cluster_db_id_value}")
                    if observer_profile:
                        raise ValueError("Observer / SDR device profiles cannot participate in VarAC clusters.")
                    updates["varac_bbs_dir"] = str(cluster.get("shared_bbs_path") or "")
                    updates["varac_bbs_archive_dir"] = str(
                        cluster.get("shared_bbs_archive_path") or ""
                    )
                    if str(saved_app.get("native_management_state") or "operator").strip().lower() == "managed":
                        member_db = normalize_varac_path(
                            str(saved_app.get("db_path") or ""),
                            "VarAC member database path",
                        )
                        cluster_db = normalize_varac_path(
                            str(cluster.get("shared_db_path") or ""),
                            "VarAC shared database path",
                        )
                        if not cluster_db or member_db != cluster_db:
                            raise ValueError(
                                "A native-managed VarAC member must use the cluster's effective shared database."
                            )
                    instance_number = _coerce_int(varac_cluster_instance_number, 0)
                    if instance_number <= 0:
                        raise ValueError("VarAC cluster instance number must be a positive integer.")
                    other = _varac_enabled_membership_for_device(
                        conn,
                        radio_id,
                        exclude_cluster_id=int(cluster_db_id_value),
                    )
                    if other is not None and not replacing:
                        raise ValueError("This radio is already an enabled member of another VarAC cluster.")
                    occupied = conn.execute(
                        """
                        SELECT device_profile_id FROM varac_cluster_members
                         WHERE cluster_id=? AND instance_number=? AND device_profile_id<>? AND enabled=1
                         LIMIT 1
                        """,
                        (int(cluster_db_id_value), instance_number, radio_id),
                    ).fetchone()
                    if occupied is not None:
                        raise ValueError(f"VarAC cluster instance {instance_number} is already assigned.")

                if replacing:
                    old_linked_app = _record_by_id(
                        conn,
                        _SOFTWARE_INSTANCE_ASSIGNMENTS[family][1],
                        int(current_link),
                    ) or {}
                    _remove_instance_launch_links_conn(
                        conn,
                        radio_profile_id=radio_id,
                        family_key=family,
                        application_system_key=str(old_linked_app.get("system_key", "") or ""),
                    )
                    if family == "varac":
                        _remove_varac_cluster_links_for_device_conn(
                            conn,
                            radio_profile_id=radio_id,
                            preserve_cluster_id=(
                                int(cluster_db_id_value)
                                if cluster_db_id_value is not None
                                else None
                            ),
                        )

                updates["updated_utc"] = _utc_now_iso()
                conn.execute(
                    f"UPDATE device_profiles SET {', '.join(f'{key}=?' for key in updates)} WHERE id=?",
                    tuple(updates.values()) + (radio_id,),
                )
                if replacing:
                    _disable_unowned_software_application_conn(
                        conn,
                        family_key=family,
                        application_id=current_link,
                    )
                self._upsert_instance_launch_items_conn(
                    conn,
                    radio_profile_id=radio_id,
                    family_key=family,
                    saved_app=saved_app,
                    manifest=saved_manifest,
                    launch_at_startup=bool(launch_at_startup),
                )
                if family == "varac" and cluster_db_id_value is not None:
                    now_iso = _utc_now_iso()
                    conn.execute(
                        """
                        INSERT INTO varac_cluster_members
                            (cluster_id, device_profile_id, instance_number, enabled, created_utc, updated_utc)
                        VALUES (?, ?, ?, 1, ?, ?)
                        ON CONFLICT(cluster_id, device_profile_id) DO UPDATE SET
                            instance_number=excluded.instance_number,
                            enabled=1,
                            updated_utc=excluded.updated_utc
                        """,
                        (int(cluster_db_id_value), radio_id, instance_number, now_iso, now_iso),
                    )
                    if gateway_for_new_cluster:
                        conn.execute(
                            "UPDATE varac_clusters SET gateway_handler_device_id=?, updated_utc=? WHERE id=?",
                            (radio_id, now_iso, int(cluster_db_id_value)),
                        )
                    elif gateway_existing_standalone:
                        conn.execute(
                            "UPDATE varac_clusters SET gateway_handler_device_id=?, updated_utc=? WHERE id=?",
                            (int(existing_standalone_device_id), now_iso, int(cluster_db_id_value)),
                        )
                    if email_gateway_sender_choice == "new_member":
                        conn.execute(
                            "UPDATE varac_clusters SET email_gateway_sender_device_id=?, updated_utc=? WHERE id=?",
                            (radio_id, now_iso, int(cluster_db_id_value)),
                        )
                    elif email_gateway_sender_choice == "existing_member":
                        if existing_standalone_device_id is None:
                            raise ValueError(
                                "The existing email gateway sender is unavailable for this cluster."
                            )
                        conn.execute(
                            "UPDATE varac_clusters SET email_gateway_sender_device_id=?, updated_utc=? WHERE id=?",
                            (int(existing_standalone_device_id), now_iso, int(cluster_db_id_value)),
                        )
                    _sync_varac_cluster_member_enabled_flags_conn(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            resolved = _resolve_device_profile_links_conn(
                conn,
                _record_by_id(conn, "device_profiles", radio_id) or profile,
            )
            return {
                "application": dict(saved_app),
                "manifest": dict(saved_manifest),
                "radio": dict(resolved),
            }

    def adopt_observer_js8_instance(
        self,
        *,
        radio_profile_id: int,
        application_values: Mapping[str, Any],
        manifest_values: Mapping[str, Any],
        replace_existing: bool = False,
        expected_current_instance_id: Any = _SOFTWARE_INSTANCE_EXPECTATION_UNSET,
        launch_at_startup: bool = False,
    ) -> Dict[str, Any]:
        """Assign an isolated JS8Call instance to an SDR for receive ingest.

        This is an ownership and launch association only. It deliberately does
        not change the observer's control backend and grants no Compose, Expect,
        QSY, PTT, scheduler, or compatibility-primary authority.
        """

        radio_id = int(radio_profile_id or 0)
        with self._connect_readonly() as conn:
            profile = _record_by_id(conn, "device_profiles", radio_id)
        if profile is None:
            raise KeyError(f"Unknown radio profile id: {radio_id}")
        if not _is_observer_device_class(profile):
            raise ValueError("Receive-only JS8Call instances can only be assigned to observer / SDR profiles.")
        return self.adopt_software_instance(
            family_key="js8call",
            radio_profile_id=radio_id,
            application_values=application_values,
            manifest_values=manifest_values,
            replace_existing=replace_existing,
            expected_current_instance_id=expected_current_instance_id,
            launch_at_startup=launch_at_startup,
            require_observer_receive_only=True,
        )

    def adopt_observer_fast_light_instance(
        self,
        *,
        radio_profile_id: int,
        application_values: Mapping[str, Any],
        manifest_values: Mapping[str, Any],
        replace_existing: bool = False,
        expected_current_instance_id: Any = _SOFTWARE_INSTANCE_EXPECTATION_UNSET,
        launch_at_startup: bool = False,
    ) -> Dict[str, Any]:
        """Assign a reviewed FLDigi-led Fast Light bundle to an observer.

        The stored bundle may include receive/log/file helpers, but never FLRig,
        CAT, PTT, automatic send, or transmit authority.
        """

        radio_id = int(radio_profile_id or 0)
        with self._connect_readonly() as conn:
            profile = _record_by_id(conn, "device_profiles", radio_id)
        if profile is None:
            raise KeyError(f"Unknown radio profile id: {radio_id}")
        if not _is_observer_device_class(profile):
            raise ValueError("Receive-only Fast Light instances can only be assigned to observer / SDR profiles.")
        app_values = dict(application_values or {})
        if str(app_values.get("flrig_path", "") or "").strip():
            raise ValueError("Observer / SDR Fast Light workflows cannot include FLRig, CAT, or PTT control.")
        app_values["flrig_path"] = ""
        # The legacy Fast Light table retains a non-null FLRig port column.
        # Keep an inert compatibility value while the observer radio has no
        # FLRig launch item, capability flag, CAT/PTT authority, or dependency.
        app_values["flrig_port"] = int(app_values.get("flrig_port") or 12345)
        manifest_payload = dict(manifest_values or {})
        evidence = manifest_payload.get("evidence", manifest_payload.get("evidence_json", {}))
        if not isinstance(evidence, Mapping):
            evidence = {}
        manifest_payload["evidence"] = {
            **dict(evidence),
            "receive_only_ingest": True,
            "transmit_authority": False,
            "execution_scope": RECEIVE_ONLY_EXECUTION_SCOPE,
        }
        return self.adopt_software_instance(
            family_key="fast_light",
            radio_profile_id=radio_id,
            application_values=app_values,
            manifest_values=manifest_payload,
            replace_existing=replace_existing,
            expected_current_instance_id=expected_current_instance_id,
            launch_at_startup=launch_at_startup,
            require_observer_receive_only=True,
        )

    def disassociate_software_instance(
        self,
        *,
        family_key: str,
        radio_profile_id: int,
        expected_current_instance_id: Any = _SOFTWARE_INSTANCE_EXPECTATION_UNSET,
    ) -> Dict[str, Any]:
        """Remove FIO's radio association without deleting application data.

        The saved application row, manifest, native application files, and
        compatibility path values remain intact.  Only the radio-to-runtime
        assignment, FIO-created startup items, and VarAC cluster membership are
        removed.
        """

        family = str(family_key or "").strip().lower()
        try:
            link_column, application_table, _label = _SOFTWARE_INSTANCE_ASSIGNMENTS[family]
        except KeyError as exc:
            raise ValueError(f"Unsupported software instance family: {family or 'blank'}") from exc
        radio_id = int(radio_profile_id or 0)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                profile = _record_by_id(conn, "device_profiles", radio_id)
                if profile is None:
                    raise KeyError(f"Unknown radio profile id: {radio_id}")
                application_id = _coerce_optional_int(profile.get(link_column))
                expected_current = (
                    _coerce_optional_int(expected_current_instance_id)
                    if expected_current_instance_id is not _SOFTWARE_INSTANCE_EXPECTATION_UNSET
                    else _SOFTWARE_INSTANCE_EXPECTATION_UNSET
                )
                if (
                    expected_current is not _SOFTWARE_INSTANCE_EXPECTATION_UNSET
                    and application_id != expected_current
                ):
                    raise ValueError("The radio's software assignment changed. Refresh and review it before removing it.")
                application = (
                    _record_by_id(conn, application_table, int(application_id))
                    if application_id is not None
                    else None
                )
                if application is not None:
                    _remove_instance_launch_links_conn(
                        conn,
                        radio_profile_id=radio_id,
                        family_key=family,
                        application_system_key=str(application.get("system_key", "") or ""),
                    )
                if family == "varac":
                    _remove_varac_cluster_links_for_device_conn(
                        conn,
                        radio_profile_id=radio_id,
                    )
                assignments = [f"{link_column}=NULL"] + [
                    f"{flag}=0" for flag in _SOFTWARE_INSTANCE_USE_FLAGS[family]
                ]
                family_control_backend = {"js8call": "js8call", "fast_light": "flrig"}.get(family)
                if (
                    family_control_backend
                    and _coerce_text(profile.get("control_backend", ""), "").lower()
                    == family_control_backend
                ):
                    assignments.append("control_backend='manual'")
                conn.execute(
                    f"UPDATE device_profiles SET {', '.join(assignments)}, updated_utc=? WHERE id=?",
                    (_utc_now_iso(), radio_id),
                )
                _disable_unowned_software_application_conn(
                    conn,
                    family_key=family,
                    application_id=application_id,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            radio = _resolve_device_profile_links_conn(
                conn,
                _record_by_id(conn, "device_profiles", radio_id) or profile,
            )
            return {
                "application": dict(application or {}),
                "radio": dict(radio),
            }

    @staticmethod
    def _upsert_instance_launch_items_conn(
        conn: sqlite3.Connection,
        *,
        radio_profile_id: int,
        family_key: str,
        saved_app: Mapping[str, Any],
        manifest: Mapping[str, Any],
        launch_at_startup: bool = False,
    ) -> None:
        """Create launch identities; enabling is explicit and never disables peers."""

        now_iso = _utc_now_iso()
        profile = _record_by_id(conn, "device_profiles", int(radio_profile_id)) or {}
        observer_profile = _is_observer_device_class(profile)
        observer_js8 = family_key == "js8call" and observer_profile
        observer_fast_light = family_key == "fast_light" and observer_profile
        raw_evidence = manifest.get("evidence", {})
        if not isinstance(raw_evidence, Mapping):
            raw_evidence = {}
        raw_recipe = raw_evidence.get("launch_recipe", {})
        if not isinstance(raw_recipe, Mapping):
            raw_recipe = {}
        recipe_status = str(raw_recipe.get("status", "") or "").strip()
        launch_pending = recipe_status == "launch_pending"
        conn.execute(
            """
            INSERT INTO radio_launch_bundles
                (radio_profile_id, schema_version, launch_enabled, migrated_from_legacy, updated_utc)
            VALUES (?, 1, ?, 0, ?)
            ON CONFLICT(radio_profile_id) DO UPDATE SET
                launch_enabled=CASE
                    WHEN ?=1 THEN 0
                    WHEN excluded.launch_enabled=1 THEN 1
                    ELSE radio_launch_bundles.launch_enabled
                END,
                updated_utc=excluded.updated_utc
            """,
            (
                int(radio_profile_id),
                1 if launch_at_startup and not launch_pending else 0,
                now_iso,
                1 if launch_pending else 0,
            ),
        )
        manifest_key = str(manifest.get("instance_key", "") or "")
        command = str(manifest.get("launch_command", "") or "")
        resources = {
            str(item.get("kind", "") or ""): str(item.get("value", "") or "")
            for item in manifest.get("resource_claims", ()) or ()
            if isinstance(item, Mapping)
        }
        raw_components = raw_recipe.get("components", ())
        # Warning-ready and launch-pending recipes are complete isolated
        # bundles too.  Persist their component facts so Launch Control and
        # first-launch reconciliation retain the reviewed roots/arguments;
        # the pending status itself keeps launch disabled until recovery.
        qualified_components = (
            tuple(item for item in raw_components if isinstance(item, Mapping))
            if recipe_status
            in {"qualified_managed", "ready_with_warnings", "launch_pending"}
            else ()
        )
        if qualified_components and family_key == "varac":
            # VarAC native preparation already produces the exact argv that
            # must be launched (including Wine-visible Windows paths).  Keep
            # that structured recipe in the existing readiness JSON seam;
            # command_override remains empty so older launch adapters cannot
            # accidentally reparse the display command or fall back to the
            # installation directory as the executable.
            recipe_rows = []
            for component in qualified_components:
                component_key = str(component.get("component_key", "") or "").strip().lower()
                if component_key not in {"varac", "vara"}:
                    raise ValueError(f"Unsupported managed VarAC launch component: {component_key or 'blank'}")
                executable = str(
                    component.get("executable", component.get("launch_executable", "")) or ""
                ).strip()
                arguments = [str(value) for value in component.get("arguments", ()) or ()]
                environment = component.get("environment", {})
                if not isinstance(environment, Mapping):
                    environment = {}
                readiness = dict(component.get("readiness") or {})
                readiness.update(
                    {
                        "structured_launch": True,
                        "executable": executable,
                        "launch_arguments": arguments,
                        "environment": {
                            str(key): str(value)
                            for key, value in environment.items()
                            if str(key).strip()
                        },
                        "effective_command": [
                            str(value) for value in component.get("effective_command", ()) or ()
                        ],
                        "effective_command_text": str(component.get("effective_command_text", "") or ""),
                        "working_directory": str(component.get("working_directory", "") or "").strip(),
                        "profile_selector": str(component.get("profile_selector", "") or "").strip(),
                        "configuration_roots": [
                            str(value) for value in component.get("configuration_roots", ()) or ()
                        ],
                        "data_roots": [
                            str(value) for value in component.get("data_roots", ()) or ()
                        ],
                        "managed_directories": [
                            str(value) for value in component.get("managed_directories", ()) or ()
                        ],
                        "endpoints": [
                            dict(value) for value in component.get("endpoints", ()) or ()
                            if isinstance(value, Mapping)
                        ],
                        "evidence": dict(component.get("evidence") or {}),
                        "confidence": str(component.get("confidence", "") or ""),
                        "execution_scope": str(component.get("execution_scope", "standard") or "standard").strip().lower(),
                        "operator_starts": bool(component.get("operator_starts", False)),
                    }
                )
                if "launch_at_startup" in component:
                    readiness["component_launch_at_startup"] = bool(
                        component.get("launch_at_startup", False)
                    )
                instance_key = f"{manifest_key}:{component_key}"
                recipe_rows.append(
                    (
                        instance_key,
                        "VARA" if component_key == "vara" else "VarAC",
                        35 if component_key == "vara" else 40,
                        "",
                        executable,
                        [str(value) for value in component.get("dependencies", ()) or ()],
                        readiness,
                    )
                )
            rows = tuple(recipe_rows)
        elif qualified_components and family_key in {"js8call", "fast_light"}:
            order_by_component = {"flrig": 10, "fldigi": 20, "flmsg": 30, "flamp": 31, "js8call": 50}
            name_by_component = {
                "flrig": "FLRig",
                "fldigi": "FLDigi",
                "flmsg": "FLMsg",
                "flamp": "FLAmp",
                "js8call": "JS8Call",
            }
            recipe_rows = []
            for component in qualified_components:
                component_key = str(component.get("component_key", "") or "").strip().lower()
                if component_key not in name_by_component:
                    raise ValueError(f"Unsupported managed launch component: {component_key or 'blank'}")
                scope = str(component.get("execution_scope", "standard") or "standard").strip().lower()
                readiness = dict(component.get("readiness") or {})
                readiness.update(
                    {
                        "launch_arguments": [
                            str(value) for value in component.get("arguments", ()) or ()
                        ],
                        "environment": {
                            str(key): str(value)
                            for key, value in (
                                component.get("environment", {})
                                if isinstance(component.get("environment", {}), Mapping)
                                else {}
                            ).items()
                            if str(key).strip()
                        },
                        "effective_command": [
                            str(value) for value in component.get("effective_command", ()) or ()
                        ],
                        "effective_command_text": str(component.get("effective_command_text", "") or ""),
                        "working_directory": str(component.get("working_directory", "") or "").strip(),
                        "profile_selector": str(component.get("profile_selector", "") or "").strip(),
                        "configuration_roots": [
                            str(value) for value in component.get("configuration_roots", ()) or ()
                        ],
                        "data_roots": [
                            str(value) for value in component.get("data_roots", ()) or ()
                        ],
                        "managed_directories": [
                            str(value) for value in component.get("managed_directories", ()) or ()
                        ],
                        "endpoints": [
                            dict(value) for value in component.get("endpoints", ()) or ()
                            if isinstance(value, Mapping)
                        ],
                        "evidence": dict(component.get("evidence") or {}),
                        "confidence": str(component.get("confidence", "") or ""),
                        "execution_scope": scope,
                        "operator_starts": bool(component.get("operator_starts", False)),
                    }
                )
                if "launch_at_startup" in component:
                    readiness["component_launch_at_startup"] = bool(
                        component.get("launch_at_startup", False)
                    )
                if scope == RECEIVE_ONLY_EXECUTION_SCOPE:
                    readiness.update(
                        {
                            "receive_only_ingest": True,
                            "transmit_authority": False,
                        }
                    )
                instance_key = (
                    f"fast-light:station-shared:{component_key}"
                    if scope == "station_shared_utility"
                    else f"{manifest_key}:{component_key}"
                )
                recipe_rows.append(
                    (
                        instance_key,
                        name_by_component[component_key],
                        order_by_component[component_key],
                        "",
                        str(component.get("executable", "") or "").strip(),
                        [str(value) for value in component.get("dependencies", ()) or ()],
                        readiness,
                    )
                )
            rows = tuple(recipe_rows)
        elif qualified_components and family_key == 'js8call':
            if len(qualified_components) != 1 or qualified_components[0].get('component_key') != 'js8call':
                raise ValueError('JS8Call needs one exact reviewed launch component.')
            component = qualified_components[0]
            readiness = dict(component.get('readiness') or {})
            readiness.update(host=str(saved_app.get('host') or ''),port=int(saved_app.get('port') or 0),
                             require_api=True,launch_arguments=list(component.get('arguments') or ()),
                             working_directory=str(component.get('working_directory') or ''),
                             environment=dict(component.get('environment') or {}),
                             profile_selector=str(component.get('profile_selector') or ''),
                             operator_starts=bool(component.get('operator_starts')),
                             component_launch_at_startup=bool(component.get('launch_at_startup')))
            rows=((f'{manifest_key}:js8call','JS8Call',50,'',str(component.get('executable') or ''),[],readiness),)
        elif family_key == "js8call":
            js8_readiness = {
                "host": str(saved_app.get("host", "127.0.0.1") or "127.0.0.1"),
                "port": int(saved_app.get("port", 2442) or 2442),
                "require_api": True,
            }
            if observer_js8:
                js8_readiness.update(
                    {
                        "execution_scope": RECEIVE_ONLY_EXECUTION_SCOPE,
                        "receive_only_ingest": True,
                        "transmit_authority": False,
                    }
                )
            rows = (
                (
                    f"{manifest_key}:js8call",
                    "JS8Call",
                    50,
                    command,
                    str(saved_app.get("install_path", "") or ""),
                    [],
                    js8_readiness,
                ),
            )
        elif family_key == "fast_light":
            host = str(saved_app.get("flrig_host", "127.0.0.1") or "127.0.0.1")
            flrig_arguments = (
                ["--config-dir", resources["flrig_configuration"]]
                if resources.get("flrig_configuration")
                else []
            )
            fldigi_arguments: List[str] = []
            if not command:
                if resources.get("fldigi_configuration"):
                    fldigi_arguments.extend(["--config-dir", resources["fldigi_configuration"]])
                fldigi_arguments.extend(
                    [
                        "--xmlrpc-server-address",
                        str(saved_app.get("fldigi_host", host) or host),
                        "--xmlrpc-server-port",
                        str(int(saved_app.get("fldigi_port", 7362) or 7362)),
                    ]
                )
            fldigi_readiness = {
                "host": str(saved_app.get("fldigi_host", host) or host),
                "port": int(saved_app.get("fldigi_port", 7362) or 7362),
                "require_service": True,
                "launch_arguments": fldigi_arguments,
            }
            if observer_fast_light:
                fldigi_readiness.update(
                    {
                        "execution_scope": RECEIVE_ONLY_EXECUTION_SCOPE,
                        "receive_only_ingest": True,
                        "transmit_authority": False,
                    }
                )
                rows = (
                    (
                        f"{manifest_key}:fldigi",
                        "FLDigi",
                        20,
                        command,
                        str(saved_app.get("fldigi_path", "") or ""),
                        [],
                        fldigi_readiness,
                    ),
                )
            else:
                rows = (
                    (
                        f"{manifest_key}:flrig",
                        "FLRig",
                        10,
                        "",
                        str(saved_app.get("flrig_path", "") or ""),
                        [],
                        {
                            "host": host,
                            "port": int(saved_app.get("flrig_port", 12345) or 12345),
                            "require_service": True,
                            "launch_arguments": flrig_arguments,
                        },
                    ),
                    (
                        f"{manifest_key}:fldigi",
                        "FLDigi",
                        20,
                        command,
                        str(saved_app.get("fldigi_path", "") or ""),
                        ["FLRig"],
                        fldigi_readiness,
                    ),
                )
        else:
            rows = (
                (
                    f"{manifest_key}:varac",
                    "VarAC",
                    40,
                    command or str(saved_app.get("launch_cmd", "") or ""),
                    str(saved_app.get("install_path", "") or ""),
                    [],
                    {"working_directory": resources.get("working_directory", "")},
                ),
            )
        for instance_key, app_name, order, command_override, path_override, dependencies, readiness in rows:
            component_autostart = bool(
                readiness.get("component_launch_at_startup", launch_at_startup)
            )
            operator_starts = bool(readiness.get("operator_starts", False))
            conn.execute(
                """
                INSERT INTO radio_launch_bundle_items (
                    radio_profile_id, instance_key, app_name, display_order, enabled,
                    launch_at_startup, monitor_health, command_override, path_override,
                    dependencies_json, readiness_json, updated_utc
                ) VALUES (?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(radio_profile_id, instance_key) DO UPDATE SET
                    app_name=excluded.app_name,
                    display_order=excluded.display_order,
                    launch_at_startup=excluded.launch_at_startup,
                    command_override=excluded.command_override,
                    path_override=excluded.path_override,
                    dependencies_json=excluded.dependencies_json,
                    readiness_json=excluded.readiness_json,
                    updated_utc=excluded.updated_utc
                """,
                (
                    int(radio_profile_id),
                    instance_key,
                    app_name,
                    int(order),
                    1 if launch_at_startup and component_autostart and not operator_starts else 0,
                    command_override,
                    path_override,
                    json.dumps(dependencies, sort_keys=True),
                    json.dumps(readiness, sort_keys=True),
                    now_iso,
                ),
            )
        if family_key == "js8call":
            spotter_path = str(saved_app.get("spotter_launch_path", "") or "").strip()
            if spotter_path:
                conn.execute(
                    """
                    INSERT INTO radio_launch_bundle_items (
                        radio_profile_id, instance_key, app_name, display_order, enabled,
                        launch_at_startup, monitor_health, command_override, path_override,
                        dependencies_json, readiness_json, updated_utc
                    ) VALUES (?, ?, 'JS8Spotter', 55, 1, ?, 1, '', ?, ?, ?, ?)
                    ON CONFLICT(radio_profile_id, instance_key) DO UPDATE SET
                        app_name=excluded.app_name,
                        display_order=excluded.display_order,
                        enabled=1,
                        launch_at_startup=excluded.launch_at_startup,
                        monitor_health=1,
                        command_override='',
                        path_override=excluded.path_override,
                        dependencies_json=excluded.dependencies_json,
                        readiness_json=excluded.readiness_json,
                        updated_utc=excluded.updated_utc
                    """,
                    (
                        int(radio_profile_id),
                        f"{manifest_key}:external-js8spotter",
                        1
                        if bool(raw_evidence.get("external_spotter_launch_at_startup", False))
                        else 0,
                        spotter_path,
                        json.dumps(["JS8Call"]),
                        json.dumps(
                            {
                                "kind": "operator_confirmed",
                                "execution_scope": "standard",
                                "operator_starts": False,
                                "host": str(saved_app.get("host", "127.0.0.1") or "127.0.0.1"),
                                "port": int(saved_app.get("port", 2442) or 2442),
                            },
                            sort_keys=True,
                        ),
                        now_iso,
                    ),
                )
            _sync_station_shared_commstat_binding_conn(
                conn,
                radio_profile_id=int(radio_profile_id),
            )

    def get_js8_instance(self, js8_instance_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            return _record_by_id(conn, "js8_instances", int(js8_instance_id))

    def save_js8_instance(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            return _save_js8_instance_conn(conn, values)

    def update_radio_js8_offset(
        self,
        radio_profile_id: int,
        offset_hz: int,
        *,
        expected_instance_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically update only the assigned JS8Call default offset.

        This intentionally does not contact JS8Call or any radio. Runtime
        enforcement observes the committed per-radio value later and remains
        subject to the scheduler's TX/busy interlocks.
        """

        radio_id = int(radio_profile_id or 0)
        offset = int(offset_hz or 0)
        if not 1800 <= offset <= 2700:
            raise ValueError("JS8Call offset must be from 1800 through 2700 Hz.")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                profile = _record_by_id(conn, "device_profiles", radio_id)
                if profile is None:
                    raise KeyError(f"Unknown radio profile id: {radio_id}")
                instance_id = _coerce_optional_int(profile.get("js8_instance_id"))
                if instance_id is None:
                    raise ValueError("This radio does not have an assigned JS8Call instance.")
                if (
                    expected_instance_id is not None
                    and int(expected_instance_id) != int(instance_id)
                ):
                    raise ValueError(
                        "The radio's JS8Call assignment changed. Refresh before changing its offset."
                    )
                instance = _record_by_id(conn, "js8_instances", int(instance_id))
                if instance is None:
                    raise KeyError(f"Unknown JS8Call instance id: {instance_id}")
                now_iso = _utc_now_iso()
                conn.execute(
                    "UPDATE js8_instances SET offset_hz=?, updated_utc=? WHERE id=?",
                    (offset, now_iso, int(instance_id)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        saved = self.get_device_profile(radio_id)
        if not isinstance(saved, dict):
            raise RuntimeError("Updated radio profile could not be read back.")
        return saved

    def delete_js8_instance(self, js8_instance_id: int) -> None:
        with self._connect() as conn:
            row = _record_by_id(conn, "js8_instances", int(js8_instance_id))
            if not row:
                raise KeyError(f"Unknown JS8 instance id: {js8_instance_id}")
            count = conn.execute(
                "SELECT COUNT(*) FROM device_profiles WHERE js8_instance_id=?",
                (int(js8_instance_id),),
            ).fetchone()[0]
            if int(count or 0) > 0:
                raise ValueError("Cannot delete a JS8 instance that is still assigned.")
            conn.execute(
                "DELETE FROM software_instance_manifests WHERE family_key='js8call' AND application_system_key=?",
                (str(row.get("system_key", "") or ""),),
            )
            conn.execute("DELETE FROM js8_instances WHERE id=?", (int(js8_instance_id),))
            conn.commit()

    def list_fast_light_configs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM fast_light_configs ORDER BY id ASC").fetchall()
            return [dict(row) for row in rows]

    def get_fast_light_config(self, fast_light_config_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            return _record_by_id(conn, "fast_light_configs", int(fast_light_config_id))

    def save_fast_light_config(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            return _save_fast_light_config_conn(conn, values)

    def delete_fast_light_config(self, fast_light_config_id: int) -> None:
        with self._connect() as conn:
            row = _record_by_id(conn, "fast_light_configs", int(fast_light_config_id))
            if not row:
                raise KeyError(f"Unknown Fast Light config id: {fast_light_config_id}")
            count = conn.execute(
                "SELECT COUNT(*) FROM device_profiles WHERE fast_light_config_id=?",
                (int(fast_light_config_id),),
            ).fetchone()[0]
            if int(count or 0) > 0:
                raise ValueError("Cannot delete a Fast Light config that is still assigned.")
            conn.execute(
                "DELETE FROM software_instance_manifests WHERE family_key='fast_light' AND application_system_key=?",
                (str(row.get("system_key", "") or ""),),
            )
            conn.execute("DELETE FROM fast_light_configs WHERE id=?", (int(fast_light_config_id),))
            conn.commit()

    def list_varac_nodes(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM varac_nodes ORDER BY id ASC").fetchall()
            return [dict(row) for row in rows]

    def get_varac_node(self, varac_node_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            return _record_by_id(conn, "varac_nodes", int(varac_node_id))

    def save_varac_node(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            return _save_varac_node_conn(conn, values)

    def delete_varac_node(self, varac_node_id: int) -> None:
        with self._connect() as conn:
            row = _record_by_id(conn, "varac_nodes", int(varac_node_id))
            if not row:
                raise KeyError(f"Unknown VarAC node id: {varac_node_id}")
            count = conn.execute(
                "SELECT COUNT(*) FROM device_profiles WHERE varac_node_id=?",
                (int(varac_node_id),),
            ).fetchone()[0]
            if int(count or 0) > 0:
                raise ValueError("Cannot delete a VarAC node that is still assigned.")
            conn.execute(
                "DELETE FROM software_instance_manifests WHERE family_key='varac' AND application_system_key=?",
                (str(row.get("system_key", "") or ""),),
            )
            conn.execute("DELETE FROM varac_nodes WHERE id=?", (int(varac_node_id),))
            conn.commit()

    def list_varac_clusters(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            _sync_derived_coordination_policies_conn(conn)
            return _list_varac_clusters_conn(conn)

    def list_varac_cluster_members(
        self,
        *,
        cluster_id: Optional[int] = None,
        device_profile_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            _sync_derived_coordination_policies_conn(conn)
            return _list_varac_cluster_members_conn(
                conn,
                cluster_id=int(cluster_id) if cluster_id is not None else None,
                device_profile_id=int(device_profile_id) if device_profile_id is not None else None,
            )

    def save_varac_cluster(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(values or {})
        requested_id = _coerce_optional_int(payload.get("id"))
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            existing = _varac_cluster_by_id(conn, int(requested_id)) if requested_id is not None else None
            now_iso = _utc_now_iso()
            name_default = _coerce_text((existing or {}).get("name", ""), "") or "VarAC Cluster"
            cluster_id_default = _coerce_text((existing or {}).get("cluster_id", ""), "") or _normalize_varac_cluster_id(name_default)
            name = _coerce_text(payload.get("name", name_default), name_default) or name_default
            cluster_key = _normalize_varac_cluster_id(payload.get("cluster_id", cluster_id_default), _normalize_varac_cluster_id(name))
            shared_db_path = _coerce_text(payload.get("shared_db_path", (existing or {}).get("shared_db_path", "")), "")
            shared_bbs_path = _coerce_text(
                payload.get("shared_bbs_path", (existing or {}).get("shared_bbs_path", "")), ""
            )
            shared_bbs_archive_path = _coerce_text(
                payload.get(
                    "shared_bbs_archive_path",
                    (existing or {}).get("shared_bbs_archive_path", ""),
                ),
                "",
            )
            counters_refresh_sec = max(
                5,
                min(
                    600,
                    _coerce_int(payload.get("counters_refresh_sec", (existing or {}).get("counters_refresh_sec", 30)), 30),
                ),
            )
            ptt_lock_enabled = _coerce_bool_int(
                payload.get("ptt_lock_enabled", (existing or {}).get("ptt_lock_enabled", 0)),
                False,
            )
            duplicate_cluster = conn.execute(
                """
                SELECT id
                  FROM varac_clusters
                 WHERE UPPER(cluster_id)=?
                   AND (? IS NULL OR id<>?)
                 LIMIT 1
                """,
                (cluster_key, requested_id, requested_id),
            ).fetchone()
            if duplicate_cluster is not None:
                raise ValueError(f"VarAC cluster ID {cluster_key} is already in use.")
            gateway_handler_device_id = _coerce_optional_int(
                payload.get("gateway_handler_device_id", (existing or {}).get("gateway_handler_device_id"))
            )
            email_gateway_sender_device_id = _coerce_optional_int(
                payload.get(
                    "email_gateway_sender_device_id",
                    (existing or {}).get("email_gateway_sender_device_id"),
                )
            )
            native_management_state = _coerce_text(
                payload.get(
                    "native_management_state",
                    (existing or {}).get("native_management_state", "operator"),
                ),
                "operator",
            ).lower()
            if native_management_state not in SUPPORTED_VARAC_NATIVE_MANAGEMENT_STATES:
                raise ValueError("Unsupported VarAC native management state.")
            native_writer_key = _coerce_text(
                payload.get("native_writer_key", (existing or {}).get("native_writer_key", "")), ""
            )
            desired_fingerprint = _coerce_text(
                payload.get("desired_fingerprint", (existing or {}).get("desired_fingerprint", "")), ""
            )
            observed_fingerprint = _coerce_text(
                payload.get("observed_fingerprint", (existing or {}).get("observed_fingerprint", "")), ""
            )
            resource_claims_json = _coerce_json_list_text(
                payload.get(
                    "resource_claims_json",
                    (existing or {}).get("resource_claims_json", "[]"),
                )
            )
            last_native_verified_utc = _coerce_text(
                payload.get("last_native_verified_utc", (existing or {}).get("last_native_verified_utc", "")), ""
            )
            native_verification_summary = _coerce_text(
                payload.get(
                    "native_verification_summary",
                    (existing or {}).get("native_verification_summary", ""),
                ),
                "",
            )
            if requested_id is None and gateway_handler_device_id is not None:
                raise ValueError("Assign cluster members before selecting a VarAC gateway handler.")
            if requested_id is None and email_gateway_sender_device_id is not None:
                raise ValueError("Assign cluster members before selecting a VarAC email gateway sender.")

            row_id = int(requested_id) if requested_id is not None else 0
            if gateway_handler_device_id is not None and row_id > 0:
                membership = _varac_cluster_membership_row(conn, row_id, int(gateway_handler_device_id))
                if membership is None or int(membership.get("enabled", 1) or 0) != 1:
                    raise ValueError("The VarAC gateway handler must be an enabled member of this cluster.")
            if email_gateway_sender_device_id is not None and row_id > 0:
                membership = _varac_cluster_membership_row(
                    conn, row_id, int(email_gateway_sender_device_id)
                )
                if membership is None or int(membership.get("enabled", 1) or 0) != 1:
                    raise ValueError(
                        "The VarAC email gateway sender must be an enabled member of this cluster."
                    )

            if existing:
                conn.execute(
                    """
                    UPDATE varac_clusters
                       SET name=?, cluster_id=?, shared_db_path=?, shared_bbs_path=?,
                           shared_bbs_archive_path=?, counters_refresh_sec=?,
                           ptt_lock_enabled=?, gateway_handler_device_id=?,
                           email_gateway_sender_device_id=?, native_management_state=?,
                           native_writer_key=?, desired_fingerprint=?, observed_fingerprint=?,
                           resource_claims_json=?, last_native_verified_utc=?,
                           native_verification_summary=?, updated_utc=?
                     WHERE id=?
                    """,
                    (
                        name,
                        cluster_key,
                        shared_db_path or None,
                        shared_bbs_path or None,
                        shared_bbs_archive_path or None,
                        counters_refresh_sec,
                        ptt_lock_enabled,
                        gateway_handler_device_id,
                        email_gateway_sender_device_id,
                        native_management_state,
                        native_writer_key or None,
                        desired_fingerprint or None,
                        observed_fingerprint or None,
                        resource_claims_json,
                        last_native_verified_utc or None,
                        native_verification_summary or None,
                        now_iso,
                        int(requested_id),
                    ),
                )
                row_id = int(requested_id)
            else:
                conn.execute(
                    """
                    INSERT INTO varac_clusters (
                        name, cluster_id, shared_db_path, shared_bbs_path,
                        shared_bbs_archive_path, counters_refresh_sec,
                        ptt_lock_enabled, gateway_handler_device_id,
                        email_gateway_sender_device_id, native_management_state,
                        native_writer_key, desired_fingerprint, observed_fingerprint,
                        resource_claims_json, last_native_verified_utc, native_verification_summary,
                        created_utc, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        cluster_key,
                        shared_db_path or None,
                        shared_bbs_path or None,
                        shared_bbs_archive_path or None,
                        counters_refresh_sec,
                        ptt_lock_enabled,
                        None,
                        None,
                        native_management_state,
                        native_writer_key or None,
                        desired_fingerprint or None,
                        observed_fingerprint or None,
                        resource_claims_json,
                        last_native_verified_utc or None,
                        native_verification_summary or None,
                        now_iso,
                        now_iso,
                    ),
                )
                row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            # Cluster BBS storage is one shared resource.  Keep every current
            # member profile's compatibility projection synchronized with the
            # canonical cluster row in the same transaction.
            conn.execute(
                """
                UPDATE device_profiles
                   SET varac_bbs_dir=?, varac_bbs_archive_dir=?, updated_utc=?
                 WHERE id IN (
                    SELECT device_profile_id
                      FROM varac_cluster_members
                     WHERE cluster_id=? AND enabled=1
                 )
                """,
                (
                    shared_bbs_path or None,
                    shared_bbs_archive_path or None,
                    now_iso,
                    row_id,
                ),
            )
            conn.commit()
            _sync_derived_coordination_policies_conn(conn)
            cluster = next((row for row in _list_varac_clusters_conn(conn) if int(row.get("id", 0) or 0) == row_id), None)
            return dict(cluster or {})

    def delete_varac_cluster(self, cluster_id: int) -> None:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            cluster = _varac_cluster_by_id(conn, int(cluster_id))
            if not cluster:
                raise KeyError(f"Unknown VarAC cluster id: {cluster_id}")
            conn.execute("DELETE FROM varac_cluster_members WHERE cluster_id=?", (int(cluster_id),))
            conn.execute("DELETE FROM varac_clusters WHERE id=?", (int(cluster_id),))
            conn.commit()
            _sync_derived_coordination_policies_conn(conn)

    def set_varac_cluster_member(
        self,
        cluster_id: int,
        device_profile_id: int,
        *,
        instance_number: int,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            cluster = _varac_cluster_by_id(conn, int(cluster_id))
            if not cluster:
                raise KeyError(f"Unknown VarAC cluster id: {cluster_id}")
            device = _record_by_id(conn, "device_profiles", int(device_profile_id))
            if not device:
                raise KeyError(f"Unknown device profile id: {device_profile_id}")
            if _is_observer_device_class(device):
                raise ValueError("Observer / SDR device profiles cannot participate in VarAC clusters.")
            instance_value = _coerce_int(instance_number, 0)
            if instance_value <= 0:
                raise ValueError("VarAC cluster instance number must be a positive integer.")
            enabled_value = _coerce_bool_int(enabled, True)
            gateway_id = (
                int(cluster.get("gateway_handler_device_id", 0) or 0)
                if cluster.get("gateway_handler_device_id") not in (None, "")
                else 0
            )
            email_sender_id = (
                int(cluster.get("email_gateway_sender_device_id", 0) or 0)
                if cluster.get("email_gateway_sender_device_id") not in (None, "")
                else 0
            )
            existing = _varac_cluster_membership_row(conn, int(cluster_id), int(device_profile_id))
            if enabled_value == 1:
                other_membership = _varac_enabled_membership_for_device(
                    conn,
                    int(device_profile_id),
                    exclude_cluster_id=int(cluster_id),
                )
                if other_membership is not None:
                    other_cluster = _varac_cluster_by_id(conn, int(other_membership.get("cluster_id", 0) or 0))
                    raise ValueError(
                        "Each device profile may have only one enabled VarAC cluster membership in this phase"
                        + (
                            f" ({str((other_cluster or {}).get('name', '') or '').strip()})."
                            if other_cluster
                            else "."
                        )
                    )
            if existing is not None and enabled_value != 1 and gateway_id == int(device_profile_id):
                raise ValueError("Clear or reassign the VarAC gateway handler before disabling this membership.")
            if existing is not None and enabled_value != 1 and email_sender_id == int(device_profile_id):
                raise ValueError(
                    "Clear or reassign the VarAC email gateway sender before disabling this membership."
                )

            duplicate = conn.execute(
                """
                SELECT device_profile_id
                  FROM varac_cluster_members
                 WHERE cluster_id=?
                   AND instance_number=?
                   AND device_profile_id<>?
                   AND enabled=1
                 LIMIT 1
                """,
                (int(cluster_id), instance_value, int(device_profile_id)),
            ).fetchone()
            if enabled_value == 1 and duplicate is not None:
                duplicate_id = int(duplicate[0] or 0)
                duplicate_device = _record_by_id(conn, "device_profiles", duplicate_id)
                duplicate_name = _coerce_text((duplicate_device or {}).get("name", f"Device {duplicate_id}"), f"Device {duplicate_id}")
                raise ValueError(
                    f"VarAC instance number {instance_value} is already assigned to {duplicate_name} in this cluster."
                )

            now_iso = _utc_now_iso()
            if existing is None:
                try:
                    conn.execute(
                        """
                        INSERT INTO varac_cluster_members (
                            cluster_id, device_profile_id, instance_number, enabled, created_utc, updated_utc
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(cluster_id),
                            int(device_profile_id),
                            instance_value,
                            enabled_value,
                            now_iso,
                            now_iso,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("Unable to save the VarAC cluster membership. Check cluster and instance uniqueness.") from exc
            else:
                try:
                    conn.execute(
                        """
                        UPDATE varac_cluster_members
                           SET instance_number=?, enabled=?, updated_utc=?
                         WHERE cluster_id=? AND device_profile_id=?
                        """,
                        (
                            instance_value,
                            enabled_value,
                            now_iso,
                            int(cluster_id),
                            int(device_profile_id),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("Unable to update the VarAC cluster membership. Check cluster and instance uniqueness.") from exc
            if enabled_value == 1:
                conn.execute(
                    """
                    UPDATE device_profiles
                       SET varac_bbs_dir=?, varac_bbs_archive_dir=?, updated_utc=?
                     WHERE id=?
                    """,
                    (
                        str(cluster.get("shared_bbs_path") or "") or None,
                        str(cluster.get("shared_bbs_archive_path") or "") or None,
                        now_iso,
                        int(device_profile_id),
                    ),
                )
            conn.commit()
            _sync_derived_coordination_policies_conn(conn)
            rows = _list_varac_cluster_members_conn(conn, cluster_id=int(cluster_id), device_profile_id=int(device_profile_id))
            return dict(rows[0]) if rows else {}

    def remove_varac_cluster_member(self, cluster_id: int, device_profile_id: int) -> None:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            cluster = _varac_cluster_by_id(conn, int(cluster_id))
            if not cluster:
                raise KeyError(f"Unknown VarAC cluster id: {cluster_id}")
            membership = _varac_cluster_membership_row(conn, int(cluster_id), int(device_profile_id))
            if membership is None:
                raise KeyError(f"Unknown VarAC cluster membership: cluster={cluster_id}, device={device_profile_id}")
            gateway_id = (
                int(cluster.get("gateway_handler_device_id", 0) or 0)
                if cluster.get("gateway_handler_device_id") not in (None, "")
                else 0
            )
            email_sender_id = (
                int(cluster.get("email_gateway_sender_device_id", 0) or 0)
                if cluster.get("email_gateway_sender_device_id") not in (None, "")
                else 0
            )
            if gateway_id == int(device_profile_id):
                raise ValueError("Clear or reassign the VarAC gateway handler before removing this membership.")
            if email_sender_id == int(device_profile_id):
                raise ValueError(
                    "Clear or reassign the VarAC email gateway sender before removing this membership."
                )
            conn.execute(
                "DELETE FROM varac_cluster_members WHERE cluster_id=? AND device_profile_id=?",
                (int(cluster_id), int(device_profile_id)),
            )
            conn.commit()
            _sync_derived_coordination_policies_conn(conn)

    def set_varac_cluster_gateway_handler(
        self,
        cluster_id: int,
        gateway_handler_device_id: Optional[int],
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            cluster = _varac_cluster_by_id(conn, int(cluster_id))
            if not cluster:
                raise KeyError(f"Unknown VarAC cluster id: {cluster_id}")
            gateway_value = _coerce_optional_int(gateway_handler_device_id)
            if gateway_value is not None:
                membership = _varac_cluster_membership_row(conn, int(cluster_id), int(gateway_value))
                if membership is None or int(membership.get("enabled", 1) or 0) != 1:
                    raise ValueError("The VarAC gateway handler must be an enabled member of this cluster.")
            conn.execute(
                "UPDATE varac_clusters SET gateway_handler_device_id=?, updated_utc=? WHERE id=?",
                (gateway_value, _utc_now_iso(), int(cluster_id)),
            )
            conn.commit()
            _sync_derived_coordination_policies_conn(conn)
            cluster_row = next((row for row in _list_varac_clusters_conn(conn) if int(row.get("id", 0) or 0) == int(cluster_id)), None)
            return dict(cluster_row or {})

    def set_varac_cluster_email_gateway_sender(
        self,
        cluster_id: int,
        email_gateway_sender_device_id: Optional[int],
    ) -> Dict[str, Any]:
        """Set the native email-relay sender without reinterpreting legacy gateway data."""

        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            cluster = _varac_cluster_by_id(conn, int(cluster_id))
            if not cluster:
                raise KeyError(f"Unknown VarAC cluster id: {cluster_id}")
            sender_value = _coerce_optional_int(email_gateway_sender_device_id)
            if sender_value is not None:
                membership = _varac_cluster_membership_row(conn, int(cluster_id), int(sender_value))
                if membership is None or int(membership.get("enabled", 1) or 0) != 1:
                    raise ValueError(
                        "The VarAC email gateway sender must be an enabled member of this cluster."
                    )
            conn.execute(
                "UPDATE varac_clusters SET email_gateway_sender_device_id=?, updated_utc=? WHERE id=?",
                (sender_value, _utc_now_iso(), int(cluster_id)),
            )
            conn.commit()
            cluster_row = next(
                (
                    row
                    for row in _list_varac_clusters_conn(conn)
                    if int(row.get("id", 0) or 0) == int(cluster_id)
                ),
                None,
            )
            return dict(cluster_row or {})

    def begin_varac_native_apply_journal(
        self,
        *,
        journal_id: str,
        plan_fingerprint: str,
        operation: str,
        writer_key: str,
        targets: Any,
        desired: Any,
        exclusive: bool = False,
    ) -> Dict[str, Any]:
        """Durably record intent before any third-party VarAC file is touched."""

        entry_id = _coerce_text(journal_id, "")
        fingerprint = _coerce_text(plan_fingerprint, "")
        writer = _coerce_text(writer_key, "")
        if not entry_id or not fingerprint or not writer:
            raise ValueError("Native VarAC apply journal identity is incomplete.")
        now_iso = _utc_now_iso()
        with self._open_connection() as conn:
            if exclusive:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM varac_native_apply_journal WHERE state NOT IN ('complete','rolled_back') LIMIT 1").fetchone():
                    raise RuntimeError("Another native VarAC apply or recovery is pending")
            conn.execute(
                """
                INSERT INTO varac_native_apply_journal (
                    id, plan_fingerprint, state, operation, writer_key,
                    targets_json, desired_json, observed_json,
                    backup_manifest_json, error, created_utc, updated_utc, completed_utc
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?, '{}', '{}', NULL, ?, ?, NULL)
                """,
                (
                    entry_id,
                    fingerprint,
                    _coerce_text(operation, ""),
                    writer,
                    json.dumps(targets if isinstance(targets, (list, tuple)) else [], sort_keys=True),
                    json.dumps(desired if isinstance(desired, Mapping) else {}, sort_keys=True),
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()
            return self._get_varac_native_apply_journal_conn(conn, entry_id) or {}

    def update_varac_native_apply_journal(
        self,
        journal_id: str,
        *,
        state: str,
        expected_states: Iterable[str],
        observed: Any = None,
        backup_manifest: Any = None,
        error: str = "",
        durable: bool = True,
    ) -> Dict[str, Any]:
        """Compare-and-set one journal state, optionally in the guided transaction."""

        entry_id = _coerce_text(journal_id, "")
        next_state = _coerce_text(state, "").lower()
        expected = {_coerce_text(item, "").lower() for item in expected_states if _coerce_text(item, "")}
        if next_state not in SUPPORTED_VARAC_NATIVE_JOURNAL_STATES:
            raise ValueError("Unsupported VarAC native journal state.")
        if not expected:
            raise ValueError("A VarAC native journal transition requires an expected state.")
        connection = self._open_connection() if durable else self._connect()
        with connection as conn:
            current = self._get_varac_native_apply_journal_conn(conn, entry_id)
            if current is None:
                raise KeyError(f"Unknown VarAC native apply journal: {entry_id}")
            current_state = _coerce_text(current.get("state", ""), "").lower()
            if current_state not in expected:
                raise ValueError(
                    f"Stale VarAC native journal transition: expected {sorted(expected)}, found {current_state or 'unknown'}."
                )
            allowed = {
                "pending": {"backup_ready", "rolled_back", "recovery_required"},
                "backup_ready": {"promoting", "rolled_back", "recovery_required"},
                "promoting": {"external_applied", "rolled_back", "recovery_required"},
                "external_applied": {"fio_committed", "rolled_back", "recovery_required"},
                "fio_committed": {"complete", "recovery_required"},
                "recovery_required": {"rolled_back", "complete"},
                "complete": set(),
                "rolled_back": set(),
            }
            if next_state != current_state and next_state not in allowed.get(current_state, set()):
                raise ValueError(
                    f"Invalid VarAC native journal transition: {current_state} -> {next_state}."
                )
            now_iso = _utc_now_iso()
            observed_json = current.get("observed_json", "{}")
            backup_json = current.get("backup_manifest_json", "{}")
            if isinstance(observed, Mapping):
                observed_json = json.dumps(dict(observed), sort_keys=True)
            if isinstance(backup_manifest, Mapping):
                backup_json = json.dumps(dict(backup_manifest), sort_keys=True)
            completed_utc = now_iso if next_state in {"complete", "rolled_back"} else None
            conn.execute(
                """
                UPDATE varac_native_apply_journal
                   SET state=?, observed_json=?, backup_manifest_json=?, error=?,
                       updated_utc=?, completed_utc=?
                 WHERE id=? AND state=?
                """,
                (
                    next_state,
                    observed_json,
                    backup_json,
                    _coerce_text(error, "") or None,
                    now_iso,
                    completed_utc,
                    entry_id,
                    current_state,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("VarAC native journal state changed concurrently.")
            conn.commit()
            return self._get_varac_native_apply_journal_conn(conn, entry_id) or {}

    def get_varac_native_apply_journal(self, journal_id: str) -> Optional[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            return self._get_varac_native_apply_journal_conn(conn, _coerce_text(journal_id, ""))

    def list_unfinished_varac_native_applies(self) -> List[Dict[str, Any]]:
        with self._connect_readonly() as conn:
            rows = conn.execute(
                """
                SELECT * FROM varac_native_apply_journal
                 WHERE state NOT IN ('complete', 'rolled_back')
              ORDER BY created_utc ASC, id ASC
                """
            ).fetchall()
            return [self._decode_varac_native_journal_row(dict(row)) for row in rows]

    def varac_native_launch_blockers(self) -> List[Dict[str, Any]]:
        """Return recovery work that must block affected managed VarAC launches."""

        return self.list_unfinished_varac_native_applies()

    @staticmethod
    def _decode_radio_hardware_binding_row(row: Mapping[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        for column, target in (
            ("selector_json", "selector"),
            ("observed_locator_json", "observed_locator"),
            ("evidence_json", "evidence"),
        ):
            try:
                parsed = json.loads(str(data.get(column, "") or "{}"))
            except (TypeError, ValueError):
                parsed = {}
            data[target] = dict(parsed) if isinstance(parsed, Mapping) else {}
        data["required"] = bool(int(data.get("required", 0) or 0))
        data["transmit_capable"] = bool(int(data.get("transmit_capable", 0) or 0))
        data["active"] = bool(int(data.get("active", 0) or 0))
        return data

    @classmethod
    def _radio_hardware_bundle_conn(
        cls,
        conn: sqlite3.Connection,
        radio_profile_id: int,
        *,
        include_retired: bool = False,
    ) -> Optional[Dict[str, Any]]:
        bundle_row = conn.execute(
            "SELECT * FROM radio_hardware_bundles WHERE radio_profile_id=?",
            (int(radio_profile_id),),
        ).fetchone()
        if bundle_row is None:
            return None
        bundle = dict(bundle_row)
        try:
            recovery = json.loads(str(bundle.get("recovery_json", "") or "{}"))
        except (TypeError, ValueError):
            recovery = {}
        bundle["recovery"] = dict(recovery) if isinstance(recovery, Mapping) else {}
        active_clause = "" if include_retired else " AND active=1"
        rows = conn.execute(
            "SELECT * FROM radio_hardware_bindings WHERE radio_profile_id=?"
            f"{active_clause} ORDER BY generation DESC, role ASC, created_utc ASC",
            (int(radio_profile_id),),
        ).fetchall()
        bundle["bindings"] = [cls._decode_radio_hardware_binding_row(dict(row)) for row in rows]
        return bundle

    def get_radio_hardware_bundle(
        self,
        radio_profile_id: int,
        *,
        include_retired: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return the radio-owned hardware bundle without resolving OS devices."""

        with self._connect_readonly() as conn:
            return self._radio_hardware_bundle_conn(
                conn,
                int(radio_profile_id),
                include_retired=include_retired,
            )

    def list_radio_hardware_bundles(self) -> List[Dict[str, Any]]:
        """Return configured bundles; legacy radios intentionally have no row."""

        with self._connect_readonly() as conn:
            radio_ids = [
                int(row[0])
                for row in conn.execute(
                    "SELECT radio_profile_id FROM radio_hardware_bundles ORDER BY radio_profile_id"
                ).fetchall()
            ]
            return [
                bundle
                for radio_id in radio_ids
                if (bundle := self._radio_hardware_bundle_conn(conn, radio_id)) is not None
            ]

    def save_radio_hardware_bundle(
        self,
        radio_profile_id: int,
        bindings: Iterable[Mapping[str, Any]],
        *,
        expected_generation: Optional[int] = None,
        source: str = "operator_confirmed",
    ) -> Dict[str, Any]:
        """Atomically replace one reviewed radio hardware bundle.

        This stores identity only.  It does not enumerate hardware, open a
        device, change application-native files, launch software, or control a
        radio.  Existing bundles require an expected generation so a stale UI
        draft cannot overwrite a newer operator choice.
        """

        radio_id = int(radio_profile_id)
        normalized = [normalize_hardware_binding(binding) for binding in bindings]
        if not normalized:
            raise HardwareBindingError("At least one reviewed hardware assignment is required.")
        roles = [str(binding["role"]) for binding in normalized]
        if len(set(roles)) != len(roles):
            raise HardwareBindingError("A radio can have only one active assignment for each hardware role.")
        if any(role not in HARDWARE_ROLES for role in roles):
            raise HardwareBindingError("The hardware bundle contains an unsupported role.")

        now_iso = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                radio = _record_by_id(conn, "device_profiles", radio_id)
                if radio is None:
                    raise KeyError(f"Unknown radio profile id: {radio_id}")
                observer_profile = _is_observer_device_class(radio)
                if observer_profile and any(role in {"ptt", "audio_tx"} for role in roles):
                    raise HardwareBindingError(
                        "Observer / SDR profiles are receive-only and cannot own PTT or transmit-audio hardware."
                    )
                if not observer_profile and any(
                    binding["role"] == "audio_rx" and binding["transport"] == "pulse-monitor"
                    for binding in normalized
                ):
                    raise HardwareBindingError(
                        "A monitor or virtual audio source may be assigned only to an "
                        "Observer / SDR receive-only profile."
                    )
                existing_row = conn.execute(
                    "SELECT * FROM radio_hardware_bundles WHERE radio_profile_id=?",
                    (radio_id,),
                ).fetchone()
                existing = dict(existing_row) if existing_row is not None else None
                current_generation = int((existing or {}).get("generation", 0) or 0)
                if existing is not None and expected_generation is None:
                    raise HardwareBindingError(
                        "Updating a hardware assignment requires its current generation. Refresh and try again."
                    )
                if expected_generation is not None and int(expected_generation) != current_generation:
                    raise HardwareBindingError(
                        f"The hardware assignment changed after review (expected generation {int(expected_generation)}, "
                        f"found {current_generation}). Refresh and review again."
                    )

                for binding in normalized:
                    conflicts = conn.execute(
                        """
                        SELECT b.role, b.radio_profile_id, b.claim_key,
                               b.selector_json, d.name AS radio_name
                          FROM radio_hardware_bindings AS b
                          JOIN device_profiles AS d ON d.id=b.radio_profile_id
                         WHERE b.active=1
                           AND b.radio_profile_id<>?
                           AND b.platform=?
                           AND b.provider=?
                           AND b.transport=?
                      ORDER BY LOWER(d.name), b.role
                        """,
                        (
                            radio_id,
                            binding["platform"],
                            binding["provider"],
                            binding["transport"],
                        ),
                    ).fetchall()
                    conflict = None
                    for row in conflicts:
                        candidate = dict(row)
                        try:
                            saved_selector = json.loads(
                                str(candidate.get("selector_json", "") or "{}")
                            )
                        except (TypeError, ValueError):
                            saved_selector = {}
                        same_claim = str(candidate.get("claim_key", "") or "") == str(
                            binding["claim_key"]
                        )
                        semantic_match = False
                        if isinstance(saved_selector, Mapping) and saved_selector:
                            semantic_match = (
                                match_hardware_selector(
                                    saved_selector,
                                    ({"selector": binding["selector"]},),
                                ).state
                                == "exact"
                                or match_hardware_selector(
                                    binding["selector"],
                                    ({"selector": saved_selector},),
                                ).state
                                == "exact"
                            )
                        if same_claim or semantic_match:
                            conflict = candidate
                            break
                    if conflict is not None:
                        raise HardwareBindingError(
                            f"{binding['role'].upper()} hardware is already assigned to "
                            f"{str(conflict.get('radio_name', '') or 'another radio')} "
                            f"as {str(conflict.get('role', '') or 'hardware').upper()}. "
                            "Choose another device or edit that radio's Connections assignment."
                        )

                generation = current_generation + 1
                bundle_key = str((existing or {}).get("bundle_key", "") or "").strip()
                if not bundle_key:
                    bundle_key = f"radio-hardware:{radio_id}:{uuid.uuid4().hex}"
                previous_rows = conn.execute(
                    "SELECT binding_key FROM radio_hardware_bindings WHERE radio_profile_id=? AND active=1",
                    (radio_id,),
                ).fetchall()
                previous_keys = [str(row[0]) for row in previous_rows]
                recovery = {
                    "prior_generation": current_generation,
                    "prior_binding_keys": previous_keys,
                }
                conn.execute(
                    """
                    INSERT INTO radio_hardware_bundles (
                        radio_profile_id, bundle_key, schema_version, generation,
                        lifecycle_state, aggregate_state, source, recovery_json,
                        last_verified_utc, created_utc, updated_utc
                    ) VALUES (?, ?, ?, ?, 'configured', 'unverified', ?, ?, NULL, ?, ?)
                    ON CONFLICT(radio_profile_id) DO UPDATE SET
                        schema_version=excluded.schema_version,
                        generation=excluded.generation,
                        lifecycle_state='configured',
                        aggregate_state='unverified',
                        source=excluded.source,
                        recovery_json=excluded.recovery_json,
                        last_verified_utc=NULL,
                        updated_utc=excluded.updated_utc
                    """,
                    (
                        radio_id,
                        bundle_key,
                        HARDWARE_BINDING_SCHEMA_VERSION,
                        generation,
                        str(source or "operator_confirmed").strip() or "operator_confirmed",
                        json.dumps(recovery, sort_keys=True),
                        now_iso,
                        now_iso,
                    ),
                )
                conn.execute(
                    """
                    UPDATE radio_hardware_bindings
                       SET active=0, retired_utc=?, updated_utc=?
                     WHERE radio_profile_id=? AND active=1
                    """,
                    (now_iso, now_iso, radio_id),
                )
                for binding in normalized:
                    binding_key = f"hardware-binding:{uuid.uuid4().hex}"
                    conn.execute(
                        """
                        INSERT INTO radio_hardware_bindings (
                            binding_key, bundle_key, radio_profile_id, role,
                            generation, platform, provider, transport,
                            selector_json, selector_fingerprint, claim_key, label,
                            observed_locator_json, match_state, stability_class,
                            required, transmit_capable, desired_fingerprint,
                            observed_fingerprint, verified_fingerprint,
                            verification_state, evidence_json, last_verified_utc,
                            last_error, active, created_utc, updated_utc, retired_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'missing', ?, ?, ?, ?, NULL, NULL,
                                  'unverified', '{}', NULL, NULL, 1, ?, ?, NULL)
                        """,
                        (
                            binding_key,
                            bundle_key,
                            radio_id,
                            binding["role"],
                            generation,
                            binding["platform"],
                            binding["provider"],
                            binding["transport"],
                            json.dumps(binding["selector"], sort_keys=True),
                            binding["selector_fingerprint"],
                            binding["claim_key"],
                            binding["label"] or None,
                            json.dumps(binding["observed_locator"], sort_keys=True),
                            binding["stability_class"],
                            int(bool(binding["required"])),
                            int(bool(binding["transmit_capable"])),
                            binding["selector_fingerprint"],
                            now_iso,
                            now_iso,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            saved = self._radio_hardware_bundle_conn(conn, radio_id)
            if saved is None:
                raise RuntimeError("Saved hardware assignment could not be read back.")
            return saved

    def retire_radio_hardware_bundle(
        self,
        radio_profile_id: int,
        *,
        expected_generation: int,
    ) -> Dict[str, Any]:
        """Retire an assignment without deleting its bounded recovery evidence."""

        radio_id = int(radio_profile_id)
        now_iso = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT generation FROM radio_hardware_bundles WHERE radio_profile_id=?",
                    (radio_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"No hardware assignment exists for radio profile id: {radio_id}")
                current_generation = int(row[0] or 0)
                if current_generation != int(expected_generation):
                    raise HardwareBindingError(
                        f"The hardware assignment changed after review (expected generation {int(expected_generation)}, "
                        f"found {current_generation}). Refresh and review again."
                    )
                next_generation = current_generation + 1
                conn.execute(
                    """
                    UPDATE radio_hardware_bindings
                       SET active=0, retired_utc=?, updated_utc=?
                     WHERE radio_profile_id=? AND active=1
                    """,
                    (now_iso, now_iso, radio_id),
                )
                conn.execute(
                    """
                    UPDATE radio_hardware_bundles
                       SET generation=?, lifecycle_state='retired',
                           aggregate_state='unbound', last_verified_utc=NULL,
                           updated_utc=?
                     WHERE radio_profile_id=?
                    """,
                    (next_generation, now_iso, radio_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            saved = self._radio_hardware_bundle_conn(conn, radio_id, include_retired=True)
            if saved is None:
                raise RuntimeError("Retired hardware assignment could not be read back.")
            return saved

    @staticmethod
    def _decode_varac_native_journal_row(row: Mapping[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        for column, key, default in (
            ("targets_json", "targets", []),
            ("desired_json", "desired", {}),
            ("observed_json", "observed", {}),
            ("backup_manifest_json", "backup_manifest", {}),
        ):
            try:
                data[key] = json.loads(str(data.get(column, "") or ""))
            except Exception:
                data[key] = default
        return data

    @classmethod
    def _get_varac_native_apply_journal_conn(
        cls,
        conn: sqlite3.Connection,
        journal_id: str,
    ) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM varac_native_apply_journal WHERE id=?",
            (str(journal_id),),
        ).fetchone()
        if row is None:
            return None
        return cls._decode_varac_native_journal_row(dict(row))

    def save_device_profile(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            return self._save_device_profile_conn(conn, values)

    def delete_device_profile(self, device_profile_id: int) -> None:
        with self._connect() as conn:
            device = _record_by_id(conn, "device_profiles", int(device_profile_id))
            if not device:
                raise KeyError(f"Unknown device profile id: {device_profile_id}")
            if int(device.get("runtime_active", 0) or 0) == 1:
                raise ValueError("Cannot delete an active radio. Stop using it first.")
            conn.execute(
                "UPDATE varac_clusters SET gateway_handler_device_id=NULL WHERE gateway_handler_device_id=?",
                (int(device_profile_id),),
            )
            conn.execute(
                "UPDATE varac_clusters SET email_gateway_sender_device_id=NULL WHERE email_gateway_sender_device_id=?",
                (int(device_profile_id),),
            )
            conn.execute("DELETE FROM operating_profile_assignments WHERE device_profile_id=?", (int(device_profile_id),))
            conn.execute("DELETE FROM assigned_plans WHERE device_profile_id=?", (int(device_profile_id),))
            conn.execute("DELETE FROM varac_cluster_members WHERE device_profile_id=?", (int(device_profile_id),))
            conn.execute("DELETE FROM runtime_policies WHERE radio_profile_id=?", (int(device_profile_id),))
            conn.execute(
                "DELETE FROM station_coordination_policies WHERE source_device_id=? OR target_device_id=?",
                (int(device_profile_id), int(device_profile_id)),
            )
            conn.execute("DELETE FROM device_profiles WHERE id=?", (int(device_profile_id),))
            orphan_links = (
                ("js8_instances", "js8_instance_id"),
                ("fast_light_configs", "fast_light_config_id"),
                ("varac_nodes", "varac_node_id"),
            )
            for table_name, device_column in orphan_links:
                linked_id = int(device.get(device_column, 0) or 0)
                if linked_id <= 0:
                    continue
                count = conn.execute(
                    f"SELECT COUNT(*) FROM device_profiles WHERE {device_column}=?",
                    (linked_id,),
                ).fetchone()[0]
                if int(count or 0) == 0:
                    conn.execute(f"DELETE FROM {table_name} WHERE id=?", (linked_id,))
            conn.commit()
            _sync_derived_coordination_policies_conn(conn)
            _normalize_runtime_primary_device(conn)

    def set_runtime_active_device_profile(self, device_profile_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            active_swap = _active_profile_swap_policy_conn(conn)
            if active_swap is not None:
                current_target_id = int(active_swap.get("target_device_id", 0) or 0)
                if int(device_profile_id) != current_target_id:
                    raise ValueError("Restore the active Temporary Plan Swap before changing the primary radio.")
            return self._set_runtime_active_device_conn(conn, int(device_profile_id), deactivate_others=False)

    def set_runtime_primary_device_profile(self, device_profile_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            active_swap = _active_profile_swap_policy_conn(conn)
            if active_swap is not None:
                current_target_id = int(active_swap.get("target_device_id", 0) or 0)
                if int(device_profile_id) != current_target_id:
                    raise ValueError("Restore the active Temporary Plan Swap before changing the primary radio.")
            return self._set_runtime_primary_device_conn(conn, int(device_profile_id), deactivate_others=False)

    def sync_shared_ptt_policies(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return _sync_shared_ptt_policies_conn(conn)

    def sync_rf_conflict_policies(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return _sync_rf_conflict_policies_conn(conn)

    def list_station_coordination_policies(self, policy_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            ensure_multi_radio_settings_schema(conn)
            _sync_derived_coordination_policies_conn(conn)
            params: List[Any] = []
            where = ""
            normalized_type = _coerce_text(policy_type, "").lower()
            if normalized_type:
                where = "WHERE policy_type=?"
                params.append(normalized_type)
            rows = conn.execute(
                f"""
                SELECT *
                  FROM station_coordination_policies
                  {where}
              ORDER BY priority ASC, policy_type ASC, source_device_id ASC, target_device_id ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
            return [_coordination_policy_from_row(row) for row in rows]

    def set_device_profile_runtime_active(self, device_profile_id: int, active: bool) -> Dict[str, Any]:
        with self._connect() as conn:
            device = _record_by_id(conn, "device_profiles", int(device_profile_id))
            if not device:
                raise KeyError(f"Unknown device profile id: {device_profile_id}")
            active_swap = _active_profile_swap_policy_conn(conn)
            if active_swap is not None:
                source_id = int(active_swap.get("source_device_id", 0) or 0)
                target_id = int(active_swap.get("target_device_id", 0) or 0)
                if int(device_profile_id) in {source_id, target_id}:
                    raise ValueError("Restore the active Temporary Plan Swap before changing runtime activation on the source/target radios.")
            if active:
                backend = _coerce_text(device.get("control_backend", "manual"), "manual").lower()
                if backend not in SUPPORTED_RUNTIME_CONTROL_BACKENDS:
                    raise ValueError(f"Cannot activate backend until runtime support exists: {backend}")
                if _coerce_text(device.get("device_class", "tx_rx"), "tx_rx").lower() == "observer":
                    assignment = _effective_assignment_for_device(conn, int(device_profile_id))
                    if not assignment:
                        raise ValueError("Observer / SDR radios require receive-only operating models before activation.")
                    operating = _record_by_id(
                        conn,
                        "operating_profiles",
                        int(assignment.get("operating_profile_id", 0) or 0),
                    )
                    if operating:
                        _validate_assignment_plan_compatibility(device, operating)
                conn.execute("UPDATE device_profiles SET runtime_active=1 WHERE id=?", (int(device_profile_id),))
                assignment = _effective_assignment_for_device(conn, int(device_profile_id))
                if not assignment:
                    operating = _record_by_system_key(conn, "operating_profiles", DEFAULT_OPERATING_SYSTEM_KEY)
                    if not operating:
                        operating = _save_operating_profile_conn(conn, _seed_operating_defaults(_load_kv_settings(conn)))
                    _ensure_default_assignment(conn, int(device_profile_id), int(operating["id"]))
                conn.commit()
                primary_id = _normalize_runtime_primary_device(conn)
                if primary_id == int(device_profile_id):
                    project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(device_profile_id))
                return _resolve_device_profile_links_conn(conn, _record_by_id(conn, "device_profiles", int(device_profile_id)) or device)

            if int(device.get("runtime_primary", 0) or 0) == 1:
                raise ValueError("Cannot deactivate the primary runtime device profile. Make another device primary first.")

            active_profiles = _runtime_active_device_profiles(conn)
            if len(active_profiles) <= 1:
                raise ValueError("At least one active radio must remain enabled.")

            conn.execute("UPDATE device_profiles SET runtime_active=0, runtime_primary=0 WHERE id=?", (int(device_profile_id),))
            conn.commit()
            primary_id = _normalize_runtime_primary_device(conn)
            if primary_id is not None:
                project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(primary_id))
            return _resolve_device_profile_links_conn(conn, _record_by_id(conn, "device_profiles", int(device_profile_id)) or device)

    def sync_runtime_active_device_to_legacy_settings(self, device_profile_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            return project_runtime_active_device_to_legacy_settings(conn, int(device_profile_id)) or {}

    def sync_runtime_active_device_to_legacy_settings_if_single_active(self, device_profile_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            return project_runtime_active_device_to_legacy_settings_if_single_active(conn, int(device_profile_id)) or {}
