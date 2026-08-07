// mask_labeler/app.js
// 保留区域 mask 标注器：选一台相机 -> 逐帧翻 -> 拖拽画胶囊笔画，标出该帧要保留的区域。
// 一笔 = 按下点到松开点的线段 + 两端半径 r 的圆（胶囊）。合成时只取被 mask 覆盖的像素。
// 保存为单个工程 json，供 python 端 merge_overhead 读取。
// 纯几何与命名解析放在 geometry.js，绘制放在 draw.js，两者都能被自测页直接导入。
// 视图变换、目录选取、保存下载在 ../labeler_core.js，与 dot 标注器共用。
import {
  capsuleArea,
  groupByCamera,
  hitStrokeIndex,
  sortCameras,
} from './geometry.js';
import { drawCursorGhost, drawPreview, drawStrokes } from './draw.js';
import {
  clampView, eventToImage, fileOf, fitCanvas as fitCanvasTo, pickRoot, readJson,
  resetView as resetViewOf, scaleOf, toImage as toImageOf,
  toScreen as toScreenOf, writeJson, zoomAt,
} from '../labeler_core.js';

const $ = (id) => document.getElementById(id);
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

// ---------- 视图变换（core 的薄包装，省掉每处传 cv/S） ----------
const scale = () => scaleOf(cv, S);
const toScreen = (x, y) => toScreenOf(cv, S, x, y);
const toImage = (sx, sy) => toImageOf(cv, S, sx, sy);
const resetView = () => resetViewOf(S);
const evToImage = (ev) => eventToImage(cv, S, ev);

// ---------- 渲染 ----------
function fitCanvas() { fitCanvasTo(cv, $('stage')); }

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

// 自动保存 + 自动合成的防抖。任何笔画改动（落笔/擦除/撤销/清空）都会触发：
// 停笔 600ms 后自动写回工程文件，再自动合成当前相机。这样画完不用点任何按钮。
let autoTimer = null;
function scheduleAuto() {
  S.dirty = true;
  if (autoTimer) clearTimeout(autoTimer);
  autoTimer = setTimeout(() => { autoSave(); autoMerge(); }, 600);
}

async function autoSave() {
  if (!S.rootHandle) return;                 // 没选过目录就没有可写回的位置
  if (!S.dirty) return;
  await save();
}

function markDirty() { scheduleAuto(); }

// ---------- 载入 snapshots 目录 ----------
async function openRoot() {
  let items;
  try {
    const picked = await pickRoot();
    items = picked.items;
    S.rootHandle = picked.rootHandle;
  } catch (e) { if (e?.name !== 'AbortError') setStatus(String(e?.message || e)); return; }
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
  const n = Object.values(doc.cameras).reduce((a, f) => a + f.length, 0);
  const how = await writeJson(S.rootHandle, PROJECT_FILE,
                              JSON.stringify(doc, null, 2),
                              (e) => setStatus(`⚠ 原地保存失败(${e?.name})，改为下载`));
  S.dirty = false;
  const verb = how === 'wrote' ? '已写回' : '已下载';
  $('saveinfo').textContent = `${verb} ${PROJECT_FILE}（${n} 个已标帧）`;
  if (how === 'wrote') setStatus(`已保存 ${PROJECT_FILE}`);
  syncUI();
}

async function tryLoadProjectFile() {
  const doc = await readJson(S.rootHandle, PROJECT_FILE, SCHEMA);
  if (!doc) return;
  let n = 0;
  for (const frames of Object.values(doc.cameras || {})) {
    for (const f of frames) {
      if (!f.image) continue;
      S.saved[f.image] = (f.strokes || []).map((s) => ({ ...s }));
      n++;
    }
  }
  $('saveinfo').textContent = `已载入 ${PROJECT_FILE}（${n} 个已标帧）`;
  setStatus(`已载入既有工程 ${PROJECT_FILE}，可续标`);
}

// ---------- 一键合成 ----------
async function autoMerge() {
  // 自动合成的防抖入口：scheduleAuto 在每次笔画改动后调用。
  // 只在选过目录（rootHandle）且当前有相机时合成，避免频繁空跑。
  if (!S.rootHandle || !S.cam) return;
  await mergeCurrent();
}

