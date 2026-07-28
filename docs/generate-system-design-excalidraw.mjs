import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const output = fileURLToPath(new URL("./system-design.excalidraw", import.meta.url));
let nonce = 1700000000;

const palette = {
  ink: "#1b1b1f",
  muted: "#495057",
  frontend: { stroke: "#1971c2", fill: "#d0ebff" },
  backend: { stroke: "#2b8a3e", fill: "#d3f9d8" },
  data: { stroke: "#6741d9", fill: "#e5dbff" },
  tool: { stroke: "#d9480f", fill: "#ffe8cc" },
  security: { stroke: "#c2255c", fill: "#ffdeeb" },
  cloud: { stroke: "#e67700", fill: "#fff3bf" },
  neutral: { stroke: "#495057", fill: "#f1f3f5" },
};

function common(id, type, x, y, width, height) {
  return {
    id,
    type,
    x,
    y,
    width,
    height,
    angle: 0,
    strokeWidth: 2,
    strokeStyle: "solid",
    roughness: 1,
    opacity: 100,
    groupIds: [],
    frameId: null,
    roundness: type === "rectangle" ? { type: 3 } : null,
    seed: ++nonce,
    version: 1,
    versionNonce: ++nonce,
    isDeleted: false,
    boundElements: [],
    updated: 1784500000000,
    link: null,
    locked: false,
  };
}

function rectangle(id, x, y, width, height, colors, options = {}) {
  return {
    ...common(id, "rectangle", x, y, width, height),
    strokeColor: colors.stroke,
    backgroundColor: options.transparent ? "transparent" : colors.fill,
    fillStyle: "solid",
    strokeStyle: options.dashed ? "dashed" : "solid",
    strokeWidth: options.strokeWidth ?? 2,
    roughness: options.roughness ?? 1,
  };
}

function textElement(id, text, x, y, width, height, options = {}) {
  const fontSize = options.fontSize ?? 20;
  return {
    ...common(id, "text", x, y, width, height),
    strokeColor: options.color ?? palette.ink,
    backgroundColor: "transparent",
    fillStyle: "solid",
    strokeWidth: 1,
    roughness: 0,
    text,
    fontSize,
    fontFamily: options.fontFamily ?? 5,
    textAlign: options.align ?? "center",
    verticalAlign: options.verticalAlign ?? "middle",
    containerId: null,
    originalText: text,
    autoResize: false,
    lineHeight: 1.25,
    baseline: Math.round(fontSize * 0.8),
  };
}

function arrow(id, points, options = {}) {
  const [x, y] = points[0];
  const relative = points.map(([px, py]) => [px - x, py - y]);
  const xs = relative.map(([px]) => px);
  const ys = relative.map(([, py]) => py);
  return {
    ...common(id, "arrow", x, y, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)),
    strokeColor: options.color ?? palette.muted,
    backgroundColor: "transparent",
    fillStyle: "solid",
    strokeStyle: options.dashed ? "dashed" : "solid",
    strokeWidth: options.emphasis ? 3 : 2,
    roughness: 1,
    points: relative,
    lastCommittedPoint: null,
    startBinding: null,
    endBinding: null,
    startArrowhead: null,
    endArrowhead: "arrow",
    elbowed: false,
  };
}

const elements = [];
const boxes = [];
const labels = [];

function section(id, title, x, y, width, height, colors) {
  elements.push(rectangle(`${id}-boundary`, x, y, width, height, colors, {
    transparent: true,
    dashed: true,
    strokeWidth: 2,
  }));
  labels.push(textElement(`${id}-title`, title, x + 18, y + 10, width - 36, 32, {
    fontSize: 22,
    color: colors.stroke,
    align: "left",
  }));
}

function box(id, title, subtitle, x, y, width, height, colors) {
  boxes.push(rectangle(`${id}-box`, x, y, width, height, colors));
  boxes.push(textElement(`${id}-label`, `${title}\n${subtitle}`, x + 10, y + 9, width - 20, height - 18, {
    fontSize: 18,
    color: colors.stroke,
  }));
}

