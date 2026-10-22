"""Transaction-counted schedule dependencies; traffic does not advance them.

Installed by startup/configuration workers. Command workers read only selected
indexed counters, so source edits in another CP process fence queued work too.
"""
from pathlib import Path
import json
import sqlite3
from freqinout.core.sqlite_utils import connect_sqlite_readonly

TABLE = '_cp_schedule_source_revision'
IMPLEMENTATION_VERSION = 3
SOURCE_TABLES = ('daily_schedule_tab','net_schedule_tab','net_schedule','sop_schedule_layer',
                 'sop_profiles','sop_net_conflict_policy','net_resources','frequency_plans')


def install_source_revisions(conn):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE}(source_key TEXT PRIMARY KEY,revision INTEGER NOT NULL)')
    def bump(key):
        return f"INSERT INTO {TABLE}(source_key,revision) VALUES({key},1) ON CONFLICT(source_key) DO UPDATE SET revision=revision+1;"
    def put_trigger(name, definition):
        existing=conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        if existing is not None and existing[0] != definition:
            conn.execute(f'DROP TRIGGER {name}')
            existing=None
        if existing is None:
            conn.execute(definition)

    existing={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in SOURCE_TABLES:
        if table not in existing:
            continue
        columns=[row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
        fields=[col for col in columns if col not in {'id','name','notes','description','created_utc'} and (col!='updated_utc' or table=='sop_profiles')]
        if table == 'frequency_plans':
            fields=[col for col in fields if col != 'name']
        for event in ('INSERT','UPDATE','DELETE'):
            ref='OLD' if event=='DELETE' else 'NEW'
            condition=' WHEN ' + ' OR '.join(f'OLD."{col}" IS NOT NEW."{col}"' for col in fields) if event=='UPDATE' and fields else ''
            sql=bump(f"'table:{table}'")
            if table == 'frequency_plans':
                sql+=bump(f"'plan:' || {ref}.id")
            if table in {'sop_profiles','sop_schedule_layer'}:
                column='id' if table=='sop_profiles' else 'profile_id'
                sql+=bump(f"'sop:' || {ref}.{column}")
                if event=='UPDATE' and table=='sop_schedule_layer':
                    sql+=bump("'sop:' || OLD.profile_id")
            name=f'ocs_source_{table}_{event.lower()}'
            put_trigger(name,f'CREATE TRIGGER {name} AFTER {event} ON {table}{condition} BEGIN {sql} END')
    if 'kv' in existing:
        for key in ('sop_schedule_layer_enabled','sop_frequency_variant_references'):
            for event in ('INSERT','UPDATE','DELETE'):
                ref='OLD' if event=='DELETE' else 'NEW'
                changed=' AND OLD.value IS NOT NEW.value' if event=='UPDATE' else ''
                put_trigger(f'ocs_source_kv_{key}_{event.lower()}',f"CREATE TRIGGER ocs_source_kv_{key}_{event.lower()} AFTER {event} ON kv WHEN {ref}.key='{key}'{changed} BEGIN {bump(repr('kv:'+key))} END")
        for event in ('INSERT','UPDATE','DELETE'):
            ref='OLD' if event=='DELETE' else 'NEW'
            def conditions(ref):
                projection="json_array(json_extract(CASE WHEN json_valid(value) THEN value ELSE '{}' END,'$.group'),json_extract(CASE WHEN json_valid(value) THEN value ELSE '{}' END,'$.use_condition_levels'),json_extract(CASE WHEN json_valid(value) THEN value ELSE '{}' END,'$.condition_level'))"
                return f"(SELECT json_group_array(v) FROM (SELECT DISTINCT {projection} AS v FROM json_each(CASE WHEN json_valid({ref}.value) THEN {ref}.value ELSE '[]' END) ORDER BY v))"
            changed=f' AND {conditions("OLD")} IS NOT {conditions("NEW")}' if event=='UPDATE' else ''
            put_trigger(f'ocs_source_group_conditions_{event.lower()}',f"CREATE TRIGGER ocs_source_group_conditions_{event.lower()} AFTER {event} ON kv WHEN {ref}.key='operating_groups'{changed} BEGIN {bump(repr('kv:group_conditions'))} END")
        for event in ('INSERT','UPDATE','DELETE'):
            refs=('NEW',) if event=='INSERT' else ('OLD',) if event=='DELETE' else ('OLD','NEW')
            # One counter per referenced logical group. Stored frequency and
            # mode variants are command inputs; unrelated groups remain valid.
            statements=[]
            for ref in refs:
                source=f"json_each(CASE WHEN json_valid({ref}.value) THEN {ref}.value ELSE '[]' END)"
                name="UPPER(TRIM(COALESCE(json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{}' END,'$.group'),'')))"
                def command_values(alias):
                    fields=('group','band','mode','frequency','vfo','primary_js8call_group','fldigi_mode','fldigi_offset','operating_group_key','operating_group_frequency_key')
                    obj=f"CASE WHEN {alias}.type='object' THEN {alias}.value ELSE '{{}}' END"
                    return 'json_array('+','.join(f"json_extract({obj},'$.{field}')" for field in fields)+')'
                changed=''
                if event=='UPDATE':
                    other='NEW' if ref=='OLD' else 'OLD'
                    other_source=f"json_each(CASE WHEN json_valid({other}.value) THEN {other}.value ELSE '[]' END)"
                    changed=f' AND NOT EXISTS (SELECT 1 FROM {other_source} previous WHERE {command_values("previous")} IS {command_values("item")})'
                statements.append(f"INSERT INTO {TABLE}(source_key,revision) SELECT 'group:' || {name},1 FROM {source} item WHERE {name}<>''{changed} ON CONFLICT(source_key) DO UPDATE SET revision=revision+1;")
            ref='OLD' if event=='DELETE' else 'NEW'
            changed=' AND OLD.value IS NOT NEW.value' if event=='UPDATE' else ''
            put_trigger(f'ocs_source_group_variants_{event.lower()}',f"CREATE TRIGGER ocs_source_group_variants_{event.lower()} AFTER {event} ON kv WHEN {ref}.key='operating_groups'{changed} BEGIN {''.join(statements)} END")
        for category,key in (('hf_daily_schedule','freqplanner_hf_daily_schedule_sets'),('hf_net_schedule','freqplanner_hf_net_schedule_sets')):
            for event in ('INSERT','UPDATE','DELETE'):
                ref='OLD' if event=='DELETE' else 'NEW'
                # Named sets are independent dependencies. Editing another
                # set's label or rows does not revoke this set's approval.
                source=f"json_each(CASE WHEN json_valid({ref}.value) THEN {ref}.value ELSE '[]' END)"
                old="json_each(CASE WHEN json_valid(OLD.value) THEN OLD.value ELSE '[]' END)" if event=='UPDATE' else None
                changed=(f" WHERE NOT EXISTS (SELECT 1 FROM {old} previous WHERE json_extract(CASE WHEN previous.type='object' THEN previous.value ELSE '{{}}' END,'$.id')=json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id') AND json_extract(CASE WHEN previous.type='object' THEN previous.value ELSE '{{}}' END,'$.rows') IS json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.rows'))" if old else '')
                sql=f"INSERT INTO {TABLE}(source_key,revision) SELECT 'set:{category}:' || json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id'),1 FROM {source} item{changed} AND json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id') IS NOT NULL" if changed else f"INSERT INTO {TABLE}(source_key,revision) SELECT 'set:{category}:' || json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id'),1 FROM {source} item WHERE json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id') IS NOT NULL"
                sql+=' ON CONFLICT(source_key) DO UPDATE SET revision=revision+1;'
                if event=='UPDATE':
                    sql+=f"INSERT INTO {TABLE}(source_key,revision) SELECT 'set:{category}:' || json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id'),1 FROM {old} item WHERE NOT EXISTS (SELECT 1 FROM {source} current WHERE json_extract(CASE WHEN current.type='object' THEN current.value ELSE '{{}}' END,'$.id')=json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{{}}' END,'$.id')) ON CONFLICT(source_key) DO UPDATE SET revision=revision+1;"
                put_trigger(f'ocs_source_sets_{category}_{event.lower()}',f"CREATE TRIGGER ocs_source_sets_{category}_{event.lower()} AFTER {event} ON kv WHEN {ref}.key='{key}' BEGIN {sql} END")
    conn.execute(f"INSERT INTO {TABLE}(source_key,revision) VALUES('__implementation__',?) ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision WHERE revision IS NOT excluded.revision", (IMPLEMENTATION_VERSION,))


def ensure_source_database(path):
    path=Path(path)
    if not path.is_file():
        return
    with connect_sqlite_readonly(path,timeout=.5,busy_timeout_ms=500) as conn:
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        needed={f'ocs_source_{table}_{event}' for table in SOURCE_TABLES if table in tables for event in ('insert','update','delete')}
        if 'kv' in tables:
            needed.update({f'{prefix}_{event}' for prefix in ('ocs_source_sets_hf_daily_schedule','ocs_source_sets_hf_net_schedule',
                'ocs_source_kv_sop_schedule_layer_enabled','ocs_source_kv_sop_frequency_variant_references','ocs_source_group_conditions','ocs_source_group_variants')
                for event in ('insert','update','delete')})
        version=conn.execute(f"SELECT revision FROM {TABLE} WHERE source_key='__implementation__'").fetchone() if TABLE in tables else None
        if version and int(version[0]) == IMPLEMENTATION_VERSION and needed.issubset(triggers):
            return
    with sqlite3.connect(path,timeout=1) as conn:
        install_source_revisions(conn)


def source_tokens(db_path,plan,choices=None):
    """Worker-only bounded dependency snapshot, not a UI or tick operation."""
    db_path=Path(db_path)
    refs=json.loads(plan.get('source_refs_json') or '[]')
    groups=json.loads(plan.get('group_refs_json') or '[]')
    groups=list(groups)+[row.get('group_name') or row.get('group') for row in json.loads(plan.get('schedule_refs_json') or '[]') if isinstance(row,dict)]
    keys={'settings':set(),'nets':set()}
    keys['settings'].update('group:'+str(group).strip().upper() for group in groups if group)
    for ref in refs:
        if not isinstance(ref,str):
            continue
        if ref.startswith('sop_schedule_layer:'):
            keys['nets'].add('sop:'+ref.split(':',1)[1])
        elif ref.startswith(('hf_daily_schedule:','hf_net_schedule:')):
            category,ident=ref.split(':',1)
            if ident=='__live__':
                keys['settings' if category=='hf_daily_schedule' else 'nets'].add('table:daily_schedule_tab' if category=='hf_daily_schedule' else 'table:net_schedule_tab')
            elif ident.startswith('plan:'):
                keys['settings'].add(ident)
            elif ident!='__no_nets__':
                keys['settings'].add('set:'+ref)
        elif ref=='hf_daily':
            keys['settings'].add('table:daily_schedule_tab')
        elif ref=='hf_nets':
            keys['nets'].add('table:net_schedule_tab')
    # Existing scheduler uses all active SOPs unless explicit profile IDs scope
    # it. These are real shared dependencies, not radio ownership transfers.
    if (choices or {}).get('sop_tuning_enabled') is not False:
        keys['settings'].update(('kv:group_conditions','kv:sop_schedule_layer_enabled','kv:sop_frequency_variant_references'))
        if not any(key.startswith('sop:') for key in keys['nets']):
            keys['nets'].update(('table:sop_schedule_layer','table:sop_profiles'))
        keys['nets'].add('table:sop_net_conflict_policy')
    if groups:
        keys['nets'].add('table:net_resources')
    result={}
    for domain,source_keys in keys.items():
        if not source_keys:
            continue
        path=db_path if domain=='settings' else db_path.with_name('freqinout_nets.db')
        if not path.is_file():
            # No SOP database is a known empty dependency. Creating it later
            # changes this stamp and requires review before applying its rows.
            result[domain]={'missing':True}
            continue
        with connect_sqlite_readonly(path,timeout=.5,busy_timeout_ms=500) as conn:
            marks=','.join('?' for _ in source_keys)
            rows=dict(conn.execute(f'SELECT source_key,revision FROM {TABLE} WHERE source_key IN ({marks})',tuple(source_keys)))
            result[domain]={key:int(rows.get(key,0)) for key in sorted(source_keys)}
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            relevant={key.split(':',1)[1] for key in source_keys if key.startswith('table:')}
            if any(key.startswith('sop:') for key in source_keys):
                relevant.update(('sop_profiles','sop_schedule_layer'))
            result[domain]['__tables__']={table:table in tables for table in relevant}
            stat=path.stat()
            result[domain]['__identity__']=[stat.st_dev,stat.st_ino]
    return result


def tokens_current(db_path,tokens):
    for domain,expected in (tokens or {}).items():
        path=Path(db_path) if domain=='settings' else Path(db_path).with_name('freqinout_nets.db')
        if expected.get('missing'):
            if path.exists():
                return False
            continue
        with connect_sqlite_readonly(path,timeout=.25,busy_timeout_ms=250) as conn:
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any((table in tables) != present for table,present in expected.get('__tables__',{}).items()):
                return False
            stat=path.stat()
            if [stat.st_dev,stat.st_ino] != expected.get('__identity__'):
                return False
            keys=tuple(key for key in expected if not key.startswith('__'))
            if not keys:
                continue
            rows=dict(conn.execute(f"SELECT source_key,revision FROM {TABLE} WHERE source_key IN ({','.join('?' for _ in keys)})",keys))
            if any(int(rows.get(key,0))!=expected[key] for key in keys):
                return False
    return True
