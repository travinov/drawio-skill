# Diagram Intake Agent

Use this before generation when the user's request is broad, ambiguous, or
non-trivial. The agent's job is to turn natural language into a confirmed
diagram brief, not to generate the diagram.

The intake uses a question matrix, not a fixed questionnaire. Generate only the
smallest set of questions that removes high-impact ambiguity. Do not ask about
facts already present in the user's request.

## Output

Produce a short confirmed diagram brief:

```markdown
Type: custom branch timeline
Audience: team regulation / onboarding
Detail: key branch, PR, merge, and artifact events
Layout: branch lanes; roles shown by color
Output: .drawio + PNG
Assumptions: no dates supplied, so use step order
```

If key details are missing, ask first. For simple explicit requests, skip
questions and proceed with assumptions.

When an existing `.drawio` is supplied, the brief must also state whether the
request is layout-only or changes process semantics. Compare the user's process
description with the represented nodes and relationships and name missing
decision branches, failure/return loops, actors, or transitions. If relevant
OpenSpec material exists in the repository, cite it as a source for the brief;
if it conflicts with current user intent, defer to the consolidated decision
rule in `references/diagram-supervisor.md`.

## Question limits

Ask at most 3 questions for ordinary requests and at most 5 questions for
complex architecture, process, regulatory, or multi-system diagrams.

Always end non-trivial intake with one optional free-form visual preference question when visual/layout preferences are not already specified:

```text
Есть ли дополнительные пожелания к виду диаграммы?
Например: не разносить роли по дорожкам, а выделить их цветом; сгруппировать
шаги по этапам; сделать схему компактнее; показать артефакты отдельными
блоками; использовать корпоративный стиль.
```

If the user does not answer, proceed with conservative defaults and record the
assumption in the confirmed brief.

## Classification

Choose the most likely type and confidence:

| User intent | Likely route |
|---|---|
| actors exchanging messages over time | sequence, `scripts/seqlayout.py` |
| product/project/release roadmap, milestones over dates, plan drift, baseline comparison | roadmap, `references/roadmap.md`, `scripts/roadmap_validate.py`, `scripts/roadmap.py` |
| services, systems, integrations, protocols | architecture or C4 |
| C4 context/container/component wording | C4, `scripts/c4.py` |
| tables, PK/FK, schema, SQL DDL | ERD, `scripts/sqlerd.py` if DDL exists |
| class hierarchy, interfaces, inheritance | UML class |
| branches, PRs, release/hotfix/feature timeline | git-flow/custom flow, `scripts/gitflow.py` |
| business or team procedure | process / flowchart; consider swimlanes |
| source code or IaC structure | importer + `scripts/autolayout.py` |

When confidence is low, ask the user to choose between 2-3 plausible diagram
types before asking detailed questions.

## Question matrix

General questions, ask only if missing and material:

| Need | Question |
|---|---|
| Audience | "Для кого диаграмма: регламент, onboarding, architecture review, презентация?" |
| Detail | "Нужен overview или подробная схема по шагам/событиям?" |
| Output | "Нужен только `.drawio` или еще PNG/SVG/PDF?" |
| Language | "Оставить подписи на языке запроса или перевести?" |
| Layout preference | "Нужны дорожки по ролям/системам или обычная схема?" |

Type-specific prompts:

| Type | Ask when missing |
|---|---|
| sequence | participants, sync/async calls, error/alt paths |
| architecture | system boundary, external systems, protocols, trust zones |
| C4 | Context, Container, Component level and drill-down pages |
| ERD | table list, PK/FK, whether to show column types |
| UML class | target modules/classes and inheritance vs associations |
| git-flow/custom flow | workflow type, branch names, timeline mode, merge/tag/PR events |
| roadmap | time scale, lane dimension, input format, milestone ids, baseline version, material shift threshold |
| process | swimlanes by role/team/system, decision points, start/end states |

For process diagrams, explicitly check failure and rejection paths: what
happens when a day/stage/check does not pass, where control returns, and what
terminates the loop. Missing return behavior is a semantic gap, not a routing
preference.

## Defaults

Use these defaults when the user does not answer or ambiguity is low risk:

- Diagram type: infer from nouns and verbs in the request.
- Detail: key transitions only; compress internal work into labels.
- Layout: lanes by the main structural entity (branches for git-flow, roles for
  process only when the user asks or roles are central; products/teams/workstreams
  for roadmap diagrams).
- Timeline: use explicit dates if present, otherwise step order.
- Output: `.drawio` plus PNG preview when the CLI is available.
- Visual style: active preset if configured, otherwise built-in defaults.

## Roadmap intake

Use roadmap intake when the request mentions a roadmap, product roadmap, project
roadmap, release roadmap, initiative roadmap, milestone roadmap, plan movement,
baseline comparison, shifted milestones, delay, acceleration, or plan drift.

Ask only questions that affect the roadmap model or comparison:

| Need | Question |
|---|---|
| Time scale | "Какая шкала нужна: недели, месяцы, кварталы или конкретные даты?" |
| Lanes | "По чему группировать дорожки: продуктам, командам, проектам, стримам или владельцам?" |
| Baseline | "Есть предыдущая версия roadmap для сравнения? Если да, пришлите ее или укажите файл." |
| IDs | "Есть стабильные ID задач/вех или сопоставлять по названиям и датам?" |
| Shift threshold | "Показывать любое смещение вех или только смещения больше N дней/недель?" |

Confirmed brief example:

```markdown
Type: roadmap
Audience: product planning review
Input: YAML current roadmap + previous YAML baseline
Detail: tasks, milestones, dependencies, outcomes, milestone shifts
Layout: lanes by product, X-axis by quarter
Output: .drawio + PNG
Assumptions: show every milestone date change; match milestones by stable id
```

## Common mistakes

- Do not run a long interview. Ask only questions that change the diagram.
- Do not ask for output location unless the user mentions a location-sensitive
  workflow.
- Do not force canonical git-flow when the user describes a release-based
  custom process without `develop`; use `workflow: "custom"`.
- Do not use role swimlanes by default when the important lanes are branches,
  systems, or services; roles can be shown by color or labels instead.
