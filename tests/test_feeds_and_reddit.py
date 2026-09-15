"""Reddit fallback path and RSS/Atom feeds with transcript and link following."""
from harness_fleet import discover
from harness_fleet.discover import fetch_feed, search_reddit_pullpush

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="http://podcastindex.org/namespace/1.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example Podcast</title>
    <item>
      <title>Kafka migrations at scale</title>
      <link>https://example.com/episodes/42</link>
      <pubDate>Tue, 03 Sep 2026 20:00:00 +0000</pubDate>
      <description>An excerpt about deliveries.</description>
      <podcast:transcript type="text/html" url="https://example.com/episodes/42/transcript" />
    </item>
    <item>
      <title>Second episode</title>
      <link>https://example.com/episodes/41</link>
      <pubDate>Tue, 25 Aug 2026 19:00:00 +0000</pubDate>
      <description>Another excerpt.</description>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Newsletter</title>
  <entry>
    <title>Inside a platform team</title>
    <link rel="alternate" href="https://example.com/p/platform-team" />
    <published>2026-09-15T15:41:25Z</published>
    <summary>Short summary of the post.</summary>
  </entry>
</feed>
"""

PULLPUSH = {"data": [
    {"title": "Who did your Kafka implementation?", "selftext": "We used Trace3 for ours.",
     "subreddit": "devops", "permalink": "/r/devops/comments/abc/who_did_your_kafka/",
     "created_utc": 1788498836.0, "over_18": False},
    {"title": "unrelated", "selftext": "", "subreddit": "devops", "permalink": "/r/devops/comments/def/x/",
     "over_18": True},
]}


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Client:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def get(self, *a, **k):
        return _Resp(self._payload, self.text)

    def close(self):
        pass


def test_pullpush_search_returns_prose_hits_and_skips_adult():
    hits = search_reddit_pullpush("kafka implementation", max_results=5, client=_Client(PULLPUSH))
    assert len(hits) == 1
    assert hits[0].backend == "reddit-pullpush"
    assert hits[0].url == "https://www.reddit.com/r/devops/comments/abc/who_did_your_kafka/"
    assert "Trace3" in hits[0].snippet


def test_pullpush_failure_is_empty_not_fatal():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("mirror down")

        def close(self):
            pass

    assert search_reddit_pullpush("q", client=Boom()) == []


def test_feed_items_keep_dates_and_are_entity_safe(monkeypatch):
    monkeypatch.setattr(discover, "fetch_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    records = fetch_feed("https://example.com/feed", client=_Client(text=RSS))
    assert [r.title for r in records] == ["Kafka migrations at scale", "Second episode"]
    assert records[0].metadata["published"] == "2026-09-03T20:00:00+00:00"
    assert records[0].metadata["captured_at"] == records[0].metadata["published"]
    assert records[0].source_uri == "https://example.com/episodes/42"
    assert records[0].metadata["source"] == "feed"
    assert "An excerpt about deliveries." in records[0].text


def test_feed_follows_podcast_transcripts(monkeypatch):
    from harness_fleet.discover import RawRecord

    monkeypatch.setattr(
        discover, "fetch_text",
        lambda url, **k: RawRecord(text="Full transcript " * 40, source_uri=url),
    )
    records = fetch_feed("https://example.com/feed", client=_Client(text=RSS), follow_links=False)
    transcripts = [r for r in records if r.metadata.get("source") == "feed-transcript"]
    assert len(transcripts) == 1
    assert transcripts[0].source_uri == "https://example.com/episodes/42/transcript"
    assert transcripts[0].metadata["captured_at"] == "2026-09-03T20:00:00+00:00"


def test_feed_follows_the_item_link_for_the_full_article(monkeypatch):
    from harness_fleet.discover import RawRecord

    monkeypatch.setattr(
        discover, "fetch_text",
        lambda url, **k: RawRecord(text="The full argument in prose. " * 50, source_uri=url),
    )
    records = fetch_feed("https://example.com/feed", client=_Client(text=ATOM))
    # One record per item: the followed page replaces the thin feed excerpt.
    assert len(records) == 1
    assert records[0].metadata["source"] == "feed-item"
    assert records[0].source_uri == "https://example.com/p/platform-team"
    assert records[0].metadata["published"] == "2026-09-15T15:41:25+00:00"
    assert len(records[0].text.split()) > 100


def test_malformed_feed_is_empty():
    assert fetch_feed("https://example.com/feed", client=_Client(text="<html>not a feed</html>")) == []
