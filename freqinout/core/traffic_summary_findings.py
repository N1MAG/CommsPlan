"""Deterministic local topic grouping; received text is evidence, never instructions.

Grouping is deliberately conservative. Counts describe canonical traffic entries,
not independent observers or incidents. A cluster never asserts corroboration.
"""
from __future__ import annotations

import collections
import datetime as dt
import re

# Explicit place text only. Two-letter words such as IN, OR and ME are not places
# unless placed in a clear address/list context. This is not a geocoder.
_STATES = dict(zip(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split(),
    ('Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia').split('|')))
_FOREIGN = ('Saudi', 'Yemen', 'Falklands', 'Philippines', 'Romania', 'Panama', 'Singapore', 'Hong Kong', 'Siberia', 'Russia', 'Ukraine', 'Japan', 'Kenya', 'Nairobi', 'Uganda', 'Israel', 'Iran', 'Lebanon', 'Gaza', 'Taiwan', 'China', 'Canada', 'Mexico', 'Venezuela', 'Europe')
# City names anchor clusters where reports explicitly describe the same event.
# Arbitrary other cities remain in the received clauses; no station lookup substitutes them.
_CITY = re.compile(r'\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2})[, ]+(' + '|'.join(list(_STATES) + sorted(_STATES.values(), key=len, reverse=True)) + r')\b')


def event_places(text: str) -> tuple[str, ...]:
    # Explicit origin annotations are not event locations. Prefer a declared
    # event clause when one exists; arbitrary prose still requires review.
    event = re.search(r'\b(?:event|incident|affected area|report covers)\s+(?:in|at|:)\s*(.+)', text, re.I)
    if event:
        text = event.group(1)
    text = re.sub(r'\b(?:sender|operator|reporter|registered origin|station origin)\s*(?:in|at|:)\s*[^;.|]+', '', text, flags=re.I)
    text = re.sub(r'\b(?:Little Colorado River|Colorado River|New York Times)\b', '', text, flags=re.I)
    found = set()
    for code, name in sorted(_STATES.items(), key=lambda item: -len(item[1])):
        if re.search(r'\b' + re.escape(name) + r'\b', text, re.I):
            found.add(code)
    # Prefer the longest state names: West Virginia must not also become Virginia.
    if re.search(r'\bWest Virginia\b', text, re.I) and not re.search(r'(?<!West )\bVirginia\b', text, re.I):
        found.discard('VA')
    for code in _STATES:
        if code in {'IN', 'OR', 'ME', 'NE', 'OK', 'HI', 'ID', 'DC', 'AL'}:
            pattern = r'(?:^' + code + r'\s*[-:]|,\s*' + code + r'\b)'
        else:
            pattern = r'(?:^' + code + r'(?=\s|[-:])|,\s*' + code + r'\b|\b(?:in|and|across)\s+' + code + r'\b|\b[A-Z][a-z]+\s+' + code + r'\b)'
        if re.search(pattern, text):
            found.add(code)
    for match in _CITY.finditer(text):
        value = match.group(2)
        if value in _STATES and (value not in {'IN', 'OR', 'ME', 'NE', 'OK', 'HI', 'ID', 'DC'} or ',' in match.group(0)):
            found.add(value)
    # Compass-qualified state names and two-state alert ranges are explicit.
    for match in re.finditer(r'\b(?:[NSEW]\.[NSEW]\.|[NS][EW])\s+([A-Z]{2})\b', text):
        if match.group(1) in _STATES:
            found.add(match.group(1))
    for match in re.finditer(r'\b([A-Z]{2})\s*-\s*([A-Z]{2})\b', text):
        first, second = match.groups()
        if second in _STATES:
            found.add(second)
            if first in _STATES and first not in {'NE', 'SE', 'NW', 'SW'}:
                found.add(first)
    if re.search(r'\bNE\s*-\s*FL\b', text):
        found.discard('NE')
    if re.search(r'\b(?:Philadelphia|Philly)\b', text, re.I):
        found.add('Philadelphia')
    found.update(place for place in _FOREIGN if re.search((r'(?<!New )' if place == 'Mexico' else '') + r'\b' + re.escape(place) + r'\b', text, re.I))
    return tuple(sorted(found))


