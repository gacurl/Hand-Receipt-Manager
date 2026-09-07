from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import assettrack.db as db
from assettrack.intake import app as intake_app
from tests.auth_test_utils import create_test_user, login_session


class AssetSearchUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "assettrack.db"
        self.conn = db.get_connection()
        intake_app.app.testing = True
        self.client = intake_app.app.test_client()
        operator_user_id = create_test_user(username="operator", password="operator-pass", role="operator")
        login_session(self.client, operator_user_id)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _insert_holder(self, holder_id: int, name: str, organization: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO holders (id, holder_type, name, organization, identifier, contact_info, created_at, updated_at)
            VALUES (?, 'PERSON', ?, ?, NULL, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """,
            (holder_id, name, organization),
        )
        self.conn.commit()

    def _insert_event(
        self,
        asset_tag: str,
        *,
        event_type: str,
        event_date: str,
        payload: dict[str, object] | None = None,
        holder_id: int | None = None,
        notes: str | None = None,
        supersedes_event_id: int | None = None,
        correction_reason: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO asset_events (
                asset_tag,
                event_type,
                event_date,
                actor,
                notes,
                payload,
                holder_id,
                supersedes_event_id,
                correction_reason
            )
            VALUES (?, ?, ?, 'system', ?, ?, ?, ?, ?);
            """,
            (
                asset_tag,
                event_type,
                event_date,
                notes,
                json.dumps(payload or {}),
                holder_id,
                supersedes_event_id,
                correction_reason,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def _insert_receipt(
        self,
        receipt_id: int,
        receipt_key: str,
        source_event_ids: list[int],
        *,
        receipt_type: str = "ISSUE",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO receipt_queue (
                id,
                receipt_key,
                receipt_type,
                source_event_ids_json,
                snapshot_json,
                commit_at,
                commit_operator_user_id,
                holder_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, '{}', '2026-04-03T09:20:00+00:00', 1, NULL, '2026-04-03T09:20:00+00:00', '2026-04-03T09:20:00+00:00');
            """,
            (receipt_id, receipt_key, receipt_type, json.dumps(source_event_ids)),
        )
        self.conn.commit()

    def _insert_slot(self, slot_id: int, case_name: str, slot_position: int) -> None:
        self.conn.execute(
            """
            INSERT INTO slots (id, case_name, slot_position, current_asset_tag)
            VALUES (?, ?, ?, NULL);
            """,
            (slot_id, case_name, slot_position),
        )
        self.conn.commit()

    def _insert_asset(
        self,
        asset_tag: str,
        *,
        serial_number: str,
        location_type: str,
        home_slot_id: int | None,
        current_holder_id: int | None = None,
        equipment_type: str = "laptop",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO assets (
                asset_tag,
                serial_number,
                manufacturer,
                equipment_type,
                building,
                room,
                building_room,
                custody_state,
                accountability_status,
                condition,
                created_date,
                updated_date,
                location_type,
                current_holder_id,
                home_slot_id
            )
            VALUES (?, ?, 'Dell', ?, 'HQ', '100', 'HQ/100', 'in_stock', 'accountable', 'serviceable', '2026-01-01', '2026-01-01T00:00:00Z', ?, ?, ?);
            """,
            (asset_tag, serial_number, equipment_type, location_type, current_holder_id, home_slot_id),
        )
        self.conn.commit()

    def test_search_page_allows_authenticated_operator(self) -> None:
        response = self.client.get("/assets/search")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Search", response.data)
        self.assertIn(b"Search assets", response.data)
        self.assertIn(b"Scan or type asset tag", response.data)
        self.assertIn(b"Optional serial lookup", response.data)
        self.assertIn(b"Ready to search", response.data)
        self.assertIn(b"custody status, holder, assigned location, and existing movement proof", response.data)
        self.assertNotIn(b">Clear<", response.data)

    def test_search_finds_asset_by_asset_tag(self) -> None:
        self._insert_holder(1, "Alex Holder", "Field Ops")
        self._insert_slot(10, "CASE-A", 4)
        self._insert_asset("AT-100", serial_number="SER-100", location_type="STORAGE", home_slot_id=10, current_holder_id=1)

        response = self.client.get("/assets/search?asset_tag=AT-100")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertNotIn(b"Matched by asset tag.", response.data)
        self.assertIn(b"AT-100", response.data)
        self.assertIn(b"SER-100", response.data)
        self.assertIn(b"Laptop", response.data)
        self.assertIn(b"Issued to Alex Holder (Field Ops)", response.data)
        self.assertIn(b"Alex Holder (Field Ops)", response.data)
        self.assertIn(b"<th>Home location</th>", response.data)
        self.assertNotIn(b"<th>Assigned location</th>", response.data)
        self.assertIn(b"Home slot: CASE-A, Slot 4", response.data)
        self.assertIn(b"No movement proof recorded", response.data)
        self.assertIn(b'href="/assets/history?asset_tag=AT-100', response.data)
        self.assertNotIn(b'href="/admin/assets/edit?asset_tag=AT-100"', response.data)

    def test_search_finds_hyphenated_asset_when_query_omits_hyphen(self) -> None:
        self._insert_asset("ABC-123", serial_number="SER-HYPHEN", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=ABC123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertIn(b"ABC-123", response.data)
        self.assertIn(b"SER-HYPHEN", response.data)

    def test_search_finds_spaced_hyphenated_asset_when_query_omits_spacing(self) -> None:
        self._insert_asset("BQ 26-1", serial_number="SER-BQ26-SEARCH", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=bq261")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertIn(b"BQ 26-1", response.data)
        self.assertIn(b"SER-BQ26-SEARCH", response.data)

    def test_search_finds_unhyphenated_asset_when_query_includes_hyphen_without_rewriting_tag(self) -> None:
        self._insert_asset("ABC123", serial_number="SER-NOHYPHEN", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=ABC-123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertIn(b"ABC123", response.data)
        self.assertIn(b"SER-NOHYPHEN", response.data)
        row = self.conn.execute(
            "SELECT asset_tag FROM assets WHERE serial_number = 'SER-NOHYPHEN';"
        ).fetchone()
        self.assertEqual(row["asset_tag"], "ABC123")

    def test_search_finds_case_when_query_format_differs(self) -> None:
        self._insert_slot(20, "CASE-12", 1)

        response = self.client.get("/assets/search?asset_tag=case12")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Case Found", response.data)
        self.assertIn(b"1 case match shown.", response.data)
        self.assertIn(b"<code>CASE-12</code>", response.data)
        self.assertNotIn(b"No asset found", response.data)

    def test_search_case_prefix_returns_matching_cases(self) -> None:
        self._insert_slot(21, "CASE-12", 1)
        self._insert_slot(22, "CASE-13", 1)
        self._insert_slot(23, "KIT-1", 1)

        response = self.client.get("/assets/search?asset_tag=CASE-")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cases Found", response.data)
        self.assertIn(b"2 case matches shown.", response.data)
        self.assertIn(b"<code>CASE-12</code>", response.data)
        self.assertIn(b"<code>CASE-13</code>", response.data)
        self.assertNotIn(b"<code>KIT-1</code>", response.data)

    def test_search_case_prefix_renders_case_results_in_natural_numeric_order(self) -> None:
        for slot_id, case_name in [
            (120, "CASE-1"),
            (121, "CASE-10"),
            (122, "CASE-2"),
            (123, "CASE-20"),
            (124, "CASE-9"),
        ]:
            self._insert_slot(slot_id, case_name, 1)

        response = self.client.get("/assets/search?asset_tag=CASE-")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cases Found", response.data)
        positions = [
            response.data.index(b"<code>CASE-1</code>"),
            response.data.index(b"<code>CASE-2</code>"),
            response.data.index(b"<code>CASE-9</code>"),
            response.data.index(b"<code>CASE-10</code>"),
            response.data.index(b"<code>CASE-20</code>"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_search_lowercase_case_prefix_returns_matching_cases(self) -> None:
        self._insert_slot(24, "CASE-21", 1)
        self._insert_slot(25, "CASE22", 1)

        response = self.client.get("/assets/search?asset_tag=case")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cases Found", response.data)
        self.assertIn(b"<code>CASE-21</code>", response.data)
        self.assertIn(b"<code>CASE22</code>", response.data)

    def test_search_case_results_keep_stored_case_formatting(self) -> None:
        self._insert_slot(26, "Case-Mixed-12", 1)

        response = self.client.get("/assets/search?asset_tag=case-mixed-12")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<code>Case-Mixed-12</code>", response.data)
        self.assertNotIn(b"<code>CASE-MIXED-12</code>", response.data)
        row = self.conn.execute("SELECT case_name FROM slots WHERE id = 26;").fetchone()
        self.assertEqual(row["case_name"], "Case-Mixed-12")

    def test_search_exact_case_match_is_listed_before_prefix_matches(self) -> None:
        self._insert_slot(27, "CASE-12", 1)
        self._insert_slot(28, "CASE-123", 1)

        response = self.client.get("/assets/search?asset_tag=CASE12")

        self.assertEqual(response.status_code, 200)
        self.assertLess(
            response.data.index(b"<code>CASE-12</code>"),
            response.data.index(b"<code>CASE-123</code>"),
        )

    def test_admin_search_links_asset_tag_to_admin_edit_asset(self) -> None:
        admin_user_id = create_test_user(username="admin-search", password="admin-pass", role="admin")
        login_session(self.client, admin_user_id)
        self._insert_asset("AT-ADMIN-1", serial_number="SER-ADMIN-1", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-ADMIN-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b' href="/admin/assets/edit?asset_tag=AT-ADMIN-1"', response.data)
        self.assertIn(b"AT-ADMIN-1", response.data)

    def test_search_finds_asset_by_serial_number(self) -> None:
        self._insert_asset("AT-200", serial_number="SER-200", location_type="IN_CUSTODY", home_slot_id=None)

        response = self.client.get("/assets/search?serial_number=SER-200")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertNotIn(b"Matched by serial number.", response.data)
        self.assertIn(b"AT-200", response.data)
        self.assertIn(b"SER-200", response.data)
        self.assertIn(b"Unslotted", response.data)
        self.assertIn(b"Assigned holder", response.data)
        self.assertIn(b"Not assigned", response.data)
        self.assertIn(b"Not assigned", response.data)

    def test_search_keeps_supported_asset_types_readable(self) -> None:
        self._insert_asset("TYPE-LAPTOP", serial_number="SER-LAPTOP", location_type="STORAGE", home_slot_id=None)
        self._insert_asset(
            "TYPE-SWITCH",
            serial_number="SER-SWITCH",
            location_type="STORAGE",
            home_slot_id=None,
            equipment_type="switch",
        )
        self._insert_asset(
            "TYPE-ROUTER",
            serial_number="SER-ROUTER",
            location_type="STORAGE",
            home_slot_id=None,
            equipment_type="router",
        )

        response = self.client.get("/assets/search?asset_tag=TYPE-")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Laptop", response.data)
        self.assertIn(b"Switch", response.data)
        self.assertIn(b"Router", response.data)

    def test_search_keeps_legacy_asset_type_readable(self) -> None:
        self._insert_asset(
            "TYPE-LEGACY",
            serial_number="SER-LEGACY",
            location_type="STORAGE",
            home_slot_id=None,
            equipment_type="tablet",
        )

        response = self.client.get("/assets/search?asset_tag=TYPE-LEGACY")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"TYPE-LEGACY", response.data)
        self.assertIn(b"Tablet", response.data)

    def test_search_shows_stored_status_cue_for_storage_asset(self) -> None:
        self._insert_asset("AT-STORED-1", serial_number="SER-STORED-1", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-STORED-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Unslotted", response.data)
        self.assertNotIn(b"Stored / returned", response.data)

    def test_search_shows_out_with_holder_status_cue_for_issued_asset(self) -> None:
        self._insert_holder(2, "Jamie Holder", "Field Ops")
        self._insert_asset(
            "AT-OUT-1",
            serial_number="SER-OUT-1",
            location_type="IN_CUSTODY",
            home_slot_id=None,
            current_holder_id=2,
        )

        response = self.client.get("/assets/search?asset_tag=AT-OUT-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Issued to Jamie Holder (Field Ops)", response.data)
        self.assertNotIn(b"Out with holder", response.data)

    def test_search_shows_stored_wording_for_asset_in_case_slot(self) -> None:
        self._insert_slot(41, "CASE-12", 4)
        self._insert_asset("AT-STORED-WORD", serial_number="SER-STORED-WORD", location_type="STORAGE", home_slot_id=41)

        response = self.client.get("/assets/search?asset_tag=AT-STORED-WORD")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Stored in CASE-12, Slot 4", response.data)
        self.assertNotIn(b"unassigned slot", response.data.lower())

    def test_search_uses_current_occupancy_for_status_and_home_for_assigned_location(self) -> None:
        self._insert_slot(44, "CASE-HOME", 7)
        self._insert_slot(45, "CASE-TEMP", 3)
        self._insert_asset("AT-TEMP-RETURN", serial_number="SER-TEMP-RETURN", location_type="STORAGE", home_slot_id=44)
        asset_id = int(
            self.conn.execute("SELECT id FROM assets WHERE asset_tag = ?;", ("AT-TEMP-RETURN",)).fetchone()["id"]
        )
        self.conn.execute(
            "INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at) VALUES (?, ?, ?);",
            (45, asset_id, "2026-01-01T00:00:00Z"),
        )
        self.conn.execute("UPDATE slots SET current_asset_tag = ? WHERE id = ?;", ("AT-TEMP-RETURN", 45))
        self.conn.commit()

        response = self.client.get("/assets/search?asset_tag=AT-TEMP-RETURN")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Stored in CASE-TEMP, Slot 3", response.data)
        self.assertIn(b"Stored in CASE-HOME, Slot 7", response.data)

    def test_search_shows_unslotted_wording_for_asset_without_slot(self) -> None:
        self._insert_asset("AT-UNSLOTTED-WORD", serial_number="SER-UNSLOTTED-WORD", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-UNSLOTTED-WORD")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Unslotted", response.data)
        self.assertNotIn(b"unassigned slot", response.data.lower())

    def test_search_shows_issued_wording_with_holder(self) -> None:
        self._insert_holder(42, "Jamie Holder", "Field Ops")
        self._insert_asset(
            "AT-ISSUED-WORD",
            serial_number="SER-ISSUED-WORD",
            location_type="IN_CUSTODY",
            home_slot_id=None,
            current_holder_id=42,
        )

        response = self.client.get("/assets/search?asset_tag=AT-ISSUED-WORD")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Issued to Jamie Holder (Field Ops)", response.data)

    def test_search_issued_asset_with_home_slot_remains_issued(self) -> None:
        self._insert_holder(43, "Case Holder", "Ops")
        self._insert_slot(43, "CASE-12", 4)
        self._insert_asset(
            "AT-ISSUED-HOME",
            serial_number="SER-ISSUED-HOME",
            location_type="IN_CUSTODY",
            home_slot_id=43,
            current_holder_id=43,
        )

        response = self.client.get("/assets/search?asset_tag=AT-ISSUED-HOME")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Issued to Case Holder (Ops)", response.data)
        self.assertIn(b"Home slot: CASE-12, Slot 4", response.data)
        self.assertNotIn(b"Stored in CASE-12, Slot 4", response.data)

    def test_search_includes_cases_link_to_existing_case_list(self) -> None:
        response = self.client.get("/assets/search")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'href="/dashboard/cases"', response.data)
        self.assertIn(b">Cases</a>", response.data)

    def test_search_marks_retired_assets_with_clear_terminal_label(self) -> None:
        self._insert_asset("AT-RET-1", serial_number="SER-RET-1", location_type="DISPOSED", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-RET-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"RETIRED \xe2\x80\x94 Not in service", response.data)
        self.assertIn(b"Retired or not in service.", response.data)
        self.assertNotIn(b"Retired / disposed", response.data)

    def test_search_shows_unknown_status_when_state_and_proof_are_missing(self) -> None:
        self._insert_asset("AT-UNKNOWN-1", serial_number="SER-UNKNOWN-1", location_type="", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-UNKNOWN-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Unslotted", response.data)
        self.assertIn(b"No movement proof recorded", response.data)

    def test_search_shows_latest_movement_event_proof(self) -> None:
        self._insert_asset("AT-PROOF-1", serial_number="SER-PROOF-1", location_type="IN_CUSTODY", home_slot_id=None)
        old_event_id = self._insert_event(
            "AT-PROOF-1",
            event_type="ISSUE",
            event_date="2026-04-01T08:00:00+00:00",
        )
        latest_event_id = self._insert_event(
            "AT-PROOF-1",
            event_type="RETURN",
            event_date="2026-04-03T09:18:00+00:00",
        )

        response = self.client.get("/assets/search?asset_tag=AT-PROOF-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Last movement proof", response.data)
        self.assertIn(b"<strong>RETURN</strong>", response.data)
        self.assertIn(b"Apr 3, 2026 9:18 AM", response.data)
        self.assertIn(f"Event #{latest_event_id}".encode("utf-8"), response.data)
        self.assertNotIn(f"Event #{old_event_id}".encode("utf-8"), response.data)
        self.assertIn(b"No receipt linked", response.data)

    def test_search_shows_slot_move_event_as_movement_proof_without_state_change(self) -> None:
        self._insert_slot(30, "1", 4)
        self._insert_asset("55555", serial_number="SER-55555", location_type="STORAGE", home_slot_id=30)
        asset_row = self.conn.execute("SELECT id FROM assets WHERE asset_tag = '55555';").fetchone()
        self.conn.execute(
            """
            INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at)
            VALUES (30, ?, '2026-04-04T10:00:00+00:00');
            """,
            (int(asset_row["id"]),),
        )
        self.conn.commit()
        before_state = self.conn.execute(
            """
            SELECT location_type, current_holder_id, home_slot_id
            FROM assets
            WHERE asset_tag = '55555';
            """
        ).fetchone()
        before_occupancy = self.conn.execute(
            "SELECT slot_id FROM slot_occupancy WHERE asset_id = ? ORDER BY slot_id;",
            (int(asset_row["id"]),),
        ).fetchall()
        event_id = self._insert_event(
            "55555",
            event_type="SLOT_MOVE",
            event_date="2026-04-04T10:15:00+00:00",
            payload={
                "from_slot": {"case_number": "1", "slot_number": 4},
                "to_slot": {"case_number": "CASE-1", "slot_number": 3},
            },
        )

        response = self.client.get("/assets/search?asset_tag=55555")

        after_state = self.conn.execute(
            """
            SELECT location_type, current_holder_id, home_slot_id
            FROM assets
            WHERE asset_tag = '55555';
            """
        ).fetchone()
        after_occupancy = self.conn.execute(
            "SELECT slot_id FROM slot_occupancy WHERE asset_id = ? ORDER BY slot_id;",
            (int(asset_row["id"]),),
        ).fetchall()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<strong>SLOT_MOVE</strong>", response.data)
        self.assertIn(b"Apr 4, 2026 10:15 AM", response.data)
        self.assertIn(f"Event #{event_id}".encode("utf-8"), response.data)
        self.assertIn(b"From case <code>1</code>,", response.data)
        self.assertIn(b"slot <code>4</code>", response.data)
        self.assertIn(b"to case <code>CASE-1</code>,", response.data)
        self.assertIn(b"slot <code>3</code>", response.data)
        self.assertIn(b"No receipt linked", response.data)
        self.assertEqual(dict(before_state), dict(after_state))
        self.assertEqual([int(row["slot_id"]) for row in before_occupancy], [int(row["slot_id"]) for row in after_occupancy])

    def test_search_shows_receipt_link_for_movement_proof(self) -> None:
        self._insert_asset("AT-RECEIPT-1", serial_number="SER-RECEIPT-1", location_type="IN_CUSTODY", home_slot_id=None)
        event_id = self._insert_event(
            "AT-RECEIPT-1",
            event_type="ISSUE",
            event_date="2026-04-03T09:18:00+00:00",
        )
        self._insert_receipt(42, "ISSUE:42", [event_id])

        response = self.client.get("/assets/search?asset_tag=AT-RECEIPT-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<strong>ISSUE</strong>", response.data)
        self.assertIn(f"Event #{event_id}".encode("utf-8"), response.data)
        self.assertIn(b'href="/receipts/42?return_to=/assets/search?', response.data)
        self.assertIn(b"asset_tag%3DAT-RECEIPT-1", response.data)
        self.assertIn(b"Receipt ISSUE:42", response.data)

    def test_search_receipt_link_preserves_report_return_context(self) -> None:
        self._insert_asset(
            "AT-REPORT-RECEIPT",
            serial_number="SER-REPORT-RECEIPT",
            location_type="IN_CUSTODY",
            home_slot_id=None,
        )
        event_id = self._insert_event(
            "AT-REPORT-RECEIPT",
            event_type="ISSUE",
            event_date="2026-04-03T09:18:00+00:00",
        )
        self._insert_receipt(43, "ISSUE:43", [event_id])

        response = self.client.get("/assets/search?asset_tag=AT-REPORT-RECEIPT&return_to=/report?include_retired=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'href="/receipts/43?return_to=/assets/search?', response.data)
        self.assertIn(b"asset_tag%3DAT-REPORT-RECEIPT", response.data)
        self.assertIn(b"return_to%3D/report", response.data)
        self.assertIn(b"include_retired", response.data)

    def test_search_ignores_superseded_movement_events(self) -> None:
        self._insert_asset("AT-CORRECTED-1", serial_number="SER-CORRECTED-1", location_type="IN_CUSTODY", home_slot_id=None)
        active_event_id = self._insert_event(
            "AT-CORRECTED-1",
            event_type="ISSUE",
            event_date="2026-04-01T08:00:00+00:00",
        )
        superseded_event_id = self._insert_event(
            "AT-CORRECTED-1",
            event_type="RETURN",
            event_date="2026-04-05T08:00:00+00:00",
        )
        correction_event_id = self._insert_event(
            "AT-CORRECTED-1",
            event_type="ASSET_UPDATED",
            event_date="2026-04-06T08:00:00+00:00",
            supersedes_event_id=superseded_event_id,
            correction_reason="Incorrect return event.",
        )

        response = self.client.get("/assets/search?asset_tag=AT-CORRECTED-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Event #{active_event_id}".encode("utf-8"), response.data)
        self.assertNotIn(f"Event #{superseded_event_id}".encode("utf-8"), response.data)
        self.assertNotIn(f"Event #{correction_event_id}".encode("utf-8"), response.data)

    def test_asset_history_requires_login(self) -> None:
        anonymous_client = intake_app.app.test_client()

        response = anonymous_client.get("/assets/history?asset_tag=AT-100")

        self.assertEqual(response.status_code, 403)

    def test_asset_history_shows_complete_events_receipts_and_corrections(self) -> None:
        self._insert_holder(3, "Casey Holder", "Signal")
        self._insert_slot(41, "CASE-HIST", 1)
        self._insert_asset(
            "HIST-LAPTOP",
            serial_number="SER-HIST-LAPTOP",
            location_type="STORAGE",
            home_slot_id=41,
            current_holder_id=None,
            equipment_type="laptop",
        )
        asset_row = self.conn.execute("SELECT id FROM assets WHERE asset_tag = 'HIST-LAPTOP';").fetchone()
        self.conn.execute(
            "INSERT INTO slot_occupancy (slot_id, asset_id, assigned_at) VALUES (41, ?, '2026-04-03T09:00:00+00:00');",
            (int(asset_row["id"]),),
        )
        self.conn.commit()
        created_event_id = self._insert_event(
            "HIST-LAPTOP",
            event_type="ASSET_CREATED",
            event_date="2026-04-01T08:00:00+00:00",
            payload={"equipment_type": "laptop"},
            notes="Initial import",
        )
        issue_event_id = self._insert_event(
            "HIST-LAPTOP",
            event_type="ISSUE",
            event_date="2026-04-02T09:00:00+00:00",
            holder_id=3,
        )
        return_event_id = self._insert_event(
            "HIST-LAPTOP",
            event_type="RETURN",
            event_date="2026-04-03T09:00:00+00:00",
            holder_id=3,
        )
        superseded_event_id = self._insert_event(
            "HIST-LAPTOP",
            event_type="RETURN",
            event_date="2026-04-04T09:00:00+00:00",
        )
        correction_event_id = self._insert_event(
            "HIST-LAPTOP",
            event_type="ASSET_UPDATED",
            event_date="2026-04-05T09:00:00+00:00",
            supersedes_event_id=superseded_event_id,
            correction_reason="Incorrect duplicate return.",
        )
        self._insert_receipt(51, "ISSUE:51", [issue_event_id])
        self._insert_receipt(52, "RETURN:52", [return_event_id], receipt_type="RETURN")
        before_state = self.conn.execute(
            "SELECT location_type, current_holder_id, home_slot_id FROM assets WHERE asset_tag = 'HIST-LAPTOP';"
        ).fetchone()

        response = self.client.get("/assets/history?asset_tag=HIST-LAPTOP")
        after_state = self.conn.execute(
            "SELECT location_type, current_holder_id, home_slot_id FROM assets WHERE asset_tag = 'HIST-LAPTOP';"
        ).fetchone()

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset History: HIST-LAPTOP", response.data)
        self.assertIn(b"Type:</strong> Laptop", response.data)
        self.assertIn(b"Status:</strong>", response.data)
        self.assertIn(b"In storage", response.data)
        self.assertIn(b"Holder:</strong>", response.data)
        self.assertIn(b"Not assigned", response.data)
        self.assertIn(b"Current slot:</strong>", response.data)
        self.assertIn(b"CASE-HIST / 1", response.data)
        self.assertIn(b"Append-Only Event History (5)", response.data)
        for event_id in [created_event_id, issue_event_id, return_event_id, superseded_event_id, correction_event_id]:
            self.assertIn(f"#{event_id}".encode("utf-8"), response.data)
        self.assertLess(response.data.index(f"#{created_event_id}".encode("utf-8")), response.data.index(f"#{issue_event_id}".encode("utf-8")))
        self.assertLess(response.data.index(f"#{issue_event_id}".encode("utf-8")), response.data.index(f"#{return_event_id}".encode("utf-8")))
        self.assertIn(b"Initial import", response.data)
        self.assertIn(b"Casey Holder (Signal)", response.data)
        self.assertIn(b'href="/receipts/51?return_to=/assets/history?', response.data)
        self.assertIn(b"ISSUE:51", response.data)
        self.assertIn(b"RETURN:52", response.data)
        self.assertIn(b"Correction:</strong> supersedes event", response.data)
        self.assertIn(b"Superseded by:</strong> event", response.data)
        self.assertIn(b"Incorrect duplicate return.", response.data)
        self.assertIn(b"<summary>Recorded facts</summary>", response.data)
        self.assertNotIn(b"<form", response.data)
        self.assertEqual(dict(before_state), dict(after_state))

    def test_asset_history_supports_all_approved_types_without_holder(self) -> None:
        rows = [
            ("laptop", "HIST-LAPTOP-TYPE", "Laptop"),
            ("switch", "HIST-SWITCH", "Switch"),
            ("router", "HIST-ROUTER", "Router"),
            ("server", "HIST-SERVER", "Server"),
            ("storage", "HIST-STORAGE", "Storage"),
            ("firewall", "HIST-FIREWALL", "Firewall"),
            ("ntp", "HIST-NTP", "NTP"),
            ("kvm", "HIST-KVM", "KVM"),
        ]
        for equipment_type, asset_tag, expected_label in rows:
            with self.subTest(equipment_type=equipment_type):
                self._insert_asset(
                    asset_tag,
                    serial_number=f"SER-{asset_tag}",
                    location_type="STORAGE",
                    home_slot_id=None,
                    equipment_type=equipment_type,
                )
                event_id = self._insert_event(
                    asset_tag,
                    event_type="SLOT_MOVE",
                    event_date="2026-04-04T10:15:00+00:00",
                    payload={
                        "from_slot": {"case_number": "CASE-OLD", "slot_number": 1},
                        "to_slot": {"case_number": "CASE-NEW", "slot_number": 2},
                    },
                )

                response = self.client.get(f"/assets/history?asset_tag={asset_tag}")

                self.assertEqual(response.status_code, 200)
                self.assertIn(asset_tag.encode("utf-8"), response.data)
                self.assertIn(expected_label.encode("utf-8"), response.data)
                self.assertIn(b"Holder:</strong>", response.data)
                self.assertIn(b"Not assigned", response.data)
                self.assertIn(b"SLOT_MOVE", response.data)
                self.assertIn(f"#{event_id}".encode("utf-8"), response.data)
                self.assertIn(b"CASE-OLD", response.data)
                self.assertIn(b"CASE-NEW", response.data)

    def test_partial_asset_tag_search_returns_matching_assets(self) -> None:
        self._insert_holder(1, "Alex Holder", "Field Ops")
        self._insert_asset("AT-100", serial_number="SER-100", location_type="IN_CUSTODY", home_slot_id=None, current_holder_id=1)
        self._insert_asset("AT-101", serial_number="SER-101", location_type="STORAGE", home_slot_id=None)
        self._insert_asset("BX-200", serial_number="SER-200", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-10")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assets Found", response.data)
        self.assertIn(b"2 matches shown.", response.data)
        self.assertIn(b"AT-100", response.data)
        self.assertIn(b"AT-101", response.data)
        self.assertNotIn(b"BX-200", response.data)
        self.assertIn(b"Alex Holder (Field Ops)", response.data)

    def test_partial_serial_search_returns_matching_assets(self) -> None:
        self._insert_asset("AT-200", serial_number="SER-200", location_type="IN_CUSTODY", home_slot_id=None)
        self._insert_asset("AT-201", serial_number="SER-201", location_type="STORAGE", home_slot_id=None)
        self._insert_asset("AT-999", serial_number="XYZ-999", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?serial_number=SER-20")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assets Found", response.data)
        self.assertIn(b"2 matches shown.", response.data)
        self.assertIn(b"AT-200", response.data)
        self.assertIn(b"AT-201", response.data)
        self.assertNotIn(b"AT-999", response.data)

    def test_combined_asset_tag_and_serial_search_uses_both_filters(self) -> None:
        self._insert_asset("AT-400", serial_number="SER-400", location_type="STORAGE", home_slot_id=None)
        self._insert_asset("AT-400X", serial_number="SER-999", location_type="STORAGE", home_slot_id=None)
        self._insert_asset("ZZ-400", serial_number="SER-400", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-400&serial_number=SER-400")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertIn(b"AT-400", response.data)
        self.assertIn(b"SER-400", response.data)
        self.assertNotIn(b"AT-400X", response.data)
        self.assertNotIn(b"ZZ-400", response.data)

    def test_combined_partial_asset_tag_and_serial_search_stays_narrow(self) -> None:
        self._insert_asset("AT-510", serial_number="SER-510", location_type="STORAGE", home_slot_id=None)
        self._insert_asset("AT-511", serial_number="SER-777", location_type="STORAGE", home_slot_id=None)
        self._insert_asset("BT-510", serial_number="SER-510", location_type="STORAGE", home_slot_id=None)

        response = self.client.get("/assets/search?asset_tag=AT-51&serial_number=SER-51")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset Found", response.data)
        self.assertIn(b"AT-510", response.data)
        self.assertNotIn(b"AT-511", response.data)
        self.assertNotIn(b"BT-510", response.data)

    def test_search_shows_plain_not_found_feedback(self) -> None:
        response = self.client.get("/assets/search?asset_tag=AT-MISSING")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Asset not found.", response.data)
        self.assertIn(b"No asset found", response.data)
        self.assertIn(b"Check the asset tag or serial number and search again.", response.data)
        self.assertNotIn(b"Asset Found", response.data)
        self.assertIn(b'href="/assets/search"', response.data)
        self.assertIn(b">Clear<", response.data)

    def test_search_page_renders_clear_link_when_any_field_is_filled(self) -> None:
        response = self.client.get("/assets/search?asset_tag=AT-100&serial_number=SER-100")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="AT-100"', response.data)
        self.assertIn(b'value="SER-100"', response.data)
        self.assertIn(b'href="/assets/search"', response.data)
        self.assertIn(b">Clear<", response.data)

    def test_clean_search_route_resets_both_fields_and_results(self) -> None:
        self._insert_asset("AT-300", serial_number="SER-300", location_type="STORAGE", home_slot_id=None)

        searched = self.client.get("/assets/search?asset_tag=AT-300&serial_number=SER-300")
        self.assertEqual(searched.status_code, 200)
        self.assertIn(b"Asset Found", searched.data)
        self.assertIn(b'value="AT-300"', searched.data)
        self.assertIn(b'value="SER-300"', searched.data)

        cleared = self.client.get("/assets/search")
        self.assertEqual(cleared.status_code, 200)
        self.assertNotIn(b"Asset Found", cleared.data)
        self.assertIn(b'value=""', cleared.data)
        self.assertNotIn(b">Clear<", cleared.data)


if __name__ == "__main__":
    unittest.main()
