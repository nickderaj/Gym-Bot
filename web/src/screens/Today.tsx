import { useState } from "react";
import type { ReactElement } from "react";

import {
  currentWorkoutSchema,
  describeFailure,
  fetchJson,
  isOffline,
  todaySchema,
  useApi,
} from "../api";
import type { PlannedDay, TodayPlan, WorkoutDetail } from "../api";
import { Load, tone } from "../components";
import { formatDay, formatSet, formatTime } from "../format";
import type { Logging } from "../log/context";
import type { DraftStore } from "../log/store";
import { useDraft, useLogging } from "../log/context";
import { blockLabel, draftFromServer, newDraft, startWrite, todayDraft } from "../log/draft";
import { finishedHere } from "../log/finished";
import type { Draft } from "../log/draft";
import { href, navigate } from "../router";
import { targetLine } from "./Log";

/** "Week 3 of 6 · Upper/Lower", or the deload week (about half the sets, easy effort). */
function weekLine(today: TodayPlan, day: PlannedDay): string {
  const deload =
    day.deload_rpe === null
      ? "Deload week: fewer sets, lighter"
      : `Deload week: about half the sets at RPE ${String(day.deload_rpe)}`;
  const week = day.deload ? deload : `Week ${String(day.week)} of ${String(today.training_weeks)}`;
  return `${week} · ${today.program_name}`;
}

type DayKind = PlannedDay["kind"];

/** Which set of days the unfinished workout on the server trains; gym for older answers. */
function trainingKind(today: TodayPlan): DayKind {
  return today.training_kind ?? "gym";
}

function Exercises({ day }: { day: PlannedDay }): ReactElement {
  return (
    <ol className="list">
      {day.blocks.map((block, group) => {
        const superset = block.exercises.length > 1;
        return (
          <li
            key={block.exercises[0]?.block_exercise_id}
            className={superset ? "card block superset" : "card block"}
          >
            {superset ? <p className="muted block-kind">Superset</p> : null}
            <ul className="lines">
              {block.exercises.map((exercise, position) => {
                const plan = {
                  label: blockLabel(group, position, block.exercises.length),
                  group,
                  rest_s: block.rest_s,
                  rep_min: exercise.rep_min,
                  rep_max: exercise.rep_max,
                  target: exercise.target,
                };
                return (
                  <li key={exercise.block_exercise_id} className="planned">
                    <span>
                      <span className="block-letter">{plan.label}</span> {exercise.name}
                    </span>
                    <span className="best">{targetLine(plan, exercise.measure)}</span>
                    {exercise.variations === null ? null : (
                      <span className="muted last">
                        {exercise.target.decision === "advance" ? "Move up: " : "Variations: "}
                        {exercise.variations}
                      </span>
                    )}
                    <span className="muted last">
                      {exercise.last === null
                        ? "Not done before"
                        : `Last ${formatDay(exercise.last.started_at)}: ${exercise.last.sets
                            .map((set) => formatSet(set, exercise.measure))
                            .join(", ")}`}
                    </span>
                  </li>
                );
              })}
            </ul>
            <p className="muted block-rest">Rest {block.rest_s} s</p>
          </li>
        );
      })}
    </ol>
  );
}

type Outcome = "started" | "stale" | { error: string };

/** @internal Exported for tests. */
export const FINISHED_HERE =
  "You finished this day on this phone. The next one shows once you are back online.";
/** @internal Exported for tests. */
export const RESUME_OFFLINE = "Picking the workout back up needs a connection.";

/** Start `day` as a new workout on this phone, prefilled from its targets. */
function startDay(logging: Logging, program_name: string, day: PlannedDay): Outcome {
  const fresh = todayDraft(
    { ...newDraft(crypto.randomUUID(), new Date()), program_name },
    day,
    () => crypto.randomUUID(),
  );
  const used = logging.drafts.update((current) => current ?? fresh);
  if (used === fresh) {
    void logging.outbox.send(startWrite(fresh));
  }
  return "started";
}

/**
 * Start the day shown, if it is still the day to train. The server is asked
 * afresh (never a saved copy); without signal, the saved plan is used unless
 * this phone has already finished that day.
 */
async function start(logging: Logging, today: TodayPlan, day: PlannedDay): Promise<Outcome> {
  let fresh: TodayPlan | null;
  try {
    fresh = await fetchJson("/api/today", todaySchema, undefined, true);
  } catch (error) {
    // Only no signal falls back to the saved plan; a refusal or an odd answer is shown.
    if (!isOffline(error)) {
      return { error: describeFailure(error) };
    }
    if (finishedHere(localStorage, day.id, day.week)) {
      return { error: FINISHED_HERE };
    }
    return startDay(logging, today.program_name, day);
  }
  const due = stillDue(fresh, day);
  return due === null ? "stale" : startDay(logging, today.program_name, due);
}

/** The next day of `kind` in a plan. */
function nextOf(plan: TodayPlan | null, kind: DayKind): PlannedDay | null {
  return (kind === "travel" ? plan?.travel_day : plan?.day) ?? null;
}

