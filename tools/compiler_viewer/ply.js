// Minimal binary-little-endian / ASCII PLY parser for compiler artefacts.
// Reads every declared property into a typed array so callers can pick up
// extended fields (support, flags, radius, f_dc_*, scale_*, rot_*, s/t uvs)
// no matter which export stage wrote them.

const TYPES = {
  char: Int8Array, int8: Int8Array,
  uchar: Uint8Array, uint8: Uint8Array, uint8_t: Uint8Array,
  short: Int16Array, int16: Int16Array,
  ushort: Uint16Array, uint16: Uint16Array,
  int: Int32Array, int32: Int32Array,
  uint: Uint32Array, uint32: Uint32Array,
  float: Float32Array, float32: Float32Array,
  double: Float64Array, float64: Float64Array,
};
const LIST_COUNT = { uchar: Uint8Array, uint8: Uint8Array, char: Int8Array, int8: Int8Array,
  ushort: Uint16Array, uint16: Uint16Array, short: Int16Array, int16: Int16Array,
  uint: Uint32Array, uint32: Uint32Array, int: Int32Array, int32: Int32Array };

const NORM = { r: 'red', g: 'green', b: 'blue',
  diffuse_red: 'red', diffuse_green: 'green', diffuse_blue: 'blue',
  texture_u: 's', texture_v: 't', u: 's', v: 't' };

function normName(n) { return NORM[n.toLowerCase()] || n; }

export function parsePLY(buf) {
  const dv = new DataView(buf);
  // header is ASCII; find end_header\n
  let hEnd = -1;
  const head = new Uint8Array(buf, 0, Math.min(buf.byteLength, 16 * 1024));
  const needle = 'end_header';
  for (let i = 0; i + 12 <= head.length; i++) {
    if (head[i] === 101 /* e */) {
      let ok = true;
      for (let k = 0; k < needle.length; k++) if (head[i + k] !== needle.charCodeAt(k)) { ok = false; break; }
      if (ok) { hEnd = i + needle.length; break; }
    }
  }
  if (hEnd < 0) throw new Error('PLY: end_header not found');
  // skip EOL after end_header (\r\n or \n)
  while (hEnd < head.length && (head[hEnd] === 10 || head[hEnd] === 13)) hEnd++;
  const headerText = new TextDecoder().decode(head.subarray(0, hEnd));
  const lines = headerText.split(/\r?\n/);
  let format = 'binary_little_endian';
  const elements = [];
  for (const line of lines) {
    const t = line.trim().split(/\s+/);
    if (t[0] === 'format') format = t[1];
    else if (t[0] === 'element') elements.push({ name: t[1], count: +t[2], props: [] });
    else if (t[0] === 'property' && elements.length) {
      if (t[1] === 'list') elements[elements.length - 1].props.push({ list: true, ctype: t[2], type: t[3], name: t[4] });
      else elements[elements.length - 1].props.push({ list: false, type: t[1], name: t[2] });
    }
  }

  let off = hEnd;
  const out = {};
  if (format === 'ascii') {
    const text = new TextDecoder().decode(new Uint8Array(buf, off));
    let idx = 0;
    const tok = () => text.slice(idx, (idx = text.indexOf(' ', idx) < 0 ? text.length : idx)).trim() || (idx++, null);
    // simpler: split once into tokens
    const toks = text.split(/\s+/).filter(s => s.length);
    let ti = 0;
    for (const el of elements) {
      const store = { count: el.count, props: {} };
      for (const p of el.props) {
        if (!p.list) {
          const A = new (TYPES[p.type] || Float32Array)(el.count);
          for (let i = 0; i < el.count; i++) A[i] = +toks[ti++];
          store.props[normName(p.name)] = A;
        } else {
          const lists = new Array(el.count);
          for (let i = 0; i < el.count; i++) {
            const n = +toks[ti++]; lists[i] = toks.slice(ti, ti + n).map(Number); ti += n;
          }
          store.props[normName(p.name)] = lists;
        }
      }
      out[el.name] = store;
    }
    return out;
  }
  if (format !== 'binary_little_endian') throw new Error(`PLY: unsupported format ${format}`);

  for (const el of elements) {
    const store = { count: el.count, props: {} };
    for (const p of el.props) store.props[normName(p.name)] = p.list ? new Array(el.count) : new (TYPES[p.type] || Float32Array)(el.count);
    for (let i = 0; i < el.count; i++) {
      for (const p of el.props) {
        const name = normName(p.name);
        if (!p.list) {
          const A = TYPES[p.type]; if (!A) throw new Error(`PLY: bad type ${p.type}`);
          store.props[name][i] = A === Float64Array ? dv.getFloat64(off, true)
            : A === Float32Array ? dv.getFloat32(off, true)
            : A === Int8Array ? dv.getInt8(off) : A === Uint8Array ? dv.getUint8(off)
            : A === Int16Array ? dv.getInt16(off, true) : A === Uint16Array ? dv.getUint16(off, true)
            : A === Int32Array ? dv.getInt32(off, true) : dv.getUint32(off, true);
          off += A.BYTES_PER_ELEMENT;
        } else {
          const C = LIST_COUNT[p.ctype] || Uint8Array;
          const n = C.BYTES_PER_ELEMENT === 1 ? dv.getUint8(off)
            : C === Int16Array ? dv.getInt16(off, true) : C === Uint16Array ? dv.getUint16(off, true)
            : dv.getUint32(off, true);
          off += C.BYTES_PER_ELEMENT;
          const V = TYPES[p.type] || Float32Array;
          const arr = new V(n);
          for (let k = 0; k < n; k++) {
            arr[k] = V === Float64Array ? dv.getFloat64(off, true)
              : V === Float32Array ? dv.getFloat32(off, true)
              : V === Int8Array ? dv.getInt8(off) : V === Uint8Array ? dv.getUint8(off)
              : V === Int16Array ? dv.getInt16(off, true) : V === Uint16Array ? dv.getUint16(off, true)
              : V === Int32Array ? dv.getInt32(off, true) : dv.getUint32(off, true);
            off += V.BYTES_PER_ELEMENT;
          }
          store.props[name][i] = arr;
        }
      }
    }
    out[el.name] = store;
  }
  return out;
}

