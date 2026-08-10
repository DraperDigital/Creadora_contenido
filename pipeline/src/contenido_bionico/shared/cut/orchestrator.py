"""Deterministic cut pipeline orchestrator.

Drives the cut flow from a raw mp4 to the contractual handoff for the
animation pipeline (`runs/<id>/source.mp4` + `runs/<id>/transcript.json`).
All fixed plumbing (transcribe, mapper, EDL build, ffmpeg
cut, re-transcribe) runs as deterministic Python helpers. ElevenLabs Scribe
uses the deterministic mapper.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


HELPERS = Path(__file__).resolve().parents[0]
SHARED = Path(__file__).resolve().parents[2] / "shared"
SRC = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(__file__).resolve().parents[4]
RUNS = REPO_ROOT / "runs"
RUNNER = HELPERS / "runner.py"
CONTRACT_ATTEMPTS = 2
# Cap how many Finalizer calls can repair a mapper-rejected final.txt
# before we declare the run unsalvageable. Each call is one extra Claude
# invocation against a single transcript, so this is a budget knob, not a
# correctness knob. Each Surgical Patcher or Finalizer call is followed by
# a mapper retry, so total mapper invocations can reach
# SURGICAL_PATCHER_MAX_ATTEMPTS + FINALIZER_MAX_ATTEMPTS + 1 (the trailing
# mapper try has no repair step behind it). The Finalizer budget is
# deliberately tight (3): a transcript that three Opus repair calls cannot
# align needs a fresh editor pass, not more repair attempts.
FINALIZER_MAX_ATTEMPTS = 3
SURGICAL_PATCHER_MAX_ATTEMPTS = 8

# Exit codes the repair helpers use to signal "output rejected by the shrink
# guard" (>20% of final.txt deleted). Kept in sync with
# surgical_patcher.SHRINK_GUARD_EXIT_CODE and runner.FINALIZER_SHRINK_EXIT_CODE.
PATCHER_SHRINK_EXIT_CODE = 32
FINALIZER_SHRINK_EXIT_CODE = 43

# Kept-text floor: fail the cut attempt (with feedback to the editor) when
# final.txt keeps fewer than this fraction of the verbose transcript's
# alignment units. Guards against the "every validator passed but the edit
# deleted most of the transcript" failure mode (a past incident deleted ~90%
# of a transcript and shipped). Env-overridable; 0 disables the check.
MIN_KEEP_RATIO_ENV = "BIONICO_MIN_KEEP_RATIO"
DEFAULT_MIN_KEEP_RATIO = 0.35

# Ensure `from contenido_bionico...` resolves when this file is invoked as a script.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from contenido_bionico.shared.ffmpeg import NO_WINDOW  # noqa: E402


class PrepOrchestratorError(RuntimeError):
    pass


class ShrinkGuardEscalation(PrepOrchestratorError):
    """A repair step (Surgical Patcher / Finalizer) wanted to delete more
    than the allowed fraction of final.txt. The repair was rejected; the cut
    attempt must be retried through the editor-loop recovery path (fresh
    editor-reviewer loop with contract feedback), never by accepting the
    oversized deletion."""


def run_step(label: str, cmd: list[str]) -> None:
    print(f"STEP: {label}", flush=True)
    print("CMD: " + " ".join(cmd), flush=True)
    print(f"Run cut: {label} starting", flush=True)
    # Stream the subprocess output line by line instead of buffering until
    # exit. The editor-reviewer loop can run for hours and emits heartbeat
    # lines the operator needs to see live; buffering hid them until the
    # step finished. stderr is merged into stdout so lines keep their
    # relative order.
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    output_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        output_lines.append(line)
        print(line, end="", flush=True)
    returncode = proc.wait()
    if returncode != 0:
        print(f"Run cut: {label} FAILED exit={returncode}", flush=True)
        last_line = next(
            (ln.strip() for ln in reversed(output_lines) if ln.strip()), ""
        )
        error = PrepOrchestratorError(
            f"{label} failed with exit code {returncode}"
            + (f" | last output: {last_line[:300]}" if last_line else "")
        )
        # Callers that need to react to a SPECIFIC child exit code (e.g. the
        # Finalizer shrink guard) read it from the exception.
        error.returncode = returncode
        raise error
    print(f"Run cut: {label} done", flush=True)


def require_file(path: Path, label: str) -> None:
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        raise PrepOrchestratorError(f"Missing or empty {label}: {path}")


def prepare_folders(video_id: int) -> tuple[Path, Path]:
    run_dir = RUNS / str(video_id)
    intermediates = run_dir / "_intermediates"
    for path in (
        run_dir,
        intermediates,
        intermediates / "editor",
        intermediates / "reviewer",
        run_dir / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return run_dir, intermediates


def write_verbose_and_word_for_word(intermediates: Path) -> None:
    raw = intermediates / "raw_transcript.json"
    require_file(raw, "raw transcript")
    data = json.loads(raw.read_text(encoding="utf-8-sig"))
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PrepOrchestratorError(f"raw transcript has no non-empty text field: {raw}")
    (intermediates / "verbose.txt").write_text(text, encoding="utf-8")
    shutil.copyfile(raw, intermediates / "word_for_word.json")


def verify_contract(run_dir: Path) -> None:
    source = run_dir / "source.mp4"
    transcript = run_dir / "transcript.json"
    require_file(source, "source.mp4")
    require_file(transcript, "transcript.json")
    data = json.loads(transcript.read_text(encoding="utf-8-sig"))
    words = data.get("words")
    if not isinstance(words, list) or not words:
        raise PrepOrchestratorError(f"transcript.json has no non-empty words[]: {transcript}")


def remove_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        raise PrepOrchestratorError(f"Could not remove generated file: {path} ({exc})") from exc


def reset_cut_attempt(intermediates: Path, run_dir: Path) -> None:
    """Remove generated cut outputs before a contract-recovery retry."""
    for path in (
        intermediates / "final.txt",
        intermediates / "edl.json",
        intermediates / "edl_trimmed.json",
        intermediates / "edl_refined.json",
        intermediates / "edl_render.json",
        run_dir / "source.mp4",
        run_dir / "transcript.json",
    ):
        remove_if_exists(path)
    for folder in (intermediates / "editor", intermediates / "reviewer"):
        if not folder.exists():
            continue
        for child in folder.iterdir():
            if child.is_file():
                remove_if_exists(child)


def run_cut_contract_validator(run_dir: Path) -> tuple[bool, list[str]]:
    verify_path = HELPERS / "verify_contract.py"
    if not verify_path.exists():
        return True, []
    report_path = run_dir / "logs" / "Prep_Contract_Report.json"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(verify_path),
                "--run-dir",
                str(run_dir),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        # Convert to the normal contract-failure result so the recovery
        # path handles it instead of crashing the orchestrator.
        return False, ["verify_contract.py timed out after 120 seconds"]
    warnings: list[str] = []
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            raw_warnings = report.get("warnings") or []
            warnings = [str(item) for item in raw_warnings if str(item).strip()]
        except (json.JSONDecodeError, OSError):
            warnings = []
    if proc.returncode == 0:
        return True, []
    if not warnings:
        text = (proc.stderr or proc.stdout or "").strip()
        warnings = [text[:800] if text else f"verify_contract.py failed exit={proc.returncode}"]
    return False, warnings


def write_contract_feedback(intermediates: Path, attempt: int, warnings: list[str]) -> None:
    feedback = [
        f"Cut contract failed after attempt {attempt}.",
        "The next editor pass must keep only transcript text that can be cut cleanly.",
        "Validator feedback:",
    ]
    feedback.extend(f"- {warning}" for warning in warnings)
    (intermediates / "contract_feedback.txt").write_text(
        "\n".join(feedback) + "\n",
        encoding="utf-8",
    )


def _min_keep_ratio() -> float:
    raw = os.environ.get(MIN_KEEP_RATIO_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if 0.0 <= value <= 1.0:
                return value
        except ValueError:
            pass
    return DEFAULT_MIN_KEEP_RATIO


def check_keep_ratio(intermediates: Path) -> tuple[bool, list[str]]:
    """Kept-text floor, checked right after mapping succeeds.

    Compares final.txt against verbose.txt using the mapper's own
    tokenization (alignment units), so it measures exactly what the mapper
    aligned. Returns (True, []) when the ratio is at or above the floor, and
    (False, [feedback]) when the edit deleted too much — the caller routes
    the feedback to a fresh editor-reviewer loop instead of crashing.
    """
    floor = _min_keep_ratio()
    if floor <= 0.0:
        return True, []
    try:
        verbose_text = (intermediates / "verbose.txt").read_text(encoding="utf-8-sig")
        final_text = (intermediates / "final.txt").read_text(encoding="utf-8-sig")
    except OSError:
        # Nothing to compare — missing artifacts are caught by the other
        # validators with clearer messages.
        return True, []
    # Local import: mapper pulls in numpy, which the orchestrator does not
    # otherwise need at import time.
    from contenido_bionico.shared.cut.mapper import (
        final_alignment_units,
        tokenize_final,
    )
    verbose_units = len(final_alignment_units(tokenize_final(verbose_text)))
    final_units = len(final_alignment_units(tokenize_final(final_text)))
    if verbose_units <= 0:
        return True, []
    ratio = final_units / verbose_units
    if ratio >= floor:
        return True, []
    return False, [
        (
            f"kept-text floor: final.txt keeps only {final_units} of "
            f"{verbose_units} alignment units ({ratio:.0%}), below the minimum "
            f"{floor:.0%} ({MIN_KEEP_RATIO_ENV}). The edit deleted too much of "
            "the source transcript. Produce a new deletion-only edit that keeps "
            "the speaker's full committed takes — remove only retakes, "
            "stumbles, and abandoned phrases, never whole sections of real "
            "content."
        )
    ]


def log_contract_recovery(run_dir: Path, attempt: int, warnings: list[str]) -> None:
    log_path = run_dir / "logs" / "Prep_Contract_Recovery.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(f"Attempt {attempt} failed cut contract validation.\n")
        for warning in warnings:
            fp.write(f"- {warning}\n")
        fp.write("\n")


def _snapshot_mapper_error_logs(intermediates: Path) -> set[str]:
    """Names of every mapper error log on disk RIGHT NOW.

    Captured just before each mapper invocation so the orchestrator can
    later compute the delta and identify the log THIS invocation wrote —
    instead of picking up a stale log from a previous attempt.
    """
    logs = intermediates / "logs"
    if not logs.is_dir():
        return set()
    return {p.name for p in logs.glob("mapper_*_error.log")}


def find_new_mapper_error_log(
    intermediates: Path, baseline: set[str]
) -> Path | None:
    """Return the mapper error log written SINCE `baseline` was snapshotted.

    Picks the newest by filename (filenames embed a sortable timestamp).
    Returns None if no new log appeared — that's the signal that the
    mapper crashed without writing diagnostics, and the orchestrator
    should refuse to feed the Finalizer stale input.
    """
    logs = intermediates / "logs"
    if not logs.is_dir():
        return None
    current = {p.name for p in logs.glob("mapper_*_error.log")}
    new_names = sorted(current - baseline)
    if not new_names:
        return None
    return logs / new_names[-1]


def _mapper_recovery_log_path(intermediates: Path) -> Path:
    return intermediates / "logs" / "Mapper_Recovery.log"


def _append_recovery_log(intermediates: Path, line: str) -> None:
    """Append a timestamped event to `_intermediates/logs/Mapper_Recovery.log`.

    Single chronological view of the mapper / finalizer recovery cycle:
    each mapper attempt, its outcome, each Finalizer call, and the
    references to the per-step error logs and input/diff dumps. Lets
    `tail -f Mapper_Recovery.log` show the full state of the loop without
    grepping across multiple agent-call files.
    """
    log_path = _mapper_recovery_log_path(intermediates)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(f"[{stamp}] {line}\n")


def _summarize_mapper_error(mapper_log: Path | None) -> str:
    """One-line headline pulled from a mapper error log.

    Picks the first `Error message:` line, falling back to the
    `Error type:` line, falling back to the basename. Bounded length so
    the recovery log stays scannable.
    """
    if mapper_log is None or not mapper_log.is_file():
        return "no mapper error log available"
    try:
        text = mapper_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return mapper_log.name
    headline = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Error message:"):
            headline = stripped[len("Error message:"):].strip()
            break
        if stripped.startswith("Error type:") and not headline:
            headline = stripped[len("Error type:"):].strip()
    if not headline:
        headline = mapper_log.name
    return headline[:240] + ("…" if len(headline) > 240 else "")


def _try_surgical_patcher(
    *,
    video_id: int,
    intermediates: Path,
    mapper_log: Path,
    patcher_attempt: int,
) -> bool:
    """Run the deterministic surgical patcher once.

    Returns True only when `final.txt` changed. A zero exit with no change still
    falls through to the Finalizer because the patcher may have parsed the log
    but found no safely removable spans. Raises ShrinkGuardEscalation when the
    patcher rejected its own output for deleting >20% of final.txt — that case
    must go back to the editor loop, not to the Finalizer.
    """
    final_path = intermediates / "final.txt"
    try:
        before = final_path.read_text(encoding="utf-8")
    except OSError as exc:
        _append_recovery_log(
            intermediates,
            f"SURGICAL_PATCHER call={patcher_attempt} | skipped | could not read final.txt: {exc}",
        )
        return False

    cmd = [
        sys.executable,
        str(HELPERS / "surgical_patcher.py"),
        "--video-id",
        str(video_id),
        "--mapper-error",
        str(mapper_log),
    ]
    print(
        (
            f"Run {video_id}: mapper failed; trying Surgical Patcher "
            f"(attempt {patcher_attempt} of {SURGICAL_PATCHER_MAX_ATTEMPTS})"
        ),
        flush=True,
    )
    _append_recovery_log(
        intermediates,
        (
            f"SURGICAL_PATCHER call={patcher_attempt}/{SURGICAL_PATCHER_MAX_ATTEMPTS} "
            f"| starting | mapper_log={mapper_log.name}"
        ),
    )
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if proc.stdout:
        print(proc.stdout, flush=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, flush=True)
    if proc.returncode == PATCHER_SHRINK_EXIT_CODE:
        # The patch would have deleted >20% of final.txt. Do NOT fall back
        # to the Finalizer (it would attempt the same oversized deletion) —
        # escalate to the editor-loop recovery path.
        _append_recovery_log(
            intermediates,
            (
                f"SURGICAL_PATCHER call={patcher_attempt}/{SURGICAL_PATCHER_MAX_ATTEMPTS} "
                "| REJECTED by shrink guard (>20% of final.txt); escalating to "
                "editor-loop recovery"
            ),
        )
        raise ShrinkGuardEscalation(
            "Repairing the mapper issues required deleting more than 20% of "
            "the approved transcript, so the repair was rejected. The kept "
            "text diverges too much from the source word-for-word transcript; "
            "a new deletion-only edit is needed that keeps only text that "
            "aligns cleanly against the source."
        )
    if proc.returncode != 0:
        _append_recovery_log(
            intermediates,
            (
                f"SURGICAL_PATCHER call={patcher_attempt}/{SURGICAL_PATCHER_MAX_ATTEMPTS} "
                f"| failed exit={proc.returncode}; falling back to Finalizer"
            ),
        )
        return False

    try:
        after = final_path.read_text(encoding="utf-8")
    except OSError as exc:
        _append_recovery_log(
            intermediates,
            (
                f"SURGICAL_PATCHER call={patcher_attempt}/{SURGICAL_PATCHER_MAX_ATTEMPTS} "
                f"| could not verify final.txt after patch: {exc}; falling back to Finalizer"
            ),
        )
        return False
    if after == before:
        _append_recovery_log(
            intermediates,
            (
                f"SURGICAL_PATCHER call={patcher_attempt}/{SURGICAL_PATCHER_MAX_ATTEMPTS} "
                "| no final.txt change; falling back to Finalizer"
            ),
        )
        return False

    patcher_dir = intermediates / "patcher"
    latest_patch = (
        max(
            patcher_dir.glob("patch_*.txt"),
            key=lambda p: p.stat().st_mtime,
            default=None,
        )
        if patcher_dir.is_dir()
        else None
    )
    latest_diff = latest_patch.with_suffix(".diff") if latest_patch is not None else None
    latest_report = latest_patch.with_suffix(".report.txt") if latest_patch is not None else None
    _append_recovery_log(
        intermediates,
        (
            f"SURGICAL_PATCHER call={patcher_attempt}/{SURGICAL_PATCHER_MAX_ATTEMPTS} "
            "| changed final.txt; retrying mapper"
            + (f" | output={latest_patch.name}" if latest_patch is not None else "")
            + (
                f" | diff={latest_diff.name}"
                if latest_diff is not None and latest_diff.exists()
                else ""
            )
            + (
                f" | report={latest_report.name}"
                if latest_report is not None and latest_report.exists()
                else ""
            )
        ),
    )
    return True


def run_mapper_with_finalizer_recovery(
    video_id: int, intermediates: Path
) -> None:
    """Run the mapper; on failure, hand the diagnostics to the Finalizer
    agent to patch `final.txt` and retry.

    The editor-reviewer loop is NOT re-entered. The Surgical Patcher gets the
    first recovery attempt because it is deterministic and narrowly scoped.
    The Finalizer is reserved as the last-resort repair agent once the patcher
    cannot make a safe change.
    """
    finalizer_calls = 0
    patcher_calls = 0
    mapper_attempt = 0
    _append_recovery_log(
        intermediates,
        (
            f"START run={video_id} | budget: {SURGICAL_PATCHER_MAX_ATTEMPTS} "
            f"Surgical Patcher + {FINALIZER_MAX_ATTEMPTS} Finalizer call(s) "
            f"over <= {SURGICAL_PATCHER_MAX_ATTEMPTS + FINALIZER_MAX_ATTEMPTS + 1} "
            f"mapper attempts"
        ),
    )
    while True:
        mapper_attempt += 1
        # Snapshot existing error logs BEFORE the mapper runs so we can
        # later identify the log THIS invocation produced — never reuse a
        # stale log from a previous attempt as Finalizer input (that's
        # what caused the iter 5 catastrophe: stale 16-issue log fed back
        # to a Finalizer whose final.txt had already addressed those
        # issues, so it panicked and deleted ~90% of the transcript).
        log_baseline = _snapshot_mapper_error_logs(intermediates)
        _append_recovery_log(
            intermediates,
            f"MAPPER attempt={mapper_attempt} | starting",
        )
        try:
            run_step(
                "map cleaned transcript to EDL",
                [
                    sys.executable,
                    str(HELPERS / "mapper.py"),
                    str(intermediates / "final.txt"),
                    str(intermediates / "word_for_word.json"),
                    "-o",
                    str(intermediates / "edl.json"),
                ],
            )
            _append_recovery_log(
                intermediates,
                (
                    f"MAPPER attempt={mapper_attempt} | OK "
                    f"(after {finalizer_calls} Finalizer call(s))"
                ),
            )
            return
        except PrepOrchestratorError:
            mapper_log = find_new_mapper_error_log(intermediates, log_baseline)
            error_headline = _summarize_mapper_error(mapper_log)
            _append_recovery_log(
                intermediates,
                (
                    f"MAPPER attempt={mapper_attempt} | FAILED | "
                    f"log={mapper_log.name if mapper_log else 'n/a'} | "
                    f"{error_headline}"
                ),
            )
            if finalizer_calls >= FINALIZER_MAX_ATTEMPTS:
                _append_recovery_log(
                    intermediates,
                    (
                        f"END run={video_id} | BUDGET EXHAUSTED after "
                        f"{finalizer_calls} Finalizer call(s); propagating "
                        f"mapper failure"
                    ),
                )
                raise
            if mapper_log is None:
                # No diagnostics to feed the Finalizer — bail with the
                # original failure rather than spinning without input.
                _append_recovery_log(
                    intermediates,
                    f"END run={video_id} | no mapper error log to feed Finalizer",
                )
                raise
            if patcher_calls < SURGICAL_PATCHER_MAX_ATTEMPTS:
                patcher_calls += 1
                if _try_surgical_patcher(
                    video_id=video_id,
                    intermediates=intermediates,
                    mapper_log=mapper_log,
                    patcher_attempt=patcher_calls,
                ):
                    continue
            else:
                _append_recovery_log(
                    intermediates,
                    (
                        f"SURGICAL_PATCHER budget exhausted after {patcher_calls} "
                        "call(s); falling back to Finalizer"
                    ),
                )
            finalizer_calls += 1
            print(
                (
                    f"Run {video_id}: mapper failed; routing diagnostics to "
                    f"Finalizer (attempt {finalizer_calls} of "
                    f"{FINALIZER_MAX_ATTEMPTS})"
                ),
                flush=True,
            )
            _append_recovery_log(
                intermediates,
                (
                    f"FINALIZER call={finalizer_calls}/{FINALIZER_MAX_ATTEMPTS} "
                    f"| starting | mapper_log={mapper_log.name}"
                ),
            )
            try:
                run_step(
                    "finalizer fix after mapper failure",
                    [
                        sys.executable,
                        str(RUNNER),
                        "finalizer-fix",
                        "--video-id",
                        str(video_id),
                        "--mapper-error",
                        str(mapper_log),
                    ],
                )
            except PrepOrchestratorError as exc:
                if getattr(exc, "returncode", None) == FINALIZER_SHRINK_EXIT_CODE:
                    # The Finalizer's fix would have deleted >20% of
                    # final.txt; the runner refused to write it. Escalate to
                    # the editor-loop recovery path instead of retrying.
                    _append_recovery_log(
                        intermediates,
                        (
                            f"FINALIZER call={finalizer_calls}/{FINALIZER_MAX_ATTEMPTS} "
                            "| REJECTED by shrink guard (>20% of final.txt); "
                            "escalating to editor-loop recovery"
                        ),
                    )
                    raise ShrinkGuardEscalation(
                        "The Finalizer's repair required deleting more than 20% "
                        "of the approved transcript, so it was rejected. The "
                        "kept text diverges too much from the source "
                        "word-for-word transcript; a new deletion-only edit is "
                        "needed that keeps only text that aligns cleanly "
                        "against the source."
                    ) from exc
                raise
            # Find the latest finalizer artifacts (input dump / diff /
            # output) and record their paths so the recovery log is a
            # single index into the rest of the per-call debugging files.
            finalizer_dir = intermediates / "finalizer"
            latest_fix = (
                max(
                    finalizer_dir.glob("fix_*.txt"),
                    key=lambda p: p.stat().st_mtime,
                    default=None,
                )
                if finalizer_dir.is_dir()
                else None
            )
            latest_diff = (
                latest_fix.with_suffix(".diff")
                if latest_fix is not None
                else None
            )
            latest_input = (
                latest_fix.with_suffix(".input.md")
                if latest_fix is not None
                else None
            )
            _append_recovery_log(
                intermediates,
                (
                    f"FINALIZER call={finalizer_calls}/{FINALIZER_MAX_ATTEMPTS} "
                    f"| done"
                    + (
                        f" | output={latest_fix.name}"
                        if latest_fix is not None
                        else ""
                    )
                    + (
                        f" | diff={latest_diff.name}"
                        if latest_diff is not None and latest_diff.exists()
                        else ""
                    )
                    + (
                        f" | input={latest_input.name}"
                        if latest_input is not None and latest_input.exists()
                        else ""
                    )
                ),
            )


def reap_sidecars() -> None:
    helper = HELPERS / "reap_sidecars.py"
    if not helper.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(helper)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=NO_WINDOW,
        )
    except Exception:
        pass


def _keep_leading_silence(edl_render_path: Path) -> None:
    """Extend the first render range back to t=0 (keep-silences mode only).

    The render EDL only covers word-bounded ranges, so the head before the first
    spoken word -- the silent intro where the ranking's AI narration plays -- is
    dropped. The region before the first word is non-speech, so pulling range 0's
    start to 0 keeps exactly that silent opening (and nothing the editor cut,
    which is always at or after the first word). Only called in keep_silences
    (ranking) mode; every other format renders unchanged.
    """
    data = json.loads(edl_render_path.read_text(encoding="utf-8-sig"))
    ranges = data.get("ranges") or []
    if ranges and float(ranges[0].get("start", 0.0)) > 0.0:
        ranges[0]["start"] = 0.0
        edl_render_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def run_cut(
    video_id: int,
    raw_mp4: Path,
    *,
    keep_silences: bool = False,
    comment_text: str | None = None,
) -> None:
    if not raw_mp4.exists():
        raise PrepOrchestratorError(f"Raw MP4 does not exist: {raw_mp4}")
    run_dir, intermediates = prepare_folders(video_id)
    print(f"Run {video_id}: cut orchestrator starting", flush=True)
    # Comment mode: hand the viewer comment to the editor/reviewer as CONTEXT
    # so they judge the answer body as a standalone reply to it. Written before
    # the loop so runner.py picks it up by path existence. Context only --
    # never inserted into the transcript (the deletion-only edit rule stays
    # intact).
    comment = (comment_text or "").strip()
    comment_mode = bool(comment)
    if comment_mode:
        (intermediates / "comment.txt").write_text(comment, encoding="utf-8")
    _run_cut_body(
        video_id,
        raw_mp4,
        intermediates,
        run_dir,
        keep_silences=keep_silences,
        comment_mode=comment_mode,
    )


def _run_cut_body(
    video_id: int,
    raw_mp4: Path,
    intermediates: Path,
    run_dir: Path,
    *,
    keep_silences: bool = False,
    comment_mode: bool = False,
) -> None:
    """Shared cut body: de-silence -> transcribe -> editor/reviewer loop ->
    mapper recovery -> render EDL -> source.mp4 -> derive transcript.json.

    Split out of `run_cut` so `recut` can drive the SAME deterministic flow
    without duplicating it. Every heavy step is reuse-guarded by on-disk
    artifacts (existing `desilenced.mp4` / `raw_transcript.json` are reused),
    so a re-cut that keeps `word_for_word.json` re-runs only the editor loop
    and the deterministic tail, never re-transcribing. The caller has already
    prepared the run folders and (for comment mode) written `comment.txt`."""
    try:
        desilenced = run_dir / "desilenced.mp4"
        if keep_silences or comment_mode:
            # Ranking keeps every deliberate pause (the TTS narration is mixed
            # into them later) and comment mode keeps the recorded leading
            # silence (the comment voiceover plays over it). In both modes the
            # real de-silence runs afterwards, on the voiceover+video mix, so
            # only non-speech / non-TTS silence is trimmed. There is therefore
            # NO front de-silence here -- the cut renders straight from
            # raw.mp4.
            print(
                f"Run {video_id}: keeping silences (cut renders from raw.mp4)",
                flush=True,
            )
        else:
            # Front-of-pipeline: cap long silences on the VIDEO once, up front, so
            # the ENTIRE cut+animate flow runs on a single de-silenced working video.
            # The original is only read to build it; transcription, silence
            # detection and the final render all operate on this de-silenced video,
            # so every timeline is native and consistent (no remap step).
            if desilenced.exists() and desilenced.stat().st_size > 0:
                print(f"Run {video_id}: reusing existing de-silenced video", flush=True)
            else:
                run_step(
                    "de-silence video",
                    [
                        sys.executable,
                        str(SHARED / "desilence.py"),
                        str(raw_mp4),
                        "-o",
                        str(desilenced),
                        "--cap-ms", "120",
                    ],
                )
            raw_mp4 = desilenced

        raw_transcript = intermediates / "raw_transcript.json"
        if raw_transcript.exists() and raw_transcript.stat().st_size > 0:
            print(f"Run {video_id}: reusing existing raw transcript", flush=True)
        else:
            run_step(
                "transcribe raw MP4",
                [
                    sys.executable,
                    str(HELPERS / "transcribe.py"),
                    str(raw_mp4),
                    "-o",
                    str(raw_transcript),
                ],
            )
        write_verbose_and_word_for_word(intermediates)
        print(f"Run {video_id}: verbose.txt and word_for_word.json ready", flush=True)
        for attempt in range(1, CONTRACT_ATTEMPTS + 1):
            if attempt > 1:
                print(
                    f"Run {video_id}: rebuilding cut contract attempt {attempt}/{CONTRACT_ATTEMPTS}",
                    flush=True,
                )
                reset_cut_attempt(intermediates, run_dir)
            run_step(
                "editor reviewer loop",
                [
                    sys.executable,
                    str(RUNNER),
                    "editor-reviewer-loop",
                    "--video-id",
                    str(video_id),
                    "--max-iterations",
                    "8",
                ],
            )
            try:
                run_mapper_with_finalizer_recovery(video_id, intermediates)
            except ShrinkGuardEscalation as exc:
                # A repair step wanted to delete >20% of final.txt. Route the
                # failure to the editor-loop recovery path (fresh loop with
                # contract feedback) instead of accepting or crashing.
                shrink_warnings = [str(exc)]
                log_contract_recovery(run_dir, attempt, shrink_warnings)
                if attempt >= CONTRACT_ATTEMPTS:
                    raise
                write_contract_feedback(intermediates, attempt, shrink_warnings)
                continue
            # Kept-text floor, right after mapping: catches the "every
            # validator passed but the edit deleted most of the transcript"
            # failure mode (final.txt may also have been rewritten by the
            # Surgical Patcher / Finalizer during recovery). Editor feedback,
            # not a crash: the next attempt re-runs the loop with the ratio
            # message as contract feedback.
            ratio_ok, ratio_warnings = check_keep_ratio(intermediates)
            if not ratio_ok:
                log_contract_recovery(run_dir, attempt, ratio_warnings)
                if attempt >= CONTRACT_ATTEMPTS:
                    raise PrepOrchestratorError(
                        "Cut kept too little of the source transcript after "
                        "recovery: " + "; ".join(ratio_warnings)[:400]
                    )
                write_contract_feedback(intermediates, attempt, ratio_warnings)
                continue
            if keep_silences:
                # Keep the deliberate pauses intact for the TTS slots
                # (trim_spacings and refine_words would eat into them); the
                # post-TTS de-silence pass removes leftover dead air instead.
                shutil.copyfile(
                    intermediates / "edl.json", intermediates / "edl_trimmed.json"
                )
                render_edl_input = intermediates / "edl_trimmed.json"
            elif comment_mode:
                # No transcript-based silence trim: the editor's keep/cut EDL
                # (words only -- false starts and filler removed, every silence
                # left intact) goes straight to the render EDL. Silences are
                # removed afterwards by the dB de-silencer, once the comment
                # voiceover has been mixed onto the speech (see the comment
                # phase), which also remaps the transcript to the de-silenced
                # timeline.
                render_edl_input = intermediates / "edl.json"
            else:
                # Bulk silence removal happens up front in the de-silence step
                # (shared/desilence.py). After the editor, trim_spacings collapses any
                # inter-word spacing still longer than its threshold -- the murmurs a
                # speaker says to themselves that Scribe marks as `spacing` and that
                # silence-detect cannot remove (they are audible) -- down to a short
                # head. It replaces the old post-editor silence-shortening pass.
                run_step(
                    "trim spacings",
                    [
                        sys.executable,
                        str(SHARED / "trim_spacings.py"),
                        str(intermediates / "edl.json"),
                        "--words",
                        str(intermediates / "word_for_word.json"),
                        "-o",
                        str(intermediates / "edl_trimmed.json"),
                    ],
                )
                # Refine the kept segments down to WORDS: cut audible non-word noise
                # (coughs, throat-clears, murmurs) that the de-silencer keeps because
                # it is above the silence floor and that Scribe leaves untranscribed.
                # Runs on the de-silenced working audio (raw_mp4 == desilenced.mp4
                # here) using the word timings; derive_transcript re-times the words
                # on the resulting cut, so no manual re-timing is needed.
                run_step(
                    "refine words",
                    [
                        sys.executable,
                        str(SHARED / "refine_words.py"),
                        str(intermediates / "edl_trimmed.json"),
                        "--words",
                        str(intermediates / "word_for_word.json"),
                        "--audio",
                        str(raw_mp4),
                        "-o",
                        str(intermediates / "edl_refined.json"),
                    ],
                )
                render_edl_input = intermediates / "edl_refined.json"
            run_step(
                "make render EDL",
                [
                    sys.executable,
                    str(HELPERS / "make_render_edl.py"),
                    str(render_edl_input),
                    "-o",
                    str(intermediates / "edl_render.json"),
                    # Keep-silences drops ONLY the spoken-word cuts so every
                    # deliberate pause survives as a TTS slot; the post-TTS
                    # de-silencer trims whatever pauses end up unused.
                    *(["--keep-silences"] if keep_silences else []),
                ],
            )
            if keep_silences:
                # Keep the silent intro so the AI narration has its slot.
                _keep_leading_silence(intermediates / "edl_render.json")
            run_step(
                "render source.mp4",
                [
                    sys.executable,
                    str(HELPERS / "render_edl.py"),
                    str(intermediates / "edl_render.json"),
                    str(raw_mp4),
                    "-o",
                    str(run_dir / "source.mp4"),
                ],
            )
            # The de-silenced timeline is final after the editor + trim_spacings,
            # so derive the transcript straight from word_for_word.json + the
            # render EDL.
            wfw_for_derive = intermediates / "word_for_word.json"
            run_step(
                "derive transcript.json from EDL + word_for_word",
                [
                    sys.executable,
                    str(HELPERS / "derive_transcript.py"),
                    str(wfw_for_derive),
                    str(intermediates / "edl_render.json"),
                    "-o",
                    str(run_dir / "transcript.json"),
                ],
            )
            verify_contract(run_dir)
            ok, warnings = run_cut_contract_validator(run_dir)
            if ok:
                remove_if_exists(intermediates / "contract_feedback.txt")
                # source.mp4 + transcript.json are rendered and the contract is
                # verified; nothing downstream reads desilenced.mp4 again. Drop
                # it now -- pure scratch (~250 MB/run) that otherwise lingers in
                # pipeline/runs forever. raw.mp4 is kept (re-animate recovery).
                remove_if_exists(desilenced)
                print(f"Run {video_id}: cut contract ready", flush=True)
                break
            log_contract_recovery(run_dir, attempt, warnings)
            if attempt >= CONTRACT_ATTEMPTS:
                raise PrepOrchestratorError(
                    "Cut could not produce a reliable cut after internal recovery. "
                    f"See {run_dir / 'logs' / 'Prep_Contract_Report.json'}"
                )
            write_contract_feedback(intermediates, attempt, warnings)
    finally:
        reap_sidecars()


def resurface_from_edl(video_id: str | int, *, keep_silences: bool = False) -> None:
    """Rebuild `source.mp4` + `transcript.json` from the CURRENT on-disk
    `_intermediates/edl_render.json`, WITHOUT re-running de-silence-transcription,
    the editor / reviewer / mapper recovery loop, trim_spacings, refine_words, or
    make_render_edl.

    This is the deterministic re-render entry: `edl_render.json` has already been
    edited on disk (by hand or by a change agent) and only the final ffmpeg cut +
    transcript derivation need to be replayed. It drives the SAME deterministic
    tail as `_run_cut_body` (render_edl -> source.mp4 -> derive transcript.json ->
    verify_contract), reusing `run_step` and the identical helper paths / params.

    The render EDL timecodes are over the DE-SILENCED working video for a default
    short, but `desilenced.mp4` is deleted after a successful cut (and a forked run
    only carries the original `raw.mp4`). So when it is absent this regenerates it
    with the SAME `desilence.py ... --cap-ms 120` step before rendering. For a
    `keep_silences` run the cut renders straight from `raw.mp4` (no de-silence),
    mirroring `_run_cut_body`. `raw.mp4` is never deleted.
    """
    run_dir, intermediates = prepare_folders(video_id)
    edl_render = intermediates / "edl_render.json"
    raw_mp4 = run_dir / "raw.mp4"
    word_for_word = intermediates / "word_for_word.json"
    # GUARD: none of these three can be rebuilt here. edl_render.json is the input
    # being re-rendered, raw.mp4 is the only re-derivable source, and
    # word_for_word.json holds the immutable base word timings derive_transcript
    # re-times against.
    if not edl_render.exists() or edl_render.stat().st_size <= 0:
        raise PrepOrchestratorError(
            f"No se puede resurgir el corte {video_id}: falta "
            f"_intermediates/edl_render.json en {edl_render}. Es la EDL de render "
            "que se vuelve a renderizar; sin ella no hay nada que reconstruir."
        )
    if not raw_mp4.exists() or raw_mp4.stat().st_size <= 0:
        raise PrepOrchestratorError(
            f"No se puede resurgir el corte {video_id}: falta raw.mp4 en {raw_mp4}. "
            "La grabación original es necesaria para volver a renderizar el corte."
        )
    if not word_for_word.exists() or word_for_word.stat().st_size <= 0:
        raise PrepOrchestratorError(
            f"No se puede resurgir el corte {video_id}: falta "
            f"_intermediates/word_for_word.json en {word_for_word}. Es la "
            "transcripción base con los tiempos de palabra para derivar transcript.json."
        )
    print(f"Run {video_id}: resurfacing source.mp4 + transcript.json from edl_render.json", flush=True)
    try:
        desilenced = run_dir / "desilenced.mp4"
        if keep_silences:
            # keep_silences renders straight from raw.mp4 (no front de-silence),
            # mirroring _run_cut_body.
            print(
                f"Run {video_id}: keeping silences (resurface renders from raw.mp4)",
                flush=True,
            )
            render_source = raw_mp4
        else:
            # Default short: render EDL timecodes are over the de-silenced working
            # video, so regenerate it with the SAME desilence.py params when absent
            # (it is deleted after a successful cut / a fork only carries raw.mp4).
            if desilenced.exists() and desilenced.stat().st_size > 0:
                print(f"Run {video_id}: reusing existing de-silenced video", flush=True)
            else:
                run_step(
                    "de-silence video",
                    [
                        sys.executable,
                        str(SHARED / "desilence.py"),
                        str(raw_mp4),
                        "-o",
                        str(desilenced),
                        "--cap-ms", "120",
                    ],
                )
            render_source = desilenced
        run_step(
            "render source.mp4",
            [
                sys.executable,
                str(HELPERS / "render_edl.py"),
                str(edl_render),
                str(render_source),
                "-o",
                str(run_dir / "source.mp4"),
            ],
        )
        run_step(
            "derive transcript.json from EDL + word_for_word",
            [
                sys.executable,
                str(HELPERS / "derive_transcript.py"),
                str(word_for_word),
                str(edl_render),
                "-o",
                str(run_dir / "transcript.json"),
            ],
        )
        verify_contract(run_dir)
        # Run the SAME contract validator the full cut runs. verify_contract.py
        # does NOT analyze the rendered audio; it compares two derived
        # artifacts: the transcript.json words must form an ordered
        # subsequence of the approved final.txt text (mapper-identical
        # normalization), and ffprobe(source.mp4) duration must match the sum
        # of the edl_render.json keep ranges. A resurface has no editor loop
        # to recover, so a failure is fatal and surfaces as an error -- the
        # duration check is also the net that catches de-silence timeline
        # drift (a regenerated desilenced.mp4 that differs from the one the
        # EDL was pinned to shifts every range, so EDL sums and rendered
        # duration diverge here).
        ok, warnings = run_cut_contract_validator(run_dir)
        if not ok:
            raise PrepOrchestratorError(
                f"El corte resurgido de {video_id} no paso el validador de contrato "
                "(fuente/transcripcion desalineadas): "
                + "; ".join(str(w) for w in (warnings or []))[:600]
            )
        print(f"Run {video_id}: resurface ready", flush=True)
    finally:
        reap_sidecars()


def recut(video_id: int, notes: str | None) -> None:
    """Re-run the editorial cut on an EXISTING run with new free-text notes.

    Requires `raw.mp4` (the kept source) + `_intermediates/word_for_word.json`
    (the immutable base Scribe transcript) present. Writes
    `_intermediates/recut_notes.txt`, then clears the prior cut outputs
    (`reset_cut_attempt`) so the editor-reviewer loop re-runs from iteration 1
    with the notes block present in EVERY editor/reviewer message, and drives
    the SAME deterministic tail (mapper recovery -> trim/refine -> render EDL
    -> source.mp4 -> derive transcript.json). Does NOT re-transcribe:
    `word_for_word.json` is reused (rebuilt byte-identically from the immutable
    `raw_transcript.json`), keeping base word timings stable, so the only
    variable across re-cuts is the editor's keep/cut decisions. A different
    editor decision -> different `edl_render.json` -> different
    `transcript.json` (new duration + word times), which invalidates every
    transcript-derived part; re-derivation of those downstream parts is left to
    the caller (`pipeline.recut_and_rederive`).

    The notes are DELETION-ONLY guidance (see runner.build_editor_message /
    build_reviewer_message): they may only steer WHICH existing words are
    kept/cut, never add words.
    """
    run_dir, intermediates = prepare_folders(video_id)
    raw_mp4 = run_dir / "raw.mp4"
    # GUARD: the raw source is the only thing a re-cut cannot rebuild. It is
    # kept in the run dir precisely for re-cut/re-animate recovery; without it
    # there is nothing to cut from.
    if not raw_mp4.exists() or raw_mp4.stat().st_size <= 0:
        raise PrepOrchestratorError(
            f"Cannot re-cut run {video_id}: raw.mp4 is missing at {raw_mp4}. "
            "The original recording is required to re-run the editorial cut."
        )
    # Write the notes FIRST so they are already on disk when the editor-reviewer
    # loop starts (runner.py reads them by path existence). reset_cut_attempt
    # then clears final.txt + per-iter proposals + every edl*.json + source.mp4
    # + transcript.json, so the loop re-runs from iteration 1 with the notes
    # present -- it does NOT touch raw_transcript.json / word_for_word.json /
    # verbose.txt / recut_notes.txt, so the base transcript is reused.
    (intermediates / "recut_notes.txt").write_text(
        (notes or "").strip() + "\n", encoding="utf-8"
    )
    print(f"Run {video_id}: re-cut requested; clearing prior cut outputs", flush=True)
    reset_cut_attempt(intermediates, run_dir)
    _run_cut_body(
        video_id,
        raw_mp4,
        intermediates,
        run_dir,
        keep_silences=False,
        comment_mode=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id")
    parser.add_argument("raw_mp4", type=Path)
    parser.add_argument(
        "--keep-silences",
        action="store_true",
        help="Skip the front de-silence and trim_spacings so deliberate pauses "
        "survive (used by the ranking flow, which de-silences after mixing TTS).",
    )
    parser.add_argument(
        "--comment-text",
        type=str,
        default=None,
        help="Comment mode: viewer comment handed to the editor/reviewer as "
        "read-only context. Also disables the front de-silence and every "
        "transcript-based silence trim (the dB de-silencer runs later, after "
        "the comment voiceover mix).",
    )
    args = parser.parse_args(argv)
    try:
        run_cut(
            args.video_id,
            args.raw_mp4,
            keep_silences=args.keep_silences,
            comment_text=args.comment_text,
        )
    except PrepOrchestratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Run {args.video_id}: cut orchestrator FAILED: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
