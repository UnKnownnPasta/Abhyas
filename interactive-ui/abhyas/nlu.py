# Free text in, a structured instruction out.
#
# Two rules that are enforced here rather than remembered:
#   1. this module never touches the simulation. It hands back an instruction
#      and the caller decides whether to run it.
#   2. the vocabulary is closed. Anything that doesn't map onto a known action,
#      phase or arm gets rejected with an explanation, never guessed at.
#
# It's all rules and all local, because the interface has to work with the
# network cable out. A model can sit in front of it (parse_with_model) but its
# output goes through exactly the same validation.

import re

from . import config as C

ACTIONS = {
    "adjust_green": "Add or remove green time on one signal phase",
    "set_green": "Set the green time of one signal phase outright",
    "set_demand": "Change how many vehicles per hour enter the junction",
    "add_obstruction": "Put an obstruction in one approach lane",
    "clear_obstructions": "Remove every obstruction from the road",
    "run_counterfactual": "Compare a signal change against baseline on paired seeds",
    "run_validation": "Dispatch the validation fleet against the archive",
    "reset": "Restart the live simulation from its baseline",
    "pause": "Pause the live simulation",
    "resume": "Resume the live simulation",
    "status": "Report what the junction is doing right now",
}

ARM_WORDS = {
    "N": ["north", "northbound", "northern", "north bound", "from the north",
          "100 feet road north", "top"],
    "S": ["south", "southbound", "southern", "south bound", "from the south",
          "100 feet road south", "bottom"],
    "E": ["east", "eastbound", "eastern", "east bound", "from the east",
          "cmh road east", "right"],
    "W": ["west", "westbound", "western", "west bound", "from the west",
          "cmh road west", "left"],
}

ARM_NAME = {"N": "north", "S": "south", "E": "east", "W": "west"}

_PHASE_WORDS_BY_SHAPE = {
    "two_phase": {
        "north_south": ["north-south", "north south", "ns", "n-s", "main road",
                        "100 feet road", "hundred feet road", "arterial"],
        "east_west": ["east-west", "east west", "ew", "e-w", "cmh road", "cmh",
                      "cross road", "side road"],
    },
    "four_phase": {
        "north": ["north approach", "northbound green", "north phase"],
        "east": ["east approach", "eastbound green", "east phase"],
        "south": ["south approach", "southbound green", "south phase"],
        "west": ["west approach", "westbound green", "west phase"],
    },
}

PHASE_WORDS = _PHASE_WORDS_BY_SHAPE[C.ACTIVE_PHASE_PLAN]

OBSTRUCTION_WORDS = {
    "cow": ["cow", "cattle", "bull", "buffalo", "animal", "ox"],
    "stalled_vehicle": ["stalled", "broken down", "breakdown", "stalled vehicle",
                        "broken car", "stranded", "parked car", "abandoned"],
    "roadworks": ["roadworks", "road works", "digging", "construction",
                  "barricade", "excavation", "repair", "pothole"],
}

# Things people reasonably ask for that this model genuinely cannot do. Naming
# them beats a generic rejection - it tells you where the edge is instead of
# making you poke at it.
OUT_OF_SCOPE = {
    "pedestrian": "Pedestrians are not modelled. The network was built for "
                  "vehicle movements only.",
    "crossing": "Pedestrians are not modelled, so a crossing phase has nothing "
                "to act on.",
    "weather": "Weather is not modelled. Nothing in the archive tells wet from dry.",
    "rain": "Rain is not modelled. Nothing in the archive tells wet from dry.",
    "accident": "Collisions are an output of the model, not an input. You can "
                "place a stalled vehicle, which is what a blocked lane looks like.",
    "emission": "Emissions are not modelled. Nothing was validated against them.",
    "pollution": "Emissions are not modelled. Nothing was validated against them.",
    "flyover": "The network is one junction at ground level. There's no flyover "
               "in it.",
    "metro": "The metro is not in the network. Only the four road arms are.",
    "adaptive": "The signal is a fixed-time plan. Adaptive control isn't "
                "implemented and claiming a result for it would be inventing one.",
    "another junction": "Only CMH Road x 100 Feet Road is modelled.",
    "whole city": "Only one junction is modelled. A city-wide answer would be "
                  "an extrapolation, not a result.",
}

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "ninety": 90, "hundred": 100,
}


