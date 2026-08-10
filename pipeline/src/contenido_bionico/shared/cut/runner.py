"""Cut workflow runner.

Launches the cut Editor and Reviewer in fresh Claude Code CLI sessions and
drives the editor-reviewer loop invoked by `shared.cut.orchestrator.run_cut`.

Loop contract (kept in sync with agents/editor.md and agents/reviewer.md):
- Iteration numbers are plain integers (1, 2, 3, ...). They are accepted on
  the CLI either bare (`--iteration 1`) or zero-padded (`--iteration 01`)
  but written to disk and injected into agent prompts as unpadded integers.
- Per-iteration artifacts live at:
    runs/<id>/_intermediates/editor/proposal_<N>.txt
    runs/<id>/_intermediates/reviewer/review_<N>.md
- The deterministic stutter check runs on every editor proposal BEFORE the
  reviewer. When it finds exact consecutive duplicate n-grams the loop writes
  the findings as that iteration's review and goes straight back to the
  editor — the reviewer is not called that round.
- Approval is detected strictly: after upper-casing and stripping Spanish
  accents, the reviewer's stripped stdout (or its first non-empty line) must
  EQUAL `TRANSCRIPCION VALIDA`. Anything else is a rejection.
- On approval, the editor proposal for that iteration is copied verbatim to
  runs/<id>/_intermediates/final.txt for the mapper stage to consume.
- The loop is capped at 8 iterations by default (see shared.cut.orchestrator).

Env flags:
- BIONICO_CUT_REVIEWER_MODEL: overrides the reviewer's model id (default
  claude-sonnet-5; set to claude-opus-4-8 to restore the Opus reviewer).

Usage from the repo root:
    python src/contenido_bionico/shared/cut/runner.py run-agent editor --video-id 17 --iteration 1
    python src/contenido_bionico/shared/cut/runner.py run-agent reviewer --video-id 17 --iteration 1
    python src/contenido_bionico/shared/cut/runner.py editor-reviewer-loop --video-id 17 --max-iterations 8
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass


PACKAGE_ROOT = Path(__file__).resolve().parents[2]      # src/contenido_bionico/
SRC = Path(__file__).resolve().parents[3]               # src/
REPO_ROOT = Path(__file__).resolve().parents[4]         # repo root (contains .env)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from contenido_bionico.shared.runtime.agent_runner import (  # noqa: E402
    HeartbeatInfo,
    load_env as load_dotenv,
    missing_agent_cmd_message,
    printable_command,
    resolve_agent_cmd,
    run_agent_code,
)
from contenido_bionico.shared.run_ids import run_number  # noqa: E402
from contenido_bionico.shared.cut.stutter_check import (  # noqa: E402
    find_fuzzy_stutters,
    find_stutters,
    format_findings as format_stutter_findings,
    format_fuzzy_findings,
)

AGENTS_DIR = Path(__file__).resolve().parent / "agents"
RUNS = REPO_ROOT / "runs"

DEFAULT_MAX_TURNS = 300
VALID_REVIEW_MARKER = "TRANSCRIPCION VALIDA"

# Reviewer model. Demoted from claude-opus-4-8 after 25 production runs where
# the Opus reviewer never rejected anything the deterministic validators did
# not also catch (and approved a literal 4-word stutter). The env override
# lets the operator restore Opus without a code change; it is resolved at
# call time so values loaded from .env by load_env() are honored.
REVIEWER_MODEL_ENV = "BIONICO_CUT_REVIEWER_MODEL"
DEFAULT_REVIEWER_MODEL = "claude-sonnet-5"

# Reject any Finalizer output whose word count shrinks final.txt by more than
# this fraction. A past incident had one bad Finalizer call delete ~90% of
# the transcript while every downstream validator passed. Exit code 43 tells
# the cut orchestrator to escalate to the editor-loop recovery path instead
# of accepting the fix.
FINALIZER_SHRINK_GUARD_RATIO = 0.20
FINALIZER_SHRINK_EXIT_CODE = 43


def format_duration(seconds: float) -> str:
    """Render an elapsed-seconds value as 'Xm Ys' (or 'Ys' under one minute)."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def format_size(num_bytes: int) -> str:
    """Render a byte count as a short KB/MB/GB string."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    prompt_filename: str
    display_name: str
    output_stem: str
    output_suffix: str
    model: str
    effort: str
    timeout_seconds: int | None

    @property
    def prompt_path(self) -> Path:
        return AGENTS_DIR / self.prompt_filename


# Editor timeout is None: long videos can take well over an hour and a fixed
# wall-clock cap killed runs that were making real progress. Liveness is now
# tracked via the heartbeat below (see HEARTBEAT_INTERVAL_SECONDS), which lets
# the operator see whether the agent is still producing output or has actually
# stalled. Reviewer keeps a generous 20-minute cap because its input is much
# smaller than the editor's and a genuine hang there should surface quickly.
AGENTS: dict[str, AgentSpec] = {
    "editor": AgentSpec(
        "editor",
        "editor.md",
        "Editor",
        "proposal",
        ".txt",
        "claude-opus-4-8",
        "max",
        None,
    ),
    "reviewer": AgentSpec(
        "reviewer",
        "reviewer.md",
        "Reviewer",
        "review",
        ".md",
        DEFAULT_REVIEWER_MODEL,
        "max",
        20 * 60,
    ),
    # The finalizer is a deterministic-contract repair agent. It only runs
    # when the mapper rejects an editor-reviewer-approved final transcript;
    # its job is to apply the minimal deletion-only edits that re-satisfy
    # the mapper. Same model + timeout policy as the editor since input
    # sizes are comparable and a single long turn is expected.
    "finalizer": AgentSpec(
        "finalizer",
        "finalizer.md",
        "Finalizer",
        "fix",
        ".txt",
        "claude-opus-4-8",
        "max",
        None,
    ),
}


HEARTBEAT_INTERVAL_SECONDS = 120.0
HEARTBEAT_STALL_THRESHOLD_SECONDS = 5 * 60.0
AGENT_MAX_ATTEMPTS = 3
AGENT_RETRY_BACKOFF_SECONDS = 3.0
# Pattern matches the "=== Duration: 7m 58s ===" line written by
# finalize_call_log. Used to learn typical iter wall-clock from completed
# agent-call logs in the same run.
_LOG_DURATION_RE = re.compile(r"Duration:\s*(?:(\d+)m\s+)?(\d+)s")


def read_previous_iter_durations(video_id: int, agent_name: str) -> list[float]:
    """Return durations (seconds) of all earlier successfully-logged calls
    for this agent in this video's run. Empty list if none yet."""
    logs_dir = agent_log_dir(video_id)
    if not logs_dir.is_dir():
        return []
    durations: list[float] = []
    for log_path in sorted(logs_dir.glob(f"*_{agent_name}_*.log")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "Status: SUCCESS" not in text and "Status: FAILED" not in text:
            # In-progress log (no finalize block yet). Skip it.
            continue
        if "Status: SUCCESS" not in text:
            # Only count successful iters; a 30-min TIMEOUT skews the ETA.
            continue
        match = _LOG_DURATION_RE.search(text)
        if not match:
            continue
        minutes = int(match.group(1) or 0)
        seconds = int(match.group(2))
        durations.append(minutes * 60.0 + seconds)
    return durations


class RunnerError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def load_env() -> None:
    load_dotenv(REPO_ROOT)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def normalize_iteration(value: str | int) -> str:
    raw = str(value).strip()
    if not raw.isdigit():
        raise RunnerError(f"Iteration must be numeric: {value}", exit_code=10)
    number = int(raw)
    if number <= 0:
        raise RunnerError(f"Iteration must be greater than zero: {value}", exit_code=10)
    return f"{number:02d}"


def run_dir(video_id: int) -> Path:
    return RUNS / str(video_id)


def intermediates_dir(video_id: int) -> Path:
    return run_dir(video_id) / "_intermediates"


def agent_output_dir(video_id: int, agent_name: str) -> Path:
    return intermediates_dir(video_id) / agent_name


def agent_output_path(video_id: int, agent_name: str, iteration: str) -> Path:
    agent = AGENTS[agent_name]
    return agent_output_dir(video_id, agent_name) / (
        f"{agent.output_stem}_{int(iteration)}{agent.output_suffix}"
    )


def agent_log_dir(video_id: int) -> Path:
    return intermediates_dir(video_id) / "logs" / "agent-calls"


def verbose_path(video_id: int) -> Path:
    return intermediates_dir(video_id) / "verbose.txt"


def final_path(video_id: int) -> Path:
    return intermediates_dir(video_id) / "final.txt"


def contract_feedback_path(video_id: int) -> Path:
    return intermediates_dir(video_id) / "contract_feedback.txt"


def comment_context_path(video_id: int) -> Path:
    return intermediates_dir(video_id) / "comment.txt"


def read_comment_context(video_id: int) -> str | None:
    """Comment-response format only: the viewer comment the speaker is answering.

    Returns None for non-comment runs (the file is absent). Context only --
    the editor/reviewer must never pull these words into the transcript. Read
    by path existence (mirrors contract_feedback_path; not read_required_text,
    which raises when the optional file is missing).
    """
    path = comment_context_path(video_id)
    try:
        if path.is_file() and path.stat().st_size > 0:
            text = path.read_text(encoding="utf-8")
            return text if text.strip() else None
    except OSError:
        return None
    return None


def recut_notes_path(video_id: int) -> Path:
    return intermediates_dir(video_id) / "recut_notes.txt"


def read_recut_notes(video_id: int) -> str | None:
    """Re-cut only: the user's free-text notes steering WHICH existing words the
    editor keeps or cuts on a re-run of an EXISTING run.

    Returns None when the file is absent (a fresh cut has no notes). The notes
    are DELETION-ONLY guidance: they may only choose what to keep/cut from the
    speaker's own words, never add or insert new words. Read by path existence
    (mirrors read_comment_context / contract_feedback_path; not
    read_required_text, which raises when the optional file is missing).
    """
    path = recut_notes_path(video_id)
    try:
        if path.is_file() and path.stat().st_size > 0:
            text = path.read_text(encoding="utf-8")
            return text if text.strip() else None
    except OSError:
        return None
    return None


def existing_agent_output(video_id: int, agent_name: str, iteration: str) -> Path | None:
    path = agent_output_path(video_id, agent_name, iteration)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def ensure_video_run(video_id: int) -> None:
    path = run_dir(video_id)
    if not path.exists():
        raise RunnerError(f"Run folder does not exist: {path}", exit_code=20)
    if not path.is_dir():
        raise RunnerError(f"Run path is not a directory: {path}", exit_code=20)


def read_required_text(path: Path, label: str) -> str:
    if not path.exists():
        raise RunnerError(f"Missing {label}: {path}", exit_code=30)
    if not path.is_file():
        raise RunnerError(f"{label} is not a file: {path}", exit_code=30)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RunnerError(f"{label} is empty: {path}", exit_code=30)
    return text


def tagged_block(name: str, value: str) -> str:
    return f"<{name}>\n{value.rstrip()}\n</{name}>"


def require_non_empty_file(path: Path, label: str) -> Path:
    if not path.exists():
        raise RunnerError(f"Missing {label}: {path}", exit_code=30)
    if not path.is_file():
        raise RunnerError(f"{label} is not a file: {path}", exit_code=30)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RunnerError(f"Cannot stat {label}: {path} ({exc})", exit_code=30) from exc
    if size <= 0:
        raise RunnerError(f"{label} is empty: {path}", exit_code=30)
    return path


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", value)
        if unicodedata.category(ch) != "Mn"
    )


