# Fix Front Scripts And Draft Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the frontend reliably load generated concepts/scripts and make `Draft Video with <creator>` start a real pipeline run using the selected creator.

**Architecture:** Add a durable run-detail API that hydrates UI state from the LangGraph checkpoint and runtime gates, then let SSE continue applying live deltas. Reuse the existing LangGraph pipeline for creator drafts by seeding `node_roster` with an existing creator instead of building a new roster.

**Tech Stack:** FastAPI, LangGraph `AsyncSqliteSaver`, React/Vite/TypeScript, existing Python pytest suite, `npm run build`.

**Execution status (2026-07-06):** Tasks 1-6 implemented. Focused backend
regressions, full Python suite with 100% coverage, and frontend production build
pass. Optional browser smoke was not run in this session; the flow was verified
through endpoint/function tests and `tsc`/Vite build.

---

## Current Findings

- `front/src/api/useRunStream.ts` only reads `/api/stream/{run_id}`. That stream exists only while `src/orchestrator/web/server.py::_runs` has the run in memory. A checkpointed or completed run can exist in SQLite but still render empty on `/scripts`.
- The backend currently exposes `GET /api/status/{run_id}`, but it returns only `runner.summarize(...)`, not the item-level concepts/scripts needed by the Concepts screen.
- `front/src/screens/Creators.tsx` renders `Draft Video with {selected.id}` with no `onClick`; it does not call the backend, pass the selected creator, or navigate to the script review flow.
- The graph already supports the right high-level sequence: `concepts -> scripts -> concept_review -> roster -> approval -> fan-out`. The missing piece is a way for `roster` to use an existing creator.

## File Structure

- Modify `src/orchestrator/web/server.py`: add durable run-state endpoint, seed-creator request fields, seed-creator lookup, and pass seed data into `_execute_run`.
- Modify `src/orchestrator/nodes/stages.py`: make `node_roster` return a seeded creator from `run_cfg` before building new creators.
- Modify `front/src/types.ts`: add run-detail response fields and `StartRunBody` fields for config and selected creator.
- Modify `front/src/api/client.ts`: add `getRunState(...)`; send selected creator fields in `startRun`.
- Modify `front/src/api/useRunStream.ts`: hydrate from run-state endpoint before/alongside SSE.
- Modify `front/src/api/useRunSelection.ts`: accept a preferred run id so `/scripts?run=...` can select the freshly created draft.
- Modify `front/src/screens/Concepts.tsx`: read `run` query param and pass it into `useRunSelection`.
- Modify `front/src/screens/Creators.tsx`: make the drawer button start a draft run with selected creator.
- Modify `front/src/screens/CampaignDetail.tsx`: when phase is `editing`, show a direct action to review concepts/scripts.
- Modify `tests/test_web_endpoints.py`: cover run-state hydration and draft run startup contracts.
- Modify `tests/test_stages_coverage.py` or `tests/test_builder.py`: cover seeded roster behavior.
- Modify `docs/PROGRESS.md`: record symptom, cause, and correction per project rule.
- Modify `docs/DEMO.md` and/or `README.md`: document the frontend flow after it works.

---

### Task 1: Add A Durable Run-State Endpoint

**Files:**
- Modify: `src/orchestrator/web/server.py`
- Test: `tests/test_web_endpoints.py`

- [ ] **Step 1: Write failing tests for checkpoint hydration**

Append tests like this to `tests/test_web_endpoints.py`:

```python
async def test_run_state_returns_checkpoint_items_with_scripts(tmp_path, monkeypatch):
    run_id = "web-state-scripts"
    db = tmp_path / "runs.sqlite"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    await web_server._execute_run(
        run_id=run_id,
        offer="serum X",
        batch=2,
        platform="tiktok",
        config_dir="config-mock",
        db_path=str(db),
        approve_creators=False,
        edit_concepts=False,
    )
    web_server._runs.pop(run_id, None)

    out = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

    assert out["run_id"] == run_id
    assert out["phase"] == "done"
    assert out["items"]
    assert all(item["script"] for item in out["items"])
    assert all(item["concept"] for item in out["items"])
```

Add a second test for a live edit gate:

