#!/usr/bin/env python3
"""Deterministic diagnostics for "vacuum succeeded but versions stay high".

Uses only the public HTTP surface of a throwaway laminad instance.  Each
scenario starts from a clean data directory, asserts the observation it makes,
and prints one JSON line.  This is an investigation aid, not a test framework.

Build first:
    go build -o bin/laminad ./cmd/laminad
Run:
    python3 -I examples/vacuum_diagnose.py --scenario all
    python3 -I examples/vacuum_diagnose.py --scenario pinned
"""
import argparse
import json
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BASE = "/v1/lamina"
SCHEMA = {
    "name": "spectra", "primary": "sample",
    "columns": [
        {"name": "sample", "type": "int"},
        {"name": "band", "type": "text"},
        {"name": "amplitude", "type": "int", "nullable": True},
        {"name": "valid", "type": "bool"},
    ],
}
SCENARIOS = ["baseline", "pinned", "pinned_partial", "stale", "tombstones",
             "stats", "accounting", "restart", "backup", "cost", "sessions",
             "image_cap"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def raw_call(session, method, path, data=None):
    """Return (status, body) without treating a non-200 status as a failure."""
    encoded = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(session.url + path, data=encoded, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Session:
    def __init__(self, binary, root, restore=None):
        self.binary = str(binary)
        self.root = pathlib.Path(root)
        self.restore = restore
        self.process = None
        self.log = None

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}{BASE}"
        args = [self.binary, "-data", str(self.root), "-listen", f"127.0.0.1:{port}"]
        if self.restore:
            args += ["-restore", str(self.restore)]
            self.restore = None
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(args, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                raise RuntimeError(self.log.read().decode())
            try:
                self.call("GET", "/health")
                return self
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        raise RuntimeError("service did not become ready")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
            self.log = None

    def call(self, method, path, data=None, status=200, raw=False):
        encoded = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.url + path, data=encoded, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                code, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            code, body = exc.code, exc.read()
        if code != status:
            raise RuntimeError(f"{method} {path}: expected {status}, got {code}: {body.decode()}")
        return body if raw else json.loads(body)

    def begin(self, readonly=False):
        return self.call("POST", "/transactions", {"read_only": readonly}, 201)["id"]

    def put(self, tx, row):
        self.call("POST", f"/transactions/{tx}/put", {"relation": "spectra", "row": row})

    def delete(self, tx, key):
        self.call("POST", f"/transactions/{tx}/delete", {"relation": "spectra", "key": key})

    def commit(self, tx, status=200):
        return self.call("POST", f"/transactions/{tx}/commit", status=status)

    def rollback(self, tx):
        return self.call("POST", f"/transactions/{tx}/rollback")

    def query(self, **kwargs):
        return self.call("POST", "/query", {"relation": "spectra", **kwargs})

    def vacuum(self, horizon):
        return self.call("POST", "/vacuum", {"horizon": horizon})

    def stats(self):
        return self.call("GET", "/stats")

    def backup(self):
        return self.call("GET", "/backup", raw=True)


def row(n, amplitude=None, band=None, valid=True):
    return {"sample": n, "band": f"b{n}" if band is None else band,
            "amplitude": n * 10 if amplitude is None else amplitude, "valid": valid}


def seed(session, count):
    session.call("POST", "/relations", SCHEMA, 201)
    tx = session.begin()
    for n in range(1, count + 1):
        session.put(tx, row(n))
    return session.commit(tx)["sequence"]


def update(session, key, amplitude):
    tx = session.begin()
    session.put(tx, row(key, amplitude=amplitude))
    return session.commit(tx)["sequence"]


def delete(session, key):
    tx = session.begin()
    session.delete(tx, key)
    return session.commit(tx)["sequence"]


def snapshot(session):
    stats = session.stats()
    return {"sequence": stats["sequence"], "horizon": stats["horizon"],
            "keys": stats["keys"], "versions": stats["versions"],
            "index_entries": stats["index_entries"],
            "active": [{"snapshot": t["snapshot"], "read_only": t["read_only"]}
                       for t in stats["active"]],
            "journal_records": stats["journal_records"]}


def image(session):
    envelope = json.loads(session.backup())
    payload = envelope["payload"]
    return payload if isinstance(payload, dict) else json.loads(payload)


def chains(img):
    out = {}
    for relation, rows in img["store"]["tables"].items():
        out[relation] = {}
        for key, chain in rows.items():
            out[relation][key] = {
                "sequences": [v["sequence"] for v in chain],
                "deleted": [bool(v.get("deleted")) for v in chain],
            }
    return out


def retention_audit(img):
    """Exact per-chain retention rule for a store image at its horizon.

    A chain is dropped when its newest version is a tombstone at or below the
    horizon.  Otherwise it keeps every version newer than the horizon plus the
    last version at or below the horizon, if one exists.
    """
    horizon = img["horizon"]
    total = 0
    violations = []
    for relation, rows in img["store"]["tables"].items():
        for key, chain in rows.items():
            sequences = [v["sequence"] for v in chain]
            newest_deleted = bool(chain[-1].get("deleted"))
            if sequences[-1] <= horizon and newest_deleted:
                violations.append([relation, key, sequences, "tombstone chain should be gone"])
                total += len(sequences)
                continue
            expected = sum(1 for s in sequences if s > horizon)
            if any(s <= horizon for s in sequences):
                expected += 1
            if len(sequences) != expected:
                violations.append([relation, key, sequences, f"expected {expected}"])
            total += len(sequences)
    return {"versions_by_image": total, "violations": violations}


def dir_sizes(root):
    root = pathlib.Path(root)
    sizes = {}
    for name in ("journal.lmw", "checkpoint.lmi"):
        path = root / name
        sizes[name] = path.stat().st_size if path.exists() else 0
    return sizes


# --------------------------------------------------------------------------

def scenario_baseline(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 6)
        s.call("POST", "/indexes", {"name": "by_amp", "relation": "spectra",
                                    "column": "amplitude"}, 201)
        update(s, 1, 111)
        update(s, 1, 112)
        delete(s, 2)
        head = s.stats()["sequence"]
        before = snapshot(s)
        result = s.vacuum(head)
        after = snapshot(s)
        image_result = retention_audit(image(s))
        require(result["pinned"] is False, "no reader should not pin")
        require(result["horizon"] == head, "effective horizon should equal head")
        require(result["removed"] > 0, "obsolete versions should be reclaimed")
        require(after["versions"] == after["keys"] == 5, "floor is one version per surviving key")
        require(image_result["violations"] == [], "image violates retention rule")
        require(image_result["versions_by_image"] == after["versions"], "image and stats disagree")
        require(after["index_entries"] == {"by_amp": 5}, "index rebuild should match surviving rows")
        s.call("POST", "/query", {"relation": "spectra", "at": head - 1}, 410)
        require(s.query(at=after["horizon"])["matched"] == 5, "horizon snapshot must stay readable")
        kinds = [r["kind"] for r in s.call("GET", "/changes?after=0&limit=1000")["records"]]
        require("vacuum" in kinds, "vacuum should be a journal record")
        require(result["removed"] + result["retained"] == before["versions"],
                "removed + retained must equal the pre-vacuum version count")
        return {"checks": ["no-reader vacuum reaches one version per key",
                           "removed + retained equals the pre-vacuum version count",
                           "retention rule holds in the frozen image",
                           "at below horizon is rejected, at horizon is readable"],
                "before": before, "vacuum": result, "after": after,
                "image_audit": image_result}
    finally:
        s.stop()


def scenario_pinned(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 5)
        reader = s.begin(readonly=True)
        reader_snapshot = s.stats()["active"][0]["snapshot"]
        for round_no in range(20):
            for key in range(1, 6):
                update(s, key, 1000 + round_no * 10 + key)
        head = s.stats()["sequence"]
        before = snapshot(s)
        first = s.vacuum(head)
        pinned_state = snapshot(s)
        require(first["pinned"] is True, "old reader must pin")
        require(first["horizon"] == reader_snapshot, "horizon must stop at oldest snapshot")
        require(first["removed"] == 0, "all updates are newer than the pinned horizon")
        require(pinned_state["versions"] == 105, "5 keys + 100 writes after the horizon")
        require(s.query(transaction=reader, where=[{"column": "sample", "op": "=", "value": 1}])
                ["rows"][0]["amplitude"] == 10, "pinned reader must keep its old row")
        require(s.query(where=[{"column": "sample", "op": "=", "value": 1}])
                ["rows"][0]["amplitude"] == 1191, "writer view must be current")
        tx = s.begin()
        for key in range(1, 6):
            s.put(tx, row(key, amplitude=9000 + key))
        commit = s.commit(tx)
        under_pin = snapshot(s)
        require(under_pin["versions"] == 110, "writes during a pin add unreclaimable versions")
        second = s.vacuum(under_pin["sequence"])
        require(second["pinned"] is True and second["removed"] == 0,
                "a second vacuum under the same pin reclaims nothing")
        # Advancing the horizon under a pin narrows the at-query window even though the
        # reader is untouched: snapshots below the oldest reader are no longer addressable.
        require(pinned_state["horizon"] > 0, "a pinned advance still moves the horizon forward")
        require(s.call("POST", "/query",
                       {"relation": "spectra", "at": pinned_state["horizon"] - 1}, 410),
                "snapshots below the new horizon are no longer addressable")
        require(s.query(at=pinned_state["horizon"])["matched"] == 5,
                "the new horizon itself stays addressable")
        s.rollback(reader)
        final = s.vacuum(s.stats()["sequence"])
        after = snapshot(s)
        require(final["pinned"] is False, "closing the oldest reader releases the pin")
        require(after["versions"] == after["keys"] == 5, "reclaim reaches the one-version floor")
        require(final["removed"] == under_pin["versions"] - after["keys"],
                "every version above the one-per-key floor is reclaimed at once")
        return {"checks": ["pinned vacuum reports success with removed=0",
                           "version count is keys + writes after the effective horizon",
                           "writes continue while pinned and expand retention",
                           "a pinned advance makes earlier at-snapshots unavailable",
                           "closing the oldest reader releases all retained versions"],
                "reader_snapshot": reader_snapshot, "head": head,
                "vacuum_pinned": first, "state_pinned": pinned_state,
                "commit_under_pin": commit, "state_after_write": under_pin,
                "vacuum_again": second, "vacuum_released": final, "after": after}
    finally:
        s.stop()


def scenario_pinned_partial(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 4)
        update(s, 1, 301)
        reader = s.begin(readonly=True)
        reader_snapshot = s.stats()["active"][0]["snapshot"]
        update(s, 1, 401)
        update(s, 1, 402)
        update(s, 2, 501)
        update(s, 2, 502)
        head = s.stats()["sequence"]
        before = snapshot(s)
        result = s.vacuum(head)
        after = snapshot(s)
        require(result["pinned"] is True, "reader should pin")
        require(result["removed"] > 0, "versions older than the pin are still reclaimable")
        require(after["versions"] == 8, "3 + 3 + 1 + 1 retained versions")
        require(after["versions"] == before["versions"] - result["removed"],
                "removed/retained accounting must balance")
        require(s.query(transaction=reader)["matched"] == 4, "reader keeps a full snapshot")
        require(s.query(transaction=reader,
                        where=[{"column": "sample", "op": "=", "value": 1}])
                ["rows"][0]["amplitude"] == 301, "reader keeps the version visible at its snapshot")
        audit = retention_audit(image(s))
        require(audit["violations"] == [], "image violates retention rule")
        return {"checks": ["pinned vacuum is not all-or-nothing",
                           "versions older than the pin are reclaimed",
                           "the reader's visible version stays intact"],
                "reader_snapshot": reader_snapshot, "head": head,
                "before": before, "vacuum": result, "after": after, "image_audit": audit}
    finally:
        s.stop()


def scenario_stale(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 3)
        stale = s.stats()["sequence"]
        for amplitude in (101, 102, 103, 104, 105):
            update(s, 1, amplitude)
        head = s.stats()["sequence"]
        before = snapshot(s)
        first = s.vacuum(stale)
        after_stale = snapshot(s)
        require(first["pinned"] is False, "no reader exists to pin")
        require(first["horizon"] == stale, "effective horizon equals the requested stale value")
        require(first["removed"] == 0, "writes after the requested horizon are retained")
        require(after_stale["versions"] == before["versions"], "version count is unchanged")
        require(after_stale["sequence"] == head + 1, "vacuum itself consumes a sequence number")
        second = s.vacuum(head)
        after_head = snapshot(s)
        require(second["removed"] == 5, "vacuum at the current head reclaims the tail")
        require(after_head["versions"] == after_head["keys"] == 3, "one version per key remains")
        return {"checks": ["a stale but valid horizon returns success with pinned=false",
                           "removed=0 because requested horizon predates the writes",
                           "vacuum at the current head reclaims the tail"],
                "requested": stale, "head_at_request": head,
                "vacuum_stale": first, "state_after_stale": after_stale,
                "vacuum_head": second, "after": after_head}
    finally:
        s.stop()


def scenario_tombstones(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 4)
        reader = s.begin(readonly=True)
        delete(s, 99)          # tombstone for a key that never existed
        delete(s, 2)           # tombstone over a live row
        before = snapshot(s)
        visible = s.query()
        require(before["keys"] == 5, "tombstone chains count as keys")
        require(visible["matched"] == 3, "only three rows are visible")
        head = s.stats()["sequence"]
        pinned = s.vacuum(head)
        during = snapshot(s)
        require(pinned["pinned"] is True and during["keys"] == 5,
                "tombstone chains stay while a reader can still see the old row")
        require(s.query(transaction=reader,
                        where=[{"column": "sample", "op": "=", "value": 2}])["matched"] == 1,
                "reader still sees the row deleted after its snapshot")
        s.rollback(reader)
        released = s.vacuum(s.stats()["sequence"])
        after = snapshot(s)
        require(after["keys"] == after["versions"] == 3, "tombstone-only chains are dropped")
        audit = retention_audit(image(s))
        require(audit["violations"] == [], "image violates retention rule")
        require(set(chains(image(s))["spectra"]) == {"int:1", "int:3", "int:4"},
                "deleted keys disappear after the horizon passes")
        return {"checks": ["keys and versions include tombstone chains",
                           "visible rows differ from retained keys",
                           "tombstone-only chains vanish once the horizon passes"],
                "before": before, "visible_rows": visible["matched"],
                "vacuum_pinned": pinned, "state_pinned": during,
                "vacuum_released": released, "after": after, "image_audit": audit}
    finally:
        s.stop()


def scenario_stats(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 4)
        s.call("POST", "/indexes", {"name": "by_amp", "relation": "spectra",
                                    "column": "amplitude"}, 201)
        update(s, 1, 999)
        stale_index = s.query(where=[{"column": "amplitude", "op": "=", "value": 10}])
        stale_scan = s.query(force_scan=True,
                             where=[{"column": "amplitude", "op": "=", "value": 10}])
        before = snapshot(s)
        require(stale_index["plan"]["candidates"] == 1, "historical candidate keeps the key")
        require(stale_index["scanned"] == 1 and stale_index["matched"] == 0,
                "candidate is scanned but fails the residual")
        require(stale_scan["plan"]["candidates"] == 4 and stale_scan["scanned"] == 4,
                "scan candidates are keys, not matches")
        require(before["versions"] == 5 and before["keys"] == 4,
                "versions exceed keys before reclamation")
        require(before["index_entries"] == {"by_amp": 5},
                "index entries count historical values")
        s.vacuum(s.stats()["sequence"])
        reclaimed = snapshot(s)
        fresh = s.query(where=[{"column": "amplitude", "op": "=", "value": 10}])
        require(fresh["plan"]["candidates"] == 0 and fresh["scanned"] == 0,
                "rebuild drops candidates that are no longer visible")
        require(reclaimed["index_entries"] == {"by_amp": 4}, "index matches surviving versions")
        delete(s, 2)
        deleted = s.query(where=[{"column": "amplitude", "op": ">=", "value": 0}])
        remain = snapshot(s)
        require(remain["keys"] == 4 and remain["versions"] == 5,
                "tombstone adds a version without adding a key")
        require(deleted["plan"]["candidates"] == 4 and deleted["scanned"] == 3,
                "index candidates can exceed visible rows")
        require(deleted["matched"] == 3, "matched counts predicates after visibility recheck")
        return {"checks": ["candidates, scanned, matched and versions are distinct facts",
                           "historical candidates shrink only after rebuild",
                           "keys counts tombstone chains"],
                "stale_index_query": {"plan": stale_index["plan"], "scanned": stale_index["scanned"],
                                      "matched": stale_index["matched"]},
                "stale_scan_query": {"plan": stale_scan["plan"], "scanned": stale_scan["scanned"],
                                     "matched": stale_scan["matched"]},
                "before": before, "after_reclaim": reclaimed,
                "fresh_query": {"plan": fresh["plan"], "scanned": fresh["scanned"]},
                "after_delete": {"stats": remain, "plan": deleted["plan"],
                                 "scanned": deleted["scanned"], "matched": deleted["matched"]}}
    finally:
        s.stop()


def scenario_restart(binary, tmp):
    root = tmp / "data"
    s = Session(binary, root).start()
    try:
        seed(s, 4)
        s.call("POST", "/indexes", {"name": "by_amp", "relation": "spectra",
                                    "column": "amplitude"}, 201)
        reader = s.begin(readonly=True)
        update(s, 1, 111)
        update(s, 1, 112)
        delete(s, 2)
        head = s.stats()["sequence"]
        pinned = s.vacuum(head)
        before = snapshot(s)
        image_before = retention_audit(image(s))
        s.call("POST", "/checkpoint")
        require(s.query(transaction=reader)["matched"] == 4,
                "reader still works after checkpoint")
        s.stop()
        s = Session(binary, root).start()
        after = snapshot(s)
        image_after = retention_audit(image(s))
        require(after["active"] == [], "active sessions do not survive restart")
        require(after["sequence"] == before["sequence"] and after["horizon"] == before["horizon"],
                "sequence and horizon survive restart")
        require(after["versions"] == before["versions"] and after["keys"] == before["keys"],
                "retained versions survive restart unchanged")
        require(image_before["violations"] == [] and image_after["violations"] == [],
                "retention rule holds before and after restart")
        require(after["versions"] == image_after["versions_by_image"],
                "stats agree with the persisted image after restart")
        require(s.query(at=after["horizon"])["matched"] == after["keys"],
                "horizon snapshot is readable after restart")
        s.call("POST", "/query", {"relation": "spectra", "at": after["horizon"] - 1}, 410)
        s.call("POST", "/query", {"relation": "spectra", "transaction": reader}, 404)
        reclaimed = s.vacuum(s.stats()["sequence"])
        final = snapshot(s)
        require(reclaimed["pinned"] is False and final["versions"] == final["keys"] == 3,
                "post-restart vacuum reclaims the versions the dead reader pinned")
        return {"checks": ["retained versions and horizon are persisted, not session artifacts",
                           "no active transaction survives restart",
                           "post-restart vacuum reclaims what the old reader pinned"],
                "vacuum_before_restart": pinned, "before_restart": before,
                "after_restart": after, "vacuum_after_restart": reclaimed, "final": final,
                "image_audit_before": image_before, "image_audit_after": image_after}
    finally:
        s.stop()


def scenario_backup(binary, tmp):
    source = tmp / "source"
    restored_dir = tmp / "restored"
    s = Session(binary, source).start()
    try:
        seed(s, 4)
        reader = s.begin(readonly=True)
        update(s, 1, 111)
        delete(s, 2)
        update(s, 3, 333)
        head = s.stats()["sequence"]
        pinned = s.vacuum(head)
        state = snapshot(s)
        require(pinned["pinned"] is True, "source reader should pin")
        backup_path = tmp / "frozen.lmi"
        backup_path.write_bytes(s.backup())
        frozen = retention_audit(image(s))
        require(frozen["violations"] == [], "frozen image violates retention rule")
        require(frozen["versions_by_image"] == state["versions"],
                "frozen image carries the retained versions")
    finally:
        s.stop()
    restored = Session(binary, restored_dir, restore=backup_path).start()
    try:
        after = snapshot(restored)
        require(after["active"] == [], "restored instance has no live sessions")
        require(after["horizon"] == state["horizon"] and after["sequence"] == state["sequence"],
                "restored horizon and sequence match the frozen image")
        require(after["versions"] == state["versions"], "restored retained versions match")
        require(restored.query(at=after["horizon"])["matched"] == 4,
                "at-horizon read works on the restored copy")
        restored.call("POST", "/query", {"relation": "spectra", "at": after["horizon"] - 1}, 410)
        restored.call("GET", "/changes?after=0", status=410)
        reclaimed = restored.vacuum(restored.stats()["sequence"])
        final = snapshot(restored)
        require(reclaimed["pinned"] is False and final["versions"] == final["keys"] == 3,
                "restored copy can reclaim once the historical read is served elsewhere")
        return {"checks": ["backup freezes the retained versions and horizon",
                           "restore preserves the readable range [horizon, sequence]",
                           "change history before the restore base is unavailable",
                           "a restored copy can be vacuumed without the original reader"],
                "source_vacuum": pinned, "source_state": state,
                "restored_state": after, "restored_vacuum": reclaimed, "final": final,
                "image_audit": frozen}
    finally:
        restored.stop()


def scenario_cost(binary, tmp):
    root = tmp / "data"
    s = Session(binary, root).start()
    try:
        seed(s, 200)
        s.call("POST", "/indexes", {"name": "by_amp", "relation": "spectra",
                                    "column": "amplitude"}, 201)
        reader = s.begin(readonly=True)
        for round_no in range(20):
            tx = s.begin()
            for key in range(1, 201):
                s.put(tx, row(key, amplitude=10000 + round_no * 200 + key))
            s.commit(tx)
        head = s.stats()["sequence"]
        before = snapshot(s)
        sizes_before = dir_sizes(root)
        backup_before = len(s.backup())
        started = time.perf_counter()
        pinned = s.vacuum(head)
        pinned_ms = round((time.perf_counter() - started) * 1000, 2)
        middle = snapshot(s)
        require(pinned["pinned"] is True and pinned["removed"] == 0,
                "pinned vacuum at scale reclaims nothing")
        require(middle["versions"] == before["versions"],
                "pinned vacuum leaves the version count unchanged")
        repeats = []
        for _ in range(2):
            started = time.perf_counter()
            again = s.vacuum(head)
            repeats.append({"removed": again["removed"], "retained": again["retained"],
                            "ms": round((time.perf_counter() - started) * 1000, 2)})
        sizes_middle = dir_sizes(root)
        require(sizes_middle["journal.lmw"] >= sizes_before["journal.lmw"],
                "pinned vacuum does not shrink the journal")
        s.rollback(reader)
        started = time.perf_counter()
        released = s.vacuum(s.stats()["sequence"])
        released_ms = round((time.perf_counter() - started) * 1000, 2)
        after = snapshot(s)
        require(released["removed"] > 0 and after["versions"] == after["keys"] == 200,
                "released vacuum reaches the one-version floor")
        started = time.perf_counter()
        s.call("POST", "/checkpoint")
        checkpoint_ms = round((time.perf_counter() - started) * 1000, 2)
        sizes_after = dir_sizes(root)
        backup_after = len(s.backup())
        require(sizes_after["checkpoint.lmi"] < sizes_middle["checkpoint.lmi"]
                or sizes_middle["checkpoint.lmi"] == 0,
                "checkpoint image reflects reclaimed state")
        require(backup_after < backup_before, "backup shrinks after reclamation")
        return {"checks": ["pinned vacuum rebuilds indexes under the exclusive lock for no reclaim",
                           "repeated pinned vacuum repeats the same cost",
                           "journal bytes do not shrink; checkpoint/backup images do"],
                "before": before, "pinned_vacuum": pinned, "pinned_ms": pinned_ms,
                "repeated": repeats, "released_vacuum": released, "released_ms": released_ms,
                "checkpoint_ms": checkpoint_ms, "after": after,
                "sizes_before": sizes_before, "sizes_pinned": sizes_middle,
                "sizes_after": sizes_after,
                "backup_bytes_before": backup_before, "backup_bytes_after": backup_after}
    finally:
        s.stop()


def scenario_sessions(binary, tmp):
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 3)
        update(s, 1, 111)
        readers = [s.begin(readonly=True) for _ in range(128)]
        saturated = s.stats()
        require(len(saturated["active"]) == 128, "128 readers should be active")
        refused = s.call("POST", "/transactions", {"read_only": False}, 429)
        require(refused["error"]["code"] == "capacity",
                "a full session table refuses new writers")
        s.rollback(readers[0])
        writer = s.begin(readonly=False)
        s.put(writer, row(2, amplitude=222))
        committed = s.commit(writer)
        require(committed["writes"] == 1, "a freed slot admits a writer")
        head = s.stats()["sequence"]
        pinned = s.vacuum(head)
        require(pinned["pinned"] is True, "the remaining readers pin at their snapshot")
        require(pinned["horizon"] == saturated["active"][0]["snapshot"],
                "the horizon stops at the surviving readers' snapshot")
        for tx in readers[1:]:
            s.rollback(tx)
        released = s.vacuum(s.stats()["sequence"])
        final = snapshot(s)
        require(released["pinned"] is False and final["versions"] == final["keys"] == 3,
                "closing the readers releases the retained versions")
        return {"checks": ["abandoned readers exhaust the 128-session budget",
                           "a saturated table refuses new transactions including writers",
                           "closing one slot restores write admission but not vacuum progress",
                           "closing every reader releases vacuum"],
                "saturated_active": len(saturated["active"]),
                "refused_begin": refused, "pinned_vacuum": pinned,
                "commit_after_free_slot": committed, "released_vacuum": released,
                "final": final}
    finally:
        s.stop()


def scenario_accounting(binary, tmp):
    """Decompose "versions" into the four facts that drive it."""
    s = Session(binary, tmp / "data").start()
    try:
        seed(s, 6)
        s.call("POST", "/indexes", {"name": "by_amp", "relation": "spectra",
                                    "column": "amplitude"}, 201)
        reader = s.begin(readonly=True)
        reader_snapshot = s.stats()["active"][0]["snapshot"]
        delete(s, 2)
        delete(s, 99)                      # tombstone for a key that never existed
        for amplitude in (111, 112, 113):
            update(s, 1, amplitude)
        update(s, 3, 333)
        head = s.stats()["sequence"]
        state = snapshot(s)
        img = image(s)
        audit = retention_audit(img)
        per_relation = {}
        for relation, rows in chains(img).items():
            per_relation[relation] = {
                "keys": len(rows),
                "versions": sum(len(c["sequences"]) for c in rows.values()),
                "tombstone_only_keys": sum(1 for c in rows.values() if all(c["deleted"])),
                "max_chain": max(len(c["sequences"]) for c in rows.values()),
            }
        visible = s.query()["matched"]
        require(state["versions"] == audit["versions_by_image"],
                "stats versions must equal the sum of the image chains")
        require(audit["violations"] == [], "image violates retention rule")
        require(state["keys"] == sum(r["keys"] for r in per_relation.values()),
                "stats keys must equal the image key count")
        require(per_relation["spectra"]["tombstone_only_keys"] == 1,
                "a tombstone for a never-existing key is still a retained chain")
        require(visible == 5, "one of six seeded rows is deleted; key 99 never existed")
        # Test the closed-form retention count: keys + versions above horizon.
        above = sum(1 for r in img["store"]["tables"].values()
                    for chain in r.values()
                    for v in chain if v["sequence"] > img["horizon"])
        below_or_at = sum(1 for r in img["store"]["tables"].values()
                          for chain in r.values()
                          if any(v["sequence"] <= img["horizon"] for v in chain))
        require(state["versions"] == above + below_or_at,
                "retained versions = newer than horizon + one floor per live chain")
        tombstone_versions = sum(1 for r in img["store"]["tables"].values()
                                 for chain in r.values()
                                 for v in chain if v.get("deleted"))
        require(tombstone_versions == 2, "two deletes produced two tombstone versions")
        require(state["index_entries"] == {"by_amp": state["versions"] - tombstone_versions},
                "index entries count non-tombstone versions only")
        pinned = s.vacuum(head)
        after_pin = snapshot(s)
        require(pinned["pinned"] is True, "the reader pins the horizon")
        require(after_pin["versions"] == state["versions"],
                "pinned vacuum makes no progress")
        s.rollback(reader)
        released = s.vacuum(s.stats()["sequence"])
        after = snapshot(s)
        final_img = image(s)
        require(released["horizon"] > head, "released vacuum moves the horizon forward")
        require(after["versions"] == sum(len(c["sequences"])
                                         for r in chains(final_img).values()
                                         for c in r.values()),
                "post-reclaim stats match the image")
        require(retention_audit(final_img)["violations"] == [],
                "post-reclaim image violates retention rule")
        return {"checks": ["versions = keys plus historical depth above the horizon",
                           "tombstone chains count as keys until the horizon passes",
                           "index entries exclude tombstones but keep historical values",
                           "pinned vacuum keeps the full accounting unchanged"],
                "reader_snapshot": reader_snapshot, "head": head,
                "retained": state, "visible_rows": visible,
                "per_relation": per_relation, "image_audit": audit,
                "pinned_vacuum": pinned, "state_after_pinned": after_pin,
                "released_vacuum": released, "final": after}
    finally:
        s.stop()


def scenario_image_cap(binary, tmp):
    """Pinned history can push checkpoint/backup past the 128MiB image cap."""
    root = tmp / "data"
    s = Session(binary, root).start()
    try:
        s.call("POST", "/relations", SCHEMA, 201)
        big = "z" * (64 * 1024)
        rows_count = 1100
        for start in range(1, rows_count + 1, 100):
            tx = s.begin()
            for key in range(start, min(start + 100, rows_count + 1)):
                s.put(tx, {"sample": key, "band": big, "amplitude": key, "valid": True})
            s.commit(tx)
        reader = s.begin(readonly=True)
        for start in range(1, rows_count + 1, 100):
            tx = s.begin()
            for key in range(start, min(start + 100, rows_count + 1)):
                s.put(tx, {"sample": key, "band": big, "amplitude": key + 1, "valid": True})
            s.commit(tx)
        head = s.stats()["sequence"]
        state = snapshot(s)
        pinned = s.vacuum(head)
        require(pinned["pinned"] is True, "the reader pins the horizon")
        require(pinned["removed"] == 0, "no history is older than the pin")
        checkpoint_status, checkpoint_body = raw_call(s, "POST", "/checkpoint")
        backup_status, backup_body = raw_call(s, "GET", "/backup")
        require(checkpoint_status == 503 and "image limit" in checkpoint_body.decode(),
                "checkpoint should fail its own image limit")
        require(backup_status == 429, "backup should fail its capacity limit")
        tx = s.begin()
        s.put(tx, {"sample": 1, "band": "q", "amplitude": 1, "valid": True})
        commit = s.commit(tx)
        require(commit["writes"] == 1, "writes still succeed while the image is capped")
        s.rollback(reader)
        released = s.vacuum(s.stats()["sequence"])
        after = snapshot(s)
        checkpoint_status2, _ = raw_call(s, "POST", "/checkpoint")
        require(released["pinned"] is False, "closing the reader releases the pin")
        require(after["versions"] == after["keys"] == rows_count,
                "reclaim reaches one version per key")
        require(checkpoint_status2 == 200, "checkpoint succeeds once history is reclaimed")
        require(dir_sizes(root)["checkpoint.lmi"] < 128 << 20,
                "the checkpoint image is under the cap again")
        return {"checks": ["pinned history can exceed the 128MiB image limit",
                           "checkpoint returns 503 while backup returns 429",
                           "writes continue while images are capped",
                           "reclaiming the history restores checkpoint/backup"],
                "state_pinned": state, "vacuum_pinned": pinned,
                "checkpoint_status": checkpoint_status,
                "backup_status": backup_status,
                "commit_under_cap": commit,
                "vacuum_released": released, "final": after,
                "checkpoint_after_status": checkpoint_status2}
    finally:
        s.stop()


RUNNERS = {
    "baseline": scenario_baseline, "pinned": scenario_pinned,
    "pinned_partial": scenario_pinned_partial, "stale": scenario_stale,
    "tombstones": scenario_tombstones, "stats": scenario_stats,
    "accounting": scenario_accounting,
    "restart": scenario_restart, "backup": scenario_backup,
    "cost": scenario_cost, "sessions": scenario_sessions,
    "image_cap": scenario_image_cap,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    parser.add_argument("--binary", default=None)
    args = parser.parse_args()
    binary = pathlib.Path(args.binary) if args.binary else \
        pathlib.Path(__file__).resolve().parents[1] / "bin" / "laminad"
    if not binary.exists():
        raise SystemExit("build first: go build -o bin/laminad ./cmd/laminad")
    names = SCENARIOS if args.scenario == "all" else [args.scenario]
    for name in names:
        with tempfile.TemporaryDirectory(prefix=f"lamina-{name}-") as tmp:
            result = RUNNERS[name](binary, pathlib.Path(tmp))
        result = {"scenario": name, "status": "verified", **result}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
