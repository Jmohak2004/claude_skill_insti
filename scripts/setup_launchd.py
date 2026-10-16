#!/usr/bin/env python3
"""Schedule Deadline Radar on macOS with launchd.

  setup_launchd.py                  write the plists and show them (no changes to launchd)
  setup_launchd.py --install        write + load them
  setup_launchd.py --uninstall      unload + remove them
  options: --scan-hours 3  --check-minutes 60  --digest-hour 8

Jobs:
  com.deadlineradar.scan      every N hours: fetch Gmail, Claude extracts, instincts
  com.deadlineradar.instincts every M minutes: alerts only (no Claude, no cost)
  com.deadlineradar.digest    daily morning summary
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import RADAR_HOME, find_claude  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
AGENTS = Path.home() / "Library" / "LaunchAgents"
LABELS = ["com.deadlineradar.scan", "com.deadlineradar.instincts", "com.deadlineradar.digest"]


def python_bin():
    venv = RADAR_HOME / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else shutil.which("python3")


def build(args):
    # run_scan.sh re-resolves the CLI each run (the VS Code extension's copy moves on update);
    # this is just an early warning.
    claude = find_claude()
    if not claude:
        print("warning: Claude Code CLI not found; the scan job won't be able to extract.", file=sys.stderr)
    path_dirs = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
    if shutil.which("claude"):
        path_dirs.insert(0, str(Path(shutil.which("claude")).parent))
    env = {"PATH": ":".join(path_dirs), "DEADLINE_RADAR_HOME": str(RADAR_HOME)}
    logs = RADAR_HOME / "logs"
    py = python_bin()

    def job(label, program, **schedule):
        return label, {
            "Label": label,
            "ProgramArguments": program,
            "EnvironmentVariables": env,
            "WorkingDirectory": str(RADAR_HOME),
            "StandardOutPath": str(logs / f"{label.split('.')[-1]}.log"),
            "StandardErrorPath": str(logs / f"{label.split('.')[-1]}.log"),
            "RunAtLoad": False,
            **schedule,
        }

    return [
        job(LABELS[0], ["/bin/bash", str(SCRIPTS / "run_scan.sh")], StartInterval=args.scan_hours * 3600),
        job(LABELS[1], [py, str(SCRIPTS / "instincts.py")], StartInterval=args.check_minutes * 60),
        job(LABELS[2], [py, str(SCRIPTS / "instincts.py"), "--digest"],
            StartCalendarInterval={"Hour": args.digest_hour, "Minute": 0}),
    ]


def launchctl(*cmd):
    subprocess.run(["launchctl", *cmd], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--scan-hours", type=int, default=3)
    parser.add_argument("--check-minutes", type=int, default=60)
    parser.add_argument("--digest-hour", type=int, default=8)
    args = parser.parse_args()
    domain = f"gui/{os.getuid()}"

    if args.uninstall:
        for label in LABELS:
            plist = AGENTS / f"{label}.plist"
            launchctl("bootout", domain, str(plist))
            plist.unlink(missing_ok=True)
        print("Removed Deadline Radar schedules.")
        return

    (RADAR_HOME / "logs").mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    for label, data in build(args):
        plist = AGENTS / f"{label}.plist"
        if args.install:
            launchctl("bootout", domain, str(plist))  # reload cleanly if it already existed
        with open(plist, "wb") as fh:
            plistlib.dump(data, fh)
        if args.install:
            launchctl("bootstrap", domain, str(plist))
        print(f"{'Loaded' if args.install else 'Wrote'} {plist}")
    if not args.install:
        print("\nNothing scheduled yet. Re-run with --install to activate.")
    else:
        print(f"\nLogs: {RADAR_HOME / 'logs'}. Run once now: launchctl kickstart {domain}/{LABELS[0]}")


if __name__ == "__main__":
    main()