/** The day in a fresh plan, if it is still `day` and no program workout is unfinished. */
function stillDue(fresh: TodayPlan | null, day: PlannedDay): PlannedDay | null {
  const due = nextOf(fresh, day.kind);
  const free = fresh?.workout_client_id === null && fresh.left_over === null;
  return due?.id === day.id && due.week === day.week && free ? due : null;
}

/**
 * Pick up the server's workout for this day, which this phone lost: only if it
 * is still the one in progress, for this day and week.
 */
async function resume(
  logging: Logging,
  today: TodayPlan,
  day: PlannedDay,
  id: string,
): Promise<Outcome> {
  let detail;
  try {
    detail = await fetchJson("/api/workouts/current", currentWorkoutSchema, undefined, true);
  } catch (error) {
    return { error: isOffline(error) ? RESUME_OFFLINE : describeFailure(error) };
  }
  if (
    detail?.client_id !== id ||
    detail.program_day_id !== day.id ||
    detail.program_week !== day.week
  ) {
    return "stale";
  }
  const { started_at } = detail;
  const rebuilt = todayDraft(
    { id, started_at, program_name: today.program_name },
    day,
    () => crypto.randomUUID(),
    detail,
  );
  logging.drafts.update((current) => current ?? rebuilt);
  return "started";
}

/** The workout this phone has is in progress: carry on with it. */
function ResumeLink({ same }: { same: boolean }): ReactElement {
  return (
    <div className="row-actions">
      {same ? null : <p className="muted">Another workout is in progress.</p>}
      <a className="primary" href={href({ name: "log" })}>
        Resume ›
      </a>
    </div>
  );
}

function Starter({
  logging,
  today,
  day,
  onStale,
}: {
  logging: Logging;
  today: TodayPlan;
  day: PlannedDay;
  onStale: () => void;
}): ReactElement {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const id = today.workout_client_id;
  const begin = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    const outcome = await (id === null
      ? start(logging, today, day)
      : resume(logging, today, day, id));
    setBusy(false);
    if (outcome === "started") {
      navigate({ name: "log" });
    } else if (outcome === "stale") {
      onStale();
    } else {
      setError(outcome.error);
    }
  };
  return (
    <>
      <button
        type="button"
        className="primary"
        disabled={busy}
        onClick={() => {
          void begin();
        }}
      >
        {id === null ? "Start this workout" : "Resume this workout"}
      </button>
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}

/** Start the day, or carry on with the workout already training it. */
function Begin({
  logging,
  draft,
  today,
  day,
  onStale,
}: {
  logging: Logging;
  draft: Draft | null;
  today: TodayPlan;
  day: PlannedDay;
  onStale: () => void;
}): ReactElement {
  if (draft !== null) {
    return <ResumeLink same={draft.program?.day_id === day.id} />;
  }
  // The server's unfinished workout trains one set of days: only that set resumes it.
  if (today.workout_client_id !== null && trainingKind(today) !== day.kind) {
    return (
      <p className="muted">
        A {trainingKind(today)} workout is in progress. Finish it before starting this one.
      </p>
    );
  }
  return <Starter logging={logging} today={today} day={day} onStale={onStale} />;
}

/** "Day 2 of 4", or "Travel day 1 of 3". */
function dayCount(today: TodayPlan, day: PlannedDay): string {
  return day.kind === "travel"
    ? `Travel day ${String(day.position)} of ${String(today.travel_days)}`
    : `Day ${String(day.position)} of ${String(today.days)}`;
}

function Planned({
  today,
  day,
  onStale,
  held,
}: {
  today: TodayPlan;
  day: PlannedDay;
  onStale: () => void;
  /** Another program workout is unfinished: this day waits for it. */
  held: boolean;
}): ReactElement {
  const logging = useLogging();
  const draft = useDraft(logging?.drafts ?? null);
  return (
    <>
      <header className="page-head today-head">
        <p className="muted">{dayCount(today, day)}</p>
        <h1>{day.name}</h1>
        <p className={day.deload ? "deload" : "muted"}>{weekLine(today, day)}</p>
        {logging === null || (held && draft === null) ? null : (
          <Begin logging={logging} draft={draft} today={today} day={day} onStale={onStale} />
        )}
      </header>
      <Exercises day={day} />
    </>
  );
}

function Nothing({ text }: { text: string }): ReactElement {
  return (
    <>
      <h1>Today</h1>
      <p className="muted">{text}</p>
      <a className="button" href={href({ name: "program" })}>
        Program ›
      </a>
    </>
  );
}

/** @internal Exported for tests. */
export const LEFT_OVER =
  "A workout of a program you have since replaced is unfinished. Finish or discard it before the next day.";

/**
 * Pick up an unfinished workout of a replaced program (possible in data from
 * before replacing a program mid-workout was refused). It has no plan to
 * rebuild, so it resumes as logged, with its program day kept for its finish.
 */
