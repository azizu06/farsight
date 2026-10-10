/** Same simulated map, unlit then lit. Mac desk, no headset. */
import playwright from "/Volumes/SANDISK/mac-mini-offload-20260522-213111/Documents/code/farsight-work/mac-offload/quest/node_modules/playwright/index.js";

const { chromium } = playwright;
const base = process.argv[2] || "http://127.0.0.1:8090";
const out = process.argv[3];
const style = process.argv[4] || "lit";
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CHROME,
  args: ["--use-angle=metal", "--enable-gpu"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
const url = `${base}/render/render.html?upstream=${base}&width=1280&height=720&style=${style}`;
await page.goto(url, { waitUntil: "domcontentloaded" });
await page.waitForFunction("() => window.__mac && window.__mac.points > 0");
await page.waitForTimeout(1800);
const samples = [];
for (let i = 0; i < 30; i += 1) {
  samples.push(await page.evaluate(() => window.__mac.renderMs));
  await page.waitForTimeout(50);
}
samples.sort((a, b) => a - b);
const renderMs = samples[Math.floor(samples.length / 2)];
await page.screenshot({ path: out });
console.log(JSON.stringify({ style, renderMs, points: await page.evaluate(() => window.__mac.points), out }));
await browser.close();
