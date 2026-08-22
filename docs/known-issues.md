# Known Issues

Each entry says whether it was checked against source code or against the test database,
and what is still unconfirmed. Nothing gets quietly deleted once resolved — the
classification is the point of the document.

**Snapshot** 2026-08-14 · `task_logs` 2,678 rows · `packages` 164 rows

| # | Issue | Class | Disposition |
|---|---|---|---|
| A-1 | `returned_at` timestamps appear inverted | Not a defect | No action |
| B-1 | Possible orphaned `package_recipients` rows | Unverified | Open |
| C-1 | Battery level renders as 0% | Defect | Fix recommended |
| C-2 | Cross-service timestamp basis differs invisibly | Defect | Fix recommended |
| C-3 | Authentication is uneven across the resident-facing surface | Security | Fix recommended |
| C-4 | Dispatch failures require manual deletion | Design limitation | Accepted, not fixed |
| D-1 | `event_type` comment list out of date | Documentation | Fix recommended |
| D-2 | Same `event_type` logged at multiple levels | Documentation | Fix recommended |
| D-3 | `task_log` presentation layer incomplete | Documentation | Fix recommended |

---

## A. Investigated, not a defect

### A-1　`returned_at` timestamps appear inverted

**Symptom.** Some packages carry a `returned_at` later than their `door_closed_at`.
Thirteen `returned` events share one timestamp: `2026-07-20 15:58:52`.

**Cause.** The robot service's return call died with a connection error
(`return_failed`, `HTTPSConnectionPool` unreachable), so those packages never got marked
returned. Later the `poll_robot_returned` scheduler saw via `/api/dashboard/status` that
the robot had reached its standby point, and backfilled all thirteen at once. Each
`task_log` entry says so in its detail field.

**Assessment.** The reconciliation loop did what it was built to do. Clocks are fine and
nothing is corrupted. The timestamps look inverted because they were written late.

**Consequence.** Any duration metric anchored on `returned_at` is garbage. None is quoted
in this project.

---

## B. Unverified

### B-1　Possible orphaned `package_recipients` rows

**Observation.** `package_recipients` references 218 distinct `package_id` values.
`packages` holds 164 rows. That leaves 54 unaccounted for.

**Hypothesis.** `delete_packages` cascades to `PackageRecipient`, so the extras are
probably residue from an older deletion path that skipped the cascade.

**Check.**
```sql
SELECT COUNT(DISTINCT pr.package_id)
FROM package_recipients pr
LEFT JOIN packages p ON p.id = pr.package_id
WHERE p.id IS NULL;
```

**Impact.** Inflates notification-count aggregates. Nothing functional breaks.

---

## C. Defects

### C-1　Battery level renders as 0%

**Location.** `flashbot-robot/src/aurobox/robot.py:65-67`

```python
battery_level = data_v2.get("battery_level")
if battery_level is None:
    battery_level = data_v1.get("battery_level", 0)
```

The v2 endpoint is tried first. When it comes back empty the code falls through to v1,
with `0` as the default.

**Why the frontend guard misses it.**
`line-backend/app/static/js/dashboard.js:834`

```javascript
const battery = src?.battery ?? robot.battery ?? robot.battery_level ?? null;
```

That chain is meant to surface "no reading" as `null`. But `??` only catches `null` and
`undefined`. A `0` is a perfectly good number as far as it is concerned, so it passes
straight through and the dashboard shows a flat battery.

**Fix.** Change the default at `robot.py:67` to `None`. The frontend chain then resolves
the way it was written to, and the UI can tell an empty battery apart from a missing
reading.

**Severity.** Low, display only. Worth writing down anyway: both sides had a guard, and
the two guards happened to cancel each other out.

---

### C-2　Cross-service timestamp basis differs invisibly

**Verified** against both model files and the project data dictionary.

| File | Line | Statement |
|---|---|---|
| `line-backend/app/models.py` | 14, 24 | `ZoneInfo("Asia/Taipei")` → `datetime.now(TAIPEI_TZ).replace(tzinfo=None)` |
| `flashbot-robot/src/aurobox/models.py` | 13 | `datetime.now(timezone.utc).replace(tzinfo=None)` |

**Symptom.** One service stores Taipei local time, the other stores UTC, and both strip
tzinfo on the way in. A stored value carries no clue about which one produced it. Compare
across the two databases and you are off by eight hours with nothing to warn you.

**Current exposure.** No business logic compares timestamps across services, so nothing is
broken right now. What it has cost so far is debugging time. Timezone was the obvious
suspect while investigating A-1 and it took a while to rule out. The real cause was
scheduler backfill.

