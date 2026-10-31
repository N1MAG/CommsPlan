---
title: Install or Upgrade
description: Install CommsPlan from the latest published release or update an established source installation safely.
---

# Install or upgrade CommsPlan

<div class="doc-badge-row">
  <span class="doc-badge">CommsPlan 2.0.9</span>
  <span class="doc-badge">Windows · Linux · macOS</span>
  <span class="doc-badge">Back up before upgrade</span>
</div>

CommsPlan 2.0.9 is available for Windows x86-64, Linux amd64, Apple Silicon and
Intel macOS. The [2.0.9 release page](https://github.com/N1MAG/CommsPlan/releases/tag/v2.0.9)
contains the packages, checksums and release notes.

| Your computer | CommsPlan 2.0.9 download |
| --- | --- |
| Windows 10/11 x86-64 | [CommsPlan-2.0.9-windows-x86_64-setup-unsigned.exe](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.9/CommsPlan-2.0.9-windows-x86_64-setup-unsigned.exe) |
| Debian/Ubuntu-family Linux amd64 | [CommsPlan-2.0.9-linux-amd64.deb](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.9/CommsPlan-2.0.9-linux-amd64.deb) |
| Apple Silicon macOS | [CommsPlan-2.0.9-macos-arm64-unsigned.dmg](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.9/CommsPlan-2.0.9-macos-arm64-unsigned.dmg) |
| Intel macOS | [CommsPlan-2.0.9-macos-x86_64-unsigned.dmg](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.9/CommsPlan-2.0.9-macos-x86_64-unsigned.dmg) |

Verify your download against [SHA256SUMS.txt](https://github.com/N1MAG/CommsPlan/releases/download/v2.0.9/SHA256SUMS.txt).
The Windows installer and macOS images are unsigned; macOS images are not
Developer ID signed or notarized. Review the release signing notice before opening.


## Choose an installation method

Use a native package when the release page provides one for your system. Check
the release notes and verify the downloaded file against the published checksum
before opening it. Windows or macOS may show an operating-system security
prompt for an unsigned package; follow the release-specific instructions.

The source commands below use the published CommsPlan 2.0.9 installer entry points. Use
the 2.0.9 source tree for these instructions. When using a source checkout,
install Git and Python 3.11 (3.10–3.13 are accepted; 3.11 is recommended).
Close CommsPlan and companion applications before installing or updating.

### Windows source installation — CommsPlan 2.0.9

```powershell
git clone https://github.com/N1MAG/CommsPlan.git "$HOME\CommsPlan"
Set-Location "$HOME\CommsPlan"
git checkout v2.0.9
py -3.11 install-commsplan.py
.\start-commsplan.cmd
```

### macOS source installation — CommsPlan 2.0.9

```bash
git clone https://github.com/N1MAG/CommsPlan.git "$HOME/CommsPlan"
cd "$HOME/CommsPlan"
git checkout v2.0.9
python3.11 install-commsplan.py
./start-commsplan.sh
```

### Linux package installation

Install the Debian package offered by the release page, then launch CommsPlan
from your applications menu or with the `commsplan` command when available.

### Linux source installation and maintenance — CommsPlan 2.0.9

The guided installer supports setup, update, repair, and desktop launcher
refresh. With no action supplied, it runs setup.

```bash
git clone https://github.com/N1MAG/CommsPlan.git "$HOME/CommsPlan"
cd "$HOME/CommsPlan"
git checkout v2.0.9
bash install-commsplan.sh setup
```

For an established source checkout, use `bash install-commsplan.sh update` to
update the current checkout and refresh its environment and launcher. Use
`bash install-commsplan.sh desktop` to refresh the launcher without updating
code or dependencies. See `bash install-commsplan.sh --help` for options.

## Upgrade an established station

Before replacing the application or changing a source checkout:

1. Close CommsPlan and affected companion applications.
2. Create and verify a restorable backup of the station profile.
3. Install the package from the latest published release, or update the source
   checkout using the platform instructions above. A Git pull alone does not
   complete a source installation update.
4. Start CommsPlan and confirm it opened the intended station.
5. Review any offered station setup or migration summary before accepting it.
6. Confirm the radio configuration, schedule, message library, map, and
   companion application settings after the first start.

A one-radio station remains supported; adding another radio is optional.
Keep the earlier application and verified backup available until the review is
complete. Do not copy a database while CommsPlan is running. Restore a database
backup as one unit with any journal sidecar files it contains.

## Review the assigned plan

Station Setup provides the saved checklist for the selected radio. Assign the
reviewed Frequency Plan after RF Guard checks. A valid assignment follows after
validation without a separate Enable scheduling action. Pause/Resume, Net control,
and enabled source choices remain radio-specific; no action bypasses readiness,
transmit/busy protection or RF Guard.

## Continue

[Review the station setup and first radio →](/guide/first-radio)