const first = (props, ...names) => { for (const n of names) if (props[n]) return props[n]; return null; };

// Build three-style attribute arrays from a parsed vertex element.
export function vertexArrays(ply, elem = 'vertex') {
  const v = ply[elem];
  if (!v) throw new Error(`PLY: no ${elem} element`);
  const p = v.props, n = v.count;
  const out = { n };
  out.position = new Float32Array(n * 3);
  const pos = out.position;
  for (let i = 0; i < n; i++) { pos[3 * i] = p.x[i]; pos[3 * i + 1] = p.y[i]; pos[3 * i + 2] = p.z[i]; }
  const r = first(p, 'red'), g = first(p, 'green'), b = first(p, 'blue');
  if (r && g && b) {
    out.color = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) { out.color[3 * i] = r[i] / 255; out.color[3 * i + 1] = g[i] / 255; out.color[3 * i + 2] = b[i] / 255; }
  }
  if (p.nx && p.ny && p.nz) {
    out.normal = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) { out.normal[3 * i] = p.nx[i]; out.normal[3 * i + 1] = p.ny[i]; out.normal[3 * i + 2] = p.nz[i]; }
  }
  const s = first(p, 's'), t = first(p, 't');
  if (s && t) { out.uv = new Float32Array(n * 2); for (let i = 0; i < n; i++) { out.uv[2 * i] = s[i]; out.uv[2 * i + 1] = t[i]; } }
  const sup = first(p, 'support', '_SUPPORT', 'SUPPORT');
  if (sup) out.support = sup instanceof Float32Array || sup instanceof Float64Array ? new Float32Array(sup) : Float32Array.from(sup);
  const fl = first(p, 'flags', 'flag', '_FLAGS');
  if (fl) out.flags = fl instanceof Uint8Array ? fl : Uint8Array.from(fl);
  const rad = first(p, 'radius');
  if (rad) out.radius = Float32Array.from(rad);
  const w = first(p, 'weight');
  if (w) out.weight = Float32Array.from(w);
  if (ply.face) {
    const lists = ply.face.props.vertex_indices || ply.face.props.vertex_index || Object.values(ply.face.props)[0];
    if (lists) {
      let m = 0; for (const l of lists) m += (l.length - 2) * 3;
      const idx = n > 65535 ? new Uint32Array(m) : new Uint16Array(m);
      let k = 0;
      for (const l of lists) for (let j = 1; j + 1 < l.length; j++) { idx[k++] = l[0]; idx[k++] = l[j]; idx[k++] = l[j + 1]; }
      out.index = idx;
    }
  }
  return out;
}