def is_valid_review(text: str) -> bool:
    """Strict approval detection.

    Approval ONLY when the stripped stdout — or its first non-empty line —
    EQUALS the marker after upper-casing and accent-stripping. A substring
    match anywhere used to count as approval, which let a rejection that
    merely mentioned the marker (or an approval buried inside commentary)
    slip through; anything that is not exactly the marker is a rejection.
    """
    normalized = strip_accents(text.upper()).strip()
    if not normalized:
        return False
    if normalized == VALID_REVIEW_MARKER:
        return True
    first_line = next(
        (line.strip() for line in normalized.splitlines() if line.strip()), ""
    )
    return first_line == VALID_REVIEW_MARKER


def resolve_agent_model(agent_name: str) -> str:
    """Model id for one agent call.

    The reviewer's model is env-overridable (BIONICO_CUT_REVIEWER_MODEL) so
    the operator can restore claude-opus-4-8 without a code change. Resolved
    per call — not at import — so .env values loaded by load_env() apply.
    """
    if agent_name == "reviewer":
        override = os.environ.get(REVIEWER_MODEL_ENV, "").strip()
        if override:
            return override
    return AGENTS[agent_name].model


def agent_timeout(agent_name: str, requested: int | None) -> int | None:
    if requested is not None:
        return requested
    return AGENTS[agent_name].timeout_seconds


