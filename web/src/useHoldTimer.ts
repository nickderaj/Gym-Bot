import { useState } from "react";

import {
  IDLE_HOLD,
  PREP_S,
  endHold,
  holdDisplay,
  holdFor,
  holdPhase,
  startHold,
} from "./holdTimer";
import type { HoldSession } from "./holdTimer";
import { useNow } from "./log/useNow";
import { startRestAudio, stopRestAudio } from "./restAudio";

export interface HoldTimer {
  /** True when this set owns the active prep or hold. */
  active: (setId: string) => boolean;
  /** Ready or hold readout for `setId`, or null when idle for that set. */
  display: (setId: string) => string | null;
  /** Phase label for the live readout. */
  phase: (setId: string) => "prep" | "hold" | null;
  /** Whether any set's timer is running (blocks starting another). */
  busy: boolean;
  start: (setId: string) => void;
  /** Seconds to write into the set, or null if not in a hold. */
  end: () => string | null;
  cancel: () => void;
}

/** One shared prep/hold timer for the logging screen. */
export function useHoldTimer(): HoldTimer {
  const [session, setSession] = useState<HoldSession>(IDLE_HOLD);
  const now = useNow(200);
  const phase = holdPhase(session, now);

  return {
    busy: phase.kind !== "idle",
    active: (setId: string): boolean => holdFor(phase, setId),
    display: (setId: string): string | null =>
      holdFor(phase, setId) ? holdDisplay(phase, now) : null,
    phase: (setId: string): "prep" | "hold" | null => {
      if (!holdFor(phase, setId)) {
        return null;
      }
      return phase.kind === "prep" ? "prep" : "hold";
    },
    start: (setId: string): void => {
      startRestAudio(PREP_S * 1000);
      setSession(startHold(setId, Date.now()));
    },
    end: (): string | null => {
      const finished = endHold(phase, Date.now());
      if (finished === null) {
        return null;
      }
      stopRestAudio();
      setSession(finished.session);
      return finished.seconds;
    },
    cancel: (): void => {
      stopRestAudio();
      setSession(IDLE_HOLD);
    },
  };
}
