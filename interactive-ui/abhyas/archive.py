# The travel time archive - what the junction actually did, straight out of the
# TomTom spreadsheet. This is the thing we validate against, so the loader is
# picky: a row with an error string, no travel time or a non-200 status gets
# dropped and counted, never quietly treated as zero.

import datetime as dt
import statistics

import openpyxl

from . import config as C
from .stats import Stats

TRAVEL_SHEET_MARKER = "Data"
FLOW_SHEET_MARKER = "FlowSegments"


class Observation:
    """One row of the sheet."""

    def __init__(self, junction, movement, observed, length_m, travel_time_s,
                 free_flow_s, historic_s, delay_s, sheet, separate):
        self.junction = junction
        self.movement = movement
        self.observed = observed
        self.length_m = length_m
        self.travel_time_s = travel_time_s
        self.free_flow_s = free_flow_s
        self.historic_s = historic_s
        self.delay_s = delay_s
        self.sheet = sheet
        self.separate = separate
        self.free_flow_derived = False

    @property
    def day_kind(self):
        return "weekend" if self.observed.weekday() >= 5 else "weekday"

    @property
    def slot(self):
        """half hour bucket, e.g. weekday 18:00-18:30"""
        half = 0 if self.observed.minute < 30 else 30
        end_h = (self.observed.hour + (1 if half else 0)) % 24
        return (self.day_kind + " " + format(self.observed.hour, "02d") + ":"
                + format(half, "02d") + "-" + format(end_h, "02d") + ":"
                + ("00" if half else "30"))

    @property
    def hour_slot(self):
        return (self.day_kind + " " + format(self.observed.hour, "02d") + ":00-"
                + format((self.observed.hour + 1) % 24, "02d") + ":00")


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_datetime(value):
    # openpyxl hands back a real datetime most of the time, but the n8n export
    # sometimes writes the string form. hence the format list at the bottom.
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    text = str(value).strip()
    for cut in ("+", "Z"):
        if cut in text[10:]:
            text = text[:10] + text[10:].split(cut)[0]
    text = text.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def plain_title(title):
    """Excel refuses [ ] in a sheet name so "[final] Data" arrives as
    "final Data". Strip the brackets and compare against that."""
    return title.replace("[", "").replace("]", "").strip()


class LoadReport:
    """Counts of what came in and what got binned."""

    def __init__(self):
        self.rows_read = 0
        self.rows_kept = 0
        self.dropped_error = 0
        self.dropped_no_time = 0
        self.dropped_bad_status = 0
        self.sheets_used = []
        self.sheets_skipped = []
        self.free_flow_derived = 0

    @property
    def dropped(self):
        return self.dropped_error + self.dropped_no_time + self.dropped_bad_status

    def to_dict(self):
        return {
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "dropped_error": self.dropped_error,
            "dropped_no_time": self.dropped_no_time,
            "dropped_bad_status": self.dropped_bad_status,
            "dropped_total": self.dropped,
            "sheets_used": list(self.sheets_used),
            "sheets_skipped": list(self.sheets_skipped),
            "free_flow_derived": self.free_flow_derived,
        }


