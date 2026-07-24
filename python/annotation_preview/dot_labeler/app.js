// dot_labeler/app.js
// 物体打点标注器：一张图一份标注，只打点（顺序无关、无语义、无文本）。
// 选根目录 -> 递归载入所有图像 -> 左右切换 -> 内存常驻，无需反复保存。
// 保存1：逐图写回同名 .json（原地）。保存2：单个工程 json（含全部图，可再载入续标）。

const $ = (id) => document.getElementById(id);
const IMG_RE = /\.(jpe?g|png|bmp|webp|gif)$/i;
const PROJECT_FILE = 'dot_label_project.json';
const HIT_PX = 9;        // 命中/删除的屏幕半径
const NUDGE = 1;         // Shift+方向 微调步长（图像像素）

const cv = $('cv');
const ctx = cv.getContext('2d');

const S = {
  rootHandle: null,      // FileSystemDirectoryHandle（Chrome/Edge），否则 null
  items: [],             // [{ name, relPath, file, fileHandle, dirHandle }]
  idx: -1,
  bitmap: null, imgW: 0, imgH: 0,
  points: [],            // 当前图的点：[{x,y}]（图像像素坐标）
  loadedProject: null,   // relPath -> [{x,y}]（工程/逐图 json 载入的缓存）
  sel: -1,               // 选中点索引
  dirty: false,
  // 视图
  zoom: 1, cx: 0, cy: 0, // (cx,cy)=视口中心对应的图像坐标
  cursor: null,          // 最近的图像坐标（用于 Enter 加点）
};

// ---------- 视图变换：fit + zoom，中心锚定 ----------
function fitScale() {
  return Math.min(cv.width / S.imgW, cv.height / S.imgH);
}
function scale() { return fitScale() * S.zoom; }
function toScreen(x, y) {
  const s = scale();
  return [cv.width / 2 + (x - S.cx) * s, cv.height / 2 + (y - S.cy) * s];
}
function toImage(sx, sy) {
  const s = scale();
  return [S.cx + (sx - cv.width / 2) / s, S.cy + (sy - cv.height / 2) / s];
}
function resetView() {
  S.zoom = 1; S.cx = S.imgW / 2; S.cy = S.imgH / 2;
}
function clampView() {
  // 允许适度平移，但避免图像完全移出视口
  const s = scale();
  const halfW = cv.width / 2 / s, halfH = cv.height / 2 / s;
  S.cx = Math.max(-halfW + 20, Math.min(S.imgW + halfW - 20, S.cx));
  S.cy = Math.max(-halfH + 20, Math.min(S.imgH + halfH - 20, S.cy));
}
function evToImage(ev) {
  const r = cv.getBoundingClientRect();
  return toImage(ev.clientX - r.left, ev.clientY - r.top);
}
function hitRadiusImg() { return HIT_PX / scale(); }

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
  ctx.drawImage(S.bitmap, ox, oy, S.imgW * s, S.imgH * s);
  for (let i = 0; i < S.points.length; i++) {
    const p = S.points[i];
    const [x, y] = toScreen(p.x, p.y);
    const on = i === S.sel;
    ctx.beginPath(); ctx.arc(x, y, on ? 7 : 5, 0, Math.PI * 2);
    ctx.fillStyle = on ? '#ffd479' : 'rgba(90,200,255,.9)';
    ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = on ? '#fff' : '#0a2b45'; ctx.stroke();
    // 十字准星
    ctx.strokeStyle = on ? '#ffd479' : 'rgba(90,200,255,.5)'; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x - 10, y); ctx.lineTo(x + 10, y);
    ctx.moveTo(x, y - 10); ctx.lineTo(x, y + 10); ctx.stroke();
    ctx.fillStyle = '#cfe'; ctx.font = '11px monospace';
    ctx.fillText(String(i + 1), x + 8, y - 8);
  }
}

