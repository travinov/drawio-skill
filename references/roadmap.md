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
python3 <this-skill-dir>/scripts/roadmap_validate.py roadmap.yaml
python3 <this-skill-dir>/scripts/roadmap_validate.py roadmap.yaml --strict
python3 <this-skill-dir>/scripts/roadmap.py roadmap.yaml -o roadmap.drawio
python3 <this-skill-dir>/scripts/validate.py roadmap.drawio
```

## Roadmap workflow

1. Run Diagram Intake Agent when the request is broad or source data is
   incomplete.
2. Normalize prose, table, YAML, or XML source data into `roadmap.yaml`.
3. Validate `roadmap.yaml` with `roadmap_validate.py`.
4. If a baseline is present, calculate milestone/task/dependency/outcome deltas.
5. Generate `.drawio` with `roadmap.py`.
6. Validate the generated `.drawio` with `validate.py`.
7. Export via draw.io CLI using the normal skill workflow when CLI is available.

## Canonical `roadmap.yaml`

```yaml
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
| `title` | yes | Diagram title |
| `time_scale` | no | `week`, `month`, `quarter`, `date`, or `order`; default `month` |
| `lane_dimension` | no | Human-facing grouping label, e.g. `product`, `team`, `workstream` |
| `shift_threshold_days` | no | Minimum absolute date movement to label as a shift; default `0` |
| `lanes[]` | no | If absent, generator creates one default lane |
| `tasks[]` | no | Duration bars; may reference milestones and outcomes |
| `milestones[]` | yes | Point markers; ids should remain stable across versions |
| `dependencies[]` | no | `blocks`, `depends_on`, `influences`, `relates_to` |
| `outcomes[]` | no | Business results attached to tasks or milestones |
| `baseline` | no | Previous version data for diff rendering |

IDs must be stable, unique, and match `^[A-Za-z][A-Za-z0-9_-]*$`. Prefer IDs
from the source system. When source data lacks IDs, derive them deterministically
from the label and date, and record the assumption.

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
- Render shift arrows from baseline position to current position.
- Label material shifts with day deltas such as `+15d` or `-7d`.
- Render blocking dependencies as solid directed arrows.
- Render influence relationships as dashed or open-arrow relationships.
- Keep outcome labels concise and near the linked task or milestone.
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
