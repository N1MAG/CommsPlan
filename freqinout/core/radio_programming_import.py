"""Read radio programming CSV files into a reviewable, Qt-free preview.

``mapping`` maps canonical names (e.g. ``receive_hz``) to source headers.
Generic frequency columns are MHz unless the mapped/header name ends in ``hz``.
No write to the catalog is performed here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .resource_catalog_models import FrequencyResource

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10000
SOURCE_KEY = 'source_station_radio_import'

@dataclass(frozen=True, slots=True)
class RadioCsvRow:
    row_number: int
    resource: FrequencyResource | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    raw_values: Mapping[str, str]

@dataclass(frozen=True, slots=True)
class RadioCsvPreview:
    format_name: str
    headers: tuple[str, ...]
    rows: tuple[RadioCsvRow, ...]


def _norm(s: str) -> str:
    return ''.join(c for c in s.lower() if c.isalnum())


def _number(value: str, multiplier: int, field: str) -> int:
    try:
        n = Decimal(value.strip()) * multiplier
        if not n.is_finite() or n != n.to_integral_value() or n <= 0 or n > 9223372036854775807:
            raise ValueError
        return int(n)
    except (DecimalException, ValueError, OverflowError):
        raise ValueError(f'{field} must be a positive, exact frequency') from None


def _tone_value(value: str, kind: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f'{field} is required for enabled tone')
    if kind == 'Tone':
        try:
            n = Decimal(value)
            if not n.is_finite() or n <= 0 or n > 1000:
                raise ValueError
            return f'CTCSS {n.normalize():f} Hz'
        except (DecimalException, ValueError):
            raise ValueError(f'{field} is invalid') from None
    if not value.isdigit() or len(value) > 3 or any(c not in '01234567' for c in value):
        raise ValueError(f'{field} must be a three-digit octal DTCS code')
    return f'DTCS {value.zfill(3)}'


def _chirp_tone(get, fields) -> str | None:
    mode = get('tone_mode').strip()
    if not mode:
        return None
    polarity = get('dtcs_polarity').strip() or 'NN'
    if polarity not in ('NN', 'NR', 'RN', 'RR'):
        raise ValueError('invalid DTCS polarity')
    if mode == 'Tone':
        sides = ('Tone', '')
    elif mode == 'TSQL':
        sides = ('Tone', 'Tone')
    elif mode == 'DTCS':
        sides = ('DTCS', 'DTCS')
    elif mode == 'Cross':
        cross = get('cross_mode').strip()
        if cross not in ('Tone->Tone', 'DTCS->', '->DTCS', 'Tone->DTCS', 'DTCS->Tone', '->Tone', 'DTCS->DTCS'):
            raise ValueError('invalid or missing CHIRP cross mode')
        sides = tuple(cross.split('->'))
    else:
        raise ValueError(f'unsupported CHIRP tone mode: {mode}')
    result = []
    for i, (direction, kind) in enumerate(zip(('TX', 'RX'), sides)):
        if not kind:
            continue
        field = ('ctone' if mode == 'TSQL' else ('rtone' if i == 0 else 'ctone')) if kind == 'Tone' else ('dtcs' if mode == 'DTCS' or i == 0 else 'rx_dtcs')
        value = get(field)
        # CHIRP copies the sole tone column when the other is omitted.
        if kind == 'Tone' and field not in fields:
            value = get('ctone' if field == 'rtone' else 'rtone')
        item = _tone_value(value, kind, field)
        if kind == 'DTCS':
            item += f' polarity {polarity[i]}'
        result.append(f'{direction}: {item}')
    return '; '.join(result) or None


def _repeaterbook_tone(value: str, warnings: list[str]) -> str | None:
    value = value.strip()
    if not value or value.upper() == 'CSQ':
        return None
    match = re.fullmatch(r'D([0-7]{3})([NR])?', value, re.IGNORECASE)
    if match:
        result = f'DTCS {match.group(1)}'
        if match.group(2):
            result += f' polarity {match.group(2).upper()}'
        return result
    try:
        return _tone_value(value, 'Tone', 'RepeaterBook tone')
    except ValueError:
        warnings.append(f'Unsupported RepeaterBook tone: {value}')
        return None


def _btech_tone(value: str) -> str | None:
    value = value.strip()
    if value in ('', '0'):
        return None
    match = re.fullmatch(r'DCS\s+([0-7]{3})([NR])', value, re.IGNORECASE)
    if match:
        return f'DTCS {match.group(1)} polarity {match.group(2).upper()}'
    if not value.isdigit():
        raise ValueError('invalid BTECH sub-audio value')
    n = int(value)
    if n >= 1000:
        return f'CTCSS {Decimal(n) / 100:f} Hz'
    return _tone_value(value, 'DTCS', 'sub-audio')


_ALIASES = {
    'receive_hz': ('frequency', 'receive frequency', 'rx frequency', 'rx_freq', 'output freq', 'output frequency', 'receive_hz', 'rx_hz', 'receive frequency (MHz)', 'rx frequency (MHz)', 'receive frequency (Hz)', 'rx frequency (Hz)'),
    'transmit_hz': ('transmit frequency', 'tx frequency', 'tx_freq', 'input freq', 'input frequency', 'transmit_hz', 'tx_hz', 'transmit frequency (MHz)', 'tx frequency (MHz)', 'transmit frequency (Hz)', 'tx frequency (Hz)'),
    'label': ('name', 'title', 'label', 'call', 'callsign'),
    'channel': ('location', 'channel', 'memory'),
    'duplex': ('duplex',), 'offset': ('offset',), 'tone_mode': ('tone',),
    'rtone': ('rToneFreq',), 'ctone': ('cToneFreq',), 'dtcs': ('DtcsCode',),
    'rx_dtcs': ('RxDtcsCode',), 'dtcs_polarity': ('DtcsPolarity',), 'cross_mode': ('CrossMode',),
    'mode': ('mode', 'modes'), 'comment': ('comment', 'notes', 'description'),
    'locality': ('location', 'city'), 'bandwidth': ('bandwidth(12500/25000)',),
    'tx_tone': ('tx_sub_audio(CTCSS=freq/DCS=number)', 'uplink tone'),
    'rx_tone': ('rx_sub_audio(CTCSS=freq/DCS=number)', 'downlink tone'),
    'tx_dis': ('tx_dis(0=OFF/1=ON)',), 'rx_mod': ('rx_modulation(0=FM/1=AM)',),
    'tx_mod': ('tx_modulation(0=FM/1=AM)',), 'talk_around': ('talk around(0=OFF/1=ON)',),
}


def parse_radio_csv(path, *, service='OTHER', mapping=None, frequency_unit=None) -> RadioCsvPreview:
    path = Path(path)
    if path.suffix.lower() not in ('.csv', '.tsv', '.txt'):
        raise ValueError('Only text CSV or TSV files are supported')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('File exceeds the 10 MiB limit')
    try:
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('File exceeds the 10 MiB limit')
        content = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('File must be UTF-8 encoded') from exc
    try:
        dialect = csv.Sniffer().sniff(content[:8192], delimiters=',;\t')
    except csv.Error:
        raise ValueError('Could not determine CSV delimiter') from None
    reader = csv.reader(io.StringIO(content, newline=''), dialect, strict=True)
    try:
        headers = tuple(h.strip() for h in next(reader))
    except (StopIteration, csv.Error):
        raise ValueError('Missing or malformed CSV headers') from None
    if not headers or any(not h for h in headers) or len(set(map(_norm, headers))) != len(headers):
        raise ValueError('Headers must be nonempty and unique')
    norm = {_norm(h): h for h in headers}
    if {'title', 'txfreq', 'rxfreq', 'txsubaudioctcssfreqdcsnumber'} <= set(norm):
        fmt = 'BTECH'
    elif {'outputfreq', 'inputfreq', 'uplinktone'} <= set(norm):
        fmt = 'RepeaterBook'
    elif {'frequency', 'duplex', 'offset', 'tone'} <= set(norm):
        fmt = 'CHIRP'
    else:
        fmt = 'Generic'
    if frequency_unit not in (None, 'MHz', 'Hz'):
        raise ValueError('frequency_unit must be MHz, Hz, or None')
    mapping = dict(mapping or {})
    if set(mapping) - set(_ALIASES):
        raise ValueError('Unknown mapping field: ' + ', '.join(sorted(set(mapping) - set(_ALIASES))))
    fields = {}
    for key, aliases in _ALIASES.items():
        if key in mapping:
            if mapping[key] not in headers:
                raise ValueError(f'Mapped header not found: {mapping[key]}')
            fields[key] = mapping[key]
        else:
            matches = list(dict.fromkeys(norm[_norm(a)] for a in aliases if _norm(a) in norm))
            if len(matches) > 1:
                raise ValueError(f'Ambiguous {key} columns: {", ".join(matches)}; provide a mapping')
            if matches:
                fields[key] = matches[0]
    if 'receive_hz' not in fields:
        raise ValueError('Missing receive frequency column; provide a mapping')
    rows = []
    try:
        while True:
            row_number = reader.line_num + 1
            try:
                values = next(reader)
            except StopIteration:
                break
            if not values or not any(v.strip() for v in values):
                continue
            if len(rows) >= MAX_ROWS:
                raise ValueError('File exceeds the 10000 row limit')
            raw = dict(zip(headers, values))
            errors, warnings = [], []
            resource = None
            if len(values) != len(headers):
                errors.append(f'Expected {len(headers)} fields; found {len(values)}')
            else:
                get = lambda name: raw.get(fields.get(name, ''), '').strip()
                try:
                    unit = lambda key: (1 if frequency_unit == 'Hz' else 1000000) if fmt == 'Generic' and frequency_unit is not None else (1 if fmt == 'BTECH' or (_norm(fields.get(key, '')).endswith('hz') and not _norm(fields.get(key, '')).endswith('mhz')) else 1000000)
                    rx = _number(get('receive_hz'), unit('receive_hz'), 'receive frequency')
                    tx = None
                    if fmt == 'CHIRP' or (fmt == 'Generic' and 'duplex' in fields):
                        duplex = get('duplex').lower()
                        if duplex == '':
                            tx = rx
                        elif duplex == 'off':
                            tx = None
                        elif duplex in ('+', '-', 'split'):
                            off = _number(get('offset'), unit('offset') if fmt == 'Generic' else 1000000, 'offset')
                            tx = off if duplex == 'split' else rx + (off if duplex == '+' else -off)
                            if tx <= 0 or tx > 9223372036854775807:
                                raise ValueError('derived transmit frequency is outside the supported range')
                        else:
                            raise ValueError(f'unsupported duplex: {duplex}')
                        tone = _chirp_tone(get, fields) if fmt == 'CHIRP' else None
                    else:
                        if get('transmit_hz'):
                            tx = _number(get('transmit_hz'), unit('transmit_hz'), 'transmit frequency')
                        if fmt == 'BTECH':
                            tx_dis = get('tx_dis')
                            if tx_dis not in ('', '0', '1'):
                                raise ValueError('invalid transmit disable value')
                            if tx_dis == '1':
                                if tx is not None:
                                    warnings.append('Transmit disabled; source transmit frequency retained in raw fields')
                                tx = None
                            talk = get('talk_around')
                            if talk not in ('', '0', '1'):
                                raise ValueError('invalid talk-around value')
                            if talk == '1':
                                warnings.append('Talk-around enabled; consult source for transmit behavior')
                            tx_mod = get('tx_mod')
                            if tx_mod not in ('', '0', '1'):
                                raise ValueError('unsupported BTECH transmit modulation')
                            if tx_mod and tx_mod != get('rx_mod'):
                                warnings.append('Transmit modulation differs from receive modulation; consult source')
                            tones = [(direction, _btech_tone(get(field))) for direction, field in (('TX', 'tx_tone'), ('RX', 'rx_tone'))]
                            tone = '; '.join(f'{d}: {t}' for d, t in tones if t) or None
                        elif fmt == 'RepeaterBook':
                            tones = [(d, _repeaterbook_tone(get(f), warnings)) for d, f in (('TX', 'tx_tone'), ('RX', 'rx_tone'))]
                            tone = '; '.join(f'{d}: {t}' for d, t in tones if t) or None
                        else:
                            tone = '; '.join(f'{d}: {get(f)}' for d, f in (('TX', 'tx_tone'), ('RX', 'rx_tone')) if get(f)) or None
                        if tx is None and fmt == 'Generic':
                            warnings.append('Transmit frequency is unspecified')
                    label = get('label') or f'{Decimal(rx) / 1000000:f} MHz'
                    if fmt == 'RepeaterBook' and get('locality'):
                        label += ' — ' + get('locality')
                    mode = get('mode') or None
                    if fmt == 'BTECH':
                        mod = get('rx_mod')
                        if mod not in ('', '0', '1'):
                            raise ValueError('unsupported BTECH receive modulation')
                        mode = {'0': 'FM', '1': 'AM'}.get(mod)
                    bandwidth = _number(get('bandwidth'), 1, 'bandwidth') if get('bandwidth') else None
                    kind = 'channel' if tx is None else ('repeater' if tx != rx else 'simplex')
                    kwargs = dict(source_key=SOURCE_KEY, resource_kind=kind, service=str(service or '').strip().upper(), label=label,
                                  receive_hz=rx, transmit_hz=tx, offset_hz=abs(tx-rx) if tx is not None and tx != rx else None,
                                  channel=(get('channel') or None) if fmt != 'RepeaterBook' else None, mode=mode, bandwidth_hz=bandwidth, tone=tone,
                                  locality=(get('locality') or None) if fmt != 'CHIRP' else None,
                                  notes=get('comment') or None)
                    stable = json.dumps(kwargs, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                    digest = hashlib.sha256(stable.encode()).hexdigest()
                    provenance = json.dumps({'filename': path.name, 'row': row_number, 'raw_fields': raw}, ensure_ascii=False, sort_keys=True)
                    resource = FrequencyResource(frequency_resource_key='radio_import_' + digest, provenance=provenance, **kwargs)
                except (ValueError, TypeError) as exc:
                    errors.append(str(exc))
            rows.append(RadioCsvRow(row_number, resource, tuple(errors), tuple(warnings), MappingProxyType(raw)))
    except csv.Error as exc:
        raise ValueError(f'Malformed CSV near line {reader.line_num}: {exc}') from exc
    return RadioCsvPreview(fmt, headers, tuple(rows))
