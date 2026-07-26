import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as save_manager


class Issue20ScanSafetyTests(unittest.TestCase):
    def test_install_root_save_dat_uses_exact_non_recursive_spec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "Game"
            install_dir.mkdir()
            (install_dir / "save.dat").write_bytes(b"save")
            (install_dir / "game.exe").write_bytes(b"binary")

            specs = save_manager.infer_install_root_file_specs(str(install_dir))

            self.assertEqual(1, len(specs))
            self.assertEqual(["save.dat"], specs[0]["includes"])
            self.assertFalse(specs[0]["recursive"])
            files = [path.name for _, _, path, _ in save_manager.iter_save_spec_files(specs)]
            self.assertEqual(["save.dat"], files)

            safe_specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(install_dir),
                save_specs=specs,
                install_dir=str(install_dir),
                cfg={"games": []},
            )
            self.assertFalse(issue)
            self.assertEqual(specs, safe_specs)

            broad_specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(install_dir),
                save_paths=[str(install_dir)],
                install_dir=str(install_dir),
                cfg={"games": []},
            )
            self.assertEqual([], broad_specs)
            self.assertEqual("install-root-requires-precise-includes", issue)

            wildcard_specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(install_dir),
                save_specs=[{
                    "base": str(install_dir),
                    "includes": ["*"],
                    "recursive": False,
                }],
                install_dir=str(install_dir),
                cfg={"games": []},
            )
            self.assertEqual([], wildcard_specs)
            self.assertEqual("install-root-requires-precise-includes", issue)

    def test_detector_never_emits_unfiltered_install_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "Game"
            steam_dir = Path(temp_dir) / "Steam"
            library_dir = Path(temp_dir) / "Library"
            install_dir.mkdir()
            steam_dir.mkdir()
            library_dir.mkdir()
            (install_dir / "save.dat").write_bytes(b"save")
            (install_dir / "game.exe").write_bytes(b"binary")

            with (
                mock.patch.object(save_manager, "COMMON_SAVE_BASES", []),
                mock.patch.object(save_manager, "KNOWN_SAVE_PATHS", {}),
                mock.patch.object(save_manager, "parse_appinfo_ufs_entries", return_value=[]),
                mock.patch.object(save_manager, "get_remotecache_entries", return_value=[]),
                mock.patch.object(save_manager, "_detect_install_paths_from_registry", return_value=[]),
            ):
                candidates = save_manager.detect_save_candidates(
                    "10",
                    "Game",
                    str(install_dir),
                    str(steam_dir),
                    str(library_dir),
                    {"games": [], "steamdb_detection_enabled": False},
                )

            root_candidate = next(
                candidate for candidate in candidates
                if save_manager._scan_paths_equal(candidate["path"], str(install_dir))
            )
            self.assertEqual(["save.dat"], root_candidate["save_specs"][0]["includes"])
            self.assertFalse(root_candidate["save_specs"][0]["recursive"])

    def test_userdata_uid_zero_is_excluded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            steam_dir = Path(temp_dir) / "Steam"
            userdata = steam_dir / "userdata"
            for uid in ("0", "123"):
                remote = userdata / uid / "10" / "remote"
                remote.mkdir(parents=True)
                (remote / "save.sav").write_bytes(uid.encode("ascii"))

            self.assertEqual(["123"], save_manager.get_steam_user_ids(str(steam_dir)))
            with mock.patch.object(
                save_manager, "get_steam_userdata_roots", return_value=[str(userdata)]
            ):
                entries = save_manager.get_remotecache_entries("10", str(steam_dir))
            self.assertEqual(["123"], [entry["accountid"] for entry in entries])

            uid_zero_remote = userdata / "0" / "10" / "remote"
            specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(uid_zero_remote),
                save_paths=[str(uid_zero_remote)],
                steam_path=str(steam_dir),
                cfg={"games": []},
            )
            self.assertEqual([], specs)
            self.assertEqual("steam-userdata-uid-0", issue)

    def test_root_zero_rule_initializes_recursive_before_use(self):
        class FakeAppInfoCache:
            def __init__(self):
                self.stored = None

            def get_ufs_templates(self, _appid):
                return None

            def ensure_loaded(self, _steam_path):
                return None

            def get_savefiles(self, _appid):
                return [{
                    "root": "0",
                    "path": "",
                    "pattern": "*.sav",
                    "platforms": "Windows",
                }]

            def store_ufs_templates(self, _appid, templates):
                self.stored = templates

        fake_cache = FakeAppInfoCache()
        with mock.patch.object(save_manager, "_APPINFO_CACHE", fake_cache):
            entries = save_manager.parse_appinfo_ufs_entries("unused", "10")

        self.assertEqual(1, len(entries))
        self.assertEqual("0", entries[0]["root_id"])
        self.assertEqual(["*.sav"], entries[0]["includes"])
        self.assertFalse(entries[0]["recursive"])

    def test_root_zero_rule_attaches_to_real_remote_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            steam_dir = Path(temp_dir) / "Steam"
            remote_dir = steam_dir / "userdata" / "123" / "10" / "remote"
            remote_dir.mkdir(parents=True)
            (remote_dir / "slot.sav").write_bytes(b"save")
            root0_entry = {
                "template": "__root0__",
                "relative_path": "",
                "includes": ["*.sav"],
                "recursive": False,
                "root_id": "0",
            }
            remote_entry = {
                "accountid": "123",
                "app_root": str(remote_dir.parent),
                "remotecache": "",
                "remote_dir": str(remote_dir),
                "mtime": 0.0,
                "local_candidates": [],
            }
            with (
                mock.patch.object(save_manager, "COMMON_SAVE_BASES", []),
                mock.patch.object(save_manager, "KNOWN_SAVE_PATHS", {}),
                mock.patch.object(
                    save_manager, "parse_appinfo_ufs_entries", return_value=[root0_entry]
                ),
                mock.patch.object(
                    save_manager, "get_remotecache_entries", return_value=[remote_entry]
                ),
                mock.patch.object(save_manager, "_detect_install_paths_from_registry", return_value=[]),
            ):
                candidates = save_manager.detect_save_candidates(
                    "10",
                    "Game",
                    "",
                    str(steam_dir),
                    "",
                    {"games": [], "steamdb_detection_enabled": False},
                )

            self.assertFalse(any(candidate["path"] == "__root0__" for candidate in candidates))
            root0_candidate = next(
                candidate for candidate in candidates
                if save_manager._scan_paths_equal(candidate["path"], str(remote_dir))
                and "appinfo" in candidate["reasons"]
            )
            self.assertEqual(["*.sav"], root0_candidate["save_specs"][0]["includes"])
            self.assertFalse(root0_candidate["save_specs"][0]["recursive"])

    def test_scanned_same_appid_merges_but_manual_profile_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            path_a = base / "A"
            path_b = base / "B"
            path_c = base / "C"
            for path in (path_a, path_b, path_c):
                path.mkdir()

            existing = {
                "appid": "10",
                "name": "Game",
                "library_path": str(base),
                "scan_generated": True,
                "scan_confidence": "high",
                "scan_confirmed": True,
                "auto_backup": True,
                "sync_enabled": True,
            }
            save_manager.set_game_save_paths(existing, [str(path_a)])
            save_manager.ensure_game_storage_identity(existing)
            storage_key = existing["storage_key"]

            incoming = {"appid": "10", "name": "Game", "favorite": False}
            save_manager.set_game_save_paths(incoming, [str(path_b)])
            save_manager.apply_scanned_game_metadata(
                incoming,
                {"source": "system-search", "confidence": "low", "materialized": True},
                user_confirmed=True,
            )
            save_manager.ensure_game_storage_identity(incoming)

            games = [existing]
            action, stored = save_manager.merge_scanned_game_record(games, incoming)
            self.assertEqual("merged", action)
            self.assertEqual(1, len(games))
            self.assertEqual(storage_key, stored["storage_key"])
            self.assertEqual(
                {os.path.normcase(str(path_a)), os.path.normcase(str(path_b))},
                {
                    os.path.normcase(spec["base"])
                    for spec in save_manager.get_game_save_specs(stored)
                },
            )
            self.assertFalse(stored["auto_backup"])
            self.assertFalse(stored["sync_enabled"])

            parent_path = base / "Parent"
            child_path = parent_path / "Child"
            child_path.mkdir(parents=True)
            narrow = {
                "appid": "15",
                "name": "Narrow",
                "library_path": str(base),
                "scan_generated": True,
            }
            save_manager.set_game_save_paths(narrow, [str(child_path)])
            broad = {"appid": "15", "name": "Broad"}
            save_manager.set_game_save_paths(broad, [str(parent_path)])
            save_manager.apply_scanned_game_metadata(
                broad,
                {"source": "known-path", "confidence": "high", "materialized": True},
                user_confirmed=True,
            )
            overlap_games = [narrow]
            action, stored = save_manager.merge_scanned_game_record(overlap_games, broad)
            self.assertEqual("unchanged", action)
            self.assertEqual(
                [os.path.normcase(str(child_path))],
                [
                    os.path.normcase(spec["base"])
                    for spec in save_manager.get_game_save_specs(stored)
                ],
            )

            filtered_parent = {
                "appid": "16",
                "name": "Filtered Parent",
                "library_path": str(base),
                "scan_generated": True,
            }
            save_manager.set_game_save_specs(filtered_parent, [{
                "base": str(parent_path),
                "includes": ["root.sav"],
                "recursive": False,
            }])
            filtered_child = {"appid": "16", "name": "Filtered Child"}
            save_manager.set_game_save_specs(filtered_child, [{
                "base": str(child_path),
                "includes": ["slot.sav"],
                "recursive": False,
            }])
            save_manager.apply_scanned_game_metadata(
                filtered_child,
                {"source": "appinfo", "confidence": "high", "materialized": True},
                user_confirmed=True,
            )
            filtered_games = [filtered_parent]
            action, stored = save_manager.merge_scanned_game_record(
                filtered_games, filtered_child
            )
            self.assertEqual("merged", action)
            self.assertEqual(2, len(save_manager.get_game_save_specs(stored)))

            manual = {
                "appid": "20",
                "name": "Manual Profile",
                "manual_save_specs": True,
            }
            save_manager.set_game_save_paths(manual, [str(path_a)])
            scanned = {"appid": "20", "name": "Scanned Profile"}
            save_manager.set_game_save_paths(scanned, [str(path_c)])
            save_manager.apply_scanned_game_metadata(
                scanned,
                {"source": "appinfo", "confidence": "high", "materialized": True},
                user_confirmed=True,
            )
            save_manager.ensure_game_storage_identity(scanned)

            manual_games = [manual]
            action, _ = save_manager.merge_scanned_game_record(manual_games, scanned)
            self.assertEqual("added", action)
            self.assertEqual(2, len(manual_games))

    def test_different_appid_cannot_claim_same_scanned_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_path = Path(temp_dir) / "save"
            save_path.mkdir()
            existing = {"appid": "20", "name": "Other"}
            save_manager.set_game_save_paths(existing, [str(save_path)])

            specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(save_path),
                save_paths=[str(save_path)],
                cfg={"games": [existing]},
            )

            self.assertEqual([], specs)
            self.assertEqual("path-used-by-appid:20", issue)

            child_path = save_path / "profile"
            child_path.mkdir()
            specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(child_path),
                save_paths=[str(child_path)],
                cfg={"games": [existing]},
            )
            self.assertEqual([], specs)
            self.assertEqual("path-used-by-appid:20", issue)

            filtered_existing = {"appid": "30", "name": "Filtered"}
            save_manager.set_game_save_specs(filtered_existing, [{
                "base": str(save_path),
                "includes": ["root-only.sav"],
                "recursive": False,
            }])
            specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(child_path),
                save_paths=[str(child_path)],
                cfg={"games": [filtered_existing]},
            )
            self.assertFalse(issue)
            self.assertEqual(1, len(specs))

            shared_existing = {"appid": "40", "name": "Shared A"}
            save_manager.set_game_save_specs(shared_existing, [{
                "base": str(save_path),
                "includes": ["a.sav"],
                "recursive": False,
            }])
            specs, issue = save_manager.validate_scan_save_specs(
                appid="41",
                selected_path=str(save_path),
                save_specs=[{
                    "base": str(save_path),
                    "includes": ["b.sav"],
                    "recursive": False,
                }],
                cfg={"games": [shared_existing]},
            )
            self.assertFalse(issue)
            self.assertEqual(["b.sav"], specs[0]["includes"])

    def test_managed_storage_descendants_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sync_root = Path(temp_dir) / "sync"
            candidate = sync_root / "SteamSaveSync" / "appid_10"
            candidate.mkdir(parents=True)

            specs, issue = save_manager.validate_scan_save_specs(
                appid="10",
                selected_path=str(candidate),
                save_paths=[str(candidate)],
                cfg={"games": [], "sync_folder": str(sync_root)},
            )

            self.assertEqual([], specs)
            self.assertEqual("managed-storage-root", issue)

    def test_steam_library_backup_and_config_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            steam_root = Path(temp_dir) / "Steam"
            library_root = Path(temp_dir) / "Library"
            cfg = {"games": []}
            cases = [
                (steam_root, "broad-system-root"),
                (library_root, "broad-system-root"),
                (Path(save_manager.BACKUP_ROOT) / "Game", "managed-storage-root"),
                (Path(save_manager.CONFIG_DIR) / "scan-cache", "managed-storage-root"),
            ]
            for candidate, expected_issue in cases:
                with self.subTest(candidate=str(candidate)):
                    specs, issue = save_manager.validate_scan_save_specs(
                        appid="10",
                        selected_path=str(candidate),
                        save_paths=[str(candidate)],
                        steam_path=str(steam_root),
                        library_path=str(library_root),
                        cfg=cfg,
                    )
                    self.assertEqual([], specs)
                    self.assertEqual(expected_issue, issue)

    def test_parent_coverage_and_file_enumeration_preserve_filtered_specs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "parent"
            child = parent / "child"
            child.mkdir(parents=True)
            (child / "slot.sav").write_bytes(b"save")

            legacy_specs = save_manager._normalize_unique_save_specs([
                {"base": str(parent), "includes": [], "recursive": True},
                {"base": str(child), "includes": [], "recursive": True},
            ])
            self.assertEqual(2, len(legacy_specs))

            collapsed = save_manager._collapse_fully_covered_save_specs(legacy_specs)
            self.assertEqual(1, len(collapsed))
            self.assertTrue(save_manager._scan_paths_equal(collapsed[0]["base"], str(parent)))

            filtered = save_manager._normalize_unique_save_specs([
                {
                    "base": str(parent),
                    "includes": ["child/*.sav"],
                    "recursive": True,
                },
                {
                    "base": str(child),
                    "includes": ["*.sav"],
                    "recursive": False,
                },
            ])
            self.assertEqual(2, len(filtered))
            files = list(save_manager.iter_save_spec_files(filtered))
            self.assertEqual(1, len(files))
            self.assertEqual("slot.sav", files[0][2].name)

    def test_legacy_parent_child_backup_keeps_restore_group_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent = root / "parent"
            child = parent / "child"
            child.mkdir(parents=True)
            archive = root / "legacy.zip"
            specs = [
                {"base": str(parent), "includes": [], "recursive": True},
                {"base": str(child), "includes": [], "recursive": True},
            ]
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("__multi__/p1/root.sav", b"parent")
                zip_file.writestr("__multi__/p2/slot.sav", b"child")
            archive.with_suffix(".meta.json").write_text(
                json.dumps({"save_specs": specs}),
                encoding="utf-8",
            )

            save_manager.restore_backup(str(archive), [])

            self.assertEqual(b"parent", (parent / "root.sav").read_bytes())
            self.assertEqual(b"child", (child / "slot.sav").read_bytes())

    def test_low_confidence_or_unconfirmed_scan_disables_automation(self):
        low = {}
        save_manager.apply_scanned_game_metadata(
            low,
            {"source": "system-search", "confidence": "low", "materialized": True},
            user_confirmed=True,
        )
        self.assertFalse(low["auto_backup"])
        self.assertFalse(low["sync_enabled"])

        unconfirmed = {}
        save_manager.apply_scanned_game_metadata(
            unconfirmed,
            {"source": "appinfo", "confidence": "high", "materialized": True},
            user_confirmed=False,
        )
        self.assertFalse(unconfirmed["auto_backup"])
        self.assertFalse(unconfirmed["sync_enabled"])

        confirmed = {}
        save_manager.apply_scanned_game_metadata(
            confirmed,
            {"source": "appinfo", "confidence": "high", "materialized": True},
            user_confirmed=True,
        )
        self.assertTrue(confirmed["auto_backup"])
        self.assertTrue(confirmed["sync_enabled"])


if __name__ == "__main__":
    unittest.main()
