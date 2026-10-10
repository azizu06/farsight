"""Turn /ask questions into confirm-first proposals and keep model answers from claiming motion.

The model never decides motion: a keyword match proposes an action, the Quest asks the
operator to confirm it, and only then does the normal deadman/permit control path run it.
"""

import math
import re

SEEK = re.compile(r"\b(go|drive|move|head|get|roll)\s+(to|toward|towards|closer)\b|\b(seek|approach|find)\b", re.I)
SCAN = re.compile(r"\bscan\b|\blook around\b|\bsweep\b", re.I)
SUBJECT = r"\b(i|i'm|i am|i'll|i will|we|we're|we'll|let me|let's)\s+(\w+\s+){0,2}?"
MOTION = (r"(go|going|head|heading|move|moving|drive|driving|approach|approaching|turn|turning|"
          r"roll|rolling|navigate|navigating|steer|steering)\b")
CLAIM = re.compile(SUBJECT + MOTION + r"|^(okay,?\s*|sure,?\s*)?(moving|heading|driving|going|approaching|"
                   r"turning|rolling|navigating)\b", re.I)


def located(boxes):
    return [box for box in boxes if isinstance(box, dict) and all(
        type(box.get(key)) in (int, float) and math.isfinite(box[key]) for key in ("x", "y", "w", "h"))]


def propose_action(question, boxes):
    """Spoken command to a proposal the operator must confirm; None for plain questions."""
    if SCAN.search(question):
        return {"type": "scan"}
    if SEEK.search(question):
        people = [box for box in located(boxes) if box.get("cls") == "person" and type(box.get("id")) is int]
        if not people:
            return None
        # A person flagged down is the rescue priority; otherwise the largest box is the nearest.
        target = max(people, key=lambda box: (box.get("down") is True, box["w"] * box["h"]))
        return {"type": "seek", "target_id": target["id"]}
    return None


def summarize_boxes(boxes):
    """Compact grounding text for the model, e.g. 'person #3 left close (down)'."""
    words = []
    for box in located(boxes)[:12]:
        center = box["x"] + box["w"] / 2
        side = "left" if center < .35 else "right" if center > .65 else "center"
        distance = "close" if box["h"] >= .5 else "mid-range" if box["h"] >= .25 else "far"
        label = re.sub(r"[^a-z0-9 _-]", "", str(box.get("cls", "object")).lower())[:40]
        identifier = f' #{box["id"]}' if type(box.get("id")) is int else ""
        words.append(f"{label}{identifier} {side} {distance}" + (" (down)" if box.get("down") is True else ""))
    return "; ".join(words) or "nothing detected"


def guard_answer(answer, action, max_sentences=2):
    """Drop sentences claiming the rover moves, keep the first few, then name any pending proposal."""
    sentences = re.split(r"(?<=[.!?])\s+", answer.replace("’", "'").strip())
    # Spoken in the headset, so model answers stay short.
    kept = " ".join([sentence for sentence in sentences if sentence and not CLAIM.search(sentence)][:max_sentences])
    kept = kept or "I can't move the rover myself."
    if action and action["type"] == "seek":
        kept += f' Confirm on the headset to seek person #{action["target_id"]}.'
    elif action and action["type"] == "scan":
        kept += " Confirm on the headset to scan the room."
    return kept
