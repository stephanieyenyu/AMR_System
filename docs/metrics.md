# Metrics

All information referenced in the README file comes from the PostgreSQL database supporting the testing system. This system is used internally by Aurotek for testing physical robots. This document documents the source and derivation process of each piece of information to verify its authenticity, rather than simply accepting it outright.

**Snapshot date** 2026-08-14
**Observation window** 2026-07-14 to 2026-08-14 (21 days with activity)
**Raw export** [`data/packages.json`](../data/packages.json) — the `GET /admin/packages`
response for all 164 records at snapshot time

Unit identifiers in the data (`stephanie`, `jason`, `kevin`, `zola`, `joyce`) are test
account labels rather than resident records. `豆豆待機` and `高高待機` denote robot standby
waypoints.

---

## Sources

| Table | Rows | What it holds |
|---|---|---|
| `packages` | 164 | Current package records with 25 columns of state and timestamps |
| `task_logs` | 2,678 | Append-only event log; every state transition and every failure |
| `line_binding` | 2 | Resident-to-unit bindings active at snapshot time |
| `package_recipients` | 218 package IDs | Notification fan-out records |

---

## Scale

| Figure | Value | Derivation |
|---|---|---|
| Commits | 2026/07/29-08/31 | `git log --oneline \| wc -l` |
| Source files | 39 | `git ls-files "*.py" "*.js" "*.html" \| wc -l` |
| HTTP routes | 39 | Route inventory in `docs/api.md` |
| Outbound robot API calls | 14 | Call sites routed through `call_robot_api` |
| Scheduled jobs | 6 | APScheduler registrations |
| Event types | 53 | `SELECT COUNT(DISTINCT event_type) FROM task_logs` |

---

## Package outcomes

```sql
SELECT status, COUNT(*) FROM packages GROUP BY status;
```

| Status | Count | Share |
|---|---|---|
| `completed` | 72 | 43.9% |
| `rejected_at_door` | 50 | 30.5% |
| `returned_timeout` | 33 | 20.1% |
| `voided` | 9 | 5.5% |
| **Total** | **164** | |

