# Aurobox — Autonomous Parcel Delivery for Residential Buildings

An end-to-end delivery system built around a Pudu autonomous mobile robot (AMR).
Residents receive arrival notifications through LINE, schedule pickup, and open the
robot's cargo door by scanning a QR code. Building staff create parcels, monitor the
robot, and resolve exceptions through a web dashboard.

**This project contains no machine learning.** It is the orchestration and state-management
layer that a robot needs before any learned policy can run on top of it — the part that
handles what happens when the hardware does not respond, when a resident refuses a
package at the door, or when a delivery times out with no one home.

---

<!-- ══════════════════════════════════════════════════════════
     VIDEO: Upload the mp4 to any GitHub issue comment box
     (do NOT submit the comment). GitHub returns a
     user-images.githubusercontent.com URL. Paste it below.
     ══════════════════════════════════════════════════════════ -->

https://github.com/user-attachments/assets/REPLACE-WITH-YOUR-VIDEO-URL

*Full delivery cycle: parcel creation → loading → dispatch → arrival → QR scan → pickup → return to standby*

---

## Key Numbers

Measured over 21 active days of testing with a physical robot
(2026-07-14 to 2026-08-14). Every figure below is derived from the production
PostgreSQL database; the derivation is documented in [`docs/metrics.md`](docs/metrics.md).

