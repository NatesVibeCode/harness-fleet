"""Clean terminal UI and formatting utilities.

Colour is conditional on a human actually watching. Every escape code in this
module used to be emitted unconditionally, so a redirect, a pipe, or a captured
subprocess got escape sequences in its log — which is what made a board startup
message unreadable in a file. `NO_COLOR` (any non-empty value), a non-tty
stdout, and `TERM=dumb` all disable colour; `FORCE_COLOR` overrides, for piping
output that is meant to keep its colour.
"""
import os
import sys
from contextlib import contextmanager

# Terminal colors
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

_COLOR_NAMES = ("BOLD", "GREEN", "CYAN", "YELLOW", "RED", "DIM", "RESET")
_COLOR_CODES = {
    "BOLD": "\033[1m",
    "GREEN": "\033[32m",
    "CYAN": "\033[36m",
    "YELLOW": "\033[33m",
    "RED": "\033[31m",
    "DIM": "\033[2m",
    "RESET": "\033[0m",
}


def colors_enabled() -> bool:
    """True only when escape codes belong in the output.

    Deliberately conservative: a redirected stream is not a terminal, so it gets
    no colour unless someone asked for it with ``FORCE_COLOR``.
    """
    forced = os.environ.get("FORCE_COLOR")
    if forced:
        return forced not in {"0", "false", "no"}
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # detached or replaced stdout
        return False


def _apply(enable: bool) -> None:
    """Reword the module constants.

    Callers interpolate them into f-strings, so an empty string prints the text
    plainly rather than suppressing it.
    """
    global BOLD, GREEN, CYAN, YELLOW, RED, DIM, RESET
    for name in _COLOR_NAMES:
        globals()[name] = _COLOR_CODES[name] if enable else ""


@contextmanager
def plain():
    """No colour for the duration, whatever the environment says.

    For tests and for anything that renders output destined for a file. The swap
    is module state; every caller reads the constants at call time, so it needs
    no lock and no per-call plumbing.
    """
    module = sys.modules[__name__]
    saved = {name: getattr(module, name) for name in _COLOR_NAMES}
    try:
        _apply(False)
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


_apply(colors_enabled())


def banner():
    art = r"""
  ___               ___ _          _   
 | __| _ ___ ___   | __| |___  ___| |_ 
 | _| '_/ -_) -_)  | _|| / -_)/ -_)  _|
 |_||_| \___|___|  |_| |_\___|\___|\__|
"""
    print(f"{CYAN}{BOLD}{art}{RESET}")
    print(f" {DIM}Coordinated Free & Local LLM Fleet{RESET}\n")

def print_routes_table(routes: list):
    print(f"{BOLD}{'ROUTE ID':<45} {'PROVIDER':<12} {'PRICE STATE':<22} {'STATUS'}{RESET}")
    print("-" * 80)
    for r in routes:
        rid = r["id"]
        prov = r.get("provider", "unknown")
        price_state = r.get("price_state", "unknown")
        price = f"{GREEN}{price_state}{RESET}" if price_state == "price_observed_zero" else f"{YELLOW}{price_state}{RESET}"
        status = f"{GREEN}Active{RESET}" if r.get("enabled") else f"{RED}Disabled{RESET}"
        print(f"{rid:<45} {prov:<12} {price:<31} {status}")
    print()

def print_cooldowns_table(cooldowns: list[dict]):
    if not cooldowns:
        info("No routes are currently cooling down. All routes are available.")
        return
    print(f"\n{BOLD}{'ROUTE ID':<45} {'EXPIRES IN':<14} {'REASON'}{RESET}")
    print("-" * 80)
    for c in cooldowns:
        rid = c["route_id"]
        rem = f"{YELLOW}{c['remaining_seconds']}s{RESET}"
        reason = str(c.get("reason", "Rate limited"))[:40]
        print(f"{rid:<45} {rem:<23} {reason}")
    print()

def print_progress(current: int, total: int, prefix: str = "", suffix: str = ""):
    percent = (current / total) * 100 if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * current // total) if total > 0 else 0
    bar = "=" * filled + "-" * (bar_len - filled)
    sys.stdout.write(f"\r{CYAN}{prefix}{RESET} [{bar}] {percent:5.1f}% ({current}/{total}) {DIM}{suffix}{RESET}")
    sys.stdout.flush()
    if current >= total:
        print()