async function mergeCurrent() {
  if (!S.cam) { setStatus('尚未选相机'); return; }
  // 快照目录绝对路径可留空：浏览器拿不到本地绝对路径（File System Access 只给
  // 句柄），留空时后端回退到默认数据根 <数据集根>/<日期>/snapshots。
  const snapDir = $('snapdir').value.trim();
  // 合成用「已保存的工程 + 当前内存里未保存的笔画」：把内存态并进 doc，
  // 这样画完不点保存也能直接看合成效果。
  const doc = projectDoc();
  const cams = doc.cameras;
  const list = S.byCam.get(S.cam) || [];
  const cur = {};
  for (const it of list) {
    const strokes = (S.saved[it.relPath] || []).map((s) => ({ ...s }));
    if (strokes.length) cur[it.relPath] = strokes;
  }
  cams[S.cam] = list
    .map((it, i) => ({
      frame_index: i + 1,
      snapshot_id: it.snapshotId,
      image: it.relPath,
      strokes: (cur[it.relPath] || []).map((s) => ({
        x1: +s.x1.toFixed(1), y1: +s.y1.toFixed(1),
        x2: +s.x2.toFixed(1), y2: +s.y2.toFixed(1), r: +(+s.r).toFixed(1),
      })),
    }))
    .filter((f) => f.strokes.length);

  const mi = $('mergeinfo');
  mi.textContent = '合成中…';
  try {
    const resp = await fetch('/mask-merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: doc, snapshots_dir: snapDir, camera: S.cam }),
    });
    if (!resp.ok) {
      const err = await resp.text().catch(() => '');
      mi.textContent = `合成失败：${resp.status} ${err}`;
      setStatus(`合成失败：${resp.status} ${err}`);
      return;
    }
    const { files } = await resp.json();
    mi.textContent = `已合成：${files.join('  ')}`;
    setStatus(`已合成 ${files.length} 张，见数据集 object-frames/`);
    // 展示合并图缩略图，点击才打开新标签（不自动弹窗）。
    const merged = files.find((f) => f.endsWith('_mask_merged.png'));
    if (merged) {
      showResultThumb('/mask-merge-result?f=' + encodeURIComponent(merged));
    }
  } catch (e) {
    mi.textContent = `请求失败：${e?.message || e}`;
    setStatus(`请求失败：${e?.message || e}`);
  }
}

// 在侧栏「合成结果」卡片里显示合并图缩略图；点击缩略图新标签打开原图。
function showResultThumb(url) {
  const box = $('resultthumb');
  box.innerHTML = '';
  const a = document.createElement('a');
  a.href = url;
  a.target = '_blank';
  a.title = '点击查看大图';
  const img = document.createElement('img');
  img.src = url;
  img.alt = '合成结果';
  img.style.maxWidth = '100%';
  img.style.maxHeight = '180px';
  img.style.border = '1px solid #39424f';
  img.style.borderRadius = '4px';
  a.appendChild(img);
  box.appendChild(a);
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
    clampView(cv, S); render(); return;
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
  zoomAt(cv, S, ev);
  refresh();
}, { passive: false });

// ---------- 键盘 ----------
let spaceDown = false;
window.addEventListener('keydown', (ev) => {
  if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'SELECT') return;
  const k = ev.key;
  if (k === ' ') { spaceDown = true; cv.style.cursor = 'grab'; ev.preventDefault(); return; }
  if ((ev.ctrlKey || ev.metaKey) && (k === 'z' || k === 'Z')) { ev.preventDefault(); undo(); return; }
  if ((ev.ctrlKey || ev.metaKey) && (k === 'm' || k === 'M')) { ev.preventDefault(); mergeCurrent(); return; }
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
$('merge').addEventListener('click', () => mergeCurrent());
$('viewmask').addEventListener('click', toggleMaskOnly);
$('radius').addEventListener('input', (ev) => setRadius(+ev.target.value));
$('alpha').addEventListener('input', (ev) => { S.alpha = +ev.target.value / 100; refresh(); });
window.addEventListener('resize', () => { if (S.bitmap) { fitCanvas(); render(); } });

syncUI();



