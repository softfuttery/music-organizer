import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
const globalCss = readFileSync(new URL('../src/modern.css', import.meta.url), 'utf8')
const libraryCss = readFileSync(new URL('../src/library-workspace.css', import.meta.url), 'utf8')
const designCss = readFileSync(new URL('../src/design-system.css', import.meta.url), 'utf8')
const reviewCss = readFileSync(new URL('../src/review-workspace.css', import.meta.url), 'utf8')
const themeCss = readFileSync(new URL('../src/theme.css', import.meta.url), 'utf8')
const appModern = readFileSync(new URL('../src/AppModern.vue', import.meta.url), 'utf8')
const brandMark = readFileSync(new URL('../src/BrandMark.vue', import.meta.url), 'utf8')
const themePicker = readFileSync(new URL('../src/ThemePicker.vue', import.meta.url), 'utf8')
const historyWorkspace = readFileSync(new URL('../src/HistoryWorkspace.vue', import.meta.url), 'utf8')
const configWorkspace = readFileSync(new URL('../src/ConfigWorkspace.vue', import.meta.url), 'utf8')
const adminCss = readFileSync(new URL('../src/admin-workspaces.css', import.meta.url), 'utf8')
const libraryWorkspace = readFileSync(new URL('../src/LibraryWorkspace.vue', import.meta.url), 'utf8')
const reviewWorkspace = readFileSync(new URL('../src/ReviewWorkspace.vue', import.meta.url), 'utf8')
const qbAttentionCard = readFileSync(new URL('../src/QbAttentionCard.vue', import.meta.url), 'utf8')
const apiSource = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
const mainSource = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8')
const serviceWorker = readFileSync(new URL('../public/sw.js', import.meta.url), 'utf8')
const viteConfig = readFileSync(new URL('../vite.config.js', import.meta.url), 'utf8')
const manifest = JSON.parse(readFileSync(new URL('../public/manifest.webmanifest', import.meta.url), 'utf8'))

test('the UI supports system, light, and dark appearance modes', () => {
  assert.match(html, /<meta name="color-scheme" content="light dark"\s*\/?>/)
  assert.match(globalCss, /:root\s*{[^}]*color-scheme:\s*light;/s)
  assert.match(themeCss, /:root\[data-theme="dark"\]\s*{[^}]*color-scheme:\s*dark;/s)
  assert.match(themePicker, /value: 'system', label: '跟随系统'/)
  assert.match(themePicker, /value: 'light', label: '浅色'/)
  assert.match(themePicker, /value: 'dark', label: '深色'/)
  assert.match(appModern, /<ThemePicker v-model="themePreference"/)
  assert.doesNotMatch(appModern, /<select[^>]*aria-label="外观模式"/)
})

test('dark appearance uses a dedicated brand mark and themed picker menu', () => {
  assert.match(brandMark, /class="brand-symbol-light"/)
  assert.match(brandMark, /class="brand-symbol-dark"/)
  assert.match(brandMark, /id="dark-mark-note"/)
  assert.match(themeCss, /:root\[data-theme="dark"\] \.brand-symbol-light\s*{[^}]*display:\s*none;/s)
  assert.match(themeCss, /:root\[data-theme="dark"\] \.brand-symbol-dark\s*{[^}]*display:\s*block;/s)
  assert.match(themeCss, /:root\[data-theme="dark"\] :is\(\.theme-picker-trigger, \.theme-picker-menu\)/)
})

