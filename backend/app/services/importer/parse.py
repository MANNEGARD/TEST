"""Säker inläsning och tolkning av Volvo-körjournaler (XLSX/CSV).

Motorn är byggd utifrån de faktiska exporterna:

* Äldre format, 14 kolumner (med Titel och Batteriregenerering), blad "Sheet1".
* Nyare format, 12 kolumner, blad "Trips".
* Datum förekommer som text ("2026-07-01 08:34"), som äkta datum-/tidsceller och
  kan förekomma som Excel-serienummer.
* Körtid ("Tid") förekommer som tidsintervall-cell, som text "00:33 h" eller tal.
* Titel innehåller ibland generiska värden och är inte resans syfte.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.places.normalize import clean, fold

MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_COLUMNS = 60
HEADER_SCAN_ROWS = 15

# Kanoniskt fältnamn → godkända rubriker (jämförs via fold() utan enhet i parentes)
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "category": ("kategori", "category", "typ"),
    "started": ("startade", "started", "start", "starttid", "startdatum"),
    "odometer_start": ("mätarställning start", "odometer start", "mätarställning vid start"),
    "start_address": ("startposition", "start position", "startadress", "start address", "från"),
    "stopped": ("stannade", "stopped", "slut", "sluttid", "end"),
    "odometer_end": ("mätarställning slut", "odometer end", "mätarställning vid slut"),
    "end_address": ("slutdestination", "destination", "slutadress", "end address", "till", "slutposition"),
    "duration": ("tid", "duration", "körtid", "time"),
    "distance": ("avstånd", "distance", "sträcka", "körsträcka"),
    "fuel": ("bränsleåtgång", "bränsleförbrukning", "fuel consumption", "fuel", "bränsle"),
    "title": ("titel", "title"),
    "energy": ("batteriförbrukning", "elförbrukning", "battery consumption", "energiförbrukning"),
    "regen": ("batteriregenerering", "battery regeneration", "regenerering"),
    "notes": ("anteckningar", "notes", "kommentar", "kommentarer", "anteckning", "syfte"),
}
REQUIRED_FIELDS = ("started", "stopped", "distance")
_SYNONYM_LOOKUP = {fold(s): canon for canon, syns in COLUMN_SYNONYMS.items() for s in syns}

CATEGORY_MAP = {
    "arbete": "work",
    "tjänst": "work",
    "tjänsteresa": "work",
    "business": "work",
    "work": "work",
    "privat": "private",
    "private": "private",
    "personal": "private",
}

_DT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d",
)
# Tolkningssätt där cellen bara innehöll ett datum (klockslaget är okänt).
DATE_ONLY_KINDS = ("date_cell", "date_string")
_DURATION_HM = re.compile(r"^\s*(\d{1,3}):(\d{2})(?::(\d{2}))?\s*(?:h|tim|t)?\s*$", re.IGNORECASE)
_DURATION_WORDS = re.compile(r"^\s*(?:(\d+)\s*(?:h|tim|t))?\s*(?:(\d+)\s*(?:m|min))?\s*$", re.IGNORECASE)
_EXCEL_EPOCH_1900 = datetime(1899, 12, 30)
_EXCEL_EPOCH_1904 = datetime(1904, 1, 1)


class ImportFileError(Exception):
    """Filen kan inte importeras (felmeddelande på svenska, säkert att visa)."""


@dataclass
class ParsedRow:
    row_number: int
    raw: dict[str, Any]
    values: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ParsedFile:
    filename: str
    sha256: str
    size_bytes: int
    format: str
    sheet_name: str | None
    column_map: dict[str, str]
    ignored_columns: list[str]
    rows: list[ParsedRow]
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------- värdetolkning


def _header_key(value: Any) -> str:
    s = fold(str(value)) if value is not None else ""
    s = re.sub(r"\(.*?\)", "", s)  # ta bort enhet, t.ex. "(km)"
    return clean(s)


def parse_local_datetime(value: Any, *, date1904: bool = False) -> tuple[datetime | None, str | None]:
    """Tolkar en cell till naiv lokal tid. Returnerar (värde, tolkningssätt)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None), "datetime_cell"
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day), "date_cell"
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        # Excel-serienummer (dagar sedan epok). Rimlighetskontroll: år 1990–2100.
        if 32874 <= float(value) <= 73415:
            epoch = _EXCEL_EPOCH_1904 if date1904 else _EXCEL_EPOCH_1900
            dt = epoch + timedelta(days=float(value))
            # avrunda till närmaste sekund för att undvika flyttalsbrus
            dt = (dt + timedelta(microseconds=500000)).replace(microsecond=0)
            return dt, "excel_serial"
        return None, None
    text = clean(str(value))
    if not text:
        return None, None
    for fmt in _DT_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)  # noqa: DTZ007 - lokal tid, zon sätts senare
        except ValueError:
            continue
        return parsed, ("date_string" if "%H" not in fmt else "string")
    try:
        num = float(text.replace(",", "."))
    except ValueError:
        return None, None
    return parse_local_datetime(num, date1904=date1904)