def build_editor_message(video_id: int, iteration: str) -> str:
    iteration_number = int(iteration)
    verbose = read_required_text(verbose_path(video_id), "verbose transcript")
    lines = [
        "Run the transcript Editor job.",
        f"<VIDEO_ID> = {run_number(video_id)}",
        f"<ITERATION> = {iteration_number}",
        "The helper has already read the required inputs.",
        "Do not read files, do not write files, and do not use tools.",
        "Return only the edited transcript on stdout.",
        tagged_block("VERBOSE_TRANSCRIPT", verbose),
    ]
    comment_context = read_comment_context(video_id)
    if comment_context is not None:
        lines.extend(
            [
                "This is a COMMENT-RESPONSE video: the speaker is answering the viewer "
                "comment below. Use it ONLY as context to judge whether the kept answer "
                "reads as a coherent, standalone reply. NEVER copy any of the comment's "
                "words into the transcript -- your edit stays deletion-only over the "
                "speaker's own words.",
                tagged_block("VIEWER_COMMENT", comment_context),
            ]
        )
    recut_notes = read_recut_notes(video_id)
    if recut_notes is not None:
        lines.extend(
            [
                "The user asked for a specific re-edit of this cut. Their request is "
                "below. Honor it, but your edit stays DELETION-ONLY: you may only "
                "choose WHICH of the speaker's own words to keep or cut. You must "
                "NEVER add, insert, rewrite, reorder, or invent words to satisfy the "
                "request -- if it asks for words that were never spoken, ignore that "
                "part. Apply the request only insofar as it selects among the existing "
                "transcript words.",
                tagged_block("USER_CHANGE_REQUEST", recut_notes),
            ]
        )
    feedback_path = contract_feedback_path(video_id)
    if feedback_path.exists():
        feedback = read_required_text(
            feedback_path,
            "contract feedback from previous cut attempt",
        )
        lines.extend(
            [
                "The previous approved transcript produced an invalid cut contract.",
                "Use this feedback to make a safer deletion-only transcript.",
                tagged_block("PREP_CONTRACT_FEEDBACK", feedback),
            ]
        )
    if iteration_number >= 2:
        previous = normalize_iteration(iteration_number - 1)
        previous_proposal = read_required_text(
            agent_output_path(video_id, "editor", previous),
            f"previous editor proposal {int(previous)}",
        )
        previous_review = read_required_text(
            agent_output_path(video_id, "reviewer", previous),
            f"previous reviewer review {int(previous)}",
        )
        lines.extend(
            [
                f"<PREVIOUS_ITERATION> = {int(previous)}",
                tagged_block("PREVIOUS_EDITOR_PROPOSAL", previous_proposal),
                tagged_block("PREVIOUS_REVIEWER_FEEDBACK", previous_review),
            ]
        )
    lines.append("Begin.")
    return "\n".join(lines)


def build_reviewer_message(video_id: int, iteration: str) -> str:
    verbose = read_required_text(verbose_path(video_id), "verbose transcript")
    proposal = read_required_text(
        agent_output_path(video_id, "editor", iteration),
        f"editor proposal {int(iteration)}",
    )
    # The editor's full system prompt is deliberately NOT embedded here:
    # reviewer.md carries a short summary of the deletion-only editor
    # contract instead, which keeps the reviewer's user message small.
    lines = [
        "Run the transcript Reviewer job.",
        f"<VIDEO_ID> = {run_number(video_id)}",
        f"<ITERATION> = {int(iteration)}",
        "The helper has already read the required inputs.",
        "Do not read files, do not write files, and do not use tools.",
        "Return only the reviewer verdict on stdout.",
        tagged_block("VERBOSE_TRANSCRIPT", verbose),
        tagged_block("EDITOR_PROPOSAL", proposal),
    ]
    comment_context = read_comment_context(video_id)
    if comment_context is not None:
        lines.extend(
            [
                "This is a COMMENT-RESPONSE video: the speaker is answering the viewer "
                "comment below. Use it ONLY to judge whether the kept answer reads as a "
                "coherent, standalone reply. The comment's words must NOT appear in the "
                "transcript.",
                tagged_block("VIEWER_COMMENT", comment_context),
            ]
        )
    recut_notes = read_recut_notes(video_id)
    if recut_notes is not None:
        lines.extend(
            [
                "The user asked for a specific re-edit of this cut (their request is "
                "below). Judge whether the editor's proposal honors it while staying "
                "DELETION-ONLY: the proposal may only keep or cut the speaker's own "
                "words. Reject any proposal that adds, inserts, rewrites, reorders, or "
                "invents words -- even if the request seemed to ask for new content, "
                "the cut can never introduce words that were not spoken.",
                tagged_block("USER_CHANGE_REQUEST", recut_notes),
            ]
        )
    lines.append("Begin.")
    return "\n".join(lines)


