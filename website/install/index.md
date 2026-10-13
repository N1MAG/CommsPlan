---
title: Install or Upgrade
description: Install CommsPlan 2.0.5 or move an established FreqInOut station through the reviewed backup and consent flow.
lastUpdated: 2026-10-12T00:00:00.000Z
---

# Install or upgrade CommsPlan

<div class="doc-badge-row">
  <span class="doc-badge">CommsPlan 2.0.5</span>
  <span class="doc-badge">Windows · Linux · macOS</span>
  <span class="doc-badge">Verified backup on upgrade</span>
</div>

CommsPlan is the current product name for the FreqInOut station application.
The supported transition preserves established station data and technical
compatibility names while changing the user-facing application identity.

## Download inputs

Use the [CommsPlan release page](https://github.com/N1MAG/CommsPlan/releases/latest)
for release notes, source archives, SHA-256 checksums, and native packages.
The CommsPlan 2.0.5 packages are:

<div class="package-table-scroll" role="region" aria-label="CommsPlan package downloads" tabindex="0">

| Platform | Package | Source option |
|---|---|---|
| Windows 10/11 x86-64 | [`CommsPlan-2.0.5-windows-x86_64-setup-unsigned.exe`](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-windows-x86_64-setup-unsigned.exe) | Python 3.11 and `install_freqinout.py` |
| Debian/Ubuntu-family Linux amd64 | [`CommsPlan-2.0.5-linux-amd64.deb`](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-linux-amd64.deb) | Python installer or guided Linux installer |
| macOS Apple Silicon | [`CommsPlan-2.0.5-macos-arm64-unsigned.dmg`](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-macos-arm64-unsigned.dmg) | Python 3.11 and `install_freqinout.py` |
| macOS Intel | [`CommsPlan-2.0.5-macos-x86_64-unsigned.dmg`](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-macos-x86_64-unsigned.dmg) | Python 3.11 and `install_freqinout.py` |

</div>

::: warning Unsigned packages
Windows and macOS packages are unsigned. Windows may
show **Unknown publisher** or SmartScreen; macOS may require **Open Anyway**
under Privacy & Security. Verify every package against `SHA256SUMS.txt` on the
release page before installation.
:::

## Frozen package backup guard

Before a frozen CommsPlan package performs its first station schema or database
writes, it holds the station lock and completes the same guarded `config`-tree
backup used for the reviewed upgrade path. The guard checks the selected
physical profile root, both operational databases (`freqinout.db` and
`freqinout_nets.db`) when they exist, and the absence of linked configuration
or database files. It copies the raw `config` tree, including any pending
SQLite WAL or journal sidecars, validates SQLite on a disposable copy, and
requires source and copy hashes to match before the cold backup is considered
verified.

The guard writes its receipt only after those checks succeed. The receipt at
`<PROFILE_ROOT>/.commsplan-packaged-startup.json` (for example,
`~/.commsplan/.commsplan-packaged-startup.json`) records the actual verified
backup directory under `<PROFILE_ROOT>/backups/` and its manifest identity.
Keep the printed backup path and receipt together. On first launch, a missing
receipt is expected: the guard captures and verifies the `config` tree for an
existing profile, then writes the receipt. A fully fresh profile with no
station databases is allowed and is recorded as `fresh-profile`. A relaunch of
the same package version reuses an intact receipt after checking the backup path
and manifest identity; it does not recapture or fully rehash the profile on
every launch. Restore-time verification is still required. A malformed,
changed, or linked receipt, or a missing or changed recorded manifest or backup,
stops startup. A missing station database stops startup only for an existing or
previously captured profile; it is allowed for a fully fresh profile.

When a fresh profile's databases later appear, the guard captures the first
verified `config` backup once and records it. This receipt and raw `config`
backup support recovery preparation; they are not a complete profile,
companion, credential, or computer backup and do not qualify historical native
replacement or rollback.

## Native package installation

### Windows 10/11

Download the [Windows installer](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-windows-x86_64-setup-unsigned.exe),
verify its SHA-256 entry, and run it. Because this installer is unsigned,
Windows may show **More info → Run anyway** or **Unknown publisher**. Complete
the installer, then launch **CommsPlan** from the installed shortcut. Keep the
historical FreqInOut installer and your verified station backup available when
upgrading an existing station.

### macOS

Download the [Apple Silicon disk image](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-macos-arm64-unsigned.dmg)
or [Intel disk image](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.5/CommsPlan-2.0.5-macos-x86_64-unsigned.dmg)
that matches the Mac, verify its SHA-256 entry, open the disk image, and copy
**CommsPlan.app** to **Applications**. The unsigned image may require
**Open Anyway** under **Privacy & Security** on first launch. For an existing
station, first quit both apps and companion writers, verify a restorable backup
of the complete station profile, and retain the old app outside Applications.
Move the old active app out of Applications before copying CommsPlan.app into
place. Launch the new app by its full path and verify radios, companion paths,
schedules, messages, and credentials. Keep the old app and backup for recovery.
See the [detailed manual replacement and rollback procedure](https://github.com/N1MAG/CommsPlan/blob/main/docs/CommsPlan-macos-upgrade.txt).

## New installation

Install Git and 64-bit Python 3.11 when using a source checkout. Python 3.10
through 3.13 is accepted; Python 3.11 is the tested and recommended release
interpreter.

### Windows source installation

```powershell
git clone https://github.com/N1MAG/CommsPlan.git "$HOME\CommsPlan"
Set-Location "$HOME\CommsPlan"
py -3.11 install_freqinout.py
.\start-commsplan.cmd
```

### macOS source installation

```bash
git clone https://github.com/N1MAG/CommsPlan.git "$HOME/CommsPlan"
cd "$HOME/CommsPlan"
python3.11 install_freqinout.py
./start-commsplan.sh
```

### Linux package installation

```bash
sudo apt install ./CommsPlan-2.0.5-linux-amd64.deb
commsplan
```

### Linux source installation

The guided installer installs supported system packages, creates or repairs the
virtual environment, and prepares the desktop launcher:

```bash
git clone https://github.com/N1MAG/CommsPlan.git "$HOME/CommsPlan"
cd "$HOME/CommsPlan"
bash install-commsplan.sh setup
```

The historical `install_FreqInOut_linux.sh` entry point remains available for
compatibility. The CommsPlan `install-commsplan.sh` wrapper delegates its setup
path to that retained implementation and does not create a second profile
root.

## Migration boundaries

The supported native Windows baseline is **FreqInOut 2.0.4 → CommsPlan 2.0.5**.
If an existing Windows station is still on native FreqInOut 1.2.8, first make a
verified backup and use the published [FreqInOut 2.0.4 release](https://github.com/N1MAG/FreqInOut/releases/tag/v2.0.4)
and its normal upgrade procedure. Open 2.0.4 and verify radios, paths,
schedules, messages, and station configuration before preparing CommsPlan.
Direct native 1.2.8 → CommsPlan migration is not qualified by this guide.

Linux **1.2.8 source installations** have a separate, explicitly selected
source-migration path. The migration helper can inspect a source installation
whose version metadata says 1.2.8 or 2.0.4, with the existing profile root and
verified backup. Native Linux 1.2.8 installers are outside that source-only
procedure. The full [CommsPlan migration guide](https://github.com/N1MAG/CommsPlan/blob/main/docs/CommsPlan-migration.md)
is the canonical procedure.

### Optional Linux profile copy to `~/.commsplan`

An operator who wants a new Linux profile root can use the reviewed profile-copy
flow after the existing station upgrade is complete. This is an explicit copy
and verification process; it is not a simple `mv` or rename of `~/.freqinout`,
and the `freqinout.db` and `freqinout_nets.db` filenames remain unchanged.
Close CommsPlan and every companion writer, keep the original profile and
backup, then preview the copy from the updated checkout:

```bash
PROFILE_SOURCE="$HOME/.freqinout"
bash install-commsplan.sh profile plan \
  --source "$PROFILE_SOURCE" \
  --destination "$HOME/.commsplan"
```

Continue only when the preview reports `ready_to_copy: true` with no blockers.
Use the exact reviewed digest and a new destination for the guarded copy:

```bash
bash install-commsplan.sh profile copy \
  --source "$PROFILE_SOURCE" \
  --destination "$HOME/.commsplan" \
  --backup-directory "$HOME/CommsPlan-migration-backups" \
  --reviewed-digest "PASTE_REVIEWED_DIGEST_HERE" \
  --companions-closed
FREQINOUT_CONFIG_DIR="$HOME/.commsplan" ./start-commsplan.sh
```

Verify station identity, radios, schedules, messages, companion paths, and
credentials in the one-time run. Only then remember the new profile with
`bash install-commsplan.sh setup --config-root "$HOME/.commsplan"`. The original
profile remains the recovery station until that review succeeds; do not launch
both profile roots at once.

## Upgrade an established station

Use this sequence when moving from the published FreqInOut 2.0.4 station or
updating an established CommsPlan installation:

1. Close CommsPlan, FreqInOut compatibility launchers, and affected companion
   applications including FLRig, FLDigi, JS8Call, FIO Spotter, VarAC, and
   CommStat.
2. Create and verify a restorable backup of the station profile before
   replacing the application or changing the source checkout.
3. Install the CommsPlan package, or update the established source checkout
   and rerun `install_freqinout.py`. A Git pull alone is not a complete update.
4. Start CommsPlan and confirm that it found the intended existing profile.
5. If **Upgrade Existing Station** appears, review the proposed default radio,
   software paths, endpoints, schedule assignment, and Launch Control choices.
6. Choose **Back Up and Upgrade Station** only after the review is correct.
7. Confirm the radio, Message Inbox, FIO Spotter, Plan Builder, Map, and
   companion software identities after the first start.

The migration is explicit and idempotent. Exit or closing the upgrade window
before confirmation makes no station-migration writes and requires the review
again on the next launch. A one-radio station becomes the default radio in the
multi-radio application; adding another radio remains optional.

## Technical compatibility names

CommsPlan preserves the technical names required for station continuity:

- Existing profile roots remain `%LOCALAPPDATA%\\FreqInOut` or
  `%APPDATA%\\FreqInOut` on Windows and `~/.freqinout` on macOS/Linux unless
  `FREQINOUT_CONFIG_DIR` selects another profile.
- Existing `freqinout.db`, `freqinout_nets.db`, keyring references, and
  `freqinout` launcher/package identities remain compatibility surfaces.
- The canonical source entry points are now `start-commsplan.sh` and
  `start-commsplan.cmd`; `start-freqinout.sh` and `.cmd` remain working
  compatibility launchers.
- An explicit profile override always wins. CommsPlan does not guess between
  multiple historical profiles or silently create a parallel `.commsplan` root.

## Verification and recovery

For source installation, require the final `Installation verified` line before
launching. If the installer reports `Installation failed. Do not launch
CommsPlan.`, preserve the named database and installer output; do not delete or
replace it while investigating.

For rollback:

1. Close CommsPlan and all affected companion applications.
2. Retain the failed or newly installed application folder for diagnosis.
3. Restore the `config` tree from the reported verified backup, including both
   FIO SQLite databases and any sidecars captured by the guard.
4. Launch the retained earlier application folder and confirm the station
   identity, radio assignment, messages, and schedules.

The reported backup is narrower than a complete profile or computer backup: it
does not include companion data, credentials, logs, caches, or downloads.

Do not copy a database while CommsPlan is running. If a backup contains SQLite
`-wal` or `-shm` files, restore the backup as one unit rather than selecting
individual database files.

## Next step

[Configure or review the first radio →](/guide/first-radio)
