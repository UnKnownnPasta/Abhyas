import json
import os
import re
import urllib.error
import urllib.request

from . import config as C
from . import controls as K
from . import nlu

# The only actions the model may return instead of edits: the ones that move
# no dial. Anything that does move one comes back as edits, so it goes through
# the same lookup and clamp the sliders do rather than a second code path.
MODEL_ACTIONS = ("pause", "resume", "reset", "status",
                 "run_validation", "run_counterfactual")

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
                  "note": "Spoken text goes to " + BASE_URL + ", which writes "
                          "the settings. Local rules answer if it can't. The "
                          "simulation still runs here."}
    else:
        status = {"backend": "local-rules", "model": None, "base_url": None,
                  "offline": True,
                  "note": "Parsed on this machine. Set " + API_KEY_ENV
                          + " to have a model write the settings instead."}
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

    if action == "restrict_access":
        return [{"id": "access." + params["vehicle_class"] + "." + params["arm"],
                 "value": bool(params.get("banned", True))}]

    if action == "inject_fleet":
        # absolute, not relative: a scheme adds N vehicles, it doesn't scale
        # whatever happens to be on the road
        return [{"id": "fleet.injected." + params["vehicle_class"],
                 "value": float(params["veh_per_hour"])}]

    if action == "set_mode_shift":
        return [{"id": "fleet.mode_shift", "value": float(params["fraction"])}]

    if action == "set_hmv_discipline":
        return [{"id": "fleet.hmv_discipline", "value": float(params["value"])}]

    if action == "add_obstruction":
        return [{"id": "obstruction." + params["kind"] + "." + params["arm"],
                 "value": True}]

    if action == "clear_obstructions":
        return [{"id": cid, "value": False} for cid, value in state.items()
                if cid.startswith("obstruction.") and value]

    return []           # pause/resume/reset/status move no dial


# ---- the hosted reader, which writes the settings -------------------------

SYSTEM_PROMPT = """You translate a spoken sentence into settings for a fixed set \
of traffic-signal controls for one junction in Bengaluru (CMH Road x 100 Feet Road).

Reply with JSON only, no prose. To change settings:
{"ok": true, "summary": "<one short sentence>", "edits": [{"id": "<control id>", "value": <number or boolean>}]}
To run one of the commands listed below instead, which move no dial:
{"ok": true, "summary": "<one short sentence>", "action": "<command>", "params": {...}}
When the request is not something this junction can do:
{"ok": false, "reason": "<one sentence saying what is out of scope>"}

Rules:
- Use only the control ids listed. Never invent one.
- Values are absolute. If the person says "ten more seconds", add ten to the \
current value yourself.
- An approach shows red whenever a different phase holds the green. There is no \
red-duration control for an approach: <group>.allred is only the short all-red \
clearance between phases, never how long one approach waits. So "fifteen more \
seconds of red on north" means adding fifteen seconds of green to the phases \
that are not north - split it across them - and "less red on north" means \
adding green to north's own phase. Say in the summary how you split it.
- A sentence that tells you NOT to do something, or takes back something \
already said, asks for no change. Return ok:false with a reason saying so. \
Never drop the "don't" and make the change anyway.
- Left-hand traffic: a left turn is free, a right turn crosses opposing traffic.
- Pedestrians, weather, adaptive control, emissions and other junctions are not \
modelled. Return ok:false for those.
- Never guess a number that was not said.

Commands (use these only when no dial moves): """ + ", ".join(sorted(MODEL_ACTIONS)) + """.
run_counterfactual takes params {"phase_group": one of """ + ", ".join(C.PHASE_GROUPS) \
+ """, "delta_seconds": <number>, "seeds": <number>}; run_validation takes \
{"seeds": <number>}; the rest take no params.

Controls:
"""


def _schema_prompt():
    lines = []
    for control in K.declare():
        if control["kind"] == "toggle":
            lines.append(control["id"] + " : on/off -- " + control["label"])
        elif control["kind"] == "choice":
            options = "|".join(o["value"] for o in control.get("options") or [])
            lines.append(control["id"] + " : one of " + options + " -- "
                         + control["label"])
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


def _refused(reason, notes=None):
    return {"ok": False, "source": "llm", "edits": [],
            "notes": list(notes or []), "reason": reason}


def _validate_action(raw):
    """A command the model asked for, held to the same closed list /api/execute
    checks against."""
    action = raw.get("action")
    if action not in MODEL_ACTIONS:
        return _refused("The model asked for '" + str(action) + "', which is "
                        "not a command this junction takes.")

    given = raw.get("params") or {}
    params = {}
    if action == "run_counterfactual":
        group = given.get("phase_group")
        if group not in C.PHASE_GROUPS:
            return _refused("The model named a phase group this plan doesn't "
                            "have (" + str(group) + ").")
        params["phase_group"] = group
        try:
            params["delta_seconds"] = float(given.get("delta_seconds"))
        except (TypeError, ValueError):
            return _refused("A counterfactual needs a number of seconds to "
                            "test, and none was given.")
    if action in ("run_validation", "run_counterfactual"):
        try:
            params["seeds"] = max(4, min(60, int(given.get("seeds") or 30)))
        except (TypeError, ValueError):
            params["seeds"] = 30

    return {"ok": True, "source": "llm", "edits": [], "action": action,
            "params": params, "notes": [],
            "summary": raw.get("summary") or action.replace("_", " ")}


