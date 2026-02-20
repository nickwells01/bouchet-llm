"""Femoral Access Imaging — one-at-a-time workflow for NilRead → REDCap.

Per-patient helper functions for the coordinator-driven workflow.
There is NO batch mode — each patient is processed individually with
coordinator confirmation at every step.

Tab layout:
    Tab 1: REDCap (redcap.med.yale.edu, PID 2423, Event 26763)
    Tab 2: NilRead (imagecore.ynhh.org)

PHI rules:
    - MRN, procedure date, and accession number NEVER appear in stdout
    - Only lengths, record IDs, and status strings are printed
    - All PHI stays in _PATIENT_DATA dict (module-level, never returned)
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nav_agent import screen

# ── Configuration ──────────────────────────────────────────────────

REDCAP_VERSION = "redcap_v15.5.34"
REDCAP_PID = "2423"
REDCAP_EVENT = "26763"
REDCAP_TAB = 1
NILREAD_TAB = 2
FORM_PAGE = "femoral_access_imaging"

RADIO_FIELDS = ["image_needle_saved", "access_angio_saved", "image_other_ind_saved"]

# Femoral access site values that qualify for imaging review
# access_site_1 / access_site_2 radio values on the procedure form
FEMORAL_ACCESS_VALUES = {"1", "2", "3", "11", "12", "13"}
# 1=Femoral Retrograde, 2=Femoral Antegrade, 3=SFA,
# 11=Femoral Retro to Antegrade, 12=Femoral Ante to Retrograde, 13=Femoral

# ── Module-level PHI storage (never returned or printed) ──────────

_PATIENT_DATA: list[dict] = []
# Each entry: {"record_id": str, "mrn": str, "proc_date_redcap": str,
#              "proc_date_nilread": str, "accession": str | None}
_REDCAP_LOCK = threading.Lock()
_DASHBOARD_RECORD_CACHE: list[str] | None = None


# ── Internal helpers ──────────────────────────────────────────────


def _redcap_js(js: str, timeout: float = 15.0) -> str:
    """Execute JS in Safari REDCap tab."""
    # REDCap tab operations can happen from both the main loop and background
    # prefetch thread; serialize Safari JS calls to avoid races.
    with _REDCAP_LOCK:
        return screen.safari_js(js, tab=REDCAP_TAB, timeout=timeout)


def _nilread_js(js: str, timeout: float = 15.0) -> str:
    """Execute JS in Safari NilRead tab."""
    return screen.safari_js(js, tab=NILREAD_TAB, timeout=timeout)


def _activate_nilread():
    """Activate Safari and switch to NilRead tab (required for synthetic events)."""
    subprocess.run(['osascript', '-e', '''
tell application "Safari"
    activate
    set current tab of window 1 to tab 2 of window 1
end tell
'''], capture_output=True)
    time.sleep(0.5)


def _activate_redcap():
    """Activate Safari and switch to REDCap tab."""
    subprocess.run(['osascript', '-e', '''
tell application "Safari"
    activate
    set current tab of window 1 to tab 1 of window 1
end tell
'''], capture_output=True)
    time.sleep(0.3)


def _convert_date_for_nilread(redcap_date: str) -> str:
    """Convert REDCap MM-DD-YYYY → NilRead 'mon dd, yyyy' (lowercase)."""
    parts = redcap_date.split('-')
    dt = datetime.date(int(parts[2]), int(parts[0]), int(parts[1]))
    return dt.strftime('%b %d, %Y').lower()


def _fetch_patient_data(record_id: str) -> dict:
    """Fetch one patient's demographics/procedure fields from REDCap.

    Returns full patient data dict (contains PHI) for internal workflow use.
    """
    js = f'''
    (function() {{
        var rec = {{record_id: "{record_id}", mrn: "", proc_date: ""}};
        try {{
            // Fetch MRN from demographics page
            var xhr1 = new XMLHttpRequest();
            xhr1.open("GET",
                "/{REDCAP_VERSION}/DataEntry/index.php?pid={REDCAP_PID}"
                + "&id={record_id}"
                + "&page=demographics&event_id={REDCAP_EVENT}&instance=1",
                false);
            xhr1.send();
            if (xhr1.status === 200) {{
                var doc1 = new DOMParser().parseFromString(xhr1.responseText, "text/html");
                var mrnInp = doc1.querySelector("input[name='mrn']");
                if (mrnInp) rec.mrn = mrnInp.value;
            }}

            // Fetch procedure fields
            var xhr2 = new XMLHttpRequest();
            xhr2.open("GET",
                "/{REDCAP_VERSION}/DataEntry/index.php?pid={REDCAP_PID}"
                + "&id={record_id}"
                + "&page=procedure&event_id={REDCAP_EVENT}&instance=1",
                false);
            xhr2.send();
            if (xhr2.status === 200) {{
                var doc2 = new DOMParser().parseFromString(xhr2.responseText, "text/html");
                var dateInp = doc2.querySelector("input[name='procedure_date']");
                if (dateInp) rec.proc_date = dateInp.value;
                var as1 = doc2.querySelector("input[name='access_site_1']");
                var as2 = doc2.querySelector("input[name='access_site_2']");
                rec.access_site_1 = as1 ? as1.value : "";
                rec.access_site_2 = as2 ? as2.value : "";
            }}
        }} catch(e) {{
            rec.error = e.message;
        }}
        return JSON.stringify(rec);
    }})()
    '''
    rec = json.loads(_redcap_js(js, timeout=30.0))
    if "error" in rec:
        return {"record_id": record_id, "error": rec["error"]}

    mrn = rec.get("mrn", "")
    proc_date = rec.get("proc_date", "")
    if not mrn:
        return {"record_id": record_id, "error": "MRN not found"}
    if not proc_date:
        return {"record_id": record_id, "error": "procedure date not found"}

    try:
        nilread_date = _convert_date_for_nilread(proc_date)
    except (ValueError, IndexError):
        return {"record_id": record_id, "error": "invalid date format"}

    access_site_1 = rec.get("access_site_1", "")
    access_site_2 = rec.get("access_site_2", "")
    has_femoral = (access_site_1 in FEMORAL_ACCESS_VALUES
                   or access_site_2 in FEMORAL_ACCESS_VALUES)

    return {
        "record_id": record_id,
        "mrn": mrn,
        "proc_date_redcap": proc_date,
        "proc_date_nilread": nilread_date,
        "accession": None,
        "access_site_1": access_site_1,
        "access_site_2": access_site_2,
        "has_femoral": has_femoral,
    }


def _get_dashboard_records(force_reload: bool = False) -> list[str]:
    """Return ordered record IDs from the REDCap status dashboard."""
    global _DASHBOARD_RECORD_CACHE
    if _DASHBOARD_RECORD_CACHE is not None and not force_reload:
        return list(_DASHBOARD_RECORD_CACHE)

    dashboard_url = (f"https://redcap.med.yale.edu/{REDCAP_VERSION}"
                     f"/DataEntry/record_status_dashboard.php?pid={REDCAP_PID}")
    _redcap_js(f"window.location.href = '{dashboard_url}'", timeout=15)
    time.sleep(3)

    js = '''
    (function() {
        var rows = document.querySelectorAll('table#record_status_table tbody tr');
        var records = [];
        for (var i = 0; i < rows.length; i++) {
            var cells = rows[i].querySelectorAll('td');
            if (cells.length < 9) continue;
            records.push(cells[0].textContent.trim());
        }
        return JSON.stringify(records);
    })()
    '''
    records = json.loads(_redcap_js(js, timeout=15))
    _DASHBOARD_RECORD_CACHE = list(records)
    return list(records)


# ── Load patient demographics from REDCap ─────────────────────────


def load_patient(record_id: str) -> dict:
    """Load MRN + procedure date for a single patient from REDCap.

    Stores PHI in _PATIENT_DATA. Returns sanitized summary only:
    {"record_id": "123", "mrn_len": 9, "date_len": 10}

    PHI safety: MRN and date values are returned from JS to Python but
    NEVER printed. Only lengths are shown.
    """
    global _PATIENT_DATA
    _PATIENT_DATA = []

    patient = _fetch_patient_data(record_id)
    rid = patient["record_id"]
    if "error" in patient:
        print(f"  [WARN] Record {rid}: {patient['error']}")
        return {"record_id": rid, "error": patient["error"]}

    _PATIENT_DATA.append(patient)
    mrn = patient["mrn"]
    proc_date = patient["proc_date_redcap"]
    has_femoral = patient["has_femoral"]

    summary = {
        "record_id": rid,
        "mrn_len": len(mrn),
        "date_len": len(proc_date),
        "has_femoral": has_femoral,
    }
    print(f"  Loaded Record {rid} "
          f"(MRN: {summary['mrn_len']} chars, date: {summary['date_len']} chars, "
          f"femoral={'yes' if has_femoral else 'NO — skip'})")
    return summary


# Keep old name as alias for compatibility with existing code
def batch_prefetch(record_ids: list[str]) -> list[dict]:
    """Alias for load_patient. Accepts a list but only uses the first record."""
    if not record_ids:
        return []
    return [load_patient(record_ids[0])]


# ── Set up NilRead search ─────────────────────────────────────────



def _set_field_value_js(field_type: str, value: str):
    """Set MRN or date field value via JS and trigger search (no OS clicks needed).

    Uses native value setter + focus/input/change/Enter events — all pure JS.
    DevExpress grids respond to the Enter keydown to apply the filter.
    """
    if field_type == "mrn":
        js = f'''
        (function() {{
            var inputs = document.querySelectorAll("input.dx-texteditor-input");
            for (var i = 0; i < inputs.length; i++) {{
                var rect = inputs[i].getBoundingClientRect();
                if (rect.top > 50 && rect.top < 100 && rect.x > 900 && rect.x < 1050) {{
                    inputs[i].focus();
                    var nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, "value").set;
                    nativeSetter.call(inputs[i], "{value}");
                    inputs[i].dispatchEvent(new Event("input", {{bubbles: true}}));
                    inputs[i].dispatchEvent(new Event("change", {{bubbles: true}}));
                    inputs[i].dispatchEvent(new KeyboardEvent("keydown",
                        {{key: "Enter", code: "Enter", keyCode: 13, bubbles: true}}));
                    return "ok";
                }}
            }}
            return "not_found";
        }})()
        '''
    else:
        js = f'''
        (function() {{
            var filterRow = document.querySelector(".dx-datagrid-filter-row");
            if (!filterRow) return "no_filter_row";
            var cells = filterRow.querySelectorAll("td");
            var input = cells[6].querySelector("input.dx-texteditor-input");
            if (!input) return "no_date_input";
            input.focus();
            var nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, "value").set;
            nativeSetter.call(input, "{value}");
            input.dispatchEvent(new Event("input", {{bubbles: true}}));
            input.dispatchEvent(new Event("change", {{bubbles: true}}));
            input.dispatchEvent(new KeyboardEvent("keydown",
                {{key: "Enter", code: "Enter", keyCode: 13, bubbles: true}}));
            return "ok";
        }})()
        '''
    return _nilread_js(js)



def _count_study_rows() -> int:
    """Count rows with 12+ cells (real study data) in the NilRead treegrid."""
    js = '''
    (function() {
        var rows = document.querySelectorAll('tr.dx-data-row');
        var count = 0;
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].querySelectorAll('td').length >= 12) count++;
        }
        return count;
    })()
    '''
    return int(float(_nilread_js(js)))


def setup_nilread_search(index: int = 0) -> dict:
    """Search NilRead for current patient via pure JS (no OS clicks).

    Two-step search:
        1. Set MRN via JS native setter + Enter keydown → triggers search
        2. Wait for studies to load (poll for 12-cell rows, up to 15s)
        3. Set date via JS native setter + Enter keydown → filters results

    index: 0-based index into _PATIENT_DATA (always 0 for one-at-a-time).
    Returns: {"status": "set", "mrn_len": N, "date_len": N}
    """
    if not _PATIENT_DATA:
        raise RuntimeError("No patient loaded. Run load_patient() first.")
    if index < 0 or index >= len(_PATIENT_DATA):
        raise IndexError(f"Index {index} out of range (0-{len(_PATIENT_DATA) - 1})")

    patient = _PATIENT_DATA[index]
    rid = patient["record_id"]
    mrn = patient["mrn"]
    date_str = patient["proc_date_nilread"]

    # Activate Safari + NilRead tab
    _activate_nilread()

    # Step 1: Set MRN + Enter via JS
    mrn_result = _set_field_value_js("mrn", mrn)
    if mrn_result != "ok":
        print(f"  [ERROR] Record {rid}: MRN field JS set returned: {mrn_result}")
        return {"error": f"MRN field: {mrn_result}"}

    # Step 2: Wait for studies to load (poll up to 15s)
    loaded = False
    for _ in range(6):
        time.sleep(2.5)
        if _count_study_rows() > 0:
            loaded = True
            break
    if not loaded:
        print(f"  [WARN] Record {rid}: no study rows after MRN search (15s timeout)")

    # Step 3: Set date + Enter via JS
    date_result = _set_field_value_js("date", date_str)
    if date_result != "ok":
        print(f"  [ERROR] Record {rid}: date field JS set returned: {date_result}")
        return {"error": f"date field: {date_result}"}

    print(f"  Record {rid}: NilRead search set "
          f"(MRN: {len(mrn)} chars, date: {len(date_str)} chars)")

    return {
        "status": "set",
        "mrn_len": len(mrn),
        "date_len": len(date_str),
    }


# ── Extract accession + open study ─────────────────────────────────


def extract_accession(index: int = 0) -> dict:
    """Extract accession number from the best NilRead study row.

    Study selection priority:
      1. Row whose Description starts with "NR FL" (fluoroscopy/angiogram)
      2. If no "NR FL" match, fall back to the row with the most images

    If all rows are collapsed (2 cells), auto-expands the first row by
    clicking its .dx-treelist-icon-container, waits, then retries.

    Stores accession in _PATIENT_DATA[index]. Never prints the value.

    Returns: {"status": "found", "acc_len": N, "images": N, "modality": str,
              "desc_prefix": str} or {"error": ...}
    """
    if not _PATIENT_DATA:
        raise RuntimeError("No patient loaded. Run load_patient() first.")

    patient = _PATIENT_DATA[index]

    _PICK_BEST_ROW_JS = """
        var prefAcc = "", prefImages = 0, prefModality = "", prefDesc = "", prefMatch = "";
        var bestAcc = "", bestImages = -1, bestModality = "", bestDesc = "";
        for (var i = 0; i < rows.length; i++) {
            var cells = rows[i].querySelectorAll('td');
            if (cells.length >= 12) {
                var desc = cells[5].textContent.trim();
                var descUp = desc.toUpperCase();
                var mod = cells[7].textContent.trim();
                var imgCount = parseInt(cells[9].textContent.trim()) || 0;
                var acc = cells[3].textContent.trim();
                // Prefer angiography/fluoro studies over ultrasound etc.
                // Skip single-image studies — not useful even if preferred type
                var isPreferred = imgCount > 1 && (descUp.indexOf("NR FL") === 0
                    || descUp.indexOf("HVC NONREPORTABLE") === 0
                    || descUp.indexOf("PV TRANSCATHETER") === 0
                    || descUp.indexOf("PV ANGIOGRAPHY") === 0);
                if (isPreferred && imgCount > prefImages) {
                    prefAcc = acc; prefImages = imgCount;
                    prefModality = mod; prefDesc = desc.substring(0, 30);
                    prefMatch = descUp.indexOf("NR FL") === 0 ? "NR FL"
                        : descUp.indexOf("HVC NONREPORTABLE") === 0 ? "HVC NONREPORTABLE"
                        : descUp.indexOf("PV TRANSCATHETER") === 0 ? "PV TRANSCATHETER"
                        : "PV ANGIOGRAPHY";
                }
                // Track overall best by image count as fallback
                if (imgCount > bestImages) {
                    bestImages = imgCount; bestAcc = acc;
                    bestModality = mod; bestDesc = desc.substring(0, 30);
                }
            }
        }
        // Return preferred match if found, otherwise fallback
        if (prefAcc) {
            return JSON.stringify({accession: prefAcc, images: prefImages,
                modality: prefModality, desc_prefix: prefDesc, match: prefMatch});
        }
        if (bestImages >= 0) {
            return JSON.stringify({accession: bestAcc, images: bestImages,
                modality: bestModality, desc_prefix: bestDesc, match: "max_images"});
        }
    """

    acc_js = """
    (function() {
        var rows = document.querySelectorAll('.dx-data-row');
        if (rows.length === 0) return JSON.stringify({error: "no rows found"});

        // Check if any row is already expanded (12+ cells)
        var hasExpanded = false;
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].querySelectorAll('td').length >= 12) { hasExpanded = true; break; }
        }

        if (!hasExpanded) {
            var toggle = rows[0].querySelector('.dx-treelist-icon-container');
            if (toggle) {
                toggle.click();
                return JSON.stringify({need_retry: true});
            }
            return JSON.stringify({error: "no expand toggle found"});
        }

        """ + _PICK_BEST_ROW_JS + """
        return JSON.stringify({error: "no expanded rows"});
    })()
    """
    result = json.loads(_nilread_js(acc_js))

    if result.get("need_retry"):
        print(f"  Auto-expanded treegrid row, waiting...")
        time.sleep(1.5)
        retry_js = """
        (function() {
            var rows = document.querySelectorAll('.dx-data-row');
            """ + _PICK_BEST_ROW_JS + """
            return JSON.stringify({error: "no expanded row after expand"});
        })()
        """
        result = json.loads(_nilread_js(retry_js))

    if "error" in result:
        print(f"  [WARN] Record {patient['record_id']}: {result['error']}")
        return result

    acc_value = result.get("accession", "")
    if not acc_value:
        print(f"  [WARN] Record {patient['record_id']}: accession empty")
        return {"error": "accession empty"}

    patient["accession"] = acc_value
    images = result.get("images", 0)
    modality = result.get("modality", "?")
    desc = result.get("desc_prefix", "")
    match = result.get("match", "?")
    print(f"  Record {patient['record_id']}: accession extracted "
          f"({len(acc_value)} chars, {desc}, {modality}, {images} images, matched={match})")
    return {"status": "found", "acc_len": len(acc_value),
            "images": images, "modality": modality, "desc_prefix": desc, "match": match}


def open_study() -> dict:
    """Open the best study in NilRead viewer via real OS double-click.

    Study selection priority:
      1. Row whose Description starts with preferred prefixes (angiography/fluoro)
      2. If no preferred match, fall back to the row with the most images

    Uses JS to find the target row and get viewport coords, then converts
    to screen coords and dispatches a real cliclick double-click.

    Returns: {"status": "opened", "rowIndex": N, "images": N, "match": str}
             or {"error": ...}
    """
    _activate_nilread()

    # Step 1: Find best row and get its viewport center coordinates
    find_js = r"""
    (function() {
        var rows = document.querySelectorAll('.dx-data-row');

        // Find best row: prefer angiography/fluoro, fallback to most images
        var prefRow = null, prefIdx = -1, prefImages = 0;
        var bestRow = null, bestIdx = -1, bestImages = -1;
        for (var i = 0; i < rows.length; i++) {
            var cells = rows[i].querySelectorAll('td');
            if (cells.length >= 12) {
                var desc = cells[5].textContent.trim();
                var descUp = desc.toUpperCase();
                var imgCount = parseInt(cells[9].textContent.trim()) || 0;
                // Skip single-image studies — not useful even if preferred type
                var isPreferred = imgCount > 1 && (descUp.indexOf("NR FL") === 0
                    || descUp.indexOf("HVC NONREPORTABLE") === 0
                    || descUp.indexOf("PV TRANSCATHETER") === 0
                    || descUp.indexOf("PV ANGIOGRAPHY") === 0);
                if (isPreferred && imgCount > prefImages) {
                    prefRow = rows[i]; prefIdx = i; prefImages = imgCount;
                }
                if (imgCount > bestImages || bestRow === null) {
                    bestImages = imgCount; bestRow = rows[i]; bestIdx = i;
                }
            }
        }

        var targetRow = prefRow || bestRow;
        var targetIdx = prefRow ? prefIdx : bestIdx;
        var targetImages = prefRow ? prefImages : bestImages;
        var matchType = prefRow ? "preferred" : "max_images";

        if (!targetRow) return JSON.stringify({error: "no expanded row found"});

        var cell = targetRow.querySelectorAll('td')[3];
        var rect = cell.getBoundingClientRect();
        var vx = rect.left + rect.width / 2;
        var vy = rect.top + rect.height / 2;

        // Include window geometry for coordinate conversion
        return JSON.stringify({
            vx: vx, vy: vy,
            screenX: window.screenX, screenY: window.screenY,
            outerWidth: window.outerWidth, outerHeight: window.outerHeight,
            innerWidth: window.innerWidth, innerHeight: window.innerHeight,
            rowIndex: targetIdx, images: targetImages, match: matchType
        });
    })()
    """
    info = json.loads(_nilread_js(find_js))
    if "error" in info:
        print(f"  [ERROR] open_study: {info['error']}")
        return info

    # Step 2: Convert viewport coords to screen coords
    scale = info["outerWidth"] / info["innerWidth"]
    chrome_h = info["outerHeight"] - info["innerHeight"] * scale
    sx = round(info["screenX"] + info["vx"] * scale)
    sy = round(info["screenY"] + chrome_h + info["vy"] * scale)

    # Step 3: Real OS double-click via cliclick
    screen.double_click(sx, sy)

    print(f"  Study opened (row {info['rowIndex']}, {info.get('images', '?')} images, "
          f"matched={info.get('match', '?')})")
    return {"status": "opened", "rowIndex": info["rowIndex"],
            "images": info["images"], "match": info["match"]}


# ── Export + Download ──────────────────────────────────────────────


def export_and_download() -> dict:
    """Save → Export → verify panel → Download in NilRead.

    Combines Steps 7-8. Verifies export panel is visible before clicking
    Download.

    Returns: {"status": "downloaded"} or {"error": ...}
    """
    _activate_nilread()

    export_js = """
    (function() {
        var btn = document.getElementById('saveMenuBtn');
        if (!btn) return JSON.stringify({error: "saveMenuBtn not found"});
        btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
        btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));

        var menus = document.querySelectorAll('.goog-menu');
        for (var i = 0; i < menus.length; i++) {
            if (menus[i].offsetParent !== null || menus[i].style.visibility !== 'hidden') {
                var items = menus[i].querySelectorAll('.goog-menuitem');
                for (var j = 0; j < items.length; j++) {
                    if (items[j].textContent.trim() === 'Export') {
                        var el = items[j];
                        var rect = el.getBoundingClientRect();
                        var cx = rect.left + rect.width / 2;
                        var cy = rect.top + rect.height / 2;

                        el.classList.add('goog-menuitem-highlight');
                        el.dispatchEvent(new PointerEvent('pointerover', {bubbles:true, clientX:cx, clientY:cy}));
                        el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, clientX:cx, clientY:cy}));
                        el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, clientX:cx, clientY:cy, button:0}));
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:cx, clientY:cy, button:0}));
                        el.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, clientX:cx, clientY:cy, button:0}));
                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:cx, clientY:cy, button:0}));
                        el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:cx, clientY:cy, button:0}));

                        return JSON.stringify({status: "export_dispatched"});
                    }
                }
            }
        }
        return JSON.stringify({error: "Export menu item not found"});
    })()
    """
    export_result = json.loads(_nilread_js(export_js))
    if "error" in export_result:
        print(f"  [ERROR] export: {export_result['error']}")
        return export_result

    time.sleep(1.5)

    download_js = """
    (function() {
        var panel = document.getElementById('divSaveImagePreviewPanel');
        if (!panel || panel.offsetWidth === 0) {
            return JSON.stringify({error: "export panel not visible"});
        }
        var btn = document.getElementById('btnSaveImagePreviewPanelDownload');
        if (!btn) return JSON.stringify({error: "download button not found"});

        var iframe = document.getElementById('saveImagePreviewDownloadFrame');
        var srcBefore = iframe ? iframe.src : "";

        btn.click();

        return JSON.stringify({
            status: "download_clicked",
            src_before_len: srcBefore.length
        });
    })()
    """
    dl_result = json.loads(_nilread_js(download_js))

    if "error" in dl_result:
        print(f"  [ERROR] download: {dl_result['error']}")
        return dl_result

    print("  Download started")
    return {"status": "download_started"}


# ── Save to REDCap ─────────────────────────────────────────────────


def save_to_redcap(index: int = 0, selection: set[int] = None,
                   comments: str = "") -> dict:
    """Save accession number + image radio buttons to REDCap in ONE XHR POST.

    Combines accession_number + all 3 radio fields + optional comments +
    form status in a single GET/POST cycle.

    index: 0-based index into _PATIENT_DATA (always 0 for one-at-a-time).
    selection: set of ints from coordinator (e.g. {1,2}, {0}, {2}).
        0 = no images, 1 = needle, 2 = angiogram, 3 = other.
    comments: optional string for incl_excl_comments field (e.g. "DSA").

    Returns: {"record_id": ..., "status": "saved", "http_status": 200}
    """
    if selection is None:
        selection = set()
    if not _PATIENT_DATA:
        raise RuntimeError("No patient loaded. Run load_patient() first.")

    patient = _PATIENT_DATA[index]
    rid = patient["record_id"]
    accession = patient.get("accession", "")

    no_images = 0 in selection
    if no_images:
        selection = set()

    needle = 1 if 1 in selection else 0
    angio = 1 if 2 in selection else 0
    other = 1 if 3 in selection else 0

    # Complete (2) when no images; Unverified (1) otherwise
    form_complete = 2 if no_images else 1

    acc_escaped = json.dumps(accession) if accession else '""'

    fill_js = f'''
    (function() {{
        // 1. GET form page for CSRF token + hidden fields
        var xhr = new XMLHttpRequest();
        xhr.open("GET",
            "/{REDCAP_VERSION}/DataEntry/index.php?pid={REDCAP_PID}"
            + "&id={rid}&page={FORM_PAGE}&event_id={REDCAP_EVENT}&instance=1",
            false);
        xhr.send();
        if (xhr.status !== 200) return JSON.stringify({{error: "GET failed", status: xhr.status}});

        var html = xhr.responseText;

        // Extract CSRF token
        var csrfMatch = html.match(/redcap_csrf_token[^>]*value=["']([^"']+)["']/);
        if (!csrfMatch) return JSON.stringify({{error: "CSRF token not found"}});
        var csrf = csrfMatch[1];

        // Extract hidden fields
        var hiddenFields = {{}};
        var hiddenRegex = /<input[^>]+type=["']hidden["'][^>]*>/gi;
        var match;
        while ((match = hiddenRegex.exec(html)) !== null) {{
            var inp = match[0];
            var nameMatch = inp.match(/name=["']([^"']+)["']/);
            var valMatch = inp.match(/value=["']([^"']*?)["']/);
            if (nameMatch) hiddenFields[nameMatch[1]] = valMatch ? valMatch[1] : "";
        }}

        // Exclude backing text inputs for radio fields + comments (would overwrite our values)
        var skipFields = ["image_needle_saved", "access_angio_saved", "image_other_ind_saved",
                          "incl_excl_comments"];

        // 2. Build POST body
        var params = [];
        params.push("redcap_csrf_token=" + encodeURIComponent(csrf));
        for (var key in hiddenFields) {{
            if (key === "redcap_csrf_token") continue;
            if (skipFields.indexOf(key) >= 0) continue;
            params.push(encodeURIComponent(key) + "=" + encodeURIComponent(hiddenFields[key]));
        }}

        // Accession number
        params.push("accession_number=" + encodeURIComponent({acc_escaped}));

        // Image radio buttons (all 3 always set)
        params.push("image_needle_saved={needle}");
        params.push("access_angio_saved={angio}");
        params.push("image_other_ind_saved={other}");

        // Comments (if provided)
        var commentsVal = {json.dumps(comments)};
        if (commentsVal) {{
            params.push("incl_excl_comments=" + encodeURIComponent(commentsVal));
        }}

        // Form status: Complete (2) if no images, Unverified (1) otherwise
        params.push("{FORM_PAGE}_complete={form_complete}");

        // Save action
        params.push("submit-action=submit-btn-saverecord");
        params.push("submit-btn-saverecord=Save+%26+Exit+Record");

        // 3. POST
        var xhr2 = new XMLHttpRequest();
        xhr2.open("POST",
            "/{REDCAP_VERSION}/DataEntry/index.php?pid={REDCAP_PID}"
            + "&event_id={REDCAP_EVENT}&page={FORM_PAGE}&instance=1",
            false);
        xhr2.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
        xhr2.send(params.join("&"));

        return JSON.stringify({{
            status: xhr2.status,
            hidden_fields: Object.keys(hiddenFields).length,
            accession_len: {acc_escaped}.length
        }});
    }})()
    '''
    raw = _redcap_js(fill_js, timeout=15.0)

    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print(f"  [WARN] Record {rid}: parse failed")
        return {"record_id": rid, "status": "failed", "raw": str(raw)[:200]}

    if "error" in result:
        print(f"  [WARN] Record {rid}: {result['error']}")
        return {"record_id": rid, "status": "failed", **result}

    http_status = result.get("status", 0)
    acc_len = result.get("accession_len", 0)
    if http_status in (200, 302):
        comment_note = f", comments={comments!r}" if comments else ""
        status_label = "Complete" if no_images else "Unverified"
        print(f"  Record {rid}: saved (accession: {acc_len} chars, "
              f"needle={needle}, angio={angio}, other={other}"
              f"{comment_note}, complete={status_label}) → HTTP {http_status}")
        return {"record_id": rid, "status": "saved", "http_status": http_status}
    else:
        print(f"  [WARN] Record {rid}: unexpected HTTP {http_status}")
        return {"record_id": rid, "status": "unknown", "http_status": http_status}


# ── Return to Directory ────────────────────────────────────────────


def return_to_directory() -> dict:
    """Return to NilRead study list via synthetic dblclick on Directory button.

    Activates Safari + NilRead tab, dispatches full synthetic dblclick,
    then verifies treegrid is visible again.

    Returns: {"status": "returned"} or {"error": ...}
    """
    _activate_nilread()

    dir_js = r"""
    (function() {
        var btn = document.getElementById('studyListButton');
        if (!btn) return JSON.stringify({error: "studyListButton not found"});
        var rect = btn.getBoundingClientRect();
        var cx = rect.left + rect.width / 2;
        var cy = rect.top + rect.height / 2;
        var opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, button: 0, detail: 1};

        // First click
        btn.dispatchEvent(new PointerEvent('pointerdown', opts));
        btn.dispatchEvent(new MouseEvent('mousedown', opts));
        btn.dispatchEvent(new PointerEvent('pointerup', opts));
        btn.dispatchEvent(new MouseEvent('mouseup', opts));
        btn.dispatchEvent(new MouseEvent('click', Object.assign({}, opts, {detail: 1})));

        // Second click + dblclick
        btn.dispatchEvent(new PointerEvent('pointerdown', opts));
        btn.dispatchEvent(new MouseEvent('mousedown', opts));
        btn.dispatchEvent(new PointerEvent('pointerup', opts));
        btn.dispatchEvent(new MouseEvent('mouseup', opts));
        btn.dispatchEvent(new MouseEvent('click', Object.assign({}, opts, {detail: 2})));
        btn.dispatchEvent(new MouseEvent('dblclick', Object.assign({}, opts, {detail: 2})));

        return JSON.stringify({status: "dblclick_dispatched"});
    })()
    """
    result = json.loads(_nilread_js(dir_js))

    if "error" in result:
        print(f"  [ERROR] return_to_directory: {result['error']}")
        return result

    time.sleep(1.5)
    verify_js = """
    (function() {
        var tree = document.querySelector('.dx-treelist');
        var grid = document.querySelector('.dx-datagrid-filter-row');
        return JSON.stringify({
            treelist_visible: !!(tree && tree.offsetWidth > 0),
            filter_row_visible: !!(grid && grid.offsetWidth > 0)
        });
    })()
    """
    verify = json.loads(_nilread_js(verify_js))

    if verify.get("treelist_visible"):
        print("  Returned to directory (treegrid visible)")
    else:
        print("  [WARN] Treegrid not visible after return — may need manual check")

    return {"status": "returned", **verify}


# ── Save current + load next ──────────────────────────────────────


def finish_and_next(current_record: str, selection: set[int],
                    next_record: str | None = None,
                    comments: str = "") -> dict:
    """Save current patient to REDCap, return to directory, and load next patient.

    Runs in a single Python process so _PATIENT_DATA state is preserved.

    Args:
        current_record: record ID to save (accession must be extractable from NilRead)
        selection: image selection set ({0}, {1,2}, etc.)
        next_record: if provided, sets up NilRead search for this record after saving
        comments: optional incl_excl_comments value

    Returns: {"save": ..., "directory": ..., "next_search": ...}
    """
    load_patient(current_record)

    # Extract accession for current patient
    acc = extract_accession(0)
    if "error" in acc:
        return {"error": f"accession extraction failed: {acc['error']}"}

    save_result = save_to_redcap(0, selection, comments=comments)
    dir_result = return_to_directory()

    result = {"save": save_result, "directory": dir_result}

    if next_record:
        print(f"\nLoading next patient (Record {next_record})...")
        load_patient(next_record)
        search_result = setup_nilread_search(0)
        result["next_search"] = search_result

    return result


def _trigger_mrn_search():
    """Trigger NilRead search via JS focus + Enter on the MRN toolbar field.

    Pure JS approach: finds the MRN input, focuses it, and dispatches
    an Enter keydown event to trigger the search.
    """
    _activate_nilread()

    js = '''
    (function() {
        var inputs = document.querySelectorAll("input.dx-texteditor-input");
        for (var i = 0; i < inputs.length; i++) {
            var rect = inputs[i].getBoundingClientRect();
            if (rect.top > 50 && rect.top < 100 && rect.x > 900 && rect.x < 1050) {
                inputs[i].focus();
                inputs[i].dispatchEvent(new KeyboardEvent("keydown",
                    {key: "Enter", code: "Enter", keyCode: 13, bubbles: true}));
                return JSON.stringify({status: "enter_dispatched",
                    value_len: inputs[i].value.length});
            }
        }
        return JSON.stringify({error: "MRN input not found"});
    })()
    '''
    result = json.loads(_nilread_js(js))
    if "error" in result:
        print(f"  [WARN] MRN search trigger: {result['error']}")
    else:
        print(f"  NilRead search triggered (MRN: {result['value_len']} chars)")
    return result


# ── High-level workflow functions ─────────────────────────────────

def process_selection(shorthand: str, comments: str = "") -> dict:
    """Process coordinator's image selection: save to REDCap + export if angio.

    Shorthand format: "type series image" or comma-separated for multiple.
        "0"           → no images
        "2 1 23"      → angiogram, series 1, image 23
        "1 1 5, 2 1 23" → needle Se1/Im5 + angio Se1/Im23

    Full sequence:
        1. If angio (2): export_and_download() + wait 10s
        2. Return to directory
        3. Re-extract accession
        4. Save to REDCap (accession + radios + series/image + completion status)
        5. If angio: rename download → record_id (kept locally)

    Must be called after open_study() with Safari showing the viewer.
    """
    if not _PATIENT_DATA:
        raise RuntimeError("No patient loaded. Run load_patient() first.")

    patient = _PATIENT_DATA[0]
    rid = patient["record_id"]

    # Parse shorthand
    entries = {}  # type -> (series, image)
    shorthand = shorthand.strip()
    if not shorthand:
        return {"record_id": rid, "status": "invalid_input", "error": "Empty selection."}

    if shorthand == "0":
        entries[0] = (None, None)
    else:
        try:
            for part in shorthand.split(","):
                segment = part.strip()
                if not segment:
                    raise ValueError("Empty segment in selection.")

                tokens = segment.split()
                if len(tokens) != 3:
                    raise ValueError(
                        f"Invalid segment '{segment}'. Expected 'type series image'."
                    )

                t = int(tokens[0])
                if t not in (1, 2, 3):
                    raise ValueError(
                        f"Invalid image type '{tokens[0]}'. Use 1, 2, or 3 (or '0' alone)."
                    )

                series = int(tokens[1])
                img = int(tokens[2])
                if series < 1 or img < 1:
                    raise ValueError(
                        f"Invalid segment '{segment}'. Series and image must be positive integers."
                    )
                entries[t] = (series, img)
        except ValueError as exc:
            return {"record_id": rid, "status": "invalid_input", "error": str(exc)}

    if 0 in entries and len(entries) > 1:
        return {
            "record_id": rid,
            "status": "invalid_input",
            "error": "Selection '0' cannot be combined with other image types.",
        }

    no_images = 0 in entries
    has_angio = 2 in entries
    selection = set(entries.keys()) - {0}

    # Step 1: Export if angiogram (still in viewer)
    if has_angio:
        print(f"  Exporting angiogram...")
        export_and_download()
        print(f"  Waiting 10s for download...")
        time.sleep(10)

    # Step 2: Return to directory
    return_to_directory()
    time.sleep(1)

    # Step 3: Re-extract accession
    acc = extract_accession()
    if "error" in acc:
        print(f"  [WARN] Could not re-extract accession: {acc}")

    # Step 4: Save to REDCap
    accession = patient.get("accession", "")
    acc_escaped = json.dumps(accession)

    needle = 1 if 1 in selection else 0
    angio = 1 if 2 in selection else 0
    other = 1 if 3 in selection else 0

    # Build series/image params for JS POST and return metadata
    sent_series_image_fields = {}
    if 1 in entries and entries[1][0] is not None:
        sent_series_image_fields["needle_series_num"] = entries[1][0]
        sent_series_image_fields["needle_image_number"] = entries[1][1]
    if 2 in entries and entries[2][0] is not None:
        sent_series_image_fields["angio_series_number"] = entries[2][0]
        sent_series_image_fields["angio_image_number"] = entries[2][1]
    if 3 in entries and entries[3][0] is not None:
        sent_series_image_fields["other_ind_series_number"] = entries[3][0]
        sent_series_image_fields["other_ind_image_number"] = entries[3][1]
    sent_comment = comments.strip()

    fill_js = f'''
    (function() {{
        var xhr = new XMLHttpRequest();
        xhr.open("GET",
            "/{REDCAP_VERSION}/DataEntry/index.php?pid={REDCAP_PID}"
            + "&id={rid}&page={FORM_PAGE}&event_id={REDCAP_EVENT}&instance=1",
            false);
        xhr.send();
        if (xhr.status !== 200) return JSON.stringify({{error: "GET failed", status: xhr.status}});

        var html = xhr.responseText;
        var csrfMatch = html.match(/redcap_csrf_token[^>]*value=["']([^"']+)["']/);
        if (!csrfMatch) return JSON.stringify({{error: "CSRF not found"}});
        var csrf = csrfMatch[1];

        // Check existing measurements for completion status
        var formComplete;
        if ({1 if no_images else 0}) {{
            formComplete = 2;
        }} else {{
            var doc = new DOMParser().parseFromString(html, "text/html");
            var allMeasured = true;
            if ({needle} === 1) {{
                var m = doc.querySelector("input[name='needle_measurement']");
                if (!m || !m.value.trim()) allMeasured = false;
            }}
            if ({angio} === 1) {{
                var m = doc.querySelector("input[name='angio_measurement']");
                if (!m || !m.value.trim()) allMeasured = false;
            }}
            if ({other} === 1) {{
                var m = doc.querySelector("input[name='other_ind_measurement']");
                if (!m || !m.value.trim()) allMeasured = false;
            }}
            formComplete = allMeasured ? 2 : 1;
        }}

        var hiddenFields = {{}};
        var hiddenRegex = /<input[^>]+type=["']hidden["'][^>]*>/gi;
        var match;
        while ((match = hiddenRegex.exec(html)) !== null) {{
            var inp = match[0];
            var nameMatch = inp.match(/name=["']([^"']+)["']/);
            var valMatch = inp.match(/value=["']([^"']*?)["']/);
            if (nameMatch) hiddenFields[nameMatch[1]] = valMatch ? valMatch[1] : "";
        }}

        var skipFields = ["image_needle_saved", "access_angio_saved", "image_other_ind_saved",
                          "incl_excl_comments", "accession_number",
                          "angio_series_number", "angio_image_number",
                          "needle_series_num", "needle_image_number",
                          "other_ind_series_number", "other_ind_image_number"];

        var params = [];
        params.push("redcap_csrf_token=" + encodeURIComponent(csrf));
        for (var key in hiddenFields) {{
            if (key === "redcap_csrf_token") continue;
            if (skipFields.indexOf(key) >= 0) continue;
            params.push(encodeURIComponent(key) + "=" + encodeURIComponent(hiddenFields[key]));
        }}

        params.push("accession_number=" + encodeURIComponent({acc_escaped}));
        params.push("image_needle_saved={needle}");
        params.push("access_angio_saved={angio}");
        params.push("image_other_ind_saved={other}");
        var seriesImageFields = {json.dumps(sent_series_image_fields)};
        for (var key in seriesImageFields) {{
            params.push(encodeURIComponent(key) + "=" + encodeURIComponent(seriesImageFields[key]));
        }}

        var commentsVal = {json.dumps(sent_comment)};
        if (commentsVal) {{
            params.push("incl_excl_comments=" + encodeURIComponent(commentsVal));
        }}
        params.push("{FORM_PAGE}_complete=" + formComplete);
        params.push("submit-action=submit-btn-saverecord");
        params.push("submit-btn-saverecord=Save+%26+Exit+Record");

        var xhr2 = new XMLHttpRequest();
        xhr2.open("POST",
            "/{REDCAP_VERSION}/DataEntry/index.php?pid={REDCAP_PID}"
            + "&event_id={REDCAP_EVENT}&page={FORM_PAGE}&instance=1",
            false);
        xhr2.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
        xhr2.send(params.join("&"));

        return JSON.stringify({{
            status: xhr2.status,
            accession_len: {acc_escaped}.length,
            form_complete: formComplete
        }});
    }})()
    '''
    raw = _redcap_js(fill_js, timeout=15.0)
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print(f"  [WARN] Record {rid}: parse failed")
        return {"record_id": rid, "status": "failed"}

    status_label = "Complete" if result.get("form_complete") == 2 else "Unverified"
    sel_str = "none" if no_images else ", ".join(
        f"{t}(Se{entries[t][0]}/Im{entries[t][1]})" for t in sorted(selection) if entries[t][0]
    )
    series_image_str = ", ".join(
        f"{k}={v}" for k, v in sent_series_image_fields.items()
    ) or "none"
    comment_str = repr(sent_comment) if sent_comment else "none"
    print(
        f"  Record {rid}: saved ({sel_str}, series/image={series_image_str}, "
        f"comment={comment_str}, accession={result.get('accession_len', 0)} chars, "
        f"status={status_label}) → HTTP {result.get('status')}"
    )

    # Step 5: If angio, rename download folder to record_id (keep locally)
    if has_angio:
        _rename_download(rid)

    screen.launch_app("Terminal")
    return {
        "record_id": rid,
        "status": "saved",
        "form_complete": result.get("form_complete"),
        "sent_series_image_fields": sent_series_image_fields,
        "sent_comment": sent_comment,
    }


def save_and_next(shorthand: str, record_id: str, comments: str = "") -> dict:
    """One-call post-input: save current patient, then open next.

    Handles load_patient() internally (state is lost between Python calls).

    Args:
        shorthand: coordinator selection (e.g. "0", "2 1 23", "1 1 5, 2 1 23")
        record_id: current patient's record ID (from previous next_patient result)
        comments: optional incl_excl_comments value

    Returns: {"save": {process_selection result}, "next": {next_patient result}}
    """
    load_patient(record_id)
    current_id = record_id

    # Save current patient
    save_result = process_selection(shorthand, comments=comments)
    if save_result.get("status") == "invalid_input":
        return {"save": save_result, "next": {"status": "not_advanced", "reason": "invalid_input"}}

    # Advance to next patient
    next_result = next_patient(current_id)

    return {"save": save_result, "next": next_result}


def _rename_download(record_id: str):
    """Rename most recent NilRead download folder to record_id. Keeps locally in ~/Downloads."""
    import glob as glob_mod
    dl_dir = os.path.expanduser("~/Downloads")
    candidates = sorted(
        glob_mod.glob(os.path.join(dl_dir, "Nil_downloaded_image_*")),
        key=os.path.getmtime, reverse=True
    )
    if not candidates:
        print(f"  [WARN] No Nil_downloaded_image folder found in Downloads")
        return

    src = candidates[0]
    dst = os.path.join(dl_dir, record_id)
    if os.path.exists(dst):
        print(f"  [WARN] {dst} already exists, skipping rename")
        return
    os.rename(src, dst)
    print(f"  Renamed → ~/Downloads/{record_id}")


def preload_next_patient(after_record_id: str) -> dict:
    """Preload next femoral-eligible patient's REDCap demographics.

    Designed for background use while coordinator reviews the current study.
    Returns internal patient_data (contains PHI) for immediate next-step use.
    """
    try:
        all_records = _get_dashboard_records(force_reload=False)
    except Exception as exc:
        return {
            "status": "error",
            "after_record_id": after_record_id,
            "error": f"dashboard lookup failed: {exc}",
        }

    if not all_records:
        return {"status": "error", "after_record_id": after_record_id, "error": "no records"}

    if after_record_id in all_records:
        start_idx = all_records.index(after_record_id) + 1
    else:
        start_idx = 0

    for idx in range(start_idx, len(all_records)):
        record_id = all_records[idx]
        patient = _fetch_patient_data(record_id)
        if "error" in patient:
            continue
        if not patient.get("has_femoral", True):
            continue
        return {
            "status": "ready",
            "after_record_id": after_record_id,
            "record_id": record_id,
            "position": f"{idx + 1} of {len(all_records)}",
            "patient_data": patient,
        }

    return {"status": "no_more_records", "after_record_id": after_record_id}


def next_patient_from_prefetch(last_record_id: str | None, prefetch: dict | None = None) -> dict:
    """Advance to next patient, using a preloaded patient when available."""
    if (
        prefetch
        and prefetch.get("status") == "ready"
        and prefetch.get("after_record_id") == last_record_id
        and isinstance(prefetch.get("patient_data"), dict)
    ):
        # Ensure NilRead is in directory view before searching.
        try:
            return_to_directory()
        except Exception:
            pass

        patient = prefetch["patient_data"]
        record_id = patient["record_id"]
        position = prefetch.get("position", "?")
        if not patient.get("has_femoral", True):
            print(f"  Skipping {record_id} — no femoral access site (prefetch guard)")
            return next_patient(record_id)

        global _PATIENT_DATA
        _PATIENT_DATA = [patient]

        print(f"  Next: Record {record_id} (#{position}) [prefetched]")
        setup_nilread_search()
        time.sleep(3)

        acc = extract_accession()
        if "error" in acc:
            print(f"  No studies found for {record_id}. Returning to Terminal.")
            screen.launch_app("Terminal")
            return {
                "record_id": record_id,
                "position": position,
                "error": acc["error"],
                "prefetched": True,
            }

        open_study()
        print(f"  Record {record_id} ready. Safari focused. (prefetched)")
        return {"record_id": record_id, "position": position, **acc, "prefetched": True}

    return next_patient(last_record_id)


def next_patient(last_record_id: str | None = None) -> dict:
    """Advance to the next record in dashboard order and open the study.

    If last_record_id is None, starts from the first record.

    Full sequence:
        0. Return to NilRead directory (required before new search)
        1. Reload dashboard, get ordered record list
        2. Find next record after last_record_id (skip non-femoral)
        3. load_patient() + setup_nilread_search() (JS-only, no OS clicks)
        4. Wait 3s, extract_accession()
        5. open_study() — leaves Safari focused

    Returns: {"record_id": str, "position": "N of M", ...accession info}
             or {"error": str}
    """
    # Step 0: Return to NilRead directory (must be on directory view before searching)
    try:
        return_to_directory()
    except Exception:
        pass  # May already be on directory or first run

    # Step 1: Reload dashboard
    all_records = _get_dashboard_records(force_reload=True)

    if not all_records:
        print("  No records found on dashboard")
        return {"error": "no records"}

    # Step 2: Find next record
    if last_record_id is None:
        idx = 0
    else:
        try:
            idx = all_records.index(last_record_id) + 1
        except ValueError:
            print(f"  [WARN] {last_record_id} not in dashboard, starting from first")
            idx = 0

    if idx >= len(all_records):
        print("  No more records on dashboard")
        return {"error": "no more records"}

    # Step 2b: Load patient, skip non-femoral access sites automatically
    while idx < len(all_records):
        record_id = all_records[idx]
        pos = f"{idx + 1} of {len(all_records)}"
        print(f"  Next: Record {record_id} (#{pos})")

        # Step 3: Load patient (checks access site)
        summary = load_patient(record_id)
        if not summary.get("has_femoral", True):
            print(f"  Skipping {record_id} — no femoral access site")
            idx += 1
            continue
        break
    else:
        print("  No more records on dashboard")
        return {"error": "no more records"}

    # Step 4: Search NilRead (clears fields, MRN+Enter, date+Enter)
    setup_nilread_search()

    # Step 5: Wait + extract
    time.sleep(3)
    acc = extract_accession()
    if "error" in acc:
        print(f"  No studies found for {record_id}. Returning to Terminal.")
        screen.launch_app("Terminal")
        return {"record_id": record_id, "position": pos, "error": acc["error"]}

    # Step 6: Open study
    open_study()
    print(f"  Record {record_id} ready. Safari focused.")
    return {"record_id": record_id, "position": pos, **acc}


# ── Safe viewport screenshot (PHI-free center crop) ──────────────


def _get_safari_window_position() -> tuple[int, int]:
    """Get Safari window (x, y) position via System Events AppleScript."""
    result = subprocess.run(['osascript', '-e', '''
tell application "System Events"
    tell process "Safari"
        set p to position of front window
        set x to item 1 of p
        set y to item 2 of p
        return (x as text) & "|" & (y as text)
    end tell
end tell
'''], capture_output=True, text=True)
    parts = result.stdout.strip().split('|')
    return int(parts[0]), int(parts[1])


def safe_viewport_screenshot(
    output_path: str = "/tmp/nilread_viewport.png",
    margin_top: float = 0.28,
    margin_bottom: float = 0.15,
    margin_left: float = 0.30,
    margin_right: float = 0.25,
) -> dict:
    """Capture center crop of NilRead viewer image, excluding all PHI overlays.

    NilRead draws demographics text (name, MRN, DOB, accession, institution)
    on a canvas overlay at the corners of the viewport. This function:
    1. Queries the panelImage canvas bounds via JS
    2. Applies configurable inward margins to exclude corner PHI text
    3. Converts viewport coords → screen coords (Safari window offset + toolbar)
    4. Uses screencapture to grab only the safe center region

    Default margins (fraction of canvas dims):
        top=0.28    — clears patient name, MRN, accession, series info (top-left)
        bottom=0.15 — clears zoom/WC/WW info (bottom-left)
        left=0.30   — clears series panel + patient demographics text
        right=0.25  — clears institution name + equipment info (top-right)

    Returns: {"path": str, "screen_region": {x,y,w,h}, "viewport_region": {x,y,w,h}}
             or {"error": str}
    """
    _activate_nilread()

    # Step 1: Get panelImage canvas bounds in viewport coordinates
    canvas_js = '''
    (function() {
        var canvases = document.querySelectorAll('canvas.panelImage');
        for (var i = 0; i < canvases.length; i++) {
            var r = canvases[i].getBoundingClientRect();
            if (r.width > 100 && r.height > 100) {
                return JSON.stringify({
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height)
                });
            }
        }
        return JSON.stringify({error: "panelImage canvas not found"});
    })()
    '''
    canvas_info = json.loads(_nilread_js(canvas_js))
    if "error" in canvas_info:
        print(f"  [ERROR] safe_viewport_screenshot: {canvas_info['error']}")
        return canvas_info

    cx, cy, cw, ch = canvas_info["x"], canvas_info["y"], canvas_info["w"], canvas_info["h"]

    # Step 2: Apply inward margins to get PHI-free center region
    crop_x = cx + int(cw * margin_left)
    crop_y = cy + int(ch * margin_top)
    crop_w = int(cw * (1.0 - margin_left - margin_right))
    crop_h = int(ch * (1.0 - margin_top - margin_bottom))

    if crop_w < 100 or crop_h < 100:
        return {"error": f"crop too small: {crop_w}x{crop_h}"}

    # Step 3: Convert viewport coords → screen coords
    win_x, win_y = _get_safari_window_position()
    safari_toolbar = 74  # tab bar + address bar height in points

    screen_x = win_x + crop_x
    screen_y = win_y + safari_toolbar + crop_y
    screen_w = crop_w
    screen_h = crop_h

    # Step 4: Capture region using macOS screencapture
    # -R flag takes a region: x,y,w,h (in points, Retina-aware)
    subprocess.run([
        "screencapture", "-x", "-R",
        f"{screen_x},{screen_y},{screen_w},{screen_h}",
        output_path,
    ], capture_output=True, check=True)

    # Always return to Terminal after capturing
    screen.launch_app("Terminal")

    print(f"  Safe viewport screenshot saved: {output_path} "
          f"(crop: {crop_w}x{crop_h} from canvas {cw}x{ch})")

    return {
        "path": output_path,
        "screen_region": {"x": screen_x, "y": screen_y, "w": screen_w, "h": screen_h},
        "viewport_region": {"x": crop_x, "y": crop_y, "w": crop_w, "h": crop_h},
        "canvas_size": {"w": cw, "h": ch},
    }