All 164 records sit in a terminal state; none are mid-flow. See
[Interpretation](#interpretation) for what that does and does not mean.

By task type:

| | delivery | return |
|---|---|---|
| `completed` | 55 | 17 |
| `rejected_at_door` | 41 | 9 |
| `returned_timeout` | 32 | 1 |
| `voided` | 9 | 0 |
| **Total** | **137** | **27** |

### Current versus cumulative

`packages` holds 164 rows, but `task_logs` contains 230 `created` events and 42
`package_deleted` events. **164 is the count of surviving records, not the number ever
created.** Records were hard-deleted during testing. The README uses 164 and states this
distinction; any claim of "164 packages created" would be wrong.

---

## Trip batching

packages are grouped into robot missions by `door_task_id`.

| Figure | Value | Derivation |
|---|---|---|
| packages carrying a group key | 106 | `COUNT(*) WHERE door_task_id IS NOT NULL` |
| Distinct trips | 83 | `COUNT(DISTINCT door_task_id)` |
| Trips carrying more than one package | 21 | Groups with `HAVING COUNT(*) > 1` |
| Largest single trip | 3 packages | `MAX` group size |
| Dispatches avoided by grouping | 23 | 106 − 83 |

The 58 records without a group key break down as 9 `voided` packages (never assigned a
cargo door, expected) and 49 records predating the grouping mechanism.

**What this figure does not claim.** It confirms the grouping mechanism engaged; it is
not an efficiency result. Every trip in this window was run to exercise a function, so
the arrival pattern was set by what needed testing rather than by anything
representative. A different pattern would produce a different number.

**What grouping is actually for.** Two attributes, two levels. Delivery: The journey, not the package, becomes the distribution unit, so residents with multiple packages only need to visit once. Pickup identification: Because the QR code payload carries `door_task_id` instead of `package_id`, a single scan unlocks all doors under that key on the website. `task_type` carries crucial information – packages delivered and returned to the same resident are separated by design.

---

## Event log

```sql
SELECT event_type, level, COUNT(*) FROM task_logs GROUP BY event_type, level;
```

**Totals** 2,678 events · 53 distinct types · 284 distinct package IDs referenced

| Level | Count | Share |
|---|---|---|
| `info` | 2,298 | 85.8% |
| `error` | 334 | 12.5% |
| `warning` | 46 | 1.7% |

The 284 distinct package IDs exceed the 230 `created` events because a multi-package
batch logs `created` once against the primary package while each package in the batch
generates its own downstream events.

### Highest-frequency events

| Event | Count |
|---|---|
| `door_assigned` | 286 |
| `dispatched` | 248 |
| `created` | 230 |
| `arrived` | 190 |
| `pickup_requested` | 177 |
| `assign_timeout_failed` | 116 |
| `trip_completed` | 82 |
| `case_closed` | 82 |

Full list: [`docs/event-types.md`](event-types.md)

---

## Failures

```sql
SELECT event_type, COUNT(*), MAX(created_at)
FROM task_logs WHERE level = 'error'
GROUP BY event_type ORDER BY COUNT(*) DESC;
```

| Error type | Count | Last seen |
|---|---|---|
| `assign_timeout_failed` | 116 | 2026-07-27 |
| `cancel_task_failed` | 51 | 2026-08-14 |
| `poll_returned_failed` | 46 | 2026-07-27 |
| `dispatch_failed` | 36 | 2026-07-27 |
| `robot_recall_failed` | 34 | 2026-08-07 |
| `return_failed` | 14 | 2026-07-20 |
| `door_assign_failed` | 13 | 2026-08-07 |
| `pickup_open_failed` | 8 | 2026-08-13 |
| `complete_failed` | 8 | 2026-07-24 |
| `return_door_open_failed` | 4 | 2026-07-20 |
| `show_qr_failed` | 2 | 2026-07-14 |
| `return_open_failed` | 1 | 2026-07-14 |
| `notify_failed` | 1 | 2026-07-23 |
| **Total** | **334** | |

### The 12.47% figure is a ceiling

Some `*_failed` events are false alarms: the robot call succeeds, but the response processing or connection timeout is logged as a failure. The logging is not granular enough to distinguish these false alarms from genuine failures; therefore, the actual failure rate is lower than the reported failure rate, the exact extent of which needs to be quantified. This is explained in the README file.

Two further caveats:

- **`door_assign_failed` and `dispatch_failed` appear at two severity levels.**
  `door_assign_failed` logs 44 `warning` and 13 `error`; `dispatch_failed` logs 36
  `error` and 1 `warning`. Filtering by level alone therefore undercounts. The 334 total
  counts `level = 'error'` only.
- **Errors are heavily clustered.** Three days account for 220 of 334 errors, almost
  entirely HTTP 404 responses from a robot service that was not running.

### Daily distribution

| Date | Events | Errors | | Date | Events | Errors |
|---|---|---|---|---|---|---|
| 07-14 | 48 | 5 | | 07-30 | 4 | 0 |
| 07-15 | 161 | 20 | | 07-31 | 212 | 8 |
| 07-16 | 37 | 8 | | 08-03 | 41 | 0 |
| 07-17 | 78 | 8 | | 08-04 | 66 | 1 |
| 07-20 | 115 | 41 | | 08-06 | 13 | 0 |
| 07-21 | 220 | 17 | | 08-07 | 58 | 8 |
| 07-22 | 102 | 7 | | 08-10 | 46 | 0 |
| 07-23 | 168 | 9 | | 08-13 | 58 | 2 |
| 07-24 | 438 | 119 | | 08-14 | 43 | 1 |
| 07-27 | 386 | 60 | | | | |
| 07-28 | 233 | 16 | | | | |
| 07-29 | 151 | 4 | | | | |

---

## Operator interventions

```sql
SELECT event_type, COUNT(*) FROM task_logs
WHERE event_type IN (...) GROUP BY event_type;
```

| Action | Count |
|---|---|
| `case_closed` | 82 |
| `door_closed` | 70 |
| `return_door_opened` | 62 |
| `package_deleted` | 42 |
| `door_released_manually` | 33 |
| `robot_recall_requested` | 33 |
| `force_resolved` | 25 |
| `return_retrieved` | 15 |
| `voided_acknowledged` | 9 |
| `robot_recharge_requested` | 8 |
| `multi_package_assigned` | 7 |
| `redispatched` | 6 |

All 6 redispatched packages ended in `returned_timeout`, `rejected_at_door`, or `voided` —
none reached `completed`. These were deliberate exception-path tests, not production
delivery attempts.

---

## Interpretation

### "0 packages stuck" is a joint result

At the time of the snapshot, no packages were in a non-terminal state. This is the result of two mechanisms working together, not a single mechanism:

**Automated.** `poll_robot_returned` polls `/api/dashboard/status` every 20 seconds. When
the robot reports a location that becomes idle, but no `returned` event is received, the scheduler automatically writes the missing status. On 2026-07-20 at 15:58:52 it fixed 13 packages in one pass, after a connection failure. Every record it backfilled carries a `task_log` entry saying so.

**Manual.** A failed dispatch call leaves no trace in the robot's position, so nothing detects it and nothing recovers it. Those took operator action: 42 task deletions, 33 manual door releases, 25 force-resolves.

The README states this explicitly. Reporting "0 stuck" as evidence of autonomy would
misrepresent what the system does.

### Durations from timestamps do not work

`returned_at` is written after the fact by the reconciliation loop. For packages whose doors were opened by hand earlier, that puts `returned_at` later than `door_closed_at`, and any duration calculated from the pair comes out negative. **No timing or latency figure appears anywhere in this project.** The data does not support such metrics.

### The test set is lopsided

One unit received 123 of 164 packages (75%). Counting standby-point records too, the top account covers 79% of all activity. Nothing here shows how the system behaves with many different destinations arriving at once.

### Pending Issues

`package_recipients` points at 218 package IDs, but only 164 packages still exist. Deleting a package is supposed to delete its recipient rows with it, so the extra ones are probably left over from an older delete path. Not checked before the snapshot; recorded in
[`known-issues.md`](known-issues.md). This issue only affects notification count aggregation.
