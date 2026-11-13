import pytest

from agent.collector import FetchError, html_to_text, parse_feed, parse_sitemap
from tests.conftest import pricing_html, rss


def test_html_to_text_keeps_content_drops_chrome():
    title, text = html_to_text(pricing_html([("Pro", "$20/month - 750 tasks")]) .replace(
        "</main>", "<table><tr><th>Plan</th><th>Price</th></tr>"
                   "<tr><td>Pro</td><td>$20</td></tr></table></main>"))
    assert title == "Pricing"
    assert "## Pro $20/month - 750 tasks" in text
    assert "Pro | $20" in text
    assert "cookies" not in text and "Login" not in text and "Example Inc" not in text


def test_html_to_text_glues_split_prices():
    _, text = html_to_text("<main><div><span>$</span><span>19</span><span>/mo</span>"
                           "</div><p>Professional plan for growing teams</p></main>")
    assert "$19/mo" in text


def test_fetch_respects_robots(site, collector):
    site.add("https://ex.com/robots.txt", "User-agent: *\nDisallow: /private", ctype="text/plain")
    site.add("https://ex.com/private/pricing", "<main><p>secret prices here</p></main>")
    with pytest.raises(FetchError, match="robots.txt"):
        collector.fetch_page("https://ex.com/private/pricing")
    assert "https://ex.com/private/pricing" not in site.hits


def test_fetch_retries_then_reports(site, collector):
    site.add("https://ex.com/pricing", "busy", status=503)
    with pytest.raises(FetchError, match="HTTP 503"):
        collector.fetch_page("https://ex.com/pricing")
    assert site.hits.count("https://ex.com/pricing") == 2  # 1 try + 1 retry


def test_http_errors_and_empty_pages(site, collector):
    site.add("https://ex.com/gone", "x", status=404)
    with pytest.raises(FetchError, match="404"):
        collector.fetch_page("https://ex.com/gone")
    site.add("https://ex.com/js", "<html><body><div id='root'></div></body></html>")
    with pytest.raises(FetchError, match="render: true"):
        collector.fetch_page("https://ex.com/js")


def test_parse_feed():
    body = rss([("https://ex.com/a", "Launching AI agents", "Tue, 10 Nov 2026 10:00:00 GMT"),
                ("https://ex.com/b", "Tips &amp; tricks", "Mon, 09 Nov 2026 10:00:00 GMT")])
    items = parse_feed(body, "https://ex.com/feed", 10)
    assert [i.title for i in items] == ["Launching AI agents", "Tips & tricks"]
    assert items[0].published_ts == 1794304800
    assert items[0].summary == "About Launching AI agents."


def test_parse_feed_rejects_html():
    with pytest.raises(FetchError):
        parse_feed("<html><body>not a feed</body></html>", "https://ex.com/feed", 10)


def test_parse_sitemap_with_include_filter():
    body = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/blog/new-ai-agents</loc><lastmod>2026-11-10</lastmod></url>
      <url><loc>https://ex.com/blog/old-post</loc><lastmod>2025-01-01T10:00:00+00:00</lastmod></url>
      <url><loc>https://ex.com/about</loc></url></urlset>"""
    items = parse_sitemap(body, "/blog/", 10)
    assert [i.title for i in items] == ["New ai agents", "Old post"]
    assert items[0].published_ts > items[1].published_ts


def test_future_publish_date_is_ignored():
    items = parse_feed(rss([("https://x.test/a", "From the future", "Wed, 10 Nov 2077 05:00:00 GMT")]),
                       "https://x.test/feed", 10)
    assert items[0].published_ts is None  # stored with "first seen" instead
