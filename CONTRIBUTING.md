# Contributing to harness-fleet

Thank you for your interest in contributing to `harness-fleet`!

## Philosophy
`harness-fleet` keeps its trust boundaries explicit:
1. SQLite owns task revisions, queue leases, attempt budgets, route observations, and receipts.
2. Every harness runs tool-less JSON prompts through its own normally installed CLI, with task-local lockdown where the harness documents one (OpenCode deny-all config; Cursor `--sandbox enabled`). Approval-bypass flags never appear.
3. Only observed-zero routes enter the zero-price ladder.
4. Closed input and output schemas reject undeclared fields.
5. Evidence must match one source slice at exact offsets.

## Development Setup

```bash
git clone https://github.com/NatesVibeCode/harness-fleet.git
cd harness-fleet

# Install dependencies in editable mode
pip install -e ".[dev]"

# Run test suite
pytest -v

# Check the shared harness-fleet contract across the local sibling checkouts
python3 scripts/check_harness_drift.py
```

Lint gates (no `ruff format`, no `--strict`, ever):

```bash
ruff check harness_fleet/ tests/
mypy harness_fleet/
```

There is one install and one script: the `harness-fleet` distribution ships the single `harness-fleet` console script (`uv tool install .` then `harness-fleet --help`). The account, career and partner products are lanes — JSON over the same engine — and their skills ship in this package.

Do not include credentials, customer data, provider responses containing private data, or local machine paths in issues, fixtures, commits, or receipts.

## Adding a New Provider
Providers implement `BaseProvider` in `harness_fleet/providers/base.py` and implement `run_prompt(route_id, prompt, system_prompt, timeout_sec, session_id)`.
All new providers must include token usage, duration, and reported cost telemetry in their receipt dict.
