// mask_labeler/app.js
// 保留区域 mask 标注器：选一台相机 -> 逐帧翻 -> 拖拽画胶囊笔画，标出该帧要保留的区域。
// 一笔 = 按下点到松开点的线段 + 两端半径 r 的圆（胶囊）。合成时只取被 mask 覆盖的像素。
// 保存为单个工程 json，供 python 端 merge_overhead 读取。
// 纯几何与命名解析放在 geometry.js，绘制放在 draw.js，两者都能被自测页直接导入。
import {
  capsuleArea,
  groupByCamera,
  hitStrokeIndex,
  sortCameras,
} from './geometry.js';
import { drawCursorGhost, drawPreview, drawStrokes } from './draw.js';

const $ = (id) => document.getElementById(id);
const IMG_RE = /\.(jpe?g|png|bmp|webp)$/i;
const PROJECT_FILE = 'mask_label_project.json';
const SCHEMA = 'mask-label/project-v1';

const cv = $('cv');
const ctx = cv.getContext('2d');

const S = {
  rootHandle: null,
  byCam: new Map(),       // cam -> [{ name, relPath, snapshotId, fileHandle, file }]
  cam: null,
  frames: [],             // 当前相机的帧
  idx: -1,
  bitmap: null, imgW: 0, imgH: 0,
  strokes: [],            // 当前帧的笔画：[{x1,y1,x2,y2,r}]（图像像素坐标）
  saved: {},              // relPath -> strokes[]（内存常驻，切帧不丢）
  radius: 40,
  alpha: 0.45,
  maskOnly: false,
  dirty: false,
  zoom: 1, cx: 0, cy: 0,
};

// ---------- 视图变换 ----------
function fitScale() { return Math.min(cv.width / S.imgW, cv.height / S.imgH); }
function scale() { return fitScale() * S.zoom; }
function toScreen(x, y) {
  const s = scale();
  return [cv.width / 2 + (x - S.cx) * s, cv.height / 2 + (y - S.cy) * s];
}
function toImage(sx, sy) {
  const s = scale();
  return [S.cx + (sx - cv.width / 2) / s, S.cy + (sy - cv.height / 2) / s];
}
function resetView() { S.zoom = 1; S.cx = S.imgW / 2; S.cy = S.imgH / 2; }
function clampView() {
  const s = scale();
  const halfW = cv.width / 2 / s, halfH = cv.height / 2 / s;
  S.cx = Math.max(-halfW + 20, Math.min(S.imgW + halfW - 20, S.cx));
  S.cy = Math.max(-halfH + 20, Math.min(S.imgH + halfH - 20, S.cy));
}
function evToImage(ev) {
  const r = cv.getBoundingClientRect();
  return toImage(ev.clientX - r.left, ev.clientY - r.top);
}

// ---------- 渲染 ----------
function fitCanvas() {
  const st = $('stage');
  cv.width = st.clientWidth; cv.height = st.clientHeight;
}

function render() {
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!S.bitmap) return;
  const s = scale();
  const [ox, oy] = toScreen(0, 0);

  if (S.maskOnly) {
    ctx.fillStyle = '#000';
    ctx.fillRect(ox, oy, S.imgW * s, S.imgH * s);
  } else {
    ctx.drawImage(S.bitmap, ox, oy, S.imgW * s, S.imgH * s);
  }

  // 已落定的笔画：绿色叠加表示「保留」；只看 mask 时画成纯白便于确认覆盖范围
  drawStrokes(ctx, S.strokes, toScreen, s,
              S.maskOnly ? '#fff' : `rgba(46,222,124,${S.alpha})`,
              S.maskOnly ? null : 'rgba(46,222,124,.9)');

  if (drawing) drawPreview(ctx, drawing, toScreen, s);
  else if (S.cursor) {
    const [x, y] = toScreen(S.cursor[0], S.cursor[1]);
    drawCursorGhost(ctx, x, y, S.radius * s);
  }
}

// ---------- UI 同步 ----------
function coveredFrac() {
  // 粗估覆盖比例（笔画重叠不去重），只用于给个手感，不参与导出
  if (!S.imgW || !S.strokes.length) return 0;
  let area = 0;
  for (const s of S.strokes) area += capsuleArea(s);
  return Math.min(1, area / (S.imgW * S.imgH));
}

