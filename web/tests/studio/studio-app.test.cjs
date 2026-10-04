const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..", "..");
const studioApp = path.join(root, "features", "studio", "StudioApp.tsx");
const studioPage = path.join(root, "app", "studio", "page.tsx");
const studioProjectPage = path.join(root, "app", "studio", "[projectId]", "page.tsx");

const files = {
  studioApp,
  studioPage,
  studioProjectPage,
};

function readSrc(name) {
  return fs.readFileSync(files[name], "utf8");
}

// ── Existence and non-emptiness ────────────────────────────────────────────

test("StudioApp.tsx exists and is non-empty", () => {
  assert.ok(fs.existsSync(studioApp), "StudioApp.tsx not found");
  assert.ok(fs.readFileSync(studioApp, "utf8").length > 0, "StudioApp.tsx is empty");
});

test("studio/page.tsx exists and is non-empty", () => {
  assert.ok(fs.existsSync(studioPage), "studio/page.tsx not found");
  assert.ok(fs.readFileSync(studioPage, "utf8").length > 0, "studio/page.tsx is empty");
});

test("[projectId]/page.tsx exists and is non-empty", () => {
  assert.ok(fs.existsSync(studioProjectPage), "[projectId]/page.tsx not found");
  assert.ok(
    fs.readFileSync(studioProjectPage, "utf8").length > 0,
    "[projectId]/page.tsx is empty",
  );
});

// ── [projectId]/page.tsx behavior ──────────────────────────────────────────

test("[projectId]/page.tsx uses decodeURIComponent on the raw param", () => {
  const src = readSrc("studioProjectPage");
  assert.ok(
    src.includes("decodeURIComponent"),
    "Expected decodeURIComponent in [projectId]/page.tsx",
  );
});

test("[projectId]/page.tsx guards empty id with invalid prop", () => {
  const src = readSrc("studioProjectPage");
  assert.ok(
    src.includes("invalid"),
    "Expected 'invalid' in [projectId]/page.tsx as empty-id guard",
  );
});

// ── studio/page.tsx behavior ───────────────────────────────────────────────

test("studio/page.tsx renders StudioApp with null projectId", () => {
  const src = readSrc("studioPage");
  assert.ok(
    /projectId\s*=\s*\{null\}/.test(src),
    "Expected projectId={null} in studio/page.tsx",
  );
});

// ── Prohibited references (all three files) ────────────────────────────────

function assertNoForbidden(label, src) {
  assert.ok(!src.includes('"/api/'), `${label} must not reference "/api/`);
  assert.ok(!src.includes("fetch("), `${label} must not call fetch(`);
  assert.ok(!src.includes('from "app/'), `${label} must not import from app/`);
  assert.ok(!src.includes("from '../app/"), `${label} must not import from ../app/`);
  assert.ok(!src.includes('from "../../app/'), `${label} must not import from ../../app/`);
  assert.ok(
    !/ProjectHome|TaskActivity|reader/.test(src),
    `${label} must not reference ProjectHome, TaskActivity, or reader`,
  );
}

test("StudioApp.tsx has no forbidden references", () => {
  assertNoForbidden("StudioApp.tsx", readSrc("studioApp"));
});

test("studio/page.tsx has no forbidden references", () => {
  assertNoForbidden("studio/page.tsx", readSrc("studioPage"));
});

test("[projectId]/page.tsx has no forbidden references", () => {
  assertNoForbidden("[projectId]/page.tsx", readSrc("studioProjectPage"));
});

// ── Styled-jsx presence ────────────────────────────────────────────────────

test("StudioApp.tsx contains <style jsx>", () => {
  const src = readSrc("studioApp");
  assert.ok(
    src.includes("<style jsx"),
    "Expected <style jsx in StudioApp.tsx",
  );
});
