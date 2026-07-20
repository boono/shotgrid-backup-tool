import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "tools/shotgrid_backup/backup.py"
SPEC = importlib.util.spec_from_file_location("shotgrid_backup", MODULE_PATH)
backup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(backup)
sys.modules["backup"] = backup
VERIFY_PATH = Path(__file__).parents[1] / "tools/shotgrid_backup/snapshot_verify.py"
VERIFY_SPEC = importlib.util.spec_from_file_location("shotgrid_snapshot_verify", VERIFY_PATH)
snapshot_verify = importlib.util.module_from_spec(VERIFY_SPEC)
assert VERIFY_SPEC.loader
VERIFY_SPEC.loader.exec_module(snapshot_verify)


class FakeShotGrid:
    def __init__(self):
        self.find_calls = []
        self.config = SimpleNamespace(records_per_page=500, server="example.shotgrid.autodesk.com")

    def info(self):
        return {"api_max_entities_per_page": 5000, "version": [8, 0, 0]}

    def schema_entity_read(self):
        return {"Shot": {"name": {"value": "Shots"}}}

    def schema_field_read(self, entity):
        return {
            "id": {"visible": {"value": True}},
            "code": {"visible": {"value": True}},
            "updated_at": {"visible": {"value": True}},
        }

    def find_one(self, entity, filters, fields, **kwargs):
        if kwargs.get("retired_only"):
            return None
        return {"type": entity, "id": 7}

    def find(self, entity, filters, fields, **kwargs):
        self.find_calls.append((entity, filters, fields, kwargs))
        last_id = next((item[2] for item in filters if item[0] == "id"), 0)
        if kwargs["retired_only"] or last_id >= 7:
            return []
        return [{"type": entity, "id": 7, "code": "sh010", "updated_at": dt.datetime(2026, 1, 2, 3, 4, 5)}]


