# Installing and Updating CommsPlan — 2.0.5

> CommsPlan 2.0.5 installation documentation. Historical native replacement
> remains bounded by the platform procedures in
> [the CommsPlan migration guide](CommsPlan-migration.md); successful
> installation does not by itself prove station continuity or historical
> rollback.

The [CommsPlan GitHub Release page](https://github.com/N1MAG/CommsPlan/releases/latest)
is the source for release notes, source archives, SHA-256 checksums, and any
published native packages for Windows x86-64, Linux amd64, macOS Intel, and
macOS Apple Silicon. Source installation remains supported on Windows 10/11,
current macOS releases, and common Linux desktop distributions.

Windows and macOS packages are unsigned and include `-unsigned` in
their download filenames. Windows may display **Unknown publisher** or a
SmartScreen prompt. macOS may require **Open Anyway** under Privacy & Security.
Historical 2.0.4 packages install **FreqInOut**; the 2.0.5 release packages
install **CommsPlan**. Verify downloads against `SHA256SUMS.txt` on the release
page and preserve the historical installer for recovery.

The 2.0.5 package filenames are:

- `CommsPlan-2.0.5-windows-x86_64-setup-unsigned.exe`
- `CommsPlan-2.0.5-linux-amd64.deb`
- `CommsPlan-2.0.5-macos-arm64-unsigned.dmg`
- `CommsPlan-2.0.5-macos-x86_64-unsigned.dmg`

Python 3.10 through 3.13 is accepted for source installation; Python 3.11 is
the tested and recommended release interpreter. Companion radio applications
are installed separately.

## Source installation rule

New installations and upgrades use the same `install_freqinout.py` check. Do
not start CommsPlan until it prints exactly:

```text
Installation verified. Run: ...
```

The installer validates the release files and Python version, repairs an
incomplete `.venv` without deleting it, installs the requirements, and runs an
isolated application/database check. When it finds an existing station, it
also requires CommsPlan to be closed, checks both CommsPlan databases, creates a cold
profile backup, verifies every copied file and database, and records the backup
location. If any step fails, it prints `Installation failed. Do not launch
CommsPlan.` and the launcher remains blocked.

On Windows, security or indexing software may prevent the verified backup
directory from receiving its preferred timestamped name. CommsPlan retries that
rename. If Windows continues to deny only the rename, the installer retains the
already verified backup under the exact temporary name it reports and continues;
the installation receipt records that actual path. This warning is safe only
when it is followed by `Verified pre-install station backup:` and the final
`Installation verified` message. Database, copy, hash, and manifest failures
still stop the installation.

If database validation fails, do not delete, replace, or edit the named
database. The installer made no changes; preserve the database and contact
CommsPlan support before attempting the upgrade again.

## First frozen-package startup backup

Before a frozen CommsPlan package performs its first station schema or database
writes, it must hold the station lock and complete the same guarded `config`
tree backup. The guard checks the selected profile root, both operational
databases when they exist, and the absence of linked configuration or database
files. It copies the raw `config` tree, including any pending SQLite WAL or
journal sidecars, and validates SQLite on a disposable copy so the verified cold
backup remains byte-for-byte unchanged. The backup manifest is written only
after the source and copy hashes still match.

This package-startup backup covers the selected profile's `config` tree only.
It is not a whole-profile, credential-store, log, cache, or companion-data
backup; use the migration helper when broader persistent-profile coverage is
required.

The package records the actual verified backup directory in
`<PROFILE_ROOT>/.commsplan-packaged-startup.json`; the receipt itself is also
under that profile root (for example,
`~/.commsplan/.commsplan-packaged-startup.json` when that is the selected root).
Keep the path printed by startup and the receipt together. A relaunch of the
same package version reuses the receipt after
checking the backup path and manifest identity. It does not recapture or fully
rehash the backup on every launch; restore-time verification remains required.
If an existing receipt is malformed, changed, or linked elsewhere; its recorded
manifest or backup is missing or mismatched; a previously verified station's
databases disappear; or present station databases are unsafe, startup stops for
recovery. A missing receipt is normal on first launch and starts a verified
capture. A new profile is recorded as
`fresh-profile`; if its databases appear later, the guard captures the first
verified backup then and updates the receipt.

## Upgrade an existing station

1. Close CommsPlan, FreqInOut, FLRig, FLDigi, JS8Call, CommsPlan Spotter, VarAC, CommStat, and
   other companion radio applications.
2. Download the appropriate package from the CommsPlan GitHub Release, or update
   the existing public source checkout using the manual procedure below.
3. For a package, complete the operating-system installation and start
   **CommsPlan**. For a source checkout, rerun `install_freqinout.py`, require
   the final `Installation verified` line, and use the neutral
   `start-freqinout` launcher.
4. CommsPlan automatically uses the established profile unless
   `FREQINOUT_CONFIG_DIR` intentionally selects another one.
5. In **Upgrade Existing Station**, review or select the manufacturer, model,
   display name, plan name, and detected software. Select **Back Up and Upgrade
   Station**.
6. Review the radio's software, schedule assignment, and Launch Control
   settings. Launch Control remains off until the operator enables it.

The only other choice in the upgrade window is **Exit CommsPlan**. Closing the window
also exits CommsPlan. No migration writes occur, and the same upgrade is required on
the next launch.

Windows stations still on historical 1.2.8 must first complete the established
FreqInOut 2.0.4 upgrade, verify the station there, and only then proceed to
CommsPlan 2.0.5. Linux 1.2.8 source installations retain the documented source
installation and informed upgrade path; the native Linux package path does not
replace that source procedure. Keep the historical application, installer, and
verified profile backup until the station is accepted.

For rollback, close CommsPlan, retain the failed/new folder, restore the `config`
folder from the reported `pre-install-...` backup, and launch the retained
1.2.8 application folder. Do not copy a database while CommsPlan is running.

## Windows 10/11

Download and run
`CommsPlan-2.0.5-windows-x86_64-setup-unsigned.exe` from the CommsPlan GitHub
Release. Windows may require **More info** followed by **Run anyway** while the
installer remains unsigned. A historical 1.2.8 station must use the FreqInOut
2.0.4 intermediate release before this package.

For a manual source installation, install 64-bit Python 3.11 from Python.org,
open PowerShell in the public checkout, and run:

```powershell
py -3.11 install_freqinout.py
.\start-freqinout.cmd
```

The launcher accepts normal CommsPlan command-line options. An explicit
`FREQINOUT_CONFIG_DIR` is honored when a separate profile is intentionally
required.

Maintainers and technical testers updating an approved source checkout use:

```powershell
Set-Location "$HOME\CommsPlan"
git pull --ff-only origin main
py -3.11 install_freqinout.py
```

Use only installers attached to the official CommsPlan GitHub Release.

## macOS

Download the disk image matching the Mac processor from the CommsPlan GitHub
Release, open it, and follow the manual replacement procedure in
`CommsPlan-macos-upgrade.txt`. The image contains **CommsPlan.app**. While the
disk images remain unsigned, macOS may require **Open Anyway** under Privacy &
Security on the first launch; retain the historical FreqInOut application and
installer until station verification and rollback checks are complete.

For a manual source installation, install Python 3.11 from Python.org or
Homebrew. Do not use an obsolete system Python. Open Terminal in the public
checkout and run:

```bash
python3.11 install_freqinout.py
./start-freqinout.sh
```

For an update:

```bash
cd "$HOME/CommsPlan"
git pull --ff-only origin main
python3.11 install_freqinout.py
```

macOS may ask for file, Bluetooth, or automation access when configured
companion applications or hardware are first used; grant only the access
needed by those configured paths.

## Linux

On Debian/Ubuntu-family amd64 systems, download
`CommsPlan-2.0.5-linux-amd64.deb` from the CommsPlan GitHub Release and install
it with:

```bash
sudo apt install ./CommsPlan-2.0.5-linux-amd64.deb
commsplan
```

The manual source path remains supported on Linux, including 1.2.8 source
installations that still require the informed station-upgrade flow:

```bash
python3.11 install_freqinout.py
./start-freqinout.sh
```

The guided Linux installer remains available for users who also need system
packages, a desktop icon, and the account-wide `freqinout` command.

The guided installer recognizes `apt`, `dnf`, `yum`, `pacman`, and `zypper`,
installs supported system dependencies, creates a virtual environment, and
prepares the launcher and desktop entry.

```bash
git clone https://github.com/N1MAG/CommsPlan.git "$HOME/CommsPlan"
cd "$HOME/CommsPlan"
bash install_FreqInOut_linux.sh
```

Rerun the same installer to update. Preserve any explicit `--dir` and
`--config-root` values used for the original installation. See the
[Linux installer reference](FreqInOut-linux-installer.md) for repair, offline,
unattended, rollback, and uninstall options.

The native Map may require `libxcb-cursor0` and `libxcb-xinerama0` on
Debian/Ubuntu-family desktops. The guided installer offers applicable platform
packages when they are missing.

## First launch for a new station

1. Enter the station callsign, location, and time preferences under
   **Configuration**.
2. Define the operating groups used by schedules, filtering, access policies,
   and publication rules.
3. Add or review the first radio and select only the software it uses.
4. Review Software Administration and Launch Control for that radio.
5. Assign a plan and schedule, then verify Ops Center and Station Health.
6. Restart CommsPlan and confirm that the selected profile and radio settings persist.

CommsPlan vendors its supported JS8 networking integration. Do not install
`pyjs8call` as a replacement for that integration.

## Profile and log locations

Unless `FREQINOUT_CONFIG_DIR` selects another profile, the normal roots are:

| OS | Default profile root |
|---|---|
| Windows | `%LOCALAPPDATA%\FreqInOut` (or `%APPDATA%\FreqInOut`) |
| macOS | `~/.freqinout` |
| Linux | `~/.freqinout` |

The settings and operational databases are under the profile's `config`
folder. Support evidence in the profile root includes:

- `freqinout.log` — primary application log;
- `perf_metrics.log` — bounded performance observations;
- `fio_cpu_hotspot_*.txt` — CPU hotspot evidence when generated;
- UI hang evidence generated by the watchdog, when present.

Review attachments for callsigns, message content, access codes, local paths,
and other private information. Database files are not normally needed unless
support specifically requests them.

The product rename retains the historical compatibility identities. Existing
profile roots remain `%LOCALAPPDATA%\FreqInOut` or `%APPDATA%\FreqInOut` on
Windows and `~/.freqinout` on macOS and Linux unless
`FREQINOUT_CONFIG_DIR` selects another root. The operational databases remain
`freqinout.db` and `freqinout_nets.db`; the Windows installer retains
`AppId=FreqInOut`; the Linux Debian package ID remains `freqinout`; and the
macOS bundle identifier remains `org.n1mag.freqinout`. Do not rename these
identities during installation or migration.

## Troubleshooting

- **Python rejected:** use Python 3.10 through 3.13. Python 3.14 is not yet
  supported.
- **Module or import error:** rerun `install_freqinout.py`, or use the Linux
  installer's `--repair` option with the original paths.
- **Wrong configuration appears:** close CommsPlan and check
  `FREQINOUT_CONFIG_DIR` before changing any settings.
- **A companion app is unavailable:** verify that the selected radio owns the
  correct executable, endpoint, profile, and data paths.
- **Map is blank:** capture the logs; the Map is designed to use bundled
  geography and does not require an online tile API key.

The in-app CommsPlan Guide contains configuration, recovery, and feature-level
reference material.
