from __future__ import annotations

import re
import os
import shutil
import tempfile
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence


MAPPER_SETTINGS_KEY = "js8_spotter_form_mappings"

PURPOSE_IGNORE = "Ignore"
PURPOSE_GENERIC = "Generic Message"
PURPOSE_NET_CHECKIN = "Net Check-in"
PURPOSE_NET_NOTIFICATION = "Net Notification"
PURPOSE_SITREP = "SitRep / StatRep"
PURPOSE_WEATHER = "Weather / Storm"
PURPOSE_HAZARD = "Hazard / Early Warning"
PURPOSE_INTEL = "Intel / RFI"
PURPOSE_SUPPLY = "Supply / Area Assessment"
PURPOSE_MEDICAL = "Medical / Hospital"
PURPOSE_STATION = "Station Capability"
PURPOSE_INFRASTRUCTURE = "Infrastructure Status"
PURPOSE_CUSTOM = "Custom"

PURPOSE_OPTIONS: Sequence[str] = (
    PURPOSE_IGNORE,
    PURPOSE_GENERIC,
    PURPOSE_NET_CHECKIN,
    PURPOSE_NET_NOTIFICATION,
    PURPOSE_SITREP,
    PURPOSE_WEATHER,
    PURPOSE_HAZARD,
    PURPOSE_INTEL,
    PURPOSE_SUPPLY,
    PURPOSE_MEDICAL,
    PURPOSE_STATION,
    PURPOSE_INFRASTRUCTURE,
    PURPOSE_CUSTOM,
)

ALPHABETIC_FORM_IDS = frozenset({"BDN"})
FORM_ID_PATTERN = rf"(?:[0-9]{{3}}[A-Z]?|{'|'.join(sorted(ALPHABETIC_FORM_IDS))})"
FORM_TOKEN_RE = re.compile(rf"\bF!{FORM_ID_PATTERN}\b", re.IGNORECASE)
FORM_FILE_RE = re.compile(rf"^MCF({FORM_ID_PATTERN})$", re.IGNORECASE)


@dataclass(frozen=True)
class SpotterFormDefinition:
    form_code: str
    title: str
    path: str = ""


@dataclass(frozen=True)
class SpotterFormField:
    key: str
    label: str
    options: tuple[tuple[str, str], ...] = ()
    kind: str = "choice"
    default_value: str = ""
    description: str = ""


@dataclass(frozen=True)
class SpotterFormsCatalogChoice:
    """Pure selection of the catalog an operator-facing consumer should use.

    Selecting a source is intentionally separate from checking that its folder
    exists or reading its files.  Render paths can therefore use this model
    without filesystem I/O, while an explicit refresh/worker owns discovery.
    """

    path: str
    source: str
    ownership: str


@dataclass(frozen=True)
class SpotterFormsImportItem:
    source: str
    name: str
    form_code: str
    size: int
    valid: bool
    duplicate: bool = False
    reason: str = ""
    source_digest: str = ""


@dataclass(frozen=True)
class SpotterFormsImportPreview:
    items: tuple[SpotterFormsImportItem, ...]
    existing: tuple[str, ...]
    source_count: int
    valid_count: int
    duplicate_count: int
    rejected_count: int
    bounded: bool = False
    existing_bounded: bool = False


@dataclass(frozen=True)
class SpotterFormsImportResult:
    target_dir: str
    copied: tuple[str, ...]
    skipped: tuple[str, ...]
    errors: tuple[str, ...]
    committed: bool


MAX_SPOTTER_IMPORT_FILES = 200
MAX_SPOTTER_FORM_BYTES = 1024 * 1024
CP_FORMS_LIBRARY_NAME = "spotter_forms"


SPOTTER_COMMENTS_KEY = "COMMENTS"
SPOTTER_COMMENTS_MAX_LENGTH = 50