function LeftOver({
  workout,
  onStale,
}: {
  workout: WorkoutDetail;
  onStale: () => void;
}): ReactElement | null {
  const logging = useLogging();
  const draft = useDraft(logging?.drafts ?? null);
  const [error, setError] = useState<string | null>(null);
  if (draft !== null) {
    return null; // this phone has it, or another workout: Resume is offered above
  }
  const pickUp = async (drafts: DraftStore): Promise<void> => {
    setError(null);
    let fresh;
    try {
      fresh = await fetchJson("/api/today", todaySchema, undefined, true);
    } catch (failure) {
      setError(isOffline(failure) ? RESUME_OFFLINE : describeFailure(failure));
      return;
    }
    // The server's left-over, read now, must still be this one.
    const left = fresh?.left_over ?? null;
    const id = workout.client_id;
    if (id === null || left?.client_id !== id) {
      onStale();
      return;
    }
    drafts.update((current) => current ?? draftFromServer(left, id));
    navigate({ name: "log" });
  };
  return (
    <section className="notice tint" style={tone("peach")} aria-label="Unfinished workout">
      <p>{LEFT_OVER}</p>
      <p className="muted">
        Started {formatDay(workout.started_at)}, {formatTime(workout.started_at)}
      </p>
      {logging === null ? null : (
        <button
          type="button"
          className="primary"
          onClick={() => {
            void pickUp(logging.drafts);
          }}
        >
          Resume it as logged
        </button>
      )}
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function Plan({ onStale }: { onStale: () => void }): ReactElement {
  const state = useApi("/api/today", todaySchema);
  return (
    <Load state={state}>
      {(today) => {
        // Both from the one answer: the day's workout and any left-over.
        const left = today?.left_over ?? null;
        return (
          <>
            {left === null ? null : <LeftOver workout={left} onStale={onStale} />}
            <Day today={today} held={left !== null} onStale={onStale} />
          </>
        );
      }}
    </Load>
  );
}

/** Gym or travel: which set of days to train. */
function SetChoice({
  kind,
  onChange,
}: {
  kind: DayKind;
  onChange: (kind: DayKind) => void;
}): ReactElement {
  return (
    <div className="segmented" role="group" aria-label="Which days">
      {(["gym", "travel"] as const).map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={kind === option}
          onClick={() => {
            onChange(option);
          }}
        >
          {option === "gym" ? "Gym" : "Travel"}
        </button>
      ))}
    </div>
  );
}

/** The set shown first: the one being trained, else gym (travel if only it has a day left). */
function firstKind(today: TodayPlan): DayKind {
  if (today.workout_client_id !== null) {
    return trainingKind(today);
  }
  return today.day === null && today.travel_day !== null ? "travel" : "gym";
}

function Day({
  today,
  held,
  onStale,
}: {
  today: TodayPlan | null;
  held: boolean;
  onStale: () => void;
}): ReactElement {
  const [chosen, setChosen] = useState<DayKind | null>(null);
  if (today === null) {
    return <Nothing text="No program yet. Ask the coach for one." />;
  }
  if (today.day === null && today.travel_day === null) {
    return <Nothing text="Block complete. Ask the coach for your next program." />;
  }
  const kind = chosen ?? firstKind(today);
  const day = kind === "travel" ? today.travel_day : today.day;
  return (
    <>
      {today.travel_days === 0 ? null : <SetChoice kind={kind} onChange={setChosen} />}
      {day === null ? (
        <p className="muted">The {kind} days of this block are done.</p>
      ) : (
        <Planned today={today} day={day} onStale={onStale} held={held} />
      )}
    </>
  );
}

/** Today: the active program's next day, with every set's target and last time. */
export function Today(): ReactElement {
  // Bumped to load the plan again when it turns out to have changed.
  const [version, setVersion] = useState(0);
  return (
    <>
      <a className="back" href={href({ name: "home" })}>
        ‹ Home
      </a>
      {version === 0 ? null : (
        <p className="notice tint" style={tone("peach")} role="status">
          The plan had changed since this screen was loaded. This is the current one.
        </p>
      )}
      <Plan
        key={version}
        onStale={() => {
          setVersion((current) => current + 1);
        }}
      />
    </>
  );
}

function NextCard({ day, travel }: { day: PlannedDay; travel: string | null }): ReactElement {
  return (
    <a className="card start tint" style={tone("green")} href={href({ name: "today" })}>
      <strong>Next in your program</strong>
      <span>
        {day.name} · {day.deload ? "Deload week" : `Week ${String(day.week)}`}
      </span>
      {travel === null ? null : <span>Travel: {travel}</span>}
      <span className="go">Open ›</span>
    </a>
  );
}

/** Home's pointer to the next program day, when there is one and nothing is in progress. */
export function NextDayCard(): ReactElement | null {
  const state = useApi("/api/today", todaySchema);
  const today = state.status === "ready" ? state.data : null;
  const day = today?.day;
  if (day === null || day === undefined) {
    return null;
  }
  return <NextCard day={day} travel={today?.travel_day?.name ?? null} />;
}
