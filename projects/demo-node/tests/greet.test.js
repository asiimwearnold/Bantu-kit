const test = require('node:test');
const assert = require('node:assert');
const { greet } = require('../src/greet.js');

test('greet says hello', () => {
  assert.strictEqual(greet('Ada'), 'Hello, Ada!');
});
