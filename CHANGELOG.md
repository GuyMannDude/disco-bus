# Changelog

## v0.14 — A status light is not a letter

- **Problem:** automated heartbeats (discord-liveness's daily ALIVE line to
  Opie) sat permanently unread: only the recipient can mark mail read, and the
  recipient is *right* to never open furniture. The result was a stuck nonzero
  eye badge that grew by one per heartbeat per day and meant nothing — Guy
  spent a morning trying to clear #3103 and could not, because no one could
  (snag heartbeat-pings-pollute-unread, 2026-08-31).
- **Fix:** optional envelope field `class: "status"` on `/mesh/ping`
  (mesh_version stays 0.5, additive). A status envelope is born swept:
  `cleared_at` = `created_at`, `cleared_reason` = `status-ping` at insert, so
  it never appears in any unread view (inbox `filter=unread`, IRIS badge)
  while `read_at` stays NULL — nobody opened it and the row still says so.
  Delivery, search, and `filter=all` are untouched; the latest heartbeat
  remains readable on demand. Unknown class values are rejected 400, not
  ignored — a typo'd class silently becoming ordinary mail would re-create
  the stuck badge. Sender updated: discord-liveness sends `class: "status"`
  on ALIVE only; BROKEN/BLIND/CRASHED remain real mail. Tests:
  `dispatcher/test_status_ping.py` (born-swept, ordinary-mail control,
  unknown-class rejection).
- **Post-review hardening (same day, code-review findings 1–5):** criticals
  can never be furniture — `class=status` on an envelope with
  `body.tier=critical` or `:critical:` in the subject is rejected 400, the
  same carve-out bulk_mark_read and the archive sweep already enforce on
  their own exits from the unread view. The immediate-delivery envelope now
  carries `cleared_at`/`cleared_reason` like the flush path already did, so a
  listener can tell furniture from mail regardless of whether the sender was
  paused. The archive sweep counts only genuinely-pending rows as never-read
  backlog and stamps swept-not-opened rows `aged-out-swept` (bulk-drained
  rows included) — born-swept furniture no longer pollutes the metric the
  sweep exists to expose. `schema/envelope-v0.5.json` updated: `class` plus
  the v0.12/v0.13 fields it had silently drifted from
  (`cleared_at`/`cleared_reason`/`archived_at`/`archive_reason`). Tests
  extended: swept-not-vanished (`filter=all` + search), HELD-through-flush,
  critical rejection. Deliberate scope: the MCP `ping` tool does not expose
  `class` — an agent's own mail is never furniture; raw-HTTP machine senders
  only. Discord mirror still mirrors status pings (the badge was the
  complaint, not the mirror).

## v0.13 — The pile is bounded, and nothing left the table

