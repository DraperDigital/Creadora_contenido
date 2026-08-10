#!/usr/bin/env python3
"""Run one LITE edit, then record its result so the watcher can reconcile it.

The watcher spawns this DETACHED (its own session / process group). Because it
is decoupled from the watcher, stopping or restarting the stack
(server + watcher + Syncthing) does not interrupt an edit already in progress.
When the edit finishes it writes `<result_path>` with the CLI exit code, which
a freshly (re)started watcher reads to finalize the job instead of re-running it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Claude subscription usage-limit deferral (sysexits.h EX_TEMPFAIL). The
# pipeline's claude_runner writes the sentinel file named by
# BIONICO_LIMIT_MARKER when the Claude CLI errors with a usage-limit message;
# a nonzero CLI exit with that marker present is mapped to this exit code so
# the watcher requeues WITHOUT consuming a retry and waits for the reset.
EXIT_QUOTA = 75

# Exit codes that indicate a deterministic input/usage error: retrying with
# the exact same arguments can only fail the exact same way, so the watcher
# terminalizes immediately instead of burning a retry. 2 = argparse usage
# error; 64/65/66 = sysexits EX_USAGE / EX_DATAERR / EX_NOINPUT.
PERMANENT_EXIT_CODES = (2, 64, 65, 66)


def run(key: str, args_json: str, pipeline_dir: str, exe: str, result_path: str) -> int:
    rc = 1
    quota = False
    result = Path(result_path)
    limit_marker = result.with_name(result.name + ".limit")
    try:
        limit_marker.parent.mkdir(parents=True, exist_ok=True)
        limit_marker.unlink(missing_ok=True)
    except OSError:
        pass
    # Force UTF-8 in the pipeline child: a cp1252 console default once crashed
    # a FINISHED 26-minute change run while it printed its final summary.
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["BIONICO_LIMIT_MARKER"] = str(limit_marker)
    try:
        cli_args = [str(a) for a in json.loads(args_json)]
        rc = subprocess.run([exe, *cli_args], cwd=pipeline_dir, env=env).returncode
    finally:
        quota = rc != 0 and limit_marker.exists()
        if quota:
            rc = EXIT_QUOTA
        try:
            limit_marker.unlink()
        except OSError:
            pass
        try:
            p = Path(result_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps({"key": key, "rc": rc, "quota": quota,
                            "permanent": rc in PERMANENT_EXIT_CODES,
                            "finished": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8",
            )
        except OSError:
            pass
    return rc


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])
    if len(a) != 5:
        print("usage: jobrunner <key> <args_json> <pipeline_dir> <exe> <result_path>",
              file=sys.stderr)
        return 2
    return run(a[0], a[1], a[2], a[3], a[4])


if __name__ == "__main__":
    raise SystemExit(main())