| Measurement | Value | What it demonstrates |
|---|---|---|
| Logged events | **2,678** across **53 event types** | Every state transition is observable and reconstructable |
| Packages on the happy path | **43.9%** (72 of 164) | Exception branches outnumber the nominal flow — the long tail *is* the system |
| Robot API call failure rate | **12.47%** (334 of 2,678) | Concrete measure of real-hardware integration uncertainty |
| Trip batching | **106 parcels → 83 trips (−21.7%)** | Measured effect of the multi-parcel grouping design |
| Packages stuck in a non-terminal state | **0** | Achieved by automated compensation *and* operator intervention — see [Limitations](#known-limitations) |
| Codebase | 90 commits, 39 HTTP routes, 6 scheduled jobs | — |

Two caveats stated up front, because they affect how these numbers should be read:

- The 12.47% failure rate is an **upper bound**. A portion of `*_failed` events are
  false negatives — the robot call succeeded but the response handling or a connection
  timeout recorded it as a failure. The true failure rate is lower and was not isolated.
- "0 packages stuck" is **not** evidence of full autonomy. It is the combined result of
  a compensating scheduler and 42 manual task deletions by an operator. The distinction
  is unpacked in [Limitations](#known-limitations).

---

## The Problem

A delivery robot in a residential building spends most of its operational life in
states that are not "successfully delivering a package."

The resident is not home. The resident refuses the package at the door. The cargo
door reports full when the backend believes it is empty. The robot returns to standby
but the API call announcing this is lost. A parcel sits for 72 hours and must be voided.

In this deployment, **56.1% of all packages terminated on an exception branch**
(refused at door, pickup timeout, or resident declined). The nominal path was the
minority case. Designing for the happy path first and adding error handling afterward
would have produced a system that worked in demos and failed in the building.

The core engineering problem was therefore not delivery. It was **maintaining a single
consistent view of parcel state across two independently deployed services when the
link between them is unreliable.**

---

## Architecture

![System Architecture](docs/images/architecture.png)

Two services, deployed separately, each with its own PostgreSQL database,
communicating over HTTP:

| Service | Stack | Responsibility |
|---|---|---|
| `line-backend` | FastAPI · SQLAlchemy · PostgreSQL · APScheduler | Business logic, parcel state machine, LINE Messaging API, two LIFF apps, admin dashboard, scheduled automation |
| `flashbot-robot` | Flask · PostgreSQL | Pudu AMR hardware control, cargo door management, mission dispatch |

The split is deliberate. Hardware control has different failure characteristics and
a different deployment cadence than business logic. Keeping them separate meant the
robot layer could be restarted or replaced without touching parcel state.

It also created the central design constraint: **two databases, one truth.**

---

## Design Decisions

### 1. The LINE backend is the sole authority on parcel state

The robot service reports hardware events — arrived at destination, cargo door opened,
returned to standby. It does not hold or mutate business state. All state transitions
are decided and written by `line-backend`.

**Why.** With two independent databases, allowing both sides to write state means a
network partition or a retry produces a divergence that cannot be reconciled — neither
side can tell which version is correct. A single authority means every anomaly has one
place to look, and any compensating mechanism has an unambiguous target to repair.

The cost is that the robot service cannot act on its own during a partition. That
tradeoff was accepted: a stalled robot is recoverable, an inconsistent parcel record is not.

### 2. Trip grouping via a three-part key

Packages are grouped into a single robot mission by a `door_task_id` derived from
`line_user_id + unit + task_type`.

**Why.** The robot can carry several parcels and open several cargo doors in one trip.
Issuing one dispatch call per parcel would send it back and forth for deliveries to the
same destination.

**Measured effect.** 106 grouped parcels were delivered in 83 trips — 23 fewer robot
round-trips than per-parcel dispatch, a 21.7% reduction. 21 of the 83 trips carried
more than one parcel.

### 3. A single exit point for all robot API calls

Every HTTP call from `line-backend` to the robot service passes through one function,
`call_robot_api`.

**Why.** With a 12.47% observed failure rate, timeout handling, retry policy, and error
logging cannot be reimplemented at 14 different call sites. Funnelling them through one
function means every failure is guaranteed to produce a `task_log` entry, which is what
made the failure analysis in this README possible at all.

This was a refactor, not the original design. The first implementation called the robot
directly from each handler, and the inconsistent error handling that produced is what
motivated the change.

### 4. Compensating polling, plus an explicit manual path

When the robot returns to standby but the reporting call is lost, a scheduled job polls
`/api/dashboard/status`, detects the position change, and writes the missing state itself.

On 2026-07-20 this mechanism recovered 13 packages in a single pass after a sustained
connection failure to the robot service.

**But this covers exactly one failure mode.** When a dispatch or door-assignment call
fails, the package stops in a non-terminal state and does not recover on its own. For
those cases the dashboard provides single-step operator actions — release door, force
resolve, redispatch, delete task — each of which writes a `task_log` entry.

**Why this shape.** The initial goal was full automatic recovery. Implementation made
it clear that failure modes are not homogeneous: "robot returned but didn't say so" is
inferable from observable facts, while "dispatch call vanished" is not. The final
position was to automate what is inferable and give every remaining case an explicit,
single-step, auditable manual exit — rather than pursue an autonomy guarantee the system
could not honor.

### 5. Row-level locking with `nowait` for cargo door allocation

Concurrent package assignments contend for a finite set of cargo doors. Allocation uses
`with_for_update(nowait=True)`, failing immediately rather than waiting for the lock.

**Why.** Under contention, a caller that waits holds a connection and delays every
downstream request. Returning "no door available" immediately lets the caller retry or
surface the condition to the operator, and keeps the failure visible instead of latent.

---

## Results

### Terminal state distribution (164 packages)

| Outcome | Count | Share |
|---|---|---|
| Completed | 72 | 43.9% |
| Refused at door | 50 | 30.5% |
| Pickup timeout | 33 | 20.1% |
| Declined by resident | 9 | 5.5% |

Every branch above was exercised with the physical robot, not simulated. The high
exception share reflects deliberate coverage testing rather than operational failure.

### Failure distribution

| Error type | Count | Last occurrence |
|---|---|---|
| `assign_timeout_failed` | 116 | 2026-07-27 |
| `cancel_task_failed` | 51 | 2026-08-14 |
| `poll_returned_failed` | 46 | 2026-07-27 |
| `dispatch_failed` | 36 | 2026-07-27 |
| `robot_recall_failed` | 34 | 2026-08-07 |

Errors clustered on three days (2026-07-20, 07-24, 07-27: 41, 119, and 60 errors
respectively), almost entirely HTTP 404 responses from a robot service that was not
running. Daily error counts fell to 0–8 after August 1.

### Operator interventions

| Action | Count |
|---|---|
| Force close case | 82 |
| Delete stuck task | 42 |
| Manually release cargo door | 33 |
| Force resolve | 25 |
| Redispatch | 6 |

These counts are the honest denominator behind "0 packages stuck." They are reported
here rather than omitted because the ratio of automated to manual recovery is the
actual finding.

---

## Known Limitations

Documented in full in [`docs/known-issues.md`](docs/known-issues.md), classified by
whether each has been verified against source or database.

**Recovery is not fully automated.** Compensating polling handles one failure mode.
Dispatch-stage failures require an operator to delete the task. A principled fix would
return the package to `pending` and release the door after a retry ceiling, but this
was not implemented.

**Reported failure rate overstates true failures.** Some `*_failed` events record calls
that actually succeeded. The events were not instrumented finely enough to separate
genuine failures from reporting artifacts, so 12.47% should be read as a ceiling.

**Timestamp basis is not self-describing.** Both services strip timezone information
before persisting (`.replace(tzinfo=None)`), so a stored timestamp does not indicate
whether its basis is UTC or local time. No cross-service time comparison exists today,
so this causes no failure — but it cost significant time during one debugging session,
when timezone was the first suspect for a set of apparently inverted timestamps.
The actual cause was the compensating scheduler backfilling 13 records at once.

**Test distribution is heavily skewed.** One test unit accounts for 123 of 164 packages
(79%). Behavior under many concurrent distinct destinations is not characterized.

**Silent configuration degradation.** If the door-mapping environment variable fails to
parse, the robot service logs a warning and continues with a fallback mapping rather
than refusing to start. A misconfiguration would silently disable one cargo door.

---

## Why This Matters for Robotics Work

The events in this system form a complete, structured, timestamped record of a physical
robot operating in an uncontrolled environment: 2,678 entries, 53 distinct event types,
every state transition and every failure. That schema was designed for debugging, but it
is also the raw material for anomaly detection or imitation learning on failure recovery.

No learning component was built. The contribution here is the layer underneath one —
the part that has to be correct before a policy has anything reliable to act on, and
the part that determines whether a robot deployment survives contact with residents
who are not home, refuse packages, and press buttons in the wrong order.

---

## Repository Layout

```
.
├── line-backend/          FastAPI service — business logic, LINE, dashboard
│   ├── app/
│   │   ├── models.py      SQLAlchemy models, TaskLog event definitions
│   │   ├── routers/       39 HTTP routes
│   │   ├── scheduler/     6 APScheduler jobs
│   │   └── static/        Dashboard frontend
│   └── .env.example
├── flashbot-robot/        Flask service — Pudu AMR hardware control
│   ├── src/aurobox/
│   └── .env.example
└── docs/
    ├── architecture.md
    ├── known-issues.md
    ├── metrics.md
    └── images/
```

## Tech Stack

**Backend** Python · FastAPI · SQLAlchemy · PostgreSQL · APScheduler · Flask
**Messaging** LINE Messaging API v3 SDK · LIFF
**Robot** Pudu Open Platform API
**Infrastructure** Render · Docker

## Running Locally

```bash
cd line-backend
cp .env.example .env        # fill in your own credentials
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The robot service requires Pudu Open Platform credentials, which are not included.
`flashbot-robot` will start without them but cannot dispatch missions.

---

## Author

<!-- Fill in -->
**[Your Name]** — AI Development Dept. Intern, Aurotek

Built with one other intern over [dates]. I owned `line-backend` end to end —
the parcel state machine, LINE integration, admin dashboard, scheduled automation,
and the integration contract with the robot service. My teammate owned
`flashbot-robot`. The HTTP interface between them was designed jointly.
