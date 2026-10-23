"""Offline paired reports: a bounded radio brief and complete shareable evidence.

Selection controls presentation only. Every included entry remains in Detailed;
no lack of new traffic is interpreted as resolution or an all-clear.
"""
from __future__ import annotations

import collections
import datetime as dt
import re

from freqinout.core.traffic_summary_findings import (
    _body, _clauses, _elevated, _FOREIGN, classification, consolidated_findings,
)

_OPERATIONAL = re.compile(
    r'\b(?:evac\w*|derail\w*|explosion|wildfire|fire\b|road closure|roads? closed|'
    r'lanes? clos\w*|lane closures?|highways?[^.;]{0,30}closed|washed out|dam damage|'
    r'outage|blackout|shelter\b|911\b|injur\w*|fatal\w*|deaths?\b|'
    r'(?:request|need|require)\w*\s+(?:help|aid|assistance|rescue|medical|water|food|fuel)|'
    r'(?:water|power|communications?|internet)\s+(?:unavailable|down|lost))\b', re.I)
_NEWS = re.compile(r'\b(?:QST|secondary bulletin|VIA HTRS|AFP|REUTERS|BLOOMBERG|'
                   r'GAZETTE|TIMES|MOH|DICT|claims?|suspected|bulletin)\b', re.I)
_FIRST_PERSON = re.compile(r'\b(?:I|we|our|my|here)\b', re.I)


def estimate_airtime_seconds(text: str) -> float:
    """Planning estimate, not modem measurement; serialization/retries are extra.

    Nominal 120 WPM and a 12-byte/second equivalent provide two lower bounds.
    The longer bound plus 20 seconds avoids treating a long unbroken ID as one
    ordinary word. UTF-8/non-ASCII can take longer on the actual modem.
    """
    return 20.0 + max(len(text.split()) / 2.0, len(text.encode('utf-8')) / 12.0)


def _population(snapshot, excluded_ids):
    excluded = set(excluded_ids)
    if excluded - {e.message_id for e in snapshot.entries}:
        raise ValueError('An excluded message is not part of this snapshot.')
    return [e for e in snapshot.entries if e.message_id not in excluded]


def _stamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime('%d %b %Y %H%MZ').upper()


def _counts_block(entries):
    from freqinout.core.traffic_summary import COMPUTED_START, COMPUTED_END
    statuses = collections.Counter(dict(e.sector_status).get('overall_status', 'unknown') for e in entries if e.sector_status)
    lines = [COMPUTED_START, f'Traffic: {len(entries)} entries; elevated source labels: {sum(_elevated(e) for e in entries)}.']
    if statuses:
        lines.append('Status: ' + ', '.join(f'{key} {value}' for key, value in sorted(statuses.items())) + '.')
    paths = collections.Counter(e.provenance for e in entries)
    path_names = {'local_rf': 'RF', 'imported': 'import', 'internet': 'internet', 'mixed': 'mixed', 'unknown': 'unknown'}
    lines.append('Paths: ' + ', '.join(f'{path_names.get(key, key)} {value}' for key, value in sorted(paths.items())) + '.')
    return [*lines, COMPUTED_END]


def _header(snapshot, entries, kind):
    from freqinout.core.traffic_summary import REPORT_MARKER
    lines = [f'TRAFFIC SUMMARY DE {snapshot.station_callsign or "Unknown"}',
             'RF BRIEF' if kind == 'rf' else 'DETAILED REPORT',
             f'{_stamp(snapshot.scope.start_ts)} - {_stamp(snapshot.scope.end_ts)}']
    filters = []
    for field, label in (("groups", "groups"), ("sources", "sources"), ("radio_ids", "radios"), ("states", "states")):
        values = getattr(snapshot.scope, field)
        if values:
            filters.append(label + ' ' + ','.join(values))
    if filters:
        lines.append('Scope: ' + '; '.join(filters))
    return [*lines, REPORT_MARKER + snapshot.report_id, *_counts_block(entries), '']



def _secondary(entry):
    text = _body(entry)
    if _FIRST_PERSON.search(text):
        return False
    # This is an editorial routing rule, never a claim that an entry is false,
    # independent, or unimportant. Its complete body remains in Detailed.
    foreign = any(p in _FOREIGN for p in entry.event_places) or bool(re.search(r'\bU\.A\.E\.', text))
    if re.search(r'\b(?:request|need|require)\w*\s+(?:help|aid|rescue|assistance)\b', text, re.I):
        return False
    return bool(_NEWS.search(text) and (foreign or re.search(r'\bsecondary bulletin\b', text, re.I)))