def parse_duration_seconds(value: Any) -> tuple[int | None, str | None]:
    """Tolkar körtid: tidsintervall, klockslag, "00:33 h", "1 h 5 min" eller dagbråk."""
    if value is None or value == "":
        return None, None
    if isinstance(value, timedelta):
        return int(round(value.total_seconds())), "timedelta_cell"
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second, "time_cell"
    if isinstance(value, datetime):
        # Excel kan visa [hh]:mm som datum 1899/1900 – använd tiden sedan epok.
        delta = value - _EXCEL_EPOCH_1900
        if timedelta(0) <= delta < timedelta(days=30):
            return int(round(delta.total_seconds())), "datetime_cell"
        return value.hour * 3600 + value.minute * 60 + value.second, "datetime_cell"
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        v = float(value)
        if 0 <= v < 5:  # bråkdel av dygn (Excel-tid)
            return int(round(v * 86400)), "excel_fraction"
        return None, None
    text = clean(str(value))
    m = _DURATION_HM.match(text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if mi < 60 and s < 60:
            return h * 3600 + mi * 60 + s, "string"
    m2 = _DURATION_WORDS.match(text)
    if m2 and (m2.group(1) or m2.group(2)):
        return int(m2.group(1) or 0) * 3600 + int(m2.group(2) or 0) * 60, "string"
    return None, None


def parse_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean(str(value)).replace(" ", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def to_utc(local_naive: datetime, tz: ZoneInfo) -> tuple[datetime, str | None]:
    """Lokal väggklocka → UTC. Rapporterar tvetydiga/obefintliga tider vid omställning."""
    from datetime import UTC

    first = local_naive.replace(tzinfo=tz, fold=0)
    second = local_naive.replace(tzinfo=tz, fold=1)
    note = None
    if first.utcoffset() != second.utcoffset():
        roundtrip = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
        if roundtrip != local_naive:
            note = "Tidpunkten finns inte (omställning till sommartid); tolkad med normaltid."
        else:
            note = "Tidpunkten är tvetydig (omställning till normaltid); första förekomsten används."
    return first.astimezone(UTC), note


def _second_occurrence(local_naive: datetime, tz: ZoneInfo) -> datetime | None:
    """UTC för andra förekomsten av en tvetydig lokal tid (omställning till normaltid), annars None."""
    from datetime import UTC

    first = local_naive.replace(tzinfo=tz, fold=0)
    second = local_naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return None
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != local_naive:
        return None  # obefintlig tid (omställning till sommartid), inte tvetydig
    return second.astimezone(UTC)


def map_category(value: Any) -> tuple[str, str | None]:
    original = clean(str(value)) if value is not None else ""
    if not original:
        return "undecided", None
    return CATEGORY_MAP.get(fold(original), "undecided"), original


def _raw_repr(value: Any) -> Any:
    """Originalvärdet som JSON-serialiserbar text (bevaras per rad)."""
    if value is None:
        return None
    if isinstance(value, (datetime, date, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, timedelta):
        return {"type": "timedelta", "seconds": value.total_seconds()}
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)


# ----------------------------------------------------------------------- inläsning


def _guard_xlsx(data: bytes) -> None:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ImportFileError("Filen är inte en giltig XLSX-fil.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ImportFileError("Filen innehåller orimligt många delar.")
            total = sum(i.file_size for i in infos)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ImportFileError("Filen är för stor när den packas upp.")
            names = {i.filename.lower() for i in infos}
            if any(n.endswith("vbaproject.bin") for n in names):
                raise ImportFileError("Filer med makron accepteras inte.")
            if "[content_types].xml" not in names or not any(n.startswith("xl/") for n in names):
                raise ImportFileError("Filen är inte en giltig Excel-arbetsbok.")
    except zipfile.BadZipFile as exc:
        raise ImportFileError("Filen är skadad och kan inte läsas.") from exc


def _read_xlsx(data: bytes, max_rows: int) -> list[tuple[str, list[list[Any]], bool]]:
    import openpyxl

    _guard_xlsx(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except Exception as exc:  # openpyxl kastar många olika fel för trasiga filer
        raise ImportFileError("Excel-filen kunde inte läsas.") from exc
    date1904 = bool(getattr(wb, "epoch", None) and wb.epoch.year == 1904)
    sheets: list[tuple[str, list[list[Any]], bool]] = []
    try:
        for ws in wb.worksheets:
            rows: list[list[Any]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > max_rows + HEADER_SCAN_ROWS:
                    raise ImportFileError(f"Bladet {ws.title!r} har fler än {max_rows} rader.")
                rows.append(list(row[:MAX_COLUMNS]))
            sheets.append((ws.title, rows, date1904))
    finally:
        wb.close()
    return sheets


def _read_csv(data: bytes, max_rows: int) -> list[tuple[str, list[list[Any]], bool]]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ImportFileError("CSV-filens teckenkodning kunde inte tolkas.")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") >= sample.count(",") else ","  # type: ignore[misc]
    rows: list[list[Any]] = []
    for i, row in enumerate(csv.reader(io.StringIO(text), dialect)):
        if i > max_rows + HEADER_SCAN_ROWS:
            raise ImportFileError(f"CSV-filen har fler än {max_rows} rader.")
        rows.append([c if c != "" else None for c in row[:MAX_COLUMNS]])
    return [("csv", rows, False)]


def _find_header(rows: list[list[Any]]) -> tuple[int, dict[str, int], list[str]] | None:
    for idx, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        mapping: dict[str, int] = {}
        ignored: list[str] = []
        for col, cell in enumerate(row):
            if cell is None or not isinstance(cell, str):
                continue
            canon = _SYNONYM_LOOKUP.get(_header_key(cell))
            if canon and canon not in mapping:
                mapping[canon] = col
            elif clean(cell):
                ignored.append(clean(cell))
        if all(f in mapping for f in REQUIRED_FIELDS):
            return idx, mapping, ignored
    return None


def _detect_format(mapping: dict[str, int], is_csv: bool) -> str:
    has_title, has_regen = "title" in mapping, "regen" in mapping
    base = "volvo_csv" if is_csv else "volvo_xlsx"
    if has_title and has_regen:
        return f"{base}_v1_14col"
    if not has_title and not has_regen and "energy" in mapping:
        return f"{base}_v2_12col"
    return f"{base}_custom"


def parse_file(filename: str, data: bytes, tz: ZoneInfo, *, max_bytes: int, max_rows: int) -> ParsedFile:
    """Läser en fil och returnerar tolkade rader. Kastar ImportFileError vid ogiltig fil."""
    if len(data) == 0:
        raise ImportFileError("Filen är tom.")
    if len(data) > max_bytes:
        raise ImportFileError(f"Filen är större än tillåtna {max_bytes // (1024 * 1024)} MB.")
    lower = filename.lower()
    sha = hashlib.sha256(data).hexdigest()
    if lower.endswith(".xlsx"):
        sheets = _read_xlsx(data, max_rows)
        is_csv = False
    elif lower.endswith((".csv", ".txt")):
        sheets = _read_csv(data, max_rows)
        is_csv = True
    elif lower.endswith((".xlsm", ".xls", ".xlsb")):
        raise ImportFileError("Endast XLSX och CSV stöds (inte XLS, XLSB eller filer med makron).")
    else:
        raise ImportFileError("Okänt filformat. Ladda upp en XLSX- eller CSV-export från Volvo.")

    chosen = None
    skipped: list[str] = []
    for sheet_name, rows, date1904 in sheets:
        found = _find_header(rows)
        if found and chosen is None:
            chosen = (sheet_name, rows, date1904, *found)
        elif found:
            n = sum(1 for r in rows[found[0] + 1 :] if any(c is not None and str(c).strip() for c in r))
            skipped.append(f"{sheet_name} ({n} rader)")
    if chosen is None:
        raise ImportFileError(
            "Inga igenkända kolumnrubriker hittades. Förväntade minst Startade, Stannade och Avstånd (km)."
        )
    sheet_name, rows, date1904, header_idx, mapping, ignored = chosen
    header_row = rows[header_idx]
    column_map = {canon: clean(str(header_row[col])) for canon, col in mapping.items()}
    parsed = ParsedFile(
        filename=filename,
        sha256=sha,
        size_bytes=len(data),
        format=_detect_format(mapping, is_csv),
        sheet_name=None if is_csv else sheet_name,
        column_map=column_map,
        ignored_columns=ignored,
        rows=[],
    )
    if "odometer_start" not in mapping or "odometer_end" not in mapping:
        parsed.warnings.append("Filen saknar mätarställningar; resorna markeras som ofullständiga.")
    if "notes" not in mapping:
        parsed.warnings.append("Filen saknar kolumnen Anteckningar; resornas syfte måste anges manuellt.")
    if skipped:
        parsed.warnings.append(
            f"Endast bladet {sheet_name!r} har lästs. Även dessa blad har resekolumner men importerades inte: "
            f"{', '.join(skipped)}. Spara varje blad som en egen fil för att importera dem."
        )

    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue  # helt tom rad
        cell = {canon: (row[col] if col < len(row) else None) for canon, col in mapping.items()}
        raw = {column_map[canon]: _raw_repr(v) for canon, v in cell.items()}
        pr = ParsedRow(row_number=i + 1, raw=raw)
        _parse_row(pr, cell, tz, date1904)
        parsed.rows.append(pr)
    if not parsed.rows:
        parsed.warnings.append("Filen innehåller inga resor.")
    return parsed


def _parse_row(pr: ParsedRow, cell: dict[str, Any], tz: ZoneInfo, date1904: bool) -> None:
    v = pr.values
    start_local, start_kind = parse_local_datetime(cell.get("started"), date1904=date1904)
    stop_local, stop_kind = parse_local_datetime(cell.get("stopped"), date1904=date1904)
    if start_local is None:
        pr.errors.append("Starttid saknas eller kan inte tolkas.")
    if stop_local is None:
        pr.errors.append("Sluttid saknas eller kan inte tolkas.")
    duration, _ = parse_duration_seconds(cell.get("duration"))
    for label, kind in (("Starttiden", start_kind), ("Sluttiden", stop_kind)):
        if kind in DATE_ONLY_KINDS:
            # Ett datum utan klockslag görs aldrig om till ett exakt klockslag (00:00).
            pr.messages.append(f"{label} saknar klockslag; endast datumet används och tiden markeras som okänd.")
    start_utc = stop_utc = None
    if start_local is not None:
        start_utc, note = to_utc(start_local, tz)
        v["started_at"], v["started_local"], v["started_kind"] = start_utc.isoformat(), start_local.isoformat(), start_kind
        if note:
            pr.messages.append(f"Start: {note}")
    if stop_local is not None:
        stop_utc, note = to_utc(stop_local, tz)
        later = _second_occurrence(stop_local, tz)
        if later is not None and start_utc is not None:
            # Tvetydig sluttid vid omställning till normaltid: välj den förekomst som går ihop
            # med starttiden och körtiden i stället för att avvisa eller korta resan.
            def off(end: datetime) -> float:
                return abs((end - start_utc).total_seconds() - duration) if duration is not None else 0.0

            if stop_utc < start_utc <= later or (duration is not None and off(later) < off(stop_utc)):
                stop_utc = later
                note = "Tidpunkten är tvetydig (omställning till normaltid); andra förekomsten används."
        v["ended_at"], v["ended_local"], v["ended_kind"] = stop_utc.isoformat(), stop_local.isoformat(), stop_kind
        if note:
            pr.messages.append(f"Slut: {note}")
    if start_utc is not None and stop_utc is not None and stop_utc < start_utc:
        pr.errors.append("Sluttiden ligger före starttiden.")

    distance = parse_number(cell.get("distance"))
    if distance is None:
        pr.errors.append("Avstånd saknas eller kan inte tolkas.")
    elif distance < 0:
        pr.errors.append("Negativt avstånd.")
    else:
        v["distance_km"] = round(distance, 3)

    odo_s, odo_e = parse_number(cell.get("odometer_start")), parse_number(cell.get("odometer_end"))
    v["odometer_start"], v["odometer_end"] = odo_s, odo_e
    if odo_s is None or odo_e is None:
        if "odometer_start" in cell or "odometer_end" in cell:
            pr.messages.append("Mätarställning saknas.")
    elif odo_e < odo_s:
        pr.errors.append("Mätarställning slut är lägre än mätarställning start.")
    elif distance is not None and abs((odo_e - odo_s) - distance) > max(2.0, 0.05 * distance):
        pr.messages.append(
            f"Avstånd ({distance:.1f} km) avviker från mätarställningarna ({odo_e - odo_s:.0f} km)."
        )

    v["duration_seconds"] = duration
    if cell.get("duration") not in (None, "") and duration is None:
        pr.messages.append("Körtiden kunde inte tolkas.")
    if duration is not None and start_utc is not None and stop_utc is not None:
        elapsed = (stop_utc - start_utc).total_seconds()
        if duration > elapsed + 120:
            pr.messages.append("Körtiden är längre än tiden mellan start och slut.")
        elif elapsed - duration > 300:
            v["includes_stops"] = True  # källan har redan slagit ihop delsträckor

    v["start_address"] = clean(cell.get("start_address")) or None
    v["end_address"] = clean(cell.get("end_address")) or None

    fuel = parse_number(cell.get("fuel"))
    energy = parse_number(cell.get("energy"))
    regen = parse_number(cell.get("regen"))
    for name, val in (("Bränsleåtgång", fuel), ("Batteriförbrukning", energy), ("Batteriregenerering", regen)):
        if val is not None and val < 0:
            pr.messages.append(f"{name} är negativ och ignoreras.")
    v["fuel_liters"] = fuel if fuel is not None and fuel >= 0 else None
    v["energy_kwh"] = energy if energy is not None and energy >= 0 else None
    v["regen_kwh"] = regen if regen is not None and regen >= 0 else None
    v["has_fuel_column"] = "fuel" in cell
    v["has_energy_column"] = "energy" in cell

    category, original_category = map_category(cell.get("category"))
    v["category"], v["original_category"] = category, original_category
    if original_category and category == "undecided":
        pr.messages.append(f"Okänd kategori {original_category!r}; resan markeras som Ej avgjord.")

    # Titel och anteckningar bevaras exakt som i källan.
    title = cell.get("title")
    v["original_title"] = None if title is None else str(title)
    notes = cell.get("notes")
    v["original_notes"] = None if notes is None else str(notes)


def natural_key(vehicle_id: Any, values: dict[str, Any]) -> str | None:
    """Naturlig nyckel för dubblettskydd: lokal starttid (minutnivå) + mätarställningar."""
    start = values.get("started_local") or values.get("ended_local")
    if not start:
        return None
    odo_s, odo_e = values.get("odometer_start"), values.get("odometer_end")
    odo = f"{odo_s:.0f}|{odo_e:.0f}" if odo_s is not None and odo_e is not None else f"d{values.get('distance_km', 0):.1f}"
    basis = f"{vehicle_id}|{start[:16]}|{odo}"
    return hashlib.sha256(basis.encode()).hexdigest()
