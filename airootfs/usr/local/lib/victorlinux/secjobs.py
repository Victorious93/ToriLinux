"""Job store and executor dispatch for the VictorLinux security backend.

Persists job metadata and output to VictorLinux's normal persistent-storage convention
(`victorcommon.data_path("security", "jobs", ...)`) so a restart does not lose job history,
and so a drive that goes missing is reported the same way `victorcommon.data_root()`
already reports it for every other VictorLinux feature - not a separate failure mode this
module invents.

Each job runs in its own daemon thread (`threading.Thread(daemon=True)`), owns its own
`Event` used as the `is_cancelled()` callback both executors (`secmsf`, `secset`) already
accept, and is reconciled at backend startup: any job this process finds still marked
"running"/"cancelling" from before a restart is moved to "interrupted" (never silently
re-run - see `reconcile_on_startup`).
"""
import json
import os
import threading
import time
import uuid

import secprotocol as proto
import victorcommon as vc

MAX_OUTPUT_CHARS = 2_000_000  # bounded per job; see docs/security-backend.md#output-limits


class JobRecord:
    def __init__(self, job_id, owner_key_id, op, params, idempotency_key, timeout_ms):
        self.job_id = job_id
        self.owner_key_id = owner_key_id
        self.op = op
        self.params = params
        self.idempotency_key = idempotency_key
        self.timeout_ms = timeout_ms
        self.state = "queued"
        self.created_at = proto.now_ms()
        self.updated_at = self.created_at
        self.output_chunks = []   # list of (seq, text) - seq is the resume cursor
        self.next_seq = 0
        self.result = None        # set on completed: {"summary": ..., "artifact_path": ...}
        self.error = None         # set on failed/cancelled/interrupted: {"code":..., "message":...}
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()

    def append_output(self, text):
        with self.lock:
            total = sum(len(t) for _, t in self.output_chunks)
            if total >= MAX_OUTPUT_CHARS:
                return
            text = text[: MAX_OUTPUT_CHARS - total]
            if not text:
                return
            self.output_chunks.append((self.next_seq, text))
            self.next_seq += 1

    def output_since(self, cursor):
        with self.lock:
            return [(s, t) for s, t in self.output_chunks if s >= cursor]

    def to_public_dict(self, output_cursor=None):
        with self.lock:
            d = {
                "job_id": self.job_id, "op": self.op, "state": self.state,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "timeout_ms": self.timeout_ms,
            }
            if self.result is not None:
                d["result"] = self.result
            if self.error is not None:
                d["error"] = self.error
            if output_cursor is not None:
                chunks = self.output_since(output_cursor)
                d["output"] = [{"seq": s, "text": t} for s, t in chunks]
                d["next_cursor"] = (chunks[-1][0] + 1) if chunks else output_cursor
            return d

    def to_disk_dict(self):
        with self.lock:
            return {
                "job_id": self.job_id, "owner_key_id": self.owner_key_id, "op": self.op,
                "params": self.params, "idempotency_key": self.idempotency_key,
                "timeout_ms": self.timeout_ms, "state": self.state,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "result": self.result, "error": self.error,
                "output": [{"seq": s, "text": t} for s, t in self.output_chunks],
            }


def _transition(job, new_state, *, result=None, error=None):
    with job.lock:
        allowed = proto.TRANSITIONS.get(job.state, set())
        if new_state not in allowed and job.state != new_state:
            raise RuntimeError(f"illegal job transition {job.state!r} -> {new_state!r} for {job.job_id}")
        job.state = new_state
        job.updated_at = proto.now_ms()
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error


