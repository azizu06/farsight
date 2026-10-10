import { expect, test } from "@playwright/test";
import { createServer, type ServerResponse } from "node:http";
import { readFileSync } from "node:fs";
import { WebSocketServer } from "ws";

const jpeg = readFileSync(new URL("./fixtures/camera.jpg", import.meta.url));
const commands: {
  seq: number;
  deadman: boolean;
  v: number;
  w: number;
  at: number;
}[] = [];
let cameraOn = true,
  repliesOn = true,
  estop = false;
const streams = new Set<ServerResponse>();
const fixture = createServer((request, response) => {
  if (request.url === "/stream.mjpg") {
    response.writeHead(200, {
      "Content-Type": "multipart/x-mixed-replace; boundary=camera",
      "Cache-Control": "no-store",
    });
    streams.add(response);
    const timer = setInterval(() => {
      if (cameraOn && !response.destroyed) {
        response.write(
          `--camera\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.length}\r\n\r\n`,
        );
        response.write(jpeg);
        response.write("\r\n");
      }
    }, 80);
    response.on("close", () => {
      clearInterval(timer);
      streams.delete(response);
    });
  } else {
    response.writeHead(404);
    response.end();
  }
});
const sockets = new WebSocketServer({ noServer: true });
fixture.on("upgrade", (request, socket, head) =>
  sockets.handleUpgrade(request, socket, head, (ws) => {
    let seq = 0,
      frame = 0;
    ws.on("message", (message) => {
      const command = JSON.parse(String(message));
      seq = command.seq;
      commands.push({ ...command, at: Date.now() });
    });
    const timer = setInterval(() => {
      if (ws.readyState !== 1) return;
      if (request.url === "/ws/control" && repliesOn)
        ws.send(
          JSON.stringify({
            seq,
            range_cm: estop ? 15 : 64,
            estop,
            mode: "teleop",
            link_ms: 24,
          }),
        );
      if (request.url === "/ws/detections")
        ws.send(
          JSON.stringify({
            ts: Date.now() / 1000,
            frame: ++frame,
            fps: 25,
            boxes: [
              {
                id: 3,
                cls: "person",
                conf: 0.91,
                x: 0.28,
                y: 0.22,
                w: 0.3,
                h: 0.5,
              },
            ],
          }),
        );
    }, 80);
    ws.on("close", () => clearInterval(timer));
  }),
);

test.beforeAll(async () => {
  await new Promise<void>((resolve) =>
    fixture.listen(18900, "127.0.0.1", resolve),
  );
});
test.afterAll(async () => {
  streams.forEach((response) => response.destroy());
  sockets.clients.forEach((ws) => ws.terminate());
  await new Promise<void>((resolve) => sockets.close(() => resolve()));
  await new Promise<void>((resolve) => fixture.close(() => resolve()));
});
test.beforeEach(() => {
  cameraOn = true;
  repliesOn = true;
  estop = false;
  commands.length = 0;
});
test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    await info.attach("control-diagnostics", {
      body: JSON.stringify({
        commands,
        state: await page.locator("#input-status").textContent(),
        reason: await page
          .locator("#input-status")
          .getAttribute("data-stop-reason"),
        video: await page.locator("#video-status").textContent(),
      }),
      contentType: "application/json",
    });
  }
});

async function centerPixel(page: import("@playwright/test").Page) {
  return page.evaluate(
    () =>
      new Promise<number[]>((resolve) =>
        requestAnimationFrame(() => {
          const canvas = document.querySelector<HTMLCanvasElement>("#view")!;
          const gl = canvas.getContext("webgl2")!;
          const pixel = new Uint8Array(4);
          gl.readPixels(
            gl.drawingBufferWidth / 2,
            gl.drawingBufferHeight / 2,
            1,
            1,
            gl.RGBA,
            gl.UNSIGNED_BYTE,
            pixel,
          );
          resolve(Array.from(pixel));
        }),
      ),
  );
}

