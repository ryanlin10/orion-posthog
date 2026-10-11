"""PostHog API client: file exports for bulk event pulls, the query API for small lookups.

PostHog asks third-party tools not to use /query for exports, so `pull` uses file download exports
(at most one week per export). /query is only used for one session's labels and for label search.
"""
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

US_HOST = "https://us.posthog.com"
EXPORT_DONE = "Completed"
EXPORT_FAILED = {"Failed", "FailedBilling", "Cancelled"}


class PostHogError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Export downloads redirect to a signed URL that must be fetched WITHOUT the API key.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ui_host_for(api_host):
    """The app host (for replay links) for an API host; ingestion hosts like us.i.posthog.com map to us.posthog.com."""
    parsed = urllib.parse.urlparse(api_host)
    return f"{parsed.scheme}://{parsed.netloc.replace('.i.posthog.com', '.posthog.com')}"


class PostHog:
    def __init__(self, host=None, project_id=None, api_key=None, ui_host=None, sleep=time.sleep, poll_seconds=5):
        self.host = (host or os.environ.get("POSTHOG_HOST") or US_HOST).rstrip("/")
        self.project_id = str(project_id or os.environ.get("POSTHOG_PROJECT_ID") or "")
        self.api_key = api_key or os.environ.get("POSTHOG_PERSONAL_API_KEY")
        self.ui_host = (ui_host or ui_host_for(self.host)).rstrip("/")
        self.sleep, self.poll_seconds = sleep, poll_seconds
        self._plain = urllib.request.build_opener()
        self._no_redirect = urllib.request.build_opener(_NoRedirect)

    def request(self, method, path, body=None, attempts=5):
        if not self.api_key:
            raise PostHogError("Set POSTHOG_PERSONAL_API_KEY (a personal API key with query:read and batch_export:read/write scopes)")
        if not self.project_id:
            raise PostHogError("Set POSTHOG_PROJECT_ID or run `orion init --project <id>`")
        url = f"{self.host}{path}"
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with self._plain.open(req, timeout=120) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")[:500]
                if error.code in (429, 502, 503, 504) and attempt < attempts - 1:
                    retry = error.headers.get("Retry-After")
                    self.sleep(float(retry) if retry and retry.replace(".", "", 1).isdigit() else min(60, 2 ** attempt * 2))
                    continue
                raise PostHogError(f"PostHog {method} {path} failed with HTTP {error.code}: {detail}") from None
        raise PostHogError(f"PostHog {method} {path} kept failing")

    def project_path(self, suffix):
        return f"/api/projects/{urllib.parse.quote(self.project_id)}/{suffix}"

    # Small, interactive lookups only.
    def query(self, sql, values=None, name="orion query"):
        body = {"query": {"kind": "HogQLQuery", "query": sql, **({"values": values} if values else {})}, "name": name}
        result = self.request("POST", self.project_path("query/"), body)
        columns = result.get("columns") or []
        return [dict(zip(columns, row)) for row in result.get("results") or []]

    def projects(self):
        return self.request("GET", "/api/projects/").get("results", [])

    def replay_url(self, session_id, offset_ms=None):
        url = f"{self.ui_host}/project/{urllib.parse.quote(self.project_id)}/replay/{urllib.parse.quote(session_id)}"
        return url + (f"?t={max(0, offset_ms) / 1000:.1f}" if offset_ms is not None else "")

    # Bulk events: file download exports, one week at most per export.
    def export_events(self, start, end, include, out_dir, timeout_s=1800):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        body = {"model": "events", "include": list(include),
                "data_interval_start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "data_interval_end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "file": {"format": "JSONLines", "compression": "gzip"}}
        created = self.request("POST", self.project_path("file_download_batch_exports/"), body)
        run_id = created.get("id")
        if not run_id:
            raise PostHogError(f"Export was not created: {created}")
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.request("GET", self.project_path(f"file_download_batch_exports/{run_id}/"))
            state = status.get("status")
            if state == EXPORT_DONE:
                break
            if state in EXPORT_FAILED:
                raise PostHogError(f"PostHog export {run_id} ended as {state}: {status.get('error', '')}")
            if time.monotonic() > deadline:
                raise PostHogError(f"PostHog export {run_id} did not finish within {timeout_s} s")
            self.sleep(self.poll_seconds)
        paths = []
        for index in range(len(status.get("files") or [])):
            path = out_dir / f"{run_id}-{index}.jsonl.gz"
            self._download(self.project_path(f"file_download_batch_exports/{run_id}/download/{index}/"), path)
            paths.append(path)
        return paths

    def _download(self, path, destination):
        req = urllib.request.Request(f"{self.host}{path}", headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with self._no_redirect.open(req, timeout=120) as response:
                body = response.read()  # Some deployments serve the file directly.
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise PostHogError(f"Download failed with HTTP {error.code}") from None
            signed = error.headers.get("Location")
            if not signed:
                raise PostHogError("Download redirect had no location") from None
            signed = urllib.parse.urljoin(f"{self.host}{path}", signed)
            with self._plain.open(urllib.request.Request(signed), timeout=600) as response:
                body = response.read()
        tmp = destination.with_suffix(destination.suffix + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(destination)


def week_chunks(start, end):
    """Day-aligned intervals of at most seven days covering [start, end)."""
    start = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while start < end:
        stop = min(start + timedelta(days=7), end)
        yield start, stop
        start = stop


def read_export(path):
    """Events from an exported JSON Lines file (plain or gzip). Properties may be objects or JSON strings."""
    path = Path(path)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            props = row.get("properties")
            if isinstance(props, str):
                props = json.loads(props) if props else {}
            row["properties"] = props or {}
            yield row


def parse_time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00").replace(" ", "T", 1)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def epoch_ms(value):
    return parse_time(value).timestamp() * 1000
