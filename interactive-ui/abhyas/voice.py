import json
import os
import re
import urllib.error
import urllib.request

from . import controls as K
from . import nlu

API_KEY_ENV = "ABHYAS_LLM_API_KEY"
BASE_URL = os.environ.get("ABHYAS_LLM_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("ABHYAS_LLM_MODEL", "openai/gpt-oss-120b")
TIMEOUT_S = float(os.environ.get("ABHYAS_LLM_TIMEOUT_S", "12"))

DEEPGRAM_KEY_ENV = "ABHYAS_DEEPGRAM_API_KEY"
DEEPGRAM_MODEL = os.environ.get("ABHYAS_DEEPGRAM_MODEL", "nova-2")
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"


def llm_available():
    return bool(os.environ.get(API_KEY_ENV))


def deepgram_available():
    return bool(os.environ.get(DEEPGRAM_KEY_ENV))


def backend_status():
    if llm_available():
        status = {"backend": "llm", "model": MODEL, "base_url": BASE_URL,
                  "offline": False,
                  "note": "Spoken text goes to " + BASE_URL + " when the local "
                          "rules can't place it. The simulation still runs here."}
    else:
        status = {"backend": "local-rules", "model": None, "base_url": None,
                  "offline": True,
                  "note": "Parsed on this machine. Set " + API_KEY_ENV
                          + " to add a hosted fallback."}
    if deepgram_available():
        status["stt"] = {"backend": "deepgram", "model": DEEPGRAM_MODEL,
                         "note": "Mic audio is streamed to Deepgram."}
    else:
        status["stt"] = {"backend": None, "model": None,
                         "note": "Voice input is off. Set " + DEEPGRAM_KEY_ENV
                                 + " to enable it."}
    return status


def edits_for(instruction, state):
    """Intent -> named control edits. Relative asks ("ten more seconds") get
    resolved against the live values here, so the dial that moves is the dial
    that was read."""
    action, params = instruction.action, instruction.params

    if action in ("adjust_green", "set_green"):
        cid = params["phase_group"] + ".green"
        current = float(state.get(cid, 0.0))
        value = (current + float(params["delta_seconds"])
                 if action == "adjust_green" else float(params["seconds"]))
        return [{"id": cid, "value": value}]

    if action == "set_demand":
        cid = "demand.veh_per_hour"
        if "multiplier" in params:
            value = float(state.get(cid, 0.0)) * float(params["multiplier"])
        else:
            value = float(params["veh_per_hour"])
        return [{"id": cid, "value": round(value)}]

    if action == "add_obstruction":
        return [{"id": "obstruction." + params["kind"] + "." + params["arm"],
                 "value": True}]

    if action == "clear_obstructions":
        return [{"id": cid, "value": False} for cid, value in state.items()
                if cid.startswith("obstruction.") and value]

    return []           # pause/resume/reset/status move no dial


# ---- hosted fallback -----------------------------------------------------

SYSTEM_PROMPT = """You translate a spoken sentence into edits on a fixed set of \
traffic-signal controls for one junction in Bengaluru (CMH Road x 100 Feet Road).

Reply with JSON only, no prose:
{"ok": true, "summary": "<one short sentence>", "edits": [{"id": "<control id>", "value": <number or boolean>}]}
or, when the request is not something these controls can do:
{"ok": false, "reason": "<one sentence saying what is out of scope>"}

Rules:
- Use only the control ids listed. Never invent one.
- Values are absolute. If the person says "ten more seconds", add ten to the \
current value yourself.
- Left-hand traffic: a left turn is free, a right turn crosses opposing traffic.
- Pedestrians, weather, adaptive control, emissions and other junctions are not \
modelled. Return ok:false for those.
- Never guess a number that was not said.

Controls:
"""


def _schema_prompt():
    lines = []
    for control in K.declare():
        if control["kind"] == "toggle":
            lines.append(control["id"] + " : on/off -- " + control["label"])
        else:
            lines.append(control["id"] + " : " + str(control["min"]) + " to "
                         + str(control["max"]) + " "
                         + (control["unit"] or "fraction") + " -- "
                         + control["label"])
    return "\n".join(lines)


def _call_llm(utterance, state):
    current = "\n".join(cid + " = " + str(state[cid]) for cid in sorted(state))
    body = json.dumps({
        "model": MODEL, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + _schema_prompt()
                                          + "\n\nCurrent values:\n" + current},
            {"role": "user", "content": utterance}],
    }).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ[API_KEY_ENV],
                 "User-Agent": "abhyas-interactive-ui/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return json.loads(payload["choices"][0]["message"]["content"])


