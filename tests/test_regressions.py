"""Regression checks using synthetic data only; no WeChat process or network."""
import asyncio
import contextlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import server
import wxmoments
from author_watch import AuthorWatch
from web_runtime import Job, diagnostics, start_job
from wechat_download import exporter, harvest, wechat, wxprofile
from wechat_decrypt_tool.modules import wechat_decrypt


def post(day, username="friend"):
    return {"createTime": int(datetime.fromisoformat(day).timestamp()),
            "username": username, "contentDesc": "测试内容", "media": []}


class CoverageTests(unittest.TestCase):
    def test_stale_latest_cache_is_reported(self):
        report = wxmoments.build_coverage_report(
            [post("2026-01-01")], [], None, None, reference_time=datetime(2026, 2, 1))
        self.assertEqual(report["local_stale_days"], 30)
        self.assertTrue(any("尚未同步" in warning for warning in report["warnings"]))

    def test_historical_end_date_does_not_claim_stale_recent_data(self):
        report = wxmoments.build_coverage_report(
            [post("2026-01-01")], [], None, datetime(2026, 1, 1), reference_time=datetime(2026, 2, 1))
        self.assertEqual(report["local_stale_days"], 0)
        self.assertFalse(any("尚未同步" in warning for warning in report["warnings"]))

    def test_threshold_counts_empty_calendar_days(self):
        report = wxmoments.build_coverage_report(
            [post("2026-01-01"), post("2026-01-23")], [], None, None)
        self.assertEqual(report["large_gaps"][0]["gap_days"], 21)
        self.assertTrue(report["warnings"])
        report = wxmoments.build_coverage_report(
            [post("2026-01-01"), post("2026-01-22")], [], None, None)
        self.assertEqual(report["large_gaps"], [])

    def test_missing_dates_and_empty_source_are_reported(self):
        posts = [{"createTime": "invalid"}, {"createTime": 10**50}, {"createTime": -1}]
        report = wxmoments.build_coverage_report(posts, [], None, None)
        self.assertEqual(report["local_posts_with_time"], 0)
        self.assertTrue(report["warnings"])

    def test_filtered_subset_does_not_create_false_gaps(self):
        posts = [post(f"2026-01-{day:02d}") for day in range(1, 32)]
        report = wxmoments.build_coverage_report(posts, [posts[0], posts[-1]], None, None)
        self.assertEqual(report["large_gap_count"], 0)
        self.assertEqual(report["exported_posts_with_time"], 2)

    def test_all_missing_periods_are_retained_for_log_view(self):
        dates = [datetime(2020, 1, 1) + timedelta(days=23 * index) for index in range(32)]
        posts = [post(day.isoformat()) for day in dates]
        report = wxmoments.build_coverage_report(posts, posts, None, None)
        self.assertEqual(report["large_gap_count"], 31)
        self.assertEqual(len(report["large_gaps"]), 31)

    def test_requested_range_outside_cache(self):
        report = wxmoments.build_coverage_report(
            [post("2026-01-15")], [], datetime(2026, 1, 1), datetime(2026, 2, 1))
        self.assertTrue(any("起始时间" in warning for warning in report["warnings"]))
        self.assertTrue(any("结束时间" in warning for warning in report["warnings"]))

    def test_export_coverage_uses_full_source(self):
        account = wxmoments.AccountInfo("self", Path("synthetic"), Path("synthetic/db"))
        posts = [post("2026-01-01"), post("2026-04-01")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            asyncio.run(wxmoments.export_markdown(
                account, Path(directory), output, None, None, None, {}, False, {},
                allow_download=False, posts_override=[posts[0]], coverage_posts=posts))
            report = json.loads((output / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual(report["local_posts_with_time"], 2)
            self.assertEqual(report["exported_posts_with_time"], 1)
            self.assertEqual(report["large_gap_count"], 1)


class DecryptScopeTests(unittest.TestCase):
    def test_optional_scope_skips_unrelated_databases(self):
        names = ["sns.db", "contact.db", "media_0.db", "message_0.db"]
        scan = {"status": "success", "account_databases": {
            "self": [{"name": name, "path": f"synthetic/{name}"} for name in names]}}
        for scope, expected in (({"sns.db", "contact.db"}, names[:2]), (None, names)):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as directory, \
                 patch.object(wechat_decrypt, "scan_account_databases_from_path", return_value=scan), \
                 patch.object(wechat_decrypt, "get_output_databases_dir", return_value=Path(directory)), \
                 patch.object(wechat_decrypt, "WeChatDatabaseDecryptor") as decryptor:
                decryptor.return_value.decrypt_database.return_value = True
                decryptor.return_value.last_result = {"success": True, "diagnostic_status": "ok"}
                result = wechat_decrypt.decrypt_wechat_databases(
                    "synthetic", "fixture-key", database_names=scope)
                actual = [Path(call.args[0]).name for call in
                          decryptor.return_value.decrypt_database.call_args_list]
                self.assertEqual(actual, expected)
                self.assertEqual(result["total_databases"], len(expected))

    def test_nonfatal_wal_fallback_is_logged_without_a_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("sns.db", "contact.db"):
                (output / name).touch()
            result = {"status": "success", "account_results": {"self": {
                "output_dir": directory,
                "processed_files": [str(output / name) for name in ("sns.db", "contact.db")],
                "db_diagnostics": {name: {"wal_merge": {"quick_check_ok": False, "applied": False}}
                                   for name in ("sns.db", "contact.db")}}}}
            account = wxmoments.AccountInfo("self", output, output / "db")
            cursor = diagnostics()["cursor"]
            with patch.object(wechat_decrypt, "decrypt_wechat_databases", return_value=result) as decrypt:
                self.assertEqual(wxmoments.decrypt_databases(account, "fixture-key"), output)
            self.assertEqual(decrypt.call_args.kwargs["database_names"], {"sns.db", "contact.db"})
            events = diagnostics(cursor)["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["notice"], "")
            self.assertIn("联系人、朋友圈", events[0]["message"])
            self.assertIn("可能未包含", events[0]["message"])

    def test_previous_outputs_cannot_hide_current_contact_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("sns.db", "contact.db"):
                (output / name).touch()
            result = {"status": "success", "account_results": {"self": {
                "output_dir": directory, "processed_files": [str(output / "sns.db")]}}}
            account = wxmoments.AccountInfo("self", output, output / "db")
            with patch.object(wechat_decrypt, "decrypt_wechat_databases", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "读取失败"):
                    wxmoments.decrypt_databases(account, "fixture-key")

    def test_other_account_outputs_are_never_used(self):
        result = {"status": "success", "account_results": {"other": {"output_dir": "synthetic"}}}
        account = wxmoments.AccountInfo("self", Path("a"), Path("b"))
        with patch.object(wechat_decrypt, "decrypt_wechat_databases", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "当前微信账号"):
                wxmoments.decrypt_databases(account, "fixture-key")

    def test_account_directory_suffix_uses_verified_source_path(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            account = wxmoments.AccountInfo("wxid_self_longsuffix", output, output / "db_storage")
            for name in ("sns.db", "contact.db"):
                (output / name).touch()
            detail = {"output_dir": directory,
                      "source_db_storage_path": str(account.db_storage_dir),
                      "processed_files": [str(output / name) for name in ("sns.db", "contact.db")]}
            result = {"status": "success", "account_results": {"wxid_self": detail}}
            with patch.object(wechat_decrypt, "decrypt_wechat_databases", return_value=result):
                self.assertEqual(wxmoments.decrypt_databases(account, "fixture-key"), output)
                detail["source_db_storage_path"] = str(output / "other_account/db_storage")
                with self.assertRaisesRegex(RuntimeError, "当前微信账号"):
                    wxmoments.decrypt_databases(account, "fixture-key")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.multiple(server, _MOMENTS={"account_info": None, "account_dir": None},
                                    _INIT_JOB=None, _MOMENTS_EXPORT_JOB=None)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_partial_initialization_does_not_mark_ready(self):
        account = wxmoments.AccountInfo("self", Path("synthetic"), Path("synthetic/db"))
        with patch.object(server, "_moments_config", return_value={"wechat_data_root": "."}), \
             patch.object(wxmoments, "find_account", return_value=account), \
             patch.object(server, "_moments_acquire_key", side_effect=RuntimeError("模拟失败")):
            job = Job(1)
            server._run_moments_init(job)
        self.assertEqual(job.status, "error")
        self.assertFalse(asyncio.run(server.api_moments_status())["ready"])

    def test_failed_reinitialization_preserves_previous_snapshot(self):
        previous = {"account_info": wxmoments.AccountInfo("old", Path("a"), Path("b")),
                    "account_dir": Path("old"), "posts": [post("2026-01-01")]}
        server._MOMENTS = previous
        with patch.object(server, "_moments_config", side_effect=ValueError("配置错误")):
            server._run_moments_init(Job(1))
        self.assertIs(server._MOMENTS, previous)
        self.assertTrue(asyncio.run(server.api_moments_status())["ready"])

    def test_concurrent_init_reuses_job(self):
        running = Job(1)
        server._INIT_JOB = running
        with patch.object(server, "start_job") as start:
            result = asyncio.run(server.api_moments_init({}))
        self.assertEqual(result["job"], running.id)
        start.assert_not_called()

    def test_export_and_init_do_not_share_mutable_download_report(self):
        server._MOMENTS_EXPORT_JOB = Job(1)
        self.assertFalse(asyncio.run(server.api_moments_init({}))["ok"])
        self.assertFalse(server._start_moments_export({})["ok"])

    def test_empty_friend_selection_and_self_aliases(self):
        server._MOMENTS = {"posts": [post("2026-01-01", "self_alias"), post("2026-01-02")],
                            "account_info": wxmoments.AccountInfo("self", Path("a"), Path("b")),
                            "self_variants": {"self_alias"}}
        self.assertEqual(server._moments_filtered_posts([], None, None), [])
        self.assertEqual(len(server._moments_filtered_posts(None, None, None)), 2)
        self.assertEqual(len(server._moments_filtered_posts(["self"], None, None)), 1)

    def test_invalid_filter_type(self):
        with self.assertRaises(ValueError):
            server._parse_moments_filters({"usernames": "friend"})
        with self.assertRaises(ValueError):
            server._parse_moments_filters({"start": "2026-02-01", "end": "2026-01-01"})

    def test_timeline_includes_global_coverage(self):
        server._MOMENTS = {"account_dir": Path("synthetic"),
                            "posts": [post("2026-01-01"), post("2026-04-01", "other")]}
        response = asyncio.run(server.api_moments_timeline({"usernames": ["friend"]}))
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["coverage"]["local_posts_with_time"], 2)
        self.assertEqual(response["coverage"]["large_gap_count"], 1)


class JobTests(unittest.TestCase):
    def test_internal_warning_remains_in_logs_without_becoming_a_notice(self):
        cursor = diagnostics()["cursor"]
        logger = logging.getLogger("regression")
        logger.warning("[decrypt.wal_merge] rejected after quick_check db=media_0.db")
        logger.warning("internal detail", extra={"browser_notice": "请稍后重新解密。"})
        events = diagnostics(cursor)["events"]
        self.assertIn("quick_check", events[0]["message"])
        self.assertEqual(events[0]["notice"], "")
        self.assertEqual(events[1]["notice"], "请稍后重新解密。")

    def test_background_logs_are_isolated_and_console_is_clean(self):
        barrier = threading.Barrier(2)
        finished = [threading.Event(), threading.Event()]
        def work(job, marker, event):
            barrier.wait(timeout=3)
            logging.getLogger("regression").warning(marker)
            job.status = "done"
            event.set()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            a = start_job(1, work, "job-alpha", finished[0])
            b = start_job(1, work, "job-beta", finished[1])
            self.assertTrue(all(event.wait(3) for event in finished))
        self.assertIn("job-alpha", " ".join(a.to_dict()["log"]))
        self.assertNotIn("job-beta", " ".join(a.to_dict()["log"]))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_diagnostics_cursor_and_bound(self):
        cursor = diagnostics()["cursor"]
        logger = logging.getLogger("regression")
        for index in range(410):
            logger.info("bounded-event-%s", index)
        response = diagnostics(cursor)
        self.assertEqual(len(response["events"]), 400)
        self.assertEqual(diagnostics(response["cursor"])["events"], [])

    def test_canceled_single_export_does_not_report_success(self):
        job = Job(1)
        job.canceled = True
        server._run_single(job, wechat.Article(url="synthetic", title="测试文章"))
        self.assertEqual(job.status, "canceled")
        self.assertEqual(job.results, [])

    def test_all_failed_batch_reports_error(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(server, "_batch_root", return_value=Path(directory)), \
             patch.object(server, "_load_article", side_effect=ValueError("模拟解析失败")):
            job = Job(1)
            server._run_batch(job, [{"url": "synthetic"}], "测试", "select")
        self.assertEqual(job.status, "error")
        self.assertIn("失败 1", job.message)


class WatchTests(unittest.TestCase):
    def test_idle_watch_expires_after_browser_disappears(self):
        watch = AuthorWatch("synthetic", lambda *args: None)
        watch.last_seen -= 91
        with patch.object(harvest, "harvest_articles") as find:
            watch._run()
        find.assert_not_called()
        self.assertFalse(watch.snapshot()["running"])

    def test_stop_ends_worker_and_allows_restart(self):
        called = threading.Event()
        def find(*args, **kwargs):
            called.set()
            return []
        watch = AuthorWatch("synthetic", lambda *args: None)
        with patch.object(harvest, "harvest_articles", side_effect=find):
            watch.start()
            self.assertTrue(called.wait(3))
            watch.stop()
            watch.thread.join(3)
            self.assertFalse(watch.thread.is_alive())
            watch.start()
            watch.stop()
            watch.thread.join(3)
            self.assertFalse(watch.thread.is_alive())

    def test_title_failure_is_bounded_and_visible(self):
        watch = AuthorWatch("synthetic", lambda *args: (_ for _ in ()).throw(ValueError("模拟失败")))
        watch.items["1_1"] = {"url": "synthetic", "mid": "1", "idx": "1"}
        for _ in range(3):
            watch._fill("1_1")
        snapshot = watch.snapshot()
        self.assertEqual(snapshot["failed_titles"], 1)
        self.assertEqual(snapshot["filling_left"], 0)


class CacheTests(unittest.TestCase):
    def test_shared_harvester_finds_chksm_without_legacy_login_key(self):
        url = "https://mp.weixin.qq.com/s?__biz=test&mid=1&idx=1&sn=abc"
        rows = [("History", 1, url + "&chksm=def")]
        with patch.object(wxprofile, "iter_cached_urls", return_value=iter(rows)):
            self.assertEqual(wechat.ensure_chksm(url), url + "&chksm=def")

    def test_page_cache_is_scoped_to_author(self):
        with patch.object(harvest, "_cache_sig", return_value=(("synthetic", 1),)), \
             patch.object(wxprofile, "find_profiles", return_value=[]), \
             patch.object(harvest, "_PAGE_MAP", {"1_1": {"title": "wrong author"}}), \
             patch.object(harvest, "_PAGE_MAP_KEY", ("author-one", (("synthetic", 1),))):
            self.assertEqual(harvest._scan_cache_pages("author-two"), {})

    def test_disk_only_articles_are_not_discarded(self):
        pages = {"1_1": {"url": "https://mp.weixin.qq.com/s?__biz=test&mid=1&idx=1&sn=abc&chksm=def",
                         "title": "仅磁盘缓存存在的文章", "ts": 1767225600}}
        with patch.object(wxprofile, "iter_cached_urls", return_value=iter([])), \
             patch.object(harvest, "_scan_cache_pages", return_value=pages):
            result = harvest.harvest_articles("test")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "仅磁盘缓存存在的文章")

    def test_cache_snapshots_are_unique_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "History"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute("create table urls (id integer, url text)")
                connection.execute("insert into urls values (1, 'https://example.test')")
                connection.commit()
            original = wxprofile._snapshot
            snapshots = []
            def track(path):
                result = original(path)
                snapshots.append(result)
                return result
            with patch.object(wxprofile, "_snapshot", side_effect=track):
                self.assertEqual(len(wxprofile._sqlite_rows(str(database))), 1)
                self.assertEqual(len(wxprofile._sqlite_rows(str(database))), 1)
            self.assertNotEqual(*snapshots)
            self.assertTrue(all(not Path(path).exists() for path in snapshots))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_view_exposes_all_periods_and_nonfatal_details(self):
        import httpx
        transport = httpx.ASGITransport(app=server.app)
        dates = [datetime(2020, 1, 1) + timedelta(days=23 * index) for index in range(32)]
        snapshot = {"account_dir": Path("synthetic"), "posts": [post(day.isoformat()) for day in dates]}
        marker = "nonfatal-log-view-fixture"
        logging.getLogger("regression").warning(marker)
        with patch.object(server, "_MOMENTS", snapshot):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                result = (await client.get("/api/logs")).json()
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["coverage"]["large_gaps"]), 31)
        self.assertTrue(any(marker in event["message"] for event in result["events"]))
        with patch.object(server, "_MOMENTS", {}):
            result = await server.api_logs()
        self.assertFalse(result["ready"])
        self.assertIsNone(result["coverage"])

    async def test_errors_and_coverage_are_available_over_http(self):
        import httpx
        transport = httpx.ASGITransport(app=server.app)
        snapshot = {"account_dir": Path("synthetic"),
                    "posts": [post("2026-01-01"), post("2026-04-01")]}
        with patch.object(server, "_MOMENTS", snapshot):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/moments/timeline", json={})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["coverage"]["large_gap_count"], 1)
                response = await client.post("/api/job/nonexistent/cancel", json={})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["msg"], "任务不存在")
                response = await client.get("/api/diagnostics")
                self.assertTrue(response.json()["ok"])

    async def test_unhandled_request_error_returns_browser_message(self):
        import httpx
        transport = httpx.ASGITransport(app=server.app)
        with patch.object(server, "diagnostics", side_effect=RuntimeError("模拟请求错误")):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 500)
        self.assertIn("模拟请求错误", response.json()["msg"])


