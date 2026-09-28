// Local vector rasterization only. Requires the repository's existing Playwright.
// Usage: node installer/windows/generate-icon.cjs <private-preview-directory>
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require('../../apps/frontend/node_modules/playwright')

async function main() {
  if (!process.argv[2]) throw new Error('A private preview directory is required.')
  const preview = path.resolve(process.argv[2])
  const iconPath = path.join(__dirname, 'assets', 'AnxinBoard.ico')
  const svg = await fs.readFile(path.join(__dirname, '../../apps/frontend/src/assets/anxin-icon.svg'), 'utf8')
  const sizes = [16, 24, 32, 48, 64, 128, 256]
  const images = []
  await fs.mkdir(preview, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 700 }, deviceScaleFactor: 1 })
    for (const size of sizes) {
      await page.setContent(`<style>html,body{margin:0;background:transparent}svg{display:block;width:${size}px;height:${size}px}</style>${svg}`)
      const png = await page.locator('svg').screenshot({ omitBackground: true })
      images.push(png)
      await fs.writeFile(path.join(preview, `anxin-${size}.png`), png)
    }
    const header = Buffer.alloc(6 + sizes.length * 16)
    header.writeUInt16LE(1, 2)
    header.writeUInt16LE(sizes.length, 4)
    let offset = header.length
    sizes.forEach((size, index) => {
      const entry = 6 + index * 16
      header[entry] = header[entry + 1] = size === 256 ? 0 : size
      header.writeUInt16LE(1, entry + 4)
      header.writeUInt16LE(32, entry + 6)
      header.writeUInt32LE(images[index].length, entry + 8)
      header.writeUInt32LE(offset, entry + 12)
      offset += images[index].length
    })
    await fs.mkdir(path.dirname(iconPath), { recursive: true })
    await fs.writeFile(iconPath, Buffer.concat([header, ...images]))
    const tiles = sizes.map((size, index) => `<figure><img width="${size}" height="${size}" src="data:image/png;base64,${images[index].toString('base64')}"><figcaption>${size} px</figcaption></figure>`).join('')
    const html = `<!doctype html><meta charset="utf-8"><title>Anxin Board icon preview</title><style>*{box-sizing:border-box}body{margin:0;font:13px 'Segoe UI',sans-serif;background:#dbe1e9}section{height:350px;padding:24px 28px;background:#f1f4f8;color:#334155}section.dark{background:#172334;color:#d7e1ee}h2{margin:0 0 18px;font-size:16px;font-weight:600}.sizes{display:flex;align-items:center;justify-content:space-between;gap:20px}figure{margin:0;text-align:center}img{display:block;margin:auto}figcaption{margin-top:12px}</style><section><h2>Light desktop</h2><div class="sizes">${tiles}</div></section><section class="dark"><h2>Dark desktop</h2><div class="sizes">${tiles}</div></section>`
    await fs.writeFile(path.join(preview, 'icon-preview.html'), html)
    await page.setContent(html)
    await page.screenshot({ path: path.join(preview, 'icon-preview.png'), fullPage: true })
    // Check both the ICO directory and every embedded PNG's real dimensions.
    const ico = await fs.readFile(iconPath)
    sizes.forEach((size, index) => {
      const entry = 6 + index * 16
      const start = ico.readUInt32LE(entry + 12)
      const length = ico.readUInt32LE(entry + 8)
      const png = ico.subarray(start, start + length)
      if (png.toString('hex', 0, 8) !== '89504e470d0a1a0a' || png.readUInt32BE(16) !== size || png.readUInt32BE(20) !== size) throw new Error('Invalid ICO frame.')
    })
    console.log(`ICO_FRAMES_PASS=${sizes.join(',')}`)
    console.log(`PREVIEW=${path.join(preview, 'icon-preview.png')}`)
  } finally { await browser.close() }
}
main().catch(error => { console.error(error.message); process.exitCode = 1 })
