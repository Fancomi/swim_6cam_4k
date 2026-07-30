// labeler_core.js
// 两个标注器（mask / dot）共用的那一半：视图变换、目录选取、帧导航、保存下载。
//
// 差异只在「一帧上标什么」——mask 画胶囊笔画、dot 打点——所以那部分留在各自的
// app.js 里。共用的部分曾经是两份逐字相同的代码，改一处漏一处。
//
// 所有函数都接受一个 view/state 对象而不是闭包捕获，这样自测页可以直接构造一个
// 假的 state 调用它们。

export const IMG_RE = /\.(jpe?g|png|bmp|webp|gif)$/i;

// ---------- 视图变换：fit + zoom，中心锚定 ----------
// state 需要 { imgW, imgH, zoom, cx, cy }；cx/cy 是视口中心对应的图像坐标。

export function fitScale(canvas, state) {
  return Math.min(canvas.width / state.imgW, canvas.height / state.imgH);
}

export function scaleOf(canvas, state) {
  return fitScale(canvas, state) * state.zoom;
}

export function toScreen(canvas, state, x, y) {
  const s = scaleOf(canvas, state);
  return [canvas.width / 2 + (x - state.cx) * s,
          canvas.height / 2 + (y - state.cy) * s];
}

export function toImage(canvas, state, sx, sy) {
  const s = scaleOf(canvas, state);
  return [state.cx + (sx - canvas.width / 2) / s,
          state.cy + (sy - canvas.height / 2) / s];
}

export function resetView(state) {
  state.zoom = 1;
  state.cx = state.imgW / 2;
  state.cy = state.imgH / 2;
}

// 允许适度平移，但不让图像整块移出视口——否则很容易“丢失”画面而不知道往哪拖。
export function clampView(canvas, state) {
  const s = scaleOf(canvas, state);
  const halfW = canvas.width / 2 / s;
  const halfH = canvas.height / 2 / s;
  state.cx = Math.max(-halfW + 20, Math.min(state.imgW + halfW - 20, state.cx));
  state.cy = Math.max(-halfH + 20, Math.min(state.imgH + halfH - 20, state.cy));
}

export function eventToImage(canvas, state, ev) {
  const r = canvas.getBoundingClientRect();
  return toImage(canvas, state, ev.clientX - r.left, ev.clientY - r.top);
}

export function fitCanvas(canvas, stage) {
  canvas.width = stage.clientWidth;
  canvas.height = stage.clientHeight;
}

// 以光标为锚点缩放：锚点下的那个图像坐标缩放后仍在光标下，否则缩放会把注意力
// 移出视野。
export function zoomAt(canvas, state, ev, { min = 1, max = 20 } = {}) {
  const r = canvas.getBoundingClientRect();
  const sx = ev.clientX - r.left;
  const sy = ev.clientY - r.top;
  const [ax, ay] = toImage(canvas, state, sx, sy);
  state.zoom = Math.max(min, Math.min(max, state.zoom * Math.exp(-ev.deltaY * 0.0015)));
  const s = scaleOf(canvas, state);
  state.cx = ax - (sx - canvas.width / 2) / s;
  state.cy = ay - (sy - canvas.height / 2) / s;
  clampView(canvas, state);
}

// ---------- 目录选取 ----------
// Chrome / Edge 有 File System Access API，能原地写回；其余浏览器退回
// <input webkitdirectory>，只能下载。

export const fsSupported = () => 'showDirectoryPicker' in window;

export async function walkDir(dirHandle, prefix, out) {
  for await (const [name, handle] of dirHandle.entries()) {
    const rel = prefix ? `${prefix}/${name}` : name;
    if (handle.kind === 'directory') await walkDir(handle, rel, out);
    else if (IMG_RE.test(name)) {
      out.push({ name, relPath: rel, fileHandle: handle, dirHandle, file: null });
    }
  }
}

export function pickViaInput() {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.webkitdirectory = true;
    input.multiple = true;
    input.addEventListener('change', () => {
      resolve([...input.files].filter((f) => IMG_RE.test(f.name)).map((f) => ({
        name: f.name,
        relPath: f.webkitRelativePath || f.name,
        file: f,
        fileHandle: null,
        dirHandle: null,
      })));
    });
    input.addEventListener('cancel', () => reject({ name: 'AbortError' }));
    input.click();
  });
}

// 返回 { items, rootHandle }；用户取消时抛 { name: 'AbortError' }。
export async function pickRoot() {
  if (!fsSupported()) return { items: await pickViaInput(), rootHandle: null };
  const rootHandle = await window.showDirectoryPicker();
  const items = [];
  await walkDir(rootHandle, '', items);
  return { items, rootHandle };
}

export async function fileOf(item) {
  return item.file ? item.file : await item.fileHandle.getFile();
}

// ---------- 工程文件读写 ----------

export function download(text, name) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 原地写回 `name`，失败（权限、只读目录）时退回下载。返回 'wrote' | 'downloaded'。
export async function writeJson(rootHandle, name, text, onFallback) {
  if (rootHandle) {
    try {
      const handle = await rootHandle.getFileHandle(name, { create: true });
      const writable = await handle.createWritable();
      await writable.write(text);
      await writable.close();
      return 'wrote';
    } catch (error) {
      if (onFallback) onFallback(error);
    }
  }
  download(text, name);
  return 'downloaded';
}

// 读回既有工程；文件不存在或 schema 不符都返回 null（不是错误，是首次标注）。
export async function readJson(rootHandle, name, schema) {
  if (!rootHandle) return null;
  try {
    const handle = await rootHandle.getFileHandle(name);
    const doc = JSON.parse(await (await handle.getFile()).text());
    return (schema && doc.schema !== schema) ? null : doc;
  } catch {
    return null;
  }
}
