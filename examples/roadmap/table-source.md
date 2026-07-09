# Roadmap table source example

| id | title | lane | start | end | milestone | milestone_date | status | dependency | outcome |
|---|---|---|---|---|---|---|---|---|---|
| task-wallets | Wallet support | checkout | 2026-07-01 | 2026-09-30 | m-wallet-pilot | 2026-09-30 | at_risk | m-billing-api | Faster successful payments |
| task-billing-api | Billing API hardening | billing | 2026-06-15 | 2026-08-01 | m-billing-api | 2026-08-01 | on_track |  | Stable billing foundation |

Normalize columns according to `references/roadmap.md`: lane/product/team fields
become lanes, task rows become `tasks[]`, milestone columns become
`milestones[]`, dependency columns become `dependencies[]`, and outcome columns
become `outcomes[]`.
