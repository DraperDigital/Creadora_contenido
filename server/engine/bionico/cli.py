"""bionico CLI entry point."""
from __future__ import annotations

import argparse

from bionico import config as _config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bionico", description="Contenido Bionico control.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("skills", help="(re)install the /bionico, /short AI-client skills")
    s = sub.add_parser("start", help="start watcher (+agent when cloud configured), supervised")
    s.add_argument("--detach", "-d", action="store_true", help="start in the background and return")
    sub.add_parser("stop", help="stop all services")
    r = sub.add_parser("restart", help="restart all services")
    r.add_argument("--detach", "-d", action="store_true", help="restart in the background and return")
    sub.add_parser("status", help="show service status")
    p = sub.add_parser("protocol", help="install/disable the local browser launcher")
    p.add_argument("action", choices=["install", "disable", "status"])
    po = sub.add_parser("protocol-open", help=argparse.SUPPRESS)
    po.add_argument("uri", nargs="?")
    a = sub.add_parser("autostart", help="enable/disable login autostart")
    a.add_argument("action", choices=["enable", "disable"])

    args = ap.parse_args(argv)
    cfg = _config.load()

    if args.cmd == "skills":
        from bionico import skills
        skills.install_all(cfg)
        return 0
    if args.cmd == "start":
        from bionico import orchestrator
        orchestrator.start_detached(cfg) if args.detach else orchestrator.start(cfg)
        return 0
    if args.cmd == "stop":
        from bionico import orchestrator
        orchestrator.stop(cfg)
        return 0
    if args.cmd == "restart":
        from bionico import orchestrator
        orchestrator.stop(cfg)
        orchestrator.start_detached(cfg) if args.detach else orchestrator.start(cfg)
        return 0
    if args.cmd == "status":
        from bionico import orchestrator
        orchestrator.status(cfg)
        return 0
    if args.cmd == "protocol":
        from bionico import protocol
        if args.action == "install":
            return 0 if protocol.install(protocol.launcher_path()) else 1
        if args.action == "disable":
            protocol.disable()
            return 0
        return 0 if protocol.status() else 1
    if args.cmd == "protocol-open":
        from bionico import protocol
        return protocol.open_uri(args.uri)
    if args.cmd == "autostart":
        from bionico import autostart
        if args.action == "enable":
            # enable() prints the outcome (mechanism or captured error) itself.
            return 0 if autostart.enable(autostart.launcher_path()) else 1
        autostart.disable()
        print("autostart disabled")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
