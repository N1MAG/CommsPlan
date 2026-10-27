---
title: What's New in CommsPlan
description: CommsPlan 2.0.8.1 folder-save hotfix, CommsPlan 2.0.8 workflows, CommsPlan 2.0.7 history, and the earlier 2.0.6 feature overview.
---

# What's new in CommsPlan

<div class="doc-badge-row">
  <span class="doc-badge">CommsPlan 2.0.8.1</span>
  <span class="doc-badge">Operator overview</span>
</div>

The [GitHub Releases page](https://github.com/N1MAG/CommsPlan/releases/latest)
lists currently available packages, checksums, and release notes. Package
availability can vary by platform, so use that page to confirm which native
installers have been published.

For the current update, jump to [CommsPlan 2.0.8.1](#commsplan-2-0-8-1). The 2.0.8, 2.0.7 and 2.0.6 feature history remains below.

## CommsPlan 2.0.8.1

This patch fixes Fast Light folder-save readback errors after enabling FLMsg or
FLAmp. It also keeps all four component settings folders and FLDigi log/check-in
folders consistent through changes, intentional clearing and reopening existing
configurations. Failed verification still rolls back the save.

Native Linux operator qualification of this hotfix remains unperformed. Existing
2.0.8 hardware, transport and macOS limitations remain unchanged. See the
[2.0.8.1 release notes](https://github.com/N1MAG/CommsPlan/releases/tag/v2.0.8.1)
for the published packages and checksums.

## CommsPlan 2.0.8

These instructions cover CommsPlan 2.0.8. Check [GitHub Releases](https://github.com/N1MAG/CommsPlan/releases/latest)
for available packages and their release notes.

### Assignment-driven schedules and Plan Builder

Assigning a valid Frequency Plan means the radio should follow it after
CommsPlan validates the saved radio, application identity, plan, enabled sources,
and readiness. There is no separate **Enable scheduling** action. Use Pause for
a deliberate hold and Resume to request the current eligible schedule. Quick
QSY remains in effect until that radio reaches its next eligible transition; a
valid schedule gap retains current tuning. RF Guard, endpoint, transmit/busy, and
receive-only checks still apply.

Plan Builder now makes radio assignments visible and supports review and swaps
with RF Guard. It shows cautions for configured conflicting radios, including
inactive ones, so review shared-resource and physical antenna concerns before
starting equipment. A caution acknowledgement does not prove physical isolation.

### Reviewed manual Mesh ↔ JS8Call forwarding

Open a retained message in the Inbox reader, choose **Forward to JS8Call…** or
**Forward to Mesh…**, select the exact connection and recipient, and review the
addressed payload and complete wire-byte count. Sending requires an explicit
**Send now** action after current source, destination, permission, readiness, and
safety checks. Saved drafts are inert and expire; they do not create an automatic
bridge or background relay. Mesh-to-JS8 forwarding is restricted to reviewed
public feeds. Forwarding does not guarantee delivery or that a person read the
message. JS8 sender/body cleanup preserves the original source for review.

### Responsiveness and Bluetooth lifecycle

CommsPlan 2.0.8 reduces repeated background and presentation work, bounds file
scans and projection writes, and defers hidden Compose and optional startup work.
Responsiveness improved in reported use, while high process CPU remains under
investigation; no fixed timing or CPU reduction is promised. Linux MeshCore helper
cleanup now retains exclusive device ownership until the helper stops and pauses
uncertain reconnects for operator action. Bluetooth worked well in limited operator checks; broader Linux hardware, device-contention, package-helper,
and long-duration soak qualification is not claimed.

### Qualification limits

Linux two-radio scheduler restart, readback, and soak qualification remains
limited. Physical RF, filter, transfer, installed JS8Call variant, and display/DPI
checks are separate from automated source tests. The complete worker-published
Plan Builder input catalog remains deferred. The previously reported
macOS/PySide Compose comments crash is unresolved. High process CPU is still
under investigation, and no software workflow can guarantee that a sent message
is received or read.


## Earlier Station Setup workflow: CommsPlan 2.0.7

In CommsPlan 2.0.7, Station Setup provided a saved checklist for station-wide
setup and a review of the selected radio. The separate **Review & enable
scheduling…** action described in the 2.0.7 release is historical; CommsPlan
2.0.8 uses assignment-driven scheduling, described above. Radio-scoped editors
keep application, connection, operating behavior, schedule, launch, readiness,
and RF Guard changes tied to the radio being reviewed.

## Shared forms library

CommsPlan Spotter provides a station-level Forms Library shared by Spotter,
Compose, and Net Control. You can review MCF form files before adding them to
the station library. Existing bundled forms remain available, and a custom
folder is optional. The library is separate from radio-specific JS8Call setup.

## Offline Call Finder and source updates

Call Finder searches locally available US FCC amateur and GMRS license records
and Canadian ISED amateur callsign records. It can also use US and Canadian
postal reference datasets for approximate grid information. Search is offline
after the reference data is loaded. Use **Sources…** to inspect source status,
record counts, and last refresh.

**Update all** downloads and imports the five supported datasets one at a time:
US amateur licenses, US GMRS licenses, US postal grids, Canadian amateur
callsigns, and Canadian postal grids. The batch stops on cancellation or
failure; already completed datasets remain available. Call Finder does not
create an operator roster entry unless you explicitly choose and review
**Add to Operators**.

## Worldwide map and license references

The Map supports an **All countries** geographic scope for worldwide evidence.
The map's Layers, Filters, Display, Paths, and Planning categories organize map
controls. CommsPlan remembers a saved starting camera view. Its License
references panel can filter by licensing country (US or Canada), service
(amateur or GMRS), and region. References from Call Finder can be reviewed on
the map using available location data. **Prepare map references** builds the
map's reference index from loaded Call Finder sources. **Search this area**
uses the settled current map view to find matching references. A plotted
license reference follows its postal-derived estimate when available; the
licensing country does not establish where the operator is currently operating
or prove activity at the estimated point.

In net-control screens, **FCC lookup…** can fill the current operator entry
draft from a selected reference. Review that draft before saving it. Lookup
does not add a check-in or transmit, and it does not replace information
already managed in the operator roster.

## CommsPlan 2.0.7 and setup fixes

These instructions cover CommsPlan 2.0.7. Check [GitHub Releases](https://github.com/N1MAG/CommsPlan/releases/latest)
for available packages and their release notes.

### Create a Station Traffic Summary

Open **Messages → Inbox → More Actions → Create Traffic Summary…**. This opens
a separate review window using stored station traffic.

1. Choose **Period**, review the UTC start/end, and confirm the station identity.
   Period choices include Last 24 hours, Last 7 days, Custom UTC window and
   Latest stored data.
2. Choose one or more **Groups** and **Source families**, or leave each at All.
   Select **Generate reports**.
3. Open **Review source reports**. Inspect the text, source and reported places.
   Clear **Include** to exclude a report from both outputs. Changing inclusion
   regenerates the drafts, so review your narrative edits again afterward.
4. Switch between **RF Brief** and **Detailed Report**. Their narratives can be
   edited independently. Keep conclusions grounded in the evidence and label
   added interpretation as an **Operator note**. Computed counts and references
   remain tied to the reviewed reports.
5. Use **Save drafts** to retain the pair and its review context. Use
   **Load saved draft…** to reopen it. **Copy / export** applies to the selected
   report. Changing scope or RF length requires generation again before staging.
6. Choose **Review in Compose…** to place the selected report in a Compose draft.
   Review the destination and staging choices there. Replacing existing Compose
   text requires confirmation; this handoff does not transmit the report.

RF length and delivery estimates help plan a transmission; they do not guarantee
its duration or delivery. Stored times may be authored/event times or file
modification times rather than actual receipt. Sender origin can differ from the
event location. Review the sources before distributing a report.

### Review JS8Call selection without losing your draft

The JS8Call selected-target row in Compose shows live external selection
separately from your message draft:

- **Refresh Target** checks the selected radio's JS8Call target.
- **Use Target** copies that target into the draft.
- **Clear selection** verifies and clears the observed selection in that JS8Call
  instance while keeping your draft. A changed or unconfirmed target requires
  another review.

Clearing selection does not send the draft. JS8Call's normal deselection behavior
can resume heartbeats that were paused by a selection.

### Inspect peer evidence and map context

In HF Peer Scheds, enter **Evidence callsign** or select a schedule row, then choose
**Inspect Evidence**. **Evidence details** reveals retained source, radio, time
and coverage information, including passive hearing/receiving evidence.
Recurrence confidence describes retained observations, not delivery probability.
Relay candidates are unverified evidence, not an automatic send route.

Map's station/roster context can show distance and initial true bearing from your
configured home grid when coordinates are available. Grid/state or postal-based
positions remain estimates. This guidance does not prove a station's current
location, reachability or movement.

### Review received Mesh reports and pins

Policy-approved structured and wrapped Mesh reports can contribute received
evidence to Inbox and Map. Message intelligence retains provenance and privacy
rules. Reported event location and node position remain separate evidence;
recognized text does not establish trusted identity or permission to respond.

On Map, open **Manage Pins**, select a pending received claim, and review its raw
text before **Accept** or **Dismiss**. Received claims are read-only. These choices
and **Delete Local** affect local records; they do not retract remote traffic or
broadcast a new PIN. Map display still depends on acceptance and layer/filter
choices.

### Keep MeshCore contact favorites and inspect history

In **Messages → Compose → Local Mesh**, select an eligible MeshCore contact.
Use **Inspect contact history** to review bounded retained traffic, or
**Add favorite** / **Remove favorite** to manage that exact contact identity.

Favorites are specific to the adapter, transport, node and full public key.
Older records containing only a key prefix may be omitted from history.
Contact-list matching is not sender authentication; favorites do not grant trust,
send permission or proof of connectivity.

### Apply an optional FLDigi net tuning preset

In HF Nets, use a row's **FLDigi Tuning…**, enable the preset, and save the
schedule. The default **Center + Narrow** requests 1500 Hz audio and a 500 Hz
receiver filter. You can change its label, audio offset, optional filter width,
signal-preservation choice and application at net start. A blank width leaves
the radio filter unchanged.

During the net, use the radio card's Net menu or **Center + Narrow…** in FLDigi
NCS session tools. Review the named radio/net before applying. Signal preservation
compensates the observed dial frequency in confirmed USB/LSB. Readback confirms
results. **Restore previous tuning** restores the preceding observation for the
same net, radio authority and VFO; an unknown previous filter width is left alone.

The action does not start transmission. It honors connection, transmit/busy and
RF Guard checks. You can still tune manually afterward; a failed automatic preset
requires an explicit manual retry.

### Copy a support report

Open **Station → Health Details → Support report…**. Review the bounded, redacted
cached diagnostics, then choose **Copy** to share them through your normal support
channel. Opening the report does not start new radio probes or upload information.

### Radio setup and Help fixes included

[First-radio setup](/guide/first-radio) explains the complete save and recovery
process. The accumulated fixes include:

- Independent application choices: FLRig, FLDigi, FLMsg and FLAmp do not require
  one another. JS8Call-only and other supported combinations remain separate.
- Existing-radio edits preserve unrelated application settings.
- Selecting a detected application fills its path. Duplicate executable
  suggestions are collapsed while distinct native profiles remain separate.
- Required-field guidance and explicit failed-save warnings explain corrections.
  **Apply to radio setup** updates the draft; **Save radio** commits it;
  **Save draft and close** retains unfinished work. Use **Resume setup** to finish
  adding a new radio.
- Radio actions and bands wrap for narrower windows or larger fonts. Full
  validation and recovery guidance scroll with the selected section; save and
  close actions remain available outside the field scroll.
- Message-file browsing does not require enabling application startup control.
- Help has left-side contents, **Filter topics**, and **Find in guide** search.

An executable may be shared, but independently controlled radios need appropriate
native application instances and distinct API connections. The rigctld control
port is separate from FLDigi and JS8Call APIs. Changing only a CommsPlan port does
not configure another instance in the native application.

### Release scope and testing

CommsPlan 2.0.7 did not add outbound PIN broadcasts, active peer-query
automation, or cross-transport forwarding. Platform/DPI, installed JS8Call
variants, physical Mesh, and live-radio/filter/transfer checks remain separate
from automated source tests. See the 2.0.7 release notes for its qualification
limits.

## Continue

- [Configure your first radio](/guide/first-radio)
- [Understand the operating picture](/guide/operating-picture)
- [Install or upgrade CommsPlan](/install/)
