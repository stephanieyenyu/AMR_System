# API Reference

**Source** `line-backend/app/main.py`
**Totals** 39 HTTP routes, 14 outbound robot calls, 11 LINE conversation entry points, 6 scheduled jobs

Routes are grouped by who calls them, not by URL. Two routes may share the same URL prefix, but needs different permissions: one for residents, one for admins only. Sorting by URL would hide that.

---

## Authentication overview

| Caller | Mechanism | Routes |
|---|---|---|
| LINE Platform | `X-Line-Signature` HMAC verification | 1 |
| Resident (LIFF in LINE) | LIFF ID Token + recipient match | 5 |
| Staff browser | HTTP Basic Auth (`require_admin_auth`) | 30 |
| Robot service | **none** | 2 |
| Health check | none | 1 |

Three types of services intentionally neglect identity verification, the reasons for which are explained in `main.py` L44–55: `/webhook` verifies LINE's own signature; `/liff/*` and QR pickup services are for residents without credentials; robot callbacks do not require credentials, adding credentials would directly disable the callback function.

The grouping was done by caller rather than effect, which has several consequences, documented in [`known-issues.md`](known-issues.md#c-3authentication-is-uneven-across-the-resident-facing-surface).

---

## A. Public endpoints

| Method | Path | Purpose | Side effects |
|---|---|---|---|
| GET | `/` | Health check, returns `{status, message, env}` | none |
| POST | `/webhook` | LINE event entry: `follow` / `unfollow` / `message` / `postback` | dispatches by event type, see section G |

---

## B. Resident-facing (LIFF)

| Method | Path | Auth | Purpose | Transition |
|---|---|---|---|---|
| GET | `/liff/scan` | none | QR pickup page (HTML) | — |
| GET | `/liff/return-request` | none | Return request page (HTML) | — |
| POST | `/liff/return-request/submit` | ID Token | Submit return, body `{id_token, quantity:1–4}` | creates N rows `task_type=return`, `status=pickup_now` |
| POST | `/door-tasks/{door_task_id}/pickup-complete` | ID Token **+ recipient match** | Scan to open door, body `{scanned_content, id_token}` | status unchanged (`arrived`); calls robot to open |
| POST | `/door-tasks/{door_task_id}/complete` | **none** | "Pickup done" / "Return placed" — close door | `arrived → completed` |

`pickup-complete` checks three things: the scanned content must be equal to the `door_task_id`;  the ID Token must be verified via LINE; and the token's `sub` must appear in that task's 'package_recipients`. This verification coverage is uneven; please refer to
[`known-issues.md`](known-issues.md#c-3authentication-is-uneven-across-the-resident-facing-surface).

---

## C. Robot → backend (callbacks)

| Method | Path | Auth | Purpose | Transition |
|---|---|---|---|---|
| POST | `/door-tasks/{door_task_id}/arrived` | none | Robot reached the resident waypoint | `delivering → arrived` for the whole group |
| POST | `/packages/{package_id}/returned` | none | Robot back at the operations room | writes `returned_at` — superseded by polling, see below |

The robot only initiates contact on these two paths. All the other status checks are initiated by polling from the backend.

---

## D. Dashboard pages (Basic Auth, HTML)

| Method | Path | Template |
|---|---|---|
| GET | `/admin` | `dashboard.html` |
| GET | `/admin/reports` | `reports.html` |
| GET | `/admin/exceptions` | `exceptions.html` |
| GET | `/admin/residents` | `residents.html` |

---

## E. Dashboard API (Basic Auth, 26 routes)

### E-1　Parcel creation and listing

| Method | Path | Params | Purpose | Transition |
|---|---|---|---|---|
| POST | `/packages` | `{unit, recipient_name?, quantity:1–4}` | Register arrival, push notification | creates N rows `status=pending` sharing a `creation_batch_id` |
| GET | `/admin/packages` | `page, page_size, date_from, date_to, unit` | Main table, server-side paging and filtering | read-only |
| GET | `/admin/packages/live` | — | Only in-flight parcels, for alert banners, dispatch buttons, door mapping | read-only |
| GET | `/admin/packages/by-unit` | `unit` | Single-unit lookup, statuses collapsed into 4 groups | read-only |
| POST | `/admin/packages/delete` | `{package_ids[]}` | Hard delete; blocks parcels occupying a door | removes from `packages` + `package_recipients` |

### E-2　Dispatch flow

| Method | Path | Params | Purpose | Transition |
|---|---|---|---|---|
| POST | `/packages/{package_id}/place` | `{door_id}` | Assign a door, call robot to open | writes `door_id` / `door_task_id` / `door_assigned_at`; status stays `pickup_now` |
| POST | `/packages/{package_id}/release-door` | — | Release door, return to "awaiting placement" | clears `door_id` / `door_task_id` |
| POST | `/admin/dispatch-batch` | — | Dispatch every loaded parcel at once | `pickup_now → delivering`, writes `stop_dispatched_at` |

### E-3　Robot and cargo doors

| Method | Path | Purpose | Side effects |
|---|---|---|---|
| GET | `/admin/robot-status` | Proxy live robot state (position, battery, doors) | read-only |
| POST | `/admin/robot/recall` | Emergency recall, terminate current mission | calls robot `/api/robot/recall` |
| POST | `/admin/robot/recharge` | Send to charging dock | calls robot `/api/robot/recharge` |
| POST | `/admin/doors/manual-open` | **The only path that opens a door** | writes `return_door_opened_at` |
| POST | `/admin/doors/manual-close` | **The only path that closes a door** | writes `door_closed_at` |

### E-4　Exception handling

| Method | Path | Purpose | Transition |
|---|---|---|---|
| GET | `/admin/packages/exceptions` | Pending refused / timed-out / declined parcels | read-only |
| POST | `/packages/{package_id}/acknowledge` | Acknowledge a declined parcel | writes `acknowledged_at` |
| POST | `/packages/{package_id}/confirm-return-retrieved` | Confirm return item removed from door | writes `return_retrieved_at` |
| POST | `/packages/{package_id}/force-resolve` | Manual close when robot hardware was bypassed | backfills `returned_at` / `return_door_opened_at` / `door_closed_at` or `acknowledged_at` |
| POST | `/admin/packages/close-case-batch` | Batch case closure | writes `case_closed_at` |
| POST | `/packages/{package_id}/redispatch` | Redeliver | creates new parcel `status=pending`; old row gets `redispatched_at` / `redispatched_to` |
| POST | `/packages/{package_id}/notify-pending-pickup` | Resend return notice (once only) | writes `pending_pickup_notified_at` |
| POST | `/packages/{package_id}/notify-completed-leftover` | Completed but items possibly left behind | updates `pending_pickup_notified_at` (repeatable) |

### E-5　Resident bindings

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/bindings` | Dropdown for the parcel creation form (active bindings only) |
| GET | `/admin/line-bindings` | All binding records |
| POST | `/admin/line-bindings/{line_user_id}/delete` | Remove mistaken or malicious bindings |
| POST | `/admin/line-bindings/{line_user_id}/update` | Change unit or name, body `{unit, name}` |

### E-6　Reports

| Method | Path | Params | Purpose |
|---|---|---|---|
| GET | `/admin/reports/daily` | `date` (YYYY-MM-DD) | Daily status summary + `TaskLog` timeline |

---

## F. Backend → robot (14 outbound calls)

All routed through `call_robot_api()`, which owns timeout, `retries=1`, and failure
logging to `TaskLog`. Target set by `ROBOT_API_BASE_URL`.

| Robot endpoint | Triggered by | When |
|---|---|---|
| `POST /api/door-tasks/{id}/assign` | `try_assign_door` | Staff clicks "place parcel" |
| `POST /api/door-tasks/{id}/assign-timeout` | `check_assign_timeout`, `release_door` | Assignment timed out, or manual release |
| `POST /api/doors/load` | `/admin/dispatch-batch` | Batch close before departure |
| `POST /api/robot/dispatch` | `/admin/dispatch-batch`, `advance_trip_or_return` | Depart for this stop |
| `POST /api/door-tasks/{id}/pickup-complete` | `/door-tasks/{id}/pickup-complete` | Resident scan verified, open door |
| `POST /api/door-tasks/{id}/complete` | `complete_pickup` | Resident confirms, close door |
| `POST /api/door-tasks/{id}/cancel` | `handle_reject_at_door`, `handle_cancel_return`, `check_pickup_timeout` | Refused / return cancelled / timed out |
| `POST /api/door-tasks/return` | `advance_trip_or_return` | Trip finished, head home |
| `POST /api/doors/return-open` | `/admin/doors/manual-open` | Staff opens door to retrieve returns |
| `POST /api/doors/return-complete` | `/admin/doors/manual-close` | Staff closes door |
| `POST /api/doors/return-timeout` | `check_return_timeout` | Door open 8 minutes without closing |
| `POST /api/robot/recall` | `/admin/robot/recall` | Emergency recall |
| `POST /api/robot/recharge` | `/admin/robot/recharge` | Return to charger |
| `GET /api/dashboard/status` | `/admin/robot-status`, `poll_robot_returned` | Live state; return detection |

---

## G. LINE conversation interface

Not HTTP routes — all arrive through `/webhook`.

### Text commands

| Input | Handler | Effect |
|---|---|---|
| `<unit> <name>` (two tokens) | `handle_text_binding` | Create or update `line_binding` |
| "我的包裹" (my packages) | `handle_my_packages_query` | Query only, no action buttons |
| "關門" (close door) | `handle_close_door_request` | Recovery when the completion button was missed |
| "開啟/關閉限本人通知" | `handle_solo_notify_toggle` | Toggle `solo_notify` |
| "使用說明" (help) | — | Fixed help text |

### Postback actions

| Action | Trigger | Transition |
|---|---|---|
| `PICKUP_NOW` | "Pick up" on arrival notice | `pending → pickup_now` |
| `SCHEDULE_PICKUP` | "Schedule pickup" on arrival notice | `pending → pickup_now`, writes `scheduled_pickup_at` (rounded to half hour, 20 units per slot) |
| `REJECT` | "Decline" on arrival notice | `pending → voided` |
| `PICKUP_DONE` | "Pickup complete" on arrival notice | `arrived → completed` |
| `REJECT_AT_DOOR` | "Refuse" after robot arrives | `arrived → rejected_at_door` |
| `CANCEL_RETURN` | "Not returning now" after arrival | `arrived → rejected_at_door` (same path as refusal) |

`PICKUP_NOW` / `SCHEDULE_PICKUP` / `REJECT` apply across a whole `creation_batch_id`.
`REJECT_AT_DOOR` / `CANCEL_RETURN` apply across a `door_task_id` — every door at that stop.

---

## H. Scheduled jobs

Not APIs, but they changes state.

| Job | Interval | Effect |
|---|---|---|
| `check_pickup_timeout` | 1 min | `arrived` past 8 minutes → `returned_timeout` |
| `check_assign_timeout` | 1 min | Door assigned but never loaded → release door |
| `check_return_timeout` | 1 min | Door open 8 minutes → robot auto-closes, resets `return_door_opened_at` |
| `check_schedule_reminder` | 1 min | Push reminder 2 hours before scheduled slot, writes `schedule_reminder_sent_at` |
| `poll_robot_returned` | 20 sec | Poll `/api/dashboard/status`, backfill `returned_at` on return detection |
| `check_stuck_dispatch` | 2 min | Safety net, retries a stalled `advance_trip_or_return` |

---

## Caveats

For a full explanation, please refer to [`known-issues.md`](known-issues.md).

**`POST /packages/{package_id}/returned` is superseded.** Its docstring says the robot
module calls it, but `poll_robot_returned` (L2813) states that the robot does not perform return reports, therefore the backend polls every 20 seconds instead. The two
statements contradict each other; polling is actually in operation. The route should be removed.

**`/admin/robot-status` accepts GET only.** The robot's `_push_dashboard_status_loop` would POST status here, but it is commented out. Uncomment it and the robot gets a 405 with no visible failure.

**Authentication requirements are not uniform** across the resident-reachable routes in
section B. Reviewed in [`known-issues.md`](known-issues.md#c-3authentication-is-uneven-across-the-resident-facing-surface).
