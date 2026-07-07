# Git-flow diagrams

Read this when the user asks for a git-flow, branching strategy, release
branch, hotfix branch, feature branch, or a branch timeline diagram where
placement matters.

Use `scripts/gitflow.py` instead of Mermaid `gitGraph` when the diagram must
preserve timeline/lane semantics: X is time or event order, Y is the branch
lane. Mermaid `gitGraph` is still fine for compact Markdown-rendered examples
or when the user only needs a simple commit graph.

## Commands

```bash
python3 <this-skill-dir>/scripts/gitflow_validate.py flow.json
python3 <this-skill-dir>/scripts/gitflow_validate.py flow.json --strict
python3 <this-skill-dir>/scripts/gitflow.py flow.json -o git-flow.drawio
python3 <this-skill-dir>/scripts/gitflow.py flow.json -o git-flow.drawio --route builtin
python3 <this-skill-dir>/scripts/gitflow.py flow.json -o git-flow.drawio --route graphviz
python3 <this-skill-dir>/scripts/validate.py git-flow.drawio
```

`--route auto` is the default. It uses Graphviz `neato -n2` when available and
falls back to the built-in deterministic router when Graphviz is not installed.
Graphviz only contributes edge bend points; semantic node coordinates are fixed
before routing and are never replaced by Graphviz.

## Input schema

```json
{
  "title": "Release 2.4 flow",
  "workflow": "git-flow",
  "timeMode": "date",
  "branches": [
    {"id": "main", "label": "main", "kind": "main"},
    {"id": "develop", "label": "develop", "kind": "develop"},
    {"id": "feature_auth", "label": "feature/auth", "kind": "feature"}
  ],
  "events": [
    {"id": "c1", "type": "commit", "branch": "main", "label": "v2.3", "at": "2026-07-01"},
    {"id": "b1", "type": "branch", "from": "main", "to": "develop", "at": "2026-07-02"},
    {"id": "m1", "type": "merge", "from": "develop", "to": "main", "label": "release", "at": "2026-07-07"},
    {"id": "t1", "type": "tag", "branch": "main", "label": "v2.4.0", "at": "2026-07-07"}
  ]
}
```

Use `timeMode: "order"` with integer `order` when dates are unknown. If dates
repeat, the generator gives same-date events a stable local offset in JSON
order.

## Git-flow rules

The validator enforces structural errors in all modes: malformed JSON, missing
ids, duplicate ids, invalid event types, unknown branches, and self merges.

Use `workflow: "custom"` for corporate release-based flows that are not
canonical git-flow, for example `master -> release/{n}` plus `spec/*` and
`feature/*` branches without `develop`.

Canonical git-flow rules are warnings by default and errors with `--strict`:
feature branches should start from and merge back to `develop`; release branches
should start from `develop` and merge to both `main`/`master` and `develop`;
hotfix branches should start from `main`/`master` and merge to `main`/`master`
plus `develop` or an active release branch.

## Layout conventions

Default lane order is `main/master`, `hotfix/*`, `release/*`, `develop`,
`feature/*`, `support/*`, then custom branches. Branch lanes are horizontal.
Commit and merge events are markers on the target lane; branch and merge
connectors are orthogonal edges with waypoints.

V1 renders explicit JSON only. It does not parse `git log` or reconstruct a real
repository history. `workflow: "custom"` validates structure and skips canonical
git-flow policy checks.
