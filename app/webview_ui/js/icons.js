// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
'use strict';
window.AppIcons = (() => {
  const paths = {
    tasks: '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="m8 9 1 1 2-2m-3 7 1 1 2-2m3-5h3m-3 6h3"/>',
    library: '<path d="M5 4h14v17H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm0 13h14M8 8h7m-7 4h5"/>',
    browser: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M7 6.5h.01m3 0h.01M9 13l-2 2 2 2m6-4 2 2-2 2"/>',
    link: '<path d="m10 13 4-4m-5 7-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m0 4a4 4 0 0 0 6 0l4-4a4 4 0 0 0-6-6l-1 1" transform="translate(1 2) scale(.9)"/>',
    import: '<path d="M12 3v12m-4-8 4-4 4 4M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
    retry: '<path d="M4 9a8 8 0 1 1 0 6m0-11v5h5"/>',
    tidy: '<path d="M5 6h14M5 12h9M5 18h11m3-8 1 3 3 1-3 1-1 3-1-3-3-1 3-1Z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    trash: '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    archive: '<rect x="3" y="3" width="18" height="5" rx="1"/><path d="M5 8v13h14V8m-9 5h4"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    folder: '<path d="M3 7V5h7l2 3h9v12H3Z"/>',
    back: '<path d="m14 5-7 7 7 7M7 12h14"/>',
  };
  return (name) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths[name] || paths.more}</svg>`;
})();
