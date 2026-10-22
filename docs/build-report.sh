#!/usr/bin/env bash
# Сборка PDF: pandoc (Markdown → HTML со встроенными картинками) + headless Chromium (HTML → PDF).
# Нужны: pandoc и Python-пакет playwright с установленным Chromium.
# Диаграммы: java -jar plantuml.jar -tpng diagrams/*.puml
set -euo pipefail
cd "$(dirname "$0")"
pandoc REPORT.md -s --toc --toc-depth=2 -N --embed-resources --css report.css --resource-path=. -o /tmp/report.html
python3 - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    pg.goto("file:///tmp/report.html")
    pg.pdf(path="ОТЧЕТ.pdf", format="A4", print_background=True,
           margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
           display_header_footer=True, header_template="<span></span>",
           footer_template='<div style="font-size:8px;width:100%;text-align:center"><span class="pageNumber"></span> / <span class="totalPages"></span></div>')
    b.close()
PY
echo "Готово: docs/ОТЧЕТ.pdf"
