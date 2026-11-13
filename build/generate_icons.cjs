// Copyright (c) 2026 Ansel
// SPDX-License-Identifier: MPL-2.0
// Render the canonical FoilPage vector mark into PNG/ICO frames.
// Small tray/extension sizes use a simpler optical variant from foilpage-small.svg.
// Requires the project's pinned Playwright/Chromium. Run: node build/generate_icons.cjs
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const iconDir = path.join(root, 'assets', 'icons');
const crypto = require('node:crypto');
const sourcePath = path.join(iconDir, 'foilpage-source.svg');
const smallSourcePath = path.join(iconDir, 'foilpage-small.svg');
const source = fs.readFileSync(sourcePath);
const smallSource = fs.readFileSync(smallSourcePath);
const svg = source.toString('utf8');
const smallSvg = smallSource.toString('utf8');
const icoSizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256];
const opticalSmallSizes = new Set([16, 20, 24]);

function packIco(frames) {
  const header = Buffer.alloc(6 + 16 * frames.length);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(frames.length, 4);
  let offset = header.length;
  frames.forEach(({size, data}, index) => {
    const entry = 6 + 16 * index;
    header[entry] = size === 256 ? 0 : size;
    header[entry + 1] = size === 256 ? 0 : size;
    header.writeUInt16LE(1, entry + 4);
    header.writeUInt16LE(32, entry + 6);
    header.writeUInt32LE(data.length, entry + 8);
    header.writeUInt32LE(offset, entry + 12);
    offset += data.length;
  });
  return Buffer.concat([header, ...frames.map(frame => frame.data)]);
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({deviceScaleFactor: 1});
    async function render(size, markSvg = svg) {
      await page.setViewportSize({width: size, height: size});
      await page.setContent('<style>html,body{margin:0;background:transparent}svg{display:block;width:' +
        size + 'px;height:' + size + 'px}</style>' + markSvg);
      return page.locator('svg').screenshot({omitBackground: true});
    }
    const master = await render(512);
    const rasterTargets = [
      path.join(iconDir, 'foilpage.png'),
      path.join(root, 'app', 'webview_ui', 'assets', 'foilpage.png'),
      // Compatibility aliases for existing integrations and older local shortcuts.
      path.join(iconDir, 'shengjian.png'),
      path.join(root, 'app', 'webview_ui', 'assets', 'shengjian.png'),
    ];
    for (const file of rasterTargets) fs.writeFileSync(file, master);
    for (const name of ['foilpage.svg', 'shengjian.svg']) {
      fs.writeFileSync(path.join(iconDir, name), svg);
    }
    fs.writeFileSync(path.join(iconDir, 'shengjian-small.svg'), smallSvg);
    const frames = [];
    for (const size of icoSizes) {
      const markSvg = opticalSmallSizes.has(size) ? smallSvg : svg;
      const data = await render(size, markSvg);
      frames.push({size, data});
      if ([16, 32, 48, 128].includes(size)) {
        fs.writeFileSync(path.join(root, 'browser-extension', 'icons', 'icon-' + size + '.png'), data);
      }
    }
    const ico = packIco(frames);
    fs.writeFileSync(path.join(iconDir, 'foilpage.ico'), ico);
    // Keep the old filename as a byte-identical compatibility alias until all external shortcuts migrate.
    fs.writeFileSync(path.join(iconDir, 'shengjian.ico'), ico);
    const audit = path.join(root, '.ui-audit');
    fs.mkdirSync(audit, {recursive: true});
    fs.writeFileSync(path.join(audit, 'icon-build.json'), JSON.stringify({
      name: 'FoilPage', source: 'assets/icons/foilpage-source.svg', sourceSize: [512,512],
      sourceSha256: crypto.createHash('sha256').update(source).digest('hex'),
      opticalSmallSource: 'assets/icons/foilpage-small.svg',
      opticalSmallSourceSha256: crypto.createHash('sha256').update(smallSource).digest('hex'),
      renderViewBox: [0,0,512,512], svgKind: 'native vector', opticalSmallSizes: [...opticalSmallSizes],
      icoSizes, pngSize: 512, transparent: true,
    }, null, 2));
    process.stdout.write('FoilPage mark: main and small optical vectors rendered to transparent PNG/ICO assets.\n');
  } finally {
    await browser.close();
  }
})().catch(error => {
  process.stderr.write(error.stack + '\n');
  process.exitCode = 1;
});
