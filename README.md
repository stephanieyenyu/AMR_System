# Flashbot — Package State Management for an Autonomous Delivery Robot

A package delivery system built on a Pudu autonomous mobile robot, developed at Aurotek and
exercised against physical hardware across 21 days of recorded testing. Residents interact
through LINE — arrival notice, scheduled collection, and a QR scan on the robot's screen that
releases the cargo door. Building staff register packages, monitor the robot, and resolve
exceptions from a web dashboard.

Delivery is not the hard part; the robot does that, and this system calls the robot to do it.
Three things took the work. **Covering the branches that are not delivery**, because a package
can terminate seven different ways and only one of them is a resident collecting it on time.
**Holding one view of state across two databases**, because the two services deploy separately
and the link between them failed at 12.47% during testing. And **knowing which failures a
machine can repair**, because "the robot returned without saying so" is recoverable from an
observable fact and "the dispatch call was lost" is not.

What is here is a single-writer package state machine covering eight states across two flows, a
reconciliation loop that repairs the inferable class of failures from robot position, and an
explicit operator path for the class that cannot be inferred. All three are argued in Design.
**It contains no learned components.**

---

![Full delivery cycle](docs/images/demo.gif)

*Package creation → assign doors → loading → dispatch → arrival → QR scan → pickup → return to standby.*