def build_finalizer_message(
    video_id: int, mapper_error_text: str
) -> str:
    """Build the finalizer prompt.

    The finalizer is invoked only when the mapper rejects the
    editor-reviewer-approved final transcript. It receives the verbose
    Scribe transcript, the current final.txt, and the raw mapper
    diagnostics, and must return a corrected final transcript that
    re-satisfies the deletion-only contract — without re-running the
    editor-reviewer loop.
    """
    verbose = read_required_text(verbose_path(video_id), "verbose transcript")
    current_final = read_required_text(
        final_path(video_id), "current final transcript"
    )
    return "\n".join(
        [
            "Run the cut Finalizer job.",
            f"<VIDEO_ID> = {run_number(video_id)}",
            "The helper has already read the required inputs.",
            "Do not read files, do not write files, and do not use tools.",
            "Return only the corrected final transcript on stdout.",
            tagged_block("VERBOSE_TRANSCRIPT", verbose),
            tagged_block("CURRENT_FINAL", current_final),
            tagged_block("MAPPER_ERROR", mapper_error_text),
            "Begin.",
        ]
    )


def build_user_message(agent_name: str, video_id: int, iteration: str) -> str:
    if agent_name == "editor":
        return build_editor_message(video_id, iteration)
    if agent_name == "reviewer":
        return build_reviewer_message(video_id, iteration)
    raise RunnerError(f"Unknown agent: {agent_name}", exit_code=11)


def open_call_log(
    *,
    video_id: int,
    agent_name: str,
    iteration: str,
    output_path: Path,
    start_stamp: str,
    attempt: int = 1,
) -> Path:
    """Create the log file at the START of the call with a header.

    Subsequent stdout/stderr/status get appended via finalize_call_log so
    the log filename and the file's mtime reflect when the agent began.
    Lets the operator tail the log file mid-run instead of waiting for
    the agent to exit before any record exists on disk.
    """
    logs = agent_log_dir(video_id)
    logs.mkdir(parents=True, exist_ok=True)
    label = AGENTS[agent_name].output_stem
    attempt_label = "" if attempt == 1 else f"_retry-{attempt}"
    log_path = logs / f"{start_stamp}_{agent_name}_{label}-{int(iteration)}{attempt_label}.log"
    log_path.write_text(
        (
            f"=== Agent: {agent_name} ===\n"
            f"=== Video ID: {video_id} ===\n"
            f"=== Iteration: {iteration} ===\n"
            f"=== Attempt: {attempt} ===\n"
            f"=== Started: {start_stamp} ===\n"
            f"=== Output path: {output_path} ===\n"
        ),
        encoding="utf-8",
    )
    return log_path


def finalize_call_log(
    *,
    log_path: Path,
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    status: str,
    end_stamp: str,
    duration_seconds: float,
) -> None:
    """Append the post-subprocess data to an already-opened call log."""
    safe_command = printable_command(command)
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(
            f"=== Finished: {end_stamp} ===\n"
            f"=== Duration: {format_duration(duration_seconds)} ===\n"
            f"=== Status: {status} ===\n"
            f"=== Exit code: {returncode} ===\n"
            f"=== Command: {' '.join(safe_command)} ===\n\n"
            f"=== STDOUT ===\n{stdout}\n\n"
            f"=== STDERR ===\n{stderr}\n"
        )


