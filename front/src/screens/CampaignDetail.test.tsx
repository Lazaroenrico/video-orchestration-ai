import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HttpError } from "../api/client";
import type { Creator, EditableConcept, GateRef } from "../types";

const reviewMutation = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  isPending: false,
}));

vi.mock("../api/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/queries")>();
  return {
    ...actual,
    useReviewRunV2Mutation: () => reviewMutation,
  };
});

import { CreativeReviewPanel } from "./CampaignDetail";

const concepts: EditableConcept[] = [
  {
    id: "concept-1",
    hook: "Hook",
    angle: "Angle",
    script: "Script",
  },
];
const creators: Creator[] = [
  {
    id: "creator-1",
    angles: ["front"],
    archetype: "Creator",
    performance_style: "Direct",
  },
];
const gate: GateRef = {
  gate_id: "gate-1",
  version: 1,
  gate_type: "review_creative_plan",
};

function renderPanel(currentGate: GateRef = gate) {
  return render(
    <CreativeReviewPanel
      key={`${currentGate.gate_id}:${currentGate.version}`}
      runId="web-test"
      initialConcepts={concepts}
      initialCreators={creators}
      gate={currentGate}
    />,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("CreativeReviewPanel", () => {
  beforeEach(() => {
    reviewMutation.mutateAsync.mockReset();
    reviewMutation.isPending = false;
  });

  it("submits only once and locks every review action immediately", async () => {
    const request = deferred<{ ok: boolean }>();
    reviewMutation.mutateAsync.mockReturnValue(request.promise);
    renderPanel();

    const approve = screen.getByRole("button", {
      name: /Aprovar e produzir/,
    });
    fireEvent.click(approve);
    fireEvent.click(approve);

    expect(reviewMutation.mutateAsync).toHaveBeenCalledTimes(1);
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
    }

    request.resolve({ ok: true });
    expect(
      await screen.findByText("Revisão enviada. Iniciando a próxima etapa..."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Aprovar e produzir/ }),
    ).not.toBeInTheDocument();
  });

  it("unlocks the actions after a non-conflict failure", async () => {
    reviewMutation.mutateAsync
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValueOnce({ ok: true });
    renderPanel();

    const approve = screen.getByRole("button", {
      name: /Aprovar e produzir/,
    });
    fireEvent.click(approve);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "network unavailable",
    );
    expect(approve).toBeEnabled();

    fireEvent.click(approve);
    await waitFor(() =>
      expect(reviewMutation.mutateAsync).toHaveBeenCalledTimes(2),
    );
  });

  it("treats a stale gate conflict as an already processed review", async () => {
    reviewMutation.mutateAsync.mockRejectedValue(
      new HttpError(
        409,
        "gate não corresponde à revisão pendente deste run",
      ),
    );
    renderPanel();

    fireEvent.click(
      screen.getByRole("button", { name: /Aprovar e produzir/ }),
    );

    expect(
      await screen.findByText(
        "Esta revisão já foi processada. Atualizando a campanha...",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("unlocks when React mounts the panel for a new gate version", async () => {
    reviewMutation.mutateAsync.mockResolvedValue({ ok: true });
    const view = renderPanel();

    fireEvent.click(
      screen.getByRole("button", { name: /Aprovar e produzir/ }),
    );
    await screen.findByText("Revisão enviada. Iniciando a próxima etapa...");

    const nextGate = { ...gate, gate_id: "gate-2", version: 2 };
    view.rerender(
      <CreativeReviewPanel
        key={`${nextGate.gate_id}:${nextGate.version}`}
        runId="web-test"
        initialConcepts={concepts}
        initialCreators={creators}
        gate={nextGate}
      />,
    );

    expect(
      screen.getByRole("button", { name: /Aprovar e produzir/ }),
    ).toBeEnabled();
  });
});