# These are intentionally explicit form semantics.  A generic label match can
# incorrectly put the operator's QTH into an incident, affected-area, or
# "other area" field.  Expand this table only after reviewing the form text.
_OPERATOR_AUTOFILL_FIELDS: Mapping[str, Mapping[str, str]] = {
    "F!104": {"ST": "state", "GR": "grid"},
    "F!105": {"CS": "callsign", "ST": "state", "GR": "grid"},
    "F!108": {"ST": "state", "GR": "grid"},
    "F!306": {"ST": "state", "GR": "grid"},
    "F!500": {"ST": "state", "GR": "grid"},
    "F!504": {"ST": "state", "GR": "grid"},
    "F!701A": {"FR": "callsign"},
    "F!701C": {"ST": "state", "GR": "grid"},
    "F!BDN": {"GR": "grid"},
}


def bundled_spotter_forms_dir() -> Path:
    """Return FIO's packaged station-level Spotter form catalog."""

    # Keep source selection pure: resolving this packaged path must not stat
    # or create anything. Discovery performs filesystem checks explicitly.
    return Path(__file__).parent.parent / "resources" / "spotter_forms"


def choose_effective_spotter_forms_path(
    global_path: object = "",
    legacy_radio_path: object = "",
    bundled_path: object = None,
) -> str:
    """Choose the active catalog path without touching the filesystem.

    The station-global CP Forms Library (`js8_forms_path`) wins whenever it is
    explicitly set.  A per-radio forms path is retained only as a clearly
    legacy fallback for stations that have not selected a global catalog.
    """

    global_text = str(global_path or "").strip()
    if global_text:
        return global_text
    legacy_text = str(legacy_radio_path or "").strip()
    if legacy_text:
        return legacy_text
    return str(bundled_path if bundled_path is not None else bundled_spotter_forms_dir())


def choose_effective_spotter_forms_catalog(
    global_path: object = "",
    legacy_radio_path: object = "",
    bundled_path: object = None,
) -> SpotterFormsCatalogChoice:
    """Return source and ownership metadata for the pure path choice."""

    global_text = str(global_path or "").strip()
    if global_text:
        return SpotterFormsCatalogChoice(global_text, "global", "CommsPlan Forms Library")
    legacy_text = str(legacy_radio_path or "").strip()
    if legacy_text:
        return SpotterFormsCatalogChoice(legacy_text, "legacy-radio", "Legacy per-radio catalog")
    bundled = str(bundled_path if bundled_path is not None else bundled_spotter_forms_dir())
    return SpotterFormsCatalogChoice(bundled, "bundled", "Bundled CommsPlan catalog")


# Descriptive aliases keep callers readable while allowing tests/integrations
# to use either the path or metadata form of the helper.
effective_spotter_forms_path = choose_effective_spotter_forms_path
effective_spotter_forms_catalog = choose_effective_spotter_forms_catalog


def cp_spotter_forms_library_dir(config_root: object = None) -> Path:
    """Return the station-owned CP library location without creating it."""

    root = Path(config_root).expanduser() if config_root else None
    if root is None:
        from freqinout.core.config_paths import get_config_dir

        root = get_config_dir() / "config"
    return root / CP_FORMS_LIBRARY_NAME


def _import_source_paths(sources: Iterable[object]) -> tuple[list[Path], bool]:
    paths: list[Path] = []
    bounded = False
    for raw in sources or ():
        try:
            path = Path(raw).expanduser()
        except (OSError, TypeError, ValueError):
            continue
        if path.is_dir():
            children, exceeded = _bounded_catalog_files(path)
            bounded = bounded or exceeded
            paths.extend(children)
        elif path.is_file() and path.suffix.lower() == ".txt" and FORM_FILE_RE.fullmatch(path.stem):
            paths.append(path)
        if len(paths) > MAX_SPOTTER_IMPORT_FILES:
            bounded = True
            paths = paths[:MAX_SPOTTER_IMPORT_FILES]
            break
    # A file selected twice or supplied by a folder and file selection is one
    # candidate, preserving source order while avoiding duplicate reads.
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique, bounded


