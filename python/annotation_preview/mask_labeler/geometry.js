// mask_labeler/geometry.js
// 纯几何与命名解析，不碰 DOM，便于 node --test 直接验证。

// 相机 id 是文件名去掉后缀后、最后一个 `__` 之后的部分。
// 不能用 /__([A-Za-z0-9_]+)\.jpg$/ ——`_` 在字符类里，正则会贪婪地吃掉中间的
// `__`，把 19_device__orbbec_camera_1__orbbec_camera_1.jpg 解析成
// `orbbec_camera_1__orbbec_camera_1`。
const IMG_NAME_RE = /^(.*)\.(?:jpe?g|png|bmp|webp)$/i;

export function cameraOf(name) {
  const m = IMG_NAME_RE.exec(name);
  if (!m) return null;
  const parts = m[1].split('__');
  return parts.length > 1 ? parts[parts.length - 1] : null;
}

// 快照 id 取图像所在目录名（raw_<ms>_<n>）；没有父目录时退回文件名。
export function snapshotIdOf(relPath) {
  const parts = relPath.split('/');
  return parts.length > 1 ? parts[parts.length - 2] : parts[0];
}

// 点到线段的距离；线段退化为一点时即为点距。
export function distToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((px - x1) * dx + (py - y1) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

// 胶囊 = 线段两端各一个半径 r 的圆；点在胶囊内 <=> 到线段距离 <= r。
export function insideStroke(px, py, s) {
  return distToSegment(px, py, s.x1, s.y1, s.x2, s.y2) <= s.r;
}

// 命中最上面那一笔（后画的在上），没命中返回 -1。
export function hitStrokeIndex(strokes, px, py) {
  for (let i = strokes.length - 1; i >= 0; i--) {
    if (insideStroke(px, py, strokes[i])) return i;
  }
  return -1;
}

// 胶囊面积：两个半圆拼成的整圆 + 中间长方形。
export function capsuleArea(s) {
  const len = Math.hypot(s.x2 - s.x1, s.y2 - s.y1);
  return Math.PI * s.r * s.r + 2 * s.r * len;
}

// 相机排序：非水下相机排前面，它们才是这个工具的目标。
export function sortCameras(cams) {
  return [...cams].sort((a, b) => {
    const ua = /^underA/i.test(a), ub = /^underA/i.test(b);
    if (ua !== ub) return ua ? 1 : -1;
    return a.localeCompare(b, undefined, { numeric: true });
  });
}

// 按相机归组，组内按快照 id 自然序排列。
export function groupByCamera(items) {
  const by = new Map();
  for (const it of items) {
    const cam = cameraOf(it.name);
    if (!cam) continue;
    it.snapshotId = snapshotIdOf(it.relPath);
    if (!by.has(cam)) by.set(cam, []);
    by.get(cam).push(it);
  }
  for (const list of by.values()) {
    list.sort((a, b) => a.snapshotId.localeCompare(b.snapshotId, undefined, { numeric: true }));
  }
  return by;
}
