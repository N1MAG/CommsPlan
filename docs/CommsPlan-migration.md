# Moving from FIO to CommsPlan

CommsPlan is the new product name for FreqInOut. The rename keeps the existing
profile locations, databases, credential-store identity and shared application
lock. Existing `FREQINOUT_CONFIG_DIR` overrides still select the same profile.
Do not rename your profile folder or databases to match the new product name.

CommsPlan 2.0.5 is the first release under the CommsPlan name. The supported
Windows transition baseline is **FreqInOut 2.0.4 → CommsPlan 2.0.5**. Historical
2.0.4 package references identify the prior installation and recovery material.

This guide describes source migration and native preparation procedures. It
preserves explicit source-only steps; historical native replacement and rollback
remain bounded by the platform procedures and operator checks below. Keep the
historical application, package or installer, and a verified backup available
until the station is verified.

The supported Windows migration baseline is **2.0.4 → CommsPlan**. If you are
running an older Windows release, first back up your station and upgrade using
the existing [FreqInOut 2.0.4 release](https://github.com/N1MAG/FreqInOut/releases/tag/v2.0.4)
and its normal upgrade procedure. Open 2.0.4 and verify your radios, paths,
schedules, messages and other station configuration before starting CommsPlan
preparation. Keep the pre-upgrade backup and historical installer for recovery.
The tool checks the selected installation; it refuses older native versions
rather than treating them as 2.0.4. Direct older-Windows-to-CommsPlan migration
is outside this procedure. Windows 1.2.8 stations must first use the historical
FreqInOut 2.0.4 intermediate release. The 1.2.8 source procedure remains
available for Linux and other source installations; preservation and rollback
checks remain required.

## Retained storage and compatibility identities

The product rename does not rename station storage or platform identities. Existing
profiles remain under their historical roots (`%LOCALAPPDATA%\FreqInOut` or
`%APPDATA%\FreqInOut` on Windows and `~/.freqinout` on macOS and Linux) unless
`FREQINOUT_CONFIG_DIR` selects another root. The databases remain
`freqinout.db` and `freqinout_nets.db`; the Windows installer retains
`AppId=FreqInOut`; the Linux package ID remains `freqinout`; and the macOS bundle
identifier remains `org.n1mag.freqinout`. Preserve the `freqinout` package,
desktop, and command compatibility routes during migration.

## Frozen-package backup evidence

The first startup of a frozen CommsPlan package performs a guarded backup before
the package can write station schema or database changes. It must own the
station lock, select a physical profile root, find both `freqinout.db` and
`freqinout_nets.db`, and reject linked configuration or database files. The raw
`config` tree is copied as a cold backup, including pending SQLite WAL or
journal sidecars. SQLite checks run against a disposable validation copy so the
raw backup is not opened or mutated; source and copied-file hashes are checked
again before success.

This package-startup backup covers only the selected profile's `config` tree.
It does not include the rest of the profile, credentials, logs, caches, or
companion-application data; the migration helper's persistent-profile backup
has broader coverage.

The profile receipt is
`<PROFILE_ROOT>/.commsplan-packaged-startup.json` (for example,
`~/.commsplan/.commsplan-packaged-startup.json` for that selected root). It
records the actual backup
directory under `<PROFILE_ROOT>/backups/` and the manifest digest. Repeated
launches of the same package version reuse that receipt after checking the
recorded path and manifest identity. Once a receipt exists, a malformed,
changed, or linked receipt; a missing or mismatched recorded manifest or backup;
or unsafe station databases in an existing or previously captured profile stops
startup. A missing receipt is normal on first launch and starts a new verified
capture. A new profile records `fresh-profile`; when its databases later appear,
the guard captures and records a verified backup once. This startup evidence
supports recovery preparation; it does not replace the platform procedures and
operator checks for historical native replacement or rollback.

## Migration preparation tool

The initial tool supports explicitly selected **source installations** whose
version metadata says 1.2.8 or 2.0.4. That metadata is recorded and hashed; it
does not prove the checkout is an unmodified historical release. Frozen/native
legacy installations and automatic discovery are outside the guided source
migration procedure. Native 2.0.4 preparation is described below and does not
use the source session commands. Native replacement/rollback remains a separate
platform procedure with operator checks. The helper never runs an installer or
removes old software.

Run it with the CommsPlan source installation's Python environment after its
requirements are installed. Close FIO, CommsPlan and companion applications.
The profile argument is the existing root containing `config/`, rather than the
`config/` directory itself. Select physical paths without symlinks. In these
examples, replace each uppercase placeholder with the path you have confirmed:

```sh
python start-commsplan-migration.py inspect --installation "OLD_SOURCE" --profile "PROFILE_ROOT"
python start-commsplan-migration.py backup --installation "OLD_SOURCE" --profile "PROFILE_ROOT" --destination "EXISTING_BACKUP_DIRECTORY"
python start-commsplan-migration.py verify-backup "BACKUP_DIRECTORY_REPORTED_BY_TOOL"
```

The helper takes the application's existing profile lock, checks both databases,
copies persistent profile files, and verifies source/copy hashes and database
contents. It excludes logs, caches, downloads, previous backups and the lock.
It leaves external companion data and the OS credential store in place; the
backup is **not** a complete computer or credential backup. Keep using the same
OS account. Close external writers before backing up: they do not share the
application lock. Pending SQLite journals, linked data, changed files or a
failed copy stop preparation. Incomplete backup folders are retained and marked
`INCOMPLETE.txt`; they cannot be used for recovery by this helper.

## Guided source migration with interruption handling

The guided source migration supports an explicitly selected historical source installation
and a **separate installed CommsPlan source tree**, with a verified source-install
receipt. Keep the historical source and its working environment intact for
rollback. The session directory must be new and outside both installation and
profile directories; its parent must already exist.

```sh
python start-commsplan-migration.py begin --installation "OLD_SOURCE" --profile "PROFILE_ROOT" --session "NEW_SESSION_DIRECTORY"
python start-commsplan-migration.py backup-session --session "SESSION_DIRECTORY"
```

The tool records hashes of historical runtime files and relevant install inputs,
then creates and verifies a backup inside the session directory. It does not
execute the historical code. These fingerprints bind the selected local files;
they do not prove they match an official historical release.

Next, manually install CommsPlan 2.0.5 in a separate directory using its supported
source installer, explicitly selecting the same `FREQINOUT_CONFIG_DIR`. The
installer must produce a valid `.freqinout-install-verified.json` receipt.
Native installer actions are not substituted for this source-only procedure.

```sh
python start-commsplan-migration.py installed --session "SESSION_DIRECTORY" --target "INSTALLED_COMMSPLAN_SOURCE"
python start-commsplan-migration.py launch-instructions --session "SESSION_DIRECTORY"
```

`installed` verifies CommsPlan metadata, source fingerprints, interpreter and
requirements receipt, and receipt profile selection. It records the handoff;
it does not claim the station upgrade succeeded. The CommsPlan source tree cannot
overlap the historical source, profile or session directory. A newer profile
migration state cannot be opened by an older application.

`launch-instructions` prints the selected launcher, explicit profile environment
and arguments as separate JSON fields. Apply that environment and launch the
printed application with `--migration-session SESSION_DIRECTORY`. The helper
never launches it automatically. CommsPlan keeps the existing informed
station-upgrade dialog and existing station automation policy. Review startup
application/scheduler behavior and isolate radios/companions for controlled tests.
No unattended hardware test is authorized by this procedure.

The application validates session/profile/source binding **before database
initialization**. A profile edited after the launch request is refused. It writes
startup evidence only after the existing upgrade gate succeeds and the usable
application shell opens. The local `launch_id` correlates this attempt; it is
not a credential or cryptographic attestation. No keyring secret is stored in the
record/evidence. Close the application and all companion writers before the next
step so databases have no pending WAL/journal data.

Review station identity, radios/bindings, companion paths, schedules/groups,
messages/attachments and credentials in CommsPlan. Only after those checks:

```sh
python start-commsplan-migration.py verify-station --session "SESSION_DIRECTORY" --confirm station-identity --confirm radios-and-bindings --confirm companion-paths --confirm schedules-and-groups --confirm messages-and-attachments --confirm credentials
```

Verification also checks startup identity, current migration marker, database
integrity, retained original table rows/values in their original columns, and
persistent files outside `config/`. It allows added columns/rows. Runtime `kv`
bookkeeping may change; existing callsign/grid values are checked, and the other
operator settings require the explicit checklist. The table checks are
conservative: legitimate historical transformations may still require a reviewed
comparison policy. A failed comparison is a stop, never an automatic waiver.
Credentials require operator verification in the same OS account; the tool does
not export secrets or prove another account can access them.

Manually retire only the reviewed old launch entries after station verification.
Keep the historical source/environment, session and backup. Then:

```sh
python start-commsplan-migration.py finish --session "SESSION_DIRECTORY" --old-launchers-retired
python start-commsplan-migration.py resume --session "SESSION_DIRECTORY"
```

`finish` revalidates the evidence/data/backup and requires the retirement
acknowledgement. It deletes nothing. `finished` means this local source workflow
and operator checklist completed; it is **not** native-platform release
operator checks or proof of every external software/hardware setting.

After interruption, `resume` rechecks physical paths, source/receipt,
backup identity and the relevant profile state. It never runs installation,
conversion or cleanup. Retry the last incomplete command. A verified backup
completed just before a checkpoint interruption is revalidated and adopted;
multiple source trees stop for review. Partial backups remain marked incomplete.
A completed CommsPlan launch can be requested again with a fresh `launch_id`
after its existing evidence and protected contents validate. If startup changed
the station but failed before producing evidence, the workflow stops for recovery
rather than blindly repeating conversion. Keep the failed profile and restore a
verified copy into a new directory with the matching historical version.

## Application upgrade

For 1.2.8 stations, the existing informed single-rig upgrade remains necessary.
For stations already upgraded in 2.0.4, retain their multi-rig configuration.
The helper reads the profile migration marker without creating schema and
reports whether the informed upgrade is still required. A 2.0.4 executable alone
does not prove that a particular profile has completed the upgrade.

Use the same explicit profile for the CommsPlan launch. Verify callsign, radios,
software paths, bindings, schedules, groups, messages and attachments before
accepting the station. Do not run the historical and CommsPlan apps together.
A database converted by the newer application must not be opened in 1.2.8.

Windows retains the historical installer identity and install directory. Linux
retains the `freqinout` package and desktop-entry identity, with both `freqinout`
and `commsplan` commands. macOS retains the bundle identity but uses
`CommsPlan.app`; replacement of an existing `FreqInOut.app` still needs the
platform procedure and operator checks. These compatibility measures are
implemented in source and do not prove that every historical native upgrade
succeeds.

## Native replacement preparation

### Native 2.0.4 backup handoff

The CommsPlan source tree provides explicit native preparation for the four
distributed 2.0.4 packages. It requires the original rollback package matching its pinned
public release size and SHA-256, the actual installation directory (or macOS
`.app`), and the confirmed existing profile root. Keep the rollback package
outside both installation and profile directories. The tool never downloads,
installs, executes the old app, converts a station or removes software.

Release selections are `windows-2.0.4`, `linux-amd64-2.0.4`,
`macos-arm64-2.0.4` and `macos-x86_64-2.0.4`. Native 1.2.8 preparation is refused:
the release has a Windows installer, but the tagged installer script reports
`1.2.5.6`. Its actual installed identity requires a separately verified
installation before this tool can use it. This does not change the source-only
1.2.8 procedure above.

Close the app and all companion writers. Use the CommsPlan executable's full
path, with a NEW report file outside the app/profile/backup and an existing
report parent directory. On Windows, wait for the process to exit before reading
the report; a windowed executable has no terminal output. Replace placeholders:

```text
COMMSPLAN_EXECUTABLE --migration-preparation --report-file "NEW_INSPECTION_REPORT.json" inspect-native --installation "OLD_NATIVE_INSTALLATION" --profile "PROFILE_ROOT" --package "RETAINED_204_PACKAGE" --release "RELEASE_SELECTION"
COMMSPLAN_EXECUTABLE --migration-preparation --report-file "NEW_BACKUP_REPORT.json" backup-native --installation "OLD_NATIVE_INSTALLATION" --profile "PROFILE_ROOT" --package "RETAINED_204_PACKAGE" --release "RELEASE_SELECTION" --destination "EXISTING_BACKUP_DIRECTORY"
COMMSPLAN_EXECUTABLE --migration-preparation --report-file "NEW_VERIFICATION_REPORT.json" verify-native-backup "BACKUP_DIRECTORY_REPORTED_BY_TOOL"
```

Each report contains `exit_code` and `result`. Require process success,
`exit_code: 0`, and both `profile_verified: true` and
`rollback_package_verified: true` in the final verification result. An absent,
empty, malformed or failed report stops the handoff. Never reuse/overwrite a
report. Report creation must succeed before preparation starts. This route
dispatches before normal GUI/database/logger initialization. Source users can
instead run `python start-commsplan-migration.py --report-file "NEW_REPORT.json"`
followed by one of the same native commands; without a report file, put the
native command immediately after the script name for terminal JSON output.

Inspection checks macOS bundle identity/version and Mach-O architecture;
Windows 64-bit machine-wide historical uninstall registration and x86-64 PE
architecture; or Linux Debian package version/ownership and amd64 ELF identity.
Portable/per-user Windows installations, relocated Debian files and unknown
metadata stop for review. It fingerprints the selected installed tree without
executing it. Relative internal bundle links are allowed; escaping, broken or
absolute installation links are refused. Profile links remain refused. SQLite
Nonempty WAL or rollback journal data stops cold preparation until all writers close
and historical recovery resolves the state.

The cold backup reuses the profile lock, database checks, persistent-file hashes
and new-directory recovery routine. It rechecks the native installation and
package after copying. The retained installer/DMG/DEB remains **by reference** at
its recorded path; it is not copied into the station backup. Moving, changing or
losing it invalidates `verify-native-backup`. Keep it alongside your recovery
materials. Generic `verify-backup`/`restore-copy` verify profile files only and
cannot prove the old application/package is available.

A package digest does not prove the installed payload matches that package.
Reports explicitly retain `installed_payload_matches_package: false` and
`native_upgrade_qualified: false`. Signature/keychain continuity, external
companion-path inventory, native session/resume/station verification and actual
historical replacement/rollback still require platform procedures and operator
checks. Do not treat this
preparation report as permission to migrate a live station or as a finished
migration.

The Windows installer explicitly reuses the historical Inno application ID and
previous installation directory. It checks for both `FreqInOut.exe` and
`CommsPlan.exe` before installation or removal, refuses when either is running,
and refuses when Windows process inspection fails. It does not automatically
close or restart station applications. This conservative name check also blocks
an app running from another directory. Source/Python launches and launches that
race with replacement still require native qualification and operator closure.

`FreqInOut.exe` in the installation directory becomes a byte-identical copy of
the new `CommsPlan.exe`, sharing its installed runtime. Existing shortcuts and
pins therefore have a compatibility path; that file is **not** a recoverable old
application. Keep the historical installer and a separately verified station
backup. The native installer does not implement the source-only migration
session or prove station continuity. Do not use it on a live profile until the
historical upgrade/rollback procedure is qualified.

Old shortcuts are retained during verification. After verifying the station,
inspect each old shortcut's Properties and confirm its target is the selected
installation's `FreqInOut.exe` before retiring that entry and pinning CommsPlan.
Do not delete arbitrary old-name files or shortcuts. No broad cleanup is part
of this installer; final one-launcher behavior remains an operator/native gate.

The macOS disk image includes **READ ME - Upgrading from FreqInOut.txt**, also
available as `CommsPlan-macos-upgrade.txt` beside this guide. It describes
identity checks, cold station backup, recoverable app copies, selected-location
replacement, explicit-path launch, station/keychain verification and rollback
into a new profile. The DMG does not automate or enforce these operations.
Bundle ID, executable/name and both version fields are checked before assembly.
Dock/LaunchServices behavior and both architectures remain unqualified.

## Recovery into a separate profile

```sh
python start-commsplan-migration.py restore-copy "VERIFIED_BACKUP_DIRECTORY" --destination "NEW_RECOVERY_DIRECTORY"
```

The destination must not exist. The helper verifies the backup again and copies
it into a new directory, without replacing an existing station. Verify the copy
with the matching historical application version, explicitly selecting it with
`FREQINOUT_CONFIG_DIR`. Preserve an interrupted recovery directory for review
and retry into a different new directory. Do not remove the original profile,
backup, or historical installer as part of this recovery procedure.

For a frozen-package backup, use the actual directory recorded in
`<PROFILE_ROOT>/.commsplan-packaged-startup.json` and confirm its
`manifest.json` before recovery. The startup guard's raw backup includes the
database sidecars present at the cold-copy point; recovery must still verify the
restored databases before opening them. Do not substitute the package startup
receipt for the broader migration helper backup: they have different coverage
and verification records.

## Linux source support: one maintenance command

Run `bash install-commsplan.sh setup` from the selected checkout, including an
existing folder named `~/Radio/FreqInOut`. There is no need to rename the source
folder. `setup` runs the verified installer and refreshes desktop integration;
`update` follows the current branch/upstream before doing that work; `repair`
rechecks the environment without a Git update; `desktop` refreshes local icon
and launchers without Git or dependency installation. Use `--dry-run` to display
the plan without writing locks/logs or changing files. See the
[Linux installer guide](FreqInOut-linux-installer.md).

If you choose to rename a verified Linux checkout from `~/Radio/FreqInOut` to
`~/Radio/CommsPlan`, first close CommsPlan and all companion applications. Keep
the selected profile (`~/.commsplan`) and its backups untouched. Pull the
relocation-capable CommsPlan release build while the checkout still has its old name,
then run, from outside the checkout:

```sh
cd "$HOME/Radio"
mv FreqInOut CommsPlan
bash "$HOME/Radio/CommsPlan/install-commsplan.sh" setup \
  --config-root "$HOME/.commsplan" \
  --relocate-from "$HOME/Radio/FreqInOut"
```

The old directory must no longer exist, the new directory must be the same Git
checkout under the same parent, and an owned launcher and matching installation
receipt must identify the old location and selected profile. Setup rebuilds the
path-bound `.venv`, verifies a profile backup and the application, then rewrites
the `commsplan` and compatibility `freqinout` launchers and desktop entry to the
new directory. Do not launch between the `mv` and successful setup. If setup
fails, let it finish its rollback, then move `CommsPlan` back to `FreqInOut`
before using the old launcher; preserve the profile and installer backup. A
source-directory rename does not rename either profile or SQLite database files.

The installer and POSIX launcher retain an explicitly selected profile in the
installation receipt. `FREQINOUT_CONFIG_DIR`/`--config-root` overrides that
remembered selection. Supply an absolute physical profile ROOT, containing
`config/`, when testing historical installations. Older single-rig launchers may
have selected a sibling runtime; do not assume the application folder's `config`
is the live profile. Desktop integration refuses to overwrite recognized command
paths belonging to another installation. For side-by-side source-only tests,
use `install_freqinout.py` with an explicit test profile and `start-commsplan.sh`
without replacing account-wide desktop entries.

| File/command | Support role |
| --- | --- |
| `install-commsplan.sh` | Supported Linux setup/update/repair/desktop/uninstall entry |
| `install_FreqInOut_linux.sh` | Compatibility implementation and fresh-install bootstrap |
| `install_freqinout.py` | Shared cross-platform verified source installer; automatic config-folder backup |
| `start-commsplan.sh` / `.cmd` | Branded source launchers |
| `start-freqinout.*`, `start-multi-rig.*` | Compatibility routes to the same source application |
| `commsplan`, `freqinout` | Account-wide verified launcher and compatibility alias |
| `start-commsplan-migration.py` | Explicit cold profile backup, guided source preparation and new-copy recovery |
| `view_logs.py`, `tools/freqinout-db.*` | Support diagnostics; legacy filenames remain compatible |
| `tools/` lab, benchmark and release scripts | Developer/support utilities, excluded from the public runtime projection |

Application/database/launcher compatibility names do not indicate an old runtime.
Support should record the source revision, installation directory, receipt,
selected profile root and version separately. An installer config-folder backup
is not the migration helper's broader persistent-profile backup and does not
include companion data outside that folder or credentials.

### Guided Linux profile transition to `~/.commsplan`

The CommsPlan source installation includes an explicit profile-copy tool. Complete
and verify the existing station upgrade first; this tool requires a multi-rig
migration marker of at least 1. It does not combine the single-rig conversion
with a directory move. Native packages and other platforms are not qualified by
this procedure. Database filenames remain `freqinout.db` and `freqinout_nets.db`.

Use the **actual profile root containing `config/`**. The supplied Linux startup
log confirmed `/home/bill/.freqinout`; the commands below use that selection.
Other users must substitute their confirmed root. Close FIO/CommsPlan and every
companion writer before planning or copying. The shared app lock cannot stop companion
software, and old/new profiles have separate locks: never run both stations at
once. Keep the original profile, original working application and recovery files.

From the updated application checkout, prepare a migration plan:

```bash
cd "$HOME/Radio/FreqInOut"
umask 077
PROFILE_SOURCE="$HOME/.freqinout"
mkdir -p "$HOME/CommsPlan-migration-backups"
bash install-commsplan.sh profile plan \
  --source "$PROFILE_SOURCE" \
  --destination "$HOME/.commsplan"
```

The plan only inspects the selected profile; it does not copy or switch it.
The terminal prints a compact JSON summary with counts, up to eight blocker
samples, `ready_to_copy`, `digest`, and `report_file`. The full evidence is saved
in that report under a new private `CommsPlan-profile-report-*` directory outside
both profiles. Review `path_changes`, `external_paths`, `excluded_roots`,
`preserved_history`, `blockers`, and `digest` in the full report. Use `--json`
only when full JSON on stdout is needed for automation or explicit redirection.
Path inventories can contain private filesystem names;
do not post the full file publicly. `ready_to_copy` must be true and `blockers`
must be empty. External existing/missing and relative/unresolved paths stay
unchanged and need operator review. In particular, the backup excludes top-level
`downloads`, `logs`, `cache`, `backups`, and the shared lock; references into
excluded content stop the copy. External companion data and OS keyring secrets
are not backed up. Preserve their separate recovery materials.

Recognized whole canonical absolute paths in database fields and configuration
JSON are retargeted only when their targets are included in the copy. Known
software-instance launch recipe path lists, profile selectors, established
path resource claims and individual whole absolute argument tokens are supported.
Generated command display text normally must exactly match `shlex.join` of the
original argument vector. A narrow native Wine VARA/VarAC exception rebuilds
display-only metadata when executable, working directory and effective vector
agree with copied canonical files. VARA requires one executable argument; VarAC
requires its executable plus its canonical copied INI argument. VARA repairs can leave
stale display text; native VarAC allows only missing/empty or its exact legacy
formatter. Custom commands and inconsistent vectors remain blocked.
Nested message-folder settings are supported.

Finalized native repair journals (`complete`/`rolled_back`) and confirmed launch
bundle migration audit rows preserve their original paths, hash keys and row
contents as historical evidence. Row identities/hashes are recorded in the plan
and checked after copying. Unfinished or unknown journal states block even if
no old-root references appear. Retained `config/backups/varac-native-repair/`
artifacts also preserve exact bytes; they describe the original station and are
not retargeted or automatically replayed. Other embedded commands/text, JSON path keys,
encoded scanner identities, SQL schema references, triggers on affected tables,
linked data, missing targets, and unsupported row identities require review.
Folders missing from the original are not automatically invented from manifest
references; their companion instance needs separate review/repair.
The tool never performs global text replacement or silently clears history.
Other opaque profile files are copied unchanged; inspect any companion-specific
configuration they contain during station verification.

If preparation reports pending SQLite journal data after shutdown, inspect the
actual files without opening or changing the databases:

```bash
bash install-commsplan.sh profile journals --source "$PROFILE_SOURCE"
```

The summary lists canonical database sidecars with sizes and file types. Its
full `report_file` also includes every sidecar in included profile folders and
their hashes. SHM alone is a transient index and is
accepted only when matching WAL and rollback journals are absent/empty. Validated
SHM and empty sidecars are omitted from new backups/copies, while original files
remain untouched. Their source snapshots are checked for changes before accepting
preparation. Nonempty WAL/journal, linked/irregular files and orphan sidecars
still require review. Do not delete sidecars, force past the check, or run a manual
checkpoint on the original profile. A nonempty WAL can contain needed committed
transactions even when no process is running.

For nonempty WAL after all processes have exited, consolidate **copies only**:

```bash
mkdir -p "$HOME/CommsPlan-migration-backups"
bash install-commsplan.sh profile recover \
  --source "$PROFILE_SOURCE" \
  --backup-directory "$HOME/CommsPlan-migration-backups" \
  --companions-closed
```

The Linux application currently defaults to an intentional hard exit after Qt
teardown, which can leave committed data in WAL instead of checkpointing it into
the main DB. WAL presence is not proof that settings were unsaved. The original
preflight assumed a checkpointed main DB; this recovery route handles that
shutdown state without changing the original profile or journals. Changing Linux
shutdown lifecycle/checkpoint policy is separate work requiring native testing.

Recovery inventories and preserves included directories, including empty trees,
as well as files. Directory inventories bind raw capture, recovered provenance,
planning, backup and restore; directory-only changes also stop preparation. Older
file-only backups remain restorable, but a journal recovery created before this
fix cannot support migration: repeat `profile recover` from the original to
obtain a new workspace. Keep the old workspace for evidence; do not add missing
folders manually to its staging profile.

Recovery first hashes and copies all included original bytes, including pending
WAL/SHM/journals, while holding the app lock. It verifies that raw capture and
rechecks the source. The raw capture is a distinct recovery artifact, not an
ordinary cold-profile backup. It is never opened by SQLite. SQLite works on a
second disposable input and backs up its logical contents into a new guarded
profile, explicitly switching the copied output to DELETE journal mode. Output integrity/table counts and
source hashes are checked before success. Both canonical databases and additional
recognized SQLite files are processed independently; this is not a cross-database
atomic transaction. Original bytes and active launcher selection remain unchanged.

The output supplies `stage: recovered-not-activated` and a `profile` path such as
`.../CommsPlan-journal-recovery-.../recovered-profile`. Keep the entire workspace:
raw evidence, manifest, working input and normalized staging profile. Recovery
needs space for roughly three profile copies; the later backup and final move
need more, roughly five profile copies across the full workflow plus margin.
Interrupted work stays guarded and retained for review. Do not launch, edit or
relocate the staging profile, because its provenance hashes bind it to the raw
capture.

Use the exact returned `profile` path for the next review step:

```bash
PROFILE_SOURCE="PASTE_PROFILE_PATH_FROM_RECOVERY_OUTPUT"
bash install-commsplan.sh profile plan \
  --source "$PROFILE_SOURCE" \
  --destination "$HOME/.commsplan"
```

Review that plan and use **the same recovered source** for the subsequent
`profile copy` command below. The verified recovery record supplies the original
`.freqinout` path as `path_origin`, so profile-owned paths are remapped from the
original root rather than being left pointing back at it. No manual DB edits or
`--path-origin` override is needed. The original `.freqinout` still remains the
active/recovery station until you explicitly verify and activate `.commsplan`.

If the plan blocks, stop and use its locations and `blocked_instances`
context (instance identity, family, verification/recipe state and missing
targets) to investigate while continuing
to use the original station. Do not edit the original databases or delete scanner
history merely to bypass a blocker. If the original profile changes, including a
new launch, generate and review a fresh plan. There is no force option.

An unassigned Fast Light draft manifest may refer to a managed directory that
was never created, even though the radio now uses a different, working
identity and external FLRig/FLDigi profiles. The plan may list that exact
row under `retired_manifests` and become ready only when no radio assignment,
canonical identity, or launch item still refers to it, and every blocked path
is inside the same missing draft. The old application configuration may itself
be absent; in that case the plan also refuses any dangling radio assignment.
The prepared copy saves the complete manifest row in
`.commsplan-retired-manifests.json` before removing it from its active database.
If an unused Fast Light configuration row exists, it remains in the copied
database for preservation. The
original and verified backup retain all rows. Other missing paths remain
blockers; the tool does not create placeholder companion profiles.

Copy using the exact `digest` from the reviewed plan:

```bash
bash install-commsplan.sh profile copy \
  --source "$PROFILE_SOURCE" \
  --destination "$HOME/.commsplan" \
  --backup-directory "$HOME/CommsPlan-migration-backups" \
  --reviewed-digest "PASTE_REVIEWED_DIGEST_HERE" \
  --companions-closed
```

The command creates a hash-verified cold persistent-profile backup, restores into
an entirely new directory, retargets reviewed paths only in that copy, and checks
database integrity, foreign-key relationships, table counts and original-file hashes. It refuses an existing
destination, including an empty one. Copy failure preserves the backup and a
visible guarded partial copy. Do not remove its guard or try to launch it; keep
it for diagnosis and retry into a different new directory after review.

Success returns `stage: prepared-not-activated` and writes
`.commsplan-profile-copy.json` inside the new profile with backup location,
manifest hash, original/post-copy hashes and the path-change inventory/hash.
The installation receipt and desktop launcher still point to the original.
Verify the new copy using a one-time override:

```bash
FREQINOUT_CONFIG_DIR="$HOME/.commsplan" ./start-commsplan.sh
```

Check station identity, radios/bindings, schedules/groups, messages/attachments,
software/companion paths and credentials. Close that run and companion apps.
Only after those checks pass, remember the new root and refresh desktop routing:

```bash
bash install-commsplan.sh setup --config-root "$HOME/.commsplan"
```

This setup runs the established verified installer and makes a config-folder
backup of the new profile. That backup has narrower coverage than the earlier
persistent-profile migration backup. Explicit environment overrides still win
at launch: remove any old `FREQINOUT_CONFIG_DIR`/`FREQINOUT_RUNTIME_ROOT` exports
from custom launch scripts or shell startup files. Check the resulting log path
and launch once from the desktop to confirm it uses `.commsplan`.

To return to the unchanged original station, close both application/companion
sets and run the same setup command with the original `--config-root` instead.
Edits made after moving to the new profile are not merged back automatically;
keep both copies and investigate before choosing which station to continue.
Do not delete the old profile or migration backup during this procedure.

Actual Linux terminal/desktop/operator acceptance remains required. These source
procedures do not by themselves qualify historical native replacement, every
Linux distribution, or every final release artifact.
Database renaming remains a separate coordinated migration across initialization,
upgrades, backup/restore, diagnostics and old-version recovery.
