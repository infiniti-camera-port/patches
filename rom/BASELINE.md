# Sync baseline

The plan's Todo 4 acceptance is that a sync with the mirror dataset mounted
"beats the recorded baseline". This is that record. Without it the acceptance
cannot be evaluated, because there is nothing to beat.

## Network-only sync, no local mirror

Measured on `aosp-builder-noble`, lane `1vivy`, from
`/srv/android/logs/STATUS-1VIVY` and its sync log:

| field | value |
| --- | --- |
| started | 2026-08-02T19:51:00Z |
| completed | 2026-08-02T20:29:46Z |
| duration | **38m46s** |
| projects | 1208 |
| tree size | 206G |
| mirror | none mounted |
| method | `-c -j8` sweeps without fail-fast, then one `-j4 --fail-fast` pass |

## How a future run is compared

`rom <lane> sync` now records `seconds=` and `mirror=yes|no` in its status line,
so a later run is comparable without reading logs or timing it by hand:

    state=SYNCED lane=1vivy ... scope=all seconds=2326 mirror=no updated=...

A mounted-mirror run beats the baseline when `seconds` is materially below 2326
with `mirror=yes` and the same `scope=all`. A scoped sync is not comparable to
it - `scope` is recorded precisely so the two are not confused.

## Status of the dataset

Not created, and not creatable from this agent: the plan places it at Tier 4 in
`docs/backup-platform.md:88`. Confirmed absent on the builder at the time of
writing - no NFS mounts, no `/srv/android/mirror`, no `/mnt/truenas`. Until it
exists, `rom sync` takes the documented degradation path: it warns, passes no
`--reference`, and syncs from the network.
