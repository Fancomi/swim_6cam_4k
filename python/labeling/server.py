#!/usr/bin/env python3
"""起本地 http 服务托管浏览器标注器，并打开浏览器。

两个标注器都用 ES module，而浏览器对 file:// 下的模块按 CORS 拦截（origin 为
null），双击 html 打开必定白屏，所以一律走 http。localhost 同时是 File System
Access API 认可的安全上下文，Chrome / Edge 因此能把工程 json 直接写回所选目录。

服务根是 python/labeling/ 而不是某个标注器的子目录：两个标注器共用
labeler_core.js，它在上一层，而静态服务不会提供服务根之外的文件。
"""
import argparse
import functools
import http.server
import json
import os
import socketserver
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8765

# 每个标注器：目录名 + 标题 + 首页提示语。自测页统一叫 selftest.html。
LABELERS = {
    "mask": ("mask_labeler", "保留区域 mask 标注器",
             "点「选择 snapshots 目录」选中数据集的 snapshots/，逐帧拖拽画保留区域。"),
    "dot": ("dot_labeler", "物体打点标注器",
            "点「选择根目录」选中 object-frames/，逐图打点。"),
}


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """静音的静态文件服务：只报错误，不逐条打印请求。"""

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        super().log_message(fmt, *args)


class MaskMergeHandler(QuietHandler):
    """在静态服务之上加一个 POST /mask-merge：收到工程 JSON + 快照目录，合成出图。

    mask_labeler 在浏览器里选目录（File System Access API 只给浏览器内句柄），
    服务器拿不到那个目录的磁盘路径——所以快照目录**可选**：前端能拿到绝对路径
    就随请求发来（snapshots_dir），拿不到（留空）时后端回退到 frames 的默认
    数据根（<数据集根>/<日期>/snapshots）。产物写到数据集
    <数据集根>/<日期>/object-frames/<相机>_mask_*.png，浏览器经
    /mask-merge-result 端点读回显示。路由规则：GET/HEAD 走静态文件，
    POST 只认 /mask-merge。
    """

    def do_POST(self):
        if self.path != "/mask-merge":
            self.send_error(404, "unknown POST endpoint")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, TypeError):
            self.send_error(400, "bad json body")
            return
        project = payload.get("project")
        snapshots_dir = payload.get("snapshots_dir") or ""
        camera = payload.get("camera")
        if not (isinstance(project, dict) and camera):
            self.send_error(400, "need project, camera")
            return
        try:
            from python.labeling import frames as M
            # 前端没传绝对路径时回退到默认数据根（frames 的 DATASET/DATE），
            # 这样「选过目录但填不了路径」也能合成。
            if not snapshots_dir:
                snapshots_dir = str(M.DATASET / M.DATE / "snapshots")
            # 产物写到数据集的 object-frames（与其他链路同根），不落仓库本地。
            # 浏览器展示走 /mask-merge-result 端点读文件返回。
            out_dir = M.DATASET / str(M.DATE) / "object-frames"
            paths = M.merge_camera(camera, project.get("cameras", {}).get(camera, []),
                                   snapshots_dir, out_dir)
            files = [os.path.basename(p) for p in paths]
        except Exception as exc:               # 合成失败要反馈给前端，不能静默
            self.send_error(500, "merge failed: %s" % exc)
            return
        body = json.dumps({"files": files},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 合成产物在数据集 object-frames（服务根之外），静态服务访问不到；
        # 这里加一个读文件端点给浏览器展示：/mask-merge-result?f=<文件名>。
        if self.path.startswith("/mask-merge-result?"):
            from urllib.parse import parse_qs
            from python.labeling import frames as M
            qs = parse_qs(self.path.split("?", 1)[1])
            name = (qs.get("f") or [""])[0]
            out_dir = M.DATASET / str(M.DATE) / "object-frames"
            target = (out_dir / name).resolve()
            # 只允许读本目录下的文件，防目录穿越。
            if not target.is_file() or out_dir.resolve() not in target.parents:
                self.send_error(404, "no such result file")
                return
            try:
                data = target.read_bytes()
            except OSError:
                self.send_error(404, "cannot read result file")
                return
            ctype = "image/png" if name.endswith(".png") else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def do_HEAD(self):
        super().do_HEAD()


def serve(directory, port=DEFAULT_PORT, page="index.html", open_browser=True,
          handler_cls=QuietHandler):
    """在 directory 上起服务，返回 (httpd, url)；调用方负责 serve_forever。

    handler_cls 可换成带 POST 端点的子类；mask 标注器用它来一键合成。
    """
    handler = functools.partial(handler_cls, directory=str(directory))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    url = "http://127.0.0.1:%d/%s" % (httpd.server_address[1], page)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    return httpd, url


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("labeler", choices=sorted(LABELERS),
                    help="要打开哪个标注器")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="监听端口（默认 %(default)s）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--selftest", action="store_true",
                    help="打开该标注器的浏览器自测页而不是标注器本身")
    args = ap.parse_args(argv)

    dirname, title, hint = LABELERS[args.labeler]
    page = f"{dirname}/{'selftest.html' if args.selftest else 'index.html'}"
    if not (HERE / page).exists():
        raise SystemExit("%s 没有 %s" % (dirname, Path(page).name))

    try:
        httpd, url = serve(HERE, port=args.port, page=page,
                           open_browser=not args.no_browser,
                           handler_cls=MaskMergeHandler if args.labeler == "mask"
                           else QuietHandler)
    except OSError as exc:
        raise SystemExit("端口 %d 起不来：%s（用 --port 换一个）" % (args.port, exc))

    print("%s: %s" % (title, url))
    if not args.selftest:
        print(hint)
    print("Ctrl-C 结束服务。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
