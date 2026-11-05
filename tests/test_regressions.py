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
from wechat_decrypt_tool.modules import chat_reader, wechat_decrypt


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


class GroupTests(unittest.TestCase):
    def test_sanitize_folder_name(self):
        self.assertEqual(wxmoments.sanitize_folder_name("家人"), "家人")
        self.assertEqual(wxmoments.sanitize_folder_name("名字 . "), "名字")
        self.assertEqual(wxmoments.sanitize_folder_name("", "分组"), "分组")
        cleaned = wxmoments.sanitize_folder_name('a/b:c*?"<>|')
        self.assertTrue(cleaned)
        self.assertFalse(any(ch in cleaned for ch in '\\/:*?"<>|'))

    def test_groups_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moments_groups.json"
            self.assertEqual(wxmoments.load_groups(path)["groups"], [])
            data = wxmoments.load_groups(path)
            data["groups"].append({"id": "g1", "name": "家人", "account": "me",
                                   "members": [{"wxid": "u1", "name": "张三"}]})
            wxmoments.save_groups(path, data)
            self.assertEqual(wxmoments.load_groups(path)["groups"][0]["id"], "g1")
            # 损坏的文件按空处理，不抛错
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(wxmoments.load_groups(path)["groups"], [])

    def test_existing_image_is_reused(self):
        ts = datetime(2026, 1, 1, 12, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            post_dir = output / "figure" / wxmoments.timestamp_folder_name(ts)
            post_dir.mkdir(parents=True)
            (post_dir / "01.jpg").write_bytes(b"cached-image")
            post = {"createTime": int(ts.timestamp()), "username": "friend",
                    "contentDesc": "x", "media": [{"type": wxmoments.MEDIA_TYPE_IMAGE}]}
            account = wxmoments.AccountInfo("self", Path("synthetic"), Path("synthetic/db"))

            async def boom(*args, **kwargs):
                raise AssertionError("已有图片不应重新下载")

            with patch.object(wxmoments, "choose_image", boom):
                stats, _ = asyncio.run(wxmoments.export_markdown(
                    account, output, output, None, None, None, {}, False, {},
                    allow_download=True, posts_override=[post], reuse_existing=True))
            self.assertEqual(stats["reused_images"], 1)
            md = (output / "moments.md").read_text(encoding="utf-8")
            self.assertIn("figure/20260101_120000/01.jpg", md)

    def test_stable_post_dir_reused_between_runs(self):
        ts = datetime(2026, 1, 1, 12, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            post_dir = output / "figure" / wxmoments.timestamp_folder_name(ts)
            post_dir.mkdir(parents=True)
            (post_dir / "01.jpg").write_bytes(b"cached-image")
            account = wxmoments.AccountInfo("self", Path("synthetic"), Path("synthetic/db"))
            post = {"createTime": int(ts.timestamp()), "username": "friend",
                    "contentDesc": "x", "media": [{"type": wxmoments.MEDIA_TYPE_IMAGE}]}
            for _ in range(2):
                asyncio.run(wxmoments.export_markdown(
                    account, output, output, None, None, None, {}, False, {},
                    allow_download=False, posts_override=[post], reuse_existing=True))
            dirs = sorted(p.name for p in (output / "figure").iterdir() if p.is_dir())
            self.assertEqual(dirs, ["20260101_120000"])


class GroupExportTests(unittest.TestCase):
    """拆分 / 组合两种分组导出模式。"""

    def _run_group(self, split, directory):
        timestamp = datetime(2026, 1, 1, 12, 0, 0).timestamp()
        posts = [
            {"createTime": int(timestamp), "username": "u1", "contentDesc": "a", "media": []},
            {"createTime": int(timestamp) + 60, "username": "u1", "contentDesc": "b", "media": []},
            {"createTime": int(timestamp) + 120, "username": "u2", "contentDesc": "c", "media": []},
        ]
        tmp = Path(directory)
        account = wxmoments.AccountInfo("acct", Path("synthetic"), Path("synthetic/db"))
        state = {"account_info": account, "account_dir": tmp, "posts": posts,
                 "self_variants": set(), "contact_names": {"u1": "张三", "u2": "李四"}}
        group = {"id": "g1", "name": "家人", "folder": "家人", "split": split,
                 "keep_interactions": False,
                 "members": [{"wxid": "u1"}, {"wxid": "u2"}, {"wxid": "u3"}]}

        async def noop(*args, **kwargs):
            return None

        with patch.object(server, "_moments_config", lambda: {"output_root": str(tmp)}), \
                patch.object(server, "_MOMENTS", state), \
                patch.object(server, "_touch_group_export", lambda *args: None), \
                patch.object(wxmoments, "save_image_keys", noop), \
                patch.object(wxmoments, "render_pdf", lambda *args, **kwargs: None):
            job = Job(1)
            server._run_moments_group(job, account, tmp, group, "家人")
        return job, tmp / "家人"

    def test_split_writes_one_folder_per_friend(self):
        with tempfile.TemporaryDirectory() as directory:
            job, group_dir = self._run_group(True, directory)
            self.assertEqual(job.status, "done")
            self.assertTrue((group_dir / "张三" / "moments.md").exists())
            self.assertTrue((group_dir / "李四" / "moments.md").exists())
            self.assertFalse((group_dir / "moments.md").exists())
            self.assertFalse((group_dir / "王五").exists())

    def test_combined_writes_single_document(self):
        with tempfile.TemporaryDirectory() as directory:
            job, group_dir = self._run_group(False, directory)
            self.assertEqual(job.status, "done")
            self.assertTrue((group_dir / "moments.md").exists())
            self.assertFalse((group_dir / "张三").exists())
            md = (group_dir / "moments.md").read_text(encoding="utf-8")
            self.assertIn("张三", md)
            self.assertIn("李四", md)


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

    def test_nonfatal_wal_fallback_is_reported_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("sns.db", "contact.db"):
                (output / name).touch()
            result = {"status": "success", "account_results": {"self": {
                "output_dir": directory,
                "processed_files": [str(output / name) for name in ("sns.db", "contact.db")],
                "db_diagnostics": {name: {"wal_merge": {"attempted": True, "applied": False,
                                                        "reason": "busy"}}
                                   for name in ("sns.db", "contact.db")}}}}
            account = wxmoments.AccountInfo("self", output, output / "db")
            with patch.object(wechat_decrypt, "decrypt_wechat_databases", return_value=result) as decrypt:
                out, stale = wxmoments.decrypt_databases(account, "fixture-key")
            self.assertEqual(out, output)
            self.assertEqual(stale, "联系人、朋友圈的最新改动未读取")
            self.assertEqual(decrypt.call_args.kwargs["database_names"], {"sns.db", "contact.db"})

    def test_empty_wal_is_not_reported_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("sns.db", "contact.db"):
                (output / name).touch()
            result = {"status": "success", "account_results": {"self": {
                "output_dir": directory,
                "processed_files": [str(output / name) for name in ("sns.db", "contact.db")],
                "db_diagnostics": {name: {"wal_merge": {"attempted": True, "applied": False,
                                                        "reason": "no_frame"}}
                                   for name in ("sns.db", "contact.db")}}}}
            account = wxmoments.AccountInfo("self", output, output / "db")
            with patch.object(wechat_decrypt, "decrypt_wechat_databases", return_value=result):
                out, stale = wxmoments.decrypt_databases(account, "fixture-key")
            self.assertEqual(out, output)
            self.assertEqual(stale, "")

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
                self.assertEqual(wxmoments.decrypt_databases(account, "fixture-key")[0], output)
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
        logger.warning("internal detail", extra={"browser_notice": "请稍后刷新。"})
        events = diagnostics(cursor)["events"]
        self.assertIn("quick_check", events[0]["message"])
        self.assertEqual(events[0]["notice"], "")
        self.assertEqual(events[1]["notice"], "请稍后刷新。")

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
            (project / "src/launch.ps1").write_text(source, encoding="utf-8-sig")
            (project / "src/server.py").write_text(code, encoding="utf-8")
            env = dict(os.environ)
            env["WXMOMENTS_NO_BROWSER"] = "1"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-File", str(project / "src/launch.ps1")],
                                    capture_output=True, timeout=20, env=env)
            page = project / "runtime/startup-error.html"
            return result, page.read_text(encoding="utf-8") if page.exists() else ""

    def test_nonfatal_stderr_stays_out_of_terminal(self):
        result, page = self._launch("import sys\nprint('normal output')\nsys.stderr.write('nonfatal warning\\n')\n")
        # 服务进程立即退出（未监听端口）→ 判定启动失败；stderr 细节进错误页文件，不进终端。
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout + result.stderr, b"")
        self.assertIn("nonfatal warning", page)

    def test_startup_crash_creates_browser_error_page(self):
        result, page = self._launch("import sys\nsys.stderr.write('fixture failure\\n')\nsys.exit(3)\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout + result.stderr, b"")
        self.assertIn("fixture failure", page)
        self.assertIn("启动未完成", page)


class ChatPagingTests(unittest.TestCase):
    """跨分片倒序分页：每个分片各自推进游标，不能漏数据也不能重复。"""

    SESSION = "friend"

    def _build(self, root: Path, per_shard: int = 5) -> None:
        conn = sqlite3.connect(root / "session.db")
        conn.execute("create table SessionTable (username BLOB, unread_count INTEGER, "
                     "summary BLOB, last_timestamp INTEGER, sort_timestamp INTEGER)")
        conn.execute("insert into SessionTable values (?, 0, ?, ?, ?)",
                     (self.SESSION.encode(), b"hi", 0, 0))
        conn.commit()
        conn.close()
        contacts = sqlite3.connect(root / "contact.db")
        contacts.execute("create table contact (username BLOB, remark BLOB, nick_name BLOB, "
                         "alias BLOB, flag INTEGER)")
        contacts.execute("insert into contact values (?, x'', x'', x'', 0)",
                         (self.SESSION.encode(),))
        contacts.commit()
        contacts.close()
        table = chat_reader.message_table_for(self.SESSION)
        for shard in (0, 1):
            db = sqlite3.connect(root / f"message_{shard}.db")
            db.execute(f'create table "{table}" (local_id INTEGER PRIMARY KEY, '
                       'server_id INTEGER, local_type INTEGER, sort_seq INTEGER, '
                       'real_sender_id INTEGER, create_time INTEGER, '
                       'message_content BLOB, compress_content BLOB)')
            for index in range(1, per_shard + 1):
                # 两个分片的时间戳交错，模拟真实多库乱序
                moment = (index - 1) * 2 + 1 + shard
                db.execute(f'insert into "{table}" values (?, 0, 1, 0, 0, ?, ?, null)',
                           (index, moment, f"s{shard}-{index}".encode()))
            db.commit()
            db.close()

    def test_pages_cover_all_shards_without_gaps(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            store = chat_reader.ChatStore(root)
            try:
                seen, times, cursor = [], [], None
                while True:
                    page = store.messages(self.SESSION, cursor=cursor, limit=3)
                    seen.extend(item["message_id"] for item in page["messages"])
                    times.extend(item["create_time"] for item in page["messages"])
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
                    self.assertLess(len(times), 50)  # 防死循环
            finally:
                store.close()
            self.assertEqual(len(seen), 10)
            self.assertEqual(len(set(seen)), 10, "分页出现重复消息")
            self.assertEqual(times, sorted(times, reverse=True), "时间未严格倒序")
            self.assertEqual(sorted(times[-5:])[:5],
                             [1, 2, 3, 4, 5])

    def test_kind_filter_keeps_scanning_across_shards(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            table = chat_reader.message_table_for(self.SESSION)
            # 图片消息放在时间最老的一侧，只在翻到最后才会命中
            db = sqlite3.connect(root / "message_0.db")
            db.execute(f'insert into "{table}" values (99, 0, 3, 0, 0, 0, ?, null)',
                       (b'<msg><img /></msg>',))
            db.commit()
            db.close()
            store = chat_reader.ChatStore(root)
            try:
                hits, cursor, rounds = [], None, 0
                while rounds < 20:
                    rounds += 1
                    page = store.messages(self.SESSION, cursor=cursor, limit=2, kind="image")
                    hits.extend(page["messages"])
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
            finally:
                store.close()
            self.assertEqual(len(hits), 1, "类型筛选时漏掉了更早的命中项")
            self.assertEqual(hits[0]["kind"], "image")

    def _add_session(self, root: Path, username: str, *, last: int, sort: int,
                     rows: int = 1) -> None:
        conn = sqlite3.connect(root / "session.db")
        conn.execute("insert into SessionTable values (?, 0, ?, ?, ?)",
                     (username.encode(), b"msg", last, sort))
        conn.commit()
        conn.close()
        table = chat_reader.message_table_for(username)
        db = sqlite3.connect(root / "message_0.db")
        db.execute(f'create table "{table}" (local_id INTEGER PRIMARY KEY, '
                   'server_id INTEGER, local_type INTEGER, sort_seq INTEGER, '
                   'real_sender_id INTEGER, create_time INTEGER, '
                   'message_content BLOB, compress_content BLOB)')
        for index in range(1, rows + 1):
            db.execute(f'insert into "{table}" values (?, 0, 1, 0, 0, ?, ?, null)',
                       (index, last, b"text"))
        db.commit()
        db.close()

    def test_sessions_skip_official_accounts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            self._add_session(root, "gh_abcdef", last=99, sort=99)
            store = chat_reader.ChatStore(root)
            try:
                ids = [item["conversation_id"] for item in store.sessions()]
            finally:
                store.close()
            self.assertEqual(ids, [self.SESSION])

    def test_sessions_hide_conversations_without_messages(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            self._add_session(root, "empty_friend", last=99, sort=99, rows=0)
            self._add_session(root, "no_table_friend", last=98, sort=98, rows=0)
            conn = sqlite3.connect(root / "session.db")
            conn.execute("insert into SessionTable values (?, 0, ?, 0, 0)",
                         (b"shell_only", b""))
            conn.commit()
            conn.close()
            store = chat_reader.ChatStore(root)
            try:
                ids = [item["conversation_id"] for item in store.sessions()]
            finally:
                store.close()
            self.assertEqual(ids, [self.SESSION], "空会话不该出现在首页")

    def _fold(self, root: Path, username: str) -> None:
        """contact.flag 的 bit28 是微信 4.x 的「折叠该群聊」。"""
        conn = sqlite3.connect(root / "contact.db")
        conn.execute("insert or replace into contact values (?, x'', x'', x'', ?)",
                     (username.encode(), chat_reader.CONTACT_FLAG_FOLDED | 3))
        conn.commit()
        conn.close()

    def _pin(self, root: Path, username: str) -> None:
        """contact.flag 的 bit11 是微信 4.x 的置顶位，低两位是「是群 / 已保存」基础位。"""
        conn = sqlite3.connect(root / "contact.db")
        conn.execute("insert or replace into contact values (?, x'', x'', x'', ?)",
                     (username.encode(), chat_reader.CONTACT_FLAG_PINNED | 3))
        conn.commit()
        conn.close()

    def test_pinned_sessions_sort_first(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            # 置顶会话的消息时间可以比普通会话更早，仍要排在前面
            self._add_session(root, "pinned_old", last=50, sort=50)
            self._add_session(root, "pinned_newer", last=70, sort=70)
            self._add_session(root, "normal_latest", last=99, sort=99)
            self._pin(root, "pinned_old")
            self._pin(root, "pinned_newer")
            store = chat_reader.ChatStore(root)
            try:
                rows = store.sessions()
            finally:
                store.close()
            self.assertEqual([item["conversation_id"] for item in rows[:2]],
                             ["pinned_newer", "pinned_old"], "置顶组内要按时间倒序")
            self.assertEqual(rows[2]["conversation_id"], "normal_latest")
            self.assertTrue(rows[0]["pinned"] and rows[1]["pinned"])
            self.assertFalse(any(item["pinned"] for item in rows[2:]))

    def test_folded_flag_marks_group_chat(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            self._add_session(root, "folded_group", last=5, sort=5)
            self._fold(root, "folded_group")
            store = chat_reader.ChatStore(root)
            try:
                marks = {item["conversation_id"]: item["folded"]
                         for item in store.sessions()}
            finally:
                store.close()
            self.assertTrue(marks["folded_group"])
            self.assertFalse(marks[self.SESSION])

    def test_sort_timestamp_alone_is_not_pinned(self):
        # sort_timestamp 被抬高只说明会话排位变过，不等于置顶
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            self._add_session(root, "bumped", last=10, sort=10 ** 9)
            store = chat_reader.ChatStore(root)
            try:
                rows = store.sessions()
            finally:
                store.close()
            self.assertFalse(any(item["pinned"] for item in rows))


class ChatSessionFilterTests(unittest.TestCase):
    """折叠群聊默认不出现在会话列表里，勾选后才回来。"""

    def _state(self) -> dict:
        rows = [
            {"conversation_id": "normal_group", "display": "普通群", "kind": "group",
             "shards": 1, "pinned": False, "folded": False, "unread": 0,
             "last_time": 30, "sort_time": 30, "last_summary": "", "last_sender": ""},
            {"conversation_id": "folded_group", "display": "折叠群", "kind": "group",
             "shards": 1, "pinned": False, "folded": True, "unread": 0,
             "last_time": 20, "sort_time": 20, "last_summary": "", "last_sender": ""},
        ]
        return {"sessions": rows, "snapshot_id": "s1", "store": None,
                "account": {"account": "wxid_fixture"}, "shard_count": 1,
                "created_at": "", "warnings": [], "capabilities": []}

    def test_folded_sessions_hidden_by_default(self):
        chat = server.wxchat
        with patch.object(chat, "_STATE", self._state()):
            hidden = chat.sessions({})
            shown = chat.sessions({"show_folded": True})
        self.assertEqual([item["conversation_id"] for item in hidden["sessions"]],
                         ["normal_group"])
        self.assertEqual([item["conversation_id"] for item in shown["sessions"]],
                         ["normal_group", "folded_group"])
        self.assertTrue(shown["sessions"][1]["folded"])


class ChatListExportTests(unittest.TestCase):
    """好友 / 群聊列表导出：元数据解析与 CSV 单元格格式化。"""

    def _build(self, root: Path) -> None:
        conn = sqlite3.connect(root / "contact.db")
        conn.execute(
            "create table contact (id INTEGER PRIMARY KEY, username BLOB, local_type INTEGER, "
            "alias BLOB, encrypt_username BLOB, flag INTEGER, delete_flag INTEGER, "
            "verify_flag INTEGER, remark BLOB, remark_quan_pin BLOB, remark_pin_yin_initial BLOB, "
            "nick_name BLOB, pin_yin_initial BLOB, quan_pin BLOB, big_head_url BLOB, "
            "small_head_url BLOB, head_img_md5 BLOB, chat_room_notify INTEGER, "
            "is_in_chat_room INTEGER, description BLOB, extra_buffer BLOB, chat_room_type INTEGER)")
        conn.execute(
            "insert into contact values (1, ?, 1, x'', x'', 0, 0, 0, ?, x'', x'', ?, x'', x'', "
            "x'', x'', x'', 0, 1, x'', x'', 0)",
            (b"wxid_friend", "备注甲".encode(), "昵称甲".encode()))
        conn.execute(
            "insert into contact values (3, ?, 1, x'', x'', 0, 0, 0, ?, x'', x'', ?, x'', x'', "
            "x'', x'', x'', 0, 1, x'', x'', 0)",
            (b"wxid_owner", "群主备注".encode(), "群主昵称".encode()))
        conn.execute(
            "insert into contact values (2, ?, 1, x'', x'', ?, 0, 0, x'', x'', x'', ?, x'', x'', "
            "x'', x'', x'', 0, 1, x'', x'', 0)",
            (b"123@chatroom",
             wxmoments.CONTACT_FLAG_PINNED | wxmoments.CONTACT_FLAG_FOLDED | 2,
             "测试群".encode()))
        conn.execute("create table chat_room (id INTEGER, username BLOB, owner BLOB, ext_buffer BLOB)")
        conn.execute("insert into chat_room values (2, ?, ?, x'')",
                     (b"123@chatroom", b"wxid_owner"))
        conn.execute("create table chat_room_info_detail (room_id_ INTEGER, username_ BLOB, "
                     "announcement_ BLOB, chat_room_status_ INTEGER)")
        conn.execute("insert into chat_room_info_detail values (2, ?, ?, 0)",
                     (b"123@chatroom", "群公告内容".encode()))
        conn.execute("create table chatroom_member (room_id INTEGER, member_id INTEGER)")
        conn.executemany("insert into chatroom_member values (?, ?)",
                         [(2, 1), (2, 3)])
        conn.commit()
        conn.close()

    def test_group_details_include_metadata(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            groups = wxmoments.load_group_details(root)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["name"], "测试群")
        self.assertEqual(group["room_id"], "123@chatroom")
        self.assertTrue(group["pinned"])
        self.assertTrue(group["folded"])
        self.assertFalse(group["muted"])
        self.assertEqual(group["owner"], "群主备注")
        self.assertEqual(group["member_count"], 2)
        self.assertEqual(group["members"], ["备注甲", "群主备注"])
        self.assertEqual(group["announcement"], "群公告内容")

    def test_contact_details_carry_pin_and_mute_flags(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            self._build(root)
            contacts = wxmoments.load_contact_details(root)
        by_id = {c["wxid"]: c for c in contacts}
        # 群聊不会出现在好友里，但群主是普通联系人
        self.assertNotIn("123@chatroom", by_id)
        self.assertIn("wxid_friend", by_id)
        self.assertFalse(by_id["wxid_friend"]["pinned"])
        self.assertEqual(wxmoments.contact_flag_states(
            wxmoments.CONTACT_FLAG_PINNED | wxmoments.CONTACT_FLAG_MUTED),
            {"pinned": True, "folded": False, "muted": True})

    def test_cell_formatting(self):
        group = {"room_id": "1@chatroom", "name": "群名", "remark": "",
                 "members": ["甲", "乙"], "pinned": True, "folded": False, "in_room": False}
        self.assertEqual(server._chatlist_cell(group, "remark"), "")
        self.assertEqual(server._chatlist_cell(group, "members"), "甲、乙")
        self.assertEqual(server._chatlist_cell(group, "pinned"), "是")
        self.assertEqual(server._chatlist_cell(group, "folded"), "否")
        self.assertEqual(server._chatlist_cell(group, "in_room"), "否")

        contact = {"wxid": "wxid_a", "remark": "", "nickname": "昵称甲",
                   "country": "CN", "province": "Beijing", "city": "", "pinned": False}
        self.assertEqual(server._chatlist_cell(contact, "remark"), "昵称甲")
        self.assertEqual(server._chatlist_cell(contact, "region"), "CN Beijing")
        self.assertEqual(server._chatlist_cell(contact, "pinned"), "否")

    def test_column_sets_stay_separated(self):
        self.assertIn("members", server.CHATLIST_GROUP_COLUMNS)
        self.assertNotIn("members", server.CHATLIST_CONTACT_COLUMNS)
        self.assertIn("wechat_id", server.CHATLIST_CONTACT_COLUMNS)
        self.assertNotIn("wechat_id", server.CHATLIST_GROUP_COLUMNS)
        self.assertIn("pinned", server.CHATLIST_CONTACT_COLUMNS)
        self.assertIn("pinned", server.CHATLIST_GROUP_COLUMNS)


if __name__ == "__main__":
    unittest.main()
