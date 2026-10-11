// Depth-sort worker for the splat renderer. Receives the splat positions once,
// then a view row on every camera move; radix/counting-sorts splat indices
// far -> near (16-bit depth keys) and transfers the order back.

let pos = null;   // Float32Array xyz*3
let n = 0;
let order = null; // Uint32Array
let count = null; // Int32Array(65536)

self.onmessage = (e) => {
  const m = e.data;
  if (m.type === 'init') {
    pos = m.positions; n = m.count;
    order = new Uint32Array(n);
    count = new Int32Array(65536);
    return;
  }
  if (m.type === 'sort' && pos) {
    const [a, b, c, d, far] = m.view; // z_view = a*x + b*y + c*z + d
    count.fill(0);
    const keys = new Uint16Array(n);
    const inv = 65534 / Math.max(far, 1e-3);
    for (let i = 0; i < n; i++) {
      const z = a * pos[3 * i] + b * pos[3 * i + 1] + c * pos[3 * i + 2] + d;
      // depth = -z_view; draw far first -> key = 65534 - depth so small key = far
      let k = 65534 - ((-z) * inv) | 0;
      if (k < 0) k = 0; else if (k > 65534) k = 65534;
      if (z > -0.05) k = 65535; // behind/at camera: draw last (shader clips anyway)
      keys[i] = k;
      count[k]++;
    }
    let sum = 0;
    for (let k = 0; k < 65536; k++) { const t = count[k]; count[k] = sum; sum += t; }
    for (let i = 0; i < n; i++) order[count[keys[i]]++] = i;
    self.postMessage({ type: 'sorted', order }, [order.buffer]);
    order = new Uint32Array(n);
  }
};
