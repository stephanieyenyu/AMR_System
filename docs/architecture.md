# Architecture

The two services are deployed independently in different PostgreSQL databases and communicate via HTTP. I separated them because the hardware control failure characteristics and business logic differ, as do their deployment cadences; isolating them allows the robot layer to be restarted or replaced without affecting the package state. The cost of this is structural and determines much of what follows: two databases, and no database-level mechanism to keep them consistent.

![System architecture](images/architecture.png)

*Editable source: [`diagrams/Aurobox-Component v2.drawio`](diagrams/)*

## Communication properties

Traffic between services is primarily unidirectional. The `line-backend` calls the robot service; all 14 calls pass through the `call_robot_api()` function in `app/main.py`. This function handles timeouts, single retries, and logging failures to the `task_log`. The target address is configured by `ROBOT_API_BASE_URL`, which is referenced only once in the codebase.

The robot initiates connections along a path. Upon reaching a designated path point, the `_poll_notify_display_qr()` function in `src/aurobox/tasks.py` sends a message to `{CENTRAL_API_BASE_URL}/door-tasks/{door_task_id}/arrived`. Other parts of the robot service do not call the backend.

The backend obtains all other information through polling. Return detection, robot availability, battery level, and location information are all obtained from `GET/api/dashboard/status`, which queries every 20 seconds, and the `poll_robot_returned()` function queries every 20 seconds.

This asymmetry stems from considerations of delivery semantics, not implementation convenience. Callback functions carry at most one semantics: if a message is lost, neither party will be aware of the loss, and the package remains in an uncorrectable state. Polling provides an idempotent coordination channel where failed loops are reflected in the next loop and can be retried without inter-service coordination. Return detection is based on polling, and it is this choice that makes the recovery described in [`known-issues.md`](known-issues.md#a-1returned_at-timestamps-appear-inverted) possible at
all.

Hardware access is limited to a single module. Robot movement, gating, and screen content access the Pudu Open Platform REST API via `src/aurobox/pudu_client.py` and are authenticated by HMAC-SHA1 request signing. No other module holds Pudu credentials or constructs Pudu requests.

## Design principle: single-writer state authority

The bot service is responsible for reporting hardware events. It neither holds nor modifies the package business state. All state transitions are determined and persisted by the `line-backend`.

The initial implementation lacked this feature. Both services maintained parallel state machines, with the bot holding its own copy of the package state. Code review revealed a risk of split-brain: with two independent databases and no decentralized transactions, partitioning or retries would result in two distinct records from which to choose. This duplicate logic has been removed from the bot service.

The acceptable cost is that the bot cannot operate autonomously during partitions. I believe that the bot's stalled state is recoverable, while inconsistent package records are not.

## Known deviation

`line-backend/app/main.py` exposes `POST /packages/{package_id}/returned`, but there's currently no corresponding call point in the bot service. It appears to have existed before the migration to the `door_task_id` group, because return detection is now implemented via `poll_robot_returned()`. The route is almost certainly invalid.

## Related

- [State machines](state-machines.md) — delivery and return flows with every exception branch
- [Known issues](known-issues.md) — verified defects and open questions
- [Metrics](metrics.md) — measured behaviour of this architecture in deployment