function syncUI() {
  $('pcount').textContent = String(S.points.length);
  $('dirty').classList.toggle('on', S.dirty);
  $('fname').textContent = S.idx >= 0
    ? `${S.idx + 1}/${S.items.length}  ${S.items[S.idx].relPath}` : '—';
  const list = $('plist'); list.innerHTML = '';
  S.points.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'row' + (i === S.sel ? ' on' : '');
    row.innerHTML = `<span>#${i + 1}</span><span>${p.x.toFixed(0)}, ${p.y.toFixed(0)}<b class="x">✕</b></span>`;
    row.addEventListener('click', (e) => {
      if (e.target.classList.contains('x')) { deletePoint(i); return; }
      S.sel = i; refresh();
    });
    list.appendChild(row);
  });
  const hud = $('hud');
  hud.textContent = S.bitmap
    ? `${S.imgW}×${S.imgH}  zoom ${S.zoom.toFixed(2)}  pts ${S.points.length}` : '';
}
function refresh() { render(); syncUI(); }
function setStatus(t) { $('status').textContent = t; }
function markDirty() { S.dirty = true; }

// ---------- 点的增删改查 ----------
function addPoint(x, y) {
  if (x < 0 || y < 0 || x > S.imgW || y > S.imgH) return;
  S.points.push({ x, y }); S.sel = S.points.length - 1; markDirty(); refresh();
}
function deletePoint(i) {
  if (i < 0 || i >= S.points.length) return;
  S.points.splice(i, 1);
  S.sel = Math.min(S.sel, S.points.length - 1);
  markDirty(); refresh();
}
function nudge(dx, dy) {
  if (S.sel < 0) return;
  const p = S.points[S.sel];
  p.x = Math.max(0, Math.min(S.imgW, p.x + dx));
  p.y = Math.max(0, Math.min(S.imgH, p.y + dy));
  markDirty(); refresh();
}
function hitPoint(ix, iy) {
  const r = hitRadiusImg();
  for (let i = S.points.length - 1; i >= 0; i--) {
    if (Math.hypot(S.points[i].x - ix, S.points[i].y - iy) <= r) return i;
  }
  return -1;
}

// ---------- 载入目录（递归） ----------
const fsSupported = () => 'showDirectoryPicker' in window;

async function pickRoot() {
  if (fsSupported()) {
    const handle = await window.showDirectoryPicker();
    S.rootHandle = handle;
    const items = [];
    await walkDir(handle, '', items);
    return items;
  }
  // 回退：<input webkitdirectory>
  return await pickViaInput();
}

async function walkDir(dirHandle, prefix, out) {
  for await (const [name, h] of dirHandle.entries()) {
    const rel = prefix ? `${prefix}/${name}` : name;
    if (h.kind === 'directory') await walkDir(h, rel, out);
    else if (IMG_RE.test(name)) out.push({ name, relPath: rel, fileHandle: h, dirHandle, file: null });
  }
}

function pickViaInput() {
  return new Promise((resolve, reject) => {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.webkitdirectory = true; inp.multiple = true;
    inp.addEventListener('change', () => {
      const items = [...inp.files]
        .filter((f) => IMG_RE.test(f.name))
        .map((f) => ({ name: f.name, relPath: f.webkitRelativePath || f.name, file: f, fileHandle: null, dirHandle: null }));
      resolve(items);
    });
    inp.addEventListener('cancel', () => reject({ name: 'AbortError' }));
    inp.click();
  });
}

async function openRoot() {
  let items;
  try { items = await pickRoot(); }
  catch (e) { if (e?.name !== 'AbortError') setStatus(String(e?.message || e)); return; }
  if (!items.length) { setStatus('目录内没有图像'); return; }
  items.sort((a, b) => a.relPath.localeCompare(b.relPath, undefined, { numeric: true }));
  S.items = items; S.loadedProject = {};
  // 目录内若有单文件工程，自动载入其标注缓存
  await tryLoadProjectFile();
  setStatus(`已载入 ${items.length} 张图像${S.rootHandle ? '（可原地保存）' : '（仅下载保存）'}`);
  await loadImage(0);
}

async function fileOf(item) {
  if (item.file) return item.file;
  return await item.fileHandle.getFile();
}

async function loadImage(i) {
  if (i < 0 || i >= S.items.length) return;
  // 切图前，把当前点缓存进内存（不写盘），实现「无需反复保存」
  if (S.idx >= 0) S.loadedProject[S.items[S.idx].relPath] = S.points.map((p) => ({ ...p }));
  S.idx = i; S.sel = -1;
  const item = S.items[i];
  const file = await fileOf(item);
  S.bitmap = await createImageBitmap(file);
  S.imgW = S.bitmap.width; S.imgH = S.bitmap.height;
  // 优先内存缓存，其次尝试同名逐图 json
  let pts = S.loadedProject[item.relPath];
  if (!pts) pts = await readSidecar(item);
  S.points = (pts || []).map((p) => ({ x: p.x, y: p.y }));
  S.dirty = false;
  resetView(); fitCanvas(); refresh();
}

