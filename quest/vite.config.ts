import { defineConfig } from "vite";

export default defineConfig({
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
