# Agent Instructions

Repo-local instructions for AI coding agents working in this checkout. User-level
doctrine defers repo commands and validation to this file, so follow it.

## Verifying the studio UI (screenshots, JS errors) — use playwright, never raw Chrome

**Never launch Google Chrome or Chromium from the sandboxed bash tool** — headless or
otherwise, with any flag combination. Under the file sandbox, Chrome's crashpad/keychain
init is denied (`Operation not permitted` writing the Crashpad database, `mach_vm_read`
failures). The browser process starts, does its work, but **never exits**: the pipeline
never sees EOF, the tool call shows `(no output)` and is killed at the timeout
(`[timed out after 120000ms]`). The SIGTERM kills the shell pipeline but not Chrome's
process tree, and the surviving processes plus the reused `--user-data-dir` profile lock
make every subsequent attempt wedge the same way. Four consecutive attempts failed
exactly like this on 2026-09-14 before switching to playwright. The screenshot itself
is not the problem — Chrome even wrote it after the tool had given up.

**Use playwright instead.** Proven working: screenshots in seconds, zero JS errors.

```bash
# One-time setup (the venv lives in /tmp and may be gone after a reboot)
python3 -m venv /tmp/hf-venv
/tmp/hf-venv/bin/pip install playwright
/tmp/hf-venv/bin/playwright install chromium   # only if no browser is cached yet
```

```python
# run with /tmp/hf-venv/bin/python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8099/", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.screenshot(path="/tmp/studio-top.png", full_page=True)
    browser.close()
print("JS errors:", errors or "none")
```

Notes:

- Confirm the server first: `curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:8099/`.
  Start it with `scripts/studio_preview.sh` as a background job if it is not running.
- Do not pipe browser/tool output through `grep` filters that can swallow everything —
  that is what turned a recoverable error into an opaque `(no output)`.
- If an installer's cache is blocked by the sandbox, retry once with approved escalation;
  do not "solve" it by going back to raw Chrome.

## Long-running commands and timeouts

- Full-suite durations: harness-fleet ≈ 25–45 s, account-fleet ≈ 50 s, career-fleet
  ≈ 60 s. Pass an explicit `timeoutMs: 180000` for any pytest run, or run it in the
  background. A 60 s cap already timed out once on career-fleet (which takes ~59 s).
- Never chain multiple test suites into a single call under a small timeout; run them
  one per call.
- `uv`/`uvx` fail instantly under the workspace sandbox (`~/.cache/uv` is outside it).
  Use plain `python3 -m pytest` and repo scripts; treat `uvx ruff` / `uvx mypy` as
  escalation-required rather than broken.
- `python3 scripts/check_harness_drift.py` is fast (~2 s) and safe to run anytime.

## Coordinate by the drift check, not by a lock

The engine ships as three checkouts that must agree — this one, `../account-fleet`, and
`career-fleet/` — and `scripts/check_harness_drift.py` compares an allowlist of files
byte-for-byte across all three. There is no lock file, no claim branch, and no journal: the
check is the coordination.

**A shared-file change is not landed until `check_harness_drift.py` exits 0 across all three
checkouts.** Landing in the baseline alone is drift, not progress. Propagation is manual, and
that manual step is the one that has failed before — when a feature lands here and reaches two
of the three checkouts, the tree is red until someone finishes the job.

Two things worth knowing before you edit a shared file:

- **`account-fleet` is a git worktree of this repo**, so the two share `.git` and
  `refs/heads/`. A branch you create here also exists there; do not read that as a peer's work.
- Run the check from the engine, and again after propagating: `python3 scripts/check_harness_drift.py`
  (~2 s). If you only touched one checkout, expect it to fail — that is the signal, not a bug.
