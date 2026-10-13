---
title: Local Mesh
description: Connect MeshCore or Meshtastic to CommsPlan and deliberately enable outbound messages.
---

# Connect Local Mesh

<div class="doc-badge-row">
  <span class="doc-badge">MeshCore</span>
  <span class="doc-badge">Meshtastic</span>
  <span class="doc-badge">Receive and optional send</span>
</div>

Local Mesh connects station-level MeshCore or Meshtastic services to CommsPlan. Received text and position evidence can join the Inbox and Map without treating the mesh device as an HF radio profile. Mesh is a supported station transport; this guide does not promise future cross-transport relay or automatic forwarding.

## Add a saved device

1. Open **Configuration → Mesh**.
2. Select **Add Device**.
3. Choose **MeshCore** or **Meshtastic**.
4. Choose the supported connection type for that protocol.
5. Enter a clear connection name. This name is separate from the Bluetooth advertised name.
6. Select how CommsPlan may use received data: **Inbox**, **Map**, or both.
7. Save the device.

## Find and authenticate a Bluetooth device

1. Keep CommsPlan open and select **Scan**.
2. Select the exact discovered device.
3. Select **Use Device** so the stable discovered identity becomes part of the pending configuration.
4. Save, then select **Connect**.
5. On Linux, if the secured device is not paired, CommsPlan makes one bounded pair-before-connect attempt. Respond to the desktop PIN prompt using the PIN displayed by the device.

CommsPlan does not store the Bluetooth PIN. Scanning never replaces a saved identity until the operator chooses a result.

::: tip If Linux pauses reconnects
If Bluetooth is powered off, the pairing is cancelled, or the device stops advertising, CommsPlan pauses automatic reconnect attempts. Restore Bluetooth and device availability, wait for advertising, then select **Connect** once.
:::

## Enable outbound messages

Receiving data does not automatically authorize transmission.

1. Open **Configuration → Mesh**.
2. Select the saved device.
3. Turn on **Allow Send** in **Saved Devices**.
4. Confirm that the device is connected.
5. Review and accept the required channel policy or select a known direct node.
6. Open **Messages → Compose → Local Mesh**.
7. Select the destination, write the message, and review the final confirmation before sending.

<div class="expectation">
  <strong>Expected result</strong>
  The selected saved device reports connected, Compose lists it as available rather than offline, and only accepted channel policies or known direct nodes can be selected for the message.
</div>

## What the controls mean

| Control | Meaning |
|---|---|
| **Inbox** | Store received mesh text in CommsPlan's message pipeline. |
| **Map** | Allow available node positions to contribute map context. |
| **Allow Send** | Authorize outbound use of this specific qualified saved connection. Off by default. |
| **Connect** | Start or resume the saved connection. |
| **Check Configuration** | Validate visible settings without silently connecting, sending, or reconfiguring the device. |

## Avoid duplicate device entries

Use **Scan → select the exact result → Use Device** when reconnecting a Bluetooth device. Do not use **Add Device** each time the same physical device advertises again. The stable saved identity—not the display name alone—is what lets CommsPlan associate health and Compose availability with the correct connection.

## If Compose reports Offline

Check these in order:

1. The intended saved device is selected and connected.
2. **Allow Send** is enabled for that saved device.
3. The protocol and connection type support sending.
4. A channel policy is accepted or a known direct node is available.
5. Another operation is not already using the same adapter.
6. On Linux Bluetooth, the device remains paired and advertising.

Use **Check Configuration** for actionable missing fields. If the behavior persists, collect the diagnostics described in the [support guide](/support).
