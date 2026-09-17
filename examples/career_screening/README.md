# Career Screening Example

A ready-made recipe for screening employers, plus six sample job postings you can run right away.

**What it does:** reads a job posting or company blurb and answers five yes/no questions about it — does it name a real problem you can solve, does it use the tech you want, is there a concrete reason they're hiring, are the leaders technical, and does the work arrangement suit you. It then scores the employer out of 100 and quotes the exact sentences it relied on, so you can check the reasoning yourself instead of trusting a summary.

The sample postings are a deliberate mix: two are strong fits with a named problem and a real stack, one is an in-office dashboard job, and one is a thin AI wrapper chasing growth. Running it should rank them in that order.

---

## Files in This Directory

| File | What it is |
| --- | --- |
| `task.json` | The screening recipe: the five questions and how much each is worth |
| `sample_employers.csv` | Six example postings, with a company name and the posting text |

---

## Try It With the Samples

No accounts, no API keys, no cost — the built-in demo route answers deterministically:

```bash
harness-fleet quickstart --demo --run-id demo-01
```

That scores the tool's own sample data. To score *these* postings instead, point a run at them:

```bash
harness-fleet run examples/career_screening/task.json \
  --input examples/career_screening/sample_employers.csv \
  --run-id careers-01
```

Either way the scored, quote-backed rows land in `runs/<run-id>/clean_packet.csv`.

---

## Score Real Postings

Career is a lane of `harness-fleet`, not a second tool: one CLI, one procedure.

```bash
harness-fleet init career-research --preset career-research   # start from the lane's preset
harness-fleet research --lane career                          # discover, bundle, score, export
harness-fleet lane report careers-01 --lane career            # what one run concluded, row by row
harness-fleet export careers-01 \
  --format csv \
  --sort-by score \
  --desc \
  --top 5 \
  --rank \
  --output ranked_employers.csv
```

---

## Where to Go Next

- **Just want it to work?** Restart Claude Desktop (or Cursor) and ask it to screen employers for you — the bundled career lane skill walks the assistant through these same steps.
- **Tune the questions:** the five questions live in `task.json`; what you are looking for lives in your profile (`harness-fleet profile`), and the score follows automatically.
- **Full walkthrough:** see `skills/career-fleet/SKILL.md`.