def success(msg: str):
    print(f"{GREEN}✔ {msg}{RESET}")

def info(msg: str):
    print(f"{CYAN}ℹ {msg}{RESET}")

def warn(msg: str):
    print(f"{YELLOW}▲ {msg}{RESET}")

def error(msg: str):
    print(f"{RED}✖ {msg}{RESET}")

def print_sessions_table(sessions: dict):
    print(f"\n{BOLD}{'SESSION ID':<22} {'WORKER':<8} {'ROUTE':<40} {'ITEMS':<8} {'STATUS'}{RESET}")
    print("-" * 90)
    for sid, s in sessions.items():
        w_id = f"#{s.get('worker_idx', 1)}"
        route = s.get('route_id', 'unknown')[:38]
        items = s.get('items_completed', 0)
        status_raw = s.get('status', 'active')
        status = f"{GREEN}{status_raw}{RESET}" if status_raw == "completed" else f"{CYAN}{status_raw}{RESET}"
        print(f"{sid:<22} {w_id:<8} {route:<40} {items:<8} {status}")
    print()

def print_status_dashboard(report):
    status_color = GREEN if report.status == "completed" else (CYAN if report.status == "running" else YELLOW)
    print(f"\n{BOLD}Run:{RESET} {report.run_id} ({BOLD}Task:{RESET} {report.task_name}) - [{status_color}{report.status.upper()}{RESET}]")
    
    items_pct = (report.verified_items / report.total_items * 100) if report.total_items > 0 else 0
    print(f"{BOLD}Progress:{RESET} {report.verified_items}/{report.total_items} items ({items_pct:.1f}%)")
    
    b = report.batches
    print(f"{BOLD}Batches:{RESET}  {GREEN}{b.verified} verified{RESET} | {CYAN}{b.leased} leased{RESET} | {YELLOW}{b.pending} pending{RESET} | {RED}{b.failed} failed{RESET} (total: {b.total})")
    print(f"{BOLD}Attempts:{RESET} {report.attempts_used} / {report.max_attempts} budget used ({report.rate_limits_encountered} rate-limit 429s absorbed)")
    
    if report.routes:
        print(f"\n{BOLD}{'ROUTE ID':<42} {'PROVIDER':<12} {'VERIFIED/ATTEMPTS':<18} {'PASS RATE':<11} {'429s':<6} {'LATENCY'}{RESET}")
        print("-" * 96)
        for r in report.routes:
            rate_str = f"{r.success_rate * 100:.1f}%"
            ratio = f"{r.verified}/{r.attempts}"
            lat_str = f"{r.avg_latency_seconds:.2f}s"
            print(f"{r.route_id:<42} {r.provider:<12} {ratio:<18} {rate_str:<11} {r.rate_limits:<6} {lat_str}")

    if report.recent_errors:
        print(f"\n{YELLOW}{BOLD}Recent Alerts / Errors:{RESET}")
        for err in report.recent_errors[:4]:
            print(f"  {DIM}•{RESET} {err[:90]}")
    print()

def print_eval_table(report):
    print(f"\n{BOLD}Evaluation Benchmark: Task '{report.task}' ({report.samples} test samples){RESET}")
    print(f"{BOLD}{'ROUTE ID':<42} {'PROVIDER':<12} {'SCORE':<8} {'SCHEMA %':<10} {'GROUND %':<10} {'429s':<6} {'LATENCY'}{RESET}")
    print("-" * 96)
    for r in report.routes:
        score_str = f"{r.composite_score:.3f}"
        schema_pct = f"{r.schema_pass_rate * 100:.1f}%"
        ground_pct = f"{r.grounding_pass_rate * 100:.1f}%"
        score_color = GREEN if r.composite_score >= 0.75 else (YELLOW if r.composite_score >= 0.4 else RED)
        print(f"{r.route_id:<42} {r.provider:<12} {score_color}{score_str:<8}{RESET} {schema_pct:<10} {ground_pct:<10} {r.rate_limit_count:<6} {r.avg_latency_seconds:.2f}s")
    print()
