// Owner-mounted adapter for the Mac renderer's existing layer contract.
// Build with vite.layer.config.mjs; Three.js stays external and shared with ctx.
import { RawView } from './raw-view.js';
export function rawTextureLayer({endpoint='/phone/frame.bin',enabled=false,pollInterval=500,surfaceHistory=true}={}) {
  let raw,polling=true;
  return {
    init(ctx) {
      raw=new RawView(ctx.root,()=>ctx.requestFrame(),{endpoint,history:false,surfaceHistory,fallbackOnly:true,pollInterval,onChange:()=>ctx.requestFrame()});
      // Native textures cover supported faces; isolated measured pixels remain
      // visible as fallback points without stippling the accepted faces.
    },
    poll() { void raw.update(enabled,false,polling); return false; },
    setEnabled(value) { enabled=Boolean(value);if(!enabled)void raw?.update(false,false); },
    setPolling(value) { polling=Boolean(value); },
    captureOnce() {
      // Seed a visible recent texture with one explicit GET, then continue
      // expiry/rendering without continuous rover traffic.
      enabled=true;polling=false;
      return raw.update(true,false,true,true);
    },
    stats() { return raw?{raw_enabled:enabled,raw_polling:polling,...raw.stats()}:null; },
    dispose() { if(raw){raw.dispose();raw.group.removeFromParent();raw=null;} },
  };
}
