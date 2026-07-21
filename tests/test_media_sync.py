from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock


TOOL_DIR = Path(__file__).parents[1] / "tools/shotgrid_backup"
sys.path.insert(0, str(TOOL_DIR))
import backup  # noqa: E402
import media_sync  # noqa: E402
import snapshot_verify  # noqa: E402


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def tree_digest(root: Path) -> str:
    """Hash names, symlink targets and file bytes without following links."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        elif path.is_dir():
            digest.update(b"dir\0")
        else:
            digest.update(b"special\0")
    return digest.hexdigest()


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class FakeShotGrid:
    """Thread-safe, offline subset of shotgun_api3 used by the media tests."""

    def __init__(self, schemas, records, downloads=None, delay=0.0):
        self.schemas = schemas
        self.records = records
        self.downloads = downloads or {}
        self.delay = delay
        self.download_calls = []
        self.find_calls = []
        self.factory_calls = 0
        self.active_downloads = 0
        self.max_active_downloads = 0
        self._lock = threading.Lock()
        self.config = SimpleNamespace(
            records_per_page=5000,
            server="https://example.shotgrid.autodesk.com",
        )

    def info(self):
        return {"api_max_entities_per_page": 5000, "version": [8, 0, 0]}

    def schema_entity_read(self):
        return {entity: {"name": {"value": entity}} for entity in self.schemas}

    def schema_field_read(self, entity):
        return self.schemas[entity]

    def find(self, entity, filters, fields, **kwargs):
        with self._lock:
            self.find_calls.append((entity, filters, fields, kwargs))
        if kwargs.get("retired_only"):
            return []
        last_id = 0
        wanted_id = None
        for field, operator, value in filters:
            if field == "id" and operator == "greater_than":
                last_id = int(value)
            if field == "id" and operator in {"is", "equals"}:
                wanted_id = int(value)
        rows = [
            dict(item)
            for item in self.records.get(entity, [])
            if int(item["id"]) > last_id
            and (wanted_id is None or int(item["id"]) == wanted_id)
        ]
        rows.sort(key=lambda item: int(item["id"]))
        return rows[: int(kwargs.get("limit") or len(rows) or 1)]

    def find_one(self, entity, filters, fields, **kwargs):
        rows = self.find(entity, filters, fields, limit=1, retired_only=False, **kwargs)
        return rows[0] if rows else None

    def factory(self):
        with self._lock:
            self.factory_calls += 1
        return self

    def download_attachment(self, value, file_path=None, **kwargs):
        if file_path is None:
            file_path = kwargs.get("file_path")
        if file_path is None:
            raise AssertionError("fake download requires file_path")
        if isinstance(value, int):
            candidates = [value, str(value)]
        elif isinstance(value, dict):
            candidates = [
                value.get("url"),
                value.get("name"),
                value.get("id"),
            ]
        else:
            candidates = [value]
        payload = None
        for key in candidates:
            if key in self.downloads:
                payload = self.downloads[key]
                break
        if payload is None:
            raise AssertionError(f"unexpected authenticated download: {value!r}")
        with self._lock:
            self.download_calls.append(value)
            self.active_downloads += 1
            self.max_active_downloads = max(
                self.max_active_downloads, self.active_downloads
            )
        try:
            if self.delay:
                time.sleep(self.delay)
            Path(file_path).write_bytes(payload)
        finally:
            with self._lock:
                self.active_downloads -= 1
        return str(file_path)


def field(data_type="text"):
    return {"data_type": {"value": data_type}, "visible": {"value": True}}


def local_value(path: Path | str, *, name=None):
    value = str(path)
    return {
        "link_type": "local",
        "local_path": value,
        "local_path_linux": value,
        "local_path_mac": value,
        "name": name or Path(value).name,
    }


def upload_value(name: str, payload: bytes):
    return {
        "link_type": "upload",
        "name": name,
        "url": f"https://example.shotgrid.autodesk.com/file/{name}",
        "size": len(payload),
    }


def make_base(output_root: Path, schemas, records, downloads=None):
    client = FakeShotGrid(schemas, records, downloads)
    args = Namespace(
        entities=None,
        output=output_root,
        updated_since=None,
        no_attachments=False,
        workers=1,
    )
    result = backup.run_backup(
        client,
        args,
        {
            "all_readable": True,
            "page_size": 5000,
            "max_retries": 1,
            "include_retired": False,
            "retirement_support": {entity: False for entity in schemas},
        },
    )
    return result, client


def supplement_index(supplement: Path):
    return load_json(supplement / "media/index.json")


def index_by_source(supplement: Path):
    return {
        (item["source"]["type"], int(item["source"]["id"]), item["field"]): item
        for item in supplement_index(supplement)
    }


def incomplete_supplements(output_root: Path, base_id: str):
    parent = output_root / "media_supplements" / base_id
    return sorted(parent.glob("*.incomplete")) if parent.is_dir() else []


def reseal_supplement(supplement: Path):
    """Refresh outer hashes so a test can isolate an inner semantic failure."""
    checksum_path = supplement / "checksums.sha256"
    lines = []
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        _, relative = line.split("  ", 1)
        target = supplement / PurePosixPath(relative)
        lines.append(f"{backup.sha256_file(target)}  {relative}")
    backup.atomic_text(checksum_path, "\n".join(lines) + "\n")
    receipt_path = supplement / "COMPLETED.json"
    receipt = load_json(receipt_path)
    receipt["manifest_sha256"] = backup.sha256_file(supplement / "manifest.json")
    receipt["checksums_sha256"] = backup.sha256_file(checksum_path)
    backup.atomic_json(receipt_path, receipt)


def flatten_legacy_media(interrupted: Path):
    """Rewrite current nested base media as the legacy flat layout being salvaged."""
    index_path = interrupted / "media/index.json"
    rows = load_json(index_path)
    for item in rows:
        source = item["source"]
        original = interrupted / item["file"]
        entity_root = interrupted / "media" / source["type"]
        target = entity_root / (
            f"{int(source['id'])}_{item['field']}_{original.name}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(original, target)
        item["file"] = target.relative_to(interrupted).as_posix()
    for entity_root in (interrupted / "media").iterdir():
        if not entity_root.is_dir():
            continue
        for child in list(entity_root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
    backup.atomic_json(index_path, rows)


def make_legacy_interrupted(output_root: Path):
    payloads = {
        1: b"legacy-one",
        2: b"legacy-two",
        3: b"download-only-missing",
    }
    values = {
        source_id: upload_value(f"legacy_{source_id}.mov", payload)
        for source_id, payload in payloads.items()
    }
    schemas = {"PublishedFile": {"id": field("number"), "path": field("url")}}
    records = {
        "PublishedFile": [
            {
                "type": "PublishedFile",
                "id": source_id,
                "path": values[source_id],
            }
            for source_id in sorted(values)
        ]
    }
    downloads = {values[key]["url"]: payloads[key] for key in values}
    base, client = make_base(output_root, schemas, records, downloads)
    interrupted = output_root / f"{media_sync._timestamp_id()}.incomplete"
    shutil.copytree(base, interrupted)
    (interrupted / "COMPLETED.json").unlink()
    (interrupted / "checksums.sha256").unlink()
    flatten_legacy_media(interrupted)
    old_index_path = interrupted / "media/index.json"
    old_index = load_json(old_index_path)
    old_index_by_id = {int(item["source"]["id"]): item for item in old_index}
    missing_item = old_index_by_id[3]
    missing_payload = interrupted / missing_item["file"]
    missing_payload.unlink()
    missing_payload.with_name(missing_payload.name + ".part").write_bytes(
        b"must-not-be-reused"
    )
    backup.atomic_json(
        old_index_path,
        [old_index_by_id[1], old_index_by_id[2]],
    )
    client.download_calls.clear()
    client.factory_calls = 0
    return base, interrupted, client, payloads


class MediaSyncTests(unittest.TestCase):
    def test_real_client_host_only_config_and_legacy_snapshot_origin(self):
        client = SimpleNamespace(
            base_url="https://example.shotgrid.autodesk.com",
            config=SimpleNamespace(
                server="example.shotgrid.autodesk.com",
                scheme="https",
            ),
        )
        self.assertEqual(
            media_sync._sg_server(client),
            "https://example.shotgrid.autodesk.com",
        )
        self.assertFalse(
            media_sync._structured_for_sg(
                {
                    "source": {"type": "PublishedFile", "id": 1},
                    "field": "path",
                    "_locator": {
                        "link_type": "upload",
                        "url": "https://external.example.invalid/file.mov",
                    },
                },
                "https://example.shotgrid.autodesk.com",
            )
        )
        with mock.patch.object(
            media_sync.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=media_sync.MIN_FREE_RESERVE_BYTES),
        ):
            with self.assertRaises(media_sync.MediaSyncError):
                media_sync._require_supplement_capacity(Path.cwd(), 1)
        pinned = media_sync._PinnedHTTPSConnection(
            "cdn.example.invalid", 443, "93.184.216.34", 5.0
        )
        raw_socket = mock.Mock()
        tls_socket = mock.Mock()
        with mock.patch.object(
            media_sync.socket, "create_connection", return_value=raw_socket
        ) as create_connection, mock.patch.object(
            pinned._context, "wrap_socket", return_value=tls_socket
        ) as wrap_socket:
            pinned.connect()
        create_connection.assert_called_once_with(
            ("93.184.216.34", 443), 5.0, None
        )
        wrap_socket.assert_called_once_with(
            raw_socket, server_hostname="cdn.example.invalid"
        )
        self.assertIs(pinned.sock, tls_socket)
        self.assertEqual(
            media_sync._normalized_origin("example.shotgrid.autodesk.com"),
            "https://example.shotgrid.autodesk.com",
        )
        del client.base_url
        self.assertEqual(
            media_sync._sg_server(client),
            "https://example.shotgrid.autodesk.com",
        )
        client.config.server = "https://example.shotgrid.autodesk.com"
        self.assertEqual(
            media_sync._sg_server(client),
            "https://example.shotgrid.autodesk.com",
        )

    maxDiff = None

    def test_local_unicode_single_and_sparse_percent_and_hash_sequences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = root / "源 素材"
            sources.mkdir()
            single = sources / "中文 空格.mov"
            single.write_bytes("单文件".encode("utf-8"))
            percent_pattern = sources / "镜头.%04d.exr"
            percent_files = {
                1: b"percent-one",
                7: b"percent-seven",
                1033: b"percent-thousand",
            }
            for frame_number, payload in percent_files.items():
                (sources / f"镜头.{frame_number:04d}.exr").write_bytes(payload)
            hash_pattern = sources / "take.####.dpx"
            hash_files = {2: b"hash-two", 19: b"hash-nineteen"}
            for frame_number, payload in hash_files.items():
                (sources / f"take.{frame_number:04d}.dpx").write_bytes(payload)

            output = root / "backups"
            schemas = {"PublishedFile": {"id": field("number"), "path": field()}}
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": local_value(single)},
                    {
                        "type": "PublishedFile",
                        "id": 2,
                        "path": local_value(percent_pattern),
                    },
                    {
                        "type": "PublishedFile",
                        "id": 18001,
                        "path": local_value(hash_pattern),
                    },
                ]
            }
            base, client = make_base(output, schemas, records)
            client.download_calls.clear()
            supplement = media_sync.materialize_latest_media(
                output,
                client,
                client_factory=client.factory,
                max_workers=4,
                copy_external=True,
            )

            verification = media_sync.verify_media_supplement(supplement)
            self.assertTrue(verification["ok"], verification.get("errors"))
            by_source = index_by_source(supplement)
            self.assertEqual(len(by_source), 3)
            expected = {
                1: [single.read_bytes()],
                2: [percent_files[key] for key in sorted(percent_files)],
                18001: [hash_files[key] for key in sorted(hash_files)],
            }
            for source_id, expected_payloads in expected.items():
                item = by_source[("PublishedFile", source_id, "path")]
                self.assertEqual(item["status"], "complete")
                payloads = []
                frames = []
                for file_item in item["files"]:
                    relative = PurePosixPath(file_item["path"])
                    self.assertGreaterEqual(len(relative.parts), 6)
                    self.assertEqual(relative.parts[:2], ("media", "PublishedFile"))
                    self.assertEqual(relative.parts[3], str(source_id))
                    target = supplement / relative
                    payload = target.read_bytes()
                    payloads.append(payload)
                    self.assertEqual(file_item["size"], len(payload))
                    self.assertEqual(file_item["sha256"], sha256_bytes(payload))
                    if "frame" in file_item:
                        frames.append(int(file_item["frame"]))
                self.assertEqual(payloads, expected_payloads)
                if source_id != 1:
                    self.assertEqual(frames, sorted(frames))
            self.assertEqual(client.download_calls, [])
            self.assertEqual(media_sync.find_latest_snapshot(output), base)

    def test_external_media_is_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "nas.mov"
            source.write_bytes(b"external-media")
            output = root / "backups"
            schemas = {
                "PublishedFile": {"id": field("number"), "path": field("text")}
            }
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": local_value(source)}
                ]
            }
            _, client = make_base(output, schemas, records)

            with mock.patch.object(
                media_sync,
                "_copy_file_atomic",
                side_effect=AssertionError("default policy must not copy external media"),
            ):
                supplement = media_sync.materialize_latest_media(
                    output, client, client_factory=client.factory, max_workers=2
                )

            item = index_by_source(supplement)[("PublishedFile", 1, "path")]
            self.assertEqual(item["status"], "excluded_by_policy")
            self.assertEqual(item["acquisition"], "excluded_by_policy")
            self.assertEqual(item["files"], [])
            manifest = load_json(supplement / "manifest.json")
            self.assertFalse(manifest["policy"]["copy_external"])
            self.assertEqual(manifest["payload_coverage"]["external"], "not_requested")
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])
            (supplement / ".DS_Store").write_bytes(b"finder-metadata")
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])
            self.assertTrue(snapshot_verify.verify_snapshot(supplement)["ok"])

    def test_base_attachment_and_media_are_reused_without_client_factory(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backups"
            attachment = b"attachment-payload"
            thumbnail = b"thumbnail-payload"
            attachment_value = upload_value("附件 原件.bin", attachment)
            thumbnail_value = upload_value("thumb.jpg", thumbnail)
            schemas = {
                "Attachment": {"id": field("number"), "this_file": field("url")},
                "Version": {"id": field("number"), "image": field("image")},
            }
            records = {
                "Attachment": [
                    {
                        "type": "Attachment",
                        "id": 11,
                        "this_file": attachment_value,
                        "file_size": len(attachment),
                    }
                ],
                "Version": [
                    {"type": "Version", "id": 22, "image": thumbnail_value}
                ],
            }
            base, client = make_base(
                output,
                schemas,
                records,
                {
                    11: attachment,
                    thumbnail_value["url"]: thumbnail,
                },
            )
            self.assertEqual(len(client.download_calls), 2)
            client.download_calls.clear()
            client.factory_calls = 0

            supplement = media_sync.materialize_latest_media(
                output, client, client_factory=client.factory, max_workers=8
            )
            index = supplement_index(supplement)
            self.assertEqual(len(index), 2)
            self.assertTrue(
                all(item["acquisition"] == "reused_base" for item in index), index
            )
            self.assertEqual(client.factory_calls, 0)
            self.assertEqual(client.download_calls, [])
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])
            self.assertEqual(media_sync.find_latest_snapshot(output), base)

    def test_complete_is_idempotent_and_incomplete_run_resumes_verified_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_a = root / "a.mov"
            source_b = root / "b.mov"
            source_a.write_bytes(b"a" * 17)
            source_b.write_bytes(b"b" * 19)
            hosted_payload = b"hosted-once"
            hosted = upload_value("once.mov", hosted_payload)
            schemas = {"PublishedFile": {"id": field("number"), "path": field()}}
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": local_value(source_a)},
                    {"type": "PublishedFile", "id": 2, "path": local_value(source_b)},
                    {"type": "PublishedFile", "id": 3, "path": hosted},
                ]
            }
            output = root / "complete"
            _, client = make_base(
                output, schemas, records, {hosted["url"]: hosted_payload}
            )
            first = media_sync.materialize_latest_media(
                output, client, max_workers=2, copy_external=True
            )
            first_digest = tree_digest(first)
            with mock.patch.object(
                media_sync,
                "_copy_file_atomic",
                side_effect=AssertionError("complete supplement must be reused"),
            ), mock.patch.object(
                client,
                "download_attachment",
                side_effect=AssertionError("complete supplement must not redownload"),
            ):
                second = media_sync.materialize_latest_media(
                    output, client, max_workers=2, copy_external=True
                )
            self.assertEqual(second, first)
            self.assertEqual(tree_digest(first), first_digest)

            resume_output = root / "resume"
            missing = root / "appears_later.mov"
            resume_records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": local_value(source_a)},
                    {"type": "PublishedFile", "id": 2, "path": local_value(missing)},
                ]
            }
            resume_base, resume_client = make_base(
                resume_output, schemas, resume_records
            )
            with self.assertRaises(RuntimeError):
                media_sync.materialize_latest_media(
                    resume_output,
                    resume_client,
                    max_workers=1,
                    copy_external=True,
                )
            partials = incomplete_supplements(resume_output, resume_base.name)
            self.assertEqual(len(partials), 1)
            partial_item = index_by_source(partials[0])[("PublishedFile", 1, "path")]
            self.assertEqual(partial_item["status"], "complete")
            partial_payload = partials[0] / partial_item["files"][0]["path"]
            partial_payload_bytes = partial_payload.read_bytes()

            missing.write_bytes(b"now-present")
            copied_sources = []
            original_copy = media_sync._copy_file_atomic

            def recording_copy(source, target):
                copied_sources.append(Path(source).resolve())
                return original_copy(source, target)

            with mock.patch.object(
                media_sync, "_copy_file_atomic", side_effect=recording_copy
            ):
                resumed = media_sync.materialize_latest_media(
                    resume_output,
                    resume_client,
                    max_workers=1,
                    copy_external=True,
                )
            resumed_index = index_by_source(resumed)
            resumed_a = resumed_index[("PublishedFile", 1, "path")]
            resumed_b = resumed_index[("PublishedFile", 2, "path")]
            self.assertEqual(resumed_a["acquisition"], "resumed")
            self.assertEqual(resumed_b["acquisition"], "copied")
            self.assertNotIn(source_a.resolve(), copied_sources)
            self.assertIn(missing.resolve(), copied_sources)
            resumed_a_path = resumed / resumed_a["files"][0]["path"]
            self.assertNotEqual(resumed_a_path.stat().st_ino, source_a.stat().st_ino)
            source_a.write_bytes(b"source-mutated-after-resume")
            self.assertEqual(resumed_a_path.read_bytes(), partial_payload_bytes)
            self.assertTrue(media_sync.verify_media_supplement(resumed)["ok"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_unsafe_local_sources_and_missing_required_media_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            regular = root / "regular.mov"
            regular.write_bytes(b"regular")
            symlink = root / "linked.mov"
            symlink.symlink_to(regular)
            directory = root / "directory.mov"
            directory.mkdir()
            fifo = root / "named_pipe.mov"
            os.mkfifo(fifo)
            missing = root / "missing.mov"
            business_url = "https://business.example.invalid/review/123"
            schemas = {
                "Project": {"id": field("number"), "website": field("url")},
                "PublishedFile": {"id": field("number"), "path": field()},
            }
            records = {
                "Project": [
                    {"type": "Project", "id": 99, "website": business_url}
                ],
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": local_value(missing)},
                    {"type": "PublishedFile", "id": 2, "path": local_value(symlink)},
                    {"type": "PublishedFile", "id": 3, "path": local_value(directory)},
                    {"type": "PublishedFile", "id": 4, "path": local_value(fifo)},
                ],
            }
            output = root / "backups"
            base, client = make_base(output, schemas, records)
            with mock.patch.object(
                media_sync.urllib.request,
                "urlopen",
                side_effect=AssertionError("ordinary business URL must stay metadata-only"),
            ) as urlopen, mock.patch.object(
                media_sync.urllib.request,
                "build_opener",
                side_effect=AssertionError("ordinary business URL must stay metadata-only"),
            ) as build_opener, mock.patch.object(
                media_sync.socket,
                "getaddrinfo",
                side_effect=AssertionError("ordinary business URL must not resolve DNS"),
            ) as getaddrinfo:
                with self.assertRaises(RuntimeError):
                    media_sync.materialize_latest_media(
                        output, client, max_workers=2, copy_external=True
                    )
            urlopen.assert_not_called()
            build_opener.assert_not_called()
            getaddrinfo.assert_not_called()

            partials = incomplete_supplements(output, base.name)
            self.assertEqual(len(partials), 1)
            partial = partials[0]
            self.assertFalse((partial / "COMPLETED.json").exists())
            manifest = load_json(partial / "manifest.json")
            self.assertEqual(manifest["status"], "partial")
            failures = {
                int(item["source"]["id"]): item
                for item in supplement_index(partial)
                if item["source"]["type"] == "PublishedFile"
            }
            self.assertEqual(set(failures), {1, 2, 3, 4})
            self.assertTrue(all(item["status"] != "complete" for item in failures.values()))
            ordinary = index_by_source(partial)[("Project", 99, "website")]
            self.assertEqual(ordinary["kind"], "ordinary_web")
            self.assertEqual(ordinary["status"], "skipped")
            self.assertEqual(ordinary["files"], [])
            self.assertFalse(media_sync.verify_media_supplement(partial)["ok"])

    def test_hosted_upload_uses_authenticated_client_while_local_uses_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local = root / "mounted NAS" / "plate.mov"
            local.parent.mkdir()
            local.write_bytes(b"copied-locally")
            hosted_payload = b"authenticated-upload"
            hosted = upload_value("hosted.mov", hosted_payload)
            bare_url = "https://public-cdn.example.invalid/bare.mov"
            bare_payload = b"isolated-no-auth"
            business_url = "https://untrusted.example.invalid/task/42"
            schemas = {
                "Project": {"id": field("number"), "website": field("url")},
                # text prevents legacy backup.py from pre-downloading this upload;
                # media_sync still classifies PublishedFile.path by its value.
                "PublishedFile": {"id": field("number"), "path": field("text")},
            }
            records = {
                "Project": [
                    {"type": "Project", "id": 9, "website": business_url}
                ],
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": hosted},
                    {"type": "PublishedFile", "id": 2, "path": local_value(local)},
                    {"type": "PublishedFile", "id": 3, "path": bare_url},
                ],
            }
            output = root / "backups"
            _, client = make_base(
                output, schemas, records, {hosted["url"]: hosted_payload}
            )
            self.assertEqual(client.download_calls, [])

            def isolated_download(url, target, expected_size):
                self.assertEqual(url, bare_url)
                backup.ensure_private_directory(Path(target).parent)
                Path(target).write_bytes(bare_payload)
                return len(bare_payload)

            with mock.patch.object(
                media_sync.urllib.request,
                "urlopen",
                side_effect=AssertionError("business URL must not be fetched"),
            ) as urlopen, mock.patch.object(
                media_sync.urllib.request,
                "build_opener",
                side_effect=AssertionError("business URL must not be fetched"),
            ) as build_opener, mock.patch.object(
                media_sync.socket,
                "getaddrinfo",
                side_effect=AssertionError("business URL must not resolve DNS"),
            ) as getaddrinfo, mock.patch.object(
                media_sync,
                "_isolated_https_download",
                side_effect=isolated_download,
            ) as isolated:
                supplement = media_sync.materialize_latest_media(
                    output,
                    client,
                    client_factory=client.factory,
                    max_workers=3,
                    copy_external=True,
                )
            urlopen.assert_not_called()
            build_opener.assert_not_called()
            getaddrinfo.assert_not_called()
            isolated.assert_called_once()
            self.assertEqual(client.download_calls, [hosted])
            by_source = index_by_source(supplement)
            uploaded = by_source[("PublishedFile", 1, "path")]
            copied = by_source[("PublishedFile", 2, "path")]
            isolated_item = by_source[("PublishedFile", 3, "path")]
            self.assertEqual(uploaded["acquisition"], "downloaded")
            self.assertEqual(copied["acquisition"], "copied")
            self.assertEqual(
                (supplement / uploaded["files"][0]["path"]).read_bytes(),
                hosted_payload,
            )
            self.assertEqual(
                (supplement / copied["files"][0]["path"]).read_bytes(),
                local.read_bytes(),
            )
            self.assertEqual(
                (supplement / isolated_item["files"][0]["path"]).read_bytes(),
                bare_payload,
            )
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])

    def test_published_file_web_and_empty_paths_are_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backups"
            web_path = {
                "link_type": "web",
                "name": "Review page",
                "url": "https://review.example.invalid/published/1",
            }
            schemas = {
                "PublishedFile": {"id": field("number"), "path": field("text")}
            }
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": web_path},
                    {"type": "PublishedFile", "id": 2, "path": None},
                ]
            }
            _, client = make_base(output, schemas, records)

            with mock.patch.object(
                client,
                "download_attachment",
                side_effect=AssertionError("metadata-only path must not be downloaded"),
            ), mock.patch.object(
                media_sync,
                "_isolated_https_download",
                side_effect=AssertionError("metadata-only path must not be fetched"),
            ):
                supplement = media_sync.materialize_latest_media(
                    output,
                    client,
                    client_factory=client.factory,
                    max_workers=2,
                    copy_external=True,
                )

            by_source = index_by_source(supplement)
            web = by_source[("PublishedFile", 1, "path")]
            empty = by_source[("PublishedFile", 2, "path")]
            self.assertEqual((web["kind"], web["status"]), ("ordinary_web", "skipped"))
            self.assertEqual((empty["kind"], empty["status"]), ("no_payload", "skipped"))
            self.assertEqual(web["files"], [])
            self.assertEqual(empty["files"], [])
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])

    def test_persistent_transient_media_stays_partial_without_completion_receipt(self):
        class PersistentTransientShotGrid(FakeShotGrid):
            def __init__(self, schemas, records, transient):
                super().__init__(schemas, records)
                self.transient = transient
                self.refetch_calls = 0

            def find_one(self, entity, filters, fields, **kwargs):
                self.refetch_calls += 1
                return {"type": entity, "id": 1, "path": self.transient}

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backups"
            transient = {
                "link_type": "upload",
                "name": "processing.png",
                "url": (
                    "https://example.shotgrid.autodesk.com/"
                    "images/status/transient/processing.png"
                ),
            }
            schemas = {
                "PublishedFile": {"id": field("number"), "path": field("text")}
            }
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": transient}
                ]
            }
            make_base(output, schemas, records)
            client = PersistentTransientShotGrid(schemas, records, transient)

            with mock.patch.object(media_sync.time, "sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    media_sync.materialize_latest_media(
                        output,
                        client,
                        client_factory=client.factory,
                        max_workers=1,
                    )

            base = media_sync.find_latest_snapshot(output)
            partials = incomplete_supplements(output, base.name)
            self.assertEqual(len(partials), 1)
            partial = partials[0]
            self.assertFalse((partial / "COMPLETED.json").exists())
            self.assertEqual(load_json(partial / "manifest.json")["status"], "partial")
            item = index_by_source(partial)[("PublishedFile", 1, "path")]
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["files"], [])
            self.assertEqual(item["error"]["code"], "TRANSIENT_MEDIA_PENDING")
            self.assertEqual(item["error"]["type"], "TransientMediaPending")
            persisted_errors = load_json(partial / "logs/errors.json")
            self.assertEqual(
                persisted_errors[0]["error"]["message"],
                item["error"]["message"],
            )
            self.assertEqual(client.refetch_calls, 1)
            self.assertEqual(client.download_calls, [])

    def test_transient_media_refetches_current_locator_and_records_fidelity(self):
        class CurrentLocatorShotGrid(FakeShotGrid):
            def __init__(self, schemas, records, downloads, current):
                super().__init__(schemas, records, downloads)
                self.current = current
                self.refetch_calls = 0

            def find_one(self, entity, filters, fields, **kwargs):
                self.refetch_calls += 1
                return {"type": entity, "id": 1, "path": self.current}

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backups"
            transient = {
                "link_type": "upload",
                "name": "processing.png",
                "url": (
                    "https://example.shotgrid.autodesk.com/"
                    "images/status/transient/processing.png"
                ),
            }
            current_payload = b"current-render-after-processing"
            current = upload_value("current.mov", current_payload)
            schemas = {
                "PublishedFile": {"id": field("number"), "path": field("text")}
            }
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 1, "path": transient}
                ]
            }
            make_base(output, schemas, records)
            client = CurrentLocatorShotGrid(
                schemas,
                records,
                {current["url"]: current_payload},
                current,
            )

            supplement = media_sync.materialize_latest_media(
                output,
                client,
                client_factory=client.factory,
                max_workers=1,
            )

            item = index_by_source(supplement)[("PublishedFile", 1, "path")]
            self.assertEqual(item["status"], "complete")
            self.assertEqual(item["temporal_fidelity"], "current_refetch")
            self.assertNotEqual(
                item["materialized_locator_sha256"], item["locator_sha256"]
            )
            self.assertEqual(
                (supplement / item["files"][0]["path"]).read_bytes(),
                current_payload,
            )
            manifest = load_json(supplement / "manifest.json")
            self.assertFalse(manifest["lineage"]["snapshot_exact"])
            self.assertEqual(
                manifest["lineage"]["temporal_fidelity"], "current_refetch"
            )
            self.assertEqual(client.refetch_calls, 1)
            self.assertEqual(client.download_calls, [current])
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])

    def test_retired_locator_refetch_queries_retired_records_only(self):
        class RetiredLookup:
            def __init__(self):
                self.calls = []

            def find(self, entity, filters, fields, **kwargs):
                self.calls.append((entity, filters, fields, kwargs))
                return [{"type": entity, "id": 9, "path": "retired-value"}]

        client = RetiredLookup()
        result = media_sync._refetch_locator(
            client,
            {
                "source": {"type": "PublishedFile", "id": 9},
                "field": "path",
                "state": "retired",
            },
        )
        self.assertEqual(result, "retired-value")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][3]["retired_only"])

    def test_retired_attachment_download_uses_validated_locator_dict(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = b"retired-attachment-payload"
            locator = upload_value("retired.bin", payload)
            client = FakeShotGrid({}, {}, {locator["url"]: payload})
            target = Path(temp) / "retired.bin"
            size = media_sync._sg_download_once(
                client,
                {
                    "source": {"type": "Attachment", "id": 41},
                    "field": "this_file",
                    "state": "retired",
                    "_locator": locator,
                    "_expected_size": len(payload),
                },
                target,
            )
            self.assertEqual(size, len(payload))
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(client.download_calls, [locator])

    def test_missing_output_and_disconnected_storage_fail_without_mass_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "missing-output"
            with self.assertRaises(media_sync.OutputStorageUnavailable):
                media_sync.materialize_latest_media(
                    missing,
                    FakeShotGrid({}, {}),
                    max_workers=1,
                )
            self.assertFalse(missing.exists())

            output = root / "backups"
            values = {
                source_id: upload_value(f"image_{source_id}.jpg", b"payload")
                for source_id in range(1, 9)
            }
            schemas = {
                "PublishedFile": {"id": field("number"), "path": field("text")}
            }
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": source_id, "path": values[source_id]}
                    for source_id in values
                ]
            }
            base, client = make_base(output, schemas, records)
            disconnected = OSError(errno.ENOTCONN, "Socket is not connected")
            with mock.patch.object(
                client, "download_attachment", side_effect=disconnected
            ), self.assertRaises(media_sync.OutputStorageUnavailable):
                media_sync.materialize_latest_media(
                    output,
                    client,
                    client_factory=client.factory,
                    max_workers=4,
                )

            partials = incomplete_supplements(output, base.name)
            self.assertEqual(len(partials), 1)
            statuses = [item["status"] for item in supplement_index(partials[0])]
            self.assertEqual(set(statuses), {"pending"})

    def test_verify_rejects_payload_size_hash_lineage_and_duplicate_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mov"
            source.write_bytes(b"0123456789abcdef")
            output = root / "backups"
            schemas = {"PublishedFile": {"id": field("number"), "path": field()}}
            records = {
                "PublishedFile": [
                    {"type": "PublishedFile", "id": 7, "path": local_value(source)}
                ]
            }
            base, client = make_base(output, schemas, records)
            base_control_paths = [
                base / "manifest.json",
                base / "checksums.sha256",
                base / "COMPLETED.json",
            ]
            before = {path.name: backup.sha256_file(path) for path in base_control_paths}
            supplement = media_sync.materialize_latest_media(
                output, client, max_workers=2, copy_external=True
            )
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])
            index_path = supplement / "media/index.json"
            original_index = index_path.read_bytes()
            manifest_path = supplement / "manifest.json"
            original_manifest = manifest_path.read_bytes()
            item = supplement_index(supplement)[0]
            payload_path = supplement / item["files"][0]["path"]
            original_payload = payload_path.read_bytes()

            payload_path.write_bytes(original_payload[::-1])
            reseal_supplement(supplement)
            verification = media_sync.verify_media_supplement(supplement)
            self.assertFalse(verification["ok"])
            self.assertIn(item["files"][0]["path"], " ".join(verification["errors"]))
            payload_path.write_bytes(original_payload)
            reseal_supplement(supplement)

            payload_path.write_bytes(original_payload + b"x")
            reseal_supplement(supplement)
            verification = media_sync.verify_media_supplement(supplement)
            self.assertFalse(verification["ok"])
            self.assertIn(item["files"][0]["path"], " ".join(verification["errors"]))
            payload_path.write_bytes(original_payload)
            reseal_supplement(supplement)

            bad_index = load_json(index_path)
            bad_index[0]["files"][0]["sha256"] = "0" * 64
            backup.atomic_json(index_path, bad_index)
            reseal_supplement(supplement)
            verification = media_sync.verify_media_supplement(supplement)
            self.assertFalse(verification["ok"])
            self.assertIn(item["files"][0]["path"], " ".join(verification["errors"]))
            index_path.write_bytes(original_index)
            reseal_supplement(supplement)

            manifest = load_json(manifest_path)
            lineage = manifest["lineage"]
            lineage_key = next(key for key in lineage if key.endswith("sha256"))
            lineage[lineage_key] = "f" * 64
            backup.atomic_json(manifest_path, manifest)
            reseal_supplement(supplement)
            verification = media_sync.verify_media_supplement(supplement)
            self.assertFalse(verification["ok"])
            self.assertIn("lineage", " ".join(verification["errors"]).lower())
            manifest_path.write_bytes(original_manifest)
            reseal_supplement(supplement)

            duplicate = load_json(index_path)
            duplicate.append(dict(duplicate[0]))
            backup.atomic_json(index_path, duplicate)
            reseal_supplement(supplement)
            verification = media_sync.verify_media_supplement(supplement)
            self.assertFalse(verification["ok"])
            self.assertIn("source_key", " ".join(verification["errors"]))

            after = {path.name: backup.sha256_file(path) for path in base_control_paths}
            self.assertEqual(after, before)

    def test_progress_reports_both_resource_pools_bytes_rate_and_worker_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local_payloads = {}
            hosted_payloads = {}
            records = []
            downloads = {}
            for source_id in range(1, 7):
                payload = (f"local-{source_id}" * 97).encode("utf-8")
                source = root / f"local_{source_id}.mov"
                source.write_bytes(payload)
                local_payloads[source_id] = payload
                records.append(
                    {
                        "type": "PublishedFile",
                        "id": source_id,
                        "path": local_value(source),
                    }
                )
            for source_id in range(101, 107):
                payload = (f"hosted-{source_id}" * 101).encode("utf-8")
                value = upload_value(f"hosted_{source_id}.mov", payload)
                hosted_payloads[source_id] = payload
                downloads[value["url"]] = payload
                records.append(
                    {"type": "PublishedFile", "id": source_id, "path": value}
                )
            schemas = {"PublishedFile": {"id": field("number"), "path": field()}}
            output = root / "backups"
            _, base_client = make_base(
                output, schemas, {"PublishedFile": records}, downloads
            )
            transfer_client = FakeShotGrid(
                schemas, {"PublishedFile": records}, downloads, delay=0.015
            )
            events = []
            supplement = media_sync.materialize_latest_media(
                output,
                transfer_client,
                client_factory=transfer_client.factory,
                max_workers=3,
                progress=events.append,
                copy_external=True,
            )
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])
            plan = next(event for event in events if event["event"] == "media_supplement_plan")
            expected_bytes = sum(map(len, local_payloads.values())) + sum(
                map(len, hosted_payloads.values())
            )
            self.assertEqual(plan["items_total"], 12)
            self.assertEqual(plan["bytes_total"], expected_bytes)
            tuning = [
                event for event in events if event["event"] == "media_transfer_tuning"
            ]
            self.assertEqual({event["kind"] for event in tuning}, {"copy", "download"})
            for event in tuning:
                self.assertLessEqual(int(event["workers"]), 3)
                self.assertGreaterEqual(float(event["bytes_per_second"]), 0.0)
            for kind in ("copy", "download"):
                final_tuning = [
                    event for event in tuning if event["kind"] == kind
                ][-1]
                self.assertTrue(final_tuning["queue_complete"])
                self.assertEqual(final_tuning["workers"], 0)
                self.assertEqual(final_tuning["bytes_per_second"], 0.0)
            copy_finished_at = next(
                index
                for index, event in enumerate(events)
                if event.get("event") == "media_transfer_tuning"
                and event.get("kind") == "copy"
                and event.get("queue_complete")
            )
            download_finished_at = next(
                index
                for index, event in enumerate(events)
                if event.get("event") == "media_transfer_tuning"
                and event.get("kind") == "download"
                and event.get("queue_complete")
            )
            self.assertLess(copy_finished_at, download_finished_at)
            transfers = [
                event
                for event in events
                if event["event"]
                in {
                    "media_transfer_complete",
                    "media_transfer_reused",
                    "media_transfer_error",
                }
            ]
            self.assertEqual(len(transfers), 12)
            for event in transfers:
                self.assertIn("bytes_done", event)
                self.assertEqual(event["bytes_total"], expected_bytes)
                self.assertIn("items_done", event)
                self.assertEqual(event["items_total"], 12)
            self.assertLessEqual(transfer_client.max_active_downloads, 3)
            self.assertEqual(base_client.download_calls, [])

    def test_eta_uses_only_latest_hundred_samples_and_calibrates_first_ten(self):
        calibrating = media_sync._estimate_eta(
            [90.0] * 9, remaining_items=20, current_target_workers=4
        )
        self.assertTrue(calibrating["calibrating"])
        self.assertIsNone(calibrating["eta_seconds"])
        self.assertEqual(calibrating["eta_sample_count"], 9)

        estimate = media_sync._estimate_eta(
            [1000.0] * 10 + [0.25] * 100,
            remaining_items=80,
            current_target_workers=5,
        )
        self.assertFalse(estimate["calibrating"])
        self.assertEqual(estimate["eta_sample_count"], 100)
        self.assertEqual(estimate["eta_seconds"], 4.0)

    def test_thousands_of_refs_and_large_sequence_are_bucketed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared = root / "shared.mov"
            shared.write_bytes(b"x")
            sequence_pattern = root / "long.%04d.exr"
            for frame_number in range(1001):
                (root / f"long.{frame_number:04d}.exr").write_bytes(
                    frame_number.to_bytes(2, "big")
                )
            records = [
                {
                    "type": "PublishedFile",
                    "id": source_id,
                    "path": local_value(shared),
                }
                for source_id in range(1, 2006)
            ]
            records.append(
                {
                    "type": "PublishedFile",
                    "id": 18001,
                    "path": local_value(sequence_pattern),
                }
            )
            schemas = {"PublishedFile": {"id": field("number"), "path": field()}}
            output = root / "backups"
            _, client = make_base(output, schemas, {"PublishedFile": records})
            supplement = media_sync.materialize_latest_media(
                output, client, max_workers=32, copy_external=True
            )
            index = index_by_source(supplement)
            self.assertEqual(len(index), 2006)
            entity_root = supplement / "media/PublishedFile"
            buckets = sorted(path.name for path in entity_root.iterdir() if path.is_dir())
            self.assertIn("000000_000999", buckets)
            self.assertIn("001000_001999", buckets)
            self.assertIn("002000_002999", buckets)
            self.assertIn("018000_018999", buckets)
            for bucket in entity_root.iterdir():
                if bucket.is_dir():
                    self.assertLessEqual(
                        len([path for path in bucket.iterdir() if path.is_dir()]),
                        1000,
                    )
            sequence = index[("PublishedFile", 18001, "path")]
            self.assertEqual(len(sequence["files"]), 1001)
            frame_parents = {
                PurePosixPath(item["path"]).parent.name for item in sequence["files"]
            }
            self.assertEqual(
                frame_parents,
                {"frames_000000_000999", "frames_001000_001999"},
            )
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])

    def test_recoverable_legacy_incomplete_is_sealed_and_media_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backups"
            old_base, interrupted, client, payloads = make_legacy_interrupted(output)
            interrupted_digest = tree_digest(interrupted)
            old_base_control = {
                name: backup.sha256_file(old_base / name)
                for name in ("manifest.json", "checksums.sha256", "COMPLETED.json")
            }
            old_media = {
                int(item["source"]["id"]): interrupted / item["file"]
                for item in load_json(interrupted / "media/index.json")
            }

            inspection = media_sync.inspect_latest_snapshot(output)
            self.assertTrue(inspection["recoverable_interrupted"], inspection)
            self.assertTrue(
                os.path.samefile(Path(inspection["interrupted_path"]), interrupted)
            )
            self.assertEqual(inspection["reusable_media_count"], 2)
            supplement = media_sync.materialize_latest_media(
                output, client, client_factory=client.factory, max_workers=3
            )

            self.assertEqual(tree_digest(interrupted), interrupted_digest)
            self.assertEqual(
                {
                    name: backup.sha256_file(old_base / name)
                    for name in ("manifest.json", "checksums.sha256", "COMPLETED.json")
                },
                old_base_control,
            )
            recovered = media_sync.find_latest_snapshot(output)
            self.assertIsNotNone(recovered)
            self.assertNotEqual(recovered, old_base)
            self.assertTrue(recovered.name.endswith("_recovered"))
            recovered_manifest = load_json(recovered / "manifest.json")
            self.assertEqual(recovered_manifest["payload_scope"], "deferred/recovered")
            self.assertEqual(
                recovered_manifest["lineage"]["source_identity_evidence"],
                "recovery_header",
            )
            self.assertEqual(
                recovered_manifest["lineage"][
                    "recovered_from_interrupted_snapshot_id"
                ],
                interrupted.name.removesuffix(".incomplete"),
            )
            self.assertTrue(snapshot_verify.verify_snapshot(recovered)["ok"])

            by_source = index_by_source(supplement)
            reused_targets = {}
            for source_id in (1, 2):
                item = by_source[("PublishedFile", source_id, "path")]
                self.assertEqual(item["acquisition"], "reused_interrupted")
                target = supplement / item["files"][0]["path"]
                reused_targets[source_id] = target
                self.assertEqual(target.read_bytes(), payloads[source_id])
                self.assertNotEqual(
                    target.stat().st_ino, old_media[source_id].stat().st_ino
                )
            old_media[1].write_bytes(b"mutated-old-artifact")
            self.assertEqual(reused_targets[1].read_bytes(), payloads[1])
            downloaded = by_source[("PublishedFile", 3, "path")]
            self.assertEqual(downloaded["acquisition"], "downloaded")
            self.assertEqual(
                (supplement / downloaded["files"][0]["path"]).read_bytes(),
                payloads[3],
            )
            self.assertEqual(len(client.download_calls), 1)
            self.assertFalse(any(path.name.endswith(".part") for path in supplement.rglob("*")))
            self.assertTrue(media_sync.verify_media_supplement(supplement)["ok"])

    def test_real_schema_v1_is_media_only_and_requires_source_evidence(self):
        def build_case(root: Path, *, include_site: bool):
            output = root / "backups"
            payload = b"schema-v1-attachment"
            uploaded = upload_value("legacy.bin", payload)
            schemas = {
                "Attachment": {
                    "id": field("number"),
                    "this_file": field("url"),
                    "file_size": field("number"),
                }
            }
            records = {
                "Attachment": [
                    {
                        "type": "Attachment",
                        "id": 41,
                        "this_file": uploaded,
                        "file_size": len(payload),
                    }
                ]
            }
            client = FakeShotGrid(schemas, records, {41: payload})
            args = Namespace(
                entities=None,
                output=output,
                updated_since=None,
                no_attachments=False,
                workers=1,
            )
            base = backup.run_backup(
                client,
                args,
                {
                    "all_readable": True,
                    "defer_media": True,
                    "page_size": 5000,
                    "max_retries": 1,
                    "include_retired": False,
                    "retirement_support": {"Attachment": False},
                },
            )
            legacy = output / "99999999T235959Z.incomplete"
            backup.atomic_json(legacy / "schema/entities.json", {"Attachment": {}})
            backup.atomic_json(legacy / "schema/fields/Attachment.json", schemas["Attachment"])
            backup.atomic_text(
                legacy / "entities/Attachment.jsonl",
                json.dumps({**records["Attachment"][0], "_backup_retired": False}) + "\n",
            )
            media_file = legacy / "attachments/41_legacy.bin"
            media_file.parent.mkdir(parents=True, exist_ok=True)
            media_file.write_bytes(payload)
            backup.atomic_json(
                legacy / "attachments/index.json",
                [
                    {
                        "attachment_id": 41,
                        "file": media_file.name,
                        "size": len(payload),
                        "sha256": backup.sha256_file(media_file),
                    }
                ],
            )
            backup.atomic_json(
                legacy / "manifest.json",
                {
                    "schema_version": 1,
                    "site": (
                        "https://example.shotgrid.autodesk.com" if include_site else ""
                    ),
                    "mode": "full",
                    "entities": {"Attachment": {"active": 1, "retired": 0}},
                    "status": "partial",
                },
            )
            client.download_calls.clear()
            return output, base, legacy, client, payload

        with tempfile.TemporaryDirectory() as temp:
            output, base, legacy, client, payload = build_case(
                Path(temp) / "bound", include_site=True
            )
            assessment = media_sync._assess_interrupted(legacy)
            self.assertFalse(assessment["recoverable"])
            self.assertTrue(assessment["legacy_v1_media_only"])
            supplement = media_sync.materialize_latest_media(
                output, client, client_factory=client.factory, max_workers=2
            )
            item = index_by_source(supplement)[("Attachment", 41, "this_file")]
            self.assertEqual(item["acquisition"], "reused_interrupted")
            self.assertEqual(
                (supplement / item["files"][0]["path"]).read_bytes(), payload
            )
            self.assertEqual(client.download_calls, [])
            self.assertEqual(media_sync.find_latest_snapshot(output), base)
            self.assertFalse(any("_recovered" in path.name for path in output.iterdir()))

        with tempfile.TemporaryDirectory() as temp:
            output, _, legacy, client, _ = build_case(
                Path(temp) / "unbound", include_site=False
            )
            self.assertTrue(
                media_sync._assess_interrupted(legacy)["legacy_v1_media_only"]
            )
            supplement = media_sync.materialize_latest_media(
                output, client, client_factory=client.factory, max_workers=2
            )
            item = index_by_source(supplement)[("Attachment", 41, "this_file")]
            self.assertEqual(item["acquisition"], "downloaded")
            self.assertEqual(client.download_calls, [41])

        with tempfile.TemporaryDirectory() as temp:
            output, _, legacy, client, current_payload = build_case(
                Path(temp) / "locator_changed", include_site=True
            )
            stale_payload = b"x" * len(current_payload)
            stale_locator = upload_value("stale.bin", stale_payload)
            backup.atomic_text(
                legacy / "entities/Attachment.jsonl",
                json.dumps(
                    {
                        "type": "Attachment",
                        "id": 41,
                        "this_file": stale_locator,
                        "file_size": len(stale_payload),
                        "_backup_retired": False,
                    }
                )
                + "\n",
            )
            stale_media = legacy / "attachments/41_legacy.bin"
            stale_media.write_bytes(stale_payload)
            backup.atomic_json(
                legacy / "attachments/index.json",
                [
                    {
                        "attachment_id": 41,
                        "file": stale_media.name,
                        "size": len(stale_payload),
                        "sha256": backup.sha256_file(stale_media),
                    }
                ],
            )
            supplement = media_sync.materialize_latest_media(
                output, client, client_factory=client.factory, max_workers=2
            )
            item = index_by_source(supplement)[("Attachment", 41, "this_file")]
            self.assertEqual(item["acquisition"], "downloaded")
            self.assertNotEqual(item["acquisition"], "reused_interrupted")
            self.assertEqual(
                (supplement / item["files"][0]["path"]).read_bytes(),
                current_payload,
            )
            self.assertEqual(client.download_calls, [41])

    def test_interrupted_recovery_requires_all_completion_events_and_no_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "template"
            _, _, template_client, _ = make_legacy_interrupted(template)
            for case in (
                "missing_complete",
                "count_mismatch",
                "missing_source",
                "entity_error",
            ):
                with self.subTest(case=case):
                    output = Path(temp) / case
                    shutil.copytree(template, output)
                    interrupted = next(output.glob("*.incomplete"))
                    event_path = interrupted / "logs/events.jsonl"
                    events = [
                        json.loads(line)
                        for line in event_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if case == "missing_complete":
                        events = [
                            event
                            for event in events
                            if event.get("event") != "entity_complete"
                        ]
                    elif case == "count_mismatch":
                        for event in events:
                            if event.get("event") == "entity_complete":
                                event["active"] = int(event.get("active", 0)) + 1
                                break
                    elif case == "missing_source":
                        for evidence_name in ("manifest.json", "recovery_header.json"):
                            evidence_path = interrupted / evidence_name
                            evidence = load_json(evidence_path)
                            evidence["source"] = {}
                            backup.atomic_json(evidence_path, evidence)
                    else:
                        events.append(
                            {
                                "event": "entity_error",
                                "entity": "PublishedFile",
                                "error": {"type": "RuntimeError", "message": "test"},
                            }
                        )
                    event_path.write_text(
                        "".join(
                            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                            for event in events
                        ),
                        encoding="utf-8",
                    )
                    inspection = media_sync.inspect_latest_snapshot(output)
                    self.assertFalse(inspection["recoverable_interrupted"], inspection)
                    with self.assertRaises(RuntimeError):
                        media_sync.materialize_latest_media(
                            output,
                            template_client,
                            client_factory=template_client.factory,
                            max_workers=2,
                        )
                    self.assertEqual(
                        media_sync.find_latest_snapshot(output).name,
                        (output / "latest.txt").read_text(encoding="utf-8").strip(),
                    )
                    self.assertFalse(
                        any(
                            path.name.endswith("_recovered")
                            for path in output.iterdir()
                            if path.is_dir()
                        )
                    )

    def test_dirty_interrupted_media_retries_bad_files_and_resume_only_retries_failure(self):
        class FlakyShotGrid(FakeShotGrid):
            def __init__(self, schemas, records, downloads, transient_url, permanent_url):
                super().__init__(schemas, records, downloads)
                self.transient_url = transient_url
                self.permanent_url = permanent_url
                self.permanent_failure = True
                self.attempts = {}

            def download_attachment(self, value, file_path=None, **kwargs):
                url = value.get("url") if isinstance(value, dict) else str(value)
                self.attempts[url] = self.attempts.get(url, 0) + 1
                if url == self.transient_url and self.attempts[url] <= 2:
                    raise TimeoutError("transient test timeout")
                if url == self.permanent_url and self.permanent_failure:
                    raise TimeoutError("permanent test timeout")
                return super().download_attachment(value, file_path=file_path, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "backups"
            payloads = {
                source_id: (f"payload-{source_id}-".encode("ascii") + b"x" * 64)
                for source_id in range(1, 8)
            }
            values = {
                source_id: upload_value(f"dirty_{source_id}.mov", payload)
                for source_id, payload in payloads.items()
            }
            schemas = {
                "PublishedFile": {"id": field("number"), "path": field("url")}
            }
            records = {
                "PublishedFile": [
                    {
                        "type": "PublishedFile",
                        "id": source_id,
                        "path": values[source_id],
                    }
                    for source_id in sorted(values)
                ]
            }
            downloads = {values[key]["url"]: payloads[key] for key in values}
            base, _ = make_base(output, schemas, records, downloads)
            interrupted = output / f"{media_sync._timestamp_id()}.incomplete"
            shutil.copytree(base, interrupted)
            (interrupted / "COMPLETED.json").unlink()
            (interrupted / "checksums.sha256").unlink()
            flatten_legacy_media(interrupted)
            old_index = {
                int(item["source"]["id"]): item
                for item in load_json(interrupted / "media/index.json")
            }
            old_paths = {
                source_id: interrupted / item["file"]
                for source_id, item in old_index.items()
            }
            old_paths[2].unlink()
            old_paths[2].with_name(old_paths[2].name + ".part").write_bytes(b"partial")
            old_paths[3].write_bytes(b"")
            html_size = len(payloads[4])
            old_paths[4].write_bytes(b"<html>" + b"h" * (html_size - len(b"<html>")))
            old_paths[5].write_bytes(b"wrong-size")
            old_paths[6].unlink()
            old_paths[7].unlink()

            client = FlakyShotGrid(
                schemas,
                records,
                downloads,
                values[6]["url"],
                values[7]["url"],
            )
            with mock.patch.object(media_sync.time, "sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    media_sync.materialize_latest_media(
                        output, client, client_factory=client.factory, max_workers=4
                    )

            recovered = media_sync.find_latest_snapshot(output)
            partials = incomplete_supplements(output, recovered.name)
            self.assertEqual(len(partials), 1)
            partial = partials[0]
            by_source = index_by_source(partial)
            self.assertEqual(
                by_source[("PublishedFile", 1, "path")]["acquisition"],
                "reused_interrupted",
            )
            self.assertEqual(client.attempts.get(values[1]["url"], 0), 0)
            for source_id in (2, 3, 4, 5, 6):
                self.assertEqual(
                    by_source[("PublishedFile", source_id, "path")]["status"],
                    "complete",
                )
            self.assertEqual(client.attempts[values[6]["url"]], 3)
            self.assertEqual(
                by_source[("PublishedFile", 7, "path")]["status"], "failed"
            )
            self.assertFalse((partial / "COMPLETED.json").exists())
            self.assertGreaterEqual(client.attempts[values[7]["url"]], 3)

            client.attempts.clear()
            client.permanent_failure = False
            with mock.patch.object(media_sync.time, "sleep", return_value=None):
                complete = media_sync.materialize_latest_media(
                    output, client, client_factory=client.factory, max_workers=4
                )
            self.assertEqual(set(client.attempts), {values[7]["url"]})
            self.assertEqual(client.attempts[values[7]["url"]], 1)
            self.assertEqual(complete.name, partial.name.removesuffix(".incomplete"))
            self.assertTrue((complete / "COMPLETED.json").is_file())
            self.assertTrue(media_sync.verify_media_supplement(complete)["ok"])
