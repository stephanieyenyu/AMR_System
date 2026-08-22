# Data Dictionary

**Source** `line-backend/app/models.py`, `flashbot-robot/src/aurobox/models.py`
**Diagram** [`images/er-diagram.png`](images/er-diagram.png)

## Conventions that apply throughout

**No physical foreign keys exist.** No `ForeignKey` is declared in any of the six tables. Every relationship is maintained by application code and marked `REF` on the ER diagram. Deleting a package requires explicitly clearing `package_recipients`; the database will not cascade.

**Two independent databases.** `line-backend` and `flashbot-robot` each connect to their own PostgreSQL instance and communicate only via HTTP.

**Timestamp bases differ.** `line-backend` stores the raw datetimes in Taipei local time (`now_taipei()` with tzinfo stripped); `flashbot-robot` stores UTC time (`_utc_now_naive()`). **An eight-hour conversion is required before comparing timestamps across the two databases.** See [`known-issues.md`](known-issues.md#c-2-cross-service-timestamp-basis-differs-invisibly).

---

# line-backend

## `packages`

The core table and the sole true source of package status. Deliveries and returns
share this table, and are distinguished by `task_type`.

### Identity and state

| Column | Type | Constraint | Notes |
|---|---|---|---|
| `id` | UUID | PK | defaults to `uuid4()` |
| `unit` | VARCHAR(50) | NOT NULL | Unit label. **Also the robot's navigation waypoint name** — passed to the robot verbatim on dispatch |
| `line_user_id` | VARCHAR(100) | NOT NULL | Recipient's LINE User ID |
| `status` | VARCHAR(30) | NOT NULL | defaults `pending`; eight values, see [state machines](state-machines.md) |
| `task_type` | VARCHAR(20) | NOT NULL | defaults `delivery`. `delivery` = staff-created; `return` = resident-requested |
| `package_count` | INTEGER | NOT NULL | defaults 1, range 1–4. Determines how many doors to open |

### Grouping keys and door assignment

| Column | Type | Notes |
|---|---|---|
| `door_id` | VARCHAR(10) | Assigned door number, NULL when unassigned |
| `door_task_id` | UUID | **Shared by all parcels at the same stop.** Grouped on `line_user_id + unit + task_type`. Reusing the same physical door later produces a different ID |
| `creation_batch_id` | UUID | **Shared by parcels created together** (`quantity > 1`). The arrival notice fires once, but a resident's pickup / schedule / decline applies to the whole batch |

`door_task_id` is the most important key in the system. Every parcel under one
`door_task_id` transitions together — arrival, verification, completion, refusal, timeout.
**The grouping condition must include `task_type`**, otherwise a unit's delivery and
return would incorrectly merge into a single stop.

### Flow timestamps

| Column | Type | Notes |
|---|---|---|
| `door_assigned_at` | DATETIME | Door assigned (placement door opened); used by `check_assign_timeout` |
| `stop_dispatched_at` | DATETIME | When `/api/robot/dispatch` was actually called for this stop; prevents concurrent duplicate dispatch |
| `arrived_at` | DATETIME | Robot arrival; used by `check_pickup_timeout` |
| `returned_at` | DATETIME | Robot back at the operations room (door still closed at this point) |
| `return_door_opened_at` | DATETIME | Staff pressed open and the door actually opened; used by `check_return_timeout` |
| `door_closed_at` | DATETIME | Staff removed the parcel and closed the door |

### Case closure and redelivery

| Column | Type | Notes |
|---|---|---|
| `acknowledged_at` | DATETIME | Staff acknowledged a `voided` parcel |
| `case_closed_at` | DATETIME | Case closed |
| `return_retrieved_at` | DATETIME | Return item confirmed removed from the door |
| `redispatched_at` | DATETIME | Redelivery triggered from the exceptions page |
| `redispatched_to` | UUID | Points at the newly created parcel's `packages.id` |

### Notification and scheduling

| Column | Type | Notes |
|---|---|---|
| `pending_pickup_notified_at` | DATETIME | Return notice sent. **Once only** — starts the 72-hour void countdown |
| `scheduled_pickup_at` | DATETIME | Resident's scheduled slot, rounded to the hour or half hour. Placement and dispatch are blocked until then |
| `schedule_reminder_sent_at` | DATETIME | Whether the 2-hour-ahead reminder fired. **Once only** |

### Audit

| Column | Type | Notes |
|---|---|---|
| `created_at` | DATETIME | defaults `now_taipei()` |
| `updated_at` | DATETIME | maintained by `onupdate` |

---

## `line_binding`

| Column | Type | Constraint | Notes |
|---|---|---|---|
| `line_user_id` | VARCHAR(100) | **PK** | The LINE User ID is the primary key, not a surrogate |
| `unit` | VARCHAR(50) | NOT NULL | Must match the robot map's waypoint name exactly |
| `name` | VARCHAR(100) | NOT NULL | Resident name |
| `bound_at` | DATETIME | | |
| `status` | VARCHAR(20) | | `active` / `inactive`, defaults `active` |
| `solo_notify` | BOOLEAN | NOT NULL | defaults `True`. When `False`, every resident bound to the unit receives notifications |

---

## `package_recipients`

Which LINE users a given parcel notifies. The fan-out is determined by `solo_notify`.

| Column | Type | Constraint |
|---|---|---|
| `package_id` | UUID | **PK (composite)** |
| `line_user_id` | VARCHAR(100) | **PK (composite)** |
| `unit` | VARCHAR(50) | NOT NULL |

Composite primary key on `(package_id, line_user_id)` — one parcel may notify several users.

---

## `task_logs`

Backs the daily report. Before this table existed, events went to `print()` and vanished
on restart, making history unrecoverable.

| Column | Type | Constraint | Notes |
|---|---|---|---|
| `id` | UUID | PK | |
| `package_id` | UUID | **nullable** | Some events (e.g. robot connection failure) belong to no parcel |
| `event_type` | VARCHAR(50) | NOT NULL | 53 distinct values observed in production |
| `level` | VARCHAR(10) | NOT NULL | `info` / `warning` / `error`, defaults `info` |
| `detail` | VARCHAR(500) | | |
| `created_at` | DATETIME | | |

Full event inventory: [`event-types.md`](event-types.md). The comment block in
`models.py` lists roughly 45 and is out of date — see
[`known-issues.md`](known-issues.md#d-1-event_type-comment-list-out-of-date).

---

# flashbot-robot

## `doors`

| Column | Type | Constraint | Notes |
|---|---|---|---|
| `id` | INTEGER | PK | auto-increment |
| `sn` | VARCHAR(50) | NOT NULL | Robot serial number |
| `door_number` | VARCHAR(10) | NOT NULL | `H_01`–`H_04` |
| `status` | VARCHAR(20) | NOT NULL | defaults `empty` |
| `door_task_id` | VARCHAR(100) | | Task ID assigned by the LINE backend, **stored as a string** while the other side holds a UUID |
| `updated_at` | DATETIME | | `onupdate`, UTC |

**Constraints**
`UNIQUE (sn, door_number)` · `CHECK door_number IN ('H_01','H_02','H_03','H_04')` ·
`CHECK status IN ('empty','assigned','loading','full','picking','putting')`

| `status` | Meaning |
|---|---|
| `empty` | Unoccupied |
| `assigned` | Allocated, nothing loaded yet |
| `loading` | Staff loading, door open |
| `full` | Parcel loaded |
| `picking` | Resident collecting, door open |
| `putting` | Resident depositing, door open |

---

## `robot_state`

| Column | Type | Constraint | Notes |
|---|---|---|---|
| `id` | INTEGER | PK | auto-increment |
| `sn` | VARCHAR(50) | **UNIQUE**, NOT NULL | Robot serial number |
| `last_point` | VARCHAR(100) | | Last known waypoint, defaults to empty string |
| `current_task_id` | VARCHAR(100) | | Mission currently executing |
| `updated_at` | DATETIME | | `onupdate`, UTC |

`last_point` is what `poll_robot_returned` compares against `ROBOT_HOME_POINT_NAME` to decide whether the robot has returned. The entire reconciliation loop rests on this one column.

---

# Relationships

## Within a database

| Relationship | Cardinality | Via |
|---|---|---|
| `line_binding` → `packages` | 1..N | `line_user_id` |
| `packages` → `package_recipients` | 1..N | `package_id` |
| `line_binding` → `package_recipients` | 1..N | `line_user_id` |
| `packages` → `task_logs` | 0..N | `package_id` (nullable) |
| `packages` → `packages` | 0..1 | `redispatched_to` → `id` (self-referential) |
| `robot_state` → `doors` | 1..N | `sn` |

## Across databases

| Relationship | Cardinality | Via |
|---|---|---|
| `packages` → `doors` | 1..N | `door_task_id` |

**This is the most consequential aspect of the data model.** The two database instances are completely independent, and there is no mechanism at the database layer to enforce this relationship:

- The backend assigns a door and passes `door_task_id` (UUID) to the robot
- The robot stores it in `doors.door_task_id` (VARCHAR(100), a string)
- Neither side has a foreign key, a trigger, or a consistency check

If either side is manually edited, or if an API call fails and is not rolled back, they will diverge, **without triggering any errors**. This divergence only becomes apparent if the bot opens the wrong door or cannot open any doors at all.

This is the structural reason for the design decision described in
[`architecture.md`](architecture.md#design-principle-one-authority-for-state): due to the lack of guarantees at the database level, correctness is guaranteed by a single writer authority plus an idempotent coordination loop.

---

# Deliberately out of scope

- The eight `packages.status` values and their transition rules → [state machines](state-machines.md)
- Request and response shapes per endpoint → FastAPI's generated `/docs`
- Inter-module call relationships → [architecture](architecture.md)
