# Native feature work and performance contracts

VibeCoding Go 0.28.2 gives recurring UI work one owner: `CueActivityScheduler` in `Sources/WorkScheduling.swift`. Future modules must follow this contract so adding features does not multiply polling and main-thread work.

## Scheduling and ownership

- Register a stable, feature-scoped key, cadence, visibility predicate and weak callback with `observe`. Retain its `CueActivityLease`; cancel it on close, deactivation or stop. Re-registering the same key replaces the old job. An old lease cannot cancel its replacement.
- The scheduler uses one adaptive common-mode one-shot timer. It caps callbacks and uses a cooperative 6 ms main-thread budget per pulse (it cannot preempt an already running callback), rotates deferred jobs by due time and drops catch-up bursts after sleep. Hidden work checks its predicate at a slower cadence and does not run its callback.
- Existing single-shot sync/price deadlines keep one owner and cancellation. Display-bound animation clocks may be local only while an effect is visible. Do not add an independent repeating timer for each new panel or background window.
- State reads, filesystem enumeration, JSON decode and diagnostic serialization belong on bounded utility work. `CueJSONSnapshotReader` coalesces requests and reuses an unchanged decoded snapshot. The inbox retains claimed events until delivery acknowledgement, including shutdown recovery.
- Reads/queries must retain a single in-flight owner and the newest pending request. Sends keep their request receipt and never auto-replay; reducing background work cannot cancel an in-flight send.

## Data and rendering

- Cache immutable identity rules and sort keys. Validate once at boundaries, not by constructing a Unicode set for every scalar in every refresh.
- Separate membership/order changes from telemetry and the clock. Rebuild task controls only when membership/order/layout changes; update only dirty visible rows. A second ticking does not rebuild menus, icons, Markdown or model controls.
- Hidden tabs, minimized windows and fully occluded conversations do not start polling/rendering. Late results may update their data cache; they do not trigger hidden layout. Reopening consumes the newest snapshot.
- Compare transcript signatures before Markdown/TextKit work. Reuse keyed WebKit content and coalesce native-to-WebKit payloads. Unchanged content cannot trigger a full DOM flush.
- Statistics refreshes reuse the same in-flight query, compare relevant data before table/heatmap rebuild, and do not launch a second full query after an unchanged FX response. Background sync progress uses `sync-status`.

## Regression gate and observability

`scripts/build.sh` automatically runs `scripts/check-ui-performance.py` and fails the build if a structural invariant regresses. To rerun it separately, use `python3 scripts/check-ui-performance.py`. It runs a fixed 12-row native fixture under a temporary home, not user history. It checks scheduler replacement/cancellation/visibility/budget behavior, inbox acknowledgement, unchanged JSON snapshot reuse, and zero list/document/row rebuild on unchanged updates. Time measurements are reported, not compared against a hardware-dependent absolute threshold.

Existing native checks additionally verify dirty-row-only updates, hidden-page zero rendering, unchanged Markdown zero parsing, idle composer zero TextKit layout, one WebKit flush in flight and statistics query merging. Every feature must add a corresponding invariant when introducing a new work path.

`app-status.json` contains `performance.scheduler`, task-list counters, conversation read/layout/flush counters and title-cache metrics. Scheduler callbacks over 16 ms are counted for investigation. These diagnostics coalesce on the main thread and serialize/write on a utility queue; they are not a new full-history scan. Raw timings and CPU/RSS samples remain in local `build/` evidence.

This contract is a scoped application architecture, not a system-wide process manager. It does not stop other projects, change macOS settings or attribute unrelated machine-level stalls to this app.
