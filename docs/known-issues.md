# Known Issues

Each entry below records whether it was verified against source code or against the test
database, and what remains unconfirmed. Entries are retained rather than
silently resolved: the classification itself — verified, unverified, accepted — is the
substance of the document.

**Snapshot** 2026-08-14 · `task_logs` 2,678 rows · `packages` 164 rows

| # | Issue | Class | Disposition |
|---|---|---|---|
| A-1 | `returned_at` timestamps appear inverted | Not a defect | No action |
| B-1 | Possible orphaned `package_recipients` rows | Unverified | Open |
| C-1 | Battery level renders as 0% | Defect | Fix recommended |
| C-2 | Cross-service timestamp basis differs invisibly | Defect | Fix recommended |
| C-3 | Authentication is uneven across the resident-facing surface | Security | Fix recommended |
| C-4 | Two endpoints overload one notification column | Defect | Fix recommended |
| C-5 | Silent fallback on malformed door mapping | Defect | Accepted, not fixed |
| C-6 | Dispatch failures require manual deletion | Design limitation | Accepted, not fixed |
| D-1 | `event_type` comment list out of date | Documentation | Fix recommended |
| D-2 | Same `event_type` logged at multiple levels | Documentation | Fix recommended |
| D-3 | `task_log` presentation layer incomplete | Documentation | Fix recommended |

---

## A. Investigated, not a defect

### A-1　`returned_at` timestamps appear inverted

**Symptom.** Some parcels carry a `returned_at` later than their `door_closed_at`. Thirteen
`returned` events share the identical timestamp `2026-07-20 15:58:52`.

**Cause.** The robot service's return API call failed with a connection error
(`return_failed`, `HTTPSConnectionPool` unreachable), so the parcels were never marked
returned. The `poll_robot_returned` scheduler subsequently detected via
`/api/dashboard/status` that the robot had reached the standby point and backfilled all
thirteen records in a single pass. Each `task_log` entry states this in its detail field.

**Assessment.** The reconciliation loop worked as designed. The apparent inversion is
a consequence of backfill, not of clock skew or data corruption.

**Consequence.** Any duration metric anchored on `returned_at` is invalid. No such metric
is quoted in this project.

---

## B. Unverified

### B-1　Possible orphaned `package_recipients` rows

**Observation.** `package_recipients` references 218 distinct `package_id` values while
`packages` holds 164 rows — a gap of 54.

**Hypothesis.** `delete_packages` cascades to `PackageRecipient`, so the excess is likely
residue from an earlier deletion path that did not cascade.

**Check.**
```sql
SELECT COUNT(DISTINCT pr.package_id)
FROM package_recipients pr
LEFT JOIN packages p ON p.id = pr.package_id
WHERE p.id IS NULL;
```

**Impact.** Inflates notification-count aggregates. No functional effect.

---

## C. Defects

### C-1　Battery level renders as 0%

**Location.** `flashbot-robot/src/aurobox/robot.py:65-67`

```python
battery_level = data_v2.get("battery_level")
if battery_level is None:
    battery_level = data_v1.get("battery_level", 0)
```

The v2 endpoint is tried first; on failure the code falls back to v1 with a default of `0`.

**Why the frontend guard does not catch it.**
`line-backend/app/static/js/dashboard.js:834`

```javascript
const battery = src?.battery ?? robot.battery ?? robot.battery_level ?? null;
```

The nullish coalescing chain is intended to surface "no data" as `null`, but `??` only
intercepts `null` and `undefined`. A `0` passes through as a valid reading. The robot
service's default of `0` defeats the frontend's guard precisely.

**Fix.** Change the default at `robot.py:67` to `None` so the frontend chain resolves as
intended and the UI can distinguish an empty battery from an unavailable reading.

**Severity.** Low — display only. Recorded because two half-guards on either side of a
service boundary cancel each other out, which is a failure mode worth naming.

---

### C-2　Cross-service timestamp basis differs invisibly

**Verified** against both model files and the project data dictionary.

| File | Line | Statement |
|---|---|---|
| `line-backend/app/models.py` | 14, 24 | `ZoneInfo("Asia/Taipei")` → `datetime.now(TAIPEI_TZ).replace(tzinfo=None)` |
| `flashbot-robot/src/aurobox/models.py` | 13 | `datetime.now(timezone.utc).replace(tzinfo=None)` |

**Symptom.** The two services persist naive datetimes on different bases — Taipei local
time and UTC respectively. Because both strip tzinfo before writing, a stored value gives
no indication of which basis produced it. Any comparison across the two databases is off
by eight hours, silently.

**Current exposure.** No business logic compares timestamps across services today, so
nothing fails. The cost so far has been debugging time: timezone was the leading
hypothesis when investigating A-1, and ruling it out took considerable effort. The actual
cause was scheduler backfill.

**One usage is correct and must not be changed.**
`flashbot-robot/src/aurobox/pudu_client.py:55` sends
`format_datetime(datetime.now(timezone.utc), usegmt=True)` to the Pudu Open Platform API,
which specifies UTC. Any normalisation effort must leave this call alone.

**Fix.** Migrate both sides to `TIMESTAMPTZ` and timezone-aware datetimes so the database
handles conversion. If schema change is unacceptable, document the basis at the top of
both `models.py` files and in each README.

**Severity.** Low today, high the moment any cross-service time calculation is added —
and the failure would be silent.

---

### C-3　Authentication is uneven across the resident-facing surface

**Specific endpoints are deliberately not named in this document.** The system is in
internal deployment; the findings are recorded here as an audit result, not as a
reproduction guide.

