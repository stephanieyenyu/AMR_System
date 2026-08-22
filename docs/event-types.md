# Event Types

Every state transition, operator action and failure in the system writes a row to
`task_logs`. This document lists all 53 event types observed in the database, grouped by
the part of the flow they belong to.

**Source** `task_logs`, 2026-08-14 snapshot — 53 distinct types across 2,678 rows.
This list supersedes the outdated comment block in `models.py` (see
[`known-issues.md`](known-issues.md) D-1).

---

## Grouped by flow

### Creation and dispatch

| Event | Meaning |
|---|---|
| `created` | package registered |
| `queued` | Queued, waiting for dispatch |
| `door_assigned` | Cargo door assigned |
| `door_joined` | Joined an existing trip — several packages, one trip |
| `multi_package_assigned` | Several doors assigned in one operation |
| `dispatched` | Dispatch command sent to the robot |
| `arrived` | Robot reached the unit |
| `trip_wait` | Trip waiting |
| `trip_completed` | Trip finished |

### Resident pickup

| Event | Meaning |
|---|---|
| `pickup_requested` | Resident asked to collect now |
| `pickup_scheduled` | Resident booked a collection time |
| `pickup_opened` | QR scan released the door |
| `completed` | Collection completed |
| `pending_pickup_notified` | Overdue-collection reminder sent by scheduler |
| `schedule_reminder_sent` | Pre-booking reminder sent by scheduler |

### Refusal and expiry

| Event | Meaning |
|---|---|
| `rejected` | Resident declined before the robot set off |
| `rejected_at_door` | Resident refused after the robot arrived |
| `voided_acknowledged` | Expiry acknowledged |

### Return flow

| Event | Meaning |
|---|---|
| `return_requested` | Resident requested a return |
| `return_cancelled` | Resident cancelled the return |
| `return_door_opened` | Return door opened |
| `return_retrieved` | Building staff confirmed collection of the returned item |
| `returned` | Robot carried the package back |
| `returned_and_opened` | Carried back, door opened |
| `returned_timeout` | Return timed out — written by scheduler |

### Operator actions

| Event | Meaning |
|---|---|
| `door_closed` | Door closed |
| `manual_door_opened` | Door opened by an operator |
| `manual_door_closed` | Door closed by an operator |
| `door_released_manually` | Door released by an operator |
| `case_closed` | Case closed |
| `force_resolved` | package force-resolved |
| `redispatched` | package redispatched |
| `package_deleted` | package record deleted |
| `task_recalled` | Robot task recalled |
| `robot_recall_requested` | Recall requested |
| `robot_recharge_requested` | Return-to-charge requested |

### LINE account binding

| Event | Meaning |
|---|---|
| `line_binding_updated` | Binding details changed |
| `line_binding_deleted` | Binding removed |
| `user_unfollowed` | User blocked or unfollowed the channel |

### Failures

Logged at `level=error` or `level=warning`. Every outbound robot call routes through one
egress function, so a failed call always produces a row here — this is what makes the
failure analysis in [`metrics.md`](metrics.md) possible.

| Event | Meaning |
|---|---|
| `assign_timeout` | Door assignment timed out |
| `assign_timeout_failed` | Handling of an assignment timeout itself failed |
| `door_assign_failed` | Door assignment failed |
| `dispatch_failed` | Dispatch call failed |
| `cancel_task_failed` | Task cancellation failed |
| `complete_failed` | Completion handling failed |
| `pickup_open_failed` | Pickup door failed to open |
| `poll_returned_failed` | Return poll failed |
| `return_failed` | Carry-back failed |
| `return_open_failed` | Return door failed to open |
| `return_door_open_failed` | Return cargo door failed to open |
| `robot_recall_failed` | Recall call failed |
| `show_qr_failed` | QR code failed to display on the robot screen |
| `notify_failed` | Push notification failed |

---

## Missing from the previous `models.py` comment

Nine types appear in the database but were absent from the comment block this document
replaces:

```
trip_completed          trip_wait               queued
door_joined             manual_door_opened      manual_door_closed
schedule_reminder_sent  return_open_failed      show_qr_failed
```

---

## Alphabetical

For cross-referencing against front-end display labels.

```
arrived                    pickup_scheduled
assign_timeout             poll_returned_failed
assign_timeout_failed      queued
cancel_task_failed         redispatched
case_closed                rejected
complete_failed            rejected_at_door
completed                  return_cancelled
created                    return_door_open_failed
dispatch_failed            return_door_opened
dispatched                 return_failed
door_assign_failed         return_open_failed
door_assigned              return_requested
door_closed                return_retrieved
door_joined                returned
door_released_manually     returned_and_opened
force_resolved             returned_timeout
line_binding_deleted       robot_recall_failed
line_binding_updated       robot_recall_requested
manual_door_closed         robot_recharge_requested
manual_door_opened         schedule_reminder_sent
multi_package_assigned     show_qr_failed
notify_failed              task_recalled
package_deleted            trip_completed
pending_pickup_notified    trip_wait
pickup_open_failed         user_unfollowed
pickup_opened              voided_acknowledged
pickup_requested
```

---

**On the descriptions.** These were derived from the event names and their observed usage
rather than read off a specification, since none exists. Four are worth confirming
against source before relying on them: `trip_wait`, `queued`, `task_recalled`, and
`returned_and_opened`.
