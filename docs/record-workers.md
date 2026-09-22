# Recording workers

The local [`record-workers.toml`](../record-workers.toml) replaces
`record-mode-canary.toml` with a two-worker fleet for ongoing TauCetiBench
collection. It uses this workstation's `tauceti` account and paths. Both workers
are enabled in desired state; the configuration has been validated but not
applied.

| Worker | Provider | Tasks | Captures |
| --- | --- | --- | --- |
| `record` | Codex | Review fixes, roadmap authoring | `fix-review-raw`, `roadmap-opportunity-raw` |
| `worker` | Claude Code | Rebases, Mathlib bump repairs, progress reports, CI fixes, PR reviews | None |

Every phase has one owner. Only `fix` and `roadmap` currently produce captures,
so `record` spends its model allowance on those phases. It follows the
existing cascade: eligible fixes take priority over roadmap work. `worker`
handles the remaining phases in their normal priority order and uses the same
review settings as Barbarella's Ping: a 20-minute minimum head age and at most
four review rounds per PR per UTC day in its own review store. Maintenance work
can delay reviews because it precedes them in the cascade.

Both workers select an explicit provider and inherit the repository's model
defaults. This assigns Codex the captured implementation work and uses the
Claude subscription for maintenance and review; it is an operating choice,
not a conclusion about model quality.

## Pacing

Both workers use `pace = "0:10,100:65"`, independently against their providers'
account-wide usage in both session and weekly windows:

| Window elapsed | Maximum usage before launch |
| --- | ---: |
| 0% | 10% |
| 35% | 29.25% |
| 50% | 37.5% |
| 100% | 65% |

Interactive work, benchmarks, and other workers on the same provider account
count toward the same watermark. In-flight rounds can overshoot the limit;
the curve is a soft launch gate, leaving roughly 35% beyond the final ceiling.
There is no automatic minimum usage threshold or unlimited worker in this fleet.

Unlike Barbarella's separate fixer with a higher ceiling, the recording worker
shares one curve between fixes and roadmap work. Fixes have scheduling priority,
but wait too when Codex is held back by pacing. Claude can continue maintenance
and reviews while its own account has room. This keeps the requested two-worker
split; the current config cannot give phases within one worker different curves.

## Record store and worker state

`record` continues writing to
`/home/tauceti/TauCetiWorker/state/records`. Existing captures stay in place and
identical task inputs still deduplicate. Worker names are provenance, not part
of capture identity. Recording remains fail-open: a failed capture is logged
and live work continues. These workers perform real work after capture; they
are not an offline benchmark runner. See the [record-mode contract](record-mode.md).

The new worker ids get their own checkout, counters, review store, and logs.
The former `record-canary` state and checkout remain available, but are not
migrated. `worker` explicitly clears inherited `TAUCETI_RECORD_DIR`.

## Login and switching fleets

Run only one worker-manager configuration at a time. `worker` uses the dedicated
Claude login at `/home/tauceti/.config/tauceti/claude-ping`. If needed,
authenticate it as the `tauceti` Unix user before applying the fleet:

```bash
CLAUDE_CONFIG_DIR=/home/tauceti/.config/tauceti/claude-ping claude auth login
```

Do not use that directory for interactive sessions or another token refresher.
Only the active fleet's Claude worker should refresh the source credential.

From `/home/tauceti/TauCetiWorker`, validate without launching:

```bash
./tauceti workers --config ./record-workers.toml apply --check
```

When ready to start, first stop any active manager and its workers. This also
handles a manager still pointing at the retired `record-mode-canary.toml` path;
`manager-stop` addresses the active manager without loading that old file.
Skip the shutdown command if no manager is running:

```bash
./tauceti workers --config ./record-workers.toml manager-stop
./tauceti workers --config ./record-workers.toml apply
./tauceti workers --config ./record-workers.toml status
```

Stopping outgoing workers prevents two refreshers from using the same login at
once. Applying before Claude is installed and logged in leaves `worker` unable
to work; `record` uses its own Codex provider.

Monitor and toggle collection with:

```bash
./tauceti workers --config ./record-workers.toml logs record --follow
./tauceti workers --config ./record-workers.toml logs worker --follow
./tauceti workers --config ./record-workers.toml disable record
./tauceti workers --config ./record-workers.toml enable record
```

Disabling `record` leaves `worker` available for maintenance and
reviews. Enable/disable commands rewrite the TOML and remove its comments.
