import type { ReactElement } from "react";

import type { HoldTimer } from "./useHoldTimer";

function HoldButtons({
  setId,
  label,
  phase,
  busy,
  onStart,
  onEnd,
  onCancel,
}: {
  setId: string;
  label: string;
  phase: "prep" | "hold" | null;
  busy: boolean;
  onStart: (setId: string) => void;
  onEnd: () => void;
  onCancel: () => void;
}): ReactElement {
  return (
    <span className="hold-controls">
      {phase === null ? (
        <button
          type="button"
          className="chip"
          disabled={busy}
          aria-label={`${label} start timer`}
          onClick={() => {
            onStart(setId);
          }}
        >
          Start
        </button>
      ) : null}
      {phase === "hold" ? (
        <button type="button" className="chip" aria-label={`${label} end timer`} onClick={onEnd}>
          End
        </button>
      ) : null}
      {phase !== null ? (
        <button
          type="button"
          className="chip"
          aria-label={`${label} cancel timer`}
          onClick={onCancel}
        >
          Cancel
        </button>
      ) : null}
    </span>
  );
}

/**
 * Seconds field for a timed set, with Start → 10 s ready → hold → End autofill.
 * After End the input stays editable so a late tap can be trimmed.
 */
export function HoldSecs({
  setId,
  seconds,
  label,
  logged,
  hold,
  onSeconds,
  onSettle,
}: {
  setId: string;
  seconds: string;
  label: string;
  logged: boolean;
  hold: HoldTimer;
  onSeconds: (value: string) => void;
  onSettle: () => void;
}): ReactElement {
  const phase = hold.phase(setId);
  const readout = hold.display(setId);
  return (
    <div className="hold-secs">
      {phase === null ? (
        <input
          aria-label={label}
          inputMode="decimal"
          value={seconds}
          onChange={(event) => {
            onSeconds(event.target.value);
          }}
          onBlur={onSettle}
        />
      ) : (
        <span className="hold-clock" role="timer" aria-label={phase === "prep" ? "Ready" : "Hold"}>
          {phase === "prep" ? `in ${readout ?? ""}` : `${readout ?? "0"} s`}
        </span>
      )}
      {logged ? null : (
        <HoldButtons
          setId={setId}
          label={label}
          phase={phase}
          busy={hold.busy}
          onStart={hold.start}
          onEnd={() => {
            const filled = hold.end();
            if (filled !== null) {
              onSeconds(filled);
            }
          }}
          onCancel={hold.cancel}
        />
      )}
    </div>
  );
}
