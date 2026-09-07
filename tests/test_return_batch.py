# file: tests/test_return_batch.py
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import assettrack.db as db
from assettrack.intake import app as intake_app
from tests.auth_test_utils import create_test_user, login_session


class ReturnBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "assettrack.db"
        self.conn = db.get_connection()
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_tag TEXT NOT NULL UNIQUE,
                location_type TEXT NULL,
                current_holder_id INTEGER NULL,
                home_slot_id INTEGER NULL
            );
            """
        )
        self.conn.commit()
        self.client = intake_app.app.test_client()
        intake_app.app.testing = True
        intake_app.SCAN_QUEUE.clear()
        operator_user_id = create_test_user(username="operator", password="op-pass", role="operator")
        login_session(self.client, operator_user_id)

    def tearDown(self) -> None:
        intake_app.SCAN_QUEUE.clear()
        self.conn.close()
        self.temp_dir.cleanup()

    def _insert_asset(self, asset_tag: str, *, location_type: str, holder_id: int | None, home_slot_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO assets (asset_tag, location_type, current_holder_id, home_slot_id)
            VALUES (?, ?, ?, ?);
            """,
            (asset_tag, location_type, holder_id, home_slot_id),
        )
        self.conn.commit()

    def _insert_slot(self, slot_id: int, case_name: str, slot_position: int, current_asset_tag: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO slots (id, case_name, slot_position, current_asset_tag)
            VALUES (?, ?, ?, ?);
            """,
            (slot_id, case_name, slot_position, current_asset_tag),
        )
        self.conn.commit()

    def _insert_holder(self, holder_id: int, name: str, email: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO holders (
                id, holder_type, name, identifier, email, contact_info, created_at, updated_at
            )
            VALUES (?, 'PERSON', ?, ?, ?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """,
            (holder_id, name, f"H-{holder_id}", email),
        )
        self.conn.commit()

    def test_return_preview_and_commit_gating(self) -> None:
        self._insert_holder(5, "Return Holder Five")
        self._insert_holder(9, "Return Holder Nine", email="return@example.org")
        self._insert_slot(10, "A", 1, None)
        self._insert_slot(20, "B", 2, None)
        self._insert_asset("TAG-VALID", location_type="IN_CUSTODY", holder_id=5, home_slot_id=10)
        self._insert_asset("TAG-OK", location_type="IN_CUSTODY", holder_id=9, home_slot_id=20)

        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-VALID", equipment_type="laptop"))

        render = self.client.get("/return")
        self.assertEqual(render.status_code, 200)
        self.assertIn(b"Return", render.data)
        self.assertIn(b">Return<", render.data)
        self.assertIn(b"Stage Returned Assets", render.data)
        self.assertIn(b"Staged only. Nothing returns until commit.", render.data)
        self.assertNotIn(b"Stage scans in the queue, then review the batch before commit.", render.data)
        self.assertIn(b"Scan or enter asset tag", render.data)
        self.assertIn(b"Add to queue", render.data)
        self.assertIn(b"Review Before Return", render.data)
        self.assertNotIn(b"Preview Queue", render.data)
        self.assertNotIn(b"Home location: Home slots", render.data)
        self.assertIn(b"1 staged return", render.data)
        self.assertIn(b"Inspect or remove staged returns", render.data)

        preview_render = self.client.get("/return/preview")
        self.assertEqual(preview_render.status_code, 200)
        self.assertIn(b"Return Preview", preview_render.data)
        self.assertIn(b"Commit", preview_render.data)
        self.assertIn(b"Ready to Return", preview_render.data)
        self.assertNotIn(b"Home location: Home slots", preview_render.data)
        self.assertNotIn(b"1 asset queued", preview_render.data)
        self.assertIn(b"Current State", preview_render.data)
        self.assertIn(b"Review home slots and selected return destinations before commit.", preview_render.data)
        self.assertIn(b"Confirm reviewed returns.", preview_render.data)
        self.assertNotIn(b"Commit only after blocked items are resolved.", preview_render.data)
        self.assertIn(b"Return Destination", preview_render.data)
        self.assertIn(b"Location: IN_CUSTODY", preview_render.data)
        self.assertIn(b"Issued to: Return Holder Five", preview_render.data)
        self.assertIn(b"Home slot: A / 1", preview_render.data)
        self.assertIn(b"Return destination: A / 1", preview_render.data)
        self.assertIn(b"Permanent home slot: A / 1", preview_render.data)
        self.assertIn(b'name="confirm_responsibility_ack"', preview_render.data)
        self.assertIn(b"responsibility for this return batch was acknowledged before commit", preview_render.data)
        self.assertNotIn(b"null", preview_render.data)

        unreviewed = self.client.post("/return/commit?json=1")
        self.assertEqual(unreviewed.status_code, 400)
        self.assertFalse(unreviewed.json["ok"])
        self.assertEqual(unreviewed.json["committed"], 0)
        self.assertEqual(
            unreviewed.json["error"],
            "Please confirm you reviewed the batch before returning assets.",
        )

        missing_ack = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on"},
        )
        self.assertEqual(missing_ack.status_code, 400)
        self.assertFalse(missing_ack.json["ok"])
        self.assertEqual(missing_ack.json["committed"], 0)
        self.assertEqual(
            missing_ack.json["error"],
            "Confirm responsibility acknowledgment before returning assets.",
        )

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-VALID", equipment_type="laptop"))
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="UNKNOWN", equipment_type="laptop"))

        blocked = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertFalse(blocked.json["ok"])
        self.assertEqual(blocked.json["committed"], 0)

        valid_asset = self.conn.execute(
            "SELECT location_type, current_holder_id FROM assets WHERE asset_tag = ?;",
            ("TAG-VALID",),
        ).fetchone()
        self.assertEqual(valid_asset["location_type"], "IN_CUSTODY")
        self.assertEqual(valid_asset["current_holder_id"], 5)
        slot_after_block = self.conn.execute(
            "SELECT current_asset_tag FROM slots WHERE id = ?;",
            (10,),
        ).fetchone()
        self.assertIsNone(slot_after_block["current_asset_tag"])

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-OK", equipment_type="laptop"))

        success = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
        )
        self.assertEqual(success.status_code, 200)
        self.assertTrue(success.json["ok"])
        self.assertEqual(success.json["committed"], 1)
        self.assertIsInstance(success.json["receipt_id"], int)
        self.assertEqual(success.json["error"], None)
        self.assertEqual(len(intake_app.SCAN_QUEUE), 0)

        asset_after = self.conn.execute(
            "SELECT location_type, current_holder_id FROM assets WHERE asset_tag = ?;",
            ("TAG-OK",),
        ).fetchone()
        self.assertEqual(asset_after["location_type"], "STORAGE")
        self.assertIsNone(asset_after["current_holder_id"])

        occupancy_after = self.conn.execute(
            """
            SELECT slot_id
            FROM slot_occupancy
            WHERE asset_id = (SELECT id FROM assets WHERE asset_tag = ?)
            LIMIT 1;
            """,
            ("TAG-OK",),
        ).fetchone()
        self.assertIsNotNone(occupancy_after)
        self.assertEqual(int(occupancy_after["slot_id"]), 20)

        slot_after = self.conn.execute(
            "SELECT current_asset_tag FROM slots WHERE id = ?;",
            (20,),
        ).fetchone()
        self.assertEqual(slot_after["current_asset_tag"], "TAG-OK")

        event_row = self.conn.execute(
            """
            SELECT id, event_type, payload FROM asset_events
            WHERE asset_tag = ?
            ORDER BY id DESC
            LIMIT 1;
            """,
            ("TAG-OK",),
        ).fetchone()
        receipt_row = self.conn.execute(
            """
            SELECT receipt_type, commit_operator_user_id, holder_id, source_event_ids_json, snapshot_json, sent_at, last_attempt_at, last_error
            FROM receipt_queue
            ORDER BY id DESC
            LIMIT 1;
            """
        ).fetchone()
        self.assertIsNotNone(event_row)
        self.assertEqual(event_row["event_type"], "RETURN")
        payload = json.loads(str(event_row["payload"]))
        self.assertEqual(payload["from_location_type"], "IN_CUSTODY")
        self.assertEqual(payload["to_location_type"], "STORAGE")
        self.assertEqual(int(payload["home_slot_id"]), 20)
        self.assertTrue(payload["responsibility_ack"]["acknowledged"])
        self.assertEqual(int(payload["responsibility_ack"]["ack_holder_id"]), 9)
        self.assertGreater(int(payload["responsibility_ack"]["ack_operator_user_id"]), 0)
        self.assertTrue(payload["responsibility_ack"]["ack_at"])
        self.assertEqual(payload["responsibility_ack"]["ack_scope"], "batch")
        self.assertIsNotNone(receipt_row)
        self.assertEqual(receipt_row["receipt_type"], "RETURN")
        self.assertGreater(int(receipt_row["commit_operator_user_id"]), 0)
        self.assertEqual(int(receipt_row["holder_id"]), 9)
        self.assertEqual(json.loads(str(receipt_row["source_event_ids_json"])), [int(event_row["id"])])
        receipt_snapshot = json.loads(str(receipt_row["snapshot_json"]))
        self.assertEqual(receipt_snapshot["receipt_type"], "RETURN")
        self.assertEqual(receipt_snapshot["holder_id"], 9)
        self.assertEqual(receipt_snapshot["source_event_ids"], [int(event_row["id"])])
        self.assertEqual(receipt_snapshot["delivery"]["state"], "pending")
        self.assertEqual(receipt_snapshot["recipient_email"], "return@example.org")
        self.assertEqual(receipt_snapshot["holder_snapshot"]["email"], "return@example.org")
        self.assertEqual(len(receipt_snapshot["assets"]), 1)
        self.assertEqual(receipt_snapshot["assets"][0]["asset_tag"], "TAG-OK")
        self.assertEqual(receipt_snapshot["assets"][0]["from_holder_snapshot"]["email"], "return@example.org")
        self.assertEqual(receipt_snapshot["assets"][0]["from_location_type"], "IN_CUSTODY")
        self.assertEqual(receipt_snapshot["assets"][0]["to_location_type"], "STORAGE")
        self.assertIsNone(receipt_row["sent_at"])
        self.assertIsNone(receipt_row["last_attempt_at"])
        self.assertIsNone(receipt_row["last_error"])

    def test_return_preview_explains_missing_assigned_home_slot_blocker(self) -> None:
        self._insert_asset("NO-HOME", location_type="IN_CUSTODY", holder_id=5, home_slot_id=None)
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="NO-HOME", equipment_type="laptop"))

        preview = self.client.get("/return/preview")
        html = preview.data.decode("utf-8")

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Review home slots and selected return destinations before commit.", preview.data)
        self.assertIn(b"Commit only after blocked items are resolved.", preview.data)
        self.assertNotIn(b"Confirm reviewed returns.", preview.data)
        self.assertIn("Needs Review", html)
        self.assertIn("Blocked Items", html)
        self.assertNotIn("<template>", html)
        self.assertIn("<li>No assigned home slot: NO-HOME</li>", html)
        self.assertIn(
            "<li>No assigned home slot. Return cannot commit until a home slot is assigned.</li>",
            html,
        )
        self.assertIn(b"Conflicts must be resolved before committing this batch.", preview.data)
        self.assertNotIn(b"Commit Return", preview.data)

        blocked = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
            follow_redirects=False,
        )

        self.assertEqual(blocked.status_code, 302)
        self.assertTrue((blocked.headers.get("Location") or "").endswith("/return/preview"))
        self.assertEqual(len(intake_app.SCAN_QUEUE), 1)

        asset_after = self.conn.execute(
            "SELECT location_type, current_holder_id FROM assets WHERE asset_tag = ?;",
            ("NO-HOME",),
        ).fetchone()
        event_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM asset_events WHERE asset_tag = ? AND event_type = 'RETURN';",
            ("NO-HOME",),
        ).fetchone()
        receipt_count = self.conn.execute("SELECT COUNT(*) AS c FROM receipt_queue;").fetchone()

        self.assertIsNotNone(asset_after)
        self.assertEqual(asset_after["location_type"], "IN_CUSTODY")
        self.assertEqual(asset_after["current_holder_id"], 5)
        self.assertEqual(int(event_count["c"]), 0)
        self.assertEqual(int(receipt_count["c"]), 0)

    def test_return_preview_explains_occupied_assigned_home_slot_blocker(self) -> None:
        self._insert_slot(12, "CASE-BLOCKED", 4, "OTHER-ASSET")
        self._insert_asset("OCCUPIED-HOME", location_type="IN_CUSTODY", holder_id=5, home_slot_id=12)
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="OCCUPIED-HOME", equipment_type="laptop"))

        preview = self.client.get("/return/preview")

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Return destination required: OCCUPIED-HOME", preview.data)
        self.assertIn(b"Choose an empty return destination.", preview.data)
        self.assertIn(b"Conflicts must be resolved before committing this batch.", preview.data)

    def test_return_preview_blocks_home_slot_occupied_by_slot_occupancy_when_marker_is_null(self) -> None:
        self._insert_slot(13, "CASE-DRIFT", 1, None)
        self._insert_asset("RETURN-DRIFT", location_type="IN_CUSTODY", holder_id=5, home_slot_id=13)
        self._insert_asset("OCCUPANT-DRIFT", location_type="STORAGE", holder_id=None, home_slot_id=13)
        occupant_id = int(
            self.conn.execute(
                "SELECT id FROM assets WHERE asset_tag = ? LIMIT 1;",
                ("OCCUPANT-DRIFT",),
            ).fetchone()[0]
        )
        self.conn.execute(
            """
            INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
            VALUES (?, ?, ?);
            """,
            (13, occupant_id, "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="RETURN-DRIFT", equipment_type="laptop"))

        preview = self.client.get("/return/preview")

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Return destination required: RETURN-DRIFT", preview.data)
        self.assertIn(b"Choose an empty return destination.", preview.data)
        self.assertIn(b"Conflicts must be resolved before committing this batch.", preview.data)
        self.assertNotIn(b"Commit Return", preview.data)

    def test_return_commit_blocks_home_slot_occupied_by_slot_occupancy_when_marker_is_null(self) -> None:
        self._insert_slot(14, "CASE-COMMIT-DRIFT", 2, None)
        self._insert_asset("RETURN-COMMIT-DRIFT", location_type="IN_CUSTODY", holder_id=5, home_slot_id=14)
        self._insert_asset("OCCUPANT-COMMIT-DRIFT", location_type="STORAGE", holder_id=None, home_slot_id=14)
        occupant_id = int(
            self.conn.execute(
                "SELECT id FROM assets WHERE asset_tag = ? LIMIT 1;",
                ("OCCUPANT-COMMIT-DRIFT",),
            ).fetchone()[0]
        )
        self.conn.execute(
            """
            INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
            VALUES (?, ?, ?);
            """,
            (14, occupant_id, "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="RETURN-COMMIT-DRIFT", equipment_type="laptop"))

        blocked = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
        )

        self.assertEqual(blocked.status_code, 400)
        self.assertFalse(blocked.json["ok"])
        self.assertEqual(blocked.json["committed"], 0)
        self.assertIn("Return destination required: RETURN-COMMIT-DRIFT", blocked.json["error"])

        returned_asset = self.conn.execute(
            "SELECT location_type, current_holder_id FROM assets WHERE asset_tag = ?;",
            ("RETURN-COMMIT-DRIFT",),
        ).fetchone()
        self.assertEqual(returned_asset["location_type"], "IN_CUSTODY")
        self.assertEqual(returned_asset["current_holder_id"], 5)
        event_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM asset_events WHERE asset_tag = ? AND event_type = 'RETURN';",
            ("RETURN-COMMIT-DRIFT",),
        ).fetchone()
        receipt_count = self.conn.execute("SELECT COUNT(*) AS c FROM receipt_queue;").fetchone()
        self.assertEqual(int(event_count["c"]), 0)
        self.assertEqual(int(receipt_count["c"]), 0)

    def test_return_preview_blocks_home_slot_marker_occupied_when_slot_occupancy_is_missing(self) -> None:
        self._insert_slot(15, "CASE-MARKER-DRIFT", 3, "MARKER-OCCUPANT")
        self._insert_asset("RETURN-MARKER-DRIFT", location_type="IN_CUSTODY", holder_id=5, home_slot_id=15)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM slot_occupancy WHERE slot_id = ? LIMIT 1;",
                (15,),
            ).fetchone()
        )
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="RETURN-MARKER-DRIFT", equipment_type="laptop"))

        preview = self.client.get("/return/preview")

        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Return destination required: RETURN-MARKER-DRIFT", preview.data)
        self.assertIn(b"Choose an empty return destination.", preview.data)
        self.assertIn(b"Conflicts must be resolved before committing this batch.", preview.data)
        self.assertNotIn(b"Commit Return", preview.data)

    def test_return_commit_blocks_home_slot_marker_occupied_when_slot_occupancy_is_missing(self) -> None:
        self._insert_slot(16, "CASE-COMMIT-MARKER-DRIFT", 4, "MARKER-COMMIT-OCCUPANT")
        self._insert_asset("RETURN-COMMIT-MARKER-DRIFT", location_type="IN_CUSTODY", holder_id=5, home_slot_id=16)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM slot_occupancy WHERE slot_id = ? LIMIT 1;",
                (16,),
            ).fetchone()
        )
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="RETURN-COMMIT-MARKER-DRIFT", equipment_type="laptop"))

        blocked = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
        )

        self.assertEqual(blocked.status_code, 400)
        self.assertFalse(blocked.json["ok"])
        self.assertEqual(blocked.json["committed"], 0)
        self.assertIn("Return destination required: RETURN-COMMIT-MARKER-DRIFT", blocked.json["error"])

        returned_asset = self.conn.execute(
            "SELECT location_type, current_holder_id FROM assets WHERE asset_tag = ?;",
            ("RETURN-COMMIT-MARKER-DRIFT",),
        ).fetchone()
        self.assertEqual(returned_asset["location_type"], "IN_CUSTODY")
        self.assertEqual(returned_asset["current_holder_id"], 5)
        event_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM asset_events WHERE asset_tag = ? AND event_type = 'RETURN';",
            ("RETURN-COMMIT-MARKER-DRIFT",),
        ).fetchone()
        receipt_count = self.conn.execute("SELECT COUNT(*) AS c FROM receipt_queue;").fetchone()
        self.assertEqual(int(event_count["c"]), 0)
        self.assertEqual(int(receipt_count["c"]), 0)

    def test_return_queue_and_preview_empty_states_show_next_step_guidance(self) -> None:
        render = self.client.get("/return")
        self.assertEqual(render.status_code, 200)
        self.assertIn(
            b"No returns staged.",
            render.data,
        )

        preview_render = self.client.get("/return/preview")
        self.assertEqual(preview_render.status_code, 200)
        self.assertIn(
            b"No assets queued.",
            preview_render.data,
        )

    def test_return_commit_restores_slot_occupancy_after_issue_path_removal(self) -> None:
        self._insert_holder(5, "Return Holder Five")
        self._insert_slot(55, "CASE-55", 5, None)
        self._insert_asset("TAG-RESTORE", location_type="IN_CUSTODY", holder_id=5, home_slot_id=55)

        asset_id = int(
            self.conn.execute(
                "SELECT id FROM assets WHERE asset_tag = ? LIMIT 1;",
                ("TAG-RESTORE",),
            ).fetchone()[0]
        )

        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM slot_occupancy WHERE asset_id = ? LIMIT 1;",
                (asset_id,),
            ).fetchone()
        )

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-RESTORE", equipment_type="laptop"))

        success = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
        )

        self.assertEqual(success.status_code, 200)
        self.assertTrue(success.json["ok"])

        occupancy_after = self.conn.execute(
            "SELECT slot_id FROM slot_occupancy WHERE asset_id = ? LIMIT 1;",
            (asset_id,),
        ).fetchone()
        self.assertIsNotNone(occupancy_after)
        self.assertEqual(int(occupancy_after["slot_id"]), 55)

        slot_after = self.conn.execute(
            "SELECT current_asset_tag FROM slots WHERE id = 55 LIMIT 1;"
        ).fetchone()
        self.assertIsNotNone(slot_after)
        self.assertEqual(str(slot_after["current_asset_tag"]), "TAG-RESTORE")

    def test_return_commit_with_mixed_holders_keeps_snapshot_email_blank(self) -> None:
        self._insert_holder(5, "Return Holder Five", email="five@example.org")
        self._insert_holder(9, "Return Holder Nine", email="nine@example.org")
        self._insert_slot(10, "A", 1, None)
        self._insert_slot(20, "B", 2, None)
        self._insert_asset("TAG-ONE", location_type="IN_CUSTODY", holder_id=5, home_slot_id=10)
        self._insert_asset("TAG-TWO", location_type="IN_CUSTODY", holder_id=9, home_slot_id=20)

        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-ONE", equipment_type="laptop"))
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-TWO", equipment_type="laptop"))

        success = self.client.post(
            "/return/commit?json=1",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
        )

        self.assertEqual(success.status_code, 200)

        receipt_row = self.conn.execute(
            """
            SELECT holder_id, snapshot_json
            FROM receipt_queue
            ORDER BY id DESC
            LIMIT 1;
            """
        ).fetchone()
        self.assertIsNotNone(receipt_row)
        self.assertIsNone(receipt_row["holder_id"])
        receipt_snapshot = json.loads(str(receipt_row["snapshot_json"]))
        self.assertIsNone(receipt_snapshot["holder_id"])
        self.assertEqual(receipt_snapshot["recipient_email"], "")

    def test_return_commit_missing_ack_redirects_back_to_preview_with_message(self) -> None:
        self._insert_slot(21, "C", 3, None)
        self._insert_asset("TAG-MSG", location_type="IN_CUSTODY", holder_id=11, home_slot_id=21)

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TAG-MSG", equipment_type="laptop"))

        blocked = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on"},
            follow_redirects=False,
        )

        self.assertEqual(blocked.status_code, 302)
        self.assertTrue((blocked.headers.get("Location") or "").endswith("/return/preview"))

        follow = self.client.get("/return/preview")
        self.assertEqual(follow.status_code, 200)
        self.assertIn(b"Confirm responsibility acknowledgment before returning assets.", follow.data)
        self.assertEqual(len(intake_app.SCAN_QUEUE), 1)
        receipt_count = self.conn.execute("SELECT COUNT(*) AS c FROM receipt_queue;").fetchone()
        self.assertEqual(int(receipt_count["c"]), 0)

    def test_single_asset_return_success_message_shows_final_location(self) -> None:
        self._insert_slot(30, "CASE-13", 6, None)
        self._insert_asset("MVPLAPTOP02", location_type="IN_CUSTODY", holder_id=7, home_slot_id=30)

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="MVPLAPTOP02", equipment_type="laptop"))

        response = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Return Receipt", response.data)
        self.assertIn(b"MVPLAPTOP02", response.data)
        self.assertIn(b"CASE-13 / 6", response.data)

        asset_after = self.conn.execute(
            "SELECT location_type, current_holder_id FROM assets WHERE asset_tag = ?;",
            ("MVPLAPTOP02",),
        ).fetchone()
        self.assertIsNotNone(asset_after)
        self.assertEqual(asset_after["location_type"], "STORAGE")
        self.assertIsNone(asset_after["current_holder_id"])

    def test_return_commit_redirects_to_exact_created_receipt(self) -> None:
        self._insert_slot(31, "CASE-14", 2, None)
        self._insert_asset("RETURN-REDIRECT", location_type="IN_CUSTODY", holder_id=12, home_slot_id=31)

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="RETURN-REDIRECT", equipment_type="laptop"))

        response = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        receipt_row = self.conn.execute(
            "SELECT id FROM receipt_queue ORDER BY id DESC LIMIT 1;"
        ).fetchone()
        self.assertIsNotNone(receipt_row)
        self.assertTrue((response.headers.get("Location") or "").endswith(f"/receipts/{int(receipt_row['id'])}"))

    def test_multi_asset_return_same_case_shows_one_case_drilldown_link(self) -> None:
        self._insert_slot(40, "CASE-SAME", 1, None)
        self._insert_slot(41, "CASE-SAME", 2, None)
        self._insert_asset("SAME-1", location_type="IN_CUSTODY", holder_id=7, home_slot_id=40)
        self._insert_asset("SAME-2", location_type="IN_CUSTODY", holder_id=8, home_slot_id=41)

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="SAME-1", equipment_type="laptop"))
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="SAME-2", equipment_type="laptop"))

        response = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Return Receipt", response.data)
        self.assertIn(b"SAME-1", response.data)
        self.assertIn(b"SAME-2", response.data)

    def test_multi_asset_return_different_cases_shows_one_link_per_case(self) -> None:
        self._insert_slot(50, "CASE-X", 1, None)
        self._insert_slot(60, "CASE-Y", 1, None)
        self._insert_asset("DIFF-1", location_type="IN_CUSTODY", holder_id=7, home_slot_id=50)
        self._insert_asset("DIFF-2", location_type="IN_CUSTODY", holder_id=8, home_slot_id=60)

        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="DIFF-1", equipment_type="laptop"))
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="DIFF-2", equipment_type="laptop"))

        response = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Return Receipt", response.data)
        self.assertIn(b"DIFF-1", response.data)
        self.assertIn(b"DIFF-2", response.data)

    def test_return_queue_can_remove_one_item_and_preview_only_remaining_items(self) -> None:
        intake_app.SCAN_QUEUE.clear()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="REMOVE-ME", equipment_type="laptop"))
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="KEEP-ME", equipment_type="laptop"))

        remove = self.client.post(
            "/",
            data={"action": "remove", "queue_index": "0", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(remove.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["KEEP-ME"])
        self.assertIn(b"1 staged return", remove.data)
        self.assertIn(b"Inspect or remove staged returns", remove.data)

        preview = self.client.get("/return/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"KEEP-ME", preview.data)
        self.assertNotIn(b"REMOVE-ME", preview.data)

    def test_return_case_scan_expands_assets_by_home_case(self) -> None:
        self._insert_slot(70, "CASE-2", 1, None)
        self._insert_slot(71, "CASE-2", 2, None)
        self._insert_slot(72, "CASE-2", 3, None)
        self._insert_asset("RT-100", location_type="IN_CUSTODY", holder_id=5, home_slot_id=70)
        self._insert_asset("RT-101", location_type="IN_CUSTODY", holder_id=6, home_slot_id=71)

        response = self.client.post(
            "/",
            data={"scan_text": "case-2", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT100", "RT101"])
        self.assertIn(b"Case CASE-2 added 2 assets to queue.", response.data)
        self.assertIn(b"2 staged returns", response.data)
        self.assertNotIn(b"CASE2", response.data)

    def test_return_case_scan_excludes_assets_already_returned_to_storage(self) -> None:
        self._insert_slot(73, "CASE-3", 1, current_asset_tag="RT-300")
        self._insert_slot(74, "CASE-3", 2, None)
        self._insert_asset("RT-300", location_type="STORAGE", holder_id=None, home_slot_id=73)
        self._insert_asset("RT-301", location_type="IN_CUSTODY", holder_id=6, home_slot_id=74)

        response = self.client.post(
            "/",
            data={"scan_text": "CASE-3", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT301"])
        self.assertIn(b"Case CASE-3 added 1 asset to queue.", response.data)
        self.assertNotIn(b"RT300", response.data)

    def test_return_case_scan_skips_assets_already_queued(self) -> None:
        self._insert_slot(80, "CASE-20", 1, None)
        self._insert_slot(81, "CASE-20", 2, None)
        self._insert_asset("RT-200", location_type="IN_CUSTODY", holder_id=5, home_slot_id=80)
        self._insert_asset("RT-201", location_type="IN_CUSTODY", holder_id=6, home_slot_id=81)

        intake_app.SCAN_QUEUE.append(intake_app.Scan.now("RT200"))

        response = self.client.post(
            "/",
            data={"scan_text": "CASE20", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT200", "RT201"])
        self.assertIn(b"Case CASE-20 added 1 asset to queue. Skipped 1 already queued.", response.data)

    def test_return_case_scan_with_dash_matches_case_stored_without_dash(self) -> None:
        self._insert_slot(82, "CASE12", 1, None)
        self._insert_asset("RT-202", location_type="IN_CUSTODY", holder_id=5, home_slot_id=82)

        response = self.client.post(
            "/",
            data={"scan_text": "case-12", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT202"])
        self.assertIn(b"Case CASE12 added 1 asset to queue.", response.data)

    def test_return_case_scan_allows_selective_removal_before_preview(self) -> None:
        self._insert_slot(90, "CASE-30", 1, None)
        self._insert_slot(91, "CASE-30", 2, None)
        self._insert_asset("RT-300", location_type="IN_CUSTODY", holder_id=5, home_slot_id=90)
        self._insert_asset("RT-301", location_type="IN_CUSTODY", holder_id=6, home_slot_id=91)

        scanned = self.client.post(
            "/",
            data={"scan_text": "CASE-30", "return_to": "/return"},
            follow_redirects=True,
        )
        self.assertEqual(scanned.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT300", "RT301"])

        removed = self.client.post(
            "/",
            data={"action": "remove", "queue_index": "0", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(removed.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT301"])
        self.assertIn(b"1 staged return", removed.data)

        preview = self.client.get("/return/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"RT-301", preview.data)
        self.assertNotIn(b"RT-300", preview.data)

    def test_return_scan_normalizes_asset_tag_to_uppercase_and_blocks_case_variant_duplicate(self) -> None:
        self._insert_slot(26, "CASE-RT", 2, None)
        self._insert_asset("RT200", location_type="IN_CUSTODY", holder_id=9, home_slot_id=26)

        first = self.client.post(
            "/",
            data={"scan_text": "rt-200", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT200"])
        self.assertIn(b"RT200", first.data)
        self.assertNotIn(b"rt-200", first.data)

        second = self.client.post(
            "/",
            data={"scan_text": "RT200", "return_to": "/return"},
            follow_redirects=True,
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["RT200"])
        self.assertIn(b"Asset RT200 is already queued.", second.data)

        preview = self.client.get("/return/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"RT200", preview.data)
        self.assertNotIn(b"rt-200", preview.data)

    def test_return_scan_redirects_back_to_queue_anchor(self) -> None:
        self._insert_slot(25, "CASE-Z", 1, None)
        self._insert_asset("RT-ANCHOR-1", location_type="IN_CUSTODY", holder_id=5, home_slot_id=25)

        response = self.client.post(
            "/",
            data={"scan_text": "rt-anchor-1", "return_to": "/return"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue((response.headers.get("Location") or "").endswith("/return#queue-section"))

    def test_return_scan_lands_on_return_placement_with_default_home_destination(self) -> None:
        self._insert_slot(26, "HOME-RETURN", 1, None)
        self._insert_asset("RT-PLACEMENT-1", location_type="IN_CUSTODY", holder_id=5, home_slot_id=26)

        response = self.client.post(
            "/",
            data={"scan_text": "RT-PLACEMENT-1", "return_to": "/return"},
            follow_redirects=True,
        )
        html = response.data.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<div class="return-flow-section" id="queue-section">', html)
        self.assertIn("<h3>Return Placement</h3>", html)
        self.assertLess(html.index("<h3>Queue</h3>"), html.index('id="queue-section"'))
        self.assertIn("RT-PLACEMENT-1</code> home: HOME-RETURN / 1", html)
        self.assertIn('<option value="26" selected>HOME-RETURN / 1</option>', html)

    def test_return_to_alternate_empty_slot_preserves_home_and_receipt_evidence(self) -> None:
        self._insert_holder(19, "Temporary Return Holder", email="temporary-return@example.org")
        self._insert_slot(110, "HOME-CASE", 1, "HOME-OCCUPANT")
        self._insert_slot(111, "TEMP-CASE", 2, None)
        self._insert_asset("TEMP-RETURN", location_type="IN_CUSTODY", holder_id=19, home_slot_id=110)
        self.conn.execute(
            "UPDATE assets SET case_number = ?, slot_number = ? WHERE asset_tag = ?;",
            ("HOME-CASE", "1", "TEMP-RETURN"),
        )
        self.conn.commit()
        intake_app.SCAN_QUEUE.append(intake_app.Scan.now(asset_tag="TEMP-RETURN", equipment_type="laptop"))

        queue = self.client.get("/return")
        self.assertIn(b"Return Placement", queue.data)
        self.assertIn(b"HOME-CASE / 1", queue.data)
        self.assertIn(b"TEMP-CASE / 2", queue.data)

        select = self.client.post(
            "/return/destination",
            data={"asset_tag": "TEMP-RETURN", "destination_slot_id": "111"},
            follow_redirects=True,
        )
        self.assertEqual(select.status_code, 200)
        self.assertIn(b"Return destination set to TEMP-CASE / 2.", select.data)

        preview = self.client.get("/return/preview")
        self.assertIn(b"Home slot: HOME-CASE / 1", preview.data)
        self.assertIn(b"Return destination: TEMP-CASE / 2", preview.data)
        self.assertIn(b"Permanent home slot: HOME-CASE / 1", preview.data)

        committed = self.client.post(
            "/return/commit",
            data={"confirm_reviewed": "on", "confirm_responsibility_ack": "on"},
            follow_redirects=False,
        )
        self.assertEqual(committed.status_code, 302)
        self.assertEqual(len(intake_app.SCAN_QUEUE), 0)

        asset = self.conn.execute(
            "SELECT location_type, current_holder_id, home_slot_id, case_number, slot_number FROM assets WHERE asset_tag = ?;",
            ("TEMP-RETURN",),
        ).fetchone()
        self.assertEqual(asset["location_type"], "STORAGE")
        self.assertIsNone(asset["current_holder_id"])
        self.assertEqual(int(asset["home_slot_id"]), 110)
        self.assertEqual(asset["case_number"], "HOME-CASE")
        self.assertEqual(asset["slot_number"], "1")
        occupancy = self.conn.execute(
            "SELECT slot_id FROM slot_occupancy so JOIN assets a ON a.id = so.asset_id WHERE a.asset_tag = ?;",
            ("TEMP-RETURN",),
        ).fetchone()
        self.assertEqual(int(occupancy["slot_id"]), 111)
        self.assertEqual(
            self.conn.execute("SELECT current_asset_tag FROM slots WHERE id = 111;").fetchone()["current_asset_tag"],
            "TEMP-RETURN",
        )
        event = self.conn.execute(
            "SELECT payload FROM asset_events WHERE asset_tag = ? AND event_type = 'RETURN' ORDER BY id DESC LIMIT 1;",
            ("TEMP-RETURN",),
        ).fetchone()
        payload = json.loads(str(event["payload"]))
        self.assertEqual(int(payload["home_slot_id"]), 110)
        self.assertEqual(int(payload["return_slot_id"]), 111)
        receipt = self.conn.execute("SELECT snapshot_json FROM receipt_queue ORDER BY id DESC LIMIT 1;").fetchone()
        snapshot = json.loads(str(receipt["snapshot_json"]))
        self.assertEqual(int(snapshot["assets"][0]["home_slot"]["slot_id"]), 110)
        self.assertEqual(int(snapshot["assets"][0]["return_slot"]["slot_id"]), 111)

    def test_return_destination_accepts_canonical_tag_for_normalized_queued_scan(self) -> None:
        self._insert_slot(120, "HOME-SCAN", 1, "HOME-OCCUPANT")
        self._insert_slot(121, "TEMP-SCAN", 2, None)
        self._insert_asset("SCAN-RETURN", location_type="IN_CUSTODY", holder_id=5, home_slot_id=120)

        queued = self.client.post(
            "/",
            data={"scan_text": "scan-return", "return_to": "/return"},
            follow_redirects=True,
        )
        self.assertEqual(queued.status_code, 200)
        self.assertEqual([scan.asset_tag for scan in intake_app.SCAN_QUEUE], ["SCANRETURN"])

        selected = self.client.post(
            "/return/destination",
            data={"asset_tag": "SCAN-RETURN", "destination_slot_id": "121"},
            follow_redirects=True,
        )

        self.assertIn(b"Return destination set to TEMP-SCAN / 2.", selected.data)
        preview = self.client.get("/return/preview")
        self.assertIn(b"Return destination: TEMP-SCAN / 2", preview.data)

    def test_return_scan_validation_error_redirects_back_to_queue_anchor(self) -> None:
        response = self.client.post(
            "/",
            data={"scan_text": "missing-tag", "return_to": "/return"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue((response.headers.get("Location") or "").endswith("/return#queue-section"))


if __name__ == "__main__":
    unittest.main()
