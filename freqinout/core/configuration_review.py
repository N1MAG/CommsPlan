"""Pure, explicit conflict review; never publishes or waives save validation."""
from copy import deepcopy
from freqinout.core.operator_radio_configuration import RADIO_FIELDS

_CONTEXT = ('expected_identity_generation', 'expected_current_instance_id',
            'replacement_instance_id', 'operator_launch_snapshot', 'operator_draft_revision')


def _changes(base, values):
    marked = values.get('__edited_fields__')
    edited = set(marked) if isinstance(marked, (list, tuple)) else None
    return {key: deepcopy(value) for key, value in values.items()
            if key not in _CONTEXT and not key.startswith('_')
            and not key.startswith('operator_')
            and (edited is None or key in edited)
            and (key in base or value not in ('', None) or (edited is not None and key in edited))
            and (key not in base or not _equivalent(key, value, base[key]))}


def _equivalent(key, left, right):
    if left in ('', None) and right in ('', None):
        return True
    if key.endswith('_port') or key in {'port', 'offset_hz'}:
        try:
            return int(left) == int(right)
        except (TypeError, ValueError):
            pass
    if key == 'sdr_application' and left in ('', None, 'Other / manual') and right in ('', None, 'Other / manual'):
        return True
    return left == right


def _summary(base, edits, current):
    lines = []
    for key, value in edits.items():
        label = key.replace('_', ' ')
        text = lambda item: str(item if item is not None else '')[:160]
        lines.append(f'{label}: saved [{text(current.get(key))}] → your entry [{text(value)}]')
    if not lines:
        lines.append('Your entries match the current saved fields. Identity and launch context will be reviewed again on Save.')
    return '\n'.join(lines[:32]) + ('\nAdditional edited fields remain in the form.' if len(lines) > 32 else '')


def application_review(base, values, response):
    current = deepcopy(dict(response['current_values']))
    edits = _changes(base, values)
    context = deepcopy(dict(response['context']))
    merged = {**current, **edits}
    if isinstance(values.get('__edited_fields__'), (list, tuple)):
        merged['__edited_fields__'] = list(values['__edited_fields__'])
    return {'base': current, 'values': merged, 'context': context,
            'summary': _summary(base, edits, current)}


def radio_review(base, values, application_drafts, response):
    current = deepcopy(dict(response['profile']))
    fields = RADIO_FIELDS | {'_operating_profile_id', '_schedule_plan_id', '_receiver_launch'}
    marked = values.get('__edited_fields__')
    edited = set(marked) if isinstance(marked, (list, tuple)) else None
    edits = {key: deepcopy(value) for key, value in values.items()
             if key in fields and (edited is None or key in edited)
             and (key in base or value not in ('', None) or (edited is not None and key in edited))
             and (key not in base or not _equivalent(key, value, base[key]))}
    if '_receiver_launch' in edits:
        # The launcher is one bounded draft field but its controls are separate
        # operator choices. Rebase only changed members, preserving newer peers.
        original = base.get('_receiver_launch') or {}
        proposed = edits['_receiver_launch']
        if isinstance(proposed, dict):
            members = {key: value for key, value in proposed.items()
                       if key not in original or value != original[key]}
            edits['_receiver_launch'] = {**deepcopy(current.get('_receiver_launch') or {}), **members}
    drafts = {}
    summaries = [_summary(base, edits, current)]
    for family, draft in application_drafts.items():
        seed = deepcopy(dict(response.get('application_seeds', {}).get(family, {})))
        context = {key: seed.pop(key) for key in _CONTEXT if key in seed}
        seed.pop('_unfinished_application', None)
        review = application_review(draft.get('operator_base_values', {}), draft.get('operator_values', {}),
                                    {'current_values': seed, 'context': context})
        drafts[family] = {**deepcopy(draft), **context,
                          'operator_base_values': review['base'], 'operator_values': review['values']}
        summaries.append(f"{family.replace('_', ' ')}:\n{review['summary']}")
    return {'base': current, 'values': {**current, **edits}, 'application_drafts': drafts,
            'summary': '\n\n'.join(summaries)}
