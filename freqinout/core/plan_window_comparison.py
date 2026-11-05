"""Worker-only detached plan window comparison and exact repair targets."""
from collections import Counter
from copy import deepcopy
import json

from freqinout.core.plan_conflict_issues import source_ref_for_window, window_context
from freqinout.core.schedule_source_sets import (
    HF_DAILY_SOURCE_SETS_KEY, HF_NET_SOURCE_SETS_KEY, source_set_row_by_id,
)

MAX_COMPARISON_WINDOWS = 50000
WINDOW_FIELDS = (
    ('day_utc', 'Day'), ('start_utc', 'Start'), ('end_utc', 'End'),
    ('band', 'Band'), ('frequency', 'Frequency'), ('mode', 'Mode'),
    ('recurrence', 'Recurrence'), ('month_weeks', 'Month weeks'),
    ('biweekly_offset_weeks', 'Bi-weekly offset'), ('group_name', 'Group'),
    ('source', 'Source'), ('vfo', 'VFO'),
)


def _rows(value):
    if isinstance(value, str):
        value = json.loads(value or '[]')
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError('Cannot compare unreadable schedule windows. Recheck the plan sources.')
    if len(value) > MAX_COMPARISON_WINDOWS:
        raise ValueError('Too many schedule windows to compare safely. Narrow the plan before reviewing.')
    return deepcopy(value)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _source_names(settings, store, plan):
    refs = plan.get('source_refs_json') or '[]'
    refs = json.loads(refs) if isinstance(refs, str) else refs
    names = {}
    for ref in refs:
        if not isinstance(ref, str) or ':' not in ref:
            continue
        category, identity = ref.split(':', 1)
        label = {'hf_daily_schedule':'HF Daily', 'hf_net_schedule':'HF Nets',
                 'sop_schedule_plan':'SOP plan', 'sop_schedule_layer':'SOP'}.get(category)
        if not label:
            continue
        source = None
        if identity.startswith('plan:') and identity[5:].isdigit():
            source = store.get_frequency_plan(int(identity[5:]))
        elif category == 'sop_schedule_plan' and identity.isdigit():
            source = store.get_frequency_plan(int(identity))
        elif category in {'hf_daily_schedule','hf_net_schedule'}:
            source = source_set_row_by_id(settings, HF_DAILY_SOURCE_SETS_KEY if category == 'hf_daily_schedule' else HF_NET_SOURCE_SETS_KEY, identity)
        name = (source or {}).get('name')
        names[ref] = f'{label} “{name}”' if name else f'{label} source'
    return names


def _identity(row, ref):
    table = str(row.get('source_table') or '').strip()
    row_id = str(row.get('source_row_id') or '').strip()
    key = str(row.get('source_key') or '').strip()
    if table and row_id:
        return (ref, table, row_id)
    if key:
        return (ref, key)
    return None


def _decorate(row, plan, names, radio_id):
    row = deepcopy(row)
    ref = source_ref_for_window(plan, row)
    source = str(row.get('source') or 'HF').upper()
    source_label = names.get(ref) or {'HF':'Daily layer','DAILY':'Daily layer','NET':'Net layer','SOP':'SOP layer'}.get(source, 'Plan window')
    if source == 'SOP' and row.get('profile_name'):
        source_label = f'SOP “{row["profile_name"]}”'
    day = str(row.get('day_utc') or 'ALL')
    if day == 'ALL': day = 'Every day'
    start, end = str(row.get('start_utc') or ''), str(row.get('end_utc') or '')
    overnight = ' (overnight)' if start and end and end <= start else ''
    pieces = [day, f'{start}–{end} UTC{overnight}' if start or end else '', row.get('band'),
              f'{row["frequency"]} MHz' if row.get('frequency') not in (None,'') else '', row.get('mode'), row.get('recurrence')]
    window = window_context(row)
    target = {'target':'source','device_profile_id':int(radio_id or 0),
              'frequency_plan_id':int(plan.get('id') or 0),'source_ref':ref,'source_label':source_label,'window':window}
    return {**row, 'source_label':source_label, 'display':' · '.join(str(value) for value in pieces if value), 'target':target}


