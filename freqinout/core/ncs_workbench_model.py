"""Read-only, bounded NCS view of canonical message evidence.

Call query helpers on a worker. Filters, net association and exclusion precede
pagination. No catalog whitelist, reply correlation, raw-log reader or importer.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from types import SimpleNamespace

from freqinout.core.group_utils import normalize_group_name
from freqinout.core.js8_message_policy import canonicalize_js8_payload, directed_js8_payload
from freqinout.core.message_semantics import message_row_kind_label
from freqinout.core.sqlite_utils import connect_sqlite_readonly

MAX_PAGE = 200
# Deliberately anchored support exchanges; ordinary prose and ACK/Y/N remain
# available to NCS judgment. Never derive membership from stations named inside.
_INCIDENTAL = re.compile(
    r'^(?:SNR(?:\?|\s+[-+]?\d+(?:\.\d+)?(?:\s*DB)?)|'
    r'QUERY\s+MSGS?(?:\s+\d+)?|(?:QUERY\s+)?HEARING\??|'
    r'HEARING(?:\s+[A-Z]{1,2}\d[A-Z0-9/]{1,8}(?:\s+[-+]?\d+)?)+|'
    r'(?:HB|HEARTBEAT)(?:\s+ACK)?)$', re.IGNORECASE)


def net_traffic_payload(text: object, *, recipient: str = '') -> str:
    raw = canonicalize_js8_payload(text)
    if re.fullmatch(r'[A-Z0-9/._-]+:\s+[@A-Z0-9/._-]+', raw, re.IGNORECASE):
        return ''
    payload = canonicalize_js8_payload(directed_js8_payload(raw))
    # A protocol relay trailer describes provenance, not meaningful message text.
    # Strip only the complete trailing marker so anchored incidental matching
    # still retains prose that mentions SNR?/HEARING or contains other content.
    payload = re.sub(r'\s+\*DE\*\s+[A-Z0-9/]+$', '', payload, flags=re.IGNORECASE).strip()
    if recipient and payload.split(' ', 1)[0].upper() == recipient.strip().upper():
        payload = payload.partition(' ')[2].strip()
    return payload


def is_incidental_net_traffic(text: object, *, recipient: str = '') -> bool:
    payload = net_traffic_payload(text, recipient=recipient)
    return bool(payload and _INCIDENTAL.fullmatch(payload))


@dataclass(frozen=True)
class NetReportScope:
    run_id: str
    radio_id: int
    instance_id: str
    group_name: str
    operator_call: str
    since_ts: float
    until_ts: float = 0.0

    def __post_init__(self):
        if not self.run_id or self.radio_id <= 0 or not self.instance_id:
            raise ValueError('A pinned net run, radio and instance are required')


@dataclass(frozen=True)
class ReportCursor:
    context: str
    anchor_ts: float
    stamp: float
    message_id: str


@dataclass(frozen=True)
class NetReportPage:
    rows: tuple[dict, ...]
    next_cursor: ReportCursor | None
    anchor_ts: float
    total: int
    newer_available: bool


def _decoded(raw, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except (ValueError, TypeError):
        return fallback


def _prepare(conn, scope, view, anchor):
    if view not in {'net', 'other', 'excluded'}:
        raise ValueError('Unknown report view')
    conn.create_function('ncs_incidental', 1, lambda text: int(is_incidental_net_traffic(text)), deterministic=True)
    conn.create_function('ncs_has_payload', 1, lambda text: int(bool(net_traffic_payload(text))), deterministic=True)
    conn.create_function('ncs_group', 1, lambda text: normalize_group_name(text or '').upper(), deterministic=True)
    # A missing source instance or unknown radio is visible under Other. Another
    # known radio/instance is never silently borrowed for this net. Relay
    # provenance does not veto otherwise clear source/time/destination evidence.
    sql = '''WITH evidence AS (
        SELECT p.*, COALESCE(p.received_ts,p.event_ts,0) AS stamp,
            COALESCE(json_extract(a.values_json,'$.association'),'') AS manual_association,
            COALESCE(json_extract(a.values_json,'$.excluded'),0) AS report_excluded,
            COALESCE(json_extract(a.values_json,'$.reviewed'),0) AS reviewed,
            COALESCE(json_extract(a.values_json,'$.follow_up'),0) AS follow_up,
            COALESCE(json_extract(a.values_json,'$.note'),'') AS note,
            COALESCE(a.version,0) AS annotation_version,
            COALESCE(json_extract(s.values_json,'$.excluded'),0) AS station_excluded,
            COALESCE(s.version,0) AS station_version,
            COALESCE(json_extract(s.values_json,'$.note'),'') AS station_note
        FROM message_projection p
        LEFT JOIN ncs_workbench_annotations a ON a.run_id=? AND a.kind='report'
            AND a.entity_id=p.canonical_key AND a.evidence_version=p.content_hash
        LEFT JOIN ncs_workbench_annotations s ON s.run_id=? AND s.kind='station'
            AND s.entity_id=UPPER(p.from_call) AND s.evidence_version='station'
        WHERE p.source_family IN ('js8','spotter','commstat') AND p.deleted=0
            AND (p.radio_id=? OR p.radio_id IS NULL OR p.radio_id=0)
            AND (COALESCE(p.app_instance_id,'')='' OR p.app_instance_id=?)
            AND ((COALESCE(p.received_ts,p.event_ts,0)>=?
                AND COALESCE(p.received_ts,p.event_ts,0)<=?)
                OR COALESCE(p.received_ts,p.event_ts,0)=0 OR a.run_id IS NOT NULL)
            AND NOT ncs_incidental(COALESCE(NULLIF(p.body_preview,''),p.summary,''))
    ), associated AS (
        SELECT *, CASE WHEN manual_association='other' OR station_excluded THEN 0
            WHEN manual_association='net' AND TRIM(COALESCE(from_call,''))<>'' THEN 1
            WHEN stamp<=0 OR stamp<? OR stamp>? THEN 0
            WHEN source_family='js8' AND NOT ncs_has_payload(COALESCE(NULLIF(body_preview,''),summary,'')) THEN 0
            WHEN radio_id=? AND app_instance_id=? AND TRIM(COALESCE(from_call,''))<>''
                AND ((?<>'' AND (ncs_group(group_name)=? OR ncs_group(to_call)=?))
                    OR (?<>'' AND UPPER(to_call)=?)
                    OR EXISTS(SELECT 1 FROM ncs_workbench_submissions q WHERE q.run_id=?
                        AND q.outcome='accepted' AND q.completed_ts<=stamp
                        AND json_extract(q.action_json,'$.kind')='query'
                        AND json_extract(q.action_json,'$.destination') LIKE '@%'
                        AND ncs_group(json_extract(q.action_json,'$.destination')) IN (ncs_group(group_name),ncs_group(to_call)))) THEN 1 ELSE 0 END AS belongs_to_net
        FROM evidence
    )'''
    group = normalize_group_name(scope.group_name).upper()
    operator = scope.operator_call.strip().upper()
    params = [scope.run_id, scope.run_id, scope.radio_id, scope.instance_id,
              scope.since_ts, anchor, scope.since_ts, anchor, scope.radio_id, scope.instance_id,
              group, group, group, operator, operator, scope.run_id]
    predicate = {'net': 'belongs_to_net=1 AND NOT report_excluded',
                 'other': 'belongs_to_net=0 AND NOT report_excluded',
                 'excluded': '(report_excluded OR station_excluded)'}[view]
    return sql, params, predicate


def _enrich(row):
    result = dict(row)
    payload = SimpleNamespace(**result)
    result['kind_label'] = message_row_kind_label(SimpleNamespace(payload=payload, origin=result['source_family']))
    result['topics'] = _decoded(result.pop('topics_json', ''), [])
    result['entities'] = _decoded(result.pop('entities_json', ''), {})
    # Read compatibility for already-indexed API imports; do not rewrite history.
    if str(result['entities'].get('relay_via') or '').strip().upper() == str(result.get('from_call') or '').strip().upper():
        result['entities']['relay_via'] = ''
    result['intelligence'] = _decoded(result.pop('intelligence_json', ''), {})
    # Projection owns the shared Inbox/map interpretation; this reader never
    # invents a second form decoder or combines reports from different times.
    intelligence = result['intelligence'] if isinstance(result['intelligence'], dict) else {}
    result['intelligence'] = intelligence
    result['traffic_summary'] = str(intelligence.get('summary') or result.get('summary') or result.get('body_preview') or 'Summary unavailable')
    decoded = _decoded(result.pop('decoded_intelligence_json', ''), {})
    if not isinstance(decoded, dict):
        decoded = {}
    result['decoded_summary'] = str(decoded.get('summary') or result.get('decoded_summary') or '')
    result['source_state'] = 'Identified' if result['belongs_to_net'] else 'Other / unassigned'
    if not result.get('radio_id') or not result.get('app_instance_id'):
        result['source_state'] = 'Source not fully identified'
    result['review_state'] = 'Reviewed' if result['reviewed'] else 'New / updated'
    return result


def query_net_reports(db_path, scope: NetReportScope, *, view='net',
                      cursor: ReportCursor | None = None, limit=MAX_PAGE,
                      roster=False, anchor_ts: float | None = None, from_call: str = '') -> NetReportPage:
    """Stable keyset page; unrelated traffic cannot consume the page bound.

    A cursor fixes the reception watermark and query scope. New traffic is
    advertised separately instead of reordering the operator's loaded page.
    """
    context = hashlib.sha256(json.dumps([scope.__dict__, view, bool(roster), from_call.strip().upper()], sort_keys=True).encode()).hexdigest()
    if cursor and cursor.context != context:
        raise ValueError('Report cursor belongs to another net or view')
    anchor = cursor.anchor_ts if cursor else float(anchor_ts if anchor_ts is not None else scope.until_ts or time.time())
    if scope.until_ts:
        anchor = min(anchor, scope.until_ts)
    bound = min(MAX_PAGE, max(1, int(limit)))
    conn = connect_sqlite_readonly(db_path)
    try:
        sql, params, predicate = _prepare(conn, scope, view, anchor)
        if from_call:
            predicate += ' AND UPPER(from_call)=?'
            params.append(from_call.strip().upper())
        count = conn.execute(sql + ' SELECT COUNT(DISTINCT UPPER(from_call)) FROM associated WHERE ' + predicate if roster else
                             sql + ' SELECT COUNT(*) FROM associated WHERE ' + predicate, params).fetchone()[0]
        if roster:
            sql += ''', ranked AS (SELECT *, ROW_NUMBER() OVER
                (PARTITION BY UPPER(from_call) ORDER BY stamp DESC,message_id DESC) AS station_rank
                FROM associated WHERE ''' + predicate + ')'
            selection, where = 'ranked', 'station_rank=1'
        else:
            selection, where = 'associated', predicate
        paging = ''
        if cursor:
            paging = ' AND (stamp,message_id)<(?,?)'
            params += [cursor.stamp, cursor.message_id]
        # sqlite rows are detached before closing the worker-owned connection.
        conn.row_factory = __import__('sqlite3').Row
        rows = conn.execute(sql + f' SELECT * FROM {selection} WHERE {where}{paging} ORDER BY stamp DESC,message_id DESC LIMIT ?',
                            (*params, bound + 1)).fetchall()
        more = len(rows) > bound
        items = [_enrich(row) for row in rows[:bound]]
        _attach_prior_followups(conn, scope, items)
        # Same query/filter contract, with a later watermark; no automatic adoption.
        newer_sql, newer_params, newer_where = _prepare(conn, scope, view, scope.until_ts or time.time())
        if from_call:
            newer_where += ' AND UPPER(from_call)=?'
            newer_params.append(from_call.strip().upper())
        newer = bool(conn.execute(newer_sql + ' SELECT 1 FROM associated WHERE ' + newer_where + ' AND stamp>? LIMIT 1',
                                  (*newer_params, anchor)).fetchone())
        next_cursor = ReportCursor(context, anchor, items[-1]['stamp'], items[-1]['message_id']) if more and items else None
        return NetReportPage(tuple(items), next_cursor, anchor, int(count), newer)
    finally:
        conn.close()


def _attach_prior_followups(conn, scope, items):
    if items:
        calls = tuple(dict.fromkeys(str(item['from_call']).upper() for item in items))
        marks = ','.join('?' for _ in calls)
        unresolved = dict(conn.execute(f"""SELECT UPPER(p.from_call),COUNT(*)
            FROM ncs_workbench_annotations a
            JOIN message_projection p ON p.canonical_key=a.entity_id
            WHERE a.run_id=? AND a.kind='report' AND UPPER(p.from_call) IN ({marks})
            AND json_extract(a.values_json,'$.follow_up')=1 GROUP BY UPPER(p.from_call)""",
            (scope.run_id, *calls)).fetchall())
        for item in items:
            item['older_follow_up'] = max(0, unresolved.get(str(item['from_call']).upper(), 0) - int(bool(item.get('follow_up'))))


def query_net_roster(db_path, scope: NetReportScope, *, view='net', after='', limit=MAX_PAGE,
                     anchor_ts=None, callsign_filter='', group_filter='', state_filter='', region_filter='', facets=None, identity_store=None):
    """Independent callsign-keyset roster, including retained receiver metadata.

    A report exclusion removes that report, not participation already recorded.
    Receiver-only entries explicitly identify their limited evidence.
    """
    conn = connect_sqlite_readonly(db_path)
    try:
        anchor = float(anchor_ts if anchor_ts is not None else scope.until_ts or time.time())
        sql, params, predicate = _prepare(conn, scope, view, anchor)
        sql += ''', ranked AS (
            SELECT *, COUNT(*) OVER(PARTITION BY UPPER(from_call)) AS traffic_count,
                ROW_NUMBER() OVER(PARTITION BY UPPER(from_call)
                ORDER BY stamp DESC,message_id DESC) AS station_rank
            FROM associated WHERE ''' + predicate + ''' AND TRIM(COALESCE(from_call,''))<>''
        ), decoded_ranked AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY UPPER(from_call)
                ORDER BY stamp DESC,message_id DESC) AS decoded_rank
            FROM ranked WHERE source_family IN ('spotter','commstat')
                OR LOWER(json_extract(intelligence_json,'$.source_type')) IN ('spotter','commstat')
        ), observed AS (
            SELECT o.callsign, o.values_json,
                COALESCE(json_extract(a.values_json,'$.excluded'),0) AS station_excluded,
                COALESCE(a.version,0) AS station_version,
                COALESCE(json_extract(a.values_json,'$.note'),'') AS station_note
            FROM ncs_workbench_stations o
            LEFT JOIN ncs_workbench_annotations a ON a.run_id=o.run_id AND a.kind='station'
                AND a.entity_id=o.callsign AND a.evidence_version='station'
            WHERE o.run_id=?
        ), identities AS (
            SELECT UPPER(from_call) AS callsign FROM ranked WHERE station_rank=1
            UNION SELECT callsign FROM observed WHERE ''' + ('NOT station_excluded' if view == 'net' else 'station_excluded' if view == 'excluded' else '0') + '''
        )'''
        params.append(scope.run_id)
        from freqinout.core.operator_groups import _row_groups
        conn.create_function('ncs_roster_groups', 4,
            lambda raw, g1, g2, g3: json.dumps(sorted(_row_groups(
                dict(groups_json=raw, group1=g1, group2=g2, group3=g3)))), deterministic=True)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(operator_checkins)')}
        if 'callsign' in columns:
            field = lambda key: 'COALESCE(' + key + ", '')" if key in columns else "''"
            metadata = ("SELECT UPPER(TRIM(callsign)) AS roster_call, " +
                'ncs_roster_groups(' + ','.join(field(key) for key in ('groups_json', 'group1', 'group2', 'group3')) +
                ') AS roster_groups, UPPER(TRIM(' + field('state') + ')) AS roster_state, ' + field('name') + ' AS roster_name FROM operator_checkins')
        else:
            metadata = "SELECT '' AS roster_call, '[]' AS roster_groups, '' AS roster_state, '' AS roster_name WHERE 0"
        sql += ', roster_metadata AS (' + metadata + ')'
        fallback = {}
        if identity_store is not None:
            from freqinout.core.call_finder import EXACT_BATCH_LIMIT
            cursor = conn.execute(sql + ''' SELECT i.callsign FROM identities i
                LEFT JOIN roster_metadata m ON m.roster_call=i.callsign
                WHERE TRIM(COALESCE(m.roster_state,''))='' OR TRIM(COALESCE(m.roster_name,''))='' ''', params)
            while batch := cursor.fetchmany(EXACT_BATCH_LIMIT):
                calls = tuple(dict.fromkeys(str(row[0]).split('/', 1)[0] for row in batch))
                try:
                    identity_store.exact_many(calls)
                except Exception:
                    continue
                for call in calls:
                    records = identity_store.cached_exact(call)
                    if records is not None and len(records) == 1:
                        record = records[0]
                        name = (' '.join(part for part in (record.first_name, record.last_name) if part)
                                if record.kind == 'Individual' else '') or record.name
                        fallback[call] = (name, record.state)
        conn.create_function('ncs_identity_name', 1, lambda call: fallback.get(str(call).split('/', 1)[0], ('', ''))[0], deterministic=True)
        conn.create_function('ncs_identity_state', 1, lambda call: fallback.get(str(call).split('/', 1)[0], ('', ''))[1], deterministic=True)
        from freqinout.core.regional_intelligence import STATE_TO_FEMA_REGION
        conn.create_function('ncs_region', 1, lambda state: STATE_TO_FEMA_REGION.get(str(state or '').strip().upper(), ''), deterministic=True)
        sql += ''', identities_with_roster AS (
            SELECT i.callsign, COALESCE(m.roster_groups,'[]') AS roster_groups,
                   COALESCE(NULLIF(m.roster_state,''),ncs_identity_state(i.callsign),'') AS roster_state,
                   ncs_region(COALESCE(NULLIF(m.roster_state,''),ncs_identity_state(i.callsign),'')) AS roster_region,
                   COALESCE(NULLIF(m.roster_name,''),ncs_identity_name(i.callsign),'') AS roster_name,
                   CASE WHEN COALESCE(m.roster_state,'')<>'' THEN 'HF Callsigns roster'
                        WHEN ncs_identity_state(i.callsign)<>'' THEN 'Call Finder mailing state; not a confirmed operating location'
                        ELSE 'State unknown' END AS roster_state_source
            FROM identities i LEFT JOIN roster_metadata m ON m.roster_call=i.callsign
        )'''
        if facets is not None:
            facets['groups'] = [row[0] for row in conn.execute(sql +
                ' SELECT DISTINCT j.value FROM identities_with_roster,json_each(roster_groups) j ORDER BY j.value', params)]
            facets['states'] = [row[0] for row in conn.execute(sql +
                ' SELECT DISTINCT roster_state FROM identities_with_roster ORDER BY roster_state', params)]
            facets['regions'] = [row[0] for row in conn.execute(sql +
                ' SELECT DISTINCT roster_region FROM identities_with_roster ORDER BY roster_region', params)]
        sql += ''', filtered_identities AS (SELECT * FROM identities_with_roster
            WHERE (?='' OR INSTR(callsign,?)>0)
              AND (?='' OR EXISTS(SELECT 1 FROM json_each(roster_groups) WHERE value=?))
              AND (?='' OR roster_state=CASE WHEN ?='__UNKNOWN__' THEN '' ELSE ? END)
              AND (?='' OR roster_region=CASE WHEN ?='__UNKNOWN__' THEN '' ELSE ? END))'''
        search = str(callsign_filter or '').strip().upper()
        group, state = normalize_group_name(group_filter), str(state_filter or '').strip().upper()
        region = str(region_filter or '').strip().upper()
        params.extend((search, search, group, group, state, state, state, region, region, region))
        total = conn.execute(sql + ' SELECT COUNT(*) FROM filtered_identities', params).fetchone()[0]
        if facets is not None:
            facets['total'] = conn.execute(sql + ' SELECT COUNT(*) FROM identities_with_roster', params).fetchone()[0] if search or group or state or region else total
            counts = conn.execute(sql + ''' SELECT COUNT(*),COALESCE(SUM(NOT reviewed),0),
                COUNT(DISTINCT CASE WHEN follow_up THEN UPPER(from_call) END)
                FROM ranked JOIN filtered_identities i ON UPPER(ranked.from_call)=i.callsign''', params).fetchone()
            facets.update(traffic_total=int(counts[0]), unreviewed=int(counts[1]), follow_up_stations=int(counts[2]))
        conn.row_factory = __import__('sqlite3').Row
        rows = conn.execute(sql + ''' SELECT i.callsign, i.roster_groups,i.roster_state,i.roster_region,i.roster_name,i.roster_state_source, r.*,
            d.summary AS decoded_summary,d.intelligence_json AS decoded_intelligence_json,
            d.stamp AS decoded_stamp,d.message_id AS decoded_message_id,o.values_json AS observation_json,
            CASE WHEN TRIM(COALESCE(json_extract(r.entities_json,'$.relay_via'),''))<>''
                AND UPPER(TRIM(json_extract(r.entities_json,'$.relay_via')))<>UPPER(i.callsign)
                THEN relay_observation.values_json ELSE o.values_json END AS rx_observation_json,
            o.station_excluded AS observed_excluded,o.station_version AS observed_version,
            o.station_note AS observed_note FROM filtered_identities i
            LEFT JOIN ranked r ON UPPER(r.from_call)=i.callsign AND r.station_rank=1
            LEFT JOIN decoded_ranked d ON UPPER(d.from_call)=i.callsign AND d.decoded_rank=1
            LEFT JOIN observed o ON o.callsign=i.callsign
            LEFT JOIN observed relay_observation ON relay_observation.callsign=
                UPPER(TRIM(json_extract(r.entities_json,'$.relay_via')))
            WHERE i.callsign>? ORDER BY i.callsign LIMIT ?''',
            (*params, str(after).upper(), min(MAX_PAGE, max(1, int(limit))) + 1)).fetchall()
        bound = min(MAX_PAGE, max(1, int(limit)))
        result = []
        for row in rows[:bound]:
            if row['message_id']:
                result.append(_enrich(row))
            else:
                data = _decoded(row['observation_json'], {})
                result.append(dict(from_call=row['callsign'], kind_label='Receiver traffic',
                    summary=str(data.get('status') or 'Received traffic'), topics=[],
                    station_excluded=row['observed_excluded'] or 0,
                    station_version=row['observed_version'] or 0, station_note=row['observed_note'] or '',
                    source_state='Pinned receiver metadata · full report not yet indexed',
                    **{key: data.get(key) for key in ('name', 'state', 'grid', 'mode', 'snr', 'offset')}))
        for item, source_row in zip(result, rows[:bound]):
            observation = _decoded(source_row['rx_observation_json'], {})
            try:
                snr = float(observation.get('snr'))
                if not math.isfinite(snr):
                    snr = None
            except (TypeError, ValueError, OverflowError):
                snr = None
            relay = str((item.get('entities') or {}).get('relay_via') or '').strip().upper()
            item['rx_snr'] = snr
            item['rx_snr_sender'] = relay or item['from_call']
            item['rx_snr_received_ts'] = observation.get('snr_received_ts') if snr is not None else None
            item['roster_groups'] = _decoded(source_row['roster_groups'], [])
            item['roster_state'] = source_row['roster_state']
            item['roster_region'] = source_row['roster_region']
            item['traffic_count'] = int(source_row['traffic_count'] or 0)
            item['roster_name'] = source_row['roster_name']
            item['roster_state_source'] = source_row['roster_state_source']
        _attach_prior_followups(conn, scope, result)
        return tuple(result), rows[bound-1]['callsign'] if len(rows) > bound else None, int(total)
    finally:
        conn.close()


def query_net_prior_work(db_path, run_id: str, *, from_call='', after=('', ''), limit=MAX_PAGE):
    """Retained annotation versions, scoped before paging; no original body claim."""
    conn = connect_sqlite_readonly(db_path)
    conn.row_factory = __import__('sqlite3').Row
    try:
        bound = min(MAX_PAGE, max(1, int(limit)))
        rows = conn.execute('''SELECT a.*,p.from_call,p.message_id,p.content_hash AS current_content_hash,
            p.deleted AS source_deleted FROM ncs_workbench_annotations a
            LEFT JOIN message_projection p ON p.canonical_key=a.entity_id
            WHERE a.run_id=? AND a.kind='report'
              AND (?='' OR UPPER(p.from_call)=?)
              AND (a.entity_id,a.evidence_version)>(?,?)
            ORDER BY a.entity_id,a.evidence_version LIMIT ?''',
            (run_id, str(from_call).strip().upper(), str(from_call).strip().upper(), *after, bound + 1)).fetchall()
        result = []
        for row in rows[:bound]:
            item = dict(row)
            item['values'] = _decoded(item.pop('values_json'), {})
            item['source_state'] = 'Source unavailable' if not item['message_id'] or item['source_deleted'] else 'Source retained'
            if item['source_deleted']:
                item['current_content_hash'] = ''
            result.append(item)
        cursor = (result[-1]['entity_id'], result[-1]['evidence_version']) if len(rows) > bound else None
        return tuple(result), cursor
    finally:
        conn.close()


def query_net_summary(db_path, scope, *, view='net', anchor_ts=None):
    """Complete scoped counts with a bounded topic payload; no UI-side scans."""
    conn = connect_sqlite_readonly(db_path)
    try:
        sql, params, predicate = _prepare(conn, scope, view, float(anchor_ts or scope.until_ts or time.time()))
        count, unread = conn.execute(sql + ' SELECT COUNT(*),COALESCE(SUM(NOT reviewed),0) FROM associated WHERE ' + predicate, params).fetchone()
        followups = conn.execute(sql + ''' SELECT COUNT(DISTINCT UPPER(p.from_call))
            FROM ncs_workbench_annotations a JOIN message_projection p ON p.canonical_key=a.entity_id
            WHERE a.run_id=? AND a.kind='report' AND json_extract(a.values_json,'$.follow_up')=1
            AND EXISTS(SELECT 1 FROM associated e WHERE ''' + predicate + ''' AND UPPER(e.from_call)=UPPER(p.from_call))''',
            (*params, scope.run_id)).fetchone()[0]
        topic_sql = sql + ''', topic_items AS (
            SELECT canonical_key,CASE WHEN j.type='text' THEN j.value
                WHEN j.type='object' THEN COALESCE(json_extract(j.value,'$.label'),json_extract(j.value,'$.name'),'Unknown topic')
                ELSE 'Unknown topic' END AS topic
            FROM associated, json_each(CASE WHEN json_valid(topics_json) THEN topics_json ELSE '[]' END) j
            WHERE ''' + predicate + '''
        ), topic_counts AS (SELECT topic,COUNT(DISTINCT canonical_key) AS items FROM topic_items GROUP BY topic)'''
        total_topics = conn.execute(topic_sql + ' SELECT COUNT(*) FROM topic_counts', params).fetchone()[0]
        topics = conn.execute(topic_sql + ' SELECT topic,items FROM topic_counts ORDER BY items DESC,topic LIMIT 12', params).fetchall()
        return dict(items=int(count), unreviewed=int(unread), follow_up_stations=int(followups),
                    topics=[dict(topic=row[0], items=int(row[1])) for row in topics], other_topics=max(0, total_topics-len(topics)))
    finally:
        conn.close()
