"""End-to-end invariant tests for the traceability/release platform.

Run:  python3 -m unittest tests.test_platform -v
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import guards  # noqa: E402
from app.core import ApiError, canonical  # noqa: E402
from app import db as dbmod  # noqa: E402

ENV = {"temp_c_min": 20.0, "temp_c_max": 30.0,
       "humidity_pct_min": 30.0, "humidity_pct_max": 60.0}


def manifest(total=10, expected=60.0, batches=None, version="v1.0",
             checksum="sha256:model-1"):
    if batches is None:
        batches = [{"batch_code": "B1", "material_type": "PA12",
                    "grade": "工业级", "spool_id": "SP-1",
                    "supplier": "供应商甲", "planned_weight_g": 90}]
    return {
        "part_name": "测试件", "customer": "测试客户",
        "model": {"name": "测试件", "version": version, "file": "p.stl",
                  "checksum": checksum},
        "slicing": {"profile": "P1", "layer_height_mm": 0.1,
                    "infill_pct": 20, "speed_mm_s": 100,
                    "total_layers": total,
                    "expected_material_g": expected},
        "device": {"device_id": "D1", "calibration_version": "C1",
                   "calibration_checksum": "sha256:cal-1"},
        "environment": ENV, "materials": batches}


class PlatformTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        dbmod.DB_PATH = self.path
        self.conn = dbmod.connect()
        dbmod.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            p = self.path + ext
            if os.path.exists(p):
                os.remove(p)

    # -- helpers ----------------------------------------------------------
    def new_job(self, **kw):
        return guards.create_job(self.conn, manifest(**kw))

    def complete_job(self, total=10, expected=60.0, charge=90.0, ret=None,
                     batch="B1", inspect="pass", temp=24.0,
                     layers=None, skip_close=False, skip_return=False):
        job = self.new_job(total=total, expected=expected)
        jid, snap = job["job_id"], job["snapshot_hash"]
        s = guards.create_session(self.conn, jid,
                                  {"kind": "start", "device_id": "D1"})
        guards.charge_material(self.conn, jid, {
            "batch_code": batch, "weight_g": charge,
            "session_id": s["session_id"]})
        layers = total if layers is None else layers
        per = expected / total
        for n in range(1, layers + 1):
            guards.upload_layer(self.conn, jid, {
                "layer_no": n, "receipt_id": f"R-{jid}-{n:03d}",
                "snapshot_hash": snap, "session_id": s["session_id"],
                "device_id": "D1",
                "material_usages": [{"batch_code": batch, "weight_g": per}],
                "env_temp_c": temp, "env_humidity_pct": 40.0})
        guards.end_session(self.conn, jid, s["session_id"],
                           {"reason": "completed"})
        consumed = per * layers
        if ret is None:
            ret = round(charge - consumed, 3)
        if not skip_return:
            guards.return_material(self.conn, jid, {
                "batch_code": batch, "weight_g": ret,
                "session_id": s["session_id"]})
        if not skip_return and not skip_close:
            guards.close_material(self.conn, jid)
        if inspect:
            guards.add_inspection(self.conn, jid, {
                "inspector": "检验员甲", "method": "CT",
                "report_ref": "QC-1", "result": inspect})
        return job

    def expect_error(self, status, error, fn, *args, **kw):
        try:
            fn(*args, **kw)
        except ApiError as e:
            self.assertEqual(e.status, status, f"{e.status} != {status}: {e}")
            self.assertEqual(e.error, error)
            return e
        self.fail(f"expected ApiError {status}/{error}")

    # -- tests ------------------------------------------------------------
    def test_01_full_release_and_valid_certificate(self):
        job = self.complete_job()
        res = guards.release_product(self.conn, job["job_id"],
                                     {"serial": "SN-1"})
        self.assertEqual(res["state"], "released")
        cert = res["certificate"]
        self.assertTrue(cert["signature"])
        # certificate verifies
        got = guards.get_certificate(self.conn, cert_id=cert["cert_id"])
        self.assertTrue(got["valid"])
        # tampering with the payload breaks verification
        tampered = dict(cert["payload"])
        tampered["serial"] = "SN-FORGED"
        v = guards.verify_certificate(self.conn, {
            "payload": tampered, "signature": cert["signature"]})
        self.assertFalse(v["valid"])
        gate = guards.evaluate_gate(self.conn, job["job_id"])
        self.assertTrue(gate["ready_for_release"])

    def test_02_duplicate_layer_is_idempotent_then_conflict(self):
        job = self.new_job()
        jid, snap = job["job_id"], job["snapshot_hash"]
        s = guards.create_session(self.conn, jid,
                                  {"kind": "start", "device_id": "D1"})
        payload = {"layer_no": 1, "receipt_id": "R-DUP-1",
                   "snapshot_hash": snap, "session_id": s["session_id"],
                   "device_id": "D1",
                   "material_usages": [{"batch_code": "B1", "weight_g": 6}],
                   "env_temp_c": 24.0, "env_humidity_pct": 40.0}
        r1 = guards.upload_layer(self.conn, jid, payload)
        r2 = guards.upload_layer(self.conn, jid, payload)
        self.assertTrue(r2.get("idempotent"))
        # a different receipt trying to claim layer 1 must be rejected
        self.expect_error(409, "duplicate_layer", guards.upload_layer,
                          self.conn, jid, {**payload,
                                           "receipt_id": "R-DUP-1B"})
        # replaying same receipt id with different payload = conflict
        self.expect_error(409, "receipt_id_conflict",
                          guards.upload_layer, self.conn, jid,
                          {**payload, "layer_no": 2})

    def test_03_snapshot_mismatch_rejected(self):
        job = self.new_job()
        jid = job["job_id"]
        s = guards.create_session(self.conn, jid,
                                  {"kind": "start", "device_id": "D1"})
        self.expect_error(409, "snapshot_mismatch",
                          guards.upload_layer, self.conn, jid, {
                              "layer_no": 1, "receipt_id": "R-X",
                              "snapshot_hash": "deadbeef",
                              "session_id": s["session_id"],
                              "device_id": "D1",
                              "material_usages": [
                                  {"batch_code": "B1", "weight_g": 6}],
                              "env_temp_c": 24, "env_humidity_pct": 40})

    def test_04_undeclared_material_rejected(self):
        job = self.new_job()
        jid, snap = job["job_id"], job["snapshot_hash"]
        s = guards.create_session(self.conn, jid,
                                  {"kind": "start", "device_id": "D1"})
        # charging an undeclared batch
        self.expect_error(422, "undeclared_batch",
                          guards.charge_material, self.conn, jid,
                          {"batch_code": "BX", "weight_g": 50,
                           "session_id": s["session_id"]})
        # layer referencing undeclared batch
        self.expect_error(422, "undeclared_batch",
                          guards.upload_layer, self.conn, jid, {
                              "layer_no": 1, "receipt_id": "R-U",
                              "snapshot_hash": snap,
                              "session_id": s["session_id"],
                              "device_id": "D1",
                              "material_usages": [
                                  {"batch_code": "BX", "weight_g": 6}],
                              "env_temp_c": 24, "env_humidity_pct": 40})
        # declare the batch mid-job -> now allowed
        guards.declare_material(self.conn, jid, {
            "batch_code": "BX", "material_type": "PA12", "grade": "工业级"})
        guards.charge_material(self.conn, jid, {
            "batch_code": "BX", "weight_g": 50,
            "session_id": s["session_id"], "spool_change": True})
        r = guards.upload_layer(self.conn, jid, {
            "layer_no": 1, "receipt_id": "R-U2",
            "snapshot_hash": snap, "session_id": s["session_id"],
            "device_id": "D1",
            "material_usages": [{"batch_code": "BX", "weight_g": 6}],
            "env_temp_c": 24, "env_humidity_pct": 40})
        self.assertEqual(r["status"], "ok")

    def test_05_material_conservation_enforced(self):
        # residual charge - return - consume = 90-24-60 = 6g > 5g tolerance
        self.expect_error(422, "material_not_conserved",
                          self.complete_job, charge=90.0, ret=24.0)

    def test_06_missing_return_blocks_settlement(self):
        job = self.complete_job(skip_return=True)
        self.expect_error(422, "material_not_conserved",
                          guards.close_material, self.conn, job["job_id"])

    def test_07_power_loss_and_resume(self):
        job = self.new_job()
        jid, snap = job["job_id"], job["snapshot_hash"]
        s1 = guards.create_session(self.conn, jid,
                                   {"kind": "start", "device_id": "D1"})
        guards.charge_material(self.conn, jid, {
            "batch_code": "B1", "weight_g": 90,
            "session_id": s1["session_id"]})
        guards.upload_layer(self.conn, jid, {
            "layer_no": 1, "receipt_id": "R-PL-1", "snapshot_hash": snap,
            "session_id": s1["session_id"], "device_id": "D1",
            "material_usages": [{"batch_code": "B1", "weight_g": 6}],
            "env_temp_c": 24, "env_humidity_pct": 40})
        guards.end_session(self.conn, jid, s1["session_id"],
                           {"reason": "power_loss"})
        # uploading to the dead session must fail
        self.expect_error(409, "session_closed", guards.upload_layer,
                          self.conn, jid, {
                              "layer_no": 2, "receipt_id": "R-PL-2",
                              "snapshot_hash": snap,
                              "session_id": s1["session_id"],
                              "device_id": "D1",
                              "material_usages": [
                                  {"batch_code": "B1", "weight_g": 6}],
                              "env_temp_c": 24, "env_humidity_pct": 40})
        # resume and continue
        s2 = guards.create_session(self.conn, jid,
                                   {"kind": "resume", "device_id": "D1"})
        # cannot resume again while a session is open
        self.expect_error(409, "session_already_open",
                          guards.create_session, self.conn, jid,
                          {"kind": "resume", "device_id": "D1"})
        r = guards.upload_layer(self.conn, jid, {
            "layer_no": 2, "receipt_id": "R-PL-2", "snapshot_hash": snap,
            "session_id": s2["session_id"], "device_id": "D1",
            "material_usages": [{"batch_code": "B1", "weight_g": 6}],
            "env_temp_c": 24, "env_humidity_pct": 40})
        self.assertEqual(r["status"], "ok")

    def test_08_model_revision_creates_new_snapshot(self):
        job = self.new_job(version="v1.0")
        rev = guards.revise_job(self.conn, job["job_id"],
                                {"model": {"version": "v1.1",
                                           "checksum": "sha256:model-2"}})
        self.assertNotEqual(rev["snapshot_hash"], job["snapshot_hash"])
        self.assertEqual(rev["revised_from"], job["job_id"])
        # revision without bumping version rejected
        self.expect_error(422, "revision_unchanged", guards.revise_job,
                          self.conn, job["job_id"],
                          {"model": {"version": "v1.0"}})

    def test_09_env_anomaly_blocks_then_resolve_allows(self):
        # one layer breaches the frozen temperature range
        job = self.complete_job(temp=33.2)
        gate = guards.evaluate_gate(self.conn, job["job_id"])
        self.assertFalse(gate["ready_for_release"])
        codes = {c["code"] for c in gate["checks"]}
        self.assertIn("no_open_anomaly", codes)
        open_ids = [a["id"] for a in gate["open_anomalies"]]
        self.assertTrue(open_ids)
        for aid in open_ids:
            guards.resolve_anomaly(self.conn, job["job_id"], aid,
                                   {"resolution": "复测温度回归范围，层厚复检合格"})
        gate = guards.evaluate_gate(self.conn, job["job_id"])
        self.assertTrue(gate["ready_for_release"])
        res = guards.release_product(self.conn, job["job_id"],
                                     {"serial": "SN-ENV"})
        self.assertEqual(res["state"], "released")

    def test_10_incomplete_layers_block_release(self):
        job = self.complete_job(total=10, expected=60.0, charge=90.0,
                                layers=9)  # layer 10 missing, 54g consumed
        jid = job["job_id"]
        # material is physically settled and balanced, but the gate still
        # detects the missing layer and the 54g vs 60g preview mismatch
        self.expect_error(409, "release_gate_failed",
                          guards.release_product, self.conn, jid,
                          {"serial": "SN-IC"})

    def test_11_inspection_must_bind_snapshot(self):
        job = self.new_job()
        self.expect_error(409, "snapshot_mismatch",
                          guards.add_inspection, self.conn, job["job_id"],
                          {"inspector": "甲", "method": "CT",
                           "result": "pass", "snapshot_hash": "other"})

    def test_12_scrap_and_release_leave_single_terminal_state(self):
        job = self.complete_job()
        jid = job["job_id"]
        # scrap first
        guards.scrap_product(self.conn, jid, {"reason": "客户取消",
                                              "serial": "SN-S"})
        self.expect_error(409, "already_scrapped",
                          guards.release_product, self.conn, jid,
                          {"serial": "SN-S"})
        self.expect_error(409, "already_scrapped",
                          guards.scrap_product, self.conn, jid,
                          {"reason": "再次报废"})
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM products WHERE job_id = ?",
            (jid,)).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_13_released_cannot_be_scrapped(self):
        job = self.complete_job()
        jid = job["job_id"]
        guards.release_product(self.conn, jid, {"serial": "SN-R"})
        self.expect_error(409, "already_released",
                          guards.scrap_product, self.conn, jid,
                          {"reason": "想反悔"})

    def test_14_qc_fail_blocks_release(self):
        job = self.complete_job(inspect="fail")
        self.expect_error(409, "release_gate_failed",
                          guards.release_product, self.conn, job["job_id"],
                          {"serial": "SN-F"})
        # a later passing inspection re-opens the gate (latest result wins)
        guards.add_inspection(self.conn, job["job_id"], {
            "inspector": "乙", "method": "复检 CT", "result": "pass"})
        res = guards.release_product(self.conn, job["job_id"],
                                     {"serial": "SN-F2"})
        self.assertEqual(res["state"], "released")

    def test_15_concurrent_release_vs_scrap_exactly_one_wins(self):
        # build two fully ready jobs, then race release vs scrap on each
        winners = []
        lock = threading.Lock()

        def race(job_id, mode, err_collect):
            conn = dbmod.connect()
            try:
                if mode == "release":
                    guards.release_product(conn, job_id,
                                           {"serial": f"SN-{job_id[-3:]}"})
                    with lock:
                        winners.append((job_id, "released"))
                else:
                    guards.scrap_product(conn, job_id,
                                         {"reason": "并发报废测试",
                                          "serial": f"SN-{job_id[-3:]}"})
                    with lock:
                        winners.append((job_id, "scrapped"))
            except ApiError as e:
                err_collect.append((mode, e.error))
            finally:
                conn.close()

        jobs = [self.complete_job() for _ in range(2)]
        threads, errs = [], []
        for j in jobs:
            t1 = threading.Thread(target=race,
                                  args=(j["job_id"], "release", errs))
            t2 = threading.Thread(target=race,
                                  args=(j["job_id"], "scrap", errs))
            threads += [t1, t2]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for j in jobs:
            state = self.conn.execute(
                "SELECT state FROM products WHERE job_id = ?",
                (j["job_id"],)).fetchone()["state"]
            n = self.conn.execute(
                "SELECT COUNT(*) c FROM products WHERE job_id = ?",
                (j["job_id"],)).fetchone()["c"]
            self.assertEqual(n, 1)
            self.assertIn(state, ("released", "scrapped"))
            row = self.conn.execute(
                "SELECT status FROM jobs WHERE job_id = ?",
                (j["job_id"],)).fetchone()
            self.assertEqual(row["status"], state)

    def test_16_layer_out_of_range_and_total_consumption_mismatch(self):
        job = self.new_job(total=10, expected=60.0)
        jid, snap = job["job_id"], job["snapshot_hash"]
        s = guards.create_session(self.conn, jid,
                                  {"kind": "start", "device_id": "D1"})
        self.expect_error(422, "layer_out_of_range",
                          guards.upload_layer, self.conn, jid, {
                              "layer_no": 11, "receipt_id": "R-OOR",
                              "snapshot_hash": snap,
                              "session_id": s["session_id"],
                              "device_id": "D1",
                              "material_usages": [
                                  {"batch_code": "B1", "weight_g": 6}],
                              "env_temp_c": 24, "env_humidity_pct": 40})

    def test_17_terminal_job_rejects_mutation(self):
        job = self.complete_job()
        jid = job["job_id"]
        guards.release_product(self.conn, jid, {"serial": "SN-T"})
        self.expect_error(409, "job_terminal", guards.upload_layer,
                          self.conn, jid, {
                              "layer_no": 1, "receipt_id": "R-AFTER",
                              "snapshot_hash": job["snapshot_hash"],
                              "session_id": "whatever", "device_id": "D1",
                              "material_usages": [
                                  {"batch_code": "B1", "weight_g": 6}],
                              "env_temp_c": 24, "env_humidity_pct": 40})

    def test_18_multiple_batches_conservation(self):
        # 60g over two declared batches, 30g each; charge 45+45, return 15+15
        job = self.new_job(total=10, expected=60.0, batches=[
            {"batch_code": "B1", "material_type": "PA12", "grade": "g",
             "planned_weight_g": 45},
            {"batch_code": "B2", "material_type": "PA12", "grade": "g",
             "planned_weight_g": 45}])
        jid, snap = job["job_id"], job["snapshot_hash"]
        s = guards.create_session(self.conn, jid,
                                  {"kind": "start", "device_id": "D1"})
        guards.charge_material(self.conn, jid, {
            "batch_code": "B1", "weight_g": 45,
            "session_id": s["session_id"]})
        guards.charge_material(self.conn, jid, {
            "batch_code": "B2", "weight_g": 45,
            "session_id": s["session_id"], "spool_change": True})
        for n in range(1, 11):
            guards.upload_layer(self.conn, jid, {
                "layer_no": n, "receipt_id": f"R-MB-{n:02d}",
                "snapshot_hash": snap, "session_id": s["session_id"],
                "device_id": "D1",
                "material_usages": [
                    {"batch_code": "B1", "weight_g": 3.0},
                    {"batch_code": "B2", "weight_g": 3.0}],
                "env_temp_c": 24, "env_humidity_pct": 40})
        guards.end_session(self.conn, jid, s["session_id"],
                           {"reason": "completed"})
        guards.return_material(self.conn, jid, {
            "batch_code": "B1", "weight_g": 15,
            "session_id": s["session_id"]})
        guards.return_material(self.conn, jid, {
            "batch_code": "B2", "weight_g": 15,
            "session_id": s["session_id"]})
        guards.close_material(self.conn, jid)
        guards.add_inspection(self.conn, jid, {
            "inspector": "甲", "method": "CT", "result": "pass"})
        res = guards.release_product(self.conn, jid, {"serial": "SN-MB"})
        # certificate enumerates both declared batches
        self.assertEqual(
            len(res["certificate"]["payload"]["declarations"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