```python
async def test_run_state_returns_pending_concepts_during_edit_gate(tmp_path, monkeypatch):
    run_id = "web-state-editing"
    db = tmp_path / "runs.sqlite"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    task = asyncio.create_task(
        web_server._execute_run(
            run_id=run_id,
            offer="serum X",
            batch=2,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(db),
            approve_creators=False,
            edit_concepts=True,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if "concept_edit" in web_server._runs[run_id]:
                break
            assert not task.done()
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("run did not pause for concept editing")

        out = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

        assert out["phase"] == "editing"
        assert len(out["edit_concepts"]) == 2
        assert all(c.get("script") for c in out["edit_concepts"])

        web_server._runs[run_id]["concept_edit"].set_result(
            {"concepts": out["edit_concepts"][:1]}
        )
        await asyncio.wait_for(task, timeout=8.0)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
```

- [ ] **Step 2: Run tests and confirm red**

Run:

```bash
rtk proxy python -m pytest --no-cov tests/test_web_endpoints.py::test_run_state_returns_checkpoint_items_with_scripts tests/test_web_endpoints.py::test_run_state_returns_pending_concepts_during_edit_gate
```

Expected: both fail because `web_server.run_state` does not exist.

- [ ] **Step 3: Implement minimal endpoint**

In `src/orchestrator/web/server.py`, add helpers near the existing status helpers:

```python
def _item_payload_from_result(item: Any) -> dict[str, Any]:
    return _safe_serialize(_complete_item_payload(_snapshot_from_item(item)))


def _runtime_phase(state: dict[str, Any] | None, summary: dict[str, Any] | None) -> str:
    if state and state.get("concept_edit") and not state.get("done"):
        return "editing"
    if state and state.get("approval") and not state.get("done"):
        return "awaiting"
    if state and not state.get("done"):
        return "running"
    if summary is not None:
        return "done" if summary.get("in_flight", 0) == 0 else "running"
    return "idle"
```

Then add an endpoint:

```python
@app.get("/api/state/{run_id}")
async def run_state(
    run_id: str,
    config_dir: Optional[str] = None,
    db: Optional[str] = None,
) -> dict[str, Any]:
    runtime = _runs.get(run_id)
    pipeline = load_pipeline(config_dir)
    db_path = db or str(default_db_path())
    checkpoint = await runner.get_status(pipeline, db_path=db_path, run_id=run_id)

    if runtime is None and checkpoint is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    summary = runner.summarize({**checkpoint, "run_id": run_id}) if checkpoint else None
    items_by_id: dict[str, dict[str, Any]] = {}
    for item in (checkpoint or {}).get("results") or []:
        payload = _item_payload_from_result(item)
        if payload.get("id"):
            items_by_id[str(payload["id"])] = payload
    for snapshot in (runtime or {}).get("item_snapshots", {}).values():
        payload = _safe_serialize(_complete_item_payload(snapshot))
        if payload.get("id"):
            items_by_id[str(payload["id"])] = payload

    return {
        "run_id": run_id,
        "phase": _runtime_phase(runtime, summary),
        "items": list(items_by_id.values()),
        "edit_concepts": list((runtime or {}).get("pending_concepts") or []),
        "awaiting": list((runtime or {}).get("pending_creators") or []),
        "summary": summary,
    }
```

- [ ] **Step 4: Run endpoint tests green**

Run:

```bash
rtk proxy python -m pytest --no-cov tests/test_web_endpoints.py::test_run_state_returns_checkpoint_items_with_scripts tests/test_web_endpoints.py::test_run_state_returns_pending_concepts_during_edit_gate
```

Expected: both pass.

---

### Task 2: Hydrate Frontend Run State Before SSE

**Files:**
- Modify: `front/src/types.ts`
- Modify: `front/src/api/client.ts`
- Modify: `front/src/api/useRunStream.ts`
- Test: `front` TypeScript build

- [ ] **Step 1: Add frontend contract types**

In `front/src/types.ts`, add:

```ts
export type RunPhaseSnapshot = "idle" | "running" | "editing" | "awaiting" | "done" | "error";

export interface RunDetail {
  run_id: string;
  phase: RunPhaseSnapshot;
  items: Item[];
  edit_concepts: EditableConcept[];
  awaiting: Creator[];
  summary: RunSummary | null;
}
```

- [ ] **Step 2: Add API method**

In `front/src/api/client.ts`, import `RunDetail` and add:

```ts
getRunState: (runId: string) =>
  req<RunDetail>(`/api/state/${encodeURIComponent(runId)}`),
```

- [ ] **Step 3: Hydrate `useRunStream`**

In `front/src/api/useRunStream.ts`, import `api` and `RunDetail`. Extend the action type:

```ts
type Action =
  | { kind: "event"; ev: StreamEvent }
  | { kind: "hydrate"; detail: RunDetail }
  | { kind: "reset" };
```

Add reducer logic:

```ts
function hydrate(detail: RunDetail): RunStreamState {
  const items = Object.fromEntries(detail.items.map((item) => [item.id, item]));
  return {
    ...initial,
    phase: detail.phase,
    items,
    editConcepts: detail.edit_concepts,
    awaiting: detail.awaiting,
    summary: detail.summary,
    log: detail.items.length
      ? [{ kind: "state", text: "loaded saved run state", ts: Date.now() }]
      : [],
  };
}

function rootReducer(s: RunStreamState, a: Action): RunStreamState {
  if (a.kind === "reset") return initial;
  if (a.kind === "hydrate") return hydrate(a.detail);
  return reduce(s, a.ev);
}
```

Then, inside the `useEffect` for `runId`, fetch state before opening SSE:

```ts
let cancelled = false;
api.getRunState(runId)
  .then((detail) => {
    if (!cancelled) dispatch({ kind: "hydrate", detail });
  })
  .catch(() => {
    /* active brand-new runs may not have a checkpoint yet; SSE remains source of truth */
  });
```

Set `cancelled = true` in cleanup before closing the EventSource.

- [ ] **Step 4: Verify frontend build**

Run:

```bash
cd front && rtk npm run build
```

Expected: TypeScript and Vite build pass.

---

### Task 3: Support Seeded Creator Runs In The Backend

**Files:**
- Modify: `src/orchestrator/nodes/stages.py`
- Modify: `src/orchestrator/web/server.py`
- Test: `tests/test_stages_coverage.py`
- Test: `tests/test_web_endpoints.py`

- [ ] **Step 1: Write failing unit test for seeded roster**

Add to `tests/test_stages_coverage.py`:

```python
async def test_node_roster_uses_seed_creator_without_building_new_creator(pipeline_cfg):
    from orchestrator.nodes.stages import node_roster

    class BoomAdapter:
        async def build_creator(self, *args, **kwargs):
            raise AssertionError("seeded roster must not build a new creator")

    seed = {
        "id": "creator-fixed",
        "image_uri": "/media/old/creator-fixed/image.png",
        "voice_ref": "/media/old/creator-fixed/voice.wav",
        "voice_preview_uri": "/media/old/creator-fixed/voice.wav",
        "angles": ["front"],
    }
    cfg = {
        "configurable": {
            "adapter": BoomAdapter(),
            "pipeline": pipeline_cfg,
            "run": {"seed_creator": seed},
            "thread_id": "run-seeded",
        }
    }

    out = await node_roster({"run_id": "run-seeded"}, cfg)

    assert out["roster"] == [
        {
            "id": "creator-fixed",
            "upscaled_base": "/media/old/creator-fixed/image.png",
            "image_uri": "/media/old/creator-fixed/image.png",
            "image": "/media/old/creator-fixed/image.png",
            "image_source_uri": "/media/old/creator-fixed/image.png",
            "voice_id": "/media/old/creator-fixed/voice.wav",
            "voice_ref": "/media/old/creator-fixed/voice.wav",
            "voice": "/media/old/creator-fixed/voice.wav",
            "voice_preview_uri": "/media/old/creator-fixed/voice.wav",
            "angles": ["front"],
        }
    ]
```

- [ ] **Step 2: Implement seed normalization in `node_roster`**

At the start of `node_roster`, after `run_cfg` is read, add:

```python
seed_creator = run_cfg.get("seed_creator")
if isinstance(seed_creator, dict) and seed_creator.get("id"):
    image_uri = (
        seed_creator.get("image_uri")
        or seed_creator.get("image")
        or seed_creator.get("upscaled_base")
    )
    voice_ref = (
        seed_creator.get("voice_ref")
        or seed_creator.get("voice")
        or seed_creator.get("voice_id")
    )
    return {
        "roster": [{
            "id": seed_creator.get("id"),
            "upscaled_base": image_uri,
            "image_uri": image_uri,
            "image": image_uri,
            "image_source_uri": image_uri,
            "voice_id": voice_ref,
            "voice_ref": voice_ref,
            "voice": voice_ref,
            "voice_preview_uri": seed_creator.get("voice_preview_uri"),
            "angles": list(seed_creator.get("angles") or []),
        }]
    }
```

- [ ] **Step 3: Add web request fields and resolver**

In `src/orchestrator/web/server.py`, extend `RunRequest`:

```python
    config_dir: Optional[str] = None
    creator_id: Optional[str] = None
    creator_run_id: Optional[str] = None
```

Add resolver:

