// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../app/webview_ui/js/api.js", import.meta.url), "utf8");
const contractSource = await readFile(new URL("../app/webview_ui/js/ui-contract.js", import.meta.url), "utf8");

function loadApi({bridge, mock} = {}) {
  const window = {};
  if (bridge) window.pywebview = {api: bridge};
  if (mock) window.__MOCK__ = mock;
  const sandbox = {window};
  vm.runInNewContext(contractSource, sandbox);
  vm.runInNewContext(source, sandbox);
  return window.AppApi;
}

function bridgeWith(extra = {}) {
  return {get_bootstrap() {}, ...extra};
}

test("API success values and business failure objects pass through unchanged", async () => {
  const businessFailure = {ok: false, error: "fixture refusal"};
  const mock = {
    get_bootstrap: () => ({settings: {}}),
    get_tasks: () => businessFailure,
  };
  const api = loadApi({mock});
  assert.equal(await api.tasks(), businessFailure);
  assert.deepEqual(await api.bootstrap(), {settings: {}});
});

test("missing bridge has a stable operation-scoped error", async () => {
  const api = loadApi();
  await assert.rejects(api.tasks(), error => {
    assert.equal(error.name, "AppApiError");
    assert.equal(error.code, "bridge_unavailable");
    assert.equal(error.category, "connection");
    assert.equal(error.operation, "get_tasks");
    assert.equal(error.retryable, false);
    assert.equal(error.diagnostic.operation, "get_tasks");
    assert.equal(error.diagnostic.stage, "bridge");
    assert.equal(error.diagnostic.error_code, "bridge_unavailable");
    return true;
  });
});

test("bridge exceptions are normalized without exposing transport details", async () => {
  const api = loadApi({bridge: bridgeWith({
    get_tasks: () => { throw new Error("https://user:secret@example.invalid/private?token=fixture"); },
  })});
  await assert.rejects(api.tasks(), error => {
    assert.equal(error.name, "AppApiError");
    assert.equal(error.code, "bridge_error");
    assert.equal(error.operation, "get_tasks");
    assert.equal(error.retryable, false);
    assert(!error.message.includes("secret"));
    assert(!JSON.stringify(error.diagnostic).includes("example.invalid"));
    return true;
  });
});

test("explicit bridge error metadata is preserved within safe bounds", async () => {
  const api = loadApi({bridge: bridgeWith({
    get_tasks: () => Promise.reject(Object.assign(new Error("private detail"), {
      code: "network_timeout", status: 503, retryable: true,
    })),
  })});
  await assert.rejects(api.tasks(), error => {
    assert.equal(error.code, "network_timeout");
    assert.equal(error.category, "connection");
    assert.equal(error.status, 503);
    assert.equal(error.retryable, true);
    assert.equal(error.diagnostic.status, 503);
    assert(!error.message.includes("private"));
    return true;
  });
});
