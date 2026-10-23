/**
 * Timed-set hold: a fixed prep countdown, then a stopwatch that fills seconds.
 *
 * Prep is always 10 seconds. The rest-voice clip (5, 4, 3, 2, 1) is scheduled
 * to finish as prep ends; the hold then counts up until End.
 */

/** Seconds of ready countdown before the hold starts. */
export const PREP_S = 10;

/** What the screen stores: idle, or one run keyed by set id and prep end. */
export type HoldSession = { kind: "idle" } | { kind: "run"; setId: string; prepEndsAt: number };

/** Derived phase used for display and End. */
export type HoldPhase =
  | { kind: "idle" }
  | { kind: "prep"; setId: string; endsAt: number }
  | { kind: "hold"; setId: string; startedAt: number };

export const IDLE_HOLD: HoldSession = { kind: "idle" };

/** Begin prep for `setId` at `now` (epoch ms). */
export function startHold(setId: string, now: number): HoldSession {
  return { kind: "run", setId, prepEndsAt: now + PREP_S * 1000 };
}

/** Phase of `session` at `now`: prep until prepEndsAt, then hold. */
export function holdPhase(session: HoldSession, now: number): HoldPhase {
  if (session.kind === "idle") {
    return { kind: "idle" };
  }
  if (now < session.prepEndsAt) {
    return { kind: "prep", setId: session.setId, endsAt: session.prepEndsAt };
  }
  return { kind: "hold", setId: session.setId, startedAt: session.prepEndsAt };
}

/** Elapsed whole seconds for a running hold, or null when not holding. */
function holdSeconds(phase: HoldPhase, now: number): number | null {
  if (phase.kind !== "hold") {
    return null;
  }
  return Math.max(0, Math.round((now - phase.startedAt) / 1000));
}

/**
 * End a running hold: idle session and the seconds string to put in the set.
 * The field stays editable afterward so a late tap can be corrected.
 */
export function endHold(
  phase: HoldPhase,
  now: number,
): { session: HoldSession; seconds: string } | null {
  const seconds = holdSeconds(phase, now);
  return seconds === null ? null : { session: IDLE_HOLD, seconds: String(seconds) };
}

/** On-screen countdown or elapsed seconds while a timer is active. */
export function holdDisplay(phase: HoldPhase, now: number): string | null {
  if (phase.kind === "prep") {
    return String(Math.max(1, Math.ceil((phase.endsAt - now) / 1000)));
  }
  if (phase.kind === "hold") {
    return String(Math.max(0, Math.floor((now - phase.startedAt) / 1000)));
  }
  return null;
}

export function holdFor(phase: HoldPhase, setId: string): boolean {
  return phase.kind !== "idle" && phase.setId === setId;
}