def _validate(raw):
    """Hold the model to the same rules the local parser obeys."""
    if not raw.get("ok"):
        return {"ok": False, "source": "llm", "edits": [], "notes": [],
                "reason": raw.get("reason") or "The model declined the request."}
    edits, notes = [], []
    for edit in raw.get("edits") or []:
        try:
            control = K.lookup(edit.get("id"))
        except K.Rejected as exc:
            notes.append(str(exc) + " Dropped.")
            continue
        value = edit.get("value")
        if control["kind"] == "toggle":
            edits.append({"id": control["id"], "value": bool(value)})
            continue
        try:
            edits.append({"id": control["id"], "value": float(value)})
        except (TypeError, ValueError):
            notes.append(control["label"] + " needs a number and got "
                         + repr(value) + ". Dropped.")
    if not edits:
        return {"ok": False, "source": "llm", "edits": [], "notes": notes,
                "reason": "Nothing in that mapped onto a control this junction has."}
    return {"ok": True, "source": "llm", "edits": edits, "notes": notes,
            "summary": raw.get("summary") or "Adjust the controls as asked."}


FAMILIES = {
    "signal": r"\b(green|signal|light|phase|timer|timing|cycle|amber|yellow)\b",
    "obstruction": r"\b(cow|cattle|bull|buffalo|stalled|broken down|roadworks|"
                   r"road works|digging|barricade|animal)\b",
    "demand": r"\b(traffic|demand|volume|vehicles|flow|busier|quieter|rush|peak)\b",
}

FAMILY_OF_ACTION = {"adjust_green": "signal", "set_green": "signal",
                    "add_obstruction": "obstruction",
                    "clear_obstructions": "obstruction",
                    "set_demand": "demand"}


def unread_families(utterance, action):
    spoken = {name for name, pattern in FAMILIES.items()
              if re.search(pattern, utterance.lower())}
    read = FAMILY_OF_ACTION.get(action)
    return sorted(spoken - {read}) if read and len(spoken) > 1 else []


class TranscriptAssembler:

    def __init__(self):
        self.segments = []

    def sentence(self):
        return " ".join(self.segments).strip()

    def flush(self):
        sentence = self.sentence()
        self.segments = []
        return [("final", sentence)] if sentence else []


    FLUSH_EVENTS = ("Metadata", "UtteranceEnd")

    def feed(self, event):
        """One Deepgram message -> the ('partial'|'final', text) to send on."""
        kind = event.get("type")
        if kind in self.FLUSH_EVENTS:
            return self.flush()

        channel = event.get("channel")
        if not isinstance(channel, dict):
            return []                    # SpeechStarted and anything else new
        alternatives = channel.get("alternatives") or [{}]
        first = alternatives[0] if isinstance(alternatives[0], dict) else {}
        text = first.get("transcript", "")
        if not text:
            return []

        if not event.get("is_final"):
            # still being revised, show it against what's already settled
            return [("partial", " ".join(self.segments + [text]).strip())]

        self.segments.append(text)
        if event.get("speech_final"):
            return self.flush()
        return [("partial", self.sentence())]


def interpret(utterance, state, allow_llm=True):
    instruction = nlu.parse(utterance)
    if instruction.ok:
        notes = list(instruction.notes)
        missed = unread_families(utterance, instruction.action)
        if missed:
            notes.append("This sentence also mentions " + " and ".join(missed)
                         + ", and the parser reads one request at a time. Only "
                           "the change shown was taken - ask for the rest "
                           "separately.")
        return {"ok": True, "source": instruction.source,
                "action": instruction.action, "params": instruction.params,
                "summary": instruction.summary, "notes": notes,
                "corrections": list(instruction.corrections),
                "edits": edits_for(instruction, state)}

    # We're scoping out models that aren't rendered in the proejction we ue via indiranagar_cmh
    scoped_out = any(phrase in (instruction.reason or "").lower()
                     for phrase in ("not modelled", "not implemented",
                                    "not in the network", "only one junction",
                                    "only cmh road"))
    if scoped_out or not (allow_llm and llm_available()):
        return {"ok": False, "source": instruction.source,
                "reason": instruction.reason, "edits": [],
                "notes": list(instruction.notes), "utterance": utterance}

    try:
        answer = _validate(_call_llm(utterance, state))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
        detail = type(exc).__name__
        if isinstance(exc, urllib.error.HTTPError):
            detail = "HTTP " + str(exc.code) + ": " + exc.read().decode(
                "utf-8", "replace")[:200]
        return {"ok": False, "source": "local-rules", "edits": [],
                "reason": instruction.reason, "utterance": utterance,
                "notes": list(instruction.notes)
                         + ["The hosted parser did not answer (" + detail
                            + "). The local reading stands."]}

    answer["utterance"] = utterance
    answer.setdefault("notes", []).append(
        "The local rules couldn't place this so it went to " + MODEL
        + " and the reply was checked against the control surface first.")
    return answer


if __name__ == "__main__":
    from . import demand as D
    from . import tls as T
    live = K.state_of(T.baseline_plan(), D.DemandSpec(), [])
    for line in ("add 10 seconds to the north-south green",
                 "put a cow on the east approach",
                 "make it 30% busier",
                 "give the pedestrians a phase"):
        result = interpret(line, live, allow_llm=False)
        print(format(line, "42"), "->",
              result.get("summary") or result.get("reason"))
        print(" " * 46, result["edits"])
    print("\nbackend:", backend_status()["backend"])