```python
def _find_creator_for_draft(creator_id: str, creator_run_id: Optional[str]) -> dict[str, Any]:
    candidates = creator_store.load_creators(default_creator_store_path())
    if not candidates:
        candidates = _recover_creators_from_media(default_media_path())
    for creator in candidates:
        same_id = creator.get("id") == creator_id or creator.get("creator_id") == creator_id
        same_run = creator_run_id is None or creator.get("run_id") == creator_run_id
        if same_id and same_run:
            return _normalize_creator(creator)
    raise HTTPException(status_code=404, detail=f"creator {creator_id!r} not found")
```

Extend `_execute_run` signature with `seed_creator: Optional[dict[str, Any]] = None` and put it into `cfg["configurable"]["run"]`:

```python
"seed_creator": seed_creator,
```

In `start_run`, resolve and pass it:

```python
seed_creator = (
    _find_creator_for_draft(req.creator_id, req.creator_run_id)
    if req.creator_id
    else None
)
background_tasks.add_task(
    _execute_run,
    run_id,
    req.offer,
    req.batch,
    req.platform,
    req.config_dir,
    db_path,
    req.creator_prompt,
    req.video_prompt,
    req.approve_creators,
    req.edit_concepts,
    seed_creator,
)
```

- [ ] **Step 4: Add integration test for seeded run**

Add to `tests/test_web_endpoints.py`:

```python
async def test_execute_run_with_seed_creator_uses_selected_creator(tmp_path, monkeypatch):
    run_id = "web-seeded-creator"
    db = tmp_path / "runs.sqlite"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    await web_server._execute_run(
        run_id=run_id,
        offer="serum X",
        batch=1,
        platform="tiktok",
        config_dir="config-mock",
        db_path=str(db),
        approve_creators=False,
        edit_concepts=False,
        seed_creator={
            "id": "creator-fixed",
            "image_uri": "data:image/png;base64,IMG",
            "voice_ref": "data:audio/wav;base64,VOICE",
            "voice_preview_uri": "data:audio/wav;base64,VOICE",
            "angles": ["front"],
        },
    )

    out = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

    assert out["items"][0]["creator_ref"] == "creator-fixed"
```

- [ ] **Step 5: Run backend tests green**

Run:

```bash
rtk proxy python -m pytest --no-cov tests/test_stages_coverage.py::test_node_roster_uses_seed_creator_without_building_new_creator tests/test_web_endpoints.py::test_execute_run_with_seed_creator_uses_selected_creator
```

Expected: both pass.

---

### Task 4: Make `Draft Video` Start A Real Run

**Files:**
- Modify: `front/src/types.ts`
- Modify: `front/src/screens/Creators.tsx`
- Modify: `front/src/api/useRunSelection.ts`
- Modify: `front/src/screens/Concepts.tsx`

- [ ] **Step 1: Extend `StartRunBody`**

In `front/src/types.ts`, add:

```ts
  config_dir?: string | null;
  creator_id?: string | null;
  creator_run_id?: string | null;
```

- [ ] **Step 2: Let screens select a run from query string**

In `front/src/api/useRunSelection.ts`, change the signature:

```ts
export function useRunSelection(preferredRunId?: string | null) {
```

Change the initial selection effect:

```ts
useEffect(() => {
  if (!data || selected) return;
  setSelected(preferredRunId ?? data.active[0] ?? data.runs[0] ?? null);
}, [data, selected, preferredRunId]);
```

In `front/src/screens/Concepts.tsx`, add:

```ts
import { useSearchParams } from "react-router-dom";
```

Then:

```ts
const [searchParams] = useSearchParams();
const preferredRunId = searchParams.get("run");
const { runs, active, selected, setSelected, loading, error } = useRunSelection(preferredRunId);
```

- [ ] **Step 3: Add draft launch state to `Creators.tsx`**

In `front/src/screens/Creators.tsx`, import `useNavigate`, create state:

```ts
const navigate = useNavigate();
const [draftOffer, setDraftOffer] = useState("");
const [drafting, setDrafting] = useState(false);
const [draftError, setDraftError] = useState<string | null>(null);
```

Add launcher:

```ts
async function launchDraft() {
  if (!selected) return;
  setDrafting(true);
  setDraftError(null);
  try {
    const { run_id } = await api.startRun({
      offer: draftOffer.trim() || selected.offer || "creator draft",
      batch: 1,
      platform: "tiktok",
      creator_id: selected.id,
      creator_run_id: selected.run_id ?? null,
      approve_creators: false,
      edit_concepts: true,
    });
    navigate(`/scripts?run=${encodeURIComponent(run_id)}`);
  } catch (err) {
    setDraftError(err instanceof Error ? err.message : "Could not draft video");
  } finally {
    setDrafting(false);
  }
}
```

