#!/usr/bin/env python3
"""Interactive femoral access imaging loop.

Single long-running process — no state loss between patients.
Run directly in Terminal (not via Claude — stdin doesn't work there).

Usage:
    python3 -m epic_extractor.femoral_loop <last_record_id>

    Then type selections at the prompt:
        0              → no images
        2 1 23         → angiogram, series 1, image 23
        2 1 23 DSA     → angiogram + DSA comment
        1 1 5, 2 1 23  → needle + angiogram
        redo           → redo current patient (re-search + re-open)
        quit           → exit
"""
import sys
import threading
import epic_extractor.femoral_batch as fb


def _parse_input(line: str) -> tuple[str, str]:
    """Parse selection line into (shorthand, comments).

    Comment is the last token if it's non-numeric (e.g. 'DSA' in '2 1 23 DSA').
    Everything before it is the shorthand.
    """
    parts = line.strip().split()
    if not parts:
        return "", ""

    # Check if last token is a non-numeric comment
    last = parts[-1]
    if len(parts) > 1 and not last.replace(",", "").isdigit():
        return " ".join(parts[:-1]), last
    return line.strip(), ""


def _start_prefetch(after_record_id: str):
    """Start background preload of the next femoral-eligible record."""
    state = {"result": None}

    def _worker():
        try:
            state["result"] = fb.preload_next_patient(after_record_id)
        except Exception as exc:
            state["result"] = {
                "status": "error",
                "after_record_id": after_record_id,
                "error": str(exc),
            }

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t, state


def _get_prefetch_result(prefetch_thread, prefetch_state):
    """Return prefetch result if background preload has completed."""
    if not prefetch_thread or not prefetch_state:
        return None
    if prefetch_thread.is_alive():
        return None
    return prefetch_state.get("result")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 -m epic_extractor.femoral_loop <last_record_id>")
        sys.exit(1)

    last_id = sys.argv[1]

    # Load first patient
    result = fb.next_patient(last_id)
    if "error" in result:
        print(f"  Error: {result['error']}")
        sys.exit(1)

    current_id = result["record_id"]
    print(f"\n  Ready: Record {current_id} ({result.get('position', '?')})")
    print(f"  Study: {result.get('desc_prefix', '?')} ({result.get('modality', '?')}), "
          f"{result.get('images', '?')} images\n")
    prefetch_thread, prefetch_state = _start_prefetch(current_id)

    while True:
        try:
            line = input("Selection: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nDone.")
            break

        if not line:
            continue
        if line.lower() == "quit":
            break
        if line.lower() == "redo":
            result = fb.next_patient(last_id)
            if "error" in result:
                print(f"  Error: {result['error']}")
                continue
            current_id = result["record_id"]
            print(f"\n  Ready: Record {current_id} ({result.get('position', '?')})")
            print(f"  Study: {result.get('desc_prefix', '?')} ({result.get('modality', '?')}), "
                  f"{result.get('images', '?')} images\n")
            prefetch_thread, prefetch_state = _start_prefetch(current_id)
            continue

        shorthand, comments = _parse_input(line)

        # Save + advance
        save_result = fb.process_selection(shorthand, comments=comments)
        if save_result.get("status") == "invalid_input":
            print(f"  Invalid selection: {save_result.get('error', 'Unknown input error.')}")
            continue

        last_id = current_id

        prefetch = _get_prefetch_result(prefetch_thread, prefetch_state)
        result = fb.next_patient_from_prefetch(current_id, prefetch)
        if "error" in result:
            print(f"\n  Saved {current_id}.")
            print(f"  {result['error']}")
            break

        current_id = result["record_id"]
        print(f"\n  Ready: Record {current_id} ({result.get('position', '?')})")
        print(f"  Study: {result.get('desc_prefix', '?')} ({result.get('modality', '?')}), "
              f"{result.get('images', '?')} images\n")
        prefetch_thread, prefetch_state = _start_prefetch(current_id)


if __name__ == "__main__":
    main()
