---
title: Configure Your First Radio
description: Review the saved station checklist and configure one radio through its scoped setup editors.
---

# Configure your first radio

CommsPlan's **Station Setup** page gives you a saved checklist for station-wide
items and a radio-scoped review for each configured radio. The checklist reads
the saved station state; it does not create radio records or change scheduling
by itself.

This walkthrough describes the updated radio setup workflow. Use the built-in
help for your installed version, and check the
[GitHub Releases page](https://github.com/N1MAG/CommsPlan/releases/latest) for
currently available installation packages and release-specific instructions.

## Open Station Setup

1. Open **Configuration**.
2. Choose **Main → Station setup**.
3. Review the checklist and select the item you want to complete.

Start with operator identity and operating groups. Then select a radio to review
its application assignments, connections, operating behavior, schedule, launch
choices, readiness, and RF Guard settings. Each editor is scoped to the chosen
radio so a change is not accidentally applied to another station profile.

## Add the radio and select its applications

1. Open **Configuration → Radios → Add Radio**. From the fresh-install station
   checklist, open **Radios & applications** to reach radio configuration.
2. On **Radio**, enter a name and role, then choose **Control backend**:
   - **FLRig** selects FLRig; review its host and port, normally
     `127.0.0.1:12345`.
   - **rigctld** uses the Hamlib service's host and port, normally
     `127.0.0.1:4532`. It does not require FLRig.
   - **JS8Call** selects JS8Call; configure its TCP API connection.
   - **Manual** leaves frequency control with the operator.
3. On **Applications**, select only the software this radio uses. FLRig, FLDigi,
   FLMsg, and FLAmp are independent selections. FLRig alone is valid; adding
   FLDigi is not required for FLRig control. FLDigi alone, JS8Call alone, VarAC
   alone, any two of FLDigi/JS8Call/VarAC, or all three are valid selections. Selecting software
   here does not install it.
4. Choose **Configure…** beside each selected application. Review the applicable
   executable or launcher, endpoint, native profile or INI, message paths, and
   who starts it. FLDigi's XML-RPC port is normally `7362`; use the TCP API port
   set in JS8Call. Use the actual host and port for remote applications. Software
   you start yourself or run remotely does not require a local CommsPlan launcher.
5. Choose **Save** in each application dialog to return its settings to the
   radio draft. Then review **Operating behavior**, **Schedule**, and **Review**,
   and choose **Save** in **Radio setup**. The outer Save commits the radio and
   its reviewed application settings together.
6. Reopen the same saved radio to make corrections. **Software → Change software
   used…** changes its application selections. When ready for live participation,
   choose **Radio actions → Use Radio**. Scheduling has its own review below.

CommsPlan Spotter, External JS8Spotter, and CommStat open their owning pages
through **Software settings…**. For a new or changed radio, **Save & Open** saves
it before opening that page. **Cancel** in that handoff keeps the radio draft open.

## Cancel or recover unfinished setup

- **Editing a saved radio:** Cancel closes a clean editor. With unsaved changes,
  choose **Save**, **Discard**, or **Stay**. Discard abandons those edits and keeps
  the previously saved radio and application settings. Stay returns to editing.
- **Adding a radio that has never been saved:** Discard abandons the uncommitted
  setup. Saving only the inner application dialog does not create the radio;
  complete the outer Radio setup Save.
- **Save reports “Draft kept; radio settings were not changed”:** correct the
  reported field and save again. You can also close the editor, reopen **Add
  Radio** (for a new radio) or the same saved radio, and choose **Recover
  unfinished**. Review the recovered draft before saving. Previously saved radios
  remain unchanged.
- **Discard unfinished:** removes the retained unfinished draft, not a saved
  radio or external application files.

If an endpoint is genuinely owned by another radio, review the named owner and
configure the intended separate application instance and port. If FLRig-only
setup asks for another Fast Light component or reports its own radio as the
endpoint owner, keep the draft and report the installed version and exact error.
Adding unused software is not part of the setup process.

## Review the saved setup

Choose **Review saved setup** for the selected radio to inspect the saved
configuration and understand what is ready or still needs attention. Discovery
or suggested values are not applied simply because they appear in a review.
Save changes through the owning settings editor, then return to the checklist
and review again.

<div class="expectation">
  <strong>Expected result</strong>
  The saved checklist reflects the station configuration. The selected radio's
  summary identifies its software, connections, schedule, and any remaining
  setup items.
</div>

## Scheduling requires an explicit review

Scheduling remains disabled until you choose to enable it. From the selected
radio's schedule actions, use **Review & enable scheduling…** and check the
radio, assigned plan and sources, applications, readiness, and safety state.
Enable only after that summary matches the station you intend to operate. Choose
**Disable scheduling** to stop automatic schedule commands for that radio.
These controls do not bypass transmit/busy protection, endpoint checks, or RF
Guard.

## Continue

- [Understand the operating picture](/guide/operating-picture)
- [See what's new in CommsPlan 2.0.6](/guide/whats-new)
- [Install or upgrade](/install/)