function syncUI() {
  $('scount').textContent = String(S.strokes.length);
  $('dirty').classList.toggle('on', S.dirty);
  $('rval').textContent = `${S.radius} px`;
  $('aval').textContent = `${Math.round(S.alpha * 100)}%`;
  $('fname').textContent = S.idx >= 0
    ? `${S.idx + 1}/${S.frames.length}  ${S.frames[S.idx].snapshotId}` : '—';

  const list = $('flist'); list.innerHTML = '';
  S.frames.forEach((fr, i) => {
    const n = (i === S.idx ? S.strokes : S.saved[fr.relPath] || []).length;
    const row = document.createElement('div');
    row.className = 'row' + (i === S.idx ? ' on' : '') + (n ? ' has' : '');
    row.innerHTML = `<span>f${String(i + 1).padStart(2, '0')}</span><b>${n || ''}</b>`;
    row.addEventListener('click', () => loadFrame(i));
    list.appendChild(row);
  });

  const hud = $('hud');
  hud.textContent = S.bitmap
    ? `${S.cam}  f${String(S.idx + 1).padStart(2, '0')}/${S.frames.length}\n`
      + `${S.imgW}×${S.imgH}  zoom ${S.zoom.toFixed(2)}\n`
      + `笔画 ${S.strokes.length}  覆盖 ~${(coveredFrac() * 100).toFixed(1)}%`
      + (S.maskOnly ? '\n[只看 mask]' : '')
    : '';
}
function refresh() { render(); syncUI(); }
function setStatus(t) { $('status').textContent = t; }
function markDirty() { S.dirty = true; }

// ---------- 载入 snapshots 目录 ----------
const fsSupported = () => 'showDirectoryPicker' in window;

async function pickRoot() {
  if (fsSupported()) {
    const handle = await window.showDirectoryPicker();
    S.rootHandle = handle;
    const items = [];
    await walkDir(handle, '', items);
    return items;
  }
  return await pickViaInput();
}

async function walkDir(dirHandle, prefix, out) {
  for await (const [name, h] of dirHandle.entries()) {
    const rel = prefix ? `${prefix}/${name}` : name;
    if (h.kind === 'directory') await walkDir(h, rel, out);
    else if (IMG_RE.test(name)) out.push({ name, relPath: rel, fileHandle: h, file: null });
  }
}

function pickViaInput() {
  return new Promise((resolve, reject) => {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.webkitdirectory = true; inp.multiple = true;
    inp.addEventListener('change', () => {
      resolve([...inp.files].filter((f) => IMG_RE.test(f.name)).map((f) => ({
        name: f.name,
        relPath: f.webkitRelativePath || f.name,
        file: f,
        fileHandle: null,
      })));
    });
    inp.addEventListener('cancel', () => reject({ name: 'AbortError' }));
    inp.click();
  });
}

async function openRoot() {
  let items;
  try { items = await pickRoot(); }
  catch (e) { if (e?.name !== 'AbortError') setStatus(String(e?.message || e)); return; }
  const by = groupByCamera(items);
  if (!by.size) { setStatus('目录里没找到 …__<相机>.jpg 形式的图像'); return; }

  S.byCam = by; S.saved = {};
  const sel = $('cam');
  sel.innerHTML = '';
  const cams = sortCameras(by.keys());
  for (const cam of cams) {
    const opt = document.createElement('option');
    opt.value = cam;
    opt.textContent = `${cam}  (${by.get(cam).length} 帧)`;
    sel.appendChild(opt);
  }
  await tryLoadProjectFile();
  setStatus(`已载入 ${items.length} 张图 / ${by.size} 台相机`
    + (S.rootHandle ? '（可原地保存）' : '（仅下载保存）'));
  await selectCamera(cams[0]);
}

async function selectCamera(cam) {
  stashCurrent();
  S.cam = cam;
  S.frames = S.byCam.get(cam) || [];
  S.idx = -1;
  $('cam').value = cam;
  if (S.frames.length) await loadFrame(0);
  else { S.bitmap = null; refresh(); }
}

// ---------- 帧切换 ----------
function stashCurrent() {
  if (S.idx >= 0 && S.frames[S.idx]) {
    S.saved[S.frames[S.idx].relPath] = S.strokes.map((s) => ({ ...s }));
  }
}

