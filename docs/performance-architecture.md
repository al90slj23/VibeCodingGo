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

## Exact projections and resource admission (0.29.0)

`CueCollectorScheduler` owns finite Python collector admission across controller instances. Numeric/history reads, small status probes, user input, long synchronization, network refreshes and batch control have separate finite lanes. A history query or sync cannot consume the reserved message/status slots. Closing an owner cancels its queued reads; active processes keep their existing receipt and cancellation rules. Saturation fails before execution, never fabricates a successful result. New collectors must declare their resource lane and bounded output. Admission does not cache results or merge/replay writes.

`CueWatcherProjectionReader` performs bounded, single-flight/latest-only native projections on a utility queue. Long conversation tails have their own exact JSON identity; changing token totals or model verification times does not repeat Unicode cleaning. Only a prepared immutable snapshot is committed on the main thread. Heartbeat, model and side-chat expiry are checked again at commit. Formatting instances are thread-local; values and rounding rules stay unchanged.

The usage ledger remains the source of truth. Disposable numeric SQL projections belong to its persistent identity and transactional mutation generation. INSERT, UPDATE and DELETE on request, provider/detail and archived-daily truth invalidate them. Reads obtain the generation and consuming page in one WAL snapshot. Schema/trigger changes retire the identity; corrupt, shared, absent or oversized cache state falls back to SQL. Cache writes use separate private files, not a write upgrade of the read transaction.

Only the timestamp upper bound may reuse a data-safe watermark from both request and archived records. Rolling lower bounds remain literal. Calendar range, prices, exchange rates, freshness flags and visible request pages still use current inputs. Task cost memoization retains original Decimal arithmetic and group order; it caches the pure numeric fold, not a current/partial/stale judgement. Cache size and parse work are bounded.

Do not add an age-only cache for authoritative totals or settings. A future module must identify its truth epoch, input scope, exact invalidation events and time boundaries, and compare new/corrected/deleted data against the uncached reference. A fast stale result is not an accepted optimization.

## Conditional overview transport (0.29.1)

The statistics collector offers opt-in `--overview-protocol` and `--overview-revision`. Default CLI callers retain the complete response. A revision is derived from the same WAL snapshot as the visible log page: ledger identity/generation, trigger eligibility, file identity, current data-safe upper watermark, filters, exact rolling lower bound, local calendar/timezone, hourly chart boundary, pricing/FX source contents and sync metadata. Only an exact match may omit charts, summaries and filter catalogs. A due-only pricing scheduler deadline is normalized to an already-due state; each response still carries the current unmodified money/scheduling metadata.

The native controller must hold that exact complete overview before merging a page response. It updates list/pager only on a proven match. Missing, malformed or mismatched bases trigger one full repair; repeated malformed responses stop the repair loop. A revision does not promise future freshness: writes after the read snapshot are seen by the next query. Rolling ranges deliberately refresh the overview when their exact lower bound moves. Deep arbitrary log pages retain the original OFFSET behavior; this change does not claim to remove that separate scaling cost.

Do not replace the SHA-256 revision with a TTL or accept a revision sent by an unrelated window without its complete base. New protocol fields are opt-in; unknown or absent proof falls back to a complete query.

## Task surfaces and future feature modules

`refreshMenu` owns one update of the visible menu list and one of the workbench list. A health-label refresh after it must pass `updateTasks: false`; opening the menu alone must not refresh the workbench. `updates_received` complements the row-write counters so duplicate no-op traversals are observable. Rebuilding a workspace page remains a separate lifecycle operation, particularly on a connection transition.

A new module uses the existing work scheduler and bounded collector admission, and declares input identity, engine version, resource class, timeout, output limit and cancellation. Browser/container/full-repository jobs require an isolated finite resource lane and must not occupy message/status capacity. No module gets a background timer merely because its tab exists. Retain a bounded raw report outside the UI, index its summary and render only the current page. The matching report/version and actual input changes define invalidation, not arbitrary cache age. Candidate modules and integration choices are documented in [Web Coding module research](web-coding-modules-2026-10-30.md).
