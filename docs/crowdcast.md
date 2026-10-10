# Testing against CrowdCast

CrowdCast is a native desktop recorder. This browser companion does not patch its Rust recorder or collect global desktop input. We use an existing local CrowdCast segment as a timestamped input fixture for the browser pipeline.

```sh
python3 -m venv .venv
.venv/bin/pip install msgpack
.venv/bin/python scripts/crowdcast-fixture.py /path/to/input_SEGMENT.msgpack
CROWDCAST_FIXTURE=artifacts/crowdcast-trace.json npm run test:browser
```

The converter supports named-map and compact-array CrowdCast InputEvent records. It keeps `raw_timestamp_us`, not the video-aligned `timestamp_us`. It converts key presses/releases and mouse moves/presses/releases; raw-timestamped redactions become test boundaries. Records without a raw clock are skipped and counted. MouseScroll and other record types are out of scope for this fixture. Source repeat flags are unavailable, so this fixture must not be used to validate repeat/hold inference. Browser-native tests cover repeats separately.

The fixture contains physical key identifiers and coordinates and is private: it is written mode 0600 under gitignored `artifacts/`. Do not commit it. Only aggregate test results belong in public reports.

The test re-dispatches the recorded input as synthetic DOM events with original relative timing fields, without waiting in real time. It checks that supported samples and timestamps survive batching without loss. Screen coordinates are carried into test events without viewport calibration: this verifies the data path, not geometric alignment with an original screen recording. It does not measure a live browser's hardware sampling rate or validate native PostHog ingestion.