function next() { if (S.idx < S.items.length - 1) loadImage(S.idx + 1); }
function prev() { if (S.idx > 0) loadImage(S.idx - 1); }

// ---------- 逐图 json（sidecar，与图像同名 .json，原地存于同一子目录） ----------
function sidecarPayload(item) {
  return {
    schema: 'dot-label/v1',
    image: item.relPath,
    width: S.imgW, height: S.imgH,
    points: S.points.map((p) => ({ x: +p.x.toFixed(2), y: +p.y.toFixed(2) })),
  };
}
function sidecarName(item) { return item.name.replace(IMG_RE, '') + '.json'; }

async function readSidecar(item) {
  try {
    if (item.dirHandle) {
      const fh = await item.dirHandle.getFileHandle(sidecarName(item));
      const obj = JSON.parse(await (await fh.getFile()).text());
      return obj.points || [];
    }
  } catch { /* 无 sidecar */ }
  return null;
}

// 保存1：原地保存当前图的逐图 json（写回图像所在子目录）
async function save1() {
  if (S.idx < 0) return;
  const item = S.items[S.idx];
  S.loadedProject[item.relPath] = S.points.map((p) => ({ ...p }));
  const text = JSON.stringify(sidecarPayload(item), null, 2);
  if (item.dirHandle) {
    try {
      const fh = await item.dirHandle.getFileHandle(sidecarName(item), { create: true });
      const w = await fh.createWritable();
      await w.write(text); await w.close();
      S.dirty = false; syncUI();
      setStatus(`已原地保存 ${sidecarName(item)}`);
      return;
    } catch (e) { setStatus(`⚠ 原地保存失败(${e?.name})，改为下载`); }
  }
  download(text, sidecarName(item));
  S.dirty = false; syncUI();
}

// 保存2：单文件工程 json（含所有图像的点），写回根目录；可再次载入续标
async function save2() {
  if (S.idx >= 0) S.loadedProject[S.items[S.idx].relPath] = S.points.map((p) => ({ ...p }));
  const doc = {
    schema: 'dot-label/project-v1',
    created: new Date().toISOString(),
    images: S.items.map((it) => ({
      image: it.relPath,
      points: (S.loadedProject[it.relPath] || []).map((p) => ({ x: +p.x.toFixed(2), y: +p.y.toFixed(2) })),
    })),
  };
  const text = JSON.stringify(doc, null, 2);
  if (S.rootHandle) {
    try {
      const fh = await S.rootHandle.getFileHandle(PROJECT_FILE, { create: true });
      const w = await fh.createWritable();
      await w.write(text); await w.close();
      S.dirty = false; syncUI();
      setStatus(`已保存工程 ${PROJECT_FILE}（${doc.images.length} 图）`);
      return;
    } catch (e) { setStatus(`⚠ 工程原地保存失败(${e?.name})，改为下载`); }
  }
  download(text, PROJECT_FILE);
  S.dirty = false; syncUI();
}

// 载入根目录内的工程文件，填充标注缓存（供续标）
async function tryLoadProjectFile() {
  if (!S.rootHandle) return;
  try {
    const fh = await S.rootHandle.getFileHandle(PROJECT_FILE);
    const obj = JSON.parse(await (await fh.getFile()).text());
    if (obj.schema === 'dot-label/project-v1') {
      for (const im of obj.images) S.loadedProject[im.image] = im.points || [];
      setStatus(`已载入工程 ${PROJECT_FILE}，可续标`);
    }
  } catch { /* 无工程文件 */ }
}

function download(text, name) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- 鼠标交互 ----------
let drag = null;   // { kind:'point', i } | { kind:'pan', x,y,cx,cy }

