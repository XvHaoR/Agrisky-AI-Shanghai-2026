import test from "node:test";
import assert from "node:assert/strict";
import { validatePublicApiOrigin } from "./validate-api-origin.mjs";

test("accepts localhost origins for development", () => {
  assert.equal(validatePublicApiOrigin("http://localhost:8000"), true);
  assert.equal(validatePublicApiOrigin("http://127.0.0.1:8000"), true);
});

test("accepts https production origins", () => {
  assert.equal(validatePublicApiOrigin("https://api.example.com"), true);
});

test("rejects non-origin URLs", () => {
  assert.throws(() => validatePublicApiOrigin("https://api.example.com/v1"), /origin only/);
  assert.throws(() => validatePublicApiOrigin("https://api.example.com?x=1"), /origin only/);
  assert.throws(() => validatePublicApiOrigin("https://api.example.com/"), /trailing slash/);
});

test("rejects unsafe URL forms", () => {
  assert.throws(() => validatePublicApiOrigin("ftp://api.example.com"), /http or https/);
  assert.throws(() => validatePublicApiOrigin("https://user:pass@api.example.com"), /credentials/);
});

test("requires https for non-local production origins", () => {
  assert.throws(
    () => validatePublicApiOrigin("http://api.example.com", { requireHttps: true }),
    /must use https/
  );
  assert.equal(validatePublicApiOrigin("http://localhost:8000", { requireHttps: true }), true);
});
