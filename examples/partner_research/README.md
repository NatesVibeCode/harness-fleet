# Implementation Partner Research & Scoring Example

This example demonstrates how to discover, evaluate, qualify, and score potential **Implementation Partners, Systems Integrators (SIs), and Specialized Consultancies** with **character-exact source evidence**:

1. **Implementation Partner Research & Qualification**: Scores partner candidates 0–100 and clips verbatim evidence of active practices, certified alliances, and client delivery case studies.
2. **The Partner Compounding Filter**: Filter broad candidate pools down to qualified, specialized service partners without wasting model compute.
3. **Verifiable Ranked Deliverable**: Export a clean, sorted CSV (`ranked_implementation_partners.csv`) with rank, partner score, identified practice, and verbatim proof quotes ready for alliance managers.

---

## Files in this Directory

- `sample_partners.csv`: Small hand-written sample (Slalom, Trace3, DataBridge, Apex, PixelCraft, Stripe) for a first smoke run.
- `sample_partners_multisource.csv`: The same contract with several sources per partner and a `source_categories` column, so the multi-source gate has something to work with.
- `showcase_partners.csv`: **Real** dossiers built by the sourcing runner (`harness-fleet partners enrich <domain>`) for trace3.com, slalom.com, excella.com, thoughtworks.com and onixnet.com — first-party practice pages, ATS boards, case studies, community threads and press in one row per partner.
- `task.json`: Pre-configured closed task specification: the 10-question revenue checklist (100 points), the attribute set, and the pipeline-computed `score`, `fit_tier`, `identified_practice`, `revenue_hypothesis` and `reasoning`.

---

## Quickstart (CLI)

### 1. Initialize Task from Preset
You can initialize directly from the built-in `partner-research` preset:

```bash
harness-fleet init partner-qualification --preset partner-research
```

Or run directly from the bundled `task.json`:
```bash
harness-fleet run examples/partner_research/task.json \
  --input examples/partner_research/sample_partners.csv \
  --id-column domain \
  --text-column research \
  --run-id partners-01
```

### 2. Export Ranked Implementation Partners CSV

Generate the ranked deliverable (sorted by score descending, with a 1-indexed rank column):

```bash
harness-fleet export partners-01 \
  --format csv \
  --sort-by score \
  --desc \
  --top 10 \
  --rank \
  --output ranked_implementation_partners.csv
```

Output format (`ranked_implementation_partners.csv`):
```csv
rank,item_id,score,fit_tier,identified_practice,reasoning,primary_quote_text,quote_count,source_uri,source_digest
1,slalom.com,100,tier_1,"Premier Consulting Partner & Cloud Data Platform Modernization","Solutions architects work directly with Fortune 500 clients delivering Snowflake, Kafka, and Kubernetes solutions","work directly with Fortune 500 enterprise clients to architect, deploy, and modernize cloud data platforms",1,"",...
2,trace3.com,100,tier_1,"Enterprise Apache Kafka Streaming & Multi-Region Kubernetes Migration","Turnkey client solutions with case study for top-10 financial services customer","Leading the enterprise Apache Kafka streaming infrastructure deployment and multi-region Kubernetes migration",1,"",...
```

---

## Sourcing: build the candidate list first

`partners find` is retired, and with it the packaged partner plan: cold discovery is
the partner **lane** now, whose queries fan out across every configured backend while
the shared surface plan decides which of a partner's pages to walk. `partners enrich`
fills in one partner you already know about. Both write the same CSV contract.

```bash
# Cold discovery: search every configured backend, keep only attributed hits.
harness-fleet research --lane partner --output partners.csv

# Enrich one partner into a single multi-source dossier row.
harness-fleet partners enrich trace3.com --max-pages 8 --output partner-trace3.csv
```

The attribution rule is what keeps this honest: a hit counts toward a partner only when that
partner's domain or name appears in the URL or the captured text, and a page is only a candidate
when it actually talks about delivering work — so a blog post about *tracing* never becomes a
partner called Trace3.

---

## The 10-Question Revenue Checklist (100 points)

| Question | Pts | True only when |
|---|---|---|
| `q1_billable_delivery` | 15 | they sell project-based delivery (engagement, SOW, implementation, managed service) |
| `q2_stack_delivery` | 15 | delivery of the target technology is evidenced for named clients |
| `q3_delivery_hiring` | 10 | an open requisition on their own ATS board is for delivery work in the stack |
| `q4_client_outcome` | 15 | a named client and a concrete outcome appear, not a capability claim |
| `q5_commercial_scale` | 5 | published commercial terms (minimum project size, rate, headcount) |
| `q6_vendor_alliance` | 10 | a vendor partner tier, certification, or marketplace listing is stated |
| `q7_vertical_focus` | 5 | one vertical has repeat delivery proof |
| `q8_independent_validation` | 15 | a source that is not their own marketing vouches for delivery |
| `q9_published_engineering` | 5 | they publish technical work of their own |
| `q10_growth_signal` | 5 | a dated growth event in the last twelve months |

Tiers: **tier_1** 85–100, **tier_2** 70–84, **tier_3** 50–69, **unfit** below 50.

---

## The 3-Tier Partner Compounding Filter

To filter large candidate sets down to top-tier partners efficiently:

```bash
# Layer 1: Professional services / consultancy screening (1,000 -> 300)
harness-fleet init l1-partner-filter --preset filter
harness-fleet run l1-partner-filter --input directory_hits.csv --run-id l1-partners
harness-fleet export l1-partners --format csv --filter '{"all": [{"field": "passed", "value": true}]}' --output l1_services_survivors.csv

# Layer 2: Ecosystem & tech stack verification (300 -> 80)
harness-fleet init l2-stack-filter --preset filter
harness-fleet run l2-stack-filter --input practice_pages.csv --only-ids l1_services_survivors.csv --run-id l2-partners
harness-fleet export l2-partners --format csv --filter '{"all": [{"field": "passed", "value": true}]}' --output l2_stack_survivors.csv

# Layer 3: Deep qualification & evidence clipping (80 -> 25)
harness-fleet init l3-partner-scoring --preset partner-research
harness-fleet run l3-partner-scoring --input qualified_postings.csv --only-ids l2_stack_survivors.csv --run-id l3-partners
harness-fleet export l3-partners --format csv --sort-by score --desc --top 25 --rank --output ranked_implementation_partners.csv
```
