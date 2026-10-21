"""FIFO release (DESIGN §4.5): over the limit, the queue head leaves no sooner than gap(ratio)
after the previous departure. The gap counts from the departure, whatever the request's size."""


def step_for(steps, ratio):
    """The last step whose `at` the ratio has reached; None under the first step."""
    chosen = None
    for step in steps:
        if ratio >= step[0]:
            chosen = step
    return chosen


def gap(steps, ratio):
    step = step_for(steps, ratio)
    return step[1] if step else 0.0
