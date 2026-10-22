---
title: Start Here
description: Choose the right CommsPlan setup path for a new station, an upgrade, or an existing installation.
---

# Start here

<div class="doc-badge-row">
  <span class="doc-badge">CommsPlan 2.0.6</span>
  <span class="doc-badge">Windows · Linux · macOS</span>
  <span class="doc-badge">Choose the right path</span>
</div>

CommsPlan is a coordinated operations console for HF digital stations. It does not replace FLRig, FLDigi, FLMsg, FLAmp, JS8Call, VarAC, VARA, MeshCore, or Meshtastic. It coordinates those applications as one station while preserving their own profiles and technical controls. The [GitHub Releases page](https://github.com/N1MAG/CommsPlan/releases/latest) lists published packages, checksums, and release notes.

The Station Setup checklist and workflow described below are part of CommsPlan
2.0.6. Use the installation and upgrade guide to select a currently published
package or source installation.

## Which path fits your station?

<div class="decision-grid">
  <div class="decision-card">
    <strong>New to CommsPlan</strong>
    <p>Install a package from the latest published release, enter the station identity, and use Station Setup to review the first radio.</p>
  </div>
  <div class="decision-card">
    <strong>Upgrading a single-radio station</strong>
    <p>Close CommsPlan and companion applications, install the update, then review the saved station setup and radio configuration.</p>
  </div>
  <div class="decision-card">
    <strong>Already using CommsPlan</strong>
    <p>Follow the upgrade steps for your installation type and keep the verified station backup available.</p>
  </div>
</div>

[Open the installation and upgrade guide →](/install/)

## Before you begin

- Know which computer and operating system will run CommsPlan.
- Know where an existing source checkout was installed, if you use one.
- Close CommsPlan and affected companion applications before an upgrade.
- Keep the radio manufacturer and model available for the guided setup.
- Treat a verified station backup as mandatory for an upgrade.

::: tip One radio is enough
You do not need to add another radio to use CommsPlan. One-radio operation remains fully supported.
:::

## First successful session

The recommended order is:

1. Open **Configuration → Main → Station setup** and review the saved checklist.
2. Complete operator and operating-group details.
3. Choose a radio and use its scoped editors to review software, connections, behavior, and schedule.
4. Review the saved setup summary. Scheduling stays off until you explicitly review and enable it.
5. Open Ops Center, Messages, and Map to confirm the resulting station context.

<div class="expectation">
  <strong>Expected result</strong>
  The saved checklist reflects the configured station. The reviewed radio and its application identities are available in the scoped setup editors.
</div>

## Understand the operating model

CommsPlan keeps three questions connected:

- **Where do I need to be?** Radio, group, band, frequency, and mode.
- **When do I need to be there?** Schedules, events, and the next meaningful transition.
- **What do I do when I am there?** The task, message, SOP action, or station service appropriate to that context.

The Station Control Bar keeps radio and schedule context visible. The active workspace focuses on the task and why an action is ready, blocked, or unnecessary. Message Intelligence carries source-aware summaries into Messages, Ops Center, and Map; Plan Builder connects schedules to the radio that owns them.

## Continue

- [Install or upgrade CommsPlan](/install/)
- [Configure your first radio](/guide/first-radio)
- [Understand the operating picture](/guide/operating-picture)
- [Connect a Local Mesh device](/integrations/mesh)
- [Prepare a support request](/support)
