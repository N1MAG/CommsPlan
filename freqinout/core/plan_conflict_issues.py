"""Pure issue vocabulary shared by scheduling validators and guided repair UIs.

Severity is supplied by the owning rule. These records explain decisions; they
never grant permission, acknowledge a block, or mutate a radio/source/policy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


def window_context(row):
    fields = ('source_key','source_row_id','source_table','source','group_name','band','frequency','mode',
              'day_utc','start_utc','end_utc','recurrence','month_weeks','biweekly_offset_weeks','sop_profile_id','sop_layer_id','resource_id')
    result = {key: deepcopy(row[key]) for key in fields if row.get(key) not in (None, '')}
    for target,aliases in {'source_key':('_source_key',),'source_row_id':('_source_row_id',),
                           'sop_profile_id':('_sop_profile_id',),'sop_layer_id':('_sop_layer_id','layer_id')}.items():
        if not result.get(target):
            value=next((row[key] for key in aliases if row.get(key) not in (None,'')),None)
            if value is not None:result[target]=deepcopy(value)
    return result


def source_ref_for_window(plan, row=None):
    row = row or {}
    refs = plan.get('source_refs', plan.get('source_refs_json', []))
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except (TypeError, ValueError):
            refs = []
    source = str(row.get('source') or 'HF').upper()
    prefix = {'HF':'hf_daily_schedule:', 'DAILY':'hf_daily_schedule:', 'NET':'hf_net_schedule:',
              'SOP':'sop_schedule_layer:'}.get(source, '')
    profile_id=row.get('sop_profile_id') or row.get('_sop_profile_id')
    if source == 'SOP' and profile_id:
        return 'sop_schedule_layer:' + str(profile_id)
    matches=[ref for ref in refs if isinstance(ref,str) and prefix and ref.startswith(prefix)]
    if not matches and source=='SOP':matches=[ref for ref in refs if isinstance(ref,str) and ref.startswith('sop_schedule_plan:')]
    return matches[0] if len(set(matches))==1 else ''


def repair(label, target, *, radio_id=0, plan_id=0, instructions='', **context):
    return {'label': str(label), 'instructions': str(instructions),
            'target': {'target': target, 'device_profile_id': int(radio_id or 0),
                       'frequency_plan_id': int(plan_id or 0), **deepcopy(context)}}


def issue(code, category, message, *, severity='blocked', radio_id=0, plan_id=0,
          impact='', instructions='', remedies=(), window=None, resource=None,
          related_radios=(), evidence=None, **context):
    severity = severity if severity in {'blocked','warning','info'} else 'blocked'
    payload = {'code': code, 'category': category, 'severity': severity, 'message': str(message),
        'radio_id': int(radio_id or 0), 'device_profile_id': int(radio_id or 0),
        'frequency_plan_id': int(plan_id or 0), 'impact': str(impact), 'instructions': str(instructions),
        'remedies': deepcopy(list(remedies)), 'window': deepcopy(window or {}),
        'resource': deepcopy(resource or {}), 'related_radios': deepcopy(list(related_radios)),
        'evidence': deepcopy(evidence or {}), **context}
    identity = {k: payload[k] for k in ('code','category','radio_id','frequency_plan_id','window','resource','related_radios')}
    payload['id'] = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return payload


def source_issue(code, message, profile, plan, *, row=None, impact='', instructions=''):
    window = window_context(row or {})
    if window.get('start_utc') and window.get('end_utc'):
        window['label']=f"{window.get('day_utc') or 'ALL'} {window['start_utc']}–{window['end_utc']} UTC"
    ref = source_ref_for_window(plan, row)
    instruction = instructions or 'Open the named source, correct this entry and save it. Return here and recheck the assignment.'
    return issue(code, 'schedule', message, radio_id=profile.get('id'), plan_id=plan.get('id'),
        source_ref=ref, window=window, scope_label=' · '.join(str(v) for v in (profile.get('name'),plan.get('name')) if v), impact=impact or 'This radio cannot follow an ambiguous or invalid schedule.',
        instructions=instruction, remedies=[repair('Edit affected schedule…', 'source',
            radio_id=profile.get('id'), plan_id=plan.get('id'), source_ref=ref, window=window, instructions=instruction)])


def compatibility_issues(device, plan):
    raw=plan.get('receive_only')
    try:
        receive_only=bool(int(raw)) if isinstance(raw,(bool,int,float)) else str(raw or '').strip().lower() in {'1','true','yes','on'}
    except (ValueError,OverflowError):
        receive_only=False
    if str(device.get('device_class') or '').strip().lower() != 'observer' or receive_only:
        return []
    return [issue('capability.receive_only', 'capability',
        f'{device.get("name") or "This receiver"} is receive-only, but {plan.get("name") or "this plan"} is not a receive-only plan.',
        radio_id=device.get('id'), plan_id=plan.get('id'),
        impact='A receiver must not be assigned a plan intended for transmit-capable operation.',
        instructions='Choose a receive-only plan or a compatible radio. Verify the radio role in Configuration if it is incorrect.',
        remedies=[repair('Choose another plan or radio…','assignment_choices',radio_id=device.get('id'),plan_id=plan.get('id'),field='plan'),
                  repair('Review radio role…','radio',radio_id=device.get('id'),plan_id=plan.get('id'),field='device_class')])]


def rf_issue(event, device, plan, peer=None, peer_plan=None, *, windows=(), peer_windows=(), frequencies=(), peer_frequencies=()):
    peer, peer_plan = peer or {}, peer_plan or {}
    kind = event['event_type']
    antenna = kind == 'antenna_band_support'
    field = {'antenna_band_support':'antenna_supported_bands','prevent_band_overlap':'band_overlap_guard_group',
             'advanced_frequency_guard':'advanced_frequency_guard_group'}[kind]
    instruction = ('Choose a supported band or plan. If the antenna information is wrong, verify the physical antenna path before correcting Configuration.' if antenna else
        'Separate the affected schedule windows or choose compatible radios/plans. Review the named RF resource if its saved configuration is incorrect; confirm physical isolation before operation.')
    def source_repairs(affected_plan, radio):
        raw=affected_plan.get('schedule_refs',affected_plan.get('schedule_refs_json',[]))
        if isinstance(raw,str):
            try:raw=json.loads(raw)
            except (ValueError,TypeError):raw=[]
        rows=[row for row in raw if isinstance(row,dict)]
        band=str(event.get('band') or '').upper()
        affected=[row for row in rows if not band or not row.get('band') or str(row['band']).upper()==band] or rows
        owners={}
        for row in affected:
            owners.setdefault(source_ref_for_window(affected_plan,row),[]).append(row)
        if not owners:owners['']=[]
        result=[]
        for ref,entries in owners.items():
            owner=('HF Daily' if ref.startswith('hf_daily_schedule:') else 'HF Net' if ref.startswith('hf_net_schedule:') else 'SOP' if ref.startswith('sop_') else 'plan')
            window=window_context(entries[0]) if len(entries)==1 else {'band':band,'windows':[window_context(row) for row in entries[:40]]}
            result.append(repair('Edit '+str(affected_plan.get('name') or 'plan')+' '+owner+' windows…','source',
                radio_id=radio.get('id'),plan_id=affected_plan.get('id'),source_ref=ref,window=window,instructions=instruction))
        return result
    remedies=source_repairs(plan,device)
    if peer_plan:remedies.extend(source_repairs(peer_plan,peer))
    remedies.extend([repair('Choose another plan or radio…','assignment_choices',radio_id=device.get('id'),plan_id=plan.get('id'),field='plan'),
        repair('Review ' + str(device.get('name') or 'radio') + ' RF configuration…','rf_guard',
            radio_id=device.get('id'),plan_id=plan.get('id'),field=field, instructions='Verify the real resource configuration. RF Guard must not be disabled or weakened to clear a conflict.')])
    def describe(window):
        def time(value):
            return f'{int(value)//60:02d}:{int(value)%60:02d}'
        return f"{window.get('day_utc') or 'ALL'} {time(window['start_minute'])}–{time(window['end_minute'])} UTC" + (' (overnight)' if window['end_minute'] <= window['start_minute'] else '')
    relevant = [w for w in windows if not event.get('band') or w['band'] == event['band']]
    window_label = '; '.join(describe(w) for w in relevant[:4]) or 'No complete time windows; configured guard uses a conservative band/frequency check.'
    if len(relevant)>4:
        window_label += f'; {len(relevant)-4} more windows in details'
    threshold=max(int(device.get('advanced_frequency_guard_window_hz') or 0),int(peer.get('advanced_frequency_guard_window_hz') or 0))
    message=event['message']
    if kind == 'advanced_frequency_guard':
        left=int(event.get('frequency') or 0)
        right=next((int(value) for value in peer_frequencies if abs(left-int(value))<=threshold),None)
        if right is not None:
            from freqinout.core.operating_group_frequency import format_frequency_mhz
            message=(f'{device.get("name") or "Radio"} at {format_frequency_mhz(left/1_000_000)} MHz and '
                f'{peer.get("name") or "other radio"} at {format_frequency_mhz(right/1_000_000)} MHz are '
                f'within the configured {threshold} Hz protection spacing in {event["guard_name"]}.')
    radios = [{'id': r['id'], 'name': r.get('name') or 'Radio', 'runtime_active': bool(r.get('runtime_active')),
               'enabled': bool(r.get('enabled',1))} for r in (device,peer) if r.get('id')]
    return issue('rf.'+kind, 'rf_guard', message, severity=event['decision'],
        radio_id=device.get('id'),plan_id=plan.get('id'), related_radios=radios,
        resource={'kind':kind,'name':event['guard_name'],'mode':event['guard_mode'],'field':field},
        window={'label':window_label, 'band':event.get('band') or '', 'frequency_hz':event.get('frequency') or '',
                'windows':list(windows), 'peer_windows':list(peer_windows)},
        impact='The configured antenna does not support this planned band.' if antenna else
            'Concurrent operation may cause RF interference or expose a receiver/front end. A stopped radio is not proof of physical isolation.',
        instructions=instruction, remedies=remedies, evidence={
            'rule':kind,'peer_plan_id':peer_plan.get('id'),'frequencies_hz':list(frequencies),
            'peer_frequencies_hz':list(peer_frequencies),'threshold_hz':threshold if kind=='advanced_frequency_guard' else None,
            'scope':'Configured recurring weekday/time envelopes; conservative for bi-weekly/monthly patterns. Runtime resource/PTT checks still apply.'})
