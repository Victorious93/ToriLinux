import time

import pytest

import secjobs
import secprotocol as proto


class FakeExecutors:
    """Runs no real tool. Lets each test control exactly how long a job takes and whether
    it fails, so secjobs.JobStore's own lifecycle/idempotency/cancel logic is tested
    independently of secmsf/secset (which have their own live-tool tests)."""

    def __init__(self, delay=0.0, fail_with=None, watch_cancel=False):
        self.delay, self.fail_with, self.watch_cancel = delay, fail_with, watch_cancel
        self.ran = []

    def run(self, job):
        self.ran.append(job.job_id)
        job.append_output("starting\n")
        if self.watch_cancel:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if job.cancel_event.is_set():
                    raise proto.ProtocolError("cancelled", "job cancelled")
                time.sleep(0.05)
        else:
            time.sleep(self.delay)
        if self.fail_with:
            raise proto.ProtocolError(self.fail_with, "synthetic failure")
        job.append_output("done\n")
        return {"summary": "ok"}


@pytest.fixture
def victor_data(tmp_path, monkeypatch):
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path))
    return tmp_path


def _submit_body(op="metasploit.module", vector_params=None):
    return {
        "op": op, "idempotency_key": "k1",
        "params": vector_params or {"module_type": "auxiliary", "module_path": "scanner/portscan/tcp", "target_host": "127.0.0.1"},
    }


def test_submit_runs_job_to_completion(victor_data):
    ex = FakeExecutors()
    store = secjobs.JobStore(ex.run)
    job = store.submit("owner1", _submit_body())
    for _ in range(50):
        if job.state in proto.TERMINAL:
            break
        time.sleep(0.05)
    assert job.state == "completed"
    assert job.result == {"summary": "ok"}
    assert ex.ran == [job.job_id]


def test_idempotency_same_params_returns_same_job_without_rerunning(victor_data):
    ex = FakeExecutors()
    store = secjobs.JobStore(ex.run)
    j1 = store.submit("owner1", _submit_body())
    time.sleep(0.2)
    j2 = store.submit("owner1", _submit_body())
    assert j1.job_id == j2.job_id
    assert len(ex.ran) == 1


def test_idempotency_conflict_on_different_params(victor_data):
    store = secjobs.JobStore(FakeExecutors().run)
    store.submit("owner1", _submit_body())
    with pytest.raises(proto.ProtocolError) as e:
        store.submit("owner1", _submit_body(vector_params={
            "module_type": "auxiliary", "module_path": "scanner/portscan/tcp", "target_host": "10.0.0.1",
        }))
    assert e.value.code == "idempotency_conflict"


def test_job_owner_isolation(victor_data):
    store = secjobs.JobStore(FakeExecutors().run)
    job = store.submit("owner1", _submit_body())
    with pytest.raises(proto.ProtocolError) as e:
        store.get("owner2", job.job_id)
    assert e.value.code == "not_job_owner"


def test_unknown_job(victor_data):
    store = secjobs.JobStore(FakeExecutors().run)
    with pytest.raises(proto.ProtocolError) as e:
        store.get("owner1", "job_does_not_exist")
    assert e.value.code == "unknown_job"


def test_cancel_reaches_running_job(victor_data):
    ex = FakeExecutors(watch_cancel=True)
    store = secjobs.JobStore(ex.run)
    job = store.submit("owner1", _submit_body())
    time.sleep(0.3)
    assert job.state == "running"
    store.cancel("owner1", job.job_id)
    for _ in range(50):
        if job.state in proto.TERMINAL:
            break
        time.sleep(0.05)
    assert job.state == "cancelled"


def test_cancel_queued_job_never_runs():
    """A job cancelled before its worker thread even starts its first loop iteration
    must never call the executor - this directly exercises the 'queued' -> 'cancelled'
    transition secprotocol.TRANSITIONS allows without ever reaching 'running'."""
    ex = FakeExecutors()
    job = secjobs.JobRecord("job_x", "owner1", "metasploit.module", {}, "k", 60_000)
    store_cancel_only = object.__new__(secjobs.JobStore)
    store_cancel_only._lock = __import__("threading").RLock()
    store_cancel_only._jobs = {"job_x": job}
    store_cancel_only._idempotency = {}
    store_cancel_only._run_executor = ex.run
    store_cancel_only._save = lambda j: None
    store_cancel_only.cancel("owner1", "job_x")
    assert job.state == "cancelled"
    assert ex.ran == []


def test_output_cursor_resumes_without_duplication(victor_data):
    ex = FakeExecutors(delay=0.3)
    store = secjobs.JobStore(ex.run)
    job = store.submit("owner1", _submit_body())
    time.sleep(0.1)
    first = job.to_public_dict(output_cursor=0)
    cursor = first["next_cursor"]
    for _ in range(30):
        if job.state in proto.TERMINAL:
            break
        time.sleep(0.05)
    second = job.to_public_dict(output_cursor=cursor)
    seen_texts = "".join(c["text"] for c in first["output"]) + "".join(c["text"] for c in second["output"])
    assert seen_texts == "starting\ndone\n"


def test_failed_job_records_structured_error(victor_data):
    store = secjobs.JobStore(FakeExecutors(fail_with="executor_unavailable").run)
    job = store.submit("owner1", _submit_body())
    for _ in range(50):
        if job.state in proto.TERMINAL:
            break
        time.sleep(0.05)
    assert job.state == "failed"
    assert job.error["code"] == "executor_unavailable"


def test_reconcile_on_startup_marks_stale_running_job_interrupted(victor_data):
    """Simulates a restart: writes a job file in 'running' state directly (as if a previous
    process died mid-job), then constructs a fresh JobStore and checks it is reconciled to
    'interrupted' rather than silently re-run."""
    import json
    import victorcommon as vc

    stale = secjobs.JobRecord("job_stale", "owner1", "metasploit.module",
                               {"module_type": "auxiliary", "module_path": "x", "target_host": "h"}, "k", 60_000)
    stale.state = "running"
    vc.atomic_write(vc.data_path("security", "jobs", "job_stale.json"), json.dumps(stale.to_disk_dict()))

    ex = FakeExecutors()
    store = secjobs.JobStore(ex.run)
    reconciled = store.get("owner1", "job_stale")
    assert reconciled.state == "interrupted"
    assert ex.ran == []  # never re-run
