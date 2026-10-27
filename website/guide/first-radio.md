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
2. On **Radio**, search **Radio model** by manufacturer or model and select a
   result, or enter **Manufacturer** and **Model** manually. The cached Hamlib
   catalog has built-in fallbacks; manual entry remains available. A model can
   suggest the name for an untouched new radio; **Radio name** stays separately
   editable and existing names are preserved. Choose the role, select supported
   antenna bands using the checkboxes under **More radio settings** (leave all
   unchecked if no band limit is specified), then choose **Control backend**.
   Choosing a model does not change the backend, bands or selected applications:

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
4. Choose **Configure…** beside each selected application. The form stays inside
   Radio setup; **Back to radio setup** returns to the radio choices. Review the applicable
   executable or launcher, endpoint, native profile or INI, message paths, and
   who starts it. Fields marked **(required)** must be completed before saving
   the radio. Missing or invalid values are explained above the fields. Fresh
   JS8Call/FLDigi forms propose common API ports `2442`/`7362`; confirm they match
   the applications' API settings. Use the actual host and port for remote applications. Software
   you start yourself or run remotely does not require a local CommsPlan launcher.
5. Choose **Apply to radio setup** in each application form to return its settings to the
   radio draft. Then review **Operating behavior**, **Schedule**, and **Review**,
   and choose **Save radio** in **Radio setup**. This commits the radio and
   its reviewed application settings together.
6. Reopen the same saved radio to make corrections. **Software → Change software
   used…** changes its application selections. When ready for live participation,
   choose **Radio actions → Use Radio**. Scheduling has its own review below.

CommsPlan Spotter, External JS8Spotter, and CommStat open their owning pages
through **Software settings…**. For a new or changed radio, **Save & Open** saves
it before opening that page. **Cancel** in that handoff keeps the radio draft open.

For another radio, the installed executable can be shared, while each radio needs
its own application instance settings and API connection. The rigctld control
port is separate from FLDigi's XML-RPC port and JS8Call's TCP API port. Use a
separate FLDigi settings folder and a distinct JS8Call native profile as needed,
with endpoints configured to match those applications. Changing a port only in
CommsPlan does not configure another native instance. If Save radio reports an
endpoint already assigned to another radio, the current entries remain a draft;
choose Resume setup, correct the instance settings, and save before choosing
Radio actions → Use Radio.

If a save is rejected, the warning explains that the radio was not added or its
changes were not saved and names the problem. Choose **Keep editing**, correct
the settings, then **Save radio** again. If saving a draft itself could not be
confirmed, keep the current entries open until that problem is resolved.

On compact displays, actions and band choices wrap. Full validation and recovery
guidance scroll with the selected section; scroll within it to reach the fields.
The save/close actions stay outside that field scroll. These accumulated setup changes are covered in the [2.0.7 update](/guide/whats-new#commsplan-2-0-7-and-setup-fixes).

## Cancel or recover unfinished setup

- **Save radio:** saves the current entries and reviewed app choices together.
  If an older draft exists, confirm **Save current setup** to replace it; loading
  that older draft is optional. If a required field is missing or invalid,
  setup opens the application and focuses the first field to correct. Apply the
  correction, then choose **Save radio** again.
- **Save draft and close:** keeps unfinished entries without creating or changing
  a saved radio. For a new radio, Configured Radios shows the unfinished setup
  name and **Resume setup**. Choose it to continue, then **Save radio** to add the
  radio. Existing radios offer their retained draft when reopened.
- **Load previous draft…:** loads the saved copy instead of current entries.
  If you have edited the form, compare the names, apps, and bands shown before
  confirming replacement. **Keep editing** preserves what is on screen.
- **Close setup:** offers Save radio, Save draft and close, Discard current
  changes and close, or Keep editing when there are unsaved entries. Discarding
  current changes keeps existing radios and previous recovery drafts unchanged.
- **Delete previous draft:** removes only that recovery copy and keeps current
  form entries and saved radios. Current entries can also replace it when saved.
- **Bands:** check supported antenna bands on Radio. Legacy `20, 40, 80` values
  are recognized as `20M, 40M, 80M`; unknown saved values remain visible to review.
  Leave all unchecked if no band limit is specified.

**Find installed…** highlights the search, detected, and selected states with
text and shared theme colors. A component's Configure form suggests only that
component. Selecting a suggestion fills that component's application path. Duplicate executable suggestions are collapsed; distinct native profiles remain separate. Selecting a detected path does not verify a radio connection.

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

## Assign a plan to follow its schedule

Assign a valid Frequency Plan to the selected radio when you are ready for it to
follow that schedule. CommsPlan validates the saved radio, application identity,
plan, enabled sources, and readiness before issuing automatic commands; a
separate enable action is not required. Use the radio card calendar to inspect
or change the assignment. Pause holds schedule changes until you resume. RF
Guard, endpoint checks, and transmit/busy protection still apply.

## Continue

- [Understand the operating picture](/guide/operating-picture)
- [See what's new in CommsPlan 2.0.8](/guide/whats-new)
- [Install or upgrade](/install/)
