// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
'use strict';

window.LibraryModel = Object.freeze({
  textSignature,
  documentView,
  sortedDocs,
  applyTagFilter,
});

function textSignature(text) {
  let value = 2166136261;
  for (const character of text) value = Math.imul(value ^ character.codePointAt(0), 16777619);
  return value >>> 0;
}

function documentView(doc, saved) {
  const tabs = {final: doc.has_final, raw: doc.has_raw};
  return {
    tab: tabs[saved?.tab] ? saved.tab : doc.has_final ? 'final' : 'raw',
    translationView: doc.has_translation && ['off', 'replace', 'segments', 'page'].includes(saved?.translationView)
      ? saved.translationView : 'off',
  };
}

function sortedDocs(docs, order = 'newest') {
  return [...docs].sort((a, b) => {
    const ta = String(a.updated_at || '');
    const tb = String(b.updated_at || '');
    if (!ta && !tb) return 0;
    if (!ta) return 1;
    if (!tb) return -1;
    return order === 'oldest' ? ta.localeCompare(tb) : tb.localeCompare(ta);
  });
}

function applyTagFilter(docs, filter = '') {
  if (!filter) return docs;
  return docs.filter(doc => Array.isArray(doc.tags) && doc.tags.includes(filter));
}
