import os
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from python.annotation_preview.mask_labeler import serve


class ServeTest(unittest.TestCase):
    """标注器是 ES module，必须走 http 才能被浏览器加载，所以这一层要真起服务。"""

    def _serve(self, directory):
        httpd, url = serve.serve(directory=directory, port=0, open_browser=False)
        self.addCleanup(httpd.server_close)
        import threading
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

    def test_selftest_flag_opens_selftest_page(self):
        captured = {}

        def fake_serve(directory=serve.HERE, port=0, page="index.html", open_browser=True):
            captured["page"] = page
            captured["open_browser"] = open_browser
            raise SystemExit("stop before serve_forever")

        with patch.object(serve, "serve", fake_serve):
            with self.assertRaises(SystemExit):
                serve.main(["--selftest", "--no-browser"])
        self.assertEqual(captured["page"], "selftest.html")
        self.assertFalse(captured["open_browser"])

    def test_reports_a_busy_port_instead_of_tracebacking(self):
        def boom(**_kw):
            raise OSError(48, "Address already in use")

        with patch.object(serve, "serve", boom):
            with self.assertRaises(SystemExit) as ctx:
                serve.main(["--port", "8765"])
        self.assertIn("8765", str(ctx.exception))


class LabelerAssetsTest(unittest.TestCase):
    """标注器由 index.html + 三个 ES module 组成，缺一个页面就白屏。"""

    def test_all_assets_present(self):
        for name in ("index.html", "selftest.html", "app.js", "geometry.js", "draw.js"):
            with self.subTest(name=name):
                self.assertTrue(os.path.exists(os.path.join(serve.HERE, name)), name)

    def test_index_loads_app_as_module(self):
        with open(os.path.join(serve.HERE, "index.html")) as f:
            html = f.read()
        self.assertIn('type="module"', html)
        self.assertIn("./app.js", html)

    def test_app_imports_the_shared_modules(self):
        with open(os.path.join(serve.HERE, "app.js")) as f:
            js = f.read()
        self.assertIn("./geometry.js", js)
        self.assertIn("./draw.js", js)


if __name__ == "__main__":
    unittest.main()
