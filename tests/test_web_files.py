import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
REQUIRED_IDS = ["topbar", "charts", "ctl-range", "ctl-from", "ctl-sort", "ctl-show", "ctl-model", "ctl-status",
                "ctl-search", "nav", "main"]


class WebFilesTests(unittest.TestCase):
    def read(self, name):
        return (WEB / name).read_text(encoding="utf-8")

    def test_page_has_the_required_element_ids(self):
        html = self.read("index.html")
        for element_id in REQUIRED_IDS:
            self.assertIn('id="%s"' % element_id, html, element_id)

    def test_page_loads_only_local_assets(self):
        html = self.read("index.html")
        self.assertIn('href="/static/style.css"', html)
        self.assertIn('src="/static/app.js"', html)
        for name in ("index.html", "style.css", "app.js"):
            text = self.read(name)
            self.assertNotRegex(text, r"""(src|href)\s*=\s*["']https?://""", name)
            self.assertNotIn("@import", text, name)
            self.assertNotRegex(text, r"url\(\s*['\"]?https?://", name)

    def test_no_html_injection_of_data(self):
        js = self.read("app.js")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, js, banned)

    def test_links_from_data_are_checked(self):
        js = self.read("app.js")
        self.assertIn("safeUrl", js)
        self.assertRegex(js, r"https\?")  # only http and https are linked

    def test_app_js_polls_only_when_visible(self):
        js = self.read("app.js")
        self.assertIn("setInterval", js)
        self.assertIn("visibilityState", js)
        self.assertRegex(js, r"fetch\(")  # still only the page's own api calls
        self.assertNotRegex(js, r"""fetch\(\s*["']https?://""")

    def test_job_page_shows_years_required_at_the_top(self):
        js = self.read("app.js")
        self.assertIn("years_required", js)
        self.assertIn("Experience required", js)
        self.assertIn("function yearsText", js)
        # placed right after the title line, before the buttons row
        self.assertLess(js.index("yearsLine(job)"), js.index("wrap.appendChild(headRow(job, models))"))

    def test_no_dependencies_or_build_step(self):
        self.assertFalse((WEB / "package.json").exists())
        self.assertFalse(list(WEB.glob("node_modules")))

    def test_sage_palette_and_base_font(self):
        css = self.read("style.css")
        self.assertIn("--green:#5d8a6b", css.replace(" ", ""))
        self.assertRegex(css, r"font:\s*15px")


if __name__ == "__main__":
    unittest.main()
