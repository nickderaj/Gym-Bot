import { describe, expect, it } from "vitest";

import {
  IDLE_HOLD,
  PREP_S,
  endHold,
  holdDisplay,
  holdFor,
  holdPhase,
  startHold,
} from "./holdTimer";

const NOW = 1_000_000;

describe("holdTimer", () => {
  it("runs a fixed prep then a hold that ends into editable seconds", () => {
    const session = startHold("s1", NOW);
    expect(session).toEqual({ kind: "run", setId: "s1", prepEndsAt: NOW + PREP_S * 1000 });

    const prep = holdPhase(session, NOW);
    expect(prep).toEqual({ kind: "prep", setId: "s1", endsAt: NOW + PREP_S * 1000 });
    expect(holdDisplay(prep, NOW)).toBe("10");
    expect(holdDisplay(prep, NOW + 9_100)).toBe("1");
    expect(holdFor(prep, "s1")).toBe(true);
    expect(holdFor(prep, "other")).toBe(false);

    expect(holdPhase(session, NOW + PREP_S * 1000 - 1).kind).toBe("prep");

    const hold = holdPhase(session, NOW + PREP_S * 1000);
    expect(hold).toEqual({ kind: "hold", setId: "s1", startedAt: NOW + PREP_S * 1000 });
    expect(holdDisplay(hold, NOW + PREP_S * 1000)).toBe("0");
    expect(holdDisplay(hold, NOW + PREP_S * 1000 + 45_400)).toBe("45");
    expect(holdDisplay({ kind: "idle" }, NOW)).toBeNull();

    expect(endHold(prep, NOW)).toBeNull();
    expect(endHold(holdPhase(IDLE_HOLD, NOW), NOW)).toBeNull();

    const finished = endHold(hold, NOW + PREP_S * 1000 + 47_200);
    expect(finished).toEqual({ session: IDLE_HOLD, seconds: "47" });
  });
});
