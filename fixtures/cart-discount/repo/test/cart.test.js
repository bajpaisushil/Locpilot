'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { subtotal, checkoutTotal } = require('../src/cart');

test('subtotal multiplies price by quantity', () => {
  assert.equal(subtotal([{ price: 10, quantity: 3 }, { price: 2.5, quantity: 2 }]), 35);
});

test('checkout total without a discount code is the subtotal', () => {
  assert.equal(checkoutTotal([{ price: 99.99, quantity: 1 }]), 99.99);
});

test('unknown discount codes are ignored', () => {
  assert.equal(checkoutTotal([{ price: 50, quantity: 2 }], 'NOPE'), 100);
});
