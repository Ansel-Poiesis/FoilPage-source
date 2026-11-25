// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
'use strict';

// Unique shared paragraphs form ordered anchors. Gaps retain every paragraph,
// including repeated text, without a quadratic table for long transcripts.
window.TextDiff = (() => {
  function paragraphs(text) {
    return String(text).replace(/\r\n?/g, '\n').split(/\n[\t ]*\n+/).filter(part => part.trim());
  }

  function compare(beforeText, afterText) {
    const before = paragraphs(beforeText), after = paragraphs(afterText);
    const occurrences = values => {
      const positions = new Map();
      values.forEach((text, index) => positions.set(text, positions.has(text) ? -1 : index));
      return positions;
    };
    const left = occurrences(before), right = occurrences(after);
    const candidates = [];
    before.forEach((text, index) => {
      if (left.get(text) === index && right.get(text) >= 0) candidates.push([index, right.get(text)]);
    });
    const tails = [], parents = [];
    candidates.forEach((pair, index) => {
      let low = 0, high = tails.length;
      while (low < high) {
        const middle = (low + high) >>> 1;
        if (candidates[tails[middle]][1] < pair[1]) low = middle + 1;
        else high = middle;
      }
      parents[index] = low ? tails[low - 1] : -1;
      tails[low] = index;
    });
    const anchors = [];
    for (let index = tails.at(-1); index !== undefined && index >= 0; index = parents[index]) anchors.push(candidates[index]);
    anchors.reverse();
    anchors.push([before.length, after.length]);
    const groups = [];
    let startLeft = 0, startRight = 0;
    anchors.forEach(([endLeft, endRight]) => {
      while (startLeft < endLeft && startRight < endRight && before[startLeft] === after[startRight]) {
        groups.push({kind: 'same', before: [before[startLeft++]], after: [after[startRight++]]});
      }
      let tailLeft = endLeft, tailRight = endRight;
      while (tailLeft > startLeft && tailRight > startRight && before[tailLeft - 1] === after[tailRight - 1]) {
        --tailLeft; --tailRight;
      }
      if (tailLeft > startLeft || tailRight > startRight) {
        const kind = tailLeft === startLeft ? 'added' : tailRight === startRight ? 'removed' : 'changed';
        groups.push({kind, before: before.slice(startLeft, tailLeft), after: after.slice(startRight, tailRight)});
      }
      while (tailLeft < endLeft) groups.push({kind: 'same', before: [before[tailLeft++]], after: [after[tailRight++]]});
      if (endLeft < before.length) groups.push({kind: 'same', before: [before[endLeft]], after: [after[endRight]]});
      startLeft = endLeft + 1; startRight = endRight + 1;
    });
    return groups;
  }

  return {compare, paragraphs};
})();
