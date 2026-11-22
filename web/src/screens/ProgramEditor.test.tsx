import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";

import { App } from "../App";
import { OFFLINE_PROGRAM } from "../api";
import { at } from "../test/fetch";
import { routeFetch } from "../test/logging";

const PROGRAMS = "GET /api/programs";
const EXERCISES = "GET /api/exercises";

function exercise(
  id: number,
  name: string,
  extra: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  return {
    id,
    exercise_id: id + 100,
    name,
    equipment: "barbell",
    measure: "reps",
    sets: 3,
    rep_min: 8,
    rep_max: 12,
    start_load_kg: null,
    notes: null,
    progression: "load",
    variations: null,
    ...extra,
  };
}

function day(id: number, name: string, ...blocks: Record<string, unknown>[][]): unknown {
  return {
    id,
    name,
    blocks: blocks.map((exercises) => ({ rest_s: 90, exercises })),
  };
}

function hybrid(extra: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    id: 1,
    name: "Hybrid",
    notes: "Gym and travel.",
    training_weeks: 6,
    status: "active",
    started_at: "2026-09-28T07:00:00+00:00",
    version: 1,
    run: 1,
    change: "new",
    days: [
      day(
        11,
        "Upper",
        [exercise(1, "Bench Press", { start_load_kg: 60 })],
        [exercise(2, "Cable Row"), exercise(3, "Curl")],
      ),
      day(12, "Lower", [exercise(4, "Squat")]),
    ],
    travel_days: [
      day(21, "Travel A", [
        exercise(5, "Push-up", {
          progression: "reps",
          variations: "push-up -> archer",
          rep_min: 10,
          rep_max: 15,
          sets: 4,
        }),
      ]),
      day(22, "Travel B", [exercise(6, "Band Row", { progression: "reps" })]),
    ],
    ...extra,
  };
}

const CATALOGUE = [
  { id: 101, name: "Bench Press", measure: "reps" },
  { id: 102, name: "Cable Row", measure: "reps" },
  { id: 105, name: "Push-up", measure: "reps" },
  { id: 120, name: "Dead Hang", measure: "seconds" },
  { id: 130, name: "Run", measure: "distance" },
].map((entry) => ({
  ...entry,
  equipment: "barbell",
  muscle_groups: null,
  workouts: 0,
  last_done: null,
  best_load_kg: null,
}));

function programs(
  active: unknown,
  proposed: unknown = null,
  next: unknown = { week: 2, day: 2 },
  nextTravel: unknown = { week: 2, day: 1 },
): Record<string, { body: unknown }> {
  return {
    [PROGRAMS]: { body: { active, proposed, next, next_travel: nextTravel } },
    [EXERCISES]: { body: CATALOGUE },
  };
}

beforeEach(() => {
  window.location.hash = "#/program";
});

afterEach(() => {
  window.location.hash = "";
  vi.unstubAllGlobals();
});

function puts(fetchMock: Mock<typeof fetch>): { path: string; body: unknown }[] {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === "PUT")
    .map(([input, init]) => ({
      path: typeof input === "string" ? input : "(not a string)",
      body: JSON.parse(typeof init?.body === "string" ? init.body : "null") as unknown,
    }));
}

/** One day's card in the editor, by its title ("Gym day 2"). */
function card(root: HTMLElement, title: string): HTMLElement {
  const found = within(root).getByLabelText(`${title} name`).closest("li");
  if (found === null) {
    throw new Error(`no card for ${title}`);
  }
  return found;
}

async function openEditor(): Promise<HTMLElement> {
  render(<App />);
  const current = await screen.findByRole("region", { name: "Current program" });
  fireEvent.click(within(current).getByRole("button", { name: "Edit program" }));
  return screen.findByRole("form", { name: "Edit program" });
}

