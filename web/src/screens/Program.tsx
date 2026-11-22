import { useRef, useState } from "react";
import type { ReactElement } from "react";

import {
  askCoach,
  changeProgram,
  programsSchema,
  restartProgram,
  useApi,
  useReplyClaim,
  versionsSchema,
} from "../api";
import type {
  CoachMessage,
  Position,
  Program as ProgramData,
  ProgramDay,
  ProgramExercise,
} from "../api";
import { Load, tone } from "../components";
import { formatDay, plural } from "../format";
import { href } from "../router";
import { ProgramEditor } from "./ProgramEditor";
import { blockLetter } from "./programEdit";

/**
 * "3 × 8–12", or "2 × 30–45 s" for a timed exercise; one number when the range is one.
 *
 * @internal Exported for tests.
 */
export function formatPrescription(exercise: {
  sets: number;
  rep_min: number;
  rep_max: number;
  measure: string;
}): string {
  const range =
    exercise.rep_min === exercise.rep_max
      ? String(exercise.rep_min)
      : `${String(exercise.rep_min)}–${String(exercise.rep_max)}`;
  const unit = exercise.measure === "seconds" ? " s" : "";
  return `${String(exercise.sets)} × ${range}${unit}`;
}

/** What an exercise shows beside its prescription: the load it starts at, or reps only. */
function startNote(exercise: ProgramExercise, starting: boolean): string {
  if (exercise.progression === "reps") {
    return " · reps only";
  }
  return starting && exercise.start_load_kg !== null
    ? ` · ${String(exercise.start_load_kg)} kg`
    : "";
}