class Archive:
    """Loaded observations, plus everything anyone wants to ask about them."""

    def __init__(self, observations, report, free_flow_kmh):
        self.observations = observations
        self.report = report
        self.free_flow_kmh = free_flow_kmh

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path=None, include_separate=False):
        path = path or C.ARCHIVE_XLSX
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=False)
        report = LoadReport()
        observations = []
        free_flow = {}

        # if the workbook has any "final" sheet, that IS the archive. The older
        # sheets were collected before the route got pinned through the
        # junction, mixing them in blends a real corridor with a wrong one.
        final_only = any(plain_title(s.title).startswith(C.ARCHIVE_FINAL_PREFIX)
                         for s in workbook.worksheets)

        for sheet in workbook.worksheets:
            title = sheet.title
            plain = plain_title(title)
            if final_only and not plain.startswith(C.ARCHIVE_FINAL_PREFIX):
                report.sheets_skipped.append(title)
                continue
            separate = plain.startswith(C.ARCHIVE_SEPARATE_PREFIX)
            if separate and not include_separate:
                report.sheets_skipped.append(title)
                continue

            rows = list(sheet.iter_rows(values_only=True))
            if not rows or not rows[0]:
                continue
            header = {name: i for i, name in enumerate(rows[0]) if name}

            if FLOW_SHEET_MARKER in title:
                cls._read_flow_sheet(rows[1:], header, free_flow)
                report.sheets_used.append(title)
                continue
            if "travelTimeInSeconds" not in header:
                continue

            report.sheets_used.append(title)
            for row in rows[1:]:
                obs = cls._read_row(row, header, title, separate, report)
                if obs is not None:
                    observations.append(obs)
                    report.rows_kept += 1

        workbook.close()
        cls._backfill_free_flow(observations, free_flow, report)
        return cls(observations, report, free_flow)

    @staticmethod
    def _read_row(row, header, title, separate, report):
        if not any(row):
            return None
        report.rows_read += 1
        if header.get("error") is not None and row[header["error"]]:
            report.dropped_error += 1
            return None
        status = 200
        if "http_status" in header:
            status = to_float(row[header["http_status"]])
        if status is not None and int(status) != 200:
            report.dropped_bad_status += 1
            return None
        travel = to_float(row[header["travelTimeInSeconds"]])
        observed = to_datetime(row[header["observed_ist"]])
        if travel is None or travel <= 0 or observed is None:
            report.dropped_no_time += 1
            return None
        return Observation(
            junction=row[header["junction"]],
            movement=row[header["movement"]],
            observed=observed,
            length_m=to_float(row[header["lengthInMeters"]]) or 0.0,
            travel_time_s=travel,
            free_flow_s=to_float(row[header["noTrafficTravelTimeInSeconds"]]),
            historic_s=to_float(row[header["historicTravelTimeInSeconds"]]),
            delay_s=to_float(row[header["trafficDelayInSeconds"]]),
            sheet=title,
            separate=separate,
        )

    @staticmethod
    def _read_flow_sheet(rows, header, free_flow):
        """Observed free flow speed per movement, km/h. These are measurements,
        and they're what the network's speed limits get set from - OSM's default
        for these road types is 100 km/h which this corridor definitely is not.
        """
        if "freeFlowSpeed" not in header:
            return
        for row in rows:
            if not any(row):
                continue
            speed = to_float(row[header["freeFlowSpeed"]])
            if not speed:
                continue
            key = (row[header["junction"]], row[header["movement"]])
            free_flow.setdefault(key, []).append(speed)

    @staticmethod
    def _backfill_free_flow(observations, free_flow, report):
        """The live sheets carry travel time but leave free flow blank. The
        flow segment sheet has an observed free flow speed for the same
        movement, so derive it as length / speed and flag the row."""
        medians = {key: statistics.median(v) for key, v in free_flow.items()}
        filled = 0
        for row in observations:
            if row.free_flow_s or not row.length_m:
                continue
            speed_kmh = medians.get((row.junction, row.movement))
            if not speed_kmh:
                continue
            row.free_flow_s = round(row.length_m / (speed_kmh / 3.6), 1)
            row.delay_s = round(max(0.0, row.travel_time_s - row.free_flow_s), 1)
            row.free_flow_derived = True
            filled += 1
        report.free_flow_derived = filled

    # -- picking rows ------------------------------------------------------

    def select(self, junction=None, movement=None, slot=None, hour_slot=None,
               separate=None):
        rows = self.observations
        if junction:
            rows = [o for o in rows if o.junction == junction]
        if movement:
            rows = [o for o in rows if o.movement == movement]
        if slot:
            rows = [o for o in rows if o.slot == slot]
        if hour_slot:
            rows = [o for o in rows if o.hour_slot == hour_slot]
        if separate is not None:
            rows = [o for o in rows if o.separate is separate]
        return rows

    def _count_by(self, attr, junction, movement, min_samples):
        counts = {}
        for row in self.select(junction=junction, movement=movement):
            key = getattr(row, attr)
            counts[key] = counts.get(key, 0) + 1
        return sorted(k for k, n in counts.items() if n >= min_samples)

    def slots(self, junction=None, movement=None, min_samples=2):
        return self._count_by("slot", junction, movement, min_samples)

    def hour_slots(self, junction=None, movement=None, min_samples=3):
        """Hour buckets. Collection runs every 15 min so a half hour holds two
        observations, which is not enough to quote a spread from. An hour holds
        four, which is only just enough."""
        return self._count_by("hour_slot", junction, movement, min_samples)

    def coverage(self, junction=None):
        rows = self.select(junction=junction)
        if not rows:
            return {"observations": 0}
        stamps = sorted(o.observed for o in rows)
        per_movement = {}
        for row in rows:
            per_movement[row.movement] = per_movement.get(row.movement, 0) + 1
        return {
            "observations": len(rows),
            "first": stamps[0].isoformat(sep=" "),
            "last": stamps[-1].isoformat(sep=" "),
            "days": len({s.date() for s in stamps}),
            "per_movement": per_movement,
        }

    # -- the numbers we validate against -----------------------------------

    def target(self, junction, movement, slot=None, hour_slot=None):
        """Typical travel time for one movement in one slot, with its spread.

        The spread is the point. A model that lands inside the variation of the
        measurement is as accurate as the measurement allows, which is a
        stronger thing to say than any percentage.
        """
        rows = self.select(junction=junction, movement=movement, slot=slot,
                           hour_slot=hour_slot)
        times = [o.travel_time_s for o in rows]
        if not times:
            return {"junction": junction, "movement": movement,
                    "slot": slot or hour_slot, "n": 0, "usable": False,
                    "reason": "no observations in this slot"}

        free_flows = [o.free_flow_s for o in rows if o.free_flow_s]
        delays = [o.delay_s for o in rows if o.delay_s is not None]
        lengths = [o.length_m for o in rows if o.length_m]
        return {
            "junction": junction,
            "movement": movement,
            "label": C.ALL_MOVEMENTS.get(movement, {}).get("label", movement),
            "slot": slot or hour_slot,
            "n": len(times),
            "usable": len(times) >= 3,
            "travel_time_s": round(statistics.median(times), 1),
            "travel_time_mean_s": round(statistics.fmean(times), 1),
            "spread_iqr_s": [round(Stats.quantile(times, 0.25), 1),
                             round(Stats.quantile(times, 0.75), 1)],
            "spread_p10_p90_s": [round(Stats.quantile(times, 0.10), 1),
                                 round(Stats.quantile(times, 0.90), 1)],
            "stdev_s": round(statistics.stdev(times), 1) if len(times) > 1 else 0.0,
            "free_flow_s": round(statistics.median(free_flows), 1) if free_flows else None,
            "delay_s": round(statistics.median(delays), 1) if delays else None,
            "length_m": round(statistics.median(lengths), 1) if lengths else None,
            "sheets": sorted({o.sheet for o in rows}),
            "separate_batch": all(o.separate for o in rows),
        }

    def daily_profile(self, junction, movement):
        buckets = {}
        for row in self.select(junction=junction, movement=movement):
            buckets.setdefault(row.observed.hour, []).append(row.travel_time_s)
        return [{"hour": hour, "n": len(v),
                 "median_s": round(statistics.median(v), 1),
                 "p10_s": round(Stats.quantile(v, 0.1), 1),
                 "p90_s": round(Stats.quantile(v, 0.9), 1)}
                for hour, v in sorted(buckets.items())]

    def free_flow_for(self, junction=None):
        """median observed free flow speed per movement, km/h"""
        junction = junction or C.JUNCTION_KEY
        return {movement: round(statistics.median(speeds), 1)
                for (junc, movement), speeds in self.free_flow_kmh.items()
                if junc == junction}


def load(path=None, include_separate=False):
    return Archive.load(path=path, include_separate=include_separate)


def observed_free_flow_kmh(junction=None, include_separate=False):
    return Archive.load(include_separate=include_separate).free_flow_for(junction)


if __name__ == "__main__":
    import json
    arc = Archive.load()
    print(json.dumps({
        "load": arc.report.to_dict(),
        "coverage": arc.coverage(C.JUNCTION_KEY),
        "free_flow_kmh": arc.free_flow_for(),
        "hour_slots": arc.hour_slots(C.JUNCTION_KEY, "NS"),
        "sample_target": arc.target(C.JUNCTION_KEY, "NS",
                                    hour_slot="weekday 09:00-10:00"),
    }, indent=2))
