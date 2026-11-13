import json

from agent.models import SourceSpec
from agent.sources import load_sources, load_yaml, parse_rows, specs_to_rows

YAML = """
competitors:
  - name: Zapier
    sources:
      - {type: pricing, url: "https://zapier.com/pricing"}
      - {type: blog, url: "https://zapier.com/blog/feeds/latest/", format: rss}
  - name: Broken
    sources:
      - {type: pricing, url: "not-a-url"}
      - {type: weird, url: "https://ex.com"}
"""


def test_load_yaml_reports_bad_entries(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(YAML, encoding="utf-8")
    result = load_yaml(path)
    assert [s.type for s in result.specs] == ["pricing", "blog"]
    assert len(result.errors) == 2
    assert result.errors[0].startswith("Broken source 1: url")


def test_project_sources_file_is_valid():
    result = load_yaml("sources.yaml")
    assert not result.errors
    assert {s.competitor for s in result.specs} >= {"Zapier", "n8n"}


def test_parse_rows_from_sheet():
    rows = [
        {"Competitor": "Zapier", "Type": "Pricing", "URL": "https://zapier.com/pricing",
         "Format": "", "Render": "", "Ignore": "trusted by \\d+ | banner", "Include": "",
         "Active": "yes"},
        {"Competitor": "", "Type": "", "URL": "", "Format": "", "Render": "", "Ignore": "",
         "Include": "", "Active": ""},                                       # blank row
        {"Competitor": "Make", "Type": "pricing", "URL": "make.com/pricing", "Active": ""},
        {"Competitor": "n8n", "Type": "blog", "URL": "https://blog.n8n.io/rss/",
         "Format": "RSS", "Active": "no"},
    ]
    specs, errors = parse_rows(rows)
    assert [s.competitor for s in specs] == ["Zapier", "n8n"]
    assert specs[0].ignore == ["trusted by \\d+", "banner"]
    assert specs[1].format == "rss" and specs[1].active is False
    assert errors == ["Row 4: url: Value error, must be a full http(s) web address"]


def test_sheet_fallback_to_cache_then_yaml(tmp_path, storage):
    path = tmp_path / "sources.yaml"
    path.write_text(YAML, encoding="utf-8")
    good = [{"competitor": "IFTTT", "type": "pricing", "url": "https://ifttt.com/plans"}]

    result = load_sources(str(path), storage, lambda: good)
    assert result.origin == "sheet" and result.specs[0].competitor == "IFTTT"
    assert json.loads(storage.get_kv("sheet_sources_cache"))[0]["competitor"] == "IFTTT"

    def broken():
        raise ConnectionError("offline")

    result = load_sources(str(path), storage, broken)
    assert result.origin == "sheet-cache" and result.specs[0].competitor == "IFTTT"
    assert "Could not read the Competitors sheet" in result.errors[0]

    storage.set_kv("sheet_sources_cache", "")
    result = load_sources(str(path), storage, broken)
    assert result.origin == "yaml" and result.specs[0].competitor == "Zapier"


def test_specs_round_trip_through_sheet_rows():
    spec = SourceSpec(competitor="Zapier", type="pricing", url="https://zapier.com/pricing",
                      ignore=["a", "b"])
    from agent.sources import SHEET_COLUMNS
    row = dict(zip(SHEET_COLUMNS, specs_to_rows([spec])[0]))
    (back,), errors = parse_rows([row])
    assert not errors and back == spec
