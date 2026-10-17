from fast_channels.functions.ingest import library


def test_names_of_runtime_resources() -> None:
    assert library.job_template_name("fast", "standard@0123456789") == "fast-standard_0123456789"
    assert library.vod_source_name("filler-1a2b3c", "standard@0123456789") == "filler-1a2b3c-standard_0123456789"
    long = library.vod_source_name("a" * 48, "standard@0123456789")
    assert len(long) == 50 and long == library.vod_source_name("a" * 48, "standard@0123456789")  # VodSourceName limit


def test_runtime_tags() -> None:
    assert library.runtime_tags("fast", ladder_version="standard@0123456789", correlation_id="c1", asset="x") == {
        "fast:app": "fast",
        "fast:owner": "pipeline",
        "fast:ladder": "standard@0123456789",
        "fast:correlation-id": "c1",
        "fast:asset": "x",
    }
    assert library.OWNER == "pipeline"