def _bounded_catalog_files(root: Path, limit: int = MAX_SPOTTER_IMPORT_FILES) -> tuple[list[Path], bool]:
    """Read at most ``limit + 1`` MCF entries so overlarge catalogs fail closed."""

    files: list[Path] = []
    try:
        for entry in root.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".txt" and FORM_FILE_RE.fullmatch(entry.stem):
                files.append(entry)
                if len(files) > limit:
                    return files[:limit], True
    except OSError:
        return files, False
    return sorted(files, key=lambda item: item.name.upper()), False


def preview_spotter_forms_import(
    sources: Iterable[object],
    *,
    existing_dir: object = None,
    existing_dirs: Iterable[object] = (),
    max_files: int = MAX_SPOTTER_IMPORT_FILES,
    max_bytes: int = MAX_SPOTTER_FORM_BYTES,
) -> SpotterFormsImportPreview:
    """Validate a bounded list of existing MCF files without changing state."""

    paths, bounded = _import_source_paths(sources)
    if max_files > 0 and len(paths) > max_files:
        paths = paths[:max_files]
        bounded = True
    existing_names: set[str] = set()
    existing_bounded = False
    directories = [existing_dir, *(existing_dirs or ())]
    for directory in directories:
        if not directory:
            continue
        try:
            root = Path(directory).expanduser()
            if root.is_dir():
                files, exceeded = _bounded_catalog_files(root)
                existing_bounded = existing_bounded or exceeded
                existing_names.update(p.name.casefold() for p in files)
        except (OSError, TypeError, ValueError):
            continue
    items: list[SpotterFormsImportItem] = []
    seen_names: set[str] = set(existing_names)
    for path in paths:
        name = path.name
        form_code = normalize_form_code(path.stem)
        valid = True
        reason = ""
        raw = b""
        try:
            size = int(path.stat().st_size)
        except OSError as exc:
            size = 0
            valid = False
            reason = f"Cannot read file: {exc}"
        if valid and size > max_bytes:
            valid = False
            reason = f"File exceeds {max_bytes} byte limit"
        if valid:
            try:
                with path.open("rb") as stream:
                    raw = stream.read(max_bytes + 1)
            except OSError as exc:
                valid = False
                reason = f"Cannot read file: {exc}"
            else:
                if len(raw) != size:
                    valid = False
                    reason = "File changed while it was being reviewed"
        if valid and not form_code:
            valid = False
            reason = "Filename is not a supported MCF form"
        digest = hashlib.sha256(raw).hexdigest() if valid else ""
        if valid:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                valid = False
                reason = "Form is not valid UTF-8"
            else:
                if not text.strip():
                    valid = False
                    reason = "Form is empty"
                elif form_code not in extract_form_codes(text):
                    valid = False
                    reason = "Form header does not match its MCF filename"
                elif not parse_spotter_form_fields(text):
                    valid = False
                    reason = "Form contains no supported fields"
        duplicate = name.casefold() in seen_names
        if duplicate and valid:
            reason = "Already present in CP Forms Library"
        if valid:
            seen_names.add(name.casefold())
        items.append(SpotterFormsImportItem(str(path), name, form_code, size, valid, duplicate, reason, digest))
    valid_count = sum(1 for item in items if item.valid and not item.duplicate)
    return SpotterFormsImportPreview(
        tuple(items), tuple(sorted(existing_names)), len(items), valid_count,
        sum(1 for item in items if item.duplicate), sum(1 for item in items if not item.valid), bounded,
        existing_bounded,
    )


