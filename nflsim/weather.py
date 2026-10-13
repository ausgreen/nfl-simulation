"""Game weather for the v2 model: venues, historical weather, and forecasts.

- Venues (data/venues.csv): coordinates and roof type (outdoor / dome / retractable) per venue.
  Games are matched to venues by stadium *name* first, because neutral-site games carry the
  host team's code (e.g. a London game listed under JAX00).
- History: Open-Meteo archive (ERA5 reanalysis), hourly, cached per venue in data/weather/archive/.
  Games newer than the archive come from Open-Meteo's forecast API for past dates, cached in
  data/weather/recent/.
- Upcoming games: Open-Meteo forecast. Retractable roofs are assumed closed when the forecast has
  rain (>= 0.5 mm over the game), temperature below 40 F, or wind above 15 mph; otherwise open.

Weather features are measured from kickoff to kickoff + 3 h and are 0 indoors:
  cold_below40 (deg F below 40), wind_excess (mean mph above 12), precip_mm (total).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .data import Dataset
from .nflverse import load_schedules

WX_FEATURES = ["cold_below40", "wind_excess", "precip_mm"]
HOURLY = "temperature_2m,precipitation,wind_speed_10m"
ARCHIVE_LAG_DAYS = 7
GAME_HOURS = 3
ET = ZoneInfo("America/New_York")

# Neutral-site names listed under a host team's code -> the venue they really are.
NAME_TO_VENUE = {
    "Tottenham Hotspur Stadium": "LON02", "Tottenham Stadium": "LON02", "Wembley Stadium": "LON00",
    "Twickenham Stadium": "LON01", "Allianz Arena": "GER00", "FC Bayern Munich Stadium": "GER00",
    "Deutsche Bank Park": "FRA00", "Azteca Stadium": "MEX00", "Estadio Banorte": "MEX00",
    "Maracana Stadium": "RIO00", "Arena Corinthians": "SAO00", "Bernabeu": "MAD01",
    "Melbourne Cricket Ground": "MEL00", "Stade de France": "PAR00",
}
# Venues whose schedule roof label is wrong or missing.
ROOF_OVERRIDE = {"MAD01": "retractable", "MEL00": "outdoor", "GER00": "outdoor", "MUN01": "outdoor",
                 "PAR00": "outdoor", "RIO00": "outdoor", "LAX01": "dome"}
EXTRA_VENUES = {  # venues not in the glare experiment's table: id -> geocoding query
    "MAD01": "Estadio Santiago Bernabéu, Madrid", "MEL00": "Melbourne Cricket Ground",
    "PAR00": "Stade de France, Saint-Denis", "MUN01": "Allianz Arena, Munich",
}


def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "nflsim"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


# --- venues -----------------------------------------------------------------------------
def venues_path(ds: Dataset) -> Path:
    return ds.data_dir / "venues.csv"


def build_venues(ds: Dataset, log=print) -> pd.DataFrame:
    """Create data/venues.csv from schedule history + the experiment's geocoded stadiums."""
    s = load_schedules(ds)
    s = s[s.season >= 2012]
    exp = pd.read_csv(ds.data_dir / "experiments" / "stadiums.csv").set_index("stadium_id")
    rows = []
    for vid, g in s.groupby(s.stadium_id.where(~s.stadium.isin(NAME_TO_VENUE), s.stadium.map(NAME_TO_VENUE))):
        roofs = set(g.roof.dropna())
        roof = ROOF_OVERRIDE.get(vid) or ("retractable" if roofs & {"open", "closed"} else
                                         "dome" if roofs == {"dome"} else "outdoor")
        row = {"venue_id": vid, "name": g.sort_values("season").stadium.iloc[-1], "roof_type": roof,
               "lat": np.nan, "lon": np.nan, "source": ""}
        if vid in exp.index and pd.notna(exp.at[vid, "lat"]):
            row.update(lat=exp.at[vid, "lat"], lon=exp.at[vid, "lon"], source="OSM Nominatim (glare experiment)")
        elif vid in EXTRA_VENUES:
            res = _get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": EXTRA_VENUES[vid], "format": "json", "limit": 1}))
            time.sleep(1.1)
            if res:
                row.update(lat=float(res[0]["lat"]), lon=float(res[0]["lon"]), source="OSM Nominatim")
        rows.append(row)
    v = pd.DataFrame(rows).sort_values("venue_id")
    v.to_csv(venues_path(ds), index=False)
    log(f"wrote {venues_path(ds)}: {len(v)} venues, {v.lat.notna().sum()} with coordinates")
    return v


def load_venues(ds: Dataset) -> pd.DataFrame:
    return pd.read_csv(venues_path(ds)).set_index("venue_id")


def venue_of(stadium_id, stadium) -> str | None:
    return NAME_TO_VENUE.get(stadium, stadium_id if isinstance(stadium_id, str) else None)


# --- hourly data ------------------------------------------------------------------------------
def _hourly_frame(data: dict) -> pd.DataFrame:
    df = pd.DataFrame(data["hourly"])
    df["time"] = pd.to_datetime(df.time)
    return df


def _params(lat, lon, start, end) -> str:
    return urllib.parse.urlencode({"latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
                                   "hourly": HOURLY, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                                   "precipitation_unit": "mm", "timezone": "UTC"})