function Day({
  day,
  label,
  number,
  next,
  starting,
}: {
  day: ProgramDay;
  label: string;
  number: number;
  next: boolean;
  starting: boolean;
}): ReactElement {
  return (
    <li className="card program-day" aria-current={next ? "step" : undefined}>
      <h3>
        <span className="muted">
          {label} {number}
        </span>{" "}
        {/* The next day opens Today, where it is started; the link covers the card. */}
        {next ? (
          <a className="day-link" href={href({ name: "today" })}>
            {day.name}
            <span className="muted" aria-hidden="true">
              {" ›"}
            </span>
          </a>
        ) : (
          day.name
        )}
        {next ? (
          <span className="pill tint" style={tone("green")}>
            Next
          </span>
        ) : null}
      </h3>
      <ol className="blocks">
        {day.blocks.map((block, index) => {
          const letter = blockLetter(index);
          const superset = block.exercises.length > 1;
          return (
            <li key={letter} className={superset ? "block superset" : "block"}>
              {superset ? <p className="muted block-kind">Superset</p> : null}
              <ul className="lines">
                {block.exercises.map((exercise, position) => (
                  <li key={exercise.id}>
                    <span>
                      <span className="block-letter">
                        {superset ? `${letter}${String(position + 1)}` : letter}
                      </span>{" "}
                      {exercise.name}
                      {exercise.variations === null ? null : (
                        <span className="muted variations"> {exercise.variations}</span>
                      )}
                    </span>
                    <span className="best">
                      {formatPrescription(exercise)}
                      {startNote(exercise, starting)}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="muted block-rest">Rest {block.rest_s} s</p>
            </li>
          );
        })}
      </ol>
    </li>
  );
}

function Days({
  days,
  label = "Day",
  next,
  starting,
}: {
  days: ProgramDay[];
  label?: string;
  next: Position | null;
  starting: boolean;
}): ReactElement {
  return (
    <ol className="list program-days">
      {days.map((day, index) => (
        <Day
          key={day.id}
          day={day}
          label={label}
          number={index + 1}
          next={next?.day === index + 1}
          starting={starting}
        />
      ))}
    </ol>
  );
}

/** The gym days, and the travel days under their own heading when there are any. */
function AllDays({
  program,
  next,
  nextTravel,
  starting,
}: {
  program: ProgramData;
  next: Position | null;
  nextTravel: Position | null;
  starting: boolean;
}): ReactElement {
  return (
    <>
      <Days days={program.days} next={next} starting={starting} />
      {program.travel_days.length === 0 ? null : (
        <section aria-label="Travel days">
          <h2 className="subhead">Travel days</h2>
          <Days
            days={program.travel_days}
            label="Travel day"
            next={nextTravel}
            starting={starting}
          />
        </section>
      )}
    </>
  );
}

function Weeks({ weeks, current }: { weeks: number; current: number | null }): ReactElement {
  const all = Array.from({ length: weeks + 1 }, (_, index) => index + 1);
  return (
    <ol className="weeks" aria-label="Weeks">
      {all.map((week) => {
        const deload = week > weeks;
        const done = current === null || week < current;
        return (
          <li
            key={week}
            className={done ? "done" : undefined}
            aria-current={week === current ? "step" : undefined}
            title={deload ? "Deload week" : `Week ${String(week)}`}
          >
            {deload ? "D" : week}
          </li>
        );
      })}
    </ol>
  );
}

function nameAt(days: ProgramDay[], position: Position | null): string | null {
  return position === null ? null : (days[position.day - 1]?.name ?? null);
}

/** The week the next sessions fall in: the earlier of the two sets', or null once done. */
function currentWeek(next: Position | null, nextTravel: Position | null): number | null {
  const weeks = [next, nextTravel].flatMap((position) =>
    position === null ? [] : [position.week],
  );
  return weeks.length === 0 ? null : Math.min(...weeks);
}

/** "Next: Lower. Next travel: Travel B." */
function upNext(program: ProgramData, next: Position | null, nextTravel: Position | null): string {
  const gym = nameAt(program.days, next);
  const travel = nameAt(program.travel_days, nextTravel);
  return [gym === null ? null : `Next: ${gym}.`, travel === null ? null : `Next travel: ${travel}.`]
    .filter((line) => line !== null)
    .join(" ");
}

function where(program: ProgramData, next: Position | null, nextTravel: Position | null): string {
  const week = currentWeek(next, nextTravel);
  if (week === null) {
    return "Block complete. Ask the coach for your next program.";
  }
  const following = upNext(program, next, nextTravel);
  if (week > program.training_weeks) {
    return `Deload week: the same days, about half the sets at RPE 6. ${following}`;
  }
  return `Week ${String(week)} of ${String(program.training_weeks)}. ${following}`;
}

/** What each version of the program was: its edits and restarts, newest first. */
function Versions({ program }: { program: ProgramData }): ReactElement | null {
  const state = useApi("/api/programs/versions", versionsSchema);
  if (program.version < 2 || state.status !== "ready") {
    return null;
  }
  const labels = { new: "Started", edit: "Edited", restart: "Restarted" };
  return (
    <details className="card versions">
      <summary>Version history</summary>
      <ol className="lines">
        {state.data.map((version) => (
          <li key={version.id}>
            <span>
              Version {version.version} · {labels[version.change]} {formatDay(version.started_at)}
              {version.current ? " · current" : ""}
            </span>
            <span className="best">{plural(version.workouts, "workout")}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

type Mode = "view" | "edit" | "restart";

function RestartAsk({
  name,
  busy,
  onYes,
  onNo,
}: {
  name: string;
  busy: boolean;
  onYes: () => void;
  onNo: () => void;
}): ReactElement {
  return (
    <div className="notice tint" style={tone("red")} role="group" aria-label="Restart">
      <p>
        Restart “{name}”? You go back to week 1, day 1 of both gym and travel days, with the same
        days. Your logged history is kept, and each exercise starts again from the load you last
        logged.
      </p>
      <div className="row-actions">
        <button type="button" className="primary" disabled={busy} onClick={onYes}>
          Yes, restart
        </button>
        <button type="button" className="link" disabled={busy} onClick={onNo}>
          Keep going
        </button>
      </div>
    </div>
  );
}

function Actions({
  program,
  mode,
  setMode,
  onChanged,
}: {
  program: ProgramData;
  mode: Mode;
  setMode: (mode: Mode) => void;
  onChanged: () => void;
}): ReactElement | null {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restart = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    const result = await restartProgram();
    setBusy(false);
    if (result.kind === "ok") {
      onChanged();
      return;
    }
    setMode("view");
    setError(result.message);
  };
  if (mode === "edit") {
    return null;
  }
  return (
    <>
      {mode === "restart" ? (
        <RestartAsk
          name={program.name}
          busy={busy}
          onYes={() => {
            void restart();
          }}
          onNo={() => {
            setMode("view");
          }}
        />
      ) : (
        <div className="row-actions program-actions">
          <button
            type="button"
            onClick={() => {
              setError(null);
              setMode("edit");
            }}
          >
            Edit program
          </button>
          <button
            type="button"
            className="link danger"
            onClick={() => {
              setError(null);
              setMode("restart");
            }}
          >
            Restart program
          </button>
        </div>
      )}
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

function Head({
  program,
  position,
  mode,
  setMode,
  onChanged,
}: {
  program: ProgramData;
  position: { next: Position | null; nextTravel: Position | null };
  mode: Mode;
  setMode: (mode: Mode) => void;
  onChanged: () => void;
}): ReactElement {
  const { next, nextTravel } = position;
  const week = currentWeek(next, nextTravel);
  return (
    <div className="card program-head">
      <h2>{program.name}</h2>
      {program.started_at === null ? null : (
        <p className="muted">Started {formatDay(program.started_at)}</p>
      )}
      <Weeks weeks={program.training_weeks} current={week} />
      <p className={week !== null && week > program.training_weeks ? "deload" : undefined}>
        {where(program, next, nextTravel)}
      </p>
      {program.notes === null ? null : <p className="notes">{program.notes}</p>}
      <Actions program={program} mode={mode} setMode={setMode} onChanged={onChanged} />
    </div>
  );
}

function Active({
  program,
  next,
  nextTravel,
  onChanged,
}: {
  program: ProgramData;
  next: Position | null;
  nextTravel: Position | null;
  onChanged: () => void;
}): ReactElement {
  const [mode, setMode] = useState<Mode>("view");
  return (
    <section aria-label="Current program">
      <Head
        program={program}
        position={{ next, nextTravel }}
        mode={mode}
        setMode={setMode}
        onChanged={onChanged}
      />
      {mode === "edit" ? (
        <ProgramEditor
          program={program}
          onSaved={onChanged}
          onCancel={() => {
            setMode("view");
          }}
        />
      ) : (
        <AllDays program={program} next={next} nextTravel={nextTravel} starting={false} />
      )}
      <Versions program={program} />
    </section>
  );
}

type Asking = "idle" | "accept" | "decline";

/** How a proposal introduces itself: a new program to start, or an edit of the live one. */
function proposalWords(program: ProgramData): { label: string; eyebrow: string; note: string } {
  if (program.change === "edit") {
    return {
      label: "Proposed edit",
      eyebrow: "Edit proposed by your coach",
      note: "Applying it changes your program as shown. Your week, next days, history and each kept exercise’s progression stay.",
    };
  }
  return {
    label: "Proposed program",
    eyebrow: "Proposed by your coach",
    note: `${String(program.training_weeks)} training weeks, then a deload week. Loads after the first session follow your log.`,
  };
}

function Proposal({
  program,
  replaces,
  onChanged,
}: {
  program: ProgramData;
  replaces: string | null;
  onChanged: () => void;
}): ReactElement {
  const [asking, setAsking] = useState<Asking>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One change at a time: a second tap must not race the first to the server.
  const changing = useRef(false);

  const change = async (what: { accept: number } | { decline: number }): Promise<void> => {
    if (changing.current) {
      return;
    }
    changing.current = true;
    setBusy(true);
    setError(null);
    const result = await changeProgram(what);
    changing.current = false;
    setBusy(false);
    if (result.kind === "ok") {
      onChanged();
      return;
    }
    setAsking("idle");
    setError(result.message);
  };

  const accept = (): void => {
    void change({ accept: program.id });
  };
  // An edit applies to the live program, which keeps its place: nothing to replace.
  const edit = program.change === "edit";
  const words = proposalWords(program);

  return (
    <section className="card proposal tint" style={tone("peach")} aria-label={words.label}>
      <p className="eyebrow">{words.eyebrow}</p>
      <h2>{program.name}</h2>
      {program.notes === null ? null : <p className="notes">{program.notes}</p>}
      <p className="muted">{words.note}</p>
      <AllDays program={program} next={null} nextTravel={null} starting={true} />
      {asking === "accept" ? (
        <div className="notice tint" style={tone("red")} role="group" aria-label="Replace">
          <p>
            Start “{program.name}”? It replaces “{replaces}”.
          </p>
          <div className="row-actions">
            <button type="button" className="primary" disabled={busy} onClick={accept}>
              Yes, start it
            </button>
            <button
              type="button"
              className="link"
              disabled={busy}
              onClick={() => {
                setAsking("idle");
              }}
            >
              Keep my program
            </button>
          </div>
        </div>
      ) : null}
      {asking === "decline" ? (
        <div className="notice tint" style={tone("red")} role="group" aria-label="Turn down">
          <p>Turn this proposal down? The coach can propose another.</p>
          <div className="row-actions">
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() => {
                void change({ decline: program.id });
              }}
            >
              Yes, turn it down
            </button>
            <button
              type="button"
              className="link"
              disabled={busy}
              onClick={() => {
                setAsking("idle");
              }}
            >
              Keep it
            </button>
          </div>
        </div>
      ) : null}
      {asking === "idle" ? (
        <div className="row-actions">
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => {
              if (replaces === null || edit) {
                accept();
              } else {
                setAsking("accept");
              }
            }}
          >
            {edit ? "Apply this edit" : "Start this program"}
          </button>
          <button
            type="button"
            className="link danger"
            disabled={busy}
            onClick={() => {
              setAsking("decline");
            }}
          >
            Turn down
          </button>
        </div>
      ) : null}
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

/**
 * What a request typed here is sent to the coach as.
 *
 * @internal Exported for tests.
 */
export function programRequest(text: string, hasProgram: boolean): string {
  return hasProgram ? `Change my program: ${text}` : `Plan a program for me: ${text}`;
}

interface ProgramRequest {
  draft: string;
  setDraft: (draft: string) => void;
  waiting: boolean;
  reply: CoachMessage | null;
  error: string | null;
  ask: (hasProgram: boolean) => void;
}

/** Asking the coach for a program; kept by the screen, so a reload keeps the reply. */
function useProgramRequest(onAnswered: () => void): ProgramRequest {
  const [draft, setDraft] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [reply, setReply] = useState<CoachMessage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const claim = useReplyClaim();

  const ask = async (hasProgram: boolean): Promise<void> => {
    const text = draft.trim();
    if (text === "" || waiting) {
      return;
    }
    setWaiting(true);
    setError(null);
    setReply(null);
    const result = await askCoach(programRequest(text, hasProgram));
    setWaiting(false);
    if (result.kind === "ok") {
      claim(result.value);
      setDraft("");
      setReply(result.value);
      onAnswered();
      return;
    }
    setError(result.message);
  };

  return {
    draft,
    setDraft,
    waiting,
    reply,
    error,
    ask: (hasProgram): void => {
      void ask(hasProgram);
    },
  };
}

function AskCoach({
  request,
  hasProgram,
}: {
  request: ProgramRequest;
  hasProgram: boolean;
}): ReactElement {
  const { draft, waiting, reply, error } = request;
  return (
    <section className="card" aria-label="Ask the coach">
      <h2>{hasProgram ? "Change it with the coach" : "Plan one with the coach"}</h2>
      <form
        className="composer inline"
        onSubmit={(event) => {
          event.preventDefault();
          request.ask(hasProgram);
        }}
      >
        <label className="sr-only" htmlFor="program-request">
          What you want
        </label>
        <textarea
          id="program-request"
          rows={2}
          maxLength={3900}
          placeholder={
            hasProgram ? "e.g. swap squats for leg press" : "e.g. 4 days, upper/lower, an hour each"
          }
          value={draft}
          onChange={(event) => {
            request.setDraft(event.target.value);
          }}
        />
        <button type="submit" className="primary" disabled={waiting || draft.trim() === ""}>
          Ask
        </button>
      </form>
      {waiting ? (
        <p className="muted" role="status">
          The coach is working on it…
        </p>
      ) : null}
      {reply === null ? null : <p className="bubble assistant reply">{reply.text}</p>}
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function Programs({
  active,
  proposed,
  next,
  nextTravel,
  onChanged,
}: {
  active: ProgramData | null;
  proposed: ProgramData | null;
  next: Position | null;
  nextTravel: Position | null;
  onChanged: () => void;
}): ReactElement {
  return (
    <>
      {proposed === null ? null : (
        <Proposal program={proposed} replaces={active?.name ?? null} onChanged={onChanged} />
      )}
      {active === null ? (
        <p className="muted">No program yet.</p>
      ) : (
        <Active program={active} next={next} nextTravel={nextTravel} onChanged={onChanged} />
      )}
    </>
  );
}

function Loaded({
  request,
  onChanged,
}: {
  request: ProgramRequest;
  onChanged: () => void;
}): ReactElement {
  const state = useApi("/api/programs", programsSchema);
  const hasProgram =
    state.status === "ready" && (state.data.active !== null || state.data.proposed !== null);
  return (
    <>
      <Load state={state}>
        {(programs) => (
          <Programs
            active={programs.active}
            proposed={programs.proposed}
            next={programs.next}
            nextTravel={programs.next_travel}
            onChanged={onChanged}
          />
        )}
      </Load>
      {/* Only once the programs are known: "plan" and "change" are different requests. */}
      {state.status === "ready" ? <AskCoach request={request} hasProgram={hasProgram} /> : null}
    </>
  );
}

/** The Program tab: the block being trained, a proposal to accept, and the coach to ask. */
export function Program(): ReactElement {
  // Bumped to load the programs again after a change.
  const [version, setVersion] = useState(0);
  const reload = (): void => {
    setVersion((current) => current + 1);
  };
  const request = useProgramRequest(reload);
  return (
    <>
      <header className="page-head">
        <h1>Program</h1>
        <p className="muted">Six training weeks, then a deload week.</p>
      </header>
      <Loaded key={version} request={request} onChanged={reload} />
    </>
  );
}
