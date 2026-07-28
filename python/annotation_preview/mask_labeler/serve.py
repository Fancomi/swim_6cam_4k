#!/usr/bin/env python3
"""起一个本地 http 服务托管 mask 标注器，并打开浏览器。

标注器用了 ES module（import/export），浏览器对 file:// 下的模块按 CORS 拦截，
所以必须走 http。localhost 同时也是 File System Access API 认可的安全上下文，
Chrome / Edge 因此能直接把工程 json 写回所选目录。
"""
import argparse
import functools
import http.server
import os
import socketserver
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8765


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """静音的静态文件服务：只报错误，不逐条打印请求。"""

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        super().log_message(fmt, *args)


def serve(directory=HERE, port=DEFAULT_PORT, page="index.html", open_browser=True):
    """在 directory 上起服务，返回 (httpd, url)；调用方负责 serve_forever。"""
    handler = functools.partial(QuietHandler, directory=directory)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    url = "http://127.0.0.1:%d/%s" % (httpd.server_address[1], page)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    return httpd, url


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="监听端口（默认 %(default)s）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--selftest", action="store_true",
                    help="打开浏览器自测页 selftest.html 而不是标注器")
    args = ap.parse_args(argv)

    page = "selftest.html" if args.selftest else "index.html"
    try:
        httpd, url = serve(port=args.port, page=page, open_browser=not args.no_browser)
    except OSError as exc:
        raise SystemExit("端口 %d 起不来：%s（用 --port 换一个）" % (args.port, exc))

    print("mask 标注器: %s" % url)
    print("在页面里点「选择 snapshots 目录」，选中数据集的 snapshots/ 即可开始标注。")
    print("Ctrl-C 结束服务。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
