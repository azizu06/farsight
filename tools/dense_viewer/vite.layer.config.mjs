import { defineConfig } from '../../quest/node_modules/vite/dist/node/index.js';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  root:fileURLToPath(new URL('.',import.meta.url)),base:'./',
  build:{outDir:'../../.cache/dense-layer-dist',emptyOutDir:true,
    lib:{entry:fileURLToPath(new URL('./mac-layer.js',import.meta.url)),formats:['es'],fileName:()=> 'raw-texture-layer.mjs'},
    rollupOptions:{external:['three'],output:{paths:{three:'/quest/node_modules/three/build/three.module.js'}}}},
});
