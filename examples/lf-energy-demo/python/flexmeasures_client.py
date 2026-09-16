"""
A small REST client for the walkthrough's FlexMeasures instance.

Everything the demo scripts need from FlexMeasures is reachable over HTTP, so they can
run on the host like seed_events.py does, rather than inside the server container the
way the ORM-based seeding scripts have to. This module owns that HTTP surface: the token
flow, the handful of endpoints involved, and the name-to-id lookups that let the demo
scripts talk about `demo-campus` instead of a database id that differs per machine.

It deliberately holds no demo logic. What a schedule run means, and how two of them
compare, lives in schedule_comparison.py.

Requires `httpx`, which the calling uv script declares in its inline dependency block.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Self

import httpx
from settings import (
    FLEXMEASURES_LOGIN_EMAIL,
    FLEXMEASURES_LOGIN_PASSWORD,
    FLEXMEASURES_URL,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# The token endpoint sits next to the versioned API rather than inside it.
AUTH_TOKEN_PATH = "/api/requestAuthToken"
API_PREFIX = "/api/v3_0"

# Generous, because a first request can land while the server is still warming up.
REQUEST_TIMEOUT = timedelta(seconds=60)

# Polling a scheduling job leaves the connection idle between requests, and gunicorn drops
# a keep-alive connection after about two seconds. Reusing one just as the server closes it
# surfaces as a transport error rather than a response. Dropping idle connections first,
# well before gunicorn does, avoids that race; retrying reads is the backstop for when a
# connection is lost anyway.
KEEPALIVE_EXPIRY = timedelta(seconds=1)
TRANSPORT_ATTEMPTS = 3
RETRY_PAUSE = timedelta(seconds=1)

# HTTP 202 on the schedule endpoints means "still running", which is not an error here.
HTTP_ACCEPTED = 202


class FlexMeasuresError(RuntimeError):
    """A request to FlexMeasures failed, or its answer could not be used."""


@dataclass(frozen=True, slots=True)
class SensorRef:
    """
    Enough of a sensor to fetch and label its data.

    :param id:          Sensor id in this FlexMeasures instance.
    :param name:        Sensor name, unique within its asset.
    :param asset_name:  Name of the asset the sensor belongs to.
    :param unit:        Sensor unit, e.g. `kW`.
    """

    id: int
    name: str
    asset_name: str
    unit: str

    @property
    def label(self) -> str:
        """Human-readable `asset/sensor` path, as the seeding scripts also print it."""
        return f"{self.asset_name}/{self.name}"


@dataclass(frozen=True, slots=True)
class TimeSeries:
    """
    A contiguous run of equally-spaced values, as the data and schedule endpoints return them.

    :param start:       Start of the first event, timezone-aware.
    :param resolution:  Duration of every event.
    :param unit:        Unit the values are expressed in.
    :param values:      One value per event; None where the sensor holds no belief.
    """

    start: datetime
    resolution: timedelta
    unit: str
    values: list[float | None]

    @property
    def end(self) -> datetime:
        """End of the last event."""
        return self.start + len(self.values) * self.resolution

    def event_starts(self) -> Iterator[datetime]:
        """Yield the start of every event, in order."""
        for index in range(len(self.values)):
            yield self.start + index * self.resolution


def parse_duration(duration: str) -> timedelta:
    """
    Parse the subset of ISO 8601 durations that FlexMeasures returns for resolutions.

    Only the day-and-below fields occur here, because a resolution is always a fixed
    length of time; months and years are not, and FlexMeasures does not emit them for
    the fields this module reads.

    :param duration:            An ISO 8601 duration, e.g. `PT15M` or `P1DT6H`.
    :returns:                   The equivalent timedelta.
    :raises FlexMeasuresError:  When the duration cannot be parsed as a fixed length.
    """
    if not duration.startswith("P") or "Y" in duration or "M" in duration.partition("T")[0]:
        msg = f"Cannot read '{duration}' as a fixed-length ISO 8601 duration."
        raise FlexMeasuresError(msg)

    date_part, _, time_part = duration[1:].partition("T")
    fields = {"D": 0.0, "H": 0.0, "M": 0.0, "S": 0.0}
    for part, symbols in ((date_part, "D"), (time_part, "HMS")):
        number = ""
        for character in part:
            if character in symbols:
                fields[character] = float(number or 0)
                number = ""
            else:
                number += character
    return timedelta(days=fields["D"], hours=fields["H"], minutes=fields["M"], seconds=fields["S"])


def format_duration(duration: timedelta) -> str:
    """
    Render a duration the way the FlexMeasures API expects it.

    :param duration:  Any non-negative duration.
    :returns:         An ISO 8601 duration in whole seconds, e.g. `PT86400S`.
    """
    return f"PT{int(duration.total_seconds())}S"


class FlexMeasuresClient:
    """
    Authenticated access to the walkthrough's FlexMeasures instance.

    One instance holds one auth token, so the scripts log in once per run.
    """

    def __init__(self, base_url: str = FLEXMEASURES_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT.total_seconds(),
            limits=httpx.Limits(keepalive_expiry=KEEPALIVE_EXPIRY.total_seconds()),
        )
        self._token: str | None = None

    def __enter__(self) -> Self:
        """Enter a context that closes the underlying HTTP connection pool on exit."""
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    # --- plumbing ------------------------------------------------------------

    def log_in(self, email: str = FLEXMEASURES_LOGIN_EMAIL, password: str = FLEXMEASURES_LOGIN_PASSWORD) -> None:
        """
        Exchange credentials for an auth token and use it for every later request.

        :param email:               Login email of a user with access to the demo account.
        :param password:            That user's password.
        :raises FlexMeasuresError:  When the instance is unreachable or rejects the credentials.
        """
        try:
            response = self._client.post(AUTH_TOKEN_PATH, json={"email": email, "password": password})
        except httpx.HTTPError as exc:
            msg = f"Could not reach FlexMeasures at {self.base_url}: {exc}. Is the walkthrough stack up (`docker compose up -d`)?"
            raise FlexMeasuresError(msg) from exc
        if response.is_error:
            msg = f"FlexMeasures rejected the login for '{email}' ({response.status_code}): {response.text}"
            raise FlexMeasuresError(msg)
        # FlexMeasures expects the bare token, without a `Bearer ` prefix.
        self._token = response.json()["auth_token"]

    def _request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> httpx.Response:
        """
        Send an authenticated request to the versioned API.

        :param method:              HTTP method.
        :param path:                Path below `/api/v3_0`, starting with a slash.
        :param params:              Query parameters, if any.
        :param json:                JSON request body, if any.
        :returns:                   The raw response, so callers can act on 202 themselves.
        :raises FlexMeasuresError:  When no token has been obtained yet, or the request fails.
        """
        if self._token is None:
            msg = "Not logged in yet: call log_in() before making API requests."
            raise FlexMeasuresError(msg)
        # Only reads are retried: replaying a lost POST could leave a second scheduling job
        # running that nobody is waiting for.
        attempts = TRANSPORT_ATTEMPTS if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._client.request(method, f"{API_PREFIX}{path}", headers={"Authorization": self._token}, params=params, json=json)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    msg = f"Request {method} {path} to FlexMeasures failed after {attempt} attempt(s): {exc}"
                    raise FlexMeasuresError(msg) from exc
                time.sleep(RETRY_PAUSE.total_seconds())
            except httpx.HTTPError as exc:
                msg = f"Request {method} {path} to FlexMeasures failed: {exc}"
                raise FlexMeasuresError(msg) from exc
        # Unreachable: the loop above either returns or raises.
        msg = f"Request {method} {path} to FlexMeasures failed."
        raise FlexMeasuresError(msg)

    def _json_object(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        """
        Send an authenticated request and return its JSON body as an object.

        :param method:              HTTP method.
        :param path:                Path below `/api/v3_0`, starting with a slash.
        :param params:              Query parameters, if any.
        :param json:                JSON request body, if any.
        :returns:                   The decoded JSON object.
        :raises FlexMeasuresError:  When FlexMeasures answers with an error status.
        """
        body = self._json(method, path, params=params, json=json)
        if not isinstance(body, dict):
            msg = f"Expected a JSON object from {method} {path}, got a {type(body).__name__}."
            raise FlexMeasuresError(msg)
        return body

    def _json_array(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> list:
        """
        Send an authenticated request and return its JSON body as an array.

        :param method:              HTTP method.
        :param path:                Path below `/api/v3_0`, starting with a slash.
        :param params:              Query parameters, if any.
        :param json:                JSON request body, if any.
        :returns:                   The decoded JSON array.
        :raises FlexMeasuresError:  When FlexMeasures answers with an error status.
        """
        body = self._json(method, path, params=params, json=json)
        if not isinstance(body, list):
            msg = f"Expected a JSON array from {method} {path}, got a {type(body).__name__}."
            raise FlexMeasuresError(msg)
        return body

    def _json(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict | list:
        """
        Send an authenticated request and return its JSON body, raising on any error status.

        :param method:              HTTP method.
        :param path:                Path below `/api/v3_0`, starting with a slash.
        :param params:              Query parameters, if any.
        :param json:                JSON request body, if any.
        :returns:                   The decoded JSON body.
        :raises FlexMeasuresError:  When FlexMeasures answers with an error status.
        """
        response = self._request(method, path, params=params, json=json)
        if response.is_error:
            msg = f"FlexMeasures answered {response.status_code} to {method} {path}: {response.text}"
            raise FlexMeasuresError(msg)
        body: dict | list = response.json()
        return body

    # --- lookups -------------------------------------------------------------

    def find_asset_id(self, name: str) -> int:
        """
        Look up an asset id by name among the assets the logged-in user can read.

        Asset ids differ per machine and per re-seed, so the demo scripts address assets
        by the names hierarchy.py gives them and resolve those here.

        :param name:                Asset name, e.g. `demo-campus`.
        :returns:                   The asset's id.
        :raises FlexMeasuresError:  When no such asset is accessible.
        """
        # Without a `page` parameter this returns every accessible asset, unpaginated.
        assets = self._json_array("GET", "/assets", params={"all_accessible": "true"})
        matches = [asset for asset in assets if asset["name"] == name]
        if not matches:
            msg = f"No accessible asset named '{name}'. Has seed_assets.py run against this instance?"
            raise FlexMeasuresError(msg)
        if len(matches) > 1:
            found = ", ".join(str(asset["id"]) for asset in matches)
            msg = f"Found more than one accessible asset named '{name}' (ids {found}); cannot tell which one the demo means."
            raise FlexMeasuresError(msg)
        return int(matches[0]["id"])

    def get_asset(self, asset_id: int) -> dict:
        """
        Fetch one asset, including its flex-context, sensors and direct children.

        :param asset_id:  Id of the asset to fetch.
        :returns:         The asset as the API represents it.
        """
        return self._json_object("GET", f"/assets/{asset_id}")

    def get_sensor(self, sensor_id: int) -> dict:
        """
        Fetch one sensor's metadata.

        :param sensor_id:  Id of the sensor to fetch.
        :returns:          The sensor as the API represents it.
        """
        return self._json_object("GET", f"/sensors/{sensor_id}")

    def find_sensor(self, asset_name: str, sensor_name: str) -> SensorRef:
        """
        Look up a sensor by its asset name and sensor name.

        :param asset_name:          Name of the asset carrying the sensor, e.g. `demo-campus`.
        :param sensor_name:         Name of the sensor on that asset, e.g. `power`.
        :returns:                   A reference holding the sensor's id and unit.
        :raises FlexMeasuresError:  When the asset or the sensor does not exist.
        """
        asset = self.get_asset(self.find_asset_id(asset_name))
        for sensor in asset.get("sensors", []):
            if sensor["name"] == sensor_name:
                return SensorRef(
                    id=int(sensor["id"]),
                    name=sensor_name,
                    asset_name=asset_name,
                    unit=self.get_sensor(int(sensor["id"]))["unit"],
                )
        available = ", ".join(sensor["name"] for sensor in asset.get("sensors", [])) or "none"
        msg = f"Asset '{asset_name}' has no sensor named '{sensor_name}' (it has: {available})."
        raise FlexMeasuresError(msg)

    def describe_sensor(self, sensor_id: int) -> SensorRef:
        """
        Build a sensor reference from an id, resolving the name of its asset.

        Used for sensors the demo learns about from a flex-context rather than by name,
        such as the OpenADR capacity-limit sensors.

        :param sensor_id:  Id of the sensor to describe.
        :returns:          A reference holding the sensor's name, asset name and unit.
        """
        sensor = self.get_sensor(sensor_id)
        asset = self.get_asset(int(sensor["generic_asset_id"]))
        return SensorRef(id=sensor_id, name=sensor["name"], asset_name=asset["name"], unit=sensor["unit"])

    # --- sensor data ---------------------------------------------------------

    def get_sensor_data(self, sensor: SensorRef, start: datetime, duration: timedelta, resolution: timedelta | None = None) -> TimeSeries:
        """
        Fetch the most recent beliefs recorded on a sensor over a window.

        Events the sensor holds no belief about come back as None rather than being
        skipped, which is what lets callers check a window for gaps.

        :param sensor:      The sensor to read.
        :param start:       Start of the window, timezone-aware.
        :param duration:    Length of the window.
        :param resolution:  Resolution to resample to; None keeps the sensor's own.
        :returns:           The values over the window.
        """
        params = {
            "start": start.isoformat(),
            "duration": format_duration(duration),
            "unit": sensor.unit,
        }
        if resolution is not None:
            params["resolution"] = format_duration(resolution)
        body = self._json_object("GET", f"/sensors/{sensor.id}/data", params=params)
        return TimeSeries(
            start=datetime.fromisoformat(body["start"]),
            resolution=parse_duration(body["resolution"]),
            unit=body["unit"],
            values=list(body["values"]),
        )

    # --- scheduling ----------------------------------------------------------

    def trigger_asset_schedule(self, asset_id: int, start: datetime, duration: timedelta) -> str:
        """
        Ask FlexMeasures to schedule a whole asset tree, and return the job to poll.

        No flex-model or flex-context is sent, on purpose: leaving both out makes the
        scheduler read whatever is stored on the asset tree at this moment. That is the
        entire mechanism behind the demo's before-and-after comparison — the request is
        identical either side of the OpenADR wiring, and only the stored flex-context
        differs.

        The job cache is bypassed, because two runs of this demo do send an identical
        request, and without that FlexMeasures would rightly hand back the first run's
        job and its now-stale plan instead of scheduling against the new flex-context.

        :param asset_id:            Id of the asset to schedule, e.g. the campus.
        :param start:               Start of the schedule, timezone-aware.
        :param duration:            Length of the schedule.
        :returns:                   Id of the scheduling job.
        :raises FlexMeasuresError:  When FlexMeasures refuses the trigger.
        """
        body = self._json_object(
            "POST",
            f"/assets/{asset_id}/schedules/trigger",
            json={
                "start": start.isoformat(),
                "duration": format_duration(duration),
                "force-new-job-creation": True,
            },
        )
        return str(body["job"])

    def await_schedule(self, sensor: SensorRef, job_id: str, duration: timedelta, poll_interval: timedelta, timeout: timedelta) -> TimeSeries:
        """
        Poll one sensor's slice of a scheduling job until the job finishes.

        The schedule endpoint answers 202 with a job status while the job is queued or
        running, and 200 with the values once it is done.

        Note that the values a finished job hands back are the *most recent* beliefs of
        the scheduler source on that sensor, not necessarily the ones this job wrote:
        a later scheduling run overwrites what this endpoint reports for an earlier job
        id. That is why the demo saves each run's values to disk as soon as it has them,
        rather than re-reading them once the second run exists.

        :param sensor:              The sensor to read the schedule of.
        :param job_id:              Id of the scheduling job, as returned by the trigger.
        :param duration:            Length of the schedule to ask back.
        :param poll_interval:       How long to wait between polls.
        :param timeout:             How long to keep polling before giving up.
        :returns:                   The scheduled values, consumption-positive.
        :raises FlexMeasuresError:  When the job fails, or does not finish in time.
        """
        params = {
            "duration": format_duration(duration),
            "unit": sensor.unit,
            # Import positive, export negative, whatever the sensor's own sign convention is.
            "sign-convention": "consumption-positive",
        }
        deadline = time.monotonic() + timeout.total_seconds()
        status = "UNKNOWN"
        while time.monotonic() < deadline:
            response = self._request("GET", f"/sensors/{sensor.id}/schedules/{job_id}", params=params)
            if response.status_code == HTTP_ACCEPTED:
                status = response.json().get("status", "UNKNOWN")
                time.sleep(poll_interval.total_seconds())
                continue
            if response.is_error:
                msg = f"Scheduling job {job_id} did not produce a schedule for {sensor.label} ({response.status_code}): {response.text}"
                raise FlexMeasuresError(msg)
            body = response.json()
            return TimeSeries(
                start=datetime.fromisoformat(body["start"]),
                resolution=parse_duration(body["duration"]) / len(body["values"]),
                unit=body["unit"],
                values=list(body["values"]),
            )
        msg = (
            f"Scheduling job {job_id} was still {status} after {timeout}. "
            "Is a worker serving the `scheduling` queue? "
            "The walkthrough's docker-compose runs one as the `scheduling-worker` service; "
            "check it with `docker compose logs scheduling-worker`."
        )
        raise FlexMeasuresError(msg)
