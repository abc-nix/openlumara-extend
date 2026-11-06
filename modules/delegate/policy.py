"""delegate — pure policy for starting and detaching runs.

Kept out of `module.py` on purpose: that file imports `channels.webui`, so the
plain-unittest suite cannot load it. Everything here is a pure function over
numbers and names, which means the two decisions that actually matter — may a
start go to the background, and may a run detach at the tool deadline — are
covered by tests/test_policy.py.

The rule behind `start_refusal`: core's `tc_manager.process()` re-requests every
tool round with no `model` argument (`core/toolcalls.py:317`), so a run only
keeps its model because the runner points `core.config` at it for the run's
whole life. While that pointer is set, anything else that asks the API runs on
the delegate's model. A background run therefore may only run on the model the
chat is already on.
"""


def running_count(running):
    """how many background runs are live (accepts a dict, set or a number)"""
    try:
        return max(0, len(running))
    except TypeError:
        try:
            return max(0, int(running or 0))
        except (TypeError, ValueError):
            return 0


def slots_left(cap, running):
    """free background slots, never negative (a bad cap still means one slot)"""
    try:
        cap = max(1, int(cap))
    except (TypeError, ValueError):
        cap = 1
    return max(0, cap - running_count(running))


def same_model(a, b):
    """model names compare case- and whitespace-insensitively (server spelling)"""
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _full_message(cap, running):
    count = running_count(running)
    return (
        f"all {cap} background delegate slot{'s are' if cap != 1 else ' is'} in use "
        f"({count} running): run this task in the foreground instead (omit "
        f"background=True) or wait until a slot frees up"
    )


def start_refusal(background, cap, running, effective_model, current_model):
    """the error message that refuses a start, or None when it may start.

    `background=False` is always allowed: it blocks the calling turn, so nothing
    is running once it returns. `background=True` needs a free slot, and the run
    must use the chat's current model (see the module docstring).
    """
    if not background:
        return None
    if slots_left(cap, running) <= 0:
        return _full_message(max(1, int(cap or 1)), running)
    if not same_model(effective_model, current_model):
        return (
            f"a background delegate must use the chat's current model "
            f"('{current_model}'): it keeps core's model pointer on its own model "
            f"while it runs, so a different one would leak into the chat's requests. "
            f"Start it in the foreground to run on '{effective_model}'."
        )
    return None


# how a completion notice reaches the channel (see announce_completion)
TAIL = "tail"            # push(defer=False): appended and displayed now
DEFERRED = "deferred"    # push(defer=True): appended after the turn ends
STEERING = "steering"    # queue_steering_message: injected into the running turn
START_TURN = "start_turn"  # start a turn with the notice (what a typed message does)


def announce_delivery(mode, turn_running, delegate_runs):
    """where a completion notice goes: TAIL / DEFERRED / STEERING / START_TURN.

    `mode` is "push" or "wake" (the caller handles "off").
    - push: never starts a turn. Tail when idle, deferred otherwise.
    - wake: the notice rides in a USER message and the assistant acts on it at
      once.
      - idle: a new turn is started with it (the webui's own start_stream path),
        so the transcript gains a user bubble plus the reply;
      - a normal turn is running and no delegated chain is live: it goes through
        the steering slot, which that chain drains at its next tool-round
        boundary (toolcalls.py:294), or answers as a continuation turn if it ends
        first (channel.py:799 / :882);
      - a delegated chain is live: it would steal the slot (core drains
        queued_message with no `context is None` guard), so fall back to the
        deferred tail.
    """
    woken = str(mode or "").strip().lower() == "wake"
    if woken:
        if not turn_running:
            return START_TURN
        return STEERING if not delegate_runs else DEFERRED
    return DEFERRED if turn_running else TAIL


def should_detach(quiet_seconds, slots):
    """detach a run at the caller's deadline: only with a slot, only if tool-less.

    `quiet_seconds` is None exactly when the run has made no tool call at all.
    A run that has used a tool keeps blocking for as long as it needs - the call
    is the caller's foreground work and must not be turned into a background run
    behind their back (2026-11-06 rule). Only a run that never called a tool is
    detached, so its call can return `running` instead of waiting out the whole
    generation. Never above the cap: with no slot free the run also keeps
    blocking, and re-checks every second so it can take the slot the moment the
    current background run ends.
    """
    return slots > 0 and quiet_seconds is None
