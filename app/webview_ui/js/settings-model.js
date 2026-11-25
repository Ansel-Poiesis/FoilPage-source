// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
'use strict';

const ACCENTS = globalThis.FoilPageContract.ACCENTS;

const PROFILES = [
  { value: 'auto', label: '自动分类' },
  { value: 'emotional_copy', label: '情感文案' },
  { value: 'consulting', label: '咨询' },
  { value: 'knowledge', label: '知识科普' },
];

// 与 engine/settings.py 的 AGENT_PROVIDER_PRESETS 保持一致，tests/test_provider_presets.py 锁定。
const PROVIDER_PRESETS = {
  mimo:        { label: "小米 MiMo（推荐，支持视觉）", base: "https://api.xiaomimimo.com/v1", model: "mimo-v2.6-flash" },
  deepseek:    { label: "DeepSeek", base: "https://api.deepseek.com", model: "deepseek-chat" },
  openai:      { label: "OpenAI（支持视觉）", base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  bailian:     { label: "阿里云百炼", base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  zhipu:       { label: "智谱 GLM", base: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4.5-flash" },
  kimi:        { label: "Kimi 月之暗面", base: "https://api.moonshot.cn/v1", model: "kimi-k2-0905-preview" },
  siliconflow: { label: "硅基流动", base: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-72B-Instruct" },
  openrouter:  { label: "OpenRouter（支持视觉）", base: "https://openrouter.ai/api/v1", model: "openai/gpt-4o-mini" },
  ark:         { label: "火山方舟", base: "https://ark.cn-beijing.volces.com/api/v3", model: "doubao-seed-1-6-lite-250815" },
  custom:      { label: "自定义", base: "", model: "" },
};

const CUSTOM_PROFILE_TEMPLATE = {
  id: '',
  name: '新的整理策略',
  system_prompt: '你是 FoilPage 的原文保真校订编辑。保持原文顺序、语气、例子和限定条件，只修复明确的 ASR 错字、同音字、标点、断句和明显赘词；不确定时保留原词并记入文末「## 存疑」清单（每行 `- 「？」原词 [mm:ss]`），禁止摘要、重排、扩写或编造。只输出 Markdown，第一行是一级标题。',
  enabled: true,
  model: '',
  temperature: null,
};

const SUBSCRIPTION_FIELDS = new Set([
  'subscription_active', 'subscription_plan', 'subscription_expires_at', 'subscription_last_order_id',
]);

window.SettingsModel = Object.freeze({
  PROFILES,
  PROVIDER_PRESETS,
  CUSTOM_PROFILE_TEMPLATE,
  ACCENTS,
  profileOptions,
  revisionKeys,
  confirmedSettings,
  externalSettingsPatch,
});

function profileOptions(settings = {}) {
  const options = PROFILES.slice();
  (settings.agent_custom_profiles || []).forEach(profile => {
    if (profile && profile.id && profile.name && profile.enabled !== false) {
      options.push({value: 'custom:' + profile.id, label: profile.name + '（自定义）'});
    }
  });
  return options;
}

function revisionKeys(changes) {
  const keys = Object.keys(changes);
  if ((Object.hasOwn(changes, 'agent_provider') || Object.hasOwn(changes, 'agent_base_url'))
      && !Object.hasOwn(changes, 'agent_api_key')) keys.push('agent_api_key');
  return keys;
}

function confirmedSettings(changes, settings, fieldRevisions, revision, keyCleared = false) {
  const confirmed = {};
  Object.keys(changes).forEach(key => {
    if (fieldRevisions.get(key) === revision) confirmed[key] = settings[key];
  });
  if (keyCleared && fieldRevisions.get('agent_api_key') === revision) confirmed.agent_api_key = '';
  return confirmed;
}

function externalSettingsPatch(incoming, fieldRevisions, fields) {
  const confirmed = {};
  for (const [key, value] of Object.entries(incoming)) {
    if (key === 'agent_settings_revision' || key === 'data_dir' || key === 'credit_balance'
        || key.startsWith('account_') || key.startsWith('license_') || key.startsWith('agent_usage_')
        || SUBSCRIPTION_FIELDS.has(key)) continue;
    if (fieldRevisions.get(key) === fields.get(key)) confirmed[key] = value;
  }
  return confirmed;
}
