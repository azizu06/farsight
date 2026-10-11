// Owner-mounted adapter for the Mac renderer's existing layer contract.
// Build with vite.layer.config.mjs; Three.js stays external and shared with ctx.
import { RawView } from './raw-view.js';
export function rawTextureLayer({endpoint='/phone/frame.bin',enabled=false,pollInterval=500,surfaceHistory=true}={}) {
  let raw;
  return {
    init(ctx) {
      raw=new RawView(ctx.root,()=>ctx.requestFrame(),{endpoint,history:false,surfaceHistory,pollInterval,onChange:()=>ctx.requestFrame()});
      // The core renderer retains measured points already. Avoid a duplicate
      // point overlay stippling the native camera texture.
      raw.setLayers({points:false});
    },
    poll() { void raw.update(enabled,false); return false; },
    setEnabled(value) { enabled=Boolean(value);if(!enabled)void raw?.update(false,false); },
    stats() { return raw?{raw_enabled:enabled,...raw.stats()}:null; },
    dispose() { if(raw){raw.dispose();raw.group.removeFromParent();raw=null;} },
  };
}
