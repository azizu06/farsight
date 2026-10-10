import { defineConfig, loadEnv } from "vite";
import { readFileSync } from "node:fs";

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ""), ...process.env };
  const cert = env.FARSIGHT_TLS_CERT;
  const key = env.FARSIGHT_TLS_KEY;
  if (Boolean(cert) !== Boolean(key))
    throw new Error("Set both FARSIGHT_TLS_CERT and FARSIGHT_TLS_KEY.");
  const https =
    cert && key
      ? { cert: readFileSync(cert), key: readFileSync(key) }
      : undefined;
  const origin = env.FARSIGHT_JETSON_ORIGIN;
  if (origin) {
    const url = new URL(origin);
    if (
      !/^https?:$/.test(url.protocol) ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash
    )
      throw new Error(
        "FARSIGHT_JETSON_ORIGIN must be an HTTP(S) origin without credentials or paths.",
      );
  }
  const proxy = origin
    ? Object.fromEntries(
        ["/stream.mjpg", "/health", "/ws/control", "/ws/detections"].map(
          (path) => [
            path,
            {
              target: origin,
              changeOrigin: true,
              ws: path.startsWith("/ws/"),
              secure: env.FARSIGHT_ALLOW_SELF_SIGNED !== "1",
            },
          ],
        ),
      )
    : undefined;
  return {
    server: {
      host: "0.0.0.0",
      port: 5174,
      https,
      proxy,
      watch: { usePolling: true },
    },
    preview: { host: "0.0.0.0", port: 5174, https, proxy },
    build: { outDir: "dist" },
    test: { include: ["src/**/*.test.ts"] },
  };
});