test("renders externally streamed JPEG pixels, HUD, and labelled desktop fallback", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/?source=mock");
  await expect(page.locator("#video-status")).toHaveText("VIDEO RECEIVING");
  await expect(page.locator("#boxes")).toHaveText("1");
  await expect(page.locator("#range")).toHaveText("64 cm");
  await expect(page.locator("#fps")).toHaveText("25.0 fps");
  await expect(page.locator("#source-badge")).toContainText("MOCK");
  await expect(page.locator("#enter-vr")).toBeDisabled();
  await expect
    .poll(async () => (await centerPixel(page))[0])
    .toBeGreaterThan(180);
  const pixel = await centerPixel(page);
  expect(pixel[1]).toBeGreaterThan(130);
  expect(pixel[2]).toBeLessThan(100);
  expect(
    commands.every(
      (command) => !command.deadman && command.v === 0 && command.w === 0,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
  if (process.env.FARSIGHT_SCREENSHOT_PATH)
    await page.screenshot({
      path: process.env.FARSIGHT_SCREENSHOT_PATH,
      fullPage: true,
    });
});

test("keyboard drive releases immediately and stale replies cannot resume held driving", async ({
  page,
}) => {
  await page.goto("/?source=mock");
  await expect(page.locator("#link")).toHaveText("LINK LIVE");
  await expect(page.locator("#video-status")).toHaveText("VIDEO RECEIVING");
  await page.locator("#drive-enabled").check();
  await page.locator("#view").click();
  await page.keyboard.down("Space");
  await page.keyboard.down("w");
  await expect
    .poll(
      () =>
        commands.filter((command) => command.deadman && command.v === 1).length,
    )
    .toBeGreaterThanOrEqual(3);
  await page.keyboard.up("Space");
  await expect.poll(() => commands.at(-1)?.deadman).toBe(false);
  await page.keyboard.up("w");
  await page.keyboard.down("Space");
  await page.keyboard.down("w");
  await expect.poll(() => commands.at(-1)?.deadman).toBe(true);
  repliesOn = false;
  await expect(page.locator("#link")).toHaveText("LINK LOST");
  await expect.poll(() => commands.at(-1)?.deadman).toBe(false);
  const stopsAt = commands.length;
  repliesOn = true;
  await expect(page.locator("#link")).toHaveText("LINK LIVE");
  await page.waitForTimeout(250);
  expect(commands.slice(stopsAt).every((command) => !command.deadman)).toBe(
    true,
  );
  await page.keyboard.up("Space");
  await page.keyboard.up("w");
});

test("clears stalled pixels and boxes and stops driving on camera loss", async ({
  page,
}) => {
  await page.goto("/?source=mock");
  await expect(page.locator("#video-status")).toHaveText("VIDEO RECEIVING");
  await page.locator("#drive-enabled").check();
  await page.locator("#view").click();
  await page.keyboard.down("Space");
  await page.keyboard.down("w");
  await expect.poll(() => commands.at(-1)?.deadman).toBe(true);
  cameraOn = false;
  await expect(page.locator("#video-status")).not.toHaveText(
    "VIDEO RECEIVING",
    { timeout: 5000 },
  );
  await expect(page.locator("#boxes")).toHaveText("0");
  await expect.poll(() => commands.at(-1)?.deadman).toBe(false);
  await expect.poll(async () => (await centerPixel(page))[0]).toBeLessThan(100);
  await page.keyboard.up("Space");
  await page.keyboard.up("w");
});

test("drill reports pending face support and counts safety-stop transitions honestly", async ({
  page,
}) => {
  await page.goto("/?source=mock");
  await expect(page.locator("#link")).toHaveText("LINK LIVE");
  await page.locator("#total").fill("2");
  await page.locator("#start-drill").click();
  await expect(page.locator("#drill-status")).toHaveText("Drill running");
  await expect(page.locator("#face-status")).toContainText("pending");
  estop = true;
  await expect(page.locator("#estop")).toHaveText("E-STOP ACTIVE");
  await expect(page.locator("#drill-score")).toHaveText(
    "0/2 faces found · 1 safety stops",
  );
  await page.locator("#end-drill").click();
  await expect(page.locator("#drill-status")).toContainText("Drill finished");
  await expect(page.locator("#drive-enabled")).not.toBeChecked();
});

test("lost WebGL output stops driving and requires explicit re-enable", async ({
  page,
}) => {
  await page.goto("/?source=mock");
  await expect(page.locator("#video-status")).toHaveText("VIDEO RECEIVING");
  await page.locator("#drive-enabled").check();
  await page.locator("#view").click();
  await page.keyboard.down("Space");
  await page.keyboard.down("w");
  await expect.poll(() => commands.at(-1)?.deadman).toBe(true);
  await page.evaluate(() => {
    const gl = document
      .querySelector<HTMLCanvasElement>("#view")!
      .getContext("webgl2")!;
    gl.getExtension("WEBGL_lose_context")!.loseContext();
  });
  await expect(page.locator("#video-status")).toHaveText(
    "Graphics lost — driving disabled",
  );
  await expect(page.locator("#drive-enabled")).not.toBeChecked();
  await expect(page.locator("#drive-enabled")).toBeDisabled();
  await expect(page.locator("#reconnect")).toBeDisabled();
  await expect.poll(() => commands.at(-1)?.deadman).toBe(false);
  await page.keyboard.up("Space");
  await page.keyboard.up("w");
});

test("slow decode clears stale output and reconnects instead of discarding future frames forever", async ({
  page,
}) => {
  await page.goto("/?source=mock");
  await expect(page.locator("#video-status")).toHaveText("VIDEO RECEIVING");
  await page.evaluate(() => {
    const original = window.createImageBitmap.bind(window);
    let delay = true;
    window.createImageBitmap = ((
      ...args: Parameters<typeof createImageBitmap>
    ) => {
      if (!delay) return original(...args);
      delay = false;
      return new Promise<ImageBitmap>((resolve) =>
        setTimeout(() => resolve(original(...args)), 2500),
      );
    }) as typeof createImageBitmap;
  });
  await expect(page.locator("#video-status")).not.toHaveText(
    "VIDEO RECEIVING",
    { timeout: 5000 },
  );
  await expect(page.locator("#video-status")).toHaveText("VIDEO RECEIVING", {
    timeout: 7000,
  });
});
