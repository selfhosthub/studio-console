# Bundled data

## `known_baselines.json`

Maps each Studio **major version** to the list of alembic **baseline revisions**
that identify a database as belonging to that major:

```json
{ "1": ["9f01e7ae15f2"] }
```

`major_version.classify_revision` uses it to block install/upgrade/restore when a
DB sits on a prior major (or an unknown future major) before the API would fail
to start. A DB rev that matches no listed baseline classifies as `ok` and defers
to the API's own guardrail — that fallback is intentional; do NOT block unknown
revisions here.

### This file is a release artifact — it MUST be re-synced from studio-app

A major has **more than one** baseline whenever studio-app squashes-and-stamps
its migration chain to a new baseline. Every historical baseline that a
**restorable** DB might still be stamped to has to stay in that major's list —
dropping one makes those DBs unrecognizable, so a prior-major restore slips past
the upfront block and only fails later at API start.

Update this file in the **same change** that:

- introduces a **new Studio major** → add a new `"<major>": ["<baseline>"]` entry, or
- **squashes-and-stamps** a new baseline for any major a deployed/restorable DB
  could be on → **append** the new baseline to that major's list (never replace).

Cadence: studio-app squashes periodically; this file lags until the next console
release. That staleness window is covered by the API guardrail + log-scrape
fallback, but it is a window — sync promptly.

The format also accepts the legacy scalar form (`{"1": "abc"}`); `load_baselines`
normalizes a scalar to a single-element list. Prefer the list form.