def _call_claude_agent_once(
    *,
    agent_name: str,
    video_id: int,
    iteration: str,
    timeout_seconds: int | None,
    max_turns: int,
    user_message_override: str | None = None,
    attempt: int,
) -> tuple[Path, Path]:
    ensure_video_run(video_id)
    agent = AGENTS[agent_name]
    effective_timeout = agent_timeout(agent_name, timeout_seconds)
    if not agent.prompt_path.exists():
        raise RunnerError(f"System prompt not found: {agent.prompt_path}", exit_code=21)

    agent_cmd = resolve_agent_cmd()
    if not agent_cmd:
        raise RunnerError(missing_agent_cmd_message(), exit_code=22)

    system_prompt = agent.prompt_path.read_text(encoding="utf-8")
    user_message = (
        user_message_override
        if user_message_override is not None
        else build_user_message(agent_name, video_id, iteration)
    )
    output_path = agent_output_path(video_id, agent_name, iteration)
    ensure_parent(output_path)

    # Open the log file BEFORE the subprocess starts so its filename and
    # mtime reflect when the call began. This also lets the operator tail the
    # in-progress log mid-run instead of waiting for the agent to exit.
    iter_num = int(iteration)
    start_stamp = now_stamp()
    start_ts = datetime.now()
    log_path = open_call_log(
        video_id=video_id,
        agent_name=agent_name,
        iteration=iteration,
        output_path=output_path,
        start_stamp=start_stamp,
        attempt=attempt,
    )

    timeout_label = (
        format_duration(effective_timeout) if effective_timeout is not None else "none"
    )
    historical_durations = read_previous_iter_durations(video_id, agent_name)
    eta_baseline_label = (
        f"avg of {len(historical_durations)} prev iter(s): "
        f"{format_duration(sum(historical_durations) / len(historical_durations))}"
        if historical_durations
        else "no historical data"
    )
    print(
        (
            f"> Run {video_id} | {agent.display_name} iter {iter_num} starting | "
            f"attempt {attempt}/{AGENT_MAX_ATTEMPTS} | "
            f"timeout: {timeout_label} | heartbeat every "
            f"{int(HEARTBEAT_INTERVAL_SECONDS)}s | ETA baseline: "
            f"{eta_baseline_label} | {log_path.name}"
        ),
        flush=True,
    )

    heartbeat_lock = threading.Lock()
    avg_duration = (
        sum(historical_durations) / len(historical_durations)
        if historical_durations
        else None
    )

    def _heartbeat(info: HeartbeatInfo) -> None:
        elapsed = format_duration(info.elapsed_seconds)
        # Progress block: percent + ETA when we have a baseline, otherwise a
        # short "first iter" note. We cap percent at 99% so a long-running
        # call never *looks* done.
        if avg_duration is not None and avg_duration > 0:
            ratio = info.elapsed_seconds / avg_duration
            pct = min(int(ratio * 100), 99)
            remaining = max(avg_duration - info.elapsed_seconds, 0.0)
            if ratio <= 1.0:
                progress_block = (
                    f"~{pct}% complete | ~{format_duration(remaining)} left"
                )
            else:
                overshoot = info.elapsed_seconds - avg_duration
                progress_block = (
                    f"~{pct}% (running {format_duration(overshoot)} past "
                    f"baseline {format_duration(avg_duration)})"
                )
        else:
            progress_block = "first iter — no ETA yet"
        # Last-event block: what Claude was last seen doing. post_turn_summary
        # status_detail is the single most useful field; otherwise show event
        # type. If we got raw bytes but no parseable event, say so — that
        # usually points at a CLI version mismatch or an output-format change.
        # Before any byte arrives, say "awaiting first byte" so a hang at
        # subprocess launch is visually distinct from a hang mid-call.
        if info.last_status_detail:
            detail = info.last_status_detail
            if len(detail) > 80:
                detail = detail[:77] + "..."
            last_block = f'last: "{detail}"'
        elif info.last_event_type:
            last_block = (
                f"last event: {info.last_event_type} "
                f"({info.event_count} total)"
            )
        elif info.stdout_bytes > 0:
            last_block = (
                f"received {format_size(info.stdout_bytes)} of stdout but "
                f"no parseable event yet"
            )
        else:
            last_block = "awaiting first byte from Claude"
        # Status tag: "STALLED?" only when *both* (a) no event for the
        # configured stall threshold AND (b) elapsed > baseline * 1.5. The
        # second clause kills the most common false positive: Claude Code's
        # CLI emits stream-json envelopes at the boundaries of a turn but
        # nothing in the middle, so a single long editor turn looks "no
        # events for 5 min" while it's actually producing tokens. Once
        # we're 50% past the historical baseline, that interpretation
        # stops being plausible and a real stall warning is meaningful.
        # Without a baseline (iter 1 of a fresh agent) we fall back to the
        # event-only check so we still warn loudly on a true hang.
        if info.stalled and avg_duration is not None:
            stall_real = info.elapsed_seconds > avg_duration * 1.5
        else:
            stall_real = info.stalled
        status_tag = (
            f"{agent.display_name} STALLED?"
            if stall_real
            else f"{agent.display_name} activo"
        )
        line = (
            f". [{status_tag}] Run {video_id} iter {iter_num} | "
            f"elapsed {elapsed} | {progress_block} | {last_block}"
        )
        with heartbeat_lock:
            print(line, flush=True)
            try:
                with log_path.open("a", encoding="utf-8") as fp:
                    fp.write(f"=== Heartbeat: {line} ===\n")
            except OSError:
                pass

    stdout = stderr = ""
    returncode = -1
    status = "UNKNOWN"
    cmd: list[str] = []
    try:
        result = run_agent_code(
            agent_cmd=agent_cmd,
            system_prompt=system_prompt,
            initial_message=user_message,
            cwd=REPO_ROOT,
            timeout_seconds=effective_timeout,
            max_turns=max_turns,
            system_prompt_mode="replace",
            tools=[],
            model=resolve_agent_model(agent_name),
            effort=agent.effort,
            permission_mode=None,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            heartbeat_callback=_heartbeat,
            stall_threshold_seconds=HEARTBEAT_STALL_THRESHOLD_SECONDS,
            log_path=log_path,
        )
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode
        cmd = result.command
        status = "SUCCESS" if returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        returncode = 124
        status = "TIMEOUT"

    end_ts = datetime.now()
    duration_seconds = (end_ts - start_ts).total_seconds()
    end_stamp = end_ts.strftime("%Y-%m-%d_%H-%M-%S")

    finalize_call_log(
        log_path=log_path,
        command=cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        status=status,
        end_stamp=end_stamp,
        duration_seconds=duration_seconds,
    )

    if returncode != 0:
        print(
            (
                f"x Run {video_id} | {agent.display_name} iter {iter_num} FAILED | "
                f"attempt {attempt}/{AGENT_MAX_ATTEMPTS} | "
                f"Status: {status} | exit {returncode} | {format_duration(duration_seconds)} | "
                f"Log: {log_path.name}"
            ),
            flush=True,
        )
        raise RunnerError(
            (
                f"{agent.display_name} failed with exit code {returncode}. "
                f"Status: {status}. Log: {log_path}"
            ),
            exit_code=returncode if returncode > 0 else 1,
        )

    output_path.write_text(stdout, encoding="utf-8")
    read_required_text(output_path, f"{agent.display_name} output")

    output_size = format_size(output_path.stat().st_size)
    print(
        (
            f"OK Run {video_id} | {agent.display_name} iter {iter_num} done | "
            f"attempt {attempt}/{AGENT_MAX_ATTEMPTS} | "
            f"Duration: {format_duration(duration_seconds)} | Output: {output_size} | "
            f"{output_path.name}"
        ),
        flush=True,
    )
    return output_path, log_path