def _essential(entry):
    return bool(_OPERATIONAL.search(_body(entry))) or any(
        key != "overall_status" and value.lower() in {"red", "critical", "emergency"}
        for key, value in entry.sector_status)


def _rf_facts(entry):
    """Keep all available event clauses for essential impacts.

    We cannot reliably rank arbitrary prose for importance offline. Budgeting
    therefore defers optional reports rather than clipping essential facts.
    """
    clauses = _clauses(_body(entry)) or ['Narrative unavailable; review source']
    red = [f'{key} {value}' for key, value in entry.sector_status if key != 'overall_status' and value.lower() in {'red', 'critical', 'emergency'}]
    if red:
        clauses.append('Reported sectors: ' + ', '.join(red))
    return clauses


def _places(entries):
    places = {p for e in entries for p in e.event_places}
    if places and any(not e.event_places for e in entries):
        places.add('Unknown')
    return ','.join(sorted(places)) or 'Unknown'


def _brief_label(value):
    return {'energy / transport': 'Transport/energy', 'fire / evacuation': 'Fire/evacuation',
            'security / civil conditions': 'Security', 'rain / flooding': 'Rain/flooding',
            'blizzard / winter weather': 'Winter weather', 'wind / severe weather': 'Wind/severe weather'}.get(value.lower(), value.capitalize())


