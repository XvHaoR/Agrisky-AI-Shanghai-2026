import test from "node:test";
import assert from "node:assert/strict";
import { computeApiBase, isLocalHost } from "../app/_lib/resolve-api-base.mjs";

test("isLocalHost recognizes loopback hostnames", () => {
  assert.equal(isLocalHost("localhost"), true);
  assert.equal(isLocalHost("127.0.0.1"), true);
  assert.equal(isLocalHost("[::1]"), true);
  assert.equal(isLocalHost("::1"), true);
});

test("isLocalHost rejects remote hostnames", () => {
  assert.equal(isLocalHost("example.com"), false);
  assert.equal(isLocalHost("192.168.1.1"), false);
  assert.equal(isLocalHost("0.0.0.0"), false);
});

test("explicit env var wins and trims trailing slashes", () => {
  assert.equal(
    computeApiBase({
      envApiBase: "https://api.example.com///",
      hostname: "localhost",
      port: "3011",
      protocol: "http:"
    }),
    "https://api.example.com"
  );
});

test("maps known local frontend ports to matching API ports", () => {
  assert.equal(
    computeApiBase({ hostname: "localhost", port: "3000", protocol: "http:" }),
    "http://localhost:8000"
  );
  assert.equal(
    computeApiBase({ hostname: "127.0.0.1", port: "3001", protocol: "http:" }),
    "http://127.0.0.1:8001"
  );
  assert.equal(
    computeApiBase({ hostname: "127.0.0.1", port: "3011", protocol: "http:" }),
    "http://127.0.0.1:8011"
  );
});

test("maps generic local 3xxx frontend ports to 8xxx API ports", () => {
  assert.equal(
    computeApiBase({ hostname: "localhost", port: "3020", protocol: "http:" }),
    "http://localhost:8020"
  );
  assert.equal(
    computeApiBase({ hostname: "127.0.0.1", port: "3999", protocol: "http:" }),
    "http://127.0.0.1:8999"
  );
});

test("does not infer API ports for non-3xxx local ports", () => {
  assert.equal(
    computeApiBase({ hostname: "localhost", port: "5000", protocol: "http:" }),
    "http://localhost:5000"
  );
});

test("returns same-origin when no port is present", () => {
  assert.equal(
    computeApiBase({ hostname: "localhost", port: "", protocol: "http:" }),
    "http://localhost"
  );
  assert.equal(
    computeApiBase({ hostname: "app.example.com", port: "", protocol: "https:" }),
    "https://app.example.com"
  );
});

test("returns same-origin for non-local hosts with explicit ports", () => {
  assert.equal(
    computeApiBase({ hostname: "preview.example.com", port: "443", protocol: "https:" }),
    "https://preview.example.com:443"
  );
});

test("falls back to localhost:8000 without browser location or env", () => {
  assert.equal(computeApiBase(), "http://localhost:8000");
  assert.equal(computeApiBase({}), "http://localhost:8000");
  assert.equal(computeApiBase({ envApiBase: "" }), "http://localhost:8000");
});
