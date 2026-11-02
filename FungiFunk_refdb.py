#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FungiFunk_refdb.py
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

try:
    from openpyxl import Workbook  # type: ignore
except ImportError:
    Workbook = None


########################################################################################################
# Global constants and default settings
########################################################################################################

TOOL_NAME = "FungiFunk_refdb"
VERSION = "0.4.0"
FINAL_FASTA_NAME = "FungiFunk_protein_refdb.faa"

ESTIMATED_RAM_GIB_PER_WORKER = 1.0
ESTIMATED_TMP_GIB_PER_WORKER = 2.0
HEADER_PREVIEW_MAX_ITEMS = 5

# Emergency reserve is a hard safeguard
EMERGENCY_FREE_DISK_GIB = 2.0
UPDATE_STAGING_SAFETY_FACTOR = 1.15

# NCBI currently documents 5 requests/s without an API key and 10 requests/s
# with one. We throttle starts of NCBI CLI operations globally across workers;
# the Datasets CLI itself also enforces NCBI-side request policy
NCBI_RPS_WITHOUT_KEY = 5.0
NCBI_RPS_WITH_KEY = 10.0

# Retry transient external failures, but do not retry indefinitely.
NCBI_MAX_RETRIES = 3
NCBI_RETRY_BASE_SECONDS = 2.0

# Limit how many NCBI Datasets commands may be alive at the same time.
#
# This is deliberately independent from --workers. Local worker capacity is
# calculated from CPU, RAM and disk, while NCBI concurrency protects access to
# the external service. The value is intentionally conservative for now and can
# later be replaced by an adaptive policy without changing worker semantics.
NCBI_MAX_CONCURRENT_COMMANDS = 4

# Stable machine-readable reasons used when an NCBI Datasets command cannot
# complete successfully after all configured retries.
#
# These reasons describe failures in retrieving data from NCBI. They must remain
# distinct from biological/data-availability outcomes such as NO_PROTEIN_FASTA.
NCBI_RETRIEVAL_FAILURE_REASONS = frozenset(
    {
        "NCBI_REQUEST_TIMEOUT",
        "NCBI_RATE_LIMITED",
        "NCBI_GATEWAY_ERROR",
        "NCBI_REQUEST_FAILED",
    }
)

# Stable output layout
DATABASE_DIR = "database"
METADATA_DIR = "metadata"
REPORTS_DIR = "reports"
CONFIG_DIR = "config"
LOGS_DIR = "logs"
STATE_DIR = "state"
TMP_DIR = "tmp"
MARKER_REL = Path(STATE_DIR) / ".fungifunk_refdb"
STATE_DB_REL = Path(STATE_DIR) / "build_state.sqlite"
RESUME_READY_REL = Path(STATE_DIR) / ".fungifunk_refdb_resume_ready.json"
MANIFEST_REL = Path(STATE_DIR) / "build_manifest.tsv"
COMPLETION_MARKER_REL = (Path(STATE_DIR) / ".fungifunk_refdb_complete.json")
CANONICAL_STORE_REL = Path(STATE_DIR) / "canonical_sequences.bin"
CONFIG_JSON_REL = Path(STATE_DIR) / "resolved_config.json"

SEED_SNAPSHOT = "fungal_seed_taxa.tsv"
EXCLUSION_SNAPSHOT = "fungal_exclusion_taxa.tsv"
FILTER_SNAPSHOT = "metadata_filters.tsv"
INCLUDE_EXPLICIT_TARGETS_SNAPSHOT = "include_explicit_targets.tsv"
EXCLUDE_EXPLICIT_TARGETS_SNAPSHOT = "exclude_explicit_targets.tsv"
RESOLVED_EXPLICIT_TARGETS_JSON = "explicit_targets_resolved.json"
RESOLVED_EXPLICIT_TARGETS_SCHEMA_VERSION = 1

REPRODUCTION_TARGETS_TSV = "reproduction_targets.tsv"

# Run modes
RUN_MODES = {
    "create",
    "resume",
    "create-new-updated-version",
    "overwrite-with-updated-version",
}

# Exit statuses
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CURATION_WAIT = 20
EXIT_RESOURCE = 21
EXIT_EXTERNAL = 22
EXIT_INTEGRITY = 23
EXIT_INTERNAL = 70


########################################################################################################
# Exceptions
########################################################################################################

class FungiFunkError(RuntimeError):
    """Base class for expected, user-readable failures."""

    exit_code = EXIT_INTERNAL


class ConfigError(FungiFunkError):
    exit_code = EXIT_CONFIG


class CurationConflict(FungiFunkError):
    """A biological curation conflict that may be fixed and resumed safely."""

    exit_code = EXIT_CURATION_WAIT

    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class ResourceSafetyError(FungiFunkError):
    exit_code = EXIT_RESOURCE


class ExternalToolError(FungiFunkError):
    exit_code = EXIT_EXTERNAL


class NCBIRequestError(ExternalToolError):
    """An NCBI Datasets request failed after the configured retries.

    ``reason`` is a stable machine-readable category that can be propagated
    through worker results and final reports.

    ``detail`` preserves the human-readable command/error information needed
    for diagnostics.

    This exception describes a retrieval failure. It must not be interpreted
    as evidence that the requested biological data do not exist.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        returncode: int | None = None,
    ):
        if reason not in NCBI_RETRIEVAL_FAILURE_REASONS:
            raise ValueError(
                f"Unknown NCBI request failure reason: {reason}"
            )

        super().__init__(detail)

        self.reason = reason
        self.detail = detail
        self.returncode = returncode


class DataIntegrityError(FungiFunkError):
    exit_code = EXIT_INTEGRITY


class HashCollisionError(DataIntegrityError):
    """A cryptographic hash matched but the full canonical sequences differed.

    This is deliberately a separate fatal error. Ordinary corrupt/missing
    assembly packages may be excluded and reported, but a SHA-256 collision in
    our canonical sequence index would invalidate the deduplication invariant and
    must stop the entire build for manual integrity review.
    """


########################################################################################################
# General utility helpers
########################################################################################################

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return dt.date.today().isoformat()


# Console timestamps are a run-local presentation setting.
# They never affect biological configuration or reference contents.
CONSOLE_TIMESTAMPS = False


def set_console_timestamps(enabled: bool) -> None:
    """Enable or disable timestamps for main-process console messages."""

    global CONSOLE_TIMESTAMPS
    CONSOLE_TIMESTAMPS = bool(enabled)


def timestamp_console_message(
    message: str,
    *,
    timestamps: bool | None = None,
) -> str:
    """Prefix one logical console message with the current local time.

    Multi-line messages receive one timestamp on their first line only.
    Blank messages remain blank.
    """

    enabled = (
        CONSOLE_TIMESTAMPS
        if timestamps is None
        else bool(timestamps)
    )

    if not enabled or not message:
        return message

    return (
        f"[{dt.datetime.now().strftime('%H:%M:%S')}] "
        f"{message}"
    )


def console_print(
    message: str = "",
    *,
    file: Any = sys.stdout,
    timestamps: bool | None = None,
) -> None:
    """Print one logical console message with optional runtime timestamping."""

    print(
        timestamp_console_message(
            message,
            timestamps=timestamps,
        ),
        file=file,
        flush=True,
    )


def info(message: str) -> None:
    console_print(
        f"[INFO] {message}"
    )


def warn(message: str) -> None:
    console_print(
        f"[WARNING] {message}",
        file=sys.stderr,
    )


def error(message: str) -> None:
    console_print(
        f"[ERROR] {message}",
        file=sys.stderr,
    )


def recovery_work_root(ns: argparse.Namespace | None) -> Path | None:
    """Return the directory whose persistent state should guide recovery.

    For ordinary create/resume/update-copy runs, this is --outdir.

    For an overwrite update, work happens in the safe sibling staging
    directory rather than directly inside the existing reference.
    """

    if ns is None:
        return None

    outdir = getattr(ns, "outdir", None)

    if not outdir:
        return None

    target = Path(outdir).expanduser().resolve()

    if getattr(ns, "overwrite_with_updated_version", False):

        staging = overwrite_staging_path(target)

        if staging.exists():
            return staging

    return target


def read_recovery_state(
    root: Path,
) -> tuple[str | None, str | None]:
    """Read global_state and build_origin_mode without creating/modifying state."""

    state_path = root / STATE_DB_REL

    if not state_path.is_file():
        return None, None

    try:
        connection = sqlite3.connect(
            f"file:{state_path.resolve()}?mode=ro",
            uri=True,
        )

        try:
            rows = dict(
                connection.execute(
                    """
                    SELECT key, value
                    FROM meta
                    WHERE key IN ('global_state', 'build_origin_mode')
                    """
                ).fetchall()
            )

        finally:
            connection.close()

    except sqlite3.Error:
        return None, None

    return (
        rows.get("global_state"),
        rows.get("build_origin_mode"),
    )


ASSEMBLY_SEARCH_STARTED_STATES = {
    "ASSEMBLY_SEARCH_AND_SELECTION",
    "ASSEMBLY_SEARCH_AND_SELECTION_COMPLETE",
    "UPDATE_COMPARISON_AND_SELECTION",
    "UPDATE_COMPARISON_AND_SELECTION_COMPLETE",
    "PROTEIN_RETRIEVAL_AND_PROCESSING",
    "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE",
    "GLOBAL_EXACT_DEDUPLICATION",
    "GLOBAL_EXACT_DEDUPLICATION_COMPLETE",
    "FINAL_OUTPUT_WRITING",
    "FINAL_OUTPUT_WRITING_COMPLETE",
    "FINAL_REFERENCE_VALIDATION",
    "FINAL_REFERENCE_VALIDATION_COMPLETE",
    "FINAL_REPORT_WRITING",
    "FINAL_REPORT_WRITING_COMPLETE",
    "FINAL_CHECKPOINT",
    "FINAL_CHECKPOINT_COMPLETE",
    "DONE",
}


PROTEIN_PROCESSING_STARTED_STATES = {
    "PROTEIN_RETRIEVAL_AND_PROCESSING",
    "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE",
    "GLOBAL_EXACT_DEDUPLICATION",
    "GLOBAL_EXACT_DEDUPLICATION_COMPLETE",
    "FINAL_OUTPUT_WRITING",
    "FINAL_OUTPUT_WRITING_COMPLETE",
    "FINAL_REFERENCE_VALIDATION",
    "FINAL_REFERENCE_VALIDATION_COMPLETE",
    "FINAL_REPORT_WRITING",
    "FINAL_REPORT_WRITING_COMPLETE",
    "FINAL_CHECKPOINT",
    "FINAL_CHECKPOINT_COMPLETE",
    "DONE",
}


def report_recovery_guidance(
    ns: argparse.Namespace | None,
) -> None:
    """Explain recovery using the persistent state of the interrupted build."""

    root = recovery_work_root(ns)

    if root is None:
        return

    # If FungiFunk never initialized this directory, there is no persistent
    # build recovery state to explain.
    if not (root / MARKER_REL).is_file():
        return

    resume_ready = (root / RESUME_READY_REL).is_file()

    console_print(
        "",
        file=sys.stderr,
    )
    console_print(
        "[RECOVERY]",
        file=sys.stderr,
    )

    # --------------------------------------------------------------
    # Recognized FungiFunk directory, but initialization stopped too
    # early to create a safe resume checkpoint.
    # --------------------------------------------------------------
    if not resume_ready:

        console_print(
            "This run stopped before a resumable FungiFunk_refdb state "
            "was created.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "--resume cannot be used for this run because no persistent "
            "recovery checkpoint is available yet.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Correct the problem reported above, remove the incomplete output "
            "directory (or its contents) manually, and start the build again "
            "with --create.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "FungiFunk_refdb will not remove the incomplete directory "
            "automatically.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print("Recovery summary:", file=sys.stderr)
        console_print("  Resumable state available: no", file=sys.stderr)
        console_print(
            "  Biological configuration editable: not applicable",
            file=sys.stderr,
        )
        console_print(
            "  Assembly search/selection started: no",
            file=sys.stderr,
        )
        console_print(
            "  Protein retrieval/processing started: no",
            file=sys.stderr,
        )
        console_print(
            "  Recommended action: clean incomplete output and run "
            "--create again",
            file=sys.stderr,
        )

        return

    current_state, origin_mode = read_recovery_state(root)

    # A resume-ready marker without readable persistent state is inconsistent.
    if current_state is None:

        console_print(
            "A resumable-state marker exists, but the persistent SQLite build "
            "state could not be read.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Automatic recovery guidance cannot safely determine which build "
            "phase was committed. Preserve the directory and investigate the "
            "state/integrity error reported above rather than deleting or "
            "overwriting it.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print("Recovery summary:", file=sys.stderr)
        console_print(
            "  Current build state: unavailable",
            file=sys.stderr,
        )
        console_print(
            "  Resumable state available: marker present, persistent state unreadable",
            file=sys.stderr,
        )
        console_print(
            "  Biological configuration editable: unknown",
            file=sys.stderr,
        )
        console_print(
            "  Recommended action: preserve the build and investigate "
            "persistent-state integrity",
            file=sys.stderr,
        )

        return

    completion_marker = root / COMPLETION_MARKER_REL

    # --------------------------------------------------------------
    # A completion marker exists. Verify it before telling the user
    # that the reference is genuinely complete.
    # --------------------------------------------------------------
    if completion_marker.is_file():

        try:
            verify_completed_reference(root)

        except FungiFunkError:

            console_print(
                "A final completion marker exists, but the completed-reference "
                "integrity check is not valid.",
                file=sys.stderr,
            )
            console_print(
                "",
                file=sys.stderr,
            )
            console_print(
                "Automatic resume should not be used to bypass this integrity "
                "problem. Preserve the reference and investigate the integrity "
                "error reported above.",
                file=sys.stderr,
            )
            console_print(
                "",
                file=sys.stderr,
            )
            console_print("Recovery summary:", file=sys.stderr)
            console_print(
                f"  Current build state: {current_state}",
                file=sys.stderr,
            )
            console_print(
                "  Resumable state available: automatic recovery stopped",
                file=sys.stderr,
            )
            console_print(
                "  Biological configuration editable: no",
                file=sys.stderr,
            )
            console_print(
                "  Recommended action: investigate final-reference integrity",
                file=sys.stderr,
            )

            return

        if origin_mode == "overwrite-with-updated-version":

            console_print(
                "The staged overwrite reference is fully built and validated, "
                "but the safe replacement step may have been interrupted.",
                file=sys.stderr,
            )
            console_print(
                "",
                file=sys.stderr,
            )
            console_print(
                "Continue the existing staging build with:",
                file=sys.stderr,
            )
            console_print(
                f"  FungiFunk_refdb.sh --resume --outdir {root}",
                file=sys.stderr,
            )
            console_print(
                "",
                file=sys.stderr,
            )
            console_print("Recovery summary:", file=sys.stderr)
            console_print(
                f"  Current build state: {current_state}",
                file=sys.stderr,
            )
            console_print(
                "  Resumable state available: yes",
                file=sys.stderr,
            )
            console_print(
                "  Biological configuration editable: no",
                file=sys.stderr,
            )
            console_print(
                "  Assembly search/selection started: yes",
                file=sys.stderr,
            )
            console_print(
                "  Protein retrieval/processing started: yes",
                file=sys.stderr,
            )
            console_print(
                "  Recommended action: rerun --resume on the staging directory",
                file=sys.stderr,
            )

            return

        console_print(
            "This reference is already complete and its final integrity "
            "checkpoint is valid.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "--resume is not needed. Use an update mode if a new database "
            "version is required.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print("Recovery summary:", file=sys.stderr)
        console_print(
            f"  Current build state: {current_state}",
            file=sys.stderr,
        )
        console_print(
            "  Resumable state available: not applicable; reference complete",
            file=sys.stderr,
        )
        console_print(
            "  Biological configuration editable: no",
            file=sys.stderr,
        )
        console_print(
            "  Recommended action: use an update mode for a new version",
            file=sys.stderr,
        )

        return

    # --------------------------------------------------------------
    # Pre-assembly recovery: biology is still editable.
    # --------------------------------------------------------------
    if current_state in PRE_ASSEMBLY_BIOLOGICAL_EDITABLE_STATES:

        console_print(
            "This build has a resumable state and assembly search/selection "
            "has not started.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Starting again with --create is not necessary. Correct the "
            "problem and continue the existing build with --resume.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Biological configuration may still be changed at this stage.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "It determines which fungal taxa and genome assemblies can "
            "contribute proteins to the reference database. For the complete "
            "option list, see BIOLOGICAL CONFIGURATION in --help.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "To change only selected biological settings, supply those options "
            "with --resume. Settings not supplied are preserved.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "To reset the entire biological configuration to clean defaults, "
            "use --reset-biological-config together with --seed-taxa.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "If biological configuration changes, FungiFunk_refdb clears any "
            "existing pre-assembly curation and rebuilds TAXONOMY_CURATION "
            "from the beginning. No assembly/protein processing is lost.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "--db-version remains locked because it identifies this existing "
            "build.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Resume command:",
            file=sys.stderr,
        )
        console_print(
            f"  FungiFunk_refdb.sh --resume --outdir {root}",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print("Recovery summary:", file=sys.stderr)
        console_print(
            f"  Current build state: {current_state}",
            file=sys.stderr,
        )
        console_print(
            "  Resumable state available: yes",
            file=sys.stderr,
        )
        console_print(
            "  Biological configuration editable: yes",
            file=sys.stderr,
        )
        console_print(
            "  Assembly search/selection started: no",
            file=sys.stderr,
        )
        console_print(
            "  Protein retrieval/processing started: no",
            file=sys.stderr,
        )
        console_print(
            "  Recommended action: correct inputs/configuration if needed "
            "and run --resume",
            file=sys.stderr,
        )

        return

    # --------------------------------------------------------------
    # Post-assembly recovery: biological selection is committed.
    # --------------------------------------------------------------
    assembly_started = current_state in ASSEMBLY_SEARCH_STARTED_STATES
    protein_started = current_state in PROTEIN_PROCESSING_STARTED_STATES

    console_print(
        "This build has a resumable state.",
        file=sys.stderr,
    )
    console_print(
        "",
        file=sys.stderr,
    )

    if assembly_started:

        console_print(
            "ASSEMBLY_SEARCH_AND_SELECTION has already started, so "
            "biological configuration is locked for this build.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Resume reuses the committed biological configuration and "
            "previously completed work.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "Computational/presentation options such as --workers, --force, "
            "--tmpdir, --verbose and --timestamps may still be changed where "
            "applicable.",
            file=sys.stderr,
        )
        console_print(
            "",
            file=sys.stderr,
        )
        console_print(
            "If the biological configuration must change, start a new build "
            "with --create using a new or manually cleaned output directory.",
            file=sys.stderr,
        )

    console_print(
        "",
        file=sys.stderr,
    )
    console_print(
        "Resume command:",
        file=sys.stderr,
    )
    console_print(
        f"  FungiFunk_refdb.sh --resume --outdir {root}",
        file=sys.stderr,
    )
    console_print(
        "",
        file=sys.stderr,
    )
    console_print(
        "Recovery summary:",
        file=sys.stderr,
    )
    console_print(
        f"  Current build state: {current_state}",
        file=sys.stderr,
    )
    console_print(
        "  Resumable state available: yes",
        file=sys.stderr,
    )
    console_print(
        "  Biological configuration editable: no",
        file=sys.stderr,
    )
    console_print(
        "  Assembly search/selection started: "
        + ("yes" if assembly_started else "no"),
        file=sys.stderr,
    )
    console_print(
        "  Protein retrieval/processing started: "
        + ("yes" if protein_started else "no"),
        file=sys.stderr,
    )
    console_print(
        "  Recommended action: rerun --resume with existing biological "
        "configuration",
        file=sys.stderr,
    )


def print_section_banner(title: str) -> None:
    """Display a readable section title without changing state identifiers."""
    readable_title = title.strip().replace("_", " ").upper()
    line = f" {readable_title} ".center(100, "#")
    print(line, flush=True)


def print_section_separator() -> None:
    """Visually separate a completed phase's summary from its progress log."""
    print("-" * 10, flush=True)


def atomic_replace(src: Path, dst: Path) -> None:
    """Atomically replace a path when src/dst are on the same filesystem."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so preceding renames survive a hard crash."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    # MD5 is used only to verify NCBI package checksums, never as a biological ID.
    digest = hashlib.md5()  # noqa: S324 - integrity compatibility with NCBI md5sum.txt
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def human_gib(n_bytes: int | float) -> str:
    return f"{float(n_bytes) / (1024 ** 3):.2f} GiB"


def normalize_missing(value: Any) -> Any | None:
    """Normalize common metadata missing representations to None.

    Important: this applies to metadata fields only. Protein sequences are never
    passed through this function.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in {"", ".", "na", "n/a", "null", "none"}:
            return None
        return stripped
    return value


def normalize_assembly_status(value: Any) -> str | None:
    """Normalize NCBI assembly-status terminology to stable internal values.

    NCBI uses slightly different terminology across interfaces and historical
    representations. In particular, the conceptual/legacy term "replaced"
    corresponds to "previous" in the current NCBI Datasets genome report,
    while "latest" corresponds to "current".

    Unknown non-missing values are preserved rather than silently discarded so
    they remain visible in metadata and can be handled explicitly if NCBI
    introduces additional statuses in the future.
    """

    raw = normalize_missing(value)

    if raw is None:
        return None

    status = str(raw).strip().lower()

    aliases = {
        "latest": "current",
        "replaced": "previous",
    }

    return aliases.get(status, status)


def parse_bool(value: Any, *, field_name: str = "boolean") -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    raise ConfigError(f"{field_name} must be TRUE/FALSE; received {value!r}.")


def parse_float(value: Any, field_name: str) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be numeric; received {value!r}.") from exc


def parse_iso_date(value: Any, field_name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{field_name} must use YYYY-MM-DD; received {value!r}.") from exc


def clean_species_for_header(name: str) -> str:
    # Preserve informative punctuation other than whitespace; the first FASTA
    # token must never contain spaces.
    return re.sub(r"\s+", "_", name.strip())


def safe_component(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return cleaned.strip("._") or "unnamed"


def split_semicolon_values(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.split(";") if item.strip() and item.strip() != "."]


def merge_text_values(values: Iterable[str | None]) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in split_semicolon_values(value):
            if item not in seen:
                seen.add(item)
                unique.append(item)
    return ";".join(unique) if unique else "."


def bool_tsv(value: bool | None) -> str:
    if value is None:
        return "."
    return "TRUE" if value else "FALSE"


def value_tsv(value: Any) -> str:
    value = normalize_missing(value)
    if value is None:
        return "."
    if isinstance(value, bool):
        return bool_tsv(value)
    return str(value)


def get_available_ram_bytes() -> int:
    if psutil is not None:
        return int(psutil.virtual_memory().available)
    # Linux fallback.
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    # POSIX fallback.
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return int(pages * page_size)
        except (ValueError, OSError):
            pass
    raise ResourceSafetyError("Could not determine available RAM safely on this system.")


def get_cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def ensure_disk_reserve(path: Path, *, context: str) -> None:
    usage = shutil.disk_usage(path)
    reserve = int(EMERGENCY_FREE_DISK_GIB * 1024 ** 3)
    if usage.free < reserve:
        raise ResourceSafetyError(
            f"Emergency disk safeguard triggered during {context}: {path} has "
            f"only {human_gib(usage.free)} free; at least {EMERGENCY_FREE_DISK_GIB:.1f} GiB "
            "must remain free. The build is checkpointed and may be resumed after freeing space."
        )


def write_tsv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: value_tsv(row.get(field)) for field in fieldnames})
        fsync_file(handle)
    atomic_replace(part, path)


def write_xlsx_from_tsv(tsv_path: Path, xlsx_path: Path) -> None:
    """Write an optional XLSX copy of a TSV metadata table."""

    # XLSX files are convenience exports. If openpyxl is not available,
    # keep the TSV output and simply skip the XLSX copy.
    if Workbook is None:

        # Remove a previous XLSX copy, if present, so a resumed build cannot
        # leave an outdated XLSX beside a newly generated TSV.
        if xlsx_path.exists():
            xlsx_path.unlink()

        return

    # Create the XLSX workbook in write-only mode to reduce memory use.
    wb = Workbook(write_only=True)

    # Create the worksheet that will contain the TSV data.
    ws = wb.create_sheet(title="FungiFunk")

    # Copy every TSV row directly into the XLSX worksheet.
    with tsv_path.open("r", encoding="utf-8", newline="") as handle:

        reader = csv.reader(handle, delimiter="\t")

        for row in reader:
            ws.append(row)

    # Write to a temporary file first so an interrupted save cannot leave
    # a partially written XLSX at the final output path.
    part = xlsx_path.with_name(xlsx_path.name + ".part.xlsx")

    wb.save(part)

    # Move the completed temporary XLSX into its final location.
    atomic_replace(part, xlsx_path)


def read_commentable_tsv(
    path: Path,
    required_columns: Sequence[str],
    *,
    allow_no_data_rows: bool = False,
) -> list[dict[str, str]]:
    """Read a TSV allowing blank lines and comments beginning with '#'.

    By default, a valid header and at least one active data row are required.

    Callers whose semantics allow an empty rule set may set
    allow_no_data_rows=True. In that mode, a completely empty file, a file
    containing only blank/comment lines, or a valid header with no active data
    rows returns an empty list.

    Non-empty malformed files are still rejected rather than silently treated
    as empty.
    """

    if not path.is_file():
        raise ConfigError(
            f"Input TSV does not exist: {path}"
        )

    logical_lines: list[str] = []

    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            if not raw.strip():
                continue

            if raw.lstrip().startswith("#"):
                continue

            logical_lines.append(raw)

    # An entirely empty/comment-only file is a valid empty rule set only for
    # callers that explicitly allow that semantic.
    if not logical_lines:
        if allow_no_data_rows:
            return []

        raise ConfigError(
            f"Input TSV contains no header/data rows after comments/blank lines: "
            f"{path}"
        )

    reader = csv.DictReader(
        logical_lines,
        delimiter="\t",
    )

    if reader.fieldnames is None:
        raise ConfigError(
            f"Could not read a TSV header from: {path}"
        )

    missing = [
        col
        for col in required_columns
        if col not in reader.fieldnames
    ]

    if missing:
        raise ConfigError(
            f"{path} is missing required column(s): {', '.join(missing)}. "
            f"Detected: {', '.join(reader.fieldnames)}"
        )

    rows: list[dict[str, str]] = []

    for idx, raw_row in enumerate(reader, start=2):
        row = {
            k: (v or "").strip()
            for k, v in raw_row.items()
            if k is not None
        }

        if not any(row.values()):
            continue

        row["__line__"] = str(idx)
        rows.append(row)

    if not rows and not allow_no_data_rows:
        raise ConfigError(
            f"Input TSV contains no active data rows: {path}"
        )

    return rows


def first_nonmissing(*values: Any) -> Any:
    for value in values:
        normalized = normalize_missing(value)
        if normalized is not None:
            return normalized
    return None


def run_capture(cmd: Sequence[str], *, env: Mapping[str, str] | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env) if env is not None else None,
        timeout=timeout,
        check=False,
    )


########################################################################################################
# CLI / configuration
########################################################################################################

@dataclass
class BuildConfig:
    run_mode: str
    outdir: str
    source_refdb: str | None = None
    reset_biological_config: bool = False
    seed_taxa: str | None = None
    exclude_taxa: str | None = None
    metadata_filters: str | None = None
    include_explicit_targets: str | None = None
    exclude_explicit_targets: str | None = None
    tmpdir: str | None = None
    db_version: str | None = None
    workers: str | int = "auto"
    force: bool = False
    verbose: bool = False
    timestamps: bool = False

    # Assembly source/scope and hard/default eligibility.
    include_gca: bool = False
    include_atypical: bool = False
    require_busco: bool = False

    # Assembly-selection depth.
    #
    # Default mode is strain-level:
    #   - all strain groups are considered
    #   - one usable assembly is retained per strain
    #
    # --max-assemblies-per-species activates the alternative compact
    # species-level selection mode.
    max_assemblies_per_species: int | None = None
    max_assemblies_per_strain: int | None = 1
    max_strains_per_species: int | None = None  # None == all

    # Optional depth expansions in strain-level mode.
    #
    # Isolates contribute one best usable assembly per included isolate.
    # Isolates attached to a strain are expanded only for represented strains;
    # isolate-only records are represented independently when enabled.
    # Unassigned records contribute one best usable assembly per BioSample,
    # or per assembly accession when BioSample is unavailable.
    include_isolates: bool = False
    include_unassigned_assemblies: bool = False

    min_contig_n50: int | None = None
    max_contig_l50: int | None = None
    max_contigs: int | None = None
    min_scaffold_n50: int | None = None
    max_scaffold_l50: int | None = None
    max_scaffolds: int | None = None
    max_scaffold_gaps: int | None = None
    min_genome_size: int | None = None
    max_genome_size: int | None = None
    min_gc_percent: float | None = None
    max_gc_percent: float | None = None
    min_protein_coding_genes: int | None = None
    max_protein_coding_genes: int | None = None
    min_busco_complete: float | None = None
    max_busco_missing: float | None = None
    max_busco_fragmented: float | None = None
    max_busco_duplicated: float | None = None
    assembly_level: list[str] = field(default_factory=list)
    released_after: str | None = None
    released_before: str | None = None
    refseq_category: str | None = None
    assembly_method_contains: list[str] = field(default_factory=list)
    sequencing_technology_contains: list[str] = field(default_factory=list)

    # These are execution/provenance fields resolved at startup.
    build_started: str = field(default_factory=now_iso)
    ncbi_datasets_version: str | None = None
    ncbi_api_key_detected: bool = False
    ncbi_request_rate: float = NCBI_RPS_WITHOUT_KEY

    def to_jsonable(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


BIOLOGICAL_ARG_FIELDS = {
    "include_gca",
    "include_isolates",
    "include_unassigned_assemblies",
    "max_assemblies_per_strain",
    "max_strains_per_species",
    "include_atypical",
    "require_busco",
    "min_contig_n50",
    "max_contig_l50",
    "max_contigs",
    "min_scaffold_n50",
    "max_scaffold_l50",
    "max_scaffolds",
    "max_scaffold_gaps",
    "min_genome_size",
    "max_genome_size",
    "min_gc_percent",
    "max_gc_percent",
    "min_protein_coding_genes",
    "max_protein_coding_genes",
    "min_busco_complete",
    "max_busco_missing",
    "max_busco_fragmented",
    "max_busco_duplicated",
    "assembly_level",
    "released_after",
    "released_before",
    "refseq_category",
    "assembly_method_contains",
    "sequencing_technology_contains",
    "max_assemblies_per_species",
}

# TSV inputs that participate directly in biological/reference selection.
BIOLOGICAL_INPUT_ARG_FIELDS = {
    "seed_taxa",
    "exclude_taxa",
    "metadata_filters",
    "include_explicit_targets",
    "exclude_explicit_targets",
}


# Presentation groups used only when writing biological configuration to the
# wrapper run-information log. BIOLOGICAL_ARG_FIELDS remains the authoritative
# set of biological configuration fields.
BIOLOGICAL_LOG_GROUPS = (
    (
        "Assembly scope",
        (
            "include_gca",
            "include_atypical",
        ),
    ),
    (
        "Assembly QC / metadata",
        (
            "require_busco",
            "min_contig_n50",
            "max_contig_l50",
            "max_contigs",
            "min_scaffold_n50",
            "max_scaffold_l50",
            "max_scaffolds",
            "max_scaffold_gaps",
            "min_genome_size",
            "max_genome_size",
            "min_gc_percent",
            "max_gc_percent",
            "min_protein_coding_genes",
            "max_protein_coding_genes",
            "min_busco_complete",
            "max_busco_missing",
            "max_busco_fragmented",
            "max_busco_duplicated",
            "assembly_level",
            "released_after",
            "released_before",
            "refseq_category",
            "assembly_method_contains",
            "sequencing_technology_contains",
        ),
    ),
    (
        "Assembly selection / representation",
        (
            "max_assemblies_per_strain",
            "max_strains_per_species",
            "include_isolates",
            "include_unassigned_assemblies",
            "max_assemblies_per_species",
        ),
    ),
)

BIOLOGICAL_BOOLEAN_ARG_FIELDS = {
    "include_gca",
    "include_isolates",
    "include_unassigned_assemblies",
    "include_atypical",
    "require_busco",
}


# All biological configuration arguments that may be corrected during a
# pre-assembly resume. Supplying any of these explicitly means that the previous
# pre-assembly curation must not be reused.
BIOLOGICAL_RESUME_ARG_FIELDS = (
    BIOLOGICAL_ARG_FIELDS
    | BIOLOGICAL_INPUT_ARG_FIELDS
)


# db_version is provenance/build identity rather than biological selection.
# Unlike biological configuration, it is locked during EVERY --resume,
# including pre-assembly recovery.
RESUME_ALWAYS_LOCKED_ARG_FIELDS = {
    "db_version",
}


# Biological configuration may be changed during the states listed below.
# Once ASSEMBLY_SEARCH_AND_SELECTION begins, it is locked for the build.
PRE_ASSEMBLY_BIOLOGICAL_EDITABLE_STATES = {
    "INITIALIZING_COMPLETE",
    "TAXONOMY_CURATION",
    "WAITING_FOR_CURATION_FIX",
    "TAXONOMY_CURATION_COMPLETE",
}


# Once taxonomy curation has completed, its committed species state may be
# reused by resume.
CURATION_COMMITTED_STATES = {
    "TAXONOMY_CURATION_COMPLETE",
    "ASSEMBLY_SEARCH_AND_SELECTION",
    "ASSEMBLY_SEARCH_AND_SELECTION_COMPLETE",
    "UPDATE_COMPARISON_AND_SELECTION",
    "UPDATE_COMPARISON_AND_SELECTION_COMPLETE",
    "PROTEIN_RETRIEVAL_AND_PROCESSING",
    "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE",
    "GLOBAL_EXACT_DEDUPLICATION",
    "GLOBAL_EXACT_DEDUPLICATION_COMPLETE",
    "FINAL_OUTPUT_WRITING",
    "FINAL_OUTPUT_WRITING_COMPLETE",
    "FINAL_REFERENCE_VALIDATION",
    "FINAL_REFERENCE_VALIDATION_COMPLETE",
    "FINAL_REPORT_WRITING",
    "FINAL_REPORT_WRITING_COMPLETE",
    "FINAL_CHECKPOINT",
    "FINAL_CHECKPOINT_COMPLETE",
    "DONE",
}


# A completed assembly-search/selection phase may be reused directly.
# The in-progress state is deliberately absent because a partial assembly
# search/selection is rebuilt from scratch before protein processing resumes.
ASSEMBLY_SELECTION_COMMITTED_STATES = {
    "ASSEMBLY_SEARCH_AND_SELECTION_COMPLETE",
    "UPDATE_COMPARISON_AND_SELECTION",
    "UPDATE_COMPARISON_AND_SELECTION_COMPLETE",
    "PROTEIN_RETRIEVAL_AND_PROCESSING",
    "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE",
    "GLOBAL_EXACT_DEDUPLICATION",
    "GLOBAL_EXACT_DEDUPLICATION_COMPLETE",
    "FINAL_OUTPUT_WRITING",
    "FINAL_OUTPUT_WRITING_COMPLETE",
    "FINAL_REFERENCE_VALIDATION",
    "FINAL_REFERENCE_VALIDATION_COMPLETE",
    "FINAL_REPORT_WRITING",
    "FINAL_REPORT_WRITING_COMPLETE",
    "FINAL_CHECKPOINT",
    "FINAL_CHECKPOINT_COMPLETE",
    "DONE",
}


# An update comparison is reusable only after that phase was committed as
# complete. If UPDATE_COMPARISON_AND_SELECTION itself was interrupted, the
# comparison is rebuilt from scratch.
UPDATE_COMPARISON_COMMITTED_STATES = {
    "UPDATE_COMPARISON_AND_SELECTION_COMPLETE",
    "PROTEIN_RETRIEVAL_AND_PROCESSING",
    "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE",
    "GLOBAL_EXACT_DEDUPLICATION",
    "GLOBAL_EXACT_DEDUPLICATION_COMPLETE",
    "FINAL_OUTPUT_WRITING",
    "FINAL_OUTPUT_WRITING_COMPLETE",
    "FINAL_REFERENCE_VALIDATION",
    "FINAL_REFERENCE_VALIDATION_COMPLETE",
    "FINAL_REPORT_WRITING",
    "FINAL_REPORT_WRITING_COMPLETE",
    "FINAL_CHECKPOINT",
    "FINAL_CHECKPOINT_COMPLETE",
    "DONE",
}


def explicit_resume_biological_options(ns: argparse.Namespace) -> list[str]:
    """Return biological options explicitly supplied in this invocation."""

    explicit = namespace_explicit_values(ns)

    options = [
        "--" + name.replace("_", "-")
        for name in sorted(BIOLOGICAL_RESUME_ARG_FIELDS)
        if explicit.get(name) is not None
    ]

    if bool(ns.reset_biological_config):
        options.append("--reset-biological-config")

    return options


def positive_int_or_all(value: str) -> int | str:
    """Parse a positive integer or the literal 'all'."""

    text = str(value).strip()

    if text.lower() == "all":
        return "all"

    try:
        parsed = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a positive integer or 'all'"
        ) from exc

    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            "must be a positive integer or 'all'"
        )

    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Authoritative Python implementation for FungiFunk_refdb.",
    )
    p.add_argument("--version", action="version", version=f"{TOOL_NAME} v{VERSION}", help="Show FungiFunk_refdb version and exit.",)

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--create", action="store_true")
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--create-new-updated-version", action="store_true")
    mode.add_argument("--overwrite-with-updated-version", action="store_true")
    mode.add_argument("--retry-ncbi-failures", action="store_true")

    p.add_argument("-o", "--outdir")
    p.add_argument("--source-refdb")
    p.add_argument("--seed-taxa")
    p.add_argument("--exclude-taxa")
    p.add_argument("--metadata-filters")
    p.add_argument(
        "--include-explicit-targets",
        metavar="FILE",
        help=(
            "TSV of explicit strain or exact assembly inclusion targets. "
            "Targets are additional to ordinary automatic selection limits. "
            "May be used without --seed-taxa to drive a target-only create."
        ),
    )
    p.add_argument(
        "--exclude-explicit-targets",
        metavar="FILE",
        help=(
            "TSV of explicit strain or exact assembly exclusion targets. "
            "Excluded targets cannot be selected and do not consume automatic "
            "selection quota."
        ),
    )
    p.add_argument("--tmpdir")
    p.add_argument("--db-version")
    p.add_argument(
        "--reset-biological-config",
        action="store_true",
        default=False,
        help=(
            "Discard inherited biological configuration, "
            "restore clean defaults, and then apply the biological options supplied "
            "in the current command. Requires at least one of --seed-taxa or "
            "--include-explicit-targets. Available for update modes; pre-assembly "
            "resume support is validated from persistent build state."
        ),
    )
    p.add_argument("--workers", default=None)
    p.add_argument("--force", action="store_true", default=False)
    p.add_argument("--verbose", action="store_true", default=False)
    p.add_argument(
        "--timestamps",
        action="store_true",
        help=(
            "Prefix textual runtime console messages with the current local "
            "time in [HH:MM:SS] format."
        ),
    )

    # Boolean biological flags use default=None so update/inherit can distinguish
    # "not supplied" from "explicitly enabled in this command".
    p.add_argument("--include-gca", action="store_true", default=None)
    p.add_argument(
        "--include-isolates",
        action="store_true",
        default=None,
        help=(
            "Expand strain-level selection to isolate-level representation. "
            "One best usable assembly is retained per included isolate."
        ),
    )

    p.add_argument(
        "--include-unassigned-assemblies",
        action="store_true",
        default=None,
        help=(
            "Also represent assemblies with neither strain nor isolate metadata. "
            "Assemblies sharing a BioSample are grouped and represented by one "
            "best usable assembly."
        ),
    )
    p.add_argument("--include-atypical", action="store_true", default=None)
    p.add_argument("--require-busco", action="store_true", default=None)

    for option in [
        "min-contig-n50", "max-contig-l50", "max-contigs", "min-scaffold-n50",
        "max-scaffold-l50", "max-scaffolds", "max-scaffold-gaps", "min-genome-size",
        "max-genome-size", "min-protein-coding-genes", "max-protein-coding-genes",
        "max-assemblies-per-species",
        "max-assemblies-per-strain",
    ]:
        p.add_argument(f"--{option}", type=int, default=None)
    p.add_argument(
        "--max-strains-per-species",
        type=positive_int_or_all,
        default=None,
    )
    for option in [
        "min-gc-percent", "max-gc-percent", "min-busco-complete", "max-busco-missing",
        "max-busco-fragmented", "max-busco-duplicated",
    ]:
        p.add_argument(f"--{option}", type=float, default=None)

    p.add_argument("--assembly-level", action="append", default=None)
    p.add_argument("--released-after", default=None)
    p.add_argument("--released-before", default=None)
    p.add_argument("--refseq-category", default=None)
    p.add_argument("--assembly-method-contains", action="append", default=None)
    p.add_argument("--sequencing-technology-contains", action="append", default=None)
    return p


def detect_run_mode(ns: argparse.Namespace) -> str:
    if ns.create:
        return "create"
    if ns.resume:
        return "resume"
    if ns.create_new_updated_version:
        return "create-new-updated-version"
    if ns.overwrite_with_updated_version:
        return "overwrite-with-updated-version"
    if ns.retry_ncbi_failures:
        return "retry-ncbi-failures"
    raise ConfigError("Exactly one run mode is required.")


def normalize_assembly_levels(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    accepted = {
        "complete": "Complete Genome",
        "complete genome": "Complete Genome",
        "chromosome": "Chromosome",
        "scaffold": "Scaffold",
        "contig": "Contig",
    }
    result: list[str] = []
    for raw in values:
        for token in raw.split(","):
            key = token.strip().lower()
            if key not in accepted:
                raise ConfigError(
                    f"Unsupported --assembly-level value {token!r}. Allowed: complete, chromosome, scaffold, contig."
                )
            canonical = accepted[key]
            if canonical not in result:
                result.append(canonical)
    return result


def validate_thresholds(values: Mapping[str, Any]) -> None:
    nonnegative = [
        "min_contig_n50",
        "max_contig_l50",
        "max_contigs",
        "min_scaffold_n50",
        "max_scaffold_l50",
        "max_scaffolds",
        "max_scaffold_gaps",
        "min_genome_size",
        "max_genome_size",
        "min_protein_coding_genes",
        "max_protein_coding_genes",
    ]

    for name in nonnegative:
        if values.get(name) is not None and int(values[name]) < 0:
            raise ConfigError(
                f"--{name.replace('_', '-')} must be >= 0."
            )

    # Assembly-selection limits are positive when supplied.
    #
    # max_strains_per_species=None means "all strains".
    # max_assemblies_per_species=None means species-level compact mode is inactive.
    # max_assemblies_per_strain=None means not applicable while compact
    # species-level mode is active.
    for name in [
        "max_assemblies_per_species",
        "max_assemblies_per_strain",
        "max_strains_per_species",
    ]:
        value = values.get(name)

        if value is not None and int(value) <= 0:
            raise ConfigError(
                f"--{name.replace('_', '-')} must be > 0."
            )

    for name in [
        "min_gc_percent",
        "max_gc_percent",
        "min_busco_complete",
        "max_busco_missing",
        "max_busco_fragmented",
        "max_busco_duplicated",
    ]:
        val = values.get(name)

        if val is not None and not 0.0 <= float(val) <= 100.0:
            raise ConfigError(
                f"--{name.replace('_', '-')} must be between 0 and 100."
            )

    pairs = [
        ("min_genome_size", "max_genome_size"),
        ("min_gc_percent", "max_gc_percent"),
        ("min_protein_coding_genes", "max_protein_coding_genes"),
    ]

    for low, high in pairs:
        if (
            values.get(low) is not None
            and values.get(high) is not None
            and values[low] > values[high]
        ):
            raise ConfigError(
                f"Contradictory thresholds: "
                f"{low} ({values[low]}) > {high} ({values[high]})."
            )

    if values.get("released_after"):
        parse_iso_date(
            values["released_after"],
            "--released-after",
        )

    if values.get("released_before"):
        parse_iso_date(
            values["released_before"],
            "--released-before",
        )

    if (
        values.get("released_after")
        and values.get("released_before")
        and parse_iso_date(
            values["released_after"],
            "--released-after",
        )
        > parse_iso_date(
            values["released_before"],
            "--released-before",
        )
    ):
        raise ConfigError(
            "--released-after cannot be later than --released-before."
        )


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataIntegrityError(f"Required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataIntegrityError(f"Invalid JSON in {path}: {exc}") from exc


def save_json_atomic(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        fsync_file(handle)
    atomic_replace(part, path)


def write_resume_ready_marker(work_root: Path) -> None:
    """Record that this build has enough persistent state for safe --resume.

    The ordinary FungiFunk_refdb ownership marker only identifies the directory
    as belonging to this tool. It does not guarantee that initialization
    progressed far enough for recovery.

    This marker is written only after the persistent SQLite state,
    configuration snapshots, and resolved configuration have been committed.
    """

    marker = work_root / RESUME_READY_REL

    if marker.is_file():
        return

    save_json_atomic(
        marker,
        {
            "tool": TOOL_NAME,
            "format_version": 1,
            "created": now_iso(),
        },
    )


def write_completion_marker(root: Path) -> None:
    """Atomically record that final manifest verification succeeded.

    The completion marker is deliberately excluded from the integrity manifest.
    It acts as the final commit token written only after:

      1. SQLite has been checkpointed in its final DONE state.
      2. build_manifest.tsv has been written.
      3. every file recorded in that manifest has been verified.

    The marker stores the SHA-256 of the manifest itself so the completion token
    is bound to the exact manifest that was successfully verified.
    """

    manifest = root / MANIFEST_REL

    if not manifest.is_file():
        raise DataIntegrityError(
            "Cannot mark the reference complete because the final build "
            f"manifest does not exist: {manifest}"
        )

    save_json_atomic(
        root / COMPLETION_MARKER_REL,
        {
            "tool": TOOL_NAME,
            "format_version": 1,
            "completed": now_iso(),
            "manifest": MANIFEST_REL.as_posix(),
            "manifest_sha256": sha256_file(manifest),
        },
    )


def verify_completed_reference(root: Path) -> None:
    """Verify both the final completion token and the integrity manifest."""

    marker = root / COMPLETION_MARKER_REL

    if not marker.is_file():
        raise DataIntegrityError(
            "Reference does not have the final FungiFunk_refdb completion "
            f"marker: {marker}"
        )

    try:
        payload = json.loads(
            marker.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise DataIntegrityError(
            f"Invalid FungiFunk_refdb completion marker: {marker}"
        ) from exc

    if payload.get("tool") != TOOL_NAME:
        raise DataIntegrityError(
            "Completion marker has an unexpected tool identifier: "
            f"{marker}"
        )

    if payload.get("manifest") != MANIFEST_REL.as_posix():
        raise DataIntegrityError(
            "Completion marker does not identify the expected build manifest: "
            f"{marker}"
        )

    expected_manifest_sha256 = payload.get("manifest_sha256")

    if not isinstance(expected_manifest_sha256, str) or not expected_manifest_sha256:
        raise DataIntegrityError(
            "Completion marker does not contain a valid manifest SHA-256: "
            f"{marker}"
        )

    manifest = root / MANIFEST_REL

    if not manifest.is_file():
        raise DataIntegrityError(
            "Completion marker exists but the build manifest is missing: "
            f"{manifest}"
        )

    observed_manifest_sha256 = sha256_file(manifest)

    if observed_manifest_sha256 != expected_manifest_sha256:
        raise DataIntegrityError(
            "Completion marker does not match the current build manifest. "
            "The completed reference may have been modified after finalization."
        )

    # The token proves which manifest was finalized; now verify every file
    # recorded by that manifest.
    verify_manifest(root)


def namespace_explicit_values(ns: argparse.Namespace) -> dict[str, Any]:
    values = vars(ns).copy()
    values["assembly_level"] = normalize_assembly_levels(values.get("assembly_level"))
    return values


def resolve_config(ns: argparse.Namespace) -> BuildConfig:
    """Resolve create/resume/update inheritance into one authoritative config."""

    run_mode = detect_run_mode(ns)

    source = (
        str(Path(ns.source_refdb).expanduser().resolve())
        if ns.source_refdb
        else None
    )

    explicit = namespace_explicit_values(ns)

    reset_biological_config = bool(ns.reset_biological_config)

    if run_mode == "retry-ncbi-failures":

        if ns.outdir is not None:
            raise ConfigError(
                "--retry-ncbi-failures uses --source-refdb; "
                "--outdir must not be supplied."
            )

        if source is None:
            raise ConfigError(
                "--retry-ncbi-failures requires --source-refdb."
            )

        forbidden_options = {
            "--" + name.replace("_", "-")
            for name in BIOLOGICAL_ARG_FIELDS | BIOLOGICAL_INPUT_ARG_FIELDS
            if explicit.get(name) is not None
        }

        if reset_biological_config:
            forbidden_options.add("--reset-biological-config")

        if ns.db_version is not None:
            forbidden_options.add("--db-version")

        if forbidden_options:
            raise ConfigError(
                "--retry-ncbi-failures cannot change biological configuration "
                "or database identity. Unsupported option(s): "
                + ", ".join(sorted(forbidden_options))
            )

        # Manual recovery repairs the existing reference in place.
        # A separate staging directory will be selected before any writes.
        outdir = source

    else:

        if ns.outdir is None:
            raise ConfigError(
                f"--outdir is required with --{run_mode}."
            )

        outdir = str(Path(ns.outdir).expanduser().resolve())

    explicit_species_mode = (
        explicit.get("max_assemblies_per_species") is not None
    )

    explicit_depth_options = [
        name
        for name in (
            "max_assemblies_per_strain",
            "max_strains_per_species",
            "include_isolates",
            "include_unassigned_assemblies",
        )
        if explicit.get(name) is not None
    ]

    if explicit_species_mode and explicit_depth_options:
        rendered = ", ".join(
            "--" + name.replace("_", "-")
            for name in explicit_depth_options
        )

        raise ConfigError(
            "--max-assemblies-per-species activates compact species-level "
            "selection and cannot be combined with strain/isolate-depth "
            f"option(s): {rendered}."
        )

    if run_mode == "create":

        if reset_biological_config:
            raise ConfigError(
                "--reset-biological-config is not valid with --create because "
                "--create already starts from clean biological defaults."
            )

        if not ns.seed_taxa and not ns.include_explicit_targets:
            raise ConfigError(
                "--create requires at least one of --seed-taxa or "
                "--include-explicit-targets."
            )

        base: dict[str, Any] = {}

    elif run_mode == "resume":

        config_path = Path(outdir) / CONFIG_JSON_REL
        previous = load_json(config_path)

        base = previous

        # Biological configuration may be corrected during
        # a pre-assembly resume. Whether that is safe depends on the persistent
        # build state, which is not available until orchestrate() opens StateDB.
        #
        # Therefore biological changes are intentionally NOT rejected here.
        # orchestrate() is the authoritative state-aware validation boundary.
        #
        # db_version is different: it identifies this same build and is locked
        # during every resume regardless of build phase.
        explicitly_locked = [
            name
            for name in RESUME_ALWAYS_LOCKED_ARG_FIELDS
            if explicit.get(name) is not None
        ]

        if explicitly_locked:

            rendered = ", ".join(
                "--" + name.replace("_", "-")
                for name in sorted(explicitly_locked)
            )

            raise ConfigError(
                "--resume cannot change build identity option(s): "
                + rendered
                + ". The database version is locked for the lifetime of this build."
            )

        # A resumed build may itself be the staging directory of an incremental
        # update. Preserve the original source reference so unchanged assemblies
        # can still be reused safely.
        if previous.get("source_refdb"):
            source = str(
                Path(previous["source_refdb"]).expanduser().resolve()
            )

    elif run_mode == "create-new-updated-version":

        if not source:
            raise ConfigError(
                "--create-new-updated-version requires --source-refdb."
            )

        if source == outdir:
            raise ConfigError(
                "--source-refdb and --outdir must be different for "
                "--create-new-updated-version."
            )

        base = (
            {}
            if reset_biological_config
            else load_json(Path(source) / CONFIG_JSON_REL)
        )

    elif run_mode == "overwrite-with-updated-version":

        source = outdir

        base = (
            {}
            if reset_biological_config
            else load_json(Path(source) / CONFIG_JSON_REL)
        )

    elif run_mode == "retry-ncbi-failures":

        # Reuse the exact biological configuration of the completed reference.
        # No new curation or biological overrides are permitted.
        base = load_json(Path(source) / CONFIG_JSON_REL)

    else:  # pragma: no cover
        raise ConfigError(f"Unknown run mode {run_mode}")

    if (
        run_mode
        in {
            "create-new-updated-version",
            "overwrite-with-updated-version",
        }
        and reset_biological_config
        and not ns.seed_taxa
        and not ns.include_explicit_targets
    ):
        raise ConfigError(
            "--reset-biological-config requires at least one of "
            "--seed-taxa or --include-explicit-targets."
        )

    # Build from explicit defaults, then apply inherited values, then explicit
    # values. This makes precedence visible and deterministic.
    resolved: dict[str, Any] = dataclasses.asdict(
        BuildConfig(
            run_mode=run_mode,
            outdir=outdir,
        )
    )

    # Some configuration fields must never be inherited blindly from a previous
    # build. Computational/presentation fields are resolved separately below.
    #
    # reset_biological_config describes the policy requested for THIS invocation,
    # not a biological property that future builds should inherit.
    #
    # db_version is a special case: --resume must preserve the existing database
    # identity, whereas every create/update operation represents a new database
    # version unless the user explicitly supplies --db-version.
    non_inherited_fields = {
        "run_mode",
        "outdir",
        "source_refdb",
        "reset_biological_config",
        "workers",
        "force",
        "tmpdir",
        "verbose",
        "timestamps",
        "build_started",
    }

    if run_mode not in {"resume", "retry-ncbi-failures"}:
        non_inherited_fields.add("db_version")

    # During a pre-assembly resume with --reset-biological-config, preserve the
    # identity/provenance of the same build but do not inherit any biological
    # reference-selection values. State-aware permission for this reset is
    # checked later by orchestrate().
    if run_mode == "resume" and reset_biological_config:
        non_inherited_fields.update(BIOLOGICAL_ARG_FIELDS)

    for key, value in base.items():
        if key in resolved and key not in non_inherited_fields:
            resolved[key] = value

    resolved["run_mode"] = run_mode
    resolved["outdir"] = outdir
    resolved["source_refdb"] = source
    resolved["reset_biological_config"] = reset_biological_config
    resolved["build_started"] = now_iso()
    resolved["force"] = bool(ns.force)
    resolved["verbose"] = bool(ns.verbose)
    resolved["timestamps"] = bool(
        getattr(
            ns,
            "timestamps",
            False,
        )
    )

    resolved["workers"] = (
        ns.workers
        if ns.workers is not None
        else (
            "auto"
            if run_mode != "resume"
            else base.get("workers", "auto")
        )
    )

    resolved["tmpdir"] = (
        str(Path(ns.tmpdir).expanduser().resolve())
        if ns.tmpdir
        else (
            base.get("tmpdir")
            if run_mode == "resume"
            else None
        )
    )

    # Explicit biological options override inherited values.
    #
    # max_strains_per_species accepts the public literal "all", but internally
    # None is the canonical representation of an unlimited number of strains.
    for field_name in BIOLOGICAL_ARG_FIELDS:

        value = explicit.get(field_name)

        if value is None:
            continue

        if field_name == "max_strains_per_species":
            resolved[field_name] = (
                None
                if value == "all"
                else int(value)
            )
        else:
            resolved[field_name] = value

    # Explicit compact species-level mode replaces strain/isolate depth.
    #
    # The implicit strain defaults are not conflicts because the user did not
    # explicitly request them. Once compact mode is selected, strain/isolate
    # depth becomes not applicable.
    if explicit_species_mode:

        resolved["max_assemblies_per_strain"] = None
        resolved["max_strains_per_species"] = None
        resolved["include_isolates"] = False
        resolved["include_unassigned_assemblies"] = False

    # If compact species-level mode was inherited from an existing reference,
    # do not allow strain/isolate-depth settings to be layered on top of it.
    #
    # Switching an inherited compact reference back to strain-level selection
    # requires --reset-biological-config so the old selection strategy is
    # discarded explicitly.
    elif resolved.get("max_assemblies_per_species") is not None:

        inherited_conflicts = [
            name
            for name in (
                "max_assemblies_per_strain",
                "max_strains_per_species",
                "include_isolates",
                "include_unassigned_assemblies",
            )
            if explicit.get(name) is not None
        ]

        if inherited_conflicts:

            rendered = ", ".join(
                "--" + name.replace("_", "-")
                for name in inherited_conflicts
            )

            raise ConfigError(
                "The inherited biological configuration uses compact "
                "species-level assembly selection. It cannot be combined with "
                f"strain/isolate-depth option(s): {rendered}. "
                "Use --reset-biological-config to switch selection strategy."
            )

    # Database version is provenance/identity rather than biological selection.
    # It is resolved separately so update inheritance cannot accidentally reuse
    # the previous reference's database-version label.
    if explicit.get("db_version") is not None:
        resolved["db_version"] = explicit["db_version"]

    # Input paths are handled separately because inherited paths in the previous
    # resolved_config.json point at the old build and must not be blindly copied.
    resolved["seed_taxa"] = ns.seed_taxa
    resolved["exclude_taxa"] = ns.exclude_taxa
    resolved["metadata_filters"] = ns.metadata_filters
    resolved["include_explicit_targets"] = ns.include_explicit_targets
    resolved["exclude_explicit_targets"] = ns.exclude_explicit_targets

    # Normalize unset booleans after inheritance/default composition.
    for name in [
        "include_gca",
        "include_isolates",
        "include_unassigned_assemblies",
        "include_atypical",
        "require_busco",
    ]:
        resolved[name] = bool(resolved.get(name, False))

    # Ensure list fields are real lists rather than None.
    for name in [
        "assembly_level",
        "assembly_method_contains",
        "sequencing_technology_contains",
    ]:
        resolved[name] = list(resolved.get(name) or [])

    validate_thresholds(resolved)

    workers = resolved["workers"]

    if workers != "auto":

        try:
            workers_int = int(workers)

        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "--workers must be 'auto' or a positive integer."
            ) from exc

        if workers_int <= 0:
            raise ConfigError("--workers must be > 0.")

        resolved["workers"] = workers_int

    if ns.force and resolved["workers"] == "auto":
        raise ConfigError(
            "--force requires an explicit numeric --workers N."
        )

    return BuildConfig(
        **{
            f.name: resolved.get(f.name)
            for f in dataclasses.fields(BuildConfig)
        }
    )


########################################################################################################
# Persistent build state (single coordinator writer)
########################################################################################################

STATE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS species (
    taxid INTEGER PRIMARY KEY,
    species TEXT NOT NULL,
    phylum TEXT,
    class_name TEXT,
    order_name TEXT,
    family TEXT,
    genus TEXT,
    authority TEXT,
    tier INTEGER NOT NULL,
    seed_taxa TEXT,
    tier_source TEXT,
    explicit_include INTEGER NOT NULL DEFAULT 0,
    evidence_type TEXT,
    reference TEXT,
    notes TEXT,
    gut_evidence INTEGER NOT NULL DEFAULT 0,
    human_evidence INTEGER NOT NULL DEFAULT 0,
    inclusion_reason TEXT,
    matched_exclusion_taxa TEXT,
    exclusion_override INTEGER NOT NULL DEFAULT 0,
    final_taxonomic_status TEXT NOT NULL,
    filter_status TEXT NOT NULL DEFAULT 'PENDING',
    state TEXT NOT NULL DEFAULT 'PENDING',
    last_checked_ncbi TEXT
);

CREATE TABLE IF NOT EXISTS assemblies (
    accession TEXT PRIMARY KEY,
    species_taxid INTEGER NOT NULL,
    species TEXT NOT NULL,
    source TEXT,
    paired_accession TEXT,
    multi_isolate INTEGER,
    assembly_status TEXT,
    atypical INTEGER,
    atypical_warnings TEXT,
    assembly_level TEXT,
    refseq_category TEXT,
    strain TEXT,
    isolate TEXT,
    infraspecific_name TEXT,
    bioproject TEXT,
    biosample TEXT,
    assembly_method TEXT,
    sequencing_technology TEXT,
    contig_n50 INTEGER,
    contig_l50 INTEGER,
    num_contigs INTEGER,
    scaffold_n50 INTEGER,
    scaffold_l50 INTEGER,
    num_scaffolds INTEGER,
    num_scaffold_gaps INTEGER,
    genome_size INTEGER,
    gc_percent REAL,
    protein_coding_genes INTEGER,
    busco_complete REAL,
    busco_fragmented REAL,
    busco_missing REAL,
    busco_duplicated REAL,
    busco_lineage TEXT,
    busco_version TEXT,
    release_date TEXT,
    annotation_name TEXT,
    annotation_release_date TEXT,
    annotation_status TEXT,
    protein_fasta_available INTEGER,
    protein_count INTEGER,
    protein_fasta_sha256 TEXT,
    ncbi_package_md5 TEXT,
    fingerprint TEXT NOT NULL,
    source_scope_status TEXT,
    qc_status TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    final_status TEXT,
    final_reason TEXT,
    update_change TEXT,
    update_reason TEXT,
    FOREIGN KEY(species_taxid) REFERENCES species(taxid)
);

CREATE INDEX IF NOT EXISTS idx_assemblies_species ON assemblies(species_taxid);
CREATE INDEX IF NOT EXISTS idx_assemblies_selected ON assemblies(selected);

CREATE TABLE IF NOT EXISTS assembly_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession TEXT,
    species_taxid INTEGER,
    species TEXT,
    filter_id TEXT,
    field TEXT,
    operator TEXT,
    required TEXT,
    observed TEXT,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS species_filter_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species_taxid INTEGER,
    species TEXT,
    filter_id TEXT,
    field TEXT,
    operator TEXT,
    required TEXT,
    observed TEXT,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS protein_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species_taxid INTEGER,
    species TEXT,
    assembly TEXT,
    protein_accession TEXT,
    warning_type TEXT,
    count INTEGER
);

CREATE TABLE IF NOT EXISTS failed_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species_taxid INTEGER,
    species TEXT,
    assembly TEXT,
    reason TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_sha256 TEXT NOT NULL,
    sequence_length INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    species_taxid INTEGER NOT NULL,
    species TEXT NOT NULL,
    tier INTEGER NOT NULL,
    assembly TEXT NOT NULL,
    assembly_source TEXT,
    paired_accession TEXT,
    strain TEXT,
    isolate TEXT,
    original_protein_accession TEXT,
    original_header TEXT,
    original_had_terminal_stop INTEGER NOT NULL DEFAULT 0,
    has_internal_stop INTEGER NOT NULL DEFAULT 0,
    num_internal_stops INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(sequence_sha256);
CREATE INDEX IF NOT EXISTS idx_sources_assembly ON sources(assembly);
CREATE INDEX IF NOT EXISTS idx_sources_species ON sources(species_taxid);

CREATE TABLE IF NOT EXISTS canonical_sequences (
    sequence_sha256 TEXT PRIMARY KEY,
    sequence_length INTEGER NOT NULL,
    store_offset INTEGER NOT NULL,
    source_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS update_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    species TEXT,
    taxid INTEGER,
    assembly TEXT,
    change TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


class StateDB:
    """Coordinator-owned SQLite database.

    Workers NEVER receive this connection. That rule is more important than
    squeezing a little extra throughput from concurrent SQLite writes: it makes
    recovery semantics understandable and avoids partial shared commits.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(STATE_SCHEMA)
        self.conn.commit()

    def checkpoint(self, *, truncate: bool = True) -> None:
        """Flush WAL content into the main SQLite file before external hashing/copying.

        SQLite transactions make database changes atomic, but a completed WAL-mode
        database can still have committed pages in the separate -wal file. Our
        build manifest intentionally hashes stable final files, so finalization
        checkpoints the WAL before computing checksums.
        """
        self.conn.commit()
        mode = "TRUNCATE" if truncate else "PASSIVE"
        self.conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()
        self.conn.commit()

    def close(self) -> None:
        self.checkpoint(truncate=True)
        self.conn.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.close()

    def set_meta(self, key: str, value: Any) -> None:
        text = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, text),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None, *, json_value: bool = False) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        if json_value:
            try:
                return json.loads(row["value"])
            except json.JSONDecodeError:
                return row["value"]
        return row["value"]

    # Only active work phases receive an entry banner.
    # Completion checkpoints and special states are excluded.
    PHASE_BANNER_STATES = frozenset({
        "INITIALIZING",
        "TAXONOMY_CURATION",
        "ASSEMBLY_SEARCH_AND_SELECTION",
        "UPDATE_COMPARISON_AND_SELECTION",
        "PROTEIN_RETRIEVAL_AND_PROCESSING",
        "GLOBAL_EXACT_DEDUPLICATION",
        "FINAL_OUTPUT_WRITING",
        "FINAL_REFERENCE_VALIDATION",
        "FINAL_REPORT_WRITING",
        "FINAL_CHECKPOINT",
    })

    def announce_phase(self, state: str) -> None:
        """Display a phase once per visit without changing persistent state."""
        if state not in self.PHASE_BANNER_STATES:
            return

        if getattr(self, "_last_announced_phase", None) == state:
            return

        print_section_banner(state)
        self._last_announced_phase = state

    def set_global_state(self, state: str) -> None:
        """Persist a checkpoint and announce entry into an active work phase."""

        # Keep the existing checkpoint writes unchanged.
        self.set_meta("global_state", state)
        self.set_meta("state_updated", now_iso())

        if state in self.PHASE_BANNER_STATES:
            # Also handles resume when the stored state already matches.
            # announce_phase() prevents duplicate consecutive banners.
            self.announce_phase(state)
        else:
            # Completion checkpoints end the previous visual phase.
            self._last_announced_phase = None

    def global_state(self) -> str:
        return str(self.get_meta("global_state", "UNKNOWN"))

    def clear_preassembly_data(self) -> None:
        """Safe only before assembly processing; used after curation fix."""
        with self.conn:
            for table in [
                "species", "assemblies", "assembly_failures", "species_filter_failures",
                "protein_warnings", "failed_downloads", "sources", "canonical_sequences",
                "update_changes",
            ]:
                self.conn.execute(f"DELETE FROM {table}")


def append_python_log(root: Path, message: str) -> None:
    path = root / LOGS_DIR / "FungiFunk_refdb.python.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()}\t{message}\n")


def update_wrapper_run_info_db_version(
    db_version: str | None,
) -> None:
    """Update the current wrapper log entry with the resolved DB version.

    The shell wrapper appends one run-information block to build_info.log
    before each Python invocation. Therefore, a resumed build legitimately
    contains multiple ``db_version:`` entries: one for each invocation.

    The most recent ``db_version:`` entry belongs to the current wrapper
    invocation. If that entry still contains ``pending resolution``, replace
    only that entry and preserve all previous log history unchanged.

    If --db-version was supplied explicitly, the wrapper may already have
    written the resolved value. In that case this helper is a no-op.

    The wrapper passes the current log path through an internal environment
    variable. Direct invocation of FungiFunk_refdb.py does not set that
    variable, so this helper becomes a no-op.

    Failure to update this presentation/provenance log must never invalidate
    an otherwise valid reference build.
    """

    log_path_raw = os.environ.get(
        "FUNGIFUNK_WRAPPER_RUN_INFO_LOG"
    )

    if not log_path_raw or not db_version:
        return

    log_path = Path(log_path_raw)

    try:

        if not log_path.is_file():

            warn(
                "Could not update the wrapper run-information log with the "
                "resolved database version because the file is missing: "
                f"{log_path}"
            )

            return

        text = log_path.read_text(
            encoding="utf-8"
        )

        lines = text.splitlines(
            keepends=True
        )

        matching_indexes = [
            i
            for i, line in enumerate(lines)
            if line.startswith("  db_version:")
        ]

        if not matching_indexes:

            warn(
                "Could not update the wrapper run-information log with the "
                "resolved database version because no db_version entry was "
                f"found in: {log_path}"
            )

            return

        # build_info.log is an append-only history of wrapper invocations.
        # Therefore the final db_version entry belongs to the current run.
        current_index = matching_indexes[-1]
        current_line = lines[current_index]

        if current_line.endswith("\r\n"):
            line_ending = "\r\n"

        elif current_line.endswith("\n"):
            line_ending = "\n"

        else:
            line_ending = ""

        current_value = current_line.rstrip(
            "\r\n"
        )

        resolved_value = (
            f"  db_version: {db_version}"
        )

        # An explicitly supplied --db-version may already have been written
        # by the wrapper. Nothing needs to be changed in that case.
        if current_value == resolved_value:
            return

        if current_value != "  db_version: pending resolution":

            warn(
                "Could not update the wrapper run-information log with the "
                "resolved database version because the most recent db_version "
                "entry is neither 'pending resolution' nor the resolved value: "
                f"{log_path}"
            )

            return

        lines[current_index] = (
            resolved_value
            + line_ending
        )

        log_path.write_text(
            "".join(lines),
            encoding="utf-8",
        )

    except Exception as exc:

        warn(
            "Could not update the wrapper run-information log with the "
            f"resolved database version: {exc}"
        )


def _wrapper_log_value(
    value: Any,
    *,
    none_text: str,
) -> str:
    """Render one configuration value for the human-readable wrapper log."""

    if isinstance(value, bool):
        return "True" if value else "False"

    if value is None:
        return none_text

    if isinstance(value, (list, tuple)):
        if not value:
            return "none"
        return ", ".join(str(item) for item in value)

    return str(value)


def _append_wrapper_run_info_lines(lines: list[str]) -> None:
    """Append human-readable information to the current wrapper log.

    Direct Python invocation does not define FUNGIFUNK_WRAPPER_RUN_INFO_LOG,
    so this helper becomes a no-op. Logging failure must not invalidate an
    otherwise valid reference build.
    """

    log_path_raw = os.environ.get(
        "FUNGIFUNK_WRAPPER_RUN_INFO_LOG"
    )

    if not log_path_raw:
        return

    log_path = Path(log_path_raw)

    try:

        if not log_path.is_file():
            warn(
                "Could not append biological configuration to the wrapper "
                f"run-information log because the file is missing: {log_path}"
            )
            return

        with log_path.open(
            "a",
            encoding="utf-8",
        ) as handle:

            handle.write(
                "\n".join(lines).rstrip()
                + "\n\n"
            )

    except Exception as exc:

        warn(
            "Could not append biological configuration to the wrapper "
            f"run-information log: {exc}"
        )


def _render_biological_config_for_wrapper_log(
    values: dict[str, Any],
    *,
    seed_snapshot: Path | None,
    exclusion_snapshot: Path | None,
    filter_snapshot: Path | None,
    include_explicit_targets_snapshot: Path | None,
    exclude_explicit_targets_snapshot: Path | None,
) -> list[str]:
    """Render one inherited/effective biological configuration."""

    lines: list[str] = []

    lines.extend(
        [
            "  Biological input snapshots:",
            (
                "    seed_taxa: "
                + (
                    str(seed_snapshot)
                    if seed_snapshot is not None
                    else "none"
                )
            ),
            (
                "    exclude_taxa: "
                + (
                    str(exclusion_snapshot)
                    if exclusion_snapshot is not None
                    else "none"
                )
            ),
            (
                "    metadata_filters: "
                + (
                    str(filter_snapshot)
                    if filter_snapshot is not None
                    else "none"
                )
            ),
            (
                "    include_explicit_targets: "
                + (
                    str(include_explicit_targets_snapshot)
                    if include_explicit_targets_snapshot is not None
                    else "none"
                )
            ),
            (
                "    exclude_explicit_targets: "
                + (
                    str(exclude_explicit_targets_snapshot)
                    if exclude_explicit_targets_snapshot is not None
                    else "none"
                )
            ),
            "",
        ]
    )

    selection_mode = (
        "SPECIES"
        if values.get("max_assemblies_per_species") is not None
        else "STRAIN"
    )

    grouped_fields: set[str] = set()

    for heading, fields in BIOLOGICAL_LOG_GROUPS:

        lines.append(f"  {heading}:")

        if heading == "Assembly selection / representation":
            lines.append(f"    mode: {selection_mode}")

        for field_name in fields:

            grouped_fields.add(field_name)
            value = values.get(field_name)

            if heading == "Assembly selection / representation":

                if (
                    selection_mode == "SPECIES"
                    and field_name
                    in {
                        "max_assemblies_per_strain",
                        "max_strains_per_species",
                        "include_isolates",
                        "include_unassigned_assemblies",
                    }
                ):
                    rendered = "not applicable"

                elif (
                    selection_mode == "STRAIN"
                    and field_name == "max_assemblies_per_species"
                ):
                    rendered = "not applicable"

                elif (
                    selection_mode == "STRAIN"
                    and field_name == "max_strains_per_species"
                    and value is None
                ):
                    rendered = "all"

                else:
                    rendered = _wrapper_log_value(
                        value,
                        none_text="not set",
                    )

            else:
                rendered = _wrapper_log_value(
                    value,
                    none_text="not set",
                )

            lines.append(
                f"    {field_name}: {rendered}"
            )

        lines.append("")

    # Future biological fields should never silently disappear from the log
    # merely because the presentation groups were not updated at the same time.
    ungrouped_fields = sorted(
        BIOLOGICAL_ARG_FIELDS - grouped_fields
    )

    if ungrouped_fields:

        lines.append("  Other biological options:")

        for field_name in ungrouped_fields:
            lines.append(
                f"    {field_name}: "
                + _wrapper_log_value(
                    values.get(field_name),
                    none_text="not set",
                )
            )

        lines.append("")

    return lines


def append_wrapper_command_and_inherited_biological_config(
    ns: argparse.Namespace,
    config: BuildConfig,
    work_root: Path,
    source_root: Path | None,
) -> None:
    """Log explicit CLI biology and the biological configuration used as base."""

    lines = [
        "Biological options supplied in this command:",
        "",
        "  Configuration resolution:",
        (
            "    reset_biological_config_supplied: "
            + (
                "True"
                if bool(ns.reset_biological_config)
                else "False"
            )
        ),
        "",
        "  Biological inputs:",
        f"    seed_taxa: {ns.seed_taxa or 'not supplied'}",
        f"    exclude_taxa: {ns.exclude_taxa or 'not supplied'}",
        f"    metadata_filters: {ns.metadata_filters or 'not supplied'}",
        f"    include_explicit_targets: {ns.include_explicit_targets or 'not supplied'}",
        f"    exclude_explicit_targets: {ns.exclude_explicit_targets or 'not supplied'}",
        "",
    ]

    grouped_fields: set[str] = set()

    for heading, fields in BIOLOGICAL_LOG_GROUPS:

        lines.append(f"  {heading}:")

        for field_name in fields:

            grouped_fields.add(field_name)
            value = getattr(ns, field_name, None)

            if field_name in BIOLOGICAL_BOOLEAN_ARG_FIELDS:

                lines.append(
                    f"    {field_name}_supplied: "
                    + (
                        "True"
                        if value is not None
                        else "False"
                    )
                )

            else:

                lines.append(
                    f"    {field_name}: "
                    + _wrapper_log_value(
                        value,
                        none_text="not supplied",
                    )
                )

        lines.append("")

    ungrouped_fields = sorted(
        BIOLOGICAL_ARG_FIELDS - grouped_fields
    )

    if ungrouped_fields:

        lines.append("  Other biological options:")

        for field_name in ungrouped_fields:

            value = getattr(ns, field_name, None)

            lines.append(
                f"    {field_name}: "
                + _wrapper_log_value(
                    value,
                    none_text="not supplied",
                )
            )

        lines.append("")

    lines.extend(
        [
            "Inherited biological configuration:",
            "",
        ]
    )

    if config.run_mode == "create":

        lines.extend(
            [
                "  none",
                "  reason: new --create starts from clean biological defaults",
                "",
            ]
        )

        _append_wrapper_run_info_lines(lines)
        return

    if config.reset_biological_config:

        lines.extend(
            [
                "  none",
                "  reason: --reset-biological-config",
                "",
            ]
        )

        _append_wrapper_run_info_lines(lines)
        return

    inherited_root = (
        work_root
        if config.run_mode == "resume"
        else source_root
    )

    if inherited_root is None:

        # This is presentation logging only. The actual update configuration
        # validation remains authoritative elsewhere.
        lines.extend(
            [
                "  unavailable",
                "  reason: inherited configuration source is unavailable",
                "",
            ]
        )

        _append_wrapper_run_info_lines(lines)
        return

    inherited_config_path = (
        inherited_root / CONFIG_JSON_REL
    )

    try:
        previous = load_json(
            inherited_config_path
        )

        # Use current BuildConfig defaults as the baseline so an older
        # resolved_config.json that lacks a newer field is represented exactly
        # as resolve_config() would interpret it.
        defaults = dataclasses.asdict(
            BuildConfig(
                run_mode=config.run_mode,
                outdir=config.outdir,
            )
        )

        inherited_values = {
            field_name: previous.get(
                field_name,
                defaults.get(field_name),
            )
            for field_name in BIOLOGICAL_ARG_FIELDS
        }

        inherited_seed = (
            inherited_root
            / CONFIG_DIR
            / SEED_SNAPSHOT
        )

        inherited_exclusion = (
            inherited_root
            / CONFIG_DIR
            / EXCLUSION_SNAPSHOT
        )

        inherited_filter_candidate = (
            inherited_root
            / CONFIG_DIR
            / FILTER_SNAPSHOT
        )

        inherited_filter = (
            inherited_filter_candidate
            if inherited_filter_candidate.is_file()
            else None
        )

        inherited_include_explicit_candidate = (
            inherited_root
            / CONFIG_DIR
            / INCLUDE_EXPLICIT_TARGETS_SNAPSHOT
        )

        inherited_include_explicit = (
            inherited_include_explicit_candidate
            if inherited_include_explicit_candidate.is_file()
            else None
        )

        inherited_exclude_explicit_candidate = (
            inherited_root
            / CONFIG_DIR
            / EXCLUDE_EXPLICIT_TARGETS_SNAPSHOT
        )

        inherited_exclude_explicit = (
            inherited_exclude_explicit_candidate
            if inherited_exclude_explicit_candidate.is_file()
            else None
        )

        lines.append(
            f"  source: {inherited_root}"
        )
        
        lines.append("")

        lines.extend(
            _render_biological_config_for_wrapper_log(
                inherited_values,
                seed_snapshot=inherited_seed,
                exclusion_snapshot=inherited_exclusion,
                filter_snapshot=inherited_filter,
                include_explicit_targets_snapshot=inherited_include_explicit,
                exclude_explicit_targets_snapshot=inherited_exclude_explicit,
            )
        )

    except Exception as exc:

        lines.extend(
            [
                "  unavailable",
                (
                    "  reason: could not read inherited biological "
                    f"configuration for logging: {exc}"
                ),
                "",
            ]
        )

    _append_wrapper_run_info_lines(lines)


def append_wrapper_effective_biological_config(
    config: BuildConfig,
    *,
    seed_snapshot: Path,
    exclusion_snapshot: Path,
    filter_snapshot: Path | None,
    include_explicit_targets_snapshot: Path | None,
    exclude_explicit_targets_snapshot: Path | None,
) -> None:
    """Log the biological configuration actually used by this invocation."""

    lines = [
        "Effective biological configuration:",
        "",
    ]

    values = {
        field_name: getattr(
            config,
            field_name,
        )
        for field_name in BIOLOGICAL_ARG_FIELDS
    }

    lines.extend(
        _render_biological_config_for_wrapper_log(
            values,
            seed_snapshot=seed_snapshot,
            exclusion_snapshot=exclusion_snapshot,
            filter_snapshot=filter_snapshot,
            include_explicit_targets_snapshot=include_explicit_targets_snapshot,
            exclude_explicit_targets_snapshot=exclude_explicit_targets_snapshot,
        )
    )

    _append_wrapper_run_info_lines(lines)


def initialize_output_layout(root: Path) -> None:
    for subdir in [DATABASE_DIR, METADATA_DIR, REPORTS_DIR, CONFIG_DIR, LOGS_DIR, STATE_DIR, TMP_DIR]:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    marker = root / MARKER_REL
    if not marker.exists():
        marker.write_text(
            f"Tool={TOOL_NAME}\nTool_version={VERSION}\nInitialized={now_iso()}\n",
            encoding="utf-8",
        )


def copy_config_snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise ConfigError(f"Configuration input does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    shutil.copy2(source, part)
    atomic_replace(part, destination)


def resolve_config_snapshots(
    config: BuildConfig,
    work_root: Path,
    source_root: Path | None,
    state: StateDB,
) -> tuple[
    Path,
    Path,
    Path | None,
    Path | None,
    Path | None,
]:
    """Select/copy the exact biological TSV snapshots used by this build.

    The resulting files live under work_root/config so future updates remain
    reproducible even if the user's original files move or disappear.

    A seed snapshot always exists. For explicit-target-only builds it is a valid
    header-only TSV representing an empty automatic seed set.

    Explicit-target snapshots are optional: absence means that no corresponding
    explicit inclusion/exclusion target file is active.
    """

    config_dir = work_root / CONFIG_DIR

    seed_dst = config_dir / SEED_SNAPSHOT
    exc_dst = config_dir / EXCLUSION_SNAPSHOT
    filt_dst = config_dir / FILTER_SNAPSHOT

    include_explicit_dst = (
        config_dir
        / INCLUDE_EXPLICIT_TARGETS_SNAPSHOT
    )

    exclude_explicit_dst = (
        config_dir
        / EXCLUDE_EXPLICIT_TARGETS_SNAPSHOT
    )

    def remove_optional_snapshot(path: Path) -> None:
        if path.exists():
            path.unlink()

    def copy_optional_or_remove(
        supplied_path: str | None,
        destination: Path,
    ) -> None:
        if supplied_path:
            copy_config_snapshot(
                Path(supplied_path),
                destination,
            )
        else:
            remove_optional_snapshot(destination)

    if config.run_mode == "resume":

        current_state = state.global_state()

        biological_editable = (
            current_state in PRE_ASSEMBLY_BIOLOGICAL_EDITABLE_STATES
        )

        # A reset deliberately discards all previous biological input snapshots
        # and reconstructs them from clean semantics.
        if config.reset_biological_config:

            if not biological_editable:
                raise ConfigError(
                    "--reset-biological-config cannot be used because assembly "
                    "search and selection has already started for this build."
                )

            if not config.seed_taxa and not config.include_explicit_targets:
                raise ConfigError(
                    "--reset-biological-config with --resume requires at least "
                    "one of --seed-taxa FILE or --include-explicit-targets FILE."
                )

            if config.seed_taxa:
                copy_config_snapshot(
                    Path(config.seed_taxa),
                    seed_dst,
                )
            else:
                write_empty_seed_snapshot(
                    seed_dst,
                )

            if config.exclude_taxa:
                copy_config_snapshot(
                    Path(config.exclude_taxa),
                    exc_dst,
                )
            else:
                write_empty_exclusion_snapshot(
                    exc_dst,
                )

            if config.metadata_filters:
                copy_config_snapshot(
                    Path(config.metadata_filters),
                    filt_dst,
                )
            else:
                remove_optional_snapshot(
                    filt_dst,
                )

            copy_optional_or_remove(
                config.include_explicit_targets,
                include_explicit_dst,
            )

            copy_optional_or_remove(
                config.exclude_explicit_targets,
                exclude_explicit_dst,
            )

            return (
                seed_dst,
                exc_dst,
                filt_dst if filt_dst.is_file() else None,
                (
                    include_explicit_dst
                    if include_explicit_dst.is_file()
                    else None
                ),
                (
                    exclude_explicit_dst
                    if exclude_explicit_dst.is_file()
                    else None
                ),
            )

        # Normal pre-assembly resume may replace selected biological TSVs while
        # preserving biological inputs that were not supplied explicitly.
        if config.seed_taxa:

            if not biological_editable:
                raise ConfigError(
                    "--seed-taxa cannot be changed because assembly search and "
                    "selection has already started for this build."
                )

            copy_config_snapshot(
                Path(config.seed_taxa),
                seed_dst,
            )

        elif not seed_dst.is_file():

            raise DataIntegrityError(
                f"Resume seed snapshot is missing: {seed_dst}"
            )

        if config.exclude_taxa:

            if not biological_editable:
                raise ConfigError(
                    "--exclude-taxa cannot be changed because assembly search "
                    "and selection has already started for this build."
                )

            copy_config_snapshot(
                Path(config.exclude_taxa),
                exc_dst,
            )

        elif not exc_dst.is_file():

            raise DataIntegrityError(
                f"Resume exclusion snapshot is missing: {exc_dst}"
            )

        if config.metadata_filters:

            if not biological_editable:
                raise ConfigError(
                    "--metadata-filters cannot be changed because assembly "
                    "search and selection has already started for this build."
                )

            copy_config_snapshot(
                Path(config.metadata_filters),
                filt_dst,
            )

        if config.include_explicit_targets:

            if not biological_editable:
                raise ConfigError(
                    "--include-explicit-targets cannot be changed because "
                    "assembly search and selection has already started for this "
                    "build."
                )

            copy_config_snapshot(
                Path(config.include_explicit_targets),
                include_explicit_dst,
            )

        if config.exclude_explicit_targets:

            if not biological_editable:
                raise ConfigError(
                    "--exclude-explicit-targets cannot be changed because "
                    "assembly search and selection has already started for this "
                    "build."
                )

            copy_config_snapshot(
                Path(config.exclude_explicit_targets),
                exclude_explicit_dst,
            )

        return (
            seed_dst,
            exc_dst,
            filt_dst if filt_dst.is_file() else None,
            (
                include_explicit_dst
                if include_explicit_dst.is_file()
                else None
            ),
            (
                exclude_explicit_dst
                if exclude_explicit_dst.is_file()
                else None
            ),
        )

    if config.run_mode == "create":

        if config.seed_taxa:
            copy_config_snapshot(
                Path(config.seed_taxa),
                seed_dst,
            )
        else:
            if not config.include_explicit_targets:
                raise ConfigError(
                    "--create requires at least one of --seed-taxa or "
                    "--include-explicit-targets."
                )

            write_empty_seed_snapshot(
                seed_dst,
            )

        if config.exclude_taxa:
            copy_config_snapshot(
                Path(config.exclude_taxa),
                exc_dst,
            )
        else:
            write_empty_exclusion_snapshot(
                exc_dst,
            )

        if config.metadata_filters:
            copy_config_snapshot(
                Path(config.metadata_filters),
                filt_dst,
            )
        else:
            remove_optional_snapshot(
                filt_dst,
            )

        copy_optional_or_remove(
            config.include_explicit_targets,
            include_explicit_dst,
        )

        copy_optional_or_remove(
            config.exclude_explicit_targets,
            exclude_explicit_dst,
        )

        return (
            seed_dst,
            exc_dst,
            filt_dst if filt_dst.is_file() else None,
            (
                include_explicit_dst
                if include_explicit_dst.is_file()
                else None
            ),
            (
                exclude_explicit_dst
                if exclude_explicit_dst.is_file()
                else None
            ),
        )

    # Update modes.
    if source_root is None:
        raise ConfigError(
            "Internal configuration error: update source is missing."
        )

    if config.reset_biological_config:

        if config.seed_taxa:
            copy_config_snapshot(
                Path(config.seed_taxa),
                seed_dst,
            )
        else:
            if not config.include_explicit_targets:
                raise ConfigError(
                    "--reset-biological-config requires at least one of "
                    "--seed-taxa or --include-explicit-targets."
                )

            write_empty_seed_snapshot(
                seed_dst,
            )

        if config.exclude_taxa:
            copy_config_snapshot(
                Path(config.exclude_taxa),
                exc_dst,
            )
        else:
            write_empty_exclusion_snapshot(
                exc_dst,
            )

        if config.metadata_filters:
            copy_config_snapshot(
                Path(config.metadata_filters),
                filt_dst,
            )
        else:
            remove_optional_snapshot(
                filt_dst,
            )

        copy_optional_or_remove(
            config.include_explicit_targets,
            include_explicit_dst,
        )

        copy_optional_or_remove(
            config.exclude_explicit_targets,
            exclude_explicit_dst,
        )

    else:

        seed_src = (
            Path(config.seed_taxa)
            if config.seed_taxa
            else source_root / CONFIG_DIR / SEED_SNAPSHOT
        )

        exc_src = (
            Path(config.exclude_taxa)
            if config.exclude_taxa
            else source_root / CONFIG_DIR / EXCLUSION_SNAPSHOT
        )

        copy_config_snapshot(
            seed_src,
            seed_dst,
        )

        copy_config_snapshot(
            exc_src,
            exc_dst,
        )

        if config.metadata_filters:
            copy_config_snapshot(
                Path(config.metadata_filters),
                filt_dst,
            )
        else:
            inherited_filter = (
                source_root
                / CONFIG_DIR
                / FILTER_SNAPSHOT
            )

            if inherited_filter.is_file():
                copy_config_snapshot(
                    inherited_filter,
                    filt_dst,
                )
            else:
                remove_optional_snapshot(
                    filt_dst,
                )

        if config.include_explicit_targets:
            copy_config_snapshot(
                Path(config.include_explicit_targets),
                include_explicit_dst,
            )
        else:
            inherited_include_explicit = (
                source_root
                / CONFIG_DIR
                / INCLUDE_EXPLICIT_TARGETS_SNAPSHOT
            )

            if inherited_include_explicit.is_file():
                copy_config_snapshot(
                    inherited_include_explicit,
                    include_explicit_dst,
                )
            else:
                remove_optional_snapshot(
                    include_explicit_dst,
                )

        if config.exclude_explicit_targets:
            copy_config_snapshot(
                Path(config.exclude_explicit_targets),
                exclude_explicit_dst,
            )
        else:
            inherited_exclude_explicit = (
                source_root
                / CONFIG_DIR
                / EXCLUDE_EXPLICIT_TARGETS_SNAPSHOT
            )

            if inherited_exclude_explicit.is_file():
                copy_config_snapshot(
                    inherited_exclude_explicit,
                    exclude_explicit_dst,
                )
            else:
                remove_optional_snapshot(
                    exclude_explicit_dst,
                )

    return (
        seed_dst,
        exc_dst,
        filt_dst if filt_dst.is_file() else None,
        (
            include_explicit_dst
            if include_explicit_dst.is_file()
            else None
        ),
        (
            exclude_explicit_dst
            if exclude_explicit_dst.is_file()
            else None
        ),
    )


def write_empty_seed_snapshot(path: Path) -> None:
    """Write a valid header-only seed snapshot for explicit-target-only builds.

    This snapshot is created only when no --seed-taxa file was supplied and the
    build is driven by explicit inclusion targets. A user-supplied empty seed
    file remains subject to normal seed-input validation.
    """

    write_tsv(
        path,
        [
            "Taxon_name",
            "Taxid",
            "Include_descendants",
            "Tier",
            "Evidence_type",
            "Reference",
            "Notes",
        ],
        [],
    )


def write_empty_exclusion_snapshot(path: Path) -> None:
    """Write a valid exclusion-taxa snapshot containing no exclusion rules.

    This is used when the user intentionally omits --exclude-taxa. Keeping a
    real header-only snapshot makes the resolved build configuration
    reproducible without requiring downstream code to handle a missing path.
    """

    write_tsv(
        path,
        [
            "Taxon_name",
            "Taxid",
            "Exclude_descendants",
            "Reason",
            "Reference",
            "Notes",
        ],
        [],
    )


########################################################################################################
# NCBI Datasets client and global launch-rate limiter
########################################################################################################

class LocalRateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self.last_start = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        remaining = self.interval - (now - self.last_start)
        if remaining > 0:
            time.sleep(remaining)
        self.last_start = time.monotonic()


@dataclass
class SharedLimiterProxy:
    lock: Any
    last_start: Any
    interval: float

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            remaining = self.interval - (now - float(self.last_start.value))
            if remaining > 0:
                time.sleep(remaining)
            self.last_start.value = time.monotonic()

@dataclass
class SharedConcurrencyLimiterProxy:
    """Limit simultaneous NCBI Datasets commands across species workers.

    The underlying semaphore is created by multiprocessing.Manager(), so every
    worker process sees the same shared concurrency limit.

    This is intentionally separate from SharedLimiterProxy:

    - SharedLimiterProxy controls how frequently NCBI commands may START.
    - SharedConcurrencyLimiterProxy controls how many NCBI commands may remain
      alive at the same time.
    """

    semaphore: Any

    def acquire(self) -> None:
        """Wait until one shared NCBI execution slot becomes available."""

        self.semaphore.acquire()

    def release(self) -> None:
        """Return one NCBI execution slot to the shared pool."""

        self.semaphore.release()

def classify_ncbi_request_failure(
    result: subprocess.CompletedProcess[str],
) -> str:
    """Classify a failed NCBI Datasets command conservatively.

    Only explicit evidence is assigned to a specific category. Unknown or
    ambiguous failures remain NCBI_REQUEST_FAILED rather than guessing at a
    more specific cause.

    The classifier may inspect stderr and stdout because different Datasets
    versions can expose external-service errors through either stream.
    """

    stderr = (result.stderr or "").lower()
    stdout = (result.stdout or "").lower()
    combined = f"{stderr}\n{stdout}"

    # FungiFunk itself uses return code 124 when subprocess.run() exceeds the
    # configured timeout. Also recognize explicit timeout wording emitted by
    # the external command.
    if (
        result.returncode == 124
        or "timeout:" in stderr
        or "timed out" in combined
    ):
        return "NCBI_REQUEST_TIMEOUT"

    # Assign a rate-limit category only when the error explicitly indicates
    # HTTP 429 or rate-limiting language.
    rate_limit_markers = (
        "too many requests",
        "rate limit",
        "rate-limit",
        "http 429",
        "status code 429",
        "status=429",
    )

    if any(marker in combined for marker in rate_limit_markers):
        return "NCBI_RATE_LIMITED"

    # NCBI Datasets can report upstream/API gateway failures explicitly.
    # HTTP 502 and 504 are also gateway-specific status codes.
    gateway_markers = (
        "[gateway]",
        "bad gateway",
        "gateway timeout",
        "http 502",
        "http 504",
        "status code 502",
        "status code 504",
        "status=502",
        "status=504",
    )

    if any(marker in combined for marker in gateway_markers):
        return "NCBI_GATEWAY_ERROR"

    # Do not infer a more specific cause when the external command provides
    # insufficient evidence.
    return "NCBI_REQUEST_FAILED"


def ncbi_request_failure_guidance(
    reason: str,
) -> tuple[str, str]:
    """Return concise user-facing explanation and recovery guidance.

    Machine-readable NCBI failure reasons remain stable for reporting and
    downstream logic. This helper only controls their human-readable
    presentation when an NCBI Datasets command ultimately fails.
    """

    guidance = {
        "NCBI_REQUEST_TIMEOUT": (
            "The NCBI Datasets request timed out.",
            (
                "Check network and NCBI availability, then retry the request."
            ),
        ),
        "NCBI_RATE_LIMITED": (
            "NCBI temporarily rate-limited the request.",
            (
                "Wait before retrying; if the problem persists, check NCBI "
                "API-key and rate-limit settings."
            ),
        ),
        "NCBI_GATEWAY_ERROR": (
            "NCBI returned a temporary gateway or service error.",
            (
                "Try the request again later."
            ),
        ),
        "NCBI_REQUEST_FAILED": (
            (
                "The request could not be completed and no more specific "
                "cause was detected."
            ),
            (
                "Review the NCBI message, check the request/input and NCBI "
                "availability, then retry."
            ),
        ),
    }

    try:
        return guidance[reason]
    except KeyError as exc:
        raise DataIntegrityError(
            f"No user-facing guidance is defined for NCBI failure reason: {reason}"
        ) from exc


def run_ncbi_command(
    cmd: Sequence[str],
    *,
    limiter: LocalRateLimiter | SharedLimiterProxy,
    concurrency_limiter: SharedConcurrencyLimiterProxy | None = None,
    log_path: Path | None = None,
    timeout: int | None = None,
    allow_no_genome_data: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an NCBI Datasets command with bounded retry/backoff.

    We never print command environments, so NCBI_API_KEY remains secret. The
    Datasets CLI reads NCBI_API_KEY automatically from the environment.

    Two independent protections may be applied:

    1. ``limiter`` controls how frequently NCBI commands may start.
    2. ``concurrency_limiter`` controls how many NCBI commands may remain alive
       simultaneously across worker processes.

    The concurrency limiter is optional because sequential NCBIClient calls
    during taxonomy and assembly discovery do not need a shared worker
    semaphore.

    When explicitly allowed by the caller, the known NCBI response for a valid
    taxon with no available genome data is returned without retrying. This lets
    genome-inventory code interpret that case as an empty result rather than as
    an external-tool failure.

    If all retries fail, classify the final external failure into a stable
    machine-readable NCBI request reason. This keeps retrieval failures distinct
    from biological/data-availability outcomes such as NO_PROTEIN_FASTA.
    """

    last: subprocess.CompletedProcess[str] | None = None

    for attempt in range(1, NCBI_MAX_RETRIES + 1):

        # Worker downloads may share a bounded number of simultaneous NCBI
        # command slots. Sequential taxonomy/inventory calls pass None here and
        # therefore retain their existing behaviour.
        if concurrency_limiter is not None:
            concurrency_limiter.acquire()

        try:
            # Apply the request-start rate limit immediately before launching
            # the external command. When worker concurrency is enabled, the
            # worker already owns one NCBI execution slot at this point.
            limiter.wait()

            try:
                result = run_capture(
                    cmd,
                    timeout=timeout,
                )

            except subprocess.TimeoutExpired as exc:
                # Convert a Python subprocess timeout into the same result
                # structure used for ordinary external-command failures.
                # Return code 124 is FungiFunk's explicit timeout marker.
                result = subprocess.CompletedProcess(
                    list(cmd),
                    124,
                    "",
                    f"Timeout: {exc}",
                )

        finally:
            # Hold the slot for the complete lifetime of the datasets process,
            # but release it immediately once that process has finished. Log
            # writing and retry backoff must not consume an NCBI concurrency
            # slot.
            if concurrency_limiter is not None:
                concurrency_limiter.release()

        last = result

        # Preserve every attempt in the existing NCBI command log.
        if log_path is not None:
            log_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with log_path.open(
                "a",
                encoding="utf-8",
            ) as handle:

                handle.write(
                    f"\n[{now_iso()}] attempt={attempt} "
                    f"returncode={result.returncode}\n"
                )

                if result.stdout:
                    handle.write(
                        "STDOUT:\n"
                        + result.stdout
                        + "\n"
                    )

                if result.stderr:
                    handle.write(
                        "STDERR:\n"
                        + result.stderr
                        + "\n"
                    )

        # A zero return code means the command completed normally.
        if result.returncode == 0:
            return result

        # NCBI Datasets returns exit status 1 when a TaxID is valid but has no
        # associated genome data. For genome-inventory queries this is an
        # expected empty result, not a transient request failure, so do not
        # retry or classify it as an NCBI retrieval error.
        if (
            allow_no_genome_data
            and result.returncode == 1
            and "no genome data is currently available for this taxon"
            in (result.stderr or "").lower()
        ):
            return result

        # Perform retry backoff only after releasing the concurrency slot.
        # This prevents a failed command from occupying scarce NCBI capacity
        # while merely waiting before its next attempt.
        if attempt < NCBI_MAX_RETRIES:
            time.sleep(
                NCBI_RETRY_BASE_SECONDS
                * (2 ** (attempt - 1))
            )

    assert last is not None

    # Classify only after all configured retries have failed. Intermediate
    # transient errors remain available in the command log but do not affect
    # the final build unless the operation ultimately cannot be completed.
    reason = classify_ncbi_request_failure(last)

    reason_text, recommended_action = (
        ncbi_request_failure_guidance(reason)
    )

    # stderr is normally the most useful diagnostic stream, but some versions
    # or failure modes may expose the relevant service message through stdout.
    ncbi_message = (
        (last.stderr or "").strip()
        or (last.stdout or "").strip()
        or "No additional message was returned by NCBI Datasets."
    )

    detail = (
        f"NCBI Datasets request failed after "
        f"{NCBI_MAX_RETRIES} attempts.\n"
        f"Reason: {reason_text}\n"
        f"NCBI message: {ncbi_message[:1500]}\n"
        f"Recommended action: {recommended_action}\n"
        f"Command: {' '.join(cmd)}"
    )

    raise NCBIRequestError(
        reason,
        detail,
        returncode=last.returncode,
    )


def parse_json_records(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    records: list[dict[str, Any]] = []
    # --as-json-lines normally emits one JSON object per lineThis fallback
    # also accepts a regular JSON object/array so minor CLI presentation changes
    # do not immediately break FungiFunk.
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExternalToolError(f"Could not parse NCBI JSON line {lineno}: {exc}") from exc
            if isinstance(obj, dict):
                records.append(obj)
        return records
    if isinstance(whole, list):
        return [x for x in whole if isinstance(x, dict)]
    if isinstance(whole, dict):
        for key in ("reports", "records", "taxonomies", "assemblies"):
            if isinstance(whole.get(key), list):
                return [x for x in whole[key] if isinstance(x, dict)]
        return [whole]
    return []


class NCBIClient:
    def __init__(self, rps: float, log_dir: Path):
        self.limiter = LocalRateLimiter(rps)
        self.log_dir = log_dir

    def taxonomy(self, query: str | int, *, children: bool = False, parents: bool = False, rank: str | None = None) -> list[dict[str, Any]]:
        cmd = ["datasets", "summary", "taxonomy", "taxon", str(query), "--as-json-lines", "--limit", "all"]
        if children:
            cmd.append("--children")
        if parents:
            cmd.append("--parents")
        if rank:
            cmd.extend(["--rank", rank])
        result = run_ncbi_command(cmd, limiter=self.limiter, log_path=self.log_dir / "ncbi_taxonomy.log")
        return parse_json_records(result.stdout)

    def genomes_for_species(
        self,
        taxid: int,
        *,
        exclude_multi_isolate: bool = False,
    ) -> list[dict[str, Any]]:
        """Return genome records available for one exact species TaxID."""

        # Do not use --tax-exact-match here. NCBI often assigns assemblies to
        # strain-, isolate-, or other below-species TaxIDs rather than directly to
        # the species TaxID. Those descendants are valid assemblies of the selected
        # species and must remain discoverable.
        cmd = [
            "datasets",
            "summary",
            "genome",
            "taxon",
            str(taxid),
            "--as-json-lines",
            "--limit",
            "all",
        ]

        if exclude_multi_isolate:
            cmd.append("--exclude-multi-isolate")

        result = run_ncbi_command(
            cmd,
            limiter=self.limiter,
            log_path=self.log_dir / "ncbi_genome_summary.log",
            allow_no_genome_data=True,
        )

        # NCBI Datasets uses exit status 1 when the TaxID is valid but no genome
        # data are available. run_ncbi_command accepts only that specific known
        # case here, which represents a valid species with zero genome assemblies.
        if result.returncode != 0:
            return []

        return parse_json_records(result.stdout)

    def genome_by_accession(
        self,
        accession: str,
        *,
        exclude_multi_isolate: bool = False,
    ) -> dict[str, Any] | None:
        """Return the exact requested versioned assembly record, if available.

        Explicit assembly targets are version-specific. Request all assembly
        versions from NCBI and then require an exact accession.version match
        rather than accepting a current or substituted assembly version.
        """

        requested = accession.strip().upper()

        cmd = [
            "datasets",
            "summary",
            "genome",
            "accession",
            requested,
            "--assembly-version",
            "all",
            "--as-json-lines",
            "--limit",
            "all",
        ]

        if exclude_multi_isolate:
            cmd.append("--exclude-multi-isolate")

        result = run_ncbi_command(
            cmd,
            limiter=self.limiter,
            log_path=self.log_dir / "ncbi_genome_summary.log",
            allow_no_genome_data=True,
        )

        if result.returncode != 0:
            return None

        exact: list[dict[str, Any]] = []

        for record in parse_json_records(result.stdout):

            observed = str(
                first_nonmissing(
                    record.get("accession"),
                    record.get("current_accession"),
                    record.get("currentAccession"),
                )
                or ""
            ).strip().upper()

            if observed == requested:
                exact.append(record)

        if not exact:
            return None

        if len(exact) != 1:
            raise ExternalToolError(
                f"NCBI returned {len(exact)} exact records for assembly "
                f"{requested}; expected exactly one."
            )

        return exact[0]


########################################################################################################
# Taxonomy curation
########################################################################################################

@dataclass(frozen=True)
class ResolvedTaxonRule:
    input_line: int
    input_name: str
    input_taxid: str
    taxid: int
    name: str
    rank: str
    depth: int
    descendants: bool
    tier: int | None
    evidence_type: str
    reference: str
    notes: str
    reason: str
    explicit_species: bool


@dataclass
class SpeciesCandidate:
    taxid: int
    species: str
    phylum: str | None = None
    class_name: str | None = None
    order_name: str | None = None
    family: str | None = None
    genus: str | None = None
    authority: str | None = None
    tier: int = 3
    seed_taxa: list[str] = field(default_factory=list)
    tier_source: str = "."
    explicit_include: bool = False
    evidence_type: str = "."
    reference: str = "."
    notes: str = "."
    gut_evidence: bool = False
    human_evidence: bool = False
    inclusion_reason: str = "."
    matched_exclusion_taxa: list[str] = field(default_factory=list)
    matched_exclusion_taxids: list[int] = field(default_factory=list)
    exclusion_override: bool = False
    final_taxonomic_status: str = "INCLUDED"
    taxonomy_record: dict[str, Any] = field(default_factory=dict, repr=False)


def taxonomy_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("taxonomy")
    return payload if isinstance(payload, Mapping) else record


def taxonomy_taxid(record: Mapping[str, Any]) -> int | None:
    payload = taxonomy_payload(record)
    raw = first_nonmissing(payload.get("taxId"), payload.get("taxid"), payload.get("tax_id"))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def taxonomy_rank(record: Mapping[str, Any]) -> str:
    payload = taxonomy_payload(record)
    return str(payload.get("rank") or "").strip().lower()


def taxonomy_name(record: Mapping[str, Any]) -> str:
    """Return the current scientific name from an NCBI taxonomy record."""

    payload = taxonomy_payload(record)

    # NCBI Datasets output may use snake_case or camelCase field names,
    # depending on the representation/version. Support both explicitly.
    current = first_nonmissing(
        payload.get("current_scientific_name"),
        payload.get("currentScientificName"),
    )

    if isinstance(current, Mapping):
        name = str(current.get("name") or "").strip()
        if name:
            return name

    if current:
        # Some representations may provide the scientific name directly
        # instead of storing it inside a nested object.
        return str(current).strip()

    # Keep compatibility fallbacks for alternative NCBI representations.
    return str(
        first_nonmissing(
            payload.get("organism_name"),
            payload.get("organismName"),
            payload.get("name"),
        )
        or ""
    ).strip()


def taxonomy_authority(record: Mapping[str, Any]) -> str | None:
    """Return the authority of the current scientific name, when available."""

    payload = taxonomy_payload(record)

    # Support both snake_case and camelCase NCBI taxonomy representations.
    current = first_nonmissing(
        payload.get("current_scientific_name"),
        payload.get("currentScientificName"),
    )

    if isinstance(current, Mapping):
        return normalize_missing(current.get("authority"))

    return None


def taxonomy_parents(record: Mapping[str, Any]) -> list[int]:
    payload = taxonomy_payload(record)
    raw = payload.get("parents") or []
    result: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
    return result


def classification_value(record: Mapping[str, Any], rank: str) -> tuple[str | None, int | None]:
    classification = taxonomy_payload(record).get("classification")
    if not isinstance(classification, Mapping):
        return None, None
    candidates = [rank, rank.lower(), rank.capitalize(), f"{rank}Name"]
    raw: Any = None
    for key in candidates:
        if key in classification:
            raw = classification[key]
            break
    if isinstance(raw, Mapping):
        name = normalize_missing(first_nonmissing(raw.get("name"), raw.get("taxName"), raw.get("scientificName")))
        tid = first_nonmissing(raw.get("id"), raw.get("taxId"), raw.get("taxid"))
        try:
            return (str(name) if name is not None else None, int(tid) if tid is not None else None)
        except (TypeError, ValueError):
            return (str(name) if name is not None else None, None)
    if raw is not None:
        return str(raw), None
    # Some NCBI representations flatten as e.g. phylumName/phylumTaxId.
    name = normalize_missing(classification.get(f"{rank}Name"))
    tid = normalize_missing(classification.get(f"{rank}TaxId"))
    try:
        return (str(name) if name is not None else None, int(tid) if tid is not None else None)
    except (TypeError, ValueError):
        return (str(name) if name is not None else None, None)


def resolve_taxon_query(client: NCBIClient, name: str, taxid_text: str) -> tuple[dict[str, Any], int]:
    """Resolve name/TaxID conservatively; never choose among ambiguous matches.

    Important NCBI Datasets detail: ``--parents`` adds ancestor taxa as separate
    result records. Therefore resolution itself is performed WITHOUT --parents.
    Once one TaxID is unambiguously selected, we query that exact TaxID again
    with --parents so rule specificity/lineage can be recorded safely.
    """
    provided_taxid: int | None = None
    if taxid_text and taxid_text != ".":
        try:
            provided_taxid = int(taxid_text)
        except ValueError as exc:
            raise ConfigError(f"Invalid NCBI TaxID {taxid_text!r} for taxon {name!r}.") from exc

    if provided_taxid is not None:
        base_records = client.taxonomy(provided_taxid)
        exact = [r for r in base_records if taxonomy_taxid(r) == provided_taxid]
        if len(exact) != 1:
            raise ConfigError(f"NCBI did not resolve TaxID {provided_taxid} uniquely for {name!r}.")
        if name and name != ".":
            name_records = client.taxonomy(name)
            name_taxids = {taxonomy_taxid(r) for r in name_records}
            if provided_taxid not in name_taxids:
                raise ConfigError(
                    f"Taxon name / TaxID mismatch: input name {name!r} does not resolve to TaxID {provided_taxid}. "
                    f"NCBI current name for that TaxID is {taxonomy_name(exact[0])!r}."
                )
        chosen_taxid = provided_taxid
    else:
        if not name or name == ".":
            raise ConfigError(
                "Each taxonomy rule must provide Taxon_name, and Taxid may only be omitted when the name is resolvable."
            )
        name_records = client.taxonomy(name)
        unique_by_taxid = {taxonomy_taxid(r): r for r in name_records if taxonomy_taxid(r) is not None}
        if len(unique_by_taxid) != 1:
            matches = ", ".join(
                f"{tid}:{taxonomy_name(rec)}" for tid, rec in sorted(unique_by_taxid.items())
            ) or "none"
            raise ConfigError(
                f"Taxon name {name!r} did not resolve to exactly one NCBI TaxID (matches: {matches}). "
                "Provide an explicit Taxid to remove ambiguity."
            )
        chosen_taxid = int(next(iter(unique_by_taxid)))

    # Re-query the exact resolved TaxID with ancestors included. Select only the
    # requested TaxID from that result; the other records are lineage context.
    parent_records = client.taxonomy(chosen_taxid, parents=True)
    chosen = [r for r in parent_records if taxonomy_taxid(r) == chosen_taxid]
    if len(chosen) != 1:
        raise ExternalToolError(
            f"NCBI taxonomy parent query did not return exactly one record for resolved TaxID {chosen_taxid}."
        )
    return chosen[0], chosen_taxid


def parse_seed_rules(
    path: Path,
    client: NCBIClient,
    *,
    allow_no_data_rows: bool = False,
) -> list[ResolvedTaxonRule]:
    required = ["Taxon_name", "Taxid", "Include_descendants", "Tier", "Evidence_type", "Reference", "Notes"]
    rows = read_commentable_tsv(
        path,
        required,
        allow_no_data_rows=allow_no_data_rows,
    )
    rules: list[ResolvedTaxonRule] = []
    # Total number of active seed taxa to resolve.
    # This allows progress messages to show a meaningful denominator.
    total_seeds = len(rows)

    if total_seeds:
        info(
            f"Checking and validating {total_seeds} input taxa against NCBI."
        )

    # Enumerate seed taxa so that we know which input row is being resolved.
    for seed_index, row in enumerate(rows, start=1):
        line = int(row["__line__"])
        record, taxid = resolve_taxon_query(client, row["Taxon_name"], row["Taxid"])
        rank = taxonomy_rank(record)
        name = taxonomy_name(record)
        descendants = parse_bool(row["Include_descendants"], field_name=f"Include_descendants line {line}")
        try:
            tier = int(row["Tier"])
        except ValueError as exc:
            raise ConfigError(f"Tier must be 1, 2, or 3 at {path}:{line}.") from exc
        if tier not in {1, 2, 3}:
            raise ConfigError(f"Tier must be 1, 2, or 3 at {path}:{line}; received {tier}.")
        is_species = rank == "species"
        if is_species and descendants:
            raise ConfigError(
                f"{path}:{line}: Taxon_name {name!r} resolves to NCBI rank 'species', "
                "but Include_descendants=TRUE. Species-level seed entries must use "
                "Include_descendants=FALSE. Change this row to FALSE and rerun."
            )
        if not is_species and not descendants:
            raise ConfigError(
                f"{path}:{line}: higher taxon {name!r} ({rank}) must use Include_descendants=TRUE."
            )
        rules.append(
            ResolvedTaxonRule(
                input_line=line, input_name=row["Taxon_name"], input_taxid=row["Taxid"],
                taxid=taxid, name=name, rank=rank, depth=len(taxonomy_parents(record)),
                descendants=descendants, tier=tier, evidence_type=row["Evidence_type"] or ".",
                reference=row["Reference"] or ".", notes=row["Notes"] or ".", reason=".",
                explicit_species=is_species and not descendants,
            )
        )
        # Report progress every five completed taxa.
        # Leave the final count for the explicit completion message below.
        if seed_index % 5 == 0 and seed_index != total_seeds:
            info(
                f"Input taxa identified and validated: "
                f"{seed_index}/{total_seeds}."
            )
    # Confirm that all input taxa have been identified and validated.
    # This message marks the end of the first curation subphase.
    if total_seeds:
        info(
            f"Input taxon identification and validation complete: "
            f"{total_seeds}/{total_seeds}."
        )

    return rules


def parse_exclusion_rules(
    path: Path,
    client: NCBIClient,
    *,
    warn_if_empty: bool = False,
) -> list[ResolvedTaxonRule]:
    required = ["Taxon_name", "Taxid", "Exclude_descendants", "Reason", "Reference", "Notes"]
    rows = read_commentable_tsv(
        path,
        required,
        allow_no_data_rows=True,
    )

    if not rows:
        if warn_if_empty:
            warn(
                "The supplied exclusion taxa file contains no active exclusion "
                f"rules: {path}. No taxa will be excluded by explicit taxonomic "
                "exclusion rules."
            )

        return []
    rules: list[ResolvedTaxonRule] = []
    for row in rows:
        line = int(row["__line__"])
        record, taxid = resolve_taxon_query(client, row["Taxon_name"], row["Taxid"])
        rank = taxonomy_rank(record)
        name = taxonomy_name(record)
        descendants = parse_bool(row["Exclude_descendants"], field_name=f"Exclude_descendants line {line}")
        is_species = rank == "species"
        if is_species and descendants:
            # For a species TRUE/FALSE has the same practical descendant-species
            # result, but the frozen contract treats FALSE as the explicit form.
            warn(f"{path}:{line}: species-level exclusion {name!r} used TRUE; normalizing semantics to explicit species exclusion.")
            descendants = False
        if not is_species and not descendants:
            raise ConfigError(
                f"{path}:{line}: higher taxon {name!r} ({rank}) must use Exclude_descendants=TRUE."
            )
        reason = (row["Reason"] or "").strip()
        if not reason or reason == ".":
            raise ConfigError(f"{path}:{line}: Reason is required for every exclusion rule.")
        rules.append(
            ResolvedTaxonRule(
                input_line=line, input_name=row["Taxon_name"], input_taxid=row["Taxid"],
                taxid=taxid, name=name, rank=rank, depth=len(taxonomy_parents(record)),
                descendants=descendants, tier=None, evidence_type=".",
                reference=row["Reference"] or ".", notes=row["Notes"] or ".", reason=reason,
                explicit_species=is_species,
            )
        )
    return rules


def species_record_from_taxonomy(record: Mapping[str, Any]) -> SpeciesCandidate:
    taxid = taxonomy_taxid(record)
    if taxid is None:
        raise ExternalToolError("NCBI taxonomy species record is missing taxId.")
    name = taxonomy_name(record)
    if not name:
        raise ExternalToolError(f"NCBI taxonomy record for TaxID {taxid} is missing current scientific name.")
    phylum, _ = classification_value(record, "phylum")
    class_name, _ = classification_value(record, "class")
    order_name, _ = classification_value(record, "order")
    family, _ = classification_value(record, "family")
    genus, _ = classification_value(record, "genus")
    return SpeciesCandidate(
        taxid=taxid, species=name, phylum=phylum, class_name=class_name,
        order_name=order_name, family=family, genus=genus,
        authority=taxonomy_authority(record), taxonomy_record=dict(record),
    )


def expand_rule_to_species(client: NCBIClient, rule: ResolvedTaxonRule) -> dict[int, SpeciesCandidate]:
    if rule.explicit_species:
        records = client.taxonomy(rule.taxid)
        exact = [r for r in records if taxonomy_taxid(r) == rule.taxid]
    else:
        records = client.taxonomy(rule.taxid, children=True, rank="species")
        exact = [r for r in records if taxonomy_rank(r) == "species"]
    result: dict[int, SpeciesCandidate] = {}
    for record in exact:
        candidate = species_record_from_taxonomy(record)
        result[candidate.taxid] = candidate
    if rule.explicit_species and rule.taxid not in result:
        raise ExternalToolError(f"Explicit species TaxID {rule.taxid} did not return a species-level NCBI record.")
    return result


def build_curated_species(
    client: NCBIClient,
    seed_rules: list[ResolvedTaxonRule],
    exclusion_rules: list[ResolvedTaxonRule],
    explicit_include_targets: Sequence["ResolvedExplicitTarget"] = (),
) -> tuple[
    dict[int, SpeciesCandidate],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[int, SpeciesCandidate],
    dict[int, int],
    dict[int, int],
]:
    """Expand seeds, resolve Tier precedence, then apply taxonomic exclusions.

    The final two dictionaries report how many species each input rule matched.
    They let the resolved-rule reports expose unexpectedly broad curation rules
    without issuing a second set of NCBI taxonomy expansion requests.
    """
    candidates: dict[int, SpeciesCandidate] = {}
    seed_matches: dict[int, list[ResolvedTaxonRule]] = defaultdict(list)
    seed_descendant_counts: dict[int, int] = {}
    exclusion_descendant_counts: dict[int, int] = {}

    # Count the seed taxa and explicit inclusion targets that can admit species.
    total_seed_rules = len(seed_rules)
    total_explicit_targets = len(explicit_include_targets)

    # Clearly announce the beginning of the second curation subphase.
    if total_seed_rules:
        info(
            f"Starting species-set construction from "
            f"{total_seed_rules} validated input taxa."
        )
    elif total_explicit_targets:
        target_noun = (
            "target"
            if total_explicit_targets == 1
            else "targets"
        )

        info(
            "No seed taxa supplied; species admission is driven by "
            f"{total_explicit_targets} explicit {target_noun}."
        )

    # Number each taxon while preserving its name, TaxID and taxonomic rank.
    for seed_index, rule in enumerate(seed_rules, start=1):
        info(
            f"Expanding seed taxon {seed_index}/{total_seed_rules}: "
            f"{rule.name} (TaxID {rule.taxid}, rank={rule.rank})"
        )
        expanded = expand_rule_to_species(client, rule)
        seed_descendant_counts[rule.input_line] = len(expanded)
        for taxid, candidate in expanded.items():
            candidates.setdefault(taxid, candidate)
            seed_matches[taxid].append(rule)

    conflicts: list[dict[str, Any]] = []
    for taxid, candidate in candidates.items():
        matches = seed_matches[taxid]
        explicit = [r for r in matches if r.explicit_species and r.taxid == taxid]
        priority = explicit if explicit else [r for r in matches if r.depth == max(x.depth for x in matches)]
        tiers = {r.tier for r in priority}
        if len(tiers) != 1:
            conflicts.append({
                "Type": "TIER_CONFLICT", "Species": candidate.species, "Taxid": taxid,
                "Rules": ";".join(f"line={r.input_line}:{r.name}:Tier{r.tier}" for r in priority),
            })
            continue
        chosen = sorted(priority, key=lambda r: (r.input_line, r.taxid))[0]
        candidate.tier = int(chosen.tier or 3)
        candidate.tier_source = "EXPLICIT_SPECIES" if explicit else chosen.name
        candidate.explicit_include = bool(explicit)
        candidate.seed_taxa = sorted({r.name for r in matches})
        candidate.evidence_type = merge_text_values(r.evidence_type for r in matches)
        candidate.reference = merge_text_values(r.reference for r in matches)
        candidate.notes = merge_text_values(r.notes for r in matches)
        evidence_lower = candidate.evidence_type.lower()
        candidate.gut_evidence = "human_gut" in evidence_lower or "gut" in evidence_lower
        candidate.human_evidence = candidate.gut_evidence or "human_association" in evidence_lower or "human" in evidence_lower
        candidate.inclusion_reason = "EXPLICIT_SPECIES_INCLUDE" if candidate.explicit_include else f"DESCENDANT_OF:{chosen.name}"

    if conflicts:
        raise CurationConflict(
            "Unresolved equal-priority Tier assignments were found in fungal_seed_taxa.tsv.",
            conflicts,
        )

    # Explicit inclusion targets may introduce species that are not present in
    # the automatic seed-derived species set. Keep this distinct from
    # SpeciesCandidate.explicit_include, which means an explicit species-level
    # seed and therefore has broader taxonomic inclusion semantics.
    explicit_targets_by_species: dict[
        int,
        list["ResolvedExplicitTarget"],
    ] = defaultdict(list)

    for target in explicit_include_targets:
        explicit_targets_by_species[
            target.species_taxid
        ].append(target)

    for taxid, targets in explicit_targets_by_species.items():

        if taxid in candidates:
            continue

        species_record = targets[0].species_record

        if not species_record:
            raise DataIntegrityError(
                f"Resolved explicit target species TaxID {taxid} has no "
                "canonical taxonomy record."
            )

        candidate = species_record_from_taxonomy(
            species_record
        )

        if candidate.taxid != taxid:
            raise DataIntegrityError(
                "Resolved explicit target taxonomy identity changed "
                f"unexpectedly: target TaxID {taxid}, taxonomy record "
                f"TaxID {candidate.taxid}."
            )

        # Explicit-target-only species have no seed Tier assignment. Retain the
        # normal conservative default Tier 3 and make the provenance explicit.
        candidate.tier = 3
        candidate.tier_source = "EXPLICIT_TARGET"
        candidate.explicit_include = False
        candidate.seed_taxa = []
        candidate.inclusion_reason = "EXPLICIT_TARGET_INCLUDE"

        candidates[taxid] = candidate

    explicit_target_species_taxids = set(
        explicit_targets_by_species
    )

    # Build exclusion-match map only for candidate species. We deliberately do
    # not treat "uncertain" morphology as an exclusion: only explicit taxonomy
    # rules supplied by the curator can remove a species.
    exclusion_matches: dict[int, list[ResolvedTaxonRule]] = defaultdict(list)
    for rule in exclusion_rules:
        info(f"Expanding exclusion taxon {rule.name} (TaxID {rule.taxid}, rank={rule.rank})")
        expanded = expand_rule_to_species(client, rule)
        exclusion_descendant_counts[rule.input_line] = len(expanded)
        for taxid in expanded:
            if taxid in candidates:
                exclusion_matches[taxid].append(rule)

    excluded_rows: list[dict[str, Any]] = []
    explicit_conflicts: list[dict[str, Any]] = []
    for taxid, candidate in candidates.items():
        matches = exclusion_matches.get(taxid, [])
        if not matches:
            continue
        candidate.matched_exclusion_taxa = sorted({r.name for r in matches})
        candidate.matched_exclusion_taxids = sorted({r.taxid for r in matches})
        explicit_exclusions = [
            r
            for r in matches
            if r.explicit_species and r.taxid == taxid
        ]

        target_explicit_include = (
            taxid in explicit_target_species_taxids
        )

        # An exact species-level taxonomic exclusion conflicts with either an
        # explicit species seed or an explicit strain/assembly inclusion target.
        # Do not silently choose one instruction over the other.
        if (
            candidate.explicit_include
            or target_explicit_include
        ) and explicit_exclusions:

            include_kind = (
                "explicit species include"
                if candidate.explicit_include
                else "explicit target include"
            )

            explicit_conflicts.append({
                "Type": "EXPLICIT_INCLUDE_EXCLUDE_CONFLICT",
                "Species": candidate.species,
                "Taxid": taxid,
                "Seed": include_kind,
                "Exclusion_rules": ";".join(
                    f"line={r.input_line}:{r.name}"
                    for r in explicit_exclusions
                ),
            })

            continue

        # Broader taxonomic exclusions may be overridden by an explicit include.
        # For explicit targets this only admits the species to later inventory;
        # 3D-C will restrict selection to the requested target rather than
        # reactivating the entire excluded species.
        if (
            candidate.explicit_include
            or target_explicit_include
        ):
            candidate.exclusion_override = True
            candidate.final_taxonomic_status = "INCLUDED"
            status = "INCLUDED"

            override_reason = (
                "EXPLICIT_SPECIES_INCLUDE"
                if candidate.explicit_include
                else "EXPLICIT_TARGET_INCLUDE"
            )

        else:
            candidate.final_taxonomic_status = "EXCLUDED"
            status = "EXCLUDED"
            override_reason = "."

        excluded_rows.append({
            "Species": candidate.species,
            "TaxID": taxid,
            "Matched_exclusion_taxa": ";".join(
                candidate.matched_exclusion_taxa
            ),
            "Matched_exclusion_taxids": ";".join(
                map(
                    str,
                    candidate.matched_exclusion_taxids,
                )
            ),
            "Exclusion_reasons": merge_text_values(
                r.reason
                for r in matches
            ),
            "Exclusion_override": (
                candidate.exclusion_override
            ),
            "Override_reason": override_reason,
            "Final_status": status,
        })

    if explicit_conflicts:
        raise CurationConflict(
            "One or more species are explicitly included and explicitly excluded at the same specificity. Edit the TSV curation and resume; no assembly/protein processing has started.",
            explicit_conflicts,
        )

    included = {tid: c for tid, c in candidates.items() if c.final_taxonomic_status == "INCLUDED"}
    return included, excluded_rows, conflicts, candidates, seed_descendant_counts, exclusion_descendant_counts


def write_resolved_taxon_rules(path: Path, rules: list[ResolvedTaxonRule], descendant_counts: Mapping[int, int], *, exclusion: bool) -> None:
    fields = [
        "Input_line", "Input_taxon", "Input_taxid", "Resolved_name", "Resolved_taxid", "Rank",
        "Include_descendants" if not exclusion else "Exclude_descendants",
        "Tier" if not exclusion else "Reason", "Evidence_type" if not exclusion else "Reference",
        "Reference" if not exclusion else "Notes", "Notes" if not exclusion else "Explicit_species",
        "Descendant_species_matched",
    ]
    rows: list[dict[str, Any]] = []
    for r in rules:
        row: dict[str, Any] = {
            "Input_line": r.input_line, "Input_taxon": r.input_name, "Input_taxid": r.input_taxid,
            "Resolved_name": r.name, "Resolved_taxid": r.taxid, "Rank": r.rank,
            "Include_descendants" if not exclusion else "Exclude_descendants": r.descendants,
            "Descendant_species_matched": descendant_counts.get(r.input_line, 0),
        }
        if exclusion:
            row.update({"Reason": r.reason, "Reference": r.reference, "Notes": r.notes, "Explicit_species": r.explicit_species})
        else:
            row.update({"Tier": r.tier, "Evidence_type": r.evidence_type, "Reference": r.reference, "Notes": r.notes})
        rows.append(row)
    write_tsv(path, fields, rows)


########################################################################################################
# Explicit strain / assembly targets
########################################################################################################

EXPLICIT_TARGET_COLUMNS = [
    "Target_type",
    "Species",
    "Taxid",
    "Strain",
    "Assembly_accession",
    "Evidence_type",
    "Notes",
]

_ASSEMBLY_ACCESSION_VERSION_RE = re.compile(
    r"^(GCF|GCA)_[0-9]+\.[0-9]+$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExplicitTargetRule:
    action: str
    input_path: str
    input_line: int
    target_type: str

    species: str | None
    input_taxid: str | None
    taxid: int | None

    strain: str | None
    strain_key: str | None

    input_assembly_accession: str | None
    assembly_accession: str | None

    evidence_type: str
    notes: str


def normalize_explicit_species_identity(
    value: str | None,
) -> str | None:
    """Normalize a species label only for local target identity comparisons."""

    value = normalize_missing(value)

    if value is None:
        return None

    return re.sub(
        r"\s+",
        " ",
        value.strip(),
    ).casefold()


def explicit_target_identity_key(
    target: ExplicitTargetRule,
) -> tuple[str, ...]:
    """Return the strongest syntactic identity available before NCBI resolution."""

    if target.target_type == "assembly":

        assert target.assembly_accession is not None

        return (
            "assembly",
            target.assembly_accession,
        )

    assert target.target_type == "strain"
    assert target.strain_key is not None

    if target.taxid is not None:

        species_identity = (
            "taxid",
            str(target.taxid),
        )

    else:

        species_key = normalize_explicit_species_identity(
            target.species,
        )

        assert species_key is not None

        species_identity = (
            "name",
            species_key,
        )

    return (
        "strain",
        *species_identity,
        target.strain_key,
    )


def explicit_target_display(
    target: ExplicitTargetRule,
) -> str:
    """Render one explicit target for concise validation errors."""

    if target.target_type == "assembly":

        return (
            target.assembly_accession
            or target.input_assembly_accession
            or "<missing assembly>"
        )

    species = (
        f"TaxID {target.taxid}"
        if target.taxid is not None
        else target.species or "<missing species>"
    )

    return (
        f"{species}; strain={target.strain or '<missing strain>'}"
    )


def parse_explicit_targets(
    path: Path | None,
    *,
    action: str,
    warn_if_empty: bool = False,
) -> list[ExplicitTargetRule]:
    """Parse and syntactically validate explicit strain/assembly target rules.

    This stage performs no NCBI lookups. Species identity, strain membership and
    assembly metadata are resolved/validated against NCBI in the later target
    resolution stage.
    """

    if action not in {"include", "exclude"}:
        raise ValueError(
            f"Unsupported explicit-target action: {action!r}"
        )

    if path is None:
        return []

    rows = read_commentable_tsv(
        path,
        EXPLICIT_TARGET_COLUMNS,
        allow_no_data_rows=True,
    )

    if not rows:

        if warn_if_empty:
            warn(
                f"The supplied explicit-{action} target file contains no active "
                f"target rows: {path}."
            )

        return []

    rules: list[ExplicitTargetRule] = []
    seen: dict[tuple[str, ...], ExplicitTargetRule] = {}

    for row in rows:

        line = int(row["__line__"])

        target_type = (
            normalize_missing(row["Target_type"])
            or ""
        ).casefold()

        if target_type not in {"assembly", "strain"}:
            raise ConfigError(
                f"{path}:{line}: Target_type must be 'assembly' or 'strain'; "
                f"received {row['Target_type']!r}."
            )

        species = normalize_missing(
            row["Species"]
        )

        input_taxid = normalize_missing(
            row["Taxid"]
        )

        taxid: int | None = None

        if input_taxid is not None:

            try:
                taxid = int(input_taxid)

            except ValueError as exc:
                raise ConfigError(
                    f"{path}:{line}: Taxid must be a positive integer when "
                    f"provided; received {input_taxid!r}."
                ) from exc

            if taxid <= 0:
                raise ConfigError(
                    f"{path}:{line}: Taxid must be a positive integer when "
                    f"provided; received {input_taxid!r}."
                )

        strain = normalize_missing(
            row["Strain"]
        )

        strain_key = (
            normalize_selection_label(strain)
            if strain is not None
            else None
        )

        input_assembly_accession = normalize_missing(
            row["Assembly_accession"]
        )

        assembly_accession: str | None = None

        if input_assembly_accession is not None:

            if not _ASSEMBLY_ACCESSION_VERSION_RE.fullmatch(
                input_assembly_accession
            ):
                raise ConfigError(
                    f"{path}:{line}: Assembly_accession must be an exact "
                    "versioned NCBI assembly accession such as "
                    f"GCF_000001405.40 or GCA_000001405.29; received "
                    f"{input_assembly_accession!r}."
                )

            assembly_accession = (
                input_assembly_accession.upper()
            )

        if target_type == "assembly":

            if assembly_accession is None:
                raise ConfigError(
                    f"{path}:{line}: Target_type='assembly' requires "
                    "Assembly_accession."
                )

        else:

            if strain is None or strain_key is None:
                raise ConfigError(
                    f"{path}:{line}: Target_type='strain' requires Strain."
                )

            if species is None and taxid is None:
                raise ConfigError(
                    f"{path}:{line}: Target_type='strain' requires at least "
                    "one species identifier: Species or Taxid."
                )

            if assembly_accession is not None:
                raise ConfigError(
                    f"{path}:{line}: Assembly_accession must be empty for "
                    "Target_type='strain'. Use Target_type='assembly' to target "
                    "an exact assembly accession."
                )

        rule = ExplicitTargetRule(
            action=action,
            input_path=str(path),
            input_line=line,
            target_type=target_type,
            species=species,
            input_taxid=input_taxid,
            taxid=taxid,
            strain=strain,
            strain_key=strain_key,
            input_assembly_accession=input_assembly_accession,
            assembly_accession=assembly_accession,
            evidence_type=(
                normalize_missing(
                    row["Evidence_type"]
                )
                or "."
            ),
            notes=(
                normalize_missing(
                    row["Notes"]
                )
                or "."
            ),
        )

        identity = explicit_target_identity_key(
            rule
        )

        previous = seen.get(identity)

        if previous is not None:
            raise ConfigError(
                f"{path}:{line}: duplicate explicit {action} target "
                f"{explicit_target_display(rule)!r}; the same target was "
                f"already defined at line {previous.input_line}."
            )

        seen[identity] = rule
        rules.append(rule)

    return rules


def detect_explicit_target_conflicts(
    include_targets: Sequence[ExplicitTargetRule],
    exclude_targets: Sequence[ExplicitTargetRule],
) -> None:
    """Reject targets that are syntactically both included and excluded."""

    includes = {
        explicit_target_identity_key(target): target
        for target in include_targets
    }

    for excluded in exclude_targets:

        identity = explicit_target_identity_key(
            excluded
        )

        included = includes.get(identity)

        if included is None:
            continue

        raise ConfigError(
            "Explicit target conflict: the same target is both included and "
            "excluded.\n\n"
            f"Target: {explicit_target_display(excluded)}\n"
            f"Include: {included.input_path}:{included.input_line}\n"
            f"Exclude: {excluded.input_path}:{excluded.input_line}"
        )


########################################################################################################
# Generic metadata filter engine
########################################################################################################

@dataclass(frozen=True)
class FieldSpec:
    level: str
    data_type: str  # text, integer, float, date, bool
    getter_key: str


SPECIES_FIELDS: dict[str, FieldSpec] = {
    "Taxon_name": FieldSpec("species", "text", "species"),
    "Taxid": FieldSpec("species", "integer", "taxid"),
    "Phylum": FieldSpec("species", "text", "phylum"),
    "Class": FieldSpec("species", "text", "class_name"),
    "Order": FieldSpec("species", "text", "order_name"),
    "Family": FieldSpec("species", "text", "family"),
    "Genus": FieldSpec("species", "text", "genus"),
    "Authority": FieldSpec("species", "text", "authority"),
    "Tier": FieldSpec("species", "integer", "tier"),
    "Explicit_include": FieldSpec("species", "bool", "explicit_include"),
    "Evidence_type": FieldSpec("species", "text", "evidence_type"),
    "Reference": FieldSpec("species", "text", "reference"),
    "Notes": FieldSpec("species", "text", "notes"),
    "Gut_evidence": FieldSpec("species", "bool", "gut_evidence"),
    "Human_evidence": FieldSpec("species", "bool", "human_evidence"),
    "Seed_taxa": FieldSpec("species", "text", "seed_taxa"),
    "Tier_source": FieldSpec("species", "text", "tier_source"),
}

ASSEMBLY_FIELDS: dict[str, FieldSpec] = {
    "Assembly_accession": FieldSpec("assembly", "text", "accession"),
    "Assembly_source": FieldSpec("assembly", "text", "source"),
    "Paired_accession": FieldSpec("assembly", "text", "paired_accession"),
    "Assembly_status": FieldSpec("assembly", "text", "assembly_status"),
    "Atypical": FieldSpec("assembly", "bool", "atypical"),
    "Atypical_warnings": FieldSpec("assembly", "text", "atypical_warnings"),
    "Multi_isolate": FieldSpec("assembly", "bool", "multi_isolate"),
    "Assembly_level": FieldSpec("assembly", "text", "assembly_level"),
    "RefSeq_category": FieldSpec("assembly", "text", "refseq_category"),
    "Strain": FieldSpec("assembly", "text", "strain"),
    "Isolate": FieldSpec("assembly", "text", "isolate"),
    "Infraspecific_name": FieldSpec("assembly", "text", "infraspecific_name"),
    "BioProject": FieldSpec("assembly", "text", "bioproject"),
    "BioSample": FieldSpec("assembly", "text", "biosample"),
    "Assembly_method": FieldSpec("assembly", "text", "assembly_method"),
    "Sequencing_technology": FieldSpec("assembly", "text", "sequencing_technology"),
    "Contig_N50": FieldSpec("assembly", "integer", "contig_n50"),
    "Contig_L50": FieldSpec("assembly", "integer", "contig_l50"),
    "Num_contigs": FieldSpec("assembly", "integer", "num_contigs"),
    "Scaffold_N50": FieldSpec("assembly", "integer", "scaffold_n50"),
    "Scaffold_L50": FieldSpec("assembly", "integer", "scaffold_l50"),
    "Num_scaffolds": FieldSpec("assembly", "integer", "num_scaffolds"),
    "Num_scaffold_gaps": FieldSpec("assembly", "integer", "num_scaffold_gaps"),
    "Genome_size": FieldSpec("assembly", "integer", "genome_size"),
    "GC_percent": FieldSpec("assembly", "float", "gc_percent"),
    "Protein_coding_genes": FieldSpec("assembly", "integer", "protein_coding_genes"),
    "BUSCO_complete": FieldSpec("assembly", "float", "busco_complete"),
    "BUSCO_fragmented": FieldSpec("assembly", "float", "busco_fragmented"),
    "BUSCO_missing": FieldSpec("assembly", "float", "busco_missing"),
    "BUSCO_duplicated": FieldSpec("assembly", "float", "busco_duplicated"),
    "Release_date": FieldSpec("assembly", "date", "release_date"),
    "Annotation_name": FieldSpec("assembly", "text", "annotation_name"),
    "Annotation_release_date": FieldSpec("assembly", "date", "annotation_release_date"),
    "Annotation_status": FieldSpec("assembly", "text", "annotation_status"),
    "Protein_FASTA_available": FieldSpec("assembly", "bool", "protein_fasta_available"),
}

FIELD_SPECS = {**SPECIES_FIELDS, **ASSEMBLY_FIELDS}
TEXT_OPERATORS = {"contains", "not_contains", "equals", "not_equals", "starts_with", "ends_with"}
PRESENCE_OPERATORS = {"present", "absent"}
NUMERIC_OPERATORS = {"min", "max"}
DATE_OPERATORS = {"after", "before"}
ALL_FILTER_OPERATORS = TEXT_OPERATORS | PRESENCE_OPERATORS | NUMERIC_OPERATORS | DATE_OPERATORS


@dataclass(frozen=True)
class FilterRule:
    filter_id: str
    level: str
    field: str
    operator: str
    value: str
    case_sensitive: bool
    notes: str
    source: str


def parse_metadata_filters(
    path: Path | None,
    *,
    warn_if_empty: bool = False,
) -> list[FilterRule]:
    if path is None:
        return []
    required = ["Level", "Field", "Operator", "Value", "Case_sensitive", "Notes"]
    rows = read_commentable_tsv(
        path,
        required,
        allow_no_data_rows=True,
    )

    if not rows:
        if warn_if_empty:
            warn(
                "The supplied metadata filters file contains no active filter "
                f"rules: {path}. No custom metadata filters will be applied."
            )

        return []
    rules: list[FilterRule] = []
    for idx, row in enumerate(rows, start=1):
        level = row["Level"].strip().lower()
        field_name = row["Field"].strip()
        operator = row["Operator"].strip().lower()
        value = row["Value"].strip()
        if level not in {"species", "assembly"}:
            raise ConfigError(f"{path}:{row['__line__']}: Level must be species or assembly.")
        spec = FIELD_SPECS.get(field_name)
        if spec is None or spec.level != level:
            supported = ", ".join(sorted(k for k, v in FIELD_SPECS.items() if v.level == level))
            raise ConfigError(
                f"{path}:{row['__line__']}: unsupported {level} Field {field_name!r}. Supported: {supported}"
            )
        if operator not in ALL_FILTER_OPERATORS:
            raise ConfigError(f"{path}:{row['__line__']}: unsupported Operator {operator!r}.")
        if operator in TEXT_OPERATORS and spec.data_type not in {"text", "bool", "integer", "float"}:
            raise ConfigError(f"{path}:{row['__line__']}: {operator} is not supported for {field_name} ({spec.data_type}).")
        if operator in NUMERIC_OPERATORS and spec.data_type not in {"integer", "float"}:
            raise ConfigError(f"{path}:{row['__line__']}: {operator} requires a numeric field.")
        if operator in DATE_OPERATORS and spec.data_type != "date":
            raise ConfigError(f"{path}:{row['__line__']}: {operator} requires a date field.")
        if operator not in PRESENCE_OPERATORS and normalize_missing(value) is None:
            raise ConfigError(f"{path}:{row['__line__']}: Value is required for operator {operator}.")
        case_raw = normalize_missing(row["Case_sensitive"])
        case_sensitive = False if case_raw is None else parse_bool(case_raw, field_name="Case_sensitive")
        # Validate parseable numeric/date literals now so errors happen before
        # any expensive NCBI work.
        if operator in NUMERIC_OPERATORS:
            parse_float(value, f"metadata filter {field_name}")
        if operator in DATE_OPERATORS:
            parse_iso_date(value, f"metadata filter {field_name}")
        rules.append(FilterRule(f"MF{idx:04d}", level, field_name, operator, value, case_sensitive, row["Notes"] or ".", "TSV"))
    detect_obvious_filter_contradictions(rules)
    return rules


def detect_obvious_filter_contradictions(rules: Sequence[FilterRule]) -> None:
    by_field: dict[tuple[str, str], list[FilterRule]] = defaultdict(list)
    for rule in rules:
        by_field[(rule.level, rule.field)].append(rule)
    for (level, field_name), group in by_field.items():
        mins = [parse_float(r.value, field_name) for r in group if r.operator == "min"]
        maxs = [parse_float(r.value, field_name) for r in group if r.operator == "max"]
        if mins and maxs and max(mins) > min(maxs):
            raise ConfigError(
                f"Contradictory metadata filters for {level}.{field_name}: minimum {max(mins)} exceeds maximum {min(maxs)}."
            )
        # Two case-equivalent equals rules under AND cannot both pass unless they
        # are identical under their own requested comparison semantics.
        equals = [r for r in group if r.operator == "equals"]
        if len(equals) > 1:
            normalized: set[str] = set()
            for r in equals:
                normalized.add(r.value if r.case_sensitive else r.value.casefold())
            if len(normalized) > 1 and all(not r.case_sensitive for r in equals):
                raise ConfigError(
                    f"Contradictory metadata filters: {level}.{field_name} has multiple incompatible equals rules under AND."
                )


def cli_filter_rules(config: BuildConfig) -> list[FilterRule]:
    rules: list[FilterRule] = []
    counter = 1

    def add(field_name: str, operator: str, value: Any, *, notes: str = ".") -> None:
        nonlocal counter
        if value is None:
            return
        rules.append(FilterRule(f"CLI{counter:04d}", "assembly", field_name, operator, str(value), False, notes, "CLI"))
        counter += 1

    mapping = [
        ("Contig_N50", "min", config.min_contig_n50),
        ("Contig_L50", "max", config.max_contig_l50),
        ("Num_contigs", "max", config.max_contigs),
        ("Scaffold_N50", "min", config.min_scaffold_n50),
        ("Scaffold_L50", "max", config.max_scaffold_l50),
        ("Num_scaffolds", "max", config.max_scaffolds),
        ("Num_scaffold_gaps", "max", config.max_scaffold_gaps),
        ("Genome_size", "min", config.min_genome_size),
        ("Genome_size", "max", config.max_genome_size),
        ("GC_percent", "min", config.min_gc_percent),
        ("GC_percent", "max", config.max_gc_percent),
        ("Protein_coding_genes", "min", config.min_protein_coding_genes),
        ("Protein_coding_genes", "max", config.max_protein_coding_genes),
        ("BUSCO_complete", "min", config.min_busco_complete),
        ("BUSCO_missing", "max", config.max_busco_missing),
        ("BUSCO_fragmented", "max", config.max_busco_fragmented),
        ("BUSCO_duplicated", "max", config.max_busco_duplicated),
        ("Release_date", "after", config.released_after),
        ("Release_date", "before", config.released_before),
        ("RefSeq_category", "equals", config.refseq_category),
    ]
    for field_name, op, value in mapping:
        add(field_name, op, value)
    if config.require_busco:
        add("BUSCO_complete", "present", ".", notes="--require-busco")
    for text in config.assembly_method_contains:
        add("Assembly_method", "contains", text)
    for text in config.sequencing_technology_contains:
        add("Sequencing_technology", "contains", text)
    # Assembly level is one criterion whose argument is a set of allowed values;
    # membership within this one option is OR, while this criterion is ANDed with
    # every other active criterion. This is the conventional meaning of
    # "--assembly-level complete,chromosome" and avoids an impossible
    # Complete AND Chromosome requirement.
    if config.assembly_level:
        add("Assembly_level", "equals_any", ";".join(config.assembly_level), notes="Allowed assembly-level set")
    return rules


def entity_value(entity: Any, spec: FieldSpec) -> Any:
    if dataclasses.is_dataclass(entity):
        value = getattr(entity, spec.getter_key, None)
    elif isinstance(entity, Mapping):
        value = entity.get(spec.getter_key)
    else:
        value = getattr(entity, spec.getter_key, None)
    if spec.getter_key == "seed_taxa" and isinstance(value, list):
        return ";".join(value)
    return value


def evaluate_filter(entity: Any, rule: FilterRule) -> tuple[bool, str, str]:
    spec = FIELD_SPECS[rule.field]
    observed = normalize_missing(entity_value(entity, spec))
    if rule.operator == "present":
        return observed is not None, value_tsv(observed), "PASS" if observed is not None else "MISSING_VALUE"
    if rule.operator == "absent":
        return observed is None, value_tsv(observed), "PASS" if observed is None else "VALUE_PRESENT"
    if observed is None:
        return False, ".", "MISSING_VALUE"

    if rule.operator == "equals_any":
        allowed = {x for x in rule.value.split(";") if x}
        ok = str(observed) in allowed
        return ok, str(observed), "PASS" if ok else "NOT_IN_ALLOWED_SET"

    if rule.operator in NUMERIC_OPERATORS:
        try:
            obs = float(observed)
        except (TypeError, ValueError):
            return False, str(observed), "NON_NUMERIC_VALUE"
        req = float(rule.value)
        ok = obs >= req if rule.operator == "min" else obs <= req
        return ok, str(observed), "PASS" if ok else ("BELOW_THRESHOLD" if rule.operator == "min" else "ABOVE_THRESHOLD")

    if rule.operator in DATE_OPERATORS:
        try:
            obs_date = dt.date.fromisoformat(str(observed)[:10])
        except ValueError:
            return False, str(observed), "INVALID_DATE_VALUE"
        req_date = dt.date.fromisoformat(rule.value)
        # Date semantics are inclusive, matching NCBI's documented
        # released-after/on and released-before/on behavior.
        ok = obs_date >= req_date if rule.operator == "after" else obs_date <= req_date
        return ok, str(observed), "PASS" if ok else ("BEFORE_MIN_DATE" if rule.operator == "after" else "AFTER_MAX_DATE")

    left = str(observed)
    right = str(rule.value)
    if not rule.case_sensitive:
        left_cmp = left.casefold()
        right_cmp = right.casefold()
    else:
        left_cmp, right_cmp = left, right
    if rule.operator == "contains":
        ok = right_cmp in left_cmp
    elif rule.operator == "not_contains":
        ok = right_cmp not in left_cmp
    elif rule.operator == "equals":
        ok = left_cmp == right_cmp
    elif rule.operator == "not_equals":
        ok = left_cmp != right_cmp
    elif rule.operator == "starts_with":
        ok = left_cmp.startswith(right_cmp)
    elif rule.operator == "ends_with":
        ok = left_cmp.endswith(right_cmp)
    else:
        raise ConfigError(f"Internal unsupported filter operator: {rule.operator}")
    return ok, left, "PASS" if ok else "TEXT_MISMATCH"


def write_resolved_filters(path: Path, rules: Sequence[FilterRule]) -> None:
    fields = ["Filter_ID", "Level", "Field", "Data_type", "Operator", "Value", "Case_sensitive", "Source", "Notes"]
    rows = []
    for r in rules:
        spec = FIELD_SPECS[r.field]
        rows.append({
            "Filter_ID": r.filter_id, "Level": r.level, "Field": r.field, "Data_type": spec.data_type,
            "Operator": r.operator, "Value": r.value, "Case_sensitive": r.case_sensitive if r.operator in TEXT_OPERATORS else ".",
            "Source": r.source, "Notes": r.notes,
        })
    write_tsv(path, fields, rows)


########################################################################################################
# Assembly inventory, normalization and filtering
########################################################################################################

@dataclass
class AssemblyRecord:
    accession: str
    species_taxid: int
    species: str
    source: str | None = None
    paired_accession: str | None = None
    multi_isolate: bool | None = None
    assembly_status: str | None = None
    atypical: bool | None = None
    atypical_warnings: str | None = None
    assembly_level: str | None = None
    refseq_category: str | None = None
    strain: str | None = None
    isolate: str | None = None
    infraspecific_name: str | None = None
    bioproject: str | None = None
    biosample: str | None = None
    assembly_method: str | None = None
    sequencing_technology: str | None = None
    contig_n50: int | None = None
    contig_l50: int | None = None
    num_contigs: int | None = None
    scaffold_n50: int | None = None
    scaffold_l50: int | None = None
    num_scaffolds: int | None = None
    num_scaffold_gaps: int | None = None
    genome_size: int | None = None
    gc_percent: float | None = None
    protein_coding_genes: int | None = None
    busco_complete: float | None = None
    busco_fragmented: float | None = None
    busco_missing: float | None = None
    busco_duplicated: float | None = None
    busco_lineage: str | None = None
    busco_version: str | None = None
    release_date: str | None = None
    annotation_name: str | None = None
    annotation_release_date: str | None = None
    annotation_status: str | None = None
    protein_fasta_available: bool | None = None
    protein_count: int | None = None
    protein_fasta_sha256: str | None = None
    ncbi_package_md5: str | None = None
    fingerprint: str = ""
    source_scope_status: str = "PENDING"
    qc_status: str = "PENDING"
    selected: bool = False
    final_status: str = "PENDING"
    final_reason: str = "."
    update_change: str | None = None
    update_reason: str | None = None
    raw_record: dict[str, Any] = field(default_factory=dict, repr=False)


def as_int_or_none(value: Any) -> int | None:
    value = normalize_missing(value)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float_or_none(value: Any) -> float | None:
    value = normalize_missing(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def busco_percent(value: Any) -> float | None:
    """NCBI BUSCO report currently stores fractions (e.g. 0.9887).

    FungiFunk's public CLI is deliberately percentage-based (e.g. 90 means 90%).
    We normalize values <=1 to percent while tolerating a future/report variant
    that already supplies 0-100 values.
    """
    x = as_float_or_none(value)
    if x is None:
        return None
    return x * 100.0 if 0.0 <= x <= 1.0 else x


def source_name(raw: Any, accession: str) -> str:
    text = str(raw or "").upper()
    if "REFSEQ" in text or accession.startswith("GCF_"):
        return "GCF"
    if "GENBANK" in text or accession.startswith("GCA_"):
        return "GCA"
    return str(raw or ".")


def extract_biosample_fields(
    assembly_info: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (biosample_accession, strain, isolate, infraspecific_name)."""

    biosample = assembly_info.get("biosample")

    if not isinstance(biosample, Mapping):
        return None, None, None, None

    accession = normalize_missing(biosample.get("accession"))

    # NCBI may expose strain/isolate directly in the BioSample object or in
    # its description, depending on the record representation. Support both.
    description = (
        biosample.get("description")
        if isinstance(biosample.get("description"), Mapping)
        else {}
    )

    strain = normalize_missing(
        first_nonmissing(
            description.get("strain"),
            biosample.get("strain"),
        )
    )

    isolate = normalize_missing(
        first_nonmissing(
            description.get("isolate"),
            biosample.get("isolate"),
        )
    )

    # Support both snake_case and camelCase representations for optional
    # below-species descriptors.
    infra_candidates = [
        strain,
        isolate,
        first_nonmissing(
            description.get("sub_species"),
            description.get("subSpecies"),
        ),
        description.get("cultivar"),
        description.get("ecotype"),
    ]

    infra = normalize_missing(first_nonmissing(*infra_candidates))

    return (
        str(accession) if accession is not None else None,
        str(strain) if strain is not None else None,
        str(isolate) if isolate is not None else None,
        str(infra) if infra is not None else None,
    )


def normalize_assembly_record(
    record: Mapping[str, Any],
    species: SpeciesCandidate,
    multi_isolate: bool,
) -> AssemblyRecord:

    accession = str(
        first_nonmissing(
            record.get("accession"),
            record.get("current_accession"),
            record.get("currentAccession"),
        )
        or ""
    ).strip()

    if not accession:
        raise ExternalToolError(
            f"NCBI genome record for {species.species} is missing assembly accession."
        )

    # NCBI Datasets JSON output may use snake_case or camelCase depending on
    # the representation/version. Support both explicitly rather than
    # silently assuming one schema.
    info_raw = first_nonmissing(
        record.get("assembly_info"),
        record.get("assemblyInfo"),
    )
    info_obj = info_raw if isinstance(info_raw, Mapping) else {}

    stats_raw = first_nonmissing(
        record.get("assembly_stats"),
        record.get("assemblyStats"),
    )
    stats = stats_raw if isinstance(stats_raw, Mapping) else {}

    annotation_raw = first_nonmissing(
        record.get("annotation_info"),
        record.get("annotationInfo"),
    )
    annotation = annotation_raw if isinstance(annotation_raw, Mapping) else {}

    atypical_raw = info_obj.get("atypical")
    atypical_obj = atypical_raw if isinstance(atypical_raw, Mapping) else {}

    paired_raw = first_nonmissing(
        info_obj.get("paired_assembly"),
        info_obj.get("pairedAssembly"),
    )
    paired_obj = paired_raw if isinstance(paired_raw, Mapping) else {}

    biosample_acc, strain_bio, isolate_bio, infra_bio = (
        extract_biosample_fields(info_obj)
    )

    organism = (
        record.get("organism")
        if isinstance(record.get("organism"), Mapping)
        else {}
    )

    infra_raw = first_nonmissing(
        organism.get("infraspecific_names"),
        organism.get("infraspecificNames"),
    )
    infra_obj = infra_raw if isinstance(infra_raw, Mapping) else {}

    strain = normalize_missing(
        first_nonmissing(
            infra_obj.get("strain"),
            strain_bio,
        )
    )

    isolate = normalize_missing(
        first_nonmissing(
            infra_obj.get("isolate"),
            isolate_bio,
        )
    )

    infra = normalize_missing(
        first_nonmissing(
            strain,
            isolate,
            infra_bio,
            infra_obj.get("cultivar"),
            infra_obj.get("ecotype"),
        )
    )

    annotation_stats = (
        annotation.get("stats")
        if isinstance(annotation.get("stats"), Mapping)
        else {}
    )

    gene_counts_raw = first_nonmissing(
        annotation_stats.get("gene_counts"),
        annotation_stats.get("geneCounts"),
    )
    gene_counts = (
        gene_counts_raw
        if isinstance(gene_counts_raw, Mapping)
        else {}
    )

    busco = (
        annotation.get("busco")
        if isinstance(annotation.get("busco"), Mapping)
        else {}
    )

    paired = first_nonmissing(
        record.get("paired_accession"),
        record.get("pairedAccession"),
        paired_obj.get("accession"),
    )

    warnings_raw = atypical_obj.get("warnings") or []

    if isinstance(warnings_raw, list):
        atypical_warnings = ";".join(str(x) for x in warnings_raw) or None
    else:
        atypical_warnings = normalize_missing(warnings_raw)

    bioproject = first_nonmissing(
        info_obj.get("bioproject_accession"),
        info_obj.get("bioprojectAccession"),
    )

    if bioproject is None:
        lineage = first_nonmissing(
            info_obj.get("bioproject_lineage"),
            info_obj.get("bioprojectLineage"),
        ) or []

        if isinstance(lineage, list):
            for level in lineage:
                if not isinstance(level, Mapping):
                    continue

                projects = level.get("bioprojects") or []

                if isinstance(projects, list) and projects:
                    first = projects[0]

                    if isinstance(first, Mapping) and first.get("accession"):
                        bioproject = first["accession"]
                        break

    source = source_name(
        first_nonmissing(
            record.get("source_database"),
            record.get("sourceDatabase"),
        ),
        accession,
    )

    normalized = AssemblyRecord(
        accession=accession,
        species_taxid=species.taxid,
        species=species.species,
        source=source,
        paired_accession=str(paired) if paired is not None else None,
        multi_isolate=multi_isolate,

        assembly_status=normalize_assembly_status(
            first_nonmissing(
                info_obj.get("assembly_status"),
                info_obj.get("assemblyStatus"),
            )
        ),

        atypical=bool(
            first_nonmissing(
                atypical_obj.get("is_atypical"),
                atypical_obj.get("isAtypical"),
                False,
            )
        ),

        atypical_warnings=(
            str(atypical_warnings)
            if atypical_warnings is not None
            else None
        ),

        assembly_level=normalize_missing(
            first_nonmissing(
                info_obj.get("assembly_level"),
                info_obj.get("assemblyLevel"),
            )
        ),

        refseq_category=normalize_missing(
            first_nonmissing(
                info_obj.get("refseq_category"),
                info_obj.get("refseqCategory"),
            )
        ),

        strain=str(strain) if strain is not None else None,
        isolate=str(isolate) if isolate is not None else None,
        infraspecific_name=str(infra) if infra is not None else None,

        bioproject=(
            str(bioproject)
            if bioproject is not None
            else None
        ),

        biosample=biosample_acc,

        assembly_method=normalize_missing(
            first_nonmissing(
                info_obj.get("assembly_method"),
                info_obj.get("assemblyMethod"),
            )
        ),

        sequencing_technology=normalize_missing(
            first_nonmissing(
                info_obj.get("sequencing_tech"),
                info_obj.get("sequencingTech"),
            )
        ),

        contig_n50=as_int_or_none(
            first_nonmissing(
                stats.get("contig_n50"),
                stats.get("contigN50"),
            )
        ),

        contig_l50=as_int_or_none(
            first_nonmissing(
                stats.get("contig_l50"),
                stats.get("contigL50"),
            )
        ),

        num_contigs=as_int_or_none(
            first_nonmissing(
                stats.get("number_of_contigs"),
                stats.get("numberOfContigs"),
            )
        ),

        scaffold_n50=as_int_or_none(
            first_nonmissing(
                stats.get("scaffold_n50"),
                stats.get("scaffoldN50"),
            )
        ),

        scaffold_l50=as_int_or_none(
            first_nonmissing(
                stats.get("scaffold_l50"),
                stats.get("scaffoldL50"),
            )
        ),

        num_scaffolds=as_int_or_none(
            first_nonmissing(
                stats.get("number_of_scaffolds"),
                stats.get("numberOfScaffolds"),
            )
        ),

        num_scaffold_gaps=as_int_or_none(
            first_nonmissing(
                stats.get("gaps_between_scaffolds_count"),
                stats.get("gapsBetweenScaffoldsCount"),
            )
        ),

        genome_size=as_int_or_none(
            first_nonmissing(
                stats.get("total_sequence_length"),
                stats.get("totalSequenceLength"),
            )
        ),

        gc_percent=as_float_or_none(
            first_nonmissing(
                stats.get("gc_percent"),
                stats.get("gcPercent"),
            )
        ),

        protein_coding_genes=as_int_or_none(
            first_nonmissing(
                gene_counts.get("protein_coding"),
                gene_counts.get("proteinCoding"),
            )
        ),

        busco_complete=busco_percent(busco.get("complete")),
        busco_fragmented=busco_percent(busco.get("fragmented")),
        busco_missing=busco_percent(busco.get("missing")),
        busco_duplicated=busco_percent(busco.get("duplicated")),

        busco_lineage=normalize_missing(
            first_nonmissing(
                busco.get("busco_lineage"),
                busco.get("buscoLineage"),
            )
        ),

        busco_version=normalize_missing(
            first_nonmissing(
                busco.get("busco_ver"),
                busco.get("buscoVer"),
            )
        ),

        release_date=normalize_missing(
            first_nonmissing(
                info_obj.get("release_date"),
                info_obj.get("releaseDate"),
            )
        ),

        annotation_name=normalize_missing(annotation.get("name")),

        annotation_release_date=normalize_missing(
            first_nonmissing(
                annotation.get("release_date"),
                annotation.get("releaseDate"),
            )
        ),

        annotation_status=normalize_missing(annotation.get("status")),

        raw_record=dict(record),
    )

    # Fingerprint includes fields whose change may alter eligibility, annotation
    # content, or downloaded protein data. We intentionally do not hash the raw
    # JSON wholesale because NCBI may add unrelated fields without changing the
    # biological assembly/protein resource.
    normalized.fingerprint = stable_json_hash(
        {
            "accession": normalized.accession,
            "paired_accession": normalized.paired_accession,
            "source": normalized.source,
            "assembly_status": normalized.assembly_status,
            "atypical": normalized.atypical,
            "assembly_level": normalized.assembly_level,
            "refseq_category": normalized.refseq_category,
            "strain": normalized.strain,
            "isolate": normalized.isolate,
            "bioproject": normalized.bioproject,
            "biosample": normalized.biosample,
            "assembly_method": normalized.assembly_method,
            "sequencing_technology": normalized.sequencing_technology,

            "stats": {
                "contig_n50": normalized.contig_n50,
                "contig_l50": normalized.contig_l50,
                "num_contigs": normalized.num_contigs,
                "scaffold_n50": normalized.scaffold_n50,
                "scaffold_l50": normalized.scaffold_l50,
                "num_scaffolds": normalized.num_scaffolds,
                "num_scaffold_gaps": normalized.num_scaffold_gaps,
                "genome_size": normalized.genome_size,
                "gc_percent": normalized.gc_percent,
            },

            "annotation": {
                "name": normalized.annotation_name,
                "release_date": normalized.annotation_release_date,
                "status": normalized.annotation_status,
                "protein_coding_genes": normalized.protein_coding_genes,
                "busco_complete": normalized.busco_complete,
                "busco_fragmented": normalized.busco_fragmented,
                "busco_missing": normalized.busco_missing,
                "busco_duplicated": normalized.busco_duplicated,
            },
        }
    )

    return normalized


def inventory_species_assemblies(
    client: NCBIClient,
    species: SpeciesCandidate,
) -> list[AssemblyRecord]:
    """Inventory all assemblies and infer multi-isolate membership without text heuristics.

    NCBI Datasets provides --exclude-multi-isolate but the genome report does not
    expose one simple boolean field. We therefore query the full exact-species
    set and the exact-species set with --exclude-multi-isolate; accessions present
    only in the first set are deterministically marked Multi_isolate=TRUE.

    Multi-isolate status is retained as descriptive metadata only. It does not
    determine source-scope eligibility or biological selection.
    """

    def record_accession(record: Mapping[str, Any]) -> str:
        """Return a normalized accession across supported Datasets schemas."""

        return str(
            first_nonmissing(
                record.get("accession"),
                record.get("current_accession"),
                record.get("currentAccession"),
            )
            or ""
        ).strip()

    all_records = client.genomes_for_species(
        species.taxid,
        exclude_multi_isolate=False,
    )

    non_multi_records = client.genomes_for_species(
        species.taxid,
        exclude_multi_isolate=True,
    )

    non_multi_accessions = {
        accession
        for record in non_multi_records
        if (accession := record_accession(record))
    }

    result: list[AssemblyRecord] = []
    seen: set[str] = set()

    for raw in all_records:
        accession = record_accession(raw)

        if not accession or accession in seen:
            continue

        seen.add(accession)

        result.append(
            normalize_assembly_record(
                raw,
                species,
                accession not in non_multi_accessions,
            )
        )

    return result


########################################################################################################
# Explicit target NCBI resolution
########################################################################################################

@dataclass(frozen=True)
class ResolvedExplicitTarget:
    rule: ExplicitTargetRule
    species_taxid: int
    species: str
    strain: str | None
    strain_key: str | None
    assembly_accession: str | None

    assembly: AssemblyRecord | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    species_record: dict[str, Any] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


def resolved_explicit_target_identity_key(
    target: ResolvedExplicitTarget,
) -> tuple[str, ...]:
    """Return the canonical biological identity of a resolved explicit target."""

    if target.rule.target_type == "assembly":

        assert target.assembly_accession is not None

        return (
            "assembly",
            target.assembly_accession.upper(),
        )

    assert target.strain_key is not None

    return (
        "strain",
        str(target.species_taxid),
        target.strain_key,
    )


def resolve_explicit_species_reference(
    client: NCBIClient,
    target: ExplicitTargetRule,
) -> tuple[dict[str, Any], int, str]:
    """Resolve a Species/Taxid target reference and require species rank."""

    record, taxid = resolve_taxon_query(
        client,
        target.species or ".",
        target.input_taxid or ".",
    )

    rank = taxonomy_rank(record)
    name = taxonomy_name(record)

    if rank != "species":
        raise ConfigError(
            f"{target.input_path}:{target.input_line}: explicit "
            f"{target.target_type} target species identity must resolve to "
            f"NCBI rank 'species'; resolved to {name!r} ({rank or 'unknown rank'}, "
            f"TaxID {taxid})."
        )

    if not name:
        raise ExternalToolError(
            f"NCBI species record for TaxID {taxid} has no scientific name."
        )

    return record, taxid, name


def resolve_canonical_species_for_taxid(
    client: NCBIClient,
    taxid: int,
    *,
    context: str,
) -> tuple[dict[str, Any], int, str]:
    """Resolve any organism TaxID to exactly one canonical species ancestor.

    Assembly reports may occasionally reference below-species or historical
    TaxIDs. Use NCBI taxonomy lineage/classification rather than assuming that
    the assembly organism TaxID itself is the species TaxID.
    """

    records = client.taxonomy(
        taxid,
        parents=True,
    )

    if not records:
        raise ExternalToolError(
            f"NCBI taxonomy returned no records while resolving species for "
            f"{context} from organism TaxID {taxid}."
        )

    candidate_taxids: set[int] = set()
    species_records: dict[int, dict[str, Any]] = {}

    for record in records:

        record_taxid = taxonomy_taxid(record)

        if (
            record_taxid is not None
            and taxonomy_rank(record) == "species"
        ):
            candidate_taxids.add(record_taxid)
            species_records[record_taxid] = record

        _, classified_species_taxid = classification_value(
            record,
            "species",
        )

        if classified_species_taxid is not None:
            candidate_taxids.add(
                classified_species_taxid
            )

    if len(candidate_taxids) != 1:
        rendered = (
            ", ".join(
                str(value)
                for value in sorted(candidate_taxids)
            )
            or "none"
        )

        raise ExternalToolError(
            f"Could not resolve exactly one canonical species for {context} "
            f"from organism TaxID {taxid}; species candidates: {rendered}."
        )

    species_taxid = next(iter(candidate_taxids))

    species_record = species_records.get(
        species_taxid
    )

    if species_record is None:

        exact_records = client.taxonomy(
            species_taxid,
            parents=True,
        )

        exact_species = [
            record
            for record in exact_records
            if (
                taxonomy_taxid(record) == species_taxid
                and taxonomy_rank(record) == "species"
            )
        ]

        if len(exact_species) != 1:
            raise ExternalToolError(
                f"NCBI taxonomy did not return exactly one species record for "
                f"canonical TaxID {species_taxid} while resolving {context}."
            )

        species_record = exact_species[0]

    species_name = taxonomy_name(
        species_record
    )

    if not species_name:
        raise ExternalToolError(
            f"NCBI canonical species TaxID {species_taxid} has no scientific "
            f"name while resolving {context}."
        )

    return (
        species_record,
        species_taxid,
        species_name,
    )


def explicit_assembly_organism_taxid(
    record: Mapping[str, Any],
    *,
    accession: str,
) -> int:
    """Return the organism TaxID carried by one NCBI genome report."""

    organism = (
        record.get("organism")
        if isinstance(record.get("organism"), Mapping)
        else {}
    )

    raw = first_nonmissing(
        organism.get("tax_id"),
        organism.get("taxId"),
        organism.get("taxid"),
        record.get("tax_id"),
        record.get("taxId"),
        record.get("taxid"),
    )

    try:
        taxid = int(raw)

    except (TypeError, ValueError) as exc:
        raise ExternalToolError(
            f"NCBI genome record for explicit assembly {accession} does not "
            "contain a valid organism TaxID."
        ) from exc

    if taxid <= 0:
        raise ExternalToolError(
            f"NCBI genome record for explicit assembly {accession} contains "
            f"invalid organism TaxID {taxid}."
        )

    return taxid


def resolve_explicit_targets(
    client: NCBIClient,
    targets: Sequence[ExplicitTargetRule],
) -> list[ResolvedExplicitTarget]:
    """Resolve explicit strain/assembly targets against current NCBI metadata."""

    resolved: list[ResolvedExplicitTarget] = []

    species_reference_cache: dict[
        tuple[str | None, int | None],
        tuple[dict[str, Any], int, str],
    ] = {}

    canonical_species_cache: dict[
        int,
        tuple[dict[str, Any], int, str],
    ] = {}

    assembly_cache: dict[
        str,
        tuple[dict[str, Any], bool],
    ] = {}

    def target_species_reference(
        target: ExplicitTargetRule,
    ) -> tuple[dict[str, Any], int, str]:

        key = (
            normalize_explicit_species_identity(
                target.species
            ),
            target.taxid,
        )

        cached = species_reference_cache.get(
            key
        )

        if cached is not None:
            return cached

        value = resolve_explicit_species_reference(
            client,
            target,
        )

        species_reference_cache[key] = value
        return value

    for target in targets:

        if target.target_type == "strain":

            species_record, species_taxid, species_name = (
                target_species_reference(
                    target
                )
            )

            assert target.strain is not None
            assert target.strain_key is not None

            resolved.append(
                ResolvedExplicitTarget(
                    rule=target,
                    species_taxid=species_taxid,
                    species=species_name,
                    strain=target.strain,
                    strain_key=target.strain_key,
                    assembly_accession=None,
                    assembly=None,
                    species_record=dict(species_record),
                )
            )

            continue

        assert target.target_type == "assembly"
        assert target.assembly_accession is not None

        accession = (
            target.assembly_accession.upper()
        )

        cached_assembly = assembly_cache.get(
            accession
        )

        if cached_assembly is None:

            raw_record = client.genome_by_accession(
                accession,
                exclude_multi_isolate=False,
            )

            if raw_record is None:
                raise ConfigError(
                    f"{target.input_path}:{target.input_line}: explicit assembly "
                    f"target {accession!r} was not returned by NCBI as that exact "
                    "accession.version."
                )

            non_multi_record = client.genome_by_accession(
                accession,
                exclude_multi_isolate=True,
            )

            multi_isolate = (
                non_multi_record is None
            )

            cached_assembly = (
                raw_record,
                multi_isolate,
            )

            assembly_cache[
                accession
            ] = cached_assembly

        raw_record, multi_isolate = (
            cached_assembly
        )

        organism_taxid = (
            explicit_assembly_organism_taxid(
                raw_record,
                accession=accession,
            )
        )

        canonical_species = (
            canonical_species_cache.get(
                organism_taxid
            )
        )

        if canonical_species is None:

            canonical_species = (
                resolve_canonical_species_for_taxid(
                    client,
                    organism_taxid,
                    context=(
                        f"explicit assembly {accession}"
                    ),
                )
            )

            canonical_species_cache[
                organism_taxid
            ] = canonical_species

        (
            species_record,
            species_taxid,
            species_name,
        ) = canonical_species

        species_candidate = SpeciesCandidate(
            taxid=species_taxid,
            species=species_name,
        )

        assembly = normalize_assembly_record(
            raw_record,
            species_candidate,
            multi_isolate,
        )

        if assembly.accession.upper() != accession:
            raise ExternalToolError(
                f"NCBI exact assembly lookup requested {accession}, but "
                f"normalization produced {assembly.accession!r}."
            )

        if (
            target.species is not None
            or target.taxid is not None
        ):

            (
                _,
                hinted_species_taxid,
                hinted_species_name,
            ) = target_species_reference(
                target
            )

            if hinted_species_taxid != species_taxid:
                raise ConfigError(
                    f"{target.input_path}:{target.input_line}: explicit assembly "
                    f"target {accession} belongs to NCBI species "
                    f"{species_name!r} (TaxID {species_taxid}), but the supplied "
                    f"Species/Taxid resolves to {hinted_species_name!r} "
                    f"(TaxID {hinted_species_taxid})."
                )

        observed_strain_key = (
            normalize_selection_label(
                assembly.strain
            )
            if assembly.strain is not None
            else None
        )

        if target.strain_key is not None:

            if observed_strain_key is None:
                raise ConfigError(
                    f"{target.input_path}:{target.input_line}: explicit assembly "
                    f"target {accession} specifies strain {target.strain!r}, but "
                    "the NCBI assembly record has no strain metadata."
                )

            if (
                observed_strain_key
                != target.strain_key
            ):
                raise ConfigError(
                    f"{target.input_path}:{target.input_line}: explicit assembly "
                    f"target {accession} specifies strain {target.strain!r}, but "
                    f"NCBI reports strain {assembly.strain!r}."
                )

        resolved.append(
            ResolvedExplicitTarget(
                rule=target,
                species_taxid=species_taxid,
                species=species_name,
                strain=assembly.strain,
                strain_key=observed_strain_key,
                assembly_accession=accession,
                assembly=assembly,
                species_record=dict(species_record),
            )
        )

    return resolved


def detect_resolved_explicit_target_conflicts(
    include_targets: Sequence[ResolvedExplicitTarget],
    exclude_targets: Sequence[ResolvedExplicitTarget],
) -> None:
    """Reject duplicates/conflicts using canonical NCBI-resolved identities."""

    def reject_resolved_duplicates(
        targets: Sequence[ResolvedExplicitTarget],
        *,
        action: str,
    ) -> None:

        seen: dict[
            tuple[str, ...],
            ResolvedExplicitTarget,
        ] = {}

        for target in targets:

            identity = (
                resolved_explicit_target_identity_key(
                    target
                )
            )

            previous = seen.get(
                identity
            )

            if previous is not None:
                raise ConfigError(
                    "Duplicate explicit target after NCBI resolution: "
                    f"{explicit_target_display(target.rule)!r} resolves to the "
                    f"same biological target as "
                    f"{previous.rule.input_path}:{previous.rule.input_line}.\n"
                    f"Current: {target.rule.input_path}:{target.rule.input_line}\n"
                    f"Action: {action}"
                )

            seen[identity] = target

    reject_resolved_duplicates(
        include_targets,
        action="include",
    )

    reject_resolved_duplicates(
        exclude_targets,
        action="exclude",
    )

    included_by_identity = {
        resolved_explicit_target_identity_key(
            target
        ): target
        for target in include_targets
    }

    for excluded in exclude_targets:

        identity = (
            resolved_explicit_target_identity_key(
                excluded
            )
        )

        included = included_by_identity.get(
            identity
        )

        if included is None:
            continue

        raise ConfigError(
            "Explicit target conflict after NCBI resolution: the same "
            "biological target is both included and excluded.\n"
            f"Target: {explicit_target_display(excluded.rule)}\n"
            f"Include: {included.rule.input_path}:{included.rule.input_line}\n"
            f"Exclude: {excluded.rule.input_path}:{excluded.rule.input_line}"
        )

    # A specific assembly cannot be explicitly included while its actual NCBI
    # strain is explicitly excluded. The reverse is not inherently a conflict:
    # an included strain may still be represented by another non-excluded
    # assembly.
    excluded_strains = {
        (
            target.species_taxid,
            target.strain_key,
        ): target
        for target in exclude_targets
        if (
            target.rule.target_type == "strain"
            and target.strain_key is not None
        )
    }

    for included in include_targets:

        if (
            included.rule.target_type
            != "assembly"
            or included.strain_key is None
        ):
            continue

        excluded_strain = (
            excluded_strains.get(
                (
                    included.species_taxid,
                    included.strain_key,
                )
            )
        )

        if excluded_strain is None:
            continue

        raise ConfigError(
            "Explicit target conflict after NCBI resolution: assembly "
            f"{included.assembly_accession} is explicitly included, but NCBI "
            f"assigns it to strain {included.strain!r} of "
            f"{included.species!r} (TaxID {included.species_taxid}), and that "
            "strain is explicitly excluded.\n"
            f"Include: {included.rule.input_path}:{included.rule.input_line}\n"
            f"Exclude: "
            f"{excluded_strain.rule.input_path}:{excluded_strain.rule.input_line}"
        )


def report_resolved_explicit_targets(
    include_targets: Sequence[ResolvedExplicitTarget],
    exclude_targets: Sequence[ResolvedExplicitTarget],
) -> None:
    """Report resolved explicit-target counts without changing selection state."""

    include_count = len(include_targets)
    exclude_count = len(exclude_targets)

    if not include_count and not exclude_count:
        return

    include_noun = (
        "target"
        if include_count == 1
        else "targets"
    )

    exclude_noun = (
        "target"
        if exclude_count == 1
        else "targets"
    )

    info(
        "Explicit targets resolved: "
        f"{include_count} inclusion {include_noun}, "
        f"{exclude_count} exclusion {exclude_noun}. "
        "Target species are incorporated into taxonomy curation."
    )


def resolved_explicit_target_to_jsonable(
    target: ResolvedExplicitTarget,
) -> dict[str, Any]:
    """Return one resolved explicit target as deterministic JSON data."""

    return {
        "rule": dict(vars(target.rule)),
        "species_taxid": target.species_taxid,
        "species": target.species,
        "strain": target.strain,
        "strain_key": target.strain_key,
        "assembly_accession": target.assembly_accession,
        "assembly": (
            dict(vars(target.assembly))
            if target.assembly is not None
            else None
        ),
        "species_record": dict(target.species_record),
    }


def save_resolved_explicit_targets(
    path: Path,
    targets: Sequence[ResolvedExplicitTarget],
) -> None:
    """Persist canonical target resolution for deterministic resume."""

    save_json_atomic(
        path,
        {
            "schema_version": (
                RESOLVED_EXPLICIT_TARGETS_SCHEMA_VERSION
            ),
            "targets": [
                resolved_explicit_target_to_jsonable(
                    target
                )
                for target in targets
            ],
        },
    )


def load_resolved_explicit_targets(
    path: Path,
    expected_rules: Sequence[ExplicitTargetRule],
) -> list[ResolvedExplicitTarget]:
    """Reload previously committed canonical explicit-target resolution."""

    expected = list(
        expected_rules
    )

    if not path.is_file():

        # Older/no-target builds do not need an empty derived file merely to
        # resume. Active explicit targets, however, must never be silently
        # re-resolved against a later NCBI state.
        if not expected:
            return []

        raise DataIntegrityError(
            "Committed taxonomy state uses explicit targets, but the "
            "persisted canonical target-resolution file is missing: "
            f"{path}"
        )

    try:
        payload = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:

        raise DataIntegrityError(
            "Could not read persisted canonical explicit-target resolution: "
            f"{path}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise DataIntegrityError(
            "Persisted explicit-target resolution is not a JSON object: "
            f"{path}"
        )

    schema_version = payload.get(
        "schema_version"
    )

    if (
        schema_version
        != RESOLVED_EXPLICIT_TARGETS_SCHEMA_VERSION
    ):
        raise DataIntegrityError(
            "Unsupported persisted explicit-target resolution schema: "
            f"{schema_version!r}"
        )

    raw_targets = payload.get(
        "targets"
    )

    if not isinstance(raw_targets, list):
        raise DataIntegrityError(
            "Persisted explicit-target resolution has no valid targets list."
        )

    resolved: list[
        ResolvedExplicitTarget
    ] = []

    for index, raw in enumerate(
        raw_targets,
        start=1,
    ):

        if not isinstance(raw, dict):
            raise DataIntegrityError(
                "Persisted explicit-target resolution contains a malformed "
                f"target at position {index}."
            )

        raw_rule = raw.get(
            "rule"
        )

        if not isinstance(raw_rule, dict):
            raise DataIntegrityError(
                "Persisted explicit-target resolution contains a malformed "
                f"rule at position {index}."
            )

        try:
            rule = ExplicitTargetRule(
                **raw_rule
            )

            raw_assembly = raw.get(
                "assembly"
            )

            assembly = (
                AssemblyRecord(
                    **raw_assembly
                )
                if isinstance(
                    raw_assembly,
                    dict,
                )
                else None
            )

            raw_species_record = raw.get(
                "species_record",
                {},
            )

            if not isinstance(
                raw_species_record,
                dict,
            ):
                raise TypeError(
                    "species_record is not an object"
                )

            target = ResolvedExplicitTarget(
                rule=rule,
                species_taxid=int(
                    raw["species_taxid"]
                ),
                species=str(
                    raw["species"]
                ),
                strain=raw.get(
                    "strain"
                ),
                strain_key=raw.get(
                    "strain_key"
                ),
                assembly_accession=raw.get(
                    "assembly_accession"
                ),
                assembly=assembly,
                species_record=dict(
                    raw_species_record
                ),
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ) as exc:

            raise DataIntegrityError(
                "Persisted explicit-target resolution contains invalid "
                f"target data at position {index}: {exc}"
            ) from exc

        if (
            target.rule.target_type
            == "assembly"
            and target.assembly is None
        ):
            raise DataIntegrityError(
                "Persisted resolved assembly target is missing its "
                f"AssemblyRecord: {target.assembly_accession}"
            )

        resolved.append(
            target
        )

    persisted_rules = [
        target.rule
        for target in resolved
    ]

    if persisted_rules != expected:
        raise DataIntegrityError(
            "Persisted canonical explicit-target resolution does not match "
            "the current immutable explicit-target snapshots."
        )

    return resolved


def merge_explicit_assembly_targets_into_inventory(
    records: Sequence[AssemblyRecord],
    species_taxid: int,
    explicit_include_targets: Sequence[ResolvedExplicitTarget],
) -> tuple[list[AssemblyRecord], set[str]]:
    """Inject exact explicit assembly targets missing from normal discovery.

    Returns the merged inventory and the exact accessions that are mandatory
    explicit assembly targets for this species.

    Existing normally discovered records are retained rather than duplicated.
    """

    merged = list(records)

    by_accession = {
        assembly.accession: assembly
        for assembly in merged
    }

    required_accessions: set[str] = set()

    for target in explicit_include_targets:

        if target.rule.target_type != "assembly":
            continue

        if target.species_taxid != species_taxid:
            continue

        accession = target.assembly_accession

        if accession is None or target.assembly is None:
            raise DataIntegrityError(
                "Resolved explicit assembly target is missing its normalized "
                f"assembly record for species TaxID {species_taxid}."
            )

        if target.assembly.species_taxid != species_taxid:
            raise DataIntegrityError(
                f"Explicit assembly {accession} resolved to species TaxID "
                f"{target.assembly.species_taxid}, but is being injected into "
                f"species TaxID {species_taxid}."
            )

        required_accessions.add(
            accession
        )

        if accession in by_accession:
            continue

        merged.append(
            target.assembly
        )

        by_accession[
            accession
        ] = target.assembly

    return (
        merged,
        required_accessions,
    )


def assembly_source_scope(
    assembly: AssemblyRecord,
    inventory_by_accession: Mapping[str, AssemblyRecord],
    config: BuildConfig,
    *,
    explicit_include_accessions: set[str] | None = None,
) -> tuple[bool, str]:
    """Determine whether an assembly belongs to the requested source scope.

    Multi-isolate project status is descriptive metadata only and does not
    affect eligibility.

    Default source scope contains GCF/RefSeq assemblies. When --include-gca is
    enabled, eligible unpaired GCA/GenBank assemblies are also admitted.
    """

    explicit_accessions = (
        explicit_include_accessions
        if explicit_include_accessions is not None
        else set()
    )

    if assembly.source == "GCF":
        return True, "IN_SCOPE"

    if assembly.source == "GCA":

        # An exact explicit GCA target is a narrow exception to the automatic
        # source-scope policy. This does not enable any other GCA assembly for
        # the species and does not alter the global --include-gca setting.
        #
        # Likewise, an explicitly requested exact GCA is not substituted by its
        # paired GCF.
        if assembly.accession in explicit_accessions:
            return True, "EXPLICIT_TARGET_IN_SCOPE"

        if not config.include_gca:
            return False, "GCA_NOT_ENABLED"

        paired = assembly.paired_accession

        if (
            paired
            and paired.startswith("GCF_")
            and paired in inventory_by_accession
        ):
            return False, "PAIRED_WITH_GCF"

        return True, "IN_SCOPE"

    return False, "UNSUPPORTED_ASSEMBLY_SOURCE"


def hard_assembly_qc(
    assembly: AssemblyRecord,
    config: BuildConfig,
) -> tuple[bool, str]:
    """Apply non-configurable assembly-status QC and optional atypical QC."""

    status = (assembly.assembly_status or "").strip().lower()

    # Do not use obsolete or unavailable assembly versions when constructing
    # a new reference. In the current NCBI Datasets schema, "previous" is the
    # status used for an assembly version that has been replaced by a newer
    # one. "Retired" is deprecated by NCBI but is retained here defensively
    # for older records. "Withdrawn" is also retained for compatibility with
    # historical/general NCBI status terminology.
    if status in {
        "previous",
        "suppressed",
        "retired",
        "withdrawn",
    }:
        return False, f"ASSEMBLY_STATUS_{status.upper()}"

    if assembly.atypical and not config.include_atypical:
        return False, "ATYPICAL"

    return True, "PASS"


def apply_filters_to_entity(entity: Any, rules: Sequence[FilterRule]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for rule in rules:
        ok, observed, reason = evaluate_filter(entity, rule)
        if not ok:
            failures.append({
                "Filter_ID": rule.filter_id,
                "Field": rule.field,
                "Operator": rule.operator,
                "Required": rule.value,
                "Observed": observed,
                "Failure_reason": reason,
            })
    return failures


def quality_rank_key(a: AssemblyRecord) -> tuple[Any, ...]:
    """Deterministic assembly-quality ranking used by all selection modes."""
    refseq_rank = {"reference genome": 0, "representative genome": 1}
    category = refseq_rank.get((a.refseq_category or "").strip().lower(), 2)
    source = 0 if a.source == "GCF" else 1
    level = {"complete genome": 0, "chromosome": 1, "scaffold": 2, "contig": 3}.get((a.assembly_level or "").strip().lower(), 4)

    # Quality metrics are secondary and missing values sort last. Prefer higher
    # N50/BUSCO and fewer contigs. This ranking does NOT invent hard QC cutoffs.
    def high(value: float | int | None) -> tuple[int, float]:
        return (1, 0.0) if value is None else (0, -float(value))

    def low(value: float | int | None) -> tuple[int, float]:
        return (1, 0.0) if value is None else (0, float(value))

    return (
        category, source, level,
        *high(a.busco_complete), *high(a.contig_n50), *high(a.scaffold_n50),
        *low(a.num_contigs), *low(a.num_scaffolds),
        a.accession,
    )


_SELECTION_SEPARATOR_RE = re.compile(r"[\s_-]+")


def normalize_selection_label(value: str | None) -> str | None:
    """Return a deterministic grouping key for strain/isolate labels.

    Grouping normalization is deliberately conservative:

      - Unicode compatibility normalization
      - case-insensitive comparison
      - trim surrounding whitespace
      - ignore whitespace, '-' and '_' separators

    Original NCBI values are never modified in AssemblyRecord, SQLite,
    provenance or user-facing metadata.
    """

    if value is None:
        return None

    text = unicodedata.normalize(
        "NFKC",
        str(value),
    ).strip().casefold()

    if not text:
        return None

    text = _SELECTION_SEPARATOR_RE.sub("", text)

    return text or None


def strain_selection_key(
    assembly: AssemblyRecord,
) -> str | None:
    return normalize_selection_label(
        assembly.strain
    )


def isolate_selection_key(
    assembly: AssemblyRecord,
) -> tuple[str | None, str] | None:
    isolate = normalize_selection_label(
        assembly.isolate
    )

    if isolate is None:
        return None

    return (
        strain_selection_key(assembly),
        isolate,
    )


def unassigned_selection_key(
    assembly: AssemblyRecord,
) -> tuple[str, str]:
    """Group strain/isolate-unassigned records by BioSample when possible."""

    if assembly.biosample:
        biosample = unicodedata.normalize(
            "NFKC",
            str(assembly.biosample),
        ).strip().casefold()

        if biosample:
            return (
                "BIOSAMPLE",
                biosample,
            )

    # Without strain, isolate or BioSample metadata, accession is the only
    # safe identity available. Do not merge such assemblies arbitrarily.
    return (
        "ASSEMBLY",
        assembly.accession.casefold(),
    )


@dataclass(frozen=True)
class AssemblySelectionPolicy:
    mode: str

    max_assemblies_per_species: int | None

    max_assemblies_per_strain: int | None

    max_strains_per_species: int | None

    include_isolates: bool

    include_unassigned_assemblies: bool


def assembly_selection_policy(
    config: BuildConfig,
) -> AssemblySelectionPolicy:
    """Resolve BuildConfig into one internally consistent selection policy."""

    if config.max_assemblies_per_species is not None:

        return AssemblySelectionPolicy(
            mode="SPECIES",
            max_assemblies_per_species=(
                config.max_assemblies_per_species
            ),
            max_assemblies_per_strain=None,
            max_strains_per_species=None,
            include_isolates=False,
            include_unassigned_assemblies=False,
        )

    if config.max_assemblies_per_strain is None:
        raise DataIntegrityError(
            "Strain-level assembly selection requires "
            "max_assemblies_per_strain to be defined."
        )

    return AssemblySelectionPolicy(
        mode="STRAIN",
        max_assemblies_per_species=None,
        max_assemblies_per_strain=(
            config.max_assemblies_per_strain
        ),
        max_strains_per_species=(
            config.max_strains_per_species
        ),
        include_isolates=(
            config.include_isolates
        ),
        include_unassigned_assemblies=(
            config.include_unassigned_assemblies
        ),
    )


EXPLICIT_SELECTION_SCOPE_EXCLUSION_REASONS = frozenset({
    "EXPLICIT_ASSEMBLY_EXCLUDED",
    "EXPLICIT_STRAIN_EXCLUDED",
    "OUTSIDE_EXPLICIT_TARGET_SCOPE",
})


@dataclass(frozen=True)
class AssemblySelectionConstraints:
    """Explicit biological constraints for one species selection."""

    include_accessions: frozenset[str] = field(
        default_factory=frozenset
    )

    include_strains: frozenset[str] = field(
        default_factory=frozenset
    )

    exclude_accessions: frozenset[str] = field(
        default_factory=frozenset
    )

    exclude_strains: frozenset[str] = field(
        default_factory=frozenset
    )

    # False is used when a species exists in the build only because an
    # explicit target admitted it. In that case, non-target assemblies must
    # not become an automatic back door around the narrow explicit request.
    automatic_selection_enabled: bool = True


def validate_assembly_selection_constraints(
    assemblies: Sequence[AssemblyRecord],
    constraints: AssemblySelectionConstraints,
) -> None:
    """Validate explicit selection requirements against the eligible pool."""

    accession_overlap = (
        constraints.include_accessions
        & constraints.exclude_accessions
    )

    if accession_overlap:
        raise DataIntegrityError(
            "Assembly selection constraints both include and exclude "
            "the same accession(s): "
            + ", ".join(sorted(accession_overlap))
        )

    strain_overlap = (
        constraints.include_strains
        & constraints.exclude_strains
    )

    if strain_overlap:
        raise DataIntegrityError(
            "Assembly selection constraints both include and exclude "
            "the same strain key(s): "
            + ", ".join(sorted(strain_overlap))
        )

    by_accession = {
        assembly.accession: assembly
        for assembly in assemblies
    }

    missing_accessions = (
        constraints.include_accessions
        - set(by_accession)
    )

    if missing_accessions:
        raise ConfigError(
            "One or more explicitly included assembly targets are not "
            "present among the eligible assembly candidates: "
            + ", ".join(sorted(missing_accessions))
        )

    available_strains: set[str] = set()

    for assembly in assemblies:

        accession = assembly.accession
        strain_key = strain_selection_key(
            assembly
        )

        if accession in constraints.exclude_accessions:
            continue

        if (
            strain_key is not None
            and strain_key in constraints.exclude_strains
        ):
            continue

        if strain_key is not None:
            available_strains.add(
                strain_key
            )

    missing_strains = (
        constraints.include_strains
        - available_strains
    )

    if missing_strains:
        raise ConfigError(
            "One or more explicitly included strain targets have no "
            "eligible non-excluded assembly candidates: "
            + ", ".join(sorted(missing_strains))
        )

    for accession in constraints.include_accessions:

        assembly = by_accession[
            accession
        ]

        strain_key = strain_selection_key(
            assembly
        )

        if (
            strain_key is not None
            and strain_key in constraints.exclude_strains
        ):
            raise DataIntegrityError(
                f"Explicitly included assembly {accession} belongs to "
                f"explicitly excluded strain key {strain_key!r}."
            )


def build_assembly_selection_constraints_by_species(
    species_map: Mapping[int, SpeciesCandidate],
    include_targets: Sequence[ResolvedExplicitTarget],
    exclude_targets: Sequence[ResolvedExplicitTarget],
) -> dict[int, AssemblySelectionConstraints]:
    """Build per-species explicit selection constraints.

    Explicit targets are additive for ordinary seed-derived species.

    A species admitted only because of an explicit target, or restored through
    a target-specific override of a broader taxonomic exclusion, is target-only:
    automatic selection must not activate unrelated assemblies or strains.
    """

    include_accessions: dict[
        int,
        set[str],
    ] = defaultdict(set)

    include_strains: dict[
        int,
        set[str],
    ] = defaultdict(set)

    exclude_accessions: dict[
        int,
        set[str],
    ] = defaultdict(set)

    exclude_strains: dict[
        int,
        set[str],
    ] = defaultdict(set)

    species_with_explicit_include: set[int] = set()

    for target in include_targets:

        if target.rule.action != "include":
            raise DataIntegrityError(
                "Include-target collection contains a non-include rule: "
                f"{target.rule.action!r}."
            )

        taxid = target.species_taxid

        if taxid not in species_map:
            raise DataIntegrityError(
                "Resolved explicit include target species is absent from the "
                f"curated species map: TaxID {taxid}."
            )

        species_with_explicit_include.add(
            taxid
        )

        if target.rule.target_type == "assembly":

            if target.assembly_accession is None:
                raise DataIntegrityError(
                    "Resolved explicit assembly include is missing its "
                    "assembly accession."
                )

            include_accessions[taxid].add(
                target.assembly_accession
            )

        elif target.rule.target_type == "strain":

            if target.strain_key is None:
                raise DataIntegrityError(
                    "Resolved explicit strain include is missing its "
                    "normalized strain key."
                )

            include_strains[taxid].add(
                target.strain_key
            )

        else:
            raise DataIntegrityError(
                "Unsupported resolved explicit include target type: "
                f"{target.rule.target_type!r}."
            )

    for target in exclude_targets:

        if target.rule.action != "exclude":
            raise DataIntegrityError(
                "Exclude-target collection contains a non-exclude rule: "
                f"{target.rule.action!r}."
            )

        taxid = target.species_taxid

        # An exclusion does not itself admit a species to the build. If the
        # species is outside the active curated species set, the exclusion is
        # simply irrelevant to this selection run.
        if taxid not in species_map:
            continue

        if target.rule.target_type == "assembly":

            if target.assembly_accession is None:
                raise DataIntegrityError(
                    "Resolved explicit assembly exclusion is missing its "
                    "assembly accession."
                )

            exclude_accessions[taxid].add(
                target.assembly_accession
            )

        elif target.rule.target_type == "strain":

            if target.strain_key is None:
                raise DataIntegrityError(
                    "Resolved explicit strain exclusion is missing its "
                    "normalized strain key."
                )

            exclude_strains[taxid].add(
                target.strain_key
            )

        else:
            raise DataIntegrityError(
                "Unsupported resolved explicit exclude target type: "
                f"{target.rule.target_type!r}."
            )

    result: dict[
        int,
        AssemblySelectionConstraints,
    ] = {}

    for taxid, species in species_map.items():

        target_only_species = (
            taxid in species_with_explicit_include
            and (
                species.inclusion_reason
                == "EXPLICIT_TARGET_INCLUDE"
                or (
                    species.exclusion_override
                    and not species.explicit_include
                )
            )
        )

        result[taxid] = AssemblySelectionConstraints(
            include_accessions=frozenset(
                include_accessions.get(
                    taxid,
                    set(),
                )
            ),
            include_strains=frozenset(
                include_strains.get(
                    taxid,
                    set(),
                )
            ),
            exclude_accessions=frozenset(
                exclude_accessions.get(
                    taxid,
                    set(),
                )
            ),
            exclude_strains=frozenset(
                exclude_strains.get(
                    taxid,
                    set(),
                )
            ),
            automatic_selection_enabled=(
                not target_only_species
            ),
        )

    return result


@dataclass
class AssemblySelectionExecution:
    selected_accessions: set[str]
    evaluated_accessions: set[str]
    not_selected_reasons: dict[str, str]

    eligible_strain_groups: int
    eligible_isolate_groups: int
    eligible_unassigned_groups: int

    represented_strains: set[str]
    represented_isolates: set[
        tuple[str | None, str]
    ]
    represented_unassigned_groups: set[
        tuple[str, str]
    ]

    species_fallback_used: bool = False

    # Number of assemblies accepted using proteins from the previous reference.
    reused_assemblies: int = 0


def execute_assembly_selection(
    assemblies: Sequence[AssemblyRecord],
    policy: AssemblySelectionPolicy,
    evaluate: Callable[[AssemblyRecord], bool],
    constraints: AssemblySelectionConstraints | None = None,
) -> AssemblySelectionExecution:
    """Execute one deterministic assembly-selection policy.

    `evaluate()` checks whether an assembly has usable protein data.

    During CREATE, this may require downloading and validating proteins.
    During UPDATE, unchanged assemblies can reuse verified proteins, while
    new or changed assemblies are downloaded only when needed.
    Final selection uses the protein availability already recorded in SQLite.

    Each accession is evaluated at most once. This is important when one
    assembly simultaneously represents a strain and one of its isolates.
    """

    ordered_all = sorted(
        assemblies,
        key=quality_rank_key,
    )

    active_constraints = (
        constraints
        if constraints is not None
        else AssemblySelectionConstraints()
    )

    validate_assembly_selection_constraints(
        ordered_all,
        active_constraints,
    )

    outcomes: dict[str, bool] = {}
    not_selected: dict[str, str] = {}

    ordered: list[AssemblyRecord] = []

    for assembly in ordered_all:

        accession = assembly.accession
        strain_key = strain_selection_key(
            assembly
        )

        if accession in active_constraints.exclude_accessions:

            not_selected[accession] = (
                "EXPLICIT_ASSEMBLY_EXCLUDED"
            )

            continue

        if (
            strain_key is not None
            and strain_key in active_constraints.exclude_strains
        ):

            not_selected[accession] = (
                "EXPLICIT_STRAIN_EXCLUDED"
            )

            continue

        explicit_target_member = (
            accession
            in active_constraints.include_accessions
            or (
                strain_key is not None
                and strain_key
                in active_constraints.include_strains
            )
        )

        if (
            not active_constraints.automatic_selection_enabled
            and not explicit_target_member
        ):

            not_selected[accession] = (
                "OUTSIDE_EXPLICIT_TARGET_SCOPE"
            )

            continue

        ordered.append(
            assembly
        )

    represented_strains: set[str] = set()

    represented_isolates: set[
        tuple[str | None, str]
    ] = set()

    represented_unassigned: set[
        tuple[str, str]
    ] = set()

    # STRAIN mode normally represents a species through its strain/isolate/
    # unassigned selection policy. If that entire policy yields no usable
    # assembly, one species-level fallback assembly may be selected later.
    species_fallback_used = False

    # Isolate groups counted here are only those that genuinely participate
    # in the active selection policy:
    #
    # - isolates belonging to strains that became represented;
    # - isolate-only groups when isolate expansion is enabled.
    #
    # Isolates belonging to strains excluded by the strain-depth policy do
    # not contribute to this denominator.
    eligible_isolates: set[
        tuple[str | None, str]
    ] = set()

    def evaluate_once(
        assembly: AssemblyRecord,
    ) -> bool:

        accession = assembly.accession

        if accession in outcomes:
            return outcomes[accession]

        usable = bool(
            evaluate(assembly)
        )

        outcomes[accession] = usable

        return usable

    by_accession = {
        assembly.accession: assembly
        for assembly in ordered
    }

    # ------------------------------------------------------------------
    # Exact explicit assembly inclusions.
    # ------------------------------------------------------------------
    explicit_assemblies = sorted(
        (
            by_accession[accession]
            for accession
            in active_constraints.include_accessions
        ),
        key=quality_rank_key,
    )

    for assembly in explicit_assemblies:

        if evaluate_once(
            assembly
        ):

            strain_key = strain_selection_key(
                assembly
            )

            if strain_key is not None:
                represented_strains.add(
                    strain_key
                )

    # ------------------------------------------------------------------
    # Explicit strain inclusions.
    # ------------------------------------------------------------------
    #
    # In normal STRAIN mode an explicitly requested strain uses the same
    # per-strain assembly depth as an automatic strain. In compact SPECIES
    # mode there is no per-strain depth setting, so the explicit strain means
    # one best usable representative.
    explicit_strain_depth = (
        policy.max_assemblies_per_strain
        if policy.mode == "STRAIN"
        else 1
    )

    if explicit_strain_depth is None:
        explicit_strain_depth = 1

    explicit_strain_records: dict[
        str,
        list[AssemblyRecord],
    ] = defaultdict(list)

    for assembly in ordered:

        strain_key = strain_selection_key(
            assembly
        )

        if (
            strain_key is not None
            and strain_key
            in active_constraints.include_strains
        ):

            explicit_strain_records[
                strain_key
            ].append(assembly)

    for records in explicit_strain_records.values():
        records.sort(
            key=quality_rank_key
        )

    ranked_explicit_strains = sorted(
        explicit_strain_records.items(),
        key=lambda item: quality_rank_key(
            item[1][0]
        ),
    )

    for strain_key, strain_records in ranked_explicit_strains:

        usable_in_strain = sum(
            outcomes.get(
                assembly.accession
            )
            is True
            for assembly in strain_records
        )

        for assembly in strain_records:

            if (
                usable_in_strain
                >= explicit_strain_depth
            ):
                break

            if assembly.accession in outcomes:
                continue

            if evaluate_once(
                assembly
            ):
                usable_in_strain += 1

        if usable_in_strain:
            represented_strains.add(
                strain_key
            )

    # ------------------------------------------------------------------
    # Compact species-level mode.
    # ------------------------------------------------------------------
    if policy.mode == "SPECIES":

        if policy.max_assemblies_per_species is None:
            raise DataIntegrityError(
                "SPECIES selection mode has no per-species assembly cap."
            )

        successful = 0

        if active_constraints.automatic_selection_enabled:

            for assembly in ordered:

                accession = assembly.accession
                strain_key = strain_selection_key(
                    assembly
                )

                # Explicit objects are additions to the automatic quota.
                if (
                    accession
                    in active_constraints.include_accessions
                    or (
                        strain_key is not None
                        and strain_key
                        in active_constraints.include_strains
                    )
                ):
                    continue

                if (
                    successful
                    >= policy.max_assemblies_per_species
                ):

                    not_selected[accession] = (
                        "MAX_ASSEMBLIES_PER_SPECIES"
                    )

                    continue

                if evaluate_once(
                    assembly
                ):
                    successful += 1

        for assembly in ordered:

            accession = assembly.accession

            if accession in outcomes:
                continue

            if accession in not_selected:
                continue

            strain_key = strain_selection_key(
                assembly
            )

            if (
                strain_key is not None
                and strain_key
                in active_constraints.include_strains
            ):
                reason = (
                    "EXPLICIT_STRAIN_ALREADY_REPRESENTED"
                )

            else:
                reason = (
                    "MAX_ASSEMBLIES_PER_SPECIES"
                )

            not_selected[
                accession
            ] = reason

        return AssemblySelectionExecution(
            selected_accessions={
                accession
                for accession, usable in outcomes.items()
                if usable
            },
            evaluated_accessions=set(outcomes),
            not_selected_reasons=not_selected,
            eligible_strain_groups=0,
            eligible_isolate_groups=0,
            eligible_unassigned_groups=0,
            represented_strains=set(),
            represented_isolates=set(),
            represented_unassigned_groups=set(),
        )

    if policy.mode != "STRAIN":
        raise DataIntegrityError(
            f"Unsupported assembly selection mode: {policy.mode}"
        )

    # ------------------------------------------------------------------
    # Partition candidates.
    # ------------------------------------------------------------------
    strain_groups: dict[
        str,
        list[AssemblyRecord],
    ] = defaultdict(list)

    isolate_only_groups: dict[
        tuple[str | None, str],
        list[AssemblyRecord],
    ] = defaultdict(list)

    unassigned_groups: dict[
        tuple[str, str],
        list[AssemblyRecord],
    ] = defaultdict(list)

    for assembly in ordered:

        strain_key = strain_selection_key(
            assembly
        )

        isolate_key = isolate_selection_key(
            assembly
        )

        if strain_key is not None:

            strain_groups[strain_key].append(
                assembly
            )

        elif isolate_key is not None:

            isolate_only_groups[
                isolate_key
            ].append(assembly)

        else:

            unassigned_groups[
                unassigned_selection_key(
                    assembly
                )
            ].append(assembly)

    for records in strain_groups.values():
        records.sort(key=quality_rank_key)

    for records in isolate_only_groups.values():
        records.sort(key=quality_rank_key)

    for records in unassigned_groups.values():
        records.sort(key=quality_rank_key)

    # A strain's priority is the quality rank of its best candidate assembly.
    ranked_strains = sorted(
        strain_groups.items(),
        key=lambda item: quality_rank_key(
            item[1][0]
        ),
    )

    automatic_represented_strains: set[str] = set()


    # ------------------------------------------------------------------
    # Isolate expansion helper for represented strains.
    # ------------------------------------------------------------------
    def expand_isolates_for_strain(
        strain_records: Sequence[AssemblyRecord],
    ) -> None:

        if not policy.include_isolates:
            return

        within_strain_isolates: dict[
            tuple[str | None, str],
            list[AssemblyRecord],
        ] = defaultdict(list)

        for assembly in strain_records:

            isolate_key = isolate_selection_key(
                assembly
            )

            if isolate_key is not None:

                within_strain_isolates[
                    isolate_key
                ].append(assembly)

        eligible_isolates.update(
            within_strain_isolates
        )

        for records in within_strain_isolates.values():
            records.sort(
                key=quality_rank_key
            )

        ranked_isolates = sorted(
            within_strain_isolates.items(),
            key=lambda item: quality_rank_key(
                item[1][0]
            ),
        )

        for isolate_key, isolate_records in ranked_isolates:

            already_represented = any(
                outcomes.get(
                    assembly.accession
                )
                is True
                for assembly in isolate_records
            )

            if already_represented:

                represented_isolates.add(
                    isolate_key
                )

                continue

            for assembly in isolate_records:

                if evaluate_once(
                    assembly
                ):

                    represented_isolates.add(
                        isolate_key
                    )

                    break

    # Explicit assembly/strain inclusions may already have represented strains
    # before the automatic strain quota begins. If isolate expansion is active,
    # those strains participate exactly like automatically represented strains.
    for strain_key in sorted(
        represented_strains
    ):

        records = strain_groups.get(
            strain_key
        )

        if records:
            expand_isolates_for_strain(
                records
            )

    # ------------------------------------------------------------------
    # Automatic strain-level representation.
    # ------------------------------------------------------------------
    for strain_key, strain_records in ranked_strains:

        # An explicitly included strain is additional to max-strains and must
        # therefore not consume one of the automatic strain slots.
        if strain_key in active_constraints.include_strains:
            continue

        # Exact explicitly included assemblies are also additive. They do not
        # consume the normal per-strain assembly quota.
        automatic_strain_records = [
            assembly
            for assembly in strain_records
            if assembly.accession
            not in active_constraints.include_accessions
        ]

        if not automatic_strain_records:
            continue

        if (
            not active_constraints.automatic_selection_enabled
        ):
            continue

        if (
            policy.max_strains_per_species is not None
            and len(
                automatic_represented_strains
            )
            >= policy.max_strains_per_species
        ):

            for assembly in automatic_strain_records:

                if assembly.accession not in outcomes:

                    not_selected[assembly.accession] = (
                        "MAX_STRAINS_PER_SPECIES"
                    )

            continue

        usable_in_strain = 0

        for assembly in automatic_strain_records:

            if (
                usable_in_strain
                >= policy.max_assemblies_per_strain
            ):
                break

            if evaluate_once(
                assembly
            ):
                usable_in_strain += 1

        if usable_in_strain == 0:

            # A completely failed automatic strain still does not consume a
            # max-strains slot.
            continue

        represented_strains.add(
            strain_key
        )

        automatic_represented_strains.add(
            strain_key
        )

        expand_isolates_for_strain(
            strain_records
        )

    # ------------------------------------------------------------------
    # Isolate-labelled records without strain assignment.
    # ------------------------------------------------------------------
    if policy.include_isolates:

        eligible_isolates.update(
            isolate_only_groups
        )

        ranked_isolate_only = sorted(
            isolate_only_groups.items(),
            key=lambda item: quality_rank_key(
                item[1][0]
            ),
        )

        for isolate_key, isolate_records in ranked_isolate_only:

            for assembly in isolate_records:

                if evaluate_once(assembly):

                    represented_isolates.add(
                        isolate_key
                    )

                    break

    else:

        for records in isolate_only_groups.values():

            for assembly in records:

                if assembly.accession not in outcomes:

                    not_selected[assembly.accession] = (
                        "ISOLATE_NOT_INCLUDED"
                    )

    # ------------------------------------------------------------------
    # Optional strain/isolate-unassigned expansion.
    # ------------------------------------------------------------------
    if policy.include_unassigned_assemblies:

        ranked_unassigned = sorted(
            unassigned_groups.items(),
            key=lambda item: quality_rank_key(
                item[1][0]
            ),
        )

        for group_key, records in ranked_unassigned:

            for assembly in records:

                if evaluate_once(assembly):

                    represented_unassigned.add(
                        group_key
                    )

                    break

    else:

        for records in unassigned_groups.values():

            for assembly in records:

                if assembly.accession not in outcomes:

                    not_selected[assembly.accession] = (
                        "UNASSIGNED_NOT_INCLUDED"
                    )

    # ------------------------------------------------------------------
    # Minimum per-species representation fallback.
    # ------------------------------------------------------------------
    # STRAIN mode should not lose an otherwise representable species merely
    # because NCBI provides no usable strain-associated assembly.
    #
    # If normal strain/isolate/unassigned selection produced no usable
    # assembly at all, fall back to the globally ranked candidate list and
    # retain at most the first usable assembly. Previously failed candidates
    # are not evaluated again because evaluate_once() caches their outcome.
    #
    # Candidates deliberately skipped by the normal strain policy may already
    # carry a provisional NOT_SELECTED reason. Remove that reason only when
    # the candidate is actually promoted into this fallback attempt.
    if (
        active_constraints.automatic_selection_enabled
        and not any(outcomes.values())
    ):

        for assembly in ordered:

            accession = assembly.accession

            # A previously evaluated candidate has already either failed or,
            # theoretically, succeeded. Failed candidates must not be retried.
            if accession in outcomes:
                continue

            not_selected.pop(
                accession,
                None,
            )

            if evaluate_once(assembly):

                species_fallback_used = True
                break

    # ------------------------------------------------------------------
    # Give every deliberately non-evaluated candidate an explicit reason.
    # ------------------------------------------------------------------
    for assembly in ordered:

        accession = assembly.accession

        if accession in outcomes:
            continue

        if accession in not_selected:
            continue

        strain_key = strain_selection_key(
            assembly
        )

        isolate_key = isolate_selection_key(
            assembly
        )

        if strain_key is not None:

            if (
                strain_key
                in active_constraints.include_strains
            ):
                reason = "MAX_ASSEMBLIES_PER_STRAIN"

            elif (
                strain_key
                not in automatic_represented_strains
                and policy.max_strains_per_species is not None
                and len(
                    automatic_represented_strains
                )
                >= policy.max_strains_per_species
            ):
                reason = "MAX_STRAINS_PER_SPECIES"

            elif (
                policy.include_isolates
                and isolate_key is not None
                and isolate_key in represented_isolates
            ):
                reason = "ISOLATE_ALREADY_REPRESENTED"

            else:
                reason = "MAX_ASSEMBLIES_PER_STRAIN"

        elif isolate_key is not None:

            if policy.include_isolates:
                reason = "ISOLATE_ALREADY_REPRESENTED"
            else:
                reason = "ISOLATE_NOT_INCLUDED"

        else:

            if policy.include_unassigned_assemblies:
                reason = "UNASSIGNED_GROUP_ALREADY_REPRESENTED"
            else:
                reason = "UNASSIGNED_NOT_INCLUDED"

        not_selected[accession] = reason

    return AssemblySelectionExecution(
        selected_accessions={
            accession
            for accession, usable in outcomes.items()
            if usable
        },
        evaluated_accessions=set(outcomes),
        not_selected_reasons=not_selected,
        eligible_strain_groups=len(
            strain_groups
        ),
        eligible_isolate_groups=(
            len(eligible_isolates)
            if policy.include_isolates
            else 0
        ),
        eligible_unassigned_groups=(
            len(unassigned_groups)
            if policy.include_unassigned_assemblies
            else 0
        ),
        represented_strains=represented_strains,
        represented_isolates=represented_isolates,
        represented_unassigned_groups=represented_unassigned,
        species_fallback_used=species_fallback_used,
    )


def insert_species_rows(state: StateDB, species_map: Mapping[int, SpeciesCandidate]) -> None:
    with state.conn:
        for s in species_map.values():
            state.conn.execute(
                """
                INSERT OR REPLACE INTO species(
                    taxid,species,phylum,class_name,order_name,family,genus,authority,tier,
                    seed_taxa,tier_source,explicit_include,evidence_type,reference,notes,
                    gut_evidence,human_evidence,inclusion_reason,matched_exclusion_taxa,
                    exclusion_override,final_taxonomic_status,filter_status,state,last_checked_ncbi
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    s.taxid,s.species,s.phylum,s.class_name,s.order_name,s.family,s.genus,s.authority,s.tier,
                    ";".join(s.seed_taxa) or ".",s.tier_source,int(s.explicit_include),s.evidence_type,s.reference,s.notes,
                    int(s.gut_evidence),int(s.human_evidence),s.inclusion_reason,";".join(s.matched_exclusion_taxa) or ".",
                    int(s.exclusion_override),s.final_taxonomic_status,"PENDING","PENDING",today_iso(),
                ),
            )


def apply_species_filters(
    state: StateDB,
    species_map: dict[int, SpeciesCandidate],
    rules: Sequence[FilterRule],
    *,
    required_taxids: set[int] | None = None,
) -> dict[int, SpeciesCandidate]:

    species_rules = [
        r
        for r in rules
        if r.level == "species"
    ]

    required = (
        required_taxids
        if required_taxids is not None
        else set()
    )
    if not species_rules:

        with state.conn:
            state.conn.execute(
                "UPDATE species "
                "SET filter_status='PASS' "
                "WHERE final_taxonomic_status='INCLUDED'"
            )

        return species_map

    kept: dict[int, SpeciesCandidate] = {}

    required_failures: list[
        tuple[
            SpeciesCandidate,
            list[dict[str, Any]],
        ]
    ] = []

    with state.conn:

        for taxid, species in species_map.items():

            failures = apply_filters_to_entity(
                species,
                species_rules,
            )

            if failures:

                state.conn.execute(
                    "UPDATE species "
                    "SET filter_status='FAIL' "
                    "WHERE taxid=?",
                    (taxid,),
                )

                for f in failures:

                    state.conn.execute(
                        """
                        INSERT INTO species_filter_failures(
                            species_taxid,
                            species,
                            filter_id,
                            field,
                            operator,
                            required,
                            observed,
                            failure_reason
                        )
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            taxid,
                            species.species,
                            f["Filter_ID"],
                            f["Field"],
                            f["Operator"],
                            f["Required"],
                            f["Observed"],
                            f["Failure_reason"],
                        ),
                    )

                if taxid in required:
                    required_failures.append(
                        (
                            species,
                            failures,
                        )
                    )

            else:

                state.conn.execute(
                    "UPDATE species "
                    "SET filter_status='PASS' "
                    "WHERE taxid=?",
                    (taxid,),
                )

                kept[taxid] = species

    if required_failures:

        details: list[str] = []

        for species, failures in required_failures:

            failed_filter_ids = ", ".join(
                str(f["Filter_ID"])
                for f in failures
            )

            details.append(
                f"{species.species} "
                f"(TaxID {species.taxid}): "
                f"{failed_filter_ids}"
            )

        raise ConfigError(
            "One or more species required by explicit inclusion targets "
            "failed active species metadata filters. Explicit targets do not "
            "bypass metadata filters.\n"
            + "\n".join(
                f"  - {detail}"
                for detail in details
            )
        )

    return kept


def assembly_to_db_tuple(a: AssemblyRecord) -> tuple[Any, ...]:
    return (
        a.accession,a.species_taxid,a.species,a.source,a.paired_accession,
        None if a.multi_isolate is None else int(a.multi_isolate),a.assembly_status,
        None if a.atypical is None else int(a.atypical),a.atypical_warnings,a.assembly_level,
        a.refseq_category,a.strain,a.isolate,a.infraspecific_name,a.bioproject,a.biosample,
        a.assembly_method,a.sequencing_technology,a.contig_n50,a.contig_l50,a.num_contigs,
        a.scaffold_n50,a.scaffold_l50,a.num_scaffolds,a.num_scaffold_gaps,a.genome_size,a.gc_percent,
        a.protein_coding_genes,a.busco_complete,a.busco_fragmented,a.busco_missing,a.busco_duplicated,
        a.busco_lineage,a.busco_version,a.release_date,a.annotation_name,a.annotation_release_date,
        a.annotation_status,None if a.protein_fasta_available is None else int(a.protein_fasta_available),
        a.protein_count,a.protein_fasta_sha256,a.ncbi_package_md5,a.fingerprint,a.source_scope_status,
        a.qc_status,int(a.selected),a.final_status,a.final_reason,a.update_change,a.update_reason,
    )


ASSEMBLY_INSERT_SQL = """
INSERT OR REPLACE INTO assemblies(
    accession,species_taxid,species,source,paired_accession,multi_isolate,assembly_status,atypical,
    atypical_warnings,assembly_level,refseq_category,strain,isolate,infraspecific_name,bioproject,biosample,
    assembly_method,sequencing_technology,contig_n50,contig_l50,num_contigs,scaffold_n50,scaffold_l50,
    num_scaffolds,num_scaffold_gaps,genome_size,gc_percent,protein_coding_genes,busco_complete,
    busco_fragmented,busco_missing,busco_duplicated,busco_lineage,busco_version,release_date,
    annotation_name,annotation_release_date,annotation_status,protein_fasta_available,protein_count,
    protein_fasta_sha256,ncbi_package_md5,fingerprint,source_scope_status,qc_status,selected,final_status,
    final_reason,update_change,update_reason
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def assembly_inventory_result_block(
    species: SpeciesCandidate,
    species_number: int,
    species_total: int,
    assembly: AssemblyRecord,
    assembly_number: int,
    assembly_total: int,
    *,
    result: str,
    details: list[tuple[str, str]],
) -> None:
    """Print one verbose multi-line result for an assembly inventory decision.

    Assembly inventory is sequential, so no multiprocessing console lock is
    required here. This output describes metadata-based assembly eligibility
    only; it does not imply that a usable protein FASTA has been found.
    """

    lines = [
        (
            f"Species {species_number}/{species_total} | "
            f"Assembly {assembly_number}/{assembly_total}: "
            f"{assembly.accession}"
        ),
        f"       Result:         {result}",
    ]

    for label, value in details:
        lines.append(
            f"       {label + ':':<16}{value}"
        )

    info("\n".join(lines))


def assembly_inventory_species_summary(
    species: SpeciesCandidate,
    species_number: int,
    species_total: int,
    *,
    assemblies_found: int,
    gcf_found: int,
    gca_found: int,
    multi_isolate_found: int,
    eligible: int,
    scope_excluded: int,
    hard_qc_failed: int,
    user_filter_failed: int,
) -> None:
    """Print a verbose summary of assembly inventory results for one species."""

    eligibility_summary = (
        "Eligible assembly FOUND"
        if eligible > 0
        else "Eligible assembly NOT FOUND"
    )

    info(
        f"Species {species_number}/{species_total} FINISHED - "
        f"{eligibility_summary}: "
        f"{species.species} (TaxID {species.taxid})\n"
        f"       Assemblies found:     {assemblies_found:,}\n"
        f"       GCF:                  {gcf_found:,}\n"
        f"       GCA:                  {gca_found:,}\n"
        f"       Multi-isolate metadata: {multi_isolate_found:,}\n"
        f"       Eligible candidates:  {eligible:,}\n"
        f"       Excluded by scope:    {scope_excluded:,}\n"
        f"       Failed hard QC:       {hard_qc_failed:,}\n"
        f"       Failed user filters:  {user_filter_failed:,}\n"
    )


def build_assembly_inventory(
    state: StateDB,
    client: NCBIClient,
    species_map: Mapping[int, SpeciesCandidate],
    config: BuildConfig,
    all_filter_rules: Sequence[FilterRule],
    explicit_include_targets: Sequence[ResolvedExplicitTarget] = (),
) -> dict[int, list[AssemblyRecord]]:

    assembly_rules = [
        r
        for r in all_filter_rules
        if r.level == "assembly"
    ]

    by_species: dict[int, list[AssemblyRecord]] = {}

    sorted_species = sorted(
        species_map.values(),
        key=lambda x: (x.species, x.taxid),
    )

    for i, species in enumerate(sorted_species, start=1):

        # This per-species progress line remains visible in normal output.
        # Detailed assembly-by-assembly decisions are shown only with --verbose.
        if config.verbose:
            info(
                f"Searching NCBI assemblies for species {i}/{len(species_map)} STARTED: "
                f"{species.species} (TaxID {species.taxid})"
            )
        else:
            info(
                f"Searching NCBI assemblies for species {i}/{len(species_map)}: "
                f"{species.species} (TaxID {species.taxid})"
            )

        records = inventory_species_assemblies(
            client,
            species,
        )

        (
            records,
            required_explicit_accessions,
        ) = merge_explicit_assembly_targets_into_inventory(
            records,
            species.taxid,
            explicit_include_targets,
        )

        inventory_by_accession = {
            a.accession: a
            for a in records
        }

        prelim_eligible: list[AssemblyRecord] = []

        # Verbose-only counters summarize how the NCBI assembly inventory was
        # reduced to the candidate pool used later for protein processing.
        scope_excluded = 0
        hard_qc_failed = 0
        user_filter_failed = 0

        gcf_found = sum(
            1
            for a in records
            if a.source == "GCF"
        )

        gca_found = sum(
            1
            for a in records
            if a.source == "GCA"
        )

        multi_isolate_found = sum(
            1
            for a in records
            if a.multi_isolate
        )

        explicit_target_rejections: list[str] = []

        with state.conn:

            for assembly_number, a in enumerate(records, start=1):

                if config.verbose:
                    info(
                        f"Species {i}/{len(species_map)} | "
                        f"Assembly {assembly_number}/{len(records)} STARTED: "
                        f"{a.accession}"
                    )

                in_scope, scope_reason = assembly_source_scope(
                    a,
                    inventory_by_accession,
                    config,
                    explicit_include_accessions=(
                        required_explicit_accessions
                    ),
                )

                a.source_scope_status = (
                    "PASS"
                    if in_scope
                    else "FAIL"
                )

                if not in_scope:
                    scope_excluded += 1

                    a.qc_status = "NOT_EVALUATED"
                    a.final_status = "EXCLUDED"
                    a.final_reason = scope_reason

                    state.conn.execute(
                        ASSEMBLY_INSERT_SQL,
                        assembly_to_db_tuple(a),
                    )

                    if (
                        a.accession
                        in required_explicit_accessions
                    ):
                        explicit_target_rejections.append(
                            f"{a.accession}: source-scope rejection "
                            f"({scope_reason})"
                        )

                    if config.verbose:
                        assembly_inventory_result_block(
                            species,
                            i,
                            len(species_map),
                            a,
                            assembly_number,
                            len(records),
                            result="EXCLUDED",
                            details=[
                                ("Source", a.source),
                                ("Source scope", "FAIL"),
                                ("Hard QC", "NOT EVALUATED"),
                                ("User filters", "NOT EVALUATED"),
                                ("Reason", scope_reason),
                            ],
                        )

                    continue

                hard_ok, hard_reason = hard_assembly_qc(
                    a,
                    config,
                )

                if not hard_ok:
                    hard_qc_failed += 1

                    a.qc_status = "FAIL"
                    a.final_status = "EXCLUDED"
                    a.final_reason = hard_reason

                    state.conn.execute(
                        ASSEMBLY_INSERT_SQL,
                        assembly_to_db_tuple(a),
                    )

                    state.conn.execute(
                        """
                        INSERT INTO assembly_failures(
                            accession,
                            species_taxid,
                            species,
                            filter_id,
                            field,
                            operator,
                            required,
                            observed,
                            failure_reason
                        )
                        VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            a.accession,
                            a.species_taxid,
                            a.species,
                            "HARD",
                            "Hard_eligibility",
                            "required",
                            "PASS",
                            hard_reason,
                            hard_reason,
                        ),
                    )

                    if (
                        a.accession
                        in required_explicit_accessions
                    ):
                        explicit_target_rejections.append(
                            f"{a.accession}: hard assembly QC failed "
                            f"({hard_reason})"
                        )

                    if config.verbose:
                        assembly_inventory_result_block(
                            species,
                            i,
                            len(species_map),
                            a,
                            assembly_number,
                            len(records),
                            result="EXCLUDED",
                            details=[
                                ("Source", a.source),
                                ("Source scope", "PASS"),
                                ("Hard QC", "FAIL"),
                                ("User filters", "NOT EVALUATED"),
                                ("Reason", hard_reason),
                            ],
                        )

                    continue

                failures = apply_filters_to_entity(
                    a,
                    assembly_rules,
                )

                if failures:
                    user_filter_failed += 1

                    a.qc_status = "FAIL"
                    a.final_status = "EXCLUDED"
                    a.final_reason = "USER_FILTER_FAILED"

                    state.conn.execute(
                        ASSEMBLY_INSERT_SQL,
                        assembly_to_db_tuple(a),
                    )

                    for f in failures:
                        state.conn.execute(
                            """
                            INSERT INTO assembly_failures(
                                accession,
                                species_taxid,
                                species,
                                filter_id,
                                field,
                                operator,
                                required,
                                observed,
                                failure_reason
                            )
                            VALUES(?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                a.accession,
                                a.species_taxid,
                                a.species,
                                f["Filter_ID"],
                                f["Field"],
                                f["Operator"],
                                f["Required"],
                                f["Observed"],
                                f["Failure_reason"],
                            ),
                        )

                    if (
                        a.accession
                        in required_explicit_accessions
                    ):
                        failed_filter_ids = ", ".join(
                            str(f["Filter_ID"])
                            for f in failures
                        )

                        explicit_target_rejections.append(
                            f"{a.accession}: active assembly metadata "
                            f"filter(s) failed ({failed_filter_ids})"
                        )

                    if config.verbose:
                        failed_filter_ids = ", ".join(
                            str(f["Filter_ID"])
                            for f in failures
                        )

                        assembly_inventory_result_block(
                            species,
                            i,
                            len(species_map),
                            a,
                            assembly_number,
                            len(records),
                            result="EXCLUDED",
                            details=[
                                ("Source", a.source),
                                ("Source scope", "PASS"),
                                ("Hard QC", "PASS"),
                                ("User filters", "FAIL"),
                                (
                                    "Failed filters",
                                    f"{len(failures)} ({failed_filter_ids})",
                                ),
                                ("Reason", "USER_FILTER_FAILED"),
                            ],
                        )

                    continue

                a.qc_status = "PASS"
                a.final_status = "ELIGIBLE_PROTEIN_PENDING"
                a.final_reason = "PASS_SCOPE_AND_METADATA_QC"

                prelim_eligible.append(a)

                state.conn.execute(
                    ASSEMBLY_INSERT_SQL,
                    assembly_to_db_tuple(a),
                )

                if config.verbose:
                    assembly_inventory_result_block(
                        species,
                        i,
                        len(species_map),
                        a,
                        assembly_number,
                        len(records),
                        result="ELIGIBLE",
                        details=[
                            ("Source", a.source),
                            ("Source scope", "PASS"),
                            ("Hard QC", "PASS"),
                            ("User filters", "PASS"),
                            (
                                "Next step",
                                "Protein FASTA validation",
                            ),
                        ],
                    )

        if explicit_target_rejections:

            raise ConfigError(
                "One or more explicitly included assembly targets failed "
                "mandatory assembly eligibility checks. Explicit targets may "
                "override automatic GCA source-scope policy, but they do not "
                "bypass hard assembly QC or active assembly metadata filters.\n"
                + "\n".join(
                    f"  - {detail}"
                    for detail
                    in explicit_target_rejections
                )
            )

        # Protein FASTA availability is only known after downloading an assembly.
        # Therefore biological depth selection is deliberately deferred to the
        # assembly-selection engine. Keep every candidate that passed source scope,
        # hard QC and user assembly filters, sorted deterministically by quality.
        #
        # Fresh builds apply strain/species/isolate/unassigned selection lazily
        # during protein processing so failed candidates can fall back to the next
        # ranked assembly. Updates initially process their complete new/changed
        # delta and resolve final selection later against reusable + processed
        # candidates together.
        prelim_eligible.sort(
            key=quality_rank_key
        )

        by_species[species.taxid] = prelim_eligible

        if config.verbose:
            assembly_inventory_species_summary(
                species,
                i,
                len(species_map),
                assemblies_found=len(records),
                gcf_found=gcf_found,
                gca_found=gca_found,
                multi_isolate_found=multi_isolate_found,
                eligible=len(prelim_eligible),
                scope_excluded=scope_excluded,
                hard_qc_failed=hard_qc_failed,
                user_filter_failed=user_filter_failed,
            )

    return by_species


########################################################################################################
# Protein FASTA streaming and species workers
########################################################################################################

LOCAL_WORKER_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE hashes (
    sequence_sha256 TEXT PRIMARY KEY,
    sequence_length INTEGER NOT NULL,
    store_offset INTEGER NOT NULL
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_sha256 TEXT NOT NULL,
    sequence_length INTEGER NOT NULL,
    species_taxid INTEGER NOT NULL,
    species TEXT NOT NULL,
    tier INTEGER NOT NULL,
    assembly TEXT NOT NULL,
    assembly_source TEXT,
    paired_accession TEXT,
    strain TEXT,
    isolate TEXT,
    original_protein_accession TEXT,
    original_header TEXT,
    original_had_terminal_stop INTEGER NOT NULL,
    has_internal_stop INTEGER NOT NULL,
    num_internal_stops INTEGER NOT NULL
);
CREATE INDEX idx_sources_hash ON sources(sequence_sha256);
CREATE TABLE warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species_taxid INTEGER,
    species TEXT,
    assembly TEXT,
    protein_accession TEXT,
    warning_type TEXT,
    count INTEGER
);
CREATE TABLE assembly_results (
    accession TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    protein_fasta_available INTEGER,
    protein_count INTEGER,
    protein_fasta_sha256 TEXT,
    ncbi_protein_md5 TEXT,
    detail TEXT
);
"""


def retryable_ncbi_failed_accessions(
    conn: sqlite3.Connection,
) -> tuple[str, ...]:
    """Return assemblies that ended in a classified NCBI retrieval failure."""

    reasons = tuple(
        sorted(NCBI_RETRIEVAL_FAILURE_REASONS)
    )

    placeholders = ",".join(
        "?"
        for _ in reasons
    )

    return tuple(
        str(row[0])
        for row in conn.execute(
            f"""
            SELECT accession
            FROM assembly_results
            WHERE status='FAILED'
              AND reason IN ({placeholders})
            ORDER BY accession
            """,
            reasons,
        )
    )


def retryable_ncbi_failed_accessions_from_state(
    state: StateDB,
    species_taxid: int,
    allowed_accessions: Sequence[str],
) -> tuple[str, ...]:
    """Return unresolved retryable NCBI failures from persistent state."""

    allowed = set(
        allowed_accessions
    )

    if not allowed:
        return ()

    reasons = tuple(
        sorted(
            NCBI_RETRIEVAL_FAILURE_REASONS
        )
    )

    placeholders = ",".join(
        "?"
        for _ in reasons
    )

    return tuple(
        sorted(
            str(row["assembly"])
            for row in state.conn.execute(
                f"""
                SELECT DISTINCT assembly
                FROM failed_downloads
                WHERE species_taxid=?
                  AND reason IN ({placeholders})
                """,
                (
                    species_taxid,
                    *reasons,
                ),
            )
            if str(row["assembly"]) in allowed
        )
    )


@dataclass
class FastaRecord:
    header: str
    sequence: str


def stream_fasta(path: Path) -> Iterator[FastaRecord]:
    """Strict streaming FASTA reader.

    The file is streamed instead of loaded entirely into memory. Malformed
    FASTA structure is reported as a data-integrity error.
    """
    header: str | None = None
    seq_parts: list[str] = []
    line_no = 0
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for raw in handle:
            line_no += 1
            line = raw.rstrip("\r\n")
            if not line.strip():
                # Empty formatting lines are harmless and do not become sequence.
                continue
            if line.startswith(">"):
                if header is not None:
                    sequence = "".join(seq_parts)
                    if not sequence:
                        raise DataIntegrityError(f"Empty FASTA sequence before {path}:{line_no}.")
                    yield FastaRecord(header=header, sequence=sequence)
                header = line[1:].strip()
                if not header:
                    raise DataIntegrityError(f"Empty FASTA header at {path}:{line_no}.")
                seq_parts = []
            else:
                if header is None:
                    raise DataIntegrityError(f"Sequence data encountered before first FASTA header at {path}:{line_no}.")
                # Remove formatting whitespace only. Every non-whitespace symbol
                # is otherwise preserved exactly at this stage.
                seq_parts.append("".join(line.split()))
    if header is None:
        raise DataIntegrityError(f"No FASTA records found in {path}.")
    sequence = "".join(seq_parts)
    if not sequence:
        raise DataIntegrityError(f"Final FASTA record in {path} has an empty sequence.")
    yield FastaRecord(header=header, sequence=sequence)


def canonicalize_protein(raw_sequence: str) -> tuple[str, bool, int]:
    sequence = "".join(raw_sequence.split()).upper()
    if not sequence:
        raise DataIntegrityError("Protein sequence is empty after removing FASTA formatting whitespace.")
    had_terminal_stop = sequence.endswith("*")
    if had_terminal_stop:
        sequence = sequence[:-1]
    if not sequence:
        raise DataIntegrityError("Protein sequence contains only a terminal stop marker and no amino-acid symbols.")
    # Internal stops are preserved as part of identity and explicitly counted.
    internal_stops = sequence.count("*")
    # Reject control characters defensively while deliberately NOT imposing a
    # narrow amino-acid alphabet; NCBI protein FASTAs may legitimately contain
    # ambiguity symbols such as X/B/Z/J/U/O.
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in sequence):
        raise DataIntegrityError("Protein sequence contains an invalid control/whitespace character after normalization.")
    return sequence, had_terminal_stop, internal_stops


def protein_accession_from_header(header: str) -> str:
    token = header.split(None, 1)[0].strip()
    return token or "NONUNIQUE"


def write_fasta_record(handle: Any, header: str, sequence: str, width: int = 60) -> None:
    handle.write(f">{header}\n")
    for i in range(0, len(sequence), width):
        handle.write(sequence[i:i + width] + "\n")


def read_sequence_from_store(handle: Any, offset: int, length: int) -> str:
    """Read one sequence without changing the store's current write position."""

    # The same file handle is used both for reading existing sequences and for
    # appending new ones. Preserve the current position so a verification read
    # cannot cause a later write to overwrite previously stored sequences.
    original_position = handle.tell()

    try:
        handle.seek(offset)
        data = handle.read(length)

        if len(data) != length:
            raise DataIntegrityError(
                "Canonical sequence store ended before the recorded sequence length."
            )

        if isinstance(data, bytes):
            return data.decode("ascii")

        return str(data)

    finally:
        handle.seek(original_position)


def verify_ncbi_md5_manifest(extract_dir: Path) -> dict[str, str]:
    manifest = extract_dir / "md5sum.txt"
    if not manifest.is_file():
        raise DataIntegrityError("NCBI data package is missing md5sum.txt.")
    expected: dict[str, str] = {}
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            raise DataIntegrityError(f"Malformed NCBI md5sum.txt line: {raw!r}")
        digest, rel = parts
        rel = rel.lstrip("* ").removeprefix("./")
        expected[rel] = digest.lower()
    for rel, digest in expected.items():
        path = extract_dir / rel
        if not path.is_file():
            raise DataIntegrityError(f"NCBI package checksum manifest references missing file: {rel}")
        observed = md5_file(path)
        if observed.lower() != digest:
            raise DataIntegrityError(f"NCBI package MD5 mismatch for {rel}: expected {digest}, observed {observed}")
    return expected


def find_protein_fasta(extract_dir: Path, accession: str) -> Path | None:
    exact = extract_dir / "ncbi_dataset" / "data" / accession / "protein.faa"
    if exact.is_file():
        return exact
    matches = sorted(extract_dir.rglob("protein.faa"))
    if not matches:
        return None
    if len(matches) > 1:
        # Exact-accession downloads should contain one assembly. Multiple FASTAs
        # are safer to reject than to concatenate accidentally.
        raise DataIntegrityError(
            f"NCBI package for {accession} unexpectedly contains multiple protein.faa files: "
            + ", ".join(str(x) for x in matches)
        )
    return matches[0]


def atomic_copy_to_ready(src: Path, ready: Path) -> None:
    ready.parent.mkdir(parents=True, exist_ok=True)
    part = ready.with_name(ready.name + ".part")
    with src.open("rb") as fin, part.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
        fsync_file(fout)
    atomic_replace(part, ready)
    fsync_dir(ready.parent)


@dataclass
class WorkerJob:
    species: SpeciesCandidate
    assemblies: list[AssemblyRecord]
    selection_policy: AssemblySelectionPolicy | None
    selection_constraints: AssemblySelectionConstraints | None
    temp_base: str
    ready_dir: str

    # Shared rate limiter: controls how frequently NCBI commands may start.
    shared_limiter: SharedLimiterProxy

    # Shared concurrency limiter: controls how many NCBI Datasets commands may
    # be alive simultaneously across all worker processes.
    shared_ncbi_concurrency: SharedConcurrencyLimiterProxy

    output_root: str
    # Shared lock used only while writing complete messages to stdout. Worker
    # computation remains fully parallel, only console writes are serialized.
    console_lock: Any
    verbose: bool
    timestamps: bool
    # Run-local identifiers used only to make concurrent console output easier
    # to follow.
    worker_id: int = 0
    worker_total: int = 0

    # UPDATE: assemblies whose protein data can be safely reused from
    # the previous reference without another NCBI download.
    # Empty for CREATE and recovery-only workers.
    reusable_accessions: frozenset[str] = frozenset()


@dataclass
class WorkerResult:
    species_taxid: int
    species: str
    fasta_ready: str
    sources_ready: str
    successful_assemblies: int
    unique_sequences: int
    total_source_occurrences: int
    attempted_assemblies: int
    failure_reasons: dict[str, int]

    retryable_ncbi_failures: tuple[str, ...] = ()

    eligible_strain_groups: int | None = None
    represented_strain_groups: int | None = None

    eligible_isolate_groups: int | None = None
    represented_isolate_groups: int | None = None

    eligible_unassigned_groups: int | None = None
    represented_unassigned_groups: int | None = None

    species_fallback_used: bool = False
    reused_assemblies: int = 0


@dataclass
class NCBIRecoverySummary:
    """Track one species through automatic restorative NCBI recovery."""

    species_taxid: int
    species: str
    retried_accessions: tuple[str, ...]
    recovered_accessions: tuple[str, ...]
    selected_before_recovery: tuple[str, ...]


def resolve_known_available_selection(
    state: StateDB,
    species_taxid: int,
    config: BuildConfig,
    constraints: AssemblySelectionConstraints,
) -> tuple[str, ...]:
    """Resolve biological selection from protein availability already in state."""

    current_pool = load_assembly_pool_from_state(
        state,
        [species_taxid],
    ).get(
        species_taxid,
        [],
    )

    usable_accessions: set[str] = set()

    for assembly in current_pool:
        row = state.conn.execute(
            """
            SELECT protein_fasta_available
            FROM assemblies
            WHERE accession=?
            """,
            (assembly.accession,),
        ).fetchone()

        if (
            row is not None
            and bool(
                row["protein_fasta_available"]
            )
        ):
            usable_accessions.add(
                assembly.accession
            )

    resolution = execute_assembly_selection(
        current_pool,
        assembly_selection_policy(config),
        lambda assembly: (
            assembly.accession
            in usable_accessions
        ),
        constraints=constraints,
    )

    return tuple(
        sorted(
            resolution.selected_accessions
        )
    )


def build_ncbi_recovery_job(
    job: WorkerJob,
    retry_accessions: Sequence[str],
) -> WorkerJob:
    """Build an isolated worker job for one automatic NCBI recovery pass."""

    requested = set(
        retry_accessions
    )

    if not requested:
        raise DataIntegrityError(
            "Cannot build an NCBI recovery job without assembly accessions."
        )

    available = {
        assembly.accession
        for assembly in job.assemblies
    }

    unknown = sorted(
        requested - available
    )

    if unknown:
        raise DataIntegrityError(
            "NCBI recovery requested assemblies outside the original "
            "species worker job: "
            + ", ".join(unknown)
        )

    recovery_assemblies = [
        assembly
        for assembly in job.assemblies
        if assembly.accession in requested
    ]

    recovery_temp_base = (
        Path(job.temp_base)
        / "ncbi_recovery"
    )

    recovery_ready_dir = (
        Path(job.ready_dir).parent
        / "ncbi_recovery_prepared_species"
    )

    recovery_temp_base.mkdir(
        parents=True,
        exist_ok=True,
    )

    recovery_ready_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return dataclasses.replace(
        job,
        assemblies=recovery_assemblies,

        # Recovery resolves protein availability only. Biological selection is
        # rerun later against the complete species pool.
        selection_policy=None,
        selection_constraints=None,

        # Recovery retries actual NCBI failures only. Reusable assemblies
        # must never be treated as recovery candidates.
        reusable_accessions=frozenset(),

        # Keep partial recovery products separate from the normal species-ready
        # checkpoint so they can never overwrite the first-pass result.
        temp_base=str(recovery_temp_base),
        ready_dir=str(recovery_ready_dir),

        # Recovery worker numbering is assigned independently when the recovery
        # queue is executed.
        worker_id=0,
        worker_total=0,
    )


def pending_manual_ncbi_recovery_accessions(
    state: StateDB,
) -> dict[int, tuple[str, ...]]:
    """Find unresolved NCBI retrieval failures in a completed reference.

    Include failed preferred assemblies even when another assembly already
    represents the species. Never retry assemblies with usable proteins.
    """
    retryable_reasons = (
        "NCBI_REQUEST_TIMEOUT",
        "NCBI_RATE_LIMITED",
        "NCBI_GATEWAY_ERROR",
        "NCBI_REQUEST_FAILED",
    )

    placeholders = ",".join("?" for _ in retryable_reasons)

    rows = state.conn.execute(
        f"""
        SELECT DISTINCT
            a.species_taxid AS taxid,
            a.accession AS accession
        FROM failed_downloads AS fd
        JOIN assemblies AS a
          ON a.accession = fd.assembly
         AND a.species_taxid = fd.species_taxid
        WHERE fd.reason IN ({placeholders})
          AND COALESCE(a.protein_fasta_available, 0) = 0
        ORDER BY a.species_taxid, a.accession
        """,
        retryable_reasons,
    ).fetchall()

    pending: dict[int, list[str]] = {}

    for row in rows:
        pending.setdefault(int(row["taxid"]), []).append(
            str(row["accession"])
        )

    return {
        taxid: tuple(accessions)
        for taxid, accessions in pending.items()
    }


def validate_species_products(fasta_path: Path, sqlite_path: Path, expected_assemblies: Sequence[str]) -> tuple[int, int]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        hashes = {row["sequence_sha256"]: (int(row["sequence_length"]), int(row["store_offset"])) for row in conn.execute("SELECT * FROM hashes")}
        source_count = int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
        result_rows = {row["accession"]: row for row in conn.execute("SELECT * FROM assembly_results")}
        expected_set = set(expected_assemblies)
        result_set = set(result_rows)

        missing_results = sorted(
            expected_set - result_set
        )

        unexpected_results = sorted(
            result_set - expected_set
        )

        if missing_results:
            raise DataIntegrityError(
                "Species worker result is missing explicit assembly states: "
                + ", ".join(missing_results)
            )

        if unexpected_results:
            raise DataIntegrityError(
                "Species worker result contains unexpected assembly states: "
                + ", ".join(unexpected_results)
            )

        allowed_statuses = {
            "COMMITTED",
            "FAILED",
            "NOT_SELECTED",
            "REUSABLE",
        }

        for accession, row in result_rows.items():
            status = row["status"]

            if status not in allowed_statuses:
                raise DataIntegrityError(
                    "Species worker result contains unsupported assembly status "
                    f"for {accession}: {status!r}"
                )

            if status == "COMMITTED":
                if (
                    row["protein_fasta_available"] != 1
                    or row["protein_count"] is None
                    or int(row["protein_count"]) <= 0
                ):
                    raise DataIntegrityError(
                        "Committed assembly result has inconsistent protein "
                        f"metadata: {accession}"
                    )

            elif status == "FAILED":
                if (
                    row["protein_fasta_available"] != 0
                    or row["protein_count"] is None
                    or int(row["protein_count"]) != 0
                ):
                    raise DataIntegrityError(
                        "Failed assembly result has inconsistent protein "
                        f"metadata: {accession}"
                    )

            elif status == "REUSABLE":
                # Reusable protein sources remain in the previous reference.
                # The UPDATE state already contains their verified availability
                # metadata; this worker checkpoint only records the reuse decision.
                if any(
                    row[field] is not None
                    for field in (
                        "protein_fasta_available",
                        "protein_count",
                        "protein_fasta_sha256",
                        "ncbi_protein_md5",
                    )
                ):
                    raise DataIntegrityError(
                        "Reusable assembly checkpoint unexpectedly contains "
                        f"protein metadata: {accession}"
                    )

            else:  # NOT_SELECTED
                if (
                    row["protein_fasta_available"] is not None
                    or row["protein_count"] is not None
                ):
                    raise DataIntegrityError(
                        "Not-selected assembly unexpectedly contains protein "
                        f"availability/count metadata: {accession}"
                    )

        invalid_source_assemblies = sorted(
            {
                row["assembly"]
                for row in conn.execute(
                    "SELECT DISTINCT assembly FROM sources"
                )
                if (
                    row["assembly"] not in result_rows
                    or result_rows[row["assembly"]]["status"] != "COMMITTED"
                )
            }
        )

        if invalid_source_assemblies:
            raise DataIntegrityError(
                "Species provenance contains sources for assemblies that are "
                "not COMMITTED: "
                + ", ".join(invalid_source_assemblies)
            )

        committed_without_sources = sorted(
            accession
            for accession, row in result_rows.items()
            if (
                row["status"] == "COMMITTED"
                and conn.execute(
                    "SELECT 1 FROM sources WHERE assembly=? LIMIT 1",
                    (accession,),
                ).fetchone()
                is None
            )
        )

        if committed_without_sources:
            raise DataIntegrityError(
                "Committed assemblies have no protein source records: "
                + ", ".join(committed_without_sources)
            )

        fasta_hashes: set[str] = set()
        count = 0
        if hashes:
            for rec in stream_fasta(fasta_path):
                count += 1
                seq, terminal, _ = canonicalize_protein(rec.sequence)
                if terminal:
                    raise DataIntegrityError("Species .ready FASTA unexpectedly contains a terminal stop marker.")
                digest = hashlib.sha256(seq.encode("ascii")).hexdigest()
                expected_header = rec.header.split(None, 1)[0]
                if expected_header != digest:
                    raise DataIntegrityError(f"Species FASTA header/hash mismatch: header={expected_header}, sequence={digest}")
                if digest in fasta_hashes:
                    raise DataIntegrityError(f"Species-local exact duplicate remained in .ready FASTA: {digest}")
                fasta_hashes.add(digest)
                if digest not in hashes or hashes[digest][0] != len(seq):
                    raise DataIntegrityError(f"Species FASTA/SQLite hash inventory mismatch for {digest}")
        elif fasta_path.stat().st_size != 0:
            raise DataIntegrityError("Species FASTA contains data but the local hash inventory is empty.")
        if count != len(hashes):
            raise DataIntegrityError(f"Species FASTA record count ({count}) != SQLite unique hash count ({len(hashes)}).")

        orphan_sources = conn.execute(
            """
            SELECT COUNT(*)
            FROM sources s
            LEFT JOIN hashes h
              ON s.sequence_sha256 = h.sequence_sha256
            WHERE h.sequence_sha256 IS NULL
            """
        ).fetchone()[0]

        if orphan_sources:
            raise DataIntegrityError(
                "Species provenance contains "
                f"{orphan_sources} source rows without a canonical sequence."
            )

        orphan_hashes = conn.execute(
            """
            SELECT COUNT(*)
            FROM hashes h
            LEFT JOIN sources s
              ON s.sequence_sha256 = h.sequence_sha256
            WHERE s.sequence_sha256 IS NULL
            """
        ).fetchone()[0]

        if orphan_hashes:
            raise DataIntegrityError(
                "Species canonical inventory contains "
                f"{orphan_hashes} sequence hashes without provenance."
            )

        return count, source_count

    finally:
        conn.close()


def process_species_worker(job: WorkerJob) -> WorkerResult:
    """Process one species in an isolated process and private workspace."""
    species = job.species
    temp_base = Path(job.temp_base)
    ready_dir = Path(job.ready_dir)
    output_root = Path(job.output_root)
    temp_base.mkdir(parents=True, exist_ok=True)
    ready_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(tempfile.mkdtemp(prefix=f"FF_{species.taxid}_", dir=temp_base))
    local_db_path = work_dir / f"Species_{species.taxid}.sources.sqlite.part"
    local_bin_path = work_dir / f"Species_{species.taxid}.seq.bin.part"
    local_fasta_path = work_dir / f"Species_{species.taxid}.faa.part"
    local_log_dir = work_dir / "logs"
    local_log_dir.mkdir()

    conn = sqlite3.connect(local_db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(LOCAL_WORKER_SCHEMA)
    conn.commit()

    successful = 0
    attempted = 0
    failure_reasons: dict[str, int] = {}

    candidate_numbers = {
        assembly.accession: index
        for index, assembly in enumerate(
            job.assemblies,
            start=1,
        )
    }


    def evaluate_candidate(
        assembly: AssemblyRecord,
    ) -> bool:
        """Attempt one assembly and return whether usable protein data committed."""

        nonlocal successful, attempted

        candidate_number = candidate_numbers[
            assembly.accession
        ]

        attempted += 1

        if job.verbose:
            worker_candidate_info(
                job,
                candidate_number,
                assembly,
                "STARTED",
            )

        # Record available disk space for the final candidate summary. The actual
        # safety checks below remain authoritative and are never bypassed by --force.
        disk_free_detail: str | None = None

        try:
            free_temp = shutil.disk_usage(work_dir).free
            free_out = shutil.disk_usage(output_root).free

            if work_dir.stat().st_dev == output_root.stat().st_dev:
                disk_free_detail = f"{human_gib(free_out)} free"
            else:
                disk_free_detail = (
                    f"temp {human_gib(free_temp)} free; "
                    f"output {human_gib(free_out)} free"
                )

        except OSError:
            pass

        ensure_disk_reserve(
            work_dir,
            context=f"download {assembly.accession} temporary workspace",
        )
        ensure_disk_reserve(
            output_root,
            context=f"download {assembly.accession} output workspace",
        )

        if disk_free_detail is not None:
            disk_status = f"OK ({disk_free_detail})"
        else:
            disk_status = "OK"

        download_status = "NOT_REACHED"
        package_status = "NOT_REACHED"
        protein_fasta_status = "NOT_REACHED"

        zip_path = (
            work_dir
            / f"{safe_component(assembly.accession)}.zip"
        )

        extract_dir = (
            work_dir
            / f"{safe_component(assembly.accession)}_extract"
        )

        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        extract_dir.mkdir()

        fasta_checkpoint = fasta_out.tell()
        bin_checkpoint = seq_store.tell()

        conn.execute("BEGIN IMMEDIATE")

        try:
            download_status = "FAILED"

            cmd = [
                "datasets",
                "download",
                "genome",
                "accession",
                assembly.accession,
                "--include",
                "protein",
                "--filename",
                str(zip_path),
                "--no-progressbar",
            ]

            run_ncbi_command(
                cmd,
                limiter=job.shared_limiter,
                concurrency_limiter=job.shared_ncbi_concurrency,
                log_path=(
                    local_log_dir
                    / f"{safe_component(assembly.accession)}.download.log"
                ),
                timeout=60 * 60,
            )

            download_status = "OK"
            package_status = "FAILED"

            if (
                not zip_path.is_file()
                or zip_path.stat().st_size == 0
            ):
                raise DataIntegrityError(
                    "NCBI Datasets reported success but the downloaded ZIP "
                    "is missing/empty."
                )

            with zipfile.ZipFile(zip_path) as zf:
                bad_member = zf.testzip()

                if bad_member is not None:
                    raise DataIntegrityError(
                        f"ZIP CRC validation failed for member {bad_member}"
                    )

                zf.extractall(extract_dir)

            md5_manifest = verify_ncbi_md5_manifest(
                extract_dir
            )

            package_status = "OK"
            protein_fasta_status = "NOT_FOUND"

            protein_fasta = find_protein_fasta(
                extract_dir,
                assembly.accession,
            )

            if protein_fasta is None:
                raise FileNotFoundError(
                    "NO_PROTEIN_FASTA"
                )

            protein_fasta_status = "FOUND"

            protein_rel = (
                protein_fasta
                .relative_to(extract_dir)
                .as_posix()
            )

            ncbi_protein_md5 = md5_manifest.get(
                protein_rel
            )

            protein_sha256 = sha256_file(
                protein_fasta
            )

            protein_count = 0
            assembly_unique_added = 0

            for rec in stream_fasta(protein_fasta):
                protein_count += 1

                accession = protein_accession_from_header(
                    rec.header
                )

                (
                    sequence,
                    terminal_stop,
                    internal_stops,
                ) = canonicalize_protein(
                    rec.sequence
                )

                digest = hashlib.sha256(
                    sequence.encode("ascii")
                ).hexdigest()

                length = len(sequence)

                existing = conn.execute(
                    """
                    SELECT sequence_length,store_offset
                    FROM hashes
                    WHERE sequence_sha256=?
                    """,
                    (digest,),
                ).fetchone()

                if existing is None:
                    offset = seq_store.tell()

                    seq_store.write(
                        sequence.encode("ascii")
                    )

                    conn.execute(
                        """
                        INSERT INTO hashes(
                            sequence_sha256,
                            sequence_length,
                            store_offset
                        )
                        VALUES(?,?,?)
                        """,
                        (
                            digest,
                            length,
                            offset,
                        ),
                    )

                    write_fasta_record(
                        fasta_out,
                        digest,
                        sequence,
                    )

                    assembly_unique_added += 1

                else:
                    existing_seq = read_sequence_from_store(
                        seq_store,
                        int(existing["store_offset"]),
                        int(existing["sequence_length"]),
                    )

                    if existing_seq != sequence:
                        raise HashCollisionError(
                            "SHA-256 collision detected within species "
                            f"{species.species} for digest {digest}. "
                            "Sequences will not be collapsed. Build stopped "
                            "for integrity review."
                        )

                conn.execute(
                    """
                    INSERT INTO sources(
                        sequence_sha256,
                        sequence_length,
                        species_taxid,
                        species,
                        tier,
                        assembly,
                        assembly_source,
                        paired_accession,
                        strain,
                        isolate,
                        original_protein_accession,
                        original_header,
                        original_had_terminal_stop,
                        has_internal_stop,
                        num_internal_stops
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        digest,
                        length,
                        species.taxid,
                        species.species,
                        species.tier,
                        assembly.accession,
                        assembly.source,
                        assembly.paired_accession,
                        assembly.strain,
                        assembly.isolate,
                        accession,
                        rec.header,
                        int(terminal_stop),
                        int(internal_stops > 0),
                        internal_stops,
                    ),
                )

                if terminal_stop:
                    conn.execute(
                        """
                        INSERT INTO warnings(
                            species_taxid,
                            species,
                            assembly,
                            protein_accession,
                            warning_type,
                            count
                        )
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            species.taxid,
                            species.species,
                            assembly.accession,
                            accession,
                            "TERMINAL_STOP_NORMALIZED",
                            1,
                        ),
                    )

                if internal_stops:
                    conn.execute(
                        """
                        INSERT INTO warnings(
                            species_taxid,
                            species,
                            assembly,
                            protein_accession,
                            warning_type,
                            count
                        )
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            species.taxid,
                            species.species,
                            assembly.accession,
                            accession,
                            "INTERNAL_STOP_RETAINED",
                            internal_stops,
                        ),
                    )

            if protein_count == 0:
                raise DataIntegrityError(
                    "Protein FASTA contains zero records."
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO assembly_results
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    assembly.accession,
                    "COMMITTED",
                    "PASS",
                    1,
                    protein_count,
                    protein_sha256,
                    ncbi_protein_md5,
                    ".",
                ),
            )

            conn.commit()

            successful += 1

            if job.verbose:
                worker_candidate_result_block(
                    job,
                    candidate_number,
                    assembly,
                    result="ACCEPTED",
                    level="INFO",
                    details=[
                        ("Source", assembly.source),
                        ("Disk", disk_status),
                        ("Download", download_status),
                        ("Package", package_status),
                        ("Protein FASTA", protein_fasta_status),
                        ("Proteins", f"{protein_count:,}"),
                        ("Unique", f"{assembly_unique_added:,}"),
                    ],
                )

            return True

        except FileNotFoundError as exc:
            conn.rollback()

            fasta_out.seek(fasta_checkpoint)
            fasta_out.truncate()

            seq_store.seek(bin_checkpoint)
            seq_store.truncate()

            reason = (
                "NO_PROTEIN_FASTA"
                if str(exc) == "NO_PROTEIN_FASTA"
                else "FILE_NOT_FOUND"
            )

            failure_reasons[reason] = (
                failure_reasons.get(reason, 0) + 1
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO assembly_results
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    assembly.accession,
                    "FAILED",
                    reason,
                    0,
                    0,
                    None,
                    None,
                    str(exc),
                ),
            )

            conn.commit()

            if job.verbose:
                worker_candidate_result_block(
                    job,
                    candidate_number,
                    assembly,
                    result="REJECTED",
                    level="WARNING",
                    details=[
                        ("Source", assembly.source),
                        ("Disk", disk_status),
                        ("Download", download_status),
                        ("Package", package_status),
                        ("Protein FASTA", protein_fasta_status),
                        ("Reason", reason),
                    ],
                )

            return False

        except NCBIRequestError as exc:
            conn.rollback()

            fasta_out.seek(fasta_checkpoint)
            fasta_out.truncate()

            seq_store.seek(bin_checkpoint)
            seq_store.truncate()

            reason = exc.reason

            failure_reasons[reason] = (
                failure_reasons.get(reason, 0) + 1
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO assembly_results
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    assembly.accession,
                    "FAILED",
                    reason,
                    0,
                    0,
                    None,
                    None,
                    exc.detail[:2000],
                ),
            )

            conn.commit()

            if job.verbose:
                worker_candidate_result_block(
                    job,
                    candidate_number,
                    assembly,
                    result="REJECTED",
                    level="WARNING",
                    details=[
                        ("Source", assembly.source),
                        ("Disk", disk_status),
                        ("Download", download_status),
                        ("Package", package_status),
                        ("Protein FASTA", protein_fasta_status),
                        ("Reason", reason),
                    ],
                )

            return False

        except (
            ResourceSafetyError,
            HashCollisionError,
        ):
            conn.rollback()

            fasta_out.seek(fasta_checkpoint)
            fasta_out.truncate()

            seq_store.seek(bin_checkpoint)
            seq_store.truncate()

            raise

        except (
            FungiFunkError,
            UnicodeError,
            OSError,
            zipfile.BadZipFile,
        ) as exc:
            conn.rollback()

            fasta_out.seek(fasta_checkpoint)
            fasta_out.truncate()

            seq_store.seek(bin_checkpoint)
            seq_store.truncate()

            reason = "ASSEMBLY_PROCESSING_FAILED"

            failure_reasons[reason] = (
                failure_reasons.get(reason, 0) + 1
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO assembly_results
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    assembly.accession,
                    "FAILED",
                    reason,
                    0,
                    0,
                    None,
                    None,
                    str(exc)[:2000],
                ),
            )

            conn.commit()

            if job.verbose:
                worker_candidate_result_block(
                    job,
                    candidate_number,
                    assembly,
                    result="REJECTED",
                    level="WARNING",
                    details=[
                        ("Source", assembly.source),
                        ("Disk", disk_status),
                        ("Download", download_status),
                        ("Package", package_status),
                        ("Protein FASTA", protein_fasta_status),
                        ("Reason", reason),
                    ],
                )

            return False

        finally:
            try:
                if zip_path.exists():
                    zip_path.unlink()
            except OSError:
                pass

            shutil.rmtree(
                extract_dir,
                ignore_errors=True,
            )

    resolution: AssemblySelectionExecution | None = None

    try:
        with (
            local_bin_path.open("w+b") as seq_store,
            local_fasta_path.open(
                "w+",
                encoding="utf-8",
            ) as fasta_out,
        ):

            if job.selection_policy is None:

                # NCBI recovery retries assemblies that failed earlier.
                # Biological selection is applied again after recovery,
                # so the retry worker processes every requested assembly.
                for assembly in job.assemblies:
                    evaluate_candidate(
                        assembly
                    )

            else:

                # CREATE and UPDATE use the same biological selection policy.
                # UPDATE can also accept assemblies whose proteins were
                # already verified for reuse, without downloading them again.
                def evaluate_for_selection(
                    assembly: AssemblyRecord,
                ) -> bool:

                    if assembly.accession not in job.reusable_accessions:
                        return evaluate_candidate(assembly)

                    # This assembly already has usable proteins in the
                    # previous reference. Record the reuse decision without
                    # copying its proteins into the worker files.
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO assembly_results
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            assembly.accession,
                            "REUSABLE",
                            "UNCHANGED_SOURCE",
                            None,
                            None,
                            None,
                            None,
                            "Proteins available for reuse from the previous reference.",
                        ),
                    )

                    # Keep the reuse decision committed before another
                    # candidate starts its own SQLite transaction.
                    conn.commit()

                    return True

                resolution = execute_assembly_selection(
                    job.assemblies,
                    job.selection_policy,
                    evaluate_for_selection,
                    constraints=job.selection_constraints,
                )

                # Every deliberately non-attempted candidate must still receive
                # an explicit assembly state so .ready validation and resume
                # remain deterministic.
                for accession, reason in (
                    resolution.not_selected_reasons.items()
                ):

                    existing = conn.execute(
                        """
                        SELECT 1
                        FROM assembly_results
                        WHERE accession=?
                        """,
                        (accession,),
                    ).fetchone()

                    if existing is not None:
                        continue

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO assembly_results
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            accession,
                            "NOT_SELECTED",
                            reason,
                            None,
                            None,
                            None,
                            None,
                            "Candidate was not attempted because its "
                            "biological selection role was already filled.",
                        ),
                    )

                conn.commit()

            retryable_ncbi_failures = (
                retryable_ncbi_failed_accessions(
                    conn
                )
            )

            fsync_file(seq_store)
            fsync_file(fasta_out)

        conn.commit()

        # Count only assemblies that the selection actually reused.
        # Other reusable candidates may have been skipped after filling
        # the biological selection quota.
        reused_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM assembly_results
                WHERE status='REUSABLE'
                """
            ).fetchone()[0]
        )

        conn.close()

        expected = [
            assembly.accession
            for assembly in job.assemblies
        ]

        unique_count, source_count = validate_species_products(
            local_fasta_path,
            local_db_path,
            expected,
        )

        fasta_ready = (
            ready_dir
            / f"Species_{species.taxid}.faa.ready"
        )

        sources_ready = (
            ready_dir
            / f"Species_{species.taxid}.sources.sqlite.ready"
        )

        atomic_copy_to_ready(
            local_fasta_path,
            fasta_ready,
        )

        atomic_copy_to_ready(
            local_db_path,
            sources_ready,
        )

        return WorkerResult(
            species_taxid=species.taxid,
            species=species.species,
            fasta_ready=str(fasta_ready),
            sources_ready=str(sources_ready),
            successful_assemblies=successful,
            unique_sequences=unique_count,
            total_source_occurrences=source_count,
            attempted_assemblies=attempted,
            failure_reasons=failure_reasons,
            reused_assemblies=reused_count,
            retryable_ncbi_failures=(
                retryable_ncbi_failures
            ),

            species_fallback_used=(
                resolution.species_fallback_used
                if resolution is not None
                else False
            ),

            eligible_strain_groups=(
                resolution.eligible_strain_groups
                if resolution is not None
                else None
            ),
            represented_strain_groups=(
                len(resolution.represented_strains)
                if resolution is not None
                else None
            ),
            eligible_isolate_groups=(
                resolution.eligible_isolate_groups
                if resolution is not None
                else None
            ),
            represented_isolate_groups=(
                len(resolution.represented_isolates)
                if resolution is not None
                else None
            ),
            eligible_unassigned_groups=(
                resolution.eligible_unassigned_groups
                if resolution is not None
                else None
            ),
            represented_unassigned_groups=(
                len(
                    resolution.represented_unassigned_groups
                )
                if resolution is not None
                else None
            ),
        )

    finally:
        try:
            conn.close()
        except Exception:
            pass

        shutil.rmtree(
            work_dir,
            ignore_errors=True,
        )


def import_species_worker_result(
    state: StateDB,
    result: WorkerResult,
    assembly_map: Mapping[str, AssemblyRecord],
    *,
    replace_species_data: bool = True,
) -> None:
    fasta_path = Path(result.fasta_ready)
    local_db_path = Path(result.sources_ready)
    validate_species_products(fasta_path, local_db_path, [a.accession for a in assembly_map.values()])
    local = sqlite3.connect(local_db_path)
    local.row_factory = sqlite3.Row
    try:
        with state.conn:
            if replace_species_data:
                # Full species import: replace the complete previously imported
                # worker result so resume/re-import remains idempotent.
                state.conn.execute(
                    "DELETE FROM sources WHERE species_taxid=?",
                    (result.species_taxid,),
                )
                state.conn.execute(
                    "DELETE FROM protein_warnings WHERE species_taxid=?",
                    (result.species_taxid,),
                )
                state.conn.execute(
                    "DELETE FROM failed_downloads WHERE species_taxid=?",
                    (result.species_taxid,),
                )

            else:
                # Partial recovery import: replace data only for the assemblies
                # being retried. Successful first-pass assemblies for the same
                # species must remain untouched.
                for accession in sorted(assembly_map):
                    state.conn.execute(
                        """
                        DELETE FROM sources
                        WHERE species_taxid=?
                          AND assembly=?
                        """,
                        (
                            result.species_taxid,
                            accession,
                        ),
                    )
                    state.conn.execute(
                        """
                        DELETE FROM protein_warnings
                        WHERE species_taxid=?
                          AND assembly=?
                        """,
                        (
                            result.species_taxid,
                            accession,
                        ),
                    )
                    state.conn.execute(
                        """
                        DELETE FROM failed_downloads
                        WHERE species_taxid=?
                          AND assembly=?
                        """,
                        (
                            result.species_taxid,
                            accession,
                        ),
                    )

            for row in local.execute("SELECT * FROM sources"):
                source_id = f"FFSRC_{row['sequence_sha256']}"
                state.conn.execute(
                    """INSERT INTO sources(sequence_sha256,sequence_length,source_id,species_taxid,species,tier,assembly,
                           assembly_source,paired_accession,strain,isolate,original_protein_accession,original_header,
                           original_had_terminal_stop,has_internal_stop,num_internal_stops)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["sequence_sha256"],row["sequence_length"],source_id,row["species_taxid"],row["species"],row["tier"],
                        row["assembly"],row["assembly_source"],row["paired_accession"],row["strain"],row["isolate"],
                        row["original_protein_accession"],row["original_header"],row["original_had_terminal_stop"],
                        row["has_internal_stop"],row["num_internal_stops"],
                    ),
                )
            for row in local.execute("SELECT * FROM warnings"):
                state.conn.execute(
                    "INSERT INTO protein_warnings(species_taxid,species,assembly,protein_accession,warning_type,count) VALUES(?,?,?,?,?,?)",
                    (row["species_taxid"],row["species"],row["assembly"],row["protein_accession"],row["warning_type"],row["count"]),
                )
            for row in local.execute("SELECT * FROM assembly_results"):
                accession = row["accession"]
                status = row["status"]
                if status == "COMMITTED":
                    state.conn.execute(
                        """UPDATE assemblies SET selected=1,final_status='PROCESSED',final_reason='PASS',
                               protein_fasta_available=1,protein_count=?,protein_fasta_sha256=?,ncbi_package_md5=? WHERE accession=?""",
                        (row["protein_count"],row["protein_fasta_sha256"],row["ncbi_protein_md5"],accession),
                    )
                elif status == "REUSABLE":
                    # Only assemblies previously verified by UPDATE may be
                    # reused. Their protein sources are imported later,
                    # after the final biological selection.
                    existing = state.conn.execute(
                        """
                        SELECT update_change, update_reason,
                               protein_fasta_available, protein_count
                        FROM assemblies
                        WHERE accession=? AND species_taxid=?
                        """,
                        (accession, result.species_taxid),
                    ).fetchone()

                    if (
                        existing is None
                        or existing["update_change"] != "KEPT"
                        or existing["update_reason"] != "UNCHANGED"
                        or existing["protein_fasta_available"] != 1
                        or existing["protein_count"] is None
                        or int(existing["protein_count"]) <= 0
                    ):
                        raise DataIntegrityError(
                            "Worker reported an assembly as reusable without "
                            "a verified UPDATE reuse classification: "
                            f"{accession}"
                        )

                    # Preserve the availability metadata from classify_update().
                    # Final assembly selection and source import happen later.

                elif status == "NOT_SELECTED":
                    state.conn.execute(
                        "UPDATE assemblies SET selected=0,final_status='NOT_SELECTED',final_reason=? WHERE accession=?",
                        (row["reason"],accession),
                    )

                else:
                    state.conn.execute(
                        """UPDATE assemblies SET selected=0,final_status='EXCLUDED',final_reason=?,
                               protein_fasta_available=0,protein_count=0 WHERE accession=?""",
                        (row["reason"],accession),
                    )
                    state.conn.execute(
                        "INSERT INTO failed_downloads(species_taxid,species,assembly,reason,detail) VALUES(?,?,?,?,?)",
                        (result.species_taxid,result.species,accession,row["reason"],row["detail"]),
                    )
            state.conn.execute("UPDATE species SET state='PREPARED' WHERE taxid=?", (result.species_taxid,))
    finally:
        local.close()


def recover_ready_species(state: StateDB, species: SpeciesCandidate, assemblies: Sequence[AssemblyRecord], ready_dir: Path) -> bool:
    fasta_ready = ready_dir / f"Species_{species.taxid}.faa.ready"
    sources_ready = ready_dir / f"Species_{species.taxid}.sources.sqlite.ready"
    if not (fasta_ready.is_file() and sources_ready.is_file()):
        return False
    try:
        unique_count, source_count = validate_species_products(fasta_ready, sources_ready, [a.accession for a in assemblies])
        result = WorkerResult(
            species_taxid=species.taxid,
            species=species.species,
            fasta_ready=str(fasta_ready),
            sources_ready=str(sources_ready),
            successful_assemblies=0,
            unique_sequences=unique_count,
            total_source_occurrences=source_count,
            attempted_assemblies=0,
            failure_reasons={},
        )
        import_species_worker_result(state, result, {a.accession: a for a in assemblies})
        info(f"Recovered validated prepared species from previous interrupted run: {species.species}")
        return True
    except FungiFunkError as exc:
        warn(f"Discarding invalid stale .ready product for {species.species}: {exc}")
        fasta_ready.unlink(missing_ok=True)
        sources_ready.unlink(missing_ok=True)
        return False


def recover_ncbi_recovery_ready_species(
    state: StateDB,
    species: SpeciesCandidate,
    assemblies: Sequence[AssemblyRecord],
    ready_dir: Path,
) -> tuple[str, ...] | None:
    """Import a committed automatic NCBI recovery product after interruption."""

    fasta_ready = (
        ready_dir
        / f"Species_{species.taxid}.faa.ready"
    )

    sources_ready = (
        ready_dir
        / f"Species_{species.taxid}.sources.sqlite.ready"
    )

    fasta_exists = fasta_ready.is_file()
    sources_exists = sources_ready.is_file()

    if not fasta_exists and not sources_exists:
        return None

    if not (
        fasta_exists
        and sources_exists
    ):
        warn(
            "Discarding incomplete automatic NCBI recovery checkpoint for "
            f"{species.species}; recovery will be attempted again."
        )

        fasta_ready.unlink(
            missing_ok=True
        )

        sources_ready.unlink(
            missing_ok=True
        )

        return None

    local: sqlite3.Connection | None = None

    try:
        local = sqlite3.connect(
            sources_ready
        )

        local.row_factory = sqlite3.Row

        result_rows = list(
            local.execute(
                """
                SELECT *
                FROM assembly_results
                ORDER BY accession
                """
            )
        )

        recovery_accessions = tuple(
            str(row["accession"])
            for row in result_rows
        )

        if not recovery_accessions:
            raise DataIntegrityError(
                "Automatic NCBI recovery checkpoint contains no "
                "assembly result rows."
            )

        assembly_map = {
            assembly.accession: assembly
            for assembly in assemblies
        }

        unknown = sorted(
            set(recovery_accessions)
            - set(assembly_map)
        )

        if unknown:
            raise DataIntegrityError(
                "Automatic NCBI recovery checkpoint contains assemblies "
                "outside the current species candidate pool: "
                + ", ".join(unknown)
            )

        unique_count, source_count = (
            validate_species_products(
                fasta_ready,
                sources_ready,
                recovery_accessions,
            )
        )

        successful = sum(
            1
            for row in result_rows
            if row["status"] == "COMMITTED"
        )

        failure_reasons = dict(
            Counter(
                str(row["reason"])
                for row in result_rows
                if row["status"] == "FAILED"
            )
        )

        result = WorkerResult(
            species_taxid=species.taxid,
            species=species.species,
            fasta_ready=str(
                fasta_ready
            ),
            sources_ready=str(
                sources_ready
            ),
            successful_assemblies=successful,
            unique_sequences=unique_count,
            total_source_occurrences=source_count,
            attempted_assemblies=len(
                recovery_accessions
            ),
            failure_reasons=failure_reasons,
        )

        import_species_worker_result(
            state,
            result,
            {
                accession: assembly_map[
                    accession
                ]
                for accession
                in recovery_accessions
            },
            replace_species_data=False,
        )

        info(
            "Recovered validated automatic NCBI recovery checkpoint from "
            f"previous interrupted run: {species.species}"
        )

        return recovery_accessions

    except (
        FungiFunkError,
        sqlite3.Error,
        OSError,
    ) as exc:
        warn(
            "Discarding invalid automatic NCBI recovery checkpoint for "
            f"{species.species}: {exc}"
        )

        fasta_ready.unlink(
            missing_ok=True
        )

        sources_ready.unlink(
            missing_ok=True
        )

        return None

    finally:
        if local is not None:
            local.close()


def worker_prefix(job: WorkerJob) -> str:
    """Return a compact, human-readable prefix for one species worker job."""

    # Use at least two digits, but expand naturally for runs with >=100 jobs.
    width = max(2, len(str(job.worker_total)))

    return (
        f"[W{job.worker_id:0{width}d}/{job.worker_total:0{width}d}] "
        f"{job.species.species}"
    )


def worker_candidate_label(
    job: WorkerJob,
    candidate_number: int,
    assembly: AssemblyRecord,
) -> str:
    """Return the repeated identifier for one candidate assembly attempt."""

    return (
        f"{worker_prefix(job)} | "
        f"Candidate {candidate_number}/{len(job.assemblies)} | "
        f"{assembly.accession}"
    )


def worker_console_print(
    job: WorkerJob,
    message: str,
) -> None:
    """Print one complete worker message without interleaving processes.

    The shared console lock is held only while stdout is being written. It
    does not serialize downloads, protein processing or any other worker work.
    """

    rendered = timestamp_console_message(
        message,
        timestamps=job.timestamps,
    )

    with job.console_lock:
        print(
            rendered,
            flush=True,
        )


def representation_summary_parts(
    policy: AssemblySelectionPolicy,
    *,
    eligible_strain_groups: int,
    represented_strain_groups: int,
    eligible_isolate_groups: int,
    represented_isolate_groups: int,
    eligible_unassigned_groups: int,
    represented_unassigned_groups: int,
    species_fallback_used: bool = False,
) -> list[str]:
    """Return concise representation coverage for one resolved selection."""

    if policy.mode != "STRAIN":
        return []

    parts = [
        (
            "Strains represented="
            f"{represented_strain_groups}/"
            f"{eligible_strain_groups}"
        )
    ]

    if policy.include_isolates:
        parts.append(
            "Isolates represented="
            f"{represented_isolate_groups}/"
            f"{eligible_isolate_groups}"
        )

    if policy.include_unassigned_assemblies:
        parts.append(
            "Unassigned groups represented="
            f"{represented_unassigned_groups}/"
            f"{eligible_unassigned_groups}"
        )

    if species_fallback_used:
        parts.append(
            "Species fallback=used"
        )

    return parts


def worker_representation_summary_parts(
    job: WorkerJob,
    result: WorkerResult,
) -> list[str]:
    """Summarize biological representation after a worker's first pass.

    CREATE and UPDATE use the same selection rules during processing.
    UPDATE may change its final selection after NCBI recovery.
    Recovery workers do not calculate this summary.
    """

    policy = job.selection_policy

    if policy is None:
        return []

    required = (
        result.eligible_strain_groups,
        result.represented_strain_groups,
        result.eligible_isolate_groups,
        result.represented_isolate_groups,
        result.eligible_unassigned_groups,
        result.represented_unassigned_groups,
    )

    if any(value is None for value in required):
        raise DataIntegrityError(
            "Worker result is missing representation coverage metrics."
        )

    return representation_summary_parts(
        policy,
        eligible_strain_groups=result.eligible_strain_groups,
        represented_strain_groups=result.represented_strain_groups,
        eligible_isolate_groups=result.eligible_isolate_groups,
        represented_isolate_groups=result.represented_isolate_groups,
        eligible_unassigned_groups=result.eligible_unassigned_groups,
        represented_unassigned_groups=result.represented_unassigned_groups,
        species_fallback_used=result.species_fallback_used,
    )


def worker_candidate_info(
    job: WorkerJob,
    candidate_number: int,
    assembly: AssemblyRecord,
    message: str,
) -> None:
    """Print one informational message for a specific candidate assembly."""

    worker_console_print(
        job,
        (
            f"[INFO] {worker_candidate_label(job, candidate_number, assembly)} "
            f"--> {message}"
        ),
    )


def worker_candidate_result_block(
    job: WorkerJob,
    candidate_number: int,
    assembly: AssemblyRecord,
    *,
    result: str,
    level: str,
    details: list[tuple[str, str]],
) -> None:
    """Print one complete multi-line candidate result block."""

    level_prefix = f"[{level}] "
    continuation = " " * len(level_prefix)

    lines = [
        f"{level_prefix}{worker_prefix(job)}",
        (
            f"{continuation}"
            f"Candidate {candidate_number}/{len(job.assemblies)}: "
            f"{assembly.accession}"
        ),
        f"{continuation}Result:        {result}",
    ]

    biological_details = (
        ("Strain", normalize_missing(assembly.strain)),
        ("Isolate", normalize_missing(assembly.isolate)),
        ("BioSample", normalize_missing(assembly.biosample)),
        ("BioProject", normalize_missing(assembly.bioproject)),
    )

    for label, value in biological_details:
        if value is None:
            continue

        lines.append(
            f"{continuation}{label + ':':<15}{value}"
        )

    for label, value in details:
        lines.append(
            f"{continuation}{label + ':':<15}{value}"
        )

    worker_console_print(
        job,
        "\n".join(lines),
    )


def worker_finished_block(
    job: WorkerJob,
    *,
    level: str,
    attempted: int,
    accepted: int,
    proteins: int,
    unique: int,
    representation_parts: list[str] | None = None,
    failure_reasons: Mapping[str, int] | None = None,
    update_mode: bool = False,
    reused: int = 0,
) -> None:
    """Print one complete multi-line species completion block."""

    level_prefix = f"[{level}] "
    continuation = " " * len(level_prefix)

    lines = [
        f"{level_prefix}{worker_prefix(job)}",
        f"{continuation}FINISHED",
    ]

    if representation_parts:
        lines.append(
            f"{continuation}"
            + " | ".join(
                representation_parts
            )
        )

    if update_mode:
        # UPDATE shows reused assemblies separately from newly processed ones.
        # Protein counts here include only new worker output.
        lines.extend(
            [
                f"{continuation}Assemblies attempted: {attempted:,}",
                f"{continuation}Newly processed: {accepted:,}",
                f"{continuation}Reused: {reused:,}",
                f"{continuation}New protein records: {proteins:,}",
                f"{continuation}New unique sequences: {unique:,}",
            ]
        )

    else:
        # Keep the existing CREATE and NCBI recovery output unchanged.
        lines.extend(
            [
                f"{continuation}Assemblies attempted: {attempted:,}",
                f"{continuation}Accepted:  {accepted:,}",
                f"{continuation}Proteins:  {proteins:,}",
                f"{continuation}Unique:    {unique:,}",
            ]
        )

    if failure_reasons:
        lines.append(f"{continuation}Failures:")

        for reason, count in sorted(failure_reasons.items()):
            lines.append(
                f"{continuation}  {reason}: {count:,}"
            )

    worker_console_print(
        job,
        "\n".join(lines),
    )


########################################################################################################
# Resume reconstruction and resource planning
########################################################################################################

def species_from_db_row(row: sqlite3.Row) -> SpeciesCandidate:
    return SpeciesCandidate(
        taxid=int(row["taxid"]), species=row["species"], phylum=normalize_missing(row["phylum"]),
        class_name=normalize_missing(row["class_name"]), order_name=normalize_missing(row["order_name"]),
        family=normalize_missing(row["family"]), genus=normalize_missing(row["genus"]),
        authority=normalize_missing(row["authority"]), tier=int(row["tier"]),
        seed_taxa=split_semicolon_values(row["seed_taxa"]), tier_source=row["tier_source"] or ".",
        explicit_include=bool(row["explicit_include"]), evidence_type=row["evidence_type"] or ".",
        reference=row["reference"] or ".", notes=row["notes"] or ".",
        gut_evidence=bool(row["gut_evidence"]), human_evidence=bool(row["human_evidence"]),
        inclusion_reason=row["inclusion_reason"] or ".",
        matched_exclusion_taxa=split_semicolon_values(row["matched_exclusion_taxa"]),
        exclusion_override=bool(row["exclusion_override"]),
        final_taxonomic_status=row["final_taxonomic_status"],
    )


def assembly_from_db_row(row: sqlite3.Row) -> AssemblyRecord:
    kwargs: dict[str, Any] = {}
    for field_info in dataclasses.fields(AssemblyRecord):
        name = field_info.name
        if name == "raw_record":
            continue
        if name in row.keys():
            kwargs[name] = row[name]
    for name in ["multi_isolate", "atypical", "protein_fasta_available", "selected"]:
        if name in kwargs and kwargs[name] is not None:
            kwargs[name] = bool(kwargs[name])
    return AssemblyRecord(**kwargs)


def load_manual_ncbi_recovery_selection_context(
    state: StateDB,
    work_root: Path,
    *,
    resolved_targets_out: list[ResolvedExplicitTarget] | None = None,
) -> tuple[
    dict[int, SpeciesCandidate],
    dict[int, list[AssemblyRecord]],
    dict[int, AssemblySelectionConstraints],
]:
    """Reconstruct the original selection context without NCBI discovery.

    Species and assembly candidates come from the completed persistent state.
    Explicit-target rules and their resolved identities come exclusively from
    the immutable configuration snapshots copied into recovery staging.
    """
    config_dir = work_root / CONFIG_DIR

    for name in (SEED_SNAPSHOT, EXCLUSION_SNAPSHOT):
        snapshot = config_dir / name
        if not snapshot.is_file():
            raise DataIntegrityError(
                "Manual NCBI recovery requires the original biological "
                f"configuration snapshot: {snapshot}"
            )

    include_snapshot = (
        config_dir / INCLUDE_EXPLICIT_TARGETS_SNAPSHOT
    )
    exclude_snapshot = (
        config_dir / EXCLUDE_EXPLICIT_TARGETS_SNAPSHOT
    )

    include_rules = (
        parse_explicit_targets(
            include_snapshot,
            action="include",
            warn_if_empty=False,
        )
        if include_snapshot.is_file()
        else []
    )

    exclude_rules = (
        parse_explicit_targets(
            exclude_snapshot,
            action="exclude",
            warn_if_empty=False,
        )
        if exclude_snapshot.is_file()
        else []
    )

    resolved_targets = load_resolved_explicit_targets(
        config_dir / RESOLVED_EXPLICIT_TARGETS_JSON,
        [
            *include_rules,
            *exclude_rules,
        ],
    )

    include_count = len(include_rules)

    resolved_include_targets = resolved_targets[:include_count]
    resolved_exclude_targets = resolved_targets[include_count:]

    species_map = load_species_map_from_state(
        state,
        only_filter_pass=True,
    )

    full_pools = load_assembly_pool_from_state(
        state,
        species_map.keys(),
    )

    selection_constraints = (
        build_assembly_selection_constraints_by_species(
            species_map,
            resolved_include_targets,
            resolved_exclude_targets,
        )
    )

    if resolved_targets_out is not None:
        resolved_targets_out.extend(resolved_targets)

    return species_map, full_pools, selection_constraints


def load_species_map_from_state(state: StateDB, *, only_filter_pass: bool = True) -> dict[int, SpeciesCandidate]:
    sql = "SELECT * FROM species WHERE final_taxonomic_status='INCLUDED'"
    if only_filter_pass:
        sql += " AND filter_status='PASS'"
    rows = state.conn.execute(sql).fetchall()
    return {int(row["taxid"]): species_from_db_row(row) for row in rows}


def load_assembly_pool_from_state(state: StateDB, species_taxids: Iterable[int]) -> dict[int, list[AssemblyRecord]]:
    result: dict[int, list[AssemblyRecord]] = {}
    for taxid in species_taxids:
        rows = state.conn.execute(
            "SELECT * FROM assemblies WHERE species_taxid=? AND source_scope_status='PASS' AND qc_status='PASS' ORDER BY accession",
            (taxid,),
        ).fetchall()
        records = [assembly_from_db_row(row) for row in rows]
        records.sort(key=quality_rank_key)
        result[taxid] = records
    return result


def directory_size_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for base, dirs, files in os.walk(root):
        for name in files:
            path = Path(base) / name
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


@dataclass
class ResourcePlan:
    cpu_count: int
    ram_available_bytes: int
    temp_free_bytes: int
    output_free_bytes: int
    source_db_bytes: int
    estimated_update_staging_bytes: int
    safe_workers: int
    used_workers: int
    limiting_factors: str
    temp_path: str
    output_path: str


def calculate_resource_plan(config: BuildConfig, work_root: Path, temp_base: Path, source_root: Path | None) -> ResourcePlan:
    temp_base.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    cpu = get_cpu_count()
    ram = get_available_ram_bytes()
    temp_free = shutil.disk_usage(temp_base).free
    output_free = shutil.disk_usage(work_root).free
    reserve = int(EMERGENCY_FREE_DISK_GIB * 1024 ** 3)
    source_size = directory_size_bytes(source_root) if source_root and source_root.exists() else 0
    update_staging = int(source_size * UPDATE_STAGING_SAFETY_FACTOR) if source_root else 0

    # A destructive update must be able to hold the old DB and a staged updated
    # product simultaneously. For create-new update we use the same conservative
    # estimate because the new final reference can approach the old reference size.
    if source_root and output_free - reserve < update_staging:
        raise ResourceSafetyError(
            f"Insufficient output disk for safe update staging. Free={human_gib(output_free)}, "
            f"estimated staged reference={human_gib(update_staging)}, mandatory reserve={EMERGENCY_FREE_DISK_GIB:.1f} GiB. "
            "The source database has not been modified."
        )

    ram_workers = max(0, int(ram // int(ESTIMATED_RAM_GIB_PER_WORKER * 1024 ** 3)))
    temp_usable = max(0, temp_free - reserve)
    temp_workers = max(0, int(temp_usable // int(ESTIMATED_TMP_GIB_PER_WORKER * 1024 ** 3)))

    # On the output filesystem reserve the estimated staged DB before estimating
    # how many concurrently prepared worker products we can tolerate. Use the
    # same per-worker disk heuristic for a conservative upper bound.
    output_usable = max(0, output_free - reserve - update_staging)
    output_workers = max(0, int(output_usable // int(ESTIMATED_TMP_GIB_PER_WORKER * 1024 ** 3)))
    safe = min(cpu, ram_workers, temp_workers, output_workers)
    limiting: list[str] = []
    if safe == cpu: limiting.append("CPU")
    if safe == ram_workers: limiting.append("RAM")
    if safe == temp_workers: limiting.append("TEMP_DISK")
    if safe == output_workers: limiting.append("OUTPUT_DISK")
    if safe < 1:
        raise ResourceSafetyError(
            "Resource estimator cannot safely allocate even one worker with the current heuristics. "
            f"CPU={cpu}, RAM available={human_gib(ram)}, temp free={human_gib(temp_free)}, output free={human_gib(output_free)}."
        )

    if config.workers == "auto":
        used = safe
    else:
        requested = int(config.workers)
        if requested > safe and not config.force:
            raise ResourceSafetyError(
                f"Requested --workers {requested}, but the calculated safe recommendation is {safe} "
                f"(limiting factors: {', '.join(limiting)}). Use --workers {safe}, free resources, "
                f"or explicitly acknowledge the local resource risk with --workers {requested} --force."
            )
        used = requested

    return ResourcePlan(
        cpu_count=cpu, ram_available_bytes=ram, temp_free_bytes=temp_free, output_free_bytes=output_free,
        source_db_bytes=source_size, estimated_update_staging_bytes=update_staging,
        safe_workers=safe, used_workers=used, limiting_factors=";".join(limiting) or ".",
        temp_path=str(temp_base), output_path=str(work_root),
    )


def write_resource_usage(path: Path, plan: ResourcePlan, config: BuildConfig) -> None:
    rows = [
        {"Metric": "CPU_count", "Value": plan.cpu_count},
        {"Metric": "Available_RAM_bytes_start", "Value": plan.ram_available_bytes},
        {"Metric": "Temp_free_bytes_start", "Value": plan.temp_free_bytes},
        {"Metric": "Output_free_bytes_start", "Value": plan.output_free_bytes},
        {"Metric": "Source_DB_size_bytes", "Value": plan.source_db_bytes},
        {"Metric": "Estimated_update_staging_bytes", "Value": plan.estimated_update_staging_bytes},
        {"Metric": "Estimated_RAM_GiB_per_worker", "Value": ESTIMATED_RAM_GIB_PER_WORKER},
        {"Metric": "Estimated_TMP_GiB_per_worker", "Value": ESTIMATED_TMP_GIB_PER_WORKER},
        {"Metric": "Emergency_free_disk_GiB", "Value": EMERGENCY_FREE_DISK_GIB},
        {"Metric": "Safe_workers", "Value": plan.safe_workers},
        {"Metric": "Workers_used", "Value": plan.used_workers},
        {"Metric": "Worker_limiting_factors", "Value": plan.limiting_factors},
        {"Metric": "Force", "Value": config.force},
        {"Metric": "Temp_path", "Value": plan.temp_path},
        {"Metric": "Output_path", "Value": plan.output_path},
        {
            "Metric": "NCBI_API_key_detected",
            "Value": config.ncbi_api_key_detected,
        },

        {
            "Metric": "NCBI_request_rate_rps",
            "Value": config.ncbi_request_rate,
        },

        # Maximum number of datasets commands that worker processes may keep
        # alive simultaneously. This is independent from --workers and from the
        # request-start rate limit above.
        {
            "Metric": "NCBI_max_concurrent_commands",
            "Value": NCBI_MAX_CONCURRENT_COMMANDS,
        },

        # The effective cap can be lower when the user intentionally runs fewer
        # local workers than the internal NCBI concurrency maximum.
        {
            "Metric": "NCBI_effective_concurrency_cap",
            "Value": min(
                NCBI_MAX_CONCURRENT_COMMANDS,
                plan.used_workers,
            ),
        },
    ]
    write_tsv(path, ["Metric", "Value"], rows)


########################################################################################################
# Validate the source reference before incremental updates
########################################################################################################

class OldReference:
    """Read-only view of a completed source FungiFunk_refdb."""

    def __init__(
        self,
        root: Path,
        *,
        immutable_source: bool = False,
    ):
        self.root = root
        db_path = root / STATE_DB_REL

        # Preserve the original SQLite behavior for existing callers.
        uri = f"file:{db_path.as_posix()}?mode=ro"

        # Manual recovery reads a validated, completed source reference
        # that must remain completely unchanged.
        if immutable_source:
            uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
        try:
            self.conn = sqlite3.connect(uri, uri=True, timeout=60.0)
        except sqlite3.Error as exc:
            raise DataIntegrityError(f"Could not open source state database read-only: {db_path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.store_path = root / CANONICAL_STORE_REL
        if not self.store_path.is_file():
            raise DataIntegrityError(f"Completed source reference is missing canonical sequence store: {self.store_path}")

    def close(self) -> None:
        self.conn.close()

    def assemblies(self) -> dict[str, sqlite3.Row]:
        return {row["accession"]: row for row in self.conn.execute("SELECT * FROM assemblies")}

    def species_taxids(self) -> set[int]:
        return {
            int(row["taxid"])
            for row in self.conn.execute(
                "SELECT taxid FROM species WHERE final_taxonomic_status='INCLUDED' AND filter_status='PASS'"
            )
        }

    def has_sources(self, accession: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sources WHERE assembly=? LIMIT 1", (accession,)).fetchone() is not None

    def sequence(self, digest: str) -> str:
        row = self.conn.execute(
            "SELECT sequence_length,store_offset FROM canonical_sequences WHERE sequence_sha256=?",
            (digest,),
        ).fetchone()
        if row is None:
            raise DataIntegrityError(f"Source DB has provenance for hash {digest} but no canonical sequence index entry.")
        with self.store_path.open("rb") as handle:
            return read_sequence_from_store(handle, int(row["store_offset"]), int(row["sequence_length"]))

    def import_sources_for_assembly(self, state: StateDB, accession: str) -> int:
        rows = self.conn.execute("SELECT * FROM sources WHERE assembly=? ORDER BY id", (accession,))
        count = 0
        with state.conn:
            # Make reuse idempotent across interrupted finalization/resume.
            state.conn.execute("DELETE FROM sources WHERE assembly=?", (accession,))
            state.conn.execute("DELETE FROM protein_warnings WHERE assembly=?", (accession,))
            for row in rows:
                state.conn.execute(
                    """INSERT INTO sources(sequence_sha256,sequence_length,source_id,species_taxid,species,tier,assembly,
                           assembly_source,paired_accession,strain,isolate,original_protein_accession,original_header,
                           original_had_terminal_stop,has_internal_stop,num_internal_stops)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(row[k] for k in [
                        "sequence_sha256","sequence_length","source_id","species_taxid","species","tier","assembly",
                        "assembly_source","paired_accession","strain","isolate","original_protein_accession","original_header",
                        "original_had_terminal_stop","has_internal_stop","num_internal_stops",
                    ]),
                )
                count += 1
            # Reuse old warning rows for selected unchanged assemblies so the
            # warning report describes the final DB rather than only new downloads.
            for row in self.conn.execute("SELECT * FROM protein_warnings WHERE assembly=?", (accession,)):
                state.conn.execute(
                    "INSERT INTO protein_warnings(species_taxid,species,assembly,protein_accession,warning_type,count) VALUES(?,?,?,?,?,?)",
                    (row["species_taxid"],row["species"],row["assembly"],row["protein_accession"],row["warning_type"],row["count"]),
                )
        return count


def verify_manifest(root: Path) -> None:
    manifest = root / MANIFEST_REL
    if not manifest.is_file():
        raise DataIntegrityError(f"Completed source reference is missing build manifest: {manifest}")
    with manifest.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        expected_cols = {"File", "Size_bytes", "SHA256"}
        if not expected_cols.issubset(reader.fieldnames or []):
            raise DataIntegrityError(f"Invalid source manifest columns in {manifest}.")
        checked = 0
        for row in reader:
            rel = Path(row["File"])
            if rel.is_absolute() or ".." in rel.parts:
                raise DataIntegrityError(f"Unsafe path in source manifest: {rel}")
            path = root / rel
            if not path.is_file():
                raise DataIntegrityError(f"Source manifest file is missing: {path}")
            size = path.stat().st_size
            if size != int(row["Size_bytes"]):
                raise DataIntegrityError(f"Source manifest size mismatch for {rel}: expected {row['Size_bytes']}, observed {size}")
            digest = sha256_file(path)
            if digest != row["SHA256"]:
                raise DataIntegrityError(f"Source manifest SHA-256 mismatch for {rel}.")
            checked += 1
    if checked == 0:
        raise DataIntegrityError("Source build manifest contains no files.")


def write_manifest(root: Path) -> None:
    rows: list[dict[str, Any]] = []
    # logs are intentionally excluded because the shell wrapper appends its final
    # exit status after Python returns. tmp is transient.
    #
    # The manifest itself is excluded to avoid self-referential hashing.
    # The atomic completion marker is also excluded because it is deliberately
    # written only AFTER the completed manifest has been successfully verified.
    for top in [DATABASE_DIR, METADATA_DIR, REPORTS_DIR, CONFIG_DIR, STATE_DIR]:
        base = root / top
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if rel in {
                MANIFEST_REL,
                COMPLETION_MARKER_REL,
            }:
                continue
            if path.name.endswith(("-wal", "-shm")):
                continue
            rows.append({"File": rel.as_posix(), "Size_bytes": path.stat().st_size, "SHA256": sha256_file(path)})
    write_tsv(root / MANIFEST_REL, ["File", "Size_bytes", "SHA256"], rows)


def add_update_change(state: StateDB, entity_type: str, species: str | None, taxid: int | None, assembly: str | None, change: str, reason: str) -> None:
    state.conn.execute(
        "INSERT INTO update_changes(entity_type,species,taxid,assembly,change,reason) VALUES(?,?,?,?,?,?)",
        (entity_type,species,taxid,assembly,change,reason),
    )


def classify_update(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
    pools: Mapping[int, list[AssemblyRecord]],
    old: OldReference,
) -> tuple[dict[int, set[str]], dict[int, list[AssemblyRecord]]]:
    """Provisionally classify eligible assemblies for reuse or new processing.

    This classification is used to plan update processing and to reconstruct that
    work safely during --resume. The final public KEPT/ADDED/UPDATED/REMOVED
    classification is reconciled later, after protein availability and final
    configured biological assembly-selection are known.
    """
    old_assemblies = old.assemblies()
    current_rows = {row["accession"]: row for row in state.conn.execute("SELECT * FROM assemblies")}
    current_eligible = {a.accession: a for records in pools.values() for a in records}
    reusable: dict[int, set[str]] = defaultdict(set)
    to_process: dict[int, list[AssemblyRecord]] = defaultdict(list)

    # Count current inventory records per species for console progress.
    # Assembly records may be interleaved between species, so we cannot
    # assume that consecutive rows always belong to the same species.
    remaining_by_species: dict[int, int] = {}

    for row in current_rows.values():
        taxid = int(row["species_taxid"])

        remaining_by_species[taxid] = (
            remaining_by_species.get(taxid, 0) + 1
        )

    # Report only species with eligible candidates, matching the scope
    # of the protein-processing progress displayed during --create.
    total_species_to_compare = sum(
        bool(pools.get(taxid))
        for taxid in remaining_by_species
    )

    compared_species = 0

    old_species = old.species_taxids()
    current_species = set(species_map)
    with state.conn:
        for taxid in sorted(current_species | old_species):
            if taxid in current_species and taxid not in old_species:
                s = species_map[taxid]
                add_update_change(state, "SPECIES", s.species, taxid, None, "ADDED", "NEW_CURRENT_SPECIES_SET")
            elif taxid in old_species and taxid not in current_species:
                old_row = old.conn.execute("SELECT species FROM species WHERE taxid=?", (taxid,)).fetchone()
                add_update_change(state, "SPECIES", old_row["species"] if old_row else None, taxid, None, "REMOVED", "NO_LONGER_IN_CURRENT_SPECIES_SET")
            else:
                s = species_map[taxid]
                add_update_change(state, "SPECIES", s.species, taxid, None, "KEPT", "CURRENT_SPECIES_SET")

        for accession, current in current_rows.items():
            old_row = old_assemblies.get(accession)
            eligible = accession in current_eligible
            old_selected = bool(old_row["selected"]) if old_row is not None else False
            old_available = bool(old_row["protein_fasta_available"]) if old_row is not None and old_row["protein_fasta_available"] is not None else False

            if eligible:
                a = current_eligible[accession]
                if old_row is not None and old_selected and old_available and old_row["fingerprint"] == current["fingerprint"] and old.has_sources(accession):
                    reusable[a.species_taxid].add(accession)
                    state.conn.execute(
                        """UPDATE assemblies SET protein_fasta_available=1,protein_count=?,protein_fasta_sha256=?,ncbi_package_md5=?,
                               final_status='REUSABLE',final_reason='UNCHANGED_SOURCE',update_change='KEPT',update_reason='UNCHANGED'
                           WHERE accession=?""",
                        (old_row["protein_count"],old_row["protein_fasta_sha256"],old_row["ncbi_package_md5"],accession),
                    )
                    add_update_change(state, "ASSEMBLY", a.species, a.species_taxid, accession, "KEPT", "UNCHANGED")
                else:
                    if old_row is None:
                        change, reason = "ADDED", "NEW_IN_NCBI"
                    elif old_selected and old_row["fingerprint"] != current["fingerprint"]:
                        change, reason = "UPDATED", "ASSEMBLY_OR_ANNOTATION_FINGERPRINT_CHANGED"
                    elif not old_selected:
                        change, reason = "ADDED", "NEWLY_ELIGIBLE_OR_PREVIOUSLY_UNSELECTED"
                    else:
                        change, reason = "UPDATED", "SOURCE_NOT_SAFELY_REUSABLE"
                    a.update_change, a.update_reason = change, reason
                    to_process[a.species_taxid].append(a)
                    state.conn.execute(
                        "UPDATE assemblies SET update_change=?,update_reason=? WHERE accession=?",
                        (change,reason,accession),
                    )
                    add_update_change(state, "ASSEMBLY", a.species, a.species_taxid, accession, change, reason)
            elif old_row is not None and old_selected:
                add_update_change(
                    state, "ASSEMBLY", old_row["species"], int(old_row["species_taxid"]), accession,
                    "REMOVED", str(current["final_reason"] or "NO_LONGER_ELIGIBLE"),
                )

            # This current assembly has now been checked. When it is the
            # last inventory record for an eligible species, report that
            # species' provisional comparison results.
            taxid = int(current["species_taxid"])
            remaining_by_species[taxid] -= 1

            if remaining_by_species[taxid] == 0 and pools.get(taxid):
                compared_species += 1

                info(
                    f"Species {compared_species}/{total_species_to_compare} "
                    f"CHECKED (provisional): {current['species']}\n"
                    f"       Eligible={len(pools.get(taxid, []))} | "
                    f"Reusable={len(reusable.get(taxid, set()))} | "
                    f"New/changed={len(to_process.get(taxid, []))}"
                )

        # Previously selected accessions that are absent from the current assembly
        # table are removals, but there are two biologically different reasons why
        # this can happen.
        #
        # 1. The species itself is no longer part of the current curated species
        #    set. In that case no current assembly inventory is built for the
        #    species, so we must NOT claim that NCBI stopped returning the assembly.
        #
        # 2. The species is still retained, but the previously selected accession
        #    is not present in its current NCBI assembly inventory. This can cover
        #    accessions that NCBI no longer returns for the retained species.
        #
        # Run this loop ONCE after checking the current inventory, not once
        # for every current assembly.
        for accession, old_row in old_assemblies.items():

            if bool(old_row["selected"]) and accession not in current_rows:

                old_taxid = int(old_row["species_taxid"])

                if old_taxid not in current_species:
                    removal_reason = "SPECIES_NO_LONGER_IN_CURRENT_SPECIES_SET"

                else:
                    removal_reason = "NOT_IN_CURRENT_NCBI_INVENTORY"

                add_update_change(
                    state,
                    "ASSEMBLY",
                    old_row["species"],
                    old_taxid,
                    accession,
                    "REMOVED",
                    removal_reason,
                )

    for taxid in to_process:
        to_process[taxid].sort(key=quality_rank_key)
    return reusable, to_process


def reconcile_final_assembly_update_changes(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
    old: OldReference,
) -> None:
    """Rebuild assembly-level update changes from the final reference selection.

    The initial update classification is deliberately provisional. It is needed
    before protein processing so FungiFunk can decide which assemblies may be
    reused and which assemblies need new protein retrieval.

    After protein availability and -configured biological assembly-selection have been
    resolved, the final public update report must instead describe the actual
    difference between the previous reference and the newly constructed
    reference.

    The provisional classification stored in assemblies.update_change and
    assemblies.update_reason is intentionally left unchanged because
    reconstruct_update_sets() uses those fields to recover interrupted update
    processing safely.

    Only ASSEMBLY rows in update_changes are rebuilt here. Species-level rows
    describe changes in the curated species set and are preserved.
    """

    old_assemblies = old.assemblies()

    current_rows = {
        row["accession"]: row
        for row in state.conn.execute(
            "SELECT * FROM assemblies"
        )
    }

    current_species = set(species_map)

    # Rebuilding the complete assembly-level change set inside one transaction
    # makes this operation idempotent and safe to repeat after interruption.
    with state.conn:

        state.conn.execute(
            "DELETE FROM update_changes WHERE entity_type='ASSEMBLY'"
        )

        all_accessions = sorted(
            set(old_assemblies)
            | set(current_rows)
        )

        for accession in all_accessions:

            old_row = old_assemblies.get(
                accession
            )

            current_row = current_rows.get(
                accession
            )

            old_selected = (
                old_row is not None
                and bool(old_row["selected"])
            )

            current_selected = (
                current_row is not None
                and bool(current_row["selected"])
            )

            # --------------------------------------------------------------
            # Present in both final references.
            # --------------------------------------------------------------
            if old_selected and current_selected:

                preliminary_change = (
                    str(current_row["update_change"])
                    if current_row["update_change"] is not None
                    else ""
                )

                preliminary_reason = (
                    str(current_row["update_reason"])
                    if current_row["update_reason"] is not None
                    else ""
                )

                if (
                    preliminary_change == "KEPT"
                    and preliminary_reason == "UNCHANGED"
                ):

                    change = "KEPT"
                    reason = "UNCHANGED"

                else:

                    # The assembly contributed to both references, but its
                    # current source could not simply be reused unchanged.
                    change = "UPDATED"
                    reason = (
                        preliminary_reason
                        or "SOURCE_REPROCESSED"
                    )

                add_update_change(
                    state,
                    "ASSEMBLY",
                    current_row["species"],
                    int(current_row["species_taxid"]),
                    accession,
                    change,
                    reason,
                )

                continue

            # --------------------------------------------------------------
            # Newly contributing to the reference.
            # --------------------------------------------------------------
            if not old_selected and current_selected:

                reason = (
                    str(current_row["update_reason"])
                    if current_row["update_reason"] is not None
                    else "NEWLY_SELECTED"
                )

                add_update_change(
                    state,
                    "ASSEMBLY",
                    current_row["species"],
                    int(current_row["species_taxid"]),
                    accession,
                    "ADDED",
                    reason,
                )

                continue

            # --------------------------------------------------------------
            # Contributed previously but no longer contributes.
            # --------------------------------------------------------------
            if old_selected and not current_selected:

                if current_row is None:

                    old_taxid = int(
                        old_row["species_taxid"]
                    )

                    # If the species itself disappeared from the current
                    # curated species set, no current NCBI inventory was built
                    # for it. Do not incorrectly claim that NCBI stopped
                    # returning this accession.
                    if old_taxid not in current_species:

                        reason = (
                            "SPECIES_NO_LONGER_IN_CURRENT_SPECIES_SET"
                        )

                    else:

                        # The species is still retained and its current
                        # inventory was queried, but this previously selected
                        # accession is absent from that inventory.
                        reason = (
                            "NOT_IN_CURRENT_NCBI_INVENTORY"
                        )

                    species = old_row["species"]
                    taxid = old_taxid

                else:

                    # The assembly is still represented in the current
                    # inventory, so preserve the most specific final reason
                    # already determined by eligibility, QC, protein
                    # availability or the biological assembly-selection policy.
                    reason = (
                        str(current_row["final_reason"])
                        if current_row["final_reason"] is not None
                        else (
                            str(current_row["update_reason"])
                            if current_row["update_reason"] is not None
                            else "NO_LONGER_SELECTED"
                        )
                    )

                    species = current_row["species"]
                    taxid = int(
                        current_row["species_taxid"]
                    )

                add_update_change(
                    state,
                    "ASSEMBLY",
                    species,
                    taxid,
                    accession,
                    "REMOVED",
                    reason,
                )

                continue

            # If the assembly did not contribute to either final reference,
            # there is no reference-to-reference change to report.


########################################################################################################
# Worker orchestration
########################################################################################################

def run_species_workers(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
    pools: Mapping[int, list[AssemblyRecord]],
    config: BuildConfig,
    work_root: Path,
    resource_plan: ResourcePlan,
    *,
    selection_constraints_by_species: Mapping[int, AssemblySelectionConstraints] | None = None,
    reusable_accessions_by_species: Mapping[int, set[str]] | None = None,
    update_mode: bool = False,
    recovery_only: bool = False,
) -> dict[int, NCBIRecoverySummary]:
    if recovery_only and update_mode:
        raise DataIntegrityError(
            "Manual NCBI recovery cannot run with update_mode=True."
        )

    pending_manual = (
        pending_manual_ncbi_recovery_accessions(state)
        if recovery_only
        else {}
    )

    if recovery_only and not pending_manual:
        info(
            "Manual NCBI recovery: no unresolved eligible NCBI "
            "retrieval failures were found."
        )
        return {}

    ready_dir = work_root / TMP_DIR / "prepared_species"
    ready_dir.mkdir(parents=True, exist_ok=True)
    temp_base = Path(config.tmpdir) if config.tmpdir else work_root / TMP_DIR / "workers"
    temp_base.mkdir(parents=True, exist_ok=True)

    # Species with no candidate assembly are still valid members of the species
    # master and should be rechecked on future updates.
    for taxid, species in species_map.items():
        if not recovery_only and not pools.get(taxid):
            with state.conn:
                state.conn.execute("UPDATE species SET state='PREPARED' WHERE taxid=?", (taxid,))

    manager = mp.Manager()

    # All species workers share one console lock. It serializes only the brief
    # moment in which a complete message is written to stdout.
    console_lock = manager.Lock()

    # The request-start limiter enforces the configured NCBI rate independently
    # from the number of local species workers.
    rps = config.ncbi_request_rate

    limiter = SharedLimiterProxy(
        manager.Lock(),
        manager.Value("d", 0.0),
        1.0 / rps,
    )

    # Local worker capacity and NCBI concurrency are different resources.
    #
    # A machine may safely run many local workers according to CPU/RAM/disk,
    # while only a small number of external datasets processes should contact
    # NCBI simultaneously.
    #
    # Never allocate more NCBI slots than local workers could actually use.
    ncbi_concurrency_limit = min(
        NCBI_MAX_CONCURRENT_COMMANDS,
        resource_plan.used_workers,
    )

    # Manager.BoundedSemaphore creates one semaphore shared by every process in
    # the ProcessPoolExecutor. BoundedSemaphore additionally protects against an
    # accidental release imbalance by preventing the counter from exceeding its
    # original capacity.
    ncbi_concurrency = SharedConcurrencyLimiterProxy(
        manager.BoundedSemaphore(ncbi_concurrency_limit)
    )

    jobs: list[WorkerJob] = []

    recovery_jobs: list[WorkerJob] = []

    recovery_summaries: dict[
        int,
        NCBIRecoverySummary,
    ] = {}

    recovery_ready_dir = (
        work_root
        / TMP_DIR
        / "ncbi_recovery_prepared_species"
    )

    recovery_ready_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # CREATE and UPDATE use the same biological selection policy while
    # processing candidates. UPDATE can also reuse unchanged assemblies
    # without downloading their proteins again.
    worker_selection_policy = assembly_selection_policy(config)

    worker_selection_constraints = (
        selection_constraints_by_species
        if selection_constraints_by_species is not None
        else {}
    )

    for taxid, species in sorted(
        species_map.items(),
        key=lambda kv: (
            kv[1].species,
            kv[0],
        ),
    ):
        assemblies = pools.get(
            taxid,
            [],
        )

        if not assemblies:
            continue

        job = WorkerJob(
            species=species,
            assemblies=assemblies,

            # Both CREATE and UPDATE select assemblies progressively using
            # the same biological rules and explicit target constraints.
            selection_policy=worker_selection_policy,

            selection_constraints=(
                worker_selection_constraints.get(
                    taxid,
                    AssemblySelectionConstraints(),
                )
            ),

            # UPDATE can accept these assemblies without downloading them.
            # CREATE has no reusable assemblies.
            reusable_accessions=frozenset(
                reusable_accessions_by_species.get(taxid, set())
                if reusable_accessions_by_species is not None
                else ()
            ),

            temp_base=str(
                temp_base
            ),

            ready_dir=str(
                ready_dir
            ),

            shared_limiter=limiter,

            shared_ncbi_concurrency=(
                ncbi_concurrency
            ),

            output_root=str(
                work_root
            ),

            console_lock=console_lock,
            verbose=config.verbose,
            timestamps=config.timestamps,
        )

        if recovery_only:
            requested = set(pending_manual.get(taxid, ()))

            # Only previously eligible assembly candidates may be retried.
            retry_accessions = [
                assembly.accession
                for assembly in assemblies
                if assembly.accession in requested
            ]

            if retry_accessions:
                recovery_jobs.append(
                    build_ncbi_recovery_job(
                        job,
                        retry_accessions,
                    )
                )

            # Skip first-pass checkpoint recovery, species state changes,
            # and ordinary protein-processing worker scheduling.
            continue

        db_row = state.conn.execute(
            """
            SELECT state
            FROM species
            WHERE taxid=?
            """,
            (taxid,),
        ).fetchone()

        species_prepared = bool(
            db_row is not None
            and db_row["state"] == "PREPARED"
        )

        # A worker may have committed its normal .ready files immediately
        # before the coordinator was interrupted. Recover that complete
        # first-pass result before deciding whether NCBI recovery is pending.
        if not species_prepared:

            if recover_ready_species(
                state,
                species,
                assemblies,
                ready_dir,
            ):
                species_prepared = True

        if species_prepared:

            # A recovery worker may itself have completed immediately before
            # coordinator interruption. Its separate .ready product is the
            # durable evidence that the one automatic recovery pass finished.
            #
            # Import it idempotently and never issue another automatic NCBI
            # retry for those assemblies.
            recovered_checkpoint = (
                recover_ncbi_recovery_ready_species(
                    state,
                    species,
                    assemblies,
                    recovery_ready_dir,
                )
            )

            if recovered_checkpoint is not None:
                continue

            # No completed recovery checkpoint exists. Reconstruct a pending
            # automatic recovery queue from the persistent first-pass failures.
            retry_accessions = (
                retryable_ncbi_failed_accessions_from_state(
                    state,
                    taxid,
                    [
                        assembly.accession
                        for assembly in assemblies
                    ],
                )
            )

            if retry_accessions:
                recovery_jobs.append(
                    build_ncbi_recovery_job(
                        job,
                        retry_accessions,
                    )
                )

            continue

        with state.conn:
            state.conn.execute(
                """
                UPDATE species
                SET state='PROCESSING'
                WHERE taxid=?
                """,
                (taxid,),
            )

        jobs.append(
            job
        )

    if recovery_only:
        queued = {
            (job.species.taxid, assembly.accession)
            for job in recovery_jobs
            for assembly in job.assemblies
        }

        skipped = [
            (taxid, accession)
            for taxid, accessions in pending_manual.items()
            for accession in accessions
            if (taxid, accession) not in queued
        ]

        if skipped:
            warn(
                "Manual NCBI recovery skipped "
                f"{len(skipped)} recorded failure(s) outside the "
                "persisted eligible assembly candidate pools."
            )

    if (
        not jobs
        and not recovery_jobs
    ):
        # No workers are needed when there are no eligible species left
        # to process or all required checkpoints are already complete.
        if update_mode:
            info(
                "Species processing: 0 species queued; no protein worker "
                "jobs remain."
            )

        manager.shutdown()

        return {}
    
    worker_total = len(jobs)

    for worker_id, job in enumerate(jobs, start=1):
        job.worker_id = worker_id
        job.worker_total = worker_total

    # Report local processing parallelism separately from external NCBI
    # concurrency so users can see that these are intentionally independent.
    effective_concurrency = min(
        len(jobs),
        resource_plan.used_workers,
    )

    effective_ncbi_concurrency = min(
        len(jobs),
        ncbi_concurrency_limit,
    )

    if jobs:
        info(
            f"Species processing: {len(jobs)} species queued; "
            f"up to {effective_concurrency} local workers will run concurrently."
        )

        info(
            f"NCBI access: up to {effective_ncbi_concurrency} concurrent "
            f"Datasets commands; request-start limit "
            f"{config.ncbi_request_rate:g}/s."
        )

    elif recovery_only:
        info(
            "Manual NCBI recovery: no first-pass workers scheduled; "
            f"{len(recovery_jobs)} recovery species jobs queued."
        )

    else:
        info(
            "Species processing: 0 first-pass species jobs remain; "
            "resuming pending automatic NCBI recovery."
        )

    try:
        with ProcessPoolExecutor(max_workers=resource_plan.used_workers) as executor:
            future_map = {executor.submit(process_species_worker, job): job for job in jobs}
            completed_species = 0
            for future in as_completed(future_map):
                job = future_map[future]
                try:
                    result = future.result()
                except FungiFunkError:
                    for other in future_map:
                        other.cancel()
                    raise
                except Exception as exc:
                    for other in future_map:
                        other.cancel()
                    raise DataIntegrityError(f"Species worker failed for {job.species.species}: {exc}") from exc
                import_species_worker_result(
                    state,
                    result,
                    {
                        assembly.accession: assembly
                        for assembly in job.assemblies
                    },
                )

                if result.retryable_ncbi_failures:
                    recovery_jobs.append(
                        build_ncbi_recovery_job(
                            job,
                            result.retryable_ncbi_failures,
                        )
                    )

                selected_accessions = tuple(
                    str(row["accession"])
                    for row in state.conn.execute(
                        """
                        SELECT accession
                        FROM assemblies
                        WHERE species_taxid=?
                          AND selected=1
                        ORDER BY accession
                        """,
                        (result.species_taxid,),
                    )
                )

                completed_species += 1

                if config.verbose:
                    if (
                        result.successful_assemblies > 0
                        or (update_mode and result.reused_assemblies > 0)
                    ):
                        worker_finished_block(
                            job,
                            level="INFO",
                            attempted=result.attempted_assemblies,
                            accepted=result.successful_assemblies,
                            proteins=result.total_source_occurrences,
                            unique=result.unique_sequences,
                            representation_parts=worker_representation_summary_parts(
                                job,
                                result,
                            ),
                            update_mode=update_mode,
                            reused=result.reused_assemblies,
                        )

                    else:
                        worker_finished_block(
                            job,
                            level="WARNING",
                            attempted=result.attempted_assemblies,
                            accepted=0,
                            proteins=0,
                            unique=0,
                            failure_reasons=result.failure_reasons,
                            representation_parts=worker_representation_summary_parts(
                                job,
                                result,
                            ),
                            update_mode=update_mode,
                            reused=result.reused_assemblies,
                        )

                else:
                    progress_width = len(str(worker_total))

                    representation_parts = (
                        worker_representation_summary_parts(
                            job,
                            result,
                        )
                    )

                    representation_line = (
                        "\n       "
                        + " | ".join(
                            representation_parts
                        )
                        if representation_parts
                        else ""
                    )

                    if (
                        result.successful_assemblies > 0
                        or (update_mode and result.reused_assemblies > 0)
                    ):
                        if update_mode:
                            # UPDATE reports new processing and reuse separately.
                            # The final selected assemblies are reported after
                            # automatic NCBI recovery and final selection.
                            info(
                                f"Species {completed_species:>{progress_width}}/{worker_total} "
                                f"{'EVALUATED':<12}: {result.species}"
                                f"{representation_line}\n"
                                f"       Assemblies attempted={result.attempted_assemblies} | "
                                f"Newly processed={result.successful_assemblies} | "
                                f"Reused={result.reused_assemblies} | "
                                f"New protein records={result.total_source_occurrences:,} | "
                                f"New unique sequences={result.unique_sequences:,}"
                            )

                        else:
                            info(
                                f"Species {completed_species:>{progress_width}}/{worker_total} "
                                f"{'INCLUDED':<12}: {result.species}"
                                f"{representation_line}\n"
                                f"       Assemblies attempted={result.attempted_assemblies} | "
                                f"Accepted={result.successful_assemblies} | "
                                f"Proteins={result.total_source_occurrences:,} | "
                                f"Unique={result.unique_sequences:,}\n"
                                f"       Assemblies included ({len(selected_accessions)}): "
                                f"{', '.join(selected_accessions)}"
                            )

                    else:
                        status = "EVALUATED" if update_mode else "NOT INCLUDED"

                        success_label = (
                            "Newly processed"
                            if update_mode
                            else "Accepted"
                        )

                        reused_fragment = " | Reused=0" if update_mode else ""

                        protein_label = (
                            "New protein records"
                            if update_mode
                            else "Proteins"
                        )

                        unique_label = (
                            "New unique sequences"
                            if update_mode
                            else "Unique"
                        )

                        failure_summary = " | ".join(
                            f"{reason}={count}"
                            for reason, count in sorted(result.failure_reasons.items())
                        )

                        message = (
                            f"Species {completed_species:>{progress_width}}/{worker_total} "
                            f"{status:<12}: "
                            f"{result.species}"
                            f"{representation_line}\n"
                            f"       Assemblies attempted={result.attempted_assemblies} | "
                            f"{success_label}=0{reused_fragment} | "
                            f"{protein_label}=0 | "
                            f"{unique_label}=0"
                        )

                        if failure_summary:
                            message += f" | {failure_summary}"

                        info(message)

        if recovery_jobs:
            recovery_assembly_total = sum(
                len(job.assemblies)
                for job in recovery_jobs
            )

            recovery_species_total = len(
                recovery_jobs
            )

            print(flush=True)

            if recovery_only:
                info(
                    "Starting manual restorative recovery for assemblies "
                    "with unresolved NCBI retrieval failures."
                )
            else:
                info(
                    "Starting automatic restorative recovery for assemblies "
                    "affected by NCBI retrieval failures."
                )

            info(
                f"Recovery queue: {recovery_assembly_total:,} assemblies "
                f"across {recovery_species_total:,} species."
            )

            for recovery_job in recovery_jobs:

                species_taxid = (
                    recovery_job.species.taxid
                )

                constraints = (
                    selection_constraints_by_species.get(
                        species_taxid,
                        AssemblySelectionConstraints(),
                    )
                    if selection_constraints_by_species is not None
                    else AssemblySelectionConstraints()
                )

                recovery_summaries[
                    species_taxid
                ] = NCBIRecoverySummary(
                    species_taxid=species_taxid,
                    species=(
                        recovery_job.species.species
                    ),
                    retried_accessions=tuple(
                        sorted(
                            assembly.accession
                            for assembly
                            in recovery_job.assemblies
                        )
                    ),
                    recovered_accessions=(),
                    selected_before_recovery=(
                        resolve_known_available_selection(
                            state,
                            species_taxid,
                            config,
                            constraints,
                        )
                    ),
                )

            for recovery_worker_id, recovery_job in enumerate(
                recovery_jobs,
                start=1,
            ):
                recovery_job.worker_id = recovery_worker_id
                recovery_job.worker_total = recovery_species_total

            recovered_assemblies = 0
            still_failed_assemblies = 0

            with ProcessPoolExecutor(
                max_workers=resource_plan.used_workers
            ) as executor:

                recovery_future_map = {
                    executor.submit(
                        process_species_worker,
                        recovery_job,
                    ): recovery_job
                    for recovery_job in recovery_jobs
                }

                for future in as_completed(
                    recovery_future_map
                ):
                    recovery_job = recovery_future_map[
                        future
                    ]

                    try:
                        recovery_result = future.result()

                    except FungiFunkError:
                        for other in recovery_future_map:
                            other.cancel()
                        raise

                    except Exception as exc:
                        for other in recovery_future_map:
                            other.cancel()

                        raise DataIntegrityError(
                            "NCBI recovery worker failed for "
                            f"{recovery_job.species.species}: {exc}"
                        ) from exc

                    import_species_worker_result(
                        state,
                        recovery_result,
                        {
                            assembly.accession: assembly
                            for assembly
                            in recovery_job.assemblies
                        },
                        replace_species_data=False,
                    )

                    recovered_accessions = tuple(
                        sorted(
                            assembly.accession
                            for assembly
                            in recovery_job.assemblies
                            if bool(
                                state.conn.execute(
                                    """
                                    SELECT protein_fasta_available
                                    FROM assemblies
                                    WHERE accession=?
                                    """,
                                    (
                                        assembly.accession,
                                    ),
                                ).fetchone()[
                                    "protein_fasta_available"
                                ]
                            )
                        )
                    )

                    recovery_summaries[
                        recovery_job.species.taxid
                    ].recovered_accessions = (
                        recovered_accessions
                    )

                    recovered_assemblies += (
                        recovery_result.successful_assemblies
                    )

                    still_failed_assemblies += (
                        recovery_result.attempted_assemblies
                        - recovery_result.successful_assemblies
                    )

            recovery_label = (
                "Manual NCBI recovery"
                if recovery_only
                else "Automatic restorative recovery"
            )

            info(
                f"{recovery_label} pass complete: "
                f"{recovered_assemblies:,}/"
                f"{recovery_assembly_total:,} assemblies recovered; "
                f"{still_failed_assemblies:,} still unavailable."
            )

    finally:
        manager.shutdown()

    return recovery_summaries


def report_unresolved_ncbi_recovery_warning(
    state: StateDB,
    recovery_species_taxids: set[int],
) -> None:
    """Warn about unresolved NCBI failures after final selection."""

    if not recovery_species_taxids:
        return

    retryable_reasons = (
        "NCBI_REQUEST_TIMEOUT",
        "NCBI_RATE_LIMITED",
        "NCBI_GATEWAY_ERROR",
        "NCBI_REQUEST_FAILED",
    )

    failure_rows = state.conn.execute(
        """
        SELECT DISTINCT
            a.species_taxid,
            a.accession
        FROM failed_downloads AS f
        JOIN assemblies AS a
          ON a.accession = f.assembly
        WHERE f.reason IN (?, ?, ?, ?)
        """,
        retryable_reasons,
    ).fetchall()

    unresolved = {
        (int(row[0]), str(row[1]))
        for row in failure_rows
        if int(row[0]) in recovery_species_taxids
    }

    if not unresolved:
        return

    affected_species = {
        taxid
        for taxid, _accession in unresolved
    }

    represented_species = {
        int(row[0])
        for row in state.conn.execute(
            """
            SELECT DISTINCT species_taxid
            FROM assemblies
            WHERE selected = 1
              AND protein_fasta_available = 1
            """
        ).fetchall()
    }

    unrepresented_affected = (
        affected_species - represented_species
    )

    represented_affected = (
        affected_species & represented_species
    )

    unresolved_count = len({
        accession
        for _taxid, accession in unresolved
    })

    if unrepresented_affected:
        warn(
            "Automatic NCBI recovery left "
            f"{unresolved_count:,} assembly retrieval failures unresolved.\n"
            f"         {len(unrepresented_affected):,} affected species "
            "have no usable selected proteins and still have unresolved "
            "NCBI retrieval failures.\n"
            f"         {len(represented_affected):,} other affected species "
            "remain represented by usable assemblies.\n"
            "         Unresolved NCBI failures have been preserved "
            "for future retry."
        )
    else:
        warn(
            "Automatic NCBI recovery left "
            f"{unresolved_count:,} assembly retrieval failures unresolved.\n"
            "         All affected species remain represented "
            "by at least one usable assembly.\n"
            "         Unresolved NCBI failures have been preserved "
            "for future retry."
        )


@dataclasses.dataclass(frozen=True)
class ManualNCBIRecoveryResult:
    """Context retained for finalizing a manually recovered reference."""

    recovered_count: int
    resource_plan: ResourcePlan | None
    explicit_targets: tuple[ResolvedExplicitTarget, ...]

    @property
    def changed(self) -> bool:
        return self.recovered_count > 0


def run_manual_ncbi_recovery_processing(
    state: StateDB,
    config: BuildConfig,
    work_root: Path,
    source_root: Path,
) -> ManualNCBIRecoveryResult:
    """Recover pending failures and retain finalization context.

    The caller must rebuild and publish only when result.changed is True.
    """
    pending = pending_manual_ncbi_recovery_accessions(state)

    if not pending:
        info(
            "Manual NCBI recovery: no unresolved eligible NCBI "
            "retrieval failures remain."
        )
        return ManualNCBIRecoveryResult(
            recovered_count=0,
            resource_plan=None,
            explicit_targets=(),
        )

    resolved_targets: list[ResolvedExplicitTarget] = []

    species_map, full_pools, constraints = (
        load_manual_ncbi_recovery_selection_context(
            state,
            work_root,
            resolved_targets_out=resolved_targets,
        )
    )

    eligible = {
        (taxid, assembly.accession)
        for taxid, assemblies in full_pools.items()
        for assembly in assemblies
    }

    actionable = {
        (taxid, accession)
        for taxid, accessions in pending.items()
        for accession in accessions
        if (taxid, accession) in eligible
    }

    if not actionable:
        warn(
            "Manual NCBI recovery found persistent NCBI failures, "
            "but none belong to the saved eligible assembly pools. "
            "No recovery downloads or output reconstruction will be performed."
        )
        return False

    info(
        "Manual NCBI recovery: "
        f"{len(actionable):,} eligible failed assemblies "
        "identified from the completed reference."
    )

    print(flush=True)

    print_section_banner("RESOURCE ASSESSMENT AND WORKER PLANNING")

    temp_base = (
        Path(config.tmpdir)
        if config.tmpdir
        else work_root / TMP_DIR / "workers"
    )

    plan = calculate_resource_plan(
        config,
        work_root,
        temp_base,
        source_root,
    )

    write_resource_usage(
        work_root / REPORTS_DIR / "resource_usage.tsv",
        plan,
        config,
    )

    info(
        "Manual NCBI recovery resource plan: "
        f"{plan.used_workers} worker(s) allocated; "
        "first-pass processing skipped."
    )

    print(flush=True)
    state.set_global_state("PROTEIN_RETRIEVAL_AND_PROCESSING")

    recovery_summaries = run_species_workers(
        state,
        species_map,
        full_pools,
        config,
        work_root,
        plan,
        selection_constraints_by_species=constraints,
        recovery_only=True,
    )

    recovered = sum(
        len(summary.recovered_accessions)
        for summary in recovery_summaries.values()
    )

    if recovered == 0:
        info(
            "Manual NCBI recovery did not recover any assemblies. "
            "The original reference will be retained without "
            "rebuilding final outputs."
        )
        return ManualNCBIRecoveryResult(
            recovered_count=0,
            resource_plan=plan,
            explicit_targets=tuple(resolved_targets),
        )

    info(
        f"Manual NCBI recovery obtained {recovered:,} assemblies. "
        "Recalculating final biological selection."
    )

    finalize_manual_ncbi_recovery_selection(
        state,
        species_map,
        full_pools,
        config,
        selection_constraints_by_species=constraints,
    )

    validate_manual_ncbi_selected_provenance(state)

    state.set_global_state(
        "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE"
    )

    report_ncbi_recovery_selection_updates(
        state,
        recovery_summaries,
    )

    return ManualNCBIRecoveryResult(
        recovered_count=recovered,
        resource_plan=plan,
        explicit_targets=tuple(resolved_targets),
    )


def validate_manual_ncbi_selected_provenance(
    state: StateDB,
) -> None:
    """Require persistent protein provenance for every selected assembly."""
    missing = [
        str(row["accession"])
        for row in state.conn.execute(
            """
            SELECT a.accession
            FROM assemblies AS a
            WHERE a.selected=1
              AND NOT EXISTS (
                  SELECT 1
                  FROM sources AS s
                  WHERE s.assembly=a.accession
              )
            ORDER BY a.accession
            """
        )
    ]

    if missing:
        raise DataIntegrityError(
            "Manual NCBI recovery cannot rebuild final outputs: "
            f"{len(missing)} selected assemblies have no persisted "
            "protein provenance. Examples: "
            + ", ".join(missing[:10])
        )


def finalize_manual_ncbi_recovery_selection(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
    pools: Mapping[int, list[AssemblyRecord]],
    config: BuildConfig,
    *,
    selection_constraints_by_species: Mapping[
        int,
        AssemblySelectionConstraints,
    ] | None = None,
) -> None:
    """Re-select recovered assemblies using the original biological policy.

    The staged SQLite already contains provenance for previously selected
    assemblies. Reuse CREATE final selection rather than UPDATE comparison
    and import logic. Preserve REUSED status for unchanged retained sources.
    """
    previously_reused = [
        str(row["accession"])
        for row in state.conn.execute(
            """
            SELECT accession
            FROM assemblies
            WHERE selected=1
              AND final_status='REUSED'
            ORDER BY accession
            """
        )
    ]

    finalize_create_selection(
        state,
        species_map,
        pools,
        config,
        selection_constraints_by_species=(
            selection_constraints_by_species
        ),
        require_source_provenance=True,
    )

    # CREATE finalization marks all selected candidates as PROCESSED.
    # Preserve the original reuse provenance for assemblies that were
    # already selected and remain selected after manual recovery.
    with state.conn:
        state.conn.executemany(
            """
            UPDATE assemblies
            SET final_status='REUSED'
            WHERE accession=?
              AND selected=1
              AND final_status='PROCESSED'
            """,
            [(accession,) for accession in previously_reused],
        )


def finalize_create_selection(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
    pools: Mapping[int, list[AssemblyRecord]],
    config: BuildConfig,
    *,
    selection_constraints_by_species: Mapping[
        int,
        AssemblySelectionConstraints,
    ] | None = None,
    require_source_provenance: bool = False,
) -> None:
    """Resolve the final CREATE selection from known protein availability."""

    policy = assembly_selection_policy(
        config
    )

    active_constraints_by_species = (
        selection_constraints_by_species
        if selection_constraints_by_species is not None
        else {}
    )

    for taxid in species_map:

        current_pool = pools.get(
            taxid,
            [],
        )

        if not current_pool:
            continue

        usable_accessions: set[str] = set()

        for assembly in current_pool:

            row = state.conn.execute(
                """
                SELECT protein_fasta_available
                FROM assemblies
                WHERE accession=?
                """,
                (assembly.accession,),
            ).fetchone()

            if (
                row is None
                or not bool(row["protein_fasta_available"])
            ):
                continue

            if require_source_provenance:
                has_sources = state.conn.execute(
                    """
                    SELECT 1
                    FROM sources
                    WHERE assembly=?
                    LIMIT 1
                    """,
                    (assembly.accession,),
                ).fetchone()

                if has_sources is None:
                    # A completed reference may retain availability metadata
                    # for candidates whose protein provenance was discarded
                    # after the original final selection.
                    continue

            usable_accessions.add(
                assembly.accession
            )

        resolution = execute_assembly_selection(
            current_pool,
            policy,
            lambda assembly: (
                assembly.accession
                in usable_accessions
            ),
            constraints=(
                active_constraints_by_species.get(
                    taxid,
                    AssemblySelectionConstraints(),
                )
            ),
        )

        selected = (
            resolution.selected_accessions
        )

        with state.conn:

            for assembly in current_pool:

                accession = assembly.accession

                if accession not in usable_accessions:
                    # Failed/unavailable candidates keep their specific
                    # processing reason.
                    continue

                if accession not in selected:

                    state.conn.execute(
                        """
                        DELETE FROM sources
                        WHERE assembly=?
                        """,
                        (accession,),
                    )

                    state.conn.execute(
                        """
                        DELETE FROM protein_warnings
                        WHERE assembly=?
                        """,
                        (accession,),
                    )

                    reason = (
                        resolution.not_selected_reasons.get(
                            accession
                        )
                    )

                    if reason is None:
                        raise DataIntegrityError(
                            "CREATE final selection returned a usable "
                            "unselected candidate without an explicit "
                            "NOT_SELECTED reason: "
                            f"{accession}"
                        )

                    state.conn.execute(
                        """
                        UPDATE assemblies
                        SET selected=0,
                            final_status='NOT_SELECTED',
                            final_reason=?
                        WHERE accession=?
                        """,
                        (
                            reason,
                            accession,
                        ),
                    )

                else:

                    state.conn.execute(
                        """
                        UPDATE assemblies
                        SET selected=1,
                            final_status='PROCESSED',
                            final_reason='PASS'
                        WHERE accession=?
                        """,
                        (accession,),
                    )

            state.conn.execute(
                """
                UPDATE species
                SET state='PREPARED'
                WHERE taxid=?
                """,
                (taxid,),
            )


def selected_assembly_accessions(
    state: StateDB,
    species_taxid: int,
) -> tuple[str, ...]:
    """Return the final selected assembly accessions for one species."""

    return tuple(
        str(row["accession"])
        for row in state.conn.execute(
            """
            SELECT accession
            FROM assemblies
            WHERE species_taxid=?
              AND selected=1
            ORDER BY accession
            """,
            (species_taxid,),
        )
    )


def report_ncbi_recovery_selection_updates(
    state: StateDB,
    recovery_summaries: Mapping[
        int,
        NCBIRecoverySummary,
    ],
    *,
    include_selected_assemblies: bool = True,
) -> None:
    """Report final biological selection after automatic NCBI recovery."""

    if not recovery_summaries:
        return

    ordered_summaries = sorted(
        recovery_summaries.values(),
        key=lambda summary: (
            summary.species,
            summary.species_taxid,
        ),
    )

    total_species = len(
        ordered_summaries
    )

    progress_width = max(
        1,
        len(str(total_species)),
    )

    for index, summary in enumerate(
        ordered_summaries,
        start=1,
    ):
        selected_after = (
            selected_assembly_accessions(
                state,
                summary.species_taxid,
            )
        )

        selected_before_set = set(
            summary.selected_before_recovery
        )

        selected_after_set = set(
            selected_after
        )

        added = tuple(
            sorted(
                selected_after_set
                - selected_before_set
            )
        )

        removed = tuple(
            sorted(
                selected_before_set
                - selected_after_set
            )
        )

        selection_updated = bool(
            added or removed
        )

        retried = len(
            summary.retried_accessions
        )

        recovered = len(
            summary.recovered_accessions
        )

        still_failed = (
            retried - recovered
        )

        lines = [
            (
                f"Species {index:>{progress_width}}/{total_species} "
                f"{'RECOVERY':<12}: "
                f"{summary.species}"
            ),
            (
                "       "
                f"Assemblies retried={retried} | "
                f"Recovered={recovered} | "
                f"Still failed={still_failed}"
            ),
            (
                "       "
                "Final selection="
                + (
                    "UPDATED"
                    if selection_updated
                    else "UNCHANGED"
                )
            ),
        ]

        if include_selected_assemblies:
            lines.append(
                "       "
                f"Assemblies included ({len(selected_after)}): "
                + (
                    ", ".join(selected_after)
                    if selected_after
                    else "none"
                )
            )

        if selection_updated:
            changes: list[str] = []

            if removed:
                changes.append(
                    "removed "
                    + ", ".join(removed)
                )

            if added:
                changes.append(
                    "added "
                    + ", ".join(added)
                )

            lines.append(
                "       Selection change: "
                + " | ".join(changes)
            )

        info(
            "\n".join(lines)
        )


def finalize_update_selection(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
    pools: Mapping[int, list[AssemblyRecord]],
    reusable: Mapping[int, set[str]],
    old: OldReference,
    config: BuildConfig,
    *,
    selection_constraints_by_species: Mapping[
        int,
        AssemblySelectionConstraints,
    ] | None = None,
    report_messages: list[str] | None = None,
) -> None:
    """Resolve the final update selection after protein availability is known.

    Reusable and newly processed candidates are evaluated together using the
    same deterministic biological selection policy used by fresh builds.
    """

    policy = assembly_selection_policy(
        config
    )

    active_constraints_by_species = (
        selection_constraints_by_species
        if selection_constraints_by_species is not None
        else {}
    )

    reportable_taxids = {
        taxid
        for taxid in species_map
        if pools.get(taxid, [])
    }

    total_reported_species = len(
        reportable_taxids
    )

    progress_width = max(
        1,
        len(str(total_reported_species)),
    )

    completed_reported_species = 0

    for taxid, species in species_map.items():

        current_pool = pools.get(
            taxid,
            [],
        )

        usable_accessions: set[str] = set()

        for assembly in current_pool:

            row = state.conn.execute(
                "SELECT protein_fasta_available "
                "FROM assemblies "
                "WHERE accession=?",
                (assembly.accession,),
            ).fetchone()

            if row is None:
                continue

            if (
                assembly.accession
                in reusable.get(taxid, set())
                or bool(
                    row["protein_fasta_available"]
                )
            ):
                usable_accessions.add(
                    assembly.accession
                )

        resolution = execute_assembly_selection(
            current_pool,
            policy,
            lambda assembly: (
                assembly.accession
                in usable_accessions
            ),
            constraints=(
                active_constraints_by_species.get(
                    taxid,
                    AssemblySelectionConstraints(),
                )
            ),
        )

        selected = (
            resolution.selected_accessions
        )

        with state.conn:

            for assembly in current_pool:

                accession = assembly.accession

                if accession not in usable_accessions:
                    # Failed/unavailable candidates already have their specific
                    # processing reason and must not be overwritten by a
                    # selection-depth reason.
                    continue

                if accession not in selected:

                    state.conn.execute(
                        "DELETE FROM sources "
                        "WHERE assembly=?",
                        (accession,),
                    )

                    state.conn.execute(
                        "DELETE FROM protein_warnings "
                        "WHERE assembly=?",
                        (accession,),
                    )

                    reason = (
                        resolution.not_selected_reasons.get(
                            accession
                        )
                    )

                    if reason is None:
                        raise DataIntegrityError(
                            "Assembly selection returned a usable unselected "
                            "candidate without an explicit NOT_SELECTED reason: "
                            f"{accession}"
                        )

                    state.conn.execute(
                        """
                        UPDATE assemblies
                        SET selected=0,
                            final_status='NOT_SELECTED',
                            final_reason=?
                        WHERE accession=?
                        """,
                        (
                            reason,
                            accession,
                        ),
                    )

                else:

                    status = (
                        "REUSED"
                        if accession
                        in reusable.get(
                            taxid,
                            set(),
                        )
                        else "PROCESSED"
                    )

                    state.conn.execute(
                        """
                        UPDATE assemblies
                        SET selected=1,
                            final_status=?,
                            final_reason='PASS'
                        WHERE accession=?
                        """,
                        (
                            status,
                            accession,
                        ),
                    )

        # Reused sources were deliberately not imported before final selection.
        for accession in sorted(
            selected
            & reusable.get(
                taxid,
                set(),
            )
        ):
            old.import_sources_for_assembly(
                state,
                accession,
            )

        with state.conn:
            state.conn.execute(
                "UPDATE species "
                "SET state='PREPARED' "
                "WHERE taxid=?",
                (taxid,),
            )

        if taxid in reportable_taxids:
            completed_reported_species += 1

            representation_parts = (
                representation_summary_parts(
                    policy,
                    eligible_strain_groups=(
                        resolution.eligible_strain_groups
                    ),
                    represented_strain_groups=len(
                        resolution.represented_strains
                    ),
                    eligible_isolate_groups=(
                        resolution.eligible_isolate_groups
                    ),
                    represented_isolate_groups=len(
                        resolution.represented_isolates
                    ),
                    eligible_unassigned_groups=(
                        resolution.eligible_unassigned_groups
                    ),
                    represented_unassigned_groups=len(
                        resolution.represented_unassigned_groups
                    ),
                    species_fallback_used=(
                        resolution.species_fallback_used
                    ),
                )
            )

            status = (
                "INCLUDED"
                if selected
                else "NOT INCLUDED"
            )

            selected_reused = len(
                selected
                & reusable.get(
                    taxid,
                    set(),
                )
            )

            selected_processed = (
                len(selected)
                - selected_reused
            )

            lines = [
                (
                    f"Species "
                    f"{completed_reported_species:>{progress_width}}/"
                    f"{total_reported_species} "
                    f"{status:<12}: "
                    f"{species.species}"
                )
            ]

            if representation_parts:
                lines.append(
                    "       "
                    + " | ".join(
                        representation_parts
                    )
                )

            lines.append(
                "       "
                f"Selected assemblies={len(selected)} | "
                f"Reused={selected_reused} | "
                f"Processed={selected_processed}"
            )

            if selected:
                lines.append(
                    "       "
                    f"Assemblies included ({len(selected)}): "
                    + ", ".join(
                        sorted(selected)
                    )
                )

            message = "\n".join(lines)

            if report_messages is None:
                info(message)
            else:
                report_messages.append(message)

    reconcile_final_assembly_update_changes(
        state,
        species_map,
        old,
    )


########################################################################################################
# Global exact deduplication and final FASTA generation
########################################################################################################

def canonical_store_insert_or_verify(state: StateDB, store: Any, digest: str, sequence: str) -> None:
    row = state.conn.execute(
        "SELECT sequence_length,store_offset FROM canonical_sequences WHERE sequence_sha256=?",
        (digest,),
    ).fetchone()
    if row is None:
        offset = store.tell()
        store.write(sequence.encode("ascii"))
        state.conn.execute(
            "INSERT INTO canonical_sequences(sequence_sha256,sequence_length,store_offset,source_id) VALUES(?,?,?,?)",
            (digest,len(sequence),offset,f"FFSRC_{digest}"),
        )
        return
    existing = read_sequence_from_store(store, int(row["store_offset"]), int(row["sequence_length"]))
    if existing != sequence:
        raise HashCollisionError(
            f"SHA-256 collision detected during global exact deduplication for {digest}. "
            "FungiFunk will not collapse unequal sequences."
        )


def rebuild_global_canonical_store(state: StateDB, work_root: Path, old: OldReference | None) -> None:
    """Build disk-backed global unique sequence store from selected provenance.

    We never load all protein hashes/sequences into RAM. Membership is queried
    through SQLite and sequence bytes live in an append-only canonical file.
    """
    ready_dirs = (
        work_root / TMP_DIR / "prepared_species",
        work_root / TMP_DIR / "ncbi_recovery_prepared_species",
    )

    store_final = work_root / CANONICAL_STORE_REL
    store_part = store_final.with_name(store_final.name + ".part")
    store_final.parent.mkdir(parents=True, exist_ok=True)
    store_part.unlink(missing_ok=True)

    with state.conn:
        state.conn.execute("DELETE FROM canonical_sequences")

    with store_part.open("w+b") as store:
        # First-pass and recovery .ready files may contain proteins from
        # assemblies that are not retained by the final biological selection.
        # Only hashes that still have selected provenance in state.sources enter
        # the global canonical store.
        for ready_dir in ready_dirs:

            if not ready_dir.exists():
                continue

            for fasta_path in sorted(
                ready_dir.glob("Species_*.faa.ready")
            ):
                if fasta_path.stat().st_size == 0:
                    continue

                for rec in stream_fasta(fasta_path):
                    sequence, terminal, _internal = (
                        canonicalize_protein(
                            rec.sequence
                        )
                    )

                    if terminal:
                        raise DataIntegrityError(
                            "Prepared FASTA unexpectedly contains terminal "
                            f"stop: {fasta_path}"
                        )

                    digest = hashlib.sha256(
                        sequence.encode("ascii")
                    ).hexdigest()

                    needed = state.conn.execute(
                        """
                        SELECT 1
                        FROM sources
                        WHERE sequence_sha256=?
                        LIMIT 1
                        """,
                        (digest,),
                    ).fetchone()

                    if needed is None:
                        continue

                    canonical_store_insert_or_verify(
                        state,
                        store,
                        digest,
                        sequence,
                    )

        state.conn.commit()

        # Unchanged assemblies are reused during updates, so they do not produce a
        # new worker FASTA. If one of their protein hashes is not already present in
        # the new canonical store, retrieve its sequence from the verified old store.
        missing_cursor = state.conn.execute(
            """SELECT DISTINCT s.sequence_sha256
               FROM sources s LEFT JOIN canonical_sequences c ON s.sequence_sha256=c.sequence_sha256
               WHERE c.sequence_sha256 IS NULL ORDER BY s.sequence_sha256"""
        )
        for row in missing_cursor:
            digest = row["sequence_sha256"]
            if old is None:
                raise DataIntegrityError(
                    f"Selected provenance references sequence {digest} but no prepared sequence file contains it."
                )
            sequence = old.sequence(digest)
            observed = hashlib.sha256(sequence.encode("ascii")).hexdigest()
            if observed != digest:
                raise DataIntegrityError(f"Source canonical sequence hash mismatch for {digest}.")
            canonical_store_insert_or_verify(state, store, digest, sequence)
        state.conn.commit()
        fsync_file(store)

    atomic_replace(store_part, store_final)
    fsync_dir(store_final.parent)

    missing = state.conn.execute(
        """SELECT COUNT(DISTINCT s.sequence_sha256)
           FROM sources s LEFT JOIN canonical_sequences c ON s.sequence_sha256=c.sequence_sha256
           WHERE c.sequence_sha256 IS NULL"""
    ).fetchone()[0]
    if missing:
        raise DataIntegrityError(f"Global canonical store is missing {missing} selected provenance sequence(s).")


def capped_preview(items: Sequence[str], label: str) -> str:
    clean = [x for x in sorted(set(items)) if normalize_missing(x) is not None]
    if not clean:
        return ""
    shown = clean[:HEADER_PREVIEW_MAX_ITEMS]
    text = f"{label}=" + ",".join(clean_species_for_header(x) for x in shown)
    remaining = len(clean) - len(shown)
    if remaining > 0:
        text += f",...(+{remaining}_more)"
    return text


def header_and_metadata_for_hash(state: StateDB, digest: str, length: int) -> tuple[str, dict[str, Any]]:
    rows = state.conn.execute(
        """SELECT species,species_taxid,tier,assembly,strain,isolate,original_protein_accession
           FROM sources WHERE sequence_sha256=? ORDER BY species,species_taxid,assembly,original_protein_accession,id""",
        (digest,),
    ).fetchall()
    if not rows:
        raise DataIntegrityError(f"Canonical sequence {digest} has no provenance rows.")

    species_values = sorted({row["species"] for row in rows if normalize_missing(row["species"]) is not None})
    assembly_values = sorted({row["assembly"] for row in rows if normalize_missing(row["assembly"]) is not None})
    protein_values = sorted({row["original_protein_accession"] for row in rows if normalize_missing(row["original_protein_accession"]) is not None})
    strain_values = sorted({row["strain"] for row in rows if normalize_missing(row["strain"]) is not None})
    tier_values = sorted({int(row["tier"]) for row in rows})

    assembly_token = assembly_values[0] if len(assembly_values) == 1 else "NONUNIQUE"
    if len(species_values) == 1:
        species_token = clean_species_for_header(species_values[0])
    else:
        species_token = "MULTI_SPECIES"
    protein_token = protein_values[0] if len(protein_values) == 1 else "NONUNIQUE"
    source_id = f"FFSRC_{digest}"
    first_token = f"{assembly_token}|{species_token}|{protein_token}|src={source_id}"

    nonunique = assembly_token == "NONUNIQUE" or species_token == "MULTI_SPECIES" or protein_token == "NONUNIQUE"
    preview_parts: list[str] = []
    if nonunique:
        if len(species_values) > 1:
            preview = capped_preview(species_values, "species")
            if preview:
                preview_parts.append(preview)
        elif len(strain_values) > 1:
            preview = capped_preview(strain_values, "strains")
            if preview:
                preview_parts.append(preview)
        preview_parts.append(f"n_sources={len(rows)}")
    header = first_token + (" " + ";".join(preview_parts) if preview_parts else "")

    meta = {
        "Source_ID": source_id,
        "Sequence_SHA256": digest,
        "Protein_length": length,
        "Multi_species": len(species_values) > 1,
        "Num_species": len(species_values),
        "Num_assemblies": len(assembly_values),
        "Num_strains": len(strain_values),
        "Num_source_occurrences": len(rows),
        "Tier_set": ";".join(map(str, tier_values)),
        "Header_assembly": assembly_token,
        "Header_species": species_token,
        "Header_protein": protein_token,
    }
    return header, meta


def write_final_fasta_and_protein_metadata(
    state: StateDB,
    work_root: Path,
) -> None:
    """
    Write the final protein FASTA and its associated metadata.

    Both files are written to temporary .part paths before being
    promoted to their final names.

    Progress reports count records actually written and do not
    change the underlying sequence or metadata processing.
    """

    fasta_final = work_root / DATABASE_DIR / FINAL_FASTA_NAME
    fasta_part = fasta_final.with_name(fasta_final.name + ".part")

    meta_final = work_root / METADATA_DIR / "fungal_protein_metadata.tsv"
    meta_part = meta_final.with_name(meta_final.name + ".part")

    fasta_final.parent.mkdir(parents=True, exist_ok=True)
    meta_final.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "Source_ID", "Sequence_SHA256", "Protein_length",
        "Multi_species", "Num_species", "Num_assemblies",
        "Num_strains", "Num_source_occurrences", "Tier_set",
        "Header_assembly", "Header_species", "Header_protein",
    ]

    store_path = work_root / CANONICAL_STORE_REL

    # Count canonical sequences before starting.
    # Each one corresponds to one final FASTA record and one metadata row.
    total_proteins = int(
        state.conn.execute(
            "SELECT COUNT(*) FROM canonical_sequences"
        ).fetchone()[0]
    )

    # Report progress at this interval.
    progress_interval = 50_000

    info(
        f"Final FASTA and protein metadata: "
        f"starting 0/{total_proteins:,} proteins."
    )

    # Keep the original temporary-file writing strategy.
    with (
        store_path.open("rb") as store,
        fasta_part.open("w", encoding="utf-8") as fasta,
        meta_part.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as meta_handle,
    ):

        writer = csv.DictWriter(
            meta_handle,
            delimiter="\t",
            fieldnames=fields,
        )

        writer.writeheader()

        # Enumerate records to track the actual number written.
        # Preserve the original SQL ordering.
        rows = state.conn.execute(
            "SELECT * FROM canonical_sequences ORDER BY sequence_sha256"
        )

        for protein_index, row in enumerate(rows, start=1):

            digest = row["sequence_sha256"]
            length = int(row["sequence_length"])

            # Retrieve the canonical amino-acid sequence.
            sequence = read_sequence_from_store(
                store,
                int(row["store_offset"]),
                length,
            )

            # Preserve the original integrity check.
            observed = hashlib.sha256(
                sequence.encode("ascii")
            ).hexdigest()

            if observed != digest:
                raise DataIntegrityError(
                    "Canonical store hash mismatch while writing "
                    f"final FASTA: {digest}"
                )

            # Build the original FASTA header and protein metadata.
            header, meta = header_and_metadata_for_hash(
                state,
                digest,
                length,
            )

            # Write the FASTA record and its corresponding metadata row.
            write_fasta_record(fasta, header, sequence)

            writer.writerow({
                k: value_tsv(meta[k])
                for k in fields
            })

            # Announce completed records, not records merely retrieved.
            # The final message also covers a non-multiple of 50,000.
            if (
                protein_index % progress_interval == 0
                or protein_index == total_proteins
            ):
                info(
                    f"Final FASTA and protein metadata: "
                    f"{protein_index:,}/{total_proteins:,} "
                    "proteins written."
                )

        # Preserve the original durability operations.
        fsync_file(fasta)
        fsync_file(meta_handle)

    # Promote the completed temporary files to their final names.
    atomic_replace(fasta_part, fasta_final)
    atomic_replace(meta_part, meta_final)

    # This message is emitted only after both final files exist.
    info(
        f"Final FASTA and protein metadata complete: "
        f"{total_proteins:,} proteins."
    )


def write_protein_sources(
    state: StateDB,
    work_root: Path,
) -> None:
    """
    Write the provenance records of the final protein reference.

    Each source occurrence receives one TSV row.

    The original SQL ordering, output columns and atomic replacement
    are preserved. Progress reports only add console/log messages.
    """

    path = work_root / METADATA_DIR / "fungal_protein_sources.tsv"
    part = path.with_name(path.name + ".part")

    fields = [
        "Source_ID", "Sequence_SHA256", "Sequence_length",
        "Species", "TaxID", "Tier", "Assembly",
        "Assembly_source", "Paired_accession", "Strain",
        "Isolate", "Original_protein_accession",
        "Original_header", "Original_had_terminal_stop",
        "Has_internal_stop", "Num_internal_stops",
    ]

    # Count all provenance records before writing them.
    # This count includes multiple origins of an identical sequence.
    total_sources = int(
        state.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0]
    )

    progress_interval = 50_000

    info(
        f"Protein provenance: "
        f"starting 0/{total_sources:,} source records."
    )

    # Preserve the original temporary-file writing strategy.
    with part.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:

        writer = csv.DictWriter(
            handle,
            delimiter="\t",
            fieldnames=fields,
        )

        writer.writeheader()

        # Preserve the exact original ordering of source records.
        rows = state.conn.execute(
            """
            SELECT *
            FROM sources
            ORDER BY
                sequence_sha256,
                species,
                species_taxid,
                assembly,
                original_protein_accession,
                id
            """
        )

        # Enumerate successfully processed records for progress tracking.
        for source_index, r in enumerate(rows, start=1):

            # Preserve every original provenance column and conversion.
            writer.writerow({
                "Source_ID": r["source_id"],
                "Sequence_SHA256": r["sequence_sha256"],
                "Sequence_length": r["sequence_length"],
                "Species": r["species"],
                "TaxID": r["species_taxid"],
                "Tier": r["tier"],
                "Assembly": r["assembly"],
                "Assembly_source": value_tsv(r["assembly_source"]),
                "Paired_accession": value_tsv(r["paired_accession"]),
                "Strain": value_tsv(r["strain"]),
                "Isolate": value_tsv(r["isolate"]),
                "Original_protein_accession": value_tsv(
                    r["original_protein_accession"]
                ),
                "Original_header": value_tsv(
                    r["original_header"]
                ),
                "Original_had_terminal_stop": bool_tsv(
                    bool(r["original_had_terminal_stop"])
                ),
                "Has_internal_stop": bool_tsv(
                    bool(r["has_internal_stop"])
                ),
                "Num_internal_stops": r["num_internal_stops"],
            })

            # Print progress after each completed group of source rows.
            if (
                source_index % progress_interval == 0
                or source_index == total_sources
            ):
                info(
                    f"Protein provenance: "
                    f"{source_index:,}/{total_sources:,} "
                    "source records written."
                )

        # Preserve the original durability operation.
        fsync_file(handle)

    # Promote the completed temporary file to its final name.
    atomic_replace(part, path)

    # Announce completion only after the final file is in place.
    info(
        f"Protein provenance complete: "
        f"{total_sources:,} source records."
    )


def validate_final_reference(state: StateDB, work_root: Path) -> None:
    fasta = work_root / DATABASE_DIR / FINAL_FASTA_NAME
    expected_count = int(state.conn.execute("SELECT COUNT(*) FROM canonical_sequences").fetchone()[0])
    if expected_count == 0:
        if fasta.stat().st_size != 0:
            raise DataIntegrityError("Final FASTA is non-empty but canonical sequence count is zero.")
        return

    fasta_iter = stream_fasta(fasta)
    canonical_iter = state.conn.execute("SELECT * FROM canonical_sequences ORDER BY sequence_sha256")
    count = 0
    for expected in canonical_iter:
        try:
            rec = next(fasta_iter)
        except StopIteration as exc:
            raise DataIntegrityError("Final FASTA ended before all canonical sequences were written.") from exc
        count += 1
        seq, terminal, _ = canonicalize_protein(rec.sequence)
        if terminal:
            raise DataIntegrityError("Final FASTA contains a terminal stop marker after canonicalization policy.")
        digest = hashlib.sha256(seq.encode("ascii")).hexdigest()
        if digest != expected["sequence_sha256"]:
            raise DataIntegrityError(
                f"Final FASTA order/hash mismatch at record {count}: expected {expected['sequence_sha256']}, observed {digest}."
            )
        token = rec.header.split(None, 1)[0]
        parts = token.split("|")
        if len(parts) != 4 or not parts[3].startswith("src=FFSRC_"):
            raise DataIntegrityError(f"Final FASTA header does not follow the four-field contract: {token}")
        if parts[3] != f"src=FFSRC_{digest}":
            raise DataIntegrityError(f"Final FASTA src identifier does not match sequence SHA-256: {token}")
        if state.conn.execute("SELECT 1 FROM sources WHERE sequence_sha256=? LIMIT 1", (digest,)).fetchone() is None:
            raise DataIntegrityError(f"Final FASTA sequence {digest} has no provenance.")
    try:
        extra = next(fasta_iter)
    except StopIteration:
        extra = None
    if extra is not None:
        raise DataIntegrityError("Final FASTA contains more records than the canonical sequence index.")
    if count != expected_count:
        raise DataIntegrityError(f"Final FASTA count {count} != canonical count {expected_count}.")



########################################################################################################
# Master tables and reports
########################################################################################################

def write_excluded_taxa_report(work_root: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "Species", "TaxID", "Matched_exclusion_taxa", "Matched_exclusion_taxids",
        "Exclusion_reasons", "Exclusion_override", "Override_reason", "Final_status",
    ]
    write_tsv(work_root / REPORTS_DIR / "excluded_taxa.tsv", fields, rows)


def write_species_filter_failures_report(state: StateDB, work_root: Path) -> None:
    fields = ["Species", "TaxID", "Filter_ID", "Field", "Operator", "Required", "Observed", "Failure_reason"]
    rows = (
        {
            "Species": r["species"], "TaxID": r["species_taxid"], "Filter_ID": r["filter_id"],
            "Field": r["field"], "Operator": r["operator"], "Required": r["required"],
            "Observed": r["observed"], "Failure_reason": r["failure_reason"],
        }
        for r in state.conn.execute("SELECT * FROM species_filter_failures ORDER BY species,species_taxid,id")
    )
    write_tsv(work_root / REPORTS_DIR / "species_filter_failures.tsv", fields, rows)


def write_assembly_failure_report(state: StateDB, work_root: Path) -> None:
    fields = ["Assembly", "Species", "TaxID", "Filter_ID", "Field", "Operator", "Required", "Observed", "Failure_reason"]
    rows = (
        {
            "Assembly": r["accession"], "Species": r["species"], "TaxID": r["species_taxid"],
            "Filter_ID": r["filter_id"], "Field": r["field"], "Operator": r["operator"],
            "Required": r["required"], "Observed": r["observed"], "Failure_reason": r["failure_reason"],
        }
        for r in state.conn.execute("SELECT * FROM assembly_failures ORDER BY species,species_taxid,accession,id")
    )
    write_tsv(work_root / REPORTS_DIR / "assembly_qc_failures.tsv", fields, rows)


def count_ncbi_retrieval_failure_entries(
    state: StateDB,
) -> int:
    """Count assembly attempts that ended in a classified NCBI retrieval failure."""

    # Keep the SQL synchronized automatically with the single central set of
    # machine-readable NCBI retrieval reasons.
    reasons = tuple(
        sorted(NCBI_RETRIEVAL_FAILURE_REASONS)
    )

    placeholders = ",".join(
        "?"
        for _ in reasons
    )

    return int(
        state.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM failed_downloads
            WHERE reason IN ({placeholders})
            """,
            reasons,
        ).fetchone()[0]
    )


def count_species_without_protein_due_to_ncbi(
    state: StateDB,
) -> int:
    """Count retained species left without proteins solely after NCBI retrieval failures.

    A species is counted only when it is taxonomically included, passes the
    species-level metadata filters, has no selected usable protein assembly,
    has at least one assembly actually assessed for protein availability, and
    every such assessed assembly ended with a classified NCBI retrieval failure.

    Assemblies deliberately marked NOT_SELECTED by biological selection policy
    do not participate in this causality classification.
    """

    reasons = tuple(
        sorted(NCBI_RETRIEVAL_FAILURE_REASONS)
    )

    placeholders = ",".join(
        "?"
        for _ in reasons
    )

    return int(
        state.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM species AS s
            WHERE s.final_taxonomic_status = 'INCLUDED'
              AND s.filter_status = 'PASS'

              AND NOT EXISTS (
                  SELECT 1
                  FROM assemblies AS selected
                  WHERE selected.species_taxid = s.taxid
                    AND selected.selected = 1
                    AND selected.protein_fasta_available = 1
              )

              AND EXISTS (
                  SELECT 1
                  FROM assemblies AS assessed
                  WHERE assessed.species_taxid = s.taxid
                    AND assessed.source_scope_status = 'PASS'
                    AND assessed.qc_status = 'PASS'
                    AND assessed.final_status != 'NOT_SELECTED'
              )

              AND NOT EXISTS (
                  SELECT 1
                  FROM assemblies AS assessed
                  WHERE assessed.species_taxid = s.taxid
                    AND assessed.source_scope_status = 'PASS'
                    AND assessed.qc_status = 'PASS'
                    AND assessed.final_status != 'NOT_SELECTED'
                    AND (
                        assessed.protein_fasta_available = 1
                        OR assessed.final_reason IS NULL
                        OR assessed.final_reason NOT IN ({placeholders})
                    )
              )
            """,
            reasons,
        ).fetchone()[0]
    )


def warn_incomplete_ncbi_protein_coverage(
    state: StateDB,
) -> None:
    """Warn when retained species remain unrepresented after NCBI retrieval failures.

    This is a presentation-only warning. It does not change build state,
    invalidate the reference, or prevent normal finalization.
    """

    affected_species = count_species_without_protein_due_to_ncbi(
        state
    )

    # A complete build, or one whose missing protein coverage is unrelated to
    # NCBI retrieval failures, needs no additional warning here.
    if affected_species == 0:
        return

    warn(
        "Protein coverage remains incomplete after automatic restorative "
        "NCBI recovery because retrieval could not be completed for "
        f"{affected_species} retained species.\n\n"
        "          Details:\n"
        "          reports/species_without_protein_FASTA.tsv\n"
        "          reports/failed_downloads.tsv\n\n"
        "          If complete coverage is required, update this completed "
        "reference later when NCBI access is stable."
    )


def write_failed_downloads_report(state: StateDB, work_root: Path) -> None:
    fields = ["Species", "TaxID", "Assembly", "Reason", "Detail"]
    rows = (
        {
            "Species": r["species"], "TaxID": r["species_taxid"], "Assembly": r["assembly"],
            "Reason": r["reason"], "Detail": r["detail"],
        }
        for r in state.conn.execute("SELECT * FROM failed_downloads ORDER BY species,species_taxid,assembly,id")
    )
    write_tsv(work_root / REPORTS_DIR / "failed_downloads.tsv", fields, rows)


def write_protein_warnings_report(state: StateDB, work_root: Path) -> None:
    fields = ["Species", "TaxID", "Assembly", "Protein_accession", "Warning_type", "Count"]
    rows = (
        {
            "Species": r["species"], "TaxID": r["species_taxid"], "Assembly": r["assembly"],
            "Protein_accession": r["protein_accession"], "Warning_type": r["warning_type"], "Count": r["count"],
        }
        for r in state.conn.execute(
            "SELECT * FROM protein_warnings ORDER BY species,species_taxid,assembly,protein_accession,warning_type,id"
        )
    )
    write_tsv(work_root / REPORTS_DIR / "protein_sequence_warnings.tsv", fields, rows)


def write_update_changes_report(state: StateDB, work_root: Path) -> None:
    fields = ["Entity_type", "Species", "TaxID", "Assembly", "Change", "Reason"]
    rows = (
        {
            "Entity_type": r["entity_type"], "Species": value_tsv(r["species"]), "TaxID": value_tsv(r["taxid"]),
            "Assembly": value_tsv(r["assembly"]), "Change": r["change"], "Reason": r["reason"],
        }
        for r in state.conn.execute("SELECT * FROM update_changes ORDER BY entity_type,species,taxid,assembly,id")
    )
    write_tsv(work_root / REPORTS_DIR / "reference_update_changes.tsv", fields, rows)


@dataclass(frozen=True)
class AssemblyCoverageMetrics:
    """Final assembly-selection coverage metrics derived from persistent state."""

    eligible_strain_groups: int
    represented_strain_groups: int

    eligible_isolate_groups: int
    isolate_only_groups_not_included: int

    unassigned_groups_not_included: int

    gca_assemblies_excluded: int
    strain_groups_only_in_excluded_gca: int

    assemblies_not_selected_by_policy: int


def calculate_assembly_coverage_metrics(
    conn: sqlite3.Connection,
) -> AssemblyCoverageMetrics:
    """Calculate strain/isolate/source coverage from the final assembly state.

    Group identities always include species TaxID so identical strain/isolate
    labels belonging to different fungal species are never merged.

    Strain and isolate labels use the same deterministic normalization as the
    biological selection engine.
    """

    rows = conn.execute(
        """
        SELECT
            a.species_taxid,
            a.accession,
            a.source,
            a.source_scope_status,
            a.qc_status,
            a.selected,
            a.protein_fasta_available,
            a.final_status,
            a.final_reason,
            a.strain,
            a.isolate,
            a.biosample
        FROM assemblies AS a
        JOIN species AS s
          ON s.taxid = a.species_taxid
        WHERE s.final_taxonomic_status='INCLUDED'
          AND s.filter_status='PASS'
        """
    ).fetchall()

    eligible_strains: set[
        tuple[int, str]
    ] = set()

    represented_strains: set[
        tuple[int, str]
    ] = set()

    eligible_isolates: set[
        tuple[int, str | None, str]
    ] = set()

    isolate_only_not_included: set[
        tuple[int, str]
    ] = set()

    unassigned_not_included: set[
        tuple[int, str, str]
    ] = set()

    excluded_gca_strains: set[
        tuple[int, str]
    ] = set()

    strains_observed_elsewhere: set[
        tuple[int, str]
    ] = set()

    gca_assemblies_excluded = 0
    assemblies_not_selected = 0

    for row in rows:

        taxid = int(row["species_taxid"])

        strain_key = normalize_selection_label(
            row["strain"]
        )

        isolate_key = normalize_selection_label(
            row["isolate"]
        )

        source_scope_pass = (
            row["source_scope_status"] == "PASS"
        )

        qc_pass = (
            row["qc_status"] == "PASS"
        )

        # Source/QC eligibility is not sufficient when explicit biological
        # constraints narrow the active selection scope. Assemblies deliberately
        # removed by an exact assembly exclusion, a strain exclusion, or a
        # target-only species boundary must not inflate coverage denominators.
        selection_scope_pass = (
            row["final_reason"]
            not in EXPLICIT_SELECTION_SCOPE_EXCLUSION_REASONS
        )

        eligible = (
            source_scope_pass
            and qc_pass
            and selection_scope_pass
        )

        excluded_gca = (
            row["source"] == "GCA"
            and row["source_scope_status"] == "FAIL"
            and row["final_reason"] == "GCA_NOT_ENABLED"
        )

        if excluded_gca:
            gca_assemblies_excluded += 1

            if strain_key is not None:
                excluded_gca_strains.add(
                    (
                        taxid,
                        strain_key,
                    )
                )

        elif strain_key is not None:

            # This strain label was observed somewhere in the inventory outside
            # the set of GCA records excluded specifically because GCA support
            # was disabled. It therefore cannot be described as GCA-only.
            strains_observed_elsewhere.add(
                (
                    taxid,
                    strain_key,
                )
            )

        if eligible:

            if strain_key is not None:
                eligible_strains.add(
                    (
                        taxid,
                        strain_key,
                    )
                )

            if isolate_key is not None:
                eligible_isolates.add(
                    (
                        taxid,
                        strain_key,
                        isolate_key,
                    )
                )

        if (
            bool(row["selected"])
            and bool(row["protein_fasta_available"])
            and strain_key is not None
        ):
            represented_strains.add(
                (
                    taxid,
                    strain_key,
                )
            )

        if row["final_status"] == "NOT_SELECTED":
            assemblies_not_selected += 1

        # Isolate-only means isolate metadata exists but strain metadata does
        # not. These records are excluded by default strain-level selection
        # unless --include-isolates is active.
        if (
            row["final_reason"] == "ISOLATE_NOT_INCLUDED"
            and strain_key is None
            and isolate_key is not None
        ):
            isolate_only_not_included.add(
                (
                    taxid,
                    isolate_key,
                )
            )

        # Unassigned biological units are grouped by BioSample where possible.
        # If BioSample is unavailable, the assembly accession itself is the
        # only identity that can safely define the unit.
        if (
            row["final_reason"] == "UNASSIGNED_NOT_INCLUDED"
            and strain_key is None
            and isolate_key is None
        ):

            biosample = row["biosample"]

            if biosample:

                normalized_biosample = unicodedata.normalize(
                    "NFKC",
                    str(biosample),
                ).strip().casefold()

            else:
                normalized_biosample = ""

            if normalized_biosample:

                group_type = "BIOSAMPLE"
                group_value = normalized_biosample

            else:

                group_type = "ASSEMBLY"
                group_value = str(
                    row["accession"]
                ).casefold()

            unassigned_not_included.add(
                (
                    taxid,
                    group_type,
                    group_value,
                )
            )

    strain_groups_only_in_excluded_gca = (
        excluded_gca_strains
        - strains_observed_elsewhere
    )

    return AssemblyCoverageMetrics(
        eligible_strain_groups=len(
            eligible_strains
        ),
        represented_strain_groups=len(
            represented_strains
        ),
        eligible_isolate_groups=len(
            eligible_isolates
        ),
        isolate_only_groups_not_included=len(
            isolate_only_not_included
        ),
        unassigned_groups_not_included=len(
            unassigned_not_included
        ),
        gca_assemblies_excluded=(
            gca_assemblies_excluded
        ),
        strain_groups_only_in_excluded_gca=len(
            strain_groups_only_in_excluded_gca
        ),
        assemblies_not_selected_by_policy=(
            assemblies_not_selected
        ),
    )


@dataclass(frozen=True)
class ExplicitTargetOutcome:
    """Final observed outcome of one resolved explicit biological target."""

    target: ResolvedExplicitTarget = field(
        compare=False,
        repr=False,
    )

    status: str

    matched_accessions: tuple[str, ...]
    selected_usable_accessions: tuple[str, ...]

    reason: str
    detail: str


@dataclass(frozen=True)
class ExplicitTargetOutcomeMetrics:
    include_total: int = 0
    include_fulfilled: int = 0
    include_unfulfilled: int = 0

    exclude_total: int = 0
    exclude_applied: int = 0
    exclude_not_applicable: int = 0


def explicit_target_matching_assembly_rows(
    conn: sqlite3.Connection,
    target: ResolvedExplicitTarget,
) -> list[sqlite3.Row]:
    """Return final assembly rows belonging to one resolved target."""

    rows = conn.execute(
        """
        SELECT
            accession,
            species_taxid,
            strain,
            source_scope_status,
            qc_status,
            selected,
            protein_fasta_available,
            final_status,
            final_reason
        FROM assemblies
        WHERE species_taxid=?
        ORDER BY accession
        """,
        (
            target.species_taxid,
        ),
    ).fetchall()

    if target.rule.target_type == "assembly":

        if target.assembly_accession is None:
            raise DataIntegrityError(
                "Resolved explicit assembly target has no assembly accession."
            )

        return [
            row
            for row in rows
            if row["accession"]
            == target.assembly_accession
        ]

    if target.rule.target_type == "strain":

        if target.strain_key is None:
            raise DataIntegrityError(
                "Resolved explicit strain target has no normalized strain key."
            )

        return [
            row
            for row in rows
            if normalize_selection_label(
                row["strain"]
            )
            == target.strain_key
        ]

    raise DataIntegrityError(
        "Unsupported resolved explicit target type during final reporting: "
        f"{target.rule.target_type!r}."
    )


def explicit_target_failure_detail(
    conn: sqlite3.Connection,
    species_taxid: int,
    accessions: Sequence[str],
) -> str:
    """Summarize persisted protein-retrieval details for target assemblies."""

    unique_accessions = sorted(
        set(accessions)
    )

    if not unique_accessions:
        return "."

    placeholders = ",".join(
        "?"
        for _ in unique_accessions
    )

    rows = conn.execute(
        f"""
        SELECT
            assembly,
            reason,
            detail
        FROM failed_downloads
        WHERE species_taxid=?
          AND assembly IN ({placeholders})
        ORDER BY assembly, id
        """,
        (
            species_taxid,
            *unique_accessions,
        ),
    ).fetchall()

    details: list[str] = []

    for row in rows:

        reason = (
            normalize_missing(
                row["reason"]
            )
            or "UNKNOWN_FAILURE"
        )

        detail = normalize_missing(
            row["detail"]
        )

        rendered = (
            f"{row['assembly']}: {reason}"
        )

        if detail is not None:
            rendered += f" ({detail})"

        details.append(
            rendered
        )

    if not details:
        return "."

    return " | ".join(
        dict.fromkeys(details)
    )


def explicit_target_unfulfilled_reason(
    rows: Sequence[sqlite3.Row],
) -> str:
    """Return the most informative persisted reason for an include failure."""

    eligible_rows = [
        row
        for row in rows
        if (
            row["source_scope_status"] == "PASS"
            and row["qc_status"] == "PASS"
        )
    ]

    assessed_rows = [
        row
        for row in eligible_rows
        if row["final_status"]
        != "NOT_SELECTED"
    ]

    reason_rows = (
        assessed_rows
        or eligible_rows
        or list(rows)
    )

    reasons = sorted({
        reason
        for row in reason_rows
        if (
            reason := normalize_missing(
                row["final_reason"]
            )
        )
        is not None
    })

    if reasons:
        return ";".join(reasons)

    return "NO_USABLE_SELECTED_PROTEIN"


def calculate_explicit_target_outcomes(
    conn: sqlite3.Connection,
    targets: Sequence[ResolvedExplicitTarget],
) -> list[ExplicitTargetOutcome]:
    """Derive target fulfillment strictly from final persistent assembly state."""

    outcomes: list[
        ExplicitTargetOutcome
    ] = []

    for target in targets:

        rows = explicit_target_matching_assembly_rows(
            conn,
            target,
        )

        matched_accessions = tuple(
            row["accession"]
            for row in rows
        )

        selected_usable = tuple(
            row["accession"]
            for row in rows
            if (
                bool(row["selected"])
                and bool(
                    row["protein_fasta_available"]
                )
            )
        )

        action = target.rule.action

        # --------------------------------------------------------------
        # INCLUDE
        # --------------------------------------------------------------
        if action == "include":

            # Explicit includes have already passed mandatory taxonomy,
            # assembly-scope/QC/filter and constraint validation. Reaching final
            # reporting without any corresponding assembly state is therefore an
            # internal consistency failure, not an ordinary unfulfilled target.
            if not rows:
                raise DataIntegrityError(
                    "Final state is missing assembly records for explicitly "
                    f"included {target.rule.target_type} target: "
                    f"{target.species} (TaxID {target.species_taxid})."
                )

            if selected_usable:

                outcomes.append(
                    ExplicitTargetOutcome(
                        target=target,
                        status="FULFILLED",
                        matched_accessions=matched_accessions,
                        selected_usable_accessions=selected_usable,
                        reason="SELECTED_USABLE_PROTEIN",
                        detail=".",
                    )
                )

                continue

            reason = explicit_target_unfulfilled_reason(
                rows
            )

            detail = explicit_target_failure_detail(
                conn,
                target.species_taxid,
                matched_accessions,
            )

            outcomes.append(
                ExplicitTargetOutcome(
                    target=target,
                    status="UNFULFILLED",
                    matched_accessions=matched_accessions,
                    selected_usable_accessions=(),
                    reason=reason,
                    detail=detail,
                )
            )

            continue

        # --------------------------------------------------------------
        # EXCLUDE
        # --------------------------------------------------------------
        if action == "exclude":

            selected_rows = [
                row
                for row in rows
                if bool(
                    row["selected"]
                )
            ]

            if selected_rows:

                selected_text = ", ".join(
                    row["accession"]
                    for row in selected_rows
                )

                raise DataIntegrityError(
                    "Explicit exclusion constraint was violated by the final "
                    f"selection: {selected_text}."
                )

            eligible_matching_rows = [
                row
                for row in rows
                if (
                    row["source_scope_status"]
                    == "PASS"
                    and row["qc_status"]
                    == "PASS"
                )
            ]

            if eligible_matching_rows:

                expected_reason = (
                    "EXPLICIT_ASSEMBLY_EXCLUDED"
                    if target.rule.target_type
                    == "assembly"
                    else "EXPLICIT_STRAIN_EXCLUDED"
                )

                outcomes.append(
                    ExplicitTargetOutcome(
                        target=target,
                        status="APPLIED",
                        matched_accessions=matched_accessions,
                        selected_usable_accessions=(),
                        reason=expected_reason,
                        detail=".",
                    )
                )

            else:

                # The exclusion was valid but had nothing eligible to remove
                # from the active build. Report this explicitly rather than
                # implying that it changed the final selection.
                outcomes.append(
                    ExplicitTargetOutcome(
                        target=target,
                        status="NOT_APPLICABLE",
                        matched_accessions=matched_accessions,
                        selected_usable_accessions=(),
                        reason=(
                            "NO_ELIGIBLE_MATCH_IN_ACTIVE_BUILD"
                        ),
                        detail=".",
                    )
                )

            continue

        raise DataIntegrityError(
            "Unsupported explicit target action during final reporting: "
            f"{action!r}."
        )

    return outcomes


def explicit_target_outcome_metrics(
    outcomes: Sequence[ExplicitTargetOutcome],
) -> ExplicitTargetOutcomeMetrics:
    """Summarize final explicit-target outcomes."""

    include_outcomes = [
        outcome
        for outcome in outcomes
        if outcome.target.rule.action
        == "include"
    ]

    exclude_outcomes = [
        outcome
        for outcome in outcomes
        if outcome.target.rule.action
        == "exclude"
    ]

    return ExplicitTargetOutcomeMetrics(
        include_total=len(
            include_outcomes
        ),
        include_fulfilled=sum(
            outcome.status == "FULFILLED"
            for outcome in include_outcomes
        ),
        include_unfulfilled=sum(
            outcome.status == "UNFULFILLED"
            for outcome in include_outcomes
        ),
        exclude_total=len(
            exclude_outcomes
        ),
        exclude_applied=sum(
            outcome.status == "APPLIED"
            for outcome in exclude_outcomes
        ),
        exclude_not_applicable=sum(
            outcome.status == "NOT_APPLICABLE"
            for outcome in exclude_outcomes
        ),
    )


def write_explicit_targets_report(
    state: StateDB,
    work_root: Path,
    targets: Sequence[ResolvedExplicitTarget],
) -> ExplicitTargetOutcomeMetrics:
    """Write final explicit-target outcomes and explain incomplete fulfillment."""

    outcomes = calculate_explicit_target_outcomes(
        state.conn,
        targets,
    )

    fields = [
        "Action",
        "Target_type",
        "Species",
        "Taxid",
        "Strain",
        "Assembly_accession",
        "Status",
        "Matched_assemblies",
        "Selected_usable_assemblies",
        "Reason",
        "Detail",
        "Evidence_type",
        "Notes",
        "Input_file",
        "Input_line",
    ]

    rows = []

    for outcome in outcomes:

        target = outcome.target
        rule = target.rule

        rows.append({
            "Action": rule.action.upper(),
            "Target_type": rule.target_type.upper(),
            "Species": target.species,
            "Taxid": target.species_taxid,
            "Strain": value_tsv(target.strain),
            "Assembly_accession": value_tsv(
                target.assembly_accession
            ),
            "Status": outcome.status,
            "Matched_assemblies": (
                ";".join(
                    outcome.matched_accessions
                )
                or "."
            ),
            "Selected_usable_assemblies": (
                ";".join(
                    outcome.selected_usable_accessions
                )
                or "."
            ),
            "Reason": outcome.reason,
            "Detail": outcome.detail,
            "Evidence_type": value_tsv(
                rule.evidence_type
            ),
            "Notes": value_tsv(
                rule.notes
            ),
            "Input_file": rule.input_path,
            "Input_line": rule.input_line,
        })

    write_tsv(
        work_root
        / REPORTS_DIR
        / "explicit_targets.tsv",
        fields,
        rows,
    )

    metrics = explicit_target_outcome_metrics(
        outcomes
    )

    if outcomes:

        info(
            "Explicit target outcomes: "
            f"includes fulfilled "
            f"{metrics.include_fulfilled}/{metrics.include_total}; "
            f"exclusions applied "
            f"{metrics.exclude_applied}/{metrics.exclude_total}"
            + (
                f"; exclusions not applicable "
                f"{metrics.exclude_not_applicable}"
                if metrics.exclude_not_applicable
                else ""
            )
            + "."
        )

    if metrics.include_unfulfilled:

        warn(
            f"{metrics.include_unfulfilled} explicitly included target(s) "
            "could not be fulfilled with usable selected protein data. "
            "The reference can still complete, but these requested targets "
            "are not represented as requested. See "
            "reports/explicit_targets.tsv for exact reasons."
        )

    return metrics


def write_species_without_protein_report(
    state: StateDB,
    work_root: Path,
) -> None:
    """Report retained species with no selected usable proteome.

    Assemblies deliberately skipped by biological selection depth are kept
    distinct from assemblies that were actually assessed and failed protein
    retrieval or validation.

    This prevents strain/isolate policy decisions from being misreported as
    biological absence of protein data.
    """

    fields = [
        "Species",
        "TaxID",
        "Tier",
        "Reason",
        "Eligible_assemblies",
        "Policy_not_selected_assemblies",
        "Assemblies_assessed_for_protein",
        "Failed_or_no_protein_assemblies",
    ]

    rows: list[dict[str, Any]] = []

    species_rows = state.conn.execute(
        """
        SELECT *
        FROM species
        WHERE final_taxonomic_status='INCLUDED'
          AND filter_status='PASS'
        ORDER BY species,taxid
        """
    ).fetchall()

    for s in species_rows:

        selected = int(
            state.conn.execute(
                """
                SELECT COUNT(*)
                FROM assemblies
                WHERE species_taxid=?
                  AND selected=1
                  AND protein_fasta_available=1
                """,
                (s["taxid"],),
            ).fetchone()[0]
        )

        if selected:
            continue

        eligible_rows = state.conn.execute(
            """
            SELECT
                protein_fasta_available,
                final_status,
                final_reason
            FROM assemblies
            WHERE species_taxid=?
              AND source_scope_status='PASS'
              AND qc_status='PASS'
            """,
            (s["taxid"],),
        ).fetchall()

        eligible = len(
            eligible_rows
        )

        # Biological selection can intentionally leave an otherwise eligible
        # assembly unattempted. Such rows are NOT protein failures.
        policy_not_selected = sum(
            1
            for row in eligible_rows
            if row["final_status"] == "NOT_SELECTED"
        )

        assessed_for_protein = (
            eligible
            - policy_not_selected
        )

        failed_or_no_protein = sum(
            1
            for row in eligible_rows
            if (
                row["final_status"] != "NOT_SELECTED"
                and row["protein_fasta_available"] != 1
            )
        )

        assessed_rows = [
            row
            for row in eligible_rows
            if row["final_status"] != "NOT_SELECTED"
        ]

        all_assessed_failed_due_to_ncbi = (
            bool(assessed_rows)
            and all(
                row["protein_fasta_available"] != 1
                and row["final_reason"]
                in NCBI_RETRIEVAL_FAILURE_REASONS
                for row in assessed_rows
            )
        )

        if eligible == 0:

            reason = (
                "NO_ELIGIBLE_ASSEMBLY_AFTER_SCOPE_QC"
            )

        elif (
            assessed_for_protein == 0
            and policy_not_selected > 0
        ):

            reason = (
                "NO_ASSEMBLY_SELECTED_BY_DEPTH_POLICY"
            )

        elif all_assessed_failed_due_to_ncbi:

            reason = "NCBI_RETRIEVAL_FAILED"

        elif (
            assessed_for_protein > 0
            and failed_or_no_protein
            >= assessed_for_protein
        ):

            reason = (
                "NO_USABLE_PROTEIN_FASTA_"
                "AMONG_SELECTED_DEPTH_CANDIDATES"
            )

        else:

            reason = (
                "NO_SELECTED_USABLE_PROTEIN_FASTA"
            )

        rows.append(
            {
                "Species": s["species"],
                "TaxID": s["taxid"],
                "Tier": s["tier"],
                "Reason": reason,
                "Eligible_assemblies": eligible,
                "Policy_not_selected_assemblies": (
                    policy_not_selected
                ),
                "Assemblies_assessed_for_protein": (
                    assessed_for_protein
                ),
                "Failed_or_no_protein_assemblies": (
                    failed_or_no_protein
                ),
            }
        )

    write_tsv(
        work_root
        / REPORTS_DIR
        / "species_without_protein_FASTA.tsv",
        fields,
        rows,
    )


def write_species_master(state: StateDB, work_root: Path, config: BuildConfig) -> None:
    """Write one row per taxonomically resolved candidate species.

    Protein_FASTA_available is tri-state in the master table:
      TRUE  = at least one assembly was confirmed to provide a usable protein.faa
      FALSE = eligible assembly candidates were checked and none was usable
      .     = not assessed (e.g. species excluded before assembly inventory)
    """
    fields = [
        "Species_taxid", "Species", "Phylum", "Class", "Order", "Family", "Genus", "Authority",
        "Tier", "Seed_taxa", "Tier_source", "Explicit_include", "Gut_evidence", "Human_evidence",
        "Evidence_type", "Reference", "Notes", "Inclusion_reason", "Matched_exclusion_taxa",
        "Exclusion_override", "Final_taxonomic_status", "Species_filter_status",
        "Genome_available", "Protein_FASTA_available", "Num_assemblies",
        "Num_eligible_assemblies", "Num_assemblies_with_protein_FASTA",
        "Num_assemblies_protein_FASTA_unknown", "Num_selected_assemblies", "Included_current_DB",
        "Last_checked_NCBI", "DB_version",
    ]
    rows: list[dict[str, Any]] = []
    for s in state.conn.execute("SELECT * FROM species ORDER BY species,taxid"):
        assembly_rows = state.conn.execute("SELECT * FROM assemblies WHERE species_taxid=?", (s["taxid"],)).fetchall()
        n_all = len(assembly_rows)
        n_eligible = sum(1 for a in assembly_rows if a["source_scope_status"] == "PASS" and a["qc_status"] == "PASS")
        n_protein = sum(1 for a in assembly_rows if a["protein_fasta_available"] == 1)
        n_unknown = sum(
            1 for a in assembly_rows
            if a["source_scope_status"] == "PASS" and a["qc_status"] == "PASS" and a["protein_fasta_available"] is None
        )
        n_selected = sum(1 for a in assembly_rows if a["selected"] == 1)
        assessed = bool(assembly_rows) or (
            s["final_taxonomic_status"] == "INCLUDED" and s["filter_status"] == "PASS" and s["state"] in {"PREPARED", "DONE"}
        )
        if n_protein > 0:
            protein_available: Any = True
        elif assessed and n_unknown == 0:
            protein_available = False
        else:
            protein_available = "."
        rows.append({
            "Species_taxid": s["taxid"], "Species": s["species"], "Phylum": value_tsv(s["phylum"]),
            "Class": value_tsv(s["class_name"]), "Order": value_tsv(s["order_name"]),
            "Family": value_tsv(s["family"]), "Genus": value_tsv(s["genus"]), "Authority": value_tsv(s["authority"]),
            "Tier": s["tier"], "Seed_taxa": value_tsv(s["seed_taxa"]), "Tier_source": value_tsv(s["tier_source"]),
            "Explicit_include": bool_tsv(bool(s["explicit_include"])), "Gut_evidence": bool_tsv(bool(s["gut_evidence"])),
            "Human_evidence": bool_tsv(bool(s["human_evidence"])), "Evidence_type": value_tsv(s["evidence_type"]),
            "Reference": value_tsv(s["reference"]), "Notes": value_tsv(s["notes"]),
            "Inclusion_reason": value_tsv(s["inclusion_reason"]),
            "Matched_exclusion_taxa": value_tsv(s["matched_exclusion_taxa"]),
            "Exclusion_override": bool_tsv(bool(s["exclusion_override"])),
            "Final_taxonomic_status": s["final_taxonomic_status"], "Species_filter_status": s["filter_status"],
            "Genome_available": bool_tsv(n_all > 0) if assessed else ".",
            "Protein_FASTA_available": bool_tsv(protein_available) if isinstance(protein_available, bool) else ".",
            "Num_assemblies": n_all, "Num_eligible_assemblies": n_eligible,
            "Num_assemblies_with_protein_FASTA": n_protein,
            "Num_assemblies_protein_FASTA_unknown": n_unknown,
            "Num_selected_assemblies": n_selected,
            "Included_current_DB": bool_tsv(n_selected > 0),
            "Last_checked_NCBI": value_tsv(s["last_checked_ncbi"]), "DB_version": value_tsv(config.db_version),
        })
    tsv = work_root / METADATA_DIR / "fungal_species_master.tsv"
    write_tsv(tsv, fields, rows)
    write_xlsx_from_tsv(tsv, work_root / METADATA_DIR / "fungal_species_master.xlsx")


def write_assemblies_master(state: StateDB, work_root: Path, config: BuildConfig) -> None:
    fields = [
        "Species", "Species_taxid", "Assembly", "Assembly_source", "Paired_accession", "Multi_isolate_project",
        "Assembly_status", "Atypical", "Atypical_warnings", "Assembly_level", "RefSeq_category",
        "Strain", "Isolate", "Infraspecific_name", "BioProject", "BioSample", "Assembly_method",
        "Sequencing_technology", "Contig_N50", "Contig_L50", "Num_contigs", "Scaffold_N50", "Scaffold_L50",
        "Num_scaffolds", "Num_scaffold_gaps", "Genome_size", "GC_percent", "Protein_coding_genes",
        "BUSCO_complete", "BUSCO_fragmented", "BUSCO_missing", "BUSCO_duplicated", "BUSCO_lineage", "BUSCO_version",
        "Release_date", "Annotation_name", "Annotation_release_date", "Annotation_status",
        "Protein_FASTA_available", "Protein_count", "Protein_FASTA_SHA256", "NCBI_package_protein_MD5",
        "Fingerprint", "Source_scope_status", "QC_status", "Selected", "Final_status", "Final_reason",
        "Update_change", "Update_reason", "DB_version",
    ]
    # Assembly.update_change and Assembly.update_reason contain the provisional
    # classification used internally to recover interrupted update processing.
    #
    # User-facing metadata must instead report the FINAL reference-to-reference
    # classification stored in update_changes after protein availability and
    # final biological assembly selection have been resolved.
    final_updates: dict[str, tuple[str, str]] = {}

    for update_row in state.conn.execute(
        """
        SELECT assembly, change, reason
        FROM update_changes
        WHERE entity_type='ASSEMBLY'
          AND assembly IS NOT NULL
        ORDER BY id
        """
    ):

        accession = str(update_row["assembly"])

        # After final reconciliation there must be at most one public change
        # classification for each assembly. Never silently choose between
        # contradictory rows.
        if accession in final_updates:

            raise DataIntegrityError(
                "Multiple final update classifications were found for assembly "
                f"{accession}. The public update report would be ambiguous."
            )

        final_updates[accession] = (
            str(update_row["change"]),
            str(update_row["reason"]),
        )

    rows: list[dict[str, Any]] = []

    for a in state.conn.execute(
        "SELECT * FROM assemblies "
        "ORDER BY species,species_taxid,accession"
    ):

        final_update = final_updates.get(
            str(a["accession"])
        )

        if final_update is None:

            final_update_change = None
            final_update_reason = None

        else:

            final_update_change, final_update_reason = final_update
        rows.append({
            "Species": a["species"], "Species_taxid": a["species_taxid"], "Assembly": a["accession"],
            "Assembly_source": value_tsv(a["source"]), "Paired_accession": value_tsv(a["paired_accession"]),
            "Multi_isolate_project": bool_tsv(bool(a["multi_isolate"])) if a["multi_isolate"] is not None else ".",
            "Assembly_status": value_tsv(a["assembly_status"]), "Atypical": bool_tsv(bool(a["atypical"])) if a["atypical"] is not None else ".",
            "Atypical_warnings": value_tsv(a["atypical_warnings"]), "Assembly_level": value_tsv(a["assembly_level"]),
            "RefSeq_category": value_tsv(a["refseq_category"]), "Strain": value_tsv(a["strain"]),
            "Isolate": value_tsv(a["isolate"]), "Infraspecific_name": value_tsv(a["infraspecific_name"]),
            "BioProject": value_tsv(a["bioproject"]), "BioSample": value_tsv(a["biosample"]),
            "Assembly_method": value_tsv(a["assembly_method"]), "Sequencing_technology": value_tsv(a["sequencing_technology"]),
            "Contig_N50": value_tsv(a["contig_n50"]), "Contig_L50": value_tsv(a["contig_l50"]),
            "Num_contigs": value_tsv(a["num_contigs"]), "Scaffold_N50": value_tsv(a["scaffold_n50"]),
            "Scaffold_L50": value_tsv(a["scaffold_l50"]), "Num_scaffolds": value_tsv(a["num_scaffolds"]),
            "Num_scaffold_gaps": value_tsv(a["num_scaffold_gaps"]), "Genome_size": value_tsv(a["genome_size"]),
            "GC_percent": value_tsv(a["gc_percent"]), "Protein_coding_genes": value_tsv(a["protein_coding_genes"]),
            "BUSCO_complete": value_tsv(a["busco_complete"]), "BUSCO_fragmented": value_tsv(a["busco_fragmented"]),
            "BUSCO_missing": value_tsv(a["busco_missing"]), "BUSCO_duplicated": value_tsv(a["busco_duplicated"]),
            "BUSCO_lineage": value_tsv(a["busco_lineage"]), "BUSCO_version": value_tsv(a["busco_version"]),
            "Release_date": value_tsv(a["release_date"]), "Annotation_name": value_tsv(a["annotation_name"]),
            "Annotation_release_date": value_tsv(a["annotation_release_date"]), "Annotation_status": value_tsv(a["annotation_status"]),
            "Protein_FASTA_available": bool_tsv(bool(a["protein_fasta_available"])) if a["protein_fasta_available"] is not None else ".",
            "Protein_count": value_tsv(a["protein_count"]), "Protein_FASTA_SHA256": value_tsv(a["protein_fasta_sha256"]),
            "NCBI_package_protein_MD5": value_tsv(a["ncbi_package_md5"]), "Fingerprint": a["fingerprint"],
            "Source_scope_status": value_tsv(a["source_scope_status"]), "QC_status": value_tsv(a["qc_status"]),
            "Selected": bool_tsv(bool(a["selected"])), "Final_status": value_tsv(a["final_status"]),
            "Final_reason": value_tsv(a["final_reason"]), "Update_change": value_tsv(final_update_change),
            "Update_reason": value_tsv(final_update_reason), "DB_version": value_tsv(config.db_version),
        })
    tsv = work_root / METADATA_DIR / "fungal_assemblies_master.tsv"
    write_tsv(tsv, fields, rows)
    write_xlsx_from_tsv(tsv, work_root / METADATA_DIR / "fungal_assemblies_master.xlsx")


def write_build_summary(
    state: StateDB,
    work_root: Path,
    config: BuildConfig,
    plan: ResourcePlan,
    *,
    explicit_target_metrics: ExplicitTargetOutcomeMetrics | None = None,
) -> None:
    """Write a compact key/value summary that is easy to inspect or parse."""
    selected_species = int(state.conn.execute("SELECT COUNT(DISTINCT species_taxid) FROM sources").fetchone()[0])
    selected_assemblies = int(state.conn.execute("SELECT COUNT(*) FROM assemblies WHERE selected=1").fetchone()[0])
    unique_proteins = int(state.conn.execute("SELECT COUNT(*) FROM canonical_sequences").fetchone()[0])
    source_occurrences = int(state.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
    terminal = int(state.conn.execute(
        "SELECT COALESCE(SUM(count),0) FROM protein_warnings WHERE warning_type='TERMINAL_STOP_NORMALIZED'"
    ).fetchone()[0])
    internal = int(state.conn.execute(
        "SELECT COALESCE(SUM(count),0) FROM protein_warnings WHERE warning_type='INTERNAL_STOP_RETAINED'"
    ).fetchone()[0])
    failed_downloads = int(state.conn.execute("SELECT COUNT(*) FROM failed_downloads").fetchone()[0])
    # Separate external NCBI retrieval failures from all other failed assembly
    # or invalid-proteome entries.
    ncbi_retrieval_failures = (
        count_ncbi_retrieval_failure_entries(state)
    )

    # Count retained species whose missing protein coverage is associated with
    # at least one classified NCBI retrieval failure.
    species_no_protein_due_to_ncbi = (
        count_species_without_protein_due_to_ncbi(state)
    )
    species_no_protein = int(state.conn.execute(
        """SELECT COUNT(*) FROM species s
           WHERE final_taxonomic_status='INCLUDED' AND filter_status='PASS'
             AND NOT EXISTS (SELECT 1 FROM assemblies a WHERE a.species_taxid=s.taxid AND a.selected=1 AND a.protein_fasta_available=1)"""
    ).fetchone()[0])

    policy = assembly_selection_policy(
        config
    )

    coverage = calculate_assembly_coverage_metrics(
        state.conn
    )

    if explicit_target_metrics is None:
        explicit_target_metrics = (
            ExplicitTargetOutcomeMetrics()
        )

    if policy.mode == "SPECIES":

        selection_mode = (
            "compact_species_level"
        )

        max_assemblies_per_species = (
            policy.max_assemblies_per_species
        )

        max_assemblies_per_strain = (
            "N/A"
        )

        max_strains_per_species = (
            "N/A"
        )

        isolate_expansion = "N/A"
        unassigned_expansion = "N/A"

    else:

        selection_mode = "strain_level"

        max_assemblies_per_species = (
            "N/A"
        )

        max_assemblies_per_strain = (
            policy.max_assemblies_per_strain
        )

        max_strains_per_species = (
            policy.max_strains_per_species
            if policy.max_strains_per_species
            is not None
            else "all"
        )

        isolate_expansion = (
            policy.include_isolates
        )

        unassigned_expansion = (
            policy.include_unassigned_assemblies
        )
        
    values = [
        ("Tool", TOOL_NAME), ("Version", VERSION),
        ("DB_version", config.db_version), ("Run_mode", config.run_mode), ("Completed", now_iso()),
        ("Final_FASTA", str(work_root / DATABASE_DIR / FINAL_FASTA_NAME)),
        ("Candidate_species_total", state.conn.execute("SELECT COUNT(*) FROM species").fetchone()[0]),
        ("Taxonomically_included_species", state.conn.execute("SELECT COUNT(*) FROM species WHERE final_taxonomic_status='INCLUDED'").fetchone()[0]),
        ("Species_passing_metadata_filters", state.conn.execute("SELECT COUNT(*) FROM species WHERE final_taxonomic_status='INCLUDED' AND filter_status='PASS'").fetchone()[0]),
        ("Species_in_current_DB", selected_species), ("Species_without_selected_protein_FASTA", species_no_protein),
        ("Assemblies_inventory_total", state.conn.execute("SELECT COUNT(*) FROM assemblies").fetchone()[0]),
        ("Assemblies_selected", selected_assemblies), 
                (
            "Assembly_selection_mode",
            selection_mode,
        ),
        (
            "Max_assemblies_per_species",
            max_assemblies_per_species,
        ),
        (
            "Max_assemblies_per_strain",
            max_assemblies_per_strain,
        ),
        (
            "Max_strains_per_species",
            max_strains_per_species,
        ),
        (
            "Isolate_expansion",
            isolate_expansion,
        ),
        (
            "Unassigned_assembly_expansion",
            unassigned_expansion,
        ),
        (
            "Eligible_strain_groups_under_current_scope_QC",
            coverage.eligible_strain_groups,
        ),
        (
            "Represented_strain_groups",
            coverage.represented_strain_groups,
        ),
        (
            "Eligible_isolate_groups_under_current_scope_QC",
            coverage.eligible_isolate_groups,
        ),
        (
            "Eligible_isolate_only_groups_not_included_by_depth_policy",
            coverage.isolate_only_groups_not_included,
        ),
        (
            "Eligible_unassigned_groups_not_included_by_depth_policy",
            coverage.unassigned_groups_not_included,
        ),
        (
            "GCA_assemblies_excluded_because_GCA_not_enabled",
            coverage.gca_assemblies_excluded,
        ),
        (
            "Strain_groups_observed_only_among_excluded_GCA_records",
            coverage.strain_groups_only_in_excluded_gca,
        ),
        (
            "Assemblies_not_selected_by_biological_selection_policy",
            coverage.assemblies_not_selected_by_policy,
        ),
                (
            "Explicit_include_targets_total",
            explicit_target_metrics.include_total,
        ),
        (
            "Explicit_include_targets_fulfilled",
            explicit_target_metrics.include_fulfilled,
        ),
        (
            "Explicit_include_targets_unfulfilled",
            explicit_target_metrics.include_unfulfilled,
        ),
        (
            "Explicit_exclude_targets_total",
            explicit_target_metrics.exclude_total,
        ),
        (
            "Explicit_exclude_targets_applied",
            explicit_target_metrics.exclude_applied,
        ),
        (
            "Explicit_exclude_targets_not_applicable",
            explicit_target_metrics.exclude_not_applicable,
        ),
        (
            "Unique_canonical_proteins", 
            unique_proteins
        ),
        (
            "Protein_source_occurrences",
            source_occurrences,
        ),

        # Preserve the broad historical metric for compatibility.
        (
            "Failed_download_or_invalid_proteome_entries",
            failed_downloads,
        ),

        # Also expose NCBI retrieval failures independently so external request
        # problems are not confused with unavailable or invalid protein data.
        (
            "NCBI_retrieval_failure_entries",
            ncbi_retrieval_failures,
        ),

        (
            "Species_without_selected_protein_due_to_NCBI_retrieval_failure",
            species_no_protein_due_to_ncbi,
        ),

        (
            "Terminal_stop_markers_normalized",
            terminal,
        ),

        (
            "Internal_stop_markers_retained",
            internal,
        ),
        (
            "Workers_requested",
            config.workers,
        ),
        (
            "Workers_safe",
            plan.safe_workers,
        ),
        (
            "Workers_used",
            plan.used_workers,
        ),

        (
            "NCBI_API_key_detected",
            config.ncbi_api_key_detected,
        ),
        (
            "NCBI_request_rate_per_second",
            config.ncbi_request_rate,
        ),
        (
            "NCBI_max_concurrent_commands",
            NCBI_MAX_CONCURRENT_COMMANDS,
        ),
        (
            "NCBI_effective_concurrency_cap",
            min(
                NCBI_MAX_CONCURRENT_COMMANDS,
                plan.used_workers,
            ),
        ),
        ("NCBI_Datasets_version", config.ncbi_datasets_version),
        ("Deduplication", "exact_100_percent_full_length_amino_acid_identity"),
        ("Hash", "SHA-256_plus_full_sequence_verification"),
        ("Protein_prediction", "NO"), ("Sequence_source", "NCBI_Datasets_only"),
    ]
    write_tsv(
        work_root / REPORTS_DIR / "build_summary.tsv",
        ["Metric", "Value"],
        ({"Metric": k, "Value": value_tsv(v)} for k, v in values),
    )


REPRODUCTION_TARGET_EVIDENCE_TYPE = "REPRODUCTION_TARGET"
REPRODUCTION_TARGET_NOTES = (
    "Automatically generated from final selected assembly."
)


def reproduction_target_rows(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Build exact-assembly targets for the completed reference.

    The reproduction target file records the effective final assembly
    composition, not the rules that originally produced that composition.

    Every selected assembly must still have usable protein data and provenance.
    Otherwise the completed state is internally inconsistent and must not emit a
    reproduction recipe that claims the assembly contributed to the reference.
    """

    selected_rows = conn.execute(
        """
        SELECT
            a.accession,
            a.species_taxid,
            a.species,
            a.strain,
            a.protein_fasta_available,
            EXISTS (
                SELECT 1
                FROM sources AS src
                WHERE src.assembly = a.accession
            ) AS has_sources
        FROM assemblies AS a
        WHERE a.selected = 1
        ORDER BY
            a.species_taxid,
            a.accession
        """
    ).fetchall()

    rows: list[dict[str, Any]] = []

    for row in selected_rows:

        accession = str(
            row["accession"]
        ).strip()

        if not _ASSEMBLY_ACCESSION_VERSION_RE.fullmatch(
            accession
        ):
            raise DataIntegrityError(
                "Cannot write reproduction target for selected assembly "
                "without an exact versioned GCF/GCA accession: "
                f"{accession!r}"
            )

        if not bool(
            row["protein_fasta_available"]
        ):
            raise DataIntegrityError(
                "Cannot write reproduction target for selected assembly "
                "without usable protein data: "
                f"{accession}"
            )

        if not bool(
            row["has_sources"]
        ):
            raise DataIntegrityError(
                "Cannot write reproduction target for selected assembly "
                "without protein provenance records: "
                f"{accession}"
            )

        rows.append(
            {
                "Target_type": "assembly",
                "Species": row["species"],
                "Taxid": row["species_taxid"],
                "Strain": row["strain"],
                "Assembly_accession": accession,
                "Evidence_type": (
                    REPRODUCTION_TARGET_EVIDENCE_TYPE
                ),
                "Notes": REPRODUCTION_TARGET_NOTES,
            }
        )

    return rows


def write_reproduction_targets(
    state: StateDB,
    work_root: Path,
) -> int:
    """Write exact final assembly targets that can reproduce this selection."""

    rows = reproduction_target_rows(
        state.conn
    )

    write_tsv(
        work_root
        / METADATA_DIR
        / REPRODUCTION_TARGETS_TSV,
        EXPLICIT_TARGET_COLUMNS,
        rows,
    )

    info(
        "Reproduction targets written: "
        f"{len(rows):,} exact selected assembly target(s)."
    )

    return len(rows)


def write_all_final_reports(
    state: StateDB,
    work_root: Path,
    config: BuildConfig,
    plan: ResourcePlan,
    *,
    explicit_targets: Sequence[ResolvedExplicitTarget] = (),
) -> None:
    write_species_filter_failures_report(state, work_root)
    write_assembly_failure_report(state, work_root)
    write_failed_downloads_report(state, work_root)
    write_protein_warnings_report(state, work_root)
    write_species_without_protein_report(state, work_root)
    if config.run_mode in {"create-new-updated-version", "overwrite-with-updated-version"} or state.get_meta("build_origin_mode") in {
        "create-new-updated-version", "overwrite-with-updated-version"
    }:
        write_update_changes_report(state, work_root)
    explicit_target_metrics = (
        write_explicit_targets_report(
            state,
            work_root,
            explicit_targets,
        )
    )

    write_reproduction_targets(
        state,
        work_root,
    )

    write_species_master(
        state,
        work_root,
        config,
    )

    write_assemblies_master(
        state,
        work_root,
        config,
    )

    write_build_summary(
        state,
        work_root,
        config,
        plan,
        explicit_target_metrics=(
            explicit_target_metrics
        ),
    )


def print_reference_build_summary(
    final_root: Path,
    *,
    immutable_source: bool = False,
) -> None:
    """Print a concise summary of the final validated protein reference.

    This summary is intended for the user-facing end of a successful run. It
    reports the main composition and exact-sequence deduplication statistics
    without requiring the user to inspect SQLite tables or run manual greps.

    Counts are read from the final persistent state database so the displayed
    values describe the reference that was actually committed.
    """

    state_db = final_root / "state" / "build_state.sqlite"
    final_fasta = final_root / DATABASE_DIR / FINAL_FASTA_NAME

    config_path = (
        final_root
        / CONFIG_JSON_REL
    )

    if not config_path.is_file():
        raise FileNotFoundError(
            f"Final resolved configuration not found: "
            f"{config_path}"
        )

    resolved_config = load_json(
        config_path
    )

    if not state_db.is_file():
        raise FileNotFoundError(
            f"Final state database not found: {state_db}"
        )

    if not final_fasta.is_file():
        raise FileNotFoundError(
            f"Final protein FASTA not found: {final_fasta}"
        )

    # Open the completed state database read-only. The summary must never modify
    # build state after the reference has already been finalized and validated.
    # Preserve the existing connection behavior for ordinary builds.
    uri = f"file:{state_db}?mode=ro"

    # Manual recovery must not create WAL/SHM files in the completed
    # original reference when printing the final summary.
    if immutable_source:
        uri = state_db.resolve().as_uri() + "?mode=ro&immutable=1"

    conn = sqlite3.connect(
        uri,
        uri=True,
    )

    conn.row_factory = sqlite3.Row

    try:
        def scalar_count(query: str) -> int:
            """Return one integer value from a COUNT/SUM SQLite query."""

            row = conn.execute(query).fetchone()

            if row is None or row[0] is None:
                return 0

            return int(row[0])

        # Complete species inventory recorded during taxonomy curation.
        candidate_species_total = scalar_count(
            "SELECT COUNT(*) FROM species"
        )

        # Species retained after taxonomic precedence and exclusion decisions.
        taxonomic_species = scalar_count(
            """
            SELECT COUNT(*)
            FROM species
            WHERE final_taxonomic_status='INCLUDED'
            """
        )

        # Species that also passed species-level metadata filters and therefore
        # entered assembly/protein reference construction.
        eligible_species = scalar_count(
            """
            SELECT COUNT(*)
            FROM species
            WHERE final_taxonomic_status='INCLUDED'
              AND filter_status='PASS'
            """
        )

        # A species is represented in the final reference only when at least
        # one committed protein provenance row exists for that species.
        represented_species = scalar_count(
            "SELECT COUNT(DISTINCT species_taxid) FROM sources"
        )

        # Assemblies that successfully contributed proteins to the reference.
        selected_assemblies = scalar_count(
            "SELECT COUNT(*) FROM assemblies WHERE selected=1"
        )

        # Every provenance row represents one original source-protein occurrence.
        # Several occurrences may collapse onto the same canonical sequence.
        source_occurrences = scalar_count(
            "SELECT COUNT(*) FROM sources"
        )

        # Count unique sequences independently within each species, then sum
        # those counts. This measures deduplication that happened before the
        # final cross-species exact-sequence collapse.
        species_local_unique = scalar_count(
            """
            SELECT COALESCE(SUM(unique_count), 0)
            FROM (
                SELECT
                    species_taxid,
                    COUNT(DISTINCT sequence_sha256) AS unique_count
                FROM sources
                GROUP BY species_taxid
            )
            """
        )

        # canonical_sequences contains the globally deduplicated sequences that
        # are represented in the final protein reference.
        final_unique_sequences = scalar_count(
            "SELECT COUNT(*) FROM canonical_sequences"
        )

        coverage = (
            calculate_assembly_coverage_metrics(
                conn
            )
        )

    finally:
        conn.close()

    # ------------------------------------------------------------------
    # Describe the biological assembly-selection policy used by this build.
    # This comes from resolved_config.json rather than SQLite because these
    # values describe configuration, not database state.
    # ------------------------------------------------------------------
    compact_species_cap = (
        resolved_config.get(
            "max_assemblies_per_species"
        )
    )

    if compact_species_cap is not None:

        selection_mode = (
            "compact species-level"
        )

        selection_depth_metrics = [
            (
                "Selection mode",
                selection_mode,
            ),
            (
                "Max assemblies per species",
                f"{compact_species_cap}",
            ),
        ]

    else:

        max_per_strain = (
            resolved_config.get(
                "max_assemblies_per_strain",
                1,
            )
        )

        max_strains = (
            resolved_config.get(
                "max_strains_per_species"
            )
        )

        max_strains_display = (
            "all"
            if max_strains is None
            else str(max_strains)
        )

        include_isolates = bool(
            resolved_config.get(
                "include_isolates",
                False,
            )
        )

        include_unassigned = bool(
            resolved_config.get(
                "include_unassigned_assemblies",
                False,
            )
        )

        selection_depth_metrics = [
            (
                "Selection mode",
                "strain-level",
            ),
            (
                "Max strains per species",
                max_strains_display,
            ),
            (
                "Max assemblies per strain",
                f"{max_per_strain}",
            ),
            (
                "Isolate expansion",
                "enabled" if include_isolates else "disabled",
            ),
            (
                "Unassigned assembly expansion",
                "enabled" if include_unassigned else "disabled",
            ),
        ]

    # Derive the different layers of exact-sequence deduplication.
    within_species_collapsed = max(
        0,
        source_occurrences - species_local_unique,
    )

    cross_species_collapsed = max(
        0,
        species_local_unique - final_unique_sequences,
    )

    total_collapsed = max(
        0,
        source_occurrences - final_unique_sequences,
    )

    if source_occurrences:
        deduplication_rate = (
            total_collapsed / source_occurrences
        ) * 100.0
    else:
        deduplication_rate = 0.0

    species_without_proteins = max(
        0,
        eligible_species - represented_species,
    )

    # Use a human-readable unit appropriate for the final FASTA size rather
    # than always reporting GiB, which is awkward for small smoke-test builds.
    fasta_bytes = final_fasta.stat().st_size

    if fasta_bytes >= 1024 ** 3:
        fasta_size = f"{fasta_bytes / (1024 ** 3):.2f} GiB"
    elif fasta_bytes >= 1024 ** 2:
        fasta_size = f"{fasta_bytes / (1024 ** 2):.2f} MiB"
    elif fasta_bytes >= 1024:
        fasta_size = f"{fasta_bytes / 1024:.2f} KiB"
    else:
        fasta_size = f"{fasta_bytes} B"

    summary_sections = [
        (
            "Reference build summary",
            [
                (
                    "Candidate species total",
                    f"{candidate_species_total:,}",
                ),
                (
                    "Taxonomically included species",
                    f"{taxonomic_species:,}",
                ),
                (
                    "Species passing metadata filters",
                    f"{eligible_species:,}",
                ),
                (
                    "Species represented by proteins",
                    f"{represented_species:,}",
                ),
                (
                    "Species without protein representation",
                    f"{species_without_proteins:,}",
                ),
                (
                    "Selected assemblies",
                    f"{selected_assemblies:,}",
                ),
            ],
        ),
        (
            "Assembly selection",
            selection_depth_metrics,
        ),
        (
            "Assembly coverage",
            [
                (
                    "Eligible strain groups",
                    f"{coverage.eligible_strain_groups:,}",
                ),
                (
                    "Represented strain groups",
                    f"{coverage.represented_strain_groups:,}",
                ),
                (
                    "Eligible isolate groups observed",
                    f"{coverage.eligible_isolate_groups:,}",
                ),
                (
                    "Isolate-only groups not included",
                    f"{coverage.isolate_only_groups_not_included:,}",
                ),
                (
                    "Unassigned groups not included",
                    f"{coverage.unassigned_groups_not_included:,}",
                ),
                (
                    "GCA assemblies excluded (--include-gca off)",
                    f"{coverage.gca_assemblies_excluded:,}",
                ),
                (
                    "Strain groups found only in excluded GCA",
                    f"{coverage.strain_groups_only_in_excluded_gca:,}",
                ),
                (
                    "Assemblies not selected by depth policy",
                    f"{coverage.assemblies_not_selected_by_policy:,}",
                ),
            ],
        ),
        (
            "Protein reference",
            [
                (
                    "Source protein occurrences",
                    f"{source_occurrences:,}",
                ),
                (
                    "Species-local unique sequences",
                    f"{species_local_unique:,}",
                ),
                (
                    "Final unique protein sequences",
                    f"{final_unique_sequences:,}",
                ),
                (
                    "Within-species duplicates collapsed",
                    f"{within_species_collapsed:,}",
                ),
                (
                    "Cross-species duplicates collapsed",
                    f"{cross_species_collapsed:,}",
                ),
                (
                    "Total exact duplicates collapsed",
                    f"{total_collapsed:,}",
                ),
                (
                    "Global deduplication rate",
                    f"{deduplication_rate:.2f}%",
                ),
                (
                    "Final FASTA size",
                    fasta_size,
                ),
            ],
        ),
    ]

    summary_label_width = max(
        len(f"{label}:")
        for _, metrics in summary_sections
        for label, _ in metrics
    )

    for section_index, (section_name, metrics) in enumerate(
        summary_sections
    ):
        if section_index:
            info("")

        info(f"{section_name}:")

        for label, value in metrics:
            label_text = f"{label}:"

            info(
                f"  {label_text:<{summary_label_width}}  "
                f"{value}"
            )


########################################################################################################
# Resume/update reconstruction helpers
########################################################################################################

def clear_inventory_for_safe_rebuild(state: StateDB) -> None:
    """Discard a partially built inventory before any protein processing began."""
    with state.conn:
        state.conn.execute("DELETE FROM assembly_failures")
        state.conn.execute("DELETE FROM failed_downloads")
        state.conn.execute("DELETE FROM assemblies")
        state.conn.execute("UPDATE species SET state='PENDING' WHERE final_taxonomic_status='INCLUDED' AND filter_status='PASS'")


def reconstruct_update_sets(
    state: StateDB,
    species_map: Mapping[int, SpeciesCandidate],
) -> tuple[
    dict[int, set[str]],
    dict[int, list[AssemblyRecord]],
    dict[int, list[AssemblyRecord]],
]:
    """Reconstruct update reuse/delta/full pools from a committed comparison."""

    full = load_assembly_pool_from_state(
        state,
        species_map.keys(),
    )

    reusable: dict[int, set[str]] = defaultdict(set)
    delta: dict[int, list[AssemblyRecord]] = defaultdict(list)

    for taxid, records in full.items():

        for assembly in records:

            row = state.conn.execute(
                """
                SELECT update_change, update_reason
                FROM assemblies
                WHERE accession=?
                """,
                (assembly.accession,),
            ).fetchone()

            if row is None:
                raise DataIntegrityError(
                    "Committed update comparison is missing assembly state for "
                    f"{assembly.accession}."
                )

            change = row["update_change"]
            reason = row["update_reason"]

            if (
                change == "KEPT"
                and reason == "UNCHANGED"
            ):
                reusable[taxid].add(
                    assembly.accession
                )

            elif change in {
                "ADDED",
                "UPDATED",
            }:
                delta[taxid].append(
                    assembly
                )

            else:
                raise DataIntegrityError(
                    "Committed update comparison contains an invalid provisional "
                    f"classification for eligible assembly {assembly.accession}: "
                    f"update_change={change!r}, update_reason={reason!r}."
                )

        delta[taxid].sort(
            key=quality_rank_key
        )

    return reusable, delta, full


########################################################################################################
# Dependency/version checks and staging safety
########################################################################################################

def check_runtime_dependencies() -> str:
    """Check required runtime dependencies and report optional ones."""

    # NCBI Datasets is required to build or update the protein reference.
    if shutil.which("datasets") is None:

        raise ConfigError(
            "Required NCBI Datasets CLI executable 'datasets' was not found in PATH."
        )

    # openpyxl is optional. Without it, the primary TSV metadata tables are
    # still written, but the convenience XLSX copies are skipped.
    if Workbook is None:

        warn(
            "Optional Python package 'openpyxl' was not found. "
            "XLSX convenience exports will be skipped; "
            "the primary TSV metadata tables will still be written."
        )

    # Record the installed NCBI Datasets version.
    result = run_capture(["datasets", "version"])

    # Some NCBI Datasets versions expose the version through --version instead.
    if result.returncode != 0:
        result = run_capture(["datasets", "--version"])

    # Stop if the executable exists but its version cannot be obtained.
    if result.returncode != 0:

        raise ExternalToolError(
            f"Could not obtain NCBI Datasets version: {result.stderr.strip()}"
        )

    return (result.stdout or result.stderr).strip().splitlines()[0] or "unknown"


def default_db_version() -> str:
    """Generate a default database version from the current local date and time."""
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def overwrite_staging_path(target: Path) -> Path:
    """Return the deterministic staging directory used for safe overwrite updates.

    Keeping this path construction in one function is important because the
    initial overwrite run and any later --resume must agree on exactly the same
    staging location.
    """
    return target.parent / f".{target.name}.FungiFunk_refdb_update_staging"


def finalize_manual_ncbi_recovery_staging(
    state: StateDB,
    work_root: Path,
    source_root: Path,
    config: BuildConfig,
    plan: ResourcePlan,
    *,
    explicit_targets: Sequence[ResolvedExplicitTarget],
) -> None:
    """Build and validate a recovered reference inside isolated staging.

    Publication is intentionally excluded. The caller must close StateDB
    before promoting the completed staging directory.
    """
    validate_manual_ncbi_selected_provenance(state)

    old = OldReference(
        source_root,
        immutable_source=True,
    )

    try:
        print(flush=True)

        state.set_global_state("GLOBAL_EXACT_DEDUPLICATION")

        info(
            "Rebuilding the canonical protein store from recovered proteins "
            "and existing source-reference sequences."
        )

        rebuild_global_canonical_store(
            state,
            work_root,
            old,
        )

        state.set_global_state(
            "GLOBAL_EXACT_DEDUPLICATION_COMPLETE"
        )

        protein_sources = int(
            state.conn.execute(
                "SELECT COUNT(*) FROM sources"
            ).fetchone()[0]
        )

        unique_proteins = int(
            state.conn.execute(
                "SELECT COUNT(*) FROM canonical_sequences"
            ).fetchone()[0]
        )

        if unique_proteins > protein_sources:
            raise DataIntegrityError(
                "Manual recovery produced more canonical sequences "
                "than protein source occurrences."
            )

        info(
            "Global exact deduplication complete: "
            f"{protein_sources:,} source records; "
            f"{unique_proteins:,} unique proteins; "
            f"{protein_sources - unique_proteins:,} duplicate occurrences."
        )

        print(flush=True)

        state.set_global_state("FINAL_OUTPUT_WRITING")

        write_final_fasta_and_protein_metadata(
            state,
            work_root,
        )

        write_protein_sources(
            state,
            work_root,
        )

        state.set_global_state(
            "FINAL_OUTPUT_WRITING_COMPLETE"
        )

        info("Recovered protein FASTA, metadata and provenance written.")

        print(flush=True)

        state.set_global_state("FINAL_REFERENCE_VALIDATION")

        validate_final_reference(
            state,
            work_root,
        )

        state.set_global_state(
            "FINAL_REFERENCE_VALIDATION_COMPLETE"
        )

        info("Recovered reference content validated.")

        print(flush=True)

        state.set_global_state("FINAL_REPORT_WRITING")

        write_all_final_reports(
            state,
            work_root,
            config,
            plan,
            explicit_targets=explicit_targets,
        )

        state.set_global_state(
            "FINAL_REPORT_WRITING_COMPLETE"
        )

        warn_incomplete_ncbi_protein_coverage(state)

        info("Recovered reference reports written.")

        print(flush=True)

        state.set_global_state("FINAL_CHECKPOINT")

        with state.conn:
            state.conn.execute(
                """
                UPDATE species
                SET state='DONE'
                WHERE state='PREPARED'
                """
            )

        state.set_meta("completed", now_iso())

        state.set_global_state(
            "FINAL_CHECKPOINT_COMPLETE"
        )

        state.set_global_state("DONE")

        # SQLite is part of the manifest: checkpoint before hashing.
        state.checkpoint(truncate=True)

        # The Bash wrapper requires this checkpoint to recognize a
        # completed reference. Preserve the same filesystem contract
        # for references published by manual NCBI recovery.
        write_resume_ready_marker(work_root)

        write_manifest(work_root)
        verify_manifest(work_root)
        write_completion_marker(work_root)

        # Validate both the Python integrity contract and the
        # filesystem checkpoints required by the Bash wrapper.
        verify_completed_reference(work_root)

        required_completion_files = (
            work_root / RESUME_READY_REL,
            work_root / DATABASE_DIR / FINAL_FASTA_NAME,
            work_root / MANIFEST_REL,
            work_root / COMPLETION_MARKER_REL,
        )

        missing_completion_files = [
            str(path)
            for path in required_completion_files
            if not path.is_file()
        ]

        if missing_completion_files:
            raise DataIntegrityError(
                "Manual NCBI recovery staging is missing required "
                "completion files:\n"
                + "\n".join(missing_completion_files)
            )

        append_python_log(
            work_root,
            "DONE manual_ncbi_recovery=PASS "
            "validation_and_manifest=PASS completion_marker=PASS",
        )

        info(
            "Manual NCBI recovery staging complete. "
            "Integrity manifest and completion marker verified."
        )

    finally:
        old.close()


def recovery_staging_path(target: Path) -> Path:
    """Return the isolated staging directory for manual NCBI recovery."""
    return target.parent / f".{target.name}.FungiFunk_refdb_recovery_staging"


def prepare_manual_recovery_output_snapshot(
    source: Path,
    staging: Path,
) -> None:
    """Preserve previously completed outputs in isolated recovery staging.

    Copy only published output directories. SQLite and biological config
    are handled separately, and the canonical sequence store is rebuilt.
    Never use hardlinks or symlinks to the original reference.
    """
    for directory_name in (
        DATABASE_DIR,
        METADATA_DIR,
        REPORTS_DIR,
    ):
        source_dir = source / directory_name

        if not source_dir.exists():
            continue

        if not source_dir.is_dir() or source_dir.is_symlink():
            raise DataIntegrityError(
                "Invalid original recovery output directory: "
                f"{source_dir}"
            )

        for path in source_dir.rglob("*"):
            if path.is_symlink():
                raise DataIntegrityError(
                    "Manual recovery cannot preserve linked output "
                    f"files safely: {path}"
                )

        shutil.copytree(
            source_dir,
            staging / directory_name,
            dirs_exist_ok=True,
            copy_function=shutil.copy2,
        )


def prepare_manual_recovery_snapshot(source: Path, staging: Path) -> None:
    """Copy the persistent recovery state without duplicating protein data.

    SQLite is backed up through its own API to obtain an independent,
    transactionally consistent database. The original canonical sequence
    store remains available through OldReference during final deduplication.
    """
    source_db = source / STATE_DB_REL
    staged_db = staging / STATE_DB_REL
    source_config = source / CONFIG_JSON_REL
    source_snapshots = source / CONFIG_DIR

    if not source_db.is_file():
        raise DataIntegrityError(
            f"Source reference SQLite state is missing: {source_db}"
        )

    if not source_config.is_file():
        raise DataIntegrityError(
            f"Source reference configuration is missing: {source_config}"
        )

    if not source_snapshots.is_dir():
        raise DataIntegrityError(
            f"Source reference configuration snapshots are missing: "
            f"{source_snapshots}"
        )

    staged_db.parent.mkdir(parents=True, exist_ok=True)

    # Open the completed source database read-only. Never reuse its
    # connection for writes or copy only the main file while WAL is active.
    source_conn = sqlite3.connect(
        source_db.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
        timeout=60.0,
    )
    staged_conn = None

    try:
        staged_conn = sqlite3.connect(staged_db, timeout=60.0)
        source_conn.backup(staged_conn)
        staged_conn.commit()
    finally:
        if staged_conn is not None:
            staged_conn.close()
        source_conn.close()

    shutil.copy2(
        source_config,
        staging / CONFIG_JSON_REL,
    )

    shutil.copytree(
        source_snapshots,
        staging / CONFIG_DIR,
        dirs_exist_ok=True,
    )


def choose_work_and_source_roots(
    config: BuildConfig,
) -> tuple[Path, Path | None, Path | None]:
    """Return (work_root, source_root, destructive_target).

    Destructive update is implemented as a sibling staging directory. The
    existing target remains untouched until the staging reference is complete,
    manifest-validated, and ready for safe promotion.
    """

    target = Path(config.outdir)

    if config.run_mode == "retry-ncbi-failures":

        source = Path(config.source_refdb or config.outdir)
        staging = recovery_staging_path(source)

        backup = source.parent / (
            f".{source.name}.FungiFunk_refdb_previous_backup"
        )

        # Never proceed while a previous replacement backup exists.
        # The public reference may need manual inspection or restoration.
        if backup.exists() or backup.is_symlink():
            raise ConfigError(
                "A previous safe-replacement backup exists:\n"
                f"  {backup}\n\n"
                "Inspect the backup and the original reference before "
                "starting another manual NCBI recovery."
            )

        if not source.is_dir():
            raise ConfigError(
                "Manual NCBI recovery requires an existing completed "
                f"reference directory: {source}"
            )

        # Recovery runs are never resumed. An existing staging path must
        # be inspected and removed manually before starting another run.
        if staging.exists() or staging.is_symlink():
            raise ConfigError(
                "A previous manual NCBI recovery staging path exists:\n"
                f"  {staging}\n\n"
                "Check that no recovery process is still running. "
                "Inspect and remove the incomplete staging directory "
                "manually before retrying."
            )

        # Validate the original reference before creating any working files.
        verify_completed_reference(source)

        # Exclusive directory creation also rejects a concurrent attempt
        # that created the same staging path after our initial check.
        try:
            staging.mkdir()
        except FileExistsError as exc:
            raise ConfigError(
                "Another manual NCBI recovery staging path already exists: "
                f"{staging}"
            ) from exc

        initialize_output_layout(staging)

        # Prepare an independent persistent state before any recovery work.
        # A failed preparation leaves staging for explicit user inspection;
        # the original completed reference is never modified.
        prepare_manual_recovery_snapshot(source, staging)
        prepare_manual_recovery_output_snapshot(source, staging)

        return staging, source, source

    if config.run_mode == "overwrite-with-updated-version":

        source = target

        staging = overwrite_staging_path(target)

        if staging.exists():

            if (staging / MARKER_REL).is_file():

                if (staging / RESUME_READY_REL).is_file():

                    raise ConfigError(
                        "A previous safe-update staging build already exists and has "
                        "a resumable persistent state:\n"
                        f"  {staging}\n\n"
                        "Continue that existing staging build instead with:\n"
                        f"  FungiFunk_refdb.sh --resume --outdir {staging}"
                    )

                raise ConfigError(
                    "A previous FungiFunk_refdb safe-update staging directory exists, "
                    "but the previous run stopped before a resumable state was created:\n"
                    f"  {staging}\n\n"
                    "--resume cannot be used for that staging directory. Correct the "
                    "original problem, remove the incomplete staging directory "
                    "manually, and rerun --overwrite-with-updated-version.\n\n"
                    "FungiFunk_refdb will not remove the incomplete staging directory "
                    "automatically."
                )

            raise ConfigError(
                "Refusing to use an existing non-FungiFunk staging path: "
                f"{staging}. Move/remove it manually first."
            )

        staging.mkdir(parents=True)

        initialize_output_layout(staging)

        # The current overwrite invocation was logged before staging started.
        # Copy that log entry into the staging directory so it is preserved
        # when the staged database replaces the old reference.
        old_wrapper_log = (
            source / LOGS_DIR / "build_info.log"
        )

        if old_wrapper_log.is_file():
            shutil.copy2(
                old_wrapper_log,
                staging / LOGS_DIR / "build_info.log",
            )

        return staging, source, target

    work = target

    source = (
        Path(config.source_refdb)
        if config.source_refdb
        else None
    )

    initialize_output_layout(work)

    return work, source, None


def safe_replace_completed_directory(staging: Path, target: Path) -> None:
    """Atomically-ish replace a directory on one filesystem with rollback.

    POSIX directory renames are atomic individually. Two renames are needed, so
    we keep the old DB under a private backup name until the new one occupies the
    public target path. If the second rename fails, the old name is restored.
    """
    if staging.parent.resolve() != target.parent.resolve():
        raise DataIntegrityError("Safe overwrite staging and target must be sibling directories on the same filesystem.")
    backup = target.parent / f".{target.name}.FungiFunk_refdb_previous_backup"
    if backup.exists():
        raise DataIntegrityError(f"Safety backup path already exists and will not be overwritten: {backup}")
    fsync_dir(target.parent)
    os.replace(target, backup)
    try:
        os.replace(staging, target)
        fsync_dir(target.parent)
    except Exception:
        # Best-effort rollback to the known-valid old reference.
        if not target.exists() and backup.exists():
            os.replace(backup, target)
            fsync_dir(target.parent)
        raise
    # Only after the new validated reference is live under the public target
    # path we attempt to remove the previous complete copy.
    #
    # Failure to delete this backup is NOT an update failure: at this point the
    # new database has already been safely promoted and the old database still
    # exists as an additional safety copy. Report the cleanup problem, but do
    # not falsely mark the completed update as failed.
    try:
        shutil.rmtree(backup)
        fsync_dir(target.parent)
    except OSError as exc:
        warn(
            "The updated reference was successfully installed, but the previous "
            f"reference backup could not be removed automatically: {backup}\n"
            f"Reason: {exc}\n"
            "The backup may be inspected and removed manually when convenient."
        )


########################################################################################################
# Curation pause handling
########################################################################################################

def write_curation_conflicts(work_root: Path, conflict: CurationConflict) -> Path:
    path = work_root / REPORTS_DIR / "curation_conflicts.tsv"
    all_fields: list[str] = ["Type", "Species", "Taxid"]
    for row in conflict.conflicts:
        for key in row:
            if key not in all_fields:
                all_fields.append(key)
    write_tsv(path, all_fields, conflict.conflicts)
    return path


def pause_for_curation_fix(
    state: StateDB,
    work_root: Path,
    conflict: CurationConflict,
) -> None:
    """Persist curation-conflict details and pause before assembly selection."""

    report = write_curation_conflicts(
        work_root,
        conflict,
    )

    state.set_meta(
        "curation_conflict_message",
        str(conflict),
    )

    state.set_meta(
        "curation_conflict_report",
        str(report),
    )

    state.set_global_state(
        "WAITING_FOR_CURATION_FIX"
    )

    error(
        "CURATION CONFLICT: "
        + str(conflict)
    )

    error(
        f"Details: {report}"
    )


########################################################################################################
# Main orchestration
########################################################################################################

def record_manual_ncbi_recovery_wrapper_action(action: str) -> None:
    """Record the intended recovery action in the external wrapper log.

    The wrapper uses this receipt only when Python exits successfully.
    The log is outside the original reference during recovery.
    """
    if action not in {"PROMOTE", "NO_CHANGE"}:
        raise DataIntegrityError(
            f"Invalid manual recovery wrapper action: {action!r}"
        )

    log_path = os.environ.get("FUNGIFUNK_WRAPPER_RUN_INFO_LOG")

    if not log_path:
        return

    with Path(log_path).open("a", encoding="utf-8") as handle:
        handle.write(f"Manual_recovery_action: {action}\n")


def report_manual_ncbi_recovery_remaining_failures(
    state: StateDB,
    source_root: Path,
) -> None:
    """Report unresolved NCBI failures and the exact manual retry command."""
    import shlex

    pending = pending_manual_ncbi_recovery_accessions(state)

    if not pending:
        info(
            "Manual NCBI recovery: no unresolved eligible "
            "NCBI retrieval failures remain."
        )
        return

    affected_species = set(pending)

    represented_species = {
        int(row["species_taxid"])
        for row in state.conn.execute(
            """
            SELECT DISTINCT species_taxid
            FROM assemblies
            WHERE selected=1
              AND protein_fasta_available=1
            """
        )
    }

    represented_affected = affected_species & represented_species
    unrepresented_affected = affected_species - represented_species

    remaining_count = sum(
        len(accessions)
        for accessions in pending.values()
    )

    wrapper = Path(__file__).resolve().with_suffix(".sh")

    retry_command = (
        "bash "
        + shlex.quote(str(wrapper))
        + " --retry-ncbi-failures --source-refdb "
        + shlex.quote(str(source_root))
    )

    warn(
        "Manual NCBI recovery finished with unresolved retrieval failures.\n"
        f"         Remaining assemblies: {remaining_count:,}\n"
        f"         Affected species: {len(affected_species):,}\n"
        f"         Still represented by usable assemblies: "
        f"{len(represented_affected):,}\n"
        f"         Without usable selected assemblies: "
        f"{len(unrepresented_affected):,}\n"
        "         These failures remain recorded for a later retry.\n"
        "         To retry:\n"
        f"         {retry_command}"
    )


def orchestrate_manual_ncbi_recovery(
    config: BuildConfig,
) -> tuple[Path, Path | None]:
    """Recover NCBI failures without changing the original until promotion."""
    work_root, source_root, destructive_target = (
        choose_work_and_source_roots(config)
    )

    if (
        source_root is None
        or destructive_target is None
        or source_root != destructive_target
        or work_root != recovery_staging_path(source_root)
    ):
        raise DataIntegrityError(
            "Manual NCBI recovery requires an isolated sibling staging "
            "directory and the original completed reference as its target."
        )

    info(
        "Manual NCBI recovery: working in isolated staging "
        f"{work_root}"
    )
    info(f"Original completed reference: {source_root}")

    # All mutations occur in the independent staging SQLite.
    # Never open the source reference through StateDB.
    with StateDB(work_root / STATE_DB_REL) as state:
        if state.global_state() != "DONE":
            raise DataIntegrityError(
                "Manual NCBI recovery staging does not contain a "
                "completed source-state snapshot."
            )

        result = run_manual_ncbi_recovery_processing(
            state,
            config,
            work_root,
            source_root,
        )

        if result.changed:
            if result.resource_plan is None:
                raise DataIntegrityError(
                    "Manual NCBI recovery produced proteins without "
                    "a finalization resource plan."
                )

            finalize_manual_ncbi_recovery_staging(
                state,
                work_root,
                source_root,
                config,
                result.resource_plan,
                explicit_targets=result.explicit_targets,
            )

        # Read-only warning: report remaining failures whether this pass
        # recovered some assemblies or none.
        report_manual_ncbi_recovery_remaining_failures(
            state,
            source_root,
        )

    # The SQLite connection is now closed. The original reference
    # remains untouched until the final safe directory promotion.
    if result.changed:
        # Validate again after StateDB has closed and released its WAL.
        verify_completed_reference(work_root)

        print(flush=True)
        print_section_banner("SAFE RECOVERY PROMOTION")

        info(
            "Recovered staging reference validated. "
            "Publishing the completed recovery."
        )

        # Report the intended action before promotion. The wrapper trusts
        # this receipt only if Python subsequently exits successfully.
        record_manual_ncbi_recovery_wrapper_action("PROMOTE")

        safe_replace_completed_directory(
            work_root,
            destructive_target,
        )

        info(
            "Manual NCBI recovery complete: "
            f"{result.recovered_count:,} assemblies recovered. "
            f"Updated reference: {destructive_target}"
        )

        return destructive_target, destructive_target

    # No proteins recovered: this is a successfully completed no-op,
    # not an interrupted build. Remove only this run's staging.
    # Unexpected errors above deliberately leave staging untouched.
    record_manual_ncbi_recovery_wrapper_action("NO_CHANGE")

    shutil.rmtree(work_root)
    fsync_dir(work_root.parent)

    info(
        "Manual NCBI recovery completed without new proteins. "
        "The original reference is unchanged and can be retried."
    )

    return source_root, None


def orchestrate(ns: argparse.Namespace) -> tuple[Path, Path | None]:

    set_console_timestamps(
        bool(
            getattr(
                ns,
                "timestamps",
                False,
            )
        )
    )

    print_section_banner(
        "STARTUP_AND_CONFIGURATION"
    )

    info(
        "Checking runtime dependencies and resolving run configuration."
    )

    datasets_version = check_runtime_dependencies()

    # The wrapper ownership marker may exist even when a previous run failed
    # before enough persistent state was committed for safe recovery.
    #
    # Check the dedicated resume-ready marker BEFORE resolve_config(), because
    # resume configuration itself depends on resolved_config.json.
    if getattr(ns, "resume", False):

        resume_root = Path(ns.outdir).expanduser().resolve()

        if not (resume_root / MARKER_REL).is_file():
            raise ConfigError(
                "The --resume target is not recognized as a FungiFunk_refdb "
                f"build: {resume_root}"
            )

        if not (resume_root / RESUME_READY_REL).is_file():
            raise ConfigError(
                "The previous run stopped before a resumable FungiFunk_refdb "
                "state was created."
            )

    config = resolve_config(ns)

    if config.run_mode == "retry-ncbi-failures":
        if not config.db_version:
            raise DataIntegrityError(
                "Manual NCBI recovery must inherit the original "
                "database version from the completed reference."
            )

        update_wrapper_run_info_db_version(
            config.db_version
        )

        return orchestrate_manual_ncbi_recovery(config)

    if config.run_mode != "resume" and not config.db_version:
        config.db_version = default_db_version()

    update_wrapper_run_info_db_version(
        config.db_version,
    )

    work_root, source_root, destructive_target = choose_work_and_source_roots(config)
    initialize_output_layout(work_root)

    info(
        f"Run configuration resolved: mode={config.run_mode}; "
        f"working directory={work_root}"
    )

    if source_root is not None:
        info(
            f"Source reference: {source_root}"
        )

    state_path = work_root / STATE_DB_REL

    with StateDB(state_path) as state:

        resume_biological_options: list[str] = []
        resume_original_state: str | None = None

        if config.run_mode == "resume":

            current_state = state.global_state()
            resume_original_state = current_state

            completion_marker = work_root / COMPLETION_MARKER_REL

            if completion_marker.is_file() and current_state != "DONE":
                raise DataIntegrityError(
                    "The final completion marker exists but the persistent build state "
                    f"is {current_state!r} instead of 'DONE'. This is an inconsistent "
                    "completion state and automatic recovery has been stopped."
                )

            if current_state == "DONE":

                if completion_marker.is_file():

                    # Validate the final commit token before trusting it.
                    verify_completed_reference(work_root)

                    origin_hint = str(
                        state.get_meta(
                            "build_origin_mode",
                            "",
                        )
                    )

                    source_hint = (
                        Path(config.source_refdb)
                        if config.source_refdb
                        else None
                    )

                    is_completed_overwrite_staging = (
                        origin_hint == "overwrite-with-updated-version"
                        and source_hint is not None
                        and work_root.resolve()
                        == overwrite_staging_path(source_hint).resolve()
                    )

                    if not is_completed_overwrite_staging:

                        raise ConfigError(
                            "This reference is already complete and its final integrity "
                            "checkpoint has been successfully verified."
                        )

                    info(
                        "Resume detected a fully validated overwrite staging reference "
                        "whose final promotion may have been interrupted. The staged "
                        "completion token will be reopened so the safe overwrite workflow "
                        "can continue."
                    )

                    # This is an internal transactional token, not user data. Removing it
                    # deliberately reopens the staging build before any persistent content
                    # can be modified again. A new token will be written only after the
                    # final manifest is successfully regenerated and verified.
                    completion_marker.unlink()

                else:

                    info(
                        "The persistent build state is DONE, but the final completion "
                        "marker is absent. This means final manifest commit did not finish "
                        "successfully. The build will be resumed and finalization will be "
                        "reconstructed safely."
                    )

            resume_biological_options = explicit_resume_biological_options(ns)

            if resume_biological_options:

                if (
                    current_state
                    not in PRE_ASSEMBLY_BIOLOGICAL_EDITABLE_STATES
                ):

                    rendered = "\n".join(
                        f"  {option}"
                        for option in resume_biological_options
                    )

                    raise ConfigError(
                        "Biological configuration cannot be changed because "
                        "ASSEMBLY_SEARCH_AND_SELECTION has already started "
                        "for this build.\n\n"
                        f"Current build state: {current_state}\n\n"
                        "Biological configuration determines which fungal taxa "
                        "and genome assemblies can contribute proteins to the "
                        "reference database. See BIOLOGICAL CONFIGURATION "
                        "in --help for the complete option list.\n\n"
                        "The following biological configuration option(s) "
                        "or reset action were supplied:\n"
                        f"{rendered}"
                    )

                if (
                    config.reset_biological_config
                    and not config.seed_taxa
                    and not config.include_explicit_targets
                ):

                    raise ConfigError(
                        "--reset-biological-config with a pre-assembly --resume "
                        "requires at least one of --seed-taxa FILE or "
                        "--include-explicit-targets FILE.\n\n"
                        "The reset returns biological configuration to clean "
                        "defaults, then applies only the biological inputs "
                        "and options supplied in the resume command."
                    )

                info(
                    "Pre-assembly resume: biological configuration may still "
                    "be changed because ASSEMBLY_SEARCH_AND_SELECTION "
                    "has not started."
                )

                if config.reset_biological_config:

                    info(
                        "Biological configuration reset requested: clean "
                        "biological defaults will be applied before the explicit "
                        "biological inputs/options from this resume command."
                    )

                else:

                    info(
                        "Explicit biological configuration options supplied: "
                        "settings not supplied are preserved; supplied settings "
                        "replace their previous values."
                    )

                # Invalidate previously committed curation BEFORE replacing
                # snapshots/configuration. If a later copy/write fails, the next
                # resume will know that old curation must not be reused.
                state.set_global_state("INITIALIZING_COMPLETE")

        else:

            # Separate startup/configuration from initialization before the
            # new-build state transition announces the INITIALIZING banner.
            print(flush=True)

            state.set_meta("build_origin_mode", config.run_mode)
            state.set_global_state("INITIALIZING")

        # Recover the mode that originally created this build.
        #
        # For a new build this is the current run mode. For --resume it is read
        # from the persistent SQLite state created during the original run.
        origin_mode = str(
            state.get_meta("build_origin_mode", config.run_mode)
        )

        # A resumed overwrite update must preserve exactly the same safety
        # guarantees as the original --overwrite-with-updated-version run.
        #
        # In particular:
        #
        #   1. The previous valid reference remains untouched.
        #   2. Work continues only inside the known staging directory.
        #   3. The staging database must complete and pass validation.
        #   4. Only then may it replace the previous reference.
        #
        # source_root points to the original valid reference because that path
        # was stored in resolved_config.json when the overwrite update started.
        if (
            config.run_mode == "resume"
            and origin_mode == "overwrite-with-updated-version"
        ):

            if source_root is None:
                raise DataIntegrityError(
                    "This build originated as --overwrite-with-updated-version, "
                    "but the original reference path is missing from the saved state."
                )

            expected_staging = overwrite_staging_path(source_root)

            # When resuming an overwrite update, only allow the final replacement if
            # the current directory is exactly the expected staging directory for the
            # original reference saved in the build state. Never guess which existing
            # reference should be replaced from an arbitrary resume directory.
            if work_root.resolve() != expected_staging.resolve():
                raise DataIntegrityError(
                    "Refusing to complete resumed overwrite because the current "
                    "directory is not the expected FungiFunk_refdb staging path.\n"
                    f"Current staging: {work_root}\n"
                    f"Expected staging: {expected_staging}\n"
                    f"Original reference: {source_root}"
                )

            if not source_root.is_dir():
                raise DataIntegrityError(
                    "The original reference database required to complete the "
                    f"resumed overwrite no longer exists: {source_root}"
                )

            # Re-enable the final safe replacement step.
            #
            # destructive_target remains None for ordinary --resume runs.
            destructive_target = source_root

            info(
                "Resume detected for an interrupted overwrite update. "
                "The existing reference will remain untouched until the staging "
                f"build has passed final validation: {source_root}"
            )
        config.ncbi_datasets_version = datasets_version
        config.ncbi_api_key_detected = bool(os.environ.get("NCBI_API_KEY"))
        config.ncbi_request_rate = NCBI_RPS_WITH_KEY if config.ncbi_api_key_detected else NCBI_RPS_WITHOUT_KEY

        # Resume does not enter INITIALIZING through set_global_state(), so add
        # the visual separation immediately before announcing the reused phase.
        if config.run_mode == "resume":
            print(flush=True)

        # On resume, show the initialization section without rewriting SQLite.
        # On a new build, announce_phase() suppresses a duplicate banner.
        state.announce_phase("INITIALIZING")

        info(
            "Preparing configuration snapshots and persistent build state."
        )

        # Record the explicit command biology and the configuration that serves as
        # the inheritance base before any resumable snapshots can be replaced.
        append_wrapper_command_and_inherited_biological_config(
            ns,
            config,
            work_root,
            source_root,
        )

        # Config snapshots are selected before saving resolved configuration so
        # every subsequent phase can rely only on immutable copies under config/.
        (
            seed_path,
            exclusion_path,
            filter_path,
            include_explicit_targets_path,
            exclude_explicit_targets_path,
        ) = resolve_config_snapshots(
            config,
            work_root,
            source_root,
            state,
        )

        # Parse and syntactically validate explicit targets before any expensive
        # taxonomy or assembly-discovery work. NCBI-backed target resolution and
        # biological selection integration are performed in later phases.
        include_explicit_targets = parse_explicit_targets(
            include_explicit_targets_path,
            action="include",
            warn_if_empty=config.include_explicit_targets is not None,
        )

        exclude_explicit_targets = parse_explicit_targets(
            exclude_explicit_targets_path,
            action="exclude",
            warn_if_empty=config.exclude_explicit_targets is not None,
        )

        detect_explicit_target_conflicts(
            include_explicit_targets,
            exclude_explicit_targets,
        )

        # At this point both the resolved BuildConfig and the exact immutable TSV
        # snapshots are known, so this is the authoritative effective configuration.
        append_wrapper_effective_biological_config(
            config,
            seed_snapshot=seed_path,
            exclusion_snapshot=exclusion_path,
            filter_snapshot=filter_path,
            include_explicit_targets_snapshot=include_explicit_targets_path,
            exclude_explicit_targets_snapshot=exclude_explicit_targets_path,
        )

        save_json_atomic(
            work_root / CONFIG_JSON_REL,
            config.to_jsonable(),
        )
        state.set_meta("db_version", config.db_version or ".")
        state.set_meta("ncbi_datasets_version", datasets_version)
        state.set_meta("ncbi_api_key_detected", config.ncbi_api_key_detected)
        state.set_meta("ncbi_request_rate", config.ncbi_request_rate)
        append_python_log(work_root, f"START mode={config.run_mode} db_version={config.db_version}")

        # From this point onward a new build has all persistent information
        # required for safe recovery: SQLite state, exact configuration
        # snapshots, resolved_config.json, and essential build metadata.
        if config.run_mode != "resume":

            state.set_global_state("INITIALIZING_COMPLETE")
            write_resume_ready_marker(work_root)

            info(
                "Initial persistent checkpoint complete. This build now has a "
                "resumable state and may be continued with --resume if a later "
                "step is interrupted."
            )

        if source_root is not None and config.run_mode != "resume":
            # Verify the source as part of initialization, before the
            # visual separation leading into taxonomy curation.
            info(f"Verifying source reference manifest before reuse: {source_root}")
            verify_completed_reference(source_root)

        client = NCBIClient(config.ncbi_request_rate, work_root / LOGS_DIR)

        resolved_explicit_targets_path = (
            work_root
            / CONFIG_DIR
            / RESOLVED_EXPLICIT_TARGETS_JSON
        )

        # ------------------------------------------------------------------
        # 1. Curated taxonomy and species-level filtering
        # ------------------------------------------------------------------
        prior_state = state.global_state()

        need_curation = (
            config.run_mode != "resume"
            or prior_state not in CURATION_COMMITTED_STATES
        )

        excluded_rows: list[dict[str, Any]] = []

        resolved_include_targets_for_inventory: Sequence[
            ResolvedExplicitTarget
        ] = ()

        resolved_exclude_targets_for_selection: Sequence[
            ResolvedExplicitTarget
        ] = ()

        if need_curation:

            if config.run_mode == "resume":

                if resume_biological_options:

                    info(
                        "Biological configuration changed during pre-assembly "
                        "resume. Clearing previous pre-assembly state and "
                        "rebuilding taxonomy curation from the beginning."
                    )

                elif prior_state == "WAITING_FOR_CURATION_FIX":

                    info(
                        "Re-running pre-assembly taxonomy curation after user "
                        "correction."
                    )

                else:

                    info(
                        "The previous taxonomy curation was not committed as "
                        "complete. Clearing partial pre-assembly state and "
                        "rebuilding taxonomy curation from the beginning."
                    )

                state.clear_preassembly_data()

            # Separate initialization from taxonomy after any resume-specific
            # initialization messages have been printed.
            print(flush=True)

            # Enter taxonomy curation before printing its introductory message.
            state.set_global_state("TAXONOMY_CURATION")

            target_only_curation = (
                config.seed_taxa is None
                and bool(include_explicit_targets)
            )

            curation_inputs: list[str] = []

            if config.seed_taxa is not None:
                curation_inputs.append("seed taxa")

            if config.exclude_taxa is not None:
                curation_inputs.append("excluded taxa")

            if include_explicit_targets:
                curation_inputs.append(
                    "explicit inclusion targets"
                )

            if exclude_explicit_targets:
                curation_inputs.append(
                    "explicit exclusion targets"
                )

            if config.metadata_filters is not None:
                curation_inputs.append(
                    "metadata filters"
                )

            if not curation_inputs:
                curation_input_text = (
                    "the biological configuration inherited "
                    "from the source reference"
                )
            elif len(curation_inputs) == 1:
                curation_input_text = curation_inputs[0]
            elif len(curation_inputs) == 2:
                curation_input_text = (
                    f"{curation_inputs[0]} and "
                    f"{curation_inputs[1]}"
                )
            else:
                curation_input_text = (
                    ", ".join(curation_inputs[:-1])
                    + f" and {curation_inputs[-1]}"
                )

            info(
                "Building the curated fungal species set using "
                f"{curation_input_text}."
            )

            all_resolved_explicit_targets = resolve_explicit_targets(
                client,
                [
                    *include_explicit_targets,
                    *exclude_explicit_targets,
                ],
            )

            include_target_count = len(
                include_explicit_targets
            )

            resolved_include_explicit_targets = (
                all_resolved_explicit_targets[
                    :include_target_count
                ]
            )

            resolved_exclude_explicit_targets = (
                all_resolved_explicit_targets[
                    include_target_count:
                ]
            )

            detect_resolved_explicit_target_conflicts(
                resolved_include_explicit_targets,
                resolved_exclude_explicit_targets,
            )

            report_resolved_explicit_targets(
                resolved_include_explicit_targets,
                resolved_exclude_explicit_targets,
            )

            save_resolved_explicit_targets(
                resolved_explicit_targets_path,
                all_resolved_explicit_targets,
            )

            resolved_include_targets_for_inventory = (
                resolved_include_explicit_targets
            )

            resolved_exclude_targets_for_selection = (
                resolved_exclude_explicit_targets
            )

            seed_rules = parse_seed_rules(
                seed_path,
                client,
                allow_no_data_rows=target_only_curation,
            )

            exclusion_rules = parse_exclusion_rules(
                exclusion_path,
                client,
                warn_if_empty=config.exclude_taxa is not None,
            )

            metadata_rules = parse_metadata_filters(
                filter_path,
                warn_if_empty=config.metadata_filters is not None,
            )
            all_rules = cli_filter_rules(config) + metadata_rules
            detect_obvious_filter_contradictions(all_rules)
            write_resolved_filters(work_root / METADATA_DIR / "metadata_filters_resolved.tsv", all_rules)
            try:
                included, excluded_rows, _, all_candidates, seed_counts, exclusion_counts = build_curated_species(
                    client,
                    seed_rules,
                    exclusion_rules,
                    resolved_include_explicit_targets,
                )
            except CurationConflict as conflict:
                pause_for_curation_fix(state, work_root, conflict)
                raise
            write_resolved_taxon_rules(
                work_root / METADATA_DIR / "fungal_seed_taxa_resolved.tsv",
                seed_rules, seed_counts, exclusion=False,
            )
            write_resolved_taxon_rules(
                work_root / METADATA_DIR / "fungal_exclusion_taxa_resolved.tsv",
                exclusion_rules, exclusion_counts, exclusion=True,
            )
            write_excluded_taxa_report(work_root, excluded_rows)
            insert_species_rows(state, all_candidates)
            species_map = apply_species_filters(
                state,
                included,
                all_rules,
                required_taxids={
                    target.species_taxid
                    for target
                    in resolved_include_explicit_targets
                },
            )
            state.set_global_state("TAXONOMY_CURATION_COMPLETE")

            # Frame the taxonomy summary as a note within this work phase.
            print_section_separator()

            info(
                f"Taxonomy curation complete: {len(all_candidates)} candidate species, "
                f"{len(included)} taxonomically retained, {len(species_map)} passing species metadata filters."
            )

            print_section_separator()
            print(flush=True)
        else:
            all_resolved_explicit_targets = (
                load_resolved_explicit_targets(
                    resolved_explicit_targets_path,
                    [
                        *include_explicit_targets,
                        *exclude_explicit_targets,
                    ],
                )
            )

            include_target_count = len(
                include_explicit_targets
            )

            resolved_include_explicit_targets = (
                all_resolved_explicit_targets[
                    :include_target_count
                ]
            )

            resolved_exclude_explicit_targets = (
                all_resolved_explicit_targets[
                    include_target_count:
                ]
            )

            detect_resolved_explicit_target_conflicts(
                resolved_include_explicit_targets,
                resolved_exclude_explicit_targets,
            )

            report_resolved_explicit_targets(
                resolved_include_explicit_targets,
                resolved_exclude_explicit_targets,
            )

            resolved_include_targets_for_inventory = (
                resolved_include_explicit_targets
            )

            resolved_exclude_targets_for_selection = (
                resolved_exclude_explicit_targets
            )

            metadata_rules = parse_metadata_filters(
                filter_path,
                warn_if_empty=config.metadata_filters is not None,
            )
            all_rules = cli_filter_rules(config) + metadata_rules
            detect_obvious_filter_contradictions(all_rules)
            species_map = load_species_map_from_state(state, only_filter_pass=True)

            # Separate initialization from the reused taxonomy phase.
            print(flush=True)

            # Announce the reused stage without modifying its checkpoint.
            state.announce_phase("TAXONOMY_CURATION")

            info(
                f"Resume: reusing committed taxonomy/species state "
                f"for {len(species_map)} species."
            )

            print(flush=True)

        # ------------------------------------------------------------------
        # 2. Assembly search, eligibility filtering and candidate selection
        # ------------------------------------------------------------------
        prior_state = state.global_state()
        assembly_count = int(
            state.conn.execute("SELECT COUNT(*) FROM assemblies").fetchone()[0]
        )

        if (
            config.run_mode == "resume"
            and prior_state in ASSEMBLY_SELECTION_COMMITTED_STATES
        ):
            # Display the reused stage without modifying persistent state.
            state.announce_phase("ASSEMBLY_SEARCH_AND_SELECTION")

            full_pools = load_assembly_pool_from_state(
                state,
                species_map.keys(),
            )

            info(
                "Resume: reusing committed assembly search and selection state."
            )

        else:
            # If an inventory query crashed midway, no protein processing has
            # started yet. Rebuilding the inventory from scratch avoids ambiguous
            # partial per-species inventories and duplicate failure rows.
            if assembly_count:
                clear_inventory_for_safe_rebuild(state)

            state.set_global_state("ASSEMBLY_SEARCH_AND_SELECTION")

            info(
                "Preparing genome assembly candidates for the retained species."
            )

            full_pools = build_assembly_inventory(
                state,
                client,
                species_map,
                config,
                all_rules,
                resolved_include_targets_for_inventory,
            )

            state.set_global_state(
                "ASSEMBLY_SEARCH_AND_SELECTION_COMPLETE"
            )

        inventory_total = int(
            state.conn.execute(
                "SELECT COUNT(*) FROM assemblies"
            ).fetchone()[0]
        )

        eligible_assemblies = sum(
            len(pool)
            for pool in full_pools.values()
        )

        species_with_eligible_assemblies = sum(
            1
            for pool in full_pools.values()
            if pool
        )

        # Frame the assembly summary after rebuilding or reusing the inventory.
        print_section_separator()

        info(
            f"Assembly candidates ready: {inventory_total:,} assemblies inventoried; "
            f"{eligible_assemblies:,} eligible candidate assemblies across "
            f"{species_with_eligible_assemblies:,}/{len(species_map):,} species."
        )

        print_section_separator()

        print(flush=True)

        selection_constraints_by_species = (
            build_assembly_selection_constraints_by_species(
                species_map,
                resolved_include_targets_for_inventory,
                resolved_exclude_targets_for_selection,
            )
        )

        # ------------------------------------------------------------------
        # 3. Resource planning before protein downloads and merging
        # ------------------------------------------------------------------
        print_section_banner("RESOURCE ASSESSMENT AND WORKER PLANNING")
        
        temp_base = Path(config.tmpdir) if config.tmpdir else work_root / TMP_DIR / "workers"

        info(
            "Planning safe parallel resources for protein processing."
        )

        plan = calculate_resource_plan(config, work_root, temp_base, source_root)
        write_resource_usage(work_root / REPORTS_DIR / "resource_usage.tsv", plan, config)

        # Distinguish the selected worker count from the safe resource limit.
        # On resume, config.workers may be inherited from the previous run.
        info(
            f"Worker allocation: {plan.used_workers} selected "
            f"(setting={config.workers}); safe maximum={plan.safe_workers} "
            f"(limiting factors: {plan.limiting_factors})."
        )

        print(flush=True)

        # ------------------------------------------------------------------
        # 4. Create vs incremental update processing
        # ------------------------------------------------------------------

        is_update = origin_mode in {
            "create-new-updated-version",
            "overwrite-with-updated-version",
        } or (
            source_root is not None
            and config.run_mode == "resume"
        )

        old: OldReference | None = None

        try:
            if is_update:
                if source_root is None:
                    raise DataIntegrityError(
                        "Update state has no source reference path for safe reuse."
                    )

                old = OldReference(source_root)

                if config.run_mode == "resume":
                    # Source was verified on original update startup, but recheck
                    # it on resume because an externally modified source cannot be
                    # trusted for sequence reuse.
                    verify_completed_reference(source_root)

                update_comparison_state = state.global_state()

                if (
                    config.run_mode == "resume"
                    and update_comparison_state
                    in UPDATE_COMPARISON_COMMITTED_STATES
                ):

                    # Show the reused comparison without rewriting its checkpoint.
                    state.announce_phase(
                        "UPDATE_COMPARISON_AND_SELECTION"
                    )

                    info(
                        "Resume: reusing the previously committed update "
                        "comparison and selection."
                    )

                    reusable, delta_pools, full_pools = reconstruct_update_sets(
                        state,
                        species_map,
                    )

                else:

                    # A previous interrupted comparison is deliberately rebuilt
                    # from scratch. classify_update() is transactional, so an
                    # interrupted comparison is not treated as committed.
                    state.set_global_state(
                        "UPDATE_COMPARISON_AND_SELECTION"
                    )

                    # Explain the work after announcing its section.
                    info(
                        "Comparing the current assembly candidates with the "
                        "previous reference to identify reusable, new and "
                        "changed sources."
                    )

                    state.conn.execute(
                        "DELETE FROM update_changes"
                    )
                    state.conn.commit()

                    reusable, delta_pools = classify_update(
                        state,
                        species_map,
                        full_pools,
                        old,
                    )

                    state.set_global_state(
                        "UPDATE_COMPARISON_AND_SELECTION_COMPLETE"
                    )

                # Both comparison paths now have the same reconstructed or
                # newly classified candidate sets. Report their contents
                # without changing the sets or any persistent checkpoint.
                reusable_total = sum(
                    len(accessions)
                    for accessions in reusable.values()
                )

                delta_total = sum(
                    len(assemblies)
                    for assemblies in delta_pools.values()
                )

                species_with_delta = sum(
                    bool(assemblies)
                    for assemblies in delta_pools.values()
                )

                # Display species with eligible assembly candidates, as the
                # create-mode worker output does. Species without eligible
                # candidates remain accounted for in the curated species set.
                comparison_species = [
                    (taxid, species)
                    for taxid, species in sorted(
                        species_map.items(),
                        key=lambda item: (item[1].species, item[0]),
                    )
                    if full_pools.get(taxid)
                ]

                # Keep the comparison summary inside its own visual section.
                print_section_separator()

                info(
                    "Update comparison results (provisional): "
                    f"{reusable_total:,} reusable assembly candidates; "
                    f"{delta_total:,} new/changed candidates across "
                    f"{species_with_delta:,} species."
                )

                # Display per-species results here only when a committed
                # comparison was reconstructed during resume.
                #
                # A newly executed comparison already prints live progress
                # inside classify_update(), so repeating it here would
                # duplicate every species message.
                if (
                    config.run_mode == "resume"
                    and update_comparison_state
                    in UPDATE_COMPARISON_COMMITTED_STATES
                ):
                    total_compared_species = len(comparison_species)

                    for species_number, (taxid, species) in enumerate(
                        comparison_species,
                        start=1,
                    ):
                        info(
                            f"Species {species_number}/{total_compared_species} "
                            f"REUSED (checkpoint): {species.species}\n"
                            f"       Eligible={len(full_pools.get(taxid, []))} | "
                            f"Reusable={len(reusable.get(taxid, set()))} | "
                            f"New/changed={len(delta_pools.get(taxid, []))}"
                        )

                if not comparison_species:
                    info(
                        "No eligible assembly candidates were found "
                        "for the curated species."
                    )

                info(
                    "Update comparison and selection complete: reusable "
                    "and changed/new assembly sets are ready."
                )

                print_section_separator()
                print(flush=True)

                # Announce protein processing before its introductory message.
                state.set_global_state("PROTEIN_RETRIEVAL_AND_PROCESSING")

                # UPDATE evaluates all eligible candidates using the same
                # biological selection rules as CREATE. Unchanged assemblies
                # can reuse their existing proteins without another download.
                info(
                    "Evaluating assembly candidates. "
                    "Unchanged assemblies will reuse their existing proteins, "
                    "and new downloads will be attempted only when needed."
                )

                info(
                    f"Eligible candidates: {eligible_assemblies:,} | "
                    f"Reusable: {reusable_total:,} | "
                    f"New/changed: {delta_total:,} "
                    f"across {species_with_delta:,} species."
                )

                recovery_summaries = run_species_workers(
                    state,
                    species_map,
                    full_pools,
                    config,
                    work_root,
                    plan,
                    selection_constraints_by_species=(
                        selection_constraints_by_species
                    ),
                    reusable_accessions_by_species=reusable,
                    update_mode=True,
                )

                info(
                    "Combining reusable and newly processed assemblies into the "
                    "updated reference selection."
                )

                selection_report_messages: list[str] = []

                finalize_update_selection(
                    state,
                    species_map,
                    full_pools,
                    reusable,
                    old,
                    config,
                    selection_constraints_by_species=(
                        selection_constraints_by_species
                    ),
                    report_messages=selection_report_messages,
                )

                report_ncbi_recovery_selection_updates(
                    state,
                    recovery_summaries,
                    include_selected_assemblies=False,
                )

                for message in selection_report_messages:
                    info(message)

                info(
                    "Updated reference selection complete."
                )

            else:
                # Announce protein processing before its introductory message.
                state.set_global_state("PROTEIN_RETRIEVAL_AND_PROCESSING")

                info(
                    "Starting protein processing for eligible assembly candidates."
                )

                recovery_summaries = run_species_workers(
                    state,
                    species_map,
                    full_pools,
                    config,
                    work_root,
                    plan,
                    selection_constraints_by_species=(
                        selection_constraints_by_species
                    ),
                    update_mode=False,
                )

                finalize_create_selection(
                    state,
                    species_map,
                    full_pools,
                    config,
                    selection_constraints_by_species=(
                        selection_constraints_by_species
                    ),
                )

                report_ncbi_recovery_selection_updates(
                    state,
                    recovery_summaries,
                )

            # Mark protein processing as complete without changing its results.
            state.set_global_state(
                "PROTEIN_RETRIEVAL_AND_PROCESSING_COMPLETE"
            )

            # Count species represented by at least one selected assembly
            # with a confirmed usable protein FASTA.
            represented_species = int(
                state.conn.execute(
                    """
                    SELECT COUNT(DISTINCT species_taxid)
                    FROM assemblies
                    WHERE selected = 1
                      AND protein_fasta_available = 1
                    """
                ).fetchone()[0]
            )

            # Count assemblies retained in the final biological selection.
            selected_assemblies = int(
                state.conn.execute(
                    "SELECT COUNT(*) FROM assemblies WHERE selected = 1"
                ).fetchone()[0]
            )

            # Count all original protein occurrences, including duplicates.
            protein_sources = int(
                state.conn.execute(
                    "SELECT COUNT(*) FROM sources"
                ).fetchone()[0]
            )

            # Species without a selected usable proteome remain in the
            # curated species set but contribute no proteins.
            species_without_proteins = (
                len(species_map) - represented_species
            )

            # Separate the worker progress log from the final selection summary.
            print_section_separator()

            info(
                "Protein retrieval and processing complete: "
                f"{represented_species:,}/{len(species_map):,} species included (with usable proteins); "
                f"{selected_assemblies:,} assemblies selected; "
                f"{protein_sources:,} protein source records; "
                f"{species_without_proteins:,} species without usable selected proteins."
            )

            report_unresolved_ncbi_recovery_warning(
                state,
                set(recovery_summaries),
            )

            print_section_separator()

            print(flush=True)

            # ------------------------------------------------------------------
            # 5. Global exact dedup + final outputs
            # ------------------------------------------------------------------

            # Enter final construction before displaying its introduction.
            state.set_global_state(
                "GLOBAL_EXACT_DEDUPLICATION"
            )

            info(
                "Starting final reference construction."
            )

            info(
                "Merging species protein data and applying "
                "global exact-sequence deduplication."
            )

            rebuild_global_canonical_store(
                state,
                work_root,
                old,
            )

            state.set_global_state(
                "GLOBAL_EXACT_DEDUPLICATION_COMPLETE"
            )

            # Count the unique sequences retained in the canonical store.
            unique_proteins = int(
                state.conn.execute(
                    "SELECT COUNT(*) FROM canonical_sequences"
                ).fetchone()[0]
            )

            # Each source occurrence beyond the unique sequence count
            # represents an exact duplicate removed from the final FASTA.
            duplicate_proteins = protein_sources - unique_proteins

            print_section_separator()

            info(
                f"Global exact deduplication complete: "
                f"{protein_sources:,} protein source records; "
                f"{unique_proteins:,} unique sequences; "
                f"{duplicate_proteins:,} duplicate occurrences collapsed."
            )

            print_section_separator()

            print(flush=True)

            state.set_global_state(
                "FINAL_OUTPUT_WRITING"
            )

            info(
                "Writing the final protein FASTA, "
                "protein metadata and protein provenance."
            )

            write_final_fasta_and_protein_metadata(
                state,
                work_root,
            )

            write_protein_sources(
                state,
                work_root,
            )

            state.set_global_state(
                "FINAL_OUTPUT_WRITING_COMPLETE"
            )

            info(
                "Final outputs written."
            )

            print(flush=True)

            state.set_global_state(
                "FINAL_REFERENCE_VALIDATION"
            )

            info(
                "Validating the final reference."
            )

            validate_final_reference(
                state,
                work_root,
            )

            state.set_global_state(
                "FINAL_REFERENCE_VALIDATION_COMPLETE"
            )

            info(
                "Final reference validated."
            )

            print(flush=True)

            state.set_global_state(
                "FINAL_REPORT_WRITING"
            )

            # Explain what this phase does before starting report generation.
            info(
                "Writing final reference reports."
            )

            write_all_final_reports(
                state,
                work_root,
                config,
                plan,
                explicit_targets=(
                    all_resolved_explicit_targets
                ),
            )

            state.set_global_state(
                "FINAL_REPORT_WRITING_COMPLETE"
            )

            info(
                "Final reference reports written."
            )

            # Reports now contain the final classified retrieval outcomes.
            # Warn only after those reports exist so the user can immediately
            # inspect the referenced files. This warning is informational and
            # deliberately does not interrupt normal build finalization.
            warn_incomplete_ncbi_protein_coverage(
                state
            )

            print(flush=True)

            # Announce the final checkpoint before describing its work.
            state.set_global_state(
                "FINAL_CHECKPOINT"
            )

            info(
                "Checkpointing persistent state and "
                "verifying the final integrity manifest."
            )

            # The SQLite state database is included in the final integrity manifest.
            # Mark the build as DONE and checkpoint any WAL changes into the main
            # database file before calculating checksums for the final stable files.
            with state.conn:
                state.conn.execute("UPDATE species SET state='DONE' WHERE state='PREPARED'")
            state.set_meta("completed", now_iso())

            state.set_global_state(
                "FINAL_CHECKPOINT_COMPLETE"
            )

            # DONE is deliberately persisted BEFORE manifest hashing because the SQLite
            # state database itself is included in the integrity manifest.
            #
            # DONE alone is not sufficient to declare the reference complete. The atomic
            # completion marker written below is the final commit token.
            state.set_global_state("DONE")

            # Flush WAL content into build_state.sqlite before hashing that file.
            state.checkpoint(truncate=True)

            write_manifest(work_root)

            # Verify every file recorded in the manifest before publishing the final
            # completion token.
            verify_manifest(work_root)

            # This atomic marker is the final commit point. A crash before this line leaves
            # state=DONE but no completion marker, which --resume treats as recoverable.
            write_completion_marker(work_root)

            append_python_log(
                work_root,
                "DONE validation_and_manifest=PASS completion_marker=PASS"
            )

            info(
                "Final reference construction complete. "
                "Integrity manifest verified and completion checkpoint committed."
            )

            print(flush=True)

        finally:
            if old is not None:
                old.close()

    # State connection is closed/checkpointed here. Only now can an overwrite
    # update safely expose the new reference under the user's target path.
    if destructive_target is not None:
        # The staged reference has passed validation. Clearly separate the
        # subsequent directory promotion from the final checkpoint phase.
        print_section_banner("SAFE OVERWRITE PROMOTION")

        # Announce the replacement before starting it.
        info(
            f"Staged update validated. Safely replacing completed reference: "
            f"{destructive_target}"
        )

        # Replace the public reference only after the staging build is valid.
        safe_replace_completed_directory(work_root, destructive_target)

        return destructive_target, destructive_target
    
    return work_root, None


def main(
    argv: Sequence[str] | None = None,
) -> int:

    parser = build_arg_parser()

    ns: argparse.Namespace | None = None

    try:

        ns = parser.parse_args(argv)

        final_root, _ = orchestrate(ns)

        # First show the composition of the reference that was actually built.
        try:
            if getattr(ns, "retry_ncbi_failures", False):
                print(flush=True)

            print_section_banner("SUMMARY")
            print_reference_build_summary(
                final_root,
                immutable_source=bool(
                    getattr(ns, "retry_ncbi_failures", False)
                ),
            )
            print(flush=True)

        except Exception as exc:

            warn(
                "The reference completed successfully, but the final console "
                f"summary could not be generated: {exc}"
            )

        info("Main output files:")

        info(
            f"  Final protein reference: "
            f"{final_root / DATABASE_DIR / FINAL_FASTA_NAME}"
        )

        info(
            f"  Species master: "
            f"{final_root / METADATA_DIR / 'fungal_species_master.tsv'}"
        )

        info(
            f"  Assembly master: "
            f"{final_root / METADATA_DIR / 'fungal_assemblies_master.tsv'}"
        )

        info(
            f"  Protein metadata: "
            f"{final_root / METADATA_DIR / 'fungal_protein_metadata.tsv'}"
        )

        info(
            f"  Protein provenance: "
            f"{final_root / METADATA_DIR / 'fungal_protein_sources.tsv'}"
        )

        info(
            f"  Reproduction targets: "
            f"{final_root / METADATA_DIR / REPRODUCTION_TARGETS_TSV}"
        )

        info(
            f"  Build summary: "
            f"{final_root / REPORTS_DIR / 'build_summary.tsv'}"
        )

        return EXIT_OK

    except CurationConflict as exc:

        # Conflict-specific details were already written/emitted by
        # pause_for_curation_fix(). Recovery instructions are centralized here.
        report_recovery_guidance(ns)

        return exc.exit_code

    except FungiFunkError as exc:

        error(
            str(exc)
        )

        report_recovery_guidance(ns)

        return exc.exit_code

    except KeyboardInterrupt:

        error(
            "Interrupted by user."
        )

        report_recovery_guidance(ns)

        return 130

    except Exception as exc:  # defensive boundary: never hide an unexpected bug

        error(
            f"Unexpected internal error: {exc}"
        )

        traceback.print_exc(
            file=sys.stderr
        )

        report_recovery_guidance(ns)

        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())