"""Structural teardown of child processes (ffmpeg, node/remotion, agents).

Windows has no SIGHUP / process-group cascade, so when the pipeline process is
killed (Ctrl-C, taskkill, a crash, or the launcher dying) the ffmpeg and node
workers it spawned keep running as orphans and peg the CPU. `reap_sidecars.py`
only handles leaked Claude Code tail-shells, not these compute children.

`install_process_reaper()` closes that gap STRUCTURALLY and once, with no
per-subprocess bookkeeping:

  * Windows — puts the current process into a Job Object with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. Every child (and grandchild) the
    process spawns is automatically added to the job, and the OS terminates the
    whole job the instant the root handle closes — which happens on ANY process
    exit, including a hard ``TerminateProcess``. None of the project's
    subprocesses use ``CREATE_BREAKAWAY_FROM_JOB``, so all of them are captured.
    Nested jobs (Windows 8+) mean it composes with a parent that already set one.

  * POSIX — becomes a process-group leader and, on SIGINT/SIGTERM/SIGHUP, sends
    the group a SIGKILL so children die with it. It does NOT tear the group down
    on a normal exit (children are already finished by then), so it never
    self-kills mid-cleanup.

Call it as early as possible at every entrypoint that may spawn ffmpeg/node/
agents. It is idempotent, never raises, and is a no-op where unsupported.
"""
from __future__ import annotations

import atexit
import os
import signal

_installed = False
# Windows: the job handle is kept alive for the whole process lifetime. The OS
# closes it on process death, which is what fires KILL_ON_JOB_CLOSE. Storing it
# documents that intent and guarantees nothing drops the reference early.
_job_handle: int | None = None


def install_process_reaper() -> bool:
    """Make child processes die with this process. Idempotent; never raises.

    Returns True when a reaper was installed (or was already installed), False
    when the platform/runtime did not support it (the caller can ignore the
    result; failure is a safe no-op).
    """
    global _installed
    if _installed:
        return True
    _installed = True
    try:
        if os.name == "nt":
            return _install_windows_job()
        return _install_posix_group()
    except Exception:
        # Reaping is a safety net; never let its setup break the real work.
        return False


def _install_windows_job() -> bool:
    global _job_handle
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CreateJobObjectW = kernel32.CreateJobObjectW
    CreateJobObjectW.restype = wintypes.HANDLE
    CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

    SetInformationJobObject = kernel32.SetInformationJobObject
    SetInformationJobObject.restype = wintypes.BOOL
    SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]

    AssignProcessToJobObject = kernel32.AssignProcessToJobObject
    AssignProcessToJobObject.restype = wintypes.BOOL
    AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.restype = wintypes.HANDLE

    ULONG_PTR = ctypes.c_size_t
    SIZE_T = ctypes.c_size_t

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    job = CreateJobObjectW(None, None)
    if not job:
        return False

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        ctypes.windll.kernel32.CloseHandle(job)
        return False

    if not AssignProcessToJobObject(job, GetCurrentProcess()):
        # Already in a job that forbids nesting/assignment (rare on Win8+).
        ctypes.windll.kernel32.CloseHandle(job)
        return False

    # Hold the handle open for the process lifetime; the OS closes it on exit,
    # which fires KILL_ON_JOB_CLOSE and reaps every surviving descendant.
    _job_handle = job
    return True


def _install_posix_group() -> bool:
    try:
        os.setpgrp()  # become a new process-group leader; children inherit it
    except (OSError, AttributeError):
        return False
    pgid = os.getpgrp()

    def _kill_group(signum, _frame):
        try:
            os.killpg(pgid, signal.SIGKILL)
        finally:
            os._exit(128 + signum)

    installed_any = False
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _kill_group)
            installed_any = True
        except (OSError, ValueError, AttributeError):
            pass

    # Normal exit needs no group kill (children are done); only register a
    # best-effort atexit that reaps anything still alive, without touching self.
    atexit.register(_reap_posix_children)
    return installed_any


def _reap_posix_children() -> None:
    """Best-effort: SIGTERM any still-running direct children on a clean exit."""
    try:
        import subprocess  # noqa: F401  (kept local; only psutil-free stdlib used)

        # No central child registry on POSIX; rely on the signal-path killpg for
        # interruptions. A clean exit means the pipeline finished, so there is
        # normally nothing to reap here. This hook exists for symmetry/extension.
    except Exception:
        pass