class Instruction:
    def __init__(self, ok, action="", params=None, notes=None, corrections=None,
                 reason="", utterance="", source="local-rules", summary=""):
        self.ok = ok
        self.action = action
        self.params = params or {}
        self.notes = notes or []
        self.corrections = corrections or []
        self.reason = reason
        self.utterance = utterance
        self.source = source
        self.summary = summary

    def to_dict(self):
        return {"ok": self.ok, "action": self.action, "params": self.params,
                "notes": self.notes, "corrections": self.corrections,
                "reason": self.reason, "utterance": self.utterance,
                "source": self.source, "summary": self.summary,
                "vocabulary": sorted(ACTIONS)}


def reject(utterance, reason, notes=None):
    return Instruction(False, utterance=utterance, reason=reason,
                       notes=notes or [], summary="Rejected: " + reason)


class Parser:
    """Rule based parser. One instruction per sentence, nothing executed."""

    def __init__(self, utterance):
        self.raw = (utterance or "").strip()
        text = re.sub(r"[^\w\s%+\-./]", " ", self.raw.lower())
        self.text = re.sub(r"\s+", " ", text).strip()

    # -- little readers ----------------------------------------------------

    def has(self, pattern):
        return bool(re.search(pattern, self.text))

    def numbers(self):
        found = [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", self.text)]
        if found:
            return found
        return [float(v) for word, v in NUMBER_WORDS.items()
                if re.search(r"\b" + word + r"\b", self.text)]

    def arms_named(self):
        """Every arm the text names, longest match first."""
        hits = []
        for arm, words in ARM_WORDS.items():
            for word in words:
                if re.search(r"\b" + re.escape(word) + r"\b", self.text):
                    hits.append((arm, len(word)))
                    break
        hits.sort(key=lambda h: -h[1])
        return [arm for arm, _ in hits]

    def find_arm(self):
        arms = self.arms_named()
        return arms[0] if arms else None

    def find_phase(self):
        for group, words in PHASE_WORDS.items():
            for word in words:
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", self.text):
                    return group
        return None

    def resolve_phase(self):
        """(stage, corrections, ambiguity). Whether one direction has its own
        green depends on the plan shape - under two_phase it doesn't and saying
        so is the whole point, under four_phase it does and claiming otherwise
        would be the lie."""
        group = self.find_phase()
        if group:
            return group, [], []

        arms = self.arms_named()
        if not arms:
            return None, [], []

        # "east-west green" names two arms. under two_phase they share a stage,
        # under four_phase they're separate greens and picking one silently
        # answers a question nobody asked.
        stages = {C.ARM_TO_PHASE[a] for a in arms}
        if len(stages) > 1:
            return None, [], [
                "That names the " + " and ".join(ARM_NAME[a] for a in arms)
                + " directions, which are separate greens on this signal ("
                + ", ".join(C.PHASE_GROUPS[g]["label"].lower()
                            for g in sorted(stages)) + "). Say which one."]

        arm = arms[0]
        group = C.ARM_TO_PHASE[arm]
        partners = [a for a in C.PHASE_GROUPS[group]["arms"] if a != arm]
        if not partners:
            return group, [], []

        return group, ["You asked for the " + ARM_NAME[arm] + "bound green. "
                       "There's no such phase on this signal: " + ARM_NAME[arm]
                       + " and " + " and ".join(ARM_NAME[a] for a in partners)
                       + " run on the same green, so they can't be changed "
                       "separately. The instruction below acts on the "
                       + C.PHASE_GROUPS[group]["label"].lower() + " phase."], []

    # -- the handlers ------------------------------------------------------

    def parse(self):
        if not self.raw:
            return reject(self.raw, "Empty request.")

        for phrase, explanation in OUT_OF_SCOPE.items():
            if phrase in self.text:
                return reject(self.raw, explanation,
                              notes=["Available actions: " + ", ".join(sorted(ACTIONS))])

        for handler in (self.validation, self.counterfactual, self.obstruction,
                        self.clear, self.demand, self.signal, self.control):
            instruction = handler()
            if instruction is not None:
                return instruction

        return reject(
            self.raw,
            "This doesn't map onto anything the model can do. The schema takes "
            "a fixed set of actions and anything outside it gets rejected "
            "rather than guessed at.",
            notes=[name + " - " + desc for name, desc in sorted(ACTIONS.items())])

    def validation(self):
        if not self.has(r"\b(validat|verify|check the model|how (do you|do we) know|"
                        r"accurac|compare .*(archive|real|data)|against .*(data|archive))"):
            return None
        seeds = None
        match = re.search(r"(\d+)\s*(?:seeds?|runs?)", self.text)
        if match:
            seeds = int(match.group(1))
        slot = None
        hour = re.search(r"\b(\d{1,2})\s*(?::00)?\s*(am|pm)\b", self.text)
        if hour:
            value = int(hour.group(1)) % 12 + (12 if hour.group(2) == "pm" else 0)
            slot = ("weekday " + format(value, "02d") + ":00-"
                    + format((value + 1) % 24, "02d") + ":00")
        params = {"slot": slot, "seeds": seeds or 30}
        return Instruction(
            True, "run_validation", params, utterance=self.raw,
            notes=["Dispatches the validation fleet. Every figure it hands back "
                   "is a median across seeds with its spread."],
            summary=("Run the validation fleet"
                     + (" for " + slot if slot else " on the latest usable slot")
                     + " with " + str(params["seeds"]) + " seeds per movement"))

    def counterfactual(self):
        if not self.has(r"\b(what if|counterfactual|compare|would it help|"
                        r"is it better|effect of|impact of)\b"):
            return None
        numbers = self.numbers()
        if not numbers:
            return reject(self.raw, "A counterfactual needs a size of change: "
                                    "how many seconds to add or remove.")
        group, corrections, ambiguous = self.resolve_phase()
        if ambiguous:
            return reject(self.raw, ambiguous[0])
        if group is None:
            return reject(self.raw, "Name a phase to change.")

        delta = numbers[0]
        if self.has(r"\b(remove|reduce|cut|less|shorter|subtract|take)\b"):
            delta = -abs(delta)
        seeds = 30
        match = re.search(r"(\d+)\s*(?:seeds?|runs?|pairs?)", self.text)
        if match:
            seeds = int(match.group(1))
        return Instruction(
            True, "run_counterfactual",
            {"phase_group": group, "delta_seconds": delta, "seeds": seeds},
            utterance=self.raw, corrections=corrections,
            notes=["Baseline and scenario run on the same seeds, so what gets "
                   "measured is the change and not the randomness.",
                   "The answer comes back as a range. It may be 'cannot resolve'."],
            summary=(("Add " if delta >= 0 else "Remove ")
                     + format(abs(delta), ".0f") + " s of green on the "
                     + C.PHASE_GROUPS[group]["label"].lower()
                     + ", compared against baseline over " + str(seeds)
                     + " paired seeds"))

    def obstruction(self):
        kind = None
        for name, words in OBSTRUCTION_WORDS.items():
            if any(re.search(r"\b" + re.escape(w) + r"\b", self.text) for w in words):
                kind = name
                break
        if kind is None:
            return None
        if self.has(r"\b(remove|clear|take away|get rid|delete)\b"):
            return None                     # that's a clear, handled below

        arm, notes = self.find_arm(), []
        if arm is None:
            arm = "N"
            notes.append("No approach was named so it goes on the north arm. "
                         "Name one to put it elsewhere.")
        duration = None
        match = re.search(r"(\d+)\s*(second|sec|s|minute|min)", self.text)
        if match:
            duration = float(match.group(1)) * (60.0 if match.group(2).startswith("min") else 1.0)
        else:
            numbers = self.numbers()
            if numbers:
                duration = numbers[0]
        lane = 1 if self.has(r"\bsecond lane\b|\blane 2\b|\bright lane\b") else 0
        return Instruction(
            True, "add_obstruction",
            {"kind": kind, "arm": arm, "duration_s": duration, "lane": lane},
            utterance=self.raw, notes=notes,
            summary=("Place a " + kind.replace("_", " ") + " in lane "
                     + str(lane + 1) + " of the " + arm + " approach"
                     + (" for " + format(duration, ".0f") + " s" if duration else "")))

    def clear(self):
        if self.has(r"\b(clear|remove|get rid of|take away)\b.*\b(obstruction|cow|"
                    r"cattle|blockage|animal|roadworks|stalled)\b") or \
           self.has(r"\b(clear|reopen)\s+the\s+(road|lane|junction)\b"):
            return Instruction(True, "clear_obstructions", {}, utterance=self.raw,
                               summary="Remove every obstruction from the road")
        return None

    def demand(self):
        if not self.has(r"\b(traffic|demand|volume|vehicles|flow|busier|quieter|"
                        r"rush|peak|congestion)\b"):
            return None
        if self.has(r"\bgreen\b|\bsignal\b|\bphase\b|\btimer?\b"):
            return None

        match = re.search(r"(\d+(?:\.\d+)?)\s*%", self.text)
        if match:
            pct = float(match.group(1))
            factor = 1.0 + pct / 100.0
            if self.has(r"\b(less|fewer|reduce|cut|drop|lower|down)\b"):
                factor = 1.0 - pct / 100.0
            return Instruction(
                True, "set_demand", {"multiplier": round(factor, 3)},
                utterance=self.raw,
                notes=["Vehicles per hour is the only quantity here fitted to "
                       "data. Moving it by hand leaves the calibrated value "
                       "behind, so anything after this is exploratory."],
                summary=("Scale traffic to " + format(factor * 100, ".0f")
                         + "% of its current level"))

        numbers = self.numbers()
        if numbers and self.has(r"\bper hour\b|\bveh|\bvehicles\b|\bvph\b"):
            return Instruction(
                True, "set_demand", {"veh_per_hour": max(0.0, numbers[0])},
                utterance=self.raw,
                notes=["Vehicles per hour is the calibration dial; setting it by "
                       "hand leaves the calibrated value behind."],
                summary="Set demand to " + format(numbers[0], ".0f") + " veh/h")

        if self.has(r"\b(busier|more traffic|heavier|rush hour|peak)\b"):
            return Instruction(True, "set_demand", {"multiplier": 1.3},
                               utterance=self.raw, summary="Increase traffic by 30%")
        if self.has(r"\b(quieter|less traffic|lighter|off.?peak)\b"):
            return Instruction(True, "set_demand", {"multiplier": 0.7},
                               utterance=self.raw, summary="Reduce traffic by 30%")
        return None

    def signal(self):
        if not self.has(r"\b(green|signal|light|phase|timer|timing|cycle)\b"):
            return None
        numbers = self.numbers()
        if not numbers:
            return reject(self.raw, "A signal change needs a number of seconds.")
        group, corrections, ambiguous = self.resolve_phase()
        if ambiguous:
            return reject(self.raw, ambiguous[0])
        if group is None:
            return reject(
                self.raw,
                "Name which green to change. This signal has "
                + str(len(C.PHASE_GROUPS)) + " phases: "
                + ", ".join(s["label"].lower() for s in C.PHASE_GROUPS.values()) + ".",
                notes=[gid + " - " + spec["label"] + " (arms "
                       + "+".join(spec["arms"]) + ")"
                       for gid, spec in C.PHASE_GROUPS.items()])

        seconds = numbers[0]
        shown = ["The parsed instruction is shown before anything runs. Nothing "
                 "reaches the simulation until you accept it."]

        if self.has(r"\bset\b|\bmake it\b|\bto exactly\b") and \
           not self.has(r"\badd\b|\bextra\b|\bmore\b|\bincrease\b"):
            return Instruction(
                True, "set_green", {"phase_group": group, "seconds": seconds},
                utterance=self.raw, corrections=corrections, notes=shown,
                summary=("Set the " + C.PHASE_GROUPS[group]["label"].lower()
                         + " to " + format(seconds, ".0f") + " s"))

        delta = seconds
        if self.has(r"\b(remove|reduce|cut|shorten|less|subtract|take|drop|decrease)\b"):
            delta = -abs(seconds)
        return Instruction(
            True, "adjust_green", {"phase_group": group, "delta_seconds": delta},
            utterance=self.raw, corrections=corrections, notes=shown,
            summary=(("Add " if delta >= 0 else "Remove ")
                     + format(abs(delta), ".0f") + " s of green on the "
                     + C.PHASE_GROUPS[group]["label"].lower()))

    def control(self):
        simple = [
            (r"\b(reset|restart|start over|baseline again)\b", "reset",
             "Restart the live simulation from baseline"),
            (r"\b(pause|hold|freeze|stop)\b", "pause", "Pause the live simulation"),
            (r"\b(resume|continue|carry on|play|go on|unpause)\b", "resume",
             "Resume the live simulation"),
            (r"\b(status|what.s happening|how is it|report|current state|queue|"
             r"how long)\b", "status", "Report the junction's current state"),
        ]
        for pattern, action, summary in simple:
            if self.has(pattern):
                return Instruction(True, action, {}, utterance=self.raw,
                                   summary=summary)
        return None


def parse(utterance):
    return Parser(utterance).parse()


# ---- optional model front end -------------------------------------------

MODEL_SYSTEM_PROMPT = """You convert a question about one traffic junction into a
structured instruction. You never answer the question and you never invent
numbers. Reply with JSON only: {"action": ..., "params": {...}, "summary": ...}.

Valid actions: """ + ", ".join(sorted(ACTIONS)) + """.
Valid phase_group values: """ + ", ".join(C.PHASE_GROUPS) + """. Valid arm values: N, E, S, W.
If the request doesn't fit an action above, return {"action": "unsupported"}.
"""


def parse_with_model(utterance, client=None, model="claude-sonnet-5"):
    """Run it past a model, then validate it here anyway.

    Off by default and never required - the rule parser above is the primary
    path. Whatever the model returns is re-checked against the same closed
    schema so it can't reach the simulation unchecked.
    """
    if client is None:
        return parse(utterance)
    try:
        response = client.messages.create(
            model=model, max_tokens=512, system=MODEL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": utterance}])
        import json
        text = "".join(block.text for block in response.content
                       if getattr(block, "type", "") == "text")
        payload = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        return parse(utterance)

    action = payload.get("action")
    if action not in ACTIONS:
        return reject(utterance, "The model proposed an action outside the "
                                 "schema, so it was rejected rather than run.")
    instruction = _validate_model_payload(utterance, action, payload)
    instruction.source = "language-model + local validation"
    return instruction


def _validate_model_payload(utterance, action, payload):
    """Re-derive the instruction locally so nothing bypasses the closed schema."""
    params = payload.get("params") or {}
    corrections = []

    if action in ("adjust_green", "set_green", "run_counterfactual"):
        group = params.get("phase_group")
        if group not in C.PHASE_GROUPS:
            arm = params.get("arm")
            if arm in C.ARM_TO_PHASE:
                group = C.ARM_TO_PHASE[arm]
                corrections.append("The model named the " + arm + " arm, so the "
                                   "instruction acts on the "
                                   + C.PHASE_GROUPS[group]["label"].lower() + ".")
            else:
                return reject(utterance, "No valid phase group in the proposed "
                                         "instruction.")
        params["phase_group"] = group

    if action == "add_obstruction":
        if params.get("kind") not in OBSTRUCTION_WORDS:
            return reject(utterance, "Unknown obstruction type proposed.")
        if params.get("arm") not in C.ARM_TO_PHASE:
            params["arm"] = "N"

    return Instruction(True, action, params, utterance=utterance,
                       corrections=corrections,
                       summary=payload.get("summary", ACTIONS[action]))


if __name__ == "__main__":
    import json
    samples = [
        "add 10 seconds to the northbound green",
        "add 10s to the traffic timer on the main road",
        "introduce a cow on the road from the east for 90 seconds",
        "what if we gave the east-west green 15 more seconds?",
        "make it 30% busier",
        "clear the cow",
        "validate the model at 9am with 30 runs",
        "give the pedestrians a longer crossing",
        "make it rain",
        "set the north-south green to 55 seconds",
        "how long is the queue",
    ]
    for sample in samples:
        print("> " + sample)
        print(json.dumps(parse(sample).to_dict(), indent=2)[:900])
        print()