def compare_windows(settings, store, plan, left_plan, right_plan, *, kind, radio_id=0):
    """Pair exact rows or unique provenance; never guess matches from appearance."""
    left = _rows(left_plan.get('schedule_refs', left_plan.get('schedule_refs_json', [])))
    right = _rows(right_plan.get('schedule_refs', right_plan.get('schedule_refs_json', [])))
    left_names = _source_names(settings, store, left_plan)
    right_names = _source_names(settings, store, right_plan)
    left_refs = [source_ref_for_window(left_plan,row) for row in left]
    right_refs = [source_ref_for_window(right_plan,row) for row in right]
    left_exact, right_exact = Counter(map(_encode,left)), Counter(map(_encode,right))
    common = left_exact & right_exact
    def remaining(rows, common_counts):
        left_count = common_counts.copy()
        out=[]
        for i,row in enumerate(rows):
            encoded=_encode(row)
            if left_count[encoded]: left_count[encoded]-=1
            else: out.append(i)
        return out
    li, ri = remaining(left,common), remaining(right,common)
    left_ids = [_identity(left[i],left_refs[i]) for i in li]
    right_ids = [_identity(right[i],right_refs[i]) for i in ri]
    lc=Counter(_identity(row, ref) for row, ref in zip(left, left_refs))
    rc=Counter(_identity(row, ref) for row, ref in zip(right, right_refs))
    right_unique={key:index for key,index in zip(right_ids,ri) if key is not None and rc[key]==1}
    matched=set(); result=[]
    for identity,i in zip(left_ids,li):
        a=_decorate(left[i],left_plan,left_names,radio_id)
        if identity is not None and lc[identity]==1 and identity in right_unique:
            j=right_unique[identity];matched.add(j)
            b=_decorate(right[j],right_plan,right_names,radio_id)
            changed=[label for key,label in WINDOW_FIELDS if left[i].get(key)!=right[j].get(key)]
            known={key for key,_ in WINDOW_FIELDS}
            if any(left[i].get(key)!=right[j].get(key) for key in (left[i].keys() | right[j].keys()) - known):
                metadata = [('Source identity' if str(key).endswith(('_id', '_key')) else str(key).replace('_',' ').capitalize()) for key in sorted((left[i].keys() | right[j].keys()) - known) if left[i].get(key) != right[j].get(key)]
                changed.extend(dict.fromkeys(metadata))
            result.append({'change':'Changed','source_label':a['source_label'],'left':a,'right':b,
                           'changed_fields':changed,'target':deepcopy(b['target'] if b['target']['source_ref'] else a['target'])})
        else:
            result.append({'change':'Only in pending changes' if kind=='source_mismatch' else 'Removed',
                           'source_label':a['source_label'],'left':a,'right':{},'changed_fields':[], 'target':deepcopy(a['target'])})
    for j in ri:
        if j in matched: continue
        b=_decorate(right[j],right_plan,right_names,radio_id)
        result.append({'change':'Only in current source' if kind=='source_mismatch' else 'Added',
                       'source_label':b['source_label'],'left':{},'right':b,'changed_fields':[], 'target':deepcopy(b['target'])})
    for key,label in (('frequency_refs','Frequency references'),('group_refs','Group references'),('source_refs','Source references')):
        a=left_plan.get(key,left_plan.get(key+'_json',[]));b=right_plan.get(key,right_plan.get(key+'_json',[]))
        if isinstance(a,str): a=json.loads(a or '[]')
        if isinstance(b,str): b=json.loads(b or '[]')
        if a!=b:
            left_values = [left_names.get(str(value), 'Unavailable source') for value in a] if key == 'source_refs' else list(map(str,a))
            right_values = [right_names.get(str(value), 'Unavailable source') for value in b] if key == 'source_refs' else list(map(str,b))
            result.append({'change':'Changed','source_label':'Plan references','left':{'display':', '.join(left_values) or 'None'},
                'right':{'display':', '.join(right_values) or 'None'},'changed_fields':[label],'target':{}})
    if left != right and left_exact == right_exact:
        result.append({'change':'Changed','source_label':'Plan window order', 'left':{'display':'Saved order'},
                       'right':{'display':'Proposed order'},'changed_fields':['Window ordering'],'target':{}})
    return result, [_decorate(row,right_plan,right_names,radio_id) for row in right]