async function fileOf(item) {
  if (item.file) return item.file;
  return await item.fileHandle.getFile();
}

async function loadFrame(i) {
  if (i < 0 || i >= S.frames.length) return;
  stashCurrent();
  S.idx = i;
  const item = S.frames[i];
  const file = await fileOf(item);
  S.bitmap = await createImageBitmap(file);
  S.imgW = S.bitmap.width; S.imgH = S.bitmap.height;
  S.strokes = (S.saved[item.relPath] || []).map((s) => ({ ...s }));
  drawing = null;
  resetView(); fitCanvas(); refresh();
}

function next() { if (S.idx < S.frames.length - 1) loadFrame(S.idx + 1); }
function prev() { if (S.idx > 0) loadFrame(S.idx - 1); }

// ---------- 保存 ----------
function projectDoc() {
  stashCurrent();
  const cams = {};
  for (const [cam, list] of S.byCam) {
    const frames = list
      .map((it, i) => ({
        frame_index: i + 1,
        snapshot_id: it.snapshotId,
        image: it.relPath,
        strokes: (S.saved[it.relPath] || []).map((s) => ({
          x1: +s.x1.toFixed(1), y1: +s.y1.toFixed(1),
          x2: +s.x2.toFixed(1), y2: +s.y2.toFixed(1),
          r: +(+s.r).toFixed(1),
        })),
      }))
      .filter((f) => f.strokes.length);
    if (frames.length) cams[cam] = frames;
  }
  return {
    schema: SCHEMA,
    created: new Date().toISOString(),
    note: '每笔是一个胶囊（线段两端各一个半径 r 的圆）；被覆盖的像素是该帧要保留的部分。坐标为图像原始像素。',
    cameras: cams,
  };
}

async function save() {
  const doc = projectDoc();
  const text = JSON.stringify(doc, null, 2);
  const n = Object.values(doc.cameras).reduce((a, f) => a + f.length, 0);
  if (S.rootHandle) {
    try {
      const fh = await S.rootHandle.getFileHandle(PROJECT_FILE, { create: true });
      const w = await fh.createWritable();
      await w.write(text); await w.close();
      S.dirty = false;
      $('saveinfo').textContent = `已写回 ${PROJECT_FILE}（${n} 个已标帧）`;
      setStatus(`已保存 ${PROJECT_FILE}`);
      syncUI();
      return;
    } catch (e) { setStatus(`⚠ 原地保存失败(${e?.name})，改为下载`); }
  }
  download(text, PROJECT_FILE);
  S.dirty = false;
  $('saveinfo').textContent = `已下载 ${PROJECT_FILE}（${n} 个已标帧）`;
  syncUI();
}

async function tryLoadProjectFile() {
  if (!S.rootHandle) return;
  try {
    const fh = await S.rootHandle.getFileHandle(PROJECT_FILE);
    const obj = JSON.parse(await (await fh.getFile()).text());
    if (obj.schema !== SCHEMA) return;
    let n = 0;
    for (const frames of Object.values(obj.cameras || {})) {
      for (const f of frames) {
        if (!f.image) continue;
        S.saved[f.image] = (f.strokes || []).map((s) => ({ ...s }));
        n++;
      }
    }
    $('saveinfo').textContent = `已载入 ${PROJECT_FILE}（${n} 个已标帧）`;
    setStatus(`已载入既有工程 ${PROJECT_FILE}，可续标`);
  } catch { /* 无工程文件 */ }
}

function download(text, name) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- 鼠标交互 ----------
let drawing = null;   // { x1,y1,x2,y2,r } 未松手的那一笔
let pan = null;

cv.addEventListener('pointerdown', (ev) => {
  if (!S.bitmap) return;
  const [ix, iy] = evToImage(ev);

  if (spaceDown || ev.button === 1) {
    pan = { x: ev.clientX, y: ev.clientY, cx: S.cx, cy: S.cy };
    cv.setPointerCapture(ev.pointerId);
    return;
  }
  if (ev.button === 2) {                       // 右键擦掉命中的那一笔
    const hit = hitStrokeIndex(S.strokes, ix, iy);
    if (hit >= 0) { S.strokes.splice(hit, 1); markDirty(); refresh(); }
    return;
  }
  if (ev.button !== 0) return;
  drawing = { x1: ix, y1: iy, x2: ix, y2: iy, r: S.radius };
  cv.setPointerCapture(ev.pointerId);
  render();
});

