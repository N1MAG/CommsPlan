---
title: Start Here
description: Choose the right CommsPlan setup path for a new station, an upgrade, or an existing installation.
---

# Start here

<div class="doc-badge-row">
  <span class="doc-badge">CommsPlan 2.0.5</span>
  <span class="doc-badge">Windows · Linux · macOS</span>
  <span class="doc-badge">Choose the right path</span>
</div>

CommsPlan is a coordinated operations console for HF digital stations. It does not replace FLRig, FLDigi, FLMsg, FLAmp, JS8Call, VarAC, VARA, MeshCore, or Meshtastic. It coordinates those applications as one station while preserving their own profiles and technical controls. CommsPlan is the current product name; FreqInOut remains the compatibility name in established profiles, launchers, package identity, and source paths.

## Which path fits your station?

<div class="decision-grid">
  <div class="decision-card">
    <strong>New to CommsPlan</strong>
    <p>Install the current package, enter the station identity, and use Guided Add Radio for the first radio.</p>
  </div>
  <div class="decision-card">
    <strong>Upgrading a single-radio station</strong>
    <p>Close CommsPlan and companion applications, install the update, then review Upgrade Existing Station.</p>
  </div>
  <div class="decision-card">
    <strong>Already using FreqInOut 2.0</strong>
    <p>Install CommsPlan over the established application and keep using the established station profile.</p>
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
You do not need to add another radio to use CommsPlan 2.0.5. The multi-radio architecture keeps one-radio operation fully supported.
:::

## First successful session

The recommended order is:

1. Enter the station identity and location under **Configuration**.
2. Define the operating groups used by scheduling, filtering, access policies, and publication rules.
3. Add or review the first radio and select only the software it actually uses.
4. Review Software Administration and Launch Control for that radio.
5. Create or assign a frequency plan and schedule.
6. Open Ops Center, Messages, and Map to confirm the resulting station context.

<div class="expectation">
  <strong>Expected result</strong>
  CommsPlan reopens with the same station profile, the configured radio appears under Configuration, and its selected application identities appear in Software Administration.
</div>

## Understand the operating model

CommsPlan keeps three questions connected:

- **Where do I need to be?** Radio, group, band, frequency, and mode.
- **When do I need to be there?** Schedules, events, and the next meaningful transition.
- **What do I do when I am there?** The task, message, SOP action, or station service appropriate to that context.

The Station Control Bar keeps radio and schedule context visible. The active workspace focuses on the task and why an action is ready, blocked, or unnecessary. Message Intelligence carries source-aware summaries into Messages, FIO Spotter, Ops Center, and Map; Plan Builder connects schedules to the radio that owns them.

## Continue

- [Install or upgrade CommsPlan](/install/)
- [Configure your first radio](/guide/first-radio)
- [Understand the operating picture](/guide/operating-picture)
- [Connect a Local Mesh device](/integrations/mesh)
- [Prepare a support request](/support)