def _retryable_agent_error(exc: RunnerError) -> bool:
    return exc.exit_code not in {10, 20, 21, 22, 30}


def call_claude_agent(
    *,
    agent_name: str,
    video_id: int,
    iteration: str,
    timeout_seconds: int | None,
    max_turns: int,
    user_message_override: str | None = None,
) -> tuple[Path, Path]:
    last_error: RunnerError | None = None
    for attempt in range(1, AGENT_MAX_ATTEMPTS + 1):
        try:
            return _call_claude_agent_once(
                agent_name=agent_name,
                video_id=video_id,
                iteration=iteration,
                timeout_seconds=timeout_seconds,
                max_turns=max_turns,
                user_message_override=user_message_override,
                attempt=attempt,
            )
        except RunnerError as exc:
            last_error = exc
            if attempt >= AGENT_MAX_ATTEMPTS or not _retryable_agent_error(exc):
                raise
            print(
                (
                    f"> Run {video_id} | {AGENTS[agent_name].display_name} iter "
                    f"{int(iteration)} attempt {attempt}/{AGENT_MAX_ATTEMPTS} "
                    f"failed; retrying in {AGENT_RETRY_BACKOFF_SECONDS}s. "
                    f"cause: {exc}"
                ),
                flush=True,
            )
            time.sleep(AGENT_RETRY_BACKOFF_SECONDS)
    assert last_error is not None
    raise last_error


def run_agent(
    *,
    agent_name: str,
    video_id: int,
    iteration: str,
    timeout_seconds: int | None,
    max_turns: int,
    user_message_override: str | None = None,
) -> tuple[Path, Path]:
    return call_claude_agent(
        agent_name=agent_name,
        video_id=video_id,
        iteration=iteration,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
        user_message_override=user_message_override,
    )


def cmd_run_agent(args: argparse.Namespace) -> int:
    iteration = normalize_iteration(args.iteration)
    output_path, log_path = run_agent(
        agent_name=args.agent_name,
        video_id=args.video_id,
        iteration=iteration,
        timeout_seconds=args.timeout_seconds,
        max_turns=args.max_turns,
    )
    item = AGENTS[args.agent_name].output_stem
    print(f"OK: {args.agent_name} {item} {int(iteration)} completed for run {args.video_id}")
    print(f"Output: {output_path}")
    print(f"Log: {log_path}")
    if args.agent_name == "reviewer":
        review = output_path.read_text(encoding="utf-8")
        print("Reviewer verdict: VALID" if is_valid_review(review) else "Reviewer verdict: INVALID")
    return 0