cv.addEventListener('pointermove', (ev) => {
  const [ix, iy] = evToImage(ev);
  S.cursor = [ix, iy];
  if (pan) {
    const s = scale();
    S.cx = pan.cx - (ev.clientX - pan.x) / s;
    S.cy = pan.cy - (ev.clientY - pan.y) / s;
    clampView(); render(); return;
  }
  if (drawing) { drawing.x2 = ix; drawing.y2 = iy; render(); return; }
  render();
});

cv.addEventListener('pointerup', (ev) => {
  try { cv.releasePointerCapture(ev.pointerId); } catch { /* 已释放 */ }
  if (pan) { pan = null; return; }
  if (!drawing) return;
  S.strokes.push(drawing);                     // 松手落定
  drawing = null;
  markDirty(); refresh();
});

cv.addEventListener('pointerleave', () => { S.cursor = null; if (!drawing) render(); });
cv.addEventListener('contextmenu', (ev) => ev.preventDefault());

$('stage').addEventListener('wheel', (ev) => {
  if (!S.bitmap) return;
  ev.preventDefault();
  const r = cv.getBoundingClientRect();
  const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
  const [ax, ay] = toImage(sx, sy);
  S.zoom = Math.max(1, Math.min(20, S.zoom * Math.exp(-ev.deltaY * 0.0015)));
  const s = scale();
  S.cx = ax - (sx - cv.width / 2) / s;
  S.cy = ay - (sy - cv.height / 2) / s;
  clampView(); refresh();
}, { passive: false });

// ---------- 键盘 ----------
let spaceDown = false;
window.addEventListener('keydown', (ev) => {
  if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'SELECT') return;
  const k = ev.key;
  if (k === ' ') { spaceDown = true; cv.style.cursor = 'grab'; ev.preventDefault(); return; }
  if ((ev.ctrlKey || ev.metaKey) && (k === 'z' || k === 'Z')) { ev.preventDefault(); undo(); return; }
  if (!S.bitmap) return;
  if (k === 'ArrowLeft') { ev.preventDefault(); prev(); }
  else if (k === 'ArrowRight') { ev.preventDefault(); next(); }
  else if (k === '[') { setRadius(S.radius - Math.max(1, Math.round(S.radius * 0.2))); }
  else if (k === ']') { setRadius(S.radius + Math.max(1, Math.round(S.radius * 0.2))); }
  else if (k === 'm' || k === 'M') { toggleMaskOnly(); }
  else if (k === 'f' || k === 'F') { resetView(); refresh(); }
  else if (k === 'Escape') { if (drawing) { drawing = null; render(); } }
  else if (k === 's' || k === 'S') { save(); }
});
window.addEventListener('keyup', (ev) => {
  if (ev.key === ' ') { spaceDown = false; cv.style.cursor = 'crosshair'; }
});

// ---------- 操作 ----------
function undo() {
  if (!S.strokes.length) return;
  S.strokes.pop(); markDirty(); refresh();
}
function setRadius(v) {
  S.radius = Math.max(2, Math.min(400, Math.round(v)));
  $('radius').value = String(S.radius);
  refresh();
}
function toggleMaskOnly() {
  S.maskOnly = !S.maskOnly;
  $('viewmask').classList.toggle('primary', S.maskOnly);
  refresh();
}

// ---------- 按钮绑定 ----------
$('open').addEventListener('click', () => openRoot());
$('cam').addEventListener('change', (ev) => selectCamera(ev.target.value));
$('prev').addEventListener('click', prev);
$('next').addEventListener('click', next);
$('undo').addEventListener('click', undo);
$('clear').addEventListener('click', () => { S.strokes = []; markDirty(); refresh(); });
$('save').addEventListener('click', () => save());
$('viewmask').addEventListener('click', toggleMaskOnly);
$('radius').addEventListener('input', (ev) => setRadius(+ev.target.value));
$('alpha').addEventListener('input', (ev) => { S.alpha = +ev.target.value / 100; refresh(); });
window.addEventListener('resize', () => { if (S.bitmap) { fitCanvas(); render(); } });

syncUI();



