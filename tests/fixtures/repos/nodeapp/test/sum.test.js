const test = require("node:test");
const assert = require("node:assert");
const { sum } = require("../src/sum");

test("sum adds", () => {
  assert.strictEqual(sum(1, 2), 3);
});
