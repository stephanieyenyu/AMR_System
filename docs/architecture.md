# Architecture

Two services deploy independently against separate PostgreSQL databases and communicate
over HTTP. I separated them because hardware control exhibits different failure
characteristics and a different deployment cadence from business logic; isolating the two
permits restarting or replacing the robot layer without touching parcel state. The cost is
structural, and it defines most of what follows: two databases, and no database-level
mechanism to keep them agreed.

![System architecture](images/architecture.png)

*Editable source: [`diagrams/Aurobox-Component v2.drawio`](diagrams/)*

## Communication properties

Traffic between the services is predominantly one-directional. `line-backend` invokes the
robot service, and every one of those 14 calls passes through `call_robot_api()` in
`app/main.py`, which owns timeout, a single retry, and failure logging to `task_log`. The
target is configured by `ROBOT_API_BASE_URL`, referenced at exactly one site in the
codebase.

The robot initiates contact on one path. After reaching a resident waypoint,
`_poll_notify_display_qr()` in `src/aurobox/tasks.py` posts to
`{CENTRAL_API_BASE_URL}/door-tasks/{door_task_id}/arrived`. Nothing else in the robot
service calls the back end.

Everything else the back end obtains by polling. Return detection, robot availability,
battery level and position all come from `GET /api/dashboard/status`, which
`poll_robot_returned()` queries every 20 seconds.

The asymmetry is a delivery-semantics decision rather than an implementation convenience.
A callback carries at-most-once semantics: if the message is lost, neither side observes
the loss, and the parcel remains in a state nothing will correct. Polling supplies an
idempotent reconciliation channel, where a failed cycle becomes visible on the next one
and retries without coordination between the services. Return detection runs on a poll for
that reason, and the choice is what makes the recovery described in
[`known-issues.md`](known-issues.md#a-1returned_at-timestamps-appear-inverted) possible at
all.

Hardware access is confined to one module. Robot motion, door actuation and screen content
reach the Pudu Open Platform REST API through `src/aurobox/pudu_client.py`, authenticated
by HMAC-SHA1 request signing. No other module holds Pudu credentials or constructs a Pudu
request.

## Design principle: single-writer state authority

The robot service reports hardware events. It neither holds nor mutates parcel business
state. Every state transition is decided and persisted by `line-backend`.

The initial implementation did not have this property. Both services maintained parallel
state machines, with the robot holding its own copy of parcel status. Code review
identified this as a split-brain hazard: given two independent databases and no distributed
transaction, a partition or a retry produces two divergent records with no basis for
choosing between them. The duplicate logic was removed from the robot service.

The accepted cost is that the robot cannot act autonomously during a partition. I judged a
stalled robot recoverable in a way an inconsistent parcel record is not.

## Known deviation

`line-backend/app/main.py` exposes `POST /packages/{package_id}/returned`, and no call site
for it exists in the current robot service. It appears to predate the move to
`door_task_id` grouping, since return detection now runs through `poll_robot_returned()`.
The route is almost certainly dead; confirm before removal.

## Related

- [State machines](state-machines.md) — delivery and return flows with every exception branch
- [Known issues](known-issues.md) — verified defects and open questions
- [Metrics](metrics.md) — measured behaviour of this architecture in deployment
