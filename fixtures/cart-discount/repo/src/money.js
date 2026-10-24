'use strict';

/** Round to cents to avoid floating point noise. */
function roundCents(value) {
  return Math.round(value * 100) / 100;
}

module.exports = { roundCents };
