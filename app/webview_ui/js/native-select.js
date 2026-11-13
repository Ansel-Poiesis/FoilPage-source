// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
/* Native customizable selects: preserve options, events, focus and fallback. */
'use strict';
window.NativeSelect = (() => {
  const supported = window.CSS?.supports('appearance', 'base-select') === true;
  const selector = 'select:not([multiple]):not([size])';
  function enhance(select) {
    if (!supported || !select.matches(selector) || select.querySelector(':scope > button')) return;
    const value = select.value;
    const trigger = document.createElement('button');
    trigger.type = 'button'; trigger.className = 'native-select-trigger';
    trigger.appendChild(document.createElement('selectedcontent'));
    select.prepend(trigger);
    select.value = value;
  }
  function scan(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.matches(selector)) enhance(node);
    node.querySelectorAll(selector).forEach(enhance);
  }
  if (supported) {
    scan(document.body);
    new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(scan)))
      .observe(document.body, {childList:true, subtree:true});
  }
  return {enhance};
})();