describe("travel days on the Program screen", () => {
  it("shows both sets, each with its own next day", async () => {
    routeFetch(programs(hybrid()));
    render(<App />);

    const current = await screen.findByRole("region", { name: "Current program" });
    expect(
      within(current).getByText("Week 2 of 6. Next: Lower. Next travel: Travel A."),
    ).toBeInTheDocument();
    const travel = within(current).getByRole("region", { name: "Travel days" });
    expect(within(travel).getByRole("heading", { name: "Travel days" })).toBeInTheDocument();
    const cards = travel.querySelectorAll(".program-day");
    expect([...cards].map((card) => card.getAttribute("aria-current"))).toEqual(["step", null]);
    expect(within(cards[0] as HTMLElement).getByText("Travel day 1")).toBeInTheDocument();
    expect(within(cards[0] as HTMLElement).getByText("4 × 10–15 · reps only")).toBeInTheDocument();
    expect(within(cards[0] as HTMLElement).getByText("push-up -> archer")).toBeInTheDocument();
    const gym = current.querySelectorAll(":scope > .program-days > .program-day");
    expect([...gym].map((card) => card.getAttribute("aria-current"))).toEqual([null, "step"]);
  });

  it("names a travel day alone when the gym days are done", async () => {
    routeFetch(programs(hybrid(), null, null, { week: 3, day: 2 }));
    render(<App />);

    expect(await screen.findByText("Week 3 of 6. Next travel: Travel B.")).toBeInTheDocument();
  });

  it("says the deload week applies to both sets", async () => {
    routeFetch(programs(hybrid(), null, { week: 7, day: 1 }, { week: 7, day: 1 }));
    render(<App />);

    const line = await screen.findByText(
      "Deload week: the same days, about half the sets at RPE 6. Next: Upper. Next travel: Travel A.",
    );
    expect(line).toHaveClass("deload");
  });

  it("is done only when both sets are", async () => {
    routeFetch(programs(hybrid(), null, null, null));
    render(<App />);

    expect(
      await screen.findByText("Block complete. Ask the coach for your next program."),
    ).toBeInTheDocument();
  });

  it("shows no travel section without travel days", async () => {
    routeFetch(programs(hybrid({ travel_days: [] }), null, { week: 1, day: 1 }, null));
    render(<App />);

    await screen.findByRole("region", { name: "Current program" });
    expect(screen.queryByRole("region", { name: "Travel days" })).not.toBeInTheDocument();
  });

  it("shows the travel days of a proposal, with no next day", async () => {
    routeFetch(programs(null, hybrid({ status: "proposed", started_at: null })));
    render(<App />);

    const proposal = await screen.findByRole("region", { name: "Proposed program" });
    const travel = within(proposal).getByRole("region", { name: "Travel days" });
    expect(travel.querySelector(".day-link")).toBeNull();
  });
});