def _validate(raw):
    """Hold the model to the same rules the local parser obeys."""
    if not raw.get("ok"):
        return _refused(raw.get("reason")
                        or "The model declined the request.")
    if raw.get("action"):
        return _validate_action(raw)
    proposed = raw.get("edits") or []
    edits, notes = [], []
    for edit in proposed:
        try:
            control = K.lookup(edit.get("id"))
        except K.Rejected as exc:
            notes.append(str(exc) + " Dropped.")
            continue
        value = edit.get("value")
        if control["kind"] == "toggle":
            edits.append({"id": control["id"], "value": bool(value)})
            continue
        if control["kind"] == "choice":
            allowed = [o["value"] for o in control.get("options") or []]
            if value not in allowed:
                notes.append(control["label"] + " is one of "
                             + ", ".join(allowed) + " and got " + repr(value)
                             + ". Dropped.")
                continue
            edits.append({"id": control["id"], "value": value})
            continue
        try:
            asked = float(value)
        except (TypeError, ValueError):
            notes.append(control["label"] + " needs a number and got "
                         + repr(value) + ". Dropped.")
            continue
        # Clamp here rather than at apply time: the proposal card is where
        # someone decides, so a value the surface won't take should say so
        # before they accept it, not after.
        clamped, note = K.clamp(control, asked)
        if note:
            notes.append(note)
        edits.append({"id": control["id"], "value": clamped})
    if not edits:
        # "proposed nothing" and "proposed twelve things, all of them bogus"
        # are different failures and used to read as the same refusal. A
        # sentence that asks for no change - "don't add ten seconds" - lands
        # in the first, and calling that out of scope is a lie: the control
        # it names usually exists.
        if not proposed:
            return _refused("That asks for no change to the junction, so "
                            "nothing was proposed.", notes)
        return _refused("Nothing in that mapped onto a control this junction "
                        "has.", notes)
    return {"ok": True, "source": "llm", "edits": edits, "notes": notes,
            "summary": raw.get("summary") or "Adjust the controls as asked."}


FAMILIES = {
    "signal": r"\b(green|signal|light|phase|timer|timing|cycle|amber|yellow)\b",
    "obstruction": r"\b(cow|cattle|bull|buffalo|stalled|broken down|roadworks|"
                   r"road works|digging|barricade|animal)\b",
    "demand": r"\b(traffic|demand|volume|vehicles|flow|busier|quieter|rush|peak)\b",
    "fleet": r"\b(bus|buses|truck|trucks|lorry|hmv|hcv|two.?wheelers?|2w|"
             r"scooters?|autos?|rickshaws?|fleet|transit|public transport)\b",
}

FAMILY_OF_ACTION = {"adjust_green": "signal", "set_green": "signal",
                    "add_obstruction": "obstruction",
                    "clear_obstructions": "obstruction",
                    "set_demand": "demand",
                    "restrict_access": "fleet", "inject_fleet": "fleet",
                    "set_mode_shift": "fleet", "set_hmv_discipline": "fleet"}


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


def _error_detail(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP " + str(exc.code) + ": " + exc.read().decode(
            "utf-8", "replace")[:200]
    return type(exc).__name__


def interpret(utterance, state, allow_llm=True):
    """The model reads the sentence; the local rules are the fallback.

    The model writes the settings as JSON directly, and that reply is still
    checked against the same closed control surface before anything can reach
    the simulation - an id this junction doesn't have, a value that isn't a
    number, an action outside the schema, all get dropped here. With no key,
    no network, or a reply the surface won't take, the rule parser answers
    instead, so the interface still works with the cable out.
    """
    carried, model_reason = [], None

    if allow_llm and llm_available():
        try:
            answer = _validate(_call_llm(utterance, state))
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            carried.append("The hosted model did not answer ("
                           + _error_detail(exc)
                           + "), so the local rules read this instead.")
        else:
            if answer["ok"]:
                answer["utterance"] = utterance
                answer.setdefault("notes", []).append(
                    "Read by " + MODEL + ", then checked against the control "
                    "surface before anything moved.")
                return answer
            model_reason = answer.get("reason")
            carried.extend(answer.get("notes") or [])

    instruction = nlu.parse(utterance)
    if instruction.ok:
        notes = list(instruction.notes) + carried
        missed = unread_families(utterance, instruction.action)
        if missed:
            notes.append("This sentence also mentions " + " and ".join(missed)
                         + ", and the parser reads one request at a time. Only "
                           "the change shown was taken - ask for the rest "
                           "separately.")
        if model_reason:
            notes.append("The model declined this (" + model_reason
                         + ") but the local rules place it, so theirs is the "
                           "reading shown.")
        return {"ok": True, "source": instruction.source,
                "action": instruction.action, "params": instruction.params,
                "summary": instruction.summary, "notes": notes,
                "corrections": list(instruction.corrections),
                "edits": edits_for(instruction, state)}

    # When the model is the one refusing, its reason stands on its own - the
    # rule parser's "here is the whole vocabulary" dump belongs to a rules
    # refusal and only adds noise under someone else's.
    return {"ok": False, "edits": [], "utterance": utterance,
            "source": "llm" if model_reason else instruction.source,
            "reason": model_reason or instruction.reason,
            "notes": carried if model_reason
                     else list(instruction.notes) + carried}


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