_RULES = (
    ('FIRE / EVACUATION', 'fire / evacuation', r'\b(?:wildfire|fire|evacuat\w*|derail\w*)\b'),
    ('WEATHER', 'extreme heat', r'\b(?:heat|hot weather|high temp\w*)\b'),
    ('WEATHER', 'blizzard / winter weather', r'\b(?:blizzard|snow|ice storm|winter storm)\b'),
    ('WEATHER', 'rain / flooding', r'\b(?:flood\w*|rain\w*|dam damage|storm drain\w*)\b'),
    ('WEATHER', 'tropical weather', r'\b(?:hurricane|tropical|NHC|invest \d+)\b'),
    ('WEATHER', 'wind / severe weather', r'\b(?:tornado|thunderstorm|wind advisory|winds?|windy|dust (?:storm|advisory))\b'),
    ('HEALTH', 'health', r'\b(?:ebola|plague|pneumonia|quarantine|outbreak|bio.containment|lab leak|hospital|medical emergency)\b'),
    ('ENERGY / TRANSPORT', 'energy / transport', r'\b(?:pipeline|fuel|gasoline|power outage|blackout|lane closure\w*|highway|road closed|roads closed|road closure|I-\d+|traffic accident|tanker|cargo ship|FPSO)\b'),
    ('SECURITY / CIVIL CONDITIONS', 'security / civil conditions', r'\b(?:attack\w*|missile\w*|airstrike\w*|counteroffensive|hijack\w*|war|unrest|crime|shooting|riot\w*)\b'),
    ('COMMUNICATIONS', 'communications', r'\b(?:antenna|repeater|JS8|fldigi|FLMsg|FLAmp|MFSK|radio|mesh|comms|net check|packet|propagation|internet outage|cyberattack\w*|subsea fiber|cable cut|internet outage)\b'),
)


def _body(entry):
    value = entry.body_text or entry.excerpt
    # Transport delimiters, links and machine brevity tokens are not findings.
    # Use remarks when a legacy structured form repeats its serialized payload.
    if 'REMARKS ' in value and 'PRECEDENCE ' in value:
        value = value.partition('REMARKS ')[2]
    elif re.match(r'^\s*,[A-R]{2}\d{2}[A-X]{0,2},[1-4],', value):
        # Standalone Spotter payload, rather than prose followed by a payload.
        match = re.match(r'^\s*,[A-R]{2}\d{2}[A-X]{0,2},[1-4],[^,]*,[^,]*,(.*?)(?:,\{.*)?$', value, re.S)
        value = match.group(1) if match else value
    else:
        value = re.split(r',(?:[A-R]{2}\d{2}[A-X]{0,2}),[1-4],', value, maxsplit=1)[0]
    value = re.sub(r'https?://\S+|(?:www\.)?weather\.gov/\S+', '', value, flags=re.I)
    value = re.sub(r'\b[19][A-Z]{7}\b', '', value)
    value = re.sub(r'\(second send[^)]*\)', '', value, flags=re.I)
    # NWS dissemination headers/zone codes and exhaustive city lists duplicate
    # the affected-area section; keep WHAT/WHERE/WHEN/IMPACTS and measured values.
    value = re.sub(r'URGENT - WEATHER MESSAGE.*?\.\.\.(?=[A-Z])', '', value, flags=re.S)
    value = re.sub(r'Including the cities of .*?(?=\d{1,4} [AP]M|\* WHEN|\* IMPACTS|\|\|)', '', value, flags=re.I|re.S)
    value = re.sub(r'\bmore info:\s*', '', value, flags=re.I)
    value = re.sub(r'\b[12][A-Z]{7}\b', '', value)
    return re.sub(r'\s+', ' ', value.replace('|', ' ')).strip()


def classification(entry):
    text = _body(entry)
    if entry.source_family in {'meshcore', 'mesh'} and not _elevated(entry):
        # Unstructured discussion of heat, war, snow or highways is not an alert.
        if not re.search(r'\b(?:warning|advisory|evacuat\w*|closed|washed out|outage|fire reported)\b', text, re.I):
            if re.search(r'\b(?:antenna|repeater|radio|mesh|JS8|fldigi|packet)\b', text, re.I):
                return 'COMMUNICATIONS', 'communications'
            return 'GENERAL / SOCIAL', 'general traffic'
    # Structured normal forms must not become weather/security incidents merely
    # because all sector names are printed in the form.
    if entry.sector_status and not text:
        return 'STATUS', 'station status'
    for topic, condition, pattern in _RULES:
        if re.search(pattern, text, re.I):
            return topic, condition
    # Retained specific topics are a fallback when content rules cannot map prose.
    # Generic Comms/Social tags on legacy forms do not override explicit findings.
    taxonomy = {'water': 'WATER', 'food': 'FOOD / SUPPLIES', 'medical': 'HEALTH',
                'shelter': 'SHELTER / SERVICES', 'power': 'ENERGY / TRANSPORT'}
    for tag in entry.topics:
        if tag.casefold() in taxonomy:
            topic = taxonomy[tag.casefold()]
            condition = {'HEALTH': 'health', 'ENERGY / TRANSPORT': 'energy / transport'}.get(topic, tag.casefold())
            return topic, condition
    if re.search(r'\b(?:shelter|911|emergency services)\b', text, re.I):
        return 'SHELTER / SERVICES', 'shelter / services'
    if re.search(r'\b(?:green|all (?:ok|good)|no (?:significant )?(?:issues|change)|routine status)\b', text, re.I):
        return 'STATUS', 'station status'
    if entry.sector_status and not re.search(r'[^A-Z0-9 ,|.+-]', text):
        return 'STATUS', 'station status'
    if entry.source_family in {'meshcore', 'mesh', 'js8'}:
        return 'GENERAL / SOCIAL', 'general traffic'
    return 'OTHER REPORTS', 'other reports'


