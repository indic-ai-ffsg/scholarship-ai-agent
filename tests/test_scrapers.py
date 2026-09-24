"""The HTML-to-text layer, and the two ways it silently lost a page.

Both cases here are real pages that broke in production, reduced to the shape
that broke them. They are regression tests in the strict sense: each one failed
before the fix it guards.
"""

from src.scrapers import (
    MIN_USEFUL_CHARS,
    EMBEDDED_HEADING,
    extract_links,
    extract_text_from_html,
    join_text,
)

CONTENT_IN_CHROME = f"""
<html><body>
  <header>
    <h1>Scholarships for students with disabilities</h1>
    <p>{'The scheme supports maintenance and books. ' * 40}</p>
  </header>
  <main><p>short</p></main>
</body></html>
"""

ORDINARY_PAGE = f"""
<html><body>
  <nav><a href="/a">Home</a><a href="/b">About</a></nav>
  <main><p>{'This scheme is open to students in Class 9 and 10. ' * 40}</p></main>
  <footer><p>Contact us</p></footer>
</body></html>
"""


def test_content_wrapped_in_chrome_survives():
    visible, _ = extract_text_from_html(CONTENT_IN_CHROME)
    assert len(visible) >= MIN_USEFUL_CHARS
    assert "maintenance and books" in visible


def test_chrome_is_still_stripped_when_the_page_can_spare_it():
    visible, _ = extract_text_from_html(ORDINARY_PAGE)
    assert "Class 9 and 10" in visible
    assert "Contact us" not in visible


def test_the_embedded_island_is_labelled_when_both_halves_exist():
    joined = join_text("the page says 19 September 2026", "applicationDeadline: 2026-07-31")
    assert EMBEDDED_HEADING in joined
    assert joined.index("19 September") < joined.index(EMBEDDED_HEADING)


def test_one_half_alone_gets_no_heading():
    assert join_text("visible only", "") == "visible only"
    assert join_text("", "island only") == "island only"


def test_links_are_same_host_and_labelled():
    html = """<a href="/scheme/one">Scheme One</a>
              <a href="https://elsewhere.example/x">Off site</a>
              <a href="/login">Sign in</a>"""
    links = extract_links(html, "https://sponsor.example/schemes")
    assert any("Scheme One" in l for l in links)
    assert not any("elsewhere.example" in l for l in links)
    assert not any("/login" in l for l in links)