Replace the drawer footer with:

```tsx
footer={
  <Button icon="movie" className="w-full" disabled={drafting} onClick={launchDraft}>
    {drafting ? "Drafting..." : `Draft Video with ${selected?.id}`}
  </Button>
}
```

Inside the drawer body, add an offer input before the source run:

```tsx
<label className="block">
  <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
    Product / Offer
  </span>
  <input
    value={draftOffer}
    onChange={(e) => setDraftOffer(e.target.value)}
    placeholder={selected.offer || "serum X"}
    className="mt-2 w-full rounded-lg border-surface-border bg-surface-container-lowest font-body-md text-body-md focus:ring-primary focus:border-primary"
  />
</label>
{draftError && (
  <div className="rounded-lg border border-error/40 bg-error/10 px-3 py-2 font-body-md text-body-md text-error">
    {draftError}
  </div>
)}
```

- [ ] **Step 4: Build frontend**

Run:

```bash
cd front && rtk npm run build
```

Expected: TypeScript and Vite build pass.

---

### Task 5: Make The Editing Gate Discoverable From Campaign Detail

**Files:**
- Modify: `front/src/screens/CampaignDetail.tsx`

- [ ] **Step 1: Add editing CTA**

In `CampaignDetail`, before the creator approval panel, add:

```tsx
{run.phase === "editing" && (
  <Card className="mb-gutter border-warning-review/30">
    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
      <div>
        <div className="font-headline-md text-headline-md text-primary">
          Concepts & scripts ready
        </div>
        <div className="font-body-md text-body-md text-on-surface-variant">
          Review, edit, include or exclude scripts before creator generation.
        </div>
      </div>
      <Button icon="description" onClick={() => navigate(`/scripts?run=${encodeURIComponent(runId)}`)}>
        Review Scripts
      </Button>
    </div>
  </Card>
)}
```

- [ ] **Step 2: Build frontend**

Run:

```bash
cd front && rtk npm run build
```

Expected: build passes and text does not overflow in the campaign detail layout.

---

### Task 6: Documentation, Regression Log, Full Verification

**Files:**
- Modify: `docs/PROGRESS.md`
- Modify: `docs/DEMO.md`
- Modify: `README.md` if command docs change

- [ ] **Step 1: Record investigated failure**

In `docs/PROGRESS.md`, add an entry:

```markdown
### Falha investigada: scripts vazios no front + Draft Video inerte

- Sintoma: `/scripts` podia abrir sem conceitos/scripts para runs existentes, e o botão `Draft Video with <creator>` na galeria não disparava nenhuma ação.
- Causa: o front dependia apenas do SSE em memória (`/api/stream/{run_id}`), que não hidrata runs checkpointados; a tela de creators renderizava o botão sem handler/API.
- Correção: endpoint durável `/api/state/{run_id}` hidrata itens/scripts a partir do checkpoint e runtime gates; o front hidrata `useRunStream` antes do SSE; `Draft Video` inicia novo run com `seed_creator`.
```

- [ ] **Step 2: Update demo docs**

In `docs/DEMO.md`, add a short frontend flow:

```markdown
## Dashboard: scripts e Draft Video

1. Abra `/creators`, selecione um creator aprovado e informe a oferta.
2. Clique `Draft Video with creator-*`.
3. A UI inicia um novo run com esse creator fixo e navega para `/scripts?run=<run_id>`.
4. Edite ou exclua conceitos/scripts, clique `Save & Continue`, e o run segue para vídeo/QC/montagem.
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
rtk proxy python -m pytest --no-cov tests/test_web_endpoints.py tests/test_web_item_updates.py tests/test_stages_coverage.py tests/test_builder.py
```

Expected: all selected tests pass.

- [ ] **Step 4: Run full suite and frontend build**

Run:

```bash
rtk proxy python -m pytest
cd front && rtk npm run build
```

Expected: Python suite remains at 100% coverage, and frontend build passes.

- [ ] **Step 5: Manual smoke, if socket access is available**

Run backend and frontend:

```bash
orchestrator serve
cd front && npm run dev
```

Smoke path:

1. Open `/creators`.
2. Select a creator with complete image + voice.
3. Enter an offer and click `Draft Video`.
4. Confirm navigation to `/scripts?run=<run_id>`.
5. Confirm concepts and scripts appear.
6. Edit one script, exclude one concept, click `Save & Continue`.
7. Confirm the run continues and the produced item has the selected `creator_ref`.