def _elevated(entry):
    return entry.severity.lower() in {'critical', 'priority', 'warning', 'watch', 'emergency', 'red', 'yellow'}


def _cluster_key(entry, topic, condition):
    text = _body(entry)
    places = entry.event_places or event_places(text)
    # Regional weather advisories may be consolidated across states. Preserve the
    # exact clauses below, including different times or contradictory updates.
    if topic == 'WEATHER':
        return condition, ''
    city = _CITY.search(text)
    location = city.group(0).strip() if city else ', '.join(places)
    # Unlocated event reports cannot be presumed to concern the same event.
    if 'Siberia' in places:
        location = 'Siberia'
    if not location:
        location = 'unqualified:' + entry.message_id
    return condition, location


def _clauses(text):
    # Do not split decimal values or abbreviations into purported independent facts.
    return [part.strip(' .;') for part in re.split(r'(?<=[!?])\s+|(?<=[a-z0-9]\.)\s+(?=[A-Z])|\s*\|+\s*', text) if part.strip(' .;')]


def consolidated_findings(entries):
    """Return topic/event groups with evidence links and lossless distinct clauses."""
    groups = collections.defaultdict(list)
    for entry in entries:
        topic, condition = classification(entry)
        key = (_elevated(entry), topic, *_cluster_key(entry, topic, condition))
        groups[key].append(entry)
    result = []
    for (elevated, topic, condition, location), reports in sorted(groups.items(), key=lambda item: (not item[0][0], item[0][1:])):
        places = sorted({p for e in reports for p in (e.event_places or event_places(_body(e)))})
        details, seen = [], set()
        for entry in sorted(reports, key=lambda e: (e.received_ts, e.message_id)):
            for clause in _clauses(_body(entry)):
                key = re.sub(r'\W+', ' ', clause).strip().casefold()
                if key and key not in seen:
                    seen.add(key)
                    details.append(clause)
        result.append(dict(elevated=elevated, topic=topic, condition=condition,
                           places=tuple(places), details=tuple(details), entries=tuple(reports)))
    rank = {"emergency": 0, "critical": 0, "red": 0, "priority": 1, "warning": 1, "yellow": 1, "watch": 2}
    return sorted(result, key=lambda f: (not f['elevated'], f['topic'],
                  min(rank.get(e.severity.lower(), 3) for e in f['entries']), f['condition'], f['places']))



def _regional_weather_details(finding):
    """Compact repeated regional advisories; concrete event impacts use excerpts.

    The weekly rollup describes advisory traffic, rather than issuing an active
    forecast. Forecast windows and exhaustive locality lists remain in evidence.
    """
    if finding['topic'] != 'WEATHER' or finding['condition'] not in {'extreme heat', 'blizzard / winter weather'}:
        return finding.get('details', ())
    text = ' '.join(_body(e) for e in finding['entries'])
    if any(e.severity.lower() in {'critical', 'emergency', 'red'} for e in finding['entries']) or re.search(
            r'\b(?:evacuat\w*|injur\w*|death|fatal\w*|closed|closure|damage|outage|shelter)\b', text, re.I):
        return finding.get('details', ())
    kinds = sorted(set(word.lower() for word in re.findall(r'\b(?:watch|warning|advisory)\b', text, re.I)))
    if not kinds:
        return finding.get('details', ())
    details = ['Regional ' + '/'.join(kinds) + ' traffic; reported forecast windows vary']
    # WHAT and IMPACTS contain measured intensity/temperature and consequence;
    # retain each distinct value instead of the repeated city/header lists.
    seen = set()
    for entry in finding['entries']:
        for match in re.finditer(r'\*\s*(?:WHAT|IMPACTS)\s*\.\.\.(.*?)(?=\*\s*[A-Z]+\s*\.\.\.|$)', _body(entry), re.I):
            value = match.group(1).strip(' .')
            if value.casefold() not in seen:
                seen.add(value.casefold())
                details.append(value)
        for clause in _clauses(_body(entry)):
            if re.search(r'\b\d+(?:\s*to\s*\d+)?\s*(?:degrees?|°[FC]?|[FC]\b|inches|feet|ft\b|mph\b)', clause, re.I) and not any(clause.casefold() in v.casefold() or v.casefold() in clause.casefold() for v in details[1:]):
                details.append(clause)
    return tuple(details)