- **Problem:** v0.12 fixed the read COUNTER, not the VOLUME. Opie's unread went
  to zero while his inbox still held 1,212 envelopes and `filter=all` returned
  every one of them (Opie #2875). His words: "if another critical goes unread it
  will now be buried in 1,212 instead of 488." Proof it is not hypothetical —
  #409, `sec-watch:critical: PyPI package 'fastapi' compromised`, sent
  2026-05-26, was still unread at the time of this change.
- **Fix:** two additive columns, `archived_at` + `archive_reason` (migration in
  `init_db`; mesh_version stays 0.5). Aged-out mail is FLAGGED, never moved to a
  second database and never deleted, so "working + archived == the table" is a
  schema invariant instead of a count somebody re-verifies after a risky move —
  and the whole sweep reverses with one UPDATE. `POST /mesh/archive` runs the
  rule, **dry-run unless `apply=true`**, and returns `conservation_ok` with the
  result. Inbox excludes archived rows from all three filters and ships
  `X-Inbox-Archived-Withheld` so a shrunken view says what it is not showing.
  New `GET /mesh/search` (id / from / to / date range / substring, scope
  working|archived|all) and a matching `search` MCP tool: retention without
  retrieval is deletion with extra steps. `ping_read` on an archived envelope
  leads with an `archive_note` naming where it now lives and whether it was ever
  opened. Daily sweep via `~/scripts/bus-archive-sweep.sh` under cronalarm.
- **The retention rule, and why it is not a flat 30 days.** Opie proposed 30 days
  and invited an argument from the traffic. The traffic says an age line bounds
  the AGE of the pile, never its SIZE: fleet volume tripled between W18-22
  (~105/wk) and W30-33 (~325/wk), and at 30 days Opie still held 566 envelopes —
  a number that grows with traffic forever. So the primary bound is a
  per-recipient COUNT cap (200), with a 7-day floor so a busy day never archives
  mail nobody has had time to read, and the 30-day line kept as the age ceiling.
  Ceiling per agent: cap + criticals + the last 7 days. Opie 1,212 -> 203.
- **Criticals are matched on the tier, never the word.** `{"tier":"critical"}` or
  a structural `<watcher>:critical:` subject. A substring match on "critical"
  instead catches 31 hot/quiet digests whose headlines mention a critical CVE
  ("sec-watch-feeds:hot: Critical Windows zero-days") and exempts them forever —
  a permanent pile wearing the carve-out's name.
- **Read and never-read are archived alike, and stay distinguishable.** A
  four-month-old unread envelope is not going to be opened. But 595 of the 2,134
  swept had never been read, and `archive_reason` records `aged-out-unread`
  separately from `aged-out` — collapsing the two would hide exactly the backlog
  the sweep exists to expose.
- **Substring search, not FTS5.** An FTS index is a second copy of the corpus
  that must be kept in sync, and an index silently out of step with its table
  reports "no such message" for a message that is right there. A LIKE scan over
  2,871 rows is sub-millisecond; revisit at six figures.

## v0.12 — Swept is not read

- **Problem:** Opie's unread stood at 488 and near-all of it was stale — daily
  green digests plus months of authored ship-notices consumed via subject
  listings but never opened (Opie #2835). v0.10's sweep could not drain it
  honestly: it stamps `read_at`, which claims the recipient opened mail nobody
  read, and it refuses authored subjects entirely. Opie declined to run it for
  exactly that reason: the read flag is the only evidence trail of what was
  actually seen.
- **Fix:** two new columns, `cleared_at` + `cleared_reason` (migration in
  `init_db`, additive; mesh_version stays 0.5). `bulk_mark_read.py` now stamps
  those and never touches `read_at` — the audit record says "swept, unread".
  The dispatcher's `filter=unread` excludes cleared rows; envelopes carry both
  new fields. New `--include-authored` flag sweeps ALL unread before the date
  for a deliberate backlog drain; `:critical:` subjects are still never swept,
  and the family-anchored mode stays the default. `mnemo-wedge-watch-` joined
  the families. Companion change outside this repo: wedge-watch and
  cronalarm-report green days now append to `~/.sparks/green-digest.jsonl`
  instead of addressing Opie at all — only non-clean results get a reader.

## v0.11 — The eye now sees the mail that died

- **Problem:** a delivery the dispatcher could not complete went `state=FAILED`
  in the DB and told nobody — the sender's 202 "SENT" is emitted before the
  background delivery thread runs, IRIS counted only unread DELIVERED mail, and
  no cron watched the column. 35 historical FAILED rows, every one silent.
  Found by Guy asking the operator question: "How will I know if delivery
  fails?" (S239, snag-bus-failed-delivery-invisible).
- **Fix:** read-only `GET /mesh/failed?since_id=N&limit=M` — FAILED rows with
  id > N, oldest first. Consumer is the IRIS poller: red FAILED badge +
  `from→to #id` panel rows, persistent until `iris-ctl ack-failed` moves the
  watermark (`~/.sparks/iris/failed-ack.json`). Historical 35 acked at deploy;
  live-fired with a deliberate ping to down Cliff (#2725): FAILED → badge →
  ack → clear, all verified. mesh_version stays 0.5 — wire contract unchanged,
  the endpoint is additive.

## v0.10 — Clearing the digests, without burying the one letter that mattered

- **Problem:** v0.9 made the unread count honest and the honest number was 710
  for Opie, 118 for Rocky. Roughly a third of it is machine-generated digests —
  sec-watch, brain-drift, liveness probes, dream reports — which nobody reads
  individually and which had made the count itself meaningless. Clearing them by
  hand is not work anyone will do.
- **Fix:** `dispatcher/bulk_mark_read.py`. Dry run by default; `--apply` writes.
  A message is marked read only if its subject **starts with** a pattern in an
  explicit `FAMILIES` allowlist, and only if it predates `--before`. There is no
  "mark everything" mode.
- **Why anchored matching, specifically:** the first draft of the classifier used
  unanchored substrings (`%cron%`, `%daily%`, `%nightly%`, `%closeout%`). Run
  against the real backlog it swept six authored messages, among them
  `key-rotation-done-not-just-the-crontab`. That is the same failure the tool
  exists to prevent — v0.9 was found because an unopened chain contained a revoke
  instruction addressed to Guy. Those six subjects are now the fixtures in
  `test_bulk_mark_read.py`, and the suite was proven to FAIL against the loose
  classifier before it was allowed to pass against the anchored one.
- **`:critical:` is carved out** of the sweep even though it matches a family. The
  purpose of clearing digests is to make the unread count mean something again;
  an unopened critical security digest is what that count should still point at.
- **Result:** Opie 710 → 491 unread (29% → 51% read), Rocky 118 → 85 (59% → 71%).
  Nothing human-authored was touched.

## v0.9 — The inbox reported its page size as a count, so a cap read as a measurement

- **Problem:** `GET /mesh/inbox/<agent>` returned a bare array and the MCP tool
  printed `len(rows)` as the count, so `limit=8` rendered as
  "Inbox for Opie (unread) — 8 message(s)". There was no total, no `has_more`,
  and no truncation marker: the output was honest about what it contained and
  silent about what it omitted, and the omission was invisible from the output
  alone. Found by Opie (#2339) after reporting a backlog to Guy as "8", then
  "10", then "20" on three consecutive days — each one the limit, not the count.
  Measured at the time of the fix, the real unread total was **500+**.
- **Fix:** the dispatcher now runs a `COUNT(*)` over the *same predicate* as the
  page, without the limit, and returns `X-Inbox-Total`, `X-Inbox-Returned` and
  `X-Inbox-Truncated`. The MCP tool prints the true total and names what it is
  not showing: `5 of 500 message(s) — TRUNCATED, 495 not shown (raise limit)`.
- **Non-breaking by construction:** the body is still a bare array, so listeners,
  tests and any other consumer are untouched. The counts ride as headers because
  they describe the *call*, not the rows — the same reasoning that keeps
  `first_read` out of `row_to_envelope`.
- The page query and the count share one `from_where` string. Two copies of that
  predicate is how a count starts disagreeing with the rows it claims to count.
- Against an older dispatcher that sends no header, the MCP tool degrades to
  `at least N — page is FULL, total unknown` rather than asserting a number it
  cannot know.
- `test_inbox_total.py` (7 tests) — **verified to fail against the old
  behaviour** (`'5' != '25'`) before being accepted as passing. Covers the exact
  `limit == total` boundary that a naive `len(rows) == limit` check would
  misreport as truncated, and asserts HELD/DROPPED stay excluded from the total
  as well as the page.

## v0.8 — Dispatcher-enforced pause: a paused agent cannot leak a ping by accident

- **Problem:** during planning sessions, agents fire pings mid-discussion —
  half-formed intent escapes and gets superseded minutes later (crossed
  in-flight pings observed same-day). A promise-based hold ("I'll queue my
  drafts") fails exactly when it matters: the enforcement lived in the agent
  that wanted to send.
- **Fix:** pause now lives in the dispatcher. `POST /mesh/pause {agent}` makes
  every subsequent ping FROM that agent land as `state:HELD` — persisted,
  never handed to a delivery thread. `POST /mesh/play {agent}` lifts the pause
  and flushes that agent's held messages one at a time in id order.
  `POST /mesh/release/<id>` ships a single held message while still paused
  (the in-the-open path for genuinely hot items); `POST /mesh/drop/<id>` parks
  it as `DROPPED` — kept in history, never delivered. Both are sender-only.
- HELD/DROPPED are dispatcher-internal: excluded from every inbox view (an
  undelivered draft is not mail, and a HELD reply does not clear unreplied),
  never POSTed to a listener — a flushed message is delivered as a normal
  SENT envelope, so `mesh_version` stays 0.5 and listeners need no change.
- `GET /mesh/pause` reports pause state and held counts for all agents,
  including *orphaned* held rows whose sender is no longer paused (a crash
  window) — a held draft nobody is watching must be visible, not laundered.
  `GET /mesh/held[/<agent>]` lists held envelopes oldest-first.

## v0.7.1 — `ping_read` reports whether THIS call opened the message

- **Problem:** `ping_read` both marks a message read and returns `read_at`, so a
  first read and a hundredth read came back byte-for-byte identical. A reader
  handed a brand-new message id saw a populated `read_at` — the timestamp its own
  call had just written — and reported the message as already read. Observed live:
  Opie stopped acting on fresh bus mail the day v0.7 landed.
- **Fix:** `/mesh/read/{id}` now returns `first_read`, taken from the guarded
  UPDATE's rowcount so a losing racer is correctly told it was not first. The MCP
  `ping_read` leads with a plain-language banner (`NEW —` / `ALREADY READ —`)
  stating that `read_at` on a first read was set by that call.
- `first_read` describes the call, not the row: inbox/history/state listings are
  unchanged and still non-mutating.
- Bumped `robot.info` to match — v0.7 shipped without it, tripping the drift guard.

## v0.7 — Read state separate from reply state

- Added nullable `read_at` with a safe startup migration; existing rows remain unread.
- `ping_read(id)` marks a recipient's message read exactly once.
- `inbox(filter=...)` supports `unread`, `unreplied`, and `all`; default remains `all`.
- Preserved `unread_only=true` as an alias for `filter="unreplied"`.
- Inbox/history/state listing remains non-mutating.

## [Unreleased]

## v0.6.0 — 2026-07-05 — Version anchor (backfill)

Versions 0.2–0.6 shipped before the changelog rule reached this repo; robot.info
carried the version forward without entries here. This entry anchors the current
shipped state so the new robot.info drift-guard test (tests/test_robot_info.py)
has a truth source: mesh v0.5 frozen envelope contract (schema/envelope-v0.5.json),
SQLite-backed push dispatcher + per-agent HTTP listeners, MCP server (ping, inbox,
thread, ping_read, ping_history), optional Discord mirror, install/uninstall
scripts. From here on: every version bump gets an entry BEFORE robot.info moves.

- **Listener bind host is now configurable** (`DISCOBUS_LISTEN_HOST`, default `127.0.0.1`). Problem: the listener hardcoded `HOST = "127.0.0.1"`, so a cross-machine agent could only receive pushes if its listener bound `0.0.0.0` (LAN-exposed) — the same anti-pattern the dispatcher already avoids. Fix: read the bind interface from `DISCOBUS_LISTEN_HOST` so a remote listener can bind a specific Tailscale IP and stay off the home LAN. Default is unchanged, so single-machine setups are unaffected.

## v0.1.0 — Initial public release

**Onboarding** — `install.sh` (interactive wizard for bus-only setup, auto-allocates ports, writes systemd units, smoke-tests end-to-end), `setup-discord.sh` (walks through Discord bot creation + channel ID collection), `uninstall.sh` (clean removal with explicit confirmation before any data deletion). The README's "manual setup" path is now an expandable section — the script path is the default.

**Non-interactive install** — `robot-install.sh` reads a JSON manifest (`robot.install`) and runs the full install with zero prompts. Designed for LLM agents and CI: stdout is a single JSON result object (`{ok, steps: {deps, config, npm, systemd, smoke_test}, error?}`), stderr carries human-readable progress, exit code is 0 on success / 1 on failure. Manifest supports `//` line comments. Agent names are regex-validated. Discord tokens stay out of the manifest (reference an external file via `discord.token_file`). All paths are env-overridable (`DISCOBUS_INSTALL_CONFIG_DIR`, etc.) and a `DISCOBUS_INSTALL_DRY_RUN=1` mode skips systemd write + smoke test for sandboxed testing.

**API additions** (incorporating early review feedback):

- `GET /mesh/inbox/<agent>` — list messages addressed to an agent, newest first. Optional `?unread_only=true` filters to messages with no reply from the recipient.
- `GET /mesh/thread/<id>` — walk the full reply chain. Given any message id, returns the root + every descendant in chronological order.
- `DISCOBUS_MAX_BODY_BYTES` env (default 1 MiB) — reject oversized payloads at the dispatcher boundary.

**MCP server** — two new tools matching the new endpoints:

- `inbox(agent?, limit?, unread_only?)` — defaults to the caller's agent.
- `thread(id)` — full conversation reconstruction.

Total surface: 5 MCP tools (`ping`, `ping_history`, `ping_read`, `inbox`, `thread`).

**Foundation:**

First public cut. Brings together:

- **Dispatcher** (HTTP, SQLite-backed) — `POST /mesh/ping` accepts envelopes, pushes to per-agent listeners, persists state.
- **Per-agent listeners** — tiny HTTP servers, one per agent. Write inbox file on receive. Optional generic auto-reply via subprocess executable (`DISCOBUS_AUTO_REPLY`).
- **Discord mirror** — fire-and-forget side car. Posts full envelope bodies (paginated for Discord's 2000-char per-message limit) to a global firehose channel + per-agent log channels.
- **MCP server** — exposes three tools (`ping`, `ping_history`, `ping_read`) so LLM agents can send, list, and read bus traffic. Identity bound by env; can't spoof `from`.
- **Schema** — frozen v0.5 envelope, JSON Schema. Mismatched versions are rejected.
- **Systemd units** — user-mode services for dispatcher + per-agent listeners.

Distilled from a private internal build that has been running 24/7. Sanitized for general use: agent names are no longer hardcoded, all paths are env-overridable, secrets ship via separate files.