function connection(id, points, label, labelX, labelY, options = {}) {
  elements.push(arrow(`${id}-arrow`, points, options));
  if (label) {
    labels.push(textElement(`${id}-text`, label, labelX, labelY, options.labelWidth ?? 150, 25, {
      fontSize: 15,
      color: options.color ?? palette.muted,
    }));
  }
}

labels.push(textElement("diagram-title", "SYSTEM DESIGN · UGC ORCHESTRATOR", 10, -70, 1100, 48, {
  fontSize: 34,
  color: palette.ink,
  align: "left",
}));
labels.push(textElement("diagram-subtitle", "Arquitetura da aplicação atual — componentes, dependências, DBs e storage", 12, -20, 1150, 32, {
  fontSize: 20,
  color: palette.muted,
  align: "left",
}));

section("clients", "01 · CLIENT SURFACES", 0, 80, 260, 500, palette.frontend);
section("runtime", "02 · PYTHON RUNTIME / OCI IMAGE", 300, 40, 1140, 870, palette.backend);
section("data", "03 · PERSISTENCE (CURRENT)", 320, 620, 1100, 260, palette.data);
section("external", "04 · EXTERNAL + TARGET INFRA", 1480, 40, 390, 870, palette.security);

connection("spa-api", [[230, 315], [350, 315]], "REST + SSE", 225, 278, {
  emphasis: true,
  color: palette.frontend.stroke,
  labelWidth: 130,
});
connection("cli-runner", [[230, 475], [285, 475], [285, 430], [645, 430], [645, 380]], "in-process", 350, 438);
connection("api-runner", [[530, 315], [570, 315]], "BackgroundTasks", 485, 274, {
  emphasis: true,
  color: palette.backend.stroke,
  labelWidth: 170,
});
connection("runner-graph", [[750, 315], [790, 315]], "ainvoke / astream", 700, 274, {
  emphasis: true,
  color: palette.backend.stroke,
  labelWidth: 180,
});
connection("graph-executor", [[980, 315], [1020, 315]], "nodes", 960, 274, {
  emphasis: true,
  color: palette.tool.stroke,
  labelWidth: 90,
});
connection("executor-adapters", [[1210, 315], [1250, 315]], "typed calls", 1165, 274, {
  emphasis: true,
  color: palette.tool.stroke,
  labelWidth: 135,
});
connection("config-runner", [[660, 200], [660, 250]], "load profile", 672, 210, {
  dashed: true,
  color: palette.data.stroke,
  labelWidth: 125,
});
connection("prompts-executor", [[1110, 200], [1110, 250]], "guardrails", 1122, 210, {
  dashed: true,
  color: palette.security.stroke,
  labelWidth: 120,
});
connection("api-memory", [[440, 380], [440, 690]], "queues · replay · gates", 450, 505, {
  dashed: true,
  color: palette.tool.stroke,
  labelWidth: 205,
});
connection("graph-checkpoint", [[885, 380], [885, 555], [665, 555], [665, 690]], "checkpoint / resume", 670, 520, {
  dashed: true,
  color: palette.data.stroke,
  labelWidth: 185,
});
connection("executor-artifacts", [[1115, 380], [1115, 575], [900, 575], [900, 690]], "artifact metadata", 905, 540, {
  dashed: true,
  color: palette.data.stroke,
  labelWidth: 175,
});
connection("api-json", [[440, 380], [440, 530], [1135, 530], [1135, 690]], "CRUD", 760, 496, {
  dashed: true,
  color: palette.data.stroke,
  labelWidth: 80,
});
connection("executor-media", [[1115, 380], [1115, 500], [1335, 500], [1335, 690]], "persist bytes", 1210, 466, {
  dashed: true,
  color: palette.cloud.stroke,
  labelWidth: 145,
});
connection("adapters-gateway", [[1430, 315], [1510, 315], [1510, 165], [1580, 165]], "HTTPS", 1460, 210, {
  emphasis: true,
  color: palette.cloud.stroke,
  labelWidth: 85,
});
connection("adapters-replicate", [[1430, 315], [1580, 365]], "models", 1465, 335, {
  emphasis: true,
  color: palette.cloud.stroke,
  labelWidth: 90,
});
connection("adapters-eleven", [[1430, 315], [1530, 315], [1530, 565], [1580, 565]], "voice", 1500, 455, {
  color: palette.cloud.stroke,
  labelWidth: 80,
});
connection("adapters-langsmith", [[1430, 315], [1550, 315], [1550, 745], [1580, 745]], "trace (opt-in)", 1515, 650, {
  dashed: true,
  color: palette.security.stroke,
  labelWidth: 145,
});

