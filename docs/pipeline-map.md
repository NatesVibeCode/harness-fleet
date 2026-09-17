# The pipeline map

What exists, who writes it, who reads it, and which of those pairings a test
holds together. Written because the audit kept finding the same shape of defect
— **two places that should agree, with nothing asserting they do** — and because
each one was found by accident rather than by looking somewhere it was known to
live.

Read this before changing a writer. If a row is added to a table here, the
"guarded by" column should not stay empty.

## 1. The chain

`lane_spec` compiles a lane's ladder into this, and every node writes a table
into the run's own SQLite database:

| node | kind | reads | writes |
|---|---|---|---|
| `g0-<rung>` | gate | the captured items, or a run's records | `rung_rows`, `rung_text` |
| `x0-<rung>` | resolve | an open firmographic at that rung | `rung_rows`, `rung_text` (`"<name>" address`) |
| `c0-<rung>` | gate | what the search said | `rung_rows`, `rung_text` |
| `r1-<rung>` … | retrieve | the survivor ids of the gate below | `rung_rows`, `rung_text`, `rung_items` |
| `g1-<rung>` … | gate | what the walk brought back | `rung_rows`, `rung_text` |
| `s-score` | score | the standing population, every node above | `rung_rows`, `rung_text`, `entity_state`, then the campaign's `score_history` |

A candidate eliminated at any node is absent from every table above it, and a
candidate whose open gates a search settled is never walked.

## 2. Sources of truth

One writer per fact. A second writer is the defect, not the fix.

| fact | writer | readers | guarded by |
|---|---|---|---|
| a node's population and verdicts | `rungs.RungTables.write` | the nodes above it, `cli.funnel_population`, `cli.funnel_stage_report`, `cli.funnel_walk_report` | `test_rung_dag`, `test_funnel_wiring` |
| the prose a verdict stood on | `rungs.RungTables.write` (`texts=`) | the gate above, `cli.funnel_walk_report` | `test_rung_dag`, `test_ledger` |
| what a walk gathered | `rungs.RungTables.write_items` | the score node | `test_ledger` |
| who is standing | `rungs.population` | the score node, `cli.funnel_population` | `test_ledger::test_the_report_and_the_node_agree…` |
| an entity's current belief | `ledger.Ledger.record` (insert branch) | `cli.ledger`, `board.ledger_view` | `test_ledger::test_a_walk_does_not_erase…` |
| …and every visit that moved it | `ledger.Ledger.record` (insert branch, append-only) | `ledger.history`, `cli.ledger --entity` | `test_ledger` |
| a score trajectory | `store.record_score_history` (engine) **and** `Ledger.record` (nodes) | `ledger.scores` — one reader, both records — then `cli.history`, `cli.ledger --trend`, the MCP history tool | `test_ledger::test_one_trajectory_from_both_records`, `test_mcp_server` |
| a tier | `evidence.enforce_tier` over `coverage` | `dag._execute_score_node` → `entity_state.tier`; `board` exposes `tier_supported` beside the band | `test_ledger`, `test_board` |
| attempts | `RungTables.write` (append, one per run) | `RungTables.rows(attempt)`, `cli.db stats` | `test_ledger` |

## 3. Invariants, and where they are held

1. **A snippet eliminates; only a fetched page qualifies.** `test_funnel_wiring`, `test_gates`.
2. **A candidate killed below is absent above**, and is never fetched. `test_rung_dag::test_the_walk_reads_only_what_the_rung_below_it_passed_on`.
3. **`outcome` holds a verdict and nothing else** — not a visit, not a score, on the way in *or* the way through. `test_ledger::test_a_walk_does_not_erase_the_verdict…`, `test_the_scoring_stage_records_a_number_not_an_outcome`.
4. **A tier is capped by evidence, not by the claim.** `test_ledger`, `test_board::test_the_board_separates_the_band…`.
5. **A write is an attempt; nothing overwrites an answer.** `test_ledger::test_rerunning_a_rung_keeps_the_earlier_answer`.
6. **Pruning keeps the newest attempt and never touches the belief.** `test_ledger::test_prune_keeps_the_answer…`, `test_pruning_the_log_never_touches_the_belief`.
7. **Docs name tables that exist and commands that parse.** `test_docs_commands`, plus the README lane table in `test_shipped_lanes`.
8. **`docs/funnels.md` is the renderer's output, byte for byte.** `test_docs_commands::test_the_funnels_doc_is_the_renderer_s_output`.
9. **A trajectory is both records, wherever it is read** — the CLI *and* the MCP
   history tool. `test_mcp_server::test_the_mcp_history_uses_the_same_reader_as_the_cli`.
10. **A tier cap is given the uri its evidence came from.** `test_ledger::test_the_tier_cap_is_given_the_uri_it_was_gathered_from`.
11. **The campaign, the node's rows and the running list agree about every
    score**, for one scored run, firm by firm. `test_ledger::test_the_campaign_and_the_ledger_agree_about_every_score`.
12. **`db stats` counts what was written.** `test_ledger::test_db_stats_counts_what_was_actually_written`.
13. **`lane report` measures the records the scoring node wrote.**
    `test_ledger::test_the_lane_report_counts_what_the_nodes_scored`.
14. **A graph over an existing run judges on the whole chain above it**, not only
    on the walks — a discovery run's records are evidence, and a walk that comes
    back empty is not a run that found nobody. Held by (11), which failed on
    exactly that before it passed.

## 4. Not guarded yet — the work queue

1. **`board` payload against the ledger, per entity.** The board's
   `tier_supported` and `score` come from the run's `evidence.json` and the
   packet; `entity_state` comes from the node's rows. The *shape* of both is
   guarded (#3, #11) but nothing compares them for the same run and firm, which
   is where a stale `evidence.json` would show up.
2. **A graph re-run over the same `dag_id`.** Attempts are kept and the newest
   is read; nothing asserts that a second `run_dag` over an existing dag leaves
   the first attempt's answers readable and unchanged.
3. **`docs/pipeline-map.md` itself.** Its commands and table names are checked;
   the claims in its tables — who writes what — are prose. A cheap guard would
   assert the writer column against the code that calls `RungTables.write`.

## 5. Decisions that are not mine to make

- **The account row carries no fields.** `account-research` defines no `answers`,
  so a scored account has a score and a gap and no verticals, stack or clients.
  The ICP names them; nothing writes them.
- **GSI / RSI / SI is not a column.** `partner_kind` exists and the class is
  derivable from headcount and territory, but nothing puts it on the row.
- **Known firms are not excluded.** A list or a rule — the operator's call.

## 6. How to work this map

Pick the highest unguarded pairing in §4, write the test that compares the two
surfaces, and fix whichever one is wrong. That is how every defect in §3 was
found: not by reading code, but by asserting that two things which must agree
do.
