'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { checkoutTotal } = require('../src/cart');
const { applyDiscount } = require('../src/discounts');

test('10% discount on 200 gives 180', () => {
  assert.equal(checkoutTotal([{ price: 100, quantity: 2 }], 'WELCOME10'), 180);
});

test('25% discount applies as a percentage', () => {
  assert.equal(applyDiscount(80, 25), 60);
});
