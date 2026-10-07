const fs = require('fs');
const path = require('path');
const os = require('os');
const {pathToFileURL} = require('url');

function loadPlaywright() {
  const candidates = ['playwright', 'playwright-core'];
  if (process.env.PDF_TRANSLATE_NODE_MODULES) {
    candidates.push(path.join(process.env.PDF_TRANSLATE_NODE_MODULES, 'playwright'));
  }
  candidates.push(path.join(os.homedir(), '.cache', 'codex-runtimes', 'codex-primary-runtime',
    'dependencies', 'node', 'node_modules', 'playwright'));
  for (const candidate of candidates) {
    try { return require(candidate); } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  throw new Error('Playwright is required. Install the Node playwright package or set PDF_TRANSLATE_NODE_MODULES.');
}

(async () => {
  const [input, output, executablePath] = process.argv.slice(2);
  const {chromium} = loadPlaywright();
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(90000);
    await page.goto(pathToFileURL(input).href, {waitUntil: 'load', timeout: 90000});
    await page.waitForFunction(() => !document.querySelector('script[data-mathjax-required]') ||
      !!window.MathJax?.startup?.promise, null, {timeout: 90000});
    await page.evaluate(async () => {
      await document.fonts.ready;
      if (window.MathJax?.startup?.promise) await window.MathJax.startup.promise;
      // A syntactically invalid model correction is replaced by its source crop.
      for (const error of document.querySelectorAll('[data-mml-node="merror"], mjx-merror')) {
        const wrapper = error.closest('[data-source-fallback]');
        if (wrapper) wrapper.innerHTML = wrapper.getAttribute('data-source-fallback');
      }
      await Promise.all([...document.images].map(img => {
        if (img.complete) return Promise.resolve();
        return new Promise(resolve => {
          img.addEventListener('load', resolve, {once: true});
          img.addEventListener('error', resolve, {once: true});
        });
      }));
    });
    const result = await page.evaluate(() => ({
      mathErrors: document.querySelectorAll('[data-mml-node="merror"], mjx-merror').length,
      brokenImages: [...document.images].filter(img => !img.complete || !img.naturalWidth).length,
      placeholders: /@@PDF_TRANSLATE_[A-Z_]+_\d+@@/.test(document.body.textContent),
      // Oversized formulas must not silently clip off the printable page.
      wideFormulas: [...document.querySelectorAll('mjx-container')].filter(el =>
        el.getBoundingClientRect().width > document.body.getBoundingClientRect().width + 2).length
    }));
    if (result.mathErrors || result.brokenImages || result.placeholders || result.wideFormulas) {
      throw new Error(`Render validation failed: ${JSON.stringify(result)}`);
    }
    await page.pdf({path: output, printBackground: true, preferCSSPageSize: true, displayHeaderFooter: false});
    if (!fs.statSync(output).size) throw new Error('Browser produced an empty PDF');
    console.log('PDF rendered successfully');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
