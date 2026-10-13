"""PostHog API client: file exports for bulk event pulls, the query API for small lookups.

PostHog asks third-party tools not to use /query for exports, so `pull` uses file download exports
(at most one week per export). /query is only used for one session's labels and for label search.
"""
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

US_HOST = "https://us.posthog.com"
KEY_NAME = "POSTHOG_PERSONAL_API_KEY"
EXPORT_DONE = "Completed"
EXPORT_FAILED = {"Failed", "FailedBilling", "Cancelled"}


class PostHogError(RuntimeError):
    pass


class PostHogAccessError(PostHogError):
    """Not signed in, a key PostHog refuses, or a key without a scope Orion needs: `orion init` fixes all three."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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
        from .credentials import env_name, get as stored_key
        self.host = (host or os.environ.get("POSTHOG_HOST") or US_HOST).rstrip("/")
        self.project_id = str(project_id or os.environ.get("POSTHOG_PROJECT_ID") or "")
        # A key passed in or set in the environment wins; a key from the keys file comes after a browser sign-in.
        self.api_key = api_key or stored_key(KEY_NAME)
        self.key_given = bool(api_key or env_name(KEY_NAME))
        self.ui_host = (ui_host or ui_host_for(self.host)).rstrip("/")
        self.sleep, self.poll_seconds = sleep, poll_seconds
        self._plain = urllib.request.build_opener()
        self._no_redirect = urllib.request.build_opener(_NoRedirect)

    def credential(self, rejected=None):
        """(bearer token, "key" or "sign-in"); (None, None) when there is neither."""
        from . import posthog_auth
        if self.api_key and self.key_given:
            return self.api_key, "key"
        record = posthog_auth.login(self.host, self.project_id or None)
        if record:
            return posthog_auth.access_token(self.host, record["project"], rejected), "sign-in"
        return (self.api_key, "key") if self.api_key else (None, None)

    def request(self, method, path, body=None, attempts=5):
        from .posthog_auth import SignInError
        if not self.project_id and path.startswith("/api/projects/") and path != "/api/projects/":
            raise PostHogError("No PostHog project: run `orion init` (or set POSTHOG_PROJECT_ID)")
        try:
            token, kind = self.credential()
        except SignInError as error:
            raise PostHogAccessError(str(error)) from None
        if not token:
            raise PostHogAccessError("Not signed in to PostHog: run `orion init`, which opens PostHog in your browser "
                                     f"(or set {KEY_NAME})")
        url = f"{self.host}{path}"
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with self._plain.open(req, timeout=120) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")[:500]
                if error.code == 401 and kind == "sign-in" and not refreshed:  # Expired early or revoked: refresh once.
                    refreshed = True
                    try:
                        token, kind = self.credential(rejected=token)
                    except SignInError as problem:
                        raise PostHogAccessError(str(problem)) from None
                    continue
                if error.code in (429, 502, 503, 504) and attempt < attempts - 1:
                    retry = error.headers.get("Retry-After")
                    self.sleep(float(retry) if retry and retry.replace(".", "", 1).isdigit() else min(60, 2 ** attempt * 2))
                    attempt += 1
                    continue
                if error.code in (401, 403):
                    raise PostHogAccessError(self.explain(error.code, detail, kind), error.code) from None
                raise PostHogError(f"PostHog {method} {path} failed with HTTP {error.code}: {detail}") from None

    def explain(self, status, detail, kind):
        """What a 401 or 403 means for this credential, and the one step that fixes it."""
        scope = re.search(r"required scope '([^']+)'", detail)
        if kind == "sign-in":
            what = f"Orion's PostHog sign-in lacks the {scope.group(1)} scope" if scope else "PostHog refused Orion's sign-in"
            return f"{what} (HTTP {status}). Run `orion init` to sign in again."
        fix = ("Run `orion init` and sign in with PostHog in your browser instead: Orion then gets exactly the access "
               f"it needs, with nothing to paste. Or edit the key at {self.ui_host}/settings/user-api-keys")
        if scope:
            return f"Your PostHog personal API key is missing the {scope.group(1)} scope (HTTP {status}). {fix} and add {scope.group(1)}."
        if status == 401:
            return f"PostHog did not accept the personal API key at {self.host} (HTTP {status}). {fix}."
        return f"PostHog refused the personal API key (HTTP {status}: {detail[:200]}). {fix}."

    def access(self):
        """What this client can reach, for checks before a pull: {"via", "project", "missing", "problem"}.
        A personal API key describes its own scopes (/api/personal_api_keys/@current/); a sign-in's scopes are saved."""
        from . import posthog_auth
        token, kind = self.credential()
        report = {"via": kind, "project": None, "missing": [], "problem": None}
        if kind == "sign-in":
            record = posthog_auth.login(self.host, self.project_id or None)
            granted = set((record.get("scope") or "").split())
            report["missing"] = [s for s in posthog_auth.SCOPES if s not in granted]
        elif kind == "key":
            key = self.request("GET", "/api/personal_api_keys/@current/")
            granted = set(key.get("scopes") or [])
            report["missing"] = [] if "*" in granted else [s for s in posthog_auth.SCOPES if s not in granted]
            teams = [str(t) for t in key.get("scoped_teams") or []]
            if teams and self.project_id and self.project_id not in teams:
                report["problem"] = f"the key is limited to other projects than {self.project_id}"
        if self.project_id and "project:read" not in report["missing"] and not report["problem"]:
            report["project"] = self.request("GET", self.project_path(""))
        return report

    def require_pull_access(self):
        """Raises PostHogAccessError, with the fix, unless a pull can run: before any export is created."""
        report = self.access()
        if report["missing"] or report["problem"]:
            why = report["problem"] or f"it lacks the {', '.join(report['missing'])} scope{'s' if len(report['missing']) > 1 else ''}"
            if report["via"] == "sign-in":
                raise PostHogAccessError(f"Orion's PostHog sign-in cannot pull: {why}. Run `orion init` to sign in again.")
            raise PostHogAccessError(f"Your PostHog personal API key cannot pull: {why}. Run `orion init` and sign in with "
                                     "PostHog in your browser instead (Orion then gets exactly the access it needs), or "
                                     f"edit the key at {self.ui_host}/settings/user-api-keys.")
        return report

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
        req = urllib.request.Request(f"{self.host}{path}", headers={"Authorization": f"Bearer {self.credential()[0]}"})
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