def archive(ds: Dataset, vid: str, lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    path = ds.data_dir / "weather" / "archive" / f"{vid}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["time"])
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _hourly_frame(_get("https://archive-api.open-meteo.com/v1/archive?" + _params(lat, lon, start, end)))
    df.to_csv(path, index=False)
    return df


def recent(ds: Dataset, vid: str, lat: float, lon: float, day: str) -> pd.DataFrame:
    """Hours for one recent date (too new for the archive), from the forecast API's past data."""
    path = ds.data_dir / "weather" / "recent" / f"{vid}_{day}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["time"])
    path.parent.mkdir(parents=True, exist_ok=True)
    nxt = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = _hourly_frame(_get("https://api.open-meteo.com/v1/forecast?" + _params(lat, lon, day, nxt)))
    if pd.Timestamp(nxt) < pd.Timestamp.now() - pd.Timedelta(days=1):  # only cache settled days
        df.to_csv(path, index=False)
    return df


def window(hours: pd.DataFrame, kickoff_utc: pd.Timestamp) -> dict:
    k = kickoff_utc.tz_convert(None).floor("h")
    w = hours.set_index("time").sort_index().loc[k: k + pd.Timedelta(hours=GAME_HOURS)]
    if w.empty or w.temperature_2m.isna().all():
        return {}
    t, wind, rain = w.temperature_2m.mean(), w.wind_speed_10m.mean(), w.precipitation.sum()
    return {"temp_f": t, "wind_mph": wind, "precip_mm": rain, "cold_below40": max(0.0, 40 - t),
            "wind_excess": max(0.0, wind - 12)}


def kickoffs(s: pd.DataFrame) -> pd.Series:
    local = pd.to_datetime(s.gameday + " " + s.gametime.fillna("13:00"))
    return pd.Series([t.tz_localize(ET).tz_convert("UTC") for t in local], index=s.game_id.to_numpy())


def assumed_roof_closed(wx: dict) -> bool:
    return wx.get("precip_mm", 0) >= 0.5 or wx.get("temp_f", 99) < 40 or wx.get("wind_mph", 0) > 15


# --- per-game features --------------------------------------------------------------------
def historical(ds: Dataset, since: int = 2012, log=print) -> pd.DataFrame:
    """Weather features for every played regular-season game (0 indoors), indexed by game_id."""
    s = load_schedules(ds)
    s = s[(s.season >= since) & (s.game_type == "REG") & s.home_score.notna()].copy()
    v = load_venues(ds)
    s["venue_id"] = [venue_of(i, n) for i, n in zip(s.stadium_id, s.stadium)]
    kick = kickoffs(s)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=ARCHIVE_LAG_DAYS)
    out = {}
    for vid, g in s.groupby("venue_id"):
        roof_type = v.roof_type.get(vid, "outdoor")
        for r in g.itertuples():
            out[r.game_id] = {"venue_id": vid, "open_air": roof_type == "outdoor" or
                              (roof_type == "retractable" and r.roof == "open"), "has_weather": False,
                              **dict.fromkeys(WX_FEATURES, 0.0)}
        if vid not in v.index or pd.isna(v.at[vid, "lat"]):
            continue
        lat, lon = v.at[vid, "lat"], v.at[vid, "lon"]
        open_games = [r for r in g.itertuples() if out[r.game_id]["open_air"]]
        if not open_games:
            continue
        old = [r for r in open_games if kick[r.game_id] < cutoff]
        hours = None
        if old:
            try:
                hours = archive(ds, vid, lat, lon, f"{min(kick[r.game_id] for r in old):%Y-%m-%d}",
                                f"{cutoff:%Y-%m-%d}")
            except Exception as e:
                log(f"weather archive unavailable for {vid}: {e}")
        for r in open_games:
            k = kick[r.game_id]
            try:
                h = hours if hours is not None and k + pd.Timedelta(hours=GAME_HOURS) <= hours.time.max().tz_localize("UTC") \
                    else recent(ds, vid, lat, lon, f"{k:%Y-%m-%d}")
            except Exception as e:
                log(f"weather unavailable for {r.game_id}: {e}")
                continue
            wx = window(h, k)
            if wx:
                out[r.game_id].update(has_weather=True, **wx)
    return pd.DataFrame.from_dict(out, orient="index")


def forecast(ds: Dataset, game_ids, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Forecast features for upcoming games, with the assumed roof state for retractables."""
    s = load_schedules(ds).set_index("game_id").loc[list(game_ids)].reset_index()
    v = load_venues(ds)
    kick = kickoffs(s)
    rows = {}
    for r in s.itertuples():
        vid = venue_of(r.stadium_id, r.stadium)
        roof_type = v.roof_type.get(vid, "outdoor") if vid else "outdoor"
        row = {"venue_id": vid, "roof_type": roof_type, "roof_assumed": "closed" if roof_type == "dome" else "open",
               "has_weather": False, "forecast_at": (now or pd.Timestamp.now(tz="UTC")).isoformat(),
               **dict.fromkeys(WX_FEATURES, 0.0), "temp_f": np.nan, "wind_mph": np.nan}
        if roof_type != "dome" and vid in v.index and pd.notna(v.at[vid, "lat"]):
            k = kick[r.game_id]
            day, nxt = f"{k:%Y-%m-%d}", f"{k + pd.Timedelta(days=1):%Y-%m-%d}"
            h = _hourly_frame(_get("https://api.open-meteo.com/v1/forecast?" +
                                   _params(v.at[vid, "lat"], v.at[vid, "lon"], day, nxt)))
            wx = window(h, k)
            if wx:
                row.update(has_weather=True, **wx)
                if roof_type == "retractable" and assumed_roof_closed(wx):
                    row["roof_assumed"] = "closed"
        if row["roof_assumed"] == "closed":  # indoors: weather does not apply
            row.update(dict.fromkeys(WX_FEATURES, 0.0))
        row["open_air"] = row["roof_assumed"] == "open"
        rows[r.game_id] = row
    return pd.DataFrame.from_dict(rows, orient="index")
