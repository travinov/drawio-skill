# Roadmap diagrams

Read this when the user asks for a roadmap, product roadmap, project roadmap,
release roadmap, initiative roadmap, milestone roadmap, shifted milestones,
plan drift, delay/acceleration against a previous version, or a time-based
planning diagram with outcomes and dependencies.

Use roadmap flow instead of a generic flowchart when X is time and the diagram
must preserve tasks, milestones, dependencies, outcomes, and baseline shifts.
Use `scripts/roadmap.py` instead of Mermaid Gantt when old/new milestone
positions or dependency influence must be rendered explicitly.

## Commands

```bash
python3 <this-skill-dir>/scripts/roadmap_template.py . --format xlsx --json
python3 <this-skill-dir>/scripts/roadmap_table.py roadmap-template.xlsx -o roadmap.yaml --strict --report roadmap.import.json
python3 <this-skill-dir>/scripts/roadmap_validate.py roadmap.yaml
python3 <this-skill-dir>/scripts/roadmap_validate.py roadmap.yaml --strict
python3 <this-skill-dir>/scripts/roadmap.py roadmap.yaml -o roadmap.drawio
python3 <this-skill-dir>/scripts/validate.py roadmap.drawio --profile roadmap --source roadmap.yaml --strict
python3 <this-skill-dir>/scripts/verify_determinism.py roadmap roadmap.yaml
python3 <this-skill-dir>/scripts/export_smoke.py roadmap.drawio -o roadmap.png
```

## Roadmap workflow

1. When source data is incomplete, offer the canonical XLSX template; offer
   CSV only when XLSX cannot be used.
2. After consent, copy the bundled asset into the working directory, report
   its absolute path, and stop until the user confirms it is filled. Never edit
   the bundled asset. Copying does not require `openpyxl`; if the importer
   dependency is missing, report `python3 -m pip install -r
   <this-skill-dir>/requirements.txt` as remediation without blocking the copy.
3. If asked to fill it, fill only the working copy, summarize it, and stop
   until the user confirms generation.
4. Import the table into deterministic v2 YAML with `roadmap_table.py`, or
   normalize an already complete prose/YAML/XML source into `roadmap.yaml`.
5. Validate `roadmap.yaml` with `roadmap_validate.py`.
6. Generate `.drawio` with `roadmap.py`.
7. Validate it structurally and against its source model; verify determinism.
8. Export locally with `export_smoke.py` when draw.io CLI is available.

## Canonical table intake

`assets/roadmap/roadmap-template.xlsx` contains `Settings`, `Lanes`, `Tasks`,
`MilestoneHistory`, `Dependencies`, `Outcomes`, `Lists`, and `Instructions`.
Yellow columns calculate `previous_planned_date`, `shift_days`,
`cumulative_shift_days`, and `shift_state`. The importer ignores those formula
results and recalculates all shifts from authoritative revision coordinates.
The macro-free workbook has no external links. `roadmap-template.csv` is the
data-only long-form fallback with an `entity_type` discriminator.

## Canonical v1 `roadmap.yaml`

```yaml
schema_version: 1
title: Payments roadmap
time_scale: quarter
lane_dimension: product
shift_threshold_days: 0

lanes:
  - id: checkout
    title: Checkout
  - id: billing
    title: Billing

outcomes:
  - id: outcome-fast-payments
    title: Faster successful payments

tasks:
  - id: task-wallets
    title: Wallet support
    lane: checkout
    start: 2026-07-01
    end: 2026-09-30
    status: at_risk
    owner: Payments
    outcomes: [outcome-fast-payments]
    milestones: [m-wallet-pilot]

milestones:
  - id: m-wallet-pilot
    title: Wallet pilot
    lane: checkout
    date: 2026-09-30
    status: at_risk

dependencies:
  - id: dep-billing-api
    from: m-billing-api
    to: m-wallet-pilot
    type: blocks
    impact: high
    rationale: Billing API must be stable before pilot

baseline:
  version: 2026-06
  milestones:
    - id: m-wallet-pilot
      title: Wallet pilot
      lane: checkout
      date: 2026-09-15
```

## Field rules

| Field | Required | Notes |
|---|---:|---|
| `schema_version` | yes | `1` for baseline comparison; `2` for full milestone revision history |
| `title` | yes | Diagram title |
| `time_scale` | no | `week`, `month`, `quarter`, `date`, or `order`; default `month` |
| `lane_dimension` | no | Human-facing grouping label, e.g. `product`, `team`, `workstream` |
| `shift_threshold_days` | no | Minimum absolute date movement to label as a shift; default `0` |
| `lanes[]` | no | If absent, generator creates one default lane |
| `tasks[]` | no | Calendar scales require `start`/`end`; `order` requires `start_order`/`end_order` |
| `milestones[]` | yes | Calendar scales require `date`; `order` requires integer `order` |
| `dependencies[]` | no | `blocks`, `depends_on`, `influences`, `relates_to` |
| `outcomes[]` | no | Business results attached to tasks or milestones |
| `baseline` | no | Previous version data for diff rendering |

