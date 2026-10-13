"""Worker-only, read-only retained mesh snapshot construction."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from freqinout.core.sqlite_utils import connect_sqlite_readonly

from freqinout.gui.mesh_map_projection import build_mesh_route_projection


def enrich_mesh_map_snapshot(payload: dict) -> dict:
    projected = dict(payload)
    db_path = projected.pop('_mesh_db_path', None)
    if db_path is None:
        return projected
    rows = []
    total = 0
    if Path(db_path).is_file():
        conn = connect_sqlite_readonly(db_path, timeout=0.5, row_factory=sqlite3.Row)
        try:
            # One read transaction keeps counts and rows consistent during ingestion.
            conn.execute('BEGIN')
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mesh_nodes'").fetchone()
            if exists:
                total = conn.execute('SELECT COUNT(*) FROM mesh_nodes').fetchone()[0]
                records = conn.execute("SELECT * FROM mesh_nodes ORDER BY CASE WHEN replace(raw_payload_json, ' ', '') LIKE '%\"is_self\":true%' THEN 0 ELSE 1 END, last_heard_utc DESC, source_ref LIMIT 1500").fetchall()
                for record in records:
                    row = dict(record)
                    try:
                        row['raw_payload'] = json.loads(row.pop('raw_payload_json') or '{}')
                    except (ValueError, TypeError):
                        row['raw_payload'] = {}
                    if not isinstance(row['raw_payload'], dict):
                        row['raw_payload'] = {}
                    rows.append(row)
        finally:
            conn.close()
    graph = build_mesh_route_projection(rows, include_paths=bool(projected.get('_mesh_show_paths')))
    recent_only = bool(projected.pop('_mesh_recent_only', False))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    def recently_heard(marker):
        try:
            stamp = datetime.fromisoformat(marker['provenance']['last_heard_utc'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp >= cutoff
        except (ValueError, TypeError):
            return False
    located = [m for m in graph['markers'] if m['lat'] is not None and m['lon'] is not None]
    excluded = 0
    if recent_only:
        retained = [m for m in located if m.get('is_mesh_self') or recently_heard(m)]
        excluded = len(located) - len(retained)
        ids = {m['id'] for m in retained}
        graph['links'] = [link for link in graph['links'] if link['origin'] in ids and link['destination'] in ids]
        graph['markers'] = retained
    by_node = {(row.get('adapter_id'), row.get('transport'), row.get('node_id')): row for row in rows}
    markers = [m for m in graph['markers'] if m['lat'] is not None and m['lon'] is not None]
    for marker in markers:
        marker['title'] = ('Companion' if marker.get('is_mesh_self') else 'Mesh node') + ' · ' + marker['label']
        marker['tooltip'] = marker['title'] + '\nAdvertised position; position age unknown.\nLast heard · ' + (marker['provenance']['last_heard_utc'] or 'Age unknown')
        row = by_node.get((marker['provenance']['adapter_id'], marker['provenance']['transport'], marker['provenance']['node_id']), {})
        marker['outgoing_route'] = (row.get('raw_payload') or {}).get('outgoing_route', {})
        marker['location_source'] = {'type': 'advertised', 'label': 'Advertised'}
        marker['color_role'] = 'accent' if marker.get('is_mesh_self') else 'info'
    linked_ids = {link[endpoint] for link in graph['links'] for endpoint in ('origin', 'destination')}
    markers.sort(key=lambda marker: (marker['id'] not in linked_ids, not marker.get('is_mesh_self')))
    own_marker = projected.pop('_mesh_operator_marker', None)
    marker_cap = 1199 if isinstance(own_marker, dict) else 1200
    renderer_omitted = max(0, len(markers) - marker_cap)
    markers = markers[:marker_cap]
    visible_ids = {marker['id'] for marker in markers}
    graph['links'] = [link for link in graph['links'] if link['origin'] in visible_ids and link['destination'] in visible_ids]
    summary = dict(graph['summary'])
    summary['observed_links'] = sum(link.get('evidence_kind') == 'observed_packet_path' for link in graph['links'])
    summary.update(known_nodes=total, located_nodes=len(markers), unlocated_nodes=len(rows)-len(located), excluded_nodes=excluded, omitted_nodes=max(0, total-len(rows)) + renderer_omitted)
    for link in graph['links']:
        link['color_role'] = 'text_muted' if link.get('evidence_kind') == 'cached_route' else 'info'
        if link.get('evidence_kind') == 'observed_packet_path':
            link['tooltip'] += '\nSignal measured at companion; not a per-hop measurement.'
    if isinstance(own_marker, dict):
        markers.insert(0, dict(own_marker))
    projected.update(markers=markers, links=graph['links'] if projected.get('_mesh_show_paths') else [], mesh_summary=summary)
    projected.pop('_mesh_show_paths', None)
    return projected