**Finding.** A review of every state-changing endpoint reachable by a resident found the
identity checks inconsistent across a single flow. One endpoint performs three
independent verifications — the scanned payload must match the task identifier, the LIFF
ID Token must verify against LINE, and the token subject must appear in that task's
recipient list. Another endpoint in the same flow, which also mutates parcel state,
performs none.

A second finding: one legacy callback accepts unauthenticated writes to a state column
and is no longer invoked by anything, having been superseded by backend polling. Its
docstring and the polling job's docstring contradict each other on which mechanism is
live.

**Why the gap exists.** Resident-facing endpoints hold no credentials, and the robot
service does not send any, so those routes were exempted from Basic Auth as a group.
The exemption was applied at the level of "who calls this" rather than "what does this
change," which is the wrong axis — a route that mutates state needs verification
regardless of who calls it.

**Fix direction.** Apply the same ID Token and recipient check to every resident-reachable
endpoint that changes state, not only the one that opens hardware. Remove the superseded
callback, or gate it behind a secret shared with the robot service.

**Severity.** Medium. No data is exposed. State can be altered by a caller who obtained an
identifier they were not intended to hold.

---

### C-4　Two endpoints overload one notification column

**Locations.** `POST /packages/{package_id}/notify-pending-pickup` and
`POST /packages/{package_id}/notify-completed-leftover`

**Symptom.** Both write `pending_pickup_notified_at`, with different semantics.
`notify-pending-pickup` fires once, guarded by a null check.
`notify-completed-leftover` overwrites on every call, restarting the 72-hour void
countdown each time.

**Impact.** Any report or countdown treating that column as a single source will
contradict itself. A parcel may appear to have been notified more recently than it was.

**Fix.** Give the second endpoint its own column, or add a discriminator recording which
notification type wrote the value.

**Severity.** Low. Affects reporting accuracy and countdown correctness.

---

### C-5　Silent fallback on malformed door mapping

> **Accepted, not fixed.** Deployment runs four-door mode exclusively; three-door mode is
> an optional configuration exercised rarely. Recorded for whoever enables it.

**Location.** `flashbot-robot/src/aurobox/config.py:25`

When the `DOOR_MAPPING` environment variable fails to parse, the service prints a warning
and continues with a fallback three-door mapping rather than refusing to start.

**Background.** Deployment uses `DOOR_MODE=4_DOORS`, where four logical doors map one-to-one
onto four physical doors and the mapping logic is bypassed entirely. `.env.example` ships
the correct four-door configuration. Three-door and four-door mappings are intentionally
different; that is by design.

**Risk.** A JSON syntax error introduced while editing environment variables would silently
disable one cargo door. Deployment succeeds, nothing raises, and the only signal is a
single `print` in the log.

**Fix, if adopted.** Raise on parse failure, or at minimum use `logger.error` rather than
`print` so the platform's error monitoring surfaces it. A configuration error should fail
the deploy, not quietly substitute different behaviour.

---

### C-6　Dispatch failures require manual task deletion

> **Accepted, not fixed.** Failure frequency dropped substantially in the final version,
> and an unquantified share of recorded failures are reporting artifacts rather than real
> failures. Retained as an operational note.

**Symptom.** When a dispatch or door-assignment call to the robot fails, the parcel halts
in a non-terminal state and blocks subsequent tasks. An operator must delete the task to
proceed.

**Recorded triggers.** `assign_timeout_failed` 116 · `door_assign_failed` 57
(44 warning + 13 error) · `dispatch_failed` 36

**These counts include false positives.** Some `*_failed` events record calls that
succeeded but whose response handling or connection timeout registered a failure. The
observed 12.47% error rate is therefore an upper bound on the true failure rate.

**Coverage of the existing mitigation.** `poll_robot_returned` handles exactly one failure
mode — robot returned, report lost — by inferring state from observable position. It
cannot address dispatch-stage failures, where no observable fact distinguishes "call lost"
from "call never made."

**Observed manual intervention.** `package_deleted` 42 · `door_released_manually` 33 ·
`force_resolved` 25 · `redispatched` 6

**Reading the "0 stuck" figure.** No parcel sits mid-flow at snapshot time, but this is the
combined result of automated compensation and manual cleanup. It is not evidence of
autonomous convergence and is not presented as such.

**Fix, if adopted.** After a retry ceiling, return the parcel to `pending` and release its
cargo door automatically rather than halting mid-flow.

---

## D. Documentation debt

### D-1　`event_type` comment list out of date

Event types present in the database but absent from the model's comment block:

```
trip_completed          trip_wait               queued
door_joined             manual_door_opened      manual_door_closed
schedule_reminder_sent  return_open_failed      show_qr_failed
```

**Impact.** Anyone implementing `task_log` display from the comment will miss these nine and
render undefined labels.

**Fix.** Regenerate from the database. Complete list in
[`event-types.md`](event-types.md).

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

**Impact.** Filtering by `level` alone under- or over-counts. This affects the 12.47% figure,
which counts `level = 'error'` only.

**Fix.** Normalise the level per event type, or filter by an explicit `event_type` allowlist.

---

### D-3　`task_log` presentation layer incomplete

- Most event types have no display label in the frontend
- Python `repr` strings leak into the UI when dicts or exception objects are logged whole
- Human-readable text and machine-readable payloads share one field

**Fix.** An event-to-label lookup table in `reports.js` addresses most of this. A full
refactor of the logging schema is disproportionate to the benefit.

---

## Excluded after review

Two items were drafted and removed once checked:

**Orphaned cargo doors after robot recall.** An operator opening and closing the cargo door
on the robot's return is the established operating procedure. The state in question is part
of the normal flow, not an inconsistency.

**Three-door mapping example contradicting its description.** Three-door and four-door
mappings are intentionally different. The documentation is correct.