def render_rf(snapshot, *, target_minutes=3, notes='', excluded_ids=()):
    if target_minutes not in (3, 5, 10):
        raise ValueError('Choose a 3, 5 or 10 minute MFSK32 planning target.')
    entries = _population(snapshot, excluded_ids)
    refs = {e.message_id: f'R{i:03d}' for i, e in enumerate(snapshot.entries, 1)}
    secondary = [e for e in entries if _secondary(e)]
    secondary_ids = {e.message_id for e in secondary}
    essential = [e for e in entries if e.message_id not in secondary_ids and _essential(e)]
    essential_ids = {e.message_id for e in essential}
    lines = _header(snapshot, entries, 'rf') + ['PRIORITY / SIGNIFICANT IMPACTS']
    represented = set()
    number = 0
    for finding in consolidated_findings(essential):
        number += 1
        reports = finding['entries']
        clauses, seen = [], set()
        for entry in reports:
            for clause in _rf_facts(entry):
                key = re.sub(r'\W+', ' ', clause).casefold().strip()
                if key and key not in seen:
                    seen.add(key)
                    clauses.append(clause)
        places = _places(reports)
        lines.append(f'- F{number:03d} {_brief_label(finding["condition"])}: {len(reports)} report{"s" if len(reports) != 1 else ""}; {places}. '
                     + '; '.join(clauses) + '. [' + ','.join(refs[e.message_id] for e in reports) + ']')
        represented.update(e.message_id for e in reports)
    if not essential:
        lines.append('No explicit operational impacts extracted; review Detailed for source claims.')

    remaining = [e for e in entries if e.message_id not in essential_ids | secondary_ids]
    weather = collections.defaultdict(list)
    other_elevated = collections.defaultdict(list)
    routine = []
    for entry in remaining:
        topic, condition = classification(entry)
        if topic == 'WEATHER':
            weather[condition].append(entry)
        elif _elevated(entry):
            other_elevated[topic].append(entry)
        else:
            routine.append(entry)
    # Reserve the final accounting/limitations before adding optional rollups.
    def tail(covered):
        deferred = [e for e in entries if e.message_id not in covered]
        elevated_deferred = sum(_elevated(e) for e in deferred)
        secondary_entries = [e for e in deferred if e.message_id in secondary_ids]
        secondary_deferred = len(secondary_entries)
        secondary_names = {'COMMUNICATIONS': 'comms', 'ENERGY / TRANSPORT': 'energy', 'HEALTH': 'health', 'SECURITY / CIVIL CONDITIONS': 'security'}
        secondary_topics = collections.Counter(secondary_names.get(classification(e)[0], classification(e)[0].lower()) for e in secondary_entries)
        secondary_summary = ', '.join(f'{topic} {count}' for topic, count in sorted(secondary_topics.items()))
        output = []
        if routine:
            output.append(f'Routine/other traffic: {len(routine)} entries.')
        if deferred:
            output.append(f'Additional detail: {len(deferred)} entries ({elevated_deferred} elevated; {secondary_deferred} secondary bulletin entries: {secondary_summary or "none"}); see matching Detailed Report.')
        output.append('Entry counts, not confirmed incidents. Stored times; no resolution inferred.')
        if notes.strip():
            from freqinout.core.traffic_summary import _clean
            output.extend(['OPERATOR NOTES', _clean(notes).strip()])
        output.append(f'end report de {snapshot.station_callsign or "Unknown"}')
        return output
    # Routine content is represented by its count, rather than duplicated in
    # the deferred count. Secondary and unselected elevated/weather are deferred.
    represented.update(e.message_id for e in routine)
    candidates = []
    selected_candidates = []
    for condition, reports in sorted(weather.items(), key=lambda v: (not any(_elevated(e) for e in v[1]), v[0])):
        label = _brief_label(condition)
        kinds = sorted({word.lower() for e in reports for word in re.findall(r'\b(?:watch|warning|advisory)\b', _body(e), re.I)})
        detail = ' ' + '/'.join(kinds) + '.' if kinds else ''
        candidates.append((reports, f'{label}: {{count}} reports; {_places(reports)}.{detail}'))
    for topic, reports in sorted(other_elevated.items()):
        candidates.append((reports, f'{_brief_label(topic)}: {{count}} reports; {_places(reports)}. Details in Detailed.'))
    if candidates:
        lines.extend(['', 'REGIONAL / OTHER'])
    regional_headline_kept = False
    for reports, template in candidates:
        candidate = (f'- F{number + 1:03d} ' + template.format(count=len(reports))).replace(': 1 reports;', ': 1 report;')
        covered = represented | {e.message_id for e in reports}
        trial = '\n'.join([*lines, candidate, '', *tail(covered)]) + '\n'
        regional_alert = any(classification(e)[0] == 'WEATHER' and re.search(r'\b(?:watch|warning|advisory)\b', _body(e), re.I) for e in reports)
        # The target is guidance: retain one regional alert headline even when
        # essential event clauses leave less than a line of space.
        if estimate_airtime_seconds(trial) <= target_minutes * 60 or (regional_alert and not regional_headline_kept):
            number += 1
            lines.append(candidate)
            represented = covered
            selected_candidates.append((len(lines) - 1, reports))
            regional_headline_kept = regional_headline_kept or regional_alert
    # Longer targets buy additional supported detail rather than merely allowing
    # more words. Upgrade whole rollups, then include secondary bulletin groups.
    if target_minutes > 3:
        for index, reports in selected_candidates:
            details, seen = [], set()
            for entry in reports:
                for clause in _rf_facts(entry):
                    key = clause.casefold()
                    if key not in seen:
                        seen.add(key)
                        details.append(clause)
            expanded = lines[index] + ' ' + '; '.join(details) + ' [' + ','.join(refs[e.message_id] for e in reports) + ']'
            trial = list(lines)
            trial[index] = expanded
            if estimate_airtime_seconds('\n'.join([*trial, '', *tail(represented)])) <= target_minutes * 60:
                lines[index] = expanded
        for finding in consolidated_findings(secondary):
            reports = finding['entries']
            detail = '; '.join(finding['details'])
            candidate = (f'- F{number + 1:03d} Secondary {finding["condition"]}: {len(reports)} reports; '
                         f'{_places(reports)}. {detail} [' + ','.join(refs[e.message_id] for e in reports) + ']')
            candidate = candidate.replace(': 1 reports;', ': 1 report;')
            covered = represented | {e.message_id for e in reports}
            if estimate_airtime_seconds('\n'.join([*lines, candidate, '', *tail(covered)])) <= target_minutes * 60:
                number += 1
                lines.append(candidate)
                represented = covered
    if lines[-1:] == ['REGIONAL / OTHER']:
        lines.pop()
    lines.extend(['', *tail(represented)])
    return '\n'.join(lines) + '\n'


