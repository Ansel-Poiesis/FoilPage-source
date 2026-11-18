// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
'use strict';

window.MaterialNavigation = (() => {
  const history = [];
  const modules = {workbench: 'Workbench', videos: 'Videos', library: 'Library'};
  const labels = {workbench: '任务', videos: '视频', library: '文本库', import: '导入'};
  let lookupRequest = 0;

  function entered(view, navigation) {
    ++lookupRequest;
    const previous = AppState.currentView;
    const context = window[modules[previous]]?.snapshot?.();
    if (navigation?.back) history.pop();
    else if (navigation?.target && previous !== view) history.push({view: previous, context});
    else history.length = 0;
    const back = document.getElementById('materialBack');
    const destination = history.at(-1);
    back.hidden = !destination;
    back.title = back.getAttribute('aria-label') || '';
    if (destination) {
      back.title = '返回' + labels[destination.view];
      back.setAttribute('aria-label', back.title);
    }
    back.onclick = () => {
      const entry = history.at(-1);
      if (entry) switchView(entry.view, {back: true, target: entry.context});
    };
  }

  function go(view, target) {
    switchView(view, {target});
  }

  function isVideo(path) {
    return /\.(mp4|mkv|mov|avi|wmv|flv|m4v|mpeg|mpg|ts|mts|m2ts|3gp|webm)$/i.test(path || '');
  }

  async function openVideo(taskId, button) {
    const serial = ++lookupRequest, from = AppState.currentView;
    button.disabled = true;
    try {
      const result = await AppApi.videos(false);
      if (serial !== lookupRequest || AppState.currentView !== from) return;
      const current = from === 'library' ? Library.currentTaskId()
        : AppState.selected.size === 1 ? [...AppState.selected][0] : null;
      if (current !== taskId) return;
      if (!result?.ok || !Array.isArray(result.items)) throw new Error(result?.error || '无法读取视频库');
      const item = result.items.find(video => video.task_ids?.includes(taskId));
      if (!item) { toast('该素材当前未登记在视频库中', 'error'); return; }
      go('videos', {videoId: item.id});
    } catch (error) {
      if (serial === lookupRequest && AppState.currentView === from) toast(error.message || '无法定位视频', 'error');
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  return {entered, go, openVideo, isVideo};
})();