describe("an edit proposed by the coach", () => {
  it("is applied at once, with no program to replace", async () => {
    const fetchMock = routeFetch({
      ...programs(
        hybrid(),
        hybrid({ id: 2, status: "proposed", started_at: null, change: "edit" }),
      ),
      "POST /api/programs/2/accept": { body: hybrid() },
    });
    render(<App />);

    const proposal = await screen.findByRole("region", { name: "Proposed edit" });
    expect(within(proposal).getByText("Edit proposed by your coach")).toBeInTheDocument();
    expect(within(proposal).getByText(/Your week, next days, history/)).toBeInTheDocument();
    fireEvent.click(within(proposal).getByRole("button", { name: "Apply this edit" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/programs/2/accept", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
    });
    expect(within(proposal).queryByRole("group", { name: "Replace" })).not.toBeInTheDocument();
  });

  it("can be turned down", async () => {
    const fetchMock = routeFetch({
      ...programs(
        hybrid(),
        hybrid({ id: 2, status: "proposed", started_at: null, change: "edit" }),
      ),
      "POST /api/programs/2/decline": { body: null },
    });
    render(<App />);

    const proposal = await screen.findByRole("region", { name: "Proposed edit" });
    fireEvent.click(within(proposal).getByRole("button", { name: "Turn down" }));
    fireEvent.click(within(proposal).getByRole("button", { name: "Yes, turn it down" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/programs/2/decline", expect.anything());
    });
  });
});

describe("restarting the program", () => {
  it("asks first, and changes nothing if the owner keeps going", async () => {
    const fetchMock = routeFetch(programs(hybrid()));
    render(<App />);

    const current = await screen.findByRole("region", { name: "Current program" });
    fireEvent.click(within(current).getByRole("button", { name: "Restart program" }));
    const ask = within(current).getByRole("group", { name: "Restart" });
    expect(ask).toHaveTextContent("Restart “Hybrid”?");
    expect(ask).toHaveTextContent("week 1, day 1 of both gym and travel days");
    expect(ask).toHaveTextContent("logged history is kept");
    expect(within(current).queryByRole("button", { name: "Edit program" })).not.toBeInTheDocument();

    fireEvent.click(within(ask).getByRole("button", { name: "Keep going" }));

    expect(within(current).queryByRole("group", { name: "Restart" })).not.toBeInTheDocument();
    expect(within(current).getByRole("button", { name: "Restart program" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("restarts and loads the program again", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "POST /api/programs/active/restart": { body: hybrid() },
    });
    render(<App />);

    const current = await screen.findByRole("region", { name: "Current program" });
    fireEvent.click(within(current).getByRole("button", { name: "Restart program" }));
    fireEvent.click(within(current).getByRole("button", { name: "Yes, restart" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/programs/active/restart", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
    });
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([path]) => path === "/api/programs")).toHaveLength(2);
    });
  });

  it.each([
    [
      { status: 409, body: { detail: "finish or discard the workout in progress first" } },
      "Finish or discard the workout in progress first.",
    ],
    [new TypeError("offline"), OFFLINE_PROGRAM],
  ])("says why it could not", async (reply, message) => {
    routeFetch({ ...programs(hybrid()), "POST /api/programs/active/restart": reply });
    render(<App />);

    const current = await screen.findByRole("region", { name: "Current program" });
    fireEvent.click(within(current).getByRole("button", { name: "Restart program" }));
    fireEvent.click(within(current).getByRole("button", { name: "Yes, restart" }));

    expect(await within(current).findByRole("alert")).toHaveTextContent(message);
    expect(within(current).getByRole("button", { name: "Restart program" })).toBeInTheDocument();
    expect(within(current).queryByRole("group", { name: "Restart" })).not.toBeInTheDocument();
  });

  it("clears an earlier failure when asked again", async () => {
    routeFetch({
      ...programs(hybrid()),
      "POST /api/programs/active/restart": { status: 500, body: "oops" },
    });
    render(<App />);

    const current = await screen.findByRole("region", { name: "Current program" });
    fireEvent.click(within(current).getByRole("button", { name: "Restart program" }));
    fireEvent.click(within(current).getByRole("button", { name: "Yes, restart" }));
    await within(current).findByRole("alert");
    fireEvent.click(within(current).getByRole("button", { name: "Restart program" }));

    expect(within(current).queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("version history", () => {
  const versions = [
    {
      id: 3,
      version: 3,
      run: 2,
      change: "restart",
      name: "Hybrid",
      started_at: "2026-10-20T08:00:00+00:00",
      current: true,
      workouts: 0,
    },
    {
      id: 2,
      version: 2,
      run: 1,
      change: "edit",
      name: "Hybrid",
      started_at: "2026-09-28T07:00:00+00:00",
      current: false,
      workouts: 1,
    },
    {
      id: 1,
      version: 1,
      run: 1,
      change: "new",
      name: "Hybrid",
      started_at: "2026-09-28T07:00:00+00:00",
      current: false,
      workouts: 8,
    },
  ];

  it("lists every version once there is more than one", async () => {
    routeFetch({
      ...programs(hybrid({ version: 3, change: "restart" })),
      "GET /api/programs/versions": { body: versions },
    });
    render(<App />);

    const history = await screen.findByText("Version history");
    const lines = [...(history.closest("details")?.querySelectorAll("li") ?? [])].map(
      (line) => line.textContent,
    );
    expect(lines).toEqual([
      "Version 3 · Restarted Tue 20 Oct · current0 workouts",
      "Version 2 · Edited Mon 28 Sept1 workout",
      "Version 1 · Started Mon 28 Sept8 workouts",
    ]);
  });

  it("is not shown for a program that was never changed", async () => {
    routeFetch({
      ...programs(hybrid()),
      "GET /api/programs/versions": { body: versions.slice(2) },
    });
    render(<App />);

    await screen.findByRole("region", { name: "Current program" });
    expect(screen.queryByText("Version history")).not.toBeInTheDocument();
  });

  it("is left out when it cannot be read", async () => {
    routeFetch({
      ...programs(hybrid({ version: 2 })),
      "GET /api/programs/versions": new TypeError("offline"),
    });
    render(<App />);

    await screen.findByRole("region", { name: "Current program" });
    expect(screen.queryByText("Version history")).not.toBeInTheDocument();
  });
});

describe("editing the program", () => {
  it("opens the editor with the program as it is, then goes back on cancel", async () => {
    routeFetch(programs(hybrid()));
    const form = await openEditor();

    expect(within(form).getByLabelText("Program name")).toHaveValue("Hybrid");
    expect(within(form).getByLabelText("Program notes")).toHaveValue("Gym and travel.");
    expect(within(form).getByRole("region", { name: "Gym days" })).toBeInTheDocument();
    expect(within(form).getByRole("region", { name: "Travel days" })).toBeInTheDocument();
    expect(within(form).getByLabelText("Gym day 1 name")).toHaveValue("Upper");
    expect(within(form).getByLabelText("Bench Press sets")).toHaveValue("3");
    expect(within(form).getByLabelText("Bench Press start kg")).toHaveValue("60");
    expect(within(form).queryByLabelText("Push-up start kg")).not.toBeInTheDocument();
    expect(within(form).getByLabelText("Push-up variations")).toHaveValue("push-up -> archer");
    expect(screen.queryByRole("button", { name: "Restart program" })).not.toBeInTheDocument();

    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("form", { name: "Edit program" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit program" })).toBeInTheDocument();
  });

  it("waits for the exercise list, and says if it cannot be read", async () => {
    routeFetch({ ...programs(hybrid()), [EXERCISES]: new TypeError("offline") });
    render(<App />);

    const current = await screen.findByRole("region", { name: "Current program" });
    fireEvent.click(within(current).getByRole("button", { name: "Edit program" }));

    expect(await screen.findByText("Could not reach the server")).toBeInTheDocument();
  });

  it("saves the changes as an edit that keeps what was not touched", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid({ version: 2, change: "edit" }) },
    });
    const form = await openEditor();

    fireEvent.change(within(form).getByLabelText("Program name"), {
      target: { value: " Hybrid v2 " },
    });
    fireEvent.change(within(form).getByLabelText("Bench Press sets"), { target: { value: "4" } });
    fireEvent.change(within(form).getByLabelText("Bench Press reps from"), {
      target: { value: "6" },
    });
    fireEvent.change(within(form).getByLabelText("Bench Press to"), { target: { value: "8" } });
    fireEvent.change(within(form).getByLabelText("Bench Press start kg"), {
      target: { value: "62.5" },
    });
    fireEvent.change(within(form).getByLabelText("Bench Press notes"), {
      target: { value: "pause" },
    });
    fireEvent.change(within(card(form, "Gym day 1")).getByLabelText("Rest after block A (s)"), {
      target: { value: "150" },
    });
    fireEvent.change(within(form).getByLabelText("Gym day 1 name"), {
      target: { value: "Upper A" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const [saved] = puts(fetchMock);
    expect(saved?.path).toBe("/api/programs/active");
    expect(saved?.body).toMatchObject({
      name: "Hybrid v2",
      notes: "Gym and travel.",
      days: [
        {
          id: 11,
          name: "Upper A",
          blocks: [
            {
              rest_s: 150,
              exercises: [
                {
                  id: 1,
                  exercise_id: 101,
                  sets: 4,
                  rep_min: 6,
                  rep_max: 8,
                  start_load_kg: 62.5,
                  notes: "pause",
                  progression: "load",
                },
              ],
            },
            { rest_s: 90, exercises: [{ id: 2 }, { id: 3 }] },
          ],
        },
        { id: 12, name: "Lower" },
      ],
      travel_days: [
        {
          id: 21,
          name: "Travel A",
          blocks: [
            { exercises: [{ id: 5, progression: "reps", variations: "push-up -> archer" }] },
          ],
        },
        { id: 22 },
      ],
    });
    // The programs are loaded again once it is saved.
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([path]) => path === "/api/programs")).toHaveLength(2);
    });
  });

  it("will not send what is not valid, and says why", async () => {
    const fetchMock = routeFetch(programs(hybrid()));
    const form = await openEditor();

    fireEvent.change(within(form).getByLabelText("Bench Press sets"), { target: { value: "0" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Bench Press: sets must be a whole number from 1 to 10.",
    );
    expect(puts(fetchMock)).toEqual([]);
  });

  it.each([
    [
      { status: 409, body: { detail: "finish or discard the workout in progress first" } },
      "Finish or discard the workout in progress first.",
    ],
    [
      { status: 422, body: { detail: "the live program has no day with id 11" } },
      "The live program has no day with id 11.",
    ],
    [new TypeError("offline"), OFFLINE_PROGRAM],
    [{ status: 500, body: "oops" }, "The server answered 500"],
  ])("says why saving failed and keeps what was typed", async (reply, message) => {
    routeFetch({ ...programs(hybrid()), "PUT /api/programs/active": reply });
    const form = await openEditor();

    fireEvent.change(within(form).getByLabelText("Bench Press sets"), { target: { value: "5" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(message);
    expect(within(form).getByLabelText("Bench Press sets")).toHaveValue("5");
    expect(within(form).getByRole("button", { name: "Save changes" })).toBeEnabled();
  });

  it("locks the form while saving, and saves once", async () => {
    let answer: (response: Response) => void = () => undefined;
    const pending = new Promise<Response>((done) => {
      answer = done;
    });
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      if (init?.method === "PUT") {
        return pending;
      }
      const body = (typeof input === "string" ? input : "").includes("exercises")
        ? CATALOGUE
        : { active: hybrid(), proposed: null, next: null, next_travel: null };
      return Promise.resolve(new Response(JSON.stringify(body)));
    });
    vi.stubGlobal("fetch", fetchMock);
    const form = await openEditor();

    const save = within(form).getByRole("button", { name: "Save changes" });
    fireEvent.click(save);
    await waitFor(() => {
      expect(save).toBeDisabled();
    });
    fireEvent.submit(form);
    expect(within(form).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(puts(fetchMock)).toHaveLength(1);
    answer(new Response(JSON.stringify(hybrid()), { status: 200 }));
    await waitFor(() => {
      expect(screen.queryByRole("form", { name: "Edit program" })).not.toBeInTheDocument();
    });
  });

  it("changes how an exercise progresses", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();

    fireEvent.change(within(form).getByLabelText("Bench Press progression"), {
      target: { value: "reps" },
    });
    expect(within(form).queryByLabelText("Bench Press start kg")).not.toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("Bench Press variations"), {
      target: { value: "bench -> floor press" },
    });
    fireEvent.change(within(form).getByLabelText("Push-up progression"), {
      target: { value: "load" },
    });
    expect(within(form).queryByLabelText("Push-up variations")).not.toBeInTheDocument();
    expect(within(form).getByLabelText("Push-up start kg")).toHaveValue("");
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(0 + 1);
    });
    const [saved] = puts(fetchMock);
    const sent = saved?.body as { days: { blocks: { exercises: unknown[] }[] }[] };
    expect(sent.days[0]?.blocks[0]?.exercises[0]).toMatchObject({
      id: 1,
      progression: "reps",
      start_load_kg: null,
      variations: "bench -> floor press",
    });
    // The other exercise, switched to load, keeps nothing of a reps slot.
    const travel = (saved?.body as { travel_days: { blocks: { exercises: unknown[] }[] }[] })
      .travel_days;
    expect(travel[0]?.blocks[0]?.exercises[0]).toMatchObject({
      id: 5,
      progression: "load",
      variations: null,
      start_load_kg: null,
    });
  });
});

describe("reshaping the days", () => {
  it("adds, removes and reorders gym days", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const gym = within(form).getByRole("region", { name: "Gym days" });

    // Add a day starting with an exercise: a new day with one load exercise.
    fireEvent.change(within(gym).getByLabelText("Add a gym day starting with"), {
      target: { value: "120" },
    });
    expect(within(gym).getByLabelText("Gym day 3 name")).toHaveValue("Day 3");
    expect(within(gym).getByLabelText("Dead Hang seconds from")).toHaveValue("30");
    // The first day cannot move up; the last cannot move down.
    expect(within(gym).getByRole("button", { name: "Move gym day 1 up" })).toBeDisabled();
    expect(within(gym).getByRole("button", { name: "Move gym day 3 down" })).toBeDisabled();
    fireEvent.click(within(gym).getByRole("button", { name: "Move gym day 3 up" }));
    expect(within(gym).getByLabelText("Gym day 2 name")).toHaveValue("Day 3");
    fireEvent.click(within(gym).getByRole("button", { name: "Move gym day 1 down" }));
    expect(within(gym).getByLabelText("Gym day 1 name")).toHaveValue("Day 3");
    fireEvent.click(within(gym).getByRole("button", { name: "Remove gym day 3" }));
    expect(within(gym).queryByLabelText("Gym day 3 name")).not.toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const saved = puts(fetchMock)[0]?.body as { days: { id?: number; name: string }[] };
    expect(saved.days.map((entry) => [entry.id ?? null, entry.name])).toEqual([
      [null, "Day 3"],
      [11, "Upper"],
    ]);
  });

  it("adds an exercise to a day as a new block, and to a block as a superset", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const lower = card(form, "Gym day 2");

    fireEvent.change(within(lower).getByLabelText("Add an exercise to gym day 2"), {
      target: { value: "105" },
    });
    expect(within(lower).getByText("Block B")).toBeInTheDocument();
    fireEvent.change(at(within(lower).getAllByLabelText("Superset with block A"), 0), {
      target: { value: "102" },
    });
    expect(within(lower).getByText("Block A · superset")).toBeInTheDocument();
    // A superset takes three exercises at most.
    fireEvent.change(at(within(lower).getAllByLabelText("Superset with block A"), 0), {
      target: { value: "101" },
    });
    expect(within(lower).queryByLabelText("Superset with block A")).not.toBeInTheDocument();
    expect(within(lower).getByLabelText("Superset with block B")).toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const saved = puts(fetchMock)[0]?.body as {
      days: { name: string; blocks: { exercises: { id?: number; exercise_id: number }[] }[] }[];
    };
    const blocks = saved.days[1]?.blocks.map((block) =>
      block.exercises.map((entry) => entry.id ?? entry.exercise_id),
    );
    expect(blocks).toEqual([[4, 102, 101], [105]]);
  });

  it("reorders blocks and the exercises of a superset", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const upper = card(form, "Gym day 1");

    expect(within(upper).getByRole("button", { name: "Move block A up" })).toBeDisabled();
    expect(within(upper).getByRole("button", { name: "Move block B down" })).toBeDisabled();
    fireEvent.click(within(upper).getByRole("button", { name: "Move block A down" }));
    expect(
      within(upper)
        .getAllByText(/^Block /)
        .map((label) => label.textContent),
    ).toEqual(["Block A · superset", "Block B"]);
    expect(within(upper).getByRole("button", { name: "Move Cable Row up" })).toBeDisabled();
    expect(within(upper).getByRole("button", { name: "Move Curl down" })).toBeDisabled();
    fireEvent.click(within(upper).getByRole("button", { name: "Move Curl up" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const saved = puts(fetchMock)[0]?.body as {
      days: { blocks: { exercises: { id: number }[] }[] }[];
    };
    expect(saved.days[0]?.blocks.map((block) => block.exercises.map((e) => e.id))).toEqual([
      [3, 2],
      [1],
    ]);
  });

  it("moves an exercise down within a superset", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const upper = card(form, "Gym day 1");

    fireEvent.click(within(upper).getByRole("button", { name: "Move Cable Row down" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const saved = puts(fetchMock)[0]?.body as {
      days: { blocks: { exercises: { id: number }[] }[] }[];
    };
    expect(saved.days[0]?.blocks[1]?.exercises.map((e) => e.id)).toEqual([3, 2]);
  });

  it("removes an exercise, then its block, then its day", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const upper = card(form, "Gym day 1");

    fireEvent.click(within(upper).getByRole("button", { name: "Remove Curl" }));
    expect(within(upper).queryByText("Block B · superset")).not.toBeInTheDocument();
    fireEvent.click(within(upper).getByRole("button", { name: "Remove Cable Row" }));
    expect(within(upper).queryByLabelText("Cable Row sets")).not.toBeInTheDocument();
    fireEvent.click(within(upper).getByRole("button", { name: "Remove Bench Press" }));
    // Gym day 1 had nothing left, so it is gone and Lower is day 1.
    const gym = within(form).getByRole("region", { name: "Gym days" });
    expect(within(gym).getByLabelText("Gym day 1 name")).toHaveValue("Lower");
    expect(within(gym).queryByLabelText("Gym day 2 name")).not.toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const saved = puts(fetchMock)[0]?.body as { days: { name: string }[] };
    expect(saved.days.map((entry) => entry.name)).toEqual(["Lower"]);
  });

  it("will not save a program with no gym day", async () => {
    routeFetch(programs(hybrid({ days: [day(11, "Upper", [exercise(1, "Bench Press")])] })));
    const form = await openEditor();

    fireEvent.click(within(form).getByRole("button", { name: "Remove Bench Press" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "The program needs at least one gym day.",
    );
    expect(within(form).getByText("No gym days.")).toBeInTheDocument();
  });

  it("adds and removes travel days, whose exercises progress by reps", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const travel = within(form).getByRole("region", { name: "Travel days" });

    fireEvent.change(within(travel).getByLabelText("Add a travel day starting with"), {
      target: { value: "105" },
    });
    expect(within(travel).getByLabelText("Travel day 3 name")).toHaveValue("Travel 3");
    fireEvent.click(within(travel).getByRole("button", { name: "Remove travel day 1" }));
    fireEvent.click(within(travel).getByRole("button", { name: "Remove travel day 1" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    const saved = puts(fetchMock)[0]?.body as {
      travel_days: { name: string; blocks: { exercises: { progression: string }[] }[] }[];
    };
    expect(saved.travel_days).toHaveLength(1);
    expect(saved.travel_days[0]?.name).toBe("Travel 3");
    expect(saved.travel_days[0]?.blocks[0]?.exercises[0]?.progression).toBe("reps");
  });

  it("turns every travel day off", async () => {
    const fetchMock = routeFetch({
      ...programs(hybrid()),
      "PUT /api/programs/active": { body: hybrid() },
    });
    const form = await openEditor();
    const travel = within(form).getByRole("region", { name: "Travel days" });

    fireEvent.click(within(travel).getByRole("button", { name: "Remove travel day 1" }));
    fireEvent.click(within(travel).getByRole("button", { name: "Remove travel day 1" }));
    expect(within(travel).getByText("No travel days.")).toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    expect(puts(fetchMock)[0]?.body).toMatchObject({ travel_days: [] });
  });

  it("offers no more days than a week has, nor distance exercises", async () => {
    const seven = Array.from({ length: 7 }, (_, index) =>
      day(30 + index, `Day ${String(index + 1)}`, [exercise(40 + index, "Bench Press")]),
    );
    routeFetch(programs(hybrid({ days: seven })));
    const form = await openEditor();
    const gym = within(form).getByRole("region", { name: "Gym days" });

    expect(within(gym).queryByLabelText("Add a gym day starting with")).not.toBeInTheDocument();
    const travel = within(form).getByRole("region", { name: "Travel days" });
    const choices = within(travel)
      .getByLabelText("Add a travel day starting with")
      .querySelectorAll("option");
    expect([...choices].map((option) => option.textContent)).toEqual([
      "Add a travel day starting with…",
      "Bench Press",
      "Cable Row",
      "Push-up",
      "Dead Hang",
    ]);
  });

  it("ignores a pick that is not an exercise", async () => {
    routeFetch(programs(hybrid()));
    const form = await openEditor();
    const gym = within(form).getByRole("region", { name: "Gym days" });

    fireEvent.change(within(gym).getByLabelText("Add a gym day starting with"), {
      target: { value: "" },
    });

    expect(within(gym).queryByLabelText("Gym day 3 name")).not.toBeInTheDocument();
  });
});
