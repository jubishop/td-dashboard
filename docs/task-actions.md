---
status: current
---

# Task actions

Jubi accepted this design on 2026-10-22, before it was built. It adds the
dashboard's first changes to td data: closing a task as done or as abandoned,
and returning it to open. Jubi kills agent sessions often. A killed session
leaves its task open or in progress, so these tasks pile up unless someone
closes them by hand. The dashboard is where Jubi sees them.

Each decision below was the recommended option of an interview. The reasons
are the tradeoffs presented then.

## Actions

td has no abandoned status. Its statuses are open, in progress, blocked, in
review, and closed, so closing with a reason is td's own way to abandon work.

| Action | td commands |
| --- | --- |
| Done | `td approve --reviewed-by "Justin Bishop"` for a task in review; otherwise `td close` with the note or "Closed as done from the dashboard" |
| Won't do | `td update --add-label wont-do`, then `td close -m "Won't do: <note>"`; a failed close takes the label back off |
| Back to open | `td unstart`, `td unblock`, `td reject`, or `td reopen` for in progress, blocked, in review, or closed; reopening also removes `wont-do` |

td records that name on the review beside the dashboard's td session, which
runs the command. The first build recorded only that session. On 2026-10-23
Jubi confirmed that Done should name Justin Bishop, as accepted; the reason is
unknown.

There is no delete and no other editing. Delete would hide history, and
fuller editing would add surface that each td upgrade could break. Back to
open covers a stale task that still needs work, so the next agent picks it up.

Won't do tasks keep the `wont-do` label. The dashboard leaves them out of the
closure charts and counts and shows a Won't do chip. The activity sparklines
still count their history, like any other activity. Agents can apply the
same label, and `td list -l wont-do` finds them.

## Interface

- The issue page has the three actions and an optional note.
- Each Active work row on the overview and repository pages has a compact
  menu with the same actions, so a recognizable task closes without opening
  it. With 12 unclosed tasks on 2026-10-22, bulk selection was not needed.
- An action applies with one click. A banner then confirms it and offers Undo,
  which runs Back to open. td's own error messages appear in the same banner.
- The minute reload pauses while a menu or note is open.

## Stale and live tasks

A task's last activity is the newest of its update time, logs, handoffs,
comments, reviews, and status changes, close to how `td unstart --stale`
judges whether a session is alive. A task in progress or in review with no
activity for 24 hours shows Stale, and the Updated column uses its last
activity. A task with activity in the last 15 minutes shows Live, and its
issue page warns that an agent may still be working on it. On phones, the Updated column
is hidden so the table fits beside the action menus; the markers remain.

## Write path

The dashboard never writes td-sync's data: `project.db` is rebuilt from td's
event log. The Pi instead runs the real `td` as a second td device:

- The Mac's weekly auto-update builds Linux `td` from the same tag as
  td-sync, so the two stay on one release.
- A sync-scoped API key named `td-dashboard` stays on the Pi, outside Git.
- The service has a writable state directory holding one td workspace per
  project, created the first time an action touches that project.
- td-sync applies the pushed change at once, and the Mac's td pulls it at its
  next command in that repository. td-sync keeps the last of two concurrent
  writes.

Two alternatives were rejected. A queue that the Mac applies stops while the
Mac is off and delays every action. SSH from the Pi to the Mac fails when the
Mac is unreachable and opens a path from the Pi into the Mac.

## Security

Changes need no login, so anyone who can read the dashboard can change a task:
the home LAN and Jubi's tailnet. The actions are reversible and recorded in
td history, so a bad click costs little. A change must be a POST whose
`Origin` matches its `Host`, one of the dashboard's own names, so another web
page cannot submit one, and the existing Host check against DNS rebinding
stays. Restricting changes to the tailnet and a shared password were rejected as
more friction than these actions need.
