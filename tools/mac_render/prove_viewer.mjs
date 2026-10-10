/**
 * Load the built viewer from the Mac server and print both socket URLs
 * plus the corner frame-age text. Desk proof only. Not a headset test.
 */
import playwright from "/Volumes/SANDISK/mac-mini-offload-20260522-213111/Documents/code/farsight-work/mac-offload/quest/node_modules/playwright/index.js";

const { chromium } = playwright;

const origin = process.argv[2] || "http://127.0.0.1:8090";
const mac = process.argv[3] || origin;
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  args: ["--disable-gpu", "--disable-software-rasterizer"],
});
const page = await browser.newPage();
const sockets = [];
page.on("websocket", (socket) => sockets.push(socket.url()));
page.on("pageerror", (error) => console.log("pageerror", String(error)));
const pageUrl = new URL(origin.includes("macview") ? origin : `${origin}/`);
pageUrl.searchParams.set("mac", mac);
await page.goto(pageUrl.href, { waitUntil: "domcontentloaded" });
const deadline = Date.now() + 8000;
let age = "";
while (Date.now() < deadline) {
  age = await page.locator(".mac-age").innerText().catch(() => "");
  if (/frame age \d+ ms/.test(age) && sockets.length >= 2) break;
  await page.waitForTimeout(250);
}
console.log(JSON.stringify({ sockets, age, href: page.url() }, null, 2));
await browser.close();
const ws = mac.replace(/^http/, "ws");
if (!sockets.includes(`${ws}/ws/macview`)) process.exit(2);
if (!sockets.includes(`${ws}/ws/macview-pose`)) process.exit(3);
if (!/frame age \d+ ms/.test(age)) process.exit(4);
