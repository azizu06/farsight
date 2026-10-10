import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

// Transpile without writing build artifacts, including on supported Node20.
const source = readFileSync(new URL("../src/vr-actions.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText.replace(/from ["']three["']/, `from ${JSON.stringify(import.meta.resolve("three"))}`);
const { VRActionMenu, vrActionHit } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("all four rows are reachable and separated by noninteractive gaps", () => {
  for (const [y, expected] of [[.76, "explore"], [.59, "scan"], [.41, "drill"], [.24, "close"]]) {
    assert.equal(vrActionHit({ x: .5, y }), expected);
    assert.equal(vrActionHit({ x: .02, y }), null);
    assert.equal(vrActionHit({ x: .98, y }), null);
  }
  for (const y of [.95, .675, .505, .325, .09]) {
    assert.equal(vrActionHit({ x: .5, y }), null, "header, card gaps and footnote never activate actions");
  }
});

test("invalid or out-of-panel UV never chooses an action", () => {
  for (const uv of [undefined, null, { x: NaN, y: .5 }, { x: .5, y: Infinity },
                    { x: -.1, y: .76 }, { x: 1.1, y: .76 }, { x: .5, y: -.1 }, { x: .5, y: 1.1 }]) {
    assert.equal(vrActionHit(uv), null);
  }
});

function withMenu(run) {
  const previous = globalThis.document;
  const labels = [];
  const context = { clearRect() {}, beginPath() {}, roundRect() {}, fill() {}, stroke() {},
                    fillText(text) { labels.push(text); } };
  let creates = 0;
  globalThis.document = {
    createElement(tag) {
      assert.equal(tag, "canvas"); creates++;
      return { getContext: kind => kind === "2d" ? context : null };
    },
    body: { appendChild() { assert.fail("menu must not append DOM"); } },
  };
  let menu;
  try {
    menu = new VRActionMenu();
    assert.equal(creates, 1);
    run(menu, labels);
  } finally {
    menu?.dispose();
    globalThis.document = previous;
  }
}

test("hidden menu cannot be clicked, and labels require no hidden button mapping", () => withMenu((menu, labels) => {
  assert.equal(menu.visible, false);
  assert.equal(menu.hit({ x: .5, y: .76 }), null);
  assert.deepEqual(menu.panel.position.toArray(), [0, 0, -2.2]);
  for (const text of ["Explore", "Scan", "Rescue drill", "Close", "Point + right grip to select", "Hold A closes"]) {
    assert.ok(labels.includes(text));
  }
  menu.visible = true;
  assert.equal(menu.hit({ x: .5, y: .76 }), "explore");
  menu.visible = false;
  assert.equal(menu.hit({ x: .5, y: .76 }), null);
}));

test("unchanged visibility and hover do not redraw or upload the texture", () => withMenu(menu => {
  const texture = menu.panel.material.map;
  const initial = texture.version;
  menu.hover("scan");
  assert.equal(texture.version, initial, "hidden hover performs no texture upload");
  menu.visible = true;
  const shown = texture.version;
  assert.ok(shown > initial);
  for (let frame = 0; frame < 100; frame++) { menu.visible = true; menu.hover("scan"); }
  assert.equal(texture.version, shown, "100 stable pointer frames upload nothing");
  menu.hover("close");
  assert.ok(texture.version > shown);
  const changed = texture.version;
  menu.hover("close");
  menu.visible = false;
  assert.equal(texture.version, changed);
}));

test("dispose releases all owned GPU resources", () => withMenu(menu => {
  const disposed = [];
  for (const [name, resource] of [["geometry", menu.panel.geometry], ["material", menu.panel.material],
                                 ["texture", menu.panel.material.map]]) {
    resource.addEventListener("dispose", () => disposed.push(name));
  }
  menu.dispose();
  assert.deepEqual(disposed, ["geometry", "material", "texture"]);
}));
