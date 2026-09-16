---
name: career-fleet
description: Run the career lane — enterprise sales and operations roles, remote, scored from public sources with character-exact quotes. Use when searching for roles rather than companies.
---

# Career lane

This is the `career` lane of `harness-fleet`, not a second tool. There is one
CLI and one procedure; this lane supplies what is specific to role search.

```bash
harness-fleet research --lane career            # discover, bundle, score, export
harness-fleet lane report <run_id> --lane career
```

The lane answers "which employers are hiring for what I do", not "which
employers exist": its config requires the posting text to match
`enterprise sales` / `sales ops` / `sales operations` and to be remote, and
items that fail those filters are dropped and counted rather than ranked.

- **Lane config:** `lanes/career.json` — edit it (or drop one in
  `<workspace>/lanes/career.json`) to change queries, sources or filters.
- **Preset:** `triage` · **Bar:** `require_kinds: ["delivery_hiring"]` — a role
  counts only when its page is an actual requisition, not a list of them · **Output:** top 50.
- **Evidence:** quotes are character-exact and verified against the source, and
  a claim only scores when its quote states the thing being claimed.

For roles rather than companies, point the lane's `backends` at the boards that
carry postings (`fetch --greenhouse-board`, `fetch --ashby-org`,
`fetch --lever-org`), instead of community sources, which produce leads rather
than requisitions.