def import_spotter_forms_to_library(
    preview: SpotterFormsImportPreview,
    *,
    target_dir: object,
    preserve_dir: object = None,
) -> SpotterFormsImportResult:
    """Copy reviewed forms into a CP-owned catalog with rollback on failure.

    ``preserve_dir`` is an existing valid catalog selected by the operator. Its
    files are copied into the staged CP library before imported files. Source
    folders are never modified and existing target names are never overwritten.
    """

    target = Path(target_dir).expanduser()
    copied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    if preview.bounded:
        errors.append("Import exceeds the bounded file limit")
    if preview.existing_bounded:
        errors.append("An existing catalog exceeds the bounded file limit")
    if len(preview.existing) + preview.valid_count > MAX_SPOTTER_IMPORT_FILES:
        errors.append("Combined catalog exceeds the bounded file limit")
    if preview.rejected_count:
        errors.extend(
            f"{item.name}: {item.reason or 'invalid form'}"
            for item in preview.items
            if not item.valid
        )
    if errors:
        return SpotterFormsImportResult(str(target), (), (), tuple(errors), False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
    except OSError as exc:
        return SpotterFormsImportResult(str(target), (), (), (f"Cannot prepare CP Forms Library: {exc}",), False)
    created: list[Path] = []
    try:
        # Preserve an already valid catalog in the staging tree.  Existing
        # target files are copied as-is into staging and never replaced.
        preserve = Path(preserve_dir).expanduser() if preserve_dir else None
        preserve_files: list[Path] = []
        if preserve and preserve.is_dir():
            preserve_files, preserve_bounded = _bounded_catalog_files(preserve)
            if preserve_bounded:
                return SpotterFormsImportResult(str(target), (), (), ("Existing catalog exceeds the bounded file limit",), False)
        existing_files, target_bounded = _bounded_catalog_files(target) if target.is_dir() else ([], False)
        if target_bounded:
            return SpotterFormsImportResult(str(target), (), (), ("CP Forms Library exceeds the bounded file limit",), False)
        staged_names = set()
        for source in [*existing_files, *preserve_files]:
            destination = staging / source.name
            if source.name.casefold() in staged_names:
                continue
            with source.open("rb") as stream:
                preserved = stream.read(MAX_SPOTTER_FORM_BYTES + 1)
            if len(preserved) > MAX_SPOTTER_FORM_BYTES:
                raise RuntimeError(f"{source.name}: existing form exceeds the bounded size limit")
            destination.write_bytes(preserved)
            staged_names.add(source.name.casefold())
        for item in preview.items:
            destination = staging / item.name
            if item.name.casefold() in staged_names or item.duplicate:
                skipped.append(item.name)
                continue
            source_path = Path(item.source)
            try:
                with source_path.open("rb") as stream:
                    current = stream.read(MAX_SPOTTER_FORM_BYTES + 1)
            except OSError as exc:
                raise RuntimeError(f"{item.name}: source could not be reread ({exc})") from exc
            if len(current) != item.size or hashlib.sha256(current).hexdigest() != item.source_digest:
                raise RuntimeError(f"{item.name}: source changed after review; preview it again")
            destination.write_bytes(current)
            staged_names.add(item.name.casefold())
        # Publish each new file atomically, checking again to preserve the
        # no-overwrite contract if another process changed the directory.
        target.mkdir(parents=True, exist_ok=True)
        for staged in _bounded_catalog_files(staging, MAX_SPOTTER_IMPORT_FILES * 3)[0]:
            destination = target / staged.name
            if destination.exists():
                continue
            try:
                # Linking a complete staged file publishes it atomically and
                # refuses to replace a concurrent/pre-existing destination.
                os.link(str(staged), str(destination))
                created.append(destination)
            except FileExistsError:
                continue
            except OSError:
                # Filesystems without hard-link support still get an
                # exclusive destination and immediate rollback registration.
                try:
                    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    created.append(destination)
                    with os.fdopen(fd, "wb") as out, staged.open("rb") as source:
                        shutil.copyfileobj(source, out)
                        out.flush()
                        os.fsync(out.fileno())
                except FileExistsError:
                    if destination in created:
                        created.remove(destination)
                    continue
            copied.append(staged.name)
        return SpotterFormsImportResult(str(target), tuple(copied), tuple(skipped), (), True)
    except Exception as exc:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        return SpotterFormsImportResult(str(target), (), tuple(skipped), tuple(errors + [str(exc)]), False)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def resolve_spotter_forms_dir(configured_dir: object = None) -> Path:
    """Resolve an optional advanced custom catalog, then the bundled catalog.

    The normal guided path deliberately supplies no per-radio folder.  A
    configured custom catalog remains an advanced station-level override, but
    an empty, missing, or invalid override always falls back to FIO's catalog.
    """

    configured = str(configured_dir or "").strip()
    if configured:
        try:
            candidate = Path(configured).expanduser()
            if candidate.exists() and candidate.is_dir():
                return candidate
        except (OSError, TypeError, ValueError):
            pass
    return bundled_spotter_forms_dir()


def resolve_effective_spotter_forms_dir(
    global_path: object = "",
    legacy_radio_path: object = "",
    bundled_path: object = None,
) -> Path:
    """Resolve the chosen catalog at an explicit read/worker boundary.

    An explicitly selected global or legacy path remains authoritative even
    when it is currently missing; callers can then show an honest empty/error
    state instead of silently switching catalog ownership.
    """

    return Path(choose_effective_spotter_forms_path(global_path, legacy_radio_path, bundled_path)).expanduser()


def discover_effective_spotter_forms(
    global_path: object = "",
    legacy_radio_path: object = "",
    bundled_path: object = None,
) -> List[SpotterFormDefinition]:
    """Discover forms from one resolved effective source, without fallback."""

    root = resolve_effective_spotter_forms_dir(global_path, legacy_radio_path, bundled_path)
    if not root.exists() or not root.is_dir():
        return []
    out: List[SpotterFormDefinition] = []
    for path in _bounded_catalog_files(root)[0]:
        match = FORM_FILE_RE.fullmatch(path.stem)
        if not match:
            continue
        out.append(SpotterFormDefinition(f"F!{match.group(1).upper()}", _read_form_title(path), str(path)))
    return out


def normalize_form_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text.startswith("MCF"):
        text = "F!" + text[3:]
    elif text.startswith("F!"):
        pass
    elif re.fullmatch(r"F[0-9]{3}[A-Z]?", text):
        text = "F!" + text[1:]
    elif text[0].isdigit():
        text = f"F!{text}"
    elif text in ALPHABETIC_FORM_IDS:
        text = f"F!{text}"
    match = FORM_TOKEN_RE.search(text)
    return match.group(0).upper() if match else ""


def extract_form_codes(text: object) -> List[str]:
    found = [normalize_form_code(match.group(0)) for match in FORM_TOKEN_RE.finditer(str(text or ""))]
    return list(dict.fromkeys(code for code in found if code))


def _read_form_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                clean = line.strip()
                if not clean:
                    continue
                return clean.split("|", 1)[0].strip() if "|" in clean else clean
    except Exception:
        return ""
    return ""


def discover_spotter_forms(forms_dir: object) -> List[SpotterFormDefinition]:
    try:
        root = resolve_spotter_forms_dir(forms_dir)
    except Exception:
        return []
    if not root.exists() or not root.is_dir():
        return []
    out: List[SpotterFormDefinition] = []
    for path in sorted(root.glob("MCF*.txt"), key=lambda item: item.name.upper()):
        match = FORM_FILE_RE.fullmatch(path.stem)
        if not match:
            continue
        form_code = f"F!{match.group(1).upper()}"
        out.append(SpotterFormDefinition(form_code=form_code, title=_read_form_title(path), path=str(path)))
    return out


def parse_spotter_form_fields(text: object) -> List[SpotterFormField]:
    """Parse the editable portion of a JS8Spotter MCForms definition.

    Choice questions, their explicit ``*`` defaults, and ``[XX]`` structured
    prompts are retained in source order.  Heading/instruction text immediately
    before a field is attached as guidance instead of being silently discarded.
    The FIO-provided universal Comments field is added by the Compose surface,
    not by this catalog parser.
    """
    fields: List[SpotterFormField] = []
    current_label = ""
    current_options: List[tuple[str, str]] = []
    current_default = ""
    pending_guidance: List[str] = []
    current_guidance: List[str] = []

    def flush() -> None:
        nonlocal current_label, current_options, current_default, current_guidance
        label = current_label.strip()
        if not label:
            current_options = []
            current_default = ""
            current_guidance = []
            return
        key_base = re.sub(r"[^A-Za-z0-9]+", "_", label.upper()).strip("_") or f"FIELD_{len(fields) + 1}"
        key = key_base
        suffix = 2
        existing = {field.key for field in fields}
        while key in existing:
            key = f"{key_base}_{suffix}"
            suffix += 1
        fields.append(
            SpotterFormField(
                key=key,
                label=label,
                options=tuple(current_options),
                kind="choice",
                default_value=current_default,
                description=" ".join(part for part in current_guidance if part).strip(),
            )
        )
        current_label = ""
        current_options = []
        current_default = ""
        current_guidance = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("?"):
            flush()
            current_label = line[1:].strip().strip(":")
            current_guidance = list(pending_guidance)
            pending_guidance = []
            continue
        if line.startswith("@") and current_label:
            parts = line[1:].strip().split(maxsplit=1)
            if not parts:
                continue
            token = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else token
            selected = label.startswith("*")
            if selected:
                label = label[1:].lstrip()
            if token:
                current_options.append((token, label))
                if selected:
                    current_default = token
            continue
        prompt_match = re.match(r"^\[([A-Z0-9]{2})\]\s*(.*?)\s*$", line, flags=re.IGNORECASE)
        if prompt_match:
            flush()
            key = prompt_match.group(1).upper()
            label = prompt_match.group(2).strip().rstrip(":") or key
            fields.append(
                SpotterFormField(
                    key=key,
                    label=label,
                    kind="prompt",
                    description=" ".join(part for part in pending_guidance if part).strip(),
                )
            )
            pending_guidance = []
            continue
        if line.startswith(("!", ".")):
            guidance = line.lstrip("!.").strip()
            if guidance:
                pending_guidance.append(guidance)
            continue
    flush()
    return fields


def parse_spotter_form_guidance(text: object) -> tuple[str, ...]:
    """Return concise operator-facing headings/instructions in source order."""
    guidance: List[str] = []
    for raw_line in str(text or "").splitlines()[1:]:
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(("!", ".")):
            continue
        clean = line.lstrip("!.").strip()
        if not clean or set(clean) <= {"-", "_"}:
            continue
        lower = clean.lower()
        if lower.startswith("version") or lower.startswith("date:") or lower.startswith("more info"):
            continue
        if clean not in guidance:
            guidance.append(clean)
    return tuple(guidance)


def spotter_operator_autofill_kind(form_code: object, field_key: object) -> str:
    """Return the reviewed operator-identity default for a form field.

    An empty result is a deliberate deny.  In particular, affected-area,
    incident, assessment, destination, wildfire, medivac, and "other area"
    fields must stay operator-entered.
    """
    code = normalize_form_code(form_code)
    key = str(field_key or "").strip().upper()
    return str(_OPERATOR_AUTOFILL_FIELDS.get(code, {}).get(key, "") or "")


def factory_mapping_for_form(form_code: object, title: object = "") -> Dict[str, object]:
    code = normalize_form_code(form_code)
    title_upper = str(title or "").upper()
    purpose = PURPOSE_GENERIC
    messages = True
    map_use = False
    alert = False
    net = False
    status = False

    if code in {"F!103", "F!700", "F!702A"} or "CHECKIN" in title_upper or "CHECK-IN" in title_upper:
        purpose = PURPOSE_NET_CHECKIN
        net = True
        status = code in {"F!104", "F!701C"}
    elif (
        code in {"F!104", "F!300", "F!301", "F!304", "F!701", "F!701A", "F!701B"}
        or "SITREP" in title_upper
        or "STATREP" in title_upper
        or "STAT-REP" in title_upper
        or "STATUS REPORT" in title_upper
        or "SITUATION REPORT" in title_upper
    ):
        purpose = PURPOSE_SITREP
        map_use = True
        status = code in {"F!104", "F!301", "F!304", "F!701B", "F!701C"}
    elif code in {"F!106", "F!108"} or "NET NOTICE" in title_upper or "NOTIFICATION" in title_upper:
        purpose = PURPOSE_NET_NOTIFICATION
        alert = True
        net = True
    elif code in {"F!305"} or "EARLY WARNING" in title_upper or "ALERT" in title_upper:
        purpose = PURPOSE_HAZARD
        map_use = True
        alert = True
    elif code in {"F!307", "F!504"} or "WEATHER" in title_upper or "WILDFIRE" in title_upper or "STORM" in title_upper:
        purpose = PURPOSE_WEATHER
        map_use = True
        alert = code == "F!307"
    elif code in {"F!105", "F!107", "F!308", "F!701"} or "RFI" in title_upper or "INTEL" in title_upper or "OBSERVATION" in title_upper:
        purpose = PURPOSE_INTEL
        map_use = code in {"F!701"}
        alert = code in {"F!107"}
    elif code in {"F!500", "F!505", "F!BDN"} or "SUPPLY" in title_upper or "AREA ASSESSMENT" in title_upper or "PRICE SURVEY" in title_upper:
        purpose = PURPOSE_SUPPLY
        map_use = True
    elif code in {"F!302", "F!303", "F!703"} or "HOSPITAL" in title_upper or "MEDIVAC" in title_upper or "MEDICAL" in title_upper:
        purpose = PURPOSE_MEDICAL
        map_use = True
        alert = True
    elif code in {"F!100", "F!101", "F!102", "F!702"} or "STATION" in title_upper or "EQUIPMENT" in title_upper:
        purpose = PURPOSE_STATION
    elif code in {"F!306"} or "POWER" in title_upper or "INFRASTRUCTURE" in title_upper:
        purpose = PURPOSE_INFRASTRUCTURE
        map_use = True
        status = True

    return {
        "form_code": code,
        "title": str(title or "").strip(),
        "purpose": purpose,
        "messages": messages,
        "map": map_use,
        "alert": alert,
        "net": net,
        "status": status,
        "use_for_nets": True,
        "operating_group_keys": [],
        "preferred_query_for": [],
        "preferred_checkin_for": [],
    }


def normalize_mapping_row(row: Mapping[str, object], *, title: str = "") -> Dict[str, object]:
    code = normalize_form_code(row.get("form_code") or row.get("form") or row.get("code"))
    base = factory_mapping_for_form(code, title or row.get("title", ""))
    purpose = str(row.get("purpose", base["purpose"]) or base["purpose"]).strip()
    if purpose not in PURPOSE_OPTIONS:
        purpose = str(base["purpose"])
    base.update(
        {
            "form_code": code,
            "title": str(row.get("title", title or base.get("title", "")) or "").strip(),
            "purpose": purpose,
            "messages": bool(row.get("messages", base["messages"])),
            "map": bool(row.get("map", base["map"])),
            "alert": bool(row.get("alert", base["alert"])),
            "net": bool(row.get("net", base["net"])),
            "status": bool(row.get("status", base["status"])),
        }
    )
    base["use_for_nets"] = bool(row.get("use_for_nets", True))
    for key in ("operating_group_keys", "preferred_query_for", "preferred_checkin_for"):
        values = row.get(key, [])
        base[key] = list(dict.fromkeys(value.strip() for value in values
                                       if isinstance(value, str) and value.strip())) if isinstance(values, (list, tuple)) else []
    if purpose == PURPOSE_IGNORE:
        base.update({"messages": False, "map": False, "alert": False, "net": False, "status": False})
    return base


def normalize_mapping_rows(rows: object) -> List[Dict[str, object]]:
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        norm = normalize_mapping_row(row)
        code = str(norm.get("form_code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(norm)
    return out


def effective_mapping_rows(settings, forms_dir: object = None) -> List[Dict[str, object]]:
    configured = {}
    try:
        raw_rows = settings.get(MAPPER_SETTINGS_KEY, []) if settings is not None else []
    except Exception:
        raw_rows = []
    for row in normalize_mapping_rows(raw_rows):
        configured[str(row.get("form_code") or "")] = row

    if forms_dir is None and settings is not None:
        try:
            forms_dir = settings.get("js8_forms_path", "")
        except Exception:
            forms_dir = ""

    out: List[Dict[str, object]] = []
    seen: set[str] = set()
    # Empty means the bundled catalog. Once an operator has explicitly chosen
    # a station path, that path remains authoritative and a missing folder is
    # represented as empty rather than silently replacing it with bundled
    # forms.
    definitions = (
        discover_effective_spotter_forms(forms_dir)
        if str(forms_dir or "").strip()
        else discover_spotter_forms("")
    )
    for definition in definitions:
        base = factory_mapping_for_form(definition.form_code, definition.title)
        row = dict(configured.get(definition.form_code, base))
        row["form_code"] = definition.form_code
        row["title"] = str(row.get("title") or definition.title or base.get("title", "") or "").strip()
        out.append(normalize_mapping_row(row, title=definition.title))
        seen.add(definition.form_code)

    for code, row in configured.items():
        if code not in seen:
            out.append(normalize_mapping_row(row))
    return out


@dataclass(frozen=True)
class NetFormOptions:
    options: tuple[tuple[str, str], ...]
    preferred: str = ""
    ambiguous: bool = False
    missing_preferred: tuple[str, ...] = ()


def resolve_net_form_options(definitions, mappings, *, group_key="", preference="query") -> NetFormOptions:
    """Resolve a catalog snapshot without filesystem access or inferred identities."""
    configured = {row["form_code"]: row for row in normalize_mapping_rows(mappings)}
    group_key = str(group_key or "").strip()
    preference_key = "preferred_checkin_for" if preference == "checkin" else "preferred_query_for"
    available = []
    present = set()
    for definition in definitions:
        code = normalize_form_code(definition.form_code)
        if not code or code in present:
            continue
        present.add(code)
        row = configured.get(code, factory_mapping_for_form(code, definition.title))
        if row["purpose"] == PURPOSE_IGNORE or not row["use_for_nets"]:
            continue
        label = code + (" - " + definition.title if definition.title else "")
        associated = row["operating_group_keys"]
        relevance = 0 if group_key and group_key in associated else 1 if not associated else 2
        available.append((relevance, code, label, row))
    available.sort(key=lambda item: (item[0], item[1]))
    preferred = [code for _, code, _, row in available if group_key and group_key in row[preference_key]]
    missing = tuple(sorted(code for code, row in configured.items()
                           if group_key and group_key in row[preference_key] and code not in {item[1] for item in available}))
    return NetFormOptions(tuple((code, label) for _, code, label, _ in available),
                          preferred[0] if len(preferred) == 1 and not missing else "",
                          len(preferred) + len(missing) > 1, missing)


def forms_enabled_for(settings, *, purpose: str = "", flag: str = "") -> set[str]:
    rows = effective_mapping_rows(settings)
    out: set[str] = set()
    for row in rows:
        if purpose and str(row.get("purpose") or "") != purpose:
            continue
        if flag and not bool(row.get(flag, False)):
            continue
        code = str(row.get("form_code") or "").strip().upper()
        if code:
            out.add(code)
    return out


def custom_mapper_configured(settings) -> bool:
    try:
        raw_rows = settings.get(MAPPER_SETTINGS_KEY, []) if settings is not None else []
    except Exception:
        return False
    return isinstance(raw_rows, list) and bool(raw_rows)


def form_codes_enabled_for(settings, *, flag: str) -> set[str] | None:
    if not custom_mapper_configured(settings):
        return None
    return forms_enabled_for(settings, flag=flag)


def form_id_enabled(form_id: object, enabled_codes: set[str] | None) -> bool:
    if enabled_codes is None:
        return True
    code = normalize_form_code(form_id)
    return bool(code and code in enabled_codes)


def legacy_default_forms_for(*, purpose: str = "", flag: str = "") -> set[str]:
    defaults = [
        factory_mapping_for_form("F!103", "Net Checkin"),
        factory_mapping_for_form("F!104", "@SITREP Basic Check-in"),
        factory_mapping_for_form("F!106", "Impromptu Net Notice"),
    ]
    out: set[str] = set()
    for row in defaults:
        if purpose and str(row.get("purpose") or "") != purpose:
            continue
        if flag and not bool(row.get(flag, False)):
            continue
        out.add(str(row["form_code"]))
    return out
