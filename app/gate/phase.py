"""Off/on steps (DESIGN §4.5), kept only to compare against the bypass throttle it replaces:
over the limit the pool alternates "off N s" and "on M s"; a request arriving in an off phase
waits for the phase to end."""
from .fifo import step_for


class Phase:
    def __init__(self, steps):
        self.steps = steps
        self.phase, self.until, self.step = "open", 0.0, None

    def tick(self, now, ratio):
        if ratio < 1:
            self.phase, self.until, self.step = "open", 0.0, None
        elif self.phase == "off" and now < self.until:
            pass
        elif self.phase == "off":
            self.phase, self.until = "on", now + self.step[2]
        elif self.phase == "on" and now < self.until:
            pass
        else:
            self.step = step_for(self.steps, ratio) or self.steps[0]
            self.phase, self.until = "off", now + self.step[1]

    def passing(self):
        return self.phase != "off"