test('dashboard light-only hotspots receive explicit dark surfaces', () => {
  assert.match(themeCss, /:root\[data-theme="dark"\] :is\(th, \.table-status\)\s*{[^}]*background:\s*#15181b;/s)
  assert.match(themeCss, /:root\[data-theme="dark"\] tbody tr:hover\s*{[^}]*background:/s)
  assert.match(themeCss, /:root\[data-theme="dark"\] \.command-card \.button\.primary\s*{[^}]*background:/s)
  assert.match(themeCss, /:root\[data-theme="dark"\] :is\(\.runtime-icon, \.folder-icon\)\s*{[^}]*background:/s)
})

test('history and config are first-class themed Vue workspaces', () => {
  assert.match(appModern, /const isHistory = window\.location\.pathname === '\/history'/)
  assert.match(appModern, /const isConfig = window\.location\.pathname === '\/config'/)
  assert.match(appModern, /<HistoryWorkspace v-else-if="isHistory"/)
  assert.match(appModern, /<ConfigWorkspace v-else-if="isConfig"/)
  assert.match(appModern, /active: !isReview && !isLibrary && !isHistory && !isConfig/)
  assert.match(historyWorkspace, /getHistory/)
  assert.match(configWorkspace, /saveConfig/)
  assert.match(adminCss, /:root\[data-theme="dark"\]/)
})

test('library lyric candidate titles keep an explicit readable foreground', () => {
  assert.match(libraryCss, /\.library-candidates button\s*{[^}]*color:\s*#272923;/s)
  assert.match(libraryCss, /\.library-candidates strong\s*{[^}]*color:\s*#272923;/s)
})

test('dark theme targets the active library and lyric workspace classes', () => {
  const activeClasses = [
    'library-drawer',
    'library-toolbar',
    'library-list-panel',
    'library-folder',
    'library-track',
    'library-editor-section',
    'library-editor-tabs',
    'library-synced-lyrics',
    'library-plain-lyrics',
    'library-action-feedback',
    'folder-toggle',
    'track-open',
    'lyrics-preview',
    'lyrics-decision-panel',
    'lyrics-search-box',
    'lyrics-tabs',
  ]
  for (const className of activeClasses) {
    assert.ok(themeCss.includes(`.${className}`), `missing dark theme selector: .${className}`)
  }

  const removedClasses = [
    'library-panel',
    'library-editor',
    'library-row',
    'library-folder-card',
    'library-track-card',
    'activity-row',
    'library-meta',
  ]
  for (const className of removedClasses) {
    assert.doesNotMatch(themeCss, new RegExp(`\\.${className}(?![-_a-zA-Z0-9])`))
  }
})

test('dark theme overrides review specificity hotspots and residual light surfaces', () => {
  for (const tone of ['strong', 'good', 'weak', 'unmatched', 'manual', 'legacy']) {
    assert.match(
      themeCss,
      new RegExp(`:root\\[data-theme="dark"\\] \\.decision-editor \\.mapping-edit-row\\.match-${tone}\\s*\\{[^}]*background:`),
    )
  }

  assert.match(themeCss, /:root\[data-theme="dark"\] \.folder-row \.folder-main\s*{[^}]*background:/s)
  assert.match(
    themeCss,
    /:root\[data-theme="dark"\] \.folder-row \.folder-main :is\(\.folder-expand, \.folder-open\)\s*{[^}]*color:\s*var\(--ink\);[^}]*background:\s*transparent;/s,
  )
  assert.match(themeCss, /\.decision-editor :is\(\.mapping-edit-row code, \.archive-cleanup code\)\s*{[^}]*color:\s*var\(--ink\);/s)
  assert.match(themeCss, /\.decision-editor \.target-file code\s*{[^}]*color:\s*#8cdaa9;/s)
  assert.match(themeCss, /\.library-list-heading\s*{[^}]*border-color:\s*var\(--line\);/s)

  for (const className of [
    'candidate-card',
    'folder-files',
    'library-danger-zone',
    'folder-trash',
    'track-trash',
    'skip-button',
    'lyrics-sources',
    'lyrics-plain',
    'library-list-heading',
  ]) {
    assert.ok(themeCss.includes(`.${className}`), `missing residual dark surface: .${className}`)
  }
})

test('shared typography and touch controls use one design foundation', () => {
  assert.match(designCss, /--font-sans:/)
  assert.match(designCss, /--control-height:\s*40px/)
  assert.match(designCss, /--touch-size:\s*44px/)
  assert.match(designCss, /\.review-primary/)
  assert.match(designCss, /\.library-primary/)
})

test('the frontend exposes an installable standalone manifest', () => {
  assert.match(html, /rel="manifest" href="\/manifest\.webmanifest"/)
  assert.equal(manifest.display, 'standalone')
  assert.equal(manifest.lang, 'zh-CN')
  assert.ok(manifest.icons.length > 0)
})

test('service worker shell cache follows a content-derived build version', () => {
  assert.match(viteConfig, /createHash\('sha256'\)/)
  assert.match(viteConfig, /public\/manifest\.webmanifest/)
  assert.match(viteConfig, /public\/app-icon\.svg/)
  assert.match(mainSource, /\/sw\.js\?v=\$\{cacheVersion\}/)
  assert.match(serviceWorker, /searchParams\.get\('v'\)/)
  assert.doesNotMatch(serviceWorker, /music-organizer-shell-v1/)
})

test('frontend housekeeping keeps active state paths only', () => {
  assert.doesNotMatch(appModern, /authenticationEnabled/)
  assert.doesNotMatch(apiSource, /function getLibraryTracks/)
  assert.doesNotMatch(globalCss, /\.settings-button/)
  assert.match(appModern, /onUnauthorized\(\(\) => \{[\s\S]*markSignedOut/)
  assert.match(reviewWorkspace, /artistAliases:\s*\[\.\.\.new Set\(/)
  assert.match(libraryWorkspace, /String\(tagForm\.value\[key\] \?\? ''\)/)
})

test('mobile review actions do not cover auxiliary file controls', () => {
  assert.match(
    reviewCss,
    /@media \(max-width: 520px\)[\s\S]*?\.decision-editor\s*{[^}]*overflow:\s*visible;/,
  )
  assert.match(
    reviewCss,
    /@media \(max-width: 520px\)[\s\S]*?\.decision-bar\s*{[^}]*position:\s*static;/,
  )
  assert.match(
    reviewCss,
    /@media \(max-width: 520px\)[\s\S]*?\.auxiliary-files summary\s*{[^}]*min-height:\s*48px;/,
  )
})

test('narrow review cards reflow actions using their own container width', () => {
  assert.match(reviewCss, /\.album-review\s*{[^}]*container-type:\s*inline-size;/s)
  assert.match(
    reviewCss,
    /@container \(max-width: 620px\)[\s\S]*?\.manual-import-entry\s*>\s*div,[\s\S]*?flex:\s*0 0 auto;/,
  )
})

test('review workspace and editors respond to their actual container width', () => {
  assert.match(
    reviewCss,
    /@container workspace \(max-width:\s*900px\)[\s\S]*?\.review-layout\s*{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);/,
  )
  assert.match(
    reviewCss,
    /@container \(max-width:\s*800px\)[\s\S]*?\.manual-track-row\s*{[^}]*grid-template-columns:\s*1fr 1fr 70px 70px;/,
  )
  assert.match(
    reviewCss,
    /@container \(max-width:\s*760px\)[\s\S]*?\.mapping-edit-row\s*{[^}]*grid-template-columns:\s*minmax\(140px,\s*\.8fr\) minmax\(180px,\s*1fr\);/,
  )
})

test('mobile review heading keeps its decorative folder icon in the title row', () => {
  assert.match(
    reviewCss,
    /@media \(max-width:\s*520px\)[\s\S]*?\.review-heading\s*{[^}]*flex-direction:\s*row;/,
  )
  assert.match(
    reviewCss,
    /@media \(max-width:\s*520px\)[\s\S]*?\.archive-result\s*{[^}]*flex-direction:\s*row;/,
  )
})

test('mobile layouts do not force a classic scrollbar beyond the viewport', () => {
  assert.doesNotMatch(globalCss, /html\s*{[^}]*min-width:\s*320px;/s)
  assert.doesNotMatch(globalCss, /body\s*{[^}]*min-width:\s*320px;/s)
})

test('mobile qBittorrent attention cards retain field labels without a table header', () => {
  for (const label of ['种子', '失败次数', '最近错误', '操作']) {
    assert.match(qbAttentionCard, new RegExp(`data-label="${label}"`))
  }
})

test('review browser exposes an explicit empty root state', () => {
  assert.match(reviewWorkspace, /v-else-if="!browser\.roots\.length"/)
  assert.match(reviewWorkspace, /尚未配置可浏览的 Inbox 目录/)
  assert.match(reviewWorkspace, /v-else-if="browser\.roots\.length && browser\.current"/)
  assert.match(reviewWorkspace, /当前目录没有可选择的子目录/)
})

test('mobile lyric search uses the drawer as its single vertical scroller', () => {
  assert.match(
    reviewCss,
    /@media \(max-width:\s*520px\)[\s\S]*?\.lyrics-candidates\s*{[^}]*max-height:\s*none;[^}]*overflow-y:\s*visible;/,
  )
  assert.match(
    libraryCss,
    /@media \(max-width:\s*520px\)[\s\S]*?\.library-candidates\s*{[^}]*max-height:\s*none;[^}]*overflow-y:\s*visible;/,
  )
})

test('desktop review search actions wrap when the card is narrower than the viewport', () => {
  assert.match(reviewCss, /\.search-row\s*{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap;/s)
  assert.match(reviewCss, /\.search-row button\s*{[^}]*flex:\s*1 1 160px;/s)
  assert.match(reviewCss, /\.manual-import-entry\s*{[^}]*flex-wrap:\s*wrap;/s)
})

test('desktop workspaces respond to available content width instead of viewport width', () => {
  assert.match(globalCss, /\.workspace\s*{[^}]*container:\s*workspace\s*\/\s*inline-size;/s)
  assert.match(globalCss, /\.metric-card\s*{[^}]*container-type:\s*inline-size;/s)
  assert.match(globalCss, /\.metric-card\s*>\s*strong\s*{[^}]*font-size:\s*clamp\(28px,\s*23cqi,\s*50px\);/s)
  assert.match(globalCss, /@container workspace \(max-width:\s*1080px\)[\s\S]*?\.command-card\s*{[^}]*grid-column:\s*span 12;/)
  assert.match(libraryCss, /@container workspace \(max-width:\s*1080px\)[\s\S]*?\.library-toolbar\s*{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\) auto auto;/)
})

test('library lyrics expose editable manual search fields in provider order', () => {
  assert.match(libraryWorkspace, /v-model="lyricQuery\.title"/)
  assert.match(libraryWorkspace, /v-model="lyricQuery\.artist"/)
  assert.match(
    libraryWorkspace,
    /lyricSources\.netease[\s\S]*lyricSources\.qqmusic[\s\S]*lyricSources\.kugou/,
  )
})