IDs must be stable, unique, and match `^[A-Za-z][A-Za-z0-9_-]*$`. Prefer IDs
from the source system. When source data lacks IDs, derive them deterministically
from the label and date, and record the assumption.

The canonical schemas are `data/roadmap.v1.schema.json` and
`data/roadmap.v2.schema.json` (JSON Schema Draft 2020-12). During one
compatibility release an input without `schema_version` is
validated as v1 and receives `contract.version.missing`; the source file is not
rewritten. Unknown versions and unknown object properties are errors.

Calendar and ordinal coordinates are exclusive. Do not mix `date`/`start`/`end`
with `order`/`start_order`/`end_order` in one model or its baseline.

## Canonical v2 milestone history

Use one current milestone plus all previous revisions under `history`. Revision
IDs and orders must be unique; order and `recorded_at` must increase; the
current revision must have the greatest `revision_order`.

```yaml
schema_version: 2
title: Payments roadmap
time_scale: month
shift_threshold_days: 3
milestones:
  - id: m-wallet-pilot
    title: Wallet pilot
    date: 2026-10-07
    revision_id: rev-4
    revision_order: 4
    plan_version: 2026-08
    recorded_at: 2026-08-03
    reason: Schedule optimized
    history:
      - {revision_id: rev-1, revision_order: 1, plan_version: 2026-05, date: 2026-09-15, recorded_at: 2026-05-10}
      - {revision_id: rev-2, revision_order: 2, plan_version: 2026-06, date: 2026-09-30, recorded_at: 2026-06-12}
      - {revision_id: rev-3, revision_order: 3, plan_version: 2026-07, date: 2026-10-14, recorded_at: 2026-07-08}
```

This example recalculates `+15d`, `+14d`, `-7d`, and `+22d` cumulative.

## Input normalization

### Prose

Extract these fields when present: roadmap title, time scale, lanes, tasks,
milestones, dates/periods, dependencies, owners, statuses, risks, outcomes, and
baseline references. Preserve uncertain mappings under an `assumptions` list.

### Tables

Common column mapping:

| Column names | Model field |
|---|---|
| `id`, `key`, `issue`, `initiative` | `tasks[].id` or `milestones[].id` |
| `title`, `name`, `summary` | `title` |
| `lane`, `product`, `team`, `stream`, `project` | `lane` |
| `start`, `from` | `tasks[].start` |
| `end`, `target`, `due` | `tasks[].end` or `milestones[].date` |
| `milestone`, `gate`, `checkpoint` | `milestones[]` |
| `depends on`, `blocked by`, `dependency` | `dependencies[]` |
| `outcome`, `result`, `business result` | `outcomes[]` |
| `status`, `risk` | `status` / `risk` |

### YAML

If the YAML already follows the canonical shape, validate it directly. Do not
rewrite labels, dates, IDs, or ordering unless validation requires it.

### XML

Map supported XML elements into the canonical model. If an XML source has no
documented roadmap schema, only map obvious fields such as `id`, `title`,
`lane`, `start`, `end`, `date`, `status`, `owner`, `dependency`, and `outcome`.
Report unsupported elements instead of guessing silently.

## Baseline comparison

Baseline comparison matches by stable `id` first. If IDs are absent, a fallback
match by normalized title and lane is allowed only when the ambiguity is low and
the assumption is recorded.

Calculated milestone delta states:

| State | Condition | Rendering |
|---|---|---|
| `delayed` | current date is later than baseline date | red dashed arrow, label `+Nd` |
| `accelerated` | current date is earlier than baseline date | green dashed arrow, label `-Nd` |
| `unchanged` | same date or below threshold | no shift arrow |
| `added` | exists only in current roadmap | current marker with `new` label |
| `removed` | exists only in baseline | faded baseline marker with `removed` label |

Dependency and outcome changes are reported in validation summaries even when
they are not rendered as separate visual deltas.

## Rendering conventions

- X-axis is time; Y-axis is lanes.
- Render task duration bars behind milestone markers.
- Render old baseline milestone positions as dashed/faded diamonds.
- Render current milestone positions as solid diamonds.
- In v2 render every historical revision as a faded diamond and every
  consecutive move as its own `+Nd`/`-Nd` arrow.
- Render shift arrows from baseline position to current position.
- Label material shifts with day deltas such as `+15d` or `-7d`.
- Render blocking dependencies as solid directed arrows.
- Render influence relationships as dashed or open-arrow relationships.
- Keep outcome labels concise and link them from both tasks and milestones.
- Status controls the fill palette. Risk uses a stronger border plus a visible
  warning annotation, so both fields remain independently visible.
- Use warning validation for overcrowded lanes, invalid time ranges, and
  excessive dependency crossings.

## Troubleshooting

| Symptom | Action |
|---|---|
| Unknown lane | Add the lane or let the validator create a clear error |
| Unknown milestone ref | Fix the task/dependency ref before rendering |
| Date cannot be parsed | Use ISO dates: `YYYY-MM-DD` |
| Too many dependency crossings | Filter low-impact influence edges or split the roadmap |
| Milestone shift missing | Check baseline milestone id and `shift_threshold_days` |