[Full demo (6 min)](https://youtu.be/weCxq86P4LI) · [Short demo (50 sec)](https://youtu.be/zTG4C7RRjNw)

---

## What it does

**Treats every exception branch as a first-class flow.** Refusal at the door, pickup timeout,
decline before dispatch, emergency recall and 72-hour expiry each carry their own transitions,
their own timeout, and their own recovery path. None of them is an error handler bolted onto a
delivery routine.

**Groups packages by destination rather than dispatching one at a time.** Packages sharing a
recipient, a unit and a task type are assigned one `door_task_id` and travel as a single trip.
The robot stops once per destination, not once per package.

**Releases every package at a stop on one scan.** The QR code on the robot's screen encodes the
trip identifier, not the package. One scan, verified against the recipient set through a LIFF
ID token, opens every cargo door assigned under that trip.

**Repairs what it can infer and escalates the rest.** A scheduler polls robot position every 20
seconds and writes back the state that a lost report would have carried. Failures with no
observable signature go to a dashboard where an operator resolves them in one step, and every
such step writes to the event log.

**Notifies whoever the household chose.** A resident binding carries a `solo_notify` flag: on,
only the named recipient is notified; off, every resident bound to that unit is. The fan-out is
recorded per package rather than recomputed at send time.

**Runs a countdown on packages nobody collected.** A returned package that has been notified
enters a 72-hour window, after which it is voided. The notification is sent once and the
countdown starts from it.

---

## Scope

Two interns built this system inside a six-person team at Aurotek during July and August 2026.
I co-led the project and held sole ownership of `line-backend`; my teammate owned
`flashbot-robot`, the Flask service driving Pudu hardware. The HTTP interface between the two
was specified jointly.

| Component | Scope |
|---|---|
| **Package state machine** | Eight states across delivery and return flows, covering every exception branch: refusal at the door, pickup timeout, resident decline, robot recall, 72-hour void countdown |
| **LINE integration** | Messaging API v3 — webhook signature verification, Flex Messages, Rich Menu, push notification, 11 conversation entry points across text commands and postback actions |
| **LIFF applications** | Two embedded web apps: QR pickup, which validates the LIFF ID token against the recipient set before releasing every cargo door under that task, and return request submission |
| **Admin dashboard** | Four pages, 26 authenticated routes — package registration, live robot and cargo-door state, exception resolution, resident binding management, daily reporting with per-package event timelines |
| **Scheduled automation** | Six APScheduler jobs: pickup timeout, door-assignment timeout, return timeout, pickup reminder, return-state reconciliation, stuck-dispatch recovery |
| **Data model** | Four tables, the grouping-key scheme for multi-package trips, and an append-only event log spanning 53 event types |
| **Integration contract** | The HTTP interface between the two services, and all 14 outbound calls into the robot service |

39 source files · 39 HTTP routes · 14 outbound robot calls · 11 LINE entry points ·
6 scheduled jobs · 53 event types · 4 tables.

Figures count both services. Route and job counts come from `line-backend/app/main.py`; the 53
event types are the distinct values present in `task_logs` at snapshot, not the list in the
model comment, which was nine short and is recorded in
[`docs/known-issues.md`](docs/known-issues.md). The 14 outbound calls are
the call sites routed through `call_robot_api`, which is every one of them — the property is
checkable by grepping for `ROBOT_API_BASE_URL`, which appears once.

Development ran 14 July to 14 August 2026.

---

## Test Setup and Success Criterion

All testing was conducted in-house at Aurotek, using the physical Pudu robot, test packages, and
team members standing in for residents. This was integration testing rather than field
deployment; every figure below describes the implementation, not its performance in an occupied
building.

The objective was branch coverage, not throughput. A test package counts as successful when it
reaches the terminal state of its assigned branch and the robot returns to standby. A package
refused at the cargo door and carried back is a completed run, not a failure. Branch assignment
was therefore deliberate, and the resulting distribution bears no relation to how often each
outcome would occur in service.

One criterion is not verifiable from the data collected. Whether the LINE flow is legible to a
resident meeting it for the first time was never tested, because every resident in these runs
already knew how the system worked. That gap is stated here rather than in a footnote.

---

## Measurement Basis

All data comes from the PostgreSQL databases backing the test system. The observation window
runs 2026-07-14 to 2026-08-14, comprising 21 days with recorded activity. The sampling frame is
the complete `packages` table at snapshot time (164 rows) and the complete `task_logs` table
(2,678 rows across 53 event types). `data/packages.json` holds the raw package export; every
derivation appears in [`docs/metrics.md`](docs/metrics.md).

| Measurement | Value | Nature |
|---|---|---|
| Test packages processed | 164 | Complete `packages` table at snapshot |
| Packages routed down a non-nominal branch | 92 of 164 (56.1%) | Test design — deliberate branch coverage |
| Logged events | 2,678 across 53 types | Complete `task_logs` table |
| Robot API call failure rate | 334 of 2,678 events (12.47%) | Property of the test environment, upper bound |
| Packages remaining in a non-terminal state | 0 | Observed, confounded — see below |
| Reminder delivery timing | not measured | No table records whether a scheduled job fired inside its window |
| Time from arrival to collection | not measurable | `returned_at` is backfilled by the reconciliation loop — see A-1 |

Three qualifications govern how these should be read.

**The exception share is a test parameter, not an outcome rate.** 56.1% of packages ended on a
non-nominal branch because they were assigned that way. The figure carries no information about
how often a delivery would be refused or time out in service.

**The failure rate characterises the test environment, not the system.** It is also an upper
bound: a subset of `*_failed` events record calls the robot executed successfully but whose
response handling or connection timeout logged a failure, and the instrumentation does not
distinguish the two. I cannot produce a point estimate from this data, because the separation
was never instrumented. Separately, 220 of the 334 errors fall on three days when the robot
service was not running. The figure is reported because it sets the conditions the state layer
had to hold under, not as a system characteristic.

**Zero packages in a non-terminal state is a joint result, not evidence of autonomy.** It
follows from a reconciliation loop and 42 manual task deletions acting together. The ratio
between the two appears in full under [Evaluation](#evaluation).

**No duration metric appears anywhere in this project.** Time deltas anchored on `returned_at`
produce negative values, because the reconciliation loop writes that column after the fact —
later than the `door_closed_at` of packages whose doors an operator had already opened. The data
does not support a latency figure and none is quoted. Full account in
[`docs/known-issues.md`](docs/known-issues.md).

---

## Problem Statement

A delivery robot in a residential building can end a run in more ways than the nominal one.
The resident is absent. The resident refuses the package at the door. The cargo door reports
occupied while the back end records it empty. The robot reaches standby but the call announcing
arrival is lost in transit. A package sits untouched past its 72-hour window and must be voided.

Each of those outcomes is a separate branch with its own state transitions, its own timeout, and
its own recovery path, and there are more of them than there are ways for a delivery to proceed
nominally. An implementation that established the nominal path first and appended error handling
afterward would produce a system that demonstrates well and fails in service. I therefore built
the exception branches as first-class flows and verified each against physical hardware.

None of the failures between the services is loud. A robot call that times out returns an error
the caller logs and moves past; the package simply stops advancing, holding a cargo door, with
nothing in the interface indicating that it stopped rather than that it is still in progress.
The state that results is not corrupt — it is stale, which is harder to notice.

The problem I addressed is therefore state consistency: maintaining a single authoritative view
of package state across two services that deploy independently, own separate databases, and
communicate over a link that failed at a measured 12.47% during testing.

---

## System Architecture

![System architecture](docs/images/architecture.png)

| Service | Stack | Responsibility |
|---|---|---|
| `line-backend` | FastAPI · SQLAlchemy · PostgreSQL · APScheduler | Business logic, package state machine, LINE Messaging API, two LIFF apps, admin dashboard, scheduled automation |
| `flashbot-robot` | Flask · PostgreSQL | Pudu AMR hardware control, cargo door management, mission dispatch |

Two services, two processes, two databases. The split is deliberate: hardware control fails
differently from business logic and ships on a different cadence, and isolating them means the
robot layer can be restarted or replaced without touching a package record.

The cost is structural and is not hidden. There is no distributed transaction and no
database-level constraint spanning the two instances — the only thing linking a package to a
cargo door is a `door_task_id` held as a UUID on one side and a string on the other. If either
side is edited manually, or an API call fails without rollback, the two diverge with nothing
raised anywhere. The divergence surfaces when the robot opens the wrong door.

Communication is deliberately asymmetric. `line-backend` invokes the robot service through a
single function. The robot initiates contact on exactly one path — arrival at a resident
waypoint. All other state, return detection included, the back end obtains by polling.

That asymmetry is a delivery-semantics choice. A callback carries at-most-once semantics: if the
message is lost, neither side observes the loss, and the package sits in a state nothing will
correct. Polling supplies an idempotent reconciliation channel — a failed cycle is visible on the
next one and retries without coordination between the services. Return detection runs on a poll
for that reason, and the decision is what makes the recovery below possible at all.

---

## Design

### One writer, because two databases cannot agree without one

The robot service reports hardware events — arrival, door actuation, return to standby. It
neither holds nor mutates package status. Every state transition is decided and persisted by
`line-backend`.

The first implementation did not work that way. Both services maintained parallel state
machines, with the robot holding its own copy of package status. Code review identified it as a
split-brain hazard: with two independent databases and no distributed transaction, letting both
sides write means a partition or a retry produces two divergent records and no basis for
choosing between them. The duplicate logic came out of the robot service.

The accepted cost is that the robot cannot act autonomously during a partition. I judged a
stalled robot recoverable in a way an inconsistent package record is not.

### One key, two problems, at two different layers

Packages bound for the same stop share a `door_task_id` derived from
`line_user_id + unit + task_type`. All packages under one key transition as a unit — arrival,
verification, completion, refusal, timeout.

**Dispatch.** The robot carries several packages and actuates several doors on one trip, so
per-package dispatch returns it to the same unit once per package. Grouping makes the trip rather
than the package the unit of dispatch. Under the test arrival pattern the mechanism engaged on
106 packages dispatched as 83 trips, with 21 trips carrying more than one package. That figure
confirms grouping fired; it is not an efficiency result, because the arrival pattern was set by
what needed testing rather than by anything representative.

**Pickup identity.** The QR code on the robot's screen originally encoded `package_id`, so a
resident with three packages waiting scanned three times to open three doors in sequence.
Changing the payload to `door_task_id` made one scan release every package under that key at that
stop. This is independent of the dispatch change: grouping determines how many times the robot
stops, the QR payload determines how many times the resident scans, and either could have been
changed without the other.

`task_type` is load-bearing in the key, and deliberately so. Omitting it would merge a delivery
and a return addressed to the same resident into one stop and one scan. Those are distinct
interactions with distinct transition sequences — one releases packages to the resident, the
other accepts packages back — and collapsing them would couple two flows that differ and leave
the resident with no boundary between what they were collecting and what they were returning.
Two scans across a delivery and a return is the intended semantics, not a missed consolidation.

### Fourteen call sites, one exit

All 14 outbound HTTP calls to the robot service route through `call_robot_api`, which owns
timeout, a single retry, and failure logging.

The first implementation invoked the robot directly from each handler, and error handling
diverged between call sites. At the failure rates seen in testing, reimplementing timeout policy,
retry behaviour and failure logging at 14 sites makes divergence a certainty rather than a risk.

Consolidating them guarantees that every failure produces a `task_log` entry, which is the only
reason the failure analysis in this README exists. The accepted cost is uniformity: a status
refresh and a door-open command carry the same timeout and the same single retry, even though one
is a poll nobody is waiting on and the other has a resident standing in front of the robot.

### Reconciliation bounded by observability

When the robot reaches standby and the reporting call is lost, a scheduled job polls
`/api/dashboard/status`, detects the position change, and writes the missing state. On 2026-07-20
this mechanism repaired 13 packages in a single pass after a sustained connection failure.

It covers one failure mode. Exactly one. A dispatch or door-assignment call that fails leaves the
package in a non-terminal state from which nothing recovers it, and for those the dashboard
exposes single-step operator actions — release door, force resolve, redispatch, delete task —
each writing to the event log.

The design objective was full automatic recovery. Implementation established that failure modes
are not homogeneous with respect to observability. "The robot returned without reporting" is
inferable, because robot position is an observable state variable. "The dispatch call was lost"
is not, because no observable variable distinguishes a lost call from a call never issued. The
resulting position was to automate the inferable class and give the remainder an explicit,
auditable manual path, rather than assert a recovery guarantee the system could not satisfy.

The accepted cost is operator burden, and it is quantified rather than assumed: 42 task deletions,
33 manual door releases and 25 force-resolves over the test window.

### Refusing immediately beats waiting politely

Cargo doors are a bounded resource under concurrent contention. Allocation acquires a row lock
with `with_for_update(nowait=True)` and aborts immediately rather than queueing.

A waiting caller holds a database connection and introduces head-of-line blocking for every
request behind it. Immediate refusal lets the caller retry or surface the condition to an
operator, and keeps the failure observable rather than latent.

The accepted cost is a false negative under momentary contention: a request that would have
succeeded after a two-hundred-millisecond wait is refused instead. At four doors and one robot,
that trade is not close.

---

## Evaluation

### Terminal state distribution

All four outcomes below are completed runs. They differ in which branch the package took, not in
whether the system handled it: in each case the package reached a terminal state, the cargo door
was released, and the robot returned to standby.

| Terminal state | Count | Share |
|---|---|---|
| Collected by resident | 72 | 43.9% |
| Refused at door | 50 | 30.5% |
| Pickup timeout, returned | 33 | 20.1% |
| Declined by resident | 9 | 5.5% |

The distribution reflects deliberate assignment of exception branches for coverage. It is not an
operational outcome rate and should not be read as one.

### Failure distribution

| Error type | Count | Last occurrence |
|---|---|---|
| `assign_timeout_failed` | 116 | 2026-07-27 |
| `cancel_task_failed` | 51 | 2026-08-14 |
| `poll_returned_failed` | 46 | 2026-07-27 |
| `dispatch_failed` | 36 | 2026-07-27 |
| `robot_recall_failed` | 34 | 2026-08-07 |

Failures are temporally clustered rather than uniformly distributed. Three days — 2026-07-20,
07-24 and 07-27 — account for 220 of 334 errors, predominantly HTTP 404 responses from a robot
service that was not running. Daily error counts fall to between 0 and 8 after 1 August.

One entry does not fit that pattern. `cancel_task_failed` last fired on 2026-08-14, the final day
of the window, while every other error type stopped in July. Whether that is a live defect or a
reporting artifact was not determined before the window closed, and it is the first thing anyone
picking this up should check.

### Operator intervention

| Action | Count |
|---|---|
| Force close case | 82 |
| Delete stuck task | 42 |
| Manually release cargo door | 33 |
| Force resolve | 25 |
| Redispatch | 6 |

This distribution is the denominator behind the zero-stuck-package figure. The ratio of automated
repair to manual intervention is reported rather than omitted because it constitutes the
substantive result.

All six redispatched packages ended on `returned_timeout`, `rejected_at_door` or `voided` — none
reached collection. Those were exception-path tests rather than delivery attempts, which is what
the number means and also what it fails to tell you: the redispatch path has never been observed
completing.

---

## Threats to Validity

[`docs/known-issues.md`](docs/known-issues.md) holds the complete set, each classified by whether
it was verified against source or against the database.

**External validity — this is in-house testing, not field deployment.** Every run was conducted
at Aurotek with test packages and team members standing in for residents. Nothing here
characterises behaviour with real occupants: not the timeout parameters, not the refusal
handling, not the legibility of the LINE flow to someone encountering it for the first time. The
package distribution is also severely skewed, with a single unit accounting for 123 of 164
packages, so behaviour under many concurrent distinct destinations is uncharacterised.

**Construct validity — the failure rate measures logging, not failure.** A subset of `*_failed`
events record calls the robot executed successfully but whose response handling or connection
timeout registered failure. The instrumentation cannot separate the two classes, so 12.47% bounds
the true rate from above by an unquantified margin.

**Internal validity — the zero-stuck result confounds two mechanisms.** Reconciliation and manual
operator intervention both contribute, and the design does not isolate their individual
contributions. The reported intervention counts are the closest available decomposition.

**Instrumentation — the scheduler was never instrumented.** Six jobs mutate package state on
timers, and no table records whether any of them fired inside its intended window. Their
correctness is a code-level argument from the timeout constants, not a measured result. Stored
timestamps compound this: both services strip timezone information before persisting
(`.replace(tzinfo=None)`), one storing UTC and one storing local time, so a stored value gives no
indication of its own basis.

**Configuration — degradation is silent.** A door-mapping environment variable that fails to
parse produces a warning and a fallback mapping rather than a startup failure. A malformed value
would disable one cargo door with nothing raised, and the only signal is a single `print` in a log
nobody reads.

---

## Open Problems

The first concerns the boundary between recoverable and unrecoverable failure. The reconciliation
loop repairs a lost return report because robot position is an observable state variable; it
cannot repair a lost dispatch call because no observable variable distinguishes that case from a
call never issued. I established that boundary case by case, by inspection. Whether it admits
systematic characterisation — what is recoverable under partial observability across an unreliable
service boundary — is the problem I would most want to pursue.

The second concerns predictability. The event log holds 2,678 timestamped entries across 53 types,
recording every state transition, every failure, and every operator intervention that followed.
Failures are temporally clustered, with three days accounting for two thirds of them. Whether that
record carries sufficient signal to anticipate failure, or to learn a recovery policy from the
interventions operators selected, is a question the data could plausibly support. It is also a
question the data would answer badly: the interventions were chosen by the person who wrote the
rules that made them necessary, and none of them was randomised.

The third came out of the instrumentation itself. The failure rate this system can compute
conflates a genuine robot failure with a timeout this client imposed and with a response the
handler mishandled, because the logging records that a call did not succeed without recording
why. Separating a failure from the artifact of its recording is the measurement problem I want to
learn to address properly, and it is the one that would have to be solved before either of the
first two questions could be answered from data like this.

**The hardware shaped what was buildable.** One robot, four cargo doors, one building. Grouping
packages into trips matters because the robot is the bottleneck; fail-fast door allocation matters
because there are four doors and not forty. A different hardware envelope would have produced
different answers to both, and neither result should be read as general.

These are the questions I want to work on. What is here is the layer underneath a learned policy:
the component that must be correct before a policy has dependable state to act on, and the
component that determines whether a robot deployment survives contact with recipients who are
absent, refuse packages, and actuate controls out of order.

---

## Repository Layout

```
.
├── line-backend/                   FastAPI service — business logic, LINE, dashboard
│   ├── app/
│   │   ├── main.py                 39 HTTP routes, 6 APScheduler jobs, call_robot_api
│   │   ├── models.py               4 SQLAlchemy models, TaskLog event definitions
│   │   ├── line_messaging.py       Push and reply, 8 message builders
│   │   ├── line_verify.py          LIFF ID token verification
│   │   ├── config.py               Environment binding
│   │   ├── db.py                   Session factory
│   │   └── static/                 Dashboard front end — 5 JS modules, 2 stylesheets
│   ├── templates/                  4 pages plus shared base and nav
│   └── .env.example
├── flashbot-robot/                 Flask service — Pudu AMR hardware control
│   ├── src/aurobox/
│   │   ├── pudu_client.py          Pudu Open Platform, HMAC-SHA1 request signing
│   │   ├── robot.py                Motion, door actuation, status merge
│   │   ├── tasks.py                Background polling, the one callback into line-backend
│   │   └── config.py               Door mapping and environment binding
│   └── .env.example
├── data/
│   └── packages.json               Raw package export backing docs/metrics.md
├── render.yaml                     Deployment blueprint for both services
└── docs/
    ├── architecture.md             Service topology and communication properties
    ├── state-machines.md           Delivery, return, and robot mission flows
    ├── database.md                 Data dictionary for all six tables
    ├── api.md                      Route inventory grouped by caller
    ├── metrics.md                  Derivation of every figure in this README
    ├── known-issues.md             Verified defects and open questions
    ├── event-types.md              All 53 event types with their admin-facing labels
    ├── diagrams/                   Editable draw.io sources
    └── images/
```

## Tech Stack

**Back end** Python · FastAPI · SQLAlchemy · PostgreSQL · APScheduler · Flask
**Messaging** LINE Messaging API v3 SDK · LIFF · Flex Messages · Rich Menu
**Robot** Pudu Open Platform API
**Infrastructure** Render · Docker

## Running Locally

```bash
cd line-backend
cp .env.example .env        # supply your own credentials
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Requires a LINE Messaging API channel, two LIFF applications, and a PostgreSQL instance. The
robot service needs Pudu Open Platform credentials, which are not included; `flashbot-robot`
starts without them but cannot dispatch a mission.

The two services want separate databases. Pointing both at one will appear to work — the tables
do not collide — but it removes the constraint the whole state design exists to handle, and any
conclusion drawn from that configuration will not transfer.

`DOOR_MODE` defaults to `4_DOORS`. A malformed `DOOR_MAPPING` does not stop startup: the robot
service logs a warning and falls back to a three-door mapping, which silently removes one door.

---

## Author

**Stephanie Lin, Yen Yu**
AI Development Dept. Intern, Aurotek · July – August 2026 · Taipei

Built and exercised against physical hardware in-house.
Commit history was squashed before publication to remove operational data and internal identifiers.
//Shared for portfolio purposes with permission from Aurotek. Not licensed for reuse.//
