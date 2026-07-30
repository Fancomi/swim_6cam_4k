"""Static HTML review pages: one shell, one lazy loader.

Three generators (pose review, annotation QC, keypoint crops) each carried their
own doctype/head/CSS scaffold and two of them their own IntersectionObserver.
The page bodies genuinely differ — frames×models, candidate×three-up, a card
grid — so only the shell and the loader are shared here.
"""
import html
from pathlib import Path

escape = html.escape

# Rules all three pages agreed on before they were merged; each page appends its
# own layout below this.
BASE_CSS = """
body{background:#14161a;color:#dde3ea;font:13px/1.5 -apple-system,Helvetica,sans-serif;margin:24px}
h1{font-size:19px;margin:0 0 6px}
h2{font-size:15px;margin:28px 0 6px;border-bottom:1px solid #2c313a;padding-bottom:4px}
.meta{color:#8d97a5}
figure{margin:0}
figure img{display:block;width:var(--w);border:2px solid #2c313a;border-radius:4px;background:#000}
figcaption{color:#8d97a5;font-size:11px;text-align:center}
"""


def lazy_loader(margin_px):
    """Swap `data-src` to `src` as images near the viewport.

    Thousands of crops loaded at once locks the browser up; `loading="lazy"`
    alone still queues them all on Safari. `margin_px` is how far ahead to
    fetch, which each page picks from its own row height."""
    return ("const io=new IntersectionObserver((es)=>{for(const e of es){"
            "if(e.isIntersecting){const i=e.target;if(i.dataset.src){"
            "i.src=i.dataset.src;delete i.dataset.src;}io.unobserve(i);}}},"
            "{rootMargin:'%dpx'});"
            "document.querySelectorAll('img[data-src]').forEach(i=>io.observe(i));"
            % margin_px)


def write_page(path, title, body, css="", cell_width=280, lazy_margin=600,
               head="", script=""):
    """Write one self-contained page: `body` is a list of HTML fragments."""
    parts = [
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title><style>", BASE_CSS, css,
        ":root{--w:%dpx}" % cell_width, "</style>", head, "</head><body>",
        *body,
        "<script>%s\n%s</script></body></html>" % (lazy_loader(lazy_margin), script),
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