**One usage is correct and must not be touched.**
`flashbot-robot/src/aurobox/pudu_client.py:55` sends
`format_datetime(datetime.now(timezone.utc), usegmt=True)` to the Pudu Open Platform API,
which specifies UTC. Leave this call alone during any normalisation work.

**Fix.** Move both sides to `TIMESTAMPTZ` and timezone-aware datetimes so the database
does the conversion. If a schema change is off the table, at least document the basis at
the top of both `models.py` files and in each README.

**Severity.** Low today. That changes the moment anyone writes code comparing times across
the two services, and when it breaks it will break quietly.

---

### C-3　Authentication is uneven across the resident-facing surface

**Specific endpoints are deliberately not named here.** The system is in internal
deployment. This is an audit result, not a reproduction guide.

**Finding.** A review of every state-changing endpoint a resident can reach found the
identity checks inconsistent inside a single flow. One endpoint runs three independent
verifications: the scanned payload has to match the task identifier, the LIFF ID Token has
to verify against LINE, and the token subject has to appear in that task's recipient list.
Another endpoint in the same flow mutates package state and runs none of them.

There is also a legacy callback that accepts unauthenticated writes to a state column.
Nothing calls it any more; backend polling replaced it. Its docstring and the polling
job's docstring disagree about which mechanism is live.

**Why the gap exists.** Resident-facing endpoints hold no credentials and the robot service
sends none, so those routes were exempted from Basic Auth as a block. The exemption keyed
on "who calls this" instead of "what does this change." Wrong axis. A route that mutates
state needs verification no matter who is calling it.

**Fix direction.** Apply the same ID Token and recipient check to every resident-reachable
endpoint that changes state, not just the one that opens hardware. Delete the superseded
callback, or put it behind a secret shared with the robot service.

**Severity.** Medium. No data is exposed. State can be altered by a caller who got hold of
an identifier they were never meant to have.

---

### C-4　Dispatch failures require manual task deletion

> **Accepted, not fixed.** Failure frequency dropped a lot in the final version, and some
> unknown share of the recorded failures are reporting artifacts rather than real
> failures. Kept as an operational note.

**Symptom.** A dispatch or door-assignment call to the robot fails, the package halts
mid-flow, and it blocks the tasks behind it. Someone has to delete the task by hand before
anything moves.

**Recorded triggers.** `assign_timeout_failed` 116 · `door_assign_failed` 57
(44 warning + 13 error) · `dispatch_failed` 36

**Those counts include false positives.** Some `*_failed` events record calls the robot
actually completed, where the response handling or a connection timeout logged a failure
anyway. So the observed 12.47% error rate is an upper bound, not the real rate.

**What the existing mitigation covers.** `poll_robot_returned` handles one failure mode and
one only: robot returned, report lost. It infers state from a position it can observe.
Dispatch-stage failures give it nothing to work with, because no observable fact separates
"call lost" from "call never made."

**Observed manual intervention.** `package_deleted` 42 · `door_released_manually` 33 ·
`force_resolved` 25 · `redispatched` 6

**Reading the "0 stuck" figure.** No package sits mid-flow at snapshot time, but automated
compensation and manual cleanup produced that together. It is not evidence the system
converges on its own, and it is not presented that way.

**Fix, if adopted.** After a retry ceiling, put the package back to `pending` and release its
cargo door automatically instead of leaving it halted.

---

## D. Documentation debt

### D-1　`event_type` comment list out of date

Event types in the database that the model's comment block never mentions:

```
trip_completed          trip_wait               queued
door_joined             manual_door_opened      manual_door_closed
schedule_reminder_sent  return_open_failed      show_qr_failed
```

**Impact.** Build a `task_log` display off that comment and you miss nine event types and
render undefined labels.

**Fix.** Regenerate from the database. Full list in [`event-types.md`](event-types.md).

```sql
SELECT DISTINCT event_type FROM task_logs ORDER BY event_type;
```

---

### D-2　Same `event_type` logged at multiple levels

| Event | Distribution |
|---|---|
| `door_assign_failed` | 44 warning / 13 error |
| `dispatch_failed` | 36 error / 1 warning |
| `user_unfollowed` | 5 info / 1 warning |

**Impact.** Filter on `level` alone and you under- or over-count. This hits the 12.47%
figure, which counts `level = 'error'` only.

**Fix.** Normalise the level per event type, or filter on an explicit `event_type`
allowlist.

---

### D-3　`task_log` presentation layer incomplete

- Most event types have no display label in the frontend
- Python `repr` strings leak into the UI wherever a dict or exception object gets logged
  whole
- Human-readable text and machine-readable payloads share one field

**Fix.** An event-to-label lookup table in `reports.js` handles most of it. Refactoring the
whole logging schema is not worth what it would cost.