def render_consolidated(snapshot, *, notes='', excluded_ids=()):
    # Import at call time to keep the core snapshot model Qt-free and acyclic.
    from freqinout.core.traffic_summary import REPORT_MARKER, COMPUTED_START, COMPUTED_END, _clean
    excluded = set(excluded_ids)
    if excluded - {e.message_id for e in snapshot.entries}:
        raise ValueError('An excluded message is not part of this snapshot.')
    entries = [e for e in snapshot.entries if e.message_id not in excluded]
    call = snapshot.station_callsign or 'Unknown'
    def stamp(value):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime('%d %b %Y %H%MZ').upper()
    lines = [f'TRAFFIC SUMMARY DE {call}', f'{stamp(snapshot.scope.start_ts)} - {stamp(snapshot.scope.end_ts)}',
             REPORT_MARKER + snapshot.report_id, COMPUTED_START,
             f'{len(entries)} reports; {sum(_elevated(e) for e in entries)} elevated by source label.',
             'Sources: ' + '; '.join(f'{k} {v}' for k, v in sorted(collections.Counter(e.source_family for e in entries).items())) + '.']
    lines.append('Source severity: ' + '; '.join(f'{k} {v}' for k, v in sorted(collections.Counter(e.severity for e in entries).items())) + '.')
    lines.append('Receive paths: ' + '; '.join(f'{k} {v}' for k, v in sorted(collections.Counter(e.provenance for e in entries).items())) + '.')
    statuses = collections.Counter(dict(e.sector_status).get('overall_status', 'unknown') for e in entries if e.sector_status)
    if statuses:
        lines.append('Reported status: ' + '; '.join(f'{k} {v}' for k, v in sorted(statuses.items())) + '.')
    lines += [COMPUTED_END, '']
    references = {e.message_id: f'R{i:03d}' for i, e in enumerate(snapshot.entries, 1)}
    findings = consolidated_findings(entries)
    finding_number = 0
    for elevated, heading in ((True, 'CRITICAL / PRIORITY'), (False, 'ROUTINE')):
        lines.append(heading)
        selected = [f for f in findings if f['elevated'] == elevated]
        if not selected:
            lines.append('No reports in this section.')
        topic = None
        # Routine status/discussion receives one count-and-geography rollup per
        # topic, while all elevated narratives retain every distinct source clause.
        if not elevated:
            event_findings = [f for f in selected if any(re.search(
                r'\b(?:warning|advisory|evacuat\w*|closed|closure|washed out|outage|injur\w*|fatal\w*|death|damage|explosion|shelter|911|emergency services)\b',
                _body(e), re.I) for e in f['entries'])]
            by_topic = collections.defaultdict(list)
            for f in selected:
                if f not in event_findings:
                    by_topic[f['topic']].extend(f['entries'])
            selected = [dict(topic=t, condition=t.lower(), entries=tuple(es), places=tuple(sorted({p for e in es for p in e.event_places}))) for t, es in sorted(by_topic.items())] + event_findings
            selected.sort(key=lambda f: (f['topic'], f['condition']))
        for finding in selected:
            if finding['topic'] != topic:
                topic = finding['topic']
                lines.append(topic)
            reports = finding['entries']
            places = ', '.join(finding['places']) or 'Unknown event geography'
            finding_number += 1
            label = f"- F{finding_number:03d} {finding['condition'].capitalize()}: {len(reports)} report{'s' if len(reports) != 1 else ''}; {places}."
            if elevated:
                label += ' Source labels: ' + '/'.join(sorted({e.severity for e in reports})) + '.'
            if elevated or finding.get('details'):
                label += ' ' + ('; '.join(_regional_weather_details(finding)) if finding['details'] else 'No readable narrative; review the retained source.') + '.'
                label += ' [' + ','.join(references[e.message_id] for e in reports) + ']'
                if any(not e.body_complete for e in reports):
                    label += ' [Stored preview; review source.]'
                # Attribution is useful for one source/ambiguous geography, not
                # repeated on consolidated regional findings.
                if len(reports) == 1:
                    e = reports[0]
                    origin = '/'.join(v for v in (e.origin_state, e.origin_grid) if v)
                    if e.from_call:
                        label += f" Source {e.from_call}" + (f' ({"registered origin" if e.origin_source == "call_finder" else "reported/registered origin" if "call_finder" in e.origin_source else "reported origin"}: {origin})' if origin else ' (origin: Unknown)') + '.'
            lines.append(label)
        lines.append('')
    if notes.strip():
        lines += ['OPERATOR NOTES', _clean(notes).strip(), '']
    lines += ['Counts are traffic entries, not confirmed incidents or independent reports. Stored times; source claims require review. Some stored records are previews; check originals.',
              f'end report de {call}']
    return '\n'.join(lines) + '\n'
