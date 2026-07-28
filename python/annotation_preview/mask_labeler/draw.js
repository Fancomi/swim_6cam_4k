// mask_labeler/draw.js
// 胶囊笔画的全部绘制。与 DOM 无关，只吃 2D context 和一个「图像坐标 -> 画布坐标」的
// mapper，因此自测页能导入同一份代码画出与标注器完全一致的图形。
//
// 一笔 = 按下点到松开点的线段 + 两端各一个半径 r 的圆（胶囊）：
// 线段本体撑出两点间的长方形，两个端点撑出圆头。

// 描出胶囊轮廓，不填不描边（由调用方决定）。
export function capsulePath(c, x1, y1, x2, y2, r) {
  c.beginPath();
  if (Math.hypot(x2 - x1, y2 - y1) < 1e-6) {
    c.arc(x1, y1, r, 0, Math.PI * 2);
    return;
  }
  const a = Math.atan2(y2 - y1, x2 - x1);
  c.arc(x1, y1, r, a + Math.PI / 2, a - Math.PI / 2);
  c.arc(x2, y2, r, a - Math.PI / 2, a + Math.PI / 2);
  c.closePath();
}

// 已落定的笔画：填充 + 描边，表示「这块保留」。
export function drawStrokes(c, strokes, mapper, rscale, fill, stroke) {
  for (const s of strokes) {
    const [ax, ay] = mapper(s.x1, s.y1);
    const [bx, by] = mapper(s.x2, s.y2);
    capsulePath(c, ax, ay, bx, by, s.r * rscale);
    if (fill) { c.fillStyle = fill; c.fill(); }
    if (stroke) { c.strokeStyle = stroke; c.lineWidth = 1; c.stroke(); }
  }
}

// 未松手时的实时预览：胶囊填充 + 两端虚线圆 + 两点间长方形 + 中轴线段与端点。
// 这四样合起来正是 mask 的绘制区域，画出来的形状与落定后判定的形状一致。
export function drawPreview(c, s, mapper, rscale) {
  const [ax, ay] = mapper(s.x1, s.y1);
  const [bx, by] = mapper(s.x2, s.y2);
  const r = s.r * rscale;

  c.save();
  capsulePath(c, ax, ay, bx, by, r);
  c.fillStyle = 'rgba(255,212,121,.35)';
  c.fill();
  c.strokeStyle = '#ffd479'; c.lineWidth = 1.5;
  c.stroke();

  // 两端的圆
  c.setLineDash([4, 3]); c.lineWidth = 1;
  c.strokeStyle = 'rgba(255,255,255,.85)';
  for (const [x, y] of [[ax, ay], [bx, by]]) {
    c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.stroke();
  }

  // 两点间的长方形（胶囊的直边部分）
  const len = Math.hypot(bx - ax, by - ay);
  if (len > 1e-6) {
    const nx = -(by - ay) / len * r, ny = (bx - ax) / len * r;
    c.beginPath();
    c.moveTo(ax + nx, ay + ny); c.lineTo(bx + nx, by + ny);
    c.lineTo(bx - nx, by - ny); c.lineTo(ax - nx, ay - ny);
    c.closePath(); c.stroke();
  }
  c.setLineDash([]);

  // 中轴线段与两个端点
  c.strokeStyle = '#fff'; c.lineWidth = 1;
  c.beginPath(); c.moveTo(ax, ay); c.lineTo(bx, by); c.stroke();
  c.fillStyle = '#fff';
  for (const [x, y] of [[ax, ay], [bx, by]]) {
    c.beginPath(); c.arc(x, y, 3, 0, Math.PI * 2); c.fill();
  }

  // 尺寸读数
  const lenImg = Math.hypot(s.x2 - s.x1, s.y2 - s.y1);
  c.fillStyle = '#ffd479'; c.font = '12px monospace';
  c.fillText(`${lenImg.toFixed(0)}px  r=${s.r}`, bx + 8, by - 8);
  c.restore();
}

// 未落笔时在光标处显示笔刷大小。
export function drawCursorGhost(c, x, y, r) {
  c.save();
  c.setLineDash([3, 3]);
  c.strokeStyle = 'rgba(255,255,255,.5)'; c.lineWidth = 1;
  c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.stroke();
  c.restore();
}
