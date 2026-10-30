"""Read a detached form-tools snapshot on a worker, without SettingsManager."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from freqinout.core.js8_spotter_forms import (
    MAPPER_SETTINGS_KEY, NetFormOptions, discover_effective_spotter_forms,
    resolve_net_form_options,
)
from freqinout.core.sqlite_utils import connect_sqlite_readonly


@dataclass(frozen=True)
class NetFormToolsSnapshot:
    choices: NetFormOptions
    revision: str


def read_net_form_tools(db_path: str | Path, *, fallback_path: str = '',
                        group_key: str = '') -> NetFormToolsSnapshot:
    """Explicit refresh only. Missing/failed stores are errors, never empty success."""
    conn = connect_sqlite_readonly(db_path)
    try:
        rows = conn.execute('SELECT key,value FROM kv WHERE key IN (?,?)',
                            (MAPPER_SETTINGS_KEY, 'js8_forms_path')).fetchall()
    finally:
        conn.close()
    values = {}
    for key, raw in rows:
        if len(str(raw).encode('utf-8')) > 1024 * 1024:
            raise ValueError('Form mappings exceed the supported snapshot size')
        values[key] = json.loads(raw)
    configured = str(values.get('js8_forms_path') or '').strip()
    definitions = discover_effective_spotter_forms(configured, fallback_path if not configured else '')
    mappings = values.get(MAPPER_SETTINGS_KEY, [])
    choices = resolve_net_form_options(definitions, mappings, group_key=group_key)
    revision = hashlib.sha256(json.dumps(
        [mappings, [(d.form_code, d.title, str(d.path)) for d in definitions]],
        ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return NetFormToolsSnapshot(choices, revision)
