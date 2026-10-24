'use strict';

const { applyDiscount, discountPercentFor } = require('./discounts');
const { roundCents } = require('./money');

/** Sum of line items (price * quantity). */
function subtotal(items) {
  return items.reduce((sum, item) => sum + item.price * item.quantity, 0);
}

/** Total shown at checkout: subtotal minus the discount code, rounded to cents. */
function checkoutTotal(items, discountCode) {
  const percent = discountPercentFor(discountCode);
  return roundCents(applyDiscount(subtotal(items), percent));
}

module.exports = { subtotal, checkoutTotal };
