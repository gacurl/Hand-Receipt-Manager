from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import assettrack.db as db
from assettrack.intake import app as intake_app
from tests.auth_test_utils import create_test_user, login_session


class AdminEditAssetUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "assettrack.db"
        self.conn = db.get_connection()
        intake_app.app.testing = True
        self.client = intake_app.app.test_client()
        admin_user_id = create_test_user(username="admin", password="admin-pass", role="admin")
        login_session(self.client, admin_user_id)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _insert_slot(self, slot_id: int, case_name: str, slot_position: int, *, current_asset_tag: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO slots (id, case_name, slot_position, current_asset_tag)
            VALUES (?, ?, ?, ?);
            """,
            (slot_id, case_name, slot_position, current_asset_tag),
        )
        self.conn.commit()

    def _insert_asset(
        self,
        asset_tag: str,
        *,
        serial_number: str,
        location_type: str,
        current_holder_id: int | None,
        home_slot_id: int | None,
        case_number: str | None = None,
        slot_number: str | None = None,
        manufacturer: str = "Dell",
        building: str = "HQ",
        room: str = "110",
        building_room: str = "HQ/110",
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO assets (
                asset_tag,
                serial_number,
                manufacturer,
                equipment_type,
                building,
                room,
                model,
                model_code,
                notes,
                building_room,
                custody_state,
                accountability_status,
                condition,
                created_date,
                updated_date,
                location_type,
                current_holder_id,
                home_slot_id,
                case_number,
                slot_number
            )
            VALUES (?, ?, ?, 'laptop', ?, ?, 'Latitude', '5400', 'seed', ?, 'in_stock', 'accountable', 'serviceable', '2026-01-01', '2026-01-01T00:00:00Z', ?, ?, ?, ?, ?);
            """,
            (
                asset_tag,
                serial_number,
                manufacturer,
                building,
                room,
                building_room,
                location_type,
                current_holder_id,
                home_slot_id,
                case_number,
                slot_number,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _insert_event(self, asset_tag: str, event_type: str = "ASSET_UPDATED") -> None:
        self.conn.execute(
            """
            INSERT INTO asset_events (asset_tag, event_type, event_date, actor, notes, payload, holder_id)
            VALUES (?, ?, '2026-01-02T00:00:00Z', 'admin', NULL, NULL, NULL);
            """,
            (asset_tag, event_type),
        )
        self.conn.commit()

    def _occupy_slot(self, slot_id: int, asset_id: int, asset_tag: str) -> None:
        self.conn.execute(
            """
            INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
            VALUES (?, ?, '2026-01-02T00:00:00Z');
            """,
            (slot_id, asset_id),
        )
        self.conn.execute("UPDATE slots SET current_asset_tag = ? WHERE id = ?;", (asset_tag, slot_id))
        self.conn.commit()

    def test_get_admin_edit_asset_route_allows_admin(self) -> None:
        response = self.client.get("/admin/assets/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Admin: Edit Asset", response.data)
        self.assertNotIn(b'<option value="tablet"', response.data)

    def test_admin_edit_asset_route_rejects_non_admin(self) -> None:
        operator_id = create_test_user(username="operator-edit-asset", password="op-pass", role="operator")
        login_session(self.client, operator_id)

        response = self.client.get("/admin/assets/edit")

        self.assertEqual(response.status_code, 403)

    def test_exact_lookup_loads_edit_form_directly(self) -> None:
        self._insert_asset(
            "AT-EXACT-EDIT-1",
            serial_number="SER-EXACT-EDIT-1",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "lookup",
                "lookup_asset_tag": "AT-EXACT-EDIT-1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Edit Asset", response.data)
        self.assertIn(b"AT-EXACT-EDIT-1", response.data)
        self.assertIn(b"Save Asset", response.data)
        self.assertIn(b"<strong>manufacturer</strong> (optional)", response.data)
        self.assertNotIn(b'id="manufacturer" name="manufacturer" value="Dell" required', response.data)
        self.assertIn(b"<strong>building</strong> (optional)", response.data)
        self.assertIn(b"<strong>room</strong> (optional)", response.data)
        self.assertNotIn(b'id="building" name="building" value="HQ" required', response.data)
        self.assertNotIn(b'id="room" name="room" value="110" required', response.data)
        self.assertNotIn(b"Select Asset", response.data)

    def test_partial_lookup_requires_explicit_selection_before_edit(self) -> None:
        self._insert_asset(
            "AT-PART-EDIT-100",
            serial_number="SER-PART-EDIT-100",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )
        self._insert_asset(
            "AT-PART-EDIT-101",
            serial_number="SER-PART-EDIT-101",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "lookup",
                "lookup_asset_tag": "AT-PART-EDIT-10",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Select Asset", response.data)
        self.assertIn(b"AT-PART-EDIT-100", response.data)
        self.assertIn(b"AT-PART-EDIT-101", response.data)
        self.assertNotIn(b"Save Asset", response.data)
        self.assertIn(b">Select<", response.data)

        selected = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "lookup",
                "lookup_asset_tag": "AT-PART-EDIT-100",
            },
        )

        self.assertEqual(selected.status_code, 200)
        self.assertIn(b"Edit Asset", selected.data)
        self.assertIn(b"Save Asset", selected.data)
        self.assertIn(b"AT-PART-EDIT-100", selected.data)
        self.assertNotIn(b"AT-PART-EDIT-101", selected.data)

    def test_partial_query_string_does_not_load_edit_form_directly(self) -> None:
        self._insert_asset(
            "AT-QUERY-EDIT-100",
            serial_number="SER-QUERY-EDIT-100",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )
        self._insert_asset(
            "AT-QUERY-EDIT-101",
            serial_number="SER-QUERY-EDIT-101",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.get("/admin/assets/edit?asset_tag=AT-QUERY-EDIT-10")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Select Asset", response.data)
        self.assertIn(b"AT-QUERY-EDIT-100", response.data)
        self.assertIn(b"AT-QUERY-EDIT-101", response.data)
        self.assertNotIn(b"Save Asset", response.data)

    def test_edit_asset_ui_marks_retired_assets_as_not_in_service(self) -> None:
        self._insert_asset(
            "AT-RET-EDIT-1",
            serial_number="SER-RET-EDIT-1",
            location_type="DISPOSED",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.get("/admin/assets/edit?asset_tag=AT-RET-EDIT-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"RETIRED \xe2\x80\x94 Not in service", response.data)
        self.assertNotIn(b"Retired / disposed", response.data)

    def test_edit_storage_asset_updates_home_without_moving_physical_occupancy(self) -> None:
        self._insert_slot(201, "CASE-A", 1)
        self._insert_slot(202, "CASE-B", 2)
        asset_id = self._insert_asset(
            "AT-EDIT-1",
            serial_number="SER-EDIT-1",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=201,
        )
        self._occupy_slot(201, asset_id, "AT-EDIT-1")

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-1",
                "asset_tag": "AT-EDIT-1",
                "serial_number": "SER-EDIT-1A",
                "manufacturer": "Lenovo",
                "equipment_type": "switch",
                "building": "HQ",
                "room": "210",
                "model": "X1",
                "model_code": "GEN9",
                "notes": "relocated",
                "case_name": "CASE-B",
                "slot_id": "202",
            },
        )
        self.assertEqual(response.status_code, 302)

        asset_row = self.conn.execute(
            """
            SELECT serial_number, manufacturer, equipment_type, building_room, home_slot_id, case_number, slot_number
            FROM assets
            WHERE id = ?;
            """,
            (asset_id,),
        ).fetchone()
        self.assertEqual(asset_row["serial_number"], "SER-EDIT-1A")
        self.assertEqual(asset_row["manufacturer"], "Lenovo")
        self.assertEqual(asset_row["equipment_type"], "switch")
        self.assertEqual(asset_row["building_room"], "HQ/210")
        self.assertEqual(asset_row["home_slot_id"], 202)
        self.assertEqual(asset_row["case_number"], "CASE-B")
        self.assertEqual(asset_row["slot_number"], "2")

        occ = self.conn.execute("SELECT slot_id FROM slot_occupancy WHERE asset_id = ?;", (asset_id,)).fetchone()
        self.assertEqual(occ["slot_id"], 201)
        old_slot = self.conn.execute("SELECT current_asset_tag FROM slots WHERE id = 201;").fetchone()
        new_slot = self.conn.execute("SELECT current_asset_tag FROM slots WHERE id = 202;").fetchone()
        self.assertEqual(old_slot["current_asset_tag"], "AT-EDIT-1")
        self.assertIsNone(new_slot["current_asset_tag"])

        events = self.conn.execute(
            """
            SELECT event_type, payload
            FROM asset_events
            WHERE asset_tag = ?
            ORDER BY id ASC;
            """,
            ("AT-EDIT-1",),
        ).fetchall()
        self.assertTrue(any(row["event_type"] == "ASSET_UPDATED" for row in events))
        payloads = [json.loads(row["payload"]) for row in events if row["payload"]]
        self.assertTrue(any(payload.get("home_slot_id") == 202 for payload in payloads))

    def test_edit_asset_shows_home_and_current_physical_location_separately(self) -> None:
        self._insert_slot(701, "HOME-CASE", 1)
        self._insert_slot(702, "PHYSICAL-CASE", 2)
        asset_id = self._insert_asset(
            "AT-EDIT-ALT-RETURN",
            serial_number="SER-EDIT-ALT-RETURN",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=701,
            case_number="HOME-CASE",
            slot_number="1",
        )
        self._occupy_slot(702, asset_id, "AT-EDIT-ALT-RETURN")

        loaded = self.client.get("/admin/assets/edit?asset_tag=AT-EDIT-ALT-RETURN")

        self.assertEqual(loaded.status_code, 200)
        self.assertIn(b"Current Physical Location", loaded.data)
        self.assertIn(b'id="current_physical_location" aria-readonly="true"', loaded.data)
        self.assertIn(b"PHYSICAL-CASE / Slot 2", loaded.data)
        self.assertIn(b"Home Location", loaded.data)
        self.assertIn(b"HOME-CASE / Slot 1", loaded.data)
        self.assertIn(b'value="701"', loaded.data)

        updated = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-ALT-RETURN",
                "asset_tag": "AT-EDIT-ALT-RETURN",
                "serial_number": "SER-EDIT-ALT-RETURN",
                "manufacturer": "Lenovo",
                "equipment_type": "laptop",
                "building": "HQ",
                "room": "110",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "metadata only",
                "case_name": "HOME-CASE",
                "slot_id": "701",
            },
        )

        self.assertEqual(updated.status_code, 302)
        asset = self.conn.execute(
            "SELECT home_slot_id, case_number, slot_number FROM assets WHERE id = ?;", (asset_id,)
        ).fetchone()
        self.assertEqual(asset["home_slot_id"], 701)
        self.assertEqual(asset["case_number"], "HOME-CASE")
        self.assertEqual(asset["slot_number"], "1")
        occupancy = self.conn.execute("SELECT slot_id FROM slot_occupancy WHERE asset_id = ?;", (asset_id,)).fetchone()
        self.assertEqual(occupancy["slot_id"], 702)
        physical_slot = self.conn.execute("SELECT current_asset_tag FROM slots WHERE id = 702;").fetchone()
        home_slot = self.conn.execute("SELECT current_asset_tag FROM slots WHERE id = 701;").fetchone()
        self.assertEqual(physical_slot["current_asset_tag"], "AT-EDIT-ALT-RETURN")
        self.assertIsNone(home_slot["current_asset_tag"])
        events = self.conn.execute(
            "SELECT event_type FROM asset_events WHERE asset_tag = ? ORDER BY id;",
            ("AT-EDIT-ALT-RETURN",),
        ).fetchall()
        self.assertEqual([event["event_type"] for event in events], ["ASSET_UPDATED"])

    def test_edit_allows_blank_manufacturer(self) -> None:
        asset_id = self._insert_asset(
            "AT-EDIT-BLANK-MANUFACTURER",
            serial_number="SER-EDIT-BLANK-MANUFACTURER",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
            manufacturer="Dell",
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-BLANK-MANUFACTURER",
                "asset_tag": "AT-EDIT-BLANK-MANUFACTURER",
                "serial_number": "SER-EDIT-BLANK-MANUFACTURER-A",
                "manufacturer": "",
                "equipment_type": "laptop",
                "building": "HQ",
                "room": "110",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "clear manufacturer",
                "case_name": "",
                "slot_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)

        asset_row = self.conn.execute(
            "SELECT manufacturer, home_slot_id FROM assets WHERE id = ?;",
            (asset_id,),
        ).fetchone()
        self.assertEqual(asset_row["manufacturer"], "")
        self.assertIsNone(asset_row["home_slot_id"])

        event = self.conn.execute(
            """
            SELECT payload
            FROM asset_events
            WHERE asset_tag = ?
            ORDER BY id DESC
            LIMIT 1;
            """,
            ("AT-EDIT-BLANK-MANUFACTURER",),
        ).fetchone()
        payload = json.loads(event["payload"])
        self.assertEqual(payload["manufacturer"], "")
        self.assertNotIn("Unknown", event["payload"])
        self.assertNotIn("N/A", event["payload"])
        self.assertNotIn("None", event["payload"])
        self.assertNotIn("Not Provided", event["payload"])

    def test_edit_allows_blank_building_and_room(self) -> None:
        asset_id = self._insert_asset(
            "AT-EDIT-BLANK-LOCATION",
            serial_number="SER-EDIT-BLANK-LOCATION",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-BLANK-LOCATION",
                "asset_tag": "AT-EDIT-BLANK-LOCATION",
                "serial_number": "SER-EDIT-BLANK-LOCATION-A",
                "manufacturer": "Dell",
                "equipment_type": "laptop",
                "building": "",
                "room": "",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "clear location text",
                "case_name": "",
                "slot_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)

        asset_row = self.conn.execute(
            "SELECT building, room, building_room, home_slot_id FROM assets WHERE id = ?;",
            (asset_id,),
        ).fetchone()
        self.assertEqual(asset_row["building"], "")
        self.assertEqual(asset_row["room"], "")
        self.assertEqual(asset_row["building_room"], "")
        self.assertIsNone(asset_row["home_slot_id"])

        occupancy = self.conn.execute("SELECT 1 FROM slot_occupancy WHERE asset_id = ?;", (asset_id,)).fetchone()
        self.assertIsNone(occupancy)

        event = self.conn.execute(
            """
            SELECT event_type, payload
            FROM asset_events
            WHERE asset_tag = ?
            ORDER BY id DESC
            LIMIT 1;
            """,
            ("AT-EDIT-BLANK-LOCATION",),
        ).fetchone()
        self.assertEqual(event["event_type"], "ASSET_UPDATED")
        payload = json.loads(event["payload"])
        self.assertEqual(payload["building"], "")
        self.assertEqual(payload["room"], "")
        self.assertEqual(payload["building_room"], "")
        self.assertNotIn("Unknown", event["payload"])
        self.assertNotIn("N/A", event["payload"])
        self.assertNotIn("Unassigned", event["payload"])

    def test_edit_allows_building_without_room(self) -> None:
        asset_id = self._insert_asset(
            "AT-EDIT-BUILDING-ONLY",
            serial_number="SER-EDIT-BUILDING-ONLY",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
            building="",
            room="",
            building_room="",
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-BUILDING-ONLY",
                "asset_tag": "AT-EDIT-BUILDING-ONLY",
                "serial_number": "SER-EDIT-BUILDING-ONLY-A",
                "manufacturer": "Dell",
                "equipment_type": "laptop",
                "building": "HQ",
                "room": "",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "building only",
                "case_name": "",
                "slot_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)

        asset_row = self.conn.execute(
            "SELECT building, room, building_room FROM assets WHERE id = ?;",
            (asset_id,),
        ).fetchone()
        self.assertEqual(asset_row["building"], "HQ")
        self.assertEqual(asset_row["room"], "")
        self.assertEqual(asset_row["building_room"], "HQ")

    def test_edit_form_preserves_existing_legacy_equipment_type(self) -> None:
        asset_id = self._insert_asset(
            "AT-EDIT-LEGACY-1",
            serial_number="SER-EDIT-LEGACY-1",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )
        self.conn.execute("UPDATE assets SET equipment_type = 'tablet' WHERE id = ?;", (asset_id,))
        self.conn.commit()

        loaded = self.client.get("/admin/assets/edit?asset_tag=AT-EDIT-LEGACY-1")
        self.assertEqual(loaded.status_code, 200)
        self.assertIn(b'<option value="tablet" selected>', loaded.data)
        self.assertIn(b"tablet (existing value)", loaded.data)

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-LEGACY-1",
                "asset_tag": "AT-EDIT-LEGACY-1",
                "serial_number": "SER-EDIT-LEGACY-1A",
                "manufacturer": "Dell",
                "equipment_type": "tablet",
                "building": "HQ",
                "room": "112",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "legacy type preserved",
                "case_name": "",
                "slot_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)

        asset_row = self.conn.execute(
            "SELECT serial_number, equipment_type, building_room FROM assets WHERE id = ?;",
            (asset_id,),
        ).fetchone()
        self.assertEqual(asset_row["serial_number"], "SER-EDIT-LEGACY-1A")
        self.assertEqual(asset_row["equipment_type"], "tablet")
        self.assertEqual(asset_row["building_room"], "HQ/112")

    def test_edit_rejects_invalid_new_equipment_type(self) -> None:
        self._insert_asset(
            "AT-EDIT-INVALID-1",
            serial_number="SER-EDIT-INVALID-1",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-INVALID-1",
                "asset_tag": "AT-EDIT-INVALID-1",
                "serial_number": "SER-EDIT-INVALID-1",
                "manufacturer": "Dell",
                "equipment_type": "tablet",
                "building": "HQ",
                "room": "110",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "invalid new type",
                "case_name": "",
                "slot_id": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Supported asset types are Laptop, Switch, Router, Server, Storage, Firewall, NTP, and KVM.", response.data)

    def test_edit_in_custody_asset_updates_home_slot_without_occupancy(self) -> None:
        self._insert_slot(301, "CASE-C", 3)
        self._insert_slot(302, "CASE-D", 4)
        asset_id = self._insert_asset(
            "AT-EDIT-2",
            serial_number="SER-EDIT-2",
            location_type="IN_CUSTODY",
            current_holder_id=77,
            home_slot_id=301,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-2",
                "asset_tag": "AT-EDIT-2",
                "serial_number": "SER-EDIT-2",
                "manufacturer": "Dell",
                "equipment_type": "laptop",
                "building": "HQ",
                "room": "111",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "new return slot",
                "case_name": "CASE-D",
                "slot_id": "302",
            },
        )
        self.assertEqual(response.status_code, 302)

        asset_row = self.conn.execute(
            "SELECT location_type, current_holder_id, home_slot_id, case_number, slot_number FROM assets WHERE id = ?;",
            (asset_id,),
        ).fetchone()
        self.assertEqual(asset_row["location_type"], "IN_CUSTODY")
        self.assertEqual(asset_row["current_holder_id"], 77)
        self.assertEqual(asset_row["home_slot_id"], 302)
        self.assertEqual(asset_row["case_number"], "CASE-D")
        self.assertEqual(asset_row["slot_number"], "4")

        occ = self.conn.execute("SELECT 1 FROM slot_occupancy WHERE asset_id = ?;", (asset_id,)).fetchone()
        self.assertIsNone(occ)

    def test_edit_rejects_occupied_target_slot(self) -> None:
        self._insert_slot(401, "CASE-E", 1)
        self._insert_slot(402, "CASE-E", 2, current_asset_tag="AT-OCCUPIER")
        asset_id = self._insert_asset(
            "AT-EDIT-3",
            serial_number="SER-EDIT-3",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=401,
        )
        self._occupy_slot(401, asset_id, "AT-EDIT-3")
        occupier_id = self._insert_asset(
            "AT-OCCUPIER",
            serial_number="SER-OCC",
            location_type="STORAGE",
            current_holder_id=None,
            home_slot_id=402,
        )
        self._occupy_slot(402, occupier_id, "AT-OCCUPIER")

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "update",
                "lookup_asset_tag": "AT-EDIT-3",
                "asset_tag": "AT-EDIT-3",
                "serial_number": "SER-EDIT-3",
                "manufacturer": "Dell",
                "equipment_type": "laptop",
                "building": "HQ",
                "room": "110",
                "model": "Latitude",
                "model_code": "5400",
                "notes": "attempted move",
                "case_name": "CASE-E",
                "slot_id": "402",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already occupied", response.data)

        asset_row = self.conn.execute("SELECT home_slot_id FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        self.assertEqual(asset_row["home_slot_id"], 401)
        occ = self.conn.execute("SELECT slot_id FROM slot_occupancy WHERE asset_id = ?;", (asset_id,)).fetchone()
        self.assertEqual(occ["slot_id"], 401)

    def test_cleanup_removes_safe_junk_asset_with_no_history(self) -> None:
        asset_id = self._insert_asset(
            "AT-JUNK-1",
            serial_number="SER-JUNK-1",
            location_type="",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "cleanup",
                "lookup_asset_tag": "AT-JUNK-1",
                "asset_tag": "AT-JUNK-1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Removed junk asset AT-JUNK-1.", response.data)

        deleted = self.conn.execute("SELECT 1 FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        self.assertIsNone(deleted)

    def test_edit_asset_ui_directs_admins_to_retire_flow_instead_of_delete(self) -> None:
        self._insert_asset(
            "AT-JUNK-UI-1",
            serial_number="SER-JUNK-UI-1",
            location_type="",
            current_holder_id=None,
            home_slot_id=None,
        )

        response = self.client.get("/admin/assets/edit?asset_tag=AT-JUNK-UI-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Removal", response.data)
        self.assertIn(b"Asset records are not removed from the admin UI.", response.data)
        self.assertIn(b"Use the retire flow for asset removal handling.", response.data)
        self.assertIn(b"Open Retire Flow", response.data)
        self.assertIn(b'href="/admin/assets/retire"', response.data)
        self.assertNotIn(b"Delete Orphan / Junk Asset", response.data)

    def test_cleanup_blocks_asset_with_event_history(self) -> None:
        asset_id = self._insert_asset(
            "AT-JUNK-2",
            serial_number="SER-JUNK-2",
            location_type="",
            current_holder_id=None,
            home_slot_id=None,
        )
        self._insert_event("AT-JUNK-2")

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "cleanup",
                "lookup_asset_tag": "AT-JUNK-2",
                "asset_tag": "AT-JUNK-2",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset has event history and cannot be removed.", response.data)
        self.assertIn(b"This asset must use the retire flow rather than removal from edit.", response.data)
        self.assertIn(b"Open Retire Flow", response.data)
        self.assertIn(b'href="/admin/assets/retire"', response.data)
        still_present = self.conn.execute("SELECT 1 FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        self.assertIsNotNone(still_present)

    def test_cleanup_blocks_asset_with_home_slot_assignment(self) -> None:
        self._insert_slot(501, "CASE-F", 1)
        asset_id = self._insert_asset(
            "AT-JUNK-3",
            serial_number="SER-JUNK-3",
            location_type="",
            current_holder_id=None,
            home_slot_id=501,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "cleanup",
                "lookup_asset_tag": "AT-JUNK-3",
                "asset_tag": "AT-JUNK-3",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset has a home slot assignment and cannot be removed.", response.data)
        still_present = self.conn.execute("SELECT 1 FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        self.assertIsNotNone(still_present)

    def test_cleanup_blocks_asset_with_case_and_slot_fields(self) -> None:
        asset_id = self._insert_asset(
            "AT-JUNK-4",
            serial_number="SER-JUNK-4",
            location_type="",
            current_holder_id=None,
            home_slot_id=None,
            case_number="CASE-G",
            slot_number="7",
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "cleanup",
                "lookup_asset_tag": "AT-JUNK-4",
                "asset_tag": "AT-JUNK-4",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset still has a case assignment and cannot be removed.", response.data)
        self.assertIn(b"Asset still has a slot assignment field and cannot be removed.", response.data)
        still_present = self.conn.execute("SELECT 1 FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        self.assertIsNotNone(still_present)

    def test_cleanup_blocks_asset_in_active_custody_state(self) -> None:
        asset_id = self._insert_asset(
            "AT-JUNK-5",
            serial_number="SER-JUNK-5",
            location_type="IN_CUSTODY",
            current_holder_id=77,
            home_slot_id=None,
        )

        response = self.client.post(
            "/admin/assets/edit",
            data={
                "action": "cleanup",
                "lookup_asset_tag": "AT-JUNK-5",
                "asset_tag": "AT-JUNK-5",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset is assigned to a holder and cannot be removed.", response.data)
        self.assertIn(b"Asset is in active inventory state IN_CUSTODY and cannot be removed.", response.data)
        still_present = self.conn.execute("SELECT 1 FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        self.assertIsNotNone(still_present)


if __name__ == "__main__":
    unittest.main()