box("spa", "React SPA", "Vite · dashboard", 40, 280, 190, 70, palette.frontend);
box("cli", "CLI", "Click · async commands", 40, 440, 190, 70, palette.frontend);
box("api", "FastAPI + SSE", "REST · stream · static", 350, 280, 180, 100, palette.backend);
box("runner", "Runner", "run · resume · status", 570, 280, 180, 100, palette.backend);
box("graph", "LangGraph", "batch + item subgraph", 790, 280, 190, 100, palette.backend);
box("executor", "Stage Executor", "typed tools · validation", 1020, 280, 190, 100, palette.tool);
box("adapters", "CompositeAdapter", "role → provider", 1250, 280, 180, 100, palette.backend);
box("config", "Configuration", "pipeline · providers · agents", 570, 120, 180, 80, palette.data);
box("prompts", "Agent Catalog", "prompts · allowlists", 1020, 120, 180, 80, palette.security);

box("runtime-memory", "Runtime memory", "SSE queues · replay · gates", 340, 690, 200, 95, palette.tool);
box("runs-db", "runs.sqlite", "LangGraph checkpoints", 570, 690, 190, 95, palette.data);
box("artifacts-db", "artifacts.sqlite", "metadata · hash · retention", 800, 690, 200, 95, palette.data);
box("json-stores", "JSON stores", "creators · prompts · feedback", 1040, 690, 190, 95, palette.data);
box("media-store", "MediaStorage", "Local FS / R2-S3", 1260, 690, 160, 95, palette.cloud);

box("gateway", "Vercel AI Gateway", "LLM · image · assembly", 1580, 125, 220, 80, palette.cloud);
box("replicate", "Replicate", "creator · video · voice", 1580, 325, 220, 80, palette.cloud);
box("elevenlabs", "ElevenLabs", "voice synthesis", 1580, 525, 220, 80, palette.cloud);
box("langsmith", "LangSmith", "optional tracing", 1580, 705, 220, 80, palette.cloud);
box("postgres", "PostgreSQL", "scaffold · NOT CONNECTED", 1580, 815, 220, 70, palette.neutral);

labels.push(textElement("current-note", "CURRENT TRUTH\nAPI + Runner share one process. No durable job queue. Runtime approvals and SSE replay live in memory.", 0, 960, 580, 105, {
  fontSize: 18,
  color: palette.backend.stroke,
  align: "left",
}));
labels.push(textElement("db-note", "DATABASE CONTRACTS\nruns.sqlite = checkpoints · artifacts.sqlite = canonical media metadata · JSON = creators/prompts/feedback", 640, 960, 600, 105, {
  fontSize: 18,
  color: palette.data.stroke,
  align: "left",
}));
labels.push(textElement("storage-note", "STORAGE MODES\nMock → local filesystem · Live → private R2/S3 · signed URLs are created only at the API boundary.", 1300, 960, 570, 105, {
  fontSize: 18,
  color: palette.cloud.stroke,
  align: "left",
}));

elements.push(...boxes, ...labels);

const document = {
  type: "excalidraw",
  version: 2,
  source: "ugc-orchestrator-system-design",
  elements,
  appState: {
    gridSize: null,
    viewBackgroundColor: "#ffffff",
    currentItemFontFamily: 5,
  },
  files: {},
};

writeFileSync(output, `${JSON.stringify(document, null, 2)}\n`, "utf8");
console.log(`wrote ${output} (${elements.length} elements)`);