@unittest.skipUnless(os.name == "nt", "Windows launcher")
class LauncherTests(unittest.TestCase):
    def _launch(self, code):
        import hashlib
        with tempfile.TemporaryDirectory(prefix="wxmoments-launch-test-") as directory:
            project = Path(directory)
            for folder in ("src", "runtime", "config"):
                (project / folder).mkdir()
            (project / "config/requirements.txt").write_bytes(b"")
            (project / "config/config.json").write_text("{}")
            version = subprocess.check_output([sys.executable, "-c", "import sys; print(sys.version)"], text=True).strip()
            fingerprint = hashlib.sha256(b"").hexdigest().upper() + ":" + version
            (project / "runtime/dependencies.sha256").write_text(fingerprint)
            source = (server.SRC_DIR / "launch.ps1").read_text(encoding="utf-8-sig")
            source = source.replace("$PythonExe = Join-Path $VenvDir 'Scripts\\python.exe'",
                                    "$PythonExe = '" + sys.executable.replace("'", "''") + "'")
            source = "function Invoke-RestMethod { throw 'offline fixture' }\nfunction Start-Process {}\n" + source
            (project / "src/launch.ps1").write_text(source, encoding="utf-8-sig")
            (project / "src/server.py").write_text(code, encoding="utf-8")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-File", str(project / "src/launch.ps1")], capture_output=True, timeout=20)
            page = project / "runtime/startup-error.html"
            return result, page.read_text(encoding="utf-8") if page.exists() else ""

    def test_nonfatal_stderr_stays_out_of_terminal(self):
        result, page = self._launch("import sys\nprint('normal output')\nsys.stderr.write('nonfatal warning\\n')\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, b"")
        self.assertEqual(page, "")

    def test_startup_crash_creates_browser_error_page(self):
        result, page = self._launch("import sys\nsys.stderr.write('fixture failure\\n')\nsys.exit(3)\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout + result.stderr, b"")
        self.assertIn("fixture failure", page)
        self.assertIn("启动未完成", page)


if __name__ == "__main__":
    unittest.main()
