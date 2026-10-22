"""Read-only catalog snapshots and pure compatibility projections for BBS setup.

Only load at an existing database/worker boundary. Rendering and readiness use
the published value, never reopen SQLite or create a schema.
"""
from copy import deepcopy
import json

BBS_CATALOG_SETTINGS_KEYS = frozenset({'station_bbs_catalog', 'varac_bbs_vault_enabled',
    'varac_bbs_vault_managed_root', 'varac_bbs_vault_default_location_id', 'varac_bbs_vault_locations_v1'})


def load_station_bbs_catalog(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('bbs_library_meta','bbs_locations')")}
    if len(tables) != 2:
        return None
    meta = dict(conn.execute("SELECT key,value FROM bbs_library_meta WHERE key IN ('station_enabled','station_managed_root','station_default_location_id')"))
    rows = conn.execute('SELECT location_id,name,source_dir,enabled,metadata_json,access_rule,retention_mode,retention_days FROM bbs_locations ORDER BY location_id LIMIT 1001').fetchall()
    if not meta:
        # Legacy catalogs without station metadata are still legacy-owned.
        return None
    if len(rows) > 1000:
        raise ValueError('Station BBS catalog exceeds the supported setup snapshot size; review the catalog.')
    locations = []
    for row in rows:
        try:
            data = json.loads(row[4] or '{}')
        except (TypeError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        data.update(id=row[0], name=row[1], source_dir=row[2] or '', enabled=bool(row[3]),
                    open_rule=row[5], retention_policy={'manual':'Keep until manually removed',
                    'expire_after_days':'Archive this location by age'}.get(row[6], 'Use global BBS archive policy'), retention_days=row[7])
        locations.append(data)
    return dict(enabled=meta.get('station_enabled', '1') == '1',
                managed_root=str(meta.get('station_managed_root') or ''),
                default_location_id=str(meta.get('station_default_location_id') or 'default'),
                locations=locations)


def project_station_bbs_settings(settings, snapshot):
    """Station metadata wins over radio compatibility mirrors, including blanks."""
    result = dict(settings)
    if snapshot is not None:
        result.update(station_bbs_catalog=deepcopy(snapshot),
                      varac_bbs_vault_enabled=bool(snapshot.get('enabled')),
                      varac_bbs_vault_managed_root=snapshot.get('managed_root', ''),
                      varac_bbs_vault_default_location_id=snapshot.get('default_location_id', 'default'),
                      varac_bbs_vault_locations_v1=deepcopy(snapshot.get('locations', [])))
    return result
