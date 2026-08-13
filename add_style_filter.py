#!/usr/bin/env python3
"""
Add a Style (Full Color / 1-Color) filter to all team pages.

For each team HTML:
  1. Adds data-style="full-color" or data-style="1-color" to every .logo-tile
     based on its .logo-tile-label text.
  2. Inserts a Style filter group into .logo-filters (after Format).
  3. Adds CSS for the Style filter active state.
  4. Adds JS for the Style filter handler.
"""

import re
import sys
from pathlib import Path

TEAMS_DIR = Path(__file__).parent / 'teams'

# ── CSS to inject (before closing </style>) ────────────────────────────────
STYLE_CSS = (
    '  .filter-btn.active[data-filter="style"][data-value="full-color"] '
    '{ color: var(--white); background: var(--team-primary); border-color: var(--team-primary); }\n'
    '  .filter-btn.active[data-filter="style"][data-value="1-color"]    '
    '{ color: var(--black); background: var(--white); border-color: var(--white); }\n'
)

# ── Filter group HTML to insert after the Format filter group ─────────────
STYLE_FILTER_HTML = '''\
          <div class="filter-group">
            <span class="filter-label">Style</span>
            <button class="filter-btn active" data-filter="style" data-value="all">All</button>
            <button class="filter-btn" data-filter="style" data-value="full-color">Full Color</button>
            <button class="filter-btn" data-filter="style" data-value="1-color">1-Color</button>
          </div>'''

# ── JS to insert after the FORMAT FILTER block ─────────────────────────────
STYLE_FILTER_JS = '''\

  // ── STYLE FILTER ─────────────────────────────────────────────────────
  document.querySelectorAll('.filter-btn[data-filter="style"]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn[data-filter="style"]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const val = btn.dataset.value;
      document.querySelectorAll('.logo-tile').forEach(tile => {
        tile.classList.toggle('hidden', val !== 'all' && tile.dataset.style !== val);
      });
    });
  });
'''


def classify_label(label_text: str) -> str:
    """Return 'full-color' or '1-color' based on the tile label text."""
    t = label_text.lower()
    if '1-color' in t or '1 color' in t or '1color' in t:
        return '1-color'
    return 'full-color'


def patch_file(path: Path) -> bool:
    html = path.read_text(encoding='utf-8')
    original = html

    # ── 1. Add data-style to every .logo-tile ─────────────────────────────
    # Each tile looks like:
    #   <div class="logo-tile" ...>
    #     ...
    #     <span class="logo-tile-label">Full Color</span>  (or 1-Color…)
    #     ...
    #   </div>
    # Strategy: find every occurrence of class="logo-tile" that doesn't
    # already have data-style, then look forward to the next logo-tile-label
    # to classify it.

    def add_data_style(m: re.Match) -> str:
        tile_open = m.group(0)
        # Already patched?
        if 'data-style=' in tile_open:
            return tile_open
        # Find the label text that follows this opening tag in the full HTML
        rest = html[m.end():]
        lm = re.search(r'class="logo-tile-label"[^>]*>(.*?)</span>', rest, re.DOTALL)
        if lm:
            raw = re.sub(r'<[^>]+>', ' ', lm.group(1))  # strip inner tags like <br/>
            style = classify_label(raw)
        else:
            style = 'full-color'
        return tile_open.rstrip('>').rstrip() + f' data-style="{style}">'

    # Match the opening div tag of each logo-tile (may span attributes).
    # Use a class pattern that matches "logo-tile" as a whole class token,
    # not as a prefix of "logo-tile-img", "logo-tile-meta", etc.
    # "logo-tile" must be at the start of the class string (or preceded by space)
    # AND followed by a space or the closing quote — not a hyphen.
    html = re.sub(
        r'<div\s[^>]*class="(?:[^"]*\s)?logo-tile(?:\s[^"]*)?"[^>]*>',
        add_data_style,
        html
    )

    # ── 2. Insert Style filter group HTML ─────────────────────────────────
    # Find the Format filter-group closing tag and insert after it.
    # The Format group ends with </div> following the Format label.
    if 'data-filter="style"' not in html:
        # Find the last </div> that closes the Format filter-group
        # Pattern: data-filter="format" ... </div>  (the enclosing filter-group)
        fmt_pattern = re.compile(
            r'(<div[^>]*class="filter-group"[^>]*>.*?'
            r'<span[^>]*class="filter-label"[^>]*>Format</span>.*?</div>)',
            re.DOTALL
        )
        m = fmt_pattern.search(html)
        if m:
            html = html[:m.end()] + '\n' + STYLE_FILTER_HTML + html[m.end():]
        else:
            print(f'  [warn] Could not find Format filter group in {path.name}')

    # ── 3. Insert CSS ──────────────────────────────────────────────────────
    if 'data-filter="style"' in STYLE_CSS and STYLE_CSS not in html:
        # Insert before the first </style>
        html = html.replace('</style>', STYLE_CSS + '</style>', 1)

    # ── 4. Insert JS ──────────────────────────────────────────────────────
    if '// ── STYLE FILTER' not in html:
        # Insert after the FORMAT FILTER block (ends just before // ── PRESS KIT)
        press_kit_marker = '// ── PRESS KIT'
        if press_kit_marker in html:
            html = html.replace(press_kit_marker, STYLE_FILTER_JS + '  ' + press_kit_marker, 1)
        else:
            # Fallback: insert before closing </script>
            html = html.replace('</script>', STYLE_FILTER_JS + '</script>', 1)

    if html == original:
        return False
    path.write_text(html, encoding='utf-8')
    return True


def main():
    files = sorted(TEAMS_DIR.glob('*.html'))
    if not files:
        print(f'No HTML files found in {TEAMS_DIR}')
        sys.exit(1)

    changed = 0
    for f in files:
        ok = patch_file(f)
        status = '✓' if ok else '–'
        print(f'  {status} {f.name}')
        if ok:
            changed += 1

    print(f'\nDone. {changed}/{len(files)} files patched.')


if __name__ == '__main__':
    main()
