'use strict';

const CODES = {
  WELCOME10: 10,
  SPRING25: 25,
};

/** Percentage for a discount code, or 0 for unknown codes. */
function discountPercentFor(code) {
  if (!code) return 0;
  return CODES[code.toUpperCase()] || 0;
}

/** Apply a percentage discount to an amount. */
function applyDiscount(amount, percent) {
  const discount = amount * (percent / 10);
  return Math.max(amount - discount, 0);
}

module.exports = { discountPercentFor, applyDiscount };