def cmd_editor_reviewer_loop(args: argparse.Namespace) -> int:
    ensure_video_run(args.video_id)
    require_non_empty_file(verbose_path(args.video_id), "verbose transcript")

    if args.max_iterations <= 0:
        raise RunnerError("--max-iterations must be greater than zero", exit_code=10)

    final = final_path(args.video_id)
    if final.exists() and final.is_file() and final.stat().st_size > 0:
        read_required_text(final, "final approved transcript")
        print(f"OK: final transcript already exists for run {args.video_id}")
        print(f"Final: {final}")
        print(
            (
                f"OK Run {args.video_id} | editor-reviewer loop SKIPPED | "
                f"Final transcript already on disk: {final.name}"
            ),
            flush=True,
        )
        return 0

    print(
        (
            f"> Run {args.video_id} | editor-reviewer loop starting | "
            f"Max iterations: {args.max_iterations}"
        ),
        flush=True,
    )

    last_review_path: Path | None = None
    for number in range(1, args.max_iterations + 1):
        iteration = normalize_iteration(number)
        editor_path = existing_agent_output(args.video_id, "editor", iteration)
        if editor_path:
            print(f"Editor proposal {int(iteration)}: reusing {editor_path}")
        else:
            editor_path, editor_log = run_agent(
                agent_name="editor",
                video_id=args.video_id,
                iteration=iteration,
                timeout_seconds=args.timeout_seconds,
                max_turns=args.max_turns,
            )
            print(f"Editor proposal {int(iteration)}: {editor_path}")
            print(f"Editor log: {editor_log}")

        # Deterministic stutter gate BEFORE the reviewer. Exact consecutive
        # duplicate n-grams are machine-checkable, and across 25 production
        # runs every real rejection came from this check while the LLM
        # reviewer approved literal stutters — so when the detector fires the
        # loop writes the findings as this iteration's review and goes
        # straight back to the editor, skipping the reviewer call entirely.
        proposal_text = editor_path.read_text(encoding="utf-8-sig")
        stutter_findings = find_stutters(proposal_text)
        fuzzy_findings = find_fuzzy_stutters(proposal_text)
        if stutter_findings:
            review_path = agent_output_path(args.video_id, "reviewer", iteration)
            ensure_parent(review_path)
            feedback = format_stutter_findings(stutter_findings)
            if fuzzy_findings:
                feedback += "\n\n" + format_fuzzy_findings(fuzzy_findings)
            review_path.write_text(
                "TRANSCRIPCIÓN INVÁLIDA\n\n"
                "Validador determinista (pre-reviewer) rechazó la propuesta:\n\n"
                + feedback
                + "\n",
                encoding="utf-8",
            )
            last_review_path = review_path
            print(
                f"Stutter validator rejected proposal {int(iteration)}: "
                f"{len(stutter_findings)} consecutive duplicate n-gram(s) found "
                f"(reviewer skipped this round)"
            )
            if number < args.max_iterations:
                print(
                    (
                        f"x Run {args.video_id} | iter {int(iteration)} rejected by stutter check, continuing | "
                        f"Next: iter {number + 1} of {args.max_iterations}"
                    ),
                    flush=True,
                )
            continue

        review_path = existing_agent_output(args.video_id, "reviewer", iteration)
        if review_path:
            print(f"Reviewer review {int(iteration)}: reusing {review_path}")
        else:
            review_path, review_log = run_agent(
                agent_name="reviewer",
                video_id=args.video_id,
                iteration=iteration,
                timeout_seconds=args.timeout_seconds,
                max_turns=args.max_turns,
            )
            print(f"Reviewer review {int(iteration)}: {review_path}")
            print(f"Reviewer log: {review_log}")
        last_review_path = review_path

        review = review_path.read_text(encoding="utf-8")
        if is_valid_review(review):
            ensure_parent(final)
            shutil.copyfile(editor_path, final)
            read_required_text(final, "final approved transcript")
            print(f"OK: reviewer approved proposal {int(iteration)}")
            print(f"Final: {final}")
            print(
                (
                    f"OK Run {args.video_id} | editor-reviewer loop CONVERGED | "
                    f"Approved at iter {int(iteration)} of {args.max_iterations} | "
                    f"Final transcript: {final.name}"
                ),
                flush=True,
            )
            return 0

        print(f"Reviewer rejected proposal {int(iteration)}; continuing.")
        # Fuzzy (near-duplicate) findings are ADVISORY: they never block an
        # approval on their own, but when the reviewer rejects anyway we
        # append them to the review so the next editor iteration also fixes
        # the almost-identical double takes the exact detector cannot flag.
        if fuzzy_findings:
            fuzzy_feedback = format_fuzzy_findings(fuzzy_findings)
            if fuzzy_feedback and fuzzy_feedback not in review:
                with review_path.open("a", encoding="utf-8") as fp:
                    fp.write("\n\n" + fuzzy_feedback + "\n")
        # Only emit the mid-loop status here. The post-loop "did NOT converge"
        # message (below) covers the final-iteration rejection so we don't
        # duplicate the message on the same event.
        if number < args.max_iterations:
            print(
                (
                    f"x Run {args.video_id} | iter {int(iteration)} rejected, continuing | "
                    f"Next: iter {number + 1} of {args.max_iterations}"
                ),
                flush=True,
            )

    last_review_name = last_review_path.name if last_review_path else "N/A"
    print(
        (
            f"x Run {args.video_id} | editor-reviewer loop did NOT converge | "
            f"{args.max_iterations} iterations exhausted without approval | "
            f"Last review: {last_review_name}"
        ),
        flush=True,
    )
    raise RunnerError(
        (
            f"Reviewer did not approve within {args.max_iterations} iterations. "
            f"Last review: {last_review_path}"
        ),
        exit_code=40,
    )


def _next_finalizer_iteration(video_id: int) -> str:
    """Pick the next zero-padded iter number for the finalizer agent.

    Finalizer outputs live at `_intermediates/finalizer/fix_<N>.txt`; the
    caller scans for the highest existing N and returns N+1 (defaulting to
    1 when the dir is empty). Lets multiple recovery rounds coexist on disk
    for audit."""
    out_dir = agent_output_dir(video_id, "finalizer")
    if not out_dir.is_dir():
        return normalize_iteration(1)
    highest = 0
    for child in out_dir.iterdir():
        match = re.match(r"^fix_(\d+)\.txt$", child.name)
        if not match:
            continue
        n = int(match.group(1))
        if n > highest:
            highest = n
    return normalize_iteration(highest + 1)


def _write_finalizer_input_dump(
    output_dir: Path, iteration: str, message: str
) -> Path:
    """Persist the exact user message the Finalizer received.

    The finalizer call log only records stdout/stderr; without the input
    block it's impossible to tell, after the fact, whether a bad fix came
    from a bad mapper diagnostic, a stale `final.txt`, or the agent itself.
    Dump path: `_intermediates/finalizer/fix_<N>.input.md`.
    """
    input_path = output_dir / f"fix_{int(iteration)}.input.md"
    input_path.write_text(message, encoding="utf-8")
    return input_path


def _write_finalizer_diff(
    output_dir: Path, iteration: str, before: str, after: str
) -> Path:
    """Write a unified diff between the pre-finalizer `final.txt` and the
    Finalizer's corrected output.

    The full corrected transcript is already saved as `fix_<N>.txt`; the
    diff is the fastest way to see what the Finalizer actually changed
    (which words it deleted, which sentence it dropped). Saved as
    `_intermediates/finalizer/fix_<N>.diff`.
    """
    diff_path = output_dir / f"fix_{int(iteration)}.diff"
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=False),
            after.splitlines(keepends=False),
            fromfile=f"final.txt (before fix_{int(iteration)})",
            tofile=f"final.txt (after fix_{int(iteration)})",
            n=3,
            lineterm="",
        )
    )
    body = "\n".join(diff_lines)
    if not body:
        body = (
            f"(no textual diff between final.txt before and after "
            f"fix_{int(iteration)} — finalizer returned an identical "
            f"transcript)\n"
        )
    diff_path.write_text(body + ("\n" if not body.endswith("\n") else ""), encoding="utf-8")
    return diff_path


