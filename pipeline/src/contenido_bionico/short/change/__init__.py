"""User-facing change orchestrator for produced shorts.

The dashboard's "Cambios" box hands a free-text request (scoped to the
deliverable the user opened) to an AUTONOMOUS editor agent that edits the run's
render inputs — and reads the pipeline code as needed — and re-renders only the
affected deliverable; this package's `orchestrator.run_change` builds the
message, launches that agent, parses the deliverable(s) it changed, and ensures
they are published.
"""