class JobStore:
    """In-memory, persisted-to-disk job registry. One instance per running backend process."""

    def __init__(self, run_executor):
        self._lock = threading.RLock()
        self._jobs = {}                 # job_id -> JobRecord
        self._idempotency = {}          # (owner_key_id, idempotency_key) -> job_id
        self._run_executor = run_executor  # callable(job) -> runs synchronously in the worker thread
        self._load_from_disk()
        self.reconcile_on_startup()

    # -- persistence -------------------------------------------------------
    def _job_path(self, job_id):
        p = vc.data_path("security", "jobs", f"{job_id}.json")
        if not p:
            raise proto.ProtocolError("storage_unavailable", "no persistent storage is selected on this VictorLinux")
        return p

    def _save(self, job):
        try:
            vc.atomic_write(self._job_path(job.job_id), json.dumps(job.to_disk_dict(), indent=2))
        except RuntimeError as e:
            raise proto.ProtocolError("storage_unavailable", str(e)) from e

    def _load_from_disk(self):
        root = vc.data_path("security", "jobs")
        if not root or not os.path.isdir(root):
            return
        for name in os.listdir(root):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name)) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            job = JobRecord(d["job_id"], d.get("owner_key_id"), d["op"], d["params"],
                             d.get("idempotency_key"), d.get("timeout_ms", 180_000))
            job.state = d.get("state", "interrupted")
            job.created_at = d.get("created_at", proto.now_ms())
            job.updated_at = d.get("updated_at", job.created_at)
            job.result = d.get("result")
            job.error = d.get("error")
            job.output_chunks = [(c["seq"], c["text"]) for c in d.get("output", [])]
            job.next_seq = (job.output_chunks[-1][0] + 1) if job.output_chunks else 0
            self._jobs[job.job_id] = job
            if job.idempotency_key:
                self._idempotency[(job.owner_key_id, job.idempotency_key)] = job.job_id

    def reconcile_on_startup(self):
        """Any job left 'queued'/'running'/'cancelling' from before this process started was
        NOT finished by this process and is never silently resumed or re-run - the real
        tool process (msfconsole/setoolkit) died with the previous backend process, so there
        is nothing left to reconnect to. Mark it interrupted and persist that."""
        for job in list(self._jobs.values()):
            if job.state in ("queued", "running", "cancelling"):
                _transition(job, "interrupted", error={
                    "code": "internal",
                    "message": "the security backend restarted while this job was in flight; "
                               "its real outcome on the tool side is unknown",
                })
                self._save(job)

    # -- submit / query / cancel -------------------------------------------
    def submit(self, owner_key_id, body):
        req = proto.validate_job_submit(body)
        key = (owner_key_id, req["idempotency_key"])
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id:
                existing = self._jobs[existing_id]
                if existing.op != req["op"] or existing.params != req["params"]:
                    raise proto.ProtocolError(
                        "idempotency_conflict",
                        f"idempotency_key '{req['idempotency_key']}' was already used with different parameters",
                    )
                return existing  # exact retry: return the original job, do not run twice

            job_id = proto.new_id("job")
            job = JobRecord(job_id, owner_key_id, req["op"], req["params"], req["idempotency_key"], req["timeout_ms"])
            self._jobs[job_id] = job
            self._idempotency[key] = job_id
            self._save(job)

        t = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"secjob-{job_id}")
        t.start()
        return job

    def _run(self, job):
        # Every transition is followed by its disk write BEFORE the lock is released, so a
        # caller that observes the terminal state in memory (e.g. a status poll racing this
        # thread) never sees a state the disk copy has not caught up to yet, and a crash
        # between "the job finished" and "the job was saved" cannot happen.
        with job.lock:
            _transition(job, "running")
            self._save(job)
        try:
            result = self._run_executor(job)
            with job.lock:
                if job.state == "cancelling":
                    _transition(job, "cancelled", result=result)
                else:
                    _transition(job, "completed", result=result)
                self._save(job)
        except proto.ProtocolError as e:
            with job.lock:
                target = "cancelled" if (job.state == "cancelling" and e.code == "cancelled") else "failed"
                _transition(job, target, error=e.to_dict()["error"])
                self._save(job)
        except Exception as e:  # noqa: BLE001 - last line of defense; never leaves a job stuck "running"
            with job.lock:
                _transition(job, "failed", error={"code": "internal", "message": str(e)})
                self._save(job)

    def get(self, owner_key_id, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise proto.ProtocolError("unknown_job", f"no job '{job_id}'")
        if job.owner_key_id != owner_key_id:
            raise proto.ProtocolError("not_job_owner", "this job belongs to a different API key")
        return job

    def cancel(self, owner_key_id, job_id):
        job = self.get(owner_key_id, job_id)
        with job.lock:
            if job.state in proto.TERMINAL:
                return job  # already finished; cancelling a finished job is a no-op, not an error
            if job.state == "queued":
                _transition(job, "cancelled", error={"code": "cancelled", "message": "cancelled before it started"})
            else:
                _transition(job, "cancelling")
                job.cancel_event.set()
        self._save(job)
        return job

    def list_jobs(self, owner_key_id):
        with self._lock:
            return [j for j in self._jobs.values() if j.owner_key_id == owner_key_id]
