import os
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from python.annotation_preview import labeler_server as L


class ServeTest(unittest.TestCase):
    """两个标注器都是 ES module，必须走 http 才能被浏览器加载，所以这一层要真起服务。"""

    def _serve(self, directory):
        httpd, url = L.serve(directory, port=0, open_browser=False)
        self.addCleanup(httpd.server_close)
        t = threading.Thread(target=httpd.handle_request, daemon=True)
        t.start()
        return httpd, url, t

    def test_serves_requested_page_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "index.html"), "w") as f:
                f.write("<h1>hi</h1>")
            _httpd, url, t = self._serve(tmp)
            body = urllib.request.urlopen(url, timeout=5).read().decode()
            t.join(timeout=5)
            self.assertIn("hi", body)

    def test_binds_loopback_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, url, _t = self._serve(tmp)
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)

    def test_url_names_the_requested_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, url, _t = self._serve(tmp)
            self.assertTrue(url.endswith("/index.html"), url)


class MainTest(unittest.TestCase):
    def _capture(self, argv):
        """拦在 serve 之前，取出 main 解析出的目录与页面。"""
        seen = {}

        def fake(directory, port=0, page="index.html", open_browser=True):
            seen.update(directory=directory, page=page, open_browser=open_browser)
            raise SystemExit("stop before serve_forever")

        with patch.object(L, "serve", fake):
            with self.assertRaises(SystemExit):
                L.main(argv)
        return seen

    def test_each_labeler_serves_its_own_directory(self):
        for name, (dirname, _title, _hint) in L.LABELERS.items():
            with self.subTest(labeler=name):
                seen = self._capture([name, "--no-browser"])
                self.assertEqual(os.path.basename(seen["directory"]), dirname)
                self.assertEqual(seen["page"], "index.html")
                self.assertFalse(seen["open_browser"])

    def test_selftest_flag_opens_the_selftest_page(self):
        seen = self._capture(["mask", "--selftest", "--no-browser"])
        self.assertEqual(seen["page"], "selftest.html")

    def test_rejects_an_unknown_labeler(self):
        with self.assertRaises(SystemExit):
            L.main(["nope", "--no-browser"])

    def test_reports_a_missing_page_instead_of_serving_a_404(self):
        # dot_labeler 没有自测页；应当直接报错，而不是起服务再让浏览器吃 404。
        with self.assertRaises(SystemExit) as ctx:
            L.main(["dot", "--selftest", "--no-browser"])
        self.assertIn("selftest.html", str(ctx.exception))

    def test_reports_a_busy_port_instead_of_tracebacking(self):
        def boom(*_a, **_kw):
            raise OSError(48, "Address already in use")

        with patch.object(L, "serve", boom):
            with self.assertRaises(SystemExit) as ctx:
                L.main(["mask", "--port", "8765"])
        self.assertIn("8765", str(ctx.exception))


class LabelerAssetsTest(unittest.TestCase):
    """每个标注器至少要有 index.html + app.js，缺一个就白屏。"""

    def _dir(self, name):
        return os.path.join(L.HERE, L.LABELERS[name][0])

    def test_every_labeler_has_its_entry_page_and_script(self):
        for name in L.LABELERS:
            for asset in ("index.html", "app.js"):
                with self.subTest(labeler=name, asset=asset):
                    self.assertTrue(os.path.exists(os.path.join(self._dir(name), asset)))

    def test_every_index_loads_its_app_as_a_module(self):
        for name in L.LABELERS:
            with self.subTest(labeler=name):
                html = open(os.path.join(self._dir(name), "index.html")).read()
                self.assertIn('type="module"', html)
                self.assertIn("./app.js", html)

    def test_mask_labeler_splits_out_geometry_and_draw(self):
        js = open(os.path.join(self._dir("mask"), "app.js")).read()
        self.assertIn("./geometry.js", js)
        self.assertIn("./draw.js", js)

    def test_mask_labeler_has_a_selftest_page(self):
        self.assertTrue(os.path.exists(os.path.join(self._dir("mask"), "selftest.html")))


if __name__ == "__main__":
    unittest.main()
