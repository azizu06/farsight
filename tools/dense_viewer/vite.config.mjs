import { defineConfig } from '../../quest/node_modules/vite/dist/node/index.js';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  resolve: { alias: { three: fileURLToPath(new URL('../../quest/node_modules/three/build/three.module.js', import.meta.url)) } },
  build: { outDir: '../../.cache/dense-viewer-dist', emptyOutDir: true },
});