class BackupTests(unittest.TestCase):
    def test_parse_since_normalizes_to_utc(self):
        self.assertEqual(backup.parse_since("2026-07-01T08:00:00+08:00"), "2026-07-01T00:00:00Z")

    def test_readable_fields_includes_id(self):
        self.assertEqual(backup.readable_fields({"code": {"visible": {"value": True}}}), ["code", "id"])

    def test_hidden_but_readable_field_is_included(self):
        fields = backup.readable_fields({"secret": {"visible": {"value": False}}})
        self.assertEqual(fields, ["id", "secret"])

    def test_hidden_but_readable_entity_is_discovered(self):
        entities = backup.discover_entities({
            "Visible": {"visible": {"value": True}},
            "Hidden": {"visible": {"value": False}},
        })
        self.assertEqual(entities, ["Hidden", "Visible"])

    def test_connect_passes_optional_proxy(self):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        fake_module = type("FakeModule", (), {"Shotgun": FakeClient})
        environment = {
            "SHOTGRID_URL": "https://example.shotgrid.autodesk.com",
            "SHOTGRID_SCRIPT_NAME": "backup",
            "SHOTGRID_SCRIPT_KEY": "test-only-key",
            "SHOTGRID_HTTP_PROXY": "127.0.0.1:7892",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.dict("sys.modules", {"shotgun_api3": fake_module}):
                client = backup.connect()
        self.assertEqual(client.kwargs["http_proxy"], "127.0.0.1:7892")

    def test_connect_cli_proxy_overrides_environment(self):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

        fake_module = type("FakeModule", (), {"Shotgun": FakeClient})
        environment = {
            "SHOTGRID_URL": "https://example.shotgrid.autodesk.com",
            "SHOTGRID_SCRIPT_NAME": "backup",
            "SHOTGRID_SCRIPT_KEY": "test-only-key",
            "SHOTGRID_HTTP_PROXY": "127.0.0.1:1111",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.dict("sys.modules", {"shotgun_api3": fake_module}):
                client = backup.connect("127.0.0.1:7892")
        self.assertEqual(client.kwargs["http_proxy"], "127.0.0.1:7892")

    def test_real_client_host_only_config_rebuilds_site_origin(self):
        client = SimpleNamespace(
            base_url="https://example.shotgrid.autodesk.com",
            config=SimpleNamespace(
                server="example.shotgrid.autodesk.com",
                scheme="https",
            ),
        )
        self.assertEqual(
            backup.shotgrid_site_origin(client),
            "https://example.shotgrid.autodesk.com",
        )
        del client.base_url
        self.assertEqual(
            backup.shotgrid_site_origin(client),
            "https://example.shotgrid.autodesk.com",
        )
        client.config.server = "https://example.shotgrid.autodesk.com"
        self.assertEqual(
            backup.shotgrid_site_origin(client),
            "https://example.shotgrid.autodesk.com",
        )

    def test_backup_writes_complete_snapshot_and_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backups"
            args = Namespace(
                entities="Shot",
                output=output,
                updated_since=None,
                no_attachments=True,
            )
            result = backup.run_backup(FakeShotGrid(), args, {"page_size": 10, "max_retries": 1})
            manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
            rows = (result / "entities/Shot.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(manifest["entities"]["Shot"]["active"], 1)
            self.assertTrue(manifest["snapshot_upper_bound"].endswith("Z"))
            envelope = json.loads(rows[0])
            self.assertEqual(envelope["source"], {"type": "Shot", "id": 7})
            self.assertEqual(envelope["record"]["updated_at"], "2026-01-02T03:04:05")
            self.assertTrue((result / "COMPLETED.json").is_file())
            self.assertTrue((result / "checksums.sha256").is_file())
            recovery_header = json.loads(
                (result / "recovery_header.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recovery_header["source"]["site"], manifest["source"]["site"])
            self.assertEqual(
                manifest["recovery_header"]["sha256"],
                backup.sha256_file(result / "recovery_header.json"),
            )
            self.assertIn("recovery_header.json", (result / "checksums.sha256").read_text())
            self.assertEqual((output / "latest.txt").read_text().strip(), result.name)

    def test_backup_uses_keyset_and_valid_time_operator(self):
        with tempfile.TemporaryDirectory() as temp:
            client = FakeShotGrid()
            args = Namespace(
                entities="Shot", output=Path(temp), updated_since="2026-01-01T00:00:00Z",
                no_attachments=True, workers=1,
            )
            backup.run_backup(client, args, {"page_size": 1, "max_retries": 1})
            active_calls = [
                call for call in client.find_calls
                if not call[3]["retired_only"] and any(item[0] == "id" for item in call[1])
            ]
            self.assertEqual(active_calls[0][1][-1], ["id", "greater_than", 0])
            self.assertEqual(active_calls[1][1][-1], ["id", "greater_than", 7])
            operators = [item[1] for item in active_calls[0][1]]
            self.assertIn("less_than", operators)
            self.assertNotIn("less_than_or_equal", operators)

    def test_snapshot_verify_detects_tamper(self):
        with tempfile.TemporaryDirectory() as temp:
            client = FakeShotGrid()
            args = Namespace(
                entities="Shot", output=Path(temp), updated_since=None,
                no_attachments=True, workers=1,
            )
            result = backup.run_backup(client, args, {"page_size": 10, "max_retries": 1})
            self.assertTrue(snapshot_verify.verify_snapshot(result)["ok"])
            entity_file = result / "entities/Shot.jsonl"
            entity_file.write_text(entity_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
            verification = snapshot_verify.verify_snapshot(result)
            self.assertFalse(verification["ok"])
            self.assertTrue(any("SHA-256" in error or "哈希" in error for error in verification["errors"]))

    @unittest.skipIf(os.name == "nt", "Windows symlink 权限因运行环境而异")
    def test_snapshot_verify_rejects_internal_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            args = Namespace(
                entities="Shot", output=Path(temp), updated_since=None,
                no_attachments=True, workers=1,
            )
            result = backup.run_backup(
                FakeShotGrid(), args, {"page_size": 10, "max_retries": 1}
            )
            (result / "rogue_link").symlink_to(result / "manifest.json")
            verification = snapshot_verify.verify_snapshot(result)
            self.assertFalse(verification["ok"])
            self.assertTrue(
                any("符号链接" in error or "symlink" in error.lower() for error in verification["errors"]),
                verification["errors"],
            )

    def test_entity_and_multi_entity_links_are_indexed_in_order(self):
        class LinkShotGrid(FakeShotGrid):
            def schema_field_read(self, entity):
                fields = super().schema_field_read(entity)
                fields["project"] = {"data_type": {"value": "entity"}}
                fields["assets"] = {"data_type": {"value": "multi_entity"}}
                return fields

            def find(self, entity, filters, fields, **kwargs):
                self.find_calls.append((entity, filters, fields, kwargs))
                last_id = next((item[2] for item in filters if item[0] == "id"), 0)
                if kwargs["retired_only"] or last_id >= 7:
                    return []
                return [{
                    "type": entity,
                    "id": 7,
                    "code": "sh010",
                    "updated_at": dt.datetime(2026, 1, 2, 3, 4, 5),
                    "project": {"type": "Project", "id": 2, "name": "Demo"},
                    "assets": [
                        {"type": "Asset", "id": 11, "name": "A"},
                        {"type": "Asset", "id": 12, "name": "B"},
                    ],
                }]

        with tempfile.TemporaryDirectory() as temp:
            args = Namespace(
                entities="Shot", output=Path(temp), updated_since=None,
                no_attachments=True, workers=1,
            )
            result = backup.run_backup(LinkShotGrid(), args, {"page_size": 10, "max_retries": 1})
            links = [json.loads(line) for line in (result / "links/Shot.jsonl").read_text().splitlines()]
            self.assertEqual(len(links), 3)
            asset_links = [item for item in links if item["field"] == "assets"]
            self.assertEqual([item["ordinal"] for item in asset_links], [0, 1])
            self.assertEqual([item["target"]["id"] for item in asset_links], [11, 12])
            self.assertTrue(snapshot_verify.verify_snapshot(result)["ok"])

    def test_full_site_profile_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            scoped_args = Namespace(
                entities="Shot", output=Path(temp) / "scoped", updated_since=None,
                no_attachments=True, workers=1,
            )
            scoped = backup.run_backup(
                FakeShotGrid(), scoped_args, {"page_size": 10, "max_retries": 1}
            )
            self.assertFalse(snapshot_verify.verify_snapshot(scoped, require_full=True)["ok"])

            full_args = Namespace(
                entities=None, output=Path(temp) / "full", updated_since=None,
                no_attachments=False, workers=1,
            )
            full = backup.run_backup(
                FakeShotGrid(), full_args,
                {"page_size": 10, "max_retries": 1, "all_readable": True},
            )
            verification = snapshot_verify.verify_snapshot(full, require_full=True)
            self.assertTrue(verification["ok"], verification["errors"])
            self.assertEqual(verification["profile"], "site_full")

    def test_bare_media_url_is_deferred_from_authenticated_client(self):
        class MediaShotGrid(FakeShotGrid):
            def __init__(self):
                super().__init__()
                self.downloads = []

            def schema_entity_read(self):
                return {"Project": {"name": {"value": "Projects"}}}

            def schema_field_read(self, entity):
                fields = super().schema_field_read(entity)
                fields["image"] = {"data_type": {"value": "image"}}
                return fields

            def find(self, entity, filters, fields, **kwargs):
                self.find_calls.append((entity, filters, fields, kwargs))
                last_id = next((item[2] for item in filters if item[0] == "id"), 0)
                if kwargs["retired_only"] or last_id >= 7:
                    return []
                return [{
                    "type": entity, "id": 7, "code": "demo",
                    "updated_at": dt.datetime(2026, 1, 2, 3, 4, 5),
                    "image": {"url": "https://example.invalid/thumb.jpg", "name": "thumb.jpg", "size": 3},
                }]

            def download_attachment(self, value, file_path=None, **kwargs):
                self.downloads.append(value)
                Path(file_path).write_bytes(b"abc")
                return file_path

        with tempfile.TemporaryDirectory() as temp:
            client = MediaShotGrid()
            args = Namespace(
                entities=None, output=Path(temp), updated_since=None,
                no_attachments=False, workers=1,
            )
            result = backup.run_backup(
                client, args, {"all_readable": True, "page_size": 10, "max_retries": 1}
            )
            manifest = json.loads((result / "manifest.json").read_text())
            self.assertEqual(manifest["media"]["downloaded"], 0)
            self.assertEqual(manifest["media"]["metadata_only"], 1)
            self.assertEqual(client.downloads, [])
            self.assertTrue(snapshot_verify.verify_snapshot(result, require_full=True)["ok"])


if __name__ == "__main__":
    unittest.main()