cv.addEventListener('pointerdown', (ev) => {
  if (!S.bitmap) return;
  const [ix, iy] = evToImage(ev);
  // Space 按住 或 中键 = 平移
  if (spaceDown || ev.button === 1) {
    drag = { kind: 'pan', x: ev.clientX, y: ev.clientY, cx: S.cx, cy: S.cy };
    cv.setPointerCapture(ev.pointerId); return;
  }
  const hit = hitPoint(ix, iy);
  if (ev.button === 2) {                      // 右键删点
    if (hit >= 0) deletePoint(hit);
    return;
  }
  if (ev.button !== 0) return;
  if (hit >= 0) {                             // 命中已有点 → 选中并可拖动
    S.sel = hit; drag = { kind: 'point', i: hit, moved: false };
    cv.setPointerCapture(ev.pointerId); refresh(); return;
  }
  addPoint(ix, iy);                           // 空白处左键 → 加点
});

cv.addEventListener('pointermove', (ev) => {
  const [ix, iy] = evToImage(ev);
  S.cursor = [ix, iy];
  if (!drag) return;
  if (drag.kind === 'pan') {
    const s = scale();
    S.cx = drag.cx - (ev.clientX - drag.x) / s;
    S.cy = drag.cy - (ev.clientY - drag.y) / s;
    clampView(); render(); return;
  }
  if (drag.kind === 'point') {
    const p = S.points[drag.i];
    p.x = Math.max(0, Math.min(S.imgW, ix));
    p.y = Math.max(0, Math.min(S.imgH, iy));
    drag.moved = true; markDirty(); render();
  }
});

cv.addEventListener('pointerup', (ev) => {
  if (drag) { try { cv.releasePointerCapture(ev.pointerId); } catch {} }
  drag = null; syncUI();
});
cv.addEventListener('contextmenu', (ev) => ev.preventDefault());

$('stage').addEventListener('wheel', (ev) => {
  if (!S.bitmap) return;
  ev.preventDefault();
  const r = cv.getBoundingClientRect();
  const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
  const [ax, ay] = toImage(sx, sy);            // 光标锚点
  const factor = Math.exp(-ev.deltaY * 0.0015);
  S.zoom = Math.max(1, Math.min(20, S.zoom * factor));
  // 使锚点保持在光标下
  const s = scale();
  S.cx = ax - (sx - cv.width / 2) / s;
  S.cy = ay - (sy - cv.height / 2) / s;
  clampView(); render();
}, { passive: false });

// ---------- 键盘交互 ----------
let spaceDown = false;
window.addEventListener('keydown', (ev) => {
  if (ev.target.tagName === 'INPUT') return;
  const k = ev.key;
  if (k === ' ') { spaceDown = true; cv.style.cursor = 'grab'; ev.preventDefault(); return; }
  if (!S.bitmap && k !== 'o') return;
  if (k === 'ArrowLeft' && !ev.shiftKey) { ev.preventDefault(); prev(); }
  else if (k === 'ArrowRight' && !ev.shiftKey) { ev.preventDefault(); next(); }
  else if (ev.shiftKey && k === 'ArrowLeft') { nudge(-NUDGE, 0); }
  else if (ev.shiftKey && k === 'ArrowRight') { nudge(NUDGE, 0); }
  else if (ev.shiftKey && k === 'ArrowUp') { nudge(0, -NUDGE); }
  else if (ev.shiftKey && k === 'ArrowDown') { nudge(0, NUDGE); }
  else if (k === 'Tab') { ev.preventDefault(); cycleSel(); }
  else if (k === 'Delete' || k === 'Backspace') { if (S.sel >= 0) deletePoint(S.sel); }
  else if (k === 'Enter') { if (S.cursor) addPoint(S.cursor[0], S.cursor[1]); }
  else if (k === 'f' || k === 'F') { resetView(); render(); }
  else if (k === '1') { save1(); }
  else if (k === '2') { save2(); }
});
window.addEventListener('keyup', (ev) => {
  if (ev.key === ' ') { spaceDown = false; cv.style.cursor = 'crosshair'; }
});
function cycleSel() {
  if (!S.points.length) return;
  S.sel = (S.sel + 1) % S.points.length; refresh();
}

// ---------- 按钮 ----------
$('open').addEventListener('click', () => openRoot());
$('prev').addEventListener('click', prev);
$('next').addEventListener('click', next);
$('save1').addEventListener('click', () => save1());
$('save2').addEventListener('click', () => save2());
$('clear').addEventListener('click', () => { S.points = []; S.sel = -1; markDirty(); refresh(); });
window.addEventListener('resize', () => { if (S.bitmap) { fitCanvas(); render(); } });

syncUI();





