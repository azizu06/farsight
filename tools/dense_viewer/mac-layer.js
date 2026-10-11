// Owner-mounted adapter for the Mac renderer's existing layer contract.
// Build with vite.layer.config.mjs; Three.js stays external and shared with ctx.
import { RawView } from './raw-view.js';
export function rawTextureLayer({endpoint='/phone/frame.bin'}={}) {
  let raw;
  return {
    init(ctx) { raw=new RawView(ctx.root,()=>ctx.requestFrame(),{endpoint,onChange:()=>ctx.requestFrame()}); },
    poll() { void raw.update(true,false); return false; },
    stats() { return raw?.stats()??null; },
    dispose() { if(raw){raw.dispose();raw.group.removeFromParent();raw=null;} },
  };
}