def render_detailed(snapshot, *, notes='', excluded_ids=()):
    entries = _population(snapshot, excluded_ids)
    raw_bytes = 0
    for entry in entries:
        raw_bytes += len((entry.body_text or entry.excerpt).encode('utf-8'))
        if raw_bytes > 4 * 1024 * 1024:
            from freqinout.core.traffic_summary import SummaryLimitError
            raise SummaryLimitError('Detailed Report exceeds the 4 MiB editor limit. Choose a shorter period or narrower source filters; no partial report was generated.')
    refs = {e.message_id: f'R{i:03d}' for i, e in enumerate(snapshot.entries, 1)}
    lines = _header(snapshot, entries, 'detailed')
    lines += ['Complete included traffic; source claims are not independently verified.',
              'Distinct details and conflicting claims are retained below. Some source records are previews.',
              'Absence of a new report does not resolve an earlier condition.', '']
    # Lead with a human-scale topic digest; the complete appendix below keeps
    # original bodies and structured fields available without burying the summary.
    from freqinout.core.traffic_summary_findings import render_consolidated
    from freqinout.core.traffic_summary import COMPUTED_END
    summary = render_consolidated(snapshot, excluded_ids=excluded_ids).partition(COMPUTED_END)[2].strip().splitlines()
    if summary and summary[-1].startswith('end report de '):
        summary.pop()
    lines.extend(['TOPIC SUMMARY', *summary, '', 'FULL EVIDENCE APPENDIX', ''])
    summary_ids = [int(match.group(1)) for line in summary
                   if (match := re.match(r'^- F(\d+) ', line))]
    groups = collections.defaultdict(list)
    for entry in entries:
        groups[(_elevated(entry), classification(entry)[0])].append(entry)
    if not entries:
        lines.append('No eligible entries in the selected period. This does not establish an all-clear.')
    section = None
    group_number = max(summary_ids, default=0)
    counted_lines = 0
    detailed_bytes = 0
    for (elevated, topic), reports in sorted(groups.items(), key=lambda item: (not item[0][0], item[0][1])):
        if section != elevated:
            section = elevated
            lines.extend(['CRITICAL / PRIORITY' if elevated else 'ROUTINE', ''])
        group_number += 1
        lines.extend([topic, f'- F{group_number:03d} Topic inventory: {len(reports)} reports; full entries below.', ''])
        for entry in sorted(reports, key=lambda e: (e.received_ts, e.message_id)):
            origin = '/'.join(v for v in (entry.origin_state, entry.origin_grid) if v) or 'Unknown'
            lines.extend([f'[{refs[entry.message_id]}] {entry.from_call or "Unknown sender"} / {entry.source_family} / {entry.severity}',
                          f'Stored time: {_stamp(entry.received_ts)}; event time: {_stamp(entry.event_ts) if entry.event_ts else "Unknown"}',
                          f'Sender origin: {origin} ({entry.origin_source or "unknown"}); reported event areas: {_places([entry])}',
                          '\n'.join('> ' + line for line in (entry.body_text or entry.excerpt or 'No stored narrative available.').splitlines())])
            lines.append(f'Group: {entry.group_name or "Unknown"}; receive path: {entry.provenance}; direction: {entry.direction}; reported grid: {entry.grid or "Unknown"}')
            if entry.sector_status:
                lines.append('Reported sectors: ' + '; '.join(f'{key} {value}' for key, value in entry.sector_status))
            # Do not distribute filesystem paths or private source configuration.
            safe_refs = [r for r in entry.source_refs if not re.search(r'(?:^|:)\s*(?:/|~[/\\]|[A-Za-z]:[/\\])|/[^\s/\[\]]+/|\\', r)]
            if safe_refs:
                lines.append('Source references:\n' + '\n'.join('> ' + line for ref in safe_refs for line in str(ref).splitlines()))
            if not entry.body_complete:
                lines.append('Completeness: stored preview; check original source for omitted upstream content.')
            lines.append('')
            detailed_bytes += sum(len(line.encode('utf-8')) + 1 for line in lines[counted_lines:])
            counted_lines = len(lines)
            if detailed_bytes > 4 * 1024 * 1024:
                from freqinout.core.traffic_summary import SummaryLimitError
                raise SummaryLimitError('Detailed Report exceeds the 4 MiB editor limit. Choose a shorter period or narrower source filters; no partial report was generated.')
    if notes.strip():
        from freqinout.core.traffic_summary import _clean
        lines.extend(['OPERATOR NOTES', _clean(notes).strip(), ''])
    lines.append(f'end report de {snapshot.station_callsign or "Unknown"}')
    return '\n'.join(lines) + '\n'
