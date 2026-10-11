import { defineConfig } from "vite";

export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        macview: "macview.html",
        splat: "splat.html",
        explore: "explore.html",
      },
    },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      [
        "/ws",
        "/phone",
        "/health",
        "/ask",
        "/scan",
        "/mission",
        "/map",
        "/camera.jpg",
        "/depth.jpg",
        "/stream.mjpg",
      ].map((path) => [path, { target: "http://127.0.0.1:8443", ws: true }]),
    ),
  },
});
