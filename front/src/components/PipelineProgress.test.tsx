import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RunProgress } from "../types";
import { PipelineProgress } from "./PipelineProgress";

const progress: RunProgress = {
  execution_status: "running",
  active_stage_ids: ["scripts", "talking_head", "qc"],
  updated_at: "2026-07-28T10:01:00+00:00",
  items: [],
  stages: [
    {
      id: "setup",
      label: "Configuração",
      parent_id: null,
      status: "completed",
      completed_units: 1,
      active_units: 0,
      failed_units: 0,
      total_units: 1,
      updated_at: "2026-07-28T10:00:00+00:00",
    },
    {
      id: "creative_plan",
      label: "Plano criativo",
      parent_id: null,
      status: "running",
      completed_units: 0,
      active_units: 0,
      failed_units: 0,
      total_units: 1,
      updated_at: null,
    },
    {
      id: "concepts",
      label: "Criando conceitos",
      parent_id: "creative_plan",
      status: "completed",
      completed_units: 1,
      active_units: 0,
      failed_units: 0,
      total_units: 1,
      updated_at: null,
    },
    {
      id: "scripts",
      label: "Escrevendo roteiros",
      parent_id: "creative_plan",
      status: "running",
      completed_units: 4,
      active_units: 1,
      failed_units: 0,
      total_units: 12,
      updated_at: null,
    },
    {
      id: "review",
      label: "Revisão",
      parent_id: null,
      status: "pending",
      completed_units: 0,
      active_units: 0,
      failed_units: 0,
      total_units: 1,
      updated_at: null,
    },
    {
      id: "production",
      label: "Produção e QC",
      parent_id: null,
      status: "running",
      completed_units: 2,
      active_units: 2,
      failed_units: 0,
      total_units: 4,
      updated_at: null,
    },
    {
      id: "talking_head",
      label: "Talking-head",
      parent_id: "production",
      status: "running",
      completed_units: 1,
      active_units: 2,
      failed_units: 0,
      total_units: 4,
      updated_at: null,
    },
    {
      id: "qc",
      label: "Controle de qualidade",
      parent_id: "production",
      status: "running",
      completed_units: 1,
      active_units: 1,
      failed_units: 0,
      total_units: 4,
      updated_at: null,
    },
    {
      id: "assembly",
      label: "Montagem",
      parent_id: null,
      status: "pending",
      completed_units: 0,
      active_units: 0,
      failed_units: 0,
      total_units: 4,
      updated_at: null,
    },
  ],
};

describe("PipelineProgress", () => {
  it("shows five public phases, nested work, and granular counters", () => {
    render(<PipelineProgress progress={progress} />);

    expect(screen.getByLabelText("Configuração: completed")).toBeInTheDocument();
    expect(screen.getByLabelText("Criando conceitos: completed")).toBeInTheDocument();
    expect(screen.getByText("4/12")).toBeInTheDocument();
    expect(screen.getByText("2 clips in Talking-head")).toBeInTheDocument();
    expect(screen.getByText("1 clip in Controle de qualidade")).toBeInTheDocument();
    expect(screen.getByText("2 of 4 clips complete")).toBeInTheDocument();
  });

  it("does not describe batch stages as clips", () => {
    render(
      <PipelineProgress
        progress={{
          ...progress,
          active_stage_ids: ["concepts"],
          stages: progress.stages.map((stage) =>
            stage.id === "concepts"
              ? { ...stage, status: "running", active_units: 1, completed_units: 0 }
              : stage,
          ),
        }}
      />,
    );

    expect(screen.getByText("Criando conceitos in progress")).toBeInTheDocument();
    expect(screen.queryByText("1 clip in Criando conceitos")).not.toBeInTheDocument();
  });
});