def cmd_finalizer_fix(args: argparse.Namespace) -> int:
    """Invoke the finalizer agent with the mapper diagnostics and overwrite
    `final.txt` with the corrected transcript so the next mapper attempt
    can consume it. Also persists the agent's input and a diff vs. the
    pre-fix transcript so failed recoveries can be inspected offline."""
    mapper_error_path = Path(args.mapper_error)
    if not mapper_error_path.is_file():
        raise RunnerError(
            f"mapper error log not found: {mapper_error_path}", exit_code=30
        )
    final = final_path(args.video_id)
    if not final.is_file() or final.stat().st_size <= 0:
        raise RunnerError(
            f"final transcript missing for run {args.video_id}: {final}",
            exit_code=30,
        )
    pre_fix_final = final.read_text(encoding="utf-8")
    mapper_error_text = mapper_error_path.read_text(
        encoding="utf-8", errors="replace"
    )
    iteration = _next_finalizer_iteration(args.video_id)
    message = build_finalizer_message(args.video_id, mapper_error_text)
    out_dir = agent_output_dir(args.video_id, "finalizer")
    out_dir.mkdir(parents=True, exist_ok=True)
    input_dump_path = _write_finalizer_input_dump(out_dir, iteration, message)
    print(
        (
            f"> Run {args.video_id} | Finalizer iter {int(iteration)} starting "
            f"| mapper error: {mapper_error_path.name} "
            f"| input dump: {input_dump_path.name}"
        ),
        flush=True,
    )
    output_path, log_path = run_agent(
        agent_name="finalizer",
        video_id=args.video_id,
        iteration=iteration,
        timeout_seconds=args.timeout_seconds,
        max_turns=args.max_turns,
        user_message_override=message,
    )
    corrected = output_path.read_text(encoding="utf-8")
    if not corrected.strip():
        raise RunnerError(
            f"Finalizer iter {int(iteration)} returned empty output",
            exit_code=42,
        )
    # Shrink guard: never accept a "fix" that deletes a large chunk of the
    # approved transcript. The rejected output stays on disk (fix_<N>.txt +
    # diff) for audit, but final.txt is NOT overwritten; the dedicated exit
    # code lets the orchestrator escalate to the editor-loop recovery path.
    pre_fix_words = len(pre_fix_final.split())
    corrected_words = len(corrected.split())
    if pre_fix_words > 0 and corrected_words < pre_fix_words * (
        1.0 - FINALIZER_SHRINK_GUARD_RATIO
    ):
        rejected_diff = _write_finalizer_diff(
            out_dir, iteration, pre_fix_final, corrected
        )
        print(
            (
                f"x Run {args.video_id} | Finalizer iter {int(iteration)} REJECTED "
                f"by shrink guard | {pre_fix_words} -> {corrected_words} words "
                f"(more than {FINALIZER_SHRINK_GUARD_RATIO:.0%} smaller) | "
                f"final.txt NOT overwritten | Diff: {rejected_diff.name}"
            ),
            flush=True,
        )
        raise RunnerError(
            (
                f"Finalizer iter {int(iteration)} output shrank final.txt from "
                f"{pre_fix_words} to {corrected_words} words (more than "
                f"{FINALIZER_SHRINK_GUARD_RATIO:.0%}); rejecting the fix and "
                f"escalating to the editor-loop recovery path"
            ),
            exit_code=FINALIZER_SHRINK_EXIT_CODE,
        )
    final.write_text(corrected, encoding="utf-8")
    diff_path = _write_finalizer_diff(
        out_dir, iteration, pre_fix_final, corrected
    )
    print(
        (
            f"OK Run {args.video_id} | Finalizer iter {int(iteration)} done "
            f"| Output: {format_size(output_path.stat().st_size)} "
            f"| Wrote: {final.name} "
            f"| Diff: {diff_path.name}"
        ),
        flush=True,
    )
    print(f"Finalizer input dump: {input_dump_path}")
    print(f"Finalizer output: {output_path}")
    print(f"Finalizer diff: {diff_path}")
    print(f"Finalizer log: {log_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch cut Editor and Reviewer agents in fresh Claude Code sessions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    agent_parser = subparsers.add_parser(
        "run-agent", help="Launch one cut workflow agent directly."
    )
    agent_parser.add_argument("agent_name", choices=sorted(AGENTS))
    agent_parser.add_argument("--video-id", required=True)
    agent_parser.add_argument("--iteration", required=True)
    agent_parser.add_argument("--timeout-seconds", type=int, default=None)
    agent_parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    agent_parser.set_defaults(func=cmd_run_agent)

    loop_parser = subparsers.add_parser(
        "editor-reviewer-loop", help="Run Editor and Reviewer until approval."
    )
    loop_parser.add_argument("--video-id", required=True)
    loop_parser.add_argument("--max-iterations", type=int, default=8)
    loop_parser.add_argument("--timeout-seconds", type=int, default=None)
    loop_parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    loop_parser.set_defaults(func=cmd_editor_reviewer_loop)

    finalizer_parser = subparsers.add_parser(
        "finalizer-fix",
        help=(
            "Run the Finalizer agent with mapper-failure diagnostics. Reads "
            "the current final.txt and the mapper error log, produces a "
            "corrected deletion-only transcript, and overwrites final.txt "
            "so the next mapper attempt can consume it."
        ),
    )
    finalizer_parser.add_argument("--video-id", required=True)
    finalizer_parser.add_argument(
        "--mapper-error",
        required=True,
        help="Path to the mapper error log written by mapper.py.",
    )
    finalizer_parser.add_argument("--timeout-seconds", type=int, default=None)
    finalizer_parser.add_argument(
        "--max-turns", type=int, default=DEFAULT_MAX_TURNS
    )
    finalizer_parser.set_defaults(func=cmd_finalizer_fix)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
